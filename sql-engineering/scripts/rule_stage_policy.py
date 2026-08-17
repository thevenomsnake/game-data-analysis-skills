"""Lifecycle-stage routing for canonical rule constraints."""

from __future__ import annotations

from typing import Any, Iterable


LIFECYCLE_STAGES = {
    "temporary_query",
    "retained_query",
    "validation",
    "dashboard_delivery",
    "review",
}
MODE_DEFAULT_STAGE = {
    "temporary": "temporary_query",
    "generation": "retained_query",
    "formalize": "retained_query",
    "review": "review",
}
STAGE_ALIASES = {
    "temporary": {"temporary_query"},
    "temporary_query": {"temporary_query"},
    "query": {"temporary_query", "retained_query"},
    "formal_query": {"retained_query"},
    "retained_query": {"retained_query"},
    "validation": {"validation"},
    "dashboard": {"dashboard_delivery"},
    "dashboard_delivery": {"dashboard_delivery"},
    "verified_dashboard": {"dashboard_delivery"},
    "review": {"review"},
}


def _values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    source: Iterable[Any] = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item or "").strip().lower() for item in source if str(item or "").strip()]


def normalize_lifecycle_stage(value: str | None, *, mode: str) -> str:
    requested = str(value or "").strip().lower()
    if not requested:
        requested = MODE_DEFAULT_STAGE.get(str(mode or "").strip().lower(), "retained_query")
    expanded = STAGE_ALIASES.get(requested, {requested})
    if len(expanded) != 1:
        raise ValueError(f"Lifecycle stage must be specific, not `{requested}`.")
    stage = next(iter(expanded))
    if stage not in LIFECYCLE_STAGES:
        raise ValueError(
            f"Unsupported lifecycle stage `{requested}`; expected one of {sorted(LIFECYCLE_STAGES)}."
        )
    return stage


def constraint_declared_stages(constraint: dict[str, Any]) -> set[str]:
    """Return explicit stages from the preferred or legacy rule field."""

    raw = _values(constraint.get("applies_in"))
    if not raw:
        raw = _values(constraint.get("required_for"))
    stages: set[str] = set()
    unknown: list[str] = []
    for item in raw:
        expanded = STAGE_ALIASES.get(item)
        if expanded is None:
            unknown.append(item)
            continue
        stages.update(expanded)
    if unknown:
        raise ValueError(
            "Unsupported lifecycle stage aliases in canonical constraint: "
            + ", ".join(sorted(set(unknown)))
        )
    return stages


def constraint_applies_to_stage(
    constraint: dict[str, Any],
    lifecycle_stage: str,
) -> bool:
    declared = constraint_declared_stages(constraint)
    return not declared or lifecycle_stage in declared


def partition_constraints_for_stage(
    constraints: list[dict[str, Any]],
    lifecycle_stage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active: list[dict[str, Any]] = []
    inactive: list[dict[str, Any]] = []
    for constraint in constraints:
        target = active if constraint_applies_to_stage(constraint, lifecycle_stage) else inactive
        target.append(constraint)
    return active, inactive
