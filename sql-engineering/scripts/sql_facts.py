#!/usr/bin/env python3
"""Canonical deterministic SQL facts and content fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from asset_provenance import strip_sql_generation_comment
from subject_identity import (
    analyze_subject_identity,
    finalize_complexity_audit,
    metric_subject_binding,
)


SQL_FACT_SCHEMA_VERSION = "sql_fact_bundle_v3"
FINGERPRINT_VERSION = "sql_fingerprint_v2"
TIME_PARAM_ALIASES = {"pt_start", "pt_end", "ts_start", "ts_end"}
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
METRIC_RE = re.compile(
    r"(玩家数|用户数|人数|次数|数量|合计|总数|占比|比例|比率|率|均值|平均|时长|耗时|cnt|count|num|total|rate|ratio|avg|sum|p\d+)",
    re.I,
)
RATIO_RE = re.compile(r"(占比|比例|比率|转化率|留存率|率|rate|ratio|percent)", re.I)
TIME_FIELD_RE = re.compile(r"\b(dtEventTime|dteventdate|tdbank_imp_date|EventTime|LogTime)\b", re.I)
NEW_USER_ALIASES = ["新增", "新增用户", "新增玩家", "新进", "新进用户", "新进玩家", "新进人数", "注册", "注册用户", "玩家注册", "首登"]


def unique_in_order(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            rows.append(cleaned)
    return rows


def normalize_sql_text(sql: str) -> str:
    lines = str(sql or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = "\n".join(line.rstrip() for line in lines).strip()
    return normalized + "\n" if normalized else ""


def execution_fingerprint(sql: str) -> str:
    return hashlib.sha256(
        normalize_sql_text(strip_sql_generation_comment(sql)).encode("utf-8")
    ).hexdigest()


def strip_sql_comments(sql: str) -> str:
    value = str(sql or "")
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(value):
        char = value[index]
        nxt = value[index + 1] if index + 1 < len(value) else ""
        if quote:
            output.append(char)
            if char == quote:
                if quote != "]" and nxt == quote:
                    output.append(nxt)
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "[":
            quote = "]"
            output.append(char)
            index += 1
            continue
        if char == "-" and nxt == "-":
            newline = value.find("\n", index + 2)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
            continue
        if char == "/" and nxt == "*":
            end = value.find("*/", index + 2)
            output.append(" ")
            index = len(value) if end < 0 else end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def casefold_sql_code(sql: str) -> str:
    """Lower SQL code while preserving quoted literals and identifiers exactly."""
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(sql):
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            output.append(char)
            if char == quote:
                if quote != "]" and nxt == quote:
                    output.append(nxt)
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            output.append(char)
        elif char == "[":
            quote = "]"
            output.append(char)
        else:
            output.append(char.lower())
        index += 1
    return "".join(output)


def compact_sql_code(sql: str) -> str:
    """Collapse code whitespace without changing quoted values."""
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(sql):
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            output.append(char)
            if char == quote:
                if quote != "]" and nxt == quote:
                    output.append(nxt)
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            output.append(char)
        elif char == "[":
            quote = "]"
            output.append(char)
        elif char.isspace():
            if output and output[-1] != " ":
                output.append(" ")
        else:
            output.append(char)
        index += 1
    return "".join(output).strip()


def split_top_level_csv(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = ""
    i = 0
    while i < len(value):
        ch = value[i]
        nxt = value[i + 1] if i + 1 < len(value) else ""
        if quote:
            if ch == quote:
                if quote == "'" and nxt == "'":
                    i += 2
                    continue
                quote = ""
            i += 1
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
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            part = value[start:i].strip()
            if part:
                parts.append(part)
            start = i + 1
        i += 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _keyword_at(sql: str, index: int, keyword: str) -> bool:
    end = index + len(keyword)
    if sql[index:end].lower() != keyword:
        return False
    before = sql[index - 1] if index > 0 else " "
    after = sql[end] if end < len(sql) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _top_level_keyword_positions(sql: str, keyword: str, *, start: int = 0) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    depth = 0
    quote: str | None = None
    i = max(0, start)
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
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and _keyword_at(sql, i, keyword):
            positions.append((i, i + len(keyword)))
            i += len(keyword)
            continue
        i += 1
    return positions


def top_level_keyword_positions(sql: str, keyword: str, *, start: int = 0) -> list[int]:
    return [position for position, _ in _top_level_keyword_positions(sql, keyword, start=start)]


def select_expression_alias(expression: str) -> str:
    expr = expression.strip()
    alias = re.search(
        r"\bas\s+(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)'|\[([^\]]+)\]|([^\s,]+))\s*$",
        expr,
        flags=re.I,
    )
    if alias:
        return next(group for group in alias.groups() if group)
    tail = re.search(
        r"(?:^|\.)(?:`([^`]+)`|\"([^\"]+)\"|\[([^\]]+)\]|([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*))\s*$",
        expr,
    )
    if tail:
        return next(group for group in tail.groups() if group)
    return ""


def parse_select_expression(expression: str) -> dict[str, str]:
    alias = select_expression_alias(expression)
    body = expression.strip()
    if alias:
        match = re.search(
            r"(?is)\s+AS\s+(?:`[^`]+`|\"[^\"]+\"|'[^']+'|\[[^\]]+\]|[^\s,]+)\s*$",
            body,
        )
        if match:
            body = body[: match.start()].strip()
    return {"alias": alias, "expression": body, "text": expression.strip()}


@dataclass
class FinalSelectProjection:
    start: int
    end: int
    expressions: list[str]


def final_select_projection(sql: str) -> FinalSelectProjection | None:
    selects = _top_level_keyword_positions(sql, "select")
    if not selects:
        return None
    _, select_end = selects[-1]
    froms = _top_level_keyword_positions(sql, "from", start=select_end)
    projection_end = froms[0][0] if froms else (sql.find(";", select_end) if ";" in sql[select_end:] else len(sql))
    expressions = [item.strip() for item in split_top_level_csv(sql[select_end:projection_end]) if item.strip()]
    return FinalSelectProjection(start=select_end, end=projection_end, expressions=expressions)


def extract_final_select_list(sql: str) -> str:
    projection = final_select_projection(sql)
    return "" if not projection else sql[projection.start : projection.end].strip()


def final_select_field_aliases(sql: str) -> list[str]:
    projection = final_select_projection(sql)
    if not projection:
        return []
    return unique_in_order(select_expression_alias(expression) for expression in projection.expressions)


def table_references(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    rows: list[str] = []
    for match in re.finditer(r"\b(?:from|join)\s+([`$\{\}\w.]+)", cleaned, flags=re.I):
        table = match.group(1).strip("`")
        if table.lower() not in {"select", "with"}:
            rows.append(table)
    return rows


def extract_tables(sql: str) -> list[str]:
    return unique_in_order(table_references(sql))


def _skip_space(value: str, index: int) -> int:
    while index < len(value) and value[index].isspace():
        index += 1
    return index


def _read_identifier(value: str, index: int) -> tuple[str, int] | None:
    index = _skip_space(value, index)
    if index >= len(value):
        return None
    quote = value[index]
    if quote in {"`", '"', "["}:
        closing = "]" if quote == "[" else quote
        end = value.find(closing, index + 1)
        if end < 0:
            return None
        return value[index + 1 : end], end + 1
    match = re.match(r"[A-Za-z_][\w$]*", value[index:])
    if not match:
        return None
    return match.group(0), index + len(match.group(0))


def extract_cte_names(sql: str) -> list[str]:
    """Return top-level CTE names without treating inner SELECT aliases as CTEs."""
    definitions, _ = _top_level_cte_definitions(sql)
    return unique_in_order(item[0] for item in definitions)


def _top_level_cte_definitions(sql: str) -> tuple[list[tuple[str, str]], str]:
    """Return ordered top-level CTE bodies and the final query body."""
    cleaned = strip_sql_comments(sql)
    match = re.match(r"\s*with\b", cleaned, flags=re.I)
    if not match:
        return [], cleaned
    index = match.end()
    recursive_match = re.match(r"\s*recursive\b", cleaned[index:], flags=re.I)
    if recursive_match:
        index += recursive_match.end()
    definitions: list[tuple[str, str]] = []
    while index < len(cleaned):
        parsed = _read_identifier(cleaned, index)
        if not parsed:
            break
        name, index = parsed
        index = _skip_space(cleaned, index)
        if index < len(cleaned) and cleaned[index] == "(":
            column_end = _matching_paren(cleaned, index)
            if column_end < 0:
                break
            index = _skip_space(cleaned, column_end + 1)
        as_match = re.match(r"as\s*\(", cleaned[index:], flags=re.I)
        if not as_match:
            break
        open_index = index + as_match.end() - 1
        close_index = _matching_paren(cleaned, open_index)
        if close_index < 0:
            break
        definitions.append((name, cleaned[open_index + 1 : close_index]))
        index = _skip_space(cleaned, close_index + 1)
        if index >= len(cleaned) or cleaned[index] != ",":
            break
        index = _skip_space(cleaned, index + 1)
    return definitions, cleaned[index:]


def cte_dependency_structure(sql: str) -> dict[str, Any]:
    """Describe top-level CTE dependencies without claiming executor expansion success."""
    definitions, final_query = _top_level_cte_definitions(sql)
    names = [name for name, _ in definitions]
    canonical = {name.casefold(): name for name in names}

    def local_references(fragment: str) -> list[str]:
        references: list[str] = []
        for relation in table_references(fragment):
            key = relation.strip("`").split(".")[-1].strip("`").casefold()
            if key in canonical:
                references.append(canonical[key])
        return unique_in_order(references)

    dependencies = {
        name: local_references(body)
        for name, body in definitions
    }
    final_references = local_references(final_query)
    memo: dict[str, int] = {}
    visiting: set[str] = set()
    cycles: set[str] = set()

    def dependency_depth(name: str) -> int:
        if name in memo:
            return memo[name]
        if name in visiting:
            cycles.add(name)
            return 0
        visiting.add(name)
        depth = 1 + max(
            (dependency_depth(dependency) for dependency in dependencies.get(name, [])),
            default=0,
        )
        visiting.remove(name)
        memo[name] = depth
        return depth

    for name in names:
        dependency_depth(name)

    definition_positions = {name: position for position, name in enumerate(names, start=1)}
    reference_spans = {
        name: len(names) + 1 - definition_positions[name]
        for name in final_references
    }
    return {
        "cte_dependency_edges": [
            {"cte": name, "depends_on": dependencies[name]}
            for name in names
        ],
        "cte_dependency_edge_count": sum(len(items) for items in dependencies.values()),
        "cte_dependency_depth": max(memo.values(), default=0),
        "final_cte_references": final_references,
        "final_cte_reference_spans": reference_spans,
        "max_final_cte_reference_span": max(reference_spans.values(), default=0),
        "cyclic_cte_references": sorted(cycles),
    }


def physical_source_tables(sql: str) -> list[str]:
    cte_names = {item.lower() for item in extract_cte_names(sql)} | {"params"}
    return [table for table in extract_tables(sql) if table.lower() not in cte_names]


def is_tlog_source_table(table: str) -> bool:
    lowered = table.lower()
    return "_dsl_" in lowered or lowered.endswith("_fht0") or "tdbank" in lowered


def extract_target_tables(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    targets: list[str] = []
    patterns = [
        r"\bcreate\s+(?:external\s+)?table\s+(?:if\s+not\s+exists\s+)?([`$\{\}\w.]+)",
        r"\bcreate\s+(?:or\s+replace\s+)?view\s+([`$\{\}\w.]+)",
        r"\binsert\s+(?:overwrite|into)\s+(?:table\s+)?([`$\{\}\w.]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned, flags=re.I):
            target = match.group(1).strip("`")
            if target.lower() not in {"directory", "local"}:
                targets.append(target)
    return unique_in_order(targets)


def extract_partition_fields(sql: str) -> list[str]:
    fields: list[str] = []
    cleaned = strip_sql_comments(sql)
    for pattern in [r"\bpartitioned\s+by\s*\((.*?)\)", r"\bpartition\s*\((.*?)\)"]:
        for match in re.finditer(pattern, cleaned, flags=re.I | re.S):
            for item in split_top_level_csv(match.group(1)):
                field_match = re.match(r"`?([a-zA-Z_][\w]*)`?", item.strip())
                if field_match:
                    fields.append(field_match.group(1))
    return unique_in_order(fields)


def extract_fields(sql: str) -> tuple[list[str], list[str]]:
    metrics: list[str] = []
    dimensions: list[str] = []
    projection = final_select_projection(sql)
    for expression in projection.expressions if projection else []:
        alias = select_expression_alias(expression)
        if not alias:
            continue
        is_metric = is_metric_expression(expression, alias)
        (metrics if is_metric else dimensions).append(alias)
    return unique_in_order(metrics), unique_in_order(dimensions)


def is_metric_expression(expression: str, alias: str) -> bool:
    text = f"{expression} {alias}".lower()
    if re.search(r"\b(count|sum|avg|min|max|percentile|approx_count_distinct)\s*\(", text):
        return True
    if METRIC_RE.search(alias):
        return True
    return any(
        word in alias.lower()
        for word in ("uv", "pv", "duration", "latency", "amount", "value")
    )


def final_select_exposes_raw_ids(sql: str) -> bool:
    projection = final_select_projection(sql)
    for expression in projection.expressions if projection else []:
        lowered = expression.lower()
        if not re.search(r"\b(?:vopenid|open_id|roleid|role_id|deviceid|device_id)\b", lowered):
            continue
        if re.search(r"\b(?:count|approx_count_distinct)\s*\(", lowered):
            continue
        if re.search(r"\b(?:md5|sha1|sha2|hash)\s*\(", lowered):
            continue
        return True
    return False


def infer_time_grain(sql: str, dimensions: list[str]) -> str:
    text = " ".join(dimensions).lower() + " " + sql.lower()
    if re.search(r"\b(stat_hour|hour|hh)\b", text):
        return "hour"
    if re.search(r"\b(stat_date|dt|date|day|yyyyMMdd|yyyymmdd)\b", text, flags=re.I):
        return "day"
    if "week" in text:
        return "week"
    if "month" in text or "yyyymm" in text:
        return "month"
    return "none"


def infer_business_category(sql: str, tables: list[str], metrics: list[str], dimensions: list[str]) -> str:
    text = " ".join([sql, " ".join(tables), " ".join(metrics), " ".join(dimensions)]).lower()
    categories = [
        ("retention", ["retention", "留存", "return_user", "next_day"]),
        ("funnel", ["funnel", "漏斗", "step_conversion", "conversion_rate", "转化"]),
        ("ab_compare", ["demo_abtest", "abtest", "a/b", "experiment_group", "group_id", "package_group"]),
        ("technical_quality", ["crash", "patchbegin", "shaderwarmup", "loginblocked", "latency", "device_model", "设备型号", "硬件"]),
        ("economy", ["lobbystore", "lobbyresourceflow", "lottery", "commercial", "currency", "shop", "resourceflow", "抽奖", "商城", "货币", "资源流水", "商业化"]),
        ("content_progression", ["battlemission", "mission_id", "missionid", "story_mission", "prologue", "progression", "任务", "关卡", "序章", "剧情"]),
        ("new_user", ["new_user", "newuser", "first_login", "playerregister", *NEW_USER_ALIASES]),
        ("active_user", ["active_user", "dau", "wau", "mau", "login_cnt", "活跃"]),
        ("battle_behavior", ["match", "roommatch", "battle", "gamemode", "战局", "匹配"]),
        ("social", ["friend", "guild", "community", "chat", "好友", "公会", "社区"]),
        ("data_quality", ["reconcile", "diagnostic", "quality_check", "duplicate_check", "对账", "数据校验"]),
        ("privacy_export", ["privacy_export", "export_candidate", "明细导出"]),
    ]
    for category, keywords in categories:
        if any(keyword in text for keyword in keywords):
            return category
    return "uncategorized"


def infer_analysis_type(sql: str, kind: str, metrics: list[str], dimensions: list[str]) -> str:
    text = sql.lower()
    if kind == "VALIDATION" or "@validation_spec" in text:
        return "metric_validation"
    if kind == "DASHBOARD" or "@dashboard_sql_spec" in text:
        if "funnel" in text:
            return "dashboard_funnel"
        if "retention" in text:
            return "dashboard_retention"
        return "dashboard_metric_card" if len(metrics) == 1 and len(dimensions) <= 1 else "dashboard_table"
    has_aggregate = bool(re.search(r"\b(count|sum|avg|min|max)\s*\(", text))
    has_group = bool(re.search(r"\bgroup\s+by\b", text))
    has_limit = bool(re.search(r"\blimit\s+\d+", text))
    if re.search(r"临时排查|排查明细|对账|\b(?:diagnostic|reconcile|anomaly)\b", text):
        return "anomaly_check"
    if has_limit and not has_aggregate:
        return "detail_check" if final_select_exposes_raw_ids(sql) else "sample"
    if has_aggregate or has_group:
        return "aggregate_query"
    return "detail_check"


def infer_grain(dimensions: list[str], analysis_type: str) -> str:
    if dimensions:
        return " x ".join(dimensions)
    return "row-level detail" if analysis_type in {"sample", "detail_check"} else "single aggregate row"


def infer_tags(category: str, analysis_type: str, tables: list[str], metrics: list[str], dimensions: list[str]) -> list[str]:
    table_tags = [Path(table.replace("`", "")).name.lower().split(".")[-1] for table in tables[:5]]
    return unique_in_order([category, analysis_type, *table_tags, *metrics[:5], *dimensions[:5]])


def analyze_sql_text(sql: str, kind: str = "QUERY") -> dict[str, Any]:
    tables = extract_tables(sql)
    source_tables = physical_source_tables(sql)
    target_tables = extract_target_tables(sql)
    partition_fields = extract_partition_fields(sql)
    metrics, dimensions = extract_fields(sql)
    category = infer_business_category(sql, tables, metrics, dimensions)
    analysis_type = infer_analysis_type(sql, kind, metrics, dimensions)
    grain = infer_grain(dimensions, analysis_type)
    time_grain = infer_time_grain(sql, dimensions)
    exposes_raw_ids = final_select_exposes_raw_ids(sql)
    reuse_candidate = bool(metrics and tables and not exposes_raw_ids and analysis_type != "detail_check")
    warnings: list[str] = []
    if not tables:
        warnings.append("No source tables inferred.")
    if not metrics and analysis_type not in {"sample", "detail_check"}:
        warnings.append("No metric aliases inferred from final SELECT.")
    if exposes_raw_ids:
        warnings.append(
            "Identifier fields appear in final output; keep them only when the business result needs them. "
            "DA owns privacy handling, so do not hash or mask them in SQL."
        )
    if not re.search(r"(?is)\bwith\s+params\s+as\s*\(", strip_sql_comments(sql)):
        warnings.append("No top params CTE inferred; retained query reuse should move dates and key filters into params.")
    return {
        "business_category": category,
        "analysis_type": analysis_type,
        "tables": tables,
        "source_tables": source_tables,
        "referenced_tables": tables,
        "cte_names": extract_cte_names(sql),
        "target_tables": target_tables,
        "partition_fields": partition_fields,
        "metrics": metrics,
        "dimensions": dimensions,
        "grain": grain,
        "time_grain": time_grain,
        "tags": infer_tags(category, analysis_type, tables, metrics, dimensions),
        "reuse_candidate": reuse_candidate,
        "reuse_notes": (
            "Candidate for reuse after confirming parameters, business rules, and output grain."
            if reuse_candidate
            else "Review before reuse; SQL may be detail-oriented or missing metric metadata. DA owns any privacy handling."
        ),
        "content_summary": (
            f"{analysis_type} on {category}; targets={','.join(target_tables) or 'none'}; "
            f"tables={','.join(tables) or 'unknown'}; metrics={','.join(metrics) or 'unknown'}; "
            f"dimensions={','.join(dimensions) or 'none'}."
        ),
        "warnings": warnings,
    }


def analyze_sql_file(sql_file: Path, kind: str = "QUERY") -> dict[str, Any]:
    return analyze_sql_text(sql_file.read_text(encoding="utf-8-sig"), kind)


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    quote = ""
    for index in range(open_index, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


SQL_SIDE_PRIVACY_FUNCTION_RE = re.compile(
    r"\b(md5|sha|sha1|sha2|sha256|hash|murmur_hash|xxhash64|crc32|"
    r"to_base64|base64|aes_encrypt|mask|mask_hash|mask_show_first_n|mask_show_last_n)\s*\(",
    flags=re.I,
)


def _sql_code_without_string_literals(sql: str) -> str:
    chars = list(sql)
    quote = ""
    index = 0
    while index < len(chars):
        char = chars[index]
        if quote:
            chars[index] = " "
            if char == quote:
                if index + 1 < len(chars) and chars[index + 1] == quote:
                    chars[index + 1] = " "
                    index += 1
                else:
                    quote = ""
        elif char in {"'", '"'}:
            quote = char
            chars[index] = " "
        index += 1
    return "".join(chars)


def sql_side_privacy_transforms(sql: str) -> list[dict[str, str]]:
    """Return executable-SQL transforms that are forbidden for de-identification."""

    cleaned = strip_sql_comments(sql)
    code = _sql_code_without_string_literals(cleaned)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in SQL_SIDE_PRIVACY_FUNCTION_RE.finditer(code):
        open_index = cleaned.find("(", match.start())
        close_index = _matching_paren(cleaned, open_index) if open_index >= 0 else -1
        expression = cleaned[match.start() : close_index + 1] if close_index >= 0 else match.group(0)
        compact_expression = re.sub(r"\s+", " ", expression).strip()[:300]
        key = (match.group(1).lower(), compact_expression.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "function": match.group(1).lower(),
                "expression": compact_expression,
            }
        )
    return rows


def params_cte_expressions(sql: str) -> dict[str, str]:
    cleaned = strip_sql_comments(sql)
    match = re.search(r"(?is)\bwith\s+params\s+as\s*\(", cleaned)
    if not match:
        return {}
    open_index = cleaned.find("(", match.start())
    close_index = _matching_paren(cleaned, open_index)
    if close_index < 0:
        return {}
    body = cleaned[open_index + 1 : close_index]
    select_match = re.search(r"(?is)\bselect\b(.*)", body)
    if not select_match:
        return {}
    rows: dict[str, str] = {}
    for expression in split_top_level_csv(select_match.group(1)):
        parsed = parse_select_expression(expression)
        alias = parsed["alias"].lower()
        if alias:
            rows[alias] = parsed["expression"].strip()
    return rows


def _is_time_parameter_literal(value: str) -> bool:
    text = value.replace("''", "'").strip()
    return bool(
        re.fullmatch(
            r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
            text,
            flags=re.I,
        )
        or re.fullmatch(r"\d{8}(?:\d{6})?", text)
        or re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?", text)
    )


def _normalize_time_param_expression(expression: str) -> str:
    normalized = re.sub(
        r"'((?:''|[^'])*)'",
        lambda match: "'<TIME_PARAM>'" if _is_time_parameter_literal(match.group(1)) else match.group(0),
        expression,
    )
    return re.sub(r"\b(?:\d{14}|\d{8})\b", "0", normalized)


def normalize_logic_sql(sql: str) -> str:
    cleaned = strip_sql_comments(normalize_sql_text(sql))
    match = re.search(r"(?is)\bwith\s+params\s+as\s*\(", cleaned)
    if match:
        open_index = cleaned.find("(", match.start())
        close_index = _matching_paren(cleaned, open_index)
        if close_index > open_index:
            body = cleaned[open_index + 1 : close_index]
            select_match = re.search(r"(?is)\bselect\b(.*)", body)
            if select_match:
                expressions: list[str] = []
                for expression in split_top_level_csv(select_match.group(1)):
                    parsed = parse_select_expression(expression)
                    alias = parsed["alias"].lower()
                    if alias in TIME_PARAM_ALIASES:
                        normalized_expression = _normalize_time_param_expression(parsed["expression"])
                        expressions.append(f"{normalized_expression} AS {alias}")
                    else:
                        expressions.append(expression.strip())
                normalized_body = "SELECT " + ", ".join(expressions)
                cleaned = cleaned[: open_index + 1] + normalized_body + cleaned[close_index:]
    return casefold_sql_code(compact_sql_code(cleaned))


def logic_fingerprint(sql: str) -> str:
    return hashlib.sha256(normalize_logic_sql(sql).encode("utf-8")).hexdigest()


def load_xml_log_catalog(root: Path | None) -> dict[str, str]:
    if root is None:
        return {}
    path = root / "sources" / "xml_catalog.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows: dict[str, str] = {}
    for item in data.get("logs", []) if isinstance(data, dict) else []:
        name = str(item.get("name") or "").strip()
        if name:
            desc = " ".join(str(item.get("desc") or name).split())[:80]
            rows[name.lower()] = f"{name}【{desc}】"
    return rows


def source_logs(sql: str, root: Path | None = None) -> list[str]:
    catalog = load_xml_log_catalog(root)
    rows: list[str] = []
    for table in physical_source_tables(sql):
        if not is_tlog_source_table(table):
            continue
        match = re.search(r"_dsl_([a-z0-9]+)_fht0", table.lower())
        token = match.group(1) if match else Path(table.lower()).name
        rows.append(catalog.get(token, table.strip("`")))
    return unique_in_order(rows)


def load_external_source_contracts(root: Path | None) -> dict[str, dict[str, Any]]:
    if root is None:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "sources").glob("*.schema.json")) if (root / "sources").exists() else []:
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(contract, dict) or not contract.get("table"):
            continue
        table = str(contract["table"]).strip("`")
        columns = contract.get("columns") if isinstance(contract.get("columns"), list) else []
        date_field = next(
            (
                str(item.get("name") or "")
                for item in columns
                if isinstance(item, dict)
                and re.search(r"cohort|date|time", str(item.get("meaning") or ""), flags=re.I)
            ),
            "",
        )
        rows[table.lower()] = {
            "source_type": str(contract.get("source_type") or "external_physical_table"),
            "table": table,
            "business_role": str(contract.get("description") or "; ".join(contract.get("authoritative_for", []) or [])),
            "availability_status": str(contract.get("availability_status") or "unknown"),
            "date_field": date_field,
            "business_scope": list(contract.get("business_scope") or []),
            "authoritative_for": list(contract.get("authoritative_for") or []),
            "source_contract": path.relative_to(root).as_posix(),
        }
    return rows


def external_sources(sql: str, root: Path | None = None) -> list[dict[str, Any]]:
    contracts = load_external_source_contracts(root)
    rows: list[dict[str, Any]] = []
    for table in physical_source_tables(sql):
        normalized = table.strip("`").lower()
        contract = contracts.get(normalized)
        if contract:
            rows.append(dict(contract))
    return rows


def _replace_param_refs(value: str, params: dict[str, str]) -> str:
    text = str(value or "")
    patterns = [
        r"\(\s*select\s+(?:[A-Za-z_][\w$]*\.)?([A-Za-z_][\w$]*)\s+from\s+params(?:\s+[A-Za-z_][\w$]*)?\s*\)",
        r"\b(?:[A-Za-z_][\w$]*\.)?([A-Za-z_][\w$]*)\b",
    ]
    for pattern in patterns:
        text = re.sub(
            pattern,
            lambda match: params.get(str(match.group(1)).lower(), match.group(0)),
            text,
            flags=re.I,
        )
    return " ".join(text.split())


def extract_filters(sql: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    params = params or params_cte_expressions(sql)
    rows: list[dict[str, Any]] = []
    patterns = [
        (
            r"\b(iZoneAreaID|GameSvrId|GameMode|MatchMode|BattleSrvId|TemplateId|ActionType)\b\s*=\s*"
            r"(\(\s*SELECT\s+[^)]+\)|(?:[A-Za-z_][\w$]*\.)?[A-Za-z_][\w$]*|\d+|'[^']+')",
            "fixed_value",
        ),
        (r"\b(iZoneAreaID|GameSvrId|GameMode|MatchMode|BattleSrvId|TemplateId|ActionType)\b\s+IN\s*\(([^)]+)\)", "value_set"),
    ]
    for pattern, kind in patterns:
        for match in re.finditer(pattern, sql, flags=re.I):
            field = match.group(1)
            raw_value = _replace_param_refs(match.group(2), params)[:160]
            rows.append(
                {
                    "field": field,
                    "label": field,
                    "condition": f"{field} {'IN' if kind == 'value_set' else '='} {raw_value}",
                    "kind": kind,
                    "business_effect": f"限定 {field} 为 {raw_value}。",
                }
            )
    for field in unique_in_order(TIME_FIELD_RE.findall(sql)):
        rows.append(
            {
                "field": field,
                "label": field,
                "condition": f"{field} 使用 params 时间边界",
                "kind": "time_scope",
                "business_effect": "限定查询业务时间或执行分区范围。",
            }
        )
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        dedup.setdefault(str(row.get("condition") or row), row)
    return list(dedup.values())[:24]


def classify_fields(
    fields: list[str],
    result_columns: list[str],
    analysis: dict[str, Any],
    *,
    sql: str,
    subject_identity: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fields = fields or result_columns
    projection = final_select_projection(sql)
    expressions_by_alias = {
        parsed["alias"].casefold(): parsed["expression"]
        for parsed in (
            parse_select_expression(expression)
            for expression in (projection.expressions if projection else [])
        )
        if parsed["alias"]
    }
    analysis_metrics = {str(item).lower() for item in analysis.get("metrics", []) or []}
    analysis_dimensions = {str(item).lower() for item in analysis.get("dimensions", []) or []}
    metrics: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    for field in fields:
        lower = field.lower()
        is_metric = lower in analysis_metrics or bool(METRIC_RE.search(field))
        if is_metric:
            metric_type = "rate" if RATIO_RE.search(field) else "count" if re.search(r"人数|次数|数量|cnt|count|num", field, re.I) else "value"
            metric = {
                "name": field,
                "field": field,
                "label": field,
                "business_meaning": f"{field}。",
                "metric_type": metric_type,
                "numerator": "由 SQL 最终字段计算，完整表达式见代码/spec。" if metric_type == "rate" else "",
                "denominator": "由 SQL 最终字段计算，完整表达式见代码/spec。" if metric_type == "rate" else "",
                "dedup_key": "",
                "aggregation_dimensions": [],
                "key_conditions": [],
            }
            metric.update(
                metric_subject_binding(
                    field,
                    expressions_by_alias.get(field.casefold(), ""),
                    subject_identity,
                )
            )
            metrics.append(metric)
        elif lower in analysis_dimensions or not is_metric:
            dimensions.append({"field": field, "label": field, "role": "grouping", "data_type": "string"})
    if not metrics and fields:
        metrics.append(
            {
                "name": fields[-1],
                "field": fields[-1],
                "label": fields[-1],
                "business_meaning": f"{fields[-1]}。",
                "metric_type": "value",
                "numerator": "",
                "denominator": "",
                "dedup_key": "",
                "aggregation_dimensions": [],
                "key_conditions": [],
            }
        )
        dimensions = [
            {"field": item, "label": item, "role": "grouping", "data_type": "string"}
            for item in fields[:-1]
        ]
    labels = [item["label"] for item in dimensions]
    for metric in metrics:
        metric["aggregation_dimensions"] = labels
    return metrics, dimensions


def performance_structure(sql: str) -> dict[str, Any]:
    """Extract project-independent performance structure once for downstream routing."""
    cleaned = strip_sql_comments(sql)
    lowered = cleaned.lower()
    cte_names = extract_cte_names(sql)
    cte_structure = cte_dependency_structure(sql)
    cte_keys = {item.lower() for item in cte_names} | {"params"}
    references = table_references(sql)
    source_references = [item for item in references if item.lower() not in cte_keys]
    source_tables = unique_in_order(source_references)
    reference_counts: dict[str, int] = {}
    for table in source_references:
        key = table.lower()
        reference_counts[key] = reference_counts.get(key, 0) + 1
    has_group_by = bool(re.search(r"\bgroup\s+by\b", lowered))
    has_aggregate = bool(
        re.search(r"\b(count|sum|avg|min|max|percentile|approx_count_distinct)\s*\(", lowered)
    )
    return {
        "source_tables": source_tables,
        "table_references": references,
        "source_reference_counts": reference_counts,
        "cte_count": len(cte_names),
        **cte_structure,
        "join_count": len(re.findall(r"\bjoin\b", lowered)),
        "count_distinct_count": len(re.findall(r"\bcount\s*\(\s*distinct\b", lowered)),
        "window_function_count": len(re.findall(r"\bover\s*\(", lowered)),
        "union_count": len(re.findall(r"\bunion\b", lowered)),
        "select_star": bool(
            re.search(r"\bselect\s+\*", lowered)
            or re.search(r",\s*\*\s*(?:,|\bfrom\b)", lowered)
        ),
        "has_limit": bool(re.search(r"\blimit\s+\d+|\blimit\s+\$\{", lowered)),
        "has_group_by": has_group_by,
        "has_aggregate": has_aggregate,
        "has_detail_output": not has_group_by and not has_aggregate,
        "has_ratio_metric": bool(
            re.search(r"(?:_rate|_ratio|_pct|_percent|占比|比例|转化率|留存率)", lowered)
            or "/" in lowered
        ),
    }


def build_sql_fact_bundle(
    sql: str,
    *,
    kind: str = "QUERY",
    root: Path | None = None,
    result_columns: list[str] | None = None,
) -> dict[str, Any]:
    analysis = analyze_sql_text(sql, kind)
    final_fields = final_select_field_aliases(sql)
    project_config: dict[str, Any] = {}
    if root is not None:
        config_path = root / "project_config.json"
        if config_path.exists():
            try:
                loaded_config = json.loads(config_path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded_config, dict):
                    project_config = loaded_config
            except (OSError, json.JSONDecodeError):
                project_config = {}
    performance = performance_structure(sql)
    subject_identity = analyze_subject_identity(
        sql,
        project_config,
        join_count=int(performance.get("join_count") or 0),
    )
    metrics, dimensions = classify_fields(
        final_fields,
        result_columns or [],
        analysis,
        sql=sql,
        subject_identity=subject_identity,
    )
    finalize_complexity_audit(subject_identity, metrics)
    params = params_cte_expressions(sql)
    external = external_sources(sql, root)
    if analysis.get("business_category") == "uncategorized":
        scopes = unique_in_order(
            scope
            for source in external
            for scope in source.get("business_scope", [])
        )
        if scopes:
            analysis["business_category"] = scopes[0]
            analysis["tags"] = unique_in_order([scopes[0], *analysis.get("tags", [])])
    return {
        "schema_version": SQL_FACT_SCHEMA_VERSION,
        "fingerprint_version": FINGERPRINT_VERSION,
        "execution_fingerprint": execution_fingerprint(sql),
        "logic_fingerprint": logic_fingerprint(sql),
        "analysis": analysis,
        "source_tables": analysis["source_tables"],
        "referenced_tables": analysis["referenced_tables"],
        "cte_names": analysis["cte_names"],
        "target_tables": analysis["target_tables"],
        "source_logs": source_logs(sql, root),
        "external_sources": external,
        "final_fields": final_fields,
        "metrics": metrics,
        "dimensions": dimensions,
        "subject_identity": subject_identity,
        "filters": extract_filters(sql, params),
        "params": params,
        "privacy": {
            "final_raw_identifier_exposed": final_select_exposes_raw_ids(sql),
            "sql_side_deidentification_forbidden": True,
            "sql_side_privacy_transforms": sql_side_privacy_transforms(sql),
            "privacy_handling_owner": "DA",
        },
        "performance": performance,
    }
