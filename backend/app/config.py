import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "claude-opus-5-5"


def api_key_configured() -> bool:
    """A real key is set. Placeholders like <your-key> from .env.example don't count."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    return bool(key) and not key.startswith("<")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    runner: str = "stub"  # stub | api | auto (auto = Claude when a key is present in .env/environment, else stub)
    model: str = DEFAULT_MODEL
    max_upload_bytes: int = 25 * 1024 * 1024
    skills_dir: Path = field(default_factory=lambda: REPO_ROOT / "skills")
    config_dir: Path = field(default_factory=lambda: REPO_ROOT / "config")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(REPO_ROOT / ".env")
        data_dir = Path(os.environ.get("DATA_DIR", REPO_ROOT / "data"))
        database_url = os.environ.get("DATABASE_URL", f"sqlite:///{(data_dir / 'workbench.db').as_posix()}")
        return cls(data_dir=data_dir, database_url=database_url, runner=os.environ.get("RUNNER") or "auto",
                   model=os.environ.get("MODEL", DEFAULT_MODEL))
