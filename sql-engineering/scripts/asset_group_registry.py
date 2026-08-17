#!/usr/bin/env python3
"""Assign immutable chronological IDs to related analytical asset groups."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from asset_provenance import build_generation_provenance, now_iso
from capability_registry import command_function_ids
from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)


SCHEMA_VERSION = "sql_asset_group_registry_v2"
SUPPORTED_CATALOG_SCHEMAS = {"sql_asset_catalog_v1", "sql_asset_catalog_v2"}
SUPPORTED_ORGANIZATION_SCHEMAS = {"sql_asset_organization_v1", "sql_asset_organization_v2"}
DEFAULT_OUTPUT_NAME = "asset_group_registry.json"
MAX_RESPONSE_ITEMS = 100
GROUP_ID_RE = re.compile(r"^AG-(\d{4,})$")
VERSIONED_ROOT_RE = re.compile(r"^(.*):v\d+$", re.IGNORECASE)

ANALYTICAL_KINDS = {
    "formal_asset_package",
    "formal_asset_member",
    "query",
    "dashboard",
    "validation",
    "run_evidence",
    "result",
    "analysis_workbook",
    "comparison_workbook",
    "visualization",
    "derived_output",
    "export",
}
ROOT_KINDS = {"formal_asset_package", "query"}
STRONG_GROUP_RELATIONS = {
    "has_member",
    "member_of_package",
    "has_current_member",
    "has_formal_query",
    "has_query_contract",
    "has_query_metadata",
    "has_dashboard_delivery",
    "has_dashboard_contract",
    "has_dashboard_metadata",
    "has_validation",
    "has_validation_contract",
    "has_validation_metadata",
    "has_evidence",
    "previous_version",
    "next_version",
    "derived_from_query",
    "validated_by",
    "has_run_evidence",
    "has_result",
    "result_of_run",
    "evidence_for",
    "has_derived_output",
    "derived_from",
    "derived_from_result",
    "has_visualization",
    "supersedes",
    "replaced_by",
}
ROOT_KIND_PRIORITY = {
    "formal_asset_package": 0,
    "query": 1,
    "dashboard": 2,
    "validation": 3,
    "run_evidence": 4,
    "result": 5,
    "visualization": 6,
    "analysis_workbook": 7,
    "comparison_workbook": 8,
    "formal_asset_member": 9,
    "derived_output": 10,
    "export": 11,
}
LOCAL_WORKSPACE_ASSET_TOKEN = ":temporary_query:"
LOCAL_PROMOTION_LEDGER_TOKEN = ":promotion_ledger:"
FORMAL_ASSET_TOKEN = ":formal_asset_package:"
LEGACY_ANALYTICAL_ID_TOKENS = (
    ":query:",
    ":dashboard:",
    ":validation:",
    ":run_evidence:",
    ":result:",
    ":analysis_workbook:",
    ":comparison_workbook:",
    ":visualization:",
    ":derived_output:",
    ":export:",
)


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return copy.deepcopy(default)
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return copy.deepcopy(default)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def excluded_legacy_member_id(asset_id: str) -> bool:
    normalized = clean_text(asset_id).lower()
    if LOCAL_WORKSPACE_ASSET_TOKEN in normalized or LOCAL_PROMOTION_LEDGER_TOKEN in normalized:
        return True
    return FORMAL_ASSET_TOKEN not in normalized and any(
        token in normalized for token in LEGACY_ANALYTICAL_ID_TOKENS
    )


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def relative_to_catalog(path: Path, catalog_path: Path) -> str:
    try:
        return path.resolve().relative_to(catalog_path.resolve().parent).as_posix()
    except ValueError:
        return path.name


class DisjointSet:
    def __init__(self, values: set[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def analytical_assets(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        clean_text(asset.get("asset_id")): asset
        for asset in list_value(catalog.get("assets"))
        if isinstance(asset, dict)
        and clean_text(asset.get("asset_id"))
        and clean_text(asset.get("asset_kind")) in ANALYTICAL_KINDS
    }


def asset_components(catalog: dict[str, Any], assets: dict[str, dict[str, Any]]) -> list[set[str]]:
    asset_ids = set(assets)
    groups = DisjointSet(asset_ids)
    version_families: dict[str, list[str]] = defaultdict(list)
    for asset_id, asset in assets.items():
        if clean_text(asset.get("asset_kind")) not in ROOT_KINDS:
            continue
        match = VERSIONED_ROOT_RE.match(asset_id)
        if match:
            version_families[match.group(1)].append(asset_id)
    for members in version_families.values():
        for asset_id in members[1:]:
            groups.union(members[0], asset_id)

    for relation in list_value(catalog.get("relationships")):
        if not isinstance(relation, dict) or clean_text(relation.get("relation")) not in STRONG_GROUP_RELATIONS:
            continue
        source = clean_text(relation.get("source_asset_id"))
        target = clean_text(relation.get("target_asset_id"))
        if source in asset_ids and target in asset_ids:
            groups.union(source, target)

    components: dict[str, set[str]] = defaultdict(set)
    for asset_id in sorted(asset_ids):
        components[groups.find(asset_id)].add(asset_id)
    return list(components.values())


def group_sequence(group_id: str) -> int:
    match = GROUP_ID_RE.match(group_id)
    return int(match.group(1)) if match else 0


def issue(code: str, message: str, *, group_id: str = "", asset_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "group_id": group_id,
        "asset_ids": sorted(asset_ids or []),
    }


def component_sort_key(component: set[str], assets: dict[str, dict[str, Any]]) -> tuple[str, str]:
    timestamps = sorted(
        clean_text(assets[asset_id].get("created_at"))
        for asset_id in component
        if clean_text(assets[asset_id].get("created_at"))
    )
    return (timestamps[0] if timestamps else "9999-12-31T23:59:59Z", min(component))


def choose_root(member_ids: list[str], assets: dict[str, dict[str, Any]]) -> str:
    def key(asset_id: str) -> tuple[int, int, str, str]:
        asset = assets[asset_id]
        kind = clean_text(asset.get("asset_kind"))
        lifecycle = clean_text(asset.get("lifecycle_state")).lower()
        current_rank = 0 if lifecycle in {"current", "runnable", "result_confirmed", "verified"} else 1
        version = asset.get("version")
        version_rank = -int(version) if isinstance(version, int) or clean_text(version).isdigit() else 0
        return (ROOT_KIND_PRIORITY.get(kind, 99), current_rank, version_rank, asset_id)

    return min(member_ids, key=key) if member_ids else ""


def organization_entry(organization: dict[str, Any], asset_id: str) -> dict[str, Any]:
    return dict_value(dict_value(organization.get("entries")).get(asset_id))


def group_metadata(
    group: dict[str, Any],
    *,
    assets: dict[str, dict[str, Any]],
    organization: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    member_ids = sorted(set(clean_text(item) for item in list_value(group.get("member_asset_ids")) if clean_text(item)))
    current_ids = [asset_id for asset_id in member_ids if asset_id in assets]
    missing_ids = sorted(set(member_ids) - set(current_ids))
    root_id = choose_root(current_ids, assets)
    root = assets.get(root_id, {})
    semantic = organization_entry(organization, root_id)
    source = clean_text(group.get("classification_source")) or "deterministic"
    reviewed = source in {"human", "llm"}

    if reviewed:
        title = clean_text(group.get("display_title"))
        summary = clean_text(group.get("semantic_summary"))
        navigation_path = [clean_text(item) for item in list_value(group.get("navigation_path")) if clean_text(item)]
    else:
        title = clean_text(root.get("title")) or root_id or clean_text(group.get("display_title"))
        summary = clean_text(semantic.get("semantic_summary")) or clean_text(root.get("summary")) or title
        navigation_path = [clean_text(item) for item in list_value(semantic.get("navigation_path")) if clean_text(item)]
    if len(navigation_path) != 2:
        navigation_path = ["其他", "待整理"]

    created_values = sorted(
        clean_text(assets[asset_id].get("created_at"))
        for asset_id in current_ids
        if clean_text(assets[asset_id].get("created_at"))
    )
    activity_values = sorted(
        value
        for asset_id in current_ids
        for value in (
            clean_text(assets[asset_id].get("updated_at")),
            clean_text(assets[asset_id].get("created_at")),
        )
        if value
    )
    package_root_ids = sorted(
        asset_id
        for asset_id in current_ids
        if clean_text(assets[asset_id].get("asset_kind")) == "formal_asset_package"
    )
    root_ids = package_root_ids or sorted(
        asset_id
        for asset_id in current_ids
        if clean_text(assets[asset_id].get("asset_kind")) in ROOT_KINDS
    )
    if not root_ids and root_id:
        root_ids = [root_id]
    kind_counts = Counter(clean_text(assets[asset_id].get("asset_kind")) for asset_id in current_ids)
    project_ids = sorted({clean_text(assets[asset_id].get("project_id")) for asset_id in current_ids if clean_text(assets[asset_id].get("project_id"))})
    formal_asset_ids = sorted(
        {
            clean_text(assets[asset_id].get("formal_asset_id"))
            for asset_id in current_ids
            if clean_text(assets[asset_id].get("formal_asset_id"))
        }
    )
    curation_state = "catalog_missing" if not current_ids else ("current" if root_ids else "needs_group_review")

    refreshed = {
        "group_id": clean_text(group.get("group_id")),
        "sequence": int(group.get("sequence") or group_sequence(clean_text(group.get("group_id")))),
        "display_order": int(group.get("display_order") or group.get("sequence") or group_sequence(clean_text(group.get("group_id")))),
        "display_title": title,
        "semantic_summary": summary,
        "navigation_path": navigation_path,
        "project_ids": project_ids,
        "formal_asset_ids": formal_asset_ids,
        "first_managed_at": clean_text(group.get("first_managed_at")) or (created_values[0] if created_values else clean_text(group.get("assigned_at")) or generated_at),
        "last_activity_at": activity_values[-1] if activity_values else clean_text(group.get("last_activity_at")),
        "assigned_at": clean_text(group.get("assigned_at")) or generated_at,
        "updated_at": generated_at,
        "group_state": "active" if current_ids else "catalog_missing",
        "curation_state": curation_state,
        "classification_source": source,
        "root_asset_ids": root_ids,
        "member_asset_ids": member_ids,
        "catalog_missing_member_asset_ids": missing_ids,
        "member_counts_by_kind": dict(sorted(kind_counts.items())),
        "notes": clean_text(group.get("notes")),
    }
    fingerprint_payload = {
        key: refreshed[key]
        for key in (
            "display_title",
            "semantic_summary",
            "navigation_path",
            "project_ids",
            "formal_asset_ids",
            "root_asset_ids",
            "member_asset_ids",
        )
    }
    refreshed["group_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return refreshed


def build_registry(
    catalog: dict[str, Any],
    catalog_path: Path,
    organization: dict[str, Any],
    organization_path: Path,
    existing: dict[str, Any] | None = None,
    *,
    catalog_fingerprint: str = "",
    organization_fingerprint: str = "",
) -> dict[str, Any]:
    generated_at = now_iso()
    assets = analytical_assets(catalog)
    existing_groups = [copy.deepcopy(item) for item in list_value(dict_value(existing).get("groups")) if isinstance(item, dict)]
    issues: list[dict[str, Any]] = []
    member_to_group: dict[str, str] = {}
    groups_by_id: dict[str, dict[str, Any]] = {}
    max_sequence = 0

    for group in existing_groups:
        group_id = clean_text(group.get("group_id"))
        sequence = group_sequence(group_id)
        if not sequence or group_id in groups_by_id:
            issues.append(issue("invalid_existing_group", "Existing group id is invalid or duplicated.", group_id=group_id))
            continue
        max_sequence = max(max_sequence, sequence)
        shared_member_ids = sorted(
            {
                clean_text(item)
                for item in list_value(group.get("member_asset_ids"))
                if clean_text(item)
                and (
                    clean_text(item) in assets
                    or not excluded_legacy_member_id(clean_text(item))
                )
            }
        )
        if not shared_member_ids:
            continue
        group["sequence"] = sequence
        group["member_asset_ids"] = shared_member_ids
        groups_by_id[group_id] = group
        for asset_id in shared_member_ids:
            asset_id = clean_text(asset_id)
            if not asset_id:
                continue
            previous = member_to_group.get(asset_id)
            if previous and previous != group_id:
                issues.append(issue("duplicate_group_membership", "One asset is assigned to multiple existing groups.", asset_ids=[asset_id]))
                continue
            member_to_group[asset_id] = group_id

    unassigned: set[str] = set()
    new_components = sorted(asset_components(catalog, assets), key=lambda value: component_sort_key(value, assets))
    for component in new_components:
        existing_ids = sorted({member_to_group[asset_id] for asset_id in component if asset_id in member_to_group})
        if len(existing_ids) > 1:
            pending = sorted(asset_id for asset_id in component if asset_id not in member_to_group)
            unassigned.update(pending)
            issues.append(
                issue(
                    "component_spans_existing_groups",
                    "A new strong relationship connects multiple immutable groups; existing IDs were preserved and unassigned members require review.",
                    asset_ids=sorted(component),
                )
            )
            continue
        if existing_ids:
            group_id = existing_ids[0]
            members = set(clean_text(item) for item in list_value(groups_by_id[group_id].get("member_asset_ids")) if clean_text(item))
            members.update(component)
            groups_by_id[group_id]["member_asset_ids"] = sorted(members)
            for asset_id in component:
                member_to_group.setdefault(asset_id, group_id)
            continue

        max_sequence += 1
        group_id = f"AG-{max_sequence:04d}"
        groups_by_id[group_id] = {
            "group_id": group_id,
            "sequence": max_sequence,
            "display_order": max_sequence,
            "member_asset_ids": sorted(component),
            "assigned_at": generated_at,
            "classification_source": "deterministic",
            "notes": "",
        }
        for asset_id in component:
            member_to_group[asset_id] = group_id

    groups = [
        group_metadata(group, assets=assets, organization=organization, generated_at=generated_at)
        for group in groups_by_id.values()
    ]
    groups.sort(key=lambda group: (int(group.get("display_order") or 0), int(group.get("sequence") or 0)))
    grouped_current_ids = {
        asset_id
        for group in groups
        for asset_id in list_value(group.get("member_asset_ids"))
        if asset_id in assets
    }
    unassigned.update(set(assets) - grouped_current_ids)
    summary = {
        "eligible_asset_count": len(assets),
        "group_count": len(groups),
        "active_group_count": sum(1 for group in groups if group.get("group_state") == "active"),
        "catalog_missing_group_count": sum(1 for group in groups if group.get("group_state") == "catalog_missing"),
        "grouped_current_asset_count": len(grouped_current_ids),
        "unassigned_asset_count": len(unassigned),
        "issue_count": len(issues),
        "groups_by_navigation_path": dict(
            sorted(Counter(" / ".join(list_value(group.get("navigation_path"))) for group in groups).items())
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "source_model": "catalog_and_organization_only",
        "generated_at": generated_at,
        "catalog_path": relative_to_catalog(catalog_path, catalog_path),
        "catalog_fingerprint": catalog_fingerprint or file_sha256(catalog_path),
        "organization_path": relative_to_catalog(organization_path, catalog_path),
        "organization_fingerprint": organization_fingerprint or file_sha256(organization_path),
        "generation_provenance": build_generation_provenance(
            generator_script="asset_group_registry.py",
            workflow="stable_asset_group_directory",
            artifact_kind="ASSET_GROUP_REGISTRY",
            generated_at=generated_at,
            source="catalog_relationships_and_organization",
        ),
        "id_policy": {
            "prefix": "AG-",
            "minimum_width": 4,
            "next_sequence": max_sequence + 1,
            "immutable_after_assignment": True,
            "late_imports_do_not_renumber": True,
            "sql_version_namespace_is_separate": True,
            "formal_asset_identity_is_preserved": True,
        },
        "grouping_policy": {
            "unit": "one analytical question or explicitly linked analysis bundle",
            "uses_text_similarity": False,
            "uses_business_topic_as_identity": False,
            "package_membership_is_authoritative": True,
            "strong_relations": sorted(STRONG_GROUP_RELATIONS),
            "eligible_asset_kinds": sorted(ANALYTICAL_KINDS),
        },
        "summary": summary,
        "groups": groups,
        "unassigned_asset_ids": sorted(unassigned),
        "issues": sorted(issues, key=lambda item: (item["code"], item["group_id"], item["asset_ids"])),
    }


def validate_registry(
    payload: dict[str, Any],
    catalog: dict[str, Any] | None = None,
    organization: dict[str, Any] | None = None,
) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    if payload.get("source_model") != "catalog_and_organization_only":
        problems.append("source_model must be catalog_and_organization_only")
    groups = list_value(payload.get("groups"))
    group_ids: set[str] = set()
    sequences: set[int] = set()
    members: dict[str, str] = {}
    max_sequence = 0
    for group in groups:
        if not isinstance(group, dict):
            problems.append("groups must contain objects")
            continue
        group_id = clean_text(group.get("group_id"))
        sequence = int(group.get("sequence") or 0)
        if not GROUP_ID_RE.match(group_id) or group_sequence(group_id) != sequence:
            problems.append(f"invalid group id/sequence: {group_id}")
        if group_id in group_ids or sequence in sequences:
            problems.append(f"duplicate group id/sequence: {group_id}")
        group_ids.add(group_id)
        sequences.add(sequence)
        max_sequence = max(max_sequence, sequence)
        if int(group.get("display_order") or 0) < 1:
            problems.append(f"display_order must be positive: {group_id}")
        navigation = list_value(group.get("navigation_path"))
        if len(navigation) != 2 or any(not clean_text(item) for item in navigation):
            problems.append(f"navigation_path must contain two labels: {group_id}")
        member_ids = [clean_text(item) for item in list_value(group.get("member_asset_ids")) if clean_text(item)]
        if not member_ids:
            problems.append(f"group must retain at least one member id: {group_id}")
        for asset_id in member_ids:
            previous = members.get(asset_id)
            if previous and previous != group_id:
                problems.append(f"asset belongs to multiple groups: {asset_id}")
            members[asset_id] = group_id
        if not set(list_value(group.get("root_asset_ids"))).issubset(set(member_ids)):
            problems.append(f"root_asset_ids must be members: {group_id}")
        if catalog is not None:
            catalog_by_id = analytical_assets(catalog)
            expected_formal_ids = {
                clean_text(catalog_by_id[asset_id].get("formal_asset_id"))
                for asset_id in member_ids
                if asset_id in catalog_by_id and clean_text(catalog_by_id[asset_id].get("formal_asset_id"))
            }
            if set(list_value(group.get("formal_asset_ids"))) != expected_formal_ids:
                problems.append(f"formal_asset_ids must preserve catalog FA identity: {group_id}")
    next_sequence = int(dict_value(payload.get("id_policy")).get("next_sequence") or 0)
    if next_sequence <= max_sequence:
        problems.append("id_policy.next_sequence must be greater than every assigned sequence")

    unassigned = {clean_text(item) for item in list_value(payload.get("unassigned_asset_ids")) if clean_text(item)}
    if set(members) & unassigned:
        problems.append("an asset cannot be both grouped and unassigned")
    if catalog is not None:
        if catalog.get("schema_version") not in SUPPORTED_CATALOG_SCHEMAS:
            problems.append("catalog uses an unsupported schema_version")
        eligible = set(analytical_assets(catalog))
        current_members = set(members) & eligible
        if current_members | unassigned != eligible:
            problems.append("every eligible catalog asset must be grouped or explicitly unassigned")
        invalid_members = {
            asset_id
            for asset_id in current_members
            if clean_text(analytical_assets(catalog)[asset_id].get("asset_kind")) not in ANALYTICAL_KINDS
        }
        if invalid_members:
            problems.append(f"non-analytical assets cannot be grouped: {sorted(invalid_members)}")
    if organization is not None and organization.get("schema_version") not in SUPPORTED_ORGANIZATION_SCHEMAS:
        problems.append("organization uses an unsupported schema_version")
    return problems


def default_registry_path(catalog_path: Path) -> Path:
    return catalog_path.parent / DEFAULT_OUTPUT_NAME


def render_summary(result: dict[str, Any]) -> str:
    lines = [f"status={result.get('status', 'unknown')}"]
    if result.get("registry_path"):
        lines.append(f"registry_path={result['registry_path']}")
    for key, value in dict_value(result.get("summary")).items():
        if not isinstance(value, dict):
            lines.append(f"{key}={value}")
    for problem in list_value(result.get("problems")):
        lines.append(f"- {problem}")
    return "\n".join(lines) + "\n"


def bounded_rows(value: Any) -> tuple[list[Any], int, bool]:
    rows = list_value(value)
    return rows[:MAX_RESPONSE_ITEMS], len(rows), len(rows) > MAX_RESPONSE_ITEMS


def add_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--registry", default="")
    parser.add_argument("--format", choices=["json", "summary"], default="summary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="Preview stable group assignment without writing")
    add_inputs(scan)
    refresh = sub.add_parser("refresh", help="Preserve existing IDs and assign IDs to new analytical groups")
    add_inputs(refresh)
    refresh.add_argument("--output", default="")
    add_function_gate_arguments(refresh, selection_help="Use [ASSET_ORGANIZATION] for stable asset grouping.")
    validate = sub.add_parser("validate", help="Validate an asset group registry")
    add_inputs(validate)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        catalog_path = Path(args.catalog).resolve()
        organization_path = Path(args.organization).resolve()
        catalog = read_json(catalog_path, {})
        organization = read_json(organization_path, {})
        registry_path = Path(args.registry).resolve() if args.registry else default_registry_path(catalog_path)
        existing = read_json(registry_path, {})
        if catalog.get("schema_version") not in SUPPORTED_CATALOG_SCHEMAS:
            raise ValueError("Catalog uses an unsupported schema_version.")
        if organization.get("schema_version") not in SUPPORTED_ORGANIZATION_SCHEMAS:
            raise ValueError("Organization uses an unsupported schema_version.")

        if args.command == "validate":
            problems = validate_registry(existing, catalog, organization)
            if clean_text(existing.get("catalog_fingerprint")) != file_sha256(catalog_path):
                problems.append("registry catalog_fingerprint does not match the current catalog")
            if clean_text(existing.get("organization_fingerprint")) != file_sha256(organization_path):
                problems.append("registry organization_fingerprint does not match the current organization")
            result = {
                "status": "fail" if problems else "pass",
                "registry_path": registry_path.as_posix(),
                "summary": dict_value(existing.get("summary")),
                "problems": problems,
            }
        else:
            payload = build_registry(catalog, catalog_path, organization, organization_path, existing)
            problems = validate_registry(payload, catalog, organization)
            existing_by_id = {
                clean_text(group.get("group_id")): group
                for group in list_value(existing.get("groups"))
                if isinstance(group, dict) and clean_text(group.get("group_id"))
            }
            new_groups = [
                {
                    "group_id": group["group_id"],
                    "display_title": group["display_title"],
                    "navigation_path": group["navigation_path"],
                    "member_count": len(group["member_asset_ids"]),
                }
                for group in payload["groups"]
                if group["group_id"] not in existing_by_id
            ]
            changed_group_ids = [
                group["group_id"]
                for group in payload["groups"]
                if group["group_id"] in existing_by_id
                and clean_text(existing_by_id[group["group_id"]].get("group_fingerprint"))
                != clean_text(group.get("group_fingerprint"))
            ]
            if args.command == "refresh":
                require_user_function_selection(
                    args.function_selection,
                    user_request=args.user_request,
                    allowed_ids=command_function_ids("asset_group_registry.py", args.command),
                    purpose="stable cross-project asset group assignment",
                )
                require_user_request(args.user_request, purpose="stable cross-project asset group assignment")
                output = Path(args.output).resolve() if args.output else registry_path
                write_json(output, payload)
                registry_path = output
            bounded_issues, issue_count, issues_truncated = bounded_rows(payload.get("issues"))
            bounded_new_groups, new_group_count, new_groups_truncated = bounded_rows(new_groups)
            bounded_changed_ids, changed_group_count, changed_groups_truncated = bounded_rows(changed_group_ids)
            result = {
                "status": "fail" if problems else ("warn" if payload.get("issues") or payload.get("unassigned_asset_ids") else "pass"),
                "registry_path": registry_path.as_posix(),
                "summary": payload["summary"],
                "problems": problems,
                "issues": bounded_issues,
                "issue_count": issue_count,
                "issues_truncated": issues_truncated,
                "new_groups": bounded_new_groups,
                "new_group_count": new_group_count,
                "new_groups_truncated": new_groups_truncated,
                "changed_group_ids": bounded_changed_ids,
                "changed_group_count": changed_group_count,
                "changed_group_ids_truncated": changed_groups_truncated,
            }
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "fail", "problems": [str(exc)]}

    print(json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else render_summary(result), end="")
    return 1 if result.get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
