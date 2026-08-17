#!/usr/bin/env python3
"""Manage immutable project knowledge datasets and project bindings.

Source files and reviewed projections are intake evidence, never query-time
dependencies. The command imports reviewed projections once, versions them by
content, and lets SQL workflows resolve only the version explicitly bound to a
project.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from asset_provenance import build_generation_provenance  # noqa: E402
from capability_registry import command_function_ids  # noqa: E402
from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    normalize_function_selection,
    require_user_function_selection,
    require_user_request,
)
from knowledge_usage import safe_resolution_receipt_path  # noqa: E402
import planning_projection  # noqa: E402
from rule_store import RuleStore  # noqa: E402


CATALOG_SCHEMA = "knowledge_catalog_v1"
SNAPSHOT_SCHEMA = "knowledge_source_snapshot_v1"
DATASET_SCHEMA = "knowledge_dataset_manifest_v1"
CONTRACT_SCHEMA = "knowledge_usage_contract_v1"
BINDINGS_SCHEMA = "knowledge_project_bindings_v1"
REFERENCE_SCHEMA = "knowledge_reference_v1"
RESOLUTION_SCHEMA = "knowledge_resolution_result_v1"
BINDING_IMPACT_SCHEMA = "knowledge_binding_impact_v1"
CONTENT_HASH_CONTRACT = "knowledge_content_v2"
STATIC_IMPORT_SCHEMA = "knowledge_static_import_v1"
CSHARP_CONST_INT_EXTRACTOR = "csharp_const_int_v1"
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
WRITE_ACTION_RE = re.compile(
    r"构造|登记|注册|导入|迁移|绑定|激活|更新|刷新|替换|新增|添加|保存|register|refresh|bind|activate|import",
    re.I,
)
KNOWLEDGE_SUBJECT_RE = re.compile(
    r"资料库|资料投影|资料|配置表|策划表|代码文件|枚举|excel|knowledge|dataset",
    re.I,
)
ALLOWED_USAGE_MODES = {
    "inline_mapping",
    "filter_set",
    "result_enrichment",
    "authoring_reference",
    "materialized_dimension",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json_text(data), encoding="utf-8")
    temporary.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_sha256(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{label} must match {IDENTIFIER_RE.pattern}: {value!r}")
    return normalized


def discover_repo_root(start: Path) -> Path:
    resolved = start.resolve()
    candidates = [resolved, *resolved.parents]
    for candidate in candidates:
        if (candidate / "sql-engineering").is_dir() and (candidate / "sql-projects").is_dir():
            return candidate
    raise ValueError(f"Cannot locate repository root from {start}")


def repo_relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"Persisted knowledge paths must stay inside the repository: {path}") from exc


def resolve_repo_path(repo_root: Path, value: str) -> Path:
    raw = Path(str(value or ""))
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"Knowledge reference must be repository-relative: {value}")
    resolved = (repo_root / raw).resolve()
    repo_relative(repo_root, resolved)
    return resolved


def catalog_path(repo_root: Path) -> Path:
    return repo_root / "knowledge-base" / "catalog.json"


def empty_catalog() -> dict[str, Any]:
    return {
        "schema_version": CATALOG_SCHEMA,
        "updated_at": "",
        "generation_provenance": {},
        "datasets": [],
    }


def load_catalog(repo_root: Path) -> dict[str, Any]:
    path = catalog_path(repo_root)
    if not path.exists():
        return empty_catalog()
    catalog = read_json(path)
    if catalog.get("schema_version") != CATALOG_SCHEMA:
        raise ValueError(f"Unsupported knowledge catalog schema: {catalog.get('schema_version')!r}")
    if not isinstance(catalog.get("datasets"), list):
        raise ValueError("knowledge catalog datasets must be an array")
    return catalog


def load_contract(repo_root: Path, contract_path: Path) -> dict[str, Any]:
    contract_path = contract_path.resolve()
    repo_relative(repo_root, contract_path)
    contract = read_json(contract_path)
    problems = contract_problems(contract)
    if problems:
        raise ValueError("Invalid knowledge usage contract: " + "; ".join(problems))
    return contract


def contract_problems(contract: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if contract.get("schema_version") != CONTRACT_SCHEMA:
        problems.append(f"schema_version must be {CONTRACT_SCHEMA}")
    try:
        validate_identifier(str(contract.get("dataset_id") or ""), "dataset_id")
    except ValueError as exc:
        problems.append(str(exc))
    if not str(contract.get("display_name") or "").strip():
        problems.append("display_name is required")
    if contract.get("status") not in {"draft", "active", "deprecated"}:
        problems.append("status must be draft, active, or deprecated")
    if contract.get("source_kind") not in {"config_excel", "manual_mapping", "external_reference"}:
        problems.append("source_kind is unsupported")
    if not isinstance(contract.get("project_scopes"), list) or not contract.get("project_scopes"):
        problems.append("project_scopes must be a non-empty array")
    projections = contract.get("projections")
    if not isinstance(projections, list) or not projections:
        problems.append("projections must be a non-empty array")
        return problems
    seen: set[str] = set()
    for item in projections:
        if not isinstance(item, dict):
            problems.append("each projection contract must be an object")
            continue
        try:
            projection_id = validate_identifier(str(item.get("projection_id") or ""), "projection_id")
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if projection_id in seen:
            problems.append(f"duplicate projection_id: {projection_id}")
        seen.add(projection_id)
        if not isinstance(item.get("primary_key"), list) or not item.get("primary_key"):
            problems.append(f"{projection_id}.primary_key must be a non-empty array")
        for field in ["required_fields", "default_fields", "allowed_fields", "allowed_usages", "forbidden_usages"]:
            if not isinstance(item.get(field), list):
                problems.append(f"{projection_id}.{field} must be an array")
        allowed_fields = set(item.get("allowed_fields") or [])
        declared_usage_fields = (
            set(item.get("primary_key") or [])
            | set(item.get("required_fields") or [])
            | set(item.get("default_fields") or [])
        )
        undeclared_fields = sorted(declared_usage_fields - allowed_fields)
        if undeclared_fields:
            problems.append(
                f"{projection_id} key/required/default fields must be allowed: {', '.join(undeclared_fields)}"
            )
        modes = item.get("allowed_usage_modes")
        if not isinstance(modes, list) or not modes:
            problems.append(f"{projection_id}.allowed_usage_modes must be a non-empty array")
        elif unknown := sorted(set(modes) - ALLOWED_USAGE_MODES):
            problems.append(f"{projection_id} has unsupported usage modes: {', '.join(unknown)}")
        if not str(item.get("source_output") or "").strip():
            problems.append(f"{projection_id}.source_output is required")
    governance = contract.get("governance")
    if not isinstance(governance, dict):
        problems.append("governance must be an object")
    else:
        if governance.get("runtime_excel_dependency") != "forbidden":
            problems.append("governance.runtime_excel_dependency must be forbidden")
        if governance.get("activation_policy") != "explicit_project_binding":
            problems.append("governance.activation_policy must be explicit_project_binding")
    return problems


def projection_contract(contract: dict[str, Any], projection_id: str) -> dict[str, Any]:
    normalized = validate_identifier(projection_id, "projection_id")
    for item in contract.get("projections", []):
        if str(item.get("projection_id") or "").lower() == normalized:
            return item
    raise ValueError(f"Projection `{normalized}` is not declared by contract `{contract.get('dataset_id')}`")


def require_knowledge_write(args: argparse.Namespace, purpose: str) -> None:
    request = str(args.user_request or "").strip()
    require_user_request(request, purpose=purpose)
    selection = normalize_function_selection(args.function_selection)
    if not selection or selection.function_id != "KNOWLEDGE":
        raise FunctionGateError(
            "BLOCKED\n\nblockers:\n"
            "  - Durable knowledge writes require explicit `--function-selection KNOWLEDGE`.\n"
            "  - QUERY/REVIEW/FORMALIZE may resolve bound datasets but cannot register, refresh, or bind them.\n"
        )
    require_user_function_selection(
        args.function_selection,
        user_request=request,
        allowed_ids=command_function_ids("config_knowledge.py", args.command),
        purpose=purpose,
    )
    if not KNOWLEDGE_SUBJECT_RE.search(request) or not WRITE_ACTION_RE.search(request):
        raise FunctionGateError(
            "BLOCKED\n\nblockers:\n"
            "  - The verbatim request does not explicitly ask to persist or bind a knowledge/config-table asset.\n\n"
            "needed_from_user:\n"
            "  - Ask to register, refresh, migrate, or bind a named config-table/knowledge dataset.\n"
        )


def csharp_class_body(source: str, class_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", class_name):
        raise ValueError(f"Invalid C# class name: {class_name!r}")
    match = re.search(rf"\bclass\s+{re.escape(class_name)}\b[^{{]*{{", source)
    if not match:
        raise ValueError(f"C# class not found: {class_name}")
    start = match.end()
    depth = 1
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index]
    raise ValueError(f"C# class has no matching closing brace: {class_name}")


def extract_csharp_const_int(
    source_file: Path,
    output_file: Path,
    extractor: dict[str, Any],
) -> dict[str, Any]:
    class_name = str(extractor.get("class_name") or "").strip()
    name_field = validate_identifier(str(extractor.get("name_field") or ""), "name_field")
    value_field = validate_identifier(str(extractor.get("value_field") or ""), "value_field")
    body = csharp_class_body(source_file.read_text(encoding="utf-8-sig"), class_name)
    matches = re.findall(
        r"^\s*public\s+const\s+int\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+)\s*;\s*$",
        body,
        re.MULTILINE,
    )
    if not matches:
        raise ValueError(f"No public const int values found in C# class: {class_name}")
    names = [name for name, _ in matches]
    values = [int(value) for _, value in matches]
    if len(names) != len(set(names)):
        raise ValueError(f"C# constant names are not unique in class: {class_name}")
    if len(values) != len(set(values)):
        raise ValueError(f"C# constant values are not unique in class: {class_name}")
    expected = int(extractor.get("expected_row_count") or 0)
    if expected and len(matches) != expected:
        raise ValueError(f"C# constant row count mismatch: expected {expected}, found {len(matches)}")
    if extractor.get("require_contiguous_values") is True:
        ordered = sorted(values)
        if ordered != list(range(ordered[0], ordered[-1] + 1)):
            raise ValueError("C# constant values are not contiguous")
    rows = [
        {value_field: value, name_field: name}
        for name, value in matches
    ]
    write_csv(output_file, [value_field, name_field], rows)
    return {
        "row_count": len(rows),
        "min_value": min(values),
        "max_value": max(values),
    }


def static_import_inputs(
    *,
    repo_root: Path,
    import_spec_path: Path,
    contract: dict[str, Any],
    run_adapter: bool,
) -> tuple[Path, dict[str, Path], dict[str, Any]]:
    spec = read_json(import_spec_path)
    if spec.get("schema_version") != STATIC_IMPORT_SCHEMA:
        raise ValueError(
            f"Static knowledge import spec schema must be {STATIC_IMPORT_SCHEMA}: {import_spec_path}"
        )
    review_status = str(spec.get("status") or "").strip().lower()
    if review_status != "active":
        raise ValueError(
            f"Static knowledge import spec must be active: {import_spec_path} has status "
            f"{review_status or '<missing>'!r}"
        )
    source_kind = str(contract.get("source_kind") or "")
    if spec.get("source_kind") != source_kind:
        raise ValueError("Static import spec source_kind must match the usage contract")
    source_file = resolve_repo_path(repo_root, str(spec.get("source_file") or ""))
    if not source_file.is_file():
        raise ValueError(f"Static knowledge source does not exist: {source_file}")

    declared = spec.get("projections")
    if not isinstance(declared, list):
        raise ValueError("Static import spec projections must be an array")
    projection_rows = {
        str(item.get("projection_id") or ""): item
        for item in declared
        if isinstance(item, dict)
    }
    expected = {str(item["projection_id"]) for item in contract.get("projections", [])}
    if set(projection_rows) != expected:
        raise ValueError(
            "Static import projections must exactly match the usage contract: "
            f"expected {sorted(expected)}, found {sorted(projection_rows)}"
        )
    projections: dict[str, Path] = {}
    for projection_id in sorted(expected):
        source_path = resolve_repo_path(
            repo_root,
            str(projection_rows[projection_id].get("source_file") or ""),
        )
        projections[projection_id] = source_path
    extractor = spec.get("extractor") if isinstance(spec.get("extractor"), dict) else None
    if run_adapter:
        if not extractor or extractor.get("extractor_id") != CSHARP_CONST_INT_EXTRACTOR:
            raise ValueError(
                "--run-adapter for static sources requires extractor_id=csharp_const_int_v1"
            )
        projection_id = validate_identifier(
            str(extractor.get("projection_id") or ""),
            "extractor.projection_id",
        )
        if set(projections) != {projection_id}:
            raise ValueError("C# const extraction requires exactly its declared projection")
        extract_csharp_const_int(source_file, projections[projection_id], extractor)
    for projection_id, source_path in projections.items():
        if not source_path.is_file():
            raise ValueError(f"Static projection source does not exist: {projection_id} -> {source_path}")
    adapter = {
        "adapter_id": str((extractor or {}).get("extractor_id") or "reviewed_static_mapping_v1"),
        "spec_path": repo_relative(repo_root, import_spec_path),
        "spec_sha256": file_sha256(import_spec_path),
        "review_status": review_status,
        "run_mode": "executed" if run_adapter else "reused_reviewed_output",
    }
    return source_file, projections, adapter


def adapter_inputs(
    *,
    repo_root: Path,
    adapter_spec_path: Path,
    contract: dict[str, Any],
    run_adapter: bool,
) -> tuple[Path, dict[str, Path], dict[str, Any]]:
    adapter_spec_path = adapter_spec_path.resolve()
    source_kind = str(contract.get("source_kind") or "")
    if source_kind in {"manual_mapping", "external_reference"}:
        return static_import_inputs(
            repo_root=repo_root,
            import_spec_path=adapter_spec_path,
            contract=contract,
            run_adapter=run_adapter,
        )
    if source_kind != "config_excel":
        raise ValueError(f"Unsupported knowledge source kind: {source_kind!r}")
    spec = read_json(adapter_spec_path)
    review_status = str(spec.get("status") or "").strip().lower()
    if review_status != "active":
        raise ValueError(
            f"Config-table export spec must be active before knowledge registration: "
            f"{adapter_spec_path} has status {review_status or '<missing>'!r}"
        )
    if spec.get("schema_version") != planning_projection.SPEC_SCHEMA:
        raise ValueError(
            f"Config-table projection spec must use {planning_projection.SPEC_SCHEMA}: {adapter_spec_path}"
        )
    output_root = repo_root / ".local" / "planning-projections" / str(contract["dataset_id"])
    source_file, source_reference = planning_projection.resolve_source(repo_root, spec)
    if run_adapter:
        if output_root.exists():
            shutil.rmtree(output_root)
        result = planning_projection.export_from_spec(
            repo_root=repo_root,
            spec_path=adapter_spec_path,
            output_dir=output_root,
        )
        source_reference = result["source_reference"]

    projections: dict[str, Path] = {}
    for item in contract.get("projections", []):
        projection_id = str(item["projection_id"])
        source_output = str(item["source_output"])
        source_path = (output_root / source_output).resolve()
        if not source_path.is_file():
            raise ValueError(f"Projection source does not exist: {projection_id} -> {source_path}")
        projections[projection_id] = source_path
    adapter = {
        "adapter_id": "planning_projection_spec_v1",
        "spec_path": repo_relative(repo_root, adapter_spec_path),
        "spec_sha256": file_sha256(adapter_spec_path),
        "review_status": review_status,
        "run_mode": "executed" if run_adapter else "reused_reviewed_output",
        "planning_source_reference": source_reference,
    }
    return source_file, projections, adapter


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [str(item or "").strip() for item in (reader.fieldnames or [])]
        if not fields or any(not item for item in fields):
            raise ValueError(f"Projection requires non-empty unique CSV headers: {path}")
        if len(fields) != len(set(fields)):
            raise ValueError(f"Projection has duplicate CSV headers: {path}")
        rows = [dict(row) for row in reader]
    return fields, rows


def primary_key_value(row: dict[str, str], primary_key: list[str]) -> str:
    return "\x1f".join(str(row.get(field) or "").strip() for field in primary_key)


def inspect_projection(path: Path, contract: dict[str, Any]) -> dict[str, Any]:
    fields, rows = read_csv_rows(path)
    required = [str(item) for item in contract.get("required_fields", [])]
    missing = sorted(set(required) - set(fields))
    if missing:
        raise ValueError(f"{contract['projection_id']} is missing required fields: {', '.join(missing)}")
    primary_key = [str(item) for item in contract.get("primary_key", [])]
    missing_keys = sorted(set(primary_key) - set(fields))
    if missing_keys:
        raise ValueError(f"{contract['projection_id']} is missing primary key fields: {', '.join(missing_keys)}")
    seen: set[str] = set()
    duplicate_keys: list[str] = []
    empty_keys: list[int] = []
    for index, row in enumerate(rows, start=2):
        key = primary_key_value(row, primary_key)
        if not key or any(not str(row.get(field) or "").strip() for field in primary_key):
            empty_keys.append(index)
        elif key in seen:
            duplicate_keys.append(key)
        else:
            seen.add(key)
    if empty_keys:
        raise ValueError(f"{contract['projection_id']} has empty primary keys at CSV rows {empty_keys[:8]}")
    if duplicate_keys:
        raise ValueError(f"{contract['projection_id']} has duplicate primary keys: {duplicate_keys[:8]}")
    columns = []
    for field in fields:
        values = [str(row.get(field) or "") for row in rows]
        columns.append(
            {
                "name": field,
                "data_type": "string",
                "non_empty_count": sum(bool(value.strip()) for value in values),
                "empty_count": sum(not value.strip() for value in values),
                "distinct_count": len(set(values)),
            }
        )
    return {
        "fields": fields,
        "rows": rows,
        "row_count": len(rows),
        "column_count": len(fields),
        "primary_key": primary_key,
        "columns": columns,
    }


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def snapshot_category(source_kind: str) -> str:
    return {
        "config_excel": "config_tables",
        "manual_mapping": "manual_mappings",
        "external_reference": "external_references",
    }.get(source_kind, "other")


def source_snapshot(
    *,
    repo_root: Path,
    dataset_id: str,
    source_file: Path,
    source_kind: str,
    audit: dict[str, Any],
) -> dict[str, Any]:
    source_hash = file_sha256(source_file)
    snapshot_id = f"kss-{source_hash[:12]}"
    snapshot_dir = (
        repo_root
        / "knowledge-base"
        / "source_snapshots"
        / snapshot_category(source_kind)
        / dataset_id
        / snapshot_id
    )
    original_path = snapshot_dir / f"original{source_file.suffix.lower()}"
    manifest_path = snapshot_dir / "snapshot.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        stored = resolve_repo_path(repo_root, str(manifest.get("stored_file") or ""))
        if file_sha256(stored) != source_hash:
            raise ValueError(f"Immutable source snapshot hash mismatch: {stored}")
        return manifest
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source_file, original_path)
    imported_from = ""
    try:
        imported_from = repo_relative(repo_root, source_file)
    except ValueError:
        imported_from = source_file.name
    manifest = {
        "schema_version": SNAPSHOT_SCHEMA,
        "source_id": dataset_id,
        "snapshot_id": snapshot_id,
        "source_type": source_kind,
        "original_file_name": source_file.name,
        "stored_file": repo_relative(repo_root, original_path),
        "source_sha256": source_hash,
        "captured_at": now_iso(),
        "generation_provenance": build_generation_provenance(
            generator_script="config_knowledge.py",
            workflow="knowledge_source_snapshot",
            artifact_kind="KNOWLEDGE_SOURCE_SNAPSHOT",
            source=source_kind,
        ),
        "provenance": {
            "imported_from": imported_from,
            "immutable": True,
            "audit": audit,
        },
    }
    write_json(manifest_path, manifest)
    return manifest


def projection_diff(
    old_path: Path | None,
    new_path: Path,
    primary_key: list[str],
) -> dict[str, Any]:
    new_fields, new_rows = read_csv_rows(new_path)
    if old_path is None or not old_path.exists():
        return {
            "status": "baseline",
            "old_row_count": 0,
            "new_row_count": len(new_rows),
            "added_key_count": len(new_rows),
            "removed_key_count": 0,
            "changed_key_count": 0,
            "added_columns": new_fields,
            "removed_columns": [],
            "sample_added_keys": [primary_key_value(row, primary_key) for row in new_rows[:10]],
            "sample_removed_keys": [],
            "sample_changed_keys": [],
        }
    old_fields, old_rows = read_csv_rows(old_path)
    old_index = {primary_key_value(row, primary_key): row for row in old_rows}
    new_index = {primary_key_value(row, primary_key): row for row in new_rows}
    added = sorted(set(new_index) - set(old_index))
    removed = sorted(set(old_index) - set(new_index))
    changed = sorted(key for key in set(old_index) & set(new_index) if old_index[key] != new_index[key])
    return {
        "status": "changed" if added or removed or changed or old_fields != new_fields else "unchanged",
        "old_row_count": len(old_rows),
        "new_row_count": len(new_rows),
        "added_key_count": len(added),
        "removed_key_count": len(removed),
        "changed_key_count": len(changed),
        "added_columns": [item for item in new_fields if item not in old_fields],
        "removed_columns": [item for item in old_fields if item not in new_fields],
        "sample_added_keys": added[:10],
        "sample_removed_keys": removed[:10],
        "sample_changed_keys": changed[:10],
    }


def latest_dataset_entry(catalog: dict[str, Any], dataset_id: str) -> dict[str, Any] | None:
    return next(
        (item for item in catalog.get("datasets", []) if str(item.get("dataset_id") or "") == dataset_id),
        None,
    )


def dataset_version_manifest(repo_root: Path, entry: dict[str, Any], version: str) -> dict[str, Any]:
    for item in entry.get("versions", []):
        if str(item.get("version") or "") == version:
            return read_json(resolve_repo_path(repo_root, str(item["manifest_path"])))
    raise ValueError(f"Dataset version not found: {entry.get('dataset_id')}@{version}")


def previous_projection_path(
    *,
    repo_root: Path,
    catalog_entry: dict[str, Any] | None,
    projection_id: str,
) -> Path | None:
    if not catalog_entry or not catalog_entry.get("latest_version"):
        return None
    previous = dataset_version_manifest(repo_root, catalog_entry, str(catalog_entry["latest_version"]))
    for item in previous.get("projections", []):
        if item.get("projection_id") == projection_id:
            return resolve_repo_path(repo_root, str(item["data_path"]))
    return None


def bound_projects(repo_root: Path, dataset_id: str, version: str) -> list[str]:
    projects: list[str] = []
    projects_root = repo_root / "sql-projects"
    for path in sorted(projects_root.glob("*/knowledge/bindings.json")):
        try:
            payload = read_json(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if any(
            item.get("dataset_id") == dataset_id
            and item.get("dataset_version") == version
            and item.get("state") == "active"
            for item in payload.get("bindings", [])
            if isinstance(item, dict)
        ):
            projects.append(str(payload.get("project_id") or path.parents[1].name))
    return projects


def register_dataset(args: argparse.Namespace) -> dict[str, Any]:
    require_knowledge_write(args, f"knowledge dataset {args.command}")
    repo_root = discover_repo_root(Path(args.repo_root))
    dataset_id = validate_identifier(args.dataset_id, "dataset_id")
    contract_path = Path(args.contract)
    if not contract_path.is_absolute():
        contract_path = repo_root / contract_path
    contract = load_contract(repo_root, contract_path)
    if contract["dataset_id"] != dataset_id:
        raise ValueError("--dataset-id must match contract.dataset_id")
    adapter_spec_path = Path(args.adapter_spec)
    if not adapter_spec_path.is_absolute():
        adapter_spec_path = repo_root / adapter_spec_path
    source_file, projection_sources, adapter = adapter_inputs(
        repo_root=repo_root,
        adapter_spec_path=adapter_spec_path,
        contract=contract,
        run_adapter=bool(args.run_adapter),
    )
    request = str(args.user_request or "").strip()
    audit = {
        "function_id": "KNOWLEDGE",
        "user_request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "recorded_at": now_iso(),
    }
    snapshot = source_snapshot(
        repo_root=repo_root,
        dataset_id=dataset_id,
        source_file=source_file,
        source_kind=str(contract.get("source_kind") or ""),
        audit=audit,
    )
    contract_hash = file_sha256(contract_path)
    inspections: dict[str, dict[str, Any]] = {}
    projection_hashes: list[dict[str, str]] = []
    for item in contract["projections"]:
        projection_id = str(item["projection_id"])
        inspection = inspect_projection(projection_sources[projection_id], item)
        inspections[projection_id] = inspection
        projection_hashes.append(
            {"projection_id": projection_id, "sha256": file_sha256(projection_sources[projection_id])}
        )
    content_hash = json_sha256(
        {
            "content_hash_contract": CONTENT_HASH_CONTRACT,
            "dataset_id": dataset_id,
            "source_sha256": snapshot["source_sha256"],
            "contract_sha256": contract_hash,
            "extraction_spec_sha256": adapter["spec_sha256"],
            "projections": projection_hashes,
        }
    )
    version = f"kdv-{content_hash[:12]}"
    catalog = load_catalog(repo_root)
    existing_entry = latest_dataset_entry(catalog, dataset_id)
    if args.command == "refresh" and not existing_entry:
        raise ValueError(f"Cannot refresh unregistered dataset: {dataset_id}")
    version_dir = repo_root / "knowledge-base" / "datasets" / dataset_id / version
    manifest_path = version_dir / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        validation = validate_dataset_manifest(repo_root, manifest)
        if validation["problems"]:
            raise ValueError(
                "Cannot reuse invalid immutable dataset version: "
                + "; ".join(validation["problems"])
            )
        reused = True
    else:
        reused = False
        knowledge_root = repo_root / "knowledge-base"
        knowledge_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{dataset_id}-", dir=knowledge_root) as temp_name:
            staging = Path(temp_name)
            shutil.copy2(adapter_spec_path, staging / "extraction_spec.json")
            shutil.copy2(contract_path, staging / "usage_contract.json")
            projection_manifests: list[dict[str, Any]] = []
            diff_rows: list[dict[str, Any]] = []
            for item in contract["projections"]:
                projection_id = str(item["projection_id"])
                source = projection_sources[projection_id]
                inspection = inspections[projection_id]
                target_dir = staging / "projections" / projection_id
                target_dir.mkdir(parents=True, exist_ok=True)
                data_path = target_dir / "data.csv"
                shutil.copy2(source, data_path)
                schema_path = target_dir / "schema.json"
                profile_path = target_dir / "profile.json"
                preview_path = target_dir / "preview.csv"
                write_json(
                    schema_path,
                    {
                        "schema_version": "knowledge_projection_schema_v1",
                        "dataset_id": dataset_id,
                        "dataset_version": version,
                        "projection_id": projection_id,
                        "primary_key": inspection["primary_key"],
                        "columns": [{"name": field, "data_type": "string"} for field in inspection["fields"]],
                    },
                )
                write_json(
                    profile_path,
                    {
                        "schema_version": "knowledge_projection_profile_v1",
                        "dataset_id": dataset_id,
                        "dataset_version": version,
                        "projection_id": projection_id,
                        "row_count": inspection["row_count"],
                        "column_count": inspection["column_count"],
                        "primary_key": inspection["primary_key"],
                        "columns": inspection["columns"],
                    },
                )
                write_csv(preview_path, inspection["fields"], inspection["rows"][:20])
                old_path = previous_projection_path(
                    repo_root=repo_root,
                    catalog_entry=existing_entry,
                    projection_id=projection_id,
                )
                diff = projection_diff(old_path, data_path, inspection["primary_key"])
                diff_rows.append({"projection_id": projection_id, **diff})
                final_projection_root = version_dir / "projections" / projection_id
                projection_manifests.append(
                    {
                        "projection_id": projection_id,
                        "description": str(item.get("description") or ""),
                        "format": "csv",
                        "data_path": repo_relative(repo_root, final_projection_root / "data.csv"),
                        "schema_path": repo_relative(repo_root, final_projection_root / "schema.json"),
                        "profile_path": repo_relative(repo_root, final_projection_root / "profile.json"),
                        "preview_path": repo_relative(repo_root, final_projection_root / "preview.csv"),
                        "sha256": file_sha256(data_path),
                        "schema_sha256": file_sha256(schema_path),
                        "profile_sha256": file_sha256(profile_path),
                        "preview_sha256": file_sha256(preview_path),
                        "row_count": inspection["row_count"],
                        "column_count": inspection["column_count"],
                        "primary_key": inspection["primary_key"],
                    }
                )
            diff_path = staging / "diff.json"
            write_json(
                diff_path,
                {
                    "schema_version": "knowledge_dataset_diff_v1",
                    "dataset_id": dataset_id,
                    "from_version": str((existing_entry or {}).get("latest_version") or ""),
                    "to_version": version,
                    "projections": diff_rows,
                },
            )
            manifest = {
                "schema_version": DATASET_SCHEMA,
                "dataset_id": dataset_id,
                "display_name": contract["display_name"],
                "version": version,
                "content_hash": content_hash,
                "content_hash_contract": CONTENT_HASH_CONTRACT,
                "build_status": "validated",
                "source_snapshot": repo_relative(
                    repo_root,
                    repo_root
                    / "knowledge-base"
                    / "source_snapshots"
                    / snapshot_category(str(contract.get("source_kind") or ""))
                    / dataset_id
                    / snapshot["snapshot_id"]
                    / "snapshot.json",
                ),
                "contract_path": repo_relative(repo_root, version_dir / "usage_contract.json"),
                "contract_source_path": repo_relative(repo_root, contract_path),
                "contract_version": str(contract.get("contract_version") or ""),
                "contract_sha256": contract_hash,
                "adapter": {
                    **adapter,
                    "spec_snapshot_path": repo_relative(repo_root, version_dir / "extraction_spec.json"),
                },
                "projections": projection_manifests,
                "diff_path": repo_relative(repo_root, version_dir / "diff.json"),
                "built_at": now_iso(),
                "generation": build_generation_provenance(
                    generator_script="config_knowledge.py",
                    workflow="config_knowledge_register",
                    artifact_kind="KNOWLEDGE_DATASET",
                    source=str(contract.get("source_kind") or ""),
                ),
                "audit": audit,
            }
            write_json(staging / "manifest.json", manifest)
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), str(version_dir))

    if existing_entry is None:
        existing_entry = {
            "dataset_id": dataset_id,
            "display_name": contract["display_name"],
            "contract_path": repo_relative(repo_root, contract_path),
            "latest_version": version,
            "versions": [],
        }
        catalog["datasets"].append(existing_entry)
    existing_entry["display_name"] = contract["display_name"]
    existing_entry["contract_path"] = repo_relative(repo_root, contract_path)
    existing_entry["latest_version"] = version
    versions = existing_entry.setdefault("versions", [])
    if not any(str(item.get("version") or "") == version for item in versions):
        versions.append(
            {
                "version": version,
                "content_hash": content_hash,
                "manifest_path": repo_relative(repo_root, manifest_path),
                "source_snapshot_id": snapshot["snapshot_id"],
                "registered_at": now_iso(),
            }
        )
    catalog["datasets"] = sorted(catalog["datasets"], key=lambda item: str(item.get("dataset_id") or ""))
    catalog["updated_at"] = now_iso()
    catalog["generation_provenance"] = build_generation_provenance(
        generator_script="config_knowledge.py",
        workflow="knowledge_catalog_update",
        artifact_kind="KNOWLEDGE_CATALOG",
        source="registered_datasets",
    )
    write_json(catalog_path(repo_root), catalog)
    active_projects = bound_projects(repo_root, dataset_id, version)
    return {
        "status": "reused" if reused else "registered",
        "schema_version": DATASET_SCHEMA,
        "dataset_id": dataset_id,
        "version": version,
        "content_hash": content_hash,
        "manifest_path": repo_relative(repo_root, manifest_path),
        "source_snapshot": snapshot,
        "projection_count": len(manifest.get("projections", [])),
        "bound_projects": active_projects,
        "next_step": (
            f"Already bound to: {', '.join(active_projects)}."
            if active_projects
            else f"Bind {dataset_id}@{version} to a project after reviewing diff.json."
        ),
    }


def binding_file(project_root: Path) -> Path:
    return project_root / "knowledge" / "bindings.json"


def load_bindings(project_root: Path) -> dict[str, Any]:
    path = binding_file(project_root)
    project_config = read_json(project_root / "project_config.json")
    project_id = str(project_config.get("project_id") or project_root.name)
    if not path.exists():
        return {
            "schema_version": BINDINGS_SCHEMA,
            "project_id": project_id,
            "updated_at": "",
            "bindings": [],
        }
    bindings = read_json(path)
    if bindings.get("schema_version") != BINDINGS_SCHEMA:
        raise ValueError(f"Unsupported project knowledge bindings schema: {bindings.get('schema_version')!r}")
    if bindings.get("project_id") != project_id:
        raise ValueError("knowledge bindings project_id does not match project_config")
    if not isinstance(bindings.get("bindings"), list):
        raise ValueError("knowledge bindings must be an array")
    return bindings


def _manifest_projection(manifest: dict[str, Any], projection_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in manifest.get("projections", []) or []
            if isinstance(item, dict) and item.get("projection_id") == projection_id
        ),
        None,
    )


def _manifest_projection_fields(repo_root: Path, projection: dict[str, Any]) -> list[str]:
    schema_path = resolve_repo_path(repo_root, str(projection.get("schema_path") or ""))
    schema = read_json(schema_path)
    return [
        str(item.get("name") or "")
        for item in schema.get("columns", []) or []
        if isinstance(item, dict) and item.get("name")
    ]


def classify_binding_change(
    previous_manifest: dict[str, Any] | None,
    target_manifest: dict[str, Any],
) -> str:
    if not previous_manifest:
        return "initial"
    if previous_manifest.get("contract_sha256") != target_manifest.get("contract_sha256"):
        return "contract_changed"
    previous_projections = {
        str(item.get("projection_id") or ""): str(item.get("sha256") or "")
        for item in previous_manifest.get("projections", []) or []
        if isinstance(item, dict)
    }
    target_projections = {
        str(item.get("projection_id") or ""): str(item.get("sha256") or "")
        for item in target_manifest.get("projections", []) or []
        if isinstance(item, dict)
    }
    if previous_projections != target_projections:
        return "projection_changed"
    return "provenance_only"


def _current_rule_dependencies(project_root: Path, dataset_id: str) -> list[dict[str, Any]]:
    store = RuleStore(project_root)
    if not store.exists:
        return []
    dependencies: list[dict[str, Any]] = []
    for rule in store.load_current():
        structured = (
            rule.get("structured_definition")
            if isinstance(rule.get("structured_definition"), dict)
            else {}
        )
        for dependency in structured.get("knowledge_dependencies", []) or []:
            if not isinstance(dependency, dict) or dependency.get("dataset_id") != dataset_id:
                continue
            dependencies.append(
                {
                    "concept_key": str(rule.get("concept_key") or ""),
                    "rule_id": str(rule.get("rule_id") or ""),
                    "projection_id": str(dependency.get("projection_id") or ""),
                    "semantic_role": str(dependency.get("semantic_role") or ""),
                    "fields": [str(item) for item in dependency.get("fields", []) or []],
                    "required": bool(dependency.get("required")),
                }
            )
    return dependencies


def build_binding_impact(
    *,
    repo_root: Path,
    project_root: Path,
    dataset_id: str,
    previous_manifest: dict[str, Any] | None,
    target_manifest: dict[str, Any],
) -> dict[str, Any]:
    dependencies = _current_rule_dependencies(project_root, dataset_id)
    target_contract = load_contract(
        repo_root,
        resolve_repo_path(repo_root, str(target_manifest["contract_path"])),
    )
    previous_contract = None
    if previous_manifest:
        previous_contract = load_contract(
            repo_root,
            resolve_repo_path(repo_root, str(previous_manifest["contract_path"])),
        )
    blockers: list[str] = []
    for dependency in dependencies:
        if not dependency["required"]:
            continue
        projection_id = dependency["projection_id"]
        projection = _manifest_projection(target_manifest, projection_id)
        if not projection:
            blockers.append(
                f"{dependency['concept_key']} requires missing projection {projection_id}"
            )
            continue
        available_fields = set(_manifest_projection_fields(repo_root, projection))
        missing_fields = sorted(set(dependency["fields"]) - available_fields)
        if missing_fields:
            blockers.append(
                f"{dependency['concept_key']} requires missing fields in {projection_id}: "
                + ", ".join(missing_fields)
            )
        try:
            target_projection_contract = projection_contract(target_contract, projection_id)
        except ValueError as exc:
            blockers.append(str(exc))
            continue
        disallowed_fields = sorted(
            set(dependency["fields"])
            - set(target_projection_contract.get("allowed_fields", []) or [])
        )
        if disallowed_fields:
            blockers.append(
                f"{dependency['concept_key']} fields are no longer allowed in {projection_id}: "
                + ", ".join(disallowed_fields)
            )
        if "authoring_reference" not in (
            target_projection_contract.get("allowed_usage_modes", []) or []
        ):
            blockers.append(
                f"{dependency['concept_key']} requires authoring_reference for {projection_id}"
            )
        if previous_contract:
            try:
                previous_projection_contract = projection_contract(
                    previous_contract,
                    projection_id,
                )
            except ValueError:
                previous_projection_contract = None
            if previous_projection_contract:
                if previous_projection_contract.get("primary_key") != target_projection_contract.get("primary_key"):
                    blockers.append(
                        f"{dependency['concept_key']} primary key changed for {projection_id}"
                    )
                previous_roles = previous_projection_contract.get("field_roles", {}) or {}
                target_roles = target_projection_contract.get("field_roles", {}) or {}
                changed_roles = [
                    field
                    for field in dependency["fields"]
                    if previous_roles.get(field) != target_roles.get(field)
                ]
                if changed_roles:
                    blockers.append(
                        f"{dependency['concept_key']} field semantics changed in {projection_id}: "
                        + ", ".join(changed_roles)
                    )
    return {
        "schema_version": BINDING_IMPACT_SCHEMA,
        "change_class": classify_binding_change(previous_manifest, target_manifest),
        "compatible": not blockers,
        "rule_version_required": False,
        "previous_version": str((previous_manifest or {}).get("version") or ""),
        "target_version": str(target_manifest.get("version") or ""),
        "affected_rules": dependencies,
        "blockers": blockers,
        "checked_at": now_iso(),
    }


def bind_dataset(args: argparse.Namespace) -> dict[str, Any]:
    require_knowledge_write(args, "project knowledge binding")
    project_root = Path(args.root).resolve()
    repo_root = discover_repo_root(project_root)
    catalog = load_catalog(repo_root)
    dataset_id = validate_identifier(args.dataset_id, "dataset_id")
    entry = latest_dataset_entry(catalog, dataset_id)
    if not entry:
        raise ValueError(f"Dataset is not registered: {dataset_id}")
    version = str(args.version or entry.get("latest_version") or "")
    manifest = dataset_version_manifest(repo_root, entry, version)
    contract_path = resolve_repo_path(repo_root, str(manifest["contract_path"]))
    contract = load_contract(repo_root, contract_path)
    if contract.get("status") != "active":
        raise ValueError(f"Contract `{dataset_id}` must be active before project binding")
    project_config = read_json(project_root / "project_config.json")
    project_id = str(project_config.get("project_id") or project_root.name)
    if project_id not in contract.get("project_scopes", []):
        raise ValueError(f"Contract `{dataset_id}` is not approved for project `{project_id}`")
    validation = validate_dataset_manifest(repo_root, manifest)
    if validation["problems"]:
        raise ValueError("Cannot bind invalid dataset: " + "; ".join(validation["problems"]))
    bindings = load_bindings(project_root)
    rows = bindings.setdefault("bindings", [])
    prior = next((item for item in rows if item.get("dataset_id") == dataset_id), None)
    previous_manifest = None
    if prior and prior.get("dataset_version"):
        previous_manifest = dataset_version_manifest(
            repo_root,
            entry,
            str(prior["dataset_version"]),
        )
    binding_impact = build_binding_impact(
        repo_root=repo_root,
        project_root=project_root,
        dataset_id=dataset_id,
        previous_manifest=previous_manifest,
        target_manifest=manifest,
    )
    if not binding_impact["compatible"]:
        raise ValueError(
            "Cannot bind an incompatible Knowledge version: "
            + "; ".join(binding_impact["blockers"])
        )
    request = str(args.user_request or "").strip()
    row = {
        "dataset_id": dataset_id,
        "dataset_version": version,
        "content_hash": manifest["content_hash"],
        "state": "active",
        "contract_path": manifest["contract_path"],
        "contract_version": manifest["contract_version"],
        "contract_sha256": manifest["contract_sha256"],
        "materialized_dimension_approved": bool(
            getattr(args, "approve_materialized_dimension", False)
        ),
        "dataset_manifest_path": next(
            item["manifest_path"] for item in entry["versions"] if item.get("version") == version
        ),
        "activated_at": now_iso(),
        "activation_reason": str(args.reason or "Approved project knowledge binding."),
        "replaces_version": (
            str((prior or {}).get("dataset_version") or "")
            if str((prior or {}).get("dataset_version") or "") not in {"", version}
            else str((prior or {}).get("replaces_version") or "")
        ),
        "audit": {
            "function_id": "KNOWLEDGE",
            "user_request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
            "binding_change": binding_impact,
        },
        "generation_provenance": build_generation_provenance(
            generator_script="config_knowledge.py",
            workflow="knowledge_project_binding",
            artifact_kind="KNOWLEDGE_BINDING",
            source="project_binding",
        ),
    }
    rows = [item for item in rows if item.get("dataset_id") != dataset_id]
    rows.append(row)
    bindings["bindings"] = sorted(rows, key=lambda item: str(item.get("dataset_id") or ""))
    bindings["updated_at"] = now_iso()
    write_json(binding_file(project_root), bindings)
    return {
        "status": "bound",
        "schema_version": BINDINGS_SCHEMA,
        "project_id": project_id,
        "dataset_id": dataset_id,
        "dataset_version": version,
        "binding_path": repo_relative(repo_root, binding_file(project_root)),
        "replaces_version": row["replaces_version"],
        "binding_impact": binding_impact,
    }


def active_binding(project_root: Path, dataset_id: str) -> dict[str, Any]:
    bindings = load_bindings(project_root)
    rows = [
        item
        for item in bindings.get("bindings", [])
        if item.get("dataset_id") == dataset_id and item.get("state") == "active"
    ]
    if len(rows) != 1:
        raise ValueError(f"Expected one active project binding for `{dataset_id}`, found {len(rows)}")
    return rows[0]


def validate_knowledge_reference(
    project_root: Path,
    reference: dict[str, Any],
    *,
    require_current_binding: bool = True,
) -> list[str]:
    """Validate one persisted reference against the project's current binding."""

    if not isinstance(reference, dict):
        return ["knowledge reference must be an object"]
    problems: list[str] = []
    if reference.get("schema_version") != REFERENCE_SCHEMA:
        return [f"knowledge reference schema must be {REFERENCE_SCHEMA}"]
    dataset_id = str(reference.get("dataset_id") or "")
    projection_id = str(reference.get("projection_id") or "")
    try:
        validate_identifier(dataset_id, "dataset_id")
        validate_identifier(projection_id, "projection_id")
    except ValueError as exc:
        return [str(exc)]
    fields = reference.get("fields")
    if not isinstance(fields, list) or not fields or any(not isinstance(item, str) or not item for item in fields):
        return [f"knowledge reference fields must be a non-empty string array for {dataset_id}/{projection_id}"]
    if len(fields) != len(set(fields)):
        return [f"knowledge reference fields must be unique for {dataset_id}/{projection_id}"]
    usage_mode = str(reference.get("usage_mode") or "")
    if usage_mode not in ALLOWED_USAGE_MODES:
        return [f"unsupported knowledge reference usage mode for {dataset_id}/{projection_id}: {usage_mode!r}"]
    selection = reference.get("selection")
    if not isinstance(selection, dict):
        problems.append(f"knowledge reference selection evidence is required for {dataset_id}/{projection_id}")
    else:
        selection_mode = str(selection.get("mode") or "")
        if selection_mode not in {"keys", "bounded_projection"}:
            problems.append(f"invalid selection mode for {dataset_id}/{projection_id}: {selection_mode!r}")
        for field in [
            "requested_key_count",
            "matched_row_count",
            "selected_row_count",
            "missing_key_count",
        ]:
            value = selection.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                problems.append(f"selection.{field} must be a non-negative integer for {dataset_id}/{projection_id}")
        for field in ["requested_keys_sha256", "selected_rows_sha256"]:
            value = str(selection.get(field) or "")
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                problems.append(f"selection.{field} must be a SHA-256 hash for {dataset_id}/{projection_id}")
        if not isinstance(selection.get("truncated"), bool):
            problems.append(f"selection.truncated must be boolean for {dataset_id}/{projection_id}")
        requested_count = selection.get("requested_key_count")
        missing_count = selection.get("missing_key_count")
        matched_count = selection.get("matched_row_count")
        selected_count = selection.get("selected_row_count")
        if isinstance(requested_count, int) and isinstance(missing_count, int) and missing_count > requested_count:
            problems.append(f"selection missing_key_count exceeds requested_key_count for {dataset_id}/{projection_id}")
        if isinstance(matched_count, int) and isinstance(selected_count, int) and selected_count > matched_count:
            problems.append(f"selection selected_row_count exceeds matched_row_count for {dataset_id}/{projection_id}")
    fingerprint = str(reference.get("resolution_fingerprint") or "")
    fingerprint_payload = copy.deepcopy(reference)
    fingerprint_payload.pop("resolution_fingerprint", None)
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        problems.append(f"resolution_fingerprint must be a SHA-256 hash for {dataset_id}/{projection_id}")
    elif fingerprint != json_sha256(fingerprint_payload):
        problems.append(f"resolution_fingerprint mismatch for {dataset_id}/{projection_id}")
    try:
        repo_root = discover_repo_root(project_root)
        bindings = load_bindings(project_root)
        if reference.get("project_id") != bindings.get("project_id"):
            problems.append(f"project_id mismatch for {dataset_id}")
        if require_current_binding:
            binding = active_binding(project_root, dataset_id)
            for reference_key, binding_key in [
                ("dataset_version", "dataset_version"),
                ("content_hash", "content_hash"),
                ("contract_path", "contract_path"),
                ("contract_version", "contract_version"),
                ("contract_sha256", "contract_sha256"),
                ("dataset_manifest_path", "dataset_manifest_path"),
            ]:
                if reference.get(reference_key) != binding.get(binding_key):
                    problems.append(f"{dataset_id} {reference_key} no longer matches the active project binding")
        manifest = read_json(resolve_repo_path(repo_root, str(reference.get("dataset_manifest_path") or "")))
        if manifest.get("dataset_id") != dataset_id:
            problems.append(f"dataset manifest identity mismatch for {dataset_id}")
        if manifest.get("version") != reference.get("dataset_version"):
            problems.append(f"dataset manifest version mismatch for {dataset_id}")
        if manifest.get("content_hash") != reference.get("content_hash"):
            problems.append(f"dataset content hash mismatch for {dataset_id}")
        dataset_skill_version = str((manifest.get("generation") or {}).get("skill_version") or "")
        if dataset_skill_version != reference.get("dataset_skill_version"):
            problems.append(f"dataset generator skill version mismatch for {dataset_id}")
        projection = next(
            (item for item in manifest.get("projections", []) if item.get("projection_id") == projection_id),
            None,
        )
        if not projection:
            problems.append(f"bound dataset lacks projection {dataset_id}/{projection_id}")
        elif reference.get("projection_sha256") != projection.get("sha256"):
            problems.append(f"projection hash mismatch for {dataset_id}/{projection_id}")
        reference_contract_path = resolve_repo_path(repo_root, str(reference.get("contract_path") or ""))
        contract = load_contract(repo_root, reference_contract_path)
        if file_sha256(reference_contract_path) != reference.get("contract_sha256"):
            problems.append(f"usage contract hash mismatch for {dataset_id}")
        if contract.get("contract_version") != reference.get("contract_version"):
            problems.append(f"usage contract version mismatch for {dataset_id}")
        rule = projection_contract(contract, projection_id)
        if usage_mode not in rule.get("allowed_usage_modes", []):
            problems.append(f"usage mode is not allowed for {dataset_id}/{projection_id}")
        allowed_fields = set(rule.get("allowed_fields", []))
        unknown_fields = sorted(set(fields) - allowed_fields)
        if unknown_fields:
            problems.append(
                f"reference uses fields outside the contract for {dataset_id}/{projection_id}: "
                + ", ".join(unknown_fields)
            )
        key_field = str(reference.get("key_field") or "")
        if key_field and key_field not in allowed_fields:
            problems.append(f"reference key_field is outside the contract for {dataset_id}/{projection_id}: {key_field}")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        problems.append(str(exc))
    return problems


