from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    owner = "owner"
    author = "author"
    approver = "approver"
    viewer = "viewer"


class StepKey(str, Enum):
    discover = "discover"
    plan = "plan"
    design = "design"


STEP_ORDER = {StepKey.discover: 1, StepKey.plan: 2, StepKey.design: 3}


class StepStatus(str, Enum):
    locked = "locked"
    open = "open"
    awaiting_approval = "awaiting_approval"
    approved = "approved"


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    role: Role


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: str = ""
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    discovery_skills_json: str = "[]"  # ordered skill keys; [] = use the defaults in config/discovery-skills.yml


class DiscoveryRun(SQLModel, table=True):
    """One execution of the project's discovery skill chain on a session's documents."""

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    session_id: int = Field(foreign_key="chatsession.id")
    number: int  # 1, 2, 3… within the project
    skills_json: str  # [{key, name, sha256, produces}] pinned at start
    status: str = "running"  # running | completed | failed
    approval: str = "pending"  # pending | approved | rejected | superseded | n/a (failed runs)
    current_skill: str = ""
    error: str = ""
    started_by: int = Field(foreign_key="user.id")
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: Optional[datetime] = None
    approved_by: Optional[int] = Field(default=None, foreign_key="user.id")
    approved_at: Optional[datetime] = None
    rejection_reason: str = ""


class Handoff(SQLModel, table=True):
    """Jira ticket that hands an approved discovery run to ETL Crew (create → attach → assign to the lead)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="discoveryrun.id", unique=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    label: str  # idempotency label on the Jira issue
    status: str = "pending"  # pending | sent | failed | not_configured
    jira_key: str = ""
    jira_url: str = ""
    created_issue: bool = False
    attached: bool = False
    assigned: bool = False
    error: str = ""
    attempts: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class CrewDispatch(SQLModel, table=True):
    """The run's ETL Crew ticket sent straight to the crew's opencode server (session + prompt to the lead agent)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="discoveryrun.id", unique=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    ticket_key: str = ""  # the Jira key, or DUMMY-<run id> when no Jira issue exists
    artifact: str = ""  # CREW_TICKET-2026-001@v1
    status: str = "failed"  # sent | failed
    session_id: str = ""
    session_url: str = ""
    error: str = ""
    attempts: int = 0
    sent_by: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_at: datetime = Field(default_factory=utcnow)


class StepSpace(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    key: StepKey
    order: int
    status: StepStatus
    instructions_md: str = ""
    instructions_version: int = 0
    author_id: Optional[int] = Field(default=None, foreign_key="user.id")
    approved_by: Optional[int] = Field(default=None, foreign_key="user.id")
    approved_at: Optional[datetime] = None
    rejection_reason: str = ""


class KnowledgeFile(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("project_id", "sha256", name="uq_project_sha256"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    step: Optional[StepKey] = None  # None = engagement-wide
    filename: str
    sha256: str
    size_bytes: int
    mime: str = ""
    stored_path: str
    uploaded_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    flagged_injection: bool = False
    extracted_path: str = ""  # plain-text rendering of the file, what the model reads
    extracted_chars: int = 0
    extract_note: str = ""  # why extraction failed or was partial


class ChatSession(SQLModel, table=True):
    """One conversation inside a step. Its context is its own files + messages."""

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    step: StepKey
    title: str
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)


class ChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="chatsession.id", index=True)
    role: str  # user | assistant
    kind: str = "text"  # text | confirmation | deliverable | note
    content: str
    attachments_json: str = "[]"  # [{file_id, filename, duplicate}]
    actions_json: str = "[]"  # buttons shown on the latest confirmation, e.g. ["start", "later"]
    artifact_id: Optional[int] = Field(default=None, foreign_key="artifact.id")
    created_at: datetime = Field(default_factory=utcnow)


class SessionFile(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("session_id", "file_id", name="uq_session_file"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="chatsession.id", index=True)
    file_id: int = Field(foreign_key="knowledgefile.id")
    added_at: datetime = Field(default_factory=utcnow)


class Artifact(SQLModel, table=True):
    """Agent drafts. Status is always 'draft' when written by a run."""

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    session_id: int = Field(foreign_key="chatsession.id", index=True)
    step: StepKey
    artifact_id: str  # e.g. DISC-2026-001
    artifact_type: str
    title: str
    version: int = 1
    status: str = "draft"  # draft (one-off) | in_review | approved | rejected | superseded (discovery runs)
    path: str
    run_id: Optional[int] = Field(default=None, foreign_key="discoveryrun.id", index=True)
    doc_type: str = ""  # INTENT, DISC, INV, … for discovery-run artifacts
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)


class Evidence(SQLModel, table=True):
    """Append-only. No update/delete endpoints, ever."""

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    at: datetime = Field(default_factory=utcnow)
    actor_id: int
    action: str
    target_type: str
    target_id: str
    before_json: str = "null"
    after_json: str = "null"
    hash: str = ""
