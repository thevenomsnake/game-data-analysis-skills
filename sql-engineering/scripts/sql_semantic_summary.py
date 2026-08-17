#!/usr/bin/env python3
"""Deterministic product summaries for formal SQL repository specs."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sql_facts import (
    build_sql_fact_bundle,
    classify_fields,
    external_sources as fact_external_sources,
    extract_filters,
    final_select_field_aliases,
    source_logs as fact_source_logs,
    unique_in_order,
)


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
METRIC_RE = re.compile(r"(人数|次数|数量|合计|总数|占比|比例|比率|率|均值|平均|时长|耗时|cnt|count|num|total|rate|ratio|avg|sum|p\d+)", re.I)
RATIO_RE = re.compile(r"(占比|比例|比率|转化率|留存率|率|rate|ratio|percent)", re.I)
TIME_FIELD_RE = re.compile(r"\b(dtEventTime|dteventdate|tdbank_imp_date|EventTime|LogTime)\b", re.I)


TOPIC_LABELS = {
    "new_user": "新增用户",
    "active_user": "活跃用户",
    "retention": "留存/回流",
    "funnel": "漏斗转化",
    "ab_compare": "AB/包体对比",
    "battle_behavior": "战斗/玩法行为",
    "economy": "经济/资源",
    "social": "社交/组队",
    "technical_quality": "技术质量",
    "content_progression": "内容进度",
    "data_quality": "数据质量",
    "ops_health": "运营健康",
    "privacy_export": "隐私导出",
    "uncategorized": "未分类",
}


def compact(value: Any, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def has_chinese(value: Any) -> bool:
    return bool(CJK_RE.search(str(value or "")))


def final_select_fields(sql: str) -> list[str]:
    return final_select_field_aliases(sql)


def source_logs(sql: str, root: Path) -> list[str]:
    return fact_source_logs(sql, root)


def external_sources(sql: str, root: Path | None = None) -> list[dict[str, Any]]:
    return fact_external_sources(sql, root)


def metric_groups(metrics: list[dict[str, Any]], dimensions: list[dict[str, Any]], filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = [str(item.get("name") or item.get("label") or item.get("field")) for item in metrics]
    if not names:
        return []
    filter_labels = [str(item.get("condition") or item.get("label")) for item in filters if item.get("kind") != "time_scope"]
    title = "同一统计口径下的指标"
    joined = " ".join(names).lower()
    if any(token in joined for token in ["rate", "ratio", "占比", "率"]):
        title = "同一统计口径下的比例/占比指标"
    elif any(token in joined for token in ["duration", "耗时", "时长", "p50", "p90"]):
        title = "同一统计口径下的时长/分位指标"
    elif any(token in joined for token in ["人数", "次数", "数量", "cnt", "count"]):
        title = "同一统计口径下的计数指标"
    return [
        {
            "title": title,
            "metrics": names,
            "shared_dedup_key": ", ".join(sorted({item.get("dedup_key", "") for item in metrics if item.get("dedup_key")})),
            "shared_dimensions": [str(item.get("label") or item.get("field")) for item in dimensions],
            "shared_filters": filter_labels,
            "quality_filters": [str(item.get("condition")) for item in filters if item.get("kind") == "time_scope"],
            "metric_notes": [str(item.get("business_meaning")) for item in metrics if item.get("business_meaning")],
            "ratio_notes": [f"{item.get('name')}：分子={item.get('numerator') or '未单独拆出'}；分母={item.get('denominator') or '未单独拆出'}" for item in metrics if item.get("metric_type") == "rate"],
        }
    ]


def _repository_helpers(root: Path) -> dict[str, Any] | None:
    """Load repository rule helpers once for a summary build."""
    try:
        from sql_repository import applied_criteria as repository_applied_criteria  # noqa: PLC0415
        from sql_repository import canonical_rule_checks as repository_rule_checks  # noqa: PLC0415
        from sql_repository import load_canonical_rule_index  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None

    try:
        rule_index = load_canonical_rule_index(root)
    except Exception:  # noqa: BLE001
        return None
    return {
        "applied_criteria": repository_applied_criteria,
        "canonical_rule_checks": repository_rule_checks,
        "rule_index": rule_index,
    }


def _repository_product_rule_checks(
    helpers: dict[str, Any] | None,
    sql: str,
    rule_context: dict[str, Any],
    logs: list[str],
) -> list[dict[str, Any]] | None:
    """Use the repository viewer's product-facing rule filter when available.

    Formalization writes `repository_summary` before the repository viewer
    rebuilds. Reusing the same event-signature filter here prevents sidecars
    from persisting reverse-audit diagnostics as "used criteria".
    """
    if not helpers:
        return None

    try:
        repository_rule_checks = helpers["canonical_rule_checks"]
        rule_index = helpers["rule_index"]
        spec = {"canonical_rule_context": rule_context}
        rows = repository_rule_checks(spec, rule_index, logs, sql_text=sql)
    except Exception:  # noqa: BLE001
        return None

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized.append(
            {
                "result": row.get("result") or "mentioned",
                "rule_id": row.get("rule_id") or "",
                "concept_key": row.get("concept_key") or "",
                "rule_title": row.get("title") or row.get("rule_title") or "",
                "rule_summary": compact(row.get("rule_summary") or row.get("message"), 520),
                "rule_display": compact(row.get("rule_display") or row.get("rule_summary") or row.get("message"), 1200),
                "full_rule": compact(row.get("full_rule"), 2400),
                "message": compact(row.get("message"), 360),
                "evidence": compact(row.get("evidence") or row.get("source"), 260),
                "source": row.get("source") or "",
            }
        )
    return normalized


def canonical_rule_checks(rule_context: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # Product/repository summaries should list business rules, not every field-
    # level constraint expanded from those rules. Hard constraints remain in the
    # machine rule-context/code evidence and only surface here when violated via
    # candidate_sql_check.blockers below.
    for key in ["active_rules", "applied_rules"]:
        value = rule_context.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                rows.append(
                    {
                        "result": "matched",
                        "rule_id": item.get("rule_id") or item.get("id") or "",
                        "concept_key": item.get("concept_key") or "",
                        "rule_title": item.get("title") or item.get("rule_title") or "",
                        "rule_summary": compact(
                            item.get("summary")
                            or item.get("message")
                            or item.get("activation_reason")
                            or item.get("title")
                            or item.get("rule_title"),
                            360,
                        ),
                    }
                )
            elif item:
                rows.append({"result": "matched", "message": compact(item, 360)})
    candidate_check = rule_context.get("candidate_sql_check") if isinstance(rule_context.get("candidate_sql_check"), dict) else {}
    for blocker in candidate_check.get("blockers", []) or []:
        rows.append({"result": "conflict", "message": compact(blocker, 360)})
    return rows


def applied_criteria(filters: list[dict[str, Any]], rule_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in filters:
        rows.append(
            {
                "category": item.get("kind") or "filter",
                "name": item.get("label") or item.get("field") or item.get("condition"),
                "description": item.get("business_effect") or item.get("condition") or "",
                "saved_rule_status": "unique",
                "rule_id": "",
                "concept_key": "",
                "rule_title": "",
                "rule_summary": "",
                "evidence": item.get("condition") or "SQL fixed filter",
            }
        )
    if not rows and rule_checks:
        for rule in rule_checks[:8]:
            if str(rule.get("result") or "").lower() != "conflict":
                continue
            rows.append(
                {
                    "category": "saved_rule",
                    "name": rule.get("rule_title") or rule.get("rule_id") or rule.get("message") or "保存口径",
                    "description": rule.get("rule_summary") or rule.get("message") or "",
                    "saved_rule_status": "conflict",
                    "rule_id": rule.get("rule_id", ""),
                    "concept_key": rule.get("concept_key", ""),
                    "rule_title": rule.get("rule_title", ""),
                    "rule_summary": rule.get("rule_summary", ""),
                    "evidence": "rule-context",
                }
            )
    return rows or [{"category": "sql", "name": "SQL 独有口径", "description": "未命中可自动证明的保存口径。", "saved_rule_status": "unique", "evidence": "deterministic_summary"}]


def external_source_criteria(external_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in external_sources:
        table = str(item.get("table") or "").strip()
        if not table:
            continue
        rows.append(
            {
                "category": "external_authoritative_source",
                "name": table,
                "description": str(item.get("business_role") or "项目登记的外部平台权威来源"),
                "saved_rule_status": "unique",
                "rule_id": "",
                "concept_key": "",
                "rule_title": "",
                "rule_summary": f"来源契约：{item.get('source_contract') or 'project sources'}；权威表：{table}；日期字段：{item.get('date_field') or '未声明'}。",
                "evidence": "repository_summary.external_sources",
            }
        )
    return rows


def _filter_conditions(filters: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for item in filters:
        if not isinstance(item, dict):
            continue
        text = str(item.get("condition") or item.get("business_effect") or item.get("label") or "").strip()
        if text:
            rows.append(text)
    return unique_in_order(rows)


def _repository_applied_criteria(
    helpers: dict[str, Any] | None,
    summary_seed: dict[str, Any],
    rule_checks: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Use the repository viewer's applied-criteria builder when available."""
    if not helpers:
        return None
    try:
        repository_applied_criteria = helpers["applied_criteria"]
        rule_index = helpers["rule_index"]
        return repository_applied_criteria(summary_seed, rule_checks, rule_index)
    except Exception:  # noqa: BLE001
        return None


