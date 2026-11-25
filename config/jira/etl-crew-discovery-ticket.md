---
# Jira ticket created automatically when a discovery run is approved. Assigning it to the ETL Crew lead
# (JIRA_LEAD_ACCOUNT_ID) is what starts ETL Crew. Edit freely; placeholders are {{name}}:
#   project_name, run_number, approved_by, approved_at, skills, source_files, artifact_list,
#   intent_id, intent_excerpt, handoff_label
# The description is sent as plain text (Jira wiki markup); keep it non-empty — ETL Crew ignores empty briefs.
summary: "Trinity discovery hand-off — {{project_name}} (Discovery #{{run_number}})"
labels: [trinity, trinity-discovery]
---
Trinity discovery for {{project_name}} is approved. Please take this engagement through planning and design.

What to do:
1. Read every attached discovery file (listed below). They are approved: treat their facts and decisions as fixed; raise questions as comments on this issue instead of changing them.
2. Produce the migration plan (dispositions, waves, estimates, risks) and the target design for the in-scope objects.
3. Report progress and results as comments on this issue. Do not merge any pull request; leave it open for review.

Approved discovery (Discovery #{{run_number}}, approved by {{approved_by}} on {{approved_at}}):
{{artifact_list}}

Skills used: {{skills}}
Source documents reviewed: {{source_files}}

Intent summary (from {{intent_id}}; the full file is attached):
{noformat}
{{intent_excerpt}}
{noformat}

Hand-off id: {{handoff_label}} (used to avoid duplicate tickets)
