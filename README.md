# Game Data Analysis Skills

This repository contains a small, file-backed Codex SQL Engineering Skill for work that must remain runnable, searchable, and
traceable after the conversation ends.

[简体中文](README.zh-CN.md)

## Why

Chat-generated SQL is easy to lose, hard to identify, and often edited without a reliable link
to the version that produced a result. This public edition establishes a simple contract:

- every generated or modified query is saved as an immutable `vNNN.sql` file;
- every version has a concise title, summary, content hash, and index entry;
- external SQL is copied into the project workspace before modification;
- a query is delivered only when an exact receipt verifies its file and hash;
- temporary, retained, and dashboard SQL remain distinct lifecycle states.

It intentionally ships without company schemas, production table names, business rules,
credentials, results, or internal execution integrations.

## Install

Clone the repository, then copy or link `sql-engineering/` into your Codex skills directory:

```powershell
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Restart or refresh Codex, then invoke `$sql-engineering` in a task.

## Initialize The Workspace

```powershell
python .\sql-engineering\scripts\sql_workspace.py bootstrap `
  --root . `
  --project-id example `
  --dialect starrocks
```

This creates `sql-projects/`, the shared `_asset_catalog`, `_review_inbox`, and `_rule_review`
directories, and the first project at `sql-projects/example`. Running it again is safe.

Save the bundled example query:

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "Counts distinct active users by date." `
  --kind temporary
```

The command creates an immutable SQL version, a sidecar metadata file, and a searchable index
entry. It returns the absolute saved SQL path and a content hash. Run `receipt` on that saved
path before delivery.

The example uses fictional `demo.events` fields, so adapt the source contract before database
execution. See the [worked agent example](sql-engineering/references/example.md) for the exact
request-to-file workflow and expected final response.

## Development

The public edition uses only the Python standard library.

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py
```

## Security

Do not commit credentials, production SQL results, private table definitions, or local absolute
paths. See [SECURITY.md](SECURITY.md) for reporting and handling guidance.

## License

Licensed under the [Apache License 2.0](LICENSE).