def canonical_status(criteria: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("saved_rule_status") or "") for item in criteria}
    if "conflict" in statuses:
        return "conflict"
    if "needs_manual_check" in statuses:
        return "needs_manual_check"
    if "matched" in statuses:
        return "matched"
    return "unique"


def build_repository_summary(
    *,
    root: Path,
    sql: str,
    title: str,
    analysis: dict[str, Any],
    result: dict[str, Any],
    rule_context: dict[str, Any],
    sql_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result_columns = [str(item) for item in result.get("columns", [])]
    facts = sql_facts or build_sql_fact_bundle(
        sql,
        kind="QUERY",
        root=root,
        result_columns=result_columns,
    )
    fields = [str(item) for item in facts.get("final_fields", [])]
    metrics = copy.deepcopy(facts.get("metrics", []))
    dimensions = copy.deepcopy(facts.get("dimensions", []))
    filters = copy.deepcopy(facts.get("filters", []))
    logs = [str(item) for item in facts.get("source_logs", [])]
    external = copy.deepcopy(facts.get("external_sources", []))
    repository_helpers = _repository_helpers(root)
    product_rule_checks = _repository_product_rule_checks(repository_helpers, sql, rule_context, logs)
    rule_checks = product_rule_checks if product_rule_checks is not None else canonical_rule_checks(rule_context)
    criteria = applied_criteria(filters, rule_checks)
    business_category = str(analysis.get("business_category") or "uncategorized")
    base = "；".join([item.get("condition", "") for item in filters if item.get("kind") in {"fixed_value", "value_set"}])
    if not base:
        base = "按 SQL WHERE/params 限定的统计对象。"
    grain = analysis.get("grain") or (" x ".join(str(item.get("label") or item.get("field")) for item in dimensions) or "单行汇总")
    purpose = f"固化已跑数 SQL：{title}。"
    question = f"{title} 对应的查询结果和后续看板数据集。"
    groups = metric_groups(metrics, dimensions, filters)
    logic_summary = [
        f"统计对象：{base}",
        f"输出粒度：{grain}",
        "指标来自 SQL 最终输出字段；字段表达式和 CTE 血缘保留在 SQL/spec 中。",
    ]
    if external:
        logic_summary.append("外部权威表：" + "；".join(str(item.get("table")) for item in external))
    repository_seed = {
        "source_logs": logs,
        "external_sources": external,
        "base_population": base,
        "filters": _filter_conditions(filters),
        "logic_summary": logic_summary,
        "metric_groups": groups,
    }
    repository_criteria = _repository_applied_criteria(repository_helpers, repository_seed, rule_checks)
    criteria = repository_criteria if repository_criteria is not None else applied_criteria(filters, rule_checks)
    external_criteria = external_source_criteria(external)
    if external_criteria:
        existing_keys = {
            (str(item.get("category") or ""), str(item.get("name") or ""), str(item.get("concept_key") or ""))
            for item in criteria
        }
        for item in external_criteria:
            key = (str(item.get("category") or ""), str(item.get("name") or ""), str(item.get("concept_key") or ""))
            if key not in existing_keys:
                criteria.append(item)
    quality = "high" if (logs or external) and metrics and dimensions else "medium" if (logs or external) and metrics else "low"
    summary = {
        "display_title": title,
        "business_topic": TOPIC_LABELS.get(business_category, business_category),
        "purpose": purpose,
        "business_question": question,
        "base_population": base,
        "grain": grain,
        "metrics": metrics,
        "metric_groups": groups,
        "dimensions": dimensions,
        "filters": filters,
        "source_logs": logs,
        "external_sources": external,
        "logic_summary": logic_summary,
        "applied_criteria": criteria,
        "canonical_rule_status": canonical_status(criteria),
        "canonical_rule_checks": rule_checks,
        "result_evidence": {
            "status": "passed",
            "row_count": result.get("row_count"),
            "columns": result_columns,
            "schema_fingerprint": result.get("schema_fingerprint"),
            "file_name": result.get("file_name"),
        },
        "generated_by": "sql_formalize.py",
        "semantic_summary_status": "deterministic",
        "semantic_summary_quality": quality,
        "semantic_fingerprint": hashlib.sha256(
            json.dumps({"fields": fields, "columns": result_columns, "filters": filters, "rules": rule_checks}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
    }
    return summary


def needs_llm_summary(summary: dict[str, Any]) -> bool:
    if summary.get("semantic_summary_quality") == "low":
        return True
    hollow = ["需确认分子", "需确认分母", "结合业务需求确认", "unknown", "未声明"]
    semantic_fields = {
        key: summary.get(key)
        for key in [
            "display_title",
            "business_topic",
            "purpose",
            "business_question",
            "base_population",
            "grain",
            "metrics",
            "metric_groups",
            "dimensions",
            "filters",
            "source_logs",
            "logic_summary",
            "applied_criteria",
            "canonical_rule_checks",
        ]
    }
    text = json.dumps(semantic_fields, ensure_ascii=False).lower()
    return any(item.lower() in text for item in hollow)
