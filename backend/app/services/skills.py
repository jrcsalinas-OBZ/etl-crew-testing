"""Skill catalogue: index skills/**/SKILL.md, classify them, and build the text a standalone run uses."""
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from app.config import Settings

PACKS = ("data-migration", "telcox", "custom")
RUNNABLE_GROUPS = {"standalone", "advanced", "phase"}
REFERENCE_PATTERN = re.compile(r"\b(templates|agents)/([A-Za-z0-9_.-]+\.md)")
MAX_REFERENCE_CHARS = 80_000


@dataclass(frozen=True)
class SkillInfo:
    key: str  # "<pack>:<name>"
    name: str
    pack: str
    description: str
    group: str  # standalone | advanced | phase | platform
    needs: str  # first "Required input" line, shown so users know what to upload
    sha256: str
    path: Path

    @property
    def runnable(self) -> bool:
        return self.group in RUNNABLE_GROUPS


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("\n")


def load_groups(settings: Settings) -> dict[str, str]:
    path = settings.config_dir / "skill-groups.yml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {name: group for group, names in (data.get("groups") or {}).items() for name in names}


def _needs(body: str) -> str:
    match = re.search(r"^## Prerequisites\s*\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    bullets = [ln.strip("-* ").strip() for ln in match.group(1).splitlines() if ln.strip().startswith(("-", "*"))]
    required = next((b for b in bullets if b.lower().startswith("required input")), bullets[0] if bullets else "")
    return re.sub(r"^(Required input|Optional)[^:]*:\s*", "", required)[:220]


def index_skills(settings: Settings) -> list[SkillInfo]:
    groups = load_groups(settings)
    skills: list[SkillInfo] = []
    for pack in PACKS:
        for path in sorted((settings.skills_dir / pack).glob("*/SKILL.md")):
            raw = path.read_bytes()
            meta, body = split_frontmatter(raw.decode("utf-8", errors="replace"))
            name = str(meta.get("name") or path.parent.name)
            skills.append(SkillInfo(
                key=f"{pack}:{name}", name=name, pack=pack, description=str(meta.get("description") or "").strip(),
                group=groups.get(name, "standalone"),
                needs=_needs(body), sha256=hashlib.sha256(raw).hexdigest(), path=path))
    return skills


def get_skill(settings: Settings, key: str) -> Optional[SkillInfo]:
    return next((s for s in index_skills(settings) if s.key == key), None)


def skill_text(skill: SkillInfo) -> str:
    """The skill body (frontmatter removed) plus any templates / personas it references."""
    _, body = split_frontmatter(skill.path.read_text(encoding="utf-8", errors="replace"))
    support = skill.path.parent.parent / "_support"
    references, seen, budget = [], set(), MAX_REFERENCE_CHARS
    for folder, filename in REFERENCE_PATTERN.findall(body):
        relative = f"{folder}/{filename}"
        target = support / folder / filename
        if relative in seen or not target.is_file():
            continue
        seen.add(relative)
        content = target.read_text(encoding="utf-8", errors="replace")[:budget]
        budget -= len(content)
        references.append(f'<reference file="{relative}">\n{content}\n</reference>')
        if budget <= 0:
            break
    if references:
        body += "\n\n# Referenced files\n\n" + "\n\n".join(references)
    return body


def suggestions(settings: Settings) -> dict[str, list[dict]]:
    path = settings.config_dir / "step-skill-map.yml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    available = {s.key: s for s in index_skills(settings) if s.runnable}
    return {step: [{"skill": item["skill"], "name": available[item["skill"]].name, "reason": item["reason"]}
                   for item in items if item["skill"] in available]
            for step, items in (data.get("suggestions") or {}).items()}
