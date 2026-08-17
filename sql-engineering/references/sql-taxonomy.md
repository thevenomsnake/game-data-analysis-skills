# SQL Taxonomy

This file defines stable discovery enums and metadata only. Lifecycle and command procedure belong to `project-workflow.md`.

## Asset Kinds

- `QUERY`: retained directly queryable SQL worth future reuse or promotion.
- `VALIDATION`: validation/promotion contract and optional executable checks.
- `DASHBOARD`: DA-facing SQL derived from a retained QUERY.

Workspace SQL is not a formal kind. It uses `query_id + immutable version + status` until formalized.

## Workspace Metadata

Family-level discovery fields:

```text
query_id, title, purpose, business_question
status, current_version, current_path
usage_class
sql_fingerprint, logic_fingerprint
change_type, coverage_relation, branch_of
business_category, analysis_type
source_logs, tables, metrics, dimensions, filters, params
grain, time_grain, tags, derived_output_count
generation_provenance
```

Each version adds path/meta/seed, both fingerprints, change summary, generation gate, lifecycle history, exact-version derived outputs, and formal link.

## Workspace Status

`draft | runnable | run_failed | result_confirmed | discarded | archived | promoted`

See `query-workspace.md` for meaning and delivery eligibility.

Status records execution and lifecycle, not long-term value. Record SQL value separately as `usage_class`:

| Value | Meaning | Typical next action |
|---|---|---|
| `personal_diagnosis` | One person/account/order/session or one incident; little reuse after the answer | Visualize the result when useful, then `discarded` |
| `reusable_diagnostic` | Parameterized method for a recurring data/telemetry/problem check | Keep current in Workspace; formalize only when team reuse justifies it |
| `ad_hoc_analysis` | One campaign, decision, or retrospective whose output may matter more than rerunning the SQL | Preserve result/visual lineage; keep history or discard SQL deliberately |
| `reusable_analysis` | Stable Base, metrics, filters, and output contract suitable for repeated analysis | Formal QUERY candidate |
| `recurring_delivery` | Repeated operational or DA delivery | Formal QUERY plus optional Dashboard |
| `unclassified` | Legacy or genuinely unresolved value | Periodic curation must decide; never infer reuse from result existence |

`usage_class`, execution `status`, and visualization attachments are orthogonal. A confirmed result does not make SQL reusable, and a discarded personal query may still retain its exact result and visualization.

Default presentation follows SQL intent, not lifecycle state: `personal_diagnosis` and `reusable_diagnostic` use a self-contained diagnostic HTML report; `ad_hoc_analysis`, `reusable_analysis`, and `recurring_delivery` use analytical Excel. `unclassified` Bug investigation uses HTML after semantic inspection. An explicit user format always wins.

## Business Categories

| Value | Scope |
|---|---|
| `new_user` | registration/cohort/first-day users |
| `active_user` | DAU/WAU/MAU/login activity |
| `retention` | retention, return, lifecycle |
| `funnel` | conversion and drop-off |
| `ab_compare` | experiment/package comparison |
| `battle_behavior` | battle, room, mode, match behavior |
| `economy` | item, currency, shop, lottery, resource flow |
| `social` | team, friend, guild, chat, community |
| `technical_quality` | crash, patch, block, device/performance quality |
| `content_progression` | mission, level, tutorial, unlock, progression |
| `data_quality` | reconciliation, null, duplicate, field checks |
| `ops_health` | online/server/operational health |
| `privacy_export` | detail/export candidates with business-required identifiers; DA owns privacy handling |
| `other` | no existing category fits |

Choose one primary category; put secondary concepts in tags. Never encode project mappings in this taxonomy.

## Analysis Types

| Value | Scope |
|---|---|
| `sample` | bounded sample rows |
| `detail_check` | row-level debugging |
| `aggregate_query` | aggregate analysis |
| `metric_validation` | metric definition/evidence validation |
| `anomaly_check` | abnormal-data investigation |
| `export_candidate` | export-oriented detail SQL |
| `dashboard_time_series` | trend-ready Dashboard table |
| `dashboard_table` | table/pivot-ready Dashboard output |
| `dashboard_metric_card` | metric-card source table |
| `dashboard_funnel` | funnel output |
| `dashboard_retention` | retention output |

## Change Types

Workspace:

- `new`
- `correction`
- `replacement`
- `superset`
- `parameter_refresh`
- `branch`
- `migration`

Workspace coverage:

- `same_contract`
- `strict_superset`
- `partial_overlap`
- `different_contract`
- `independent`
- `unknown`

Formal artifacts additionally use `clarification`, `refresh`, and `promotion` where supported by schema. Same-family changes advance the version and move prior current to history. Branch uses a new slug with `branch_of`.

## Formal Artifact State

- `current`: normal discovery/current contract.
- `history`: superseded by a newer same-family version.
- `archived`: retained for audit outside normal work.

Normal repository/search hides history unless explicitly requested.

## Reuse

`reusable=true` requires:

1. clear output contract;
2. explicit parameters;
3. linked business rules/source contracts;
4. recorded sources, metrics, dimensions, and grain;
5. no SQL-side de-identification; identifiers are included only when needed and left unchanged for DA;
6. current result/validation status appropriate to intended use.

`reuse_candidate` is deterministic shape analysis only. It never grants reusable status.

## Derived Output Kinds

`result_evidence | analysis_workbook | comparison_workbook | visualization | export | other`

Source kind:

`user_result | skill_generated`

Derived outputs bind to exact workspace query version and execution fingerprint. They are not SQL families or formal Dashboard assets.

## Intermediate Tables

Table type:

`intermediate | snapshot | lookup | mart | temp`

Lifecycle:

`session | artifact | project | persistent`

Registry metadata includes table name/type/materialization/lifecycle, purpose, grain, time grain, partitions, keys, sources, downstream artifacts, refresh, retention, fields, metrics, dimensions, availability, fallback, tags, and reuse notes.

Intermediate-table registration requires explicit acceptance. Complexity scoring may recommend one but cannot create it.

## Run Evidence

Run status:

- `passed`: matching result file and user confirmation.
- `proxy_verified`: matching proxy result with role/limitation/future-plan metadata.
- `skipped`: explicit unverified exception with reason, risk, and future plan.
- failure statuses as defined by the run schema.

Run evidence belongs to a specific query artifact/version and execution fingerprint. Replacing SQL requires new evidence for verified promotion.

## Discovery Order

1. Search query workspace for prior exploratory/history work.
2. Search formal repository/manifest for current reusable QUERY assets.
3. Search exact source logs, metrics, filters, categories, and tags.
4. Include history only for audit.
5. Update missing metadata on an existing asset instead of duplicating SQL.
