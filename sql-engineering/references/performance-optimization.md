# SQL Performance Optimization

Use this full reference only when `scripts/performance_preflight.py` returns L2/L3, when fixing performance blockers, or when the user explicitly asks for best/deep performance. For L0/L1, use `references/performance-routing.md` instead. Optimization is always mandatory, but full-guide loading is tiered and every optimization must be equivalence-preserving.

## Boundary

Performance optimization may reduce scan rows, repeated scans, join input size, high-cost aggregation, and repeated computation.

Performance optimization must not change:

1. Business definition.
2. Numerator or denominator scope.
3. Time window.
4. Deduplication grain.
5. `CASE WHEN` order semantics.
6. `LEFT JOIN` / `INNER JOIN` business meaning.
7. Event-count vs distinct-count meaning.
8. Cohort, retention, or observation-day semantics.

P0 is always business correctness. If an optimization changes口径, reject the optimization and document it as an optional logic-changing rewrite.

## Generation Order

Generate SQL in this order:

1. Confirm metric definitions, numerator, denominator, and whether the metric counts users, roles, battles, events, days, or amounts.
2. Confirm final output grain and every `GROUP BY` dimension.
3. Confirm time window, partition range, and the bound convention declared by `project_config.partition_policy`.
4. Confirm deduplication key and join key.
5. Build the simplest correct SQL.
6. Run the optimization pass.
7. Run the self-check checklist and record the result in `performance_level`.

Do not optimize before the business grain is clear.

## Optimization Priority

Apply optimizations in this priority order:

1. P0: preserve business logic.
2. P1: reduce large-table scan range.
3. P2: reduce repeated scans of the same large table.
4. P3: reduce rows entering JOINs.
5. P4: reduce `COUNT(DISTINCT ...)` when a safe pre-dedup key CTE is possible.
6. P5: reduce rows entering window functions.
7. P6: reduce final sort volume and sort on business order fields.
8. P7: improve field names, CTE names, and口径 readability.

## Standard Shape

Prefer this CTE shape for analysis SQL:

```sql
WITH
params AS (...),
target_dim AS (...),

main_base AS (
    SELECT
        needed_columns_only
    FROM source_table t
    JOIN params p ON 1 = 1
    WHERE partition_filter
      AND event_time_filter
      AND business_filter
      AND valid_key_filter
),

entity_key AS (
    SELECT grain_keys
    FROM main_base
    GROUP BY grain_keys
),

numerator_agg AS (...),
denominator_agg AS (...),

final AS (...)
SELECT ...
FROM final
ORDER BY ...;
```

Every large-log base CTE must perform partition filtering, event-time filtering, business filtering, key validity filtering, and field pruning.

## Time And Partition

Use the selected project's executable time contract; do not impose one global
bound convention. For `demo_log_dt_event_date` whole-day scans, use actual
date-only bounds and no detailed event-time WHERE range:

```sql
t.dtEventDate >= p.pt_start
AND t.dtEventDate <= p.pt_end
```

Only for a genuine partial-day or explicit timestamp boundary, add detailed
event-time bounds. Demo's configured inclusive form is:

```sql
t.dtEventTime >= p.ts_start
AND t.dtEventTime <= p.ts_end
```

When the project `partition_policy.required_for_tlog = true`, also use the
configured partition field for pruning. For `tdbank_hourly` this is:

```sql
t.tdbank_imp_date >= p.pt_start
AND t.tdbank_imp_date <= p.pt_end
```

For `demo_log_dt_event_date` projects, use `dtEventDate` as the required
partition/date pruning field and add `dtEventTime` only when the SQL logic needs
detailed WHERE bounds. Selecting `dtEventTime` or using it for ordering/elapsed
logic alone does not require another scan filter. For true `event_time_only` projects, do not add
`tdbank_imp_date` unless the project config or schema explicitly confirms it. Do
not wrap large-table time or partition fields in functions inside `WHERE`, such
as `CAST(dtEventTime AS DATE)`, `DATE(dtEventTime)`,
`SUBSTR(tdbank_imp_date, ...)`, or `FROM_UNIXTIME(time_field)`. Derive date
fields in `SELECT` after filtering.

## Field Pruning And Cleaning

Do not use `SELECT *`.

A large-table base CTE should select only fields used by downstream CTEs or the final output.

