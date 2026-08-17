# SQL Execution Routing

Use this contract when one project exposes the same TLOG through a fast
StarRocks path and a stable Hive/TDBank path.

## Single Business SQL

Write business logic once as `portable_tlog_sql_v1`. The adapter owns only:

- TLOG database qualification;
- partition field and value format;
- executor-specific exact-case identifier quoting;
- configured StarRocks default or explicit Hive profile selection;
- the persisted `execution_route_v1` receipt.

Do not ask the model to independently generate an SR SQL and a Hive SQL. Render
one runnable SQL, save that file, and persist its selected profile.

The selected profile owns `identifier_policy`. Rendering preserves the exact
configured spelling and adds backticks around every listed case-sensitive
field; static gates reject materialized SQL that bypasses this policy.

```sql
WITH params AS (
    SELECT
        '{{PT_START}}' AS pt_start,
        '{{PT_END}}' AS pt_end,
        10001 AS zone_id
),
base AS (
    SELECT pl.vOpenID
    FROM {{TLOG:PlayerLogin:pl}}
    WHERE {{TLOG_TIME_FILTER:pl}}
      AND pl.iZoneAreaID = (SELECT zone_id FROM params)
)
SELECT COUNT(DISTINCT vOpenID) AS user_cnt FROM base
```

Every `{{TLOG:LogName:alias}}` must have
`{{TLOG_TIME_FILTER:alias}}`. DA-owned physical tables are written literally;
the adapter never rewrites them or injects TLOG partitions into them.

The selected profile's `time_integrity_policy_v1` is applied at the same
rendering point. For DEMO_ANALYTICS StarRocks, a query that uses `dtEventTime` or
includes today receives a per-alias `dtEventTime`/`dteventdate` same-local-date
filter. Use `{{TLOG_TIME_INTEGRITY_FILTER:alias}}` when the template needs to
place it explicitly; otherwise the adapter adds it to the regular time token.
Profiles without a verified paired server-date field remain `report_only` and
must not be given guessed columns. This is separate from the universal
today-result coverage contract; see `time-integrity.md`.

## Physical Contract

`{log_lower}` comes from the XML/TLOG log name. The project config owns the
project table prefix, physical database, pattern, and partition policy for each
profile. The adapter substitutes those fields without changing business CTEs.

| Profile role | Physical TLOG | Whole-day partition |
|---|---|---|
| StarRocks fast | profile `table_pattern` | configured event-date field with inclusive date bounds |
| Hive stable | profile `table_pattern` | configured import partition with inclusive hourly bounds |

## Default Route

StarRocks is the only implicit route. `--profile auto` means “use the configured
StarRocks `default_profile`”; it does not choose an executor from complexity.
Select Hive only when the user explicitly requests Hive, by passing the exact
Hive profile such as `--profile hive_stable`.

Structural complexity, relative source density, and requested date span remain
bounded diagnostics. They can trigger optimization warnings or StarRocks
compatibility blockers, but never silently switch the executor. When a query
crosses the Hive advisory threshold, keep StarRocks and report the diagnostic;
compress or split the SQL first. Offer Hive as an alternative only for explicit
user choice.

Historical row counts are relative-density evidence only. Never present them as
current BASE scan estimates. The route receipt must preserve
`absolute_count_usable=false` when the reference is stale or from TEST. It also
stores `scan_assessment`, including structural score, source-density multiplier,
date multiplier, amplified score, advisory threshold, and the fact that route
selection is `configured_starrocks_default_or_explicit_profile`.

TLOG environment prefixes remain project-configured. Projects whose table
profile uses `demo_dsl_*` accept that family; projects whose profile uses
`demo_dsl_*` accept the RMTEST family. Do not treat either prefix as globally
valid or globally invalid. For materialized SQL, physical-table matching only
validates the configured route; it never selects another profile. A mismatch
blocks until an exact explicit route receipt or the correct project binding is
provided.

## Command

```powershell
python scripts/sql_execution_adapter.py render `
  --root <project-root> --template-sql <portable.sql> `
  --start-date 2026-07-09 --end-date 2026-07-14 `
  --profile auto --function-selection QUERY `
  --user-request "<verbatim request>" --format json
```

The rendered file stays under the project. Save it immediately through
`sql_query_workspace.py save`; do not deliver the portable template.

## Explicit Execution Variants

The route receipt is also the only source for an exact materialized SQL's
engine, dialect, selected profile, routing role, route status, and portable
template reference. Downstream catalog code projects those persisted facts as
`execution_delivery_v1`; it does not inspect SQL text to rediscover them.

Normally one adapter call creates one materialized engine. When an upstream
workflow deliberately materializes the same logical revision for more than one
executor, pass one stable logical revision and variant group to every exact
render:

```powershell
python scripts/sql_execution_adapter.py render `
  --root <project-root> --template-sql <portable.sql> `
  --start-date 2026-07-09 --end-date 2026-07-14 `
  --profile starrocks_fast `
  --logical-revision-id lr-retention-v003 `
  --variant-group-id vg-retention-v003 `
  --recommended-variant `
  --function-selection QUERY --user-request "<verbatim request>"
```

Use the same IDs and a distinct `variant_key` selected by the persisted route
for the other materialization. Mark at most one exact variant recommended.
Workspace and formal save reuse this identity only when the route receipt hash
matches the exact SQL. A changed SQL loses the identity until an upstream flow
explicitly assigns a new stable relation.

Never infer `logical_revision_id`, `variant_group_id`, exact variant members, or
recommendation from title, tags, table/database prefix, path, SQL text, or
`branch_of`. A portable template with one materialized target is one execution
version, not a dual-engine asset. Missing historical route facts remain
`legacy_unlabeled`.
