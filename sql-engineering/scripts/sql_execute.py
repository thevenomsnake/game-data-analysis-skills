#!/usr/bin/env python3
"""Execute one saved SQL version through a project-configured database connection."""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sql_workspace import delivery_receipt, load_project, read_json, sha256_bytes, write_json


CONNECTION_SCHEMA = "sql_engineering_connections_v1"
EXECUTION_RECEIPT_SCHEMA = "sql_execution_receipt_v1"
ALLOWED_FIRST_TOKENS = {"select", "with", "show", "describe", "desc", "explain", "pragma"}
MUTATING_TOKENS = {
    "insert",
    "update",
    "delete",
    "merge",
    "create",
    "alter",
    "drop",
    "truncate",
    "grant",
    "revoke",
    "call",
    "load",
    "copy",
    "replace",
}


class ManualExecutionRequired(ValueError):
    """Raised when automatic execution is not configured or unavailable."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mask_comments_and_strings(sql: str) -> str:
    output: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "code":
            if char == "-" and next_char == "-":
                output.extend("  ")
                index += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                output.extend("  ")
                index += 2
                state = "block_comment"
                continue
            if char in {"'", '"', "`"}:
                quote = char
                output.append(" ")
                index += 1
                state = "string"
                continue
            output.append(char)
            index += 1
            continue
        if state == "line_comment":
            output.append("\n" if char == "\n" else " ")
            index += 1
            if char == "\n":
                state = "code"
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                output.extend("  ")
                index += 2
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        output.append(" ")
        if char == quote:
            if next_char == quote:
                output.append(" ")
                index += 2
            else:
                index += 1
                state = "code"
        else:
            index += 1
    return "".join(output)


def require_read_only_sql(sql: str) -> None:
    cleaned = mask_comments_and_strings(sql).strip()
    if not cleaned:
        raise ValueError("SQL is empty after removing comments")
    without_trailing = cleaned.rstrip().removesuffix(";").rstrip()
    if ";" in without_trailing:
        raise ValueError("Automatic execution accepts exactly one read-only SQL statement")
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", without_trailing.lower())
    if not tokens or tokens[0] not in ALLOWED_FIRST_TOKENS:
        raise ValueError("Automatic execution accepts only read-only SELECT/SHOW/DESCRIBE/EXPLAIN statements")
    if tokens[0] in {"select", "with", "explain"}:
        mutation = next((token for token in tokens if token in MUTATING_TOKENS), None)
        if mutation:
            raise ValueError(f"Automatic execution blocks mutating SQL token: {mutation}")


def manual_receipt(delivery: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "schema_version": EXECUTION_RECEIPT_SCHEMA,
        "status": "manual_required",
        "delivery_file": delivery.get("delivery_file"),
        "project_relative_path": delivery.get("project_relative_path"),
        "content_sha256": delivery.get("content_sha256"),
        "reason": reason,
        "next_action": "Run the saved SQL in your database environment and return the result file.",
    }


def load_connections(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if value.get("schema_version") != CONNECTION_SCHEMA:
        raise ValueError(f"Unsupported connections schema in {path}")
    if not isinstance(value.get("profiles"), dict):
        raise ValueError(f"Connection profiles must be an object: {path}")
    return value


def resolve_connections_path(root: Path, requested: str) -> Path:
    if requested:
        return Path(requested).expanduser().resolve()
    environment_path = os.environ.get("SQL_ENGINEERING_CONNECTIONS_FILE")
    if environment_path:
        return Path(environment_path).expanduser().resolve()
    return (root / ".sql-engineering" / "connections.local.json").resolve()


def select_environment(
    config: dict[str, Any], meta: dict[str, Any], requested: str
) -> tuple[str, dict[str, Any]]:
    execution = config.get("execution")
    if not isinstance(execution, dict):
        raise ManualExecutionRequired("project_has_no_execution_configuration")
    environments = execution.get("environments")
    if not isinstance(environments, dict) or not environments:
        raise ManualExecutionRequired("project_has_no_execution_environments")
    name = requested.strip() or str(meta.get("execution_environment") or "").strip()
    if not name:
        name = str(execution.get("default_environment") or "").strip()
    if not name:
        raise ManualExecutionRequired("project_has_no_default_execution_environment")
    environment = environments.get(name)
    if not isinstance(environment, dict):
        raise ValueError(f"Unknown execution environment: {name}")
    return name, environment


def require_local_secret_mapping(profile: dict[str, Any]) -> None:
    connect = profile.get("connect")
    if not isinstance(connect, dict):
        return
    forbidden = {"password", "passwd", "token", "secret", "api_key", "apikey"}
    exposed = sorted(key for key in connect if key.lower() in forbidden and connect.get(key) not in (None, ""))
    if exposed:
        raise ValueError("Connection secrets must use secret_env, not literal connect values: " + ", ".join(exposed))


def resolve_secret_values(mapping: Any) -> dict[str, str]:
    if mapping in (None, {}):
        return {}
    if not isinstance(mapping, dict):
        raise ValueError("secret_env must be an object")
    values: dict[str, str] = {}
    missing: list[str] = []
    for target, source in mapping.items():
        source_name = str(source).strip()
        value = os.environ.get(source_name)
        if value is None:
            missing.append(source_name)
        else:
            values[str(target)] = value
    if missing:
        raise ManualExecutionRequired("missing_secret_environment_variables:" + ",".join(sorted(missing)))
    return values


def stringify_row(row: Iterable[Any]) -> list[Any]:
    return ["" if value is None else value for value in row]


def execute_dbapi(
    profile: dict[str, Any], sql: str, result_path: Path, max_rows: int
) -> tuple[list[str], int, bool]:
    module_name = str(profile.get("module") or "").strip()
    if not module_name:
        raise ValueError("DB-API profile requires module")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ManualExecutionRequired(f"dbapi_module_not_installed:{module_name}") from exc

    require_local_secret_mapping(profile)
    connect = dict(profile.get("connect") or {})
    connect.update(resolve_secret_values(profile.get("secret_env")))
    connection = module.connect(**connect)
    cursor = None
    row_count = 0
    truncated = False
    columns: list[str] = []
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        description = cursor.description or []
        columns = [str(column[0]) for column in description]
        if not columns:
            raise ValueError("Read-only query did not return a result set")
        with result_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            while row_count < max_rows:
                remaining = max_rows - row_count
                batch = cursor.fetchmany(min(1000, remaining + 1))
                if not batch:
                    break
                if len(batch) > remaining:
                    batch = batch[:remaining]
                    truncated = True
                writer.writerows(stringify_row(row) for row in batch)
                row_count += len(batch)
                if truncated:
                    break
            if row_count >= max_rows and not truncated:
                extra = cursor.fetchone()
                truncated = extra is not None
    finally:
        if cursor is not None:
            cursor.close()
        try:
            connection.rollback()
        except Exception:
            pass
        connection.close()
    return columns, row_count, truncated


def execute_cli(
    profile: dict[str, Any], query_path: Path, result_path: Path, max_rows: int
) -> tuple[list[str], int, bool]:
    program = str(profile.get("program") or "").strip()
    arguments = profile.get("arguments")
    if not program or not isinstance(arguments, list):
        raise ValueError("CLI profile requires program and an arguments array")
    if not bool(profile.get("read_only")):
        raise ValueError("CLI execution profile must declare read_only=true")
    executable = shutil.which(program)
    if executable is None:
        raise ManualExecutionRequired(f"database_cli_not_installed:{program}")

    command = [executable]
    uses_sql_file = False
    for argument in arguments:
        text = str(argument)
        uses_sql_file = uses_sql_file or "{sql_file}" in text
        command.append(text.replace("{sql_file}", str(query_path)))

    environment = os.environ.copy()
    literal_environment = profile.get("environment") or {}
    if not isinstance(literal_environment, dict):
        raise ValueError("CLI environment must be an object")
    environment.update({str(key): str(value) for key, value in literal_environment.items()})
    environment.update(resolve_secret_values(profile.get("secret_environment")))
    output_format = str(profile.get("output_format") or "tsv").strip().lower()
    if output_format not in {"csv", "tsv"}:
        raise ValueError("CLI output_format must be csv or tsv")
    delimiter = "," if output_format == "csv" else "\t"
    stderr_path = result_path.with_name("cli.stderr.tmp")
    stdin_stream = None if uses_sql_file else query_path.open("r", encoding="utf-8")
    row_count = 0
    truncated = False
    columns: list[str] = []
    try:
        with stderr_path.open("w", encoding="utf-8") as stderr_stream:
            process = subprocess.Popen(
                command,
                stdin=stdin_stream,
                stdout=subprocess.PIPE,
                stderr=stderr_stream,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
            assert process.stdout is not None
            reader = csv.reader(process.stdout, delimiter=delimiter)
            with result_path.open("w", encoding="utf-8-sig", newline="") as result_stream:
                writer = csv.writer(result_stream)
                try:
                    columns = next(reader)
                except StopIteration:
                    columns = []
                if columns:
                    writer.writerow(columns)
                    for row in reader:
                        if row_count >= max_rows:
                            truncated = True
                            process.terminate()
                            break
                        writer.writerow(row)
                        row_count += 1
            process.stdout.close()
            try:
                return_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait()
        stderr_text = stderr_path.read_text(encoding="utf-8").strip()
        if not columns:
            raise ValueError(stderr_text or "Database CLI returned no tabular output")
        if return_code != 0 and not truncated:
            raise RuntimeError(stderr_text or f"database CLI exited with {return_code}")
        return columns, row_count, truncated
    finally:
        if stdin_stream is not None:
            stdin_stream.close()
        stderr_path.unlink(missing_ok=True)


def command_run(args: argparse.Namespace) -> dict[str, Any]:
    root, config, _ = load_project(Path(args.root))
    delivery = delivery_receipt(root, Path(args.sql_file))
    if delivery.get("status") != "ready":
        raise ValueError("SQL delivery receipt is blocked")
    sql_path = Path(str(delivery["delivery_file"]))
    meta = read_json(sql_path.with_suffix(".meta.json"))

    try:
        environment_name, environment = select_environment(config, meta, args.environment)
    except ManualExecutionRequired as exc:
        return manual_receipt(delivery, str(exc))

    selected_dialect = str(environment.get("dialect") or "").strip().lower()
    saved_dialect = str(meta.get("dialect") or "").strip().lower()
    if not selected_dialect:
        raise ValueError(f"Execution environment has no dialect: {environment_name}")
    if selected_dialect != saved_dialect:
        raise ValueError(
            f"Saved SQL dialect {saved_dialect!r} does not match environment dialect {selected_dialect!r}"
        )

    profile_name = str(environment.get("connection_profile") or "").strip()
    if not profile_name:
        raise ValueError(f"Execution environment has no connection_profile: {environment_name}")
    connections_path = resolve_connections_path(root, args.connections_file)
    if not connections_path.is_file():
        return manual_receipt(delivery, "local_connection_configuration_not_found")
    connections = load_connections(connections_path)
    profile = connections["profiles"].get(profile_name)
    if not isinstance(profile, dict):
        raise ValueError(f"Connection profile not found: {profile_name}")
    if not bool(profile.get("read_only")):
        raise ValueError("Automatic execution requires a profile with read_only=true")
    method = str(profile.get("method") or "").strip().lower()
    if method not in {"dbapi", "cli"}:
        raise ValueError("Connection method must be dbapi or cli; browser execution is not supported")

    sql = sql_path.read_text(encoding="utf-8")
    require_read_only_sql(sql)
    max_rows = int(args.max_rows)
    if max_rows < 1:
        raise ValueError("max-rows must be positive")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + str(delivery["content_sha256"])[:8]
    asset_key = re.sub(r"[^A-Za-z0-9._-]+", "-", str(delivery.get("asset_id") or sql_path.stem)).strip("-")
    run_dir = root / ".sql-engineering" / "runs" / asset_key / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    query_copy = run_dir / "query.sql"
    result_path = run_dir / "result.csv"
    receipt_path = run_dir / "receipt.json"
    shutil.copyfile(sql_path, query_copy)
    started_at = utc_now()
    try:
        if method == "dbapi":
            columns, row_count, truncated = execute_dbapi(profile, sql, result_path, max_rows)
        else:
            columns, row_count, truncated = execute_cli(profile, query_copy, result_path, max_rows)
    except ManualExecutionRequired as exc:
        shutil.rmtree(run_dir)
        return manual_receipt(delivery, str(exc))
    except Exception as exc:
        failure = {
            "schema_version": EXECUTION_RECEIPT_SCHEMA,
            "status": "failed",
            "asset_id": delivery.get("asset_id"),
            "environment": environment_name,
            "connection_profile": profile_name,
            "connection_method": method,
            "delivery_file": str(sql_path),
            "query_file": str(query_copy),
            "content_sha256": delivery.get("content_sha256"),
            "started_at": started_at,
            "completed_at": utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        write_json(receipt_path, failure)
        return failure

    receipt = {
        "schema_version": EXECUTION_RECEIPT_SCHEMA,
        "status": "ready",
        "asset_id": delivery.get("asset_id"),
        "environment": environment_name,
        "dialect": selected_dialect,
        "connection_profile": profile_name,
        "connection_method": method,
        "delivery_file": str(sql_path),
        "query_file": str(query_copy),
        "result_file": str(result_path),
        "receipt_file": str(receipt_path),
        "content_sha256": delivery.get("content_sha256"),
        "result_sha256": sha256_bytes(result_path.read_bytes()),
        "columns": columns,
        "row_count": row_count,
        "truncated": truncated,
        "max_rows": max_rows,
        "started_at": started_at,
        "completed_at": utc_now(),
    }
    write_json(receipt_path, receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--root", required=True)
    run.add_argument("--sql-file", required=True)
    run.add_argument("--environment", default="")
    run.add_argument("--connections-file", default="")
    run.add_argument("--max-rows", type=int, default=100000)
    run.set_defaults(handler=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except ValueError as exc:
        result = {"status": "blocked", "error": str(exc)}
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0 if result.get("status") in {"ready", "manual_required"} else 2


if __name__ == "__main__":
    sys.exit(main())
