#!/usr/bin/env python3
"""Shared optional function-routing gate for SQL Engineering Skill scripts."""

from __future__ import annotations

import re
import sys
import hashlib
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Iterable

from capability_registry import capabilities
from skill_menu import render_markdown


BRACKET_PATTERN = re.compile(r"(【[^】]+】|\[[^\]]+\])")
RULE_WRITE_AUTHORIZATION_VERSION = "rule_write_authorization_v1"
RULE_WRITE_ACTION_PATTERNS = {
    "proposed": re.compile(
        r"(?:保存|新增|添加|登记|记录|提出|提议|修改|更新|订正|写入|沉淀|propos(?:e|ed)|save|add|update).{0,20}(?:口径|规则)|"
        r"(?:口径|规则).{0,20}(?:保存|新增|添加|登记|记录|提出|提议|修改|更新|订正|写入|沉淀|待确认|propos(?:e|ed)|save|add|update)",
        re.I,
    ),
    "confirmed": re.compile(
        r"(?:确认|正式|以后|后续|统一|全项目|订正|改为|更新|替换|confirm(?:ed)?|approve).{0,24}(?:口径|规则|按)|"
        r"(?:口径|规则).{0,24}(?:确认|正式|以后|后续|统一|订正|改为|更新|替换|confirm(?:ed)?|approve)",
        re.I,
    ),
    "deprecated": re.compile(
        r"(?:废弃|停用|弃用|删除|作废|deprecat(?:e|ed)|disable|retire).{0,20}(?:口径|规则)|"
        r"(?:口径|规则).{0,20}(?:废弃|停用|弃用|删除|作废|deprecat(?:e|ed)|disable|retire)",
        re.I,
    ),
}


@dataclass(frozen=True)
class FunctionSelection:
    raw: str
    function_id: str
    label: str
    mode: str


