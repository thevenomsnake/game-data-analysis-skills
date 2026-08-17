#!/usr/bin/env python3
"""Deterministic source-provider helpers for managed planning releases."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit


SVN_SOURCE_SCHEMA = "svn_source_revision_v2"
LEGACY_SVN_SOURCE_SCHEMA = "svn_source_revision_v1"
SVN_SOURCE_SCHEMAS = {LEGACY_SVN_SOURCE_SCHEMA, SVN_SOURCE_SCHEMA}
SVN_CREDENTIAL_REF_SCHEMA = "planning_source_credential_ref_v1"
SVN_PROVIDER = "svn"
FOLDER_PROVIDER = "folder"
SUPPORTED_PROVIDERS = {SVN_PROVIDER, FOLDER_PROVIDER}
PREVIEW_LIMIT = 40


class PlanningSourceProviderError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_svn_url(value: str) -> str:
    text = value.strip().replace("\\", "/").rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"svn", "http", "https"} or not parsed.hostname:
        raise PlanningSourceProviderError(f"unsupported SVN URL: {value!r}")
    if parsed.username or parsed.password:
        raise PlanningSourceProviderError("SVN credentials must not be embedded in the source URL")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", ""))


def safe_relative(value: str) -> str:
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PlanningSourceProviderError(f"invalid planning-source relative path: {value}")
    return relative.as_posix()


def _svn_executable() -> str:
    executable = shutil.which("svn")
    if not executable:
        raise PlanningSourceProviderError("SVN command-line client is not installed or not on PATH")
    return executable


def _user_environment_value(name: str) -> str:
    value = os.environ.get(name, "")
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except (FileNotFoundError, OSError):
        return ""


def resolve_svn_auth(credential_ref: dict[str, Any] | None) -> dict[str, str] | None:
    if not credential_ref:
        return None
    if (
        credential_ref.get("contract_version") != SVN_CREDENTIAL_REF_SCHEMA
        or credential_ref.get("kind") != "environment_variable"
    ):
        raise PlanningSourceProviderError("unsupported planning-source credential reference")
    username = str(credential_ref.get("username") or "").strip()
    secret_env = str(credential_ref.get("secret_env") or "").strip()
    if not username or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", secret_env):
        raise PlanningSourceProviderError("planning-source credential reference is incomplete")
    password = _user_environment_value(secret_env)
    if not password:
        raise PlanningSourceProviderError(
            f"planning-source credential is unavailable in local secret reference: {secret_env}"
        )
    return {"username": username, "password": password}


def run_svn(*args: str, auth: dict[str, str] | None = None) -> str:
    command = [_svn_executable(), *args]
    stdin = None
    if auth:
        username = str(auth.get("username") or "").strip()
        password = str(auth.get("password") or "")
        if not username or not password:
            raise PlanningSourceProviderError("SVN authentication requires username and password")
        command.extend(
            ["--username", username, "--password-from-stdin", "--no-auth-cache"]
        )
        stdin = password + "\n"
    completed = subprocess.run(
        command,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown SVN failure").strip()
        raise PlanningSourceProviderError(f"SVN command failed: {detail}")
    return completed.stdout


def _xml(value: str, label: str) -> ET.Element:
    try:
        return ET.fromstring(value.lstrip("\ufeff"))
    except ET.ParseError as error:
        raise PlanningSourceProviderError(f"invalid SVN {label} XML: {error}") from error


def _info_payload(value: str) -> dict[str, Any]:
    root = _xml(value, "info")
    entry = root.find("entry")
    if entry is None or entry.get("kind") != "dir":
        raise PlanningSourceProviderError("SVN planning source must resolve to one directory")
    repository = entry.find("repository")
    source_url = canonical_svn_url(entry.findtext("url") or "")
    repository_root = canonical_svn_url(
        repository.findtext("root") if repository is not None else ""
    )
    repository_uuid = (repository.findtext("uuid") if repository is not None else "") or ""
    commit = entry.find("commit")
    content_revision = int(
        (commit.get("revision") if commit is not None else "")
        or entry.get("revision")
        or 0
    )
    repository_revision = int(entry.get("revision") or content_revision)
    if not repository_uuid or content_revision <= 0:
        raise PlanningSourceProviderError("SVN info is missing repository UUID or revision")
    return {
        "contract_version": SVN_SOURCE_SCHEMA,
        "provider": SVN_PROVIDER,
        "source_url": source_url,
        "repository_root": repository_root,
        "repository_uuid": repository_uuid,
        "revision": content_revision,
        "repository_revision": repository_revision,
        "last_changed_author": (commit.findtext("author") if commit is not None else "") or "",
        "last_changed_at": (commit.findtext("date") if commit is not None else "") or "",
        "working_copy_revision": int(entry.get("revision") or 0),
        "is_working_copy": entry.find("wc-info") is not None,
    }


def inspect_svn_url(
    source_url: str,
    revision: int | None = None,
    *,
    auth: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = canonical_svn_url(source_url)
    args = ["info", "--xml", "--non-interactive"]
    if revision is not None:
        args.extend(["--revision", str(int(revision))])
    args.append(url)
    payload = _info_payload(run_svn(*args, auth=auth))
    if payload["source_url"] != url:
        raise PlanningSourceProviderError(
            f"SVN source URL resolved unexpectedly: {payload['source_url']} != {url}"
        )
    payload["revision_selection"] = "remote_latest"
    return payload


def inspect_working_copy_status(path: Path) -> dict[str, Any]:
    root_path = path.resolve()
    root = _xml(
        run_svn("status", "--xml", "--ignore-externals", "--non-interactive", str(root_path)),
        "status",
    )
    changes: list[dict[str, Any]] = []
    for entry in root.findall(".//entry"):
        status = entry.find("wc-status")
        if status is None:
            continue
        item = str(status.get("item") or "")
        props = str(status.get("props") or "")
        switched = status.get("switched") == "true"
        tree_conflicted = status.get("tree-conflicted") == "true"
        if item in {"", "normal", "none", "external"} and props in {"", "normal", "none"} and not switched and not tree_conflicted:
            continue
        raw_path = Path(str(entry.get("path") or ""))
        try:
            relative = raw_path.resolve().relative_to(root_path).as_posix()
        except (OSError, ValueError):
            relative = str(entry.get("path") or "").replace("\\", "/")
        changes.append(
            {
                "relative_path": relative,
                "item": item,
                "properties": props,
                "switched": switched,
                "tree_conflicted": tree_conflicted,
            }
        )
    changes.sort(key=lambda item: str(item["relative_path"]).casefold())
    return {
        "clean": not changes,
        "change_count": len(changes),
        "changes": changes[:PREVIEW_LIMIT],
        "truncated": len(changes) > PREVIEW_LIMIT,
    }


def inspect_working_copy_revision(path: Path) -> dict[str, Any]:
    executable = shutil.which("svnversion")
    if not executable:
        raise PlanningSourceProviderError("svnversion is not installed or not on PATH")
    completed = subprocess.run(
        [executable, "--no-newline", str(path.resolve())],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown svnversion failure").strip()
        raise PlanningSourceProviderError(f"svnversion failed: {detail}")
    raw = completed.stdout.strip()
    match = re.fullmatch(r"([0-9]+)", raw)
    return {
        "raw": raw,
        "exact": bool(match),
        "revision": int(match.group(1)) if match else 0,
    }


def inspect_svn_working_copy(path: Path) -> dict[str, Any]:
    root_path = path.resolve()
    if not root_path.is_dir():
        raise PlanningSourceProviderError(f"SVN working copy is unavailable: {root_path}")
    local = _info_payload(run_svn("info", "--xml", "--non-interactive", str(root_path)))
    if not local["is_working_copy"]:
        raise PlanningSourceProviderError(f"path is not an SVN working copy: {root_path}")
    revision_state = inspect_working_copy_revision(root_path)
    selected_revision = int(revision_state.get("revision") or 0)
    return {
        **local,
        "revision": selected_revision,
        "repository_revision": int(local["working_copy_revision"]),
        "revision_selection": "working_copy_pinned",
        "working_copy_last_changed_revision": local["revision"],
        "working_copy_revision": local["working_copy_revision"],
        "working_copy_revision_state": revision_state,
        "working_copy_status": inspect_working_copy_status(root_path),
    }


def inspect_configured_svn(local_config: dict[str, Any]) -> dict[str, Any]:
    management_mode = str(local_config.get("management_mode") or "")
    source_path = str(local_config.get("source_path") or "").strip()
    svn = local_config.get("svn") if isinstance(local_config.get("svn"), dict) else {}
    if management_mode == "user_managed" and source_path and Path(source_path).is_dir():
        identity = inspect_svn_working_copy(Path(source_path))
    elif management_mode == "tool_managed":
        auth = resolve_svn_auth(
            local_config.get("credential_ref")
            if isinstance(local_config.get("credential_ref"), dict)
            else None
        )
        source_url = str(svn.get("source_url") or "")
        if not source_url:
            raise PlanningSourceProviderError("SVN source URL is not configured")
        identity = inspect_svn_url(source_url, auth=auth)
        identity["working_copy_status"] = {
            "clean": True,
            "change_count": 0,
            "changes": [],
            "truncated": False,
            "status": "not_configured",
        }
    else:
        raise PlanningSourceProviderError(
            "planning-source management mode requires explicit reconfiguration"
        )
    expected_uuid = str(svn.get("repository_uuid") or "")
    expected_url = str(svn.get("source_url") or "")
    if expected_uuid and identity["repository_uuid"] != expected_uuid:
        raise PlanningSourceProviderError("configured SVN repository UUID changed")
    if expected_url and identity["source_url"] != canonical_svn_url(expected_url):
        raise PlanningSourceProviderError("configured SVN source URL changed")
    return identity


def svn_externals(
    source_url: str,
    revision: int,
    *,
    auth: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    url = canonical_svn_url(source_url)
    root = _xml(
        run_svn(
            "propget",
            "svn:externals",
            "--xml",
            "--recursive",
            "--revision",
            str(int(revision)),
            url,
            auth=auth,
        ),
        "properties",
    )
    rows = []
    for target in root.findall("target"):
        for prop in target.findall("property"):
            if prop.get("name") == "svn:externals" and (prop.text or "").strip():
                rows.append(
                    {
                        "path": str(target.get("path") or ""),
                        "value": (prop.text or "").strip(),
                    }
                )
    return rows


def svn_changed_paths(
    source_url: str,
    from_revision: int,
    to_revision: int,
    *,
    auth: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    if int(from_revision) == int(to_revision):
        return {"added": [], "changed": [], "removed": []}
    url = canonical_svn_url(source_url)
    root = _xml(
        run_svn(
            "diff",
            "--summarize",
            "--xml",
            "--notice-ancestry",
            "--revision",
            f"{int(from_revision)}:{int(to_revision)}",
            url,
            auth=auth,
        ),
        "diff",
    )
    result: dict[str, list[str]] = {"added": [], "changed": [], "removed": []}
    for path_node in root.findall(".//path"):
        full = unquote((path_node.text or "").strip()).rstrip("/")
        prefix = unquote(url).rstrip("/")
        if full == prefix:
            relative = "."
        elif full.startswith(prefix + "/"):
            relative = safe_relative(full[len(prefix) + 1 :])
        else:
            continue
        item = str(path_node.get("item") or "modified")
        bucket = "added" if item == "added" else "removed" if item == "deleted" else "changed"
        result[bucket].append(relative)
    for bucket in result:
        result[bucket] = sorted(set(result[bucket]), key=str.casefold)
    return result


def export_svn_revision(
    source_url: str,
    revision: int,
    destination: Path,
    *,
    auth: dict[str, str] | None = None,
) -> None:
    destination = destination.resolve()
    if destination.exists():
        raise PlanningSourceProviderError(f"SVN export destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_svn(
        "export",
        "--non-interactive",
        "--ignore-externals",
        "--force",
        "--revision",
        str(int(revision)),
        canonical_svn_url(source_url),
        str(destination),
        auth=auth,
    )


def _svn_file_url(source_url: str, relative_file: str) -> str:
    relative = safe_relative(relative_file)
    return canonical_svn_url(source_url) + "/" + quote(relative, safe="/")


def materialize_svn_file(
    *,
    repo_root: Path,
    release_id: str,
    source_control: dict[str, Any],
    relative_file: str,
    expected_sha256: str,
    expected_size: int | None = None,
    auth: dict[str, str] | None = None,
) -> Path:
    if source_control.get("contract_version") not in SVN_SOURCE_SCHEMAS:
        raise PlanningSourceProviderError("unsupported SVN source-control contract")
    source_url = canonical_svn_url(str(source_control.get("source_url") or ""))
    repository_uuid = str(source_control.get("repository_uuid") or "")
    revision = int(source_control.get("revision") or 0)
    if not repository_uuid or revision <= 0:
        raise PlanningSourceProviderError("SVN source-control contract is incomplete")
    remote = inspect_svn_url(source_url, revision=revision, auth=auth)
    if remote["repository_uuid"] != repository_uuid:
        raise PlanningSourceProviderError("SVN repository UUID changed before file materialization")

    relative = safe_relative(relative_file)
    suffix = Path(relative).suffix.lower()
    filename = Path(relative).name
    cache_root = (repo_root / ".local" / "planning-source-materialized").resolve()
    target = (cache_root / release_id / expected_sha256[:12] / filename).resolve()
    try:
        target.relative_to(cache_root)
    except ValueError as error:
        raise PlanningSourceProviderError("SVN materialization target escapes local cache") from error
    if suffix and target.suffix.lower() != suffix:
        raise PlanningSourceProviderError("SVN materialization changed the source suffix")
    if target.is_file() and file_sha256(target) == expected_sha256:
        if expected_size is None or target.stat().st_size == int(expected_size):
            return target

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        run_svn(
            "export",
            "--non-interactive",
            "--ignore-externals",
            "--force",
            "--revision",
            str(revision),
            _svn_file_url(source_url, relative),
            str(temporary),
            auth=auth,
        )
        if not temporary.is_file():
            raise PlanningSourceProviderError(f"SVN did not export the requested file: {relative}")
        actual_hash = file_sha256(temporary)
        actual_size = temporary.stat().st_size
        if actual_hash != expected_sha256:
            raise PlanningSourceProviderError(f"SVN source file hash mismatch: {relative}")
        if expected_size is not None and actual_size != int(expected_size):
            raise PlanningSourceProviderError(f"SVN source file size mismatch: {relative}")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
