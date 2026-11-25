# Decisions

Short ADR log of anything changed from `TRINITY-WORKBENCH-BUILD-SPEC.md`.

## D1 — Fixture notebook name (spec §6.2 fixture note)
The reference fixture notebook is named `vlr_nokia_dly_bronze.ipynb` (renamed from `_loader`), so lint L1 stays strict: every path's base name equals `pipeline`. No `_loader` suffix exception.

## D2 — `make` on Windows
`make` is not installed on the dev machine (Windows 11). The Makefile is kept per spec and works under Git Bash/WSL with GNU make; README lists the equivalent direct commands.

## D3 — Backend venv location
Venv lives in `backend/.venv` (Python 3.12), git-ignored.

## D5 — Chat sessions inside steps (user-directed change, 2026-11-25)
The Workbench is chat-first. Each step holds multiple `ChatSession`s; a session's context is its own files and messages only (no cross-session leakage). Uploads dedupe per project (SHA-256) but are *linked* to each session that receives them. After an upload the server — not the LLM — posts a deterministic receipt and asks whether to start processing; "Start processing" streams a draft deliverable (DISC / ASSESS / TDD) saved as a `draft` artifact. The step gate (submit → approve by a different user) is kept, so Plan unlocks only after Discover is approved.

## D6 — LLM runner uses the Anthropic Messages API for now
`RUNNER=api` calls `client.beta.messages.stream` (model `claude-opus-5-5`, override with `MODEL`) with the server-side refusal fallback (`server-side-fallback-2026-07-01`, `fallbacks: "default"`, sent via `extra_body`). The spec's Claude Agent SDK runner (skills in a step workspace, hooks) is deferred to M4/M5; the `StepRunner` interface is unchanged so it can slot in. `RUNNER=stub` stays for tests and key-less use. Documents are sent as untrusted `<document>` blocks with a cache breakpoint.

## D7 — Document extraction
PDF (text layer only, no OCR), DOCX (paragraphs + tables), XLSX (first 5,000 rows per sheet), notebooks and plain-text formats are extracted at upload. Extracted text lives in `data/projects/<id>/extracted/`. Per-document cap 150k characters, per-session cap 800k; truncation is reported in the reply.

## D8 — Skills run one at a time, standalone (user-directed change, 2026-11-25)
The `data-migration` skills are written for Claude Code and chain on each other's artifacts (intent → discover → assess → …). The Workbench instead runs **one skill per run** on the session's documents, with a standalone adapter prompt: missing prerequisites become a "Missing inputs and assumptions" section, interview questions become "Open questions", and platform-specific checks (the pack targets Teradata/Cloudera → Snowflake/Redshift) become "N/A — reason" for Databricks. Skills a skill *references* (`templates/*.md`, `agents/*.md`) are attached automatically from `skills/data-migration/_support/` — other skills are never invoked.
Grouping lives in `config/skill-groups.yml`: `standalone` (default list), `advanced` (need cutover-era records), `phase` (the numbered chain — runnable but hidden behind a tab), `platform` (state-tracking, guardian-*, … — not runnable). Skills are keyed by their frontmatter `name`, not their folder (`00-intent-capture` → `intent`).
**Deviation from spec §5.4:** there is no approved "bundle"/lockfile yet. A human click on one skill is the approval; each run's evidence row records the skill key and SHA-256. `config/step-skill-map.yml` v0.4.0 now holds *suggestions* only (nothing required). Bundles, stale detection and "unticking" impact are deferred.

## D9 — Runner selection and hot-reload
`RUNNER=auto` (default) re-reads `.env` on every request and picks: a real `ANTHROPIC_API_KEY` → Claude API; else a logged-in Claude Code CLI (`claude auth status` → `loggedIn`) → **CLI runner**; else stub. Empty or `<placeholder>` keys don't count. Saving a key or running `/login` needs no API restart. Force one with `RUNNER=stub|api|cli`.