def is_bracketed_selection(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    return (
        (stripped.startswith("【") and stripped.endswith("】"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    )


def strip_brackets(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("【") and stripped.endswith("】"):
        return stripped[1:-1].strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    return stripped


def _selection_index() -> dict[str, FunctionSelection]:
    index: dict[str, FunctionSelection] = {}
    items = capabilities()
    for item in items:
        selection = FunctionSelection(
            raw=f"[{item.id}]",
            function_id=item.id,
            label=item.label,
            mode=item.governance_mode,
        )
        index[item.id.lower()] = selection
        index[item.label.lower()] = FunctionSelection(
            raw=f"【{item.label}】",
            function_id=item.id,
            label=item.label,
            mode=item.governance_mode,
        )
        for alias in item.aliases:
            index[alias.lower()] = FunctionSelection(
                raw=f"【{alias}】",
                function_id=item.id,
                label=item.label,
                mode=item.governance_mode,
            )
    return index


def allowed_selection_examples(allowed_ids: Iterable[str] | None) -> list[str]:
    allowed = {item.lower() for item in allowed_ids or []}
    examples = []
    for item in capabilities():
        if not allowed or item.id.lower() in allowed:
            examples.append(f"【{item.label}】 or [{item.id}]")
    return examples


def normalize_function_selection(value: str | None) -> FunctionSelection | None:
    if not value or not value.strip():
        return None
    key = strip_brackets(value).lower()
    return _selection_index().get(key)


def extract_bracketed_selections(text: str | None) -> list[str]:
    if not text:
        return []
    return BRACKET_PATTERN.findall(text)


def user_request_contains_selection(user_request: str | None, selection: FunctionSelection) -> bool:
    for raw in extract_bracketed_selections(user_request):
        normalized = normalize_function_selection(raw)
        if normalized and normalized.function_id == selection.function_id:
            return True
    return False


def block_message(
    *,
    purpose: str,
    blockers: Iterable[str],
    needed_from_user: Iterable[str] | None = None,
) -> str:
    lines = [
        "BLOCKED",
        "",
        "blockers:",
    ]
    lines.extend(f"  - {item}" for item in blockers)
    lines.extend(
        [
            "",
            "needed_from_user:",
        ]
    )
    needs = list(needed_from_user or [])
    if not needs:
        needs = [
            f"Provide a valid function choice for {purpose}, such as `【中文功能】`, `[function_id]`, or a bare function id.",
        ]
    lines.extend(f"  - {item}" for item in needs)
    lines.extend(
        [
            "",
            "allowed_next_steps:",
            "  - Choose from the function menu below.",
            "",
            render_markdown(include_all=True).rstrip(),
            "",
        ]
    )
    return "\n".join(lines)


def require_user_request(user_request: str | None, *, purpose: str) -> None:
    if user_request and user_request.strip():
        return
    raise FunctionGateError(
        block_message(
            purpose=purpose,
            blockers=[
                f"`--user-request` is required for {purpose} because this command writes or persists skill artifacts.",
                "Auto routing is allowed for clear intent, but asset-changing script runs must keep the original user request for audit.",
            ],
            needed_from_user=[
                'Pass the verbatim user request with `--user-request "<original request>"`.',
                "Use `--function-selection` only when an explicit route improves reproducibility; invalid explicit routes will still be blocked.",
            ],
        )
    )


def require_user_function_selection(
    selection_value: str | None,
    *,
    user_request: str | None,
    allowed_ids: Iterable[str] | None,
    purpose: str,
) -> FunctionSelection:
    examples = allowed_selection_examples(allowed_ids)
    selection = normalize_function_selection(selection_value)
    if not selection:
        if selection_value and selection_value.strip():
            raise FunctionGateError(
                block_message(
                    purpose=purpose,
                    blockers=[
                        f"Function selection `{selection_value}` is not a known SQL Engineering Skill function.",
                        "Use the exact menu label or function id; invalid explicit routes are blocked.",
                    ],
                    needed_from_user=[
                        f"Provide a valid function choice for {purpose}.",
                        "Allowed here: " + "; ".join(examples),
                    ],
                )
            )
        allowed = list(allowed_ids or [])
        if len(allowed) == 1:
            inferred = normalize_function_selection(allowed[0])
            if inferred:
                return inferred
        return FunctionSelection(
            raw="[AUTO]",
            function_id="AUTO",
            label="自动路由",
            mode=purpose,
        )

    allowed = {item.lower() for item in allowed_ids or []}
    if allowed and selection.function_id.lower() not in allowed:
        raise FunctionGateError(
            block_message(
                purpose=purpose,
                blockers=[
                    f"Function selection `{selection.raw}` maps to `{selection.function_id}`, which is not allowed for {purpose}.",
                    "Do not reuse a bracket label from another workflow.",
                ],
                needed_from_user=[
                    f"Provide a valid function choice for {purpose}.",
                    "Allowed here: " + "; ".join(examples),
                ],
            )
        )

    return selection


def require_explicit_rule_write_authorization(
    selection_value: str | None,
    *,
    user_request: str | None,
    requested_status: str,
) -> dict[str, str | bool]:
    """Require a user-owned RULES capability before mutating canonical rules."""

    request = str(user_request or "").strip()
    selection = normalize_function_selection(selection_value)
    blockers: list[str] = []
    if not selection or selection.function_id != "RULES":
        blockers.append(
            "Canonical rule writes require an explicit `--function-selection RULES`; auto-routed QUERY/REVIEW/DASHBOARD work is read-only for canonical rules."
        )
    if not request or not selection or not user_request_contains_selection(request, selection):
        blockers.append(
            "The verbatim user request must contain `【口径管理】` or `[RULES]`; passing a RULES flag only from the agent is not user authorization."
        )
    action_pattern = RULE_WRITE_ACTION_PATTERNS.get(requested_status)
    if not action_pattern or not action_pattern.search(request):
        blockers.append(
            f"The user request does not explicitly authorize a `{requested_status}` canonical-rule write. Reading or temporarily changing SQL is not rule-write permission."
        )
    if blockers:
        raise FunctionGateError(
            block_message(
                purpose="canonical rule write",
                blockers=blockers,
                needed_from_user=[
                    "Send a separate rule-management request that names the durable action, for example `【口径管理】保存为待确认口径：...`.",
                    "For confirmed rules, explicitly confirm the exact future-use definition and pass `--confirmed-by-user`.",
                ],
            )
        )

    authorized_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "contract_version": RULE_WRITE_AUTHORIZATION_VERSION,
        "function_id": "RULES",
        "selection": selection.raw,
        "requested_status": requested_status,
        "user_request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "explicit_user_selection": True,
        "authorized_at": authorized_at,
    }


def add_function_gate_arguments(parser, *, selection_help: str) -> None:
    parser.add_argument(
        "--function-selection",
        help=selection_help,
    )
    parser.add_argument(
        "--user-request",
        help="Verbatim user request for audit context. Required for asset-changing workflow commands.",
    )


def exit_with_gate_error(parser, error: Exception) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser.exit(2, str(error).rstrip() + "\n")


class FunctionGateError(ValueError):
    """Raised when an explicitly provided function choice is invalid."""
