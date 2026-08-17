# Hive Strict Dialect

Use this profile only when the effective project or selected execution profile has `sql_dialect = Hive`.

Hive is the SQL dialect. TDBank is only one possible Hive-backed query engine/profile.
Do not infer TDBank import partitions from the Hive dialect alone.

## Entry Requirements

- Load `project_config.json` before generating SQL.
- Use `scripts/sql_project.py resolve-table --root <project-root> <LogName>` for TLOG table names unless the user gives a physical table.
- Block formal SQL if `query_environment`, `query_engine`, or `table_naming_profile` is missing.
- Use the project table naming profile, not a global default.

## Required TLOG Filters

Every TLOG source must satisfy `project_config.json.partition_policy`.

When `partition_policy.required_for_tlog = true`, include the configured partition
field with lower and upper bounds. For the AB_TEST `tdbank_hourly` policy this is:

```sql
tdbank_imp_date >= p.pt_start
AND tdbank_imp_date <= p.pt_end
```

When `partition_policy.required_for_tlog = false`, do not invent a partition
field such as `tdbank_imp_date`; filter by the configured business event-time field.

When `business_time_required=true`, also filter the configured business time.
When `business_time_required_when=detailed_time_logic`, add this filter only for
an explicit partial-day/timestamp boundary, using the configured bound style:

```sql
`dtEventTime` >= p.ts_start
AND `dtEventTime` <= p.ts_end
```

Do not wrap configured partition or business time fields in functions inside `WHERE`.

## Exact-Case Identifiers

Read `identifier_policy` from the effective project or execution profile. A
field listed in `case_sensitive_fields` must keep that exact spelling and use
backticks in every SELECT, WHERE, JOIN, CASE, window, and ORDER BY reference.
For the current TDBank chains this includes:

```sql
t.`dtEventTime`
```

Do not emit bare `dtEventTime` or lowercase `dteventtime`. The execution layer
may normalize an unquoted name to lowercase before resolving the case-sensitive
TLOG schema. Portable SQL is quoted by the adapter; fixed Hive SQL is blocked
by preflight when the required quote or exact casing is missing.

## Safe Params CTE Aliases

For directly runnable Hive QUERY SQL, put literal values in a `params AS (...)` CTE and use parser-safe aliases:

```sql
WITH
params AS (
    SELECT
        '2026051400' AS pt_start,
        '2026053023' AS pt_end,
        20001 AS zone_id
)
```

When the project uses `demo_log_dt_event_date`, keep `pt_start` and `pt_end` as
the actual `dtEventDate` lower/upper dates and compare them with `>=` / `<=`.
For a complete natural-day query, these are the only time params and filters;
internal `dtEventTime` ordering does not require a `dtEventTime` WHERE range.
Add `ts_start`/`ts_end` only for a real detailed-time window, also using
inclusive bounds. For true `event_time_only` projects,
omit `pt_start` and `pt_end` unless the project config supplies a real partition
field. Do not use `start_partition`, `end_partition`, `partition`, or `end` as
executable CTE aliases in Hive SQL; some Hive-compatible parsers reject these
names even when the logic is otherwise valid. Descriptive names may still appear
in prose, but executable SQL should use the safe aliases above.

For retained QUERY artifacts, `params` must be the first executable CTE. Rewrite
received SQL before saving when date/time or iZoneAreaID/GameSvrId filter values
are hard-coded in WHERE clauses.

## Allowed Core Syntax

- CTEs with `WITH`.
- `CASE WHEN`.
- `substr`, `concat`, `date_add`, `date_sub`, `datediff`.
- `unix_timestamp`, `from_unixtime`.
- `row_number() OVER (PARTITION BY ... ORDER BY ...)`.
- `CAST(x AS string|bigint|int|double)`.


## Execution-Chain Compatibility

Hive project SQL may be executed through DA platforms, PyMySQL error layers, or StarRocks/MySQL-style analyzers even when `sql_dialect = Hive`. Therefore Hive SQL must avoid syntax that Hive accepts loosely but compatibility analyzers reject.

Hard rule: in the same `SELECT` query block, do not combine `SELECT DISTINCT` with `GROUP BY`. For deduplication, use `GROUP BY` only.

Hard rule for non-native Hive execution paths: when SQL may pass through DA,
PyMySQL, StarRocks/MySQL-compatible analyzers, or a StarRocks executor, do not
use Hive-native string sample aggregation:

```sql
concat_ws(',', collect_list(<varchar_expr>))
```

StarRocks may reject this with `No matching function with signature:
collect_list(varchar)`. For string samples, ID samples, or TopN/detail
string stitching, use:

```sql
group_concat(CAST(<expr> AS string))
-- or
group_concat(concat(...))
```

Only use the Hive-native form below when the target execution path is explicitly
confirmed as native Hive:

```sql
concat_ws(',', collect_list(CAST(<expr> AS string)))
```

Recommended:

```sql
dedup_user AS (
    SELECT
        zone_id,
        open_id
    FROM source_cte
    GROUP BY
        zone_id,
        open_id
)
```

Forbidden:

```sql
dedup_user AS (
    SELECT DISTINCT
        zone_id,
        open_id
    FROM source_cte
    GROUP BY
        zone_id,
        open_id
)
```

## Forbidden In Formal SQL

- `SELECT *`.
- Same-block `SELECT DISTINCT` with `GROUP BY`; use `GROUP BY`-only deduplication.
- `collect_list` / `collect_set` string aggregation on DA/PyMySQL/StarRocks-compatible execution paths; use `group_concat(...)`.
- `QUALIFY`.
- StarRocks-only functions or syntax.
- Missing configured partition filter when `partition_policy.required_for_tlog = true`.
- Missing configured business-time bounds when `business_time_required = true`.
- Unquoted or case-mismatched fields declared by `identifier_policy`, including bare `dtEventTime` on the configured TDBank chains.
- Whole-day `demo_log_dt_event_date` SQL that uses next-day-exclusive bounds,
  timestamp-valued `pt_start`/`pt_end`, or redundant `dtEventTime` WHERE bounds.
- Parser-sensitive params CTE aliases such as `start_partition` or `end_partition` in executable SQL.
- SQL-side MD5/SHA/HASH/BASE64/AES/MASK de-identification. Keep only business-required identifiers unchanged; DA owns privacy handling.

## Table Profiles

- `demo_abtest_hive`: `demo_warehouse.demo_dsl_{log_lower}_fht0`.
- `demo_hive`: `demo_log.demo_dsl_{log_lower}_fht0`.
- BASE stable profile: `demo_warehouse.demo_dsl_{log_lower}_fht0` with inclusive `tdbank_imp_date` values `YYYYMMDD00` through `YYYYMMDD23`.

BASE keeps the `demo_dsl_...` prefix on both engines. `demo_dsl_...` belongs
to TEST evidence and must not be materialized into BASE SQL.

Default policy by current built-in profile:

- `demo_abtest_hive`: `tdbank_hourly`; requires `tdbank_imp_date` plus business time.
- `demo_hive`: `demo_log_dt_event_date`; requires `dtEventDate` lower/upper bounds, uses `dtEventTime` only when detailed time logic needs it, and must not add `tdbank_imp_date` by default.

Project overrides win over profile patterns.
