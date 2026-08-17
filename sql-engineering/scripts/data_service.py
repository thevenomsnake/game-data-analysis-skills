#!/usr/bin/env python3
"""Manage shared data services and explicit project-stage bindings."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_provenance import generated_by_ldap
from capability_registry import command_function_ids
from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)


CATALOG_CONTRACT = "data_service_catalog_v1"
STAGE_CONTRACT = "stage_data_service_bindings_v1"
RESOLUTION_CONTRACT = "data_service_resolution_v1"
LOCAL_PROBE_CONTRACT = "data_service_local_probe_v1"
CATALOG_RELATIVE_PATH = Path("sql-projects") / "_data_services" / "catalog.json"
STAGE_FILE_NAME = "data_services.json"
PURPOSE_ADAPTERS = {
    "development_inspection": "sql_readonly",
    "production_query": "browser_query",
}
ADAPTER_TARGET_FIELDS = {
    "sql_readonly": {"database", "implicit_business_scope"},
    "browser_query": {"project_id", "agent_id"},
}
PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FORBIDDEN_SECRET_KEYS = {"password", "passwd", "secret", "token", "cookie", "cookies"}


class DataServiceError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataServiceError(f"missing data-service file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataServiceError(f"invalid data-service JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataServiceError(f"data-service document must be an object: {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def resolve_repo(path: Path) -> Path:
    repo = path.resolve()
    if not (repo / "sql-projects").is_dir():
        raise DataServiceError(f"repository does not contain sql-projects: {repo}")
    return repo


def project_root(repo: Path, project: str) -> Path:
    if not PROJECT_RE.fullmatch(project):
        raise DataServiceError("project must contain only letters, digits, _ or -")
    root = (repo / "sql-projects" / project).resolve()
    if root.parent != (repo / "sql-projects").resolve():
        raise DataServiceError(f"project escapes sql-projects: {project}")
    if not (root / "project_config.json").is_file():
        raise DataServiceError(f"project is not configured: {project}")
    return root


def stage_path(repo: Path, project: str) -> Path:
    root = project_root(repo, project)
    config = read_json(root / "project_config.json")
    relative = str(config.get("data_services_file") or STAGE_FILE_NAME)
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.parent != Path(".") or candidate.name != STAGE_FILE_NAME:
        raise DataServiceError("project_config.data_services_file must be data_services.json")
    return root / candidate


def load_catalog(repo: Path) -> dict[str, Any]:
    catalog = read_json(repo / CATALOG_RELATIVE_PATH)
    if catalog.get("contract_version") != CATALOG_CONTRACT:
        raise DataServiceError(f"catalog contract must be {CATALOG_CONTRACT}")
    services = catalog.get("services")
    if not isinstance(services, dict) or not services:
        raise DataServiceError("data-service catalog must contain services")
    for service_id, service in services.items():
        if not SERVICE_RE.fullmatch(str(service_id)) or not isinstance(service, dict):
            raise DataServiceError(f"invalid service definition: {service_id}")
        if service.get("service_id") != service_id:
            raise DataServiceError(f"service_id mismatch: {service_id}")
        if service.get("adapter") not in set(PURPOSE_ADAPTERS.values()):
            raise DataServiceError(f"unsupported adapter for {service_id}")
        purposes = service.get("purposes")
        if not isinstance(purposes, list) or not purposes:
            raise DataServiceError(f"service {service_id} must declare purposes")
        for purpose in purposes:
            if PURPOSE_ADAPTERS.get(str(purpose)) != service.get("adapter"):
                raise DataServiceError(f"service {service_id} has incompatible purpose {purpose}")
        product_ids = service.get("product_ids")
        if not isinstance(product_ids, list) or not product_ids:
            raise DataServiceError(f"service {service_id} must declare product_ids")
        connection = service.get("connection")
        policy = service.get("policy")
        if not isinstance(connection, dict) or not isinstance(policy, dict):
            raise DataServiceError(f"service {service_id} must declare connection and policy")
        overlapping_fields = sorted(set(connection) & set(policy))
        if overlapping_fields:
            raise DataServiceError(
                f"service {service_id} duplicates connection/policy fields: "
                + ", ".join(overlapping_fields)
            )
        secret_paths = forbidden_secret_paths(service)
        if secret_paths:
            raise DataServiceError(
                f"service {service_id} contains plaintext secret fields: "
                + ", ".join(secret_paths)
            )
        if service["adapter"] == "sql_readonly":
            _required(
                connection,
                {
                    "engine",
                    "host",
                    "port",
                    "username",
                    "password_env",
                    "readonly",
                    "allowed_databases",
                },
                f"service {service_id} connection",
            )
            _required(
                policy,
                {
                    "default_lookback_days",
                    "max_scan_days",
                    "max_result_rows",
                    "enum_top_n",
                    "query_timeout_seconds",
                    "results_policy",
                },
                f"service {service_id} policy",
            )
        else:
            _required(
                connection,
                {"root_url", "query_url_template"},
                f"service {service_id} connection",
            )
    return catalog


def forbidden_secret_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).lower() in FORBIDDEN_SECRET_KEYS:
                paths.append(path)
            paths.extend(forbidden_secret_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(forbidden_secret_paths(child, f"{prefix}[{index}]"))
    return paths


def validate_confirmation(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise DataServiceError(f"{label} must declare confirmation")
    _required(
        value,
        {"method", "confirmed_by_ldap", "confirmed_at"},
        f"{label} confirmation",
    )


def load_stage(repo: Path, project: str) -> dict[str, Any]:
    path = stage_path(repo, project)
    stage = read_json(path)
    if stage.get("contract_version") != STAGE_CONTRACT:
        raise DataServiceError(f"stage contract must be {STAGE_CONTRACT}: {path}")
    if stage.get("project_id") != project:
        raise DataServiceError(f"stage project_id must be {project}: {path}")
    if not str(stage.get("product_id") or "") or not str(stage.get("stage_id") or ""):
        raise DataServiceError(f"stage must declare product_id and stage_id: {path}")
    bindings = stage.get("bindings")
    if not isinstance(bindings, dict):
        raise DataServiceError(f"stage bindings must be an object: {path}")
    for purpose in PURPOSE_ADAPTERS:
        binding = bindings.get(purpose)
        if not isinstance(binding, dict) or binding.get("status") not in {
            "confirmed",
            "unbound",
            "not_enabled",
        }:
            raise DataServiceError(
                f"{project}.{purpose} must be confirmed, unbound, or not_enabled"
            )
        if binding.get("status") == "confirmed":
            if not SERVICE_RE.fullmatch(str(binding.get("service_id") or "")):
                raise DataServiceError(f"{project}.{purpose} must declare service_id")
            if not isinstance(binding.get("target"), dict):
                raise DataServiceError(f"{project}.{purpose} must declare target")
            secret_paths = forbidden_secret_paths(binding["target"])
            if secret_paths:
                raise DataServiceError(
                    f"{project}.{purpose} target contains secret fields: "
                    + ", ".join(secret_paths)
                )
            validate_confirmation(binding.get("confirmation"), f"{project}.{purpose}")
        elif binding.get("status") == "not_enabled":
            validate_confirmation(binding.get("confirmation"), f"{project}.{purpose}")
    return stage


def validate_binding(
    catalog: dict[str, Any], stage: dict[str, Any], purpose: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if purpose not in PURPOSE_ADAPTERS:
        raise DataServiceError(f"unsupported data-service purpose: {purpose}")
    binding = stage["bindings"][purpose]
    if binding.get("status") != "confirmed":
        raise DataServiceError(
            f"{stage['project_id']}.{purpose} is {binding.get('status')}; "
            "confirm or copy a stage binding first"
        )
    service_id = str(binding["service_id"])
    service = catalog["services"].get(service_id)
    if not isinstance(service, dict):
        raise DataServiceError(f"unknown data service: {service_id}")
    if stage["product_id"] not in service.get("product_ids", []):
        raise DataServiceError(
            f"service {service_id} is not available to product {stage['product_id']}"
        )
    if purpose not in service.get("purposes", []):
        raise DataServiceError(f"service {service_id} does not support {purpose}")
    expected_adapter = PURPOSE_ADAPTERS[purpose]
    if service.get("adapter") != expected_adapter:
        raise DataServiceError(f"{purpose} requires adapter {expected_adapter}")
    unexpected_target_fields = sorted(
        set(binding["target"]) - ADAPTER_TARGET_FIELDS[expected_adapter]
    )
    if unexpected_target_fields:
        raise DataServiceError(
            f"{stage['project_id']}.{purpose} target cannot override service fields: "
            + ", ".join(unexpected_target_fields)
        )
    return service, binding


def _required(value: dict[str, Any], fields: set[str], label: str) -> None:
    missing = sorted(
        field for field in fields if value.get(field) is None or value.get(field) == ""
    )
    if missing:
        raise DataServiceError(f"{label} is missing: " + ", ".join(missing))


def resolve_profile(service: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    adapter = str(service["adapter"])
    connection = copy.deepcopy(service.get("connection") or {})
    policy = copy.deepcopy(service.get("policy") or {})
    target = copy.deepcopy(binding.get("target") or {})
    if adapter == "sql_readonly":
        profile = {"enabled": True, **connection, **policy, **target}
        _required(
            profile,
            {
                "engine",
                "host",
                "port",
                "username",
                "database",
                "password_env",
                "allowed_databases",
                "default_lookback_days",
                "max_scan_days",
                "max_result_rows",
                "enum_top_n",
                "query_timeout_seconds",
                "results_policy",
            },
            "resolved sql_readonly profile",
        )
        if profile.get("readonly") is not True:
            raise DataServiceError("resolved sql_readonly profile must be readonly")
        if profile.get("results_policy") != "local_ignored":
            raise DataServiceError("resolved sql_readonly results_policy must be local_ignored")
        if profile["database"] not in profile["allowed_databases"]:
            raise DataServiceError("resolved database must be listed in allowed_databases")
        return profile
    if adapter == "browser_query":
        _required(connection, {"root_url", "query_url_template"}, "browser service")
        _required(target, {"project_id", "agent_id"}, "browser stage target")
        try:
            query_url = str(connection["query_url_template"]).format(
                project_id=int(target["project_id"]), agent_id=int(target["agent_id"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataServiceError("browser stage target has invalid project_id or agent_id") from exc
        if int(target["project_id"]) <= 0 or int(target["agent_id"]) <= 0:
            raise DataServiceError("browser project_id and agent_id must be positive")
        _required(
            policy,
            {
                "check_auth_at_root",
                "authentication_mode",
                "auto_execute_ready_receipt",
                "query_tab_policy",
                "submit_once",
                "export_format",
                "download_policy",
                "auto_attach_result",
                "auto_visualization",
            },
            "browser service policy",
        )
        return {
            "contract_version": "browser_query_execution_v2",
            "enabled": True,
            "provider": service["provider"],
            "root_url": connection["root_url"],
            "query_url": query_url,
            "project_id": int(target["project_id"]),
            "agent_id": int(target["agent_id"]),
            **policy,
        }
    raise DataServiceError(f"unsupported adapter: {adapter}")


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve(repo: Path, project: str, purpose: str) -> dict[str, Any]:
    catalog = load_catalog(repo)
    stage = load_stage(repo, project)
    service, binding = validate_binding(catalog, stage, purpose)
    profile = resolve_profile(service, binding)
    fingerprint = canonical_hash(
        {
            "service": service,
            "target": binding["target"],
            "purpose": purpose,
        }
    )
    return {
        "contract_version": RESOLUTION_CONTRACT,
        "project_id": project,
        "product_id": stage["product_id"],
        "stage_id": stage["stage_id"],
        "purpose": purpose,
        "service_id": service["service_id"],
        "adapter": service["adapter"],
        "evidence_role": service["evidence_role"],
        "binding_fingerprint": fingerprint,
        "binding_confirmation": copy.deepcopy(binding["confirmation"]),
        "profile": profile,
    }


def resolve_from_project_root(root: Path, purpose: str) -> dict[str, Any]:
    project_dir = root.resolve()
    config = read_json(project_dir / "project_config.json")
    project = str(config.get("project_id") or project_dir.name)
    if project_dir.parent.name != "sql-projects":
        raise DataServiceError("project root must be directly below sql-projects")
    return resolve(project_dir.parent.parent, project, purpose)


def user_environment_value(name: str) -> str:
    value = os.environ.get(name, "")
    if value or os.name != "nt":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            stored, _ = winreg.QueryValueEx(key, name)
            return str(stored or "")
    except (FileNotFoundError, OSError):
        return ""


def local_probe_path(repo: Path, resolution: dict[str, Any]) -> Path:
    return (
        repo
        / ".local"
        / "data-services"
        / str(resolution["service_id"])
        / f"{resolution['binding_fingerprint']}.json"
    )


def write_local_probe(
    repo: Path, resolution: dict[str, Any], server_capabilities: dict[str, Any]
) -> dict[str, Any]:
    if resolution.get("adapter") != "sql_readonly":
        raise DataServiceError("local server probes are supported only for sql_readonly services")
    profile = resolution["profile"]
    receipt = {
        "contract_version": LOCAL_PROBE_CONTRACT,
        "service_id": resolution["service_id"],
        "adapter": resolution["adapter"],
        "binding_fingerprint": resolution["binding_fingerprint"],
        "credential_ref": {
            "username": profile.get("username"),
            "password_env": profile.get("password_env"),
        },
        "status": "ready",
        "checked_at": now_iso(),
        "server_capabilities": copy.deepcopy(server_capabilities),
    }
    atomic_write_json(local_probe_path(repo, resolution), receipt)
    return receipt


def member_status(repo: Path, resolution: dict[str, Any]) -> dict[str, Any]:
    profile = resolution["profile"]
    if resolution["adapter"] == "browser_query":
        return {
            "status": "authentication_required",
            "setup_blocking": False,
            "authentication_mode": profile.get("authentication_mode"),
            "reason": "Browser authentication is checked once when an execution queue starts.",
        }
    password_env = str(profile["password_env"])
    if not user_environment_value(password_env):
        return {
            "status": "credential_required",
            "setup_blocking": True,
            "password_env": password_env,
            "credential_present": False,
        }
    probe_path = local_probe_path(repo, resolution)
    if not probe_path.is_file():
        return {
            "status": "probe_required",
            "setup_blocking": True,
            "password_env": password_env,
            "credential_present": True,
        }
    probe = read_json(probe_path)
    if (
        probe.get("contract_version") != LOCAL_PROBE_CONTRACT
        or probe.get("binding_fingerprint") != resolution["binding_fingerprint"]
        or probe.get("status") != "ready"
    ):
        return {
            "status": "probe_required",
            "setup_blocking": True,
            "password_env": password_env,
            "credential_present": True,
        }
    return {
        "status": "ready",
        "setup_blocking": False,
        "password_env": password_env,
        "credential_present": True,
        "last_probe": {
            "checked_at": probe.get("checked_at"),
            "server_capabilities": probe.get("server_capabilities"),
        },
    }


def copy_candidates(repo: Path, target_stage: dict[str, Any], purpose: str) -> list[dict[str, Any]]:
    candidates = []
    catalog = load_catalog(repo)
    projects_root = repo / "sql-projects"
    for child in sorted(projects_root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or child.name.startswith("_") or child.name == target_stage["project_id"]:
            continue
        if not (child / STAGE_FILE_NAME).is_file():
            continue
        try:
            sibling = load_stage(repo, child.name)
            if sibling["product_id"] != target_stage["product_id"]:
                continue
            binding = sibling["bindings"].get(purpose) or {}
            if binding.get("status") != "confirmed":
                continue
            service, _ = validate_binding(catalog, sibling, purpose)
        except DataServiceError:
            continue
        candidates.append(
            {
                "source_project_id": sibling["project_id"],
                "source_stage_id": sibling["stage_id"],
                "service_id": service["service_id"],
                "service_name": service["display_name"],
                "target": copy.deepcopy(binding["target"]),
            }
        )
    return candidates


def status(repo: Path, project: str) -> dict[str, Any]:
    path = stage_path(repo, project)
    if not path.is_file():
        return {
            "contract_version": RESOLUTION_CONTRACT,
            "status": "needs_input",
            "project_id": project,
            "stage_binding_status": "setup_required",
            "required_actions": [
                "Initialize explicit product/stage identity before binding data services."
            ],
        }
    stage = load_stage(repo, project)
    services: dict[str, Any] = {}
    required_actions: list[str] = []
    for purpose in PURPOSE_ADAPTERS:
        binding = stage["bindings"][purpose]
        if binding["status"] == "not_enabled":
            services[purpose] = {
                "binding_status": "not_enabled",
                "member_status": "not_enabled",
                "copy_candidates": [],
            }
            continue
        if binding["status"] == "unbound":
            candidates = copy_candidates(repo, stage, purpose)
            services[purpose] = {
                "binding_status": "unbound",
                "member_status": "not_enabled",
                "copy_candidates": candidates,
            }
            required_actions.append(
                f"Confirm a new {purpose} binding"
                + (" or copy one candidate." if candidates else ".")
            )
            continue
        resolution = resolve(repo, project, purpose)
        local = member_status(repo, resolution)
        services[purpose] = {
            "binding_status": "confirmed",
            "service_id": resolution["service_id"],
            "adapter": resolution["adapter"],
            "evidence_role": resolution["evidence_role"],
            "target": copy.deepcopy(binding["target"]),
            "confirmation": copy.deepcopy(binding["confirmation"]),
            "member": local,
            "copy_candidates": [],
        }
        if local.get("setup_blocking"):
            required_actions.append(f"Configure local access for {purpose}: {local['status']}.")
    return {
        "contract_version": RESOLUTION_CONTRACT,
        "status": "ready" if not required_actions else "needs_input",
        "project_id": project,
        "product_id": stage["product_id"],
        "stage_id": stage["stage_id"],
        "stage_binding_status": "configured",
        "services": services,
        "required_actions": required_actions,
    }


def initialize_stage(repo: Path, project: str, product: str, stage_id: str) -> dict[str, Any]:
    if not product or not stage_id:
        raise DataServiceError("product and stage are required")
    path = stage_path(repo, project)
    if path.exists():
        raise DataServiceError(f"stage data services already exist: {path}")
    planning_binding_path = project_root(repo, project) / "planning" / "source_binding.json"
    if planning_binding_path.is_file():
        planning_binding = read_json(planning_binding_path)
        if (
            planning_binding.get("product_id") != product
            or planning_binding.get("stage_id") != stage_id
        ):
            raise DataServiceError(
                "product/stage must match the existing planning source binding"
            )
    document = {
        "contract_version": STAGE_CONTRACT,
        "project_id": project,
        "product_id": product,
        "stage_id": stage_id,
        "bindings": {purpose: {"status": "unbound"} for purpose in PURPOSE_ADAPTERS},
    }
    atomic_write_json(path, document)
    config_path = project_root(repo, project) / "project_config.json"
    config = read_json(config_path)
    config["data_services_file"] = STAGE_FILE_NAME
    config["updated_at"] = now_iso()
    atomic_write_json(config_path, config)
    return status(repo, project)


def confirmation(repo: Path, method: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
    value = {
        "method": method,
        "confirmed_by_ldap": generated_by_ldap(repo),
        "confirmed_at": now_iso(),
    }
    if source:
        value.update(source)
    return value


def copy_stage_bindings(
    repo: Path, project: str, source_project: str, purposes: list[str], *, replace: bool = False
) -> dict[str, Any]:
    target = load_stage(repo, project)
    source = load_stage(repo, source_project)
    if target["product_id"] != source["product_id"]:
        raise DataServiceError("data-service bindings can be copied only within one product")
    catalog = load_catalog(repo)
    for purpose in purposes:
        validate_binding(catalog, source, purpose)
        current = target["bindings"][purpose]
        if current.get("status") == "confirmed" and not replace:
            raise DataServiceError(f"{project}.{purpose} is already confirmed; use --replace")
        source_binding = source["bindings"][purpose]
        target["bindings"][purpose] = {
            "status": "confirmed",
            "service_id": source_binding["service_id"],
            "target": copy.deepcopy(source_binding["target"]),
            "confirmation": confirmation(
                repo,
                "user_confirmed_copy",
                {
                    "copied_from_project_id": source["project_id"],
                    "copied_from_stage_id": source["stage_id"],
                },
            ),
        }
    atomic_write_json(stage_path(repo, project), target)
    return status(repo, project)


def bind_stage_service(
    repo: Path,
    project: str,
    purpose: str,
    service_id: str,
    target_value: dict[str, Any],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    stage = load_stage(repo, project)
    current = stage["bindings"][purpose]
    if current.get("status") == "confirmed" and not replace:
        raise DataServiceError(f"{project}.{purpose} is already confirmed; use --replace")
    stage["bindings"][purpose] = {
        "status": "confirmed",
        "service_id": service_id,
        "target": copy.deepcopy(target_value),
        "confirmation": confirmation(repo, "user_confirmed_binding"),
    }
    validate_binding(load_catalog(repo), stage, purpose)
    resolve_profile(
        load_catalog(repo)["services"][service_id], stage["bindings"][purpose]
    )
    atomic_write_json(stage_path(repo, project), stage)
    return status(repo, project)


def disable_stage_service(
    repo: Path, project: str, purpose: str, *, replace: bool = False
) -> dict[str, Any]:
    stage = load_stage(repo, project)
    current = stage["bindings"][purpose]
    if current.get("status") == "confirmed" and not replace:
        raise DataServiceError(f"{project}.{purpose} is already confirmed; use --replace")
    stage["bindings"][purpose] = {
        "status": "not_enabled",
        "confirmation": confirmation(repo, "user_confirmed_not_enabled"),
    }
    atomic_write_json(stage_path(repo, project), stage)
    return status(repo, project)


def parse_target(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DataServiceError(f"--target-json must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DataServiceError("--target-json must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--project", required=True)
    parser.add_argument("--format", choices=["json"], default="json")
    add_function_gate_arguments(parser, selection_help="Use PROJECT_ADMIN for data services.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show stage bindings, local readiness, and copy candidates.")
    resolve_parser = subparsers.add_parser("resolve", help="Resolve one exact stage service.")
    resolve_parser.add_argument("--purpose", choices=sorted(PURPOSE_ADAPTERS), required=True)
    init_parser = subparsers.add_parser("init", help="Initialize explicit product/stage bindings.")
    init_parser.add_argument("--product", required=True)
    init_parser.add_argument("--stage", required=True)
    copy_parser = subparsers.add_parser("copy", help="Copy selected bindings from a sibling stage.")
    copy_parser.add_argument("--from-project", required=True)
    copy_parser.add_argument(
        "--purpose", action="append", choices=sorted(PURPOSE_ADAPTERS), required=True
    )
    copy_parser.add_argument("--replace", action="store_true")
    bind_parser = subparsers.add_parser("bind", help="Bind one catalog service to this stage.")
    bind_parser.add_argument("--purpose", choices=sorted(PURPOSE_ADAPTERS), required=True)
    bind_parser.add_argument("--service-id", required=True)
    bind_parser.add_argument("--target-json", required=True)
    bind_parser.add_argument("--replace", action="store_true")
    disable_parser = subparsers.add_parser(
        "disable", help="Confirm that this stage does not use one service purpose."
    )
    disable_parser.add_argument("--purpose", choices=sorted(PURPOSE_ADAPTERS), required=True)
    disable_parser.add_argument("--replace", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids(Path(__file__).name, args.command),
            purpose="project-stage data-service management",
        )
        if args.command in {"init", "copy", "bind", "disable"}:
            require_user_request(args.user_request, purpose="project-stage data-service management")
        repo = resolve_repo(args.repo_root)
        if args.command == "status":
            result = status(repo, args.project)
        elif args.command == "resolve":
            result = resolve(repo, args.project, args.purpose)
            result["member"] = member_status(repo, result)
        elif args.command == "init":
            result = initialize_stage(repo, args.project, args.product, args.stage)
        elif args.command == "copy":
            result = copy_stage_bindings(
                repo,
                args.project,
                args.from_project,
                list(dict.fromkeys(args.purpose)),
                replace=args.replace,
            )
        elif args.command == "bind":
            result = bind_stage_service(
                repo,
                args.project,
                args.purpose,
                args.service_id,
                parse_target(args.target_json),
                replace=args.replace,
            )
        else:
            result = disable_stage_service(
                repo, args.project, args.purpose, replace=args.replace
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") == "needs_input" and args.command == "status":
            raise SystemExit(2)
    except FunctionGateError as error:
        exit_with_gate_error(parser, error)
    except DataServiceError as error:
        parser.exit(2, f"BLOCKED: {error}\n")


if __name__ == "__main__":
    main()
