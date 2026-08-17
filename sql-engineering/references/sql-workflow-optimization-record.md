# SQL Workflow Optimization Record

## 2026-07-11: Separate matching mode from lifecycle source policy

Canonical rule matching mode and asset lifecycle stage are independent. A
single `FormalizeBundle` still avoids repeated QUERY analysis, but Dashboard SQL
is a distinct executable candidate whenever delivery changes its data source.
The fast path therefore runs one retained-QUERY rule gate and one Dashboard
delivery gate; this is intentional correctness work, not a return to repeated
full review.

Project-specific source substitutions belong in stage-scoped canonical
constraints (`applies_in`), never in generic Python branches. An explicit
`--dashboard-sql-file` carries the DA candidate when deterministic parameter
adaptation is insufficient. Query result evidence may support a
`proxy_verified` Dashboard only with the existing role/limitation/future-plan
contract; source requirements for `dashboard_delivery` are never downgraded.

This is the current architecture decision record, not a changelog. Replace superseded decisions instead of appending compatibility branches.

## Objective

Optimize wall-clock speed and human clarity while preserving formal quality:

1. Extract deterministic facts once.
2. Persist facts for the next stage.
3. Stop at the earliest blocker that later stages cannot repair.
4. Keep temporary work light and traceable.
5. Keep formal evidence, validation, and Dashboard gates strict.
6. Use LLM only for semantic judgement that deterministic code cannot provide.

## Ownership

| Stage | Owns | Does not own |
|---|---|---|
| Data-service catalog and stage binding | Reusable non-secret physical service definitions, explicit product/stage identity, purpose-specific confirmed targets, and local service-target probe cache | Planning releases, business semantics, automatic sibling inheritance, credentials in Git, or cross-environment fallback |
| Planning source space | Explicit local-user or remote-tool management mode, exact SVN revision manifests, embedded non-SVN folder releases, file-level diff, and project source binding | Scheduling, caller-specific integration, a second editable SVN mirror, Knowledge semantics, business rules, or direct SQL use |
| Local source workspace | Machine-local code/document/reference candidate roots, bounded discovery index, and exact selected-file hash evidence | Planning folders, Knowledge registration, project truth, SQL mappings, or Git publication |
| Project Knowledge binding | Exact active KDV/provenance plus deterministic compatibility impact for logical rule dependencies | Business formulas or canonical-rule version identity |
| Query workspace | Exact runnable/history SQL, family/version/state, concise purpose, SQL `usage_class`, shared facts, derived outputs | Formal specs or product review |
| User feedback | Exact run failure/result and retention decision | Rule mutation |
| Result evidence maintenance | Deterministic 10 MB result slicing, payload profiles, and reusable-output preservation | SQL changes, lifecycle changes, or semantic classification |
| Result visualization | Exact-result percentile/Base evidence completeness, one canonical workbook audit, and deterministic binding | Spreadsheet authoring runtime, inferred business denominator truth, or SQL repair |
| Fast formalize | One result-backed formal transaction and optional Dashboard derivative | Raw batch review |
| SQL Review | Product/code judgement of raw SQL | Formal save or canonical-rule writes |
| Formal repository | Persisted current QUERY discovery and copy | Re-analysis, approval, or LLM generation |
| All-status asset catalog | Cross-project identity, files, hashes, provenance, and relationships for shared managed assets across lifecycle states | Project-local workspace history, publication, approval, promotion, copying, semantic inference, or source-asset mutation |
| Stable asset groups | Immutable chronological identity and directory membership for one analytical question and its linked outputs | SQL versioning, topic inference, lifecycle mutation, or daily-save refresh |
| Dashboard review | Saved DA contract inspection and approval | Query inventory |
| Canonical Rules | Explicitly authorized durable business truth | Temporary assumptions |
| Skill evolution | Explicitly requested script/test/schema/reference changes | Repairing the skill opportunistically during QUERY |

`project-workflow.md` is the sole lifecycle procedure. Other references may define their local contract but must not restate stage transitions.

Planning-source provider and management ownership are independent. `user_managed` treats the local
source as user-authoritative without remote freshness checks; an older source is valid, and a clean
single revision is required only when explicitly sealing a new PSR. `tool_managed` selects remote
latest only when the standard `check` or `sync` operation is explicitly called. Neither mode owns
scheduling or a named consumer. Synchronization exports the selected committed revision into
repository-local staging and never copies local modifications. The PSR keeps a complete hash
manifest, exact SVN receipt, and revision-selection policy while omitting the exported file tree;
Knowledge materializes only its declared source file and preserves that byte sequence in the
existing immutable source snapshot. Non-SVN inputs retain the embedded complete-folder provider.

