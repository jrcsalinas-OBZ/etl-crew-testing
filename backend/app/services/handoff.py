"""Hand an approved discovery run to ETL Crew: render the Jira template, create the issue, attach the approved
files, then assign it to the ETL Crew lead — assignment is what triggers ETL Crew, so it always happens last."""
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import Settings
from app.models import Artifact, ChatSession, DiscoveryRun, Handoff, Project, User
from app.services import chat, evidence
from app.services.jira import JiraClient, JiraConfig, JiraError
from app.services.skills import split_frontmatter

log = logging.getLogger(__name__)
TEMPLATE = Path("jira") / "etl-crew-discovery-ticket.md"
MAX_DESCRIPTION = 30_000  # Jira's limit is 32,767 characters
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
JiraFactory = Callable[[JiraConfig], JiraClient]


@dataclass(frozen=True)
class Ticket:
    summary: str
    description: str
    labels: list[str]
    attachments: list[tuple[str, bytes]]


def label_for(run: DiscoveryRun, artifacts: list[Artifact]) -> str:
    seed = f"{run.project_id}|{run.id}|" + "|".join(f"{a.artifact_id}@v{a.version}" for a in artifacts)
    return f"trinity-disc-{hashlib.sha256(seed.encode()).hexdigest()[:10]}"


def _fill(text: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), text)


def render(db: Session, settings: Settings, run: DiscoveryRun) -> Ticket:
    template_path = settings.config_dir / TEMPLATE
    if not template_path.exists():
        raise HTTPException(500, f"Jira template missing: config/{TEMPLATE.as_posix()}")
    meta, body = split_frontmatter(template_path.read_text(encoding="utf-8"))
    artifacts = list(db.exec(select(Artifact).where(Artifact.run_id == run.id).order_by(Artifact.id)))
    project = db.get(Project, run.project_id)
    approver = db.get(User, run.approved_by) if run.approved_by else None
    label = label_for(run, artifacts)
    files = chat.session_files(db, run.session_id)
    intent = next((a for a in artifacts if a.doc_type == "INTENT"), artifacts[0] if artifacts else None)
    intent_body = split_frontmatter(Path(intent.path).read_text(encoding="utf-8"))[1].strip() if intent else ""

    values = {
        "project_name": project.name, "run_number": str(run.number), "approved_by": approver.name if approver else "",
        "approved_at": run.approved_at.strftime("%Y-%m-%d %H:%M UTC") if run.approved_at else "",
        "skills": " → ".join(s["name"] for s in json.loads(run.skills_json)),
        "source_files": ", ".join(f.filename for f in files) or "(none)",
        "artifact_list": "\n".join(f"- {a.artifact_id} v{a.version} ({a.doc_type}) — attached as {a.artifact_id}-v{a.version}.md"
                                   for a in artifacts),
        "intent_id": f"{intent.artifact_id} v{intent.version}" if intent else "(no intent)", "handoff_label": label,
        "intent_excerpt": "",
    }
    budget = MAX_DESCRIPTION - len(_fill(body, values))
    excerpt = intent_body if len(intent_body) <= budget else intent_body[:max(budget - 80, 0)].rstrip() + \
        "\n\n[…truncated — see the attached file for the full intent]"
    values["intent_excerpt"] = excerpt
    description = _fill(body, values).strip()
    summary = _fill(str(meta.get("summary") or "Trinity discovery hand-off — {{project_name}}"), values)[:250]
    labels = [str(x) for x in (meta.get("labels") or [])] + [label]
    attachments = [(f"{a.artifact_id}-v{a.version}.md", Path(a.path).read_bytes()) for a in artifacts]
    return Ticket(summary=summary, description=description, labels=labels, attachments=attachments)


def ensure_record(db: Session, run: DiscoveryRun) -> Handoff:
    record = db.exec(select(Handoff).where(Handoff.run_id == run.id)).first()
    if record is None:
        artifacts = list(db.exec(select(Artifact).where(Artifact.run_id == run.id).order_by(Artifact.id)))
        record = Handoff(run_id=run.id, project_id=run.project_id, label=label_for(run, artifacts))
        db.add(record)
        db.commit()
        db.refresh(record)
    return record


