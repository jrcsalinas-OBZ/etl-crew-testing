import base64
import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import Settings
from app.db import make_engine
from app.main import create_app
from app.models import Evidence
from app.services import crew_ticket
from app.services.crew_ticket import CrewConfig, OpencodeClient
from tests.conftest import NO_JIRA, TEST_SKILLS
from tests.test_chat_sessions import AUTHOR, APPROVER, JOBS, new_session, send_files

CONFIG = CrewConfig(opencode_url="http://opencode.test")
TICKET = """=== ARTIFACT: CREW_TICKET ===
# ETL Crew ticket — sales.daily_summary

## New Pipeline: Daily Sales Summary (sales.daily_summary)

### 6.1 Reference job YML

```yaml
{{GIT:ref/jobs/job.yml}}
```

A notebook with {{NB_CELL_COUNT}} cells.
{gap}
## Trinity notes

- Assumed the target schema from the inventory.
"""
REFERENCE = {"GIT:ref/jobs/job.yml": "resources:\n  jobs: {}", "NB_CELL_COUNT": "30"}


class TicketRunner:
    name = "fake"

    def __init__(self, gap: str = "", ticket: str = TICKET):
        self.ticket = ticket.replace("{gap}", gap)

    def stream(self, run):
        if run.artifact_types == ("CREW_TICKET",):
            yield self.ticket
        else:
            yield "".join(f"=== ARTIFACT: {t} ===\n# {t}\nsynthetic\n" for t in run.artifact_types)


class FakeOpencode:
    def __init__(self, fail_prompt: int = 0):
        self.calls: list[tuple[str, dict]] = []
        self.fail_prompt = fail_prompt

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.method == "POST" and request.url.path == "/session":
            self.calls.append(("session", body))
            return httpx.Response(200, json={"id": f"ses_{len(self.calls)}", "directory": "/work/crew", "title": body["title"]})
        if request.method == "POST" and request.url.path.endswith("/prompt_async"):
            self.calls.append((request.url.path, body))
            if self.fail_prompt:
                self.fail_prompt -= 1
                return httpx.Response(500, text="provider error")
            return httpx.Response(204)
        return httpx.Response(404, text="not found")


@pytest.fixture
def crew_settings(tmp_path) -> Settings:
    skills_dir = tmp_path / "skills"
    shutil.copytree(TEST_SKILLS, skills_dir)
    (skills_dir / "custom" / "etl-crew-ticket").mkdir(parents=True)
    (skills_dir / "custom" / "etl-crew-ticket" / "SKILL.md").write_text(
        "---\nname: etl-crew-ticket\ndescription: Synthetic ticket skill.\n---\n\n# ETL Crew ticket\n", encoding="utf-8")
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
                        skills_dir=skills_dir)
    crew_ticket.save_snapshot(settings, REFERENCE)
    return settings


def make_client(settings, fake: FakeOpencode, runner=None, config: CrewConfig = CONFIG, jira_config=NO_JIRA, **kw):
    return TestClient(create_app(settings, runner=runner or TicketRunner(), inline_runs=True, jira_config=jira_config,
                                 crew_config=lambda: config,
                                 opencode_factory=lambda cfg: OpencodeClient(cfg, transport=httpx.MockTransport(fake.handler)),
                                 **kw))


def completed_run(c):
    pid = c.post("/api/projects", json={"name": "Sales"}).json()["id"]
    sid = new_session(c, pid)
    send_files(c, sid, ("jobs.xlsx", JOBS))
    return pid, sid, c.post(f"/api/sessions/{sid}/discovery", headers=AUTHOR).json()["id"]


# ---- rendering ---------------------------------------------------------------------------------

def test_learn_recovers_each_placeholder_from_a_rendered_example():
    source = "# T\n```\n{{GIT:a.yml}}\n```\ncells: {{NB_CELL_COUNT}}\n```\n{{GIT:a.yml}}\n```\n"
    rendered = source.replace("{{GIT:a.yml}}", "k: 1\nv: 2").replace("{{NB_CELL_COUNT}}", "7")
    assert crew_ticket.learn(source, rendered) == {"GIT:a.yml": "k: 1\nv: 2", "NB_CELL_COUNT": "7"}
    with pytest.raises(ValueError):
        crew_ticket.learn(source, rendered.replace("cells:", "rows:"))


def test_a_self_signed_cert_is_verified_against_its_own_name():
    config = CrewConfig(opencode_url="https://alb.example.com", ca_bundle="cert.pem", tls_server_name="crew.internal")
    seen = []
    client = OpencodeClient(config, transport=httpx.MockTransport(
        lambda r: seen.append(r.extensions.get("sni_hostname")) or httpx.Response(200, json={"id": "s1"})))
    client.create_session("Jira: X-1")
    assert seen == ["crew.internal"]

    prepared = crew_ticket.Prepared("X-1", False, "A", "text", [], [])
    script = crew_ticket.curl_script(config, prepared)
    assert "OPENCODE_URL=https://crew.internal" in script and "-k" not in script.split()
    assert '--connect-to crew.internal:443:alb.example.com:443 --cacert "$OPENCODE_CA_BUNDLE"' in script