Result payloads and reusable analytical outputs have different value. A raw query result exists to prove schema, sample values, scale, and display behavior; retaining hundreds of megabytes adds transport cost without improving that review. The managed copy is therefore complete through 10 MB and becomes a verified head/tail slice above 10 MB. A chart, formatted analysis workbook, or comparison output contains reusable analytical work and remains complete regardless of size. This deterministic maintenance occurs once during attachment or periodic migration and is reused by formalize, repository, and external catalogs.

## Shared Fact Decision

`scripts/sql_facts.py` is the single deterministic SQL fact source. Workspace, formalize, semantic summary, SQL Review, performance routing, and repository hydration consume `sql_fact_bundle_v3` instead of maintaining separate parsers.

The bundle contains physical and referenced sources, CTE names, targets, XML logs, project external-source contracts, final fields, metric/dimension classification, concrete filters, params, analysis, final-output privacy, and performance structure.

CTE facts distinguish declaration from executor safety. The bundle records top-level dependency edges, longest dependency depth, final-query references, and reference spans. StarRocks delivery consumes a combined empirical guardrail; a declared CTE can still fail executor expansion, so local name resolution alone never proves delivery readiness.

Player identity is selected once as `subject_key_selection_v1`. Project config owns confirmed key uniqueness, the semantic default, and native event-role fields. Generation keeps the default when cost is equal, but a native event-role RoleID may replace `vOpenID` when it answers the same person-level question and removes a pure identity bridge. Facts record actual metric keys and optimization candidates; Review and formalize consume that decision instead of re-deriving identity from output labels.

Executable SQL never owns de-identification. The shared privacy fact records SQL-side hash/mask transforms as blockers, while business-required identifiers remain unchanged for DA-side privacy handling. Internal SHA-256 fingerprints remain metadata-only lineage evidence.

Two fingerprints have different responsibilities:

| Fingerprint | Includes | Use |
|---|---|---|
| `execution_fingerprint` | Exact normalized runnable SQL and all parameter values | Version dedup, result/run binding, rule/performance/time gates |
| `logic_fingerprint` | SQL logic with only time-param values normalized | Reuse deterministic analysis and product semantics across date refreshes |

Zone, GameMode, item IDs, mapping values, fields, joins, formulas, and output shape remain logic-significant. A date refresh may reuse semantics but still reruns current execution-sensitive gates.

External authoritative tables are discovered from project `sources/*.schema.json`; global code and docs do not hard-code one project's table contract.

## Execution Guarantees

`project-workflow.md` owns stage order. This record only constrains implementation: deterministic SQL/result facts are extracted once per current execution fingerprint, logic-matched semantics may be reused, execution-sensitive gates always rerun, one formalize transaction writes the related assets, and viewer builds consume persisted facts without invoking LLM.

The unit of reuse is one exact SQL plus one project-config fingerprint inside a top-level transaction. `execution_route_v1` owns profile detection and one bundled project time contract; `sql_fact_bundle_v3` owns SQL structure and performance shape. Project contracts, performance preflight, workspace save, delivery receipt, formal spec builders, and same-SQL derivatives consume those objects instead of calling their source analyzers again. A supplied route is trusted only when both fingerprints match. Legacy assets without identity are analyzed once and then persisted in the current form.

Fast Formalize selects one normalized QUERY as the transaction's canonical SQL identity. If normalization or result-field pruning changes an indexed source, the workspace receives a new same-contract revision; the old version remains immutable. Workspace, formal QUERY, run evidence, and the shared fact bundle must all resolve to that canonical execution fingerprint. External input metadata remains intake evidence only. Planning files are staged under the project-local `.tmp` directory and removed after the command.

An existing `execution_route_v1` is reused by the formalize and rule-context gates only after its SQL/config fingerprints and physical TLOG profile still match. A selected Hive route therefore remains explicit through promotion; a receipt cannot relabel a StarRocks physical table. The semantic-summary cache is local build state and is excluded from project asset tracking.

