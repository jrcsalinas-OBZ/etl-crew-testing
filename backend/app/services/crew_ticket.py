"""Send an approved discovery run's ETL Crew ticket straight to the crew's opencode server, with the same two calls the
crew's Jira intake makes: POST /session {"title": "Jira: KEY"}, then POST /session/{id}/prompt_async.

The ticket is the run's CREW_TICKET artifact (written by the etl-crew-ticket skill). Its reference-file placeholders —
{{GIT:path}}, {{NB_CELLS}}, {{NB_CELL_COUNT}}, {{NB_META}}, {{S3_SQL}} — are filled the way render_ticket.py fills them:
from a git repo when TICKET_REFERENCE_REPO is set, otherwise from a snapshot learned from an example ticket
(python -m app.services.crew_ticket learn <ticket.src.md> <ticket.md>)."""
import base64
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import REPO_ROOT, Settings
from app.models import Artifact, CrewDispatch, DiscoveryRun, Handoff, Role, User
from app.services import chat, evidence
from app.services.skills import split_frontmatter

DOC_TYPE = "CREW_TICKET"
# VAR:name = an engagement value (e.g. a package folder named after the client) the model never sees or writes
TOKEN = re.compile(r"\{\{(GIT:[^}]+|NB_[A-Z_]+|S3_SQL|VAR:[a-z_]+)\}\}")
GAP = re.compile(r"<<MISSING:\s*([^>]*)>>")
NOTES = re.compile(r"^## Trinity notes\b.*", re.MULTILINE | re.DOTALL)  # reviewer-only tail, never sent
SECTION = re.compile(r"^## (\d+)\.", re.MULTILINE)
DUMMY_NOTE = "This is a dummy ticket; skip all Jira steps."
SNAPSHOT = Path("crew-ticket") / "reference.json"
TEMPLATE = Path("crew-ticket") / "template.src.md"  # the real template; its reference sections are used verbatim
STUB = "_Inserted by the Workbench when the ticket is sent. Write only this section's heading and this line._"


class CrewError(Exception):
    """An opencode call failed; the message is safe to show (never contains the password)."""


@dataclass(frozen=True)
class CrewConfig:
    opencode_url: str
    agent: str = "lead"
    username: str = ""
    password: str = ""
    public_url: str = ""  # opencode web UI, for "open session" links; defaults to opencode_url
    reference_repo: str = ""  # local clone holding the reference files ({{GIT:…}})
    reference_ref: str = "origin/main"
    reference_notebook: str = ""  # repo path of the reference notebook ({{NB_…}})
    s3_sql_file: str = ""  # local copy of the legacy SQL ({{S3_SQL}})
    ca_bundle: str = ""  # PEM file to trust for a self-signed opencode certificate; TLS is always verified
    tls_server_name: str = ""  # name the certificate is issued to, when it isn't the URL's host (e.g. a raw ALB name)

    @classmethod
    def from_env(cls) -> "CrewConfig":
        load_dotenv(REPO_ROOT / ".env", override=True)
        env = lambda name: (os.environ.get(name) or "").strip()
        return cls(opencode_url=env("OPENCODE_URL").rstrip("/"), agent=env("OPENCODE_AGENT") or "lead",
                   username=env("OPENCODE_USERNAME"), password=env("OPENCODE_PASSWORD"),
                   public_url=env("OPENCODE_PUBLIC_URL").rstrip("/"), reference_repo=env("TICKET_REFERENCE_REPO"),
                   reference_ref=env("TICKET_REFERENCE_REF") or "origin/main",
                   reference_notebook=env("TICKET_REFERENCE_NOTEBOOK"), s3_sql_file=env("TICKET_S3_SQL_FILE"),
                   ca_bundle=env("OPENCODE_CA_BUNDLE"), tls_server_name=env("OPENCODE_TLS_SERVER_NAME"))

    def missing(self) -> list[str]:
        return [] if self.opencode_url and not self.opencode_url.startswith("<") else ["OPENCODE_URL"]

    @property
    def auth(self) -> Optional[tuple[str, str]]:
        return (self.username, self.password) if self.username else None

    def session_link(self, directory: str, session_id: str) -> str:
        """opencode's UI routes sessions under the base64url-encoded project directory."""
        segment = base64.urlsafe_b64encode(directory.encode()).rstrip(b"=").decode() if directory else ""
        base = self.public_url or self.opencode_url
        return f"{base}/{segment}/session/{session_id}" if segment else ""


