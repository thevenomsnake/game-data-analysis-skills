# Local Project Workflow

This is the sole authority for SQL lifecycle transitions. Other references define local data or review contracts but must not create another lifecycle.

## Architecture

```text
sql-projects/<project>/
├── project_config.json
├── data_services.json
├── manifest.json
├── rules/store.json
├── rules/activation-index.json
├── rules/definitions/<concept_key>/vNNN.json
├── sources/
├── knowledge/bindings.json
├── dev_inspections/YYYYMMDD/<inspection-id>/query.sql|result.csv|receipt.json
├── dev_inspections/index.json
├── query_workspace/
│   ├── index.json|md|html
│   ├── organization.json
│   ├── promotion_ledger.json
│   ├── _working/
│   └── YYYYMMDD/qw-.../vNNN.sql|meta.json|formalize_seed.json|outputs/
├── formal_assets/
│   ├── index.json
│   ├── migration-map.v1.json
│   └── FA-NNNN-<slug>/
│       ├── manifest.json
│       ├── manifests/RNNNN.json
│       ├── receipts/RNNNN.json
│       └── members/queries|evidence|validations|dashboards|outputs|history/
├── intermediate_tables/
└── reviews/sql_repository.html|dashboard_review.html
```

Raw review input lives at `sql-projects/_review_inbox/<project>/<batch>/`. Review does not make it formal.

Global immutable config-table snapshots, projections, and usage contracts live under `knowledge-base/`; project folders store only the active binding.

Reusable physical data services live in `sql-projects/_data_services/catalog.json`. Each project
folder is one explicit product-stage context and owns `data_services.json`; a new stage begins
unbound and may copy a same-product service target only after user confirmation. Service reuse
never transfers planning releases, rules, mappings, SQL, results, or lifecycle state.

Development inspection evidence is a local, ignored observation layer. Search its v2 index before repeating a schema, enum, or bounded diagnostic query. Observations never become project knowledge or canonical rules automatically; promotion requires the corresponding explicit workflow.

Query workspace is also a local, ignored layer. It persists SQL history, results, visualizations, metadata, build state, and content-bound Promotion Ledger decisions on this machine, but it is never Git-tracked or projected into the shared cross-project catalog. A clean clone initializes it on the first QUERY save. Unregistered local files are neither moved nor copied; scans ignore them, and unchanged indexed candidates with a Ledger decision are skipped.

`formal_assets/` is the only shared formal store. Each immutable `FA-NNNN` Package owns its members, current pointers, lineage, manifest history, and repository receipts. `query_sql/`, `dashboard_sql/`, `validations/`, `runs/`, and physical `archive/` are migration inputs only and must never receive new writes.

The cross-project read-only index lives at `sql-projects/_asset_catalog/asset_catalog.json`. Its semantic navigation overlay is `asset_organization.json`, and its persistent analytical directory is `asset_group_registry.json`. The registry assigns immutable `AG-NNNN` group IDs while SQL keeps its separate `vNNN` version namespace. These are independent periodic maintenance outputs and never implicit steps in QUERY, formalization, Review, Validation, Dashboard, repository, or ordinary saves.

## Lifecycle

```text
requirements + project rules/sources
  -> QUERY_WORKSPACE candidate
  -> indexed runnable SQL version
  -> ready query_delivery_receipt_v1
  -> configured Chrome execution OR user run
  -> failure/result evidence
  -> discard/keep local OR Promotion Plan
  -> confirmed Formal Asset Package + repository receipt
  -> optional validation + Dashboard Delivery in the same Package
```

No temporary Dashboard lane exists. Dashboard is created only as a formal derivative of a retained query after evidence gates pass.

Result visualization is orthogonal to this promotion path. A temporary SQL, formal QUERY, or DASHBOARD SQL may each produce a result and visual Excel without changing lifecycle state. When browser execution is enabled, chartable returned results enter this same visualization capability automatically; unchartable results retain their exact CSV evidence and a skip reason.

