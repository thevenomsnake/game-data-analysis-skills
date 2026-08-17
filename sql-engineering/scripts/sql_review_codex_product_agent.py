#!/usr/bin/env python3
"""Codex-backed product-view reviewer for SQL review evidence bundles."""

from __future__ import annotations

import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _compact(value: Any, limit: int = 1600) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("Codex output did not contain a JSON object")


def _build_prompt(evidence: dict) -> str:
    schema_hint = {
        "execution_evidence": {},
        "business_story_cards": [{"title": "", "body": "", "evidence_ref": ""}],
        "walkthrough_sections": [
            {
                "title": "",
                "paragraphs": [],
                "table": {"headers": [], "rows": []},
                "bullets": [],
            }
        ],
        "metric_path_cards": [
            {
                "metric_name": "",
                "title": "",
                "body": "",
                "formula": "",
                "base": "",
                "caveat": "",
                "confidence": "high|medium|low",
            }
        ],
        "output_contract": {"fields": [], "result_columns": [], "product_check": "", "warning": ""},
        "event_contracts": [
            {
                "event_id": "E1",
                "event_name": "",
                "event_family": "",
                "source_logs_or_tables": [],
                "event_condition": "",
                "id_or_mapping": "",
                "statistic_object": "",
                "first_or_final_rule": "",
                "join_or_backfill_rule": "",
                "source_fields": [],
                "product_interpretation": "",
                "business_risk": "",
                "sql_evidence_refs": [],
                "sql_evidence": [{"ref": "", "snippet": ""}],
                "confidence": "high|medium|low",
            }
        ],
        "event_index": [
            {
                "event_id": "E1",
                "event_name": "",
                "event_condition": "",
                "statistic_object": "",
                "source_logs_or_tables": [],
                "source_fields": [],
                "risk_summary": "",
                "confidence": "high|medium|low",
            }
        ],
        "risk_register": [
            {
                "risk_id": "R1",
                "title": "",
                "severity": "high|medium|low",
                "description": "",
                "conflict_object": "",
                "sql_current": "",
                "expected_or_standard": "",
                "difference": "",
                "impact": "",
                "affected_metrics": [],
                "action": "",
                "evidence_refs": [],
            }
        ],
        "metric_summary_table": [
            {
                "metric_name": "",
                "metric_type": "",
                "business_meaning": "",
                "calculation": "",
                "key_conditions": [],
                "numerator": "",
                "denominator": "",
                "dedup_key": "",
                "grain": "",
                "event_refs": [],
                "risk_refs": [],
                "confidence": "high|medium|low",
                "review_status": "",
            }
        ],
        "review_actions": [
            {"action_id": "A1", "source_ref": "R1", "owner_hint": "产品/DA", "action": "", "why": ""}
        ],
        "metric_overview": [
            {
                "metric_name": "",
                "metric_type": "",
                "review_status": "",
                "main_risk": "",
                "confidence": "high|medium|low",
                "confirmation_count": 0,
            }
        ],
        "metric_cards": [
            {
                "metric_name": "",
                "business_meaning": "",
                "metric_type": "",
                "calculation": "",
                "key_conditions": [],
                "numerator": "",
                "denominator": "",
                "dedup_key": "",
                "aggregation_dimensions": [],
                "row_grain_explanation": "",
                "source_logs_fields": [],
                "metric_filters": [],
                "standard_rule_alignment": "",
                "metric_confirmations": [],
                "sql_evidence_refs": [],
                "event_refs": [],
                "risk_refs": [],
                "risk_notes": [],
                "confidence": "high|medium|low",
            }
        ],
        "common_filters": [],
        "shared_confirmations": [],
        "evidence_sections": [],
    }
    is_batch = isinstance(evidence.get("batch_items"), list)
    output_contract = (
        {"items": [{"path": "", "product_view": schema_hint}]}
        if is_batch
        else schema_hint
    )
    extra_batch_rule = (
        "这是批次级输入。必须返回 {\"items\":[...]}，每个 items[] 使用输入 batch_items[] 的 path 原样回填，并在 product_view 中放上述结构。"
        if is_batch
        else "这是单 SQL 输入。直接返回上述 product_view JSON object。"
    )
    return "\n".join(
        [
            "你是 SQL Review 的产品口径审查员，不是 SQL 老师。",
            "你只读取下面的 evidence bundle，不能访问外部文件，不能输出 HTML/Markdown。",
            "任务：把脚本证据收拢成产品同学能直接读的指标口径审查 JSON。",
            "",
            "核心原则：",
            "1. 产品视角只讲最终指标是否对、Base/分母/分子/去重/聚合维度是否清楚。",
            "2. CTE、JOIN、WHERE、字段血缘只能作为证据 refs 或折叠证据，不要成为主体长文。",
            "3. 当前输入 SQL 是唯一审查主体；执行项目、结果角色和验证范围只采用 execution_evidence 中的显式事实。",
            "4. 不确定就给 low confidence 和具体 metric-bound confirmation，不要编造业务含义。",
            "5. 严禁泛泛说“需要结合业务需求确认”；每个待确认项必须绑定 metric_name、reason、evidence_ref。",
            "6. 输出中文，短句，结论先行；不要复述大段 SQL 表达式。",
            "6a. 如果说“冲突/不符合口径/边界不一致”，必须同时写清：冲突对象、SQL 当前取值/算法、项目规则或标准口径期望、具体差异、为什么会影响哪些指标。不能只写“存在冲突”或“不符合口径”。",
            "6b. 重要风险必须收敛到 risk_register[]，并给 risk_id。每张受影响的 metric_cards[] 和 metric_summary_table[] 必须用 risk_refs 引用风险编号，而不是在每个指标里重复长段风险说明。",
            "6c. 行为/事件口径必须收敛到 event_contracts[] 和 event_index[]，并给 event_id。每张相关 metric_cards[] 和 metric_summary_table[] 必须用 event_refs 引用事件编号。",
            "6d. 每张 metric_cards[] 和 metric_summary_table[] 必须输出 key_conditions[]，把关键 ID、范围、GameMode、区服、任务/奖励/道具、时长、分桶、完成/领取/解锁条件前置为短句。",
            "7. source_logs_fields 必须尽量溯源到 evidence 中的本源日志、物理表和真实业务字段；不要把 CTE 名、表别名、临时层名称、formula_expression 或完整 SQL 表达式写进产品字段。",
            "8. source_steps 和 lineage 是给你理解 CTE/alias 的材料，不是给产品页照抄的内容；如果无法静态溯源到本源字段，就写“本源字段未能静态溯源，完整血缘见代码视角”，并给该 metric 绑定确认项。",
            "9. common_filters、metric_filters、evidence_sections 也要产品化表达：说业务范围、ID/分桶/边界和审核重点；原始 WHERE/JOIN/CASE 条件留给代码视角。",
            "10. evidence.event_contract_candidates 是强制收口材料。每个候选都必须产出一条 event_contracts[]，用产品语言说明：这是什么事件/口径、来自什么日志/物理表、哪些字段和条件让事件成立、ID/档位/任务范围是什么、按什么对象去重、是否取首次/最终、如何回挂或归因，以及对应 SQL 证据 ref/snippet。",
            "11. 行为指标不能只写抽象动词。必须按 event_contract_candidates 原样说明事件成立字段与条件、ID/映射、统计对象、首次或最终规则、关联或归因方式；证据缺失时记录具体风险，不得补写项目知识。",
            "12. event_contracts.sql_evidence_refs 必须引用输入候选里的 ref；sql_evidence 可以放短 snippet。不要输出大段 SQL，但要让产品 reviewer 能知道证据来自哪一类 SQL 事实。",
            "13. 不要在产品输出里说“输入候选”“候选中不一致”“模型看到”等内部话术。ID/映射必须按 evidence.id_fields 的 label-value 配对原样表达，不要把不同 label 的值混成冲突；只有 SQL 证据与显式规则或资料明确矛盾时才写产品风险。",
            "",
            extra_batch_rule,
            "",
            "必须返回一个 JSON object，且只能返回 JSON。结构参考：",
            json.dumps(output_contract, ensure_ascii=False, indent=2),
            "",
            "metric_cards 必须覆盖 evidence.metric_cards 里的每个指标候选。",
            "如果某个输入 item 的 event_contract_candidates 非空，product_view.event_contracts 数量不能少于候选数量；否则这次 review 会被脚本拒绝。",
            "walkthrough_sections 用来写模型收口段落，建议包含：审查结论、口径路径、结果证据限制。",
            "business_story_cards 建议包含：它回答什么、Base / 分母、人群与关联、分组/分桶、结果证据。",
            "risk_register 是产品页第一优先级：凡是业务范围、ID/映射、分母、去重或执行证据存在冲突或待确认，都必须在这里写成可执行问题。字段不要空：sql_current 写 SQL 当前做法，expected_or_standard 写规则/标准期望，difference 写二者差异，impact 写影响哪些指标和为什么，action 写下一步确认方式。",
            "metric_summary_table 是产品页第二优先级：每行必须让 reviewer 不进 SQL 就知道这个指标怎么算、分子分母是什么、依赖哪个事件、挂了哪些风险。",
            "",
            "Evidence bundle:",
            json.dumps(evidence, ensure_ascii=False, indent=2),
        ]
    )


