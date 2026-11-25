from sqlmodel import Session, select

from app.models import STEP_ORDER, Project, StepKey, StepSpace, StepStatus
from app.services import evidence


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