| Stage | Fresh deterministic work allowed | Must reuse / must not run |
|---|---|---|
| Requirement intake / generation | One request-bound rule application; one route/time contract for each exact executable SQL; one fact bundle; one required performance preflight | No Product Review, semantic LLM, viewer build, or second route/time/fact pass |
| Workspace save + receipt | One transactional index write; consume the generation route and fact bundle | Receipt verifies the written bytes but must not reroute or rebuild facts when exact context was supplied |
| Result attachment | One bounded result inspection and retention pass | No SQL regeneration, rule review, viewer rebuild, or repeated workbook scan |
| Fast Formalize | Recompute only a missing/stale execution-sensitive fact; inspect the result once; create one transaction | Reuse matching workspace facts, route, performance, rule application, and semantic cache; do not run raw SQL Review |
| Dashboard derivative | One separate route/rule/performance assessment only when Dashboard SQL differs from QUERY | Preview and save consume the same Dashboard assessment; do not treat spec generation as another analysis stage |
| SQL Review | One evidence package and, when required, one semantic completion | Review does not save formal assets or make every ordinary QUERY pay its LLM cost |
| Viewers / catalogs | Render persisted bounded payloads | Never parse raw SQL, reopen historical workbooks in bulk, or invoke LLM |

Performance checks are stage- and risk-scoped. L0/L1 uses the compact deterministic result; L2/L3 loads the deep guide only when its triggers require it. Adding a new check requires naming its owner and accepting an existing fact input where available; a wrapper that silently reruns another top-level analyzer is rejected.

Dual-engine projects use one `portable_tlog_sql_v1` business template. A deterministic adapter materializes the configured StarRocks default before workspace save; complexity, source density, and date span remain diagnostics and never switch the executor. Hive is selected only from an explicit user request. The saved `execution_route_v1` is reused downstream. Database/partition wrapping and executor-specific exact-case identifier quoting are code-owned and must never trigger a second LLM business-SQL generation pass. Multiple materialized execution variants require one explicit stable `execution_variant_identity_v1` assigned upstream and preserved only for exact receipt-matching SQL. Catalog consumers never infer identity or recommendation from names, SQL, paths, tags, table prefixes, or branch relations.

Development inspection resolves one confirmed stage purpose against the shared service catalog,
then detects the target server version once per service-target fingerprint before syntax validation.
An exact copied stage target reuses the same member-local probe; changed target or policy requires a
new probe. Version-derived executor capabilities gate features such as CTEs; the same table-level
resolution feeds generated enum SQL and custom-query bounds validation. A service policy expresses
time-field preference, while live columns decide availability. No command owns a hard-coded
development time field or assumes capabilities from an engine-family label.

Execution-error diagnosis reuses the same CTE namespace. `Unknown table '<db>.<cte>'` is classified as probable CTE scope/expansion loss when `<cte>` is locally declared; it is not rewritten as a physical-source problem. Runnable save and final query receipt both consume the same high-risk StarRocks assessment, while elevated-but-below-guardrail structures remain warnings.

Grouped-summary planning is also generation-time work. `summary_feasibility_v1` classifies every requested overall statistic once. Exact additive or component-based summaries stay in one SQL; non-composable metrics create two grain-distinct query families linked by `query_analysis_bundle_v1`. Shared semantics and gates are reasoned once, while each executable SQL keeps its own fingerprint and result evidence. A combined visualization consumes both exact results through the existing multi-result lineage model; it never causes another SQL generation pass.

Result visualization owns percentile-family completeness. Returned result columns define the closed set of available percentile points, and the workbook binder verifies that chart series/categories cover that full set before accepting the reusable output. Titles, notes, and source sheets are context rather than plotted evidence.

Result visualization also owns value-only workbook reuse. It compares the prior and target SQL logic fingerprints plus the exact old/new result shape and source-table footprint. Compatible changes produce a new candidate by replacing source values while preserving presentation objects; any semantic or structural change returns to normal workbook authoring. History is immutable, and the existing binder remains the only final lineage write.

SQL value, execution state, and result presentation are separate. `usage_class` records whether the SQL is a personal diagnosis, reusable diagnostic, ad hoc analysis, reusable analysis, or recurring delivery. Select it before generation because it also chooses the generation objective: analytical SQL minimizes output to the decision contract, while diagnostic SQL maximizes plausible hypothesis coverage inside exact subject and bounded time/partition scope. A diagnostic predicate should establish execution bounds or confirmed facts; uncertain state/result/correlation alternatives stay visible as filterable evidence. A confirmed result never implies reuse. Before retention is decided, diagnostic classes default to an exact-result HTML investigation report and analytical classes default to Excel; discarding SQL does not discard its exact result or presentation lineage.