def main() -> int:
    try:
        evidence = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid evidence JSON: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    codex_bin = os.environ.get("SQL_REVIEW_CODEX_BIN") or shutil.which("codex")
    if not codex_bin:
        print("codex executable not found", file=sys.stderr)
        return 127
    timeout = int(os.environ.get("SQL_REVIEW_CODEX_AGENT_TIMEOUT", "600"))
    cwd = os.environ.get("SQL_REVIEW_CODEX_CWD") or str(Path.cwd())
    output_encoding = os.environ.get("SQL_REVIEW_CODEX_OUTPUT_ENCODING") or locale.getpreferredencoding(False)
    prompt = _build_prompt(evidence).encode("utf-8", errors="replace").decode("utf-8")
    with tempfile.TemporaryDirectory(prefix="sql-review-codex-agent-") as tmpdir:
        output_path = Path(tmpdir) / "last_message.txt"
        command = [
            codex_bin,
            "-c",
            f"model_reasoning_effort=\"{os.environ.get('SQL_REVIEW_CODEX_REASONING_EFFORT', 'low')}\"",
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-rules",
            "-C",
            cwd,
            "--color",
            "never",
            "-o",
            str(output_path),
            "-",
        ]
        model = os.environ.get("SQL_REVIEW_CODEX_MODEL", "").strip()
        if model:
            command[1:1] = ["-m", model]
        proc = subprocess.run(
            command,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if output_path.exists():
            raw_output = output_path.read_bytes()
            try:
                output_text = raw_output.decode("utf-8")
            except UnicodeDecodeError:
                output_text = raw_output.decode(output_encoding, errors="replace")
        else:
            output_text = proc.stdout
        if proc.returncode != 0:
            print(_compact(proc.stderr or proc.stdout or f"codex exit={proc.returncode}"), file=sys.stderr)
            return proc.returncode
        try:
            payload = _extract_json_object(output_text)
        except ValueError as exc:
            print(f"{exc}: {_compact(output_text)}", file=sys.stderr)
            return 3
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
