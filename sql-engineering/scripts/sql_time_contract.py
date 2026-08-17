#!/usr/bin/env python3
"""Project-configured SQL time-window analysis shared by generation gates."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


DATE_LITERAL_RE = re.compile(
    r"'(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?|\d{8}(?:\d{2})?)'"
)
MANAGED_TIME_ALIASES = {"pt_start", "pt_end", "ts_start", "ts_end"}
TIME_CONTRACT_VERSION = "project_time_contract_v1"
TIME_INTEGRITY_POLICY_VERSION = "time_integrity_policy_v1"
TIME_INTEGRITY_MODES = {
    "disabled",
    "report_only",
    "optional",
    "required_when_today",
    "required_when_event_time_or_today",
    "always",
}
TIME_INTEGRITY_DEFAULTS = {
    "contract_version": TIME_INTEGRITY_POLICY_VERSION,
    "mode": "report_only",
    "calendar": "gregorian",
    "date_field": "",
    "time_field": "",
    "date_match": "same_local_date",
    "mismatch_action": "exclude",
    "timezone_offset": "+08:00",
}
SQL_ALIAS_RESERVED_WORDS = {
    "where",
    "join",
    "left",
    "right",
    "full",
    "inner",
    "outer",
    "cross",
    "on",
    "group",
    "order",
    "having",
    "limit",
    "union",
    "qualify",
}
ACTUAL_RANGE_START_KEYS = {
    "实际数据开始时间",
    "实际数据起始时间",
    "actualdatastart",
    "actualdatastarttime",
    "observedstart",
    "observedstarttime",
    "datacoveragestart",
    "datacoveragestarttime",
}
ACTUAL_RANGE_END_KEYS = {
    "实际数据结束时间",
    "实际数据截止时间",
    "actualdataend",
    "actualdataendtime",
    "observedend",
    "observedendtime",
    "datacoverageend",
    "datacoverageendtime",
}
TEMPORAL_OUTPUT_NAME_RE = re.compile(
    r"(?:^|[_\s])(date|time|day|dt|ts|timestamp)(?:$|[_\s])|日期|时间|统计日|数据截至|实际数据|"
    r"actual_data|observed_|data_coverage|cohort|event_time|event_date|stat_date|"
    r"login_time|logout_time|eventtime|logtime|created_at|updated_at",
    re.I,
)
TEMPORAL_OUTPUT_EXCLUDE_RE = re.compile(
    r"(?:open|role|account|device|channel|mode|mission|item|server|session|battle|"
    r"template|entity|task).*id|id$|时长|duration|耗时|间隔",
    re.I,
)
REQUESTED_RANGE_EXPRESSION_RE = re.compile(
    r"\b(?:pt_start|pt_end|ts_start|ts_end)\b|"
    r"\b(?:current_date|current_timestamp|localtimestamp|sysdate)\b|\bnow\s*\(",
    re.I,
)


def strip_sql_comments(sql: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", " ", sql or "", flags=re.S)
    return re.sub(r"--[^\r\n]*", " ", without_block)


def split_top_level_csv(text: str) -> list[str]:
    rows: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if quote:
            if char == quote:
                if quote == "'" and nxt == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            rows.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        rows.append(tail)
    return rows


def _find_matching_paren(sql: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    i = open_index
    while i < len(sql):
        char = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote:
            if char == quote:
                if quote == "'" and nxt == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char == "-" and nxt == "-":
            newline = sql.find("\n", i + 2)
            i = len(sql) if newline == -1 else newline + 1
            continue
        if char == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = len(sql) if end == -1 else end + 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def params_cte_span(sql: str) -> tuple[int, int] | None:
    match = re.search(r"\bwith\s+params\s+as\s*\(", sql or "", flags=re.I)
    if not match:
        return None
    open_index = match.end() - 1
    close_index = _find_matching_paren(sql, open_index)
    if close_index == -1:
        return None
    return open_index + 1, close_index


def params_cte_items(sql: str) -> list[dict[str, str]]:
    span = params_cte_span(sql)
    if not span:
        return []
    body = sql[span[0] : span[1]]
    select_match = re.search(r"\bselect\b(.*)$", body, flags=re.I | re.S)
    if not select_match:
        return []
    rows: list[dict[str, str]] = []
    for item in split_top_level_csv(select_match.group(1)):
        match = re.search(r"\bas\s+`?([A-Za-z_][\w$]*)`?\s*$", item, flags=re.I)
        if not match:
            rows.append({"alias": "", "expression": item})
            continue
        rows.append({"alias": match.group(1), "expression": item[: match.start()].strip()})
    return rows


def params_cte_expressions(sql: str) -> dict[str, str]:
    return {
        str(item.get("alias") or "").lower(): str(item.get("expression") or "").strip()
        for item in params_cte_items(sql)
        if item.get("alias")
    }


def literal_value(expression: str) -> str:
    match = DATE_LITERAL_RE.search(str(expression or ""))
    return match.group(1) if match else ""


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    text = re.sub(r"([+-]\d{2}:?\d{2})$", "", text)
    if re.fullmatch(r"\d{8}(?:\d{2}|\d{4})?", text):
        for fmt in ["%Y%m%d%H%M", "%Y%m%d%H", "%Y%m%d"]:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None
    for fmt in [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_timezone_offset(value: Any) -> timezone:
    text = str(value or "+08:00").strip()
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", text)
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


def date_text(value: str) -> str:
    parsed = parse_datetime(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def datetime_text(value: str) -> str:
    parsed = parse_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else ""


def is_tlog_sql(sql: str) -> bool:
    cleaned = strip_sql_comments(sql).lower()
    return bool(re.search(r"\b(?:from|join)\s+`?[\w.]*?(?:_dsl_|_fht0\b|tdbank)", cleaned))


def project_time_integrity_policy(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the explicit project/profile time-integrity policy.

    Missing policy deliberately means report-only. Matching two clocks is a
    project contract, not a global assumption about every data source.
    """

    raw = (config or {}).get("time_integrity_policy")
    policy = dict(raw) if isinstance(raw, dict) else {}
    result = dict(TIME_INTEGRITY_DEFAULTS)
    result.update(policy)
    return result


