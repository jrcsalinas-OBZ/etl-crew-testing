import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import make_engine
from app.main import create_app
from app.models import Evidence
from app.services.jira import JiraClient, JiraConfig
from tests.test_chat_sessions import AUTHOR, APPROVER, JOBS, new_session, send_files

CONFIG = JiraConfig(base_url="https://api.atlassian.com/ex/jira/cloud-123", project_key="ETL",
                    lead_account_id="acc-lead", token="secret-token", site_url="https://example.atlassian.net")


class FakeJira:
    """In-memory Jira: records every call so tests can assert order and payloads."""

    def __init__(self, existing_label: str = "", fail: dict | None = None):
        self.calls: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.attachments: list[str] = []
        self.assignee: str | None = None
        self.auth: set[str] = set()
        self.existing_label = existing_label
        self.fail = dict(fail or {})  # {"assign": 1} → fail the first assign with 403

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("Authorization", ""))
        path, method = request.url.path.removeprefix("/ex/jira/cloud-123"), request.method
        if path == "/rest/api/3/search/jql":
            self.calls.append(("search", request.url.params["jql"]))
            label = re.search(r'labels = "([^"]+)"', request.url.params["jql"]).group(1)
            hit = (self.existing_label and label == self.existing_label) or any(label in i["labels"] for i in self.created)
            return httpx.Response(200, json={"issues": [{"key": "ETL-7" if self.existing_label else "ETL-1"}] if hit else []})
        if path == "/rest/api/2/issue" and method == "POST":
            self.calls.append(("create", ""))
            fields = json.loads(request.content)["fields"]
            self.created.append(fields)
            return httpx.Response(201, json={"key": "ETL-1"})
        if re.fullmatch(r"/rest/api/2/issue/ETL-\d+", path) and method == "GET":
            self.calls.append(("read", path))
            return httpx.Response(200, json={"fields": {"attachment": [{"filename": n} for n in self.attachments]}})
        if path.endswith("/attachments"):
            self.calls.append(("attach", request.headers.get("X-Atlassian-Token", "")))
            self.attachments += re.findall(r'filename="([^"]+)"', request.content.decode("utf-8", "replace"))
            return httpx.Response(200, json=[])
        if path.endswith("/assignee"):
            self.calls.append(("assign", path))
            if self.fail.get("assign"):
                self.fail["assign"] -= 1
                return httpx.Response(403, json={"errorMessages": ["You do not have permission to assign issues."]})
            self.assignee = json.loads(request.content)["accountId"]
            return httpx.Response(204)
        return httpx.Response(404, json={"errorMessages": [f"unexpected {method} {path}"]})


def make_client(settings, fake: FakeJira, config: JiraConfig = CONFIG):
    return TestClient(create_app(settings, inline_runs=True, jira_config=lambda: config,
                                 jira_factory=lambda cfg: JiraClient(cfg, transport=httpx.MockTransport(fake.handler))))


def approved_run(client, name="Network Feeds"):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    sid = new_session(client, pid)
    send_files(client, sid, ("jobs.xlsx", JOBS))
    run_id = client.post(f"/api/sessions/{sid}/discovery", headers=AUTHOR).json()["id"]
    return pid, sid, run_id, client.post(f"/api/runs/{run_id}/approve", headers=APPROVER).json()


def test_approval_creates_the_ticket_attaches_files_and_assigns_the_lead_last(settings):
    fake = FakeJira()
    with make_client(settings, fake) as c:
        pid, sid, run_id, _ = approved_run(c)
        run = c.get(f"/api/runs/{run_id}").json()
        state = c.get(f"/api/projects/{pid}/discovery").json()
        messages = c.get(f"/api/sessions/{sid}").json()["messages"]
    assert [kind for kind, _ in fake.calls] == ["search", "create", "read", "attach", "assign"]
    assert fake.calls[-1][0] == "assign"  # assignment triggers ETL Crew, so it must come after everything else
    fields = fake.created[0]
    assert fields["project"] == {"key": "ETL"} and fields["issuetype"] == {"name": "Task"}
    assert fields["summary"] == "Trinity discovery hand-off — Network Feeds (Discovery #1)"
    assert "assignee" not in fields  # created unassigned; the separate assign call fires issue_assigned
    labels = fields["labels"]
    assert labels[:2] == ["trinity", "trinity-discovery"] and re.fullmatch(r"trinity-disc-[0-9a-f]{10}", labels[2])
    description = fields["description"]
    assert description.strip() and "{{" not in description
    assert "INTENT-" in description and "DISC-" in description and "INV-" in description and "jobs.xlsx" in description
    assert sorted(fake.attachments) == sorted(f"{a['artifact_id']}-v1.md" for a in run["artifacts"])
    assert fake.calls[3][1] == "no-check"
    assert fake.assignee == "acc-lead" and fake.auth == {"Bearer secret-token"}
    handoff = run["handoff"]
    assert handoff["status"] == "sent" and handoff["jira_key"] == "ETL-1"
    assert handoff["jira_url"] == "https://example.atlassian.net/browse/ETL-1"
    assert state["status"] == "sent_to_etl_crew"
    assert "Jira **ETL-1**" in messages[-1]["content"]


