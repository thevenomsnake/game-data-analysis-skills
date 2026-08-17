#!/usr/bin/env python3
"""Apply and validate executor-specific SQL identifier policies."""

from __future__ import annotations

from typing import Any, Iterator


def identifier_policy(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("identifier_policy")
    return value if isinstance(value, dict) else {}


def required_fields(config: dict[str, Any]) -> list[str]:
    rows = identifier_policy(config).get("case_sensitive_fields")
    if not isinstance(rows, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in rows:
        field = str(item or "").strip()
        if field and field.lower() not in seen:
            seen.add(field.lower())
            output.append(field)
    return output


def _environment_text(config: dict[str, Any]) -> str:
    environment = config.get("query_environment")
    if isinstance(environment, dict):
        environment = " ".join(str(value) for value in environment.values())
    return f"{config.get('query_engine', '')} {environment or ''}".lower()


def config_problems(config: dict[str, Any], *, label: str = "identifier_policy") -> list[str]:
    policy = identifier_policy(config)
    problems: list[str] = []
    fields = required_fields(config)
    if policy:
        if policy.get("quote_style") != "backtick":
            problems.append(f"{label}.quote_style must be backtick.")
        raw_fields = policy.get("case_sensitive_fields")
        if not isinstance(raw_fields, list) or not fields:
            problems.append(f"{label}.case_sensitive_fields requires at least one field.")
        elif len(fields) != len(raw_fields):
            problems.append(f"{label}.case_sensitive_fields must be non-empty and case-insensitively unique.")
    policy_config = config.get("partition_policy")
    policy_config = policy_config if isinstance(policy_config, dict) else {}
    business_time_field = str(policy_config.get("business_time_field") or "").strip()
    is_tdbank_hive = config.get("sql_dialect") == "Hive" and "tdbank" in _environment_text(config)
    if is_tdbank_hive and business_time_field and any(char.isupper() for char in business_time_field):
        if business_time_field not in fields:
            problems.append(
                f"{label}.case_sensitive_fields must include exact business_time_field {business_time_field!r} for the TDBank Hive execution chain."
            )
    return problems


def _identifier_tokens(sql: str) -> Iterator[tuple[int, int, str, bool]]:
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""
        if char == "-" and nxt == "-":
            end = sql.find("\n", index + 2)
            index = length if end < 0 else end + 1
            continue
        if char == "#":
            end = sql.find("\n", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "/" and nxt == "*":
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            while index < length:
                if sql[index] == "\\" and index + 1 < length:
                    index += 2
                    continue
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == "`":
            start = index
            index += 1
            value_start = index
            while index < length and sql[index] != "`":
                index += 1
            if index < length:
                yield start, index + 1, sql[value_start:index], True
                index += 1
            continue
        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < length and (sql[index].isalnum() or sql[index] in {"_", "$"}):
                index += 1
            yield start, index, sql[start:index], False
            continue
        index += 1


def quote_required_identifiers(sql: str, config: dict[str, Any]) -> str:
    if identifier_policy(config).get("quote_style") != "backtick":
        return sql
    exact = set(required_fields(config))
    replacements = [
        (start, end, f"`{token}`")
        for start, end, token, quoted in _identifier_tokens(sql)
        if not quoted and token in exact
    ]
    output = sql
    for start, end, value in reversed(replacements):
        output = output[:start] + value + output[end:]
    return output


def policy_findings(sql: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    expected_by_lower = {field.lower(): field for field in required_fields(config)}
    findings: dict[tuple[str, str], dict[str, Any]] = {}
    for _start, _end, token, quoted in _identifier_tokens(sql):
        expected = expected_by_lower.get(token.lower())
        if not expected:
            continue
        if token != expected:
            key = ("case_mismatched_identifier", expected)
            findings[key] = {
                "code": key[0],
                "field": expected,
                "actual": token,
                "message": f"字段 {token} 大小写与执行契约中的 {expected} 不一致。",
            }
        elif not quoted:
            key = ("unquoted_case_sensitive_identifier", expected)
            findings[key] = {
                "code": key[0],
                "field": expected,
                "actual": token,
                "message": f"字段 {expected} 在当前执行链中区分大小写，必须写为 `{expected}`。",
            }
    return list(findings.values())
