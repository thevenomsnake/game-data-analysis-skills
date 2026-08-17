#!/usr/bin/env python3
"""Move legacy scratch/work SQL into the indexed query workspace."""

from __future__ import annotations

import argparse
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
    _query_facts,
    file_sha256,
    finalize_legacy_source_intake,
    find_query_reference,
    normalize_sql_text,
    now_iso,
    project_relative,
    record_legacy_source_reference,
    save_query,
    sql_fingerprint,
)


SKIP_COMMENT_PREFIXES = (
    "项目",
    "目标平台",
    "平台",
    "方言",
    "数据源",
    "统计日期",
    "修改日期",
    "参数区",
    "说明",
)


def _comment_lines(sql: str) -> list[str]:
    match = re.match(r"\s*/\*(.*?)\*/", sql, flags=re.S)
    if not match:
        return []
    rows: list[str] = []
    for raw_line in match.group(1).splitlines():
        line = re.sub(r"^\s*[=*#-]+\s*", "", raw_line).strip()
        line = re.sub(r"\s+", " ", line)
        if not line or re.fullmatch(r"[=*_\-\s]+", line):
            continue
        rows.append(line)
    return rows


def _clean_title(value: str) -> str:
    text = re.sub(r"^(?:指标名称|指标|临时排查明细样本|临时排查明细|临时修正版|临时排查)\s*[：:]\s*", "", value).strip()
    text = re.sub(r"\s+", " ", text).strip("。；; ")
    return text[:96]


def _fallback_title(path: Path) -> str:
    stem = re.sub(r"[-_]v\d+$", "", path.stem, flags=re.I)
    stem = re.sub(r"[_-]+", " ", stem).strip()
    return stem[:96] or "历史 SQL"


def _purpose_from_facts(facts: dict[str, Any], title: str) -> str:
    logs = [str(item).split("【", 1)[0] for item in facts.get("source_logs", []) if str(item).strip()]
    metrics = [str(item) for item in facts.get("metrics", []) if str(item).strip()]
    dimensions = [str(item) for item in facts.get("dimensions", []) if str(item).strip()]
    source_text = "、".join(logs[:3]) or "现有日志"
    metric_text = "、".join(metrics[:4]) or "查询结果"
    dimension_text = f"，按{'、'.join(dimensions[:3])}观察" if dimensions else ""
    return f"基于 {source_text} 检查{metric_text}{dimension_text}；原始工作标题为“{title}”。"


def derive_title_and_purpose(path: Path, sql: str, facts: dict[str, Any]) -> tuple[str, str]:
    lines = _comment_lines(sql)
    title = ""
    for line in lines:
        if line.startswith(SKIP_COMMENT_PREFIXES):
            continue
        candidate = _clean_title(line)
        if len(candidate) >= 2:
            title = candidate
            break
    title = title or _fallback_title(path)

    meaningful: list[str] = []
    for line in lines:
        if line.startswith(SKIP_COMMENT_PREFIXES):
            continue
        cleaned = re.sub(r"^(?:目的|目标|口径摘要)\s*[：:]\s*", "", line).strip()
        if cleaned and cleaned not in meaningful:
            meaningful.append(cleaned)
        if len("；".join(meaningful)) >= 48:
            break
    purpose = "；".join(meaningful[:3]).strip("；; ")
    if len(purpose) < 12:
        purpose = _purpose_from_facts(facts, title)
    elif not purpose.endswith(("。", "！", "？", ".", "!", "?")):
        purpose += "。"
    return title, purpose[:360]


def resolve_source_dir(root: Path, value: str) -> Path:
    source_dir = (root / str(value or "_scratch")).resolve()
    source_dir.relative_to(root.resolve())
    workspace = (root / "query_workspace").resolve()
    if source_dir == workspace or workspace in source_dir.parents:
        raise ValueError("Legacy source directory cannot be inside query_workspace.")
    return source_dir


