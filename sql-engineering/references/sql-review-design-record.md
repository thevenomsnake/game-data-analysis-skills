# SQL Review Design Record

This is the current design authority for SQL Review. Replace superseded decisions instead of appending a changelog.

## Product Decision

SQL Review has one evidence package and two lenses:

- Product View explains the business calculation and decisions.
- Code View preserves deterministic engineering proof.

Review is a pre-formalization judgement surface. It does not save QUERY/Dashboard assets, mutate rules, or replace the formal repository and Dashboard approval viewers.

## Ownership

| Component | Owns | Does not own |
|---|---|---|
| `sql_facts.py` | Deterministic SQL structure and output facts | Product meaning |
| Review evidence builder | Bounded SQL/result/rule/role evidence | Final prose or execution inference by naming |
| Product agent | Human-readable semantic closure | Paths, lifecycle, rule mutation, or invented project facts |
| Product validator | Completeness and anti-filler checks | Repairing missing evidence |
| Code View | Exact traces and gates | Product narrative |
| Product View | Base, metrics, events, risks, actions | Raw CTE walkthrough as its main content |

## Evidence Identity

The current SQL file is the only review subject. A result is attached only by exact parent directory and exact stem. Multiple formats use `.xlsx`, `.csv`, `.txt` priority and remain visible as alternatives.

The following never establish a relationship:

- numeric prefixes;
- words in filenames or titles;
- inbox paths;
- dates, zone values, or broad source overlap;
- similar SQL text;
- historical naming conventions.

Execution project identity requires an explicit selection/file-role map or one unambiguous physical-table profile. A shared profile yields `execution_project_unresolved`. Definition/delivery project context may come from explicit project arguments or the configured inbox project boundary, but that context never proves execution.

## Semantic Contract

`sql_review_v14` exposes `product_view` and `code_view`. Evidence Package v3 and Product Agent v9 use `execution_evidence`; old `review_family`/`variant_evidence` group semantics are removed.

Product View requires:

1. conclusion and Base;
2. risk register;
3. metric summary and metric cards;
4. event contracts when event candidates exist;
5. common filters and evidence-bound actions;
6. folded deterministic evidence.

Every final metric must be represented. Shared events and risks are normalized to `E*` and `R*` references. Critical SQL conditions remain visible but receive no business label unless comments, active knowledge, or applied rules provide one.

Normal reports require `semantic_review_status=llm|llm_cached`. Deterministic fallback is debug evidence, not a product report. Static `logic_review`, legacy metrics, formulas, and CTE steps can only enter folded evidence and cannot overwrite accepted semantic fields.

## Rule Boundary

Product View receives only:

- applied criteria;
- matched saved rules;
- real conflicts;
- manual checks tied to missing evidence.

Weak candidates, partial/reverse source audit, rejected matches, and broad token overlap remain Code View diagnostics. Historical text cannot become a current condition or a correct mapping.

## Failure Behavior

Block Product View when:

- the model is unavailable outside explicit debug mode;
- a final metric is missing or contains filler;
- an event candidate has no event contract;
- a conflict lacks current/expected/difference/impact/action evidence;
- output cannot be mapped back to the exact SQL item.

Missing result evidence does not prevent code review. It downgrades evidence status and creates a concrete action. Loaded results without execution identity remain unresolved.

## Performance

Deterministic extraction runs once per SQL. Product work runs only for uncached identities and may be chunked. Rendering consumes persisted JSON and never calls an LLM or reparses historical SQL.

## Rejected Designs

- Numbered batch files as implicit SQL/query/result families.
- Filename/path words as project or proxy evidence.
- Static regex prose as a valid Product View.
- A combined Review/formal repository/Dashboard approval page.
- Feeding every candidate saved rule into Product View.
- Re-running Review during fast formalization of already executed SQL.

## Acceptance Anchors

- A file named `001_obt.sql` does not select an execution project.
- `candidate.sql` pairs only with a same-directory `candidate.<result-ext>`.
- A real result with no execution evidence is unresolved.
- Product evidence uses `execution_evidence`, never numbered variant buckets.
- LLM metric/event/risk fields survive normalization unchanged.
- Evidence-only output is rejected as a normal Product View.
