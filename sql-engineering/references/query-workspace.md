# Query Workspace Contract

This reference defines query-family storage, immutable versions, statuses, and derived outputs. Lifecycle procedure is in `project-workflow.md`.

## Boundary

A query is deliverable only when its immutable SQL version is indexed under `query_workspace/`, its concise purpose is searchable, and `generation_gate.status=ok` with `delivery_ready=true`.

Delivery additionally requires a ready `query_delivery_receipt_v1`. Run `sql_query_workspace.py receipt` against the exact indexed version as the final QUERY step. The receipt verifies path, file existence, fingerprint, recorded gate, delivery state, and absence of SQL-side de-identification. The final response must link `absolute_path`; a SQL code block or verbal summary is not a file deliverable.

Workspace is lightweight project-local execution history, not the formal SQL repository or a shared Git surface. It includes temporary, failed, discarded, archived, and promoted versions and their local results/visualizations. The entire `sql-projects/<project>/query_workspace/` tree is Git-ignored and excluded from the cross-project asset catalog. It never enters `manifest.artifacts` until explicit formalization.

A clean clone may have manifest pointers but no local workspace directory. The first QUERY save initializes it. Project validation treats this as a valid uninitialized local state; when an index exists, all normal index, file, fingerprint, viewer, and lineage checks remain strict.

## Storage

```text
query_workspace/
├── index.json
├── index.md
├── index.html
├── organization.json
├── bundles/qab-<fingerprint>.json
├── _working/<family>/candidate.sql
└── YYYYMMDD/qw-.../
    ├── v001.sql
    ├── v001.meta.json
    ├── v001.formalize_seed.json
    └── outputs/v001/<derived-file>
```

`vNNN.meta.json` is the version fact source. `index.json` is a compact search projection: one current-family summary plus immutable `path`/`meta_path` pointers for each version. Load it through `sql_query_workspace.load_index()` or the local API when full version facts are needed. `organization.json` is an optional semantic overlay keyed by `query_id`; `_working` is mutable staging only; indexed `vNNN.sql` is immutable.

`query_workspace_index_v2` is written on the next normal workspace transaction. The loader still accepts legacy v1 indexes and hydrates v2 pointers from their meta sidecars. Do not copy request envelopes, rule applications, result lineage, generation gates, or full fact bundles back into the stored index; those belong to the version meta/seed.

`index.html` is a fixed versioned HTML/JS shell. Normal query saves update JSON and Markdown but do not embed every SQL body or rewrite the shell unless its viewer contract changed. Run `query_workspace_maintenance.py serve`; the page reads `/api/query-workspace` and loads one indexed SQL version on demand from `/api/query-workspace/sql`.

The formalize seed stores the shared SQL fact bundle, both fingerprints, project fingerprints, lightweight temporary diagnostics, `knowledge_usage_v1`, and any exact `knowledge_reference_v1` rows used by this version. In a dual-engine project it also stores `execution_route_v1`: selected profile, physical TLOG sources, partition policy, route reasons, structural complexity, and density/date amplification evidence. It is local resumable state, not validation, a formal spec, or a Git artifact. Pass resolver output through `sql_query_workspace.py save --knowledge-reference-file query_workspace/_working/knowledge_receipts/<receipt>.json`; the save gate validates the reference against the current project binding and persists only compact selection evidence, not lookup rows or intake paths. If active project knowledge was intentionally irrelevant, pass `--knowledge-usage not-used`; no active binding records `not_available` automatically.

Every newly generated grouped metric query also stores `summary_feasibility_v1`. `single_exact`, `single_with_components`, and `no_overall_needed` remain one query family. `grouped_plus_overall` creates two independently runnable families because their grains differ, then links their exact versions through `query_analysis_bundle_v1`. Both versions stay non-deliverable until the bundle verifies identical params, source tables, Base filters, and metric contract.

## Temporary Rule Override

A normal workspace query follows current canonical rules. If the user explicitly identifies the request as temporary SQL or explicitly confirms a one-query exception after seeing the conflict, save `temporary_rule_override_v1` in the version row, meta, formalize seed, and current family summary.

The contract stores the canonical conflict signature, affected rule/concept IDs, concrete conflict reasons, the current user confirmation, acknowledgement time, follow-up routes, and `formalization_blocked=true`. The first occurrence is `notification_status=new`; later versions of the same family with the same signature are `acknowledged` and do not repeat the warning. A changed conflict signature is new and must be shown once. This state never edits canonical rules or skill source and never downgrades project execution, privacy, correctness, or performance blockers.

## States

| State | Meaning | Deliverable |
|---|---|---|
| `draft` | Indexed but gate not complete | No |
| `runnable` | Current gate passed | Yes |
| `run_failed` | Exact version failed on user/platform | No |
| `result_confirmed` | User accepted matching result | Yes |
| `discarded` | No formal value; searchable history | No |
| `archived` | Migrated history without current gate/run proof | No |
| `superseded` | Replaced by a later version or explicit Package member | No |
| `promoted` | Linked to a Formal Asset Package | Yes; prefer formal copy for reuse |

