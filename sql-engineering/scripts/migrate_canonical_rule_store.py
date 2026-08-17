#!/usr/bin/env python3
"""Audit and migrate legacy monolithic canonical rules into Rule Store v2."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from capability_registry import command_function_ids
from function_gate import (
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from rule_store import (
    DEFINITIONS_RELATIVE_PATH,
    INDEX_RELATIVE_PATH,
    LEGACY_RELATIVE_PATH,
    STORE_RELATIVE_PATH,
    STORE_SCHEMA_VERSION,
    VERSION_SCHEMA_VERSION,
    RuleStore,
    RuleStoreError,
    atomic_write_json,
    compact_reference,
    file_sha256,
    object_sha256,
)


MIGRATION_SCHEMA_VERSION = "canonical_rule_store_migration_v2"
MIGRATION_RECEIPT_RELATIVE_PATH = Path("rules/migrations/canonical-rule-store-v2.json")
STAGING_RELATIVE_PATH = Path("rules/.migration-staging/canonical-rule-store-v2/project")
MANIFEST_CONTRACT = {
    "contract_version": STORE_SCHEMA_VERSION,
    "store": STORE_RELATIVE_PATH.as_posix(),
    "activation_index": INDEX_RELATIVE_PATH.as_posix(),
    "definitions_root": DEFINITIONS_RELATIVE_PATH.as_posix(),
}
BUSINESS_CONTENT_FIELDS = (
    "rule_id",
    "concept_key",
    "version",
    "title",
    "content",
    "source",
    "source_evidence",
    "confirmed_by_user",
    "scope",
    "lifetime",
    "applies_to",
    "affected_artifacts",
    "decision_question",
    "supersedes",
    "created_at",
    "updated_at",
    "notes",
    "change_authorization",
    "activation_contract",
    "structured_definition",
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def legacy_path(root: Path) -> Path:
    return root / LEGACY_RELATIVE_PATH


def business_content(rule: dict[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(rule[field]) for field in BUSINESS_CONTENT_FIELDS if field in rule}


def audit_legacy(root: Path) -> dict[str, Any]:
    source = legacy_path(root)
    if not source.exists():
        raise RuleStoreError(f"Legacy canonical rule file does not exist: {source}")
    document = read_json(source)
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise RuleStoreError(f"Legacy canonical rules array is missing: {source}")
    statuses = Counter(str(rule.get("status") or "missing") for rule in rules)
    concepts: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for ordinal, rule in enumerate(rules, start=1):
        concept_key = str(rule.get("concept_key") or "").strip()
        concepts[concept_key].append((ordinal, rule))

    duplicate_concept_versions: list[dict[str, Any]] = []
    multiple_rule_ids: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    for concept_key, rows in sorted(concepts.items()):
        version_groups = Counter(int(rule.get("version") or 0) for _, rule in rows)
        for version, count in sorted(version_groups.items()):
            if count > 1:
                duplicate_concept_versions.append(
                    {"concept_key": concept_key, "rule_version": version, "count": count}
                )
        rule_ids = sorted({str(rule.get("rule_id") or "") for _, rule in rows})
        if len(rule_ids) > 1:
            multiple_rule_ids.append({"concept_key": concept_key, "rule_ids": rule_ids})
        for store_version, (ordinal, rule) in enumerate(rows, start=1):
            target_rows.append(
                {
                    "source_ordinal": ordinal,
                    "concept_key": concept_key,
                    "rule_id": str(rule.get("rule_id") or ""),
                    "rule_version": int(rule.get("version") or 0),
                    "effective_status": str(rule.get("status") or ""),
                    "store_version": store_version,
                    "target_path": (
                        DEFINITIONS_RELATIVE_PATH / concept_key / f"v{store_version:03d}.json"
                    ).as_posix(),
                    "record_sha256": object_sha256(rule),
                    "business_content_sha256": object_sha256(business_content(rule)),
                }
            )
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "mode": "audit",
        "project_id": root.name,
        "project_root": ".",
        "source_path": LEGACY_RELATIVE_PATH.as_posix(),
        "source_sha256": file_sha256(source),
        "source_bytes": source.stat().st_size,
        "source_updated_at": str(document.get("updated_at") or ""),
        "record_count": len(rules),
        "concept_count": len(concepts),
        "status_counts": dict(sorted(statuses.items())),
        "target_files": target_rows,
        "anomalies": {
            "missing_concept_keys": [
                row["source_ordinal"] for row in target_rows if not row["concept_key"]
            ],
            "duplicate_concept_versions": duplicate_concept_versions,
            "concepts_with_multiple_rule_ids": multiple_rule_ids,
        },
    }


def _reference_from_target(target: dict[str, Any], rule: dict[str, Any], target_file: Path) -> dict[str, Any]:
    return {
        "store_version": int(target["store_version"]),
        "rule_id": str(rule.get("rule_id") or ""),
        "rule_version": int(rule.get("version") or 0),
        "effective_status": str(rule.get("status") or ""),
        "path": str(target["target_path"]),
        "record_sha256": str(target["record_sha256"]),
        "file_sha256": file_sha256(target_file),
        "created_at": str(rule.get("created_at") or ""),
    }


def build_staging_store(root: Path, audit: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    source_document = read_json(legacy_path(root))
    rules = source_document.get("rules", [])
    stage_root = root / STAGING_RELATIVE_PATH
    stage_container = stage_root.parent
    if stage_container.exists():
        shutil.rmtree(stage_container)
    stage_root.mkdir(parents=True, exist_ok=True)

    targets_by_ordinal = {
        int(row["source_ordinal"]): row for row in audit.get("target_files", [])
    }
    concept_versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mappings: list[dict[str, Any]] = []
    for ordinal, rule in enumerate(rules, start=1):
        target = targets_by_ordinal[ordinal]
        relative = Path(str(target["target_path"]))
        target_file = stage_root / relative
        version_document = {
            "schema_version": VERSION_SCHEMA_VERSION,
            "project_id": root.name,
            "concept_key": str(target["concept_key"]),
            "store_version": int(target["store_version"]),
            "record": copy.deepcopy(rule),
        }
        atomic_write_json(target_file, version_document)
        reference = _reference_from_target(target, rule, target_file)
        concept_versions[str(target["concept_key"])].append(reference)
        mappings.append(
            {
                **copy.deepcopy(target),
                "target_file_sha256": reference["file_sha256"],
            }
        )

    concepts: dict[str, Any] = {}
    for concept_key, versions in sorted(concept_versions.items()):
        confirmed = [row for row in versions if row.get("effective_status") == "confirmed"]
        if len(confirmed) > 1:
            raise RuleStoreError(f"More than one current confirmed rule for {concept_key}")
        concepts[concept_key] = {
            "concept_key": concept_key,
            "latest_store_version": max(int(row["store_version"]) for row in versions),
            "latest_rule_version": max(int(row["rule_version"]) for row in versions),
            "current_confirmed": compact_reference(confirmed[0]) if confirmed else None,
            "proposed_versions": [
                compact_reference(row) for row in versions if row.get("effective_status") == "proposed"
            ],
            "deprecated_versions": [
                compact_reference(row) for row in versions if row.get("effective_status") == "deprecated"
            ],
            "versions": versions,
        }
    store_document = {
        "schema_version": STORE_SCHEMA_VERSION,
        "project_id": root.name,
        "updated_at": str(source_document.get("updated_at") or ""),
        "authorization_contract": copy.deepcopy(source_document.get("authorization_contract") or {}),
        "concepts": concepts,
    }
    atomic_write_json(stage_root / STORE_RELATIVE_PATH, store_document)
    stage_store = RuleStore(stage_root)
    stage_store.rebuild_activation_index()
    validation = stage_store.validate_store(require_no_legacy=True)
    if validation.get("status") != "ok":
        raise RuleStoreError("Staging rule store failed validation:\n- " + "\n- ".join(validation["errors"]))

    current_selection = {
        status: [
            {
                "concept_key": mapping["concept_key"],
                "rule_id": mapping["rule_id"],
                "rule_version": mapping["rule_version"],
                "store_version": mapping["store_version"],
                "target_path": mapping["target_path"],
            }
            for mapping in mappings
            if mapping["effective_status"] == status
        ]
        for status in ("confirmed", "proposed", "deprecated", "superseded")
    }
    receipt = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "status": "verified",
        "project_id": root.name,
        "source": {
            "path": audit["source_path"],
            "sha256": audit["source_sha256"],
            "bytes": audit["source_bytes"],
            "record_count": audit["record_count"],
            "concept_count": audit["concept_count"],
            "status_counts": audit["status_counts"],
        },
        "target": {
            "contract": MANIFEST_CONTRACT,
            "store_sha256": object_sha256(store_document),
            "store_file_sha256": file_sha256(stage_root / STORE_RELATIVE_PATH),
            "activation_index_file_sha256": file_sha256(stage_root / INDEX_RELATIVE_PATH),
            "definition_count": len(mappings),
        },
        "record_mappings": mappings,
        "current_selection": current_selection,
        "anomalies": copy.deepcopy(audit.get("anomalies") or {}),
        "validation": validation,
    }
    atomic_write_json(stage_root / MIGRATION_RECEIPT_RELATIVE_PATH, receipt)
    return stage_root, receipt


def _update_manifest(root: Path) -> None:
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    manifest.pop("canonical_rule_file", None)
    manifest["canonical_rule_store"] = copy.deepcopy(MANIFEST_CONTRACT)
    atomic_write_json(manifest_path, manifest)


def publish_staging(root: Path, stage_root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    rules_root = root / "rules"
    destinations = [
        (stage_root / STORE_RELATIVE_PATH, root / STORE_RELATIVE_PATH),
        (stage_root / INDEX_RELATIVE_PATH, root / INDEX_RELATIVE_PATH),
        (stage_root / DEFINITIONS_RELATIVE_PATH, root / DEFINITIONS_RELATIVE_PATH),
        (stage_root / MIGRATION_RECEIPT_RELATIVE_PATH, root / MIGRATION_RECEIPT_RELATIVE_PATH),
    ]
    collisions = [str(target) for _, target in destinations if target.exists()]
    if collisions:
        raise RuleStoreError("Rule Store v2 targets already exist:\n- " + "\n- ".join(collisions))
    for _, target in destinations:
        target.parent.mkdir(parents=True, exist_ok=True)
    for source, target in destinations:
        os.replace(source, target)
    _update_manifest(root)

    source = legacy_path(root)
    if file_sha256(source) != receipt["source"]["sha256"]:
        raise RuleStoreError("Legacy source changed during migration; refusing to delete it.")
    source.unlink()
    stage_container = root / STAGING_RELATIVE_PATH.parts[0] / STAGING_RELATIVE_PATH.parts[1]
    if stage_container.exists():
        shutil.rmtree(stage_container)
    result = verify_migration(root)
    if result.get("status") != "ok":
        raise RuleStoreError("Published rule store failed verification:\n- " + "\n- ".join(result["errors"]))
    return result


def verify_migration(root: Path) -> dict[str, Any]:
    receipt_path = root / MIGRATION_RECEIPT_RELATIVE_PATH
    errors: list[str] = []
    if not receipt_path.exists():
        return {"status": "error", "errors": [f"Missing migration receipt: {receipt_path}"]}
    receipt = read_json(receipt_path)
    store = RuleStore(root)
    validation = store.validate_store(require_no_legacy=True)
    errors.extend(validation.get("errors", []))
    mappings = receipt.get("record_mappings", []) or []
    if len(mappings) != int(receipt.get("source", {}).get("record_count") or 0):
        errors.append("Migration receipt mapping count does not match the legacy record count.")
    if len(mappings) != int(receipt.get("target", {}).get("definition_count") or 0):
        errors.append("Migration receipt mapping count does not match the target definition count.")
    for mapping in mappings:
        target = root / str(mapping.get("target_path") or "")
        if not target.exists():
            errors.append(f"Migrated version is missing: {mapping.get('target_path')}")
            continue
        document = read_json(target)
        record = document.get("record") or {}
        if object_sha256(record) != mapping.get("record_sha256"):
            errors.append(f"Record hash mismatch after migration: {mapping.get('target_path')}")
        if object_sha256(business_content(record)) != mapping.get("business_content_sha256"):
            errors.append(f"Business-content hash mismatch after migration: {mapping.get('target_path')}")
        if file_sha256(target) != mapping.get("target_file_sha256"):
            errors.append(f"Target-file hash mismatch after migration: {mapping.get('target_path')}")
    manifest = read_json(root / "manifest.json")
    if manifest.get("canonical_rule_store") != MANIFEST_CONTRACT:
        errors.append("Manifest canonical_rule_store contract is missing or incorrect.")
    if "canonical_rule_file" in manifest:
        errors.append("Manifest still contains canonical_rule_file.")
    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "project_id": root.name,
        "validation": validation,
        "receipt": MIGRATION_RECEIPT_RELATIVE_PATH.as_posix(),
        "source_sha256": receipt.get("source", {}).get("sha256", ""),
        "record_count": len(mappings),
        "concept_count": validation.get("counts", {}).get("concepts", 0),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "migrate", "verify"):
        item = subparsers.add_parser(name)
        item.add_argument("--root", required=True)
        item.add_argument("--format", choices=("json", "summary"), default="json")
        add_function_gate_arguments(
            item,
            selection_help="Allowed routes: SKILL_EVOLUTION, RULES, or PROJECT_ADMIN.",
        )
    return parser.parse_args()


def print_result(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"status: {payload.get('status') or payload.get('mode')}")
    print(f"project: {payload.get('project_id')}")
    if "record_count" in payload:
        print(f"records: {payload.get('record_count')}")
    if "concept_count" in payload:
        print(f"concepts: {payload.get('concept_count')}")
    if payload.get("source_sha256"):
        print(f"source_sha256: {payload['source_sha256']}")
    for error in payload.get("errors", []) or []:
        print(f"ERROR: {error}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    root = Path(args.root).resolve()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("migrate_canonical_rule_store.py", args.command),
            purpose="canonical rule store migration",
        )
        if args.command == "migrate":
            require_user_request(args.user_request, purpose="migrate canonical rule storage")
    except Exception as exc:  # pragma: no cover - shared CLI gate
        exit_with_gate_error(argparse.ArgumentParser(), exc)
    try:
        if args.command == "audit":
            payload = audit_legacy(root)
        elif args.command == "verify":
            payload = verify_migration(root)
        else:
            audit = audit_legacy(root)
            stage_root, receipt = build_staging_store(root, audit)
            payload = publish_staging(root, stage_root, receipt)
    except (OSError, ValueError, RuleStoreError, json.JSONDecodeError) as exc:
        payload = {"status": "error", "project_id": root.name, "errors": [str(exc)]}
    print_result(payload, args.format)
    if payload.get("status") == "error":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
