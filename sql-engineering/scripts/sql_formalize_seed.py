#!/usr/bin/env python3
"""Create a compact formalize seed next to a temporary SQL file."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from capability_registry import command_function_ids  # noqa: E402
from sql_formalize import (  # noqa: E402
    config_fingerprint,
    ensure_rule_context_generation_gate,
    performance_level,
    project_rules_fingerprint,
    project_staging_directory,
    read_text,
    run_rule_context,
    sha256_text,
    write_text,
)
from sql_param_normalizer import normalize_query_sql  # noqa: E402
from sql_project import project_context_snapshot, read_json, read_project_config, slugify, validate_project_config  # noqa: E402
from sql_query_workspace import find_query_reference, project_relative  # noqa: E402
from sql_execution_adapter import rebase_execution_route_for_sql, route_matches_context, route_receipt_path  # noqa: E402
from sql_semantic_summary import build_repository_summary, needs_llm_summary  # noqa: E402
from sql_facts import build_sql_fact_bundle  # noqa: E402
from spec_utils import write_json_object  # noqa: E402


def default_output_path(sql_file: Path) -> Path:
    return sql_file.with_name(f"{sql_file.stem}.formalize_seed.json")


def empty_result_placeholder() -> dict[str, Any]:
    return {
        "file_name": "",
        "file_path": "",
        "file_type": "",
        "row_count": None,
        "columns": [],
        "schema_fingerprint": "",
        "sample_rows": [],
        "notes": "Temporary SQL seed has no user-run result file yet.",
    }


def public_rule_context(rule_context: dict[str, Any]) -> dict[str, Any]:
    """Keep the seed useful without turning it into a large review artifact."""
    keep_keys = [
        "status",
        "mode",
        "lifecycle_stage",
        "request_envelope",
        "rule_application",
        "active_rules",
        "applied_rules",
        "inherited_rules",
        "excluded_rules",
        "hard_constraints",
        "candidate_sql_check",
        "project_contract_check",
        "project_time_contract",
        "generation_gate",
        "reverse_source_audit",
        "source_metric_audit",
        "name_logic_mismatches",
    ]
    return {key: rule_context.get(key) for key in keep_keys if key in rule_context}


def build_seed(args, tmp_dir: Path) -> dict[str, Any]:
    started = time.perf_counter()
    root = Path(args.root).resolve()
    sql_file = Path(args.sql_file).resolve()
    title = args.title or sql_file.stem
    output = Path(args.output).resolve() if args.output else default_output_path(sql_file)
    warnings: list[str] = []
    blockers: list[str] = []
    steps: list[dict[str, Any]] = []
    last_mark = started

    def mark(step: str, status: str = "done", detail: str = "") -> None:
        nonlocal last_mark
        now = time.perf_counter()
        steps.append(
            {
                "step": step,
                "status": status,
                "elapsed_ms": int((now - started) * 1000),
                "duration_ms": int((now - last_mark) * 1000),
                "detail": detail,
            }
        )
        last_mark = now

    try:
        relative_sql_file = project_relative(root, sql_file)
        relative_output = project_relative(root, output)
    except ValueError as exc:
        blockers.append(str(exc))
        relative_sql_file = ""
        relative_output = ""
    expected_output = default_output_path(sql_file)
    if output != expected_output:
        blockers.append(
            "Formalize seed output must stay adjacent to its indexed SQL as "
            f"`{expected_output.name}`; arbitrary seed paths are not supported."
        )

    config = read_project_config(root)
    config_problems = validate_project_config(config, "QUERY")
    if config_problems and not args.allow_incomplete_project_config:
        blockers.extend(config_problems)
    if not sql_file.exists():
        blockers.append(f"SQL file not found: {sql_file}")
    workspace_reference = find_query_reference(root, sql_file) if sql_file.exists() and relative_sql_file else None
    if sql_file.exists() and relative_sql_file and not workspace_reference:
        blockers.append(
            "Formalize seeds may only be written for an SQL version already registered in "
            "query_workspace/index.json. Save the query with sql_query_workspace.py first."
        )
    if blockers:
        mark("preflight_inputs", "blocked", "Required project/source context is incomplete.")
        return {
            "status": "blocked",
            "blockers": blockers,
            "warnings": warnings,
            "root": str(root),
            "sql_file": str(sql_file),
            "output": str(output),
            "steps": steps,
        }

    raw_sql = read_text(sql_file)
    normalized = normalize_query_sql(raw_sql, config)
    warnings.extend(normalized.warnings)
    normalized_file = tmp_dir / "query.sql"
    write_text(normalized_file, normalized.sql)
    mark("normalize_sql", detail="params CTE normalized" if normalized.changed else "source SQL already normalized")

    parent_route = None
    for candidate in [
        (workspace_reference or {}).get("execution_route"),
    ]:
        if route_matches_context(candidate, raw_sql, config):
            parent_route = candidate
            break
    if parent_route is None:
        sidecar = route_receipt_path(sql_file)
        if sidecar.exists():
            candidate = read_json(sidecar, {})
            if route_matches_context(candidate, raw_sql, config):
                parent_route = candidate
    normalized_route = (
        rebase_execution_route_for_sql(normalized.sql, config, parent_route)
        if parent_route
        else None
    )

    sql_facts = build_sql_fact_bundle(normalized.sql, kind="QUERY", root=root)
    analysis = sql_facts["analysis"]
    mark("analyze_sql", detail=f"{len(sql_facts.get('source_tables', []))} physical sources detected")

    # A standalone seed describes a temporary asset; it does not itself authorize
    # bypassing canonical rules. Explicit one-query overrides are captured by the
    # workspace save flow from the verbatim user request and then reused here.
    rule_context = run_rule_context(
        root,
        normalized_file,
        args.user_request,
        mode="generation",
        lifecycle_stage="temporary_query",
        execution_route=normalized_route,
    )
    generation_gate = ensure_rule_context_generation_gate(
        rule_context,
        sql=normalized.sql,
        config=config,
        execution_route=normalized_route,
    )
    execution_route = (
        (rule_context.get("project_contract_check") or {}).get("execution_route")
        if isinstance(rule_context.get("project_contract_check"), dict)
        else None
    )
    gate_status = str(generation_gate.get("status") or "not_run")
    if gate_status in {"conflict", "error"}:
        blockers.extend([str(item) for item in generation_gate.get("blockers", [])] or ["temporary SQL generation gate failed"])
    mark("rule_context", "blocked" if gate_status in {"conflict", "error"} else "done", gate_status)

    result = empty_result_placeholder()
    summary = build_repository_summary(
        root=root,
        sql=normalized.sql,
        title=title,
        analysis=analysis,
        result=result,
        rule_context=rule_context,
        sql_facts=sql_facts,
    )
    summary["result_evidence"] = {
        "status": "pending_user_run",
        "row_count": None,
        "columns": [],
        "schema_fingerprint": "",
        "file_name": "",
    }
    if needs_llm_summary(summary):
        warnings.append("repository_summary is low-confidence; formalization may require a better title/comment or manual seed repair.")
    mark("repository_summary", "warn" if needs_llm_summary(summary) else "done", str(summary.get("semantic_summary_quality") or "deterministic"))

    performance: dict[str, Any] = {}
    try:
        performance = performance_level(
            normalized.sql,
            config,
            "QUERY",
            reusable=True,
            sql_facts=sql_facts,
            execution_route=execution_route,
        )
        if performance.get("preflight_status") == "block":
            warnings.extend([str(item) for item in performance.get("risk_items", [])] or ["performance preflight has blockers; formalization will rerun or block if unresolved."])
        perf_detail = f"{performance.get('optimization_tier', 'unknown')} score={performance.get('preflight_score', 0)}"
        mark("performance_preflight", "warn" if performance.get("preflight_status") == "block" else "done", perf_detail)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"performance preflight seed skipped: {exc}")
        mark("performance_preflight", "warn", "skipped")

    seed = {
        "schema_version": "formalize_seed_v2",
        "source": "sql_formalize_seed.py",
        "title": title,
        "slug": slugify(args.slug or title, "formalized-sql"),
        "project_root": ".",
        "project_context": project_context_snapshot(
            config or {},
            normalized.sql,
            execution_route=execution_route,
        ),
        "project_config_fingerprint": config_fingerprint(config),
        "source_sql_file": relative_sql_file,
        "source_sql_fingerprint": sha256_text(raw_sql),
        "normalized_sql_fingerprint": sha256_text(normalized.sql),
        "logic_fingerprint": sql_facts["logic_fingerprint"],
        "normalized_changed": normalized.changed,
        "normalizer_warnings": normalized.warnings,
        "project_rules_fingerprint": project_rules_fingerprint(root),
        "rule_context_mode": "generation",
        "analysis": analysis,
        "sql_fact_bundle": sql_facts,
        "rule_context": public_rule_context(rule_context),
        "request_envelope": copy.deepcopy(rule_context.get("request_envelope") or {}),
        "rule_application": copy.deepcopy(rule_context.get("rule_application") or {}),
        "query_workspace_ref": {
            "query_id": workspace_reference.get("query_id", ""),
            "version": workspace_reference.get("version"),
            "path": workspace_reference.get("path", ""),
            "sql_fingerprint": workspace_reference.get("sql_fingerprint", ""),
        },
        "performance_level": performance,
        "repository_summary": summary,
        "reuse_contract": {
            "analysis": "Reusable when logic_fingerprint matches; time parameter values may change, business parameters may not.",
            "performance_level": "Reusable when normalized_sql_fingerprint and project_config_fingerprint match; result-field pruning that changes SQL forces a fresh preflight.",
            "repository_summary": "Reusable when logic_fingerprint matches; result_evidence is replaced during formalize.",
            "rule_context": "Temporary diagnostics only; formal save must rerun rule-context in formalize mode.",
        },
    }
    mark("build_seed", detail=f"summary={summary.get('semantic_summary_quality')}")
    return {
        "status": "blocked" if blockers else "ready",
        "blockers": blockers,
        "warnings": warnings,
        "root": str(root),
        "sql_file": str(sql_file),
        "output": str(output),
        "output_path": relative_output,
        "steps": steps,
        "seed": seed,
    }


def render_text(result: dict[str, Any]) -> str:
    lines = [f"status: {result.get('status')}", f"sql_file: {result.get('sql_file')}", f"output: {result.get('output')}"]
    if result.get("blockers"):
        lines.append("blockers:")
        lines.extend(f"  - {item}" for item in result["blockers"])
    if result.get("warnings"):
        lines.append("warnings:")
        lines.extend(f"  - {item}" for item in result["warnings"])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Project root, such as sql-projects/DEMO_ANALYTICS")
    parser.add_argument("--sql-file", required=True, help="Temporary SQL file to describe")
    parser.add_argument("--title", help="Human-readable temporary SQL title. Defaults to SQL filename.")
    parser.add_argument("--slug", help="Future formalization slug hint. Defaults to slugified title.")
    parser.add_argument("--output", help="Seed output path. Defaults to <sql-stem>.formalize_seed.json.")
    parser.add_argument("--allow-incomplete-project-config", action="store_true", help="Allow seed creation with incomplete formal QUERY config; formalization will still block later.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(parser, selection_help="Optional route such as 【查询SQL】, [QUERY], or [SQL_FORMALIZE].")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("sql_formalize_seed.py"),
            purpose="temporary SQL formalize seed",
        )
        require_user_request(args.user_request, purpose="temporary SQL formalize seed")
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    try:
        with project_staging_directory(Path(args.root), "sql_formalize_seed_") as tmp:
            result = build_seed(args, Path(tmp))
            if result.get("status") == "ready" and not args.dry_run:
                output = Path(result["output"])
                write_json_object(output, result["seed"])
                result["status"] = "written"
    except SystemExit as exc:
        result = {"status": "error", "blockers": [str(exc) or f"internal tool exited with code {exc.code}"]}
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "blockers": [str(exc)]}
    safe = {key: value for key, value in result.items() if key != "seed"}
    if args.format == "json":
        print(json.dumps(safe, ensure_ascii=False, indent=2))
    else:
        print(render_text(safe), end="")
    return 3 if result.get("status") == "error" else 1 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
