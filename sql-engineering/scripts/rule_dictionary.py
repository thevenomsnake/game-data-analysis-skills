#!/usr/bin/env python3
"""Build a read-only static canonical-rule dictionary viewer.

This is intentionally separate from rule_review.py:
- rule_review.py is for cross-project review and manual approve/reject state.
- rule_dictionary.py is a static catalogue for seeing what has been saved.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_rules import config_owned_rule_markers, dictionary_snapshot, has_v2_store

PROJECT_ORDER = ["DEMO_EXPERIMENT", "DEMO_AB_TEST", "DEMO_ANALYTICS"]
DEFAULT_HTML_REL = "_rule_review/rule_dictionary.html"
DEFAULT_JSON_REL = "_rule_review/rule_dictionary.json"
DEFAULT_CONCEPTS_REL = "_rule_review/rule_concepts.json"
COMPARE_FIELDS = [
    ("status", "状态"),
    ("title", "标题"),
    ("content", "口径内容"),
    ("applies_to", "适用范围"),
    ("source_evidence", "证据"),
    ("notes", "备注"),
    ("activation_contract", "激活条件"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slug_text(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(value).lower()).strip("-")
    return slug or hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:8]


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def rel_to_projects(projects_root: Path, path: Path | str) -> str:
    path_obj = Path(path)
    try:
        return path_obj.resolve().relative_to(projects_root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path).replace("\\", "/")


def rel_to_project(project_root: Path, path: Path | str) -> str:
    path_obj = Path(path)
    try:
        return path_obj.resolve().relative_to(project_root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path).replace("\\", "/")


def catalog_source_file(project_root: Path, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(project_root.resolve()).as_posix()
        except (OSError, ValueError):
            project_copy = project_root / "sources" / path.name
            if project_copy.exists():
                return rel_to_project(project_root, project_copy)
            return path.name
    return text.replace("\\", "/")


def project_sort_key(path: Path) -> tuple[int, str]:
    if path.name in PROJECT_ORDER:
        return (PROJECT_ORDER.index(path.name), path.name)
    return (99, path.name)


def discover_projects(projects_root: Path, explicit_projects: list[str] | None) -> list[Path]:
    if explicit_projects:
        return [projects_root / item for item in explicit_projects]
    if not projects_root.exists():
        return []
    return sorted(
        [
            path
            for path in projects_root.iterdir()
            if path.is_dir()
            and not path.name.startswith("_")
            and has_v2_store(path)
        ],
        key=project_sort_key,
    )


def load_concepts(path: Path) -> tuple[dict[str, dict], list[dict]]:
    registry = read_json(path, {"concepts": []})
    concepts = {}
    issues = []
    seen = set()
    for raw in registry.get("concepts", []):
        key = slug_text(raw.get("concept_key", ""))
        if not key:
            issues.append({"severity": "ERROR", "code": "missing_concept_key", "message": "concept registry entry has no concept_key"})
            continue
        if key in seen:
            issues.append({"severity": "ERROR", "code": "duplicate_concept_key", "message": f"duplicate concept_key: {key}", "concept_key": key})
            continue
        seen.add(key)
        concepts[key] = {
            "concept_key": key,
            "label": raw.get("label") or key,
            "description": raw.get("description", ""),
            "expected_projects": [str(item) for item in raw.get("expected_projects", [])],
            "keywords": [str(item) for item in raw.get("keywords", [])],
            "status": raw.get("status", "active"),
            "notes": raw.get("notes", ""),
            "concept_type": raw.get("concept_type", infer_concept_type(key)),
            "coverage_policy": raw.get("coverage_policy", infer_coverage_policy(key, raw)),
            "inheritance_policy": raw.get("inheritance_policy", "none"),
        }
    return concepts, issues


def infer_concept_type(key: str) -> str:
    if key == "tlog-xml-baseline":
        return "source_baseline"
    if key == "canonical-rule-source-boundary":
        return "governance_policy"
    if key == "project-default-player-scope":
        return "project_parameter"
    return "business_rule"


def infer_coverage_policy(key: str, raw: dict) -> str:
    if key == "tlog-xml-baseline":
        return "source_catalog"
    if key == "canonical-rule-source-boundary":
        return "global_governance"
    expected = raw.get("expected_projects", []) or []
    if len(expected) >= 2:
        return "all_expected_projects"
    return "stage_specific"


def config_owned_concepts(project_root: Path, config: dict | None = None) -> dict[str, dict[str, Any]]:
    """Compatibility name for the shared project-config ownership resolver."""

    return config_owned_rule_markers(project_root)


def load_project(project_root: Path) -> dict:
    manifest = read_json(project_root / "manifest.json", {})
    config = read_json(project_root / "project_config.json", {})
    rules_store = dictionary_snapshot(project_root, include_history=False)
    source_catalog = load_source_catalog(project_root)
    rules = []
    snapshot_concepts: dict[str, dict[str, Any]] = {}
    for concept in rules_store.get("concepts", []) or []:
        concept_key = slug_text(concept.get("concept_key", ""))
        current: list[dict[str, Any]] = []
        proposed: list[dict[str, Any]] = []
        for state, target in (("current", current), ("proposed", proposed)):
            for item in concept.get(state, []) or []:
                rule = dict(item)
                rule.setdefault("scope", "project")
                rule.setdefault("lifetime", "persistent")
                rule.setdefault("applies_to", "")
                rule.setdefault("affected_artifacts", [])
                rule.setdefault("decision_question", "")
                rule.setdefault("source_evidence", "")
                rule.setdefault("notes", "")
                rule.setdefault("confirmed_by_user", rule.get("status") == "confirmed")
                rule["concept_key"] = slug_text(
                    rule.get("concept_key")
                    or concept_key
                    or f"unmapped-{rule.get('rule_id', rule.get('title', 'rule'))}"
                )
                rule["_dictionary_view"] = build_dictionary_rule_view(project_root, rule)
                target.append(rule)
                rules.append(rule)
        snapshot_concepts[concept_key] = {
            "current": current,
            "proposed": proposed,
            "version_count": int(concept.get("version_count") or len(current) + len(proposed)),
            "semantic_version_count": int(
                concept.get("semantic_version_count") or len(current) + len(proposed)
            ),
            "history_summary": list(concept.get("history_summary", []) or []),
        }
    return {
        "slug": project_root.name,
        "name": config.get("display_name") or manifest.get("project_name") or project_root.name,
        "root": project_root.name,
        "config": {
            "sql_dialect": config.get("sql_dialect", "missing"),
            "query_engine": config.get("query_engine", "missing"),
            "query_environment": environment_name(config.get("query_environment")),
            "dashboard_application": environment_name(config.get("dashboard_application")),
            "table_naming_profile": table_profile_name(config.get("table_naming_profile")),
            "table_naming_pattern": table_profile_pattern(config.get("table_naming_profile")),
            "partition_policy": config.get("partition_policy", {}),
        },
        "execution_profile": build_execution_profile(config),
        "source_catalog": source_catalog,
        "rule_store_snapshot": rules_store,
        "rule_concepts": snapshot_concepts,
        "config_owned_concepts": config_owned_concepts(project_root, config),
        "rules": sorted(rules, key=lambda rule: (rule.get("concept_key", ""), rule.get("rule_id", ""), int(rule.get("version") or 0))),
    }


def load_source_catalog(project_root: Path) -> dict:
    catalog_path = project_root / "sources" / "xml_catalog.json"
    catalog = read_json(catalog_path, None)
    if not catalog:
        xml_files = sorted((project_root / "sources").glob("*.xml")) if (project_root / "sources").exists() else []
        return {
            "present": False,
            "catalog_path": "",
            "source_file": rel_to_project(project_root, xml_files[0]) if xml_files else "",
            "generated_at": "",
            "log_count": 0,
            "field_count": 0,
            "sample_logs": [],
            "status": "not_cataloged" if xml_files else "missing",
        }
    logs = catalog.get("logs", []) if isinstance(catalog.get("logs"), list) else []
    field_count = sum(len(log.get("fields", []) or []) for log in logs if isinstance(log, dict))
    return {
        "present": True,
        "catalog_path": rel_to_project(project_root, catalog_path),
        "source_file": catalog_source_file(project_root, catalog.get("source_file", "")),
        "generated_at": catalog.get("generated_at", ""),
        "log_count": int(catalog.get("log_count") or len(logs)),
        "field_count": field_count,
        "sample_logs": [str(log.get("name", "")) for log in logs[:12] if isinstance(log, dict)],
        "status": "cataloged",
    }


def environment_name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("id") or "configured")
    return str(value or "missing")


def table_profile_name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "configured")
    return str(value or "missing")


def table_profile_pattern(value) -> str:
    if isinstance(value, dict):
        return str(value.get("pattern") or "")
    return ""


def build_execution_profile(config: dict) -> dict:
    dialect = str(config.get("sql_dialect") or "missing")
    engine = str(config.get("query_engine") or "missing")
    table_profile = config.get("table_naming_profile") or {}
    partition = config.get("partition_policy") or {}
    label = f"{dialect} / {engine}"
    profile_name = table_profile_name(table_profile)
    pattern = table_profile_pattern(table_profile)
    business_time = str(partition.get("business_time_field") or "")
    partition_field = str(partition.get("partition_field") or "")
    partition_required = partition.get("required_for_tlog") is True
    detail_time_note = f"{business_time or 'dtEventTime/dteventtime'} 只在详细事件时间逻辑需要时使用。"
    if dialect.lower() == "hive":
        meaning = f"Hive 是 SQL 方言；实际查询引擎/环境按项目配置为 {engine}。是否使用 TDBank 导入分区由 partition_policy 决定。"
        if partition_required:
            time_policy = (
                f"TLOG 查询必须裁剪分区/日期字段 {partition_field or '未配置'}；{detail_time_note}"
            )
            constraints = [
                f"必须带 {partition_field or '项目配置中的分区字段'} 上下界分区裁剪。",
                "纯日期范围计算可只使用分区/日期字段。",
                "params CTE 中使用 pt_start/pt_end；只有详细时间逻辑需要时才使用 ts_start/ts_end。",
                "不得混用 StarRocks-only 语法。",
            ]
        else:
            time_policy = (
                f"TLOG 查询不要求导入分区字段；使用项目确认的业务时间字段 {business_time or 'dtEventTime/dteventtime'} 做事件时间过滤。"
            )
            constraints = [
                "不得默认添加 tdbank_imp_date，除非项目配置、表 schema 或用户明确确认存在且需要。",
                "业务时间字段必须同时有上下界过滤。",
                "params CTE 中使用 ts_start/ts_end，避免 Hive 解析敏感别名。",
                "不得混用 StarRocks-only 语法。",
            ]
    elif dialect.lower() == "starrocks":
        meaning = "StarRocks 同时是 SQL 方言和查询执行平台。SQL 需要按 StarRocks 语法、函数和字段类型生成。"
        if partition_required:
            time_policy = (
                f"TLOG 查询必须裁剪分区/日期字段 {partition_field or '未配置'}；{detail_time_note}不默认使用 TDBank 的 tdbank_imp_date。"
            )
        else:
            time_policy = (
                f"TLOG 查询使用项目确认的业务时间字段 {business_time or 'dteventdate'}；不默认使用 TDBank 的 tdbank_imp_date。"
            )
        constraints = [
            "不得默认添加 tdbank_imp_date，除非表 schema 明确存在。",
            "纯日期范围计算可只使用分区/日期字段。",
            "Hive-only 函数和字符串日期假设需要转换或确认。",
            "正式 SQL 需要 schema/事件时间字段确认。",
            "不得混用 Hive-only 或 TDBank 查询平台专属语法。",
        ]
    else:
        meaning = "执行环境未完整配置，正式 SQL 不能可靠生成。"
        time_policy = "缺少明确时间/分区策略。"
        constraints = ["补齐 sql_dialect、query_engine、table_naming_profile、partition_policy 后再保留正式 SQL。"]
    return {
        "label": label,
        "sql_dialect": dialect,
        "query_engine": engine,
        "query_environment": environment_name(config.get("query_environment")),
        "dashboard_application": environment_name(config.get("dashboard_application")),
        "table_naming_profile": profile_name,
        "table_naming_pattern": pattern,
        "partition_policy_name": str(partition.get("name") or ""),
        "partition_required_for_tlog": partition_required,
        "partition_field": partition_field,
        "business_time_field": business_time,
        "meaning": meaning,
        "table_policy": f"逻辑日志名按 {profile_name} 映射为物理表；模式为 {pattern or '未配置'}。",
        "time_policy": time_policy,
        "sql_constraints": constraints,
    }


def status_rank(status: str) -> int:
    return {"confirmed": 4, "proposed": 3, "superseded": 2, "deprecated": 1}.get(status, 0)


def version_number(rule: dict) -> int:
    try:
        return int(rule.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def latest_rule(rules: list[dict]) -> dict | None:
    if not rules:
        return None
    active = [rule for rule in rules if rule.get("status") in {"confirmed", "proposed"}]
    candidates = active or rules
    return sorted(
        candidates,
        key=lambda rule: (
            status_rank(str(rule.get("status", ""))),
            version_number(rule),
            str(rule.get("updated_at", "")),
            str(rule.get("created_at", "")),
        ),
    )[-1]


def active_rules(rules: list[dict]) -> list[dict]:
    return [rule for rule in rules if rule.get("status") in {"confirmed", "proposed"}]


def compact_rule(rule: dict | None) -> dict | None:
    if not rule:
        return None
    keys = [
        "rule_id",
        "concept_key",
        "version",
        "semantic_version",
        "record_version",
        "record_store_version",
        "technical_revision_count",
        "semantic_fingerprint",
        "status",
        "title",
        "content",
        "source",
        "source_evidence",
        "confirmed_by_user",
        "scope",
        "lifetime",
        "applies_to",
        "affected_artifacts",
        "decision_question",
        "supersedes",
        "created_at",
        "updated_at",
        "notes",
        "activation_contract",
        "structured_view",
        "record_kind",
        "config_path",
        "config_pointer",
        "config_value",
        "migration_action",
    ]
    compact = {key: rule.get(key, "" if key != "affected_artifacts" else []) for key in keys}
    compact["structured_view"] = rule.get("_dictionary_view") or build_dictionary_rule_view(None, rule)
    return compact


def short_text(value: str, limit: int = 180) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def comparable_rule(rule: dict | None) -> dict:
    if not rule:
        return {}
    result = {}
    for field, _label in COMPARE_FIELDS:
        value = rule.get(field)
        if isinstance(value, str):
            value = re.sub(r"\s+", " ", value).strip()
        result[field] = value or ""
    return result


def rule_signature(rule: dict | None) -> str:
    if not rule:
        return ""
    return stable_hash(comparable_rule(rule))


def split_lines(value) -> list[str]:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    text = str(value or "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def strip_markdown_tables(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            continue
        if re.fullmatch(r"[-:| ]+", stripped):
            continue
        lines.append(line)
    return "\n".join(lines)


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_table_separator(line: str) -> bool:
    cells = table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def parse_markdown_tables(content: str) -> list[dict]:
    lines = str(content or "").splitlines()
    tables = []
    i = 0
    while i < len(lines) - 1:
        if lines[i].strip().startswith("|") and is_table_separator(lines[i + 1]):
            headers = table_cells(lines[i])
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                values = table_cells(lines[j])
                if len(values) < len(headers):
                    values += [""] * (len(headers) - len(values))
                rows.append({headers[idx]: values[idx] for idx in range(len(headers))})
                j += 1
            title = "结构化表"
            for back in range(i - 1, max(i - 4, -1), -1):
                candidate = lines[back].strip().rstrip("：:")
                if candidate and not candidate.startswith("|"):
                    title = candidate
                    break
            kind = "table"
            normalized_headers = {header.lower().strip(): header for header in headers}
            if "mode_id" in normalized_headers or "gamemode" in normalized_headers:
                kind = "mode_mapping"
                title = "GameMode 映射表"
            tables.append({"title": title, "kind": kind, "columns": headers, "rows": rows})
            i = j
            continue
        i += 1
    return tables


def parse_inline_mode_map(content: str) -> list[dict]:
    text = strip_markdown_tables(content)
    rows = []
    seen = set()
    for match in re.finditer(r"(?<![A-Za-z0-9_])(\d{1,5})\s*[=＝]\s*([^，。；;\n]+)", text):
        mode_id = match.group(1)
        mode_name = re.sub(r"\s+", " ", match.group(2)).strip()
        if not mode_name or re.search(r"\d", mode_name) and len(mode_name) < 3:
            continue
        key = (mode_id, mode_name)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"mode_id": mode_id, "mode_name": mode_name})
    return rows


def parse_id_values(raw: str) -> list[str]:
    return re.findall(r"\d{1,5}", str(raw or ""))


def condition_label(flag_or_label: str) -> str:
    mapping = {
        "has_normal": "常规模式",
        "has_fast": "快速模式",
        "has_newbie": "新手服",
    }
    return mapping.get(flag_or_label, flag_or_label)


def condition_flag(label: str) -> str:
    mapping = {
        "常规模式": "has_normal",
        "快速模式": "has_fast",
        "新手服": "has_newbie",
    }
    return label if label.startswith("has_") else mapping.get(label, "")


def parse_condition_sets(content: str) -> list[dict]:
    text = strip_markdown_tables(content)
    pattern = re.compile(
        r"(?P<label>has_normal|has_fast|has_newbie|常规模式|快速模式|新手服)\s*(?:=|：|:)\s*"
        r"(?:GameMode|gamemode)\s*(?P<op>IN|=)\s*(?P<values>\([^)]+\)|\d{1,5})",
        re.IGNORECASE,
    )
    rows = []
    seen = set()
    for match in pattern.finditer(text):
        raw_label = match.group("label")
        label = condition_label(raw_label)
        values = parse_id_values(match.group("values"))
        if not values:
            continue
        end = match.end()
        tail = text[end : end + 90]
        meaning_match = re.match(r"[，, ]*(?:表示|业务含义为)?([^。；;\n]+)", tail)
        meaning = meaning_match.group(1).strip(" ，,") if meaning_match else ""
        key = (label, tuple(values))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "label": label,
                "flag": condition_flag(raw_label),
                "field": "GameMode",
                "operator": match.group("op").upper(),
                "values": values,
                "business_meaning": meaning,
            }
        )
    return rows


def split_numbered_items(lines: list[str]) -> list[str]:
    text = " ".join(line.strip() for line in lines if line.strip())
    if not text:
        return []
    matches = list(re.finditer(r"(?:^|\s)(\d+)[.、]\s*", text))
    if not matches:
        return [text]
    items = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        item = text[start:end].strip(" ；;")
        if item:
            items.append(item)
    return items


def parse_rule_sections(content: str) -> list[dict]:
    text = strip_markdown_tables(content)
    sections = []
    heading = "当前规则"
    body = []

    def flush() -> None:
        nonlocal body
        items = split_numbered_items(body)
        if items:
            sections.append({"heading": heading, "items": items})
        body = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading_match = re.fullmatch(r"(.{2,32})[:：]", line)
        if heading_match and not re.search(r"\d+[.、]", line):
            flush()
            heading = heading_match.group(1).strip()
            continue
        body.append(line)
    flush()
    return sections


def extract_unknown_handling(content: str) -> list[str]:
    text = strip_markdown_tables(content)
    sentences = re.split(r"[。；;\n]+", text)
    result = []
    for sentence in sentences:
        item = sentence.strip()
        if not item:
            continue
        if re.search(r"未出现在|未配置|unknown|不得自行猜测|不得自动归入", item, re.IGNORECASE):
            result.append(re.sub(r"^\d+[.、]\s*", "", item))
    return list(dict.fromkeys(result))[:6]


def extract_rule_variables(content: str) -> list[dict]:
    text = str(content or "")
    candidates = [
        (r"\bGameMode\b|\bgamemode\b|gameModeID", "GameMode/gamemode", "模式 ID，用于映射玩法名称、类型或体验分类。"),
        (r"mode_name|game_mode_name|模式名称", "mode_name/game_mode_name", "GameMode 映射后的具体玩法名称。"),
        (r"mode_category|mode_type|模式大类|模式类型|体验分类", "mode_category/mode_type", "模式大类或体验分类，不能和模式名称互相替代。"),
        (r"\bBattleSrvId\b|\bbattlesrvid\b", "BattleSrvId/battlesrvid", "战斗服 ID；无 GameMode 的行为表需要用它归因模式。"),
        (r"\biZoneAreaID\b|\bizoneareaid\b", "iZoneAreaID", "业务区服/大区字段。"),
        (r"\bvOpenID\b|\bvopenid\b", "vOpenID", "玩家账号标识；用于去重统计，不应在最终看板明细暴露。"),
        (r"\bRoleID\b|\broleid\b", "RoleID", "玩家角色 ID，部分 Territory/Battle 归因需要与 vOpenID 稳定映射。"),
        (r"\bdtEventTime\b|\bdteventtime\b", "dtEventTime/dteventtime", "完整事件时间，用于窗口、顺序、区间和峰值归属。"),
        (r"\bdtEventDate\b|\bdteventdate\b", "dtEventDate/dteventdate", "demo_log 分区/日期粒度字段，按项目配置用于日期范围裁剪。"),
        (r"\btdbank_imp_date\b", "tdbank_imp_date", "TDBank 导入分区字段；只在项目 partition_policy 要求时使用。"),
    ]
    variables = []
    for pattern, name, meaning in candidates:
        if re.search(pattern, text, re.IGNORECASE):
            variables.append({"name": name, "meaning": meaning})
    return variables


def first_summary(content: str) -> str:
    text = strip_markdown_tables(content)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.split(r"[:：]\s*1[.、]\s*", text)[0]
    parts = [part.strip() for part in re.split(r"[。；;]", text) if part.strip()]
    if not parts:
        return ""
    summary = "。".join(parts[:2])
    if len(summary) > 220:
        summary = summary[:219] + "…"
    return summary


def infer_rule_kind(rule: dict, tables: list[dict], condition_sets: list[dict]) -> str:
    concept_key = str(rule.get("concept_key") or "")
    content = str(rule.get("content") or "")
    if concept_key == "game-mode-map" or any(table.get("kind") == "mode_mapping" for table in tables):
        return "mapping_table"
    if condition_sets:
        return "classification_rule"
    if re.search(r"分桶|桶边界|bucket", content, re.IGNORECASE):
        return "bucket_rule"
    if re.search(r"PCU|DAU|留存|率|人数|次数|时长|并发", content, re.IGNORECASE):
        return "metric_rule"
    if re.search(r"字段|含义|边界|不得.*替代", content):
        return "field_semantics"
    return "business_rule"


def rule_kind_label(kind: str) -> str:
    return {
        "mapping_table": "映射表",
        "classification_rule": "分类规则",
        "bucket_rule": "分桶规则",
        "metric_rule": "指标算法",
        "field_semantics": "字段边界",
        "business_rule": "业务规则",
    }.get(kind, kind)


def build_structured_rule_view(rule: dict) -> dict:
    content = str(rule.get("content") or "")
    tables = parse_markdown_tables(content)
    if not tables and rule.get("concept_key") == "game-mode-map":
        rows = parse_inline_mode_map(content)
        if rows:
            tables.append({"title": "GameMode 映射表", "kind": "mode_mapping", "columns": ["mode_id", "mode_name"], "rows": rows})
    condition_sets = parse_condition_sets(content)
    sections = parse_rule_sections(content)
    kind = infer_rule_kind(rule, tables, condition_sets)
    constraints = []
    other_sections = []
    for section in sections:
        heading = section.get("heading", "")
        if heading != "当前规则" and re.search(r"约束|禁止|规则|边界|默认|处理|质量|注意", heading):
            constraints.extend(section.get("items", []))
        else:
            other_sections.append(section)
    unknown_handling = extract_unknown_handling(content)
    return {
        "view_version": "rule_dictionary_structured_v1",
        "rule_kind": kind,
        "rule_kind_label": rule_kind_label(kind),
        "headline": first_summary(content) or rule.get("title", ""),
        "mapping_tables": tables,
        "condition_sets": condition_sets,
        "variables": extract_rule_variables(content),
        "constraints": list(dict.fromkeys(constraints))[:12],
        "unknown_handling": unknown_handling,
        "sections": other_sections[:8],
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def active_knowledge_bindings(project_root: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(project_root / "knowledge" / "bindings.json", {"bindings": []})
    return {
        str(item.get("dataset_id") or ""): item
        for item in payload.get("bindings", []) or []
        if isinstance(item, dict) and item.get("state") == "active" and item.get("dataset_id")
    }


def knowledge_table_for_dependency(project_root: Path, dependency: dict[str, Any]) -> dict[str, Any]:
    dataset_id = str(dependency.get("dataset_id") or "").strip()
    projection_id = str(dependency.get("projection_id") or "").strip()
    base = {
        "title": str(dependency.get("display_name") or dataset_id or "知识映射"),
        "kind": "knowledge_mapping",
        "columns": [],
        "rows": [],
        "dataset_id": dataset_id,
        "dataset_version": "",
        "projection_id": projection_id,
        "content_hash": "",
        "projection_sha256": "",
        "semantic_role": str(dependency.get("semantic_role") or ""),
        "status": "missing",
        "binding_status": "missing",
        "binding_note": "",
        "legacy_pin": {
            key: str(dependency.get(key) or "")
            for key in ("dataset_version", "content_hash", "projection_sha256")
            if dependency.get(key)
        },
        "message": "",
    }
    if not (dataset_id and projection_id):
        base["message"] = "规则缺少资料集或投影定位信息。"
        return base

    repo_root = project_root.parent.parent
    binding = active_knowledge_bindings(project_root).get(dataset_id) or {}
    dataset_version = str(binding.get("dataset_version") or "")
    manifest_relative = str(
        binding.get("dataset_manifest_path")
        or f"knowledge-base/datasets/{dataset_id}/{dataset_version}/manifest.json"
    )
    if not dataset_version:
        base["message"] = f"项目没有激活资料：{dataset_id}"
        return base
    manifest_path = repo_root / manifest_relative
    if not manifest_path.exists():
        base["message"] = f"知识版本不存在：{dataset_id}/{dataset_version}"
        return base
    manifest = read_json(manifest_path, {})
    base["title"] = str(manifest.get("display_name") or base["title"])
    if str(manifest.get("version") or "") != dataset_version or str(
        manifest.get("content_hash") or ""
    ) != str(binding.get("content_hash") or ""):
        base["message"] = "项目资料绑定与知识清单不一致。"
        return base
    projection = next(
        (
            item
            for item in manifest.get("projections", []) or []
            if isinstance(item, dict) and item.get("projection_id") == projection_id
        ),
        None,
    )
    if not projection:
        base["message"] = f"知识版本中不存在投影：{projection_id}"
        return base
    data_path = repo_root / str(projection.get("data_path") or "")
    if not data_path.exists():
        base["message"] = f"知识投影数据不存在：{projection.get('data_path') or projection_id}"
        return base
    expected_sha = str(projection.get("sha256") or "")
    if expected_sha and file_sha256(data_path) != expected_sha:
        base["message"] = "项目当前知识投影文件哈希不一致。"
        return base

    with data_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        source_columns = list(reader.fieldnames or [])
        requested_fields = [str(item) for item in dependency.get("fields", []) or [] if str(item)]
        columns = requested_fields or source_columns
        missing_fields = sorted(set(columns) - set(source_columns))
        if missing_fields:
            base["message"] = "项目当前知识投影缺少字段：" + ", ".join(missing_fields)
            return base
        rows = [{field: str(row.get(field) or "") for field in columns} for row in reader]
    base.update(
        {
            "dataset_version": dataset_version,
            "content_hash": str(manifest.get("content_hash") or ""),
            "projection_sha256": expected_sha,
            "columns": columns,
            "rows": rows,
            "status": "ready",
            "binding_status": "active_compatible",
            "binding_note": "读取项目当前资料绑定；资料换版不会创建业务口径版本。",
            "message": "",
        }
    )
    return base


def build_dictionary_rule_view(project_root: Path | None, rule: dict[str, Any]) -> dict[str, Any]:
    parsed = build_structured_rule_view(rule)
    structured = rule.get("structured_definition") if isinstance(rule.get("structured_definition"), dict) else {}
    dependencies = [item for item in structured.get("knowledge_dependencies", []) or [] if isinstance(item, dict)]
    tables = []
    if project_root:
        tables = [knowledge_table_for_dependency(project_root, item) for item in dependencies]
    current_fact = str(structured.get("current_fact") or "").strip()
    if current_fact:
        parsed["headline"] = current_fact
    if dependencies:
        parsed["rule_kind"] = "knowledge_bound_rule"
        parsed["rule_kind_label"] = "规则 + 知识映射"
    parsed["knowledge_dependencies"] = dependencies
    parsed["knowledge_tables"] = tables
    parsed["knowledge_status"] = (
        "ready"
        if tables
        and all(
            item.get("status") == "ready"
            and item.get("binding_status") == "active_compatible"
            for item in tables
        )
        else ("missing" if dependencies else "not_required")
    )
    return parsed


def line_diff(before, after, limit: int = 5) -> dict:
    before_lines = split_lines(before)
    after_lines = split_lines(after)
    removed = []
    added = []
    for line in difflib.ndiff(before_lines, after_lines):
        if line.startswith("- ") and len(removed) < limit:
            removed.append(line[2:])
        elif line.startswith("+ ") and len(added) < limit:
            added.append(line[2:])
        if len(removed) >= limit and len(added) >= limit:
            break
    return {"removed": removed, "added": added}


def compare_rules(before: dict | None, after: dict | None) -> dict:
    if before and after and rule_signature(before) == rule_signature(after):
        return {"change_type": "unchanged", "field_changes": [], "content_diff": {"removed": [], "added": []}}
    if not before and after:
        return {
            "change_type": "added",
            "field_changes": [{"field": "content", "label": "口径内容", "before": "", "after": short_text(after.get("content", ""))}],
            "content_diff": {"removed": [], "added": split_lines(after.get("content", ""))[:5]},
        }
    if before and not after:
        return {
            "change_type": "removed",
            "field_changes": [{"field": "content", "label": "口径内容", "before": short_text(before.get("content", "")), "after": ""}],
            "content_diff": {"removed": split_lines(before.get("content", ""))[:5], "added": []},
        }
    if not before and not after:
        return {"change_type": "both_missing", "field_changes": [], "content_diff": {"removed": [], "added": []}}

    changes = []
    for field, label in COMPARE_FIELDS:
        before_value = comparable_rule(before).get(field, "")
        after_value = comparable_rule(after).get(field, "")
        if before_value != after_value:
            changes.append(
                {
                    "field": field,
                    "label": label,
                    "before": short_text(before.get(field, "")),
                    "after": short_text(after.get(field, "")),
                }
            )
    return {
        "change_type": "changed",
        "field_changes": changes,
        "content_diff": line_diff(before.get("content", ""), after.get("content", "")),
    }


def evolution_label(status: str) -> str:
    return {
        "changed": "项目间有变化",
        "partial_changed": "部分项目缺失且有变化",
        "partial": "部分项目缺失",
        "single_project": "仅一个项目有口径",
        "consistent": "三阶段一致",
        "missing": "无当前口径",
    }.get(status, status)


def build_evolution(projects: list[dict], project_cells: dict[str, dict]) -> dict:
    ordered = [project["slug"] for project in projects]
    nodes = []
    transitions = []
    present_count = 0
    changed_count = 0
    missing_count = 0
    for slug in ordered:
        cell = project_cells.get(slug, {})
        current = cell.get("current")
        if current:
            present_count += 1
        else:
            missing_count += 1
        nodes.append(
            {
                "project": slug,
                "status": cell.get("status", "missing"),
                "present": bool(current),
                "rule_id": current.get("rule_id", "") if current else "",
                "version": (
                    current.get("semantic_version", current.get("version", ""))
                    if current
                    else ""
                ),
                "title": current.get("title", "") if current else "",
                "hash": rule_signature(current),
            }
        )
    for left, right in zip(ordered, ordered[1:]):
        left_rule = (project_cells.get(left) or {}).get("current")
        right_rule = (project_cells.get(right) or {}).get("current")
        comparison = compare_rules(left_rule, right_rule)
        if comparison["change_type"] == "changed":
            changed_count += 1
        transitions.append(
            {
                "from_project": left,
                "to_project": right,
                "from_status": (project_cells.get(left) or {}).get("status", "missing"),
                "to_status": (project_cells.get(right) or {}).get("status", "missing"),
                **comparison,
            }
        )
    if present_count == 0:
        status = "missing"
    elif present_count == 1:
        status = "single_project"
    elif missing_count and changed_count:
        status = "partial_changed"
    elif missing_count:
        status = "partial"
    elif changed_count:
        status = "changed"
    else:
        status = "consistent"
    latest_transition = transitions[-1] if transitions else None
    return {
        "evolution_status": status,
        "evolution_label": evolution_label(status),
        "evolution_change_count": changed_count,
        "evolution_missing_count": missing_count,
        "evolution_nodes": nodes,
        "evolution_transitions": transitions,
        "latest_transition": latest_transition,
    }


def should_warn_expected_missing(meta: dict) -> bool:
    return meta.get("coverage_policy") in {"all_expected_projects", "explicit_per_stage"}


def evolution_rank(status: str) -> int:
    return {
        "partial_changed": 0,
        "changed": 1,
        "partial": 2,
        "single_project": 3,
        "missing": 4,
        "consistent": 5,
    }.get(status, 9)


def extract_terms(*values: str) -> list[str]:
    text = "\n".join(str(value or "") for value in values)
    candidates = []
    patterns = [
        r"\b[A-Za-z][A-Za-z0-9_]{2,}\b",
        r"`([^`]+)`",
        r"([A-Za-z][A-Za-z0-9_]*\s*(?:=|<>|>|<|IN)\s*[^，。；;\n]+)",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text):
            item = match if isinstance(match, str) else " ".join(match)
            item = str(item).strip()
            if len(item) > 2:
                candidates.append(item)
    cleaned = []
    seen = set()
    stopwords = {
        "and",
        "or",
        "case",
        "when",
        "then",
        "else",
        "end",
        "select",
        "from",
        "where",
        "group",
        "order",
        "by",
        "confirmed",
        "proposed",
    }
    for item in candidates:
        key = item.lower()
        if key in stopwords or key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
        if len(cleaned) >= 18:
            break
    return cleaned


def build_payload(projects_root: Path, concept_registry: Path, explicit_projects: list[str] | None = None) -> dict:
    concepts_meta, registry_issues = load_concepts(concept_registry)
    projects = [load_project(path) for path in discover_projects(projects_root, explicit_projects)]
    concept_keys = set(concepts_meta)
    for project in projects:
        concept_keys.update(rule.get("concept_key", "") for rule in project["rules"])

    rows = []
    issues = list(registry_issues)
    totals = Counter()
    for key in sorted(concept_keys):
        meta = concepts_meta.get(key) or {
            "concept_key": key,
            "label": f"未登记 concept_key：{key}",
            "description": "项目规则使用了未登记的 concept_key，需要补充到 rule_concepts.json 或修正项目规则。",
            "expected_projects": [],
            "keywords": [],
            "status": "active",
            "notes": "",
            "concept_type": "business_rule",
            "coverage_policy": "unregistered",
            "inheritance_policy": "none",
        }
        if key not in concepts_meta:
            issues.append({"severity": "ERROR", "code": "unregistered_concept_key", "message": f"unregistered concept_key: {key}", "concept_key": key})
        project_cells = {}
        all_latest_content = []
        status_counter = Counter()
        present_count = 0
        active_count = 0
        confirmed_count = 0
        proposed_count = 0
        config_owned_count = 0
        for project in projects:
            snapshot = (project.get("rule_concepts") or {}).get(key) or {}
            confirmed = list(snapshot.get("current", []) or [])
            proposals = list(snapshot.get("proposed", []) or [])
            config_marker = (project.get("config_owned_concepts") or {}).get(key)
            current = (confirmed[0] if confirmed else None) or config_marker
            status = current.get("status") if current else ("proposed" if proposals else "missing")
            status_counter[status] += 1
            if current or proposals:
                present_count += 1
                for record in ([current] if current else []) + proposals:
                    all_latest_content.append(record.get("title", ""))
                    all_latest_content.append(record.get("content", ""))
                if current and current.get("status") == "confirmed":
                    confirmed_count += 1
                if proposals:
                    proposed_count += 1
                if current and current.get("status") == "config_owned":
                    config_owned_count += 1
            if current or proposals:
                active_count += 1
            if len(confirmed) > 1:
                issues.append(
                    {
                        "severity": "ERROR",
                        "code": "multiple_current_confirmed_rules",
                        "message": f"{project['slug']} has multiple current confirmed rules for {key}",
                        "concept_key": key,
                        "project": project["slug"],
                    }
                )
            version_count = int(snapshot.get("version_count") or len(confirmed) + len(proposals))
            semantic_version_count = int(
                snapshot.get("semantic_version_count") or len(confirmed) + len(proposals)
            )
            history_count = max(0, version_count - len(confirmed) - len(proposals))
            project_cells[project["slug"]] = {
                "present": bool(current or proposals),
                "status": status,
                "current": compact_rule(current),
                "proposals": [compact_rule(rule) for rule in proposals],
                "proposal_count": len(proposals),
                "active_count": len(confirmed) + len(proposals) + (1 if config_marker else 0),
                "version_count": version_count,
                "semantic_version_count": semantic_version_count,
                "history_count": history_count,
                "hash": stable_hash(([current] if current else []) + proposals),
            }

        expected_missing = []
        for project_slug in meta.get("expected_projects", []):
            if project_slug in project_cells and not project_cells[project_slug]["present"]:
                expected_missing.append(project_slug)
                if should_warn_expected_missing(meta):
                    issues.append(
                        {
                            "severity": "WARN",
                            "code": "expected_project_missing",
                            "message": f"{key} expects {project_slug}, but no rule is saved there",
                            "concept_key": key,
                            "project": project_slug,
                        }
                    )
        row = {
            "concept_key": key,
            "label": meta.get("label", key),
            "description": meta.get("description", ""),
            "registry_status": "registered" if key in concepts_meta else "unregistered",
            "concept_status": meta.get("status", "active"),
            "notes": meta.get("notes", ""),
            "concept_type": meta.get("concept_type", "business_rule"),
            "coverage_policy": meta.get("coverage_policy", "stage_specific"),
            "inheritance_policy": meta.get("inheritance_policy", "none"),
            "keywords": meta.get("keywords", []),
            "expected_projects": meta.get("expected_projects", []),
            "expected_missing": expected_missing,
            "present_count": present_count,
            "active_project_count": active_count,
            "confirmed_project_count": confirmed_count,
            "proposed_project_count": proposed_count,
            "config_owned_project_count": config_owned_count,
            "status_counts": dict(status_counter),
            "terms": extract_terms(meta.get("label", ""), meta.get("description", ""), *all_latest_content),
            "project_cells": project_cells,
        }
        row.update(build_evolution(projects, project_cells))
        totals["concepts"] += 1
        totals["present_cells"] += present_count
        totals["confirmed_cells"] += confirmed_count
        totals["proposed_cells"] += proposed_count
        totals["config_owned_cells"] += config_owned_count
        if row["registry_status"] == "unregistered":
            totals["unregistered_concepts"] += 1
        rows.append(row)

    for project in projects:
        for rule in project["rules"]:
            totals[f"rule_status_{rule.get('status', 'unknown')}"] += 1

    rows.sort(
        key=lambda row: (
            row["registry_status"] != "unregistered",
            evolution_rank(row.get("evolution_status", "")),
            -row["confirmed_project_count"],
            -row["present_count"],
            row["label"],
            row["concept_key"],
        )
    )
    status = "fail" if any(item.get("severity") == "ERROR" for item in issues) else ("warn" if issues else "pass")
    return {
        "generated_at": now_iso(),
        "projects_root": ".",
        "concept_registry": rel_to_projects(projects_root, concept_registry),
        "status": status,
        "summary": {
            "projects": len(projects),
            "registered_concepts": len(concepts_meta),
            "concepts_in_dictionary": len(rows),
            "saved_rules": sum(len(project["rules"]) for project in projects),
            "confirmed_rules": totals.get("rule_status_confirmed", 0),
            "proposed_rules": totals.get("rule_status_proposed", 0),
            "config_owned_concepts": totals.get("config_owned_cells", 0),
            "superseded_rules": totals.get("rule_status_superseded", 0),
            "deprecated_rules": totals.get("rule_status_deprecated", 0),
            "unregistered_concepts": totals.get("unregistered_concepts", 0),
            "issues": len(issues),
        },
        "projects": projects_for_payload(projects),
        "concepts": rows,
        "issues": issues,
    }


def projects_for_payload(projects: list[dict]) -> list[dict]:
    return [
        {
            "slug": project["slug"],
            "name": project["name"],
            "root": project["root"],
            "config": project["config"],
            "execution_profile": project.get("execution_profile", {}),
            "source_catalog": project.get("source_catalog", {}),
            "rule_count": len(project["rules"]),
            "config_owned_count": len(project.get("config_owned_concepts") or {}),
            "status_counts": dict(Counter(rule.get("status", "unknown") for rule in project["rules"])),
        }
        for project in projects
    ]


def html_page(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>口径字典</title>
  <style>
    :root {{
      --bg: #f5f7fa;
      --surface: #ffffff;
      --line: #d7dde7;
      --text: #19212b;
      --muted: #667085;
      --accent: #0b5cad;
      --good: #087443;
      --warn: #a45b00;
      --bad: #b42318;
      --soft: #eef2f6;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif; }}
    header {{ min-height: 58px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 18px; background: var(--surface); border-bottom: 1px solid var(--line); }}
    h1 {{ margin: 0; font-size: 20px; letter-spacing: 0; }}
    h2 {{ margin: 0; font-size: 17px; }}
    h3 {{ margin: 0; font-size: 15px; }}
    .muted {{ color: var(--muted); }}
    .layout {{ display: grid; grid-template-columns: 360px 1fr; min-height: calc(100vh - 58px); }}
    aside {{ background: var(--surface); border-right: 1px solid var(--line); min-width: 0; }}
    .toolbar {{ padding: 12px; display: grid; gap: 10px; border-bottom: 1px solid var(--line); }}
    input, select {{ width: 100%; min-height: 36px; border: 1px solid var(--line); border-radius: 6px; background: #fff; color: var(--text); padding: 7px 9px; font: inherit; }}
    button {{ min-height: 32px; border: 1px solid var(--line); border-radius: 6px; background: #fff; color: var(--text); padding: 6px 9px; cursor: pointer; }}
    button.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .chip {{ display: inline-flex; align-items: center; min-height: 22px; padding: 2px 7px; border-radius: 999px; background: var(--soft); color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .chip.good {{ background: #e7f6ee; color: var(--good); }}
    .chip.warn {{ background: #fff2dc; color: var(--warn); }}
    .chip.bad {{ background: #fff0ee; color: var(--bad); }}
    .chip.accent {{ background: #e7f0fb; color: var(--accent); }}
    .list {{ max-height: calc(100vh - 180px); overflow: auto; }}
    .item {{ padding: 12px; display: grid; gap: 8px; border-bottom: 1px solid var(--line); cursor: pointer; }}
    .item:hover {{ background: #f8fbff; }}
    .item.selected {{ background: #eaf3ff; box-shadow: inset 3px 0 0 var(--accent); }}
    .item-title {{ font-weight: 650; font-size: 14px; }}
    .item-sub {{ color: var(--muted); font-size: 12px; line-height: 1.35; }}
    .group-title {{ padding: 12px 12px 6px; color: var(--muted); font-size: 12px; font-weight: 700; display: flex; justify-content: space-between; gap: 8px; }}
    .group-title span:last-child {{ font-weight: 500; }}
    main {{ padding: 16px; overflow: auto; max-height: calc(100vh - 58px); }}
    .summary {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .card, .panel {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }}
    .card {{ padding: 12px; display: grid; gap: 5px; }}
    .card strong {{ font-size: 22px; }}
    .panel {{ margin-bottom: 14px; }}
    .panel-head {{ padding: 12px 14px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
    .panel-body {{ padding: 12px 14px; display: grid; gap: 12px; }}
    .rule-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 12px; }}
    .project-card {{ border: 1px solid var(--line); border-radius: 8px; background: #fff; padding: 12px; display: grid; gap: 10px; align-content: start; }}
    .project-card.missing {{ border-style: dashed; background: #fbfcfe; }}
    .kv {{ display: grid; grid-template-columns: 118px minmax(0, 1fr); gap: 8px; padding-bottom: 7px; border-bottom: 1px solid #edf0f5; font-size: 13px; }}
    .kv span:first-child {{ color: var(--muted); }}
    .content {{ white-space: pre-wrap; word-break: break-word; line-height: 1.5; font-size: 13px; padding: 10px; border: 1px solid #edf0f5; border-radius: 6px; background: #f8fafc; }}
    .rule-headline {{ padding: 10px; border: 1px solid #dce9f8; border-radius: 6px; background: #f7fbff; line-height: 1.5; font-size: 13px; }}
    .section-title {{ font-size: 13px; font-weight: 700; color: var(--text); }}
    .structured-block {{ display: grid; gap: 8px; }}
    .structured-block ul {{ margin: 0; padding-left: 18px; display: grid; gap: 5px; }}
    .data-table-wrap {{ overflow-x: auto; border: 1px solid #edf0f5; border-radius: 6px; background: #fff; }}
    table.data-table {{ width: 100%; border-collapse: collapse; min-width: 420px; font-size: 13px; }}
    .data-table th, .data-table td {{ text-align: left; vertical-align: top; padding: 7px 8px; border-bottom: 1px solid #edf0f5; }}
    .data-table th {{ background: #f8fafc; color: var(--muted); font-weight: 700; white-space: nowrap; }}
    .data-table tr:last-child td {{ border-bottom: 0; }}
    .compare-table {{ min-width: 760px; }}
    .compare-cell {{ display: grid; gap: 3px; line-height: 1.4; }}
    .compare-cell.missing {{ color: var(--muted); }}
    .compare-cell.diff {{ color: var(--accent); font-weight: 600; }}
    .change-list {{ display: grid; gap: 8px; }}
    .change-item {{ padding: 8px 10px; border: 1px solid #dce9f8; border-radius: 6px; background: #f7fbff; line-height: 1.45; font-size: 13px; }}
    .shared-rule {{ display: grid; gap: 8px; padding: 10px; border: 1px solid #edf0f5; border-radius: 6px; background: #fbfcfe; }}
    .project-detail summary {{ font-size: 14px; }}
    .raw-rule {{ margin-top: 2px; }}
    details {{ border: 1px solid #edf0f5; border-radius: 6px; padding: 8px 10px; background: #fbfcfe; }}
    details summary {{ cursor: pointer; font-weight: 600; }}
    .timeline {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }}
    .timeline-node {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fff; display: grid; gap: 6px; }}
    .timeline-node.missing {{ border-style: dashed; background: #fbfcfe; }}
    .transition {{ border: 1px solid #edf0f5; border-radius: 8px; padding: 10px; display: grid; gap: 8px; background: #fbfcfe; }}
    .diff-lines {{ display: grid; gap: 4px; font-size: 12px; }}
    .diff-line {{ padding: 5px 7px; border-radius: 6px; word-break: break-word; }}
    .diff-line.add {{ background: #e7f6ee; color: var(--good); }}
    .diff-line.remove {{ background: #fff0ee; color: var(--bad); }}
    .source-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; }}
    .note {{ padding: 9px 10px; border-radius: 6px; background: #fff8e8; border: 1px solid #f0d49a; color: #694100; font-size: 13px; line-height: 1.45; }}
    .issue {{ display: grid; gap: 4px; padding: 8px 0; border-bottom: 1px solid #edf0f5; }}
    .load-status {{ font-size: 12px; line-height: 1.4; color: var(--muted); }}
    @media (max-width: 960px) {{
      .layout {{ grid-template-columns: 1fr; }}
      aside {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .list {{ max-height: 320px; }}
      .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 560px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      .summary {{ grid-template-columns: 1fr; }}
      .kv {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>口径字典</h1>
      <div class="muted" id="generated"></div>
    </div>
    <div class="chips" id="headerChips"></div>
  </header>
  <div class="layout">
    <aside>
      <div class="toolbar">
        <div class="load-status">当前页只读取 RuleStore 生成的 current snapshot；更新口径后重新构建本页。</div>
        <input id="search" placeholder="搜索口径、字段、条件、rule_id">
        <select id="statusFilter">
          <option value="needs_review" selected>重点：变化/待确认/未登记</option>
          <option value="">全部口径</option>
          <option value="type_business_rule">业务口径</option>
          <option value="type_project_parameter">项目参数</option>
          <option value="type_source_baseline">来源/XML 基线</option>
          <option value="type_governance_policy">治理规则</option>
          <option value="evolution_changed">只看项目间变化</option>
          <option value="consistent">只看三阶段一致</option>
          <option value="confirmed">只看 confirmed</option>
          <option value="config_owned">只看项目配置接管</option>
          <option value="proposed">只看 proposed</option>
          <option value="missing">包含项目未登记</option>
          <option value="deprecated">包含 deprecated</option>
          <option value="unregistered">未登记 concept_key</option>
        </select>
        <div class="chips" id="projectFilters"></div>
      </div>
      <div class="list" id="conceptList"></div>
    </aside>
    <main>
      <section class="summary" id="summary"></section>
      <section id="detail"></section>
    </main>
  </div>
  <script>
    let payload = {data};
    payload.source_note = payload.source_note || 'RuleStore current snapshot';
    let selected = 0;
    const projectFilter = new Set();
    const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#039;'}}[ch]));
    const chip = (value) => {{
      const v = String(value || 'missing');
      let cls = '';
      if (v === 'confirmed' || v === 'config_owned' || v === 'pass' || v === 'registered' || v === 'consistent' || v === 'cataloged') cls = 'good';
      else if (v === 'proposed' || v === 'warn' || v === 'WARN' || v === 'changed' || v === 'partial_changed' || v === 'not_cataloged') cls = 'warn';
      else if (v === 'deprecated' || v === 'fail' || v === 'ERROR' || v === 'unregistered' || v === 'missing' || v === 'removed') cls = 'bad';
      else if (v === 'superseded') cls = 'accent';
      return `<span class="chip ${{cls}}">${{esc(v)}}</span>`;
    }};
    function ensureStructuredRule(rule) {{
      return rule?.structured_view || {{}};
    }}
    function projectName(slug) {{
      return payload.projects.find(p => p.slug === slug)?.name || slug;
    }}
    function rowText(row) {{
      return `${{row.concept_key || ''}} ${{row.label || ''}} ${{row.description || ''}} ${{(row.terms || []).join(' ')}}`.toLowerCase();
    }}
    function rowCategory(row) {{
      const text = rowText(row);
      if (row.concept_type === 'project_parameter' || row.concept_type === 'source_baseline') return {{rank: 0, id: 'project_material', label: '项目资料'}};
      if (text.match(/new-user|retention|player-scope|新增|留存|活跃|玩家范围/)) return {{rank: 1, id: 'player_retention', label: '玩家与留存'}};
      if (text.match(/pcu|dau|onlinecnt|onlinetime|在线|并发/)) return {{rank: 3, id: 'online_concurrency', label: '在线与并发'}};
      if (text.match(/rank|段位|积分区间/)) return {{rank: 5, id: 'rank', label: '段位'}};
      if (text.match(/matchend|matchsucess|matchsuccess|匹配/)) return {{rank: 6, id: 'matching', label: '匹配'}};
      if (text.match(/territory|raid|kill|death|damage|击杀|抄家|遇袭|死亡|领地/)) return {{rank: 4, id: 'battle_behavior', label: '战斗行为'}};
      if (text.match(/gamemode|game-mode|模式|battlesrv|server|服务器|izoneareaid|dteventtime|dteventdate|event-time|开服/)) return {{rank: 2, id: 'mode_server_time', label: '模式 / 服务器 / 时间'}};
      if (row.concept_type === 'governance_policy') return {{rank: 7, id: 'governance', label: '治理规则'}};
      return {{rank: 8, id: 'other_business', label: '其他业务口径'}};
    }}
    function attentionRank(row) {{
      const cells = Object.values(row.project_cells || {{}});
      if (row.registry_status === 'unregistered') return 0;
      if (cells.some(cell => cell.status === 'proposed')) return 1;
      if (cells.some(cell => cell.status === 'deprecated')) return 2;
      if ((row.expected_missing || []).length) return 3;
      if (['changed', 'partial_changed', 'partial', 'single_project'].includes(row.evolution_status)) return 4;
      return 9;
    }}
    function compareRowsForSidebar(a, b) {{
      const ac = rowCategory(a);
      const bc = rowCategory(b);
      return ac.rank - bc.rank ||
        attentionRank(a) - attentionRank(b) ||
        Number(b.confirmed_project_count || 0) - Number(a.confirmed_project_count || 0) ||
        Number(b.present_count || 0) - Number(a.present_count || 0) ||
        String(a.label || '').localeCompare(String(b.label || ''), 'zh-Hans-CN') ||
        String(a.concept_key || '').localeCompare(String(b.concept_key || ''));
    }}
    function filterRows() {{
      const q = document.getElementById('search').value.trim().toLowerCase();
      const status = document.getElementById('statusFilter').value;
      return payload.concepts.filter(row => {{
        const text = [
          row.concept_key, row.label, row.description, row.notes, (row.terms || []).join(' '),
          ...Object.values(row.project_cells).flatMap(cell => cell.current ? [cell.current.rule_id, cell.current.title, cell.current.content, cell.current.applies_to, cell.current.source_evidence, cell.current.decision_question, cell.current.notes] : [])
        ].join(' ').toLowerCase();
        const projectOk = !projectFilter.size || [...projectFilter].some(slug => row.project_cells[slug]?.present);
        let statusOk = true;
        if (status === 'confirmed') statusOk = Object.values(row.project_cells).some(cell => cell.status === 'confirmed');
        if (status === 'config_owned') statusOk = Object.values(row.project_cells).some(cell => cell.status === 'config_owned');
        if (status === 'proposed') statusOk = Object.values(row.project_cells).some(cell => cell.status === 'proposed');
        if (status === 'missing') statusOk = Object.values(row.project_cells).some(cell => cell.status === 'missing');
        if (status === 'deprecated') statusOk = Object.values(row.project_cells).some(cell => cell.status === 'deprecated');
        if (status === 'unregistered') statusOk = row.registry_status === 'unregistered';
        if (status === 'type_business_rule') statusOk = row.concept_type === 'business_rule';
        if (status === 'type_project_parameter') statusOk = row.concept_type === 'project_parameter';
        if (status === 'type_source_baseline') statusOk = row.concept_type === 'source_baseline';
        if (status === 'type_governance_policy') statusOk = row.concept_type === 'governance_policy';
        if (status === 'evolution_changed') statusOk = ['changed', 'partial_changed'].includes(row.evolution_status);
        if (status === 'consistent') statusOk = row.evolution_status === 'consistent';
        if (status === 'needs_review') statusOk =
          row.registry_status === 'unregistered' ||
          ['changed', 'partial_changed', 'partial', 'single_project'].includes(row.evolution_status) ||
          Object.values(row.project_cells).some(cell => ['proposed', 'deprecated'].includes(cell.status));
        return projectOk && statusOk && (!q || text.includes(q));
      }}).sort(compareRowsForSidebar);
    }}
    function renderHeader() {{
      document.getElementById('generated').textContent = `数据时间：${{payload.generated_at}} · 来源：${{payload.source_note || payload.projects_root}}`;
      document.getElementById('headerChips').innerHTML = [
        chip(payload.status),
        `<span class="chip">${{payload.summary.projects}} 项目</span>`,
        `<span class="chip">${{payload.summary.concepts_in_dictionary}} 口径</span>`,
        `<span class="chip">${{payload.summary.saved_rules}} 当前/提案记录</span>`,
        `<span class="chip good">${{payload.summary.confirmed_rules}} confirmed</span>`,
        `<span class="chip warn">${{payload.summary.proposed_rules}} proposed</span>`,
        `<span class="chip">${{payload.summary.config_owned_concepts || 0}} 配置接管</span>`
      ].join('');
      document.getElementById('projectFilters').innerHTML = payload.projects.map(project =>
        `<button class="${{projectFilter.has(project.slug) ? 'active' : ''}}" onclick="toggleProject('${{esc(project.slug)}}')">${{esc(project.name)}}</button>`
      ).join('');
    }}
    function toggleProject(slug) {{
      if (projectFilter.has(slug)) projectFilter.delete(slug); else projectFilter.add(slug);
      selected = 0;
      render();
    }}
    function renderList() {{
      const rows = filterRows();
      if (selected >= rows.length) selected = 0;
      const html = [];
      let lastCategory = '';
      rows.forEach((row, i) => {{
        const category = rowCategory(row);
        if (category.id !== lastCategory) {{
          const count = rows.filter(item => rowCategory(item).id === category.id).length;
          html.push(`<div class="group-title"><span>${{esc(category.label)}}</span><span>${{count}}</span></div>`);
          lastCategory = category.id;
        }}
        const projectChips = payload.projects.map(project => chip(row.project_cells[project.slug]?.status)).join('');
        html.push(`<div class="item ${{i === selected ? 'selected' : ''}}" onclick="selected=${{i}}; render()">
          <div class="item-title">${{esc(row.label)}}</div>
          <div class="item-sub">${{esc(row.concept_key)}} · 覆盖 ${{row.present_count}}/${{payload.projects.length}} · confirmed ${{row.confirmed_project_count}} · ${{esc(row.evolution_label || '')}}</div>
          <div class="chips">${{chip(conceptTypeLabel(row.concept_type))}}${{chip(row.registry_status)}}${{chip(row.evolution_status)}}${{projectChips}}</div>
        </div>`);
      }});
      document.getElementById('conceptList').innerHTML = html.join('') || '<div class="item"><div class="item-sub">没有匹配结果</div></div>';
    }}
    function currentRow() {{
      return filterRows()[selected] || payload.concepts[0];
    }}
    function renderSummary() {{
      const row = currentRow();
      document.getElementById('summary').innerHTML = `
        <div class="card"><span class="muted">当前口径</span><strong>${{esc(row?.label || '')}}</strong></div>
        <div class="card"><span class="muted">concept_key</span><strong>${{esc(row?.concept_key || '')}}</strong></div>
        <div class="card"><span class="muted">项目覆盖</span><strong>${{row?.present_count || 0}} / ${{payload.projects.length}}</strong></div>
        <div class="card"><span class="muted">演进状态</span><strong>${{esc(row?.evolution_label || '')}}</strong></div>`;
    }}
    function renderDataTable(table) {{
      const columns = table.columns || [];
      const rows = table.rows || [];
      if (!columns.length || !rows.length) return '';
      return `<div class="structured-block">
        <div class="section-title">${{esc(table.title || '结构化表')}} <span class="muted">(${{rows.length}} 行)</span></div>
        <div class="data-table-wrap"><table class="data-table">
          <thead><tr>${{columns.map(col => `<th>${{esc(col)}}</th>`).join('')}}</tr></thead>
          <tbody>${{rows.map(row => `<tr>${{columns.map(col => `<td>${{esc(row[col] ?? '')}}</td>`).join('')}}</tr>`).join('')}}</tbody>
        </table></div>
      </div>`;
    }}
    function renderConditionSets(sets) {{
      if (!sets || !sets.length) return '';
      const columns = ['分类/标记', '字段', '条件', '业务含义'];
      return `<div class="structured-block">
        <div class="section-title">分类 / 标记规则</div>
        <div class="data-table-wrap"><table class="data-table">
          <thead><tr>${{columns.map(col => `<th>${{esc(col)}}</th>`).join('')}}</tr></thead>
          <tbody>${{sets.map(item => `<tr>
            <td>${{esc(item.label || '')}}${{item.flag ? `<br><span class="muted">${{esc(item.flag)}}</span>` : ''}}</td>
            <td>${{esc(item.field || '')}}</td>
            <td>${{esc(`${{item.operator || ''}} ${{(item.values || []).join(', ')}}`)}}</td>
            <td>${{esc(item.business_meaning || '')}}</td>
          </tr>`).join('')}}</tbody>
        </table></div>
      </div>`;
    }}
    function renderVariables(vars) {{
      if (!vars || !vars.length) return '';
      return `<div class="structured-block">
        <div class="section-title">字段 / 变量边界</div>
        <div>${{vars.map(item => `<div class="kv"><span>${{esc(item.name)}}</span><span>${{esc(item.meaning)}}</span></div>`).join('')}}</div>
      </div>`;
    }}
    function renderListBlock(title, items) {{
      if (!items || !items.length) return '';
      return `<div class="structured-block"><div class="section-title">${{esc(title)}}</div><ul>${{items.map(item => `<li>${{esc(item)}}</li>`).join('')}}</ul></div>`;
    }}
    function renderSections(sections) {{
      if (!sections || !sections.length) return '';
      return sections.map(section => renderListBlock(section.heading || '说明', section.items || [])).join('');
    }}
    function currentRuleFor(row, slug) {{
      return row?.project_cells?.[slug]?.current || null;
    }}
    function firstMappingTable(rule) {{
      const view = ensureStructuredRule(rule) || {{}};
      return [...(view.knowledge_tables || []), ...(view.mapping_tables || [])].find(table => (table.rows || []).length) || null;
    }}
    function mappingRowId(row) {{
      return String(row.mode_id ?? row.GameMode ?? row.gamemode ?? row.id ?? '').trim();
    }}
    function mappingDisplay(row) {{
      if (!row) return '';
      const skip = new Set(['mode_id', 'GameMode', 'gamemode', 'id']);
      return Object.entries(row)
        .filter(([key, value]) => !skip.has(key) && String(value ?? '').trim())
        .map(([key, value]) => `${{key}}：${{value}}`)
        .join('\\n');
    }}
    function mappingFields(row) {{
      if (!row) return {{}};
      const skip = new Set(['mode_id', 'GameMode', 'gamemode', 'id']);
      const out = {{}};
      for (const [key, value] of Object.entries(row)) {{
        const text = normalizedDisplay(value);
        if (!skip.has(key) && text) out[key] = text;
      }}
      return out;
    }}
    function hasMaterialMappingDiff(records) {{
      const fieldNames = [...new Set(records.flatMap(record => Object.keys(mappingFields(record))))];
      return fieldNames.some(field => {{
        const values = records.map(record => mappingFields(record)[field]).filter(Boolean);
        return values.length >= 2 && new Set(values).size > 1;
      }});
    }}
    function normalizedDisplay(value) {{
      return String(value || '').replace(/\\s+/g, ' ').trim();
    }}
    function modeIdSort(a, b) {{
      const an = Number(a), bn = Number(b);
      if (Number.isFinite(an) && Number.isFinite(bn)) return an - bn;
      return String(a).localeCompare(String(b), 'zh-Hans-CN');
    }}
    function renderMappingComparison(row, title = '映射关系变化') {{
      const projectMaps = payload.projects.map(project => {{
        const rule = currentRuleFor(row, project.slug);
        const table = firstMappingTable(rule);
        const map = new Map();
        for (const record of table?.rows || []) {{
          const id = mappingRowId(record);
          if (id) map.set(id, record);
        }}
        return {{project, rule, table, map}};
      }});
      if (!projectMaps.some(item => item.map.size)) return '';
      const ids = [...new Set(projectMaps.flatMap(item => [...item.map.keys()]))].sort(modeIdSort);
      const changed = ids.filter(id => hasMaterialMappingDiff(projectMaps.map(item => item.map.get(id))));
      const partialOnly = ids.filter(id => {{
        const records = projectMaps.map(item => item.map.get(id));
        const presentCount = records.filter(Boolean).length;
        return presentCount > 0 && presentCount < projectMaps.length && !hasMaterialMappingDiff(records);
      }});
      const columns = ['mode_id', ...payload.projects.map(project => project.name)];
      return `<div class="structured-block">
        <div class="section-title">${{esc(title)}} <span class="muted">(${{changed.length}} 个关键差异 / ${{ids.length}} 个 ID；${{partialOnly.length}} 个仅部分项目配置)</span></div>
        ${{changed.length ? `<div class="change-list">${{changed.slice(0, 12).map(id => {{
          const pieces = projectMaps.map(item => `${{item.project.name}}：${{normalizedDisplay(mappingDisplay(item.map.get(id))) || '未配置'}}`);
          return `<div class="change-item"><strong>GameMode=${{esc(id)}}</strong><br>${{esc(pieces.join('；'))}}</div>`;
        }}).join('')}}${{changed.length > 12 ? `<div class="muted">还有 ${{changed.length - 12}} 个差异 ID，可在下方完整表格查看。</div>` : ''}}</div>` : '<div class="note">这些项目的结构化映射没有发现差异。</div>'}}
        <div class="data-table-wrap"><table class="data-table compare-table">
          <thead><tr>${{columns.map(col => `<th>${{esc(col)}}</th>`).join('')}}</tr></thead>
          <tbody>${{ids.map(id => {{
            const values = projectMaps.map(item => normalizedDisplay(mappingDisplay(item.map.get(id))) || '未配置');
            const isDiff = hasMaterialMappingDiff(projectMaps.map(item => item.map.get(id)));
            return `<tr><td><strong>${{esc(id)}}</strong></td>${{projectMaps.map((item, idx) => {{
              const value = values[idx];
              const cls = value === '未配置' ? 'missing' : (isDiff ? 'diff' : '');
              return `<td><div class="compare-cell ${{cls}}">${{esc(value).replace(/\\n/g, '<br>')}}</div></td>`;
            }}).join('')}}</tr>`;
          }}).join('')}}</tbody>
        </table></div>
      </div>`;
    }}
    function conditionKey(item) {{
      return item.flag || item.label || '';
    }}
    function conditionDisplay(item) {{
      if (!item) return '';
      const values = (item.values || []).join(', ');
      const meaning = item.business_meaning ? `；${{item.business_meaning}}` : '';
      return `${{item.operator || ''}} ${{values}}${{meaning}}`.trim();
    }}
    function conditionSignature(item) {{
      if (!item) return '';
      const values = [...(item.values || [])].map(value => String(value)).sort(modeIdSort).join(',');
      return [item.field || '', item.operator || '', values].map(normalizedDisplay).join('|');
    }}
    function renderConditionComparison(row, title = '分类集合变化') {{
      const projectSets = payload.projects.map(project => {{
        const rule = currentRuleFor(row, project.slug);
        const view = ensureStructuredRule(rule) || {{}};
        const map = new Map();
        for (const item of view.condition_sets || []) {{
          const key = conditionKey(item);
          if (key) map.set(key, item);
        }}
        return {{project, rule, map}};
      }});
      if (!projectSets.some(item => item.map.size)) return '';
      const keys = [...new Set(projectSets.flatMap(item => [...item.map.keys()]))];
      const changed = keys.filter(key => {{
        const signatures = projectSets.map(item => conditionSignature(item.map.get(key))).filter(Boolean);
        return signatures.length >= 2 && new Set(signatures).size > 1;
      }});
      return `<div class="structured-block">
        <div class="section-title">${{esc(title)}} <span class="muted">(${{changed.length}} 个差异 / ${{keys.length}} 个分类)</span></div>
        ${{changed.length ? `<div class="change-list">${{changed.map(key => {{
          const label = projectSets.find(item => item.map.get(key))?.map.get(key)?.label || key;
          const pieces = projectSets.map(item => `${{item.project.name}}：${{normalizedDisplay(conditionDisplay(item.map.get(key))) || '未配置'}}`);
          return `<div class="change-item"><strong>${{esc(label)}}${{key !== label ? ` / ${{esc(key)}}` : ''}}</strong><br>${{esc(pieces.join('；'))}}</div>`;
        }}).join('')}}</div>` : '<div class="note">这些项目的结构化分类集合没有发现差异。</div>'}}
        <div class="data-table-wrap"><table class="data-table compare-table">
          <thead><tr><th>分类</th>${{payload.projects.map(project => `<th>${{esc(project.name)}}</th>`).join('')}}</tr></thead>
          <tbody>${{keys.map(key => {{
            const first = projectSets.find(item => item.map.get(key))?.map.get(key);
            const values = projectSets.map(item => normalizedDisplay(conditionDisplay(item.map.get(key))) || '未配置');
            const signatures = projectSets.map(item => conditionSignature(item.map.get(key))).filter(Boolean);
            const isDiff = signatures.length >= 2 && new Set(signatures).size > 1;
            return `<tr><td><strong>${{esc(first?.label || key)}}</strong><br><span class="muted">${{esc(key)}}</span></td>${{values.map(value => `<td><div class="compare-cell ${{value === '未配置' ? 'missing' : (isDiff ? 'diff' : '')}}">${{esc(value)}}</div></td>`).join('')}}</tr>`;
          }}).join('')}}</tbody>
        </table></div>
      </div>`;
    }}
    function normalizedText(value) {{
      return String(value || '').replace(/DEMO-EXPERIMENT|DEMO-AB_TEST|DEMO-ANALYTICS|EXPERIMENT|AB_TEST|BASE/g, '<PROJECT>').replace(/cbt3-|abtest-|obt-/g, '<project>-').replace(/\\s+/g, ' ').trim();
    }}
    function renderSharedRuleCore(row) {{
      const rows = [];
      const common = new Map();
      for (const project of payload.projects) {{
        const rule = currentRuleFor(row, project.slug);
        const view = ensureStructuredRule(rule) || {{}};
        for (const item of [...(view.constraints || []), ...(view.unknown_handling || [])]) {{
          const key = normalizedText(item);
          if (!key) continue;
          if (!common.has(key)) common.set(key, {{text: item, projects: []}});
          common.get(key).projects.push(project.name);
        }}
      }}
      for (const item of common.values()) {{
        if (item.projects.length >= 2) rows.push(`${{item.text}}（${{item.projects.join('、')}}）`);
      }}
      if (!rows.length) return '';
      return renderListBlock('跨项目共用边界（去重后）', rows.slice(0, 8));
    }}
    function findConcept(key) {{
      return (payload.concepts || []).find(item => item.concept_key === key);
    }}
    function renderRelatedGameModeContext(row) {{
      if (row.concept_key !== 'game-mode-name-type-boundary') return '';
      const mappingRow = findConcept('game-mode-map');
      const flagsRow = findConcept('battle-gamemode-experience-flags');
      const parts = [];
      if (mappingRow) parts.push(renderMappingComparison(mappingRow, '关联：GameMode 模式名称 / 大类映射变化'));
      if (flagsRow) parts.push(renderConditionComparison(flagsRow, '关联：首日体验分类 GameMode 集合变化'));
      if (!parts.length) return '';
      return `<section class="panel">
        <div class="panel-head"><h2>模式映射关系</h2><div class="chips"><span class="chip">关联口径</span><span class="chip">game-mode-map</span><span class="chip">battle-gamemode-experience-flags</span></div></div>
        <div class="panel-body">
          <div class="note">当前口径是“模式名称”和“模式类型/大类”的边界。真正要看的变化来自它依赖的两个口径：GameMode 名称/大类映射，以及 has_normal/has_fast/has_newbie 体验分类集合。</div>
          ${{parts.join('')}}
        </div>
      </section>`;
    }}
    function renderCrossProjectView(row) {{
      const pieces = [
        renderMappingComparison(row, '本口径映射变化'),
        renderConditionComparison(row, '本口径分类集合变化'),
        renderSharedRuleCore(row)
      ].filter(Boolean);
      if (!pieces.length) return '';
      return `<section class="panel">
        <div class="panel-head"><h2>关键变化</h2><div class="chips"><span class="chip">去重展示</span><span class="chip">结构化对比</span></div></div>
        <div class="panel-body">${{pieces.join('')}}</div>
      </section>`;
    }}
    function renderProjectCards(row) {{
      return `<section class="panel">
        <div class="panel-head"><h2>项目原文 / 证据</h2><div class="chips">${{chip(`${{row.confirmed_project_count}} confirmed`)}}${{chip(`${{row.proposed_project_count}} proposed`)}}</div></div>
        <div class="panel-body">
          <details class="project-detail"><summary>展开查看各项目当前定义、待审提案和证据</summary>
            <div class="rule-grid" style="margin-top:12px;">
              ${{payload.projects.map(project => {{
                const cell = row.project_cells[project.slug] || {{}};
                const rule = cell.current;
                const proposals = cell.proposals || [];
                return `<div class="project-card ${{cell.present ? '' : 'missing'}}">
                  <div class="panel-head" style="padding:0 0 8px;border-bottom:1px solid #edf0f5;">
                    <h3>${{esc(project.name)}}</h3><div class="chips">${{chip(cell.status)}}${{proposals.length ? chip(`${{proposals.length}} proposed`) : ''}}</div>
                  </div>
                  <div class="kv"><span>环境</span><span>${{esc(project.config.sql_dialect)}} / ${{esc(project.config.query_engine)}}</span></div>
                  <div class="kv"><span>表名规则</span><span>${{esc(project.config.table_naming_profile)}}</span></div>
                  <div class="section-title">当前定义</div>
                  ${{renderRule(rule)}}
                  ${{proposals.length ? `<div class="section-title" style="margin-top:14px;">待审提案</div>${{proposals.map(renderRule).join('')}}` : ''}}
                  <div class="muted" style="margin-top:12px;">历史正文未嵌入本页：共 ${{cell.semantic_version_count || 0}} 个业务版本、${{cell.version_count || 0}} 条存储记录，其中 ${{cell.history_count || 0}} 条历史记录。需要时按 concept_key 查询。</div>
                </div>`;
              }}).join('')}}
            </div>
          </details>
        </div>
      </section>`;
    }}
    function renderRule(rule) {{
      if (!rule) return '<div class="muted">该项目未登记此 concept_key；这不等于业务上不存在，只说明当前项目规则库没有保存这条项目规则。</div>';
      if (rule.record_kind === 'project_config') {{
        const value = typeof rule.config_value === 'string'
          ? rule.config_value
          : JSON.stringify(rule.config_value, null, 2);
        return `
          <div class="note">该项已经从业务口径迁出。SQL 生成和校验读取项目配置，旧规则仅保留为历史证据，不再参与口径激活。</div>
          <div class="kv"><span>状态</span><span>${{chip('config_owned')}}<span class="chip">项目配置</span></span></div>
          <div class="kv"><span>配置位置</span><span>${{esc(rule.config_path || 'project_config.json')}}#${{esc(rule.config_pointer || '')}}</span></div>
          <div class="structured-block"><div class="section-title">当前配置值</div><div class="content">${{esc(value ?? '未提取到结构化值')}}</div></div>
          ${{rule.source_evidence ? `<details class="raw-rule"><summary>迁移证据</summary><div class="content">${{esc(rule.source_evidence)}}</div></details>` : ''}}
          ${{rule.notes ? `<div class="kv"><span>备注</span><span>${{esc(rule.notes)}}</span></div>` : ''}}`;
      }}
      const affected = Array.isArray(rule.affected_artifacts) ? rule.affected_artifacts.join(', ') : '';
      const view = ensureStructuredRule(rule) || {{}};
      const mappingTables = (view.mapping_tables || []).map(renderDataTable).join('');
      const knowledgeTables = (view.knowledge_tables || []).map(table => `
        <div class="structured-block">
          <div class="section-title">当前资料：${{esc(table.title || '知识映射')}}</div>
          ${{table.binding_note ? `<div class="note">${{esc(table.binding_note)}}</div>` : ''}}
          ${{table.status === 'ready' ? renderDataTable({{...table, title: table.title || '知识映射'}}) : `<div class="issue"><strong>知识依赖不可读</strong><span>${{esc(table.message || '缺少知识投影')}}</span></div>`}}
          <details class="raw-rule"><summary>资料技术血缘</summary><div class="content">当前绑定：${{esc(table.dataset_id || '')}} / ${{esc(table.dataset_version || '')}} / ${{esc(table.projection_id || '')}}\n投影哈希：${{esc(table.projection_sha256 || '')}}${{table.legacy_pin && table.legacy_pin.dataset_version ? `\n历史记录 pin：${{esc(table.legacy_pin.dataset_version)}}` : ''}}</div></details>
        </div>`).join('');
      const semanticVersion = rule.semantic_version || rule.version || '';
      const recordVersion = rule.record_version || rule.version || '';
      const technicalRevision = Number(recordVersion) !== Number(semanticVersion) || Number(rule.technical_revision_count || 1) > 1;
      return `
        <div class="kv"><span>口径</span><span>${{esc(rule.rule_id)}} v${{esc(semanticVersion)}}</span></div>
        <div class="kv"><span>状态</span><span>${{chip(rule.status)}}${{chip(view.rule_kind_label || '业务规则')}} ${{rule.confirmed_by_user ? '<span class="chip good">用户确认</span>' : '<span class="chip warn">需谨慎</span>'}}</span></div>
        <div class="kv"><span>标题</span><span>${{esc(rule.title)}}</span></div>
        <div class="rule-headline">${{esc(view.headline || rule.title || '')}}</div>
        ${{view.knowledge_status === 'ready' ? '<div class="note">业务口径与可变资料已分离；下表读取项目当前审核通过的资料绑定。</div>' : ''}}
        ${{knowledgeTables}}
        ${{mappingTables}}
        ${{renderConditionSets(view.condition_sets || [])}}
        ${{renderVariables(view.variables || [])}}
        ${{renderListBlock('约束 / 禁止 / 边界', view.constraints || [])}}
        ${{renderListBlock('未知或未配置值处理', view.unknown_handling || [])}}
        ${{renderSections(view.sections || [])}}
        <div class="kv"><span>适用</span><span>${{esc(rule.applies_to || '')}}</span></div>
        <details class="raw-rule"><summary>技术修订与来源${{technicalRevision ? `（存储修订 v${{esc(recordVersion)}}）` : ''}}</summary><div class="content">业务版本：v${{esc(semanticVersion)}}\n存储记录版本：v${{esc(recordVersion)}}\n等价技术记录：${{esc(rule.technical_revision_count || 1)}}\n来源：${{esc(rule.source || '')}}\n时间：${{esc(rule.created_at || '')}} / ${{esc(rule.updated_at || '')}}</div></details>
        <details class="raw-rule"><summary>原文证据 / content</summary><div class="content">${{esc(rule.content || '')}}</div></details>
        ${{rule.source_evidence ? `<details class="raw-rule"><summary>来源证据</summary><div class="content">${{esc(rule.source_evidence)}}</div></details>` : ''}}
        ${{rule.decision_question ? `<div class="kv"><span>待确认</span><span>${{esc(rule.decision_question)}}</span></div>` : ''}}
        ${{affected ? `<div class="kv"><span>影响资产</span><span>${{esc(affected)}}</span></div>` : ''}}
        ${{rule.notes ? `<div class="kv"><span>备注</span><span>${{esc(rule.notes)}}</span></div>` : ''}}`;
    }}
    function renderIssues(row) {{
      const items = (payload.issues || []).filter(item => !row || !item.concept_key || item.concept_key === row.concept_key);
      if (!items.length) return '';
      return `<section class="panel">
        <div class="panel-head"><h2>校验提示</h2><div class="chips">${{items.map(item => chip(item.severity)).join('')}}</div></div>
        <div class="panel-body">${{items.map(item => `<div class="issue"><strong>${{esc(item.code)}}</strong><span>${{esc(item.message)}}</span></div>`).join('')}}</div>
      </section>`;
    }}
    function changeTypeLabel(value) {{
      return {{
        unchanged: '未变化',
        changed: '有变化',
        added: '新增登记',
        removed: '后续未登记',
        both_missing: '两侧均未登记'
      }}[value] || value;
    }}
    function conceptTypeLabel(value) {{
      return {{
        business_rule: '业务口径',
        source_baseline: '来源/XML',
        project_parameter: '项目参数',
        execution_profile: '执行环境',
        governance_policy: '治理规则',
        review_only: 'Review 提示'
      }}[value] || value || '';
    }}
    function coverageLabel(value) {{
      return {{
        all_expected_projects: '三阶段需显式登记',
        explicit_per_stage: '逐阶段显式登记',
        stage_specific: '阶段特有',
        source_catalog: '看 source catalog',
        global_governance: '全局治理规则',
        unregistered: '未登记概念'
      }}[value] || value || '';
    }}
    function renderEvolution(row) {{
      const nodes = row.evolution_nodes || [];
      const transitions = row.evolution_transitions || [];
      return `<section class="panel">
        <div class="panel-head"><h2>项目时间线</h2><div class="chips">${{chip(row.evolution_status)}}<span class="chip">${{esc(row.evolution_label || '')}}</span></div></div>
        <div class="panel-body">
          <div class="note">这里比较的是同一 concept_key 在 EXPERIMENT → AB_TEST → BASE 的登记版本。未登记不等于业务不存在；它表示该项目规则库还没有保存这一条项目规则。</div>
          <div class="timeline">
            ${{nodes.map(node => `<div class="timeline-node ${{node.present ? '' : 'missing'}}">
              <strong>${{esc(projectName(node.project))}}</strong>
              <div class="chips">${{chip(node.status)}}${{node.present ? `<span class="chip">v${{esc(node.version)}}</span>` : '<span class="chip">未登记</span>'}}</div>
              <div class="muted">${{esc(node.title || '该项目规则库未保存此口径')}}</div>
            </div>`).join('')}}
          </div>
          <div>
            ${{transitions.map(item => `<div class="transition">
              <div class="chips"><span class="chip">${{esc(projectName(item.from_project))}} → ${{esc(projectName(item.to_project))}}</span>${{chip(item.change_type)}}<span class="chip">${{esc(changeTypeLabel(item.change_type))}}</span></div>
              ${{item.field_changes?.length ? `<div>${{item.field_changes.map(change => `<div class="kv"><span>${{esc(change.label)}}</span><span><strong>前：</strong>${{esc(change.before || '未登记')}}<br><strong>后：</strong>${{esc(change.after || '未登记')}}</span></div>`).join('')}}</div>` : '<div class="muted">可比较字段未发现变化。</div>'}}
              <div class="diff-lines">
                ${{(item.content_diff?.removed || []).map(line => `<div class="diff-line remove">- ${{esc(line)}}</div>`).join('')}}
                ${{(item.content_diff?.added || []).map(line => `<div class="diff-line add">+ ${{esc(line)}}</div>`).join('')}}
              </div>
            </div>`).join('')}}
          </div>
        </div>
      </section>`;
    }}
    function renderSourceCatalogs() {{
      return `<section class="panel">
        <div class="panel-head"><h2>XML / Source 基线</h2><div class="chips"><span class="chip">来源目录</span></div></div>
        <div class="panel-body">
          <div class="note">这块展示每个项目实际采集到的 XML catalog。它是字段/日志结构来源，不等同于 canonical rule 是否登记。</div>
          <div class="source-grid">
            ${{payload.projects.map(project => {{
              const src = project.source_catalog || {{}};
              const status = src.status || (src.present ? 'cataloged' : 'missing');
              return `<div class="project-card ${{src.present ? '' : 'missing'}}">
                <div class="panel-head" style="padding:0 0 8px;border-bottom:1px solid #edf0f5;">
                  <h3>${{esc(project.name)}}</h3><div class="chips">${{chip(status)}}</div>
                </div>
                <div class="kv"><span>catalog</span><span>${{esc(src.catalog_path || '未采集 xml_catalog.json')}}</span></div>
                <div class="kv"><span>XML</span><span>${{esc(src.source_file || '未登记 XML 文件')}}</span></div>
                <div class="kv"><span>日志/字段</span><span>${{Number(src.log_count || 0)}} logs / ${{Number(src.field_count || 0)}} fields</span></div>
                <div class="kv"><span>生成时间</span><span>${{esc(src.generated_at || '')}}</span></div>
                <div class="chips">${{(src.sample_logs || []).map(name => `<span class="chip">${{esc(name)}}</span>`).join('')}}</div>
              </div>`;
            }}).join('')}}
          </div>
        </div>
      </section>`;
    }}
    function renderExecutionProfiles() {{
      return `<section class="panel">
        <div class="panel-head"><h2>执行环境</h2><div class="chips"><span class="chip">project_config.json</span></div></div>
        <div class="panel-body">
          <div class="note">执行环境不是业务指标口径，但会决定 SQL 语法、物理表名、分区裁剪、业务时间字段和 Review 判断标准。</div>
          <div class="source-grid">
            ${{payload.projects.map(project => {{
              const env = project.execution_profile || {{}};
              return `<div class="project-card">
                <div class="panel-head" style="padding:0 0 8px;border-bottom:1px solid #edf0f5;">
                  <h3>${{esc(project.name)}}</h3><div class="chips"><span class="chip">${{esc(env.label || '')}}</span></div>
                </div>
                <div class="kv"><span>代表什么</span><span>${{esc(env.meaning || '')}}</span></div>
                <div class="kv"><span>物理表</span><span>${{esc(env.table_policy || '')}}</span></div>
                <div class="kv"><span>时间/分区</span><span>${{esc(env.time_policy || '')}}</span></div>
                <div class="kv"><span>业务时间字段</span><span>${{esc(env.business_time_field || '')}}</span></div>
                <div class="kv"><span>分区字段</span><span>${{esc(env.partition_field || '无默认分区字段')}}</span></div>
                <div class="chips">${{(env.sql_constraints || []).map(item => `<span class="chip">${{esc(item)}}</span>`).join('')}}</div>
              </div>`;
            }}).join('')}}
          </div>
        </div>
      </section>`;
    }}
    function shouldShowSourceCatalogs(row) {{
      return row && (row.concept_type === 'source_baseline' || row.coverage_policy === 'source_catalog');
    }}
    function shouldShowExecutionProfiles(row) {{
      return row && row.concept_type === 'project_parameter';
    }}
    function renderDetail() {{
      const row = currentRow();
      if (!row) {{
        document.getElementById('detail').innerHTML = '<section class="panel"><div class="panel-body muted">没有口径</div></section>';
        return;
      }}
      const terms = (row.terms || []).map(term => `<span class="chip">${{esc(term)}}</span>`).join('');
      document.getElementById('detail').innerHTML = `
        ${{renderIssues(row)}}
        ${{shouldShowExecutionProfiles(row) ? renderExecutionProfiles() : ''}}
        ${{shouldShowSourceCatalogs(row) ? renderSourceCatalogs() : ''}}
        <section class="panel">
          <div class="panel-head"><h2>${{esc(row.label)}}</h2><div class="chips">${{chip(conceptTypeLabel(row.concept_type))}}${{chip(row.registry_status)}}${{chip(row.concept_status)}}</div></div>
          <div class="panel-body">
            <div class="content">${{esc(row.description || '')}}</div>
            <div class="chips">${{terms}}</div>
            <div class="kv"><span>资产类型</span><span>${{esc(conceptTypeLabel(row.concept_type))}}</span></div>
            <div class="kv"><span>覆盖策略</span><span>${{esc(coverageLabel(row.coverage_policy))}}</span></div>
            <div class="kv"><span>继承策略</span><span>${{esc(row.inheritance_policy || 'none')}}</span></div>
            ${{row.notes ? `<div class="kv"><span>概念备注</span><span>${{esc(row.notes)}}</span></div>` : ''}}
            <div class="kv"><span>预期项目</span><span>${{esc((row.expected_projects || []).join(', ') || '未配置')}}</span></div>
          </div>
        </section>
        ${{renderRelatedGameModeContext(row)}}
        ${{renderCrossProjectView(row)}}
        ${{renderEvolution(row)}}
        ${{renderProjectCards(row)}}`;
    }}
    function render() {{
      renderHeader();
      renderList();
      renderSummary();
      renderDetail();
    }}
    document.getElementById('search').addEventListener('input', () => {{ selected = 0; render(); }});
    document.getElementById('statusFilter').addEventListener('change', () => {{ selected = 0; render(); }});
    render();
  </script>
</body>
</html>
"""


