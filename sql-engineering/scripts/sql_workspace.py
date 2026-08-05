#!/usr/bin/env python3
"""Minimal immutable SQL workspace used by the public SQL Engineering Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_SCHEMA = "sql_engineering_public_project_v1"
REPOSITORY_SCHEMA = "sql_engineering_public_repository_v1"
INDEX_SCHEMA = "sql_workspace_index_v1"
META_SCHEMA = "sql_workspace_item_v1"
RECEIPT_SCHEMA = "sql_delivery_receipt_v1"
KINDS = ("temporary", "retained", "dashboard")
RESERVED_PROJECT_DIRECTORIES = ("_asset_catalog", "_review_inbox", "_rule_review")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def project_paths(root: Path) -> tuple[Path, Path, Path]:
    root = root.resolve()
    return root, root / ".sql-engineering" / "project.json", root / "sql-workspace" / "index.json"


def load_project(root: Path) -> tuple[Path, dict[str, Any], Path]:
    root, config_path, index_path = project_paths(root)
    config = read_json(config_path)
    if config.get("schema_version") != PROJECT_SCHEMA:
        raise ValueError(f"Unsupported project schema in {config_path}")
    if not str(config.get("project_id", "")).strip():
        raise ValueError(f"project_id is required in {config_path}")
    return root, config, index_path


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if slug:
        return slug[:80]
    return "query-" + sha256_bytes(value.encode("utf-8"))[:12]


def next_version(family_dir: Path) -> int:
    versions = []
    if family_dir.exists():
        for path in family_dir.glob("v[0-9][0-9][0-9].sql"):
            versions.append(int(path.stem[1:]))
    return max(versions, default=0) + 1


def load_index(index_path: Path, project_id: str) -> dict[str, Any]:
    if not index_path.exists():
        return {"schema_version": INDEX_SCHEMA, "project_id": project_id, "items": []}
    index = read_json(index_path)
    if index.get("schema_version") != INDEX_SCHEMA or index.get("project_id") != project_id:
        raise ValueError(f"Index does not match project: {index_path}")
    if not isinstance(index.get("items"), list):
        raise ValueError(f"Index items must be a list: {index_path}")
    return index


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    root, config_path, index_path = project_paths(Path(args.root))
    if config_path.exists() and not args.force:
        raise ValueError(f"Project already initialized: {config_path}")
    if config_path.exists() and args.force:
        existing = read_json(config_path)
        if str(existing.get("project_id", "")) != args.project_id.strip():
            raise ValueError("Refusing to change project-id in an initialized workspace")
    config = {
        "schema_version": PROJECT_SCHEMA,
        "project_id": args.project_id.strip(),
        "dialect": args.dialect.strip().lower(),
    }
    if not config["project_id"] or not config["dialect"]:
        raise ValueError("project-id and dialect are required")
    write_json(config_path, config)
    if not index_path.exists():
        write_json(index_path, {"schema_version": INDEX_SCHEMA, "project_id": config["project_id"], "items": []})
    return {
        "status": "ready",
        "schema_version": PROJECT_SCHEMA,
        "project_root": str(root),
        "config_file": str(config_path),
        "index_file": str(index_path),
    }


def command_bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = Path(args.root).resolve()
    projects_root = workspace_root / "sql-projects"
    projects_root.mkdir(parents=True, exist_ok=True)

    reserved_paths = []
    for directory_name in RESERVED_PROJECT_DIRECTORIES:
        directory = projects_root / directory_name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch(exist_ok=True)
        reserved_paths.append(str(directory))

    project_result: dict[str, Any] | None = None
    project_id = args.project_id.strip()
    dialect = args.dialect.strip().lower()
    if project_id or dialect:
        if not project_id or not dialect:
            raise ValueError("project-id and dialect must be supplied together")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project_id):
            raise ValueError("project-id must use letters, numbers, underscores, or hyphens")
        project_root = projects_root / project_id
        _, config_path, index_path = project_paths(project_root)
        if config_path.exists():
            config = read_json(config_path)
            if config.get("schema_version") != PROJECT_SCHEMA:
                raise ValueError(f"Unsupported project schema in {config_path}")
            if config.get("project_id") != project_id or config.get("dialect") != dialect:
                raise ValueError(f"Existing project configuration conflicts with bootstrap request: {config_path}")
            if not index_path.exists():
                write_json(index_path, {"schema_version": INDEX_SCHEMA, "project_id": project_id, "items": []})
            project_result = {
                "status": "existing",
                "project_root": str(project_root),
                "config_file": str(config_path),
                "index_file": str(index_path),
            }
        else:
            project_result = command_init(
                argparse.Namespace(
                    root=str(project_root),
                    project_id=project_id,
                    dialect=dialect,
                    force=False,
                )
            )

    return {
        "status": "ready",
        "schema_version": REPOSITORY_SCHEMA,
        "workspace_root": str(workspace_root),
        "projects_root": str(projects_root),
        "reserved_directories": reserved_paths,
        "project": project_result,
    }


def command_save(args: argparse.Namespace) -> dict[str, Any]:
    root, config, index_path = load_project(Path(args.root))
    source = Path(args.sql_file).resolve()
    if not source.is_file():
        raise ValueError(f"SQL input does not exist: {source}")
    data = source.read_bytes()
    if not data.strip():
        raise ValueError("SQL input is empty")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SQL input must be UTF-8") from exc

    title = args.title.strip()
    summary = args.summary.strip()
    if not title or not summary:
        raise ValueError("title and summary are required")
    slug = args.slug.strip().lower() if args.slug else slugify(title)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
        raise ValueError("slug must use lowercase ASCII letters, numbers, and hyphens")

    family_dir = root / "sql-workspace" / args.kind / slug
    version_number = next_version(family_dir)
    version = f"v{version_number:03d}"
    sql_path = family_dir / f"{version}.sql"
    meta_path = family_dir / f"{version}.meta.json"
    if sql_path.exists() or meta_path.exists():
        raise ValueError(f"Refusing to overwrite existing version: {sql_path}")

    sql_path.parent.mkdir(parents=True, exist_ok=True)
    sql_path.write_bytes(data)
    digest = sha256_bytes(data)
    relative_sql = sql_path.relative_to(root).as_posix()
    relative_meta = meta_path.relative_to(root).as_posix()
    created_at = utc_now()
    asset_id = f"{config['project_id']}:{args.kind}:{slug}:{version}"
    tags = sorted({tag.strip() for tag in (args.tag or []) if tag.strip()})
    meta = {
        "schema_version": META_SCHEMA,
        "asset_id": asset_id,
        "project_id": config["project_id"],
        "kind": args.kind,
        "slug": slug,
        "version": version,
        "title": title,
        "summary": summary,
        "tags": tags,
        "dialect": config["dialect"],
        "relative_path": relative_sql,
        "content_sha256": digest,
        "source": {"file_name": source.name, "content_sha256": digest},
        "created_at": created_at,
    }
    write_json(meta_path, meta)

    index = load_index(index_path, config["project_id"])
    index["items"].append(
        {
            "asset_id": asset_id,
            "kind": args.kind,
            "slug": slug,
            "version": version,
            "title": title,
            "summary": summary,
            "tags": tags,
            "relative_path": relative_sql,
            "meta_path": relative_meta,
            "content_sha256": digest,
            "created_at": created_at,
        }
    )
    write_json(index_path, index)
    return delivery_receipt(root, sql_path)


def delivery_receipt(root: Path, sql_path: Path) -> dict[str, Any]:
    root, config, index_path = load_project(root)
    sql_path = sql_path.resolve()
    try:
        relative = sql_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("SQL file must be inside the project root") from exc
    if not sql_path.is_file():
        raise ValueError(f"Saved SQL does not exist: {sql_path}")
    meta_path = sql_path.with_suffix(".meta.json")
    meta = read_json(meta_path)
    actual_hash = sha256_bytes(sql_path.read_bytes())
    index = load_index(index_path, config["project_id"])
    indexed = next((item for item in index["items"] if item.get("relative_path") == relative), None)
    blockers = []
    if meta.get("relative_path") != relative:
        blockers.append("metadata_path_mismatch")
    if meta.get("content_sha256") != actual_hash:
        blockers.append("metadata_hash_mismatch")
    if not indexed:
        blockers.append("not_indexed")
    elif indexed.get("content_sha256") != actual_hash:
        blockers.append("index_hash_mismatch")
    return {
        "schema_version": RECEIPT_SCHEMA,
        "status": "ready" if not blockers else "blocked",
        "asset_id": meta.get("asset_id"),
        "delivery_file": str(sql_path),
        "project_relative_path": relative,
        "content_sha256": actual_hash,
        "blockers": blockers,
    }


def command_receipt(args: argparse.Namespace) -> dict[str, Any]:
    return delivery_receipt(Path(args.root), Path(args.sql_file))


def command_search(args: argparse.Namespace) -> dict[str, Any]:
    root, config, index_path = load_project(Path(args.root))
    index = load_index(index_path, config["project_id"])
    query = args.query.strip().lower()
    matches = []
    for item in reversed(index["items"]):
        haystack = " ".join(
            str(value) for value in [item.get("title"), item.get("summary"), *(item.get("tags") or [])]
        ).lower()
        if not query or all(token in haystack for token in query.split()):
            match = dict(item)
            match["absolute_path"] = str((root / str(item["relative_path"])).resolve())
            matches.append(match)
    return {"schema_version": "sql_workspace_search_v1", "status": "ready", "query": query, "matches": matches}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--root", required=True)
    bootstrap.add_argument("--project-id", default="")
    bootstrap.add_argument("--dialect", default="")
    bootstrap.set_defaults(handler=command_bootstrap)

    init = subparsers.add_parser("init")
    init.add_argument("--root", required=True)
    init.add_argument("--project-id", required=True)
    init.add_argument("--dialect", required=True)
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=command_init)

    save = subparsers.add_parser("save")
    save.add_argument("--root", required=True)
    save.add_argument("--sql-file", required=True)
    save.add_argument("--title", required=True)
    save.add_argument("--summary", required=True)
    save.add_argument("--kind", choices=KINDS, default="temporary")
    save.add_argument("--slug", default="")
    save.add_argument("--tag", action="append")
    save.set_defaults(handler=command_save)

    receipt = subparsers.add_parser("receipt")
    receipt.add_argument("--root", required=True)
    receipt.add_argument("--sql-file", required=True)
    receipt.set_defaults(handler=command_receipt)

    search = subparsers.add_parser("search")
    search.add_argument("--root", required=True)
    search.add_argument("--query", default="")
    search.set_defaults(handler=command_search)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except ValueError as exc:
        result = {"status": "blocked", "error": str(exc)}
        code = 2
    else:
        code = 0 if result.get("status") == "ready" else 2
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return code


if __name__ == "__main__":
    sys.exit(main())
