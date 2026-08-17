#!/usr/bin/env python3
"""Result-file output contracts and safe final SELECT pruning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sql_facts import (
    FinalSelectProjection,
    _keyword_at,
    _top_level_keyword_positions,
    final_select_field_aliases,
    final_select_projection,
    select_expression_alias,
    split_top_level_csv,
    unique_in_order,
)


def normalize_field_name(value: Any) -> str:
    text = str(value or "").strip().strip("`").strip('"').strip("'")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class CteProjection:
    name: str
    body_start: int
    body_end: int
    select_start: int
    select_end: int
    expressions: list[str]
    cte_start: int = 0
    cte_end: int = 0
    cte_end_with_separator: int = 0


def _skip_ws_and_comments(sql: str, index: int) -> int:
    i = index
    while i < len(sql):
        if sql[i].isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            newline = sql.find("\n", i + 2)
            i = len(sql) if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = len(sql) if end == -1 else end + 2
            continue
        break
    return i


def _parse_sql_identifier(sql: str, index: int) -> tuple[str, int] | None:
    i = _skip_ws_and_comments(sql, index)
    if i >= len(sql):
        return None
    ch = sql[i]
    if ch in {'`', '"'}:
        end = sql.find(ch, i + 1)
        if end == -1:
            return None
        return sql[i + 1 : end], end + 1
    if ch == "[":
        end = sql.find("]", i + 1)
        if end == -1:
            return None
        return sql[i + 1 : end], end + 1
    match = re.match(r"[A-Za-z_\u4e00-\u9fff][\w$\u4e00-\u9fff]*", sql[i:])
    if not match:
        return None
    return match.group(0), i + len(match.group(0))


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


def _projection_is_distinct(sql: str, projection: FinalSelectProjection) -> bool:
    return bool(re.match(r"\s*distinct\b", sql[projection.start : projection.end], flags=re.I))


def _cte_projections(sql: str) -> list[CteProjection]:
    with_positions = _top_level_keyword_positions(sql, "with")
    if not with_positions:
        return []
    _, with_end = with_positions[0]
    i = _skip_ws_and_comments(sql, with_end)
    if _keyword_at(sql, i, "recursive"):
        i = _skip_ws_and_comments(sql, i + len("recursive"))

    ctes: list[CteProjection] = []
    while i < len(sql):
        cte_start = i
        parsed = _parse_sql_identifier(sql, i)
        if not parsed:
            break
        name, i = parsed
        i = _skip_ws_and_comments(sql, i)
        if i < len(sql) and sql[i] == "(":
            close_columns = _find_matching_paren(sql, i)
            if close_columns == -1:
                break
            i = _skip_ws_and_comments(sql, close_columns + 1)
        if not _keyword_at(sql, i, "as"):
            break
        i = _skip_ws_and_comments(sql, i + len("as"))
        if i >= len(sql) or sql[i] != "(":
            break
        close_body = _find_matching_paren(sql, i)
        if close_body == -1:
            break
        body_start = i + 1
        body_end = close_body
        cte_end = close_body + 1
        separator_start = _skip_ws_and_comments(sql, cte_end)
        cte_end_with_separator = cte_end
        if separator_start < len(sql) and sql[separator_start] == ",":
            cte_end_with_separator = _skip_ws_and_comments(sql, separator_start + 1)
        body = sql[body_start:body_end]
        top_selects = _top_level_keyword_positions(body, "select")
        has_union = bool(_top_level_keyword_positions(body, "union"))
        projection = final_select_projection(body) if len(top_selects) == 1 and not has_union else None
        if projection:
            ctes.append(
                CteProjection(
                    name=name,
                    body_start=body_start,
                    body_end=body_end,
                    select_start=body_start + projection.start,
                    select_end=body_start + projection.end,
                    expressions=projection.expressions,
                    cte_start=cte_start,
                    cte_end=cte_end,
                    cte_end_with_separator=cte_end_with_separator,
                )
            )
        i = _skip_ws_and_comments(sql, close_body + 1)
        if i < len(sql) and sql[i] == ",":
            i = _skip_ws_and_comments(sql, i + 1)
            continue
        break
    return ctes


def _contains_identifier(text: str, identifier: str) -> bool:
    if not identifier:
        return False
    escaped = re.escape(identifier)
    quoted = re.compile(rf"`{escaped}`|\"{escaped}\"|\[{escaped}\]", flags=re.I)
    if quoted.search(text):
        return True
    boundary = re.compile(rf"(?<![\w\u4e00-\u9fff]){escaped}(?![\w\u4e00-\u9fff])", flags=re.I)
    return bool(boundary.search(text))


def _is_star_projection(expression: str) -> bool:
    expr = expression.strip()
    return expr == "*" or bool(re.fullmatch(r"(?:`[^`]+`|[A-Za-z_][\w$]*|\[[^\]]+\])\.\*", expr))


def _cte_prune_once(sql: str) -> tuple[str, list[dict[str, Any]]]:
    replacements: list[tuple[int, int, str]] = []
    removed: list[dict[str, Any]] = []
    for cte in reversed(_cte_projections(sql)):
        if normalize_field_name(cte.name) == "params":
            continue
        if _projection_is_distinct(sql, FinalSelectProjection(cte.select_start, cte.select_end, cte.expressions)):
            continue
        downstream = sql[cte.body_end + 1 :]
        cte_tail = sql[cte.select_end : cte.body_end]
        kept: list[str] = []
        removed_here: list[dict[str, Any]] = []
        for expression in cte.expressions:
            alias = select_expression_alias(expression)
            if not alias or _is_star_projection(expression):
                kept.append(expression)
                continue
            if _contains_identifier(cte_tail, alias) or _contains_identifier(downstream, alias):
                kept.append(expression)
                continue
            removed_here.append({"cte": cte.name, "field": alias, "expression": expression.strip()})
        if removed_here and kept:
            replacement = "\n        " + ",\n        ".join(kept) + "\n    "
            replacements.append((cte.select_start, cte.select_end, replacement))
            removed.extend(removed_here)
    if not replacements:
        return sql, []
    new_sql = sql
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        new_sql = new_sql[:start] + replacement + new_sql[end:]
    return new_sql, removed



def _normalized_expression_key(expression: str) -> str:
    expr = re.sub(r"\s+", " ", str(expression or "").strip().strip("`\"[]"))
    expr = re.sub(r"\s+", " ", expr)
    if re.fullmatch(r"[A-Za-z_][\w$]*\.(?:`[^`]+`|[A-Za-z_][\w$]*|\[[^\]]+\])", expr):
        expr = expr.split(".", 1)[1]
    return normalize_field_name(expr)


def _expression_without_alias(expression: str) -> str:
    return re.sub(
        r"\s+as\s+(?:`[^`]+`|\"[^\"]+\"|'[^']+'|\[[^\]]+\]|[^\s,]+)\s*$",
        "",
        expression.strip(),
        flags=re.I,
    ).strip()


def _top_level_clause_start(sql: str, keywords: list[str], *, start: int) -> int:
    positions: list[int] = []
    for keyword in keywords:
        positions.extend(pos for pos, _ in _top_level_keyword_positions(sql, keyword, start=start))
    return min(positions) if positions else len(sql)


def _quote_identifier(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def _order_by_expression_without_direction(expression: str) -> str:
    expr = str(expression or "").strip().rstrip(";").strip()
    changed = True
    while changed:
        changed = False
        updated = re.sub(r"\s+nulls\s+(?:first|last)\s*$", "", expr, flags=re.I).strip()
        if updated != expr:
            expr = updated
            changed = True
        updated = re.sub(r"\s+(?:asc|desc)\s*$", "", expr, flags=re.I).strip()
        if updated != expr:
            expr = updated
            changed = True
    return expr


def _direct_identifier_key(expression: str) -> str:
    parsed = _parse_sql_identifier(expression, 0)
    if not parsed:
        return ""
    identifier, index = parsed
    if _skip_ws_and_comments(expression, index) != len(expression):
        return ""
    return normalize_field_name(identifier)


def _replace_identifier_references(fragment: str, identifier: str, replacement: str) -> tuple[str, int]:
    target = normalize_field_name(identifier)
    if not target or not replacement.strip():
        return fragment, 0
    output: list[str] = []
    replacements = 0
    i = 0
    while i < len(fragment):
        ch = fragment[i]
        nxt = fragment[i + 1] if i + 1 < len(fragment) else ""
        if ch == "-" and nxt == "-":
            newline = fragment.find("\n", i + 2)
            end = len(fragment) if newline == -1 else newline + 1
            output.append(fragment[i:end])
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = fragment.find("*/", i + 2)
            end = len(fragment) if end == -1 else end + 2
            output.append(fragment[i:end])
            i = end
            continue
        if ch == "'":
            start = i
            i += 1
            while i < len(fragment):
                if fragment[i] == "'" and i + 1 < len(fragment) and fragment[i + 1] == "'":
                    i += 2
                    continue
                if fragment[i] == "'":
                    i += 1
                    break
                i += 1
            output.append(fragment[start:i])
            continue
        if ch in {'`', '"'}:
            end = fragment.find(ch, i + 1)
            if end == -1:
                output.append(fragment[i:])
                break
            content = fragment[i + 1 : end]
            if normalize_field_name(content) == target:
                output.append(f"({replacement})")
                replacements += 1
            else:
                output.append(fragment[i : end + 1])
            i = end + 1
            continue
        if ch == "[":
            end = fragment.find("]", i + 1)
            if end == -1:
                output.append(fragment[i:])
                break
            content = fragment[i + 1 : end]
            if normalize_field_name(content) == target:
                output.append(f"({replacement})")
                replacements += 1
            else:
                output.append(fragment[i : end + 1])
            i = end + 1
            continue
        match = re.match(r"[A-Za-z_\u4e00-\u9fff][\w$\u4e00-\u9fff]*", fragment[i:])
        if match:
            token = match.group(0)
            if normalize_field_name(token) == target:
                output.append(f"({replacement})")
                replacements += 1
            else:
                output.append(token)
            i += len(token)
            continue
        output.append(ch)
        i += 1
    return "".join(output), replacements


def _rewrite_final_having_removed_aliases(
    sql: str,
    removed_alias_expressions: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    report: dict[str, Any] = {
        "final_having_rewritten": False,
        "final_having_rewritten_aliases": [],
        "final_having_note": "No final HAVING aliases referenced removed output fields.",
    }
    if not removed_alias_expressions:
        return sql, report
    having_positions = _top_level_keyword_positions(sql, "having")
    if not having_positions:
        return sql, report
    _, having_end = having_positions[-1]
    fragment_start = _skip_ws_and_comments(sql, having_end)
    fragment_end = _top_level_clause_start(sql, ["order", "limit", "qualify", "union"], start=fragment_start)
    fragment = sql[fragment_start:fragment_end]
    rewritten = fragment
    rows: list[dict[str, Any]] = []
    for alias, expression in removed_alias_expressions.items():
        raw_expression = _expression_without_alias(expression)
        if not raw_expression or _is_star_projection(raw_expression):
            continue
        rewritten, count = _replace_identifier_references(rewritten, alias, raw_expression)
        if count:
            rows.append(
                {
                    "alias": alias,
                    "expression": raw_expression,
                    "references_rewritten": count,
                    "reason": "removed_output_alias_retained_as_having_filter_expression",
                }
            )
    if not rows:
        return sql, report
    report.update(
        {
            "final_having_rewritten": True,
            "final_having_rewritten_aliases": rows,
            "final_having_note": "Rewrote final HAVING references to output aliases removed by the result-file contract back to their original expressions, preserving the row filter while keeping the final output fields aligned to the result file.",
        }
    )
    return sql[:fragment_start] + rewritten + sql[fragment_end:], report


def _prune_final_order_by_removed_fields(
    sql: str,
    removed_fields: list[str],
    *,
    field_order_before: list[str] | None = None,
    field_order_after: list[str] | None = None,
    removed_alias_expressions: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    removed_norms = {normalize_field_name(field) for field in removed_fields if str(field or "").strip()}
    alias_expression_by_norm = {
        normalize_field_name(alias): _expression_without_alias(expression)
        for alias, expression in (removed_alias_expressions or {}).items()
        if str(alias or "").strip() and str(expression or "").strip()
    }
    before_fields = [str(item or "").strip() for item in (field_order_before or []) if str(item or "").strip()]
    after_fields = [str(item or "").strip() for item in (field_order_after or []) if str(item or "").strip()]
    after_position_by_norm = {normalize_field_name(field): index for index, field in enumerate(after_fields, start=1)}
    report: dict[str, Any] = {
        "final_order_by_pruned": False,
        "final_order_by_removed_terms": [],
        "final_order_by_rewritten_terms": [],
        "final_order_by_note": "No final ORDER BY terms referenced removed output fields.",
    }
    if not removed_norms and not (before_fields and after_fields) and not alias_expression_by_norm:
        return sql, report
    order_positions = _top_level_keyword_positions(sql, "order")
    if not order_positions:
        return sql, report
    order_start, order_end = order_positions[-1]
    by_start = _skip_ws_and_comments(sql, order_end)
    if not _keyword_at(sql, by_start, "by"):
        return sql, report
    expr_start = _skip_ws_and_comments(sql, by_start + len("by"))
    expr_end = _top_level_clause_start(sql, ["limit", "qualify", "union"], start=expr_start)
    terms = [term.strip() for term in split_top_level_csv(sql[expr_start:expr_end]) if term.strip()]
    if not terms:
        return sql, report
    kept: list[str] = []
    removed: list[str] = []
    rewritten: list[dict[str, Any]] = []
    for term in terms:
        order_expression = _order_by_expression_without_direction(term)
        key = _direct_identifier_key(order_expression)
        if key and key in removed_norms:
            removed.append(term)
            continue
        if re.fullmatch(r"[1-9]\d*", order_expression):
            ordinal = int(order_expression)
            if 1 <= ordinal <= len(before_fields):
                field = before_fields[ordinal - 1]
                field_norm = normalize_field_name(field)
                if field_norm in removed_norms:
                    removed.append(term)
                    continue
                new_ordinal = after_position_by_norm.get(field_norm)
                if new_ordinal and new_ordinal != ordinal:
                    rewritten_term = str(new_ordinal) + term[len(order_expression) :]
                    kept.append(rewritten_term)
                    rewritten.append(
                        {
                            "from": term,
                            "to": rewritten_term,
                            "field": field,
                            "reason": "retained_output_field_position_changed",
                        }
                    )
                    continue
        rewritten_term = term
        rewritten_aliases: list[dict[str, Any]] = []
        for alias_norm in sorted(removed_norms & set(alias_expression_by_norm)):
            expression = alias_expression_by_norm[alias_norm]
            alias = next((field for field in removed_fields if normalize_field_name(field) == alias_norm), alias_norm)
            rewritten_term, count = _replace_identifier_references(rewritten_term, alias, expression)
            if count:
                rewritten_aliases.append(
                    {
                        "alias": alias,
                        "expression": expression,
                        "references_rewritten": count,
                    }
                )
        if rewritten_aliases:
            kept.append(rewritten_term)
            rewritten.append(
                {
                    "from": term,
                    "to": rewritten_term,
                    "aliases": rewritten_aliases,
                    "reason": "removed_output_alias_rewritten_inside_order_expression",
                }
            )
            continue
        kept.append(term)
    if not removed and not rewritten:
        return sql, report
    if kept:
        replacement = ", ".join(kept) + "\n"
        new_sql = sql[:expr_start] + replacement + sql[expr_end:]
    else:
        new_sql = sql[:order_start].rstrip() + "\n" + sql[expr_end:].lstrip()
    report.update(
        {
            "final_order_by_pruned": True,
            "final_order_by_removed_terms": removed,
            "final_order_by_rewritten_terms": rewritten,
            "final_order_by_note": "Removed final ORDER BY terms that directly referenced output fields removed by the result-file contract, remapped ordinal terms that still refer to retained fields after output reordering, and rewrote complex ORDER BY expressions that referenced removed aliases back to their original expressions.",
        }
    )
    return new_sql, report


def _star_projection_owner(expression: str) -> str | None:
    expr = expression.strip()
    if expr == "*":
        return ""
    match = re.fullmatch(
        r"(?:`([^`]+)`|\"([^\"]+)\"|\[([^\]]+)\]|([A-Za-z_\u4e00-\u9fff][\w$\u4e00-\u9fff]*))\.\*",
        expr,
    )
    if not match:
        return None
    return next(group for group in match.groups() if group)


def _final_from_simple_cte(sql: str, projection: FinalSelectProjection) -> tuple[str, str] | None:
    froms = _top_level_keyword_positions(sql, "from", start=projection.end)
    if not froms:
        return None
    _, from_end = froms[0]
    clause_end = _top_level_clause_start(sql, ["where", "group", "having", "order", "limit", "qualify", "union"], start=from_end)
    parsed_source = _parse_sql_identifier(sql, from_end)
    if not parsed_source:
        return None
    source, index = parsed_source
    index = _skip_ws_and_comments(sql, index)
    if index < clause_end and sql[index] == ".":
        return None
    alias = source
    if index < clause_end and _keyword_at(sql, index, "as"):
        parsed_alias = _parse_sql_identifier(sql, index + len("as"))
        if parsed_alias and parsed_alias[1] <= clause_end:
            alias, index = parsed_alias
    elif index < clause_end and not any(_keyword_at(sql, index, keyword) for keyword in ["where", "group", "having", "order", "limit", "qualify", "union"]):
        parsed_alias = _parse_sql_identifier(sql, index)
        if parsed_alias and parsed_alias[1] <= clause_end:
            alias, index = parsed_alias
    tail = sql[_skip_ws_and_comments(sql, index) : clause_end].strip()
    if tail.rstrip(";").strip():
        return None
    return source, alias


def _group_by_expressions(body: str) -> list[str]:
    for group_start, group_end in _top_level_keyword_positions(body, "group"):
        by_start = _skip_ws_and_comments(body, group_end)
        if not _keyword_at(body, by_start, "by"):
            continue
        expr_start = _skip_ws_and_comments(body, by_start + len("by"))
        expr_end = _top_level_clause_start(body, ["having", "order", "limit", "qualify", "union"], start=expr_start)
        return [item.strip() for item in split_top_level_csv(body[expr_start:expr_end]) if item.strip()]
    return []


def _distinct_projection_key_aliases(cte: CteProjection) -> set[str]:
    aliases: set[str] = set()
    for index, expression in enumerate(cte.expressions):
        expr = expression
        if index == 0:
            expr = re.sub(r"^\s*distinct\b\s*", "", expr, flags=re.I)
        if not expr.strip() or _is_star_projection(expr):
            return set()
        alias = select_expression_alias(expr)
        if not alias:
            return set()
        aliases.add(normalize_field_name(alias))
    return aliases


def _cte_unique_key_aliases(sql: str) -> dict[str, dict[str, Any]]:
    keys_by_cte: dict[str, dict[str, Any]] = {}
    for cte in _cte_projections(sql):
        cte_key = normalize_field_name(cte.name)
        body = sql[cte.body_start : cte.body_end]
        group_items = _group_by_expressions(body)
        group_keys = {_normalized_expression_key(item) for item in group_items}
        group_ordinals = {int(item) for item in group_items if re.fullmatch(r"\d+", item.strip())}
        if group_keys or group_ordinals:
            aliases: set[str] = set()
            for ordinal, expression in enumerate(cte.expressions, start=1):
                alias = select_expression_alias(expression)
                if not alias:
                    continue
                alias_key = normalize_field_name(alias)
                base_key = _normalized_expression_key(_expression_without_alias(expression))
                if alias_key in group_keys or base_key in group_keys or ordinal in group_ordinals:
                    aliases.add(alias_key)
            if aliases:
                keys_by_cte[cte_key] = {"keys": aliases, "proof": "group_by"}
        if _projection_is_distinct(sql, FinalSelectProjection(cte.select_start, cte.select_end, cte.expressions)):
            distinct_aliases = _distinct_projection_key_aliases(cte)
            if distinct_aliases and cte_key not in keys_by_cte:
                keys_by_cte[cte_key] = {"keys": distinct_aliases, "proof": "distinct_projection"}
    return keys_by_cte


def _join_clause_start(body: str, join_start: int) -> int:
    prefix_start = max(0, join_start - 64)
    prefix = body[prefix_start:join_start]
    match = re.search(r"\b(?:left\s+(?:outer\s+)?|right\s+(?:outer\s+)?|full\s+(?:outer\s+)?|inner\s+|cross\s+)?$", prefix, flags=re.I)
    if not match or not match.group(0).strip():
        return join_start
    return prefix_start + match.start()


def _left_join_start(body: str, join_start: int) -> int | None:
    prefix_start = max(0, join_start - 64)
    prefix = body[prefix_start:join_start]
    match = re.search(r"\bleft\s+(?:outer\s+)?$", prefix, flags=re.I)
    if not match:
        return None
    return prefix_start + match.start()


def _next_join_or_clause_start(body: str, *, start: int) -> int:
    candidates = [len(body)]
    for keyword in ["where", "group", "having", "order", "limit", "qualify", "union"]:
        candidates.extend(pos for pos, _ in _top_level_keyword_positions(body, keyword, start=start))
    for join_pos, _ in _top_level_keyword_positions(body, "join", start=start):
        candidates.append(_join_clause_start(body, join_pos))
    return min(pos for pos in candidates if pos >= start)


def _parse_join_source_alias(body: str, join_keyword_end: int, segment_end: int) -> tuple[str, str, int] | None:
    source_start = _skip_ws_and_comments(body, join_keyword_end)
    if source_start >= segment_end or body[source_start] == "(":
        return None
    parsed_source = _parse_sql_identifier(body, source_start)
    if not parsed_source:
        return None
    source, index = parsed_source
    index = _skip_ws_and_comments(body, index)
    if index < segment_end and body[index] == ".":
        return None
    alias = source
    if _keyword_at(body, index, "as"):
        parsed_alias = _parse_sql_identifier(body, index + len("as"))
        if parsed_alias:
            alias, index = parsed_alias
    else:
        parsed_alias = _parse_sql_identifier(body, index)
        if parsed_alias:
            maybe_alias, alias_end = parsed_alias
            if not _keyword_at(body, index, "on") and alias_end <= segment_end:
                alias, index = maybe_alias, alias_end
    on_positions = _top_level_keyword_positions(body, "on", start=index)
    on_positions = [item for item in on_positions if item[0] < segment_end]
    if not on_positions:
        return None
    return source, alias, on_positions[0][1]


def _alias_field_refs(text: str, alias: str) -> set[str]:
    if not alias:
        return set()
    escaped = re.escape(alias)
    pattern = re.compile(
        rf"(?<![\w\u4e00-\u9fff])(?:`{escaped}`|{escaped})\s*\.\s*(?:`([^`]+)`|\"([^\"]+)\"|\[([^\]]+)\]|([A-Za-z_\u4e00-\u9fff][\w$\u4e00-\u9fff]*))",
        flags=re.I,
    )
    refs: set[str] = set()
    for match in pattern.finditer(text):
        refs.add(normalize_field_name(next(group for group in match.groups() if group)))
    return refs


def _cte_left_join_prune_once(sql: str) -> tuple[str, list[dict[str, Any]]]:
    unique_keys = _cte_unique_key_aliases(sql)
    replacements: list[tuple[int, int, str]] = []
    removed: list[dict[str, Any]] = []
    for cte in reversed(_cte_projections(sql)):
        body = sql[cte.body_start : cte.body_end]
        for join_pos, join_end in reversed(_top_level_keyword_positions(body, "join")):
            local_start = _left_join_start(body, join_pos)
            if local_start is None:
                continue
            local_end = _next_join_or_clause_start(body, start=join_end)
            parsed = _parse_join_source_alias(body, join_end, local_end)
            if not parsed:
                continue
            source, alias, on_end = parsed
            source_unique = unique_keys.get(normalize_field_name(source), {})
            source_keys = source_unique.get("keys", set()) if isinstance(source_unique, dict) else set()
            if not source_keys:
                continue
            segment = body[local_start:local_end]
            body_without_segment = body[:local_start] + body[local_end:]
            if _contains_identifier(body_without_segment, alias):
                continue
            right_refs = _alias_field_refs(body[on_end:local_end], alias)
            if not source_keys.issubset(right_refs):
                continue
            abs_start = cte.body_start + local_start
            abs_end = cte.body_start + local_end
            replacements.append((abs_start, abs_end, ""))
            removed.append(
                {
                    "cte": cte.name,
                    "join_source": source,
                    "join_alias": alias,
                    "unique_keys": sorted(source_keys),
                    "unique_proof": source_unique.get("proof", "") if isinstance(source_unique, dict) else "",
                    "reason": "unused_left_join_unique_by_join_key",
                }
            )
    if not replacements:
        return sql, []
    new_sql = sql
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        new_sql = new_sql[:start] + replacement + new_sql[end:]
    return new_sql, removed


def _unused_cte_prune_once(sql: str) -> tuple[str, list[dict[str, Any]]]:
    ctes = _cte_projections(sql)
    if not ctes:
        return sql, []
    replacements: list[tuple[int, int, str]] = []
    removed: list[dict[str, Any]] = []
    for index, cte in reversed(list(enumerate(ctes))):
        if normalize_field_name(cte.name) == "params":
            continue
        downstream = sql[cte.body_end + 1 :]
        if _contains_identifier(downstream, cte.name):
            continue
        if len(ctes) == 1:
            with_positions = _top_level_keyword_positions(sql, "with")
            if not with_positions:
                continue
            start, end = with_positions[0][0], cte.cte_end
        elif index == 0:
            start, end = cte.cte_start, cte.cte_end_with_separator
        else:
            start, end = ctes[index - 1].cte_end, cte.cte_end_with_separator if cte.cte_end_with_separator > cte.cte_end else cte.cte_end
        if start >= end:
            continue
        replacements.append((start, end, ""))
        removed.append({"cte": cte.name, "reason": "unused_after_output_contract_pruning"})
    if not replacements:
        return sql, []
    new_sql = sql
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        new_sql = new_sql[:start] + replacement + new_sql[end:]
    return new_sql, removed


def prune_internal_cte_outputs(sql: str, *, max_iterations: int = 6) -> tuple[str, dict[str, Any]]:
    current = sql
    removed_fields: list[dict[str, Any]] = []
    removed_joins: list[dict[str, Any]] = []
    removed_ctes: list[dict[str, Any]] = []
    iterations = 0
    for _ in range(max_iterations):
        changed = False
        next_sql, removed_once = _cte_prune_once(current)
        if removed_once:
            current = next_sql
            removed_fields.extend(removed_once)
            changed = True
        next_sql, removed_join_once = _cte_left_join_prune_once(current)
        if removed_join_once:
            current = next_sql
            removed_joins.extend(removed_join_once)
            changed = True
        next_sql, removed_cte_once = _unused_cte_prune_once(current)
        if removed_cte_once:
            current = next_sql
            removed_ctes.extend(removed_cte_once)
            changed = True
        if not changed:
            break
        iterations += 1
    if removed_fields or removed_joins or removed_ctes:
        return current, {
            "internal_pruning_status": "pruned",
            "internal_pruning_removed_fields": removed_fields,
            "internal_pruning_removed_joins": removed_joins,
            "internal_pruning_removed_ctes": removed_ctes,
            "internal_pruning_iterations": iterations,
            "internal_pruning_note": "Safely removed unused CTE output expressions after applying the result-file output contract. Also removed only LEFT JOINs whose right side is a grouped CTE unique by the JOIN key and whose alias is otherwise unused, then removed CTEs proven unreferenced by downstream SQL. WHERE/GROUP BY semantics were not rewritten.",
        }
    return current, {
        "internal_pruning_status": "no_safe_prune",
        "internal_pruning_removed_fields": [],
        "internal_pruning_removed_joins": [],
        "internal_pruning_removed_ctes": [],
        "internal_pruning_iterations": 0,
        "internal_pruning_note": "No unused CTE output aliases, safe unique-key LEFT JOINs, or downstream-unreferenced CTEs could be proven safe to remove.",
    }


def build_output_field_contract(
    *,
    sql: str,
    result_columns: list[str],
    pruned: bool = False,
    pruning_reason: str = "",
    missing_result_fields: list[str] | None = None,
) -> dict[str, Any]:
    sql_fields = final_select_field_aliases(sql)
    result_fields = [str(item or "").strip() for item in result_columns if str(item or "").strip()]
    sql_by_norm = {normalize_field_name(field): field for field in sql_fields}
    result_norms = [normalize_field_name(field) for field in result_fields]
    retained = [field for field in result_fields if normalize_field_name(field) in sql_by_norm or not sql_fields]
    removed = [field for field in sql_fields if normalize_field_name(field) not in set(result_norms)]
    missing = missing_result_fields if missing_result_fields is not None else [field for field in result_fields if normalize_field_name(field) not in sql_by_norm]
    if not result_fields:
        status = "no_result_fields"
    elif missing:
        status = "mismatch"
    elif pruned:
        status = "pruned"
    elif sql_fields == result_fields:
        status = "matched"
    elif sql_fields and [normalize_field_name(item) for item in sql_fields] == result_norms:
        status = "matched_case_or_quote_diff"
    else:
        status = "result_contract_recorded"
    return {
        "schema_version": "result_output_contract_v1",
        "status": status,
        "source": "result_file_columns",
        "result_fields": result_fields,
        "retained_fields": retained or result_fields,
        "sql_final_fields_before_prune": sql_fields,
        "removed_output_fields": removed,
        "missing_result_fields": missing,
        "final_select_pruned": pruned,
        "final_order_by_pruned": False,
        "final_order_by_removed_terms": [],
        "final_order_by_rewritten_terms": [],
        "final_order_by_note": "Final ORDER BY cleanup has not removed any terms.",
        "final_having_rewritten": False,
        "final_having_rewritten_aliases": [],
        "final_having_note": "Final HAVING alias rewrite has not changed any terms.",
        "pruning_reason": pruning_reason,
        "internal_pruning_status": "not_attempted",
        "internal_pruning_note": "Internal CTE output pruning has not run yet for this contract. Fast formalization updates this after the safe downstream-alias pass when retained result fields are available.",
    }


def _expand_final_select_star_to_result_columns(
    sql: str,
    projection: FinalSelectProjection,
    result_fields: list[str],
) -> tuple[str, dict[str, Any]] | None:
    if len(projection.expressions) != 1:
        return None
    star_owner = _star_projection_owner(projection.expressions[0])
    if star_owner is None:
        return None
    source_info = _final_from_simple_cte(sql, projection)
    if not source_info:
        return None
    source, source_alias = source_info
    if star_owner and normalize_field_name(star_owner) not in {normalize_field_name(source), normalize_field_name(source_alias)}:
        return None
    cte = next((item for item in _cte_projections(sql) if normalize_field_name(item.name) == normalize_field_name(source)), None)
    if not cte:
        return None
    source_fields = [alias for alias in (select_expression_alias(expression) for expression in cte.expressions) if alias]
    if not source_fields:
        return None
    source_by_norm = {normalize_field_name(field): field for field in source_fields}
    missing = [field for field in result_fields if normalize_field_name(field) not in source_by_norm]
    if missing:
        contract = build_output_field_contract(
            sql=sql,
            result_columns=result_fields,
            missing_result_fields=missing,
            pruning_reason="Final SELECT * source CTE does not expose every result column.",
        )
        contract["sql_final_fields_before_prune"] = source_fields
        return sql, contract

    retained_expressions = []
    for field in result_fields:
        source_field = source_by_norm[normalize_field_name(field)]
        if source_field == field:
            retained_expressions.append(_quote_identifier(source_field))
        else:
            retained_expressions.append(f"{_quote_identifier(source_field)} AS {_quote_identifier(field)}")
    replacement = "\n    " + ",\n    ".join(retained_expressions) + "\n"
    expanded_sql = sql[: projection.start] + replacement + sql[projection.end :]
    contract = build_output_field_contract(
        sql=expanded_sql,
        result_columns=result_fields,
        pruned=True,
        pruning_reason="Final SELECT * was expanded from its source CTE to match the result-file output contract.",
    )
    result_norms = {normalize_field_name(field) for field in result_fields}
    contract["sql_final_fields_before_prune"] = source_fields
    contract["removed_output_fields"] = [field for field in source_fields if normalize_field_name(field) not in result_norms]
    expanded_sql, order_report = _prune_final_order_by_removed_fields(
        expanded_sql,
        contract["removed_output_fields"],
        field_order_before=source_fields,
        field_order_after=result_fields,
        removed_alias_expressions={
            alias: expression
            for expression in cte.expressions
            for alias in [select_expression_alias(expression)]
            if alias and normalize_field_name(alias) in {normalize_field_name(field) for field in contract["removed_output_fields"]}
        },
    )
    contract.update(order_report)
    return expanded_sql, contract

def prune_final_select_to_result_columns(sql: str, result_columns: list[str]) -> tuple[str, dict[str, Any]]:
    result_fields = [str(item or "").strip() for item in result_columns if str(item or "").strip()]
    if not result_fields:
        return sql, build_output_field_contract(sql=sql, result_columns=result_fields)
    projection = final_select_projection(sql)
    if not projection:
        contract = build_output_field_contract(
            sql=sql,
            result_columns=result_fields,
            missing_result_fields=result_fields,
            pruning_reason="No parseable final SELECT projection.",
        )
        return sql, contract

    star_expansion = _expand_final_select_star_to_result_columns(sql, projection, result_fields)
    if star_expansion:
        return star_expansion

    expression_by_alias: dict[str, str] = {}
    ordered_aliases: list[str] = []
    for expression in projection.expressions:
        alias = select_expression_alias(expression)
        if not alias:
            continue
        key = normalize_field_name(alias)
        if key not in expression_by_alias:
            expression_by_alias[key] = expression.strip()
            ordered_aliases.append(alias)

    missing = [field for field in result_fields if normalize_field_name(field) not in expression_by_alias]
    if missing:
        contract = build_output_field_contract(
            sql=sql,
            result_columns=result_fields,
            missing_result_fields=missing,
            pruning_reason="Result columns are not all present in the SQL final SELECT.",
        )
        return sql, contract

    retained_expressions = [expression_by_alias[normalize_field_name(field)] for field in result_fields]
    current_norms = [normalize_field_name(alias) for alias in ordered_aliases]
    result_norms = [normalize_field_name(field) for field in result_fields]
    if current_norms == result_norms:
        return sql, build_output_field_contract(sql=sql, result_columns=result_fields)

    removed_alias_expressions = {
        alias: expression_by_alias[normalize_field_name(alias)]
        for alias in ordered_aliases
        if normalize_field_name(alias) not in set(result_norms)
    }
    replacement = "\n    " + ",\n    ".join(retained_expressions) + "\n"
    pruned_sql = sql[: projection.start] + replacement + sql[projection.end :]
    contract = build_output_field_contract(
        sql=pruned_sql,
        result_columns=result_fields,
        pruned=True,
        pruning_reason="Final result file columns are the retained output contract.",
    )
    contract["sql_final_fields_before_prune"] = ordered_aliases
    contract["removed_output_fields"] = [alias for alias in ordered_aliases if normalize_field_name(alias) not in set(result_norms)]
    pruned_sql, having_report = _rewrite_final_having_removed_aliases(pruned_sql, removed_alias_expressions)
    contract.update(having_report)
    pruned_sql, order_report = _prune_final_order_by_removed_fields(
        pruned_sql,
        contract["removed_output_fields"],
        field_order_before=ordered_aliases,
        field_order_after=result_fields,
        removed_alias_expressions=removed_alias_expressions,
    )
    contract.update(order_report)
    return pruned_sql, contract
