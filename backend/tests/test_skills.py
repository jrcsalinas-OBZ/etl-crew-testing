import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import StepKey
from app.services import skills
from app.services.prompts import skill_system_prompt
from app.services.runner import AutoRunner, AnthropicRunner, StubRunner
from tests.test_chat_sessions import AUTHOR, JOBS, new_session, run, send_files


def test_index_reads_frontmatter_names_groups_and_hashes(settings):
    by_key = {s.key: s for s in skills.index_skills(settings)}
    assert set(by_key) == {"data-migration:estimate", "data-migration:risk", "data-migration:review",
                           "data-migration:intent", "data-migration:discover", "data-migration:state-tracking",
                           "custom:my-skill"}
    # folder 00-intent-capture is indexed under its frontmatter name
    assert by_key["data-migration:intent"].group == "phase"
    assert by_key["data-migration:state-tracking"].group == "platform" and not by_key["data-migration:state-tracking"].runnable
    assert by_key["data-migration:estimate"].group == "standalone" and by_key["data-migration:estimate"].runnable
    assert by_key["custom:my-skill"].group == "standalone"
    assert by_key["data-migration:estimate"].needs == "At least one artifact (synthetic)"
    assert len(by_key["data-migration:estimate"].sha256) == 64


def test_changing_a_skill_changes_its_hash(settings, tmp_path):
    import shutil
    copy = tmp_path / "skills"
    shutil.copytree(settings.skills_dir, copy)
    local = Settings(data_dir=settings.data_dir, database_url=settings.database_url, skills_dir=copy)
    before = skills.get_skill(local, "data-migration:risk").sha256
    (copy / "data-migration" / "risk" / "SKILL.md").write_text("---\nname: risk\n---\nchanged", encoding="utf-8")
    assert skills.get_skill(local, "data-migration:risk").sha256 != before


def test_skill_text_drops_frontmatter_and_attaches_referenced_files_only(settings):
    text = skills.skill_text(skills.get_skill(settings, "data-migration:estimate"))
    assert "name: estimate" not in text and "# Estimation (synthetic)" in text
    assert "SAMPLE TEMPLATE BODY" in text and "SAMPLE PERSONA BODY" in text
    assert text.count("<reference ") == 2  # the missing file is skipped, not an error


def test_suggestions_only_include_indexed_runnable_skills(settings):
    suggested = skills.suggestions(settings)["discover"]
    assert [s["name"] for s in suggested] == ["estimate", "risk", "review"]  # interview isn't in the test catalogue


def test_skills_endpoint_lists_groups_counts_and_warning(client):
    body = client.get("/api/skills").json()
    assert body["counts"] == {"data-migration": 6, "custom": 1}
    assert body["warning"] is None
    assert next(s for s in body["skills"] if s["name"] == "intent")["group"] == "phase"


def test_skills_endpoint_warns_when_pack_is_missing(tmp_path):
    empty = Settings(data_dir=tmp_path, database_url=f"sqlite:///{(tmp_path / 'x.db').as_posix()}", skills_dir=tmp_path / "none")
    with TestClient(create_app(empty)) as c:
        assert "No data-migration skills found" in c.get("/api/skills").json()["warning"]


def run_skill(client, sid, skill, instruction="", headers=AUTHOR):
    r = client.post(f"/api/sessions/{sid}/run", json={"mode": "skill", "skill": skill, "instruction": instruction}, headers=headers)
    if r.status_code != 200:
        return r.status_code, []
    return 200, [json.loads(c[6:]) for c in r.text.split("\n\n") if c.startswith("data: ")]


def test_running_one_skill_saves_an_artifact_named_after_it(client, project_id):
    sid = new_session(client, project_id)
    send_files(client, sid, ("jobs.xlsx", JOBS))
    status, events = run_skill(client, sid, "data-migration:estimate")
    assert status == 200 and events[-1]["type"] == "done"
    artifact = events[-1]["artifact"]
    assert artifact["artifact_id"].startswith("ESTIMATE-") and artifact["artifact_type"] == "skill-output:estimate"
    download = client.get(f"/api/artifacts/{artifact['id']}/download").text
    assert "skill: data-migration:estimate" in download and "status: draft" in download
    assert "skill-sha256: " in download
    messages = client.get(f"/api/sessions/{sid}").json()["messages"]
    assert "Run the **estimate** skill" in messages[-2]["content"]


