#!/usr/bin/env python3
"""Move mutable mapping rows out of current rules into project-bound knowledge datasets."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from config_knowledge import active_binding, bind_dataset, read_json, register_dataset, write_json  # noqa: E402
from rule_store import RuleStore, file_sha256  # noqa: E402


PLAN_SCHEMA = "canonical_rule_mapping_knowledge_plan_v1"
RECEIPT_SCHEMA = "canonical_rule_mapping_knowledge_receipt_v1"


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repo_root_from_project(project_root: Path) -> Path:
    for candidate in [project_root.resolve(), *project_root.resolve().parents]:
        if (candidate / "sql-engineering").is_dir() and (candidate / "sql-projects").is_dir():
            return candidate
    raise ValueError(f"Cannot locate repository root from {project_root}")


def relative(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def receipt_path_for_plan(project_root: Path, plan_path: Path) -> Path:
    name = plan_path.name
    if name.endswith(".plan.json"):
        name = name[: -len(".plan.json")] + ".receipt.json"
    else:
        name = plan_path.stem + ".receipt.json"
    return project_root / "rules" / "migrations" / name


def clean_rule(rule: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(rule)
    result.pop("_rule_store", None)
    return result


def normalize_cell(value: Any) -> str:
    return str(value or "").strip().strip("`")


def markdown_table_rows(content: str, extraction: dict[str, Any]) -> list[dict[str, str]]:
    columns = extraction.get("columns") or []
    source_headers = [str(item.get("source") or "") for item in columns]
    target_fields = [str(item.get("field") or "") for item in columns]
    table: list[list[str]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            if table:
                break
            continue
        cells = [normalize_cell(item) for item in stripped.strip("|").split("|")]
        if not table:
            if cells == source_headers:
                table.append(cells)
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        if len(cells) != len(target_fields):
            raise ValueError(f"Markdown mapping row has {len(cells)} cells, expected {len(target_fields)}")
        table.append(cells)
    if len(table) < 2:
        raise ValueError(f"Markdown mapping table not found for headers: {source_headers}")
    return [dict(zip(target_fields, row)) for row in table[1:]]


def structured_mapping_rows(rule: dict[str, Any], extraction: dict[str, Any]) -> list[dict[str, str]]:
    structured = rule.get("structured_definition") or {}
    table_name = str(extraction.get("table_name") or "")
    table = next(
        (
            item
            for item in structured.get("mapping_tables", []) or []
            if isinstance(item, dict) and item.get("name") == table_name
        ),
        None,
    )
    if not table:
        raise ValueError(f"structured_definition.mapping_tables does not contain {table_name!r}")
    rename = extraction.get("rename") or {}
    rows: list[dict[str, str]] = []
    for source in table.get("rows", []) or []:
        rows.append(
            {
                str(rename.get(key) or key): normalize_cell(value)
                for key, value in source.items()
            }
        )
    if not rows:
        raise ValueError(f"Structured mapping table {table_name!r} is empty")
    return rows


def extract_rows(rule: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, str]]:
    extraction = entry.get("extraction") or {}
    kind = extraction.get("type")
    if kind == "markdown_table":
        rows = markdown_table_rows(str(rule.get("content") or ""), extraction)
    elif kind == "structured_mapping_table":
        rows = structured_mapping_rows(rule, extraction)
    elif kind == "explicit_rows":
        rows = [
            {str(key): normalize_cell(value) for key, value in row.items()}
            for row in extraction.get("rows", []) or []
            if isinstance(row, dict)
        ]
    else:
        raise ValueError(f"Unsupported extraction type for {entry.get('concept_key')}: {kind!r}")
    fields = [str(item) for item in (entry.get("projection") or {}).get("fields", [])]
    if not fields:
        raise ValueError(f"Projection fields are missing for {entry.get('concept_key')}")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    primary_key = [str(item) for item in (entry.get("projection") or {}).get("primary_key", [])]
    for row in rows:
        missing = [field for field in fields if field not in row]
        if missing:
            raise ValueError(f"Mapping row is missing fields {missing}: {row}")
        result = {field: normalize_cell(row.get(field)) for field in fields}
        key = tuple(result[field] for field in primary_key)
        if key in seen:
            raise ValueError(f"Duplicate mapping primary key {key} for {entry.get('concept_key')}")
        seen.add(key)
        normalized.append(result)
    if not normalized:
        raise ValueError(f"No mapping rows extracted for {entry.get('concept_key')}")
    return normalized


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def knowledge_contract(entry: dict[str, Any], row_count: int, project_id: str) -> dict[str, Any]:
    dataset = entry["dataset"]
    projection = entry["projection"]
    limit = max(100, row_count)
    return {
        "schema_version": "knowledge_usage_contract_v1",
        "contract_version": "1.0.0",
        "dataset_id": dataset["dataset_id"],
        "status": "active",
        "display_name": dataset["display_name"],
        "source_kind": "manual_mapping",
        "business_domain": dataset["business_domain"],
        "project_scopes": [project_id],
        "purpose": dataset["purpose"],
        "exclusions": dataset.get("exclusions", []),
        "projections": [
            {
                "projection_id": projection["projection_id"],
                "source_output": f"{projection['projection_id']}.csv",
                "description": projection["description"],
                "primary_key": projection["primary_key"],
                "required_fields": projection["fields"],
                "default_fields": projection["fields"],
                "allowed_fields": projection["fields"],
                "field_roles": projection["field_roles"],
                "allowed_usage_modes": [
                    "authoring_reference",
                    "inline_mapping",
                    "filter_set",
                    "result_enrichment",
                ],
                "limits": {
                    "authoring_reference_max_rows": limit,
                    "inline_mapping_max_rows": limit,
                    "filter_set_max_rows": limit,
                    "result_enrichment_max_rows": limit,
                },
                "log_bindings": projection.get("log_bindings", []),
                "allowed_usages": projection.get("allowed_usages", []),
                "forbidden_usages": projection.get("forbidden_usages", []),
            }
        ],
        "governance": {
            "runtime_excel_dependency": "forbidden",
            "activation_policy": "explicit_project_binding",
            "materialized_dimension_requires_approval": True,
            "refresh_policy": "Mapping changes create a new immutable version; bind it only after reviewing diff.json.",
        },
    }


def import_spec(entry: dict[str, Any], source_path: str, row_count: int, csv_path: str) -> dict[str, Any]:
    return {
        "schema_version": "knowledge_static_import_v1",
        "status": "active",
        "source_kind": "manual_mapping",
        "source_file": source_path,
        "projections": [
            {
                "projection_id": entry["projection"]["projection_id"],
                "source_file": csv_path,
                "note": "Exact rows extracted from the immutable pre-migration rule version.",
            }
        ],
        "provenance": {
            "migration": PLAN_SCHEMA,
            "source_concept_key": entry["concept_key"],
            "row_count": row_count,
        },
    }


def authorization(user_request: str) -> dict[str, Any]:
    return {
        "contract_version": "rule_write_authorization_v1",
        "function_id": "RULES",
        "selection": "[RULES]",
        "requested_status": "confirmed",
        "user_request_sha256": hashlib.sha256(user_request.encode("utf-8")).hexdigest(),
        "explicit_user_selection": True,
        "authorized_at": now_iso(),
    }


def dependency_from_manifest(manifest: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    projection_id = entry["projection"]["projection_id"]
    return {
        "dataset_id": manifest["dataset_id"],
        "projection_id": projection_id,
        "semantic_role": entry["rule"]["semantic_role"],
        "fields": entry["projection"]["fields"],
        "required": True,
        "binding_policy": "active_project_binding",
    }


def completed_dependency(current: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    structured = current.get("structured_definition") if isinstance(current.get("structured_definition"), dict) else {}
    expected_dataset = str(entry.get("dataset", {}).get("dataset_id") or "")
    expected_projection = str(entry.get("projection", {}).get("projection_id") or "")
    for dependency in structured.get("knowledge_dependencies", []) or []:
        if not isinstance(dependency, dict):
            continue
        if (
            str(dependency.get("dataset_id") or "") == expected_dataset
            and str(dependency.get("projection_id") or "") == expected_projection
            and dependency.get("required") is True
        ):
            return dependency
    return None


def migrated_rule(
    current: dict[str, Any],
    entry: dict[str, Any],
    dependency: dict[str, Any],
    user_request: str,
    plan_path: str,
) -> dict[str, Any]:
    result = clean_rule(current)
    result["version"] = int(current.get("version") or 0) + 1
    result["status"] = "confirmed"
    result["content"] = entry["rule"]["content"]
    result["source"] = "rule_mapping_knowledge_migration.py"
    result["source_evidence"] = plan_path
    result["supersedes"] = f"{current.get('rule_id')}@v{current.get('version')}"
    result["created_at"] = now_iso()
    result["updated_at"] = result["created_at"]
    result["notes"] = entry["rule"].get(
        "notes",
        "Mutable mapping rows moved into an immutable, project-bound knowledge dataset; prior rule versions preserve the original rows.",
    )
    structured = copy.deepcopy(entry["rule"].get("structured_definition") or {})
    structured["knowledge_dependencies"] = [dependency]
    result["structured_definition"] = structured
    contract = copy.deepcopy(current.get("activation_contract") or {})
    contract["contract_version"] = "canonical_rule_activation_v2"
    contract["activation_policy"] = {
        "forward": str((contract.get("activation_policy") or {}).get("forward") or "automatic"),
        "reverse": "disabled",
    }
    contract.pop("event_signature", None)
    contract.pop("source_signature", None)
    contract.pop("negative_signature", None)
    contract["hard_constraints"] = [
        {
            "type": "must_use_knowledge_dependency",
            "dataset_id": dependency["dataset_id"],
            "projection_id": dependency["projection_id"],
            "fields": dependency["fields"],
            "binding_policy": "active_project_binding",
            "reason": "Mutable mapping values must come from the project's reviewed active Knowledge binding.",
        },
        *copy.deepcopy(entry["rule"].get("hard_constraints") or []),
    ]
    result["activation_contract"] = contract
    result["change_authorization"] = authorization(user_request)
    return result


def apply_plan(project_root: Path, plan_path: Path, user_request: str, dry_run: bool) -> dict[str, Any]:
    repo_root = repo_root_from_project(project_root)
    plan = read_json(plan_path)
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise ValueError(f"Unsupported migration plan schema: {plan.get('schema_version')!r}")
    plan_sha256 = file_sha256(plan_path)
    receipt_path = receipt_path_for_plan(project_root, plan_path)
    if receipt_path.exists() and not dry_run:
        existing = read_json(receipt_path)
        if existing.get("plan_sha256") != plan_sha256:
            raise ValueError(f"Existing receipt belongs to a different plan: {receipt_path}")
        existing["status"] = "already_applied"
        existing["receipt_path"] = relative(repo_root, receipt_path)
        return existing
    store = RuleStore(project_root)
    results: list[dict[str, Any]] = []
    for entry in plan.get("migrations", []) or []:
        concept_key = str(entry.get("concept_key") or "")
        current_rows = store.load_current([concept_key])
        if len(current_rows) != 1:
            raise ValueError(f"Expected one current rule for {concept_key}, found {len(current_rows)}")
        current = current_rows[0]
        existing_dependency = completed_dependency(current, entry)
        if existing_dependency:
            binding = active_binding(project_root, str(existing_dependency["dataset_id"]))
            manifest_path = repo_root / str(binding["dataset_manifest_path"])
            manifest = read_json(manifest_path)
            projection = next(
                item
                for item in manifest.get("projections", []) or []
                if item.get("projection_id") == existing_dependency.get("projection_id")
            )
            results.append(
                {
                    "concept_key": concept_key,
                    "dataset_id": existing_dependency["dataset_id"],
                    "projection_id": existing_dependency["projection_id"],
                    "row_count": int(projection.get("row_count") or 0),
                    "source_rule": str((current.get("_rule_store") or {}).get("path") or ""),
                    "dataset_version": manifest["version"],
                    "content_hash": manifest["content_hash"],
                    "projection_sha256": projection["sha256"],
                    "rule_version": int(current.get("version") or 0),
                    "rule_path": str((current.get("_rule_store") or {}).get("path") or ""),
                    "migration_state": "already_completed",
                }
            )
            continue
        rows = extract_rows(current, entry)
        dataset_id = entry["dataset"]["dataset_id"]
        projection_id = entry["projection"]["projection_id"]
        import_dir = repo_root / "knowledge-base" / "imports" / dataset_id
        csv_path = import_dir / f"{projection_id}.csv"
        spec_path = import_dir / "v001.import.json"
        contract_path = repo_root / "knowledge-base" / "contracts" / f"{dataset_id}.json"
        source_path = str((current.get("_rule_store") or {}).get("path") or "")
        source_repo_path = relative(repo_root, project_root / source_path)
        preview = {
            "concept_key": concept_key,
            "dataset_id": dataset_id,
            "projection_id": projection_id,
            "row_count": len(rows),
            "source_rule": source_path,
        }
        if dry_run:
            results.append(preview)
            continue
        write_csv(csv_path, entry["projection"]["fields"], rows)
        write_json(contract_path, knowledge_contract(entry, len(rows), project_root.name))
        write_json(
            spec_path,
            import_spec(entry, source_repo_path, len(rows), relative(repo_root, csv_path)),
        )
        knowledge_request = f"[KNOWLEDGE] {user_request}"
        registration = register_dataset(
            SimpleNamespace(
                command="register",
                repo_root=str(repo_root),
                dataset_id=dataset_id,
                contract=relative(repo_root, contract_path),
                adapter_spec=relative(repo_root, spec_path),
                run_adapter=False,
                function_selection="KNOWLEDGE",
                user_request=knowledge_request,
            )
        )
        bind_dataset(
            SimpleNamespace(
                command="bind",
                root=str(project_root),
                dataset_id=dataset_id,
                version=registration["version"],
                reason="Canonical rule mapping migration.",
                approve_materialized_dimension=False,
                function_selection="KNOWLEDGE",
                user_request=knowledge_request,
            )
        )
        manifest = read_json(repo_root / registration["manifest_path"])
        dependency = dependency_from_manifest(manifest, entry)
        record = migrated_rule(
            current,
            entry,
            dependency,
            user_request,
            relative(repo_root, plan_path),
        )
        saved = store.write_new_version(record)
        results.append(
            {
                **preview,
                "dataset_version": manifest["version"],
                "content_hash": manifest["content_hash"],
                "projection_sha256": next(
                    item["sha256"]
                    for item in manifest["projections"]
                    if item["projection_id"] == dependency["projection_id"]
                ),
                "rule_version": saved["rule_version"],
                "rule_path": saved["path"],
            }
        )
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "dry_run" if dry_run else "applied",
        "project_id": project_root.name,
        "plan": relative(repo_root, plan_path),
        "plan_sha256": plan_sha256,
        "created_at": now_iso(),
        "migrations": results,
    }
    if not dry_run:
        write_json(receipt_path, receipt)
        receipt["receipt_path"] = relative(repo_root, receipt_path)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--user-request", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json"], default="json")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = apply_plan(
        Path(args.root).resolve(),
        Path(args.plan).resolve(),
        args.user_request,
        args.dry_run,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
