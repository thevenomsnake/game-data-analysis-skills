# SQL Header Layering

Use this when deciding what belongs in SQL comments versus sidecar specs.

## Principle

SQL files should be readable and runnable. Full governance belongs in sidecar
JSON. Do not make a SQL file carry every audit, review, performance, lineage,
and dashboard detail in its top comment.

## Three Artifact Levels

### Temporary SQL

Purpose: answer "can this data run and look right?"

- No formal header. Keep the mandatory one-line script-managed generation identity:

  ```sql
  -- @SQL_GENERATION skill=sql-engineering; skill_version=<version>; generated_by_ldap=<ldap-user>
  ```

  The workspace writer inserts or refreshes this line; never type it manually.
- No formal sidecar spec or formal manifest metadata.
- Keep only short ordinary comments when helpful.
- Put run values in a top `params AS (...)` CTE when practical.
- Save each deliverable/revised SQL under `query_workspace/` with lightweight
  version metadata and a short searchable purpose before showing it for run.
- Preserve the version the user ran; corrected SQL becomes the next version.

### Retained Query SQL

Purpose: preserve a useful data question and its product/business logic.

Use `@SQL_QUERY_HEADER`. Focus on:

- business question
- Base/statistical object
- metrics in business language
- fixed filters/exclusions and important ID ranges
- time range and output grain
- verification status and sidecar path

The executable body must start with `WITH params AS (...)` before business CTEs.
Put configured partition/date bounds (`pt_start`, `pt_end`) and zone/server scope
such as `zone_id` there. Add business-time bounds (`ts_start`, `ts_end`) only
when the SQL has an explicit detailed-time WHERE boundary. If a SQL
received from another channel hard-codes dates or iZoneAreaID/GameSvrId values,
rewrite it into this params-CTE shape before saving it as a retained query.

Full numerator/denominator details, canonical rule traces, performance preflight,
data sources, output contract, and quality gate go into `vNNN.spec.json`.
The same one-line `@SQL_GENERATION` identity precedes the formal header.

### Dashboard SQL

Purpose: hand a stable table dataset to DA.

Use `@DASHBOARD_SQL_HEADER`. Focus on:

- `指标`
- `维度`
- `筛选项`
- `统计周期`
- SQL parameters and DA filters
- display format rules such as percent with two decimals
- SQL-declared output-shape policy
- verification status and source query

For date/total output, default to one date-range result and describe the explicit SQL/spec shape without changing it. The header/sidecar can say whether the SQL emits daily rows, total rows, mixed rows, or another shape, but should not invent or require those rows outside the SQL.
Do not re-explain the full query logic in dashboard SQL. The dashboard artifact
inherits the source query/validation logic by reference. If logic changes, mark
`logic_changed: true` and put the full reason and verification requirement in
the sidecar.
The same one-line `@SQL_GENERATION` identity precedes the Dashboard header.

## Dashboard Filter Semantics

`筛选项` means dashboard controls users can change. It does not mean:

- SQL-internal WHERE filters
- fixed business filters
- dimensions
- bucket fields
- sort fields
- fields that merely appear in the final table

If no dashboard control is explicitly requested, write `筛选项：无`.

## Sidecar Ownership

Use sidecar specs for anything machines or governance need:

- canonical rule context
- performance preflight
- source tables and intermediate tables
- query output contract
- validation evidence
- dashboard machine review contract
- DA filter/display/total contract
- quality gates

Use meta files for lifecycle, discovery, and generation provenance:

- current/history/superseded
- reusable and reuse notes
- linked query/validation/run
- metrics, dimensions, tables, tags
- project snapshot
- `generation_provenance` copied from the sidecar spec

## Save Workflow

Normal retained-artifact saves still use the shared project writer:

1. Save/gate the directly runnable source in `query_workspace/`.
2. After the user confirms retention value, create the matching `*.spec.json`
   with `origin_query_workspace`.
3. Run `save-sql --spec-file <spec.json>`; unindexed QUERY sources are blocked.
4. Let `save-sql` write the final `vNNN.sql`, `vNNN.spec.json`, and
   `vNNN.meta.json`.
5. Run `project_validate.py --format json --strict` when validating a project.

For already-run SQL plus result evidence, prefer the fast formalization owner:

```bash
python scripts/sql_formalize.py --root <project-root> --source-sql <query.sql> --result-file <result.xlsx> --target query-dashboard --user-confirmed --format json --use-fact-bundle auto --refresh-viewers incremental --user-request "$req" --function-selection SQL_FORMALIZE
```

`sql_formalize.py` first reuses or creates the source query-workspace record,
then writes the same formal three-file artifacts through one bundle transaction
instead of calling `save-sql`/`save-run` repeatedly. It records the workspace
origin, normalizes params, inspects result evidence, generates linked specs,
writes manifest/index once, marks the source promoted, and refreshes viewers
according to `--refresh-viewers incremental|deferred|full`.

Do not hand-edit the final `spec_path` in the SQL header. It depends on the
version number assigned by the artifact writer.
