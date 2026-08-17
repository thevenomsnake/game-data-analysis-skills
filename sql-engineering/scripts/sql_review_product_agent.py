#!/usr/bin/env python3
"""Generate metric-centered product review views from SQL review evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PRODUCT_REVIEW_VERSION = "sql_review_product_agent_v9"
FORBIDDEN_PRODUCT_PHRASES = [
    "SQL 最终输出字段；需要结合业务需求确认展示意义",
    "需要结合业务需求确认展示意义",
    "SQL最终输出字段",
    "SQL 最终输出字段",
    "Base 中字段",
]


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _compact(value: Any, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


PRODUCT_FIELD_DISPLAY_NAMES = {
    "izoneareaid": "iZoneAreaID",
    "gamesvrid": "GameSvrId",
    "gamemode": "GameMode",
    "gamemodeid": "GameMode",
    "battlesrvid": "BattleSrvId",
    "uniquebattleid": "UniqueBattleID",
    "vopenid": "vOpenID",
    "openid": "OpenID",
    "dteventtime": "dtEventTime",
    "dteventdate": "dtEventDate",
    "totalactiveduration": "TotalActiveDuration",
    "onlinetime": "OnlineTime",
    "matchduration": "MatchDuration",
    "battlemissionid": "BattleMissionId",
    "battlemissionsubid": "BattleMissionSubId",
    "battlemissioncomplete": "BattleMissionComplete",
    "battleitemid": "BattleItemId",
    "battleitemdelta": "BattleItemDelta",
    "templateid": "TemplateId",
    "deltavalue": "DeltaValue",
    "itemid": "ItemId",
    "propid": "PropId",
}


def _product_field_display_name(value: str) -> str:
    canonical = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).lower()
    return PRODUCT_FIELD_DISPLAY_NAMES.get(canonical, str(value or "").strip("`"))


def _strip_product_sql_aliases(value: str) -> str:
    text = str(value or "")

    def replace_alias(match: re.Match[str]) -> str:
        return _product_field_display_name(match.group("field"))

    text = re.sub(r"(?<![\w`])`?[A-Za-z_][\w]*`?\s*\.\s*`?(?P<field>[A-Za-z_][\w]*)`?", replace_alias, text)
    for label in sorted(set(PRODUCT_FIELD_DISPLAY_NAMES.values()), key=len, reverse=True):
        text = re.sub(
            rf"{re.escape(label)}(?:\s*[、,，]\s*{re.escape(label)})+",
            label,
            text,
        )
    return text


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _unique(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _compact(value, 500)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_text(value: Any, fallback: str = "", *, strip_aliases: bool = True) -> str:
    text = _compact(value)
    if strip_aliases:
        text = _strip_product_sql_aliases(text)
    if not text:
        return fallback
    for phrase in FORBIDDEN_PRODUCT_PHRASES:
        text = text.replace(phrase, "")
    return text.strip() or fallback


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 64) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


SQL_SOURCE_KEYWORDS = {
    "as",
    "case",
    "cast",
    "coalesce",
    "count",
    "date",
    "distinct",
    "else",
    "end",
    "from",
    "if",
    "in",
    "is",
    "max",
    "min",
    "null",
    "nullif",
    "over",
    "partition",
    "regexp_extract",
    "row_number",
    "sum",
    "then",
    "when",
}
TECHNICAL_SOURCE_TOKENS = {
    "cte",
    "field_expression",
    "formula_expression",
    "source_step",
    "source_tables",
}
KNOWN_BUSINESS_FIELDS = {
    "battleitem",
    "battleitemdelta",
    "battleitemid",
    "battleloginout",
    "battlelogout",
    "battlemission",
    "battlemissioncomplete",
    "battlemissionsubid",
    "battleloginout",
    "battlesrvid",
    "dteventtime",
    "gamemode",
    "izoneareaid",
    "totalactiveduration",
    "vopenid",
}


def _looks_like_sql_trace(value: Any) -> bool:
    text = str(value or "")
    lower = text.lower()
    if not text:
        return False
    if any(token in lower for token in TECHNICAL_SOURCE_TOKENS):
        return True
    if re.search(r"\b[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\b", lower):
        return True
    if re.search(r"\b(?:with|select|from|join|where|group\s+by|partition\s+by)\b", lower):
        return True
    if ":=" in text or "${" in text:
        return True
    if re.search(r"\b[a-z_][a-z0-9_]*\s*\(", lower) and any(op in text for op in ["(", ")"]):
        return True
    if re.search(r"[<>=*/+-]", text) and re.search(r"\b[a-zA-Z_][\w]*\b", text):
        return True
    return False


def _origin_identifiers(value: Any) -> list[str]:
    text = str(value or "")
    identifiers = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text)
    result: list[str] = []
    seen: set[str] = set()
    for identifier in identifiers:
        lower = identifier.lower()
        if lower in SQL_SOURCE_KEYWORDS or lower in TECHNICAL_SOURCE_TOKENS:
            continue
        if re.fullmatch(r"[abtps]\d*", lower):
            continue
        if "_" in identifier and identifier == identifier.lower() and lower not in KNOWN_BUSINESS_FIELDS:
            continue
        keep = (
            lower in KNOWN_BUSINESS_FIELDS
            or "openid" in lower
            or "duration" in lower
            or "gamemode" in lower
            or "srv" in lower
            or "zone" in lower
            or any(ch.isupper() for ch in identifier[1:])
        )
        if not keep:
            continue
        if identifier not in seen:
            seen.add(identifier)
            result.append(identifier)
    return result


def _product_source_field_text(value: Any, fallback: str = "完整字段血缘见代码视角") -> str:
    text = _safe_text(value)
    if not text:
        return fallback
    fields = _origin_identifiers(text)
    if _looks_like_sql_trace(text):
        return "、".join(fields[:10]) if fields else fallback
    return text


def _product_source_story(value: Any, fallback: str = "作为该指标的本源字段证据") -> str:
    text = _safe_text(value, fallback)
    if _looks_like_sql_trace(text):
        fields = _origin_identifiers(text)
        if fields:
            return "本源字段：" + "、".join(fields[:10])
        return fallback + "；完整 SQL 血缘见代码视角。"
    return text


def _payload_has_forbidden_text(payload: Any) -> bool:
    if isinstance(payload, dict):
        return any(_payload_has_forbidden_text(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_has_forbidden_text(value) for value in payload)
    if isinstance(payload, str):
        return any(phrase in payload for phrase in FORBIDDEN_PRODUCT_PHRASES)
    return False


GENERIC_PRODUCT_FILLER_PATTERNS = [
    re.compile(r"需要\s*(?:确认|补充)?\s*(?:「[^」]+」的)?\s*(?:分母|分子)\s*$"),
    re.compile(r"未识别明确(?:分母|分子|Base|业务定义|统计对象)\s*$"),
    re.compile(r"基于最终\s*SELECT\s*表达式计算"),
    re.compile(r"从最终输出字段反推"),
    re.compile(r"结合业务需求确认"),
]


def _is_generic_product_filler(value: Any) -> bool:
    text = _compact(value)
    if not text:
        return True
    compacted = re.sub(r"\s+", "", text)
    if any(pattern.search(text) or pattern.search(compacted) for pattern in GENERIC_PRODUCT_FILLER_PATTERNS):
        return True
    if re.search(r"\b(?:CAST|SUM|COUNT|NULLIF|COALESCE|CASE|SELECT)\s*\(", text, flags=re.I):
        # Product-facing fields should explain meaning first. Raw formulas belong in Code View/evidence.
        return True
    if re.search(r"\b[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\b", text, flags=re.I):
        return True
    return False


def _product_conclusion_status(payload: dict) -> str:
    risk_text = " ".join(
        str(_first_non_empty(item.get("severity"), item.get("title"), item.get("description")))
        for item in _list(payload.get("risk_register"))
        if isinstance(item, dict)
    ).lower()
    if "high" in risk_text or "冲突" in risk_text or "阻断" in risk_text:
        return "fail"
    if _list(payload.get("shared_confirmations")) or "medium" in risk_text:
        return "needs_confirmation"
    if str(payload.get("semantic_review_status") or "").lower() in {"llm", "llm_cached"}:
        return "pass"
    return "needs_confirmation"


def _ensure_conclusion(payload: dict) -> None:
    existing = payload.get("conclusion") if isinstance(payload.get("conclusion"), dict) else {}
    project_roles = _dict(payload.get("project_roles"))
    result_status = _first_non_empty(
        project_roles.get("evidence_status"),
        _dict(payload.get("execution_evidence")).get("result_status"),
        _dict(payload.get("output_contract")).get("warning"),
    )
    payload["conclusion"] = {
        "status": _safe_text(existing.get("status"), _product_conclusion_status(payload)),
        "business_question": _safe_text(
            _first_non_empty(existing.get("business_question"), payload.get("business_question"), payload.get("one_sentence")),
            "未识别明确业务问题",
        ),
        "analysis_pattern": _safe_text(_first_non_empty(existing.get("analysis_pattern"), payload.get("analysis_pattern")), "generic"),
        "base": _safe_text(_first_non_empty(existing.get("base"), payload.get("base")), "未识别明确 Base"),
        "grouping": _safe_text(_first_non_empty(existing.get("grouping"), payload.get("grouping")), "整体汇总或未识别明确分组粒度"),
        "evidence_status": _safe_text(existing.get("evidence_status"), result_status or "unknown"),
        "semantic_review_status": _safe_text(payload.get("semantic_review_status"), "unknown"),
    }


def _metric_type(metric: dict) -> str:
    name = str(metric.get("metric_name") or "")
    calc = str(metric.get("calculation_type") or "").lower()
    if calc == "average" or re.search(r"(均值|平均|人均|avg)", name, flags=re.I):
        return "均值指标"
    if re.search(r"(分组|分桶|区间|bucket|标签|阶段)", name, flags=re.I):
        return "分桶/维度字段"
    if calc == "ratio" or re.search(r"(占比|比例|比率|率|_rate|_ratio|_pct|percent)", name, flags=re.I):
        return "比率指标"
    if calc in {"count_distinct", "row_count"} or re.search(r"(人数|用户数|玩家数|uv|cnt|count)$", name, flags=re.I):
        return "计数指标"
    if calc in {"sum", "conditional_sum"} or re.search(r"(次数|数量|个数|总量|sum)", name, flags=re.I):
        return "汇总指标"
    if calc == "percentile" or re.search(r"(分位|p\d{1,2})", name, flags=re.I):
        return "分位指标"
    if re.search(r"(时长|耗时|duration|time)", name, flags=re.I):
        return "时长指标"
    return "派生指标"


def _infer_dedup_key(metric: dict) -> str:
    haystack = " ".join(
        [
            str(metric.get("formula_expression") or ""),
            str(metric.get("numerator_expression") or ""),
            str(metric.get("denominator_expression") or ""),
            " ".join(str(step.get("field_expression") or "") for step in _list(metric.get("source_steps")) if isinstance(step, dict)),
        ]
    )
    match = re.search(r"count\s*\(\s*distinct\s+([^)]+)\)", haystack, flags=re.I)
    if match:
        return _compact(match.group(1), 120)
    if re.search(r"\bvopenid\b|\bopenid\b", haystack, flags=re.I):
        return "vOpenID / OpenID"
    if _metric_type(metric) in {"比率指标", "计数指标"}:
        return "未从 SQL 证据识别明确去重对象"
    return "不适用"


def _source_logs_fields(metric: dict, evidence: dict) -> list[dict]:
    rows: list[dict] = []
    fallback_sources = _list(evidence.get("source_logs")) or _list(evidence.get("source_tables"))
    for step in _list(metric.get("source_steps")):
        if not isinstance(step, dict):
            continue
        rows.append(
            {
                "role": _safe_text(step.get("role"), "metric_value"),
                "source_logs_or_tables": _list(step.get("source_tables")) or fallback_sources,
                "field_expression": _product_source_field_text(
                    _first_non_empty(step.get("field_expression"), step.get("source_step")),
                    "完整字段血缘见代码视角",
                ),
                "business_story": _product_source_story(step.get("story"), "作为该指标的本源字段证据"),
                "group_by": _list(step.get("group_by")),
            }
        )
    if not rows:
        rows.append(
            {
                "role": "metric_value",
                "source_logs_or_tables": fallback_sources,
                "field_expression": "完整字段血缘见代码视角",
                "business_story": "未能在产品层静态溯源到单个本源字段；请看代码视角的血缘展开。",
                "group_by": _list(evidence.get("dimension_names")),
            }
        )
    return rows[:8]


def _metric_filters(metric: dict) -> list[dict]:
    rows: list[dict] = []
    for source_name in ["metric_business_filters", "metric_conditions", "join_business_filters"]:
        for item in _list(metric.get(source_name)):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "scope": source_name,
                    "label": _safe_text(_first_non_empty(item.get("label"), item.get("field"), item.get("business_effect")), "指标条件"),
                    "business_effect": _safe_text(item.get("business_effect"), _safe_text(item.get("condition"), "指标内条件")),
                    "condition": _safe_text(item.get("condition")),
                }
            )
    return rows[:12]


KEY_CONDITION_RE = re.compile(
    r"(ID|Id|id|GameMode|iZoneAreaID|BattleSrvId|UniqueBattleID|BattleMission|BattleItem|TemplateId|ItemId|PropId|"
    r"任务|奖励|道具|资源|模式|区服|战斗服|时长|分桶|区间|范围|完成|领取|接取|解锁|DeltaValue|TotalActiveDuration)",
    flags=re.I,
)


def _condition_fragments(text: str) -> list[str]:
    cleaned = _safe_text(text, "")
    if not cleaned:
        return []
    parts = [
        part.strip(" ，,")
        for part in re.split(r"[；;。]\s*", cleaned)
        if part.strip(" ，,")
    ]
    important = [part for part in parts if KEY_CONDITION_RE.search(part)]
    if important:
        return important
    return [cleaned] if KEY_CONDITION_RE.search(cleaned) else []


def _metric_key_conditions(filters: list[dict], *, limit: int = 6) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for index, item in enumerate(filters):
        if not isinstance(item, dict):
            continue
        label = _safe_text(_first_non_empty(item.get("label"), item.get("role")), "")
        text = _safe_text(
            _first_non_empty(
                item.get("business_effect"),
                item.get("business_story"),
                item.get("condition"),
                item.get("field_expression"),
                label,
            ),
            "",
        )
        for offset, fragment in enumerate(_condition_fragments(text)):
            if label and label not in {"指标条件", "metric_value", "event_contract", "source", "source_field", fragment} and label not in fragment:
                fragment = f"{label}：{fragment}"
            priority = 0 if KEY_CONDITION_RE.search(fragment) else 1
            candidates.append((priority * 1000 + index * 10 + offset, fragment))
    return _unique([text for _, text in sorted(candidates, key=lambda item: item[0])])[:limit]


def _standard_rule_alignment(metric: dict) -> str:
    checks = _list(metric.get("related_saved_rule_checks"))
    if not checks:
        return "未命中已保存标准口径；当前判断只基于 SQL 与结果证据。"
    conflicts = [item for item in checks if str(item.get("result") or "").lower() in {"conflict", "proposed_conflict"}]
    if conflicts:
        first = conflicts[0]
        return "自动核对发现冲突：" + _safe_text(_first_non_empty(first.get("message"), first.get("rule_summary"), first.get("title")), "保存口径与 SQL 证据不一致")
    matched = [item for item in checks if str(item.get("result") or "").lower() in {"matched", "covered", "pass"}]
    if matched:
        first = matched[0]
        return "已自动核对通过：" + _safe_text(_first_non_empty(first.get("message"), first.get("rule_summary"), first.get("title")), "SQL 证据覆盖保存口径")
    first = checks[0]
    return "自动核对证据不足：" + _safe_text(_first_non_empty(first.get("message"), first.get("rule_summary"), first.get("title")), "需要补充可解析 SQL 证据")


def _summarize_id_fields(rows: list[dict]) -> str:
    pieces: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        field = _safe_text(item.get("field"), "ID 字段")
        label = _safe_text(item.get("label"), "")
        values = [str(value) for value in _list(item.get("values"))]
        if values:
            shown = "、".join(values[:18])
            if len(values) > 18:
                shown += " 等"
            if label:
                pieces.append(f"{field} 中 {shown} 归为 {label}")
            else:
                pieces.append(f"{field} 命中 {shown}")
        elif label:
            pieces.append(f"{field} 映射为 {label}")
    return "；".join(pieces)


def _summarize_event_conditions(rows: list[dict]) -> str:
    pieces: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        condition = _safe_text(item.get("sql_condition"), "")
        meaning = _safe_text(item.get("business_meaning_hint"), "")
        if condition and meaning:
            pieces.append(f"{condition}：{meaning}")
        elif condition:
            pieces.append(condition)
    return "；".join(pieces)


def _event_sql_refs(candidate: dict) -> list[str]:
    refs: list[Any] = []
    for item in _list(candidate.get("sql_evidence")):
        if isinstance(item, dict):
            refs.append(_first_non_empty(item.get("ref"), item.get("snippet")))
        else:
            refs.append(item)
    for item in _list(candidate.get("event_condition_candidates")):
        if isinstance(item, dict):
            for evidence in _list(item.get("sql_evidence")):
                if isinstance(evidence, dict):
                    refs.append(_first_non_empty(evidence.get("ref"), evidence.get("snippet")))
    for item in _list(candidate.get("id_fields")):
        if isinstance(item, dict):
            refs.append(_first_non_empty(item.get("evidence_ref"), item.get("field")))
    return _unique(refs)[:20]


def _fallback_event_contracts(evidence: dict) -> list[dict]:
    rows: list[dict] = []
    for candidate in _list(evidence.get("event_contract_candidates")):
        if not isinstance(candidate, dict):
            continue
        condition = _summarize_event_conditions(_list(candidate.get("event_condition_candidates")))
        id_summary = _summarize_id_fields(_list(candidate.get("id_fields")))
        first_rules = [
            _safe_text(_first_non_empty(item.get("rule"), item.get("partition_by")))
            for item in _list(candidate.get("first_last_rule_candidates"))
            if isinstance(item, dict)
        ]
        source_fields = [str(item) for item in _list(candidate.get("source_field_candidates"))[:16]]
        entity_keys = [str(item) for item in _list(candidate.get("entity_keys"))[:12]]
        rows.append(
            {
                "event_name": _safe_text(candidate.get("event_name_hint"), "行为事件"),
                "event_family": _safe_text(candidate.get("event_family"), "generic_event"),
                "source_logs_or_tables": _list(candidate.get("source_logs_or_tables")),
                "event_condition": condition or "未从 SQL 识别单一事件成立条件；需结合注释和代码视角确认。",
                "id_or_mapping": id_summary or "未识别固定 ID/档位映射。",
                "statistic_object": "、".join(entity_keys) if entity_keys else "未识别统计对象/去重键。",
                "first_or_final_rule": "；".join(first_rules) if first_rules else "未识别首次/最终事件规则。",
                "join_or_backfill_rule": "；".join(str(item) for item in _list(candidate.get("join_backfill_candidates"))[:6]),
                "source_fields": source_fields,
                "product_interpretation": "这是脚本抽取到的事件候选事实，需要 LLM 在正常 review 中收口成产品可读口径。",
                "business_risk": "如果事件成立条件、ID 映射或去重键不符合产品定义，相关人数/占比都会偏。",
                "sql_evidence_refs": _event_sql_refs(candidate),
                "sql_evidence": _list(candidate.get("sql_evidence"))[:12],
                "confidence": "medium" if condition or id_summary else "low",
            }
        )
    return rows[:8]


def _normalize_event_contract(item: dict, fallback_name: str = "行为事件", fallback_candidate: dict | None = None) -> dict:
    candidate = fallback_candidate if isinstance(fallback_candidate, dict) else {}
    source_logs = [str(value) for value in _list(item.get("source_logs_or_tables") or item.get("source_tables"))]
    if not source_logs:
        source_logs = [str(value) for value in _list(candidate.get("source_logs_or_tables"))]
    source_fields = [str(value) for value in _list(item.get("source_fields"))]
    if not source_fields:
        source_fields = [str(value) for value in _list(candidate.get("source_field_candidates"))]
    sql_evidence_refs = [str(value) for value in _list(item.get("sql_evidence_refs"))]
    sql_evidence = [
        {
            "ref": _safe_text(ev.get("ref"), ""),
            "snippet": _safe_text(ev.get("snippet"), ""),
        }
        if isinstance(ev, dict)
        else {"ref": "", "snippet": _safe_text(ev)}
        for ev in _list(item.get("sql_evidence"))
    ]
    if not sql_evidence_refs and not sql_evidence and candidate:
        sql_evidence_refs = _event_sql_refs(candidate)
        sql_evidence = [
            {
                "ref": _safe_text(ev.get("ref"), ""),
                "snippet": _safe_text(ev.get("snippet"), ""),
            }
            if isinstance(ev, dict)
            else {"ref": "", "snippet": _safe_text(ev)}
            for ev in _list(candidate.get("sql_evidence"))[:12]
        ]
    return {
        "event_name": _safe_text(_first_non_empty(item.get("event_name"), item.get("name")), fallback_name),
        "event_family": _safe_text(item.get("event_family"), "generic_event"),
        "source_logs_or_tables": source_logs,
        "event_condition": _safe_text(_first_non_empty(item.get("event_condition"), item.get("condition")), "未说明事件成立条件。"),
        "id_or_mapping": _safe_text(_first_non_empty(item.get("id_or_mapping"), item.get("id_mapping"), item.get("mapping")), "未说明 ID/映射。"),
        "statistic_object": _safe_text(_first_non_empty(item.get("statistic_object"), item.get("dedup_key"), item.get("grain")), "未说明统计对象/去重键。"),
        "first_or_final_rule": _safe_text(_first_non_empty(item.get("first_or_final_rule"), item.get("first_rule"), item.get("order_rule")), "未说明首次/最终规则。"),
        "join_or_backfill_rule": _safe_text(_first_non_empty(item.get("join_or_backfill_rule"), item.get("join_rule"), item.get("attribution_rule")), ""),
        "source_fields": source_fields,
        "product_interpretation": _safe_text(item.get("product_interpretation"), "模型未补充产品解释。"),
        "business_risk": _safe_text(item.get("business_risk"), "该事件口径会影响相关指标是否正确。"),
        "sql_evidence_refs": sql_evidence_refs,
        "sql_evidence": sql_evidence,
        "confidence": _safe_text(item.get("confidence"), "low"),
    }


def _normalize_event_contracts(payload: dict, evidence: dict | None = None) -> None:
    candidates = _list(_dict(evidence).get("event_contract_candidates")) if evidence else []
    rows: list[dict] = []
    for index, item in enumerate(_list(payload.get("event_contracts"))):
        if not isinstance(item, dict):
            continue
        candidate = candidates[index] if index < len(candidates) and isinstance(candidates[index], dict) else None
        rows.append(_normalize_event_contract(item, f"行为事件 {index + 1}", candidate))
    payload["event_contracts"] = rows


def _normalize_text_list(values: Any, *, limit: int = 80) -> list[str]:
    rows: list[str] = []
    for item in _list(values):
        if isinstance(item, dict):
            text = _first_non_empty(
                item.get("field"),
                item.get("name"),
                item.get("label"),
                item.get("metric_name"),
                item.get("title"),
                item.get("value"),
            )
            if not text:
                text = json.dumps(item, ensure_ascii=False, sort_keys=True)
        else:
            text = item
        cleaned = _safe_text(text)
        if cleaned:
            rows.append(cleaned)
        if len(rows) >= limit:
            break
    return rows


def _event_contract_text(contract: dict) -> str:
    return "；".join(
        part
        for part in [
            _safe_text(contract.get("event_name")),
            _safe_text(contract.get("event_condition")),
            _safe_text(contract.get("id_or_mapping")),
            _safe_text(contract.get("statistic_object")),
            _safe_text(contract.get("first_or_final_rule")),
        ]
        if part
    )


def _metric_core_text(card: dict) -> str:
    return " ".join(
        str(part or "")
        for part in [
            card.get("metric_name"),
            card.get("business_meaning"),
            card.get("metric_type"),
            card.get("calculation"),
            card.get("numerator"),
            card.get("denominator"),
            card.get("dedup_key"),
            card.get("row_grain_explanation"),
            " ".join(str(item) for item in _list(card.get("aggregation_dimensions"))),
            " ".join(
                " ".join(str(row.get(key) or "") for key in ["label", "business_effect", "condition"])
                for row in _list(card.get("metric_filters"))
                if isinstance(row, dict)
            ),
        ]
    )


def _metric_formula_text(card: dict) -> str:
    return " ".join(
        str(part or "")
        for part in [
            card.get("metric_name"),
            card.get("business_meaning"),
            card.get("metric_type"),
            card.get("calculation"),
            card.get("numerator"),
            card.get("denominator"),
            card.get("dedup_key"),
        ]
    )


def _metric_matches_event(card: dict, contract: dict) -> bool:
    text = _metric_formula_text(card)
    event_text = _event_contract_text(contract)
    if not event_text:
        return False
    event_family = _safe_text(contract.get("event_family"), "")
    source_text = " ".join(str(item) for item in _list(contract.get("source_logs_or_tables")) + _list(contract.get("source_fields")))
    if re.search(r"(duration|时长|非挂机|TotalActiveDuration|BattleLoginOut)", event_family + " " + event_text + " " + source_text, flags=re.I):
        return bool(re.search(r"(时长|区间|分桶|累计|占比|比例|比率|rate|ratio)", text, flags=re.I))
    if re.search(r"(resource|LobbyResourceFlow|TemplateId|资源)", event_family + " " + event_text + " " + source_text, flags=re.I):
        return bool(re.search(r"(资源|TemplateId|技巧点|获得|人数|占比|比例|比率|累计)", text, flags=re.I))
    keyword_groups = [
        ["任务", "Mission", "完成"],
        ["奖励", "领取", "领奖", "Item"],
        ["资源", "TemplateId", "DeltaValue", "ActionType", "技巧点", "LobbyResourceFlow"],
        ["Talent", "TalentID", "解锁", "天赋"],
        ["时长", "非挂机", "Duration", "BattleSrvId"],
        ["登录", "注册", "PlayerLogin"],
    ]
    for group in keyword_groups:
        if any(keyword in event_text for keyword in group) and any(keyword in text for keyword in group):
            return True
    return False


def _apply_event_contract_links(payload: dict) -> dict:
    contracts = _list(payload.get("event_contracts"))
    if not contracts:
        return payload
    story_cards = _list(payload.get("business_story_cards"))
    if not any(str(card.get("title") or "") == "事件口径契约" for card in story_cards if isinstance(card, dict)):
        story_cards.insert(
            1 if story_cards else 0,
            {
                "title": "事件口径契约",
                "body": "；".join(_event_contract_text(contract) for contract in contracts[:3] if isinstance(contract, dict)),
                "evidence_ref": "event_contracts",
            },
        )
        payload["business_story_cards"] = story_cards[:8]
    for card in _list(payload.get("metric_cards")):
        if not isinstance(card, dict):
            continue
        matched = [contract for contract in contracts if isinstance(contract, dict) and _metric_matches_event(card, contract)]
        if not matched:
            continue
        source_rows = _list(card.get("source_logs_fields"))
        filter_rows = _list(card.get("metric_filters"))
        evidence_refs = _list(card.get("sql_evidence_refs"))
        for contract in matched[:2]:
            if not any(_safe_text(contract.get("event_name")) in str(row) for row in source_rows):
                source_rows.insert(
                    0,
                    {
                        "role": "event_contract",
                        "source_logs_or_tables": _list(contract.get("source_logs_or_tables")),
                        "field_expression": "、".join(_list(contract.get("source_fields"))[:10]) or _safe_text(contract.get("event_condition")),
                        "business_story": _event_contract_text(contract),
                        "group_by": [_safe_text(contract.get("statistic_object"))],
                    },
                )
            if not any(_safe_text(contract.get("event_name")) in str(row) for row in filter_rows):
                filter_rows.insert(
                    0,
                    {
                        "label": _safe_text(contract.get("event_name"), "事件口径"),
                        "business_effect": _event_contract_text(contract),
                        "condition": _safe_text(contract.get("event_condition")),
                        "scope": "event_contract",
                    },
                )
            evidence_refs.extend(_list(contract.get("sql_evidence_refs")))
        card["source_logs_fields"] = source_rows[:10]
        card["metric_filters"] = filter_rows[:12]
        card["sql_evidence_refs"] = _unique(evidence_refs)[:20]
    return payload


def _risk_source_text(item: dict) -> str:
    return _safe_text(
        "；".join(
            part
            for part in [
                item.get("title"),
                item.get("question"),
                item.get("reason"),
                item.get("main_risk"),
                item.get("body"),
                item.get("business_risk"),
                item.get("standard_rule_alignment"),
            ]
            if part
        ),
        "",
    )


def _is_risk_text(text: str) -> bool:
    if not text:
        return False
    if re.search(r"(暂无|基本清楚|已自动核对通过)", text):
        return False
    return bool(
        re.search(
            r"(冲突|不符合|不一致|需确认|待确认|确认|未闭环|缺失|缺口|风险|影响|证据不足|low confidence|未能静态溯源|无法)",
            text,
            flags=re.I,
        )
    )


def _risk_title(text: str) -> str:
    if re.search(r"(GameMode|模式)", text, flags=re.I):
        return "模式范围未闭环"
    if re.search(r"(代理|执行环境|目标环境|结果文件)", text, flags=re.I):
        return "执行证据范围需确认"
    if re.search(r"(映射|枚举|配置|资源|任务|道具)", text, flags=re.I):
        return "业务映射需确认"
    if re.search(r"(分母|分子|占比|公式|累计占比|100%)", text, flags=re.I):
        return "占比分母/公式需复核"
    if re.search(r"(时长区间排序|排序|同游戏服内累计|累计范围)", text, flags=re.I):
        return "时长桶排序/累计范围需确认"
    if re.search(r"(去重|vOpenID|OpenID|玩家)", text, flags=re.I):
        return "去重对象需确认"
    cleaned = re.sub(r"^确认「[^」]+」[:：]?", "", text).strip(" ：;；")
    return _compact(cleaned, 26) or "口径待确认"


def _risk_severity(text: str) -> str:
    if re.search(r"(冲突|不符合|不一致|错|错误|expected|期望)", text, flags=re.I):
        return "high"
    if re.search(r"(需确认|待确认|未闭环|缺失|证据不足|low confidence|无法)", text, flags=re.I):
        return "medium"
    return "low"


def _risk_sort_key(item: dict) -> tuple[int, str]:
    order = {"high": 0, "medium": 1, "low": 2}
    return (order.get(str(item.get("severity") or "low"), 2), str(item.get("title") or ""))


def _risk_current_value(text: str) -> str:
    match = re.search(r"SQL\s*当前[:：]?\s*([^；。]+)", text, flags=re.I)
    return _compact(match.group(1), 220) if match else ""


def _risk_expected_value(text: str) -> str:
    match = re.search(r"(?:期望|标准口径)[:：]?\s*([^；。]+)", text, flags=re.I)
    return _compact(match.group(1), 220) if match else ""


def _risk_difference(text: str) -> str:
    if re.search(r"GameMode|模式", text, flags=re.I):
        return "SQL 纳入的模式集合与保存口径/规则检查期望不一致。"
    if re.search(r"映射|枚举|配置|资源|任务|道具", text, flags=re.I):
        return "SQL 使用的业务值或映射缺少当前资料证据。"
    if re.search(r"分母|占比|累计占比", text, flags=re.I):
        return "比率指标需要确认分母是否固定为同一 Base。"
    if re.search(r"代理|执行环境|目标环境", text, flags=re.I):
        return "当前结果只能证明已声明的执行环境，不能证明目标环境已经执行。"
    return ""


def _risk_impact(text: str) -> str:
    impact_match = re.search(r"(?:冲突影响|影响)[:：]?\s*([^。]+。?)", text)
    if impact_match:
        return _compact(impact_match.group(1), 260)
    if re.search(r"GameMode|模式", text, flags=re.I):
        return "会改变 BattleLoginOut 时长归因范围，进而影响玩家落入的时长桶、区间人数、区间占比和累计占比。"
    if re.search(r"映射|枚举|配置|资源|任务|道具", text, flags=re.I):
        return "会改变事件是否成立及其相关指标的统计范围。"
    if re.search(r"分母|占比|累计占比", text, flags=re.I):
        return "会影响占比数值解释，以及累计占比最后一桶是否应收敛到 100%。"
    if re.search(r"代理|执行环境|目标环境", text, flags=re.I):
        return "产品页只能说明当前证据覆盖的环境，不能扩大验证范围。"
    return "会影响相关指标口径是否可被产品确认。"


def _risk_action(text: str) -> str:
    if re.search(r"GameMode|模式", text, flags=re.I):
        return "确认需求应使用仅常规模式、规则期望模式，还是显式包含当前 SQL 的赛季自定义模式集合。"
    if re.search(r"映射|枚举|配置|资源|任务|道具", text, flags=re.I):
        return "使用当前项目绑定的资料版本确认业务值、映射和事件条件。"
    if re.search(r"分母|占比|累计占比", text, flags=re.I):
        return "确认每个占比指标的固定分母，以及累计口径的排序和分区范围。"
    if re.search(r"代理|执行环境|目标环境", text, flags=re.I):
        return "在已声明的目标环境执行并补充精确结果证据。"
    return "补充业务口径或标准规则后重新审查。"


def _risk_refs_from_text(text: str, fallback: str = "") -> list[str]:
    refs = []
    for pattern in [r"`([^`]+)`", r"(rule_checks:[^；。,\s]+)", r"(comment_lines\[[0-9]+\])", r"(sql\.[A-Za-z0-9_.-]+)"]:
        refs.extend(re.findall(pattern, text))
    if fallback:
        refs.append(fallback)
    return _unique(refs)[:10]


def _iter_risk_sources(payload: dict) -> list[dict]:
    rows: list[dict] = []
    for item in _list(payload.get("shared_confirmations")):
        if not isinstance(item, dict):
            continue
        text = _risk_source_text(item)
        if _is_risk_text(text):
            rows.append(
                {
                    "metric_name": _safe_text(item.get("metric_name"), ""),
                    "text": text,
                    "evidence_ref": _safe_text(item.get("evidence_ref"), "shared_confirmations"),
                    "source": "shared_confirmations",
                }
            )
    for metric in _list(payload.get("metric_cards")):
        if not isinstance(metric, dict):
            continue
        metric_name = _safe_text(metric.get("metric_name"), "")
        alignment = _safe_text(metric.get("standard_rule_alignment"), "")
        if _is_risk_text(alignment):
            rows.append(
                {
                    "metric_name": metric_name,
                    "text": alignment,
                    "evidence_ref": "metric_cards.standard_rule_alignment",
                    "source": "standard_rule_alignment",
                }
            )
        for item in _list(metric.get("metric_confirmations")):
            if not isinstance(item, dict):
                continue
            text = _risk_source_text(item)
            if _is_risk_text(text):
                rows.append(
                    {
                        "metric_name": _safe_text(item.get("metric_name"), metric_name),
                        "text": text,
                        "evidence_ref": _safe_text(item.get("evidence_ref"), "metric_cards.metric_confirmations"),
                        "source": "metric_confirmations",
                    }
                )
    for item in _list(payload.get("metric_overview")):
        if not isinstance(item, dict):
            continue
        text = _risk_source_text(item)
        if _is_risk_text(text):
            rows.append(
                {
                    "metric_name": _safe_text(item.get("metric_name"), ""),
                    "text": text,
                    "evidence_ref": "metric_overview.main_risk",
                    "source": "metric_overview",
                }
            )
    for item in _list(payload.get("business_story_cards")):
        if not isinstance(item, dict):
            continue
        title = _safe_text(item.get("title"), "")
        text = _risk_source_text(item)
        if re.search(r"(风险|冲突|不符合|待确认)", title + text) and _is_risk_text(text):
            rows.append(
                {
                    "metric_name": "",
                    "text": text,
                    "evidence_ref": _safe_text(item.get("evidence_ref"), "business_story_cards"),
                    "source": "business_story_cards",
                }
            )
    return rows


def _build_risk_register(payload: dict) -> list[dict]:
    buckets: dict[str, dict] = {}
    for source in _iter_risk_sources(payload):
        text = _safe_text(source.get("text"), "")
        if not text:
            continue
        title = _risk_title(text)
        row = buckets.setdefault(
            title,
            {
                "title": title,
                "severity": _risk_severity(text),
                "description": text,
                "conflict_object": title,
                "sql_current": _risk_current_value(text),
                "expected_or_standard": _risk_expected_value(text),
                "difference": _risk_difference(text),
                "impact": _risk_impact(text),
                "affected_metrics": [],
                "action": _risk_action(text),
                "evidence_refs": [],
            },
        )
        if _risk_sort_key({"severity": _risk_severity(text), "title": title}) < _risk_sort_key(row):
            row["severity"] = _risk_severity(text)
        if len(text) > len(str(row.get("description") or "")):
            row["description"] = text
            row["sql_current"] = _risk_current_value(text) or row.get("sql_current", "")
            row["expected_or_standard"] = _risk_expected_value(text) or row.get("expected_or_standard", "")
            row["difference"] = _risk_difference(text) or row.get("difference", "")
            row["impact"] = _risk_impact(text) or row.get("impact", "")
            row["action"] = _risk_action(text) or row.get("action", "")
        if source.get("metric_name"):
            row["affected_metrics"].append(source["metric_name"])
        row["evidence_refs"].extend(_risk_refs_from_text(text, source.get("evidence_ref") or ""))
    risks = sorted(buckets.values(), key=_risk_sort_key)
    for index, risk in enumerate(risks, 1):
        risk["risk_id"] = f"R{index}"
        risk["affected_metrics"] = _unique(risk.get("affected_metrics", []))[:20]
        risk["evidence_refs"] = _unique(risk.get("evidence_refs", []))[:12]
        for key in ["sql_current", "expected_or_standard", "difference", "impact", "action"]:
            risk[key] = _safe_text(risk.get(key), "见风险描述。")
    return risks[:12]


def _build_event_index(payload: dict) -> list[dict]:
    rows: list[dict] = []
    for index, contract in enumerate(_list(payload.get("event_contracts")), 1):
        if not isinstance(contract, dict):
            continue
        event_id = _safe_text(contract.get("event_id"), f"E{index}")
        contract["event_id"] = event_id
        rows.append(
            {
                "event_id": event_id,
                "event_name": _safe_text(contract.get("event_name"), f"事件 {index}"),
                "event_condition": _safe_text(contract.get("event_condition"), ""),
                "statistic_object": _safe_text(contract.get("statistic_object"), ""),
                "source_logs_or_tables": _list(contract.get("source_logs_or_tables")),
                "source_fields": _list(contract.get("source_fields"))[:12],
                "risk_summary": _safe_text(contract.get("business_risk"), ""),
                "confidence": _safe_text(contract.get("confidence"), "low"),
            }
        )
    return rows


def _risk_matches_metric(card: dict, risk: dict) -> bool:
    metric_name = _safe_text(card.get("metric_name"), "")
    if metric_name and metric_name in _list(risk.get("affected_metrics")):
        return True
    text = _metric_formula_text(card)
    title = _safe_text(risk.get("title"), "")
    if "GameMode" in title and re.search(r"GameMode|模式|时长|分桶|区间|累计", text, flags=re.I):
        return True
    if "资源" in title and re.search(r"TemplateId|资源|技巧点|获得|人数|占比", text, flags=re.I):
        return True
    if "分母" in title and re.search(r"占比|比例|比率|rate|ratio|pct|percent", text, flags=re.I):
        return True
    if "代理" in title and not _list(risk.get("affected_metrics")):
        return True
    return False


def _metric_review_status(card: dict, risk_refs: list[str]) -> str:
    if any(ref.startswith("R") for ref in risk_refs):
        if str(card.get("confidence") or "").lower() == "low":
            return "高优先级待确认"
        return "有风险待确认"
    if str(card.get("confidence") or "").lower() == "high":
        return "口径较完整"
    return "需复核"


def _build_metric_summary_table(payload: dict, event_index: list[dict], risks: list[dict]) -> list[dict]:
    contracts = _list(payload.get("event_contracts"))
    rows: list[dict] = []
    for card in _list(payload.get("metric_cards")):
        if not isinstance(card, dict):
            continue
        event_refs = _unique(
            [event.get("event_id") for event, contract in zip(event_index, contracts) if isinstance(contract, dict) and _metric_matches_event(card, contract)]
            + [str(ref) for ref in _list(card.get("event_refs"))]
        )
        risk_refs = _unique(
            [str(risk.get("risk_id")) for risk in risks if _risk_matches_metric(card, risk)]
            + [str(ref) for ref in _list(card.get("risk_refs"))]
        )
        card["event_refs"] = event_refs
        card["risk_refs"] = risk_refs
        card["risk_notes"] = [
            _safe_text(risk.get("title"), "")
            for risk in risks
            if str(risk.get("risk_id")) in risk_refs
        ]
        rows.append(
            {
                "metric_name": _safe_text(card.get("metric_name"), "未命名指标"),
                "metric_type": _safe_text(card.get("metric_type"), ""),
                "business_meaning": _safe_text(card.get("business_meaning"), ""),
                "calculation": _safe_text(card.get("calculation"), ""),
                "key_conditions": _unique(
                    _normalize_text_list(card.get("key_conditions"), limit=12)
                    + _metric_key_conditions(_list(card.get("metric_filters")) + _list(card.get("source_logs_fields")))
                )[:8],
                "numerator": _safe_text(card.get("numerator"), ""),
                "denominator": _safe_text(card.get("denominator"), ""),
                "dedup_key": _safe_text(card.get("dedup_key"), ""),
                "grain": _safe_text(
                    "；".join(
                        part
                        for part in [
                            "、".join(str(item) for item in _list(card.get("aggregation_dimensions"))),
                            card.get("row_grain_explanation"),
                        ]
                        if part
                    ),
                    "整体汇总",
                ),
                "event_refs": event_refs,
                "risk_refs": risk_refs,
                "confidence": _safe_text(card.get("confidence"), "low"),
                "review_status": _metric_review_status(card, risk_refs),
            }
        )
    return rows


def _build_review_actions(risks: list[dict], payload: dict) -> list[dict]:
    rows: list[dict] = []
    for risk in risks:
        rows.append(
            {
                "action_id": f"A{len(rows) + 1}",
                "source_ref": risk.get("risk_id", ""),
                "owner_hint": "产品/DA",
                "action": _safe_text(risk.get("action"), ""),
                "why": _safe_text(risk.get("impact"), ""),
            }
        )
    for item in _list(payload.get("shared_confirmations")):
        if not isinstance(item, dict):
            continue
        action = _safe_text(item.get("question"), "")
        if not action:
            continue
        if any(action == row.get("action") for row in rows):
            continue
        rows.append(
            {
                "action_id": f"A{len(rows) + 1}",
                "source_ref": _safe_text(item.get("evidence_ref"), "shared_confirmations"),
                "owner_hint": "产品/DA",
                "action": action,
                "why": _safe_text(item.get("reason"), ""),
            }
        )
        if len(rows) >= 12:
            break
    return rows[:12]


def _finalize_product_structure(payload: dict) -> dict:
    payload = _apply_event_contract_links(payload)
    event_index = _build_event_index(payload)
    risks = _build_risk_register(payload)
    payload["event_index"] = event_index
    payload["risk_register"] = risks
    payload["metric_summary_table"] = _build_metric_summary_table(payload, event_index, risks)
    payload["review_actions"] = _build_review_actions(risks, payload)
    _ensure_conclusion(payload)
    return payload


def _metric_confirmations(metric: dict, evidence: dict, dedup_key: str) -> list[dict]:
    rows: list[dict] = []
    metric_name = _safe_text(metric.get("metric_name"), "未命名指标")
    if str(metric.get("confidence") or "").lower() == "low" or metric.get("needs_manual_confirmation"):
        rows.append(
            {
                "metric_name": metric_name,
                "question": f"补充「{metric_name}」的业务定义",
                "reason": "当前只能从表达式或别名推断，缺少明确注释/标准口径。",
                "evidence_ref": "metric_cards[].business_definition",
            }
        )
    if "未从 SQL 证据识别明确去重对象" in dedup_key:
        rows.append(
            {
                "metric_name": metric_name,
                "question": f"确认「{metric_name}」按什么对象去重",
                "reason": "计数或比率指标应明确玩家、账号、战斗、局次或其他去重粒度。",
                "evidence_ref": "metric_cards[].dedup_key",
            }
        )
    denominator = str(metric.get("denominator") or "")
    if _metric_type(metric) == "比率指标" and re.search(r"unknown|未识别|不明确|不适用|非比率", denominator, flags=re.I):
        rows.append(
            {
                "metric_name": metric_name,
                "question": f"确认「{metric_name}」的分母",
                "reason": "比率指标必须能说清楚分子除以哪个 Base。",
                "evidence_ref": "metric_cards[].denominator",
            }
        )
    for check in _list(metric.get("related_saved_rule_checks")):
        result = str(check.get("result") or "").lower()
        if result in {"conflict", "proposed_conflict", "needs_manual_check"}:
            rows.append(
                {
                    "metric_name": metric_name,
                    "question": f"处理「{metric_name}」关联标准口径的 {result}",
                    "reason": _safe_text(_first_non_empty(check.get("message"), check.get("rule_summary")), "保存口径检查未通过或证据不足。"),
                    "evidence_ref": _safe_text(_first_non_empty(check.get("rule_id"), check.get("concept_key")), "rule_checks"),
                }
            )
    result_status = str(_dict(evidence.get("result_evidence")).get("status") or "")
    if result_status in {"missing_result_file", "field_mismatch"}:
        rows.append(
            {
                "metric_name": metric_name,
                "question": f"用结果文件核对「{metric_name}」",
                "reason": "当前结果文件缺失或列不匹配，无法完成输出形态和样例值核对。",
                "evidence_ref": "result_evidence",
            }
        )
    return rows[:8]


def _status_from_metric(metric: dict, confirmations: list[dict]) -> str:
    alignment = _standard_rule_alignment(metric)
    if "发现冲突" in alignment:
        return "conflict"
    if confirmations:
        return "needs_confirmation"
    if str(metric.get("confidence") or "").lower() == "high":
        return "auto_checked"
    return "evidence_based"


def _build_metric_card(metric: dict, evidence: dict) -> dict:
    metric_name = _safe_text(metric.get("metric_name"), "未命名指标")
    metric_type = _metric_type(metric)
    denominator = _safe_text(metric.get("denominator"), "不适用或未识别明确分母")
    if metric_type == "比率指标" and re.search(r"不适用|非比率", denominator):
        denominator = "未识别明确分母"
    dedup_key = _infer_dedup_key(metric)
    confirmations = _metric_confirmations(metric, evidence, dedup_key)
    metric_filters = _metric_filters(metric)
    source_logs_fields = _source_logs_fields(metric, evidence)
    return {
        "metric_name": metric_name,
        "business_meaning": _safe_text(metric.get("business_definition"), f"{metric_name} 的业务含义未在 SQL 注释中声明。"),
        "metric_type": metric_type,
        "calculation": _safe_text(metric.get("calculation"), "基于最终 SELECT 表达式计算。"),
        "numerator": _safe_text(metric.get("numerator"), "未识别明确分子"),
        "denominator": denominator,
        "dedup_key": dedup_key,
        "aggregation_dimensions": _list(evidence.get("dimension_names"))[:20],
        "row_grain_explanation": _safe_text(evidence.get("grouping"), "整体汇总或未识别明确分组粒度。"),
        "source_logs_fields": source_logs_fields,
        "metric_filters": metric_filters,
        "key_conditions": _metric_key_conditions(metric_filters + source_logs_fields),
        "standard_rule_alignment": _standard_rule_alignment(metric),
        "metric_confirmations": confirmations,
        "sql_evidence_refs": _unique(
            [
                f"final_output_fields.{metric_name}",
                "metric_logic.source_steps",
                "metric_logic.formula_expression" if metric.get("formula_expression") else "",
                "result_evidence.columns",
            ]
        ),
        "confidence": str(metric.get("confidence") or "low"),
        "review_status": _status_from_metric(metric, confirmations),
    }


def _common_filters(evidence: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for item in _list(evidence.get("common_filters")):
        if not isinstance(item, dict):
            continue
        row = {
            "label": _safe_text(item.get("label"), "筛选条件"),
            "scope": _safe_text(item.get("scope"), "公共范围"),
            "business_effect": _safe_text(item.get("business_effect"), _safe_text(item.get("condition"), "SQL 公共筛选条件")),
            "review_focus": _safe_text(item.get("review_focus"), "核对该条件是否属于本批指标的公共范围。"),
            "condition": _safe_text(item.get("condition")),
            "values": _list(item.get("values"))[:20],
            "source": _safe_text(item.get("source"), "sql_filter"),
        }
        key = (row["label"], row["scope"], row["business_effect"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows[:24]


def _dimension_overview(evidence: dict) -> list[dict]:
    if _list(evidence.get("dimension_cards")):
        return _list(evidence.get("dimension_cards"))[:30]
    return [
        {
            "field": name,
            "role": "输出维度/分组字段",
            "description": f"结果按「{name}」拆分或展示。",
            "source": "final_output_fields",
            "confidence": "medium",
        }
        for name in _list(evidence.get("dimension_names"))[:30]
    ]


def _execution_evidence(evidence: dict) -> dict:
    execution = _dict(evidence.get("execution_evidence"))
    result = _dict(evidence.get("result_evidence"))
    return {
        "current_sql_role": _safe_text(execution.get("current_sql_role"), "review_subject"),
        "review_subject": _safe_text(execution.get("review_subject"), "current_sql"),
        "result_evidence_role": _safe_text(execution.get("result_evidence_role")),
        "sql_files": _list(execution.get("sql_files")),
        "result_files": _list(execution.get("result_files")),
        "selected_result_file": _safe_text(_first_non_empty(execution.get("selected_result_file"), result.get("path"))),
        "result_pairing_method": _safe_text(execution.get("result_pairing_method"), _safe_text(result.get("pairing_method"))),
        "result_status": _safe_text(result.get("status"), "unknown"),
        "result_rows": result.get("row_count"),
        "execution_project": _safe_text(execution.get("execution_project")),
        "delivery_project": _safe_text(execution.get("delivery_project")),
        "evidence_status": _safe_text(execution.get("evidence_status")),
    }


def _business_story_cards(
    evidence: dict,
    metric_cards: list[dict],
    common_filters: list[dict],
    dimension_overview: list[dict],
) -> list[dict]:
    execution = _dict(evidence.get("execution_evidence"))
    result = _dict(evidence.get("result_evidence"))
    question = _safe_text(
        _first_non_empty(evidence.get("question"), evidence.get("conclusion_hint")),
        "从最终输出字段和结果列反推当前查询要回答的业务问题。",
    )
    denominators = _unique(
        [
            card.get("denominator")
            for card in metric_cards
            if card.get("denominator") and "不适用" not in str(card.get("denominator"))
        ]
    )
    base_body = _safe_text(evidence.get("base"), "未识别明确 Base。")
    if denominators:
        base_body = base_body.rstrip("。") + "；主要分母：" + "；".join(denominators[:4]) + "。"

    people_body = "人群范围由公共筛选和关联条件限定；重点检查统计对象、时间范围及业务范围是否与需求一致。"

    dimension_names = _unique(
        [
            _first_non_empty(item.get("field"), item.get("description"))
            for item in dimension_overview
            if isinstance(item, dict)
        ]
        + [str(item) for item in _list(evidence.get("dimension_names"))]
    )
    bucket_dimensions = [
        name
        for name in dimension_names
        if re.search(r"(时长|区间|分桶|阶段|类型|大类|顺序|顺位)", name, flags=re.I)
    ]
    if bucket_dimensions:
        bucket_title = "时长 / 分桶"
        bucket_body = "结果按 " + "、".join(bucket_dimensions[:8]) + " 拆分；这些字段更像观察维度/分桶，不应自动当成业务指标。"
    else:
        bucket_title = "分组 / 维度"
        bucket_body = "结果粒度由 " + ("、".join(dimension_names[:8]) if dimension_names else "最终 SELECT 分组字段") + " 决定；先确认这些维度是否正是产品要看的切面。"

    return [
        {
            "title": "它回答什么",
            "body": question,
            "evidence_ref": "question + final_output_fields",
        },
        {
            "title": "Base / 分母",
            "body": base_body,
            "evidence_ref": "base + metric_cards[].denominator",
        },
        {
            "title": "人群与回挂",
            "body": people_body,
            "evidence_ref": "common_filters + join_business_filters",
        },
        {
            "title": bucket_title,
            "body": bucket_body,
            "evidence_ref": "dimension_overview + final_output_fields",
        },
        {
            "title": "结果证据",
            "body": _safe_text(
                _first_non_empty(
                    result.get("note"),
                    f"结果状态为 {result.get('status') or 'unknown'}；证据范围为 {execution.get('evidence_status') or '未声明'}。",
                )
            ),
            "evidence_ref": "execution_evidence + result_evidence",
        },
    ]


def _metric_path_cards(metric_cards: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for card in metric_cards:
        metric_name = _safe_text(card.get("metric_name"), "未命名指标")
        confirmation = ""
        if card.get("metric_confirmations"):
            first = card["metric_confirmations"][0]
            confirmation = _safe_text(_first_non_empty(first.get("question"), first.get("reason")))
        rows.append(
            {
                "metric_name": metric_name,
                "title": metric_name,
                "body": _safe_text(
                    _first_non_empty(card.get("business_meaning"), card.get("numerator")),
                    f"{metric_name} 的业务含义需要结合指标卡证据确认。",
                ),
                "formula": _safe_text(card.get("calculation"), "基于最终 SELECT 表达式计算。"),
                "base": _safe_text(card.get("denominator"), "不适用或未识别明确分母"),
                "caveat": _safe_text(
                    _first_non_empty(confirmation, card.get("standard_rule_alignment")),
                    "用结果列、样例值和 SQL 证据做交叉核对。",
                ),
                "confidence": _safe_text(card.get("confidence"), "low"),
            }
        )
    return rows


def _output_contract(evidence: dict, metric_cards: list[dict]) -> dict:
    result = _dict(evidence.get("result_evidence"))
    execution = _dict(evidence.get("execution_evidence"))
    fields = _list(evidence.get("final_output_fields"))
    warnings = []
    if "proxy" in str(execution.get("evidence_status") or "").lower():
        warnings.append("当前结果仅证明已声明的代理执行环境，不等于目标环境已直接执行通过")
    if result.get("missing_columns"):
        warnings.append("结果文件缺少 SQL 输出字段：" + "、".join(_list(result.get("missing_columns"))[:12]))
    if result.get("extra_columns"):
        warnings.append("结果文件包含 SQL 未声明字段：" + "、".join(_list(result.get("extra_columns"))[:12]))
    ratio_metrics = [card.get("metric_name") for card in metric_cards if card.get("metric_type") == "比率指标"]
    if ratio_metrics:
        warnings.append("比率指标要重点核对分子/分母是否来自同一 Base：" + "、".join(str(item) for item in ratio_metrics[:8]))
    return {
        "fields": [str(item) for item in fields[:40]],
        "result_columns": [str(item) for item in _list(result.get("columns"))[:40]],
        "product_check": "结合当前 SQL 和精确结果证据，确认输出字段覆盖产品需要的维度、指标和排序/分桶字段。",
        "warning": "；".join(warnings) if warnings else "暂无结果列结构阻断；仍建议抽看样例量级和极端分桶。",
    }


def _evidence_sections(evidence: dict) -> list[dict]:
    result = _dict(evidence.get("result_evidence"))
    sections = [
        {
            "title": "最终输出字段",
            "default_collapsed": True,
            "summary": "用于确认产品视角的指标和维度是否覆盖最终 SELECT。",
            "items": _list(evidence.get("final_output_fields")),
        },
        {
            "title": "来源日志/表",
            "default_collapsed": True,
            "summary": "用于追溯指标来自哪些日志或中间表。",
            "items": _list(evidence.get("source_logs")) or _list(evidence.get("source_tables")),
        },
        {
            "title": "结果文件证据",
            "default_collapsed": True,
            "summary": f"status={result.get('status') or 'unknown'}; rows={result.get('row_count') if result.get('row_count') is not None else 'unknown'}",
            "items": [
                f"path={result.get('path') or 'none'}",
                f"columns={', '.join(_list(result.get('columns'))[:20]) or 'none'}",
                f"missing={', '.join(_list(result.get('missing_columns'))) or 'none'}",
                f"extra={', '.join(_list(result.get('extra_columns'))) or 'none'}",
            ],
        },
    ]
    if _list(evidence.get("rule_checks")):
        sections.append(
            {
                "title": "保存口径检查",
                "default_collapsed": True,
                "summary": f"{len(_list(evidence.get('rule_checks')))} 条规则检查证据。",
                "items": [
                    _safe_text(_first_non_empty(item.get("message"), item.get("rule_summary"), item.get("title")), "规则检查")
                    for item in _list(evidence.get("rule_checks"))[:20]
                    if isinstance(item, dict)
                ],
            }
        )
    return sections


def _compat_metric(card: dict) -> dict:
    return {
        "metric": card["metric_name"],
        "business_definition": card["business_meaning"],
        "base": card["row_grain_explanation"],
        "numerator": card["numerator"],
        "denominator": card["denominator"],
        "calculation": card["calculation"],
        "how_to_review": "核对业务含义、分子、分母、去重对象、聚合维度和结果样例是否一致。",
        "pass_criteria": "业务定义、SQL 证据、结果列和值样例可以互相解释。",
        "confidence": card["confidence"],
    }


def _build_deterministic_view(evidence: dict, *, mode: str, status: str, note: str = "") -> dict:
    metric_cards = [_build_metric_card(metric, evidence) for metric in _list(evidence.get("metric_cards"))]
    confirmations = []
    for card in metric_cards:
        confirmations.extend(card.get("metric_confirmations", []))
    common_filters = _common_filters(evidence)
    dimension_overview = _dimension_overview(evidence)
    execution_evidence = _execution_evidence(evidence)
    business_story_cards = _business_story_cards(evidence, metric_cards, common_filters, dimension_overview)
    metric_path_cards = _metric_path_cards(metric_cards)
    output_contract = _output_contract(evidence, metric_cards)
    event_contracts = _fallback_event_contracts(evidence)
    metric_overview = [
        {
            "metric_name": card["metric_name"],
            "metric_type": card["metric_type"],
            "review_status": card["review_status"],
            "main_risk": _first_non_empty(
                card["metric_confirmations"][0]["question"] if card.get("metric_confirmations") else "",
                card["standard_rule_alignment"],
            ),
            "confidence": card["confidence"],
            "confirmation_count": len(card.get("metric_confirmations", [])),
        }
        for card in metric_cards
    ]
    evidence_note = _safe_text(
        _first_non_empty(
            _dict(evidence.get("result_evidence")).get("note"),
            f"结果文件状态：{_dict(evidence.get('result_evidence')).get('status') or 'unknown'}",
        )
    )
    one_sentence = _safe_text(
        evidence.get("conclusion_hint"),
        f"{evidence.get('name') or '当前 SQL'} 输出 {len(metric_cards)} 个指标、{len(dimension_overview)} 个维度。",
    )
    if status != "llm":
        one_sentence = one_sentence.rstrip("。") + "；产品语义审查基于证据包生成。"
    payload = {
        "product_review_version": PRODUCT_REVIEW_VERSION,
        "product_review_mode": mode,
        "semantic_review_status": status,
        "semantic_review_note": note,
        "title": _safe_text(evidence.get("name"), "SQL Review"),
        "one_sentence": one_sentence,
        "business_question": _safe_text(evidence.get("question"), "从最终输出指标反推业务问题。"),
        "analysis_pattern": _safe_text(evidence.get("analysis_pattern"), "generic metric"),
        "source_logs": _list(evidence.get("source_logs")),
        "business_scope": [
            _safe_text(item.get("business_effect"))
            for item in common_filters
            if isinstance(item, dict) and item.get("business_effect")
        ],
        "base": _safe_text(evidence.get("base"), "未识别明确 Base；请补充统计对象和人群范围。"),
        "grouping": _safe_text(evidence.get("grouping"), "整体汇总或未识别明确分组粒度。"),
        "logic_steps": _list(evidence.get("logic_steps")),
        "walkthrough_sections": [],
        "metrics": [_compat_metric(card) for card in metric_cards],
        "key_filters": common_filters,
        "reviewer_should_check": _unique(
            [
                "先看指标总览，确认最终指标名称和业务问题是否一致。",
                "逐张指标卡核对分子、分母、去重对象、聚合维度和公共筛选。",
                "最后展开 SQL 证据区，追溯 CTE/JOIN/WHERE 是否支撑指标口径。",
            ]
        ),
        "unknowns_to_confirm": [
            f"{item.get('metric_name')}: {item.get('question')}（{item.get('reason')}）"
            for item in confirmations[:20]
        ],
        "evidence_note": evidence_note,
        "project_roles": {},
        "execution_evidence": execution_evidence,
        "business_story_cards": business_story_cards,
        "metric_path_cards": metric_path_cards,
        "output_contract": output_contract,
        "event_contracts": event_contracts,
        "metric_overview": metric_overview,
        "metric_cards": metric_cards,
        "dimension_overview": dimension_overview,
        "common_filters": common_filters,
        "shared_confirmations": confirmations[:30],
        "evidence_sections": _evidence_sections(evidence),
    }
    return _finalize_product_structure(payload)


def _cache_key(evidence: dict) -> str:
    execution = _dict(evidence.get("execution_evidence"))
    result = _dict(evidence.get("result_evidence"))
    payload = {
        "version": PRODUCT_REVIEW_VERSION,
        "sql_hash": evidence.get("sql_hash"),
        "final_output_fields": evidence.get("final_output_fields"),
        "metric_cards": evidence.get("metric_cards"),
        "common_filters": evidence.get("common_filters"),
        "event_contract_candidates": evidence.get("event_contract_candidates"),
        "criteria_alignment": evidence.get("criteria_alignment"),
        "rule_checks": evidence.get("rule_checks"),
        # Keep date-refresh values reusable while invalidating a changed result
        # contract or execution/evidence role.
        "result_schema": {
            "columns": result.get("columns"),
            "missing_columns": result.get("missing_columns"),
            "extra_columns": result.get("extra_columns"),
            "order_mismatch": result.get("order_mismatch"),
        },
        "execution_context": {
            "current_sql_role": execution.get("current_sql_role"),
            "result_evidence_role": execution.get("result_evidence_role"),
            "result_pairing_method": execution.get("result_pairing_method"),
            "execution_project": execution.get("execution_project"),
            "delivery_project": execution.get("delivery_project"),
            "evidence_status": execution.get("evidence_status"),
            "result_status": execution.get("result_status"),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_cached(cache_dir: Path, evidence: dict) -> dict | None:
    path = cache_dir / f"{_cache_key(evidence)}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    payload = _normalize_llm_payload(payload, evidence)
    return payload if validate_product_view(payload, evidence)[0] else None


def _save_cached(cache_dir: Path, evidence: dict, payload: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{_cache_key(evidence)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _fill_structural_defaults(payload: dict, evidence: dict, *, mode: str, status: str) -> dict:
    fallback = _build_deterministic_view(evidence, mode=mode, status=status)
    if isinstance(payload.get("execution_evidence"), dict):
        payload["execution_evidence"] = {
            **payload["execution_evidence"],
            **_dict(fallback.get("execution_evidence")),
        }
    else:
        payload["execution_evidence"] = fallback.get("execution_evidence")
    for key in ["output_contract"]:
        if isinstance(payload.get(key), dict):
            payload[key] = {**_dict(fallback.get(key)), **payload[key]}
        else:
            payload[key] = fallback.get(key)
    for key in ["business_story_cards", "metric_path_cards", "evidence_sections"]:
        if not payload.get(key):
            payload[key] = fallback.get(key)
    if not payload.get("event_contracts") and status not in {"llm", "llm_cached"}:
        payload["event_contracts"] = fallback.get("event_contracts")
    return _finalize_product_structure(payload)


def _normalize_confirmation(item: dict, metric_name: str, fallback_ref: str) -> dict:
    reason = _safe_text(_first_non_empty(item.get("reason"), item.get("question")), "需要补充指标口径证据。")
    question = _safe_text(item.get("question"), f"确认「{metric_name}」：{reason}")
    return {
        **item,
        "metric_name": _safe_text(item.get("metric_name"), metric_name),
        "question": question,
        "reason": reason,
        "evidence_ref": _safe_text(item.get("evidence_ref"), fallback_ref),
    }


def _normalize_llm_payload(payload: dict, evidence: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    _normalize_event_contracts(payload, evidence)
    if isinstance(payload.get("output_contract"), dict):
        contract = payload["output_contract"]
        contract["fields"] = _normalize_text_list(contract.get("fields"), limit=60)
        contract["result_columns"] = _normalize_text_list(contract.get("result_columns"), limit=60)
        contract["product_check"] = _safe_text(contract.get("product_check"), "")
        contract["warning"] = _safe_text(contract.get("warning"), "")
    for card in _list(payload.get("business_story_cards")):
        if isinstance(card, dict):
            card.setdefault("title", "口径卡")
            card.setdefault("body", "")
            card.setdefault("evidence_ref", "model_summary")
    for section in _list(payload.get("walkthrough_sections")):
        if isinstance(section, dict):
            section.setdefault("title", "口径拆解")
            section.setdefault("paragraphs", [])
            section.setdefault("table", {"headers": [], "rows": []})
            section.setdefault("bullets", [])
            table = section.get("table")
            if isinstance(table, dict):
                table.setdefault("headers", [])
                table.setdefault("rows", [])
            else:
                section["table"] = {"headers": [], "rows": []}
    for card in _list(payload.get("metric_path_cards")):
        if isinstance(card, dict):
            metric_name = _safe_text(_first_non_empty(card.get("metric_name"), card.get("title")), "未命名指标")
            card.setdefault("metric_name", metric_name)
            card.setdefault("title", metric_name)
            card.setdefault("body", "")
            card.setdefault("formula", "")
            card.setdefault("base", "")
            card.setdefault("caveat", "")
            card.setdefault("confidence", "low")
    for card in _list(payload.get("metric_cards")):
        if not isinstance(card, dict):
            continue
        metric_name = _safe_text(card.get("metric_name"), "未命名指标")
        card["aggregation_dimensions"] = [str(item) for item in _list(card.get("aggregation_dimensions"))]
        card["sql_evidence_refs"] = [str(item) for item in _list(card.get("sql_evidence_refs"))]
        card["source_logs_fields"] = [
            {
                "role": _safe_text(item.get("role"), "metric_value"),
                "source_logs_or_tables": (
                    _list(item.get("source_logs_or_tables"))
                    or _list(item.get("source_tables"))
                    or _list(evidence.get("source_logs"))
                    or _list(evidence.get("source_tables"))
                ),
                "field_expression": _product_source_field_text(
                    _first_non_empty(item.get("field_expression"), item.get("field")),
                    "完整字段血缘见代码视角",
                ),
                "business_story": _product_source_story(
                    _first_non_empty(item.get("business_story"), item.get("story")),
                    "作为该指标的本源字段证据",
                ),
                "group_by": _list(item.get("group_by")),
            }
            if isinstance(item, dict)
            else {
                "role": "metric_value",
                "source_logs_or_tables": _list(evidence.get("source_logs")) or _list(evidence.get("source_tables")),
                "field_expression": _product_source_field_text(item, "完整字段血缘见代码视角"),
                "business_story": _product_source_story(item, "作为该指标的本源字段证据"),
                "group_by": [],
            }
            for item in _list(card.get("source_logs_fields"))
        ]
        card["metric_filters"] = [
            {
                "label": _safe_text(item.get("label"), _safe_text(item.get("field"), "指标条件")),
                "business_effect": _safe_text(item.get("business_effect"), _safe_text(item.get("condition"), "")),
                "condition": _safe_text(item.get("condition"), ""),
                "scope": _safe_text(item.get("scope"), "metric_filter"),
            }
            if isinstance(item, dict)
            else {
                "label": "指标条件",
                "business_effect": _safe_text(item),
                "condition": _safe_text(item),
                "scope": "metric_filter",
            }
            for item in _list(card.get("metric_filters"))
        ]
        explicit_key_conditions = _normalize_text_list(card.get("key_conditions"), limit=12)
        card["key_conditions"] = _unique(
            explicit_key_conditions
            + _metric_key_conditions(card["metric_filters"] + card["source_logs_fields"])
        )[:8]
        card["metric_confirmations"] = [
            _normalize_confirmation(item, metric_name, "metric_cards")
            for item in _list(card.get("metric_confirmations"))
            if isinstance(item, dict)
        ]
    payload["common_filters"] = [
        {
            "label": _safe_text(item.get("label"), _safe_text(item.get("field"), "公共筛选")),
            "scope": _safe_text(item.get("scope"), "公共范围"),
            "business_effect": _safe_text(item.get("business_effect"), _safe_text(item.get("condition"), "")),
            "review_focus": _safe_text(item.get("review_focus"), "核对该条件是否属于本批指标的公共范围。"),
            "condition": _safe_text(item.get("condition"), ""),
        }
        if isinstance(item, dict)
        else {
            "label": "公共筛选",
            "scope": "公共范围",
            "business_effect": _safe_text(item),
            "review_focus": "核对该条件是否属于本批指标的公共范围。",
            "condition": _safe_text(item),
        }
        for item in _list(payload.get("common_filters"))
    ]
    payload["shared_confirmations"] = [
        _normalize_confirmation(item, _safe_text(item.get("metric_name"), "shared"), "shared_confirmations")
        for item in _list(payload.get("shared_confirmations"))
        if isinstance(item, dict)
    ]
    payload["evidence_sections"] = [
        {
            **section,
            "title": _safe_text(section.get("title"), "SQL 证据"),
            "default_collapsed": bool(section.get("default_collapsed", True)),
            "summary": _safe_text(section.get("summary"), ""),
            "items": [str(item) for item in _list(section.get("items"))],
        }
        if isinstance(section, dict)
        else {
            "title": "SQL 证据",
            "default_collapsed": True,
            "summary": _safe_text(section),
            "items": [_safe_text(section)],
        }
        for section in _list(payload.get("evidence_sections"))
    ]
    return _finalize_product_structure(payload)


def _run_agent_command(command: str, evidence: dict) -> dict:
    timeout = int(str(os.environ.get("SQL_REVIEW_PRODUCT_AGENT_TIMEOUT", "900")))
    proc = subprocess.run(
        command,
        input=json.dumps(evidence, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(_compact(proc.stderr or proc.stdout or f"exit={proc.returncode}", 1000))
    return json.loads(proc.stdout)


def _trim_filter(item: dict) -> dict:
    return {
        "label": _safe_text(_first_non_empty(item.get("label"), item.get("field"))),
        "scope": _safe_text(_first_non_empty(item.get("scope_label"), item.get("scope"))),
        "business_effect": _safe_text(item.get("business_effect"), _safe_text(item.get("condition"))),
        "condition": _safe_text(item.get("condition"), "", strip_aliases=False),
        "values": _list(item.get("values"))[:12],
    }


def _trim_metric_for_llm(metric: dict) -> dict:
    return {
        "metric_name": _safe_text(metric.get("metric_name")),
        "calculation_type": _safe_text(metric.get("calculation_type")),
        "business_definition": _safe_text(metric.get("business_definition"), ""),
        "base": _safe_text(metric.get("base"), ""),
        "numerator": _safe_text(metric.get("numerator"), ""),
        "denominator": _safe_text(metric.get("denominator"), ""),
        "calculation": _safe_text(metric.get("calculation"), ""),
        "formula_expression": _safe_text(metric.get("formula_expression"), "", strip_aliases=False),
        "numerator_expression": _safe_text(metric.get("numerator_expression"), "", strip_aliases=False),
        "denominator_expression": _safe_text(metric.get("denominator_expression"), "", strip_aliases=False),
        "confidence": _safe_text(metric.get("confidence"), "low"),
        "needs_manual_confirmation": bool(metric.get("needs_manual_confirmation")),
        "source_steps": [
            {
                "role": _safe_text(step.get("role")),
                "source_tables": _list(step.get("source_tables"))[:8],
                "group_by": _list(step.get("group_by"))[:12],
                "field_expression": _safe_text(step.get("field_expression"), strip_aliases=False),
                "story": _safe_text(step.get("story")),
                "lineage": _list(step.get("lineage"))[:8],
            }
            for step in _list(metric.get("source_steps"))[:8]
            if isinstance(step, dict)
        ],
        "lineage": _list(metric.get("lineage"))[:10],
        "base_business_filters": [_trim_filter(item) for item in _list(metric.get("base_business_filters"))[:8] if isinstance(item, dict)],
        "metric_business_filters": [_trim_filter(item) for item in _list(metric.get("metric_business_filters"))[:8] if isinstance(item, dict)],
        "join_business_filters": [_trim_filter(item) for item in _list(metric.get("join_business_filters"))[:8] if isinstance(item, dict)],
        "metric_conditions": [
            {
                "business_effect": _safe_text(item.get("business_effect")),
                "condition": _safe_text(item.get("condition"), strip_aliases=False),
            }
            for item in _list(metric.get("metric_conditions"))[:8]
            if isinstance(item, dict)
        ],
        "related_saved_rule_checks": [
            {
                "result": _safe_text(item.get("result")),
                "title": _safe_text(item.get("title")),
                "message": _safe_text(item.get("message")),
                "rule_summary": _safe_text(item.get("rule_summary")),
            }
            for item in _list(metric.get("related_saved_rule_checks"))[:5]
            if isinstance(item, dict)
        ],
    }


def _evidence_for_llm(evidence: dict) -> dict:
    result = _dict(evidence.get("result_evidence"))
    return {
        "schema_version": evidence.get("schema_version"),
        "path": evidence.get("path"),
        "name": evidence.get("name"),
        "sql_hash": evidence.get("sql_hash"),
        "question": evidence.get("question"),
        "analysis_pattern": evidence.get("analysis_pattern"),
        "conclusion_hint": evidence.get("conclusion_hint"),
        "business_comment_lines": _list(evidence.get("business_comment_lines"))[:60],
        "event_contract_candidates": _list(evidence.get("event_contract_candidates"))[:8],
        "source_logs": _list(evidence.get("source_logs"))[:12],
        "source_tables": _list(evidence.get("source_tables"))[:20],
        "final_output_fields": _list(evidence.get("final_output_fields"))[:50],
        "execution_evidence": evidence.get("execution_evidence"),
        "metric_names": _list(evidence.get("metric_names"))[:50],
        "dimension_names": _list(evidence.get("dimension_names"))[:40],
        "grouping": evidence.get("grouping"),
        "base": evidence.get("base"),
        "logic_steps": _list(evidence.get("logic_steps"))[:12],
        "metric_cards": [_trim_metric_for_llm(item) for item in _list(evidence.get("metric_cards")) if isinstance(item, dict)],
        "dimension_cards": _list(evidence.get("dimension_cards"))[:20],
        "common_filters": [_trim_filter(item) for item in _list(evidence.get("common_filters"))[:20] if isinstance(item, dict)],
        "result_evidence": {
            "status": result.get("status"),
            "path": result.get("path"),
            "row_count": result.get("row_count"),
            "columns": _list(result.get("columns"))[:40],
            "missing_columns": _list(result.get("missing_columns"))[:30],
            "extra_columns": _list(result.get("extra_columns"))[:30],
            "sample_rows": _list(result.get("sample_rows"))[:5],
            "note": result.get("note"),
        },
        "criteria_alignment": evidence.get("criteria_alignment"),
        "rule_checks": [
            {
                "result": _safe_text(item.get("result")),
                "title": _safe_text(item.get("title")),
                "message": _safe_text(item.get("message")),
                "rule_summary": _safe_text(item.get("rule_summary")),
                "concept_key": _safe_text(item.get("concept_key")),
            }
            for item in _list(evidence.get("rule_checks"))[:12]
            if isinstance(item, dict)
        ],
        "dimensions_status": evidence.get("dimensions_status"),
    }


def validate_product_view(payload: dict, evidence: dict) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["payload is not an object"]
    if _payload_has_forbidden_text(payload):
        errors.append("payload contains forbidden template phrase")
    status = str(payload.get("semantic_review_status") or "").strip().lower()
    if status and status not in {"llm", "llm_cached"}:
        errors.append(f"semantic_review_status must be llm|llm_cached for accepted product review, got {status}")
    cards = payload.get("metric_cards")
    if not isinstance(cards, list):
        errors.append("metric_cards must be a list")
        cards = []
    required = {
        "metric_name",
        "business_meaning",
        "metric_type",
        "calculation",
        "key_conditions",
        "numerator",
        "denominator",
        "dedup_key",
        "aggregation_dimensions",
        "row_grain_explanation",
        "source_logs_fields",
        "metric_filters",
        "standard_rule_alignment",
        "metric_confirmations",
        "sql_evidence_refs",
        "event_refs",
        "risk_refs",
        "confidence",
    }
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            errors.append(f"metric_cards[{index}] is not an object")
            continue
        missing = sorted(required - set(card))
        if missing:
            errors.append(f"metric_cards[{index}] missing {', '.join(missing)}")
        if card.get("confidence") not in {"high", "medium", "low"}:
            errors.append(f"metric_cards[{index}] confidence must be high|medium|low")
        if not isinstance(card.get("key_conditions"), list):
            errors.append(f"metric_cards[{index}] key_conditions must be a list")
        if not isinstance(card.get("event_refs"), list):
            errors.append(f"metric_cards[{index}] event_refs must be a list")
        if not isinstance(card.get("risk_refs"), list):
            errors.append(f"metric_cards[{index}] risk_refs must be a list")
        for field in ["business_meaning", "calculation", "numerator", "denominator", "dedup_key", "row_grain_explanation"]:
            if _is_generic_product_filler(card.get(field)):
                errors.append(f"metric_cards[{index}] {field} is generic filler or SQL trace")
        key_conditions = _list(card.get("key_conditions"))
        if not key_conditions:
            errors.append(f"metric_cards[{index}] key_conditions is empty")
        elif all(_is_generic_product_filler(item) for item in key_conditions):
            errors.append(f"metric_cards[{index}] key_conditions are generic filler")
        for confirmation in _list(card.get("metric_confirmations")):
            if isinstance(confirmation, dict):
                for key in ["metric_name", "question", "reason", "evidence_ref"]:
                    if not confirmation.get(key):
                        errors.append(f"metric_cards[{index}] confirmation missing {key}")
                if _is_generic_product_filler(confirmation.get("question")):
                    errors.append(f"metric_cards[{index}] confirmation question is generic filler")
    summary_rows = payload.get("metric_summary_table")
    if summary_rows is not None:
        if not isinstance(summary_rows, list):
            errors.append("metric_summary_table must be a list")
        for index, row in enumerate(_list(summary_rows)):
            if not isinstance(row, dict):
                errors.append(f"metric_summary_table[{index}] is not an object")
                continue
            if not isinstance(row.get("key_conditions"), list):
                errors.append(f"metric_summary_table[{index}] key_conditions must be a list")
            for field in ["calculation", "numerator", "denominator", "dedup_key", "grain"]:
                if _is_generic_product_filler(row.get(field)):
                    errors.append(f"metric_summary_table[{index}] {field} is generic filler or SQL trace")
    for index, confirmation in enumerate(_list(payload.get("shared_confirmations"))):
        if isinstance(confirmation, dict):
            for key in ["metric_name", "question", "reason", "evidence_ref"]:
                if not confirmation.get(key):
                    errors.append(f"shared_confirmations[{index}] missing {key}")
    expected_event_candidates = [
        item
        for item in _list(evidence.get("event_contract_candidates"))
        if isinstance(item, dict) and item.get("must_be_reviewed_by_llm")
    ]
    event_contracts = payload.get("event_contracts")
    if expected_event_candidates:
        if not isinstance(event_contracts, list) or not event_contracts:
            errors.append("event_contracts must cover detected event_contract_candidates")
            event_contracts = []
        elif len(event_contracts) < len(expected_event_candidates):
            errors.append("event_contracts does not cover all detected event candidates")
    event_required = {
        "event_name",
        "source_logs_or_tables",
        "event_condition",
        "statistic_object",
        "sql_evidence_refs",
        "confidence",
    }
    for index, contract in enumerate(_list(event_contracts)):
        if not isinstance(contract, dict):
            errors.append(f"event_contracts[{index}] is not an object")
            continue
        missing = sorted(event_required - set(contract))
        if missing:
            errors.append(f"event_contracts[{index}] missing {', '.join(missing)}")
        if not _list(contract.get("source_logs_or_tables")):
            errors.append(f"event_contracts[{index}] source_logs_or_tables is empty")
        if not _safe_text(contract.get("event_condition")) or _safe_text(contract.get("event_condition")).startswith("未说明"):
            errors.append(f"event_contracts[{index}] event_condition is empty")
        if not _safe_text(contract.get("statistic_object")) or _safe_text(contract.get("statistic_object")).startswith("未说明"):
            errors.append(f"event_contracts[{index}] statistic_object is empty")
        if not _list(contract.get("sql_evidence_refs")) and not _list(contract.get("sql_evidence")):
            errors.append(f"event_contracts[{index}] missing SQL evidence refs")
        if contract.get("confidence") not in {"high", "medium", "low"}:
            errors.append(f"event_contracts[{index}] confidence must be high|medium|low")
    expected_metrics = [str(item.get("metric_name") or "") for item in _list(evidence.get("metric_cards")) if isinstance(item, dict)]
    produced_metrics = [str(item.get("metric_name") or "") for item in cards if isinstance(item, dict)]
    if expected_metrics and len(produced_metrics) < len(expected_metrics):
        errors.append("metric_cards does not cover all deterministic metric candidates")
    return not errors, errors


def generate_product_view(
    evidence: dict,
    *,
    mode: str = "evidence-only",
    agent_command: str = "",
    cache_dir: Path | None = None,
) -> dict:
    if mode == "off":
        return _build_deterministic_view(
            evidence,
            mode=mode,
            status="off",
            note="产品语义审查已关闭，仅保留指标候选和证据摘要。",
        )

    if mode == "llm" and agent_command:
        cached = _load_cached(cache_dir, evidence) if cache_dir else None
        if cached:
            cached = _fill_structural_defaults(cached, evidence, mode=mode, status="llm_cached")
            cached["semantic_review_status"] = "llm_cached"
            return cached
        try:
            candidate = _run_agent_command(agent_command, _evidence_for_llm(evidence))
            candidate = _normalize_llm_payload(candidate, evidence)
            ok, errors = validate_product_view(candidate, evidence)
            if ok:
                candidate.setdefault("product_review_version", PRODUCT_REVIEW_VERSION)
                candidate.setdefault("product_review_mode", mode)
                candidate.setdefault("semantic_review_status", "llm")
                candidate = _fill_structural_defaults(candidate, evidence, mode=mode, status="llm")
                if cache_dir:
                    _save_cached(cache_dir, evidence, candidate)
                return candidate
            note = "模型输出未通过校验：" + "；".join(errors[:5])
        except Exception as exc:  # noqa: BLE001 - keep batch review alive and report downgrade.
            note = "模型命令不可用：" + _compact(exc, 500)
        return _build_deterministic_view(evidence, mode=mode, status="model_unavailable", note=note)

    status = "evidence_only" if mode == "evidence-only" else "model_unavailable"
    note = "" if status == "evidence_only" else "未配置 --product-review-command，已降级为证据包审查。"
    return _build_deterministic_view(evidence, mode=mode, status=status, note=note)


def _batch_candidate_items(payload: dict) -> list[dict]:
    if isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    if isinstance(payload.get("product_views"), dict):
        return [
            {"path": key, "product_view": value}
            for key, value in payload["product_views"].items()
            if isinstance(value, dict)
        ]
    return []


def _accept_llm_product_view(
    raw_view: dict,
    evidence: dict,
    *,
    mode: str,
    cache_dir: Path | None,
) -> tuple[dict | None, str]:
    raw_view = _normalize_llm_payload(raw_view, evidence)
    ok, errors = validate_product_view(raw_view, evidence)
    if not ok:
        return None, "模型输出未通过校验：" + "；".join(errors[:5])
    raw_view.setdefault("product_review_version", PRODUCT_REVIEW_VERSION)
    raw_view.setdefault("product_review_mode", mode)
    raw_view.setdefault("semantic_review_status", "llm")
    raw_view = _fill_structural_defaults(raw_view, evidence, mode=mode, status="llm")
    if cache_dir:
        _save_cached(cache_dir, evidence, raw_view)
    return raw_view, ""


def _run_missing_product_chunk(
    chunk: list[tuple[int, dict]],
    *,
    mode: str,
    agent_command: str,
    cache_dir: Path | None,
) -> tuple[dict[int, dict], dict[int, str]]:
    produced: dict[int, dict] = {}
    notes: dict[int, str] = {}
    try:
        if len(chunk) == 1:
            index, evidence = chunk[0]
            raw_view = _run_agent_command(agent_command, _evidence_for_llm(evidence))
            view, note = _accept_llm_product_view(raw_view, evidence, mode=mode, cache_dir=cache_dir)
            if view:
                produced[index] = view
            else:
                notes[index] = note
            return produced, notes

        batch_payload = {
            "batch_contract": "sql_review_product_batch_v1",
            "batch_items": [_evidence_for_llm(evidence) for _, evidence in chunk],
        }
        candidate = _run_agent_command(agent_command, batch_payload)
        by_path: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        for item in _batch_candidate_items(candidate):
            view = item.get("product_view") if isinstance(item.get("product_view"), dict) else item
            path = _safe_text(_first_non_empty(item.get("path"), item.get("item_path"), view.get("path")))
            name = _safe_text(_first_non_empty(item.get("name"), item.get("item_name"), view.get("title")))
            if path:
                by_path[path] = view
            if name:
                by_name[name] = view
        for index, evidence in chunk:
            raw_view = by_path.get(str(evidence.get("path") or "")) or by_name.get(str(evidence.get("name") or ""))
            if not raw_view:
                notes[index] = "模型批次输出缺少当前 SQL 的 product_view。"
                continue
            view, note = _accept_llm_product_view(raw_view, evidence, mode=mode, cache_dir=cache_dir)
            if view:
                produced[index] = view
            else:
                notes[index] = "模型批次输出未通过校验：" + note.removeprefix("模型输出未通过校验：")
    except Exception as exc:  # noqa: BLE001
        note = "模型批次命令不可用：" + _compact(exc, 500)
        for index, _ in chunk:
            if index not in produced:
                notes[index] = note
    return produced, notes


def generate_product_views_batch(
    evidences: list[dict],
    *,
    mode: str = "evidence-only",
    agent_command: str = "",
    cache_dir: Path | None = None,
) -> list[dict]:
    if mode != "llm" or not agent_command or len(evidences) <= 1:
        return [
            generate_product_view(
                evidence,
                mode=mode,
                agent_command=agent_command,
                cache_dir=cache_dir,
            )
            for evidence in evidences
        ]
    batch_size = _env_int("SQL_REVIEW_PRODUCT_AGENT_BATCH_SIZE", 2, minimum=1, maximum=16)
    parallelism = _env_int("SQL_REVIEW_PRODUCT_AGENT_PARALLELISM", 10, minimum=1, maximum=16)

    results: list[dict | None] = [None] * len(evidences)
    missing: list[tuple[int, dict]] = []
    for index, evidence in enumerate(evidences):
        cached = _load_cached(cache_dir, evidence) if cache_dir else None
        if cached:
            cached = _fill_structural_defaults(cached, evidence, mode=mode, status="llm_cached")
            cached["semantic_review_status"] = "llm_cached"
            results[index] = cached
        else:
            missing.append((index, evidence))

    if missing:
        chunks = [missing[start : start + batch_size] for start in range(0, len(missing), batch_size)]
        failed_notes: dict[int, str] = {}
        if parallelism <= 1 or len(chunks) <= 1:
            for chunk in chunks:
                produced, notes = _run_missing_product_chunk(
                    chunk,
                    mode=mode,
                    agent_command=agent_command,
                    cache_dir=cache_dir,
                )
                for index, view in produced.items():
                    results[index] = view
                failed_notes.update(notes)
        else:
            with ThreadPoolExecutor(max_workers=min(parallelism, len(chunks))) as executor:
                futures = [
                    executor.submit(
                        _run_missing_product_chunk,
                        chunk,
                        mode=mode,
                        agent_command=agent_command,
                        cache_dir=cache_dir,
                    )
                    for chunk in chunks
                ]
                for future in as_completed(futures):
                    produced, notes = future.result()
                    for index, view in produced.items():
                        results[index] = view
                    failed_notes.update(notes)

        for index, evidence in missing:
            if results[index] is None:
                results[index] = _build_deterministic_view(
                    evidence,
                    mode=mode,
                    status="model_unavailable",
                    note=failed_notes.get(index) or "模型批次输出不可用，已降级为证据包审查。",
                )

    return [item if item is not None else _build_deterministic_view(evidences[index], mode=mode, status="model_unavailable") for index, item in enumerate(results)]