def test_notebook_cells_render_like_render_ticket():
    values = crew_ticket.notebook_values({"metadata": {"k": 1}, "cells": [
        {"cell_type": "markdown", "source": ["# Title"]}, {"cell_type": "code", "source": ["x = '```'\n", "y = 1"]}]})
    assert values["NB_CELL_COUNT"] == "2" and json.loads(values["NB_META"]) == {"k": 1}
    assert values["NB_CELLS"] == "**[0] markdown**\n\n```markdown\n# Title\n```\n\n**[1] code**\n\n````python\nx = '```'\ny = 1\n````"


TEMPLATE = """## New Pipeline: Example

## 1. Identity

- Path: src/{{VAR:package_dir}}/example

---

## 6. Reference Files

```yaml
{{GIT:ref/{{VAR:package_dir}}/job.yml}}
```

---

## 7. Target Files
"""


def test_reference_sections_come_from_the_template_and_the_skill_never_sees_them():
    template = TEMPLATE.replace("{{VAR:package_dir}}", "acme")  # the real template names the client
    skill_copy = crew_ticket.skill_template(template, {"package_dir": "acme"})
    assert "acme" not in skill_copy and "src/{{VAR:package_dir}}/example" in skill_copy
    assert "{{GIT" not in skill_copy and crew_ticket.STUB in skill_copy
    generated = ("## New Pipeline: Sales\n\n## 1. Identity\n\n- Path: src/{{VAR:package_dir}}/sales\n\n---\n\n"
                 "## 6. Reference Files\n\n{{GIT:ref/<<MISSING: package directory>>/job.yml}}\n\n---\n\n## 7. Target Files\n")
    restored = crew_ticket.restore_reference_sections(generated, template)
    assert "{{GIT:ref/acme/job.yml}}" in restored and "<<MISSING" not in restored
    assert restored.index("## 1.") < restored.index("## 6.") < restored.index("## 7.")
    text, unresolved = crew_ticket.fill(restored, {"GIT:ref/acme/job.yml": "jobs: {}", "VAR:package_dir": "acme"})
    assert unresolved == [] and "src/acme/sales" in text and "jobs: {}" in text
    # a ticket that left the section out gets it inserted in order
    assert "## 6. Reference Files" in crew_ticket.restore_reference_sections(generated.split("## 6.")[0] + "## 7. T\n", template)


def test_the_ticket_skill_is_in_the_default_chain_when_installed(crew_settings):
    with make_client(crew_settings, FakeOpencode()) as c:
        pid, _, run_id = completed_run(c)
        run = c.get(f"/api/runs/{run_id}").json()
    assert run["skills"] == ["intent", "discover", "etl-crew-ticket"]
    assert [a["doc_type"] for a in run["artifacts"]] == ["INTENT", "DISC", "INV", "CREW_TICKET"]


def test_preview_fills_reference_placeholders_and_drops_reviewer_notes(crew_settings):
    with make_client(crew_settings, FakeOpencode()) as c:
        _, _, run_id = completed_run(c)
        preview = c.get(f"/api/runs/{run_id}/crew-ticket").json()
        payload = c.get(f"/api/runs/{run_id}/crew-ticket/payload")
    key = f"DUMMY-{run_id}"
    assert preview["ticket_key"] == key and preview["dummy"] is True and preview["ready"] is True
    text = preview["text"]
    assert text.startswith(f"[Jira: {key}]\n\nThis is a dummy ticket; skip all Jira steps.\n\n## New Pipeline: Daily Sales")
    assert "resources:\n  jobs: {}" in text and "with 30 cells" in text and "{{" not in text
    assert "Trinity notes" not in text and "# ETL Crew ticket —" not in text
    assert preview["artifact"].startswith("CREW_TICKET-") and preview["artifact"].endswith("@v1")
    assert "/session/$SESSION_ID/prompt_async" in preview["curl"] and f"@crew-ticket-{key}.json" in preview["curl"]
    assert payload.headers["content-disposition"] == f'attachment; filename="crew-ticket-{key}.json"'
    assert payload.json() == {"parts": [{"type": "text", "text": text}], "agent": "lead"}


# ---- sending -----------------------------------------------------------------------------------

