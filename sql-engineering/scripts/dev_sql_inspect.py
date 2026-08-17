#!/usr/bin/env python3
"""Run bounded read-only development-database inspections with local receipts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import dev_inspection_catalog as inspection_catalog
import data_service
from asset_provenance import stamp_sql_generation
from capability_registry import command_function_ids
from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from secret_input import prompt_secret


RECEIPT_VERSION = inspection_catalog.RECEIPT_VERSION
ALLOWED_FIRST_KEYWORDS = {"select", "with", "show", "desc", "describe", "explain"}
SERVER_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
BANNED_CODE_PATTERNS = {
    "write statement": r"\b(insert|update|delete|replace|merge)\b",
    "DDL statement": r"\b(create|drop|alter|truncate|rename)\b",
    "privileged statement": r"\b(call|grant|revoke|set|use|load|kill|lock|unlock)\b",
    "file access": r"\binto\s+(outfile|dumpfile)\b|\bload_file\s*\(|\binfile\b",
    "delay function": r"\b(sleep|benchmark)\s*\(",
}
IDENTIFIER_VALUE_RE = inspection_catalog.IDENTIFIER_VALUE_RE
SQL_ALIAS_STOP_WORDS = {
    "where", "join", "left", "right", "inner", "outer", "cross", "on",
    "group", "order", "limit", "union", "having",
}


class InspectionError(ValueError):
    """Raised when an inspection is unsafe or cannot be executed."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InspectionError(f"missing project config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InspectionError(f"invalid project config JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InspectionError(f"project config must be an object: {path}")
    return value


def load_profile(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    project = read_json(root / "project_config.json")
    try:
        resolution = data_service.resolve_from_project_root(root, "development_inspection")
    except data_service.DataServiceError as exc:
        raise InspectionError(str(exc)) from exc
    profile = resolution["profile"]
    required = {
        "host", "port", "username", "database", "password_env", "readonly",
        "allowed_databases", "max_scan_days", "max_result_rows", "enum_top_n",
        "query_timeout_seconds", "results_policy",
    }
    missing = sorted(required - set(profile))
    if missing:
        raise InspectionError("resolved development service is missing: " + ", ".join(missing))
    if profile.get("readonly") is not True:
        raise InspectionError("resolved development service must be readonly")
    if profile.get("results_policy") != "local_ignored":
        raise InspectionError("resolved development results_policy must be local_ignored")
    if profile.get("database") not in profile.get("allowed_databases", []):
        raise InspectionError("resolved development database is not in allowed_databases")
    return project, profile


def mask_comments_and_literals(sql: str, *, preserve_identifiers: bool = False) -> str:
    """Replace comments, strings, and quoted identifiers while preserving code shape."""
    output: list[str] = []
    state = "code"
    quote = ""
    index = 0
    while index < len(sql):
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "line_comment":
            if char == "\n":
                state = "code"
                output.append("\n")
            else:
                output.append(" ")
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and nxt == "/":
                output.extend("  ")
                index += 2
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if state == "quoted":
            output.append("\n" if char == "\n" else " ")
            if char == quote:
                if nxt == quote and quote in {"'", '"', "`"}:
                    output.append(" ")
                    index += 2
                    continue
                state = "code"
            elif char == "\\" and nxt:
                output.append(" ")
                index += 2
                continue
            index += 1
            continue
        if char == "-" and nxt == "-":
            output.extend("  ")
            index += 2
            state = "line_comment"
        elif char == "#":
            output.append(" ")
            index += 1
            state = "line_comment"
        elif char == "/" and nxt == "*":
            output.extend("  ")
            index += 2
            state = "block_comment"
        elif char in {"'", '"'} or (char == "`" and not preserve_identifiers):
            quote = char
            state = "quoted"
            output.append(" ")
            index += 1
        else:
            output.append(char)
            index += 1
    if state in {"block_comment", "quoted"}:
        raise InspectionError("SQL contains an unterminated comment or quoted value")
    return "".join(output)


def configured_time_fields(profile: dict[str, Any]) -> list[str]:
    values = profile.get("time_field_candidates") or [profile.get("date_field")]
    return list(dict.fromkeys(str(item).strip() for item in values if str(item or "").strip()))


def _field_references(sql: str, alias: str, field: str, *, allow_unqualified: bool) -> list[str]:
    code = mask_comments_and_literals(sql, preserve_identifiers=True).replace("`", "").lower()
    refs = [f"{alias.lower()}.{field.lower()}"] if alias else []
    if allow_unqualified:
        refs.append(field.lower())
    return [item for item in refs if item in code]


def _field_is_bounded(sql: str, alias: str, field: str, *, allow_unqualified: bool) -> bool:
    code = mask_comments_and_literals(sql, preserve_identifiers=True).replace("`", "").lower()
    for ref in _field_references(sql, alias, field, allow_unqualified=allow_unqualified):
        escaped = re.escape(ref)
        if re.search(rf"(?<![\w$]){escaped}\s+between\b", code):
            return True
        lower = re.search(rf"(?<![\w$]){escaped}\s*(?:>=|>)", code)
        upper = re.search(rf"(?<![\w$]){escaped}\s*(?:<=|<)", code)
        if lower and upper:
            return True
    return False


def validate_readonly_sql(
    sql: str,
    *,
    profile: dict[str, Any],
    required_time_bounds: list[dict[str, Any]] | None = None,
    server_capabilities: dict[str, Any] | None = None,
) -> list[str]:
    text = str(sql or "").strip()
    if not text:
        return ["SQL is empty"]
    try:
        code = mask_comments_and_literals(text)
    except InspectionError as exc:
        return [str(exc)]
    stripped = code.strip()
    while stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    problems: list[str] = []
    if ";" in stripped:
        problems.append("only one SQL statement is allowed")
    first = re.match(r"[A-Za-z_]+", stripped)
    keyword = first.group(0).lower() if first else ""
    if keyword not in ALLOWED_FIRST_KEYWORDS:
        problems.append("only SELECT/WITH/SHOW/DESCRIBE/EXPLAIN statements are allowed")
    elif keyword == "with" and not bool((server_capabilities or {}).get("supports_cte")):
        version = str((server_capabilities or {}).get("server_version") or "not detected")
        problems.append(
            f"development server {version} does not support WITH/CTE; "
            "rewrite with derived tables or split the inspection"
        )
    for label, pattern in BANNED_CODE_PATTERNS.items():
        if re.search(pattern, code, re.I):
            problems.append(f"blocked {label}")
    lower = code.lower()
    if keyword in {"select", "with"}:
        has_limit = bool(re.search(r"\blimit\s+\d+\b", lower))
        aggregate = bool(re.search(r"\b(count|sum|avg|min|max)\s*\(|\bgroup\s+by\b", lower))
        if not has_limit and not aggregate:
            problems.append("detail inspection SELECT requires an explicit LIMIT")
        limit_match = re.search(r"\blimit\s+(\d+)\b", lower)
        if limit_match and int(limit_match.group(1)) > int(profile["max_result_rows"]):
            problems.append(f"LIMIT exceeds resolved service max_result_rows={profile['max_result_rows']}")
        if required_time_bounds is not None:
            for item in required_time_bounds:
                if not _field_is_bounded(
                    text,
                    str(item.get("alias") or ""),
                    str(item["field"]),
                    allow_unqualified=bool(item.get("allow_unqualified")),
                ):
                    problems.append(
                        f"development inspection must bound `{item['table']}` with "
                        f"`{item['field']}` on both sides"
                    )
        else:
            fields = configured_time_fields(profile)
            identifier_code = mask_comments_and_literals(text, preserve_identifiers=True).lower()
            if "_fht0" in identifier_code and fields and not any(field.lower() in identifier_code for field in fields):
                problems.append("TLOG inspection must bound one of: " + ", ".join(fields))
    return problems


def quote_identifier(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or not re.fullmatch(r"[A-Za-z0-9_$-]+", cleaned):
        raise InspectionError(f"unsafe SQL identifier: {value!r}")
    return "`" + cleaned.replace("`", "``") + "`"


def qualified_table(value: str, profile: dict[str, Any]) -> str:
    parts = [part.strip(" `") for part in str(value or "").split(".")]
    if len(parts) == 1:
        database, table = str(profile["database"]), parts[0]
    elif len(parts) == 2:
        database, table = parts
    else:
        raise InspectionError("table must be TABLE or DATABASE.TABLE")
    if database not in profile["allowed_databases"]:
        raise InspectionError(f"database is not allowed by project config: {database}")
    return f"{quote_identifier(database)}.{quote_identifier(table)}"


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def parse_date_range(args: argparse.Namespace, profile: dict[str, Any]) -> tuple[date, date]:
    yesterday = date.today() - timedelta(days=1)
    end = date.fromisoformat(args.end_date) if args.end_date else yesterday
    lookback = int(profile.get("default_lookback_days") or 1)
    start = date.fromisoformat(args.start_date) if args.start_date else end - timedelta(days=lookback - 1)
    days = (end - start).days + 1
    if days < 1:
        raise InspectionError("start-date must be on or before end-date")
    if days > int(profile["max_scan_days"]):
        raise InspectionError(f"inspection range {days} days exceeds max_scan_days={profile['max_scan_days']}")
    return start, end


def build_enum_sql(args: argparse.Namespace, profile: dict[str, Any]) -> str:
    if IDENTIFIER_VALUE_RE.search(args.field) and not args.allow_identifier_values:
        raise InspectionError("identifier-like fields require --allow-identifier-values and remain local-only")
    start, end = parse_date_range(args, profile)
    table = qualified_table(args.table, profile)
    field = quote_identifier(args.field)
    date_field = quote_identifier(args.date_field or profile.get("date_field") or "dteventdate")
    top = args.top if args.top is not None else int(profile["enum_top_n"])
    if top < 1 or top > int(profile["max_result_rows"]):
        raise InspectionError(f"top must be between 1 and {profile['max_result_rows']}")
    filters = [
        f"{date_field} >= {sql_literal(start.isoformat() + ' 00:00:00')}",
        f"{date_field} < {sql_literal((end + timedelta(days=1)).isoformat() + ' 00:00:00')}",
    ]
    if args.zone_field and args.zone_value is not None:
        filters.append(f"{quote_identifier(args.zone_field)} = {sql_literal(args.zone_value)}")
    where = "\n      AND ".join(filters)
    return (
        "-- Local development inspection; not a formal QUERY asset.\n"
        f"SELECT\n    {field} AS field_value,\n    COUNT(*) AS row_count\n"
        f"FROM {table}\nWHERE {where}\n"
        f"GROUP BY {field}\nORDER BY row_count DESC\nLIMIT {top};\n"
    )


def build_command_sql(args: argparse.Namespace, profile: dict[str, Any], root: Path | None = None) -> str:
    if args.command == "tables":
        sql = f"SHOW TABLES FROM {quote_identifier(profile['database'])}"
        if args.like:
            sql += f" LIKE {sql_literal(args.like)}"
        return sql + ";\n"
    if args.command == "describe":
        return f"DESCRIBE {qualified_table(args.table, profile)};\n"
    if args.command == "enum":
        return build_enum_sql(args, profile)
    if args.command == "query":
        source = Path(args.sql_file).resolve()
        if root is None:
            raise InspectionError("project root is required for custom SQL intake")
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise InspectionError("custom SQL must be copied into the project root before execution") from exc
        return source.read_text(encoding="utf-8-sig")
    raise InspectionError(f"unsupported command: {args.command}")


def user_environment_value(name: str) -> str:
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


def get_password(profile: dict[str, Any], prompt: bool) -> str:
    env_name = str(profile["password_env"])
    value = user_environment_value(env_name)
    if not value and prompt:
        value = prompt_secret(f"Password for {profile['username']}@{profile['host']}: ")
    if not value:
        raise InspectionError(f"missing local credential environment variable: {env_name}")
    return value


def connect(profile: dict[str, Any], password: str):
    try:
        import pymysql
    except ImportError as exc:
        raise InspectionError("PyMySQL is required for development inspection") from exc
    timeout = int(profile["query_timeout_seconds"])
    return pymysql.connect(
        host=str(profile["host"]),
        port=int(profile["port"]),
        user=str(profile["username"]),
        password=password,
        database=str(profile["database"]),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=min(timeout, 15),
        read_timeout=timeout,
        write_timeout=timeout,
        cursorclass=pymysql.cursors.DictCursor,
    )


def execute_sql(profile: dict[str, Any], password: str, sql: str) -> tuple[list[str], list[dict[str, Any]]]:
    connection = connect(profile, password)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [str(item[0]) for item in (cursor.description or [])]
            rows = list(cursor.fetchmany(int(profile["max_result_rows"]) + 1)) if columns else []
    finally:
        connection.close()
    if len(rows) > int(profile["max_result_rows"]):
        raise InspectionError("result exceeds configured max_result_rows")
    return columns, rows


def server_capabilities_from_version(server_version: str, configured_engine: str = "") -> dict[str, Any]:
    value = str(server_version or "").strip()
    match = SERVER_VERSION_RE.search(value)
    if not match:
        raise InspectionError(f"unable to parse development server version: {value!r}")
    version = tuple(int(item or 0) for item in match.groups())
    lower = value.casefold()
    engine = str(configured_engine or "").casefold()
    if "starrocks" in lower or "starrocks" in engine:
        product = "starrocks"
        supports_cte = True
        basis = "StarRocks supports common table expressions"
    elif "mariadb" in lower:
        product = "mariadb"
        supports_cte = version >= (10, 2, 1)
        basis = "MariaDB CTE support starts at 10.2.1"
    else:
        product = "mysql"
        supports_cte = version >= (8, 0, 1)
        basis = "MySQL CTE support starts at 8.0.1"
    return {
        "contract_version": "development_server_capabilities_v1",
        "detection_method": "select_version",
        "server_version": value,
        "product": product,
        "version": {"major": version[0], "minor": version[1], "patch": version[2]},
        "supports_cte": supports_cte,
        "cte_support_basis": basis,
    }


def detect_server_capabilities(
    profile: dict[str, Any],
    password: str,
) -> dict[str, Any]:
    columns, rows = execute_sql(profile, password, "SELECT VERSION() AS server_version")
    if not rows:
        raise InspectionError("SELECT VERSION() returned no development server version")
    row = rows[0]
    key = next((item for item in columns if item.casefold() == "server_version"), columns[0] if columns else "")
    if not key or row.get(key) is None:
        raise InspectionError("SELECT VERSION() did not return a readable server version")
    return server_capabilities_from_version(str(row[key]), str(profile.get("engine") or ""))


def describe_table_fields(profile: dict[str, Any], password: str, table: str) -> list[str]:
    columns, rows = execute_sql(profile, password, f"DESCRIBE {qualified_table(table, profile)}")
    field_column = next((item for item in columns if item.lower() in {"field", "column_name"}), "")
    if not field_column:
        raise InspectionError(f"DESCRIBE did not return a field column for {table}")
    return [str(row[field_column]) for row in rows if row.get(field_column) is not None]


def resolve_time_field(
    available_fields: list[str],
    profile: dict[str, Any],
    requested: str | None = None,
) -> str:
    available = {item.casefold(): item for item in available_fields}
    candidates = [requested] if requested else configured_time_fields(profile)
    selected = next((available[str(item).casefold()] for item in candidates if str(item).casefold() in available), "")
    if selected:
        return selected
    label = requested or ", ".join(configured_time_fields(profile))
    raise InspectionError(f"table has no usable time field from [{label}]")


def sql_physical_sources(sql: str) -> list[dict[str, str]]:
    code = mask_comments_and_literals(sql, preserve_identifiers=True)
    ctes = {
        match.group(1).strip("`").casefold()
        for match in re.finditer(r"(?:\bwith|,)\s*(`?[A-Za-z_][\w$-]*`?)\s+as\s*\(", code, re.I)
    }
    token = r"(?:`[^`]+`|[A-Za-z0-9_$-]+)(?:\.(?:`[^`]+`|[A-Za-z0-9_$-]+))?"
    rows: list[dict[str, str]] = []
    for match in re.finditer(rf"\b(?:from|join)\s+({token})(?:\s+(?:as\s+)?(`?[A-Za-z_][\w$-]*`?))?", code, re.I):
        table = match.group(1).replace("`", "")
        base = table.rsplit(".", 1)[-1]
        if base.casefold() in ctes:
            continue
        explicit_alias = (match.group(2) or "").strip("`")
        if explicit_alias.casefold() in SQL_ALIAS_STOP_WORDS:
            explicit_alias = ""
        rows.append({"table": table, "alias": explicit_alias or base, "explicit_alias": explicit_alias})
    return rows


def resolve_query_time_bounds(
    sql: str,
    profile: dict[str, Any],
    field_lookup,
) -> list[dict[str, Any]]:
    sources = sql_physical_sources(sql)
    requirements: list[dict[str, Any]] = []
    for source in sources:
        available = field_lookup(source["table"])
        eligible = [
            field for field in configured_time_fields(profile)
            if field.casefold() in {item.casefold() for item in available}
        ]
        if not eligible:
            continue
        allow_unqualified = len(sources) == 1 or not source["explicit_alias"]
        selected = next(
            (
                resolve_time_field(available, profile, field)
                for field in eligible
                if _field_references(sql, source["alias"], field, allow_unqualified=allow_unqualified)
            ),
            resolve_time_field(available, profile),
        )
        requirements.append(
            {
                "table": source["table"],
                "alias": source["alias"],
                "field": selected,
                "allow_unqualified": allow_unqualified,
            }
        )
    return requirements


def serialize_cell(value: Any) -> Any:
    return inspection_catalog.serialize_cell(value)


def sha256_file(path: Path) -> str:
    return inspection_catalog.sha256_file(path)


def write_artifacts(
    root: Path,
    project: dict[str, Any],
    profile: dict[str, Any],
    command: str,
    sql: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    user_request: str | None = None,
    server_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sql = stamp_sql_generation(root, sql)
    now = datetime.now(timezone.utc).astimezone()
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    inspection_id = f"{now:%H%M%S}-{command}-{digest[:8]}"
    folder = root / "dev_inspections" / f"{now:%Y%m%d}" / inspection_id
    folder.mkdir(parents=True, exist_ok=False)
    sql_file = folder / "query.sql"
    result_file = folder / "result.csv"
    receipt_file = folder / "receipt.json"
    sql_file.write_text(sql.rstrip() + "\n", encoding="utf-8")
    with result_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if columns:
            writer.writeheader()
            for row in rows:
                writer.writerow({key: serialize_cell(row.get(key)) for key in columns})
    receipt = inspection_catalog.build_receipt(
        root=root,
        project_id=str(project.get("project_id") or root.name),
        command=command,
        database=str(profile["database"]),
        sql_file=sql_file,
        result_file=result_file,
        receipt_file=receipt_file,
        sql=sql,
        columns=columns,
        rows=rows,
        executed_at=now.isoformat(timespec="seconds"),
        user_request=user_request,
        implicit_business_scope=(
            profile.get("implicit_business_scope")
            if isinstance(profile.get("implicit_business_scope"), dict)
            else None
        ),
        project_relation=(
            profile.get("project_relation")
            if isinstance(profile.get("project_relation"), dict)
            else None
        ),
        server_capabilities=server_capabilities,
    )
    inspection_catalog.write_json_atomic(receipt_file, receipt)
    inspection_catalog.write_index(
        root,
        inspection_catalog.load_receipts(
            root,
            profile.get("implicit_business_scope")
            if isinstance(profile.get("implicit_business_scope"), dict)
            else None,
            profile.get("project_relation")
            if isinstance(profile.get("project_relation"), dict)
            else None,
        ),
    )
    return receipt


def sanitize_error(exc: Exception, password: str = "") -> str:
    message = str(exc)
    return message.replace(password, "<redacted>") if password else message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="SQL project root containing project_config.json")
    parser.add_argument("--prompt-password", action="store_true", help="Prompt with * masking when the password environment variable is absent")
    parser.add_argument("--format", choices=["json", "summary"], default="json")
    add_function_gate_arguments(parser, selection_help="Optional explicit DEV_SQL_INSPECT route")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    tables = sub.add_parser("tables")
    tables.add_argument("--like")
    describe = sub.add_parser("describe")
    describe.add_argument("--table", required=True)
    enum = sub.add_parser("enum")
    enum.add_argument("--table", required=True)
    enum.add_argument("--field", required=True)
    enum.add_argument("--start-date")
    enum.add_argument("--end-date")
    enum.add_argument("--date-field")
    enum.add_argument("--top", type=int)
    enum.add_argument("--zone-field")
    enum.add_argument("--zone-value")
    enum.add_argument("--allow-identifier-values", action="store_true")
    query = sub.add_parser("query")
    query.add_argument("--sql-file", required=True)
    history = sub.add_parser("history")
    history.add_argument("--search")
    history.add_argument("--inspection-command", choices=["tables", "describe", "enum", "query"])
    history.add_argument("--table")
    history.add_argument("--field")
    history.add_argument("--all-observations", action="store_true")
    history.add_argument("--limit", type=int, default=20)
    migrate = sub.add_parser("migrate-history")
    migrate.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("dev_sql_inspect.py", args.command),
            purpose="development SQL inspection",
        )
        if args.command not in {"ping", "history"}:
            require_user_request(args.user_request, purpose="development SQL inspection artifact write")
        root = Path(args.root).resolve()
        if args.command == "history":
            output = inspection_catalog.search_history(
                root,
                search=args.search,
                command=args.inspection_command,
                table=args.table,
                field=args.field,
                latest_only=not args.all_observations,
                limit=args.limit,
            )
        elif args.command == "migrate-history":
            project, profile = load_profile(root)
            project = {**project, "resolved_development_profile": profile}
            output = inspection_catalog.migrate_history(root, project, dry_run=args.dry_run)
        else:
            project, profile = load_profile(root)
            password = get_password(profile, args.prompt_password)
            server_capabilities = detect_server_capabilities(profile, password)
        if args.command == "ping":
            output = {
                "status": "ready",
                "database": profile["database"],
                "readonly": True,
                "server_capabilities": server_capabilities,
            }
        elif args.command not in {"history", "migrate-history"}:
            required_time_bounds = None
            if args.command == "enum":
                args.date_field = resolve_time_field(
                    describe_table_fields(profile, password, args.table),
                    profile,
                    args.date_field,
                )
            sql = build_command_sql(args, profile, root)
            if args.command == "query":
                preliminary_problems = validate_readonly_sql(
                    sql,
                    profile=profile,
                    required_time_bounds=[],
                    server_capabilities=server_capabilities,
                )
                if preliminary_problems:
                    raise InspectionError("; ".join(preliminary_problems))
            if args.command == "enum":
                required_time_bounds = [
                    {
                        "table": args.table,
                        "alias": "",
                        "field": args.date_field,
                        "allow_unqualified": True,
                    }
                ]
            elif args.command == "query":
                required_time_bounds = resolve_query_time_bounds(
                    sql,
                    profile,
                    lambda table: describe_table_fields(profile, password, table),
                )
            problems = validate_readonly_sql(
                sql,
                profile=profile,
                required_time_bounds=required_time_bounds,
                server_capabilities=server_capabilities,
            )
            if problems:
                raise InspectionError("; ".join(problems))
            columns, rows = execute_sql(profile, password, sql)
            output = write_artifacts(
                root,
                project,
                profile,
                args.command,
                sql,
                columns,
                rows,
                user_request=args.user_request,
                server_capabilities=server_capabilities,
            )
    except (InspectionError, FunctionGateError, OSError, ValueError) as exc:
        message = sanitize_error(exc, locals().get("password", ""))
        if isinstance(exc, FunctionGateError):
            exit_with_gate_error(parser, exc)
        output = {"status": "blocked", "error": message}
    if args.format == "summary":
        print(f"status={output.get('status')} rows={output.get('row_count', 0)}")
        if output.get("sql_file"):
            print(f"sql_file={output['sql_file']}")
            print(f"result_file={output['result_file']}")
        if output.get("error"):
            print(f"error={output['error']}")
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    if output.get("status") != "ready":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
