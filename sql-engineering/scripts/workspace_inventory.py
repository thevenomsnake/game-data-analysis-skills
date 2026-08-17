#!/usr/bin/env python3
"""Inventory files under Query Workspace that are not registered members."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "workspace_unregistered_inventory_v1"
INVENTORY_RELATIVE_PATH = Path("query_workspace") / "unregistered_inventory.json"
KNOWN_WORKSPACE_FILES = {
    "index.json",
    "index.md",
    "index.html",
    "organization.json",
    "promotion_ledger.json",
    "unregistered_inventory.json",
}
IGNORED_DIRECTORY_NAMES = {"_working", ".work", "node_modules", "__pycache__"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_path(project_root: Path) -> Path:
    return project_root.resolve() / INVENTORY_RELATIVE_PATH


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "files": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unregistered Workspace inventory is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported or malformed unregistered Workspace inventory.")
    if not isinstance(value.get("files"), list):
        raise ValueError("Unregistered Workspace inventory files must be an array.")
    return value


def _add_path(paths: set[str], value: Any) -> None:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return
    if text.startswith("query_workspace/"):
        paths.add(text)


def _indexed_paths(project_root: Path) -> set[str]:
    paths = {
        f"query_workspace/{name}"
        for name in KNOWN_WORKSPACE_FILES
    }
    index_path = project_root / "query_workspace" / "index.json"
    if not index_path.is_file():
        return paths
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return paths
    entries = index.get("entries") if isinstance(index, dict) else []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        for key in ("current_path", "current_meta_path", "organization_path"):
            _add_path(paths, entry.get(key))
        for version in entry.get("versions", []) if isinstance(entry.get("versions"), list) else []:
            if not isinstance(version, dict):
                continue
            for key in ("path", "meta_path", "formalize_seed_path"):
                _add_path(paths, version.get(key))
            for output in version.get("derived_outputs", []) if isinstance(version.get("derived_outputs"), list) else []:
                if isinstance(output, dict):
                    _add_path(paths, output.get("path"))
            bundle = version.get("analysis_bundle")
            if isinstance(bundle, dict):
                _add_path(paths, bundle.get("path"))
            meta_path = str(version.get("meta_path") or "")
            if meta_path:
                try:
                    meta = json.loads((project_root / Path(meta_path)).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = {}
                if isinstance(meta, dict):
                    for output in meta.get("derived_outputs", []) if isinstance(meta.get("derived_outputs"), list) else []:
                        if isinstance(output, dict):
                            _add_path(paths, output.get("path"))
                    meta_bundle = meta.get("analysis_bundle")
                    if isinstance(meta_bundle, dict):
                        _add_path(paths, meta_bundle.get("path"))
    return paths


def scan(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    workspace = root / "query_workspace"
    if not workspace.is_dir():
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": root.name,
            "generated_at": now_iso(),
            "status": "pass",
            "files": [],
            "unregistered_count": 0,
            "changed_count": 0,
            "unchanged_count": 0,
        }
    previous = _read_json(inventory_path(root))
    previous_by_path = {
        str(item.get("path")): item
        for item in previous.get("files", [])
        if isinstance(item, dict) and str(item.get("path") or "")
    }
    indexed = _indexed_paths(root)
    rows: list[dict[str, Any]] = []
    changed_count = 0
    unchanged_count = 0
    generated_at = now_iso()
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        workspace_relative = path.relative_to(workspace).as_posix()
        if relative in indexed or workspace_relative.startswith("_working/"):
            continue
        if any(part in IGNORED_DIRECTORY_NAMES for part in Path(workspace_relative).parts):
            continue
        if path.name.startswith(".") or path.suffix == ".tmp":
            continue
        digest = _sha256(path)
        prior = previous_by_path.get(relative, {})
        unchanged = str(prior.get("sha256") or "") == digest
        if unchanged:
            unchanged_count += 1
        else:
            changed_count += 1
        rows.append(
            {
                "path": relative,
                "sha256": digest,
                "size_bytes": path.stat().st_size,
                "first_seen_at": str(prior.get("first_seen_at") or generated_at),
                "last_seen_at": generated_at,
                "state": "unregistered",
                "scan_action": "unchanged_skipped" if unchanged else ("changed" if prior else "discovered"),
            }
        )
    for relative, prior in previous_by_path.items():
        if any(row.get("path") == relative for row in rows):
            continue
        rows.append(
            {
                **prior,
                "state": "missing",
                "scan_action": "missing",
            }
        )
    rows.sort(key=lambda item: str(item.get("path") or ""))
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": root.name,
        "generated_at": generated_at,
        "status": "warn" if changed_count or rows else "pass",
        "files": rows,
        "unregistered_count": sum(1 for row in rows if row.get("state") == "unregistered"),
        "changed_count": changed_count,
        "unchanged_count": unchanged_count,
    }


def write_inventory(project_root: str | Path, report: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve()
    report = report or scan(root)
    path = inventory_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return {"status": "written", "path": INVENTORY_RELATIVE_PATH.as_posix(), "report": report}


__all__ = ["INVENTORY_RELATIVE_PATH", "SCHEMA_VERSION", "inventory_path", "scan", "write_inventory"]
