#!/usr/bin/env python3
"""Check or initialize a local public SQL project without database credentials."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import web_query_adapter


def project_root(repo: Path, project: str) -> Path:
    if not project or Path(project).name != project:
        raise ValueError("project must be a single directory name")
    return (repo / "sql-projects" / project).resolve()


def status(repo: Path, project: str) -> dict[str, object]:
    root = project_root(repo, project)
    config = root / "project_config.json"
    initialized = (root / "manifest.json").is_file()
    direct_config = root / ".sql-engineering" / "connections.local.json"
    web_config = root / ".sql-engineering" / "web-query-adapter.local.json"
    surfaces: list[str] = []
    if direct_config.is_file():
        surfaces.append("direct")
    web_status = "missing"
    if web_config.is_file():
        try:
            web_query_adapter.load_adapter(web_config)
            surfaces.append("web")
            web_status = "ready"
        except web_query_adapter.WebQueryAdapterError as error:
            web_status = f"blocked: {error}"
    return {
        "schema_version": "public_local_setup_v1",
        "status": "ready" if initialized else "needs_input",
        "project": project,
        "project_root": str(root),
        "project_initialized": initialized,
        "database": "configured" if direct_config.is_file() else "not_configured",
        "execution_surface": "+".join(surfaces) if surfaces else "manual",
        "web_adapter": web_status,
        "config_file": str(config) if config.is_file() else None,
        "next_action": "Run bootstrap_repo.py demo --root <workspace>" if not initialized else "Use the project with sql-engineering.",
    }


def initialize(
    repo: Path,
    project: str,
    dialect: str,
    execution_surface: str = "manual",
    web_adapter_file: str | Path | None = None,
) -> dict[str, object]:
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
    if execution_surface not in {"manual", "direct", "web"}:
        raise ValueError("execution_surface must be manual, direct, or web")
    if execution_surface == "web":
        source = Path(web_adapter_file).resolve() if web_adapter_file else repo / web_query_adapter.EXAMPLE_RELATIVE
        adapter = web_query_adapter.load_adapter(source)
        destination = root / web_query_adapter.DEFAULT_RELATIVE
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if not destination.is_file():
            raise ValueError("web adapter initialization did not create the local config")
    result = status(repo, project)
    result["status"] = "ready"
    result["requested_execution_surface"] = execution_surface
    if execution_surface == "web" and result.get("web_adapter") != "ready":
        raise ValueError("web adapter initialization did not validate")
    if execution_surface == "direct" and result.get("database") != "configured":
        result["next_execution_action"] = "Configure .sql-engineering/connections.local.json with a local read-only profile."
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "init"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--project", required=True)
    parser.add_argument("--dialect", default="starrocks", choices=("starrocks", "hive"))
    parser.add_argument("--execution-surface", default="manual", choices=("manual", "direct", "web"))
    parser.add_argument("--web-adapter-file")
    args = parser.parse_args(argv)
    try:
        repo = args.repo_root.resolve()
        result = (
            status(repo, args.project)
            if args.command == "status"
            else initialize(repo, args.project, args.dialect, args.execution_surface, args.web_adapter_file)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ready" else 2
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
