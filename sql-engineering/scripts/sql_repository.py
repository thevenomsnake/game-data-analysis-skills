#!/usr/bin/env python3
"""Build one read-only viewer entry per Formal Asset Package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from repository_snapshot import persisted_repository_snapshot  # noqa: E402
from rule_store import RuleStore  # noqa: E402
from urllib.parse import urlparse

from dashboard_review import (
    DEFAULT_STATE_REL as DASHBOARD_REVIEW_STATE_REL,
    build_payload as build_dashboard_review_payload,
    dashboard_summary,
    html_shell as dashboard_review_html_shell,
    load_state as load_dashboard_review_state,
    read_csv_sample,
    read_xlsx_sample,
    sample_rows as dashboard_sample_rows,
)
from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from capability_registry import command_function_ids
from asset_provenance import provenance_from_sources
from sql_output_contract import normalize_field_name
from spec_utils import load_sidecar_spec, spec_path_for_artifact


PAYLOAD_VERSION = "sql_repository_v2"
PACKAGE_SCHEMA_VERSION = "formal_asset_package_v1"
FORMAL_ASSET_ROOT = Path("formal_assets")
PACKAGE_MANIFEST_NAME = "manifest.json"
DEFAULT_HTML_REL = "reviews/sql_repository.html"
DEFAULT_JSON_REL = "reviews/sql_repository.json"
DEFAULT_DASHBOARD_REVIEW_REL = "reviews/dashboard_review.html"
SOURCE_TITLE_PREFIX_RE = re.compile(r"^(?:\s*\d{1,4}\s*[\.\、．,，\)）\]】]\s*)+")
SOURCE_EXTENSION_RE = re.compile(r"\.(?:sql|csv|txt|xlsx)\s*$", re.I)
PHYSICAL_TLOG_RE = re.compile(r"_dsl_([a-z0-9]+)_fht0\b", re.I)
SHARED_LOG_TEXT_GATED_CONCEPTS = {
    "battlesrvid-mode-attribution",
    "game-total-active-duration",
    "game-total-active-duration-minute-buckets",
}
QUERY_SQL_ROLES = {
    "formal_query",
    "formal_query_unverified",
    "formal_query_sql",
    "query_sql",
    "historical_query_sql",
}
QUERY_SPEC_ROLES = {"formal_query_spec", "query_spec"}
QUERY_META_ROLES = {"formal_query_meta", "query_meta"}
DASHBOARD_SQL_ROLES = {"dashboard_delivery", "dashboard_delivery_sql", "dashboard_sql"}
DASHBOARD_SPEC_ROLES = {"dashboard_delivery_spec", "dashboard_spec"}
DASHBOARD_META_ROLES = {"dashboard_delivery_meta", "dashboard_meta"}
VALIDATION_SQL_ROLES = {"validation", "validation_sql"}
VALIDATION_SPEC_ROLES = {"validation_spec"}
VALIDATION_META_ROLES = {"validation_meta"}
EVIDENCE_ROLES = {
    "result_evidence",
    "historical_result_evidence",
    "run_record",
    "run_evidence",
}
DERIVED_OUTPUT_ROLES = {
    "analysis_workbook",
    "comparison_workbook",
    "visualization",
    "export",
    "derived_output",
    "historical_derived_output",
    "registered_output",
    "legacy_quarantine_evidence",
    "other",
}

TOPIC_LABELS = {
    "new_user": "新增用户",
    "retention": "留存回流",
    "funnel": "漏斗转化",
    "distribution": "分布分桶",
    "conversion": "转化率/占比",
    "duration": "时长",
    "battle_behavior": "战斗行为",
    "economy": "经济/道具",
    "quality_check": "质量监控",
    "ab_compare": "AB/包体对比",
    "ops_health": "运营健康",
    "uncategorized": "未分类",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_text_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compact_text(value: Any, limit: int = 480) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).replace("\r", "\n").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def compact_multiline_text(value: Any, limit: int = 2400) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in text.split("\n")]
    cleaned: list[str] = []
    blank = 0
    for line in lines:
        if not line.strip():
            blank += 1
            if blank <= 1:
                cleaned.append("")
            continue
        blank = 0
        cleaned.append(line)
    text = "\n".join(cleaned).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def unique(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def unique_structured(values: list[Any]) -> list[Any]:
    """Deduplicate JSON-compatible values without flattening object rows."""

    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        if value in (None, ""):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def merge_description_text(*values: Any, limit: int = 520) -> str:
    parts: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        parts.extend(part.strip() for part in re.split(r"[；;]\s*", text) if part.strip())
    generic = {"该 SQL 的统计对象或输出行含义。", "该 SQL 的核心计算/归因逻辑。", "该 SQL 的指标共享这个去重或聚合规则。"}
    if any(part not in generic for part in parts):
        parts = [part for part in parts if part not in generic]
    return compact_text("；".join(unique(parts)), limit)


def lower_compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def extract_int_values(value: Any) -> set[str]:
    return set(re.findall(r"(?<!\d)(\d{1,6})(?!\d)", str(value or "")))


def extract_sql_condition_values(value: Any, field_names: tuple[str, ...]) -> set[str]:
    text = str(value or "")
    values: set[str] = set()
    field_alt = "|".join(re.escape(field) for field in field_names)
    for match in re.finditer(rf"(?:{field_alt})\s*(?:=|in\s*\()\s*([0-9,\s]+)\)?", text, flags=re.I):
        values.update(re.findall(r"\d{1,6}", match.group(1)))
    return values or extract_int_values(text)


def labels_from_items(items: Any, keys: tuple[str, ...] = ("label", "field", "metric", "name", "title")) -> list[str]:
    labels: list[str] = []
    for item in as_list(items):
        if isinstance(item, dict):
            for key in keys:
                if item.get(key) not in (None, ""):
                    labels.append(str(item[key]))
                    break
        elif item not in (None, ""):
            labels.append(str(item))
    return unique(labels)


def clean_source_title(value: Any) -> str:
    return SOURCE_EXTENSION_RE.sub("", str(value or "").strip()).strip()


def strip_source_prefix(value: Any) -> str:
    original = clean_source_title(value)
    text = original
    while text:
        next_text = SOURCE_TITLE_PREFIX_RE.sub("", text).strip()
        if next_text == text:
            break
        text = next_text
    return text or original


def normalize_rel(root: Path, path: Path | str) -> str:
    path_obj = Path(str(path).replace("\\", "/"))
    if path_obj.is_absolute():
        try:
            return path_obj.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return path_obj.as_posix()
    return path_obj.as_posix()


def package_manifest_rows(
    root: Path,
    *,
    include_history: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Load only current package manifests, never receipt snapshots or old stores."""

    root = root.resolve()
    formal_root = root / FORMAL_ASSET_ROOT
    packages: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    if not formal_root.is_dir():
        return packages, issues
    for manifest_path in sorted(formal_root.glob(f"*/{PACKAGE_MANIFEST_NAME}")):
        relative = normalize_rel(root, manifest_path)
        try:
            manifest = read_json(manifest_path, {})
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            issues.append({"code": "package_manifest_unreadable", "path": relative, "message": str(exc)})
            continue
        if not isinstance(manifest, dict) or manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
            issues.append(
                {
                    "code": "package_manifest_schema",
                    "path": relative,
                    "message": f"expected {PACKAGE_SCHEMA_VERSION}",
                }
            )
            continue
        lifecycle_state = str(manifest.get("lifecycle_state") or "").lower()
        if not include_history and lifecycle_state != "current":
            continue
        package_root = manifest_path.parent.resolve()
        declared_directory = str(manifest.get("directory") or "")
        actual_directory = package_root.relative_to(root).as_posix()
        if declared_directory != actual_directory:
            issues.append(
                {
                    "code": "package_directory_mismatch",
                    "path": relative,
                    "message": f"declared {declared_directory!r}, actual {actual_directory!r}",
                }
            )
            continue
        packages.append(
            {
                "manifest": manifest,
                "manifest_path": relative,
                "package_root": package_root,
            }
        )
    return packages, issues


def package_member_path(
    root: Path,
    package_root: Path,
    member: dict[str, Any],
) -> Path | None:
    relative = Path(str(member.get("path") or "").replace("\\", "/"))
    if not relative.as_posix() or relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to((package_root / "members").resolve())
    except ValueError:
        return None
    return candidate