class OpencodeClient:
    def __init__(self, config: CrewConfig, transport: Optional[httpx.BaseTransport] = None):
        self.config = config
        self.http = httpx.Client(base_url=config.opencode_url, transport=transport, timeout=30, auth=config.auth,
                                 verify=config.ca_bundle or True)

    def _call(self, method: str, url: str, action: str, **kwargs) -> httpx.Response:
        if self.config.tls_server_name:  # verify the certificate against its own name instead of the URL host
            kwargs["extensions"] = {"sni_hostname": self.config.tls_server_name}
        try:
            response = self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            hint = ("check OPENCODE_CA_BUNDLE / OPENCODE_TLS_SERVER_NAME" if "CERTIFICATE" in str(exc)
                    else "is the server up and is your IP allowed?")
            raise CrewError(f"opencode {action} failed: could not reach {self.config.opencode_url} "
                            f"({type(exc).__name__}: {str(exc)[:160]}) — {hint}") from exc
        if response.status_code >= 400:
            hint = {401: " (check OPENCODE_USERNAME / OPENCODE_PASSWORD)", 404: " (unknown session or wrong OPENCODE_URL)"}
            raise CrewError(f"opencode {action} failed: HTTP {response.status_code}{hint.get(response.status_code, '')}"
                            f" — {response.text[:200]}")
        return response

    def create_session(self, title: str) -> dict:
        return self._call("POST", "/session", "create session", json={"title": title}).json()

    def prompt_async(self, session_id: str, payload: dict) -> None:
        self._call("POST", f"/session/{session_id}/prompt_async", "send prompt", json=payload)


OpencodeFactory = Callable[[CrewConfig], OpencodeClient]


# ---- reference values (render_ticket.py, generalised) -------------------------------------------

def notebook_values(notebook: dict) -> dict[str, str]:
    cells = []
    for i, cell in enumerate(notebook["cells"]):
        lang = "python" if cell["cell_type"] == "code" else "markdown"
        body = "".join(cell["source"])
        fence = "````" if "```" in body else "```"
        cells.append(f"**[{i}] {cell['cell_type']}**\n\n{fence}{lang}\n{body}\n{fence}")
    return {"NB_CELLS": "\n\n".join(cells), "NB_CELL_COUNT": str(len(notebook["cells"])),
            "NB_META": json.dumps(notebook["metadata"], indent=2)}


def _git_show(config: CrewConfig, path: str) -> Optional[str]:
    result = subprocess.run(["git", "-C", config.reference_repo, "show", f"{config.reference_ref}:{path}"],
                            capture_output=True, text=True, encoding="utf-8")
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def load_snapshot(settings: Settings) -> dict[str, str]:
    path = settings.data_dir / SNAPSHOT
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def reference_values(settings: Settings, config: CrewConfig, tokens: set[str]) -> dict[str, str]:
    """Snapshot first; a configured git repo / SQL file overrides it."""
    values = load_snapshot(settings)
    if config.reference_repo:
        for token in tokens:
            if token.startswith("GIT:") and (text := _git_show(config, token[4:])) is not None:
                values[token] = text
        if config.reference_notebook and any(t.startswith("NB_") for t in tokens):
            if (raw := _git_show(config, config.reference_notebook)) is not None:
                values.update(notebook_values(json.loads(raw)))
    if config.s3_sql_file and Path(config.s3_sql_file).is_file():
        values["S3_SQL"] = Path(config.s3_sql_file).read_text(encoding="utf-8").rstrip("\n")
    return values