def time_integrity_policy_fingerprint(config: dict[str, Any] | None) -> str:
    payload = project_time_integrity_policy(config)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:16]


def time_integrity_config_problems(
    config: dict[str, Any] | None,
    *,
    label: str = "time_integrity_policy",
) -> list[str]:
    raw = (config or {}).get("time_integrity_policy")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{label} must be an object."]
    problems: list[str] = []
    if raw.get("contract_version") != TIME_INTEGRITY_POLICY_VERSION:
        problems.append(f"{label}.contract_version must be {TIME_INTEGRITY_POLICY_VERSION}.")
    mode = str(raw.get("mode") or "").strip()
    if mode not in TIME_INTEGRITY_MODES:
        problems.append(f"{label}.mode must be one of {sorted(TIME_INTEGRITY_MODES)}.")
    if str(raw.get("calendar") or "") != "gregorian":
        problems.append(f"{label}.calendar must be gregorian.")
    date_field = str(raw.get("date_field") or "").strip().strip("`")
    time_field = str(raw.get("time_field") or "").strip().strip("`")
    if mode not in {"disabled", "report_only"}:
        if not date_field or not time_field:
            problems.append(f"{label} matching modes require date_field and time_field.")
        elif date_field.casefold() == time_field.casefold():
            problems.append(f"{label}.date_field and time_field must be different fields.")
    if str(raw.get("date_match") or "") != "same_local_date":
        problems.append(f"{label}.date_match must be same_local_date.")
    if str(raw.get("mismatch_action") or "") != "exclude":
        problems.append(f"{label}.mismatch_action must be exclude.")
    try:
        parse_timezone_offset(raw.get("timezone_offset"))
    except ValueError as exc:
        problems.append(f"{label}: {exc}")
    return problems


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = parse_datetime(value)
    if parsed:
        return parsed.date()
    return None


def _value_has_time(value: Any) -> bool:
    return bool(re.search(r"[ T]\d{1,2}:\d{2}", str(value or "")))


def _alias_bound_operator(sql: str, alias: str, *, lower: bool) -> str:
    operators = r">=|>" if lower else r"<=|<"
    alias_ref = (
        rf"(?:\(\s*select\s+`?{re.escape(alias)}`?\s+from\s+params\s*\)"
        rf"|(?:[A-Za-z_][\w$]*\.)?`?{re.escape(alias)}`?)"
    )
    match = re.search(rf"({operators})\s*{alias_ref}", strip_sql_comments(sql), flags=re.I)
    return match.group(1) if match else ""


def _managed_param_bounds(sql: str) -> dict[str, Any]:
    expressions = params_cte_expressions(sql)
    for lower_alias, upper_alias, basis in [
        ("ts_start", "ts_end", "managed_time_params"),
        ("pt_start", "pt_end", "managed_partition_params"),
    ]:
        start_value = literal_value(expressions.get(lower_alias, ""))
        end_value = literal_value(expressions.get(upper_alias, ""))
        lower_operator = _alias_bound_operator(sql, lower_alias, lower=True)
        upper_operator = _alias_bound_operator(sql, upper_alias, lower=False)
        if start_value and end_value and lower_operator and upper_operator:
            return {
                "basis": basis,
                "field": "",
                "lower": (lower_operator, start_value),
                "upper": (upper_operator, end_value),
            }
    return {}


