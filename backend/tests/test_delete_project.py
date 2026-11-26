import json

from sqlmodel import Session, select

from app.db import make_engine
from app.models import (Artifact, ChatMessage, ChatSession, DiscoveryRun, Evidence, Handoff, KnowledgeFile, SessionFile,
                        StepSpace)
from tests.test_chat_sessions import AUTHOR, APPROVER, JOBS, new_session, send_files

OWNER = {"X-User-Id": "1"}


def full_project(client, name="To delete"):
    """A project with a session, an upload, an approved discovery run and its artifacts."""
    pid = client.post("/api/projects", json={"name": name}, headers=AUTHOR).json()["id"]
    sid = new_session(client, pid)
    send_files(client, sid, ("jobs.xlsx", JOBS))
    run_id = client.post(f"/api/sessions/{sid}/discovery", headers=AUTHOR).json()["id"]
    client.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
    return pid


def rows(settings, model, **where):
    with Session(make_engine(settings.database_url)) as s:
        query = select(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return list(s.exec(query))


def test_creator_deletes_project_rows_and_files_but_evidence_is_kept(client, settings):
    pid = full_project(client)
    keep = full_project(client, "Keep me")
    folder = settings.data_dir / "projects" / str(pid)
    assert folder.exists()

    assert client.delete(f"/api/projects/{pid}", headers=AUTHOR).status_code == 204

    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert [p["name"] for p in client.get("/api/projects").json()] == ["Keep me"]
    for model in (ChatSession, KnowledgeFile, DiscoveryRun, Artifact, Handoff, StepSpace):
        assert rows(settings, model, project_id=pid) == [], model.__name__
    assert not folder.exists()
    assert rows(settings, SessionFile) and rows(settings, ChatMessage)  # the other project's rows are untouched
    assert (settings.data_dir / "projects" / str(keep)).exists()

    actions = [e.action for e in rows(settings, Evidence, project_id=pid)]
    assert actions[0] == "project.create" and actions[-1] == "project.delete"
    before = json.loads(rows(settings, Evidence, project_id=pid)[-1].before_json)
    assert before["name"] == "To delete" and before["sessions"] == 1 and before["runs"] == 1 and before["artifacts"] == 3


def test_owner_can_delete_someone_elses_project(client):
    pid = client.post("/api/projects", json={"name": "P"}, headers=AUTHOR).json()["id"]
    assert client.delete(f"/api/projects/{pid}", headers=OWNER).status_code == 204


def test_other_users_cannot_delete(client):
    pid = client.post("/api/projects", json={"name": "P"}, headers=AUTHOR).json()["id"]
    r = client.delete(f"/api/projects/{pid}", headers=APPROVER)
    assert r.status_code == 403 and client.get(f"/api/projects/{pid}").status_code == 200


def test_cannot_delete_while_discovery_is_running(client, settings):
    pid = client.post("/api/projects", json={"name": "P"}, headers=AUTHOR).json()["id"]
    sid = new_session(client, pid)
    with Session(make_engine(settings.database_url)) as s:
        s.add(DiscoveryRun(project_id=pid, session_id=sid, number=1, skills_json="[]", started_by=2))
        s.commit()
    assert client.delete(f"/api/projects/{pid}", headers=AUTHOR).status_code == 409


def test_deleting_an_unknown_project_is_404(client):
    assert client.delete("/api/projects/999", headers=AUTHOR).status_code == 404