QUERY and Dashboard are separate executable stages even when they answer the
same business question. Formalization checks the normalized QUERY with
`lifecycle_stage=retained_query`, then checks the actual Dashboard candidate
with `lifecycle_stage=dashboard_delivery`. If delivery must replace an
inaccessible/proxy source with a DA-maintained authoritative table, pass that
candidate through `sql_formalize.py --dashboard-sql-file`; do not rewrite the
retained QUERY or weaken its evidence record.

## Project Setup

Formal work requires:

- `sql_dialect`, `query_engine`, and `query_environment`;
- table naming profile and strict partition/time policy;
- `dashboard_application` before Dashboard generation.
- explicit product/stage data-service status for development inspection and production execution.

Use `sql_project.py init/show-config/set-config/resolve-table` for SQL config and `data_service.py`
for service status/binding/resolution. Do not infer one stage, service target, or execution chain
from another.

Project business truth lives in canonical rules; static mappings and config-table facts live in active knowledge bindings; physical source availability lives in source contracts. External partner/DA table evidence is copied into `sources/` with project-relative paths and a `*.schema.json` contract. Temporary proxies stay proxy evidence; verified formal delivery uses the authoritative source or an explicit proxy/unverified contract.

## Shared Facts

Every new workspace SQL version stores a `sql_fact_bundle_v3`, `request_envelope_v1`, and `rule_application_v1` in its metadata and adjacent formalize seed. The request envelope is the verbatim current message; the application records current, inherited, and excluded rules separately.

- Workspace uses it for search/index metadata.
- Formalize builds one current bundle and reuses logic-matched analysis/semantics.
- SQL Review uses the same base source/final field facts, then adds review-only evidence and judgement.
- Formal QUERY sidecars persist `formalize_bundle.sql_facts` for repository/viewer reuse.
- Every SQL version declares `knowledge_usage_v1`. A config-table lookup persists `used` plus exact `knowledge_reference_v1`; active-but-unused knowledge requires `--knowledge-usage not-used`; projects without active bindings record `not_available` automatically.

Execution-sensitive rule, time, performance, result, and delivery gates always evaluate the current SQL even when semantics are reused.

## Config Knowledge Loop

Use `config_knowledge.py` only for an explicit knowledge-management request. Register/refresh creates immutable source and projection versions; it does not activate them. Review `diff.json`, then bind one exact version to a project. QUERY/Review resolve only that binding and never reopen Excel. See `knowledge-management.md` for contracts and commands.

## Temporary Query Loop

Canonical rules remain strict by default. Only when the current verbatim user message explicitly scopes the exception to this query or confirms a one-query override may the current instruction override a conflicting canonical business rule. Store a `temporary_rule_override_v1` in the exact workspace version, notify once for each conflict signature in that family, and leave canonical rules and skill source untouched. Project execution, privacy, correctness, and performance blockers never downgrade. An unresolved override cannot enter formalization.

Choose `usage_class` before generation. Analytical queries optimize a concise decision output; `personal_diagnosis` and `reusable_diagnostic` optimize first-pass hypothesis coverage inside a narrow execution scope. For diagnostics, use **范围窄，证据宽**:

- Bind the exact player/account/role/session/order or incident key and an explicit bounded time/partition range. This is the performance boundary.
- Before writing SQL, enumerate the plausible paths supported by the symptom and available contracts: triggering event, prerequisite state, state transitions, downstream result, delayed/out-of-order reporting, correlation failure, and nearby session/battle context. Query only semantically relevant sources.
- Preserve event time, source/event name, subject key, raw state/result/change/error values, before/after values, and available session/battle/server/mode correlation keys. Use explicit columns, never `SELECT *`.
- Prefer per-source bounded CTEs plus one normalized `UNION ALL` event timeline when the evidence can share a useful shape. Keep source-specific discriminator fields even when some rows are null.
- Do not turn an unproven hypothesis into an early filter. If several states or result codes are plausible, return and label them; do not keep only one guessed value in `WHERE`. If exact-time equality is not a confirmed contract, expose candidate matches and time deltas or use a bounded nearest-event rule instead of silently discarding non-equal events.
- Record any relevant source or field intentionally deferred and the concrete cost, access, or missing-relationship reason. “Keep the first SQL small” is not a reason.

