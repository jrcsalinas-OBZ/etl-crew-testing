---
artifact-id: INTENT-2026-001
artifact-type: intent
title: Trinity Workbench — governed Discover/Plan/Design workspace on the data-migration pack
parent-artifact: none
status: draft
trace-id: TRN-WB-2026-001
created: 2026-11-25T08:05:38Z
author: Joshua Salinas
approved-by: <pending — named reviewer, not the author>
client-alias: Telco X
activity: Stage 2 / Activity 1 (items 1–6)
interview: completed (project-type, constraints, stakeholders, success-criteria)
---
 
# INTENT-2026-001 — Trinity Workbench
 
## 1. Problem Statement
 
Trinity's `data-migration` skill pack (Phases 0–4: intent, discover, assess, design, waves) can today only be run as loose skills in a chat session. For a migration engagement such as Telco X, there is **no workspace** that:
 
1. holds an engagement's uploaded files once (no duplicates),
2. keeps **Discover, Plan and Design** in separate spaces, each with its own files, instructions, memory and skills,
3. **locks** each step until the previous step is approved by someone other than its author,
4. keeps only **approved, source-linked facts** in memory, which later steps can read but not change,
5. lets a human **approve which skills run** in each step (including the 7 Telco X pipeline skills in Design), and
6. turns an approved design into pipeline specs in the **Telco X 4-file layout plus separate DDL**.
**Impact:** migration architects (and the bootcamp demo) can't produce a governed, traceable design package. Gate evidence, approvals and fact provenance live in chat history, not in a system. That blocks a clean later hand-off to ETL Crew. It affects every engagement and every Trinity step run.
 
## 2. Desired Outcome and Success Criteria
 
A local, Claude-Project-like **Trinity Workbench** in `etl-crew-testing`. One project per engagement, each with three step spaces (Discover → Plan → Design), running the `data-migration` pack as its standard skills.
 
**Success = milestones M1–M5 of TRN-BOOTCAMP-WB-SPEC-001 pass their "Done when" checks, with automated tests:**
 
| # | Criterion (observable) | Spec ref |
|---|---|---|
| S1 | Uploading the same file twice leaves **1** DB row and **1** file on disk; a `.sql` with a planted instruction is flagged; `make seed` creates "Telco X — Network Feeds (demo)" | M1 §5.1 |
| S2 | Plan and Design are unreachable until Discover is approved; the author's approve call returns **403**; every transition writes an Evidence row first | M2 §5.2 |
| S3 | A proposed fact never appears in the compiled `CLAUDE.md`; an approved Discover fact appears in Plan's `CLAUDE.md`, and editing it from Plan returns **403** | M3 §5.3 |
| S4 | No run starts without an approved skill bundle; the 7 Telco X skills plus `telcox-pipeline-design` are offered **only in Design**; changing a `SKILL.md` marks the bundle stale; unticking `risk` in Plan shows the QG2 impact | M4 §5.4 |
| S5 | **Deterministic:** in the seeded demo, a Design run with the `StubRunner` and the reference fixture produces a PIPESPEC for `vlr_nokia_dly_bronze` that passes lint **L1–L8** (L8 = no forbidden client tokens), as an automated test. Runs through the Claude Agent SDK are supported but are not part of acceptance, because LLM output isn't repeatable. | M5 §5.5, §6.3 |
 
**Stretch (not a success criterion):** M6 hand-off package (ticket text plus PWP JSON assembled for later sending).
**Out of scope:** ETL Crew integration, Jira or GitHub calls, sending the package, Postgres/pgvector, auth/SSO.
 
## 3. Constraints and Scope Boundaries
 
### Constraints
 
| Type | Constraint |
|---|---|
| Team / time | Solo build (Joshua Salinas) with Claude Code. No fixed deadline; scope is what can be achieved now, milestone by milestone. |
| Repo | `github.com/jrcsalinas-OBZ/etl-crew-testing` (public, currently empty) holds the Workbench only; ETL Crew stays in its own repo. Because it's **public**, nothing client-identifying or secret may be committed: `skills/data-migration/`, `skills/telcox/`, `.env` and `config/forbidden_tokens.txt` are **git-ignored** and loaded from the local disk. |
| Tech | Python 3.12 + FastAPI + SQLite; React + Vite + TypeScript; Claude Agent SDK (Python) behind a `StepRunner` interface. Check option names against the current SDK docs and log any differences in `docs/DECISIONS.md`. |
| LLM | Anthropic API key only, from a git-ignored `.env`. Ship `.env.example` with placeholders. `StubRunner` stays for tests and runs without a key. |
| Skills | `data-migration` is the standard pack, copied from the installed plugin and never invented. The 7 Telco X skills are converted with skill-creator and offered in Design only. |
| Data / compliance | Synthetic Telco X data only. Client masking enforced by a git-ignored `config/forbidden_tokens.txt` and a grep test (lint L8). Uploads are treated as data, never as instructions. |
| Governance | AI recommends, humans decide. The agent can never set `approved`, and approver ≠ author is enforced server-side. This is a **demo**: Approver B is a simulated "Acting as" user, which is accepted for now. Real sign-off of this INTENT is pending a named reviewer. |
 
