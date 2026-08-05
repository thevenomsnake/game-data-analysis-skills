# Database Connections And SQL Dialects

A connection method answers how SQL reaches a database. A dialect answers which SQL syntax is generated.
They are separate project decisions.

| Example environment | Typical connection | Project dialect |
|---|---|---|
| StarRocks | MySQL-compatible DB-API driver such as `pymysql`, or MySQL CLI | `starrocks` |
| MySQL | MySQL DB-API driver or MySQL CLI | `mysql` |
| Hive | PyHive/other DB-API integration, Beeline, or Hive CLI | `hive` |
| PostgreSQL | `psycopg` or `psql` | `postgresql` |
| Trino | Trino DB-API driver or Trino CLI | `trino` |
| SQLite | Python `sqlite3` | `sqlite` |

These are examples, not bundled dependencies. The user supplies the driver or database client available in
their environment.

## Selection Rules

1. Set the generation dialect when creating the project.
2. Register named execution environments with their dialect and local connection-profile name.
3. Save the selected environment and dialect with every SQL version.
4. Block execution when the saved SQL dialect differs from the selected environment dialect.
5. Never infer SQL syntax from a port, Python module, CLI program, table prefix, or browser platform.
6. Put engine-version quirks and organization-specific syntax in a project-relative document under
   `context/`, then list it in `context_paths`.

One business question may require separate saved SQL versions for genuinely different dialects. The execution
layer does not rewrite StarRocks SQL into Hive SQL or vice versa.