def learn(source: str, rendered: str) -> dict[str, str]:
    """Recover each placeholder's value by aligning a ticket source with its rendered copy."""
    parts = TOKEN.split(source)  # literal, token, literal, token, …, literal
    if not rendered.startswith(parts[0]):
        raise ValueError("The rendered ticket does not start like the source ticket.")
    values, pos = {}, len(parts[0])
    for i in range(1, len(parts), 2):
        token, literal = parts[i], parts[i + 1]
        if not literal and i + 2 < len(parts):
            raise ValueError(f"{{{{{token}}}}} is directly followed by another placeholder; cannot align.")
        end = rendered.find(literal, pos) if literal else len(rendered)
        if end < 0:
            raise ValueError(f"Could not find the text that follows {{{{{token}}}}} in the rendered ticket.")
        value = rendered[pos:end]
        if values.setdefault(token, value) != value:
            raise ValueError(f"{{{{{token}}}}} has two different values in the rendered ticket.")
        pos = end + len(literal)
    if fill(source, values)[0] != rendered:
        raise ValueError("Re-rendering the source with the learned values does not reproduce the rendered ticket.")
    return values


def save_snapshot(settings: Settings, values: dict[str, str]) -> Path:
    path = settings.data_dir / SNAPSHOT
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**load_snapshot(settings), **values}
    path.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def split_sections(text: str) -> tuple[str, list[tuple[int, str]]]:
    """(text before '## 1.', [(number, section text incl. heading)])."""
    starts = list(SECTION.finditer(text))
    if not starts:
        return text, []
    bounds = [m.start() for m in starts] + [len(text)]
    return text[:bounds[0]], [(int(m.group(1)), text[bounds[i]:bounds[i + 1]]) for i, m in enumerate(starts)]


def restore_reference_sections(source: str, template: str) -> str:
    """Replace (or add) every numbered section that holds reference placeholders in the template with the
    template's own text, so the model can never mangle them."""
    fixed = {n: body for n, body in split_sections(template)[1] if TOKEN.search(body)}
    if not fixed:
        return source
    preamble, sections = split_sections(source)
    merged = {n: body for n, body in sections}
    for n, body in fixed.items():
        merged[n] = body if body.endswith("\n") else body + "\n"
    return preamble + "".join(merged[n] for n in sorted(merged))


def skill_template(template: str, variables: dict[str, str]) -> str:
    """The template the skill reads: reference sections stubbed out, engagement values replaced by {{VAR:name}}."""
    preamble, sections = split_sections(template)
    text = preamble + "".join(
        f"{body.splitlines()[0]}\n\n{STUB}\n\n---\n\n" if TOKEN.search(body) else body for _, body in sections)
    for name, value in variables.items():
        text = text.replace(value, f"{{{{VAR:{name}}}}}")
    return text


def load_template(settings: Settings) -> str:
    path = settings.data_dir / TEMPLATE
    return path.read_text(encoding="utf-8") if path.exists() else ""


def fill(source: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """(rendered text, placeholders left unresolved)."""
    unresolved = sorted({t for t in TOKEN.findall(source) if t not in values})
    return TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), source), unresolved


# ---- the ticket for a run ----------------------------------------------------------------------

@dataclass(frozen=True)
class Prepared:
    ticket_key: str
    dummy: bool  # no real Jira issue behind it: tell the crew to skip Jira steps
    artifact: str  # CREW_TICKET-2026-001@v2
    text: str  # the prompt text sent to opencode
    unresolved: list[str]  # reference placeholders with no value
    gaps: list[str]  # <<MISSING: …>> markers the skill left for a human

    @property
    def ready(self) -> bool:
        return not self.unresolved and not self.gaps

    def payload(self, agent: str) -> dict:
        return {"parts": [{"type": "text", "text": self.text}], "agent": agent}


def ticket_artifact(db: Session, run: DiscoveryRun) -> Optional[Artifact]:
    return db.exec(select(Artifact).where(Artifact.run_id == run.id, Artifact.doc_type == DOC_TYPE)
                   .order_by(Artifact.version.desc())).first()


def ticket_source(path: Path) -> str:
    """The artifact body without frontmatter, its level-1 title, or the reviewer-only 'Trinity notes' tail."""
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    body = re.sub(r"\A\s*# [^\n]*\n", "", body)
    return NOTES.sub("", body).strip() + "\n"