Clean IDs once in the base CTE, then reuse normalized names:

| Source field | Standard alias |
|---|---|
| `RoleID` | `role_id` |
| `vOpenID` / `vopenid` | `vopenid` |
| `vopenid_da` | `vopenid_da` |
| `BattleSrvId` | `battle_srv_id` |
| `GameMode` | `game_mode` |
| `iZoneAreaID` | `zone_id` |
| `dtEventTime` | `event_time` |

Avoid repeated `CAST()` in `JOIN`, `GROUP BY`, and `ORDER BY`.

## Large Table Reuse

If the same large table appears more than once with the same time and business range, create one base CTE and derive later CTEs from it.

The base CTE must include only needed fields and every required filter. Repeated large-table scans are allowed only when the time range, project profile, or business range is materially different; document why in `performance_level.risk_items`.

## JOIN Rules

Reduce grain before joining:

1. If the right side only filters existence, reduce it to unique keys first.
2. If a side contributes a count metric, aggregate it to the business grain before the final join.
3. Do not raw-join multiple large logs unless target-grain matching truly requires it.

JOIN keys must include all anti-crossing fields needed by the project and metric. Common fields:

```text
zone_id, game_mode, battle_key, battle_srv_id, unique_battle_id, role_id, vopenid, event_date
```

For package, zone, mode, battle, or session analysis, do not join only on `BattleSrvId`. `BattleSrvId` may repeat across package, zone, date, or mode. Prefer `UniqueBattleID` when available; otherwise include at least `zone_id`, and include `event_date` or `game_mode` when needed.

## COUNT DISTINCT

Prefer pre-deduplication plus `COUNT(1)`. For Hive execution-chain compatibility, do not write `SELECT DISTINCT` and `GROUP BY` together in the same SELECT block; use the `GROUP BY` pattern below:

```sql
user_key AS (
    SELECT zone_id, role_id
    FROM raw_log
    GROUP BY zone_id, role_id
)
SELECT zone_id, COUNT(1) AS user_cnt
FROM user_key
GROUP BY zone_id
```

Only keep `COUNT(DISTINCT ...)` when a safe pre-dedup key CTE is not possible or when the engine-specific plan is intentionally preferred and documented.

Never confuse metric type:

1. People/roles: count deduplicated user or role key.
2. Events/times: count event rows.
3. Battles: count deduplicated battle key.
4. Days: count deduplicated date key.

## UNION

Use `UNION ALL` by default. Use `UNION` only when cross-source deduplication is part of the business definition.

If deduplication is required, prefer:

```sql
union_base AS (
    SELECT ... FROM a
    UNION ALL
    SELECT ... FROM b
),
dedup AS (
    SELECT key1, key2
    FROM union_base
    GROUP BY key1, key2
)
```

## Window Functions

Use window functions only after time filtering, field pruning, invalid-key filtering, and safe deduplication.

For `LAG` / `LEAD`, specify:

1. Partition fields.
2. Sort fields.
3. Boundary condition.
4. Abnormal-value handling.

Do not open windows on full raw logs when a filtered or deduplicated event stream is enough.

## Buckets And CASE WHEN

Complex bucket `CASE` logic should be computed once in a CTE. Output both label and order:

```sql
bucket_label,
bucket_order
```

Final `ORDER BY` must use the order field, not text labels.

Bucket boundaries must be mutually exclusive, stable, and explain open/closed intervals. Tail buckets such as `5+`, `10+`, or `80-100%` must be explicit.

`CASE WHEN` may carry priority semantics. Do not reorder branches for performance when the CASE represents funnel levels, player stage, anomaly priority, or highest-progress attribution. Compute flags first if optimization is needed, then preserve the final priority CASE order.

## Ratio Metrics

Ratio metrics must separate numerator and denominator before division:

1. Build `numerator_agg`.
2. Build `denominator_agg`.
3. Join them at the metric grain.
4. Divide with zero-denominator handling.

Do not calculate denominators after detail joins that can multiply rows.

Use naming consistently:

1. `xxx_rate`: numerator / denominator rate.
2. `xxx_share`: current bucket / current dimension total.
3. `xxx_ratio`: numeric ratio such as duration / duration.

## Retention

For retention, reduce active logs to one row per user and active day before joining to cohorts.

Each retention observation day should become a user-level flag, then final aggregation sums the flags.

