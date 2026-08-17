# SQL Review Workflow

Use this reference only for raw SQL review. Formal save, repository browsing, and Dashboard approval remain separate workflows.

## Purpose

SQL Review answers two questions from one evidence package:

- `product_view`: What does this SQL measure, what is the Base, how are metrics calculated, what evidence supports them, and what must be resolved?
- `code_view`: What SQL structure, physical sources, expressions, result files, rules, performance, dialect, and privacy facts support that judgement?

`sql_review_v14` is the only current report contract. Legacy `logic_review`, formulas, and CTE traces may appear as folded code evidence; they never replace semantic Product View fields.

## Entry

Use capability `REVIEW` and pass the verbatim request. Review does not save formal assets or mutate rules.

```powershell
python scripts/sql_review.py <sql-or-directory> `
  --function-selection REVIEW `
  --user-request "<verbatim request>" `
  --definition-project-root <project-root> `
  --delivery-project-root <project-root> `
  --product-review-mode llm `
  --product-review-command "<configured command>"
```

Use legacy `--project-root` only when definition, execution, and delivery are explicitly the same project.

## Evidence Package

Build deterministic evidence once per SQL:

- exact SQL facts and final output fields;
- SQL/CTE comments;
- project config and explicitly selected rules;
- result schema, row count, bounded samples, and column alignment;
- performance, dialect, privacy, and rule traces;
- `execution_evidence` describing only facts proven for this SQL.

The current SQL is always the review subject. Do not infer another SQL's relationship, execution role, proxy status, or result lineage from numbering, filename words, titles, paths, shared logs, dates, zone values, or SQL resemblance.

## Result Pairing

Pair a result only when it has the same parent directory and exact stem:

```text
candidate.sql
candidate.xlsx | candidate.csv | candidate.txt
```

When several exact-stem formats exist, prefer `.xlsx`, then `.csv`, then `.txt`, and report alternatives. Different stems are orphan results. Missing evidence does not stop code review, but it sets `missing_result_file`.

## Project Roles

Keep these roles distinct:

- `definition_project`: business rules used for judgement;
- `execution_project`: environment that actually produced the result;
- `delivery_project`: intended destination.

Accept execution identity only from an explicit execution selection, `file_role_map`, an unambiguous physical-table profile match, or the documented legacy `--project-root` contract that explicitly declares definition, execution, and delivery to be the same project. Shared profiles remain unresolved. A loaded result with no execution evidence is `execution_project_unresolved`, not target verified.

Evidence states are:

- `target_reviewed`;
- `proxy_reviewed_needs_target_verification`;
- `execution_project_unresolved`;
- `field_mismatch`;
- `missing_result_file` or a result read error.

Proxy evidence proves only its declared execution environment. Compare cross-project rules only through explicit `concept_key` evidence.

## Product View

The default Product View presents:

1. Conclusion and business question.
2. Base and grouping.
3. `risk_register` with stable `R1`, `R2` references.
4. `metric_summary_table` and one `metric_card` per final metric.
5. `event_contracts`/`event_index` with stable `E1`, `E2` references when event candidates exist.
6. Common filters and metric-bound actions.
7. Folded evidence.

Every metric states business meaning, calculation, key conditions, numerator, denominator or counted object, dedup key, grain, source evidence, event/risk references, and confidence. Shared event/risk text is written once and referenced.

The deterministic layer extracts evidence; it does not author the final product narrative. Normal review requires `semantic_review_status=llm|llm_cached`. `evidence_only` and `model_unavailable` are explicit debug states and cannot be presented as a valid Product View.

Reject filler such as `需确认分子/分母`, `结合业务需求确认`, raw `SUM(...)` as business meaning, or confirmations not bound to a metric/risk/evidence reference.

## Code View

Code View retains:

- role and execution inference evidence;
- source/target tables, params, final fields, CTE/JOIN/window structure;
- metric expressions, lineage, filters, and dedup traces;
- result-file alignment and bounded samples;
- rule application and diagnostics;
- performance, partition/time, dialect, and SQL-side privacy blockers;
- lifecycle readiness.

Candidate, partial, reverse-audit, and weak-overlap rules stay here. Product View receives only applied criteria, matched saved rules, real conflicts, and evidence-bound manual checks.

## Outputs

Each SQL directory receives:

```text
sql_review_product.md
sql_review_code.md
```

The batch root receives:

```text
sql_review_summary.md
sql_review.json
sql_review.html
```

The HTML defaults to Product View and reads only `product_view` for that tab; Code View reads only `code_view`. If semantic closure fails, show a blocker instead of a regex-authored substitute.

Inspect an existing report without rebuilding it:

```powershell
python scripts/sql_review.py --serve <review-root-or-sql_review.json>
```

For large semantic batches, use `sql-review-subagents.md`; workers consume deterministic shards and only the merge step writes final report files.

## Change Rule

Before changing Review, read `sql-review-design-record.md`. Keep one evidence package, one schema version, and one renderer contract. Add one focused regression for the observed failure; do not encode a project name, fixed value, or historical filename convention into global Review code.
