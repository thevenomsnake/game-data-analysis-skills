#!/usr/bin/env python3
"""Plan and apply the one-way migration to Formal Asset Packages."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from typing import Any, Iterable, Mapping

from formal_asset_repository import (
    FormalAssetRepositoryError,
    INDEX_SCHEMA_VERSION,
    apply_plan as apply_repository_plan,
    list_packages,
    load_package,
    plan_package,
    validate_receipt,
)


PLAN_SCHEMA_VERSION = "formal_asset_migration_plan_v1"
MAP_SCHEMA_VERSION = "formal_asset_path_migration_map_v1"
PROJECT_MANIFEST_SCHEMA_VERSION = "project_manifest_v2"
DECISIONS_SCHEMA_VERSION = "legacy_quarantine_decisions_v1"
LEGACY_FORMAL_DIRS = ("query_sql", "dashboard_sql", "validations", "runs")
LEGACY_ALL_DIRS = (*LEGACY_FORMAL_DIRS, "archive")
FORMAL_ROOT = "formal_assets"
MIGRATION_MAP_REL = f"{FORMAL_ROOT}/migration-map.v1.json"
MIGRATION_PLAN_REL = f"{FORMAL_ROOT}/migration-plan.v1.json"
MIGRATION_REPORT_REL = f"{FORMAL_ROOT}/migration-report.v1.md"
BACKUP_DIR = ".formal-asset-migration-backup"


class MigrationError(ValueError):
    """Raised when a migration cannot be planned or safely applied."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"Cannot read JSON object {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MigrationError(f"Expected a JSON object: {path}")
    return payload


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str, *, label: str) -> str:
    text = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationError(f"{label} must be a safe project-relative path: {value}")
    return path.as_posix()


def _member_id(path: str) -> str:
    return f"M-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:20]}"


def _package_slug(artifact: Mapping[str, Any]) -> str:
    slug = str(artifact.get("slug") or "").strip()
    if not slug:
        raise MigrationError(f"Formal QUERY has no stable slug: {artifact.get('path')}")
    return slug


