#!/usr/bin/env python3
"""Classify SQL performance optimization depth before loading the full guide."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sql_facts import (  # noqa: E402
    build_sql_fact_bundle,
    execution_fingerprint,
    performance_structure,
    strip_sql_comments,
    unique_in_order,
)
from sql_execution_adapter import (  # noqa: E402
    effective_config_for_sql,
    effective_config_from_route,
    execution_route_for_sql,
    route_matches_context,
)
from sql_identifier_policy import policy_findings as identifier_policy_findings  # noqa: E402
from sql_time_contract import (  # noqa: E402
    TIME_CONTRACT_VERSION,
    _depth_at_position,
    _query_clause_at,
    _word_tokens_with_depth,
    analyze_time_contract,
)


ROUTING_REFERENCE = "references/performance-routing.md"
FULL_REFERENCE = "references/performance-optimization.md"
TIER_L0 = "L0_perf_lite"
TIER_L1 = "L1_perf_standard"
TIER_L2 = "L2_perf_deep"
TIER_L3 = "L3_perf_blocking"

BLOCKER_CODES = {
    "hive_missing_partition_filter",
    "hive_incomplete_partition_filter",
    "hive_missing_partition_policy",
    "hive_select_distinct_group_by",
    "non_native_hive_string_collect",
    "unconfigured_import_partition",
    "starrocks_tdbank_partition",
    "missing_business_time_filter",
    "function_wrapped_time_filter",
    "select_star",
    "incomplete_predicate",
    "raw_large_log_join",
    "battlesrvid_without_anti_crossing_key",
    "ratio_after_detail_join_risk",
    "raw_cumulative_duration_sum",
    "unobservable_retention_zero",
    "unsafe_midnight_concat",
    "execution_profile_contract",
    "unquoted_case_sensitive_identifier",
    "case_mismatched_identifier",
    "starrocks_cte_expansion_risk",
}

STARROCKS_CTE_GUARDRAIL = {
    "cte_count": 48,
    "dependency_depth": 30,
    "join_count": 30,
    "final_reference_span": 32,
}

ANALYSIS_PATTERN_KEYWORDS = {
    "retention": ["retention", "留存", "次留", "n日留存"],
    "funnel": ["funnel", "漏斗", "转化漏斗"],
    "return": ["return", "reflow", "回流", "流失"],
    "duration": ["duration", "onlinetime", "matchduration", "totalactiveduration", "时长", "耗时"],
    "battle": ["battlesrvid", "uniquebattleid", "battle", "session", "战斗", "对局", "新手服"],
    "distribution": ["bucket", "分桶", "分布", "区间"],
}


@dataclass
class Trigger:
    code: str
    message: str
    score: int = 0
    severity: str = "info"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "score": self.score,
            "severity": self.severity,
        }


def read_json_file(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        if path.is_dir():
            path = path / "project_config.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_sql(sql: str) -> str:
    return compact(strip_sql_comments(sql)).lower()


def performance_fingerprint(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode("utf-8")).hexdigest()[:16]


def is_tlog_table(table: str) -> bool:
    lowered = table.lower()
    return "_dsl_" in lowered or "tdbank" in lowered or lowered.endswith("_fht0")


def parse_date_literal(value: str) -> datetime | None:
    text = value.strip().replace("T", " ")
    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d")
        except ValueError:
            return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def inferred_scan_days(sql: str) -> int | None:
    literals = re.findall(r"'(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?|\d{8})'", sql)
    dates = [item for item in (parse_date_literal(value) for value in literals) if item]
    if len(dates) < 2:
        return None
    delta = max(dates) - min(dates)
    return max(0, delta.days)


def has_lower_and_upper(text: str, field: str) -> tuple[bool, bool]:
    escaped = re.escape(field.lower())
    has_between = bool(re.search(rf"\b{escaped}\b\s+between\b", text))
    has_lower = has_between or bool(re.search(rf"\b{escaped}\b\s*(?:>=|>)", text))
    has_upper = has_between or bool(re.search(rf"\b{escaped}\b\s*(?:<=|<)", text))
    return has_lower, has_upper


TIME_WRAPPER_NAMES = "cast|date|substr|substring|from_unixtime|date_format|to_date"
SQL_EXPRESSION_WORDS = {
    "as",
    "binary",
    "boolean",
    "date",
    "datetime",
    "decimal",
    "double",
    "float",
    "int",
    "integer",
    "string",
    "timestamp",
    "varchar",
}


def _matching_parenthesis(text: str, open_index: int) -> int:
    """Return the close paren for a function call, respecting SQL literals."""

    depth = 0
    quote: str | None = None
    index = open_index
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if quote:
            if char == quote:
                if quote == "'" and next_char == "'":
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _skip_sql_space(text: str, index: int, step: int = 1) -> int:
    while 0 <= index < len(text) and text[index].isspace():
        index += step
    return index


def _expression_is_column_derived(expression: str) -> bool:
    """Identify a row/column expression so equality checks are not called ranges.

    The time-integrity predicate compares two event fields after converting both
    to DATE.  That equality cannot prune the partition, but it also is not a
    wrapped-field-versus-constant range filter.  Keep this classifier deliberately
    conservative: literals, params, subqueries, and runtime dates remain
    non-column expressions and therefore retain the blocker.
    """

    value = strip_sql_comments(expression).strip()
    if not value:
        return False
    if "'" in value or re.search(r"\b(?:select|from|current_date|current_timestamp|now)\b", value, re.I):
        return False
    if re.search(r"\b(?:pt_start|pt_end|ts_start|ts_end)\b", value, re.I):
        return False
    if re.search(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?(?![A-Za-z_])", value):
        return False
    identifiers = re.findall(r"`[^`]+`|[A-Za-z_][\w$]*", value)
    return any(item.strip("`").casefold() not in SQL_EXPRESSION_WORDS for item in identifiers)


def _comparison_rhs(text: str, start: int) -> str:
    """Read one comparison operand until its enclosing WHERE predicate ends."""

    depth = 0
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if quote:
            if char == quote:
                if quote == "'" and next_char == "'":
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            if depth == 0:
                break
            depth -= 1
            index += 1
            continue
        if depth == 0 and re.match(r"\s+(?:and|or|group\s+by|order\s+by|having|qualify|limit|union)\b", text[index:], re.I):
            break
        index += 1
    return text[start:index].strip()


def _comparison_lhs(text: str, operator_start: int, expression_start: int) -> str:
    """Read the left operand of a comparison, bounded by WHERE boolean syntax."""

    current_depth = _depth_at_position(text, operator_start)
    boundary = expression_start
    for word, token_position, token_depth in _word_tokens_with_depth(text):
        if token_position >= operator_start or token_depth != current_depth:
            continue
        if word in {"where", "and", "or"}:
            boundary = max(boundary, token_position + len(word))

    index = operator_start - 1
    depth = 0
    quote: str | None = None
    while index >= expression_start:
        char = text[index]
        if quote:
            if char == quote:
                quote = None
            index -= 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index -= 1
            continue
        if char == ")":
            depth += 1
        elif char == "(":
            if depth:
                depth -= 1
            else:
                break
        elif depth == 0 and char in {"\n", ";"}:
            break
        index -= 1
    segment = text[max(index + 1, boundary) : operator_start].strip()
    segment = re.sub(r"^(?:where|and|or)\s+", "", segment, flags=re.I)
    return segment.strip()


def function_wrapped_range_filter(text: str, field: str) -> bool:
    """Detect a wrapped time field used as a pruning predicate.

    Do not treat a wrapper in SELECT/GROUP BY, or a field-to-field equality
    such as ``CAST(event_time AS DATE) = CAST(event_date AS DATE)``, as a
    failed range filter.  The raw partition bounds remain independently
    checked by ``has_lower_and_upper``.
    """

    if not field:
        return False
    cleaned = strip_sql_comments(text)
    tokens = _word_tokens_with_depth(cleaned)
    escaped = re.escape(field.strip("`"))
    wrapper = re.compile(rf"\b(?:{TIME_WRAPPER_NAMES})\s*\(", re.I)
    field_arg = re.compile(
        rf"(?:[A-Za-z_][\w$]*\s*\.\s*)?`?{escaped}`?(?=\s*(?:,|\)|\bas\b))",
        re.I,
    )
    comparison_after = re.compile(r"(?P<operator><=|>=|<>|!=|=|<|>|\bin\b|\bbetween\b|\bis\b)", re.I)
    comparison_before = re.compile(r"(?P<operator><=|>=|<>|!=|=|<|>|\bin\b|\bbetween\b|\bis\b)\s*$", re.I)

    for match in wrapper.finditer(cleaned):
        open_index = cleaned.find("(", match.start(), match.end())
        argument = field_arg.match(cleaned, open_index + 1)
        if not argument:
            continue
        depth = _depth_at_position(cleaned, match.start())
        if _query_clause_at(tokens, match.start(), depth) != "where":
            continue
        close_index = _matching_parenthesis(cleaned, open_index)
        if close_index == -1:
            continue

        after_index = _skip_sql_space(cleaned, close_index + 1)
        after = comparison_after.match(cleaned, after_index)
        if after:
            operator = after.group("operator").casefold()
            rhs_start = after.end()
            rhs = _comparison_rhs(cleaned, rhs_start)
            if operator == "=" and _expression_is_column_derived(rhs):
                continue
            return True

        before = comparison_before.search(cleaned, 0, match.start())
        if before:
            operator = before.group("operator").casefold()
            if operator == "=":
                lhs = _comparison_lhs(cleaned, before.start(), max(0, match.start() - 512))
                if _expression_is_column_derived(lhs):
                    continue
            return True
    return False


def depth_before(text: str, pos: int) -> int:
    depth = 0
    for char in text[:pos]:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
    return depth


def keyword_at(text: str, pos: int, keyword: str) -> bool:
    end = pos + len(keyword)
    if text[pos:end] != keyword:
        return False
    before = text[pos - 1] if pos > 0 else " "
    after = text[end] if end < len(text) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def select_distinct_group_by_blocks(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    lowered = cleaned.lower()
    findings: list[str] = []
    for match in re.finditer(r"\bselect\b", lowered):
        select_pos = match.start()
        select_depth = depth_before(lowered, select_pos)
        after_select = match.end()
        distinct_match = re.match(r"\s+distinct\b", lowered[after_select:])
        if not distinct_match:
            continue
        scan_pos = after_select + distinct_match.end()
        block_end = len(lowered)
        i = scan_pos
        depth = select_depth
        while i < len(lowered):
            char = lowered[i]
            if char == "(":
                depth += 1
                i += 1
                continue
            if char == ")":
                depth -= 1
                if depth < select_depth:
                    block_end = i
                    break
                i += 1
                continue
            if depth == select_depth and (
                keyword_at(lowered, i, "union")
                or keyword_at(lowered, i, "intersect")
                or keyword_at(lowered, i, "except")
            ):
                block_end = i
                break
            i += 1
        depth = select_depth
        j = scan_pos
        has_same_level_group_by = False
        while j < block_end:
            char = lowered[j]
            if char == "(":
                depth += 1
                j += 1
                continue
            if char == ")":
                depth -= 1
                j += 1
                continue
            if depth == select_depth and re.match(r"group\s+by\b", lowered[j:]):
                has_same_level_group_by = True
                break
            j += 1
        if has_same_level_group_by:
            snippet = compact(cleaned[select_pos : min(block_end, select_pos + 180)])
            findings.append(snippet)
    return unique_in_order(findings)


def native_hive_string_collect_patterns(sql: str) -> list[str]:
    """Detect Hive-native string sample aggregation patterns unsafe for compatibility analyzers."""
    cleaned = strip_sql_comments(sql)
    findings: list[str] = []
    patterns = [
        r"\bconcat_ws\s*\(\s*['\"][^'\"]*['\"]\s*,\s*collect_(?:list|set)\s*\(",
        r"\bcollect_(?:list|set)\s*\(",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned, flags=re.I):
            snippet = compact(cleaned[match.start() : min(len(cleaned), match.start() + 180)])
            findings.append(snippet)
    return unique_in_order(findings)


def find_matching_paren(sql: str, open_index: int) -> int:
    """Return the closing paren for open_index while ignoring quoted text."""

    depth = 0
    quote: str | None = None
    i = open_index
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote:
            if ch == quote:
                if quote == "'" and nxt == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
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


def split_top_level_args(text: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if quote:
            if ch == quote:
                if quote == "'" and nxt == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        args.append(tail)
    return args


def _is_midnight_suffix_literal(arg: str) -> bool:
    return bool(re.fullmatch(r"""(?is)['"]\s*00:00:00\s*['"]""", arg.strip()))


