import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTECTED = ("skills/data-migration/", "skills/telcox/", "skills/custom/", "data/", ".env", "config/forbidden_tokens.txt")


def git_ls_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.splitlines()


def test_no_skill_content_secrets_or_data_are_tracked():
    tracked = [f for f in git_ls_files() if f == ".env" or any(f.startswith(p) for p in PROTECTED if p != ".env")]
    assert tracked == []


def test_protected_paths_are_gitignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
    for path in PROTECTED:
        assert path in gitignore, f"{path} missing from .gitignore"
