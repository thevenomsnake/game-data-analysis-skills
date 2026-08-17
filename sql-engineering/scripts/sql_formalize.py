#!/usr/bin/env python3
"""Fast formalization path: source SQL + result evidence -> formal QUERY and optional DASHBOARD."""

from __future__ import annotations

import argparse
import copy
import hashlib
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from asset_provenance import apply_generation_provenance, merge_generation_provenance, stamp_sql_generation  # noqa: E402
from dashboard_review import validate_top_contract  # noqa: E402
from config_knowledge import validate_knowledge_reference  # noqa: E402
from knowledge_usage import (  # noqa: E402
    build_knowledge_usage,
    load_reference_files as load_knowledge_reference_files,
    validate_knowledge_usage,
)
from project_rules import rules_fingerprint  # noqa: E402
from rule_application import (  # noqa: E402
    RULE_APPLICATION_VERSION,
    application_integrity_ok,
    build_inheritance_contract,
)
from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from sql_artifact_builder import (  # noqa: E402
    dashboard_blockers,
    dashboard_spec,
    performance_level,
    query_spec,
    validation_spec,
)
from sql_param_normalizer import dashboardize_time_params, normalize_query_sql  # noqa: E402
from sql_execution_adapter import (  # noqa: E402
    effective_config_for_context,
    execution_route_for_file,
    rebase_execution_route_for_sql,
    route_receipt_path,
    route_config_fingerprint,
    route_matches_context,
)
from sql_project import (  # noqa: E402
    DEFAULT_ANALYSIS_TYPE,
    DEFAULT_BUSINESS_CATEGORY,
    REPLACEMENT_CHANGE_TYPES,
    RESULT_FILE_EXTENSIONS,
    artifact_dir,
    compose_generation_gate,
    csv_or_inferred,
    evaluate_rule_context,
    is_current_artifact,
    manifest_path,
    markdown_value,
    next_artifact_version,
    now_iso,
    now_stamp,
    query_params_contract_problems,
    project_context_snapshot,
    project_execution_contract_check,
    read_json,
    read_project_config,
    rebuild_index,
    resolve_change_type,
    slugify,
    strip_source_prefix,
    validate_project_config,
    write_artifact_meta,
    write_json,
)
from sql_output_contract import normalize_field_name, prune_final_select_to_result_columns, prune_internal_cte_outputs  # noqa: E402
from sql_query_workspace import (  # noqa: E402
    external_source_intake,
    find_query_reference,
    is_project_local as is_query_project_local,
    mark_promoted as mark_workspace_query_promoted,
    origin_contract as public_query_workspace_origin,
    save_query as save_workspace_query,
    transition_query as transition_workspace_query,
)
from sql_result_inspector import (  # noqa: E402
    inspect_result_file,
    time_coverage_problem_messages,
)
from result_evidence_retention import (  # noqa: E402
    prepare_result_evidence,
    write_retained_result,
)
from sql_semantic_summary import build_repository_summary, needs_llm_summary  # noqa: E402
from sql_facts import build_sql_fact_bundle, execution_fingerprint, logic_fingerprint, sql_side_privacy_transforms  # noqa: E402
from capability_registry import command_function_ids  # noqa: E402
from temporary_rule_override import unresolved_temporary_rule_override  # noqa: E402
from spec_utils import build_short_header, has_full_spec_block, replace_or_prepend_short_header, set_spec_version, strip_any_short_header, write_json_object  # noqa: E402
from formal_asset_repository import (  # noqa: E402
    FormalAssetRepositoryError,
    apply_plan as apply_formal_asset_plan,
    list_packages as list_formal_asset_packages,
    load_package as load_formal_asset_package,
    plan_package as plan_formal_asset_package,
    validate_receipt as validate_formal_asset_receipt,
)


SEMANTIC_SUMMARY_CACHE_VERSION = "formalize_repository_summary_cache_v2"
FORMAL_QUERY_SQL_ROLES = frozenset(
    {"formal_query", "formal_query_unverified", "formal_query_sql", "query_sql"}
)
FORMAL_QUERY_META_ROLES = frozenset({"formal_query_meta", "query_meta"})
FORMAL_QUERY_SPEC_ROLES = frozenset({"formal_query_spec", "query_spec"})
FORMAL_VALIDATION_ROLES = frozenset({"validation_sql", "validation_spec", "validation_meta"})
FORMAL_DASHBOARD_ROLES = frozenset(
    {
        "dashboard_delivery_sql",
        "dashboard_delivery_spec",
        "dashboard_delivery_meta",
        "dashboard_sql",
        "dashboard_spec",
        "dashboard_meta",
    }
)


