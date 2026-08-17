#!/usr/bin/env python3
"""Initialize a local checkout of the public Game Data Analysis Skills repository."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_REMOTE = "https://github.com/thevenomsnake/game-data-analysis-skills.git"


class BootstrapError(RuntimeError):
    pass


def run(root: Path, *args: str, check: bool = True) -> str:
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
    }
    if current == "repository":
        result.update(
            branch=run(root, "branch", "--show-current"),
            head=run(root, "rev-parse", "HEAD"),
            dirty=bool(run(root, "status", "--porcelain", "--untracked-files=all")),
            expected_remote_configured=bool(remote_name(root, remote)),
        )
    elif current == "nonempty":
        result["blocker"] = "selected folder is not empty and is not a repository"
    return result


def sync(root: Path, remote: str) -> dict[str, object]:
    current = mode(root)
    if current == "nonempty":
        raise BootstrapError("selected folder must be empty or an existing public repository")
    if current == "empty":
        run(root, "clone", "--origin", "origin", "--branch", "main", "--single-branch", remote, ".")
    else:
        if run(root, "status", "--porcelain", "--untracked-files=all"):
            raise BootstrapError("existing repository has local changes; resolve them before sync")
        name = remote_name(root, remote)
        if not name:
            raise BootstrapError("existing repository does not have the public GitHub remote")
        run(root, "fetch", name, "main")
        branch = run(root, "branch", "--show-current")
        if branch != "main":
            run(root, "switch", "main")
        run(root, "merge", "--ff-only", f"{name}/main")
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
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "sync", "demo"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default=DEFAULT_REMOTE)
    args = parser.parse_args(argv)
    try:
        root = safe_root(args.root)
        result = (
            status(root, args.remote)
            if args.command == "status"
            else sync(root, args.remote)
            if args.command == "sync"
            else demo(root)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"ready", "synced"} else 2
    except BootstrapError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
