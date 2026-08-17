#!/usr/bin/env python3
"""Persist and index every temporary, diagnostic, or promoted query SQL."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from asset_provenance import build_generation_provenance, stamp_sql_generation  # noqa: E402
from capability_registry import command_function_ids  # noqa: E402
from config_knowledge import validate_knowledge_reference  # noqa: E402
from knowledge_usage import (  # noqa: E402
    build_knowledge_usage,
    load_reference_files as load_knowledge_reference_files,
)
from project_rules import rules_fingerprint  # noqa: E402
from rule_application import (  # noqa: E402
    application_integrity_ok,
    build_inheritance_contract,
    build_request_envelope,
    build_rule_application,
    legacy_unlabeled_application,
)
from sql_facts import (  # noqa: E402
    build_sql_fact_bundle,
    execution_fingerprint,
    logic_fingerprint as sql_logic_fingerprint,
    normalize_sql_text as normalize_fact_sql_text,
    sql_side_privacy_transforms,
)
from sql_execution_adapter import (  # noqa: E402
    effective_config_for_context,
    execution_route_for_file,
    execution_route_for_sql,
    route_matches_context,
)
from performance_preflight import starrocks_cte_expansion_assessment  # noqa: E402
from sql_summary_planner import (  # noqa: E402
    SUMMARY_PLAN_VERSION,
    summary_plan_fingerprint,
    validate_summary_plan,
)
from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from temporary_rule_override import (  # noqa: E402
    acknowledge_temporary_rule_override,
    request_declares_temporary_sql,
)
from result_evidence_retention import (  # noqa: E402
    full_reusable_output_retention,
    prepare_result_evidence,
)
from workbook_manifest import build_workbook_manifest, is_reusable_workbook  # noqa: E402
from sql_result_inspector import (  # noqa: E402
    RESULT_EXTENSIONS as INSPECTABLE_RESULT_EXTENSIONS,
    inspect_result_file,
    time_coverage_problem_messages,
    unobservable_time_coverage,
)
from sql_time_contract import time_integrity_plan  # noqa: E402


WORKSPACE_REL = Path("query_workspace")
INDEX_REL = WORKSPACE_REL / "index.json"
INDEX_MD_REL = WORKSPACE_REL / "index.md"
INDEX_HTML_REL = WORKSPACE_REL / "index.html"
INDEX_SCHEMA_VERSION = "query_workspace_index_v2"
LEGACY_INDEX_SCHEMA_VERSION = "query_workspace_index_v1"
META_SCHEMA_VERSION = "query_workspace_meta_v1"
SEED_SCHEMA_VERSION = "formalize_seed_v2"
DELIVERY_RECEIPT_SCHEMA_VERSION = "query_delivery_receipt_v1"
QUERY_STATUSES = {
    "draft",
    "runnable",
    "run_failed",
    "result_confirmed",
    "discarded",
    "archived",
    "superseded",
    "promoted",
}
DELIVERY_READY_STATUSES = {"runnable", "result_confirmed", "promoted"}
SAVE_STATUSES = {"draft", "runnable", "result_confirmed", "archived"}
STATUS_TRANSITIONS = {
    "draft": {"runnable", "result_confirmed", "discarded"},
    "runnable": {"run_failed", "result_confirmed", "discarded", "promoted"},
    "run_failed": {"result_confirmed", "discarded"},
    "result_confirmed": {"discarded", "superseded", "promoted"},
    "discarded": {"result_confirmed", "superseded"},
    "archived": {"result_confirmed", "discarded", "superseded"},
    "superseded": {"result_confirmed"},
    "promoted": {"superseded"},
}
WORKSPACE_ROLES = {"query", "dashboard_delivery", "unknown"}
QUERY_CHANGE_TYPES = {"new", "correction", "replacement", "superset", "parameter_refresh", "branch", "migration"}
COVERAGE_RELATIONS = {"same_contract", "strict_superset", "partial_overlap", "different_contract", "independent", "unknown"}
USAGE_CLASSES = {
    "personal_diagnosis",
    "reusable_diagnostic",
    "ad_hoc_analysis",
    "reusable_analysis",
    "recurring_delivery",
    "unclassified",
}
CHANGE_COVERAGE_MATRIX = {
    "new": {"independent"},
    "correction": {"same_contract"},
    "replacement": {"same_contract"},
    "superset": {"strict_superset"},
    "parameter_refresh": {"same_contract"},
    "branch": {"partial_overlap", "different_contract", "independent"},
    "migration": {"unknown"},
}
DERIVED_OUTPUT_KINDS = {
    "result_evidence",
    "analysis_workbook",
    "comparison_workbook",
    "visualization",
    "export",
    "other",
}
DERIVED_OUTPUT_SOURCE_KINDS = {"user_result", "skill_generated"}
DERIVED_OUTPUT_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".xlsx",
    ".xls",
    ".json",
    ".html",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".md",
}
GENERIC_PURPOSES = {
    "sql",
    "query",
    "查询",
    "查询sql",
    "临时sql",
    "临时查询",
    "数据查询",
    "待确认",
    "测试sql",
}
PATH_KEYS = {
    "path",
    "meta_path",
    "formalize_seed_path",
    "previous_version_path",
    "next_version_path",
    "formal_artifact_path",
    "source_sql_file",
    "source_snapshot_path",
    "working_copy_path",
    "source_project_path",
    "legacy_source_path",
    "query_workspace_index",
    "query_workspace_view",
}

INDEX_ENTRY_FIELDS = (
    "query_id",
    "title",
    "purpose",
    "business_question",
    "status",
    "current_version",
    "current_path",
    "sql_fingerprint",
    "logic_fingerprint",
    "usage_class",
    "created_at",
    "updated_at",
    "formal_artifacts",
    "change_type",
    "coverage_relation",
    "branch_of",
    "derived_output_count",
    "business_category",
    "analysis_type",
    "source_logs",
    "tables",
    "metrics",
    "dimensions",
    "filters",
    "grain",
    "time_grain",
    "tags",
)
INDEX_VERSION_FIELDS = (
    "version",
    "path",
    "meta_path",
    "formalize_seed_path",
    "sql_fingerprint",
    "logic_fingerprint",
    "previous_version_path",
    "next_version_path",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def normalize_sql_text(sql: str) -> str:
    return normalize_fact_sql_text(sql)


def sql_fingerprint(sql: str) -> str:
    return execution_fingerprint(sql)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_fingerprint(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def project_relative(root: Path, path: Path) -> str:
    root = root.resolve()
    path = path.resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Path must stay inside project root: {path}") from exc


def is_project_local(root: Path, path: Path) -> bool:
    try:
        project_relative(root, path)
        return True
    except ValueError:
        return False


def external_source_intake(source_sql: Path) -> dict[str, Any]:
    source_sql = source_sql.resolve()
    source_text = normalize_sql_text(source_sql.read_text(encoding="utf-8-sig"))
    return {
        "contract_version": "external_sql_intake_v1",
        "source_kind": "external_import",
        "original_file_name": source_sql.name,
        "source_sha256": file_sha256(source_sql),
        "source_sql_fingerprint": sql_fingerprint(source_text),
        "external_input_immutable": True,
        "absolute_source_path_persisted": False,
    }


def resolve_project_path(root: Path, value: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Project-relative path is required.")
    candidate = Path(text)
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
        raise ValueError(f"Absolute paths are forbidden in query workspace state: {text}")
    resolved = (root / candidate).resolve()
    project_relative(root, resolved)
    return resolved


def slugify(value: str, fallback: str = "query") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:48] or fallback


def normalize_query_id(value: str) -> str:
    query_id = str(value or "").strip().lower()
    if not re.fullmatch(r"qw-[a-z0-9-]{8,120}", query_id):
        raise ValueError(f"Invalid query workspace id: {value}")
    return query_id


def validate_title_and_purpose(title: str, purpose: str) -> tuple[str, str]:
    clean_title = str(title or "").strip()
    clean_purpose = re.sub(r"\s+", " ", str(purpose or "").strip())
    if len(clean_title) < 2:
        raise ValueError("Query workspace title must identify the query.")
    normalized_purpose = re.sub(r"[\s_\-：:。.]", "", clean_purpose).lower()
    if len(clean_purpose) < 6 or normalized_purpose in GENERIC_PURPOSES:
        raise ValueError("--purpose must quickly explain what the SQL calculates; generic labels such as `临时查询` are not accepted.")
    return clean_title, clean_purpose


def resolve_change_contract(
    *,
    query_id: str,
    source_kind: str,
    change_type: str,
    coverage_relation: str,
    branch_of: str,
    revision_note: str,
) -> tuple[str, str, str]:
    """Resolve one query-family decision before fingerprint deduplication."""

    requested_query_id = normalize_query_id(query_id) if str(query_id or "").strip() else ""
    requested_change_type = str(change_type or "auto").strip().lower()
    if requested_change_type == "auto":
        if source_kind in {"historical_formal_migration", "legacy_work_migration"}:
            requested_change_type = "migration"
        elif branch_of:
            requested_change_type = "branch"
        elif requested_query_id:
            requested_change_type = "replacement"
        else:
            requested_change_type = "new"
    if requested_change_type not in QUERY_CHANGE_TYPES:
        raise ValueError(f"Unsupported query change type: {requested_change_type}")
    if requested_change_type == "new" and requested_query_id:
        raise ValueError("change_type=new creates a new query family; omit --query-id.")
    if requested_change_type == "branch":
        if requested_query_id:
            raise ValueError("A branch creates a new query family; do not combine --branch-of with --query-id.")
        if not branch_of:
            raise ValueError("Query change_type=branch requires --branch-of.")
    elif branch_of:
        raise ValueError("--branch-of is valid only when change_type=branch.")
    if requested_change_type in {"correction", "replacement", "superset", "parameter_refresh"} and not requested_query_id:
        raise ValueError(f"Query change_type={requested_change_type} requires an existing --query-id.")
    if requested_change_type not in {"new", "migration"} and len(str(revision_note or "").strip()) < 6:
        raise ValueError(
            "A correction, replacement, superset, parameter refresh, or branch requires --revision-note explaining the change."
        )

    default_coverage = {
        "correction": "same_contract",
        "replacement": "same_contract",
        "superset": "strict_superset",
        "parameter_refresh": "same_contract",
        "branch": "different_contract",
        "new": "independent",
        "migration": "unknown",
    }[requested_change_type]
    resolved_coverage = str(coverage_relation or default_coverage).strip().lower()
    if resolved_coverage not in COVERAGE_RELATIONS:
        raise ValueError(f"Unsupported query coverage relation: {resolved_coverage}")
    allowed_coverage = CHANGE_COVERAGE_MATRIX[requested_change_type]
    if resolved_coverage not in allowed_coverage:
        raise ValueError(
            f"change_type={requested_change_type} cannot use coverage_relation={resolved_coverage}; "
            f"expected one of {sorted(allowed_coverage)}."
        )
    return requested_query_id, requested_change_type, resolved_coverage


def _write_transaction(files: dict[Path, str | bytes]) -> None:
    """Application-level all-or-rollback write for SQL, metadata, and both indexes."""

    snapshots: dict[Path, bytes | None] = {}
    temp_paths: dict[Path, Path] = {}
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            snapshots[path] = path.read_bytes() if path.exists() else None
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            if isinstance(content, bytes):
                temp.write_bytes(content)
            else:
                temp.write_text(content, encoding="utf-8")
            temp_paths[path] = temp
        for path, temp in temp_paths.items():
            os.replace(temp, path)
    except Exception:
        for temp in temp_paths.values():
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        for path, content in snapshots.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    restore = path.with_name(f".{path.name}.{uuid.uuid4().hex}.restore")
                    try:
                        restore.write_bytes(content)
                        os.replace(restore, path)
                    finally:
                        restore.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def default_index(root: Path) -> dict[str, Any]:
    project_config = read_json(root / "project_config.json", {})
    manifest = read_json(root / "manifest.json", {})
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "project_id": str(project_config.get("project_id") or root.name),
        "project_name": str(manifest.get("project_name") or project_config.get("display_name") or root.name),
        "updated_at": now_iso(),
        "entries": [],
    }


def _hydrate_index(root: Path, index: dict[str, Any]) -> dict[str, Any]:
    """Hydrate pointer-index versions from their immutable meta sidecars."""

    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        versions = entry.get("versions")
        if not isinstance(versions, list):
            continue
        for version in versions:
            if not isinstance(version, dict):
                continue
            meta_path = str(version.get("meta_path") or "")
            if not meta_path:
                continue
            meta = read_json(resolve_project_path(root, meta_path), {})
            if not isinstance(meta, dict):
                continue
            pointer = {key: copy.deepcopy(version[key]) for key in INDEX_VERSION_FIELDS if key in version}
            version.clear()
            version.update(meta)
            version.update(pointer)
        current_version = int(entry.get("current_version") or 0)
        current = next(
            (
                item
                for item in versions
                if isinstance(item, dict) and int(item.get("version") or 0) == current_version
            ),
            None,
        )
        if isinstance(current, dict):
            for key, value in current.items():
                if key not in {"query_id", "formal_artifacts"}:
                    entry[key] = copy.deepcopy(value)
            derived_outputs = current.get("derived_outputs")
            if isinstance(derived_outputs, list):
                entry["derived_output_count"] = len(derived_outputs)
    return index


def _compact_index(index: dict[str, Any]) -> dict[str, Any]:
    """Keep searchable entry fields and version pointers; meta owns full facts."""

    compact = {
        key: copy.deepcopy(value)
        for key, value in index.items()
        if key not in {"entries", "schema_version"}
    }
    compact["schema_version"] = INDEX_SCHEMA_VERSION
    compact["entries"] = []
    for raw_entry in index.get("entries", []):
        if not isinstance(raw_entry, dict):
            continue
        entry = {
            key: copy.deepcopy(raw_entry[key])
            for key in INDEX_ENTRY_FIELDS
            if key in raw_entry
        }
        entry["versions"] = []
        for raw_version in raw_entry.get("versions", []):
            if not isinstance(raw_version, dict):
                continue
            entry["versions"].append(
                {
                    key: copy.deepcopy(raw_version[key])
                    for key in INDEX_VERSION_FIELDS
                    if key in raw_version
                }
            )
        compact["entries"].append(entry)
    return compact


def load_index(root: Path) -> dict[str, Any]:
    index = read_json(root / INDEX_REL, default_index(root))
    if not isinstance(index, dict):
        raise ValueError(f"Query workspace index must be a JSON object: {INDEX_REL.as_posix()}")
    if index.get("schema_version") not in {INDEX_SCHEMA_VERSION, LEGACY_INDEX_SCHEMA_VERSION}:
        raise ValueError(
            f"Unsupported query workspace index schema: {index.get('schema_version') or 'missing'}"
        )
    if not isinstance(index.get("entries"), list):
        raise ValueError("Query workspace index `entries` must be an array.")
    if index.get("schema_version") == INDEX_SCHEMA_VERSION:
        return _hydrate_index(root, index)
    return index


def render_index_markdown(index: dict[str, Any]) -> str:
    entries = [item for item in index.get("entries", []) if isinstance(item, dict)]
    counts = {status: 0 for status in sorted(QUERY_STATUSES)}
    for item in entries:
        status = str(item.get("status") or "draft")
        counts[status] = counts.get(status, 0) + 1
    lines = [
        f"# {index.get('project_name') or index.get('project_id') or 'SQL Project'} SQL 工作台",
        "",
        "临时查询、诊断 SQL、废弃实验和正式查询的来源版本统一保存在这里；共享正式资产位于 `formal_assets/`，本地工作台不自动同步。",
        "",
        f"Updated: {index.get('updated_at') or now_iso()}",
        "",
        "## Status",
        "",
        "- " + ", ".join(f"`{key}`={value}" for key, value in counts.items()),
        "",
        "## SQL",
        "",
        "| Work ID | Status | Title | What it does | Current SQL |",
        "|---|---|---|---|---|",
    ]
    if not entries:
        lines.append("| - | - | No query workspace entries yet | - | - |")
    else:
        for item in sorted(entries, key=lambda row: str(row.get("updated_at") or ""), reverse=True):
            title = str(item.get("title") or "").replace("|", "\\|")
            purpose = str(item.get("purpose") or "").replace("|", "\\|")
            path = str(item.get("current_path") or "")
            lines.append(
                f"| `{item.get('query_id', '')}` | `{item.get('status', '')}` | {title} | {purpose} | `{path}` |"
            )
    lines.extend(
        [
            "",
            "## Search",
            "",
            f"- Viewer shell: `{INDEX_HTML_REL.as_posix()}`",
            "- Live viewer: `python .\\sql-engineering\\scripts\\query_workspace_maintenance.py serve --root <project-root>`",
            "",
            "```powershell",
            "python .\\sql-engineering\\scripts\\sql_query_workspace.py search --root <project-root> --query \"<metric/log/filter/purpose>\"",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _index_files(
    root: Path,
    index: dict[str, Any],
    *,
    sql_overrides: dict[str, str] | None = None,
) -> dict[Path, str]:
    from query_workspace_viewer import VIEWER_SHELL_VERSION, render_workspace_html  # noqa: PLC0415

    del sql_overrides
    index["updated_at"] = now_iso()
    stored_index = _compact_index(index)
    files = {
        root / INDEX_REL: json_text(stored_index),
        root / INDEX_MD_REL: render_index_markdown(index),
    }
    html_path = root / INDEX_HTML_REL
    current_shell = ""
    if html_path.exists():
        try:
            current_shell = html_path.read_text(encoding="utf-8-sig")
        except OSError:
            current_shell = ""
    if VIEWER_SHELL_VERSION not in current_shell:
        files[html_path] = render_workspace_html(root, index)
    return files


def ensure_workspace(root: Path, *, update_manifest: bool = True) -> dict[str, Any]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / INDEX_REL
    index = load_index(root) if index_path.exists() else default_index(root)
    files = _index_files(root, index)
    if update_manifest and (root / "manifest.json").exists():
        manifest = read_json(root / "manifest.json", {})
        manifest_changed = False
        if manifest.get("query_workspace_index") != INDEX_REL.as_posix():
            manifest["query_workspace_index"] = INDEX_REL.as_posix()
            manifest_changed = True
        if manifest.get("query_workspace_view") != INDEX_HTML_REL.as_posix():
            manifest["query_workspace_view"] = INDEX_HTML_REL.as_posix()
            manifest_changed = True
        if manifest_changed:
            manifest["updated_at"] = now_iso()
            files[root / "manifest.json"] = json_text(manifest)
    _write_transaction(files)
    return {
        "status": "ready",
        "index_path": INDEX_REL.as_posix(),
        "index_markdown_path": INDEX_MD_REL.as_posix(),
        "index_html_path": INDEX_HTML_REL.as_posix(),
    }


def _legacy_version_change_contract(version: dict[str, Any], position: int) -> tuple[str, str, str]:
    intake = version.get("source_intake") if isinstance(version.get("source_intake"), dict) else {}
    migrated = str(intake.get("contract_version") or "") in {
        "historical_formal_query_backfill_v1",
        "legacy_work_import_v1",
    }
    if position == 0:
        if migrated:
            return (
                "migration",
                "unknown",
                "Historical SQL was indexed without reconstructing its earlier semantic change history.",
            )
        return "new", "independent", "Initial indexed query version."
    return (
        "replacement",
        "same_contract",
        "Historical later version replaced the prior current version in the same query family.",
    )


def upgrade_change_contract(root: Path) -> dict[str, Any]:
    """Backfill the query-family contract without changing any saved SQL body."""

    root = root.resolve()
    ensure_workspace(root)
    index = load_index(root)
    files: dict[Path, str] = {}
    changed_versions = 0
    changed_entries = 0
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        entry_changed = False
        versions = [item for item in entry.get("versions", []) if isinstance(item, dict)]
        versions.sort(key=lambda item: int(item.get("version") or 0))
        for position, version in enumerate(versions):
            fallback_type, fallback_coverage, fallback_summary = _legacy_version_change_contract(version, position)
            before = (
                version.get("change_type"),
                version.get("coverage_relation"),
                version.get("change_summary"),
                version.get("branch_of"),
                version.get("derived_outputs"),
                version.get("rule_application"),
            )
            version.setdefault("change_type", fallback_type)
            version.setdefault("coverage_relation", fallback_coverage)
            version.setdefault("change_summary", fallback_summary)
            if not isinstance(version.get("branch_of"), dict):
                version["branch_of"] = {}
            if not isinstance(version.get("derived_outputs"), list):
                version["derived_outputs"] = []
            if not application_integrity_ok(version.get("rule_application")):
                version["rule_application"] = legacy_unlabeled_application(
                    note="Workspace version predates request-bound rule application capture."
                )
                version["request_envelope"] = copy.deepcopy(
                    version["rule_application"]["request_envelope"]
                )
            after = (
                version.get("change_type"),
                version.get("coverage_relation"),
                version.get("change_summary"),
                version.get("branch_of"),
                version.get("derived_outputs"),
                version.get("rule_application"),
            )
            if before == after:
                continue
            changed_versions += 1
            entry_changed = True
            meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
            meta = read_json(meta_path, {})
            meta.update(
                {
                    "change_type": version["change_type"],
                    "coverage_relation": version["coverage_relation"],
                    "change_summary": version["change_summary"],
                    "branch_of": copy.deepcopy(version["branch_of"]),
                    "derived_outputs": copy.deepcopy(version["derived_outputs"]),
                    "request_envelope": copy.deepcopy(version["request_envelope"]),
                    "rule_application": copy.deepcopy(version["rule_application"]),
                    "updated_at": now_iso(),
                }
            )
            files[meta_path] = json_text(meta)
            seed_relative = str(version.get("formalize_seed_path") or "")
            if seed_relative:
                seed_path = resolve_project_path(root, seed_relative)
                seed = read_json(seed_path, {})
                if isinstance(seed, dict):
                    seed["request_envelope"] = copy.deepcopy(version["request_envelope"])
                    seed["rule_application"] = copy.deepcopy(version["rule_application"])
                    files[seed_path] = json_text(seed)
        current_version = int(entry.get("current_version") or 0)
        current = next((item for item in versions if int(item.get("version") or 0) == current_version), None)
        if current:
            existing_family_branch = entry.get("branch_of") if isinstance(entry.get("branch_of"), dict) else {}
            family_branch = existing_family_branch or next(
                (
                    copy.deepcopy(item.get("branch_of"))
                    for item in versions
                    if isinstance(item.get("branch_of"), dict) and item.get("branch_of")
                ),
                {},
            )
            current_contract = (
                current.get("change_type"),
                current.get("coverage_relation"),
                family_branch,
            )
            entry_contract = (
                entry.get("change_type"),
                entry.get("coverage_relation"),
                entry.get("branch_of"),
            )
            if current_contract != entry_contract:
                entry.update(
                    {
                        "change_type": current.get("change_type"),
                        "coverage_relation": current.get("coverage_relation"),
                        "branch_of": copy.deepcopy(current.get("branch_of") or {}),
                        "request_envelope": copy.deepcopy(current.get("request_envelope") or {}),
                        "rule_application": copy.deepcopy(current.get("rule_application") or {}),
                    }
                )
                entry_changed = True
            if (
                entry.get("rule_application") != current.get("rule_application")
                or entry.get("request_envelope") != current.get("request_envelope")
            ):
                entry["request_envelope"] = copy.deepcopy(current.get("request_envelope") or {})
                entry["rule_application"] = copy.deepcopy(current.get("rule_application") or {})
                entry_changed = True
        if entry_changed:
            changed_entries += 1
        output_count = sum(len(item.get("derived_outputs", [])) for item in versions)
        if "derived_output_count" not in entry or int(entry.get("derived_output_count") or 0) != output_count:
            entry["derived_output_count"] = output_count
            if not entry_changed:
                changed_entries += 1
    if changed_entries:
        files.update(_index_files(root, index))
        _write_transaction(files)
    return {
        "status": "upgraded" if changed_entries else "unchanged",
        "query_family_count": len(index.get("entries", [])),
        "changed_entry_count": changed_entries,
        "changed_version_count": changed_versions,
        "index_path": INDEX_REL.as_posix(),
        "index_html_path": INDEX_HTML_REL.as_posix(),
    }


def _compact_gate(gate: dict[str, Any] | None, *, mode: str) -> dict[str, Any]:
    gate = gate if isinstance(gate, dict) else {}
    blockers = gate.get("blockers", []) if isinstance(gate.get("blockers"), list) else []
    warnings = gate.get("warnings", []) if isinstance(gate.get("warnings"), list) else []
    return {
        "status": str(gate.get("status") or "not_run"),
        "mode": mode,
        "checks": gate.get("checks") if isinstance(gate.get("checks"), dict) else {},
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
    }


def _public_rule_context(rule_context: dict[str, Any] | None) -> dict[str, Any]:
    rule_context = rule_context if isinstance(rule_context, dict) else {}
    keep = [
        "status",
        "mode",
        "lifecycle_stage",
        "request_envelope",
        "rule_application",
        "active_rules",
        "applied_rules",
        "inherited_rules",
        "excluded_rules",
        "hard_constraints",
        "inactive_stage_constraints",
        "candidate_sql_check",
        "project_contract_check",
        "project_time_contract",
        "generation_gate",
        "temporary_rule_override",
        "name_logic_mismatches",
    ]
    return {key: rule_context.get(key) for key in keep if key in rule_context}


def run_delivery_gate(
    root: Path,
    sql_file: Path,
    request: str,
    *,
    mode: str = "generation",
    lifecycle_stage: str = "temporary_query",
    parent_rule_application: dict[str, Any] | None = None,
    inheritance_contract: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from sql_formalize import ensure_rule_context_generation_gate, run_rule_context  # noqa: PLC0415
    from sql_project import read_project_config  # noqa: PLC0415

    sql = normalize_sql_text(sql_file.read_text(encoding="utf-8-sig"))
    config = read_project_config(root)
    rule_context = run_rule_context(
        root,
        sql_file,
        request,
        mode=mode,
        lifecycle_stage=lifecycle_stage,
        parent_rule_application=parent_rule_application,
        inheritance_contract=inheritance_contract,
    )
    gate = ensure_rule_context_generation_gate(rule_context, sql=sql, config=config)
    return gate, rule_context


def _stored_rule_application(root: Path, version: dict[str, Any]) -> dict[str, Any]:
    direct = version.get("rule_application")
    if application_integrity_ok(direct):
        return copy.deepcopy(direct)
    for key in ("meta_path", "formalize_seed_path"):
        relative = str(version.get(key) or "")
        if not relative:
            continue
        try:
            document = read_json(resolve_project_path(root, relative), {})
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        candidates = [document.get("rule_application")]
        context = document.get("rule_context")
        if isinstance(context, dict):
            candidates.append(context.get("rule_application"))
        for candidate in candidates:
            if application_integrity_ok(candidate):
                return copy.deepcopy(candidate)
    return {}


def revision_rule_inheritance(
    root: Path,
    *,
    query_id: str,
    change_type: str,
    coverage_relation: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if change_type not in {"correction", "parameter_refresh"} or coverage_relation != "same_contract":
        return {}, build_inheritance_contract()
    index = load_index(root)
    entry, version = _find_entry(index, query_id=query_id)
    if not entry:
        return {}, build_inheritance_contract()
    if version is None:
        current = int(entry.get("current_version") or 0)
        version = next(
            (
                item
                for item in entry.get("versions", []) or []
                if isinstance(item, dict) and int(item.get("version") or 0) == current
            ),
            None,
        )
    if not isinstance(version, dict):
        return {}, build_inheritance_contract()
    parent_application = _stored_rule_application(root, version)
    contract = build_inheritance_contract(
        "same_contract_revision",
        change_type=change_type,
        coverage_relation=coverage_relation,
        parent_asset={
            "query_id": str(entry.get("query_id") or ""),
            "version": int(version.get("version") or 0),
            "path": str(version.get("path") or ""),
        },
    )
    return parent_application, contract


def _replace_param_refs(condition: str, params: dict[str, str]) -> str:
    text = str(condition or "")
    pattern = re.compile(r"\(\s*select\s+([A-Za-z_][\w$]*)\s+from\s+params\s*\)", flags=re.I)

    def replacement(match: re.Match[str]) -> str:
        alias = match.group(1).lower()
        return params.get(alias, match.group(0))

    return pattern.sub(replacement, text)


def _query_facts(root: Path, sql_file: Path, sql: str, tags: list[str] | None = None) -> dict[str, Any]:
    del sql_file  # SQL text is the canonical input; paths are provenance only.
    bundle = build_sql_fact_bundle(sql, kind="QUERY", root=root)
    analysis = bundle["analysis"]
    metric_rows = bundle["metrics"]
    dimension_rows = bundle["dimensions"]
    metrics = [str(item.get("name") or item.get("field") or "") for item in metric_rows]
    dimensions = [str(item.get("label") or item.get("field") or "") for item in dimension_rows]
    params = bundle["params"]
    filters: list[str] = []
    for item in bundle["filters"]:
        condition = str(item.get("condition") or "")
        if condition and condition not in filters:
            filters.append(condition)
    if params.get("pt_start") or params.get("pt_end"):
        period = f"查询日期={params.get('pt_start', '?')} 至 {params.get('pt_end', '?')}"
        if period not in filters:
            filters.insert(0, period)
    for alias, label in [("zone_id", "区服参数"), ("game_mode", "模式参数"), ("game_mode_id", "模式参数")]:
        if params.get(alias):
            value = f"{label}={params[alias]}"
            if value not in filters:
                filters.append(value)
    merged_tags: list[str] = []
    for item in [*(analysis.get("tags", []) or []), *(tags or [])]:
        value = str(item or "").strip()
        if value and value not in merged_tags:
            merged_tags.append(value)
    return {
        "analysis": analysis,
        "sql_fact_bundle": bundle,
        "logic_fingerprint": bundle["logic_fingerprint"],
        "business_category": analysis.get("business_category", "uncategorized"),
        "analysis_type": analysis.get("analysis_type", "unspecified"),
        "tables": bundle.get("source_tables", []),
        "source_logs": bundle.get("source_logs", []),
        "metrics": [item for item in metrics if item],
        "dimensions": [item for item in dimensions if item],
        "filters": filters[:20],
        "params": params,
        "grain": analysis.get("grain", ""),
        "time_grain": analysis.get("time_grain", ""),
        "tags": merged_tags,
    }


def _new_query_id(title: str, fingerprint: str, index: dict[str, Any]) -> str:
    stem = slugify(title)
    base = f"qw-{now_day()}-{stem}-{fingerprint[:8]}"
    existing = {str(item.get("query_id") or "") for item in index.get("entries", []) if isinstance(item, dict)}
    if base not in existing:
        return base
    for number in range(2, 1000):
        candidate = f"{base}-{number}"
        if candidate not in existing:
            return candidate
    raise ValueError("Could not allocate a query workspace id.")


def _find_entry(index: dict[str, Any], *, query_id: str = "", sql_path: str = "", fingerprint: str = "") -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    normalized_path = str(sql_path or "").replace("\\", "/").lstrip("./")
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if query_id and entry.get("query_id") != query_id:
            continue
        for version in entry.get("versions", []) or []:
            if not isinstance(version, dict):
                continue
            if normalized_path and version.get("path") == normalized_path:
                return entry, version
            if fingerprint and version.get("sql_fingerprint") == fingerprint:
                return entry, version
        if query_id:
            return entry, None
    return None, None


def _entry_version(entry: dict[str, Any], version_number: int = 0) -> dict[str, Any] | None:
    target = version_number or int(entry.get("current_version") or 0)
    return next(
        (
            item
            for item in entry.get("versions", [])
            if isinstance(item, dict) and int(item.get("version") or 0) == target
        ),
        None,
    )


def _related_query_reference(index: dict[str, Any], value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(qw-[a-z0-9-]+)(?:@v?(\d+))?", text, flags=re.I)
    if not match:
        raise ValueError(f"Invalid related query reference `{value}`; use qw-... or qw-...@vNNN.")
    query_id = normalize_query_id(match.group(1).lower())
    entry, _ = _find_entry(index, query_id=query_id)
    if not entry:
        raise ValueError(f"Related query family is not indexed: {query_id}")
    version = _entry_version(entry, int(match.group(2) or 0))
    if not version:
        raise ValueError(f"Related query version is not indexed: {value}")
    return {
        "query_id": query_id,
        "version": int(version.get("version") or 0),
        "path": str(version.get("path") or ""),
        "sql_fingerprint": str(version.get("sql_fingerprint") or ""),
    }


def attach_derived_output(
    *,
    root: Path,
    file_path: Path,
    title: str,
    purpose: str,
    kind: str,
    source_kind: str,
    query_id: str = "",
    sql_path: str = "",
    version_number: int = 0,
    related_queries: list[str] | None = None,
    source_result_id: str = "",
    source_result_references: list[dict[str, Any]] | None = None,
    analysis_bundle_reference: dict[str, Any] | None = None,
    result_inspection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy a result/Excel/visualization into the exact query version that produced it."""

    root = root.resolve()
    file_path = file_path.resolve()
    title, purpose = validate_title_and_purpose(title, purpose)
    kind = str(kind or "").strip().lower()
    source_kind = str(source_kind or "").strip().lower()
    if kind not in DERIVED_OUTPUT_KINDS:
        raise ValueError(f"Unsupported derived output kind: {kind}")
    if source_kind not in DERIVED_OUTPUT_SOURCE_KINDS:
        raise ValueError(f"Unsupported derived output source kind: {source_kind}")
    if not file_path.is_file():
        raise ValueError(f"Derived output file not found: {file_path}")
    extension = file_path.suffix.lower()
    if extension not in DERIVED_OUTPUT_EXTENSIONS:
        raise ValueError(
            f"Unsupported derived output extension `{extension or '(none)'}`; "
            f"expected one of {sorted(DERIVED_OUTPUT_EXTENSIONS)}."
        )

    index = load_index(root)
    normalized_query_id = normalize_query_id(query_id) if str(query_id or "").strip() else ""
    entry, version = _find_entry(index, query_id=normalized_query_id, sql_path=sql_path)
    if not entry:
        raise ValueError("Attach outputs only to an indexed query family or SQL path.")
    if version_number:
        version = _entry_version(entry, version_number)
    elif version is None:
        version = _entry_version(entry)
    if not version:
        raise ValueError("The requested query version is not indexed.")

    result_time_coverage: dict[str, Any] = {}
    result_time_blockers: list[str] = []
    if kind == "result_evidence":
        sql_file = resolve_project_path(root, str(version.get("path") or ""))
        sql_text = sql_file.read_text(encoding="utf-8-sig")
        project_config = read_json(root / "project_config.json", {})
        effective_config, _ = effective_config_for_context(
            project_config,
            sql_text,
            version.get("execution_route"),
        )
        time_plan = time_integrity_plan(sql_text, effective_config)
        inspect_time_values = bool(
            time_plan.get("actual_range_required")
            or time_plan.get("actual_range_runtime_conditional")
        )
        if isinstance(result_inspection, dict) and result_inspection:
            result_time_coverage = copy.deepcopy(
                result_inspection.get("time_coverage") or {}
            )
        elif extension in INSPECTABLE_RESULT_EXTENSIONS and inspect_time_values:
            inspection = inspect_result_file(
                file_path,
                sql=sql_text,
                project_config=effective_config,
            )
            result_time_coverage = copy.deepcopy(inspection.get("time_coverage") or {})
        else:
            result_time_coverage = unobservable_time_coverage(
                sql=sql_text,
                project_config=effective_config,
                basis=(
                    "unsupported_result_format"
                    if inspect_time_values
                    else "not_required_for_fixed_historical_window"
                ),
            )
        result_time_blockers = time_coverage_problem_messages(result_time_coverage)

    if kind == "result_evidence":
        retained_result = prepare_result_evidence(file_path)
        retention = retained_result.retention
        source_hash = str(retention["source_sha256"])
        stored_hash = retained_result.stored_sha256
        stored_extension = retained_result.suffix
        stored_media_type = retained_result.media_type
        stored_payload = retained_result.payload
    else:
        retained_result = None
        retention = full_reusable_output_retention(file_path, kind)
        source_hash = str(retention["source_sha256"])
        stored_hash = str(retention["stored_sha256"])
        stored_extension = extension
        stored_media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        stored_payload = file_path.read_bytes()
    workbook_manifest = (
        build_workbook_manifest(file_path)
        if is_reusable_workbook(kind, stored_media_type, file_path.name)
        else {}
    )
    outputs = version.setdefault("derived_outputs", [])
    if not isinstance(outputs, list):
        raise ValueError("Query version derived_outputs must be an array.")
    source_result_id = str(source_result_id or "").strip()
    source_result_references = copy.deepcopy(source_result_references or [])
    analysis_bundle_reference = copy.deepcopy(analysis_bundle_reference or {})
    if analysis_bundle_reference:
        required_bundle_fields = {
            "contract_version",
            "bundle_id",
            "path",
            "metric_contract_fingerprint",
        }
        if analysis_bundle_reference.get("contract_version") != "query_analysis_bundle_output_ref_v1":
            raise ValueError("analysis_bundle_reference must use query_analysis_bundle_output_ref_v1.")
        if required_bundle_fields - set(analysis_bundle_reference):
            raise ValueError("analysis_bundle_reference is missing required fields.")
        if validate_no_absolute_paths(analysis_bundle_reference):
            raise ValueError("analysis_bundle_reference paths must stay project-relative.")
    source_result: dict[str, Any] | None = None
    resolved_source_results: list[dict[str, Any]] = []
    if kind == "result_evidence":
        if source_result_references:
            raise ValueError("Result evidence cannot depend on other result evidence rows.")
        source_result_id = ""
        lineage_status = "result_evidence"
    elif source_result_references:
        if source_result_id:
            raise ValueError("Use either source_result_id or source_result_references, not both.")
        seen_result_ids: set[tuple[str, int, str]] = set()
        for reference in source_result_references:
            if not isinstance(reference, dict):
                raise ValueError("source_result_references must contain JSON objects.")
            ref_query_id = normalize_query_id(str(reference.get("query_id") or ""))
            ref_version_number = int(reference.get("version") or 0)
            ref_result_id = str(reference.get("result_id") or "").strip()
            identity = (ref_query_id, ref_version_number, ref_result_id)
            if not ref_query_id or not ref_version_number or not ref_result_id or identity in seen_result_ids:
                raise ValueError("Each source result reference needs one unique query_id, version, and result_id.")
            seen_result_ids.add(identity)
            ref_entry, _ = _find_entry(index, query_id=ref_query_id)
            ref_version = _entry_version(ref_entry, ref_version_number) if ref_entry else None
            ref_output = next(
                (
                    item
                    for item in (ref_version or {}).get("derived_outputs", [])
                    if isinstance(item, dict)
                    and item.get("kind") == "result_evidence"
                    and item.get("attachment_id") == ref_result_id
                ),
                None,
            )
            if not ref_entry or not ref_version or not ref_output:
                raise ValueError(
                    f"Result reference `{ref_query_id}@v{ref_version_number}:{ref_result_id}` does not resolve."
                )
            resolved_source_results.append(
                {
                    "query_id": ref_query_id,
                    "version": ref_version_number,
                    "sql_path": str(ref_version.get("path") or ""),
                    "sql_fingerprint": str(ref_version.get("sql_fingerprint") or ""),
                    "result_id": ref_result_id,
                    "result_path": str(ref_output.get("path") or ""),
                    "result_sha256": str(ref_output.get("source_sha256") or ref_output.get("sha256") or ""),
                }
            )
        if len(resolved_source_results) < 2:
            raise ValueError("Multi-result binding requires at least two exact result references.")
        lineage_status = "exact_results"
    elif source_result_id:
        source_result = next(
            (
                item
                for item in outputs
                if isinstance(item, dict)
                and item.get("attachment_id") == source_result_id
                and item.get("kind") == "result_evidence"
            ),
            None,
        )
        if not source_result:
            raise ValueError(
                f"source_result_id `{source_result_id}` is not a result_evidence attachment on this exact query version."
            )
        lineage_status = "exact_result"
    else:
        lineage_status = "sql_version_only"

    existing = next(
        (
            item
            for item in outputs
            if isinstance(item, dict)
            and (item.get("source_sha256") or item.get("sha256")) == source_hash
        ),
        None,
    )
    if existing:
        if resolved_source_results and existing.get("source_results") != resolved_source_results:
            raise ValueError("The same reusable output bytes are already bound to a different result lineage.")
        if analysis_bundle_reference and existing.get("analysis_bundle") != analysis_bundle_reference:
            raise ValueError("The same reusable output bytes are already bound to a different analysis bundle.")
        if workbook_manifest and existing.get("workbook_manifest") != workbook_manifest:
            existing["workbook_manifest"] = workbook_manifest
            existing["preview_status"] = str(existing.get("preview_status") or "not_available")
        if kind == "result_evidence" and result_time_coverage:
            existing["result_time_coverage"] = result_time_coverage
            if result_time_blockers:
                existing["asset_state"] = "needs_review"
                existing["state_reason"] = result_time_blockers[0]
            elif any(
                marker in str(existing.get("state_reason") or "")
                for marker in ["查询包含今日", "查询范围包含或可能包含今日"]
            ):
                existing["asset_state"] = "active"
                existing.pop("state_reason", None)
        if (
            workbook_manifest
            or (kind == "result_evidence" and result_time_coverage)
        ):
            meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
            meta = read_json(meta_path, {})
            meta["derived_outputs"] = copy.deepcopy(outputs)
            meta["updated_at"] = now_iso()
            _write_transaction({meta_path: json_text(meta), **_index_files(root, index)})
        return {
            "status": "reused",
            "query_id": entry.get("query_id", ""),
            "version": version.get("version"),
            "attachment_id": existing.get("attachment_id", ""),
            "kind": existing.get("kind", ""),
            "path": existing.get("path", ""),
            "source_result_id": existing.get("source_result_id", ""),
            "lineage_status": existing.get("lineage_status", ""),
            "result_time_coverage": copy.deepcopy(
                existing.get("result_time_coverage") or {}
            ),
            "time_coverage_blockers": result_time_blockers,
            "index_path": INDEX_REL.as_posix(),
            "index_html_path": INDEX_HTML_REL.as_posix(),
        }

    related_refs = [_related_query_reference(index, value) for value in (related_queries or [])]
    for result_reference in resolved_source_results:
        related_ref = {
            "query_id": result_reference["query_id"],
            "version": result_reference["version"],
            "path": result_reference["sql_path"],
            "sql_fingerprint": result_reference["sql_fingerprint"],
        }
        if related_ref not in related_refs:
            related_refs.append(related_ref)
    primary_identity = (str(entry.get("query_id") or ""), int(version.get("version") or 0))
    related_refs = [
        item
        for item in related_refs
        if (str(item.get("query_id") or ""), int(item.get("version") or 0)) != primary_identity
    ]
    created_at = now_iso()
    attachment_id = f"qwo-{source_hash[:12]}"
    family_dir = Path(str(version.get("path") or "")).parent
    slice_suffix = "-slice" if retention.get("is_sliced") else ""
    file_name = f"{slugify(title, 'output')}-{source_hash[:8]}{slice_suffix}{stored_extension}"
    rel_output = (
        family_dir / "outputs" / f"v{int(version.get('version') or 0):03d}" / file_name
    ).as_posix()
    if kind == "result_evidence":
        source_result_id = attachment_id
        source_results = [
            {
                "query_id": str(entry.get("query_id") or ""),
                "version": int(version.get("version") or 0),
                "sql_path": str(version.get("path") or ""),
                "sql_fingerprint": str(version.get("sql_fingerprint") or ""),
                "result_id": attachment_id,
                "result_path": rel_output,
                "result_sha256": source_hash,
            }
        ]
    elif resolved_source_results:
        source_results = resolved_source_results
    elif source_result is not None:
        source_results = [
            {
                "query_id": str(entry.get("query_id") or ""),
                "version": int(version.get("version") or 0),
                "sql_path": str(version.get("path") or ""),
                "sql_fingerprint": str(version.get("sql_fingerprint") or ""),
                "result_id": str(source_result.get("attachment_id") or ""),
                "result_path": str(source_result.get("path") or ""),
                "result_sha256": str(source_result.get("source_sha256") or source_result.get("sha256") or ""),
            }
        ]
    else:
        source_results = []
    provenance = build_generation_provenance(
        generator_script="sql_query_workspace.py",
        workflow="query_workspace_attach_output",
        artifact_kind="QUERY_DERIVED_OUTPUT",
        generated_at=created_at,
        source=source_kind,
        extra={
            "query_id": entry.get("query_id", ""),
            "query_version": version.get("version"),
            "attachment_id": attachment_id,
        },
    )
    output_row = {
        "attachment_id": attachment_id,
        "kind": kind,
        "source_kind": source_kind,
        "title": title,
        "purpose": purpose,
        "path": rel_output,
        "original_file_name": file_path.name,
        "media_type": stored_media_type,
        "sha256": stored_hash,
        "source_sha256": source_hash,
        "retention": retention,
        "asset_state": "active",
        "source_result_id": source_result_id,
        "source_results": source_results,
        "lineage_status": lineage_status,
        "source_sql_fingerprint": str(version.get("sql_fingerprint") or ""),
        "related_queries": related_refs,
        "generation_provenance": provenance,
        "created_at": created_at,
    }
    if kind == "result_evidence":
        output_row["result_time_coverage"] = result_time_coverage
        if result_time_blockers:
            output_row["asset_state"] = "needs_review"
            output_row["state_reason"] = result_time_blockers[0]
    if analysis_bundle_reference:
        output_row["analysis_bundle"] = analysis_bundle_reference
    if workbook_manifest:
        output_row["workbook_manifest"] = workbook_manifest
        output_row["preview_status"] = "not_available"
    outputs.append(output_row)
    entry["derived_output_count"] = sum(
        len(item.get("derived_outputs", []))
        for item in entry.get("versions", [])
        if isinstance(item, dict) and isinstance(item.get("derived_outputs", []), list)
    )
    entry["updated_at"] = created_at
    version["updated_at"] = created_at
    meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
    meta = read_json(meta_path, {})
    meta["derived_outputs"] = copy.deepcopy(outputs)
    meta["updated_at"] = created_at
    _write_transaction(
        {
            root / rel_output: stored_payload,
            meta_path: json_text(meta),
            **_index_files(root, index),
        }
    )
    return {
        "status": "attached",
        "query_id": entry.get("query_id", ""),
        "version": version.get("version"),
        "attachment_id": attachment_id,
        "kind": kind,
        "path": rel_output,
        "retention": retention,
        "result_time_coverage": result_time_coverage,
        "time_coverage_blockers": result_time_blockers,
        "index_path": INDEX_REL.as_posix(),
        "index_html_path": INDEX_HTML_REL.as_posix(),
    }


