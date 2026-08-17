# Core SQL Rules

These are project-independent SQL generation rules. Project dates, tables, mappings, mode IDs, item IDs, bucket boundaries, and business definitions belong only in project config, canonical rules, XML/TLOG catalogs, or project source contracts.

## Source Priority

Before generating SQL, resolve in order:

1. Confirmed project canonical rules.
2. Explicit current user instruction.
3. Project source contracts and schemas.
4. XML/TLOG structure and comments.
5. Existing artifact facts.
6. General SQL assumptions.

Do not turn a one-off query assumption into a canonical rule. QUERY and downstream delivery routes are read-only against the rule store.

## Project And Dialect

- Read `project_config.json` for `sql_dialect`, `query_engine`, `query_environment`, table profile, and partition policy.
- Resolve log tables through project overrides/profile. Do not borrow another project's physical table convention.
- Hive is not synonymous with TDBank. Use `tdbank_imp_date` only when project policy or schema explicitly declares it.
- Read only the configured dialect guide.
- When the project has `execution_adapters`, read `execution-routing.md`, materialize once, then read only the selected profile's dialect guide. The configured default must be StarRocks; complexity, source density, and date span are diagnostics only. Select Hive only when the user explicitly requests Hive. TLOG table names keep the project-configured environment prefix; the adapter changes database and time policy, not business logic.
- Never guess a field, type, time field, or partition field when that choice changes executability or metric meaning.

## Parameters And Time

Executable retained queries use a top `params AS (...)` CTE for date/time and single-value zone/server controls.

```sql
WITH params AS (
    SELECT
        '2026-07-01' AS pt_start,
        '2026-07-09' AS pt_end,
        10001 AS zone_id
)
```

Use parser-safe names such as `pt_start`, `pt_end`, `ts_start`, `ts_end`, `zone_id`, and domain-specific IDs. Do not use reserved-looking aliases such as `end`, `partition`, or `end_partition`.

Rules:

1. If the user omits QUERY dates, run `query_window.py`: use the configured project start date through yesterday as inclusive fixed literals. Explicit user dates override this default. A missing project start date blocks default resolution; never guess or emit `CURRENT_DATE`.
2. Every TLOG source gets its own project-required time/partition predicate.
3. Natural-day/date-range logic uses the project date field and declared inclusive/exclusive policy.
4. Add event timestamp predicates only for explicit partial-day or timestamp-bound logic. Selecting or ordering by event time is not enough reason.
5. Do not wrap pruning fields in functions inside `WHERE` when direct comparisons are possible.
6. Retention, return, funnel, and conversion windows must include every required observation date. Unknown/unobservable windows are not zero.
7. Do not hard-code formal date or zone values in business CTEs; reference `params`.
8. DA parameters may already include time. Do not append a fixed midnight suffix blindly. Dashboard date ranges remain explicit DA inputs and do not inherit the QUERY default silently.
9. Portable TLOG SQL must give every source alias its own `TLOG_TIME_FILTER` token. Render it before workspace save; unresolved tokens are never executable SQL.

## Base, Grain, And Dedup

Before writing metrics, state internally:

- Base population;
- counted object and dedup key;
- output grain;
- numerator and denominator for ratios;
- event condition and attribution rule;
- concrete business filters and mappings.

Reduce each source to the intended join/metric grain before joining or dividing. A user metric normally dedups users before aggregation; a battle/session metric preserves the necessary battle/session key. Do not use `COUNT(DISTINCT ...)` to hide a many-to-many JOIN whose business relation is undefined.

## Player Subject Selection

Read `project_config.subject_identity_policy` before choosing a player key. The configured default is a semantic default, not a mandate to add joins.

1. Keep the default key when candidate keys have equal source coverage and SQL cost.
2. Prefer a confirmed native event-role key when it represents the same unique person and avoids an identity-conversion JOIN. Killer and victim metrics should normally use their native RoleID-role fields when no `vOpenID`-only attribute is required.
3. Use role-specific aliases such as `killer_player_id` and `victim_player_id` when one SQL contains multiple people. Do not collapse both into an ambiguous `player_id` before attribution.
4. Use `vOpenID` when it is the native/common key, or when the requested cohort, dimension, or downstream source genuinely requires it. Such a JOIN is business work, not identity normalization.
5. Never compare, concatenate, or `COALESCE` different identity namespaces. A cross-key bridge requires a project-confirmed relationship and a business reason.
6. Persist `subject_key_selection_v1` in the SQL fact bundle. Metric dedup metadata must use the selected source field; never infer `vOpenID` merely because an output label contains “玩家” or “用户”.

## JOIN Safety

- Join only after source filters and field projection.
- Include every anti-crossing key required by the business relation: date, zone/package, user/role, battle/server/session, mode, and unique event IDs as applicable.
- Do not assume `BattleSrvId` or a similar local ID is globally unique.
- For small dimensions/tag tables, reduce to one row per join key first.
- Ratios must not be computed after a detail JOIN that can multiply numerator or denominator.
- When a JOIN is intentionally many-to-many, declare the intended multiplication and aggregate at that relation's grain.

