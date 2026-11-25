import hashlib
import json
from typing import Any

from sqlmodel import Session, select

from app.models import Evidence


def record(session: Session, *, project_id: int, actor_id: int, action: str,
           target_type: str, target_id: Any, before: Any = None, after: Any = None) -> Evidence:
    """Add an evidence row to the session WITHOUT committing.

    Callers add this first, then apply the state change, then commit once — so the
    evidence row and the change land in the same transaction (hard rule 6).
    Rows are hash-chained per project.
    """
    previous = session.exec(
        select(Evidence).where(Evidence.project_id == project_id).order_by(Evidence.id.desc())
    ).first()
    row = Evidence(
        project_id=project_id, actor_id=actor_id, action=action,
        target_type=target_type, target_id=str(target_id),
        before_json=json.dumps(before, default=str, sort_keys=True),
        after_json=json.dumps(after, default=str, sort_keys=True),
    )
    payload = "|".join([previous.hash if previous else "", str(project_id), str(actor_id), action,
                        target_type, row.target_id, row.before_json, row.after_json])
    row.hash = hashlib.sha256(payload.encode()).hexdigest()
    session.add(row)
    session.flush()
    return row
