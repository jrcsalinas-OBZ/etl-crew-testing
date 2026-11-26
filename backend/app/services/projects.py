import logging
import shutil

from fastapi import HTTPException
from sqlmodel import Session, delete, select

from app.config import Settings
from app.models import (STEP_ORDER, Artifact, ChatMessage, ChatSession, DiscoveryRun, Handoff, KnowledgeFile, Project,
                        Role, SessionFile, StepKey, StepSpace, StepStatus, User)
from app.services import evidence

log = logging.getLogger(__name__)


def create_project(session: Session, *, name: str, description: str = "", user_id: int) -> Project:
    project = Project(name=name, description=description, created_by=user_id)
    session.add(project)
    session.flush()
    evidence.record(session, project_id=project.id, actor_id=user_id, action="project.create",
                    target_type="project", target_id=project.id, after={"name": name})
    for key in StepKey:
        session.add(StepSpace(
            project_id=project.id, key=key, order=STEP_ORDER[key],
            status=StepStatus.open if key == StepKey.discover else StepStatus.locked,
        ))
    session.commit()
    session.refresh(project)
    return project


def list_projects(session: Session) -> list[Project]:
    return list(session.exec(select(Project).order_by(Project.id)))


def get_steps(session: Session, project_id: int) -> list[StepSpace]:
    return list(session.exec(select(StepSpace).where(StepSpace.project_id == project_id).order_by(StepSpace.order)))


def delete_project(session: Session, settings: Settings, project: Project, user: User) -> None:
    """Remove a project, its sessions, files, runs and artifacts (DB rows and files on disk).
    Evidence rows are append-only and are kept; a final project.delete row records who deleted what."""
    if user.id != project.created_by and user.role != Role.owner:
        raise HTTPException(403, "Only the person who created this project or an owner can delete it.")
    if session.exec(select(DiscoveryRun).where(DiscoveryRun.project_id == project.id,
                                               DiscoveryRun.status == "running")).first():
        raise HTTPException(409, "A discovery run is in progress. Wait for it to finish, then delete the project.")

    pid = project.id
    session_ids = list(session.exec(select(ChatSession.id).where(ChatSession.project_id == pid)))
    counts = {
        "sessions": len(session_ids),
        "files": len(session.exec(select(KnowledgeFile.id).where(KnowledgeFile.project_id == pid)).all()),
        "runs": len(session.exec(select(DiscoveryRun.id).where(DiscoveryRun.project_id == pid)).all()),
        "artifacts": len(session.exec(select(Artifact.id).where(Artifact.project_id == pid)).all()),
        "jira_tickets_left_in_jira": [h.jira_key for h in session.exec(select(Handoff).where(Handoff.project_id == pid))
                                      if h.jira_key],
    }
    evidence.record(session, project_id=pid, actor_id=user.id, action="project.delete", target_type="project",
                    target_id=pid, before={"name": project.name, **counts}, after=None)
    # children first, so foreign keys never point at a deleted row
    session.exec(delete(Handoff).where(Handoff.project_id == pid))
    if session_ids:
        session.exec(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
        session.exec(delete(SessionFile).where(SessionFile.session_id.in_(session_ids)))
    session.exec(delete(Artifact).where(Artifact.project_id == pid))
    session.exec(delete(DiscoveryRun).where(DiscoveryRun.project_id == pid))
    session.exec(delete(ChatSession).where(ChatSession.project_id == pid))
    session.exec(delete(KnowledgeFile).where(KnowledgeFile.project_id == pid))
    session.exec(delete(StepSpace).where(StepSpace.project_id == pid))
    session.delete(project)
    session.commit()

    folder = settings.data_dir / "projects" / str(pid)
    try:
        if folder.exists():
            shutil.rmtree(folder)
    except OSError:  # the DB is already consistent; leftover files are harmless and logged
        log.exception("could not remove %s", folder)