def _explicit_bundle(artifact: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Read an explicit multi-query bundle declaration from legacy metadata.

    Migration may only merge query families when the old manifest records the
    relationship explicitly. Titles, tags, and SQL similarity are intentionally
    ignored. ``analysis_bundle`` is the canonical field; the two aliases keep
    historical manifests readable during the one-way migration.
    """

    raw = artifact.get("analysis_bundle")
    if not isinstance(raw, Mapping):
        raw = artifact.get("formal_asset_bundle")
    if not isinstance(raw, Mapping):
        bundle_id = artifact.get("analysis_bundle_id") or artifact.get("bundle_id")
        raw = {"bundle_id": bundle_id} if bundle_id else {}
    bundle_id = str(raw.get("bundle_id") or raw.get("id") or "").strip()
    if not bundle_id:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}", bundle_id):
        raise MigrationError(f"Invalid explicit analysis bundle id: {bundle_id}")
    role = str(raw.get("role") or "").strip().lower()
    title = str(raw.get("title") or "").strip()
    return bundle_id, role, title


def _bundle_package_slug(bundle_id: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9]+", "-", bundle_id.lower()).strip("-") or "bundle"
    suffix = hashlib.sha256(bundle_id.encode("utf-8")).hexdigest()[:8]
    return f"bundle-{readable[:55].rstrip('-')}-{suffix}"


def _artifact_state(artifact: Mapping[str, Any]) -> str:
    state = str(artifact.get("artifact_state") or "current").lower()
    return state if state in {"current", "history", "archived"} else "current"


def _target_for_source(source: str) -> str:
    path = PurePosixPath(source)
    first = path.parts[0]
    prefixes = {
        "query_sql": "queries",
        "dashboard_sql": "dashboards",
        "validations": "validations",
        "runs": "evidence",
    }
    if first not in prefixes:
        raise MigrationError(f"Not a legacy formal path: {source}")
    return PurePosixPath(prefixes[first], *path.parts[1:]).as_posix()


def _role_for_path(source: str) -> str:
    path = PurePosixPath(source)
    first = path.parts[0]
    name = path.name.lower()
    if first == "query_sql":
        base = "formal_query"
    elif first == "dashboard_sql":
        base = "dashboard_delivery"
    elif first == "validations":
        base = "validation"
    else:
        if name.endswith(".record.json"):
            return "run_record"
        if len(path.parts) > 2 or path.suffix.lower() not in {".md", ".json"}:
            return "derived_output"
        return "run_evidence"
    if name.endswith(".spec.json"):
        return f"{base}_spec"
    if name.endswith(".meta.json"):
        return f"{base}_meta"
    return f"{base}_sql"


def _artifact_files(root: Path, artifact: Mapping[str, Any]) -> list[str]:
    sql_path = _safe_relative(str(artifact.get("path") or ""), label="artifact path")
    sql = root / Path(sql_path)
    if not sql.is_file():
        raise MigrationError(f"Manifest artifact is missing: {sql_path}")
    spec_value = str(artifact.get("spec_path") or "")
    spec_path = _safe_relative(spec_value, label="artifact spec path") if spec_value else sql.with_suffix(".spec.json").relative_to(root).as_posix()
    meta_path = sql.with_suffix(".meta.json").relative_to(root).as_posix()
    required = [sql_path, spec_path, meta_path]
    missing = [path for path in required if not (root / Path(path)).is_file()]
    if missing:
        raise MigrationError(f"Formal artifact closure is incomplete for {sql_path}: {missing}")
    return required


def _query_family_for_artifact(
    artifact: Mapping[str, Any],
    query_by_path: Mapping[str, str],
    query_slugs: set[str],
    query_slug_to_package: Mapping[str, str],
    validation_to_query: Mapping[str, str],
) -> tuple[str, str] | None:
    path = str(artifact.get("path") or "").replace("\\", "/")
    kind = str(artifact.get("kind") or "").upper()
    if kind == "QUERY":
        return query_by_path.get(path, _package_slug(artifact)), "query_family"
    linked_query = str(artifact.get("linked_query") or "").replace("\\", "/")
    if linked_query in query_by_path:
        return query_by_path[linked_query], "explicit_linked_query"
    if kind == "DASHBOARD":
        linked_validation = str(artifact.get("linked_validation") or "").replace("\\", "/")
        if linked_validation in validation_to_query:
            return validation_to_query[linked_validation], "explicit_linked_validation"
    slug = str(artifact.get("slug") or "")
    suffix = "-validation" if kind == "VALIDATION" else "-dashboard" if kind == "DASHBOARD" else ""
    if suffix and slug.endswith(suffix) and slug[: -len(suffix)] in query_slugs:
        return query_slug_to_package[slug[: -len(suffix)]], "canonical_family_slug"
    return None


def _collect_run_paths(root: Path, record: Mapping[str, Any]) -> list[str]:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            text = value.replace("\\", "/")
            if text.startswith("runs/"):
                try:
                    relative = _safe_relative(text, label="run member path")
                except MigrationError:
                    return
                if (root / Path(relative)).is_file():
                    found.add(relative)

    visit(record)
    run_id = str(record.get("run_id") or "").strip()
    if run_id:
        bundle_root = root / "runs" / run_id
        if bundle_root.is_dir():
            found.update(
                path.relative_to(root).as_posix()
                for path in bundle_root.rglob("*")
                if path.is_file()
            )
    return sorted(found)


def _lineage_edge(
    edges: list[dict[str, str]],
    relation: str,
    source_path: str,
    target_path: str,
    member_by_source: Mapping[str, str],
    *,
    note: str = "",
) -> None:
    source_id = member_by_source.get(source_path)
    target_id = member_by_source.get(target_path)
    if not source_id or not target_id or source_id == target_id:
        return
    edge = {
        "relation": relation,
        "from_member_id": source_id,
        "to_member_id": target_id,
    }
    if note:
        edge["note"] = note
    if edge not in edges:
        edges.append(edge)


def _next_package_ids(root: Path, slugs: Iterable[str]) -> dict[str, str]:
    existing = list_packages(root)
    existing_by_slug = {str(item.get("slug") or ""): str(item.get("package_id") or "") for item in existing}
    used = [int(str(item.get("package_id") or "FA-0000").split("-")[-1]) for item in existing]
    next_number = max(used, default=0) + 1
    result: dict[str, str] = {}
    for slug in sorted(set(slugs)):
        if slug in existing_by_slug:
            result[slug] = existing_by_slug[slug]
        else:
            result[slug] = f"FA-{next_number:04d}"
            next_number += 1
    return result


def build_plan(project_root: str | Path, *, include_archive: bool = True) -> dict[str, Any]:
    root = Path(project_root).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise MigrationError(f"Project manifest is missing: {manifest_path}")
    manifest = _read_json(manifest_path)
    artifacts = [item for item in manifest.get("artifacts", []) if isinstance(item, dict)]
    queries = [item for item in artifacts if str(item.get("kind") or "").upper() == "QUERY"]
    query_slug_to_package: dict[str, str] = {}
    bundle_details: dict[str, dict[str, str]] = {}
    for item in queries:
        raw_slug = _package_slug(item)
        bundle = _explicit_bundle(item)
        if bundle is None:
            package_key = raw_slug
        else:
            bundle_id, bundle_role, bundle_title = bundle
            package_key = _bundle_package_slug(bundle_id)
            bundle_details.setdefault(
                package_key,
                {"bundle_id": bundle_id, "title": bundle_title, "role": bundle_role},
            )
        query_slug_to_package.setdefault(raw_slug, package_key)
    query_by_path = {
        str(item.get("path") or "").replace("\\", "/"): query_slug_to_package[_package_slug(item)]
        for item in queries
    }
    query_slugs = set(query_slug_to_package)
    package_keys = set(query_by_path.values())
    validation_to_query: dict[str, str] = {}
    for item in artifacts:
        if str(item.get("kind") or "").upper() != "VALIDATION":
            continue
        linked = str(item.get("linked_query") or "").replace("\\", "/")
        if linked in query_by_path:
            validation_to_query[str(item.get("path") or "").replace("\\", "/")] = query_by_path[linked]
    package_ids = _next_package_ids(root, package_keys)
    packages: dict[str, dict[str, Any]] = {
        package_key: {
            "package_id": package_ids[package_key],
            "slug": package_key,
            "title": next(
                (
                    str(item.get("title") or package_key)
                    for item in queries
                    if query_by_path.get(str(item.get("path") or "").replace("\\", "/")) == package_key
                ),
                package_key,
            ),
            "lifecycle_state": "current",
            "members": [],
            "lineage": [],
            "reasons": ["query_family"],
        }
        for package_key in sorted(package_keys)
    }
    for package_key, details in bundle_details.items():
        package = packages[package_key]
        if details.get("title"):
            package["title"] = details["title"]
        package["analysis_bundle"] = {
            "bundle_id": details["bundle_id"],
            "roles": sorted(
                {
                    str(_explicit_bundle(item)[1])
                    for item in queries
                    if query_by_path.get(str(item.get("path") or "").replace("\\", "/")) == package_key
                    and _explicit_bundle(item) is not None
                    and _explicit_bundle(item)[1]
                }
            ),
        }
    source_owner: dict[str, str] = {}
    source_state: dict[str, str] = {}
    artifact_owner: dict[str, str] = {}
    artifact_records: dict[str, Mapping[str, Any]] = {}
    unresolved: list[dict[str, str]] = []

    for artifact in artifacts:
        relation = _query_family_for_artifact(
            artifact,
            query_by_path,
            query_slugs,
            query_slug_to_package,
            validation_to_query,
        )
        source_artifact = str(artifact.get("path") or "").replace("\\", "/")
        if not relation:
            unresolved.append({
                "source_path": source_artifact,
                "reason": "formal artifact has no strong query-family lineage",
                "kind": str(artifact.get("kind") or ""),
            })
            continue
        slug, reason = relation
        artifact_owner[source_artifact] = slug
        artifact_records[source_artifact] = artifact
        if reason not in packages[slug]["reasons"]:
            packages[slug]["reasons"].append(reason)
        for source in _artifact_files(root, artifact):
            owner = source_owner.setdefault(source, slug)
            if owner != slug:
                unresolved.append({
                    "source_path": source,
                    "reason": f"formal member has conflicting package owners: {owner}, {slug}",
                    "kind": str(artifact.get("kind") or ""),
                })
                continue
            source_state[source] = _artifact_state(artifact)

    run_records = [item for item in manifest.get("run_evidence", []) if isinstance(item, dict)]
    run_record_by_path: dict[str, Mapping[str, Any]] = {}
    for record in run_records:
        source_artifact = str(record.get("source_artifact") or record.get("sql_path") or "").replace("\\", "/")
        slug = artifact_owner.get(source_artifact)
        if not slug and source_artifact in query_by_path:
            slug = query_by_path[source_artifact]
        if not slug:
            for path in _collect_run_paths(root, record):
                unresolved.append({
                    "source_path": path,
                    "reason": "run evidence has no strong owning formal query",
                    "kind": "RUN_EVIDENCE",
                })
            continue
        for source in _collect_run_paths(root, record):
            owner = source_owner.setdefault(source, slug)
            if owner != slug:
                unresolved.append({
                    "source_path": source,
                    "reason": f"run member has conflicting package owners: {owner}, {slug}",
                    "kind": "RUN_EVIDENCE",
                })
            source_state[source] = "current"
            if source == str(record.get("path") or "").replace("\\", "/"):
                run_record_by_path[source] = record

    all_legacy_files: set[str] = set()
    for directory in LEGACY_FORMAL_DIRS:
        path = root / directory
        if path.is_dir():
            all_legacy_files.update(
                file.relative_to(root).as_posix() for file in path.rglob("*") if file.is_file()
            )
    for source in sorted(all_legacy_files - set(source_owner)):
        unresolved.append({
            "source_path": source,
            "reason": "legacy formal file is not registered in a manifest closure",
            "kind": "UNREGISTERED",
        })

    entries: list[dict[str, Any]] = []
    member_by_source: dict[str, str] = {}
    for source, slug in sorted(source_owner.items()):
        target = _target_for_source(source)
        member_id = _member_id(target)
        member_by_source[source] = member_id
        source_path = root / Path(source)
        entry = {
            "source_path": source,
            "source_sha256": _sha256_file(source_path),
            "size_bytes": source_path.stat().st_size,
            "action": "move_to_package",
            "package_id": package_ids[slug],
            "package_slug": slug,
            "member_id": member_id,
            "member_role": _role_for_path(source),
            "lifecycle_state": source_state.get(source, "current"),
            "target_path": target,
            "reason": "manifest_lineage",
        }
        entries.append(entry)
        packages[slug]["members"].append({
            "source_path": source,
            "source_sha256": entry["source_sha256"],
            "size_bytes": entry["size_bytes"],
            "target_path": target,
            "member_id": member_id,
            "role": entry["member_role"],
            "lifecycle_state": entry["lifecycle_state"],
        })

    for source_artifact, artifact in artifact_records.items():
        source_spec = str(artifact.get("spec_path") or "").replace("\\", "/")
        source_meta = str((root / Path(source_artifact)).with_suffix(".meta.json").relative_to(root)).replace("\\", "/")
        package = packages[artifact_owner[source_artifact]]
        _lineage_edge(package["lineage"], "described_by", source_artifact, source_spec, member_by_source)
        _lineage_edge(package["lineage"], "described_by", source_artifact, source_meta, member_by_source)
        for old_path in artifact.get("supersedes", []) if isinstance(artifact.get("supersedes"), list) else []:
            _lineage_edge(
                package["lineage"],
                "superseded_by",
                str(old_path).replace("\\", "/"),
                source_artifact,
                member_by_source,
            )
        linked_query = str(artifact.get("linked_query") or "").replace("\\", "/")
        if linked_query:
            relation = "derived_from" if str(artifact.get("kind") or "").upper() == "DASHBOARD" else "validates"
            _lineage_edge(package["lineage"], relation, source_artifact, linked_query, member_by_source)
        linked_validation = str(artifact.get("linked_validation") or "").replace("\\", "/")
        if linked_validation:
            _lineage_edge(package["lineage"], "validated_by", source_artifact, linked_validation, member_by_source)
        linked_run = str(artifact.get("linked_run") or "").replace("\\", "/")
        if linked_run:
            _lineage_edge(package["lineage"], "evidenced_by", source_artifact, linked_run, member_by_source)

    # Preserve explicit grouped/overall (or other named) bundle membership as
    # lineage. This is the only permitted automatic cross-family merge.
    bundle_sources: dict[tuple[str, str], list[str]] = {}
    for artifact in queries:
        bundle = _explicit_bundle(artifact)
        source = str(artifact.get("path") or "").replace("\\", "/")
        if bundle is not None and source in artifact_owner:
            bundle_sources.setdefault((artifact_owner[source], bundle[0]), []).append(source)
    for (package_key, bundle_id), sources in bundle_sources.items():
        ordered = sorted(sources)
        for left, right in zip(ordered, ordered[1:]):
            _lineage_edge(
                packages[package_key]["lineage"],
                "bundle_member",
                left,
                right,
                member_by_source,
                note=f"explicit analysis bundle {bundle_id}",
            )

    for run_path, record in run_record_by_path.items():
        source_artifact = str(record.get("source_artifact") or record.get("sql_path") or "").replace("\\", "/")
        slug = artifact_owner.get(source_artifact) or query_by_path.get(source_artifact)
        if slug:
            _lineage_edge(packages[slug]["lineage"], "evidence_for", run_path, source_artifact, member_by_source)

    archive_rows: list[dict[str, Any]] = []
    if include_archive and (root / "archive").is_dir():
        formal_hashes: dict[str, list[str]] = {}
        for entry in entries:
            formal_hashes.setdefault(str(entry["source_sha256"]), []).append(str(entry["target_path"]))
        for path in sorted((root / "archive").rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            sha256 = _sha256_file(path)
            duplicates = formal_hashes.get(sha256, [])
            archive_rows.append({
                "source_path": relative,
                "source_sha256": sha256,
                "size_bytes": path.stat().st_size,
                "action": "remove_duplicate" if duplicates else "unresolved",
                "target_path": duplicates[0] if len(duplicates) == 1 else "",
                "candidate_targets": duplicates,
                "reason": "byte_identical_formal_member" if duplicates else "no_deterministic_owner",
            })

    formal_unresolved = sorted(
        {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in unresolved}.values(),
        key=lambda item: item["source_path"],
    )
    archive_unresolved = [item for item in archive_rows if item["action"] == "unresolved"]
    package_rows = []
    for slug in sorted(packages):
        package = packages[slug]
        package["members"] = sorted(package["members"], key=lambda item: item["target_path"])
        package["lineage"] = sorted(
            package["lineage"],
            key=lambda item: (item["from_member_id"], item["relation"], item["to_member_id"]),
        )
        package_rows.append(package)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "project_id": root.name,
        "source_manifest": "manifest.json",
        "source_manifest_sha256": _sha256_file(manifest_path),
        "formal_status": "ready" if not formal_unresolved else "blocked",
        "archive_status": "ready" if not archive_unresolved else "needs_decision",
        "packages": package_rows,
        "files": entries,
        "formal_unresolved": formal_unresolved,
        "archive": archive_rows,
        "summary": {
            "package_count": len(package_rows),
            "formal_file_count": len(entries),
            "formal_unresolved_count": len(formal_unresolved),
            "archive_file_count": len(archive_rows),
            "archive_unresolved_count": len(archive_unresolved),
        },
    }


def render_report(plan: Mapping[str, Any]) -> str:
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    lines = [
        f"# {plan.get('project_id')} Formal Asset Migration",
        "",
        f"- Formal status: `{plan.get('formal_status')}`",
        f"- Archive status: `{plan.get('archive_status')}`",
        f"- Packages: {summary.get('package_count', 0)}",
        f"- Formal files: {summary.get('formal_file_count', 0)}",
        f"- Formal unresolved: {summary.get('formal_unresolved_count', 0)}",
        f"- Archive files: {summary.get('archive_file_count', 0)}",
        f"- Archive unresolved: {summary.get('archive_unresolved_count', 0)}",
        "",
        "## Packages",
        "",
    ]
    for package in plan.get("packages", []):
        lines.append(
            f"- `{package.get('package_id')}` {package.get('title')} "
            f"({len(package.get('members', []))} members)"
        )
    lines.extend(["", "## Blockers", ""])
    blockers = [*plan.get("formal_unresolved", []), *[row for row in plan.get("archive", []) if row.get("action") == "unresolved"]]
    if blockers:
        for item in blockers:
            lines.append(f"- `{item.get('source_path')}`: {item.get('reason')}")
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def _verify_source_plan(root: Path, plan: Mapping[str, Any]) -> None:
    manifest_path = root / str(plan.get("source_manifest") or "manifest.json")
    if not manifest_path.is_file() or _sha256_file(manifest_path) != plan.get("source_manifest_sha256"):
        raise MigrationError("Legacy project manifest changed after dry-run; build a new plan")
    for entry in plan.get("files", []):
        source = root / Path(_safe_relative(str(entry.get("source_path") or ""), label="source path"))
        if (
            not source.is_file()
            or source.stat().st_size != int(entry.get("size_bytes") or -1)
            or _sha256_file(source) != entry.get("source_sha256")
        ):
            raise MigrationError(f"Migration source changed after dry-run: {entry.get('source_path')}")


def _compact_manifest(root: Path, legacy: Mapping[str, Any]) -> dict[str, Any]:
    formal_index = _read_json(root / FORMAL_ROOT / "index.json")
    preserved = {
        key: value
        for key, value in legacy.items()
        if key
        not in {
            "artifacts",
            "artifact_counters",
            "run_evidence",
            "query_workspace_index",
            "query_workspace_view",
        }
    }
    preserved.update(
        {
            "schema_version": PROJECT_MANIFEST_SCHEMA_VERSION,
            "formal_asset_repository": {
                "index": f"{FORMAL_ROOT}/index.json",
                "migration_map": MIGRATION_MAP_REL,
                "package_count": len(formal_index.get("packages", [])),
            },
            "packages": formal_index.get("packages", []),
        }
    )
    return preserved


def _migration_map(plan: Mapping[str, Any], receipts: list[Mapping[str, Any]]) -> dict[str, Any]:
    receipt_by_package = {str(item.get("package_id") or ""): item for item in receipts}
    paths = []
    for entry in plan.get("files", []):
        package_id = str(entry.get("package_id") or "")
        receipt = receipt_by_package.get(package_id, {})
        paths.append(
            {
                "old_path": entry.get("source_path"),
                "old_sha256": entry.get("source_sha256"),
                "action": "migrated",
                "package_id": package_id,
                "new_member_path": next(
                    (
                        item.get("path")
                        for item in receipt.get("files", [])
                        if str(item.get("path") or "").endswith(f"/members/{entry.get('target_path')}")
                    ),
                    "",
                ),
                "reason": entry.get("reason"),
            }
        )
    return {
        "schema_version": MAP_SCHEMA_VERSION,
        "project_id": plan.get("project_id"),
        "source_manifest_sha256": plan.get("source_manifest_sha256"),
        "paths": sorted(paths, key=lambda item: str(item.get("old_path") or "")),
        "legacy_quarantine": plan.get("archive", []),
    }


def apply_formal_migration(project_root: str | Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(project_root).resolve()
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise MigrationError("Unsupported migration plan schema")
    if plan.get("project_id") != root.name:
        raise MigrationError("Migration plan project does not match --root")
    if plan.get("formal_status") != "ready":
        raise MigrationError("Formal migration plan has unresolved files")
    _verify_source_plan(root, plan)
    backup = root / BACKUP_DIR
    if backup.exists():
        raise MigrationError(f"Migration backup already exists: {backup}")
    backup.mkdir(parents=True)
    legacy_manifest = _read_json(root / "manifest.json")
    formal_root = root / FORMAL_ROOT
    if formal_root.exists():
        shutil.copytree(formal_root, backup / FORMAL_ROOT)
    shutil.copyfile(root / "manifest.json", backup / "manifest.json")
    receipts: list[dict[str, Any]] = []
    moved_dirs: list[str] = []
    try:
        for package in plan.get("packages", []):
            members = []
            for item in package.get("members", []):
                source = root / Path(str(item["source_path"]))
                if _sha256_file(source) != item.get("source_sha256"):
                    raise MigrationError(f"Package member drifted: {item.get('source_path')}")
                members.append(
                    {
                        "source_path": source,
                        "target_path": item["target_path"],
                        "member_id": item["member_id"],
                        "role": item["role"],
                        "lifecycle_state": item["lifecycle_state"],
                    }
                )
            repository_plan = plan_package(
                root,
                title=str(package.get("title") or package.get("slug") or "Formal Asset"),
                members=members,
                package_id=str(package.get("package_id") or ""),
                slug=str(package.get("slug") or ""),
                lineage=package.get("lineage", []),
                lifecycle_state=str(package.get("lifecycle_state") or "current"),
                metadata=package.get("metadata")
                or ({"analysis_bundle": package["analysis_bundle"]} if package.get("analysis_bundle") else None),
            )
            receipt = apply_repository_plan(repository_plan)
            validation = validate_receipt(root, receipt)
            if validation.get("status") != "valid":
                raise MigrationError(f"Package receipt validation failed: {package.get('package_id')}")
            receipts.append(receipt)
        if not receipts and not (formal_root / "index.json").is_file():
            _write_json_atomic(
                formal_root / "index.json",
                {
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "project_id": str(plan.get("project_id") or root.name),
                    "updated_at": "",
                    "packages": [],
                },
            )
        migration_map = _migration_map(plan, receipts)
        _write_json_atomic(root / MIGRATION_MAP_REL, migration_map)
        for directory in LEGACY_FORMAL_DIRS:
            source = root / directory
            if source.exists():
                source.replace(backup / directory)
                moved_dirs.append(directory)
        _write_json_atomic(root / "manifest.json", _compact_manifest(root, legacy_manifest))
        (root / MIGRATION_REPORT_REL).write_text(render_report(plan), encoding="utf-8")
        shutil.rmtree(backup)
    except Exception:
        for directory in reversed(moved_dirs):
            source = backup / directory
            if source.exists() and not (root / directory).exists():
                source.replace(root / directory)
        if (backup / "manifest.json").is_file():
            shutil.copyfile(backup / "manifest.json", root / "manifest.json")
        if formal_root.exists():
            shutil.rmtree(formal_root)
        if (backup / FORMAL_ROOT).is_dir():
            shutil.copytree(backup / FORMAL_ROOT, formal_root)
        if backup.exists():
            shutil.rmtree(backup)
        raise
    return {
        "schema_version": "formal_asset_migration_receipt_v1",
        "status": "migrated",
        "project_id": root.name,
        "package_receipts": receipts,
        "migration_map": MIGRATION_MAP_REL,
        "archive_status": plan.get("archive_status"),
    }


def load_decisions(path: str | Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(Path(path))
    if payload.get("schema_version") != DECISIONS_SCHEMA_VERSION:
        raise MigrationError("Unsupported Legacy Quarantine decisions schema")
    result: dict[str, dict[str, Any]] = {}
    for item in payload.get("decisions", []):
        if not isinstance(item, dict):
            raise MigrationError("Legacy Quarantine decisions must be objects")
        source = _safe_relative(str(item.get("source_path") or ""), label="archive source path")
        if source in result:
            raise MigrationError(f"Duplicate Legacy Quarantine decision: {source}")
        result[source] = dict(item)
    return result


def _resolve_migrated_target(
    root: Path,
    migration_map: Mapping[str, Any],
    target_value: str,
) -> tuple[str, Path]:
    relative = _safe_relative(target_value, label="quarantine target")
    if relative.startswith(f"{FORMAL_ROOT}/"):
        return relative, root / Path(relative)
    candidates = [
        str(item.get("new_member_path") or "")
        for item in migration_map.get("paths", [])
        if str(item.get("new_member_path") or "").endswith(f"/members/{relative}")
    ]
    if len(candidates) != 1:
        raise MigrationError(
            f"Quarantine target does not resolve to one migrated member: {target_value}"
        )
    return candidates[0], root / Path(candidates[0])


def _migrate_keep_local_sql(
    root: Path,
    source: Path,
    decision: Mapping[str, Any],
) -> str:
    from migrate_legacy_sql_work import derive_title_and_purpose
    from promotion_ledger import build_content_snapshot, record_decision
    from sql_query_workspace import (
        _query_facts,
        finalize_legacy_source_intake,
        find_query_reference,
        normalize_sql_text,
        save_query,
        sql_fingerprint,
    )

    sql = normalize_sql_text(source.read_text(encoding="utf-8-sig"))
    facts = _query_facts(root, source, sql, ["legacy_quarantine", "migrated"])
    title, purpose = derive_title_and_purpose(source, sql, facts)
    fingerprint = sql_fingerprint(sql)
    try:
        saved = save_query(
            root=root,
            source_sql=source,
            title=title,
            purpose=purpose,
            business_question=purpose,
            status="archived",
            source_kind="legacy_work_migration",
            tags=["legacy_quarantine", "migrated", "keep_local"],
            revision_note="Moved from read-only Legacy Quarantine by an explicit keep_local decision.",
            gate={
                "status": "not_run",
                "blockers": [],
                "warnings": ["Historical Legacy Quarantine migration; generation gate was not replayed."],
            },
            rule_context=None,
            gate_mode="legacy_work_migration",
            facts=facts,
            write_seed=False,
            knowledge_usage_declaration="legacy-unknown",
            source_intake={
                "contract_version": "legacy_work_import_v1",
                "source_kind": "legacy_quarantine",
                "original_file_name": source.name,
                "source_sha256": _sha256_file(source),
                "source_sql_fingerprint": fingerprint,
                "legacy_source_path": source.relative_to(root).as_posix(),
                "source_removed_after_verified_copy": False,
                "external_input_immutable": True,
                "absolute_source_path_persisted": False,
            },
        )
    except ValueError as exc:
        raise MigrationError(f"keep_local SQL could not be indexed safely: {source}") from exc
    reference = find_query_reference(
        root,
        root / str(saved.get("path") or ""),
        match_fingerprint=False,
    )
    if not reference:
        raise MigrationError(f"keep_local SQL did not become an indexed Workspace Query: {source}")
    target = root / str(reference.get("path") or "")
    if sql_fingerprint(target.read_text(encoding="utf-8-sig")) != fingerprint:
        raise MigrationError(f"keep_local SQL fingerprint verification failed: {source}")
    finalize_legacy_source_intake(root, reference)
    snapshot = build_content_snapshot(root, reference)
    record_decision(
        root,
        snapshot,
        decision="deferred",
        reason=str(decision.get("reason") or "Legacy Quarantine item remains local"),
        user_request=str(decision.get("user_request") or "全部按照推荐"),
        confirmed_by_user=True,
        confirmed_by=str(decision.get("confirmed_by") or "user"),
        project_id=root.name,
        missing_conditions=["legacy formalization assessment is still pending"],
        revisit_when="resurface when the closeout planner can assign a target Package",
    )
    return target.relative_to(root).as_posix()


def _publish_quarantine_package_members(
    root: Path,
    rows: list[tuple[str, Path, Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, str]:
    grouped: dict[str, list[tuple[str, Path, Mapping[str, Any], Mapping[str, Any]]]] = {}
    for row in rows:
        package_id = str(row[2].get("package_id") or "")
        if not package_id:
            raise MigrationError(f"move_to_package requires package_id: {row[0]}")
        grouped.setdefault(package_id, []).append(row)
    published: dict[str, str] = {}
    for package_id, items in sorted(grouped.items()):
        package = load_package(root, package_id)
        members = []
        lineage = []
        current = package.get("current") if isinstance(package.get("current"), dict) else {}
        by_role = current.get("by_role") if isinstance(current.get("by_role"), dict) else {}
        query_member_ids = [str(item) for item in by_role.get("formal_query_sql", []) if str(item)]
        if not query_member_ids:
            query_member_ids = [
                str(item.get("member_id") or "")
                for item in package.get("members", [])
                if isinstance(item, dict) and item.get("role") == "formal_query_sql"
            ]
        lineage_target = query_member_ids[-1] if query_member_ids else ""
        for source_path, source, decision, inventory in items:
            target_path = _safe_relative(
                str(decision.get("target_path") or ""),
                label="quarantine package member target",
            )
            member_id = _member_id(target_path)
            members.append(
                {
                    "source_path": source,
                    "target_path": target_path,
                    "member_id": member_id,
                    "role": str(decision.get("member_role") or "legacy_quarantine_evidence"),
                    "lifecycle_state": str(decision.get("lifecycle_state") or "archived"),
                }
            )
            if lineage_target:
                lineage.append(
                    {
                        "relation": "historical_evidence_for",
                        "from_member_id": member_id,
                        "to_member_id": lineage_target,
                    }
                )
        repository_plan = plan_package(
            root,
            title=str(package.get("title") or package.get("slug") or package_id),
            members=members,
            package_id=package_id,
            slug=str(package.get("slug") or ""),
            lineage=lineage,
            lifecycle_state=str(package.get("lifecycle_state") or "current"),
        )
        receipt = apply_repository_plan(repository_plan)
        validation = validate_receipt(root, receipt)
        if validation.get("status") != "valid":
            raise MigrationError(f"Legacy Quarantine Package receipt is invalid: {package_id}")
        directory = str(load_package(root, package_id).get("directory") or "")
        for source_path, _source, decision, _inventory in items:
            target_path = _safe_relative(
                str(decision.get("target_path") or ""),
                label="quarantine package member target",
            )
            published[source_path] = f"{directory}/members/{target_path}"
    return published


def resolve_quarantine(
    project_root: str | Path,
    plan: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    archive_rows = {str(item.get("source_path") or ""): item for item in plan.get("archive", [])}
    missing = sorted(set(archive_rows) - set(decisions))
    extra = sorted(set(decisions) - set(archive_rows))
    if missing or extra:
        raise MigrationError(f"Legacy Quarantine decisions do not match inventory; missing={missing}, extra={extra}")
    migration_map_path = root / MIGRATION_MAP_REL
    migration_map = _read_json(migration_map_path)
    package_rows: list[tuple[str, Path, Mapping[str, Any], Mapping[str, Any]]] = []
    for source_path in sorted(archive_rows):
        decision = decisions[source_path]
        if decision.get("action") != "move_to_package":
            continue
        source = root / Path(source_path)
        inventory = archive_rows[source_path]
        if not source.is_file() or _sha256_file(source) != inventory.get("source_sha256"):
            raise MigrationError(f"Legacy Quarantine source drifted: {source_path}")
        if decision.get("source_sha256") != inventory.get("source_sha256"):
            raise MigrationError(f"Legacy Quarantine decision hash mismatch: {source_path}")
        package_rows.append((source_path, source, decision, inventory))
    published_package_paths = _publish_quarantine_package_members(root, package_rows)
    applied: list[dict[str, Any]] = []
    for source_path in sorted(archive_rows):
        source = root / Path(source_path)
        inventory = archive_rows[source_path]
        decision = decisions[source_path]
        if not source.is_file() or _sha256_file(source) != inventory.get("source_sha256"):
            raise MigrationError(f"Legacy Quarantine source drifted: {source_path}")
        if decision.get("source_sha256") != inventory.get("source_sha256"):
            raise MigrationError(f"Legacy Quarantine decision hash mismatch: {source_path}")
        action = str(decision.get("action") or "")
        target_value = str(decision.get("target_path") or "")
        resolved_target_value = target_value
        target: Path | None = None
        if action == "remove_duplicate":
            resolved_target_value, target = _resolve_migrated_target(root, migration_map, target_value)
            if target is None or not target.is_file() or _sha256_file(target) != inventory.get("source_sha256"):
                raise MigrationError(f"Duplicate target is missing or differs: {source_path} -> {target_value}")
            source.unlink()
        elif action == "move_to_source":
            target = root / Path(_safe_relative(target_value, label="quarantine target")) if target_value else None
            if target is None:
                raise MigrationError(f"{action} requires target_path: {source_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file() or _sha256_file(target) != inventory.get("source_sha256"):
                    raise MigrationError(f"Quarantine target conflicts: {target_value}")
                source.unlink()
            else:
                source.replace(target)
        elif action == "keep_local":
            if source.suffix.lower() != ".sql":
                raise MigrationError(f"keep_local currently requires a SQL file: {source_path}")
            resolved_target_value = _migrate_keep_local_sql(root, source, decision)
            source.unlink()
        elif action == "move_to_package":
            resolved_target_value = published_package_paths[source_path]
            target = root / Path(resolved_target_value)
            if not target.is_file() or _sha256_file(target) != inventory.get("source_sha256"):
                raise MigrationError(f"Published Package member is missing or differs: {source_path}")
            source.unlink()
        else:
            raise MigrationError(f"Unsupported or unresolved quarantine action for {source_path}: {action}")
        applied.append(
            {
                "old_path": source_path,
                "old_sha256": inventory.get("source_sha256"),
                "action": action,
                "new_path": resolved_target_value,
                "reason": decision.get("reason") or "user_confirmed_quarantine_decision",
            }
        )
    archive = root / "archive"
    if archive.exists():
        leftovers = [item for item in archive.rglob("*") if item.is_file()]
        if leftovers:
            raise MigrationError(f"Legacy Quarantine still contains files: {leftovers[:3]}")
        shutil.rmtree(archive)
    migration_map["legacy_quarantine"] = applied
    _write_json_atomic(migration_map_path, migration_map)
    return {
        "schema_version": "legacy_quarantine_resolution_receipt_v1",
        "status": "resolved",
        "project_id": root.name,
        "files": applied,
        "archive_removed": True,
    }


def verify_migration(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    problems: list[str] = []
    for directory in LEGACY_ALL_DIRS:
        if (root / directory).exists():
            problems.append(f"legacy directory remains: {directory}")
    manifest = _read_json(root / "manifest.json")
    if manifest.get("schema_version") != PROJECT_MANIFEST_SCHEMA_VERSION:
        problems.append("project manifest is not compact project_manifest_v2")
    migration_map = _read_json(root / MIGRATION_MAP_REL)
    for package in list_packages(root):
        package_id = str(package.get("package_id") or "")
        latest = str(package.get("latest_receipt") or "")
        if not latest:
            problems.append(f"package has no latest receipt: {package_id}")
            continue
        validation = validate_receipt(root, latest)
        if validation.get("status") != "valid":
            problems.append(f"invalid package receipt: {package_id}")
    if not migration_map.get("paths") and manifest.get("packages"):
        problems.append("migration map has no legacy path entries")
    return {
        "schema_version": "formal_asset_migration_verification_v1",
        "status": "valid" if not problems else "invalid",
        "project_id": root.name,
        "package_count": len(list_packages(root)),
        "problems": problems,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--root", required=True)
    plan_parser.add_argument("--without-archive", action="store_true")
    plan_parser.add_argument("--output")
    plan_parser.add_argument("--report")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--root", required=True)
    apply_parser.add_argument("--plan", required=True)
    resolve_parser = subparsers.add_parser("resolve-quarantine")
    resolve_parser.add_argument("--root", required=True)
    resolve_parser.add_argument("--plan", required=True)
    resolve_parser.add_argument("--decisions", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "plan":
            plan = build_plan(args.root, include_archive=not args.without_archive)
            if args.output:
                _write_json_atomic(Path(args.output), plan)
            if args.report:
                report = Path(args.report)
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(render_report(plan), encoding="utf-8")
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0 if plan["formal_status"] == "ready" else 2
        if args.command == "apply":
            result = apply_formal_migration(args.root, _read_json(Path(args.plan)))
        elif args.command == "resolve-quarantine":
            result = resolve_quarantine(
                args.root,
                _read_json(Path(args.plan)),
                load_decisions(args.decisions),
            )
        else:
            result = verify_migration(args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"migrated", "resolved", "valid"} else 2
    except (MigrationError, FormalAssetRepositoryError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
