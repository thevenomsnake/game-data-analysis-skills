# Development SQL Inspection

Use this route for bounded, read-only questions against a configured development database: table discovery, field definitions, enum distributions, date coverage, and small diagnostic queries. It is not a replacement for formal QUERY generation or user-supplied production result evidence.

## Contract

- Resolve the stage's confirmed `development_inspection` binding through `data_service.py`; read physical connection policy from the shared service catalog and target values from the stage binding.
- Run `SELECT VERSION()` once before an executable inspection, derive executor capabilities from the returned product/version, and persist that evidence in the local receipt. Do not infer feature support from the configured engine family alone.
- Resolve the time field independently for each physical table by matching its live `DESCRIBE` fields against ordered `time_field_candidates`. Do not fall back to a field absent from that table.
- Read `project_relation` before inspection. `same_project_development_environment` keeps business semantics in the current project while limiting development evidence to schema and bounded sample observations; it does not verify production data.
- Keep host, port, username, database, limits, and password variable name in Git.
- Never store the password in Git, SQL, receipts, command history, or error output.
- Obtain the password from `password_env`; use `--prompt-password` only for an interactive one-off run.
- Do not inherit production `business_scope` predicates. Use only filters explicitly requested for this development inspection.
- Treat the resolved `implicit_business_scope` as environment identity. Do not emit those fields as SQL predicates; normalize an accidentally matching historical predicate into the same catalog subject.
- Permit one read-only statement beginning with `SELECT`, `SHOW`, `DESCRIBE`, `DESC`, or `EXPLAIN`. Permit `WITH` only when detected server capabilities explicitly report `supports_cte=true`; MySQL 5.7 must use derived tables or split diagnostics.
- Block DML, DDL, session changes, file access, delay functions, multi-statements, and unbounded TLOG scans.
- For custom SQL, require both lower and upper bounds for every source table that exposes a configured time candidate. Tables with no candidate remain valid only under the normal aggregate/`LIMIT` bounds.
- Save every executed inspection under ignored `dev_inspections/` as `query.sql`, `result.csv`, and `receipt.json`.
- Write `dev_sql_inspection_receipt_v2` with inspected table/field/date/filter facts, a bounded result preview, project-relative files, fingerprints, and an observation-only reuse contract.
- Refresh `dev_sql_inspection_index_v2` after each execution. Search it before repeating the same inspection; exact duplicates remain evidence but are marked.
- Do not hash, mask, or encode identifiers in SQL. Identifier-value enumeration requires explicit `--allow-identifier-values`; its output remains local.
- Suppress identifier-like enum values from receipt/index previews even when their complete local CSV is explicitly allowed.
- Let deterministic code execute and persist results. The LLM may read the result file and summarize it, but must not reimplement the connection in an ad hoc shell command.
- Treat every result as an observation, not a mapping or business definition. Only an explicit `KNOWLEDGE` request may promote a mutable mapping, and only an explicit `RULES` request may define business meaning.

## First-Time Setup

Run the Skill-owned configuration flow. The tracked project config supplies the target and read-only account; the member selects which item to configure and enters secrets through the masked `*` prompt:

```powershell
$req = "[PROJECT_ADMIN] 开始配置 DEMO_ANALYTICS 本机 SQL 工作环境"
python .\sql-engineering\scripts\local_setup.py `
  --repo-root . --project DEMO_ANALYTICS --function-selection PROJECT_ADMIN `
  --user-request $req configure `
  --section planning_source --section data_services `
  --product EXAMPLE --stage BASE --management-mode user_managed `
  --source-path <svn-working-copy-or-folder> --source-provider auto `
  --prompt-dev-password
```

This calls the same `SELECT VERSION()` detector used by `dev_sql_inspect.py`; setup does not maintain a second connection implementation. Never paste the password into chat or a command argument.

## Search Before Querying

Search the local catalog without connecting to the database or loading a password:

```powershell
python .\sql-engineering\scripts\dev_sql_inspect.py `
  --root .\sql-projects\DEMO_ANALYTICS `
  --function-selection DEV_SQL_INSPECT `
  history --search "TeamChangeType"
```

Use `--table`, `--field`, or `--inspection-command enum` for deterministic filters. The default returns only the latest observation for each logical subject; date-window changes and predicates already implied by the development environment do not create fake parallel subjects. Add `--all-observations` to audit prior executions and duplicates.

Upgrade legacy local evidence without connecting to the database:

```powershell
python .\sql-engineering\scripts\dev_sql_inspect.py `
  --root .\sql-projects\DEMO_ANALYTICS `
  --user-request "迁移现有开发库检查结果到可检索目录" `
  --function-selection DEV_SQL_INSPECT `
  migrate-history --dry-run

python .\sql-engineering\scripts\dev_sql_inspect.py `
  --root .\sql-projects\DEMO_ANALYTICS `
  --user-request "迁移现有开发库检查结果到可检索目录" `
  --function-selection DEV_SQL_INSPECT `
  migrate-history
```

Migration verifies existing SQL/result hashes, rewrites metadata only, and stops on drift. Legacy requests that were never recorded remain explicitly `legacy_unavailable`.

## Common Operations

Discover tables before assuming development-table naming:

```powershell
python .\sql-engineering\scripts\dev_sql_inspect.py `
  --root .\sql-projects\DEMO_ANALYTICS `
  --user-request "查看开发库中的 PlayerLogin 表" `
  tables --like "%playerlogin%"
```

Inspect a table or field enum:

```powershell
python .\sql-engineering\scripts\dev_sql_inspect.py `
  --root .\sql-projects\DEMO_ANALYTICS `
  --user-request "查看某日志 GameMode 枚举" `
  describe --table <table>

python .\sql-engineering\scripts\dev_sql_inspect.py `
  --root .\sql-projects\DEMO_ANALYTICS `
  --user-request "查看某日志 GameMode 枚举" `
  enum --table <table> --field GameMode --start-date 2026-07-15 --end-date 2026-07-15
```

`enum` performs one schema preflight and selects the first field that actually exists on that table. Use `--date-field` only to demand a specific field; an absent explicit field blocks instead of silently changing semantics.

Run custom SQL only after saving it inside the project workspace:

```powershell
python .\sql-engineering\scripts\dev_sql_inspect.py `
  --root .\sql-projects\DEMO_ANALYTICS `
  --user-request "执行开发库只读诊断" `
  query --sql-file .\sql-projects\DEMO_ANALYTICS\_scratch\diagnostic.sql
```

The command returns absolute paths to the local SQL, CSV, and receipt. These outputs are deliberately excluded from Git.

## Observation And Promotion

Keep these ownership boundaries:

- `dev_inspections/`: raw, searchable, local observations with exact execution evidence.
- `KNOWLEDGE`: explicitly reviewed mutable enums/mappings that should be reused across queries.
- `RULES`: explicitly authorized business interpretation or calculation logic.

Database presence proves only that a value was observed in a bounded window. It does not prove completeness, stability, name mapping, or business meaning.
