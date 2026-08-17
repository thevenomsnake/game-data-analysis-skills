#!/usr/bin/env python3
"""Plan exact grouped summaries and link grouped/overall query versions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from asset_provenance import build_generation_provenance, now_iso  # noqa: E402
from capability_registry import command_function_ids  # noqa: E402
from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from sql_facts import (  # noqa: E402
    build_sql_fact_bundle,
    final_select_projection,
    normalize_sql_text,
    parse_select_expression,
)


SUMMARY_PLAN_VERSION = "summary_feasibility_v1"
ANALYSIS_BUNDLE_VERSION = "query_analysis_bundle_v1"
ANALYSIS_BUNDLE_REF_VERSION = "query_analysis_bundle_ref_v1"
SUMMARY_ROUTES = {
    "not_grouped",
    "no_overall_needed",
    "single_exact",
    "single_with_components",
    "grouped_plus_overall",
}
FEASIBILITY_VALUES = {
    "exact_from_grouped",
    "exact_with_components",
    "requires_overall_query",
    "not_meaningful",
}
SEMANTIC_TYPES = {
    "additive",
    "distinct_count",
    "mean",
    "rate",
    "minimum",
    "maximum",
    "percentile",
    "distribution",
    "other",
}
GROUP_PARTITIONS = {"exclusive_exhaustive", "overlapping", "unknown", "not_applicable"}


def _field_key(value: str) -> str:
    return re.sub(r"[\s`\"'\[\]]+", "", str(value or "")).casefold()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unique(values: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = _field_key(text)
        if text and key not in seen:
            seen.add(key)
            rows.append(text)
    return rows


def _projection_rows(sql: str) -> list[dict[str, str]]:
    projection = final_select_projection(sql)
    if not projection:
        return []
    return [parse_select_expression(item) for item in projection.expressions]


def _semantic_type(alias: str, expression: str) -> str:
    lowered = f"{alias} {expression}".casefold()
    if re.search(r"\b(percentile|percentile_approx|approx_percentile|median)\s*\(", lowered) or re.search(
        r"(?:^|[_\s])(p\d{2}|median)(?:$|[_\s])|中位|分位", lowered
    ):
        return "percentile"
    if re.search(r"\bavg\s*\(", lowered) or re.search(r"平均|均值|(?:^|_)avg(?:_|$)|(?:^|_)mean(?:_|$)", lowered):
        return "mean"
    if re.search(r"\bcount\s*\(\s*distinct\b|approx_count_distinct\s*\(", lowered):
        return "distinct_count"
    if "/" in expression or re.search(r"占比|比例|转化率|留存率|命中率|(?:^|_)(?:rate|ratio|pct|percent|share)(?:_|$)", lowered):
        return "rate"
    if re.search(r"\bmin\s*\(", lowered):
        return "minimum"
    if re.search(r"\bmax\s*\(", lowered):
        return "maximum"
    if re.search(r"\b(?:sum|count)\s*\(", lowered):
        return "additive"
    return "other"


def _is_distribution_share(alias: str) -> bool:
    lowered = str(alias or "").casefold()
    return bool(re.search(r"占比|构成比|分布比例|(?:^|_)(?:share|distribution_pct)(?:_|$)", lowered))


def _bucket_dimension(dimensions: list[str]) -> str:
    return next(
        (
            item
            for item in dimensions
            if re.search(r"桶|区间|范围|bucket|bin", str(item or ""), flags=re.I)
        ),
        "",
    )


def _bucket_overall_field(dimension: str) -> str:
    base = re.sub(r"(?:桶排序|排序|桶|区间|范围|bucket|bin)", "", dimension, flags=re.I).strip(" _-")
    return f"整体{base or '指标'}平均值"


def _default_metric(alias: str, expression: str, group_partition: str) -> dict[str, Any]:
    semantic_type = _semantic_type(alias, expression)
    feasibility = "requires_overall_query"
    statistic = f"整体{alias}"
    reason = "The grouped output does not contain exact source-level evidence for the overall statistic."
    if semantic_type in {"additive", "minimum", "maximum"}:
        feasibility = "exact_from_grouped"
        statistic = "合计" if semantic_type == "additive" else ("整体最小值" if semantic_type == "minimum" else "整体最大值")
        reason = "The aggregate is algebraically composable across complete grouped rows."
    elif semantic_type == "distinct_count" and group_partition == "exclusive_exhaustive":
        feasibility = "exact_from_grouped"
        statistic = "整体去重数"
        reason = "The declared groups are mutually exclusive and exhaustive at the distinct-entity grain."
    elif semantic_type == "distinct_count":
        reason = "Distinct entities may appear in more than one group, so grouped distinct counts are not additive."
    elif semantic_type == "mean":
        statistic = "整体平均"
        reason = "A grouped mean cannot be averaged or reweighted without exact unrounded numerator and weight fields."
    elif semantic_type == "rate":
        statistic = "整体比率"
        if _is_distribution_share(alias):
            feasibility = "not_meaningful"
            reason = "A repeated 100% total is not a useful summary for a normalized distribution."
        else:
            reason = "An overall rate requires exact numerator and denominator fields, not an average of displayed rates."
    elif semantic_type == "percentile":
        statistic = alias
        reason = "Percentiles are not composable from grouped percentiles and must be recomputed from source-level values."
    return {
        "metric": alias,
        "semantic_type": semantic_type,
        "overall_statistic": statistic,
        "feasibility": feasibility,
        "grouped_fields": [alias],
        "numerator_field": "",
        "denominator_field": "",
        "overall_fields": [alias] if feasibility == "requires_overall_query" else [],
        "reason": reason,
    }


def _merge_metric_rows(inferred: list[dict[str, Any]], supplied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = copy.deepcopy(inferred)
    positions = {_field_key(item.get("metric", "")): index for index, item in enumerate(rows)}
    for supplied_row in supplied:
        if not isinstance(supplied_row, dict):
            raise ValueError("Summary metric contracts must be JSON objects.")
        metric = str(supplied_row.get("metric") or "").strip()
        if not metric:
            raise ValueError("Every summary metric contract needs a metric name.")
        key = _field_key(metric)
        if key in positions:
            rows[positions[key]].update(copy.deepcopy(supplied_row))
        else:
            rows.append(copy.deepcopy(supplied_row))
            positions[key] = len(rows) - 1
    return rows


def _aggregate_argument(expression: str, function: str) -> str:
    match = re.fullmatch(rf"(?is)\s*{re.escape(function)}\s*\((.*)\)\s*", str(expression or ""))
    return re.sub(r"\s+", "", match.group(1)).casefold() if match else ""


def _infer_exact_components(
    metrics: list[dict[str, Any]],
    projections: list[dict[str, str]],
    *,
    group_partition: str,
) -> None:
    if group_partition != "exclusive_exhaustive":
        return
    sums: dict[str, str] = {}
    counts: dict[str, str] = {}
    for row in projections:
        alias = str(row.get("alias") or "")
        expression = str(row.get("expression") or "")
        sum_arg = _aggregate_argument(expression, "sum")
        count_arg = _aggregate_argument(expression, "count")
        if sum_arg and alias:
            sums[sum_arg] = alias
        if count_arg and count_arg != "*" and alias:
            counts[count_arg] = alias
    for metric in metrics:
        if metric.get("semantic_type") != "mean":
            continue
        projection = next(
            (row for row in projections if _field_key(row.get("alias", "")) == _field_key(metric.get("metric", ""))),
            None,
        )
        avg_arg = _aggregate_argument(str((projection or {}).get("expression") or ""), "avg")
        if not avg_arg or avg_arg not in sums or avg_arg not in counts:
            continue
        metric.update(
            {
                "feasibility": "exact_with_components",
                "grouped_fields": _unique(
                    [*list(metric.get("grouped_fields") or []), sums[avg_arg], counts[avg_arg]]
                ),
                "numerator_field": sums[avg_arg],
                "denominator_field": counts[avg_arg],
                "overall_fields": [],
                "reason": "The SQL outputs an unrounded SUM and matching non-null COUNT for the same mean expression.",
            }
        )


def _route_for_metrics(metrics: list[dict[str, Any]], *, grouped: bool) -> str:
    if not grouped:
        return "not_grouped"
    feasibility = {str(item.get("feasibility") or "") for item in metrics}
    if "requires_overall_query" in feasibility:
        return "grouped_plus_overall"
    if "exact_with_components" in feasibility:
        return "single_with_components"
    if feasibility and feasibility <= {"not_meaningful"}:
        return "no_overall_needed"
    return "single_exact"


def build_summary_plan(
    sql: str,
    *,
    root: Path | None = None,
    group_partition: str = "unknown",
    target_overall_grain: str = "",
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a conservative metric-level composability plan for one SQL result."""

    group_partition = str(group_partition or "unknown")
    if group_partition not in GROUP_PARTITIONS:
        raise ValueError(f"Unsupported group_partition `{group_partition}`.")
    contract = copy.deepcopy(contract or {})
    facts = build_sql_fact_bundle(sql, kind="QUERY", root=root)
    final_fields = list(facts.get("final_fields") or [])
    inferred_dimensions = [str(item.get("field") or item.get("label") or "") for item in facts.get("dimensions", [])]
    if not inferred_dimensions and (facts.get("performance") or {}).get("has_group_by"):
        inferred_dimensions = [
            row["alias"]
            for row in _projection_rows(sql)
            if row.get("alias")
            and _semantic_type(row["alias"], row["expression"]) == "other"
            and re.fullmatch(r"(?:[`\"\[]?[A-Za-z_\u4e00-\u9fff][\w.\u4e00-\u9fff]*[`\"\]]?)", row["expression"].strip())
        ]
    group_dimensions = _unique(list(contract.get("group_dimensions") or inferred_dimensions))
    retained_dimensions = _unique(list(contract.get("retained_dimensions") or []))
    if contract.get("group_partition"):
        group_partition = str(contract["group_partition"])
    grouped = bool(group_dimensions)
    dimension_keys = {_field_key(item) for item in [*group_dimensions, *retained_dimensions]}
    projection_rows = _projection_rows(sql)
    inferred_metrics = [
        _default_metric(row["alias"], row["expression"], group_partition)
        for row in projection_rows
        if row.get("alias") and _field_key(row["alias"]) not in dimension_keys
    ]
    _infer_exact_components(inferred_metrics, projection_rows, group_partition=group_partition)
    metric_rows = _merge_metric_rows(inferred_metrics, list(contract.get("metrics") or []))

    bucket = _bucket_dimension(group_dimensions)
    if bucket and not any(item.get("semantic_type") in {"mean", "percentile", "distribution"} for item in metric_rows):
        overall_field = _bucket_overall_field(bucket)
        metric_rows.append(
            {
                "metric": f"{bucket}源值整体统计",
                "semantic_type": "distribution",
                "overall_statistic": "整体平均",
                "feasibility": "requires_overall_query",
                "grouped_fields": _unique(
                    field
                    for item in metric_rows
                    for field in list(item.get("grouped_fields") or [])
                ),
                "numerator_field": "",
                "denominator_field": "",
                "overall_fields": [overall_field],
                "reason": "Bucket counts do not retain the exact pre-bucket values needed for a source-level mean or percentile.",
            }
        )

    route = _route_for_metrics(metric_rows, grouped=grouped)
    overall_required_fields = _unique(
        field
        for item in metric_rows
        if item.get("feasibility") == "requires_overall_query"
        for field in list(item.get("overall_fields") or [])
    )
    plan = {
        "schema_version": SUMMARY_PLAN_VERSION,
        "grouped_output": grouped,
        "group_dimensions": group_dimensions,
        "retained_dimensions": retained_dimensions,
        "group_partition": group_partition if grouped else "not_applicable",
        "target_overall_grain": str(
            contract.get("target_overall_grain") or target_overall_grain or (", ".join(retained_dimensions) if retained_dimensions else "single overall row")
        ),
        "routing": route,
        "metrics": metric_rows,
        "overall_required_fields": overall_required_fields,
        "decision_reason": str(contract.get("decision_reason") or "Metric-level exact composability preflight."),
    }
    problems = validate_summary_plan(sql, plan, role="grouped" if grouped else "standalone", root=root)
    if problems:
        raise ValueError("Invalid summary feasibility plan: " + "; ".join(problems))
    plan["metric_contract_fingerprint"] = summary_plan_fingerprint(plan)
    return plan


