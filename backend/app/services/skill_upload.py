"""Add a user's own skill: a SKILL.md, or a .zip containing one (optionally with reference files)."""
import io
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from app.config import Settings
from app.services.skills import split_frontmatter

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
TEXT_SUFFIXES = {".md", ".txt", ".yml", ".yaml", ".json", ".csv"}  # nothing executable is ever written
MAX_ZIP_FILES = 50
MAX_TOTAL_BYTES = 5 * 1024 * 1024


def _validated_name(skill_md: bytes) -> str:
    meta, body = split_frontmatter(skill_md.decode("utf-8", errors="replace"))
    name = str(meta.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "SKILL.md needs frontmatter with a `name:` (and ideally a `description:`).")
    if not NAME_PATTERN.match(name):
        raise HTTPException(422, f"Skill name '{name}' must be lowercase letters, digits and dashes.")
    if not body.strip():
        raise HTTPException(422, "SKILL.md has no instructions after the frontmatter.")
    return name


def _zip_members(content: bytes) -> tuple[bytes, dict[str, bytes]]:
    """Returns (SKILL.md bytes, {relative path: bytes}) for safe text files, relative to the SKILL.md folder."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise HTTPException(422, "That file is not a valid .zip.") from exc
    entries = [i for i in archive.infolist() if not i.is_dir()]
    if len(entries) > MAX_ZIP_FILES or sum(i.file_size for i in entries) > MAX_TOTAL_BYTES:
        raise HTTPException(413, f"Skill zips are limited to {MAX_ZIP_FILES} files and {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
    skill_entries = sorted((i for i in entries if PurePosixPath(i.filename).name == "SKILL.md"),
                           key=lambda i: len(PurePosixPath(i.filename).parts))
    if not skill_entries:
        raise HTTPException(422, "No SKILL.md found in the zip.")
    root = PurePosixPath(skill_entries[0].filename).parent
    files: dict[str, bytes] = {}
    for info in entries:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise HTTPException(422, f"Unsafe path in zip: {info.filename}")
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue  # outside the skill folder
        if relative.suffix.lower() in TEXT_SUFFIXES:
            files[str(relative)] = archive.read(info)
    return files["SKILL.md"], files


def install(settings: Settings, filename: str, content: bytes, replace: bool) -> str:
    if filename.lower().endswith(".zip"):
        skill_md, files = _zip_members(content)
    elif filename.lower().endswith(".md"):
        skill_md, files = content, {"SKILL.md": content}
    else:
        raise HTTPException(422, "Upload a SKILL.md file or a .zip that contains one.")
    name = _validated_name(skill_md)
    target = settings.skills_dir / "custom" / name
    if target.exists() and not replace:
        raise HTTPException(409, f"A custom skill named '{name}' already exists. Tick 'replace' to overwrite it.")
    if target.exists():
        shutil.rmtree(target)
    for relative, data in files.items():
        destination = (target / relative).resolve()
        if not destination.is_relative_to(target.resolve()):
            raise HTTPException(422, f"Unsafe path: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return name