def test_hand_off_steps_are_in_the_evidence_log(settings):
    with make_client(settings, FakeJira()) as c:
        pid, *_ = approved_run(c)
    with Session(make_engine(settings.database_url)) as s:
        actions = [e.action for e in s.exec(select(Evidence).where(Evidence.project_id == pid).order_by(Evidence.id))]
    assert actions[-3:] == ["discovery.approve", "handoff.create", "handoff.assign"]


def test_without_jira_settings_the_approval_stands_and_the_gap_is_explained(settings):
    blank = JiraConfig(base_url="", project_key="", lead_account_id="")
    fake = FakeJira()
    with make_client(settings, fake, blank) as c:
        pid, _, run_id, approved = approved_run(c)
        handoff = c.get(f"/api/runs/{run_id}").json()["handoff"]
        status = c.get(f"/api/projects/{pid}/discovery").json()["status"]
    assert approved["approval"] == "approved" and status == "approved"
    assert handoff["status"] == "not_configured" and "JIRA_BASE_URL" in handoff["error"] and "JIRA_TOKEN" in handoff["error"]
    assert fake.calls == []


def test_failed_assign_can_be_retried_without_duplicating_the_issue_or_attachments(settings):
    fake = FakeJira(fail={"assign": 1})
    with make_client(settings, fake) as c:
        _, _, run_id, _ = approved_run(c)
        failed = c.get(f"/api/runs/{run_id}").json()["handoff"]
        assert failed["status"] == "failed" and "HTTP 403" in failed["error"] and "permission to assign" in failed["error"]
        assert "secret-token" not in failed["error"]
        attached_before = list(fake.attachments)
        retried = c.post(f"/api/runs/{run_id}/handoff/retry", headers=AUTHOR)
        assert retried.status_code == 202
        sent = c.get(f"/api/runs/{run_id}").json()["handoff"]
    assert sent["status"] == "sent" and sent["attempts"] == 2
    assert [k for k, _ in fake.calls].count("create") == 1
    assert fake.attachments == attached_before and fake.assignee == "acc-lead"


def test_an_existing_ticket_with_the_label_is_reused(settings):
    fake = FakeJira()
    with make_client(settings, fake) as c:
        # learn the label the run will get, then pretend that issue already exists in Jira
        pid = c.post("/api/projects", json={"name": "P"}).json()["id"]
        sid = new_session(c, pid)
        send_files(c, sid, ("jobs.xlsx", JOBS))
        run_id = c.post(f"/api/sessions/{sid}/discovery", headers=AUTHOR).json()["id"]
        preview = c.get(f"/api/runs/{run_id}/handoff/preview").json()
        fake.existing_label = preview["labels"][-1]
        c.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
        handoff = c.get(f"/api/runs/{run_id}").json()["handoff"]
    assert "create" not in [k for k, _ in fake.calls] and handoff["jira_key"] == "ETL-7" and handoff["status"] == "sent"


def test_basic_auth_when_email_and_api_token_are_used(settings):
    fake = FakeJira()
    basic = JiraConfig(base_url="https://example.atlassian.net", project_key="ETL", lead_account_id="acc-lead",
                       email="bot@example.com", api_token="tkn")
    fake_handler = fake.handler
    fake.handler = lambda req: fake_handler(req)  # same fake; base URL differs only in host
    with make_client(settings, fake, basic) as c:
        _, _, run_id, _ = approved_run(c)
        handoff = c.get(f"/api/runs/{run_id}").json()["handoff"]
    assert fake.auth == {"Basic Ym90QGV4YW1wbGUuY29tOnRrbg=="}
    assert handoff["jira_url"] == "https://example.atlassian.net/browse/ETL-1"


def test_long_intent_is_truncated_to_fit_jiras_description_limit(settings):
    class Verbose:
        name = "verbose"

        def stream(self, run):
            yield "".join(f"=== ARTIFACT: {t} ===\n# {t}\n" + ("x" * 50_000 if t == "INTENT" else "short") + "\n"
                          for t in run.artifact_types)

    fake = FakeJira()
    app = create_app(settings, runner=Verbose(), inline_runs=True, jira_config=lambda: CONFIG,
                     jira_factory=lambda cfg: JiraClient(cfg, transport=httpx.MockTransport(fake.handler)))
    with TestClient(app) as c:
        approved_run(c)
    description = fake.created[0]["description"]
    assert len(description) <= 30_000 and "truncated — see the attached file" in description


def test_preview_and_retry_rules(settings):
    with make_client(settings, FakeJira()) as c:
        pid = c.post("/api/projects", json={"name": "P"}).json()["id"]
        sid = new_session(c, pid)
        send_files(c, sid, ("jobs.xlsx", JOBS))
        run_id = c.post(f"/api/sessions/{sid}/discovery", headers=AUTHOR).json()["id"]
        preview = c.get(f"/api/runs/{run_id}/handoff/preview").json()
        assert preview["jira_configured"] is True and len(preview["attachments"]) == 3
        assert preview["description"].strip()
        assert c.post(f"/api/runs/{run_id}/handoff/retry", headers=AUTHOR).status_code == 409  # not approved yet
        c.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
        assert c.post(f"/api/runs/{run_id}/handoff/retry", headers=AUTHOR).status_code == 409  # already sent
