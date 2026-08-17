#!/usr/bin/env python3
"""Build and validate the read-only Asset Provider Snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from formal_asset_repository import list_packages, load_package, load_receipt, validate_receipt
from asset_catalog import validate_catalog
from asset_group_registry import validate_registry
from asset_organization import validate_organization


SCHEMA_VERSION = "asset_provider_snapshot_v1"
MANIFEST_SCHEMA_VERSION = "asset_provider_manifest_v1"
DEFAULT_OUTPUT_RELATIVE = Path("_asset_catalog") / "provider_snapshot.json"
DEFAULT_MANIFEST_RELATIVE = Path("_asset_catalog") / "provider_manifest.json"
FORBIDDEN_PARTS = {
    "query_workspace",
    "promotion_ledger.json",
    "unregistered_inventory.json",
    "credentials",
    "cache",
    ".local",
    ".tmp",
}
SUPPORTED_CONTRACT_VERSIONS = {
    "asset_catalog": "sql_asset_catalog_v2",
    "asset_organization": "sql_asset_organization_v2",
    "asset_group_registry": "sql_asset_group_registry_v2",
    "formal_asset_package": "formal_asset_package_v1",
    "asset_provider_snapshot": SCHEMA_VERSION,
}


class AssetProviderError(ValueError):
    """Raised when the Provider Snapshot cannot prove a clean shared closure."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise AssetProviderError(f"Provider path escaped repository root: {path}") from exc


def _file_row(repo_root: Path, path: Path, *, role: str) -> dict[str, Any]:
    if not path.is_file():
        raise AssetProviderError(f"Provider file is missing: {path}")
    relative = _repo_relative(repo_root, path)
    _validate_relative(relative)
    return {
        "path": relative,
        "role": role,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetProviderError(f"Provider input is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise AssetProviderError(f"Provider input must be an object: {path}")
    return value


def _validate_shared_projections(repo_root: Path, projects_root: Path) -> None:
    output_root = projects_root / "_asset_catalog"
    catalog_path = output_root / "asset_catalog.json"
    organization_path = output_root / "asset_organization.json"
    registry_path = output_root / "asset_group_registry.json"
    refresh_path = output_root / "refresh_receipt.json"
    catalog = _read_json(catalog_path)
    organization = _read_json(organization_path)
    registry = _read_json(registry_path)
    problems = [
        *validate_catalog(catalog, repo_root),
        *validate_organization(organization, catalog),
        *validate_registry(registry, catalog, organization),
    ]
    if problems:
        raise AssetProviderError("Shared projections are invalid: " + "; ".join(problems[:12]))
    refresh = _read_json(refresh_path)
    if refresh.get("schema_version") != "shared_asset_read_models_refresh_v1" or refresh.get("status") != "ready":
        raise AssetProviderError("Shared refresh receipt is missing or not ready.")
    receipt_files = refresh.get("files")
    if not isinstance(receipt_files, list):
        raise AssetProviderError("Shared refresh receipt files must be an array.")
    expected = {
        "asset_catalog.json": catalog_path,
        "asset_organization.json": organization_path,
        "asset_group_registry.json": registry_path,
    }
    seen: set[str] = set()
    for row in receipt_files:
        if not isinstance(row, dict):
            raise AssetProviderError("Shared refresh receipt contains a malformed file row.")
        relative = str(row.get("path") or "").replace("\\", "/")
        _validate_relative(relative)
        name = Path(relative).name
        path = expected.get(name)
        if path is None or name in seen:
            raise AssetProviderError(f"Shared refresh receipt contains an unexpected file: {relative}")
        if relative != _repo_relative(repo_root, path):
            raise AssetProviderError(f"Shared refresh receipt path is not canonical: {relative}")
        seen.add(name)
        if _sha256(path) != str(row.get("sha256") or ""):
            raise AssetProviderError(f"Shared projection is stale relative to refresh receipt: {relative}")
    if seen != set(expected):
        raise AssetProviderError("Shared refresh receipt does not cover every projection.")


def _write_pair_atomic(payloads: list[tuple[Path, bytes]]) -> None:
    temporary_paths: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path, bool]] = []
    try:
        for destination, content in payloads:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            temporary.write_bytes(content)
            backup = destination.with_name(f".{destination.name}.{os.getpid()}.bak")
            existed = destination.exists()
            if existed:
                backup.write_bytes(destination.read_bytes())
            temporary_paths.append((temporary, destination))
            backups.append((backup, destination, existed))
        for temporary, destination in temporary_paths:
            os.replace(temporary, destination)
    except Exception:
        for backup, destination, existed in reversed(backups):
            if existed and backup.exists():
                os.replace(backup, destination)
            elif not existed and destination.exists():
                destination.unlink()
        raise
    finally:
        for temporary, _ in temporary_paths:
            if temporary.exists():
                temporary.unlink()
        for backup, _, _ in backups:
            if backup.exists():
                backup.unlink()


