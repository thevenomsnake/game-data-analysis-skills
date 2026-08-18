#!/usr/bin/env python3
"""Validate the six public README files and their shared navigation contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


LOCALES = (
    ("en", "English", "README.md"),
    ("zh-CN", "简体中文", "README.zh-CN.md"),
    ("zh-TW", "繁體中文", "README.zh-TW.md"),
    ("ja", "日本語", "README.ja.md"),
    ("ko", "한국어", "README.ko.md"),
    ("es", "Español", "README.es.md"),
)
REQUIRED_LINKS = (
    "sql-engineering/references/execution-surfaces.md",
    "docs/INTEGRATION_INTERFACES.md",
    "docs/READONLY_ASSET_CONSUMER_GUIDE.md",
    "docs/PUBLIC_MAINTENANCE.md",
    "excel-report-visualizer/README.md",
)
CI_BADGE = "actions/workflows/public-validation.yml/badge.svg"


def validate(root: str | Path) -> dict[str, Any]:
    base = Path(root).resolve()
    findings: list[dict[str, str]] = []
    locale_names = [name for _, name, _ in LOCALES]
    locale_files = [file for _, _, file in LOCALES]

    for locale, current_name, filename in LOCALES:
        path = base / filename
        if not path.is_file():
            findings.append({"id": "readme_missing", "locale": locale, "path": filename})
            continue
        text = path.read_text(encoding="utf-8")
        navigation = "\n".join(text.splitlines()[:12])
        for name in locale_names:
            if name not in navigation:
                findings.append({"id": "language_name_missing", "locale": locale, "path": filename, "value": name})
        for other_file in locale_files:
            if other_file == filename:
                continue
            if f"]({other_file})" not in navigation:
                findings.append({"id": "language_link_missing", "locale": locale, "path": filename, "value": other_file})
        if f"]({filename})" in navigation:
            findings.append({"id": "current_locale_self_linked", "locale": locale, "path": filename})
        if current_name not in navigation:
            findings.append({"id": "current_locale_name_missing", "locale": locale, "path": filename})
        for target in REQUIRED_LINKS:
            if target not in text:
                findings.append({"id": "required_link_missing", "locale": locale, "path": filename, "value": target})
            elif not (base / target).is_file():
                findings.append({"id": "required_link_target_missing", "locale": locale, "path": filename, "value": target})

    root_readme = base / "README.md"
    if root_readme.is_file() and CI_BADGE not in root_readme.read_text(encoding="utf-8"):
        findings.append({"id": "ci_badge_missing", "locale": "en", "path": "README.md"})

    return {
        "schema_version": "public_readme_validation_v1",
        "status": "pass" if not findings else "block",
        "root": str(base),
        "locale_count": len(LOCALES),
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("--root", default=".")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args(argv)
    result = validate(args.root)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"status={result['status']}")
        for finding in result["findings"]:
            print(f"- {finding}")
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
