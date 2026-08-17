#!/usr/bin/env python3
"""Build an interactive dashboard SQL review page from saved dashboard SQL sidecar specs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from asset_provenance import provenance_from_sources
from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_request,
    require_user_function_selection,
)
from capability_registry import command_function_ids
from formal_asset_repository import list_packages, load_package
from sql_output_contract import final_select_field_aliases
from spec_utils import load_sidecar_spec


STATE_VERSION = 1
REVIEW_CONTRACT_VERSION = "dashboard_review_v1"
VISUAL_REVIEW_CONTRACT_VERSION = "dashboard_visual_review_v1"
VISUAL_REVIEW_SPEC_VERSION = 4.8
DEFAULT_STATE_REL = "reviews/dashboard_review_state.json"
DEFAULT_HTML_REL = "reviews/dashboard_review.html"
DEFAULT_JSON_REL = "reviews/dashboard_review.json"
RESULT_EXTENSIONS = {".csv", ".xlsx"}
DASHBOARD_MEMBER_ROLES = {"dashboard_delivery", "dashboard_delivery_sql", "dashboard_sql"}
DASHBOARD_SPEC_ROLES = {"dashboard_delivery_spec", "dashboard_spec"}
RUN_EVIDENCE_ROLES = {"run_record", "run_evidence"}
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
RATIO_FIELD_RE = re.compile(r"(占比|比例|比率|转化率|留存率|率|percent|percentage|ratio|rate)", flags=re.I)
PERCENT_SOURCE_SCALES = {"ratio_0_to_1", "percent_0_to_100"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_rel(root: Path, path: Path | str) -> str:
    path_obj = Path(path)
    if path_obj.is_absolute():
        return path_obj.resolve().relative_to(root).as_posix()
    return path_obj.as_posix().replace("\\", "/")


def manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def is_current_artifact(item: dict) -> bool:
    return (item.get("artifact_state") or "current") == "current" and item.get("status") != "superseded"


def _member_document(root: Path, member: dict | None) -> dict:
    if not isinstance(member, dict):
        return {}
    path = root / str(member.get("path") or "")
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _companion_member(sql_member: dict, candidates: list[dict], suffix: str) -> dict | None:
    sql_path = Path(str(sql_member.get("path") or ""))
    expected = sql_path.with_name(f"{sql_path.stem}.{suffix}").as_posix()
    return next(
        (
            item
            for item in candidates
            if str(item.get("path") or "").replace("\\", "/") == expected
        ),
        None,
    )


def _package_dashboard_artifacts(root: Path, include_history: bool) -> tuple[list[dict], bool]:
    index_path = root / "formal_assets" / "index.json"
    if not index_path.is_file():
        return [], False
    artifacts: list[dict] = []
    for package_entry in list_packages(root):
        manifest = load_package(root, str(package_entry.get("package_id") or ""))
        members = [item for item in manifest.get("members", []) if isinstance(item, dict)]
        current_ids = {
            str(item)
            for item in (manifest.get("current") or {}).get("member_ids", [])
            if str(item)
        }
        specs = [item for item in members if str(item.get("role") or "").lower() in DASHBOARD_SPEC_ROLES]
        metas = [
            item
            for item in members
            if str(item.get("role") or "").lower() in {"dashboard_delivery_meta", "dashboard_meta"}
        ]
        for member in members:
            role = str(member.get("role") or "").lower()
            member_id = str(member.get("member_id") or "")
            if role not in DASHBOARD_MEMBER_ROLES or (not include_history and member_id not in current_ids):
                continue
            sql_path = root / str(member.get("path") or "")
            if not sql_path.is_file():
                continue
            spec_member = _companion_member(member, specs, "spec.json")
            meta_member = _companion_member(member, metas, "meta.json")
            spec = _member_document(root, spec_member)
            meta = _member_document(root, meta_member)
            version_match = re.search(r"v(\d+)\.sql$", sql_path.name, flags=re.I)
            intent = spec.get("dashboard_intent") if isinstance(spec.get("dashboard_intent"), dict) else {}
            validation = spec.get("validation_reference") if isinstance(spec.get("validation_reference"), dict) else {}
            artifacts.append(
                {
                    "kind": "DASHBOARD",
                    "slug": str((spec.get("spec_meta") or {}).get("artifact_slug") or sql_path.parent.name),
                    "version": int(version_match.group(1)) if version_match else 0,
                    "title": str(meta.get("title") or intent.get("title") or manifest.get("title") or sql_path.stem),
                    "status": str(member.get("lifecycle_state") or ""),
                    "artifact_state": "current" if member_id in current_ids else str(member.get("lifecycle_state") or "history"),
                    "path": str(member.get("path") or ""),
                    "spec_path": str((spec_member or {}).get("path") or ""),
                    "linked_run": str(validation.get("user_run_evidence") or ""),
                    "verification_status": str(validation.get("verification_status") or "not_applicable"),
                    "package_id": str(manifest.get("package_id") or ""),
                    "package_member_id": member_id,
                    "package_manifest": manifest,
                }
            )
    return sorted(artifacts, key=lambda x: (x.get("slug", ""), x.get("version", 0), x.get("path", ""))), True


def load_dashboard_artifacts(root: Path, include_history: bool) -> list[dict]:
    package_artifacts, package_repository_available = _package_dashboard_artifacts(root, include_history)
    if package_repository_available:
        return package_artifacts

    manifest = read_json(manifest_path(root), {})
    artifacts = []
    for item in manifest.get("artifacts", []):
        if item.get("kind") != "DASHBOARD":
            continue
        if not include_history and not is_current_artifact(item):
            continue
        sql_path = root / item.get("path", "")
        if sql_path.exists():
            artifacts.append(dict(item))
    if artifacts:
        return sorted(artifacts, key=lambda x: (x.get("slug", ""), x.get("version", 0), x.get("path", "")))

    dashboard_root = root / "dashboard_sql"
    if not dashboard_root.exists():
        return []
    for sql_path in sorted(dashboard_root.rglob("*.sql")):
        rel = sql_path.relative_to(root).as_posix()
        artifacts.append(
            {
                "kind": "DASHBOARD",
                "slug": sql_path.parent.name,
                "version": 0,
                "title": sql_path.stem,
                "status": "unknown",
                "artifact_state": "current",
                "path": rel,
                "linked_run": "",
                "verification_status": "not_applicable",
            }
        )
    return artifacts


def split_top_level_csv(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            items.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        items.append(tail)
    return items


def strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--.*?$", " ", sql, flags=re.M)
    return sql


def final_select_fields(sql: str) -> list[str]:
    return final_select_field_aliases(sql)


def has_chinese(value: object) -> bool:
    return bool(CJK_RE.search(str(value or "")))


def require_chinese_output_fields(owner: str, fields: list, errors: list[str]) -> None:
    non_string = [repr(field) for field in fields if not isinstance(field, str)]
    if non_string:
        errors.append(f"{owner} 必须是字符串字段名；非法字段={non_string}。")
        return
    missing = [field for field in fields if not has_chinese(field)]
    if missing:
        errors.append(
            f"{owner} 必须默认使用稳定中文字段名，英文/技术名只能保留在 CTE、YAML key 或 stable_key 中；"
            f"缺少中文的字段={missing}。"
        )


def require_module(spec: dict, module: str, errors: list[str]) -> dict:
    value = spec.get(module)
    if not isinstance(value, dict):
        errors.append(f"缺少 `{module}` 模块或模块不是 object。")
        return {}
    return value


def likely_ratio_field(field: object) -> bool:
    return bool(RATIO_FIELD_RE.search(str(field or "")))


def spec_version_number(spec: dict) -> float:
    version = (spec.get("spec_meta") or {}).get("spec_version")
    try:
        return float(version)
    except (TypeError, ValueError):
        return 0.0


def visual_review_enforced(spec: dict) -> bool:
    return spec_version_number(spec) >= VISUAL_REVIEW_SPEC_VERSION


def visual_review_needed_fields(table_fields: list, metrics: list) -> set[str]:
    needed = {str(field) for field in table_fields if likely_ratio_field(field)}
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        field = str(metric.get("field") or "")
        metric_type = str(metric.get("metric_type") or "").lower()
        if field and (likely_ratio_field(field) or metric_type in {"ratio", "rate", "percentage"}):
            needed.add(field)
    return needed


def metric_by_field(metrics: list) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for metric in metrics:
        if isinstance(metric, dict) and metric.get("field"):
            index[str(metric["field"])] = metric
    return index


def validate_visual_review_contract(
    spec: dict,
    table_fields: list,
    metrics: list,
    errors: list[str],
    warnings: list[str],
) -> None:
    visual = spec.get("visual_review_contract")
    needed_fields = visual_review_needed_fields(table_fields, metrics)
    enforced = visual_review_enforced(spec)
    if not isinstance(visual, dict):
        if needed_fields:
            message = "字段需要展示转换，必须声明 visual_review_contract：" + ", ".join(sorted(needed_fields))
            if enforced:
                errors.append(message)
            else:
                warnings.append(message)
        return

    for key in ["contract_version", "scope", "visualization_owner", "field_display_rules"]:
        if key not in visual:
            errors.append(f"visual_review_contract 缺少 `{key}`。")
    if visual.get("contract_version") != VISUAL_REVIEW_CONTRACT_VERSION:
        errors.append(f"visual_review_contract.contract_version 必须为 `{VISUAL_REVIEW_CONTRACT_VERSION}`。")
    if visual.get("visualization_owner") != "DA":
        errors.append("visual_review_contract.visualization_owner 必须为 DA；SQL 只声明展示审查契约，不决定图表类型或布局。")

    rules = visual.get("field_display_rules")
    if not isinstance(rules, list) or not rules:
        errors.append("visual_review_contract.field_display_rules 必须是非空数组。")
        rules = []

    metric_index = metric_by_field(metrics)
    rule_fields: set[str] = set()
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            errors.append(f"visual_review_contract.field_display_rules[{index}] 必须是 object。")
            continue
        for key in [
            "output_field",
            "semantic_type",
            "source_value_scale",
            "display_format",
            "decimal_places",
            "display_suffix",
            "preserve_raw_value",
            "sample_check",
        ]:
            if key not in rule:
                errors.append(f"展示规则 `{rule.get('output_field', index)}` 缺少 `{key}`。")
        field = str(rule.get("output_field") or "")
        if field:
            rule_fields.add(field)
        if field not in table_fields:
            errors.append(f"展示规则 `{field}` 必须对应最终输出字段。")

        display_format = str(rule.get("display_format") or "")
        source_scale = str(rule.get("source_value_scale") or "")
        semantic_type = str(rule.get("semantic_type") or "")
        decimals = rule.get("decimal_places")
        if display_format in {"percent", "decimal", "number", "integer", "duration_seconds"}:
            if not isinstance(decimals, int) or decimals < 0:
                errors.append(f"展示规则 `{field}` 的 decimal_places 必须是非负整数。")
        if display_format == "percent":
            if source_scale not in PERCENT_SOURCE_SCALES:
                errors.append(f"百分比展示字段 `{field}` 必须声明 source_value_scale 为 ratio_0_to_1 或 percent_0_to_100。")
            if rule.get("display_suffix") != "%":
                errors.append(f"百分比展示字段 `{field}` 的 display_suffix 必须为 `%`。")
            formula = str(rule.get("display_formula") or "").replace(" ", "").lower()
            if source_scale == "ratio_0_to_1" and formula and ("*100" not in formula and "100*" not in formula):
                errors.append(f"ratio_0_to_1 字段 `{field}` 如声明 display_formula，必须是 raw_value * 100 等价表达。")
            metric = metric_index.get(field, {})
            if metric and metric.get("dashboard_agg") == "SUM":
                warnings.append(f"百分比字段 `{field}` 的 metrics.dashboard_agg=SUM；请确认合计语义已在 total_metric_rules 中解释。")
        if likely_ratio_field(field) and display_format != "percent":
            warnings.append(f"字段 `{field}` 看起来是比例/率字段，但 visual_review_contract.display_format 不是 percent。")
        if semantic_type in {"ratio", "rate", "percentage"} and display_format != "percent":
            errors.append(f"比例类字段 `{field}` 的 display_format 必须为 percent。")
        if rule.get("preserve_raw_value") is not True:
            errors.append(f"展示规则 `{field}` 必须 preserve_raw_value=true，展示转换不得改变 SQL 原始数值口径。")
        if not isinstance(rule.get("sample_check"), dict):
            errors.append(f"展示规则 `{field}` 必须包含 sample_check object，例如 raw_value=0.2307, display_value=23.07%。")

    ratio_fields = {str(field) for field in table_fields if likely_ratio_field(field)}
    ratio_fields.update(str(metric.get("field")) for metric in metrics if isinstance(metric, dict) and likely_ratio_field(metric.get("field")))
    missing_ratio_rules = sorted(field for field in ratio_fields if field and field not in rule_fields)
    if missing_ratio_rules:
        message = (
            "比例/率/占比字段必须声明展示审查规则，说明原始值尺度、百分比展示和小数位："
            + ", ".join(missing_ratio_rules)
        )
        if enforced:
            errors.append(message)
        else:
            warnings.append(message)

    review_checks = visual.get("review_checks")
    if review_checks is not None and not isinstance(review_checks, list):
        errors.append("visual_review_contract.review_checks 如存在，必须是数组。")


def item_label(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("label") or item.get("field") or item.get("output_field") or item.get("parameter") or "").strip()


def active_filter_label(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    if item.get("current_effect") != "active":
        return ""
    if item.get("visible_to_dashboard_user") is not True:
        return ""
    return str(item.get("label") or item.get("output_field") or item.get("parameter") or "").strip()


def statistical_period_label(intent: dict, output_contract: dict | None = None) -> str:
    output_shape = (output_contract or {}).get("output_shape") if isinstance(output_contract, dict) else {}
    if not isinstance(output_shape, dict):
        output_shape = {}
    result_mode = str(output_shape.get("result_mode") or output_shape.get("mode") or intent.get("result_mode") or "").strip()
    time_grain = str(output_shape.get("time_grain") or intent.get("time_grain") or "").strip().lower()
    if result_mode == "daily_plus_total_table":
        return "按日 + 合计"
    if result_mode == "period_total_table" or time_grain == "none":
        return "区间合计"
    if result_mode == "daily_table" or time_grain == "day":
        return "按日"
    if result_mode == "hourly_table" or time_grain == "hour":
        return "按小时"
    if time_grain == "week":
        return "按周"
    if time_grain == "month":
        return "按月"
    if result_mode == "retention_table":
        return "留存周期"
    if result_mode == "funnel_table":
        return "漏斗步骤"
    if result_mode == "detail_table":
        return "明细"
    if result_mode == "sql_declared_table" or time_grain == "sql_declared":
        return "SQL 声明"
    return result_mode or time_grain or ""


def dashboard_summary(spec: dict | None) -> dict:
    spec = spec or {}
    filter_contract = spec.get("da_filter_contract") or {}
    filters = []
    for item in (filter_contract.get("sql_parameter_filters") or []) + (filter_contract.get("filterable_fields") or []):
        label = active_filter_label(item)
        if label and label not in filters:
            filters.append(label)
    return {
        "metrics": [label for label in (item_label(item) for item in spec.get("metrics") or []) if label],
        "dimensions": [label for label in (item_label(item) for item in spec.get("dimensions") or []) if label],
        "filters": filters,
        "statistical_period": statistical_period_label(spec.get("dashboard_intent") or {}, spec.get("sql_output_contract") or {}),
    }


def validate_top_contract(spec: dict | None, sql_text: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if spec is None:
        return errors, warnings

    required_modules = [
        "spec_meta",
        "dialect_profile",
        "project_context",
        "machine_review_contract",
        "validation_reference",
        "dashboard_intent",
        "da_delivery_contract",
        "da_output_contract",
        "da_filter_contract",
        "parameters",
        "dimensions",
        "metrics",
        "sql_output_contract",
    ]
    for module in required_modules:
        if module not in spec:
            errors.append(f"缺少 `{module}` 模块。")

    spec_meta = require_module(spec, "spec_meta", errors)
    if spec_meta.get("spec_version") not in {"4.8", 4.8}:
        errors.append("spec_meta.spec_version 必须为 4.8。")
    elif spec_meta.get("spec_version") in {"4.6", 4.6}:
        warnings.append("该看板仍使用 4.6 旧契约；新生成看板 SQL 应升级到 4.7。")

    dialect_profile = require_module(spec, "dialect_profile", errors)
    if dialect_profile.get("current_dialect") not in {"Hive", "StarRocks"}:
        errors.append("dialect_profile.current_dialect 必须为 Hive 或 StarRocks。")
    if dialect_profile.get("status") != "enabled":
        errors.append("dialect_profile.status 必须为 enabled。")

    project_context = require_module(spec, "project_context", errors)
    for key in [
        "project_id",
        "display_name",
        "query_engine",
        "query_environment",
        "dashboard_application",
        "table_naming_profile",
    ]:
        if key not in project_context:
            errors.append(f"project_context 缺少 `{key}`。")
    if str(project_context.get("dashboard_application", "")).strip().lower() in {"", "missing", "unknown"}:
        errors.append("project_context.dashboard_application 必须是 project_config.json 中配置的具体看板应用。")

    validation_reference = require_module(spec, "validation_reference", errors)
    verification_status = validation_reference.get("verification_status")
    if verification_status not in {"verified", "unverified_skipped_run", "proxy_verified"}:
        errors.append("validation_reference.verification_status 必须为 verified、unverified_skipped_run 或 proxy_verified。")
    if verification_status == "proxy_verified":
        for key in [
            "definition_project",
            "execution_project",
            "delivery_project",
            "concept_keys",
            "proxy_limitations",
            "future_verification_plan",
        ]:
            if not validation_reference.get(key):
                errors.append(f"proxy_verified validation_reference 缺少 `{key}`。")
        if not isinstance(validation_reference.get("concept_keys"), list):
            errors.append("proxy_verified validation_reference.concept_keys 必须是数组。")

    review_contract = require_module(spec, "machine_review_contract", errors)
    if review_contract.get("contract_version") != REVIEW_CONTRACT_VERSION:
        errors.append(f"machine_review_contract.contract_version 必须为 `{REVIEW_CONTRACT_VERSION}`。")
    if review_contract.get("parse_required") is not True:
        errors.append("machine_review_contract.parse_required 必须为 true。")
    if review_contract.get("contract_preview_required") is not True:
        errors.append("machine_review_contract.contract_preview_required 必须为 true。")

    delivery = require_module(spec, "da_delivery_contract", errors)
    for key in ["language", "da_owner_note", "grouping_total_policy"]:
        if key not in delivery:
            errors.append(f"da_delivery_contract 缺少 `{key}`。")
    if delivery.get("language") != "zh-CN":
        warnings.append("中文看板建议 da_delivery_contract.language = zh-CN。")

    output_contract = require_module(spec, "da_output_contract", errors)
    expected_output_contract = {
        "result_shape": "table_dataset",
        "visualization_owner": "DA",
    }
    for key, expected_value in expected_output_contract.items():
        if output_contract.get(key) != expected_value:
            errors.append(f"da_output_contract.{key} 必须为 `{expected_value}`。")
    if not isinstance(output_contract.get("table_fields"), list) or not output_contract.get("table_fields"):
        errors.append("da_output_contract.table_fields 必须是非空数组。")
    else:
        require_chinese_output_fields("da_output_contract.table_fields", output_contract.get("table_fields"), errors)
    for key in ["sql_responsibility", "da_responsibility"]:
        if not output_contract.get(key):
            errors.append(f"da_output_contract 缺少 `{key}`。")

    dashboard_intent = require_module(spec, "dashboard_intent", errors)
    for key in ["title", "description", "result_shape", "visualization_owner", "result_mode", "time_grain"]:
        if key not in dashboard_intent:
            errors.append(f"dashboard_intent 缺少 `{key}`。")
    if dashboard_intent.get("result_shape") != "table_dataset":
        errors.append("dashboard_intent.result_shape 必须为 table_dataset。")
    if dashboard_intent.get("visualization_owner") != "DA":
        errors.append("dashboard_intent.visualization_owner 必须为 DA。")
    if not statistical_period_label(dashboard_intent):
        errors.append("dashboard_intent 必须能派生统计周期；请填写 result_mode 或 time_grain。")
    result_mode = str(dashboard_intent.get("result_mode") or "")
    if result_mode not in {"sql_declared_table", "daily_plus_total_table", "period_total_table", "daily_table", "hourly_table", "retention_table", "funnel_table", "detail_table"}:
        errors.append("dashboard_intent.result_mode 不在支持范围内。")

    refresh_contract = spec.get("refresh_contract")
    if not isinstance(refresh_contract, dict):
        warnings.append("缺少 refresh_contract；新生成看板 SQL 必须声明 DA 只负责日期范围和是否实时刷新。")
        refresh_contract = {}
    else:
        if refresh_contract.get("da_decides_realtime_refresh") is not True:
            errors.append("refresh_contract.da_decides_realtime_refresh 必须为 true；DA 只判断是否需要实时刷新。")
        required_da = set(refresh_contract.get("required_da_decisions") or [])
        if not {"date_range", "realtime_refresh"}.issubset(required_da):
            errors.append("refresh_contract.required_da_decisions 必须包含 date_range 和 realtime_refresh。")


    filter_contract = require_module(spec, "da_filter_contract", errors)
    time_range = filter_contract.get("time_range")
    if not isinstance(time_range, dict):
        errors.append("da_filter_contract.time_range 必须是 object。")
    else:
        for key in ["parameter_names", "label", "default_range", "business_time_field", "business_time_mapping", "partition_parameters"]:
            if key not in time_range:
                errors.append(f"da_filter_contract.time_range 缺少 `{key}`。")
        parameter_names = time_range.get("parameter_names")
        if not isinstance(parameter_names, list) or not {"start_date", "end_date"}.issubset(set(parameter_names)):
            errors.append("time_range.parameter_names 必须包含 start_date 和 end_date。")

    sql_parameter_filters = filter_contract.get("sql_parameter_filters")
    if not isinstance(sql_parameter_filters, list):
        errors.append("da_filter_contract.sql_parameter_filters 必须是数组。")
        sql_parameter_filters = []

    filterable_fields = filter_contract.get("filterable_fields")
    if not isinstance(filterable_fields, list):
        errors.append("da_filter_contract.filterable_fields 必须是数组。")
        filterable_fields = []
    table_fields = output_contract.get("table_fields") if isinstance(output_contract.get("table_fields"), list) else []
    for field in filterable_fields:
        if not isinstance(field, dict):
            errors.append("filterable_fields 中每一项必须是 object。")
            continue
        for key in [
            "parameter",
            "label",
            "output_field",
            "current_effect",
            "visible_to_dashboard_user",
            "visible_to_external_user",
            "default",
            "value_source",
            "filter_behavior",
        ]:
            if key not in field:
                errors.append(f"可筛选字段 `{field.get('parameter', '')}` 缺少 `{key}`。")
        if field.get("current_effect") == "active":
            if field.get("output_field") not in table_fields:
                errors.append(
                    f"当前生效的 DA 可筛选字段 `{field.get('parameter')}` "
                    f"必须输出 `{field.get('output_field')}`。"
                )
        else:
            warnings.append(f"filterable_fields 中 `{field.get('parameter', '')}` 不是 active；预留筛选建议放入 future_filters。")

    future_filters = filter_contract.get("future_filters", [])
    if not isinstance(future_filters, list):
        errors.append("da_filter_contract.future_filters 必须是数组。")
        future_filters = []
    for field in future_filters:
        if not isinstance(field, dict):
            errors.append("future_filters 中每一项必须是 object。")
            continue
        for key in [
            "parameter",
            "label",
            "output_field",
            "current_effect",
            "visible_to_dashboard_user",
            "visible_to_external_user",
            "default",
            "value_source",
            "activation_requirement",
        ]:
            if key not in field:
                errors.append(f"未来预留筛选 `{field.get('parameter', '')}` 缺少 `{key}`。")
        if field.get("current_effect") != "not_active":
            errors.append(f"未来预留筛选 `{field.get('parameter', '')}` 必须 current_effect=not_active。")
        if not field.get("activation_requirement"):
            errors.append(f"未来预留筛选 `{field.get('parameter', '')}` 必须声明 activation_requirement。")

    fixed_filters = filter_contract.get("fixed_sql_filters", [])
    if not isinstance(fixed_filters, list):
        errors.append("da_filter_contract.fixed_sql_filters 必须是数组。")
        fixed_filters = []
    for fixed_filter in fixed_filters:
        if not isinstance(fixed_filter, dict):
            errors.append("fixed_sql_filters 中每一项必须是 object。")
            continue
        for key in ["field", "condition", "da_filterable", "reason"]:
            if key not in fixed_filter:
                errors.append(f"固定 SQL 筛选 `{fixed_filter.get('field', '')}` 缺少 `{key}`。")
        if fixed_filter.get("da_filterable") is True and fixed_filter.get("all_values_available") is not True:
            errors.append(
                f"固定筛选字段 `{fixed_filter.get('field')}` 若要在 DA 可筛选，必须 all_values_available=true 并输出全量值。"
            )
        if fixed_filter.get("da_filterable") is True and fixed_filter.get("output_field") not in table_fields:
            errors.append(
                f"固定筛选字段 `{fixed_filter.get('field')}` 若要在 DA 可筛选，必须把 output_field 写入最终输出字段。"
            )

    params = spec.get("parameters")
    if not isinstance(params, list) or not params:
        errors.append("parameters 必须是非空数组。")
        params = []
    param_names: set[str] = set()
    for index, param in enumerate(params, start=1):
        if not isinstance(param, dict):
            errors.append(f"parameters[{index}] 必须是 object。")
            continue
        if param.get("name"):
            param_names.add(str(param.get("name")))
        for key in [
            "name",
            "label",
            "type",
            "default",
            "required",
            "visible",
            "visible_to_dashboard_user",
            "visible_to_external_user",
            "parameter_role",
            "values",
            "sql_usage",
            "field_mapping",
        ]:
            if key not in param:
                errors.append(f"参数 `{param.get('name', index)}` 缺少 `{key}`。")
        role = param.get("parameter_role")
        if role == "hidden_partition":
            if param.get("visible_to_dashboard_user") is not False:
                errors.append(f"隐藏分区参数 `{param.get('name')}` 不能对普通看板用户可见。")
            if param.get("visible_to_external_user") is not False:
                errors.append(f"隐藏分区参数 `{param.get('name')}` 不能对外部用户可见。")

    for field in sql_parameter_filters:
        if not isinstance(field, dict):
            errors.append("sql_parameter_filters 中每一项必须是 object。")
            continue
        for key in [
            "parameter",
            "label",
            "current_effect",
            "visible_to_dashboard_user",
            "visible_to_external_user",
            "default",
            "values",
            "value_source",
            "sql_mapping",
            "filter_behavior",
        ]:
            if key not in field:
                errors.append(f"SQL 参数筛选 `{field.get('parameter', '')}` 缺少 `{key}`。")
        if field.get("parameter") not in param_names:
            errors.append(f"SQL 参数筛选 `{field.get('parameter', '')}` 必须匹配 parameters.name。")
        if field.get("current_effect") != "active":
            warnings.append(f"SQL 参数筛选 `{field.get('parameter', '')}` 不是 active；若当前未生效应放入 future_filters。")
        if not isinstance(field.get("values"), list):
            errors.append(f"SQL 参数筛选 `{field.get('parameter', '')}` 的 values 必须是数组。")
        if not field.get("sql_mapping"):
            errors.append(f"SQL 参数筛选 `{field.get('parameter', '')}` 必须声明 sql_mapping。")

    business_logic = require_module(spec, "business_logic", errors)
    for key in [
        "source_query_logic_reference",
        "logic_consistency",
        "dashboard_adaptation_scope",
        "business_logic_changed",
        "change_validation_requirement",
    ]:
        if key not in business_logic:
            errors.append(f"business_logic 缺少 `{key}`。")
    if business_logic.get("business_logic_changed") is True:
        validation_reference = spec.get("validation_reference") or {}
        if validation_reference.get("verification_status") != "unverified_skipped_run" and not business_logic.get("change_validation_requirement"):
            errors.append("看板 SQL 改变查询口径时，必须声明重新验证要求或标记未验证。")

    grouping = delivery.get("grouping_total_policy")
    if not isinstance(grouping, dict):
        errors.append("da_delivery_contract.grouping_total_policy 必须是 object。")
    else:
        for key in ["grouping_fields", "sql_contains_total_rows", "da_generate_total", "total_metric_rules"]:
            if key not in grouping:
                errors.append(f"grouping_total_policy 缺少 `{key}`。")
        if grouping.get("da_generate_total") is not False:
            errors.append("grouping_total_policy.da_generate_total 必须为 false；DA 不再另行生成合计。")
        if grouping.get("sql_contains_total_rows") is True and grouping.get("total_label") not in {"合计", None, ""}:
            errors.append("SQL 声明含合计行时 total_label 如填写应为 合计。")
        if grouping.get("avoid_duplicate_scan") is not True:
            errors.append("grouping_total_policy.avoid_duplicate_scan 必须为 true。")

    dimensions = spec.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        errors.append("dimensions 必须是非空数组；如看板没有拆分维度，应声明“整体/无维度”维度说明。")
        dimensions = []
    dimension_fields: set[str] = set()
    for dim in dimensions:
        if not isinstance(dim, dict):
            errors.append("dimensions 中每个维度必须是 object。")
            continue
        if dim.get("field"):
            dimension_fields.add(str(dim.get("field")))
        if "total_policy" not in dim:
            errors.append(f"维度 `{dim.get('field', '')}` 缺少 total_policy。")
    for field in filterable_fields:
        if not isinstance(field, dict) or field.get("current_effect") != "active":
            continue
        output_field = str(field.get("output_field") or "")
        if output_field and output_field in dimension_fields:
            warnings.append(
                f"DA 可筛选项 `{field.get('label') or output_field}` 同时也是维度；"
                "请确认这是明确要求的看板交互筛选，不是从维度/桶/排序字段自动推导。"
            )

    metrics = spec.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errors.append("metrics 必须是非空数组。")
        metrics = []
    for metric in metrics:
        if not isinstance(metric, dict):
            errors.append("metrics 中每个指标必须是 object。")
            continue
        for key in ["field", "label", "dashboard_agg", "total_safe", "total_meaning"]:
            if key not in metric:
                errors.append(f"指标 `{metric.get('field', '')}` 缺少 `{key}`。")
        if metric.get("metric_type") == "rate":
            for key in ["numerator", "denominator", "dashboard_formula"]:
                if not metric.get(key):
                    errors.append(f"率指标 `{metric.get('field', '')}` 必须声明 `{key}`。")

    validate_visual_review_contract(spec, table_fields, metrics, errors, warnings)

    output = require_module(spec, "sql_output_contract", errors)
    expected = output.get("expected_fields")
    if not isinstance(expected, list) or not expected:
        errors.append("sql_output_contract.expected_fields 必须是非空数组。")
    else:
        require_chinese_output_fields("sql_output_contract.expected_fields", expected, errors)
    final_fields = final_select_fields(sql_text)
    if final_fields:
        require_chinese_output_fields("最终 SELECT 输出字段", final_fields, errors)
    if isinstance(expected, list) and final_fields and expected != final_fields:
        errors.append(
            "expected_fields 必须与最终 SELECT 字段和顺序一致；"
            f"expected={expected}, actual={final_fields}。"
        )
    table_fields = output_contract.get("table_fields")
    if isinstance(expected, list) and isinstance(table_fields, list) and expected != table_fields:
        errors.append("da_output_contract.table_fields 必须与 sql_output_contract.expected_fields 一致。")
    if "contains_total_rows" not in output:
        errors.append("sql_output_contract 缺少 contains_total_rows。")
    output_shape = output.get("output_shape")
    if not isinstance(output_shape, dict):
        warnings.append("sql_output_contract.output_shape 缺失；暂按 dashboard_intent/contains_total_rows 兼容，后续生成器应显式写出输出形态。")
    else:
        shape_mode = output_shape.get("result_mode") or output_shape.get("mode")
        if shape_mode not in {"sql_declared_table", "daily_plus_total_table", "period_total_table", "daily_table", "hourly_table", "retention_table", "funnel_table", "detail_table"}:
            errors.append("sql_output_contract.output_shape.result_mode 不在支持范围内。")
        if "contains_total_rows" in output_shape and output_shape.get("contains_total_rows") != output.get("contains_total_rows"):
            errors.append("sql_output_contract.output_shape.contains_total_rows 必须与 contains_total_rows 一致。")
        if not output_shape.get("default_semantics"):
            warnings.append("sql_output_contract.output_shape 建议声明 default_semantics，说明默认区间结果/按日/合计的业务含义。")

    if output.get("contains_total_rows") and not output.get("total_row_indicator_field"):
        errors.append("SQL 输出合计行时必须声明 total_row_indicator_field。")
    if output.get("contains_total_rows") and output.get("total_row_label") != "合计":
        errors.append("SQL 输出合计行时必须声明 total_row_label=合计。")

    if not isinstance(spec.get("refresh_contract"), dict):
        migration_only_patterns = [
            "dashboard_intent.result_mode 不在支持范围内",
            "grouping_total_policy.da_generate_total 必须为 false",
            "grouping_total_policy.avoid_duplicate_scan 必须为 true",
            "total_source 必须",
            "contains_total_rows",
        ]
        remaining_errors = []
        for error in errors:
            if any(pattern in error for pattern in migration_only_patterns):
                warnings.append("历史看板缺少 refresh_contract，按 SQL 声明输出形态契约迁移前暂不硬阻断：" + error)
            else:
                remaining_errors.append(error)
        errors = remaining_errors

    return errors, warnings


def find_run_record(root: Path, artifact: dict, spec: dict | None) -> dict | None:
    package = artifact.get("package_manifest")
    if isinstance(package, dict):
        members = [item for item in package.get("members", []) if isinstance(item, dict)]
        run_members = [
            item
            for item in members
            if str(item.get("role") or "").lower() in RUN_EVIDENCE_ROLES
        ]
        linked_run = str(artifact.get("linked_run") or "")
        linked_name = Path(linked_run).name if linked_run else ""
        member_id = str(artifact.get("package_member_id") or "")
        evidence_ids = {
            str(edge.get("to_member_id") or "")
            for edge in package.get("lineage", [])
            if isinstance(edge, dict)
            and str(edge.get("from_member_id") or "") == member_id
            and str(edge.get("relation") or "") == "evidenced_by"
        }
        evidence_ids.update(
            str(edge.get("from_member_id") or "")
            for edge in package.get("lineage", [])
            if isinstance(edge, dict)
            and str(edge.get("to_member_id") or "") == member_id
            and str(edge.get("relation") or "") == "records_run_for"
        )
        selected = next(
            (
                item
                for item in run_members
                if str(item.get("member_id") or "") in evidence_ids
                or (linked_name and Path(str(item.get("path") or "")).name == linked_name)
            ),
            None,
        )
        if selected is not None:
            record_path = root / str(selected.get("path") or "")
            record: dict[str, object] = {
                "path": str(selected.get("path") or ""),
                "status": "recorded",
            }
            try:
                raw_record = record_path.read_text(encoding="utf-8-sig")
                if record_path.suffix.lower() == ".json":
                    loaded = json.loads(raw_record)
                    if isinstance(loaded, dict):
                        record.update(loaded)
                else:
                    for line in raw_record.splitlines():
                        match = re.match(r"^- ([a-zA-Z0-9_]+):\s*(.*)$", line.strip())
                        if not match:
                            continue
                        key, value = match.groups()
                        if value.lower() in {"true", "false"}:
                            record[key] = value.lower() == "true"
                        elif key == "row_count" and value.isdigit():
                            record[key] = int(value)
                        else:
                            record[key] = value
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
            stem = record_path.stem
            result_member = next(
                (
                    item
                    for item in members
                    if Path(str(item.get("path") or "")).stem == stem
                    and Path(str(item.get("path") or "")).suffix.lower() in RESULT_EXTENSIONS
                ),
                None,
            )
            if result_member is not None:
                record["evidence_file"] = str(result_member.get("path") or "")
            return record

    manifest = read_json(manifest_path(root), {})
    linked_run = artifact.get("linked_run") or ""
    validation_run = ""
    if spec:
        validation_reference = spec.get("validation_reference") or {}
        validation_run = validation_reference.get("user_run_evidence") or ""
    for run in manifest.get("run_evidence", []):
        if linked_run and run.get("path") == linked_run:
            return run
        if validation_run and run.get("path") == validation_run:
            return run
    return None


def result_file_from(root: Path, artifact: dict, spec: dict | None) -> str:
    run = find_run_record(root, artifact, spec)
    if run and run.get("evidence_file"):
        return run["evidence_file"]
    if spec:
        reference = spec.get("validation_reference") or {}
        result_ref = reference.get("result_file_reference") or ""
        if result_ref:
            return result_ref
    return ""


def retained_result_contract(run: dict | None) -> dict:
    if not run:
        return {}
    retained = run.get("retained_result_evidence")
    if isinstance(retained, dict) and retained:
        return retained
    columns = run.get("result_columns") or run.get("sample_fields")
    if isinstance(columns, list) and columns:
        return {
            "contract_version": "legacy_run_fields",
            "columns": columns,
            "sample_rows": [],
            "schema_fingerprint": run.get("result_schema_fingerprint") or "",
        }
    return {}


def dashboard_expected_fields(spec: dict | None) -> list[str]:
    if not isinstance(spec, dict):
        return []
    output = spec.get("sql_output_contract") if isinstance(spec.get("sql_output_contract"), dict) else {}
    fields = output.get("expected_fields")
    if isinstance(fields, list) and fields:
        return [str(item or "").strip() for item in fields if str(item or "").strip()]
    da_output = spec.get("da_output_contract") if isinstance(spec.get("da_output_contract"), dict) else {}
    table_fields = da_output.get("table_fields")
    if isinstance(table_fields, list):
        return [str(item or "").strip() for item in table_fields if str(item or "").strip()]
    return []


def filter_sample_rows(rows: list[dict], fields: list[str]) -> list[dict]:
    if not fields:
        return rows
    return [
        {field: row.get(field, "") for field in fields}
        for row in rows
        if isinstance(row, dict)
    ]


def read_csv_sample(path: Path, limit: int) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(dict(row))
            if len(rows) >= limit:
                break
    return rows


def xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        xml = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml)
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = []
    for item in root.findall("x:si", ns):
        texts = [node.text or "" for node in item.findall(".//x:t", ns)]
        strings.append("".join(texts))
    return strings


def read_xlsx_sample(path: Path, limit: int) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        shared = xlsx_shared_strings(zf)
        sheet_names = [name for name in zf.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")]
        if not sheet_names:
            return []
        root = ET.fromstring(zf.read(sorted(sheet_names)[0]))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    matrix = []
    for row in root.findall(".//x:sheetData/x:row", ns):
        values = []
        for cell in row.findall("x:c", ns):
            cell_type = cell.attrib.get("t")
            value_node = cell.find("x:v", ns)
            if cell_type == "inlineStr":
                text_node = cell.find(".//x:t", ns)
                values.append(text_node.text if text_node is not None else "")
            elif value_node is None:
                values.append("")
            elif cell_type == "s":
                index = int(value_node.text or 0)
                values.append(shared[index] if index < len(shared) else "")
            else:
                values.append(value_node.text or "")
        if values:
            matrix.append(values)
        if len(matrix) > limit:
            break
    if not matrix:
        return []
    headers = [str(value) or f"col_{idx + 1}" for idx, value in enumerate(matrix[0])]
    rows = []
    for values in matrix[1 : limit + 1]:
        rows.append({headers[idx]: values[idx] if idx < len(values) else "" for idx in range(len(headers))})
    return rows


def synthetic_value(field: str, index: int) -> str | int | float:
    lower = field.lower()
    if "date" in lower or lower in {"dt", "stat_day"}:
        return f"2026-06-{index + 1:02d}"
    if "hour" in lower:
        return f"{index:02d}:00"
    if "rate" in lower or "ratio" in lower:
        return round(0.12 + index * 0.03, 4)
    if any(token in lower for token in ["cnt", "count", "num", "uv", "pv", "total", "amount"]):
        return 100 + index * 17
    if "duration" in lower:
        return round(20.5 + index * 3.2, 2)
    return f"样例{index + 1}"


def synthetic_rows(spec: dict | None, limit: int) -> list[dict]:
    output = (spec or {}).get("sql_output_contract") or {}
    fields = output.get("expected_fields")
    if not isinstance(fields, list) or not fields:
        metrics = (spec or {}).get("metrics") or []
        dimensions = (spec or {}).get("dimensions") or []
        fields = [item.get("field") for item in dimensions + metrics if isinstance(item, dict) and item.get("field")]
    fields = fields or ["stat_date", "metric_field"]
    return [{field: synthetic_value(str(field), idx) for field in fields} for idx in range(min(limit, 5))]


def sample_rows(root: Path, artifact: dict, spec: dict | None, limit: int) -> tuple[list[dict], dict]:
    run = find_run_record(root, artifact, spec)
    expected_fields = dashboard_expected_fields(spec)
    retained = retained_result_contract(run)
    contract_fields = [str(item or "").strip() for item in retained.get("columns", []) if str(item or "").strip()]
    fields = expected_fields or contract_fields
    retained_rows = [row for row in retained.get("sample_rows", []) if isinstance(row, dict)]
    if retained_rows:
        return filter_sample_rows(retained_rows[:limit], fields), {
            "type": "retained_contract",
            "path": str((run or {}).get("evidence_file") or ""),
            "note": "使用固化时写入的保留字段样例；原始结果文件仅作为审计证据。",
            "columns": fields,
        }
    rel = result_file_from(root, artifact, spec)
    if rel:
        path = root / rel
        if path.exists() and path.suffix.lower() in RESULT_EXTENSIONS:
            try:
                rows = read_csv_sample(path, limit) if path.suffix.lower() == ".csv" else read_xlsx_sample(path, limit)
                rows = filter_sample_rows(rows, fields)
                if rows:
                    return rows, {"type": "actual", "path": rel, "note": "使用已归档真实跑数结果文件样例，并按正式输出字段契约过滤。", "columns": fields}
            except Exception as exc:  # noqa: BLE001
                return synthetic_rows(spec, limit), {
                    "type": "synthetic",
                    "path": rel,
                    "note": f"真实结果文件读取失败，改用自动样例：{exc}",
                }
        return synthetic_rows(spec, limit), {
            "type": "synthetic",
            "path": rel,
            "note": "声明了结果文件但文件不存在或格式不支持，改用自动样例。",
        }
    return synthetic_rows(spec, limit), {
        "type": "synthetic",
        "path": "",
        "note": "没有可用真实结果文件，仅展示筛选合同和自动样例。",
    }


def load_state(state_path: Path) -> dict:
    state = read_json(state_path, {})
    if not state:
        state = {"version": STATE_VERSION, "updated_at": now_iso(), "items": {}}
    state.setdefault("version", STATE_VERSION)
    state.setdefault("updated_at", now_iso())
    state.setdefault("items", {})
    return state


def state_entry(state: dict, rel_path: str) -> dict:
    return state.get("items", {}).get(rel_path, {})


def should_skip_approved(state: dict, rel_path: str, sql_hash: str, include_approved: bool) -> bool:
    if include_approved:
        return False
    item = state_entry(state, rel_path)
    return item.get("status") == "approved" and item.get("sql_hash") == sql_hash


def build_dashboard_item(root: Path, artifact: dict, state: dict, include_approved: bool, sample_limit: int) -> tuple[dict, bool]:
    sql_path = root / artifact["path"]
    sql_text = sql_path.read_text(encoding="utf-8")
    sql_hash = sha256_text(sql_text)
    rel_path = normalize_rel(root, sql_path)
    spec, parse_errors = load_sidecar_spec(root, artifact, sql_path)
    contract_errors, contract_warnings = validate_top_contract(spec, sql_text)
    rows, sample_meta = sample_rows(root, artifact, spec, sample_limit)
    entry = state_entry(state, rel_path)
    item = {
        "state_key": rel_path,
        "path": rel_path,
        "title": artifact.get("title") or rel_path,
        "slug": artifact.get("slug", ""),
        "version": artifact.get("version"),
        "sql_hash": sql_hash,
        "verification_status": artifact.get("verification_status") or (spec or {}).get("validation_reference", {}).get("verification_status", ""),
        "linked_run": artifact.get("linked_run") or "",
        "generation_provenance": provenance_from_sources(artifact, spec or {}),
        "state": entry,
        "parse_errors": parse_errors,
        "contract_errors": contract_errors,
        "contract_warnings": contract_warnings,
        "spec": spec or {},
        "dashboard_summary": dashboard_summary(spec),
        "contracts": {
            "project_context": (spec or {}).get("project_context") or {},
            "time_range": ((spec or {}).get("da_filter_contract") or {}).get("time_range") or {},
            "sql_filters": ((spec or {}).get("da_filter_contract") or {}).get("sql_parameter_filters") or [],
            "visible": ((spec or {}).get("da_filter_contract") or {}).get("filterable_fields") or [],
            "future": ((spec or {}).get("da_filter_contract") or {}).get("future_filters") or [],
            "fixed": ((spec or {}).get("da_filter_contract") or {}).get("fixed_sql_filters") or [],
            "hidden": [
                param
                for param in ((spec or {}).get("parameters") or [])
                if isinstance(param, dict) and param.get("parameter_role") == "hidden_partition"
            ],
            "parameters": (spec or {}).get("parameters") or [],
        },
        "da_output_contract": (spec or {}).get("da_output_contract") or {},
        "sql_output_contract": (spec or {}).get("sql_output_contract") or {},
        "visual_review_contract": (spec or {}).get("visual_review_contract") or {},
        "grouping_total_policy": ((spec or {}).get("da_delivery_contract") or {}).get("grouping_total_policy") or {},
        "refresh_contract": (spec or {}).get("refresh_contract") or {},
        "metrics": (spec or {}).get("metrics") or [],
        "dimensions": (spec or {}).get("dimensions") or [],
        "sample": rows,
        "sample_meta": sample_meta,
    }
    return item, should_skip_approved(state, rel_path, sql_hash, include_approved)


def build_payload(root: Path, state_path: Path, include_approved: bool, include_history: bool, sample_limit: int) -> dict:
    state = load_state(state_path)
    dashboard_items = []
    skipped_items = []
    for artifact in load_dashboard_artifacts(root, include_history):
        item, skipped = build_dashboard_item(root, artifact, state, include_approved, sample_limit)
        if skipped:
            skipped_items.append(item)
        else:
            dashboard_items.append(item)
    return {
        "project_root": ".",
        "generated_at": now_iso(),
        "state_path": normalize_rel(root, state_path),
        "items": dashboard_items,
        "skipped_items": skipped_items,
        "state": state,
        "review_contract_version": REVIEW_CONTRACT_VERSION,
    }


def html_shell(payload: dict | None, api_url: str | None = None) -> str:
    payload_json = "null" if payload is None else json.dumps(payload, ensure_ascii=False)
    api_url_json = json.dumps(api_url, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>看板 SQL Review</title>
  <style>
    :root {{ --bg: #f6f7f9; --panel: #fff; --line: #d8dde6; --text: #18202a; --muted: #6a7280; --ok: #0f7b43; --bad: #b42318; --warn: #a15c00; --brand: #2454a6; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif; color: var(--text); background: var(--bg); }}
    .layout {{ display: grid; grid-template-columns: 360px 1fr; min-height: calc(100vh - 172px); }}
    header {{ padding: 14px 18px; border-bottom: 1px solid var(--line); background: var(--panel); display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    h1 {{ margin: 0; font-size: 18px; }}
    .sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    aside {{ border-right: 1px solid var(--line); background: var(--panel); overflow: auto; }}
    main {{ padding: 18px; overflow: auto; }}
    .sql-item {{ border-bottom: 1px solid var(--line); padding: 12px 14px; cursor: pointer; }}
    .sql-item:hover, .sql-item.active {{ background: #edf3ff; }}
    .sql-title {{ font-weight: 600; font-size: 14px; }}
    .sql-path {{ color: var(--muted); font-size: 12px; word-break: break-all; margin-top: 4px; }}
    .badge {{ display: inline-block; padding: 2px 7px; border-radius: 12px; font-size: 12px; margin: 6px 4px 0 0; background: #eef1f5; }}
    .ok {{ color: var(--ok); background: #e8f6ee; }}
    .bad {{ color: var(--bad); background: #fdecec; }}
    .warn {{ color: var(--warn); background: #fff4df; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; margin-bottom: 14px; }}
    .card h2 {{ margin: 0 0 10px; font-size: 16px; }}
    .card-title-row {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 10px; }}
    .card-title-row h2 {{ margin: 0; }}
    .card h3 {{ margin: 0 0 8px; font-size: 14px; }}
    .kv {{ display: grid; grid-template-columns: 120px 1fr; gap: 8px; font-size: 13px; margin: 6px 0; }}
    .kv span:first-child {{ color: var(--muted); }}
    .field-list {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .field-chip {{ display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; background: #f8fafc; color: var(--text); }}
    .summary-note {{ color: var(--muted); font-size: 12px; margin-top: 8px; }}
    input, select, textarea {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 7px 8px; font: inherit; background: #fff; }}
    button {{ border: 1px solid var(--line); background: #fff; border-radius: 6px; padding: 8px 12px; cursor: pointer; font-weight: 600; }}
    button.small {{ padding: 5px 9px; font-size: 12px; }}
    button.primary {{ background: var(--brand); color: white; border-color: var(--brand); }}
    button.danger {{ color: var(--bad); border-color: #f4b4ae; }}
    .copy-status {{ color: var(--muted); font-size: 12px; margin-left: 6px; white-space: nowrap; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 13px; }}
    th, td {{ border: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f0f3f8; }}
    .issues li {{ margin: 6px 0; }}
    footer {{ border-top: 1px solid var(--line); background: var(--panel); padding: 12px 18px; }}
    .review-summary {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    .empty {{ color: var(--muted); padding: 24px; text-align: center; }}
    @media (max-width: 900px) {{ .layout {{ grid-template-columns: 1fr; }} aside {{ border-right: 0; border-bottom: 1px solid var(--line); max-height: 260px; }} }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>看板 SQL Review</h1>
      <div class="sub" id="meta"></div>
    </div>
    <div>
      <button id="exportState">导出状态 JSON</button>
      <button id="clearLocal">清空本页本地状态</button>
    </div>
  </header>
  <div class="layout">
    <aside id="list"></aside>
    <main id="detail"></main>
  </div>
  <footer>
    <div class="review-summary">
      <div><strong>已确认通过</strong><div id="passed"></div></div>
      <div><strong>标记有问题</strong><div id="failed"></div></div>
    </div>
  </footer>
  <script>
    const dashboardReviewApiUrl = {api_url_json};
    let payload = {payload_json} || {{project_root: '', generated_at: '', state_path: '', items: [], skipped_items: [], state: {{version: 1, items: {{}}}}}};
    function storageKey() {{
      return 'dashboard-review-state:' + (payload.project_root || 'dynamic');
    }}
    function loadDashboardState() {{
      state = JSON.parse(localStorage.getItem(storageKey()) || JSON.stringify(payload.state || {{version: 1, items: {{}}}}));
      state.items = state.items || {{}};
    }}
    let state = {{version: 1, items: {{}}}};
    loadDashboardState();
    state.items = state.items || {{}};
    let selected = 0;

    function statusOf(item) {{
      const saved = state.items[item.state_key] || item.state || {{}};
      if (saved.sql_hash && saved.sql_hash !== item.sql_hash) return 'changed';
      return saved.status || 'pending';
    }}
    function labelStatus(status) {{
      if (status === 'approved') return '<span class="badge ok">已确认</span>';
      if (status === 'rejected') return '<span class="badge bad">有问题</span>';
      if (status === 'changed') return '<span class="badge warn">SQL 已变化</span>';
      return '<span class="badge">待确认</span>';
    }}
    function saveLocal() {{
      state.updated_at = new Date().toISOString();
      localStorage.setItem(storageKey(), JSON.stringify(state));
    }}
    async function persistServer() {{
      if (!location.protocol.startsWith('http')) return;
      try {{
        await fetch('/api/state', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(state)}});
      }} catch (err) {{
        console.warn('state server save failed', err);
      }}
    }}
    function setStatus(status) {{
      const item = payload.items[selected];
      if (!item) return;
      const note = document.getElementById('reviewNote').value || '';
      state.items[item.state_key] = {{status, note, sql_hash: item.sql_hash, reviewed_at: new Date().toISOString(), path: item.path, title: item.title}};
      saveLocal();
      persistServer();
      render();
    }}
    function renderList() {{
      const list = document.getElementById('list');
      if (!payload.items.length) {{
        list.innerHTML = '<div class="empty">没有需要 review 的看板 SQL。已跳过 ' + payload.skipped_items.length + ' 个已确认项。</div>';
        return;
      }}
      list.innerHTML = payload.items.map((item, idx) => {{
        const problems = (item.parse_errors.length + item.contract_errors.length);
        return `<div class="sql-item ${{idx === selected ? 'active' : ''}}" onclick="selected=${{idx}}; render()">
          <div class="sql-title">${{escapeHtml(item.title)}}</div>
          <div class="sql-path">${{escapeHtml(item.path)}}</div>
          ${{labelStatus(statusOf(item))}}
          ${{problems ? '<span class="badge bad">契约问题 ' + problems + '</span>' : '<span class="badge ok">可解析</span>'}}
          ${{item.sample_meta.type === 'actual' ? '<span class="badge ok">真实样例</span>' : '<span class="badge warn">自动样例</span>'}}
        </div>`;
      }}).join('');
    }}
    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, m => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#039;'}}[m]));
    }}
    function provenanceText(prov) {{
      if (!prov || !Object.keys(prov).length) return '生成来源未记录';
      const skill = prov.skill_name || 'sql-engineering';
      const version = prov.skill_version || 'unknown';
      const workflow = prov.workflow || 'unknown';
      const script = prov.generated_by_script || 'unknown';
      const source = prov.source || 'generated';
      const spec = prov.sql_spec_version || 'unknown';
      return `${{skill}} v${{version}} / spec ${{spec}} / ${{workflow}} / ${{script}} / ${{source}}`;
    }}
    function renderProvenance(prov) {{
      return `
        <div class="card">
          <h2>资产生成来源</h2>
          <div class="kv"><span>生成来源</span><span>${{escapeHtml(provenanceText(prov))}}</span></div>
          <div class="kv"><span>SQL Spec</span><span>${{escapeHtml(prov?.sql_spec_version || '未记录')}}</span></div>
          <div class="kv"><span>生成时间</span><span>${{escapeHtml(prov?.generated_at || '未记录')}}</span></div>
          <div class="kv"><span>保存时间</span><span>${{escapeHtml(prov?.saved_at || '未记录')}}</span></div>
          <div class="kv"><span>保存脚本</span><span>${{escapeHtml(prov?.saved_by_script || '未记录')}}</span></div>
          ${{prov?.backfilled_by_script ? `<div class="kv"><span>历史回填</span><span>${{escapeHtml(prov.backfilled_by_script)}}；skill ${{escapeHtml(prov.backfilled_by_skill_version || 'unknown')}}；${{escapeHtml(prov.backfilled_at || '未记录')}}</span></div>` : ''}}
        </div>`;
    }}
    function renderChipList(values) {{
      const list = (values || []).filter(Boolean);
      if (!list.length) return '<span class="sub">无</span>';
      return list.map(value => '<span class="field-chip">' + escapeHtml(value) + '</span>').join('');
    }}
    function summaryListText(values) {{
      const list = (values || []).map(value => String(value ?? '').trim()).filter(Boolean);
      return list.length ? list.join('、') : '无';
    }}
    function dashboardSummaryText(summary) {{
      summary = summary || {{}};
      return [
        '指标：' + summaryListText(summary.metrics),
        '维度：' + summaryListText(summary.dimensions),
        '筛选项：' + summaryListText(summary.filters),
        '统计周期：' + (String(summary.statistical_period ?? '').trim() || '无')
      ].join('\\n');
    }}
    function fallbackCopyText(text) {{
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.left = '-9999px';
      textarea.style.top = '0';
      document.body.appendChild(textarea);
      textarea.select();
      let copied = false;
      try {{
        copied = document.execCommand('copy');
      }} catch (err) {{
        copied = false;
      }}
      document.body.removeChild(textarea);
      return copied;
    }}
    async function copyText(text) {{
      try {{
        if (navigator.clipboard && window.isSecureContext) {{
          await navigator.clipboard.writeText(text);
          return true;
        }}
      }} catch (err) {{
        console.warn('clipboard api failed, using fallback', err);
      }}
      return fallbackCopyText(text);
    }}
    async function copyDashboardSummary(event) {{
      event.preventDefault();
      event.stopPropagation();
      const item = payload.items[selected];
      if (!item) return;
      const ok = await copyText(dashboardSummaryText(item.dashboard_summary || {{}}));
      const status = document.getElementById('summaryCopyStatus');
      if (status) {{
        status.textContent = ok ? '已复制' : '复制失败';
        window.setTimeout(() => {{ status.textContent = ''; }}, 1600);
      }}
    }}
    function renderDashboardSummary(item) {{
      const summary = item.dashboard_summary || {{}};
      return `
        <div class="card">
          <div class="card-title-row">
            <h2>看板摘要</h2>
            <div>
              <button class="small" onclick="copyDashboardSummary(event)">复制摘要</button>
              <span class="copy-status" id="summaryCopyStatus"></span>
            </div>
          </div>
          <div class="kv"><span>指标</span><span class="field-list">${{renderChipList(summary.metrics)}}</span></div>
          <div class="kv"><span>维度</span><span class="field-list">${{renderChipList(summary.dimensions)}}</span></div>
          <div class="kv"><span>筛选项</span><span class="field-list">${{renderChipList(summary.filters)}}</span></div>
          <div class="kv"><span>统计周期</span><span>${{escapeHtml(summary.statistical_period || '')}}</span></div>
          <div class="summary-note">筛选项仅指看板可交互控制；SQL 固定条件和维度不会自动成为筛选项。</div>
        </div>`;
    }}
    function renderControls(item) {{
      const timeRange = item.contracts.time_range || {{}};
      const sqlFilters = item.contracts.sql_filters || [];
      const visible = item.contracts.visible || [];
      const fixed = item.contracts.fixed || [];
      const future = item.contracts.future || [];
      const hidden = item.contracts.hidden || [];
      const blocks = [];
      blocks.push('<div class="card"><h2>时间范围</h2>' +
        '<div class="kv"><span>参数</span><span>' + escapeHtml((timeRange.parameter_names || []).join(', ')) + '</span></div>' +
        '<div class="kv"><span>默认范围</span><span>' + escapeHtml(timeRange.default_label || timeRange.default_range || '') + '</span></div>' +
        '<div class="kv"><span>业务时间</span><span>' + escapeHtml(timeRange.business_time_field || '') + '</span></div>' +
        '<div class="kv"><span>SQL 映射</span><span>' + escapeHtml(timeRange.business_time_mapping || '') + '</span></div>' +
        '<div class="kv"><span>分区参数</span><span>' + escapeHtml((timeRange.partition_parameters || []).join(', ')) + '</span></div>' +
        '</div>');
      blocks.push('<div class="card"><h2>SQL 参数筛选</h2><div class="grid">' + (sqlFilters.length ? sqlFilters.map(ctrl => `
        <div class="card">
          <h3>${{escapeHtml(ctrl.label || ctrl.parameter)}}</h3>
          <div class="kv"><span>参数名</span><span>${{escapeHtml(ctrl.parameter || '')}}</span></div>
          <div class="kv"><span>默认值</span><span>${{escapeHtml(ctrl.default || '')}}</span></div>
          <div class="kv"><span>当前生效</span><span>${{ctrl.current_effect === 'active' ? '是' : '否'}}</span></div>
          <div class="kv"><span>用户可见</span><span>${{ctrl.visible_to_dashboard_user ? '是' : '否'}}</span></div>
          <div class="kv"><span>对外可见</span><span>${{ctrl.visible_to_external_user ? '是' : '否'}}</span></div>
          <div class="kv"><span>可选值</span><span>${{escapeHtml((ctrl.values || []).join(', '))}}</span></div>
          <div class="kv"><span>SQL 映射</span><span>${{escapeHtml(ctrl.sql_mapping || '')}}</span></div>
          <div class="kv"><span>筛选规则</span><span>${{escapeHtml(ctrl.filter_behavior || '')}}</span></div>
        </div>`).join('') : '<div class="empty">无 SQL 参数筛选</div>') + '</div></div>');
      blocks.push('<div class="card"><h2>DA 可筛选输出字段</h2><div class="grid">' + visible.map(ctrl => `
        <div class="card">
          <h3>${{escapeHtml(ctrl.label || ctrl.parameter)}}</h3>
          <div class="kv"><span>参数</span><span>${{escapeHtml(ctrl.parameter)}}</span></div>
          <div class="kv"><span>输出字段</span><span>${{escapeHtml(ctrl.output_field || '')}}</span></div>
          <div class="kv"><span>默认值</span><span>${{escapeHtml(ctrl.default || '')}}</span></div>
          <div class="kv"><span>当前生效</span><span>${{ctrl.current_effect === 'active' ? '是' : '否'}}</span></div>
          <div class="kv"><span>用户可见</span><span>${{ctrl.visible_to_dashboard_user ? '是' : '否'}}</span></div>
          <div class="kv"><span>对外可见</span><span>${{ctrl.visible_to_external_user ? '是' : '否'}}</span></div>
          <div class="kv"><span>可选值</span><span>${{escapeHtml((ctrl.values || []).join(', '))}}</span></div>
          <div class="kv"><span>筛选规则</span><span>${{escapeHtml(ctrl.filter_behavior || '')}}</span></div>
        </div>`).join('') + '</div></div>');
      blocks.push('<div class="card"><h2>固定 SQL 条件</h2>' + (fixed.length ? fixed.map(ctrl => `
        <div class="kv"><span>${{escapeHtml(ctrl.field || '')}}</span><span>${{escapeHtml(ctrl.condition || '')}}；DA可筛选：${{ctrl.da_filterable ? '是' : '否'}}；${{escapeHtml(ctrl.reason || '')}}</span></div>`).join('') : '<div class="empty">无固定 SQL 条件</div>') + '</div>');
      blocks.push('<div class="card"><h2>未来预留筛选</h2>' + (future.length ? future.map(ctrl => `
        <div class="kv"><span>${{escapeHtml(ctrl.label || ctrl.parameter)}}</span><span>当前未生效；输出字段：${{escapeHtml(ctrl.output_field || '')}}；激活条件：${{escapeHtml(ctrl.activation_requirement || '')}}</span></div>`).join('') : '<div class="empty">无未来预留筛选</div>') + '</div>');
      blocks.push('<div class="card"><h2>隐藏参数</h2>' + (hidden.length ? hidden.map(ctrl => `
        <div class="kv"><span>${{escapeHtml(ctrl.label || ctrl.name)}}</span><span>${{escapeHtml(ctrl.sql_usage || '')}}；来源：${{escapeHtml(ctrl.derive_from || '')}}</span></div>`).join('') : '<div class="empty">无隐藏参数</div>') + '</div>');
      return blocks.join('');
    }}
    function renderVisualReview(item) {{
      const visual = item.visual_review_contract || {{}};
      const rules = visual.field_display_rules || [];
      const checks = visual.review_checks || [];
      if (!Object.keys(visual).length) {{
        return '<div class="card"><h2>展示审查契约</h2><div class="empty">未声明字段展示转换；若存在占比/比例/率字段，校验会要求补充最小展示规则。</div></div>';
      }}
      const ruleRows = rules.length ? rules.map(rule => {{
        const sample = rule.sample_check || {{}};
        const sampleText = Object.keys(sample).length ? ('样例：' + escapeHtml(sample.raw_value ?? '') + ' -> ' + escapeHtml(sample.display_value ?? '')) : '';
        return `
          <tr>
            <td>${{escapeHtml(rule.output_field || '')}}</td>
            <td>${{escapeHtml(rule.semantic_type || '')}}</td>
            <td>${{escapeHtml(rule.source_value_scale || '')}}</td>
            <td>${{escapeHtml(rule.display_format || '')}}</td>
            <td>${{escapeHtml(rule.decimal_places ?? '')}}</td>
            <td>${{escapeHtml(rule.display_suffix || '')}}</td>
            <td>${{sampleText}}</td>
          </tr>`;
      }}).join('') : '<tr><td colspan="7" class="empty">没有字段展示规则</td></tr>';
      const checkList = checks.length ? '<ul>' + checks.map(check => '<li>' + escapeHtml(check) + '</li>').join('') + '</ul>' : '<div class="empty">没有展示审查点</div>';
      return `
        <div class="card">
          <h2>展示审查契约</h2>
          <div class="kv"><span>契约版本</span><span>${{escapeHtml(visual.contract_version || '')}}</span></div>
          <div class="kv"><span>职责边界</span><span>SQL 保留原始可计算数值；DA 按本契约做展示格式，不改变口径。</span></div>
          <div style="overflow:auto;">
            <table>
              <thead><tr><th>字段</th><th>语义</th><th>原始尺度</th><th>展示格式</th><th>小数位</th><th>后缀</th><th>样例</th></tr></thead>
              <tbody>${{ruleRows}}</tbody>
            </table>
          </div>
          ${{checks.length ? '<h3>人工审查点</h3>' + checkList : ''}}
        </div>`;
    }}
    function renderSample(item) {{
      const rows = item.sample || [];
      if (!rows.length) return '<div class="card"><h2>样例数据</h2><div class="empty">没有样例数据</div></div>';
      const columns = Object.keys(rows[0]);
      return '<div class="card"><h2>样例数据</h2><div class="sub">' + escapeHtml(item.sample_meta.note || '') + '</div><table><thead><tr>' +
        columns.map(col => '<th>' + escapeHtml(col) + '</th>').join('') + '</tr></thead><tbody>' +
        rows.map(row => '<tr>' + columns.map(col => '<td>' + escapeHtml(row[col]) + '</td>').join('') + '</tr>').join('') +
        '</tbody></table></div>';
    }}
    function renderDetail() {{
      const detail = document.getElementById('detail');
      const item = payload.items[selected];
      if (!item) {{
        detail.innerHTML = '<div class="empty">没有待 review 项。默认已跳过同 hash 已确认通过的看板 SQL。</div>';
        return;
      }}
      const problems = [...item.parse_errors, ...item.contract_errors];
      const warnings = item.contract_warnings || [];
      const project = (item.contracts || {{}}).project_context || {{}};
      const grouping = item.grouping_total_policy || {{}};
      const output = item.da_output_contract || {{}};
      const sqlOutput = item.sql_output_contract || {{}};
      const outputShape = sqlOutput.output_shape || {{}};
      const outputFieldList = (output.table_fields || []).map(field => '<span class="field-chip">' + escapeHtml(field) + '</span>').join('');
      const saved = state.items[item.state_key] || item.state || {{}};
      detail.innerHTML = `
        <div class="card">
          <h2>${{escapeHtml(item.title)}}</h2>
          <div class="kv"><span>SQL</span><span>${{escapeHtml(item.path)}}</span></div>
          <div class="kv"><span>验证状态</span><span>${{escapeHtml(item.verification_status || '')}}</span></div>
          <div class="kv"><span>样例来源</span><span>${{escapeHtml(item.sample_meta.path || item.sample_meta.type)}}</span></div>
          ${{labelStatus(statusOf(item))}}
        </div>
        ${{renderProvenance(item.generation_provenance)}}
        <div class="card">
          <h2>项目上下文</h2>
          <div class="kv"><span>项目</span><span>${{escapeHtml(project.display_name || project.project_id || '')}}</span></div>
          <div class="kv"><span>查询环境</span><span>${{escapeHtml(project.query_environment || '')}}</span></div>
          <div class="kv"><span>看板应用</span><span>${{escapeHtml(project.dashboard_application || '')}}</span></div>
          <div class="kv"><span>表名规则</span><span>${{escapeHtml(project.table_naming_profile || '')}}</span></div>
        </div>
        ${{renderDashboardSummary(item)}}
        <div class="card issues">
          <h2>契约检查</h2>
          ${{problems.length ? '<ul>' + problems.map(p => '<li class="bad">' + escapeHtml(p) + '</li>').join('') + '</ul>' : '<span class="badge ok">顶部契约可解析且通过强约束检查</span>'}}
          ${{warnings.length ? '<ul>' + warnings.map(p => '<li class="warn">' + escapeHtml(p) + '</li>').join('') + '</ul>' : ''}}
        </div>
        ${{renderControls(item)}}
        <div class="card">
          <h2>刷新与日期职责</h2>
          <div class="kv"><span>DA 判断实时刷新</span><span>${{item.refresh_contract?.da_decides_realtime_refresh ? '是' : '否'}}</span></div>
          <div class="kv"><span>DA 必填决策</span><span>${{escapeHtml((item.refresh_contract?.required_da_decisions || []).join(', '))}}</span></div>
          <div class="kv"><span>SQL 日期参数</span><span>${{escapeHtml((item.refresh_contract?.sql_date_range_parameters || []).join(', '))}}</span></div>
          <div class="kv"><span>SQL 输出形态</span><span>${{escapeHtml(outputShape.output_grain || outputShape.label || item.refresh_contract?.sql_output_shape_note || sqlOutput.output_grain || '以 SQL 输出为准')}}</span></div>
          <div class="sub">${{escapeHtml(item.refresh_contract?.note || 'DA 只管日期范围和是否实时刷新；SQL 输出形态以 SQL 正文和结果合同为准。')}}</div>
        </div>
        <div class="card">
          <h2>DA 输出边界</h2>
          <div class="kv"><span>结果形态</span><span>${{escapeHtml(output.result_shape || '')}}</span></div>
          <div class="kv"><span>输出字段</span><span class="field-list">${{outputFieldList || '<span class="sub">未声明</span>'}}</span></div>
          <div class="kv"><span>SQL 职责</span><span>${{escapeHtml(output.sql_responsibility || '')}}</span></div>
          <div class="kv"><span>DA 职责</span><span>${{escapeHtml(output.da_responsibility || '')}}</span></div>
          <div class="kv"><span>说明</span><span>${{escapeHtml(output.output_usage_note || '')}}</span></div>
        </div>
        ${{renderVisualReview(item)}}
        <div class="card">
          <h2>分组和合计</h2>
          <div class="kv"><span>分组字段</span><span>${{escapeHtml((grouping.grouping_fields || []).join(', '))}}</span></div>
          <div class="kv"><span>SQL 合计行</span><span>${{grouping.sql_contains_total_rows ? '是' : '否'}}</span></div>
          <div class="kv"><span>DA 生成合计</span><span>${{grouping.da_generate_total ? '是' : '否'}}</span></div>
          <div class="kv"><span>合计标签</span><span>${{escapeHtml(grouping.total_label || '')}}</span></div>
          <div class="kv"><span>合计范围</span><span>${{escapeHtml(grouping.total_scope || '')}}</span></div>
        </div>
        ${{renderSample(item)}}
        <div class="card">
          <h2>人工确认</h2>
          <textarea id="reviewNote" rows="3" placeholder="有问题时写原因；没问题也可以写备注。">${{escapeHtml(saved.note || '')}}</textarea>
          <div style="margin-top:10px; display:flex; gap:8px;">
            <button class="primary" onclick="setStatus('approved')">确认 SQL 没问题</button>
            <button class="danger" onclick="setStatus('rejected')">标记 SQL 有问题</button>
          </div>
        </div>`;
    }}
    function renderSummary() {{
      const values = Object.values(state.items || {{}});
      document.getElementById('passed').innerHTML = values.filter(v => v.status === 'approved').map(v => '<div class="badge ok">' + escapeHtml(v.path) + '</div>').join('') || '<span class="sub">暂无</span>';
      document.getElementById('failed').innerHTML = values.filter(v => v.status === 'rejected').map(v => '<div class="badge bad">' + escapeHtml(v.path) + (v.note ? '：' + escapeHtml(v.note) : '') + '</div>').join('') || '<span class="sub">暂无</span>';
    }}
    function render() {{
      document.getElementById('meta').textContent = `项目：${{payload.project_root}}；生成时间：${{payload.generated_at}}；待 review：${{payload.items.length}}；已跳过：${{payload.skipped_items.length}}；状态文件：${{payload.state_path}}`;
      renderList();
      renderDetail();
      renderSummary();
    }}
    document.getElementById('exportState').onclick = () => {{
      const blob = new Blob([JSON.stringify(state, null, 2)], {{type: 'application/json'}});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'dashboard_review_state.json';
      a.click();
      URL.revokeObjectURL(url);
    }};
    document.getElementById('clearLocal').onclick = () => {{
      localStorage.removeItem(storageKey());
      loadDashboardState();
      render();
    }};
    async function refreshPayload() {{
      if (!dashboardReviewApiUrl) return;
      document.getElementById('meta').textContent = '正在读取最新 dashboard_review 数据...';
      try {{
        const response = await fetch(dashboardReviewApiUrl);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        payload = await response.json();
        payload.items = payload.items || [];
        payload.skipped_items = payload.skipped_items || [];
        payload.state = payload.state || {{version: 1, items: {{}}}};
        if (selected >= payload.items.length) selected = 0;
        loadDashboardState();
        render();
      }} catch (err) {{
        document.getElementById('meta').textContent = '读取 dashboard_review 数据失败：' + err;
        document.getElementById('detail').innerHTML = '<div class="empty">无法读取最新 dashboard_review payload，请检查本地服务和项目 manifest。</div>';
      }}
    }}
    render();
    refreshPayload();
  </script>
</body>
</html>
"""


