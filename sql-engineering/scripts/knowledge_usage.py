#!/usr/bin/env python3
"""Own knowledge-use declarations and resolver receipt handoff.

The public interface is deliberately small: build or validate one usage
declaration, load resolver receipts, and constrain optional receipt output to a
project-local working directory. Callers do not need to understand bindings or
reference validation details.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


USAGE_SCHEMA = "knowledge_usage_v1"
USAGE_STATUSES = {"used", "not_used", "not_available", "legacy_unknown"}
DECLARATIONS = {"auto", "not-used", "legacy-unknown"}
RECEIPT_ROOT = Path("query_workspace") / "_working" / "knowledge_receipts"


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Knowledge receipt must contain a JSON object: {path}")
    return payload


def active_dataset_ids(project_root: Path) -> list[str]:
    path = project_root.resolve() / "knowledge" / "bindings.json"
    if not path.exists():
        return []
    payload = _read_json(path)
    return sorted(
        {
            str(item.get("dataset_id") or "")
            for item in payload.get("bindings", [])
            if isinstance(item, dict)
            and item.get("state") == "active"
            and str(item.get("dataset_id") or "")
        }
    )


def safe_resolution_receipt_path(project_root: Path, value: str) -> Path:
    """Resolve one receipt output without allowing a read route to overwrite assets."""

    project_root = project_root.resolve()
    raw = Path(str(value or "").strip())
    if not str(raw):
        raise ValueError("Knowledge receipt output path is empty.")
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("Knowledge receipt output must be project-relative and cannot contain `..`.")
    allowed_root = (project_root / RECEIPT_ROOT).resolve()
    candidate = (allowed_root / raw if len(raw.parts) == 1 else project_root / raw).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError(
            f"Knowledge receipt output must stay under `{RECEIPT_ROOT.as_posix()}`: {value}"
        ) from exc
    if candidate.suffix.lower() != ".json":
        raise ValueError("Knowledge receipt output must use a .json suffix.")
    return candidate


def load_reference_files(project_root: Path, paths: list[str] | None) -> list[dict[str, Any]]:
    """Load compact references from resolver receipts and validate current bindings."""

    from config_knowledge import validate_knowledge_reference

    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in paths or []:
        raw = Path(str(value or "").strip())
        if raw.is_absolute():
            path = raw.resolve()
            allowed_root = (project_root.resolve() / RECEIPT_ROOT).resolve()
            try:
                path.relative_to(allowed_root)
            except ValueError as exc:
                raise ValueError(
                    f"Knowledge reference files must stay under `{RECEIPT_ROOT.as_posix()}`: {value}"
                ) from exc
        else:
            path = safe_resolution_receipt_path(project_root, str(raw))
        payload = _read_json(path)
        reference = payload.get("reference") if isinstance(payload.get("reference"), dict) else payload
        problems = validate_knowledge_reference(project_root, reference)
        if problems:
            raise ValueError("Invalid knowledge reference: " + "; ".join(problems))
        identity = json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if identity not in seen:
            seen.add(identity)
            references.append(copy.deepcopy(reference))
    return references


def build_knowledge_usage(
    project_root: Path,
    references: list[dict[str, Any]] | None,
    *,
    declaration: str = "auto",
    declaration_source: str = "query_save",
) -> dict[str, Any]:
    """Create one explicit usage declaration for an exact SQL version."""

    normalized = str(declaration or "auto").strip().lower()
    if normalized not in DECLARATIONS:
        raise ValueError(f"Unsupported knowledge usage declaration: {declaration!r}")
    rows = [copy.deepcopy(item) for item in (references or [])]
    datasets = sorted({str(item.get("dataset_id") or "") for item in rows if item.get("dataset_id")})
    active = active_dataset_ids(project_root)
    if rows:
        if normalized == "not-used":
            raise ValueError("Knowledge references were supplied but usage was declared not-used.")
        status = "used"
    elif normalized == "not-used":
        status = "not_used"
    elif normalized == "legacy-unknown":
        status = "legacy_unknown"
    elif not active:
        status = "not_available"
    else:
        raise ValueError(
            "Project has active knowledge datasets. Declare `--knowledge-usage not-used` "
            "or pass resolver receipt(s) with `--knowledge-reference-file`."
        )
    return {
        "schema_version": USAGE_SCHEMA,
        "status": status,
        "reference_count": len(rows),
        "dataset_ids": datasets,
        "active_dataset_ids_at_save": active,
        "declaration_source": declaration_source,
    }


def validate_knowledge_usage(
    project_root: Path,
    usage: dict[str, Any],
    references: list[dict[str, Any]] | None,
    *,
    require_current_binding: bool = True,
) -> list[str]:
    """Validate declaration/reference consistency through the same seam callers use."""

    from config_knowledge import validate_knowledge_reference

    problems: list[str] = []
    if not isinstance(usage, dict) or usage.get("schema_version") != USAGE_SCHEMA:
        return [f"knowledge usage schema must be {USAGE_SCHEMA}"]
    status = str(usage.get("status") or "")
    if status not in USAGE_STATUSES:
        problems.append(f"unsupported knowledge usage status: {status!r}")
    rows = references if isinstance(references, list) else []
    reference_count = usage.get("reference_count")
    if not isinstance(reference_count, int) or isinstance(reference_count, bool) or reference_count < 0:
        problems.append("knowledge usage reference_count must be a non-negative integer")
    elif reference_count != len(rows):
        problems.append("knowledge usage reference_count does not match knowledge_references")
    dataset_ids = sorted({str(item.get("dataset_id") or "") for item in rows if isinstance(item, dict)})
    declared_dataset_ids = usage.get("dataset_ids")
    if not isinstance(declared_dataset_ids, list) or any(not isinstance(item, str) for item in declared_dataset_ids):
        problems.append("knowledge usage dataset_ids must be a string array")
    elif sorted(declared_dataset_ids) != dataset_ids:
        problems.append("knowledge usage dataset_ids do not match knowledge_references")
    active_at_save = usage.get("active_dataset_ids_at_save")
    if not isinstance(active_at_save, list) or any(not isinstance(item, str) for item in active_at_save):
        problems.append("knowledge usage active_dataset_ids_at_save must be a string array")
    if not str(usage.get("declaration_source") or "").strip():
        problems.append("knowledge usage declaration_source is required")
    if status == "used" and not rows:
        problems.append("knowledge usage is `used` but has no references")
    if status in {"not_used", "not_available", "legacy_unknown"} and rows:
        problems.append(f"knowledge usage is `{status}` but references are present")
    if require_current_binding and status == "not_available" and active_dataset_ids(project_root):
        problems.append("knowledge usage is `not_available` but the project now has active knowledge bindings")
    for reference in rows:
        if not isinstance(reference, dict):
            problems.append("knowledge reference rows must be objects")
            continue
        problems.extend(
            validate_knowledge_reference(
                project_root,
                reference,
                require_current_binding=require_current_binding,
            )
        )
    return problems
