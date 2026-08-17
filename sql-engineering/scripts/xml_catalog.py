#!/usr/bin/env python3
"""Extract a compact catalog from a TLOG XML file."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_request,
    require_user_function_selection,
)
from capability_registry import command_function_ids




def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def project_relative_source(xml_path: Path, out_path: Path) -> str:
    """Return a durable source path for catalogs saved under a project."""
    project_root = out_path.parent.parent if out_path.parent.name == "sources" else out_path.parent
    try:
        return xml_path.relative_to(project_root).as_posix()
    except ValueError:
        return xml_path.name


def parse_catalog(xml_path: Path, source_file: str | None = None) -> dict:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    logs = []
    for struct in root.findall(".//struct"):
        fields = []
        for entry in struct.findall("entry"):
            fields.append(
                {
                    "name": entry.attrib.get("name", ""),
                    "type": entry.attrib.get("type", ""),
                    "desc": entry.attrib.get("desc", ""),
                    "index": entry.attrib.get("index", ""),
                    "defaultvalue": entry.attrib.get("defaultvalue", ""),
                    "size": entry.attrib.get("size", ""),
                }
            )
        logs.append(
            {
                "name": struct.attrib.get("name", ""),
                "version": struct.attrib.get("version", ""),
                "desc": struct.attrib.get("desc", ""),
                "field_count": len(fields),
                "fields": fields,
            }
        )
    return {
        "source_file": source_file or xml_path.name,
        "generated_at": now_iso(),
        "log_count": len(logs),
        "logs": logs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_file")
    parser.add_argument("--out", required=True)
    add_function_gate_arguments(
        parser,
        selection_help="Optional explicit source-intake function route, such as 【来源/XML同步】 or [SOURCE_INTAKE].",
    )
    args = parser.parse_args()
    try:
        purpose = "source/XML intake"
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("xml_catalog.py"),
            purpose=purpose,
        )
        require_user_request(args.user_request, purpose=purpose)
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)

    xml_path = Path(args.xml_file).resolve()
    out_path = Path(args.out).resolve()
    catalog = parse_catalog(xml_path, project_relative_source(xml_path, out_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {catalog['log_count']} logs to {out_path}")


if __name__ == "__main__":
    main()