def build_html(root: Path, output: Path, state_path: Path, include_approved: bool, include_history: bool, sample_limit: int, json_output: Path | None = None) -> dict:
    payload = build_payload(root, state_path, include_approved, include_history, sample_limit)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_shell(payload), encoding="utf-8")
    json_output = json_output or root / DEFAULT_JSON_REL
    write_json(json_output, payload)
    if not state_path.exists():
        write_json(state_path, payload["state"])
    return payload


def cmd_build(args) -> None:
    root = Path(args.root).resolve()
    output = Path(args.output).resolve() if args.output else root / DEFAULT_HTML_REL
    json_output = Path(args.json_output).resolve() if getattr(args, "json_output", None) else root / DEFAULT_JSON_REL
    state_path = Path(args.state_file).resolve() if args.state_file else root / DEFAULT_STATE_REL
    payload = build_html(root, output, state_path, args.include_approved, args.include_history, args.sample_rows, json_output)
    print(f"dashboard_review_html: {output}")
    print(f"dashboard_review_json: {json_output}")
    print(f"dashboard_review_state: {state_path}")
    print(f"review_items: {len(payload['items'])}")
    print(f"skipped_approved: {len(payload['skipped_items'])}")


def cmd_mark(args) -> None:
    root = Path(args.root).resolve()
    state_path = Path(args.state_file).resolve() if args.state_file else root / DEFAULT_STATE_REL
    rel_path = normalize_rel(root, args.sql_path)
    sql_path = root / rel_path
    if not sql_path.exists():
        raise SystemExit(f"SQL file not found: {sql_path}")
    state = load_state(state_path)
    state["items"][rel_path] = {
        "status": args.status,
        "note": args.note or "",
        "sql_hash": sha256_text(sql_path.read_text(encoding="utf-8")),
        "reviewed_at": now_iso(),
        "path": rel_path,
        "title": args.title or rel_path,
    }
    state["updated_at"] = now_iso()
    write_json(state_path, state)
    print(f"marked {rel_path}: {args.status}")


