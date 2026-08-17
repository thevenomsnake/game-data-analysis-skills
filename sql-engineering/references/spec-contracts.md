# Spec Contracts

Use this contract for formal lifecycle artifacts. SQL files are no longer the
full audit container. A formal artifact is a three-file unit:

```text
vNNN.sql
vNNN.spec.json
vNNN.meta.json
```

## Storage Layers

- `vNNN.sql`: executable SQL plus a short stage-specific header.
- `vNNN.spec.json`: full machine/governance contract for query, validation, or dashboard.
- `vNNN.meta.json`: discovery, lifecycle, lineage, reuse, links, project snapshot, and generation provenance.
- Package `manifest.json`: indexes every member, current pointer, and lineage edge; the project manifest keeps only a compact Package projection.

The sidecar spec is authoritative for machines. The SQL header is for quick
human orientation and for locating the sidecar. Formal assets also carry
`generation_provenance` in sidecar spec and manifest/meta so reviewers can see
which skill version, script, and workflow generated or migrated the asset.

## Temporary Query

Temporary SQL does not use formal headers or formal sidecar specs, but every
deliverable query is a lightweight project-local workspace record:

- Use literal values, normally through `params AS (...)`.
- Use the configured project dialect and time/partition policy.
- Do not generate `@SQL_QUERY_SPEC`, `@SQL_QUERY_HEADER`, or formal metadata.
- Save SQL plus `vNNN.meta.json` and a lightweight
  `vNNN.formalize_seed.json` under `query_workspace/`.
- Update `query_workspace/index.json` and `index.md` before delivery.
- Require `generation_gate.status=ok` and `delivery_ready=true` before returning
  the SQL path for execution.
- Do not promote into `formal_assets/` until the user confirms the SQL has future value and approves its Promotion Plan.
- If it is not worth retaining, transition the existing workspace version to
  `discarded`; do not create another archive copy.

## Retained Query SQL

Retained query SQL uses exactly one short `@SQL_QUERY_HEADER` block. The header
must stay product/business-facing and include:

- `spec_path`
- artifact id, project, and title
- business question
- Base/statistical object
- metric summary
- key filters and exclusions
- time range
- output grain
- result usage and verification status

The executable body must start with a top `params AS (...)` CTE. Required
conventions:

- `pt_start` and `pt_end` appear only when the project partition policy requires
  partition/date pruning. Whole-day Demo SQL stores actual date-only bounds and
  compares with `>=` / `<=`.
- `ts_start` and `ts_end` appear only when the SQL has an explicit detailed-time
  WHERE boundary. Selecting or ordering by event time does not require them.
- `zone_id` or an equivalent short alias holds iZoneAreaID/GameSvrId scope when
  the query filters one.
- Hard-coded date/time and iZoneAreaID/GameSvrId literals in WHERE must be
  rewritten into `params` before `save-sql`.

Do not put module docs, performance preflight details, quality gates,
canonical-rule traces, full data source inventories, or script logs in the SQL
header. Put them in `vNNN.spec.json`.

The query sidecar follows `schemas/query_spec.json` and remains the source for:

- full `query_logic`
- metric numerator/denominator/formula meaning
- canonical rule context
- data sources and intermediate tables
- performance preflight
- output contract and quality gate
- `origin_query_workspace`, linking the exact temporary/executed SQL version
  that was promoted

## Validation

Validation may be evidence-only through `vNNN.spec.json` plus run evidence. If a
validation SQL file exists, it uses exactly one short `@VALIDATION_HEADER` with:

- `spec_path`
- artifact id and title
- validation purpose
- evidence status
- promotion decision
- confidence score

The validation sidecar follows `schemas/validation_spec.json` and stores locked
data sources, grain, metrics, dimensions, user-run evidence, privacy,
performance, confidence, and promotion decision.

## Dashboard SQL

Dashboard SQL uses exactly one short `@DASHBOARD_SQL_HEADER` block. It is a DA
handoff header, not a business-logic explanation. Include:

