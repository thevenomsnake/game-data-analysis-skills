# SQL Review Product Agent

Use this reference when changing the semantic Product View command, prompt, validator, or cache.

## Role

Act as a product metric reviewer. Consume the deterministic Evidence Package v3 and return strict JSON; do not teach SQL or infer execution history.

Evidence priority:

1. Final output and exact result columns/samples.
2. Applied rule checks and explicit project roles.
3. Metric expressions, source steps, filters, and lineage.
4. SQL and CTE comments.
5. Remaining code evidence.

When evidence is insufficient, use low confidence and one evidence-bound question. Never substitute project memory, filename conventions, or generic domain guesses.

## Input Contract

`execution_evidence` contains the current SQL role, exact paired result, declared execution/delivery projects, and evidence status. It is the only product-agent source for execution claims.

`criteria_alignment` contains applied/matched/conflicting/manual-check criteria. Weak candidates and reverse diagnostics remain under code evidence.

`event_contract_candidates`, metric cards, dimensions, filters, comments, result evidence, and source facts are bounded deterministic inputs. Preserve their IDs and conditions; do not invent mappings.

## Output Contract

Return:

- `execution_evidence`;
- `conclusion`;
- `risk_register` and `review_actions`;
- `metric_summary_table`, `metric_overview`, and `metric_cards`;
- `event_contracts` and `event_index` when candidates exist;
- `common_filters` and metric-bound confirmations;
- `business_story_cards`, `metric_path_cards`, and `output_contract`;
- folded `evidence_sections`;
- `semantic_review_status=llm|llm_cached`.

Each metric card includes:

- metric name, business meaning, and type;
- calculation and visible key conditions;
- numerator plus denominator, or the counted/statistical object;
- dedup key, aggregation dimensions, and row grain;
- source logs/fields, metric filters, event refs, risk refs;
- saved-rule alignment, metric-bound confirmations, evidence refs, and confidence.

Write shared event definitions once under `E1`, `E2`; write shared risks once under `R1`, `R2`. Metrics reference those IDs instead of repeating paragraphs.

## Semantic Rules

- Translate expressions into business meaning; keep formulas and CTE lineage in folded evidence.
- Promote actual SQL ID/range/mode/time boundaries into `key_conditions` without assigning meanings not present in evidence.
- For event metrics, state the proving source, event condition, mapping evidence, counted object, first/final rule, attribution, and concise SQL refs.
- Explain a conflict with current SQL behavior, expected evidence, exact difference, affected metrics, impact, and action.
- Strip SQL aliases from product prose. When source lineage is unresolved, say so and add a metric-bound confirmation.
- Do not claim a project/environment executed unless `execution_evidence` proves it.

Reject:

- `需确认分子/分母` without a metric and concrete evidence gap;
- `结合业务需求确认` filler;
- raw formulas as the main business explanation;
- global unbound confirmation lists;
- project-specific values or mappings not present in evidence;
- Product View built from `evidence_only` or `model_unavailable`.

## Runtime

`sql_review_product_agent_v9` processes only uncached SQL identities. Cache identity is based on the SQL/evidence contract, not output filenames.

Chunked commands return:

```json
{"items": [{"path": "candidate.sql", "product_view": {}}]}
```

A one-item command may return the Product View object directly. The caller maps by exact `path` or `name`, normalizes minor shape drift, and validates every metric/event/risk contract.

Use `SQL_REVIEW_PRODUCT_AGENT_BATCH_SIZE` and `SQL_REVIEW_PRODUCT_AGENT_PARALLELISM` only to control model work. Viewer builds never call the model. An invalid or unmapped response blocks normal Review; downgrade is allowed only with explicit `--allow-product-review-downgrade` for offline debugging.

Read `sql-review-design-record.md` before changing this contract and update that record when the ownership model changes.
