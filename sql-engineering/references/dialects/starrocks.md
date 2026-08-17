# StarRocks Strict Dialect

Use this profile only when `project_config.json.sql_dialect = StarRocks`.

## Entry Requirements

- Load `project_config.json` before generating SQL.
- Use `scripts/sql_project.py resolve-table --root <project-root> <LogName>` for TLOG table names unless the user gives a physical table.
- Block formal SQL if `query_environment`, `query_engine`, `table_naming_profile`, schema confirmation, or partition policy is missing.
- Do not reuse Hive-only SQL mechanically. Convert functions and date handling to StarRocks-safe syntax before output.

## Required TLOG Filters

StarRocks TLOG SQL must use the project-confirmed StarRocks event-time field for time filtering. For DEMO-EXPERIMENT `demo_starrocks` tables, the default confirmed field is:

```sql
dteventdate
```

Use direct timestamp bounds, preferably from a top `params AS (...)` CTE:

```sql
WITH
params AS (
    SELECT
        '2026-02-03 00:00:00' AS ts_start,
        '2026-02-28 23:59:59' AS ts_end,
        10001 AS zone_id
)
SELECT ...
FROM demo_log.demo_dsl_playerquestion_fht0 AS e
JOIN params AS p ON 1 = 1
WHERE e.dteventdate >= p.ts_start
  AND e.dteventdate <= p.ts_end
  AND e.iZoneAreaID = p.zone_id
```

`tdbank_imp_date` is a TDBank import partition convention. Do not add `tdbank_imp_date` to StarRocks SQL unless the user provides schema evidence that the specific StarRocks physical table really has that column.

If a StarRocks physical table uses a different event-time or partition field, stop and update `project_config.json.partition_policy` before generating formal SQL. If field type or format is unknown, ask for schema confirmation instead of guessing casts.

Example StarRocks table and time usage:

```sql
FROM demo_log.demo_dsl_playerquestion_fht0 AS e
JOIN params AS p ON 1 = 1
WHERE e.dteventdate >= p.ts_start
  AND e.dteventdate <= p.ts_end
```

For retained QUERY artifacts, `params` must be the first executable CTE. Rewrite
received SQL before saving when date/time or iZoneAreaID/GameSvrId filter values
are hard-coded in WHERE clauses.

## Allowed Core Syntax

- CTEs with `WITH`.
- CTEs remain subject to the combined expansion guardrail. There is no universal CTE-count limit: many shallow CTEs may pass, while a large set plus a deep dependency chain and heavy JOIN/final-reference pressure must be compressed or split before delivery. Hive may be used only after explicit user selection.
- `CASE WHEN`.
- `CAST`.
- `group_concat(CAST(<expr> AS string/varchar))` or `group_concat(concat(...))` for string samples, ID samples, and TopN/detail stitching.
- Window functions such as `row_number() OVER (...)`.
- `date_format`, `str_to_date`, `date_add`, `date_sub` only when the input type is known.

## Forbidden In Formal SQL

- Hive-only functions such as `unix_timestamp` and Hive-specific `from_unixtime` usage unless the project explicitly confirms an equivalent.
- Hive-native string sample aggregation such as `concat_ws(',', collect_list(<varchar_expr>))`, `collect_list`, or `collect_set`; use `group_concat(...)` instead.
- Hive-only assumptions about string date fields when StarRocks column types are unknown.
- TDBank import partition assumptions such as `tdbank_imp_date` unless the StarRocks table schema explicitly confirms the field.
- `SELECT *`.
- Missing project-confirmed event-time filtering on any TLOG source.
- SQL-side MD5/SHA/HASH/BASE64/AES/MASK de-identification. Keep only business-required identifiers unchanged; DA owns privacy handling.
- Producing a formal retained SQL artifact while schema, event-time field, or optional partition field is unconfirmed.
- Delivering a query that fails the shared StarRocks CTE expansion assessment.

## CTE Scope Failure Diagnosis

When StarRocks/DA reports `Unknown table '<db>.<name>'`, first compare `<name>` with the SQL's declared top-level CTEs. A match means probable `cte_scope_or_expansion_failure`, not proof that a physical table is absent. Run `performance_preflight.py --sql-file <path> --execution-error "<message>"`; then shorten the dependency chain, remove pass-through CTEs, inline a small terminal aggregate, or split the query. Use the Hive profile only when the user explicitly requests Hive.

## Table Profiles

- `demo_starrocks`: `demo_log.demo_dsl_{log_lower}_fht0`.

For BASE dual execution, this is the fast profile. The project prefix remains
`demo`; only the database/time adapter changes when routing to Hive.

Project overrides win over profile patterns.
