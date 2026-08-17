#!/usr/bin/env python3
"""Pre-bind visualization validator that reuses the canonical workbook audit."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from sql_result_inspector import inspect_result_file
from sql_result_visualization import VIZ_TOKENS, inspect_visual_workbook


SCHEMA_VERSION = "visualization_prebind_validation_v1"
MESSAGE_RULES = [
    ("merged cells", "VIS-LAYOUT-001"),
    ("no Excel chart", "VIS-CHART-001"),
    ("no visible chart title", "VIS-TITLE-001"),
    ("overlays its title", "VIS-TITLE-002"),
    ("normalized composition", "VIS-AXIS-001"),
    ("omits available percentile points", "VIS-PERCENTILE-001"),
    ("contrast", "VIS-CONTRAST-001"),
    ("valid XLSX", "VIS-PACKAGE-001"),
]


def _blocker(exc: Exception) -> dict[str, str]:
    message = str(exc)
    match = re.match(r"\[([A-Z0-9-]+)\]\s*", message)
    inferred_rule = next(
        (rule_id for fragment, rule_id in MESSAGE_RULES if fragment.casefold() in message.casefold()),
        "VIS-VALIDATION-001",
    )
    return {
        "rule_id": match.group(1) if match else inferred_rule,
        "message": message[match.end() :] if match else message,
    }


def validate_workbook(workbook: Path, result_files: list[Path]) -> dict[str, Any]:
    try:
        inspections = [inspect_result_file(path.resolve()) for path in result_files]
        columns = [
            str(column)
            for inspection in inspections
            for column in inspection.get("columns", [])
        ]
        check = inspect_visual_workbook(
            workbook.resolve(),
            required_percentile_fields=columns,
            result_inspections=inspections,
        )
    except (OSError, ValueError) as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "tokens_version": VIZ_TOKENS["schema_version"],
            "workbook": str(workbook.resolve()),
            "result_files": [str(path.resolve()) for path in result_files],
            "blockers": [_blocker(exc)],
            "checks": None,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "tokens_version": VIZ_TOKENS["schema_version"],
        "workbook": str(workbook.resolve()),
        "result_files": [str(path.resolve()) for path in result_files],
        "blockers": [],
        "checks": check,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, help="Reusable XLSX workbook to validate before binding.")
    parser.add_argument(
        "--result-file",
        action="append",
        required=True,
        type=Path,
        help="Exact returned CSV/XLSX result. Repeat for grouped/overall bundles.",
    )
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    receipt = validate_workbook(args.workbook, args.result_file)
    if args.format == "json":
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    elif receipt["status"] == "pass":
        print("PASS: visualization workbook is ready for rendered QA and binding.")
    else:
        for blocker in receipt["blockers"]:
            print(f"BLOCKED [{blocker['rule_id']}]: {blocker['message']}")
    return 0 if receipt["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