class ReviewHandler(BaseHTTPRequestHandler):
    root: Path
    state_path: Path
    include_approved: bool
    include_history: bool
    sample_rows: int

    def send_text(self, status: int, content: str, content_type: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_text(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/dashboard-review":
            payload = build_payload(self.root, self.state_path, self.include_approved, self.include_history, self.sample_rows)
            self.send_json(200, payload)
            return
        if path == "/api/state":
            self.send_json(200, load_state(self.state_path))
            return
        if path in {"/", "/dashboard_review.html", "/dashboard-review"}:
            self.send_text(200, html_shell(None, api_url="/api/dashboard-review"), "text/html; charset=utf-8")
            return
        self.send_text(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/state":
            self.send_text(404, "not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        data = json.loads(body or "{}")
        data.setdefault("version", STATE_VERSION)
        data.setdefault("items", {})
        data["updated_at"] = now_iso()
        write_json(self.state_path, data)
        self.send_text(200, json.dumps({"ok": True}, ensure_ascii=False), "application/json; charset=utf-8")

    def log_message(self, format, *args):  # noqa: A002
        sys.stderr.write("dashboard_review: " + (format % args) + "\n")


def cmd_serve(args) -> None:
    root = Path(args.root).resolve()
    state_path = Path(args.state_file).resolve() if args.state_file else root / DEFAULT_STATE_REL
    state_path.parent.mkdir(parents=True, exist_ok=True)
    handler = type(
        "BoundReviewHandler",
        (ReviewHandler,),
        {
            "root": root,
            "state_path": state_path,
            "include_approved": args.include_approved,
            "include_history": args.include_history,
            "sample_rows": args.sample_rows,
        },
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"dashboard_review_url: http://{args.host}:{server.server_port}")
    print(f"dashboard_review_state: {state_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Generate a static dashboard review HTML file")
    build.add_argument("--root", required=True)
    build.add_argument("--output")
    build.add_argument("--json-output", help="Payload JSON path. Defaults to reviews/dashboard_review.json")
    build.add_argument("--state-file")
    build.add_argument("--include-approved", action="store_true")
    build.add_argument("--include-history", action="store_true")
    build.add_argument("--sample-rows", type=int, default=8)
    add_function_gate_arguments(
        build,
        selection_help="Optional explicit dashboard review function route, such as 【看板HTML审查】 or [DASHBOARD_REVIEW_HTML].",
    )
    build.set_defaults(func=cmd_build)

    serve = sub.add_parser("serve", help="Serve interactive review HTML and persist button clicks")
    serve.add_argument("--root", required=True)
    serve.add_argument("--state-file")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--include-approved", action="store_true")
    serve.add_argument("--include-history", action="store_true")
    serve.add_argument("--sample-rows", type=int, default=8)
    add_function_gate_arguments(
        serve,
        selection_help="Optional explicit dashboard review function route, such as 【看板HTML审查】 or [DASHBOARD_REVIEW_HTML].",
    )
    serve.set_defaults(func=cmd_serve)

    mark = sub.add_parser("mark", help="Mark one dashboard SQL as approved or rejected in the state file")
    mark.add_argument("--root", required=True)
    mark.add_argument("--sql-path", required=True)
    mark.add_argument("--status", choices=["approved", "rejected"], required=True)
    mark.add_argument("--note")
    mark.add_argument("--title")
    mark.add_argument("--state-file")
    add_function_gate_arguments(
        mark,
        selection_help="Optional explicit dashboard review function route, such as 【看板HTML审查】 or [DASHBOARD_REVIEW_HTML].",
    )
    mark.set_defaults(func=cmd_mark)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        purpose = "dashboard HTML review"
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("dashboard_review.py", args.command),
            purpose=purpose,
        )
        require_user_request(args.user_request, purpose=purpose)
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    args.func(args)


if __name__ == "__main__":
    main()