### Scope
 
- **In scope:** M1–M5 (projects and dedupe upload; step spaces, gates and evidence; memory; skill catalogue, picker and bundle lockfile; SDK runner, workspace builder, Telco X skills, `telcox-pipeline-design`, PIPESPEC and lint).
- **Stretch:** M6 hand-off package (render only).
- **Out:** Telco X → Databricks overlay for the `data-migration` pack (deferred; see Open items); ETL Crew, Jira or GitHub integration; sending artifacts; Postgres/pgvector and embeddings; auth/SSO and multi-user hosting; AWS deployment.
### Assumptions
 
- **A1:** The installed `data-migration` plugin's `SKILL.md` files can be found on disk and copied into the repo. If not, the user exports them.
- **A2:** The Python Agent SDK supports per-run skill scoping, `cwd` and `PreToolUse` hooks; any differences get an ADR.
- **A3:** The pack targets Teradata/Cloudera → Snowflake/Redshift. For Telco X → Databricks, it's adapted through an overlay, and checks that don't apply are marked "N/A — reason".
## 4. Selected Approach
 
**Selected: A — build TRN-BOOTCAMP-WB-SPEC-001 as written.** FastAPI + SQLite + React/Vite, with the Python Claude Agent SDK behind `StepRunner`, delivered milestone by milestone (M1 → M5). Each milestone ends with its "Done when" check and a human review before the next starts.
 
| Approach | Effort (L0) | Risk | Key assumption | Decision |
|---|---|---|---|---|
| **A. Spec as written** | ~8–12 dev days | Medium (two processes, split stack) | The Python SDK supports skill scoping, `cwd` and hooks | **Selected**: direct fit with the spec and the guide's ADR-BC-001/004; no spec rewrite |
| B. TypeScript full-stack (Next.js) | ~7–10 dev days | Medium (departs from ADR-BC-004) | The TS SDK has the same guardrails | Rejected: needs a rewrite of spec §2–3 |
| C. Streamlit thin UI | ~4–6 dev days | Low to build, high to rework | Streamlit UX is good enough | Rejected: weak Claude-Project UX, hard to grow |
 
- **Decomposition:** single track. M1–M5 are sequential milestones of one deliverable, so no sub-intents.
- **Traceability:** this INTENT is the parent of `claude/TRINITY-WORKBENCH-BUILD-SPEC.md`. The spec's frontmatter gets `parent-artifact: INTENT-2026-001`, and its acceptance bar is narrowed to M1–M5, with M6 as stretch.
## Review outcomes (data-migration:review, 2026-11-25)
 
| # | Finding | Decision | Change |
|---|---|---|---|
| G1 | The public repo could expose skill content | Accepted | Pack and Telco X skills are git-ignored (§3 Repo) |
| G2 | S5 depended on non-deterministic LLM output | Accepted | S5 made deterministic: StubRunner + fixture, automated test (§2) |
| G3 | No milestone owns the Databricks overlay | Parked | Out of scope; Open item O1 |
| G4 | Simulated four-eyes isn't real segregation | Rejected: it's a demo, accepted for now | Demo wording added to §3 Governance |
| C5 | Python 3.12 vs ETL Crew's 3.11 | Accepted | The Workbench and ETL Crew live in separate repos (§3 Repo) |
| C6 | The impact has no number | Rejected: fine as is | None |
| C7, M8, M9 | Pack-found check, frontmatter, L0 basis | Not discussed | Left as is |
 
## Open items
 
- **O1:** Telco X → Databricks overlay for `data-migration:design` and `data-migration:waves` (type map, N/A rules). Revisit after the Workbench core (M1–M5) works.
- ~~**O2:**~~ Done 2026-11-25: build spec v0.2.0 is linked to this INTENT, acceptance is M1–M5, §5.5 is deterministic, and skill folders are git-ignored.
## Section approval log
 
| Section | Approved by (author review) | When |
|---|---|---|
| 1. Problem Statement | Joshua Salinas (author) | 2026-11-25 |
| 2. Desired Outcome | Joshua Salinas (author) | 2026-11-25 |
| 3. Constraints and Scope | Joshua Salinas (author) | 2026-11-25 |
| 4. Selected Approach | Joshua Salinas (author) | 2026-11-25 |
 
> Author approval of sections ≠ gate approval. `status` stays `draft` until a named reviewer who is not the author approves the whole document (QG0).
 