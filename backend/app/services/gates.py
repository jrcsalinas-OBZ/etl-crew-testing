from datetime import datetime, timezone

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import Artifact, Role, StepKey, StepSpace, StepStatus, User
from app.services import evidence
from app.services.chat import STEP_NAME, get_step

GATES = {StepKey.discover: "QG0 + QG1", StepKey.plan: "QG2", StepKey.design: "QG3 + QG4"}


def _snapshot(step: StepSpace) -> dict:
    return {"status": step.status, "author_id": step.author_id, "approved_by": step.approved_by}


def submit(db: Session, project_id: int, key: StepKey, user: User) -> StepSpace:
    step = get_step(db, project_id, key)
    if step.status != StepStatus.open:
        raise HTTPException(409, f"{STEP_NAME[key]} is {step.status.value.replace('_', ' ')}; only an open step can be submitted.")
    has_draft = db.exec(select(Artifact).where(Artifact.project_id == project_id, Artifact.step == key)).first()
    if has_draft is None:
        raise HTTPException(409, "Nothing to approve yet: generate a draft deliverable in a session first.")
    before = _snapshot(step)
    step.status, step.author_id, step.rejection_reason = StepStatus.awaiting_approval, user.id, ""
    evidence.record(db, project_id=project_id, actor_id=user.id, action="gate.submit", target_type="step",
                    target_id=step.id, before=before, after=_snapshot(step))
    db.add(step)
    db.commit()
    db.refresh(step)
    return step


def approve(db: Session, project_id: int, key: StepKey, user: User) -> StepSpace:
    step = get_step(db, project_id, key)
    if step.status != StepStatus.awaiting_approval:
        raise HTTPException(409, f"{STEP_NAME[key]} is not awaiting approval.")
    if user.id == step.author_id:
        raise HTTPException(403, "The author of a step cannot approve it (approver must differ from author).")
    if user.role not in (Role.approver, Role.owner):
        raise HTTPException(403, "Only an approver or owner can approve a gate.")
    before = _snapshot(step)
    step.status, step.approved_by, step.approved_at = StepStatus.approved, user.id, datetime.now(timezone.utc)
    evidence.record(db, project_id=project_id, actor_id=user.id, action="gate.approve", target_type="step",
                    target_id=step.id, before=before, after=_snapshot(step))
    db.add(step)
    following = db.exec(select(StepSpace).where(StepSpace.project_id == project_id, StepSpace.order == step.order + 1)).first()
    if following and following.status == StepStatus.locked:
        evidence.record(db, project_id=project_id, actor_id=user.id, action="step.unlock", target_type="step",
                        target_id=following.id, before={"status": following.status}, after={"status": StepStatus.open})
        following.status = StepStatus.open
        db.add(following)
    db.commit()
    db.refresh(step)
    return step


def reject(db: Session, project_id: int, key: StepKey, user: User, reason: str) -> StepSpace:
    if not reason.strip():
        raise HTTPException(422, "A rejection reason is required.")
    step = get_step(db, project_id, key)
    if step.status != StepStatus.awaiting_approval:
        raise HTTPException(409, f"{STEP_NAME[key]} is not awaiting approval.")
    if user.id == step.author_id:
        raise HTTPException(403, "The author of a step cannot review it.")
    if user.role not in (Role.approver, Role.owner):
        raise HTTPException(403, "Only an approver or owner can reject a gate.")
    before = _snapshot(step)
    step.status, step.rejection_reason = StepStatus.open, reason.strip()
    evidence.record(db, project_id=project_id, actor_id=user.id, action="gate.reject", target_type="step",
                    target_id=step.id, before=before, after={**_snapshot(step), "reason": reason.strip()})
    db.add(step)
    db.commit()
    db.refresh(step)
    return step