def find_query_reference(root: Path, sql_file: Path, *, match_fingerprint: bool = True) -> dict[str, Any] | None:
    root = root.resolve()
    index_path = root / INDEX_REL
    if not index_path.exists() or not sql_file.exists():
        return None
    index = load_index(root)
    rel = ""
    try:
        rel = project_relative(root, sql_file)
    except ValueError:
        pass
    fingerprint = sql_fingerprint(sql_file.read_text(encoding="utf-8-sig")) if match_fingerprint else ""
    entry, version = _find_entry(index, sql_path=rel, fingerprint=fingerprint)
    if not entry or not version:
        return None
    return {
        "query_id": entry.get("query_id", ""),
        "version": version.get("version"),
        "status": version.get("status", entry.get("status", "")),
        "path": version.get("path", ""),
        "meta_path": version.get("meta_path", ""),
        "formalize_seed_path": version.get("formalize_seed_path", ""),
        "sql_fingerprint": version.get("sql_fingerprint", ""),
        "logic_fingerprint": version.get("logic_fingerprint", entry.get("logic_fingerprint", "")),
        "purpose": version.get("purpose") or entry.get("purpose", ""),
        "title": version.get("title") or entry.get("title", ""),
        "delivery_ready": bool(version.get("delivery_ready")),
        "formal_artifact_path": version.get("formal_artifact_path", ""),
        "change_type": version.get("change_type", ""),
        "coverage_relation": version.get("coverage_relation", ""),
        "branch_of": copy.deepcopy(entry.get("branch_of") or {}),
        "derived_output_count": len(version.get("derived_outputs", [])) if isinstance(version.get("derived_outputs"), list) else 0,
        "source_intake": copy.deepcopy(version.get("source_intake") or entry.get("source_intake") or {}),
        "temporary_rule_override": copy.deepcopy(
            version.get("temporary_rule_override") or entry.get("temporary_rule_override") or {}
        ),
        "knowledge_references": copy.deepcopy(
            version.get("knowledge_references") or entry.get("knowledge_references") or []
        ),
        "knowledge_usage": copy.deepcopy(
            version.get("knowledge_usage") or entry.get("knowledge_usage") or {}
        ),
        "execution_route": copy.deepcopy(
            version.get("execution_route") or entry.get("execution_route") or {}
        ),
        "summary_plan": copy.deepcopy(
            version.get("summary_plan") or entry.get("summary_plan") or {}
        ),
        "analysis_role": str(version.get("analysis_role") or entry.get("analysis_role") or ""),
        "workspace_role": str(version.get("workspace_role") or entry.get("workspace_role") or "unknown"),
        "role_lineage": copy.deepcopy(version.get("role_lineage") or entry.get("role_lineage") or {}),
        "analysis_bundle": copy.deepcopy(
            version.get("analysis_bundle") or entry.get("analysis_bundle") or {}
        ),
    }


