#!/usr/bin/env python3
"""Shared helpers for SQL artifact sidecar specs and short SQL headers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced by callers that need legacy YAML
    yaml = None


SPEC_VERSION = "4.8"
HEADER_VERSION = "1"
SPEC_STORAGE = "sidecar_json"

FULL_SPEC_MARKERS = {
    "QUERY": ("@SQL_QUERY_SPEC", "@END_SQL_QUERY_SPEC"),
    "VALIDATION": ("@VALIDATION_SPEC", "@END_VALIDATION_SPEC"),
    "DASHBOARD": ("@DASHBOARD_SQL_SPEC", "@END_DASHBOARD_SQL_SPEC"),
}

HEADER_MARKERS = {
    "QUERY": ("@SQL_QUERY_HEADER", "@END_SQL_QUERY_HEADER"),
    "VALIDATION": ("@VALIDATION_HEADER", "@END_VALIDATION_HEADER"),
    "DASHBOARD": ("@DASHBOARD_SQL_HEADER", "@END_DASHBOARD_SQL_HEADER"),
}

HEADER_LINE_BUDGET = {
    "QUERY": 80,
    "VALIDATION": 60,
    "DASHBOARD": 60,
}


def version_stem(sql_path: Path) -> str:
    return sql_path.stem


def expected_spec_path(sql_path: Path) -> Path:
    return sql_path.with_name(f"{version_stem(sql_path)}.spec.json")


def read_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def write_json_object(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_rel(root: Path, path: Path | str) -> str:
    path_obj = Path(str(path).replace("\\", "/"))
    if path_obj.is_absolute():
        return path_obj.resolve().relative_to(root.resolve()).as_posix()
    return path_obj.as_posix()


def artifact_label(item: dict[str, Any]) -> str:
    version = item.get("version", "")
    version_text = f"v{int(version):03d}" if isinstance(version, int) else str(version or "unknown")
    return f"{item.get('kind', 'UNKNOWN')}/{item.get('slug', 'unknown')}/{version_text}"


def sidecar_rel_path(root: Path, sql_path: Path) -> str:
    return expected_spec_path(sql_path).relative_to(root).as_posix()


def spec_path_for_artifact(root: Path, artifact: dict[str, Any], sql_path: Path | None = None) -> Path:
    explicit = str(artifact.get("spec_path") or "").strip()
    if explicit:
        path = Path(explicit.replace("\\", "/"))
        return path if path.is_absolute() else root / path
    if sql_path is None:
        rel_sql = str(artifact.get("path") or "")
        sql_path = root / rel_sql
    return expected_spec_path(sql_path)


def load_sidecar_spec(root: Path, artifact: dict[str, Any], sql_path: Path | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    path = spec_path_for_artifact(root, artifact, sql_path)
    if not path.exists():
        return None, [f"Spec sidecar not found: {normalize_rel(root, path)}"]
    try:
        return read_json_object(path), []
    except Exception as exc:  # noqa: BLE001
        return None, [f"Spec sidecar parse failed: {normalize_rel(root, path)}: {exc}"]


def full_spec_marker_counts(sql_text: str) -> dict[str, tuple[int, int]]:
    counts: dict[str, tuple[int, int]] = {}
    for kind, (start, end) in FULL_SPEC_MARKERS.items():
        starts = len(re.findall(rf"^\s*(?:/\*\s*)?{re.escape(start)}\b", sql_text, flags=re.M))
        ends = len(re.findall(rf"^\s*(?:\*/\s*)?{re.escape(end)}\b", sql_text, flags=re.M))
        counts[kind] = (starts, ends)
    return counts


def has_full_spec_block(sql_text: str) -> bool:
    return any(starts or ends for starts, ends in full_spec_marker_counts(sql_text).values())


def header_marker_count(kind: str, sql_text: str) -> tuple[int, int]:
    start, end = HEADER_MARKERS[kind]
    starts = len(re.findall(rf"^\s*(?:/\*\s*)?{re.escape(start)}\b", sql_text, flags=re.M))
    ends = len(re.findall(rf"^\s*{re.escape(end)}\b", sql_text, flags=re.M))
    return starts, ends


def extract_header_block(kind: str, sql_text: str) -> tuple[str | None, list[str]]:
    start, end = HEADER_MARKERS[kind]
    pattern = re.compile(
        rf"^\s*/\*\s*{re.escape(start)}\b(?P<body>.*?){re.escape(end)}\s*\*/",
        flags=re.M | re.S,
    )
    match = pattern.search(sql_text)
    if not match:
        return None, [f"Missing short header {start}."]
    return match.group(0), []


def replace_or_prepend_short_header(kind: str, sql_text: str, header: str) -> str:
    start, end = HEADER_MARKERS[kind]
    pattern = re.compile(
        rf"^\s*/\*\s*{re.escape(start)}\b.*?{re.escape(end)}\s*\*/\s*",
        flags=re.M | re.S,
    )
    replaced, count = pattern.subn(header, sql_text, count=1)
    if count:
        return replaced
    return header + sql_text.lstrip("\r\n")


def strip_short_header(kind: str, sql_text: str) -> tuple[str, bool]:
    start, end = HEADER_MARKERS[kind]
    pattern = re.compile(
        rf"^\s*/\*\s*{re.escape(start)}\b.*?{re.escape(end)}\s*\*/\s*",
        flags=re.M | re.S,
    )
    stripped, count = pattern.subn("", sql_text, count=1)
    if count:
        return stripped.lstrip("\r\n"), True
    return sql_text, False


def strip_any_short_header(sql_text: str) -> tuple[str, str]:
    for kind in HEADER_MARKERS:
        stripped, changed = strip_short_header(kind, sql_text)
        if changed:
            return stripped, kind
    return sql_text, ""


def extract_legacy_yaml_spec(kind: str, sql_text: str) -> tuple[dict[str, Any] | None, list[str]]:
    if yaml is None:
        return None, ["PyYAML is required to read legacy inline SQL specs."]
    start, end = FULL_SPEC_MARKERS[kind]
    starts = [m.start() for m in re.finditer(rf"^\s*{re.escape(start)}\b", sql_text, flags=re.M)]
    ends = [m.start() for m in re.finditer(rf"^\s*{re.escape(end)}\b", sql_text, flags=re.M)]
    if len(starts) != 1 or len(ends) != 1:
        return None, [f"Expected exactly one legacy {start} block; start={len(starts)}, end={len(ends)}."]
    if ends[0] <= starts[0]:
        return None, [f"{end} must appear after {start}."]
    block = sql_text[starts[0] + len(start) : ends[0]].strip()
    try:
        loaded = yaml.safe_load(block) or {}
    except Exception as exc:  # noqa: BLE001
        return None, [f"Legacy YAML parse failed: {exc}"]
    if not isinstance(loaded, dict):
        return None, [f"Legacy {start} block must parse to an object."]
    return loaded, []


def strip_legacy_top_spec(kind: str, sql_text: str) -> tuple[str, list[str]]:
    start, end = FULL_SPEC_MARKERS[kind]
    # Normal artifact files put the legacy spec in the first block comment. Remove
    # the full comment when possible so the new short header owns the top.
    comment_pattern = re.compile(
        rf"^\s*/\*.*?{re.escape(start)}\b.*?{re.escape(end)}.*?\*/\s*",
        flags=re.S,
    )
    stripped, count = comment_pattern.subn("", sql_text, count=1)
    if count == 1:
        return stripped.lstrip("\r\n"), []
    block_pattern = re.compile(
        rf"^\s*{re.escape(start)}\b.*?{re.escape(end)}\s*",
        flags=re.M | re.S,
    )
    stripped, count = block_pattern.subn("", sql_text, count=1)
    if count == 1:
        return stripped.lstrip("\r\n"), []
    return sql_text, [f"Could not strip legacy {start} block."]


def set_spec_version(spec: dict[str, Any]) -> None:
    meta = spec.setdefault("spec_meta", {})
    if isinstance(meta, dict):
        meta["spec_version"] = SPEC_VERSION
        meta["spec_storage"] = SPEC_STORAGE


def scalar(value: Any, default: str = "未声明") -> str:
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        return text if text else default
    return default


def labels_from_items(items: Any, keys: tuple[str, ...] = ("label", "field", "metric", "name")) -> list[str]:
    if not isinstance(items, list):
        return []
    labels: list[str] = []
    for item in items:
        label = ""
        if isinstance(item, dict):
            for key in keys:
                if item.get(key):
                    label = str(item[key])
                    break
        elif isinstance(item, str):
            label = item
        if label and label not in labels:
            labels.append(label)
    return labels


def csv_line(items: list[str], empty: str = "无", limit: int = 8) -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    if not values:
        return empty
    shown = values[:limit]
    suffix = f" 等{len(values)}项" if len(values) > limit else ""
    return "、".join(shown) + suffix


def text_from_mapping(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def filter_labels(spec: dict[str, Any]) -> list[str]:
    contract = spec.get("da_filter_contract") or {}
    labels: list[str] = []
    for item in (contract.get("sql_parameter_filters") or []) + (contract.get("filterable_fields") or []):
        if not isinstance(item, dict):
            continue
        if item.get("current_effect") != "active" or item.get("visible_to_dashboard_user") is not True:
            continue
        label = str(item.get("label") or item.get("output_field") or item.get("parameter") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def statistical_period(spec: dict[str, Any]) -> str:
    intent = spec.get("dashboard_intent") or {}
    output_shape = (spec.get("sql_output_contract") or {}).get("output_shape") or {}
    if not isinstance(output_shape, dict):
        output_shape = {}
    mode = str(output_shape.get("result_mode") or output_shape.get("mode") or intent.get("result_mode") or "")
    grain = str(output_shape.get("time_grain") or intent.get("time_grain") or "").lower()
    if mode == "daily_plus_total_table":
        return "按日 + 合计"
    if mode == "period_total_table" or grain == "none":
        return "区间合计"
    if mode == "sql_declared_table" or grain == "sql_declared":
        return "SQL 声明"
    if mode == "daily_table" or grain == "day":
        return "按日"
    if mode == "hourly_table" or grain == "hour":
        return "按小时"
    if grain == "week":
        return "按周"
    if grain == "month":
        return "按月"
    if mode == "retention_table":
        return "留存周期"
    if mode == "funnel_table":
        return "漏斗步骤"
    if mode == "detail_table":
        return "明细"
    return mode or grain or "未声明"


def query_header(root: Path, artifact: dict[str, Any], spec: dict[str, Any], spec_rel: str) -> str:
    intent = spec.get("query_intent") or {}
    logic = spec.get("query_logic") or {}
    output = spec.get("query_output_contract") or {}
    project = spec.get("project_context") or artifact.get("project_context") or {}
    metrics = labels_from_items(logic.get("metric_definitions"), ("metric", "label", "field", "name"))
    if not metrics:
        metrics = list(artifact.get("metrics") or [])
    filters = []
    for key in ("filter_rules", "filters"):
        value = logic.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    text = text_from_mapping(item, ("description", "condition", "field", "label", "name"))
                else:
                    text = scalar(item, "")
                if text:
                    filters.append(text)
    lines = [
        "/* @SQL_QUERY_HEADER",
        f"header_version: {HEADER_VERSION}",
        f"spec_path: {spec_rel}",
        f"artifact: {artifact_label(artifact)}",
        f"project: {scalar(project.get('project_id') or project.get('display_name'))}",
        f"title: {scalar(intent.get('title') or artifact.get('title'))}",
        f"business_question: {scalar(intent.get('description') or artifact.get('natural_language_intent'))}",
        f"base: {scalar(logic.get('business_context') or output.get('one_row_means'))}",
        f"metrics: {csv_line(metrics)}",
        f"key_filters: {csv_line(filters)}",
        f"time_range: {scalar(logic.get('time_range'), '见 spec')}",
        f"output_grain: {scalar(output.get('output_grain') or artifact.get('grain'))}",
        f"result_usage: {scalar(intent.get('default_usage'))}",
        f"verification_status: {scalar(artifact.get('verification_status'), 'not_applicable')}",
        "@END_SQL_QUERY_HEADER */",
        "",
    ]
    return "\n".join(lines)


def validation_header(root: Path, artifact: dict[str, Any], spec: dict[str, Any], spec_rel: str) -> str:
    intent = spec.get("validation_intent") or {}
    evidence = spec.get("user_run_evidence") or {}
    promotion = spec.get("promotion") or {}
    lines = [
        "/* @VALIDATION_HEADER",
        f"header_version: {HEADER_VERSION}",
        f"spec_path: {spec_rel}",
        f"artifact: {artifact_label(artifact)}",
        f"title: {scalar(intent.get('title') or artifact.get('title'))}",
        f"purpose: {scalar(intent.get('description'))}",
        f"evidence_status: {scalar(evidence.get('status'))}",
        f"promotion_decision: {scalar(promotion.get('decision'))}",
        f"confidence_score: {scalar((spec.get('confidence_assessment') or {}).get('confidence_score'))}",
        "@END_VALIDATION_HEADER */",
        "",
    ]
    return "\n".join(lines)


def dashboard_header(root: Path, artifact: dict[str, Any], spec: dict[str, Any], spec_rel: str) -> str:
    project = spec.get("project_context") or artifact.get("project_context") or {}
    validation = spec.get("validation_reference") or {}
    delivery = spec.get("da_delivery_contract") or {}
    totals = delivery.get("grouping_total_policy") or {}
    visual = spec.get("visual_review_contract") or {}
    display_rules = labels_from_items(visual.get("field_display_rules") or [], ("output_field", "label"))
    sql_filters = labels_from_items((spec.get("da_filter_contract") or {}).get("sql_parameter_filters") or [], ("label", "parameter"))
    da_filters = labels_from_items((spec.get("da_filter_contract") or {}).get("filterable_fields") or [], ("label", "output_field", "parameter"))
    lines = [
        "/* @DASHBOARD_SQL_HEADER",
        f"header_version: {HEADER_VERSION}",
        f"spec_path: {spec_rel}",
        f"artifact: {artifact_label(artifact)}",
        f"project: {scalar(project.get('project_id') or project.get('display_name'))}",
        f"dashboard_application: {scalar(project.get('dashboard_application'))}",
        f"source_query: {scalar((spec.get('business_logic') or {}).get('source_query_logic_reference') or artifact.get('linked_query'))}",
        f"指标：{csv_line(labels_from_items(spec.get('metrics') or []))}",
        f"维度：{csv_line(labels_from_items(spec.get('dimensions') or []))}",
        f"筛选项：{csv_line(filter_labels(spec))}",
        f"统计周期：{statistical_period(spec)}",
        f"time_parameters: {csv_line(((spec.get('da_filter_contract') or {}).get('time_range') or {}).get('parameter_names') or [])}",
        f"sql_parameter_filters: {csv_line(sql_filters)}",
        f"da_filterable_fields: {csv_line(da_filters)}",
        f"display_rules: {csv_line(display_rules)}",
        f"total_policy: sql_total={scalar(totals.get('sql_contains_total_rows'))}, da_total={scalar(totals.get('da_generate_total'))}",
        f"verification_status: {scalar(validation.get('verification_status') or artifact.get('verification_status'))}",
        f"logic_changed: {scalar((spec.get('business_logic') or {}).get('business_logic_changed'), 'false')}",
        "@END_DASHBOARD_SQL_HEADER */",
        "",
    ]
    return "\n".join(lines)


def build_short_header(root: Path, artifact: dict[str, Any], spec: dict[str, Any], spec_rel: str) -> str:
    kind = str(artifact.get("kind") or "")
    if kind == "QUERY":
        return query_header(root, artifact, spec, spec_rel)
    if kind == "VALIDATION":
        return validation_header(root, artifact, spec, spec_rel)
    if kind == "DASHBOARD":
        return dashboard_header(root, artifact, spec, spec_rel)
    raise ValueError(f"Unsupported artifact kind: {kind}")