## Duration And State Fields

- Cumulative duration/state counters are not additive detail measures by default.
- First reduce at the correct player + battle/server/session grain using the saved rule: for example max, final-first delta, or terminal record.
- Sum only the reduced measure.
- Show bucket labels and stable sort keys for range output.
- Bucket boundaries and mapping names come from project rules or the current request, never global defaults.

## Ratios And Counts

- Define numerator and denominator in business terms and bind each to SQL evidence.
- Aggregate both at compatible grain before division.
- Protect zero denominators with the target dialect's supported form.
- Preserve raw decimal ratios in query results unless the output contract requires another representation; Dashboard display formatting belongs in spec.
- For large counts, pre-dedup then `COUNT(*)` when equivalent and clearer than repeated raw `COUNT(DISTINCT ...)`.

## Grouped Summary Feasibility

Before saving a grouped metric SQL, run `sql_summary_planner.py plan` and persist `summary_feasibility_v1`. Judge each metric at the requested overall grain, not merely at event-row grain.

- Sum additive metrics only when grouped rows form a complete partition of the metric units.
- Reconstruct a mean only from an unrounded source sum plus its exact weight. Never reweight a rounded displayed average.
- Reconstruct a rate only from its exact numerator and denominator. A normalized distribution's repeated `100%` is not a useful overall statistic.
- Sum distinct counts only when groups are mutually exclusive and exhaustive at the distinct-entity grain. Daily unique users, game-mode users, and other multi-membership groups are overlapping unless proven otherwise.
- Recompute median, P50/P90, percentile, and unique users across overlapping groups from source-level data.
- A bucket distribution cannot recover an exact source-level mean or percentile after the original values are discarded. Do not use bucket midpoints unless the user explicitly requests a documented approximation.

Use one grouped SQL when the overall value is exact from grouped rows or exact support components. Otherwise create separate `grouped` and `overall` query families, then link them with `query_analysis_bundle_v1`. The two SQLs must share parameters, physical sources, Base filters, and one metric-contract fingerprint.

## Compatibility

For Hive SQL that may pass through DA, PyMySQL, MySQL-style, or StarRocks-compatible analyzers:

1. Do not combine `SELECT DISTINCT` and `GROUP BY` in the same SELECT block. Prefer `GROUP BY` for deduplication.
2. Do not use `collect_list`, `collect_set`, or `concat_ws(',', collect_list(...))` for string samples unless execution is explicitly native Hive.
3. Apply the effective `identifier_policy`: preserve exact field casing and backtick every configured case-sensitive identifier. For current TDBank profiles, use ``alias.`dtEventTime` `` rather than bare `dtEventTime` or lowercase `dteventtime`.
4. Prefer the target executor's supported `group_concat(CAST(... AS string/varchar))` or `group_concat(concat(...))` form.

Recommended dedup:

```sql
dedup_user AS (
    SELECT zone_id, open_id
    FROM source_cte
    GROUP BY zone_id, open_id
)
```

Forbidden redundant form:

```sql
SELECT DISTINCT zone_id, open_id
FROM source_cte
GROUP BY zone_id, open_id
```

Also avoid mixing dialect-only functions, relying on SELECT aliases in `GROUP BY`, or using a function unsupported by the configured execution chain.

## Output And Privacy

- Formal SQL never uses `SELECT *`.
- Final fields use stable business-facing aliases; Dashboard output defaults to Chinese aliases unless explicitly overridden.
- Include `vOpenID` or equivalent identifiers only when the business result needs them; keep those values unchanged in SQL.
- Never add MD5/SHA/HASH/BASE64/AES/MASK or any other SQL-side de-identification. DA owns privacy handling for query and Dashboard output.
- Internal join/dedup use of identifiers and aggregate counts remain valid. Python-side fingerprints used for lineage are metadata, not SQL privacy transforms.
- Result columns, final SELECT, sidecar expected fields, samples, and Dashboard table fields must agree.
- Detail checks have a bounded `LIMIT`; formal aggregate output may omit it.

## Performance Route

Run `performance_preflight.py` for retained QUERY, VALIDATION, DASHBOARD, and review code view:

- L0/L1: apply `performance-routing.md` only.
- L2/L3: also load `performance-optimization.md`.
- L3 blocks formal save/promotion until repaired.

Optimization must preserve Base, filters, time window, dedup/grain, event attribution, numerator, denominator, CASE priority, and JOIN meaning.

## Generation Gate

Before delivering executable SQL:

1. Check the candidate against current active rules and unconditional project execution contracts.
2. Apply only active rules/hard constraints; keep weak/reverse candidates as diagnostics.
3. Save the SQL into query workspace.
4. For grouped metrics, persist the summary plan; create and validate the linked overall member before delivery when routing is `grouped_plus_overall`.
5. Obtain a ready `query_delivery_receipt_v1` for every exact indexed version.
6. Deliver only when `generation_gate.status=ok`, `delivery_ready=true`, and the final response links every receipt's absolute SQL file path.