def requested_window_bounds(
    sql: str,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    policy = project_time_policy(config)
    candidates = [
        (
            "business_time_bounds",
            str(policy.get("business_time_field") or ""),
            ("ts_start",),
            ("ts_end",),
        ),
        (
            "partition_bounds",
            str(policy.get("partition_field") or ""),
            ("pt_start",),
            ("pt_end",),
        ),
    ]
    for basis, field, lower_aliases, upper_aliases in candidates:
        if not field:
            continue
        bounds = resolved_bounds(sql, field, lower_aliases, upper_aliases)
        if bounds.get("lower") and bounds.get("upper"):
            return {
                "basis": basis,
                "field": field,
                "lower": bounds["lower"],
                "upper": bounds["upper"],
            }
    return _managed_param_bounds(sql)


def _normalized_requested_bounds(bounds: dict[str, Any]) -> dict[str, Any]:
    lower = bounds.get("lower")
    upper = bounds.get("upper")
    if not lower or not upper:
        return {}
    start = parse_datetime(lower[1])
    end = parse_datetime(upper[1])
    if not start or not end:
        return {}
    precision = "datetime" if (
        bounds.get("basis") == "business_time_bounds"
        or _value_has_time(lower[1])
        or _value_has_time(upper[1])
    ) else "date"
    end_exclusive = upper[0] == "<"
    inclusive_end_date = end.date()
    if end_exclusive and not _value_has_time(upper[1]) and end > start:
        inclusive_end_date = (end - timedelta(days=1)).date()
    elif end_exclusive and _value_has_time(upper[1]) and end > start:
        inclusive_end_date = (end - timedelta(microseconds=1)).date()
    comparison_start = start
    comparison_end = end
    if precision == "datetime" and not _value_has_time(upper[1]) and not end_exclusive:
        comparison_end = end + timedelta(days=1) - timedelta(microseconds=1)
    return {
        "start_date": start.date().isoformat(),
        "end_date": inclusive_end_date.isoformat(),
        "comparison_precision": precision,
        "comparison_start": comparison_start.isoformat(sep=" "),
        "comparison_end": comparison_end.isoformat(sep=" "),
        "comparison_start_operator": lower[0],
        "comparison_end_operator": upper[0],
        "comparison_field": str(bounds.get("field") or ""),
        "basis": str(bounds.get("basis") or "unknown"),
    }


def fixed_time_window(
    start_date: str,
    end_date: str,
    config: dict[str, Any] | None,
    *,
    as_of_date: str | date | None = None,
    basis: str = "fixed_literals",
    dynamic: bool = False,
) -> dict[str, Any]:
    integrity = project_time_integrity_policy(config)
    timezone_offset = str(
        integrity.get("timezone_offset")
        or (config or {}).get("default_query_window", {}).get("timezone_offset")
        or "+08:00"
    )
    try:
        local_tz = parse_timezone_offset(timezone_offset)
    except ValueError:
        local_tz = timezone(timedelta(hours=8))
    today = _coerce_date(as_of_date) if as_of_date else datetime.now(local_tz).date()
    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    return {
        "start_date": start.isoformat() if start else str(start_date or ""),
        "end_date": end.isoformat() if end else str(end_date or ""),
        "comparison_precision": "date" if start and end else "unknown",
        "comparison_start": start.isoformat() if start else "",
        "comparison_end": end.isoformat() if end else "",
        "comparison_start_operator": ">=",
        "comparison_end_operator": "<=",
        "comparison_field": "",
        "basis": basis,
        "dynamic": bool(dynamic),
        "today_included": bool(start and end and today and start <= today <= end),
        "as_of_date": today.isoformat() if today else "",
        "timezone_offset": timezone_offset,
    }


def requested_time_window(
    sql: str,
    config: dict[str, Any] | None,
    *,
    as_of_date: str | date | None = None,
) -> dict[str, Any]:
    """Resolve the SQL's fixed date window and whether it contains today.

    Dashboard placeholders are reported as runtime-dynamic instead of being
    guessed. This fact is shared by generation gates and result evidence.
    """

    policy = project_time_policy(config)
    integrity = project_time_integrity_policy(config)
    timezone_offset = str(
        integrity.get("timezone_offset")
        or (config or {}).get("default_query_window", {}).get("timezone_offset")
        or "+08:00"
    )
    try:
        local_tz = parse_timezone_offset(timezone_offset)
    except ValueError:
        local_tz = timezone(timedelta(hours=8))
    if isinstance(as_of_date, date) and not isinstance(as_of_date, datetime):
        today = as_of_date
    elif as_of_date:
        today = _coerce_date(as_of_date)
    else:
        today = datetime.now(local_tz).date()

    dynamic = bool(
        re.search(
            r"\$\{(?:start_date|end_date|start_time|end_time)\}|\{\{(?:START_DATE|END_DATE)\}\}",
            sql or "",
            flags=re.I,
        )
    )
    resolved = _normalized_requested_bounds(requested_window_bounds(sql, config))
    dates = whole_day_dates(sql, config) if not resolved else None
    if resolved:
        start_date = str(resolved.get("start_date") or "")
        end_date = str(resolved.get("end_date") or "")
        basis = str(resolved.get("basis") or "unknown")
    elif dates:
        start_date, end_date = dates
        basis = "params_or_literals"
        resolved = {
            "comparison_precision": "date",
            "comparison_start": start_date,
            "comparison_end": end_date,
            "comparison_start_operator": ">=",
            "comparison_end_operator": "<=",
            "comparison_field": str(policy.get("partition_field") or ""),
        }
    else:
        start_date = ""
        end_date = ""
        basis = "unknown"
    if dynamic and not start_date and not end_date:
        basis = "dashboard_dynamic"
    if start_date and end_date and today:
        start = _coerce_date(start_date)
        end = _coerce_date(end_date)
        today_included: bool | None = bool(start and end and start <= today <= end)
    else:
        today_included = None if dynamic else False
    return {
        "start_date": start_date,
        "end_date": end_date,
        "comparison_precision": str(resolved.get("comparison_precision") or "unknown"),
        "comparison_start": str(resolved.get("comparison_start") or ""),
        "comparison_end": str(resolved.get("comparison_end") or ""),
        "comparison_start_operator": str(resolved.get("comparison_start_operator") or ""),
        "comparison_end_operator": str(resolved.get("comparison_end_operator") or ""),
        "comparison_field": str(resolved.get("comparison_field") or ""),
        "basis": basis,
        "dynamic": dynamic,
        "today_included": today_included,
        "as_of_date": today.isoformat() if today else "",
        "timezone_offset": timezone_offset,
    }


def sql_uses_field(sql: str, field: str) -> bool:
    field = str(field or "").strip().strip("`")
    if not field:
        return False
    cleaned = re.sub(r"'(?:''|[^'])*'", " ", strip_sql_comments(sql))
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_])`?{re.escape(field)}`?(?![A-Za-z0-9_])",
            cleaned,
            flags=re.I,
        )
    )