Chrome tab lifecycle uses a serial single-slot model. A fresh visible query tab isolates the current SQL version, but another ready version cannot start until the current download is moved and its agent-created query/extraction tabs are closed. Terminal status plus conversation/task identity is captured before close; local result attachment and visualization happen afterward and do not keep DA pages alive. User-owned pre-existing tabs are immutable. Authentication, unknown post-submit state, failed export, or another explicit manual continuation may preserve at most one required handoff page and pauses the queue until recovery closes it.

Result visualization also owns deterministic Base evidence completeness for ratios and normalized distributions. The exact result must expose an explicit Base/denominator candidate, and that Base must be visible on a chart-containing presentation sheet at the appropriate display grain. The pre-bind validator is a thin caller of the binder audit, not a parallel rules engine. Deterministic code does not claim that a label proves business denominator meaning; the receipt preserves exact-denominator, filter/grain, and small-sample checks for human review, while missing or source-sheet-only Base blocks finalization early.

Historical grouped/overall pairs remain a result-lineage responsibility. After the same bundle contract validates the exact SQL members, one user-confirmed `adopt_bundle` decision may reuse their existing result ids and workbook bytes. The transaction updates the bundle and reusable-output lineage together; `related_queries` alone never represents consumption.

## Skill Release Gate

Skill evolution uses two bounded checks rather than an ever-growing full suite:

1. Run one directly affected regression test for each changed contract while implementing.
2. Before every runtime replacement, run `tools/verify-sql-engineering.ps1`.

The fixed release gate validates Skill structure, compiles all bundled Python, checks architecture and capability-registry invariants, then runs one isolated CLI lifecycle: external SQL intake, runnable workspace save and receipt, exact CSV result attachment, result confirmation, QUERY + VALIDATION + DASHBOARD formalization, run evidence, manifest/index links, static viewer payloads, immutable external input, and no formal writes after a blocked preflight. Its fixture is self-contained under repository `.tmp`; it never connects to a database, DA, browser, LLM, GitLab, or business project assets.

`tools/deploy-skill.ps1` runs this gate before replacing runtime. A failure leaves the installed Skill untouched. After replacement it compares the filtered source/runtime file lists and SHA-256 hashes. Do not bypass this path for normal releases, and do not add unrelated historical project-health failures to this fixed gate; repair those under their owning project workflow.

## Semantic Boundary

Deterministic code owns paths, fingerprints, lifecycle, table/field extraction, result schema, rule evidence, dialect, and gates.

LLM may own:

- a human-readable business question/Base explanation;
- metric numerator/denominator/event explanation when SQL comments and facts are insufficient;
- Product Review semantic closure;
- save-time business-topic classification when deterministic evidence is ambiguous.

Viewer builds never invoke LLM. Hollow text such as “需确认分子/分母” is not an accepted semantic summary.

Requirement completeness is a pre-generation state, not an LLM courtesy. Generic slots remain owned by requirement intake; project business truth remains owned by active canonical rules. Intake evaluates the original request once, projects active `requires_explicit_business_decision` constraints into bounded `business_decisions`, and blocks before SQL when any are unresolved. A follow-up clarification resolves only those keys against the original rule application, so a short answer such as “常规” cannot become fresh forward-rule evidence. Deterministic normalization handles governed choices; free-form semantic invention remains forbidden.

## Rule Boundary

- Canonical rules own business semantics and logical Knowledge dependencies; project bindings own exact KDV/provenance; SQL artifacts own exact consumed references.
- A compatible Knowledge rebind never creates a business-rule version. Rule Store rejects confirmed records whose semantic fingerprint changes only technical pins/source/audit metadata.
- Legacy exact pins remain immutable technical evidence. Current rule consumers resolve the project binding, while health separately validates each historical pin against its historical manifest.
- Forward activation consumes only `request_envelope_v1`; a concept/rule selector is forward evidence only when its identifier is quoted by that envelope. Candidate SQL never enters that input.
- `rule_application_v1` is the durable application decision. Exact request quotes, inherited parent hash/asset, exclusions, and diagnostics are separate fields.
- Optional application inheritance is closed by default and limited to exact lifecycle promotion, same-contract correction/parameter refresh, and same-contract Dashboard derivation. Branch/title/comment inheritance is forbidden.
- Shared source logs never prove a metric rule by overlap alone.
- Product-facing criteria contain only actual applied/matched criteria, explicit conflicts, manual checks tied to missing evidence, and SQL-unique criteria.
- Candidate/rejected/partial/reverse diagnostics remain code evidence.
- Formalization blocks active hard conflicts and execution-contract failures, not weak reverse similarity.
- Historical rule text cannot become a current SQL condition without matching structured evidence.
- Reverse exact evidence may flag `unrequested_scope_mutation`; it detects an unauthorized SQL scope but never retroactively authorizes the rule.
- User-confirmed one-query exceptions become workspace governance feedback. QUERY never resolves them by editing Python or canonical rules; later RULES or SKILL_EVOLUTION owns that decision.

