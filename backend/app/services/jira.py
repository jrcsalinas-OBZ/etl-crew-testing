"""Minimal Jira Cloud client for the ETL Crew hand-off. Configuration comes from .env (re-read on every use)."""
import base64
import os
from dataclasses import dataclass
from typing import Optional

import httpx
from dotenv import load_dotenv

from app.config import REPO_ROOT


class JiraError(Exception):
    """A Jira call failed; the message is safe to show (never contains the token)."""


@dataclass(frozen=True)
class JiraConfig:
    base_url: str  # https://api.atlassian.com/ex/jira/<cloud-id>  or  https://<site>.atlassian.net
    project_key: str
    lead_account_id: str
    issue_type: str = "Task"
    token: str = ""  # service-account / OAuth token, sent as Bearer
    email: str = ""  # with api_token: classic Basic auth
    api_token: str = ""
    site_url: str = ""  # https://<site>.atlassian.net, for browse links when base_url is api.atlassian.com

    @classmethod
    def from_env(cls) -> "JiraConfig":
        load_dotenv(REPO_ROOT / ".env", override=True)
        env = lambda name: (os.environ.get(name) or "").strip()
        return cls(base_url=env("JIRA_BASE_URL").rstrip("/"), project_key=env("JIRA_PROJECT_KEY"),
                   lead_account_id=env("JIRA_LEAD_ACCOUNT_ID"), issue_type=env("JIRA_ISSUE_TYPE") or "Task",
                   token=env("JIRA_TOKEN"), email=env("JIRA_EMAIL"), api_token=env("JIRA_API_TOKEN"),
                   site_url=env("JIRA_SITE_URL").rstrip("/"))

    def missing(self) -> list[str]:
        needed = {"JIRA_BASE_URL": self.base_url, "JIRA_PROJECT_KEY": self.project_key,
                  "JIRA_LEAD_ACCOUNT_ID": self.lead_account_id}
        gaps = [name for name, value in needed.items() if not value or value.startswith("<")]
        if not (self.token or (self.email and self.api_token)):
            gaps.append("JIRA_TOKEN (or JIRA_EMAIL + JIRA_API_TOKEN)")
        return gaps

    def browse_url(self, key: str) -> str:
        site = self.site_url or (self.base_url if "atlassian.net" in self.base_url else "")
        return f"{site}/browse/{key}" if site else ""

    def auth_header(self) -> str:
        if self.token:
            return f"Bearer {self.token}"
        return "Basic " + base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()


class JiraClient:
    def __init__(self, config: JiraConfig, transport: Optional[httpx.BaseTransport] = None):
        self.config = config
        self.http = httpx.Client(base_url=config.base_url, transport=transport, timeout=30,
                                 headers={"Authorization": config.auth_header(), "Accept": "application/json"})

    def _check(self, response: httpx.Response, action: str) -> httpx.Response:
        if response.status_code >= 400:
            try:
                body = response.json()
                detail = "; ".join(body.get("errorMessages") or []) or "; ".join(f"{k}: {v}" for k, v in (body.get("errors") or {}).items())
            except ValueError:
                detail = response.text[:200]
            hint = {401: " (check the Jira token)", 403: " (the account lacks permission)", 404: " (check JIRA_BASE_URL / project key)"}
            raise JiraError(f"Jira {action} failed: HTTP {response.status_code}{hint.get(response.status_code, '')}"
                            f"{' — ' + detail if detail else ''}")
        return response

    def _call(self, method: str, url: str, action: str, **kwargs) -> httpx.Response:
        try:
            return self._check(self.http.request(method, url, **kwargs), action)
        except httpx.HTTPError as exc:
            raise JiraError(f"Jira {action} failed: could not reach {self.config.base_url} ({type(exc).__name__})") from exc

    def find_by_label(self, label: str) -> Optional[dict]:
        jql = f'project = "{self.config.project_key}" AND labels = "{label}" ORDER BY created DESC'
        params = {"jql": jql, "fields": "status,assignee,attachment", "maxResults": 1}
        try:
            response = self._call("GET", "/rest/api/3/search/jql", "search", params=params)
        except JiraError as exc:
            if "HTTP 404" not in str(exc) and "HTTP 410" not in str(exc):
                raise
            response = self._call("GET", "/rest/api/2/search", "search", params=params)  # older sites / proxies
        issues = response.json().get("issues") or []
        return issues[0] if issues else None

    def create_issue(self, summary: str, description: str, labels: list[str], issue_type: str) -> str:
        fields = {"project": {"key": self.config.project_key}, "summary": summary, "description": description,
                  "issuetype": {"name": issue_type}, "labels": labels}
        return self._call("POST", "/rest/api/2/issue", "create issue", json={"fields": fields}).json()["key"]

    def attachment_names(self, key: str) -> set[str]:
        fields = self._call("GET", f"/rest/api/2/issue/{key}", "read issue", params={"fields": "attachment"}).json()
        return {a["filename"] for a in (fields.get("fields") or {}).get("attachment") or []}

    def attach(self, key: str, files: list[tuple[str, bytes]]) -> None:
        if files:
            self._call("POST", f"/rest/api/2/issue/{key}/attachments", "attach files",
                       headers={"X-Atlassian-Token": "no-check"},
                       files=[("file", (name, data, "text/markdown")) for name, data in files])

    def assign(self, key: str, account_id: str) -> None:
        self._call("PUT", f"/rest/api/2/issue/{key}/assignee", "assign issue", json={"accountId": account_id})
