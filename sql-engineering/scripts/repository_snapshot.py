#!/usr/bin/env python3
"""Load the persisted repository summary without re-analyzing SQL."""

from __future__ import annotations

import copy
from typing import Any


REQUIRED_TEXT_FIELDS = ("display_title", "purpose", "base_population", "grain")
RULE_STATUSES = {"matched", "conflict", "needs_manual_check", "unique"}


def validate_persisted_repository_summary(summary: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in REQUIRED_TEXT_FIELDS:
        if not str(summary.get(field) or "").strip():
            problems.append(f"repository_summary.{field} is required")
    if summary.get("canonical_rule_status") not in RULE_STATUSES:
        problems.append(
            "repository_summary.canonical_rule_status must be matched, conflict, "
            "needs_manual_check, or unique"
        )
    for field in ("metrics", "metric_groups", "applied_criteria"):
        if not isinstance(summary.get(field), list) or not summary.get(field):
            problems.append(f"repository_summary.{field} must not be empty")
    source_logs = summary.get("source_logs")
    external_sources = summary.get("external_sources")
    if not (isinstance(source_logs, list) and source_logs) and not (
        isinstance(external_sources, list) and external_sources
    ):
        problems.append(
            "repository_summary.source_logs or repository_summary.external_sources "
            "must contain a declared source"
        )
    return problems


def persisted_repository_snapshot(spec: dict[str, Any]) -> dict[str, Any]:
    """Return one immutable viewer snapshot and a visible diagnostic state."""

    summary = spec.get("repository_summary") if isinstance(spec, dict) else None
    if not isinstance(summary, dict) or not summary:
        return {
            "schema_version": "repository_snapshot_v1",
            "status": "missing",
            "summary": {},
            "problems": ["repository_summary is missing from the persisted QUERY sidecar"],
        }
    problems = validate_persisted_repository_summary(summary)
    return {
        "schema_version": "repository_snapshot_v1",
        "status": "invalid" if problems else "ready",
        "summary": copy.deepcopy(summary),
        "problems": problems,
    }
