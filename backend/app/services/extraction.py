"""Turn uploaded documents into plain text the model can read."""
import io
import json
from dataclasses import dataclass
from pathlib import Path

PLAIN_TEXT = {".txt", ".md", ".csv", ".tsv", ".sql", ".hql", ".bteq", ".ddl", ".json", ".yml", ".yaml",
              ".xml", ".py", ".sh", ".log", ".ini", ".cfg", ".properties", ".html"}
MAX_SHEET_ROWS = 5000
MAX_CELL_CHARS = 500


@dataclass(frozen=True)
class Extraction:
    ok: bool
    text: str = ""
    note: str = ""


def extract_text(filename: str, content: bytes) -> Extraction:
    ext = Path(filename).suffix.lower()
    try:
        if ext in PLAIN_TEXT:
            return _done(content.decode("utf-8", errors="replace"))
        if ext == ".ipynb":
            return _done(_notebook(content))
        if ext == ".pdf":
            return _pdf(content)
        if ext == ".docx":
            return _docx(content)
        if ext in {".xlsx", ".xlsm"}:
            return _xlsx(content)
    except Exception as exc:  # corrupt or password-protected files must not fail the upload
        return Extraction(False, note=f"could not read {ext or 'file'}: {type(exc).__name__}: {exc}")
    hint = " (legacy Office format — save as .docx/.xlsx)" if ext in {".doc", ".xls", ".ppt"} else ""
    return Extraction(False, note=f"unsupported file type {ext or '(none)'}{hint}")


def _done(text: str) -> Extraction:
    if not text.strip():
        return Extraction(False, note="file is empty")
    return Extraction(True, text)


def _notebook(content: bytes) -> str:
    try:
        cells = json.loads(content)["cells"]
    except (ValueError, KeyError, TypeError):
        return content.decode("utf-8", errors="replace")
    parts = []
    for cell in cells:
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else str(source)
        parts.append(f"[{cell.get('cell_type', 'cell')}]\n{source}")
    return "\n\n".join(parts)


def _pdf(content: bytes) -> Extraction:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        return Extraction(False, note="PDF is password-protected")
    pages = [f"[page {i}]\n{(page.extract_text() or '').strip()}" for i, page in enumerate(reader.pages, 1)]
    text = "\n\n".join(pages)
    if not any(p.split("\n", 1)[-1].strip() for p in pages):
        return Extraction(False, note="no extractable text (scanned PDF? OCR is not supported)")
    return Extraction(True, text)


def _docx(content: bytes) -> Extraction:
    from docx import Document

    doc = Document(io.BytesIO(content))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for index, table in enumerate(doc.tables, 1):
        rows = [" | ".join(cell.text.strip().replace("\n", " ") for cell in row.cells) for row in table.rows]
        parts.append(f"[table {index}]\n" + "\n".join(rows))
    return _done("\n\n".join(parts))


def _xlsx(content: bytes) -> Extraction:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    parts, truncated = [], []
    for sheet in workbook.worksheets:
        rows = []
        for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
            if number > MAX_SHEET_ROWS:
                truncated.append(sheet.title)
                break
            cells = ["" if v is None else str(v)[:MAX_CELL_CHARS].replace("\t", " ").replace("\n", " ") for v in row]
            if any(cells):
                rows.append("\t".join(cells))
        parts.append(f"## Sheet: {sheet.title}\n" + "\n".join(rows))
    result = _done("\n\n".join(parts))
    if result.ok and truncated:
        return Extraction(True, result.text, f"only the first {MAX_SHEET_ROWS} rows were read from: {', '.join(truncated)}")
    return result