def build_outputs(args) -> dict:
    projects_root = Path(args.projects_root).resolve()
    concept_registry = Path(args.concept_registry).resolve() if args.concept_registry else projects_root / DEFAULT_CONCEPTS_REL
    html_output = Path(args.output).resolve() if args.output else projects_root / DEFAULT_HTML_REL
    json_output = Path(args.json_output).resolve() if args.json_output else projects_root / DEFAULT_JSON_REL
    payload = build_payload(projects_root, concept_registry, args.project)
    html_output.parent.mkdir(parents=True, exist_ok=True)
    html_output.write_text(html_page(payload), encoding="utf-8")
    write_json(json_output, payload)
    return {"payload": payload, "html_output": html_output, "json_output": json_output}


def cmd_build(args) -> None:
    result = build_outputs(args)
    payload = result["payload"]
    print(f"rule_dictionary_html: {result['html_output']}")
    print(f"rule_dictionary_json: {result['json_output']}")
    print(f"status: {payload['status']}")
    print(f"projects: {payload['summary']['projects']}")
    print(f"concepts: {payload['summary']['concepts_in_dictionary']}")
    print(f"saved_rules: {payload['summary']['saved_rules']}")
    print(f"confirmed_rules: {payload['summary']['confirmed_rules']}")
    print(f"proposed_rules: {payload['summary']['proposed_rules']}")
    print(f"issues: {payload['summary']['issues']}")


