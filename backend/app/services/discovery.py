"""Discovery runs: a project's chosen skill chain (default intent → discover) run on a session's documents,
producing versioned Trinity artifacts that wait for four-eyes approval."""
import json
import logging
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml
from fastapi import HTTPException
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import Settings
from app.models import Artifact, ChatSession, DiscoveryRun, Project, Role, User
from app.services import chat, evidence, handoff, skills
from app.services.prompts import discovery_system_prompt
from app.services.runner import RunInput, RunnerError, StepRunner

log = logging.getLogger(__name__)
MARKER = re.compile(r"^\s*=== ARTIFACT: ([A-Z][A-Z0-9_]*) ===\s*$", re.MULTILINE)


# ---- catalogue and per-project selection --------------------------------------------------------

@dataclass(frozen=True)
class DiscoverySkill:
    key: str
    name: str
    description: str
    note: str
    produces: list[str]
    source: str  # core (on by default) | optional | custom (uploaded)
    available: bool  # the SKILL.md is on disk and runnable
    needs: str


def artifact_prefix(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "SKILL"


def _config(settings: Settings) -> dict:
    path = settings.config_dir / "discovery-skills.yml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def catalogue(settings: Settings) -> list[DiscoverySkill]:
    config = _config(settings)
    defaults = config.get("default") or []
    indexed = {s.key: s for s in skills.index_skills(settings)}
    out: list[DiscoverySkill] = []
    for item in config.get("skills") or []:
        s = indexed.get(item["skill"])
        out.append(DiscoverySkill(
            key=item["skill"], name=s.name if s else item["skill"].split(":", 1)[-1],
            description=s.description if s else "", note=item.get("note", ""), produces=list(item["produces"]),
            source="core" if item["skill"] in defaults else "optional", available=bool(s and s.runnable),
            needs=s.needs if s else ""))
    for s in indexed.values():
        if s.pack == "custom" and s.runnable:
            out.append(DiscoverySkill(key=s.key, name=s.name, description=s.description, note="Uploaded skill",
                                      produces=[artifact_prefix(s.name)], source="custom", available=True, needs=s.needs))
    return out


def selected_keys(settings: Settings, project: Project) -> list[str]:
    available = {s.key for s in catalogue(settings) if s.available}
    chosen = [k for k in json.loads(project.discovery_skills_json or "[]") if k in available]
    return chosen or [k for k in (_config(settings).get("default") or []) if k in available]


def selected_names(settings: Settings, project: Project) -> list[str]:
    by_key = {s.key: s.name for s in catalogue(settings)}
    return [by_key[k] for k in selected_keys(settings, project)]


def set_selection(db: Session, settings: Settings, project: Project, keys: list[str], user_id: int) -> list[str]:
    available = {s.key for s in catalogue(settings) if s.available}
    if not keys:
        raise HTTPException(422, "Choose at least one skill for discovery.")
    if len(set(keys)) != len(keys):
        raise HTTPException(422, "Each skill can be selected once.")
    unknown = [k for k in keys if k not in available]
    if unknown:
        raise HTTPException(422, f"Not available for discovery: {', '.join(unknown)}")
    evidence.record(db, project_id=project.id, actor_id=user_id, action="discovery.skills_set", target_type="project",
                    target_id=project.id, before=json.loads(project.discovery_skills_json or "[]"), after=keys)
    project.discovery_skills_json = json.dumps(keys)
    db.add(project)
    db.commit()
    return keys


# ---- live progress (in memory, per API process) ------------------------------------------------

class LiveRuns:
    """Partial output of the skill currently running, so the UI can show text as it streams."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[int, dict] = {}

    def start(self, run_id: int, skill: str) -> None:
        with self._lock:
            self._runs[run_id] = {"skill": skill, "text": ""}

    def append(self, run_id: int, text: str) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id]["text"] += text

    def get(self, run_id: int) -> Optional[dict]:
        with self._lock:
            entry = self._runs.get(run_id)
            return dict(entry) if entry else None

    def clear(self, run_id: int) -> None:
        with self._lock:
            self._runs.pop(run_id, None)


class RunExecutor:
    """Runs discovery in a background thread so it survives the browser tab closing. inline=True for tests."""

    def __init__(self, inline: bool = False):
        self.inline = inline

    def submit(self, work: Callable[[], None]) -> None:
        if self.inline:
            work()
        else:
            threading.Thread(target=work, daemon=True, name="discovery-run").start()


# ---- status ------------------------------------------------------------------------------------

def runs_for(db: Session, project_id: int) -> list[DiscoveryRun]:
    return list(db.exec(select(DiscoveryRun).where(DiscoveryRun.project_id == project_id).order_by(DiscoveryRun.id.desc())))


def project_status(db: Session, project_id: int) -> str:
    """not_started | running | failed | awaiting_approval | approved | rejected"""
    latest = db.exec(select(DiscoveryRun).where(DiscoveryRun.project_id == project_id)
                     .order_by(DiscoveryRun.id.desc())).first()
    if latest is None:
        return "not_started"
    if latest.status != "completed":
        return latest.status
    if latest.approval == "approved":
        sent = handoff.view(db, latest)
        return "sent_to_etl_crew" if sent and sent["status"] == "sent" else "approved"
    return {"pending": "awaiting_approval"}.get(latest.approval, latest.approval)


def recover_interrupted(engine: Engine) -> None:
    """A restart kills the worker thread; mark runs that were mid-flight as failed."""
    with Session(engine) as db:
        for run in db.exec(select(DiscoveryRun).where(DiscoveryRun.status == "running")):
            run.status, run.approval, run.error = "failed", "n/a", "Interrupted: the server restarted during this run."
            run.ended_at = datetime.now(timezone.utc)
            db.add(run)
        db.commit()


# ---- starting and executing a run --------------------------------------------------------------

def start_run(db: Session, settings: Settings, row: ChatSession, user_id: int) -> DiscoveryRun:
    chat.ensure_step_open(db, row)
    project = db.get(Project, row.project_id)
    if db.exec(select(DiscoveryRun).where(DiscoveryRun.project_id == project.id, DiscoveryRun.status == "running")).first():
        raise HTTPException(409, "A discovery run is already in progress for this project.")
    if not chat.readable(chat.session_files(db, row.id)):
        raise HTTPException(409, "Upload at least one readable document to this session before running discovery.")
    by_key = {s.key: s for s in catalogue(settings)}
    pinned = []
    for key in selected_keys(settings, project):
        info = skills.get_skill(settings, key)
        pinned.append({"key": key, "name": info.name, "sha256": info.sha256, "produces": by_key[key].produces})
    if not pinned:
        raise HTTPException(409, "No discovery skills are available. Check skills/README.md.")

    number = len(runs_for(db, project.id)) + 1
    run = DiscoveryRun(project_id=project.id, session_id=row.id, number=number, skills_json=json.dumps(pinned),
                       started_by=user_id)
    db.add(run)
    db.flush()
    evidence.record(db, project_id=project.id, actor_id=user_id, action="discovery.run_start", target_type="discovery_run",
                    target_id=run.id, after={"number": number, "skills": pinned})
    chain = " → ".join(p["name"] for p in pinned)
    chat.add_message(db, row.id, "user", f"Run discovery #{number}: **{chain}**")
    db.commit()
    db.refresh(run)
    return run


def split_artifacts(text: str, produces: list[str]) -> list[tuple[str, str]]:
    """Split skill output on '=== ARTIFACT: TYPE ===' markers. Unknown markers fold into the previous section;
    output without markers becomes the first expected type."""
    matches = [m for m in MARKER.finditer(text) if m.group(1) in produces]
    if not matches:
        return [(produces[0], text.strip())]
    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        sections[match.group(1)] = (sections.get(match.group(1), "") + "\n\n" + body).strip()
    preamble = text[:matches[0].start()].strip()
    if preamble:
        first = matches[0].group(1)
        sections[first] = (preamble + "\n\n" + sections[first]).strip()
    return [(t, sections[t]) for t in produces if t in sections]


def _artifact_dir(settings: Settings, project_id: int) -> Path:
    path = settings.data_dir / "projects" / str(project_id) / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_version(db: Session, settings: Settings, run: DiscoveryRun, session_row: ChatSession, doc_type: str, body: str,
                 skill: dict, parent: str) -> Artifact:
    """Same artifact id across runs (INTENT-2026-001), new version each run."""
    latest = db.exec(select(Artifact).where(Artifact.project_id == run.project_id, Artifact.doc_type == doc_type)
                     .order_by(Artifact.version.desc())).first()
    if latest:
        artifact_id, version = latest.artifact_id, latest.version + 1
    else:
        taken = {a.artifact_id for a in db.exec(select(Artifact).where(Artifact.project_id == run.project_id))}
        n = 1
        while f"{doc_type}-{datetime.now(timezone.utc).year}-{n:03d}" in taken:
            n += 1
        artifact_id, version = f"{doc_type}-{datetime.now(timezone.utc).year}-{n:03d}", 1
    path = _artifact_dir(settings, run.project_id) / f"{artifact_id}@v{version}.md"
    path.write_text(
        f"---\nartifact-id: {artifact_id}\nartifact-type: {doc_type.lower()}\nversion: {version}\n"
        f"parent-artifact: {parent or 'none'}\nstatus: draft\ntrace-id: TRN-P{run.project_id}-R{run.number}\n"
        f"approved-by:\nskill: {skill['key']}\nskill-sha256: {skill['sha256']}\ndiscovery-run: {run.number}\n---\n\n{body}\n",
        encoding="utf-8")
    art = Artifact(project_id=run.project_id, session_id=session_row.id, step=session_row.step, artifact_id=artifact_id,
                   artifact_type=doc_type.lower(), title=f"{artifact_id} v{version}", version=version, status="draft",
                   path=str(path), created_by=run.started_by, run_id=run.id, doc_type=doc_type)
    db.add(art)
    db.flush()
    return art


def execute_run(engine: Engine, settings: Settings, runner: StepRunner, live: LiveRuns, run_id: int) -> None:
    with Session(engine) as db:
        run = db.get(DiscoveryRun, run_id)
        row = db.get(ChatSession, run.session_id)
        documents, notes, _ = chat.build_documents(chat.session_files(db, row.id))
        produced: list[Artifact] = []
        outputs: list[tuple[str, str]] = []  # (filename, markdown) from earlier skills, fed forward
        try:
            for spec in json.loads(run.skills_json):
                run.current_skill = spec["name"]
                db.add(run)
                db.commit()
                info = skills.get_skill(settings, spec["key"])
                if info is None or info.sha256 != spec["sha256"]:
                    raise RunnerError(f"The '{spec['name']}' skill changed or was removed after this run started.")
                earlier = [name for name, _ in outputs]
                extra = "".join(f'\n\n<document name="{name}" type=".md" source="earlier skill in this run">\n{text}\n</document>'
                                for name, text in outputs)
                run_input = RunInput(
                    system=discovery_system_prompt(info.name, skills.skill_text(info), spec["produces"], earlier),
                    documents=(documents + extra).strip(),
                    turns=[("user", f"Run the {info.name} skill on the attached documents and produce: "
                                    f"{', '.join(spec['produces'])}.")],
                    skill_name=info.name, artifact_types=tuple(spec["produces"]))
                live.start(run.id, info.name)
                parts = []
                for chunk in runner.stream(run_input):
                    parts.append(chunk)
                    live.append(run.id, chunk)
                text = "".join(parts)
                if not text.strip():
                    raise RunnerError(f"The '{info.name}' skill returned no output.")
                parent = produced[0].artifact_id if produced else ""
                saved = [save_version(db, settings, run, row, t, body, spec, parent)
                         for t, body in split_artifacts(text, spec["produces"])]
                produced.extend(saved)
                outputs.extend((f"{a.artifact_id}.md", Path(a.path).read_text(encoding="utf-8")) for a in saved)
                note = ("_Context note: " + "; ".join(notes) + "._\n\n") if notes and not earlier else ""
                chat.add_message(db, row.id, "assistant", note + text, kind="deliverable", artifact_id=saved[0].id)
                db.commit()
        except Exception as exc:  # noqa: BLE001 — any failure ends the run; never leave it "running"
            if not isinstance(exc, RunnerError):
                log.exception("discovery run %s failed", run_id)
            message = str(exc) if isinstance(exc, RunnerError) else "Unexpected error — see the server log."
            db.rollback()
            run = db.get(DiscoveryRun, run_id)
            evidence.record(db, project_id=run.project_id, actor_id=run.started_by, action="discovery.run_fail",
                            target_type="discovery_run", target_id=run.id, after={"error": message})
            run.status, run.approval, run.error = "failed", "n/a", message
            run.ended_at, run.current_skill = datetime.now(timezone.utc), ""
            db.add(run)
            chat.add_message(db, run.session_id, "assistant", f"❌ Discovery #{run.number} failed: {message}", kind="note")
            db.commit()
            live.clear(run_id)
            return

        evidence.record(db, project_id=run.project_id, actor_id=run.started_by, action="discovery.run_complete",
                        target_type="discovery_run", target_id=run.id,
                        after={"artifacts": [f"{a.artifact_id}@v{a.version}" for a in produced], "approval": "pending"})
        for older in db.exec(select(DiscoveryRun).where(DiscoveryRun.project_id == run.project_id, DiscoveryRun.id != run.id,
                                                        DiscoveryRun.approval == "pending", DiscoveryRun.status == "completed")):
            _set_run_approval(db, older, "superseded")
        run.status, run.current_skill, run.ended_at = "completed", "", datetime.now(timezone.utc)
        for art in produced:
            _set_artifact_status(art, "in_review")
            db.add(art)
        db.add(run)
        files = ", ".join(f"**{a.artifact_id}** v{a.version}" for a in produced)
        chat.add_message(db, run.session_id, "assistant",
                         f"🟡 Discovery #{run.number} is done and **waiting for approval**. Generated: {files}. "
                         "An approver can review it under **Runs**.", kind="note")
        db.commit()
        live.clear(run_id)


# ---- approval ----------------------------------------------------------------------------------

def _set_artifact_status(art: Artifact, status: str, approved_by: str = "") -> None:
    art.status = status
    path = Path(art.path)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    head, sep, rest = text.partition("\n---\n")
    head = re.sub(r"^status: .*$", f"status: {status}", head, count=1, flags=re.MULTILINE)
    if approved_by:
        head = re.sub(r"^approved-by:.*$", f"approved-by: {approved_by}", head, count=1, flags=re.MULTILINE)
    path.write_text(head + sep + rest, encoding="utf-8")


def _set_run_approval(db: Session, run: DiscoveryRun, approval: str, approved_by: str = "") -> None:
    run.approval = approval
    db.add(run)
    for art in db.exec(select(Artifact).where(Artifact.run_id == run.id)):
        _set_artifact_status(art, approval if approval != "pending" else "in_review", approved_by)
        db.add(art)


def _reviewable(db: Session, run_id: int, user: User) -> DiscoveryRun:
    run = db.get(DiscoveryRun, run_id)
    if run is None:
        raise HTTPException(404, "Discovery run not found")
    if run.status != "completed" or run.approval != "pending":
        raise HTTPException(409, f"Discovery #{run.number} is not waiting for approval ({run.status}, {run.approval}).")
    if user.id == run.started_by:
        raise HTTPException(403, "The person who started a discovery run cannot approve or reject it.")
    if user.role not in (Role.approver, Role.owner):
        raise HTTPException(403, "Only an approver or owner can review a discovery run.")
    return run


def approve(db: Session, run_id: int, user: User) -> DiscoveryRun:
    run = _reviewable(db, run_id, user)
    evidence.record(db, project_id=run.project_id, actor_id=user.id, action="discovery.approve",
                    target_type="discovery_run", target_id=run.id, before={"approval": "pending"}, after={"approval": "approved"})
    for older in db.exec(select(DiscoveryRun).where(DiscoveryRun.project_id == run.project_id, DiscoveryRun.id != run.id,
                                                    DiscoveryRun.approval == "approved")):
        _set_run_approval(db, older, "superseded")
    _set_run_approval(db, run, "approved", user.name)
    run.approved_by, run.approved_at = user.id, datetime.now(timezone.utc)
    chat.add_message(db, run.session_id, "assistant",
                     f"✅ Discovery #{run.number} approved by {user.name}. Creating the ETL Crew Jira ticket…", kind="note")
    db.commit()
    db.refresh(run)
    return run


def reject(db: Session, run_id: int, user: User, reason: str) -> DiscoveryRun:
    if not reason.strip():
        raise HTTPException(422, "A rejection reason is required.")
    run = _reviewable(db, run_id, user)
    evidence.record(db, project_id=run.project_id, actor_id=user.id, action="discovery.reject",
                    target_type="discovery_run", target_id=run.id, before={"approval": "pending"},
                    after={"approval": "rejected", "reason": reason.strip()})
    _set_run_approval(db, run, "rejected")
    run.rejection_reason = reason.strip()
    chat.add_message(db, run.session_id, "assistant",
                     f"↩️ Discovery #{run.number} was rejected by {user.name}: _{reason.strip()}_. "
                     "Add or change documents, then run discovery again.", kind="note")
    db.commit()
    db.refresh(run)
    return run


# ---- read models -------------------------------------------------------------------------------

def run_summary(db: Session, run: DiscoveryRun, live: Optional[LiveRuns] = None) -> dict:
    users = {u.id: u.name for u in db.exec(select(User))}
    artifacts = db.exec(select(Artifact).where(Artifact.run_id == run.id).order_by(Artifact.id))
    data = run.model_dump(mode="json")
    data.update(
        skills=[s["name"] for s in json.loads(run.skills_json)],
        started_by_name=users.get(run.started_by, "?"), approved_by_name=users.get(run.approved_by, ""),
        artifacts=[{"id": a.id, "artifact_id": a.artifact_id, "version": a.version, "doc_type": a.doc_type,
                    "status": a.status} for a in artifacts],
        live=live.get(run.id) if live and run.status == "running" else None,
        handoff=handoff.view(db, run))
    data.pop("skills_json", None)
    return data


def catalogue_view(settings: Settings, project: Project) -> list[dict]:
    chosen = selected_keys(settings, project)
    return [{**asdict(s), "selected": s.key in chosen, "order": chosen.index(s.key) if s.key in chosen else None}
            for s in catalogue(settings)]
