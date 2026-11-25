import json
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import Settings
from app.db import init_db, make_engine
from app.models import (Artifact, ChatMessage, ChatSession, DiscoveryRun, KnowledgeFile, Project, StepKey, StepSpace,
                        User)
from app.services import (chat, discovery, evidence, gates, handoff, knowledge, local_files, projects, skill_upload,
                          skills)
from app.services.jira import JiraConfig
from app.services.runner import StepRunner, get_runner


class SessionIn(BaseModel):
    step: StepKey
    title: str = ""


class RunIn(BaseModel):
    mode: str = "chat"  # "skill" = run one skill; "start" = general step draft; "chat" = reply to the last message
    skill: str = ""  # "<pack>:<name>" when mode == "skill"
    instruction: str = ""


class RejectIn(BaseModel):
    reason: str


class SkillsIn(BaseModel):
    skills: list[str]  # ordered skill keys for the project's discovery runs


class PathsIn(BaseModel):
    paths: list[str]  # file or folder paths, e.g. from Explorer's "Copy as path"
    text: str = ""


class ProjectIn(BaseModel):
    name: str


class UploadOut(BaseModel):
    file: KnowledgeFile
    duplicate: bool
    message: str


class ProjectDetail(BaseModel):
    project: Project
    steps: list[StepSpace]


