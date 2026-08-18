---
name: sql-engineering
description: "Governed, file-backed SQL work for local projects with immutable versions, explicit rules, optional read-only execution, and auditable results."
metadata:
  short-description: Governed SQL, assets, and delivery
  version: "4.229.0"
  edition: public
---

# SQL Engineering

Use this skill to turn a data question into a durable local artifact. The public edition keeps
the useful engineering contracts from the full project while removing organization-specific
schemas, rules, credentials, result files, and private service integrations.

## Start here

Read `references/operating-contract.md` and `references/project-workflow.md`. For a new project,
read `references/project-contract.md` and `references/setup-onboarding.md`. For a small first run,
use the compatibility commands in `scripts/sql_workspace.py`; advanced flows use
`scripts/sql_project.py` and the capability registry.

```powershell
python .\setup\scripts\bootstrap_repo.py configure --root . --planning-provider none
python .\setup\scripts\bootstrap_repo.py demo --root .
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "Counts distinct fictional users by date." `
  --kind temporary
```

## Core contract

1. Save every generated or modified executable SQL file in a project workspace.
2. Never overwrite an earlier version; revisions use the next `vNNN` in the same query family.
3. Deliver only an exact path whose metadata, index, and content hash agree.
4. Keep raw source evidence, planning evidence, confirmed knowledge, rules, SQL, and results as
   separate lifecycle objects.
5. Do not invent table names, event semantics, business IDs, date policy, or metric definitions.
6. Automatic execution is optional, read-only, and adapter-based. A missing adapter produces a
   manual handoff rather than a claim that the query ran.
7. SQL never performs privacy masking or de-identification. Keep required business identifiers
   unchanged and apply platform privacy policy at the configured execution surface.
8. Results, visualizations, validation, and formal asset packages must point to the exact SQL
   version and content hash that produced them.

## Capability routes

The machine-readable registry is `references/capabilities.json`. The main routes are:

- `PROJECT_ADMIN`: initialize and validate a project.
- `SOURCE_INTAKE` / `PLANNING_SOURCE` / `KNOWLEDGE`: register and bind evidence.
- `QUERY`: save, revise, search, and inspect workspace SQL.
- `VALIDATION` / `RESULT_VISUALIZATION`: attach evidence and create bounded presentations.
- `SQL_FORMALIZE` / `DASHBOARD`: promote confirmed work into formal package members.
- `REVIEW`: inspect SQL from product and code perspectives.
- `ASSET_CATALOG` / `ASSET_ORGANIZATION`: build explicit read-only shared indexes.

`QUERY_EXECUTE` selects one explicit execution surface after an exact receipt. Direct execution
uses `sql_execute.py` with a project-local DB-API/CLI profile. Web execution first resolves an
ignored `web_query_adapter_v1`, then uses the Chrome plugin and the user's own session. A missing
adapter produces a manual handoff. Read `references/execution-surfaces.md` before executing or
adapting a website. The `COLLABORATION_SUBMIT` route only creates a local review plan.

## Project layout

```text
sql-projects/<project>/
  manifest.json
  project_config.json
  context/
  rules/
  sources/
  query_workspace/       # local history; do not commit results or credentials
  formal_assets/         # optional shared package layout
```

Use `sql_project.py init` for the advanced layout and `sql_workspace.py bootstrap` for the
minimal immutable SQL workspace. Both are idempotent and preserve existing user files.

## Security boundary

The public repository contains fictional examples only. Never commit production results,
credentials, private endpoints, private table definitions, local absolute paths, or personal
workspaces. The setup flow does not request or store secrets. See `SECURITY.md` and
`tools/public_release.py` before publishing a derived snapshot.

Git is the setup baseline, while GitHub/GitLab/self-hosted/local remotes are configuration. The
planning source is independently configured as Git, SVN, a local folder, or `none`.
