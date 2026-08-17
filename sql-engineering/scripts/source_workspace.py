#!/usr/bin/env python3
"""Manage local, unconfirmed source folders without promoting them to knowledge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from capability_registry import command_function_ids
from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)


ROOTS_CONTRACT = "source_workspace_roots_v1"
CATALOG_CONTRACT = "source_workspace_catalog_v1"
SELECTION_CONTRACT = "source_workspace_selection_v1"
ROOTS_PATH = Path(".local/source_roots.json")
CATALOG_PATH = Path(".local/source_workspace/catalog.json")
SELECTION_DIR = Path(".local/source_workspace/selections")
ROOT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
SOURCE_KINDS = (
    "code_reference",
    "external_reference",
    "tlog_document",
)
DEFAULT_EXTENSIONS = (
    ".csv",
    ".cs",
    ".json",
    ".md",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
)
SKIP_DIRS = {"__pycache__", "$recycle.bin", "system volume information"}


class SourceWorkspaceError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def request_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SourceWorkspaceError(f"invalid local source workspace JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise SourceWorkspaceError(f"local source workspace file must contain an object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def repo_root(value: Path) -> Path:
    root = value.resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise SourceWorkspaceError(f"repository root is invalid: {root}")
    return root


def normalize_extensions(values: list[str]) -> list[str]:
    source = values or list(DEFAULT_EXTENSIONS)
    extensions = []
    for value in source:
        extension = value.strip().lower()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = "." + extension
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", extension):
            raise SourceWorkspaceError(f"invalid file extension: {value}")
        extensions.append(extension)
    if not extensions:
        raise SourceWorkspaceError("at least one allowed extension is required")
    return sorted(set(extensions))


def validate_root_id(value: str) -> str:
    root_id = value.strip().lower()
    if not ROOT_ID_RE.fullmatch(root_id):
        raise SourceWorkspaceError(
            "root id must start with a letter and contain only lowercase letters, digits, _ or -"
        )
    return root_id


def roots_file(root: Path) -> Path:
    return root / ROOTS_PATH


def catalog_file(root: Path) -> Path:
    return root / CATALOG_PATH


def load_roots(root: Path) -> dict[str, Any]:
    payload = load_json(
        roots_file(root),
        {"contract_version": ROOTS_CONTRACT, "updated_at": "", "roots": []},
    )
    if payload.get("contract_version") != ROOTS_CONTRACT or not isinstance(payload.get("roots"), list):
        raise SourceWorkspaceError("unsupported local source roots contract")
    return payload


def find_root(payload: dict[str, Any], root_id: str) -> dict[str, Any]:
    for row in payload["roots"]:
        if isinstance(row, dict) and row.get("root_id") == root_id:
            return row
    raise SourceWorkspaceError(f"source root is not configured: {root_id}")


def project_allowed(row: dict[str, Any], project: str | None) -> bool:
    scope = row.get("project_scope") or []
    return not project or not scope or project in scope


def configure_root(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    root_id = validate_root_id(args.root_id)
    source_path = args.path.resolve()
    if not source_path.is_dir() or source_path.parent == source_path:
        raise SourceWorkspaceError(f"source root must be an existing non-system directory: {source_path}")
    try:
        local_relative = source_path.relative_to(root)
    except ValueError:
        local_relative = None
    if local_relative is not None and (not local_relative.parts or local_relative.parts[0].lower() != ".local"):
        raise SourceWorkspaceError("a source root inside the repository must live under .local/")

    payload = load_roots(root)
    projects = sorted({item.strip() for item in args.project if item.strip()})
    timestamp = now_iso()
    entry = {
        "root_id": root_id,
        "kind": args.kind,
        "path": str(source_path),
        "project_scope": projects,
        "recursive": bool(args.recursive),
        "extensions": normalize_extensions(args.extension),
        "max_files": args.max_files,
        "configured_at": timestamp,
        "audit": {
            "function_id": "SOURCE_WORKSPACE",
            "user_request_sha256": request_hash(args.user_request.strip()),
        },
    }
    rows = [row for row in payload["roots"] if row.get("root_id") != root_id]
    rows.append(entry)
    payload.update(updated_at=timestamp, roots=sorted(rows, key=lambda item: item["root_id"]))
    write_json(roots_file(root), payload)
    return {
        "contract_version": ROOTS_CONTRACT,
        "status": "configured",
        "root": entry,
        "local_config": ROOTS_PATH.as_posix(),
        "tracked": False,
    }


def list_roots(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    payload = load_roots(root)
    rows = [row for row in payload["roots"] if project_allowed(row, args.project)]
    return {
        "contract_version": ROOTS_CONTRACT,
        "status": "ok",
        "project": args.project or "",
        "roots": rows,
        "local_config": ROOTS_PATH.as_posix(),
    }


def candidate_id(root_id: str, relative_path: str) -> str:
    return "src-" + hashlib.sha256(f"{root_id}\0{relative_path}".encode("utf-8")).hexdigest()[:16]


def iter_source_files(source_root: Path, recursive: bool, extensions: set[str]):
    for directory, subdirs, files in os.walk(source_root, followlinks=False):
        current = Path(directory)
        subdirs[:] = [
            name
            for name in subdirs
            if not name.startswith(".")
            and name.lower() not in SKIP_DIRS
            and not (current / name).is_symlink()
        ]
        for name in sorted(files):
            path = current / name
            if name.startswith((".", "~$")) or path.is_symlink():
                continue
            if path.suffix.lower() in extensions:
                yield path
        if not recursive:
            break


def scan_root(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    roots = load_roots(root)
    row = find_root(roots, validate_root_id(args.root_id))
    if not project_allowed(row, args.project):
        raise SourceWorkspaceError(f"source root {row['root_id']} is not scoped to project {args.project}")
    source_root = Path(row["path"])
    if not source_root.is_dir():
        raise SourceWorkspaceError(f"configured source root is unavailable: {row['root_id']}")

    entries = []
    max_files = int(row["max_files"])
    for path in iter_source_files(source_root, bool(row["recursive"]), set(row["extensions"])):
        if len(entries) >= max_files:
            raise SourceWorkspaceError(
                f"source root {row['root_id']} exceeds max_files={max_files}; narrow the root or reconfigure the limit"
            )
        stat = path.stat()
        relative = path.relative_to(source_root).as_posix()
        entries.append(
            {
                "candidate_id": candidate_id(row["root_id"], relative),
                "relative_path": relative,
                "file_name": path.name,
                "extension": path.suffix.lower(),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
                "discovery_state": "discovered",
                "content_hash_status": "not_computed",
            }
        )
    entries.sort(key=lambda item: item["relative_path"].lower())

    catalog_path = catalog_file(root)
    catalog = load_json(
        catalog_path,
        {"contract_version": CATALOG_CONTRACT, "updated_at": "", "roots": {}},
    )
    if catalog.get("contract_version") != CATALOG_CONTRACT or not isinstance(catalog.get("roots"), dict):
        raise SourceWorkspaceError("unsupported local source workspace catalog contract")
    timestamp = now_iso()
    catalog["updated_at"] = timestamp
    catalog["roots"][row["root_id"]] = {
        "root_id": row["root_id"],
        "kind": row["kind"],
        "project_scope": row["project_scope"],
        "scanned_at": timestamp,
        "candidate_count": len(entries),
        "candidates": entries,
    }
    write_json(catalog_path, catalog)
    return {
        "contract_version": CATALOG_CONTRACT,
        "status": "scanned",
        "root_id": row["root_id"],
        "candidate_count": len(entries),
        "preview": entries[: args.preview_limit],
        "preview_truncated": len(entries) > args.preview_limit,
        "catalog": CATALOG_PATH.as_posix(),
        "tracked": False,
    }


def content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_path(source_root: Path, relative_value: str) -> tuple[Path, str]:
    relative = PurePosixPath(relative_value.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SourceWorkspaceError("relative path must stay inside the configured source root")
    path = (source_root / Path(*relative.parts)).resolve()
    try:
        normalized = path.relative_to(source_root.resolve()).as_posix()
    except ValueError as error:
        raise SourceWorkspaceError("selected path escapes the configured source root") from error
    if not path.is_file() or path.is_symlink():
        raise SourceWorkspaceError(f"selected source file does not exist: {relative.as_posix()}")
    return path, normalized


def select_source(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    roots = load_roots(root)
    row = find_root(roots, validate_root_id(args.root_id))
    if not project_allowed(row, args.project):
        raise SourceWorkspaceError(f"source root {row['root_id']} is not scoped to project {args.project}")
    source_root = Path(row["path"])
    path, relative = selected_path(source_root, args.relative_path)
    if path.suffix.lower() not in set(row["extensions"]):
        raise SourceWorkspaceError(f"selected file extension is not allowed: {path.suffix.lower()}")
    digest = content_sha256(path)
    selection_id = "sel-" + hashlib.sha256(
        f"{row['root_id']}\0{relative}\0{digest}".encode("utf-8")
    ).hexdigest()[:16]
    receipt = {
        "contract_version": SELECTION_CONTRACT,
        "selection_id": selection_id,
        "state": "selected_not_reviewed",
        "knowledge_status": "unregistered",
        "root_id": row["root_id"],
        "kind": row["kind"],
        "project_scope": row["project_scope"],
        "relative_path": relative,
        "original_file_name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
        "selected_at": now_iso(),
        "audit": {
            "function_id": "SOURCE_WORKSPACE",
            "user_request_sha256": request_hash(args.user_request.strip()),
        },
    }
    receipt_path = root / SELECTION_DIR / f"{selection_id}.json"
    write_json(receipt_path, receipt)
    return {
        **receipt,
        "source_file": str(path),
        "selection_receipt": receipt_path.relative_to(root).as_posix(),
        "tracked": False,
        "allowed_next_step": "Use an explicit KNOWLEDGE request to review, copy, register, and bind this file.",
    }


def authorize(args: argparse.Namespace, command: str) -> None:
    if command != "list":
        require_user_request(args.user_request, purpose=f"source workspace {command}")
    require_user_function_selection(
        args.function_selection,
        user_request=args.user_request,
        allowed_ids=command_function_ids(Path(__file__).name, command),
        purpose=f"source workspace {command}",
    )


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=["json"], default="json")
    add_function_gate_arguments(parser, selection_help="Optional explicit route: [SOURCE_WORKSPACE].")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="Configure one unmanaged candidate-source root.")
    add_common(configure)
    configure.add_argument("--root-id", required=True)
    configure.add_argument("--kind", choices=SOURCE_KINDS, required=True)
    configure.add_argument("--path", type=Path, required=True)
    configure.add_argument("--project", action="append", default=[])
    configure.add_argument("--recursive", action="store_true")
    configure.add_argument("--extension", action="append", default=[])
    configure.add_argument("--max-files", type=int, default=2000, choices=range(1, 100001))

    listing = subparsers.add_parser("list", help="List configured local source roots.")
    add_common(listing)
    listing.add_argument("--project")

    scan = subparsers.add_parser("scan", help="Build a bounded local candidate catalog.")
    add_common(scan)
    scan.add_argument("--root-id", required=True)
    scan.add_argument("--project")
    scan.add_argument("--preview-limit", type=int, default=30, choices=range(0, 201))

    select = subparsers.add_parser("select", help="Hash one exact candidate without promoting it.")
    add_common(select)
    select.add_argument("--root-id", required=True)
    select.add_argument("--relative-path", required=True)
    select.add_argument("--project")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        root = repo_root(args.repo_root)
        authorize(args, args.command)
        if args.command == "configure":
            result = configure_root(args, root)
        elif args.command == "list":
            result = list_roots(args, root)
        elif args.command == "scan":
            result = scan_root(args, root)
        else:
            result = select_source(args, root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except FunctionGateError as error:
        exit_with_gate_error(parser, error)
    except SourceWorkspaceError as error:
        parser.exit(2, f"BLOCKED: {error}\n")


if __name__ == "__main__":
    main()