## D10 — CLI runner (no API key needed, 2026-11-25)
The user's account is a Claude app/subscription account without API access. `CliRunner` shells out to the official `claude` CLI headlessly (`-p --output-format stream-json --include-partial-messages`) and streams `text_delta` events, so usage bills to the subscription. It is isolated from the user's Claude Code setup: `--tools ""`, `--disable-slash-commands`, `--no-session-persistence`, `--setting-sources project` (no user hooks/plugins — a SessionStart hook otherwise injects text into every run), `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` (no user CLAUDE.md), empty working dir (`data/cli-workdir/`), API-key env vars stripped, and the system prompt passed via `--system-prompt-file` (skills can exceed the Windows command-line limit). Documents and the conversation go on stdin as one prompt. Trade-offs vs the API runner: no prompt caching, no model pinning (`CLI_MODEL=opus|sonnet` optional), subject to subscription usage limits, one subprocess per run. Intended for the user's own machine; the Anthropic-API runner remains for a key-based deployment.

## D11 — Discovery-only scope (user-directed change, 2026-11-25)
The Workbench now covers **discovery only**; planning and design are handed to ETL Crew after approval (hand-off not built yet). Plan/Design disappear from the UI; the step tables and legacy gate endpoints remain in the backend but are unused by the UI.
- **Skill chain per project**, default `intent → discover` (Trinity Discover = P0 + P1). Offered skills come from `config/discovery-skills.yml` (intent, discover, interview, estimate, risk, review) plus any uploaded `custom:*` skill. Selection is ordered and saved per project.
- **Add a skill**: upload `SKILL.md` or a `.zip` → `skills/custom/<name>/`. Name validated from frontmatter; only text files (.md/.txt/.yml/.yaml/.json/.csv) are written; zip path traversal refused; 50 files / 5 MB cap.
- **Runs** execute in a background thread (survive closing the tab), one at a time per project. Each skill gets the session's documents plus the earlier skills' outputs from the same run, and an output contract: one `=== ARTIFACT: TYPE ===` section per Trinity artifact (INTENT; DISC + INV; …), split into separate files. Skill SHA-256s are pinned at start; a skill changed mid-run fails the run. Runs interrupted by a server restart are marked failed on startup. Live partial output is held in memory for the UI to poll.
- **Versioning**: an artifact keeps its id across runs (`INTENT-2026-001`) and gets `@v2`, `@v3`…; files live in `data/projects/<id>/artifacts/`. A newer completed run supersedes an older one still awaiting approval; approving a run supersedes the previously approved one, so only the latest approved version counts.
- **Approval** is per run: completed runs wait for approval automatically; approver ≠ the person who started the run, role approver/owner; reject needs a reason. The API rewrites `status:`/`approved-by:` in each file's frontmatter (the model only ever writes drafts). Evidence rows: `discovery.run_start`, `run_complete`/`run_fail`, `approve`/`reject`, `skills_set`, `skill.upload`.

## D12 — ETL Crew hand-off through Jira (user-directed, 2026-11-25)
Approving a discovery run **automatically** hands it to ETL Crew (user's choice over a confirm button). A background job:
1. renders `config/jira/etl-crew-discovery-ticket.md` (committed, editable; YAML frontmatter = summary + labels, body = description with `{{placeholders}}`; plain text / wiki markup, intent excerpt in `{noformat}`, kept under 30,000 chars — Jira's limit is 32,767);
2. searches `labels = trinity-disc-<hash>` first (`/rest/api/3/search/jql`, falling back to `/rest/api/2/search`) so retries never duplicate;
3. creates the issue **unassigned** via `/rest/api/2/issue` (plain-string description), status = the project's initial status (To Do);
4. attaches the approved `.md` files (skipping names already attached);
5. **assigns** it to `JIRA_LEAD_ACCOUNT_ID` last — ETL Crew triggers on `issue_assigned` while To Do with a non-empty description (guide §2.2, §5.1 rule 1).
Auth: `JIRA_TOKEN` as Bearer (the `api.atlassian.com/ex/jira/<cloud-id>` form ETL Crew uses) or `JIRA_EMAIL` + `JIRA_API_TOKEN` as Basic. Settings are re-read from `.env` per attempt. Each step is recorded on a `Handoff` row so **Retry hand-off** resumes where it stopped; evidence rows `handoff.create`, `handoff.assign`, `handoff.fail`, `handoff.retry`. If Jira isn't configured or a call fails, the approval stands and the UI shows the error.
**Scope:** the Workbench's job ends at a correctly created, attached and assigned Jira issue. What happens after the assignment (ETL Crew's trigger, reading the ticket and attachments, planning and design) is already implemented on the ETL Crew side and out of scope here.

## D4 — Acting-as user
The acting user is sent in an `X-User-Id` header. Three local users are seeded: Author A, Approver B, Owner. This is a demo, not auth.
