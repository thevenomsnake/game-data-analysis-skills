"""Atomic project-local storage for formal asset packages.

The repository owns ``<project>/formal_assets`` and, when present, keeps the
``project_manifest_v2`` compact Package projection in the same transaction. It
does not modify legacy manifests, Query Workspace, or generated viewers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
from typing import Any, Mapping, Sequence
import uuid


PACKAGE_SCHEMA_VERSION = "formal_asset_package_v1"
RECEIPT_SCHEMA_VERSION = "formal_asset_repository_receipt_v1"
INDEX_SCHEMA_VERSION = "formal_asset_repository_index_v1"
FORMAL_ROOT_NAME = "formal_assets"
INDEX_NAME = "index.json"
PACKAGE_MANIFEST_NAME = "manifest.json"
LIFECYCLE_STATES = frozenset({"current", "history", "archived"})
PACKAGE_ID_RE = re.compile(r"^FA-(\d{4,})$")
MEMBER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
RECEIPT_ID_RE = re.compile(r"^R(\d{4,})$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

def _replace_path(source: str | Path, destination: str | Path) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.is_dir() and not destination_path.exists():
        last_error: PermissionError | None = None
        for attempt in range(8):
            try:
                os.rename(source_path, destination_path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.01 * (attempt + 1))
        if last_error is not None:
            raise last_error
    os.replace(source_path, destination_path)


# Kept indirect so the publication boundary can be fault-injected in tests.
_replace = _replace_path


class FormalAssetRepositoryError(ValueError):
    """Base error for malformed or unsafe repository operations."""


class StalePlanError(FormalAssetRepositoryError):
    """Raised when repository state changed after planning."""


class ReceiptValidationError(FormalAssetRepositoryError):
    """Raised when a receipt cannot be loaded safely."""


@dataclass(frozen=True)
class MemberSource:
    source_path: Path
    destination_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class FormalAssetPlan:
    project_root: Path
    package_id: str
    package_directory: str
    receipt_id: str
    base_index_sha256: str
    base_manifest_sha256: str
    base_project_manifest_sha256: str
    manifest: dict[str, Any]
    project_index: dict[str, Any]
    project_manifest: dict[str, Any] | None
    receipt: dict[str, Any]
    member_sources: tuple[MemberSource, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "formal_asset_repository_plan_v1",
            "project_root": str(self.project_root),
            "package_id": self.package_id,
            "package_directory": self.package_directory,
            "receipt_id": self.receipt_id,
            "base_index_sha256": self.base_index_sha256,
            "base_manifest_sha256": self.base_manifest_sha256,
            "base_project_manifest_sha256": self.base_project_manifest_sha256,
            "manifest": json.loads(json.dumps(self.manifest)),
            "project_index": json.loads(json.dumps(self.project_index)),
            "project_manifest": (
                json.loads(json.dumps(self.project_manifest))
                if self.project_manifest is not None
                else None
            ),
            "receipt": json.loads(json.dumps(self.receipt)),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalAssetRepositoryError(f"Cannot read JSON object: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FormalAssetRepositoryError(f"Expected a JSON object: {path}")
    return value


def _project_root(value: str | Path) -> Path:
    root = Path(value).resolve()
    if not root.is_dir():
        raise FormalAssetRepositoryError(f"Project root does not exist: {root}")
    return root


def _formal_root(root: Path) -> Path:
    return root / FORMAL_ROOT_NAME


def _index_path(root: Path) -> Path:
    return _formal_root(root) / INDEX_NAME


def _safe_relative(value: str | Path, *, label: str) -> str:
    text = str(value).replace("\\", "/").strip()
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or re.match(r"^[A-Za-z]:", text)
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\x00" in text
    ):
        raise FormalAssetRepositoryError(f"{label} must be a safe relative path: {value}")
    return path.as_posix()


def _ensure_formal_relative(value: str | Path, *, label: str) -> str:
    relative = _safe_relative(value, label=label)
    if PurePosixPath(relative).parts[0] != FORMAL_ROOT_NAME:
        raise FormalAssetRepositoryError(f"{label} must stay under {FORMAL_ROOT_NAME}/: {value}")
    return relative


def _staged_path(staging_root: Path, formal_relative: str | Path) -> Path:
    relative = _ensure_formal_relative(formal_relative, label="staged formal path")
    parts = PurePosixPath(relative).parts
    return staging_root / Path(*parts[1:])


def _slugify(value: str, fallback: str = "asset") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    if slug:
        return slug[:72].rstrip("-")
    digest = hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:8]
    return f"{fallback}-{digest}"


def _validate_state(value: str, *, label: str) -> str:
    state = str(value or "").strip().lower()
    if state not in LIFECYCLE_STATES:
        raise FormalAssetRepositoryError(f"{label} must be one of {sorted(LIFECYCLE_STATES)}")
    return state


def _validate_package_id(value: str) -> str:
    package_id = str(value or "").strip().upper()
    if not PACKAGE_ID_RE.fullmatch(package_id):
        raise FormalAssetRepositoryError(f"Invalid formal asset package id: {value}")
    return package_id


def _empty_index(root: Path) -> dict[str, Any]:
    project_config_path = root / "project_config.json"
    project_id = root.name
    if project_config_path.is_file():
        try:
            project_config = json.loads(project_config_path.read_text(encoding="utf-8"))
            if isinstance(project_config, dict):
                project_id = str(project_config.get("project_id") or project_id)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "project_id": project_id,
        "updated_at": "",
        "packages": [],
    }


def _load_index(root: Path) -> tuple[dict[str, Any], bytes]:
    path = _index_path(root)
    if not path.is_file():
        return _empty_index(root), b""
    raw = path.read_bytes()
    try:
        index = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormalAssetRepositoryError(f"Invalid formal asset repository index: {path}: {exc}") from exc
    if not isinstance(index, dict) or index.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise FormalAssetRepositoryError(f"Unsupported formal asset repository index: {path}")
    packages = index.get("packages")
    if not isinstance(packages, list):
        raise FormalAssetRepositoryError(f"Formal asset repository index packages must be an array: {path}")
    seen: set[str] = set()
    for item in packages:
        if not isinstance(item, dict):
            raise FormalAssetRepositoryError(f"Formal asset repository index contains a non-object package: {path}")
        package_id = _validate_package_id(str(item.get("package_id") or ""))
        if package_id in seen:
            raise FormalAssetRepositoryError(f"Duplicate package id in repository index: {package_id}")
        seen.add(package_id)
        _ensure_formal_relative(str(item.get("directory") or ""), label="package directory")
        _ensure_formal_relative(str(item.get("manifest_path") or ""), label="package manifest path")
    return index, raw


def _package_number(package_id: str) -> int:
    match = PACKAGE_ID_RE.fullmatch(package_id)
    if not match:
        raise FormalAssetRepositoryError(f"Invalid formal asset package id: {package_id}")
    return int(match.group(1))


def _next_package_id(root: Path, index: Mapping[str, Any]) -> str:
    used = {
        _package_number(str(item.get("package_id")))
        for item in index.get("packages", [])
        if isinstance(item, dict) and PACKAGE_ID_RE.fullmatch(str(item.get("package_id") or ""))
    }
    formal_root = _formal_root(root)
    if formal_root.is_dir():
        for path in formal_root.iterdir():
            if not path.is_dir():
                continue
            prefix = path.name.split("-", 2)
            if len(prefix) >= 2:
                match = PACKAGE_ID_RE.fullmatch("-".join(prefix[:2]))
                if match:
                    used.add(int(match.group(1)))
    return f"FA-{max(used, default=0) + 1:04d}"


def _index_entry(index: Mapping[str, Any], package_id: str) -> dict[str, Any] | None:
    for item in index.get("packages", []):
        if isinstance(item, dict) and item.get("package_id") == package_id:
            return dict(item)
    return None


def _manifest_path_from_entry(root: Path, entry: Mapping[str, Any]) -> Path:
    relative = _ensure_formal_relative(str(entry.get("manifest_path") or ""), label="package manifest path")
    return root / Path(relative)


def _validate_manifest_shape(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise FormalAssetRepositoryError("Unsupported formal asset package schema_version")
    package_id = _validate_package_id(str(manifest.get("package_id") or ""))
    if not str(manifest.get("project_id") or "").strip():
        raise FormalAssetRepositoryError("Formal asset package project_id is required")
    _validate_state(str(manifest.get("lifecycle_state") or ""), label="package lifecycle_state")
    directory = _ensure_formal_relative(str(manifest.get("directory") or ""), label="package directory")
    expected_prefix = f"{FORMAL_ROOT_NAME}/{package_id}-"
    if not directory.startswith(expected_prefix):
        raise FormalAssetRepositoryError(f"Package directory does not match package id {package_id}: {directory}")
    members = manifest.get("members")
    if not isinstance(members, list):
        raise FormalAssetRepositoryError("Package members must be an array")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for member in members:
        if not isinstance(member, dict):
            raise FormalAssetRepositoryError("Package member must be an object")
        member_id = str(member.get("member_id") or "")
        if not MEMBER_ID_RE.fullmatch(member_id):
            raise FormalAssetRepositoryError(f"Invalid formal asset member id: {member_id}")
        path = _ensure_formal_relative(str(member.get("path") or ""), label="member path")
        if not path.startswith(f"{directory}/members/"):
            raise FormalAssetRepositoryError(f"Member path is outside its package members directory: {path}")
        if member_id in seen_ids or path in seen_paths:
            raise FormalAssetRepositoryError(f"Duplicate member id or path: {member_id} {path}")
        seen_ids.add(member_id)
        seen_paths.add(path)
        _validate_state(str(member.get("lifecycle_state") or ""), label=f"member {member_id} lifecycle_state")
        if not SHA256_RE.fullmatch(str(member.get("sha256") or "")):
            raise FormalAssetRepositoryError(f"Invalid member sha256: {member_id}")
        if not isinstance(member.get("size_bytes"), int) or int(member["size_bytes"]) < 0:
            raise FormalAssetRepositoryError(f"Invalid member size_bytes: {member_id}")
    current = manifest.get("current")
    if not isinstance(current, dict) or not isinstance(current.get("member_ids"), list):
        raise FormalAssetRepositoryError("Package current must contain member_ids")
    expected_current = sorted(
        str(item["member_id"]) for item in members if item.get("lifecycle_state") == "current"
    )
    if sorted(str(item) for item in current["member_ids"]) != expected_current:
        raise FormalAssetRepositoryError("Package current.member_ids does not match member lifecycle states")
    lineage = manifest.get("lineage")
    if not isinstance(lineage, list):
        raise FormalAssetRepositoryError("Package lineage must be an array")
    for edge in lineage:
        if not isinstance(edge, dict):
            raise FormalAssetRepositoryError("Package lineage edge must be an object")
        source_id = str(edge.get("from_member_id") or "")
        target_id = str(edge.get("to_member_id") or "")
        if source_id not in seen_ids or target_id not in seen_ids or source_id == target_id:
            raise FormalAssetRepositoryError(f"Invalid package lineage edge: {source_id} -> {target_id}")
    if "metadata" in manifest and not isinstance(manifest.get("metadata"), dict):
        raise FormalAssetRepositoryError("Formal asset package metadata must be an object")


def _allowed_transition(previous: str, current: str) -> bool:
    return current == previous or (previous, current) in {
        ("current", "history"),
        ("current", "archived"),
        ("history", "archived"),
    }


def _member_id_for_path(target_path: str) -> str:
    digest = hashlib.sha256(target_path.encode("utf-8")).hexdigest()[:16].upper()
    return f"FM-{digest}"


def _resolve_source(value: Any) -> Path:
    path = Path(str(value)).resolve()
    if not path.is_file():
        raise FormalAssetRepositoryError(f"Formal asset member source is not a file: {path}")
    return path


def _build_members(
    root: Path,
    package_directory: str,
    existing_manifest: Mapping[str, Any] | None,
    member_inputs: Sequence[Mapping[str, Any]],
    timestamp: str,
) -> tuple[list[dict[str, Any]], tuple[MemberSource, ...]]:
    existing_members = [
        dict(item)
        for item in (existing_manifest or {}).get("members", [])
        if isinstance(item, dict)
    ]
    by_id = {str(item.get("member_id")): item for item in existing_members}
    by_path = {str(item.get("path")): item for item in existing_members}
    sources: list[MemberSource] = []

    for raw in member_inputs:
        if not isinstance(raw, Mapping):
            raise FormalAssetRepositoryError("Each formal asset member input must be a mapping")
        supplied_id = str(raw.get("member_id") or "").strip()
        existing = by_id.get(supplied_id) if supplied_id else None
        target_value = raw.get("target_path", raw.get("path"))

        if existing is not None:
            if target_value:
                target_path = _safe_relative(target_value, label="member target_path")
                expected = f"{package_directory}/members/{target_path}"
                if expected != existing.get("path"):
                    raise FormalAssetRepositoryError(
                        f"Existing member {supplied_id} path is immutable: {existing.get('path')}"
                    )
            role = str(raw.get("role") or existing.get("role") or "artifact").strip()
            if role != existing.get("role"):
                raise FormalAssetRepositoryError(f"Existing member {supplied_id} role is immutable")
            state = _validate_state(
                str(raw.get("lifecycle_state") or existing.get("lifecycle_state")),
                label=f"member {supplied_id} lifecycle_state",
            )
            if not _allowed_transition(str(existing.get("lifecycle_state")), state):
                raise FormalAssetRepositoryError(
                    f"Invalid member lifecycle transition for {supplied_id}: "
                    f"{existing.get('lifecycle_state')} -> {state}"
                )
            if raw.get("source_path") is not None:
                source = _resolve_source(raw["source_path"])
                source_hash = _sha256_file(source)
                if source_hash != existing.get("sha256") or source.stat().st_size != existing.get("size_bytes"):
                    raise FormalAssetRepositoryError(
                        f"Existing member {supplied_id} bytes are immutable; add a new member path/version"
                    )
            existing["lifecycle_state"] = state
            continue

        if target_value is None or raw.get("source_path") is None:
            raise FormalAssetRepositoryError("A new member requires source_path and target_path")
        target_path = _safe_relative(target_value, label="member target_path")
        project_relative = f"{package_directory}/members/{target_path}"
        member_id = supplied_id or _member_id_for_path(target_path)
        if not MEMBER_ID_RE.fullmatch(member_id):
            raise FormalAssetRepositoryError(f"Invalid formal asset member id: {member_id}")
        if member_id in by_id:
            raise FormalAssetRepositoryError(f"Duplicate formal asset member id: {member_id}")
        if project_relative in by_path:
            raise FormalAssetRepositoryError(f"Formal asset member path already exists: {project_relative}")
        source = _resolve_source(raw["source_path"])
        role = str(raw.get("role") or "artifact").strip()
        if not role or len(role) > 80:
            raise FormalAssetRepositoryError("Formal asset member role must be 1-80 characters")
        state = _validate_state(
            str(raw.get("lifecycle_state") or "current"),
            label=f"member {member_id} lifecycle_state",
        )
        source_hash = _sha256_file(source)
        member = {
            "member_id": member_id,
            "role": role,
            "lifecycle_state": state,
            "path": project_relative,
            "sha256": source_hash,
            "size_bytes": source.stat().st_size,
            "created_at": timestamp,
        }
        existing_members.append(member)
        by_id[member_id] = member
        by_path[project_relative] = member
        sources.append(
            MemberSource(
                source_path=source,
                destination_path=project_relative,
                sha256=source_hash,
                size_bytes=source.stat().st_size,
            )
        )

    if not existing_members:
        raise FormalAssetRepositoryError("A formal asset package requires at least one member")
    return sorted(existing_members, key=lambda item: str(item["member_id"])), tuple(sources)


def _build_lineage(
    members: Sequence[Mapping[str, Any]],
    existing_manifest: Mapping[str, Any] | None,
    lineage_inputs: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    member_ids = {str(item.get("member_id")) for item in members}
    lineage = [
        {key: str(value) for key, value in item.items() if key in {"relation", "from_member_id", "to_member_id", "note"}}
        for item in (existing_manifest or {}).get("lineage", [])
        if isinstance(item, dict)
    ]
    seen = {
        (item.get("relation"), item.get("from_member_id"), item.get("to_member_id"), item.get("note", ""))
        for item in lineage
    }
    for raw in lineage_inputs:
        if not isinstance(raw, Mapping):
            raise FormalAssetRepositoryError("Each lineage input must be a mapping")
        edge = {
            "relation": str(raw.get("relation") or "").strip(),
            "from_member_id": str(raw.get("from_member_id") or "").strip(),
            "to_member_id": str(raw.get("to_member_id") or "").strip(),
        }
        note = str(raw.get("note") or "").strip()
        if note:
            edge["note"] = note
        if not edge["relation"] or len(edge["relation"]) > 80:
            raise FormalAssetRepositoryError("Lineage relation must be 1-80 characters")
        if (
            edge["from_member_id"] not in member_ids
            or edge["to_member_id"] not in member_ids
            or edge["from_member_id"] == edge["to_member_id"]
        ):
            raise FormalAssetRepositoryError(
                f"Lineage must reference two distinct package members: "
                f"{edge['from_member_id']} -> {edge['to_member_id']}"
            )
        key = (edge["relation"], edge["from_member_id"], edge["to_member_id"], edge.get("note", ""))
        if key not in seen:
            lineage.append(edge)
            seen.add(key)
    return lineage


def _current_projection(members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    current = [item for item in members if item.get("lifecycle_state") == "current"]
    by_role: dict[str, list[str]] = {}
    for item in current:
        by_role.setdefault(str(item.get("role") or "artifact"), []).append(str(item["member_id"]))
    return {
        "member_ids": sorted(str(item["member_id"]) for item in current),
        "by_role": {key: sorted(value) for key, value in sorted(by_role.items())},
    }


def _next_receipt_id(existing_manifest: Mapping[str, Any] | None) -> str:
    revision = int((existing_manifest or {}).get("revision") or 0) + 1
    return f"R{revision:04d}"


def _build_index_entry(manifest: Mapping[str, Any], manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "package_id": manifest["package_id"],
        "slug": manifest["slug"],
        "title": manifest["title"],
        "lifecycle_state": manifest["lifecycle_state"],
        "revision": manifest["revision"],
        "directory": manifest["directory"],
        "manifest_path": f"{manifest['directory']}/{PACKAGE_MANIFEST_NAME}",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "current": manifest["current"],
        "latest_receipt": manifest["latest_receipt"],
        "updated_at": manifest["updated_at"],
    }


def _project_manifest_projection(
    root: Path,
    project_index: Mapping[str, Any],
    timestamp: str,
) -> tuple[str, dict[str, Any] | None]:
    path = root / "manifest.json"
    if not path.is_file():
        return "", None
    raw = path.read_bytes()
    try:
        current = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormalAssetRepositoryError(f"Project manifest is unreadable: {path}") from exc
    if not isinstance(current, dict):
        raise FormalAssetRepositoryError(f"Project manifest must be a JSON object: {path}")
    if current.get("schema_version") != "project_manifest_v2":
        return "", None

    projection = dict(current)
    repository_contract = dict(projection.get("formal_asset_repository") or {})
    repository_contract.update(
        {
            "index": f"{FORMAL_ROOT_NAME}/{INDEX_NAME}",
            "migration_map": str(
                repository_contract.get("migration_map")
                or f"{FORMAL_ROOT_NAME}/migration-map.v1.json"
            ),
            "package_count": len(project_index.get("packages", [])),
        }
    )
    projection.update(
        {
            "updated_at": timestamp,
            "formal_asset_repository": repository_contract,
            "packages": json.loads(json.dumps(project_index.get("packages", []))),
        }
    )
    return _sha256_bytes(raw), projection


def _repository_history_files(
    root: Path,
    package_directory: str,
    receipt_id: str,
) -> list[dict[str, Any]]:
    current_match = RECEIPT_ID_RE.fullmatch(receipt_id)
    if not current_match:
        raise FormalAssetRepositoryError(f"Invalid planned receipt id: {receipt_id}")
    current_number = int(current_match.group(1))
    package_root = root / Path(package_directory)
    result: list[dict[str, Any]] = []
    for directory_name, role in (
        ("manifests", "package_manifest_snapshot_history"),
        ("receipts", "package_receipt_history"),
    ):
        history_root = package_root / directory_name
        if not history_root.is_dir():
            continue
        for path in sorted(history_root.glob("R*.json")):
            match = RECEIPT_ID_RE.fullmatch(path.stem)
            if not match or int(match.group(1)) >= current_number:
                continue
            relative = path.relative_to(root).as_posix()
            result.append(
                {
                    "path": _ensure_formal_relative(relative, label="repository history path"),
                    "sha256": _sha256_file(path),
                    "size_bytes": path.stat().st_size,
                    "role": role,
                }
            )
    return result


def plan_package(
    project_root: str | Path,
    *,
    title: str,
    members: Sequence[Mapping[str, Any]],
    package_id: str | None = None,
    slug: str | None = None,
    lineage: Sequence[Mapping[str, Any]] = (),
    lifecycle_state: str = "current",
    metadata: Mapping[str, Any] | None = None,
) -> FormalAssetPlan:
    """Plan one package create/update without writing to disk.

    New member inputs require ``source_path`` and ``target_path``. Existing
    members are addressed by ``member_id`` and may only move forward through
    current -> history -> archived. Existing bytes, paths, and roles are
    immutable; a changed file must be added as a new member.
    """

    root = _project_root(project_root)
    clean_title = str(title or "").strip()
    if not clean_title:
        raise FormalAssetRepositoryError("Formal asset package title is required")
    package_state = _validate_state(lifecycle_state, label="package lifecycle_state")
    index, index_raw = _load_index(root)
    base_index_sha256 = _sha256_bytes(index_raw)
    timestamp = _now_iso()

    existing_manifest: dict[str, Any] | None = None
    base_manifest_sha256 = ""
    requested_package_id = _validate_package_id(package_id) if package_id is not None else None
    requested_entry = _index_entry(index, requested_package_id) if requested_package_id else None
    if requested_entry is None:
        resolved_package_id = requested_package_id or _next_package_id(root, index)
        resolved_slug = _slugify(slug or clean_title)
        package_directory = f"{FORMAL_ROOT_NAME}/{resolved_package_id}-{resolved_slug}"
        conflicting_directories = [
            path
            for path in _formal_root(root).glob(f"{resolved_package_id}-*")
            if path.exists()
        ] if _formal_root(root).is_dir() else []
        if conflicting_directories:
            raise FormalAssetRepositoryError(
                f"Formal asset package id already has an unindexed directory: {resolved_package_id}"
            )
        if (root / Path(package_directory)).exists():
            raise FormalAssetRepositoryError(f"Formal asset package directory already exists: {package_directory}")
        created_at = timestamp
    else:
        resolved_package_id = requested_package_id or ""
        entry = requested_entry
        manifest_path = _manifest_path_from_entry(root, entry)
        existing_manifest = _read_json(manifest_path)
        _validate_manifest_shape(existing_manifest)
        if existing_manifest.get("package_id") != resolved_package_id:
            raise FormalAssetRepositoryError(f"Package manifest identity mismatch: {resolved_package_id}")
        base_manifest_sha256 = _sha256_file(manifest_path)
        if entry.get("manifest_sha256") != base_manifest_sha256:
            raise FormalAssetRepositoryError(f"Package index manifest hash is stale: {resolved_package_id}")
        resolved_slug = str(existing_manifest["slug"])
        if slug is not None and _slugify(slug) != resolved_slug:
            raise FormalAssetRepositoryError(f"Package slug is immutable for {resolved_package_id}")
        package_directory = str(existing_manifest["directory"])
        created_at = str(existing_manifest["created_at"])
        previous_state = str(existing_manifest["lifecycle_state"])
        if not _allowed_transition(previous_state, package_state):
            raise FormalAssetRepositoryError(
                f"Invalid package lifecycle transition: {previous_state} -> {package_state}"
            )

    built_members, sources = _build_members(
        root,
        package_directory,
        existing_manifest,
        members,
        timestamp,
    )
    built_lineage = _build_lineage(built_members, existing_manifest, lineage)
    receipt_id = _next_receipt_id(existing_manifest)
    revision = int((existing_manifest or {}).get("revision") or 0) + 1
    receipt_relative = f"{package_directory}/receipts/{receipt_id}.json"
    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "project_id": str(index.get("project_id") or root.name),
        "package_id": resolved_package_id,
        "slug": resolved_slug,
        "title": clean_title,
        "lifecycle_state": package_state,
        "revision": revision,
        "directory": package_directory,
        "created_at": created_at,
        "updated_at": timestamp,
        "members": built_members,
        "current": _current_projection(built_members),
        "lineage": built_lineage,
        "latest_receipt": receipt_relative,
    }
    existing_metadata = (existing_manifest or {}).get("metadata")
    if existing_metadata is not None or metadata is not None:
        selected_metadata = metadata if metadata is not None else existing_metadata
        if not isinstance(selected_metadata, Mapping):
            raise FormalAssetRepositoryError("Formal asset package metadata must be an object")
        try:
            manifest["metadata"] = json.loads(json.dumps(selected_metadata, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise FormalAssetRepositoryError("Formal asset package metadata must be JSON serializable") from exc
    _validate_manifest_shape(manifest)
    manifest_bytes = _json_bytes(manifest)
    snapshot_relative = f"{package_directory}/manifests/{receipt_id}.json"

    index_packages = [
        dict(item)
        for item in index.get("packages", [])
        if isinstance(item, dict) and item.get("package_id") != resolved_package_id
    ]
    index_packages.append(_build_index_entry(manifest, manifest_bytes))
    index_packages.sort(key=lambda item: _package_number(str(item["package_id"])))
    project_index = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "project_id": str(index.get("project_id") or root.name),
        "updated_at": timestamp,
        "packages": index_packages,
    }
    index_bytes = _json_bytes(project_index)
    base_project_manifest_sha256, project_manifest = _project_manifest_projection(
        root,
        project_index,
        timestamp,
    )

    receipt_files = [
        {
            "path": f"{package_directory}/{PACKAGE_MANIFEST_NAME}",
            "sha256": _sha256_bytes(manifest_bytes),
            "size_bytes": len(manifest_bytes),
            "role": "package_manifest",
        },
        {
            "path": snapshot_relative,
            "sha256": _sha256_bytes(manifest_bytes),
            "size_bytes": len(manifest_bytes),
            "role": "package_manifest_snapshot",
        }
    ]
    receipt_files.extend(
        _repository_history_files(root, package_directory, receipt_id)
    )
    receipt_files.extend(
        {
            "path": str(item["path"]),
            "sha256": str(item["sha256"]),
            "size_bytes": int(item["size_bytes"]),
            "role": f"member:{item['role']}",
        }
        for item in built_members
    )
    receipt_files.append(
        {
            "path": f"{FORMAL_ROOT_NAME}/{INDEX_NAME}",
            "sha256": _sha256_bytes(index_bytes),
            "size_bytes": len(index_bytes),
            "role": "project_index",
        }
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "project_id": str(index.get("project_id") or root.name),
        "package_id": resolved_package_id,
        "package_revision": revision,
        "status": "ready",
        "created_at": timestamp,
        "package_manifest_path": snapshot_relative,
        "project_index_path": f"{FORMAL_ROOT_NAME}/{INDEX_NAME}",
        "project_index_sha256": _sha256_bytes(index_bytes),
        "files": sorted(receipt_files, key=lambda item: str(item["path"])),
    }

    return FormalAssetPlan(
        project_root=root,
        package_id=resolved_package_id,
        package_directory=package_directory,
        receipt_id=receipt_id,
        base_index_sha256=base_index_sha256,
        base_manifest_sha256=base_manifest_sha256,
        base_project_manifest_sha256=base_project_manifest_sha256,
        manifest=manifest,
        project_index=project_index,
        project_manifest=project_manifest,
        receipt=receipt,
        member_sources=sources,
    )


def _verify_plan_is_current(plan: FormalAssetPlan) -> None:
    _, index_raw = _load_index(plan.project_root)
    if _sha256_bytes(index_raw) != plan.base_index_sha256:
        raise StalePlanError("Formal asset repository index changed after planning")
    final_package = plan.project_root / Path(plan.package_directory)
    manifest_path = final_package / PACKAGE_MANIFEST_NAME
    if plan.base_manifest_sha256:
        if not manifest_path.is_file() or _sha256_file(manifest_path) != plan.base_manifest_sha256:
            raise StalePlanError(f"Formal asset package changed after planning: {plan.package_id}")
    elif final_package.exists():
        raise StalePlanError(f"Formal asset package directory appeared after planning: {plan.package_id}")
    if plan.project_manifest is not None:
        project_manifest_path = plan.project_root / "manifest.json"
        if (
            not project_manifest_path.is_file()
            or _sha256_file(project_manifest_path) != plan.base_project_manifest_sha256
        ):
            raise StalePlanError("Compact project manifest changed after planning")
    for source in plan.member_sources:
        if (
            not source.source_path.is_file()
            or source.source_path.stat().st_size != source.size_bytes
            or _sha256_file(source.source_path) != source.sha256
        ):
            raise StalePlanError(f"Formal asset member source changed after planning: {source.source_path}")


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def apply_plan(plan: FormalAssetPlan) -> dict[str, Any]:
    """Apply a planned package and compact index as one rollback transaction."""

    if not isinstance(plan, FormalAssetPlan):
        raise FormalAssetRepositoryError("apply_plan requires a FormalAssetPlan")
    _verify_plan_is_current(plan)
    root = plan.project_root
    formal_root = _formal_root(root)
    formal_root.mkdir(parents=True, exist_ok=True)
    transaction_id = uuid.uuid4().hex
    staging_root = formal_root / f".staging-{transaction_id}"
    staged_package = staging_root / Path(plan.package_directory).name
    final_package = root / Path(plan.package_directory)
    backup_package = formal_root / f".{final_package.name}.{transaction_id}.backup"
    index_path = _index_path(root)
    index_temp = formal_root / f".{INDEX_NAME}.{transaction_id}.tmp"
    original_index = index_path.read_bytes() if index_path.is_file() else None
    project_manifest_path = root / "manifest.json"
    project_manifest_temp = root / f".manifest.{transaction_id}.tmp"
    original_project_manifest = (
        project_manifest_path.read_bytes() if plan.project_manifest is not None else None
    )
    package_swapped = False
    old_package_moved = False
    project_manifest_published = False

    try:
        staging_root.mkdir(parents=True, exist_ok=False)
        if final_package.is_dir():
            shutil.copytree(final_package, staged_package)
        else:
            staged_package.mkdir(parents=True)

        for source in plan.member_sources:
            destination = _staged_path(staging_root, source.destination_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source.source_path, destination)

        manifest_bytes = _json_bytes(plan.manifest)
        manifest_path = staged_package / PACKAGE_MANIFEST_NAME
        manifest_path.write_bytes(manifest_bytes)
        snapshot_path = staged_package / "manifests" / f"{plan.receipt_id}.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(manifest_bytes)
        receipt_path = staged_package / "receipts" / f"{plan.receipt_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_bytes(_json_bytes(plan.receipt))

        index_bytes = _json_bytes(plan.project_index)
        for item in plan.receipt["files"]:
            if item.get("role") == "project_index":
                if len(index_bytes) != item["size_bytes"] or _sha256_bytes(index_bytes) != item["sha256"]:
                    raise FormalAssetRepositoryError("Planned project index does not match receipt file entry")
                continue
            relative = _ensure_formal_relative(str(item["path"]), label="receipt file path")
            staged_file = _staged_path(staging_root, relative)
            if not staged_file.is_file():
                raise FormalAssetRepositoryError(f"Planned receipt file was not staged: {relative}")
            if staged_file.stat().st_size != item["size_bytes"] or _sha256_file(staged_file) != item["sha256"]:
                raise FormalAssetRepositoryError(f"Planned receipt file does not match staged bytes: {relative}")

        if _sha256_bytes(index_bytes) != plan.receipt["project_index_sha256"]:
            raise FormalAssetRepositoryError("Planned project index hash does not match receipt")
        index_temp.write_bytes(index_bytes)
        if plan.project_manifest is not None:
            if plan.project_manifest.get("packages") != plan.project_index.get("packages"):
                raise FormalAssetRepositoryError(
                    "Compact project manifest does not match the planned repository index"
                )
            project_manifest_temp.write_bytes(_json_bytes(plan.project_manifest))

        if final_package.exists():
            _replace(final_package, backup_package)
            old_package_moved = True
        _replace(staged_package, final_package)
        package_swapped = True
        _replace(index_temp, index_path)
        if plan.project_manifest is not None:
            _replace(project_manifest_temp, project_manifest_path)
            project_manifest_published = True
    except Exception:
        index_temp.unlink(missing_ok=True)
        project_manifest_temp.unlink(missing_ok=True)
        if package_swapped:
            _remove_tree(final_package)
        if old_package_moved and backup_package.exists():
            _replace_path(backup_package, final_package)
        if original_index is None:
            index_path.unlink(missing_ok=True)
        elif not index_path.is_file() or index_path.read_bytes() != original_index:
            restore = formal_root / f".{INDEX_NAME}.{transaction_id}.restore"
            restore.write_bytes(original_index)
            _replace_path(restore, index_path)
        if plan.project_manifest is not None and (
            project_manifest_published
            or not project_manifest_path.is_file()
            or project_manifest_path.read_bytes() != original_project_manifest
        ):
            restore = root / f".manifest.{transaction_id}.restore"
            restore.write_bytes(original_project_manifest or b"")
            _replace_path(restore, project_manifest_path)
        _remove_tree(staging_root)
        raise
    else:
        _remove_tree(backup_package)
        _remove_tree(staging_root)
        index_temp.unlink(missing_ok=True)
        project_manifest_temp.unlink(missing_ok=True)
    return json.loads(json.dumps(plan.receipt))


def load_package(project_root: str | Path, package_id: str) -> dict[str, Any]:
    root = _project_root(project_root)
    resolved_id = _validate_package_id(package_id)
    index, _ = _load_index(root)
    entry = _index_entry(index, resolved_id)
    if entry is None:
        raise FormalAssetRepositoryError(f"Formal asset package is not indexed: {resolved_id}")
    manifest_path = _manifest_path_from_entry(root, entry)
    manifest = _read_json(manifest_path)
    _validate_manifest_shape(manifest)
    actual_hash = _sha256_file(manifest_path)
    if entry.get("manifest_sha256") != actual_hash:
        raise FormalAssetRepositoryError(f"Formal asset package manifest hash mismatch: {resolved_id}")
    return manifest


def list_packages(
    project_root: str | Path,
    *,
    lifecycle_states: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    root = _project_root(project_root)
    index, _ = _load_index(root)
    selected_states = None
    if lifecycle_states is not None:
        selected_states = {
            _validate_state(value, label="package lifecycle state filter") for value in lifecycle_states
        }
    packages = [
        dict(item)
        for item in index.get("packages", [])
        if isinstance(item, dict)
        and (selected_states is None or item.get("lifecycle_state") in selected_states)
    ]
    return sorted(packages, key=lambda item: _package_number(str(item["package_id"])))


def _package_directory(project_root: Path, package_id: str) -> Path:
    index, _ = _load_index(project_root)
    entry = _index_entry(index, _validate_package_id(package_id))
    if entry is None:
        raise FormalAssetRepositoryError(f"Formal asset package is not indexed: {package_id}")
    relative = _ensure_formal_relative(str(entry.get("directory") or ""), label="package directory")
    return project_root / Path(relative)


def list_receipts(project_root: str | Path, package_id: str) -> list[dict[str, Any]]:
    root = _project_root(project_root)
    receipts_root = _package_directory(root, package_id) / "receipts"
    if not receipts_root.is_dir():
        return []
    receipts: list[dict[str, Any]] = []
    for path in sorted(receipts_root.glob("R*.json")):
        receipt = _read_json(path)
        if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
            raise ReceiptValidationError(f"Unsupported formal asset receipt: {path}")
        receipts.append(receipt)
    return sorted(receipts, key=lambda item: int(RECEIPT_ID_RE.fullmatch(str(item["receipt_id"])).group(1)))


def load_receipt(
    project_root: str | Path,
    package_id: str,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    root = _project_root(project_root)
    manifest = load_package(root, package_id)
    resolved_receipt_id = receipt_id
    if resolved_receipt_id is None:
        resolved_receipt_id = Path(str(manifest["latest_receipt"])).stem
    if not RECEIPT_ID_RE.fullmatch(str(resolved_receipt_id or "")):
        raise ReceiptValidationError(f"Invalid formal asset receipt id: {resolved_receipt_id}")
    path = _package_directory(root, package_id) / "receipts" / f"{resolved_receipt_id}.json"
    if not path.is_file():
        raise ReceiptValidationError(f"Formal asset receipt does not exist: {path}")
    receipt = _read_json(path)
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ReceiptValidationError(f"Unsupported formal asset receipt: {path}")
    if receipt.get("package_id") != _validate_package_id(package_id):
        raise ReceiptValidationError(f"Formal asset receipt package mismatch: {path}")
    return receipt


def validate_receipt(
    project_root: str | Path,
    receipt_or_path: Mapping[str, Any] | str | Path,
) -> dict[str, Any]:
    root = _project_root(project_root)
    if isinstance(receipt_or_path, Mapping):
        receipt = dict(receipt_or_path)
    else:
        relative = _ensure_formal_relative(receipt_or_path, label="receipt path")
        receipt = _read_json(root / Path(relative))

    problems: list[str] = []
    advanced_paths: list[str] = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        problems.append("unsupported schema_version")
    if not RECEIPT_ID_RE.fullmatch(str(receipt.get("receipt_id") or "")):
        problems.append("invalid receipt_id")
    try:
        _validate_package_id(str(receipt.get("package_id") or ""))
    except FormalAssetRepositoryError as exc:
        problems.append(str(exc))
    if receipt.get("status") != "ready":
        problems.append("receipt status is not ready")
    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        problems.append("receipt files must be a non-empty array")
        files = []
    is_latest = False
    try:
        current_manifest = load_package(root, str(receipt.get("package_id") or ""))
        is_latest = (
            int(current_manifest.get("revision") or 0) == int(receipt.get("package_revision") or -1)
            and Path(str(current_manifest.get("latest_receipt") or "")).stem
            == str(receipt.get("receipt_id") or "")
        )
    except (FormalAssetRepositoryError, TypeError, ValueError):
        current_manifest = None
    receipt_manifest_hash = next(
        (
            str(item.get("sha256") or "")
            for item in files
            if isinstance(item, dict) and item.get("role") == "package_manifest"
        ),
        "",
    )
    index_entry: dict[str, Any] | None = None
    try:
        current_index, _ = _load_index(root)
        index_entry = _index_entry(current_index, str(receipt.get("package_id") or ""))
    except FormalAssetRepositoryError:
        index_entry = None
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            problems.append("receipt file entry is not an object")
            continue
        try:
            relative = _ensure_formal_relative(str(item.get("path") or ""), label="receipt file path")
        except FormalAssetRepositoryError as exc:
            problems.append(str(exc))
            continue
        if relative in seen:
            problems.append(f"duplicate receipt file path: {relative}")
            continue
        seen.add(relative)
        path = root / Path(relative)
        if not path.is_file():
            problems.append(f"missing receipt file: {relative}")
            continue
        expected_size = item.get("size_bytes")
        expected_hash = str(item.get("sha256") or "")
        if not isinstance(expected_size, int) or expected_size < 0:
            problems.append(f"invalid size in receipt: {relative}")
            continue
        if not SHA256_RE.fullmatch(expected_hash):
            problems.append(f"invalid hash in receipt: {relative}")
            continue
        size_matches = path.stat().st_size == expected_size
        hash_matches = size_matches and _sha256_file(path) == expected_hash
        if not size_matches or not hash_matches:
            if item.get("role") == "project_index":
                indexed_revision = int((index_entry or {}).get("revision") or 0)
                receipt_revision = int(receipt.get("package_revision") or 0)
                same_receipt_snapshot = bool(
                    index_entry
                    and indexed_revision == receipt_revision
                    and index_entry.get("manifest_sha256") == receipt_manifest_hash
                    and Path(str(index_entry.get("latest_receipt") or "")).stem
                    == str(receipt.get("receipt_id") or "")
                )
                valid_later_revision = bool(index_entry and indexed_revision > receipt_revision and not is_latest)
                if same_receipt_snapshot or valid_later_revision:
                    advanced_paths.append(relative)
                elif not index_entry:
                    problems.append(f"project index no longer contains package: {receipt.get('package_id')}")
                else:
                    problems.append(f"project index package entry does not match receipt: {receipt.get('package_id')}")
            elif not is_latest and item.get("role") == "package_manifest":
                advanced_paths.append(relative)
            elif not size_matches:
                problems.append(f"size mismatch: {relative}")
            else:
                problems.append(f"sha256 mismatch: {relative}")
    manifest_path = str(receipt.get("package_manifest_path") or "")
    if manifest_path and manifest_path not in seen:
        problems.append("package_manifest_path is not enumerated in receipt files")
    return {
        "schema_version": "formal_asset_receipt_validation_v1",
        "status": "valid" if not problems else "invalid",
        "package_id": str(receipt.get("package_id") or ""),
        "project_id": str(receipt.get("project_id") or ""),
        "receipt_id": str(receipt.get("receipt_id") or ""),
        "checked_file_count": len(seen),
        "advanced_paths": advanced_paths,
        "problems": problems,
    }


__all__ = [
    "FormalAssetPlan",
    "FormalAssetRepositoryError",
    "ReceiptValidationError",
    "StalePlanError",
    "apply_plan",
    "list_packages",
    "list_receipts",
    "load_package",
    "load_receipt",
    "plan_package",
    "validate_receipt",
]