The first result should normally be sufficient for local filtering and diagnosis. Generate a later SQL only after an execution failure, genuinely new evidence that makes another source/field relevant, or a changed investigation question. Do not create another version merely to add obvious context omitted from the first pass.

1. Run `requirement_intake.py` with the original request and project root before SQL generation. It resolves generic slots and rule-declared business decisions. If `business_decisions.status=needs_input`, ask the returned question and stop; rerun with the same original request plus `--clarification-text` after the user answers. For duration rules that require `mode_scope`, bare “常规” resolves to the configured regular scope including activity, while “纯常规/仅常规/不含活动” remains regular-only. Then read project config, relevant confirmed rules, source contracts, active knowledge bindings, and the selected dialect. If the user omitted dates, resolve the project's fixed start-through-yesterday QUERY window before generation; stop and ask only when that project default is unavailable.
2. If the project has execution adapters, write business logic once with portable TLOG tokens, run `sql_execution_adapter.py render`, and load only the selected dialect. Omitted/`auto` profile selection uses the configured StarRocks default; pass a Hive profile only when the user explicitly requests Hive. Rendering also applies the selected profile's exact-case `identifier_policy`. Do not create separate SR/Hive business SQL versions.
3. Run rule context with the current user request as the only forward text. Persist exact request quotes for applied optional rules. Candidate SQL is reverse validation only.
4. Select `usage_class`, apply the diagnostic or analytical generation objective above, and generate one directly runnable file under `query_workspace/_working/`.
5. Use a top params CTE and the selected execution profile's time contract. Persist the resolved route and `pt_start`/`pt_end` literals so the saved version is reproducible; do not leave a runtime-relative yesterday expression.
6. Apply the selected profile's `time_integrity_policy_v1`. Use the exact fields declared by the configured project profile; do not hand-copy one predicate across aliases. Read `references/time-integrity.md` for the result coverage contract.
7. Run minimal dialect, compatibility, SQL-side de-identification, JOIN, ratio, and performance checks. Business-required identifiers remain unchanged; DA owns privacy.
8. If the final output is grouped, run `sql_summary_planner.py plan`. Keep one SQL when every useful overall statistic is exact from additive rows or unrounded support components. When routing is `grouped_plus_overall`, save separate grouped and overall query families and run `create-bundle`; do not deliver either member before both receipts are ready.
9. Save with `sql_query_workspace.py save --status runnable --usage-class <class>`, passing the class selected before generation plus `--summary-plan-file` and `--analysis-role` for grouped analysis. SQL value remains independent from execution/result state; use `unclassified` only when the request genuinely does not resolve it.
10. Run `sql_query_workspace.py receipt` on every exact saved version. Do not finish unless every required `query_delivery_receipt_v1` is ready.
11. Return clickable links to all required `vNNN.sql` paths plus concise role/purpose/status summaries. If the user explicitly selects a configured browser adapter, route each exact ready version to `QUERY_EXECUTE`; otherwise use a local DB-API/CLI adapter or manual handoff. Never paste SQL as the only deliverable.
12. For `QUERY_EXECUTE`, use only the Chrome plugin and the user's own authenticated session. Check the configured root URL, keep at most one agent-created query tab active, fill the receipt-backed SQL, submit once, wait for a terminal state, and move the returned file into the exact version's managed outputs. Never use the Windows App built-in browser, automate credentials, or resubmit after an uncertain post-submit state.
13. After the download is moved and terminal/task identity is captured, close agent-created query and extraction tabs before local result attachment, visualization, or the next SQL. Never close a user-owned pre-existing tab. An unresolved authentication/download handoff stops the serial queue.
14. Attach each returned result to its exact version. The attachment computes `result_time_coverage_v1` in the same evidence pass; a today result with missing or anomalous coverage is retained as evidence but cannot transition to `result_confirmed`. Date-only coverage is valid with `precision=date` and never implies an intraday cutoff.
15. After any successful result attachment, route presentation by `usage_class` unless the user explicitly chooses a format: personal/reusable diagnostics default to self-contained HTML; ad hoc/reusable analysis and recurring delivery default to Excel. Diagnostic HTML may use a timeline, state transitions, differences, and evidence tables without a traditional chart. Empty results, explicit opt-out, or disabled project policy record `visualization_skipped` with a reason.
16. At full `completed`, leave no agent-created browser page open. Close a recovered handoff before continuing the serial queue. A final local diagnostic HTML may remain as a normal deliverable.
17. Save corrections/replacements/refreshes/supersets as immutable later versions of the same family. Only `correction` and `parameter_refresh` with `same_contract` may inherit the prior structured rule application; replacement, superset, and branch reevaluate from the current request.
18. Decide SQL retention from `usage_class`, never from result or workbook existence: completed personal diagnosis normally becomes `discarded`; reusable diagnostics remain searchable in Workspace; ad hoc analysis preserves valuable result/visual lineage without automatic formalization; reusable analysis may become formal QUERY; recurring delivery may become formal QUERY plus Dashboard.
19. If the exception reveals a bad canonical rule or bad skill matching/guard behavior, keep its reasons in the override feedback. Resolve it later through a separate RULES or SKILL_EVOLUTION task; never patch Python or rules inside the QUERY loop.