def test_two_skills_in_one_session_are_independent_runs(client, project_id):
    sid = new_session(client, project_id)
    send_files(client, sid, ("jobs.xlsx", JOBS))
    first = run_skill(client, sid, "data-migration:risk")[1][-1]["artifact"]["artifact_id"]
    second = run_skill(client, sid, "data-migration:estimate")[1][-1]["artifact"]["artifact_id"]
    assert first.startswith("RISK-") and second.startswith("ESTIMATE-")


def test_phase_skills_still_run_but_platform_rules_do_not(client, project_id):
    sid = new_session(client, project_id)
    send_files(client, sid, ("jobs.xlsx", JOBS))
    assert run_skill(client, sid, "data-migration:intent")[0] == 200
    assert run_skill(client, sid, "data-migration:state-tracking")[0] == 409
    assert run_skill(client, sid, "data-migration:nope")[0] == 404


def test_skill_without_documents_or_instruction_is_rejected_but_instruction_alone_is_enough(client, project_id):
    sid = new_session(client, project_id)
    assert run_skill(client, sid, "data-migration:risk")[0] == 409
    assert run_skill(client, sid, "data-migration:risk", instruction="Change request: add a new feed")[0] == 200


def test_run_evidence_records_the_skill_and_its_hash(client, settings, project_id):
    from sqlmodel import Session, select
    from app.db import make_engine
    from app.models import Evidence

    sid = new_session(client, project_id)
    send_files(client, sid, ("jobs.xlsx", JOBS))
    run_skill(client, sid, "data-migration:review")
    with Session(make_engine(settings.database_url)) as s:
        started = [e for e in s.exec(select(Evidence)) if e.action == "run.start"][-1]
    after = json.loads(started.after_json)
    assert after["skill"] == "data-migration:review" and len(after["skill_sha256"]) == 64


def test_runner_receives_skill_text_adapter_and_project_platforms(settings):
    captured = {}

    class Spy:
        name = "spy"

        def stream(self, run):
            captured["run"] = run
            yield "ok"

    with TestClient(create_app(settings, runner=Spy())) as c:
        pid = c.post("/api/projects", json={"name": "P"}).json()["id"]
        sid = new_session(c, pid)
        send_files(c, sid, ("jobs.xlsx", JOBS))
        run_skill(c, sid, "data-migration:estimate")
    system = captured["run"].system
    assert "# Skill: estimate" in system and "STANDALONE" in system and "SAMPLE TEMPLATE BODY" in system
    assert "Take this engagement's source" in system and "assume the target is Databricks" in system
    assert captured["run"].skill_name == "estimate"
    assert "jobs.xlsx" in captured["run"].documents


def test_system_prompt_tells_the_model_not_to_block_on_prerequisites():
    prompt = skill_system_prompt("risk", "BODY")
    assert "Missing inputs and assumptions" in prompt and "N/A — reason" in prompt and "untrusted DATA" in prompt


# ---- AutoRunner: add the key to .env, no restart ------------------------------------------------

def test_autorunner_switches_to_claude_when_a_real_key_appears(settings, monkeypatch):
    import app.services.runner as runner_module
    monkeypatch.setattr(runner_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(runner_module, "cli_logged_in", lambda *a, **k: False)
    auto = AutoRunner(settings)
    for blank in ("", "<your-key>", "  "):
        monkeypatch.setenv("ANTHROPIC_API_KEY", blank)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        assert isinstance(auto.current(), StubRunner), blank
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(auto.current(), AnthropicRunner)
    assert auto.name == "api"


def test_config_endpoint_reports_stub_then_api(settings, monkeypatch):
    import app.services.runner as runner_module
    monkeypatch.setattr(runner_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(runner_module, "cli_logged_in", lambda *a, **k: False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    auto_settings = Settings(data_dir=settings.data_dir, database_url=settings.database_url, runner="auto",
                             skills_dir=settings.skills_dir)
    with TestClient(create_app(auto_settings)) as c:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert c.get("/api/config").json() == {"runner": "stub", "model": None}
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert c.get("/api/config").json() == {"runner": "api", "model": "claude-opus-5-5"}