def package_member_key(member: dict[str, Any]) -> str:
    path = Path(str(member.get("path") or "").replace("\\", "/"))
    name = path.name
    for suffix in (".spec.json", ".meta.json", ".sql", ".json"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return (path.parent / name).as_posix()


def package_current_member_ids(manifest: dict[str, Any]) -> set[str]:
    current = manifest.get("current") if isinstance(manifest.get("current"), dict) else {}
    values = {str(item) for item in as_list(current.get("member_ids")) if str(item)}
    by_role = current.get("by_role") if isinstance(current.get("by_role"), dict) else {}
    for member_ids in by_role.values():
        values.update(str(item) for item in as_list(member_ids) if str(item))
    return values


def member_document(root: Path, package_root: Path, member: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(member, dict):
        return {}
    path = package_member_path(root, package_root, member)
    if path is None or path.suffix.lower() != ".json" or not path.is_file():
        return {}
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def companion_member(
    sql_member: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    key = package_member_key(sql_member)
    exact = [item for item in candidates if package_member_key(item) == key]
    if len(exact) == 1:
        return exact[0]
    return candidates[0] if len(candidates) == 1 else None


def normalized_package_member(
    root: Path,
    package_root: Path,
    member: dict[str, Any],
    *,
    current_ids: set[str],
) -> dict[str, Any]:
    path = package_member_path(root, package_root, member)
    relative = normalize_rel(root, path) if path is not None else str(member.get("path") or "")
    return {
        "member_id": str(member.get("member_id") or ""),
        "role": str(member.get("role") or ""),
        "lifecycle_state": str(member.get("lifecycle_state") or ""),
        "is_current": str(member.get("member_id") or "") in current_ids,
        "path": relative,
        "available": bool(path and path.is_file()),
        "sha256": str(member.get("sha256") or ""),
        "size_bytes": int(member.get("size_bytes") or 0),
        "created_at": str(member.get("created_at") or ""),
    }


def read_manifest(root: Path) -> dict[str, Any]:
    return read_json(root / "manifest.json", {})


def is_current_artifact(item: dict[str, Any]) -> bool:
    state = str(item.get("artifact_state") or "").lower()
    status = str(item.get("status") or "").lower()
    if state:
        return state == "current"
    return status not in {"history", "superseded", "deprecated", "archived"}


def project_artifacts(manifest: dict[str, Any], kind: str, include_history: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in manifest.get("artifacts", []):
        if not isinstance(item, dict) or str(item.get("kind") or "").upper() != kind.upper():
            continue
        if include_history or is_current_artifact(item):
            rows.append(item)
    return rows


def load_xml_log_catalog(root: Path) -> dict[str, dict[str, str]]:
    data = read_json(root / "sources" / "xml_catalog.json", {})
    catalog: dict[str, dict[str, str]] = {}
    for item in as_list(data.get("logs") if isinstance(data, dict) else []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        desc = str(item.get("desc") or "").strip()
        chinese = desc.split("。", 1)[0].split(".", 1)[0].strip()
        display = f"{name}【{chinese}】" if chinese else name
        catalog[name.lower()] = {"name": name, "desc": desc, "display": display}
    return catalog


def physical_table_log_token(value: Any) -> str:
    match = PHYSICAL_TLOG_RE.search(str(value or ""))
    return match.group(1).lower() if match else ""


def source_log_display(value: Any, catalog: dict[str, dict[str, str]]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    raw = text.split("/", 1)[0].split("【", 1)[0].strip()
    candidates = [raw, text]
    token = physical_table_log_token(text)
    if token:
        candidates.insert(0, token)
    for candidate in candidates:
        key = str(candidate or "").strip().lower()
        if key in catalog:
            return catalog[key]["display"]
    return ""


def source_logs_from_spec(spec: dict[str, Any], artifact: dict[str, Any], catalog: dict[str, dict[str, str]]) -> list[str]:
    logs: list[str] = []
    for src in as_list(spec.get("data_sources")):
        if not isinstance(src, dict):
            continue
        for key in ("log_name", "table", "physical_table"):
            display = source_log_display(src.get(key), catalog)
            if display:
                logs.append(display)
    for table in as_list(artifact.get("tables")):
        display = source_log_display(table, catalog)
        if display:
            logs.append(display)
    return unique(logs)


def technical_sources_from_spec(spec: dict[str, Any], artifact: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    for src in as_list(spec.get("data_sources")):
        if not isinstance(src, dict):
            continue
        log_name = src.get("log_name")
        physical = src.get("physical_table") or src.get("table")
        if log_name and physical and str(log_name) != str(physical):
            sources.append(f"{log_name} / {physical}")
        elif log_name or physical:
            sources.append(str(log_name or physical))
    sources.extend(str(item) for item in as_list(artifact.get("tables")))
    return unique(sources)


def latest_run_for_artifact(manifest: dict[str, Any], artifact: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    artifact_path = str(artifact.get("path") or "")
    linked_run = str(artifact.get("linked_run") or "")
    validation_ref = spec.get("validation_reference") if isinstance(spec.get("validation_reference"), dict) else {}
    ref_run = str(validation_ref.get("user_run_evidence") or "")
    candidates: list[dict[str, Any]] = []
    for run in manifest.get("run_evidence", []):
        if not isinstance(run, dict):
            continue
        if run.get("source_artifact") == artifact_path or run.get("sql_path") == artifact_path:
            candidates.append(run)
        elif linked_run and run.get("path") == linked_run:
            candidates.append(run)
        elif ref_run and run.get("path") == ref_run:
            candidates.append(run)
    if not candidates:
        return None
    return sorted(candidates, key=lambda row: str(row.get("created_at") or row.get("run_id") or ""), reverse=True)[0]


def run_evidence_summary(root: Path, run: dict[str, Any] | None) -> dict[str, Any]:
    if not run:
        return {
            "status": "missing",
            "path": "",
            "evidence_file": "",
            "evidence_file_exists": False,
            "row_count": None,
            "summary": "",
            "issues": "",
            "user_confirmed": False,
            "columns": [],
            "schema_fingerprint": "",
        }
    evidence_file = str(run.get("evidence_file") or "")
    retained = retained_result_contract(run)
    return {
        "status": str(run.get("status") or ""),
        "path": str(run.get("path") or ""),
        "evidence_file": evidence_file,
        "evidence_file_exists": bool(evidence_file and (root / evidence_file).exists()),
        "row_count": run.get("row_count"),
        "summary": compact_text(run.get("result_summary"), 520),
        "issues": compact_text(run.get("issues"), 360),
        "user_confirmed": bool(run.get("user_confirmed")),
        "columns": retained.get("columns") or run.get("result_columns") or run.get("sample_fields") or [],
        "schema_fingerprint": retained.get("schema_fingerprint") or run.get("result_schema_fingerprint") or "",
        "retained_fields_override": run.get("retained_fields_override") or retained.get("retained_fields_override") or {},
    }


def retained_result_contract(run: dict[str, Any] | None) -> dict[str, Any]:
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


def query_expected_fields(spec: dict[str, Any] | None) -> list[str]:
    if not isinstance(spec, dict):
        return []
    output = spec.get("query_output_contract") if isinstance(spec.get("query_output_contract"), dict) else {}
    fields = output.get("expected_fields")
    if isinstance(fields, list):
        return [str(item or "").strip() for item in fields if str(item or "").strip()]
    output_fields = spec.get("output_fields")
    if isinstance(output_fields, list):
        return [str(item.get("field") or item.get("label") or "").strip() for item in output_fields if isinstance(item, dict) and str(item.get("field") or item.get("label") or "").strip()]
    return []


def query_display_rules(spec: dict[str, Any] | None, run: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    expected_norms = {normalize_field_name(field) for field in query_expected_fields(spec)}
    rules: list[Any] = []
    if isinstance(spec, dict):
        output = spec.get("query_output_contract") if isinstance(spec.get("query_output_contract"), dict) else {}
        spec_rules = output.get("field_display_rules")
        if isinstance(spec_rules, list):
            rules = spec_rules
    if not rules:
        retained = retained_result_contract(run)
        run_rules = retained.get("ratio_field_rules")
        if isinstance(run_rules, list):
            rules = run_rules
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        field = str(rule.get("output_field") or "").strip()
        if not field:
            continue
        field_norm = normalize_field_name(field)
        if expected_norms and field_norm not in expected_norms:
            continue
        if field_norm in seen:
            continue
        seen.add(field_norm)
        rows.append(rule)
    return rows


def filter_sample_rows(rows: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    if not fields:
        return rows
    return [
        {field: row.get(field, "") for field in fields}
        for row in rows
        if isinstance(row, dict)
    ]


def sample_for_evidence(root: Path, run: dict[str, Any] | None, limit: int, expected_fields: list[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not run:
        return [], {"type": "none", "path": "", "note": "没有保存结果文件。"}
    retained = retained_result_contract(run)
    expected = [str(item or "").strip() for item in (expected_fields or []) if str(item or "").strip()]
    contract_fields = [str(item or "").strip() for item in retained.get("columns", []) if str(item or "").strip()]
    fields = expected or contract_fields
    retained_rows = [row for row in retained.get("sample_rows", []) if isinstance(row, dict)]
    if retained_rows:
        return filter_sample_rows(retained_rows[:limit], fields), {
            "type": "retained_contract",
            "path": str(run.get("evidence_file") or ""),
            "note": "使用固化时写入的保留字段样例；原始结果文件仅作为审计证据。",
            "columns": fields or contract_fields,
        }
    evidence_file = str(run.get("evidence_file") or "")
    if not evidence_file:
        return [], {"type": "none", "path": "", "note": "跑数证据没有结果文件。"}
    path = root / evidence_file
    if not path.exists():
        return [], {"type": "missing", "path": evidence_file, "note": "结果文件路径不存在。"}
    try:
        if path.suffix.lower() == ".csv":
            rows = filter_sample_rows(read_csv_sample(path, limit), fields)
            return rows, {"type": "actual", "path": evidence_file, "note": "使用已保存 CSV 结果样例，并按正式输出字段契约过滤。", "columns": fields}
        if path.suffix.lower() == ".xlsx":
            rows = filter_sample_rows(read_xlsx_sample(path, limit), fields)
            return rows, {"type": "actual", "path": evidence_file, "note": "使用已保存 XLSX 结果样例，并按正式输出字段契约过滤。", "columns": fields}
        return [], {"type": "unsupported", "path": evidence_file, "note": "结果文件格式不支持预览。"}
    except Exception as exc:  # noqa: BLE001
        return [], {"type": "read_error", "path": evidence_file, "note": f"结果文件读取失败：{exc}"}


def query_metrics(spec: dict[str, Any], artifact: dict[str, Any]) -> list[str]:
    logic = spec.get("query_logic") if isinstance(spec.get("query_logic"), dict) else {}
    names: list[str] = []
    names.extend(labels_from_items(spec.get("metrics")))
    names.extend(labels_from_items(artifact.get("metrics")))
    for item in as_list(logic.get("metric_definitions")):
        if isinstance(item, dict):
            names.extend(str(key) for key in item.keys())
        elif item:
            names.append(str(item))
    return unique(names)


def query_dimensions(spec: dict[str, Any], artifact: dict[str, Any]) -> list[str]:
    return unique(labels_from_items(spec.get("dimensions")) + labels_from_items(artifact.get("dimensions")))


def output_fields(spec: dict[str, Any], artifact: dict[str, Any]) -> list[str]:
    fields = labels_from_items(spec.get("output_fields"), keys=("field", "label", "name"))
    contract = spec.get("query_output_contract") if isinstance(spec.get("query_output_contract"), dict) else {}
    fields.extend(labels_from_items(contract.get("expected_fields")))
    fields.extend(labels_from_items(artifact.get("dimensions")))
    fields.extend(labels_from_items(artifact.get("metrics")))
    return unique(fields)


def output_field_lookup(spec: dict[str, Any]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for item in as_list(spec.get("output_fields")):
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or item.get("name") or "").strip()
        label = str(item.get("label") or "").strip()
        purpose = str(item.get("purpose") or item.get("business_meaning") or "").strip()
        if not (field or label):
            continue
        meta = {
            "field": field,
            "label": label,
            "purpose": purpose,
            "source": str(item.get("source") or "").strip(),
        }
        for key in unique([field, label]):
            lookup[key.lower()] = meta
    return lookup


def load_canonical_rule_index(root: Path) -> dict[str, dict[str, Any]]:
    store = RuleStore(root)
    entries = list(store.load_index().get("entries", []) or [])
    status_rank = {"confirmed": 5, "proposed": 4, "superseded": 2, "deprecated": 1}
    entries = sorted(
        entries,
        key=lambda item: (
            status_rank.get(str(item.get("status") or "").lower(), 0),
            int(item.get("rule_version") or 0),
        ),
        reverse=True,
    )
    by_rule_id: dict[str, dict[str, Any]] = {}
    by_concept_key: dict[str, dict[str, Any]] = {}
    for entry in entries:
        rule_id = str(entry.get("rule_id") or "").strip()
        concept_key = str(entry.get("concept_key") or "").strip()
        if rule_id and rule_id not in by_rule_id:
            by_rule_id[rule_id] = entry
        if concept_key and concept_key not in by_concept_key:
            by_concept_key[concept_key] = entry
    return {
        "store": store,
        "by_rule_id": by_rule_id,
        "by_concept_key": by_concept_key,
        "record_cache": {},
    }


def load_indexed_rule(rule_index: dict[str, Any], entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    path = str(entry.get("path") or "")
    cache = rule_index.setdefault("record_cache", {})
    if path not in cache:
        store = rule_index.get("store")
        if not isinstance(store, RuleStore):
            return None
        rows = store.load_candidate_records([entry])
        cache[path] = rows[0] if rows else None
    return cache.get(path)


def lookup_rule(rule_index: dict[str, Any], rule_id: str = "", concept_key: str = "", title: str = "") -> dict[str, Any] | None:
    if concept_key and concept_key in rule_index.get("by_concept_key", {}):
        concept_rule = load_indexed_rule(rule_index, rule_index["by_concept_key"][concept_key])
        if not concept_rule:
            return None
        if not rule_id or str(concept_rule.get("rule_id") or "") == rule_id:
            return concept_rule
        # concept_key is the stable business identity; rule_id is version lineage.
        return concept_rule
    if rule_id and rule_id in rule_index.get("by_rule_id", {}):
        rule = load_indexed_rule(rule_index, rule_index["by_rule_id"][rule_id])
        if not rule:
            return None
        if not concept_key or str(rule.get("concept_key") or "") == concept_key:
            return rule
    return None


def rules_selected_for_sql(rule_index: dict[str, Any], sql_text: str) -> list[dict[str, Any]]:
    if not sql_text.strip():
        return []
    store = rule_index.get("store")
    if not isinstance(store, RuleStore):
        return []
    from sql_project import scan_intent_source  # noqa: PLC0415

    observed = scan_intent_source(sql_text)
    candidates = store.select_candidates(
        {
            "candidate_sql_observed": {
                "source_logs": observed.get("source_logs", []),
                "source_fields": observed.get("source_fields", []),
                "domains": observed.get("domains", []),
            }
        },
        query_text="",
        statuses=("confirmed", "proposed"),
    )
    return [
        rule
        for entry in candidates
        if (rule := load_indexed_rule(rule_index, entry)) is not None
    ]


def rule_result_from_text(text: str) -> str:
    normalized = str(text or "")
    lower = normalized.lower()
    if any(token in normalized for token in ("冲突", "不符合", "缺少", "多出")) or any(token in lower for token in ("conflict", "fail", "blocked")):
        return "conflict"
    if any(token in normalized for token in ("证据不足", "需要", "待确认", "仍需")) or "manual" in lower:
        return "needs_manual_check"
    if any(token in normalized for token in ("通过", "符合", "命中")) or any(token in lower for token in ("matched", "pass", "ok", "constraint")):
        return "matched"
    return "mentioned"


DIAGNOSTIC_RULE_SOURCE_MARKERS = (
    "candidate_rules",
    "rejected_rules",
    "rejected_weak_rules",
    "hard_constraints",
    "reverse_source_audit",
    "source_metric_audit",
    "name_logic_mismatch",
    "candidate_sql_check.diagnostics",
)

DIAGNOSTIC_RULE_TEXT_MARKERS = (
    "reverse_source_audit",
    "source_metric_audit",
    "name_logic_mismatch",
    "partial reverse",
    "weak reverse",
    "source log overlap",
    "source_log overlap",
    "shared source",
    "shared log",
    "diagnostic",
)


def normalize_rule_check(value: Any, rule_index: dict[str, dict[str, Any]], source: str = "") -> dict[str, Any] | None:
    if isinstance(value, dict):
        rule_id = str(value.get("rule_id") or value.get("source_rule_id") or "").strip()
        concept_key = str(value.get("concept_key") or "").strip()
        title = str(value.get("title") or value.get("rule_title") or "").strip()
        rule = lookup_rule(rule_index, rule_id, concept_key, title)
        message = (
            value.get("message")
            or value.get("detail")
            or value.get("activation_reason")
            or value.get("reason")
            or value.get("expected")
            or value.get("evidence")
            or ""
        )
        if not (rule_id or concept_key or title or rule or message):
            return None
        result = str(value.get("result") or value.get("check_result") or "").strip()
        if not result and rule and source.startswith("canonical_rule_context.") and any(
            token in source for token in ("applied_rules", "active_rules")
        ):
            result = "matched"
        if not result:
            result = rule_result_from_text(str(message))
        if result == "constraint":
            result = "matched"
        current_summary = rule_product_summary(rule or {})
        current_display = rule_product_display(rule or {})
        return {
            "rule_id": rule_id or str((rule or {}).get("rule_id") or ""),
            "concept_key": concept_key or str((rule or {}).get("concept_key") or ""),
            "title": title or str((rule or {}).get("title") or "已保存口径"),
            "status": str(value.get("status") or (rule or {}).get("status") or ""),
            "result": result,
            "message": compact_text(message, 360),
            "evidence": compact_text(value.get("evidence") or value.get("source") or source, 260),
            "rule_summary": compact_text(current_summary or value.get("rule_summary"), 520),
            "rule_display": compact_multiline_text(current_display or value.get("rule_display") or value.get("rule_summary"), 1600),
            "full_rule": compact_multiline_text((rule or {}).get("content") or value.get("rule_summary") or "", 2400),
            "source": source,
        }
    text = compact_text(value, 520)
    if not text:
        return None
    rule = None
    rule_id = ""
    concept_key = ""
    for candidate in re.findall(r"`([^`]+)`", text):
        if candidate in rule_index.get("by_rule_id", {}):
            rule_id = candidate
            rule = lookup_rule(rule_index, rule_id=candidate)
            break
        if candidate in rule_index.get("by_concept_key", {}):
            concept_key = candidate
            rule = lookup_rule(rule_index, concept_key=candidate)
            break
    if not rule and not any(token in text for token in ("口径", "rule", "规则", "GameMode", "iZoneAreaID", "TotalActiveDuration")):
        return None
    return {
        "rule_id": rule_id or str((rule or {}).get("rule_id") or ""),
        "concept_key": concept_key or str((rule or {}).get("concept_key") or ""),
        "title": str((rule or {}).get("title") or "已保存口径线索"),
        "status": str((rule or {}).get("status") or ""),
        "result": rule_result_from_text(text),
        "message": text,
        "evidence": source,
        "rule_summary": compact_text(rule_product_summary(rule or {}), 520),
        "rule_display": compact_multiline_text(rule_product_display(rule or {}), 1600),
        "full_rule": compact_multiline_text((rule or {}).get("content") or "", 2400),
        "source": source,
    }


def rule_product_display(rule: dict[str, Any]) -> str:
    """Product-facing saved-rule brief.

    Full rule prose often contains historical corrections and forbidden
    substitutions. Those are useful audit evidence, but the repository's default
    detail should show the current applied fact first.
    """
    if not rule:
        return ""
    structured = rule.get("structured_definition") if isinstance(rule.get("structured_definition"), dict) else {}
    lines: list[str] = []
    current_fact = str(structured.get("current_fact") or "").strip()
    if current_fact:
        lines.append("当前事实：" + current_fact)

    source_contract = structured.get("source_log_contract") if isinstance(structured.get("source_log_contract"), dict) else {}
    external_contract = structured.get("external_source_contract") if isinstance(structured.get("external_source_contract"), dict) else {}
    physical_table = str(external_contract.get("physical_table") or "").strip()
    if physical_table:
        lines.append("权威表：" + physical_table)
    availability = str(external_contract.get("availability_status") or "").strip()
    if availability:
        lines.append("可用性：" + availability)
    default_platform = str(external_contract.get("default_platform_filter") or "").strip()
    if default_platform:
        lines.append("默认筛选：" + default_platform)
    source_log = str(source_contract.get("source_log") or "").strip()
    if source_log:
        lines.append("来源日志：" + source_log)
    event_filters = [str(item).strip() for item in as_list(source_contract.get("event_filters")) if str(item).strip()]
    if event_filters:
        lines.append("事件条件：" + "；".join(event_filters))
    formula = str(
        source_contract.get("quantity_formula")
        or source_contract.get("formula")
        or source_contract.get("metric_formula")
        or ""
    ).strip()
    if formula:
        lines.append("计算公式：" + formula)

    boundaries: list[str] = []
    for item in as_list(structured.get("metric_boundaries")):
        if not isinstance(item, dict):
            continue
        metric_name = str(item.get("metric_name") or "").strip()
        note = str(item.get("note") or item.get("formula") or "").strip()
        if metric_name and note:
            boundaries.append(f"{metric_name}：{note}")
    if boundaries:
        lines.append("适用边界：" + "；".join(boundaries[:4]))

    if lines:
        return "\n".join(lines)
    return compact_multiline_text(rule.get("content"), 1200)


def rule_product_summary(rule: dict[str, Any]) -> str:
    """One-line current-fact summary for product-facing JSON/search text."""
    display = rule_product_display(rule)
    if not display:
        return ""
    return compact_text(display, 520)


def rule_identity(rule: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(rule.get("rule_id") or "").strip(),
        str(rule.get("concept_key") or "").strip(),
        str(rule.get("title") or "").strip(),
    )


def normalize_rule_result(value: Any) -> str:
    result = str(value or "").strip().lower()
    if result in {"constraint", "pass", "passed", "ok"}:
        return "matched"
    if result in {"warn", "warning", "manual", "mentioned"}:
        return "needs_manual_check" if result != "mentioned" else "mentioned"
    if result in {"fail", "failed", "blocked"}:
        return "conflict"
    return result


def rule_check_is_diagnostic(row: dict[str, Any]) -> bool:
    source_text = " ".join(
        str(row.get(key) or "")
        for key in ("source", "evidence", "message", "reason")
    ).lower()
    return any(marker in source_text for marker in DIAGNOSTIC_RULE_SOURCE_MARKERS + DIAGNOSTIC_RULE_TEXT_MARKERS)


def rule_check_is_product_facing(row: dict[str, Any]) -> bool:
    """True when a saved-rule row may appear as "本 SQL 使用的口径"."""
    result = normalize_rule_result(row.get("result"))
    status = str(row.get("status") or "").lower()
    if status in {"superseded", "deprecated", "history", "archived"}:
        return False
    if result in {"", "mentioned", "partial", "weak", "not_relevant", "diagnostic"}:
        return False
    if rule_check_is_diagnostic(row):
        return False
    if result in {"matched", "conflict", "needs_manual_check"}:
        return True
    return False


def rule_result_priority(value: Any) -> int:
    result = normalize_rule_result(value)
    if result == "conflict":
        return 40
    if result == "needs_manual_check":
        return 30
    if result == "matched":
        return 20
    if result == "mentioned":
        return 10
    return 0


def append_rule_check(
    rows: list[dict[str, Any]],
    value: Any,
    rule_index: dict[str, dict[str, Any]],
    source: str,
    *,
    skip_weak_existing: bool = False,
) -> None:
    normalized = normalize_rule_check(value, rule_index, source)
    if not normalized:
        return
    normalized["result"] = normalize_rule_result(normalized.get("result")) or "mentioned"
    identity = rule_identity(normalized)
    if skip_weak_existing and normalized["result"] == "mentioned":
        return
    for idx, row in enumerate(rows):
        if rule_identity(row) != identity:
            continue
        if normalized.get("message") and normalized.get("message") != row.get("message"):
            merged_messages = unique([row.get("message"), normalized.get("message")])
            row["message"] = compact_text("；".join(merged_messages), 520)
        if rule_result_priority(normalized.get("result")) > rule_result_priority(row.get("result")):
            rows[idx] = {**row, **normalized, "message": row.get("message") or normalized.get("message") or ""}
        return
    rows.append(normalized)


def query_filters(spec: dict[str, Any]) -> list[str]:
    logic = spec.get("query_logic") if isinstance(spec.get("query_logic"), dict) else {}
    filters: list[str] = []
    for key in ("filters", "filter_rules", "exclusion_rules"):
        for item in as_list(logic.get(key)):
            if item:
                filters.append(compact_text(item, 220))
    if logic.get("bucket_rule"):
        filters.append("分桶规则：" + compact_text(logic.get("bucket_rule"), 260))
    for param in as_list(spec.get("parameters")):
        if not isinstance(param, dict):
            continue
        role = str(param.get("parameter_role") or "")
        if role not in {"sql_filter", "time_range"}:
            continue
        label = param.get("label") or param.get("name")
        default = param.get("default")
        usage = param.get("sql_usage") or ""
        if default not in (None, ""):
            filters.append(compact_text(f"{label}={default}；{usage}", 220))
        elif usage:
            filters.append(compact_text(f"{label}；{usage}", 220))
    return unique(filters)


def metric_definitions(spec: dict[str, Any], artifact: dict[str, Any]) -> list[dict[str, Any]]:
    logic = spec.get("query_logic") if isinstance(spec.get("query_logic"), dict) else {}
    perf = spec.get("performance_level") if isinstance(spec.get("performance_level"), dict) else {}
    metadata = perf.get("metric_metadata_locked") if isinstance(perf.get("metric_metadata_locked"), dict) else {}
    field_lookup = output_field_lookup(spec)
    filters = query_filters(spec)
    dimensions = query_dimensions(spec, artifact)
    dedup = logic.get("dedup_grain") or metadata.get("dedup_key") or ""
    rows: list[dict[str, Any]] = []
    for item in as_list(logic.get("metric_definitions")):
        if isinstance(item, dict):
            for name, meaning in item.items():
                meta = field_lookup.get(str(name).lower(), {})
                label = meta.get("label") or str(name)
                purpose = compact_text(meaning or meta.get("purpose") or label, 360)
                rows.append(
                    {
                        "name": label,
                        "field": str(name),
                        "business_meaning": purpose,
                        "numerator": metadata.get("numerator") if "率" in str(name) or "占比" in str(name) else "",
                        "denominator": metadata.get("denominator") if "率" in str(name) or "占比" in str(name) else "",
                        "dedup_key": dedup,
                        "aggregation_dimensions": dimensions,
                        "key_conditions": filters,
                    }
                )
        elif item:
            meta = field_lookup.get(str(item).lower(), {})
            label = meta.get("label") or str(item)
            rows.append(
                {
                    "name": label,
                    "field": str(item),
                    "business_meaning": meta.get("purpose") or label,
                    "numerator": "",
                    "denominator": "",
                    "dedup_key": dedup,
                    "aggregation_dimensions": dimensions,
                    "key_conditions": filters,
                }
            )
    known_names = {row["name"] for row in rows}
    for name in query_metrics(spec, artifact):
        meta = field_lookup.get(str(name).lower(), {})
        label = meta.get("label") or name
        if label not in known_names and name not in known_names:
            rows.append(
                {
                    "name": label,
                    "field": name,
                    "business_meaning": meta.get("purpose") or label,
                    "numerator": "",
                    "denominator": "",
                    "dedup_key": dedup,
                    "aggregation_dimensions": dimensions,
                    "key_conditions": filters,
                }
            )
    return rows


def normalize_metric_item(value: Any, field_lookup: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    field_lookup = field_lookup or {}
    if isinstance(value, dict):
        item = dict(value)
        raw_name = str(item.get("name") or item.get("field") or item.get("label") or "").strip()
        field = str(item.get("field") or raw_name).strip()
        meta = field_lookup.get(raw_name.lower()) or field_lookup.get(field.lower()) or {}
        label = str(item.get("label") or meta.get("label") or "").strip()
        if label and (not item.get("name") or str(item.get("name")) == field):
            item["name"] = label
            item["field"] = field
        meaning = str(item.get("business_meaning") or "").strip()
        if not meaning or meaning in {raw_name, field}:
            item["business_meaning"] = meta.get("purpose") or item.get("name") or field
        return item
    return {
        "name": str(value or "").strip(),
        "field": str(value or "").strip(),
        "business_meaning": str(value or "").strip(),
        "numerator": "",
        "denominator": "",
        "dedup_key": "",
        "aggregation_dimensions": [],
        "key_conditions": [],
    }


def condition_role(value: Any) -> str:
    text = str(value or "")
    lower = text.lower()
    if "tdbank_imp_date" in lower or "pt_start" in lower or "pt_end" in lower:
        return "execution_filter"
    if (
        " is not null" in lower
        or " is null" in lower
        or "<> ''" in lower
        or "!= ''" in lower
        or "非空" in text
        or "为空" in text
        or "清洗" in text
        or "trim" in lower
        or "unix_timestamp" in lower
        or "无法解析" in text
    ):
        return "data_quality"
    if "dteventtime" in lower or "dteventdate" in lower or "ts_start" in lower or "ts_end" in lower:
        return "time_scope"
    core_tokens = (
        "izoneareaid",
        "gamesvrid",
        "gamemode",
        "matchmode",
        "battlesrvid",
        "matchsuccess",
        "territory",
        "itemid",
        "道具",
        "模式",
        "分桶",
        "桶",
    )
    if any(token in lower for token in core_tokens):
        return "business_filter"
    return "business_filter"


def metric_group_key(metric: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    return (
        compact_text(metric.get("dedup_key"), 240),
        tuple(unique([str(item) for item in as_list(metric.get("aggregation_dimensions"))])),
        tuple(unique([str(item) for item in as_list(metric.get("key_conditions"))])),
    )


def metric_group_title(names: list[str]) -> str:
    joined = " ".join(names).lower()
    if any(token in joined for token in ("duration", "耗时", "时长", "p50", "p90", "p95")):
        return "同一统计口径下的耗时/分位指标"
    if any(token in joined for token in ("rate", "ratio", "占比", "率")):
        return "同一统计口径下的比例/占比指标"
    if any(token in joined for token in ("cnt", "count", "人数", "次数", "数量")):
        return "同一统计口径下的计数指标"
    return f"同一统计口径下的 {len(names)} 个指标"


def metric_groups(metrics: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, tuple[str, ...], tuple[str, ...]], list[dict[str, Any]]] = {}
    order: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for raw in metrics:
        metric = normalize_metric_item(raw)
        name = str(metric.get("name") or metric.get("label") or "").strip()
        if not name:
            continue
        key = metric_group_key(metric)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(metric)

    rows: list[dict[str, Any]] = []
    for key in order:
        items = grouped[key]
        names = unique([str(item.get("name") or item.get("label") or "") for item in items])
        metric_notes: list[str] = []
        ratio_notes: list[str] = []
        shared_filters = list(key[2])
        visible_filters = [item for item in shared_filters if condition_role(item) == "business_filter"]
        quality_filters = [item for item in shared_filters if condition_role(item) == "data_quality"]
        for item in items:
            name = str(item.get("name") or item.get("label") or "").strip()
            meaning = str(item.get("business_meaning") or "").strip()
            field = str(item.get("field") or "").strip()
            numerator = str(item.get("numerator") or "").strip()
            denominator = str(item.get("denominator") or "").strip()
            if meaning and meaning != name:
                prefix = f"{name}（{field}）" if field and field != name else name
                metric_notes.append(f"{prefix}：{meaning}")
            if numerator or denominator:
                ratio_notes.append(f"{name}：分子={numerator or '无'}；分母={denominator or '无'}")
        rows.append(
            {
                "title": metric_group_title(names),
                "metrics": names,
                "shared_dedup_key": key[0],
                "shared_dimensions": list(key[1]),
                "shared_filters": visible_filters,
                "quality_filters": quality_filters,
                "metric_notes": unique(metric_notes),
                "ratio_notes": unique(ratio_notes),
            }
        )
    return rows


def normalize_repository_metrics(metrics: list[Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    lookup = output_field_lookup(spec)
    rows: list[dict[str, Any]] = []
    for item in metrics:
        metric = normalize_metric_item(item, lookup)
        if metric.get("name"):
            rows.append(metric)
    return rows


def rule_check_allowed_by_source_logs(rule_check: dict[str, Any], rule_index: dict[str, dict[str, Any]], source_logs: list[Any] | None) -> bool:
    if source_logs is None:
        return True
    rule = lookup_rule(
        rule_index,
        str(rule_check.get("rule_id") or ""),
        str(rule_check.get("concept_key") or ""),
        str(rule_check.get("title") or ""),
    )
    if not rule:
        return True
    return rule_source_gate(rule, source_log_tokens(source_logs))


def canonical_rule_checks(
    spec: dict[str, Any],
    rule_index: dict[str, dict[str, Any]],
    source_logs: list[Any] | None = None,
    sql_text: str = "",
) -> list[dict[str, Any]]:
    context = spec.get("canonical_rule_context") if isinstance(spec.get("canonical_rule_context"), dict) else {}
    rows: list[dict[str, Any]] = []
    for key in ("applied_rules", "active_rules"):
        for rule in as_list(context.get(key)):
            append_rule_check(rows, rule, rule_index, f"canonical_rule_context.{key}")
    # hard_constraints are field/log/aggregation requirements expanded from a
    # rule. They are useful code evidence, but they are not independent business
    # criteria for the repository's product-facing "used口径" card.
    business_checks = spec.get("business_rule_checks")
    if isinstance(business_checks, dict):
        for check in as_list(business_checks.get("items")) + as_list(business_checks.get("checks")):
            append_rule_check(rows, check, rule_index, "business_rule_checks")
    else:
        for check in as_list(business_checks):
            append_rule_check(rows, check, rule_index, "business_rule_checks")
    existing = spec.get("repository_summary") if isinstance(spec.get("repository_summary"), dict) else {}
    for check in as_list(existing.get("canonical_rule_checks")):
        append_rule_check(
            rows,
            check,
            rule_index,
            "repository_summary.canonical_rule_checks",
            skip_weak_existing=True,
        )
    if sql_text and source_logs:
        for rule in rules_selected_for_sql(rule_index, sql_text):
            if not isinstance(rule, dict):
                continue
            event_check = event_signature_rule_check_from_sql(rule, sql_text, source_logs)
            if event_check:
                append_rule_check(rows, event_check, rule_index, "sql_event_signature")
    if any(normalize_rule_result(row.get("result")) == "matched" and row.get("source") == "sql_event_signature" for row in rows):
        rows = [
            row
            for row in rows
            if not (
                row.get("source") == "sql_event_signature"
                and normalize_rule_result(row.get("result")) == "conflict"
            )
        ]
    rows = [
        row
        for row in rows
        if rule_check_is_product_facing(row)
        and rule_check_allowed_by_source_logs(row, rule_index, source_logs)
    ]
    return rows[:24]


def canonical_rule_status(rule_checks: list[dict[str, Any]]) -> str:
    if not rule_checks:
        return "unique"
    results = {normalize_rule_result(item.get("result")) for item in rule_checks}
    if "conflict" in results:
        return "conflict"
    if "needs_manual_check" in results:
        return "needs_manual_check"
    if "matched" in results:
        return "matched"
    if "mentioned" in results:
        return "needs_manual_check"
    return "needs_manual_check"


def criterion_status_from_rule_result(result: Any) -> str:
    normalized = normalize_rule_result(result)
    if normalized == "conflict":
        return "conflict"
    if normalized == "matched":
        return "matched"
    if normalized in {"needs_manual_check", "mentioned"}:
        return "needs_manual_check"
    return "unique"


def criterion_key(value: dict[str, Any]) -> tuple[str, str, str]:
    name = str(value.get("name") or "")
    normalized_name = lower_compact(re.sub(r"[。；;,.，、\s]+", " ", name))
    mode_values = extract_sql_condition_values(name, ("GameMode", "gameModeID", "MatchMode", "mode_id", "模式"))
    if mode_values and any(token in normalized_name for token in ("gamemode", "matchmode", "模式")):
        normalized_name = "gamemode=" + ",".join(sorted(mode_values))
    zone_values = extract_sql_condition_values(name, ("iZoneAreaID", "zone_id", "zone_area_id", "区服"))
    if zone_values and any(token in normalized_name for token in ("izoneareaid", "zone_id", "zoneareaid", "区服")):
        normalized_name = "izoneareaid=" + ",".join(sorted(zone_values))
    if "tdbank_imp_date" in normalized_name:
        normalized_name = "tdbank_imp_date-range"
    elif any(token in normalized_name for token in ("dteventtime", "dteventdate", "ts_start", "ts_end")):
        normalized_name = "event-time-range"
    category = str(value.get("category") or "").strip()
    if normalized_name.startswith(("gamemode=", "izoneareaid=")):
        category = "核心业务筛选"
    return (
        category,
        normalized_name,
        "",
    )


def criterion_merge_priority(value: dict[str, Any]) -> tuple[int, int]:
    status = str(value.get("saved_rule_status") or "unique").lower()
    status_priority = {"conflict": 40, "matched": 30, "needs_manual_check": 20, "unique": 10}.get(status, 0)
    concept = str(value.get("concept_key") or "").lower()
    concept_priority = {
        "game-mode-map": 50,
        "battle-gamemode-experience-flags": 45,
        "game-mode-name-type-boundary": 42,
        "first-day-battle-progress-tag": 35,
        "izoneareaid-default": 45,
        "battlesrvid-mode-attribution": 40,
    }.get(concept, 0)
    return status_priority, concept_priority


def source_log_tokens(source_logs: list[Any]) -> set[str]:
    tokens: set[str] = set()
    for item in source_logs:
        raw = str(item or "").split("【", 1)[0].strip().lower()
        if raw:
            tokens.add(raw)
    return tokens


def log_tokens_from_value(value: Any) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    candidates = [text]
    candidates.extend(part.strip() for part in re.split(r"[/,，、|]", text) if part.strip())
    physical = physical_table_log_token(text)
    if physical:
        candidates.append(physical)
    tokens: set[str] = set()
    for candidate in candidates:
        raw = str(candidate or "").split("【", 1)[0].strip().strip("`\"[]").lower()
        if raw:
            tokens.add(raw)
    return tokens


def rule_declared_source_log_tokens(rule: dict[str, Any]) -> set[str]:
    activation = rule.get("activation_contract") if isinstance(rule.get("activation_contract"), dict) else {}
    source_signature = activation.get("source_signature") if isinstance(activation.get("source_signature"), dict) else {}
    event_signature = activation.get("event_signature") if isinstance(activation.get("event_signature"), dict) else {}
    structured = rule.get("structured_definition") if isinstance(rule.get("structured_definition"), dict) else {}
    source_log_contract = structured.get("source_log_contract") if isinstance(structured.get("source_log_contract"), dict) else {}
    values: list[Any] = []
    values.extend(as_list(activation.get("source_logs")))
    values.extend(as_list(source_signature.get("source_logs") or source_signature.get("logs")))
    values.extend(as_list(event_signature.get("required_logs")))
    if event_signature.get("required_log"):
        values.append(event_signature.get("required_log"))
    for constraint in as_list(activation.get("hard_constraints")):
        if not isinstance(constraint, dict):
            continue
        if constraint.get("type") in {"must_use_log", "must_use_field", "do_not_substitute_log"}:
            values.extend([constraint.get("log"), constraint.get("expected_log")])
    values.extend([source_log_contract.get("source_log"), source_log_contract.get("physical_table")])
    tokens: set[str] = set()
    for value in values:
        tokens.update(log_tokens_from_value(value))
    return tokens


def rule_requires_structured_shared_log_evidence(rule: dict[str, Any]) -> bool:
    """Return true when a saved rule needs SQL event-signature evidence.

    Text snippets such as "BattleSrvId 补模式" or "TotalActiveDuration MAX 分桶"
    are useful repository prose, but they are not enough to prove that a
    shared-log metric rule is actually applied.
    """
    concept = str(rule.get("concept_key") or "").lower()
    if concept in SHARED_LOG_TEXT_GATED_CONCEPTS:
        return True
    try:
        from sql_project import REVERSE_AUDIT_SHARED_LOGS  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        shared_logs = {"battleitem", "battleloginout", "damage"}
    else:
        shared_logs = {str(item or "").lower() for item in REVERSE_AUDIT_SHARED_LOGS}
    return bool(rule_declared_source_log_tokens(rule) & shared_logs)


def rule_full_text(rule: dict[str, Any]) -> str:
    activation = rule.get("activation_contract") if isinstance(rule.get("activation_contract"), dict) else {}
    return " ".join(
        [
            str(rule.get("rule_id") or ""),
            str(rule.get("concept_key") or ""),
            str(rule.get("title") or ""),
            str(rule.get("content") or ""),
            str(rule.get("applies_to") or ""),
            json.dumps(activation, ensure_ascii=False, sort_keys=True) if activation else "",
        ]
    )


def rule_expected_values(rule: dict[str, Any], field: str) -> set[str]:
    text = rule_full_text(rule)
    values: set[str] = set()
    field_re = re.escape(field)
    for match in re.finditer(field_re + r"[^0-9]{0,80}(\d{1,6})", text, flags=re.I):
        values.add(match.group(1))
    if not values and field.lower() == "izoneareaid":
        for match in re.finditer(r"固定为\s*(\d{1,6})", text, flags=re.I):
            values.add(match.group(1))
    return values


def rule_gamemode_values(rule: dict[str, Any]) -> set[str]:
    concept = str(rule.get("concept_key") or "").lower()
    text = rule_full_text(rule)
    values: set[str] = set()
    if concept == "game-mode-map":
        for line in str(rule.get("content") or "").splitlines():
            match = re.match(r"\|\s*(\d{1,6})\s*\|", line.strip())
            if match:
                values.add(match.group(1))
    for match in re.finditer(r"gamemode\s+in\s*\(([^)]*)\)", text, flags=re.I):
        values.update(re.findall(r"\d{1,6}", match.group(1)))
    for match in re.finditer(r"gamemode\s*=\s*(\d{1,6})", text, flags=re.I):
        values.add(match.group(1))
    return values


def gamemode_label(rule: dict[str, Any], mode_id: str) -> str:
    if str(rule.get("concept_key") or "").lower() != "game-mode-map":
        return ""
    for line in str(rule.get("content") or "").splitlines():
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) >= 3 and parts[0] == mode_id:
            return f"{parts[1]} / {parts[2]}"
    return ""


def criterion_gamemode_values(criterion_text: str, rule: dict[str, Any]) -> set[str]:
    values = extract_sql_condition_values(criterion_text, ("GameMode", "gameModeID", "MatchMode", "mode_id", "模式"))
    if values:
        return values
    text = str(criterion_text)
    rule_text = rule_full_text(rule)
    for line in str(rule.get("content") or "").splitlines():
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) >= 3 and parts[0].isdigit() and ((parts[1] and parts[1] in text) or (parts[2] and parts[2] in text)):
            values.add(parts[0])
    if "生存训练" in text and "生存训练" in rule_text:
        values.add("6")
    return values


def is_direct_gamemode_rule(rule: dict[str, Any], criterion_text: str) -> bool:
    concept = str(rule.get("concept_key") or "").lower()
    text = lower_compact(criterion_text)
    if concept in {"game-mode-map", "battle-gamemode-experience-flags", "game-mode-name-type-boundary"}:
        if concept == "game-mode-name-type-boundary":
            return any(token in text for token in ("mode_name", "mode_category", "mode_type", "模式名称", "模式类型", "模式大类"))
        return True
    if concept == "first-day-battle-progress-tag":
        return any(token in text for token in ("has_newbie", "has_normal", "has_fast", "首日", "进度标签"))
    return False


def rule_source_gate(rule: dict[str, Any], logs: set[str]) -> bool:
    declared_logs = rule_declared_source_log_tokens(rule)
    if not declared_logs:
        return True
    concept = str(rule.get("concept_key") or "").lower()
    if concept == "new-user-window-cohort" and logs & {"playerregister", "playerlogin"}:
        return True
    if not logs:
        return False
    return bool(logs & declared_logs)


def event_signature_rule_check_from_sql(rule: dict[str, Any], sql_text: str, source_logs: list[Any]) -> dict[str, Any] | None:
    from sql_project import (  # noqa: PLC0415
        REVERSE_AUDIT_SHARED_LOGS,
        contract_event_signature,
        event_signature_match,
        extract_sql_evidence,
    )

    activation = rule.get("activation_contract") if isinstance(rule.get("activation_contract"), dict) else {}
    signature = contract_event_signature(activation)
    if not signature:
        return None
    if not rule_source_gate(rule, source_log_tokens(source_logs)):
        return None

    evidence = extract_sql_evidence(sql_text)
    observed_logs = source_log_tokens(source_logs) | {str(item or "").lower() for item in evidence.get("source_logs", [])}
    required_logs = {str(item or "").lower() for item in signature.get("required_logs", [])}
    shared_log_match = bool((observed_logs & required_logs) & set(REVERSE_AUDIT_SHARED_LOGS))
    match = event_signature_match(signature, evidence, shared_log_match=shared_log_match)

    incompatible_hits = [
        str(item.get("value") or "")
        for item in match.get("missing_evidence", []) or []
        if item.get("type") in {"incompatible_predicate_present", "incompatible_metric_role_present"}
    ]
    matched_types = {str(item.get("type") or "") for item in match.get("matched_evidence", []) or []}
    missing_types = {str(item.get("type") or "") for item in match.get("missing_evidence", []) or []}
    role_core_ok = not (
        signature.get("required_metric_roles") or signature.get("required_any_metric_roles")
    ) or "metric_role" in matched_types
    aggregation_core_ok = not (
        signature.get("required_aggregations") or signature.get("required_any_aggregations")
    ) or "aggregation" in matched_types
    predicate_core_ok = not signature.get("required_predicate_signatures") or "predicate" in matched_types
    field_role_core_ok = not signature.get("required_field_roles") or "field_role" in matched_types

    if incompatible_hits and role_core_ok and aggregation_core_ok and predicate_core_ok and field_role_core_ok:
        return rule_check_from_saved_rule(
            rule,
            "conflict",
            f"SQL 出现禁用条件 {'、'.join(incompatible_hits)}，与保存口径《{rule.get('title') or rule.get('rule_id')}》冲突。",
            "sql_event_signature",
        )
    if match.get("strength") == "exact" and not incompatible_hits and not any(
        item in missing_types
        for item in (
            "required_log",
            "required_predicate",
            "required_metric_role",
            "required_any_metric_role",
            "required_aggregation",
            "required_any_aggregation",
            "required_field_role",
            "required_text_term",
        )
    ):
        return rule_check_from_saved_rule(
            rule,
            "matched",
            f"SQL 的事件条件、指标角色和聚合方式命中保存口径《{rule.get('title') or rule.get('rule_id')}》。",
            "sql_event_signature",
        )
    return None


def rule_match_result_for_text(rule: dict[str, Any], criterion_text: str, source_logs: list[Any]) -> tuple[str, str] | None:
    text = lower_compact(criterion_text)
    full = lower_compact(rule_full_text(rule))
    logs = source_log_tokens(source_logs)
    concept = str(rule.get("concept_key") or "").lower()
    title = str(rule.get("title") or "")
    status = str(rule.get("status") or "").lower()

    def relation(result: str, reason: str) -> tuple[str, str]:
        if status and status != "confirmed" and result == "matched":
            return "needs_manual_check", f"命中保存的 {status} 口径，需确认后才能作为强约束：{reason}"
        return result, reason

    is_izonearea_default_rule = (
        "izoneareaid-default" in concept
        or ("izoneareaid" in lower_compact(title) and "默认" in lower_compact(title))
    )
    if "izoneareaid" in text and "izoneareaid" in full and is_izonearea_default_rule:
        sql_values = extract_int_values(criterion_text)
        expected = rule_expected_values(rule, "iZoneAreaID")
        if sql_values and expected:
            if sql_values & expected:
                return relation("matched", f"SQL 使用 iZoneAreaID={', '.join(sorted(sql_values & expected))}，与保存口径《{title}》一致。")
            return relation("conflict", f"SQL 使用 iZoneAreaID={', '.join(sorted(sql_values))}，保存口径期望 {', '.join(sorted(expected))}。")
        return relation("needs_manual_check", f"SQL 使用 iZoneAreaID 条件，关联到保存口径《{title}》，但无法自动比对具体值。")

    mode_tokens = ("gamemode", "matchmode", "模式映射", "模式id")
    mode_rule = concept in {"game-mode-map", "battle-gamemode-experience-flags", "first-day-battle-progress-tag", "game-mode-name-type-boundary"}
    has_duration_formula_evidence = any(token in text for token in ("clientmatchclicktime", "matchbegintime", "matchduration", "客户端点击", "服务端开始", "匹配耗时"))
    if any(token in text for token in mode_tokens) and mode_rule and not has_duration_formula_evidence:
        if not is_direct_gamemode_rule(rule, criterion_text):
            return None
        if not rule_source_gate(rule, logs):
            return None
        sql_values = criterion_gamemode_values(criterion_text, rule)
        rule_values = rule_gamemode_values(rule)
        if sql_values and rule_values:
            if sql_values <= rule_values:
                labels = [f"{mode}={gamemode_label(rule, mode)}" for mode in sorted(sql_values) if gamemode_label(rule, mode)]
                label_text = "；" + "、".join(labels) if labels else ""
                return relation("matched", f"SQL 使用的模式 ID 集合被保存口径《{title}》覆盖{label_text}。")
            missing = sorted(sql_values - rule_values)
            return relation("conflict", f"SQL 出现保存模式映射未覆盖的 ID：{', '.join(missing)}。")
        if sql_values and concept == "game-mode-name-type-boundary":
            return relation("matched", f"SQL 明确使用 GameMode 模式字段，关联到保存口径《{title}》。")
        return relation("needs_manual_check", f"SQL 使用模式相关口径，关联到保存口径《{title}》，需人工核对映射值。")

    if "battlesrvid" in text and "battlesrvid-mode-attribution" in concept:
        if rule_requires_structured_shared_log_evidence(rule):
            return None
        if not rule_source_gate(rule, logs):
            return None
        if any(token in text for token in ("gamemode", "模式归因", "模式映射", "没有gamemode", "无gamemode", "补模式")):
            return relation("matched", f"SQL 使用 BattleSrvId 模式归因相关逻辑，关联到保存口径《{title}》。")
        return None

    if ("dteventtime" in text or "dteventdate" in text or "tdbank_imp_date" in text) and (
        "event-time" in concept or "事件时间字段" in title or "time_filter" in full
    ):
        if "tdbank_imp_date" in text and ("不再要求" in rule_full_text(rule) or "mustnotrequiretdbank_imp_date" in full):
            return relation("conflict", f"SQL 条件包含 tdbank_imp_date，但保存口径《{title}》要求本项目默认不使用它。")
        if "dteventtime" in text or "dteventdate" in text:
            return relation("matched", f"SQL 使用项目事件时间字段，关联到保存口径《{title}》。")
        return relation("needs_manual_check", f"SQL 使用时间字段条件，关联到保存口径《{title}》。")

    if ("clientmatchclicktime" in text or "matchbegintime" in text or "matchduration" in text or "匹配耗时" in text) and (
        "matchend-actual-match-duration" in concept
    ):
        has_formula_parts = (
            ("clientmatchclicktime" in text or "客户端点击" in text)
            and ("matchbegintime" in text or "服务端开始" in text)
            and "matchduration" in text
        )
        return relation(
            "matched" if has_formula_parts else "needs_manual_check",
            f"SQL 使用 MatchEnd 实际匹配耗时相关字段，关联到保存口径《{title}》。" + ("" if has_formula_parts else " 当前证据不足以证明完整公式。"),
        )

    if ("totalactiveduration" in text or "累计游戏时长" in text or "非挂机" in text) and (
        "game-total-active-duration" in concept
    ):
        if rule_requires_structured_shared_log_evidence(rule):
            return None
        if not rule_source_gate(rule, logs):
            return None
        if "max" in text or "最大" in text or "分桶" in text or "bucket" in text:
            return relation("matched", f"SQL 使用累计非挂机时长/分桶口径，关联到保存口径《{title}》。")
        return relation("needs_manual_check", f"SQL 使用 TotalActiveDuration，但需要核对是否按玩家 x BattleSrvId 取 MAX 后汇总。")

    if ("留存" in text or "活跃" in text or "playerlogout" in text) and (
        "retention" in concept
    ):
        if "playerlogin" in text and "playerlogout" in text:
            return relation("matched", f"SQL 使用 PlayerLogin/PlayerLogout 活跃来源，关联到保存口径《{title}》。")
        return relation("needs_manual_check", f"SQL 可能涉及留存/活跃口径，关联到保存口径《{title}》。")

    return None


def rule_check_from_saved_rule(rule: dict[str, Any], result: str, message: str, evidence: str) -> dict[str, Any]:
    normalized = normalize_rule_check(
        {
            "rule_id": rule.get("rule_id"),
            "concept_key": rule.get("concept_key"),
            "title": rule.get("title"),
            "status": rule.get("status"),
            "result": result,
            "message": message,
            "evidence": evidence,
        },
        {
            "by_rule_id": {str(rule.get("rule_id") or ""): rule},
            "by_concept_key": {str(rule.get("concept_key") or ""): rule},
            "by_title": {str(rule.get("title") or ""): rule},
        },
        evidence,
    )
    return normalized or {
        "rule_id": str(rule.get("rule_id") or ""),
        "concept_key": str(rule.get("concept_key") or ""),
        "title": str(rule.get("title") or "已保存口径"),
        "status": str(rule.get("status") or ""),
        "result": result,
        "message": message,
        "evidence": evidence,
        "rule_summary": compact_text(rule_product_summary(rule), 520),
        "rule_display": compact_multiline_text(rule_product_display(rule), 1600),
        "full_rule": compact_multiline_text(rule.get("content"), 2400),
        "source": evidence,
    }


def rule_matches_filter(rule: dict[str, Any], filter_text: str) -> bool:
    text = filter_text.lower()
    hay = " ".join(
        str(rule.get(key) or "").lower()
        for key in ("rule_id", "concept_key", "title", "message", "rule_summary", "full_rule")
    )
    if "izoneareaid" in text and "izoneareaid" in hay:
        return True
    if "gamemode" in text and ("gamemode" in hay or "game-mode" in hay):
        return True
    if "battlesrvid" in text and "battlesrvid" in hay and any(token in text for token in ("gamemode", "模式", "mode")):
        return True
    if "dteventtime" in text and ("dteventtime" in hay or "event-time" in hay):
        return True
    if "tdbank_imp_date" in text and "tdbank_imp_date" in hay:
        return True
    return False


def saved_match_sort_key(row: dict[str, Any]) -> tuple[int, int]:
    return (
        rule_result_priority(row.get("result")),
        criterion_merge_priority(
            {
                "saved_rule_status": criterion_status_from_rule_result(row.get("result")),
                "concept_key": row.get("concept_key"),
            }
        )[1],
    )


def matching_rule_for_criterion(
    rule_checks: list[dict[str, Any]],
    rule_index: dict[str, Any],
    criterion_text: str,
    *,
    source_logs: list[Any],
    evidence: str,
) -> dict[str, Any] | None:
    """Attach only already-activated rule evidence to a displayed criterion.

    Repository rendering is not a rule-retrieval engine. It must never scan all
    saved rule prose to infer that a SQL uses a rule. Forward activation and
    reverse event-signature audit happen upstream and are persisted in
    ``rule_checks``. This function only associates that explicit evidence with a
    human-readable filter or logic row.
    """
    for rule in rule_checks:
        if not rule_check_is_product_facing(rule):
            continue
        if not rule_matches_filter(rule, criterion_text):
            continue
        saved_rule = lookup_rule(
            rule_index,
            str(rule.get("rule_id") or ""),
            str(rule.get("concept_key") or ""),
            str(rule.get("title") or ""),
        )
        if saved_rule:
            return rule_check_from_saved_rule(
                saved_rule,
                normalize_rule_result(rule.get("result")) or "needs_manual_check",
                str(rule.get("message") or rule.get("rule_summary") or saved_rule.get("title") or ""),
                str(rule.get("evidence") or rule.get("source") or evidence),
            )
        return rule
    return None


def append_applied_criterion(rows: list[dict[str, Any]], criterion: dict[str, Any]) -> None:
    name = compact_text(criterion.get("name"), 220)
    if not name:
        return
    concept_key = str(criterion.get("concept_key") or "").strip()
    if concept_key == "game-mode-map":
        mode_values = extract_sql_condition_values(name, ("GameMode", "gameModeID", "MatchMode", "mode_id", "模式"))
        if len(mode_values) == 1:
            mode_id = next(iter(mode_values))
            label = gamemode_label(
                {"concept_key": concept_key, "content": criterion.get("full_rule") or criterion.get("rule_summary") or ""},
                mode_id,
            )
            if label and label not in name:
                name = f"GameMode = {mode_id}（{label}）"
    criterion = {
        "category": str(criterion.get("category") or "口径").strip(),
        "name": name,
        "description": compact_text(criterion.get("description"), 420),
        "saved_rule_status": criterion.get("saved_rule_status") or "unique",
        "rule_id": str(criterion.get("rule_id") or "").strip(),
        "concept_key": str(criterion.get("concept_key") or "").strip(),
        "rule_title": compact_text(criterion.get("rule_title"), 220),
        "rule_summary": compact_text(criterion.get("rule_summary"), 520),
        "rule_display": compact_multiline_text(criterion.get("rule_display"), 1600),
        "full_rule": compact_multiline_text(criterion.get("full_rule"), 2400),
        "evidence": compact_text(criterion.get("evidence"), 260),
    }
    key = criterion_key(criterion)
    for idx, row in enumerate(rows):
        if criterion_key(row) != key:
            continue
        merged_description = merge_description_text(row.get("description"), criterion.get("description"))
        if criterion_merge_priority(criterion) > criterion_merge_priority(row):
            rows[idx] = {**row, **criterion, "description": merged_description}
        elif merged_description and merged_description != row.get("description"):
            row["description"] = merged_description
        return
    rows.append(criterion)


def applied_criteria(summary_seed: dict[str, Any], rule_checks: list[dict[str, Any]], rule_index: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_logs = as_list(summary_seed.get("source_logs"))
    for log_name in as_list(summary_seed.get("source_logs")):
        text = str(log_name or "").strip()
        if text:
            append_applied_criterion(
                rows,
                {
                    "category": "数据来源口径",
                    "name": "使用日志：" + text,
                    "description": "该 SQL 的计算来源包含这个原始日志。",
                    "saved_rule_status": "unique",
                    "evidence": "repository_summary.source_logs",
                },
            )
    for source in as_list(summary_seed.get("external_sources")):
        if not isinstance(source, dict):
            continue
        table = str(source.get("table") or source.get("physical_table") or "").strip()
        if not table:
            continue
        append_applied_criterion(
            rows,
            {
                "category": "数据来源口径",
                "name": "外部权威表：" + table,
                "description": str(source.get("business_role") or "该 SQL 使用外部 DA 平台权威来源。"),
                "saved_rule_status": "unique",
                "rule_summary": (
                    f"权威表：{table}；日期字段：{source.get('date_field') or '未声明'}；默认筛选：{source.get('default_filter') or '未声明'}。"
                ),
                "evidence": "repository_summary.external_sources",
            },
        )

    base = str(summary_seed.get("base_population") or "").strip()
    if base:
        append_applied_criterion(
            rows,
            {
                "category": "统计对象口径",
                "name": "Base：" + base,
                "description": "该 SQL 的统计对象或输出行含义。",
                "saved_rule_status": "unique",
                "evidence": "repository_summary.base_population",
            },
        )

    for filter_text in as_list(summary_seed.get("filters")):
        text = str(filter_text or "").strip()
        if not text:
            continue
        role = condition_role(text)
        category = {
            "business_filter": "固定筛选口径",
            "time_scope": "时间范围",
            "execution_filter": "执行裁剪条件",
            "data_quality": "数据质量条件",
        }.get(role, "固定筛选口径")
        matched_rule = None
        if role == "business_filter":
            matched_rule = matching_rule_for_criterion(
                rule_checks,
                rule_index,
                text,
                source_logs=source_logs,
                evidence="repository_summary.filters",
            )
        append_applied_criterion(
            rows,
            {
                "category": category,
                "name": text,
                "description": (matched_rule or {}).get("message") or text,
                "saved_rule_status": criterion_status_from_rule_result((matched_rule or {}).get("result")) if matched_rule else "unique",
                "rule_id": (matched_rule or {}).get("rule_id") or "",
                "concept_key": (matched_rule or {}).get("concept_key") or "",
                "rule_title": (matched_rule or {}).get("title") or "",
                "rule_summary": (matched_rule or {}).get("rule_summary") or "",
                "rule_display": (matched_rule or {}).get("rule_display") or "",
                "full_rule": (matched_rule or {}).get("full_rule") or "",
                "evidence": "repository_summary.filters",
            },
        )

    for logic_text in as_list(summary_seed.get("logic_summary")):
        text = str(logic_text or "").strip()
        if not text:
            continue
        matched_rule = matching_rule_for_criterion(
            rule_checks,
            rule_index,
            text,
            source_logs=source_logs,
            evidence="repository_summary.logic_summary",
        )
        if not matched_rule and len(text) < 18:
            continue
        append_applied_criterion(
            rows,
            {
                "category": "计算口径",
                "name": text,
                "description": (matched_rule or {}).get("message") or "该 SQL 的核心计算/归因逻辑。",
                "saved_rule_status": criterion_status_from_rule_result((matched_rule or {}).get("result")) if matched_rule else "unique",
                "rule_id": (matched_rule or {}).get("rule_id") or "",
                "concept_key": (matched_rule or {}).get("concept_key") or "",
                "rule_title": (matched_rule or {}).get("title") or "",
                "rule_summary": (matched_rule or {}).get("rule_summary") or "",
                "rule_display": (matched_rule or {}).get("rule_display") or "",
                "full_rule": (matched_rule or {}).get("full_rule") or "",
                "evidence": "repository_summary.logic_summary",
            },
        )

    for rule in rule_checks:
        if not rule_check_is_product_facing(rule):
            continue
        message = str(rule.get("message") or rule.get("rule_summary") or rule.get("title") or "").strip()
        append_applied_criterion(
            rows,
            {
                "category": "项目口径",
                "name": message or rule.get("title") or "已保存口径",
                "description": message,
                "saved_rule_status": criterion_status_from_rule_result(rule.get("result")),
                "rule_id": rule.get("rule_id") or "",
                "concept_key": rule.get("concept_key") or "",
                "rule_title": rule.get("title") or "",
                "rule_summary": rule.get("rule_summary") or "",
                "rule_display": rule.get("rule_display") or "",
                "full_rule": rule.get("full_rule") or "",
                "evidence": rule.get("evidence") or rule.get("source") or "",
            },
        )

    for metric_group in as_list(summary_seed.get("metric_groups")):
        if not isinstance(metric_group, dict):
            continue
        dedup = str(metric_group.get("shared_dedup_key") or "").strip()
        if dedup:
            matched_rule = matching_rule_for_criterion(
                rule_checks,
                rule_index,
                dedup,
                source_logs=source_logs,
                evidence="repository_summary.metric_groups.shared_dedup_key",
            )
            append_applied_criterion(
                rows,
                {
                    "category": "统计口径",
                    "name": "去重 / 聚合：" + dedup,
                    "description": (matched_rule or {}).get("message") or "该 SQL 的指标共享这个去重或聚合规则。",
                    "saved_rule_status": criterion_status_from_rule_result((matched_rule or {}).get("result")) if matched_rule else "unique",
                    "rule_id": (matched_rule or {}).get("rule_id") or "",
                    "concept_key": (matched_rule or {}).get("concept_key") or "",
                    "rule_title": (matched_rule or {}).get("title") or "",
                    "rule_summary": (matched_rule or {}).get("rule_summary") or "",
                    "rule_display": (matched_rule or {}).get("rule_display") or "",
                    "full_rule": (matched_rule or {}).get("full_rule") or "",
                    "evidence": "repository_summary.metric_groups.shared_dedup_key",
                },
            )

    return rows[:32]


def applied_criteria_status(criteria: list[dict[str, Any]]) -> str:
    business_rows = [
        item
        for item in criteria
        if str(item.get("category") or "") not in {"数据质量条件", "执行裁剪条件", "时间范围"}
    ]
    if not business_rows:
        return "unique"
    statuses = {str(item.get("saved_rule_status") or "").lower() for item in business_rows}
    if "conflict" in statuses:
        return "conflict"
    if "needs_manual_check" in statuses:
        return "needs_manual_check"
    if "matched" in statuses:
        return "matched"
    return "unique"


def infer_business_topic(artifact: dict[str, Any], spec: dict[str, Any], summary: dict[str, Any]) -> str:
    for value in (
        summary.get("business_topic"),
        artifact.get("business_category"),
        spec.get("business_category"),
        artifact.get("analysis_type"),
    ):
        text = str(value or "").strip()
        if not text:
            continue
        return TOPIC_LABELS.get(text, text)
    return "未分类"


def build_repository_summary(
    root: Path,
    manifest: dict[str, Any],
    artifact: dict[str, Any],
    spec: dict[str, Any],
    catalog: dict[str, dict[str, str]],
    rule_index: dict[str, dict[str, Any]],
    sql_text: str = "",
) -> dict[str, Any]:
    existing = spec.get("repository_summary") if isinstance(spec.get("repository_summary"), dict) else {}
    formalize_bundle = spec.get("formalize_bundle") if isinstance(spec.get("formalize_bundle"), dict) else {}
    sql_facts = formalize_bundle.get("sql_facts") if isinstance(formalize_bundle.get("sql_facts"), dict) else {}
    run = latest_run_for_artifact(manifest, artifact, spec)
    intent = spec.get("query_intent") if isinstance(spec.get("query_intent"), dict) else {}
    logic = spec.get("query_logic") if isinstance(spec.get("query_logic"), dict) else {}
    contract = spec.get("query_output_contract") if isinstance(spec.get("query_output_contract"), dict) else {}
    display_title = strip_source_prefix(existing.get("display_title") or artifact.get("title") or intent.get("title") or artifact.get("path"))
    source_logs = existing.get("source_logs") or sql_facts.get("source_logs") or source_logs_from_spec(spec, artifact, catalog)
    external_sources = (
        existing.get("external_sources")
        if isinstance(existing.get("external_sources"), list)
        else sql_facts.get("external_sources", [])
    )
    rule_checks = canonical_rule_checks(spec, rule_index, source_logs, sql_text=sql_text)
    metrics = normalize_repository_metrics(existing.get("metrics") or sql_facts.get("metrics") or metric_definitions(spec, artifact), spec)
    dimensions = existing.get("dimensions") or sql_facts.get("dimensions") or query_dimensions(spec, artifact)
    filters = existing.get("filters") or sql_facts.get("filters") or query_filters(spec)
    groups = metric_groups(metrics)
    logic_summary = existing.get("logic_summary") or unique(
        [
            compact_text(logic.get("business_context"), 260),
            compact_text(logic.get("calculation_path"), 360),
            compact_text(logic.get("dedup_grain"), 180),
            compact_text(logic.get("bucket_rule"), 240),
        ]
    )
    summary_seed = {
        "filters": filters,
        "metric_groups": groups,
        "source_logs": source_logs,
        "external_sources": external_sources,
        "logic_summary": logic_summary,
        "base_population": existing.get("base_population") or logic.get("business_context") or contract.get("one_row_means") or artifact.get("grain") or "",
    }
    criteria = applied_criteria(summary_seed, rule_checks, rule_index)
    summary = {
        "display_title": display_title,
        "source_title": clean_source_title(artifact.get("source_title") or artifact.get("title") or intent.get("title") or artifact.get("path")),
        "business_topic": existing.get("business_topic") or TOPIC_LABELS.get(str(artifact.get("business_category") or ""), str(artifact.get("business_category") or "未分类")),
        "purpose": existing.get("purpose") or intent.get("description") or intent.get("title") or artifact.get("natural_language_intent") or artifact.get("content_summary") or "",
        "business_question": existing.get("business_question") or intent.get("description") or artifact.get("natural_language_intent") or "",
        "base_population": existing.get("base_population") or logic.get("business_context") or contract.get("one_row_means") or artifact.get("grain") or "",
        "grain": existing.get("grain") or contract.get("output_grain") or artifact.get("grain") or "",
        "metrics": metrics,
        "metric_groups": groups,
        "dimensions": dimensions,
        "filters": filters,
        "source_logs": source_logs,
        "external_sources": external_sources,
        "logic_summary": logic_summary,
        "applied_criteria": criteria,
        "canonical_rule_status": applied_criteria_status(criteria) if criteria else canonical_rule_status(rule_checks),
        "canonical_rule_checks": rule_checks,
        "result_evidence": existing.get("result_evidence") or run_evidence_summary(root, run),
        "generated_by": existing.get("generated_by") or "sql_repository.py",
        "sql_fact_bundle_version": sql_facts.get("schema_version", ""),
    }
    summary["business_topic"] = infer_business_topic(artifact, spec, summary)
    return summary


def validate_repository_summary(summary: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in ("display_title", "purpose", "base_population", "grain"):
        if not str(summary.get(field) or "").strip():
            problems.append(f"repository_summary.{field} is required")
    if summary.get("canonical_rule_status") not in {"matched", "conflict", "needs_manual_check", "unique"}:
        problems.append("repository_summary.canonical_rule_status must be matched, conflict, needs_manual_check, or unique")
    if not as_list(summary.get("metrics")):
        problems.append("repository_summary.metrics must not be empty")
    if not as_list(summary.get("metric_groups")):
        problems.append("repository_summary.metric_groups must not be empty")
    if not as_list(summary.get("applied_criteria")):
        problems.append("repository_summary.applied_criteria must not be empty")
    if not as_list(summary.get("source_logs")) and not as_list(summary.get("external_sources")):
        problems.append("repository_summary.source_logs or repository_summary.external_sources must contain a declared source")
    return problems


def source_sql_payload(root: Path, sql_path: Path, sql_text: str, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": normalize_rel(root, sql_path),
        "available": bool(sql_text),
        "text": sql_text,
        "line_count": len(sql_text.splitlines()) if sql_text else 0,
        "char_count": len(sql_text) if sql_text else 0,
    }


def build_dashboard_attachment(
    root: Path,
    artifact: dict[str, Any],
    sample_limit: int,
    dashboard_review_html: str,
) -> dict[str, Any]:
    sql_path = root / str(artifact.get("path") or "")
    sql_text = read_text_file(sql_path)
    spec, parse_errors = load_sidecar_spec(root, artifact, sql_path)
    spec = spec or {}
    rows, sample_meta = dashboard_sample_rows(root, artifact, spec, sample_limit)
    validation = spec.get("validation_reference") if isinstance(spec.get("validation_reference"), dict) else {}
    return {
        "path": normalize_rel(root, sql_path),
        "title": strip_source_prefix(artifact.get("title") or artifact.get("path") or ""),
        "slug": artifact.get("slug") or "",
        "version": artifact.get("version"),
        "status": artifact.get("verification_status") or validation.get("verification_status") or artifact.get("status") or "",
        "linked_validation": artifact.get("linked_validation") or "",
        "linked_run": artifact.get("linked_run") or "",
        "sql_hash": sha256_text(sql_text),
        "source_sql": source_sql_payload(root, sql_path, sql_text, "看板 SQL"),
        "dashboard_summary": dashboard_summary(spec),
        "knowledge_references": as_list(spec.get("knowledge_references")),
        "generation_provenance": provenance_from_sources(artifact, spec),
        "spec": {
            "path": normalize_rel(root, spec_path_for_artifact(root, artifact, sql_path)),
            "parse_errors": parse_errors,
        },
        "sample": rows,
        "sample_meta": sample_meta,
        "dashboard_review_html": dashboard_review_html,
    }


def build_query_item(
    root: Path,
    manifest: dict[str, Any],
    artifact: dict[str, Any],
    attachments: list[dict[str, Any]],
    sample_limit: int,
) -> dict[str, Any]:
    sql_path = root / str(artifact.get("path") or "")
    sql_text = read_text_file(sql_path)
    spec, parse_errors = load_sidecar_spec(root, artifact, sql_path)
    spec = spec or {}
    snapshot = persisted_repository_snapshot(spec)
    summary = snapshot["summary"]
    run = latest_run_for_artifact(manifest, artifact, spec)
    rows, sample_meta = sample_for_evidence(root, run, sample_limit, query_expected_fields(spec))
    display_rules = query_display_rules(spec, run)
    return {
        "schema": PAYLOAD_VERSION,
        "state_key": f"query::{normalize_rel(root, sql_path)}",
        "asset_type": "query",
        "path": normalize_rel(root, sql_path),
        "title": summary.get("display_title") or strip_source_prefix(artifact.get("title") or artifact.get("path")),
        "source_title": summary.get("source_title") or clean_source_title(artifact.get("title") or ""),
        "slug": artifact.get("slug") or "",
        "version": artifact.get("version"),
        "status": artifact.get("verification_status") or artifact.get("status") or "",
        "artifact_state": artifact.get("artifact_state") or "",
        "business_topic": summary.get("business_topic") or "未分类",
        "canonical_rule_status": summary.get("canonical_rule_status") or "unique",
        "sql_hash": sha256_text(sql_text),
        "source_sql": source_sql_payload(root, sql_path, sql_text, "查询 SQL"),
        "generation_provenance": provenance_from_sources(artifact, spec),
        "origin_query_workspace": spec.get("origin_query_workspace") or artifact.get("origin_query_workspace") or {},
        "knowledge_references": as_list(spec.get("knowledge_references")),
        "repository_summary": summary,
        "repository_snapshot": snapshot,
        "summary": {
            "metrics": [row.get("name") if isinstance(row, dict) else str(row) for row in as_list(summary.get("metrics"))],
            "dimensions": labels_from_items(summary.get("dimensions")),
            "filters": labels_from_items(summary.get("filters")),
            "source_logs": labels_from_items(summary.get("source_logs")),
            "result_evidence": summary.get("result_evidence") or run_evidence_summary(root, run),
            "display_rules": display_rules,
            "dashboard_state": "已转看板" if attachments else "未转看板",
        },
        "dashboard_attachments": attachments,
        "sample": rows,
        "sample_meta": sample_meta,
        "spec": {
            "path": normalize_rel(root, spec_path_for_artifact(root, artifact, sql_path)),
            "parse_errors": parse_errors,
            "quality_gate": spec.get("quality_gate") or {},
            "performance": spec.get("performance_level") or {},
            "technical_sources": technical_sources_from_spec(spec, artifact),
            "output_fields": output_fields(spec, artifact),
            "display_rules": display_rules,
        },
    }


def package_evidence(
    root: Path,
    package_root: Path,
    members: list[dict[str, Any]],
    *,
    sample_limit: int,
    include_history: bool,
    current_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    evidence_members = [
        item
        for item in members
        if str(item.get("role") or "").lower() in EVIDENCE_ROLES
        and (include_history or str(item.get("member_id") or "") in current_ids)
    ]
    records = [
        member_document(root, package_root, item)
        for item in evidence_members
        if str(item.get("role") or "").lower() in {"run_record", "run_evidence"}
    ]
    records = [item for item in records if item]
    results = [
        item for item in evidence_members if str(item.get("role") or "").lower() == "result_evidence"
    ]
    normalized = [
        {
            "member_id": str(item.get("member_id") or ""),
            "role": str(item.get("role") or ""),
            "lifecycle_state": str(item.get("lifecycle_state") or ""),
            "path": str(item.get("path") or ""),
            "available": bool(
                (path := package_member_path(root, package_root, item)) and path.is_file()
            ),
        }
        for item in evidence_members
    ]
    primary_result = next(
        (item for item in results if str(item.get("lifecycle_state") or "") == "current"),
        results[0] if results else None,
    )
    rows: list[dict[str, Any]] = []
    sample_meta: dict[str, Any] = {
        "type": "none",
        "path": "",
        "note": "Package 没有登记结果证据。",
    }
    if primary_result:
        result_path = package_member_path(root, package_root, primary_result)
        relative = str(primary_result.get("path") or "")
        if result_path and result_path.is_file():
            try:
                if result_path.suffix.lower() == ".csv":
                    rows = read_csv_sample(result_path, sample_limit)
                elif result_path.suffix.lower() == ".xlsx":
                    rows = read_xlsx_sample(result_path, sample_limit)
                sample_meta = {
                    "type": "actual" if rows else "registered",
                    "path": relative,
                    "note": "使用 Package 中登记的精确结果证据样例。",
                }
            except Exception as exc:  # noqa: BLE001
                sample_meta = {
                    "type": "read_error",
                    "path": relative,
                    "note": f"Package 结果证据读取失败：{exc}",
                }
    record = records[-1] if records else {}
    summary = {
        "status": str(record.get("status") or ("recorded" if results else "missing")),
        "path": str(primary_result.get("path") or "") if primary_result else "",
        "evidence_file": str(primary_result.get("path") or "") if primary_result else "",
        "evidence_file_exists": bool(primary_result and sample_meta.get("type") != "read_error"),
        "row_count": record.get("row_count"),
        "summary": compact_text(record.get("result_summary"), 520),
        "issues": compact_text(record.get("issues"), 360),
        "user_confirmed": bool(record.get("user_confirmed")),
        "result_member_count": len(results),
    }
    return normalized, summary, rows, sample_meta


def package_query_rows(
    root: Path,
    package_root: Path,
    package: dict[str, Any],
    members: list[dict[str, Any]],
    *,
    include_history: bool,
    current_ids: set[str],
    evidence_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    query_members = [
        item
        for item in members
        if str(item.get("role") or "").lower() in QUERY_SQL_ROLES
        and (include_history or str(item.get("member_id") or "") in current_ids)
    ]
    spec_members = [item for item in members if str(item.get("role") or "").lower() in QUERY_SPEC_ROLES]
    meta_members = [item for item in members if str(item.get("role") or "").lower() in QUERY_META_ROLES]
    rows: list[dict[str, Any]] = []
    for member in query_members:
        sql_path = package_member_path(root, package_root, member)
        sql_text = read_text_file(sql_path) if sql_path else ""
        spec_member = companion_member(member, spec_members)
        meta_member = companion_member(member, meta_members)
        spec = member_document(root, package_root, spec_member)
        meta = member_document(root, package_root, meta_member)
        snapshot = persisted_repository_snapshot(spec)
        summary = dict(snapshot.get("summary") or {})
        repository_summary = spec.get("repository_summary") if isinstance(spec.get("repository_summary"), dict) else {}
        intent = spec.get("query_intent") if isinstance(spec.get("query_intent"), dict) else {}
        contract = spec.get("query_output_contract") if isinstance(spec.get("query_output_contract"), dict) else {}
        summary.setdefault("display_title", strip_source_prefix(meta.get("title") or package.get("title") or member.get("path")))
        summary.setdefault("source_title", clean_source_title(meta.get("source_title") or meta.get("title") or package.get("title")))
        summary.setdefault("business_topic", repository_summary.get("business_topic") or meta.get("business_category") or "未分类")
        summary.setdefault("purpose", repository_summary.get("purpose") or intent.get("description") or package.get("title") or "")
        summary.setdefault("business_question", repository_summary.get("business_question") or intent.get("description") or "")
        summary.setdefault("base_population", repository_summary.get("base_population") or contract.get("one_row_means") or meta.get("grain") or "")
        summary.setdefault("grain", repository_summary.get("grain") or contract.get("output_grain") or meta.get("grain") or "")
        summary.setdefault("metrics", as_list(repository_summary.get("metrics") or meta.get("metrics")))
        summary.setdefault("metric_groups", as_list(repository_summary.get("metric_groups")))
        summary.setdefault("dimensions", as_list(repository_summary.get("dimensions") or meta.get("dimensions")))
        summary.setdefault("filters", as_list(repository_summary.get("filters")))
        summary.setdefault("source_logs", as_list(repository_summary.get("source_logs")))
        summary.setdefault("external_sources", as_list(repository_summary.get("external_sources")))
        summary.setdefault("logic_summary", as_list(repository_summary.get("logic_summary")))
        summary.setdefault("applied_criteria", as_list(repository_summary.get("applied_criteria")))
        summary.setdefault("canonical_rule_checks", as_list(repository_summary.get("canonical_rule_checks")))
        summary.setdefault("canonical_rule_status", repository_summary.get("canonical_rule_status") or "unique")
        summary["result_evidence"] = evidence_summary
        artifact = {**meta, "path": str(member.get("path") or ""), "title": package.get("title") or ""}
        rows.append(
            {
                "member_id": str(member.get("member_id") or ""),
                "role": str(member.get("role") or ""),
                "lifecycle_state": str(member.get("lifecycle_state") or ""),
                "path": str(member.get("path") or ""),
                "source_sql": source_sql_payload(root, sql_path or Path(str(member.get("path") or "")), sql_text, "正式查询 SQL"),
                "sql_hash": sha256_text(sql_text),
                "repository_summary": summary,
                "repository_snapshot": snapshot,
                "generation_provenance": provenance_from_sources(meta, spec),
                "origin_query_workspace": spec.get("origin_query_workspace") or meta.get("origin_query_workspace") or {},
                "knowledge_references": as_list(spec.get("knowledge_references")),
                "spec": {
                    "member_id": str((spec_member or {}).get("member_id") or ""),
                    "path": str((spec_member or {}).get("path") or ""),
                    "available": bool(spec),
                    "quality_gate": spec.get("quality_gate") or {},
                    "performance": spec.get("performance_level") or {},
                    "technical_sources": technical_sources_from_spec(spec, artifact),
                    "output_fields": output_fields(spec, artifact),
                    "display_rules": query_display_rules(spec),
                },
                "meta": {
                    "member_id": str((meta_member or {}).get("member_id") or ""),
                    "path": str((meta_member or {}).get("path") or ""),
                    "available": bool(meta),
                },
            }
        )
    return rows


def package_dashboard_rows(
    root: Path,
    package_root: Path,
    members: list[dict[str, Any]],
    *,
    include_history: bool,
    current_ids: set[str],
    dashboard_review_html: str,
    lineage: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sql_members = [
        item
        for item in members
        if str(item.get("role") or "").lower() in DASHBOARD_SQL_ROLES
        and (include_history or str(item.get("member_id") or "") in current_ids)
    ]
    specs = [item for item in members if str(item.get("role") or "").lower() in DASHBOARD_SPEC_ROLES]
    metas = [item for item in members if str(item.get("role") or "").lower() in DASHBOARD_META_ROLES]
    rows: list[dict[str, Any]] = []
    for member in sql_members:
        sql_path = package_member_path(root, package_root, member)
        sql_text = read_text_file(sql_path) if sql_path else ""
        spec_member = companion_member(member, specs)
        meta_member = companion_member(member, metas)
        spec = member_document(root, package_root, spec_member)
        meta = member_document(root, package_root, meta_member)
        member_id = str(member.get("member_id") or "")
        related = [
            edge
            for edge in lineage
            if isinstance(edge, dict)
            and member_id in {str(edge.get("from_member_id") or ""), str(edge.get("to_member_id") or "")}
        ]
        rows.append(
            {
                "member_id": member_id,
                "role": str(member.get("role") or ""),
                "path": str(member.get("path") or ""),
                "title": strip_source_prefix(meta.get("title") or Path(str(member.get("path") or "")).stem),
                "status": meta.get("verification_status") or member.get("lifecycle_state") or "",
                "lifecycle_state": str(member.get("lifecycle_state") or ""),
                "sql_hash": sha256_text(sql_text),
                "source_sql": source_sql_payload(root, sql_path or Path(str(member.get("path") or "")), sql_text, "看板 SQL"),
                "dashboard_summary": dashboard_summary(spec),
                "knowledge_references": as_list(spec.get("knowledge_references")),
                "generation_provenance": provenance_from_sources(meta, spec),
                "spec": {
                    "member_id": str((spec_member or {}).get("member_id") or ""),
                    "path": str((spec_member or {}).get("path") or ""),
                    "available": bool(spec),
                },
                "meta": {
                    "member_id": str((meta_member or {}).get("member_id") or ""),
                    "path": str((meta_member or {}).get("path") or ""),
                    "available": bool(meta),
                },
                "lineage": related,
                "dashboard_review_html": dashboard_review_html,
            }
        )
    return rows


def package_role_rows(
    root: Path,
    package_root: Path,
    members: list[dict[str, Any]],
    roles: set[str],
    *,
    include_history: bool,
    current_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        {
            "member_id": str(item.get("member_id") or ""),
            "role": str(item.get("role") or ""),
            "lifecycle_state": str(item.get("lifecycle_state") or ""),
            "path": str(item.get("path") or ""),
            "available": bool(
                (path := package_member_path(root, package_root, item)) and path.is_file()
            ),
            "document": member_document(root, package_root, item),
        }
        for item in members
        if str(item.get("role") or "").lower() in roles
        and (include_history or str(item.get("member_id") or "") in current_ids)
    ]


def build_package_item(
    root: Path,
    package_row: dict[str, Any],
    *,
    include_history: bool,
    sample_limit: int,
    dashboard_review_html: str,
) -> dict[str, Any]:
    package = package_row["manifest"]
    package_root = package_row["package_root"]
    members = [item for item in as_list(package.get("members")) if isinstance(item, dict)]
    current_ids = package_current_member_ids(package)
    lineage = [item for item in as_list(package.get("lineage")) if isinstance(item, dict)]
    package_issues: list[dict[str, str]] = []
    member_ids = {str(item.get("member_id") or "") for item in members if str(item.get("member_id") or "")}
    for member in members:
        member_path = str(member.get("path") or "")
        resolved = package_member_path(root, package_root, member)
        if resolved is None:
            package_issues.append(
                {
                    "code": "package_member_outside_members",
                    "path": member_path,
                    "message": "Package member path must remain inside this Package members directory.",
                }
            )
        elif not resolved.is_file():
            package_issues.append(
                {
                    "code": "package_member_missing",
                    "path": member_path,
                    "message": "Package manifest member does not exist on disk.",
                }
            )
    for member_id in sorted(current_ids - member_ids):
        package_issues.append(
            {
                "code": "package_current_member_missing",
                "path": package_row["manifest_path"],
                "message": f"Current pointer references unknown member {member_id}.",
            }
        )
    evidence, evidence_summary, sample, sample_meta = package_evidence(
        root,
        package_root,
        members,
        sample_limit=sample_limit,
        include_history=include_history,
        current_ids=current_ids,
    )
    queries = package_query_rows(
        root,
        package_root,
        package,
        members,
        include_history=include_history,
        current_ids=current_ids,
        evidence_summary=evidence_summary,
    )
    dashboards = package_dashboard_rows(
        root,
        package_root,
        members,
        include_history=include_history,
        current_ids=current_ids,
        dashboard_review_html=dashboard_review_html,
        lineage=lineage,
    )
    validations = package_role_rows(
        root,
        package_root,
        members,
        VALIDATION_SQL_ROLES | VALIDATION_SPEC_ROLES | VALIDATION_META_ROLES,
        include_history=include_history,
        current_ids=current_ids,
    )
    outputs = package_role_rows(
        root,
        package_root,
        members,
        DERIVED_OUTPUT_ROLES,
        include_history=include_history,
        current_ids=current_ids,
    )
    primary = queries[0] if queries else {}
    summary = dict(primary.get("repository_summary") or {})
    query_summaries = [dict(item.get("repository_summary") or {}) for item in queries]
    summary_metrics = unique(
        [
            label
            for query_summary in query_summaries
            for label in labels_from_items(query_summary.get("metrics"))
        ]
    )
    summary_dimensions = unique(
        [
            label
            for query_summary in query_summaries
            for label in labels_from_items(query_summary.get("dimensions"))
        ]
    )
    summary_filters = unique(
        [
            label
            for query_summary in query_summaries
            for label in labels_from_items(query_summary.get("filters"))
        ]
    )
    summary_logs = unique(
        [
            label
            for query_summary in query_summaries
            for label in labels_from_items(query_summary.get("source_logs"))
        ]
    )
    receipt_path = str(package.get("latest_receipt") or "")
    receipt_member = {"path": receipt_path, "available": False, "receipt": {}}
    if receipt_path:
        candidate: Path | None = None
        try:
            relative_receipt = Path(receipt_path.replace("\\", "/"))
            if relative_receipt.is_absolute() or ".." in relative_receipt.parts:
                raise ValueError("unsafe receipt path")
            resolved_receipt = (root / relative_receipt).resolve()
            resolved_receipt.relative_to(package_root)
            candidate = resolved_receipt
        except (OSError, ValueError):
            package_issues.append(
                {
                    "code": "package_receipt_outside_package",
                    "path": receipt_path,
                    "message": "Latest receipt must remain inside its Formal Asset Package.",
                }
            )
        if candidate is not None and candidate.is_file():
            receipt_member = {
                "path": receipt_path,
                "available": True,
                "receipt": read_json(candidate, {}),
            }
        elif candidate is not None:
            package_issues.append(
                {
                    "code": "package_receipt_missing",
                    "path": receipt_path,
                    "message": "Latest Package receipt does not exist on disk.",
                }
            )
    return {
        "schema": PAYLOAD_VERSION,
        "state_key": f"package::{package.get('project_id', root.name)}::{package.get('package_id', '')}",
        "asset_type": "formal_asset_package",
        "formal_asset_id": str(package.get("package_id") or ""),
        "package_id": str(package.get("package_id") or ""),
        "package_revision": int(package.get("revision") or 0),
        "package_manifest_path": package_row["manifest_path"],
        "path": package_row["manifest_path"],
        "title": str(package.get("title") or package.get("package_id") or "Formal Asset Package"),
        "slug": str(package.get("slug") or ""),
        "version": int(package.get("revision") or 0),
        "status": str(package.get("lifecycle_state") or ""),
        "artifact_state": str(package.get("lifecycle_state") or ""),
        "business_topic": summary.get("business_topic") or "未分类",
        "canonical_rule_status": summary.get("canonical_rule_status") or "unique",
        "source_sql": primary.get("source_sql") or source_sql_payload(root, package_root, "", "正式查询 SQL"),
        "sql_hash": primary.get("sql_hash") or "",
        "generation_provenance": primary.get("generation_provenance") or {},
        "origin_query_workspace": primary.get("origin_query_workspace") or {},
        "knowledge_references": unique_structured(
            [
                row
                for asset in [*queries, *dashboards]
                for row in as_list(asset.get("knowledge_references"))
                if isinstance(row, (dict, str))
            ]
        ),
        "repository_summary": summary,
        "repository_snapshot": primary.get("repository_snapshot") or {
            "schema_version": "repository_snapshot_v1",
            "status": "missing",
            "summary": {},
            "problems": ["Package has no current formal query sidecar summary."],
        },
        "summary": {
            "metrics": summary_metrics,
            "dimensions": summary_dimensions,
            "filters": summary_filters,
            "source_logs": summary_logs,
            "result_evidence": evidence_summary,
            "display_rules": (primary.get("spec") or {}).get("display_rules") or [],
            "dashboard_state": "已有看板" if dashboards else "未转看板",
        },
        "queries": queries,
        "dashboard_attachments": dashboards,
        "validations": validations,
        "evidence_members": evidence,
        "derived_outputs": outputs,
        "members": [
            normalized_package_member(root, package_root, item, current_ids=current_ids)
            for item in members
        ],
        "current_member_ids": sorted(current_ids),
        "lineage": lineage,
        "latest_receipt": receipt_member,
        "issues": package_issues,
        "sample": sample,
        "sample_meta": sample_meta,
        "spec": primary.get("spec") or {},
        "package_completeness": {
            "query_count": len(queries),
            "evidence_count": len(evidence),
            "derived_output_count": len(outputs),
            "validation_member_count": len(validations),
            "dashboard_count": len(dashboards),
            "member_count": len(members),
        },
    }


def link_relative_to_html(root: Path, html_output: Path, target_rel: str) -> str:
    target = (root / target_rel).resolve()
    relative = os.path.relpath(target, html_output.resolve().parent)
    return Path(relative).as_posix()


def build_payload(root: Path, *, include_history: bool, sample_limit: int, html_output: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    project_config_path = root / "project_config.json"
    discovery_issues: list[dict[str, str]] = []
    try:
        project_config = read_json(project_config_path, {})
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        project_config = {}
        discovery_issues.append(
            {
                "code": "project_config_unreadable",
                "path": "project_config.json",
                "message": str(exc),
            }
        )
    if not isinstance(project_config, dict):
        project_config = {}
        discovery_issues.append(
            {
                "code": "project_config_invalid",
                "path": "project_config.json",
                "message": "Project configuration must be a JSON object.",
            }
        )
    repository_html = html_output or (root / DEFAULT_HTML_REL)
    dashboard_review_html = link_relative_to_html(root, repository_html, DEFAULT_DASHBOARD_REVIEW_REL)
    package_rows, package_issues = package_manifest_rows(root, include_history=include_history)
    discovery_issues.extend(package_issues)
    items: list[dict[str, Any]] = []
    for package_row in package_rows:
        try:
            item = build_package_item(
                root,
                package_row,
                include_history=include_history,
                sample_limit=sample_limit,
                dashboard_review_html=dashboard_review_html,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            discovery_issues.append(
                {
                    "code": "package_projection_failed",
                    "path": str(package_row.get("manifest_path") or ""),
                    "message": str(exc),
                }
            )
            continue
        items.append(item)

    item_issues = [
        {**issue, "package_id": str(item.get("package_id") or "")}
        for item in items
        for issue in as_list(item.get("issues"))
        if isinstance(issue, dict)
    ]
    issues = [*discovery_issues, *item_issues]
    return {
        "schema": PAYLOAD_VERSION,
        "project": str(
            project_config.get("display_name")
            or project_config.get("project_name")
            or project_config.get("project_id")
            or root.name
        ),
        "project_id": str(project_config.get("project_id") or root.name),
        "project_root": ".",
        "discovery_source": "formal_assets/*/manifest.json",
        "generated_at": now_iso(),
        "package_count": len(items),
        "formal_asset_package_count": len(items),
        "package_member_count": sum(len(as_list(item.get("members"))) for item in items),
        "query_count": sum(len(as_list(item.get("queries"))) for item in items),
        "dashboard_attachment_count": sum(len(as_list(item.get("dashboard_attachments"))) for item in items),
        "validation_member_count": sum(len(as_list(item.get("validations"))) for item in items),
        "evidence_member_count": sum(len(as_list(item.get("evidence_members"))) for item in items),
        "derived_output_count": sum(len(as_list(item.get("derived_outputs"))) for item in items),
        "issue_count": len(issues),
        "issues": issues,
        "items": items,
        # Kept as empty compatibility fields; Package lineage owns dashboard
        # attachment identity and no legacy orphan discovery is performed.
        "orphan_dashboard_count": 0,
        "orphan_dashboard_attachments": [],
    }


def payload_for_html(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return "null"
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SQL 仓库</title>
  <style>
    :root { --bg:#f5f6f8; --panel:#fff; --line:#d8dee8; --text:#17202a; --muted:#667085; --brand:#2157a6; --ok:#0f7b43; --bad:#b42318; --warn:#a15c00; --soft:#f8fafc; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif; }
    header { min-height:64px; display:flex; justify-content:space-between; align-items:center; gap:16px; padding:12px 18px; background:var(--panel); border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:20px; }
    h2 { margin:0 0 10px; font-size:17px; }
    h3 { margin:0 0 8px; font-size:14px; }
    .sub { color:var(--muted); font-size:12px; }
    .layout { display:grid; grid-template-columns:minmax(340px,420px) minmax(760px,1fr); min-height:calc(100vh - 65px); }
    aside { background:var(--panel); border-right:1px solid var(--line); overflow:auto; }
    main { overflow:auto; padding:16px 18px 28px; }
    .filters { display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:12px; border-bottom:1px solid var(--line); }
    input, select, textarea { width:100%; border:1px solid var(--line); border-radius:6px; padding:8px; background:#fff; font:inherit; }
    .wide { grid-column:1 / -1; }
    .item { padding:12px 14px; border-bottom:1px solid var(--line); cursor:pointer; }
    .item:hover, .item.active { background:#edf3ff; }
    .item-title { font-weight:700; font-size:14px; line-height:1.35; }
    .item-path { margin-top:4px; color:var(--muted); font-size:12px; word-break:break-all; }
    .item-brief { margin-top:7px; color:#344054; font-size:12px; line-height:1.45; }
    .chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
    .chip { display:inline-flex; align-items:center; min-height:22px; padding:2px 8px; border-radius:999px; background:#eef1f5; color:#344054; font-size:12px; }
    .chip.ok { color:var(--ok); background:#e8f6ee; }
    .chip.bad { color:var(--bad); background:#fdecec; }
    .chip.warn { color:var(--warn); background:#fff4df; }
    .chip.neutral { color:#344054; background:#eef1f5; }
    .detail-stack { max-width:1360px; margin:0 auto; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:14px; }
    .hero { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:start; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }
    .detail-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px; }
    .mini-panel { border:1px solid var(--line); border-radius:8px; background:#f8fafc; padding:12px; }
    .repo-stats { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
    .repo-stat { min-width:88px; border:1px solid var(--line); border-radius:8px; background:var(--soft); padding:6px 10px; text-align:right; }
    .repo-stat strong { display:block; font-size:16px; }
    .summary-strip { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; margin-bottom:14px; }
    .summary-tile { border:1px solid var(--line); border-radius:8px; background:var(--soft); padding:10px; }
    .summary-tile strong { display:block; margin-bottom:4px; font-size:13px; }
    .summary-tile .big { font-size:18px; font-weight:800; }
    .rule-panel { border:1px solid var(--line); border-left:5px solid #98a2b3; border-radius:8px; background:#fff; padding:12px; margin:10px 0; }
    .rule-panel.conflict { border-left-color:var(--bad); background:#fff7f7; }
    .rule-panel.matched { border-left-color:var(--ok); background:#f4fbf7; }
    .rule-panel.needs { border-left-color:var(--warn); background:#fffaf0; }
    .rule-panel.unique { border-left-color:#667085; background:#f8fafc; }
    .rule-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
    .rule-meta { display:grid; grid-template-columns:110px minmax(0,1fr); gap:6px 10px; margin:10px 0; padding:10px; background:#fff; border:1px solid var(--line); border-radius:8px; font-size:13px; }
    .rule-meta span:nth-child(odd) { color:var(--muted); }
    .rule-text { margin:10px 0 0; padding:12px; max-height:360px; overflow:auto; white-space:pre-wrap; word-break:break-word; background:#f8fafc; border:1px solid var(--line); border-radius:8px; font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif; font-size:13px; line-height:1.65; }
    .kv { display:grid; grid-template-columns:112px minmax(0,1fr); gap:10px; margin:7px 0; line-height:1.55; }
    .kv > span:first-child { color:var(--muted); }
    .pill-list { display:flex; gap:6px; flex-wrap:wrap; }
    .pill { border:1px solid var(--line); background:#f8fafc; border-radius:999px; padding:3px 8px; font-size:13px; }
    button { border:1px solid var(--line); background:#fff; border-radius:6px; padding:8px 11px; cursor:pointer; font-weight:700; }
    button.primary { background:var(--brand); border-color:var(--brand); color:#fff; }
    .button-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
    .metric-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfcfe; margin:8px 0; }
    details { border:1px solid var(--line); border-radius:8px; background:#fff; padding:10px 12px; margin:10px 0; }
    summary { cursor:pointer; font-weight:700; }
    .sql-preview { width:100%; min-height:320px; margin-top:10px; font-family:Consolas,"Courier New",monospace; font-size:12px; line-height:1.45; white-space:pre; overflow:auto; }
    .table-wrap { width:100%; overflow:auto; border:1px solid var(--line); border-radius:8px; }
    table { width:100%; min-width:700px; border-collapse:collapse; font-size:13px; background:#fff; }
    th, td { border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }
    th { background:#f0f3f8; position:sticky; top:0; }
    .empty { color:var(--muted); text-align:center; padding:32px; }
    @media (max-width:980px) { .layout { grid-template-columns:1fr; } aside { max-height:360px; border-right:0; border-bottom:1px solid var(--line); } .hero { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>SQL 仓库</h1>
      <div id="meta" class="sub"></div>
    </div>
    <div id="repoStats" class="repo-stats"></div>
  </header>
  <div class="layout">
    <aside>
      <div class="filters">
        <select id="topicFilter"></select>
        <select id="logFilter"></select>
        <select id="dashboardFilter">
          <option value="">全部看板状态</option>
          <option value="with">已有看板</option>
          <option value="without">未转看板</option>
        </select>
        <select id="ruleFilter"></select>
        <select id="statusFilter"></select>
        <input id="search" class="wide" placeholder="搜索资产包、成员、SQL、证据、产物、看板">
      </div>
      <div id="list"></div>
    </aside>
    <main id="detail"></main>
  </div>
  <script>
    const repositoryApiUrl = __API_URL_JSON__;
    function normalizePayload(nextPayload) {
      const normalized = nextPayload || {};
      normalized.schema = normalized.schema || 'sql_repository_v2';
      normalized.project = normalized.project || '';
      normalized.project_id = normalized.project_id || '';
      normalized.project_root = normalized.project_root || '';
      normalized.generated_at = normalized.generated_at || '';
      normalized.package_count = Number(normalized.package_count || normalized.formal_asset_package_count || 0);
      normalized.package_member_count = Number(normalized.package_member_count || 0);
      normalized.query_count = Number(normalized.query_count || 0);
      normalized.dashboard_attachment_count = Number(normalized.dashboard_attachment_count || 0);
      normalized.validation_member_count = Number(normalized.validation_member_count || 0);
      normalized.evidence_member_count = Number(normalized.evidence_member_count || 0);
      normalized.derived_output_count = Number(normalized.derived_output_count || 0);
      normalized.issue_count = Number(normalized.issue_count || 0);
      normalized.orphan_dashboard_count = Number(normalized.orphan_dashboard_count || 0);
      normalized.items = Array.isArray(normalized.items) ? normalized.items : [];
      normalized.issues = Array.isArray(normalized.issues) ? normalized.issues : [];
      normalized.orphan_dashboard_attachments = Array.isArray(normalized.orphan_dashboard_attachments) ? normalized.orphan_dashboard_attachments : [];
      return normalized;
    }
    let payload = normalizePayload(__PAYLOAD_JSON__);
    let activeKey = (payload.items[0] || {}).state_key || '';
    function esc(value) { return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
    function arr(value) { return Array.isArray(value) ? value.filter(v => v !== null && v !== undefined && String(v).trim() !== '') : []; }
    function chip(text, cls='') { return text ? `<span class="chip ${cls}">${esc(text)}</span>` : ''; }
    function statusClass(value) {
      const text = String(value || '').toLowerCase();
      if (['passed','verified','current','ok'].some(v => text.includes(v))) return 'ok';
      if (['fail','blocked','error','missing','conflict'].some(v => text.includes(v))) return 'bad';
      if (['warn','proxy','skipped','draft','needs'].some(v => text.includes(v))) return 'warn';
      return '';
    }
    function ruleClass(value) {
      const text = String(value || '').toLowerCase();
      if (text.includes('conflict') || text.includes('fail') || text.includes('blocked')) return 'conflict';
      if (text.includes('matched') || text.includes('pass') || text.includes('ok')) return 'matched';
      if (text.includes('unique')) return 'unique';
      return 'needs';
    }
    function ruleStatusText(value) {
      const text = String(value || '').toLowerCase();
      if (text === 'conflict') return '发现口径冲突';
      if (text === 'matched') return '已命中口径库';
      if (text === 'unique') return '独有 / 未命中口径库';
      return '需要人工确认';
    }
    function ruleChipClass(value) {
      const text = String(value || '').toLowerCase();
      if (text === 'conflict') return 'bad';
      if (text === 'matched') return 'ok';
      if (text === 'unique') return 'neutral';
      return 'warn';
    }
    function pills(values, empty='无') {
      const rows = arr(values);
      if (!rows.length) return `<span class="sub">${esc(empty)}</span>`;
      return `<div class="pill-list">${rows.slice(0, 24).map(v => `<span class="pill">${esc(typeof v === 'object' ? (v.name || v.label || JSON.stringify(v)) : v)}</span>`).join('')}</div>`;
    }
    function displayValue(value) {
      if (typeof value === 'object') return value.name || value.label || value.business_meaning || value.metric || JSON.stringify(value);
      return value;
    }
    function compactLabels(values, limit=3, empty='无') {
      const rows = arr(values).map(displayValue).filter(Boolean);
      if (!rows.length) return empty;
      const more = rows.length > limit ? ` 等 ${rows.length} 项` : '';
      return rows.slice(0, limit).join('、') + more;
    }
    function listHtml(values, empty='无', limit=12) {
      const rows = arr(values);
      if (!rows.length) return `<span class="sub">${esc(empty)}</span>`;
      return `<ul>${rows.slice(0, limit).map(v => `<li>${esc(typeof v === 'object' ? (v.name || v.label || v.business_meaning || JSON.stringify(v)) : v)}</li>`).join('')}</ul>`;
    }
    function currentItem() { return payload.items.find(v => v.state_key === activeKey); }
    function sampleTable(rows) {
      rows = arr(rows);
      if (!rows.length) return '<span class="sub">无样例行</span>';
      const headers = [...new Set(rows.flatMap(row => Object.keys(row || {})))];
      return `<div class="table-wrap"><table><thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.slice(0,8).map(row => `<tr>${headers.map(h => `<td>${esc(row[h] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    async function copyText(text, id) {
      const status = document.getElementById(id);
      try {
        await navigator.clipboard.writeText(text || '');
        if (status) status.textContent = '已复制';
      } catch (_) {
        const ta = document.createElement('textarea');
        ta.value = text || '';
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus(); ta.select(); document.execCommand('copy'); ta.remove();
        if (status) status.textContent = '已复制';
      }
    }
    function optionValues(selector, values, emptyLabel) {
      const current = selector.value || '';
      selector.innerHTML = `<option value="">${esc(emptyLabel)}</option>` + values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
      selector.value = values.includes(current) ? current : '';
    }
    function setupFilters() {
      optionValues(document.getElementById('topicFilter'), [...new Set(payload.items.map(i => i.business_topic).filter(Boolean))].sort(), '全部业务主题');
      optionValues(document.getElementById('logFilter'), [...new Set(payload.items.flatMap(i => arr(i.summary?.source_logs)))].sort(), '全部原始日志');
      optionValues(document.getElementById('ruleFilter'), [...new Set(payload.items.map(i => i.canonical_rule_status).filter(Boolean))].map(v => `${v}|${ruleStatusText(v)}`), '全部口径状态');
      [...document.getElementById('ruleFilter').options].forEach(opt => { if (opt.value.includes('|')) opt.textContent = opt.value.split('|')[1]; });
      optionValues(document.getElementById('statusFilter'), [...new Set(payload.items.map(i => i.status).filter(Boolean))].sort(), '全部状态');
    }
    function searchablePackageText(item) {
      const memberText = arr(item.members).flatMap(row => [row.member_id, row.role, row.path, row.lifecycle_state]);
      const queryText = arr(item.queries).flatMap(row => [
        row.member_id, row.role, row.path, row.source_sql?.text,
        JSON.stringify(row.repository_summary || {}), JSON.stringify(row.spec || {}), JSON.stringify(row.meta || {}),
      ]);
      const dashboardText = arr(item.dashboard_attachments).flatMap(row => [
        row.member_id, row.path, row.title, row.source_sql?.text,
        JSON.stringify(row.dashboard_summary || {}), JSON.stringify(row.spec || {}), JSON.stringify(row.meta || {}),
      ]);
      const validationText = arr(item.validations).flatMap(row => [row.member_id, row.role, row.path, JSON.stringify(row.document || {})]);
      const evidenceText = arr(item.evidence_members).flatMap(row => [row.member_id, row.role, row.path, row.lifecycle_state]);
      const outputText = arr(item.derived_outputs).flatMap(row => [row.member_id, row.role, row.path, JSON.stringify(row.document || {})]);
      const lineageText = arr(item.lineage).flatMap(row => [row.relation, row.from_member_id, row.to_member_id, row.note]);
      return [
        item.formal_asset_id, item.package_id, item.package_manifest_path,
        ...memberText, ...queryText, ...dashboardText, ...validationText,
        ...evidenceText, ...outputText, ...lineageText,
        item.latest_receipt?.path, JSON.stringify(item.latest_receipt?.receipt || {}),
      ].join(' ').toLowerCase();
    }
    function filteredItems() {
      const topic = document.getElementById('topicFilter').value || '';
      const log = document.getElementById('logFilter').value || '';
      const dashboard = document.getElementById('dashboardFilter').value || '';
      const ruleStatus = (document.getElementById('ruleFilter').value || '').split('|')[0];
      const status = document.getElementById('statusFilter').value || '';
      const q = (document.getElementById('search').value || '').toLowerCase();
      return payload.items.filter(item => {
        const s = item.repository_summary || {};
        const ruleText = arr(s.canonical_rule_checks).map(r => `${r.title || ''} ${r.message || ''} ${r.rule_summary || ''} ${r.rule_id || ''} ${r.concept_key || ''}`);
        const criteriaText = arr(s.applied_criteria).map(c => `${c.category || ''} ${c.name || ''} ${c.description || ''} ${c.rule_title || ''} ${c.rule_id || ''} ${c.concept_key || ''}`);
        const metricGroupText = arr(s.metric_groups).map(g => `${g.title || ''} ${arr(g.metrics).join(' ')} ${g.shared_dedup_key || ''} ${arr(g.shared_filters).join(' ')} ${arr(g.quality_filters).join(' ')}`);
        const hay = [item.title, item.path, item.business_topic, item.status, item.canonical_rule_status, s.purpose, s.business_question, s.base_population, s.grain, ...(item.summary?.metrics || []), ...(item.summary?.dimensions || []), ...(item.summary?.filters || []), ...(item.summary?.source_logs || []), ...arr(s.logic_summary), ...ruleText, ...criteriaText, ...metricGroupText, searchablePackageText(item)].join(' ').toLowerCase();
        if (topic && item.business_topic !== topic) return false;
        if (log && !arr(item.summary?.source_logs).includes(log)) return false;
        if (dashboard === 'with' && !arr(item.dashboard_attachments).length) return false;
        if (dashboard === 'without' && arr(item.dashboard_attachments).length) return false;
        if (ruleStatus && item.canonical_rule_status !== ruleStatus) return false;
        if (status && item.status !== status) return false;
        if (q && !hay.includes(q)) return false;
        return true;
      });
    }
    function renderList() {
      const items = filteredItems();
      if (!items.find(i => i.state_key === activeKey)) activeKey = (items[0] || {}).state_key || '';
      document.getElementById('list').innerHTML = items.map(item => {
        const cls = item.state_key === activeKey ? ' active' : '';
        const dash = arr(item.dashboard_attachments).length ? `看板 ${arr(item.dashboard_attachments).length}` : '未转看板';
        const completeness = item.package_completeness || {};
        return `<div class="item${cls}" onclick="activeKey='${esc(item.state_key)}'; render();">
          <div class="item-title">${esc(item.title)}</div>
          <div class="item-brief">${esc(item.formal_asset_id || item.package_id)}；成员 ${esc(completeness.member_count || arr(item.members).length)}</div>
          <div class="item-brief">指标：${esc(compactLabels(item.summary?.metrics, 3))}</div>
          <div class="item-brief">日志：${esc(compactLabels(item.summary?.source_logs, 2))}</div>
          <div class="item-path">${esc(item.path)}</div>
          <div class="chips">${chip(item.business_topic)}${chip(ruleStatusText(item.canonical_rule_status), ruleChipClass(item.canonical_rule_status))}${chip(item.status, statusClass(item.status))}${chip(dash, dash === '未转看板' ? 'warn' : 'ok')}</div>
        </div>`;
      }).join('') || '<div class="empty">没有匹配的正式资产包</div>';
    }
    function renderMetricGroups(summary) {
      const groups = arr(summary?.metric_groups);
      if (!groups.length) return '<span class="sub">无指标组摘要</span>';
      return groups.map(group => `<div class="metric">
        <div class="hero">
          <div>
            <strong>${esc(group.title || '同一统计口径指标组')}</strong>
            <div class="sub">这组指标共享同一套 Base、筛选、维度和去重逻辑。</div>
          </div>
          <div>${chip(`${arr(group.metrics).length} 个指标`, 'neutral')}</div>
        </div>
        <div class="kv"><span>指标</span><span>${pills(group.metrics)}</span></div>
        <div class="kv"><span>去重/聚合</span><span>${esc(group.shared_dedup_key || '无特殊去重说明')}</span></div>
        <div class="kv"><span>共同维度</span><span>${pills(group.shared_dimensions)}</span></div>
        <div class="kv"><span>共同业务筛选</span><span>${listHtml(group.shared_filters, '无', 12)}</span></div>
        ${arr(group.quality_filters).length ? `<details><summary>数据质量条件</summary>${listHtml(group.quality_filters, '无', 12)}</details>` : ''}
        ${arr(group.ratio_notes).length ? `<div class="kv"><span>分子/分母</span><span>${listHtml(group.ratio_notes, '无', 8)}</span></div>` : ''}
        ${arr(group.metric_notes).length ? `<details><summary>指标补充说明</summary>${listHtml(group.metric_notes, '无', 12)}</details>` : ''}
      </div>`).join('');
    }
    function criteriaStatusText(value) {
      const text = String(value || '').toLowerCase();
      if (text === 'conflict') return '与保存口径冲突';
      if (text === 'matched') return '命中保存口径';
      if (text === 'needs_manual_check') return '需人工确认';
      return 'SQL 独有';
    }
    function criteriaChipClass(value) {
      const text = String(value || '').toLowerCase();
      if (text === 'conflict') return 'bad';
      if (text === 'matched') return 'ok';
      if (text === 'needs_manual_check') return 'warn';
      return 'neutral';
    }
    function savedRuleDetail(row, summaryLabel='查看命中的保存口径') {
      const linked = row.rule_title || row.title || row.rule_id || row.concept_key;
      if (!linked) return '';
      const brief = row.rule_display || row.rule_summary || row.message || '';
      const raw = row.full_rule || '';
      const body = brief || raw || '无结构化口径摘要';
      const meta = [
        ['保存口径', row.rule_title || row.title || '未命名保存口径'],
        ['rule_id', row.rule_id || '无'],
        ['concept_key', row.concept_key || '无'],
        ['状态', row.saved_rule_status || row.result || ''],
        ['证据来源', row.evidence || ''],
      ].map(pair => `<span>${esc(pair[0])}</span><span>${esc(pair[1])}</span>`).join('');
      const rawBlock = raw && raw !== body ? `<details><summary>展开完整规则原文</summary><pre class="rule-text">${esc(raw)}</pre></details>` : '';
      return `<details><summary>${esc(summaryLabel)}</summary><div class="rule-meta">${meta}</div><pre class="rule-text">${esc(body)}</pre>${rawBlock}</details>`;
    }
    function renderAppliedCriteria(summary) {
      const rows = arr(summary?.applied_criteria);
      const nonCoreCategories = new Set(['数据质量条件', '执行裁剪条件', '时间范围']);
      const coreRowsRaw = rows.filter(row => !nonCoreCategories.has(row.category));
      const qualityRowsRaw = rows.filter(row => nonCoreCategories.has(row.category));
      const counts = coreRowsRaw.reduce((acc, row) => {
        const key = row.saved_rule_status || 'unique';
        acc[key] = (acc[key] || 0) + 1;
        return acc;
      }, {});
      const statusPriority = { conflict: 0, matched: 1, needs_manual_check: 2, unique: 3 };
      const categoryPriority = { '数据来源口径': 0, '统计对象口径': 1, '固定筛选口径': 2, '计算口径': 3, '统计口径': 4, '项目口径': 5, '时间范围': 8, '执行裁剪条件': 9, '数据质量条件': 10 };
      const ordered = [...rows].sort((a, b) => (statusPriority[a.saved_rule_status] ?? 9) - (statusPriority[b.saved_rule_status] ?? 9) || (categoryPriority[a.category] ?? 8) - (categoryPriority[b.category] ?? 8));
      const coreRows = ordered.filter(row => !nonCoreCategories.has(row.category));
      const qualityRows = ordered.filter(row => nonCoreCategories.has(row.category));
      const renderRows = part => part.map(row => {
        const cls = ruleClass(row.saved_rule_status);
        const linked = row.rule_title || row.rule_id || row.concept_key;
        return `<div class="rule-panel ${cls}">
          <div class="rule-head">
            <div>
              <strong>${esc(row.name || '')}</strong>
              <div class="sub">${esc(row.category || '口径')}${linked ? '；保存口径：' + esc(row.rule_title || row.rule_id || row.concept_key) : ''}</div>
            </div>
            ${chip(criteriaStatusText(row.saved_rule_status), criteriaChipClass(row.saved_rule_status))}
          </div>
          <div style="margin-top:8px; line-height:1.55;">${esc(row.description || '')}</div>
          ${linked ? savedRuleDetail(row) : ''}
        </div>`;
      }).join('');
      if (!rows.length) {
        return `<div class="card"><h2>本 SQL 使用的口径</h2><div class="sub">未整理出结构化口径。</div></div>`;
      }
      return `<div class="card">
        <div class="hero">
          <h2>本 SQL 使用的口径</h2>
          <div class="chips">${chip(`命中 ${counts.matched || 0}`, 'ok')}${chip(`冲突 ${counts.conflict || 0}`, counts.conflict ? 'bad' : 'neutral')}${chip(`待确认 ${counts.needs_manual_check || 0}`, counts.needs_manual_check ? 'warn' : 'neutral')}${chip(`独有 ${counts.unique || 0}`, 'neutral')}</div>
        </div>
        ${renderRows(coreRows)}
        ${qualityRows.length ? `<details><summary>时间 / 执行 / 数据质量条件 ${qualityRows.length} 条</summary>${renderRows(qualityRows)}</details>` : ''}
      </div>`;
    }
    function renderRuleDetails(checks) {
      const rows = arr(checks);
      if (!rows.length) {
        return `<div class="rule-panel unique">
          <div class="rule-head">
            <div>
              <strong>独有 / 未命中口径库</strong>
              <div class="sub">未在当前项目已保存口径中找到可直接关联的规则。</div>
            </div>
            ${chip('unique', 'warn')}
          </div>
          <div style="margin-top:8px; line-height:1.55;">这不代表 SQL 错误，只表示这条查询的业务逻辑目前更像一次独立分析；复用前应判断是否需要沉淀为项目口径。</div>
        </div>`;
      }
      return rows.map(rule => {
        const cls = ruleClass(rule.result);
        const detail = [rule.evidence, rule.rule_id ? `rule_id=${rule.rule_id}` : '', rule.concept_key ? `concept_key=${rule.concept_key}` : ''].filter(Boolean).join('；');
        return `<div class="rule-panel ${cls}">
          <div class="rule-head">
            <div>
              <strong>${esc(rule.title || '已保存口径')}</strong>
              <div class="sub">${esc(rule.status || '')}${detail ? '；' + esc(detail) : ''}</div>
            </div>
            ${chip(rule.result || 'mentioned', statusClass(rule.result))}
          </div>
          <div style="margin-top:8px; line-height:1.55;">${esc(rule.message || rule.rule_display || rule.rule_summary || '')}</div>
          ${savedRuleDetail({...rule, saved_rule_status: rule.result}, '查看口径原文')}
        </div>`;
      }).join('');
    }
    function renderRuleStatus(summary) {
      const status = summary?.canonical_rule_status || 'unique';
      const checks = arr(summary?.canonical_rule_checks);
      const first = checks[0] || {};
      const summaryText = status === 'unique'
        ? '未命中当前项目口径库，作为独立查询逻辑保存。'
        : (first.message || first.rule_display || first.rule_summary || `${checks.length} 条口径依据`);
      return `<div class="card">
        <div class="hero"><h2>口径状态</h2><div>${chip(ruleStatusText(status), ruleChipClass(status))}</div></div>
        <div style="line-height:1.55;">${esc(summaryText)}</div>
        <details open><summary>口径依据 ${checks.length ? checks.length + ' 条' : '：独有'}</summary>${renderRuleDetails(checks)}</details>
      </div>`;
    }
    function provenanceText(prov) {
      if (!prov || !Object.keys(prov).length) return '生成来源未记录';
      const skill = prov.skill_name || 'sql-engineering';
      const version = prov.skill_version || 'unknown';
      const workflow = prov.workflow || 'unknown';
      const script = prov.generated_by_script || 'unknown';
      const source = prov.source || 'generated';
      const spec = prov.sql_spec_version || 'unknown';
      return `${skill} v${version} / spec ${spec} / ${workflow} / ${script} / ${source}`;
    }
    function renderProvenance(prov, origin) {
      return `<div class="card">
        <h2>资产生成来源</h2>
        <div class="kv"><span>生成来源</span><span>${esc(provenanceText(prov))}</span></div>
        <div class="kv"><span>SQL Spec</span><span>${esc(prov?.sql_spec_version || '未记录')}</span></div>
        <div class="kv"><span>生成时间</span><span>${esc(prov?.generated_at || '未记录')}</span></div>
        <div class="kv"><span>保存时间</span><span>${esc(prov?.saved_at || '未记录')}</span></div>
        <div class="kv"><span>保存脚本</span><span>${esc(prov?.saved_by_script || '未记录')}</span></div>
        <div class="kv"><span>来源查询</span><span>${origin?.path ? `${esc(origin.path)} (${esc(origin.query_id || '')} v${esc(origin.version || '')})` : '历史资产未记录'}</span></div>
        ${prov?.backfilled_by_script ? `<div class="kv"><span>历史回填</span><span>${esc(prov.backfilled_by_script)}；skill ${esc(prov.backfilled_by_skill_version || 'unknown')}；${esc(prov.backfilled_at || '未记录')}</span></div>` : ''}
      </div>`;
    }
    function renderRepositorySnapshot(item, summary, evidence) {
      const dashboards = arr(item.dashboard_attachments).length;
      const ruleStatus = summary?.canonical_rule_status || item.canonical_rule_status || 'unique';
      const criteria = arr(summary?.applied_criteria);
      const matchedCriteria = criteria.filter(row => row.saved_rule_status === 'matched').length;
      const sourceLabels = arr(summary?.source_logs).length ? summary.source_logs : arr(summary?.external_sources).map(s => s.table || s.physical_table || s.name).filter(Boolean);
      const sourceTitle = arr(summary?.source_logs).length ? '原始日志' : '外部权威表';
      return `<div class="summary-strip">
        <div class="summary-tile"><strong>实际使用口径</strong><div>${chip(ruleStatusText(ruleStatus), ruleChipClass(ruleStatus))}</div><div class="sub">${criteria.length || 0} 条；${matchedCriteria} 条命中保存口径</div></div>
        <div class="summary-tile"><strong>指标组</strong><div class="big">${arr(summary?.metric_groups).length || 0}</div><div class="sub">${arr(summary?.metrics).length || 0} 个输出指标</div></div>
        <div class="summary-tile"><strong>${sourceTitle}</strong><div>${pills(sourceLabels)}</div></div>
        <div class="summary-tile"><strong>结果/看板</strong><div>${chip(evidence?.status || 'missing', statusClass(evidence?.status))}${chip(dashboards ? '已有看板' : '未转看板', dashboards ? 'ok' : 'warn')}</div><div class="sub">行数：${esc(evidence?.row_count ?? '未记录')}</div></div>
      </div>`;
    }
    function renderRepoStats() {
      const rows = arr(payload.items);
      document.getElementById('repoStats').innerHTML = `
        <div class="repo-stat"><strong>${rows.length}</strong><span class="sub">正式资产包</span></div>
        <div class="repo-stat"><strong>${payload.package_member_count || 0}</strong><span class="sub">Package 成员</span></div>
        <div class="repo-stat"><strong>${payload.derived_output_count || 0}</strong><span class="sub">衍生产物</span></div>
        <div class="repo-stat"><strong>${payload.dashboard_attachment_count || 0}</strong><span class="sub">看板附件</span></div>`;
    }
    function dashboardSummaryText(summary) {
      const row = key => arr(summary?.[key]).join('、') || '无';
      return `指标：${row('metrics')}\n维度：${row('dimensions')}\n筛选项：${row('filters')}\n统计周期：${summary?.period || summary?.statistical_period || '无'}`;
    }
    function renderDisplayRules(item) {
      const rules = arr(item.summary?.display_rules || item.spec?.display_rules);
      if (!rules.length) return '';
      return `<div class="card">
        <h2>展示格式</h2>
        <table><thead><tr><th>字段</th><th>原始尺度</th><th>展示</th><th>样例</th></tr></thead><tbody>
          ${rules.map(rule => {
            const sample = rule.sample_check || {};
            const raw = sample.raw_value ?? '';
            const shown = sample.display_value ?? '';
            const suffix = rule.display_suffix || '';
            const decimals = Number.isInteger(rule.decimal_places) ? `${rule.decimal_places} 位` : '';
            return `<tr>
              <td>${esc(rule.output_field || '')}</td>
              <td>${esc(rule.source_value_scale || '')}</td>
              <td>${esc([rule.display_format, decimals, suffix].filter(Boolean).join(' / '))}</td>
              <td>${esc(String(raw))}${shown !== '' ? ' -> ' + esc(String(shown)) : ''}</td>
            </tr>`;
          }).join('')}
        </tbody></table>
        <div class="sub">来自固化结果文件的保留字段契约；SQL 保留原始数值，展示层按规则格式化。</div>
      </div>`;
    }
    function renderDashboards(item) {
      const rows = arr(item.dashboard_attachments);
      if (!rows.length) return `<div class="card"><h2>看板附件</h2><div class="sub">未转看板。需要时从这条查询资产继续验证并晋升。</div></div>`;
      return `<div class="card"><h2>看板附件</h2>${rows.map((dash, idx) => `
        <div class="metric">
          <strong>${esc(dash.title)}</strong>
          <div class="sub">${esc(dash.path)}；${esc(dash.status || '')}</div>
          <div class="sub">生成来源：${esc(provenanceText(dash.generation_provenance))}</div>
          <div class="button-row">
            <button onclick="copyText(payload.items.find(i => i.state_key === activeKey).dashboard_attachments[${idx}].source_sql.text, 'dashCopy${idx}')">复制看板 SQL</button>
            <button onclick="copyText(dashboardSummaryText(payload.items.find(i => i.state_key === activeKey).dashboard_attachments[${idx}].dashboard_summary), 'dashCopy${idx}')">复制看板摘要</button>
            <button onclick="location.href='${esc(dash.dashboard_review_html)}'">打开 dashboard_review.html</button>
            <span id="dashCopy${idx}" class="sub"></span>
          </div>
          <details><summary>看板摘要</summary><pre>${esc(dashboardSummaryText(dash.dashboard_summary))}</pre></details>
        </div>`).join('')}</div>`;
    }
    function renderKnowledgeReferences(item) {
      const rows = arr(item.knowledge_references);
      if (!rows.length) return '';
      return `<div class="card"><h2>资料引用</h2>${rows.map(row => `
        <div class="metric">
          <strong>${esc(row.dataset_id || '')} / ${esc(row.projection_id || '')}</strong>
          <div class="sub">版本：${esc(row.dataset_version || '')}；使用方式：${esc(row.usage_mode || '')}；生成 skill：${esc(row.dataset_skill_version || 'unknown')}</div>
          <div class="sub">字段：${esc(arr(row.fields).join('、') || '未记录')}</div>
        </div>`).join('')}</div>`;
    }
    function packageMemberList(rows, empty='未登记') {
      rows = arr(rows);
      if (!rows.length) return `<span class="sub">${esc(empty)}</span>`;
      return `<ul>${rows.slice(0, 16).map(row => `<li><strong>${esc(row.role || '')}</strong>${row.member_id ? ` ${esc(row.member_id)}` : ''}<div class="sub">${esc(row.path || '')}</div></li>`).join('')}</ul>`;
    }
    function renderPackageContents(item) {
      const completeness = item.package_completeness || {};
      const receipt = item.latest_receipt || {};
      const receiptBody = receipt.receipt || {};
      const lineage = arr(item.lineage);
      const members = arr(item.members);
      return `<div class="card">
        <div class="hero">
          <div>
            <h2>资产包内容</h2>
            <div class="sub">${esc(item.package_manifest_path || item.path || '')}</div>
          </div>
          <div class="chips">${chip(item.formal_asset_id || item.package_id, 'neutral')}${chip(`R${item.package_revision || item.version || ''}`, 'neutral')}${chip(receipt.available ? '回执可用' : '回执缺失', receipt.available ? 'ok' : 'bad')}</div>
        </div>
        <div class="detail-grid">
          <div class="mini-panel"><h3>查询成员 ${esc(completeness.query_count || arr(item.queries).length)}</h3>${packageMemberList(item.queries)}</div>
          <div class="mini-panel"><h3>结果证据 ${esc(completeness.evidence_count || arr(item.evidence_members).length)}</h3>${packageMemberList(item.evidence_members)}</div>
          <div class="mini-panel"><h3>衍生产物 ${esc(completeness.derived_output_count || arr(item.derived_outputs).length)}</h3>${packageMemberList(item.derived_outputs)}</div>
          <div class="mini-panel"><h3>验证成员 ${esc(completeness.validation_member_count || arr(item.validations).length)}</h3>${packageMemberList(item.validations)}</div>
          <div class="mini-panel"><h3>看板 ${esc(completeness.dashboard_count || arr(item.dashboard_attachments).length)}</h3>${packageMemberList(item.dashboard_attachments)}</div>
          <div class="mini-panel"><h3>成员闭包</h3><div class="kv"><span>全部成员</span><span>${esc(completeness.member_count || members.length)}</span></div><div class="kv"><span>当前成员</span><span>${esc(arr(item.current_member_ids).length)}</span></div><div class="kv"><span>血缘边</span><span>${esc(lineage.length)}</span></div></div>
        </div>
        <details><summary>完整成员清单 ${members.length} 项</summary>${packageMemberList(members)}</details>
        <details><summary>显式血缘 ${lineage.length} 条</summary>${lineage.length ? `<ul>${lineage.map(edge => `<li>${esc(edge.relation || '')}: ${esc(edge.from_member_id || '')} -> ${esc(edge.to_member_id || '')}${edge.note ? `；${esc(edge.note)}` : ''}</li>`).join('')}</ul>` : '<span class="sub">未登记血缘。</span>'}</details>
        <details><summary>最新 Package 回执</summary><div class="kv"><span>路径</span><span>${esc(receipt.path || '')}</span></div><div class="kv"><span>receipt_id</span><span>${esc(receiptBody.receipt_id || '')}</span></div><div class="kv"><span>状态</span><span>${esc(receiptBody.status || (receipt.available ? 'ready' : 'missing'))}</span></div><div class="kv"><span>同步闭包</span><span>${esc(arr(receiptBody.files).length)} 个文件</span></div></details>
        ${arr(item.issues).length ? `<details open><summary>Package 诊断 ${arr(item.issues).length} 条</summary>${packageMemberList(arr(item.issues).map(issue => ({role: issue.code, path: `${issue.path || ''} ${issue.message || ''}`})))}</details>` : ''}
      </div>`;
    }
    function renderOrphans() {
      const rows = arr(payload.orphan_dashboard_attachments);
      if (!rows.length) return '';
      return `<details><summary>孤立看板资产 ${rows.length} 个</summary>${rows.map(row => `<div class="metric"><strong>${esc(row.title)}</strong><div class="sub">${esc(row.path)}</div></div>`).join('')}</details>`;
    }
    function renderDetail() {
      const item = currentItem();
      if (!item) { document.getElementById('detail').innerHTML = '<div class="empty">请选择一个正式资产包</div>'; return; }
      const s = item.repository_summary || {};
      const ev = s.result_evidence || item.summary?.result_evidence || {};
      document.getElementById('detail').innerHTML = `<div class="detail-stack">
        <div class="card hero">
          <div>
            <h2>${esc(item.title)}</h2>
            <div class="sub">${esc(item.path)}</div>
            <div class="chips">${chip(item.formal_asset_id || item.package_id, 'neutral')}${chip(item.business_topic)}${chip(ruleStatusText(item.canonical_rule_status), ruleChipClass(item.canonical_rule_status))}${chip(item.status, statusClass(item.status))}${chip(arr(item.dashboard_attachments).length ? '已有看板' : '未转看板', arr(item.dashboard_attachments).length ? 'ok' : 'warn')}</div>
          </div>
          <div>
            <button class="primary" onclick="copyText(currentItem().source_sql.text, 'heroCopyStatus')" ${item.source_sql?.text ? '' : 'disabled'}>复制主查询 SQL</button>
            <div id="heroCopyStatus" class="sub" style="margin-top:6px; text-align:right;">生成：${esc(payload.generated_at)}</div>
          </div>
        </div>
        ${renderPackageContents(item)}
        ${item.repository_snapshot?.status === 'ready' ? '' : `<div class="card"><h2>仓库摘要诊断</h2><div class="sub">${esc(arr(item.repository_snapshot?.problems).join('；') || '持久化摘要不可用')}</div></div>`}
        ${renderRepositorySnapshot(item, s, ev)}
        ${renderProvenance(item.generation_provenance, item.origin_query_workspace)}
        ${renderKnowledgeReferences(item)}
        <div class="card core-card">
          <h2>主查询是干什么的</h2>
          <div class="detail-grid">
            <div class="mini-panel"><h3>业务问题</h3><div>${esc(s.business_question || s.purpose || '未整理')}</div></div>
            <div class="mini-panel"><h3>Base / 统计对象</h3><div>${esc(s.base_population || '未整理')}</div></div>
            <div class="mini-panel"><h3>输出粒度</h3><div>${esc(s.grain || '未整理')}</div></div>
            <div class="mini-panel"><h3>原始日志</h3>${pills(s.source_logs)}</div>
            <div class="mini-panel"><h3>外部权威表</h3>${pills(arr(s.external_sources).map(x => x.table || x.physical_table || x.name).filter(Boolean))}</div>
          </div>
        </div>
        ${renderAppliedCriteria(s)}
        <div class="card">
          <h2>指标逻辑</h2>
          ${renderMetricGroups(s)}
        </div>
        <div class="card grid">
          <div><h2>维度</h2>${pills(s.dimensions)}</div>
          <div><h2>关键筛选</h2>${listHtml(s.filters)}</div>
          <div><h2>结果证据</h2><div class="kv"><span>状态</span><span>${esc(ev.status || 'missing')}</span></div><div class="kv"><span>行数</span><span>${esc(ev.row_count ?? '')}</span></div><div>${esc(ev.summary || '')}</div></div>
        </div>
        <div class="card">
          <h2>逻辑摘要</h2>
          ${listHtml(s.logic_summary, '无')}
        </div>
        ${renderDisplayRules(item)}
        <div class="card">
          <h2>主查询复跑</h2>
          <div class="button-row"><button class="primary" onclick="copyText(currentItem().source_sql.text, 'copyStatus')" ${item.source_sql?.text ? '' : 'disabled'}>复制主查询 SQL</button><span id="copyStatus" class="sub"></span></div>
          <details><summary>查看完整 SQL</summary><textarea class="sql-preview" readonly>${esc(item.source_sql?.text || '')}</textarea></details>
        </div>
        ${renderDashboards(item)}
        <details><summary>样例结果</summary>${sampleTable(item.sample)}<div class="sub">${esc(item.sample_meta?.note || '')}</div></details>
        <details><summary>代码 / 治理信息</summary>
          <div class="kv"><span>spec</span><span>${esc(item.spec?.path || '')}</span></div>
          <div class="kv"><span>输出字段</span><span>${pills(item.spec?.output_fields || [])}</span></div>
          <div class="kv"><span>技术源表</span><span>${pills(item.spec?.technical_sources || [])}</span></div>
          <pre>${esc(JSON.stringify({parse_errors:item.spec?.parse_errors || [], quality_gate:item.spec?.quality_gate || {}, performance:item.spec?.performance || {}}, null, 2))}</pre>
        </details>
        ${renderOrphans()}
      </div>`;
    }
    function render() {
      document.getElementById('meta').textContent = `项目：${payload.project}；正式资产包：${payload.package_count}；成员：${payload.package_member_count}；看板：${payload.dashboard_attachment_count}；诊断：${payload.issue_count}`;
      renderRepoStats();
      setupFilters();
      renderList();
      renderDetail();
    }
    async function refreshPayload() {
      if (!repositoryApiUrl) return;
      document.getElementById('meta').textContent = '正在读取最新 sql_repository 数据...';
      try {
        const response = await fetch(repositoryApiUrl);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        payload = normalizePayload(await response.json());
        if (!payload.items.find(item => item.state_key === activeKey)) {
          activeKey = (payload.items[0] || {}).state_key || '';
        }
        render();
      } catch (err) {
        document.getElementById('meta').textContent = '读取 sql_repository 数据失败：' + err;
        document.getElementById('detail').innerHTML = '<div class="empty">无法读取最新 sql_repository payload，请检查本地服务和 Formal Asset Package 清单。</div>';
      }
    }
    ['topicFilter','logFilter','dashboardFilter','ruleFilter','statusFilter','search'].forEach(id => document.getElementById(id).addEventListener('input', () => { renderList(); renderDetail(); }));
    render();
    refreshPayload();
  </script>
</body>
</html>
"""


def html_shell(payload: dict[str, Any] | None, api_url: str | None = None) -> str:
    return (
        HTML_TEMPLATE
        .replace("__PAYLOAD_JSON__", payload_for_html(payload))
        .replace("__API_URL_JSON__", json.dumps(api_url, ensure_ascii=False))
    )


def cmd_build(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    output = Path(args.output).resolve() if args.output else root / DEFAULT_HTML_REL
    json_output = Path(args.json_output).resolve() if args.json_output else root / DEFAULT_JSON_REL
    payload = build_payload(root, include_history=args.include_history, sample_limit=args.sample_rows, html_output=output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_shell(payload), encoding="utf-8")
    write_json(json_output, payload)
    print(f"sql_repository_html: {output}")
    print(f"sql_repository_json: {json_output}")
    print(f"formal_asset_packages: {payload['package_count']}")
    print(f"query_members: {payload['query_count']}")
    print(f"dashboard_attachments: {payload['dashboard_attachment_count']}")


def cmd_enrich(args: argparse.Namespace) -> None:
    del args
    raise SystemExit(
        "sql_repository enrich is retired: the repository viewer is read-only; "
        "update Package members through FormalAssetRepository ownership."
    )


class RepositoryHandler(BaseHTTPRequestHandler):
    root: Path
    include_history: bool
    sample_rows: int
    state_path: Path

    def send_text(self, status: int, content: str, content_type: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, status: int, payload: Any) -> None:
        self.send_text(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def repository_payload(self) -> dict[str, Any]:
        return build_payload(
            self.root,
            include_history=self.include_history,
            sample_limit=self.sample_rows,
            html_output=self.root / DEFAULT_HTML_REL,
        )

    def dashboard_review_payload(self) -> dict[str, Any]:
        return build_dashboard_review_payload(
            self.root,
            self.state_path,
            False,
            self.include_history,
            self.sample_rows,
        )

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html", "/sql_repository.html"}:
            self.send_text(200, html_shell(None, api_url="/api/repository"), "text/html; charset=utf-8")
            return
        if path == "/api/repository":
            self.send_json(200, self.repository_payload())
            return
        if path == "/api/dashboard-review":
            self.send_json(200, self.dashboard_review_payload())
            return
        if path in {"/dashboard_review.html", "/dashboard-review"}:
            self.send_text(200, dashboard_review_html_shell(None, api_url="/api/dashboard-review"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self.send_json(200, load_dashboard_review_state(self.state_path))
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
        data.setdefault("version", 1)
        data.setdefault("items", {})
        data["updated_at"] = now_iso()
        write_json(self.state_path, data)
        self.send_json(200, {"ok": True})

    def log_message(self, format, *args):  # noqa: A002
        sys.stderr.write("sql_repository: " + (format % args) + "\n")


def cmd_serve(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    state_path = Path(args.state_file).resolve() if args.state_file else root / DASHBOARD_REVIEW_STATE_REL
    state_path.parent.mkdir(parents=True, exist_ok=True)
    handler = type(
        "BoundRepositoryHandler",
        (RepositoryHandler,),
        {
            "root": root,
            "include_history": args.include_history,
            "sample_rows": args.sample_rows,
            "state_path": state_path,
        },
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"sql_repository_url: http://{args.host}:{server.server_port}")
    print(f"sql_repository_api: http://{args.host}:{server.server_port}/api/repository")
    print(f"dashboard_review_url: http://{args.host}:{server.server_port}/dashboard_review.html")
    print(f"dashboard_review_state: {state_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Generate Formal Asset Package repository HTML and JSON")
    build.add_argument("--root", required=True, help="Project root, such as sql-projects/DEMO_ANALYTICS")
    build.add_argument("--output")
    build.add_argument("--json-output")
    build.add_argument("--include-history", action="store_true", help="Include history/archived Packages and members for debugging")
    build.add_argument("--sample-rows", type=int, default=8)
    add_function_gate_arguments(build, selection_help="Optional explicit route, such as 【SQL仓库】 or [SQL_REPOSITORY].")
    build.set_defaults(func=cmd_build)

    enrich = sub.add_parser("enrich", help="Retired: repository viewing is read-only")
    enrich.add_argument("--root", required=True, help="Project root, such as sql-projects/DEMO_ANALYTICS")
    enrich.add_argument("--include-history", action="store_true", help=argparse.SUPPRESS)
    add_function_gate_arguments(enrich, selection_help="Optional explicit route, such as 【SQL仓库】 or [SQL_REPOSITORY].")
    enrich.set_defaults(func=cmd_enrich)

    serve = sub.add_parser("serve", help="Serve the Formal Asset Package repository and dashboard review")
    serve.add_argument("--root", required=True, help="Project root, such as sql-projects/DEMO_ANALYTICS")
    serve.add_argument("--state-file", help="Dashboard review state file. Defaults to reviews/dashboard_review_state.json")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--include-history", action="store_true", help="Include history/archived Packages and members for debugging")
    serve.add_argument("--sample-rows", type=int, default=8)
    add_function_gate_arguments(serve, selection_help="Optional explicit route, such as 【SQL仓库】 or [SQL_REPOSITORY].")
    serve.set_defaults(func=cmd_serve)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        purpose = "formal asset package repository"
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("sql_repository.py", args.command),
            purpose=purpose,
        )
        require_user_request(args.user_request, purpose=purpose)
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    args.func(args)


if __name__ == "__main__":
    main()
