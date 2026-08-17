# Quality Gate

Apply the common checks, then only the section for the artifact being produced. Do not replay unrelated lifecycle gates.

## Contents

1. [Common](#common)
2. [Workspace Query](#workspace-query)
3. [Development Inspection](#development-inspection)
4. [Formal Query](#formal-query)
5. [Fast Formalization](#fast-formalization)
6. [Result Visualization](#result-visualization)
7. [SQL Review](#sql-review)
8. [Dashboard](#dashboard)
9. [Repository And Viewers](#repository-and-viewers)
10. [Project Health](#project-health)

## Common

- [ ] One route was inferred and is allowed by `operating-contract.md`.
- [ ] Asset-changing scripts received the verbatim `--user-request`.
- [ ] Project, dialect, engine, table profile, and time policy are known where required.
- [ ] QUERY/REVIEW/VALIDATION/DASHBOARD did not mutate canonical rules.
- [ ] Non-SKILL_EVOLUTION routes did not modify source/runtime skill files; non-RULES routes did not modify canonical rule assets.
- [ ] Any direct protected-path edit passed `write_scope_guard.py` for the selected route.
- [ ] Only confirmed active rules and hard constraints affected SQL.
- [ ] No live database execution/deployment was claimed.
- [ ] All persisted paths are project-relative.
- [ ] SQL matches the configured Hive or StarRocks dialect.
- [ ] Every TLOG scan has the project-required date/partition bounds.
- [ ] Every TLOG alias required by `time_integrity_policy_v1` filters paired service/client clocks to the same local date; a SELECT-only comparison does not count.
- [ ] Whole-day Demo SQL uses inclusive date-only `dtEventDate` bounds and no redundant event-time WHERE range.
- [ ] Partial-day/timestamp predicates exist only when the business requirement needs them.
- [ ] No unsafe params aliases, fixed-midnight concatenation, same-block DISTINCT+GROUP BY, or unsupported string aggregation.
- [ ] Effective `identifier_policy` fields preserve exact casing and are backtick-quoted; TDBank Hive SQL never exposes bare/lowercased `dtEventTime`.
- [ ] SQL contains no MD5/SHA/HASH/BASE64/AES/MASK de-identification; business-required identifiers remain unchanged for DA-side privacy handling.
- [ ] Ratios use correct-grain numerator/denominator and zero handling.
- [ ] JOIN keys and input grain prevent amplification/crossing.
- [ ] No blocker was converted into a warning merely to produce an artifact.

## Workspace Query

- [ ] Candidate was written under project `query_workspace/_working/` or imported as immutable external SQL.
- [ ] No durable SQL was left in `_scratch`, `work`, `_work`, or `draft`.
- [ ] SQL is directly runnable with literal values, normally through top `params AS (...)`.
- [ ] A fixed or runtime window that can include today exposes an observed date/time field or explicit actual-range pair; requested params and execution time are not evidence.
- [ ] Output is compact; detail checks have safe fields and a LIMIT where appropriate.
- [ ] `sql_query_workspace.py save` created immutable SQL, meta, optional seed, JSON/Markdown/HTML indexes, and fingerprint.
- [ ] Purpose clearly says what the query calculates.
- [ ] Every grouped metric SQL has `summary_feasibility_v1`; composability was judged at the target metric grain, not guessed from displayed rows.
- [ ] Exact means/rates use unrounded numerator and weight/denominator fields; overlapping distincts, bucket-source statistics, and percentiles use a linked overall SQL.
- [ ] A `grouped_plus_overall` plan has two separate query families, one `query_analysis_bundle_v1`, matching params/sources/Base filters/metric fingerprint, and ready receipts for both SQL files.
- [ ] Delivered path has `generation_gate.status=ok` and `delivery_ready=true`.
- [ ] `sql_query_workspace.py receipt` returned ready, and the final response includes its clickable absolute SQL file path.
- [ ] Exact duplicate reused the existing version; identical SQL did not create a fake branch.
- [ ] Correction/replacement/parameter refresh or same-Base strict superset used `v002+` in one family; only independently useful Base/grain/use-case changes branched.
- [ ] The family has exactly one current version and a useful revision note.
- [ ] Supplied results and generated Excel/comparison/visualization files were attached to the exact SQL version and fingerprint.
- [ ] User was asked to run the exact indexed version and report concrete feedback.
- [ ] Failed/discarded/archived work remains searchable but is not described as verified or formal.
- [ ] A user-confirmed temporary rule exception records conflict reasons and follow-up route, warns once, and leaves skill source/canonical rules unchanged.

## Development Inspection

- [ ] Existing local inspection history was searched before repeating the same table/field diagnostic.
- [ ] Production business-scope predicates were not copied into development SQL; configured implicit development scope was represented as environment identity, not a physical WHERE filter.
- [ ] Every executed check produced local `query.sql`, `result.csv`, and `dev_sql_inspection_receipt_v2` evidence.
- [ ] The v2 subject records inspected tables/field/date range/filters where deterministically recoverable.
- [ ] Identifier-like enum values are absent from index previews even when explicit local enumeration was allowed.
- [ ] `dev_sql_inspection_index_v2` is current and stores project-relative evidence paths, latest-subject markers, and exact-duplicate diagnostics.
- [ ] Observed values were not promoted into knowledge or canonical rules without a separate explicit request.

## Formal Query

- [ ] User confirmed future retention value.
- [ ] Formal QUERY has valid `origin_query_workspace` lineage.
- [ ] Formalization returned ready `formal_sql_delivery_receipt_v1`, and every created SQL file is linked in the final response.
- [ ] SQL has a short `@SQL_QUERY_HEADER`; complete contract is in sibling `vNNN.spec.json`.
- [ ] Executable body begins with top params CTE; date/zone literals are not scattered in WHERE.
- [ ] No formal `SELECT *`.
- [ ] Performance preflight ran; full guide loaded only for L2/L3 or explicit deep optimization.
- [ ] Every optimization preserves numerator, denominator, grain, time window, CASE priority, and JOIN meaning.
- [ ] Repository summary has useful purpose, Base, metric groups, dimensions, concrete filters, original logs, applied criteria, and result evidence.
- [ ] Product summary contains no empty “需确认分子/分母” placeholder.
- [ ] Spec/meta/manifest store generation provenance and current skill version.

## Fast Formalization

- [ ] Already-run SQL plus `.csv`/`.xlsx` used `sql_formalize.py`, not raw batch review.
- [ ] Result schema, samples, row count, display rules, and optional retained fields were inspected once.
- [ ] `result_time_coverage_v1` records the observed range and precision; missing or anomalous today coverage blocks confirmation and verified formalization.
- [ ] Output mismatch stopped before rule/performance/semantic/Dashboard/save work.
- [ ] Formal rule-context and performance ran once or were reused only with valid fingerprints.
- [ ] QUERY/run/validation/optional DASHBOARD consumed one FormalizeBundle.
- [ ] Query-only target skipped validation and Dashboard work.
- [ ] Artifact write plan/result explains save/reuse/skip actions.
- [ ] Manifest/index updated once; viewer refresh mode was deliberate.
- [ ] No partial formal files were written after a blocker.

## Result Visualization

- [ ] The user returned a result for one exact SQL version, or exact grouped/overall results for one analysis bundle, and explicitly requested visualization.
- [ ] The result was not treated as user-confirmed unless the user separately confirmed correctness.
- [ ] The Spreadsheets skill created and visually rendered every sheet of a reusable `.xlsx`.
- [ ] The workbook contains source data plus at least one decision-useful native Excel chart.
- [ ] Every grouped table has one metric-aware bottom summary: additive metrics use `合计`, ratios are recomputed, means use valid weights, distinct/percentile metrics use source-level evidence, and normalized distributions never use a useless `100%` row as the summary.
- [ ] Percentage/rate cells use a restrained shared-scale color scale; comparable non-negative magnitude cells use solid, borderless data bars with visible values. Both preserve natural order, exclude headers/summary rows, avoid per-column autoscaling, and never overlap on the same cells.
- [ ] Source sheets, workbook/chart titles, evidence bands, metrics, and summary rows remain unmerged; only validated parent-group labels or nested table-header groups inside presentation tables use merges, while cross-column titles use `Center Across Selection` or continuous styling.
- [ ] The institutional role palette is consistent across sheets: one primary emphasis, neutral context, darkest total, and semantic colors only for semantic states; no equal-weight rainbow series remain.
- [ ] Workbook/chart titles literally identify the analytical object or metric × dimension; conclusions appear only as optional `观察` annotations. Unit/Base/window, source, as-of date, and applicable footnotes remain nearby.
- [ ] Every chart title is non-overlaid and has reserved space above the plot. Normalized stacked compositions reconcile to `100%` and use `percentStacked` or an explicit `0..1` percentage value axis; automatic `120%` endpoints are absent.
- [ ] If any exact result contains percentile/quantile fields, the union of plotted chart series/categories contains every available point; P50-only selection, title/note-only mentions, and source-sheet-only fields are absent.
- [ ] Every ratio/rate or normalized distribution result exposes an explicit same-grain Base/denominator field; a missing Base was repaired in QUERY rather than invented in Excel.
- [ ] A varying Base is visible beside the chart as an adjacent column/label at the same cohort/date/bucket grain; only one proven common Base is shown in a subtitle. Source-sheet-only or distant-note Base is absent.
- [ ] `validate_style.py` passed against every exact result before rendered QA, final binding reran the same audit, and `base_coverage` retains manual checks for exact denominator, filter/grain alignment, and small-sample policy.
- [ ] The final exported viewport was inspected at normal zoom for title/legend/mark collisions, clipped labels, semantic bucket order, correct series/category orientation, and chart ranges that exclude unintended summary rows.
- [ ] `bind` attached a single workbook to one exact result, or `attach-bundle-result` + `bind-bundle` attached one workbook to every exact grouped/overall result with `lineage_status=exact_results`.
- [ ] A bundle workbook contains separate `分组结果` and `整体结果` source sheets; one missing result leaves the bundle incomplete instead of producing a partial final workbook.
- [ ] Raw result retention follows the 10 MB rule; the visual workbook is retained in full.
- [ ] A ready `sql_result_visualization_receipt_v1` or `sql_result_visualization_bundle_receipt_v1` returned every clickable SQL, result-preview, and workbook path.
- [ ] No SQL version, formalization state, or Dashboard state changed because of post-processing alone.

## SQL Review

- [ ] Review used the `REVIEW` route and correct project roles.
- [ ] Product View and Code View are separate.
- [ ] Product View explains business question, Base, metric groups/cards, numerator/denominator or statistical object, dedup/grain, event contracts, concrete IDs/ranges/mappings, risks, and decisions.
- [ ] Product prose uses `LogName【XML中文名】`, not aliases/CTEs/physical tables as the main explanation.
- [ ] Code View keeps tables, CTE/lineage, expressions, partitions, dialect, result alignment, privacy, performance, and rule trace.
- [ ] Funnel/distribution/retention/duration SQL exposes its pattern-specific judgement points.
- [ ] Saved-rule matches require SQL evidence; weak/reverse/shared-log diagnostics are not displayed as used criteria.
- [ ] Normal Product View used LLM semantic closure; evidence-only debug did not masquerade as complete review.
- [ ] Proxy evidence was not called target verified.
- [ ] Result files were paired and columns/samples/row count were checked.

## Dashboard

- [ ] Source QUERY, run evidence, and validation are linked.
- [ ] Verification is correctly `verified`, `proxy_verified`, or `unverified_skipped_run`.
- [ ] Verified output has passed user-confirmed real result evidence.
- [ ] Proxy/unverified output records limitations/risk and future target verification.
- [ ] Grain, metrics, dimensions are locked and confidence meets promotion policy.
- [ ] SQL uses a short Dashboard header plus machine-readable sidecar.
- [ ] Final aliases, expected fields, table fields, and active filter output fields are stable Chinese names and match exactly.
- [ ] `筛选项` contains only explicitly requested user-changeable controls.
- [ ] Fixed SQL filters, dimensions, buckets, and sort fields were not misdeclared as Dashboard filters.
- [ ] Output shape is SQL-declared; no external daily/total behavior was invented.
- [ ] Percentage/rate fields have persisted display rules with source scale and decimals.
- [ ] Dashboard SQL outputs a table dataset and leaves visualization/layout to DA.
- [ ] The final response links the saved Dashboard SQL file from the formal delivery receipt.

## Repository And Viewers

- [ ] `query_workspace/index.html` is the stable dynamic shell; `query_workspace_maintenance.py serve` loads one row per query family, one current answer, version evolution, exact-version result/derived files, and complete SQL on demand.
- [ ] Historical result organization is not inferred from names: every applied decision explains metric/filter/grain differences, coverage, transform safety, wrong-decision risk, recommendation, and the user's confirmation.
- [ ] An adopted grouped/overall bundle reuses exact existing result ids, persists `exact_results` plus one bundle reference on the reusable output, and updates the bundle and output metadata in one transaction.
- [ ] `sql_repository.html` lists only manifest-current formal QUERY assets.
- [ ] Linked Dashboard SQL appears only as a QUERY attachment.
- [ ] Raw `_review_inbox` and workspace rows are absent from the formal repository.
- [ ] Repository criteria show only actual SQL-used matched/conflict/manual/unique criteria.
- [ ] Original-log filters contain only XML-resolved `LogName【中文名】` values.
- [ ] Dashboard review shows DA contract summary, real/synthetic sample provenance, and durable approval state.
- [ ] Viewer builds read persisted facts and do not run LLM.

## Project Health

- [ ] `project_validate.py --format json --strict` was run when project assets changed.
- [ ] Workspace index/view pointers, schemas, SQL/meta/seed/derived files, fingerprints, one-current-family state, branch lineage, and formal links pass.
- [ ] Health reports active reusable outputs that are bound to one result while a related query also has result evidence as multi-result lineage candidates.
- [ ] No unmanaged scratch/work/draft SQL remains.
- [ ] Formal artifact registration, sidecars, headers, lineage, and evidence gates pass.
- [ ] Canonical rule concept keys and shared-log event signatures pass relevant checks.
- [ ] Skill scripts compile, tests pass, `quick_validate.py` passes with UTF-8, and `git diff --check` is clean.
- [ ] Runtime skill copy was synchronized and validated when source skill changed.
