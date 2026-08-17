#!/usr/bin/env python3
"""Audit, compact, or confirm revisions to managed SQL result assets."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from capability_registry import command_function_ids  # noqa: E402
from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from result_evidence_retention import (  # noqa: E402
    RESULT_EVIDENCE_MAX_BYTES,
    REUSABLE_OUTPUT_KINDS,
    file_sha256,
    full_reusable_output_retention,
    prepare_result_evidence,
)
from sql_project import rebuild_index  # noqa: E402
from sql_query_workspace import (  # noqa: E402
    _index_files,
    _write_transaction,
    json_text,
    load_index,
    now_iso,
    resolve_project_path,
)
from workbook_manifest import build_workbook_manifest, is_reusable_workbook  # noqa: E402


SCAN_VERSION = "result_evidence_maintenance_scan_v2"
RESULT_VERSION = "result_evidence_maintenance_result_v2"
CONTENT_REVISION_VERSION = "derived_output_content_revision_v1"


def _manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest.json"
    if not path.exists():
        return {"run_evidence": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _output_rows(index: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        for version in entry.get("versions", []):
            if not isinstance(version, dict):
                continue
            for output in version.get("derived_outputs", []):
                if isinstance(output, dict):
                    rows.append((entry, version, output))
    return rows


def _actual_fingerprint(path: Path) -> tuple[int, str]:
    if not path.is_file():
        return 0, ""
    return path.stat().st_size, file_sha256(path)


def _recorded_fingerprint(row: dict[str, Any], retention_key: str = "retention") -> tuple[int | None, str]:
    retention = row.get(retention_key)
    size = None
    digest = ""
    if isinstance(retention, dict):
        raw_size = retention.get("stored_size_bytes")
        if isinstance(raw_size, int):
            size = raw_size
        digest = str(retention.get("stored_sha256") or "")
    return size, digest or str(row.get("sha256") or "")


def _fingerprint_drift(
    *,
    actual_size: int,
    actual_sha: str,
    recorded_size: int | None,
    recorded_sha: str,
) -> bool:
    if not actual_sha:
        return False
    return bool(
        (recorded_sha and recorded_sha != actual_sha)
        or (recorded_size is not None and recorded_size != actual_size)
    )


def scan(root: Path) -> dict[str, Any]:
    """Report missing retention metadata and content drift without changing assets."""

    root = root.resolve()
    index = load_index(root)
    manifest = _manifest(root)
    candidates: list[dict[str, Any]] = []
    missing_retention: list[dict[str, Any]] = []
    fingerprint_drift: list[dict[str, Any]] = []
    reusable_outputs: list[dict[str, Any]] = []

    for entry, version, output in _output_rows(index):
        path_ref = str(output.get("path") or "")
        path = resolve_project_path(root, path_ref)
        size, actual_sha = _actual_fingerprint(path)
        retention = output.get("retention")
        recorded_size, recorded_sha = _recorded_fingerprint(output)
        output_sha = str(output.get("sha256") or "")
        kind = str(output.get("kind") or "")
        base = {
            "scope": "query_workspace",
            "query_id": entry.get("query_id") or "",
            "version": version.get("version"),
            "attachment_id": output.get("attachment_id") or "",
            "kind": kind,
            "path": path_ref,
            "size_bytes": size,
            "size_mb": round(size / 1024 / 1024, 2),
            "actual_sha256": actual_sha,
            "recorded_size_bytes": recorded_size,
            "recorded_sha256": recorded_sha,
            "output_sha256": output_sha,
            "retention_policy": retention.get("policy") if isinstance(retention, dict) else "",
        }
        if not path.is_file():
            finding = {**base, "issue_type": "missing_file", "action": "restore_or_remove_metadata"}
            candidates.append(finding)
            continue

        drifted = _fingerprint_drift(
            actual_size=size,
            actual_sha=actual_sha,
            recorded_size=recorded_size,
            recorded_sha=recorded_sha,
        ) or bool(output_sha and output_sha != actual_sha)
        if drifted:
            finding = {
                **base,
                "issue_type": "fingerprint_drift",
                "action": "confirm_refresh" if kind in REUSABLE_OUTPUT_KINDS else "restore_immutable_evidence",
                "user_confirmation_required": True,
            }
            candidates.append(finding)
            fingerprint_drift.append(finding)

        if not isinstance(retention, dict):
            finding = {
                **base,
                "issue_type": "missing_retention",
                "action": "slice" if kind == "result_evidence" and size > RESULT_EVIDENCE_MAX_BYTES else "backfill_retention",
            }
            candidates.append(finding)
            missing_retention.append(finding)

        if kind != "result_evidence":
            reusable_outputs.append(
                {
                    **base,
                    "action": "confirm_refresh" if drifted else "preserve_full",
                    "fingerprint_status": "drift" if drifted else "match",
                }
            )

    for run in manifest.get("run_evidence", []):
        if not isinstance(run, dict) or not run.get("evidence_file"):
            continue
        path_ref = str(run.get("evidence_file") or "")
        path = resolve_project_path(root, path_ref)
        size, actual_sha = _actual_fingerprint(path)
        retention = run.get("result_evidence_retention")
        recorded_size, recorded_sha = _recorded_fingerprint(run, "result_evidence_retention")
        base = {
            "scope": "formal_run_evidence",
            "run_id": run.get("run_id") or "",
            "kind": "result_evidence",
            "path": path_ref,
            "size_bytes": size,
            "size_mb": round(size / 1024 / 1024, 2),
            "actual_sha256": actual_sha,
            "recorded_size_bytes": recorded_size,
            "recorded_sha256": recorded_sha,
            "retention_policy": retention.get("policy") if isinstance(retention, dict) else "",
        }
        if not path.is_file():
            candidates.append({**base, "issue_type": "missing_file", "action": "restore_or_remove_metadata"})
            continue
        drifted = _fingerprint_drift(
            actual_size=size,
            actual_sha=actual_sha,
            recorded_size=recorded_size,
            recorded_sha=recorded_sha,
        )
        if drifted:
            finding = {
                **base,
                "issue_type": "fingerprint_drift",
                "action": "restore_immutable_evidence",
                "user_confirmation_required": True,
            }
            candidates.append(finding)
            fingerprint_drift.append(finding)
        if not isinstance(retention, dict):
            finding = {
                **base,
                "issue_type": "missing_retention",
                "action": "slice" if size > RESULT_EVIDENCE_MAX_BYTES else "backfill_retention",
            }
            candidates.append(finding)
            missing_retention.append(finding)

    return {
        "schema_version": SCAN_VERSION,
        "status": "action_required" if candidates else "pass",
        "project_id": index.get("project_id") or root.name,
        "threshold_bytes": RESULT_EVIDENCE_MAX_BYTES,
        "candidate_count": len(candidates),
        "missing_retention_count": len(missing_retention),
        "fingerprint_drift_count": len(fingerprint_drift),
        "slice_candidate_count": sum(1 for item in missing_retention if item["action"] == "slice"),
        "reusable_output_count": len(reusable_outputs),
        "candidates": candidates,
        "missing_retention": missing_retention,
        "fingerprint_drift": fingerprint_drift,
        "reusable_outputs": reusable_outputs,
        "policy": {
            "large_result_evidence_is_sliced": True,
            "result_evidence_is_immutable": True,
            "reusable_output_edits_require_confirmed_refresh": True,
            "visualizations_and_analysis_outputs_are_preserved_full": True,
            "sql_mutated": False,
            "lifecycle_mutated": False,
        },
    }


def _safe_old_payload(root: Path, path: Path, scope: str) -> bool:
    allowed_root = root / ("query_workspace" if scope == "query_workspace" else "runs")
    try:
        path.resolve().relative_to(allowed_root.resolve())
        return True
    except ValueError:
        return False


def _has_drift(scan_result: dict[str, Any], *, scope: str, path_ref: str) -> bool:
    for finding in scan_result.get("fingerprint_drift", []):
        if finding.get("scope") != scope:
            continue
        if finding.get("path") == path_ref:
            return True
    return False


def _write_touched_versions(
    root: Path,
    index: dict[str, Any],
    touched: dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]],
    writes: dict[Path, str | bytes],
) -> None:
    for entry, version in touched.values():
        changed_at = now_iso()
        version["updated_at"] = changed_at
        entry["updated_at"] = changed_at
        meta_path = resolve_project_path(root, str(version.get("meta_path") or ""))
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["derived_outputs"] = copy.deepcopy(version.get("derived_outputs") or [])
        meta["updated_at"] = changed_at
        writes[meta_path] = json_text(meta)
    if touched:
        writes.update(_index_files(root, index))


def compact(root: Path) -> dict[str, Any]:
    """Backfill retention or slice large immutable results without touching clean versions."""

    root = root.resolve()
    before = scan(root)
    index = load_index(root)
    manifest = _manifest(root)
    writes: dict[Path, str | bytes] = {}
    old_payloads: list[tuple[Path, str]] = []
    compacted: list[dict[str, Any]] = []
    touched: dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]] = {}

    for entry, version, output in _output_rows(index):
        path_ref = str(output.get("path") or "")
        path = resolve_project_path(root, path_ref)
        if not path.exists():
            continue
        attachment_id = str(output.get("attachment_id") or "")
        if _has_drift(before, scope="query_workspace", path_ref=path_ref):
            continue
        kind = str(output.get("kind") or "")
        changed = False
        if kind == "result_evidence":
            if path.stat().st_size <= RESULT_EVIDENCE_MAX_BYTES and isinstance(output.get("retention"), dict):
                continue
            retained = prepare_result_evidence(path)
            original_name = str(output.get("original_file_name") or path.name)
            retained.retention["source_file_name"] = original_name
            if retained.retention["is_sliced"]:
                new_path = path.with_name(f"{path.stem}-slice{retained.suffix}")
                writes[new_path] = retained.payload
                old_payloads.append((path, "query_workspace"))
                output["path"] = new_path.relative_to(root).as_posix()
            output["media_type"] = retained.media_type
            output["sha256"] = retained.stored_sha256
            output["source_sha256"] = retained.retention["source_sha256"]
            output["retention"] = retained.retention
            changed = True
            compacted.append(
                {
                    "scope": "query_workspace",
                    "attachment_id": attachment_id,
                    "old_path": path_ref,
                    "new_path": output.get("path") or path_ref,
                    "policy": retained.retention["policy"],
                    "source_size_bytes": retained.retention["source_size_bytes"],
                    "stored_size_bytes": retained.retention["stored_size_bytes"],
                }
            )
        elif kind in REUSABLE_OUTPUT_KINDS and not isinstance(output.get("retention"), dict):
            retention = full_reusable_output_retention(path, kind)
            output["source_sha256"] = retention["source_sha256"]
            output["sha256"] = retention["stored_sha256"]
            output["retention"] = retention
            changed = True
            compacted.append(
                {
                    "scope": "query_workspace",
                    "attachment_id": attachment_id,
                    "old_path": path_ref,
                    "new_path": path_ref,
                    "policy": "full_reusable_output",
                    "source_size_bytes": retention["source_size_bytes"],
                    "stored_size_bytes": retention["stored_size_bytes"],
                }
            )
        if changed:
            key = (str(entry.get("query_id") or ""), int(version.get("version") or 0))
            touched[key] = (entry, version)

    manifest_changed = False
    for run in manifest.get("run_evidence", []):
        if not isinstance(run, dict) or not run.get("evidence_file"):
            continue
        path_ref = str(run.get("evidence_file") or "")
        path = resolve_project_path(root, path_ref)
        if not path.exists():
            continue
        run_id = str(run.get("run_id") or "")
        if _has_drift(before, scope="formal_run_evidence", path_ref=path_ref):
            continue
        if path.stat().st_size <= RESULT_EVIDENCE_MAX_BYTES and isinstance(run.get("result_evidence_retention"), dict):
            continue
        retained = prepare_result_evidence(path)
        retained.retention["source_file_name"] = path.name
        if retained.retention["is_sliced"]:
            new_path = path.with_name(f"{path.stem}-slice{retained.suffix}")
            writes[new_path] = retained.payload
            old_payloads.append((path, "formal_run_evidence"))
            run["evidence_file"] = new_path.relative_to(root).as_posix()
        run["result_file_type"] = retained.suffix
        run["result_evidence_retention"] = retained.retention
        manifest_changed = True
        compacted.append(
            {
                "scope": "formal_run_evidence",
                "run_id": run_id,
                "old_path": path_ref,
                "new_path": run.get("evidence_file") or path_ref,
                "policy": retained.retention["policy"],
                "source_size_bytes": retained.retention["source_size_bytes"],
                "stored_size_bytes": retained.retention["stored_size_bytes"],
            }
        )

    _write_touched_versions(root, index, touched, writes)
    if manifest_changed:
        manifest["updated_at"] = now_iso()
        writes[root / "manifest.json"] = json_text(manifest)
    if writes:
        _write_transaction(writes)
        for old_path, scope in old_payloads:
            if not _safe_old_payload(root, old_path, scope):
                raise ValueError(f"Refusing to remove payload outside managed roots: {old_path}")
            old_path.unlink(missing_ok=True)
        if manifest_changed:
            rebuild_index(root)

    after = scan(root)
    return {
        "schema_version": RESULT_VERSION,
        "status": "compacted" if compacted else ("action_required" if after["candidate_count"] else "pass"),
        "project_id": before["project_id"],
        "threshold_bytes": RESULT_EVIDENCE_MAX_BYTES,
        "changed_count": len(compacted),
        "changed_version_count": len(touched),
        "removed_full_result_count": len(old_payloads),
        "remaining_candidate_count": after["candidate_count"],
        "remaining_fingerprint_drift_count": after["fingerprint_drift_count"],
        "changes": compacted,
        "policy": after["policy"],
    }


def _replace_output_references(outputs: list[Any], old_id: str, new_id: str) -> bool:
    changed = False
    for output in outputs:
        if not isinstance(output, dict):
            continue
        for ref in output.get("superseded_by", []) if isinstance(output.get("superseded_by"), list) else []:
            if isinstance(ref, dict) and ref.get("attachment_id") == old_id:
                ref["attachment_id"] = new_id
                changed = True
    return changed


def refresh(
    root: Path,
    *,
    attachment_id: str,
    reason: str,
    user_request: str,
    query_id: str = "",
    version_number: int = 0,
) -> dict[str, Any]:
    """Accept a user-confirmed in-place revision of one reusable output."""

    root = root.resolve()
    attachment_id = str(attachment_id or "").strip()
    reason = str(reason or "").strip()
    user_request = str(user_request or "").strip()
    if not attachment_id:
        raise ValueError("refresh requires an attachment_id")
    if len(reason) < 4:
        raise ValueError("refresh requires a concrete reason of at least 4 characters")
    if not user_request:
        raise ValueError("refresh requires the verbatim user request")

    index = load_index(root)
    matches: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for entry, version, output in _output_rows(index):
        if output.get("attachment_id") != attachment_id:
            continue
        if query_id and entry.get("query_id") != query_id:
            continue
        if version_number and int(version.get("version") or 0) != version_number:
            continue
        matches.append((entry, version, output))
    if not matches:
        raise ValueError(f"Reusable output attachment not found: {attachment_id}")
    if len(matches) > 1:
        raise ValueError("attachment_id is ambiguous; provide --query-id and --version")

    entry, version, output = matches[0]
    kind = str(output.get("kind") or "")
    if kind == "result_evidence":
        raise ValueError("result_evidence is immutable and cannot be refreshed in place")
    if kind not in REUSABLE_OUTPUT_KINDS:
        raise ValueError(f"Unsupported reusable output kind: {kind}")
    path_ref = str(output.get("path") or "")
    path = resolve_project_path(root, path_ref)
    if not path.is_file():
        raise ValueError(f"Reusable output file not found: {path_ref}")

    actual_size, actual_sha = _actual_fingerprint(path)
    previous_sha = str(output.get("sha256") or "")
    previous_size, _ = _recorded_fingerprint(output)
    if actual_sha == previous_sha and (previous_size is None or previous_size == actual_size):
        return {
            "schema_version": RESULT_VERSION,
            "status": "pass",
            "message": "Reusable output fingerprint already matches metadata.",
            "query_id": entry.get("query_id") or "",
            "version": version.get("version"),
            "attachment_id": attachment_id,
            "path": path_ref,
        }

    new_attachment_id = f"qwo-{actual_sha[:12]}"
    outputs = version.get("derived_outputs") if isinstance(version.get("derived_outputs"), list) else []
    if any(
        isinstance(item, dict)
        and item is not output
        and (item.get("attachment_id") == new_attachment_id or item.get("sha256") == actual_sha)
        for item in outputs
    ):
        raise ValueError("The revised payload duplicates another output on this query version.")

    refreshed_at = now_iso()
    retention = full_reusable_output_retention(path, kind)
    revisions = output.setdefault("content_revisions", [])
    if not isinstance(revisions, list):
        raise ValueError("content_revisions must be an array")
    revisions.append(
        {
            "contract_version": CONTENT_REVISION_VERSION,
            "revision": len(revisions) + 1,
            "reason": reason,
            "user_confirmation": user_request,
            "confirmed_at": refreshed_at,
            "previous_attachment_id": attachment_id,
            "previous_sha256": previous_sha,
            "previous_size_bytes": previous_size,
            "attachment_id": new_attachment_id,
            "sha256": actual_sha,
            "size_bytes": actual_size,
        }
    )
    output["attachment_id"] = new_attachment_id
    output["sha256"] = actual_sha
    output["source_sha256"] = actual_sha
    output["retention"] = retention
    if is_reusable_workbook(kind, output.get("media_type"), path_ref):
        output["workbook_manifest"] = build_workbook_manifest(path)
        output["preview_status"] = "not_available"
    provenance = output.get("generation_provenance")
    if isinstance(provenance, dict):
        if provenance.get("attachment_id") == attachment_id:
            provenance["attachment_id"] = new_attachment_id
        provenance["content_refreshed_at"] = refreshed_at

    touched: dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]] = {}
    target_key = (str(entry.get("query_id") or ""), int(version.get("version") or 0))
    touched[target_key] = (entry, version)
    for other_entry in index.get("entries", []):
        if not isinstance(other_entry, dict):
            continue
        for other_version in other_entry.get("versions", []):
            if not isinstance(other_version, dict):
                continue
            other_outputs = other_version.get("derived_outputs")
            if not isinstance(other_outputs, list):
                continue
            if _replace_output_references(other_outputs, attachment_id, new_attachment_id):
                key = (str(other_entry.get("query_id") or ""), int(other_version.get("version") or 0))
                touched[key] = (other_entry, other_version)

    writes: dict[Path, str | bytes] = {}
    _write_touched_versions(root, index, touched, writes)
    _write_transaction(writes)
    return {
        "schema_version": RESULT_VERSION,
        "status": "refreshed",
        "query_id": entry.get("query_id") or "",
        "version": version.get("version"),
        "previous_attachment_id": attachment_id,
        "attachment_id": new_attachment_id,
        "previous_sha256": previous_sha,
        "sha256": actual_sha,
        "previous_size_bytes": previous_size,
        "size_bytes": actual_size,
        "path": path_ref,
        "reason": reason,
        "updated_version_count": len(touched),
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [f"status={payload.get('status', 'unknown')}"]
    for key in (
        "project_id",
        "candidate_count",
        "missing_retention_count",
        "fingerprint_drift_count",
        "slice_candidate_count",
        "changed_count",
        "changed_version_count",
        "removed_full_result_count",
        "remaining_candidate_count",
        "remaining_fingerprint_drift_count",
        "attachment_id",
    ):
        if key in payload:
            lines.append(f"{key}={payload[key]}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="Audit retention metadata and stored-file fingerprints")
    scan_parser.add_argument("--root", required=True)
    scan_parser.add_argument("--format", choices=["json", "text"], default="json")
    compact_parser = sub.add_parser("compact", help="Slice large result evidence and backfill missing retention metadata")
    compact_parser.add_argument("--root", required=True)
    compact_parser.add_argument("--dry-run", action="store_true")
    compact_parser.add_argument("--format", choices=["json", "text"], default="json")
    add_function_gate_arguments(compact_parser, selection_help="Use [RESULT_EVIDENCE_MAINTENANCE].")
    refresh_parser = sub.add_parser("refresh", help="Accept one user-confirmed revision of a reusable output")
    refresh_parser.add_argument("--root", required=True)
    refresh_parser.add_argument("--attachment-id", required=True)
    refresh_parser.add_argument("--query-id", default="")
    refresh_parser.add_argument("--version", type=int, default=0)
    refresh_parser.add_argument("--reason", required=True)
    refresh_parser.add_argument("--format", choices=["json", "text"], default="json")
    add_function_gate_arguments(refresh_parser, selection_help="Use [RESULT_EVIDENCE_MAINTENANCE].")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.command == "scan" or (args.command == "compact" and args.dry_run):
            result = scan(root)
            if args.command == "compact":
                result["dry_run"] = True
        elif args.command == "compact":
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=command_function_ids("result_evidence_maintenance.py", "compact"),
                purpose="compact managed result evidence",
            )
            require_user_request(args.user_request, purpose="compact managed result evidence")
            result = compact(root)
        else:
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=command_function_ids("result_evidence_maintenance.py", "refresh"),
                purpose="refresh a user-confirmed reusable output",
            )
            require_user_request(args.user_request, purpose="refresh a user-confirmed reusable output")
            result = refresh(
                root,
                attachment_id=args.attachment_id,
                reason=args.reason,
                user_request=args.user_request,
                query_id=args.query_id,
                version_number=args.version,
            )
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "error", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else render_text(result), end="")
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
