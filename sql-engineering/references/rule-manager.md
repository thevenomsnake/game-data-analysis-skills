# Canonical Rule Management

This reference is the authority for project business rules, rule activation, and the read-only rule dictionary.

## Ownership Model

Keep these five layers separate:

| Layer | Owns | Must not own |
|---|---|---|
| `project_config.json` | dialect, engine, source profile, partition/time policy, default zone | metric definitions and ID mappings |
| `knowledge-base/` + project binding | versioned ID mappings, enums, static classifications, config-table facts | event/numerator/denominator logic |
| Canonical Rule Store v2 | Base, event, numerator, denominator, deduplication, attribution, lifecycle source policy, hard business constraints | mutable mapping rows and execution configuration |
| activation contract | when one saved rule is relevant to a request or candidate SQL | business truth itself |
| SQL artifact spec | which exact rule and knowledge versions one SQL used | global or project-wide truth |

Moving a value between layers is a governed migration, not a copy. Preserve the old immutable version, create the new authority, and validate equivalence. Write a new rule version only when the business formula, meaning, activation, or required logical contract changes.

## Rule Store v2

Every active project uses only:

```text
rules/
├── store.json
├── activation-index.json
├── definitions/<concept_key>/vNNN.json
├── governance/
└── migrations/
```

`store.json` owns lifecycle pointers. Each definition file is immutable. A new confirmed rule creates the next version and supersedes the old pointer without rewriting history. `activation-index.json` is derived from current confirmed/proposed records and may be rebuilt. It contains structural selectors and version pointers only: never titles, rule prose, weak terms, or text-search aliases.

`rules/canonical_rules.json` is legacy migration input only. Query, review, formalization, repository, dashboard, health, and dictionary runtime paths must never read it.

Statuses:

- `proposed`: saved candidate, inactive for SQL generation;
- `confirmed`: the one active version for a concept;
- `superseded`: immutable prior version;
- `deprecated`: immutable inactive version with no current replacement.

Project-scoped persistent rules require a registered `concept_key`. The cross-project registry groups comparable concepts; it does not contain business definitions or create global truth.

## Rule Versus Knowledge

A rule explains how to calculate or classify. Knowledge supplies stable rows used by that explanation.

Current confirmed rules must not inline mutable mapping rows, even when the table has only a few rows. If a value can change because a config table, project stage, operation plan, or partner source changes, it is Knowledge data. Declare a logical project-binding dependency instead:

```json
{
  "knowledge_dependencies": [
    {
      "dataset_id": "example_mapping",
      "projection_id": "item_level",
      "semantic_role": "classification",
      "fields": ["item_id", "level"],
      "required": true,
      "binding_policy": "active_project_binding"
    }
  ]
}
```

The project binding owns the exact KDV, content hash, projection hash, and source release. `bind` checks every current rule's required projection, fields, primary key, field semantics, and authoring mode before it writes. Compatible changes are classified as `provenance_only`, `projection_changed`, or `contract_changed` and return `rule_version_required=false`. Incompatible changes block before activation. Each SQL persists the exact KDV it actually used in `knowledge_reference_v1`.

Rule Store computes a semantic fingerprint from the rule's title/content, scope, activation contract, formulas, and logical dependencies. It ignores KDV pins, source paths, timestamps, notes, and authorization metadata. A new confirmed record with the same semantic fingerprint is rejected; update the Knowledge binding instead. Immutable legacy exact pins remain technical history and are checked against their historical manifests, while current runtime resolution always uses the project's active binding.

Use this decision test:

| Question | Owner |
|---|---|
| Would the value change when a config/operation table changes, while the metric algorithm stays the same? | Knowledge dataset |
| Is the row an ID-to-name/category/level/item/channel mapping? | Knowledge dataset |
| Is the set itself the calculation boundary, such as bucket edges, lifecycle branches, or a state-machine transition? | Canonical rule |
| Is it dialect, database, partition, default date, or execution routing? | Project config/source contract |

The rule records why and how the mapping is used. The knowledge projection records the rows. The activation contract records when the rule is relevant. Never repeat the same rows in all three places.

## Write Authority

QUERY, REVIEW, SQL_FORMALIZE, VALIDATION, DASHBOARD, and repository/viewer builds are read-only rule consumers.

Any `proposed`, `confirmed`, or `deprecated` write requires a separate explicit `[RULES]` request. The saved version records the authorization contract and request hash. `confirmed` additionally requires explicit user confirmation.

