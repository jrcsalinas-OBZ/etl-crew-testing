"""`make seed` — creates the demo project. Idempotent."""
from sqlmodel import Session, select

from app.config import Settings
from app.db import init_db, make_engine
from app.models import Project, Role, User
from app.services import projects

DEMO_NAME = "Telco X — Network Feeds (demo)"


def seed(settings: Settings | None = None) -> Project:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        existing = session.exec(select(Project).where(Project.name == DEMO_NAME)).first()
        if existing:
            return existing
        author = session.exec(select(User).where(User.role == Role.author)).first()
        return projects.create_project(session, name=DEMO_NAME, user_id=author.id)


if __name__ == "__main__":
    project = seed()
    print(f"Seeded project {project.id}: {project.name}")
