#!/usr/bin/env python3
"""Plan and merge Codex Desktop sub-agent product-view review shards.

This helper does not spawn Codex Desktop agents by itself. Spawning is a host
tool capability owned by the parent Codex agent. The helper creates deterministic
shard evidence files and merge targets so left-side sub-agents can work on
disjoint product-view JSON slices without racing on the main review outputs.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sql_review  # noqa: E402
import sql_review_product_agent as product_agent  # noqa: E402


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _items(payload: dict) -> list[dict]:
    return [item for item in payload.get("items", []) if isinstance(item, dict)]


def _evidence_for_item(item: dict) -> dict:
    evidence = item.get("product_review_evidence")
    if not isinstance(evidence, dict):
        raise ValueError(f"review item missing product_review_evidence: {item.get('path') or item.get('name')}")
    return product_agent._evidence_for_llm(evidence)  # Internal tool contract for compact LLM evidence.


def _result_name(index: int) -> str:
    return f"product_views_shard_{index:03d}.json"


def _evidence_name(index: int) -> str:
    return f"evidence_shard_{index:03d}.json"


def _prompt_name(index: int) -> str:
    return f"subagent_prompt_{index:03d}.md"


def _chunk_items(items: list[dict], *, shard_size: int, target_shards: int) -> list[list[dict]]:
    if shard_size > 0:
        return [items[start : start + shard_size] for start in range(0, len(items), shard_size)]
    shard_count = min(max(1, target_shards), len(items))
    base, extra = divmod(len(items), shard_count)
    chunks: list[list[dict]] = []
    start = 0
    for index in range(shard_count):
        size = base + (1 if index < extra else 0)
        chunks.append(items[start : start + size])
        start += size
    return [chunk for chunk in chunks if chunk]


def build_prompt(evidence_path: Path, result_path: Path, *, plan_id: str, shard_id: str) -> str:
    design_record = SKILL_ROOT / "references" / "sql-review-design-record.md"
    product_agent_ref = SKILL_ROOT / "references" / "sql-review-product-agent.md"
    return f"""# SQL Review Sub-Agent Task

You are a Codex Desktop sub-agent working on one shard of SQL Review Product View.

Rules:

1. Do not edit any file except the output JSON path below.
2. Read the shard evidence JSON:
   `{evidence_path}`
3. Read these skill references before writing output:
   - `{design_record}`
   - `{product_agent_ref}`
4. For each `batch_items[]` evidence object, produce one metric-centered `product_view`.
5. Output strict JSON only to:
   `{result_path}`
6. Output shape must be:

```json
{{
  "plan_id": "{plan_id}",
  "shard_id": "{shard_id}",
  "items": [
    {{
      "path": "same path as evidence.path",
      "product_view": {{}}
    }}
  ]
}}
```

Product View requirements:

- Include `event_contracts` when evidence has `event_contract_candidates`.
- Include metric cards covering every evidence metric candidate.
- Include `key_conditions` for critical ID, range, mode, mission, reward, item, and duration boundaries.
- Explain conflicts concretely. Do not write vague text such as "存在冲突".
- Use `risk_register`, `event_index`, `metric_summary_table`, `review_actions`, and per-metric `event_refs` / `risk_refs` when relevant.
- Do not output generic filler like "SQL 最终输出字段；需要结合业务需求确认展示意义。"
- Keep product-facing prose free of SQL aliases such as `uu.izoneareaid`; use business field names such as `iZoneAreaID`.
- Keep SQL expressions as evidence refs, not as the product narrative.

