#!/usr/bin/env python3
"""Migrate formal SQL artifacts from inline full specs to sidecar specs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from asset_provenance import stamp_sql_generation
from spec_utils import (
    HEADER_MARKERS,
    SPEC_STORAGE,
    build_short_header,
    expected_spec_path,
    extract_legacy_yaml_spec,
    has_full_spec_block,
    normalize_rel,
    set_spec_version,
    sidecar_rel_path,
    strip_legacy_top_spec,
    write_json_object,
)


FORMAL_DIRS = {"query_sql", "dashboard_sql", "validations"}
RATIO_FIELD_RE = re.compile(r"(占比|比例|比率|转化率|留存率|率|percent|percentage|ratio|rate)", flags=re.I)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_formal_artifact_path(path_value: str) -> bool:
    parts = Path(path_value.replace("\\", "/")).parts
    return bool(parts) and parts[0] in FORMAL_DIRS


def manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def meta_path_for_sql(sql_path: Path) -> Path:
    return sql_path.with_name(sql_path.stem + ".meta.json")


def labels_from_spec_items(items: Any) -> list[str]:
    labels: list[str] = []
    if not isinstance(items, list):
        return labels
    for item in items:
        label = ""
        if isinstance(item, dict):
            label = str(item.get("field") or item.get("label") or item.get("output_field") or "").strip()
        elif isinstance(item, str):
            label = item.strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def infer_ratio_scale(sql_text: str, output_field: str) -> tuple[str, Any, str]:
    alias_pattern = re.compile(
        rf"(?P<expr>.{{0,600}}?)\bAS\s+[`\"']?{re.escape(output_field)}[`\"']?",
        flags=re.I | re.S,
    )
    matches = list(alias_pattern.finditer(sql_text))
    expr = matches[-1].group("expr") if matches else ""
    normalized = re.sub(r"\s+", "", expr).lower()
    if "*100" in normalized or "100*" in normalized:
        return "percent_0_to_100", 23.07, "23.07%"
    return "ratio_0_to_1", 0.2307, "23.07%"


def ensure_visual_review_contract(spec: dict[str, Any], sql_text: str) -> bool:
    fields = labels_from_spec_items((spec.get("da_output_contract") or {}).get("table_fields") or [])
    fields.extend(labels_from_spec_items(spec.get("metrics") or []))
    needed = []
    for field in fields:
        if field and RATIO_FIELD_RE.search(field) and field not in needed:
            needed.append(field)
    if not needed:
        return False

    visual = spec.setdefault(
        "visual_review_contract",
        {
            "contract_version": "dashboard_visual_review_v1",
            "scope": "display_format_only",
            "visualization_owner": "DA",
            "field_display_rules": [],
            "review_checks": [],
        },
    )
    if not isinstance(visual, dict):
        return False
    rules = visual.setdefault("field_display_rules", [])
    if not isinstance(rules, list):
        visual["field_display_rules"] = rules = []
    existing = {str(rule.get("output_field") or "") for rule in rules if isinstance(rule, dict)}
    changed = False
    for field in needed:
        if field in existing:
            continue
        scale, raw_value, display_value = infer_ratio_scale(sql_text, field)
        rules.append(
            {
                "output_field": field,
                "semantic_type": "ratio",
                "source_value_scale": scale,
                "display_format": "percent",
                "decimal_places": 2,
                "display_suffix": "%",
                "display_formula": "raw_value" if scale == "percent_0_to_100" else "raw_value * 100",
                "preserve_raw_value": True,
                "sample_check": {"raw_value": raw_value, "display_value": display_value},
                "review_note": "迁移补齐：占比/比例类字段必须声明 DA 展示格式。",
            }
        )
        changed = True
    checks = visual.setdefault("review_checks", [])
    if isinstance(checks, list) and changed:
        note = "确认占比/比例字段在 DA 中按百分比展示并保留 SQL 原始数值。"
        if note not in checks:
            checks.append(note)
    return changed


def migrate_artifact(root: Path, artifact: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    kind = str(artifact.get("kind") or "")
    rel_sql = str(artifact.get("path") or "")
    result = {
        "kind": kind,
        "path": rel_sql,
        "status": "skipped",
        "messages": [],
        "spec_path": "",
    }
    if kind not in HEADER_MARKERS:
        result["messages"].append(f"unsupported kind: {kind}")
        return result
    if not rel_sql or not is_formal_artifact_path(rel_sql):
        result["messages"].append("not a formal artifact path")
        return result

    sql_path = root / rel_sql
    if not sql_path.exists():
        result["status"] = "error"
        result["messages"].append("sql file missing")
        return result
    sql_text = sql_path.read_text(encoding="utf-8")
    spec_path = expected_spec_path(sql_path)
    spec_rel = sidecar_rel_path(root, sql_path)
    result["spec_path"] = spec_rel

    if has_full_spec_block(sql_text):
        spec, errors = extract_legacy_yaml_spec(kind, sql_text)
        if errors or spec is None:
            result["status"] = "error"
            result["messages"].extend(errors)
            return result
        set_spec_version(spec)
        body, strip_errors = strip_legacy_top_spec(kind, sql_text)
        if strip_errors:
            result["status"] = "error"
            result["messages"].extend(strip_errors)
            return result
        header = build_short_header(root, artifact, spec, spec_rel)
        new_sql = header + body
        result["status"] = "migrated"
    elif spec_path.exists():
        spec = read_json(spec_path, {})
        if not isinstance(spec, dict):
            result["status"] = "error"
            result["messages"].append("existing spec sidecar is not an object")
            return result
        set_spec_version(spec)
        header = build_short_header(root, artifact, spec, spec_rel)
        _old_header, header_errors = extract_existing_short_header(kind, sql_text)
        if header_errors:
            new_sql = header + sql_text.lstrip("\r\n")
        else:
            new_sql = replace_existing_short_header(kind, sql_text, header)
        result["status"] = "refreshed"
    else:
        result["status"] = "error"
        result["messages"].append("no legacy inline spec and no sidecar spec")
        return result

    if kind == "DASHBOARD" and ensure_visual_review_contract(spec, sql_text):
        header = build_short_header(root, artifact, spec, spec_rel)
        new_sql = replace_existing_short_header(kind, new_sql, header)
        result["messages"].append("visual_review_contract completed for ratio/rate fields")

    if dry_run:
        return result

    write_json_object(spec_path, spec)
    sql_path.write_text(stamp_sql_generation(root, new_sql), encoding="utf-8")

    artifact["spec_path"] = spec_rel
    artifact["spec_storage"] = SPEC_STORAGE
    artifact["header_contract_version"] = "1"

    meta_path = meta_path_for_sql(sql_path)
    meta = read_json(meta_path, {})
    if isinstance(meta, dict):
        meta["spec_path"] = spec_rel
        meta["spec_storage"] = SPEC_STORAGE
        meta["header_contract_version"] = "1"
        write_json(meta_path, meta)

    return result


def extract_existing_short_header(kind: str, sql_text: str) -> tuple[str | None, list[str]]:
    start, end = HEADER_MARKERS[kind]
    import re

    pattern = re.compile(
        rf"^\s*/\*\s*{re.escape(start)}\b.*?{re.escape(end)}\s*\*/\s*",
        flags=re.M | re.S,
    )
    match = pattern.search(sql_text)
    if not match:
        return None, [f"missing {start}"]
    return match.group(0), []


def replace_existing_short_header(kind: str, sql_text: str, header: str) -> str:
    start, end = HEADER_MARKERS[kind]
    import re

    pattern = re.compile(
        rf"^\s*/\*\s*{re.escape(start)}\b.*?{re.escape(end)}\s*\*/\s*",
        flags=re.M | re.S,
    )
    return pattern.sub(header, sql_text, count=1)


def project_roots(projects_root: Path) -> list[Path]:
    roots = []
    for path in sorted(projects_root.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        if (path / "manifest.json").exists():
            roots.append(path)
    return roots


def migrate_project(root: Path, dry_run: bool) -> dict[str, Any]:
    manifest_file = manifest_path(root)
    manifest = read_json(manifest_file, {})
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        return {"project": root.name, "status": "error", "items": [], "message": "manifest.artifacts must be an array"}

    items = [migrate_artifact(root, item, dry_run) for item in artifacts if isinstance(item, dict)]
    if not dry_run:
        manifest["updated_at"] = manifest.get("updated_at") or ""
        write_json(manifest_file, manifest)
    status = "pass"
    if any(item["status"] == "error" for item in items):
        status = "error"
    elif any(item["status"] in {"migrated", "refreshed"} for item in items):
        status = "changed" if not dry_run else "would_change"
    return {"project": root.name, "status": status, "items": items}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-root", default="sql-projects", help="Root containing SQL projects")
    parser.add_argument("--root", help="Single project root to migrate")
    parser.add_argument("--dry-run", action="store_true", help="Only report planned migration")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    roots = [Path(args.root).resolve()] if args.root else project_roots(Path(args.projects_root).resolve())
    results = [migrate_project(root, args.dry_run) for root in roots]
    payload = {
        "dry_run": args.dry_run,
        "projects": results,
        "summary": {
            "projects": len(results),
            "artifacts": sum(len(project.get("items", [])) for project in results),
            "errors": sum(1 for project in results for item in project.get("items", []) if item.get("status") == "error"),
            "changed": sum(1 for project in results for item in project.get("items", []) if item.get("status") in {"migrated", "refreshed"}),
        },
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"projects={payload['summary']['projects']} artifacts={payload['summary']['artifacts']} changed={payload['summary']['changed']} errors={payload['summary']['errors']}")
        for project in results:
            for item in project.get("items", []):
                if item["status"] in {"migrated", "refreshed", "error"}:
                    print(f"{project['project']} {item['status']} {item['path']} -> {item.get('spec_path', '')}")
                    for message in item.get("messages", []):
                        print(f"  - {message}")
    sys.exit(1 if payload["summary"]["errors"] else 0)


if __name__ == "__main__":
    main()