def query_delivery_receipt(
    root: Path,
    *,
    query_id: str = "",
    sql_path: str = "",
    version_number: int = 0,
    execution_route: dict[str, Any] | None = None,
    sql_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify one indexed SQL file and return the only valid QUERY delivery receipt."""

    root = root.resolve()
    index = load_index(root)
    entry: dict[str, Any] | None = None
    version: dict[str, Any] | None = None
    if query_id:
        entry, _ = _find_entry(index, query_id=normalize_query_id(query_id))
        if entry:
            version = _entry_version(entry, version_number)
    elif sql_path:
        entry, version = _find_entry(index, sql_path=sql_path)

    blockers: list[str] = []
    if not entry or not version:
        blockers.append("SQL is not an indexed query workspace version.")
        return {
            "schema_version": DELIVERY_RECEIPT_SCHEMA_VERSION,
            "status": "blocked",
            "delivery_ready": False,
            "blockers": blockers,
        }

    relative_path = str(version.get("path") or "")
    absolute_path = ""
    sql_text = ""
    current_route: dict[str, Any] = {}
    execution_compatibility: dict[str, Any] = {}
    try:
        resolved_path = resolve_project_path(root, relative_path)
        absolute_path = str(resolved_path)
    except ValueError as exc:
        blockers.append(str(exc))
        resolved_path = None
    if resolved_path is None or not resolved_path.exists():
        blockers.append(f"Indexed SQL file is missing: {relative_path}")
    else:
        sql_text = normalize_sql_text(resolved_path.read_text(encoding="utf-8-sig"))
        actual_fingerprint = sql_fingerprint(sql_text)
        if actual_fingerprint != str(version.get("sql_fingerprint") or ""):
            blockers.append("Indexed SQL fingerprint does not match the file on disk.")
        privacy_transforms = sql_side_privacy_transforms(sql_text)
        if privacy_transforms:
            functions = ", ".join(sorted({item["function"] for item in privacy_transforms}))
            blockers.append(
                "SQL-side de-identification is forbidden "
                f"(found: {functions}); DA owns privacy handling."
            )
        project_config = read_json(root / "project_config.json", {})
        if route_matches_context(execution_route, sql_text, project_config):
            current_route = copy.deepcopy(execution_route)
        else:
            current_route = execution_route_for_sql(sql_text, project_config)
        if current_route.get("status") != "ready":
            blockers.extend(str(item) for item in current_route.get("blockers", []) or ["Execution route is not ready."])
        current_facts: dict[str, Any] = {}
        if (
            isinstance(sql_facts, dict)
            and sql_facts.get("execution_fingerprint") == actual_fingerprint
            and isinstance(sql_facts.get("performance"), dict)
            and "cte_dependency_depth" in sql_facts["performance"]
        ):
            current_facts = copy.deepcopy(sql_facts)
        seed_reference = str(version.get("formalize_seed_path") or "")
        if not current_facts and seed_reference:
            try:
                seed = read_json(resolve_project_path(root, seed_reference), {})
                candidate_facts = seed.get("sql_fact_bundle") if isinstance(seed, dict) else {}
                if (
                    isinstance(candidate_facts, dict)
                    and candidate_facts.get("execution_fingerprint") == actual_fingerprint
                    and isinstance(candidate_facts.get("performance"), dict)
                    and "cte_dependency_depth" in candidate_facts["performance"]
                ):
                    current_facts = candidate_facts
            except (OSError, ValueError, json.JSONDecodeError):
                current_facts = {}
        if not current_facts:
            current_facts = build_sql_fact_bundle(sql_text, kind="QUERY", root=root)
        execution_config = {
            "sql_dialect": current_route.get("sql_dialect") or project_config.get("sql_dialect"),
            "query_engine": current_route.get("query_engine") or project_config.get("query_engine"),
            "query_environment": project_config.get("query_environment", {}),
        }
        cte_assessment = starrocks_cte_expansion_assessment(
            current_facts.get("performance", {}),
            execution_config,
        )
        execution_compatibility = {"starrocks_cte_expansion": cte_assessment}
        if cte_assessment.get("blocks_starrocks_delivery"):
            observed = cte_assessment.get("observed", {})
            blockers.append(
                "StarRocks/DA CTE expansion risk blocks delivery "
                f"(CTEs={observed.get('cte_count')}, depth={observed.get('dependency_depth')}, "
                f"JOINs={observed.get('join_count')}, final_span={observed.get('final_reference_span')})."
            )

    gate_status = str((version.get("generation_gate") or {}).get("status") or "not_run")
    summary_plan = version.get("summary_plan") if isinstance(version.get("summary_plan"), dict) else {}
    analysis_bundle = version.get("analysis_bundle") if isinstance(version.get("analysis_bundle"), dict) else {}
    if summary_plan.get("routing") == "grouped_plus_overall":
        bundle_ref = str(analysis_bundle.get("path") or "")
        if not bundle_ref:
            blockers.append("Grouped/overall summary routing is pending its linked analysis bundle.")
        else:
            try:
                bundle_path = resolve_project_path(root, bundle_ref)
                bundle = read_json(bundle_path, {})
                member = next(
                    (
                        item
                        for item in bundle.get("members", [])
                        if isinstance(item, dict)
                        and item.get("query_id") == entry.get("query_id")
                        and int(item.get("version") or 0) == int(version.get("version") or 0)
                    ),
                    None,
                )
                if (
                    bundle.get("schema_version") != "query_analysis_bundle_v1"
                    or bundle.get("bundle_id") != analysis_bundle.get("bundle_id")
                    or not member
                    or member.get("sql_fingerprint") != version.get("sql_fingerprint")
                ):
                    blockers.append("Grouped/overall analysis bundle does not resolve to this exact SQL version.")
            except (OSError, ValueError, json.JSONDecodeError):
                blockers.append(f"Grouped/overall analysis bundle is missing or invalid: {bundle_ref}")
    if not version.get("delivery_ready") or gate_status != "ok":
        blockers.append(
            f"Indexed version is not deliverable: delivery_ready={bool(version.get('delivery_ready'))}, "
            f"generation_gate={gate_status}."
        )
    status = "blocked" if blockers else "ready"
    return {
        "schema_version": DELIVERY_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "query_id": entry.get("query_id", ""),
        "version": version.get("version"),
        "query_status": version.get("status", entry.get("status", "")),
        "project_relative_path": relative_path,
        "absolute_path": absolute_path,
        "delivery_file": absolute_path if status == "ready" else "",
        "purpose": version.get("purpose") or entry.get("purpose", ""),
        "delivery_ready": status == "ready",
        "generation_gate_status": gate_status,
        "knowledge_usage": copy.deepcopy(
            version.get("knowledge_usage") or entry.get("knowledge_usage") or {}
        ),
        "execution_route": copy.deepcopy(
            version.get("execution_route") or entry.get("execution_route") or current_route
        ),
        "execution_compatibility": execution_compatibility,
        "summary_plan": copy.deepcopy(summary_plan),
        "analysis_bundle": copy.deepcopy(analysis_bundle),
        "is_current": int(version.get("version") or 0) == int(entry.get("current_version") or 0),
        "blockers": blockers,
        "final_response_requirement": (
            "Return a clickable link to absolute_path and a concise purpose/status summary. "
            "Do not paste SQL as the only deliverable and do not finish QUERY without this ready receipt."
        ),
    }


def with_delivery_receipt(
    root: Path,
    result: dict[str, Any],
    *,
    execution_route: dict[str, Any] | None = None,
    sql_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not result.get("delivery_ready") or not result.get("path"):
        return result
    receipt = query_delivery_receipt(
        root,
        query_id=str(result.get("query_id") or ""),
        sql_path=str(result.get("path") or ""),
        version_number=int(result.get("version") or 0),
        execution_route=execution_route,
        sql_facts=sql_facts,
    )
    result["delivery_receipt"] = receipt
    result["delivery_file"] = receipt.get("absolute_path", "") if receipt.get("status") == "ready" else ""
    if receipt.get("status") != "ready":
        result["status"] = "blocked"
        result["delivery_ready"] = False
        result["blockers"] = list(receipt.get("blockers") or [])
    return result


def origin_contract(reference: dict[str, Any] | None) -> dict[str, Any]:
    reference = reference if isinstance(reference, dict) else {}
    if not reference.get("query_id") or not reference.get("path"):
        return {}
    return {
        "contract_version": "query_workspace_origin_v1",
        "query_id": reference.get("query_id", ""),
        "version": reference.get("version"),
        "path": reference.get("path", ""),
        "meta_path": reference.get("meta_path", ""),
        "formalize_seed_path": reference.get("formalize_seed_path", ""),
        "source_status": reference.get("status", reference.get("query_status", "")),
        "source_sql_fingerprint": reference.get("sql_fingerprint", ""),
        "source_logic_fingerprint": reference.get("logic_fingerprint", ""),
        "purpose": reference.get("purpose", ""),
        "change_type": reference.get("change_type", ""),
        "coverage_relation": reference.get("coverage_relation", ""),
        "usage_class": reference.get("usage_class", "unclassified"),
        "workspace_role": reference.get("workspace_role", "unknown"),
        "role_lineage": copy.deepcopy(reference.get("role_lineage") or {}),
        "branch_of": copy.deepcopy(reference.get("branch_of") or {}),
        "request_envelope": copy.deepcopy(reference.get("request_envelope") or {}),
        "rule_application": copy.deepcopy(reference.get("rule_application") or {}),
        "derived_output_count": int(reference.get("derived_output_count") or 0),
        "temporary_rule_override": copy.deepcopy(
            reference.get("temporary_rule_override") or {}
        ),
        "knowledge_usage": copy.deepcopy(reference.get("knowledge_usage") or {}),
        "execution_route": copy.deepcopy(reference.get("execution_route") or {}),
    }


def _seed_document(
    *,
    root: Path,
    rel_sql: str,
    query_id: str,
    version: int,
    title: str,
    facts: dict[str, Any],
    fingerprint: str,
    rule_context: dict[str, Any] | None,
    gate_mode: str,
    provenance: dict[str, Any],
    knowledge_references: list[dict[str, Any]] | None = None,
    knowledge_usage: dict[str, Any] | None = None,
    execution_route: dict[str, Any] | None = None,
    summary_plan: dict[str, Any] | None = None,
    analysis_role: str = "",
    usage_class: str = "unclassified",
    workspace_role: str = "query",
    role_lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = read_json(root / "project_config.json", {})
    return {
        "schema_version": SEED_SCHEMA_VERSION,
        "source": "sql_query_workspace.py",
        "title": title,
        "slug": query_id,
        "project_root": ".",
        "project_context": {
            "project_id": config.get("project_id", root.name),
            "display_name": config.get("display_name", root.name),
            "sql_dialect": config.get("sql_dialect", ""),
            "query_engine": config.get("query_engine", ""),
            "query_environment": config.get("query_environment", ""),
            "table_naming_profile": (config.get("table_naming_profile") or {}).get("name", "") if isinstance(config.get("table_naming_profile"), dict) else "",
        },
        "project_config_fingerprint": json_fingerprint(config),
        "project_rules_fingerprint": rules_fingerprint(root),
        "source_sql_file": rel_sql,
        "source_sql_fingerprint": fingerprint,
        "normalized_sql_fingerprint": fingerprint,
        "logic_fingerprint": str(facts.get("logic_fingerprint") or ""),
        "normalized_changed": False,
        "rule_context_mode": gate_mode,
        "analysis": facts.get("analysis", {}),
        "sql_fact_bundle": copy.deepcopy(facts.get("sql_fact_bundle") or {}),
        "rule_context": _public_rule_context(rule_context),
        "request_envelope": copy.deepcopy((rule_context or {}).get("request_envelope") or {}),
        "rule_application": copy.deepcopy((rule_context or {}).get("rule_application") or {}),
        "temporary_rule_override": copy.deepcopy(
            (rule_context or {}).get("temporary_rule_override") or {}
        ),
        "query_workspace_ref": {
            "query_id": query_id,
            "version": version,
            "path": rel_sql,
            "sql_fingerprint": fingerprint,
        },
        "knowledge_references": copy.deepcopy(knowledge_references or []),
        "knowledge_usage": copy.deepcopy(knowledge_usage or {}),
        "execution_route": copy.deepcopy(execution_route or {}),
        "summary_plan": copy.deepcopy(summary_plan or {}),
        "analysis_role": str(analysis_role or ""),
        "usage_class": usage_class,
        "workspace_role": workspace_role,
        "role_lineage": copy.deepcopy(role_lineage or {}),
        "generation_provenance": provenance,
        "reuse_contract": {
            "analysis": "Reusable when logic_fingerprint matches; time parameter values may change, business parameters may not.",
            "rule_context": "Temporary diagnostics only; formal save reruns rule-context in formalize mode.",
            "repository_summary": "Not generated by the lightweight workspace index; formalization builds or reuses a semantic summary separately.",
        },
    }


def save_query(
    *,
    root: Path,
    source_sql: Path,
    title: str,
    purpose: str,
    business_question: str = "",
    status: str = "runnable",
    query_id: str = "",
    source_kind: str = "generated",
    tags: list[str] | None = None,
    revision_note: str = "",
    gate: dict[str, Any] | None = None,
    rule_context: dict[str, Any] | None = None,
    gate_mode: str = "temporary",
    facts: dict[str, Any] | None = None,
    write_seed: bool = True,
    source_intake: dict[str, Any] | None = None,
    create_working_copy: bool = False,
    change_type: str = "auto",
    coverage_relation: str = "",
    branch_of: str = "",
    knowledge_references: list[dict[str, Any]] | None = None,
    knowledge_usage_declaration: str = "auto",
    summary_plan: dict[str, Any] | None = None,
    analysis_role: str = "",
    usage_class: str = "",
    workspace_role: str = "query",
    role_lineage: dict[str, Any] | None = None,
    user_request: str = "",
) -> dict[str, Any]:
    root = root.resolve()
    source_sql = source_sql.resolve()
    title, purpose = validate_title_and_purpose(title, purpose)
    if status not in SAVE_STATUSES:
        raise ValueError(f"Unsupported save status `{status}`; expected one of {sorted(SAVE_STATUSES)}.")
    if usage_class and usage_class not in USAGE_CLASSES:
        raise ValueError(
            f"Unsupported usage class `{usage_class}`; expected one of {sorted(USAGE_CLASSES)}."
        )
    workspace_role = str(workspace_role or "unknown").strip().lower()
    if workspace_role not in WORKSPACE_ROLES:
        raise ValueError(
            f"Unsupported Workspace role `{workspace_role}`; expected one of {sorted(WORKSPACE_ROLES)}."
        )
    role_lineage = copy.deepcopy(role_lineage or {})
    if workspace_role == "dashboard_delivery":
        source_query_id = str(role_lineage.get("source_query_id") or "").strip().lower()
        source_query_version = int(role_lineage.get("source_query_version") or 0)
        if not re.fullmatch(r"qw-[a-z0-9-]{8,120}", source_query_id) or source_query_version < 1:
            raise ValueError(
                "dashboard_delivery Workspace roles require source_query_id and source_query_version lineage."
            )
        try:
            current_index = load_index(root)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("dashboard_delivery requires an existing Workspace index with its source query.") from exc
        source_entries = [
            item
            for item in current_index.get("entries", [])
            if isinstance(item, dict) and str(item.get("query_id") or "") == source_query_id
        ]
        if len(source_entries) != 1:
            raise ValueError("dashboard_delivery source_query_id must resolve to exactly one Workspace query family.")
        source_versions = {
            int(item.get("version") or 0)
            for item in source_entries[0].get("versions", [])
            if isinstance(item, dict)
        }
        if source_query_version not in source_versions:
            raise ValueError("dashboard_delivery source_query_version is not present in the Workspace index.")
        role_lineage.update(
            {
                "source_query_id": source_query_id,
                "source_query_version": source_query_version,
                "contract_version": "workspace_role_lineage_v1",
            }
        )
    if not source_sql.exists():
        raise ValueError(f"SQL file not found: {source_sql}")
    sql = stamp_sql_generation(
        root,
        normalize_sql_text(source_sql.read_text(encoding="utf-8-sig")),
    )
    if not sql or not re.search(r"\b(select|with|show|describe|desc|explain)\b", sql, flags=re.I):
        raise ValueError("Query workspace accepts non-empty query SQL only.")
    privacy_transforms = sql_side_privacy_transforms(sql)
    if status in DELIVERY_READY_STATUSES and privacy_transforms:
        functions = ", ".join(sorted({item["function"] for item in privacy_transforms}))
        raise ValueError(
            "Executable SQL cannot perform de-identification in SQL "
            f"(found: {functions}). Keep business-required identifiers unchanged; DA owns privacy handling."
        )
    compact_gate = _compact_gate(gate, mode=gate_mode)
    rule_application = (
        copy.deepcopy((rule_context or {}).get("rule_application"))
        if isinstance(rule_context, dict)
        else {}
    )
    if not application_integrity_ok(rule_application):
        request_envelope = build_request_envelope(
            user_request,
            function_id="QUERY",
            lifecycle_stage="temporary_query",
        )
        rule_application = build_rule_application(
            request_envelope=request_envelope,
            mode=gate_mode,
            lifecycle_stage="temporary_query",
        )
    request_envelope = copy.deepcopy(rule_application.get("request_envelope") or {})
    if not isinstance(rule_context, dict):
        rule_context = {}
    rule_context["request_envelope"] = copy.deepcopy(request_envelope)
    rule_context["rule_application"] = copy.deepcopy(rule_application)
    project_config = read_json(root / "project_config.json", {})
    supplied_route = None
    if isinstance(facts, dict) and isinstance(facts.get("execution_route"), dict):
        supplied_route = facts.get("execution_route")
    if supplied_route is None and isinstance(rule_context.get("project_contract_check"), dict):
        candidate_route = rule_context["project_contract_check"].get("execution_route")
        if isinstance(candidate_route, dict):
            supplied_route = candidate_route
    execution_route = execution_route_for_file(
        source_sql,
        sql,
        project_config,
        precomputed_route=supplied_route,
    )
    if status in DELIVERY_READY_STATUSES and execution_route.get("status") != "ready":
        raise ValueError(
            "Query execution route is not deliverable: "
            + "; ".join(
                str(item)
                for item in execution_route.get("blockers", []) or ["execution route not ready"]
            )
        )
    if status in DELIVERY_READY_STATUSES and compact_gate.get("status") != "ok":
        raise ValueError(
            f"Query cannot be saved as `{status}` until generation_gate.status=ok; got `{compact_gate.get('status')}`."
        )
    ensure_workspace(root)
    index = load_index(root)
    fingerprint = sql_fingerprint(sql)
    knowledge_references = copy.deepcopy(knowledge_references or [])
    for reference in knowledge_references:
        if not isinstance(reference, dict):
            raise ValueError("Knowledge reference rows must be JSON objects.")
        problems = validate_knowledge_reference(root, reference)
        if problems:
            raise ValueError("Invalid knowledge reference: " + "; ".join(problems))
    knowledge_usage = build_knowledge_usage(
        root,
        knowledge_references,
        declaration=knowledge_usage_declaration,
        declaration_source="query_workspace_save",
    )
    supplied_bundle = facts.get("sql_fact_bundle") if isinstance(facts, dict) else None
    if not isinstance(supplied_bundle, dict) or supplied_bundle.get("execution_fingerprint") != fingerprint:
        generated_facts = _query_facts(root, source_sql, sql, tags)
        generated_bundle = generated_facts["sql_fact_bundle"]
        generated_facts.update(facts or {})
        generated_facts["sql_fact_bundle"] = generated_bundle
        generated_facts["logic_fingerprint"] = generated_bundle["logic_fingerprint"]
        facts = generated_facts
    summary_plan = copy.deepcopy(summary_plan or {})
    fact_bundle = facts.get("sql_fact_bundle") if isinstance(facts.get("sql_fact_bundle"), dict) else {}
    performance = fact_bundle.get("performance") if isinstance(fact_bundle.get("performance"), dict) else {}
    cte_assessment = starrocks_cte_expansion_assessment(
        performance,
        {
            "sql_dialect": execution_route.get("sql_dialect") or project_config.get("sql_dialect"),
            "query_engine": execution_route.get("query_engine") or project_config.get("query_engine"),
            "query_environment": project_config.get("query_environment", {}),
        },
    )
    facts["execution_compatibility"] = {"starrocks_cte_expansion": cte_assessment}
    if status in DELIVERY_READY_STATUSES and cte_assessment.get("blocks_starrocks_delivery"):
        observed = cte_assessment.get("observed", {})
        raise ValueError(
            "StarRocks/DA CTE expansion risk blocks runnable delivery: "
            f"CTEs={observed.get('cte_count')}, depth={observed.get('dependency_depth')}, "
            f"JOINs={observed.get('join_count')}, final_span={observed.get('final_reference_span')}. "
            "Compress passthrough CTEs, shorten the dependency chain, inline a small terminal aggregate, "
            "Use stable Hive only after explicit user selection."
        )
    grouped_metric_output = bool(
        fact_bundle.get("metrics")
        and fact_bundle.get("dimensions")
        and (performance.get("has_group_by") or performance.get("has_aggregate"))
    )
    if status in DELIVERY_READY_STATUSES and grouped_metric_output and not summary_plan:
        raise ValueError(
            "Grouped metric SQL requires a summary_feasibility_v1 plan before runnable delivery. "
            "Run sql_summary_planner.py plan and decide whether one SQL is exact or a grouped/overall bundle is required."
        )
    if summary_plan:
        analysis_role = str(analysis_role or ("grouped" if grouped_metric_output else "standalone")).strip().lower()
        if analysis_role not in {"grouped", "overall", "standalone"}:
            raise ValueError("analysis_role must be grouped, overall, or standalone.")
        if summary_plan.get("schema_version") != SUMMARY_PLAN_VERSION:
            raise ValueError(f"summary_plan schema_version must be {SUMMARY_PLAN_VERSION}.")
        summary_plan["metric_contract_fingerprint"] = summary_plan_fingerprint(summary_plan)
        summary_problems = validate_summary_plan(sql, summary_plan, role=analysis_role, root=root)
        if summary_problems:
            raise ValueError("Invalid summary feasibility plan: " + "; ".join(summary_problems))
        if summary_plan.get("routing") == "grouped_plus_overall" and analysis_role not in {"grouped", "overall"}:
            raise ValueError("grouped_plus_overall routing requires analysis_role grouped or overall.")
    elif analysis_role:
        raise ValueError("analysis_role cannot be saved without summary_plan.")
    logic_fingerprint = str(facts.get("logic_fingerprint") or sql_logic_fingerprint(sql))
    requested_query_id, requested_change_type, resolved_coverage = resolve_change_contract(
        query_id=query_id,
        source_kind=source_kind,
        change_type=change_type,
        coverage_relation=coverage_relation,
        branch_of=branch_of,
        revision_note=revision_note,
    )
    duplicate_entry, duplicate_version = _find_entry(index, fingerprint=fingerprint)
    if duplicate_entry and duplicate_version:
        existing_role = str(
            duplicate_version.get("workspace_role")
            or duplicate_entry.get("workspace_role")
            or "unknown"
        )
        if workspace_role != "unknown" and existing_role not in {"unknown", workspace_role}:
            raise ValueError(
                "This SQL fingerprint already belongs to a different Workspace role; "
                "create an explicit role-linked revision instead of silently reusing it."
            )
        duplicate_query_id = str(duplicate_entry.get("query_id") or "")
        if requested_change_type == "branch":
            raise ValueError(
                "A branch must contain materially different SQL. This fingerprint already belongs to "
                f"{duplicate_query_id}."
            )
        if requested_query_id and duplicate_query_id != requested_query_id:
            raise ValueError(
                f"This SQL fingerprint already belongs to query family {duplicate_query_id}; "
                "reuse that family instead of assigning identical SQL to another query_id."
            )
        if summary_plan:
            existing_plan = duplicate_version.get("summary_plan") if isinstance(duplicate_version.get("summary_plan"), dict) else {}
            if existing_plan and existing_plan != summary_plan:
                raise ValueError(
                    "This exact SQL fingerprint already has a different summary feasibility contract; "
                    "save corrected SQL or reuse the existing contract."
                )
            if not existing_plan:
                updated_at = now_iso()
                duplicate_version["summary_plan"] = copy.deepcopy(summary_plan)
                duplicate_version["analysis_role"] = str(analysis_role or "")
                duplicate_version.setdefault("analysis_bundle", {})
                duplicate_version["delivery_ready"] = bool(
                    duplicate_version.get("status") in DELIVERY_READY_STATUSES
                    and str((duplicate_version.get("generation_gate") or {}).get("status") or "") == "ok"
                    and summary_plan.get("routing") != "grouped_plus_overall"
                )
                duplicate_version["updated_at"] = updated_at
                if int(duplicate_entry.get("current_version") or 0) == int(duplicate_version.get("version") or 0):
                    duplicate_entry["summary_plan"] = copy.deepcopy(summary_plan)
                    duplicate_entry["analysis_role"] = str(analysis_role or "")
                    duplicate_entry.setdefault("analysis_bundle", {})
                    duplicate_entry["updated_at"] = updated_at
                duplicate_meta_path = resolve_project_path(root, str(duplicate_version.get("meta_path") or ""))
                duplicate_meta = read_json(duplicate_meta_path, {})
                duplicate_meta["summary_plan"] = copy.deepcopy(summary_plan)
                duplicate_meta["analysis_role"] = str(analysis_role or "")
                duplicate_meta.setdefault("analysis_bundle", {})
                duplicate_meta["delivery_ready"] = bool(duplicate_version.get("delivery_ready"))
                duplicate_meta["updated_at"] = updated_at
                files = {
                    duplicate_meta_path: json_text(duplicate_meta),
                    **_index_files(root, index),
                }
                duplicate_seed_ref = str(duplicate_version.get("formalize_seed_path") or "")
                if duplicate_seed_ref:
                    duplicate_seed_path = resolve_project_path(root, duplicate_seed_ref)
                    duplicate_seed = read_json(duplicate_seed_path, {})
                    duplicate_seed["summary_plan"] = copy.deepcopy(summary_plan)
                    duplicate_seed["analysis_role"] = str(analysis_role or "")
                    duplicate_seed.setdefault("analysis_bundle", {})
                    files[duplicate_seed_path] = json_text(duplicate_seed)
                _write_transaction(files)
        if create_working_copy:
            duplicate_intake = copy.deepcopy(source_intake or {})
            duplicate_working = (
                WORKSPACE_REL / "_working" / str(duplicate_entry.get("query_id") or "query") / "candidate.sql"
            ).as_posix()
            duplicate_intake.update(
                {
                    "contract_version": str(duplicate_intake.get("contract_version") or "external_sql_intake_v1"),
                    "source_snapshot_path": str(duplicate_version.get("path") or ""),
                    "working_copy_path": duplicate_working,
                    "external_input_immutable": True,
                    "absolute_source_path_persisted": False,
                }
            )
            duplicate_version["source_intake"] = copy.deepcopy(duplicate_intake)
            duplicate_entry["source_intake"] = copy.deepcopy(duplicate_intake)
            duplicate_meta_path = resolve_project_path(root, str(duplicate_version.get("meta_path") or ""))
            duplicate_meta = read_json(duplicate_meta_path, {})
            duplicate_meta["source_intake"] = copy.deepcopy(duplicate_intake)
            duplicate_meta["updated_at"] = now_iso()
            _write_transaction(
                {
                    root / duplicate_working: sql,
                    duplicate_meta_path: json_text(duplicate_meta),
                    **_index_files(root, index),
                }
            )
        is_current_duplicate = (
            int(duplicate_version.get("version") or 0) == int(duplicate_entry.get("current_version") or 0)
        )
        if (
            status == "runnable"
            and compact_gate.get("status") == "ok"
            and is_current_duplicate
            and not duplicate_version.get("delivery_ready")
            and not (
                summary_plan.get("routing") == "grouped_plus_overall"
                and not duplicate_version.get("analysis_bundle")
            )
        ):
            reactivated_at = now_iso()
            duplicate_version.update(
                {
                    "status": "runnable",
                    "title": title,
                    "purpose": purpose,
                    "business_question": business_question or purpose,
                    "delivery_ready": True,
                    "generation_gate": compact_gate,
                    "logic_fingerprint": logic_fingerprint,
                    "updated_at": reactivated_at,
                }
            )
            duplicate_version.setdefault("status_history", []).append(
                {
                    "status": "runnable",
                    "at": reactivated_at,
                    "reason": revision_note or "Reactivated the exact indexed SQL after rerunning the delivery gate.",
                }
            )
            duplicate_entry.update(
                {
                    "status": "runnable",
                    "title": title,
                    "purpose": purpose,
                    "business_question": business_question or purpose,
                    "logic_fingerprint": logic_fingerprint,
                    "updated_at": reactivated_at,
                }
            )
            duplicate_meta_path = resolve_project_path(root, str(duplicate_version.get("meta_path") or ""))
            duplicate_meta = read_json(duplicate_meta_path, {})
            duplicate_meta.update(
                {
                    "status": "runnable",
                    "title": title,
                    "purpose": purpose,
                    "business_question": business_question or purpose,
                    "delivery_ready": True,
                    "generation_gate": compact_gate,
                    "logic_fingerprint": logic_fingerprint,
                    "status_history": duplicate_version.get("status_history", []),
                    "updated_at": reactivated_at,
                }
            )
            _write_transaction(
                {
                    duplicate_meta_path: json_text(duplicate_meta),
                    **_index_files(root, index),
                }
            )
            return with_delivery_receipt(
                root,
                {
                    "status": "reactivated",
                    "query_id": duplicate_entry.get("query_id", ""),
                    "version": duplicate_version.get("version"),
                    "query_status": "runnable",
                    "path": duplicate_version.get("path", ""),
                    "meta_path": duplicate_version.get("meta_path", ""),
                    "formalize_seed_path": duplicate_version.get("formalize_seed_path", ""),
                    "index_path": INDEX_REL.as_posix(),
                    "index_html_path": INDEX_HTML_REL.as_posix(),
                    "delivery_ready": True,
                    "logic_fingerprint": duplicate_version.get("logic_fingerprint", ""),
                    "deduplicated_by": "sql_fingerprint",
                    "change_type": str(duplicate_version.get("change_type") or ""),
                    "coverage_relation": str(duplicate_version.get("coverage_relation") or ""),
                    "working_copy_path": str(
                        ((duplicate_version.get("source_intake") or duplicate_entry.get("source_intake") or {}).get("working_copy_path"))
                        or ""
                    ),
                },
                execution_route=execution_route,
                sql_facts=fact_bundle,
            )
        return with_delivery_receipt(
            root,
            {
                "status": "reused",
                "query_id": duplicate_entry.get("query_id", ""),
                "version": duplicate_version.get("version"),
                "query_status": duplicate_version.get("status", duplicate_entry.get("status", "")),
                "path": duplicate_version.get("path", ""),
                "meta_path": duplicate_version.get("meta_path", ""),
                "formalize_seed_path": duplicate_version.get("formalize_seed_path", ""),
                "index_path": INDEX_REL.as_posix(),
                "index_html_path": INDEX_HTML_REL.as_posix(),
                "delivery_ready": bool(duplicate_version.get("delivery_ready")),
                "logic_fingerprint": duplicate_version.get("logic_fingerprint", ""),
                "deduplicated_by": "sql_fingerprint",
                "change_type": str(duplicate_version.get("change_type") or ""),
                "coverage_relation": str(duplicate_version.get("coverage_relation") or ""),
                "working_copy_path": str(
                    ((duplicate_version.get("source_intake") or duplicate_entry.get("source_intake") or {}).get("working_copy_path"))
                    or ""
                ),
            },
            execution_route=execution_route,
            sql_facts=fact_bundle,
        )

    entry: dict[str, Any] | None = None
    branch_reference: dict[str, Any] | None = None
    if branch_of:
        branch_text = str(branch_of).replace("\\", "/").lstrip("./")
        if branch_text.startswith("qw-"):
            branch_entry, branch_version = _find_entry(index, query_id=normalize_query_id(branch_text))
        else:
            branch_entry, branch_version = _find_entry(index, sql_path=branch_text)
        if not branch_entry:
            raise ValueError(f"Branch source is not indexed: {branch_of}")
        if branch_version is None:
            current = int(branch_entry.get("current_version") or 0)
            branch_version = next(
                (item for item in branch_entry.get("versions", []) if int(item.get("version") or 0) == current),
                None,
            )
        branch_reference = {
            "query_id": branch_entry.get("query_id", ""),
            "version": (branch_version or {}).get("version"),
            "path": (branch_version or {}).get("path", branch_entry.get("current_path", "")),
        }

    if requested_query_id:
        query_id = requested_query_id
        entry, _ = _find_entry(index, query_id=requested_query_id)
        if not entry:
            raise ValueError(f"Query workspace entry not found for revision: {requested_query_id}")
    resolved_usage_class = str(
        usage_class or (entry or {}).get("usage_class") or "unclassified"
    )
    if entry is None:
        query_id = _new_query_id(title, fingerprint, index)
        version = 1
        family_dir = WORKSPACE_REL / now_day() / query_id
        entry = {
            "query_id": query_id,
            "title": title,
            "purpose": purpose,
            "business_question": business_question or purpose,
            "status": status,
            "current_version": version,
            "current_path": "",
            "sql_fingerprint": fingerprint,
            "logic_fingerprint": logic_fingerprint,
            "usage_class": resolved_usage_class,
            "workspace_role": workspace_role,
            "role_lineage": copy.deepcopy(role_lineage),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "formal_artifacts": [],
            "branch_of": copy.deepcopy(branch_reference or {}),
            "derived_output_count": 0,
            "versions": [],
        }
        index.setdefault("entries", []).append(entry)
    else:
        versions = [item for item in entry.get("versions", []) if isinstance(item, dict)]
        version = max([int(item.get("version") or 0) for item in versions] or [0]) + 1
        current_path = str(entry.get("current_path") or "")
        family_dir = Path(current_path).parent if current_path else WORKSPACE_REL / now_day() / query_id

    rel_sql = (family_dir / f"v{version:03d}.sql").as_posix()
    rel_meta = (family_dir / f"v{version:03d}.meta.json").as_posix()
    rel_seed = (family_dir / f"v{version:03d}.formalize_seed.json").as_posix() if write_seed else ""
    previous_path = str(entry.get("current_path") or "")
    created_at = now_iso()
    temporary_override = acknowledge_temporary_rule_override(
        (rule_context or {}).get("temporary_rule_override"),
        entry.get("versions", []),
        acknowledged_at=created_at,
    )
    if temporary_override and isinstance(rule_context, dict):
        rule_context["temporary_rule_override"] = copy.deepcopy(temporary_override)
    intake = copy.deepcopy(source_intake or entry.get("source_intake") or {})
    rel_working = ""
    if intake:
        intake.setdefault("source_snapshot_path", rel_sql)
    if create_working_copy:
        rel_working = (WORKSPACE_REL / "_working" / query_id / "candidate.sql").as_posix()
        intake.update(
            {
                "contract_version": str(intake.get("contract_version") or "external_sql_intake_v1"),
                "source_snapshot_path": rel_sql,
                "working_copy_path": rel_working,
                "external_input_immutable": True,
                "absolute_source_path_persisted": False,
            }
        )
    provenance = build_generation_provenance(
        generator_script="sql_query_workspace.py",
        workflow="query_workspace_save",
        artifact_kind="TEMP_QUERY",
        generated_at=created_at,
        source=source_kind,
        extra={"query_id": query_id, "query_version": version},
    )
    pending_analysis_bundle = bool(summary_plan.get("routing") == "grouped_plus_overall")
    delivery_ready = (
        status in DELIVERY_READY_STATUSES
        and compact_gate.get("status") == "ok"
        and not pending_analysis_bundle
    )
    status_history = [
        {
            "status": status,
            "at": created_at,
            "reason": revision_note or ("Saved and indexed before query delivery." if delivery_ready else "Saved as a non-deliverable draft."),
        }
    ]
    version_row = {
        "version": version,
        "status": status,
        "title": title,
        "purpose": purpose,
        "business_question": business_question or purpose,
        "path": rel_sql,
        "meta_path": rel_meta,
        "formalize_seed_path": rel_seed,
        "sql_fingerprint": fingerprint,
        "logic_fingerprint": logic_fingerprint,
        "previous_version_path": previous_path,
        "next_version_path": "",
        "delivery_ready": delivery_ready,
        "generation_gate": compact_gate,
        "request_envelope": copy.deepcopy(request_envelope),
        "rule_application": copy.deepcopy(rule_application),
        "temporary_rule_override": copy.deepcopy(temporary_override),
        "knowledge_references": copy.deepcopy(knowledge_references),
        "knowledge_usage": copy.deepcopy(knowledge_usage),
        "execution_route": copy.deepcopy(execution_route),
        "summary_plan": copy.deepcopy(summary_plan),
        "analysis_role": str(analysis_role or ""),
        "analysis_bundle": {},
        "usage_class": resolved_usage_class,
        "workspace_role": workspace_role,
        "role_lineage": copy.deepcopy(role_lineage),
        "status_history": status_history,
        "formal_artifact_path": "",
        "change_type": requested_change_type,
        "coverage_relation": resolved_coverage,
        "change_summary": revision_note or (
            "Initial indexed query version."
            if requested_change_type == "new"
            else f"Saved as {requested_change_type} relative to the prior query contract."
        ),
        "branch_of": copy.deepcopy(branch_reference or {}),
        "derived_outputs": [],
        "created_at": created_at,
        "updated_at": created_at,
    }
    if intake:
        version_row["source_intake"] = copy.deepcopy(intake)
    prior_meta_update: tuple[Path, dict[str, Any]] | None = None
    if entry.get("versions"):
        prior = entry["versions"][-1]
        if isinstance(prior, dict):
            prior["next_version_path"] = rel_sql
            prior_meta_path = resolve_project_path(root, str(prior.get("meta_path") or ""))
            prior_meta = read_json(prior_meta_path, {})
            if isinstance(prior_meta, dict):
                prior_meta["next_version_path"] = rel_sql
                prior_meta["updated_at"] = created_at
                prior_meta_update = (prior_meta_path, prior_meta)
    entry.setdefault("versions", []).append(version_row)
    entry.update(
        {
            "title": title,
            "purpose": purpose,
            "business_question": business_question or purpose,
            "status": status,
            "current_version": version,
            "current_path": rel_sql,
            "sql_fingerprint": fingerprint,
            "logic_fingerprint": logic_fingerprint,
            "business_category": facts.get("business_category", "uncategorized"),
            "analysis_type": facts.get("analysis_type", "unspecified"),
            "usage_class": resolved_usage_class,
            "workspace_role": workspace_role,
            "role_lineage": copy.deepcopy(role_lineage),
            "source_logs": facts.get("source_logs", []),
            "tables": facts.get("tables", []),
            "metrics": facts.get("metrics", []),
            "dimensions": facts.get("dimensions", []),
            "filters": facts.get("filters", []),
            "params": facts.get("params", {}),
            "grain": facts.get("grain", ""),
            "time_grain": facts.get("time_grain", ""),
            "tags": facts.get("tags", tags or []),
            "generation_provenance": provenance,
            "request_envelope": copy.deepcopy(request_envelope),
            "rule_application": copy.deepcopy(rule_application),
            "knowledge_references": copy.deepcopy(knowledge_references),
            "knowledge_usage": copy.deepcopy(knowledge_usage),
            "execution_route": copy.deepcopy(execution_route),
            "summary_plan": copy.deepcopy(summary_plan),
            "analysis_role": str(analysis_role or ""),
            "analysis_bundle": {},
            "change_type": requested_change_type,
            "coverage_relation": resolved_coverage,
            "branch_of": copy.deepcopy(branch_reference or entry.get("branch_of") or {}),
            "derived_output_count": sum(
                len(item.get("derived_outputs", []))
                for item in entry.get("versions", [])
                if isinstance(item, dict) and isinstance(item.get("derived_outputs", []), list)
            ),
            "updated_at": created_at,
        }
    )
    if intake:
        entry["source_intake"] = copy.deepcopy(intake)
    if temporary_override:
        entry["temporary_rule_override"] = copy.deepcopy(temporary_override)
    meta = {
        "schema_version": META_SCHEMA_VERSION,
        "query_id": query_id,
        "version": version,
        "lifecycle": "temporary_query",
        "formal_artifact": False,
        "status": status,
        "delivery_ready": delivery_ready,
        "title": title,
        "purpose": purpose,
        "business_question": business_question or purpose,
        "path": rel_sql,
        "meta_path": rel_meta,
        "formalize_seed_path": rel_seed,
        "sql_fingerprint": fingerprint,
        "logic_fingerprint": logic_fingerprint,
        "previous_version_path": previous_path,
        "next_version_path": "",
        "source_kind": source_kind,
        "business_category": facts.get("business_category", "uncategorized"),
        "analysis_type": facts.get("analysis_type", "unspecified"),
        "usage_class": resolved_usage_class,
        "source_logs": facts.get("source_logs", []),
        "tables": facts.get("tables", []),
        "metrics": facts.get("metrics", []),
        "dimensions": facts.get("dimensions", []),
        "filters": facts.get("filters", []),
        "params": facts.get("params", {}),
        "grain": facts.get("grain", ""),
        "time_grain": facts.get("time_grain", ""),
        "tags": facts.get("tags", tags or []),
        "generation_gate": compact_gate,
        "request_envelope": copy.deepcopy(request_envelope),
        "rule_application": copy.deepcopy(rule_application),
        "temporary_rule_override": copy.deepcopy(temporary_override),
        "knowledge_references": copy.deepcopy(knowledge_references),
        "knowledge_usage": copy.deepcopy(knowledge_usage),
        "execution_route": copy.deepcopy(execution_route),
        "summary_plan": copy.deepcopy(summary_plan),
        "analysis_role": str(analysis_role or ""),
        "analysis_bundle": {},
        "workspace_role": workspace_role,
        "role_lineage": copy.deepcopy(role_lineage),
        "status_history": status_history,
        "formal_artifact_path": "",
        "change_type": requested_change_type,
        "coverage_relation": resolved_coverage,
        "change_summary": version_row["change_summary"],
        "branch_of": copy.deepcopy(branch_reference or {}),
        "derived_outputs": [],
        "generation_provenance": provenance,
        "created_at": created_at,
        "updated_at": created_at,
    }
    if intake:
        meta["source_intake"] = copy.deepcopy(intake)
    files = {
        root / rel_sql: sql,
        root / rel_meta: json_text(meta),
        **_index_files(root, index),
    }
    if rel_working:
        files[root / rel_working] = sql
    if prior_meta_update:
        files[prior_meta_update[0]] = json_text(prior_meta_update[1])
    if rel_seed:
        seed = _seed_document(
            root=root,
            rel_sql=rel_sql,
            query_id=query_id,
            version=version,
            title=title,
            facts=facts,
            fingerprint=fingerprint,
            rule_context=rule_context,
            gate_mode=gate_mode,
            provenance=provenance,
            knowledge_references=knowledge_references,
            knowledge_usage=knowledge_usage,
            execution_route=execution_route,
            summary_plan=summary_plan,
            analysis_role=analysis_role,
            usage_class=resolved_usage_class,
            workspace_role=workspace_role,
            role_lineage=role_lineage,
        )
        files[root / rel_seed] = json_text(seed)
    _write_transaction(files)
    return with_delivery_receipt(
        root,
        {
            "status": "saved",
            "query_id": query_id,
            "version": version,
            "query_status": status,
            "path": rel_sql,
            "meta_path": rel_meta,
            "formalize_seed_path": rel_seed,
            "index_path": INDEX_REL.as_posix(),
            "index_html_path": INDEX_HTML_REL.as_posix(),
            "delivery_ready": delivery_ready,
            "sql_fingerprint": fingerprint,
            "logic_fingerprint": logic_fingerprint,
            "temporary_rule_override": copy.deepcopy(temporary_override),
            "knowledge_references": copy.deepcopy(knowledge_references),
            "knowledge_usage": copy.deepcopy(knowledge_usage),
            "execution_route": copy.deepcopy(execution_route),
            "summary_plan": copy.deepcopy(summary_plan),
            "analysis_role": str(analysis_role or ""),
            "usage_class": resolved_usage_class,
            "workspace_role": workspace_role,
            "role_lineage": copy.deepcopy(role_lineage),
            "analysis_bundle": {},
            "change_type": requested_change_type,
            "coverage_relation": resolved_coverage,
            "branch_of": copy.deepcopy(branch_reference or {}),
            "working_copy_path": rel_working,
        },
        execution_route=execution_route,
        sql_facts=fact_bundle,
    )


def import_external_query(
    *,
    root: Path,
    source_sql: Path,
    title: str,
    purpose: str,
    business_question: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Snapshot external SQL and create the only project-local editable copy."""

    root = root.resolve()
    source_sql = source_sql.resolve()
    if not source_sql.exists():
        raise ValueError(f"SQL file not found: {source_sql}")
    if is_project_local(root, source_sql):
        raise ValueError(
            "`import` is for SQL outside the target project. Use `save` for an existing project-local candidate."
        )
    source_hash_before = file_sha256(source_sql)
    intake = external_source_intake(source_sql)
    result = save_query(
        root=root,
        source_sql=source_sql,
        title=title,
        purpose=purpose,
        business_question=business_question,
        status="draft",
        source_kind="external_import",
        tags=tags,
        revision_note="Imported external SQL as a read-only source snapshot before project-local editing.",
        gate=None,
        rule_context=None,
        gate_mode="external_intake",
        write_seed=False,
        source_intake=intake,
        create_working_copy=True,
    )
    source_hash_after = file_sha256(source_sql)
    if source_hash_after != source_hash_before:
        raise OSError("External SQL changed during intake; the project copy was not accepted as an immutable import.")
    result.update(
        {
            "source_unchanged": True,
            "source_sha256": source_hash_before,
            "next_step": (
                f"Edit only `{result.get('working_copy_path')}` and save it as a new immutable version with "
                f"`--query-id {result.get('query_id')}`."
            ),
        }
    )
    return result


def transition_query(
    *,
    root: Path,
    status: str,
    query_id: str = "",
    sql_path: str = "",
    reason: str,
    result_status: str = "",
    formal_artifact_path: str = "",
) -> dict[str, Any]:
    root = root.resolve()
    if status not in QUERY_STATUSES - {"draft", "runnable"}:
        raise ValueError(f"Unsupported transition status: {status}")
    if len(str(reason or "").strip()) < 4:
        raise ValueError("A concrete transition --reason is required.")
    index = load_index(root)
    normalized_id = normalize_query_id(query_id) if query_id else ""
    normalized_path = str(sql_path or "").replace("\\", "/").lstrip("./")
    entry, version = _find_entry(index, query_id=normalized_id, sql_path=normalized_path)
    if not entry:
        raise ValueError("Query workspace entry not found.")
    if version is None:
        current = int(entry.get("current_version") or 0)
        version = next((item for item in entry.get("versions", []) if int(item.get("version") or 0) == current), None)
    if not isinstance(version, dict):
        raise ValueError("Query workspace current version is missing.")
    if status == "result_confirmed":
        result_rows = [
            item
            for item in version.get("derived_outputs", [])
            if isinstance(item, dict)
            and item.get("kind") == "result_evidence"
            and item.get("asset_state", "active") != "discarded"
        ]
        if not result_rows:
            raise ValueError("result_confirmed requires exact result_evidence on this query version.")
        latest_result = result_rows[-1]
        coverage = latest_result.get("result_time_coverage")
        coverage_blockers = time_coverage_problem_messages(
            coverage if isinstance(coverage, dict) else {}
        )
        if not isinstance(coverage, dict) or not coverage:
            sql_file = resolve_project_path(root, str(version.get("path") or ""))
            sql_text = sql_file.read_text(encoding="utf-8-sig")
            project_config = read_json(root / "project_config.json", {})
            effective_config, _ = effective_config_for_context(
                project_config,
                sql_text,
                version.get("execution_route"),
            )
            if time_integrity_plan(sql_text, effective_config).get("actual_range_required"):
                coverage_blockers = [
                    "查询范围包含或可能包含今日，但结果附件没有 result_time_coverage_v1；请重新绑定结果并核对实际数据范围。"
                ]
        if coverage_blockers:
            raise ValueError(" ".join(coverage_blockers))
    current_status = str(version.get("status") or "draft")
    if status != current_status and status not in STATUS_TRANSITIONS.get(current_status, set()):
        raise ValueError(
            f"Invalid query status transition `{current_status}` -> `{status}`; create a new revision when SQL must become runnable again."
        )
    if formal_artifact_path:
        formal_artifact_path = project_relative(root, resolve_project_path(root, formal_artifact_path))
    changed_at = now_iso()
    history = version.setdefault("status_history", [])
    if status != current_status or not history:
        history.append({"status": status, "at": changed_at, "reason": reason})
    version["status"] = status
    version["updated_at"] = changed_at
    version["delivery_ready"] = status in DELIVERY_READY_STATUSES and str((version.get("generation_gate") or {}).get("status")) == "ok"
    if result_status:
        version["result_status"] = result_status
    if status == "discarded":
        version["discard_reason"] = reason
    if formal_artifact_path:
        version["formal_artifact_path"] = formal_artifact_path
        links = entry.setdefault("formal_artifacts", [])
        if formal_artifact_path not in links:
            links.append(formal_artifact_path)
    if int(version.get("version") or 0) == int(entry.get("current_version") or 0):
        entry["status"] = status
        entry["updated_at"] = changed_at
    meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
    meta = read_json(meta_path, {})
    if not isinstance(meta, dict):
        raise ValueError(f"Query workspace metadata must be an object: {version.get('meta_path')}")
    meta.update(
        {
            "status": status,
            "delivery_ready": version.get("delivery_ready", False),
            "status_history": history,
            "updated_at": changed_at,
        }
    )
    if result_status:
        meta["result_status"] = result_status
    if status == "discarded":
        meta["discard_reason"] = reason
    if formal_artifact_path:
        meta["formal_artifact_path"] = formal_artifact_path
    _write_transaction({meta_path: json_text(meta), **_index_files(root, index)})
    return {
        "status": "updated",
        "query_id": entry.get("query_id", ""),
        "version": version.get("version"),
        "query_status": status,
        "path": version.get("path", ""),
        "meta_path": version.get("meta_path", ""),
        "formal_artifact_path": version.get("formal_artifact_path", ""),
        "delivery_ready": version.get("delivery_ready", False),
        "index_path": INDEX_REL.as_posix(),
        "index_html_path": INDEX_HTML_REL.as_posix(),
    }


def mark_promoted(root: Path, reference: dict[str, Any], formal_artifact_path: str) -> dict[str, Any]:
    status = str(reference.get("status") or "")
    if status == "promoted" and reference.get("formal_artifact_path") == formal_artifact_path:
        return {"status": "reused", **reference}
    return transition_query(
        root=root,
        query_id=str(reference.get("query_id") or ""),
        sql_path=str(reference.get("path") or ""),
        status="promoted",
        reason="Promoted to a formal QUERY artifact after user-confirmed result evidence.",
        formal_artifact_path=formal_artifact_path,
    )


def record_legacy_source_reference(
    root: Path,
    reference: dict[str, Any],
    source_reference: dict[str, Any],
) -> dict[str, Any]:
    """Attach project-relative migration provenance to an indexed SQL version."""

    root = root.resolve()
    index = load_index(root)
    entry, version = _find_entry(
        index,
        query_id=str(reference.get("query_id") or ""),
        sql_path=str(reference.get("path") or ""),
    )
    if not entry or not isinstance(version, dict):
        raise ValueError("Indexed SQL version was not found for legacy-source provenance.")
    legacy_path = project_relative(root, resolve_project_path(root, str(source_reference.get("legacy_source_path") or "")))
    source_sha256 = str(source_reference.get("source_sha256") or "")
    clean = {
        "contract_version": "legacy_work_source_v1",
        "legacy_source_path": legacy_path,
        "original_file_name": str(source_reference.get("original_file_name") or Path(legacy_path).name),
        "source_sha256": source_sha256,
        "source_sql_fingerprint": str(source_reference.get("source_sql_fingerprint") or ""),
        "migrated_at": str(source_reference.get("migrated_at") or now_iso()),
        "source_removed_after_verified_copy": bool(source_reference.get("source_removed_after_verified_copy")),
    }

    def upsert(rows: Any) -> list[dict[str, Any]]:
        values = [copy.deepcopy(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
        key = (clean["legacy_source_path"], clean["source_sha256"])
        for position, item in enumerate(values):
            if (str(item.get("legacy_source_path") or ""), str(item.get("source_sha256") or "")) == key:
                values[position] = copy.deepcopy(clean)
                return values
        values.append(copy.deepcopy(clean))
        return values

    version["legacy_source_refs"] = upsert(version.get("legacy_source_refs"))
    entry["legacy_source_refs"] = upsert(entry.get("legacy_source_refs"))
    meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
    meta = read_json(meta_path, {})
    meta["legacy_source_refs"] = upsert(meta.get("legacy_source_refs"))
    meta["updated_at"] = now_iso()
    _write_transaction({meta_path: json_text(meta), **_index_files(root, index)})
    return {
        "status": "updated",
        "query_id": entry.get("query_id", ""),
        "version": version.get("version"),
        "path": version.get("path", ""),
        "legacy_source_path": legacy_path,
        "source_removed_after_verified_copy": clean["source_removed_after_verified_copy"],
    }


def finalize_legacy_source_intake(root: Path, reference: dict[str, Any]) -> dict[str, Any]:
    """Mark a legacy source removed only after its indexed SQL copy was verified."""

    root = root.resolve()
    index = load_index(root)
    entry, version = _find_entry(
        index,
        query_id=str(reference.get("query_id") or ""),
        sql_path=str(reference.get("path") or ""),
    )
    if not entry or not isinstance(version, dict):
        raise ValueError("Indexed SQL version was not found for legacy migration finalization.")
    intake = version.get("source_intake") if isinstance(version.get("source_intake"), dict) else {}
    if intake.get("contract_version") != "legacy_work_import_v1":
        raise ValueError("Indexed SQL version is not a legacy-work intake.")
    intake["source_removed_after_verified_copy"] = True
    version["source_intake"] = copy.deepcopy(intake)
    if (
        isinstance(entry.get("source_intake"), dict)
        and entry["source_intake"].get("legacy_source_path") == intake.get("legacy_source_path")
    ):
        entry["source_intake"] = copy.deepcopy(intake)
    meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
    meta = read_json(meta_path, {})
    meta["source_intake"] = copy.deepcopy(intake)
    meta["updated_at"] = now_iso()
    _write_transaction({meta_path: json_text(meta), **_index_files(root, index)})
    return {
        "status": "updated",
        "query_id": entry.get("query_id", ""),
        "version": version.get("version"),
        "path": version.get("path", ""),
        "source_removed_after_verified_copy": True,
    }


def mark_historical_formal_backfill(
    root: Path,
    reference: dict[str, Any],
    formal_artifact_path: str,
) -> dict[str, Any]:
    """Link a retroactive formal snapshot without claiming run evidence existed."""

    root = root.resolve()
    index = load_index(root)
    entry, version = _find_entry(
        index,
        query_id=str(reference.get("query_id") or ""),
        sql_path=str(reference.get("path") or ""),
    )
    if not entry or not isinstance(version, dict):
        raise ValueError("Historical workspace snapshot was not found for promotion backfill.")
    formal_artifact_path = project_relative(root, resolve_project_path(root, formal_artifact_path))
    changed_at = now_iso()
    migration_reason = (
        "Historical formal QUERY lineage backfill; the formal SQL body was preserved and no fresh run-evidence or generation-gate claim was made."
    )
    version.update(
        {
            "status": "promoted",
            "delivery_ready": True,
            "formal_artifact_path": formal_artifact_path,
            "status_history": [{"status": "promoted", "at": changed_at, "reason": migration_reason}],
            "updated_at": changed_at,
        }
    )
    links = entry.setdefault("formal_artifacts", [])
    if formal_artifact_path not in links:
        links.append(formal_artifact_path)
    if int(version.get("version") or 0) == int(entry.get("current_version") or 0):
        entry["status"] = "promoted"
        entry["updated_at"] = changed_at
    meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
    meta = read_json(meta_path, {})
    meta.update(
        {
            "status": "promoted",
            "delivery_ready": True,
            "formal_artifact_path": formal_artifact_path,
            "status_history": copy.deepcopy(version["status_history"]),
            "updated_at": changed_at,
        }
    )
    _write_transaction({meta_path: json_text(meta), **_index_files(root, index)})
    return {
        "status": "updated",
        "query_id": entry.get("query_id", ""),
        "version": version.get("version"),
        "query_status": "promoted",
        "path": version.get("path", ""),
        "meta_path": version.get("meta_path", ""),
        "formal_artifact_path": formal_artifact_path,
        "delivery_ready": True,
        "index_path": INDEX_REL.as_posix(),
        "index_html_path": INDEX_HTML_REL.as_posix(),
    }


def _search_text(entry: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in [
        "query_id",
        "title",
        "purpose",
        "business_question",
        "status",
        "current_path",
        "business_category",
        "analysis_type",
        "usage_class",
        "grain",
        "time_grain",
    ]:
        parts.append(str(entry.get(key) or ""))
    for key in ["source_logs", "tables", "metrics", "dimensions", "filters", "tags", "formal_artifacts"]:
        parts.extend(str(item) for item in entry.get(key, []) or [])
    return " ".join(parts).lower()


def search_queries(
    root: Path,
    *,
    query: str = "",
    status: str = "",
    source_log: str = "",
    metric: str = "",
    tag: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    index = load_index(root.resolve())
    tokens = [item.lower() for item in re.split(r"\s+", str(query or "")) if item.strip()]
    rows: list[dict[str, Any]] = []
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if status and entry.get("status") != status:
            continue
        if source_log and not any(source_log.lower() in str(item).lower() for item in entry.get("source_logs", []) or []):
            continue
        if metric and not any(metric.lower() in str(item).lower() for item in entry.get("metrics", []) or []):
            continue
        if tag and tag not in (entry.get("tags", []) or []):
            continue
        text = _search_text(entry)
        if tokens and not all(token in text for token in tokens):
            continue
        rows.append(copy.deepcopy(entry))
    return sorted(rows, key=lambda item: str(item.get("updated_at") or ""), reverse=True)[: max(1, limit)]


def validate_no_absolute_paths(value: Any, *, key: str = "") -> list[str]:
    problems: list[str] = []
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            problems.extend(validate_no_absolute_paths(child_value, key=str(child_key)))
    elif isinstance(value, list):
        for child in value:
            problems.extend(validate_no_absolute_paths(child, key=key))
    elif key in PATH_KEYS and isinstance(value, str) and value:
        if Path(value).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\") or value.lower().startswith("file://"):
            problems.append(f"{key} must be project-relative: {value}")
        elif ".." in Path(value.replace("\\", "/")).parts:
            problems.append(f"{key} must not escape the project root: {value}")
    return problems


def _format_result(result: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    lines = [f"status: {result.get('status')}"]
    for key in [
        "query_id",
        "version",
        "query_status",
        "change_type",
        "coverage_relation",
        "attachment_id",
        "kind",
        "path",
        "project_relative_path",
        "delivery_file",
        "working_copy_path",
        "index_path",
        "index_html_path",
        "delivery_ready",
        "source_unchanged",
        "query_family_count",
        "changed_entry_count",
        "changed_version_count",
    ]:
        if key in result:
            lines.append(f"{key}: {result.get(key)}")
    if result.get("final_response_requirement"):
        lines.append(f"final_response_requirement: {result.get('final_response_requirement')}")
    if result.get("blockers"):
        lines.append("blockers:")
        lines.extend(f"  - {item}" for item in result.get("blockers", []))
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create the project query workspace and empty indexes")
    init.add_argument("--root", required=True)
    init.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(init, selection_help="Optional route such as [PROJECT_ADMIN] or [QUERY].")

    upgrade = sub.add_parser("upgrade-contract", help="Backfill query-family change metadata without changing SQL")
    upgrade.add_argument("--root", required=True)
    upgrade.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(upgrade, selection_help="Explicit route [PROJECT_ADMIN].")

    intake = sub.add_parser("import", help="Copy external SQL into the project before any modification")
    intake.add_argument("--root", required=True)
    intake.add_argument("--sql-file", required=True)
    intake.add_argument("--title", required=True)
    intake.add_argument("--purpose", required=True)
    intake.add_argument("--business-question", default="")
    intake.add_argument("--tags", default="")
    intake.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(intake, selection_help="Optional route such as 【查询SQL】 or [QUERY].")

    save = sub.add_parser("save", help="Save and index a query before it is delivered for execution")
    save.add_argument("--root", required=True)
    save.add_argument("--sql-file", required=True)
    save.add_argument("--title", required=True)
    save.add_argument("--purpose", required=True)
    save.add_argument("--business-question", default="")
    save.add_argument("--status", choices=["draft", "runnable"], default="runnable")
    save.add_argument("--query-id", help="Existing query id when saving a revised SQL version")
    save.add_argument("--source-kind", choices=["generated", "user_provided"], default="generated")
    save.add_argument("--tags", default="")
    save.add_argument(
        "--usage-class",
        choices=sorted(USAGE_CLASSES),
        default="",
        help="SQL value class; revisions inherit the family value when omitted.",
    )
    save.add_argument(
        "--workspace-role",
        choices=sorted(WORKSPACE_ROLES - {"unknown"}),
        default="query",
        help="Role of this local SQL version; dashboard_delivery requires explicit source lineage.",
    )
    save.add_argument("--source-query-id", default="", help="Exact source query family for dashboard_delivery.")
    save.add_argument("--source-query-version", type=int, default=0, help="Exact source query version for dashboard_delivery.")
    save.add_argument("--revision-note", default="")
    save.add_argument("--change-type", choices=["auto", *sorted(QUERY_CHANGE_TYPES)], default="auto")
    save.add_argument("--coverage-relation", choices=sorted(COVERAGE_RELATIONS), default="")
    save.add_argument("--branch-of", default="", help="Existing query id or indexed SQL path when both query families remain useful")
    save.add_argument(
        "--knowledge-reference-file",
        action="append",
        default=[],
        help="JSON emitted by config_knowledge.py resolve. Only the validated reference is persisted.",
    )
    save.add_argument(
        "--knowledge-usage",
        choices=["auto", "not-used"],
        default="auto",
        help="Declare that active project knowledge was intentionally not used when no resolver receipt applies.",
    )
    save.add_argument(
        "--summary-plan-file",
        default="",
        help="Project-local summary_feasibility_v1 JSON. Required for grouped metric SQL.",
    )
    save.add_argument(
        "--analysis-role",
        choices=["grouped", "overall", "standalone"],
        default="",
        help="Role in a grouped summary plan; dual-query members use grouped or overall.",
    )
    save.add_argument("--no-seed", action="store_true")
    save.add_argument(
        "--rule-policy",
        choices=["auto", "canonical", "temporary-user-override"],
        default="auto",
        help="Use canonical rules unless the request explicitly declares temporary SQL; override mode never changes canonical rules.",
    )
    save.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(save, selection_help="Optional route such as 【查询SQL】 or [QUERY].")

    receipt = sub.add_parser("receipt", help="Verify the exact indexed SQL file before the QUERY response is delivered")
    receipt.add_argument("--root", required=True)
    receipt_identity = receipt.add_mutually_exclusive_group(required=True)
    receipt_identity.add_argument("--query-id")
    receipt_identity.add_argument("--sql-path")
    receipt.add_argument("--version", type=int, default=0, help="Defaults to current when --query-id is used")
    receipt.add_argument("--format", choices=["json", "text"], default="text")

    attach = sub.add_parser("attach-output", help="Copy a result, Excel analysis, comparison, or visualization into one query version")
    attach.add_argument("--root", required=True)
    attach_identity = attach.add_mutually_exclusive_group(required=True)
    attach_identity.add_argument("--query-id")
    attach_identity.add_argument("--sql-path")
    attach.add_argument("--version", type=int, default=0, help="Defaults to current when --query-id is used")
    attach.add_argument("--file", required=True)
    attach.add_argument("--kind", choices=sorted(DERIVED_OUTPUT_KINDS), required=True)
    attach.add_argument("--source-kind", choices=sorted(DERIVED_OUTPUT_SOURCE_KINDS), required=True)
    attach.add_argument("--title", required=True)
    attach.add_argument("--purpose", required=True)
    attach.add_argument("--related-query", action="append", default=[], help="Optional qw-... or qw-...@vNNN dependency")
    attach.add_argument(
        "--source-result-id",
        default="",
        help="Required by the result-visualization workflow; must name a result_evidence attachment on this exact SQL version.",
    )
    attach.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(attach, selection_help="Optional route such as [QUERY] or [SQL_FORMALIZE].")

    mark = sub.add_parser("mark", help="Record run failure or user-confirmed query results")
    mark.add_argument("--root", required=True)
    mark_identity = mark.add_mutually_exclusive_group(required=True)
    mark_identity.add_argument("--query-id")
    mark_identity.add_argument("--sql-path")
    mark.add_argument("--status", choices=["run_failed", "result_confirmed"], required=True)
    mark.add_argument("--reason", required=True)
    mark.add_argument("--result-status", default="")
    mark.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(mark, selection_help="Optional route such as [QUERY] or [SQL_FORMALIZE].")

    discard = sub.add_parser("discard", help="Move an existing workspace query into discarded lifecycle state")
    discard.add_argument("--root", required=True)
    discard_identity = discard.add_mutually_exclusive_group(required=True)
    discard_identity.add_argument("--query-id")
    discard_identity.add_argument("--sql-path")
    discard.add_argument("--reason", required=True)
    discard.add_argument("--result-status", default="obsolete")
    discard.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(discard, selection_help="Optional route such as [QUERY].")

    search = sub.add_parser("search", help="Search query workspace purpose, logs, metrics, filters, and status")
    search.add_argument("--root", required=True)
    search.add_argument("--query", default="")
    search.add_argument("--status", choices=sorted(QUERY_STATUSES), default="")
    search.add_argument("--source-log", default="")
    search.add_argument("--metric", default="")
    search.add_argument("--tag", default="")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--format", choices=["json", "text"], default="text")

    show = sub.add_parser("show", help="Show one query workspace entry")
    show.add_argument("--root", required=True)
    show.add_argument("--query-id", required=True)
    show.add_argument("--format", choices=["json", "text"], default="json")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command in {"init", "upgrade-contract", "import", "save", "attach-output", "mark", "discard"}:
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=command_function_ids("sql_query_workspace.py", args.command),
                purpose=f"sql_query_workspace.py {args.command}",
            )
            require_user_request(args.user_request, purpose=f"sql_query_workspace.py {args.command}")
        root = Path(args.root).resolve()
        if args.command == "init":
            result = ensure_workspace(root)
        elif args.command == "upgrade-contract":
            result = upgrade_change_contract(root)
        elif args.command == "import":
            result = import_external_query(
                root=root,
                source_sql=Path(args.sql_file).resolve(),
                title=args.title,
                purpose=args.purpose,
                business_question=args.business_question,
                tags=[item.strip() for item in args.tags.split(",") if item.strip()],
            )
        elif args.command == "save":
            source = Path(args.sql_file).resolve()
            if not is_project_local(root, source):
                raise ValueError(
                    "External SQL is immutable input. Run `sql_query_workspace.py import` first, then edit only the returned project-local working_copy_path."
                )
            indexed_reference = find_query_reference(root, source, match_fingerprint=False)
            if indexed_reference and indexed_reference.get("sql_fingerprint") != sql_fingerprint(source.read_text(encoding="utf-8-sig")):
                raise ValueError(
                    "An immutable indexed workspace SQL version was modified in place. Restore that version and put the correction in its project-local working copy before saving v002+."
                )
            resolved_query_id, resolved_change_type, resolved_coverage = resolve_change_contract(
                query_id=args.query_id or "",
                source_kind=args.source_kind,
                change_type=args.change_type,
                coverage_relation=args.coverage_relation,
                branch_of=args.branch_of,
                revision_note=args.revision_note,
            )
            parent_rule_application, inheritance_contract = revision_rule_inheritance(
                root,
                query_id=resolved_query_id,
                change_type=resolved_change_type,
                coverage_relation=resolved_coverage,
            )
            gate: dict[str, Any] | None = None
            rule_context: dict[str, Any] | None = None
            gate_mode = "generation"
            if args.rule_policy == "temporary-user-override" or (
                args.rule_policy == "auto" and request_declares_temporary_sql(args.user_request)
            ):
                gate_mode = "temporary"
            if args.status == "runnable":
                gate, rule_context = run_delivery_gate(
                    root,
                    source,
                    args.user_request,
                    mode=gate_mode,
                    lifecycle_stage="temporary_query",
                    parent_rule_application=parent_rule_application or None,
                    inheritance_contract=inheritance_contract,
                )
                if str(gate.get("status") or "") != "ok":
                    result = {
                        "status": "blocked",
                        "blockers": gate.get("blockers", []) or ["generation gate did not pass"],
                        "delivery_ready": False,
                    }
                    print(_format_result(result, args.format), end="")
                    return 1
            summary_plan: dict[str, Any] = {}
            if args.summary_plan_file:
                summary_path = Path(args.summary_plan_file)
                if not summary_path.is_absolute():
                    summary_path = root / summary_path
                summary_path = summary_path.resolve()
                if not is_project_local(root, summary_path) or not summary_path.is_file():
                    raise ValueError("Summary plan file must be an existing project-local JSON file.")
                summary_plan = read_json(summary_path, {})
                if not isinstance(summary_plan, dict) or not summary_plan:
                    raise ValueError("Summary plan file must contain one non-empty JSON object.")
            result = save_query(
                root=root,
                source_sql=source,
                title=args.title,
                purpose=args.purpose,
                business_question=args.business_question,
                status=args.status,
                query_id=args.query_id or "",
                source_kind=args.source_kind,
                tags=[item.strip() for item in args.tags.split(",") if item.strip()],
                revision_note=args.revision_note,
                change_type=args.change_type,
                coverage_relation=args.coverage_relation,
                branch_of=args.branch_of,
                gate=gate,
                rule_context=rule_context,
                gate_mode=gate_mode,
                write_seed=not args.no_seed,
                knowledge_references=load_knowledge_reference_files(
                    root,
                    args.knowledge_reference_file,
                ),
                knowledge_usage_declaration=args.knowledge_usage,
                summary_plan=summary_plan,
                analysis_role=args.analysis_role,
                usage_class=args.usage_class,
                workspace_role=args.workspace_role,
                role_lineage={
                    "source_query_id": args.source_query_id,
                    "source_query_version": args.source_query_version,
                }
                if args.source_query_id or args.source_query_version
                else {},
                user_request=args.user_request,
            )
        elif args.command == "attach-output":
            result = attach_derived_output(
                root=root,
                file_path=Path(args.file).resolve(),
                title=args.title,
                purpose=args.purpose,
                kind=args.kind,
                source_kind=args.source_kind,
                query_id=args.query_id or "",
                sql_path=args.sql_path or "",
                version_number=args.version,
                related_queries=args.related_query,
                source_result_id=args.source_result_id,
            )
        elif args.command == "receipt":
            result = query_delivery_receipt(
                root,
                query_id=args.query_id or "",
                sql_path=args.sql_path or "",
                version_number=args.version,
            )
        elif args.command == "mark":
            result = transition_query(
                root=root,
                query_id=args.query_id or "",
                sql_path=args.sql_path or "",
                status=args.status,
                reason=args.reason,
                result_status=args.result_status,
            )
        elif args.command == "discard":
            result = transition_query(
                root=root,
                query_id=args.query_id or "",
                sql_path=args.sql_path or "",
                status="discarded",
                reason=args.reason,
                result_status=args.result_status,
            )
        elif args.command == "search":
            rows = search_queries(
                root,
                query=args.query,
                status=args.status,
                source_log=args.source_log,
                metric=args.metric,
                tag=args.tag,
                limit=args.limit,
            )
            if args.format == "json":
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows:
                print("No matching query workspace entries.")
            else:
                for item in rows:
                    print(
                        f"{item.get('query_id')} | {item.get('status')} | {item.get('title')} | "
                        f"{item.get('purpose')} | path={item.get('current_path')}"
                    )
            return 0
        else:
            index = load_index(root)
            entry, _ = _find_entry(index, query_id=normalize_query_id(args.query_id))
            if not entry:
                result = {"status": "not_found", "query_id": args.query_id}
                print(_format_result(result, args.format), end="")
                return 1
            result = entry
        print(_format_result(result, args.format), end="")
        return 0 if result.get("status") not in {"blocked", "error"} else 1
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "error", "blockers": [str(exc)]}
        output_format = getattr(args, "format", "text")
        print(_format_result(result, output_format), end="")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