Discovering a questionable rule during SQL work does not authorize a source-code or rule edit. Record the evidence on the current query. Resolve it later through:

- `[RULES]` when business truth changes;
- `[KNOWLEDGE]` when mapping/reference data changes;
- `[SKILL_EVOLUTION]` when matching, routing, or validation behavior changes.

External evidence must first be copied into the repository. Persist only project-relative evidence paths.

## Activation Contract v2

Business truth and activation are separate. Every current confirmed rule uses `canonical_rule_activation_v2`.

### Forward activation

Forward activation answers: "Did the user ask for this concept?"

Only explicit `request_signatures` matched against `request_envelope_v1.text`, or a concept/rule identifier quoted by that same current message, may activate a rule. `--concept-key` and `--rule-id` are retrieval hints when their identifier is absent from the message; a caller cannot turn an inferred key into user intent. Every applied intent-required rule persists the exact current-request quote in `rule_application_v1`. Titles, purposes, summaries, comments, full rule prose, `search_terms`, old SQL, `branch_of`, shared logs, and candidate SQL text cannot enter the forward input. Runtime source gating reads the stored activation contract only; it must not infer an expected source by regex-scanning rule prose.

```json
{
  "contract_version": "canonical_rule_activation_v2",
  "status": "confirmed",
  "application_class": "intent_required",
  "unrequested_sql_policy": "block",
  "activation_policy": {
    "forward": "automatic",
    "reverse": "exact_only"
  },
  "request_signatures": [
    {
      "label": "造物等级",
      "any_of": ["造物等级", "工作台造物等级"],
      "all_of": [],
      "none_of": []
    }
  ]
}
```

Use `explicit_only` when automatic intent matching is unsafe. Use `disabled` only for an inactive boundary. Automatic forward activation requires at least one narrow request signature.

Current-request negation wins over activation and inheritance. Use `request_exclusions` for domain-specific negative forms; the evaluator also recognizes direct negative scope around a matched quote. Persist the decision under `excluded_rules`.

### Structured application and inheritance

`rule_application_v1` is the only durable statement that a SQL version applies a canonical rule. It separates current-request `applied_rules`, eligible `inherited_rules`, explicit `excluded_rules`, and diagnostics.

Inheritance is closed by default. It is allowed only for:

- exact SQL lifecycle promotion;
- `correction` or `parameter_refresh` in the same query family with `coverage_relation=same_contract`;
- a Dashboard derivative proven to keep the QUERY logic contract.

`replacement`, superset, branch, new analysis, title similarity, and SQL comments do not inherit. A changed current rule version also invalidates the parent application until reevaluated.

### Reverse SQL audit

Reverse audit answers: "What saved event/metric does this SQL actually implement?"

It uses deterministic SQL evidence: original logs, predicates, field roles, final metric roles, final aggregations, grain, and conservative lineage. Shared-log overlap alone is never exact.

Reverse policies:

- `exact_only`: exact event evidence may support applied-rule display or an explicitly declared mutual-exclusion conflict;
- `diagnostic_only`: evidence stays in Code View/diagnostics;
- `disabled`: no reverse audit for this rule.

Weak/partial reverse matches never activate a rule and never block formalization. An exact reverse match may block when an intent-required rule declares `unrequested_sql_policy=block` and the SQL implements that optional scope without a current or eligible inherited application. This is a scope-mutation guard, not reverse activation.

See `rule-context-event-signatures.md` for the normalized evidence contract.

### Hard constraints

Only constraints from forward-active rules may govern SQL. Scope source/formula requirements with `applies_in` when QUERY and Dashboard legitimately use different sources:

- `temporary_query`
- `retained_query`
- `validation`
- `dashboard_delivery`
- `review`

Unknown lifecycle stages fail validation. An omitted `applies_in` means the constraint applies everywhere.

Rules whose metric meaning depends on a user-selected business scope may declare `requires_explicit_business_decision` with a stable `decision_key`, allowed semantics, and `unresolved_policy=ask_requester`. Requirement intake consumes this constraint before QUERY generation. The original request activates the rule; a later clarification may fill only that already-active decision and cannot activate another rule. Do not duplicate operational defaults across rule prose, `decision_question`, and code. `decision_question` is a human-readable fallback; the structured constraint is the runtime source.

## Temporary Exceptions

When the user explicitly says a query is temporary or confirms a one-query exception, a canonical business conflict may become a diagnostic for that query family. Persist `temporary_rule_override_v1` with the exact conflict, reason, and confirmation.

