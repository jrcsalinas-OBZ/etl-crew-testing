import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import Settings
from app.db import make_engine
from app.main import create_app
from app.models import DiscoveryRun, Evidence
from app.services.discovery import split_artifacts
from app.services.runner import RunnerError
from tests.test_chat_sessions import AUTHOR, APPROVER, JOBS, new_session, send_files

OWNER = {"X-User-Id": "1"}


def discovery(client, pid):
    return client.get(f"/api/projects/{pid}/discovery").json()


def start(client, sid, headers=AUTHOR):
    return client.post(f"/api/sessions/{sid}/discovery", headers=headers)


def ready_session(client, pid):
    sid = new_session(client, pid)
    send_files(client, sid, ("jobs.xlsx", JOBS))
    return sid


# ---- skill selection ----------------------------------------------------------------------------

def test_default_chain_is_intent_then_discover_and_only_discovery_skills_are_offered(client, project_id):
    body = discovery(client, project_id)
    assert body["selected"] == ["data-migration:intent", "data-migration:discover"]
    offered = {s["key"]: s for s in body["skills"]}
    assert set(offered) == {"data-migration:intent", "data-migration:discover", "data-migration:interview",
                            "data-migration:estimate", "data-migration:risk", "data-migration:review", "custom:my-skill"}
    assert offered["data-migration:discover"]["produces"] == ["DISC", "INV"]
    assert offered["data-migration:interview"]["available"] is False  # not in the test catalogue
    assert offered["custom:my-skill"]["source"] == "custom" and offered["custom:my-skill"]["produces"] == ["MY_SKILL"]
    assert body["status"] == "not_started"


def test_selection_is_saved_in_order_and_used_by_the_receipt(client, project_id):
    keys = ["data-migration:intent", "data-migration:risk", "data-migration:discover"]
    assert client.put(f"/api/projects/{project_id}/discovery/skills", json={"skills": keys}, headers=AUTHOR).status_code == 200
    assert discovery(client, project_id)["selected"] == keys
    sid = new_session(client, project_id)
    text = send_files(client, sid, ("jobs.xlsx", JOBS)).json()["messages"][1]["content"]
    assert "intent → risk → discover" in text


@pytest.mark.parametrize("keys", [[], ["data-migration:intent", "data-migration:intent"],
                                  ["data-migration:interview"], ["data-migration:state-tracking"], ["nope:x"]])
def test_invalid_selections_are_rejected(client, project_id, keys):
    assert client.put(f"/api/projects/{project_id}/discovery/skills", json={"skills": keys}, headers=AUTHOR).status_code == 422


# ---- running ------------------------------------------------------------------------------------

def test_run_needs_readable_documents(client, project_id):
    sid = new_session(client, project_id)
    assert start(client, sid).status_code == 409


def test_run_produces_versioned_trinity_artifacts_and_waits_for_approval(client, project_id):
    sid = ready_session(client, project_id)
    r = start(client, sid)
    assert r.status_code == 202
    run = client.get(f"/api/runs/{r.json()['id']}").json()
    assert run["status"] == "completed" and run["approval"] == "pending" and run["skills"] == ["intent", "discover"]
    arts = {a["doc_type"]: a for a in run["artifacts"]}
    assert list(arts) == ["INTENT", "DISC", "INV"]
    assert all(a["version"] == 1 and a["status"] == "in_review" for a in arts.values())
    assert arts["INTENT"]["artifact_id"].startswith("INTENT-") and arts["INV"]["artifact_id"].startswith("INV-")
    assert discovery(client, project_id)["status"] == "awaiting_approval"

    content = client.get(f"/api/artifacts/{arts['DISC']['id']}").json()["content"]
    assert "status: in_review" in content and f"parent-artifact: {arts['INTENT']['artifact_id']}" in content
    assert "version: 1" in content and "skill: data-migration:discover" in content

    messages = client.get(f"/api/sessions/{sid}").json()["messages"]
    assert [m["kind"] for m in messages[-3:]] == ["deliverable", "deliverable", "note"]
    assert "waiting for approval" in messages[-1]["content"]


def test_later_skills_receive_earlier_outputs_and_the_output_contract(settings):
    seen = []

    class Spy:
        name = "spy"

        def stream(self, run):
            seen.append(run)
            yield "".join(f"=== ARTIFACT: {t} ===\n# {t}\nbody\n" for t in run.artifact_types)

    with TestClient(create_app(settings, runner=Spy(), inline_runs=True)) as c:
        pid = c.post("/api/projects", json={"name": "P"}).json()["id"]
        start(c, ready_session(c, pid))
    first, second = seen
    assert first.artifact_types == ("INTENT",) and second.artifact_types == ("DISC", "INV")
    assert "jobs.xlsx" in first.documents and "INTENT-" not in first.documents
    assert "INTENT-" in second.documents and "jobs.xlsx" in second.documents
    assert "=== ARTIFACT: DISC ===" in second.system and "drafts awaiting human approval" in second.system
    assert "# Skill: discover" in second.system


