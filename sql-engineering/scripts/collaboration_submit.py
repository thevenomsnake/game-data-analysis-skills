#!/usr/bin/env python3
"""Create a safe local collaboration plan for the public repository.

The public edition never authenticates to or pushes a remote service. It reports the exact local
files a user may review and leaves the final Git operation to the user's normal Git client.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


EXCLUDED_PREFIXES = (
    ".git/",
    ".tmp/",
    ".test-tmp/",
    "sql-projects/",
    "knowledge-base/",
    "planning-sources/",
    "Better" + "Xml/",
)


def git(root: Path, *args: str) -> list[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise ValueError(completed.stderr.strip() or completed.stdout.strip() or "git command failed")
    return [line for line in completed.stdout.splitlines() if line]


def allowed(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return not any(normalized.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def plan(root: Path) -> dict[str, object]:
    staged = [path for path in git(root, "diff", "--cached", "--name-only") if allowed(path)]
    modified = [path for path in git(root, "diff", "--name-only") if allowed(path)]
    untracked = [path for path in git(root, "ls-files", "--others", "--exclude-standard") if allowed(path)]
    blocked = sorted(
        set(git(root, "diff", "--cached", "--name-only") + git(root, "diff", "--name-only") + git(root, "ls-files", "--others", "--exclude-standard"))
        - set(staged)
        - set(modified)
        - set(untracked)
    )
    return {
        "schema_version": "public_collaboration_plan_v1",
        "status": "ready",
        "mode": "local_review_only",
        "root": str(root.resolve()),
        "staged": sorted(staged),
        "modified": sorted(modified),
        "untracked": sorted(untracked),
        "blocked": blocked,
        "next_action": "Review the listed files and use the user's normal Git client to commit or push.",
    }


def build_plan(root: Path) -> dict[str, object]:
    """Compatibility alias used by project-level read-only summaries."""

    return plan(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "status", "submit"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        payload = plan(args.repo_root.resolve())
        if args.command == "submit":
            payload["status"] = "manual_required"
            payload["reason"] = "public collaboration never pushes automatically"
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