def _validate_relative(path: str) -> None:
    normalized = str(path or "").replace("\\", "/")
    parsed = PurePosixPath(normalized)
    if not normalized or parsed.is_absolute() or ".." in parsed.parts:
        raise AssetProviderError(f"Provider path must be repository relative: {path}")
    lowered = normalized.lower()
    if any(part in lowered or part in {item.lower() for item in parsed.parts} for part in FORBIDDEN_PARTS):
        raise AssetProviderError(f"Provider Snapshot contains forbidden local surface: {path}")


def _shared_projection_files(projects_root: Path) -> list[tuple[Path, str]]:
    output_root = projects_root / "_asset_catalog"
    return [
        (output_root / "asset_catalog.json", "asset_catalog"),
        (output_root / "asset_organization.json", "asset_organization"),
        (output_root / "asset_group_registry.json", "asset_group_registry"),
        (output_root / "refresh_receipt.json", "shared_read_model_receipt"),
    ]


def _contract_files(repo_root: Path) -> list[tuple[Path, str]]:
    return [
        (repo_root / "sql-engineering" / "schemas" / "asset_catalog.json", "contract_schema"),
        (repo_root / "sql-engineering" / "schemas" / "asset_group_registry.json", "contract_schema"),
        (repo_root / "sql-engineering" / "schemas" / "asset_organization.json", "contract_schema"),
        (repo_root / "sql-engineering" / "schemas" / "formal_asset_package.json", "contract_schema"),
        (repo_root / "sql-engineering" / "schemas" / "asset_provider_snapshot.json", "contract_schema"),
        (repo_root / "sql-engineering" / "schemas" / "asset_provider_manifest.json", "contract_schema"),
        (repo_root / "docs" / "READONLY_ASSET_CONSUMER_GUIDE.md", "consumer_contract"),
    ]


