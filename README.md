# Game Data Analysis Skills

**A file-backed SQL lifecycle with configurable read-only database execution for Codex.**

Game Data Analysis Skills turns SQL conversations into durable project files. Every generated
or modified query is saved, versioned, indexed, searchable, and delivered by exact path, so the
work remains understandable after the chat ends.

[简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [Español](README.es.md) · [한국어](README.ko.md)

> A SQL code block is an explanation. A verified `vNNN.sql` file is the deliverable.

## What Problem It Solves

SQL created in chat is easy to lose. A useful query is often copied into another file, edited
without history, or separated from the explanation of what it does. Later, nobody knows which
version produced a result or whether an external source file was overwritten.

This Skill gives Codex a small, enforceable workspace contract:

| Capability | What happens |
|---|---|
| Repository bootstrap | Creates a stable `sql-projects/` layout and the first project |
| Project context governance | Versions original telemetry, planning inputs, human-confirmed material, and canonical rules separately |
| SQL delivery | Saves every generated or modified query as an immutable `vNNN.sql` version |
| Environment-aware execution | Runs saved SQL through a configured read-only DB-API driver or database CLI |
| External SQL intake | Treats the supplied file as input and works on a project-local copy |
| Searchable history | Records a human title, purpose, tags, dialect, path, and content hash |
| Revision control | Keeps corrections and extensions in one query family without overwriting history |
| Exact receipt | Verifies the saved file, metadata, index entry, and current content hash before delivery |
| Lifecycle labels | Separates temporary, retained, and dashboard-oriented SQL |

The public specification edition deliberately contains no company schemas, production table
names, credentials, private business rules, query results, or internal execution integrations.

## What You Provide For A Project

| Required context | What the Skill does with it |
|---|---|
| Original telemetry definition | Copies the XML, JSON, YAML, Excel, CSV, text, or other source unchanged into `sources/raw/` and records a versioned hash |
| Database and SQL dialect | Records named environments and the dialect used to generate SQL; local DB-API or CLI connection details stay outside Git |
| Planning/configuration tables | Preserves original design-owned mappings and IDs under `knowledge/planning/`; they are evidence, not automatic rules |
| Human-confirmed material | Stores the reviewed version, confirmer, reason, and lineage under `knowledge/confirmed/` |
| Canonical metric rules | Saves explicitly confirmed Base, grain, calculation, filters, and cited source/knowledge IDs as immutable versions under `rules/definitions/` |

The Skill cannot supply these project facts for you. It provides the structure that keeps their
ownership and changes visible. See the [project onboarding guide](sql-engineering/references/project-onboarding.md)
for the complete sequence.

## Set Up A Project

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

`bootstrap` initializes `sql-projects/example`, including empty source, knowledge, rule, and SQL
catalogs. Running it again repairs missing empty structure without clearing registered content.

### 3. Register Project Context

Give Codex the raw telemetry files, planning/configuration tables, and any separately confirmed
materials. Register them before fixing rules. Then declare the database environment and SQL dialect,
fix only rules a person has explicitly confirmed, and run:

```powershell
python .\sql-engineering\scripts\sql_workspace.py status `
  --root .\sql-projects\example
```

`query_context_ready=false` means the project still has no registered raw telemetry definition.
No automatic database connection is acceptable; that project uses manual SQL handoff.

### 4. Ask Codex Naturally

```text
$sql-engineering Create a StarRocks query that counts distinct login users by day.
Use a fixed date range in a params CTE, save it in the example project, and return the exact file.
```

Codex should inspect the project, create or reuse a query family, save a version such as
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql`, run a receipt, and
return the absolute saved path. Database execution is reported separately and is never assumed.

Automatic execution is optional. Register a named project environment and keep its real connection
profile in the ignored `.sql-engineering/connections.local.json` file. When no driver, CLI, secret,
or connection profile is available, the Skill returns `manual_required`, gives the exact SQL path,
and asks the user to run it and return the result. It never clicks a browser or DA web console.

## Common Requests

| Goal | Example request |
|---|---|
| Create a project | `$sql-engineering Create project alpha for StarRocks and tell me which source, knowledge, rule, and connection inputs are still missing.` |
| Register telemetry | `$sql-engineering Register this event XML as the original PlayerLogin source definition.` |
| Register planning evidence | `$sql-engineering Store this mode configuration workbook as a planning input; do not treat it as a confirmed rule.` |
| Fix a rule | `$sql-engineering Fix this human-confirmed daily-active-user definition as a new canonical rule version.` |
| Create SQL | `$sql-engineering Create a daily active-user query for this project and save it.` |
| Modify external SQL | `$sql-engineering Import this SQL, fix it for the project dialect, and do not overwrite the original.` |
| Find prior work | `$sql-engineering Find saved queries related to retention and summarize their purpose.` |
| Revise a query | `$sql-engineering Add platform as a dimension to the existing active-user query family.` |
| Keep a useful query | `$sql-engineering Save the confirmed logic as a retained query version.` |
| Verify delivery | `$sql-engineering Check the receipt for this v003.sql and return the exact path.` |
| Execute directly | `$sql-engineering Run this saved query in the configured development database.` |

## How The Lifecycle Works

```text
request
  -> original telemetry registered
  -> planning and confirmed knowledge separated
  -> applicable canonical rules fixed
  -> project environment and dialect selected
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
    sources/
      source-catalog.json
      raw/<source>/vNNN.*      unchanged telemetry definitions
    knowledge/
      planning/<item>/vNNN.*   original planning/configuration inputs
      confirmed/<item>/vNNN.* human-confirmed material
    rules/
      definitions/<rule>/vNNN.json
    context/                    non-authoritative notes and manuals
    sql-workspace/
      index.json               searchable machine index
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

The underscore directories are stable cross-project extension points. Project directories separate
raw evidence, human confirmation, canonical rules, and executable SQL so one cannot silently replace another.

## Command Reference

| Command | Purpose |
|---|---|
| `bootstrap` | Create the repository layout and optionally initialize the first project |
| `init` | Initialize one standalone project |
| `environment` | Map a named project environment to a local database connection profile |
| `source` | Copy and register an original telemetry definition without changing its format |
| `knowledge` | Register a planning input or separately human-confirmed material |
| `rule` | Fix an explicitly confirmed canonical rule as a new immutable version |
| `status` | Report missing source, knowledge, rule, and execution setup |
| `save` | Save a new immutable SQL version and update its index |
| `search` | Search titles, summaries, and tags |
| `receipt` | Verify one exact saved SQL version before delivery |
| `sql_execute.py run` | Execute a saved read-only query or return a manual handoff |

Try the bundled fictional query at
[`sql-engineering/assets/examples/daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql).
The [worked agent example](sql-engineering/references/example.md) shows the request, commands,
expected files, and final-response contract.

## Design Boundaries

- The project configuration selects the dialect. The Skill does not guess tables, partitions,
  business IDs, or metric definitions.
- Project context is optional and explicitly declared. The Skill does not depend on a personal
  knowledge base; missing schema context can be inspected through saved read-only database queries.
- Automatic execution uses DB-API or database command-line clients only. Browser and DA-console
  automation are intentionally unsupported, and missing configuration falls back to manual execution.
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
| New project inputs and setup flow | [`references/project-onboarding.md`](sql-engineering/references/project-onboarding.md) |
| Complete worked example | [`references/example.md`](sql-engineering/references/example.md) |
| Project and directory contract | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| Query-family lifecycle | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL delivery checks | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| Database environments and execution | [`references/database-execution.md`](sql-engineering/references/database-execution.md) |
| Connection methods and SQL dialects | [`references/dialects.md`](sql-engineering/references/dialects.md) |
| Contribution rules | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security policy | [SECURITY.md](SECURITY.md) |

## Development

The public core uses only the Python standard library. DB-API execution imports the database driver
selected by the user's local connection profile.

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py .\sql-engineering\scripts\sql_execute.py
```

Licensed under the [Apache License 2.0](LICENSE).
