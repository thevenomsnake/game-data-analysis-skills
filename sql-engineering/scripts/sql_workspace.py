#!/usr/bin/env python3
"""Minimal immutable SQL workspace used by the public SQL Engineering Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_SCHEMA = "sql_engineering_public_project_v1"
REPOSITORY_SCHEMA = "sql_engineering_public_repository_v1"
INDEX_SCHEMA = "sql_workspace_index_v1"
META_SCHEMA = "sql_workspace_item_v1"
RECEIPT_SCHEMA = "sql_delivery_receipt_v1"
SOURCE_CATALOG_SCHEMA = "sql_source_catalog_v1"
KNOWLEDGE_CATALOG_SCHEMA = "sql_knowledge_catalog_v1"
RULE_CATALOG_SCHEMA = "sql_rule_catalog_v1"
SOURCE_CONTRACT_SCHEMA = "sql_source_contract_v1"
KNOWLEDGE_CONTRACT_SCHEMA = "sql_knowledge_contract_v1"
RULE_INPUT_SCHEMA = "sql_rule_input_v1"
RULE_DEFINITION_SCHEMA = "sql_canonical_rule_v1"
PROJECT_STATUS_SCHEMA = "sql_project_status_v1"
KINDS = ("temporary", "retained", "dashboard")
RESERVED_PROJECT_DIRECTORIES = ("_asset_catalog", "_review_inbox", "_rule_review")
ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


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


def relative_project_path(value: Any, field_name: str) -> str:
    normalized = str(value).strip().replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or ".." in normalized.split("/")
    ):
        raise ValueError(f"{field_name} must be a project-relative path: {value!r}")
    return normalized


def ensure_project_contracts(root: Path, config: dict[str, Any]) -> dict[str, Path]:
    project_id = str(config.get("project_id") or "").strip()
    original_catalogs = (
        config.get("source_catalog"),
        config.get("knowledge_catalog"),
        config.get("rule_catalog"),
    )
    source_relative = relative_project_path(
        config.setdefault("source_catalog", "sources/source-catalog.json"), "source_catalog"
    )
    rule_relative = relative_project_path(
        config.setdefault("rule_catalog", "rules/rule-catalog.json"), "rule_catalog"
    )
    knowledge_relative = relative_project_path(
        config.setdefault("knowledge_catalog", "knowledge/knowledge-catalog.json"), "knowledge_catalog"
    )
    source_path = root / source_relative
    rule_path = root / rule_relative
    knowledge_path = root / knowledge_relative
    if not source_path.exists():
        write_json(
            source_path,
            {"schema_version": SOURCE_CATALOG_SCHEMA, "project_id": project_id, "sources": []},
        )
    if not rule_path.exists():
        write_json(
            rule_path,
            {"schema_version": RULE_CATALOG_SCHEMA, "project_id": project_id, "rules": []},
        )
    if not knowledge_path.exists():
        write_json(
            knowledge_path,
            {"schema_version": KNOWLEDGE_CATALOG_SCHEMA, "project_id": project_id, "items": []},
        )
    (root / "sources" / "raw").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "planning").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "confirmed").mkdir(parents=True, exist_ok=True)
    (root / "rules" / "definitions").mkdir(parents=True, exist_ok=True)
    (root / "context").mkdir(parents=True, exist_ok=True)
    current_catalogs = (
        config.get("source_catalog"),
        config.get("knowledge_catalog"),
        config.get("rule_catalog"),
    )
    if current_catalogs != original_catalogs:
        _, config_path, _ = project_paths(root)
        write_json(config_path, config)
    return {
        "source_catalog": source_path,
        "knowledge_catalog": knowledge_path,
        "rule_catalog": rule_path,
    }


def load_project(root: Path) -> tuple[Path, dict[str, Any], Path]:
    root, config_path, index_path = project_paths(root)
    config = read_json(config_path)
    if config.get("schema_version") != PROJECT_SCHEMA:
        raise ValueError(f"Unsupported project schema in {config_path}")
    if not str(config.get("project_id", "")).strip():
        raise ValueError(f"project_id is required in {config_path}")
    context_paths = config.get("context_paths", [])
    if not isinstance(context_paths, list):
        raise ValueError(f"context_paths must be an array: {config_path}")
    for raw_path in context_paths:
        relative_project_path(raw_path, "context_paths")
    if config.get("source_catalog"):
        relative_project_path(config["source_catalog"], "source_catalog")
    if config.get("rule_catalog"):
        relative_project_path(config["rule_catalog"], "rule_catalog")
    if config.get("knowledge_catalog"):
        relative_project_path(config["knowledge_catalog"], "knowledge_catalog")
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
        if str(existing.get("dialect", "")).strip().lower() != args.dialect.strip().lower():
            raise ValueError("Refusing to change dialect through init; configure an execution environment instead")
        config = dict(existing)
    else:
        config = {
            "schema_version": PROJECT_SCHEMA,
            "project_id": args.project_id.strip(),
            "dialect": args.dialect.strip().lower(),
        }
    config.setdefault("source_catalog", "sources/source-catalog.json")
    config.setdefault("knowledge_catalog", "knowledge/knowledge-catalog.json")
    config.setdefault("rule_catalog", "rules/rule-catalog.json")
    config.setdefault("context_paths", [])
    if not config["project_id"] or not config["dialect"]:
        raise ValueError("project-id and dialect are required")
    write_json(config_path, config)
    contracts = ensure_project_contracts(root, config)
    if not index_path.exists():
        write_json(index_path, {"schema_version": INDEX_SCHEMA, "project_id": config["project_id"], "items": []})
    return {
        "status": "ready",
        "schema_version": PROJECT_SCHEMA,
        "project_root": str(root),
        "config_file": str(config_path),
        "index_file": str(index_path),
        "source_catalog": str(contracts["source_catalog"]),
        "knowledge_catalog": str(contracts["knowledge_catalog"]),
        "rule_catalog": str(contracts["rule_catalog"]),
    }


def command_environment(args: argparse.Namespace) -> dict[str, Any]:
    root, config, _ = load_project(Path(args.root))
    name = args.name.strip()
    dialect = args.dialect.strip().lower()
    connection_profile = args.connection_profile.strip()
    if not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
        raise ValueError("environment name must use letters, numbers, underscores, or hyphens")
    if not dialect or not connection_profile:
        raise ValueError("dialect and connection-profile are required")

    execution = config.setdefault("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("project execution configuration must be an object")
    environments = execution.setdefault("environments", {})
    if not isinstance(environments, dict):
        raise ValueError("project execution environments must be an object")
    environments[name] = {
        "dialect": dialect,
        "connection_profile": connection_profile,
    }
    if args.default or not str(execution.get("default_environment", "")).strip():
        execution["default_environment"] = name

    _, config_path, _ = project_paths(root)
    write_json(config_path, config)
    return {
        "status": "ready",
        "schema_version": PROJECT_SCHEMA,
        "project_root": str(root),
        "environment": name,
        "dialect": dialect,
        "connection_profile": connection_profile,
        "default_environment": execution.get("default_environment"),
        "config_file": str(config_path),
    }


def load_project_catalog(
    root: Path,
    config: dict[str, Any],
    config_key: str,
    schema_version: str,
    items_key: str,
) -> tuple[Path, dict[str, Any]]:
    paths = ensure_project_contracts(root, config)
    catalog_path = paths[config_key]
    catalog = read_json(catalog_path)
    if catalog.get("schema_version") != schema_version:
        raise ValueError(f"Unsupported catalog schema in {catalog_path}")
    if catalog.get("project_id") != config.get("project_id"):
        raise ValueError(f"Catalog does not match project: {catalog_path}")
    if not isinstance(catalog.get(items_key), list):
        raise ValueError(f"Catalog {items_key} must be an array: {catalog_path}")
    return catalog_path, catalog


def next_catalog_version(items: list[dict[str, Any]], slug: str) -> int:
    versions = []
    for item in items:
        if item.get("slug") != slug:
            continue
        match = re.fullmatch(r"v([0-9]{3})", str(item.get("version") or ""))
        if match:
            versions.append(int(match.group(1)))
    return max(versions, default=0) + 1


def preserved_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix and re.fullmatch(r"\.[a-z0-9_-]{1,12}", suffix):
        return suffix
    return ".bin"


def command_source(args: argparse.Namespace) -> dict[str, Any]:
    root, config, _ = load_project(Path(args.root))
    source = Path(args.file).resolve()
    if not source.is_file():
        raise ValueError(f"Source definition file does not exist: {source}")
    name = args.name.strip()
    description = args.description.strip()
    if not name or not description:
        raise ValueError("name and description are required")
    slug = args.slug.strip().lower() if args.slug else slugify(name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
        raise ValueError("source slug must use lowercase ASCII letters, numbers, and hyphens")
    data = source.read_bytes()
    if not data:
        raise ValueError("Source definition file is empty")
    digest = sha256_bytes(data)
    catalog_path, catalog = load_project_catalog(
        root, config, "source_catalog", SOURCE_CATALOG_SCHEMA, "sources"
    )
    existing = next(
        (item for item in catalog["sources"] if item.get("slug") == slug and item.get("content_sha256") == digest),
        None,
    )
    if existing:
        return {
            "status": "ready",
            "registration_status": "existing",
            "source_id": existing.get("source_id"),
            "source_file": str((root / str(existing["relative_path"])).resolve()),
            "catalog_file": str(catalog_path),
        }

    version = f"v{next_catalog_version(catalog['sources'], slug):03d}"
    suffix = preserved_suffix(source)
    destination = root / "sources" / "raw" / slug / f"{version}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError(f"Refusing to overwrite registered source: {destination}")
    shutil.copyfile(source, destination)
    relative_path = destination.relative_to(root).as_posix()
    source_format = args.source_format.strip().lower() if args.source_format else suffix.lstrip(".")
    source_id = f"{slug}:{version}"
    item = {
        "schema_version": SOURCE_CONTRACT_SCHEMA,
        "source_id": source_id,
        "slug": slug,
        "version": version,
        "name": name,
        "description": description,
        "format": source_format or "binary",
        "relative_path": relative_path,
        "content_sha256": digest,
        "original_file_name": source.name,
        "registered_at": utc_now(),
    }
    catalog["sources"].append(item)
    write_json(catalog_path, catalog)
    return {
        "status": "ready",
        "registration_status": "created",
        "source_id": source_id,
        "source_file": str(destination),
        "catalog_file": str(catalog_path),
        "content_sha256": digest,
    }


def command_knowledge(args: argparse.Namespace) -> dict[str, Any]:
    root, config, _ = load_project(Path(args.root))
    source = Path(args.file).resolve()
    if not source.is_file():
        raise ValueError(f"Knowledge file does not exist: {source}")
    kind = args.kind
    name = args.name.strip()
    description = args.description.strip()
    if not name or not description:
        raise ValueError("name and description are required")
    confirmed_by = args.confirmed_by.strip()
    confirmation_note = args.confirmation_note.strip()
    if kind == "confirmed" and (not confirmed_by or not confirmation_note):
        raise ValueError("confirmed knowledge requires confirmed-by and confirmation-note")
    if kind == "planning" and (confirmed_by or confirmation_note):
        raise ValueError("planning inputs are evidence, not confirmed knowledge")
    slug = args.slug.strip().lower() if args.slug else slugify(name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
        raise ValueError("knowledge slug must use lowercase ASCII letters, numbers, and hyphens")
    data = source.read_bytes()
    if not data:
        raise ValueError("Knowledge file is empty")
    digest = sha256_bytes(data)
    catalog_path, catalog = load_project_catalog(
        root, config, "knowledge_catalog", KNOWLEDGE_CATALOG_SCHEMA, "items"
    )
    based_on = sorted({str(item).strip() for item in (args.based_on or []) if str(item).strip()})
    known_ids = {str(item.get("knowledge_id")) for item in catalog["items"]}
    missing = [item for item in based_on if item not in known_ids]
    if missing:
        raise ValueError("Unknown based-on knowledge IDs: " + ", ".join(missing))
    existing = next(
        (
            item
            for item in catalog["items"]
            if item.get("kind") == kind and item.get("slug") == slug and item.get("content_sha256") == digest
        ),
        None,
    )
    if existing:
        return {
            "status": "ready",
            "registration_status": "existing",
            "knowledge_id": existing.get("knowledge_id"),
            "knowledge_file": str((root / str(existing["relative_path"])).resolve()),
            "catalog_file": str(catalog_path),
        }

    same_kind_items = [item for item in catalog["items"] if item.get("kind") == kind]
    version = f"v{next_catalog_version(same_kind_items, slug):03d}"
    destination = root / "knowledge" / kind / slug / f"{version}{preserved_suffix(source)}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError(f"Refusing to overwrite registered knowledge: {destination}")
    shutil.copyfile(source, destination)
    relative_path = destination.relative_to(root).as_posix()
    knowledge_id = f"{kind}:{slug}:{version}"
    item = {
        "schema_version": KNOWLEDGE_CONTRACT_SCHEMA,
        "knowledge_id": knowledge_id,
        "kind": kind,
        "status": "human_confirmed" if kind == "confirmed" else "source_evidence",
        "slug": slug,
        "version": version,
        "name": name,
        "description": description,
        "relative_path": relative_path,
        "content_sha256": digest,
        "original_file_name": source.name,
        "based_on": based_on,
        "confirmed_by": confirmed_by or None,
        "confirmation_note": confirmation_note or None,
        "registered_at": utc_now(),
    }
    catalog["items"].append(item)
    write_json(catalog_path, catalog)
    return {
        "status": "ready",
        "registration_status": "created",
        "knowledge_id": knowledge_id,
        "knowledge_file": str(destination),
        "catalog_file": str(catalog_path),
        "content_sha256": digest,
    }


def command_rule(args: argparse.Namespace) -> dict[str, Any]:
    root, config, _ = load_project(Path(args.root))
    rule_file = Path(args.rule_file).resolve()
    raw = rule_file.read_bytes() if rule_file.is_file() else b""
    if not raw:
        raise ValueError(f"Rule input file does not exist or is empty: {rule_file}")
    rule = read_json(rule_file)
    if rule.get("schema_version") != RULE_INPUT_SCHEMA:
        raise ValueError(f"Rule input must use {RULE_INPUT_SCHEMA}")
    concept_key = str(rule.get("concept_key") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", concept_key):
        raise ValueError("concept_key must use lowercase letters, numbers, dots, underscores, or hyphens")
    for field in ("title", "business_definition", "grain"):
        if not str(rule.get(field) or "").strip():
            raise ValueError(f"Rule input requires {field}")
    if not rule.get("calculation"):
        raise ValueError("Rule input requires calculation")
    source_refs = rule.get("source_contracts") or []
    knowledge_refs = rule.get("knowledge_contracts") or []
    if not isinstance(source_refs, list) or not isinstance(knowledge_refs, list):
        raise ValueError("source_contracts and knowledge_contracts must be arrays")
    if not source_refs and not knowledge_refs:
        raise ValueError("Rule input must cite at least one source or knowledge contract")
    if not isinstance(rule.get("filters", []), list):
        raise ValueError("filters must be an array")
    confirmed_by = args.confirmed_by.strip()
    confirmation_note = args.confirmation_note.strip()
    if not confirmed_by or not confirmation_note:
        raise ValueError("confirmed-by and confirmation-note are required to fix a canonical rule")

    source_catalog_path, source_catalog = load_project_catalog(
        root, config, "source_catalog", SOURCE_CATALOG_SCHEMA, "sources"
    )
    knowledge_catalog_path, knowledge_catalog = load_project_catalog(
        root, config, "knowledge_catalog", KNOWLEDGE_CATALOG_SCHEMA, "items"
    )
    known_sources = {str(item.get("source_id")) for item in source_catalog["sources"]}
    known_knowledge = {str(item.get("knowledge_id")) for item in knowledge_catalog["items"]}
    missing_sources = [str(item) for item in source_refs if str(item) not in known_sources]
    missing_knowledge = [str(item) for item in knowledge_refs if str(item) not in known_knowledge]
    if missing_sources or missing_knowledge:
        missing_text = [*("source:" + item for item in missing_sources), *("knowledge:" + item for item in missing_knowledge)]
        raise ValueError("Rule references unknown contracts: " + ", ".join(missing_text))

    catalog_path, catalog = load_project_catalog(root, config, "rule_catalog", RULE_CATALOG_SCHEMA, "rules")
    current = next((item for item in catalog["rules"] if item.get("concept_key") == concept_key), None)
    input_digest = sha256_bytes(raw)
    if current:
        current_path = root / str(current.get("current_path") or "")
        if current_path.is_file() and read_json(current_path).get("input_sha256") == input_digest:
            return {
                "status": "ready",
                "registration_status": "existing",
                "concept_key": concept_key,
                "rule_file": str(current_path),
                "catalog_file": str(catalog_path),
            }
    current_number = int(str(current.get("current_version", "v000"))[1:]) if current else 0
    version = f"v{current_number + 1:03d}"
    destination = root / "rules" / "definitions" / concept_key / f"{version}.json"
    if destination.exists():
        raise ValueError(f"Refusing to overwrite canonical rule: {destination}")
    definition = dict(rule)
    definition.update(
        {
            "schema_version": RULE_DEFINITION_SCHEMA,
            "concept_key": concept_key,
            "version": version,
            "status": "confirmed",
            "source_contracts": [str(item) for item in source_refs],
            "knowledge_contracts": [str(item) for item in knowledge_refs],
            "confirmed_by": confirmed_by,
            "confirmation_note": confirmation_note,
            "confirmed_at": utc_now(),
            "input_sha256": input_digest,
        }
    )
    write_json(destination, definition)
    entry = {
        "concept_key": concept_key,
        "title": str(rule["title"]),
        "current_version": version,
        "current_path": destination.relative_to(root).as_posix(),
        "confirmed_by": confirmed_by,
        "confirmation_note": confirmation_note,
        "updated_at": utc_now(),
    }
    if current:
        catalog["rules"][catalog["rules"].index(current)] = entry
    else:
        catalog["rules"].append(entry)
    write_json(catalog_path, catalog)
    return {
        "status": "ready",
        "registration_status": "created",
        "concept_key": concept_key,
        "version": version,
        "rule_file": str(destination),
        "catalog_file": str(catalog_path),
        "source_catalog_file": str(source_catalog_path),
        "knowledge_catalog_file": str(knowledge_catalog_path),
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    root, config, _ = load_project(Path(args.root))
    source_catalog_path, source_catalog = load_project_catalog(
        root, config, "source_catalog", SOURCE_CATALOG_SCHEMA, "sources"
    )
    knowledge_catalog_path, knowledge_catalog = load_project_catalog(
        root, config, "knowledge_catalog", KNOWLEDGE_CATALOG_SCHEMA, "items"
    )
    rule_catalog_path, rule_catalog = load_project_catalog(
        root, config, "rule_catalog", RULE_CATALOG_SCHEMA, "rules"
    )
    planning_count = sum(item.get("kind") == "planning" for item in knowledge_catalog["items"])
    confirmed_count = sum(item.get("kind") == "confirmed" for item in knowledge_catalog["items"])
    execution = config.get("execution") if isinstance(config.get("execution"), dict) else {}
    environments = execution.get("environments") if isinstance(execution.get("environments"), dict) else {}
    warnings = []
    if not source_catalog["sources"]:
        warnings.append("register_at_least_one_raw_telemetry_definition")
    if planning_count == 0:
        warnings.append("no_planning_or_config_tables_registered")
    if confirmed_count == 0:
        warnings.append("no_human_confirmed_knowledge_registered")
    if not rule_catalog["rules"]:
        warnings.append("no_canonical_rules_fixed")
    if not environments:
        warnings.append("automatic_execution_not_configured_manual_handoff_only")
    return {
        "schema_version": PROJECT_STATUS_SCHEMA,
        "status": "ready",
        "project_id": config["project_id"],
        "dialect": config["dialect"],
        "raw_source_count": len(source_catalog["sources"]),
        "planning_knowledge_count": planning_count,
        "confirmed_knowledge_count": confirmed_count,
        "canonical_rule_count": len(rule_catalog["rules"]),
        "execution_environment_count": len(environments),
        "default_environment": execution.get("default_environment"),
        "source_catalog": str(source_catalog_path),
        "knowledge_catalog": str(knowledge_catalog_path),
        "rule_catalog": str(rule_catalog_path),
        "query_context_ready": bool(source_catalog["sources"]),
        "automatic_execution_configured": bool(environments),
        "manual_execution_available": True,
        "warnings": warnings,
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
            contracts = ensure_project_contracts(project_root, config)
            write_json(config_path, config)
            project_result = {
                "status": "existing",
                "project_root": str(project_root),
                "config_file": str(config_path),
                "index_file": str(index_path),
                "source_catalog": str(contracts["source_catalog"]),
                "knowledge_catalog": str(contracts["knowledge_catalog"]),
                "rule_catalog": str(contracts["rule_catalog"]),
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

    requested_environment = str(getattr(args, "environment", "") or "").strip()
    execution = config.get("execution") if isinstance(config.get("execution"), dict) else {}
    if not requested_environment:
        requested_environment = str(execution.get("default_environment", "") or "").strip()
    environments = execution.get("environments") if isinstance(execution.get("environments"), dict) else {}
    environment_config = environments.get(requested_environment) if requested_environment else None
    if requested_environment and not isinstance(environment_config, dict):
        raise ValueError(f"Unknown execution environment: {requested_environment}")
    saved_dialect = (
        str(environment_config.get("dialect", "")).strip().lower()
        if isinstance(environment_config, dict)
        else str(config["dialect"]).strip().lower()
    )

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
        "dialect": saved_dialect,
        "execution_environment": requested_environment or None,
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
            "dialect": saved_dialect,
            "execution_environment": requested_environment or None,
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

    environment = subparsers.add_parser("environment")
    environment.add_argument("--root", required=True)
    environment.add_argument("--name", required=True)
    environment.add_argument("--dialect", required=True)
    environment.add_argument("--connection-profile", required=True)
    environment.add_argument("--default", action="store_true")
    environment.set_defaults(handler=command_environment)

    source = subparsers.add_parser("source")
    source.add_argument("--root", required=True)
    source.add_argument("--file", required=True)
    source.add_argument("--name", required=True)
    source.add_argument("--description", required=True)
    source.add_argument("--source-format", default="")
    source.add_argument("--slug", default="")
    source.set_defaults(handler=command_source)

    knowledge = subparsers.add_parser("knowledge")
    knowledge.add_argument("--root", required=True)
    knowledge.add_argument("--file", required=True)
    knowledge.add_argument("--kind", choices=("planning", "confirmed"), required=True)
    knowledge.add_argument("--name", required=True)
    knowledge.add_argument("--description", required=True)
    knowledge.add_argument("--slug", default="")
    knowledge.add_argument("--confirmed-by", default="")
    knowledge.add_argument("--confirmation-note", default="")
    knowledge.add_argument("--based-on", action="append")
    knowledge.set_defaults(handler=command_knowledge)

    rule = subparsers.add_parser("rule")
    rule.add_argument("--root", required=True)
    rule.add_argument("--rule-file", required=True)
    rule.add_argument("--confirmed-by", required=True)
    rule.add_argument("--confirmation-note", required=True)
    rule.set_defaults(handler=command_rule)

    status = subparsers.add_parser("status")
    status.add_argument("--root", required=True)
    status.set_defaults(handler=command_status)

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
    save.add_argument("--environment", default="")
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