def build_snapshot(
    projects_root: str | Path,
    *,
    repository_id: str,
    output_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    projects = Path(projects_root).resolve()
    repo_root = projects.parent
    if not projects.is_dir():
        raise AssetProviderError(f"Projects root does not exist: {projects}")
    clean_repository_id = str(repository_id or "").strip()
    if not clean_repository_id or "\\" in clean_repository_id or "/" not in clean_repository_id:
        raise AssetProviderError("repository_id must be a stable slash-separated identity.")
    _validate_shared_projections(repo_root, projects)
    files: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    project_rows: list[dict[str, Any]] = []
    for project_root in sorted(projects.iterdir()):
        if not project_root.is_dir() or project_root.name.startswith("_"):
            continue
        config_path = project_root / "project_config.json"
        formal_root = project_root / "formal_assets"
        if not config_path.is_file() or not formal_root.is_dir():
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssetProviderError(f"Project config is unreadable: {config_path}") from exc
        project_id = str(config.get("project_id") or project_root.name) if isinstance(config, dict) else project_root.name
        project_rows.append({"project_id": project_id, "project_path": _repo_relative(repo_root, project_root)})
        for package_entry in list_packages(project_root):
            package_id = str(package_entry.get("package_id") or "")
            manifest = load_package(project_root, package_id)
            receipt = load_receipt(project_root, package_id)
            validation = validate_receipt(project_root, receipt)
            if validation.get("status") != "valid":
                raise AssetProviderError(f"Formal Package receipt is invalid: {project_id}/{package_id}")
            package_directory = project_root / Path(str(manifest.get("directory") or ""))
            package_files = []
            for path in sorted(package_directory.rglob("*")):
                if path.is_file():
                    row = _file_row(repo_root, path, role="formal_asset_package_file")
                    package_files.append(row["path"])
                    files.append(row)
            receipt_paths = {
                str(item.get("path") or "").replace("\\", "/")
                for item in receipt.get("files", [])
                if isinstance(item, dict)
            }
            actual_package_paths = {
                path.relative_to(project_root).as_posix()
                for path in package_directory.rglob("*")
                if path.is_file()
            }
            package_prefix = Path(str(manifest.get("directory") or "")).as_posix().rstrip("/") + "/"
            expected_package_paths = {path for path in receipt_paths if path.startswith(package_prefix)}
            receipt_path = str(package_entry.get("latest_receipt") or "").replace("\\", "/")
            if receipt_path:
                expected_package_paths.add(receipt_path)
            if expected_package_paths != actual_package_paths:
                raise AssetProviderError(
                    f"Formal Package closure differs from its receipt: {project_id}/{package_id}"
                )
            assets.append(
                {
                    "asset_id": package_id,
                    "project_id": project_id,
                    "package_id": package_id,
                    "manifest_path": _repo_relative(repo_root, package_directory / "manifest.json"),
                    "receipt_id": str(receipt.get("receipt_id") or ""),
                    "files": package_files,
                }
            )
        index_path = formal_root / "index.json"
        if index_path.is_file():
            files.append(_file_row(repo_root, index_path, role="formal_asset_project_index"))
    for path, role in _shared_projection_files(projects):
        files.append(_file_row(repo_root, path, role=role))
    for path, role in _contract_files(repo_root):
        files.append(_file_row(repo_root, path, role=role))
    files.sort(key=lambda item: str(item.get("path") or ""))
    assets.sort(key=lambda item: (str(item.get("project_id") or ""), str(item.get("asset_id") or "")))
    generated_at = now_iso()
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "source_model": "formal_asset_packages_v1",
        "repository_id": clean_repository_id,
        "projects": project_rows,
        "assets": assets,
        "files": files,
        "consumer_contract": {
            "access_mode": "read_only",
            "identity": ["repository_id", "project_id", "asset_id"],
            "pin": ["git_commit", "snapshot_digest"],
            "workspace_access": "forbidden",
        },
        "supported_contract_versions": SUPPORTED_CONTRACT_VERSIONS,
        "entry_points": {
            "catalog": "sql-projects/_asset_catalog/asset_catalog.json",
            "organization": "sql-projects/_asset_catalog/asset_organization.json",
            "group_registry": "sql-projects/_asset_catalog/asset_group_registry.json",
            "formal_packages": "sql-projects/<PROJECT>/formal_assets/<PACKAGE>/manifest.json",
        },
        "forbidden_local_surfaces": sorted(FORBIDDEN_PARTS),
    }
    snapshot = {
        **unsigned,
        "generated_at": generated_at,
        "snapshot_digest": hashlib.sha256(_canonical(unsigned)).hexdigest(),
    }
    output = Path(output_path).resolve() if output_path else projects / DEFAULT_OUTPUT_RELATIVE
    manifest_output = Path(manifest_path).resolve() if manifest_path else projects / DEFAULT_MANIFEST_RELATIVE
    output_relative = _repo_relative(repo_root, output)
    manifest_relative = _repo_relative(repo_root, manifest_output)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "repository_id": clean_repository_id,
        "snapshot_path": output_relative,
        "snapshot_digest": snapshot["snapshot_digest"],
        "project_ids": [item["project_id"] for item in project_rows],
        "asset_count": len(assets),
        "generated_at": generated_at,
        "supported_contract_versions": SUPPORTED_CONTRACT_VERSIONS,
        "entry_points": unsigned["entry_points"],
        "consumer_contract": unsigned["consumer_contract"],
    }
    _write_pair_atomic(
        [(output, _canonical(snapshot)), (manifest_output, _canonical(manifest))]
    )
    return {
        "status": "ready",
        "repository_id": clean_repository_id,
        "snapshot_path": _repo_relative(repo_root, output),
        "manifest_path": manifest_relative,
        "snapshot_digest": snapshot["snapshot_digest"],
        "project_count": len(project_rows),
        "asset_count": len(assets),
        "file_count": len(files),
    }