def plan_migration(root: Path, source_dir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    if not source_dir.exists():
        blockers.append(f"Legacy SQL directory does not exist: {project_relative(root, source_dir)}")
    for source_path in sorted(source_dir.rglob("*.sql")) if source_dir.exists() else []:
        try:
            source_rel = project_relative(root, source_path)
            sql = normalize_sql_text(source_path.read_text(encoding="utf-8-sig"))
            if not sql or not re.search(r"\b(?:select|with|show|describe|desc|explain)\b", sql, flags=re.I):
                raise ValueError("file does not contain query SQL")
            fingerprint = sql_fingerprint(sql)
            existing = find_query_reference(root, source_path)
            facts = _query_facts(root, source_path, sql, ["legacy_work", "migrated"])
            title, purpose = derive_title_and_purpose(source_path, sql, facts)
            result_candidates = [
                project_relative(root, candidate)
                for suffix in (".xlsx", ".csv")
                if (candidate := source_path.with_suffix(suffix)).exists()
            ]
            rows.append(
                {
                    "source_path": source_rel,
                    "source_sha256": file_sha256(source_path),
                    "sql_fingerprint": fingerprint,
                    "title": title,
                    "purpose": purpose,
                    "business_category": facts.get("business_category") or "uncategorized",
                    "analysis_type": facts.get("analysis_type") or "unspecified",
                    "source_logs": facts.get("source_logs", []),
                    "metrics": facts.get("metrics", []),
                    "dimensions": facts.get("dimensions", []),
                    "result_candidates": result_candidates,
                    "action": "reuse_indexed_fingerprint" if existing else "archive_in_workspace",
                    "existing_workspace_path": str((existing or {}).get("path") or ""),
                }
            )
        except (OSError, UnicodeError, ValueError) as exc:
            blockers.append(f"{source_path}: {exc}")
    return {
        "status": "blocked" if blockers else "ready",
        "root": str(root),
        "source_dir": project_relative(root, source_dir),
        "sql_file_count": len(rows),
        "archive_count": sum(row["action"] == "archive_in_workspace" for row in rows),
        "reuse_count": sum(row["action"] == "reuse_indexed_fingerprint" for row in rows),
        "rows": rows,
        "blockers": blockers,
    }


def _remove_verified_source(root: Path, source_path: Path, expected_fingerprint: str) -> None:
    source_path = source_path.resolve()
    source_path.relative_to(root.resolve())
    if "query_workspace" in [part.lower() for part in source_path.relative_to(root).parts]:
        raise ValueError(f"Refusing to remove managed workspace SQL: {source_path}")
    actual = sql_fingerprint(source_path.read_text(encoding="utf-8-sig"))
    if actual != expected_fingerprint:
        raise ValueError(f"Legacy source changed before cleanup: {source_path}")
    source_path.unlink()


def execute_migration(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("status") != "ready":
        return plan
    migrated_at = now_iso()
    results: list[dict[str, Any]] = []
    for row in plan.get("rows", []):
        source_path = (root / str(row.get("source_path") or "")).resolve()
        source_path.relative_to(root.resolve())
        source_sql = normalize_sql_text(source_path.read_text(encoding="utf-8-sig"))
        fingerprint = sql_fingerprint(source_sql)
        if fingerprint != row.get("sql_fingerprint"):
            raise ValueError(f"Legacy SQL changed after dry-run planning: {row.get('source_path')}")
        existing = find_query_reference(root, source_path)
        source_ref = {
            "legacy_source_path": row.get("source_path"),
            "original_file_name": source_path.name,
            "source_sha256": row.get("source_sha256"),
            "source_sql_fingerprint": fingerprint,
            "migrated_at": migrated_at,
            "source_removed_after_verified_copy": False,
        }
        if existing:
            record_legacy_source_reference(root, existing, source_ref)
            target_reference = existing
            action = "reused"
        else:
            facts = _query_facts(root, source_path, source_sql, ["legacy_work", "migrated"])
            intake = {
                "contract_version": "legacy_work_import_v1",
                "source_kind": "legacy_work_file",
                "original_file_name": source_path.name,
                "source_sha256": row.get("source_sha256"),
                "source_sql_fingerprint": fingerprint,
                "legacy_source_path": row.get("source_path"),
                "migrated_at": migrated_at,
                "source_removed_after_verified_copy": False,
                "external_input_immutable": True,
                "absolute_source_path_persisted": False,
            }
            saved = save_query(
                root=root,
                source_sql=source_path,
                title=str(row.get("title") or source_path.stem),
                purpose=str(row.get("purpose") or ""),
                business_question=str(row.get("purpose") or ""),
                status="archived",
                source_kind="legacy_work_migration",
                tags=["legacy_work", "migrated"],
                revision_note="Migrated from an unmanaged project work directory; execution status was not inferred.",
                gate={"status": "not_run", "blockers": [], "warnings": ["Historical work migration; generation gate was not replayed."]},
                rule_context=None,
                gate_mode="legacy_work_migration",
                facts=facts,
                write_seed=False,
                source_intake=intake,
            )
            target_reference = find_query_reference(root, root / str(saved.get("path") or ""), match_fingerprint=False)
            if not target_reference:
                raise ValueError(f"Migrated SQL could not be resolved in the workspace: {row.get('source_path')}")
            action = "archived"
        target_path = root / str(target_reference.get("path") or "")
        target_fingerprint = sql_fingerprint(target_path.read_text(encoding="utf-8-sig"))
        if target_fingerprint != fingerprint:
            raise ValueError(f"Indexed SQL fingerprint verification failed: {row.get('source_path')}")
        _remove_verified_source(root, source_path, fingerprint)
        source_ref["source_removed_after_verified_copy"] = True
        if action == "reused":
            record_legacy_source_reference(root, target_reference, source_ref)
        else:
            finalize_legacy_source_intake(root, target_reference)
        results.append(
            {
                "source_path": row.get("source_path"),
                "workspace_path": target_reference.get("path"),
                "query_id": target_reference.get("query_id"),
                "action": action,
                "source_removed": not source_path.exists(),
                "fingerprint_verified": True,
            }
        )
    return {
        "status": "migrated",
        "root": str(root),
        "source_dir": plan.get("source_dir"),
        "migrated_count": len(results),
        "archived_count": sum(row["action"] == "archived" for row in results),
        "reused_count": sum(row["action"] == "reused" for row in results),
        "source_removed_count": sum(bool(row["source_removed"]) for row in results),
        "fingerprint_verified_count": sum(bool(row["fingerprint_verified"]) for row in results),
        "rows": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--source-dir", default="_scratch", help="Project-relative legacy SQL directory")
    parser.add_argument("--write", action="store_true", help="Move verified SQL into query_workspace; default is dry-run")
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
                allowed_ids=command_function_ids("migrate_legacy_sql_work.py"),
                purpose="migrate_legacy_sql_work.py --write",
            )
            require_user_request(args.user_request, purpose="migrate_legacy_sql_work.py --write")
        root = Path(args.root).resolve()
        source_dir = resolve_source_dir(root, args.source_dir)
        plan = plan_migration(root, source_dir)
        result = execute_migration(root, plan) if args.write and plan.get("status") == "ready" else plan
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"status: {result.get('status')}")
            print(f"source_dir: {result.get('source_dir')}")
            print(f"sql_file_count: {result.get('sql_file_count', result.get('migrated_count', 0))}")
            print(f"archive_count: {result.get('archive_count', result.get('archived_count', 0))}")
            print(f"reuse_count: {result.get('reuse_count', result.get('reused_count', 0))}")
            for blocker in result.get("blockers", []):
                print(f"blocker: {blocker}")
        return 0 if result.get("status") not in {"blocked", "error"} else 1
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "blockers": [str(exc)]}, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
