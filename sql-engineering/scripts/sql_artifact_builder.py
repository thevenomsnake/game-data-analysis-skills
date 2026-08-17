#!/usr/bin/env python3
"""Build formal query/validation/dashboard specs for the fast formalization path."""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_provenance import build_generation_provenance
from performance_preflight import analyze_performance
from sql_facts import extract_tables, is_tlog_source_table
from sql_project import params_cte_aliases, project_context_snapshot
from sql_execution_adapter import (
    effective_config_for_sql,
    effective_config_from_route,
    execution_route_for_sql,
    route_matches_context,
)
from sql_semantic_summary import final_select_fields, has_chinese
from spec_utils import SPEC_STORAGE, SPEC_VERSION


FORBIDDEN_FIELDS: list[str] = []
DA_MANAGED_IDENTIFIER_FIELDS = {"vopenid", "open_id", "roleid", "role_id", "deviceid", "device_id"}
TOTAL_ROW_LABEL = "合计"
DA_DATE_PARAMETERS = ["start_date", "end_date"]


def normalize_output_field_name(value: Any) -> str:
    return str(value or "").strip().strip("`").lower()


def filter_field_display_rules(rules: Any, expected_fields: list[str]) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        return []
    expected_norms = {
        normalized
        for field in expected_fields
        if (normalized := normalize_output_field_name(field))
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        field = str(rule.get("output_field") or "").strip()
        field_norm = normalize_output_field_name(field)
        if not field_norm:
            continue
        if expected_norms and field_norm not in expected_norms:
            continue
        if field_norm in seen:
            continue
        seen.add(field_norm)
        rows.append(dict(rule))
    return rows


def output_field_display_rules(
    *,
    result: dict[str, Any],
    expected_fields: list[str],
    query_spec_doc: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    result_rules = filter_field_display_rules(result.get("ratio_field_rules"), expected_fields)
    if result_rules:
        return result_rules, "result_file_ratio_field_rules"
    if isinstance(query_spec_doc, dict):
        contract = query_spec_doc.get("query_output_contract")
        if isinstance(contract, dict):
            spec_rules = filter_field_display_rules(contract.get("field_display_rules"), expected_fields)
            if spec_rules:
                return spec_rules, "query_output_contract_field_display_rules"
    return [], "none"


def sql_declared_total_policy(grouping_fields: list[str], metrics: list[dict[str, Any]], *, contains_total_rows: bool) -> dict[str, Any]:
    total_scope = "默认按 DA 选择的日期范围输出区间结果；只有 SQL/spec 明确声明合计行时才按合计行解释，DA 不额外生成合计。"
    return {
        "grouping_fields": grouping_fields,
        "sql_contains_total_rows": contains_total_rows,
        "da_generate_total": False,
        "total_label": TOTAL_ROW_LABEL if contains_total_rows else None,
        "total_scope": total_scope,
        "total_source": "sql_declared_output",
        "avoid_duplicate_scan": True,
        "total_metric_rules": [
            {
                "metric": item["field"],
                "method": item["dashboard_agg"],
                "total_safe": item["total_safe"],
                "total_meaning": item["total_meaning"],
            }
            for item in metrics
        ],
    }


def sql_body_for_shape_detection(sql: str) -> str:
    without_block_comments = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--.*", " ", without_block_comments)


def infer_dashboard_output_shape(dashboard_sql: str, fields: list[str]) -> dict[str, Any]:
    """Declare dashboard output shape explicitly while keeping total-row sniffing lightweight."""
    sql_body = sql_body_for_shape_detection(dashboard_sql)
    contains_total_rows = TOTAL_ROW_LABEL in sql_body
    lowered_fields = [field.lower() for field in fields]
    has_date_field = any(
        "日期" in field
        or field in {"date", "dt", "stat_date", "event_date", "dteventdate"}
        for field in lowered_fields
    )
    if contains_total_rows and has_date_field:
        result_mode = "daily_plus_total_table"
        time_grain = "day"
        output_grain = "按日结果 + SQL 明确输出的合计行"
        label = "按日 + 合计"
        sql_outputs_daily_rows = True
        sql_outputs_period_total = False
    elif has_date_field:
        result_mode = "daily_table"
        time_grain = "day"
        output_grain = "按日结果"
        label = "按日"
        sql_outputs_daily_rows = True
        sql_outputs_period_total = False
    else:
        result_mode = "period_total_table"
        time_grain = "none"
        output_grain = "按 DA 日期范围输出的区间结果"
        label = "区间合计"
        sql_outputs_daily_rows = False
        sql_outputs_period_total = True
    return {
        "result_mode": result_mode,
        "time_grain": time_grain,
        "label": label,
        "output_grain": output_grain,
        "contains_total_rows": contains_total_rows,
        "contains_total_rows_source": "sql_text_heuristic",
        "sql_outputs_daily_rows": sql_outputs_daily_rows,
        "sql_outputs_period_total": sql_outputs_period_total,
        "default_semantics": "默认看板 SQL 只对 DA 选择的日期范围产出一份区间结果；按日行或合计行必须由 SQL/spec 显式声明。",
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dialect(config: dict[str, Any]) -> str:
    return str(config.get("sql_dialect") or "StarRocks")


def performance_level(
    sql: str,
    config: dict[str, Any],
    kind: str,
    reusable: bool = True,
    sql_facts: dict[str, Any] | None = None,
    execution_route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = analyze_performance(
        sql=sql,
        sql_facts=sql_facts,
        project_config=config,
        mode="review",
        artifact_kind=kind,
        reusable=reusable,
        execution_route=execution_route,
    )
    tier = result.get("tier") or "L1_perf_standard"
    grade = "D" if result.get("status") == "block" else "B" if tier in {"L0_perf_lite", "L1_perf_standard"} else "C"
    return {
        "grade": grade,
        "judgement": result.get("optimization_hint") or "deterministic performance preflight applied.",
        "optimization_tier": tier,
        "preflight_score": result.get("score", 0),
        "preflight_triggers": result.get("triggers", []),
        "optimization_reference": result.get("required_references", ["references/performance-routing.md"])[-1],
        "full_guide_required": bool(result.get("full_guide_required")),
        "optimization_applied": ["fast formalization kept source SQL logic unchanged."],
        "optimization_rejected": [],
        "equivalence_preserved": True,
        "performance_fingerprint": result.get("performance_fingerprint") or "",
        "scan_partition_days": (result.get("facts") or {}).get("scan_days") or 0,
        "log_table_count": (result.get("facts") or {}).get("tlog_table_count") or 0,
        "risk_items": result.get("blockers", []),
        "preflight_status": result.get("status"),
    }


def data_sources(sql: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    policy = config.get("partition_policy") if isinstance(config.get("partition_policy"), dict) else {}
    rows: list[dict[str, Any]] = []
    for table in extract_tables(sql):
        rows.append(
            {
                "table": table,
                "table_type": "TLOG" if is_tlog_source_table(table) else "lookup_or_derived",
                "required_fields": [],
                "partition_policy": policy.get("name", "missing"),
                "business_time_field": policy.get("business_time_field", ""),
            }
        )
    return rows


def query_parameters(sql: str) -> list[dict[str, Any]]:
    aliases = params_cte_aliases(sql)
    rows: list[dict[str, Any]] = []
    for name in ["ts_start", "ts_end", "pt_start", "pt_end", "zone_id"]:
        if name not in aliases:
            continue
        role = "time_range" if name in {"ts_start", "ts_end"} else "hidden_partition" if name.startswith("pt_") else "sql_filter"
        rows.append(
            {
                "name": name,
                "type": "integer" if name == "zone_id" else "timestamp_string" if name.startswith("ts_") else "string",
                "required": name in {"ts_start", "ts_end"},
                "label": {"ts_start": "开始时间", "ts_end": "结束时间", "pt_start": "分区开始", "pt_end": "分区结束", "zone_id": "区服"}.get(name, name),
                "parameter_role": role,
                "sql_usage": f"Top params CTE alias `{name}` used by the executable SQL.",
            }
        )
    return rows


def result_output_contract(result: dict[str, Any]) -> dict[str, Any]:
    contract = result.get("output_field_contract") if isinstance(result.get("output_field_contract"), dict) else {}
    return contract


def retained_output_fields(sql: str, result: dict[str, Any]) -> list[str]:
    contract = result_output_contract(result)
    retained = [str(item or "").strip() for item in contract.get("retained_fields", []) if str(item or "").strip()]
    if retained:
        return retained
    return final_select_fields(sql) or [str(item or "").strip() for item in result.get("columns", []) if str(item or "").strip()]


def output_fields(sql: str, summary: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    fields = retained_output_fields(sql, result)
    source = "result_output_contract" if result_output_contract(result) else "final_select"
    if not fields:
        fields = [str(item.get("field") or item.get("name")) for item in summary.get("metrics", []) if isinstance(item, dict)]
    rows = []
    for field in fields:
        privacy = "da_managed" if field.lower() in DA_MANAGED_IDENTIFIER_FIELDS else "not_applicable"
        rows.append({"field": field, "label": field, "source": source, "privacy": privacy})
    return rows


def query_spec(
    *,
    root: Path,
    sql: str,
    title: str,
    config: dict[str, Any],
    analysis: dict[str, Any],
    result: dict[str, Any],
    repository_summary: dict[str, Any],
    rule_context: dict[str, Any],
    performance: dict[str, Any] | None = None,
    knowledge_references: list[dict[str, Any]] | None = None,
    knowledge_usage: dict[str, Any] | None = None,
    execution_route_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if route_matches_context(execution_route_override, sql, config):
        execution_route = copy.deepcopy(execution_route_override)
        effective_config = effective_config_from_route(config, execution_route)
    else:
        effective_config, detection = effective_config_for_sql(config, sql)
        execution_route = execution_route_for_sql(
            sql,
            config,
            effective_config=effective_config,
            detection=detection,
        )
    perf = (
        performance
        if isinstance(performance, dict) and performance
        else performance_level(
            sql,
            config,
            "QUERY",
            reusable=True,
            execution_route=execution_route,
        )
    )
    output_contract = result_output_contract(result)
    fields = retained_output_fields(sql, result)
    display_rules, display_source = output_field_display_rules(result=result, expected_fields=fields)
    metrics = repository_summary.get("metrics", [])
    filters = repository_summary.get("filters", [])

    provenance = build_generation_provenance(
        generator_script="sql_formalize.py",
        workflow="fast_formalize_query",
        artifact_kind="QUERY",
        source="fast_formalize",
    )
    return {
        "generation_provenance": provenance,
        "spec_meta": {
            "spec_version": SPEC_VERSION,
            "spec_storage": SPEC_STORAGE,
            "lifecycle_stage": "QUERY",
            "sql_type": "sql_data_query",
            "target_engine": dialect(effective_config),
            "generated_at": now_iso(),
            "generated_by": "sql_formalize.py",
        },
        "dialect_profile": {"current_dialect": dialect(effective_config), "status": "enabled"},
        "project_context": {
            **project_context_snapshot(config, sql, execution_route=execution_route),
            "table_resolution_rule": "Use scripts/sql_project.py resolve-table --execution-profile <profile> unless the user provides a physical table.",
        },
        "execution_route": execution_route,
        "canonical_rule_context": rule_context or {"applied_rules": [], "hard_constraints": [], "candidate_sql_check": {"status": "not_run", "blockers": []}},
        "knowledge_references": list(knowledge_references or []),
        "knowledge_usage": copy.deepcopy(knowledge_usage or {}),
        "performance_level": perf,
        "query_intent": {
            "title": title,
            "description": repository_summary.get("business_question") or title,
            "query_type": analysis.get("analysis_type") or "aggregate_query",
            "result_mode": "aggregate_table",
            "default_usage": "Saved formal query SQL produced by fast formalization from user-confirmed result evidence.",
        },
        "parameters": query_parameters(sql),
        "data_sources": data_sources(sql, effective_config),
        "intermediate_tables": [],
        "query_logic": {
            "business_context": repository_summary.get("base_population") or "按 SQL params/WHERE 限定的统计对象。",
            "business_rules": repository_summary.get("applied_criteria", []),
            "metric_definitions": metrics,
            "filter_rules": filters,
            "exclusion_rules": [],
            "time_range": "由 params.ts_start / params.ts_end 控制。",
            "partition_range": "由 project_config.partition_policy 和 SQL params 控制。",
            "calculation_path": "快线保留源 SQL 计算逻辑；产品摘要见 repository_summary，表达式细节见 SQL。",
            "assumptions": [],
            "conflict_notes": [],
        },
        "output_fields": output_fields(sql, repository_summary, result),
        "query_output_contract": {
            "one_row_means": repository_summary.get("grain") or analysis.get("grain") or "SQL output row",
            "output_grain": repository_summary.get("grain") or analysis.get("grain") or "SQL output row",
            "expected_fields": fields,
            "forbidden_fields": FORBIDDEN_FIELDS,
            "source": "result_file_columns" if output_contract else "final_select",
            "result_output_contract": output_contract,
            "field_display_rules": display_rules,
            "display_contract_source": display_source,
        },
        **(
            {"result_time_coverage": copy.deepcopy(result.get("time_coverage"))}
            if isinstance(result.get("time_coverage"), dict) and result.get("time_coverage")
            else {}
        ),
        "repository_summary": repository_summary,
        "quality_gate": {
            "must_pass": ["project_config", "params_cte", "performance_preflight", "rule_context", "user_result_evidence"],
            "status": "blocked" if perf.get("preflight_status") == "block" else "passed",
            "notes": perf.get("risk_items", []),
        },
    }


def confidence_score(result_status: str, summary: dict[str, Any], perf: dict[str, Any]) -> float:
    score = 0.50
    if summary.get("source_logs"):
        score += 0.12
    if summary.get("metrics") and summary.get("metric_groups"):
        score += 0.15
    if summary.get("filters"):
        score += 0.08
    if result_status in {"passed", "proxy_verified"}:
        score += 0.15
    if perf.get("preflight_status") == "block":
        score = min(score, 0.70)
    return min(score, 0.90)


def validation_spec(
    *,
    query_sql_path: str,
    run_record: dict[str, Any],
    query_spec_doc: dict[str, Any],
    title: str,
) -> dict[str, Any]:
    perf = query_spec_doc.get("performance_level", {})
    summary = query_spec_doc.get("repository_summary", {})
    status = run_record.get("status") or "passed"
    result_file = run_record.get("evidence_file") or None
    score = confidence_score(status, summary, perf)
    eligible = status in {"passed", "proxy_verified", "skipped"} and score >= 0.85 and perf.get("preflight_status") != "block"
    if status == "proxy_verified":
        decision = "promote_proxy_verified_dashboard" if eligible else "blocked"
    elif status == "skipped":
        decision = "promote_unverified_dashboard" if eligible else "blocked"
    else:
        decision = "promote_to_dashboard" if eligible else "blocked"

    provenance = build_generation_provenance(
        generator_script="sql_formalize.py",
        workflow="fast_formalize_validation",
        artifact_kind="VALIDATION",
        source="fast_formalize",
    )
    return {
        "generation_provenance": provenance,
        "spec_meta": {"spec_version": SPEC_VERSION, "spec_storage": SPEC_STORAGE, "lifecycle_stage": "VALIDATION", "target_engine": query_spec_doc.get("spec_meta", {}).get("target_engine", "StarRocks"), "generated_at": now_iso(), "generated_by": "sql_formalize.py"},
        "dialect_profile": query_spec_doc.get("dialect_profile", {}),
        "project_context": query_spec_doc.get("project_context", {}),
        "execution_route": query_spec_doc.get("execution_route", {}),
        "formalize_bundle": query_spec_doc.get("formalize_bundle", {}),
        "validation_intent": {"title": f"{title} 验证", "description": "Fast formalization generated validation sidecar from user result evidence.", "target_lifecycle_stage": "promote_to_dashboard"},
        "candidate_artifact": {"source": "formalized_query", "summary": summary.get("purpose") or title, "sql_reference": query_sql_path},
        "locked_data_sources": [{**item, "status": "locked"} for item in query_spec_doc.get("data_sources", [])],
        "locked_grain": {"status": "locked", "grain": summary.get("grain") or "", "one_row_means": query_spec_doc.get("query_output_contract", {}).get("one_row_means", "")},
        "locked_dimensions": [{"field": item.get("field"), "status": "locked", "role": item.get("role", "grouping")} for item in summary.get("dimensions", []) if isinstance(item, dict)],
        "locked_metrics": [{"field": item.get("field") or item.get("name"), "status": "locked", "metric_type": item.get("metric_type", "value"), "formula_sql": "see source query SQL"} for item in summary.get("metrics", []) if isinstance(item, dict)],
        "parameter_contract": {"parameters": query_spec_doc.get("parameters", [])},
        "business_rule_checks": {"passed": summary.get("canonical_rule_checks", []), "warnings": [], "failed": []},
        "data_quality_checks": {"required_checks": [], "optional_sql_checks": []},
        "user_run_evidence": {
            "required": True,
            "status": status,
            "evidence_reference": run_record.get("path", ""),
            "result_file_reference": result_file,
            "result_file_type": run_record.get("result_file_type") or None,
            "definition_project": run_record.get("definition_project") or None,
            "execution_project": run_record.get("execution_project") or None,
            "delivery_project": run_record.get("delivery_project") or None,
            "concept_keys": run_record.get("concept_keys", []),
            "proxy_limitations": run_record.get("proxy_limitations") or None,
            "result_summary": run_record.get("result_summary") or "User provided result file and confirmation.",
            **(
                {"result_time_coverage": copy.deepcopy(run_record.get("result_time_coverage"))}
                if isinstance(run_record.get("result_time_coverage"), dict)
                and run_record.get("result_time_coverage")
                else {}
            ),
            "user_confirmed": bool(run_record.get("user_confirmed")),
            "skip_reason": run_record.get("skip_reason") or None,
            "unverified_dashboard_allowed": status == "skipped",
            "unverified_risk_note": run_record.get("risk_note") or None,
            "future_verification_plan": run_record.get("future_verification_plan") or None,
            "promotion_blocker": not eligible,
        },
        "privacy_checks": {
            "status": "passed",
            "handling_owner": "DA",
            "sql_side_deidentification_forbidden": True,
            "notes": ["Business-required identifiers remain unchanged in SQL; DA handles privacy."],
        },
        "performance_precheck": perf,
        "confidence_assessment": {"confidence_score": score, "confidence_level": "high" if score >= 0.85 else "medium", "confidence_basis": ["user result evidence", "deterministic SQL summary", "performance preflight"], "confidence_caps_applied": [] if score >= 0.85 else ["deterministic summary may be incomplete"]},
        "promotion": {"eligible": eligible, "decision": decision, "blockers": [] if eligible else ["confidence or performance gate not eligible"], "required_changes": [], "unverified_output_required": status == "skipped", "dashboard_template": "templates/dashboard.sql"},
        "quality_gate": {"must_pass": ["run_evidence", "locked_metrics", "locked_dimensions", "performance_precheck"]},
    }


def dashboard_fixed_filters(summary_filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in summary_filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or item.get("label") or "").strip()
        condition = str(item.get("condition") or item.get("business_effect") or "").strip()
        if not field and not condition:
            continue
        rows.append(
            {
                "field": field or "fixed_filter",
                "condition": condition or field,
                "da_filterable": False,
                "all_values_available": False,
                "output_field": "",
                "reason": "该条件是源查询固定逻辑或默认时间/范围约束；不是用户明确要求的看板可改筛选项。",
                "source": item.get("kind") or "repository_summary",
            }
        )
    return rows

def dashboard_dimensions(fields: list[str], metric_names: set[str], *, contains_total_rows: bool = False) -> list[dict[str, Any]]:
    dims = [field for field in fields if field not in metric_names]
    if not dims:
        dims = ["整体"]
    return [
        {
            "field": field,
            "label": field,
            "role": "grouping" if field != "整体" else "overall",
            "data_type": "string",
            "total_policy": {"sql_contains_total_rows": contains_total_rows, "da_generate_total": False, "total_label": TOTAL_ROW_LABEL if contains_total_rows else None, "total_source": "sql_declared_output"},
        }
        for field in dims
    ]


def dashboard_metrics(summary_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in summary_metrics:
        field = str(item.get("field") or item.get("name") or item.get("label") or "").strip()
        if not field:
            continue
        metric_type = item.get("metric_type") or ("rate" if "率" in field or "占比" in field else "value")
        safe_sum = metric_type in {"count", "sum"} or any(token in field for token in ["人数", "次数", "数量"])
        rows.append(
            {
                "field": field,
                "label": field,
                "metric_type": metric_type,
                "dashboard_agg": "SUM" if safe_sum else "NONE",
                "total_safe": bool(safe_sum),
                "total_meaning": f"{field}按当前 DA 筛选范围汇总。" if safe_sum else f"{field}不应跨分组直接求和；需要总计时按源明细重算。",
                "numerator": item.get("numerator", "") if metric_type == "rate" else "",
                "denominator": item.get("denominator", "") if metric_type == "rate" else "",
                "dashboard_formula": "DA formula from numerator/denominator" if metric_type == "rate" else "",
            }
        )
    return rows


def dashboard_spec(
    *,
    dashboard_sql: str,
    query_spec_doc: dict[str, Any],
    validation_path: str,
    run_record: dict[str, Any],
    query_sql_path: str,
    title: str,
    result: dict[str, Any],
    canonical_rule_context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_contract = result_output_contract(result)
    fields = retained_output_fields(dashboard_sql, result)
    summary = query_spec_doc.get("repository_summary", {})
    metrics = dashboard_metrics(summary.get("metrics", []))
    metric_names = {item["field"] for item in metrics}
    output_shape = infer_dashboard_output_shape(dashboard_sql, fields)
    contains_total_rows = bool(output_shape["contains_total_rows"])
    dimensions = dashboard_dimensions(fields, metric_names, contains_total_rows=contains_total_rows)
    visual_rules, visual_source = output_field_display_rules(
        result=result,
        expected_fields=fields,
        query_spec_doc=query_spec_doc,
    )
    status = "proxy_verified" if run_record.get("status") == "proxy_verified" else "unverified_skipped_run" if run_record.get("status") == "skipped" else "verified"
    saved_project_check = (
        canonical_rule_context.get("project_contract_check")
        if isinstance(canonical_rule_context, dict)
        and isinstance(canonical_rule_context.get("project_contract_check"), dict)
        else {}
    )
    saved_route = saved_project_check.get("execution_route")
    if isinstance(config, dict) and config:
        if route_matches_context(saved_route, dashboard_sql, config):
            execution_route = copy.deepcopy(saved_route)
        else:
            effective_config, detection = effective_config_for_sql(config, dashboard_sql)
            execution_route = execution_route_for_sql(
                dashboard_sql,
                config,
                effective_config=effective_config,
                detection=detection,
            )
        context = project_context_snapshot(
            config,
            dashboard_sql,
            execution_route=execution_route,
        )
    else:
        execution_route = copy.deepcopy(query_spec_doc.get("execution_route") or {})
        context = query_spec_doc.get("project_context", {})
    filter_contract = {
        "time_range": {
            "parameter_names": DA_DATE_PARAMETERS,
            "label": "时间范围",
            "default_range": "来自源查询 params 默认时间窗",
            "default_label": "源查询默认时间窗",
            "business_time_field": (context.get("partition_policy") or "event_time"),
            "business_time_mapping": "params.ts_start/ts_end use DA date/time parameter values directly or date_add for exclusive end bounds; do not append fixed 00:00:00 suffix strings.",
            "partition_parameters": [],
            "partition_mapping": "按 project_config.partition_policy 执行；未配置则不额外生成分区字段。",
        },
        "sql_parameter_filters": [],
        "filterable_fields": [],
        "fixed_sql_filters": dashboard_fixed_filters(summary.get("filters", [])),
        "future_filters": [],
    }

    grouping_fields = [item["field"] for item in dimensions]

    total_policy = sql_declared_total_policy(grouping_fields, metrics, contains_total_rows=contains_total_rows)

    result_mode = output_shape["result_mode"]
    time_grain = output_shape["time_grain"]
    output_grain = query_spec_doc.get("query_output_contract", {}).get("output_grain") or summary.get("grain") or output_shape["output_grain"]

    provenance = build_generation_provenance(

        generator_script="sql_formalize.py",

        workflow="fast_formalize_dashboard",
        artifact_kind="DASHBOARD",
        source="fast_formalize",
    )
    spec = {
        "generation_provenance": provenance,
        "spec_meta": {"spec_version": SPEC_VERSION, "spec_storage": SPEC_STORAGE, "lifecycle_stage": "DASHBOARD", "sql_type": "da_dashboard_sql", "target_engine": query_spec_doc.get("spec_meta", {}).get("target_engine", "StarRocks"), "generated_at": now_iso(), "generated_by": "sql_formalize.py"},
        "dialect_profile": query_spec_doc.get("dialect_profile", {}),
        "project_context": context,
        "execution_route": execution_route,
        "canonical_rule_context": (
            canonical_rule_context
            if isinstance(canonical_rule_context, dict) and canonical_rule_context
            else query_spec_doc.get("canonical_rule_context", {})
        ),
        "knowledge_references": list(query_spec_doc.get("knowledge_references", [])),
        "knowledge_usage": copy.deepcopy(query_spec_doc.get("knowledge_usage") or {}),
        "formalize_bundle": query_spec_doc.get("formalize_bundle", {}),
        "machine_review_contract": {"contract_version": "dashboard_review_v1", "parse_required": True, "parser": "scripts/dashboard_review.py", "contract_preview_required": True, "review_state_file": "reviews/dashboard_review_state.json", "skip_approved_on_next_review": True, "result_sample_policy": "use_saved_result_file_else_auto_sample"},
        "validation_reference": {
            "validation_status": "promoted",
            "confidence_score": 0.85,
            "promotion_decision": "promote_to_dashboard"
            if status == "verified"
            else "promote_proxy_verified_dashboard"
            if status == "proxy_verified"
            else "promote_unverified_dashboard",
            "verification_status": status,
            "user_run_evidence": run_record.get("path", ""),
            "result_file_reference": run_record.get("evidence_file") or None,
            "user_run_status": run_record.get("status"),
            "definition_project": run_record.get("definition_project") or None,
            "execution_project": run_record.get("execution_project") or None,
            "delivery_project": run_record.get("delivery_project") or None,
            "concept_keys": run_record.get("concept_keys", []),
            "proxy_limitations": run_record.get("proxy_limitations") or None,
            "future_verification_plan": run_record.get("future_verification_plan") or None,
            **(
                {"result_time_coverage": copy.deepcopy(run_record.get("result_time_coverage"))}
                if isinstance(run_record.get("result_time_coverage"), dict)
                and run_record.get("result_time_coverage")
                else {}
            ),
            "validation_artifact": validation_path,
            "source_query_sql": query_sql_path,
        },
        "performance_level": {**query_spec_doc.get("performance_level", {}), "risk_items": query_spec_doc.get("performance_level", {}).get("risk_items", [])},
        "dashboard_intent": {"title": title, "description": summary.get("purpose") or title, "result_shape": "table_dataset", "visualization_owner": "DA", "result_mode": result_mode, "time_grain": time_grain, "default_usage": "默认按 DA 选择的日期范围输出区间结果；按日或合计必须由 SQL/spec 显式声明。"},
        "refresh_contract": {"da_decides_realtime_refresh": True, "required_da_decisions": ["date_range", "realtime_refresh"], "sql_date_range_parameters": DA_DATE_PARAMETERS, "sql_outputs_daily_rows": output_shape["sql_outputs_daily_rows"], "sql_outputs_total_row": contains_total_rows, "sql_outputs_period_total": output_shape["sql_outputs_period_total"], "sql_output_shape_note": output_shape["output_grain"], "note": "DA only decides the query date range and whether realtime refresh is needed; SQL defaults to a date-range result unless the spec declares daily or total rows."},
        "da_delivery_contract": {"language": "zh-CN", "da_owner_note": "DA only chooses date range and realtime refresh. Dashboard SQL defaults to one result for the selected date range unless SQL/spec explicitly declares daily or total rows.", "dashboard_boundary": {"sql_provides": ["表格输出字段", "日期范围参数", "显式输出形态", "展示格式规则"], "da_decides": ["查询日期范围", "是否需要实时刷新", "展示布局", "可视化呈现", "交互样式"]}, "grouping_total_policy": total_policy},
        "da_output_contract": {"result_shape": "table_dataset", "visualization_owner": "DA", "table_fields": fields, "sql_responsibility": "提供稳定中文表格输出字段、日期范围参数、显式输出形态和必要展示格式规则。", "da_responsibility": "只决定查询日期范围、是否需要实时刷新，以及展示布局/可视化呈现。", "output_usage_note": "默认看板 SQL 输出一个日期区间结果；DA 不另行生成日期维度或合计。"},
        "da_filter_contract": filter_contract,
        "parameters": [{"name": "start_date", "label": "开始日期", "type": "date", "default": "", "required": True, "visible": True, "visible_to_dashboard_user": True, "visible_to_external_user": True, "parameter_role": "time_range", "values": [], "sql_usage": "feeds params.ts_start directly; do not append fixed 00:00:00 suffix", "field_mapping": {"display_value": "${start_date}", "sql_condition": "source time >= params.ts_start"}}, {"name": "end_date", "label": "结束日期", "type": "date", "default": "", "required": True, "visible": True, "visible_to_dashboard_user": True, "visible_to_external_user": True, "parameter_role": "time_range", "values": [], "sql_usage": "feeds params.ts_end via date_add/exclusive bound when needed; do not append fixed 00:00:00 suffix", "field_mapping": {"display_value": "${end_date}", "sql_condition": "source time < params.ts_end"}}],
        "data_sources": query_spec_doc.get("data_sources", []),
        "intermediate_tables": query_spec_doc.get("intermediate_tables", []),
        "business_logic": {"source_query_logic_reference": query_sql_path, "logic_consistency": "Keep source query metric definitions, filters, exclusions, dedup grain, and calculation path unchanged.", "dashboard_adaptation_scope": ["parameterized_time_range", "table_output_contract"], "business_logic_changed": False, "change_validation_requirement": "If business logic changes, rerun validation or mark the dashboard artifact unverified."},
        "dimensions": dimensions,
        "metrics": metrics,
        "sql_output_contract": {"one_row_means": query_spec_doc.get("query_output_contract", {}).get("one_row_means", ""), "output_grain": output_grain, "output_shape": output_shape, "expected_fields": fields, "forbidden_fields": FORBIDDEN_FIELDS, "result_output_contract": output_contract, "field_display_rules": visual_rules, "display_contract_source": visual_source, "contains_total_rows": contains_total_rows, "total_row_indicator_field": fields[0] if contains_total_rows and fields else "", "total_row_label": TOTAL_ROW_LABEL if contains_total_rows else ""},
        "quality_gate": {"must_pass": ["linked_query", "linked_validation", "linked_run", "chinese_output_fields", "visual_review_contract_when_needed"], "status": "passed"},
    }
    if visual_rules:
        spec["visual_review_contract"] = {"contract_version": "dashboard_visual_review_v1", "scope": "dashboard_display_only", "visualization_owner": "DA", "field_display_rules": visual_rules, "display_contract_source": visual_source, "review_checks": ["比例/率字段 SQL 保留原始数值，DA 展示为百分比。"]}
    return spec


def dashboard_blockers(sql: str, result: dict[str, Any]) -> list[str]:
    fields = retained_output_fields(sql, result)
    missing = [field for field in fields if not has_chinese(field)]
    if missing:
        return ["Dashboard final output fields must be stable Chinese aliases: " + ", ".join(missing)]
    return []
