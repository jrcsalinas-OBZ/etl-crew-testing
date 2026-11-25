import hashlib
import re
from pathlib import Path
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import Settings
from app.models import KnowledgeFile, StepKey
from app.services import evidence
from app.services.extraction import extract_text

TEXT_EXTENSIONS = {".sql", ".csv", ".txt", ".md", ".json", ".yml", ".yaml", ".py", ".ipynb", ".hql", ".bteq"}
INJECTION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"ignore\s+(all\s+)?(previous|prior|above)",
    r"you\s+are\s+now",
    r"system\s+prompt",
    r"approve\s+this",
)]


def is_text_upload(filename: str, mime: str) -> bool:
    return mime.startswith("text/") or Path(filename).suffix.lower() in TEXT_EXTENSIONS


def has_injection(text: str) -> bool:
    return any(p.search(text) for p in INJECTION_PATTERNS)


def scan_injection(filename: str, mime: str, content: bytes) -> bool:
    """Flag injection-style phrases in text uploads. The text is never acted on."""
    if not is_text_upload(filename, mime):
        return False
    return has_injection(content.decode("utf-8", errors="ignore"))


def safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "upload"


def find_by_hash(session: Session, project_id: int, sha256: str) -> Optional[KnowledgeFile]:
    return session.exec(select(KnowledgeFile).where(
        KnowledgeFile.project_id == project_id, KnowledgeFile.sha256 == sha256)).first()


def store_upload(session: Session, settings: Settings, *, project_id: int, step: Optional[StepKey],
                 filename: str, content: bytes, mime: str, user_id: int) -> tuple[KnowledgeFile, bool]:
    """Returns (file, duplicate). A duplicate returns the existing row and stores nothing."""
    sha256 = hashlib.sha256(content).hexdigest()
    existing = find_by_hash(session, project_id, sha256)
    if existing:
        return existing, True

    target_dir = settings.data_dir / "projects" / str(project_id) / "knowledge"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{sha256[:16]}_{safe_filename(filename)}"
    path.write_bytes(content)

    extraction = extract_text(filename, content)
    extracted_path = ""
    if extraction.ok:
        text_dir = settings.data_dir / "projects" / str(project_id) / "extracted"
        text_dir.mkdir(parents=True, exist_ok=True)
        text_path = text_dir / (path.name + ".txt")
        text_path.write_text(extraction.text, encoding="utf-8")
        extracted_path = str(text_path)

    row = KnowledgeFile(
        project_id=project_id, step=step, filename=safe_filename(filename), sha256=sha256,
        size_bytes=len(content), mime=mime, stored_path=str(path), uploaded_by=user_id,
        flagged_injection=scan_injection(filename, mime, content) or (extraction.ok and has_injection(extraction.text)),
        extracted_path=extracted_path, extracted_chars=len(extraction.text), extract_note=extraction.note,
    )
    try:
        session.add(row)
        session.flush()
        evidence.record(session, project_id=project_id, actor_id=user_id, action="knowledge.upload",
                        target_type="knowledge_file", target_id=row.id,
                        after={"filename": row.filename, "sha256": sha256, "step": step,
                               "flagged_injection": row.flagged_injection})
        session.commit()
    except IntegrityError:  # concurrent upload of the same bytes
        session.rollback()
        path.unlink(missing_ok=True)
        existing = find_by_hash(session, project_id, sha256)
        if existing is None:
            raise
        return existing, True
    session.refresh(row)
    return row, False


def list_files(session: Session, project_id: int, step: Optional[StepKey]) -> list[KnowledgeFile]:
    """step=None -> engagement-wide files only; step=X -> engagement-wide + that step's files only."""
    query = select(KnowledgeFile).where(KnowledgeFile.project_id == project_id)
    if step is None:
        query = query.where(KnowledgeFile.step.is_(None))
    else:
        query = query.where((KnowledgeFile.step.is_(None)) | (KnowledgeFile.step == step))
    return list(session.exec(query.order_by(KnowledgeFile.id)))
