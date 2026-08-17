#!/usr/bin/env python3
"""Project-configured player subject selection and SQL identity evidence."""

from __future__ import annotations

import re
from typing import Any


POLICY_VERSION = "subject_identity_policy_v1"
SELECTION_VERSION = "subject_key_selection_v1"
SELECTION_FLAGS = (
    "prefer_default_when_equal_cost",
    "prefer_native_role_when_avoids_bridge",
    "forbid_bridge_only_for_default_key",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mask_sql(sql: str) -> str:
    """Mask comments and string literals while preserving identifier positions."""
    value = str(sql or "")
    output: list[str] = []
    index = 0
    quote = False
    while index < len(value):
        char = value[index]
        nxt = value[index + 1] if index + 1 < len(value) else ""
        if quote:
            if char == "'" and nxt == "'":
                output.extend("  ")
                index += 2
                continue
            quote = char != "'"
            output.append(" ")
            index += 1
            continue
        if char == "'":
            quote = True
            output.append(" ")
            index += 1
            continue
        if char == "-" and nxt == "-":
            end = value.find("\n", index + 2)
            end = len(value) if end < 0 else end
            output.extend(" " * (end - index))
            index = end
            continue
        if char == "/" and nxt == "*":
            end = value.find("*/", index + 2)
            end = len(value) if end < 0 else end + 2
            output.extend(" " * (end - index))
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _has_identifier(code: str, field: str) -> bool:
    escaped = re.escape(field)
    return bool(re.search(rf"(?<![\w$])(?:`{escaped}`|{escaped})(?![\w$])", code, re.I))


def validate_subject_identity_policy(config: dict[str, Any]) -> list[str]:
    policy = config.get("subject_identity_policy")
    if policy is None:
        return []
    if not isinstance(policy, dict):
        return ["subject_identity_policy must be an object."]

    problems: list[str] = []
    if policy.get("contract_version") != POLICY_VERSION:
        problems.append(f"subject_identity_policy.contract_version must be {POLICY_VERSION}.")
    if policy.get("business_subject") != "player":
        problems.append("subject_identity_policy.business_subject must be player.")

    definitions = policy.get("key_definitions") or []
    keys = {
        _text(item.get("key")).casefold()
        for item in definitions
        if isinstance(item, dict) and _text(item.get("key"))
    }
    default_key = _text(policy.get("default_key"))
    if not isinstance(definitions, list) or not keys:
        problems.append("subject_identity_policy.key_definitions must be a non-empty array.")
    elif default_key.casefold() not in keys:
        problems.append("subject_identity_policy.default_key must exist in key_definitions.")
    for item in definitions if isinstance(definitions, list) else []:
        if not isinstance(item, dict):
            problems.append("subject_identity_policy.key_definitions items must be objects.")
        elif item.get("unique_per_person") is not True or not _text(item.get("uniqueness_scope")):
            problems.append("Each subject key requires unique_per_person=true and uniqueness_scope.")

    relationship = policy.get("namespace_relationship") or {}
    if (
        relationship.get("relation") != "same_person_distinct_namespaces"
        or relationship.get("direct_comparison_allowed") is not False
        or relationship.get("coalesce_allowed") is not False
    ):
        problems.append("Identity namespaces must be distinct and forbid direct comparison/coalesce.")

    role_fields = policy.get("native_role_fields") or []
    seen_fields: set[str] = set()
    if not isinstance(role_fields, list) or not role_fields:
        problems.append("subject_identity_policy.native_role_fields must be a non-empty array.")
    for item in role_fields if isinstance(role_fields, list) else []:
        if not isinstance(item, dict):
            problems.append("subject_identity_policy.native_role_fields items must be objects.")
            continue
        field = _text(item.get("field"))
        key = _text(item.get("key"))
        if not _text(item.get("role")) or not field or key.casefold() not in keys:
            problems.append("Each native role field requires role, field, and a defined key.")
        if field.casefold() in seen_fields:
            problems.append(f"subject_identity_policy.native_role_fields repeats field {field}.")
        seen_fields.add(field.casefold())

    selection = policy.get("selection_policy") or {}
    for flag in SELECTION_FLAGS:
        if not isinstance(selection.get(flag), bool):
            problems.append(f"subject_identity_policy.selection_policy.{flag} must be boolean.")
    return problems


def _effective_policy(config: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    policy = (config or {}).get("subject_identity_policy")
    if isinstance(policy, dict) and policy.get("contract_version") == POLICY_VERSION:
        return policy, "configured"
    return {
        "business_subject": "player",
        "default_key": "vOpenID",
        "key_definitions": [
            {
                "key": "vOpenID",
                "namespace": "vOpenID",
                "uniqueness_scope": "unspecified",
            }
        ],
        "namespace_relationship": {
            "relation": "same_person_distinct_namespaces",
            "direct_comparison_allowed": False,
            "coalesce_allowed": False,
        },
        "native_role_fields": [
            {
                "role": "player",
                "field": "vOpenID",
                "key": "vOpenID",
                "canonical_alias": "player_id",
                "metric_terms": [],
            }
        ],
        "selection_policy": {flag: flag == "prefer_default_when_equal_cost" for flag in SELECTION_FLAGS},
    }, "legacy_default"


def analyze_subject_identity(
    sql: str,
    project_config: dict[str, Any] | None,
    *,
    join_count: int = 0,
) -> dict[str, Any]:
    policy, policy_status = _effective_policy(project_config)
    default_key = _text(policy.get("default_key")) or "vOpenID"
    key_meta = {
        _text(item.get("key")).casefold(): item
        for item in policy.get("key_definitions", [])
        if isinstance(item, dict) and _text(item.get("key"))
    }
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    code = _mask_sql(sql)
    for mapping in policy.get("native_role_fields", []):
        if not isinstance(mapping, dict) or not _has_identifier(code, _text(mapping.get("field"))):
            continue
        role = _text(mapping.get("role")) or "player"
        key = _text(mapping.get("key"))
        field = _text(mapping.get("field"))
        meta = key_meta.get(key.casefold(), {})
        entity = grouped.setdefault(
            (role.casefold(), key.casefold()),
            {
                "subject_ref": role,
                "business_role": role,
                "selected_key": key,
                "key_namespace": _text(meta.get("namespace")) or key,
                "uniqueness_scope": _text(meta.get("uniqueness_scope")),
                "canonical_alias": _text(mapping.get("canonical_alias")) or "player_id",
                "source_fields": [],
                "metric_terms": [],
                "selection_reason": (
                    "default_key_native"
                    if key.casefold() == default_key.casefold()
                    else "event_role_native_lower_complexity_candidate"
                ),
            },
        )
        entity["source_fields"].append(field)
        entity["metric_terms"].extend(
            term for term in map(_text, mapping.get("metric_terms", [])) if term
        )

    entities = list(grouped.values())
    players = [item for item in entities if item["business_role"] == "player"]
    primary = next(
        (item for item in players if item["selected_key"].casefold() == default_key.casefold()),
        players[0] if len(players) == 1 else entities[0] if len(entities) == 1 else None,
    )
    if primary is None and not entities:
        meta = key_meta.get(default_key.casefold(), {})
        primary = {
            "subject_ref": "player",
            "business_role": "player",
            "selected_key": default_key,
            "key_namespace": _text(meta.get("namespace")) or default_key,
            "uniqueness_scope": _text(meta.get("uniqueness_scope")),
            "canonical_alias": "player_id",
            "source_fields": [],
            "metric_terms": [],
            "selection_reason": "project_default_not_observed",
        }

    keys_present = list(dict.fromkeys(item["selected_key"] for item in entities))
    bridge_candidate = len({item.casefold() for item in keys_present}) > 1 and join_count > 0
    return {
        "contract_version": SELECTION_VERSION,
        "policy_status": policy_status,
        "business_subject": _text(policy.get("business_subject")) or "player",
        "default_key": default_key,
        "selection_strategy": "default_key_unless_native_event_key_avoids_identity_bridge",
        "namespace_relationship": policy.get("namespace_relationship") or {},
        "subject_entities": entities,
        "primary_subject": primary,
        "observed_key_namespaces": keys_present,
        "identity_bridge": {
            "detected": bridge_candidate,
            "status": "candidate" if bridge_candidate else "not_detected",
            "join_count": int(join_count),
            "forbidden_when_only_for_default_key": bool(
                (policy.get("selection_policy") or {}).get("forbid_bridge_only_for_default_key")
            ),
        },
        "complexity_audit": {"status": "ok", "recommendations": []},
    }


def _entity_for_field(identity: dict[str, Any], expression: str) -> dict[str, Any] | None:
    code = _mask_sql(expression)
    return next(
        (
            entity
            for entity in identity.get("subject_entities", [])
            if any(_has_identifier(code, field) for field in entity.get("source_fields", []))
        ),
        None,
    )


def _semantic_entity(identity: dict[str, Any], metric_name: str) -> dict[str, Any] | None:
    lowered = metric_name.casefold()
    candidates = [
        (len(term), entity)
        for entity in identity.get("subject_entities", [])
        if entity.get("business_role") != "player"
        for term in map(lambda item: _text(item).casefold(), entity.get("metric_terms", []))
        if term and term in lowered
    ]
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def metric_subject_binding(
    metric_name: str,
    expression: str,
    identity: dict[str, Any],
) -> dict[str, Any]:
    direct = _entity_for_field(identity, expression)
    semantic = _semantic_entity(identity, metric_name)
    distinct = bool(re.search(r"\bcount\s*\(\s*distinct\b", expression, re.I))
    person_metric = bool(re.search(r"玩家|用户|人数|player|user|openid|role", metric_name, re.I))
    selected = direct if distinct and direct else semantic if person_metric else None
    selected = selected or (identity.get("primary_subject") if person_metric else None)
    if not isinstance(selected, dict):
        return {}

    fields = selected.get("source_fields") or []
    binding = {
        "subject_ref": _text(selected.get("subject_ref")),
        "subject_key_namespace": _text(selected.get("key_namespace")),
        "dedup_key": fields[0] if fields else _text(selected.get("selected_key")),
        "subject_selection_reason": (
            "direct_metric_expression" if direct is selected and distinct else _text(selected.get("selection_reason"))
        ),
    }
    if (
        direct
        and semantic
        and direct.get("selected_key") != semantic.get("selected_key")
        and identity.get("identity_bridge", {}).get("detected")
    ):
        alternative_fields = semantic.get("source_fields") or []
        binding["lower_complexity_alternative"] = {
            "subject_ref": _text(semantic.get("subject_ref")),
            "subject_key_namespace": _text(semantic.get("key_namespace")),
            "dedup_key": alternative_fields[0] if alternative_fields else _text(semantic.get("selected_key")),
            "reason": "native_event_role_avoids_identity_conversion_when_no_vopenid_only_output_is_required",
        }
    return binding


def finalize_complexity_audit(identity: dict[str, Any], metrics: list[dict[str, Any]]) -> None:
    recommendations = [
        {"metric": _text(metric.get("name") or metric.get("field")), **metric["lower_complexity_alternative"]}
        for metric in metrics
        if isinstance(metric.get("lower_complexity_alternative"), dict)
    ]
    event_role_selected = any(
        item.get("business_role") != "player"
        and _text(item.get("selected_key")).casefold() != _text(identity.get("default_key")).casefold()
        for item in identity.get("subject_entities", [])
    )
    bridge = identity["identity_bridge"]
    bridge["detected"] = bool(recommendations)
    bridge["status"] = (
        "business_reason_required"
        if recommendations
        else "native_event_key_selected" if event_role_selected else "not_detected"
    )
    identity["complexity_audit"] = {
        "status": "optimization_available" if recommendations else "ok",
        "recommendations": recommendations,
    }