## Viewer Boundary

Workspace, formal repository, raw SQL Review, and Dashboard review have independent payloads and lifecycle responsibilities. They may share facts and render helpers, but one viewer never scrapes or promotes another viewer's output.

The all-status asset catalog is a shared integration index rather than another viewer. It aggregates formal manifests/run evidence, Rule Store, knowledge bindings, shared read models, and platform contracts. Project-local query workspaces remain searchable only through their local indexes and never enter Git or this catalog. Draft, failed, historical, proposed, and superseded shared states remain visible; status is descriptive only. Exact SQL execution delivery is projected from persisted route receipts. Reusable workbook presentation is projected from attachment-time bounded manifests; legacy workbooks remain downloadable without a preview, and catalog refresh never opens them in bulk. The catalog never invokes LLM, re-analyzes SQL, or establishes a publication boundary. Command output remains bounded; complete projections live in the generated JSON files.

The asset group registry is a persistent directory overlay, not a build-time sort. `AG-NNNN` is allocated once from strong lineage after semantic organization and never reused or renumbered. SQL `vNNN` remains version identity. Text similarity and broad business topics cannot merge groups; ambiguous cross-group edges stay visible for explicit curation.

Refresh modes:

- `dynamic`: save and serve current state without static rebuild.
- `incremental`: update compatible changed items.
- `deferred`: batch saves, rebuild once later.
- `full`: migration, repair, or release verification.

## Documentation Budget

Before this refactor, a simple query route could require roughly 27k tokens of mandatory global references before project rules or SQL. The target is:

- `SKILL.md`: routing and hard boundaries only; lifecycle procedure stays elsewhere.
- `operating-contract.md`: cross-stage blockers only.
- `project-workflow.md`: sole lifecycle authority.
- `core-rules.md`: project-independent SQL correctness only.
- route-specific references loaded only when their route is active.
- `capability-map.md` generated from `capabilities.json`, never hand-maintained.

Measured after the refactor (2026-07-11), the common Hive query route files (`SKILL.md`, operating contract, core rules, query workspace, and Hive dialect) total about 30.9k characters, roughly 9.6k tokens using a conservative 3.2 characters/token estimate. Project config/rules and the user's SQL are additional, but unrelated review/dashboard/performance-deep documents are no longer mandatory.

## Regression Anchors

- Date-only parameter changes: execution fingerprint changes, logic fingerprint stays equal.
- Zone/mode/item/business changes: logic fingerprint changes.
- Workspace seed/meta persist both fingerprints and one fact bundle.
- Formalize may reuse semantics on logic match but not performance/rule/time execution gates.
- Workspace, formalize, review, and repository agree on physical sources and final fields.
- External intake paths never persist as absolute paths.
- Result/Excel/visualization files bind to exact SQL version and execution fingerprint.
- Rule/performance/output blockers stop before optional semantic or Dashboard work.
- Viewer builds do not run LLM or raw SQL analysis when persisted facts exist.
- QUERY delivery ends with a ready `query_delivery_receipt_v1`; the response must link its absolute SQL file path.
- QUERY execution runs ready versions serially with at most one agent-created query tab, closes completed query/extraction pages after download capture and before local binding, never closes user-owned tabs, and pauses on the single allowed handoff page.
- SQL-side de-identification is blocked consistently by workspace save/receipt, formal gates, Review, and project health.
- A high combined StarRocks CTE expansion risk blocks both runnable save and final receipt; many shallow CTEs remain a non-blocking control.

## Change Test

Before accepting another workflow change, answer:

1. Which stage owns the new fact or decision?
2. Is its source of truth singular?
3. Does this remove repeated work or only relocate it?
4. Can a cheaper blocker stop earlier?
5. Does temporary work remain fast and formal work remain strict?
6. Did any old CLI, prompt, schema, validator, or viewer path remain contradictory?
7. Is the original failure covered by a focused regression test?

If ownership is unclear, change the model before adding another branch.
