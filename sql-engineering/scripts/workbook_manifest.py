#!/usr/bin/env python3
"""Extract a bounded presentation manifest from an XLSX package without loading cells."""

from __future__ import annotations

import html
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


SCHEMA_VERSION = "workbook_manifest_v1"
MAX_SHEETS = 64
MAX_CHART_TITLES = 64
MAX_TEXT_LENGTH = 200
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
REUSABLE_WORKBOOK_KINDS = {"analysis_workbook", "comparison_workbook", "visualization"}
CHART_PART_PATTERN = re.compile(r"xl/(?:drawings/)?charts/chart\d+\.xml")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _bounded(value: Any) -> str:
    return html.unescape(str(value or "").strip())[:MAX_TEXT_LENGTH]


def _safe_relative_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute() or path.drive or ".." in path.parts or re.match(r"^[A-Za-z]:", text):
        return ""
    return path.as_posix()


def _sheet_rows(root: ElementTree.Element) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for node in root.iter():
        if _local_name(node.tag) != "sheet":
            continue
        state = str(node.attrib.get("state") or "visible").strip().lower()
        rows.append(
            {
                "name": _bounded(node.attrib.get("name")),
                "visibility": state if state in {"visible", "hidden", "veryhidden"} else "visible",
            }
        )
    return rows


def _chart_title(root: ElementTree.Element) -> str:
    title_nodes = [node for node in root.iter() if _local_name(node.tag) == "title"]
    if not title_nodes:
        return ""
    pieces = [
        _bounded(node.text)
        for title in title_nodes[:1]
        for node in title.iter()
        if _local_name(node.tag) == "t" and _bounded(node.text)
    ]
    return _bounded(" ".join(pieces))


def xlsx_chart_parts(names: Iterable[str]) -> list[str]:
    return sorted(name for name in names if CHART_PART_PATTERN.fullmatch(name))


def build_workbook_manifest(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if path.suffix.lower() != ".xlsx" or not zipfile.is_zipfile(path):
        raise ValueError(f"Workbook manifest requires a valid .xlsx package: {path}")
    with zipfile.ZipFile(path) as package:
        names = set(package.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("Workbook package is missing xl/workbook.xml.")
        workbook_root = ElementTree.fromstring(package.read("xl/workbook.xml"))
        all_sheets = _sheet_rows(workbook_root)
        chart_parts = xlsx_chart_parts(names)
        all_titles: list[str] = []
        for part in chart_parts[:MAX_CHART_TITLES]:
            title = _chart_title(ElementTree.fromstring(package.read(part)))
            all_titles.append(title)
    return {
        "schema_version": SCHEMA_VERSION,
        "sheet_count": len(all_sheets),
        "sheets": all_sheets[:MAX_SHEETS],
        "chart_count": len(chart_parts),
        "chart_titles": all_titles,
        "display_metadata": {
            "source": "xlsx_package_structure",
            "bounded": True,
            "max_sheet_entries": MAX_SHEETS,
            "max_chart_titles": MAX_CHART_TITLES,
            "sheets_truncated": len(all_sheets) > MAX_SHEETS,
            "chart_titles_truncated": len(chart_parts) > MAX_CHART_TITLES,
        },
    }


def is_reusable_workbook(kind: Any, media_type: Any, path: Any) -> bool:
    safe_path = _safe_relative_path(path)
    return (
        str(kind or "").strip().lower() in REUSABLE_WORKBOOK_KINDS
        and str(media_type or "").strip().lower() == XLSX_MEDIA_TYPE
        and Path(safe_path).suffix.lower() == ".xlsx"
    )


def reusable_workbook_presentation(kind: Any, media_type: Any, path: Any, output: Any) -> dict[str, Any]:
    output = output if isinstance(output, dict) else {}
    safe_path = _safe_relative_path(path)
    eligible = is_reusable_workbook(kind, media_type, path)
    manifest = output.get("workbook_manifest") if isinstance(output.get("workbook_manifest"), dict) else {}
    preview_status = str(output.get("preview_status") or "not_available").strip()
    if preview_status not in {"available", "not_available"}:
        preview_status = "not_available"
    return {
        "schema_version": "reusable_workbook_presentation_v1",
        "eligible": eligible,
        "workbook_type": str(kind or "").strip().lower() if eligible else "",
        "media_type": str(media_type or "").strip(),
        "download_path": safe_path if eligible else "",
        "preview_status": preview_status if eligible else "not_available",
        "workbook_manifest": manifest if eligible and manifest.get("schema_version") == SCHEMA_VERSION else {},
    }