def output_field_key(value: Any) -> str:
    return re.sub(r"[\s_`\-]+", "", str(value or "")).casefold()


def actual_range_role(field: str) -> str:
    key = output_field_key(field)
    if key in ACTUAL_RANGE_START_KEYS:
        return "start"
    if key in ACTUAL_RANGE_END_KEYS:
        return "end"
    return ""


def is_temporal_output_name(field: str, known_fields: list[str] | set[str] | None = None) -> bool:
    key = output_field_key(field)
    known_keys = {output_field_key(item) for item in (known_fields or []) if str(item or "").strip()}
    return bool(
        actual_range_role(field)
        or key in known_keys
        or (
            TEMPORAL_OUTPUT_NAME_RE.search(str(field or ""))
            and not TEMPORAL_OUTPUT_EXCLUDE_RE.search(str(field or ""))
        )
    )


def _constant_or_requested_range_expression(expression: str) -> str:
    cleaned = strip_sql_comments(expression).strip()
    if REQUESTED_RANGE_EXPRESSION_RE.search(cleaned):
        return "requested_or_execution_time"
    if DATE_LITERAL_RE.search(cleaned):
        return "fixed_literal"
    if (
        re.fullmatch(r"'[^']+'", cleaned)
        or re.fullmatch(
            r"(?:date|datetime|timestamp)\s*\(\s*'[^']+'\s*\)",
            cleaned,
            flags=re.I,
        )
        or re.fullmatch(
            r"cast\s*\(\s*'[^']+'\s+as\s+[A-Za-z_][\w]*(?:\s*\([^)]*\))?\s*\)",
            cleaned,
            flags=re.I,
        )
    ):
        return "fixed_literal"
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned):
        return "fixed_literal"
    return ""