def ticket_key(db: Session, run: DiscoveryRun) -> tuple[str, bool]:
    jira = db.exec(select(Handoff).where(Handoff.run_id == run.id)).first()
    if jira and jira.jira_key:
        return jira.jira_key, False
    return f"DUMMY-{run.id}", True


def prepare(db: Session, settings: Settings, run: DiscoveryRun, config: CrewConfig) -> Prepared:
    art = ticket_artifact(db, run)
    if art is None:
        raise HTTPException(409, f"Discovery #{run.number} has no {DOC_TYPE} artifact. Add the etl-crew-ticket skill "
                                 "to the discovery chain and run discovery again.")
    source = restore_reference_sections(ticket_source(Path(art.path)), load_template(settings))
    rendered, unresolved = fill(source, reference_values(settings, config, set(TOKEN.findall(source))))
    key, dummy = ticket_key(db, run)
    text = f"[Jira: {key}]\n\n" + (DUMMY_NOTE + "\n\n" if dummy else "") + rendered
    gaps = list(dict.fromkeys(g.strip() for g in GAP.findall(source)))  # each distinct gap once
    return Prepared(ticket_key=key, dummy=dummy, artifact=f"{art.artifact_id}@v{art.version}", text=text,
                    unresolved=unresolved, gaps=gaps)


def payload_filename(prepared: Prepared) -> str:
    return f"crew-ticket-{prepared.ticket_key}.json"


def curl_script(config: CrewConfig, prepared: Prepared) -> str:
    """The same two calls as a shell script; the prompt goes in the downloaded payload file."""
    title = shlex.quote(json.dumps({"title": f"Jira: {prepared.ticket_key}"}))
    read_id = "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"id\"])'"
    url = httpx.URL(config.opencode_url or "http://127.0.0.1:4096")
    setup, opts = [], ""
    if config.tls_server_name and url.host != config.tls_server_name:
        # curl checks the certificate against the URL's host: address the cert's name and connect to the real host
        port = url.port or 443
        opts += f" --connect-to {config.tls_server_name}:{port}:{url.host}:{port}"
        url = url.copy_with(host=config.tls_server_name)
    if config.ca_bundle:
        setup.append(f"OPENCODE_CA_BUNDLE={shlex.quote(config.ca_bundle)}")
        opts += ' --cacert "$OPENCODE_CA_BUNDLE"'
    if config.username:
        opts += ' -u "$OPENCODE_USERNAME:$OPENCODE_PASSWORD"'
    return "\n".join([
        *setup,
        f"OPENCODE_URL={shlex.quote(str(url).rstrip('/'))}",
        f"SESSION_ID=$(curl -sS{opts} -X POST \"$OPENCODE_URL/session\" -H 'Content-Type: application/json' "
        f"-d {title} | {read_id})",
        f"curl -sS{opts} -X POST \"$OPENCODE_URL/session/$SESSION_ID/prompt_async\" "
        f"-H 'Content-Type: application/json' --data-binary @{payload_filename(prepared)}",
        'echo "opencode session: $SESSION_ID"',
    ])


# ---- sending -----------------------------------------------------------------------------------

def view(db: Session, run: DiscoveryRun) -> Optional[dict]:
    record = db.exec(select(CrewDispatch).where(CrewDispatch.run_id == run.id)).first()
    return record.model_dump(mode="json") if record else None