Discarded and archived are states, not directories. Reactivation requires the current generation gate.

## Families And Versions

A family represents one analytical question and has exactly one current answer. Every physical version is immutable.

| Change | Decision | Metadata |
|---|---|---|
| Fix field/predicate/JOIN/dedup/formula/filter | Same `query_id`, next version | `correction` or `replacement`; `same_contract` |
| Add outputs while fully retaining old Base/grain/use case | Same `query_id`, next version | `superset`; `strict_superset` |
| Change only parameter values | Same `query_id`, next version unless exact duplicate | `parameter_refresh`; `same_contract` |
| Change Base/grain/question/use case and both remain useful | New linked family | `branch`; overlap/different/independent |

Exact execution fingerprints deduplicate. Identical SQL cannot create a branch. A logic fingerprint groups date-refresh-equivalent semantics but never deduplicates exact runnable versions or result evidence.

Do not default to “all in one”. A complete same-contract superset replaces the current family answer; orthogonal or independently useful logic branches.

Every non-initial version records a concise revision note and previous/next links.

## Indexed Facts

The stored family row records only search and navigation fields:

- title, purpose, business question;
- `workspace_role` (`query`, `dashboard_delivery`, or `unknown`) and explicit role lineage;
- category, analysis type, `usage_class`, physical source logs/tables;
- metrics, dimensions, concrete filters, grain, tags;
- `sql_fingerprint`/execution fingerprint and `logic_fingerprint`;
- current status/change relation, derived-output count, and formal links;
- version identity and `path`/`meta_path` pointers.

The pointed `vNNN.meta.json` stores generation gate, request/rule/knowledge evidence, params, lifecycle history, provenance, exact derived outputs, and the rest of the version contract. `vNNN.formalize_seed.json` stores resumable deterministic facts for formalization. Consumers must hydrate instead of treating the compact JSON as a second facts document.

This is an index, not a product review. Complete product semantics belong to formal repository summary or SQL Review.

`usage_class` records SQL value independently from execution state and attached outputs. Pass it on save when the intent is clear:

```powershell
python scripts/sql_query_workspace.py save `
  --root <project-root> --sql-file <candidate.sql> `
  --title "<title>" --purpose "<purpose>" `
  --usage-class personal_diagnosis|reusable_diagnostic|ad_hoc_analysis|reusable_analysis|recurring_delivery
```

A revision inherits its query family's value when omitted. Legacy families remain `unclassified`; do not infer `reusable_analysis` merely because they have a confirmed result or a polished workbook.

`workspace_role` is a separate fact. New Dashboard Delivery SQL is saved in the same Workspace with `--workspace-role dashboard_delivery --source-query-id <qw-id> --source-query-version <n>`. It is never inferred from a `dashboard_sql/` directory or from `usage_class`.

## Diagnostic First Pass

For `personal_diagnosis` and `reusable_diagnostic`, select the class before generating SQL. The initial version is a bounded evidence query, not a minimal answer query:

- Narrow execution by exact subject and explicit time/partition bounds.
- Cover the plausible event, state, result, and correlation paths already supported by the current request, source contracts, and active knowledge.
- Return explicit event/source labels, timestamps, raw discriminator values, before/after values, and useful correlation identifiers so the result can be filtered locally.
- Keep uncertain state/result/time relationships visible as rows, flags, or time deltas instead of removing them with an early predicate.
- State covered evidence families and named deferments in the indexed purpose or SQL header so later readers know what the first pass did not inspect.

A second SQL in the same diagnostic family should mean the first version failed to execute, returned evidence that makes a genuinely new source/field relevant, or the investigation question changed. It should not exist only because the first version omitted an obvious adjacent state, result code, event source, or correlation field. Corrections still use immutable next versions; this rule changes generation quality, not version history.

## Derived Outputs

Attach post-query files to the exact query version:

- `result_evidence`;
- `analysis_workbook`;
- `comparison_workbook`;
- `visualization`;
- `export` or `other`.

Each attachment stores project-relative path, content hash, media type, source kind, source execution fingerprint, result-level lineage, lifecycle state, provenance, and optional related query versions. New reusable visual outputs store canonical `source_results`; ordinary binding uses one result from the exact SQL version, while explicit historical organization may bind multiple results or a user-confirmed deterministic transform.

Active outputs appear in the main viewer. `superseded` and `discarded` outputs remain traceable in a folded history section with reasons and replacement links. Historical organization requires a decision packet that explains concrete semantic differences; attachment names alone are never a valid migration basis.

Result files and reusable outputs have different retention contracts:

- `result_evidence` exists only to show what the SQL returned. At or below 10 MB it is retained in full; above 10 MB the managed asset keeps only a verified head/tail slice plus original size/hash, columns, row count, and sampling method.
- `analysis_workbook`, `comparison_workbook`, and `visualization` are reusable analytical products. Preserve their complete files regardless of size; never slice them under the result-evidence rule.
- `scan` separately reports missing retention metadata and stored-file fingerprint drift. It streams each file for SHA-256 and reports the old/new hash and size; setting an Excel number format is not content acceptance.
- `compact` backfills missing contracts and replaces oversized result previews, but writes only versions that actually changed. Existing retained outputs and their version meta remain byte-for-byte untouched.
- A user may edit a reusable workbook or visualization in place. Accept it only with `refresh`, a concrete reason, and the verbatim confirming request; refresh records `derived_output_content_revision_v1`, changes its content-addressed attachment ID, and updates references. Raw `result_evidence` is immutable and cannot be refreshed in place.

Changing SQL creates a SQL version. Changing only workbook layout/comparison/visualization creates another attachment on the same version.

For a single-result visualization, use `sql_result_visualization.py bind` instead of two independent `attach-output` calls. For a grouped/overall bundle, attach each result with `attach-bundle-result`; the first remains `awaiting_other_result`, and only complete bundles may use `bind-bundle`. The combined workbook is stored once with `lineage_status=exact_results` and references both exact SQL/result fingerprints.

```powershell
python scripts/sql_query_workspace.py attach-output `
  --root <project-root> --query-id <qw-id> --version <n> `
  --file <file> --kind <kind> --source-kind user_result|skill_generated `
  --title "<title>" --purpose "<purpose>" --user-request "<request>"
```

