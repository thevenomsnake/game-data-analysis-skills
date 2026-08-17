#!/usr/bin/env python3
"""Backfill historical formal QUERY artifacts into the indexed query workspace."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
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
from sql_query_workspace import (  # noqa: E402
    _write_transaction,
    file_sha256,
    find_query_reference,
    json_text,
    mark_historical_formal_backfill,
    normalize_sql_text,
    now_iso,
    origin_contract,
    save_query,
    sql_fingerprint,
)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def version_number(path_value: str) -> int:
    match = re.search(r"/v(\d{3})\.sql$", str(path_value or "").replace("\\", "/"), flags=re.I)
    return int(match.group(1)) if match else 0


def artifact_slug(item: dict[str, Any]) -> str:
    value = str(item.get("slug") or "").strip()
    if value:
        return value
    parts = str(item.get("path") or "").replace("\\", "/").split("/")
    return parts[-2] if len(parts) >= 2 else "historical-query"


def summary_list(value: Any, *keys: str) -> list[str]:
    rows: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            text = next((str(item.get(key) or "").strip() for key in keys if item.get(key)), "")
        else:
            text = str(item or "").strip()
        if text and text not in rows:
            rows.append(text)
    return rows


def migration_facts(item: dict[str, Any], spec: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    summary = spec.get("repository_summary") if isinstance(spec.get("repository_summary"), dict) else {}
    title = str(summary.get("display_title") or item.get("title") or artifact_slug(item)).strip()
    purpose = str(
        summary.get("purpose")
        or summary.get("business_question")
        or item.get("content_summary")
        or f"保留并复用正式查询：{title}。"
    ).strip()
    if len(purpose) < 6:
        purpose = f"保留并复用正式查询：{title}。"
    filter_rows: list[str] = []
    for value in summary.get("filters", []) if isinstance(summary.get("filters"), list) else []:
        if isinstance(value, dict):
            text = str(value.get("condition") or value.get("label") or value.get("value") or "").strip()
        else:
            text = str(value or "").strip()
        if text and text not in filter_rows:
            filter_rows.append(text)
    analysis = {
        "business_category": item.get("business_category") or "uncategorized",
        "analysis_type": item.get("analysis_type") or "unspecified",
        "tables": item.get("tables") if isinstance(item.get("tables"), list) else [],
        "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
        "grain": summary.get("grain") or item.get("grain") or "",
        "time_grain": item.get("time_grain") or "",
    }
    facts = {
        "analysis": analysis,
        "business_category": analysis["business_category"],
        "analysis_type": analysis["analysis_type"],
        "tables": analysis["tables"],
        "source_logs": summary_list(summary.get("source_logs", []), "name", "label"),
        "metrics": summary_list(summary.get("metrics", []), "name", "field", "label"),
        "dimensions": summary_list(summary.get("dimensions", []), "name", "field", "label"),
        "filters": filter_rows,
        "params": {},
        "grain": analysis["grain"],
        "time_grain": analysis["time_grain"],
        "tags": analysis["tags"],
    }
    return title, purpose, facts


def load_artifact_documents(root: Path, item: dict[str, Any]) -> tuple[Path, Path, Path, dict[str, Any], dict[str, Any]]:
    sql_path = (root / str(item.get("path") or "")).resolve()
    spec_ref = str(item.get("spec_path") or "")
    spec_path = (root / spec_ref).resolve() if spec_ref else sql_path.with_name(f"{sql_path.stem}.spec.json")
    meta_path = sql_path.with_name(f"{sql_path.stem}.meta.json")
    return sql_path, spec_path, meta_path, read_json(spec_path, {}), read_json(meta_path, {})


def plan_migration(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "manifest.json", {})
    rows: list[dict[str, Any]] = []
    for item in manifest.get("artifacts", []) if isinstance(manifest.get("artifacts"), list) else []:
        if not isinstance(item, dict) or str(item.get("kind") or "").upper() != "QUERY":
            continue
        sql_path, spec_path, meta_path, spec, _ = load_artifact_documents(root, item)
        origin = spec.get("origin_query_workspace") if isinstance(spec.get("origin_query_workspace"), dict) else {}
        rows.append(
            {
                "path": str(item.get("path") or ""),
                "slug": artifact_slug(item),
                "formal_version": version_number(str(item.get("path") or "")),
                "artifact_state": item.get("artifact_state") or "current",
                "sql_exists": sql_path.exists(),
                "spec_exists": spec_path.exists(),
                "meta_exists": meta_path.exists(),
                "action": "reuse_origin" if origin else "backfill_workspace_snapshot",
                "existing_origin": origin,
            }
        )
    rows.sort(key=lambda row: (str(row.get("slug")), int(row.get("formal_version") or 0)))
    blockers = [f"Missing formal SQL: {row['path']}" for row in rows if not row["sql_exists"]]
    return {
        "status": "blocked" if blockers else "ready",
        "root": str(root),
        "artifact_count": len(rows),
        "backfill_count": sum(row["action"] == "backfill_workspace_snapshot" for row in rows),
        "reuse_count": sum(row["action"] == "reuse_origin" for row in rows),
        "rows": rows,
        "blockers": blockers,
    }


def execute_migration(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("status") != "ready":
        return plan
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path, {})
    query_ids: dict[str, str] = {}
    document_updates: dict[Path, str] = {}
    migrated: list[dict[str, Any]] = []
    for item in sorted(
        [row for row in manifest.get("artifacts", []) if isinstance(row, dict) and str(row.get("kind") or "").upper() == "QUERY"],
        key=lambda row: (artifact_slug(row), version_number(str(row.get("path") or ""))),
    ):
        sql_path, spec_path, meta_path, spec, meta = load_artifact_documents(root, item)
        existing_origin = spec.get("origin_query_workspace") if isinstance(spec.get("origin_query_workspace"), dict) else {}
        if existing_origin:
            reference = find_query_reference(root, root / str(existing_origin.get("path") or ""), match_fingerprint=False)
            source_intake = reference.get("source_intake") if isinstance(reference, dict) else {}
            if isinstance(source_intake, dict) and source_intake.get("contract_version") == "historical_formal_query_backfill_v1":
                mark_historical_formal_backfill(root, reference or {}, str(item.get("path") or ""))
                action = "repaired_existing_backfill"
            else:
                action = "reused_existing_origin"
            migrated.append({"path": item.get("path"), "action": action, "origin": existing_origin})
            continue
        sql_text = normalize_sql_text(sql_path.read_text(encoding="utf-8-sig"))
        title, purpose, facts = migration_facts(item, spec)
        slug = artifact_slug(item)
        intake = {
            "contract_version": "historical_formal_query_backfill_v1",
            "source_kind": "historical_formal_artifact",
            "original_file_name": sql_path.name,
            "source_sha256": file_sha256(sql_path),
            "source_sql_fingerprint": sql_fingerprint(sql_text),
            "source_project_path": str(item.get("path") or "").replace("\\", "/"),
            "historical_backfill": True,
            "fresh_generation_gate_not_replayed": True,
            "external_input_immutable": True,
            "absolute_source_path_persisted": False,
        }
        gate = {
            "status": "ok",
            "mode": "historical_formal_migration",
            "checks": {
                "formal_manifest_registration": "ok",
                "fresh_generation_validation": "not_replayed",
            },
            "blockers": [],
            "warnings": ["Historical formal artifact backfill; no fresh generation gate was replayed."],
        }
        existing_family_id = query_ids.get(slug, "")
        saved = save_query(
            root=root,
            source_sql=sql_path,
            title=title,
            purpose=purpose,
            business_question=str((spec.get("repository_summary") or {}).get("business_question") or purpose),
            status="result_confirmed",
            query_id=existing_family_id,
            source_kind="historical_formal_migration",
            tags=facts.get("tags", []),
            revision_note="Backfilled from a historical formal QUERY without changing the formal SQL body.",
            gate=gate,
            rule_context=None,
            gate_mode="historical_formal_migration",
            facts=facts,
            write_seed=False,
            source_intake=intake,
            change_type="replacement" if existing_family_id else "migration",
            coverage_relation="same_contract" if existing_family_id else "unknown",
        )
        reference = find_query_reference(root, sql_path)
        if not reference:
            raise ValueError(f"Could not resolve migrated workspace snapshot for {item.get('path')}")
        query_ids.setdefault(slug, str(reference.get("query_id") or saved.get("query_id") or ""))
        promoted = mark_historical_formal_backfill(root, reference, str(item.get("path") or ""))
        reference = find_query_reference(root, sql_path)
        origin = origin_contract(reference)
        if not origin:
            raise ValueError(f"Could not build origin contract for {item.get('path')}")
        spec["origin_query_workspace"] = origin
        if isinstance(spec.get("formalize_bundle"), dict):
            spec["formalize_bundle"]["origin_query_workspace"] = copy.deepcopy(origin)
        meta["origin_query_workspace"] = copy.deepcopy(origin)
        item["origin_query_workspace"] = copy.deepcopy(origin)
        document_updates[spec_path] = json_text(spec)
        document_updates[meta_path] = json_text(meta)
        migrated.append(
            {
                "path": item.get("path"),
                "action": "backfilled",
                "workspace_path": origin.get("path"),
                "query_id": origin.get("query_id"),
                "workspace_version": origin.get("version"),
                "promotion_status": promoted.get("query_status"),
            }
        )
    manifest["updated_at"] = now_iso()
    document_updates[manifest_path] = json_text(manifest)
    _write_transaction(document_updates)
    return {
        "status": "migrated",
        "root": str(root),
        "artifact_count": len(migrated),
        "backfilled_count": sum(row.get("action") == "backfilled" for row in migrated),
        "reused_count": sum(row.get("action") in {"reused_existing_origin", "repaired_existing_backfill"} for row in migrated),
        "rows": migrated,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--write", action="store_true", help="Apply the migration; default is dry-run")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    add_function_gate_arguments(parser, selection_help="Optional explicit route [PROJECT_ADMIN].")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.write:
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=command_function_ids("migrate_query_workspace.py"),
                purpose="migrate_query_workspace.py --write",
            )
            require_user_request(args.user_request, purpose="migrate_query_workspace.py --write")
        root = Path(args.root).resolve()
        plan = plan_migration(root)
        result = execute_migration(root, plan) if args.write and plan.get("status") == "ready" else plan
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"status: {result.get('status')}")
            print(f"artifact_count: {result.get('artifact_count', 0)}")
            print(f"backfill_count: {result.get('backfill_count', result.get('backfilled_count', 0))}")
            print(f"reuse_count: {result.get('reuse_count', result.get('reused_count', 0))}")
            for blocker in result.get("blockers", []):
                print(f"blocker: {blocker}")
        return 0 if result.get("status") not in {"blocked", "error"} else 1
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "blockers": [str(exc)]}, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
