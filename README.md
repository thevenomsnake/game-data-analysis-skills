# SQL Engineering Skill

A small, file-backed Codex skill for SQL work that must remain runnable, searchable, and
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

## Initialize A Project

```powershell
python .\sql-engineering\scripts\sql_workspace.py init `
  --root .\example-project `
  --project-id example `
  --dialect starrocks
```

Save a query:

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\example-project `
  --sql-file .\query.sql `
  --title "Daily active users" `
  --summary "Counts distinct active users by date." `
  --kind temporary
```

The command returns the absolute saved SQL path and a content hash. See
[`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) for the agent workflow.

## Public And Internal Editions

This repository is a clean public-ready edition with its own version and release history. Its
hosting visibility may remain private; the source still follows public-content rules. It is not
a mirror of any private analytics workspace. Private projects may build additional rule stores,
result evidence, review systems, data catalogs, and execution adapters on top of the public
file-backed contract without publishing those assets here.

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
