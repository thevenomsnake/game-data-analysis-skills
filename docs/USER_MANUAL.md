# User Manual

## First run

```powershell
python .\setup\scripts\bootstrap_repo.py configure --root . --planning-provider none
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Use `$sql-engineering` after refreshing Codex. The fictional project is safe to inspect and does
not connect to a database.

During installation, configure the repository Git remote/provider and choose a planning-source
provider: `git`, `svn`, `local`, or `none`. Run `planning-sync` after configuring Git or SVN.

## Small SQL workflow

1. Read the project configuration and search existing query families.
2. Draft SQL in an external working file; do not edit a saved version in place.
3. Save with `sql_workspace.py save` or `sql_project.py save-sql`.
4. Run `receipt` against the exact saved version.
5. Select direct DB-API/CLI, a configured web adapter, or manual handoff; never switch silently.
6. Attach results, visualization, validation, or formalization only as an explicit next step.

## Rules and evidence

Raw source files are evidence. Planning inputs are not automatically confirmed. Canonical rules
require an explicit user-authorized write and a versioned definition. A query may use a rule only
when the project has declared the rule and its source.

## Find and execute project SQL

Search local/history SQL with `sql_query_workspace.py search`. Browse formal SQL through
`sql_repository.py build|serve`; Formal Asset Package manifests are the shared authority.

For execution, the direct surface uses DB-API/CLI profiles from an ignored local connection file.
The web surface uses an ignored `web_query_adapter_v1` plus the Chrome plugin and the user's own
session. Both consume an exact receipt and attach the returned result to that SQL version. Missing
configuration produces `manual_required`. See
[`execution-surfaces.md`](../sql-engineering/references/execution-surfaces.md).
