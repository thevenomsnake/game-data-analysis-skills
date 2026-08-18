# Integration Interfaces

The public edition exposes two supported entry points. Choose one deliberately; they share the
same file contracts and execution boundaries but have different callers.

## 1. Codex Skill Interface

Use this path when the operator is working inside Codex.

```powershell
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Refresh Codex and invoke `$sql-engineering`. The Skill routes the request through Setup, PROJECT_ADMIN,
QUERY, QUERY_EXECUTE, REVIEW, VALIDATION, RESULT_VISUALIZATION, or SQL_FORMALIZE as appropriate.
It reads the project references and asks for the user's decisions where a write, credential, browser
session, or business confirmation is required.

The normal Codex path is:

```text
initialize project -> discover sources/rules -> save immutable SQL -> receipt
-> select direct/web/manual surface -> attach result -> review/validate -> optional formalize
```

Codex-specific behavior includes selecting the capability route, using the user's active Chrome
session for a web surface, and returning human-readable handoffs alongside JSON receipts.

## 2. External Agent / Third-Party Software Interface

Use this path when another AI agent, service, desktop tool, or custom orchestrator owns the call.
No Codex runtime is required. Clone the repository, invoke the scripts as subprocesses, and parse
their JSON output with `--format json`.

### Command interface

| Need | Command | Contract |
|---|---|---|
| Initialize or inspect a project | `local_setup.py status|init` | `public_local_setup_v1` |
| Find local SQL | `sql_query_workspace.py search` | `query_workspace_index_v2` rows |
| Verify one exact SQL version | `sql_query_workspace.py receipt` | `query_delivery_receipt_v1` |
| Run direct SQL | `sql_execute.py run` | read-only DB-API/CLI; result receipt |
| Resolve web execution | `web_query_adapter.py resolve` | `web_query_adapter_resolution_v1` |
| Attach and mark a result | `sql_query_workspace.py attach-output|mark` | exact SQL/result lineage |
| Browse formal assets | `sql_repository.py build|serve` | Formal Asset Package read model |

Every asset-changing command receives the verbatim request and explicit function selection when its
CLI exposes those options. Exit status and JSON `status` are both part of the interface. A caller
must stop on `blocked`, `manual_required`, a non-ready receipt, or a hash/path mismatch.

### File interface

An external consumer may read the files directly, without importing Python modules:

- `sql-projects/<project>/query_workspace/index.json` and each version's `.meta.json` for local SQL;
- `sql-projects/<project>/formal_assets/*/manifest.json` and receipts for formal packages;
- `sql-engineering/schemas/*.json` for machine validation;
- Provider `manifest`/`snapshot`/Catalog files for pinned, read-only cross-project consumption.

Use repository-relative paths and the declared SHA-256. Do not infer lineage from filenames, titles,
directory order, or SQL similarity. Do not treat HTML as a machine API.

### Execution surfaces

The external caller selects exactly one surface:

- `direct`: provide a project-local ignored DB-API/CLI profile and call `sql_execute.py run`;
- `web`: resolve `web_query_adapter_v1`, use the caller's browser integration and user session, then
  attach the downloaded result;
- `manual`: return the exact SQL path and wait for a user-supplied result.

The repository does not provide a generic browser automation server. A third-party agent that can
control Chrome may implement the web steps from `execution-surfaces.md`; an agent without browser
control should use direct or manual execution.

### Adapter implementation boundary

To support another web site, copy the local adapter example and follow
[`execution-surfaces.md`](../sql-engineering/references/execution-surfaces.md). Keep site locators
and URLs in the ignored adapter file. The adapter must be read-only, user-session based, single-submit,
terminal-state aware, and explicit about small/large result download routing. Add a focused validator
test when the schema changes. Never place cookies, tokens, passwords, internal endpoints, or result
files in the public repository.

This interface split lets an external AI use the same receipts and asset lineage as Codex without
pretending that it has Codex's skill router or browser tools.