def resolve_knowledge(
    *,
    project_root: Path,
    dataset_id: str,
    projection_id: str,
    usage_mode: str,
    fields: list[str] | None = None,
    key_field: str = "",
    keys: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    repo_root = discover_repo_root(project_root)
    dataset_id = validate_identifier(dataset_id, "dataset_id")
    projection_id = validate_identifier(projection_id, "projection_id")
    if usage_mode not in ALLOWED_USAGE_MODES:
        raise ValueError(f"Unsupported usage mode: {usage_mode}")
    binding = active_binding(project_root, dataset_id)
    manifest = read_json(resolve_repo_path(repo_root, str(binding["dataset_manifest_path"])))
    contract_path = resolve_repo_path(repo_root, str(binding["contract_path"]))
    contract = load_contract(repo_root, contract_path)
    projection_rule = projection_contract(contract, projection_id)
    allowed_modes = set(projection_rule.get("allowed_usage_modes", []))
    if usage_mode not in allowed_modes:
        raise ValueError(
            f"Usage mode `{usage_mode}` is not allowed for {dataset_id}/{projection_id}; "
            f"allowed: {', '.join(sorted(allowed_modes))}"
        )
    if usage_mode == "materialized_dimension" and not binding.get("materialized_dimension_approved"):
        raise ValueError(
            f"Project binding for `{dataset_id}` does not approve materialized_dimension delivery."
        )
    projection = next(
        (item for item in manifest.get("projections", []) if item.get("projection_id") == projection_id),
        None,
    )
    if not projection:
        raise ValueError(f"Bound dataset version lacks projection: {projection_id}")
    data_path = resolve_repo_path(repo_root, str(projection["data_path"]))
    available_fields, rows = read_csv_rows(data_path)
    requested_fields = [str(item).strip() for item in (fields or []) if str(item).strip()]
    if not requested_fields:
        requested_fields = [str(item) for item in projection_rule.get("default_fields", [])]
    if not requested_fields:
        requested_fields = available_fields
    allowed_fields = set(projection_rule.get("allowed_fields") or available_fields)
    disallowed = sorted(set(requested_fields) - allowed_fields)
    missing = sorted(set(requested_fields) - set(available_fields))
    if disallowed:
        raise ValueError(f"Fields are not allowed by the usage contract: {', '.join(disallowed)}")
    if missing:
        raise ValueError(f"Fields are absent from the bound projection: {', '.join(missing)}")
    primary_key = [str(item) for item in projection_rule.get("primary_key", [])]
    effective_key_field = str(key_field or (primary_key[0] if len(primary_key) == 1 else ""))
    key_values = [str(item) for item in (keys or [])]
    if key_values:
        if not effective_key_field:
            raise ValueError("--key-field is required for a composite-key projection")
        if effective_key_field not in available_fields:
            raise ValueError(f"Unknown key field: {effective_key_field}")
        wanted = set(key_values)
        rows = [row for row in rows if str(row.get(effective_key_field) or "") in wanted]
    configured_limit = int((projection_rule.get("limits") or {}).get(f"{usage_mode}_max_rows") or limit)
    effective_limit = min(max(1, int(limit)), configured_limit)
    truncated = len(rows) > effective_limit
    selected = [{field: row.get(field, "") for field in requested_fields} for row in rows[:effective_limit]]
    found_keys = {str(row.get(effective_key_field) or "") for row in rows} if effective_key_field else set()
    missing_keys = [item for item in key_values if item not in found_keys]
    selection = {
        "mode": "keys" if key_values else "bounded_projection",
        "key_field": effective_key_field,
        "requested_key_count": len(key_values),
        "requested_keys_sha256": json_sha256(key_values),
        "matched_row_count": len(rows),
        "selected_row_count": len(selected),
        "selected_rows_sha256": json_sha256(selected),
        "truncated": truncated,
        "missing_key_count": len(missing_keys),
    }
    reference = {
        "schema_version": REFERENCE_SCHEMA,
        "project_id": load_bindings(project_root)["project_id"],
        "dataset_id": dataset_id,
        "dataset_version": str(binding["dataset_version"]),
        "content_hash": str(binding["content_hash"]),
        "dataset_skill_version": str((manifest.get("generation") or {}).get("skill_version") or "unknown"),
        "projection_id": projection_id,
        "projection_sha256": str(projection["sha256"]),
        "usage_mode": usage_mode,
        "fields": requested_fields,
        "key_field": effective_key_field,
        "contract_path": str(binding["contract_path"]),
        "contract_version": str(binding["contract_version"]),
        "contract_sha256": str(binding["contract_sha256"]),
        "dataset_manifest_path": str(binding["dataset_manifest_path"]),
        "selection": selection,
    }
    reference["resolution_fingerprint"] = json_sha256(reference)
    return {
        "schema_version": RESOLUTION_SCHEMA,
        "status": "warn" if truncated or missing_keys else "pass",
        "reference": reference,
        "rows": selected,
        "row_count": len(selected),
        "matched_row_count": len(rows),
        "truncated": truncated,
        "missing_keys": missing_keys,
        "warnings": [
            message
            for message in [
                f"Result truncated to {effective_limit} rows." if truncated else "",
                f"Keys not found: {', '.join(missing_keys)}" if missing_keys else "",
            ]
            if message
        ],
    }


def validate_dataset_manifest(repo_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    if manifest.get("schema_version") != DATASET_SCHEMA:
        problems.append(f"manifest schema_version must be {DATASET_SCHEMA}")
    dataset_id = str(manifest.get("dataset_id") or "")
    version = str(manifest.get("version") or "")
    if not dataset_id or not version:
        problems.append("dataset_id and version are required")
    generation = manifest.get("generation") if isinstance(manifest.get("generation"), dict) else {}
    if not generation.get("skill_version") or not generation.get("generated_by_script"):
        problems.append("dataset generation provenance is incomplete")
    contract_hash = ""
    spec_hash = ""
    source_hash = ""
    projection_hashes: list[dict[str, str]] = []
    content_hash_contract = str(manifest.get("content_hash_contract") or "knowledge_content_v1")
    if content_hash_contract not in {"knowledge_content_v1", CONTENT_HASH_CONTRACT}:
        problems.append(f"unsupported content_hash_contract: {content_hash_contract}")
    try:
        contract_path = resolve_repo_path(repo_root, str(manifest.get("contract_path") or ""))
        contract = load_contract(repo_root, contract_path)
        contract_hash = file_sha256(contract_path)
        if contract_hash != manifest.get("contract_sha256"):
            problems.append("usage contract hash mismatch")
        if contract.get("contract_version") != manifest.get("contract_version"):
            problems.append("usage contract version mismatch")
        adapter = manifest.get("adapter") if isinstance(manifest.get("adapter"), dict) else {}
        spec_snapshot = resolve_repo_path(repo_root, str(adapter.get("spec_snapshot_path") or ""))
        spec_hash = file_sha256(spec_snapshot)
        if spec_hash != adapter.get("spec_sha256"):
            problems.append("extraction spec snapshot hash mismatch")
        spec = read_json(spec_snapshot)
        if spec.get("status") != "active" or adapter.get("review_status") != "active":
            problems.append("extraction spec was not active when the dataset version was built")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        problems.append(f"invalid immutable dataset contract: {exc}")
    projection_rows = manifest.get("projections") if isinstance(manifest.get("projections"), list) else []
    if not projection_rows:
        problems.append("dataset manifest must contain at least one projection")
    seen_projection_ids: set[str] = set()
    for item in projection_rows:
        try:
            projection_id = str(item.get("projection_id") or "")
            if not projection_id or projection_id in seen_projection_ids:
                problems.append(f"missing or duplicate projection_id: {projection_id!r}")
            seen_projection_ids.add(projection_id)
            data_path = resolve_repo_path(repo_root, str(item.get("data_path") or ""))
            if not data_path.is_file():
                problems.append(f"missing projection data: {item.get('data_path')}")
            else:
                projection_hash = file_sha256(data_path)
                projection_hashes.append({"projection_id": projection_id, "sha256": projection_hash})
                if projection_hash != item.get("sha256"):
                    problems.append(f"projection hash mismatch: {projection_id}")
            metadata_hash_fields = {
                "schema_path": "schema_sha256",
                "profile_path": "profile_sha256",
                "preview_path": "preview_sha256",
            }
            for field, hash_field in metadata_hash_fields.items():
                path = resolve_repo_path(repo_root, str(item.get(field) or ""))
                if not path.is_file():
                    problems.append(f"missing projection {field}: {item.get(field)}")
                elif content_hash_contract == CONTENT_HASH_CONTRACT:
                    expected_hash = str(item.get(hash_field) or "")
                    if not expected_hash:
                        problems.append(f"missing projection {hash_field}: {projection_id}")
                    elif file_sha256(path) != expected_hash:
                        problems.append(f"projection metadata hash mismatch: {projection_id}/{field}")
        except (ValueError, OSError) as exc:
            problems.append(str(exc))
    try:
        snapshot_manifest = read_json(resolve_repo_path(repo_root, str(manifest.get("source_snapshot") or "")))
        if snapshot_manifest.get("schema_version") != SNAPSHOT_SCHEMA:
            problems.append("source snapshot schema mismatch")
        if snapshot_manifest.get("source_id") != dataset_id:
            problems.append("source snapshot dataset identity mismatch")
        snapshot_generation = (
            snapshot_manifest.get("generation_provenance")
            if isinstance(snapshot_manifest.get("generation_provenance"), dict)
            else {}
        )
        if not snapshot_generation.get("skill_version"):
            problems.append("source snapshot generation provenance is incomplete")
        source_file = resolve_repo_path(repo_root, str(snapshot_manifest.get("stored_file") or ""))
        source_hash = file_sha256(source_file)
        if source_hash != snapshot_manifest.get("source_sha256"):
            problems.append("source snapshot hash mismatch")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        problems.append(f"invalid source snapshot: {exc}")
    if contract_hash and spec_hash and source_hash and projection_hashes:
        content_payload = {
            "dataset_id": dataset_id,
            "source_sha256": source_hash,
            "contract_sha256": contract_hash,
            "extraction_spec_sha256": spec_hash,
            "projections": projection_hashes,
        }
        if content_hash_contract == CONTENT_HASH_CONTRACT:
            content_payload = {"content_hash_contract": CONTENT_HASH_CONTRACT, **content_payload}
        expected_content_hash = json_sha256(content_payload)
        if manifest.get("content_hash") != expected_content_hash:
            problems.append("dataset content hash does not match immutable inputs")
        if version != f"kdv-{expected_content_hash[:12]}":
            problems.append("dataset version does not match content hash")
    return {"status": "pass" if not problems else "fail", "problems": problems}


def validate_repository(repo_root: Path, project_root: Path | None = None) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []
    catalog = load_catalog(repo_root)
    catalog_entries = [item for item in catalog.get("datasets", []) if isinstance(item, dict)]
    scoped_bindings: dict[str, Any] | None = None
    if project_root is not None:
        project_root = project_root.resolve()
        bindings_path = binding_file(project_root)
        if bindings_path.exists():
            try:
                scoped_bindings = load_bindings(project_root)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                problems.append(f"project bindings: {exc}")
                scoped_bindings = {"bindings": []}
            bound_ids = {
                str(item.get("dataset_id") or "")
                for item in scoped_bindings.get("bindings", []) or []
                if isinstance(item, dict) and item.get("state") == "active"
            }
            catalog_entries = [item for item in catalog_entries if str(item.get("dataset_id") or "") in bound_ids]
        else:
            catalog_entries = []
            warnings.append("project has no knowledge/bindings.json")
    seen: set[str] = set()
    for entry in catalog_entries:
        dataset_id = str(entry.get("dataset_id") or "")
        if not dataset_id or dataset_id in seen:
            problems.append(f"missing or duplicate catalog dataset_id: {dataset_id!r}")
            continue
        seen.add(dataset_id)
        try:
            contract = load_contract(repo_root, resolve_repo_path(repo_root, str(entry.get("contract_path") or "")))
            if contract.get("dataset_id") != dataset_id:
                problems.append(f"contract dataset_id mismatch: {dataset_id}")
            versions = entry.get("versions") if isinstance(entry.get("versions"), list) else []
            if not versions:
                problems.append(f"dataset has no versions: {dataset_id}")
            for version_row in versions:
                manifest = read_json(resolve_repo_path(repo_root, str(version_row.get("manifest_path") or "")))
                result = validate_dataset_manifest(repo_root, manifest)
                problems.extend(f"{dataset_id}@{version_row.get('version')}: {item}" for item in result["problems"])
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            problems.append(f"{dataset_id}: {exc}")
    binding_summary: list[dict[str, Any]] = []
    if project_root is not None:
        bindings_path = binding_file(project_root)
        if bindings_path.exists():
            try:
                bindings = scoped_bindings or load_bindings(project_root)
                bound_ids: set[str] = set()
                for binding in bindings.get("bindings", []):
                    dataset_id = str(binding.get("dataset_id") or "")
                    if dataset_id in bound_ids:
                        problems.append(f"duplicate project binding: {dataset_id}")
                    bound_ids.add(dataset_id)
                    entry = latest_dataset_entry(catalog, dataset_id)
                    if not entry:
                        problems.append(f"binding references unknown dataset: {dataset_id}")
                        continue
                    manifest = read_json(resolve_repo_path(repo_root, str(binding.get("dataset_manifest_path") or "")))
                    if manifest.get("dataset_id") != dataset_id:
                        problems.append(f"binding manifest dataset mismatch: {dataset_id}")
                    if manifest.get("version") != binding.get("dataset_version"):
                        problems.append(f"binding version mismatch: {dataset_id}")
                    if manifest.get("content_hash") != binding.get("content_hash"):
                        problems.append(f"binding content hash mismatch: {dataset_id}")
                    if manifest.get("contract_version") != binding.get("contract_version"):
                        problems.append(f"binding contract version mismatch: {dataset_id}")
                    if manifest.get("contract_sha256") != binding.get("contract_sha256"):
                        problems.append(f"binding contract hash mismatch: {dataset_id}")
                    binding_generation = (
                        binding.get("generation_provenance")
                        if isinstance(binding.get("generation_provenance"), dict)
                        else {}
                    )
                    if not binding_generation.get("skill_version"):
                        problems.append(f"binding generation provenance is incomplete: {dataset_id}")
                    contract = load_contract(repo_root, resolve_repo_path(repo_root, str(binding.get("contract_path") or "")))
                    if bindings.get("project_id") not in contract.get("project_scopes", []):
                        problems.append(f"binding contract excludes project: {dataset_id}")
                    binding_summary.append(
                        {
                            "dataset_id": dataset_id,
                            "dataset_version": binding.get("dataset_version"),
                            "state": binding.get("state"),
                        }
                    )
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                problems.append(f"project bindings: {exc}")
    return {
        "schema_version": "knowledge_validation_result_v1",
        "status": "fail" if problems else "warn" if warnings else "pass",
        "validation_scope": "project_bindings" if project_root is not None else "repository",
        "dataset_count": len(catalog_entries),
        "repository_dataset_count": len(catalog.get("datasets", [])),
        "bindings": binding_summary,
        "problems": problems,
        "warnings": warnings,
    }


def _bound_discovery_contract(repo_root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    contract = load_contract(repo_root, resolve_repo_path(repo_root, str(binding["contract_path"])))
    projections = []
    for item in contract.get("projections", []):
        projections.append(
            {
                "projection_id": item.get("projection_id"),
                "description": item.get("description", ""),
                "primary_key": item.get("primary_key", []),
                "default_fields": item.get("default_fields", []),
                "allowed_usage_modes": item.get("allowed_usage_modes", []),
                "allowed_usages": item.get("allowed_usages", []),
                "field_roles": item.get("field_roles", {}),
            }
        )
    return {
        "source_kind": contract.get("source_kind", ""),
        "business_domain": contract.get("business_domain", ""),
        "purpose": contract.get("purpose", ""),
        "projections": projections,
    }


def list_datasets(
    repo_root: Path,
    project_root: Path | None = None,
    active_only: bool = False,
) -> dict[str, Any]:
    if active_only and project_root is None:
        raise ValueError("--active-only requires --root so discovery is scoped to one project")
    catalog = load_catalog(repo_root)
    active: dict[str, dict[str, Any]] = {}
    if project_root is not None and binding_file(project_root).exists():
        active = {str(item["dataset_id"]): item for item in load_bindings(project_root).get("bindings", [])}
    rows = []
    for item in catalog.get("datasets", []):
        binding = active.get(str(item.get("dataset_id") or ""), {})
        is_active = binding.get("state") == "active"
        if active_only and not is_active:
            continue
        manifest_version = str(binding.get("dataset_version") or item.get("latest_version") or "")
        manifest = dataset_version_manifest(repo_root, item, manifest_version)
        row = {
            "dataset_id": item.get("dataset_id"),
            "display_name": item.get("display_name"),
            "latest_version": item.get("latest_version"),
            "bound_version": binding.get("dataset_version", ""),
            "binding_state": binding.get("state", "unbound"),
            "contract_path": binding.get("contract_path") if is_active else item.get("contract_path"),
            "generated_by_skill_version": (manifest.get("generation") or {}).get("skill_version", "unknown"),
        }
        if is_active:
            row.update(_bound_discovery_contract(repo_root, binding))
        rows.append(row)
    return {"schema_version": CATALOG_SCHEMA, "dataset_count": len(rows), "datasets": rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ["register", "refresh"]:
        item = sub.add_parser(command, help=f"{command.title()} an immutable config-table dataset version")
        item.add_argument("--repo-root", default=".")
        item.add_argument("--dataset-id", required=True)
        item.add_argument("--contract", required=True)
        item.add_argument("--adapter-spec", required=True)
        item.add_argument(
            "--run-adapter",
            action="store_true",
            help="Run the reviewed Excel or supported static-source extractor before registration.",
        )
        item.add_argument("--format", choices=["json"], default="json")
        add_function_gate_arguments(item, selection_help="Required explicit route: [KNOWLEDGE].")

    bind = sub.add_parser("bind", help="Activate one registered dataset version for a project")
    bind.add_argument("--root", required=True, help="SQL project root")
    bind.add_argument("--dataset-id", required=True)
    bind.add_argument("--version", default="", help="Defaults to catalog latest_version")
    bind.add_argument("--reason", default="")
    bind.add_argument(
        "--approve-materialized-dimension",
        action="store_true",
        help="Explicitly approve runtime use through a separately governed materialized dimension.",
    )
    bind.add_argument("--format", choices=["json"], default="json")
    add_function_gate_arguments(bind, selection_help="Required explicit route: [KNOWLEDGE].")

    resolve = sub.add_parser("resolve", help="Resolve rows from the project-bound dataset version")
    resolve.add_argument("--root", required=True, help="SQL project root")
    resolve.add_argument("--dataset-id", required=True)
    resolve.add_argument("--projection", required=True)
    resolve.add_argument("--usage-mode", choices=sorted(ALLOWED_USAGE_MODES), default="authoring_reference")
    resolve.add_argument("--field", action="append", default=[])
    resolve.add_argument("--key-field", default="")
    resolve.add_argument("--key", action="append", default=[])
    resolve.add_argument("--limit", type=int, default=100)
    resolve.add_argument("--out", default="", help="Optional JSON output path; absolute paths are not persisted in the result")
    resolve.add_argument("--format", choices=["json"], default="json")

    validate = sub.add_parser("validate", help="Validate catalog, immutable versions, and optional project bindings")
    validate.add_argument("--repo-root", default=".")
    validate.add_argument("--root", default="", help="Optional SQL project root")
    validate.add_argument("--format", choices=["json"], default="json")

    listing = sub.add_parser("list", help="List registered datasets and optional project bindings")
    listing.add_argument("--repo-root", default=".")
    listing.add_argument("--root", default="", help="Optional SQL project root")
    listing.add_argument(
        "--active-only",
        action="store_true",
        help="List only active project bindings and their bounded semantic discovery contracts; requires --root.",
    )
    listing.add_argument("--format", choices=["json"], default="json")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command in {"register", "refresh"}:
            payload = register_dataset(args)
        elif args.command == "bind":
            payload = bind_dataset(args)
        elif args.command == "resolve":
            payload = resolve_knowledge(
                project_root=Path(args.root),
                dataset_id=args.dataset_id,
                projection_id=args.projection,
                usage_mode=args.usage_mode,
                fields=args.field,
                key_field=args.key_field,
                keys=args.key,
                limit=args.limit,
            )
            if args.out:
                receipt_path = safe_resolution_receipt_path(Path(args.root), args.out)
                write_json(receipt_path, payload)
        elif args.command == "validate":
            repo_root = discover_repo_root(Path(args.repo_root))
            project_root = Path(args.root).resolve() if args.root else None
            payload = validate_repository(repo_root, project_root)
        else:
            repo_root = discover_repo_root(Path(args.repo_root))
            project_root = Path(args.root).resolve() if args.root else None
            payload = list_datasets(repo_root, project_root, active_only=args.active_only)
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
        return 2
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        payload = {"status": "fail", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
