#!/usr/bin/env python3
"""Validate and fingerprint the public repository before a release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


TEXT_SUFFIXES = {".md", ".py", ".js", ".json", ".yaml", ".yml", ".toml", ".txt", ".sql", ".ps1", ".cmd"}
ALLOWED_ROOT_FILES = {"README.md", "README.zh-CN.md", "README.zh-TW.md", "README.ja.md", "README.ko.md", "README.es.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md", ".gitignore"}
FORBIDDEN_TOP_LEVEL = {"betterxml", "knowledge-base", "planning-sources", "outputs", "work"}
FORBIDDEN_SUFFIXES = {".xlsx", ".xls", ".parquet", ".db", ".sqlite", ".pem", ".key", ".xml", ".csv", ".tsv"}
FORBIDDEN_PATTERNS = (
    ("internal_host", re.compile(r"gitlab\.wd\.com", re.I)),
    ("internal_project_id", re.compile(r"\b(?:RM_OBT|RM_CBT3|RM_ABTEST|rm_obt|rm_cbt3|rm_abtest|rmcn|ieg_tdbank|rmtest_dsl_)\b", re.I)),
    ("absolute_machine_path", re.compile(r"(?i)(?:[A-Z]:[\\/](?:Users|AI_space)[\\/]|/home/[^/\s]+/)")),
    ("credential_url", re.compile(r"(?i)https?://[^/\s:@]+:[^/\s@]+@")),
    ("credential_assignment", re.compile(r"(?i)\b(?:password|passwd|pwd|token|api[_-]?key|secret)\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("personal_tool_path", re.compile(r"(?i)BetterXml[\\/]")),
)


def files(root: Path) -> list[Path]:
    ignored = {".git", "__pycache__", ".tmp", ".test-tmp", ".local", "dist"}
    result = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        if path.name == "release-manifest.json":
            continue
        if "sql-projects" in path.parts and path.name != "README.md":
            continue
        result.append(path)
    return sorted(result, key=lambda path: path.as_posix())


def validate(root: Path) -> dict[str, object]:
    root = root.resolve()
    findings: list[dict[str, object]] = []
    if not root.is_dir():
        return {"schema_version": "public_release_validation_v1", "status": "block", "root": str(root), "findings": [{"id": "root_missing", "path": str(root)}]}
    for child in root.iterdir():
        if child.name.lower() in FORBIDDEN_TOP_LEVEL or child.name.startswith(".public-") or child.name in {".tmp", ".test-tmp"}:
            findings.append({"id": "forbidden_top_level", "path": child.name})
        if child.name == "sql-projects" and child.is_dir():
            for project_child in child.iterdir():
                if project_child.name != "README.md" and not project_child.name.startswith("_"):
                    findings.append({"id": "project_data_present", "path": project_child.relative_to(root).as_posix()})
    for path in files(root):
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() in FORBIDDEN_SUFFIXES and not relative.startswith("sql-engineering/assets/examples/"):
            findings.append({"id": "forbidden_file_type", "path": relative})
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            findings.append({"id": "file_too_large", "path": relative})
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"LICENSE", ".gitignore"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append({"id": "non_utf8", "path": relative})
            continue
        if relative != "tools/public_release.py":
            for finding_id, pattern in FORBIDDEN_PATTERNS:
                match = pattern.search(text)
                if match:
                    findings.append({"id": finding_id, "path": relative, "sample": match.group(0)[:80]})
        if path.suffix.lower() == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as error:
                findings.append({"id": "invalid_json", "path": relative, "detail": str(error)})
        if path.suffix.lower() == ".py":
            try:
                compile(text, relative, "exec")
            except SyntaxError as error:
                findings.append({"id": "python_syntax", "path": relative, "detail": str(error)})
    for required in ("README.md", "README.zh-CN.md", "setup/SKILL.md", "setup/scripts/bootstrap_repo.py", "setup/schemas/setup-config.json", "sql-engineering/SKILL.md"):
        if not (root / required).is_file():
            findings.append({"id": "required_path_missing", "path": required})
    return {
        "schema_version": "public_release_validation_v1",
        "status": "pass" if not findings else "block",
        "root": str(root),
        "file_count": len(files(root)),
        "findings": findings,
    }


def manifest(root: Path) -> dict[str, object]:
    checked = validate(root)
    if checked["status"] != "pass":
        return {"status": "block", "validation": checked}
    entries = []
    for path in files(root):
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        entries.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    tree_input = "\n".join(f"{item['path']}\0{item['sha256']}" for item in entries).encode("utf-8")
    return {
        "schema_version": "public_release_manifest_v1",
        "status": "ready",
        "file_count": len(entries),
        "source_tree_sha256": hashlib.sha256(tree_input).hexdigest(),
        "files": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "manifest"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = validate(args.root) if args.command == "validate" else manifest(args.root.resolve())
    if args.output and result.get("status") in {"ready", "pass"}:
        args.output.resolve().write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ready", "pass"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
