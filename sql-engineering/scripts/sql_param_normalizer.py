#!/usr/bin/env python3
"""Normalize directly runnable query SQL into the retained params-CTE shape."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sql_time_contract import (
    MANAGED_TIME_ALIASES,
    params_cte_items,
    parse_datetime,
    project_time_policy,
    resolved_bounds,
    whole_day_dates,
)


DATE_LITERAL_RE = re.compile(r"'(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?|\d{8}(?:\d{2})?)'")
TIME_FIELDS = ["dtEventTime", "EventTime", "LogTime"]


@dataclass
class NormalizedSql:
    sql: str
    changed: bool
    params: dict[str, Any]
    warnings: list[str]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        rows.append(text)
    return rows


def _split_leading_comments(sql: str) -> tuple[str, str]:
    """Return leading whitespace/comments and the executable SQL body."""

    pos = 0
    length = len(sql)
    while pos < length:
        ws_match = re.match(r"\s+", sql[pos:])
        if ws_match:
            pos += ws_match.end()
            continue
        if sql.startswith("--", pos):
            line_end = sql.find("\n", pos)
            if line_end == -1:
                return sql, ""
            pos = line_end + 1
            continue
        if sql.startswith("/*", pos):
            block_end = sql.find("*/", pos + 2)
            if block_end == -1:
                return sql, ""
            pos = block_end + 2
            continue
        break
    return sql[:pos], sql[pos:]


def _has_top_params(sql: str) -> bool:
    _, body = _split_leading_comments(sql)
    return bool(re.match(r"\s*with\s+params\s+as\s*\(", body, flags=re.I))


def _find_matching_paren(sql: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    i = open_index
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote:
            if quote == "]" and ch == "]":
                quote = None
            elif quote != "]" and ch == quote:
                if quote == "'" and nxt == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and nxt == "-":
            newline = sql.find("\n", i + 2)
            i = len(sql) if newline == -1 else newline + 1
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = len(sql) if end == -1 else end + 2
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            continue
        if ch == "[":
            quote = "]"
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _top_params_span(sql: str) -> tuple[int, int, int, int] | None:
    prefix, body = _split_leading_comments(sql)
    match = re.match(r"\s*with\s+params\s+as\s*\(", body, flags=re.I)
    if not match:
        return None
    body_offset = len(prefix)
    open_index = body_offset + match.end() - 1
    close_index = _find_matching_paren(sql, open_index)
    if close_index == -1:
        return None
    return body_offset, open_index, open_index + 1, close_index


def _existing_param_aliases(sql: str) -> set[str]:
    span = _top_params_span(sql)
    if not span:
        return set()
    _, _, body_start, body_end = span
    body = sql[body_start:body_end]
    aliases = set()
    for match in re.finditer(
        r"\bas\s+(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)'|\[([^\]]+)\]|([A-Za-z_][\w$]*))",
        body,
        flags=re.I,
    ):
        alias = next((group for group in match.groups() if group), "")
        if alias:
            aliases.add(alias.lower())
    return aliases


def _param_rows_for_missing_aliases(params: dict[str, Any], aliases: set[str]) -> list[str]:
    rows: list[str] = []
    if params.get("ts_start") and "ts_start" not in aliases:
        rows.append(f"        '{params['ts_start']}' AS ts_start")
    if params.get("ts_end") and "ts_end" not in aliases:
        rows.append(f"        '{params['ts_end']}' AS ts_end")
    if params.get("pt_start") and "pt_start" not in aliases:
        rows.append(f"        '{params['pt_start']}' AS pt_start")
    if params.get("pt_end") and "pt_end" not in aliases:
        rows.append(f"        '{params['pt_end']}' AS pt_end")
    if params.get("zone_id") is not None and "zone_id" not in aliases:
        rows.append(f"        {params['zone_id']} AS zone_id")
    return rows


def _append_missing_params(sql: str, params: dict[str, Any]) -> tuple[str, bool]:
    span = _top_params_span(sql)
    if not span:
        return sql, False
    aliases = _existing_param_aliases(sql)
    rows = _param_rows_for_missing_aliases(params, aliases)
    if not rows:
        return sql, False
    _, _, body_start, body_end = span
    body = sql[body_start:body_end]
    insertion = ("\n" if body.rstrip().endswith(",") else ",\n") + ",\n".join(rows)
    return sql[:body_end] + insertion + sql[body_end:], True


def _date_sort_key(value: str) -> str:
    text = value.replace("T", " ")
    if re.fullmatch(r"\d{8,10}", text):
        return text
    return re.sub(r"\D", "", text).ljust(14, "0")


def _infer_date_bounds(sql: str) -> tuple[str | None, str | None]:
    literals = _unique(DATE_LITERAL_RE.findall(sql))
    if len(literals) < 2:
        return None, None
    ordered = sorted(literals, key=_date_sort_key)
    return ordered[0], ordered[-1]


def _infer_zone(sql: str) -> str | None:
    match = re.search(
        r"\b(?:iZoneAreaID|GameSvrId|game_svr_id|zone_area_id)\b\s*(?:=\s*(\d+|'[^']+')|in\s*\(\s*(\d+|'[^']+')\s*\))",
        sql,
        flags=re.I,
    )
    if match:
        return next(group for group in match.groups() if group).strip("'")
    for item in params_cte_items(sql):
        if str(item.get("alias") or "").lower() != "zone_id":
            continue
        expression = str(item.get("expression") or "").strip()
        numeric = re.fullmatch(r"(?:cast\s*\(\s*)?['\"]?(\d+)['\"]?(?:\s+as\s+\w+\s*\))?", expression, flags=re.I)
        if numeric:
            return numeric.group(1)
    return None


def _infer_partition_bounds(sql: str) -> tuple[str | None, str | None]:
    values = re.findall(r"\btdbank_imp_date\b\s*(?:>=|>|<=|<|=)\s*'?(\d{8,10})'?", sql, flags=re.I)
    values = _unique(values)
    if len(values) < 2:
        return None, None
    ordered = sorted(values)
    return ordered[0], ordered[-1]


def _params_cte(params: dict[str, Any]) -> str:
    rows = []
    if params.get("ts_start"):
        rows.append(f"        '{params['ts_start']}' AS ts_start")
    if params.get("ts_end"):
        rows.append(f"        '{params['ts_end']}' AS ts_end")
    if params.get("pt_start"):
        rows.append(f"        '{params['pt_start']}' AS pt_start")
    if params.get("pt_end"):
        rows.append(f"        '{params['pt_end']}' AS pt_end")
    if params.get("zone_id") is not None:
        rows.append(f"        {params['zone_id']} AS zone_id")
    if not rows:
        rows.append("        '1970-01-01 00:00:00' AS ts_start")
        rows.append("        '1970-01-02 00:00:00' AS ts_end")
    return "params AS (\n    SELECT\n" + ",\n".join(rows) + "\n)"


def _param_row(alias: str, value: Any) -> str:
    if alias == "zone_id":
        return f"        {value} AS zone_id"
    return f"        '{value}' AS {alias}"


def _replace_top_params(sql: str, params: dict[str, Any]) -> str:
    span = _top_params_span(sql)
    if not span:
        return _insert_params_cte(sql, params)
    _, _, body_start, body_end = span
    managed = set(MANAGED_TIME_ALIASES) | ({"zone_id"} if params.get("zone_id") is not None else set())
    rows: list[str] = []
    for item in params_cte_items(sql):
        alias = str(item.get("alias") or "")
        expression = str(item.get("expression") or "").strip()
        if alias.lower() in managed:
            continue
        if alias:
            rows.append(f"        {expression} AS {alias}")
    for alias in ["pt_start", "pt_end", "ts_start", "ts_end", "zone_id"]:
        if params.get(alias) is not None:
            rows.append(_param_row(alias, params[alias]))
    body = "\n    SELECT\n" + ",\n".join(rows) + "\n"
    return sql[:body_start] + body + sql[body_end:]


def _insert_params_cte(sql: str, params: dict[str, Any]) -> str:
    cte = _params_cte(params)
    prefix, body = _split_leading_comments(sql)
    if re.match(r"\s*with\b", body, flags=re.I):
        return prefix + re.sub(r"^\s*with\b", "WITH\n" + cte + ",", body, count=1, flags=re.I)
    return prefix + "WITH\n" + cte + "\n" + body.lstrip()


def _sanitize_existing_params(sql: str) -> tuple[str, bool]:
    replacements = {
        "start_partition": "pt_start",
        "end_partition": "pt_end",
        "`start_partition`": "pt_start",
        "`end_partition`": "pt_end",
    }
    changed = False
    result = sql
    for old, new in replacements.items():
        updated = re.sub(rf"\b{re.escape(old)}\b", new, result)
        if updated != result:
            changed = True
            result = updated
    return result, changed


def _rewrite_time_literals(sql: str, params: dict[str, Any], time_fields: list[str] | None = None) -> str:
    result = sql
    configured_time_fields = _unique((time_fields or []) + TIME_FIELDS)
    if params.get("ts_start"):
        for field in configured_time_fields:
            result = re.sub(
                rf"(\b{re.escape(field)}\b\s*(?:>=|>))\s*'{re.escape(str(params['ts_start']))}'",
                r"\1 (SELECT ts_start FROM params)",
                result,
                flags=re.I,
            )
    if params.get("ts_end"):
        for field in configured_time_fields:
            result = re.sub(
                rf"(\b{re.escape(field)}\b\s*(?:<=|<))\s*'{re.escape(str(params['ts_end']))}'",
                r"\1 (SELECT ts_end FROM params)",
                result,
                flags=re.I,
            )
    if params.get("pt_start"):
        result = re.sub(
            rf"(\btdbank_imp_date\b\s*(?:>=|>))\s*'?{re.escape(str(params['pt_start']))}'?",
            r"\1 (SELECT pt_start FROM params)",
            result,
            flags=re.I,
        )
    if params.get("pt_end"):
        result = re.sub(
            rf"(\btdbank_imp_date\b\s*(?:<=|<))\s*'?{re.escape(str(params['pt_end']))}'?",
            r"\1 (SELECT pt_end FROM params)",
            result,
            flags=re.I,
        )
    return result


def _rewrite_field_bounds(sql: str, field: str, lower_alias: str, upper_alias: str) -> str:
    if not field:
        return sql
    escaped = re.escape(field)
    result = re.sub(
        rf"(\b(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*)(?:>|>=)(\s*)'[^']+'",
        rf"\1>=\2(SELECT {lower_alias} FROM params)",
        sql,
        flags=re.I,
    )
    result = re.sub(
        rf"(\b(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*)(?:<|<=)(\s*)'[^']+'",
        rf"\1<=\2(SELECT {upper_alias} FROM params)",
        result,
        flags=re.I,
    )
    result = re.sub(
        rf"(\b(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*)(?:>=|>)(\s*)(?P<rhs>(?:[A-Za-z_][\w$]*\.)?`?{re.escape(lower_alias)}`?)\b",
        rf"\1>=\2\g<rhs>",
        result,
        flags=re.I,
    )
    result = re.sub(
        rf"(\b(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*)(?:<|<=)(\s*)(?P<rhs>(?:[A-Za-z_][\w$]*\.)?`?{re.escape(upper_alias)}`?)\b",
        rf"\1<=\2\g<rhs>",
        result,
        flags=re.I,
    )
    result = re.sub(
        rf"(\b(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*)(?:>|>=)(\s*)\(\s*select\s+{re.escape(lower_alias)}\s+from\s+params\s*\)",
        rf"\1>=\2(SELECT {lower_alias} FROM params)",
        result,
        flags=re.I,
    )
    return re.sub(
        rf"(\b(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*)(?:<|<=)(\s*)\(\s*select\s+{re.escape(upper_alias)}\s+from\s+params\s*\)",
        rf"\1<=\2(SELECT {upper_alias} FROM params)",
        result,
        flags=re.I,
    )


def _remove_detail_time_where_bounds(sql: str, field: str) -> str:
    if not field:
        return sql
    escaped = re.escape(field)
    predicate = re.compile(
        rf"^(?P<indent>[ \t]*)(?P<keyword>WHERE|AND)\s+"
        rf"(?:[A-Za-z_][\w$]*\.)?`?{escaped}`?\s*(?:>=|>|<=|<)\s*[^\r\n]+(?:\r?\n)?$",
        flags=re.I,
    )
    lines = sql.splitlines(keepends=True)
    promote_from: list[int] = []
    kept: list[str] = []
    for line in lines:
        match = predicate.match(line)
        if not match:
            kept.append(line)
            continue
        if match.group("keyword").lower() == "where":
            promote_from.append(len(kept))
    if promote_from:
        boundary = re.compile(r"^[ \t]*(?:GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|UNION|\)|,)", flags=re.I)
        for start in promote_from:
            for index in range(start, len(kept)):
                line = kept[index]
                if boundary.match(line):
                    break
                match = re.match(r"^(?P<indent>[ \t]*)AND\b", line, flags=re.I)
                if match:
                    kept[index] = line[: match.start()] + match.group("indent") + "WHERE" + line[match.end() :]
                    break
    return "".join(kept)


def _conditional_time_params(sql: str, config: dict[str, Any], zone_id: str | None) -> tuple[str, dict[str, Any], list[str]] | None:
    policy = project_time_policy(config)
    if str(policy.get("whole_day_filter_mode") or "") != "partition_only":
        return None
    partition_field = str(policy.get("partition_field") or "")
    detail_field = str(policy.get("business_time_field") or "")
    warnings: list[str] = []
    whole_dates = whole_day_dates(sql, config)
    if whole_dates:
        params: dict[str, Any] = {"pt_start": whole_dates[0], "pt_end": whole_dates[1]}
        if zone_id is not None:
            params["zone_id"] = zone_id
        rewritten = _replace_top_params(sql, params)
        rewritten = _rewrite_field_bounds(rewritten, partition_field, "pt_start", "pt_end")
        rewritten = _remove_detail_time_where_bounds(rewritten, detail_field)
        return rewritten, params, warnings

    detail = resolved_bounds(sql, detail_field, ("ts_start",), ("ts_end",))
    if not detail.get("lower") or not detail.get("upper"):
        return None
    start_dt = parse_datetime(detail["lower"][1])
    end_dt = parse_datetime(detail["upper"][1])
    if not start_dt or not end_dt:
        return None
    if detail["upper"][0] == "<":
        end_dt -= timedelta(seconds=1)
    params = {
        "pt_start": start_dt.strftime("%Y-%m-%d"),
        "pt_end": end_dt.strftime("%Y-%m-%d"),
        "ts_start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "ts_end": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if zone_id is not None:
        params["zone_id"] = zone_id
    rewritten = _replace_top_params(sql, params)
    rewritten = _rewrite_field_bounds(rewritten, partition_field, "pt_start", "pt_end")
    rewritten = _rewrite_field_bounds(rewritten, detail_field, "ts_start", "ts_end")
    return rewritten, params, warnings


def _rewrite_zone_literals(sql: str, zone_id: str | None) -> str:
    if zone_id is None:
        return sql
    result = re.sub(
        rf"(\b(?:iZoneAreaID|GameSvrId|game_svr_id|zone_area_id)\b\s*=\s*)'?{re.escape(str(zone_id))}'?",
        r"\1(SELECT zone_id FROM params)",
        sql,
        flags=re.I,
    )
    return re.sub(
        rf"(\b(?:iZoneAreaID|GameSvrId|game_svr_id|zone_area_id)\b\s+in\s*\()\s*'?{re.escape(str(zone_id))}'?\s*(\))",
        r"\1SELECT zone_id FROM params\2",
        result,
        flags=re.I,
    )


def normalize_query_sql(sql: str, project_config: dict[str, Any] | None = None) -> NormalizedSql:
    warnings: list[str] = []
    config = project_config or {}
    params: dict[str, Any] = {}
    sql, changed_existing = _sanitize_existing_params(sql)

    zone_id = _infer_zone(sql)
    conditional = _conditional_time_params(sql, config, zone_id)
    if conditional:
        rewritten, params, conditional_warnings = conditional
        rewritten = _rewrite_zone_literals(rewritten, zone_id)
        return NormalizedSql(
            sql=rewritten,
            changed=changed_existing or rewritten != sql,
            params=params,
            warnings=conditional_warnings,
        )

    ts_start, ts_end = _infer_date_bounds(sql)
    pt_start, pt_end = _infer_partition_bounds(sql)
    if ts_start:
        params["ts_start"] = ts_start
    if ts_end:
        params["ts_end"] = ts_end
    policy = config.get("partition_policy") if isinstance(config.get("partition_policy"), dict) else {}
    time_fields = [str(policy.get("business_time_field") or "")]
    if policy.get("required_for_tlog") is True and pt_start and pt_end:
        params["pt_start"] = pt_start
        params["pt_end"] = pt_end
    if zone_id is not None:
        params["zone_id"] = zone_id
    if not ts_start or not ts_end:
        warnings.append("Could not infer complete time bounds; formal QUERY save may still block.")

    if _has_top_params(sql):
        with_missing_params, params_appended = _append_missing_params(sql, params)
        rewritten = _rewrite_time_literals(with_missing_params, params, time_fields)
        rewritten = _rewrite_zone_literals(rewritten, zone_id)
        return NormalizedSql(sql=rewritten, changed=changed_existing or params_appended or rewritten != sql, params=params, warnings=warnings)

    rewritten = _rewrite_time_literals(sql, params, time_fields)
    rewritten = _rewrite_zone_literals(rewritten, zone_id)
    normalized = _insert_params_cte(rewritten, params)
    return NormalizedSql(sql=normalized, changed=True, params=params, warnings=warnings)


def dashboardize_time_params(sql: str, dialect: str) -> str:
    """Convert retained query literal params into DA date variables when obvious."""

    if not _has_top_params(sql):
        return sql
    expression = r"(?:TIMESTAMP\s*\(\s*)?'[^']+'(?:\s*\))?"

    def replace_alias(text: str, alias: str, variable: str) -> str:
        return re.sub(
            rf"{expression}\s+AS\s+`?{re.escape(alias)}`?\b",
            f"'${{{variable}}}' AS {alias}",
            text,
            count=1,
            flags=re.I,
        )

    result = replace_alias(sql, "pt_start", "start_date")
    result = replace_alias(result, "pt_end", "end_date")
    result = replace_alias(result, "ts_start", "start_date")
    result = replace_alias(result, "ts_end", "end_date")
    return result