def actual_range_output_contract(
    sql: str,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Check that a today-capable result can report observed, not requested, time."""

    try:
        from sql_facts import final_select_projection, parse_select_expression
    except ImportError:  # pragma: no cover - shared runtime layout guard
        return {
            "status": "unresolved",
            "basis": "final_select_parser_unavailable",
            "fields": [],
            "rejected_fields": [],
        }
    projection = final_select_projection(sql)
    if not projection:
        return {
            "status": "missing",
            "basis": "final_select_unresolved",
            "fields": [],
            "rejected_fields": [],
        }
    policy = project_time_integrity_policy(config)
    known_keys = {
        output_field_key(policy.get("date_field")),
        output_field_key(policy.get("time_field")),
        output_field_key(project_time_policy(config).get("partition_field")),
        output_field_key(project_time_policy(config).get("business_time_field")),
    }
    known_keys.discard("")
    accepted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    for raw_expression in projection.expressions:
        parsed = parse_select_expression(raw_expression)
        alias = str(parsed.get("alias") or "").strip()
        expression = str(parsed.get("expression") or "").strip()
        role = actual_range_role(alias)
        temporal_name = is_temporal_output_name(alias, known_keys)
        if not temporal_name:
            continue
        rejection = _constant_or_requested_range_expression(expression)
        row = {"field": alias, "role": role or "temporal", "expression": expression}
        if rejection:
            rejected.append({**row, "reason": rejection})
        else:
            accepted.append(row)
    range_roles = {item["role"] for item in accepted if item["role"] in {"start", "end"}}
    temporal_fields = [item for item in accepted if item["role"] == "temporal"]
    observable = bool(temporal_fields or range_roles == {"start", "end"})
    return {
        "status": "observable" if observable else "missing",
        "basis": (
            "explicit_range_fields"
            if range_roles == {"start", "end"}
            else "result_temporal_field"
            if temporal_fields
            else "no_observed_time_projection"
        ),
        "fields": accepted,
        "rejected_fields": rejected,
    }


def tlog_source_aliases(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    rows: list[str] = []
    for match in re.finditer(
        r"\b(?:from|join)\s+`?([A-Za-z_][\w$]*)`?\s*\.\s*`?([A-Za-z_][\w$]*)`?(?:\s+(?:as\s+)?`?([A-Za-z_][\w$]*)`?)?",
        cleaned,
        flags=re.I,
    ):
        database, table, alias = match.groups()
        full = f"{database}.{table}"
        if not re.search(r"(?:_dsl_|_fht0\b|tdbank)", full, flags=re.I):
            continue
        if not alias or alias.casefold() in SQL_ALIAS_RESERVED_WORDS:
            alias = f"__unqualified__:{table}"
        if alias.casefold() not in {item.casefold() for item in rows}:
            rows.append(alias)
    return rows


def time_integrity_match_expression(alias: str, config: dict[str, Any] | None) -> str:
    policy = project_time_integrity_policy(config)
    date_field = str(policy.get("date_field") or "").strip().strip("`")
    time_field = str(policy.get("time_field") or "").strip().strip("`")
    if not date_field or not time_field:
        raise ValueError("Time-integrity matching requires configured date_field and time_field.")
    date_ref = f"{alias}.{date_field}"
    time_ref = f"{alias}.{time_field}"
    return (
        f"{time_ref} IS NOT NULL\n"
        f"      AND {date_ref} IS NOT NULL\n"
        f"      AND CAST({time_ref} AS DATE) = CAST({date_ref} AS DATE)"
    )


def time_integrity_predicate_present(
    sql: str,
    alias: str,
    config: dict[str, Any] | None,
) -> bool:
    policy = project_time_integrity_policy(config)
    date_field = str(policy.get("date_field") or "").strip().strip("`")
    time_field = str(policy.get("time_field") or "").strip().strip("`")
    if not date_field or not time_field:
        return False
    cleaned = strip_sql_comments(sql)
    if re.search(
        rf"\{{\{{\s*TLOG_TIME_INTEGRITY_FILTER\s*:\s*{re.escape(alias)}\s*\}}\}}",
        cleaned,
        flags=re.I,
    ):
        return True
    unqualified = str(alias).casefold().startswith("__unqualified__:")
    alias_ref = "" if unqualified else rf"{re.escape(alias)}\s*\.\s*"
    time_ref = rf"{alias_ref}`?{re.escape(time_field)}`?"
    date_ref = rf"{alias_ref}`?{re.escape(date_field)}`?"
    pair = rf"(?:cast\s*\(\s*{time_ref}\s+as\s+date\s*\)|date\s*\(\s*{time_ref}\s*\))\s*=\s*(?:cast\s*\(\s*{date_ref}\s+as\s+date\s*\)|date\s*\(\s*{date_ref}\s*\))"
    reverse = rf"(?:cast\s*\(\s*{date_ref}\s+as\s+date\s*\)|date\s*\(\s*{date_ref}\s*\))\s*=\s*(?:cast\s*\(\s*{time_ref}\s+as\s+date\s*\)|date\s*\(\s*{time_ref}\s*\))"
    tokens = _word_tokens_with_depth(cleaned)
    for match in re.finditer(rf"(?:{pair}|{reverse})", cleaned, flags=re.I | re.S):
        clause = _query_clause_at(
            tokens,
            match.start(),
            _depth_at_position(cleaned, match.start()),
        )
        if clause == "where":
            return True
    return False


def time_integrity_plan(
    sql: str,
    config: dict[str, Any] | None,
    *,
    as_of_date: str | date | None = None,
    portable_aliases: list[str] | None = None,
    window_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = project_time_integrity_policy(config)
    window = (
        dict(window_override)
        if isinstance(window_override, dict)
        else requested_time_window(sql, config, as_of_date=as_of_date)
    )
    mode = str(policy.get("mode") or "report_only")
    today_or_dynamic = bool(window.get("today_included") is True or window.get("dynamic"))
    uses_time = sql_uses_field(sql, str(policy.get("time_field") or ""))
    has_tlog = bool(is_tlog_sql(sql) or portable_aliases)
    if mode == "always":
        apply_match = True
    elif mode == "required_when_event_time_or_today":
        apply_match = uses_time or today_or_dynamic
    elif mode == "required_when_today":
        apply_match = today_or_dynamic
    else:
        apply_match = False
    aliases: list[str] = []
    for alias in portable_aliases or tlog_source_aliases(sql):
        cleaned_alias = str(alias or "").strip()
        if cleaned_alias and cleaned_alias.casefold() not in {
            item.casefold() for item in aliases
        }:
            aliases.append(cleaned_alias)
    actual_range_required = bool(
        window.get("today_included") is True or window.get("dynamic")
    )
    range_output = (
        actual_range_output_contract(sql, config)
        if actual_range_required
        else {
            "status": "not_required",
            "basis": "fixed_historical_window",
            "fields": [],
            "rejected_fields": [],
        }
    )
    return {
        "contract_version": TIME_INTEGRITY_POLICY_VERSION,
        "policy_fingerprint": time_integrity_policy_fingerprint(config),
        "mode": mode,
        "policy": {
            "calendar": policy.get("calendar"),
            "date_field": str(policy.get("date_field") or ""),
            "time_field": str(policy.get("time_field") or ""),
            "date_match": policy.get("date_match"),
            "mismatch_action": policy.get("mismatch_action"),
        },
        "window": window,
        "uses_time_field": uses_time,
        "apply_match_filter": bool(has_tlog and apply_match and mode not in {"disabled", "report_only"}),
        "actual_range_required": actual_range_required,
        "actual_range_runtime_conditional": bool(window.get("dynamic")),
        "actual_range_output": range_output,
        "source_aliases": aliases,
        "filter_expression": "same_local_date" if apply_match else "",
    }


def analyze_time_integrity_contract(
    sql: str,
    config: dict[str, Any] | None,
    *,
    as_of_date: str | date | None = None,
) -> dict[str, Any]:
    policy = project_time_integrity_policy(config)
    plan = time_integrity_plan(sql, config, as_of_date=as_of_date)
    mode = str(policy.get("mode") or "report_only")
    aliases = plan.get("source_aliases") or []
    findings: list[dict[str, str]] = []

    def add(code: str, message: str, severity: str = "blocker") -> None:
        if code not in {item.get("code") for item in findings}:
            findings.append({"code": code, "severity": severity, "message": message})

    config_problems = time_integrity_config_problems(config)
    for problem in config_problems:
        add("invalid_time_integrity_policy", problem)
    matching_required = bool(plan.get("apply_match_filter")) and mode not in {"optional", "disabled", "report_only"}
    if matching_required and not aliases:
        add("time_integrity_source_alias_unresolved", "已启用时间一致性检查，但没有识别到可绑定的 TLOG 源别名。")
    if matching_required and len(aliases) > 1 and any(
        str(alias).casefold().startswith("__unqualified__:") for alias in aliases
    ):
        add(
            "time_integrity_source_alias_unresolved",
            "多个 TLOG 源中存在未命名别名；必须为每个源提供显式别名，才能逐源匹配时间字段。",
        )
    if matching_required and (not policy.get("date_field") or not policy.get("time_field")):
        add("time_integrity_fields_missing", "已启用时间一致性检查，但项目未配置成对的日期字段和时间字段。")
    output_contract = plan.get("actual_range_output") or {}
    if plan.get("actual_range_required") and output_contract.get("status") != "observable":
        add(
            "missing_actual_time_range_output",
            "查询日期范围包含今日或运行时可能包含今日；最终输出必须保留一个来自已过滤结果的日期/时间字段，"
            "或同时输出 `实际数据开始时间` 与 `实际数据结束时间`。固定 params、CURRENT_TIMESTAMP 和查询执行时间不能冒充实际数据范围。",
        )
    missing_aliases = [alias for alias in aliases if not time_integrity_predicate_present(sql, alias, config)]
    if matching_required and missing_aliases:
        display_aliases = [
            str(alias).split(":", 1)[1]
            if str(alias).startswith("__unqualified__:")
            else str(alias)
            for alias in missing_aliases
        ]
        add(
            "missing_time_integrity_filter",
            f"TLOG 源 `{', '.join(display_aliases)}` 缺少 {policy.get('time_field')} 与 "
            f"{policy.get('date_field')} 同自然日匹配过滤；异常时间记录必须排除。",
        )
    elif mode == "optional" and aliases and missing_aliases:
        add(
            "optional_time_integrity_filter_not_applied",
            "项目将时间一致性检查配置为可选；当前 SQL 未声明日期/时间同日匹配，结果范围仍需按实际输出字段核对。",
            "warning",
        )
    return {
        "contract_version": TIME_INTEGRITY_POLICY_VERSION,
        "status": "block" if any(item["severity"] == "blocker" for item in findings) else "pass",
        "findings": findings,
        "plan": plan,
        "facts": {
            "mode": mode,
            "date_field": str(policy.get("date_field") or ""),
            "time_field": str(policy.get("time_field") or ""),
            "source_aliases": aliases,
            "missing_filter_aliases": missing_aliases,
            "today_included": plan.get("window", {}).get("today_included"),
            "actual_range_required": plan.get("actual_range_required", False),
            "actual_range_output": output_contract,
        },
    }


def project_time_policy(config: dict[str, Any] | None) -> dict[str, Any]:
    policy = (config or {}).get("partition_policy")
    policy = dict(policy) if isinstance(policy, dict) else {}
    conditional_detail = str(policy.get("business_time_required_when") or "") == "detailed_time_logic"
    if conditional_detail:
        policy.setdefault("partition_bounds", "inclusive")
        policy.setdefault("whole_day_filter_mode", "partition_only")
        policy.setdefault("detail_time_bounds", "inclusive")
    return policy


def _word_tokens_with_depth(sql: str) -> list[tuple[str, int, int]]:
    tokens: list[tuple[str, int, int]] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(sql):
        char = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote:
            if char == quote:
                if quote == "'" and nxt == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            i += 1
            continue
        if char == "(":
            depth += 1
            i += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if char.isalpha() or char == "_":
            start = i
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] in {"_", "$"}):
                i += 1
            tokens.append((sql[start:i].lower(), start, depth))
            continue
        i += 1
    return tokens


def _depth_at_position(sql: str, position: int) -> int:
    depth = 0
    quote: str | None = None
    i = 0
    while i < min(position, len(sql)):
        char = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote:
            if char == quote:
                if quote == "'" and nxt == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        i += 1
    return depth


def _query_clause_at(tokens: list[tuple[str, int, int]], position: int, current_depth: int) -> str:
    prior = [item for item in tokens if item[1] < position]
    selects = [item for item in prior if item[0] == "select" and item[2] <= current_depth]
    if not selects:
        return ""
    select = selects[-1]
    query_depth = select[2]
    clause = "select"
    for word, token_position, depth in prior:
        if depth != query_depth or token_position < select[1]:
            continue
        if word in {"select", "from", "where", "having", "limit", "qualify", "union", "on", "join"}:
            clause = word
        elif word in {"group", "order"}:
            clause = word
    return clause


def field_operators(sql: str, field: str) -> list[str]:
    if not field:
        return []
    cleaned = strip_sql_comments(sql)
    tokens = _word_tokens_with_depth(cleaned)
    escaped = re.escape(field)
    rows: list[str] = []
    for match in re.finditer(
        rf"\b(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*(>=|<=|>|<|=|between\b)",
        cleaned,
        flags=re.I,
    ):
        if _query_clause_at(tokens, match.start(), _depth_at_position(cleaned, match.start())) == "where":
            rows.append(match.group(1).lower())
    return rows


def field_has_param(sql: str, field: str, alias: str) -> bool:
    cleaned = strip_sql_comments(sql)
    escaped_field = re.escape(field)
    escaped_alias = re.escape(alias)
    patterns = [
        rf"\b(?:[A-Za-z_][\w$]*\.)?`?{escaped_field}`?\s*(?:>=|<=|>|<|=)\s*(?:[A-Za-z_][\w$]*\.)?`?{escaped_alias}`?\b",
        rf"\b(?:[A-Za-z_][\w$]*\.)?`?{escaped_field}`?\s*(?:>=|<=|>|<|=)\s*\(\s*select\s+`?{escaped_alias}`?\s+from\s+params\s*\)",
    ]
    return any(re.search(pattern, cleaned, flags=re.I) for pattern in patterns)


def field_literal_bounds(sql: str, field: str) -> dict[str, tuple[str, str]]:
    if not field:
        return {}
    cleaned = strip_sql_comments(sql)
    escaped = re.escape(field)
    rows: dict[str, tuple[str, str]] = {}
    for match in re.finditer(
        rf"\b(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*(>=|<=|>|<)\s*'(\d{{4}}-\d{{2}}-\d{{2}}(?:[ T]\d{{2}}:\d{{2}}(?::\d{{2}})?)?|\d{{8}})'",
        cleaned,
        flags=re.I,
    ):
        operator = match.group(1)
        key = "lower" if operator in {">=", ">"} else "upper"
        rows[key] = (operator, match.group(2))
    return rows


def _param_bound(sql: str, field: str, aliases: tuple[str, ...], side: str) -> tuple[str, str] | None:
    expressions = params_cte_expressions(sql)
    operators = field_operators(sql, field)
    expected_ops = {">=", ">"} if side == "lower" else {"<=", "<"}
    for alias in aliases:
        if alias not in expressions or not field_has_param(sql, field, alias):
            continue
        value = literal_value(expressions[alias])
        operator = next((item for item in operators if item in expected_ops), "")
        if value:
            return operator, value
    return None


def resolved_bounds(sql: str, field: str, lower_aliases: tuple[str, ...], upper_aliases: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    rows = field_literal_bounds(sql, field)
    lower = _param_bound(sql, field, lower_aliases, "lower")
    upper = _param_bound(sql, field, upper_aliases, "upper")
    if lower:
        rows["lower"] = lower
    if upper:
        rows["upper"] = upper
    return rows


def _is_midnight(value: str) -> bool:
    parsed = parse_datetime(value)
    return bool(parsed and parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0)


def _is_day_end(value: str) -> bool:
    parsed = parse_datetime(value)
    return bool(parsed and parsed.hour == 23 and parsed.minute == 59 and parsed.second == 59)


def whole_day_dates(sql: str, config: dict[str, Any] | None) -> tuple[str, str] | None:
    policy = project_time_policy(config)
    partition_field = str(policy.get("partition_field") or "")
    detail_field = str(policy.get("business_time_field") or "")
    partition = resolved_bounds(sql, partition_field, ("pt_start",), ("pt_end",))
    detail = resolved_bounds(sql, detail_field, ("ts_start",), ("ts_end",))
    lower = partition.get("lower") or detail.get("lower")
    upper = partition.get("upper") or detail.get("upper")
    if not lower or not upper:
        return None
    start = parse_datetime(lower[1])
    end = parse_datetime(upper[1])
    if not start or not end:
        return None
    upper_op = upper[0]
    if upper_op == "<" and _is_midnight(upper[1]) and end > start:
        end -= timedelta(days=1)
    elif upper_op == "<" and end.date() > start.date():
        end -= timedelta(days=1)
    detail_present = bool(field_operators(sql, detail_field))
    if detail_present:
        detail_lower = detail.get("lower")
        detail_upper = detail.get("upper")
        if not detail_lower or not detail_upper or not _is_midnight(detail_lower[1]):
            return None
        detail_end = parse_datetime(detail_upper[1])
        if not detail_end:
            return None
        if detail_upper[0] == "<" and not _is_midnight(detail_upper[1]):
            return None
        if detail_upper[0] == "<=" and not (_is_day_end(detail_upper[1]) or " " not in detail_upper[1]):
            return None
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def analyze_time_contract(
    sql: str,
    config: dict[str, Any] | None,
    *,
    declared_mode: str = "",
    as_of_date: str | date | None = None,
) -> dict[str, Any]:
    policy = project_time_policy(config)
    partition_field = str(policy.get("partition_field") or "")
    detail_field = str(policy.get("business_time_field") or "")
    time_integrity = analyze_time_integrity_contract(
        sql,
        config,
        as_of_date=as_of_date,
    )
    applicable = bool(is_tlog_sql(sql) and policy.get("required_for_tlog") is True and partition_field)
    if not applicable:
        integrity_blockers = [
            item
            for item in time_integrity.get("findings", [])
            if item.get("severity") == "blocker"
        ]
        return {
            "contract_version": TIME_CONTRACT_VERSION,
            "status": "block" if integrity_blockers else "not_applicable",
            "mode": "not_applicable",
            "findings": integrity_blockers,
            "warnings": [
                item.get("message", "")
                for item in time_integrity.get("findings", [])
                if item.get("severity") == "warning"
            ],
            "time_integrity": time_integrity,
            "facts": {
                "has_tlog": is_tlog_sql(sql),
                "partition_field": partition_field,
                "detail_time_field": detail_field,
                "time_integrity": time_integrity.get("facts", {}),
            },
        }

    partition_ops = field_operators(sql, partition_field)
    detail_ops = field_operators(sql, detail_field)
    aliases = set(params_cte_expressions(sql))
    whole_dates = whole_day_dates(sql, config)
    mode = declared_mode if declared_mode in {"whole_day", "detailed_time"} else ("whole_day" if whole_dates or not detail_ops else "detailed_time")
    findings: list[dict[str, str]] = []

    def add(code: str, message: str) -> None:
        if code not in {item["code"] for item in findings}:
            findings.append({"code": code, "severity": "blocker", "message": message})

    if not any(item in {">=", ">", "between"} for item in partition_ops):
        add("missing_partition_lower_bound", f"TLOG 必须使用 {partition_field} 开始边界。")
    if not any(item in {"<=", "<", "between"} for item in partition_ops):
        add("missing_partition_upper_bound", f"TLOG 必须使用 {partition_field} 结束边界。")
    if str(policy.get("partition_bounds") or "") == "inclusive":
        if ">=" not in partition_ops:
            add("partition_lower_not_inclusive", f"{partition_field} 开始边界必须使用 >=。")
        if "<=" not in partition_ops:
            add("partition_upper_not_inclusive", f"{partition_field} 结束边界必须使用 <=，并使用实际结束日。")

    expressions = params_cte_expressions(sql)
    if mode == "whole_day" and str(policy.get("whole_day_filter_mode") or "") == "partition_only":
        if detail_ops or aliases & {"ts_start", "ts_end"}:
            add(
                "redundant_detail_time_filter_for_whole_day",
                f"完整自然日查询只使用 {partition_field}；不得额外定义 ts_start/ts_end 或增加 {detail_field} WHERE 范围。",
            )
        for alias in ("pt_start", "pt_end"):
            value = literal_value(expressions.get(alias, ""))
            if value and (" " in value or "T" in value):
                add("partition_param_not_date_only", f"{alias} 必须是日期值，不应携带 00:00:00。")

    if detail_ops and str(policy.get("detail_time_bounds") or "") == "inclusive":
        if ">=" not in detail_ops:
            add("detail_time_lower_not_inclusive", f"{detail_field} 范围开始边界必须使用 >=。")
        if "<=" not in detail_ops:
            add("detail_time_upper_not_inclusive", f"{detail_field} 范围结束边界必须使用 <=。")
    ts_end = literal_value(expressions.get("ts_end", ""))
    if ts_end and _is_midnight(ts_end) and "<" in detail_ops:
        add("forbidden_next_day_exclusive_end", "不得使用次日 00:00:00 配合 < 作为自然日结束边界。")

    for finding in time_integrity.get("findings", []) or []:
        if finding.get("severity") != "blocker":
            continue
        add(
            str(finding.get("code") or "time_integrity"),
            str(finding.get("message") or "时间一致性契约不满足。"),
        )

    return {
        "contract_version": TIME_CONTRACT_VERSION,
        "status": "block" if findings else "pass",
        "mode": mode,
        "findings": findings,
        "warnings": [
            item.get("message", "")
            for item in time_integrity.get("findings", [])
            if item.get("severity") == "warning"
        ],
        "time_integrity": time_integrity,
        "facts": {
            "has_tlog": True,
            "partition_field": partition_field,
            "detail_time_field": detail_field,
            "partition_operators": partition_ops,
            "detail_time_operators": detail_ops,
            "params_aliases": sorted(aliases),
            "whole_day_dates": list(whole_dates) if whole_dates else [],
            "time_integrity": time_integrity.get("facts", {}),
        },
    }


def time_contract_problem_messages(sql: str, config: dict[str, Any] | None, *, declared_mode: str = "") -> list[str]:
    return [item["message"] for item in analyze_time_contract(sql, config, declared_mode=declared_mode).get("findings", [])]
