"""Read files from the local disk by path, as an alternative to a browser file dialog."""
import mimetypes
from pathlib import Path

from fastapi import HTTPException

MAX_FILES = 50
BLOCKED_NAMES = {".env", "id_rsa", "id_ed25519", "known_hosts", "credentials"}
BLOCKED_SUFFIXES = {".pem", ".key", ".pfx", ".p12"}


def _clean(raw: str) -> str:
    """Explorer's 'Copy as path' wraps paths in double quotes."""
    return raw.strip().strip('"').strip("'").strip()


def _blocked(path: Path) -> bool:
    return path.name.lower() in BLOCKED_NAMES or path.suffix.lower() in BLOCKED_SUFFIXES or ".ssh" in path.parts


def read_paths(raw_paths: list[str], max_bytes: int) -> tuple[list[tuple[str, bytes, str]], list[str]]:
    """Returns ([(filename, content, mime)], skipped reasons); a folder contributes its direct child files."""
    candidates: list[Path] = []
    problems: list[str] = []
    for raw in (p for entry in raw_paths for p in entry.splitlines()):
        text = _clean(raw)
        if not text:
            continue
        path = Path(text).expanduser()
        if path.is_dir():
            candidates.extend(sorted(child for child in path.iterdir() if child.is_file()))
        elif path.is_file():
            candidates.append(path)
        else:
            problems.append(f"not found: {text}")

    files: list[tuple[str, bytes, str]] = []
    for path in dict.fromkeys(candidates):  # de-duplicate, keep order
        if _blocked(path):
            problems.append(f"not allowed (credentials/keys): {path.name}")
        elif path.stat().st_size > max_bytes:
            problems.append(f"too large (over {max_bytes // (1024 * 1024)} MB): {path.name}")
        else:
            try:
                files.append((path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or ""))
            except OSError as exc:
                problems.append(f"could not read {path.name}: {exc.strerror or exc}")

    if len(files) > MAX_FILES:
        raise HTTPException(422, f"That is {len(files)} files; add at most {MAX_FILES} at a time.")
    if problems and not files:
        raise HTTPException(422, "No files added — " + "; ".join(problems))
    if not files:
        raise HTTPException(422, "Paste at least one file or folder path.")
    return files, problems