def cmd_validate(args) -> None:
    projects_root = Path(args.projects_root).resolve()
    concept_registry = Path(args.concept_registry).resolve() if args.concept_registry else projects_root / DEFAULT_CONCEPTS_REL
    try:
        payload = build_payload(projects_root, concept_registry, args.project)
    except Exception as exc:  # noqa: BLE001
        result = {
            "project": "rule_dictionary",
            "status": "error",
            "summary": {"checks": 1, "passed": 0, "warnings": 0, "failures": 1},
            "errors": [{"id": "runtime.error", "status": "fail", "message": str(exc)}],
            "warnings": [],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(3)
    errors = [item for item in payload["issues"] if item.get("severity") == "ERROR"]
    warnings = [item for item in payload["issues"] if item.get("severity") == "WARN"]
    status = "fail" if errors else ("warn" if warnings else "pass")
    checks = [
        {
            "id": "rule_dictionary.catalog",
            "status": status,
            "message": f"{payload['summary']['concepts_in_dictionary']} concepts, {payload['summary']['saved_rules']} saved rule versions.",
            "path": str(projects_root),
        }
    ]
    result = {
        "project": "rule_dictionary",
        "status": status,
        "projects_root": str(projects_root),
        "summary": {"checks": len(checks), "passed": 0 if status != "pass" else 1, "warnings": len(warnings), "failures": len(errors)},
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "details": payload["summary"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if status == "fail":
        raise SystemExit(1)
    if status == "warn":
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build static rule dictionary HTML and JSON")
    build.add_argument("--projects-root", default="./sql-projects")
    build.add_argument("--concept-registry")
    build.add_argument("--output")
    build.add_argument("--json-output")
    build.add_argument("--project", action="append")
    build.set_defaults(func=cmd_build)

    validate = sub.add_parser("validate", help="Validate dictionary payload and emit machine-readable health JSON")
    validate.add_argument("--projects-root", default="./sql-projects")
    validate.add_argument("--concept-registry")
    validate.add_argument("--project", action="append")
    validate.add_argument("--format", choices=["json"], default="json")
    validate.set_defaults(func=cmd_validate)

    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
