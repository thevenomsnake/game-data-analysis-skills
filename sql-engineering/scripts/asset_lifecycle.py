#!/usr/bin/env python3
"""High-level scan, plan, and one-candidate Workspace promotion lifecycle."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import shutil
from typing import Any

from formal_asset_repository import (
    apply_plan as apply_repository_plan,
    list_packages,
    load_package,
    load_receipt,
    plan_package,
    validate_receipt,
)
from promotion_ledger import (
    DECISIONS,
    PromotionLedgerError,
    build_content_snapshot,
    canonical_json,
    decision_record,
    load_ledger,
    now_iso,
    record_decision,
    review_state,
)
from sql_query_workspace import load_index as load_workspace_index
from sql_facts import execution_fingerprint
from workspace_inventory import scan as scan_unregistered_workspace


SCAN_SCHEMA_VERSION = "asset_lifecycle_scan_v2"
PLAN_SCHEMA_VERSION = "promotion_plan_v1"
APPLY_SCHEMA_VERSION = "asset_lifecycle_apply_receipt_v1"
BATCH_PLAN_SCHEMA_VERSION = "asset_lifecycle_batch_plan_v1"
BATCH_RECEIPT_SCHEMA_VERSION = "asset_lifecycle_batch_receipt_v1"
CLOSEOUT_PLAN_SCHEMA_VERSION = "asset_lifecycle_closeout_plan_v1"
READ_MODEL_REFRESH_SCHEMA_VERSION = "shared_asset_read_models_refresh_v1"
READ_MODEL_REFRESH_REL = Path("_asset_catalog") / "refresh_receipt.json"
PROMOTABLE_STATUSES = {"runnable", "result_confirmed"}
WORKSPACE_ROLES = {"query", "dashboard_delivery", "unknown"}


class AssetLifecycleError(ValueError):
    """Raised when a lifecycle operation cannot prove its managed inputs."""


def _project_id(project_root: Path) -> str:
    config_path = project_root / "project_config.json"
    if config_path.is_file():
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssetLifecycleError(f"Project configuration is unreadable: {config_path}") from exc
        if not isinstance(value, dict):
            raise AssetLifecycleError("Project configuration must be a JSON object.")
        return str(value.get("project_id") or project_root.name)
    return project_root.name


def _normalized_query_id(value: str) -> str:
    query_id = str(value or "").strip().lower()
    if not re.fullmatch(r"qw-[a-z0-9-]{8,120}", query_id):
        raise AssetLifecycleError(f"Invalid Workspace query id: {value}")
    return query_id


def _load_entries(project_root: Path) -> list[dict[str, Any]]:
    index_path = project_root / "query_workspace" / "index.json"
    if not index_path.is_file():
        raise AssetLifecycleError(f"Query Workspace index is missing: {index_path}")
    try:
        index = load_workspace_index(project_root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AssetLifecycleError("Query Workspace index could not be loaded safely.") from exc
    entries = index.get("entries") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        raise AssetLifecycleError("Query Workspace index entries must be an array.")
    return [item for item in entries if isinstance(item, dict)]


def _resolve_candidate(
    project_root: Path,
    *,
    query_id: str,
    version: int = 0,
    entries: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    normalized_id = _normalized_query_id(query_id)
    workspace_entries = entries if entries is not None else _load_entries(project_root)
    matches = [item for item in workspace_entries if item.get("query_id") == normalized_id]
    if len(matches) != 1:
        raise AssetLifecycleError(
            f"Expected exactly one indexed Workspace query `{normalized_id}`, found {len(matches)}."
        )
    entry = matches[0]
    resolved_version = int(version or entry.get("current_version") or 0)
    versions = [
        item
        for item in entry.get("versions", [])
        if isinstance(item, dict) and int(item.get("version") or 0) == resolved_version
    ]
    if len(versions) != 1:
        raise AssetLifecycleError(
            f"Expected one indexed version for `{normalized_id}` v{resolved_version:03d}, found {len(versions)}."
        )
    workspace_version = copy.deepcopy(versions[0])
    version_query_id = str(workspace_version.get("query_id") or normalized_id)
    if version_query_id != normalized_id:
        raise AssetLifecycleError("Workspace entry and immutable version query ids do not match.")
    workspace_version["query_id"] = normalized_id
    try:
        snapshot = build_content_snapshot(project_root, workspace_version)
    except PromotionLedgerError as exc:
        raise AssetLifecycleError(str(exc)) from exc
    return entry, workspace_version, snapshot


def _candidate_view(
    entry: dict[str, Any],
    version: dict[str, Any],
    snapshot: dict[str, Any],
    ledger: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    state = review_state(ledger, snapshot)
    outputs = version.get("derived_outputs", [])
    result_count = sum(
        1
        for item in outputs
        if isinstance(item, dict) and item.get("kind") == "result_evidence"
    )
    indexed_sql = next(
        (item for item in snapshot.get("members", []) if item.get("role") == "indexed_sql"),
        {},
    )
    fact_bundle = version.get("sql_fact_bundle") if isinstance(version.get("sql_fact_bundle"), dict) else {}
    facts = version.get("facts") if isinstance(version.get("facts"), dict) else {}
    def fact_values(value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return sorted({str(item).strip() for item in values if str(item).strip()})

    relation_facts = {
        "sql_sha256": str(indexed_sql.get("sha256") or ""),
        "logic_fingerprint": str(
            version.get("logic_fingerprint")
            or fact_bundle.get("logic_fingerprint")
            or facts.get("logic_fingerprint")
            or ""
        ),
        "tables": fact_values(version.get("tables") or facts.get("tables")),
        "metrics": fact_values(version.get("metrics") or facts.get("metrics")),
        "dimensions": fact_values(version.get("dimensions") or facts.get("dimensions")),
        "filters": fact_values(version.get("filters") or facts.get("filters")),
    }
    workspace_role = str(
        version.get("workspace_role")
        or entry.get("workspace_role")
        or "unknown"
    ).strip().lower()
    if workspace_role not in WORKSPACE_ROLES:
        workspace_role = "unknown"
    workspace_status = str(version.get("status") or entry.get("status") or "draft")
    if workspace_status in {"promoted"} or version.get("formal_artifact_path") or entry.get("formal_artifacts"):
        formalization_assessment = "already_formalized"
    elif workspace_status in {"run_failed", "discarded", "archived", "superseded"}:
        formalization_assessment = "historical_local"
    elif workspace_role == "unknown":
        formalization_assessment = "blocked_missing_role"
    elif workspace_status == "draft":
        formalization_assessment = "blocked_draft"
    elif workspace_status not in PROMOTABLE_STATUSES:
        formalization_assessment = "blocked_status"
    elif version.get("delivery_ready") is not True:
        formalization_assessment = "blocked_validation"
    elif workspace_status == "result_confirmed" and result_count == 0:
        formalization_assessment = "blocked_missing_result"
    elif workspace_role == "dashboard_delivery" and not (
        isinstance(version.get("role_lineage"), dict)
        and str(version.get("role_lineage", {}).get("source_query_id") or "")
        and int(version.get("role_lineage", {}).get("source_query_version") or 0) > 0
    ):
        formalization_assessment = "blocked_missing_lineage"
    else:
        formalization_assessment = "ready"
    formal_paths = [
        str(version.get("formal_artifact_path") or ""),
        *(
            str(item)
            for item in entry.get("formal_artifacts", [])
            if isinstance(item, str)
        ),
    ]
    package_ids = sorted(
        {
            match.group(1)
            for path in formal_paths
            for match in [re.search(r"(?:^|/)formal_assets/(FA-\d{4,})-", path)]
            if match
        }
    )
    if not package_ids and workspace_status == "promoted" and project_root is not None:
        try:
            existing = _existing_promotion(project_root, version, snapshot)
            package_id = str(existing.get("manifest", {}).get("package_id") or "")
            if package_id:
                package_ids = [package_id]
        except (AssetLifecycleError, ValueError, OSError, json.JSONDecodeError):
            package_ids = []
    if package_ids:
        target_package = {
            "status": "existing",
            "package_id": package_ids[0] if len(package_ids) == 1 else "",
            "conflict": len(package_ids) > 1,
        }
    elif formalization_assessment == "ready":
        target_package = {"status": "proposed", "package_id": "", "conflict": False}
    elif formalization_assessment == "historical_local":
        target_package = {"status": "local_only", "package_id": "", "conflict": False}
    else:
        target_package = {"status": "blocked", "package_id": "", "conflict": False}
    return {
        "candidate_key": snapshot["candidate_key"],
        "query_id": snapshot["query_id"],
        "version": snapshot["version"],
        "title": str(version.get("title") or entry.get("title") or snapshot["query_id"]),
        "purpose": str(version.get("purpose") or entry.get("purpose") or ""),
        "status": workspace_status,
        "workspace_status": workspace_status,
        "workspace_role": workspace_role,
        "role_lineage": copy.deepcopy(version.get("role_lineage") or entry.get("role_lineage") or {}),
        "formalization_assessment": formalization_assessment,
        "target_package": target_package,
        "ledger_decision": state.get("prior_decision", ""),
        "delivery_ready": version.get("delivery_ready") is True,
        "verification_state": "verified" if result_count else "unverified",
        "registered_output_count": len(outputs),
        "result_evidence_count": result_count,
        "content_fingerprint": snapshot["content_fingerprint"],
        "members": copy.deepcopy(snapshot["members"]),
        "relationship_facts": relation_facts,
        **state,
    }


def _candidate_relationship(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_facts = left.get("relationship_facts") if isinstance(left.get("relationship_facts"), dict) else {}
    right_facts = right.get("relationship_facts") if isinstance(right.get("relationship_facts"), dict) else {}
    relation = "independent"
    evidence: list[str] = []
    recommendation = "review_independently"
    if left_facts.get("sql_sha256") and left_facts.get("sql_sha256") == right_facts.get("sql_sha256"):
        relation = "byte_identical"
        evidence.append("indexed SQL bytes are identical")
        recommendation = "strongly_recommend_one_shared_copy_after_confirmation"
    elif left_facts.get("logic_fingerprint") and left_facts.get("logic_fingerprint") == right_facts.get("logic_fingerprint"):
        relation = "same_logic"
        evidence.append("logic_fingerprint is identical")
        recommendation = "strongly_recommend_same_family_review"
    else:
        keys = ("tables", "metrics", "dimensions", "filters")
        left_sets = {key: set(left_facts.get(key) or []) for key in keys}
        right_sets = {key: set(right_facts.get(key) or []) for key in keys}
        comparable = any(left_sets[key] or right_sets[key] for key in keys)
        left_contains = comparable and all(right_sets[key] <= left_sets[key] for key in keys)
        right_contains = comparable and all(left_sets[key] <= right_sets[key] for key in keys)
        if left_contains ^ right_contains:
            relation = "strict_superset"
            superset = left if left_contains else right
            evidence.append(f"{superset['candidate_key']} contains every registered fact of the other candidate")
            recommendation = "recommend_superset_but_require_separate_value_confirmation"
        else:
            overlaps = {
                key: sorted(left_sets[key] & right_sets[key])
                for key in keys
                if left_sets[key] & right_sets[key]
            }
            if overlaps:
                relation = "partial_overlap"
                evidence.extend(f"shared {key}: {', '.join(values[:6])}" for key, values in overlaps.items())
                recommendation = "review_both_business_questions"
    return {
        "left_candidate_key": left["candidate_key"],
        "right_candidate_key": right["candidate_key"],
        "relation": relation,
        "evidence": evidence,
        "recommendation": recommendation,
    }


def _scan_relationships(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    relationships: list[dict[str, Any]] = []
    independent = 0
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            item = _candidate_relationship(left, right)
            if item["relation"] == "independent":
                independent += 1
            else:
                relationships.append(item)
    return relationships, independent


def _closeout_blockers(candidate: dict[str, Any]) -> list[str]:
    assessment = str(candidate.get("formalization_assessment") or "")
    blockers = {
        "blocked_missing_role": ["workspace_role"],
        "blocked_draft": ["status:runnable_or_result_confirmed"],
        "blocked_validation": ["delivery_ready"],
        "blocked_missing_result": ["result_evidence"],
        "blocked_missing_lineage": ["role_lineage"],
        "blocked_status": ["promotable_workspace_status"],
    }
    return list(blockers.get(assessment, []))


def _closeout_closure(candidate: dict[str, Any]) -> dict[str, Any]:
    members = [
        {
            "role": str(item.get("role") or ""),
            "path": str(item.get("path") or ""),
            "sha256": str(item.get("sha256") or ""),
        }
        for item in candidate.get("members", [])
        if isinstance(item, dict)
    ]
    members.sort(key=lambda item: (item["role"], item["path"]))
    complete = bool(members) and all(
        item["path"] and re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
        for item in members
    )
    return {
        "complete": complete,
        "member_count": len(members),
        "registered_output_count": int(candidate.get("registered_output_count") or 0),
        "members": members,
    }


def _closeout_proposal_id(candidate_keys: list[str]) -> str:
    digest = hashlib.sha256(canonical_json(sorted(candidate_keys)).encode("utf-8")).hexdigest()
    return f"proposal-{digest[:16]}"


def _closeout_package_proposals(
    candidates: list[dict[str, Any]],
    version_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    by_key = {
        str(item.get("candidate_key") or ""): item
        for item in [*candidates, *(version_rows or [])]
    }
    proposals: dict[str, dict[str, Any]] = {}
    assigned: set[str] = set()

    def proposal_for(member_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        keys = sorted(str(item["candidate_key"]) for item in member_candidates)
        proposal_id = _closeout_proposal_id(keys)
        proposal = proposals.get(proposal_id)
        if proposal is not None:
            return proposal
        closure_members = []
        for item in member_candidates:
            closure_members.extend(_closeout_closure(item)["members"])
        unique_members = {
            (str(item.get("role") or ""), str(item.get("path") or "")): item
            for item in closure_members
        }
        closure = {
            "complete": bool(unique_members)
            and all(
                re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256") or ""))
                for item in unique_members.values()
            ),
            "member_count": len(unique_members),
            "members": sorted(
                unique_members.values(),
                key=lambda item: (str(item.get("role") or ""), str(item.get("path") or "")),
            ),
        }
        proposal = {
            "proposal_id": proposal_id,
            "status": "proposed",
            "member_candidate_keys": keys,
            "member_roles": [
                {"candidate_key": str(item["candidate_key"]), "workspace_role": str(item.get("workspace_role") or "unknown")}
                for item in sorted(member_candidates, key=lambda row: str(row["candidate_key"]))
            ],
            "artifact_closure": closure,
            "blockers": [],
        }
        proposals[proposal_id] = proposal
        for item in member_candidates:
            assigned.add(str(item["candidate_key"]))
            item["target_package"] = {
                "status": "proposed",
                "package_id": "",
                "proposal_id": proposal_id,
                "conflict": False,
                "member_candidate_keys": keys,
                "member_roles": proposal["member_roles"],
                "artifact_closure": closure,
                "blockers": [],
            }
        return proposal

    for candidate in sorted(candidates, key=lambda item: str(item.get("candidate_key") or "")):
        key = str(candidate.get("candidate_key") or "")
        assessment = str(candidate.get("formalization_assessment") or "")
        if key in assigned or assessment in {"already_formalized", "historical_local"}:
            if assessment == "already_formalized":
                existing_target = candidate.get("target_package") if isinstance(candidate.get("target_package"), dict) else {}
                candidate["target_package"] = {
                    **existing_target,
                    "status": "existing",
                    "proposal_id": "",
                    "member_candidate_keys": [key],
                    "member_roles": [{"candidate_key": key, "workspace_role": candidate.get("workspace_role", "unknown")}],
                    "artifact_closure": _closeout_closure(candidate),
                    "blockers": [],
                }
            if assessment == "historical_local":
                candidate["target_package"] = {
                    "status": "local_only",
                    "package_id": "",
                    "proposal_id": "",
                    "conflict": False,
                    "member_candidate_keys": [key],
                    "member_roles": [{"candidate_key": key, "workspace_role": candidate.get("workspace_role", "unknown")}],
                    "artifact_closure": _closeout_closure(candidate),
                    "blockers": [],
                }
            continue
        if assessment != "ready":
            candidate["target_package"] = {
                "status": "blocked",
                "package_id": "",
                "proposal_id": "",
                "conflict": False,
                "member_candidate_keys": [key],
                "member_roles": [{"candidate_key": key, "workspace_role": candidate.get("workspace_role", "unknown")}],
                "artifact_closure": _closeout_closure(candidate),
                "blockers": _closeout_blockers(candidate),
            }
            continue
        if str(candidate.get("workspace_role") or "") == "dashboard_delivery":
            lineage = candidate.get("role_lineage") if isinstance(candidate.get("role_lineage"), dict) else {}
            source_id = str(lineage.get("source_query_id") or "").strip().lower()
            source_version = int(lineage.get("source_query_version") or 0)
            source_key = f"{source_id}@v{source_version:03d}"
            source = by_key.get(source_key)
            if (
                source is None
                or str(source.get("workspace_role") or "") != "query"
                or str(source.get("workspace_status") or "") in {"run_failed", "discarded", "superseded"}
            ):
                candidate["target_package"]["blockers"] = ["role_lineage.source_query_version"]
                continue
            relation = _candidate_relationship(source, candidate)
            if relation["relation"] == "independent":
                candidate["target_package"]["blockers"] = ["role_lineage.compatible_contract"]
                continue
            proposal_for([source, candidate])
            continue
        proposal_for([candidate])

    return sorted(proposals.values(), key=lambda item: str(item.get("proposal_id") or ""))


def scan(
    project_root: str | Path,
    *,
    query_id: str = "",
    version: int = 0,
) -> dict[str, Any]:
    """Scan indexed current versions, skipping unchanged candidates with a Ledger decision."""

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise AssetLifecycleError(f"Project root does not exist: {root}")
    project_id = _project_id(root)
    ledger = load_ledger(root, project_id=project_id)
    candidates: list[dict[str, Any]] = []
    if query_id:
        entries = _load_entries(root)
        entry, workspace_version, snapshot = _resolve_candidate(
            root, query_id=query_id, version=version, entries=entries
        )
        candidates.append(_candidate_view(entry, workspace_version, snapshot, ledger, root))
    else:
        if version:
            raise AssetLifecycleError("A version filter requires a query_id.")
        entries = _load_entries(root)
        seen: set[str] = set()
        for entry in entries:
            candidate_id = _normalized_query_id(str(entry.get("query_id") or ""))
            if candidate_id in seen:
                raise AssetLifecycleError(f"Duplicate Workspace query id: {candidate_id}")
            seen.add(candidate_id)
            _, workspace_version, snapshot = _resolve_candidate(
                root,
                query_id=candidate_id,
                version=int(entry.get("current_version") or 0),
                entries=entries,
            )
            candidates.append(_candidate_view(entry, workspace_version, snapshot, ledger, root))
    review_candidates = [item for item in candidates if item["requires_review"]]
    skipped_candidates = [item for item in candidates if not item["requires_review"]]
    relationships, independent_pair_count = _scan_relationships(review_candidates)
    return {
        "schema_version": SCAN_SCHEMA_VERSION,
        "project_id": project_id,
        "generated_at": now_iso(),
        "candidate_count": len(candidates),
        "review_required_count": len(review_candidates),
        "unchanged_skipped_count": len(skipped_candidates),
        "candidates": candidates,
        "relationships": relationships,
        "independent_pair_count": independent_pair_count,
        "unregistered_inventory": scan_unregistered_workspace(root),
    }


def closeout_plan(
    projects_root: str | Path,
    *,
    expected_family_count: int = 0,
    expected_project_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a repository-wide, read-only closeout plan for families and versions."""

    projects_path = Path(projects_root).resolve()
    if not projects_path.is_dir():
        raise AssetLifecycleError(f"Projects root does not exist: {projects_path}")
    project_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    unregistered: list[dict[str, Any]] = []
    for project_root in sorted(projects_path.iterdir()):
        if (
            not project_root.is_dir()
            or project_root.name.startswith("_")
            or not (project_root / "project_config.json").is_file()
            or not (project_root / "query_workspace" / "index.json").is_file()
        ):
            continue
        result = scan(project_root)
        index = load_workspace_index(project_root)
        entries = index.get("entries") if isinstance(index, dict) else []
        project_id = str(result["project_id"])
        project_candidates = [
            {"project_id": project_id, **copy.deepcopy(item)}
            for item in result.get("candidates", [])
            if isinstance(item, dict)
        ]
        project_version_rows: list[dict[str, Any]] = []
        ledger = load_ledger(project_root, project_id=project_id)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            query_id = _normalized_query_id(str(entry.get("query_id") or ""))
            entry_versions = [
                item
                for item in entry.get("versions", [])
                if isinstance(item, dict) and int(item.get("version") or 0) > 0
            ]
            for version_record in sorted(entry_versions, key=lambda item: int(item.get("version") or 0)):
                _, workspace_version, snapshot = _resolve_candidate(
                    project_root,
                    query_id=query_id,
                    version=int(version_record.get("version") or 0),
                    entries=entries,
                )
                version_view = _candidate_view(
                    entry,
                    workspace_version,
                    snapshot,
                    ledger,
                    project_root,
                )
                version_view["project_id"] = project_id
                version_view["is_current"] = int(version_view.get("version") or 0) == int(entry.get("current_version") or 0)
                project_version_rows.append(version_view)
        project_unregistered = []
        for item in result.get("unregistered_inventory", {}).get("files", []):
            if not isinstance(item, dict) or item.get("state") != "unregistered":
                continue
            # Keep only stable file facts in the digest. Scan timestamps and
            # inventory persistence state are local observation metadata.
            project_unregistered.append(
                {
                    "project_id": project_id,
                    "path": str(item.get("path") or ""),
                    "sha256": str(item.get("sha256") or ""),
                    "size_bytes": int(item.get("size_bytes") or 0),
                    "state": "unregistered",
                }
            )
        package_revisions = []
        for package_entry in list_packages(project_root):
            if not isinstance(package_entry, dict):
                continue
            revision = {
                "package_id": str(package_entry.get("package_id") or ""),
                "revision": int(package_entry.get("revision") or 0),
                "manifest_path": str(package_entry.get("manifest_path") or ""),
                "manifest_sha256": str(package_entry.get("manifest_sha256") or ""),
                "receipt_path": str(package_entry.get("latest_receipt") or ""),
            }
            receipt_path = project_root / Path(revision["receipt_path"])
            if receipt_path.is_file():
                revision["receipt_sha256"] = _sha256_file(receipt_path)
            else:
                revision["receipt_sha256"] = ""
            package_revisions.append(revision)
        package_revisions.sort(key=lambda item: str(item.get("package_id") or ""))
        project_version_count = sum(
            len(item.get("versions", []))
            for item in entries
            if isinstance(item, dict) and isinstance(item.get("versions"), list)
        )
        if project_version_count != len(project_version_rows):
            raise AssetLifecycleError(f"Workspace version count changed while planning {project_id}.")
        try:
            index_relative = (project_root / "query_workspace" / "index.json").relative_to(projects_path.parent).as_posix()
        except ValueError as exc:
            raise AssetLifecycleError("Workspace index escaped repository root.") from exc
        candidates.extend(project_candidates)
        versions.extend(project_version_rows)
        unregistered.extend(project_unregistered)
        project_rows.append(
            {
                "project_id": project_id,
                "index_path": index_relative,
                "index_sha256": _sha256_file(project_root / "query_workspace" / "index.json"),
                "family_count": int(result.get("candidate_count") or 0),
                "version_count": project_version_count,
                "review_required_count": int(result.get("review_required_count") or 0),
                "unregistered_count": len(project_unregistered),
                "package_revisions": package_revisions,
                "formal_index_sha256": _sha256_file(project_root / "formal_assets" / "index.json")
                if (project_root / "formal_assets" / "index.json").is_file()
                else "",
                "workspace_index_schema": str(index.get("schema_version") or ""),
            }
        )
    candidates.sort(key=lambda item: (str(item.get("project_id") or ""), str(item.get("candidate_key") or "")))
    versions.sort(key=lambda item: (str(item.get("project_id") or ""), str(item.get("candidate_key") or "")))
    unregistered.sort(key=lambda item: (str(item.get("project_id") or ""), str(item.get("path") or "")))
    family_identities = [f"{item.get('project_id')}:{item.get('query_id')}" for item in candidates]
    if len(family_identities) != len(set(family_identities)):
        raise AssetLifecycleError("Closeout plan contains duplicate repository-scoped family identities.")
    categories: dict[str, int] = {}
    for item in candidates:
        key = str(item.get("formalization_assessment") or "unknown")
        categories[key] = categories.get(key, 0) + 1
    package_proposals = _closeout_package_proposals(candidates, versions)
    family_count = len(candidates)
    project_baseline = {
        str(item.get("project_id") or ""): {
            "family_count": int(item.get("family_count") or 0),
            "version_count": int(item.get("version_count") or 0),
        }
        for item in project_rows
    }
    shared_projection_inputs = []
    for name in (
        "asset_catalog.json",
        "asset_organization.json",
        "asset_group_registry.json",
        "refresh_receipt.json",
    ):
        path = projects_path / "_asset_catalog" / name
        if path.is_file():
            shared_projection_inputs.append(
                {
                    "path": path.relative_to(projects_path.parent).as_posix(),
                    "sha256": _sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    current_project_counts = {
        str(item.get("project_id") or ""): int(item.get("family_count") or 0)
        for item in project_rows
    }
    expected_project_counts = {
        str(key): int(value)
        for key, value in (expected_project_counts or {}).items()
    }
    project_delta = {
        project_id: {
            "expected": expected_project_counts.get(project_id),
            "current": current_count,
            "delta": current_count - expected_project_counts[project_id],
        }
        for project_id, current_count in current_project_counts.items()
        if project_id in expected_project_counts and current_count != expected_project_counts[project_id]
    }
    missing_projects = sorted(set(expected_project_counts) - set(current_project_counts))
    if missing_projects:
        for project_id in missing_projects:
            project_delta[project_id] = {
                "expected": expected_project_counts[project_id],
                "current": 0,
                "delta": -expected_project_counts[project_id],
            }
    baseline_delta = {
        "expected_family_count": expected_family_count,
        "current_family_count": family_count,
        "family_count_delta": family_count - expected_family_count if expected_family_count else 0,
        "project_counts": project_delta,
        "expected_project_counts": expected_project_counts,
    }
    baseline_status = (
        "inventory_changed"
        if (expected_family_count and family_count != expected_family_count) or project_delta
        else "ready"
    )
    unsigned = {
        "schema_version": CLOSEOUT_PLAN_SCHEMA_VERSION,
        "projects": project_rows,
        "input_receipt": {
            "projects": copy.deepcopy(project_rows),
            "shared_projection_inputs": shared_projection_inputs,
            "family_identities": sorted(family_identities),
            "family_count": family_count,
            "version_count": len(versions),
        },
        "family_count": family_count,
        "version_count": len(versions),
        "candidates": candidates,
        "versions": versions,
        "package_proposals": package_proposals,
        "shared_projection_inputs": shared_projection_inputs,
        "unregistered": unregistered,
        "categories": dict(sorted(categories.items())),
        "human_summary": {
            "family_count": family_count,
            "version_count": len(versions),
            "project_baseline": project_baseline,
            "categories": dict(sorted(categories.items())),
            "unregistered_count": len(unregistered),
            "proposal_count": len(package_proposals),
        },
        "baseline_delta": baseline_delta,
    }
    plan_digest = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    return {
        **unsigned,
        "status": baseline_status,
        "generated_at": now_iso(),
        "expected_family_count": expected_family_count,
        "expected_project_counts": expected_project_counts,
        "plan_digest": plan_digest,
    }


def _promotion_eligibility(
    version: dict[str, Any],
    *,
    allow_unverified: bool,
) -> str:
    status = str(version.get("status") or "")
    if status not in PROMOTABLE_STATUSES:
        raise AssetLifecycleError(
            f"Only runnable or result_confirmed Workspace versions may be promoted, not `{status}`."
        )
    if version.get("delivery_ready") is not True:
        raise AssetLifecycleError("Promotion requires a delivery-ready Workspace version.")
    gate = version.get("generation_gate")
    if isinstance(gate, dict):
        blockers = gate.get("blockers", [])
        if gate.get("status") != "ok" or (isinstance(blockers, list) and blockers):
            raise AssetLifecycleError("Promotion is blocked by the Workspace generation gate.")
    outputs = version.get("derived_outputs", [])
    has_result = any(
        isinstance(item, dict) and item.get("kind") == "result_evidence" for item in outputs
    )
    if status == "result_confirmed" and not has_result:
        raise AssetLifecycleError("result_confirmed promotion requires registered result evidence.")
    if not has_result and allow_unverified is not True:
        raise AssetLifecycleError(
            "Promotion without result evidence requires explicit allow_unverified confirmation."
        )
    return "verified" if has_result else "unverified"


def _promotion_eligibility_for_legacy_promoted(
    version: dict[str, Any],
    *,
    allow_unverified: bool,
) -> str:
    """Validate a promoted row whose legacy Package no longer exists."""

    if str(version.get("status") or "") == "promoted":
        version = {**version, "status": "runnable"}
    return _promotion_eligibility(version, allow_unverified=allow_unverified)


def _existing_promotion(
    project_root: Path,
    workspace_version: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Resolve one valid existing Package for an already-promoted Workspace version."""

    query_id = str(workspace_version.get("query_id") or "")
    version = int(workspace_version.get("version") or 0)
    sql_fingerprint = str(workspace_version.get("sql_fingerprint") or "")
    sql_sha256 = next(
        (
            str(item.get("sha256") or "")
            for item in snapshot.get("members", [])
            if isinstance(item, dict) and item.get("role") == "indexed_sql"
        ),
        "",
    )
    workspace_sql_path = project_root / Path(str(workspace_version.get("path") or ""))
    try:
        workspace_sql_fingerprint = execution_fingerprint(
            workspace_sql_path.read_text(encoding="utf-8-sig")
        )
    except OSError as exc:
        raise AssetLifecycleError(
            f"Promoted Workspace SQL is unreadable: {workspace_version.get('path') or ''}"
        ) from exc
    if workspace_sql_fingerprint != sql_fingerprint:
        raise AssetLifecycleError(
            "Promoted Workspace SQL no longer matches its indexed execution fingerprint."
        )
    matches: list[dict[str, Any]] = []
    for package_entry in list_packages(project_root):
        package_id = str(package_entry.get("package_id") or "")
        manifest = load_package(project_root, package_id)
        members = [item for item in manifest.get("members", []) if isinstance(item, dict)]
        by_path = {str(item.get("path") or ""): item for item in members}
        matched_paths: tuple[str, str] | None = None
        for member in members:
            if member.get("role") != "formal_query_meta":
                continue
            relative = str(member.get("path") or "")
            sidecar_path = (project_root / Path(relative)).resolve()
            try:
                sidecar_path.relative_to(project_root)
            except ValueError as exc:
                raise AssetLifecycleError(
                    f"Formal query metadata escaped the project root: {relative}"
                ) from exc
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AssetLifecycleError(
                    f"Formal query metadata is unreadable: {relative}"
                ) from exc
            origin = sidecar.get("origin_query_workspace")
            has_explicit_origin = isinstance(origin, dict)
            if not has_explicit_origin:
                origin = sidecar
            if (
                str(origin.get("query_id") or "") != query_id
                or int(origin.get("version") or 0) != version
                or str(origin.get("path") or "") != str(workspace_version.get("path") or "")
                or str(origin.get("meta_path") or "")
                != str(workspace_version.get("meta_path") or "")
            ):
                continue
            origin_fingerprint = str(
                origin.get("source_sql_fingerprint") or origin.get("sql_fingerprint") or ""
            )
            if origin_fingerprint and origin_fingerprint != sql_fingerprint:
                continue
            sql_relative = relative.removesuffix(".meta.json") + ".sql"
            sql_member = by_path.get(sql_relative)
            if not isinstance(sql_member, dict):
                continue
            formal_sql_sha256 = str(sql_member.get("sha256") or "")
            if has_explicit_origin:
                formal_sql_path = (project_root / Path(sql_relative)).resolve()
                try:
                    formal_sql_fingerprint = execution_fingerprint(
                        formal_sql_path.read_text(encoding="utf-8-sig")
                    )
                except OSError as exc:
                    raise AssetLifecycleError(
                        f"Formal query SQL is unreadable: {sql_relative}"
                    ) from exc
                if (
                    not origin_fingerprint
                    or formal_sql_fingerprint != origin_fingerprint
                    or formal_sql_fingerprint != workspace_sql_fingerprint
                ):
                    continue
            elif formal_sql_sha256 != sql_sha256:
                continue
            matched_paths = (relative, sql_relative)
            break
        if matched_paths is None:
            continue
        receipt = load_receipt(project_root, package_id)
        validation = validate_receipt(project_root, receipt)
        receipt_paths = {
            str(item.get("path") or "")
            for item in receipt.get("files", [])
            if isinstance(item, dict)
        }
        if validation.get("status") != "valid" or not set(matched_paths) <= receipt_paths:
            raise AssetLifecycleError(
                f"Existing promotion receipt is invalid or incomplete: {package_id}"
            )
        matches.append({"manifest": manifest, "receipt": receipt})
    if len(matches) != 1:
        raise AssetLifecycleError(
            f"Expected one valid existing Package for promoted Workspace query "
            f"`{query_id}` v{version:03d}, found {len(matches)}."
        )
    return matches[0]


def _existing_promotion_or_none(
    project_root: Path,
    workspace_version: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    """Treat a missing legacy Package as a new-promotion recovery case.

    A promoted Workspace row can predate Formal Asset Package manifests and retain
    only a stale legacy path. Missing Packages are rebuilt from the immutable
    Workspace closure; any other repository-integrity error remains blocking.
    """

    try:
        return _existing_promotion(project_root, workspace_version, snapshot)
    except AssetLifecycleError as exc:
        if "found 0" in str(exc):
            return None
        raise


def plan(
    project_root: str | Path,
    *,
    query_id: str,
    version: int = 0,
    decision: str,
    reason: str,
    user_request: str,
    confirmed_by_user: bool,
    confirmed_by: str = "user",
    allow_unverified: bool = False,
    package_id: str = "",
    package_title: str = "",
    missing_conditions: list[str] | None = None,
    revisit_when: str = "",
    _entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one final, explicitly confirmed Promotion Plan without writing any files."""

    root = Path(project_root).resolve()
    project_id = _project_id(root)
    entry, workspace_version, snapshot = _resolve_candidate(
        root, query_id=query_id, version=version, entries=_entries
    )
    ledger = load_ledger(root, project_id=project_id)
    state = review_state(ledger, snapshot)
    if not state["requires_review"]:
        raise AssetLifecycleError(
            "This candidate is unchanged and already has a Promotion Ledger decision."
        )
    if decision not in DECISIONS:
        raise AssetLifecycleError(f"Unsupported promotion decision: {decision}")
    confirmed_at = now_iso()
    try:
        confirmed_decision = decision_record(
            snapshot,
            decision=decision,
            reason=reason,
            user_request=user_request,
            confirmed_by_user=confirmed_by_user,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            missing_conditions=missing_conditions,
            revisit_when=revisit_when,
        )
    except PromotionLedgerError as exc:
        raise AssetLifecycleError(str(exc)) from exc

    verification_state = "not_applicable"
    resolved_package_id = str(package_id or "").strip()
    existing_package: dict[str, Any] | None = None
    if decision == "promote":
        if str(workspace_version.get("status") or "") == "promoted":
            existing_package = _existing_promotion_or_none(root, workspace_version, snapshot)
            if existing_package is not None:
                existing_package_id = str(existing_package["manifest"].get("package_id") or "")
                if resolved_package_id and resolved_package_id != existing_package_id:
                    raise AssetLifecycleError(
                        "Promoted Workspace query belongs to a different Formal Asset Package."
                    )
                resolved_package_id = existing_package_id
        if existing_package is None:
            verification_state = _promotion_eligibility_for_legacy_promoted(
                workspace_version, allow_unverified=allow_unverified
            )
            if not resolved_package_id and state["prior_decision"] == "promote":
                prior = ledger["entries"].get(snapshot["candidate_key"], {})
                receipt = prior.get("repository_receipt") if isinstance(prior, dict) else None
                if isinstance(receipt, dict):
                    resolved_package_id = str(receipt.get("package_id") or "")
    elif package_id or package_title:
        raise AssetLifecycleError("Only a promote decision may target a Formal Asset Package.")
    if decision != "promote" and allow_unverified:
        raise AssetLifecycleError("allow_unverified applies only to promote decisions.")

    candidate = _candidate_view(entry, workspace_version, snapshot, ledger)
    resolved_package_title = str(package_title or "").strip()
    if existing_package is not None:
        existing_title = str(existing_package["manifest"].get("title") or "").strip()
        if resolved_package_title and resolved_package_title != existing_title:
            raise AssetLifecycleError(
                "Promoted Workspace query cannot rename its existing Formal Asset Package."
            )
        resolved_package_title = existing_title
    if resolved_package_id and not resolved_package_title:
        try:
            existing_package = load_package(root, resolved_package_id)
        except ValueError:
            existing_package = {}
        resolved_package_title = str(existing_package.get("title") or "").strip()
    if not resolved_package_title:
        resolved_package_title = candidate["title"]
    promotion_plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "project_id": project_id,
        "created_at": confirmed_at,
        "candidate": candidate,
        "decision": confirmed_decision,
        "verification_state": verification_state,
        "allow_unverified": bool(allow_unverified),
        "missing_conditions": sorted(
            {str(item).strip() for item in (missing_conditions or []) if str(item).strip()}
        ),
        "revisit_when": str(revisit_when or "").strip(),
        "repository_target": {
            "package_id": resolved_package_id,
            "title": resolved_package_title,
        },
    }
    promotion_plan["plan_fingerprint"] = hashlib.sha256(
        canonical_json(promotion_plan).encode("utf-8")
    ).hexdigest()
    return promotion_plan


build_plan = plan


def _validate_plan_shape(promotion_plan: dict[str, Any]) -> None:
    if not isinstance(promotion_plan, dict) or promotion_plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise AssetLifecycleError("Unsupported or malformed Promotion Plan.")
    supplied_fingerprint = str(promotion_plan.get("plan_fingerprint") or "")
    unsigned = {key: value for key, value in promotion_plan.items() if key != "plan_fingerprint"}
    actual_fingerprint = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if supplied_fingerprint != actual_fingerprint:
        raise AssetLifecycleError("Promotion Plan fingerprint mismatch.")
    candidate = promotion_plan.get("candidate")
    decision = promotion_plan.get("decision")
    if not isinstance(candidate, dict) or not isinstance(decision, dict):
        raise AssetLifecycleError("Promotion Plan requires one candidate and one decision.")
    if (
        candidate.get("candidate_key") != decision.get("candidate_key")
        or candidate.get("content_fingerprint") != decision.get("content_fingerprint")
    ):
        raise AssetLifecycleError("Promotion Plan candidate and decision do not identify the same content.")


def _safe_output_name(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    suffix = "".join(path.suffixes).lower()
    stem = path.name[: -len(suffix)] if suffix else path.name
    clean_stem = re.sub(r"[^a-zA-Z0-9]+", "-", stem).strip("-")[:48] or "output"
    path_digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12]
    return f"{path_digest}-{clean_stem}{suffix}"


def _repository_member_inputs(
    project_root: Path,
    promotion_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    candidate = promotion_plan["candidate"]
    query_id = str(candidate["query_id"])
    version = int(candidate["version"])
    verification_state = str(promotion_plan.get("verification_state") or "")
    desired: list[dict[str, Any]] = []
    for member in candidate.get("members", []):
        if not isinstance(member, dict):
            raise AssetLifecycleError("Promotion Plan member snapshot contains a non-object.")
        role = str(member.get("role") or "")
        relative = str(member.get("path") or "")
        source_path = (project_root / Path(relative)).resolve()
        if role == "indexed_sql":
            target_path = f"queries/{query_id}/v{version:03d}.sql"
            repository_role = (
                "formal_query" if verification_state == "verified" else "formal_query_unverified"
            )
        elif role == "query_meta":
            target_path = f"queries/{query_id}/v{version:03d}.meta.json"
            repository_role = "formal_query_meta"
        elif role == "query_spec":
            target_path = f"queries/{query_id}/v{version:03d}.spec.json"
            repository_role = "formal_query_spec"
        elif role == "registered_output":
            target_path = (
                f"outputs/{query_id}/v{version:03d}/{_safe_output_name(relative)}"
            )
            repository_role = str(member.get("kind") or "registered_output")
        else:
            raise AssetLifecycleError(f"Unsupported promotion member role: {role}")
        desired.append(
            {
                "source_path": source_path,
                "target_path": target_path,
                "role": repository_role,
                "lifecycle_state": "current",
                "sha256": str(member.get("sha256") or ""),
            }
        )

    package_id = str((promotion_plan.get("repository_target") or {}).get("package_id") or "")
    if not package_id:
        return [{key: value for key, value in item.items() if key != "sha256"} for item in desired]
    try:
        manifest = load_package(project_root, package_id)
    except ValueError as exc:
        if "not indexed" in str(exc):
            # Batch plans reserve IDs before the Package directory exists.
            return [{key: value for key, value in item.items() if key != "sha256"} for item in desired]
        raise AssetLifecycleError(f"Target Formal Asset Package cannot be loaded: {package_id}") from exc
    directory = str(manifest.get("directory") or "")
    existing_members = [item for item in manifest.get("members", []) if isinstance(item, dict)]
    owned_path_markers = (
        f"/members/queries/{query_id}/",
        f"/members/outputs/{query_id}/",
    )
    existing_by_path = {str(item.get("path") or ""): item for item in existing_members}
    selected_ids: set[str] = set()
    repository_inputs: list[dict[str, Any]] = []
    for item in desired:
        full_path = f"{directory}/members/{item['target_path']}"
        existing = existing_by_path.get(full_path)
        if existing is None:
            repository_inputs.append({key: value for key, value in item.items() if key != "sha256"})
            continue
        if existing.get("sha256") != item["sha256"]:
            raise AssetLifecycleError(
                f"A promoted member changed bytes at an immutable package path: {item['target_path']}"
            )
        if existing.get("lifecycle_state") != "current":
            raise AssetLifecycleError(
                f"A historical package member cannot be reactivated: {existing.get('member_id')}"
            )
        selected_ids.add(str(existing["member_id"]))
        repository_inputs.append(
            {"member_id": existing["member_id"], "lifecycle_state": "current"}
        )
    for existing in existing_members:
        member_id = str(existing.get("member_id") or "")
        belongs_to_candidate = any(
            marker in str(existing.get("path") or "") for marker in owned_path_markers
        )
        if (
            belongs_to_candidate
            and existing.get("lifecycle_state") == "current"
            and member_id not in selected_ids
        ):
            repository_inputs.append({"member_id": member_id, "lifecycle_state": "history"})
    return repository_inputs


def _repository_plan(project_root: Path, promotion_plan: dict[str, Any]):
    target = promotion_plan.get("repository_target")
    if not isinstance(target, dict):
        raise AssetLifecycleError("Promotion Plan repository_target must be an object.")
    members = _repository_member_inputs(project_root, promotion_plan)
    try:
        return plan_package(
            project_root,
            title=str(target.get("title") or ""),
            members=members,
            package_id=str(target.get("package_id") or "") or None,
            lifecycle_state="current",
        )
    except ValueError as exc:
        raise AssetLifecycleError("Formal Asset Repository rejected the Promotion Plan.") from exc


def apply(
    project_root: str | Path,
    promotion_plan: dict[str, Any],
    *,
    dry_run: bool = False,
    _entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply one confirmed decision; promotion delegates all formal writes to the repository."""

    _validate_plan_shape(promotion_plan)
    root = Path(project_root).resolve()
    project_id = _project_id(root)
    if promotion_plan.get("project_id") != project_id:
        raise AssetLifecycleError("Promotion Plan belongs to a different project.")
    candidate = promotion_plan["candidate"]
    _, workspace_version, current_snapshot = _resolve_candidate(
        root,
        query_id=str(candidate.get("query_id") or ""),
        version=int(candidate.get("version") or 0),
        entries=_entries,
    )
    candidate_identity = {
        "candidate_key": candidate.get("candidate_key"),
        "query_id": candidate.get("query_id"),
        "version": candidate.get("version"),
        "content_fingerprint": candidate.get("content_fingerprint"),
        "members": candidate.get("members"),
    }
    current_identity = {
        "candidate_key": current_snapshot["candidate_key"],
        "query_id": current_snapshot["query_id"],
        "version": current_snapshot["version"],
        "content_fingerprint": current_snapshot["content_fingerprint"],
        "members": current_snapshot["members"],
    }
    if candidate_identity != current_identity:
        raise AssetLifecycleError("Workspace candidate changed after planning; rescan and reconfirm it.")
    decision = promotion_plan["decision"]
    if decision.get("members") != current_snapshot["members"]:
        raise AssetLifecycleError("Promotion decision member closure does not match the Workspace candidate.")
    try:
        decision_record(
            current_snapshot,
            decision=str(decision.get("decision") or ""),
            reason=str(decision.get("reason") or ""),
            user_request=str(decision.get("user_request") or ""),
            confirmed_by_user=decision.get("confirmed_by_user") is True,
            confirmed_by=str(decision.get("confirmed_by") or "user"),
            confirmed_at=str(decision.get("confirmed_at") or ""),
            missing_conditions=decision.get("missing_conditions") if isinstance(decision.get("missing_conditions"), list) else [],
            revisit_when=str(decision.get("revisit_when") or ""),
        )
    except PromotionLedgerError as exc:
        raise AssetLifecycleError(str(exc)) from exc

    ledger = load_ledger(root, project_id=project_id)
    state = review_state(ledger, current_snapshot)
    if not state["requires_review"]:
        prior = ledger["entries"][current_snapshot["candidate_key"]]
        if prior.get("decision") != decision.get("decision"):
            raise AssetLifecycleError(
                "The unchanged candidate already has a different Promotion Ledger decision."
            )
        return {
            "schema_version": APPLY_SCHEMA_VERSION,
            "status": "unchanged_skipped",
            "project_id": project_id,
            "candidate_key": current_snapshot["candidate_key"],
            "decision": prior["decision"],
            "workspace_unchanged": True,
            "ledger_receipt": {},
            "repository_receipt": copy.deepcopy(prior.get("repository_receipt") or {}),
            "sync_ready": prior["decision"] == "promote" and bool(prior.get("repository_receipt")),
        }

    action = str(decision["decision"])
    repository_plan = None
    repository_receipt: dict[str, Any] | None = None
    if action == "promote":
        if str(workspace_version.get("status") or "") == "promoted":
            existing = _existing_promotion_or_none(root, workspace_version, current_snapshot)
            if existing is not None:
                repository_receipt = copy.deepcopy(existing["receipt"])
                target_package_id = str(
                    (promotion_plan.get("repository_target") or {}).get("package_id") or ""
                )
                if target_package_id != repository_receipt.get("package_id"):
                    raise AssetLifecycleError(
                        "Promotion Plan does not target the candidate's existing Formal Asset Package."
                    )
        if repository_receipt is None:
            _promotion_eligibility_for_legacy_promoted(
                workspace_version,
                allow_unverified=promotion_plan.get("allow_unverified") is True,
            )
            repository_plan = _repository_plan(root, promotion_plan)
    if dry_run:
        after = build_content_snapshot(root, workspace_version)
        if after != current_snapshot:
            raise AssetLifecycleError("Workspace members changed during dry-run planning.")
        return {
            "schema_version": APPLY_SCHEMA_VERSION,
            "status": "dry_run",
            "project_id": project_id,
            "candidate_key": current_snapshot["candidate_key"],
            "decision": action,
            "workspace_unchanged": True,
            "ledger_receipt": {},
            "repository_receipt": copy.deepcopy(repository_receipt or {}),
            "repository_plan": repository_plan.as_dict() if repository_plan else {},
            "sync_ready": False,
        }

    if repository_plan is not None:
        try:
            repository_receipt = apply_repository_plan(repository_plan)
            validation = validate_receipt(root, repository_receipt)
        except ValueError as exc:
            raise AssetLifecycleError("Formal Asset Repository promotion failed.") from exc
        if validation.get("status") != "valid":
            raise AssetLifecycleError("Formal Asset Repository returned an invalid receipt.")
    after = build_content_snapshot(root, workspace_version)
    if after != current_snapshot:
        raise AssetLifecycleError("Workspace members changed while applying the Promotion Plan.")
    try:
        ledger_receipt = record_decision(
            root,
            current_snapshot,
            decision=action,
            reason=str(decision["reason"]),
            user_request=str(decision["user_request"]),
            confirmed_by_user=True,
            confirmed_by=str(decision.get("confirmed_by") or "user"),
            confirmed_at=str(decision.get("confirmed_at") or ""),
            project_id=project_id,
            repository_receipt=repository_receipt,
            missing_conditions=decision.get("missing_conditions") if isinstance(decision.get("missing_conditions"), list) else [],
            revisit_when=str(decision.get("revisit_when") or ""),
        )
    except PromotionLedgerError as exc:
        raise AssetLifecycleError("Promotion Ledger rejected the applied decision.") from exc
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "status": "applied",
        "project_id": project_id,
        "candidate_key": current_snapshot["candidate_key"],
        "decision": action,
        "workspace_unchanged": True,
        "ledger_receipt": ledger_receipt,
        "repository_receipt": repository_receipt or {},
        "sync_ready": action == "promote" and repository_receipt is not None,
    }


def dry_run_apply(project_root: str | Path, promotion_plan: dict[str, Any]) -> dict[str, Any]:
    return apply(project_root, promotion_plan, dry_run=True)


def _batch_fingerprint(value: dict[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "plan_fingerprint"}
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def build_batch_plan(
    project_root: str | Path,
    decisions: list[dict[str, Any]],
    *,
    closeout_plan_digest: str = "",
    closeout_family_count: int = 0,
    closeout_expected_project_counts: dict[str, int] | None = None,
    confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one final plan covering every candidate that currently requires review."""

    root = Path(project_root).resolve()
    scan_result = scan(root)
    review_candidates = [
        item for item in scan_result["candidates"] if item.get("requires_review") is True
    ]
    expected = {str(item["candidate_key"]): item for item in review_candidates}
    supplied: dict[str, dict[str, Any]] = {}
    for item in decisions:
        if not isinstance(item, dict):
            raise AssetLifecycleError("Batch decisions must be objects.")
        query_id = _normalized_query_id(str(item.get("query_id") or ""))
        version = int(item.get("version") or 0)
        candidate_key = f"{query_id}@v{version:03d}"
        if candidate_key in supplied:
            raise AssetLifecycleError(f"Duplicate batch decision: {candidate_key}")
        supplied[candidate_key] = dict(item)
    missing = sorted(set(expected) - set(supplied))
    extra = sorted(set(supplied) - set(expected))
    if missing or extra:
        raise AssetLifecycleError(
            f"Final Promotion Plan must decide every review candidate; missing={missing}, extra={extra}"
        )

    from formal_asset_repository import list_packages

    existing_numbers = [
        int(str(item.get("package_id") or "FA-0000").split("-")[-1])
        for item in list_packages(root)
    ]
    next_package_number = max(existing_numbers, default=0) + 1
    workspace_entries = _load_entries(root)
    candidate_plans: list[dict[str, Any]] = []
    for candidate_key in sorted(supplied):
        item = supplied[candidate_key]
        package_id = str(item.get("package_id") or "")
        if item.get("decision") == "promote" and not package_id:
            _, workspace_version, snapshot = _resolve_candidate(
                root,
                query_id=str(item["query_id"]),
                version=int(item["version"]),
                entries=workspace_entries,
            )
            existing = (
                _existing_promotion_or_none(root, workspace_version, snapshot)
                if str(workspace_version.get("status") or "") == "promoted"
                else None
            )
            if existing is not None:
                package_id = str(existing["manifest"].get("package_id") or "")
            else:
                # Reserve IDs for legacy promoted rows whose old Package is gone
                # as well as for new promotions; apply never allocates dynamically.
                package_id = f"FA-{next_package_number:04d}"
                next_package_number += 1
        candidate_plans.append(
            plan(
                root,
                query_id=str(item["query_id"]),
                version=int(item["version"]),
                decision=str(item.get("decision") or ""),
                reason=str(item.get("reason") or ""),
                user_request=str(item.get("user_request") or ""),
                confirmed_by_user=item.get("confirmed_by_user") is True,
                confirmed_by=str(item.get("confirmed_by") or "user"),
                allow_unverified=item.get("allow_unverified") is True,
                package_id=package_id,
                package_title=str(item.get("package_title") or ""),
                missing_conditions=item.get("missing_conditions") if isinstance(item.get("missing_conditions"), list) else [],
                revisit_when=str(item.get("revisit_when") or ""),
                _entries=workspace_entries,
            )
        )
    result = {
        "schema_version": BATCH_PLAN_SCHEMA_VERSION,
        "project_id": scan_result["project_id"],
        "created_at": now_iso(),
        "scan": scan_result,
        "candidate_plans": candidate_plans,
        "closeout_plan_digest": str(closeout_plan_digest or ""),
        "closeout_family_count": int(closeout_family_count or 0),
        "closeout_expected_project_counts": {
            str(key): int(value)
            for key, value in (closeout_expected_project_counts or {}).items()
        },
        "confirmation": copy.deepcopy(confirmation or {}),
        "summary": {
            "decision_count": len(candidate_plans),
            "promote_count": sum(
                item.get("decision", {}).get("decision") == "promote" for item in candidate_plans
            ),
            "deferred_count": sum(
                item.get("decision", {}).get("decision") == "deferred" for item in candidate_plans
            ),
            "excluded_count": sum(
                item.get("decision", {}).get("decision") == "excluded" for item in candidate_plans
            ),
            "unchanged_skipped_count": scan_result["unchanged_skipped_count"],
        },
    }
    result["plan_fingerprint"] = _batch_fingerprint(result)
    return result


def _validate_batch_plan(batch_plan: dict[str, Any]) -> None:
    if not isinstance(batch_plan, dict) or batch_plan.get("schema_version") != BATCH_PLAN_SCHEMA_VERSION:
        raise AssetLifecycleError("Unsupported Asset Lifecycle batch plan.")
    if batch_plan.get("plan_fingerprint") != _batch_fingerprint(batch_plan):
        raise AssetLifecycleError("Asset Lifecycle batch plan fingerprint mismatch.")
    plans = batch_plan.get("candidate_plans")
    if not isinstance(plans, list):
        raise AssetLifecycleError("Asset Lifecycle batch plan candidate_plans must be an array.")
    for item in plans:
        _validate_plan_shape(item)


def _closeout_plan_digest(plan_payload: dict[str, Any]) -> str:
    unsigned = {
        key: value
        for key, value in plan_payload.items()
        if key
        not in {
            "status",
            "generated_at",
            "expected_family_count",
            "expected_project_counts",
            "plan_digest",
        }
    }
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def _validate_closeout_confirmation(
    root: Path,
    batch_plan: dict[str, Any],
    *,
    closeout_plan_data: dict[str, Any] | None,
    confirmation: dict[str, Any] | None,
) -> dict[str, Any]:
    expected_digest = str(batch_plan.get("closeout_plan_digest") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        raise AssetLifecycleError("Asset Lifecycle apply requires an exact closeout plan digest.")
    supplied = confirmation if isinstance(confirmation, dict) else {}
    if str(supplied.get("plan_digest") or "") != expected_digest:
        raise AssetLifecycleError("Closeout confirmation digest does not match the batch plan.")
    confirmation_text = str(supplied.get("confirmation_text") or "").strip()
    confirmer = str(supplied.get("confirmed_by") or "").strip()
    confirmed_at = str(supplied.get("confirmed_at") or "").strip()
    if len(confirmation_text) < 12 or expected_digest not in confirmation_text:
        raise AssetLifecycleError("Closeout confirmation text must name the exact plan digest.")
    if not confirmer or not confirmed_at:
        raise AssetLifecycleError("Closeout confirmation requires confirmer identity and time.")
    if closeout_plan_data is not None:
        if str(closeout_plan_data.get("plan_digest") or "") != expected_digest:
            raise AssetLifecycleError("Closeout plan file does not match the confirmed digest.")
        if _closeout_plan_digest(closeout_plan_data) != expected_digest:
            raise AssetLifecycleError("Closeout plan file has been altered after planning.")
    expected_family_count = int(batch_plan.get("closeout_family_count") or 0)
    expected_project_counts = closeout_plan_data.get("expected_project_counts") if isinstance(closeout_plan_data, dict) else None
    if not isinstance(expected_project_counts, dict):
        expected_project_counts = batch_plan.get("closeout_expected_project_counts")
    if not isinstance(expected_project_counts, dict):
        expected_project_counts = {}
    if expected_family_count < 1:
        raise AssetLifecycleError("Closeout confirmation requires the planned family count.")
    fresh = closeout_plan(
        root.parent,
        expected_family_count=expected_family_count,
        expected_project_counts={str(key): int(value) for key, value in expected_project_counts.items()},
    )
    if fresh.get("status") != "ready" or fresh.get("plan_digest") != expected_digest:
        raise AssetLifecycleError("plan_stale: Workspace or shared inputs changed after closeout planning.")
    project_id = _project_id(root)
    project_row = next(
        (item for item in fresh.get("projects", []) if isinstance(item, dict) and item.get("project_id") == project_id),
        None,
    )
    expected_project_family_count = expected_project_counts.get(project_id)
    if project_row is None or (
        expected_project_family_count is not None
        and int(project_row.get("family_count") or 0) != int(expected_project_family_count)
    ):
        raise AssetLifecycleError(
            "closeout apply rejected: current project family count does not match the confirmed baseline."
        )
    return {
        "plan_digest": expected_digest,
        "confirmation_text": confirmation_text,
        "confirmed_by": confirmer,
        "confirmed_at": confirmed_at,
        "fresh_plan_digest": str(fresh.get("plan_digest") or ""),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _snapshot_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _restore_path(source: Path, destination: Path) -> None:
    if destination.is_dir():
        shutil.rmtree(destination)
    elif destination.exists():
        destination.unlink()
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _read_model_refresh_receipt(
    repo_root: Path,
    refresh_result: dict[str, Any],
) -> dict[str, Any]:
    files = []
    for name, key in (
        ("catalog", "catalog_path"),
        ("organization", "organization_path"),
        ("asset_group_registry", "registry_path"),
    ):
        path = Path(str(refresh_result.get(key) or "")).resolve()
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise AssetLifecycleError(f"Shared read model escaped the repository: {path}") from exc
        if not path.is_file():
            raise AssetLifecycleError(f"Shared read model was not written: {path}")
        files.append(
            {
                "name": name,
                "path": relative,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return {
        "schema_version": READ_MODEL_REFRESH_SCHEMA_VERSION,
        "status": "ready",
        "source_model": str(refresh_result.get("source_model") or "formal_asset_packages_v1"),
        "source_snapshot": copy.deepcopy(refresh_result.get("source_snapshot") or {}),
        "refreshed_at": now_iso(),
        "files": files,
        "warning_count": int(refresh_result.get("warning_count") or 0),
        "warnings": list(refresh_result.get("warnings") or []),
    }


def _batch_run_receipt_path(root: Path, batch_plan: dict[str, Any]) -> Path:
    digest = str(batch_plan.get("plan_fingerprint") or "")
    closeout_digest = str(batch_plan.get("closeout_plan_digest") or "")
    run_id = hashlib.sha256(f"{digest}:{closeout_digest}".encode("utf-8")).hexdigest()[:20]
    return root / "query_workspace" / "asset_lifecycle_runs" / f"{run_id}.json"


def _write_batch_run_receipt(path: Path, payload: dict[str, Any]) -> None:
    _write_json_atomic(path, payload)


def arrange_and_sync(
    project_root: str | Path,
    batch_plan: dict[str, Any],
    *,
    dry_run: bool = False,
    sync: bool = False,
    user_request: str = "",
    function_selection: str | None = None,
    closeout_plan_data: dict[str, Any] | None = None,
    confirmation: dict[str, Any] | None = None,
    repository_id: str = "",
) -> dict[str, Any]:
    """Apply one confirmed closeout batch and publish a validated Provider Snapshot."""

    _validate_batch_plan(batch_plan)
    if sync:
        raise AssetLifecycleError("Push is a separate Git transport action; arrange-sync cannot push.")
    root = Path(project_root).resolve()
    if batch_plan.get("project_id") != _project_id(root):
        raise AssetLifecycleError("Asset Lifecycle batch plan belongs to a different project.")
    workspace_entries = _load_entries(root)
    if dry_run:
        receipts = [
            apply(root, item, dry_run=True, _entries=workspace_entries)
            for item in batch_plan["candidate_plans"]
        ]
        return {
            "schema_version": BATCH_RECEIPT_SCHEMA_VERSION,
            "status": "dry_run",
            "project_id": batch_plan["project_id"],
            "candidate_receipts": receipts,
            "shared_read_models_refreshed": False,
            "sync_ready": False,
        }

    projects_root = root.parent
    repo_root = projects_root.parent
    confirmation_record = _validate_closeout_confirmation(
        root,
        batch_plan,
        closeout_plan_data=closeout_plan_data,
        confirmation=confirmation or batch_plan.get("confirmation"),
    )
    provider_id = str(repository_id or "").strip()
    if not provider_id:
        provider_manifest_path = projects_root / "_asset_catalog" / "provider_manifest.json"
        if provider_manifest_path.is_file():
            try:
                provider_id = str(json.loads(provider_manifest_path.read_text(encoding="utf-8")).get("repository_id") or "")
            except (OSError, json.JSONDecodeError):
                provider_id = ""
    if not provider_id:
        raise AssetLifecycleError("A stable repository_id is required to publish the Provider Snapshot.")
    run_receipt_path = _batch_run_receipt_path(root, batch_plan)
    prior_run_receipt: dict[str, Any] = {}
    if run_receipt_path.is_file():
        try:
            loaded_receipt = json.loads(run_receipt_path.read_text(encoding="utf-8"))
            if isinstance(loaded_receipt, dict) and loaded_receipt.get("plan_fingerprint") == batch_plan.get("plan_fingerprint"):
                prior_run_receipt = loaded_receipt
        except (OSError, json.JSONDecodeError):
            prior_run_receipt = {}
    run_receipt = {
        "schema_version": BATCH_RECEIPT_SCHEMA_VERSION,
        "run_status": "started",
        "project_id": batch_plan["project_id"],
        "plan_fingerprint": batch_plan["plan_fingerprint"],
        "closeout_confirmation": confirmation_record,
        "candidate_keys": [
            str(item.get("candidate_key") or "")
            for item in batch_plan.get("candidate_plans", [])
            if isinstance(item, dict)
        ],
        "candidate_receipts": [],
        "completed_candidate_keys": [],
        "resumable": True,
        "updated_at": now_iso(),
    }
    if prior_run_receipt.get("run_status") == "failed":
        run_receipt["resumed_from"] = run_receipt_path.relative_to(repo_root).as_posix()
        run_receipt["prior_completed_candidate_keys"] = list(
            prior_run_receipt.get("completed_candidate_keys") or []
        )
    _write_batch_run_receipt(run_receipt_path, run_receipt)
    backup = root / ".asset-lifecycle-batch-backup"
    if backup.exists():
        raise AssetLifecycleError(f"Asset Lifecycle backup already exists: {backup}")
    backup.mkdir(parents=True)
    snapshot_targets = [
        root / "formal_assets",
        root / "manifest.json",
        root / "index.md",
        root / "query_workspace" / "promotion_ledger.json",
        projects_root / "_asset_catalog" / "asset_catalog.json",
        projects_root / "_asset_catalog" / "asset_organization.json",
        projects_root / "_asset_catalog" / "asset_group_registry.json",
        projects_root / READ_MODEL_REFRESH_REL,
        projects_root / "_asset_catalog" / "provider_snapshot.json",
        projects_root / "_asset_catalog" / "provider_manifest.json",
    ]
    snapshot_entries: list[tuple[Path, Path, bool]] = []
    for index, target in enumerate(snapshot_targets):
        snapshot = backup / f"snapshot-{index:02d}"
        existed = target.exists()
        if existed:
            _snapshot_path(target, snapshot)
        snapshot_entries.append((snapshot, target, existed))

    try:
        candidate_receipts: list[dict[str, Any]] = []
        for item in batch_plan["candidate_plans"]:
            receipt = apply(root, item, _entries=workspace_entries)
            candidate_receipts.append(receipt)
            run_receipt["candidate_receipts"] = copy.deepcopy(candidate_receipts)
            run_receipt["completed_candidate_keys"] = [
                str(row.get("candidate_key") or "")
                for row in candidate_receipts
                if isinstance(row, dict)
            ]
            run_receipt["updated_at"] = now_iso()
            _write_batch_run_receipt(run_receipt_path, run_receipt)
        from sql_project import rebuild_index

        rebuild_index(root)
        from asset_catalog import refresh_shared_asset_read_models

        refresh_result = refresh_shared_asset_read_models(projects_root)
        if refresh_result.get("status") not in {"pass", "warn"}:
            raise AssetLifecycleError("Shared Asset Read Models did not complete successfully.")
        refresh_receipt = _read_model_refresh_receipt(repo_root, refresh_result)
        refresh_receipt_path = projects_root / READ_MODEL_REFRESH_REL
        _write_json_atomic(refresh_receipt_path, refresh_receipt)

        from asset_provider import build_snapshot

        provider_snapshot = build_snapshot(
            projects_root,
            repository_id=provider_id,
        )
        import collaboration_submit

        sync_plan = collaboration_submit.build_plan(repo_root)
        result = {
            "schema_version": BATCH_RECEIPT_SCHEMA_VERSION,
            "status": "arranged",
            "project_id": batch_plan["project_id"],
            "candidate_receipts": candidate_receipts,
            "package_receipts": [
                item.get("repository_receipt")
                for item in candidate_receipts
                if item.get("repository_receipt")
            ],
            "project_index_refreshed": True,
            "shared_read_models_refreshed": True,
            "shared_read_models_receipt": refresh_receipt,
            "provider_snapshot": provider_snapshot,
            "closeout_confirmation": confirmation_record,
            "collaboration_plan": sync_plan,
            "sync_ready": sync_plan.get("status") == "ready",
            "run_receipt_path": run_receipt_path.relative_to(repo_root).as_posix(),
        }
        run_receipt.update(
            {
                "run_status": "completed",
                "provider_snapshot": provider_snapshot,
                "project_index_refreshed": True,
                "shared_read_models_refreshed": True,
                "updated_at": now_iso(),
            }
        )
        _write_batch_run_receipt(run_receipt_path, run_receipt)
        shutil.rmtree(backup)
        return result
    except Exception as exc:
        for snapshot, target, existed in reversed(snapshot_entries):
            if existed:
                _restore_path(snapshot, target)
            elif target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        run_receipt.update(
            {
                "run_status": "failed",
                "error": str(exc),
                "updated_at": now_iso(),
            }
        )
        _write_batch_run_receipt(run_receipt_path, run_receipt)
        if backup.exists():
            shutil.rmtree(backup)
        raise


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetLifecycleError(f"Promotion Plan is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise AssetLifecycleError("Promotion Plan file must contain a JSON object.")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--root", required=True)
    scan_parser.add_argument("--query-id", default="")
    scan_parser.add_argument("--version", type=int, default=0)

    inventory_parser = subparsers.add_parser("inventory", help="Record unregistered local Workspace files")
    inventory_parser.add_argument("--root", required=True)
    inventory_parser.add_argument("--write", action="store_true")

    closeout_parser = subparsers.add_parser(
        "closeout-plan", help="Build a read-only plan across all project Workspaces"
    )
    closeout_parser.add_argument("--projects-root", required=True)
    closeout_parser.add_argument("--expected-family-count", type=int, default=0)
    closeout_parser.add_argument(
        "--expected-project-count",
        action="append",
        default=[],
        metavar="PROJECT=COUNT",
        help="Expected baseline family count per project; repeat for each project.",
    )

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--root", required=True)
    plan_parser.add_argument("--query-id", required=True)
    plan_parser.add_argument("--version", type=int, default=0)
    plan_parser.add_argument("--decision", choices=sorted(DECISIONS), required=True)
    plan_parser.add_argument("--reason", required=True)
    plan_parser.add_argument("--user-request", required=True)
    plan_parser.add_argument("--confirmed", action="store_true")
    plan_parser.add_argument("--confirmed-by", default="user")
    plan_parser.add_argument("--allow-unverified", action="store_true")
    plan_parser.add_argument("--package-id", default="")
    plan_parser.add_argument("--package-title", default="")
    plan_parser.add_argument("--missing-condition", action="append", default=[])
    plan_parser.add_argument("--revisit-when", default="")

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--root", required=True)
    apply_parser.add_argument("--plan-json", type=Path, required=True)
    apply_parser.add_argument("--dry-run", action="store_true")
    batch_parser = subparsers.add_parser("batch-plan")
    batch_parser.add_argument("--root", required=True)
    batch_parser.add_argument("--decisions-json", type=Path, required=True)
    batch_parser.add_argument("--closeout-plan", type=Path, default=None)
    arrange_parser = subparsers.add_parser("arrange-sync")
    arrange_parser.add_argument("--root", required=True)
    arrange_parser.add_argument("--plan-json", type=Path, required=True)
    arrange_parser.add_argument("--dry-run", action="store_true")
    arrange_parser.add_argument("--sync", action="store_true")
    arrange_parser.add_argument("--user-request", default="")
    arrange_parser.add_argument("--function-selection", default=None)
    arrange_parser.add_argument("--closeout-plan", type=Path, default=None)
    arrange_parser.add_argument("--confirmation-text", default="")
    arrange_parser.add_argument("--confirmed-by", default="")
    arrange_parser.add_argument("--confirmed-at", default="")
    arrange_parser.add_argument("--repository-id", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "scan":
        result = scan(args.root, query_id=args.query_id, version=args.version)
    elif args.command == "inventory":
        from workspace_inventory import write_inventory

        report = scan_unregistered_workspace(args.root)
        result = write_inventory(args.root, report) if args.write else report
    elif args.command == "closeout-plan":
        expected_project_counts: dict[str, int] = {}
        for value in args.expected_project_count:
            if "=" not in value:
                raise AssetLifecycleError("Expected project count must use PROJECT=COUNT.")
            project_id, count = value.split("=", 1)
            try:
                expected_project_counts[project_id.strip()] = int(count)
            except ValueError as exc:
                raise AssetLifecycleError("Expected project count must use an integer COUNT.") from exc
        result = closeout_plan(
            args.projects_root,
            expected_family_count=args.expected_family_count,
            expected_project_counts=expected_project_counts,
        )
    elif args.command == "plan":
        result = plan(
            args.root,
            query_id=args.query_id,
            version=args.version,
            decision=args.decision,
            reason=args.reason,
            user_request=args.user_request,
            confirmed_by_user=bool(args.confirmed),
            confirmed_by=args.confirmed_by,
            allow_unverified=bool(args.allow_unverified),
            package_id=args.package_id,
            package_title=args.package_title,
            missing_conditions=args.missing_condition,
            revisit_when=args.revisit_when,
        )
    elif args.command == "apply":
        result = apply(args.root, _read_plan(args.plan_json), dry_run=bool(args.dry_run))
    elif args.command == "batch-plan":
        decisions_payload = _read_plan(args.decisions_json)
        decisions = decisions_payload.get("decisions")
        if not isinstance(decisions, list):
            raise AssetLifecycleError("Batch decisions file requires a decisions array.")
        closeout_payload = _read_plan(args.closeout_plan) if args.closeout_plan else {}
        result = build_batch_plan(
            args.root,
            decisions,
            closeout_plan_digest=str(closeout_payload.get("plan_digest") or decisions_payload.get("closeout_plan_digest") or ""),
            closeout_family_count=int(closeout_payload.get("family_count") or decisions_payload.get("closeout_family_count") or 0),
            closeout_expected_project_counts=closeout_payload.get("expected_project_counts")
            if isinstance(closeout_payload.get("expected_project_counts"), dict)
            else decisions_payload.get("closeout_expected_project_counts")
            if isinstance(decisions_payload.get("closeout_expected_project_counts"), dict)
            else {},
            confirmation=decisions_payload.get("confirmation") if isinstance(decisions_payload.get("confirmation"), dict) else {},
        )
    else:
        batch_plan = _read_plan(args.plan_json)
        closeout_payload = _read_plan(args.closeout_plan) if args.closeout_plan else None
        confirmation = {
            "plan_digest": str(batch_plan.get("closeout_plan_digest") or ""),
            "confirmation_text": args.confirmation_text,
            "confirmed_by": args.confirmed_by,
            "confirmed_at": args.confirmed_at,
        }
        result = arrange_and_sync(
            args.root,
            batch_plan,
            dry_run=bool(args.dry_run),
            sync=bool(args.sync),
            user_request=args.user_request,
            function_selection=args.function_selection,
            closeout_plan_data=closeout_payload,
            confirmation=confirmation,
            repository_id=args.repository_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
