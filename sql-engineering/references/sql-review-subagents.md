# SQL Review Desktop Sub-Agent Orchestration

Use this reference when a SQL Review batch is large enough that one product semantic reviewer or the internal Codex CLI fallback is too slow. This workflow uses Codex Desktop left-side sub-agents. It is separate from the script's `ThreadPoolExecutor + codex exec --ephemeral` fallback.

## Default Policy

For large SQL Review batches, prefer Desktop sub-agent orchestration when the host exposes sub-agent tools.

Default worker target: `10` sub-agents.

Use fewer workers only when:

- the batch has fewer than 10 review items;
- the user asks for a smaller run;
- the environment is rate-limited or unstable;
- workers need disjoint write scopes and the current task cannot be split safely.

The script fallback default is also 10 concurrent product-agent child processes via `SQL_REVIEW_PRODUCT_AGENT_PARALLELISM`, but this is not the same as visible Desktop sub-agents.

## Why This Exists

`scripts/sql_review.py` can run model product-review calls in chunks, but those calls are local child processes. They do not appear as left-side sub-agents and they still serialize work inside each chunk.

Desktop sub-agents are useful when:

- the user explicitly wants visible parallel workers;
- each SQL or small shard needs real semantic reading;
- review latency matters more than keeping everything inside one Python process.

## Safe Architecture

Do not let multiple sub-agents run `sql_review.py` against the same batch root and write the same fixed output files. That races on:

- `sql_review.json`
- `sql_review.html`
- `sql_review_product.md`
- `sql_review_code.md`
- same-directory report files

Instead:

1. Main agent creates or reuses a base `sql_review.json` containing deterministic `product_review_evidence`.
2. Main agent runs `scripts/sql_review_subagent_orchestrator.py plan` to split evidence into shard files and worker prompts.
3. Main agent spawns up to 10 left-side sub-agents. Each worker reads exactly one `evidence_shard_XXX.json` and writes exactly one `product_views_shard_XXX.json`.
4. Main agent runs `scripts/sql_review_subagent_orchestrator.py merge` to validate, normalize, merge product views, and render final JSON/HTML.

## Commands

Prepare a base review JSON. For a sub-agent run, it is acceptable to create the base with product review disabled or downgraded because the final product views will be supplied by workers:

```powershell
$req = "【SQL审查】帮我 review 这个批次，BASE 口径，EXPERIMENT/AB_TEST 代理跑数，未来用于 BASE"
python .\sql-engineering\scripts\sql_review.py <batch-path> `
  --user-request "$req" `
  --function-selection "【SQL审查】" `
  --product-review-mode off `
  --allow-product-review-downgrade `
  --json-name sql_review_base.json `
  --html-name sql_review_base.html
```

Plan sub-agent shards:

```powershell
python .\sql-engineering\scripts\sql_review_subagent_orchestrator.py plan `
  --review-json <batch-root>\sql_review_base.json `
  --out-dir <batch-root>\_subagent_review `
  --target-shards 10
```

The planner writes:

- `subagent_plan.json`
- `evidence_shard_001.json`, `evidence_shard_002.json`, ...
- `subagent_prompt_001.md`, `subagent_prompt_002.md`, ...
- expected result paths `product_views_shard_001.json`, ...

Each `plan` run removes stale `product_views_shard_*.json` files in the output directory and stamps a fresh `plan_id` into `subagent_plan.json`, each evidence shard, and each worker prompt. `merge` rejects shard result files whose `plan_id` or `shard_id` does not match the current plan.

Spawn one worker per shard. Each worker prompt should be the corresponding `subagent_prompt_XXX.md`. Workers must not edit anything except their shard result JSON.

Merge after workers finish:

```powershell
python .\sql-engineering\scripts\sql_review_subagent_orchestrator.py merge `
  --review-json <batch-root>\sql_review_base.json `
  --views-dir <batch-root>\_subagent_review `
  --output-json <batch-root>\sql_review.json `
  --output-html <batch-root>\sql_review.html
```

The merge blocks unless every review item has a valid product view. Use `--allow-partial` only for debugging.

## Main-Agent Spawn Pattern

When the `multi_agent_v1.spawn_agent` tool is available, spawn workers like this:

- `agent_type`: `worker`
- `fork_context`: `false`
- prompt: content of one `subagent_prompt_XXX.md`
- ownership: exactly one `product_views_shard_XXX.json`

Do not ask workers to run the whole review. Do not give two workers the same result path.

While workers run, the main agent should do non-overlapping work:

- inspect deterministic findings;
- prepare merge/validation commands;
- update docs/tests;
- review completed worker outputs as they arrive.

## Worker Output Contract

Each worker writes strict JSON:

```json
{
  "plan_id": "same plan_id as evidence.plan_id",
  "shard_id": "same shard_id as evidence.shard_id",
  "items": [
    {
      "path": "same path as evidence.path",
      "product_view": {}
    }
  ]
}
```

The product view must satisfy the same requirements as `references/sql-review-product-agent.md`:

- `event_contracts` covers every event candidate.
- `metric_cards` covers every metric candidate.
- `metric_cards[].key_conditions` and `metric_summary_table[].key_conditions` expose critical ID/range/mode/duration boundaries.
- `risk_register`, `event_index`, `metric_summary_table`, `review_actions` are present when relevant.
- `metric_cards[].event_refs` and `metric_cards[].risk_refs` are present when relevant.
- no generic filler.
- conflicts explain current SQL, expected口径, difference, impact, affected metrics, and action.

## Failure Handling

If a worker fails:

1. Reuse the same shard prompt with a new worker.
2. If only one item is invalid, split that shard into smaller evidence files or ask one worker to rewrite only the invalid item.
3. Do not merge partial output into the final `sql_review.html` unless the user explicitly accepts a partial debug artifact.

If many workers fail due to rate limits, reduce `--target-shards` or use script fallback:

```powershell
$env:SQL_REVIEW_PRODUCT_AGENT_PARALLELISM="10"
$env:SQL_REVIEW_PRODUCT_AGENT_BATCH_SIZE="2"
```

## Design Rule

Desktop sub-agents are an orchestration layer, not a replacement for deterministic review gates. Scripts still own evidence extraction, saved-rule checks, performance/privacy/deployment gates, schema validation, and final rendering. Workers only produce product semantic closure for assigned evidence shards.
