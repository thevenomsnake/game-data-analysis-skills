#!/usr/bin/env python3
"""Print the SQL Engineering Skill function menu."""

from __future__ import annotations

import argparse
import json
import sys
from capability_registry import capabilities, load_registry


MENU_ITEMS = list(capabilities())


def render_markdown(*, include_all: bool = False) -> str:
    registry = load_registry()
    lines = [
        "# SQL Engineering Skill 功能",
        "",
        "使用规则：skill 会根据用户意图自动选择功能；也可以用 `【中文功能】`、`[function_id]` 或裸 function_id 显式指定。",
        "直接运行会写资产、报告、目录或审核状态的脚本时，必须传 `--user-request`；`--function-selection` 只用于可选的显式路线固定。",
        "普通使用只需从下面六个动作出发；专项工具由 skill 自动路由。",
        "",
        "| 常用动作 | 对应功能 | 做什么 |",
        "|---|---|---|",
    ]
    for action in registry["common_actions"]:
        routed = "、".join(f"[{function_id}]" for function_id in action["function_ids"])
        lines.append(f"| {action['label']} | {routed} | {action['description']} |")
    if include_all:
        lines.extend(
            [
                "",
                "## 全部专项功能",
                "",
                "| 选择 | Mode | 可见性 | 适用场景 | 需要补充 |",
                "|---|---|---|---|---|",
            ]
        )
        for item in MENU_ITEMS:
            required = "；".join(item.required_context) if item.required_context else "无"
            lines.append(
                f"| 【{item.label}】 [{item.id}] | `{item.governance_mode}` | `{item.audience}` | {item.use_when} | {required} |"
            )
    else:
        lines.append(
            "\n需要查看所有专项入口时运行 `skill_menu.py --all`，完整机器契约见 `references/capability-map.md`。"
        )
    return "\n".join(lines) + "\n"


def render_json() -> str:
    registry = load_registry()
    return json.dumps(
        {
            "selection_contract": {
                "accepted_brackets": ["【】", "[]"],
                "accepted_bare_ids": True,
                "required_when_ambiguous": False,
                "workflow_script_gate": {
                    "required_args_for_asset_changing_commands": ["--user-request"],
                    "optional_args": ["--function-selection"],
                    "selection_must_appear_in_user_request": False,
                    "agent_may_infer_function": True,
                },
                "examples": ["【需求判定】", "【SQL审查】", "【SQL固化】", "【SQL仓库】", "[REQUIREMENT_INTAKE]", "[REVIEW]", "[SQL_FORMALIZE]", "[SQL_REPOSITORY]", "REVIEW"],
            },
            "common_actions": registry["common_actions"],
            "governance_modes": registry["governance_modes"],
            "items": [item.as_dict() for item in MENU_ITEMS],
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--all", action="store_true", help="Include advanced and integration capabilities in Markdown output")
    args = parser.parse_args()
    print(render_json() if args.format == "json" else render_markdown(include_all=args.all), end="")


if __name__ == "__main__":
    main()
