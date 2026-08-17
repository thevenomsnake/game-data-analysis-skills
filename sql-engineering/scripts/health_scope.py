#!/usr/bin/env python3
"""Select active health scope and compact large project-health payloads."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any


SCOPES = {"current", "full"}


def current_artifact(item: dict[str, Any]) -> bool:
    return str(item.get("artifact_state") or "current") == "current" and str(
        item.get("status") or ""
    ) != "superseded"


def artifacts_for_scope(artifacts: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    if scope not in SCOPES:
        raise ValueError(f"Unsupported health scope: {scope}")
    if scope == "full":
        return list(artifacts)
    return [item for item in artifacts if current_artifact(item)]


def workspace_versions_for_scope(
    versions: list[dict[str, Any]],
    current_version: int,
    scope: str,
) -> list[dict[str, Any]]:
    if scope not in SCOPES:
        raise ValueError(f"Unsupported health scope: {scope}")
    if scope == "full":
        return list(versions)
    return [item for item in versions if int(item.get("version") or 0) == current_version]


def deferred_history_summary(
    artifacts: list[dict[str, Any]],
    workspace_entries: list[dict[str, Any]],
    scope: str,
) -> dict[str, Any]:
    if scope == "full":
        return {
            "artifact_count": 0,
            "workspace_version_count": 0,
            "note": "Full scope includes historical artifacts and workspace versions.",
        }
    historical_artifacts = len(artifacts) - len(artifacts_for_scope(artifacts, "current"))
    historical_workspace_versions = 0
    for entry in workspace_entries:
        if not isinstance(entry, dict):
            continue
        versions = [item for item in entry.get("versions", []) if isinstance(item, dict)]
        current_version = int(entry.get("current_version") or 0)
        historical_workspace_versions += max(
            0,
            len(versions) - len(workspace_versions_for_scope(versions, current_version, "current")),
        )
    return {
        "artifact_count": historical_artifacts,
        "workspace_version_count": historical_workspace_versions,
        "note": "Historical debt is outside the active delivery gate; run --scope full for release or migration audit.",
    }


def compact_health_payload(payload: dict[str, Any], *, path_samples: int = 3) -> dict[str, Any]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {
        "fail": defaultdict(lambda: {"count": 0, "messages": [], "paths": []}),
        "warn": defaultdict(lambda: {"count": 0, "messages": [], "paths": []}),
    }
    for check in payload.get("checks", []):
        if not isinstance(check, dict) or check.get("status") not in grouped:
            continue
        row = grouped[str(check["status"])][str(check.get("id") or "unknown")]
        row["count"] += 1
        message = str(check.get("message") or "")
        path = str(check.get("path") or "")
        if message and message not in row["messages"] and len(row["messages"]) < 2:
            row["messages"].append(message)
        if path and path not in row["paths"] and len(row["paths"]) < path_samples:
            row["paths"].append(path)

    def rows(status: str) -> list[dict[str, Any]]:
        return [
            {"id": check_id, **copy.deepcopy(values)}
            for check_id, values in sorted(
                grouped[status].items(),
                key=lambda pair: (-int(pair[1]["count"]), pair[0]),
            )
        ]

    return {
        "schema_version": "project_health_summary_v1",
        "project": payload.get("project"),
        "status": payload.get("status"),
        "root": payload.get("root"),
        "strict": payload.get("strict"),
        "scope": payload.get("scope"),
        "summary": copy.deepcopy(payload.get("summary") or {}),
        "deferred_history": copy.deepcopy(payload.get("deferred_history") or {}),
        "failure_groups": rows("fail"),
        "warning_groups": rows("warn"),
    }