This never edits the rule or skill. Execution, dialect, privacy, correctness, and performance blockers remain strict. An unresolved override cannot be promoted to a formal QUERY or Dashboard.

## Rule Dictionary

The rule dictionary is a read-only current-first viewer. It consumes Rule Store snapshots, not raw prose dumps.

Show:

- one current fact per concept;
- mappings/classifications as structured tables or linked knowledge dependencies;
- only meaningful project differences;
- activation and raw evidence folded by default;
- history loaded separately from current state.

The viewer must identify whether a rule uses a project-bound Knowledge dataset and show the current mapping without making a KDV ID the rule's identity. It must not duplicate 450 mapping rows inside the current rule card.

For Knowledge-backed rules, the viewer renders three separate blocks: current stable rule, current project-bound mapping rows, and material cross-project/version differences. Raw KDV/source IDs and duplicate storage revisions stay in folded technical evidence. It never reconstructs current mappings from historical rule prose.

For a concept migrated with a verified `retire_to_config` action, the viewer renders a dedicated project-config card with the exact `project_config.json` pointer and current value. This counts as governed coverage, not as a saved rule. The dictionary and cross-project review must use the same config-ownership resolver. Deprecated rule prose remains immutable history and must not cause `expected_project_missing`, rule activation, or a duplicate current fact.

## Commands

List current project rules:

```powershell
python scripts/sql_project.py rule-report --root <project-root>
```

Save a proposed or confirmed immutable version:

```powershell
$req = "[RULES] 保存项目持久口径：<exact action>"
python scripts/sql_project.py add-rule `
  --root <project-root> `
  --concept-key <registered-concept-key> `
  --title "<title>" `
  --content "<concise current fact>" `
  --status proposed `
  --user-request $req `
  --function-selection RULES
```

Build and validate the cross-project dictionary:

```powershell
python scripts/rule_dictionary.py build --projects-root ./sql-projects
python scripts/rule_dictionary.py validate --projects-root ./sql-projects --format json
```

The generated dictionary is a disposable current snapshot. It reads confirmed current definitions and proposals through `RuleStore.build_dictionary_snapshot(include_history=False)`. Browser code must not scan `store.json` or `definitions/**`, and historical bodies are never embedded in the default HTML/JSON.

Load one concept's immutable history only when requested:

```powershell
python scripts/sql_project.py rule-report `
  --root <project-root> `
  --concept-key <concept-key> `
  --status all `
  --json
```

This path calls `RuleStore.load_versions(concept_key)` and does not load every project's history.

Audit activation contracts:

```powershell
python scripts/rule_activation_governance.py audit --root <project-root> --format json
```

Migrate reviewed mutable mappings from current rules into immutable knowledge versions:

```powershell
python scripts/rule_mapping_knowledge_migration.py `
  --root <project-root> `
  --plan <project-root>/rules/migrations/<migration>.plan.json `
  --user-request "<verbatim approved migration request>" `
  --dry-run
```

Run the same command without `--dry-run` only after reviewing every extracted row, target dataset, projection, and replacement rule. The receipt under `rules/migrations/` is the completion record.

Immutable historical versions are never rewritten to add missing authorization metadata. When a validator identifies history created before authorization enforcement, create a hash-bound governance amendment instead:

```powershell
python scripts/rule_authorization_governance.py amend `
  --root <project-root> `
  --rule-id <rule-id> `
  --reason "<why this preserved historical record is being ratified>" `
  --function-selection RULES `
  --user-request "[RULES] <verbatim confirmation>"
```

## Quality Gates

Block current rule delivery when:

- Rule Store v2 or its activation index is missing/invalid;
- a current rule lacks `canonical_rule_activation_v2`;
- forward activation depends on title/body/search-term fuzziness;
- the activation index contains titles, descriptions, rule prose, `search_terms`, `must_have_any`, or `weak_terms`;
- a required logical knowledge dependency cannot resolve through the active project binding;
- a legacy exact dependency no longer matches its immutable historical manifest;
- a current rule contains mutable mapping rows in prose or `structured_definition.mapping_tables`;
- a mapping concept has no required `active_project_binding` knowledge dependency;
- a shared-log metric claims exact reverse match without predicate/field-role, metric-role, and final-aggregation evidence;
- a rule write lacks explicit RULES authorization;
- an evidence path is absolute or outside the repository.
