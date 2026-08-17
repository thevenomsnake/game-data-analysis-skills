#!/usr/bin/env python3
"""Load and render the SQL Engineering capability registry."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = SKILL_ROOT / "references" / "capabilities.json"
REFERENCE_PATH = SKILL_ROOT / "references" / "capability-map.md"
REGISTRY_SCHEMA_VERSION = "sql_capability_registry_v1"
AUDIENCES = {"common", "advanced", "integration"}


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    governance_mode: str
    audience: str
    use_when: str
    required_context: tuple[str, ...]
    entrypoints: tuple[str, ...]
    writes_assets: bool
    llm_policy: str
    quality_profile: str
    references: tuple[str, ...]
    outputs: tuple[str, ...]
    aliases: tuple[str, ...]

    @property
    def command_hint(self) -> str:
        return f"【{self.label}】 或 [{self.id}]"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "governance_mode": self.governance_mode,
            "mode": self.governance_mode,
            "audience": self.audience,
            "use_when": self.use_when,
            "required_context": list(self.required_context),
            "entrypoints": list(self.entrypoints),
            "writes_assets": self.writes_assets,
            "llm_policy": self.llm_policy,
            "quality_profile": self.quality_profile,
            "references": list(self.references),
            "outputs": list(self.outputs),
            "aliases": list(self.aliases),
            "command_hint": self.command_hint,
        }


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"capability registry `{field}` must be an array of non-empty strings")
    return tuple(item.strip() for item in value)


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValueError(f"unsupported capability registry schema: {data.get('schema_version')!r}")
    modes = _string_list(data.get("governance_modes"), "governance_modes")
    functions = data.get("functions")
    if not isinstance(functions, list) or not functions:
        raise ValueError("capability registry `functions` must be a non-empty array")
    ids: set[str] = set()
    labels: set[str] = set()
    for row in functions:
        if not isinstance(row, dict):
            raise ValueError("each capability must be an object")
        function_id = str(row.get("id") or "").strip()
        label = str(row.get("label") or "").strip()
        mode = str(row.get("governance_mode") or "").strip()
        audience = str(row.get("audience") or "").strip()
        if not function_id or function_id in ids:
            raise ValueError(f"capability id is missing or duplicated: {function_id!r}")
        if not label or label in labels:
            raise ValueError(f"capability label is missing or duplicated: {label!r}")
        if mode not in modes:
            raise ValueError(f"capability {function_id} uses unknown governance mode {mode!r}")
        if audience not in AUDIENCES:
            raise ValueError(f"capability {function_id} uses unknown audience {audience!r}")
        for field in ["required_context", "entrypoints", "references", "outputs", "aliases"]:
            _string_list(row.get(field), f"{function_id}.{field}")
        ids.add(function_id)
        labels.add(label)
    protected_writes = data.get("protected_writes")
    if not isinstance(protected_writes, list) or not protected_writes:
        raise ValueError("capability registry `protected_writes` must be a non-empty array")
    protected_scopes: set[str] = set()
    for row in protected_writes:
        if not isinstance(row, dict):
            raise ValueError("each protected write policy must be an object")
        scope = str(row.get("scope") or "").strip()
        if not scope or scope in protected_scopes:
            raise ValueError(f"protected write scope is missing or duplicated: {scope!r}")
        allowed = _string_list(
            row.get("allowed_function_ids"),
            f"protected_writes.{scope}.allowed_function_ids",
        )
        unknown = sorted(set(allowed) - ids)
        if unknown:
            raise ValueError(
                f"protected write scope {scope} references unknown capabilities: {', '.join(unknown)}"
            )
        if not str(row.get("reason") or "").strip():
            raise ValueError(f"protected write scope {scope} requires a reason")
        protected_scopes.add(scope)
    common_actions = data.get("common_actions")
    if not isinstance(common_actions, list) or not common_actions:
        raise ValueError("capability registry `common_actions` must be a non-empty array")
    for action in common_actions:
        function_ids = _string_list(action.get("function_ids"), "common_actions.function_ids")
        unknown = sorted(set(function_ids) - ids)
        if unknown:
            raise ValueError(f"common action references unknown capabilities: {', '.join(unknown)}")
    command_routes = data.get("command_routes")
    if not isinstance(command_routes, dict) or not command_routes:
        raise ValueError("capability registry `command_routes` must be a non-empty object")
    for script_name, command_map in command_routes.items():
        if not isinstance(script_name, str) or not script_name.endswith(".py"):
            raise ValueError(f"command route script must be a Python file name: {script_name!r}")
        if not isinstance(command_map, dict) or "*" not in command_map:
            raise ValueError(f"command route {script_name} must define a `*` fallback")
        for command, function_ids in command_map.items():
            if not isinstance(command, str) or not command.strip():
                raise ValueError(f"command route {script_name} contains an empty command")
            route_ids = _string_list(function_ids, f"command_routes.{script_name}.{command}")
            unknown = sorted(set(route_ids) - ids)
            if unknown:
                raise ValueError(
                    f"command route {script_name} {command} references unknown capabilities: {', '.join(unknown)}"
                )
    return data


def capabilities() -> tuple[Capability, ...]:
    rows: list[Capability] = []
    for row in load_registry()["functions"]:
        rows.append(
            Capability(
                id=row["id"],
                label=row["label"],
                governance_mode=row["governance_mode"],
                audience=row["audience"],
                use_when=row["use_when"],
                required_context=tuple(row["required_context"]),
                entrypoints=tuple(row["entrypoints"]),
                writes_assets=bool(row["writes_assets"]),
                llm_policy=row["llm_policy"],
                quality_profile=row["quality_profile"],
                references=tuple(row["references"]),
                outputs=tuple(row["outputs"]),
                aliases=tuple(row["aliases"]),
            )
        )
    return tuple(rows)


def capability_ids() -> set[str]:
    return {item.id for item in capabilities()}


def governance_modes() -> set[str]:
    return set(load_registry()["governance_modes"])


def protected_write_policies() -> dict[str, dict[str, Any]]:
    """Return protected write scopes keyed by their stable scope id."""

    return {
        str(item["scope"]): dict(item)
        for item in load_registry()["protected_writes"]
    }


def command_routes(script_name: str) -> dict[str, set[str]]:
    """Return all registry-owned command routes for one script."""
    name = Path(script_name).name
    command_map = load_registry()["command_routes"].get(name)
    if not isinstance(command_map, dict):
        raise KeyError(f"no command route registered for {name}")
    return {
        command: set(_string_list(route, f"command_routes.{name}.{command}"))
        for command, route in command_map.items()
    }


def command_function_ids(script_name: str, command: str | None = None) -> set[str]:
    """Return the registry-owned function ids accepted by one CLI command."""
    name = Path(script_name).name
    command_map = command_routes(name)
    route = command_map.get(command) if command else None
    if route is None:
        route = command_map.get("*")
    if route is None:
        raise KeyError(f"no fallback command route registered for {name}")
    return set(route)


def capability_index() -> dict[str, Capability]:
    return {item.id: item for item in capabilities()}


def selected_capability(function_id: str) -> Capability:
    item = capability_index().get(function_id.strip().upper())
    if item is None:
        raise KeyError(f"unknown capability: {function_id}")
    return item


def render_reference() -> str:
    data = load_registry()
    by_id = capability_index()
    lines = [
        "# SQL Capability Map",
        "",
        "This file is generated from `references/capabilities.json`. Edit the registry, then regenerate this file; do not maintain a second capability list by hand.",
        "",
        "## Common Actions",
        "",
        "| Action | Routed functions | Purpose |",
        "|---|---|---|",
    ]
    for action in data["common_actions"]:
        routed = "、".join(f"`{item}`" for item in action["function_ids"])
        lines.append(f"| {action['label']} | {routed} | {action['description']} |")
    lines.extend(
        [
            "",
            "## Capability Contract",
            "",
            "| Function | Mode | Visibility | Entry points | LLM policy | Quality profile | Output |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for item in capabilities():
        lines.append(
            "| "
            + f"【{item.label}】 `[{item.id}]` | `{item.governance_mode}` | `{item.audience}` | "
            + "<br>".join(f"`{entry}`" for entry in item.entrypoints)
            + f" | `{item.llm_policy}` | `{item.quality_profile}` | "
            + "；".join(item.outputs)
            + " |"
        )
    lines.extend(["", "## Reference Routing", ""])
    for function_id in sorted(by_id):
        item = by_id[function_id]
        refs = "、".join(f"`{ref}`" for ref in item.references) or "无"
        context = "；".join(item.required_context) or "无"
        lines.append(f"- `{function_id}`: context={context}; references={refs}.")
    lines.extend(["", "## Protected Writes", "", "| Scope | Accepted functions | Reason |", "|---|---|---|"])
    for item in data["protected_writes"]:
        accepted = "、".join(f"`{value}`" for value in item["allowed_function_ids"])
        lines.append(f"| `{item['scope']}` | {accepted} | {item['reason']} |")
    lines.extend(["", "## Command Gates", "", "| Script / command | Accepted functions |", "|---|---|"])
    for script_name, command_map in data["command_routes"].items():
        for command, function_ids in command_map.items():
            accepted = "、".join(f"`{item}`" for item in function_ids)
            lines.append(f"| `{script_name} {command}` | {accepted} |")
    return "\n".join(lines) + "\n"


def validate_reference() -> list[str]:
    expected = render_reference()
    if not REFERENCE_PATH.exists():
        return [f"missing generated capability map: {REFERENCE_PATH}"]
    actual = REFERENCE_PATH.read_text(encoding="utf-8")
    return [] if actual == expected else ["references/capability-map.md is stale; regenerate it from capabilities.json"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--write-reference", action="store_true")
    parser.add_argument("--user-request", default="")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--capability", help="Render one capability contract without the full registry")
    args = parser.parse_args()
    if args.capability:
        try:
            item = selected_capability(args.capability)
        except KeyError as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "status": "pass",
                    "schema_version": load_registry()["schema_version"],
                    "capability": item.as_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.write_reference:
        if not args.user_request.strip():
            parser.error("--write-reference requires the verbatim --user-request audit context")
        REFERENCE_PATH.write_text(render_reference(), encoding="utf-8")
    problems = validate_reference() if args.validate else []
    if args.format == "markdown":
        print(render_reference(), end="")
    else:
        print(
            json.dumps(
                {
                    "status": "pass" if not problems else "fail",
                    "schema_version": load_registry()["schema_version"],
                    "capability_count": len(capabilities()),
                    "governance_modes": sorted(governance_modes()),
                    "problems": problems,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