def send(db: Session, settings: Settings, run: DiscoveryRun, user: User, config: CrewConfig,
         factory: Optional[OpencodeFactory] = None, force: bool = False, again: bool = False) -> CrewDispatch:
    """Create the opencode session (once — a retry reuses it) and post the ticket to the lead agent.
    force=True sends despite <<MISSING>> gaps (the Lead sees the markers); unresolved placeholders always block.
    again=True re-sends a sent ticket in a new session (for re-testing); the earlier sessions stay in the evidence."""
    if user.role == Role.viewer:
        raise HTTPException(403, "Viewers cannot send tickets to ETL Crew.")
    if run.approval != "approved":
        raise HTTPException(409, "Only an approved discovery run can be sent to ETL Crew.")
    record = db.exec(select(CrewDispatch).where(CrewDispatch.run_id == run.id)).first()
    previous = record.session_id if record and record.status == "sent" else ""
    if previous and not again:
        raise HTTPException(409, f"Already sent to ETL Crew (opencode session {previous}). Use Send again for a new session.")
    if config.missing():
        raise HTTPException(409, "opencode is not configured. Set in .env: " + ", ".join(config.missing()))
    prepared = prepare(db, settings, run, config)
    if prepared.unresolved:
        raise HTTPException(409, "No value for these reference placeholders: " + ", ".join(prepared.unresolved)
                            + ". Set TICKET_REFERENCE_REPO or learn a reference snapshot.")
    if prepared.gaps and not force:
        raise HTTPException(409, f"The ticket still has {len(prepared.gaps)} gaps to fill: " + "; ".join(prepared.gaps)
                            + ". Send anyway to let the ETL Crew lead handle them.")

    record = record or CrewDispatch(run_id=run.id, project_id=run.project_id)
    record.ticket_key, record.artifact, record.sent_by = prepared.ticket_key, prepared.artifact, user.id
    record.attempts += 1
    if previous:
        record.session_id, record.session_url = "", ""
    evidence.record(db, project_id=run.project_id, actor_id=user.id, action="crew.dispatch", target_type="discovery_run",
                    target_id=run.id, after={"ticket_key": prepared.ticket_key, "artifact": prepared.artifact,
                                             "opencode_url": config.opencode_url, "agent": config.agent,
                                             "sent_with_gaps": prepared.gaps, "replaces_session": previous})
    client = (factory or OpencodeClient)(config)
    try:
        if not record.session_id:
            suffix = f" #{record.attempts}" if record.attempts > 1 else ""  # tell repeat sends apart in opencode's list
            session = client.create_session(f"Jira: {prepared.ticket_key}{suffix}")
            record.session_id = session["id"]
            record.session_url = config.session_link(session.get("directory", ""), session["id"])
        client.prompt_async(record.session_id, prepared.payload(config.agent))
        record.status, record.error = "sent", ""
        link = f" — [open session]({record.session_url})" if record.session_url else ""
        chat.add_message(db, run.session_id, "assistant",
                         f"🚀 Discovery #{run.number} ticket **{prepared.ticket_key}** was sent to ETL Crew "
                         f"(opencode session `{record.session_id}`){link}.", kind="note")
    except CrewError as exc:
        record.status, record.error = "failed", str(exc)
        evidence.record(db, project_id=run.project_id, actor_id=user.id, action="crew.dispatch_fail",
                        target_type="discovery_run", target_id=run.id, after={"error": record.error})
    record.updated_at = datetime.now(timezone.utc)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _cli(argv: list[str]) -> int:
    """learn <ticket.src.md> <ticket.md> [--var name=value ...] [--skill-template PATH]

    Learns the reference values, keeps the real template for verbatim reference sections, and (with
    --skill-template) writes the copy the skill reads, where each --var value becomes {{VAR:name}}."""
    if len(argv) < 3 or argv[0] != "learn":
        print(_cli.__doc__)
        return 2
    source_path, rendered_path, rest = argv[1], argv[2], argv[3:]
    variables, skill_path = {}, None
    while rest:
        flag, value, rest = rest[0], rest[1], rest[2:]
        if flag == "--var":
            name, _, val = value.partition("=")
            if not re.fullmatch(r"[a-z_]+", name) or len(val) < 4:
                raise SystemExit(f"--var needs name=value with a lower_snake name and a value of 4+ characters: {value}")
            variables[name] = val
        elif flag == "--skill-template":
            skill_path = Path(value)
        else:
            raise SystemExit(f"unknown option {flag}")
    source = Path(source_path).read_text(encoding="utf-8")
    values = learn(source, Path(rendered_path).read_text(encoding="utf-8"))
    settings = Settings.from_env()
    path = save_snapshot(settings, {**values, **{f"VAR:{k}": v for k, v in variables.items()}})
    (settings.data_dir / TEMPLATE).write_text(source, encoding="utf-8")
    print(f"learned {len(values)} reference values and {len(variables)} variables -> {path}")
    if skill_path:
        skill_path.write_text(skill_template(source, variables), encoding="utf-8")
        print(f"skill template -> {skill_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
