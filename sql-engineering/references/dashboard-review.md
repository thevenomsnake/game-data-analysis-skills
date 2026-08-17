# Dashboard SQL HTML Review

Use this workflow when reviewing saved DA dashboard SQL artifacts.

## Repository Navigation

Default project SQL browsing should use the formal SQL repository workspace. Use `serve` for fast local browsing without rewriting static files, especially while formalizing several SQL/result pairs:

```bash
req="[SQL_REPOSITORY] serve formal SQL repository for <project-root>"
python scripts/sql_repository.py serve --root <project-root> --user-request "$req" --function-selection SQL_REPOSITORY
```

The repository server exposes the live repository API at `/api/repository`, exposes the saved-dashboard review payload at `/api/dashboard-review`, serves the repository page at `/`, and serves this dashboard specialist surface at `/dashboard_review.html`. Use static `build` only when a durable file snapshot is needed:

```bash
req="[SQL_REPOSITORY] build formal SQL repository for <project-root>"
python scripts/sql_repository.py build --root <project-root> --user-request "$req" --function-selection SQL_REPOSITORY
```

Static build writes:

```text
<project-root>/reviews/sql_repository.html
<project-root>/reviews/sql_repository.json
```

That workspace reads only formal saved current `query_sql` from `manifest.json`. Saved `dashboard_sql` appears as an attachment under its linked source query, with copy/open actions. It never reads `_review_inbox` or raw `sql_review.json`; pre-stage review stays in `sql_review.py`.

Keep `dashboard_review.py` as the dashboard-contract specialist. Use it when the reviewer needs saved dashboard SQL approve/reject state or needs to inspect DA filter/total/display contracts in isolation.

Do not index draft queries or diagnostic SQL here and do not create a temporary Dashboard workspace. Temporary work stays in `query_workspace/index.html`; this page begins only after a Dashboard artifact is formally saved.

When a dashboard SQL is produced by `scripts/sql_formalize.py --target query-dashboard`, this page should consume the generated sidecar directly. Do not re-review the source SQL batch just to populate dashboard review; the formalization path already links QUERY, run evidence, validation, DASHBOARD, and the result sample.

## Purpose

Dashboard review is a tool-backed inspection, not a prose-only review. It reads each dashboard SQL artifact's `vNNN.spec.json` sidecar, derives a dashboard summary card, extracts the table output contract, compact field display rules, default time range, explicitly requested SQL parameter filters, explicitly requested DA interactive filters, fixed SQL filters, future inactive filters, grouping, totals, validation references, and sample data, then generates an HTML surface for human approval. The SQL file only carries a short `@DASHBOARD_SQL_HEADER` that points to the sidecar. It enforces that dashboard-facing final result columns are stable Chinese aliases by default. It does not judge or enforce DA visualization details or layout.

## Required Tool

Use:

```bash
req="build dashboard SQL HTML review for <project-root>"
python scripts/dashboard_review.py build --root <project-root> --user-request "$req" --function-selection DASHBOARD_REVIEW_HTML
```

Default outputs:

```text
<project-root>/reviews/dashboard_review.html
<project-root>/reviews/dashboard_review_state.json
```

For durable button clicks in the browser, prefer server mode:

```bash
req="serve dashboard SQL HTML review for <project-root>"
python scripts/dashboard_review.py serve --root <project-root> --user-request "$req" --function-selection DASHBOARD_REVIEW_HTML
```

Open the printed local URL. In `serve` mode, the HTML is a lightweight shell that fetches the latest saved dashboard sidecars, samples, and state from `/api/dashboard-review`; clicking `确认 SQL 没问题` or `标记 SQL 有问题` writes back to `reviews/dashboard_review_state.json` through `/api/state`.

Static HTML mode still supports click interaction and export, but browser clicks are persisted only in localStorage unless the exported state JSON is saved back to the project state file.

## Skip Rule

By default, `build` and `serve` skip dashboard SQL whose state is:

```json
{
  "status": "approved",
  "sql_hash": "<current SQL hash>"
}
```

Use `--include-approved` to include approved SQL again. If SQL content changes, the hash changes and the SQL returns to the review list automatically.

## Review Surface Contract

The HTML must show:

1. Left side: all dashboard SQL pending review.
2. Right side: a compact `看板摘要` card with exactly these reviewer-facing rows: `指标`, `维度`, `筛选项`, and `统计周期`, plus a `复制摘要` button.
3. Parsed dashboard control contract from `da_filter_contract`, including true SQL parameter filters from `sql_parameter_filters` and explicitly requested DA output-field filters from `filterable_fields`.
4. Hidden technical parameters such as partition bounds.
5. Grouping fields and SQL-declared output-shape policy.
6. Compact display rules from `visual_review_contract` when fields need display conversion, such as `玩家占比` raw `0-1` displayed as percent with 2 decimals.
7. Metric labels, aggregation, and total safety.
8. Actual sample rows from the saved `.csv` or `.xlsx` result file when linked run evidence has one.
9. Synthetic sample rows when no real result file exists.
10. Contract errors when the SQL top comment cannot be parsed or required fields are missing.
11. Buttons to approve or reject each SQL.
12. Bottom summary of approved and rejected SQL.