Periodic result-payload maintenance:

```powershell
python scripts/result_evidence_maintenance.py scan --root <project-root>
python scripts/result_evidence_maintenance.py compact `
  --root <project-root> --function-selection RESULT_EVIDENCE_MAINTENANCE `
  --user-request "整理项目结果证据"

python scripts/result_evidence_maintenance.py refresh `
  --root <project-root> --attachment-id <qwo-id> `
  --reason "用户调整了工作簿格式" `
  --function-selection RESULT_EVIDENCE_MAINTENANCE `
  --user-request "<用户确认接受修改后文件的原话>"
```

## External SQL

External SQL is immutable input. `sql_query_workspace.py import` copies an immutable snapshot and returns a project-local working copy. Persist original name and hashes, never Downloads/OneDrive/drive/UNC paths. Save every correction as another indexed version.

## Legacy Migration

Use `migrate_legacy_sql_work.py` for old project-local scratch/work SQL:

1. Dry-run and inspect planned family/title/purpose/facts/duplicates.
2. Write as `archived`, not runnable or verified.
3. Verify indexed normalized fingerprint.
4. Remove each source only after verified copy.

Formal QUERY lineage backfill uses `migrate_query_workspace.py`; it does not ingest scratch files.

## Discovery

Start the local dynamic viewer or use CLI search:

```powershell
python scripts/query_workspace_maintenance.py serve --root <project-root>
```

```powershell
python scripts/sql_query_workspace.py search --root <project-root> --query "<purpose log metric filter>"
```

Search workspace before rebuilding an experiment. Search the formal repository for approved reusable assets.

Final delivery receipt:

```powershell
python scripts/sql_query_workspace.py receipt `
  --root <project-root> --query-id <qw-id> --version <n> --format json
```

Only `status=ready` may be delivered. `delivery_file` is output-only and is never persisted into project JSON.

## Periodic Curation

Use the dedicated maintenance capability when the workspace has accumulated enough history to justify slower semantic organization:

```powershell
python scripts/query_workspace_maintenance.py scan --root <project-root> --format json

python scripts/query_workspace_maintenance.py apply `
  --root <project-root> --decisions-file <reviewed-decisions.json> `
  --user-request "<verbatim request>" `
  --function-selection QUERY_WORKSPACE_MAINTENANCE
```

`scan` is deterministic and read-only. It reports missing index/files, same-logic and near-duplicate query families, result/formal evidence, open temporary-rule governance feedback, and a conservative curation seed. It uses `usage_class` when present: personal completed checks become discard candidates, reusable diagnostics/analysis/delivery become reuse candidates, and unclassified confirmed results remain neutral. The feedback list is the later RULES/SKILL_EVOLUTION inbox; it is not permission for QUERY to repair source files. The LLM or human reviewer writes business topics, concise summaries, confidence, related-query links, and curation suggestions to a decisions file. `apply` validates those decisions and writes only `organization.json` plus the stable viewer shell.

The overlay may mark `reusable_candidate`, `keep_history`, `discard_candidate`, `needs_summary`, `duplicate_candidate`, or `reviewed`. These are curation labels, not lifecycle states. A later explicit QUERY/SQL_FORMALIZE action performs any real discard, correction, merge, or promotion after current gates pass.

## Health

Validation checks index/meta/seed paths, both fingerprints, duplicate integrity, current family consistency, branch lineage, derived file hashes, promotion links, relative provenance, delivery gates, and unmanaged scratch/work SQL. Persistence failure blocks delivery.