@dataclass
class FormalizeBundle:
    """Reusable facts shared by QUERY, VALIDATION, DASHBOARD, and viewers."""

    source: str
    sql_fingerprint: str
    logic_fingerprint: str
    normalized_changed: bool
    result_schema_fingerprint: str
    project_config_fingerprint: str
    project_rules_fingerprint: str
    fact_bundle_source: str
    analysis: dict[str, Any]
    sql_facts: dict[str, Any]
    rule_context_status: str
    repository_summary_quality: str
    performance_fingerprint: str
    performance_level: dict[str, Any] | None = None
    output_field_contract: dict[str, Any] | None = None
    fact_reuse_summary: dict[str, Any] | None = None
    knowledge_references: list[dict[str, Any]] | None = None
    knowledge_usage: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        payload = {
            "schema_version": "formalize_bundle_v2",
            "source": self.source,
            "sql_fingerprint": self.sql_fingerprint,
            "logic_fingerprint": self.logic_fingerprint,
            "normalized_changed": self.normalized_changed,
            "result_schema_fingerprint": self.result_schema_fingerprint,
            "project_config_fingerprint": self.project_config_fingerprint,
            "project_rules_fingerprint": self.project_rules_fingerprint,
            "fact_bundle_source": self.fact_bundle_source,
            "sql_facts": copy.deepcopy(self.sql_facts),
            "analysis": {
                "business_category": self.analysis.get("business_category", ""),
                "analysis_type": self.analysis.get("analysis_type", ""),
                "tables": self.analysis.get("tables", []),
                "metrics": self.analysis.get("metrics", []),
                "dimensions": self.analysis.get("dimensions", []),
                "grain": self.analysis.get("grain", ""),
                "time_grain": self.analysis.get("time_grain", ""),
            },
            "rule_context_status": self.rule_context_status,
            "repository_summary_quality": self.repository_summary_quality,
            "performance_fingerprint": self.performance_fingerprint,
        }
        if self.performance_level:
            payload["performance_level"] = self.performance_level
        if self.output_field_contract:
            payload["output_field_contract"] = self.output_field_contract
        if self.fact_reuse_summary:
            payload["fact_reuse_summary"] = self.fact_reuse_summary
        if self.knowledge_references:
            payload["knowledge_references"] = copy.deepcopy(self.knowledge_references)
        if self.knowledge_usage:
            payload["knowledge_usage"] = copy.deepcopy(self.knowledge_usage)
        return payload


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def config_fingerprint(config: dict[str, Any]) -> str:
    payload = json.dumps(config or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def semantic_summary_cache_dir(root: Path) -> Path:
    return root / "reviews" / ".sql_formalize_summary_cache"


def semantic_summary_cache_metadata(
    root: Path,
    *,
    sql: str,
    result: dict[str, Any],
    title: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    result_columns = [str(item) for item in result.get("columns", [])]
    payload = {
        "cache_version": SEMANTIC_SUMMARY_CACHE_VERSION,
        "logic_fingerprint": logic_fingerprint(sql),
        "result_schema_fingerprint": str(result.get("schema_fingerprint") or ""),
        "result_columns": result_columns,
        "title": strip_source_prefix(title),
        "project_config_fingerprint": config_fingerprint(config),
        "project_rules_fingerprint": project_rules_fingerprint(root),
    }
    cache_key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "cache_version": SEMANTIC_SUMMARY_CACHE_VERSION,
        "cache_key": cache_key,
        "cache_file": str(semantic_summary_cache_dir(root) / f"{cache_key}.json"),
        **payload,
    }


def current_result_evidence(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "passed",
        "row_count": result.get("row_count"),
        "columns": result.get("columns", []),
        "schema_fingerprint": result.get("schema_fingerprint"),
        "file_name": result.get("file_name"),
        "result_time_coverage": copy.deepcopy(result.get("time_coverage") or {}),
    }


def retained_result_evidence(result: dict[str, Any]) -> dict[str, Any]:
    columns = [str(item or "").strip() for item in result.get("columns", []) if str(item or "").strip()]
    sample_rows = [row for row in result.get("sample_rows", []) if isinstance(row, dict)]
    payload: dict[str, Any] = {
        "contract_version": "result_output_contract_v1",
        "row_count": result.get("row_count"),
        "columns": columns,
        "sample_rows": sample_rows,
        "schema_fingerprint": result.get("schema_fingerprint") or "",
        "file_name": result.get("file_name") or "",
        "file_type": result.get("file_type") or "",
    }
    ratio_rules = result.get("ratio_field_rules")
    if isinstance(ratio_rules, list):
        payload["ratio_field_rules"] = ratio_rules
    time_coverage = result.get("time_coverage")
    if isinstance(time_coverage, dict) and time_coverage:
        payload["result_time_coverage"] = copy.deepcopy(time_coverage)
    override = result.get("retained_fields_override")
    if isinstance(override, dict) and override:
        payload["retained_fields_override"] = override
        payload["original_columns"] = override.get("original_columns", [])
        payload["removed_columns"] = override.get("removed_columns", [])
    output_contract = result.get("output_field_contract")
    if isinstance(output_contract, dict) and output_contract:
        payload["output_field_contract"] = output_contract
    return payload


def load_cached_repository_summary(cache_meta: dict[str, Any], *, result: dict[str, Any]) -> dict[str, Any] | None:
    if not cache_meta.get("result_schema_fingerprint"):
        return None
    path = Path(str(cache_meta.get("cache_file") or ""))
    if not path.exists():
        return None
    try:
        payload = load_json_object(path)
    except Exception:
        return None
    if payload.get("cache_version") != SEMANTIC_SUMMARY_CACHE_VERSION:
        return None
    if payload.get("cache_key") != cache_meta.get("cache_key"):
        return None
    summary = payload.get("repository_summary")
    if not isinstance(summary, dict) or not complete_repository_summary(summary):
        return None
    summary = copy.deepcopy(summary)
    summary["result_evidence"] = current_result_evidence(result)
    summary["semantic_summary_status"] = "semantic_cache_reused"
    summary["semantic_summary_quality"] = summary.get("semantic_summary_quality") or "cached"
    return summary


def write_repository_summary_cache(cache_meta: dict[str, Any], summary: dict[str, Any]) -> bool:
    if not cache_meta or not cache_meta.get("result_schema_fingerprint") or not complete_repository_summary(summary) or needs_llm_summary(summary):
        return False
    path = Path(str(cache_meta.get("cache_file") or ""))
    if not path.name:
        return False
    payload = {
        "cache_version": SEMANTIC_SUMMARY_CACHE_VERSION,
        "cache_key": cache_meta.get("cache_key"),
        "created_at": now_iso(),
        "metadata": {key: value for key, value in cache_meta.items() if key != "cache_file"},
        "repository_summary": summary,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)
    return True


def parse_retained_fields_text(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    return [item.strip().strip("`\"'") for item in re.split(r"[\n,，;；]+", text) if item.strip()]


def load_retained_fields(args) -> tuple[list[str], str, str]:
    inline = parse_retained_fields_text(getattr(args, "retained_fields", "") or "")
    file_value = getattr(args, "retained_fields_file", None)
    if not file_value:
        return inline, "inline" if inline else "", ""
    path = Path(file_value).resolve()
    if not path.exists():
        return inline, str(path), f"retained fields file not found: {path}"
    file_fields = parse_retained_fields_text(read_text(path))
    if inline and file_fields:
        return [], str(path), "Use either --retained-fields or --retained-fields-file, not both."
    return file_fields or inline, str(path), ""


def result_schema_fingerprint(result: dict[str, Any]) -> str:
    payload = {
        "columns": result.get("columns", []),
        "sample": (result.get("sample_rows", []) or [])[:3],
        "row_count": result.get("row_count"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def apply_retained_fields_override(result: dict[str, Any], retained_fields: list[str], *, source: str) -> tuple[dict[str, Any], list[str]]:
    if not retained_fields:
        return result, []
    updated = copy.deepcopy(result)
    original_columns = [str(item or "").strip() for item in updated.get("columns", [])]
    original_by_norm = {normalize_field_name(item): item for item in original_columns if item}
    missing: list[str] = []
    resolved: list[str] = []
    seen: set[str] = set()
    for requested in retained_fields:
        norm = normalize_field_name(requested)
        actual = original_by_norm.get(norm)
        if not actual:
            missing.append(requested)
            continue
        if norm in seen:
            continue
        seen.add(norm)
        resolved.append(actual)
    if missing:
        updated["retained_fields_override"] = {
            "status": "mismatch",
            "source": source,
            "requested_fields": retained_fields,
            "original_columns": original_columns,
            "missing_fields": missing,
        }
        return updated, missing
    removed = [column for column in original_columns if normalize_field_name(column) not in seen]
    old_fingerprint = str(updated.get("schema_fingerprint") or "")
    updated["columns"] = resolved
    if isinstance(updated.get("sample_rows"), list):
        updated["sample_rows"] = [
            {field: row.get(field, "") for field in resolved}
            for row in updated.get("sample_rows", [])
            if isinstance(row, dict)
        ]
    if isinstance(updated.get("ratio_field_rules"), list):
        retained_norms = {normalize_field_name(field) for field in resolved}
        updated["ratio_field_rules"] = [
            item
            for item in updated.get("ratio_field_rules", [])
            if isinstance(item, dict) and normalize_field_name(str(item.get("output_field") or "")) in retained_norms
        ]
    updated["schema_fingerprint"] = result_schema_fingerprint(updated)
    updated["retained_fields_override"] = {
        "status": "applied",
        "source": source,
        "requested_fields": retained_fields,
        "original_columns": original_columns,
        "retained_fields": resolved,
        "removed_columns": removed,
        "schema_fingerprint_before": old_fingerprint,
        "schema_fingerprint_after": updated.get("schema_fingerprint"),
    }
    return updated, []


def load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def candidate_fact_bundle_paths(source_sql: Path) -> list[Path]:
    return [
        source_sql.with_name(f"{source_sql.stem}.formalize_seed.json"),
        source_sql.with_name(f"{source_sql.stem}.spec.json"),
        source_sql.with_name(f"{source_sql.stem}.fact_bundle.json"),
        source_sql.with_suffix(source_sql.suffix + ".formalize_seed.json"),
        source_sql.with_suffix(source_sql.suffix + ".spec.json"),
        source_sql.with_suffix(source_sql.suffix + ".fact_bundle.json"),
    ]


def inline_fact_bundle(raw_sql: str) -> dict[str, Any]:
    match = re.search(r"/\*\s*@FORMALIZE_SEED\s*(\{.*?\})\s*@END_FORMALIZE_SEED\s*\*/", raw_sql, flags=re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"parse_error": "inline @FORMALIZE_SEED is not valid JSON"}
    return data if isinstance(data, dict) else {}


def load_fact_bundle(args, source_sql: Path, raw_sql: str) -> tuple[dict[str, Any], str, list[str]]:
    if args.use_fact_bundle == "off":
        return {}, "disabled", []
    warnings: list[str] = []
    if args.fact_bundle:
        path = Path(args.fact_bundle).resolve()
        if path.exists():
            return load_json_object(path), str(path), warnings
        return {}, "missing_explicit", [f"fact bundle not found: {path}"]
    for path in candidate_fact_bundle_paths(source_sql):
        if path.exists():
            return load_json_object(path), str(path), warnings
    inline = inline_fact_bundle(raw_sql)
    if inline:
        if inline.get("parse_error"):
            warnings.append(str(inline["parse_error"]))
            return {}, "inline_parse_error", warnings
        return inline, "inline_sql_comment", warnings
    if args.use_fact_bundle == "required":
        warnings.append("--use-fact-bundle required but no formalize seed/fact bundle was found.")
    return {}, "not_found", warnings


def seeded_repository_summary(summary: dict[str, Any], seed: dict[str, Any], *, result: dict[str, Any]) -> dict[str, Any]:
    seed_summary = seed.get("repository_summary") if isinstance(seed.get("repository_summary"), dict) else {}
    if not seed_summary and isinstance(seed.get("formalize_seed"), dict):
        nested = seed["formalize_seed"].get("repository_summary")
        seed_summary = nested if isinstance(nested, dict) else {}
    if not seed_summary:
        return summary
    merged = {**summary}
    for key, value in seed_summary.items():
        if value not in (None, "", []):
            merged[key] = value
    merged["result_evidence"] = summary.get("result_evidence") or {
        "status": "passed",
        "row_count": result.get("row_count"),
        "columns": result.get("columns", []),
        "schema_fingerprint": result.get("schema_fingerprint"),
        "file_name": result.get("file_name"),
    }
    merged["semantic_summary_status"] = "formalize_seed"
    merged["semantic_summary_quality"] = seed_summary.get("semantic_summary_quality") or "seeded"
    return merged


def seed_sections(seed: dict[str, Any]) -> list[dict[str, Any]]:
    """Return top-level and nested fact-bundle sections in priority order."""
    sections: list[dict[str, Any]] = []
    if isinstance(seed, dict):
        sections.append(seed)
        for key in ["formalize_seed", "formalize_bundle"]:
            value = seed.get(key)
            if isinstance(value, dict):
                sections.append(value)
    return sections


def seed_sql_fingerprints(seed: dict[str, Any]) -> set[str]:
    keys = [
        "sql_fingerprint",
        "normalized_sql_fingerprint",
        "source_sql_fingerprint",
        "fingerprint",
    ]
    values: set[str] = set()
    for section in seed_sections(seed):
        for key in keys:
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                values.add(value.strip().lower())
    return values


def seed_logic_fingerprints(seed: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for section in seed_sections(seed):
        value = section.get("logic_fingerprint")
        if isinstance(value, str) and value.strip():
            values.add(value.strip().lower())
        sql_facts = section.get("sql_fact_bundle") or section.get("sql_facts")
        if isinstance(sql_facts, dict):
            value = sql_facts.get("logic_fingerprint")
            if isinstance(value, str) and value.strip():
                values.add(value.strip().lower())
    return values


def seed_logic_match_reason(seed: dict[str, Any], *, raw_sql: str, normalized_sql: str) -> tuple[bool, str]:
    if not seed:
        return False, "no_seed"
    fingerprints = seed_logic_fingerprints(seed)
    if not fingerprints:
        matched, reason = seed_sql_match_reason(
            seed,
            raw_sql=raw_sql,
            normalized_sql=normalized_sql,
            normalized_only=False,
        )
        return (True, f"legacy_{reason}") if matched else (False, reason)
    for label, sql in [("normalized", normalized_sql), ("source", raw_sql)]:
        if logic_fingerprint(sql).lower() in fingerprints:
            return True, f"{label}_logic_fingerprint_match"
    return False, "logic_fingerprint_mismatch"


def sql_fingerprint_candidates(raw_sql: str, normalized_sql: str) -> list[tuple[str, str]]:
    candidates = [
        ("normalized_sql_fingerprint_match", normalized_sql),
        ("source_sql_fingerprint_match", raw_sql),
    ]
    normalized_body, normalized_header_kind = strip_any_short_header(normalized_sql)
    if normalized_header_kind and normalized_body != normalized_sql:
        candidates.append(("normalized_sql_without_header_fingerprint_match", normalized_body))
    raw_body, raw_header_kind = strip_any_short_header(raw_sql)
    if raw_header_kind and raw_body != raw_sql:
        candidates.append(("source_sql_without_header_fingerprint_match", raw_body))
    seen: set[str] = set()
    unique_candidates: list[tuple[str, str]] = []
    for reason, text in candidates:
        fingerprint = sha256_text(text).lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique_candidates.append((reason, text))
    return unique_candidates


def seed_matches_sql(seed: dict[str, Any], *, raw_sql: str, normalized_sql: str, normalized_only: bool = False) -> bool:
    matched, _ = seed_sql_match_reason(
        seed,
        raw_sql=raw_sql,
        normalized_sql=normalized_sql,
        normalized_only=normalized_only,
    )
    return matched


def seed_sql_match_reason(
    seed: dict[str, Any],
    *,
    raw_sql: str,
    normalized_sql: str,
    normalized_only: bool = False,
) -> tuple[bool, str]:
    if not seed:
        return False, "no_seed"
    fingerprints = seed_sql_fingerprints(seed)
    if not fingerprints:
        return False, "missing_sql_fingerprint"
    for reason, text in sql_fingerprint_candidates(raw_sql, normalized_sql):
        if normalized_only and reason.startswith("source_sql"):
            continue
        if sha256_text(text).lower() not in fingerprints:
            continue
        if normalized_only and reason == "source_sql_fingerprint_match":
            return False, "source_sql_fingerprint_match_but_normalized_required"
        if normalized_only and reason == "source_sql_without_header_fingerprint_match":
            return False, "source_sql_without_header_fingerprint_match_but_normalized_required"
        if reason.startswith("source_sql"):
            return True, reason
        return True, reason
    raw_hash = sha256_text(raw_sql).lower()
    if raw_hash in fingerprints:
        if normalized_only:
            return False, "source_sql_fingerprint_match_but_normalized_required"
        return True, "source_sql_fingerprint_match"
    return False, "sql_fingerprint_mismatch"


def seed_value(seed: dict[str, Any], key: str) -> Any:
    for section in seed_sections(seed):
        value = section.get(key)
        if value not in (None, "", []):
            return value
    return None


def reusable_seed_dict(
    seed: dict[str, Any],
    key: str,
    *,
    raw_sql: str,
    normalized_sql: str,
    normalized_only: bool = True,
) -> dict[str, Any] | None:
    if not seed_matches_sql(seed, raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=normalized_only):
        return None
    value = seed_value(seed, key)
    if isinstance(value, dict) and value:
        return copy.deepcopy(value)
    return None


def reusable_logic_seed_dict(
    seed: dict[str, Any],
    key: str,
    *,
    raw_sql: str,
    normalized_sql: str,
) -> dict[str, Any] | None:
    matched, _ = seed_logic_match_reason(seed, raw_sql=raw_sql, normalized_sql=normalized_sql)
    if not matched:
        return None
    value = seed_value(seed, key)
    if isinstance(value, dict) and value:
        return copy.deepcopy(value)
    return None


def explain_logic_seed_dict_reuse(
    seed: dict[str, Any],
    key: str,
    *,
    raw_sql: str,
    normalized_sql: str,
) -> dict[str, Any]:
    matched, reason = seed_logic_match_reason(seed, raw_sql=raw_sql, normalized_sql=normalized_sql)
    if not matched:
        return {"status": "not_reused", "reason": reason}
    value = seed_value(seed, key)
    if isinstance(value, dict) and value:
        return {"status": "reused", "reason": reason}
    return {"status": "not_reused", "reason": f"missing_{key}"}


def explain_seed_dict_reuse(
    seed: dict[str, Any],
    key: str,
    *,
    raw_sql: str,
    normalized_sql: str,
    normalized_only: bool = True,
) -> dict[str, Any]:
    matched, reason = seed_sql_match_reason(
        seed,
        raw_sql=raw_sql,
        normalized_sql=normalized_sql,
        normalized_only=normalized_only,
    )
    if not matched:
        return {"status": "not_reused", "reason": reason}
    value = seed_value(seed, key)
    if isinstance(value, dict) and value:
        return {"status": "reused", "reason": "available"}
    return {"status": "not_reused", "reason": f"missing_{key}"}


REPOSITORY_SUMMARY_REQUIRED_KEYS = [
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
    "canonical_rule_status",
    "canonical_rule_checks",
]


def complete_repository_summary(summary: dict[str, Any]) -> bool:
    if not isinstance(summary, dict):
        return False
    for key in REPOSITORY_SUMMARY_REQUIRED_KEYS:
        if key not in summary:
            return False
    for key in ["display_title", "business_topic", "purpose", "business_question", "base_population", "grain", "metrics", "metric_groups", "source_logs", "logic_summary", "canonical_rule_status"]:
        if summary.get(key) in (None, "", []):
            return False
    return True


def repository_summary_from_seed(seed: dict[str, Any], *, raw_sql: str, normalized_sql: str, result: dict[str, Any]) -> dict[str, Any] | None:
    summary = reusable_logic_seed_dict(seed, "repository_summary", raw_sql=raw_sql, normalized_sql=normalized_sql)
    if not summary or not complete_repository_summary(summary):
        return None
    summary["result_evidence"] = {
        "status": "passed",
        "row_count": result.get("row_count"),
        "columns": result.get("columns", []),
        "schema_fingerprint": result.get("schema_fingerprint"),
        "file_name": result.get("file_name"),
    }
    summary["semantic_summary_status"] = "formalize_seed_reused"
    summary["semantic_summary_quality"] = summary.get("semantic_summary_quality") or "seeded"
    return summary


def explain_repository_summary_seed_reuse(seed: dict[str, Any], *, raw_sql: str, normalized_sql: str) -> dict[str, Any]:
    base = explain_logic_seed_dict_reuse(
        seed,
        "repository_summary",
        raw_sql=raw_sql,
        normalized_sql=normalized_sql,
    )
    if base["status"] != "reused":
        return base
    summary = reusable_logic_seed_dict(
        seed,
        "repository_summary",
        raw_sql=raw_sql,
        normalized_sql=normalized_sql,
    )
    if not summary:
        return {"status": "not_reused", "reason": "missing_repository_summary"}
    if not complete_repository_summary(summary):
        return {"status": "not_reused", "reason": "incomplete_repository_summary"}
    return {"status": "reused", "reason": "complete_repository_summary"}


def apply_output_field_contract_to_summary(summary: dict[str, Any], output_contract: dict[str, Any]) -> dict[str, Any]:
    if not output_contract:
        return summary
    retained = [str(item or "").strip() for item in output_contract.get("retained_fields", []) if str(item or "").strip()]
    if not retained:
        return {**summary, "output_field_contract": output_contract}
    retained_norms = {normalize_field_name(item) for item in retained}

    def item_field(item: dict[str, Any]) -> str:
        return str(item.get("field") or item.get("name") or item.get("label") or "").strip()

    def keep_item(item: Any) -> bool:
        if not isinstance(item, dict):
            return True
        field = item_field(item)
        return not field or normalize_field_name(field) in retained_norms

    retained_order = {normalize_field_name(item): index for index, item in enumerate(retained)}

    def order_items(items: list[Any]) -> list[Any]:
        return sorted(
            items,
            key=lambda item: retained_order.get(normalize_field_name(item_field(item)) if isinstance(item, dict) else "", len(retained_order)),
        )

    merged = {**summary}
    metrics = order_items([item for item in summary.get("metrics", []) if keep_item(item)])
    dimensions = order_items([item for item in summary.get("dimensions", []) if keep_item(item)])
    if isinstance(summary.get("metrics"), list):
        merged["metrics"] = metrics
    if isinstance(summary.get("dimensions"), list):
        merged["dimensions"] = dimensions

    groups: list[dict[str, Any]] = []
    for group in summary.get("metric_groups", []) or []:
        if not isinstance(group, dict):
            continue
        group_copy = {**group}
        names = [str(item or "").strip() for item in group.get("metrics", []) if str(item or "").strip()]
        if names:
            group_copy["metrics"] = sorted(
                [name for name in names if normalize_field_name(name) in retained_norms],
                key=lambda name: retained_order.get(normalize_field_name(name), len(retained_order)),
            )
            if not group_copy["metrics"]:
                continue
        groups.append(group_copy)
    if isinstance(summary.get("metric_groups"), list):
        merged["metric_groups"] = groups

    evidence = merged.get("result_evidence") if isinstance(merged.get("result_evidence"), dict) else {}
    merged["result_evidence"] = {**evidence, "columns": retained}
    merged["output_field_contract"] = output_contract
    return merged


def _analysis_item_label(item: Any) -> str:
    if isinstance(item, dict):
        for key in ["field", "name", "label"]:
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""
    return str(item or "").strip()


def apply_output_field_contract_to_analysis(analysis: dict[str, Any], output_contract: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(analysis, dict) or not isinstance(output_contract, dict):
        return analysis, []
    removed_fields = [str(item or "").strip() for item in output_contract.get("removed_output_fields", []) if str(item or "").strip()]
    if not removed_fields:
        return analysis, []
    before_fields = [str(item or "").strip() for item in output_contract.get("sql_final_fields_before_prune", []) if str(item or "").strip()]
    retained_fields = [str(item or "").strip() for item in output_contract.get("retained_fields", []) if str(item or "").strip()]
    output_field_norms = {normalize_field_name(item) for item in before_fields + retained_fields + removed_fields}
    removed_norms = {normalize_field_name(item) for item in removed_fields}
    retained_order = {normalize_field_name(item): index for index, item in enumerate(retained_fields)}
    filtered = copy.deepcopy(analysis)
    removed_items: list[dict[str, str]] = []

    for key in ["metrics", "dimensions"]:
        values = filtered.get(key)
        if not isinstance(values, list):
            continue
        kept: list[Any] = []
        for item in values:
            label = _analysis_item_label(item)
            item_norm = normalize_field_name(label)
            if item_norm and item_norm in removed_norms and item_norm in output_field_norms:
                removed_items.append({"section": key, "field": label})
                continue
            kept.append(item)
        kept.sort(
            key=lambda item: retained_order.get(normalize_field_name(_analysis_item_label(item)), len(retained_order))
        )
        filtered[key] = kept

    if removed_items:
        notes = filtered.get("warnings") if isinstance(filtered.get("warnings"), list) else []
        filtered["warnings"] = [
            *notes,
            "analysis metrics/dimensions filtered by result-file retained output contract.",
        ]
    return filtered, removed_items

def project_rules_fingerprint(root: Path) -> str:
    return rules_fingerprint(root)


def seed_project_rules_fingerprints(seed: dict[str, Any]) -> set[str]:
    keys = ["project_rules_fingerprint", "canonical_rules_fingerprint", "rule_context_fingerprint"]
    values: set[str] = set()
    for section in seed_sections(seed):
        for key in keys:
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                values.add(value.strip().lower())
        reuse_summary = section.get("fact_reuse_summary")
        if isinstance(reuse_summary, dict):
            project_fingerprints = reuse_summary.get("project_fingerprints")
            if isinstance(project_fingerprints, dict):
                current_rules = project_fingerprints.get("current_project_rules")
                if isinstance(current_rules, str) and current_rules.strip():
                    values.add(current_rules.strip().lower())
                seed_rules = project_fingerprints.get("seed_project_rules")
                if isinstance(seed_rules, list):
                    values.update(str(item).strip().lower() for item in seed_rules if str(item).strip())
    return values


def seed_project_config_fingerprints(seed: dict[str, Any]) -> set[str]:
    keys = ["project_config_fingerprint", "config_fingerprint"]
    values: set[str] = set()
    for section in seed_sections(seed):
        for key in keys:
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                values.add(value.strip().lower())
    return values


def seed_rule_application(seed: dict[str, Any]) -> dict[str, Any]:
    for section in seed_sections(seed):
        candidates = [section.get("rule_application")]
        for key in ("rule_context", "canonical_rule_context"):
            context = section.get(key)
            if isinstance(context, dict):
                candidates.append(context.get("rule_application"))
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("schema_version") == RULE_APPLICATION_VERSION
                and application_integrity_ok(candidate)
            ):
                return copy.deepcopy(candidate)
    return {}


def reusable_formalize_rule_context(
    seed: dict[str, Any],
    *,
    root: Path,
    raw_sql: str,
    normalized_sql: str,
    lifecycle_stage: str,
    user_request: str,
) -> dict[str, Any] | None:
    rule_context = reusable_seed_dict(seed, "rule_context", raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not rule_context:
        rule_context = reusable_seed_dict(seed, "canonical_rule_context", raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not rule_context:
        return None
    mode = str(seed_value(seed, "rule_context_mode") or rule_context.get("mode") or "").lower()
    if mode != "formalize":
        return None
    if str(rule_context.get("lifecycle_stage") or "") != lifecycle_stage:
        return None
    rule_application = rule_context.get("rule_application")
    if not application_integrity_ok(rule_application):
        return None
    expected_request_hash = hashlib.sha256(str(user_request or "").strip().encode("utf-8")).hexdigest()
    if str((rule_application.get("request_envelope") or {}).get("text_sha256") or "") != expected_request_hash:
        return None
    rule_fingerprints = seed_project_rules_fingerprints(seed)
    current_fingerprint = project_rules_fingerprint(root).lower()
    if current_fingerprint and current_fingerprint not in rule_fingerprints:
        return None
    candidate_check = rule_context.get("candidate_sql_check")
    if not isinstance(candidate_check, dict):
        return None
    return rule_context


def explain_formalize_rule_context_reuse(
    seed: dict[str, Any],
    *,
    root: Path,
    raw_sql: str,
    normalized_sql: str,
    lifecycle_stage: str,
    user_request: str,
) -> dict[str, Any]:
    matched, reason = seed_sql_match_reason(seed, raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not matched:
        return {"status": "not_reused", "reason": reason}
    rule_context = reusable_seed_dict(seed, "rule_context", raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not rule_context:
        rule_context = reusable_seed_dict(seed, "canonical_rule_context", raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not rule_context:
        return {"status": "not_reused", "reason": "missing_rule_context"}
    mode = str(seed_value(seed, "rule_context_mode") or rule_context.get("mode") or "").lower()
    if mode != "formalize":
        return {"status": "not_reused", "reason": "seed_mode_not_formalize", "seed_mode": mode or "missing"}
    seed_stage = str(rule_context.get("lifecycle_stage") or "")
    if seed_stage != lifecycle_stage:
        return {
            "status": "not_reused",
            "reason": "lifecycle_stage_mismatch",
            "seed_stage": seed_stage or "missing",
            "required_stage": lifecycle_stage,
        }
    rule_application = rule_context.get("rule_application")
    if not application_integrity_ok(rule_application):
        return {"status": "not_reused", "reason": "missing_or_invalid_rule_application_v1"}
    expected_request_hash = hashlib.sha256(str(user_request or "").strip().encode("utf-8")).hexdigest()
    if str((rule_application.get("request_envelope") or {}).get("text_sha256") or "") != expected_request_hash:
        return {"status": "not_reused", "reason": "current_user_request_mismatch"}
    rule_fingerprints = seed_project_rules_fingerprints(seed)
    current_fingerprint = project_rules_fingerprint(root).lower()
    if current_fingerprint and current_fingerprint not in rule_fingerprints:
        return {"status": "not_reused", "reason": "project_rules_fingerprint_mismatch"}
    candidate_check = rule_context.get("candidate_sql_check")
    if not isinstance(candidate_check, dict):
        return {"status": "not_reused", "reason": "missing_candidate_sql_check"}
    return {"status": "reused", "reason": "formalize_rule_context_fingerprint_match"}


def reusable_performance_level(seed: dict[str, Any], *, raw_sql: str, normalized_sql: str, config: dict[str, Any]) -> dict[str, Any] | None:
    performance = reusable_seed_dict(seed, "performance_level", raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not performance:
        performance = reusable_seed_dict(seed, "performance", raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not performance:
        return None
    config_fingerprints = seed_project_config_fingerprints(seed)
    current_config_fingerprint = config_fingerprint(config).lower()
    if not config_fingerprints or current_config_fingerprint not in config_fingerprints:
        return None
    if not performance.get("performance_fingerprint"):
        return None
    return performance


def explain_performance_reuse(seed: dict[str, Any], *, raw_sql: str, normalized_sql: str, config: dict[str, Any]) -> dict[str, Any]:
    matched, reason = seed_sql_match_reason(seed, raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not matched:
        return {"status": "not_reused", "reason": reason}
    performance = reusable_seed_dict(seed, "performance_level", raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not performance:
        performance = reusable_seed_dict(seed, "performance", raw_sql=raw_sql, normalized_sql=normalized_sql, normalized_only=True)
    if not performance:
        return {"status": "not_reused", "reason": "missing_performance_level"}
    config_fingerprints = seed_project_config_fingerprints(seed)
    current_config_fingerprint = config_fingerprint(config).lower()
    if not config_fingerprints:
        return {"status": "not_reused", "reason": "missing_project_config_fingerprint"}
    if current_config_fingerprint not in config_fingerprints:
        return {"status": "not_reused", "reason": "project_config_fingerprint_mismatch"}
    if not performance.get("performance_fingerprint"):
        return {"status": "not_reused", "reason": "missing_performance_fingerprint"}
    return {"status": "reused", "reason": "performance_and_project_config_fingerprint_match"}


def build_fact_reuse_summary(
    *,
    seed: dict[str, Any],
    source: str,
    root: Path,
    raw_sql: str,
    normalized_for_seed: str,
    normalized_sql: str,
    config: dict[str, Any],
    user_request: str,
) -> dict[str, Any]:
    seed_fingerprints = sorted(seed_sql_fingerprints(seed))
    seed_logic = sorted(seed_logic_fingerprints(seed))
    raw_fingerprint = sha256_text(raw_sql)
    normalized_for_seed_fingerprint = sha256_text(normalized_for_seed)
    normalized_sql_fingerprint = sha256_text(normalized_sql)
    summary = {
        "source": source,
        "seed_present": bool(seed),
        "sql_fingerprint": {
            "raw": raw_fingerprint,
            "normalized_for_seed": normalized_for_seed_fingerprint,
            "normalized_after_output_contract": normalized_sql_fingerprint,
            "seed_fingerprints": seed_fingerprints,
        },
        "logic_fingerprint": {
            "raw": logic_fingerprint(raw_sql),
            "normalized_for_seed": logic_fingerprint(normalized_for_seed),
            "normalized_after_output_contract": logic_fingerprint(normalized_sql),
            "seed_fingerprints": seed_logic,
        },
        "project_fingerprints": {
            "current_project_config": config_fingerprint(config),
            "seed_project_config": sorted(seed_project_config_fingerprints(seed)),
            "current_project_rules": project_rules_fingerprint(root),
            "seed_project_rules": sorted(seed_project_rules_fingerprints(seed)),
        },
        "facts": {
            "analysis": explain_logic_seed_dict_reuse(
                seed,
                "analysis",
                raw_sql=raw_sql,
                normalized_sql=normalized_for_seed,
            ),
            "rule_context": explain_formalize_rule_context_reuse(
                seed,
                root=root,
                raw_sql=raw_sql,
                normalized_sql=normalized_sql,
                lifecycle_stage="retained_query",
                user_request=user_request,
            ),
            "repository_summary": explain_repository_summary_seed_reuse(
                seed,
                raw_sql=raw_sql,
                normalized_sql=normalized_for_seed,
            ),
            "performance_level": explain_performance_reuse(
                seed,
                raw_sql=raw_sql,
                normalized_sql=normalized_sql,
                config=config,
            ),
        },
    }
    return summary


def read_text(path: Path) -> str:
    for encoding in ["utf-8-sig", "utf-8", "gb18030"]:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def project_staging_directory(root: Path, prefix: str):
    staging_root = root.resolve() / ".tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=staging_root)


def formal_sql_delivery_receipt(root: Path, paths: dict[str, str]) -> dict[str, Any]:
    """Return verified output-only paths for SQL files written by formalization."""

    root = root.resolve()
    files: list[dict[str, str]] = []
    blockers: list[str] = []
    for kind, relative in paths.items():
        relative = str(relative or "").replace("\\", "/").lstrip("./")
        if not relative:
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            blockers.append(f"{kind} SQL path escapes the project root: {relative}")
            continue
        if not candidate.exists():
            blockers.append(f"{kind} SQL file is missing after formalization: {relative}")
            continue
        transforms = sql_side_privacy_transforms(candidate.read_text(encoding="utf-8-sig"))
        if transforms:
            functions = ", ".join(sorted({item["function"] for item in transforms}))
            blockers.append(
                f"{kind} SQL performs forbidden SQL-side de-identification: {functions}."
            )
            continue
        files.append(
            {
                "kind": kind,
                "project_relative_path": relative,
                "absolute_path": str(candidate),
            }
        )
    return {
        "schema_version": "formal_sql_delivery_receipt_v1",
        "status": "blocked" if blockers else "ready",
        "files": files,
        "blockers": blockers,
        "final_response_requirement": (
            "Return clickable links for every files[].absolute_path created by this request. "
            "Do not paste SQL as the only deliverable."
        ),
    }


def run_rule_context(
    root: Path,
    sql_file: Path,
    user_request: str,
    *,
    mode: str = "formalize",
    lifecycle_stage: str | None = None,
    concept_keys: list[str] | None = None,
    parent_rule_application: dict[str, Any] | None = None,
    inheritance_contract: dict[str, Any] | None = None,
    execution_route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if not isinstance(execution_route, dict):
            receipt_path = route_receipt_path(sql_file)
            if receipt_path.exists():
                candidate_route = read_json(receipt_path, {})
                if isinstance(candidate_route, dict):
                    execution_route = candidate_route
        return evaluate_rule_context(
            root=root,
            user_request=user_request,
            candidate_sql=sql_file.read_text(encoding="utf-8-sig"),
            mode=mode,
            lifecycle_stage=lifecycle_stage,
            concept_keys=concept_keys,
            parent_rule_application=parent_rule_application,
            inheritance_contract=inheritance_contract,
            execution_route=execution_route,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "candidate_sql_check": {"status": "error", "blockers": [str(exc) or "rule-context failed"]}}


def ensure_rule_context_generation_gate(
    rule_context: dict[str, Any],
    *,
    sql: str,
    config: dict[str, Any],
    execution_route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh the project contract even when canonical-rule facts came from an older seed."""

    candidate_check = rule_context.get("candidate_sql_check") if isinstance(rule_context.get("candidate_sql_check"), dict) else None
    saved_project_check = (
        rule_context.get("project_contract_check")
        if isinstance(rule_context.get("project_contract_check"), dict)
        else None
    )
    current_sql_fingerprint = execution_fingerprint(sql)
    current_config_fingerprint = route_config_fingerprint(config)
    if (
        saved_project_check
        and saved_project_check.get("sql_fingerprint") == current_sql_fingerprint
        and saved_project_check.get("config_fingerprint") == current_config_fingerprint
    ):
        project_check = saved_project_check
    else:
        project_check = project_execution_contract_check(
            sql,
            config,
            execution_route=execution_route,
        )
    generation_gate = compose_generation_gate(candidate_check, project_check)
    rule_context["project_contract_check"] = project_check
    rule_context["project_time_contract"] = project_check.get("time_contract", {})
    rule_context["generation_gate"] = generation_gate
    rule_context["status"] = generation_gate.get("status", rule_context.get("status", "not_run"))
    return generation_gate


def planned_rel(root: Path, kind: str, slug: str) -> str:
    del root
    section = {"QUERY": "query", "VALIDATION": "validation", "DASHBOARD": "dashboard"}[kind]
    member_prefix = f"analyses/{slugify(slug, 'formalized-sql')}"
    return f"{member_prefix}/{section}/v001.sql"

def run_internal_tool(fn, arg) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        fn(arg)
    return buffer.getvalue().strip()


def powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def dynamic_viewer_info(root: Path, args) -> dict[str, Any]:
    user_request = str(getattr(args, "user_request", "") or "SQL formalize dynamic viewer")
    command_args = [
        sys.executable,
        str(SCRIPT_DIR / "sql_repository.py"),
        "serve",
        "--root",
        str(root),
        "--user-request",
        user_request,
        "--function-selection",
        "SQL_REPOSITORY",
    ]
    sample_rows = int(getattr(args, "sample_rows", 8) or 8)
    if sample_rows != 8:
        command_args.extend(["--sample-rows", str(sample_rows)])
    return {
        "mode": "live_project_state",
        "description": "Run this command when you want a live repository/dashboard-review viewer without rebuilding static HTML.",
        "command_args": command_args,
        "powershell_command": " ".join(powershell_quote(item) for item in command_args),
        "routes": {
            "repository": "/",
            "repository_api": "/api/repository",
            "dashboard_review": "/dashboard_review.html",
            "dashboard_review_state_api": "/api/state",
        },
    }

def concept_keys_from_summary(summary: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in summary.get("applied_criteria", []) + summary.get("canonical_rule_checks", []):
        if isinstance(item, dict) and item.get("concept_key"):
            values.append(str(item["concept_key"]))
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def current_query_artifact_for_source(root: Path, manifest: dict[str, Any], source_sql: Path) -> dict[str, Any] | None:
    """Return the current QUERY artifact whose SQL path is the supplied source."""

    try:
        source_resolved = source_sql.resolve()
    except OSError:
        source_resolved = source_sql.absolute()
    for item in manifest.get("artifacts", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") != "QUERY" or not is_current_artifact(item):
            continue
        rel_path = str(item.get("path") or "").strip()
        if not rel_path:
            continue
        try:
            artifact_resolved = (root / rel_path).resolve()
        except OSError:
            artifact_resolved = (root / rel_path).absolute()
        if artifact_resolved == source_resolved:
            return item
    return None


def query_reuse_decision(
    existing_query: dict[str, Any] | None,
    *,
    explicit_slug: str,
    effective_slug: str,
    normalized_changed: bool,
    output_contract: dict[str, Any],
) -> dict[str, Any]:
    if not existing_query:
        return {
            "status": "new_query",
            "reason": "source_sql_not_current_query_artifact",
            "path": "",
        }
    path = str(existing_query.get("path") or "")
    existing_slug = str(existing_query.get("slug") or "")
    if explicit_slug and existing_slug and effective_slug != existing_slug:
        return {
            "status": "new_query",
            "reason": "explicit_slug_differs_from_source_query",
            "path": path,
            "source_slug": existing_slug,
            "requested_slug": effective_slug,
        }
    if output_contract.get("status") == "mismatch":
        return {
            "status": "blocked",
            "reason": "output_field_contract_mismatch",
            "path": path,
        }
    if normalized_changed:
        return {
            "status": "new_query",
            "reason": "normalized_sql_or_retained_output_contract_changed",
            "path": path,
            "source_slug": existing_slug,
        }
    return {
        "status": "reused",
        "reason": "source_sql_is_current_query_and_sql_body_unchanged",
        "path": path,
        "source_slug": existing_slug,
    }


def artifact_write_plan(query_reuse: dict[str, Any], target: str) -> dict[str, Any]:
    query_action = "reuse" if query_reuse.get("status") == "reused" else "save"
    items = [
        {
            "kind": "QUERY",
            "action": query_action,
            "reason": query_reuse.get("reason") or ("existing_query_reused" if query_action == "reuse" else "formal_query_save_required"),
            "path": query_reuse.get("path") or "",
        },
        {
            "kind": "RUN_EVIDENCE",
            "action": "save",
            "reason": "bind_user_result_file_to_query",
            "path": "",
        },
        {
            "kind": "VALIDATION",
            "action": "save" if target == "query-dashboard" else "skip",
            "reason": "dashboard_promotion_gate" if target == "query-dashboard" else "target_query_does_not_promote_dashboard",
            "path": "",
        },
        {
            "kind": "DASHBOARD",
            "action": "save" if target == "query-dashboard" else "skip",
            "reason": "target_query_dashboard" if target == "query-dashboard" else "target_query_only",
            "path": "",
        },
    ]
    counts = {"save": 0, "reuse": 0, "skip": 0}
    for item in items:
        action = str(item.get("action") or "")
        if action in counts:
            counts[action] += 1
    return {
        "contract_version": "formalize_artifact_write_plan_v1",
        "target": target,
        "items": items,
        "counts": counts,
    }


def skipped_artifact_write_plan(target: str, reason: str) -> dict[str, Any]:
    items = [
        {"kind": "QUERY", "action": "skip", "reason": reason, "path": ""},
        {"kind": "RUN_EVIDENCE", "action": "skip", "reason": reason, "path": ""},
        {"kind": "VALIDATION", "action": "skip", "reason": reason, "path": ""},
        {"kind": "DASHBOARD", "action": "skip", "reason": reason, "path": ""},
    ]
    return {
        "contract_version": "formalize_artifact_write_plan_v1",
        "target": target,
        "items": items,
        "counts": {"save": 0, "reuse": 0, "skip": len(items)},
    }


def dashboard_contract_preview_matches(preview: dict[str, Any], dashboard_sql: str, result: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(preview, dict) or not preview:
        return False, "dashboard contract preview is missing"
    if preview.get("status") != "passed":
        return False, f"dashboard contract preview status is {preview.get('status') or 'unknown'}"
    expected_sql_fingerprint = sha256_text(dashboard_sql)
    if preview.get("sql_fingerprint") != expected_sql_fingerprint:
        return False, "dashboard SQL fingerprint differs from the plan-stage contract preview"
    expected_schema_fingerprint = str((result or {}).get("schema_fingerprint") or "")
    if preview.get("result_schema_fingerprint") != expected_schema_fingerprint:
        return False, "result schema fingerprint differs from the plan-stage contract preview"
    return True, "plan-stage dashboard contract preview is still valid"


def list_from_summary(items: list[Any], *keys: str) -> list[str]:
    values: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            for key in keys:
                value = str(item.get(key) or "").strip()
                if value:
                    values.append(value)
                    break
        elif item:
            values.append(str(item))
    return values


def save_artifact_record(
    *,
    root: Path,
    manifest: dict[str, Any],
    kind: str,
    slug: str,
    title: str,
    sql_text: str,
    spec_doc: dict[str, Any],
    analysis: dict[str, Any],
    summary: dict[str, Any],
    created_at: str,
    status: str,
    analysis_type: str | None = None,
    tags: list[str] | None = None,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    linked_query: str = "",
    linked_validation: str = "",
    linked_run: str = "",
    verification_status: str = "not_applicable",
    verification_note: str = "",
    future_verification_plan: str = "",
    project_context: dict[str, Any] | None = None,
    change_type_override: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raise SystemExit(
        "save_artifact_record is retired: formal SQL must be written through "
        "Formal Asset Repository / sql_formalize repository transaction."
    )
    if has_full_spec_block(sql_text):
        raise SystemExit("Formal SQL must use a short header plus sidecar spec, not a legacy full spec block.")
    stable_title = strip_source_prefix(title)
    directory = artifact_dir(root, kind, slug)
    directory.mkdir(parents=True, exist_ok=True)
    version = next_artifact_version(directory)
    sql_name = f"v{version:03d}.sql"
    spec_name = f"v{version:03d}.spec.json"
    meta_name = f"v{version:03d}.meta.json"
    destination = directory / sql_name
    spec_destination = directory / spec_name
    rel_sql = destination.relative_to(root).as_posix()
    rel_spec = spec_destination.relative_to(root).as_posix()

    artifacts = manifest.setdefault("artifacts", [])
    change_type = resolve_change_type(change_type_override if version > 1 and change_type_override else "auto", version)
    supersedes: list[str] = []
    superseded_items: list[dict[str, Any]] = []
    if change_type in REPLACEMENT_CHANGE_TYPES:
        for item in artifacts:
            if item.get("kind") == kind and item.get("slug") == slug and is_current_artifact(item):
                item["artifact_state"] = "history"
                item["status"] = "superseded"
                item["replaced_by"] = rel_sql
                item["replaced_at"] = created_at
                item["reusable"] = False
                supersedes.append(item.get("path", ""))
                superseded_items.append(item)

    workflow = {
        "QUERY": "fast_formalize_query",
        "VALIDATION": "fast_formalize_validation",
        "DASHBOARD": "fast_formalize_dashboard",
    }.get(kind, "fast_formalize")
    generation_provenance = merge_generation_provenance(
        spec_doc.get("generation_provenance") if isinstance(spec_doc.get("generation_provenance"), dict) else None,
        fallback_generator_script="sql_formalize.py",
        fallback_workflow=workflow,
        artifact_kind=kind,
        saved_at=created_at,
        saved_by_script="sql_formalize.py",
    )

    business_category = analysis.get("business_category", DEFAULT_BUSINESS_CATEGORY)
    resolved_analysis_type = analysis_type or analysis.get("analysis_type", DEFAULT_ANALYSIS_TYPE)
    metadata = {
        "kind": kind,
        "slug": slug,
        "version": version,
        "title": stable_title,
        "source_title": title if title != stable_title else "",
        "status": status,
        "artifact_state": "current",
        "change_type": change_type,
        "supersedes": [path for path in supersedes if path],
        "replaced_by": "",
        "branch_of": "",
        "change_reason": "",
        "path": rel_sql,
        "spec_path": rel_spec,
        "spec_storage": "sidecar_json",
        "header_contract_version": "1",
        "generation_provenance": generation_provenance,
        "project_context": (
            copy.deepcopy(spec_doc.get("project_context"))
            if isinstance(spec_doc.get("project_context"), dict)
            else project_context or project_context_snapshot(read_project_config(root) or {})
        ),
        "execution_route": copy.deepcopy(spec_doc.get("execution_route") or {}),
        "business_category": business_category,
        "analysis_type": resolved_analysis_type,
        "tags": tags if tags is not None else analysis.get("tags", []),
        "metrics": metrics if metrics is not None else list_from_summary(summary.get("metrics", []), "field", "name", "label"),
        "dimensions": dimensions if dimensions is not None else list_from_summary(summary.get("dimensions", []), "field", "label"),
        "tables": analysis.get("tables", []),
        "intermediate_tables": [],
        "grain": summary.get("grain") or analysis.get("grain", ""),
        "time_grain": analysis.get("time_grain", ""),
        "reusable": True,
        "reuse_candidate": bool(analysis.get("reuse_candidate", False)),
        "reuse_notes": "Fast-formalized artifact; adjust params CTE/date range before rerun and keep output contract stable.",
        "content_summary": analysis.get("content_summary", ""),
        "auto_metadata": False,
        "auto_metadata_warnings": analysis.get("warnings", []),
        "natural_language_intent": title,
        "linked_query": linked_query,
        "linked_validation": linked_validation,
        "linked_run": linked_run,
        "verification_status": verification_status,
        "verification_note": verification_note,
        "future_verification_plan": future_verification_plan,
        "created_at": created_at,
        "notes": "Generated by sql_formalize.py fast path bundle transaction.",
    }
    if kind == "DASHBOARD" and verification_status == "unverified_skipped_run":
        metadata["tags"] = [*metadata["tags"], "unvalidated", "no_result_file"]
    if kind == "DASHBOARD" and verification_status == "proxy_verified":
        metadata["tags"] = [*metadata["tags"], "proxy_verified", "needs_target_verification"]
    origin_query_workspace = spec_doc.get("origin_query_workspace")
    if kind == "QUERY" and isinstance(origin_query_workspace, dict) and origin_query_workspace:
        metadata["origin_query_workspace"] = copy.deepcopy(origin_query_workspace)

    set_spec_version(spec_doc)
    apply_generation_provenance(spec_doc, generation_provenance)
    destination.write_text(
        stamp_sql_generation(
            root,
            replace_or_prepend_short_header(
                kind,
                sql_text,
                build_short_header(root, metadata, spec_doc, rel_spec),
            ),
        ),
        encoding="utf-8",
    )
    write_json_object(spec_destination, spec_doc)
    write_json(directory / meta_name, metadata)
    artifacts.append(metadata)
    counters = manifest.setdefault("artifact_counters", {"QUERY": {}, "DASHBOARD": {}, "VALIDATION": {}})
    counters.setdefault(kind, {})[slug] = version
    return metadata, superseded_items


def unique_run_paths(
    root: Path,
    timestamp: str,
    slug: str,
    evidence_suffix: str,
    *,
    run_relative_dir: Path = Path("runs"),
    reserved_paths: set[str] | None = None,
) -> tuple[Path, Path]:
    run_dir = root / run_relative_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    reserved = {str(item).replace("\\", "/") for item in (reserved_paths or set())}
    for index in range(1, 1000):
        suffix = "" if index == 1 else f"-{index}"
        markdown_path = run_dir / f"{timestamp}_{slug}{suffix}.md"
        evidence_path = run_dir / f"{timestamp}_{slug}{suffix}{evidence_suffix}"
        markdown_relative = markdown_path.relative_to(root).as_posix()
        evidence_relative = evidence_path.relative_to(root).as_posix()
        if (
            not markdown_path.exists()
            and not evidence_path.exists()
            and markdown_relative not in reserved
            and evidence_relative not in reserved
        ):
            return markdown_path, evidence_path
    raise SystemExit("Could not allocate a run evidence filename.")


def save_run_record(
    *,
    root: Path,
    manifest: dict[str, Any],
    args,
    source_artifact: str,
    sql_path: str,
    slug: str,
    title: str,
    result: dict[str, Any],
    result_file: Path | None,
    concept_keys: list[str],
    sql_facts: dict[str, Any],
    run_relative_dir: Path = Path("runs"),
    reserved_paths: set[str] | None = None,
) -> dict[str, Any]:
    if args.verification_status in {"passed", "proxy_verified"}:
        if not args.user_confirmed:
            raise SystemExit(f"{args.verification_status} run evidence requires --user-confirmed.")
        if not result_file.exists():
            raise SystemExit(f"Evidence file not found: {result_file}")
        if result_file.suffix.lower() not in RESULT_FILE_EXTENSIONS:
            allowed = ", ".join(sorted(RESULT_FILE_EXTENSIONS))
            raise SystemExit(f"{args.verification_status} run evidence requires result file type: {allowed}.")
    timestamp = now_stamp()
    run_slug = slugify(slug or title or source_artifact, "run")
    evidence_rel = ""
    result_file_type = ""
    result_evidence_retention: dict[str, Any] = {}
    if result_file:
        retained_result = prepare_result_evidence(result_file)
        destination, evidence_destination = unique_run_paths(
            root,
            timestamp,
            run_slug,
            retained_result.suffix,
            run_relative_dir=run_relative_dir,
            reserved_paths=reserved_paths,
        )
        write_retained_result(retained_result, evidence_destination)
        evidence_rel = evidence_destination.relative_to(root).as_posix()
        result_file_type = retained_result.suffix
        result_evidence_retention = retained_result.retention
    else:
        destination, _ = unique_run_paths(
            root,
            timestamp,
            run_slug,
            "",
            run_relative_dir=run_relative_dir,
            reserved_paths=reserved_paths,
        )
    created_at = now_iso()
    retained_evidence = retained_result_evidence(result)
    retained_columns = retained_evidence.get("columns", [])
    override = retained_evidence.get("retained_fields_override") if isinstance(retained_evidence.get("retained_fields_override"), dict) else {}
    result_summary = (
        f"Fast formalization: user supplied {result.get('file_name')} with {result.get('row_count')} rows and retained columns {', '.join(retained_columns[:20])}."
        if result_file
        else f"Fast formalization skipped real run evidence: {args.skip_reason or 'no result file was provided'}. This record is unverified and must be verified later."
    )
    if result_file and override and override.get("removed_columns"):
        result_summary += " Removed result columns from the formal output contract: " + ", ".join(str(item) for item in override.get("removed_columns", [])[:20]) + "."
    record = {
        "run_id": destination.stem,
        "title": f"{title} run evidence",
        "source_artifact": source_artifact,
        "sql_path": sql_path,
        "status": args.verification_status,
        "row_count": result.get("row_count"),
        "checked_metrics": [],
        "checked_dimensions": [],
        "sample_fields": retained_columns[:20],
        "result_summary": result_summary,
        "issues": "",
        "user_confirmed": bool(args.user_confirmed),
        "skip_reason": args.skip_reason or "",
        "risk_note": args.risk_note or "",
        "future_verification_plan": args.future_verification_plan or "",
        "definition_project": args.definition_project or Path(root).name,
        "execution_project": args.execution_project or Path(root).name,
        "delivery_project": args.delivery_project or Path(root).name,
        "concept_keys": concept_keys,
        "proxy_limitations": args.proxy_limitations or "",
        "confirmed_by": args.confirmed_by or "user",
        "evidence_file": evidence_rel,
        "result_file_type": result_file_type,
        "result_evidence_retention": result_evidence_retention,
        "result_columns": retained_columns,
        "result_schema_fingerprint": retained_evidence.get("schema_fingerprint") or "",
        "retained_result_evidence": retained_evidence,
        "retained_fields_override": retained_evidence.get("retained_fields_override") or {},
        "output_field_contract": retained_evidence.get("output_field_contract") or {},
        "created_at": created_at,
        "notes": "Generated by sql_formalize.py fast path bundle transaction.",
    }
    if retained_evidence.get("result_time_coverage"):
        record["result_time_coverage"] = copy.deepcopy(
            retained_evidence["result_time_coverage"]
        )
    source_sql_file = root / sql_path
    if result_file and source_sql_file.is_file():
        record.update(
            {
                "contract_version": "sql_result_binding_v1",
                "result_binding_id": destination.stem,
                "sql_asset_kind": "query",
                "source_sql_fingerprint": execution_fingerprint(source_sql_file.read_text(encoding="utf-8-sig")),
                "parameter_snapshot": copy.deepcopy(sql_facts.get("params") or {}),
                "derived_outputs": [],
            }
        )
    body = [
        f"# {record['title']}",
        "",
        f"- run_id: {record['run_id']}",
        f"- source_artifact: {record['source_artifact']}",
        f"- sql_path: {markdown_value(record['sql_path'])}",
        f"- status: {record['status']}",
        f"- row_count: {markdown_value(record['row_count'])}",
        f"- checked_metrics: {markdown_value(record['checked_metrics'])}",
        f"- checked_dimensions: {markdown_value(record['checked_dimensions'])}",
        f"- sample_fields: {markdown_value(record['sample_fields'])}",
        f"- user_confirmed: {str(record['user_confirmed']).lower()}",
        f"- skip_reason: {markdown_value(record['skip_reason'])}",
        f"- risk_note: {markdown_value(record['risk_note'])}",
        f"- future_verification_plan: {markdown_value(record['future_verification_plan'])}",
        f"- definition_project: {markdown_value(record['definition_project'])}",
        f"- execution_project: {markdown_value(record['execution_project'])}",
        f"- delivery_project: {markdown_value(record['delivery_project'])}",
        f"- concept_keys: {markdown_value(record['concept_keys'])}",
        f"- proxy_limitations: {markdown_value(record['proxy_limitations'])}",
        f"- confirmed_by: {markdown_value(record['confirmed_by'])}",
        f"- evidence_file: {markdown_value(record['evidence_file'])}",
        f"- result_file_type: {markdown_value(record['result_file_type'])}",
        f"- result_evidence_retention: {markdown_value(record['result_evidence_retention'])}",
        f"- result_time_coverage: {markdown_value(record.get('result_time_coverage') or {})}",
        f"- result_columns: {markdown_value(record['result_columns'])}",
        f"- result_schema_fingerprint: {markdown_value(record['result_schema_fingerprint'])}",
        f"- retained_fields_override: {markdown_value(record['retained_fields_override'])}",
        f"- created_at: {record['created_at']}",
        "",
        "## Result Summary",
        "",
        record["result_summary"],
        "",
        "## Issues",
        "",
        record["issues"],
        "",
        "## Notes",
        "",
        record["notes"],
        "",
    ]
    destination.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
    record["path"] = destination.relative_to(root).as_posix()
    manifest.setdefault("run_evidence", []).append(record)
    return record


def finalize_manifest(root: Path, manifest: dict[str, Any], superseded_items: list[dict[str, Any]]) -> None:
    manifest.setdefault("project_config_file", "project_config.json")
    manifest["updated_at"] = now_iso()
    write_json(manifest_path(root), manifest)
    for item in superseded_items:
        write_artifact_meta(root, item)
    rebuild_index(root)


def _build_repository_full(root: Path, sample_rows: int) -> str:
    from sql_repository import cmd_build as repo_build

    return run_internal_tool(
        repo_build,
        SimpleNamespace(root=str(root), output=None, json_output=None, include_history=False, sample_rows=sample_rows),
    )


def _build_dashboard_review_full(root: Path, sample_rows: int) -> str:
    from dashboard_review import cmd_build as dash_build

    return run_internal_tool(
        dash_build,
        SimpleNamespace(root=str(root), output=None, json_output=None, state_file=None, include_history=False, include_approved=False, sample_rows=sample_rows),
    )


def refresh_repository_incremental(root: Path, query_artifact: dict[str, Any], *, sample_rows: int) -> str:
    """Refresh only the changed QUERY card in the static repository payload."""
    try:
        import sql_repository as repo  # imported lazily to avoid viewer cost during dry-run

        output = root / repo.DEFAULT_HTML_REL
        json_output = root / repo.DEFAULT_JSON_REL
        if not json_output.exists():
            full = _build_repository_full(root, sample_rows)
            return f"sql_repository_incremental_fallback: missing existing payload; {full}"
        payload = repo.read_json(json_output, {})
        if payload.get("schema") != repo.PAYLOAD_VERSION:
            full = _build_repository_full(root, sample_rows)
            return f"sql_repository_incremental_fallback: incompatible payload; {full}"

        manifest = repo.read_manifest(root)
        current_queries = repo.project_artifacts(manifest, "QUERY", include_history=False)
        current_query_paths = [str(item.get("path") or "") for item in current_queries]
        current_query_set = set(current_query_paths)
        query_path = str(query_artifact.get("path") or "")
        if query_path not in current_query_set:
            full = _build_repository_full(root, sample_rows)
            return f"sql_repository_incremental_fallback: changed query is not current; {full}"

        catalog = repo.load_xml_log_catalog(root)
        rule_index = repo.load_canonical_rule_index(root)
        dashboard_review_html = repo.link_relative_to_html(root, output, repo.DEFAULT_DASHBOARD_REVIEW_REL)
        dashboard_by_query: dict[str, list[dict[str, Any]]] = {}
        orphan_dashboards: list[dict[str, Any]] = []
        for dash in repo.project_artifacts(manifest, "DASHBOARD", include_history=False):
            attachment = repo.build_dashboard_attachment(root, dash, sample_rows, dashboard_review_html)
            linked_query = str(dash.get("linked_query") or "").strip()
            if linked_query:
                dashboard_by_query.setdefault(linked_query, []).append(attachment)
            else:
                orphan_dashboards.append(attachment)

        changed_item = repo.build_query_item(
            root,
            manifest,
            query_artifact,
            dashboard_by_query.get(query_path, []),
            sample_rows,
            catalog,
            rule_index,
        )
        changed_slug = str(query_artifact.get("slug") or "")
        items: list[dict[str, Any]] = []
        for item in payload.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            item_path = str(item.get("path") or "")
            item_slug = str(item.get("slug") or "")
            if item_path not in current_query_set:
                continue
            if item_path == query_path or (changed_slug and item_slug == changed_slug):
                continue
            items.append(item)
        items.append(changed_item)
        order = {path: idx for idx, path in enumerate(current_query_paths)}
        items.sort(key=lambda item: order.get(str(item.get("path") or ""), 10**9))

        payload.update(
            {
                "project": str(manifest.get("project_name") or root.name),
                "project_root": ".",
                "generated_at": repo.now_iso(),
                "query_count": len(items),
                "dashboard_attachment_count": sum(len(item.get("dashboard_attachments") or []) for item in items),
                "orphan_dashboard_count": len(orphan_dashboards),
                "items": items,
                "orphan_dashboard_attachments": orphan_dashboards,
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(repo.html_shell(payload), encoding="utf-8")
        repo.write_json(json_output, payload)
        return f"sql_repository_html_incremental: {output}\nsql_repository_json_incremental: {json_output}\nquery_items: {payload['query_count']}\ndashboard_attachments: {payload['dashboard_attachment_count']}"
    except Exception as exc:  # noqa: BLE001
        full = _build_repository_full(root, sample_rows)
        return f"sql_repository_incremental_fallback: {exc}; {full}"


def refresh_dashboard_review_incremental(root: Path, dashboard_artifact: dict[str, Any], *, sample_rows: int) -> str:
    """Refresh only the changed DASHBOARD item in the static dashboard review payload."""
    try:
        import dashboard_review as dash  # imported lazily to avoid viewer cost during dry-run/query-only saves

        output = root / dash.DEFAULT_HTML_REL
        json_output = root / dash.DEFAULT_JSON_REL
        state_path = root / dash.DEFAULT_STATE_REL
        if not json_output.exists():
            full = _build_dashboard_review_full(root, sample_rows)
            return f"dashboard_review_incremental_fallback: missing existing payload; {full}"
        payload = dash.read_json(json_output, {})
        if payload.get("review_contract_version") != dash.REVIEW_CONTRACT_VERSION:
            full = _build_dashboard_review_full(root, sample_rows)
            return f"dashboard_review_incremental_fallback: incompatible payload; {full}"

        current_dashboards = dash.load_dashboard_artifacts(root, include_history=False)
        current_paths = [str(item.get("path") or "") for item in current_dashboards]
        current_set = set(current_paths)
        dashboard_path = str(dashboard_artifact.get("path") or "")
        if dashboard_path not in current_set:
            full = _build_dashboard_review_full(root, sample_rows)
            return f"dashboard_review_incremental_fallback: changed dashboard is not current; {full}"

        state = dash.load_state(state_path)
        changed_item, skipped = dash.build_dashboard_item(root, dashboard_artifact, state, include_approved=False, sample_limit=sample_rows)
        changed_slug = str(dashboard_artifact.get("slug") or "")

        def keep_existing(item: Any) -> bool:
            if not isinstance(item, dict):
                return False
            item_path = str(item.get("path") or "")
            item_slug = str(item.get("slug") or "")
            if item_path not in current_set:
                return False
            if item_path == dashboard_path or (changed_slug and item_slug == changed_slug):
                return False
            return True

        items = [item for item in payload.get("items", []) or [] if keep_existing(item)]
        skipped_items = [item for item in payload.get("skipped_items", []) or [] if keep_existing(item)]
        if skipped:
            skipped_items.append(changed_item)
        else:
            items.append(changed_item)
        order = {path: idx for idx, path in enumerate(current_paths)}
        items.sort(key=lambda item: order.get(str(item.get("path") or ""), 10**9))
        skipped_items.sort(key=lambda item: order.get(str(item.get("path") or ""), 10**9))

        payload.update(
            {
                "project_root": ".",
                "generated_at": dash.now_iso(),
                "state_path": dash.normalize_rel(root, state_path),
                "items": items,
                "skipped_items": skipped_items,
                "state": state,
                "review_contract_version": dash.REVIEW_CONTRACT_VERSION,
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(dash.html_shell(payload), encoding="utf-8")
        dash.write_json(json_output, payload)
        return f"dashboard_review_html_incremental: {output}\ndashboard_review_json_incremental: {json_output}\nreview_items: {len(items)}\nskipped_approved: {len(skipped_items)}"
    except Exception as exc:  # noqa: BLE001
        full = _build_dashboard_review_full(root, sample_rows)
        return f"dashboard_review_incremental_fallback: {exc}; {full}"


def build_viewers(
    root: Path,
    *,
    target: str,
    mode: str,
    query_artifact: dict[str, Any] | None = None,
    dashboard_artifact: dict[str, Any] | None = None,
    sample_rows: int = 8,
) -> list[str]:
    if mode == "deferred":
        return ["viewer refresh deferred"]
    if mode == "dynamic":
        return ["viewer refresh dynamic: static repository/dashboard HTML not rebuilt; use sql_repository.py serve for live project state."]
    messages: list[str] = []
    if mode == "incremental" and query_artifact:
        messages.append(refresh_repository_incremental(root, query_artifact, sample_rows=sample_rows))
    else:
        messages.append(_build_repository_full(root, sample_rows))
    if target == "query-dashboard":
        if mode == "incremental" and dashboard_artifact:
            messages.append(refresh_dashboard_review_incremental(root, dashboard_artifact, sample_rows=sample_rows))
        else:
            messages.append(_build_dashboard_review_full(root, sample_rows))
    return [message for message in messages if message]

def build_plan(args, tmp_dir: Path) -> dict[str, Any]:
    started = time.perf_counter()
    last_mark = started
    steps: list[dict[str, Any]] = []

    def mark(step: str, status: str = "done", detail: str = "") -> None:
        nonlocal last_mark
        now = time.perf_counter()
        steps.append(
            {
                "step": step,
                "status": status,
                "elapsed_ms": int((now - started) * 1000),
                "duration_ms": int((now - last_mark) * 1000),
                "detail": detail,
            }
        )
        last_mark = now

    root = Path(args.root).resolve()
    source_sql = Path(args.source_sql).resolve()
    result_file = Path(args.result_file).resolve() if args.result_file else None
    dashboard_sql_input = (
        Path(getattr(args, "dashboard_sql_file", "")).resolve()
        if getattr(args, "dashboard_sql_file", None)
        else None
    )
    title = args.title or source_sql.stem
    slug = slugify(args.slug or title, "formalized-sql")
    config = read_project_config(root)
    existing_manifest = read_json(manifest_path(root), {})
    existing_query = current_query_artifact_for_source(root, existing_manifest, source_sql) if source_sql.exists() else None
    workspace_origin = find_query_reference(root, source_sql) if source_sql.exists() else None
    source_temporary_override = copy.deepcopy(
        (workspace_origin or {}).get("temporary_rule_override") or {}
    )
    if existing_query:
        if not args.title:
            title = str(existing_query.get("title") or title)
        if not args.slug:
            slug = str(existing_query.get("slug") or slug)
    context_snapshot = project_context_snapshot(config or {})
    blockers: list[str] = []
    warnings: list[str] = []

    def blocked_plan(**extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "blocked",
            "blockers": blockers,
            "warnings": warnings,
            "root": str(root),
            "title": title,
            "slug": slug,
            "target": args.target,
            "steps": steps,
            "plan_elapsed_ms": int((time.perf_counter() - started) * 1000),
            "artifact_write_plan": skipped_artifact_write_plan(args.target, "blocked_before_artifact_write"),
        }
        payload.update(extra)
        return payload

    for problem in validate_project_config(config, "DASHBOARD" if args.target == "query-dashboard" else "QUERY"):
        blockers.append(problem)
    if not source_sql.exists():
        blockers.append(f"source SQL not found: {source_sql}")
    if dashboard_sql_input and args.target != "query-dashboard":
        blockers.append("--dashboard-sql-file is valid only with --target query-dashboard.")
    if dashboard_sql_input and not dashboard_sql_input.exists():
        blockers.append(f"dashboard SQL not found: {dashboard_sql_input}")
    if args.verification_status in {"passed", "proxy_verified"} and (not result_file or not result_file.exists()):
        blockers.append("verified/proxy formalization requires --result-file with a real .csv or .xlsx result.")
    if args.verification_status in {"passed", "proxy_verified"} and not args.user_confirmed:
        blockers.append(f"{args.verification_status} formalization requires --user-confirmed.")
    if args.verification_status == "proxy_verified":
        for name, value in [("definition_project", args.definition_project), ("execution_project", args.execution_project), ("delivery_project", args.delivery_project), ("future_verification_plan", args.future_verification_plan), ("proxy_limitations", args.proxy_limitations)]:
            if not value:
                blockers.append(f"proxy_verified requires --{name.replace('_', '-')}.")
    if args.verification_status == "skipped":
        for name, value in [("skip_reason", args.skip_reason), ("risk_note", args.risk_note), ("future_verification_plan", args.future_verification_plan)]:
            if not value or len(str(value).strip()) < 8:
                blockers.append(f"skipped formalization requires --{name.replace('_', '-')}.")
    if blockers:
        mark("preflight_inputs", "blocked", "Required project/source/result context is incomplete.")
        return blocked_plan()

    raw_sql = read_text(source_sql)
    persisted_route: dict[str, Any] | None = None
    for route_candidate in [
        (workspace_origin or {}).get("execution_route"),
        (existing_query or {}).get("execution_route"),
    ]:
        if route_matches_context(route_candidate, raw_sql, config):
            persisted_route = copy.deepcopy(route_candidate)
            break
    if persisted_route is None:
        sidecar = route_receipt_path(source_sql)
        if sidecar.exists():
            sidecar_route = read_json(sidecar, {})
            if route_matches_context(sidecar_route, raw_sql, config):
                persisted_route = sidecar_route
    query_config, _ = effective_config_for_context(config, raw_sql, persisted_route)
    if result_file:
        result = inspect_result_file(
            result_file,
            sample_limit=args.sample_rows,
            sql=raw_sql,
            project_config=query_config,
        )
    else:
        result = {
            "file_name": "",
            "file_path": "",
            "file_type": "",
            "row_count": None,
            "columns": [],
            "schema_fingerprint": "",
            "sample_rows": [],
            "notes": "No result file was provided because verification status is skipped.",
        }
    retained_fields, retained_source, retained_error = load_retained_fields(args)
    if retained_error:
        blockers.append(retained_error)
    retained_missing: list[str] = []
    if retained_fields:
        result, retained_missing = apply_retained_fields_override(result, retained_fields, source=retained_source)
        if retained_missing:
            blockers.append("retained fields are not present in result file: " + ", ".join(retained_missing))
    time_coverage = result.get("time_coverage") if isinstance(result.get("time_coverage"), dict) else {}
    time_coverage_blockers = time_coverage_problem_messages(time_coverage)
    applicable_time_blockers: list[str] = []
    coverage_output_blockers: list[str] = []
    if result_file and args.verification_status in {"passed", "proxy_verified"}:
        applicable_time_blockers = time_coverage_blockers
        blockers.extend(applicable_time_blockers)
        if time_coverage.get("required"):
            coverage_fields = {
                str(time_coverage.get("primary_field") or ""),
                str((time_coverage.get("range_fields") or {}).get("start") or ""),
                str((time_coverage.get("range_fields") or {}).get("end") or ""),
            }
            coverage_fields.discard("")
            retained_columns = {str(item) for item in result.get("columns", [])}
            removed_coverage = sorted(coverage_fields - retained_columns)
            if removed_coverage:
                coverage_output_blockers.append(
                    "retained fields removed required actual-time coverage columns: "
                    + ", ".join(removed_coverage)
                )
                blockers.extend(coverage_output_blockers)
    result_detail = f"{result.get('row_count')} rows, {len(result.get('columns', []))} columns"
    override = result.get("retained_fields_override") if isinstance(result.get("retained_fields_override"), dict) else {}
    if override:
        removed_count = len(override.get("removed_columns", []) or [])
        result_detail += f"; retained_fields={override.get('status')}"
        if removed_count:
            result_detail += f"; removed_result_columns={removed_count}"
    mark(
        "inspect_result",
        "blocked"
        if retained_error or retained_missing or applicable_time_blockers or coverage_output_blockers
        else "done",
        result_detail,
    )
    if retained_error or retained_missing or applicable_time_blockers or coverage_output_blockers:
        return blocked_plan(result=result)
    fact_seed, fact_seed_source, fact_seed_warnings = load_fact_bundle(args, source_sql, raw_sql)
    warnings.extend(fact_seed_warnings)
    if args.use_fact_bundle == "required" and not fact_seed:
        blockers.append("--use-fact-bundle required but no valid formalize fact bundle was found.")
    mark("load_fact_bundle", "done" if fact_seed else "warn", fact_seed_source)
    explicit_reference_files = list(getattr(args, "knowledge_reference_file", []) or [])
    explicit_references = load_knowledge_reference_files(root, explicit_reference_files)
    knowledge_references = copy.deepcopy(
        explicit_references
        or (fact_seed.get("knowledge_references") if isinstance(fact_seed, dict) else None)
        or (workspace_origin or {}).get("knowledge_references")
        or []
    )
    knowledge_problems = []
    for reference in knowledge_references:
        if not isinstance(reference, dict):
            knowledge_problems.append("knowledge reference rows must be objects")
            continue
        knowledge_problems.extend(validate_knowledge_reference(root, reference))
    if knowledge_problems:
        blockers.extend(f"knowledge_reference: {problem}" for problem in knowledge_problems)
        mark("knowledge_references", "blocked", f"{len(knowledge_problems)} stale or invalid reference(s)")
        return blocked_plan(result=result, fact_bundle_source=fact_seed_source)
    seed_knowledge_usage = copy.deepcopy(
        (fact_seed.get("knowledge_usage") if isinstance(fact_seed, dict) else None)
        or (workspace_origin or {}).get("knowledge_usage")
        or {}
    )
    knowledge_declaration = str(
        getattr(args, "knowledge_usage", "not-used") or "not-used"
    )
    try:
        if explicit_references or knowledge_declaration != "auto" or not seed_knowledge_usage:
            knowledge_usage = build_knowledge_usage(
                root,
                knowledge_references,
                declaration=knowledge_declaration,
                declaration_source="sql_formalize",
            )
        else:
            knowledge_usage = seed_knowledge_usage
        knowledge_usage_problems = validate_knowledge_usage(root, knowledge_usage, knowledge_references)
        if knowledge_usage.get("status") == "legacy_unknown":
            knowledge_usage_problems.append(
                "legacy_unknown knowledge usage cannot be formalized; pass resolver receipt(s) or --knowledge-usage not-used"
            )
    except ValueError as exc:
        knowledge_usage = {}
        knowledge_usage_problems = [str(exc)]
    if knowledge_usage_problems:
        blockers.extend(f"knowledge_usage: {problem}" for problem in knowledge_usage_problems)
        mark("knowledge_usage", "blocked", f"{len(knowledge_usage_problems)} usage contract problem(s)")
        return blocked_plan(result=result, fact_bundle_source=fact_seed_source)
    mark(
        "knowledge_usage",
        "done",
        f"{knowledge_usage.get('status')} with {len(knowledge_references)} exact dataset reference(s)",
    )
    normalized = normalize_query_sql(raw_sql, query_config)
    warnings.extend(normalized.warnings)
    normalized_for_seed = normalized.sql
    mark("normalize_sql", detail="params CTE normalized" if normalized.changed else "source SQL already normalized")

    pruned_sql, output_contract = prune_final_select_to_result_columns(normalized.sql, result.get("columns", []))
    if pruned_sql != normalized.sql:
        normalized.sql = pruned_sql
        normalized.changed = True
    if output_contract.get("status") not in {"mismatch", "no_result_fields"}:
        internal_sql, internal_report = prune_internal_cte_outputs(normalized.sql)
        if internal_sql != normalized.sql:
            normalized.sql = internal_sql
            normalized.changed = True
        output_contract.update(internal_report)
    result["output_field_contract"] = output_contract
    if output_contract.get("status") == "mismatch":
        blockers.append(
            "result output fields are not all present in SQL final SELECT: "
            + ", ".join(output_contract.get("missing_result_fields", []))
        )
    output_status = "blocked" if output_contract.get("status") == "mismatch" else "done"
    output_detail = str(output_contract.get("status") or "unknown")
    removed_fields = output_contract.get("removed_output_fields") or []
    if removed_fields:
        output_detail += "; removed=" + ", ".join(str(item) for item in removed_fields[:8])
    internal_removed = output_contract.get("internal_pruning_removed_fields") or []
    if internal_removed:
        output_detail += f"; internal_pruned={len(internal_removed)}"
    mark("output_field_contract", output_status, output_detail)
    early_fact_reuse_summary = build_fact_reuse_summary(
        seed=fact_seed,
        source=fact_seed_source,
        root=root,
        raw_sql=raw_sql,
        normalized_for_seed=normalized_for_seed,
        normalized_sql=normalized.sql,
        config=config,
        user_request=str(getattr(args, "user_request", "") or ""),
    )
    if output_contract.get("status") == "mismatch":
        return blocked_plan(
            result=result,
            normalized_changed=normalized.changed,
            output_field_contract=output_contract,
            fact_bundle_source=fact_seed_source,
            fact_reuse_summary=early_fact_reuse_summary,
            sql_fingerprint=sha256_text(normalized.sql),
        )
    query_reuse = query_reuse_decision(
        existing_query,
        explicit_slug=str(args.slug or ""),
        effective_slug=slug,
        normalized_changed=normalized.changed,
        output_contract=output_contract,
    )
    mark("query_artifact_reuse", query_reuse.get("status", "new_query"), str(query_reuse.get("reason") or ""))
    write_plan = artifact_write_plan(query_reuse, args.target)

    canonical_fingerprint = execution_fingerprint(normalized.sql)
    if workspace_origin:
        workspace_origin_plan = {
            "action": (
                "reuse"
                if str(workspace_origin.get("sql_fingerprint") or "") == canonical_fingerprint
                else "save_revision"
            ),
            "reason": (
                "source_sql_already_indexed"
                if str(workspace_origin.get("sql_fingerprint") or "") == canonical_fingerprint
                else "formalize_normalization_changed_indexed_sql"
            ),
            "reference": copy.deepcopy(workspace_origin),
        }
    elif existing_query:
        workspace_origin_plan = {
            "action": "not_required",
            "reason": "source_sql_is_current_formal_query",
            "reference": None,
        }
    else:
        workspace_origin_plan = {
            "action": "save",
            "reason": "canonical_formalize_sql_must_be_indexed_before_formal_artifact_write",
            "reference": None,
        }

    fact_reuse_summary = early_fact_reuse_summary

    query_sql_file = tmp_dir / "query.sql"
    write_text(query_sql_file, normalized.sql)

    sql_facts = build_sql_fact_bundle(
        normalized.sql,
        kind="QUERY",
        root=root,
        result_columns=[str(item) for item in result.get("columns", [])],
    )
    analysis = reusable_logic_seed_dict(
        fact_seed,
        "analysis",
        raw_sql=raw_sql,
        normalized_sql=normalized_for_seed,
    )
    if analysis:
        sql_facts["analysis"] = copy.deepcopy(analysis)
        mark("analyze_sql", "reused", f"{len(sql_facts.get('source_tables', []))} physical sources from logic-matched fact bundle")
    else:
        analysis = sql_facts["analysis"]
        mark("analyze_sql", detail=f"{len(sql_facts.get('source_tables', []))} physical sources detected")
    analysis, removed_analysis_items = apply_output_field_contract_to_analysis(analysis, output_contract)
    sql_facts["analysis"] = copy.deepcopy(analysis)
    if removed_analysis_items:
        mark(
            "analysis_output_contract",
            "pruned",
            ", ".join(f"{item['section']}:{item['field']}" for item in removed_analysis_items[:8]),
        )
    else:
        mark("analysis_output_contract", "matched", "analysis already follows retained result fields")
    normalized_route = (
        rebase_execution_route_for_sql(normalized.sql, config, persisted_route)
        if persisted_route
        else None
    )
    rule_context = reusable_formalize_rule_context(
        fact_seed,
        root=root,
        raw_sql=raw_sql,
        normalized_sql=normalized.sql,
        lifecycle_stage="retained_query",
        user_request=str(getattr(args, "user_request", "") or ""),
    )
    if rule_context:
        candidate_check = rule_context.get("candidate_sql_check") if isinstance(rule_context.get("candidate_sql_check"), dict) else {}
        mark("rule_context", "reused", f"formalize seed {candidate_check.get('status', 'not_run')}")
    else:
        source_rule_application = seed_rule_application(fact_seed)
        source_fingerprints = seed_sql_fingerprints(fact_seed)
        exact_parent_sql = bool(
            source_fingerprints
            & {
                sha256_text(raw_sql),
                sha256_text(normalized_for_seed),
                sha256_text(normalized.sql),
            }
        )
        rule_context_args = {
            "mode": "formalize",
            "lifecycle_stage": "retained_query",
            "parent_rule_application": source_rule_application or None,
            "inheritance_contract": build_inheritance_contract(
                "lifecycle_promotion_exact_sql",
                same_execution_fingerprint=exact_parent_sql,
                parent_asset=public_query_workspace_origin(workspace_origin) if workspace_origin else {},
            ),
        }
        if normalized_route is not None:
            rule_context_args["execution_route"] = normalized_route
        rule_context = run_rule_context(
            root,
            query_sql_file,
            str(getattr(args, "user_request", "") or ""),
            **rule_context_args,
        )
        candidate_check = rule_context.get("candidate_sql_check") if isinstance(rule_context.get("candidate_sql_check"), dict) else {}
        mark("rule_context", "done", candidate_check.get("status", "not_run"))
    generation_gate = ensure_rule_context_generation_gate(
        rule_context,
        sql=normalized.sql,
        config=config,
        execution_route=normalized_route,
    )
    query_execution_route = (
        (rule_context.get("project_contract_check") or {}).get("execution_route")
        if isinstance(rule_context.get("project_contract_check"), dict)
        else None
    )
    gate_status = str(generation_gate.get("status") or "not_run")
    if steps and steps[-1].get("step") == "rule_context":
        steps[-1]["status"] = "blocked" if gate_status in {"conflict", "error"} else steps[-1].get("status", "done")
        steps[-1]["detail"] = gate_status
    if gate_status == "conflict":
        blockers.extend([str(item) for item in generation_gate.get("blockers", [])] or ["rule-context generation gate conflict"])
    if gate_status == "error":
        blockers.extend([str(item) for item in generation_gate.get("blockers", [])] or ["rule-context generation gate failed"])
    if gate_status in {"conflict", "error"}:
        if unresolved_temporary_rule_override(source_temporary_override):
            blockers.insert(
                0,
                "temporary_rule_override_unresolved: this workspace query used a user-scoped "
                "canonical-rule override; resolve the current strict conflict before formalization.",
            )
        return blocked_plan(
            result=result,
            analysis=analysis,
            normalized_changed=normalized.changed,
            output_field_contract=output_contract,
            fact_bundle_source=fact_seed_source,
            fact_reuse_summary=fact_reuse_summary,
            query_reuse=query_reuse,
            rule_context_status=gate_status,
            sql_fingerprint=sha256_text(normalized.sql),
        )

    perf = reusable_performance_level(fact_seed, raw_sql=raw_sql, normalized_sql=normalized.sql, config=config)
    perf_reused = bool(perf)
    if not perf:
        perf = performance_level(
            normalized.sql,
            config,
            "QUERY",
            reusable=True,
            sql_facts=sql_facts,
            execution_route=query_execution_route,
        )
    perf_detail = f"{perf.get('optimization_tier', 'unknown')} score={perf.get('preflight_score', 0)} triggers={len(perf.get('preflight_triggers', []) or [])}"
    perf_status = "reused" if perf_reused else "blocked" if perf.get("preflight_status") == "block" else "done"
    mark("performance_preflight", perf_status, perf_detail)
    if perf.get("preflight_status") == "block":
        blockers.extend(perf.get("risk_items", []) or ["performance preflight blocked formal SQL"])
        return blocked_plan(
            result=result,
            analysis=analysis,
            normalized_changed=normalized.changed,
            output_field_contract=output_contract,
            fact_bundle_source=fact_seed_source,
            fact_reuse_summary=fact_reuse_summary,
            query_reuse=query_reuse,
            rule_context_status=candidate_check.get("status", "not_run"),
            performance_level=perf,
            blocked_stage="query_formalization",
            blocked_reason="performance_preflight",
            sql_fingerprint=sha256_text(normalized.sql),
        )

    summary_cache = semantic_summary_cache_metadata(root, sql=normalized.sql, result=result, title=title, config=config)
    summary_cache_status = "not_used"
    summary = repository_summary_from_seed(fact_seed, raw_sql=raw_sql, normalized_sql=normalized_for_seed, result=result)
    if summary:
        mark("repository_summary", "reused", str(summary.get("semantic_summary_quality") or "seeded"))
        summary_cache_status = "seed_preferred"
        fact_reuse_summary["facts"]["repository_summary"]["final_source"] = "seed"
    else:
        summary = load_cached_repository_summary(summary_cache, result=result)
        if summary:
            mark("repository_summary", "cached", str(summary.get("semantic_summary_quality") or "cached"))
            summary_cache_status = "hit"
            fact_reuse_summary["facts"]["repository_summary"]["final_source"] = "semantic_cache"
        else:
            summary = build_repository_summary(
                root=root,
                sql=normalized.sql,
                title=title,
                analysis=analysis,
                result=result,
                rule_context=rule_context,
                sql_facts=sql_facts,
            )
            summary = seeded_repository_summary(summary, fact_seed, result=result)
            mark("repository_summary", "done", str(summary.get("semantic_summary_quality") or "deterministic"))
            summary_cache_status = "miss"
            fact_reuse_summary["facts"]["repository_summary"]["final_source"] = "built"
    fact_reuse_summary["facts"]["repository_summary"]["semantic_cache_status"] = summary_cache_status
    summary = apply_output_field_contract_to_summary(summary, output_contract)
    if needs_llm_summary(summary):
        blockers.append(
            "repository_summary is too low-confidence for formal save; provide a better formalize fact bundle/seed or improve the SQL title/comments so the repository page can explain the asset."
        )

    persisted_workspace_route = copy.deepcopy(normalized_route or {})
    if not persisted_workspace_route:
        if route_matches_context(query_execution_route, normalized.sql, config):
            persisted_workspace_route = copy.deepcopy(query_execution_route)
        else:
            persisted_workspace_route = execution_route_for_file(
                Path(args.source_sql).resolve(),
                normalized.sql,
                config,
                precomputed_route=query_execution_route,
            )
    q_spec = query_spec(
        root=root,
        sql=normalized.sql,
        title=title,
        config=config,
        analysis=analysis,
        result=result,
        repository_summary=summary,
        rule_context=rule_context,
        performance=perf,
        knowledge_references=knowledge_references,
        knowledge_usage=knowledge_usage,
        execution_route_override=persisted_workspace_route,
    )
    if workspace_origin:
        q_spec["origin_query_workspace"] = public_query_workspace_origin(workspace_origin)
    bundle = FormalizeBundle(
        source="sql_formalize.py",
        sql_fingerprint=sha256_text(normalized.sql),
        logic_fingerprint=sql_facts["logic_fingerprint"],
        normalized_changed=normalized.changed,
        result_schema_fingerprint=str(result.get("schema_fingerprint") or ""),
        project_config_fingerprint=config_fingerprint(config),
        project_rules_fingerprint=project_rules_fingerprint(root),
        fact_bundle_source=fact_seed_source,
        analysis=analysis,
        sql_facts=sql_facts,
        rule_context_status=candidate_check.get("status", "not_run"),
        repository_summary_quality=str(summary.get("semantic_summary_quality") or ""),
        performance_fingerprint=str(q_spec.get("performance_level", {}).get("performance_fingerprint") or ""),
        performance_level=q_spec.get("performance_level") if isinstance(q_spec.get("performance_level"), dict) else perf,
        output_field_contract=output_contract,
        fact_reuse_summary=fact_reuse_summary,
        knowledge_references=knowledge_references,
        knowledge_usage=knowledge_usage,
    )
    q_spec["formalize_bundle"] = bundle.public()
    if workspace_origin:
        q_spec["formalize_bundle"]["origin_query_workspace"] = public_query_workspace_origin(workspace_origin)
    mark("build_query_spec", detail=f"summary={summary.get('semantic_summary_quality')}")
    param_problems = query_params_contract_problems(normalized.sql, config, q_spec)
    blockers.extend(param_problems)
    blocked_stage = "query_formalization" if blockers else ""

    query_slug = slug
    query_rel = planned_rel(root, "QUERY", query_slug)
    validation_rel = planned_rel(root, "VALIDATION", query_slug) if args.target == "query-dashboard" else ""
    dashboard_rel = planned_rel(root, "DASHBOARD", query_slug) if args.target == "query-dashboard" else ""
    run_status = args.verification_status
    concepts = csv_or_inferred(args.concept_keys, concept_keys_from_summary(summary))
    if run_status == "proxy_verified" and not concepts:
        blockers.append("proxy_verified requires --concept-keys or automatically matched concept keys.")

    dashboard_sql = ""
    dash_blockers: list[str] = []
    dashboard_contract_preview: dict[str, Any] = {}
    dashboard_rule_context: dict[str, Any] = {}
    dashboard_generation_gate: dict[str, Any] = {}
    dashboard_execution_route: dict[str, Any] = {}
    dashboard_candidate_source = "not_applicable"
    if args.target == "query-dashboard":
        query_stage_blockers = list(blockers)
        if query_stage_blockers:
            blocked_stage = "query_formalization"
            mark("preview_dashboard_contract", "blocked", "query formalization blockers; skipped validation, dashboard candidate, and top-contract preview")
            dashboard_contract_preview = {
                "status": "blocked",
                "sql_fingerprint": "",
                "result_schema_fingerprint": str(result.get("schema_fingerprint") or ""),
                "validation_eligible": False,
                "error_count": 0,
                "warning_count": 0,
                "warnings": [],
                "blockers": query_stage_blockers,
                "skipped_reason": "query_formalization_blockers",
                "reuse_scope": "not_applicable",
            }
        else:
            preview_run = {
                "status": run_status,
                "path": f"analyses/{slugify(query_slug, 'formalized-sql')}/runs/planned_fast_formalize.md",
                "evidence_file": result_file.name if result_file else "",
                "result_file_type": result_file.suffix.lower() if result_file else "",
                "user_confirmed": bool(args.user_confirmed),
                "definition_project": args.definition_project or root.name,
                "execution_project": args.execution_project or root.name,
                "delivery_project": args.delivery_project or root.name,
                "concept_keys": concepts,
                "proxy_limitations": args.proxy_limitations or "",
                "skip_reason": args.skip_reason or "",
                "risk_note": args.risk_note or "",
                "future_verification_plan": args.future_verification_plan or "",
                "result_summary": "planned fast formalization result evidence",
            }
            if result.get("time_coverage"):
                preview_run["result_time_coverage"] = copy.deepcopy(
                    result["time_coverage"]
                )
            preview_validation = validation_spec(
                query_sql_path=query_rel,
                run_record=preview_run,
                query_spec_doc=q_spec,
                title=title,
            )
            preview_validation_eligible = bool(preview_validation.get("promotion", {}).get("eligible"))
            if not preview_validation_eligible:
                blocked_stage = "validation_promotion"
                validation_blockers = preview_validation.get("promotion", {}).get("blockers") or ["validation promotion gate is not eligible for dashboard formalization"]
                blockers.extend(validation_blockers)
                mark("preview_dashboard_contract", "blocked", "validation promotion gate not eligible; skipped dashboard candidate and top-contract preview")
                dashboard_contract_preview = {
                    "status": "blocked",
                    "sql_fingerprint": "",
                    "result_schema_fingerprint": str(result.get("schema_fingerprint") or ""),
                    "validation_eligible": False,
                    "error_count": 0,
                    "warning_count": 0,
                    "warnings": [],
                    "blockers": validation_blockers,
                    "skipped_reason": "validation_promotion_not_eligible",
                    "reuse_scope": "not_applicable",
                }
            else:
                if dashboard_sql_input:
                    dashboard_sql = dashboardize_time_params(
                        read_text(dashboard_sql_input),
                        config.get("sql_dialect", "StarRocks"),
                    )
                    dashboard_candidate_source = "explicit_dashboard_sql_file"
                else:
                    dashboard_sql = dashboardize_time_params(
                        normalized.sql,
                        config.get("sql_dialect", "StarRocks"),
                    )
                    dashboard_candidate_source = "derived_from_query"
                dashboard_sql_file = tmp_dir / "dashboard.sql"
                write_text(dashboard_sql_file, dashboard_sql)
                dash_blockers.extend(dashboard_blockers(dashboard_sql, result))
                if not dash_blockers:
                    parent_dashboard_route = copy.deepcopy(query_execution_route or {})
                    if dashboard_sql_input:
                        sidecar = route_receipt_path(dashboard_sql_input)
                        if sidecar.exists():
                            candidate_route = read_json(sidecar, {})
                            if isinstance(candidate_route, dict):
                                parent_dashboard_route = candidate_route
                    dashboard_execution_route = (
                        rebase_execution_route_for_sql(
                            dashboard_sql,
                            config,
                            parent_dashboard_route,
                        )
                        if parent_dashboard_route
                        else execution_route_for_file(
                            dashboard_sql_input or dashboard_sql_file,
                            dashboard_sql,
                            config,
                        )
                    )
                    dashboard_rule_context_args = {
                        "mode": "formalize",
                        "lifecycle_stage": "dashboard_delivery",
                        "parent_rule_application": rule_context.get("rule_application")
                        if isinstance(rule_context.get("rule_application"), dict)
                        else None,
                        "inheritance_contract": build_inheritance_contract(
                            "dashboard_derivative_same_contract",
                            same_logic_contract=dashboard_candidate_source == "derived_from_query",
                            parent_asset={
                                "kind": "QUERY",
                                "path": query_rel,
                                "sql_fingerprint": sha256_text(normalized.sql),
                            },
                        ),
                    }
                    if dashboard_execution_route:
                        dashboard_rule_context_args["execution_route"] = dashboard_execution_route
                    dashboard_rule_context = run_rule_context(
                        root,
                        dashboard_sql_file,
                        str(getattr(args, "user_request", "") or ""),
                        **dashboard_rule_context_args,
                    )
                    dashboard_generation_gate = ensure_rule_context_generation_gate(
                        dashboard_rule_context,
                        sql=dashboard_sql,
                        config=config,
                        execution_route=dashboard_execution_route or None,
                    )
                    dashboard_gate_status = str(
                        dashboard_generation_gate.get("status") or "not_run"
                    )
                    if dashboard_gate_status in {"conflict", "error"}:
                        dash_blockers.extend(
                            str(item)
                            for item in dashboard_generation_gate.get("blockers", [])
                        )
                        if not dash_blockers:
                            dash_blockers.append(
                                "dashboard_delivery rule-context gate did not pass"
                            )
                    mark(
                        "dashboard_rule_context",
                        "blocked" if dashboard_gate_status in {"conflict", "error"} else "done",
                        dashboard_gate_status,
                    )
                blockers.extend(dash_blockers)
                if dash_blockers:
                    blocked_stage = "dashboard_candidate"
                mark(
                    "build_dashboard_candidate",
                    "blocked" if dash_blockers else "done",
                    dashboard_candidate_source
                    if not dash_blockers
                    else "; ".join(dash_blockers[:2]),
                )
                if not dash_blockers:
                    preview_dashboard = dashboard_spec(
                        dashboard_sql=dashboard_sql,
                        query_spec_doc=q_spec,
                        validation_path=validation_rel,
                        run_record=preview_run,
                        query_sql_path=query_rel,
                        title=title,
                        result=result,
                        canonical_rule_context=dashboard_rule_context,
                        config=config,
                    )
                    preview_errors, preview_warnings = validate_top_contract(preview_dashboard, dashboard_sql)
                    if preview_errors:
                        blocked_stage = "dashboard_contract"
                        blockers.extend([f"dashboard top-contract: {item}" for item in preview_errors])
                    warnings.extend([f"dashboard top-contract: {item}" for item in preview_warnings])
                    preview_status = "blocked" if preview_errors else "done"
                    mark("preview_dashboard_contract", preview_status, f"eligible={preview_validation_eligible}, errors={len(preview_errors)}, warnings={len(preview_warnings)}")
                    dashboard_contract_preview = {
                        "status": "passed" if preview_status == "done" else "blocked",
                        "sql_fingerprint": sha256_text(dashboard_sql),
                        "result_schema_fingerprint": str(result.get("schema_fingerprint") or ""),
                        "validation_eligible": preview_validation_eligible,
                        "error_count": len(preview_errors),
                        "warning_count": len(preview_warnings),
                        "warnings": preview_warnings,
                        "rule_context_status": str(
                            dashboard_generation_gate.get("status") or "not_run"
                        ),
                        "reuse_scope": "dashboard_sql_and_result_schema",
                    }

    planned_outputs = {
        "query_sql": query_rel,
        "validation_sql": validation_rel,
        "dashboard_sql": dashboard_rel,
        "viewer_refresh_mode": "explicit_shared_projection",
        "query_sql_reuse_status": query_reuse.get("status") or "new_query",
    }

    return {
        "status": "blocked" if blockers else "ready",
        "root": str(root),
        "title": title,
        "slug": slug,
        "target": args.target,
        "blockers": blockers,
        "warnings": warnings,
        "steps": steps,
        "plan_elapsed_ms": int((time.perf_counter() - started) * 1000),
        "normalized_changed": normalized.changed,
        "result": result,
        "analysis": analysis,
        "sql_facts": sql_facts,
        "project_context": context_snapshot,
        "rule_context_status": candidate_check.get("status", "not_run"),
        "dashboard_rule_context_status": str(
            dashboard_generation_gate.get("status") or "not_run"
        ),
        "dashboard_candidate_source": dashboard_candidate_source,
        "repository_summary_quality": summary.get("semantic_summary_quality"),
        "fact_bundle_source": fact_seed_source,
        "fact_reuse_summary": fact_reuse_summary,
        "query_reuse": query_reuse,
        "query_workspace_origin_plan": workspace_origin_plan,
        "blocked_stage": blocked_stage if blockers else "",
        "artifact_write_plan": skipped_artifact_write_plan(args.target, "blocked_before_artifact_write") if blockers else write_plan,
        "semantic_summary_cache": {
            "status": summary_cache_status,
            "cache_key": summary_cache.get("cache_key", ""),
            "cache_file": summary_cache.get("cache_file", ""),
            "cache_version": SEMANTIC_SUMMARY_CACHE_VERSION,
        },
        "sql_fingerprint": sha256_text(normalized.sql),
        "logic_fingerprint": sql_facts["logic_fingerprint"],
        "planned_outputs": planned_outputs,
        "staging": {
            "query_sql_file": str(query_sql_file),
            "dashboard_sql_file": str(tmp_dir / "dashboard.sql"),
            "query_spec_file": str(tmp_dir / "query.spec.json"),
            "validation_spec_file": str(tmp_dir / "validation.spec.json"),
            "dashboard_spec_file": str(tmp_dir / "dashboard.spec.json"),
        },
        "objects": {
            "query_sql": normalized.sql,
            "dashboard_sql": dashboard_sql,
            "query_spec": q_spec,
            "repository_summary": summary,
            "concept_keys": concepts,
            "output_field_contract": output_contract,
            "dashboard_contract_preview": dashboard_contract_preview,
            "dashboard_rule_context": dashboard_rule_context,
            "dashboard_generation_gate": dashboard_generation_gate,
            "existing_query_artifact": copy.deepcopy(existing_query) if query_reuse.get("status") == "reused" else None,
            "query_workspace_rule_context": rule_context,
            "query_workspace_generation_gate": generation_gate,
        },
    }


def _formal_member_target(manifest: dict[str, Any], member: dict[str, Any]) -> str:
    prefix = f"{manifest.get('directory', '')}/members/"
    path = str(member.get("path") or "")
    if not prefix.strip("/") or not path.startswith(prefix):
        raise SystemExit(f"Formal Asset Package member path is outside its package: {path}")
    return path[len(prefix) :]


def _read_formal_json_member(root: Path, member: dict[str, Any]) -> dict[str, Any]:
    path = root / str(member.get("path") or "")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Formal Asset Package JSON member is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Formal Asset Package JSON member must be an object: {path}")
    return value


def _formal_query_rows(root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    members = [item for item in manifest.get("members", []) if isinstance(item, dict)]
    current_ids = set((manifest.get("current") or {}).get("member_ids") or [])
    query_members = [
        item
        for item in members
        if item.get("member_id") in current_ids
        and str(item.get("role") or "") in FORMAL_QUERY_SQL_ROLES
    ]
    metadata_members = [
        item
        for item in members
        if item.get("member_id") in current_ids and item.get("role") in FORMAL_QUERY_META_ROLES
    ]
    member_by_id = {
        str(item.get("member_id") or ""): item
        for item in members
        if str(item.get("member_id") or "")
    }
    metadata_by_query_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    metadata_payloads = {
        str(item.get("member_id") or ""): _read_formal_json_member(root, item)
        for item in metadata_members
    }
    for edge in manifest.get("lineage", []):
        if not isinstance(edge, dict) or edge.get("relation") not in {"describes", "described_by"}:
            continue
        source_id = str(edge.get("from_member_id") or "")
        target_id = str(edge.get("to_member_id") or "")
        source = member_by_id.get(source_id, {})
        target = member_by_id.get(target_id, {})
        if source.get("role") in FORMAL_QUERY_META_ROLES and target.get("role") in FORMAL_QUERY_SQL_ROLES:
            metadata_by_query_id[target_id] = (source, metadata_payloads[source_id])
        elif target.get("role") in FORMAL_QUERY_META_ROLES and source.get("role") in FORMAL_QUERY_SQL_ROLES:
            metadata_by_query_id[source_id] = (target, metadata_payloads[target_id])
    metadata_by_query_path: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for metadata_member in metadata_members:
        metadata = metadata_payloads[str(metadata_member.get("member_id") or "")]
        query_path = str(metadata.get("path") or "")
        if not query_path:
            raise SystemExit(
                f"Formal query metadata does not identify its query member: {metadata_member.get('path')}"
            )
        if query_path in metadata_by_query_path:
            raise SystemExit(f"Formal Asset Package has duplicate current query metadata: {query_path}")
        metadata_by_query_path[query_path] = (metadata_member, metadata)
    rows: list[dict[str, Any]] = []
    for query_member in query_members:
        target = _formal_member_target(manifest, query_member)
        metadata_member, metadata = metadata_by_query_id.get(
            str(query_member.get("member_id") or ""),
            metadata_by_query_path.get(target, ({}, {})),
        )
        rows.append(
            {
                "member": query_member,
                "target_path": target,
                "metadata_member": metadata_member,
                "metadata": metadata,
                "origin_query_workspace": copy.deepcopy(
                    metadata.get("origin_query_workspace") or {}
                ),
            }
        )
    return rows


def resolve_formal_package_for_source(
    root: Path,
    source_sql: Path,
    workspace_reference: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve one existing Package only from an exact member or Workspace origin."""

    try:
        source_relative = source_sql.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        source_relative = ""
    workspace_query_id = str((workspace_reference or {}).get("query_id") or "")
    matches: list[dict[str, Any]] = []
    try:
        package_entries = list_formal_asset_packages(root)
    except FormalAssetRepositoryError as exc:
        raise SystemExit("Formal Asset Repository cannot be read safely before formalization.") from exc
    for package_entry in package_entries:
        manifest = load_formal_asset_package(root, str(package_entry.get("package_id") or ""))
        matched_rows: list[tuple[dict[str, Any], str]] = []
        for row in _formal_query_rows(root, manifest):
            member_path = str(row["member"].get("path") or "")
            origin_query_id = str(row["origin_query_workspace"].get("query_id") or "")
            if source_relative and member_path == source_relative:
                matched_rows.append((row, "source_member"))
            elif workspace_query_id and origin_query_id == workspace_query_id:
                matched_rows.append((row, "workspace_origin"))
        deduplicated: dict[str, tuple[dict[str, Any], str]] = {}
        for row, reason in matched_rows:
            member_id = str(row["member"].get("member_id") or "")
            prior = deduplicated.get(member_id)
            if prior is None or prior[1] != "source_member":
                deduplicated[member_id] = (row, reason)
        if len(deduplicated) > 1:
            raise SystemExit(
                f"Formalization source matches multiple current queries inside Package {manifest.get('package_id')}."
            )
        if deduplicated:
            row, reason = next(iter(deduplicated.values()))
            if manifest.get("lifecycle_state") != "current":
                raise SystemExit(
                    f"Formalization cannot update non-current Package {manifest.get('package_id')}."
                )
            matches.append(
                {
                    "package_id": str(manifest.get("package_id") or ""),
                    "manifest": manifest,
                    "query": row,
                    "matched_by": reason,
                }
            )
    if len(matches) > 1:
        package_ids = ", ".join(item["package_id"] for item in matches)
        raise SystemExit(
            f"Formalization source matches multiple Formal Asset Packages ({package_ids}); resolve the duplicate origin before saving."
        )
    return matches[0] if matches else None


def _formalization_member_id(target_path: str) -> str:
    digest = hashlib.sha256(target_path.encode("utf-8")).hexdigest()[:24].upper()
    return f"FZ-{digest}"


def _new_repository_member(source_path: Path, target_path: str, role: str) -> dict[str, Any]:
    return {
        "member_id": _formalization_member_id(target_path),
        "source_path": source_path,
        "target_path": target_path,
        "role": role,
        "lifecycle_state": "current",
    }


def _analysis_prefix(slug: str, resolution: dict[str, Any] | None) -> str:
    if resolution:
        target = str(resolution["query"].get("target_path") or "")
        marker = "/query/"
        if marker in target:
            return target.split(marker, 1)[0]
        # Lifecycle, migration, and the project CLI predate the formalization
        # subtree layout. New immutable versions may start a formalization
        # subtree inside the same Package while lineage preserves the old path.
        return f"analyses/{slugify(slug, 'formalized-sql')}"
    return f"analyses/{slugify(slug, 'formalized-sql')}"


def _analysis_member_ids(manifest: dict[str, Any], query_member_id: str) -> set[str]:
    """Return the lineage component for one query, excluding explicit bundles."""

    selected = {query_member_id}
    changed = True
    while changed:
        changed = False
        for edge in manifest.get("lineage", []):
            if not isinstance(edge, dict) or edge.get("relation") == "bundle_member":
                continue
            source = str(edge.get("from_member_id") or "")
            target = str(edge.get("to_member_id") or "")
            if source in selected and target and target not in selected:
                selected.add(target)
                changed = True
            elif target in selected and source and source not in selected:
                selected.add(source)
                changed = True
    return selected


def _next_member_version(
    manifest: dict[str, Any] | None,
    *,
    prefix: str,
    section: str,
    role: str | set[str],
) -> int:
    pattern = re.compile(
        rf"^{re.escape(prefix)}/{re.escape(section)}/v([0-9]+)\.sql$"
    )
    versions: list[int] = []
    accepted_roles = {role} if isinstance(role, str) else set(role)
    for member in (manifest or {}).get("members", []):
        if not isinstance(member, dict) or member.get("role") not in accepted_roles:
            continue
        try:
            target = _formal_member_target(manifest or {}, member)
        except SystemExit:
            continue
        match = pattern.fullmatch(target)
        if match:
            versions.append(int(match.group(1)))
    return max(versions, default=0) + 1


def _stage_artifact_record(
    *,
    project_root: Path,
    staging_root: Path,
    kind: str,
    member_prefix: str,
    section: str,
    version: int,
    slug: str,
    title: str,
    sql_text: str,
    spec_doc: dict[str, Any],
    analysis: dict[str, Any],
    summary: dict[str, Any],
    created_at: str,
    status: str,
    supersedes: list[str] | None = None,
    analysis_type: str | None = None,
    tags: list[str] | None = None,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    linked_query: str = "",
    linked_validation: str = "",
    linked_run: str = "",
    verification_status: str = "not_applicable",
    verification_note: str = "",
    future_verification_plan: str = "",
    project_context: dict[str, Any] | None = None,
    change_type_override: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if has_full_spec_block(sql_text):
        raise SystemExit("Formal SQL must use a short header plus sidecar spec.")
    target_dir = f"{member_prefix}/{section}"
    sql_target = f"{target_dir}/v{version:03d}.sql"
    spec_target = f"{target_dir}/v{version:03d}.spec.json"
    meta_target = f"{target_dir}/v{version:03d}.meta.json"
    stable_title = strip_source_prefix(title)
    change_type = resolve_change_type(
        change_type_override if version > 1 and change_type_override else "auto",
        version,
    )
    workflow = {
        "QUERY": "fast_formalize_query",
        "VALIDATION": "fast_formalize_validation",
        "DASHBOARD": "fast_formalize_dashboard",
    }.get(kind, "fast_formalize")
    generation_provenance = merge_generation_provenance(
        spec_doc.get("generation_provenance")
        if isinstance(spec_doc.get("generation_provenance"), dict)
        else None,
        fallback_generator_script="sql_formalize.py",
        fallback_workflow=workflow,
        artifact_kind=kind,
        saved_at=created_at,
        saved_by_script="sql_formalize.py",
    )
    metadata = {
        "kind": kind,
        "slug": slug,
        "version": version,
        "title": stable_title,
        "source_title": title if title != stable_title else "",
        "status": status,
        "artifact_state": "current",
        "change_type": change_type,
        "supersedes": list(supersedes or []),
        "replaced_by": "",
        "branch_of": "",
        "change_reason": "",
        "path": sql_target,
        "spec_path": spec_target,
        "path_scope": "formal_asset_package_members",
        "spec_storage": "sidecar_json",
        "header_contract_version": "1",
        "generation_provenance": generation_provenance,
        "project_context": (
            copy.deepcopy(spec_doc.get("project_context"))
            if isinstance(spec_doc.get("project_context"), dict)
            else project_context or project_context_snapshot(read_project_config(project_root) or {})
        ),
        "execution_route": copy.deepcopy(spec_doc.get("execution_route") or {}),
        "business_category": analysis.get("business_category", DEFAULT_BUSINESS_CATEGORY),
        "analysis_type": analysis_type or analysis.get("analysis_type", DEFAULT_ANALYSIS_TYPE),
        "tags": tags if tags is not None else analysis.get("tags", []),
        "metrics": metrics
        if metrics is not None
        else list_from_summary(summary.get("metrics", []), "field", "name", "label"),
        "dimensions": dimensions
        if dimensions is not None
        else list_from_summary(summary.get("dimensions", []), "field", "label"),
        "tables": analysis.get("tables", []),
        "intermediate_tables": [],
        "grain": summary.get("grain") or analysis.get("grain", ""),
        "time_grain": analysis.get("time_grain", ""),
        "reusable": True,
        "reuse_candidate": bool(analysis.get("reuse_candidate", False)),
        "reuse_notes": "Fast-formalized Package member; preserve its output contract on reuse.",
        "content_summary": analysis.get("content_summary", ""),
        "auto_metadata": False,
        "auto_metadata_warnings": analysis.get("warnings", []),
        "natural_language_intent": title,
        "linked_query": linked_query,
        "linked_validation": linked_validation,
        "linked_run": linked_run,
        "verification_status": verification_status,
        "verification_note": verification_note,
        "future_verification_plan": future_verification_plan,
        "created_at": created_at,
        "notes": "Generated by sql_formalize.py as one Formal Asset Package transaction.",
    }
    if kind == "DASHBOARD" and verification_status == "unverified_skipped_run":
        metadata["tags"] = [*metadata["tags"], "unvalidated", "no_result_file"]
    if kind == "DASHBOARD" and verification_status == "proxy_verified":
        metadata["tags"] = [*metadata["tags"], "proxy_verified", "needs_target_verification"]
    origin = spec_doc.get("origin_query_workspace")
    if kind == "QUERY" and isinstance(origin, dict) and origin:
        metadata["origin_query_workspace"] = copy.deepcopy(origin)

    stored_spec = copy.deepcopy(spec_doc)
    set_spec_version(stored_spec)
    apply_generation_provenance(stored_spec, generation_provenance)
    sql_path = staging_root / Path(sql_target)
    spec_path = staging_root / Path(spec_target)
    meta_path = staging_root / Path(meta_target)
    sql_path.parent.mkdir(parents=True, exist_ok=True)
    sql_path.write_text(
        stamp_sql_generation(
            project_root,
            replace_or_prepend_short_header(
                kind,
                sql_text,
                build_short_header(project_root, metadata, stored_spec, spec_target),
            ),
        ),
        encoding="utf-8",
    )
    write_json_object(spec_path, stored_spec)
    write_json(meta_path, metadata)
    role_prefix = {"QUERY": "query", "VALIDATION": "validation", "DASHBOARD": "dashboard"}[kind]
    query_role = (
        "formal_query_unverified"
        if kind == "QUERY" and status in {"skipped", "unverified"}
        else "formal_query"
    )
    members = [
        _new_repository_member(sql_path, sql_target, query_role if kind == "QUERY" else f"{role_prefix}_sql"),
        _new_repository_member(spec_path, spec_target, f"{role_prefix}_spec"),
        _new_repository_member(meta_path, meta_target, f"{role_prefix}_meta"),
    ]
    return metadata, members


def _repository_path_for_member(repository_plan, member_id: str) -> str:
    matches = [
        str(item.get("path") or "")
        for item in repository_plan.manifest.get("members", [])
        if isinstance(item, dict) and item.get("member_id") == member_id
    ]
    if len(matches) != 1:
        raise SystemExit(f"Formal Asset Repository plan lost member identity: {member_id}")
    return matches[0]


def execute_plan(args, plan: dict[str, Any]) -> dict[str, Any]:
    if plan["status"] != "ready":
        return plan
    started = time.perf_counter()
    last_mark = started
    execution_steps: list[dict[str, Any]] = []

    def mark(step: str, status: str = "done", detail: str = "") -> None:
        nonlocal last_mark
        now = time.perf_counter()
        execution_steps.append(
            {
                "step": step,
                "status": status,
                "elapsed_ms": int((now - started) * 1000),
                "duration_ms": int((now - last_mark) * 1000),
                "detail": detail,
            }
        )
        last_mark = now

    root = Path(plan["root"]).resolve()
    title = plan["title"]
    slug = plan["slug"]
    objects = plan["objects"]
    analysis = plan["analysis"]
    sql_facts = plan.get("sql_facts") if isinstance(plan.get("sql_facts"), dict) else {}
    project_context = plan.get("project_context") if isinstance(plan.get("project_context"), dict) else None
    summary = objects["repository_summary"]
    result_file = Path(args.result_file).resolve() if args.result_file else None
    if sql_facts.get("execution_fingerprint") != execution_fingerprint(str(objects.get("query_sql") or "")):
        mark("formalize_fact_bundle_identity", "blocked", "planned QUERY and fact bundle fingerprints differ")
        plan.setdefault("blockers", []).append(
            "formalize fact bundle does not match the planned QUERY; rebuild the plan before writing artifacts."
        )
        plan["blocked_stage"] = "formalize_fact_bundle_identity"
        plan["artifact_write_plan"] = skipped_artifact_write_plan(args.target, "stale_or_missing_fact_bundle")
        plan["artifact_write_result"] = skipped_artifact_write_plan(args.target, "stale_or_missing_fact_bundle")
        plan["execution_steps"] = execution_steps
        plan["execution_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        plan["status"] = "blocked"
        plan.pop("objects", None)
        return plan
    mark("formalize_fact_bundle_identity", "reused", "exact planned QUERY facts")
    if args.target == "query-dashboard":
        preview = objects.get("dashboard_contract_preview") if isinstance(objects.get("dashboard_contract_preview"), dict) else {}
        preview_ok, preview_detail = dashboard_contract_preview_matches(preview, str(objects.get("dashboard_sql") or ""), plan.get("result") or {})
        if not preview_ok:
            mark("dashboard_contract_validation", "blocked", preview_detail)
            reason = "dashboard_contract_preview_missing_or_stale"
            plan.setdefault("blockers", []).append(
                "dashboard contract preview is missing or stale before save; rerun sql_formalize planning so no partial artifacts are written."
            )
            plan["blocked_stage"] = "dashboard_contract_preview"
            plan["artifact_write_plan"] = skipped_artifact_write_plan(args.target, reason)
            plan["artifact_write_result"] = skipped_artifact_write_plan(args.target, reason)
            plan["execution_steps"] = execution_steps
            plan["execution_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
            plan["status"] = "blocked"
            plan.pop("objects", None)
            return plan

    workspace_origin_plan = plan.get("query_workspace_origin_plan") if isinstance(plan.get("query_workspace_origin_plan"), dict) else {}
    workspace_reference = workspace_origin_plan.get("reference") if isinstance(workspace_origin_plan.get("reference"), dict) else None
    workspace_action = str(workspace_origin_plan.get("action") or "save")
    package_resolution = resolve_formal_package_for_source(
        root,
        Path(args.source_sql).resolve(),
        workspace_reference,
    )
    if (
        package_resolution
        and package_resolution.get("matched_by") == "source_member"
        and not workspace_reference
    ):
        workspace_action = "not_required"
    mark(
        "resolve_formal_asset_package",
        "reused" if package_resolution else "new",
        str((package_resolution or {}).get("package_id") or "new package"),
    )
    legacy_manifest = manifest_path(root)
    legacy_manifest_before = legacy_manifest.read_bytes() if legacy_manifest.is_file() else None
    if legacy_manifest_before is not None:
        legacy_manifest_value = read_json(legacy_manifest, {})
        if (
            legacy_manifest_value.get("query_workspace_index")
            != "query_workspace/index.json"
            or legacy_manifest_value.get("query_workspace_view")
            != "query_workspace/index.html"
        ):
            raise SystemExit(
                "Legacy manifest is not Workspace-pointer complete; formalization refuses to mutate it."
            )
    if workspace_action != "not_required":
        if workspace_action in {"save", "save_revision"}:
            summary_filters = []
            for item in summary.get("filters", []) if isinstance(summary.get("filters"), list) else []:
                if isinstance(item, dict):
                    value = str(item.get("condition") or item.get("label") or "").strip()
                else:
                    value = str(item or "").strip()
                if value:
                    summary_filters.append(value)
            source_sql_path = Path(args.source_sql).resolve()
            workspace_facts = {
                "analysis": analysis,
                "sql_fact_bundle": copy.deepcopy(sql_facts),
                "logic_fingerprint": sql_facts.get("logic_fingerprint", ""),
                "execution_route": copy.deepcopy(objects.get("query_spec", {}).get("execution_route") or {}),
                "business_category": analysis.get("business_category", DEFAULT_BUSINESS_CATEGORY),
                "analysis_type": analysis.get("analysis_type", DEFAULT_ANALYSIS_TYPE),
                "tables": analysis.get("tables", []),
                "source_logs": summary.get("source_logs", []),
                "metrics": list_from_summary(summary.get("metrics", []), "field", "name", "label"),
                "dimensions": list_from_summary(summary.get("dimensions", []), "field", "label"),
                "filters": summary_filters,
                "params": copy.deepcopy(sql_facts.get("params") or {}),
                "grain": summary.get("grain") or analysis.get("grain", ""),
                "time_grain": analysis.get("time_grain", ""),
                "tags": analysis.get("tags", []),
            }
            source_is_project_local = is_query_project_local(root, source_sql_path)
            source_intake = copy.deepcopy((workspace_reference or {}).get("source_intake") or {})
            if not source_intake and not source_is_project_local:
                source_intake = external_source_intake(source_sql_path)
            with project_staging_directory(root, "sql_formalize_workspace_") as workspace_tmp:
                canonical_sql_path = Path(workspace_tmp) / "query.sql"
                write_text(canonical_sql_path, str(objects.get("query_sql") or ""))
                workspace_save = save_workspace_query(
                    root=root,
                    source_sql=canonical_sql_path,
                    title=title,
                    purpose=str(summary.get("purpose") or summary.get("business_question") or title),
                    business_question=str(summary.get("business_question") or summary.get("purpose") or title),
                    status="result_confirmed",
                    query_id=str((workspace_reference or {}).get("query_id") or "") if workspace_action == "save_revision" else "",
                    source_kind="user_provided" if source_is_project_local else "external_import",
                    tags=analysis.get("tags", []),
                    revision_note="User supplied real result evidence and requested formalization.",
                    change_type="correction" if workspace_action == "save_revision" else "auto",
                    coverage_relation="same_contract" if workspace_action == "save_revision" else "",
                    gate=objects.get("query_workspace_generation_gate") if isinstance(objects.get("query_workspace_generation_gate"), dict) else {},
                    rule_context=objects.get("query_workspace_rule_context") if isinstance(objects.get("query_workspace_rule_context"), dict) else {},
                    gate_mode="formalize",
                    facts=workspace_facts,
                    write_seed=True,
                    source_intake=source_intake or None,
                    knowledge_references=list(objects.get("query_spec", {}).get("knowledge_references", [])),
                )
            saved_workspace_path = root / str(workspace_save.get("path") or "")
            workspace_reference = find_query_reference(root, saved_workspace_path)
            if not workspace_reference:
                raise SystemExit("Query workspace save succeeded but the indexed source reference could not be resolved.")
            if workspace_save.get("status") == "reused" and workspace_reference.get("status") not in {"result_confirmed", "promoted"}:
                transition_workspace_query(
                    root=root,
                    query_id=str(workspace_reference.get("query_id") or ""),
                    sql_path=str(workspace_reference.get("path") or ""),
                    status="result_confirmed",
                    reason="User supplied real result evidence and requested formalization.",
                    result_status=args.verification_status,
                )
                workspace_reference = find_query_reference(root, Path(args.source_sql).resolve())
            mark("save_query_workspace_origin", "reused" if workspace_save.get("status") == "reused" else "done", str(workspace_reference.get("path") or ""))
        elif workspace_reference and workspace_reference.get("status") not in {"result_confirmed", "promoted"}:
            transition_workspace_query(
                root=root,
                query_id=str(workspace_reference.get("query_id") or ""),
                sql_path=str(workspace_reference.get("path") or ""),
                status="result_confirmed",
                reason="User supplied real result evidence and requested formalization.",
                result_status=args.verification_status,
            )
            workspace_reference = find_query_reference(root, Path(args.source_sql).resolve())
            mark("save_query_workspace_origin", "updated", str((workspace_reference or {}).get("path") or ""))
        else:
            mark("save_query_workspace_origin", "reused", str(workspace_reference.get("path") or ""))
        origin_contract = public_query_workspace_origin(workspace_reference)
        objects["query_spec"]["origin_query_workspace"] = origin_contract
        objects["query_spec"].setdefault("formalize_bundle", {})["origin_query_workspace"] = origin_contract
        plan["query_workspace_origin"] = origin_contract
    else:
        mark("save_query_workspace_origin", "skipped", "source SQL is already a current formal QUERY artifact")
    if legacy_manifest_before is not None and legacy_manifest.read_bytes() != legacy_manifest_before:
        raise SystemExit("Workspace-origin handling changed the legacy manifest; formalization stopped.")
    inherited_origin = copy.deepcopy(
        (package_resolution or {}).get("query", {}).get("origin_query_workspace") or {}
    )
    if workspace_reference:
        inherited_origin = public_query_workspace_origin(workspace_reference)
    if inherited_origin:
        objects["query_spec"]["origin_query_workspace"] = copy.deepcopy(inherited_origin)
        objects["query_spec"].setdefault("formalize_bundle", {})[
            "origin_query_workspace"
        ] = copy.deepcopy(inherited_origin)

    created_at = now_iso()
    existing_manifest = (
        package_resolution.get("manifest")
        if isinstance((package_resolution or {}).get("manifest"), dict)
        else None
    )
    member_prefix = _analysis_prefix(slug, package_resolution)
    existing_targets = {
        _formal_member_target(existing_manifest, item)
        for item in (existing_manifest or {}).get("members", [])
        if isinstance(item, dict)
    }
    current_ids = set(((existing_manifest or {}).get("current") or {}).get("member_ids") or [])
    new_members: list[dict[str, Any]] = []
    lineage: list[dict[str, str]] = []
    state_updates: list[dict[str, str]] = []
    query_artifact: dict[str, Any]
    query_member_id = ""
    query_reused = False
    validation_member_id = ""
    dashboard_member_id = ""
    run_member_id = ""

    with project_staging_directory(root, "sql_formalize_package_") as package_tmp:
        staging_root = Path(package_tmp).resolve()
        try:
            staging_root.relative_to(root)
        except ValueError as exc:
            raise SystemExit("Formalization staging must stay inside the project root.") from exc

        existing_query = (package_resolution or {}).get("query") or {}
        existing_query_member = (
            existing_query.get("member")
            if isinstance(existing_query.get("member"), dict)
            else None
        )
        query_target = str(existing_query.get("target_path") or "")
        if existing_query_member:
            existing_sql_path = root / str(existing_query_member.get("path") or "")
            desired_query_role = (
                "formal_query_unverified"
                if args.verification_status == "skipped"
                else "formal_query"
            )
            query_reused = (
                bool(existing_query.get("metadata"))
                and
                execution_fingerprint(existing_sql_path.read_text(encoding="utf-8-sig"))
                == execution_fingerprint(str(objects.get("query_sql") or ""))
                and (
                    existing_query_member.get("role") == desired_query_role
                    or (
                        desired_query_role == "formal_query"
                        and existing_query_member.get("role")
                        in {"formal_query_sql", "query_sql"}
                    )
                )
            )
        if query_reused:
            query_member_id = str(existing_query_member.get("member_id") or "")
            query_artifact = copy.deepcopy(existing_query.get("metadata") or {})
            if not query_artifact:
                raise SystemExit("Reusable formal query is missing its Package metadata member.")
            staged_query = staging_root / Path(query_target)
            staged_query.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / str(existing_query_member.get("path") or ""), staged_query)
            mark("stage_query_members", "reused", query_target)
        else:
            query_version = _next_member_version(
                existing_manifest,
                prefix=member_prefix,
                section="query",
                role=set(FORMAL_QUERY_SQL_ROLES),
            )
            workspace_change_type = str((workspace_reference or {}).get("change_type") or "")
            formal_change_type = {
                "correction": "correction",
                "replacement": "replacement",
                "superset": "superset",
                "parameter_refresh": "refresh",
            }.get(workspace_change_type, "")
            supersedes = [query_target] if query_target else []
            query_artifact, query_members = _stage_artifact_record(
                project_root=root,
                staging_root=staging_root,
                kind="QUERY",
                member_prefix=member_prefix,
                section="query",
                version=query_version,
                slug=slug,
                title=title,
                sql_text=objects["query_sql"],
                spec_doc=objects["query_spec"],
                analysis=analysis,
                summary=summary,
                created_at=created_at,
                status="verified" if args.verification_status == "passed" else args.verification_status,
                supersedes=supersedes,
                analysis_type=analysis.get("analysis_type", "aggregate_query"),
                metrics=list_from_summary(summary.get("metrics", []), "field", "name"),
                dimensions=list_from_summary(summary.get("dimensions", []), "field", "label"),
                tags=analysis.get("tags", []),
                project_context=project_context,
                change_type_override=formal_change_type,
            )
            new_members.extend(query_members)
            query_target = str(query_artifact["path"])
            query_member_id = str(query_members[0]["member_id"])
            mark("stage_query_members", detail=query_target)

        run_manifest: dict[str, Any] = {"run_evidence": []}
        run_record = save_run_record(
            root=staging_root,
            manifest=run_manifest,
            args=args,
            source_artifact=query_target,
            sql_path=query_target,
            slug=slug,
            title=title,
            result=plan["result"],
            result_file=result_file,
            concept_keys=objects.get("concept_keys", []),
            sql_facts=sql_facts,
            run_relative_dir=Path(member_prefix) / "runs",
            reserved_paths=existing_targets,
        )
        run_json_target = PurePosixPath(str(run_record["path"])).with_suffix(".json").as_posix()
        run_json_path = staging_root / Path(run_json_target)
        write_json(run_json_path, run_record)
        run_markdown_path = staging_root / Path(str(run_record["path"]))
        run_member = _new_repository_member(
            run_markdown_path,
            str(run_record["path"]),
            "run_record",
        )
        run_member_id = str(run_member["member_id"])
        new_members.extend(
            [
                run_member,
                _new_repository_member(run_json_path, run_json_target, "run_meta"),
            ]
        )
        if run_record.get("evidence_file"):
            evidence_target = str(run_record["evidence_file"])
            new_members.append(
                _new_repository_member(
                    staging_root / Path(evidence_target),
                    evidence_target,
                    "result_evidence",
                )
            )
        mark("stage_run_evidence", detail=str(run_record.get("path") or ""))

        validation_target = ""
        dashboard_target = ""
        if args.target == "query-dashboard":
            validation_version = _next_member_version(
                existing_manifest,
                prefix=member_prefix,
                section="validation",
                role="validation_sql",
            )
            validation_target = (
                f"{member_prefix}/validation/v{validation_version:03d}.sql"
            )
            v_spec = validation_spec(
                query_sql_path=query_target,
                run_record=run_record,
                query_spec_doc=objects["query_spec"],
                title=title,
            )
            validation_artifact, validation_members = _stage_artifact_record(
                project_root=root,
                staging_root=staging_root,
                kind="VALIDATION",
                member_prefix=member_prefix,
                section="validation",
                version=validation_version,
                slug=f"{slug}-validation",
                title=f"{title} 验证",
                sql_text="SELECT 1 AS validation_marker;\n",
                spec_doc=v_spec,
                analysis=analysis,
                summary=summary,
                created_at=created_at,
                status="promoted" if v_spec.get("promotion", {}).get("eligible") else "blocked",
                analysis_type="metric_validation",
                metrics=list_from_summary(summary.get("metrics", []), "field", "name"),
                dimensions=list_from_summary(summary.get("dimensions", []), "field", "label"),
                tags=["validation", "fast_formalize"],
                linked_query=query_target,
                linked_run=str(run_record.get("path") or ""),
                project_context=project_context,
            )
            new_members.extend(validation_members)
            validation_member_id = str(validation_members[0]["member_id"])
            validation_target = str(validation_artifact["path"])
            mark("stage_validation_members", detail=validation_target)

            dash_sql = objects["dashboard_sql"]
            dash_spec = dashboard_spec(
                dashboard_sql=dash_sql,
                query_spec_doc=objects["query_spec"],
                validation_path=validation_target,
                run_record=run_record,
                query_sql_path=query_target,
                title=title,
                result=plan["result"],
                canonical_rule_context=objects.get("dashboard_rule_context") or {},
                config=read_project_config(root),
            )
            preview = (
                objects.get("dashboard_contract_preview")
                if isinstance(objects.get("dashboard_contract_preview"), dict)
                else {}
            )
            mark(
                "dashboard_contract_validation",
                "reused",
                f"preview warnings={preview.get('warning_count', 0)}",
            )
            dash_verification = (
                "proxy_verified"
                if args.verification_status == "proxy_verified"
                else "unverified_skipped_run"
                if args.verification_status == "skipped"
                else "verified"
            )
            dashboard_version = _next_member_version(
                existing_manifest,
                prefix=member_prefix,
                section="dashboard",
                role={"dashboard_delivery_sql", "dashboard_sql"},
            )
            dashboard_artifact, dashboard_members = _stage_artifact_record(
                project_root=root,
                staging_root=staging_root,
                kind="DASHBOARD",
                member_prefix=member_prefix,
                section="dashboard",
                version=dashboard_version,
                slug=f"{slug}-dashboard",
                title=f"{title} 看板 SQL",
                sql_text=dash_sql,
                spec_doc=dash_spec,
                analysis=analysis,
                summary=summary,
                created_at=created_at,
                status="ready",
                analysis_type="dashboard_table",
                metrics=[
                    str(item.get("field") or "")
                    for item in dash_spec.get("metrics", [])
                    if isinstance(item, dict) and item.get("field")
                ],
                dimensions=[
                    str(item.get("field") or "")
                    for item in dash_spec.get("dimensions", [])
                    if isinstance(item, dict) and item.get("field")
                ],
                tags=["dashboard", "fast_formalize"],
                linked_query=query_target,
                linked_validation=validation_target,
                linked_run=str(run_record.get("path") or ""),
                verification_status=dash_verification,
                verification_note="Generated by sql_formalize.py from user-confirmed result evidence.",
                future_verification_plan=args.future_verification_plan or "",
                project_context=project_context,
            )
            new_members.extend(dashboard_members)
            dashboard_member_id = str(dashboard_members[0]["member_id"])
            dashboard_target = str(dashboard_artifact["path"])
            mark("stage_dashboard_members", detail=dashboard_target)
        else:
            mark("stage_validation_members", "skipped", "target=query")

        generated_targets = [str(item["target_path"]) for item in new_members]
        duplicate_targets = sorted(set(existing_targets).intersection(generated_targets))
        if duplicate_targets:
            raise SystemExit(
                "Formalization generated immutable Package member path collisions: "
                + ", ".join(duplicate_targets)
            )
        for item in new_members:
            role = str(item.get("role") or "")
            if role.endswith("_sql") or role in {"formal_query", "formal_query_unverified"}:
                transforms = sql_side_privacy_transforms(
                    Path(item["source_path"]).read_text(encoding="utf-8-sig")
                )
                if transforms:
                    functions = ", ".join(
                        sorted({str(row.get("function") or "") for row in transforms})
                    )
                    raise SystemExit(
                        f"Formal Package SQL performs forbidden SQL-side de-identification: {functions}."
                    )

        retire_roles = {"run_record", "run_meta", "result_evidence"}
        if not query_reused:
            retire_roles.update(
                FORMAL_QUERY_SQL_ROLES
                | FORMAL_QUERY_SPEC_ROLES
                | FORMAL_QUERY_META_ROLES
                | FORMAL_VALIDATION_ROLES
                | FORMAL_DASHBOARD_ROLES
            )
        elif args.target == "query-dashboard":
            retire_roles.update(
                {
                    "validation_sql",
                    "validation_spec",
                    "validation_meta",
                    "dashboard_sql",
                    "dashboard_spec",
                    "dashboard_meta",
                }
            )
        analysis_member_ids = (
            _analysis_member_ids(
                existing_manifest or {},
                str((existing_query_member or {}).get("member_id") or ""),
            )
            if existing_query_member
            else set()
        )
        for member in (existing_manifest or {}).get("members", []):
            if not isinstance(member, dict) or member.get("member_id") not in current_ids:
                continue
            target = _formal_member_target(existing_manifest or {}, member)
            belongs_to_analysis = (
                member.get("member_id") in analysis_member_ids
                or target.startswith(f"{member_prefix}/")
            )
            if belongs_to_analysis and member.get("role") in retire_roles:
                state_updates.append(
                    {
                        "member_id": str(member.get("member_id") or ""),
                        "lifecycle_state": "history",
                    }
                )

        relation_by_role = {
            "query_spec": "describes",
            "query_meta": "describes",
            "run_record": "evidence_for",
            "run_meta": "describes_evidence_for",
            "result_evidence": "result_for",
            "validation_sql": "validates",
            "validation_spec": "describes_validation_for",
            "validation_meta": "describes_validation_for",
            "dashboard_sql": "derived_from",
            "dashboard_spec": "describes_dashboard_for",
            "dashboard_meta": "describes_dashboard_for",
        }
        for member in new_members:
            role = str(member.get("role") or "")
            relation = relation_by_role.get(role)
            if relation:
                lineage.append(
                    {
                        "relation": relation,
                        "from_member_id": str(member["member_id"]),
                        "to_member_id": query_member_id,
                    }
                )
        if not query_reused and existing_query_member:
            lineage.append(
                {
                    "relation": "supersedes",
                    "from_member_id": query_member_id,
                    "to_member_id": str(existing_query_member.get("member_id") or ""),
                }
            )

        try:
            repository_plan = plan_formal_asset_package(
                root,
                title=title,
                members=[*state_updates, *new_members],
                package_id=str((package_resolution or {}).get("package_id") or "") or None,
                slug=slug if not package_resolution else None,
                lineage=lineage,
                lifecycle_state="current",
            )
            repository_receipt = apply_formal_asset_plan(repository_plan)
            receipt_validation = validate_formal_asset_receipt(root, repository_receipt)
        except FormalAssetRepositoryError as exc:
            raise SystemExit("Formal Asset Repository rejected the formalization transaction.") from exc
        if receipt_validation.get("status") != "valid":
            raise SystemExit(
                "Formal Asset Repository returned an invalid Package receipt: "
                + "; ".join(str(item) for item in receipt_validation.get("problems", []))
            )
        mark(
            "apply_formal_asset_repository",
            detail=f"{repository_receipt.get('package_id')}@{repository_receipt.get('receipt_id')}",
        )

        query_path = _repository_path_for_member(repository_plan, query_member_id)
        run_path = _repository_path_for_member(repository_plan, run_member_id)
        validation_path = (
            _repository_path_for_member(repository_plan, validation_member_id)
            if validation_member_id
            else ""
        )
        dashboard_path = (
            _repository_path_for_member(repository_plan, dashboard_member_id)
            if dashboard_member_id
            else ""
        )

    if workspace_reference:
        promoted_workspace = mark_workspace_query_promoted(root, workspace_reference, query_path)
        plan["query_workspace_origin"] = {
            **public_query_workspace_origin(workspace_reference),
            "promoted_status": promoted_workspace.get("query_status", "promoted"),
            "formal_artifact_path": query_path,
            "formal_asset_package_id": repository_receipt.get("package_id", ""),
        }
        mark(
            "link_query_workspace_origin",
            detail=f"{workspace_reference.get('path')} -> {query_path}",
        )
    else:
        mark("link_query_workspace_origin", "skipped", "no Workspace origin")

    mark("write_legacy_manifest", "skipped", "Formal Asset Repository is the only formal writer")
    mark("write_semantic_summary_cache", "skipped", "formalization does not write read models")
    mark("refresh_viewers", "skipped", "shared read models refresh from Package facts explicitly")

    actual_query_reuse = {
        "status": "reused" if query_reused else "new_query",
        "reason": "formal_asset_package_member_reused"
        if query_reused
        else "formal_asset_package_member_created",
        "path": query_path,
        "package_id": repository_receipt.get("package_id", ""),
    }
    plan["query_reuse"] = actual_query_reuse
    plan["execution_steps"] = execution_steps
    plan["execution_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    plan["tool_messages"] = []
    plan["status"] = "saved"
    plan["saved_outputs"] = {
        "formal_asset_package_id": repository_receipt.get("package_id", ""),
        "package_manifest": repository_receipt.get("package_manifest_path", ""),
        "query_sql": query_path,
        "query_sql_reuse_status": actual_query_reuse["status"],
        "run_evidence": run_path,
        "validation_sql": validation_path,
        "dashboard_sql": dashboard_path,
        "viewer_refresh_mode": "explicit_shared_projection",
        "query_workspace_origin": plan.get("query_workspace_origin") or {},
    }
    plan["delivery_receipt"] = repository_receipt
    write_result = copy.deepcopy(
        plan.get("artifact_write_plan") or artifact_write_plan(actual_query_reuse, args.target)
    )
    write_items = (
        write_result.get("items", []) if isinstance(write_result.get("items"), list) else []
    )
    for item in write_items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "QUERY":
            item["action"] = "reuse" if query_reused else "save"
            item["path"] = query_path
        elif kind == "RUN_EVIDENCE":
            item["path"] = run_path
        elif kind == "VALIDATION":
            item["path"] = validation_path
        elif kind == "DASHBOARD":
            item["path"] = dashboard_path
    counts = {"save": 0, "reuse": 0, "skip": 0}
    for item in write_items:
        action = str(item.get("action") or "") if isinstance(item, dict) else ""
        if action in counts:
            counts[action] += 1
    write_result["counts"] = counts
    write_result["repository_receipt_id"] = repository_receipt.get("receipt_id", "")
    write_result["formal_asset_package_id"] = repository_receipt.get("package_id", "")
    plan["artifact_write_result"] = write_result
    plan.pop("objects", None)
    return plan

def timing_step_category(step: str) -> str:
    name = str(step or "").lower()
    if any(token in name for token in ["inspect", "preflight_inputs", "load_fact", "normalize", "output_field_contract"]):
        return "inputs_and_normalization"
    if any(token in name for token in ["analyze", "rule_context", "repository_summary", "performance_preflight"]):
        return "analysis_and_gates"
    if any(token in name for token in ["query_spec", "validation", "dashboard_candidate", "dashboard_contract"]):
        return "spec_and_contracts"
    if any(token in name for token in ["save_", "manifest", "rebuild_index", "run_evidence"]):
        return "save_transaction"
    if "viewer" in name or "refresh" in name:
        return "viewer_refresh"
    return "other"


def add_timing_total(target: dict[str, dict[str, Any]], key: str, item: dict[str, Any]) -> None:
    row = target.setdefault(key, {"duration_ms": 0, "step_count": 0, "blocked_count": 0, "warn_count": 0, "skipped_count": 0})
    row["duration_ms"] += int(item.get("duration_ms") or 0)
    row["step_count"] += 1
    status = str(item.get("status") or "")
    if status == "blocked":
        row["blocked_count"] += 1
    elif status == "warn":
        row["warn_count"] += 1
    elif status == "skipped":
        row["skipped_count"] += 1


def attach_timing_summary(output: dict[str, Any]) -> dict[str, Any]:
    """Add a compact timing summary so speed work can target real slow stages."""
    timed_steps: list[dict[str, Any]] = []
    for phase in ["steps", "execution_steps"]:
        for item in output.get(phase, []) or []:
            if not isinstance(item, dict):
                continue
            step_name = str(item.get("step", ""))
            timed_steps.append(
                {
                    "phase": "plan" if phase == "steps" else "execute",
                    "step": step_name,
                    "category": timing_step_category(step_name),
                    "status": item.get("status", ""),
                    "duration_ms": int(item.get("duration_ms") or 0),
                    "elapsed_ms": int(item.get("elapsed_ms") or 0),
                }
            )
    if not timed_steps:
        return output
    phase_totals: dict[str, dict[str, Any]] = {}
    category_totals: dict[str, dict[str, Any]] = {}
    for item in timed_steps:
        add_timing_total(phase_totals, str(item["phase"]), item)
        add_timing_total(category_totals, str(item["category"]), item)
    plan_elapsed = int(output.get("plan_elapsed_ms") or 0)
    execution_elapsed = int(output.get("execution_elapsed_ms") or 0)
    output["timing_summary"] = {
        "plan_elapsed_ms": output.get("plan_elapsed_ms"),
        "execution_elapsed_ms": output.get("execution_elapsed_ms"),
        "total_elapsed_ms": plan_elapsed + execution_elapsed,
        "phase_totals": phase_totals,
        "category_totals": dict(sorted(category_totals.items(), key=lambda pair: pair[1]["duration_ms"], reverse=True)),
        "slowest_steps": sorted(timed_steps, key=lambda item: item["duration_ms"], reverse=True)[:5],
        "slowest_plan_steps": sorted([item for item in timed_steps if item["phase"] == "plan"], key=lambda item: item["duration_ms"], reverse=True)[:3],
        "slowest_execute_steps": sorted([item for item in timed_steps if item["phase"] == "execute"], key=lambda item: item["duration_ms"], reverse=True)[:3],
    }
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Project root, such as sql-projects/DEMO_ANALYTICS")
    parser.add_argument("--source-sql", required=True, help="Already-run source SQL file")
    parser.add_argument(
        "--dashboard-sql-file",
        help=(
            "Optional independently prepared Dashboard SQL. Use when delivery changes "
            "an execution-only source or other DA adapter while preserving the QUERY contract."
        ),
    )
    parser.add_argument("--result-file", help="User-provided .csv/.xlsx result evidence")
    parser.add_argument("--target", choices=["query", "query-dashboard"], default="query")
    parser.add_argument("--title", help="Stable formal asset title. Defaults to source SQL filename.")
    parser.add_argument("--slug", help="Stable artifact slug. Defaults to slugified title.")
    parser.add_argument("--verification-status", choices=["passed", "proxy_verified", "skipped"], default="passed")
    parser.add_argument("--user-confirmed", action="store_true", help="User confirmed the result evidence is acceptable.")
    parser.add_argument("--definition-project")
    parser.add_argument("--execution-project")
    parser.add_argument("--delivery-project")
    parser.add_argument("--concept-keys", default="")
    parser.add_argument("--proxy-limitations", default="")
    parser.add_argument("--future-verification-plan", default="")
    parser.add_argument("--skip-reason", default="")
    parser.add_argument("--risk-note", default="")
    parser.add_argument("--confirmed-by", default="user")
    parser.add_argument("--semantic-mode", choices=["auto", "deterministic"], default="auto")
    parser.add_argument("--retained-fields", default="", help="Comma/newline/JSON-list fields to retain from the result file, in final output order.")
    parser.add_argument("--retained-fields-file", help="File containing retained result fields as newline/comma separated text or a JSON array.")
    parser.add_argument("--use-fact-bundle", choices=["auto", "required", "off"], default="auto", help="Reuse a formalize seed/fact bundle when present; required blocks when no seed exists.")
    parser.add_argument("--fact-bundle", help="Explicit formalize seed/fact bundle JSON produced with the temporary SQL.")
    parser.add_argument(
        "--knowledge-reference-file",
        action="append",
        default=[],
        help="Project-local resolver receipt emitted by config_knowledge.py resolve.",
    )
    parser.add_argument(
        "--knowledge-usage",
        choices=["auto", "not-used"],
        default="auto",
        help="Declare that active project knowledge was intentionally not used when no resolver receipt applies.",
    )
    parser.add_argument("--refresh-viewers", choices=["incremental", "deferred", "full", "dynamic"], default="incremental", help="Control repository/dashboard viewer refresh after saving. Dynamic skips static rebuilds and returns a live viewer command.")
    parser.add_argument("--sample-rows", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(parser, selection_help="Optional route such as 【SQL固化】, [SQL_FORMALIZE], [QUERY], or [DASHBOARD].")
    return parser


def render_text(plan: dict[str, Any]) -> str:
    lines = [f"status: {plan.get('status')}", f"title: {plan.get('title')}", f"slug: {plan.get('slug')}"]
    if plan.get("blockers"):
        lines.append("blockers:")
        lines.extend(f"  - {item}" for item in plan["blockers"])
    if plan.get("warnings"):
        lines.append("warnings:")
        lines.extend(f"  - {item}" for item in plan["warnings"])
    outputs = plan.get("saved_outputs") or plan.get("planned_outputs") or {}
    if outputs:
        lines.append("outputs:")
        lines.extend(f"  - {key}: {value}" for key, value in outputs.items() if value)
    receipt = plan.get("delivery_receipt") if isinstance(plan.get("delivery_receipt"), dict) else {}
    if receipt:
        lines.append(f"delivery_receipt: {receipt.get('status')}")
        for item in receipt.get("files", []):
            if not isinstance(item, dict):
                continue
            label = item.get("role") or item.get("kind") or "file"
            path = item.get("path") or item.get("absolute_path") or item.get("project_relative_path")
            if path:
                lines.append(f"  - {label}: {path}")
        if receipt.get("final_response_requirement"):
            lines.append(f"final_response_requirement: {receipt.get('final_response_requirement')}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("sql_formalize.py"),
            purpose="SQL formalization fast path",
        )
        require_user_request(args.user_request, purpose="SQL formalization fast path")
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    try:
        with project_staging_directory(Path(args.root), "sql_formalize_") as tmp:
            plan = build_plan(args, Path(tmp))
            if args.dry_run or plan.get("status") != "ready":
                output = plan
            else:
                output = execute_plan(args, plan)
    except SystemExit as exc:
        message = str(exc) or f"internal tool exited with code {exc.code}"
        output = {"status": "error", "blockers": [message]}
    except Exception as exc:  # noqa: BLE001
        output = {"status": "error", "blockers": [str(exc)]}
    if args.format == "json":
        safe = attach_timing_summary({key: value for key, value in output.items() if key != "objects"})
        print(json.dumps(safe, ensure_ascii=False, indent=2))
    else:
        print(render_text(output), end="")
    return 3 if output.get("status") == "error" else 1 if output.get("status") == "blocked" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "blockers": [str(exc)]}, ensure_ascii=False, indent=2))
        raise SystemExit(3)