def send(engine: Engine, settings: Settings, run_id: int, jira_factory: Optional[JiraFactory] = None,
         jira_config: Callable[[], JiraConfig] = JiraConfig.from_env) -> None:
    """Idempotent: every step checks what already happened, so a retry never duplicates the issue or attachments."""
    with Session(engine) as db:
        run = db.get(DiscoveryRun, run_id)
        if run is None or run.approval != "approved":
            return
        record = ensure_record(db, run)
        if record.status == "sent":
            return
        record.attempts += 1
        config = jira_config()
        missing = config.missing()
        if missing:
            record.status, record.error = "not_configured", "Jira is not configured. Set in .env: " + ", ".join(missing)
            _save(db, record)
            return
        client = (jira_factory or JiraClient)(config)
        try:
            ticket = render(db, settings, run)
            if not record.jira_key:
                existing = client.find_by_label(record.label)
                if existing:
                    record.jira_key, record.created_issue = existing["key"], True
                else:
                    evidence.record(db, project_id=run.project_id, actor_id=run.approved_by, action="handoff.create",
                                    target_type="discovery_run", target_id=run.id, after={"label": record.label})
                    record.jira_key = client.create_issue(ticket.summary, ticket.description, ticket.labels, config.issue_type)
                    record.created_issue = True
                record.jira_url = config.browse_url(record.jira_key)
                _save(db, record)
            if not record.attached:
                already = client.attachment_names(record.jira_key)
                client.attach(record.jira_key, [(n, d) for n, d in ticket.attachments if n not in already])
                record.attached = True
                _save(db, record)
            if not record.assigned:
                evidence.record(db, project_id=run.project_id, actor_id=run.approved_by, action="handoff.assign",
                                target_type="discovery_run", target_id=run.id,
                                after={"jira_key": record.jira_key, "assignee": config.lead_account_id})
                client.assign(record.jira_key, config.lead_account_id)
                record.assigned = True
            record.status, record.error = "sent", ""
            _save(db, record)
            chat.add_message(db, run.session_id, "assistant",
                             f"📨 Discovery #{run.number} was sent to ETL Crew: Jira **{record.jira_key}**, assigned to the lead. "
                             "ETL Crew takes it from here (planning and design).", kind="note")
            db.commit()
        except (JiraError, HTTPException) as exc:
            db.rollback()
            record = db.get(Handoff, record.id)
            record.status = "failed"
            record.error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            evidence.record(db, project_id=run.project_id, actor_id=run.approved_by, action="handoff.fail",
                            target_type="discovery_run", target_id=run.id, after={"error": record.error})
            _save(db, record)
        except Exception:  # noqa: BLE001 — record, never crash the worker
            log.exception("hand-off for run %s failed", run_id)
            db.rollback()
            record = db.get(Handoff, record.id)
            record.status, record.error = "failed", "Unexpected error — see the server log."
            _save(db, record)


def _save(db: Session, record: Handoff) -> None:
    record.updated_at = datetime.now(timezone.utc)
    db.add(record)
    db.commit()


def view(db: Session, run: DiscoveryRun) -> Optional[dict]:
    record = db.exec(select(Handoff).where(Handoff.run_id == run.id)).first()
    return record.model_dump(mode="json") if record else None


def retryable(db: Session, run_id: int) -> DiscoveryRun:
    run = db.get(DiscoveryRun, run_id)
    if run is None:
        raise HTTPException(404, "Discovery run not found")
    if run.approval != "approved":
        raise HTTPException(409, "Only an approved discovery run can be handed to ETL Crew.")
    record = db.exec(select(Handoff).where(Handoff.run_id == run.id)).first()
    if record and record.status == "sent":
        raise HTTPException(409, f"Already sent to ETL Crew as {record.jira_key}.")
    return run