Dashboard output-shape ownership is SQL-declared: DA only chooses the query date range and whether realtime refresh is needed. Dashboard SQL defaults to one result for the selected date range. Daily rows, total rows, mixed rows, or another table shape must be explicitly declared by `sql_output_contract.output_shape`. The review page may display `result_mode`, `time_grain`, `output_grain`, and declared total-row metadata, but it must not force daily rows, total rows, period totals, or `UNION ALL` outside the SQL.
The `看板摘要` card is derived from existing spec modules and must not require a new top-level SQL block:

- `指标`: labels or fields from `metrics`; it must be non-empty.
- `维度`: labels or fields from `dimensions`; it must be non-empty. For an overall-only dashboard, declare an explicit overall/no-dimension dimension rather than leaving it ambiguous.
- `筛选项`: only active, dashboard-user-visible items from `da_filter_contract.sql_parameter_filters` and `da_filter_contract.filterable_fields` that are explicitly meant to be dashboard controls. If no such item is explicitly declared, show `无`; do not infer filters from dimensions, buckets, sort fields, fixed SQL conditions, or SQL-internal logic filters.
- `统计周期`: derive first from `sql_output_contract.output_shape`, then fall back to `dashboard_intent.result_mode` and `dashboard_intent.time_grain`, such as `区间合计`, `按日`, `按日 + 合计`, `SQL 声明`, or `按小时`. This is a display summary only; do not use it to force or repair the SQL output shape. The default for new dashboard SQL is `区间合计`.

The `复制摘要` button must write exactly four plain-text lines to the clipboard, using `、` between multiple values and `无` for empty filters:

```text
指标：删除战斗服数、平均开启时长小时
维度：桶排序、战斗服开启时长小时桶
筛选项：无
统计周期：区间合计
```

Keep the visible card readable, but do not require reviewers to manually select chip text or reformat copied content.

## Sidecar Enforcement

Dashboard sidecar spec must include a machine-review module:

```yaml
machine_review_contract:
  contract_version: dashboard_review_v1
  parse_required: true
  parser: scripts/dashboard_review.py
  contract_preview_required: true
  review_state_file: reviews/dashboard_review_state.json
  skip_approved_on_next_review: true
  result_sample_policy: use_saved_result_file_else_auto_sample
```

Missing or malformed `machine_review_contract`, `da_output_contract`, `da_filter_contract`, `sql_parameter_filters`, parameter visibility, grouping/total policy, metric total policy, non-Chinese final output fields, or mismatched `expected_fields` must be treated as a review failure for new dashboard SQL. Missing or malformed `visual_review_contract` is a failure only when output fields need display conversion, such as ratio/rate/percentage fields.

Formal dashboard SQL must not keep the old full `@DASHBOARD_SQL_SPEC` YAML block. The short SQL header is for quick orientation only; machines read `vNNN.spec.json`.

`sql_output_contract.expected_fields`, `da_output_contract.table_fields`, active `filterable_fields.output_field`, and final `SELECT` aliases must match exactly and use the Chinese field names seen by dashboard readers. English names such as `stat_date`, `metric_name`, or `metric_value` may remain as CTE/internal/stable keys, but not as the final result column names.

`visual_review_contract` must stay short and should be omitted when no output field needs display conversion. It declares how emitted fields are displayed, not chart type or layout. For fields named like `占比`, `比例`, `比率`, `转化率`, `留存率`, or `率`, declare `display_format=percent`, `source_value_scale=ratio_0_to_1` or `percent_0_to_100`, `decimal_places`, and a sample `raw_value -> display_value` check. For example, `0.2307443725 -> 23.07%`.

SQL parameter filters and DA output filters must stay separate. A filter such as `GameMode IN (${game_mode_ids})` changes the SQL execution range and must be declared in `da_filter_contract.sql_parameter_filters` with a matching `parameters.name`. A DA filter over an emitted field belongs in `da_filter_contract.filterable_fields` only when the dashboard requirement explicitly says viewers should be able to change it. A dimension, bucket, sort column, fixed SQL condition, or query-only WHERE condition is not a dashboard filter by default.

## State File

The state file is project material. Keep it under:

```text
reviews/dashboard_review_state.json
```

Accepted statuses:

- `approved`: reviewer confirmed the SQL is OK and unchanged hashes can be skipped later.
- `rejected`: reviewer found an issue; keep it in future review until fixed and re-approved.

Do not mark a SQL approved just because it parses. Approval means the reviewer has inspected the parsed filter contract, samples or synthetic rows, grouping, totals, and contract warnings.

## Result File Handling

When a dashboard artifact links to passed user-run evidence with a saved `.csv` or `.xlsx`, the HTML uses the first rows as sample data. When no result file is available, the HTML still renders the filter contract and synthetic rows, but the sample source must say it is synthetic.

Synthetic samples never count as data validation.
