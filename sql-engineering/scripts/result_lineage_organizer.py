#!/usr/bin/env python3
"""Inspect ambiguous result lineage and apply only user-confirmed decisions."""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from asset_provenance import build_generation_provenance  # noqa: E402
from capability_registry import command_function_ids  # noqa: E402
from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from result_evidence_retention import prepare_result_evidence  # noqa: E402
from sql_result_inspector import inspect_result_file  # noqa: E402
from sql_summary_planner import load_analysis_bundle  # noqa: E402
from sql_query_workspace import (  # noqa: E402
    INDEX_REL,
    _index_files,
    _write_transaction,
    json_text,
    load_index,
    now_iso,
    read_json,
    resolve_project_path,
)


INSPECTION_VERSION = "result_lineage_inspection_v1"
DECISION_VERSION = "result_lineage_decision_v1"
APPLY_RECEIPT_VERSION = "result_lineage_apply_receipt_v1"
ACTIVE_STATE = "active"
RETIRED_STATES = {"superseded", "discarded"}
BUNDLE_OUTPUT_REF_VERSION = "query_analysis_bundle_output_ref_v1"
BUNDLE_OUTPUT_KINDS = {"visualization", "analysis_workbook", "comparison_workbook"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _version_map(index: dict[str, Any]) -> dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]]:
    values: dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]] = {}
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        query_id = _clean(entry.get("query_id"))
        for version in entry.get("versions", []):
            if isinstance(version, dict):
                values[(query_id, int(version.get("version") or 0))] = (entry, version)
    return values


