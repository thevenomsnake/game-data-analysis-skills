#!/usr/bin/env python3
"""Resolve a reproducible QUERY date window from user input or project config."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from capability_registry import command_function_ids
from function_gate import (
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
)


SCHEMA_VERSION = "query_window_v1"
DEFAULT_TIMEZONE_OFFSET = "+08:00"
ALLOWED_MODES = {"project_start_to_yesterday", "missing"}
TIMEZONE_PATTERN = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def parse_iso_date(value: Any, field: str) -> date:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD; got {text!r}.") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must use YYYY-MM-DD; got {text!r}.")
    return parsed


def parse_timezone_offset(value: Any) -> timezone:
    text = str(value or DEFAULT_TIMEZONE_OFFSET).strip()
    match = TIMEZONE_PATTERN.fullmatch(text)
    if not match:
        raise ValueError(f"timezone_offset must use +HH:MM or -HH:MM; got {text!r}.")
    sign, hours_text, minutes_text = match.groups()
    hours = int(hours_text)
    minutes = int(minutes_text)
    if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
        raise ValueError(f"timezone_offset is outside the supported UTC range; got {text!r}.")
    total_minutes = hours * 60 + minutes
    if sign == "-":
        total_minutes = -total_minutes
    return timezone(timedelta(minutes=total_minutes))


def validate_default_query_window(config: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    contract = config.get("default_query_window")
    if not isinstance(contract, dict):
        return ["default_query_window must be configured as an object."]

    mode = str(contract.get("mode") or "").strip()
    if mode not in ALLOWED_MODES:
        problems.append("default_query_window.mode must be project_start_to_yesterday or missing.")
    materialization = str(contract.get("materialization") or "").strip()
    if materialization != "fixed_literals":
        problems.append("default_query_window.materialization must be fixed_literals.")
    try:
        parse_timezone_offset(contract.get("timezone_offset"))
    except ValueError as exc:
        problems.append(str(exc))

    start_text = str(contract.get("project_start_date") or "").strip()
    if mode == "project_start_to_yesterday":
        try:
            parse_iso_date(start_text, "default_query_window.project_start_date")
        except ValueError as exc:
            problems.append(str(exc))
    elif mode == "missing" and start_text:
        problems.append("default_query_window.project_start_date must be empty when mode is missing.")
    return problems


def blocked_payload(*, mode: str, timezone_offset: str, blockers: list[str], as_of: date | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "source": "missing",
        "mode": mode,
        "pt_start": "",
        "pt_end": "",
        "as_of_date": as_of.isoformat() if as_of else "",
        "timezone_offset": timezone_offset,
        "materialization": "fixed_literals",
        "blockers": blockers,
    }


def resolve_query_window(
    config: dict[str, Any],
    *,
    explicit_start: str | None = None,
    explicit_end: str | None = None,
    as_of_date: str | date | None = None,
) -> dict:
    """Resolve inclusive date bounds. Explicit user bounds always win."""

    contract = config.get("default_query_window")
    contract = contract if isinstance(contract, dict) else {}
    timezone_offset = str(contract.get("timezone_offset") or DEFAULT_TIMEZONE_OFFSET).strip()
    try:
        configured_timezone = parse_timezone_offset(timezone_offset)
    except ValueError as exc:
        return blocked_payload(mode="missing", timezone_offset=timezone_offset, blockers=[str(exc)])

    try:
        if isinstance(as_of_date, date):
            as_of = as_of_date
        elif as_of_date:
            as_of = parse_iso_date(as_of_date, "as_of_date")
        else:
            as_of = datetime.now(configured_timezone).date()
    except ValueError as exc:
        return blocked_payload(mode="missing", timezone_offset=timezone_offset, blockers=[str(exc)])

    if bool(explicit_start) != bool(explicit_end):
        return blocked_payload(
            mode="explicit",
            timezone_offset=timezone_offset,
            as_of=as_of,
            blockers=["explicit_start and explicit_end must be provided together."],
        )
    if explicit_start and explicit_end:
        try:
            start = parse_iso_date(explicit_start, "explicit_start")
            end = parse_iso_date(explicit_end, "explicit_end")
        except ValueError as exc:
            return blocked_payload(
                mode="explicit", timezone_offset=timezone_offset, as_of=as_of, blockers=[str(exc)]
            )
        if end < start:
            return blocked_payload(
                mode="explicit",
                timezone_offset=timezone_offset,
                as_of=as_of,
                blockers=["explicit_end must be on or after explicit_start."],
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ready",
            "source": "user_explicit",
            "mode": "explicit",
            "pt_start": start.isoformat(),
            "pt_end": end.isoformat(),
            "as_of_date": as_of.isoformat(),
            "timezone_offset": timezone_offset,
            "materialization": "fixed_literals",
            "blockers": [],
        }

    config_problems = validate_default_query_window(config)
    mode = str(contract.get("mode") or "missing").strip()
    if config_problems or mode != "project_start_to_yesterday":
        blockers = config_problems or [
            "Project default query window is not configured; provide an explicit date range."
        ]
        return blocked_payload(
            mode=mode if mode in ALLOWED_MODES else "missing",
            timezone_offset=timezone_offset,
            as_of=as_of,
            blockers=blockers,
        )

    start = parse_iso_date(contract.get("project_start_date"), "default_query_window.project_start_date")
    end = as_of - timedelta(days=1)
    if end < start:
        return blocked_payload(
            mode=mode,
            timezone_offset=timezone_offset,
            as_of=as_of,
            blockers=[
                f"Yesterday {end.isoformat()} is before project start date {start.isoformat()}."
            ],
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "source": "project_default",
        "mode": mode,
        "pt_start": start.isoformat(),
        "pt_end": end.isoformat(),
        "as_of_date": as_of.isoformat(),
        "timezone_offset": timezone_offset,
        "materialization": "fixed_literals",
        "blockers": [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="SQL project root containing project_config.json.")
    parser.add_argument("--explicit-start")
    parser.add_argument("--explicit-end")
    parser.add_argument("--as-of-date", help="Optional deterministic clock override for tests/debugging.")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    add_function_gate_arguments(
        parser,
        selection_help="Optional route. Allowed: QUERY, REQUIREMENT_INTAKE, or PROJECT_ADMIN.",
    )
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("query_window.py"),
            purpose="query window resolution",
        )
    except Exception as exc:  # pragma: no cover
        exit_with_gate_error(parser, exc)

    config_path = Path(args.root).resolve() / "project_config.json"
    if not config_path.exists():
        payload = blocked_payload(
            mode="missing",
            timezone_offset=DEFAULT_TIMEZONE_OFFSET,
            blockers=[f"Project config not found: {config_path}"],
        )
    else:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            payload = blocked_payload(
                mode="missing",
                timezone_offset=DEFAULT_TIMEZONE_OFFSET,
                blockers=[f"project_config.json is invalid JSON: {exc}"],
            )
        else:
            payload = resolve_query_window(
                config,
                explicit_start=args.explicit_start,
                explicit_end=args.explicit_end,
                as_of_date=args.as_of_date,
            )
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"status: {payload['status']}")
        print(f"source: {payload['source']}")
        print(f"range: {payload['pt_start']} .. {payload['pt_end']}")
        for blocker in payload["blockers"]:
            print(f"blocker: {blocker}")
    if payload["status"] != "ready":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