def test_only_one_run_at_a_time(client, settings, project_id):
    sid = ready_session(client, project_id)
    with Session(make_engine(settings.database_url)) as s:
        s.add(DiscoveryRun(project_id=project_id, session_id=sid, number=1, skills_json="[]", started_by=2))
        s.commit()
    assert start(client, sid).status_code == 409


def test_a_failing_skill_fails_the_run_and_says_why(settings):
    class Broken:
        name = "broken"

        def stream(self, run):
            if run.skill_name == "discover":
                raise RunnerError("usage limit reached")
            yield "=== ARTIFACT: INTENT ===\n# Intent\n"

    with TestClient(create_app(settings, runner=Broken(), inline_runs=True)) as c:
        pid = c.post("/api/projects", json={"name": "P"}).json()["id"]
        sid = ready_session(c, pid)
        run = c.get(f"/api/runs/{start(c, sid).json()['id']}").json()
        assert run["status"] == "failed" and run["approval"] == "n/a" and run["error"] == "usage limit reached"
        assert discovery(c, pid)["status"] == "failed"
        assert "Discovery #1 failed: usage limit reached" in c.get(f"/api/sessions/{sid}").json()["messages"][-1]["content"]


def test_runs_interrupted_by_a_restart_are_marked_failed(settings):
    with TestClient(create_app(settings, inline_runs=True)) as c:
        pid = c.post("/api/projects", json={"name": "P"}).json()["id"]
        sid = ready_session(c, pid)
    with Session(make_engine(settings.database_url)) as s:
        s.add(DiscoveryRun(project_id=pid, session_id=sid, number=1, skills_json="[]", started_by=2))
        s.commit()
    with TestClient(create_app(settings, inline_runs=True)) as c:
        run = discovery(c, pid)["runs"][0]
        assert run["status"] == "failed" and "server restarted" in run["error"]


# ---- approval -----------------------------------------------------------------------------------

def test_starter_cannot_approve_and_approver_can(client, project_id):
    run_id = start(client, ready_session(client, project_id)).json()["id"]
    assert client.post(f"/api/runs/{run_id}/approve", headers=AUTHOR).status_code == 403
    approved = client.post(f"/api/runs/{run_id}/approve", headers=APPROVER).json()
    assert approved["approval"] == "approved" and approved["approved_by_name"] == "Approver B"
    assert all(a["status"] == "approved" for a in approved["artifacts"])
    content = client.get(f"/api/artifacts/{approved['artifacts'][0]['id']}").json()["content"]
    assert "status: approved" in content and "approved-by: Approver B" in content
    assert discovery(client, project_id)["status"] == "approved"
    assert client.post(f"/api/runs/{run_id}/approve", headers=APPROVER).status_code == 409  # already decided


def test_reject_needs_a_reason(client, project_id):
    run_id = start(client, ready_session(client, project_id)).json()["id"]
    assert client.post(f"/api/runs/{run_id}/reject", json={"reason": " "}, headers=APPROVER).status_code == 422
    rejected = client.post(f"/api/runs/{run_id}/reject", json={"reason": "Inventory misses the hourly feeds"},
                           headers=APPROVER).json()
    assert rejected["approval"] == "rejected" and rejected["rejection_reason"] == "Inventory misses the hourly feeds"
    assert discovery(client, project_id)["status"] == "rejected"


def test_rerun_creates_new_versions_and_only_the_latest_counts(client, project_id):
    sid = ready_session(client, project_id)
    first = start(client, sid).json()["id"]
    client.post(f"/api/runs/{first}/approve", headers=APPROVER)
    second = client.get(f"/api/runs/{start(client, sid).json()['id']}").json()
    first_arts = {a["doc_type"]: a for a in client.get(f"/api/runs/{first}").json()["artifacts"]}
    second_arts = {a["doc_type"]: a for a in second["artifacts"]}
    assert all(second_arts[t]["artifact_id"] == first_arts[t]["artifact_id"] for t in ("INTENT", "DISC", "INV"))
    assert all(a["version"] == 2 for a in second_arts.values())

    client.post(f"/api/runs/{second['id']}/approve", headers=OWNER)  # owner may approve too (not the starter)
    old = client.get(f"/api/runs/{first}").json()
    assert old["approval"] == "superseded" and all(a["status"] == "superseded" for a in old["artifacts"])


def test_a_newer_run_supersedes_one_still_awaiting_approval(client, project_id):
    sid = ready_session(client, project_id)
    first = start(client, sid).json()["id"]
    start(client, sid)
    assert client.get(f"/api/runs/{first}").json()["approval"] == "superseded"
    assert client.post(f"/api/runs/{first}/approve", headers=APPROVER).status_code == 409


