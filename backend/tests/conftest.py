from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.crew_ticket import CrewConfig
from app.services.jira import JiraConfig


TEST_SKILLS = Path(__file__).parent / "fixtures" / "skills"


def NO_JIRA() -> JiraConfig:
    """Tests never read the developer's real .env Jira settings."""
    return JiraConfig(base_url="", project_key="", lead_account_id="")


def NO_OPENCODE() -> CrewConfig:
    """Tests never read the developer's real .env opencode settings."""
    return CrewConfig(opencode_url="")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
                    skills_dir=TEST_SKILLS)


@pytest.fixture
def client(settings) -> TestClient:
    with TestClient(create_app(settings, inline_runs=True, jira_config=NO_JIRA, crew_config=NO_OPENCODE)) as c:
        yield c


@pytest.fixture
def project_id(client) -> int:
    return client.post("/api/projects", json={"name": "Test engagement"}).json()["id"]
