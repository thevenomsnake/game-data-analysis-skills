#!/usr/bin/env python3
"""Block direct writes to protected SQL skill and canonical-rule paths by route."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from capability_registry import protected_write_policies  # noqa: E402
from function_gate import normalize_function_selection  # noqa: E402


SKILL_DIR_NAME = "sql-engineering"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def classify_protected_scopes(path: Path, *, repo_root: Path) -> list[str]:
    """Return protected scope ids matched by one proposed write path."""

    resolved = path.resolve()
    repo_root = repo_root.resolve()
    scopes: list[str] = []
    source_skill = repo_root / SKILL_DIR_NAME
    lowered_parts = [item.lower() for item in resolved.parts]

    is_source_skill = _is_within(resolved, source_skill)
    is_runtime_skill = any(
        lowered_parts[index : index + 2] == ["skills", SKILL_DIR_NAME]
        for index in range(max(0, len(lowered_parts) - 1))
    )
    if is_source_skill or is_runtime_skill:
        scopes.append("skill_source")

    parent_name = resolved.parent.name.lower()
    file_name = resolved.name.lower()
    is_project_rule_file = parent_name == "rules" and file_name in {
        "canonical_rules.json",
        "rule_change_log.md",
    }
    is_project_rule_tree = any(
        lowered_parts[index] == "sql-projects"
        and index + 2 < len(lowered_parts)
        and lowered_parts[index + 2] == "rules"
        for index in range(len(lowered_parts))
    )
    is_rule_concept_registry = (
        parent_name == "_rule_review" and file_name == "rule_concepts.json"
    )
    if is_project_rule_file or is_project_rule_tree or is_rule_concept_registry:
        scopes.append("canonical_rules")

    knowledge_root = repo_root / "knowledge-base"
    is_global_knowledge = _is_within(resolved, knowledge_root)
    is_project_knowledge = any(
        lowered_parts[index] == "sql-projects"
        and index + 2 < len(lowered_parts)
        and lowered_parts[index + 2] == "knowledge"
        for index in range(len(lowered_parts))
    )
    if is_global_knowledge or is_project_knowledge:
        scopes.append("knowledge_assets")

    planning_root = repo_root / "planning-sources"
    is_global_planning = _is_within(resolved, planning_root)
    is_project_planning = any(
        lowered_parts[index] == "sql-projects"
        and index + 2 < len(lowered_parts)
        and lowered_parts[index + 2] == "planning"
        for index in range(len(lowered_parts))
    )
    if is_global_planning or is_project_planning:
        scopes.append("planning_sources")
    return scopes


def evaluate_write_scope(
    *,
    function_selection: str,
    paths: list[Path],
    repo_root: Path,
) -> dict[str, Any]:
    selection = normalize_function_selection(function_selection)
    if not selection:
        return {
            "status": "block",
            "function_id": "",
            "checks": [],
            "blockers": [f"Unknown function selection: {function_selection}"],
        }

    policies = protected_write_policies()
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    for raw_path in paths:
        resolved = raw_path.resolve()
        scopes = classify_protected_scopes(resolved, repo_root=repo_root)
        allowed = True
        reasons: list[str] = []
        for scope in scopes:
            policy = policies[scope]
            accepted = set(policy["allowed_function_ids"])
            if selection.function_id not in accepted:
                allowed = False
                reasons.append(str(policy["reason"]))
                blockers.append(
                    f"{selection.function_id} cannot write protected scope `{scope}`: {resolved}"
                )
        checks.append(
            {
                "path": str(resolved),
                "protected_scopes": scopes,
                "allowed": allowed,
                "reasons": reasons,
            }
        )
    return {
        "status": "pass" if not blockers else "block",
        "function_id": selection.function_id,
        "checks": checks,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function-selection", required=True)
    parser.add_argument("--path", action="append", required=True)
    parser.add_argument("--repo-root", default=str(SCRIPT_DIR.parents[1]))
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args()
    result = evaluate_write_scope(
        function_selection=args.function_selection,
        paths=[Path(item) for item in args.path],
        repo_root=Path(args.repo_root),
    )
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"status: {result['status']}")
        for blocker in result["blockers"]:
            print(f"blocker: {blocker}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
