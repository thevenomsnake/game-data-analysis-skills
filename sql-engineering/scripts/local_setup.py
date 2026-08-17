#!/usr/bin/env python3
"""Check or initialize a local public SQL project without database credentials."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def project_root(repo: Path, project: str) -> Path:
    if not project or Path(project).name != project:
        raise ValueError("project must be a single directory name")
    return (repo / "sql-projects" / project).resolve()


def status(repo: Path, project: str) -> dict[str, object]:
    root = project_root(repo, project)
    config = root / "project_config.json"
    initialized = (root / "manifest.json").is_file()
    return {
        "schema_version": "public_local_setup_v1",
        "status": "ready" if initialized else "needs_input",
        "project": project,
        "project_root": str(root),
        "project_initialized": initialized,
        "database": "not_configured",
        "config_file": str(config) if config.is_file() else None,
        "next_action": "Run bootstrap_repo.py demo --root <workspace>" if not initialized else "Use the project with sql-engineering.",
    }


def initialize(repo: Path, project: str, dialect: str) -> dict[str, object]:
    root = project_root(repo, project)
    script = repo / "sql-engineering" / "scripts" / "sql_project.py"
    if not script.is_file():
        raise ValueError(f"sql_project.py is missing: {script}")
    if not (root / "manifest.json").is_file():
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "init",
                "--root",
                str(root),
                "--project-name",
                project,
                "--project-id",
                project,
                "--display-name",
                f"{project} public project",
                "--dialect",
                dialect,
                "--user-request",
                "Initialize a local public SQL project",
                "--function-selection",
                "PROJECT_ADMIN",
            ],
            cwd=repo,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise ValueError(completed.stderr.strip() or completed.stdout.strip() or "project initialization failed")
    result = status(repo, project)
    result["status"] = "ready"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "init"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--project", required=True)
    parser.add_argument("--dialect", default="starrocks", choices=("starrocks", "hive"))
    args = parser.parse_args(argv)
    try:
        repo = args.repo_root.resolve()
        result = status(repo, args.project) if args.command == "status" else initialize(repo, args.project, args.dialect)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ready" else 2
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