External SQL must be imported before modification. Legacy scratch/work SQL uses `migrate_legacy_sql_work.py`: dry-run first, then write; migrated unknown history is `archived`.

## Fast Formalization

Use `sql_formalize.py` when the user supplies already-run SQL plus real `.csv`/`.xlsx` evidence and asks to save or create a Dashboard. Do not run raw batch review first.

```powershell
python scripts/sql_formalize.py `
  --root <project-root> --source-sql <sql> --result-file <result.xlsx> `
  --dashboard-sql-file <optional-da-candidate.sql> `
  --target query-dashboard --title "<title>" --user-confirmed `
  --use-fact-bundle auto --refresh-viewers dynamic --format json `
  --user-request "<verbatim request>" --function-selection SQL_FORMALIZE
```

Transaction order:

1. Inspect result schema, samples, and `result_time_coverage_v1` once. If the fixed or runtime source window can include today, stop before writing formal assets when coverage is missing or anomalous.
2. Reuse/create the exact workspace source version.
3. Normalize params and retained output fields in staging.
4. Build one current fact bundle; reuse only fingerprint-safe facts.
5. Run retained-QUERY rule context and performance once. An exact workspace application may cross lifecycle by structured inheritance; title/purpose never substitute for it. If a Dashboard is requested, run a separate lightweight `dashboard_delivery` gate on the actual Dashboard SQL and inherit only when it is a same-contract derivative.
6. Stop before optional work on output/rule/performance/SQL-side-de-identification blockers.
7. Build QUERY, run evidence, optional validation/DASHBOARD, and repository summary from one bundle.
8. Preview and fingerprint-match Dashboard contract before writes.
9. Submit all staged members and lineage once to Formal Asset Repository; it atomically writes the Package manifest, immutable manifest snapshot, receipt, repository index, and the `project_manifest_v2` compact projection. A later receipt also enumerates every prior immutable manifest snapshot and receipt in that Package so Collaboration can prove the whole history from the latest receipt.
10. Mark workspace promoted only after the Package receipt validates.
11. Leave shared read models unchanged during ordinary formalization. A later explicit Collaboration Submit refreshes the project index, Catalog, Organization, AG Registry and Provider Snapshot before staging the Package closure.
12. Require `formal_asset_repository_receipt_v1.status=ready`; return the Package identity and clickable paths for every QUERY/VALIDATION/DASHBOARD member written by the transaction.

The real result file, optionally narrowed by `--retained-fields`, is the retained output contract. It drives final SELECT, expected fields, samples, repository metrics, validation, Dashboard fields, and display rules.

## Formal Asset Packages

Every formal SQL version remains a three-file member unit inside one Package:

```text
vNNN.sql
vNNN.spec.json
vNNN.meta.json
```

The Package manifest is authoritative for member identity, current/history/archived state, and lineage. Sidecar spec/meta remain authoritative for one SQL version's contract. Project `manifest.json`, Markdown indexes, Repository HTML/JSON, Catalog, Organization, and AG registry are compact or rebuildable projections; none may write back into a Package.

Lifecycle promotion copies the exact indexed Workspace SQL, its `.meta.json`, an available `.formalize_seed.json` contract sidecar, and every registered output into the confirmed Package closure. Multiple explicitly selected candidates may target the same `--package-id`; pass `--package-title` for the shared analysis title. Updating one candidate preserves other query families already owned by that Package and advances only the selected family's superseded members to history.

Every generated SQL starts with the script-managed `@SQL_GENERATION` line containing Skill version and generator LDAP username. Short SQL headers remain human handoff only; complete machine contracts live in sidecars. QUERY headers explain business purpose/Base/metric scope. Dashboard headers declare DA interface fields, controls, date params, formatting, totals, source QUERY, and verification status.

Dashboard rules:

- Verified requires real result evidence, user confirmation, validation, confidence threshold, and no blocker.
- Proxy evidence remains `proxy_verified` with roles, limitations, and future target plan.
- `筛选项` means user-changeable Dashboard controls only.
- DA owns date range and realtime-refresh decision. SQL/spec owns output shape.
- Dashboard is linked to its source QUERY and appears as an attachment in the repository.

## Returned Result Visualization

When a successful result file is attached, default to `RESULT_VISUALIZATION`; an explicit request is not required. Route Bug diagnosis to self-contained HTML and traditional analysis to Excel from `usage_class`. Respect an explicit user format, opt-out, or disabled project policy. Do not rerun QUERY generation, SQL Review, formalization, or Dashboard creation.

1. Resolve one exact project-local SQL version and fingerprint, or one `query_analysis_bundle_v1` whose grouped/overall members are both exact indexed versions.
2. Treat the returned file as observed result evidence unless the user also confirms correctness.
3. When a managed prior visualization exists, run `refresh-values --dry-run`. Reuse it only for unchanged SQL logic and a same-shape exact source table; otherwise use the Spreadsheets skill to rebuild. Render-check the resulting `.xlsx` with at least one useful native chart.
4. For one result, run `sql_result_visualization.py bind`. For a bundle, run `attach-bundle-result` once per role, then `bind-bundle` after both results are attached.
5. Require a ready single-result or bundle receipt and return all SQL, retained result-preview, and workbook paths.

Workspace attachments remain local on the exact version and are visible only through the local workspace viewer. Formal QUERY/DASHBOARD attachments are added through Formal Asset Repository as result, run-record, and visualization members of the same Package; `sql_result_visualization.py` must not recreate `runs/`. Both layers retain result-level lineage in their own authority. See `result-visualization.md`.

Historical relationship cleanup is separate. Run `RESULT_LINEAGE_ORGANIZATION` only as an explicit maintenance task: inspect deterministic evidence once, explain metric/filter/grain differences and coverage to the user one case at a time, then apply a user-confirmed decision file. It may bind multiple results, adopt an existing grouped/overall SQL-result pair into a previously validated `query_analysis_bundle_v1`, record deterministic transforms, mark outputs superseded/discarded, or remove byte-identical duplicates. Bundle adoption updates bundle and output lineage in one transaction; it never reruns SQL, changes SQL versions, copies result files, or joins ordinary delivery latency. See `result-lineage-organization.md`.

Formalization output is not complete until its `formal_asset_repository_receipt_v1` verifies the complete Package closure and the final response links each requested member path.

## Review And Discovery

- Query workspace HTML finds temporary/history SQL and versions.
- SQL repository HTML reads one row per Formal Asset Package and expands its query, evidence, validation, output, and Dashboard members.
- Raw SQL Review judges inbox SQL through separate Product and Code views.
- Dashboard review discovers saved DA contracts from Package manifests and writes only its separate review state.

These viewers share deterministic facts but never scrape, promote, or mutate one another.

The query-workspace viewer is dynamic: `index.html` is a stable shell, while the local maintenance server reads the latest `index.json`, optional `organization.json`, and requested SQL version at request time. Periodic curation writes only the organization overlay. It cannot change status, current version, delivery readiness, SQL, or formal links.

Search workspace before reconstructing exploratory work. Search repository when the user needs a retained reusable asset.

Use `asset_catalog.py build` only as an explicit periodic task when another tool needs one structured inventory across shared project assets and lifecycle states. The catalog includes formal SQL, run evidence, reusable outputs, rules, knowledge, read models, platform manuals, and consumer contracts; it excludes all project-local query workspaces. State is a label for shared consumers, not a visibility or publication gate. The builder reads persisted shared indexes and documentation only during that explicit run, never invokes an LLM, and never mutates the indexed assets. Do not invoke it from daily SQL paths. See `asset-catalog.md` for the asset/file/relationship contract.

After semantic organization, run `asset_group_registry.py` as the final periodic directory step. One group represents one analytical question or explicit multi-query analysis bundle, not a broad business topic. Existing IDs never change; late historical imports receive the next sequence, and homepage reorder uses `display_order`. See `asset-groups.md`.

## Revision And Lineage

- Correction, replacement, parameter refresh, or same-contract strict superset: same family/slug, next immutable version.
- Independently useful Base/grain/question: linked branch.
- Formal correction adds an immutable member and advances Package lineage/current pointers; never delete historical versions.
- Results attach to one exact SQL version and execution fingerprint. Generated Excel/visualizations additionally attach to one exact result id, or every exact result in one grouped/overall analysis bundle. Raw result evidence is a preview contract: retain the full file only through 10 MB and slice anything larger. Generated analysis workbooks, comparisons, and visualizations are reusable assets and remain complete.
- Formal QUERY records `origin_query_workspace`; promotion links the local version to its Package/member path without copying the rest of Workspace.

## Rules And Intermediate Tables

Rules are edited only by explicit RULES workflow. Query/review/formalize/dashboard routes may report candidate or conflict evidence but cannot save rules.

Skill source is edited only by explicit SKILL_EVOLUTION workflow. SQL routes may report reproducible failures and persist governance feedback, but cannot modify scripts/tests/docs as part of producing one query.

Intermediate tables are registered only after explicit acceptance. Their metadata declares grain, fields, refresh/partition policy, lineage, availability, and fallback. Complexity alone creates a recommendation, not a table.

## Health

Daily delivery uses `python scripts/project_validate.py --root <project-root> --scope current --format summary --strict`. Release and migration audit use `--scope full --format json`. Current scope validates Package manifests, current member pointers, receipts, and each Workspace query family's current version while reporting excluded history counts separately; it does not declare historical debt resolved.

Health validates config, workspace index/meta/seed/fingerprints, unmanaged SQL, formal sidecars, lineage, run evidence, Dashboard gates, relative paths, and viewer contracts. A failed persistence or health gate blocks delivery; chat is never fallback storage.
