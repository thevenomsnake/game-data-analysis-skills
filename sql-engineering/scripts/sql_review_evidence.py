#!/usr/bin/env python3
"""Build deterministic evidence bundles for SQL review product analysis."""

from __future__ import annotations

import re
from typing import Any


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _compact(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _metric_logic_by_name(metric_logic: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in metric_logic:
        name = str(item.get("metric") or "").strip().lower()
        if name and name not in result:
            result[name] = item
    return result


def _normalize_name(value: Any) -> str:
    return str(value or "").strip().strip("`").lower()


def _comment_lines_from_sql(sql: str, limit: int = 60) -> list[str]:
    rows: list[str] = []
    for block in re.findall(r"/\*([\s\S]*?)\*/", sql):
        for line in block.splitlines():
            cleaned = re.sub(r"^\s*[*=-]+\s*", "", line).strip()
            if cleaned:
                rows.append(_compact(cleaned, 500))
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            cleaned = stripped[2:].strip()
            if cleaned:
                rows.append(_compact(cleaned, 500))
    seen: set[str] = set()
    result: list[str] = []
    for row in rows:
        if row in seen:
            continue
        seen.add(row)
        result.append(row)
        if len(result) >= limit:
            break
    return result


def _sql_line_evidence(sql: str, pattern: str, *, label: str, limit: int = 12) -> list[dict]:
    rows: list[dict] = []
    for line_number, line in enumerate(sql.splitlines(), start=1):
        if not re.search(pattern, line, flags=re.I):
            continue
        rows.append(
            {
                "ref": f"sql:L{line_number}:{label}",
                "snippet": _compact(line.strip(), 360),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _source_tables_for_log(sql: str, log_name: str, limit: int = 8) -> list[str]:
    token = re.escape(log_name.lower())
    raw_sources = sorted(set(re.findall(rf"\b[\w.]*{token}[\w.]*\b", sql.lower(), flags=re.I)))
    result: list[str] = []
    seen: set[str] = set()
    simple = log_name.lower()
    for item in raw_sources:
        if not (
            item == simple
            or item.startswith("demo_warehouse.")
            or f"_dsl_{simple}" in item
            or item.endswith(f"_{simple}_fht0")
        ):
            continue
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _extract_case_id_mappings(sql: str) -> list[dict]:
    rows: list[dict] = []
    for match in re.finditer(
        r"WHEN\s+CAST\s*\(\s*(?P<field>[A-Za-z_][\w.]*?(?:ItemId|MissionSubId|TaskId|StageId|Mode|Type|Id))\s+AS\s+BIGINT\s*\)\s+IN\s*\((?P<ids>[\s\S]*?)\)\s+THEN\s+'(?P<label>[^']+)'",
        sql,
        flags=re.I,
    ):
        ids = re.findall(r"\b\d{3,}\b", match.group("ids"))
        field = match.group("field").split(".")[-1]
        rows.append(
            {
                "field": field,
                "label": match.group("label"),
                "values": ids[:80],
                "evidence_ref": "sql.case_mapping." + field,
            }
        )
    return rows


def _extract_prefix_mappings(sql: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for match in re.finditer(
        r"WHEN\s+substr\s*\(\s*CAST\s*\(\s*(?P<field>[A-Za-z_][\w.]*?(?:MissionId|MissionSubId|TalentID|TemplateId|Id))\s+AS\s+string\s*\)\s*,\s*1\s*,\s*(?P<len>\d+)\s*\)\s*(?P<op>=|IN)\s*(?P<value>\([^)]+\)|'[^']+')\s*THEN\s+'(?P<label>[^']+)'",
        sql,
        flags=re.I,
    ):
        value_text = match.group("value")
        values = re.findall(r"'([^']+)'", value_text)
        if not values:
            values = [_compact(value_text.strip("'() "), 80)]
        field = match.group("field").split(".")[-1]
        key = (field, match.group("label"), tuple(values))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "field": field,
                "label": match.group("label"),
                "prefix_length": int(match.group("len")),
                "prefix_values": values[:40],
                "values": values[:40],
                "evidence_ref": "sql.prefix_mapping." + field,
            }
        )
    for match in re.finditer(r"WHEN\s+(?P<body>[\s\S]{0,700}?)\s+THEN\s+'(?P<label>[^']+)'", sql, flags=re.I):
        body = match.group("body")
        prefix_matches = re.findall(
            r"substr\s*\(\s*CAST\s*\(\s*(?P<field>[A-Za-z_][\w.]*?(?:MissionId|MissionSubId|TalentID|TemplateId|Id))\s+AS\s+string\s*\)\s*,\s*1\s*,\s*(?P<len>\d+)\s*\)\s*(?:=|IN)\s*(?P<value>\([^)]+\)|'[^']+')",
            body,
            flags=re.I,
        )
        grouped: dict[str, list[str]] = {}
        lengths: dict[str, int] = {}
        for field, length, value_text in prefix_matches:
            values = re.findall(r"'([^']+)'", value_text)
            if not values:
                values = [_compact(value_text.strip("'() "), 80)]
            display = field.split(".")[-1]
            grouped.setdefault(display, []).extend(values)
            lengths[display] = int(length)
        for field, values in grouped.items():
            unique_values = _unique_preserve_order(values)
            key = (field, match.group("label"), tuple(unique_values))
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "field": field,
                    "label": match.group("label"),
                    "prefix_length": lengths.get(field),
                    "prefix_values": unique_values[:40],
                    "values": unique_values[:40],
                    "evidence_ref": "sql.prefix_mapping." + field,
                }
            )
    return rows


def _extract_select_id_maps(sql: str) -> list[dict]:
    grouped: dict[str, list[str]] = {}
    for match in re.finditer(r"\bSELECT\s+(?P<id>\d{2,})\s+AS\s+(?P<field>[A-Za-z_][\w]*)\b", sql, flags=re.I):
        field = match.group("field")
        display = field
        lower = field.lower()
        if "battlemission" in lower or "mission" in lower:
            display = "BattleMissionSubId"
        elif "battleitem" in lower or "item" in lower:
            display = "BattleItemId"
        elif "talent" in lower:
            display = "TalentID"
        grouped.setdefault(display, []).append(match.group("id"))
    rows: list[dict] = []
    for field, values in grouped.items():
        unique_values = _unique_preserve_order(values)
        rows.append(
            {
                "field": field,
                "values": unique_values[:160],
                "evidence_ref": "sql.id_map." + field,
            }
        )
    return rows


def _extract_inline_dimension_mappings(sql: str) -> list[dict]:
    rows: list[dict] = []
    talent_values: list[str] = []
    talent_labels: list[dict] = []
    for match in re.finditer(
        r"(?:UNION\s+ALL\s+)?SELECT\s+(?P<id>\d{2,6})\s*,\s*'(?P<label>[^']+)'(?P<rest>[^\n]*)",
        sql,
        flags=re.I,
    ):
        nearby = sql[max(0, match.start() - 800) : min(len(sql), match.end() + 800)]
        if not re.search(r"TalentID|talent_id|天赋|技巧|解锁", nearby, flags=re.I):
            continue
        talent_values.append(match.group("id"))
        talent_labels.append(
            {
                "value": match.group("id"),
                "label": match.group("label"),
                "extra": _compact(match.group("rest"), 160),
            }
        )
    if talent_values:
        rows.append(
            {
                "field": "TalentID",
                "values": _unique_preserve_order(talent_values)[:220],
                "mapping": talent_labels[:80],
                "evidence_ref": "sql.inline_dimension.TalentID",
            }
        )
    return rows


def _extract_fixed_resource_fields(sql: str) -> list[dict]:
    rows: list[dict] = []
    template_values = _unique_preserve_order(re.findall(r"\b(?:r\.)?TemplateId\s*=\s*(\d+)\b", sql, flags=re.I))
    if template_values:
        rows.append(
            {
                "field": "TemplateId",
                "values": template_values[:20],
                "evidence_ref": "sql.fixed_resource.TemplateId",
            }
        )
    action_values = _unique_preserve_order(re.findall(r"\b(?:r\.)?ActionType\s*=\s*'([^']+)'\b", sql, flags=re.I))
    for in_match in re.finditer(r"\b(?:r\.)?ActionType\s+IN\s*\(([^)]+)\)", sql, flags=re.I):
        action_values.extend(re.findall(r"'([^']+)'", in_match.group(1)))
    action_values = _unique_preserve_order(action_values)
    if action_values:
        rows.append(
            {
                "field": "ActionType",
                "values": action_values[:20],
                "mapping": [
                    {"value": "add", "label": "资源获得/增加"},
                    {"value": "del", "label": "资源消耗/扣减"},
                ],
                "evidence_ref": "sql.fixed_resource.ActionType",
            }
        )
    resource_pair_values = _unique_preserve_order(re.findall(r"split\s*\(\s*resource_pair\s*,\s*':'\s*\)\s*\[\s*0\s*\]\s*=\s*'([^']+)'", sql, flags=re.I))
    if resource_pair_values:
        rows.append(
            {
                "field": "resource_pair.resource_id",
                "values": resource_pair_values[:20],
                "evidence_ref": "sql.fixed_resource.resource_pair",
            }
        )
    return rows


def _unique_preserve_order(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _extract_event_conditions(sql: str) -> list[dict]:
    rows: list[dict] = []
    patterns = [
        (
            r"\b(?P<field>[A-Za-z_][\w.]*TemplateId)\b\s*=\s*(?P<value>\d+)",
            "TemplateId = {value}",
            "资源模板 ID 命中指定资源，通常用于限定信任点/技巧点等资源口径。",
        ),
        (
            r"\b(?P<field>[A-Za-z_][\w.]*ActionType)\b\s*=\s*'(?P<value>add|del)'",
            "ActionType = '{value}'",
            "资源流水方向：add 表示获得/增加，del 表示消耗/扣减。",
        ),
        (
            r"\b(?P<field>[A-Za-z_][\w.]*DeltaValue)\b\s*>\s*0",
            "DeltaValue > 0",
            "资源变化量为正，通常表示获得量有效。",
        ),
        (
            r"COALESCE\s*\(\s*(?P<field>[A-Za-z_][\w.]*DeltaValue)\s*,\s*0\s*\)\s*>\s*0",
            "DeltaValue > 0",
            "资源变化量为正，通常表示获得量有效。",
        ),
        (
            r"CAST\s*\(\s*(?P<field>[A-Za-z_][\w.]*BattleItemDelta)\s+AS\s+BIGINT\s*\)\s*>\s*0",
            "BattleItemDelta > 0",
            "领取/获得数量为正，通常表示发生奖励领取或道具获得事件。",
        ),
        (
            r"\b(?P<field>[A-Za-z_][\w.]*BattleItemDelta)\b\s*>\s*0",
            "BattleItemDelta > 0",
            "领取/获得数量为正，通常表示发生奖励领取或道具获得事件。",
        ),
        (
            r"CAST\s*\(\s*(?P<field>[A-Za-z_][\w.]*BattleMissionComplete)\s+AS\s+BIGINT\s*\)\s*=\s*1",
            "BattleMissionComplete = 1",
            "任务完成标记为 1，通常表示任务完成事件成立。",
        ),
        (
            r"\b(?P<field>[A-Za-z_][\w.]*BattleMissionComplete)\b\s*=\s*1",
            "BattleMissionComplete = 1",
            "任务完成标记为 1，通常表示任务完成事件成立。",
        ),
    ]
    for pattern, condition, meaning in patterns:
        field_match = re.search(pattern, sql, flags=re.I)
        field = field_match.group("field").split(".")[-1] if field_match and "field" in field_match.groupdict() else ""
        condition_text = condition
        if field_match and "value" in field_match.groupdict():
            condition_text = condition.format(value=field_match.group("value"))
        evidence = _sql_line_evidence(sql, pattern, label=condition_text, limit=8)
        if not evidence:
            continue
        rows.append(
            {
                "field": field,
                "sql_condition": condition_text,
                "business_meaning_hint": meaning,
                "sql_evidence": evidence,
            }
        )
    generic_pattern = (
        r"CAST\s*\(\s*(?P<field>[A-Za-z_][\w.]*?(?:Complete|Finish|Delta|Status|State|Flag|Cnt|Count))\s+AS\s+BIGINT\s*\)\s*"
        r"(?P<op>=|>|>=|<|<=)\s*(?P<value>\d+)"
    )
    for match in re.finditer(generic_pattern, sql, flags=re.I):
        field = match.group("field").split(".")[-1]
        condition = f"{field} {match.group('op')} {match.group('value')}"
        if any(row.get("sql_condition") == condition for row in rows):
            continue
        rows.append(
            {
                "field": field,
                "sql_condition": condition,
                "business_meaning_hint": "事件状态或计数字段满足阈值，需由模型结合上下文解释为具体行为。",
                "sql_evidence": _sql_line_evidence(sql, re.escape(match.group(0)), label=condition, limit=4),
            }
        )
    return rows[:20]


def _extract_row_number_rules(sql: str) -> list[dict]:
    rows: list[dict] = []
    for match in re.finditer(
        r"ROW_NUMBER\s*\(\s*\)\s*OVER\s*\(\s*PARTITION\s+BY\s+(?P<partition>[\s\S]*?)\s+ORDER\s+BY\s+(?P<order>[\s\S]*?)\)\s+AS\s+(?P<alias>[A-Za-z_][\w]*)",
        sql,
        flags=re.I,
    ):
        partition = _compact(match.group("partition"), 220)
        order = _compact(match.group("order"), 220)
        rows.append(
            {
                "rule": f"ROW_NUMBER 按 {partition} 分组，按 {order} 排序后取 {match.group('alias')}=1 时代表首次/最早事件。",
                "partition_by": partition,
                "order_by": order,
                "alias": match.group("alias"),
                "evidence_ref": "sql.window.row_number",
            }
        )
    return rows[:12]


def _extract_time_fields(sql: str) -> list[str]:
    return _unique_preserve_order(re.findall(r"\b(?:dtEventTime|EventTime|LogTime|CreateTime|UpdateTime|RegTime|reg_date|dt)\b", sql, flags=re.I))[:16]


def _extract_entity_keys(sql: str) -> list[str]:
    return _unique_preserve_order(
        re.findall(r"\b(?:iZoneAreaID|vOpenID|OpenID|RoleID|BattleSrvId|UniqueBattleID|GameMode|dtEventTime|reg_date|reward_tier|BattleMissionId|BattleMissionSubId|TalentID|TemplateId|ActionType)\b", sql, flags=re.I)
    )[:20]


def _extract_source_fields(sql: str) -> list[str]:
    candidates = re.findall(
        r"\b(?:BattleItemDelta|BattleItemId|BattleMissionComplete|BattleMissionId|BattleMissionSubId|BattleMissionBattleDuration|TemplateId|ActionType|DeltaValue|TalentID|TotalActiveDuration|BattleSrvId|dtEventTime|vOpenID|iZoneAreaID|GameMode|OnlineTime|MatchDuration|resource_pair)\b",
        sql,
        flags=re.I,
    )
    return _unique_preserve_order(candidates)[:24]


def _comment_refs_for_event(comment_lines: list[str], keywords: list[str], limit: int = 12) -> list[dict]:
    rows: list[dict] = []
    for index, line in enumerate(comment_lines):
        if not any(keyword.lower() in line.lower() for keyword in keywords):
            continue
        rows.append({"ref": f"comment_lines[{index}]", "snippet": line})
        if len(rows) >= limit:
            break
    return rows


def _event_contract_candidates(sql: str, comment_lines: list[str]) -> list[dict]:
    if not sql.strip():
        return []
    conditions = _extract_event_conditions(sql)
    case_mappings = _extract_case_id_mappings(sql)
    id_maps = _extract_select_id_maps(sql)
    prefix_mappings = _extract_prefix_mappings(sql)
    inline_dimension_mappings = _extract_inline_dimension_mappings(sql)
    fixed_resource_fields = _extract_fixed_resource_fields(sql)
    first_last_rules = _extract_row_number_rules(sql)
    time_fields = _extract_time_fields(sql)
    entity_keys = _extract_entity_keys(sql)
    source_fields = _extract_source_fields(sql)
    candidates: list[dict] = []

    def append_candidate(
        *,
        event_family: str,
        event_name_hint: str,
        source_log: str,
        condition_fields: list[str],
        id_field_keywords: list[str],
        comment_keywords: list[str],
    ) -> None:
        source_tables = _source_tables_for_log(sql, source_log)
        has_source = bool(source_tables) or re.search(re.escape(source_log), sql, flags=re.I)
        event_conditions = [
            item
            for item in conditions
            if any(keyword.lower() in str(item.get("field") or "").lower() for keyword in condition_fields)
        ]
        id_fields: list[dict] = []
        for item in case_mappings + id_maps + prefix_mappings + inline_dimension_mappings + fixed_resource_fields:
            field = str(item.get("field") or "")
            if any(keyword.lower() in field.lower() for keyword in id_field_keywords):
                id_fields.append(item)
        comment_refs = _comment_refs_for_event(comment_lines, comment_keywords)
        if not (has_source and (event_conditions or id_fields or comment_refs)):
            return
        sql_evidence: list[dict] = []
        for item in event_conditions:
            sql_evidence.extend(_list(item.get("sql_evidence")))
        for item in id_fields:
            evidence_ref = str(item.get("evidence_ref") or "")
            summary = ""
            values = _list(item.get("values"))
            if values:
                shown = "、".join(str(value) for value in values[:18])
                if len(values) > 18:
                    shown += " 等"
                summary = f"{item.get('field')} ID 范围/映射：{shown}"
            elif item.get("label"):
                summary = f"{item.get('field')} 映射到 {item.get('label')}"
            if summary:
                sql_evidence.append({"ref": evidence_ref or "sql.id_mapping", "snippet": summary})
        sql_evidence.extend(comment_refs[:6])
        candidates.append(
            {
                "candidate_id": f"event_contract_candidate_{len(candidates) + 1}",
                "event_family": event_family,
                "event_name_hint": event_name_hint,
                "source_logs_or_tables": source_tables or [source_log],
                "event_condition_candidates": event_conditions,
                "id_fields": id_fields[:12],
                "time_fields": time_fields,
                "entity_keys": entity_keys,
                "first_last_rule_candidates": first_last_rules,
                "source_field_candidates": source_fields,
                "join_backfill_candidates": [
                    item["snippet"]
                    for item in _sql_line_evidence(
                        sql,
                        r"\bJOIN\b|BattleSrvId|dtEventTime\s*<=|reg_date|iZoneAreaID|vOpenID",
                        label="join_or_scope",
                        limit=16,
                    )
                ],
                "business_comment_refs": comment_refs,
                "sql_evidence": sql_evidence[:24],
                "must_be_reviewed_by_llm": True,
            }
        )

    append_candidate(
        event_family="mission_completion",
        event_name_hint="每日任务完成事件",
        source_log="BattleMission",
        condition_fields=["Mission", "Complete"],
        id_field_keywords=["Mission"],
        comment_keywords=["每日任务", "势力任务", "任务完成", "首次完成", "到达人数", "完成人数", "BattleMission", "BattleMissionId", "BattleMissionSubId", "BattleMissionComplete"],
    )
    append_candidate(
        event_family="reward_claim",
        event_name_hint="每日任务奖励领取事件",
        source_log="BattleItem",
        condition_fields=["Item", "Delta"],
        id_field_keywords=["Item"],
        comment_keywords=["奖励", "领奖", "领取", "BattleItem", "BattleItemId", "BattleItemDelta"],
    )
    append_candidate(
        event_family="resource_flow",
        event_name_hint="资源流水获得/消耗事件",
        source_log="LobbyResourceFlow",
        condition_fields=["TemplateId", "ActionType", "DeltaValue"],
        id_field_keywords=["TemplateId", "ActionType", "resource"],
        comment_keywords=["LobbyResourceFlow", "资源", "信任点", "技巧点", "获得", "消耗", "TemplateId", "ActionType", "DeltaValue", "4001"],
    )
    append_candidate(
        event_family="talent_unlock",
        event_name_hint="势力技巧/TalentID 解锁事件",
        source_log="TalentSystem",
        condition_fields=["Talent"],
        id_field_keywords=["Talent"],
        comment_keywords=["TalentSystem", "TalentID", "天赋", "技巧", "解锁", "前5个"],
    )
    duration_pattern = r"TotalActiveDuration|BattleSrvId|累计非挂机|非挂机时长|ActiveDuration|first_.*duration|duration_attribution"
    if re.search(duration_pattern, sql, flags=re.I):
        comment_refs = _comment_refs_for_event(comment_lines, ["累计非挂机", "时长", "BattleLogInOut", "BattleLoginOut", "TotalActiveDuration"])
        candidates.append(
            {
                "candidate_id": f"event_contract_candidate_{len(candidates) + 1}",
                "event_family": "duration_attribution",
                "event_name_hint": "累计非挂机时长归因",
                "source_logs_or_tables": _source_tables_for_log(sql, "battleloginout") or _source_tables_for_log(sql, "battlelogout") or ["BattleLogInOut"],
                "event_condition_candidates": [],
                "id_fields": [],
                "time_fields": time_fields,
                "entity_keys": entity_keys,
                "first_last_rule_candidates": first_last_rules,
                "source_field_candidates": source_fields,
                "join_backfill_candidates": [
                    item["snippet"]
                    for item in _sql_line_evidence(
                        sql,
                        r"TotalActiveDuration|BattleSrvId|dtEventTime\s*<=|first_.*duration|累计非挂机|其他战斗服",
                        label="duration_attribution",
                        limit=18,
                    )
                ],
                "business_comment_refs": comment_refs,
                "sql_evidence": _sql_line_evidence(sql, r"TotalActiveDuration|BattleSrvId|dtEventTime\s*<=", label="duration_attribution", limit=18) + comment_refs[:6],
                "must_be_reviewed_by_llm": True,
            }
        )
    return candidates[:8]


def _source_step_payload(step: dict) -> dict:
    return {
        "role": _compact(step.get("role"), 80),
        "source_step": _compact(step.get("source_step"), 180),
        "source_tables": [str(item) for item in _list(step.get("source_tables"))[:8]],
        "group_by": [str(item) for item in _list(step.get("group_by"))[:8]],
        "field_expression": _compact(step.get("field_expression"), 240),
        "story": _compact(step.get("story"), 240),
        "lineage": [_compact(item, 220) for item in _list(step.get("lineage"))[:8]],
    }


PRODUCT_RULE_RESULTS = {"matched", "conflict", "proposed_conflict", "needs_manual_check"}


def _product_rule_checks(checks: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in checks:
        if not isinstance(item, dict):
            continue
        result = str(item.get("result") or "").strip().lower()
        if result not in PRODUCT_RULE_RESULTS:
            continue
        message = str(item.get("message") or "")
        evidence = str(item.get("evidence") or "")
        if result == "needs_manual_check" and "Saved rule appears relevant" in message:
            # Token-overlap diagnostics are useful in Code View, but they are not "this SQL used this口径".
            continue
        rows.append(
            {
                "rule_id": _compact(item.get("rule_id"), 120),
                "concept_key": _compact(item.get("concept_key"), 160),
                "title": _compact(item.get("title"), 220),
                "result": result,
                "message": _compact(message, 420),
                "evidence": _compact(evidence, 260),
                "rule_summary": _compact(item.get("rule_summary"), 420),
            }
        )
    return rows[:30]


def _criteria_alignment(rule_checks: list[dict]) -> dict:
    applied = _product_rule_checks(rule_checks)
    return {
        "applied_criteria": [
            item for item in applied if item.get("result") in {"matched", "conflict", "proposed_conflict", "needs_manual_check"}
        ],
        "matched_saved_rules": [item for item in applied if item.get("result") == "matched"],
        "conflicts": [item for item in applied if item.get("result") in {"conflict", "proposed_conflict"}],
        "needs_manual_check": [item for item in applied if item.get("result") == "needs_manual_check"],
        "diagnostic_note": "Weak token-overlap and reverse-source diagnostics are kept in Code View, not in Product View.",
    }


def build_evidence_bundle(
    *,
    item_path: str,
    item_name: str,
    sql_hash: str,
    sql_text: str = "",
    static_product_view: dict,
    code_view: dict,
    dimensions: dict,
) -> dict:
    """Create a compact evidence bundle from existing deterministic review facts.

    The bundle intentionally contains facts and traces, not final product wording.
    Product language is generated by sql_review_product_agent from this structure.
    """

    story = _dict(static_product_view)
    code = _dict(code_view)
    sql_summary = _dict(code.get("sql_summary"))
    result_file = _dict(code.get("result_file"))
    execution_evidence = _dict(code.get("execution_evidence"))
    role_context = _dict(code.get("role_context"))
    metric_trace = _dict(code.get("metric_review_trace"))
    metric_logic = _list(code.get("metric_logic"))
    logic_by_name = _metric_logic_by_name(metric_logic)
    business_comment_lines = _comment_lines_from_sql(sql_text)
    product_rule_checks = _product_rule_checks(_list(code.get("rule_checks")))

    metric_cards: list[dict] = []
    for card in _list(metric_trace.get("metric_cards")):
        metric_name = _first_non_empty(card.get("metric"), card.get("metric_name"))
        logic = logic_by_name.get(_normalize_name(metric_name), {})
        metric_cards.append(
            {
                "metric_name": metric_name,
                "calculation_type": _first_non_empty(logic.get("calculation_type"), card.get("calculation_type")),
                "business_definition": _compact(card.get("business_definition"), 600),
                "base": _compact(_first_non_empty(card.get("base"), card.get("base_population")), 600),
                "numerator": _compact(card.get("numerator"), 500),
                "denominator": _compact(card.get("denominator"), 500),
                "calculation": _compact(card.get("calculation"), 500),
                "confidence": _first_non_empty(card.get("confidence"), logic.get("confidence"), "low"),
                "source": _first_non_empty(card.get("source"), logic.get("description_source")),
                "needs_manual_confirmation": bool(
                    card.get("needs_manual_confirmation") or logic.get("needs_manual_confirmation")
                ),
                "how_to_review": _compact(card.get("how_to_review"), 500),
                "pass_criteria": _compact(card.get("pass_criteria"), 500),
                "reviewer_question": _compact(card.get("reviewer_question"), 500),
                "source_steps": [
                    _source_step_payload(step)
                    for step in _list(card.get("source_steps") or logic.get("source_steps"))[:10]
                    if isinstance(step, dict)
                ],
                "base_business_filters": _list(card.get("base_business_filters") or logic.get("base_business_filters")),
                "metric_business_filters": _list(card.get("metric_business_filters") or logic.get("metric_business_filters")),
                "join_business_filters": _list(card.get("join_business_filters") or logic.get("join_business_filters")),
                "metric_conditions": _list(card.get("metric_conditions") or logic.get("metric_condition_cards")),
                "related_saved_rule_checks": _product_rule_checks(
                    _list(card.get("related_saved_rule_checks") or logic.get("related_saved_rule_checks"))
                ),
                "sql_trace": _dict(card.get("sql_trace")),
                "formula_expression": _compact(logic.get("formula_expression"), 500),
                "numerator_expression": _compact(logic.get("numerator_expression"), 300),
                "denominator_expression": _compact(logic.get("denominator_expression"), 300),
                "lineage": [_compact(item, 260) for item in _list(logic.get("lineage"))[:10]],
            }
        )

    dimension_cards: list[dict] = []
    for card in _list(metric_trace.get("dimension_cards")):
        dimension_cards.append(
            {
                "field": _compact(card.get("field"), 120),
                "role": _compact(card.get("role"), 120),
                "description": _compact(card.get("description"), 300),
                "source": _compact(card.get("source"), 80),
                "confidence": _compact(card.get("confidence"), 80),
            }
        )

    common_filters: list[dict] = []
    for item in _list(story.get("key_filters")):
        common_filters.append(
            {
                "label": _compact(item.get("label"), 160),
                "scope": _compact(item.get("scope"), 160),
                "business_effect": _compact(item.get("business_effect"), 360),
                "review_focus": _compact(item.get("review_focus"), 360),
                "source": "product_key_filter",
            }
        )
    for item in _list(code.get("business_filters")):
        common_filters.append(
            {
                "label": _compact(_first_non_empty(item.get("label"), item.get("field")), 160),
                "scope": _compact(_first_non_empty(item.get("scope_label"), item.get("scope")), 160),
                "business_effect": _compact(item.get("business_effect"), 360),
                "review_focus": _compact(_first_non_empty(item.get("how_to_judge"), item.get("pass_criteria")), 360),
                "condition": _compact(item.get("condition"), 300),
                "values": [str(value) for value in _list(item.get("values"))[:20]],
                "mapping": _list(item.get("mapping"))[:20],
                "source": "sql_filter",
            }
        )

    return {
        "schema_version": "sql_review_evidence_v3",
        "path": item_path,
        "name": item_name,
        "sql_hash": sql_hash,
        "question": _compact(story.get("business_question"), 500),
        "analysis_pattern": _compact(story.get("analysis_pattern"), 120),
        "conclusion_hint": _compact(story.get("one_sentence"), 600),
        "business_comment_lines": business_comment_lines,
        "event_contract_candidates": _event_contract_candidates(sql_text, business_comment_lines),
        "source_logs": [str(item) for item in _list(story.get("source_logs"))[:20]],
        "source_tables": [str(item) for item in _list(sql_summary.get("source_tables"))[:30]],
        "target_tables": [str(item) for item in _list(sql_summary.get("target_tables"))[:20]],
        "final_output_fields": [str(item) for item in _list(sql_summary.get("final_fields"))[:80]],
        "execution_evidence": {
            "current_sql_role": _compact(
                _first_non_empty(execution_evidence.get("current_role"), sql_summary.get("current_sql_role")),
                60,
            ),
            "review_subject": _compact(execution_evidence.get("review_subject"), 80),
            "result_evidence_role": _compact(execution_evidence.get("result_evidence_role"), 80),
            "sql_files": [str(item) for item in _list(execution_evidence.get("sql_files"))[:20]],
            "result_files": [str(item) for item in _list(execution_evidence.get("result_files"))[:20]],
            "selected_result_file": _compact(result_file.get("path"), 260),
            "result_pairing_method": _compact(
                _first_non_empty(result_file.get("pairing_method"), sql_summary.get("result_pairing_method")),
                80,
            ),
            "execution_project": _compact(role_context.get("execution_project"), 120),
            "delivery_project": _compact(role_context.get("delivery_project"), 120),
            "evidence_status": _compact(role_context.get("evidence_status"), 100),
            "result_status": _compact(result_file.get("status"), 100),
            "result_rows": result_file.get("row_count"),
        },
        "metric_names": [str(item) for item in _list(sql_summary.get("metrics"))[:80]],
        "dimension_names": [str(item) for item in _list(sql_summary.get("dimensions"))[:80]],
        "grouping": _compact(story.get("grouping"), 400),
        "base": _compact(story.get("base"), 700),
        "logic_steps": [_compact(item, 500) for item in _list(story.get("logic_steps"))[:20]],
        "metric_cards": metric_cards,
        "dimension_cards": dimension_cards,
        "common_filters": common_filters,
        "result_evidence": {
            "status": _compact(result_file.get("status"), 100),
            "path": _compact(result_file.get("path"), 260),
            "row_count": result_file.get("row_count"),
            "columns": [str(item) for item in _list(result_file.get("columns"))[:80]],
            "sample_rows": _list(result_file.get("sample_rows"))[:5],
            "missing_columns": [str(item) for item in _list(result_file.get("missing_columns"))[:40]],
            "extra_columns": [str(item) for item in _list(result_file.get("extra_columns"))[:40]],
            "order_mismatch": bool(result_file.get("order_mismatch")),
            "note": _compact(result_file.get("note"), 500),
        },
        "criteria_alignment": _criteria_alignment(_list(code.get("rule_checks"))),
        "rule_checks": product_rule_checks,
        "diagnostic_rule_checks": _list(code.get("rule_checks"))[:50],
        "dimensions_status": dimensions,
    }
