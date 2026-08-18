# Database Execution

The public Skill never assumes one company's knowledge base, network, database, or DA platform.
Storage works without execution configuration. Automatic execution is an optional project layer.
Read `project-onboarding.md` first when the project has not yet registered telemetry, planning
knowledge, human-confirmed material, and canonical rules.
Read `dialects.md` before assuming that a connection protocol determines SQL syntax.

## Separation Of Responsibilities

- `project.json` names environments and maps each one to a connection profile.
- `connections.local.json` contains machine-local connection options and is ignored by Git.
- Environment variables provide passwords, tokens, and other secrets.
- `vNNN.sql` remains the immutable executable asset.
- `sql_execute.py` verifies the saved receipt, runs one read-only statement, and writes local evidence.

This document covers the `direct` execution surface only. Browser execution is a separate,
agent-controlled `QUERY_EXECUTE` surface and is never loaded as a DB-API/CLI profile by
`sql_execute.py`. Read `execution-surfaces.md` for web adapter initialization, Chrome execution,
download binding, and the contract for adapting another website. If neither surface is ready, hand
the exact saved SQL path to the user and request the returned result file.

## Configure Project Environments

Register an environment in the project:

```powershell
python <skill-root>/scripts/sql_workspace.py environment `
  --root <project-root> `
  --name development `
  --dialect starrocks `
  --connection-profile development-starrocks `
  --default
```

The resulting project section is safe to commit because it contains no credentials:

```json
{
  "execution": {
    "default_environment": "development",
    "environments": {
      "development": {
        "dialect": "starrocks",
        "connection_profile": "development-starrocks"
      }
    }
  }
}
```

Different environments may use different profiles. A saved SQL version records its selected environment
and dialect. Execution blocks when the selected environment dialect differs from the saved SQL dialect;
this tool does not translate SQL during execution.

## Configure Local Connections

Copy `assets/examples/connections.example.json` to
`<project-root>/.sql-engineering/connections.local.json` and edit the local copy. It is ignored by Git.
Alternatively set `SQL_ENGINEERING_CONNECTIONS_FILE` to a configuration file outside the repository.
Treat this as trusted local execution configuration: it selects Python modules or executable programs,
so do not use a connection file received from an untrusted project or message.

### DB-API

DB-API is the default choice for direct database connections. Install the driver required by the target
environment, such as `pymysql` for a MySQL-compatible StarRocks endpoint. The Skill does not install it
automatically.

```json
{
  "schema_version": "sql_engineering_connections_v1",
  "profiles": {
    "development-starrocks": {
      "method": "dbapi",
      "module": "pymysql",
      "read_only": true,
      "connect": {
        "host": "127.0.0.1",
        "port": 9030,
        "user": "readonly_user",
        "database": "example",
        "charset": "utf8mb4"
      },
      "secret_env": {
        "password": "SQL_ENGINEERING_DEV_PASSWORD"
      }
    }
  }
}
```

Set the secret only in the local process environment:

```powershell
$env:SQL_ENGINEERING_DEV_PASSWORD = "<secret>"
```

### Database CLI

Use `method=cli` when an environment is accessed through a native client such as Beeline, MySQL,
PostgreSQL, or Trino CLI. The command runs with `shell=False`. Use `{sql_file}` in the argument list when
the client accepts a file; otherwise SQL is supplied on standard input. Configure output as `csv` or `tsv`.
The client must print one header row followed by tabular rows.

Credentials still come from `secret_environment`, which maps a target process variable to a source local
environment variable. Never place passwords in command arguments or committed configuration.

## Execute

```powershell
python <skill-root>/scripts/sql_execute.py run `
  --root <project-root> `
  --sql-file <absolute-saved-vNNN.sql> `
  --environment development `
  --max-rows 100000
```

The environment argument is optional. Resolution order is: explicit argument, environment recorded in
the saved SQL metadata, then project default.

Execution accepts one read-only `SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `DESC`, `EXPLAIN`, or `PRAGMA`
statement. The configured database account must also be read-only. A ready execution writes:

```text
<project-root>/.sql-engineering/runs/<asset>/<run-id>/
  query.sql
  result.csv
  receipt.json
```

These local run files are ignored by Git. The receipt records the exact SQL hash, environment, connection
method, columns, row count, truncation state, and result hash.

## Manual Fallback

`manual_required` is expected when the project has no automatic environment, the local connection file is
absent, a required driver or CLI is not installed, or a secret environment variable is missing. In that
case:

1. Keep the ready SQL asset unchanged.
2. Return its exact `delivery_file` path.
3. Ask the user to execute it in their own database tool.
4. Ask the user to return the result file for the next workflow step.

Do not attempt a browser or DA-console fallback.

## Project Context And Schema Discovery

Optional `context_paths` in `project.json` may point to repository-relative schema, metric, or platform
documentation. Do not depend on undeclared personal files. When context is absent or stale, create and save
a read-only metadata query such as `SHOW`, `DESCRIBE`, or an enum `GROUP BY`, then execute it through the
selected database environment. If automatic execution is unavailable, deliver that SQL for manual execution.