Unavailable retention windows must output `NULL`, not `0`. Summary retention denominators include only cohorts that are observable for that retention day.

## Empty Buckets

Distribution reports that need stable display should create a target bucket dimension and fill missing buckets only after aggregation:

```sql
SELECT
    b.bucket_label,
    COALESCE(s.user_cnt, 0) AS user_cnt
FROM buckets b
LEFT JOIN bucket_summary s
  ON b.bucket_order = s.bucket_order
ORDER BY b.bucket_order
```

Do not cross-join raw detail rows to bucket dimensions just to fill empty buckets.

## Package, Zone, Battle, Session, And Duration

For multi-package or multi-zone queries:

1. Define package/zone dimensions explicitly.
2. Keep `zone_id` through every CTE.
3. Include `package_name` and `zone_area_id` in the final output.

For battle/session metrics:

1. Prefer `COALESCE(NULLIF(UniqueBattleID, ''), BattleSrvId)` as `battle_key` when available.
2. Aggregate battle-level metrics to battle grain before user-level aggregation.
3. For cumulative duration fields such as `TotalActiveDuration`, use `MAX()` at battle grain before summing.
4. Segment duration should use cumulative-value differences, such as `end_total_active_duration - start_total_active_duration`, and abnormal segments should be `NULL` or filtered.

## Required Metric Metadata

Before generating SQL, internally identify:

1. Statistical object: user, role, battle, event, day, amount, or session.
2. Output grain and final grouping fields.
3. Time window and partition window.
4. Population/base entering the denominator.
5. Behavior/event entering the numerator.
6. Deduplication key.
7. JOIN key.
8. Ratio formula and zero/null handling.
9. Empty bucket requirements.
10. Business sort order.

## High-Risk Anti-Patterns

Treat these as blockers or explicit risks:

1. `SELECT *`.
2. Missing partition or event-time filter on large TLOG tables.
3. Function-wrapped large-table time filters in `WHERE`.
4. Same large table scanned repeatedly with the same range.
5. Raw large-log-to-large-log many-to-many joins.
6. Unnecessary `COUNT(DISTINCT ...)`.
7. Unnecessary `UNION`.
8. JOIN key missing `zone_id` or equivalent anti-crossing field.
9. `BattleSrvId` treated as globally unique without evidence.
10. Cumulative duration fields summed over raw detail rows.
11. Priority/funnel `CASE WHEN` reordered.
12. Numerator and denominator counted after a multiplying join.
13. Unobservable retention output as `0`.
14. Bucket labels without order fields.
15. Final result missing the time window fields when the result is reusable.
16. StarRocks/DA query combines a very large top-level CTE set, a deep dependency chain, and heavy JOIN or long final-reference pressure. Compress pass-through layers, inline a small terminal aggregate, or split the query without changing business grain. Hive is an alternative only after explicit user selection.

## Post-Generation Self-Check

Every generated SQL must pass this checklist:

1. Every large table has partition filtering when available.
2. Every large table has business event-time filtering when available.
3. Large table base CTEs select only needed fields.
4. Same large table is not unnecessarily scanned repeatedly.
5. JOIN inputs are reduced to the required grain.
6. JOIN keys are complete enough to prevent package/zone/battle/date crossing.
7. Many-to-many joins cannot inflate metrics.
8. `COUNT(DISTINCT ...)` was considered for pre-deduplication.
9. `UNION ALL` is used unless deduplication is required.
10. `CASE WHEN` order semantics are preserved.
11. Numerator and denominator are aggregated separately for ratio metrics.
12. Zero denominators are handled.
13. Unobservable retention is `NULL`.
14. Distribution buckets include `bucket_label` and `bucket_order`, and empty buckets are filled only after aggregation when needed.
15. Final sort uses business order fields.
16. StarRocks/DA CTE structure passes the combined expansion guardrail; do not equate local CTE name recognition with executor expandability.

## Output Requirement For Best-Performance Requests

When the user asks for "最佳性能", "优化", "性能最好", or similar wording, output:

1. The optimized SQL.
2. Core performance optimizations applied.
3. Metric/business口径 explanation.
4. Optional changes that could further improve performance but would change口径 or require user confirmation.

Do not output SQL only. Explicitly distinguish logic-preserving optimizations from logic-changing alternatives.