def create_app(settings: Optional[Settings] = None, runner: Optional[StepRunner] = None,
               inline_runs: bool = False, jira_factory: Optional[handoff.JiraFactory] = None,
               jira_config: Callable[[], JiraConfig] = JiraConfig.from_env) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    init_db(engine)
    discovery.recover_interrupted(engine)
    runner = runner or get_runner(settings)
    live = discovery.LiveRuns()
    executor = discovery.RunExecutor(inline=inline_runs)

    app = FastAPI(title="Trinity Workbench")
    app.state.settings = settings
    app.state.engine = engine
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"])

    def get_session(request: Request):
        with Session(request.app.state.engine) as session:
            yield session

    def acting_user(session: Session = Depends(get_session),
                    x_user_id: Optional[int] = Header(default=None)) -> User:
        user = session.get(User, x_user_id) if x_user_id else session.exec(select(User).order_by(User.id)).first()
        if user is None:
            raise HTTPException(401, "Unknown acting user")
        return user

    def require_project(project_id: int, session: Session) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        return project

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/users")
    def list_users(session: Session = Depends(get_session)) -> list[User]:
        return list(session.exec(select(User).order_by(User.id)))

    @app.get("/api/projects")
    def list_projects(session: Session = Depends(get_session)) -> list[Project]:
        return projects.list_projects(session)

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn, session: Session = Depends(get_session),
                       user: User = Depends(acting_user)) -> Project:
        if not body.name.strip():
            raise HTTPException(422, "Project name is required")
        return projects.create_project(session, name=body.name.strip(), user_id=user.id)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: int, session: Session = Depends(get_session)) -> ProjectDetail:
        project = require_project(project_id, session)
        return ProjectDetail(project=project, steps=projects.get_steps(session, project_id))

    @app.post("/api/projects/{project_id}/files", status_code=201)
    async def upload_file(project_id: int, file: UploadFile = File(...), step: Optional[StepKey] = Form(default=None),
                          session: Session = Depends(get_session), user: User = Depends(acting_user)) -> UploadOut:
        require_project(project_id, session)
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(413, "File too large")
        row, duplicate = knowledge.store_upload(
            session, settings, project_id=project_id, step=step, filename=file.filename or "upload",
            content=content, mime=file.content_type or "", user_id=user.id)
        message = f"Already uploaded as {row.filename}" if duplicate else f"Uploaded {row.filename}"
        return UploadOut(file=row, duplicate=duplicate, message=message)

    @app.get("/api/projects/{project_id}/files")
    def list_files(project_id: int, step: Optional[StepKey] = None,
                   session: Session = Depends(get_session)) -> list[KnowledgeFile]:
        require_project(project_id, session)
        return knowledge.list_files(session, project_id, step)

    # ---- config ---------------------------------------------------------------------------------
    @app.get("/api/config")
    def get_config() -> dict:
        active = runner.current() if hasattr(runner, "current") else runner
        return {"runner": active.name, "model": getattr(active, "model", None)}

    @app.get("/api/skills")
    def list_skills() -> dict:
        indexed = skills.index_skills(settings)
        counts: dict[str, int] = {}
        for s in indexed:
            counts[s.pack] = counts.get(s.pack, 0) + 1
        return {
            "skills": [{"key": s.key, "name": s.name, "pack": s.pack, "description": s.description, "group": s.group,
                        "runnable": s.runnable, "needs": s.needs, "sha256": s.sha256} for s in indexed],
            "counts": counts,
            "suggestions": skills.suggestions(settings),
            "warning": None if counts.get("data-migration") else
            "No data-migration skills found. Copy the plugin's skills/ folder into skills/data-migration/ (see skills/README.md).",
        }

    # ---- chat sessions (one per conversation, scoped to a step) --------------------------------
    def session_detail(session_id: int, session: Session) -> dict:
        row = chat.require_session(session, session_id)
        messages = session.exec(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id))
        artifacts = session.exec(select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.id))
        step = chat.get_step(session, row.project_id, row.step)
        return {
            "session": row.model_dump(mode="json"),
            "step_status": step.status,
            "messages": [m.model_dump(mode="json") for m in messages],
            "files": [{"id": f.id, "filename": f.filename, "size_bytes": f.size_bytes, "readable": bool(f.extracted_path),
                       "flagged_injection": f.flagged_injection, "note": f.extract_note}
                      for f in chat.session_files(session, session_id)],
            "artifacts": [a.model_dump(mode="json") for a in artifacts],
        }

    @app.post("/api/projects/{project_id}/sessions", status_code=201)
    def create_session(project_id: int, body: SessionIn, session: Session = Depends(get_session),
                       user: User = Depends(acting_user)) -> dict:
        require_project(project_id, session)
        row = chat.create_session(session, project_id=project_id, step=body.step, title=body.title, user_id=user.id)
        return session_detail(row.id, session)

    @app.get("/api/projects/{project_id}/sessions")
    def list_sessions(project_id: int, session: Session = Depends(get_session)) -> list[ChatSession]:
        require_project(project_id, session)
        return list(session.exec(select(ChatSession).where(ChatSession.project_id == project_id).order_by(ChatSession.id)))

    @app.get("/api/sessions/{session_id}")
    def get_session_detail(session_id: int, session: Session = Depends(get_session)) -> dict:
        return session_detail(session_id, session)

    @app.post("/api/sessions/{session_id}/messages", status_code=201)
    async def post_message(session_id: int, text: str = Form(default=""), files: list[UploadFile] = File(default=[]),
                           session: Session = Depends(get_session), user: User = Depends(acting_user)) -> dict:
        row = chat.require_session(session, session_id)
        if not text.strip() and not files:
            raise HTTPException(422, "Write a message or attach a file.")
        if files:
            uploads = [(f.filename or "upload", await f.read(settings.max_upload_bytes + 1), f.content_type or "") for f in files]
            user_msg, confirmation = chat.receive_files(session, settings, row, uploads, user_text=text.strip(), user_id=user.id)
            return {"messages": [user_msg.model_dump(mode="json"), confirmation.model_dump(mode="json")], "needs_reply": False}
        chat.ensure_step_open(session, row)
        msg = chat.add_message(session, session_id, "user", text.strip())
        session.commit()
        session.refresh(msg)
        return {"messages": [msg.model_dump(mode="json")], "needs_reply": True}

    @app.post("/api/sessions/{session_id}/messages/paths", status_code=201)
    def post_message_from_paths(session_id: int, body: PathsIn, session: Session = Depends(get_session),
                                user: User = Depends(acting_user)) -> dict:
        """Add files by local path — for browsers that can't open a file dialog. The app runs on the user's own machine."""
        row = chat.require_session(session, session_id)
        uploads, skipped = local_files.read_paths(body.paths, settings.max_upload_bytes)
        note = body.text.strip() + (("\n\nSkipped: " + "; ".join(skipped)) if skipped else "")
        user_msg, confirmation = chat.receive_files(session, settings, row, uploads, user_text=note.strip(), user_id=user.id)
        return {"messages": [user_msg.model_dump(mode="json"), confirmation.model_dump(mode="json")], "needs_reply": False}

    @app.post("/api/sessions/{session_id}/decline", status_code=201)
    def decline_start(session_id: int, session: Session = Depends(get_session), user: User = Depends(acting_user)) -> dict:
        row = chat.require_session(session, session_id)
        return {"messages": [m.model_dump(mode="json") for m in chat.decline_start(session, row, user.id)]}

    @app.post("/api/sessions/{session_id}/run")
    def run_session(session_id: int, body: RunIn, session: Session = Depends(get_session),
                    user: User = Depends(acting_user)) -> StreamingResponse:
        row = chat.require_session(session, session_id)
        chat.prepare_run(session, settings, row, body.mode, body.skill, body.instruction)

        def events():
            for event in chat.stream_run(engine, settings, runner, session_id, body.mode, user.id, body.skill):
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- artifacts ------------------------------------------------------------------------------
    def require_artifact(artifact_id: int, session: Session) -> Artifact:
        art = session.get(Artifact, artifact_id)
        if art is None:
            raise HTTPException(404, "Artifact not found")
        return art

    @app.get("/api/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: int, session: Session = Depends(get_session)) -> FileResponse:
        art = require_artifact(artifact_id, session)
        return FileResponse(art.path, media_type="text/markdown", filename=f"{art.artifact_id}-v{art.version}.md")

    @app.get("/api/artifacts/{artifact_id}")
    def get_artifact(artifact_id: int, session: Session = Depends(get_session)) -> dict:
        art = require_artifact(artifact_id, session)
        content = Path(art.path).read_text(encoding="utf-8") if Path(art.path).exists() else ""
        return {"artifact": art.model_dump(mode="json"), "content": content}

    # ---- discovery ------------------------------------------------------------------------------
    def require_run(run_id: int, session: Session) -> DiscoveryRun:
        run = session.get(DiscoveryRun, run_id)
        if run is None:
            raise HTTPException(404, "Discovery run not found")
        return run

    @app.get("/api/projects/{project_id}/discovery")
    def get_discovery(project_id: int, session: Session = Depends(get_session)) -> dict:
        project = require_project(project_id, session)
        return {
            "status": discovery.project_status(session, project_id),
            "skills": discovery.catalogue_view(settings, project),
            "selected": discovery.selected_keys(settings, project),
            "runs": [discovery.run_summary(session, r, live) for r in discovery.runs_for(session, project_id)],
        }

    @app.put("/api/projects/{project_id}/discovery/skills")
    def set_discovery_skills(project_id: int, body: SkillsIn, session: Session = Depends(get_session),
                             user: User = Depends(acting_user)) -> dict:
        project = require_project(project_id, session)
        return {"selected": discovery.set_selection(session, settings, project, body.skills, user.id)}

    @app.post("/api/skills/upload", status_code=201)
    async def upload_skill(file: UploadFile = File(...), project_id: int = Form(...), replace: bool = Form(default=False),
                           session: Session = Depends(get_session), user: User = Depends(acting_user)) -> dict:
        require_project(project_id, session)
        content = await file.read(skill_upload.MAX_TOTAL_BYTES + 1)
        if len(content) > skill_upload.MAX_TOTAL_BYTES:
            raise HTTPException(413, "Skill uploads are limited to 5 MB.")
        name = skill_upload.install(settings, file.filename or "", content, replace)
        evidence.record(session, project_id=project_id, actor_id=user.id, action="skill.upload", target_type="skill",
                        target_id=f"custom:{name}", after={"filename": file.filename, "replace": replace})
        session.commit()
        return {"key": f"custom:{name}", "name": name}

    @app.post("/api/sessions/{session_id}/discovery", status_code=202)
    def start_discovery(session_id: int, session: Session = Depends(get_session), user: User = Depends(acting_user)) -> dict:
        row = chat.require_session(session, session_id)
        run = discovery.start_run(session, settings, row, user.id)
        executor.submit(lambda: discovery.execute_run(engine, settings, runner, live, run.id))
        session.expire_all()
        return discovery.run_summary(session, session.get(DiscoveryRun, run.id), live)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int, session: Session = Depends(get_session)) -> dict:
        return discovery.run_summary(session, require_run(run_id, session), live)

    @app.post("/api/runs/{run_id}/approve")
    def approve_run(run_id: int, session: Session = Depends(get_session), user: User = Depends(acting_user)) -> dict:
        run = discovery.approve(session, run_id, user)
        handoff.ensure_record(session, run)  # visible as "pending" straight away
        executor.submit(lambda: handoff.send(engine, settings, run_id, jira_factory, jira_config))  # approval → Jira → ETL Crew
        session.expire_all()
        return discovery.run_summary(session, session.get(DiscoveryRun, run_id))

    @app.get("/api/runs/{run_id}/handoff/preview")
    def preview_handoff(run_id: int, session: Session = Depends(get_session)) -> dict:
        run = require_run(run_id, session)
        if run.status != "completed":
            raise HTTPException(409, "Only a completed discovery run has a hand-off ticket.")
        ticket = handoff.render(session, settings, run)
        return {"summary": ticket.summary, "description": ticket.description, "labels": ticket.labels,
                "attachments": [name for name, _ in ticket.attachments],
                "jira_configured": not jira_config().missing()}

    @app.post("/api/runs/{run_id}/handoff/retry", status_code=202)
    def retry_handoff(run_id: int, session: Session = Depends(get_session), user: User = Depends(acting_user)) -> dict:
        run = handoff.retryable(session, run_id)
        evidence.record(session, project_id=run.project_id, actor_id=user.id, action="handoff.retry",
                        target_type="discovery_run", target_id=run.id)
        session.commit()
        executor.submit(lambda: handoff.send(engine, settings, run_id, jira_factory, jira_config))
        session.expire_all()
        return discovery.run_summary(session, session.get(DiscoveryRun, run_id))

    @app.post("/api/runs/{run_id}/reject")
    def reject_run(run_id: int, body: RejectIn, session: Session = Depends(get_session),
                   user: User = Depends(acting_user)) -> dict:
        return discovery.run_summary(session, discovery.reject(session, run_id, user, body.reason))

    # ---- gates (legacy step gates; the UI now uses discovery-run approval) ----------------------
    @app.post("/api/projects/{project_id}/steps/{step}/submit")
    def submit_step(project_id: int, step: StepKey, session: Session = Depends(get_session),
                    user: User = Depends(acting_user)) -> StepSpace:
        require_project(project_id, session)
        return gates.submit(session, project_id, step, user)

    @app.post("/api/projects/{project_id}/steps/{step}/approve")
    def approve_step(project_id: int, step: StepKey, session: Session = Depends(get_session),
                     user: User = Depends(acting_user)) -> StepSpace:
        require_project(project_id, session)
        return gates.approve(session, project_id, step, user)

    @app.post("/api/projects/{project_id}/steps/{step}/reject")
    def reject_step(project_id: int, step: StepKey, body: RejectIn, session: Session = Depends(get_session),
                    user: User = Depends(acting_user)) -> StepSpace:
        require_project(project_id, session)
        return gates.reject(session, project_id, step, user, body.reason)

    return app
