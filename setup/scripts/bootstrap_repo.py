#!/usr/bin/env python3
"""Initialize a local checkout of the public Game Data Analysis Skills repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_REMOTE = "https://github.com/thevenomsnake/game-data-analysis-skills.git"
SETUP_CONFIG_SCHEMA = "public_setup_config_v1"
SETUP_CONFIG_REL = Path(".local") / "setup-config.json"
PLANNING_PROVIDERS = {"none", "git", "svn", "local"}
GIT_PROVIDERS = {"auto", "github", "gitlab", "self_hosted", "local"}


class BootstrapError(RuntimeError):
    pass


def run(root: Path, *args: str, check: bool = True) -> str:
    if not shutil.which("git"):
        raise BootstrapError("Git is required and must be available on PATH")
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=dict(os.environ),
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise BootstrapError(f"git {args[0]} failed: {detail}")
    return completed.stdout.strip()


def safe_root(value: Path) -> Path:
    root = value.resolve()
    if not root.is_dir() or root.parent == root:
        raise BootstrapError(f"workspace must be an existing non-root directory: {root}")
    return root


def setup_config_path(root: Path) -> Path:
    return root / SETUP_CONFIG_REL


def read_setup_config(root: Path) -> dict[str, object]:
    path = setup_config_path(root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BootstrapError(f"invalid setup configuration: {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SETUP_CONFIG_SCHEMA:
        raise BootstrapError(f"unsupported setup configuration: {path}")
    return payload


def write_setup_config(root: Path, payload: dict[str, object]) -> None:
    path = setup_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ensure_local_exclude(root: Path) -> None:
    if not (root / ".git").exists():
        return
    git_dir_value = run(root, "rev-parse", "--git-dir", check=False)
    if not git_dir_value:
        return
    git_dir = Path(git_dir_value)
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
    if "/.local/" not in text.splitlines():
        exclude.write_text(text.rstrip("\n") + "\n/.local/\n", encoding="utf-8")


def infer_git_provider(remote: str) -> str:
    value = remote.strip().lower()
    if not value:
        return "local"
    if value.startswith("file:") or ("://" not in value and Path(value).exists()):
        return "local"
    host = urlsplit(value).hostname or value.split("@", 1)[-1].split(":", 1)[0]
    if host == "github.com":
        return "github"
    if host == "gitlab.com" or "gitlab" in host:
        return "gitlab"
    return "self_hosted"


def validate_git_remote(remote: str, base: Path | None = None) -> str:
    value = remote.strip().replace("\\", "/")
    if not value:
        raise BootstrapError("Git remote is required")
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https", "ssh", "file"}:
        if parsed.scheme in {"http", "https"} and parsed.username:
            raise BootstrapError("Git remote must not contain embedded credentials")
        if parsed.password:
            raise BootstrapError("Git remote must not contain embedded credentials")
        if parsed.scheme != "file" and not parsed.hostname:
            raise BootstrapError("Git remote must include a host")
        return value.rstrip("/")
    if re.fullmatch(r"[^/:\\\s]+@[^:\\s]+:.+", value):
        return value
    path = Path(value).expanduser()
    if base is not None and not path.is_absolute():
        path = base / path
    if path.exists():
        return str(path.resolve())
    raise BootstrapError("Git remote must be an HTTP(S)/SSH URL or an existing local path")


def validate_svn_remote(remote: str) -> str:
    value = remote.strip().replace("\\", "/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"svn", "http", "https", "file"}:
        raise BootstrapError("SVN remote must use svn://, HTTP(S), or file://")
    if parsed.username or parsed.password:
        raise BootstrapError("SVN remote must not contain embedded credentials")
    if parsed.scheme != "file" and not parsed.hostname:
        raise BootstrapError("SVN remote must include a host")
    return value.rstrip("/")


def run_external(executable: str, cwd: Path, *args: str, check: bool = True) -> str:
    command = [executable, *args]
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=dict(os.environ),
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise BootstrapError(f"{executable} {' '.join(args[:2])} failed: {detail}")
    return completed.stdout.strip()


def configure_installation(args: argparse.Namespace, root: Path) -> dict[str, object]:
    if not shutil.which("git"):
        raise BootstrapError("Git is required and must be available on PATH")
    remote = validate_git_remote(args.remote or DEFAULT_REMOTE, root)
    git_provider = args.git_provider if args.git_provider != "auto" else infer_git_provider(remote)
    if git_provider not in GIT_PROVIDERS - {"auto"}:
        raise BootstrapError(f"unsupported Git provider: {git_provider}")
    planning_provider = args.planning_provider
    if planning_provider not in PLANNING_PROVIDERS:
        raise BootstrapError(f"unsupported planning source provider: {planning_provider}")
    planning_remote = (
        validate_git_remote(args.planning_url, root)
        if planning_provider == "git" and args.planning_url
        else validate_svn_remote(args.planning_url)
        if planning_provider == "svn" and args.planning_url
        else ""
    )
    if args.planning_path:
        planning_candidate = Path(args.planning_path).expanduser()
        if not planning_candidate.is_absolute():
            planning_candidate = root / planning_candidate
        planning_path = str(planning_candidate.resolve())
    else:
        planning_path = ""
    if not args.branch.strip() or not args.planning_branch.strip():
        raise BootstrapError("Git branch names cannot be empty")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.planning_id):
        raise BootstrapError("planning id must use letters, digits, dot, underscore, or hyphen")
    if args.planning_revision is not None and args.planning_revision <= 0:
        raise BootstrapError("SVN planning revision must be positive")
    if planning_provider == "none" and (planning_remote or planning_path):
        raise BootstrapError("planning URL/path must be omitted when planning provider is none")
    if planning_provider == "local" and (not planning_path or not Path(planning_path).is_dir()):
        raise BootstrapError("local planning provider requires an existing directory")
    if planning_provider in {"git", "svn"} and not (planning_remote or planning_path):
        raise BootstrapError(f"planning provider {planning_provider} requires --planning-url or --planning-path")
    if planning_provider == "git" and planning_path and not (Path(planning_path) / ".git").exists():
        raise BootstrapError("Git planning path must contain a .git directory")
    if planning_provider == "svn" and planning_path and not (Path(planning_path) / ".svn").exists():
        raise BootstrapError("SVN planning path must contain a .svn directory")
    payload = {
        "schema_version": SETUP_CONFIG_SCHEMA,
        "git": {"provider": git_provider, "remote": remote, "branch": args.branch},
        "planning_source": {
            "provider": planning_provider,
            "remote": planning_remote or None,
            "path": planning_path or None,
            "branch": args.planning_branch if planning_provider == "git" else None,
            "revision": args.planning_revision if planning_provider == "svn" else None,
            "checkout": args.planning_id,
        },
    }
    ensure_local_exclude(root)
    write_setup_config(root, payload)
    return {"schema_version": SETUP_CONFIG_SCHEMA, "status": "ready", "config_file": str(setup_config_path(root).resolve()), **payload}


def planning_status(root: Path) -> dict[str, object]:
    config = read_setup_config(root)
    planning = config.get("planning_source") if isinstance(config.get("planning_source"), dict) else {}
    provider = str(planning.get("provider") or "none")
    tool = "svn" if provider == "svn" else "git" if provider == "git" else ""
    local_ready = provider == "local" and Path(str(planning.get("path") or "")).is_dir()
    return {
        "schema_version": "public_planning_source_status_v1",
        "status": "ready" if provider == "none" or local_ready or (tool and shutil.which(tool)) else "needs_input",
        "provider": provider,
        "remote": planning.get("remote"),
        "path": planning.get("path"),
        "branch": planning.get("branch"),
        "revision": planning.get("revision"),
        "tool": tool or None,
        "tool_installed": bool(shutil.which(tool)) if tool else None,
        "config_file": str(setup_config_path(root).resolve()) if config else None,
    }


def planning_sync(root: Path) -> dict[str, object]:
    config = read_setup_config(root)
    planning = config.get("planning_source") if isinstance(config.get("planning_source"), dict) else {}
    provider = str(planning.get("provider") or "none")
    if provider == "none":
        return {"schema_version": "public_planning_source_sync_v1", "status": "manual_required", "provider": "none", "next_action": "Configure a Git, SVN, or local planning source first."}
    checkout_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(planning.get("checkout") or "default")).strip("-") or "default"
    checkout = root / ".local" / "planning-sources" / checkout_name
    remote = str(planning.get("remote") or "")
    configured_path = str(planning.get("path") or "")
    if provider == "local":
        path = Path(configured_path).resolve()
        if not path.is_dir():
            raise BootstrapError(f"configured planning path is unavailable: {path}")
        return {"schema_version": "public_planning_source_sync_v1", "status": "ready", "provider": provider, "path": str(path), "mutated": False}
    if provider == "git":
        git_exe = shutil.which("git")
        if not git_exe:
            raise BootstrapError("Git is required for a Git planning source")
        path = Path(configured_path).resolve() if configured_path else checkout
        if configured_path:
            if run_external(git_exe, path, "status", "--porcelain"):
                raise BootstrapError(f"Git planning checkout is dirty: {path}")
            commit = run_external(git_exe, path, "rev-parse", "HEAD")
            branch = run_external(git_exe, path, "branch", "--show-current", check=False) or None
            return {"schema_version": "public_planning_source_sync_v1", "status": "ready", "provider": provider, "path": str(path), "branch": branch, "commit": commit, "mutated": False}
        if not (path / ".git").is_dir():
            path.parent.mkdir(parents=True, exist_ok=True)
            run_external(git_exe, path.parent, "clone", "--branch", str(planning.get("branch") or "main"), remote, str(path))
        else:
            if run_external(git_exe, path, "status", "--porcelain"):
                raise BootstrapError(f"Git planning checkout is dirty: {path}")
            branch = str(planning.get("branch") or "main")
            run_external(git_exe, path, "fetch", "origin", branch)
            run_external(git_exe, path, "switch", branch, check=False)
            run_external(git_exe, path, "merge", "--ff-only", f"origin/{branch}")
        commit = run_external(git_exe, path, "rev-parse", "HEAD")
        return {"schema_version": "public_planning_source_sync_v1", "status": "ready", "provider": provider, "path": str(path), "commit": commit, "mutated": True}
    if provider == "svn":
        svn_exe = shutil.which("svn")
        if not svn_exe:
            raise BootstrapError("SVN is required for an SVN planning source")
        path = Path(configured_path).resolve() if configured_path else checkout
        revision = planning.get("revision")
        if configured_path:
            actual_revision = run_external("svnversion", path, check=False) if shutil.which("svnversion") else "unknown"
            return {"schema_version": "public_planning_source_sync_v1", "status": "ready", "provider": provider, "path": str(path), "revision": actual_revision, "mutated": False}
        if not (path / ".svn").is_dir():
            path.parent.mkdir(parents=True, exist_ok=True)
            args = ["checkout"]
            if revision is not None:
                args.extend(["--revision", str(revision)])
            args.extend([remote, str(path)])
            run_external(svn_exe, path.parent, *args)
        else:
            args = ["update"]
            if revision is not None:
                args.extend(["--revision", str(revision)])
            run_external(svn_exe, path, *args)
        actual_revision = run_external("svnversion", path, check=False) if shutil.which("svnversion") else "unknown"
        return {"schema_version": "public_planning_source_sync_v1", "status": "ready", "provider": provider, "path": str(path), "revision": actual_revision, "mutated": True}
    raise BootstrapError(f"unsupported planning source provider: {provider}")


def mode(root: Path) -> str:
    if (root / ".git").exists():
        top = Path(run(root, "rev-parse", "--show-toplevel")).resolve()
        if top != root:
            raise BootstrapError(f"workspace is nested inside another repository: {top}")
        return "repository"
    return "empty" if not any(root.iterdir()) else "nonempty"


def normalized_remote(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme and parsed.hostname:
        return f"{parsed.hostname.lower()}/{parsed.path.strip('/').removesuffix('.git').lower()}"
    return str(Path(value).resolve()).replace("\\", "/").rstrip("/").lower()


def remote_name(root: Path, expected: str) -> str | None:
    expected_key = normalized_remote(expected)
    for name in run(root, "remote", check=False).splitlines():
        url = run(root, "remote", "get-url", name, check=False)
        if normalized_remote(url) == expected_key:
            return name
    return None


def status(root: Path, remote: str) -> dict[str, object]:
    current = mode(root)
    result: dict[str, object] = {
        "schema_version": "public_setup_status_v1",
        "status": "ready" if current in {"empty", "repository"} else "blocked",
        "mode": current,
        "workspace": str(root),
        "remote": remote,
        "dependencies": {
            "git": {"required": True, "installed": bool(shutil.which("git"))},
            "svn": {"required": False, "installed": bool(shutil.which("svn"))},
        },
    }
    if current == "repository":
        result.update(
            branch=run(root, "branch", "--show-current"),
            head=run(root, "rev-parse", "--verify", "HEAD", check=False) or None,
            dirty=bool(run(root, "status", "--porcelain", "--untracked-files=all")),
            expected_remote_configured=bool(remote_name(root, remote)),
        )
    elif current == "nonempty":
        result["blocker"] = "selected folder is not empty and is not a repository"
    config = read_setup_config(root)
    if config:
        git_config = config.get("git") if isinstance(config.get("git"), dict) else {}
        result["configured_git"] = git_config
        result["planning_source"] = planning_status(root)
    else:
        result["configured_git"] = None
        result["planning_source"] = {"provider": "none", "status": "not_configured"}
    return result


def sync(root: Path, remote: str) -> dict[str, object]:
    configured = read_setup_config(root)
    git_config = configured.get("git") if isinstance(configured.get("git"), dict) else {}
    branch = str(git_config.get("branch") or "main")
    current = mode(root)
    if current == "nonempty":
        raise BootstrapError("selected folder must be empty or an existing public repository")
    if current == "empty":
        run(root, "clone", "--origin", "origin", "--branch", branch, "--single-branch", remote, ".")
    else:
        if run(root, "status", "--porcelain", "--untracked-files=all"):
            raise BootstrapError("existing repository has local changes; resolve them before sync")
        name = remote_name(root, remote)
        if not name:
            raise BootstrapError("existing repository does not have the configured Git remote")
        run(root, "fetch", name, branch)
        current_branch = run(root, "branch", "--show-current")
        if current_branch != branch:
            run(root, "switch", branch)
        run(root, "merge", "--ff-only", f"{name}/{branch}")
    result = status(root, remote)
    result["status"] = "synced"
    return result


def demo(root: Path) -> dict[str, object]:
    project_root = root / "sql-projects" / "example"
    project_root.mkdir(parents=True, exist_ok=True)
    project_script = root / "sql-engineering" / "scripts" / "sql_project.py"
    if project_script.is_file() and not (project_root / "manifest.json").is_file():
        completed = subprocess.run(
            [
                sys.executable,
                str(project_script),
                "init",
                "--root",
                str(project_root),
                "--project-name",
                "example",
                "--project-id",
                "example",
                "--display-name",
                "Example Analytics",
                "--dialect",
                "starrocks",
                "--user-request",
                "Initialize the fictional public demo project",
                "--function-selection",
                "PROJECT_ADMIN",
            ],
            cwd=root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise BootstrapError(completed.stderr.strip() or completed.stdout.strip() or "demo initialization failed")
    elif not (project_root / "manifest.json").is_file():
        for directory in ("context", "rules", "sources", "reviews", "query_workspace"):
            (project_root / directory).mkdir(parents=True, exist_ok=True)
        (project_root / "project_config.json").write_text(
            json.dumps(
                {
                    "schema_version": "public_project_config_v1",
                    "project_id": "example",
                    "display_name": "Example Analytics",
                    "dialect": "starrocks",
                    "execution": {"mode": "manual_required"},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (project_root / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "public_project_manifest_v1",
                    "project_id": "example",
                    "project_name": "Example Analytics",
                    "project_config_file": "project_config.json",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    compatibility_script = root / "sql-engineering" / "scripts" / "sql_workspace.py"
    compatibility_config = project_root / ".sql-engineering" / "project.json"
    if compatibility_script.is_file() and not compatibility_config.is_file():
        completed = subprocess.run(
            [
                sys.executable,
                str(compatibility_script),
                "bootstrap",
                "--root",
                str(root),
                "--project-id",
                "example",
                "--dialect",
                "starrocks",
            ],
            cwd=root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise BootstrapError(completed.stderr.strip() or completed.stdout.strip() or "compatibility workspace initialization failed")
    (root / "sql-projects" / "README.md").write_text(
        "# Local SQL Projects\n\nProjects created here are local workspaces. Keep production results and credentials out of Git.\n",
        encoding="utf-8",
    )
    return {
        "schema_version": "public_setup_demo_v1",
        "status": "ready",
        "project_root": str(project_root.resolve()),
        "fictional": True,
        "execution": "not_configured",
        "setup_configured": bool(read_setup_config(root)),
        "planning_source": planning_status(root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "sync", "demo", "configure", "planning-status", "planning-sync"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--git-provider", choices=sorted(GIT_PROVIDERS), default="auto")
    parser.add_argument("--planning-provider", choices=sorted(PLANNING_PROVIDERS), default="none")
    parser.add_argument("--planning-url", default="")
    parser.add_argument("--planning-path", type=Path)
    parser.add_argument("--planning-branch", default="main")
    parser.add_argument("--planning-revision", type=int)
    parser.add_argument("--planning-id", default="default")
    args = parser.parse_args(argv)
    try:
        root = safe_root(args.root)
        configured = read_setup_config(root)
        configured_git = configured.get("git") if isinstance(configured.get("git"), dict) else {}
        remote = args.remote or str(configured_git.get("remote") or DEFAULT_REMOTE)
        if args.command == "status":
            result = status(root, remote)
        elif args.command == "sync":
            result = sync(root, remote)
        elif args.command == "demo":
            result = demo(root)
        elif args.command == "configure":
            result = configure_installation(args, root)
        elif args.command == "planning-status":
            result = planning_status(root)
        else:
            result = planning_sync(root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"ready", "synced"} else 2
    except (BootstrapError, OSError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
