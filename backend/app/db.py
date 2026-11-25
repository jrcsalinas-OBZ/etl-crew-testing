from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Role, User


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///"):
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})


OBSOLETE_COLUMNS = {"project": ("source_platform", "target_platform")}  # removed 2026-11-25; projects are just names now


def _drop_obsolete_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    for table, columns in OBSOLETE_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for column in columns:
                if column in existing:
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))


# Columns added to existing tables after first release: create_all() only creates missing *tables*.
ADDED_COLUMNS = {
    "project": [("discovery_skills_json", "VARCHAR NOT NULL DEFAULT '[]'")],
    "artifact": [("run_id", "INTEGER REFERENCES discoveryrun (id)"), ("doc_type", "VARCHAR NOT NULL DEFAULT ''")],
}


def _add_missing_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    for table, columns in ADDED_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for column, ddl in columns:
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db(engine: Engine) -> None:
    _drop_obsolete_columns(engine)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    with Session(engine) as session:
        if session.exec(select(User)).first() is None:
            session.add_all([
                User(name="Owner", role=Role.owner),
                User(name="Author A", role=Role.author),
                User(name="Approver B", role=Role.approver),
            ])
            session.commit()
