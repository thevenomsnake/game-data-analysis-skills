#!/usr/bin/env python3
"""Audit the reviewed source-to-public allowlist without copying source files."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


MANIFEST_RELATIVE = Path("tools") / "public-sync-allowlist.json"
TEXT_SUFFIXES = {".cmd", ".html", ".js", ".json", ".md", ".py", ".sql", ".txt", ".yaml", ".yml"}
_PROJECT_TERMS = (
    "RM_" + "OBT",
    "RM_" + "CBT3",
    "RM_" + "ABTEST",
    "rm" + "cn",
    "ieg" + "_tdbank",
    "rmtest" + "_dsl_",
)
FORBIDDEN_TEXT = re.compile(
    r"(?i)gitlab" + r"\.wd\.com|\b(?:" + "|".join(map(re.escape, _PROJECT_TERMS)) + r")\b|"
    r"(?:[A-Z]:[\\/](?:Users|AI_space)[\\/]|/" + r"home/[^/\s]+/)|Better" + r"Xml[\\/]"
)


class SyncAuditError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SyncAuditError(f"Cannot read JSON: {path}") from error
    if not isinstance(value, dict):
        raise SyncAuditError(f"Manifest must be an object: {path}")
    return value


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _files(root: Path, watch_roots: list[str], excluded: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for watch_root in watch_roots:
        base = root / watch_root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix.lower() == ".pyc":
                continue
            relative = path.relative_to(root).as_posix()
            if _matches(relative, excluded):
                continue
            result[relative] = path
    return result


def _fingerprint(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def _scan_forbidden(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        match = FORBIDDEN_TEXT.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return f"unreadable text: {path}"
    return match.group(0) if match else None


def audit(source_root: str | Path, public_root: str | Path) -> dict[str, Any]:
    public = Path(public_root).resolve()
    source = Path(source_root).resolve()
    manifest = _read_json(public / MANIFEST_RELATIVE)
    if manifest.get("schema_version") != "public_sync_allowlist_v1":
        raise SyncAuditError("Unsupported public sync allowlist schema.")
    watch_roots = [str(item) for item in manifest.get("watch_roots", [])]
    excluded = [str(item) for item in manifest.get("excluded_source_globs", [])]
    public_only = [str(item) for item in manifest.get("allowed_public_only_globs", [])]
    exact_paths = [str(item) for item in manifest.get("exact_paths", [])]
    if not source.is_dir():
        raise SyncAuditError(f"Source root does not exist: {source}")
    source_files = _files(source, watch_roots, excluded)
    public_files = _files(public, watch_roots, [])

    source_only = sorted(set(source_files) - set(public_files))
    public_only_paths = sorted(
        path for path in set(public_files) - set(source_files) if not _matches(path, public_only)
    )
    exact_missing: list[str] = []
    exact_drift: list[str] = []
    for relative in exact_paths:
        source_path = source / relative
        public_path = public / relative
        if not source_path.is_file() or not public_path.is_file():
            exact_missing.append(relative)
        elif _fingerprint(source_path) != _fingerprint(public_path):
            exact_drift.append(relative)

    forbidden: list[dict[str, str]] = []
    for relative in exact_paths:
        path = source_files.get(relative) or source / relative
        if not path.is_file():
            continue
        sample = _scan_forbidden(path)
        if sample:
            forbidden.append({"path": relative, "sample": sample})

    changed_review: list[str] = sorted(
        relative
        for relative in set(source_files) & set(public_files)
        if _fingerprint(source_files[relative]) != _fingerprint(public_files[relative])
        and relative not in exact_paths
    )
    blocking = bool(source_only or public_only_paths or exact_missing or exact_drift or forbidden)
    return {
        "schema_version": "public_sync_audit_v1",
        "status": "block" if blocking else "pass",
        "source_root": str(source),
        "public_root": str(public),
        "watch_roots": watch_roots,
        "source_file_count": len(source_files),
        "public_file_count": len(public_files),
        "source_only": source_only,
        "public_only": public_only_paths,
        "exact_missing": exact_missing,
        "exact_drift": exact_drift,
        "forbidden": forbidden,
        "changed_review": changed_review,
        "changed_review_count": len(changed_review),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit",))
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--public-root", default=".")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args(argv)
    try:
        result = audit(args.source_root, args.public_root)
    except (OSError, SyncAuditError) as error:
        result = {"schema_version": "public_sync_audit_v1", "status": "error", "error": str(error)}
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"status={result['status']}")
        for key in ("source_only", "public_only", "exact_missing", "exact_drift", "forbidden", "changed_review"):
            values = result.get(key, [])
            print(f"{key}={len(values)}")
            for value in values[:20]:
                print(f"- {value}")
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