After writing the JSON file, reply briefly with the shard id and number of product views written.
"""


def command_plan(args: argparse.Namespace) -> int:
    review_json = Path(args.review_json).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale_result in out_dir.glob("product_views_shard_*.json"):
        stale_result.unlink()
    payload = _read_json(review_json)
    items = _items(payload)
    if not items:
        raise SystemExit(f"no review items found in {review_json}")
    shard_size = max(0, int(args.shard_size))
    target_shards = max(1, int(args.target_shards))
    plan_id = uuid.uuid4().hex
    plan_items: list[dict] = []
    chunks = _chunk_items(items, shard_size=shard_size, target_shards=target_shards)
    for shard_index, chunk in enumerate(chunks, 1):
        shard_id = f"shard_{shard_index:03d}"
        evidence_path = out_dir / _evidence_name(shard_index)
        result_path = out_dir / _result_name(shard_index)
        prompt_path = out_dir / _prompt_name(shard_index)
        shard_payload = {
            "batch_contract": "sql_review_desktop_subagent_v1",
            "plan_id": plan_id,
            "source_review_json": str(review_json),
            "shard_id": shard_id,
            "expected_output_path": str(result_path),
            "batch_items": [_evidence_for_item(item) for item in chunk],
        }
        _write_json(evidence_path, shard_payload)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(
            build_prompt(evidence_path, result_path, plan_id=plan_id, shard_id=shard_id),
            encoding="utf-8",
        )
        plan_items.append(
            {
                "shard_id": shard_id,
                "evidence_path": str(evidence_path),
                "prompt_path": str(prompt_path),
                "result_path": str(result_path),
                "item_count": len(chunk),
                "paths": [str(item.get("path") or item.get("name") or "") for item in chunk],
            }
        )
    plan = {
        "contract": "sql_review_desktop_subagent_plan_v1",
        "plan_id": plan_id,
        "source_review_json": str(review_json),
        "out_dir": str(out_dir),
        "shard_size": shard_size or None,
        "target_shards": target_shards,
        "total_items": len(items),
        "shards": plan_items,
    }
    _write_json(out_dir / "subagent_plan.json", plan)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def _candidate_items(payload: dict) -> list[dict]:
    rows = product_agent._batch_candidate_items(payload)
    if rows:
        return rows
    if isinstance(payload.get("path"), str) and isinstance(payload.get("product_view"), dict):
        return [payload]
    return []


def _result_specs(views_dir: Path) -> list[dict]:
    plan_path = views_dir / "subagent_plan.json"
    if plan_path.exists():
        plan = _read_json(plan_path)
        result_specs: list[dict] = []
        plan_id = str(plan.get("plan_id") or "").strip()
        for shard in plan.get("shards", []):
            if not isinstance(shard, dict) or not shard.get("result_path"):
                continue
            result_path = Path(str(shard["result_path"]))
            if not result_path.is_absolute():
                result_path = views_dir / result_path
            result_specs.append(
                {
                    "path": result_path,
                    "plan_id": plan_id,
                    "shard_id": str(shard.get("shard_id") or "").strip(),
                }
            )
        return sorted(result_specs, key=lambda item: str(item["path"]))
    return [{"path": path, "plan_id": "", "shard_id": ""} for path in sorted(views_dir.glob("product_views_shard_*.json"))]


def _load_product_views(views_dir: Path) -> dict[str, dict]:
    views: dict[str, dict] = {}
    for spec in _result_specs(views_dir):
        path = Path(spec["path"])
        if not path.exists():
            raise FileNotFoundError(f"missing expected sub-agent result file: {path}")
        payload = _read_json(path)
        expected_plan_id = str(spec.get("plan_id") or "").strip()
        expected_shard_id = str(spec.get("shard_id") or "").strip()
        if expected_plan_id and payload.get("plan_id") != expected_plan_id:
            raise ValueError(f"{path} has stale or missing plan_id; expected {expected_plan_id}")
        if expected_shard_id and payload.get("shard_id") != expected_shard_id:
            raise ValueError(f"{path} has stale or missing shard_id; expected {expected_shard_id}")
        for row in _candidate_items(payload):
            view = row.get("product_view") if isinstance(row.get("product_view"), dict) else row
            key = str(row.get("path") or row.get("item_path") or view.get("path") or "").strip()
            if not key:
                raise ValueError(f"{path} contains a product_view without path")
            if key in views:
                raise ValueError(f"duplicate product_view for {key}: {path}")
            views[key] = view
    return views


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _join(values: Any) -> str:
    return "、".join(str(value) for value in _as_list(values) if str(value).strip())


def _md(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", "<br>")


def _product_confirmation_texts(product_view: dict) -> list[str]:
    texts: list[str] = []
    try:
        texts.extend(sql_review.product_view_confirmation_texts(product_view))
    except Exception:  # noqa: BLE001
        pass
    for item in _as_list(product_view.get("unknowns_to_confirm")):
        text = str(item).strip()
        if text:
            texts.append(text)
    return list(dict.fromkeys(texts))[:12]


def _json_product_next_focus(product_view: dict) -> str:
    for action in _as_list(product_view.get("review_actions")):
        if isinstance(action, dict) and action.get("action"):
            return str(action.get("action"))
    for risk in _as_list(product_view.get("risk_register")):
        if isinstance(risk, dict):
            title = str(risk.get("title") or risk.get("risk_id") or "风险待确认").strip()
            action = str(risk.get("action") or risk.get("impact") or "").strip()
            return title + (f"：{action}" if action else "")
    confirmations = _product_confirmation_texts(product_view)
    if confirmations:
        return confirmations[0]
    for card in _as_list(product_view.get("metric_cards")):
        if isinstance(card, dict):
            for confirmation in _as_list(card.get("metric_confirmations")):
                if isinstance(confirmation, dict) and confirmation.get("question"):
                    return str(confirmation.get("question"))
    return str(product_view.get("one_sentence") or product_view.get("business_question") or "查看产品视角指标口径。")


def _refresh_payload_derivatives(payload: dict) -> None:
    focus_by_path: dict[str, str] = {}
    for item in _items(payload):
        product_view = item.get("product_view") if isinstance(item.get("product_view"), dict) else {}
        next_focus = _json_product_next_focus(product_view)
        item["next_focus"] = next_focus
        item["product_digest"] = {
            "confirmations": _product_confirmation_texts(product_view),
            "semantic_review_status": product_view.get("semantic_review_status", ""),
        }
        focus_by_path[str(item.get("path") or item.get("name") or "")] = next_focus
    if isinstance(payload.get("action_queue"), list):
        for queue_item in payload["action_queue"]:
            if not isinstance(queue_item, dict):
                continue
            actions = [
                focus_by_path[path]
                for path in _as_list(queue_item.get("files"))
                if path in focus_by_path and focus_by_path[path]
            ]
            queue_item["top_actions"] = list(dict.fromkeys(actions))[:8]


def _render_risk_markdown(product_view: dict) -> list[str]:
    rows = _as_list(product_view.get("risk_register"))
    if not rows:
        return ["- 未识别共享风险。", ""]
    lines = [
        "| 风险 | 等级 | SQL 当前 | 标准/期望 | 差异 | 影响 | 动作 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = f"{row.get('risk_id', '')} {row.get('title', '')}".strip()
        lines.append(
            f"| {_md(title)} | {_md(row.get('severity', ''))} | {_md(row.get('sql_current', ''))} | "
            f"{_md(row.get('expected_or_standard', ''))} | {_md(row.get('difference', ''))} | "
            f"{_md(row.get('impact', ''))} | {_md(row.get('action', ''))} |"
        )
    return lines + [""]


def _render_metric_summary_markdown(product_view: dict) -> list[str]:
    rows = _as_list(product_view.get("metric_summary_table"))
    if not rows:
        return ["- 未识别指标总表。", ""]
    lines = [
        "| 指标 | 类型 | 计算 | 关键口径 | 分子 | 分母 | 去重 | 粒度 | 事件 | 风险 | 状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"| {_md(row.get('metric_name', ''))} | {_md(row.get('metric_type', ''))} | "
            f"{_md(row.get('calculation', ''))} | {_md(_join(row.get('key_conditions')))} | "
            f"{_md(row.get('numerator', ''))} | {_md(row.get('denominator', ''))} | "
            f"{_md(row.get('dedup_key', ''))} | {_md(row.get('grain', ''))} | "
            f"{_md(_join(row.get('event_refs')))} | {_md(_join(row.get('risk_refs')))} | "
            f"{_md(row.get('review_status', ''))} |"
        )
    return lines + [""]


def _render_event_markdown(product_view: dict) -> list[str]:
    rows = _as_list(product_view.get("event_contracts"))
    if not rows:
        return ["- 未识别事件口径契约。", ""]
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = f"{row.get('event_id', '')} {row.get('event_name', '')}".strip()
        lines.extend(
            [
                f"- **{_md(title)}**",
                f"  - 来源: {_md(_join(row.get('source_logs_or_tables')))}",
                f"  - 成立条件: {_md(row.get('event_condition', ''))}",
                f"  - 统计对象: {_md(row.get('statistic_object', ''))}",
                f"  - ID/映射: {_md(row.get('id_or_mapping', ''))}",
            ]
        )
    return lines + [""]


def _render_metric_cards_markdown(product_view: dict) -> list[str]:
    rows = _as_list(product_view.get("metric_cards"))
    if not rows:
        return ["- 未识别指标卡。", ""]
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.extend(
            [
                f"#### {_md(row.get('metric_name', '未命名指标'))}",
                "",
                f"- 指标含义: {_md(row.get('business_meaning', ''))}",
                f"- 指标类型: {_md(row.get('metric_type', ''))}",
                f"- 关键口径条件: {_md(_join(row.get('key_conditions')) or '未识别')}",
                f"- 最终计算: {_md(row.get('calculation', ''))}",
                f"- 分子: {_md(row.get('numerator', ''))}",
                f"- 分母: {_md(row.get('denominator', ''))}",
                f"- 去重对象: {_md(row.get('dedup_key', ''))}",
                f"- 事件引用: {_md(_join(row.get('event_refs')))}",
                f"- 风险引用: {_md(_join(row.get('risk_refs')))}",
                "",
            ]
        )
    return lines


def render_product_markdown(payload: dict) -> str:
    lines = [
        "# SQL Review Product View",
        "",
        f"- batch_root: `{payload.get('batch_root', '')}`",
        f"- review_entry: `{payload.get('review_entry', 'SQL审查')}`",
        "- renderer: `sql_review_subagent_orchestrator merge`",
        "",
        "## 处理队列",
        "",
    ]
    for row in _as_list(payload.get("action_queue")):
        if isinstance(row, dict):
            lines.append(f"- {row.get('label') or row.get('bucket')}: {row.get('count', 0)} 个；优先动作：{_join(row.get('top_actions')) or '无'}")
    if not _as_list(payload.get("action_queue")):
        lines.append("- 无")
    for item in _items(payload):
        product_view = item.get("product_view") if isinstance(item.get("product_view"), dict) else {}
        lines.extend(
            [
                "",
                f"## {item.get('name') or item.get('path')}",
                "",
                f"- path: `{item.get('path', '')}`",
                f"- semantic_review_status: `{product_view.get('semantic_review_status', '')}`",
                f"- 下一步重点: {_md(item.get('next_focus', ''))}",
                "",
                "### 风险登记表",
                "",
            ]
        )
        lines.extend(_render_risk_markdown(product_view))
        lines.extend(["### 指标总表", ""])
        lines.extend(_render_metric_summary_markdown(product_view))
        lines.extend(["### 事件口径契约", ""])
        lines.extend(_render_event_markdown(product_view))
        lines.extend(["### 指标卡", ""])
        lines.extend(_render_metric_cards_markdown(product_view))
    return "\n".join(lines).rstrip() + "\n"


def render_summary_markdown(payload: dict) -> str:
    lines = [
        "# SQL Review Product Summary",
        "",
        f"- batch_root: `{payload.get('batch_root', '')}`",
        f"- sql_file_count: `{payload.get('summary', {}).get('sql_file_count', len(_items(payload)))}`",
        "- renderer: `sql_review_subagent_orchestrator merge`",
        "",
        "## SQL Files",
        "",
        "| SQL File | Semantic Status | Next Focus |",
        "|---|---|---|",
    ]
    for item in _items(payload):
        product_view = item.get("product_view") if isinstance(item.get("product_view"), dict) else {}
        lines.append(
            f"| `{_md(item.get('path', ''))}` | `{_md(product_view.get('semantic_review_status', ''))}` | {_md(item.get('next_focus', ''))} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def command_merge(args: argparse.Namespace) -> int:
    review_json = Path(args.review_json).resolve()
    views_dir = Path(args.views_dir).resolve()
    output_json = Path(args.output_json).resolve() if args.output_json else review_json
    output_html = Path(args.output_html).resolve() if args.output_html else output_json.with_suffix(".html")
    payload = _read_json(review_json)
    views = _load_product_views(views_dir)
    if not views:
        raise SystemExit(f"no product_views_shard_*.json files found in {views_dir}")

    missing: list[str] = []
    invalid: list[str] = []
    accepted = 0
    for item in _items(payload):
        key = str(item.get("path") or item.get("name") or "").strip()
        evidence = item.get("product_review_evidence")
        raw_view = views.get(key)
        if not isinstance(evidence, dict):
            invalid.append(f"{key}: missing product_review_evidence")
            continue
        if not isinstance(raw_view, dict):
            missing.append(key)
            continue
        view, note = product_agent._accept_llm_product_view(
            raw_view,
            evidence,
            mode="llm",
            cache_dir=None,
        )
        if not view:
            invalid.append(f"{key}: {note}")
            continue
        view["semantic_review_status"] = "subagent_llm"
        note = str(view.get("semantic_review_note") or "").strip()
        view["semantic_review_note"] = "Codex Desktop sub-agent product semantic review." + (f" {note}" if note else "")
        item["product_view"] = view
        accepted += 1

    if (missing or invalid) and not args.allow_partial:
        message = {
            "accepted": accepted,
            "missing": missing,
            "invalid": invalid,
        }
        raise SystemExit("subagent merge blocked:\n" + json.dumps(message, ensure_ascii=False, indent=2))

    _refresh_payload_derivatives(payload)
    _write_json(output_json, payload)
    output_html.write_text(sql_review.render_html_viewer(payload), encoding="utf-8")
    product_output = ""
    summary_output = ""
    if not args.no_markdown:
        product_path = output_json.parent / args.product_name
        summary_path = output_json.parent / args.summary_name
        product_path.write_text(render_product_markdown(payload), encoding="utf-8")
        summary_path.write_text(render_summary_markdown(payload), encoding="utf-8")
        product_output = str(product_path)
        summary_output = str(summary_path)
    print(
        json.dumps(
            {
                "accepted": accepted,
                "missing": missing,
                "invalid": invalid,
                "output_json": str(output_json),
                "output_html": str(output_html),
                "output_product_markdown": product_output,
                "output_summary_markdown": summary_output,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Create sub-agent evidence shards and prompts from sql_review.json")
    plan.add_argument("--review-json", required=True, help="Base sql_review.json containing product_review_evidence")
    plan.add_argument("--out-dir", required=True, help="Directory for shard evidence, prompts, and results")
    plan.add_argument("--target-shards", type=int, default=10, help="Target number of Codex Desktop sub-agent shards")
    plan.add_argument("--shard-size", type=int, default=0, help="Review items per shard. 0 means auto-split across --target-shards")
    plan.set_defaults(func=command_plan)

    merge = subparsers.add_parser("merge", help="Merge sub-agent product_view JSON files into final review JSON/HTML")
    merge.add_argument("--review-json", required=True, help="Base sql_review.json containing product_review_evidence")
    merge.add_argument("--views-dir", required=True, help="Directory containing product_views_shard_*.json")
    merge.add_argument("--output-json", default="", help="Output review JSON. Defaults to overwriting --review-json")
    merge.add_argument("--output-html", default="", help="Output HTML. Defaults to output JSON path with .html suffix")
    merge.add_argument("--product-name", default="sql_review_product.md", help="Product Markdown filename written next to --output-json")
    merge.add_argument("--summary-name", default="sql_review_summary.md", help="Summary Markdown filename written next to --output-json")
    merge.add_argument("--no-markdown", action="store_true", help="Do not rewrite product/summary Markdown outputs")
    merge.add_argument("--allow-partial", action="store_true", help="Write outputs even when some views are missing/invalid")
    merge.set_defaults(func=command_merge)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