def unsafe_midnight_concat_patterns(sql: str) -> list[str]:
    """Detect SQL that hand-appends a fixed midnight suffix to date/time text."""

    cleaned = strip_sql_comments(sql)
    findings: list[str] = []
    for match in re.finditer(r"\bconcat\s*\(", cleaned, flags=re.I):
        open_index = cleaned.find("(", match.start())
        close_index = find_matching_paren(cleaned, open_index)
        if close_index == -1:
            continue
        args = split_top_level_args(cleaned[open_index + 1 : close_index])
        if len(args) < 2 or not any(_is_midnight_suffix_literal(arg) for arg in args[1:]):
            continue
        snippet = compact(cleaned[match.start() : min(len(cleaned), close_index + 1)])
        findings.append(snippet)
    return unique_in_order(findings)


def incomplete_predicates(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    text = compact(cleaned)
    findings: list[str] = []
    comparison_pattern = re.compile(
        r"(?P<field>(?:`[^`]+`|[a-zA-Z_][\w]*)(?:\.(?:`[^`]+`|[a-zA-Z_][\w]*))?)"
        r"\s*(?P<operator>=|<>|!=|>=|<=|>|<)\s*"
        r"(?=(?:\)|,|;|\b(?:and|or|group|order|having|limit|union|where|join|on)\b|$))",
        flags=re.I,
    )
    for match in comparison_pattern.finditer(text):
        findings.append(f"{match.group('field')} {match.group('operator')}")
    empty_in_pattern = re.compile(
        r"(?P<field>(?:`[^`]+`|[a-zA-Z_][\w]*)(?:\.(?:`[^`]+`|[a-zA-Z_][\w]*))?)\s+"
        r"(?P<operator>not\s+in|in)\s*\(\s*\)",
        flags=re.I,
    )
    for match in empty_in_pattern.finditer(text):
        findings.append(f"{match.group('field')} {compact(match.group('operator')).upper()} ()")
    return unique_in_order(findings)


def project_partition_policy(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("partition_policy")
    return value if isinstance(value, dict) else {}


def project_dialect(config: dict[str, Any]) -> str:
    return str(config.get("sql_dialect") or config.get("query_engine") or "").strip()


def config_text(config: dict[str, Any]) -> str:
    environment = config.get("query_environment")
    if isinstance(environment, dict):
        environment_text = " ".join(str(value or "") for value in environment.values())
    else:
        environment_text = str(environment or "")
    return f"{config.get('sql_dialect', '')} {config.get('query_engine', '')} {environment_text}".lower()


def is_hive_like(config: dict[str, Any]) -> bool:
    text = config_text(config)
    return "hive" in text or "tdbank" in text


def is_starrocks(config: dict[str, Any]) -> bool:
    text = config_text(config)
    return "starrocks" in text


def uses_non_native_hive_execution(config: dict[str, Any]) -> bool:
    """Return true when SQL may pass through DA/MySQL/StarRocks-compatible execution."""
    if is_starrocks(config):
        return True
    text = config_text(config)
    compatibility_tokens = [
        "da",
        "pymysql",
        "mysql",
        "starrocks",
        "dashboard",
        "compat",
        "兼容",
        "分析器",
        "执行器",
    ]
    if any(token in text for token in compatibility_tokens):
        return True
    native_hive_tokens = [
        "native hive",
        "pure hive",
        "原生hive",
        "原生 hive",
        "纯hive",
        "纯 hive",
    ]
    if any(token in text for token in native_hive_tokens):
        return False
    return True


def starrocks_cte_expansion_assessment(
    structure: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Apply an empirical combined guardrail, not a claimed StarRocks engine limit."""
    cte_count = int(structure.get("cte_count") or 0)
    dependency_depth = int(structure.get("cte_dependency_depth") or 0)
    join_count = int(structure.get("join_count") or 0)
    final_reference_span = int(structure.get("max_final_cte_reference_span") or 0)
    signals = {
        "many_ctes": cte_count >= STARROCKS_CTE_GUARDRAIL["cte_count"],
        "deep_dependency_chain": dependency_depth >= STARROCKS_CTE_GUARDRAIL["dependency_depth"],
        "many_joins": join_count >= STARROCKS_CTE_GUARDRAIL["join_count"],
        "long_final_reference_span": final_reference_span
        >= STARROCKS_CTE_GUARDRAIL["final_reference_span"],
    }
    high_risk = bool(
        is_starrocks(config)
        and signals["many_ctes"]
        and signals["deep_dependency_chain"]
        and (signals["many_joins"] or signals["long_final_reference_span"])
    )
    elevated_signal_count = sum(
        [
            cte_count >= 40,
            dependency_depth >= 24,
            join_count >= 24,
            final_reference_span >= 24,
        ]
    )
    risk_level = "high" if high_risk else "elevated" if is_starrocks(config) and elevated_signal_count >= 3 else "normal"
    return {
        "schema_version": "starrocks_cte_expansion_assessment_v1",
        "risk_level": risk_level,
        "blocks_starrocks_delivery": high_risk,
        "empirical_guardrail": dict(STARROCKS_CTE_GUARDRAIL),
        "observed": {
            "cte_count": cte_count,
            "dependency_depth": dependency_depth,
            "join_count": join_count,
            "final_reference_span": final_reference_span,
        },
        "signals": signals,
        "note": "Combined structural guardrail derived from a DA/StarRocks CTE scope-loss incident; it is not an engine limit.",
    }


def classify_execution_error(sql: str, error_message: str) -> dict[str, Any]:
    """Classify executor relation errors using the SQL's declared CTE namespace."""
    message = compact(error_message)
    match = re.search(
        r"unknown\s+table\s+['\"`]?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
        message,
        flags=re.I,
    )
    if not match:
        return {
            "schema_version": "sql_execution_error_diagnosis_v1",
            "status": "unclassified",
            "classification": "unclassified",
            "reported_relation": "",
            "message": message,
        }
    relation = match.group(1)
    relation_name = relation.split(".")[-1]
    structure = performance_structure(sql)
    declared = {
        str(item.get("cte") or "").casefold(): str(item.get("cte") or "")
        for item in structure.get("cte_dependency_edges", [])
        if isinstance(item, dict)
    }
    declared_name = declared.get(relation_name.casefold(), "")
    if declared_name:
        spans = structure.get("final_cte_reference_spans")
        return {
            "schema_version": "sql_execution_error_diagnosis_v1",
            "status": "classified",
            "classification": "cte_scope_or_expansion_failure",
            "reported_relation": relation,
            "declared_cte": declared_name,
            "final_reference_span": int((spans or {}).get(declared_name) or 0),
            "message": message,
            "recommended_actions": [
                "Compress passthrough CTEs and shorten the dependency chain.",
                "Move a small terminal aggregate inline when it preserves the business grain.",
                "Split the query; use the stable Hive profile only after explicit user selection.",
            ],
        }
    return {
        "schema_version": "sql_execution_error_diagnosis_v1",
        "status": "classified",
        "classification": "missing_or_unresolved_relation",
        "reported_relation": relation,
        "declared_cte": "",
        "message": message,
    }


def join_on_segments(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    segments = []
    pattern = re.compile(
        r"\bjoin\b.+?\bon\b(?P<on>.*?)(?=\b(?:join|where|group\s+by|having|order\s+by|limit|union)\b|$)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(cleaned):
        segments.append(compact(match.group("on")))
    return segments


def detect_generation_triggers(
    *,
    intent: str,
    artifact_kind: str,
    source_table_count: int,
    expected_time_days: int | None,
    reusable: bool,
    intermediate_candidate: bool,
) -> list[Trigger]:
    triggers: list[Trigger] = []
    lowered = intent.lower()
    if not intent.strip():
        triggers.append(Trigger("generation_default_standard", "生成前缺少可解析 SQL，默认使用标准性能路由。", 3, "warn"))
    if source_table_count > 0:
        triggers.append(Trigger("expected_source_tables", f"预计来源表 {source_table_count} 个。", source_table_count * 2, "info"))
        if source_table_count >= 3:
            triggers.append(Trigger("many_expected_source_tables", "预计 3 个及以上来源表，进入深度优化。", 4, "warn"))
    for code, keywords in ANALYSIS_PATTERN_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            triggers.append(Trigger(f"intent_{code}", f"需求命中复杂分析类型：{code}。", 4, "warn"))
    if any(word in lowered for word in ["最佳性能", "性能最好", "深度优化", "优化到最好", "best performance"]):
        triggers.append(Trigger("explicit_best_performance", "用户明确要求最佳性能或深度优化。", 8, "warn"))
    if expected_time_days and expected_time_days > 30:
        triggers.append(Trigger("long_expected_window", f"预计扫描窗口超过 30 天：{expected_time_days} 天。", 3, "warn"))
    if artifact_kind.upper() in {"DASHBOARD", "VALIDATION"}:
        triggers.append(Trigger("formal_artifact", f"{artifact_kind.upper()} 需要保留性能结论。", 2, "info"))
    if reusable:
        triggers.append(Trigger("reusable_artifact", "计划沉淀为可复用 SQL。", 2, "info"))
    if intermediate_candidate:
        triggers.append(Trigger("intermediate_candidate", "已标记为中间表候选。", 2, "warn"))
    return triggers


def shared_performance_structure(
    sql: str,
    sql_facts: dict[str, Any] | None,
    artifact_kind: str,
) -> tuple[dict[str, Any], str]:
    if sql_facts:
        expected = execution_fingerprint(sql)
        actual = str(sql_facts.get("execution_fingerprint") or "")
        if actual != expected:
            raise ValueError("performance preflight received a stale SqlFactBundle for different SQL")
        structure = sql_facts.get("performance")
        if (
            isinstance(structure, dict)
            and structure
            and "cte_dependency_depth" in structure
        ):
            return structure, "provided_sql_fact_bundle"
    bundle = build_sql_fact_bundle(sql, kind=artifact_kind)
    structure = bundle.get("performance")
    if not isinstance(structure, dict) or not structure:
        raise ValueError("SqlFactBundle is missing performance structure")
    return structure, "built_sql_fact_bundle"


def detect_sql_triggers(
    *,
    sql: str,
    config: dict[str, Any],
    artifact_kind: str,
    reusable: bool,
    intermediate_candidate: bool,
    sql_facts: dict[str, Any] | None = None,
    time_contract: dict[str, Any] | None = None,
) -> tuple[list[Trigger], dict[str, Any]]:
    triggers: list[Trigger] = []
    lowered = normalize_sql(sql)
    structure, fact_source = shared_performance_structure(sql, sql_facts, artifact_kind)
    physical_tables = [str(item) for item in structure.get("source_tables", [])]
    tlog_tables = [table for table in physical_tables if is_tlog_table(table)]
    source_reference_counts = structure.get("source_reference_counts")
    tlog_ref_counts = {
        str(table).lower(): int(count)
        for table, count in (source_reference_counts.items() if isinstance(source_reference_counts, dict) else [])
        if is_tlog_table(str(table))
    }
    repeated_tables = sorted({table for table, count in tlog_ref_counts.items() if count > 1})
    cte_count = int(structure.get("cte_count") or 0)
    join_count = int(structure.get("join_count") or 0)
    count_distinct_count = int(structure.get("count_distinct_count") or 0)
    window_count = int(structure.get("window_function_count") or 0)
    union_count = int(structure.get("union_count") or 0)
    select_star = bool(structure.get("select_star"))
    has_limit = bool(structure.get("has_limit"))
    has_group_by = bool(structure.get("has_group_by"))
    hive_distinct_group_blocks = select_distinct_group_by_blocks(sql) if is_hive_like(config) else []
    hive_string_collect_blocks = (
        native_hive_string_collect_patterns(sql) if uses_non_native_hive_execution(config) else []
    )
    has_aggregate = bool(structure.get("has_aggregate"))
    has_detail_output = bool(structure.get("has_detail_output"))
    scan_days = inferred_scan_days(sql)

    if tlog_tables:
        score = len(tlog_tables) * 2 + max(0, len(tlog_tables) - 1) * 2
        triggers.append(Trigger("tlog_source_tables", f"TLOG 来源表 {len(tlog_tables)} 个。", score, "info"))
        if len(tlog_tables) >= 3:
            triggers.append(Trigger("many_tlog_tables", "3 个及以上 TLOG 来源表需要深度优化。", 4, "warn"))
    if join_count:
        triggers.append(Trigger("join_count", f"JOIN 数量 {join_count} 个。", join_count * 2, "info" if join_count < 4 else "warn"))
    if cte_count > 4:
        triggers.append(Trigger("cte_count", f"顶层 CTE 数量 {cte_count}，超过 4 个的部分计入复杂度。", cte_count - 4, "warn" if cte_count >= 8 else "info"))
    cte_expansion = starrocks_cte_expansion_assessment(structure, config)
    if cte_expansion["blocks_starrocks_delivery"]:
        observed = cte_expansion["observed"]
        triggers.append(
            Trigger(
                "starrocks_cte_expansion_risk",
                "StarRocks/DA CTE 展开风险过高："
                f"顶层 CTE {observed['cte_count']} 个、最长依赖链 {observed['dependency_depth']} 层、"
                f"JOIN {observed['join_count']} 个、最终引用最大跨度 {observed['final_reference_span']}。"
                "先压缩透传 CTE、缩短依赖链、内联小型终点聚合，或改走稳定 Hive，再交付。",
                0,
                "blocker",
            )
        )
    elif cte_expansion["risk_level"] == "elevated":
        observed = cte_expansion["observed"]
        triggers.append(
            Trigger(
                "starrocks_cte_expansion_elevated",
                "StarRocks SQL 的 CTE 结构压力较高："
                f"顶层 CTE {observed['cte_count']} 个、最长依赖链 {observed['dependency_depth']} 层、"
                f"JOIN {observed['join_count']} 个；交付前优先压缩无业务语义的透传层。",
                0,
                "warn",
            )
        )
    if count_distinct_count:
        triggers.append(Trigger("count_distinct", f"COUNT DISTINCT {count_distinct_count} 处。", count_distinct_count * 2, "warn"))
    if window_count:
        triggers.append(Trigger("window_function", f"窗口函数 {window_count} 处。", 3, "warn"))
    if union_count:
        triggers.append(Trigger("union", f"UNION/UNION ALL {union_count} 处。", union_count * 2, "info"))
    if structure.get("has_ratio_metric"):
        triggers.append(Trigger("ratio_metric", "检测到比例/占比/除法类指标。", 2, "info"))
    for code, keywords in ANALYSIS_PATTERN_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            triggers.append(Trigger(f"analysis_{code}", f"SQL 命中复杂分析类型：{code}。", 4, "warn"))
    if repeated_tables:
        triggers.append(Trigger("repeated_large_table_scan", f"同一大表重复扫描：{', '.join(repeated_tables[:5])}。", 4 * len(repeated_tables), "warn"))
    if scan_days and scan_days > 30:
        triggers.append(Trigger("long_scan_window", f"推断扫描窗口超过 30 天：约 {scan_days} 天。", 3, "warn"))
    if has_detail_output and not has_limit:
        triggers.append(Trigger("detail_without_limit", "疑似明细输出且没有 LIMIT。", 3, "warn"))
    if artifact_kind.upper() in {"DASHBOARD", "VALIDATION"}:
        triggers.append(Trigger("formal_artifact", f"{artifact_kind.upper()} 需要保留性能结论。", 2, "info"))
    if reusable:
        triggers.append(Trigger("reusable_artifact", "计划沉淀为可复用 SQL。", 2, "info"))
    if intermediate_candidate:
        triggers.append(Trigger("intermediate_candidate", "已标记为中间表候选。", 2, "warn"))

    partition_policy = project_partition_policy(config)
    partition_required = partition_policy.get("required_for_tlog") is True
    partition_field = str(partition_policy.get("partition_field") or "").strip()
    business_time_field = str(partition_policy.get("business_time_field") or ("dteventdate" if is_starrocks(config) else "dtEventTime")).strip()
    business_time_required = partition_policy.get("business_time_required") is True
    if tlog_tables and partition_required:
        if not partition_field:
            triggers.append(Trigger("hive_missing_partition_policy", "项目配置要求 TLOG 分区裁剪，但未配置 partition_field。", 0, "blocker"))
        else:
            has_lower, has_upper = has_lower_and_upper(lowered, partition_field)
            if not has_lower and not has_upper:
                triggers.append(Trigger("hive_missing_partition_filter", f"TLOG 缺少项目配置分区字段 `{partition_field}` 上下界。", 0, "blocker"))
            elif not (has_lower and has_upper):
                triggers.append(Trigger("hive_incomplete_partition_filter", f"TLOG 项目配置分区字段 `{partition_field}` 过滤不完整。", 0, "blocker"))
    if tlog_tables and not partition_required and not partition_field and "tdbank_imp_date" in lowered:
        triggers.append(
            Trigger(
                "unconfigured_import_partition",
                "当前项目分区策略不要求导入分区字段，SQL 不应默认使用 `tdbank_imp_date`。",
                0,
                "blocker",
            )
        )
    if tlog_tables and is_starrocks(config) and "tdbank_imp_date" in lowered:
        triggers.append(Trigger("starrocks_tdbank_partition", "StarRocks SQL 不应默认使用 TDBank `tdbank_imp_date`。", 0, "blocker"))
    if tlog_tables and business_time_required and business_time_field:
        has_lower, has_upper = has_lower_and_upper(lowered, business_time_field)
        if not (has_lower and has_upper):
            triggers.append(Trigger("missing_business_time_filter", f"TLOG 缺少项目业务时间字段 `{business_time_field}` 上下界。", 0, "blocker"))
        if function_wrapped_range_filter(lowered, business_time_field):
            triggers.append(Trigger("function_wrapped_time_filter", f"业务时间字段 `{business_time_field}` 在 WHERE 中被函数包裹，可能无法裁剪。", 0, "blocker"))
    if tlog_tables and partition_required and partition_field and function_wrapped_range_filter(lowered, partition_field):
        triggers.append(Trigger("function_wrapped_time_filter", f"分区字段 `{partition_field}` 在 WHERE 中被函数包裹，可能无法裁剪。", 0, "blocker"))
    time_contract = (
        time_contract
        if isinstance(time_contract, dict)
        and time_contract.get("contract_version") == TIME_CONTRACT_VERSION
        else analyze_time_contract(sql, config)
    )
    existing_codes = {item.code for item in triggers}
    for finding in time_contract.get("findings", []) or []:
        code = str(finding.get("code") or "project_time_contract")
        if code in existing_codes:
            continue
        if code in {"missing_partition_lower_bound", "missing_partition_upper_bound"} and existing_codes & {
            "hive_missing_partition_filter",
            "hive_incomplete_partition_filter",
        }:
            continue
        triggers.append(Trigger(code, str(finding.get("message") or "项目时间契约不满足。"), 0, "blocker"))
        existing_codes.add(code)
    identifier_findings = identifier_policy_findings(sql, config)
    for finding in identifier_findings:
        triggers.append(
            Trigger(
                str(finding.get("code") or "identifier_policy"),
                str(finding.get("message") or "SQL 标识符不满足执行环境契约。"),
                0,
                "blocker",
            )
        )
    if select_star:
        triggers.append(Trigger("select_star", "正式/生产 SQL 不应使用 SELECT *。", 0, "blocker"))
    if hive_distinct_group_blocks:
        triggers.append(
            Trigger(
                "hive_select_distinct_group_by",
                "Hive 执行端兼容硬约束：同一个 SELECT 查询块禁止同时使用 SELECT DISTINCT 和 GROUP BY；去重统一改为 GROUP BY。",
                0,
                "blocker",
            )
        )
    if hive_string_collect_blocks:
        triggers.append(
            Trigger(
                "non_native_hive_string_collect",
                "非纯 Hive 执行链禁止使用 collect_list/collect_set 生成字符串样例；改用 group_concat(CAST(expr AS string/varchar)) 或 group_concat(concat(...))。",
                0,
                "blocker",
            )
        )
    unsafe_midnight_blocks = unsafe_midnight_concat_patterns(sql)
    if unsafe_midnight_blocks:
        triggers.append(
            Trigger(
                "unsafe_midnight_concat",
                "不要对 DA 参数、cohort_date、date_add(...) 等日期/时间表达式再 concat 固定 `00:00:00`；这些值可能已带时间，改用参数原值或 date/to_date/date_add 类型函数。",
                0,
                "blocker",
            )
        )
    for predicate in incomplete_predicates(sql):
        triggers.append(Trigger("incomplete_predicate", f"SQL 条件不完整：{predicate}。", 0, "blocker"))

    if len(tlog_tables) >= 2 and join_count and not has_group_by:
        triggers.append(Trigger("raw_large_log_join", "多个大日志表疑似在明细层直接 JOIN，可能放大结果。", 0, "blocker"))
    on_text = " ".join(join_on_segments(sql)).lower()
    if "battlesrvid" in on_text and "uniquebattleid" not in on_text:
        if not any(key in on_text for key in ["izoneareaid", "zone_id", "game_mode", "gamemode", "event_date", "stat_date", "dt"]):
            triggers.append(
                Trigger(
                    "battlesrvid_without_anti_crossing_key",
                    "BattleSrvId JOIN 未看到区服/模式/日期/UniqueBattleID 等防串键。",
                    0,
                    "blocker",
                )
            )
    if re.search(r"(?:_rate|_ratio|_pct|_percent|占比|比例|转化率|留存率)", lowered) and join_count and not re.search(r"nullif|case\s+when", lowered):
        triggers.append(Trigger("ratio_after_detail_join_risk", "比例指标经过 JOIN 后计算且未看到零分母/预聚合保护。", 0, "blocker"))
    if re.search(r"\bsum\s*\(\s*(?:[\w]+\.)?`?totalactiveduration`?\s*\)", lowered) and not re.search(r"\bmax\s*\(\s*(?:[\w]+\.)?`?totalactiveduration`?\s*\)", lowered):
        triggers.append(Trigger("raw_cumulative_duration_sum", "累计时长 TotalActiveDuration 疑似直接 SUM，需先按正确粒度取 MAX/差值。", 0, "blocker"))
    if any(keyword in lowered for keyword in ["retention", "留存"]) and re.search(r"\bcoalesce\s*\([^)]*,\s*0\s*\)", lowered):
        triggers.append(Trigger("unobservable_retention_zero", "留存不可观测窗口疑似输出 0，应使用 NULL 或排除不可观测 cohort。", 0, "blocker"))

    facts = {
        "dialect": project_dialect(config) or "unknown",
        "table_count": len(physical_tables),
        "tlog_table_count": len(tlog_tables),
        "source_tables": physical_tables,
        "cte_count": cte_count,
        "cte_dependency_depth": int(structure.get("cte_dependency_depth") or 0),
        "cte_dependency_edge_count": int(structure.get("cte_dependency_edge_count") or 0),
        "final_cte_references": list(structure.get("final_cte_references") or []),
        "max_final_cte_reference_span": int(structure.get("max_final_cte_reference_span") or 0),
        "starrocks_cte_expansion": cte_expansion,
        "join_count": join_count,
        "count_distinct_count": count_distinct_count,
        "window_function_count": window_count,
        "union_count": union_count,
        "scan_days": scan_days,
        "has_detail_output": has_detail_output,
        "has_limit": has_limit,
        "hive_select_distinct_group_by_blocks": hive_distinct_group_blocks,
        "non_native_hive_execution": uses_non_native_hive_execution(config),
        "hive_string_collect_blocks": hive_string_collect_blocks,
        "unsafe_midnight_concat_blocks": unsafe_midnight_blocks,
        "partition_required_for_tlog": partition_required,
        "partition_field": partition_field,
        "business_time_field": business_time_field,
        "time_contract": time_contract,
        "identifier_policy_findings": identifier_findings,
        "performance_fingerprint": performance_fingerprint(sql),
        "sql_fact_source": fact_source,
    }
    return triggers, facts


def classify_tier(triggers: list[Trigger]) -> tuple[str, int, str]:
    score = sum(max(0, trigger.score) for trigger in triggers)
    blocker_codes = {trigger.code for trigger in triggers if trigger.severity == "blocker" or trigger.code in BLOCKER_CODES}
    if blocker_codes:
        return TIER_L3, score, "block"
    deep = any(
        trigger.code.startswith("analysis_")
        or trigger.code.startswith("intent_")
        or trigger.code in {
            "many_tlog_tables",
            "many_expected_source_tables",
            "window_function",
            "repeated_large_table_scan",
            "explicit_best_performance",
            "intermediate_candidate",
        }
        for trigger in triggers
    )
    if score >= 8 or deep:
        return TIER_L2, score, "warn"
    if score >= 3:
        return TIER_L1, score, "warn"
    return TIER_L0, score, "pass"


def required_checks_for_tier(tier: str) -> list[str]:
    base = [
        "确认当前项目方言的时间/分区过滤。",
        "确认没有 SELECT *。",
        "确认 SQL 未使用 MD5/SHA/HASH/BASE64/AES/MASK 做脱敏；隐私由 DA 侧处理。",
    ]
    if tier == TIER_L0:
        return base + ["明细/样例 SQL 必须有限制行数。"]
    if tier == TIER_L1:
        return base + [
            "大表 base CTE 先过滤时间/业务条件并裁剪字段。",
            "简单 JOIN 前尽量降到业务粒度。",
            "COUNT DISTINCT 如可安全预去重，应记录是否改写。",
        ]
    return base + [
        "加载完整性能手册并执行深度等价优化。",
        "检查重复大表扫描、raw 大表 JOIN、窗口函数、COUNT DISTINCT、比例分母放大。",
        "判断是否建议中间表；不得自动创建中间表。",
        "记录 optimization_applied 与 optimization_rejected。",
    ]


def optimization_hint(tier: str, status: str) -> str:
    if tier == TIER_L0:
        return "使用轻量性能清单即可，不需要加载完整性能优化手册。"
    if tier == TIER_L1:
        return "使用标准性能路由清单，记录必要的等价优化；默认不加载完整手册。"
    if tier == TIER_L2:
        return "进入深度优化，必须加载完整性能优化手册并记录等价优化结论。"
    if status == "block":
        return "存在阻断项；先按 blocker 改写 SQL，再进入需要的优化等级。"
    return "按路由结果执行性能优化。"


def analyze_performance(
    *,
    sql: str | None = None,
    sql_facts: dict[str, Any] | None = None,
    project_config: dict[str, Any] | None = None,
    mode: str = "review",
    artifact_kind: str = "QUERY",
    intent: str = "",
    source_table_count: int = 0,
    expected_time_days: int | None = None,
    reusable: bool = False,
    intermediate_candidate: bool = False,
    execution_error: str = "",
    execution_route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = project_config or {}
    if sql is not None and sql.strip():
        if route_matches_context(execution_route, sql, config):
            route = execution_route
            effective_config = effective_config_from_route(config, route)
        else:
            effective_config, detection = effective_config_for_sql(config, sql)
            route = execution_route_for_sql(
                sql,
                config,
                effective_config=effective_config,
                detection=detection,
            )
        triggers, facts = detect_sql_triggers(
            sql=sql,
            config=effective_config,
            artifact_kind=artifact_kind,
            reusable=reusable,
            intermediate_candidate=intermediate_candidate,
            sql_facts=sql_facts,
            time_contract=route.get("time_contract"),
        )
        for blocker in route.get("blockers", []) or []:
            triggers.append(Trigger("execution_profile_contract", str(blocker), 0, "blocker"))
        facts["execution_route"] = route
        facts["execution_profile"] = route.get("selected_profile", "")
    else:
        triggers = detect_generation_triggers(
            intent=intent,
            artifact_kind=artifact_kind,
            source_table_count=source_table_count,
            expected_time_days=expected_time_days,
            reusable=reusable,
            intermediate_candidate=intermediate_candidate,
        )
        facts = {
            "dialect": project_dialect(config) or "unknown",
            "mode": mode,
            "artifact_kind": artifact_kind.upper(),
            "performance_fingerprint": "",
        }
    tier, score, status = classify_tier(triggers)
    full_required = tier in {TIER_L2, TIER_L3}
    blockers = [trigger.message for trigger in triggers if trigger.severity == "blocker" or trigger.code in BLOCKER_CODES]
    trigger_messages = [f"{trigger.code}: {trigger.message}" for trigger in triggers]
    references = [ROUTING_REFERENCE]
    if full_required:
        references.append(FULL_REFERENCE)
    execution_error_diagnosis = (
        classify_execution_error(sql or "", execution_error)
        if sql and execution_error.strip()
        else {}
    )
    return {
        "status": "block" if blockers else status,
        "tier": tier,
        "score": score,
        "triggers": trigger_messages,
        "trigger_details": [trigger.as_dict() for trigger in triggers],
        "required_references": references,
        "required_checks": required_checks_for_tier(tier),
        "blockers": blockers,
        "optimization_hint": optimization_hint(tier, "block" if blockers else status),
        "full_guide_required": full_required,
        "performance_fingerprint": str(facts.get("performance_fingerprint") or ""),
        "facts": facts,
        "execution_error_diagnosis": execution_error_diagnosis,
    }


def render_text(result: dict[str, Any]) -> str:
    lines = [
        f"status: {result['status']}",
        f"tier: {result['tier']}",
        f"score: {result['score']}",
        "required_references:",
    ]
    lines.extend(f"  - {item}" for item in result.get("required_references", []))
    if result.get("blockers"):
        lines.append("blockers:")
        lines.extend(f"  - {item}" for item in result["blockers"])
    if result.get("triggers"):
        lines.append("triggers:")
        lines.extend(f"  - {item}" for item in result["triggers"])
    lines.append(f"optimization_hint: {result.get('optimization_hint', '')}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="SQL project root containing project_config.json")
    parser.add_argument("--project-config", help="Explicit project_config.json path")
    parser.add_argument("--mode", default="review", choices=["generation", "review", "validation", "dashboard", "promotion"])
    parser.add_argument("--artifact-kind", default="QUERY", choices=["QUERY", "VALIDATION", "DASHBOARD", "REVIEW"])
    parser.add_argument("--sql-file", help="Existing SQL file to classify")
    parser.add_argument("--intent", default="", help="User intent text for generation preflight when SQL is not available")
    parser.add_argument("--source-table-count", type=int, default=0, help="Expected source table count for generation preflight")
    parser.add_argument("--expected-time-days", type=int, help="Expected scan window in days for generation preflight")
    parser.add_argument("--reusable", action="store_true", help="Mark planned SQL as reusable/current artifact")
    parser.add_argument("--intermediate-candidate", action="store_true", help="Mark SQL as an intermediate-table candidate")
    parser.add_argument("--execution-error", default="", help="Optional executor error text to classify against declared CTEs")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--quiet", action="store_true", help="Reserved for future non-JSON logs; JSON output is unchanged")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config_path = Path(args.project_config).resolve() if args.project_config else None
    project_root = Path(args.project_root).resolve() if args.project_root else None
    config = read_json_file(config_path or project_root)
    sql = None
    if args.sql_file:
        sql_path = Path(args.sql_file).resolve()
        try:
            sql = sql_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            sql = sql_path.read_text(encoding="gb18030")
    result = analyze_performance(
        sql=sql,
        project_config=config,
        mode=args.mode,
        artifact_kind=args.artifact_kind,
        intent=args.intent,
        source_table_count=max(0, args.source_table_count),
        expected_time_days=args.expected_time_days,
        reusable=args.reusable,
        intermediate_candidate=args.intermediate_candidate,
        execution_error=args.execution_error,
    )
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result), end="")
    return 1 if result["status"] == "block" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        error = {
            "status": "error",
            "tier": TIER_L3,
            "score": 0,
            "triggers": [],
            "required_references": [ROUTING_REFERENCE],
            "required_checks": [],
            "blockers": [str(exc)],
            "optimization_hint": "performance_preflight runtime error",
        }
        print(json.dumps(error, ensure_ascii=False, indent=2))
        raise SystemExit(3)