def _output_map(index: dict[str, Any]) -> dict[tuple[str, int, str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    values: dict[tuple[str, int, str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
    for (query_id, version_number), (entry, version) in _version_map(index).items():
        for output in version.get("derived_outputs", []):
            if isinstance(output, dict):
                values[(query_id, version_number, _clean(output.get("attachment_id")))] = (entry, version, output)
    return values


def _selector_key(selector: dict[str, Any], id_field: str) -> tuple[str, int, str]:
    return (
        _clean(selector.get("query_id")),
        int(selector.get("version") or 0),
        _clean(selector.get(id_field)),
    )


def _find_output(index: dict[str, Any], selector: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    key = _selector_key(selector, "attachment_id")
    found = _output_map(index).get(key)
    if not found:
        raise ValueError(f"Derived output not found: {key}")
    return found


def _find_result(index: dict[str, Any], selector: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    key = _selector_key(selector, "result_id")
    found = _output_map(index).get(key)
    if not found or found[2].get("kind") != "result_evidence":
        raise ValueError(f"Result evidence not found: {key}")
    return found


def _output_reference(entry: dict[str, Any], version: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    return {
        "query_id": _clean(entry.get("query_id")),
        "version": int(version.get("version") or 0),
        "attachment_id": _clean(output.get("attachment_id")),
        "path": _clean(output.get("path")),
    }


def _result_reference(entry: dict[str, Any], version: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "query_id": _clean(entry.get("query_id")),
        "version": int(version.get("version") or 0),
        "sql_path": _clean(version.get("path")),
        "sql_fingerprint": _clean(version.get("sql_fingerprint")),
        "result_id": _clean(result.get("attachment_id")),
        "result_path": _clean(result.get("path")),
        "result_sha256": _clean(result.get("source_sha256") or result.get("sha256")),
    }


def _field_key(value: Any) -> str:
    return "".join(
        character
        for character in _clean(value).casefold()
        if not character.isspace() and character not in "`'[]"
    ).replace(chr(34), "")


def _result_columns(root: Path, result: dict[str, Any]) -> list[str]:
    retention = result.get("retention") if isinstance(result.get("retention"), dict) else {}
    columns = [str(item) for item in retention.get("columns", []) if _clean(item)]
    if columns:
        return columns
    path = resolve_project_path(root, _clean(result.get("path")))
    return [str(item) for item in inspect_result_file(path).get("columns", []) if _clean(item)]


def _bundle_output_reference(root: Path, bundle_path: Path, bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": BUNDLE_OUTPUT_REF_VERSION,
        "bundle_id": _clean(bundle.get("bundle_id")),
        "path": bundle_path.relative_to(root).as_posix(),
        "metric_contract_fingerprint": _clean(bundle.get("metric_contract_fingerprint")),
    }


def _xlsx_sheets(path: Path) -> list[str]:
    if path.suffix.lower() != ".xlsx" or not path.is_file():
        return []
    try:
        with zipfile.ZipFile(path) as archive:
            text = archive.read("xl/workbook.xml").decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return []
    return re.findall(r'<(?:\w+:)?sheet[^>]+name="([^"]+)"', text)


def _parse_time(value: Any) -> datetime | None:
    text = _clean(value)
    if not text:
        return None


def _result_preview(path: Path) -> dict[str, Any]:
    try:
        inspected = inspect_result_file(path, sample_limit=3)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile):
        return {"columns": [], "row_count": None, "sample_rows": []}
    return {
        "columns": inspected.get("columns") or [],
        "row_count": inspected.get("row_count"),
        "sample_rows": inspected.get("sample_rows") or [],
    }
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def inspect_lineage(root: Path) -> dict[str, Any]:
    root = root.resolve()
    index = load_index(root)
    cases: list[dict[str, Any]] = []
    hashes: dict[str, list[dict[str, Any]]] = {}
    for (query_id, version_number), (entry, version) in _version_map(index).items():
        outputs = [item for item in version.get("derived_outputs", []) if isinstance(item, dict)]
        results = [item for item in outputs if item.get("kind") == "result_evidence"]
        for output in outputs:
            digest = _clean(output.get("source_sha256") or output.get("sha256"))
            if digest:
                hashes.setdefault(digest, []).append(
                    {
                        "query_id": query_id,
                        "version": version_number,
                        "attachment_id": _clean(output.get("attachment_id")),
                        "kind": _clean(output.get("kind")),
                        "path": _clean(output.get("path")),
                    }
                )
            state = _clean(output.get("asset_state")) or ACTIVE_STATE
            lineage = _clean(output.get("lineage_status"))
            if state not in {ACTIVE_STATE, "needs_review"} or lineage not in {"", "sql_version_only", "unresolved_legacy"}:
                continue
            output_time = _parse_time(output.get("created_at"))
            candidate_results: list[dict[str, Any]] = []
            for result in results:
                result_time = _parse_time(result.get("created_at"))
                delta = None
                if output_time and result_time:
                    delta = int((output_time - result_time).total_seconds())
                candidate_results.append(
                    {
                        "result_id": _clean(result.get("attachment_id")),
                        "title": _clean(result.get("title")),
                        "purpose": _clean(result.get("purpose")),
                        "created_at": _clean(result.get("created_at")),
                        "seconds_before_output": delta,
                        "path": _clean(result.get("path")),
                        "result_preview": _result_preview(
                            resolve_project_path(root, _clean(result.get("path")))
                        ),
                    }
                )
            output_path = resolve_project_path(root, _clean(output.get("path")))
            cases.append(
                {
                    "case_id": f"{query_id}-v{version_number:03d}-{_clean(output.get('attachment_id'))}",
                    "query_id": query_id,
                    "version": version_number,
                    "sql_path": _clean(version.get("path")),
                    "sql_fingerprint": _clean(version.get("sql_fingerprint")),
                    "query_facts": {
                        "business_question": _clean(version.get("business_question") or entry.get("business_question")),
                        "metrics": version.get("metrics") or entry.get("metrics") or [],
                        "dimensions": version.get("dimensions") or entry.get("dimensions") or [],
                        "filters": version.get("filters") or entry.get("filters") or [],
                        "grain": _clean(version.get("grain") or entry.get("grain")),
                    },
                    "output": {
                        "attachment_id": _clean(output.get("attachment_id")),
                        "title": _clean(output.get("title")),
                        "purpose": _clean(output.get("purpose")),
                        "kind": _clean(output.get("kind")),
                        "created_at": _clean(output.get("created_at")),
                        "path": _clean(output.get("path")),
                        "workbook_sheets": _xlsx_sheets(output_path),
                    },
                    "same_version_results": sorted(
                        candidate_results,
                        key=lambda item: abs(item["seconds_before_output"])
                        if isinstance(item.get("seconds_before_output"), int)
                        else 10**12,
                    ),
                }
            )
    duplicate_groups = [
        {"source_sha256": digest, "copies": copies}
        for digest, copies in sorted(hashes.items())
        if len(copies) > 1
    ]
    return {
        "schema_version": INSPECTION_VERSION,
        "status": "needs_semantic_review" if cases else "clean",
        "project_id": _clean(index.get("project_id")),
        "project_root": str(root),
        "summary": {
            "ambiguous_output_count": len(cases),
            "duplicate_hash_group_count": len(duplicate_groups),
        },
        "cases": cases,
        "duplicate_hash_groups": duplicate_groups,
        "semantic_review_contract": {
            "instruction": "Use LLM reasoning to compare business meaning, key differences, coverage, transform safety, and risk. Discuss one case at a time with the user before writing a decision file.",
            "required_explanation": [
                "关键指标、筛选、粒度或展示差异",
                "旧资产是否被完整覆盖",
                "能否由已有结果无损推导",
                "判断错误会造成什么误导",
                "推荐保留、替代、绑定或继续待审的原因",
            ],
            "forbidden_shortcut": "Do not ask for confirmation using file names alone.",
        },
    }


def _validate_decision(decision: dict[str, Any], project_id: str) -> None:
    if decision.get("schema_version") != DECISION_VERSION:
        raise ValueError("Decision file must use result_lineage_decision_v1.")
    if decision.get("status") != "user_confirmed":
        raise ValueError("Decision file must be explicitly user_confirmed.")
    if _clean(decision.get("project_id")) != project_id:
        raise ValueError("Decision project_id does not match the query workspace.")
    if not _clean(decision.get("user_request")) or not _clean(decision.get("confirmed_at")):
        raise ValueError("Decision file must retain the user request and confirmation time.")
    actions = decision.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("Decision file must contain at least one action.")
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("Every lineage action must be an object.")
        semantic = action.get("semantic_review")
        if not isinstance(semantic, dict):
            raise ValueError("Every lineage action requires semantic_review evidence.")
        differences = semantic.get("key_differences")
        required_text = ("summary", "risk_if_wrong", "recommendation", "confidence", "coverage")
        if not isinstance(differences, list) or not differences or any(not _clean(value) for value in differences):
            raise ValueError("semantic_review.key_differences must explain at least one concrete difference.")
        if any(not _clean(semantic.get(key)) for key in required_text) or not _clean(action.get("user_confirmation")):
            raise ValueError("Semantic review and user confirmation cannot be empty or name-only.")
        if _clean(action.get("action")) == "adopt_bundle":
            if _clean(action.get("lineage_status")) != "exact_results":
                raise ValueError("adopt_bundle requires lineage_status=exact_results.")
            bundle_results = action.get("bundle_results")
            if not isinstance(bundle_results, dict) or set(bundle_results) != {"grouped", "overall"}:
                raise ValueError("adopt_bundle requires exact grouped and overall result selectors.")
            if not _clean(action.get("bundle")) or not isinstance(action.get("target"), dict):
                raise ValueError("adopt_bundle requires one existing bundle and one reusable target output.")


def _refresh_maps(index: dict[str, Any]) -> tuple[
    dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]],
    dict[tuple[str, int, str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
]:
    return _version_map(index), _output_map(index)


def _write_with_deletions(files: dict[Path, str | bytes], delete_paths: list[Path]) -> None:
    moved: dict[Path, Path] = {}
    try:
        for path in delete_paths:
            if not path.exists():
                continue
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.lineage-delete")
            os.replace(path, temp)
            moved[path] = temp
        _write_transaction(files)
    except Exception:
        for original, temp in moved.items():
            if temp.exists():
                os.replace(temp, original)
        raise
    for temp in moved.values():
        temp.unlink(missing_ok=True)


def apply_decisions(root: Path, decision_file: Path, *, dry_run: bool) -> dict[str, Any]:
    root = root.resolve()
    decision = read_json(decision_file.resolve(), {})
    index = copy.deepcopy(load_index(root))
    project_id = _clean(index.get("project_id"))
    _validate_decision(decision, project_id)
    touched: set[tuple[str, int]] = set()
    delete_paths: list[Path] = []
    action_results: list[dict[str, Any]] = []
    bundle_updates: dict[Path, dict[str, Any]] = {}

    for action in decision["actions"]:
        action_type = _clean(action.get("action"))
        action_id = _clean(action.get("action_id"))
        versions, outputs = _refresh_maps(index)
        if action_type == "register_result":
            target = action.get("target") or {}
            version_key = (_clean(target.get("query_id")), int(target.get("version") or 0))
            found = versions.get(version_key)
            if not found:
                raise ValueError(f"register_result target version not found: {version_key}")
            entry, version = found
            path_text = _clean(action.get("existing_result_path"))
            result_path = resolve_project_path(root, path_text)
            retained = prepare_result_evidence(result_path)
            if retained.retention.get("is_sliced") or retained.stored_sha256 != retained.retention.get("source_sha256"):
                raise ValueError("Registering an existing result in place is allowed only for a full result at or below 10 MB.")
            attachment_id = f"qwo-{retained.retention['source_sha256'][:12]}"
            if attachment_id != _clean(target.get("attachment_id")):
                raise ValueError("register_result target attachment_id does not match the existing file SHA-256.")
            existing_result = next(
                (
                    item
                    for item in version.get("derived_outputs", [])
                    if isinstance(item, dict) and item.get("attachment_id") == attachment_id
                ),
                None,
            )
            if existing_result:
                if existing_result.get("kind") != "result_evidence" or _clean(existing_result.get("path")) != path_text:
                    raise ValueError(f"Existing attachment conflicts with the requested result: {attachment_id}")
                existing_result["asset_state"] = ACTIVE_STATE
                existing_result["source_result_id"] = attachment_id
                existing_result["lineage_status"] = "result_evidence"
                existing_result["source_results"] = [_result_reference(entry, version, existing_result)]
                touched.add(version_key)
                action_results.append({"action_id": action_id, "action": action_type, "status": "planned" if dry_run else "applied"})
                continue
            created_at = now_iso()
            output = {
                "attachment_id": attachment_id,
                "kind": "result_evidence",
                "source_kind": "user_result",
                "title": _clean(action.get("title")),
                "purpose": _clean(action.get("purpose")),
                "path": path_text,
                "original_file_name": result_path.name,
                "media_type": retained.media_type or mimetypes.guess_type(result_path.name)[0] or "application/octet-stream",
                "sha256": retained.stored_sha256,
                "source_sha256": _clean(retained.retention.get("source_sha256")),
                "retention": retained.retention,
                "asset_state": ACTIVE_STATE,
                "source_sql_fingerprint": _clean(version.get("sql_fingerprint")),
                "source_result_id": attachment_id,
                "lineage_status": "result_evidence",
                "related_queries": [],
                "generation_provenance": build_generation_provenance(
                    generator_script="result_lineage_organizer.py",
                    workflow="register_existing_result_evidence",
                    artifact_kind="QUERY_DERIVED_OUTPUT",
                    generated_at=created_at,
                    source="user_confirmed_historical_organization",
                ),
                "created_at": created_at,
            }
            output["source_results"] = [_result_reference(entry, version, output)]
            version.setdefault("derived_outputs", []).append(output)
            touched.add(version_key)
        elif action_type == "bind":
            entry, version, output = _find_output(index, action.get("target") or {})
            refs = [
                _result_reference(*_find_result(index, selector))
                for selector in action.get("source_results", [])
            ]
            if not refs:
                raise ValueError(f"bind action requires source_results: {action_id}")
            lineage_status = _clean(action.get("lineage_status"))
            output["asset_state"] = ACTIVE_STATE
            output.pop("state_reason", None)
            output.pop("superseded_by", None)
            output["source_results"] = refs
            output["lineage_status"] = lineage_status
            same_version = all(
                ref["query_id"] == _clean(entry.get("query_id"))
                and ref["version"] == int(version.get("version") or 0)
                for ref in refs
            )
            if lineage_status == "exact_result" and len(refs) == 1 and same_version:
                output["source_result_id"] = refs[0]["result_id"]
            else:
                output.pop("source_result_id", None)
            if lineage_status == "deterministic_transform":
                transform = action.get("transformation") or {}
                output["transformation"] = {
                    "contract_version": "result_transformation_v1",
                    "kind": _clean(transform.get("kind")),
                    "description": _clean(transform.get("description")),
                    "equivalence_preserved": bool(transform.get("equivalence_preserved")),
                    "user_confirmed": True,
                    "user_confirmation": _clean(action.get("user_confirmation")),
                }
            else:
                output.pop("transformation", None)
            touched.add((_clean(entry.get("query_id")), int(version.get("version") or 0)))
        elif action_type == "adopt_bundle":
            bundle_path, loaded_bundle = load_analysis_bundle(root, _clean(action.get("bundle")))
            bundle = bundle_updates.get(bundle_path, copy.deepcopy(loaded_bundle))
            if bundle.get("schema_version") != "query_analysis_bundle_v1":
                raise ValueError("adopt_bundle requires one query_analysis_bundle_v1 asset.")
            members = {
                _clean(item.get("role")): item
                for item in bundle.get("members", [])
                if isinstance(item, dict)
            }
            if set(members) != {"grouped", "overall"}:
                raise ValueError("Analysis bundle must contain exactly grouped and overall members.")

            target_entry, target_version, output = _find_output(index, action.get("target") or {})
            if output.get("kind") not in BUNDLE_OUTPUT_KINDS:
                raise ValueError("adopt_bundle target must be a reusable visualization or analysis workbook.")
            target_identity = (
                _clean(target_entry.get("query_id")),
                int(target_version.get("version") or 0),
            )
            member_identities = {
                (_clean(item.get("query_id")), int(item.get("version") or 0))
                for item in members.values()
            }
            if target_identity not in member_identities:
                raise ValueError("adopt_bundle target output must be attached to one exact bundle member.")

            existing_bindings = (
                bundle.get("result_bindings")
                if isinstance(bundle.get("result_bindings"), dict)
                else {}
            )
            existing_visualization = (
                bundle.get("visualization")
                if isinstance(bundle.get("visualization"), dict)
                else {}
            )

            refs: list[dict[str, Any]] = []
            result_bindings: dict[str, dict[str, Any]] = {}
            for role in ("grouped", "overall"):
                selector = (action.get("bundle_results") or {}).get(role) or {}
                result_entry, result_version, result = _find_result(index, selector)
                result_identity = (
                    _clean(result_entry.get("query_id")),
                    int(result_version.get("version") or 0),
                )
                member = members[role]
                if result_identity != (
                    _clean(member.get("query_id")),
                    int(member.get("version") or 0),
                ):
                    raise ValueError(f"{role} result does not belong to the exact {role} bundle member.")
                columns = _result_columns(root, result)
                actual_columns = {_field_key(item) for item in columns}
                missing = [
                    str(item)
                    for item in member.get("expected_fields", [])
                    if _field_key(item) not in actual_columns
                ]
                if missing:
                    raise ValueError(f"{role} result is missing expected SQL fields: {', '.join(missing)}")
                refs.append(_result_reference(result_entry, result_version, result))
                binding = {
                    "query_id": result_identity[0],
                    "version": result_identity[1],
                    "result_id": _clean(result.get("attachment_id")),
                    "path": _clean(result.get("path")),
                    "source_sha256": _clean(result.get("source_sha256") or result.get("sha256")),
                    "columns": columns,
                    "attached_at": now_iso(),
                }
                existing_binding = existing_bindings.get(role)
                if isinstance(existing_binding, dict) and existing_binding:
                    immutable_keys = ("query_id", "version", "result_id", "path", "source_sha256")
                    if any(existing_binding.get(key) != binding.get(key) for key in immutable_keys):
                        raise ValueError(
                            f"Analysis bundle already has a different immutable {role} result binding."
                        )
                    binding["attached_at"] = _clean(existing_binding.get("attached_at")) or binding["attached_at"]
                result_bindings[role] = binding

            output["asset_state"] = ACTIVE_STATE
            output.pop("state_reason", None)
            output.pop("superseded_by", None)
            output.pop("source_result_id", None)
            output.pop("transformation", None)
            output["source_results"] = refs
            output["lineage_status"] = "exact_results"
            output["analysis_bundle"] = _bundle_output_reference(root, bundle_path, bundle)
            related = [item for item in output.get("related_queries", []) if isinstance(item, dict)]
            for member in members.values():
                reference = {
                    "query_id": _clean(member.get("query_id")),
                    "version": int(member.get("version") or 0),
                    "path": _clean(member.get("path")),
                    "sql_fingerprint": _clean(member.get("sql_fingerprint")),
                }
                if (
                    (reference["query_id"], reference["version"]) != target_identity
                    and reference not in related
                ):
                    related.append(reference)
            output["related_queries"] = related

            visualization = {
                "query_id": target_identity[0],
                "version": target_identity[1],
                "attachment_id": _clean(output.get("attachment_id")),
                "path": _clean(output.get("path")),
                "kind": _clean(output.get("kind")),
                "source_result_ids": [item["result_id"] for item in refs],
            }
            if existing_visualization:
                immutable_keys = ("query_id", "version", "attachment_id", "path")
                if any(existing_visualization.get(key) != visualization.get(key) for key in immutable_keys):
                    raise ValueError("Analysis bundle already points to a different immutable visualization.")
                existing_kind = _clean(existing_visualization.get("kind"))
                if existing_kind and existing_kind != visualization["kind"]:
                    raise ValueError("Analysis bundle visualization kind conflicts with the selected output.")
                existing_sources = {
                    _clean(item) for item in existing_visualization.get("source_result_ids", [])
                }
                if existing_sources and existing_sources != set(visualization["source_result_ids"]):
                    raise ValueError("Analysis bundle visualization already uses different source results.")

            bundle["result_bindings"] = result_bindings
            bundle["visualization"] = visualization
            bundle["status"] = "visualized"
            bundle["updated_at"] = now_iso()
            bundle_updates[bundle_path] = bundle
            touched.add(target_identity)
        elif action_type in {"supersede", "discard"}:
            entry, version, output = _find_output(index, action.get("target") or {})
            output["asset_state"] = "superseded" if action_type == "supersede" else "discarded"
            output["state_reason"] = _clean(action["semantic_review"].get("summary"))
            if action_type == "supersede":
                replacement = _find_output(index, action.get("canonical_output") or {})
                output["superseded_by"] = [_output_reference(*replacement)]
            else:
                output.pop("superseded_by", None)
            touched.add((_clean(entry.get("query_id")), int(version.get("version") or 0)))
        elif action_type == "deduplicate":
            target_entry, target_version, target_output = _find_output(index, action.get("target") or {})
            _, _, canonical = _find_output(index, action.get("canonical_output") or {})
            target_hash = _clean(target_output.get("source_sha256") or target_output.get("sha256"))
            canonical_hash = _clean(canonical.get("source_sha256") or canonical.get("sha256"))
            if not target_hash or target_hash != canonical_hash:
                raise ValueError("deduplicate requires byte-identical source hashes.")
            target_version["derived_outputs"] = [
                item for item in target_version.get("derived_outputs", []) if item is not target_output
            ]
            target_path = resolve_project_path(root, _clean(target_output.get("path")))
            canonical_path = resolve_project_path(root, _clean(canonical.get("path")))
            if target_path != canonical_path:
                delete_paths.append(target_path)
            touched.add((_clean(target_entry.get("query_id")), int(target_version.get("version") or 0)))
        elif action_type == "remove_duplicate_file":
            duplicate_path = resolve_project_path(root, _clean(action.get("duplicate_path")))
            _, _, canonical = _find_output(index, action.get("canonical_output") or {})
            indexed_paths = {
                resolve_project_path(root, _clean(item.get("path")))
                for _, _, item in _output_map(index).values()
            }
            if duplicate_path in indexed_paths:
                raise ValueError("remove_duplicate_file accepts only an unindexed duplicate; use deduplicate for indexed outputs.")
            if not duplicate_path.is_file():
                raise ValueError(f"Duplicate file not found: {duplicate_path}")
            from result_evidence_retention import file_sha256  # noqa: PLC0415

            canonical_hash = _clean(canonical.get("sha256"))
            if file_sha256(duplicate_path) != canonical_hash:
                raise ValueError("Unindexed duplicate does not match the canonical output hash.")
            delete_paths.append(duplicate_path)
        else:
            raise ValueError(f"Unsupported lineage action: {action_type}")
        action_results.append({"action_id": action_id, "action": action_type, "status": "planned" if dry_run else "applied"})

    updated_at = now_iso()
    versions = _version_map(index)
    files: dict[Path, str | bytes] = {}
    for query_id, version_number in touched:
        entry, version = versions[(query_id, version_number)]
        entry["derived_output_count"] = sum(
            len(item.get("derived_outputs", []))
            for item in entry.get("versions", [])
            if isinstance(item, dict) and isinstance(item.get("derived_outputs"), list)
        )
        entry["updated_at"] = updated_at
        version["updated_at"] = updated_at
        meta_path = resolve_project_path(root, _clean(version.get("meta_path")))
        meta = read_json(meta_path, {})
        meta["derived_outputs"] = copy.deepcopy(version.get("derived_outputs", []))
        meta["updated_at"] = updated_at
        files[meta_path] = json_text(meta)

    for bundle_path, bundle in bundle_updates.items():
        files[bundle_path] = json_text(bundle)

    audit_rel = Path("query_workspace") / "lineage_decisions" / f"{_clean(decision.get('decision_id'))}.json"
    files[root / audit_rel] = json_text(decision)
    files.update(_index_files(root, index))
    if not dry_run:
        _write_with_deletions(files, sorted(set(delete_paths)))
    return {
        "schema_version": APPLY_RECEIPT_VERSION,
        "status": "planned" if dry_run else "applied",
        "project_id": project_id,
        "decision_id": _clean(decision.get("decision_id")),
        "decision_path": audit_rel.as_posix(),
        "action_count": len(action_results),
        "touched_version_count": len(touched),
        "deleted_duplicate_file_count": len(set(delete_paths)),
        "actions": action_results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Build deterministic evidence for LLM and human review")
    inspect.add_argument("--root", required=True)
    inspect.add_argument("--format", choices=["json", "text"], default="json")
    apply = sub.add_parser("apply", help="Apply one user-confirmed result-lineage decision file")
    apply.add_argument("--root", required=True)
    apply.add_argument("--decision-file", required=True)
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--format", choices=["json", "text"], default="json")
    add_function_gate_arguments(
        apply,
        selection_help="Use [RESULT_LINEAGE_ORGANIZATION], [PROJECT_ADMIN], or [SKILL_EVOLUTION].",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "inspect":
            result = inspect_lineage(Path(args.root))
        else:
            require_user_request(args.user_request, purpose="result-lineage decision application")
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=command_function_ids("result_lineage_organizer.py", args.command),
                purpose="result-lineage decision application",
            )
            result = apply_decisions(Path(args.root), Path(args.decision_file), dry_run=bool(args.dry_run))
    except (FunctionGateError, ValueError, OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, FunctionGateError):
            return exit_with_gate_error(exc)
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
