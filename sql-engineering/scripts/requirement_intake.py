#!/usr/bin/env python3
"""Classify whether a text is a clear data-query requirement.

This is a read-only entrypoint for external agents. It does not generate SQL,
write project assets, save rules, or call live databases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from function_gate import (
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
)
from capability_registry import command_function_ids
from query_window import resolve_query_window


SCRIPT_VERSION = "2.0.0"
SCHEMA_VERSION = "requirement_intake_v2"

MODE_SCOPE_QUESTION = "本次时长统计看哪些模式：游戏整体、常规（默认含活动）、纯常规、仅活动，还是其他明确模式？"


@dataclass(frozen=True)
class Signal:
    kind: str
    value: str
    evidence: str


@dataclass(frozen=True)
class Slot:
    name: str
    status: str
    value: object
    evidence: list[str]


QUERY_ACTION_TERMS = [
    "取数",
    "查询",
    "统计",
    "拉数",
    "拉一下",
    "导出",
    "生成sql",
    "写sql",
    "跑数",
    "看一下",
    "看下",
    "看看",
    "看",
    "求",
    "how many",
    "count",
    "query",
    "extract",
]

METRIC_TERMS = [
    "新增",
    "新用户",
    "新增用户",
    "新增玩家",
    "活跃",
    "日活",
    "月活",
    "dau",
    "mau",
    "留存",
    "次留",
    "回流",
    "常驻",
    "人数",
    "用户数",
    "玩家数",
    "角色数",
    "次数",
    "局数",
    "占比",
    "比例",
    "率",
    "转化",
    "漏斗",
    "分布",
    "趋势",
    "时长",
    "在线时长",
    "匹配时长",
    "胜率",
    "付费",
    "充值",
    "流水",
    "明细",
]

REVIEW_TERMS = ["review", "审查", "审核", "检查sql", "sql检查", "代码视角", "产品视角"]
RULE_TERMS = ["口径", "定义", "规则", "映射", "concept_key", "canonical rule", "指标定义"]
DASHBOARD_TERMS = ["看板", "dashboard", "da ", "da看板", "报表", "图表"]
VALIDATION_TERMS = ["验证", "validation", "跑数结果", "结果文件", ".csv", ".xlsx", "user-run"]
SOURCE_TERMS = ["xml", "tlog", "来源", "字段目录", "日志字段", "source intake", "source-intake"]
KNOWLEDGE_LOOKUP_TERMS = [
    "负责人",
    "谁负责",
    "找谁",
    "联系人",
    "owner",
    "qa负责",
    "qa 负责",
    "qa是谁",
    "qa 是谁",
    "哪个qa",
    "哪个 qa",
    "程序负责",
]
FORMALIZE_TERMS = ["固化", "入库", "正式保存", "保存sql", "保存 SQL", "formalize"]
INTERMEDIATE_TERMS = ["中间表", "intermediate table", "物化"]
PROJECT_ADMIN_TERMS = ["项目健康", "repo-health", "project-health", "manifest", "project_config"]


METRIC_PATTERNS = [
    ("新增用户数", r"新增(?:用户|玩家|人数)?"),
    ("活跃用户数", r"(?:活跃|日活|dau|月活|mau)"),
    ("留存", r"(?:留存|次留|[0-9]+日留存)"),
    ("回流用户数", r"回流"),
    ("常驻用户数", r"常驻"),
    ("人数", r"(?:人数|用户数|玩家数|角色数)"),
    ("次数", r"(?:次数|局数|场次)"),
    ("占比/比例", r"(?:占比|比例|率)"),
    ("转化", r"(?:转化|漏斗)"),
    ("分布", r"分布"),
    ("趋势", r"趋势"),
    ("时长", r"(?:时长|耗时|在线时长|匹配时长)"),
    ("付费/充值", r"(?:付费|充值|流水)"),
    ("明细", r"明细"),
]


PROJECT_PATTERN = re.compile(
    r"\b(RM[-_ ]?(?:BASE|EXPERIMENT|AB_TEST|AB)|DEMO_ANALYTICS|DEMO_EXPERIMENT|DEMO_AB_TEST)\b",
    re.IGNORECASE,
)
DATE_PATTERNS = [
    re.compile(r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}[日号]?)?"),
    re.compile(r"\d{1,2}月\d{1,2}[日号]?"),
    re.compile(r"\d{1,2}\s*月"),
    re.compile(r"(?:近|过去|最近)\s*\d+\s*(?:天|日|周|月)"),
    re.compile(r"(?:本|上|下)?(?:今天|昨日|昨天|本周|上周|本月|上月|本季度|上季度)"),
    re.compile(r"(?:全量|不限时间|全部历史|历史全量)"),
]
FILTER_PATTERNS = [
    ("iZoneAreaID", re.compile(r"\b(?:i?zoneareaid|izoneareaid|区服)\s*(?:=|为|是|:|：)?\s*(\d+)", re.IGNORECASE)),
    ("GameSvrId", re.compile(r"\b(?:gamesvrid|服务器)\s*(?:=|为|是|:|：)?\s*(\d+)", re.IGNORECASE)),
    ("GameMode", re.compile(r"\b(?:gamemode|模式)\s*(?:=|为|是|:|：)?\s*([0-9,，、 ]+)", re.IGNORECASE)),
]
SOURCE_LOG_PATTERN = re.compile(r"\b(Player[A-Za-z]+|Battle[A-Za-z]+|Match[A-Za-z]+|Damage|OnlineTime)\b")


def text_sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def empty_business_decisions(status: str = "not_applicable") -> dict:
    return {
        "status": status,
        "rule_application_sha256": "",
        "required": [],
        "unresolved_keys": [],
        "request_context": {
            "initial_text_sha256": "",
            "clarification_text_sha256": "",
            "clarification_present": False,
        },
    }


def mode_scope_from_text(text: str, *, source: str) -> dict | None:
    value = compact(text)
    if not value:
        return None

    def matched(pattern: str) -> re.Match[str] | None:
        return re.search(pattern, value, flags=re.IGNORECASE)

    overall = matched(r"游戏整体|整体游戏|全模式|不限模式|所有模式|不限制模式")
    if overall:
        return {
            "scope_type": "game_overall",
            "categories": [],
            "raw_scope": overall.group(0),
            "normalization": "explicit_game_overall",
            "source": source,
        }

    explicit_single = [
        (r"纯常规|仅常规|只看常规|常规(?:模式)?[^，。；,;]{0,8}(?:不含|排除|不看)活动|(?:不含|排除|不看)活动[^，。；,;]{0,8}常规", ["常规"], "explicit_regular_only"),
        (r"纯活动|仅活动|只看活动", ["活动"], "explicit_activity_only"),
        (r"纯快速|仅快速|只看快速", ["快速"], "explicit_fast_only"),
        (r"仅新手服|只看新手服", ["新手服"], "explicit_newbie_only"),
        (r"仅训练服|只看训练服", ["训练服"], "explicit_training_only"),
    ]
    for pattern, categories, normalization in explicit_single:
        hit = matched(pattern)
        if hit:
            return {
                "scope_type": "configured_mode_categories",
                "categories": categories,
                "raw_scope": hit.group(0),
                "normalization": normalization,
                "source": source,
            }

    categories: list[str] = []
    evidence: list[str] = []
    regular = matched(r"(?<!非)常规(?:模式|玩法|服)?")
    if regular:
        categories.extend(["常规", "活动"])
        evidence.append(regular.group(0))
    for category, pattern in [
        ("活动", r"活动(?:模式|玩法|服)?"),
        ("快速", r"快速(?:模式|玩法|服)?"),
        ("新手服", r"新手服"),
        ("训练服", r"训练服"),
    ]:
        hit = matched(pattern)
        if hit:
            categories.append(category)
            evidence.append(hit.group(0))
    categories = list(dict.fromkeys(categories))
    if categories:
        return {
            "scope_type": "configured_mode_categories",
            "categories": categories,
            "raw_scope": "+".join(evidence),
            "normalization": "bare_regular_includes_activity" if regular else "explicit_named_categories",
            "source": source,
        }

    mode_ids = matched(r"(?:GameMode|模式)\s*(?:=|为|是|:|：|IN)?\s*\(?\s*([0-9]+(?:\s*[,，、]\s*[0-9]+)*)")
    if mode_ids:
        ids = [int(item) for item in re.split(r"\s*[,，、]\s*", mode_ids.group(1))]
        return {
            "scope_type": "explicit_mode_ids",
            "categories": [],
            "mode_ids": ids,
            "raw_scope": mode_ids.group(0),
            "normalization": "explicit_mode_ids",
            "source": source,
        }
    return None


def fixed_mode_scope_from_rules(rule_context: dict) -> dict | None:
    for constraint in rule_context.get("hard_constraints", []) or []:
        if str(constraint.get("field") or "").lower() != "gamemode":
            continue
        if constraint.get("type") not in {"allowed_values", "must_use_values", "must_use_mode_category"}:
            continue
        return {
            "scope_type": "fixed_by_confirmed_rule",
            "categories": [],
            "values": constraint.get("values", []),
            "rule_id": str(constraint.get("rule_id") or ""),
            "concept_key": str(constraint.get("concept_key") or ""),
            "normalization": "more_specific_confirmed_rule",
            "source": "confirmed_rule",
        }
    return None


def resolve_business_decisions(rule_context: dict, initial_text: str, clarification_text: str = "") -> dict:
    requirements: dict[str, dict] = {}
    active_rules = {
        str(item.get("rule_id") or ""): item
        for item in rule_context.get("active_rules", []) or []
        if str(item.get("rule_id") or "")
    }
    for constraint in rule_context.get("hard_constraints", []) or []:
        if constraint.get("type") != "requires_explicit_business_decision":
            continue
        key = str(constraint.get("decision_key") or "").strip()
        if not key:
            continue
        owner = {
            "rule_id": str(constraint.get("rule_id") or ""),
            "concept_key": str(constraint.get("concept_key") or ""),
            "title": str(constraint.get("title") or ""),
        }
        row = requirements.setdefault(
            key,
            {
                "key": key,
                "status": "unresolved",
                "value": None,
                "question": "",
                "reason": str(constraint.get("reason") or ""),
                "allowed_semantics": [],
                "owners": [],
                "evidence": [],
            },
        )
        row["allowed_semantics"] = list(
            dict.fromkeys([*row["allowed_semantics"], *(constraint.get("allowed_semantics") or [])])
        )
        if owner not in row["owners"]:
            row["owners"].append(owner)
        rule_question = str((active_rules.get(owner["rule_id"]) or {}).get("decision_question") or "").strip()
        if rule_question and not row["question"]:
            row["question"] = rule_question

    if not requirements:
        payload = empty_business_decisions("not_applicable")
    else:
        fixed_mode_scope = fixed_mode_scope_from_rules(rule_context)
        for key, row in requirements.items():
            resolution = None
            if key == "mode_scope":
                resolution = fixed_mode_scope
                if resolution is None and clarification_text:
                    resolution = mode_scope_from_text(clarification_text, source="clarification")
                if resolution is None:
                    resolution = mode_scope_from_text(initial_text, source="initial_request")
                if not row["question"]:
                    row["question"] = MODE_SCOPE_QUESTION
            if resolution is not None:
                row["status"] = "resolved"
                row["value"] = resolution
                row["evidence"] = [
                    {
                        "source": resolution.get("source", ""),
                        "quote": resolution.get("raw_scope", ""),
                        "normalization": resolution.get("normalization", ""),
                    }
                ]
        unresolved = [key for key, row in requirements.items() if row["status"] != "resolved"]
        payload = {
            "status": "needs_input" if unresolved else "resolved",
            "rule_application_sha256": str(
                (rule_context.get("rule_application") or {}).get("application_sha256") or ""
            ),
            "required": list(requirements.values()),
            "unresolved_keys": unresolved,
            "request_context": {},
        }
    payload["request_context"] = {
        "initial_text_sha256": text_sha256(initial_text),
        "clarification_text_sha256": text_sha256(clarification_text) if clarification_text else "",
        "clarification_present": bool(compact(clarification_text)),
    }
    return payload


def evaluate_project_business_decisions(
    project_root: Path | None,
    initial_text: str,
    clarification_text: str = "",
) -> dict:
    if not project_root:
        return empty_business_decisions("not_available")
    root = project_root.resolve()
    if not (root / "rules" / "store.json").exists():
        return empty_business_decisions("not_available")
    try:
        from sql_project import evaluate_rule_context

        rule_context = evaluate_rule_context(
            root=root,
            user_request=initial_text,
            mode="generation",
            lifecycle_stage="temporary_query",
        )
        return resolve_business_decisions(rule_context, initial_text, clarification_text)
    except Exception as exc:
        payload = empty_business_decisions("error")
        payload["error"] = str(exc)
        payload["request_context"] = {
            "initial_text_sha256": text_sha256(initial_text),
            "clarification_text_sha256": text_sha256(clarification_text) if clarification_text else "",
            "clarification_present": bool(compact(clarification_text)),
        }
        return payload


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def lower_compact(text: str) -> str:
    return compact(text).lower()


def contains_any(text_lower: str, terms: Iterable[str]) -> list[str]:
    hits = []
    for term in terms:
        if term.lower() in text_lower:
            hits.append(term)
    return hits


def read_project_context(project_root: Path | None) -> dict:
    if not project_root:
        return {
            "project_root": "",
            "project_known": False,
            "project_id": "",
            "display_name": "",
            "config_loaded": False,
            "config_warnings": [],
            "default_query_window": {},
        }
    root = project_root.resolve()
    context = {
        "project_root": str(root),
        "project_known": root.exists(),
        "project_id": "",
        "display_name": root.name if root.exists() else "",
        "config_loaded": False,
        "config_warnings": [],
        "default_query_window": {},
    }
    config_path = root / "project_config.json"
    if not config_path.exists():
        context["config_warnings"].append("project_config.json not found")
        return context
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        context["config_warnings"].append(f"project_config.json is invalid JSON: {exc}")
        return context
    context["config_loaded"] = True
    context["project_id"] = str(config.get("project_id") or "")
    context["display_name"] = str(config.get("display_name") or context["display_name"])
    context["sql_dialect"] = str(config.get("sql_dialect") or "")
    context["query_engine"] = str(config.get("query_engine") or "")
    context["default_query_window"] = resolve_query_window(config)
    return context


def extract_project(text: str, project_context: dict) -> Slot:
    matches = [m.group(1).replace(" ", "-").replace("_", "-").upper() for m in PROJECT_PATTERN.finditer(text)]
    if matches:
        return Slot("project", "present", matches[0], matches[:3])
    if project_context.get("project_known"):
        value = project_context.get("display_name") or project_context.get("project_id") or Path(project_context["project_root"]).name
        return Slot("project", "provided_by_context", value, [str(project_context.get("project_root", ""))])
    return Slot("project", "missing", None, [])


def extract_metrics(text: str) -> Slot:
    values: list[str] = []
    evidence: list[str] = []
    for label, pattern in METRIC_PATTERNS:
        found = re.findall(pattern, text, flags=re.IGNORECASE)
        if found:
            values.append(label)
            evidence.extend([str(item) for item in found[:3]])
    values = list(dict.fromkeys(values))
    return Slot("metrics", "present" if values else "missing", values, evidence[:10])


def extract_time_range(text: str, project_context: dict) -> Slot:
    evidence: list[str] = []
    for pattern in DATE_PATTERNS:
        evidence.extend(pattern.findall(text))
    evidence = [str(item) for item in evidence if str(item).strip()]
    if evidence:
        return Slot("time_range", "present", evidence[:3], evidence[:5])
    default_window = project_context.get("default_query_window")
    if isinstance(default_window, dict) and default_window.get("status") == "ready":
        value = {
            "pt_start": default_window["pt_start"],
            "pt_end": default_window["pt_end"],
            "source": default_window["source"],
            "mode": default_window["mode"],
            "materialization": default_window["materialization"],
        }
        return Slot(
            "time_range",
            "provided_by_context",
            value,
            [
                f"project default: {default_window['pt_start']} <= date <= {default_window['pt_end']}",
                "project_start_to_yesterday",
            ],
        )
    return Slot("time_range", "missing", None, [])


def extract_grain_and_dimensions(text: str) -> tuple[Slot, Slot]:
    dimensions: list[str] = []
    evidence: list[str] = []
    checks = [
        ("day", ["按天", "按日", "每天", "每日", "日粒度"]),
        ("hour", ["按小时", "小时粒度", "每小时"]),
        ("week", ["按周", "每周", "周粒度"]),
        ("month", ["按月", "每月", "月粒度"]),
        ("zone", ["按区服", "分区服", "按izoneareaid"]),
        ("server", ["按服务器", "分服务器"]),
        ("game_mode", ["按模式", "分模式", "按gamemode"]),
        ("package", ["按包体", "分包体", "按渠道", "分渠道", "按package"]),
        ("bucket", ["分桶", "档位", "区间"]),
    ]
    lowered = lower_compact(text)
    for value, terms in checks:
        hits = contains_any(lowered, terms)
        if hits:
            dimensions.append(value)
            evidence.extend(hits[:2])
    for match in re.findall(r"按([^，。；,;]+)", text):
        phrase = re.split(r"(?:看|查|统计|求|取|拉|输出|导出)", match.strip(), maxsplit=1)[0].strip()
        if phrase in {"天", "日", "每天", "每日", "小时", "周", "月"}:
            continue
        if phrase.lower() in {"区服", "服务器", "模式", "gamemode", "包体", "渠道", "package"}:
            continue
        if any(term in phrase for term in METRIC_TERMS):
            continue
        if phrase and len(phrase) <= 20:
            dimensions.append(phrase)
            evidence.append(f"按{phrase}")
    dimensions = list(dict.fromkeys(dimensions))
    if "总计" in text or "总数" in text or "一共" in text:
        return (
            Slot("grain", "present", "total", ["总计/总数"]),
            Slot("dimensions", "present" if dimensions else "not_required", dimensions, evidence),
        )
    status = "present" if dimensions else "missing"
    return Slot("grain", status, dimensions[0] if dimensions else None, evidence[:8]), Slot(
        "dimensions",
        "present" if dimensions else "missing",
        dimensions,
        evidence[:8],
    )


def extract_filters(text: str) -> Slot:
    values: list[str] = []
    evidence: list[str] = []
    for name, pattern in FILTER_PATTERNS:
        for match in pattern.finditer(text):
            value = re.sub(r"\s+", "", match.group(1).replace("，", ",").replace("、", ","))
            if value:
                values.append(f"{name}={value}")
                evidence.append(match.group(0))
    return Slot("filters", "present" if values else "not_provided", list(dict.fromkeys(values)), evidence[:8])


def extract_population(text: str) -> Slot:
    lowered = lower_compact(text)
    checks = [
        ("player", ["玩家", "用户", "vopenid", "openid", "账号"]),
        ("role", ["角色", "roleid"]),
        ("match", ["对局", "战局", "battle", "局"]),
        ("item", ["道具", "物品", "item"]),
        ("order", ["订单", "付费", "充值"]),
    ]
    values: list[str] = []
    evidence: list[str] = []
    for value, terms in checks:
        hits = contains_any(lowered, terms)
        if hits:
            values.append(value)
            evidence.extend(hits[:2])
    return Slot("population", "present" if values else "missing", values[:3] if values else None, evidence[:8])


def extract_output_shape(text: str) -> Slot:
    lowered = lower_compact(text)
    shapes = [
        ("detail", ["明细", "列表", "样本", "导出"]),
        ("time_series", ["趋势", "按天", "每日", "每天", "按小时", "每小时"]),
        ("distribution", ["分布", "分桶", "档位", "区间"]),
        ("funnel", ["漏斗", "转化"]),
        ("aggregate", ["统计", "总计", "总数", "人数", "次数"]),
    ]
    for shape, terms in shapes:
        hits = contains_any(lowered, terms)
        if hits:
            return Slot("output_shape", "present", shape, hits[:5])
    return Slot("output_shape", "missing", None, [])


def extract_source_logs(text: str) -> Slot:
    values = list(dict.fromkeys(SOURCE_LOG_PATTERN.findall(text)))
    return Slot("source_logs", "present" if values else "not_provided", values, values[:8])


def infer_route(text: str) -> tuple[str, list[Signal], dict[str, int]]:
    lowered = lower_compact(text)
    query_hits = contains_any(lowered, QUERY_ACTION_TERMS)
    metric_hits = contains_any(lowered, METRIC_TERMS)
    review_hits = contains_any(lowered, REVIEW_TERMS)
    rule_hits = contains_any(lowered, RULE_TERMS)
    dashboard_hits = contains_any(lowered, DASHBOARD_TERMS)
    validation_hits = contains_any(lowered, VALIDATION_TERMS)
    source_hits = contains_any(lowered, SOURCE_TERMS)
    knowledge_hits = contains_any(lowered, KNOWLEDGE_LOOKUP_TERMS)
    formalize_hits = contains_any(lowered, FORMALIZE_TERMS)
    intermediate_hits = contains_any(lowered, INTERMEDIATE_TERMS)
    project_admin_hits = contains_any(lowered, PROJECT_ADMIN_TERMS)

    scores = {
        "QUERY": len(query_hits) + len(metric_hits) * 2,
        "REVIEW": len(review_hits) * 3 + (2 if ".sql" in lowered and review_hits else 0),
        "RULES": len(rule_hits) * 3,
        "DASHBOARD": len(dashboard_hits) * 3,
        "VALIDATION": len(validation_hits) * 3,
        "KNOWLEDGE": len(knowledge_hits) * 3,
        "SOURCE_INTAKE": len(source_hits) * 3,
        "SQL_FORMALIZE": len(formalize_hits) * 3,
        "INTERMEDIATE_TABLE": len(intermediate_hits) * 3,
        "PROJECT_ADMIN": len(project_admin_hits) * 3,
    }
    if "sql" in lowered and any(term in lowered for term in ["review", "审查", "审核", "检查"]):
        scores["REVIEW"] += 3
    explicit_query_hits = [hit for hit in query_hits if hit not in {"看", "看看", "看一下", "看下", "求"}]
    rule_question = any(term in text for term in ["是什么", "怎么定义", "定义是什么", "如何定义", "怎么算", "如何算"])
    if rule_hits and not explicit_query_hits and (len(metric_hits) <= 1 or rule_question):
        scores["RULES"] += 2
    if rule_hits and rule_question and not explicit_query_hits:
        scores["RULES"] += 4
    if dashboard_hits and ("转成" in text or "生成" in text or "看板sql" in lowered):
        scores["DASHBOARD"] += 2

    route_hits = {
        "SQL_FORMALIZE": formalize_hits,
        "REVIEW": review_hits,
        "DASHBOARD": dashboard_hits,
        "VALIDATION": validation_hits,
        "RULES": rule_hits,
        "KNOWLEDGE": knowledge_hits,
        "SOURCE_INTAKE": source_hits,
        "INTERMEDIATE_TABLE": intermediate_hits,
        "PROJECT_ADMIN": project_admin_hits,
    }
    non_query_priority = [
        "SQL_FORMALIZE",
        "REVIEW",
        "DASHBOARD",
        "VALIDATION",
        "RULES",
        "KNOWLEDGE",
        "SOURCE_INTAKE",
        "INTERMEDIATE_TABLE",
        "PROJECT_ADMIN",
    ]
    for route in non_query_priority:
        if scores[route] >= 3 and scores[route] >= scores["QUERY"]:
            signals = [Signal("route", route, hit) for hit in route_hits[route][:5]]
            return route, signals, scores

    if scores["QUERY"] >= 3 or (query_hits and metric_hits):
        signals = [Signal("query_action", hit, hit) for hit in query_hits[:5]]
        signals.extend(Signal("metric", hit, hit) for hit in metric_hits[:8])
        return "QUERY", signals, scores

    if metric_hits or query_hits:
        signals = [Signal("weak_query", hit, hit) for hit in (metric_hits + query_hits)[:8]]
        return "UNKNOWN_QUERY_RELATED", signals, scores

    return "OUTSIDE_SCOPE", [], scores


def slot_map(slots: Iterable[Slot]) -> dict[str, Slot]:
    return {slot.name: slot for slot in slots}


def build_missing_and_questions(route: str, slots: dict[str, Slot], text: str) -> tuple[list[str], list[str], list[str]]:
    if route != "QUERY":
        return [], [], []
    missing: list[str] = []
    questions: list[str] = []
    notes: list[str] = []

    if slots["project"].status == "missing":
        missing.append("project")
        questions.append("这个取数需求属于哪个项目或哪个项目根目录？")
    if slots["metrics"].status == "missing":
        missing.append("metrics")
        questions.append("要取哪个指标或业务问题？")
    if slots["time_range"].status == "missing":
        missing.append("time_range")
        questions.append("统计哪个时间范围？")
    if slots["grain"].status == "missing" and slots["output_shape"].value != "detail":
        missing.append("grain")
        questions.append("输出按什么粒度？如果只要总计，请明确说总计。")
    if slots["population"].status == "missing" and slots["metrics"].value in (["人数"], ["次数"]):
        missing.append("population")
        questions.append("统计对象是谁？例如玩家、角色、对局、道具或订单。")

    if re.search(r"(?:区服|i?zoneareaid|服务器|gamesvrid)\s*(?:$|[，。；,;])", text, flags=re.IGNORECASE):
        missing.append("filter_value")
        questions.append("区服/服务器筛选的具体取值是什么？")

    if slots["filters"].status == "not_provided":
        notes.append("未识别到固定筛选条件；如果项目有默认区服、模式或包体，需要调用方补充或确认。")
    return list(dict.fromkeys(missing)), list(dict.fromkeys(questions)), notes


def assess(route: str, slots: dict[str, Slot], missing_slots: list[str]) -> tuple[bool, str, str, float]:
    if route == "QUERY":
        is_query = True
        if not missing_slots:
            return is_query, "clear_query", "handoff_to_query", 0.9
        if slots["metrics"].status == "present" or slots["time_range"].status == "present":
            confidence = 0.78 if "metrics" not in missing_slots else 0.66
            return is_query, "partially_clear", "ask_clarifying_question", confidence
        return is_query, "query_related", "ask_for_data_question", 0.58
    if route == "UNKNOWN_QUERY_RELATED":
        return False, "query_related", "ask_for_data_question", 0.55
    if route == "OUTSIDE_SCOPE":
        return False, "not_query", "ignore_or_ask_for_sql_request", 0.72
    return False, "not_query", "route_non_query", 0.82


def classify(
    text: str,
    project_root: Path | None = None,
    clarification_text: str = "",
) -> dict:
    text = compact(text)
    clarification_text = compact(clarification_text)
    project_context = read_project_context(project_root)
    route, route_signals, route_scores = infer_route(text)

    slots = [
        extract_project(text, project_context),
        extract_metrics(text),
        extract_time_range(text, project_context),
    ]
    grain_slot, dimensions_slot = extract_grain_and_dimensions(text)
    slots.extend(
        [
            grain_slot,
            dimensions_slot,
            extract_filters(text),
            extract_population(text),
            extract_output_shape(text),
            extract_source_logs(text),
        ]
    )
    slots_by_name = slot_map(slots)
    missing_slots, blocking_questions, non_blocking_notes = build_missing_and_questions(route, slots_by_name, text)
    business_decisions = (
        evaluate_project_business_decisions(project_root, text, clarification_text)
        if route == "QUERY"
        else empty_business_decisions("not_applicable")
    )
    if business_decisions["status"] == "needs_input":
        for row in business_decisions["required"]:
            if row["status"] == "resolved":
                continue
            missing_slots.append(f"business_decision:{row['key']}")
            if row.get("question"):
                blocking_questions.append(row["question"])
    elif business_decisions["status"] == "error":
        missing_slots.append("business_decision:rule_context")
        blocking_questions.append("项目口径决策检查失败，暂不能生成 SQL。")
    missing_slots = list(dict.fromkeys(missing_slots))
    blocking_questions = list(dict.fromkeys(blocking_questions))
    is_query, clarity, decision, confidence = assess(route, slots_by_name, missing_slots)

    if route == "UNKNOWN_QUERY_RELATED":
        route_hint = "QUERY"
    elif route == "OUTSIDE_SCOPE":
        route_hint = "outside_scope"
    else:
        route_hint = route

    extracted = {slot.name: slot.value for slot in slots}
    slot_payload = [asdict(slot) for slot in slots]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "is_data_query_request": is_query,
        "clarity": clarity,
        "route_hint": route_hint,
        "decision": decision,
        "confidence": round(confidence, 2),
        "extracted": extracted,
        "missing_slots": missing_slots,
        "blocking_questions": blocking_questions,
        "non_blocking_notes": non_blocking_notes,
        "evidence": {
            "route_scores": route_scores,
            "route_signals": [asdict(signal) for signal in route_signals],
            "slots": slot_payload,
        },
        "business_decisions": business_decisions,
        "project_context": project_context,
        "contract": {
            "read_only": True,
            "writes_assets": False,
            "generates_sql": False,
            "next_step_when_clear": "Call QUERY only when route_hint=QUERY and clarity=clear_query.",
        },
    }
    return payload


def render_markdown(payload: dict) -> str:
    lines = [
        f"# Requirement Intake: {payload['clarity']}",
        "",
        f"- route_hint: `{payload['route_hint']}`",
        f"- decision: `{payload['decision']}`",
        f"- confidence: `{payload['confidence']}`",
        f"- is_data_query_request: `{str(payload['is_data_query_request']).lower()}`",
    ]
    if payload["missing_slots"]:
        lines.append(f"- missing_slots: {', '.join(payload['missing_slots'])}")
    if payload["blocking_questions"]:
        lines.append("")
        lines.append("## Blocking Questions")
        lines.extend(f"- {question}" for question in payload["blocking_questions"])
    if payload["business_decisions"]["required"]:
        lines.append("")
        lines.append("## Business Decisions")
        for row in payload["business_decisions"]["required"]:
            value = json.dumps(row.get("value"), ensure_ascii=False) if row.get("value") is not None else "unresolved"
            lines.append(f"- `{row['key']}`: `{row['status']}` {value}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", help="User text to classify.")
    parser.add_argument("--text-file", help="UTF-8 text file to classify. Use - for stdin.")
    parser.add_argument(
        "--clarification-text",
        default="",
        help="Optional current clarification used only to resolve decisions activated by the original --text.",
    )
    parser.add_argument("--project-root", help="Optional local SQL project root for project context.")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument(
        "--require-clear-query",
        action="store_true",
        help="Exit with code 2 unless the result is a clear QUERY requirement.",
    )
    add_function_gate_arguments(
        parser,
        selection_help="Optional explicit route. Allowed here: 【需求判定】 or [REQUIREMENT_INTAKE].",
    )
    return parser


def read_input(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.text_file:
        if args.text_file == "-":
            return sys.stdin.read()
        return Path(args.text_file).read_text(encoding="utf-8")
    raise SystemExit("Provide --text or --text-file.")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("requirement_intake.py"),
            purpose="requirement intake",
        )
    except Exception as exc:  # pragma: no cover - shared gate formats this.
        exit_with_gate_error(parser, exc)

    project_root = Path(args.project_root) if args.project_root else None
    payload = classify(
        read_input(args),
        project_root=project_root,
        clarification_text=args.clarification_text,
    )
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(payload), end="")
    if args.require_clear_query and payload["clarity"] != "clear_query":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