- `spec_path`
- artifact id, project, dashboard application, and source query
- exactly the reviewer summary rows: `指标`, `维度`, `筛选项`, `统计周期`
- time parameters
- true SQL parameter filters
- DA output-field filters
- display-format rules
- SQL-declared output-shape policy
- verification status
- `logic_changed`

Dashboard SQL must not repeat full query business logic. If dashboard conversion
changes metric formulas, default filters, dedup grain, source logs, or other
business meaning, set `logic_changed: true` and record the full change,
validation requirement, and risk in the sidecar.

Dashboard `筛选项` only means controls that dashboard users can change. Do not
infer it from dimensions, buckets, sort fields, fixed SQL conditions, or
SQL-internal WHERE logic.

The dashboard sidecar must record the explicit output-shape contract. `refresh_contract` states that DA only decides date range and realtime refresh. Default to `dashboard_intent.result_mode=period_total_table`, `time_grain=none`, and `sql_output_contract.output_shape.result_mode=period_total_table` for one date-range result. Use daily, total-row, or mixed modes only when SQL/spec explicitly declares them; do not infer, force, or block daily rows, total rows, period totals, or `UNION ALL` outside the SQL.
The dashboard sidecar follows `schemas/dashboard_spec.json` and stores the full:

- `machine_review_contract`
- `validation_reference`
- `da_delivery_contract`
- `da_output_contract`
- `visual_review_contract`
- `da_filter_contract`
- dimensions, metrics, totals, output fields, performance, and quality gate

## Save Requirements

Normal formal saves go through `scripts/sql_formalize.py` from an indexed workspace SQL plus result evidence. It writes the final `vNNN.sql`, `vNNN.spec.json`, and `vNNN.meta.json`, refreshes the short header, and records manifest/meta lineage in one transaction. Direct `sql_project.py save-sql` is an internal migration/repair primitive, not a parallel user lifecycle.

The formalize bundle writer may bypass repeated low-level save subprocesses so QUERY, run evidence, VALIDATION, optional DASHBOARD, manifest, superseded meta, and index update happen once in one transaction.

Formal artifact metadata must include:

```json
{
  "spec_path": ".../vNNN.spec.json",
  "spec_storage": "sidecar_json",
  "header_contract_version": "1",
  "generation_provenance": {
    "schema_version": "asset_generation_v1",
    "skill_name": "sql-engineering",
    "skill_version": "4.162.0",
    "sql_spec_version": "4.8",
    "workflow": "save-sql|fast_formalize_query|fast_formalize_validation|fast_formalize_dashboard|historical_asset_backfill",
    "generated_by_script": "sql_project.py|sql_formalize.py|unknown_historical",
    "generated_at": "...",
    "saved_by_script": "sql_project.py|sql_formalize.py"
  }
}
```

Fast-formalized QUERY specs also store `formalize_bundle` with reusable facts such as SQL fingerprint, result schema fingerprint, normalized-param status, performance fingerprint, rule-context status, fact-bundle source, retained `analysis`, and `output_field_contract` when a result file defines retained output fields. This records what was already known so later dashboard/repository work does not repeat review/generation reasoning. A fact bundle is reusable only when its SQL fingerprint matches the current normalized/source SQL. Stored `rule_context` may be reused for formal save only when it declares `rule_context_mode=formalize` and its canonical-rule fingerprint still matches the current project rules; temporary-query diagnostics are not strict formalization evidence. When the result contract removes output fields, `formalize_bundle.analysis.metrics` and `formalize_bundle.analysis.dimensions` must be filtered by the same retained output-field contract before writing specs or metadata.

Workspace metadata, formalize seeds, QUERY specs, `FormalizeBundle`, and Dashboard specs store `knowledge_usage_v1`. `used` requires matching `knowledge_references`; `not_used` is an explicit decision when active bindings are irrelevant; `not_available` is automatic only when the project has no active binding; `legacy_unknown` never promotes. Each `knowledge_reference_v1` locks project, dataset version/content hash, immutable usage-contract version/hash, projection hash, fields, usage mode, compact selection evidence, and `resolution_fingerprint`. Formalization blocks stale/tampered references instead of silently resolving a newer binding.

