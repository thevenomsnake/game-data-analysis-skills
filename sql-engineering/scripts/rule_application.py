#!/usr/bin/env python3
"""Typed request and canonical-rule application contracts."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable


REQUEST_ENVELOPE_VERSION = "request_envelope_v1"
RULE_APPLICATION_VERSION = "rule_application_v1"

APPLICATION_CLASSES = {"intent_required", "explicit_only", "audit_only"}
INHERITANCE_MODES = {
    "none",
    "same_contract_revision",
    "lifecycle_promotion_exact_sql",
    "dashboard_derivative_same_contract",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def object_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def build_request_envelope(
    user_request: str,
    *,
    function_id: str = "",
    lifecycle_stage: str = "",
) -> dict[str, Any]:
    text = str(user_request or "").strip()
    return {
        "schema_version": REQUEST_ENVELOPE_VERSION,
        "source": "current_user_message",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "present": bool(text),
        "function_id": str(function_id or ""),
        "lifecycle_stage": str(lifecycle_stage or ""),
    }


def application_class(contract: dict[str, Any] | None) -> str:
    contract = contract if isinstance(contract, dict) else {}
    declared = str(contract.get("application_class") or "").strip().lower()
    if declared in APPLICATION_CLASSES:
        return declared
    policy = contract.get("activation_policy") if isinstance(contract.get("activation_policy"), dict) else {}
    forward = str(policy.get("forward") or "explicit_only")
    if forward == "disabled":
        return "audit_only"
    if forward == "explicit_only":
        return "explicit_only"
    return "intent_required"


def rule_reference(
    rule: dict[str, Any],
    *,
    source: str,
    evidence: Iterable[dict[str, Any]] | None = None,
    parent_application_sha256: str = "",
    parent_asset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = rule.get("activation_contract") if isinstance(rule.get("activation_contract"), dict) else {}
    row = {
        "rule_id": str(rule.get("rule_id") or ""),
        "concept_key": str(rule.get("concept_key") or ""),
        "version": int(rule.get("version") or 0),
        "application_class": application_class(contract),
        "source": str(source or ""),
        "evidence": copy.deepcopy(list(evidence or [])),
    }
    if parent_application_sha256:
        row["parent_application_sha256"] = parent_application_sha256
    if parent_asset:
        row["parent_asset"] = copy.deepcopy(parent_asset)
    return row


def build_inheritance_contract(
    mode: str = "none",
    *,
    change_type: str = "",
    coverage_relation: str = "",
    same_execution_fingerprint: bool = False,
    same_logic_contract: bool = False,
    parent_asset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_mode = str(mode or "none").strip().lower()
    if normalized_mode not in INHERITANCE_MODES:
        normalized_mode = "none"
    return {
        "mode": normalized_mode,
        "change_type": str(change_type or ""),
        "coverage_relation": str(coverage_relation or ""),
        "same_execution_fingerprint": bool(same_execution_fingerprint),
        "same_logic_contract": bool(same_logic_contract),
        "parent_asset": copy.deepcopy(parent_asset or {}),
    }


def inheritance_allowed(contract: dict[str, Any] | None) -> bool:
    contract = contract if isinstance(contract, dict) else {}
    mode = str(contract.get("mode") or "none")
    if mode == "same_contract_revision":
        return (
            str(contract.get("change_type") or "") in {"correction", "parameter_refresh"}
            and str(contract.get("coverage_relation") or "") == "same_contract"
        )
    if mode == "lifecycle_promotion_exact_sql":
        return bool(contract.get("same_execution_fingerprint"))
    if mode == "dashboard_derivative_same_contract":
        return bool(contract.get("same_logic_contract"))
    return False


def _application_without_hash(application: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(application)
    payload.pop("application_sha256", None)
    return payload


def application_integrity_ok(application: dict[str, Any] | None) -> bool:
    if not isinstance(application, dict):
        return False
    if application.get("schema_version") != RULE_APPLICATION_VERSION:
        return False
    expected = str(application.get("application_sha256") or "")
    return bool(expected and expected == object_sha256(_application_without_hash(application)))


def inherited_rule_references(
    parent_application: dict[str, Any] | None,
    inheritance_contract: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    if not parent_application:
        return [], diagnostics
    if not application_integrity_ok(parent_application):
        diagnostics.append(
            {
                "type": "parent_rule_application_rejected",
                "reason": "missing_or_invalid_application_hash",
            }
        )
        return [], diagnostics
    if parent_application.get("status") != "evaluated":
        diagnostics.append(
            {
                "type": "parent_rule_application_not_inherited",
                "reason": "parent_application_not_evaluated",
                "status": str(parent_application.get("status") or ""),
            }
        )
        return [], diagnostics
    if not inheritance_allowed(inheritance_contract):
        diagnostics.append(
            {
                "type": "parent_rule_application_not_inherited",
                "reason": "inheritance_contract_not_eligible",
                "mode": str((inheritance_contract or {}).get("mode") or "none"),
            }
        )
        return [], diagnostics
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for key in ("applied_rules", "inherited_rules"):
        for raw in parent_application.get(key, []) or []:
            if not isinstance(raw, dict):
                continue
            identity = (
                str(raw.get("rule_id") or ""),
                str(raw.get("concept_key") or ""),
                int(raw.get("version") or 0),
            )
            if not identity[0] or not identity[1] or identity in seen:
                continue
            if str(raw.get("application_class") or "") == "audit_only":
                continue
            seen.add(identity)
            rows.append(copy.deepcopy(raw))
    return rows, diagnostics


def build_rule_application(
    *,
    request_envelope: dict[str, Any],
    mode: str,
    lifecycle_stage: str,
    applied_rules: Iterable[dict[str, Any]] = (),
    inherited_rules: Iterable[dict[str, Any]] = (),
    excluded_rules: Iterable[dict[str, Any]] = (),
    diagnostics: Iterable[dict[str, Any]] = (),
    inheritance_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": RULE_APPLICATION_VERSION,
        "status": "evaluated",
        "request_envelope": copy.deepcopy(request_envelope),
        "mode": str(mode or ""),
        "lifecycle_stage": str(lifecycle_stage or ""),
        "inheritance_contract": copy.deepcopy(inheritance_contract or build_inheritance_contract()),
        "applied_rules": copy.deepcopy(list(applied_rules)),
        "inherited_rules": copy.deepcopy(list(inherited_rules)),
        "excluded_rules": copy.deepcopy(list(excluded_rules)),
        "diagnostics": copy.deepcopy(list(diagnostics)),
    }
    payload["application_sha256"] = object_sha256(payload)
    return payload


def legacy_unlabeled_application(*, note: str = "") -> dict[str, Any]:
    payload = {
        "schema_version": RULE_APPLICATION_VERSION,
        "status": "legacy_unlabeled",
        "request_envelope": build_request_envelope(""),
        "mode": "migration",
        "lifecycle_stage": "legacy",
        "inheritance_contract": build_inheritance_contract(),
        "applied_rules": [],
        "inherited_rules": [],
        "excluded_rules": [],
        "diagnostics": [
            {
                "type": "legacy_rule_application_unlabeled",
                "reason": str(note or "Historical asset predates request-bound rule applications."),
            }
        ],
    }
    payload["application_sha256"] = object_sha256(payload)
    return payload