def summary_plan_fingerprint(plan: dict[str, Any]) -> str:
    contract = {
        "schema_version": plan.get("schema_version"),
        "group_dimensions": plan.get("group_dimensions", []),
        "retained_dimensions": plan.get("retained_dimensions", []),
        "group_partition": plan.get("group_partition"),
        "target_overall_grain": plan.get("target_overall_grain"),
        "metrics": plan.get("metrics", []),
        "overall_required_fields": plan.get("overall_required_fields", []),
    }
    return _canonical_hash(contract)


def validate_summary_plan(
    sql: str,
    plan: dict[str, Any],
    *,
    role: str,
    root: Path | None = None,
) -> list[str]:
    problems: list[str] = []
    if plan.get("schema_version") != SUMMARY_PLAN_VERSION:
        problems.append(f"schema_version must be {SUMMARY_PLAN_VERSION}")
        return problems
    if plan.get("routing") not in SUMMARY_ROUTES:
        problems.append("routing is invalid")
    if plan.get("group_partition") not in GROUP_PARTITIONS:
        problems.append("group_partition is invalid")
    metrics = plan.get("metrics")
    if not isinstance(metrics, list):
        problems.append("metrics must be an array")
        return problems
    facts = build_sql_fact_bundle(sql, kind="QUERY", root=root)
    final_fields = {_field_key(item) for item in facts.get("final_fields", [])}
    group_dimensions = _unique(list(plan.get("group_dimensions") or []))
    retained_dimensions = _unique(list(plan.get("retained_dimensions") or []))
    if plan.get("grouped_output") and not group_dimensions:
        problems.append("grouped_output requires at least one group_dimension")
    if role == "grouped":
        for field in [*group_dimensions, *retained_dimensions]:
            if _field_key(field) not in final_fields:
                problems.append(f"grouped SQL does not output declared dimension `{field}`")
    elif role == "overall":
        for field in group_dimensions:
            if _field_key(field) in final_fields:
                problems.append(f"overall SQL must remove grouped dimension `{field}`")
        for field in retained_dimensions:
            if _field_key(field) not in final_fields:
                problems.append(f"overall SQL does not retain dimension `{field}`")
        for field in plan.get("overall_required_fields", []):
            if _field_key(field) not in final_fields:
                problems.append(f"overall SQL does not output required field `{field}`")

    for metric in metrics:
        if not isinstance(metric, dict):
            problems.append("metric rows must be objects")
            continue
        name = str(metric.get("metric") or "").strip()
        semantic_type = str(metric.get("semantic_type") or "")
        feasibility = str(metric.get("feasibility") or "")
        if not name:
            problems.append("metric name is required")
        if semantic_type not in SEMANTIC_TYPES:
            problems.append(f"metric `{name}` has invalid semantic_type")
        if feasibility not in FEASIBILITY_VALUES:
            problems.append(f"metric `{name}` has invalid feasibility")
        if not str(metric.get("reason") or "").strip():
            problems.append(f"metric `{name}` needs a concrete reason")
        if role == "grouped":
            for field in metric.get("grouped_fields", []):
                if _field_key(field) not in final_fields:
                    problems.append(f"metric `{name}` references missing grouped field `{field}`")
        if feasibility == "exact_with_components":
            numerator = str(metric.get("numerator_field") or "").strip()
            denominator = str(metric.get("denominator_field") or "").strip()
            if not numerator or not denominator:
                problems.append(f"metric `{name}` needs exact numerator_field and denominator_field")
            if role == "grouped":
                for field in [numerator, denominator]:
                    if field and _field_key(field) not in final_fields:
                        problems.append(f"metric `{name}` support field `{field}` is not output by grouped SQL")
            if semantic_type in {"mean", "rate"} and plan.get("group_partition") != "exclusive_exhaustive":
                problems.append(
                    f"metric `{name}` needs exclusive_exhaustive denominator units for exact component recomposition"
                )
        if semantic_type == "percentile" and feasibility in {"exact_from_grouped", "exact_with_components"}:
            problems.append(f"percentile metric `{name}` cannot be composed from grouped scalar results")
        if semantic_type == "distinct_count" and feasibility == "exact_from_grouped" and plan.get("group_partition") != "exclusive_exhaustive":
            problems.append(f"distinct metric `{name}` needs exclusive_exhaustive groups for exact summation")
        if feasibility == "requires_overall_query" and not list(metric.get("overall_fields") or []):
            problems.append(f"metric `{name}` must declare overall_fields")

    expected_route = _route_for_metrics(metrics, grouped=bool(plan.get("grouped_output")))
    if plan.get("routing") != expected_route:
        problems.append(f"routing must be `{expected_route}` for the declared metric feasibility")
    required = _unique(
        field
        for metric in metrics
        if metric.get("feasibility") == "requires_overall_query"
        for field in list(metric.get("overall_fields") or [])
    )
    if {_field_key(item) for item in required} != {_field_key(item) for item in plan.get("overall_required_fields", [])}:
        problems.append("overall_required_fields must equal the union of metric overall_fields")
    fingerprint = str(plan.get("metric_contract_fingerprint") or "")
    if fingerprint and fingerprint != summary_plan_fingerprint(plan):
        problems.append("metric_contract_fingerprint is stale")
    return _unique(problems)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected one JSON object: {path}")
    return value


