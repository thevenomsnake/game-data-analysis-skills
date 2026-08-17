#!/usr/bin/env python3
"""Backfill generation_provenance for historical formal SQL assets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from asset_provenance import (
    apply_generation_provenance,
    build_generation_provenance,
    merge_generation_provenance,
    now_iso,
    provenance_from_sources,
    skill_metadata,
)
from spec_utils import load_sidecar_spec, spec_path_for_artifact, write_json_object


FORMAL_KINDS = {"QUERY", "VALIDATION", "DASHBOARD"}


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def project_roots(args: argparse.Namespace) -> list[Path]:
    roots: list[Path] = []
    if args.root:
        roots.append(Path(args.root).resolve())
    if args.projects_root:
        base = Path(args.projects_root).resolve()
        for manifest in sorted(base.glob("*/manifest.json")):
            if manifest.parent.name.startswith("_"):
                continue
            roots.append(manifest.parent)
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def historical_provenance(item: dict[str, Any], spec: dict[str, Any], source_label: str) -> dict[str, Any]:
    existing = provenance_from_sources(item, spec)
    if existing and existing.get("source") != "legacy_fallback":
        return existing

    spec_meta = spec.get("spec_meta") if isinstance(spec.get("spec_meta"), dict) else {}
    current_skill = skill_metadata()
    original_skill_version = str(spec_meta.get("skill_version") or "").strip()
    original_spec_version = str(spec_meta.get("spec_version") or current_skill["sql_spec_version"]).strip()
    generator = str(spec_meta.get("generated_by") or spec_meta.get("generator_script") or "unknown_historical")
    workflow = str(spec_meta.get("generation_workflow") or spec_meta.get("workflow") or "unknown_historical")
    generated_at = str(spec_meta.get("generated_at") or item.get("created_at") or "") or None

    provenance = build_generation_provenance(
        generator_script=generator,
        workflow=workflow,
        artifact_kind=str(item.get("kind") or ""),
        generated_at=generated_at,
        source=source_label,
        extra={
            "backfilled_by_script": "migrate_asset_provenance.py",
            "backfilled_by_skill_version": current_skill["skill_version"],
            "backfilled_at": now_iso(),
            "original_generated_by": spec_meta.get("generated_by") or spec_meta.get("generator_script") or "",
            "original_skill_version": original_skill_version or "unknown_historical",
        },
    )
    provenance["skill_version"] = original_skill_version or "unknown_historical"
    provenance["sql_spec_version"] = original_spec_version or current_skill["sql_spec_version"]
    return provenance

def migrate_project(root: Path, *, write: bool) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path, {})
    changed = 0
    checked = 0
    errors: list[str] = []
    planned: list[dict[str, Any]] = []
    artifacts = manifest.get("artifacts", []) if isinstance(manifest.get("artifacts"), list) else []
    for item in artifacts:
        if item.get("kind") not in FORMAL_KINDS:
            continue
        checked += 1
        sql_path = root / str(item.get("path") or "")
        if not sql_path.exists():
            errors.append(f"{item.get('path')}: SQL file missing")
            continue
        meta_path = sql_path.with_name(sql_path.stem + ".meta.json")
        meta = read_json(meta_path, {})
        spec, spec_errors = load_sidecar_spec(root, item, sql_path)
        if spec_errors or not spec:
            errors.append(f"{item.get('path')}: {'; '.join(spec_errors)}")
            continue
        existing = spec.get("generation_provenance") if isinstance(spec.get("generation_provenance"), dict) else None
        if existing and isinstance(item.get("generation_provenance"), dict) and (not meta or isinstance(meta.get("generation_provenance"), dict)):
            continue
        provenance = historical_provenance(item, spec, "historical_backfill")
        provenance = merge_generation_provenance(
            provenance,
            fallback_generator_script="migrate_asset_provenance.py",
            fallback_workflow="historical_asset_backfill",
            artifact_kind=str(item.get("kind") or ""),
            saved_at=str(item.get("created_at") or "") or None,
            saved_by_script=str(provenance.get("saved_by_script") or "unknown_historical"),
        )
        planned.append(
            {
                "path": item.get("path"),
                "kind": item.get("kind"),
                "slug": item.get("slug"),
                "version": item.get("version"),
                "skill_version": provenance.get("skill_version"),
                "generator_script": provenance.get("generated_by_script"),
                "workflow": provenance.get("workflow"),
            }
        )
        if write:
            item["generation_provenance"] = provenance
            if isinstance(meta, dict) and meta_path.exists():
                meta["generation_provenance"] = provenance
                write_json(meta_path, meta)
            apply_generation_provenance(spec, provenance)
            write_json_object(spec_path_for_artifact(root, item, sql_path), spec)
            changed += 1
    if write and changed:
        write_json(manifest_path, manifest)
    return {
        "project": root.name,
        "root": str(root),
        "checked": checked,
        "planned_updates": len(planned),
        "changed": changed,
        "errors": errors,
        "updates": planned,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root", help="Single project root")
    group.add_argument("--projects-root", help="sql-projects root containing multiple projects")
    parser.add_argument("--write", action="store_true", help="Apply updates. Default is dry-run only.")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    roots = project_roots(args)
    results = [migrate_project(root, write=args.write) for root in roots]
    payload = {
        "status": "error" if any(result["errors"] for result in results) else "updated" if args.write else "dry_run",
        "write": bool(args.write),
        "skill": skill_metadata(),
        "projects": results,
        "summary": {
            "projects": len(results),
            "checked": sum(result["checked"] for result in results),
            "planned_updates": sum(result["planned_updates"] for result in results),
            "changed": sum(result["changed"] for result in results),
            "errors": sum(len(result["errors"]) for result in results),
        },
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"status: {payload['status']}")
        print(f"projects: {payload['summary']['projects']}")
        print(f"checked: {payload['summary']['checked']}")
        print(f"planned_updates: {payload['summary']['planned_updates']}")
        print(f"changed: {payload['summary']['changed']}")
        if payload["summary"]["errors"]:
            print(f"errors: {payload['summary']['errors']}")
    return 1 if payload["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())