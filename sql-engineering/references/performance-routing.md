# SQL Performance Routing

Execution-engine materialization happens first. In a dual-engine project,
`sql_execution_adapter.py` uses the configured StarRocks default unless the
user explicitly requests Hive. Structural SQL complexity, relative source
density, and date span remain bounded diagnostics; none of them may silently
change the executor.
Performance preflight then evaluates that one materialized SQL with its selected
profile; it must not trigger a second business-SQL generation pass.

Use this short reference before retained SQL generation, validation, dashboard promotion, or SQL review. It decides how much performance knowledge must be loaded before optimizing.

Performance optimization remains mandatory for retained/formal SQL. Temporary query drafts use minimal safety checks first; they enter this routing flow when they are complex, likely reusable, explicitly performance-sensitive, or promoted to retained SQL.

## Temporary Query Exception

For first-pass temporary query SQL:

- Do not load the full optimization guide.
- Do not run deep artifact optimization unless a blocker is obvious.
- Check only direct executability, selected dialect time/partition policy, no SQL-side de-identification, no production `SELECT *`, and obvious JOIN amplification risks.
- Ask the user to run the SQL and confirm whether the data is correct.
- If the SQL will be retained, rerun preflight on the final SQL and record the result in `performance_level`.

## Required Preflight

Run the deterministic preflight before loading the full optimization guide for retained SQL or review:

```bash
python scripts/performance_preflight.py --project-root <project-root> --mode generation --artifact-kind QUERY --format json
python scripts/performance_preflight.py --project-root <project-root> --mode review --sql-file <sql-file> --format json
```

Record the result in `performance_level` or review JSON:

```yaml
optimization_tier: L0_perf_lite | L1_perf_standard | L2_perf_deep | L3_perf_blocking
preflight_score: number
preflight_triggers: []
optimization_reference: references/performance-routing.md | references/performance-optimization.md
full_guide_required: true_or_false
equivalence_preserved: true
performance_fingerprint: hash
```

## Tier Routing

### L0 `perf_lite`

Use for simple single-source SQL.

Rules:

- Do not load `references/performance-optimization.md` by default.
- Check project dialect time/partition filter, no `SELECT *`, limited detail output, and no SQL-side de-identification; DA owns privacy handling.
- Record a concise `performance_level`.

### L1 `perf_standard`

Use for normal analysis SQL.

Rules:

- Use this routing file plus the dialect reference.
- Apply obvious equivalence-preserving optimizations: base CTE filtering, field pruning, safe key cleanup, simple pre-aggregation before JOIN.
- Do not load the full optimization guide unless the preflight result says so.

### L2 `perf_deep`

Use for complex SQL.

Triggers include multi-log joins, repeated large-table scans, multiple `COUNT DISTINCT`, windows, retention, funnel, return, duration, battle/session analysis, reusable/dashboard source SQL, and intermediate-table candidates.

Rules:

- Load `references/performance-optimization.md`.
- Record applied equivalence-preserving optimizations and rejected logic-changing alternatives.
- Suggest intermediate tables when useful, but do not create them without user acceptance.

### L3 `perf_blocking`

Use when the SQL has a correctness, dialect, or execution-risk blocker.

Blockers include:

- TLOG missing the configured required partition lower or upper bound.
- StarRocks SQL using TDBank `tdbank_imp_date` without schema proof.
- Missing configured business-time bounds on TLOG tables.
- Function-wrapped time or partition predicates that prevent pruning.
- The preflight applies this blocker only when the wrapped field is used as a
  `WHERE` range/set predicate. A wrapper in `SELECT`/`GROUP BY`, or a
  field-to-field same-row integrity equality such as
  `CAST(dtEventTime AS DATE) = CAST(dteventdate AS DATE)`, is not itself a
  pruning failure; the raw partition bounds must still be present.
- Event-time-only projects adding an unconfigured import partition such as `tdbank_imp_date`.
- `SELECT *` in formal SQL.
- Same-block Hive `SELECT DISTINCT` with `GROUP BY`; use `GROUP BY`-only deduplication for execution-chain compatibility.
- Unquoted or case-mismatched identifiers required by the effective execution profile, such as bare `dtEventTime` on configured TDBank Hive chains.
- `collect_list` / `collect_set` / `concat_ws(collect_list(...))` string aggregation on DA/PyMySQL/StarRocks-compatible execution paths; use `group_concat(CAST(expr AS string/varchar))` or `group_concat(concat(...))`.
- DA/parameterized SQL appending a fixed midnight suffix with `concat(..., ' 00:00:00')`; DA time parameters may already include time.
- Incomplete predicates such as `iZoneAreaID =`.
- Raw large-log many-to-many JOINs.
- `BattleSrvId` JOIN without anti-crossing keys.
- Ratio metrics that may be calculated after detail JOIN row multiplication.
- Cumulative duration raw `SUM(TotalActiveDuration)`.
- Retention unavailable windows output as `0`.
- StarRocks/DA SQL crosses the empirical combined CTE-expansion guardrail: at least 48 top-level CTEs, dependency depth at least 30, and either at least 30 JOINs or a final CTE reference span of at least 32. This is a conservative incident-derived combination, not a claimed engine limit; many shallow CTEs alone do not block.

Rules:

- Block formal saving, validation promotion, or dashboard promotion.
- Fix blockers first.
- Load the full guide only when rewriting is needed.

## Scoring Summary

Preflight scoring is deterministic:

- TLOG source table: `+2`; second and later source tables add another `+2`.
- JOIN: `+2`.
- Top-level CTEs beyond 4: `+1` each; dependency depth is recorded separately.
- `COUNT DISTINCT`: `+2`.
- Window function: `+3`.
- `UNION`: `+2`.
- Ratio/share metric: `+2`.
- Retention, funnel, return, duration, battle/session, or complex distribution: `+4`.
- Same large table repeated scan: `+4`.
- Time window over 30 days: `+3`.
- Detail output without `LIMIT`: `+3`.
- Dashboard, reusable, or intermediate-table candidate: `+2`.

Thresholds:

- `0-2`: L0.
- `3-7`: L1.
- `8+`: L2 unless a blocker makes it L3.
- Any blocker: L3.

## Review Display

SQL review must show the full preflight result in code view. Product view should only surface performance risks that affect business judgement, such as JOIN amplification, unobservable retention, duration aggregation grain, and BattleSrvId crossing.
