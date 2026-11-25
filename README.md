# Trinity Workbench (local)

A local web app that works like Claude Projects, per migration engagement: three step spaces (Discover → Plan → Design), each with its own files, instructions, memory and approved skills. Built from `TRINITY-WORKBENCH-BUILD-SPEC.md`.

**Status:** M1 (projects + uploads) done; chat sessions, step gates and the Claude runner added ahead of M2–M5 (see `docs/DECISIONS.md` D5–D7). Skill picker, memory facts and the Telco X skills are still to come. This is a demo; "Acting as" is simulated, not auth.

## How it works

The Workbench does **discovery**; after approval the result goes to ETL Crew for planning and design.

1. Create a project (just a name) → **Start a session**. Each session has its own documents and context.
2. Attach design documents, job inventories, etc. (PDF, Word, Excel, SQL, CSV, text) — button, drag-and-drop, paste, or "add files by path". The assistant confirms what it received and asks whether to start discovery.
3. Optional: **Change discovery skills** — default is Trinity's `intent → discover`; add interview / estimate / risk / review, reorder, or upload your own skill (SKILL.md or .zip).
4. **Run discovery** — the skills run in order on the server (closing the tab is fine), streaming into the chat. Output: Trinity artifacts such as `INTENT-2026-001`, `DISC-…`, `INV-…`, each a versioned .md file.
5. The run then **waits for approval**. Switch "Acting as" to Approver B, open the run under **Runs**, read each file, and **Approve** or **Reject** (with a reason). Whoever started the run can't approve it. Running again creates new versions; only the latest approved run counts.
6. **Approval hands it to ETL Crew automatically**: a Jira issue is created from `config/jira/etl-crew-discovery-ticket.md`, the approved files are attached, and it is assigned to the ETL Crew lead (that assignment starts ETL Crew). Fill the `JIRA_*` settings in `.env` first; if they're missing or Jira errors, the run shows why and offers **Retry hand-off**.

**Connecting to Claude (pick one, no restart needed):**
- **Claude app account (no API key):** install Claude Code, run `claude` once and sign in with `/login`. The app detects it and calls the `claude` CLI on your subscription. The top bar shows "Claude · your account". Usage counts against your plan's limits.
- **API key:** put `ANTHROPIC_API_KEY=sk-ant-...` in `.env` at the repo root. A real key takes priority over the CLI.
- Neither: **stub mode** (scripted text, amber badge). Force a mode with `RUNNER=stub|api|cli` in `.env`.

## Run

```bash
make install   # backend/.venv (Python 3.12) + npm install
make seed      # creates "Telco X — Network Feeds (demo)"
make dev       # API :8000 + UI :5173
make test      # backend pytest + frontend typecheck
```

No `make` (e.g. plain Windows PowerShell)? Equivalent commands:

```powershell
cd backend; py -3.12 -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt
cd ..\frontend; npm install
cd ..\backend; .venv\Scripts\python -m app.seed
.venv\Scripts\python -m uvicorn app.main:create_app --factory --reload --port 8000   # terminal 1
cd ..\frontend; npm run dev                                                          # terminal 2
cd ..\backend; .venv\Scripts\python -m pytest -q; cd ..\frontend; npm run typecheck  # tests
```

## Public repo rules

Skill packs, `data/`, `.env` and `config/forbidden_tokens.txt` are git-ignored and live on local disk only — see `skills/README.md`. Fixtures are synthetic and use the client alias **Telco X** only. Deviations from the spec are logged in `docs/DECISIONS.md`.
