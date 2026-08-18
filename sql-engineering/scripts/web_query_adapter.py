#!/usr/bin/env python3
"""Validate and resolve a local web-query adapter for QUERY_EXECUTE."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = "web_query_adapter_v1"
DEFAULT_RELATIVE = Path(".sql-engineering") / "web-query-adapter.local.json"
EXAMPLE_RELATIVE = Path("sql-engineering") / "assets" / "examples" / "web-query-adapter.deltaverse.json"
LOCATOR_STRATEGIES = {"role", "label", "text", "css"}
FORBIDDEN_KEY = re.compile(r"(?:password|passwd|token|cookie|secret|authorization|credential)", re.I)
TOP_LEVEL_FIELDS = {
    "schema_version",
    "adapter_id",
    "display_name",
    "allowed_hosts",
    "entry",
    "editor",
    "submit",
    "completion",
    "result",
    "policy",
    "tab_policy",
}


class WebQueryAdapterError(ValueError):
    """Raised when a web-query adapter cannot be used safely."""


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WebQueryAdapterError(f"{field} must be an object.")
    return value


def _reject_secret_fields(value: Any, field: str = "adapter") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if FORBIDDEN_KEY.search(str(key)):
                raise WebQueryAdapterError(f"{field}.{key} is a forbidden credential field.")
            _reject_secret_fields(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{field}[{index}]")


def _locator(value: Any, field: str) -> dict[str, Any]:
    locator = _object(value, field)
    strategy = str(locator.get("strategy") or "")
    text = str(locator.get("value") or "")
    if strategy not in LOCATOR_STRATEGIES:
        raise WebQueryAdapterError(f"{field}.strategy is not supported: {strategy}")
    if not text.strip():
        raise WebQueryAdapterError(f"{field}.value is required.")
    if set(locator) - {"strategy", "value", "exact"}:
        raise WebQueryAdapterError(f"{field} contains unsupported fields.")
    if "exact" in locator and not isinstance(locator["exact"], bool):
        raise WebQueryAdapterError(f"{field}.exact must be boolean.")
    return locator


def _url(value: Any, field: str, allowed_hosts: set[str], allow_http: bool) -> str:
    clean = str(value or "").strip()
    parsed = urlsplit(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebQueryAdapterError(f"{field} must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise WebQueryAdapterError(f"{field} must not contain credentials.")
    host = parsed.hostname.lower()
    if host not in allowed_hosts:
        raise WebQueryAdapterError(f"{field} host is not listed in allowed_hosts: {host}")
    if parsed.scheme == "http" and not allow_http and host not in {"127.0.0.1", "localhost"}:
        raise WebQueryAdapterError(f"{field} requires HTTPS unless policy.allow_http=true.")
    return clean


def validate_adapter(value: Any) -> dict[str, Any]:
    adapter = _object(value, "adapter")
    _reject_secret_fields(adapter)
    unknown = set(adapter) - TOP_LEVEL_FIELDS
    if unknown:
        raise WebQueryAdapterError(f"adapter contains unsupported fields: {', '.join(sorted(unknown))}")
    if adapter.get("schema_version") != SCHEMA_VERSION:
        raise WebQueryAdapterError(f"schema_version must be {SCHEMA_VERSION}.")
    adapter_id = str(adapter.get("adapter_id") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", adapter_id):
        raise WebQueryAdapterError("adapter_id must use lowercase letters, digits, and hyphens.")
    if not str(adapter.get("display_name") or "").strip():
        raise WebQueryAdapterError("display_name is required.")

    hosts = adapter.get("allowed_hosts")
    if not isinstance(hosts, list) or not hosts:
        raise WebQueryAdapterError("allowed_hosts must be a non-empty array.")
    allowed_hosts = {str(host).strip().lower() for host in hosts}
    if len(allowed_hosts) != len(hosts) or any(not re.fullmatch(r"[a-z0-9.-]+", host) for host in allowed_hosts):
        raise WebQueryAdapterError("allowed_hosts must contain unique hostnames without schemes or paths.")

    policy = _object(adapter.get("policy"), "policy")
    if policy.get("authentication") != "user_session":
        raise WebQueryAdapterError("policy.authentication must be user_session.")
    if policy.get("read_only") is not True or policy.get("submit_once") is not True:
        raise WebQueryAdapterError("policy.read_only and policy.submit_once must be true.")
    allow_http = policy.get("allow_http")
    if not isinstance(allow_http, bool):
        raise WebQueryAdapterError("policy.allow_http must be boolean.")

    entry = _object(adapter.get("entry"), "entry")
    _url(entry.get("root_url"), "entry.root_url", allowed_hosts, allow_http)
    _url(entry.get("query_url"), "entry.query_url", allowed_hosts, allow_http)
    _locator(adapter.get("editor"), "editor")
    _locator(adapter.get("submit"), "submit")

    completion = _object(adapter.get("completion"), "completion")
    success = completion.get("success")
    failure = completion.get("failure")
    if not isinstance(success, list) or not success:
        raise WebQueryAdapterError("completion.success must contain at least one locator.")
    if not isinstance(failure, list):
        raise WebQueryAdapterError("completion.failure must be an array.")
    for index, locator in enumerate(success):
        _locator(locator, f"completion.success[{index}]")
    for index, locator in enumerate(failure):
        _locator(locator, f"completion.failure[{index}]")
    timeout = completion.get("timeout_seconds")
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise WebQueryAdapterError("completion.timeout_seconds must be between 1 and 3600.")

    result = _object(adapter.get("result"), "result")
    inline_max_rows = result.get("inline_max_rows")
    if not isinstance(inline_max_rows, int) or inline_max_rows < 1:
        raise WebQueryAdapterError("result.inline_max_rows must be a positive integer.")
    locator_fields = {
        "row_count",
        "inline_download",
        "large_result_link",
        "export_button",
        "format_option",
    }
    for field in locator_fields:
        if field in result:
            _locator(result[field], f"result.{field}")
    if not ({"inline_download", "large_result_link"} & set(result)):
        raise WebQueryAdapterError("result must define inline_download or large_result_link.")

    tab_policy = _object(adapter.get("tab_policy"), "tab_policy")
    if tab_policy.get("max_active_query_tabs") != 1 or tab_policy.get("close_after_download") is not True:
        raise WebQueryAdapterError("tab_policy must keep one active query tab and close it after download.")
    return adapter


def load_adapter(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WebQueryAdapterError(f"Adapter file is unreadable: {source}") from error
    return validate_adapter(value)


def resolve_adapter(project_root: str | Path, adapter_file: str | Path | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(adapter_file).resolve() if adapter_file else root / DEFAULT_RELATIVE
    if not source.is_file():
        return {
            "schema_version": "web_query_adapter_resolution_v1",
            "status": "manual_required",
            "execution_surface": "web",
            "adapter_file": str(source),
            "example_file": EXAMPLE_RELATIVE.as_posix(),
            "reason": "No local web-query adapter is configured.",
        }
    adapter = load_adapter(source)
    return {
        "schema_version": "web_query_adapter_resolution_v1",
        "status": "ready",
        "execution_surface": "web",
        "adapter_file": str(source),
        "adapter": adapter,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Validate one adapter file")
    validate.add_argument("--adapter-file", required=True)
    resolve = sub.add_parser("resolve", help="Resolve a project's ignored local adapter")
    resolve.add_argument("--project-root", required=True)
    resolve.add_argument("--adapter-file")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            adapter = load_adapter(args.adapter_file)
            result = {
                "schema_version": "web_query_adapter_validation_v1",
                "status": "valid",
                "adapter_id": adapter["adapter_id"],
                "allowed_hosts": adapter["allowed_hosts"],
            }
        else:
            result = resolve_adapter(args.project_root, args.adapter_file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"valid", "ready"} else 2
    except WebQueryAdapterError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
