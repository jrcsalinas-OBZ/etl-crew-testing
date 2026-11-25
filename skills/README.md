# Skill catalogue

Skill content is **never committed** (public repo, hard rule 7). These folders are git-ignored and live on local disk only:

| Folder | What goes in it | How to populate |
|---|---|---|
| `skills/data-migration/` | The standard pack: one `<skill>/SKILL.md` per skill | Copy the `skills/` folder of the installed `data-migration` plugin. Find it with `find ~ -path '*data-migration*' -name SKILL.md`. If it is not on disk, ask for an export — do not invent skill contents. |
| `skills/telcox/` | The 7 Telco X skills + `telcox-pipeline-design` (+ `telcox-etl-crew-handoff`, M6) | Convert the 7 source skill files with skill-creator, masking client tokens (spec §7.2), then copy the `telcox-*/SKILL.md` folders here. |
| `skills/custom/` | Skills uploaded in the app | Created by the app. |

The `data-migration` pack also needs its support files: copy the plugin's `templates/` and `agents/` folders into `skills/data-migration/_support/` (skills reference them, e.g. `templates/risk-register.md`; they are attached automatically when the skill runs).
Skills are keyed by the `name:` in their frontmatter, not the folder name. How each skill is grouped is set in `config/skill-groups.yml`.

The app starts with these folders empty and shows a warning banner.
Tests use the synthetic catalogue in `backend/tests/fixtures/skills/`, so `make test` passes on a fresh clone.

Also local-only: `config/forbidden_tokens.txt` — one real client token per line, used by the masking test (lint L8).