The result file is authoritative for retained output fields during fast formalization. `query_output_contract.expected_fields`, dashboard `sql_output_contract.expected_fields`, `da_output_contract.table_fields`, repository summary metrics, and final SELECT order must follow the retained result columns. If SQL has extra final output columns, they may be removed from the final SELECT and recorded as `removed_output_fields`; if the final output is a simple `SELECT *`/`alias.*` from one CTE whose aliases cover the result columns, and the final FROM clause contains only that source plus an optional alias before the next safe clause, formalization may expand it to explicit retained fields first; joined, comma, subquery, or schema-qualified final sources are not expanded, and if the result file has columns missing from SQL final SELECT or that source CTE, formalization blocks. After final SELECT pruning, formalization may also remove unused explicitly aliased CTE output expressions whose aliases are no longer referenced downstream, safe `LEFT JOIN` clauses whose right side is a grouped CTE unique by explicit or ordinal GROUP BY join keys and whose alias is otherwise unused, and CTEs that become downstream-unreferenced. It records `internal_pruning_status`, `internal_pruning_removed_fields`, `internal_pruning_removed_joins`, `internal_pruning_removed_ctes`, and `internal_pruning_iterations`. This is not permission to rewrite INNER/RIGHT/FULL joins, WHERE, GROUP BY, HAVING, ORDER BY, or source scans without stronger lineage proof.

Every workspace version may have an adjacent `vNNN.formalize_seed.json`
produced by `scripts/sql_query_workspace.py`. That seed is intentionally
smaller than a formal sidecar spec: it stores project-relative SQL identity,
fingerprint, deterministic analysis, project/rule fingerprints, provenance,
workspace reference, and `rule_context_mode=temporary` diagnostics. It does
not invent a repository summary, must not be referenced as a formal manifest
artifact, must not unlock validation/dashboard promotion, and must not claim
user-run evidence. Formalization still runs or reuses a strict matching
formalize-mode rule context and accepted semantic summary.

New seeds use `schema_version=formalize_seed_v2`. Their `project_root` is `.`,
their SQL/workspace paths are project-relative, and the standalone seed tool
may only repair the adjacent seed for an already indexed workspace SQL.

Formal SQL that still contains `@SQL_QUERY_SPEC`, `@VALIDATION_SPEC`, or
`@DASHBOARD_SQL_SPEC` is invalid after migration.

## Enforcement

Project health checks must verify:

- sidecar spec exists, parses, and has `spec_meta.spec_version = 4.8`
- SQL has exactly one short header for its kind
- SQL has no legacy full spec block
- header is within the line budget
- manifest/meta `spec_path` and `spec_storage` agree
- sidecar and manifest/meta record `generation_provenance`; historical missing values are warnings until backfilled
- dashboard review reads the sidecar spec, not SQL inline YAML
- performance and dashboard contracts are checked from the sidecar
- every new formal QUERY has a valid `origin_query_workspace` link
- query-workspace SQL/meta/index fingerprints and project-relative paths are consistent

Header budgets:

| Artifact | Target | Warning |
|---|---:|---:|
| Temporary SQL | 0-10 ordinary comment lines | not formal |
| Query SQL | 20-60 lines | >80 |
| Validation SQL | 10-40 lines | >60 |
| Dashboard SQL | 20-50 lines | >60 |

## Migration

Use `scripts/migrate_sql_headers.py` for one-time header/sidecar migration: it extracts legacy inline YAML specs into `vNNN.spec.json`, replaces the SQL top with a short header, and updates manifest/meta spec references.

Use `scripts/migrate_asset_provenance.py` for generation-provenance backfill: it updates existing formal QUERY/VALIDATION/DASHBOARD manifest records, meta files, and sidecar specs with explicit asset generation provenance. Run it in dry-run mode first and add `--write` only after reviewing the planned changes.

Do not use legacy full spec blocks as a long-term compatibility path.
