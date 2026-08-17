# Rule Context Event Signatures

## Purpose

Use event signatures to distinguish business rules that share a raw log or common fields. Global skill documentation owns the matching contract only. Project log names, IDs, mappings, corrected predicates, and historical mistakes belong in project canonical rules and regression fixtures.

## Evidence Model

Extract SQL evidence deterministically before matching rules:

- `source_logs`: original XML/TLOG log identities inferred from physical tables;
- `predicates`: normalized field/operator/value conditions;
- `ids_and_ranges`: typed ID, mode, zone, duration, time, and partition boundaries;
- `aggregations`: normalized `SUM`, `COUNT`, `COUNT_DISTINCT`, `MAX`, `MIN`, and final `RATIO` markers;
- `final_metrics`: top-level output aliases and expressions, excluding helper CTE outputs;
- `final_metric_aggregations`: aggregations proven to feed final metrics through conservative alias lineage;
- `metric_roles`: quantity, presence, event count, support count, duration, classification, progression, distribution, and ratio;
- `field_role_evidence`: predicate, group-by, final dimension, final output, aggregation, and final aggregation roles.

The extractor may return weak or partial evidence. It must never promote a shared-log rule to an exact match from log or field overlap alone.

## Contract

A rule may declare `activation_contract.event_signature`:

```json
{
  "required_logs": ["ExampleEvent"],
  "required_predicates": ["ChangeType = 'add'"],
  "required_metric_roles": ["quantity"],
  "required_aggregations": ["SUM(ChangeAmount)"],
  "required_field_roles": [
    {"field": "ChangeAmount", "roles": ["aggregation", "final_aggregation"]}
  ],
  "incompatible_predicates": ["ChangeType = 'remove'"],
  "incompatible_metric_roles": ["presence"],
  "incompatible_concept_keys": ["example-presence"]
}
```

Singular and plural forms normalize to one internal representation. This applies to required logs, predicates/conditions, metric roles, aggregations, field roles, text terms, incompatible predicates/roles, and incompatible or mutually exclusive concept keys. Scalar strings are one value, never a character list.

Use `required_any_metric_roles`, `required_any_aggregations`, or `required_any_text_terms` when several equivalent shapes are valid. They still require the correct source log and structural boundary evidence. Text terms can strengthen reverse implementation evidence but never become forward request evidence.

For a non-metric boundary rule, declare:

```json
{
  "required_log": "ExampleLifecycle",
  "match_policy": "boundary_only",
  "required_field_roles": [
    {"field": "EndTime", "roles": ["predicate", "final_output"]}
  ]
}
```

`boundary_only` may explain event semantics in review, but cannot become an exact metric match by itself.

Current confirmed rules use `canonical_rule_activation_v2`. Runtime consumers never derive an activation contract from rule prose or a historical record. `source_signature` may narrow candidate evidence, but shared-log reverse matching requires an explicit `event_signature`; prose, `metric_families`, and broad field overlap cannot replace it.

## Matching Semantics

- Required source logs are the candidate gate. If none are present, return no reverse match.
- Shared logs require final metric role, final aggregation, and predicate or field-role boundary evidence for `exact`.
- Intermediate aggregations do not prove a final metric. Use conservative lineage to the final SELECT.
- `partial` means source evidence exists but a required role, aggregation, predicate, or field role is missing.
- `weak` means only broad source evidence matched.
- `boundary_only` remains contextual/diagnostic.
- Negative or incompatible evidence can reject an exact match, but free-form prose cannot create a blocker.
- A reverse exact match blocks when an active rule explicitly marks it mutually exclusive, or when an intent-required optional rule declares unrequested SQL implementation blocking and has no current/eligible inherited application.
- Ratio rules require a final numerator/denominator expression or equivalent proven lineage, not slash text in comments.

## Consumer Boundary

The same normalized evidence governs rule-context, formalization summaries, SQL Review, repository enrichment, and Dashboard conversion.

- `rule_application_v1.applied_rules` and `inherited_rules` may enter product-facing output only after request evidence plus source/event-signature gating.
- Weak/partial candidates, rejected rules, reverse diagnostics, source-metric audits, and unrelated name/logic mismatches stay in code diagnostics.
- Product-facing applied criteria show only rules the SQL actually uses, explicit conflicts, concrete evidence gaps, and SQL-unique criteria.
- Forbidden substitutions or historical mistakes mentioned in rule prose are not expected SQL filters.
- Viewer builds consume persisted summaries. They do not rerun event-signature matching or repair stale sidecars.
- Explicit repository enrichment and formalization may compute and persist a new summary through this contract.

## Lifecycle Semantics

- Temporary mode may retain a user-scoped business-rule conflict as diagnostics through `temporary_rule_override_v1`; execution, privacy, correctness, and performance blockers remain strict.
- Generation and review block only on active hard constraints or explicit mutually exclusive exact evidence.
- Formalization reruns current strict evidence. Weak reverse similarity never blocks an already-run SQL.
- A title, comment, old SQL, or branch relation cannot activate or inherit a rule. Only the current request envelope, explicit selection, and an eligible structured parent application can do so.

## Regression Requirements

- A required-log mismatch returns no reverse rule match.
- A helper CTE aggregation without final-metric lineage cannot exact-match a metric rule.
- A final alias lineaged to the required source field may satisfy aggregation evidence.
- Presence and quantity rules over one log remain distinct by final metric role and aggregation.
- Boundary-only rules never appear as exact metric criteria.
- An incompatible predicate conflicts only when the candidate SQL actually contains it.
- Repository build tests fail if build invokes rule matching or summary generation.
- Project-specific corrections stay in project rules/tests and never re-enter this global reference.
- A title-only activation regression, an ineligible branch inheritance, and an unrequested optional-scope implementation are covered independently.
