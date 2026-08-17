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
5. Execute only through a configured read-only adapter, or hand the exact path to the user.
6. Attach results, visualization, validation, or formalization only as an explicit next step.

## Rules and evidence

Raw source files are evidence. Planning inputs are not automatically confirmed. Canonical rules
require an explicit user-authorized write and a versioned definition. A query may use a rule only
when the project has declared the rule and its source.

## Local execution

The compatibility executor supports DB-API and CLI profiles from an ignored local connection
file. It accepts one read-only statement and records the SQL/result hashes. No browser or web
console is used by the public edition.
