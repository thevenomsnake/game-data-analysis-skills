#!/usr/bin/env python3
"""Build bounded consumer projections from persisted SQL execution-route facts."""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


DELIVERY_SCHEMA_VERSION = "execution_delivery_v1"
VARIANT_IDENTITY_SCHEMA_VERSION = "execution_variant_identity_v1"
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_TEXT = 240


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def engine_key(route: dict[str, Any]) -> str:
    values = [clean_text(route.get("query_engine")), clean_text(route.get("sql_dialect"))]
    for value in values:
        token = re.sub(r"[^a-z0-9]+", "", value.lower())
        if "starrocks" in token:
            return "starrocks"
        if token == "hive" or token.startswith("hive"):
            return "hive"
    return "unlabeled"


def build_variant_identity(
    *,
    logical_revision_id: str,
    variant_group_id: str,
    variant_key: str,
    recommended: bool = False,
) -> dict[str, Any]:
    logical_revision_id = clean_text(logical_revision_id)
    variant_group_id = clean_text(variant_group_id)
    variant_key = clean_text(variant_key).lower()
    if not logical_revision_id and not variant_group_id and not recommended:
        return {}
    if not IDENTITY_RE.fullmatch(logical_revision_id):
        raise ValueError("logical_revision_id must be a stable 3-128 character identifier.")
    if not IDENTITY_RE.fullmatch(variant_group_id):
        raise ValueError("variant_group_id must be a stable 3-128 character identifier.")
    if variant_key not in {"starrocks", "hive", "other"}:
        raise ValueError("variant_key must resolve to starrocks, hive, or other.")
    return {
        "schema_version": VARIANT_IDENTITY_SCHEMA_VERSION,
        "logical_revision_id": logical_revision_id,
        "variant_group_id": variant_group_id,
        "variant_key": variant_key,
        "recommended": bool(recommended),
    }


def _bounded_strings(value: Any) -> list[str]:
    rows: list[str] = []
    for item in list_value(value)[:MAX_EVIDENCE_ITEMS]:
        text = clean_text(item)
        if text:
            rows.append(text[:MAX_EVIDENCE_TEXT])
    return rows


def _safe_relative_path(value: Any) -> str:
    text = clean_text(value).replace("\\", "/")
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", text):
        return ""
    return path.as_posix()


def build_execution_delivery(asset_id: str, route: Any) -> dict[str, Any]:
    route = dict_value(route)
    has_route = route.get("schema_version") == "execution_route_v1"
    identity = dict_value(route.get("execution_variant_identity"))
    if identity.get("schema_version") != VARIANT_IDENTITY_SCHEMA_VERSION:
        identity = {}
    profile_id = clean_text(route.get("selected_profile")) if has_route else ""
    materialized_engine_key = engine_key(route) if has_route else "unlabeled"
    template_contract = clean_text(route.get("template_contract")) if has_route else ""
    return {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "asset_id": clean_text(asset_id),
        "status": "materialized" if has_route else "legacy_unlabeled",
        "materialized_engine_key": materialized_engine_key,
        "sql_dialect": clean_text(route.get("sql_dialect")) if has_route else "",
        "query_engine": clean_text(route.get("query_engine")) if has_route else "",
        "profile_id": profile_id,
        "routing_role": clean_text(route.get("routing_role")) if has_route else "",
        "route_status": clean_text(route.get("status")) if has_route else "not_available",
        "has_execution_evidence": has_route,
        "execution_evidence": {
            "schema_version": clean_text(route.get("schema_version")) if has_route else "",
            "selection_mode": clean_text(route.get("selection_mode")) if has_route else "",
            "routing_reasons": _bounded_strings(route.get("routing_reasons")) if has_route else [],
            "blockers": _bounded_strings(route.get("blockers")) if has_route else [],
            "source_tlog_count": len(list_value(route.get("source_tlogs"))) if has_route else 0,
            "passthrough_table_count": len(list_value(route.get("passthrough_tables"))) if has_route else 0,
            "rendered_sql_sha256": clean_text(route.get("rendered_sql_sha256")) if has_route else "",
        },
        "portable_template": {
            "available": bool(template_contract),
            "contract": template_contract,
            "path": _safe_relative_path(route.get("portable_template_path")) if has_route else "",
        },
        "variant_status": "pending_explicit_group" if identity else "not_grouped",
        "logical_revision_id": "",
        "variant_group_id": "",
        "variant_key": clean_text(identity.get("variant_key")),
        "exact_variant_asset_ids": [],
        "recommended_variant_asset_id": "",
        "is_recommended_variant": bool(identity.get("recommended")),
        "_explicit_variant_identity": copy.deepcopy(identity),
    }


def finalize_execution_deliveries(assets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    issues: list[dict[str, Any]] = []
    for asset_id, asset in assets.items():
        facts = dict_value(asset.get("facts"))
        delivery = dict_value(facts.get("execution_delivery"))
        identity = dict_value(delivery.get("_explicit_variant_identity"))
        group_id = clean_text(identity.get("variant_group_id"))
        if group_id:
            grouped[group_id].append((asset_id, delivery, identity))

    for group_id, rows in sorted(grouped.items()):
        logical_ids = {clean_text(identity.get("logical_revision_id")) for _, _, identity in rows}
        variant_keys = [clean_text(identity.get("variant_key")) for _, _, identity in rows]
        engine_keys = [clean_text(delivery.get("materialized_engine_key")) for _, delivery, _ in rows]
        recommended = [asset_id for asset_id, _, identity in rows if identity.get("recommended")]
        conflict_reasons: list[str] = []
        if len(logical_ids) != 1 or "" in logical_ids:
            conflict_reasons.append("logical_revision_id values differ or are empty")
        if len(set(variant_keys)) != len(variant_keys):
            conflict_reasons.append("variant_key values are duplicated")
        if any(key != engine for key, engine in zip(variant_keys, engine_keys)):
            conflict_reasons.append("variant_key does not match the persisted execution route")
        if len(recommended) > 1:
            conflict_reasons.append("more than one variant is marked recommended")
        if conflict_reasons:
            for _, delivery, _ in rows:
                delivery["variant_status"] = "identity_conflict"
            issues.append(
                {
                    "code": "execution_variant_identity_conflict",
                    "message": f"Explicit execution variant group `{group_id}` is inconsistent: " + "; ".join(conflict_reasons),
                    "asset_ids": sorted(asset_id for asset_id, _, _ in rows),
                }
            )
            continue
        exact_ids = sorted(asset_id for asset_id, _, _ in rows)
        logical_revision_id = next(iter(logical_ids))
        recommended_id = recommended[0] if recommended else ""
        for _, delivery, identity in rows:
            delivery.update(
                {
                    "variant_status": "grouped",
                    "logical_revision_id": logical_revision_id,
                    "variant_group_id": group_id,
                    "variant_key": clean_text(identity.get("variant_key")),
                    "exact_variant_asset_ids": exact_ids,
                    "recommended_variant_asset_id": recommended_id,
                }
            )

    for asset in assets.values():
        delivery = dict_value(dict_value(asset.get("facts")).get("execution_delivery"))
        delivery.pop("_explicit_variant_identity", None)
    return issues
