#!/usr/bin/env python3
"""Build and search the local catalog for development SQL inspections."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any


RECEIPT_VERSION = "dev_sql_inspection_receipt_v2"
INDEX_VERSION = "dev_sql_inspection_index_v2"
MAX_INDEX_ITEMS = 500
MAX_PREVIEW_ROWS = 20
IDENTIFIER_VALUE_RE = re.compile(
    r"(^|_)(v?openid|open_id|role_?id|player_?id|device_?id|account_?id|uin|uuid|guid|ip)(_|$)",
    re.I,
)
TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+((?:`[^`]+`\.)?`[^`]+`|[A-Za-z0-9_$-]+(?:\.[A-Za-z0-9_$-]+)?)",
    re.I,
)


def serialize_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def clean_identifier(value: str) -> str:
    return ".".join(part.strip().strip("`") for part in str(value or "").split("."))


def diagnostic_fingerprint(sql: str) -> str:
    normalized = re.sub(
        r"'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?'",
        "'<date>'",
        sql,
    )
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def extract_tables(sql: str) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for match in TABLE_REF_RE.finditer(sql):
        value = clean_identifier(match.group(1))
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def extract_predicates(sql: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"(?P<field>`?[A-Za-z_][A-Za-z0-9_$]*`?)\s*"
        r"(?P<operator>>=|<=|=|>|<)\s*"
        r"(?P<value>'(?:''|[^'])*'|-?\d+(?:\.\d+)?)",
        re.I,
    )
    predicates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for match in pattern.finditer(sql):
        raw = match.group("value")
        value: Any = raw[1:-1].replace("''", "'") if raw.startswith("'") else raw
        field = clean_identifier(match.group("field"))
        operator = match.group("operator")
        key = (field.lower(), operator, str(value))
        if key not in seen:
            seen.add(key)
            predicates.append({"field": field, "operator": operator, "value": value})
    return predicates


def infer_subject(
    command: str,
    sql: str,
    database: str,
    implicit_business_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    subject: dict[str, Any] = {
        "kind": {
            "tables": "table_discovery",
            "describe": "table_schema",
            "enum": "field_enum",
            "query": "custom_diagnostic",
        }.get(command, command),
        "database": database,
        "tables": extract_tables(sql),
    }
    if command == "tables":
        like_match = re.search(r"\blike\s+'((?:''|[^'])*)'", sql, re.I)
        subject["table_pattern"] = like_match.group(1).replace("''", "'") if like_match else None
    elif command == "describe":
        match = re.search(
            r"\b(?:describe|desc)\s+((?:`[^`]+`\.)?`[^`]+`|[A-Za-z0-9_$-]+(?:\.[A-Za-z0-9_$-]+)?)",
            sql,
            re.I,
        )
        if match:
            subject["tables"] = [clean_identifier(match.group(1))]
    elif command == "enum":
        field_match = re.search(
            r"\bselect\s+`?([A-Za-z_][A-Za-z0-9_$]*)`?\s+as\s+field_value\b",
            sql,
            re.I,
        )
        subject["field"] = field_match.group(1) if field_match else None
        limit_match = re.search(r"\blimit\s+(\d+)\b", sql, re.I)
        subject["limit"] = int(limit_match.group(1)) if limit_match else None
    elif command == "query":
        subject["diagnostic_fingerprint"] = diagnostic_fingerprint(sql)

    predicates = extract_predicates(sql)
    implicit_filters = (
        implicit_business_scope.get("filters")
        if isinstance(implicit_business_scope, dict) and isinstance(implicit_business_scope.get("filters"), list)
        else []
    )
    implicit_keys = {
        (str(item.get("field") or "").lower(), str(item.get("value")))
        for item in implicit_filters
        if isinstance(item, dict)
    }
    date_groups: dict[str, dict[str, Any]] = {}
    other_filters: list[dict[str, Any]] = []
    for item in predicates:
        value = str(item["value"])
        if re.match(r"\d{4}-\d{2}-\d{2}", value) and item["operator"] in {">=", ">", "<=", "<"}:
            group = date_groups.setdefault(item["field"], {"field": item["field"]})
            if item["operator"] in {">=", ">"}:
                group["start"] = value
                group["start_inclusive"] = item["operator"] == ">="
            else:
                group["end"] = value
                group["end_inclusive"] = item["operator"] == "<="
        else:
            key = (str(item.get("field") or "").lower(), str(item.get("value")))
            if key not in implicit_keys:
                other_filters.append(item)
    if date_groups:
        subject["date_ranges"] = list(date_groups.values())
    if other_filters:
        subject["filters"] = other_filters
    if implicit_filters:
        subject["implicit_business_scope"] = {
            "filters": implicit_filters,
            "sql_predicate_policy": str(implicit_business_scope.get("sql_predicate_policy") or "omit"),
        }
    subject["subject_fingerprint"] = stable_fingerprint(
        {
            key: value
            for key, value in subject.items()
            if key not in {"subject_fingerprint", "date_ranges", "limit"}
        }
    )
    return subject


def summarize_result(
    command: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    subject: dict[str, Any],
) -> dict[str, Any]:
    serialized = [
        {column: serialize_cell(row.get(column)) for column in columns}
        for row in rows[:MAX_PREVIEW_ROWS]
    ]
    sensitive_enum = command == "enum" and bool(IDENTIFIER_VALUE_RE.search(str(subject.get("field") or "")))
    summary: dict[str, Any] = {
        "row_count": len(rows),
        "columns": columns,
        "preview_suppressed": sensitive_enum,
        "preview": [] if sensitive_enum else serialized,
    }
    if command == "enum":
        total = 0
        total_known = True
        values: list[dict[str, Any]] = []
        for row in rows:
            try:
                total += int(row.get("row_count", 0))
            except (TypeError, ValueError):
                total_known = False
            if not sensitive_enum and len(values) < MAX_PREVIEW_ROWS:
                values.append(
                    {
                        "value": serialize_cell(row.get("field_value")),
                        "row_count": serialize_cell(row.get("row_count")),
                    }
                )
        summary.update(
            {
                "observed_distinct_value_count": len(rows),
                "observed_row_count": total if total_known else None,
                "value_preview": values,
                "limit_reached": bool(subject.get("limit") and len(rows) >= int(subject["limit"])),
                "summary_text": f"Observed {len(rows)} distinct values"
                + (f" across {total} rows" if total_known else ""),
            }
        )
    elif command == "describe":
        summary["summary_text"] = f"Observed {len(rows)} fields"
    elif command == "tables":
        summary["summary_text"] = f"Observed {len(rows)} matching tables"
    else:
        summary["summary_text"] = f"Returned {len(rows)} rows with {len(columns)} columns"
    return summary


def build_receipt(
    *,
    root: Path,
    project_id: str,
    command: str,
    database: str,
    sql_file: Path,
    result_file: Path,
    receipt_file: Path,
    sql: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    executed_at: str,
    user_request: str | None,
    legacy_contract_version: str | None = None,
    implicit_business_scope: dict[str, Any] | None = None,
    project_relation: dict[str, Any] | None = None,
    server_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    subject = infer_subject(command, sql, database, implicit_business_scope)
    result_summary = summarize_result(command, columns, rows, subject)
    receipt: dict[str, Any] = {
        "contract_version": RECEIPT_VERSION,
        "inspection_id": receipt_file.parent.name,
        "status": "ready",
        "project_id": project_id,
        "command": command,
        "database": database,
        "user_request": user_request,
        "request_status": "recorded" if user_request else "legacy_unavailable",
        "subject": subject,
        "result_summary": result_summary,
        "files": {
            "sql": project_relative(root, sql_file),
            "result": project_relative(root, result_file),
            "receipt": project_relative(root, receipt_file),
        },
        "sql_file": str(sql_file.resolve()),
        "result_file": str(result_file.resolve()),
        "receipt_file": str(receipt_file.resolve()),
        "row_count": len(rows),
        "columns": columns,
        "sql_sha256": sha256_file(sql_file),
        "result_sha256": sha256_file(result_file),
        "executed_at": executed_at,
        "local_only": True,
        "privacy_owner": "DA",
        "reuse_contract": {
            "state": "observed",
            "searchable": True,
            "automatic_promotion": False,
            "knowledge_route": "KNOWLEDGE",
            "business_rule_route": "RULES",
        },
    }
    if project_relation:
        receipt["project_relation"] = project_relation
    if server_capabilities:
        receipt["server_capabilities"] = server_capabilities
    if legacy_contract_version and legacy_contract_version != RECEIPT_VERSION:
        receipt["migration"] = {
            "from_contract_version": legacy_contract_version,
            "raw_sql_and_result_unchanged": True,
        }
    return receipt


def receipt_to_index_item(receipt: dict[str, Any]) -> dict[str, Any]:
    subject = receipt.get("subject") if isinstance(receipt.get("subject"), dict) else {}
    result_summary = receipt.get("result_summary") if isinstance(receipt.get("result_summary"), dict) else {}
    search_values = [
        receipt.get("user_request"),
        receipt.get("command"),
        receipt.get("database"),
        subject.get("field"),
        subject.get("table_pattern"),
        *(subject.get("tables") or []),
        *(receipt.get("columns") or []),
    ]
    for item in result_summary.get("value_preview") or []:
        if isinstance(item, dict):
            search_values.append(item.get("value"))
    index_summary = {key: value for key, value in result_summary.items() if key != "preview"}
    return {
        "inspection_id": receipt.get("inspection_id"),
        "executed_at": receipt.get("executed_at"),
        "command": receipt.get("command"),
        "database": receipt.get("database"),
        "user_request": receipt.get("user_request"),
        "project_relation": receipt.get("project_relation"),
        "server_capabilities": receipt.get("server_capabilities"),
        "subject": subject,
        "result_summary": index_summary,
        "row_count": receipt.get("row_count", 0),
        "columns": receipt.get("columns") or [],
        "sql_sha256": receipt.get("sql_sha256"),
        "result_sha256": receipt.get("result_sha256"),
        "files": receipt.get("files") or {},
        "search_text": " ".join(str(value) for value in search_values if value is not None and value != "").lower(),
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def write_index(root: Path, receipts: list[dict[str, Any]]) -> dict[str, Any]:
    items = [receipt_to_index_item(receipt) for receipt in receipts]
    items.sort(key=lambda item: str(item.get("executed_at") or ""), reverse=True)
    items = items[:MAX_INDEX_ITEMS]
    subject_counts = Counter(
        str(item.get("subject", {}).get("subject_fingerprint") or "") for item in items
    )
    latest_by_subject: dict[str, str] = {}
    latest_by_sql: dict[str, str] = {}
    for item in items:
        inspection_id = str(item.get("inspection_id") or "")
        subject_key = str(item.get("subject", {}).get("subject_fingerprint") or "")
        sql_key = str(item.get("sql_sha256") or "")
        if subject_key:
            item["subject_observation_count"] = subject_counts[subject_key]
            item["is_latest_for_subject"] = subject_key not in latest_by_subject
            latest_by_subject.setdefault(subject_key, inspection_id)
        if sql_key:
            if sql_key in latest_by_sql:
                item["exact_duplicate_of"] = latest_by_sql[sql_key]
            else:
                latest_by_sql[sql_key] = inspection_id
    index = {
        "contract_version": INDEX_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "item_count": len(items),
        "items": items,
    }
    index_path = root / "dev_inspections" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(index_path, index)
    return index


def load_csv(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        return columns, list(reader)


def load_receipts(
    root: Path,
    implicit_business_scope: dict[str, Any] | None = None,
    project_relation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for path in sorted((root / "dev_inspections").glob("*/*/receipt.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                continue
            if value.get("contract_version") == RECEIPT_VERSION:
                receipts.append(value)
                continue
            sql_file = path.parent / "query.sql"
            result_file = path.parent / "result.csv"
            if value.get("sql_sha256") and value["sql_sha256"] != sha256_file(sql_file):
                continue
            if value.get("result_sha256") and value["result_sha256"] != sha256_file(result_file):
                continue
            columns, rows = load_csv(result_file)
            receipts.append(
                build_receipt(
                    root=root,
                    project_id=str(value.get("project_id") or root.name),
                    command=str(value.get("command") or "query"),
                    database=str(value.get("database") or ""),
                    sql_file=sql_file,
                    result_file=result_file,
                    receipt_file=path,
                    sql=sql_file.read_text(encoding="utf-8-sig"),
                    columns=columns,
                    rows=rows,
                    executed_at=str(value.get("executed_at") or ""),
                    user_request=value.get("user_request") if isinstance(value.get("user_request"), str) else None,
                    legacy_contract_version=str(value.get("contract_version") or "unknown"),
                    implicit_business_scope=implicit_business_scope,
                    project_relation=project_relation,
                )
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return receipts


def migrate_history(root: Path, project: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    inspection_root = root / "dev_inspections"
    candidates = sorted(inspection_root.glob("*/*/receipt.json"))
    changed: list[tuple[Path, dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    migrated_count = 0
    refreshed_count = 0
    unchanged_count = 0
    blockers: list[str] = []
    profile = (
        project.get("resolved_development_profile")
        if isinstance(project.get("resolved_development_profile"), dict)
        else {}
    )
    database = str(profile.get("database") or "")
    implicit_business_scope = (
        profile.get("implicit_business_scope")
        if isinstance(profile.get("implicit_business_scope"), dict)
        else None
    )
    project_relation = profile.get("project_relation") if isinstance(profile.get("project_relation"), dict) else None
    project_id = str(project.get("project_id") or root.name)
    for receipt_path in candidates:
        try:
            old = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(old, dict):
                raise ValueError("receipt is not an object")
            sql_file = receipt_path.parent / "query.sql"
            result_file = receipt_path.parent / "result.csv"
            if not sql_file.exists() or not result_file.exists():
                raise ValueError("query.sql or result.csv is missing")
            if old.get("sql_sha256") and old["sql_sha256"] != sha256_file(sql_file):
                raise ValueError("query.sql hash does not match the legacy receipt")
            if old.get("result_sha256") and old["result_sha256"] != sha256_file(result_file):
                raise ValueError("result.csv hash does not match the legacy receipt")
            columns, rows = load_csv(result_file)
            upgraded = build_receipt(
                root=root,
                project_id=str(old.get("project_id") or project_id),
                command=str(old.get("command") or "query"),
                database=str(old.get("database") or database),
                sql_file=sql_file,
                result_file=result_file,
                receipt_file=receipt_path,
                sql=sql_file.read_text(encoding="utf-8-sig"),
                columns=columns,
                rows=rows,
                executed_at=str(old.get("executed_at") or ""),
                user_request=old.get("user_request") if isinstance(old.get("user_request"), str) else None,
                legacy_contract_version=(
                    str(old.get("contract_version") or "unknown")
                    if old.get("contract_version") != RECEIPT_VERSION
                    else None
                ),
                implicit_business_scope=implicit_business_scope,
                project_relation=project_relation,
            )
            if isinstance(old.get("migration"), dict) and "migration" not in upgraded:
                upgraded["migration"] = old["migration"]
            if upgraded != old:
                changed.append((receipt_path, upgraded))
                if old.get("contract_version") == RECEIPT_VERSION:
                    refreshed_count += 1
                else:
                    migrated_count += 1
            else:
                unchanged_count += 1
            current.append(upgraded)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"{project_relative(root, receipt_path)}: {exc}")
    if blockers:
        return {
            "status": "blocked",
            "dry_run": dry_run,
            "candidate_count": len(candidates),
            "migrated_count": 0,
            "blockers": blockers,
        }
    if not dry_run:
        for path, receipt in changed:
            write_json_atomic(path, receipt)
        index = write_index(root, current)
    else:
        index = {"item_count": len(current)}
    return {
        "status": "ready",
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        "migrated_count": migrated_count,
        "refreshed_count": refreshed_count,
        "unchanged_count": unchanged_count,
        "index_item_count": index.get("item_count", 0),
        "index_file": str((inspection_root / "index.json").resolve()),
        "blockers": [],
    }


def search_history(
    root: Path,
    *,
    search: str | None,
    command: str | None,
    table: str | None,
    field: str | None,
    latest_only: bool,
    limit: int,
) -> dict[str, Any]:
    index_path = root / "dev_inspections" / "index.json"
    if not index_path.exists():
        return {"status": "ready", "match_count": 0, "items": [], "index_file": str(index_path.resolve())}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    items = index.get("items") if isinstance(index, dict) else []
    if not isinstance(items, list):
        items = []
    token = str(search or "").strip().lower()
    table_token = str(table or "").strip().lower()
    field_token = str(field or "").strip().lower()
    matches: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject") if isinstance(item.get("subject"), dict) else {}
        if token and token not in str(item.get("search_text") or ""):
            continue
        if command and item.get("command") != command:
            continue
        if table_token and not any(table_token in str(value).lower() for value in subject.get("tables") or []):
            continue
        if field_token and field_token not in str(subject.get("field") or "").lower():
            continue
        if latest_only and subject.get("subject_fingerprint") and item.get("is_latest_for_subject") is False:
            continue
        matches.append(item)
    return {
        "status": "ready",
        "match_count": len(matches),
        "items": matches[: max(1, min(limit, 100))],
        "index_file": str(index_path.resolve()),
    }
