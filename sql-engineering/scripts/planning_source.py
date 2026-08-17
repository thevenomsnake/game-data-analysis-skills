#!/usr/bin/env python3
"""Manage exact SVN-revision or embedded-folder planning sources for SQL projects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from asset_provenance import build_generation_provenance, generated_by_ldap
from capability_registry import command_function_ids
from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
import planning_source_provider as source_provider


RELEASE_SCHEMA = "planning_source_release_v2"
LEGACY_RELEASE_SCHEMA = "planning_source_release_v1"
RELEASE_SCHEMAS = {LEGACY_RELEASE_SCHEMA, RELEASE_SCHEMA}
FILES_SCHEMA = "planning_source_files_v1"
BINDING_SCHEMA = "planning_source_binding_v2"
LEGACY_BINDING_SCHEMA = "planning_source_binding_v1"
BINDING_SCHEMAS = {LEGACY_BINDING_SCHEMA, BINDING_SCHEMA}
REGISTRY_SCHEMA = "planning_source_registry_v1"
LOCAL_SCHEMA = "planning_source_local_config_v3"
LEGACY_LOCAL_SCHEMAS = {
    "planning_source_local_config_v1",
    "planning_source_local_config_v2",
}
LOCAL_SCHEMAS = {*LEGACY_LOCAL_SCHEMAS, LOCAL_SCHEMA}
MANAGEMENT_MODES = {"user_managed", "tool_managed"}
CACHE_SCHEMA = "planning_source_scan_cache_v1"
RELEASE_ID_RE = re.compile(r"^PSR-([A-Z0-9][A-Z0-9_-]{0,31})-([A-Z0-9][A-Z0-9_-]{0,31})-(\d{4})$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
SKIP_DIRS = {".git", ".svn", ".trae", "__pycache__", "$recycle.bin", "system volume information"}
SKIP_FILES = {".ds_store", "thumbs.db"}
SKIP_SUFFIXES = {".tmp", ".bak.tmp", ".db-journal"}
PREVIEW_LIMIT = 40


class PlanningSourceError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if default is not None:
            return default
        raise PlanningSourceError(f"JSON file does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PlanningSourceError(f"invalid JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise PlanningSourceError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_sha256(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def request_sha256(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def validate_id(value: str, label: str) -> str:
    normalized = value.strip().upper()
    if not IDENTIFIER_RE.fullmatch(normalized):
        raise PlanningSourceError(f"{label} must contain only letters, digits, _ or -")
    return normalized


def resolve_repo(value: Path) -> Path:
    root = value.resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise PlanningSourceError(f"repository root is invalid: {root}")
    return root


def project_context(repo: Path, project: str) -> tuple[str, Path]:
    project_id = project.strip()
    if not IDENTIFIER_RE.fullmatch(project_id):
        raise PlanningSourceError("project must contain only letters, digits, _ or -")
    root = (repo / "sql-projects" / project_id).resolve()
    expected_parent = (repo / "sql-projects").resolve()
    if root.parent != expected_parent or not (root / "project_config.json").is_file():
        raise PlanningSourceError(f"project is not configured: {project_id}")
    config = read_json(root / "project_config.json")
    configured_id = str(config.get("project_id") or "")
    if configured_id and configured_id != project_id:
        raise PlanningSourceError(f"project_config project_id mismatch: {configured_id} != {project_id}")
    return project_id, root


def local_config_path(repo: Path, project: str) -> Path:
    return repo / ".local" / "planning-sources" / f"{project}.json"


def scan_cache_path(repo: Path, project: str) -> Path:
    return repo / ".local" / "planning-source-cache" / f"{project}.json"


def inbox_path(repo: Path, product: str, stage: str) -> Path:
    return repo / ".local" / "planning-source-inbox" / product / stage


def binding_path(project_root: Path) -> Path:
    return project_root / "planning" / "source_binding.json"


def registry_path(repo: Path) -> Path:
    return repo / "planning-sources" / "registry.json"


def release_root(repo: Path, product: str, stage: str) -> Path:
    return repo / "planning-sources" / product / "stages" / stage / "releases"


def load_local_config(repo: Path, project: str) -> dict[str, Any]:
    payload = read_json(local_config_path(repo, project))
    if payload.get("contract_version") not in LOCAL_SCHEMAS or payload.get("project_id") != project:
        raise PlanningSourceError(f"invalid local planning-source config for {project}")
    return payload


def configured_management_mode(local: dict[str, Any]) -> str:
    if local.get("contract_version") != LOCAL_SCHEMA:
        raise PlanningSourceError(
            "planning-source management mode is not configured; run configure explicitly"
        )
    value = str(local.get("management_mode") or "")
    if value not in MANAGEMENT_MODES:
        raise PlanningSourceError("planning-source management mode is invalid")
    return value


def configured_svn_auth(local: dict[str, Any]) -> dict[str, str] | None:
    credential_ref = (
        local.get("credential_ref")
        if isinstance(local.get("credential_ref"), dict)
        else None
    )
    try:
        return source_provider.resolve_svn_auth(credential_ref)
    except source_provider.PlanningSourceProviderError as error:
        raise PlanningSourceError(str(error)) from error


def svn_source_blockers(local: dict[str, Any], identity: dict[str, Any]) -> list[str]:
    if configured_management_mode(local) != "user_managed":
        return []
    blockers = []
    status = identity.get("working_copy_status")
    if not isinstance(status, dict) or not status.get("clean"):
        blockers.append(
            "The user-managed SVN working copy must be clean before its revision can be sealed."
        )
    revision_state = identity.get("working_copy_revision_state")
    if not isinstance(revision_state, dict) or not revision_state.get("exact"):
        blockers.append(
            "The user-managed SVN working copy must be at one exact revision; mixed, switched, modified, or partial states are not sealable."
        )
    return blockers


def load_binding(project_root: Path) -> dict[str, Any] | None:
    path = binding_path(project_root)
    if not path.exists():
        return None
    payload = read_json(path)
    if payload.get("contract_version") not in BINDING_SCHEMAS:
        raise PlanningSourceError(f"unsupported planning-source binding: {path}")
    return payload


def resolve_release(
    repo: Path,
    project: str,
    project_root: Path,
    binding: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if binding.get("project_id") != project:
        raise PlanningSourceError("planning-source binding project_id does not match the project")
    release_reference = Path(str(binding.get("release_manifest") or ""))
    if release_reference.is_absolute():
        raise PlanningSourceError("planning-source binding release_manifest must be relative")
    manifest_path = (project_root / release_reference).resolve()
    try:
        manifest_path.relative_to((repo / "planning-sources").resolve())
    except ValueError as error:
        raise PlanningSourceError("planning-source binding points outside planning-sources") from error
    manifest = read_json(manifest_path)
    if manifest.get("contract_version") not in RELEASE_SCHEMAS:
        raise PlanningSourceError(f"unsupported planning-source release: {manifest_path}")
    expected = {
        "release_id": binding.get("active_release_id"),
        "product_id": binding.get("product_id"),
        "stage_id": binding.get("stage_id"),
        "tree_sha256": binding.get("tree_sha256"),
    }
    mismatched = [field for field, value in expected.items() if manifest.get(field) != value]
    if mismatched:
        raise PlanningSourceError(
            "planning-source binding differs from its release: " + ", ".join(mismatched)
        )
    canonical_reference = Path(str(manifest.get("release_manifest") or ""))
    if canonical_reference.is_absolute() or (repo / canonical_reference).resolve() != manifest_path:
        raise PlanningSourceError("planning-source release_manifest does not identify itself")
    return manifest_path, manifest


def safe_relative(value: str) -> str:
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PlanningSourceError(f"source path escapes its root: {value}")
    return relative.as_posix()


def should_skip_file(name: str) -> bool:
    lowered = name.lower()
    return (
        name.startswith(("~$", ".~"))
        or lowered in SKIP_FILES
        or any(lowered.endswith(suffix) for suffix in SKIP_SUFFIXES)
    )


def source_files(source_root: Path):
    for directory, subdirs, files in os.walk(source_root, followlinks=False):
        current = Path(directory)
        subdirs[:] = [
            name
            for name in sorted(subdirs)
            if name.lower() not in SKIP_DIRS and not (current / name).is_symlink()
        ]
        for name in sorted(files):
            path = current / name
            if should_skip_file(name) or path.is_symlink():
                continue
            yield path


def scan_source(repo: Path, project: str, source_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    if not source_root.is_dir() or source_root.parent == source_root:
        raise PlanningSourceError(f"planning source folder is unavailable: {source_root}")
    try:
        source_root.relative_to(repo / "planning-sources")
    except ValueError:
        pass
    else:
        raise PlanningSourceError("the local source folder cannot be inside formal planning-sources")

    cache_path = scan_cache_path(repo, project)
    cached = read_json(
        cache_path,
        {"contract_version": CACHE_SCHEMA, "project_id": project, "source_path": "", "files": {}},
    )
    old_rows = cached.get("files") if cached.get("source_path") == str(source_root) else {}
    if not isinstance(old_rows, dict):
        old_rows = {}
    cache_rows: dict[str, dict[str, Any]] = {}
    formal_rows: list[dict[str, Any]] = []
    hashed_files = 0
    for path in source_files(source_root):
        stat = path.stat()
        relative = safe_relative(path.relative_to(source_root).as_posix())
        old = old_rows.get(relative) if isinstance(old_rows.get(relative), dict) else {}
        if old.get("size_bytes") == stat.st_size and old.get("mtime_ns") == stat.st_mtime_ns and old.get("sha256"):
            digest = str(old["sha256"])
        else:
            digest = file_sha256(path)
            hashed_files += 1
        cache_rows[relative] = {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest,
        }
        formal_rows.append({"relative_path": relative, "size_bytes": stat.st_size, "sha256": digest})
    formal_rows.sort(key=lambda item: item["relative_path"].casefold())
    tree_hash = json_sha256(
        [{"path": row["relative_path"], "size": row["size_bytes"], "sha256": row["sha256"]} for row in formal_rows]
    )
    write_json(
        cache_path,
        {
            "contract_version": CACHE_SCHEMA,
            "project_id": project,
            "source_path": str(source_root),
            "scanned_at": now_iso(),
            "tree_sha256": tree_hash,
            "files": cache_rows,
        },
    )
    return {
        "source_root": source_root,
        "files": formal_rows,
        "file_count": len(formal_rows),
        "total_bytes": sum(int(row["size_bytes"]) for row in formal_rows),
        "tree_sha256": tree_hash,
        "hashed_file_count": hashed_files,
    }


def release_files(
    repo: Path,
    project: str,
    project_root: Path,
    binding: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not binding:
        return []
    manifest_path, manifest = resolve_release(repo, project, project_root, binding)
    files_path = (manifest_path.parent / str(manifest.get("files_manifest") or "files.json")).resolve()
    try:
        files_path.relative_to(manifest_path.parent)
    except ValueError as error:
        raise PlanningSourceError("planning-source files manifest escapes its release") from error
    payload = read_json(files_path)
    rows = payload.get("files")
    if payload.get("contract_version") != FILES_SCHEMA or not isinstance(rows, list):
        raise PlanningSourceError(f"invalid planning-source files manifest: {files_path}")
    if payload.get("release_id") != manifest.get("release_id"):
        raise PlanningSourceError("planning-source files manifest release_id mismatch")
    if payload.get("tree_sha256") != manifest.get("tree_sha256"):
        raise PlanningSourceError("planning-source files manifest tree hash mismatch")
    return [row for row in rows if isinstance(row, dict)]


def file_diff(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    old = {str(row.get("relative_path")): row for row in previous}
    new = {str(row.get("relative_path")): row for row in current}
    added = sorted(set(new) - set(old), key=str.casefold)
    removed = sorted(set(old) - set(new), key=str.casefold)
    changed = sorted(
        (path for path in set(old) & set(new) if old[path].get("sha256") != new[path].get("sha256")),
        key=str.casefold,
    )
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "counts": {"added": len(added), "changed": len(changed), "removed": len(removed)},
    }


def release_source_kind(release: dict[str, Any] | None) -> str:
    if not release:
        return ""
    if release.get("contract_version") == LEGACY_RELEASE_SCHEMA:
        return "folder_snapshot"
    return str(release.get("source_kind") or "")


def configured_provider(local: dict[str, Any]) -> str:
    explicit = str(local.get("provider") or "").strip().lower()
    if explicit:
        if explicit not in source_provider.SUPPORTED_PROVIDERS:
            raise PlanningSourceError(f"unsupported planning-source provider: {explicit}")
        return explicit
    source_path = Path(str(local.get("source_path") or ""))
    if source_path.is_dir() and (source_path / ".svn").is_dir():
        return source_provider.SVN_PROVIDER
    return source_provider.FOLDER_PROVIDER


def inspect_configured_svn(local: dict[str, Any]) -> dict[str, Any]:
    try:
        return source_provider.inspect_configured_svn(local)
    except source_provider.PlanningSourceProviderError as error:
        raise PlanningSourceError(str(error)) from error


def persisted_svn_identity(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": source_provider.SVN_SOURCE_SCHEMA,
        "provider": source_provider.SVN_PROVIDER,
        "repository_uuid": str(identity["repository_uuid"]),
        "repository_root": str(identity["repository_root"]),
        "source_url": str(identity["source_url"]),
        "revision": int(identity["revision"]),
        "repository_revision": int(identity["repository_revision"]),
        "revision_selection": str(identity.get("revision_selection") or "remote_latest"),
        "last_changed_author": str(identity.get("last_changed_author") or ""),
        "last_changed_at": str(identity.get("last_changed_at") or ""),
        "externals_policy": "forbidden",
        "materialization": "on_demand_verified",
    }


def same_svn_revision(release: dict[str, Any] | None, identity: dict[str, Any]) -> bool:
    if release_source_kind(release) != "svn_revision":
        return False
    control = release.get("source_control") if isinstance(release.get("source_control"), dict) else {}
    return (
        control.get("contract_version") in source_provider.SVN_SOURCE_SCHEMAS
        and control.get("repository_uuid") == identity.get("repository_uuid")
        and control.get("source_url") == identity.get("source_url")
        and int(control.get("revision") or 0) == int(identity.get("revision") or 0)
    )


def active_release(
    repo: Path,
    project: str,
    project_root: Path,
    binding: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not binding:
        return None
    return resolve_release(repo, project, project_root, binding)[1]


def projection_impacts(repo: Path, project: str, diff: dict[str, Any]) -> list[dict[str, Any]]:
    changed_paths = {
        path: change
        for change in ("added", "changed", "removed")
        for path in diff.get(change, [])
    }
    impacts = []
    specs_root = repo / "knowledge-base" / "projection_specs"
    if not specs_root.exists():
        return impacts
    for spec_path in sorted(specs_root.glob("*.export.json")):
        try:
            spec = read_json(spec_path)
        except PlanningSourceError:
            continue
        if (
            spec.get("schema_version") != "planning_projection_spec_v1"
            or str(spec.get("status") or "").lower() != "active"
        ):
            continue
        source = spec.get("source") if isinstance(spec.get("source"), dict) else {}
        if source.get("project_id") != project:
            continue
        relative_file = str(source.get("relative_file") or "")
        change_type = changed_paths.get(relative_file)
        if not change_type:
            continue
        dataset_id = str(spec.get("table_id") or spec_path.stem.replace(".export", ""))
        contract_path = repo / "knowledge-base" / "contracts" / f"{dataset_id}.json"
        contract = read_json(contract_path, {})
        governance = contract.get("governance") if isinstance(contract.get("governance"), dict) else {}
        auto_refresh = bool(governance.get("compatible_auto_refresh"))
        impacts.append(
            {
                "dataset_id": dataset_id,
                "relative_file": relative_file,
                "change_type": change_type,
                "projection_spec": spec_path.relative_to(repo).as_posix(),
                "compatible_auto_refresh": auto_refresh,
                "action": "refresh_compatible" if auto_refresh else "review_required",
            }
        )
    return impacts


def unresolved_projection_impacts(
    repo: Path,
    project: str,
    project_root: Path,
    binding: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not binding:
        return []
    release_path, release = resolve_release(repo, project, project_root, binding)
    diff_path = (release_path.parent / str(release.get("diff_manifest") or "diff.json")).resolve()
    try:
        diff_path.relative_to(release_path.parent)
    except ValueError as error:
        raise PlanningSourceError("planning-source diff manifest escapes its release") from error
    diff = read_json(diff_path)
    impacts = projection_impacts(repo, project, diff)
    if not impacts:
        return []

    knowledge_path = project_root / "knowledge" / "bindings.json"
    if not knowledge_path.exists():
        return []
    knowledge = read_json(knowledge_path)
    rows = knowledge.get("bindings")
    if knowledge.get("project_id") != project or not isinstance(rows, list):
        raise PlanningSourceError(f"invalid project Knowledge bindings: {knowledge_path}")
    active = {
        str(row.get("dataset_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("state") == "active" and row.get("dataset_id")
    }
    release_hashes = {
        str(row.get("relative_path")): str(row.get("sha256") or "")
        for row in release_files(repo, project, project_root, binding)
    }
    unresolved = []
    for impact in impacts:
        dataset_id = str(impact["dataset_id"])
        active_binding = active.get(dataset_id)
        if not active_binding:
            continue
        manifest_reference = Path(str(active_binding.get("dataset_manifest_path") or ""))
        if manifest_reference.is_absolute():
            raise PlanningSourceError(
                f"Knowledge binding manifest path must be relative: {dataset_id}"
            )
        dataset_manifest = (repo / manifest_reference).resolve()
        try:
            dataset_manifest.relative_to((repo / "knowledge-base").resolve())
        except ValueError as error:
            raise PlanningSourceError(
                f"Knowledge binding manifest escapes knowledge-base: {dataset_id}"
            ) from error
        manifest = read_json(dataset_manifest)
        adapter = manifest.get("adapter") if isinstance(manifest.get("adapter"), dict) else {}
        reference = (
            adapter.get("planning_source_reference")
            if isinstance(adapter.get("planning_source_reference"), dict)
            else {}
        )
        relative_file = str(impact["relative_file"])
        expected_hash = release_hashes.get(relative_file, "")
        aligned = (
            reference.get("contract_version") == "planning_source_reference_v1"
            and reference.get("project_id") == project
            and reference.get("release_id") == release.get("release_id")
            and reference.get("release_tree_sha256") == release.get("tree_sha256")
            and reference.get("relative_file") == relative_file
            and bool(expected_hash)
            and reference.get("file_sha256") == expected_hash
        )
        if aligned:
            continue
        unresolved.append(
            {
                **impact,
                "status": "refresh_required",
                "active_dataset_version": str(active_binding.get("dataset_version") or ""),
                "active_dataset_release_id": str(reference.get("release_id") or "legacy_unlabeled"),
                "required_release_id": str(release.get("release_id") or ""),
            }
        )
    return unresolved


def configure(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    project, project_root = project_context(repo, args.project)
    product = validate_id(args.product, "product")
    stage = validate_id(args.stage, "stage")
    management_mode = str(getattr(args, "management_mode", "") or "").strip()
    if management_mode not in MANAGEMENT_MODES:
        raise PlanningSourceError(
            "management mode must be user_managed or tool_managed"
        )
    provider_choice = str(getattr(args, "provider", "auto") or "auto").lower()
    svn_url = str(getattr(args, "svn_url", "") or "").strip()
    if provider_choice not in {"auto", *source_provider.SUPPORTED_PROVIDERS}:
        raise PlanningSourceError(f"unsupported planning-source provider: {provider_choice}")
    if svn_url and args.source_path:
        raise PlanningSourceError("use either --source-path or --svn-url, not both")
    if management_mode == "user_managed" and svn_url:
        raise PlanningSourceError("user_managed requires a local source path or managed inbox")
    if management_mode == "tool_managed" and args.source_path:
        raise PlanningSourceError("tool_managed requires a canonical SVN URL, not a local path")
    if management_mode == "tool_managed" and not svn_url:
        raise PlanningSourceError("tool_managed requires --svn-url")
    if management_mode == "tool_managed" and provider_choice == source_provider.FOLDER_PROVIDER:
        raise PlanningSourceError("tool_managed currently requires the SVN provider")
    existing_binding = load_binding(project_root)
    if existing_binding and (
        existing_binding.get("product_id") != product or existing_binding.get("stage_id") != stage
    ):
        raise PlanningSourceError("requested product/stage conflicts with the existing project binding")
    credential_ref: dict[str, Any] | None = None
    svn_username = str(getattr(args, "svn_username", "") or "").strip()
    credential_env = str(getattr(args, "credential_env", "") or "").strip()
    if svn_username or credential_env:
        if not svn_username or not credential_env:
            raise PlanningSourceError(
                "SVN credentials require both --svn-username and --credential-env"
            )
        credential_ref = {
            "contract_version": source_provider.SVN_CREDENTIAL_REF_SCHEMA,
            "kind": "environment_variable",
            "username": svn_username,
            "secret_env": credential_env,
        }
    if management_mode == "tool_managed" and credential_ref is None:
        raise PlanningSourceError(
            "tool_managed requires a non-secret SVN credential reference"
        )

    svn_identity: dict[str, Any] | None = None
    if management_mode == "user_managed" and args.source_path:
        source_path = args.source_path.resolve()
        if not source_path.is_dir():
            raise PlanningSourceError(f"planning source folder does not exist: {source_path}")
        if provider_choice in {"auto", source_provider.SVN_PROVIDER} and (source_path / ".svn").is_dir():
            try:
                svn_identity = source_provider.inspect_svn_working_copy(source_path)
            except source_provider.PlanningSourceProviderError as error:
                raise PlanningSourceError(str(error)) from error
        provider = source_provider.SVN_PROVIDER if svn_identity else source_provider.FOLDER_PROVIDER
        if provider_choice == source_provider.SVN_PROVIDER and provider != source_provider.SVN_PROVIDER:
            raise PlanningSourceError(f"path is not a usable SVN working copy: {source_path}")
        input_mode = "svn_working_copy" if provider == source_provider.SVN_PROVIDER else "linked_folder"
    elif management_mode == "tool_managed":
        try:
            svn_auth = source_provider.resolve_svn_auth(credential_ref)
        except source_provider.PlanningSourceProviderError as error:
            raise PlanningSourceError(str(error)) from error
        try:
            svn_identity = source_provider.inspect_svn_url(svn_url, auth=svn_auth)
        except source_provider.PlanningSourceProviderError as error:
            raise PlanningSourceError(str(error)) from error
        provider = source_provider.SVN_PROVIDER
        source_path = None
        input_mode = "svn_url"
    else:
        if provider_choice == source_provider.SVN_PROVIDER:
            raise PlanningSourceError("the SVN provider requires --source-path or --svn-url")
        source_path = inbox_path(repo, product, stage).resolve()
        source_path.mkdir(parents=True, exist_ok=True)
        provider = source_provider.FOLDER_PROVIDER
        input_mode = "managed_inbox"
    revision_policy = (
        "working_copy_pinned"
        if provider == source_provider.SVN_PROVIDER and management_mode == "user_managed"
        else "remote_latest"
        if provider == source_provider.SVN_PROVIDER
        else "folder_current"
    )
    configured_at = now_iso()
    existing_local = (
        read_json(local_config_path(repo, project))
        if local_config_path(repo, project).exists()
        else None
    )
    payload = {
        "contract_version": LOCAL_SCHEMA,
        "project_id": project,
        "product_id": product,
        "stage_id": stage,
        "management_mode": management_mode,
        "revision_policy": revision_policy,
        "provider": provider,
        "input_mode": input_mode,
        "source_path": str(source_path) if source_path else "",
        "configured_at": str((existing_local or {}).get("configured_at") or configured_at),
        "updated_at": configured_at,
        "audit": {
            "function_id": "PLANNING_SOURCE",
            "user_request_sha256": request_sha256(args.user_request),
        },
    }
    if credential_ref:
        payload["credential_ref"] = credential_ref
    if svn_identity:
        payload["svn"] = persisted_svn_identity(svn_identity)
    write_json(local_config_path(repo, project), payload)
    result = {
        "contract_version": LOCAL_SCHEMA,
        "status": "reconfigured" if existing_local else "configured",
        "project_id": project,
        "product_id": product,
        "stage_id": stage,
        "management_mode": management_mode,
        "revision_policy": revision_policy,
        "provider": provider,
        "input_mode": input_mode,
        "source_path": str(source_path) if source_path else "",
        "credential_configured": credential_ref is not None,
        "local_config": local_config_path(repo, project).relative_to(repo).as_posix(),
        "tracked": False,
        "next_action": "Run planning_source.py check, then sync the exact source revision when ready.",
    }
    if svn_identity:
        result["source_control"] = persisted_svn_identity(svn_identity)
        result["working_copy_status"] = svn_identity.get("working_copy_status", {})
    return result


def check_status(repo: Path, project: str) -> dict[str, Any]:
    project, project_root = project_context(repo, project)
    local_path = local_config_path(repo, project)
    binding = load_binding(project_root)
    if not local_path.exists():
        return {
            "contract_version": LOCAL_SCHEMA,
            "status": "setup_required",
            "project_id": project,
            "local_source_configured": False,
            "binding_configured": bool(binding),
        }
    local = load_local_config(repo, project)
    if local.get("contract_version") != LOCAL_SCHEMA:
        return {
            "contract_version": LOCAL_SCHEMA,
            "status": "management_mode_required",
            "project_id": project,
            "product_id": str(local.get("product_id") or ""),
            "stage_id": str(local.get("stage_id") or ""),
            "local_source_configured": False,
            "binding_configured": bool(binding),
            "active_release_id": str((binding or {}).get("active_release_id") or ""),
            "next_action": (
                "Run planning_source.py configure and choose user_managed or tool_managed. "
                "The active release remains unchanged until then."
            ),
        }
    management_mode = configured_management_mode(local)
    provider = configured_provider(local)
    source_path = Path(str(local.get("source_path") or "")) if local.get("source_path") else None
    pending = unresolved_projection_impacts(repo, project, project_root, binding)
    source_details: dict[str, Any] = {}
    if provider == source_provider.SVN_PROVIDER:
        try:
            identity = inspect_configured_svn(local)
            source_details = {
                "source_control": persisted_svn_identity(identity),
                "working_copy_status": identity.get("working_copy_status", {}),
            }
            blockers = svn_source_blockers(local, identity)
            status = "ready"
            if blockers:
                source_details["sync_readiness"] = "blocked"
                source_details["sync_blockers"] = blockers
            else:
                source_details["sync_readiness"] = "ready"
            if management_mode == "user_managed":
                source_details["freshness_policy"] = "user_authoritative_no_remote_head_check"
            current_release = active_release(repo, project, project_root, binding)
            if (
                management_mode == "tool_managed"
                and binding
                and release_source_kind(current_release) != "svn_revision"
            ):
                status = "migration_required"
        except PlanningSourceError as error:
            status = (
                "credential_required"
                if "credential is unavailable" in str(error)
                else "source_unavailable"
            )
            source_details = {"source_error": str(error)}
    else:
        status = "ready" if source_path and source_path.is_dir() else "source_unavailable"
    if status == "ready" and pending:
        status = "projection_refresh_required"
    if status == "management_mode_required":
        next_action = "Choose one planning-source management mode explicitly."
    elif status == "credential_required":
        next_action = "Configure the remote-source credential through the private setup prompt."
    elif status == "source_unavailable":
        next_action = "Restore or reconfigure the local planning source folder."
    elif status == "migration_required":
        next_action = "Run sync to replace the legacy folder release with an exact SVN revision release."
    elif not binding:
        next_action = "Run check, then sync to seal and bind the first complete release."
    elif pending:
        next_action = "Run the explicit KNOWLEDGE refresh/bind workflow for pending datasets."
    elif source_details.get("sync_readiness") == "blocked":
        next_action = (
            "The current binding remains usable. Resolve the local source state only before an "
            "explicit sync."
        )
    else:
        next_action = "Planning source and active planning-backed Knowledge are aligned."
    return {
        "contract_version": LOCAL_SCHEMA,
        "status": status,
        "project_id": project,
        "product_id": local["product_id"],
        "stage_id": local["stage_id"],
        "management_mode": management_mode,
        "revision_policy": local["revision_policy"],
        "provider": provider,
        "input_mode": local["input_mode"],
        "source_path": str(source_path) if source_path else "",
        "credential_configured": isinstance(local.get("credential_ref"), dict),
        "local_source_configured": True,
        "binding_configured": bool(binding),
        "active_release_id": str((binding or {}).get("active_release_id") or ""),
        "pending_projection_count": len(pending),
        "pending_projections": pending[:PREVIEW_LIMIT],
        "next_action": next_action,
        **source_details,
    }


def check(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    project, project_root = project_context(repo, args.project)
    local = load_local_config(repo, project)
    management_mode = configured_management_mode(local)
    binding = load_binding(project_root)
    provider = configured_provider(local)
    current_release = active_release(repo, project, project_root, binding)
    if provider == source_provider.SVN_PROVIDER:
        identity = inspect_configured_svn(local)
        blockers = svn_source_blockers(local, identity)
        active_control = (
            current_release.get("source_control")
            if isinstance((current_release or {}).get("source_control"), dict)
            else {}
        )
        if (
            active_control.get("repository_uuid") == identity.get("repository_uuid")
            and active_control.get("source_url") == identity.get("source_url")
            and int(identity.get("revision") or 0) > 0
            and int(active_control.get("revision") or 0) > int(identity.get("revision") or 0)
        ):
            blockers.append(
                "The configured source revision is older than the active release; update the source or reconfigure it instead of rolling back implicitly."
            )
        if blockers:
            return {
                "contract_version": RELEASE_SCHEMA,
                "status": "source_not_ready",
                "project_id": project,
                "management_mode": management_mode,
                "revision_policy": local["revision_policy"],
                "provider": provider,
                "active_release_id": str((binding or {}).get("active_release_id") or ""),
                "source_control": persisted_svn_identity(identity),
                "working_copy_status": identity.get("working_copy_status", {}),
                "blockers": blockers,
                "next_action": "Resolve the source state, then run check again.",
            }
        if same_svn_revision(current_release, identity):
            return {
                "contract_version": RELEASE_SCHEMA,
                "status": "unchanged",
                "project_id": project,
                "product_id": local["product_id"],
                "stage_id": local["stage_id"],
                "management_mode": management_mode,
                "revision_policy": local["revision_policy"],
                "provider": provider,
                "active_release_id": str((binding or {}).get("active_release_id") or ""),
                "source_control": persisted_svn_identity(identity),
                "working_copy_status": identity.get("working_copy_status", {}),
                "next_action": "No sync is required.",
            }
        revision_diff = {"added": [], "changed": [], "removed": []}
        if (
            release_source_kind(current_release) == "svn_revision"
            and active_control.get("repository_uuid") == identity.get("repository_uuid")
            and active_control.get("source_url") == identity.get("source_url")
        ):
            try:
                revision_diff = source_provider.svn_changed_paths(
                    str(identity["source_url"]),
                    int(active_control["revision"]),
                    int(identity["revision"]),
                    auth=configured_svn_auth(local),
                )
            except source_provider.PlanningSourceProviderError as error:
                raise PlanningSourceError(str(error)) from error
        migration = release_source_kind(current_release) not in {"", "svn_revision"}
        return {
            "contract_version": RELEASE_SCHEMA,
            "status": "migration_required" if migration else "changed",
            "project_id": project,
            "product_id": local["product_id"],
            "stage_id": local["stage_id"],
            "management_mode": management_mode,
            "revision_policy": local["revision_policy"],
            "provider": provider,
            "active_release_id": str((binding or {}).get("active_release_id") or ""),
            "source_control": persisted_svn_identity(identity),
            "working_copy_status": identity.get("working_copy_status", {}),
            "revision_diff": {
                "counts": {key: len(revision_diff[key]) for key in ("added", "changed", "removed")},
                "preview": {key: revision_diff[key][:PREVIEW_LIMIT] for key in ("added", "changed", "removed")},
            },
            "next_action": "Run sync to export and seal the exact SVN revision.",
        }

    scan = scan_source(repo, project, Path(str(local["source_path"])))
    previous = release_files(repo, project, project_root, binding)
    diff = file_diff(previous, scan["files"])
    changed = not binding or scan["tree_sha256"] != binding.get("tree_sha256")
    return {
        "contract_version": RELEASE_SCHEMA,
        "status": "changed" if changed else "unchanged",
        "project_id": project,
        "product_id": local["product_id"],
        "stage_id": local["stage_id"],
        "management_mode": management_mode,
        "revision_policy": local["revision_policy"],
        "provider": provider,
        "active_release_id": str((binding or {}).get("active_release_id") or ""),
        "file_count": scan["file_count"],
        "total_bytes": scan["total_bytes"],
        "tree_sha256": scan["tree_sha256"],
        "hashed_file_count": scan["hashed_file_count"],
        "diff": {**diff["counts"], "preview": {key: diff[key][:PREVIEW_LIMIT] for key in ("added", "changed", "removed")}},
        "next_action": "Run sync to create a complete immutable release." if changed else "No sync is required.",
    }


def next_release_id(root: Path, product: str, stage: str) -> str:
    highest = 0
    if root.exists():
        for path in root.iterdir():
            if not path.is_dir():
                continue
            match = RELEASE_ID_RE.fullmatch(path.name)
            if match and match.group(1) == product and match.group(2) == stage:
                highest = max(highest, int(match.group(3)))
    return f"PSR-{product}-{stage}-{highest + 1:04d}"


def update_registry(repo: Path, release: dict[str, Any]) -> None:
    path = registry_path(repo)
    registry = read_json(path, {"contract_version": REGISTRY_SCHEMA, "releases": []})
    if registry.get("contract_version") != REGISTRY_SCHEMA or not isinstance(registry.get("releases"), list):
        raise PlanningSourceError("unsupported planning-source registry")
    row = {
        "release_id": release["release_id"],
        "product_id": release["product_id"],
        "stage_id": release["stage_id"],
        "source_kind": release_source_kind(release),
        "tree_sha256": release["tree_sha256"],
        "file_count": release["file_count"],
        "release_manifest": release["release_manifest"],
        "sealed_at": release["sealed_at"],
    }
    control = release.get("source_control") if isinstance(release.get("source_control"), dict) else {}
    if control:
        row["source_revision"] = int(control.get("revision") or 0)
    rows = [item for item in registry["releases"] if item.get("release_id") != row["release_id"]]
    rows.append(row)
    registry["releases"] = sorted(rows, key=lambda item: str(item["release_id"]))
    registry["updated_at"] = now_iso()
    registry["generation_provenance"] = build_generation_provenance(
        generator_script="planning_source.py",
        workflow="planning_source_registry_update",
        artifact_kind="PLANNING_SOURCE_REGISTRY",
        source="sealed_releases",
    )
    write_json(path, registry)


def seal_release(
    *,
    args: argparse.Namespace,
    repo: Path,
    project: str,
    project_root: Path,
    local: dict[str, Any],
    binding: dict[str, Any] | None,
    scan: dict[str, Any],
    source_kind: str,
    source_control: dict[str, Any] | None = None,
    embedded_source_root: Path | None = None,
) -> dict[str, Any]:
    product = validate_id(str(local["product_id"]), "product")
    stage = validate_id(str(local["stage_id"]), "stage")
    previous_rows = release_files(repo, project, project_root, binding)
    diff = file_diff(previous_rows, scan["files"])
    current_release = active_release(repo, project, project_root, binding)
    same_identity = release_source_kind(current_release) == source_kind
    if source_kind == "svn_revision" and source_control:
        same_identity = same_svn_revision(current_release, source_control)
    if binding and binding.get("tree_sha256") == scan["tree_sha256"] and same_identity:
        return {
            "contract_version": RELEASE_SCHEMA,
            "status": "unchanged",
            "project_id": project,
            "active_release_id": binding["active_release_id"],
            "source_kind": source_kind,
            "tree_sha256": scan["tree_sha256"],
            "file_count": scan["file_count"],
        }

    releases = release_root(repo, product, stage)
    release_id = next_release_id(releases, product, stage)
    final_dir = releases / release_id
    if final_dir.exists():
        raise PlanningSourceError(f"release already exists: {release_id}")
    staging = repo / ".local" / "planning-source-staging" / f"{release_id}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    files_dir = staging / "files"
    if source_kind == "folder_snapshot":
        if embedded_source_root is None:
            raise PlanningSourceError("folder snapshot requires an embedded source root")
        files_dir.mkdir()
    binding_file = binding_path(project_root)
    registry_file = registry_path(repo)
    old_binding = binding_file.read_bytes() if binding_file.exists() else None
    old_registry = registry_file.read_bytes() if registry_file.exists() else None
    release_moved = False
    try:
        if source_kind == "folder_snapshot":
            for row in scan["files"]:
                relative = safe_relative(str(row["relative_path"]))
                source = embedded_source_root / Path(*PurePosixPath(relative).parts)
                target = files_dir / Path(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if file_sha256(target) != row["sha256"]:
                    raise PlanningSourceError(f"copied planning file hash mismatch: {relative}")
        sealed_at = now_iso()
        write_json(
            staging / "files.json",
            {
                "contract_version": FILES_SCHEMA,
                "release_id": release_id,
                "tree_sha256": scan["tree_sha256"],
                "files": scan["files"],
            },
        )
        previous_release = str((binding or {}).get("active_release_id") or "")
        write_json(
            staging / "diff.json",
            {
                "contract_version": "planning_source_diff_v1",
                "from_release_id": previous_release,
                "to_release_id": release_id,
                **diff,
            },
        )
        release_manifest_rel = (
            Path("planning-sources")
            / product
            / "stages"
            / stage
            / "releases"
            / release_id
            / "release.json"
        ).as_posix()
        provenance_source = "svn_revision" if source_kind == "svn_revision" else "complete_folder_snapshot"
        release = {
            "contract_version": RELEASE_SCHEMA,
            "release_id": release_id,
            "product_id": product,
            "stage_id": stage,
            "state": "sealed",
            "source_kind": source_kind,
            "previous_release_id": previous_release,
            "tree_sha256": scan["tree_sha256"],
            "file_count": scan["file_count"],
            "total_bytes": scan["total_bytes"],
            "files_manifest": "files.json",
            "diff_manifest": "diff.json",
            "release_manifest": release_manifest_rel,
            "source_policy": {
                "snapshot_mode": "manifest_only_svn_revision"
                if source_kind == "svn_revision"
                else "embedded_complete_folder",
                "complete_file_manifest": True,
                "embedded_files": source_kind == "folder_snapshot",
                "knowledge_source_snapshot_required": True,
                "excluded_directories": sorted(SKIP_DIRS),
                "excluded_file_rules": [
                    "Office temporary files",
                    "system metadata",
                    "temporary suffixes",
                ],
            },
            "sealed_at": sealed_at,
            "generation_provenance": build_generation_provenance(
                generator_script="planning_source.py",
                workflow="planning_source_sync",
                artifact_kind="PLANNING_SOURCE_RELEASE",
                generated_at=sealed_at,
                source=provenance_source,
                extra={"generated_by_ldap": generated_by_ldap(repo)},
            ),
            "audit": {
                "function_id": "PLANNING_SOURCE",
                "user_request_sha256": request_sha256(args.user_request),
            },
        }
        if source_kind == "folder_snapshot":
            release["files_root"] = "files"
        elif source_control:
            release["source_control"] = source_control
        write_json(staging / "release.json", release)
        releases.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(final_dir))
        release_moved = True
        relative_manifest = Path(os.path.relpath(final_dir / "release.json", project_root)).as_posix()
        project_binding = {
            "contract_version": BINDING_SCHEMA,
            "project_id": project,
            "product_id": product,
            "stage_id": stage,
            "active_release_id": release_id,
            "release_manifest": relative_manifest,
            "source_kind": source_kind,
            "tree_sha256": scan["tree_sha256"],
            "bound_at": sealed_at,
            "binding_reason": args.reason,
            "generation_provenance": build_generation_provenance(
                generator_script="planning_source.py",
                workflow="planning_source_binding",
                artifact_kind="PLANNING_SOURCE_BINDING",
                generated_at=sealed_at,
                source="sealed_release",
                extra={"generated_by_ldap": generated_by_ldap(repo)},
            ),
        }
        if source_control:
            project_binding["source_revision"] = int(source_control["revision"])
        write_json(binding_file, project_binding)
        update_registry(repo, release)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if old_binding is None:
            if binding_file.exists():
                binding_file.unlink()
        else:
            binding_file.parent.mkdir(parents=True, exist_ok=True)
            binding_file.write_bytes(old_binding)
        if old_registry is None:
            if registry_file.exists():
                registry_file.unlink()
        else:
            registry_file.parent.mkdir(parents=True, exist_ok=True)
            registry_file.write_bytes(old_registry)
        if release_moved and final_dir.exists():
            shutil.rmtree(final_dir)
        raise
    impacts = projection_impacts(repo, project, diff)
    result = {
        "contract_version": RELEASE_SCHEMA,
        "status": "synced",
        "project_id": project,
        "product_id": product,
        "stage_id": stage,
        "source_kind": source_kind,
        "release_id": release_id,
        "tree_sha256": scan["tree_sha256"],
        "file_count": scan["file_count"],
        "total_bytes": scan["total_bytes"],
        "diff": diff["counts"],
        "projection_impacts": impacts,
        "release_manifest": release_manifest_rel,
        "project_binding": binding_path(project_root).relative_to(repo).as_posix(),
    }
    if source_control:
        result["source_control"] = source_control
    return result


def sync(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    project, project_root = project_context(repo, args.project)
    local = load_local_config(repo, project)
    management_mode = configured_management_mode(local)
    binding = load_binding(project_root)
    provider = configured_provider(local)
    if provider == source_provider.FOLDER_PROVIDER:
        source_root = Path(str(local["source_path"]))
        scan = scan_source(repo, project, source_root)
        result = seal_release(
            args=args,
            repo=repo,
            project=project,
            project_root=project_root,
            local=local,
            binding=binding,
            scan=scan,
            source_kind="folder_snapshot",
            embedded_source_root=scan["source_root"],
        )
        result["management_mode"] = management_mode
        result["revision_policy"] = local["revision_policy"]
        return result

    identity = inspect_configured_svn(local)
    current_release = active_release(repo, project, project_root, binding)
    blockers = svn_source_blockers(local, identity)
    active_control = (
        current_release.get("source_control")
        if isinstance((current_release or {}).get("source_control"), dict)
        else {}
    )
    if (
        active_control.get("repository_uuid") == identity.get("repository_uuid")
        and active_control.get("source_url") == identity.get("source_url")
        and int(identity.get("revision") or 0) > 0
        and int(active_control.get("revision") or 0) > int(identity.get("revision") or 0)
    ):
        blockers.append(
            "The configured source revision is older than the active release; implicit rollback is forbidden."
        )
    if blockers:
        raise PlanningSourceError(" ".join(blockers))
    if binding and same_svn_revision(current_release, identity):
        return {
            "contract_version": RELEASE_SCHEMA,
            "status": "unchanged",
            "project_id": project,
            "management_mode": management_mode,
            "revision_policy": local["revision_policy"],
            "active_release_id": binding["active_release_id"],
            "source_kind": "svn_revision",
            "source_control": persisted_svn_identity(identity),
            "working_copy_status": identity.get("working_copy_status", {}),
            "tree_sha256": binding["tree_sha256"],
            "file_count": int((current_release or {}).get("file_count") or 0),
            "local_working_copy_used": False,
        }
    auth = configured_svn_auth(local)
    try:
        externals = source_provider.svn_externals(
            str(identity["source_url"]), int(identity["revision"]), auth=auth
        )
    except source_provider.PlanningSourceProviderError as error:
        raise PlanningSourceError(str(error)) from error
    if externals:
        paths = ", ".join(str(item.get("path") or "") for item in externals[:PREVIEW_LIMIT])
        raise PlanningSourceError(
            f"SVN externals are not supported in sealed planning sources: {paths}"
        )
    export_transaction = (
        repo
        / ".local"
        / "planning-source-export"
        / f"{project}-{int(identity['revision'])}-{uuid.uuid4().hex}"
    )
    export_root = export_transaction / "source"
    try:
        try:
            source_provider.export_svn_revision(
                str(identity["source_url"]),
                int(identity["revision"]),
                export_root,
                auth=auth,
            )
        except source_provider.PlanningSourceProviderError as error:
            raise PlanningSourceError(str(error)) from error
        scan = scan_source(repo, project, export_root)
        result = seal_release(
            args=args,
            repo=repo,
            project=project,
            project_root=project_root,
            local=local,
            binding=binding,
            scan=scan,
            source_kind="svn_revision",
            source_control=persisted_svn_identity(identity),
        )
        result["working_copy_status"] = identity.get("working_copy_status", {})
        result["local_working_copy_used"] = False
        result["management_mode"] = management_mode
        result["revision_policy"] = local["revision_policy"]
        return result
    finally:
        if export_transaction.exists():
            shutil.rmtree(export_transaction)
def validate_active(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    project, project_root = project_context(repo, args.project)
    binding = load_binding(project_root)
    if not binding:
        return {
            "contract_version": RELEASE_SCHEMA,
            "status": "fail",
            "project_id": project,
            "problems": ["project planning-source binding is missing"],
        }
    problems = []
    pending: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    release: dict[str, Any] = {}
    verification_mode = "unknown"
    try:
        project_manifest, release = resolve_release(repo, project, project_root, binding)
        rows = release_files(repo, project, project_root, binding)
        verified_rows = []
        for row in rows:
            relative = safe_relative(str(row.get("relative_path") or ""))
            verified_rows.append(
                {"path": relative, "size": row.get("size_bytes"), "sha256": row.get("sha256")}
            )
        tree_hash = json_sha256(verified_rows)
        if tree_hash != release.get("tree_sha256"):
            problems.append("release tree fingerprint mismatch")
        if len(rows) != release.get("file_count"):
            problems.append("release file_count differs from files manifest")
        total_bytes = sum(int(row.get("size_bytes") or 0) for row in rows)
        if total_bytes != release.get("total_bytes"):
            problems.append("release total_bytes differs from files manifest")
        source_kind = release_source_kind(release)
        if binding.get("contract_version") == BINDING_SCHEMA and binding.get("source_kind") != source_kind:
            problems.append("planning-source binding source_kind differs from its release")
        if source_kind == "folder_snapshot":
            verification_mode = "embedded_files"
            files_root = project_manifest.parent / str(release.get("files_root") or "files")
            for row in rows:
                relative = safe_relative(str(row.get("relative_path") or ""))
                path = files_root / Path(*PurePosixPath(relative).parts)
                if not path.is_file():
                    problems.append(f"missing release file: {relative}")
                    continue
                digest = file_sha256(path)
                if digest != row.get("sha256") or path.stat().st_size != row.get("size_bytes"):
                    problems.append(f"release file drift: {relative}")
            listed = {str(row.get("relative_path") or "") for row in rows}
            actual = {
                safe_relative(path.relative_to(files_root).as_posix())
                for path in source_files(files_root)
            }
            if actual != listed:
                problems.append("release files directory differs from files manifest")
        elif source_kind == "svn_revision":
            verification_mode = "svn_manifest"
            control = release.get("source_control") if isinstance(release.get("source_control"), dict) else {}
            try:
                if control.get("contract_version") not in source_provider.SVN_SOURCE_SCHEMAS:
                    raise PlanningSourceError("SVN release source_control contract is invalid")
                source_provider.canonical_svn_url(str(control.get("source_url") or ""))
                source_provider.canonical_svn_url(str(control.get("repository_root") or ""))
                if not control.get("repository_uuid") or int(control.get("revision") or 0) <= 0:
                    raise PlanningSourceError("SVN release source_control identity is incomplete")
                if release.get("files_root"):
                    raise PlanningSourceError("SVN revision release must not embed a files directory")
            except (source_provider.PlanningSourceProviderError, ValueError) as error:
                problems.append(str(error))
        else:
            problems.append(f"unsupported planning-source release source_kind: {source_kind}")
        registry = read_json(registry_path(repo))
        registry_rows = registry.get("releases") if isinstance(registry.get("releases"), list) else []
        matches = [row for row in registry_rows if row.get("release_id") == release.get("release_id")]
        if registry.get("contract_version") != REGISTRY_SCHEMA or len(matches) != 1:
            problems.append("planning-source registry does not contain exactly one active release row")
        elif any(
            matches[0].get(field) != release.get(field)
            for field in ("product_id", "stage_id", "tree_sha256", "file_count")
        ):
            problems.append("planning-source registry row differs from active release")
        pending = unresolved_projection_impacts(repo, project, project_root, binding)
        for item in pending:
            problems.append(
                f"Knowledge dataset {item['dataset_id']} is not refreshed for {item['required_release_id']}"
            )
    except (PlanningSourceError, OSError) as error:
        problems.append(str(error))
    return {
        "contract_version": RELEASE_SCHEMA,
        "status": "pass" if not problems else "fail",
        "project_id": project,
        "active_release_id": binding.get("active_release_id"),
        "source_kind": release_source_kind(release),
        "verification_mode": verification_mode,
        "verified_file_count": len(rows),
        "pending_projection_count": len(pending),
        "pending_projections": pending[:PREVIEW_LIMIT],
        "problems": problems[:PREVIEW_LIMIT],
        "problem_count": len(problems),
    }


def history(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    project, project_root = project_context(repo, args.project)
    binding = load_binding(project_root)
    local = load_local_config(repo, project)
    root = release_root(repo, str(local["product_id"]), str(local["stage_id"]))
    rows = []
    if root.exists():
        for path in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
            if not path.is_dir() or not (path / "release.json").is_file():
                continue
            manifest = read_json(path / "release.json")
            control = manifest.get("source_control") if isinstance(manifest.get("source_control"), dict) else {}
            rows.append(
                {
                    "release_id": manifest.get("release_id"),
                    "state": manifest.get("state"),
                    "source_kind": release_source_kind(manifest),
                    "source_revision": control.get("revision"),
                    "file_count": manifest.get("file_count"),
                    "total_bytes": manifest.get("total_bytes"),
                    "tree_sha256": manifest.get("tree_sha256"),
                    "sealed_at": manifest.get("sealed_at"),
                    "active": manifest.get("release_id") == (binding or {}).get("active_release_id"),
                }
            )
    return {
        "contract_version": REGISTRY_SCHEMA,
        "status": "ok",
        "project_id": project,
        "product_id": local["product_id"],
        "stage_id": local["stage_id"],
        "releases": rows[: args.limit],
        "truncated": len(rows) > args.limit,
    }


def authorize(args: argparse.Namespace) -> None:
    if args.command not in {"status", "history", "validate"}:
        require_user_request(args.user_request, purpose=f"planning source {args.command}")
    require_user_function_selection(
        args.function_selection,
        user_request=args.user_request,
        allowed_ids=command_function_ids(Path(__file__).name, args.command),
        purpose=f"planning source {args.command}",
    )


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--project", required=True)
    parser.add_argument("--format", choices=["json"], default="json")
    add_function_gate_arguments(parser, selection_help="Use PLANNING_SOURCE or PROJECT_ADMIN.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show local source and active release status.")
    add_common(status)

    configure_parser = subparsers.add_parser(
        "configure", help="Configure or modify one project planning source."
    )
    add_common(configure_parser)
    configure_parser.add_argument("--product", required=True)
    configure_parser.add_argument("--stage", required=True)
    configure_parser.add_argument(
        "--management-mode",
        choices=sorted(MANAGEMENT_MODES),
        required=True,
    )
    configure_parser.add_argument("--source-path", type=Path)
    configure_parser.add_argument("--svn-url")
    configure_parser.add_argument("--svn-username")
    configure_parser.add_argument("--credential-env")
    configure_parser.add_argument(
        "--provider",
        choices=["auto", source_provider.SVN_PROVIDER, source_provider.FOLDER_PROVIDER],
        default="auto",
    )

    check_parser = subparsers.add_parser("check", help="Compare the configured source with the active release.")
    add_common(check_parser)

    sync_parser = subparsers.add_parser("sync", help="Create and bind an exact immutable source release.")
    add_common(sync_parser)
    sync_parser.add_argument("--reason", required=True)

    validate_parser = subparsers.add_parser("validate", help="Verify the active release and project binding.")
    add_common(validate_parser)

    history_parser = subparsers.add_parser("history", help="List bounded source-release history.")
    add_common(history_parser)
    history_parser.add_argument("--limit", type=int, default=20, choices=range(1, 201))
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        authorize(args)
        repo = resolve_repo(args.repo_root)
        if args.command == "status":
            result = check_status(repo, args.project)
        elif args.command == "configure":
            result = configure(args, repo)
        elif args.command == "check":
            result = check(args, repo)
        elif args.command == "sync":
            result = sync(args, repo)
        elif args.command == "validate":
            result = validate_active(args, repo)
        else:
            result = history(args, repo)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except FunctionGateError as error:
        exit_with_gate_error(parser, error)
    except (PlanningSourceError, OSError) as error:
        parser.exit(2, f"BLOCKED: {error}\n")


if __name__ == "__main__":
    main()
