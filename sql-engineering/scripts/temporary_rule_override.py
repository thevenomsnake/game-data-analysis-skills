"""Temporary query rule-override contract.

A temporary override records an explicit user choice for one query family. It
never edits canonical rules and never weakens project execution, privacy, or
performance checks.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


SCHEMA_VERSION = "temporary_rule_override_v1"
TEMPORARY_SQL_RE = re.compile(
    r"(?:临时\s*(?:sql|SQL|查询)|这次先按|本次先按|仅本次|temporary\s+(?:sql|query))",
    flags=re.I,
)
CONFIRMED_OVERRIDE_RE = re.compile(
    r"(?=.*(?:确认|同意|以我说的为准|按我说的))"
    r"(?=.*(?:本次|这次|临时))"
    r"(?=.*(?:sql|查询|口径|规则|冲突))",
    flags=re.I | re.S,
)
SIGNATURE_FIELDS = (
    "type",
    "rule_id",
    "concept_key",
    "expected_log",
    "forbidden_log",
    "expected_field",
    "forbidden_field",
    "expected_condition",
    "expected_expression",
    "expected_table",
    "expected_proxy_logs",
    "actual_logs",
)


def _compact_text(value: Any, *, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def request_declares_temporary_sql(user_request: str) -> bool:
    """Return whether the verbatim request explicitly scopes work as temporary."""

    return bool(TEMPORARY_SQL_RE.search(str(user_request or "")))


def request_authorizes_temporary_override(user_request: str) -> bool:
    """Return whether the current user message explicitly authorizes one-query override."""

    request = str(user_request or "")
    return request_declares_temporary_sql(request) or bool(CONFIRMED_OVERRIDE_RE.search(request))


def _signature_row(blocker: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for field in SIGNATURE_FIELDS:
        value = blocker.get(field)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            row[field] = sorted(
                {
                    _compact_text(item, limit=200)
                    for item in value
                    if _compact_text(item, limit=200)
                }
            )
        else:
            row[field] = _compact_text(value, limit=300)
    return row


def canonical_conflict_signature(blockers: list[dict[str, Any]]) -> str:
    rows = [_signature_row(item) for item in blockers if isinstance(item, dict)]
    rows = [item for item in rows if item]
    rows.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    if not rows:
        return ""
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_temporary_rule_override(
    *,
    user_request: str,
    blockers: list[dict[str, Any]],
    acknowledged_at: str,
) -> dict[str, Any]:
    """Build a query-scoped override from canonical-rule conflicts."""

    canonical_blockers = [
        copy.deepcopy(item)
        for item in blockers
        if isinstance(item, dict) and (item.get("rule_id") or item.get("concept_key"))
    ]
    signature = canonical_conflict_signature(canonical_blockers)
    if not signature:
        return {}
    instruction = _compact_text(user_request)
    conflict_reasons = sorted(
        {
            _compact_text(item.get("reason") or item.get("message"), limit=400)
            for item in canonical_blockers
            if _compact_text(item.get("reason") or item.get("message"), limit=400)
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": True,
        "source": "explicit_user_request",
        "user_instruction": instruction,
        "user_request_hash": hashlib.sha256(str(user_request or "").encode("utf-8")).hexdigest(),
        "conflict_signature": signature,
        "conflicted_rule_ids": sorted(
            {str(item.get("rule_id") or "") for item in canonical_blockers if item.get("rule_id")}
        ),
        "conflicted_concept_keys": sorted(
            {
                str(item.get("concept_key") or "")
                for item in canonical_blockers
                if item.get("concept_key")
            }
        ),
        "conflicts": [_signature_row(item) for item in canonical_blockers],
        "conflict_reasons": conflict_reasons,
        "acknowledged_at": acknowledged_at,
        "first_acknowledged_at": acknowledged_at,
        "notification_status": "new",
        "should_notify": True,
        "repeat_count": 0,
        "formalization_blocked": True,
        "follow_up": {
            "status": "open",
            "routes": ["RULES", "SKILL_EVOLUTION"],
            "guidance": (
                "Use RULES only when canonical business truth must change. Use SKILL_EVOLUTION "
                "when matching, routing, or guard behavior is wrong. Do not repair either during QUERY."
            ),
        },
        "resolution_required": (
            "Before formalization, make the SQL comply with current canonical rules "
            "or update the canonical rule through an independently authorized RULES request."
        ),
    }


def acknowledge_temporary_rule_override(
    contract: dict[str, Any] | None,
    prior_versions: list[dict[str, Any]] | None,
    *,
    acknowledged_at: str,
) -> dict[str, Any]:
    """Mark repeated conflict signatures as already acknowledged in one family."""

    current = copy.deepcopy(contract) if isinstance(contract, dict) else {}
    signature = str(current.get("conflict_signature") or "")
    if not current.get("enabled") or not signature:
        return {}
    prior_match: dict[str, Any] | None = None
    for version in reversed(prior_versions or []):
        if not isinstance(version, dict):
            continue
        candidate = version.get("temporary_rule_override")
        if not isinstance(candidate, dict):
            continue
        if candidate.get("enabled") and str(candidate.get("conflict_signature") or "") == signature:
            prior_match = candidate
            break
    current["acknowledged_at"] = acknowledged_at
    if prior_match:
        current["first_acknowledged_at"] = (
            prior_match.get("first_acknowledged_at")
            or prior_match.get("acknowledged_at")
            or acknowledged_at
        )
        current["notification_status"] = "acknowledged"
        current["should_notify"] = False
        current["repeat_count"] = int(prior_match.get("repeat_count") or 0) + 1
    else:
        current["first_acknowledged_at"] = current.get("first_acknowledged_at") or acknowledged_at
        current["notification_status"] = "new"
        current["should_notify"] = True
        current["repeat_count"] = 0
    return current


def unresolved_temporary_rule_override(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("enabled")
        and value.get("formalization_blocked")
        and value.get("conflict_signature")
    )
