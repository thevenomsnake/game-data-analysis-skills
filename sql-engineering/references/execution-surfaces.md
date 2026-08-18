# Query Execution Surfaces

Use this reference when a user asks where project SQL is stored, how an existing SQL is found, or
whether a receipted query should run through a direct database connection, a web query product, or
a manual handoff.

## Initialize A Formal Project

Create the project structure without credentials:

```powershell
python .\sql-engineering\scripts\local_setup.py init `
  --repo-root . `
  --project example-game `
  --dialect starrocks `
  --execution-surface manual
```

The project lives at `sql-projects/example-game/`. Initialization creates the governed project
layout and Query Workspace, but does not select an execution surface or store a password.

To initialize the proven web surface in the same step, use
`--execution-surface web`; it copies the Deltaverse example into the ignored local adapter path.
Use `--web-adapter-file <path>` to initialize another validated adapter. For a direct surface, use
`--execution-surface direct`; this records the requested intent but still requires the user to add
the local DB-API/CLI connection profile.

## Find Existing SQL First

Query Workspace is local execution history. Search it by purpose, source log, metric, tag, or
status:

```powershell
python .\sql-engineering\scripts\sql_query_workspace.py search `
  --root .\sql-projects\example-game `
  --query "retention iOS" `
  --format json
```

Its files are under `query_workspace/` and remain Git-ignored. They include temporary, failed,
discarded, current, and promoted versions.

Formal SQL is stored only in Formal Asset Packages under `formal_assets/`. Build or serve the
read-only repository view instead of scanning filenames:

```powershell
python .\sql-engineering\scripts\sql_repository.py build `
  --root .\sql-projects\example-game

python .\sql-engineering\scripts\sql_repository.py serve `
  --root .\sql-projects\example-game
```

Before any execution, run `sql_query_workspace.py receipt` against the exact indexed SQL path or
query ID. Neither a chat code block nor a Formal Asset title is execution authority.

## Choose One Surface Explicitly

| Surface | Use when | Executor | Missing configuration |
|---|---|---|---|
| `direct` | A read-only database endpoint and driver/CLI are available | `sql_execute.py run` | `manual_required` or `credential_required` |
| `web` | The team queries through a browser product and the user has selected that route | Chrome plugin + local web adapter | `manual_required` |
| `manual` | No trusted automatic adapter is ready | The user runs the exact SQL and returns a result file | Expected handoff |

When both direct and web surfaces are available, use the project/user-selected surface. Do not
silently fall back from one to the other after an error. SQL dialect and connection transport are
separate facts.

## Direct Database Execution

Use [`database-execution.md`](database-execution.md) to configure a DB-API module or native CLI in
the ignored `.sql-engineering/connections.local.json`. Passwords remain environment variables.

```powershell
python .\sql-engineering\scripts\sql_execute.py run `
  --root .\sql-projects\example-game `
  --sql-file <absolute-receipted-vNNN.sql> `
  --environment development
```

`sql_execute.py` accepts one read-only statement, writes result evidence locally, and records the
SQL/result fingerprints. It never drives a browser.

## Web Query Initialization

The public repository includes a Deltaverse example because `https://da.deltaverse.cn/` is the
currently proven web surface. Copy it into the project-local ignored configuration before use:

```powershell
New-Item -ItemType Directory -Force `
  .\sql-projects\example-game\.sql-engineering | Out-Null

Copy-Item `
  .\sql-engineering\assets\examples\web-query-adapter.deltaverse.json `
  .\sql-projects\example-game\.sql-engineering\web-query-adapter.local.json

python .\sql-engineering\scripts\web_query_adapter.py validate `
  --adapter-file .\sql-projects\example-game\.sql-engineering\web-query-adapter.local.json

python .\sql-engineering\scripts\web_query_adapter.py resolve `
  --project-root .\sql-projects\example-game
```

The adapter contains URLs, stable UI locators, completion signals, result-download routing, and
tab policy. It contains no password, cookie, token, or account identity. Authentication always
uses the user's existing Chrome session. Confirm the example locators against the current page
before the first real query because external UI contracts can change independently of this Skill.

## Normal Web Execution

1. Produce a ready receipt for the exact saved SQL file.
2. Resolve the local adapter with `web_query_adapter.py resolve`; stop on `manual_required` or
   `blocked`.
3. Use the Chrome plugin only. Open the configured root URL and hand authentication to the user.
4. Keep at most one agent-created query tab. Fill the exact receipt file, then submit once.
5. Wait for a configured success/failure signal. Timeout or lost state never permits resubmission.
6. Follow the adapter's result rule. Deltaverse uses the inline download at or below 2,000 rows
   and the separate `task_id` export flow for larger results.
7. Move the downloaded file into the exact query version's `outputs/vNNN/` directory.
8. Attach it with `sql_query_workspace.py attach-output`, then mark the exact version
   `result_confirmed` or `run_failed` with observed evidence.
9. Close agent-created query/export tabs before visualization or another query. Never close a
   user-owned pre-existing tab.

The web adapter describes a UI contract; it does not bypass site permissions, automate login, or
prove business correctness.

## Website Adapter Guide

Use this checklist when adapting another website:

1. Read this file, `schemas/web_query_adapter.json`,
   `scripts/web_query_adapter.py`, and the Deltaverse example.
2. Use the Chrome plugin to inspect the target site's user-visible flow. Do not use the built-in
   browser, inspect cookies, or automate authentication.
3. Copy the example to the project's ignored `web-query-adapter.local.json`; change only the
   allowed hosts, entry URLs, locators, completion signals, row threshold, and export controls.
4. Prefer role, label, or stable visible text locators. Use CSS only when the site exposes no
   stable accessible locator. Never use generated class names when a stable alternative exists.
5. Run `web_query_adapter.py validate` and `resolve`. A URL with credentials, an unlisted host,
   missing completion signal, reusable query tab, or multi-submit policy must fail.
6. With explicit user authorization, smoke one harmless read-only query. Verify one submit,
   terminal-state detection, correct small/large result routing, download completion, local
   attachment, and tab cleanup.
7. If the site cannot fit `web_query_adapter_v1`, update the schema, validator, focused tests, and
   this reference together. Keep site-specific selectors in an adapter file, not in core SQL or
   result-lineage scripts.