def test_approved_ticket_is_sent_to_the_lead_agent_once(crew_settings):
    fake = FakeOpencode()
    with make_client(crew_settings, fake) as c:
        pid, sid, run_id = completed_run(c)
        assert c.post(f"/api/runs/{run_id}/crew-ticket/send", headers=AUTHOR).status_code == 409  # not approved yet
        c.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
        sent = c.post(f"/api/runs/{run_id}/crew-ticket/send", headers=APPROVER).json()
        again = c.post(f"/api/runs/{run_id}/crew-ticket/send", headers=APPROVER)
        run = c.get(f"/api/runs/{run_id}").json()
        messages = c.get(f"/api/sessions/{sid}").json()["messages"]
    key = f"DUMMY-{run_id}"
    assert [kind for kind, _ in fake.calls] == ["session", "/session/ses_1/prompt_async"]
    assert fake.calls[0][1] == {"title": f"Jira: {key}"}
    prompt = fake.calls[1][1]
    assert prompt["agent"] == "lead" and prompt["parts"][0]["text"].startswith(f"[Jira: {key}]")
    segment = base64.urlsafe_b64encode(b"/work/crew").rstrip(b"=").decode()
    assert sent["status"] == "sent" and sent["session_id"] == "ses_1"
    assert sent["session_url"] == f"http://opencode.test/{segment}/session/ses_1"
    assert again.status_code == 409 and run["crew"]["status"] == "sent"
    assert "sent to ETL Crew" in messages[-1]["content"]
    with Session(make_engine(crew_settings.database_url)) as s:
        actions = [e.action for e in s.exec(select(Evidence).where(Evidence.project_id == pid))]
    assert actions[-1] == "crew.dispatch"


def test_send_again_starts_a_new_session_and_keeps_the_old_one_in_evidence(crew_settings):
    fake = FakeOpencode()
    with make_client(crew_settings, fake) as c:
        pid, _, run_id = completed_run(c)
        c.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
        c.post(f"/api/runs/{run_id}/crew-ticket/send", headers=APPROVER)
        resent = c.post(f"/api/runs/{run_id}/crew-ticket/send?again=true", headers=APPROVER).json()
    assert [kind for kind, _ in fake.calls].count("session") == 2
    assert fake.calls[2][1] == {"title": f"Jira: DUMMY-{run_id} #2"}
    assert resent["status"] == "sent" and resent["session_id"] == "ses_3" and resent["attempts"] == 2
    with Session(make_engine(crew_settings.database_url)) as s:
        dispatches = [e.after_json for e in s.exec(select(Evidence).where(Evidence.action == "crew.dispatch"))]
    assert '"replaces_session": "ses_1"' in dispatches[-1]


def test_failed_prompt_is_recorded_and_the_retry_reuses_the_session(crew_settings):
    fake = FakeOpencode(fail_prompt=1)
    with make_client(crew_settings, fake) as c:
        _, _, run_id = completed_run(c)
        c.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
        failed = c.post(f"/api/runs/{run_id}/crew-ticket/send", headers=APPROVER).json()
        retried = c.post(f"/api/runs/{run_id}/crew-ticket/send", headers=APPROVER).json()
    assert failed["status"] == "failed" and "HTTP 500" in failed["error"]
    assert retried["status"] == "sent" and retried["attempts"] == 2
    assert [kind for kind, _ in fake.calls].count("session") == 1


@pytest.mark.parametrize("runner, config, expected", [
    (TicketRunner(gap="<<MISSING: target repository>>\n"), CONFIG, "gaps to fill: target repository"),
    (TicketRunner(ticket=TICKET.replace("{{NB_CELL_COUNT}}", "{{NB_META}}")), CONFIG, "placeholders: NB_META"),
    (TicketRunner(), CrewConfig(opencode_url=""), "Set in .env: OPENCODE_URL"),
])
def test_incomplete_tickets_and_missing_settings_are_never_sent(crew_settings, runner, config, expected):
    fake = FakeOpencode()
    with make_client(crew_settings, fake, runner=runner, config=config) as c:
        _, _, run_id = completed_run(c)
        c.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
        r = c.post(f"/api/runs/{run_id}/crew-ticket/send", headers=APPROVER)
    assert r.status_code == 409 and expected in r.json()["detail"]
    assert fake.calls == []


def test_gaps_are_listed_once_and_can_be_sent_anyway(crew_settings):
    fake = FakeOpencode()
    gap = "<<MISSING: target catalog>> and again <<MISSING: target catalog>>\n"
    with make_client(crew_settings, fake, runner=TicketRunner(gap=gap)) as c:
        pid, _, run_id = completed_run(c)
        c.post(f"/api/runs/{run_id}/approve", headers=APPROVER)
        assert c.get(f"/api/runs/{run_id}/crew-ticket").json()["gaps"] == ["target catalog"]
        sent = c.post(f"/api/runs/{run_id}/crew-ticket/send?force=true", headers=APPROVER).json()
    assert sent["status"] == "sent"
    assert "<<MISSING: target catalog>>" in fake.calls[1][1]["parts"][0]["text"]  # the lead sees the gap
    with Session(make_engine(crew_settings.database_url)) as s:
        last = s.exec(select(Evidence).where(Evidence.project_id == pid).order_by(Evidence.id.desc())).first()
    assert json.loads(last.after_json)["sent_with_gaps"] == ["target catalog"]


def test_a_run_without_the_ticket_skill_explains_how_to_get_one(client, project_id):
    sid = new_session(client, project_id)
    send_files(client, sid, ("jobs.xlsx", JOBS))
    run_id = client.post(f"/api/sessions/{sid}/discovery", headers=AUTHOR).json()["id"]
    r = client.get(f"/api/runs/{run_id}/crew-ticket")
    assert r.status_code == 409 and "etl-crew-ticket" in r.json()["detail"]