def validate_snapshot(path: str | Path, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    snapshot_path = Path(path).resolve()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetProviderError(f"Provider Snapshot is unreadable: {snapshot_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise AssetProviderError("Unsupported Provider Snapshot schema.")
    unsigned = {key: value for key, value in payload.items() if key not in {"generated_at", "snapshot_digest"}}
    expected_digest = hashlib.sha256(_canonical(unsigned)).hexdigest()
    if payload.get("snapshot_digest") != expected_digest:
        raise AssetProviderError("Provider Snapshot digest mismatch.")
    root = Path(repo_root).resolve() if repo_root else None
    manifest_path = snapshot_path.parent / "provider_manifest.json"
    if manifest_path.is_file():
        manifest = _read_json(manifest_path)
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise AssetProviderError("Provider manifest schema is unsupported.")
        if manifest.get("snapshot_digest") != payload.get("snapshot_digest"):
            raise AssetProviderError("Provider manifest and snapshot digests differ.")
        if manifest.get("repository_id") != payload.get("repository_id"):
            raise AssetProviderError("Provider manifest and snapshot identities differ.")
        if not isinstance(manifest.get("supported_contract_versions"), dict) or not isinstance(manifest.get("entry_points"), dict):
            raise AssetProviderError("Provider manifest is missing its versioned contract and entry points.")
        _validate_relative(str(manifest.get("snapshot_path") or ""))
    identities = [
        (
            str(item.get("project_id") or ""),
            str(item.get("asset_id") or ""),
        )
        for item in payload.get("assets", [])
        if isinstance(item, dict)
    ]
    if len(identities) != len(set(identities)):
        raise AssetProviderError("Provider Snapshot asset identities must be unique per project.")
    for row in payload.get("files", []) if isinstance(payload.get("files"), list) else []:
        if not isinstance(row, dict):
            raise AssetProviderError("Provider Snapshot files must be objects.")
        relative = str(row.get("path") or "")
        _validate_relative(relative)
        if root:
            file_path = root / Path(relative)
            if not file_path.is_file() or _sha256(file_path) != str(row.get("sha256") or ""):
                raise AssetProviderError(f"Provider Snapshot file drifted: {relative}")
    return {
        "status": "valid",
        "repository_id": str(payload.get("repository_id") or ""),
        "snapshot_digest": str(payload.get("snapshot_digest") or ""),
        "project_count": len(payload.get("projects") or []),
        "asset_count": len(payload.get("assets") or []),
        "file_count": len(payload.get("files") or []),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--projects-root", required=True)
    build.add_argument("--repository-id", required=True)
    build.add_argument("--output", default="")
    build.add_argument("--manifest", default="")
    validate = sub.add_parser("validate")
    validate.add_argument("--snapshot", required=True)
    validate.add_argument("--repo-root", default="")
    args = parser.parse_args()
    if args.command == "build":
        result = build_snapshot(
            args.projects_root,
            repository_id=args.repository_id,
            output_path=args.output or None,
            manifest_path=args.manifest or None,
        )
    else:
        result = validate_snapshot(args.snapshot, repo_root=args.repo_root or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
