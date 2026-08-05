# Game Data Analysis Skills

**A file-backed SQL lifecycle for Codex.**

Game Data Analysis Skills turns SQL conversations into durable project files. Every generated
or modified query is saved, versioned, indexed, searchable, and delivered by exact path, so the
work remains understandable after the chat ends.

[简体中文](README.zh-CN.md)

> A SQL code block is an explanation. A verified `vNNN.sql` file is the deliverable.

## What Problem It Solves

SQL created in chat is easy to lose. A useful query is often copied into another file, edited
without history, or separated from the explanation of what it does. Later, nobody knows which
version produced a result or whether an external source file was overwritten.

This Skill gives Codex a small, enforceable workspace contract:

| Capability | What happens |
|---|---|
| Repository bootstrap | Creates a stable `sql-projects/` layout and the first project |
| SQL delivery | Saves every generated or modified query as an immutable `vNNN.sql` version |
| External SQL intake | Treats the supplied file as input and works on a project-local copy |
| Searchable history | Records a human title, purpose, tags, dialect, path, and content hash |
| Revision control | Keeps corrections and extensions in one query family without overwriting history |
| Exact receipt | Verifies the saved file, metadata, index entry, and current content hash before delivery |
| Lifecycle labels | Separates temporary, retained, and dashboard-oriented SQL |

The public specification edition deliberately contains no company schemas, production table
names, credentials, private business rules, query results, or internal execution integrations.

## Start In Three Minutes

### 1. Install The Skill

Clone this repository, then copy or link `sql-engineering/` into the Codex skills directory:

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Restart or refresh Codex. The Skill can now be invoked as `$sql-engineering`.

### 2. Initialize A Workspace

```powershell
python .\sql-engineering\scripts\sql_workspace.py bootstrap `
  --root . `
  --project-id example `
  --dialect starrocks
```

The repository already includes the shared `_asset_catalog`, `_review_inbox`, and `_rule_review`
directory skeleton. `bootstrap` repairs missing directories and initializes
`sql-projects/example`; running it again does not clear existing content.

### 3. Ask Codex Naturally

```text
$sql-engineering Create a StarRocks query that counts distinct login users by day.
Use a fixed date range in a params CTE, save it in the example project, and return the exact file.
```

Codex should inspect the project, create or reuse a query family, save a version such as
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql`, run a receipt, and
return the absolute saved path. Database execution is reported separately and is never assumed.

## Common Requests

| Goal | Example request |
|---|---|
| Create SQL | `$sql-engineering Create a daily active-user query for this project and save it.` |
| Modify external SQL | `$sql-engineering Import this SQL, fix it for the project dialect, and do not overwrite the original.` |
| Find prior work | `$sql-engineering Find saved queries related to retention and summarize their purpose.` |
| Revise a query | `$sql-engineering Add platform as a dimension to the existing active-user query family.` |
| Keep a useful query | `$sql-engineering Save the confirmed logic as a retained query version.` |
| Verify delivery | `$sql-engineering Check the receipt for this v003.sql and return the exact path.` |

## How The Lifecycle Works

```text
request
  -> project and dialect context
  -> saved temporary SQL version
  -> execution in the user's environment
  -> correction or extension as the next version
  -> optional retained or dashboard-oriented version
  -> exact delivery receipt
```

A query family represents one analytical question. Date refreshes, syntax corrections, and
fully containing extensions stay in that family as new versions. A different Base, primary
metric, or decision starts a new family.

## Workspace Layout

```text
sql-projects/
  _asset_catalog/              reserved cross-project discovery output
  _review_inbox/               external SQL and evidence awaiting intake
  _rule_review/                reserved rule-review output
  example/
    .sql-engineering/
      project.json             project identity and dialect
    sql-workspace/
      index.json               searchable machine index
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

The underscore directories are stable extension points. The public core creates them but does
not invent catalog, review, or rule content.

## Command Reference

| Command | Purpose |
|---|---|
| `bootstrap` | Create the repository layout and optionally initialize the first project |
| `init` | Initialize one standalone project |
| `save` | Save a new immutable SQL version and update its index |
| `search` | Search titles, summaries, and tags |
| `receipt` | Verify one exact saved SQL version before delivery |

Try the bundled fictional query at
[`sql-engineering/assets/examples/daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql).
The [worked agent example](sql-engineering/references/example.md) shows the request, commands,
expected files, and final-response contract.

## Design Boundaries

- The project configuration selects the dialect. The Skill does not guess tables, partitions,
  business IDs, or metric definitions.
- External SQL remains immutable input; revisions are saved inside the project.
- Saved versions are not overwritten. Manual edits are detected by the receipt hash checks.
- A lifecycle label describes intended use; it does not prove business correctness or execution.
- Results, visualizations, validations, and dashboards may be added by governed extensions, but
  they are not silently inferred from a SQL file.
- Credentials, private schemas, production results, and local absolute paths must not be committed.

## Documentation

| Topic | Document |
|---|---|
| Agent workflow and hard boundaries | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| Complete worked example | [`references/example.md`](sql-engineering/references/example.md) |
| Project and directory contract | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| Query-family lifecycle | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL delivery checks | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| Contribution rules | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security policy | [SECURITY.md](SECURITY.md) |

## Development

The public edition uses only the Python standard library.

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py
```

Licensed under the [Apache License 2.0](LICENSE).