def test_every_decision_is_in_the_evidence_log(client, settings, project_id):
    run_id = start(client, ready_session(client, project_id)).json()["id"]
    client.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
    with Session(make_engine(settings.database_url)) as s:
        actions = [e.action for e in s.exec(select(Evidence).where(Evidence.project_id == project_id).order_by(Evidence.id))]
    assert actions[-3:] == ["discovery.run_start", "discovery.run_complete", "discovery.approve"]
    start_row = json.loads(next(e.after_json for e in Session(make_engine(settings.database_url)).exec(
        select(Evidence).where(Evidence.action == "discovery.run_start"))))
    assert [s["key"] for s in start_row["skills"]] == ["data-migration:intent", "data-migration:discover"]
    assert all(len(s["sha256"]) == 64 for s in start_row["skills"])


# ---- splitting model output ---------------------------------------------------------------------

def test_split_on_markers_keeps_order_and_folds_preamble_into_first():
    text = "Here you go.\n=== ARTIFACT: DISC ===\n# Disc\n=== ARTIFACT: INV ===\n# Inv\n"
    assert split_artifacts(text, ["DISC", "INV"]) == [("DISC", "Here you go.\n\n# Disc"), ("INV", "# Inv")]


def test_output_without_markers_becomes_the_first_artifact():
    assert split_artifacts("# Whole thing", ["DISC", "INV"]) == [("DISC", "# Whole thing")]


def test_unexpected_marker_stays_inside_the_previous_artifact():
    text = "=== ARTIFACT: DISC ===\n# Disc\n=== ARTIFACT: OTHER ===\nextra\n"
    assert split_artifacts(text, ["DISC", "INV"]) == [("DISC", "# Disc\n=== ARTIFACT: OTHER ===\nextra")]


# ---- adding a skill -----------------------------------------------------------------------------

@pytest.fixture
def skills_copy(settings, tmp_path):
    copy = tmp_path / "skills-copy"
    shutil.copytree(settings.skills_dir, copy)
    local = Settings(data_dir=settings.data_dir, database_url=settings.database_url, skills_dir=copy)
    with TestClient(create_app(local, inline_runs=True)) as c:
        yield c, copy


def upload(client, pid, name, content, replace=False):
    return client.post("/api/skills/upload", files={"file": (name, content, "application/octet-stream")},
                       data={"project_id": str(pid), "replace": str(replace).lower()}, headers=AUTHOR)


SKILL = b"---\nname: source-profiler\ndescription: Profile source systems\n---\n# Source profiler\nDo the thing.\n"


def test_uploaded_skill_md_appears_as_a_discovery_skill(skills_copy):
    client, copy = skills_copy
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    r = upload(client, pid, "SKILL.md", SKILL)
    assert r.status_code == 201 and r.json()["key"] == "custom:source-profiler"
    assert (copy / "custom" / "source-profiler" / "SKILL.md").read_bytes() == SKILL
    offered = {s["key"]: s for s in discovery(client, pid)["skills"]}
    assert offered["custom:source-profiler"]["produces"] == ["SOURCE_PROFILER"]
    assert upload(client, pid, "SKILL.md", SKILL).status_code == 409
    assert upload(client, pid, "SKILL.md", SKILL, replace=True).status_code == 201


def test_uploaded_skill_can_join_the_chain(skills_copy):
    client, _ = skills_copy
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    upload(client, pid, "SKILL.md", SKILL)
    client.put(f"/api/projects/{pid}/discovery/skills", json={"skills": ["data-migration:intent", "custom:source-profiler"]},
               headers=AUTHOR)
    run = client.get(f"/api/runs/{start(client, ready_session(client, pid)).json()['id']}").json()
    assert [a["doc_type"] for a in run["artifacts"]] == ["INTENT", "SOURCE_PROFILER"]


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_zip_upload_keeps_text_files_and_drops_others(skills_copy):
    client, copy = skills_copy
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    z = make_zip({"profiler/SKILL.md": SKILL, "profiler/templates/report.md": b"# Report", "profiler/run.exe": b"MZ",
                  "README.md": b"outside the skill folder"})
    assert upload(client, pid, "profiler.zip", z).status_code == 201
    target = copy / "custom" / "source-profiler"
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()) == ["SKILL.md", "templates/report.md"]


@pytest.mark.parametrize("name,content,status", [
    ("SKILL.md", b"# no frontmatter", 422),
    ("SKILL.md", b"---\nname: Bad Name\n---\nbody", 422),
    ("SKILL.md", b"---\nname: empty-body\n---\n", 422),
    ("skill.exe", b"MZ", 422),
    ("x.zip", b"not a zip", 422),
])
def test_bad_skill_uploads_are_refused(skills_copy, name, content, status):
    client, _ = skills_copy
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    assert upload(client, pid, name, content).status_code == status


def test_zip_with_path_traversal_is_refused(skills_copy):
    client, copy = skills_copy
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    z = make_zip({"SKILL.md": SKILL, "../escape.md": b"x"})
    assert upload(client, pid, "evil.zip", z).status_code == 422
    assert not (copy / "escape.md").exists() and not (copy / "custom" / "source-profiler").exists()