def _resolve_project_file(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Managed analysis files must stay inside project root `{root}`: {path}") from exc
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    return path


def _canonical_filters(facts: dict[str, Any]) -> list[str]:
    return sorted(
        re.sub(r"\s+", " ", str(item.get("condition") or "")).strip().casefold()
        for item in facts.get("filters", [])
        if str(item.get("condition") or "").strip()
    )


def load_analysis_bundle(root: Path, value: str | Path) -> tuple[Path, dict[str, Any]]:
    text = str(value or "").strip()
    path = root / "query_workspace" / "bundles" / f"{text}.json" if re.fullmatch(r"qab-[a-f0-9]{12}", text) else Path(text)
    path = _resolve_project_file(root, path)
    bundle = _load_json(path)
    if bundle.get("schema_version") != ANALYSIS_BUNDLE_VERSION:
        raise ValueError(f"Unsupported analysis bundle schema: {bundle.get('schema_version')}")
    return path, bundle


def create_analysis_bundle(
    *,
    root: Path,
    grouped_sql: str | Path,
    overall_sql: str | Path,
    plan: dict[str, Any],
    title: str,
    purpose: str,
) -> dict[str, Any]:
    """Persist one exact grouped/overall query bundle and update both version contracts."""

    from sql_query_workspace import (  # noqa: PLC0415
        DELIVERY_READY_STATUSES,
        _find_entry,
        _index_files,
        _write_transaction,
        json_text,
        load_index,
        query_delivery_receipt,
        read_json,
        resolve_project_path,
    )

    root = root.resolve()
    grouped_path = _resolve_project_file(root, grouped_sql)
    overall_path = _resolve_project_file(root, overall_sql)
    grouped_text = normalize_sql_text(grouped_path.read_text(encoding="utf-8-sig"))
    overall_text = normalize_sql_text(overall_path.read_text(encoding="utf-8-sig"))
    if plan.get("routing") != "grouped_plus_overall":
        raise ValueError("Analysis bundles are created only when summary routing is grouped_plus_overall.")
    grouped_problems = validate_summary_plan(grouped_text, plan, role="grouped", root=root)
    overall_problems = validate_summary_plan(overall_text, plan, role="overall", root=root)
    if grouped_problems or overall_problems:
        raise ValueError("Analysis bundle SQL does not satisfy the summary plan: " + "; ".join(grouped_problems + overall_problems))

    grouped_facts = build_sql_fact_bundle(grouped_text, kind="QUERY", root=root)
    overall_facts = build_sql_fact_bundle(overall_text, kind="QUERY", root=root)
    if grouped_facts.get("params") != overall_facts.get("params"):
        raise ValueError("Grouped and overall SQL parameter snapshots differ.")
    grouped_sources = sorted(str(item).casefold() for item in grouped_facts.get("source_tables", []))
    overall_sources = sorted(str(item).casefold() for item in overall_facts.get("source_tables", []))
    if grouped_sources != overall_sources:
        raise ValueError("Grouped and overall SQL physical source contracts differ.")
    if _canonical_filters(grouped_facts) != _canonical_filters(overall_facts):
        raise ValueError("Grouped and overall SQL filter contracts differ.")

    index = load_index(root)
    grouped_rel = grouped_path.relative_to(root).as_posix()
    overall_rel = overall_path.relative_to(root).as_posix()
    grouped_entry, grouped_version = _find_entry(index, sql_path=grouped_rel)
    overall_entry, overall_version = _find_entry(index, sql_path=overall_rel)
    if not grouped_entry or not grouped_version or not overall_entry or not overall_version:
        raise ValueError("Both grouped and overall SQL must be exact indexed query workspace versions.")
    if grouped_entry.get("query_id") == overall_entry.get("query_id"):
        raise ValueError("Grouped and overall SQL have different grains and must use separate query families.")

    metric_fingerprint = summary_plan_fingerprint(plan)
    bundle_hash = _canonical_hash(
        {
            "grouped": grouped_version.get("sql_fingerprint"),
            "overall": overall_version.get("sql_fingerprint"),
            "metric_contract": metric_fingerprint,
        }
    )
    bundle_id = f"qab-{bundle_hash[:12]}"
    bundle_rel = (Path("query_workspace") / "bundles" / f"{bundle_id}.json").as_posix()
    created_at = now_iso()
    members = []
    for role, entry, version, facts in [
        ("grouped", grouped_entry, grouped_version, grouped_facts),
        ("overall", overall_entry, overall_version, overall_facts),
    ]:
        members.append(
            {
                "role": role,
                "query_id": str(entry.get("query_id") or ""),
                "version": int(version.get("version") or 0),
                "path": str(version.get("path") or ""),
                "sql_fingerprint": str(version.get("sql_fingerprint") or ""),
                "logic_fingerprint": str(version.get("logic_fingerprint") or ""),
                "expected_fields": list(facts.get("final_fields") or []),
            }
        )
    bundle = {
        "schema_version": ANALYSIS_BUNDLE_VERSION,
        "bundle_id": bundle_id,
        "title": str(title).strip(),
        "purpose": str(purpose).strip(),
        "status": "awaiting_results",
        "summary_plan": copy.deepcopy(plan),
        "metric_contract_fingerprint": metric_fingerprint,
        "parameter_snapshot": copy.deepcopy(grouped_facts.get("params") or {}),
        "parameter_fingerprint": _canonical_hash(grouped_facts.get("params") or {}),
        "source_tables": list(grouped_facts.get("source_tables") or []),
        "filter_contract": _canonical_filters(grouped_facts),
        "members": members,
        "result_bindings": {},
        "visualization": {},
        "generation_provenance": build_generation_provenance(
            generator_script="sql_summary_planner.py",
            workflow="query_analysis_bundle",
            artifact_kind="QUERY_ANALYSIS_BUNDLE",
            generated_at=created_at,
            source="skill_generated",
            extra={"bundle_id": bundle_id},
        ),
        "created_at": created_at,
        "updated_at": created_at,
    }

    files: dict[Path, str | bytes] = {root / bundle_rel: json_text(bundle)}
    for role, entry, version in [
        ("grouped", grouped_entry, grouped_version),
        ("overall", overall_entry, overall_version),
    ]:
        reference = {
            "contract_version": ANALYSIS_BUNDLE_REF_VERSION,
            "bundle_id": bundle_id,
            "role": role,
            "path": bundle_rel,
            "metric_contract_fingerprint": metric_fingerprint,
        }
        version["summary_plan"] = copy.deepcopy(plan)
        version["analysis_bundle"] = copy.deepcopy(reference)
        gate_ok = str((version.get("generation_gate") or {}).get("status") or "") == "ok"
        version["delivery_ready"] = str(version.get("status") or "") in DELIVERY_READY_STATUSES and gate_ok
        version.setdefault("status_history", []).append(
            {
                "status": str(version.get("status") or "runnable"),
                "at": created_at,
                "reason": f"Linked as the {role} member of analysis bundle {bundle_id}; paired SQL delivery is complete.",
            }
        )
        version["updated_at"] = created_at
        if int(entry.get("current_version") or 0) == int(version.get("version") or 0):
            entry["summary_plan"] = copy.deepcopy(plan)
            entry["analysis_bundle"] = copy.deepcopy(reference)
            entry["updated_at"] = created_at
        meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
        meta = read_json(meta_path, {})
        meta["summary_plan"] = copy.deepcopy(plan)
        meta["analysis_bundle"] = copy.deepcopy(reference)
        meta["delivery_ready"] = bool(version.get("delivery_ready"))
        meta["status_history"] = copy.deepcopy(version.get("status_history") or [])
        meta["updated_at"] = created_at
        files[meta_path] = json_text(meta)
        seed_ref = str(version.get("formalize_seed_path") or "")
        if seed_ref:
            seed_path = resolve_project_path(root, seed_ref)
            seed = read_json(seed_path, {})
            seed["summary_plan"] = copy.deepcopy(plan)
            seed["analysis_bundle"] = copy.deepcopy(reference)
            files[seed_path] = json_text(seed)
    files.update(_index_files(root, index))
    _write_transaction(files)

    receipts = [
        query_delivery_receipt(root, query_id=member["query_id"], version_number=member["version"])
        for member in members
    ]
    status = "ready" if all(item.get("status") == "ready" for item in receipts) else "blocked"
    return {
        "schema_version": ANALYSIS_BUNDLE_VERSION,
        "status": status,
        "bundle_id": bundle_id,
        "path": bundle_rel,
        "absolute_path": str((root / bundle_rel).resolve()),
        "members": members,
        "delivery_receipts": receipts,
    }


def _format_result(value: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return f"{value.get('status', 'ready')}: {value.get('schema_version', '')}\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Classify whether grouped metrics can produce exact useful overall summaries")
    plan.add_argument("--root", required=True)
    plan.add_argument("--sql-file", required=True)
    plan.add_argument("--group-partition", choices=sorted(GROUP_PARTITIONS), default="unknown")
    plan.add_argument("--target-overall-grain", default="")
    plan.add_argument("--contract-file", default="")
    plan.add_argument("--output", default="", help="Optional project-local JSON path")
    plan.add_argument("--format", choices=["json", "text"], default="json")
    add_function_gate_arguments(plan, selection_help="Use [QUERY].")

    bundle = sub.add_parser("create-bundle", help="Link exact grouped and overall workspace SQL versions")
    bundle.add_argument("--root", required=True)
    bundle.add_argument("--grouped-sql", required=True)
    bundle.add_argument("--overall-sql", required=True)
    bundle.add_argument("--plan-file", required=True)
    bundle.add_argument("--title", required=True)
    bundle.add_argument("--purpose", required=True)
    bundle.add_argument("--format", choices=["json", "text"], default="json")
    add_function_gate_arguments(bundle, selection_help="Use [QUERY].")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("sql_summary_planner.py", args.command),
            purpose=f"sql_summary_planner.py {args.command}",
        )
        require_user_request(args.user_request, purpose=f"sql_summary_planner.py {args.command}")
        root = Path(args.root).resolve()
        if args.command == "plan":
            sql_file = _resolve_project_file(root, args.sql_file)
            contract = _load_json(_resolve_project_file(root, args.contract_file)) if args.contract_file else {}
            result = build_summary_plan(
                normalize_sql_text(sql_file.read_text(encoding="utf-8-sig")),
                root=root,
                group_partition=args.group_partition,
                target_overall_grain=args.target_overall_grain,
                contract=contract,
            )
            if args.output:
                output = Path(args.output)
                if not output.is_absolute():
                    output = root / output
                output = output.resolve()
                try:
                    output.relative_to(root)
                except ValueError as exc:
                    raise ValueError("Summary plan output must stay inside the project root.") from exc
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result = {**result, "output_path": output.relative_to(root).as_posix()}
        else:
            plan_path = _resolve_project_file(root, args.plan_file)
            result = create_analysis_bundle(
                root=root,
                grouped_sql=args.grouped_sql,
                overall_sql=args.overall_sql,
                plan=_load_json(plan_path),
                title=args.title,
                purpose=args.purpose,
            )
        print(_format_result(result, args.format), end="")
        return 0 if result.get("status") != "blocked" else 1
    except FunctionGateError as exc:
        return exit_with_gate_error(exc)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
