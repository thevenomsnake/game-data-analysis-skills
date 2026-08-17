# Project Overview

Game Data Analysis Skills has four public layers:

```text
setup
  -> Git remote, planning-source provider, local checkout, and fictional project bootstrap
sql-engineering
  -> rules, schemas, templates, workspace, review, validation, and formal assets
sql-projects
  -> user-owned project configuration and local query history
excel-report-visualizer
  -> optional offline workbook inspection and report presentation source
```

The repository ships capabilities, not business truth. A project supplies its own dialect,
tables, event definitions, mappings, and confirmed rules. The public tree contains no project
result evidence.

## Main modules

- `scripts/sql_workspace.py` is the small compatibility surface for immutable SQL versions,
  metadata, search, and delivery receipts.
- `scripts/sql_project.py` owns the richer project contract: `project_config.json`, Rule Store,
  query workspace, formal asset packages, run evidence, and intermediate tables.
- `scripts/sql_facts.py` extracts deterministic SQL facts reused by review, performance, and
  formalization.
- `scripts/project_validate.py` and related health scripts are read-only checks.
- `scripts/sql_execute.py` is a local read-only adapter; absent configuration means manual
  handoff. An explicitly selected browser route uses only the Chrome plugin and the user's own
  session.

## Lifecycle

```text
request
  -> requirement/context discovery
  -> workspace SQL version
  -> exact receipt
  -> optional read-only execution and result evidence
  -> validation/review
  -> explicit formal asset package or dashboard derivative
```

Execution state, result presentation, and asset value remain separate. A result does not silently
promote a query, and a lifecycle label does not prove correctness.

## Public boundary

`BetterXml`, real project folders, production results, private connection details, and local
absolute paths are intentionally outside this repository. Generic adapters and documentation may
refer to those concepts, but never to a real organization.
