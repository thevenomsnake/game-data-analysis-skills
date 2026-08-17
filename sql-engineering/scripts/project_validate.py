#!/usr/bin/env python3
"""Validate one SQL project folder and emit stable machine-readable health JSON."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spec_utils import (
    HEADER_LINE_BUDGET,
    HEADER_MARKERS,
    SPEC_STORAGE,
    has_full_spec_block,
    load_sidecar_spec,
    spec_path_for_artifact,
)
from config_knowledge import (
    discover_repo_root,
    resolve_knowledge,
    validate_dataset_manifest,
    validate_knowledge_reference,
    validate_repository as validate_knowledge_repository,
)
from health_scope import (  # noqa: E402
    artifacts_for_scope,
    compact_health_payload,
    deferred_history_summary,
    workspace_versions_for_scope,
)
from knowledge_usage import validate_knowledge_usage  # noqa: E402
from rule_authorization_governance import amendment_index  # noqa: E402
from rule_store import RuleStore  # noqa: E402
from sql_project import (
    project_index_manifest_fingerprint,
    project_manifest_fingerprint,
    query_params_contract_problems,
)
from formal_asset_repository import (
    FormalAssetRepositoryError,
    list_packages as list_formal_asset_packages,
    load_package as load_formal_asset_package,
    validate_receipt as validate_formal_asset_receipt,
)
from sql_query_workspace import (
    CHANGE_COVERAGE_MATRIX,
    COVERAGE_RELATIONS,
    INDEX_HTML_REL as QUERY_WORKSPACE_HTML_REL,
    INDEX_REL as QUERY_WORKSPACE_INDEX_REL,
    LEGACY_INDEX_SCHEMA_VERSION as LEGACY_QUERY_WORKSPACE_INDEX_SCHEMA_VERSION,
    INDEX_SCHEMA_VERSION as QUERY_WORKSPACE_INDEX_SCHEMA_VERSION,
    META_SCHEMA_VERSION as QUERY_WORKSPACE_META_SCHEMA_VERSION,
    QUERY_CHANGE_TYPES,
    QUERY_STATUSES,
    file_sha256 as query_workspace_file_sha256,
    load_index as load_query_workspace_index,
    resolve_project_path as resolve_query_workspace_path,
    sql_fingerprint as query_workspace_sql_fingerprint,
    validate_no_absolute_paths as validate_query_workspace_paths,
)
from performance_preflight import (
    native_hive_string_collect_patterns,
    select_distinct_group_by_blocks,
    unsafe_midnight_concat_patterns,
    uses_non_native_hive_execution,
)
from sql_facts import build_sql_fact_bundle, sql_side_privacy_transforms
from sql_summary_planner import summary_plan_fingerprint, validate_summary_plan
from sql_execution_adapter import (
    adapter_config_problems,
    effective_config_for_context,
    materialize_profile_config,
)
from sql_time_contract import time_integrity_config_problems
from sql_identifier_policy import config_problems as identifier_policy_config_problems
from sql_identifier_policy import policy_findings as identifier_policy_findings
from query_window import validate_default_query_window
from result_evidence_retention import RESULT_EVIDENCE_MAX_BYTES
import planning_source
import data_service

RESULT_EXTENSIONS = {".csv", ".xlsx"}
ALLOWED_DIALECTS = {"Hive", "StarRocks"}
ALLOWED_DASHBOARD_VERIFICATION = {"verified", "unverified_skipped_run", "proxy_verified"}
ALLOWED_TABLE_AVAILABILITY = {"unknown", "available", "unavailable"}
ALLOWED_TABLE_SOURCE_CONTRACT_MODES = {"dual_path", "intermediate_preferred", "intermediate_only", "raw_logs_only"}
LOCAL_ABSOLUTE_REF_PATTERN = re.compile(
    r"(?<![A-Za-z])([A-Za-z]:[\\/][^\s，。；;,\n\r\"'）)]+|\\\\[^\s，。；;,\n\r\"'）)]+|file://[^\s，。；;,\n\r\"'）)]+)",
    flags=re.I,
)

REQUIRED_CONFIG_FIELDS = [
    "version",
    "project_id",
    "display_name",
    "sql_dialect",
    "query_engine",
    "query_environment",
    "dashboard_application",
    "data_services_file",
    "table_naming_profile",
    "partition_policy",
    "table_overrides",
    "generation_contract",
]

ARTIFACT_SCAN_DIRS = ["query_sql", "dashboard_sql", "validations"]
UNMANAGED_WORK_DIR_NAMES = {"_scratch", "scratch", "work", "_work", "_working", "draft", "drafts"}

PLACEHOLDER_PATTERN = re.compile(
    r"(\{\{[^}]+\}\}|\bTODO\b|\bTBD\b|\bPLACEHOLDER\b|\bREPLACE_ME\b|__[^_\n]{2,80}__|待补充)",
    flags=re.I,
)
TDBANK_UNSAFE_PARAM_ALIAS_PATTERN = re.compile(
    r"\bas\s+(`?(?:start_partition|end_partition|partition|end)`?)\b",
    flags=re.I,
)
REQUIRED_PERFORMANCE_KEYS = [
    "optimization_tier",
    "preflight_score",
    "preflight_triggers",
    "optimization_reference",
    "full_guide_required",
    "equivalence_preserved",
    "performance_fingerprint",
]
SHARED_RULE_SOURCE_LOGS_REQUIRING_EVENT_SIGNATURE = {"battleitem", "battleloginout", "damage"}
BOUNDARY_ONLY_EVENT_SIGNATURE_POLICIES = {"boundaryonly", "partialonly", "diagnosticonly"}
EVENT_SIGNATURE_ROLE_KEYS = [
    "required_metric_roles",
    "required_metric_role",
    "required_any_metric_roles",
    "required_any_metric_role",
]
EVENT_SIGNATURE_AGGREGATION_KEYS = [
    "required_aggregations",
    "required_aggregation",
    "required_any_aggregations",
    "required_any_aggregation",
]
EVENT_SIGNATURE_PREDICATE_KEYS = [
    "required_predicates",
    "required_predicate",
    "required_conditions",
    "required_condition",
]
EVENT_SIGNATURE_FIELD_ROLE_KEYS = [
    "required_field_roles",
    "required_field_role",
]


def slug_text(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or value.strip().lower()


def normalize_signal(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def listify(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def event_signature_values(signature: dict[str, Any], keys: list[str]) -> list[Any]:
    values: list[Any] = []
    for key in keys:
        values.extend(listify(signature.get(key)))
    return [item for item in values if str(item or "").strip()]


def shared_logs_from_activation_contract(contract: dict[str, Any]) -> list[str]:
    logs: list[Any] = []
    logs.extend(listify(contract.get("source_logs")))
    source_signature = contract.get("source_signature")
    if isinstance(source_signature, dict):
        logs.extend(listify(source_signature.get("source_logs")))
        logs.extend(listify(source_signature.get("logs")))
    event_signature = contract.get("event_signature")
    if isinstance(event_signature, dict):
        logs.extend(event_signature_values(event_signature, ["required_logs", "required_log"]))
    normalized_logs = {
        normalize_signal(item)
        for item in logs
        if str(item or "").strip()
    }
    return sorted(normalized_logs & SHARED_RULE_SOURCE_LOGS_REQUIRING_EVENT_SIGNATURE)


def event_signature_has_exact_core(signature: dict[str, Any]) -> bool:
    roles = event_signature_values(signature, EVENT_SIGNATURE_ROLE_KEYS)
    aggregations = event_signature_values(signature, EVENT_SIGNATURE_AGGREGATION_KEYS)
    predicates = event_signature_values(signature, EVENT_SIGNATURE_PREDICATE_KEYS)
    field_roles = event_signature_values(signature, EVENT_SIGNATURE_FIELD_ROLE_KEYS)
    return bool(roles and aggregations and (predicates or field_roles))


def event_signature_is_boundary_only(contract: dict[str, Any], signature: dict[str, Any]) -> bool:
    policy = (
        signature.get("match_policy")
        or signature.get("match_scope")
        or contract.get("match_policy")
        or contract.get("match_scope")
    )
    return normalize_signal(policy) in BOUNDARY_ONLY_EVENT_SIGNATURE_POLICIES


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "missing", "unknown", "todo", "tbd"}
    if isinstance(value, dict):
        status = str(value.get("status", "")).strip().lower()
        if status in {"missing", "unknown", "todo", "tbd"}:
            return True
        if "name" in value and is_missing(value.get("name")):
            return True
        return False
    return False


def is_nonempty_text(value: Any, min_len: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= min_len


def normalize_rel(value: str | Path) -> str:
    return Path(str(value).replace("\\", "/")).as_posix()


def resolve_project_path(root: Path, value: str | Path) -> Path:
    path = Path(str(value).replace("\\", "/"))
    if path.is_absolute():
        return path
    return root / path


def is_absolute_reference(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\") or text.lower().startswith("file://"))


def local_absolute_references(value: Any) -> list[str]:
    return [match.group(1) for match in LOCAL_ABSOLUTE_REF_PATTERN.finditer(str(value or ""))]


def validate_json_project_relative_references(
    report: HealthReport,
    payload: Any,
    path: Path,
    check_id: str,
    label: str,
    pointer: str = "$",
) -> int:
    failures = 0
    if isinstance(payload, str):
        for match in local_absolute_references(payload):
            report.fail(
                check_id,
                f"{label} stores local absolute path `{match}` at {pointer}; copy evidence into the project and reference it with a project-relative path.",
                path,
            )
            failures += 1
    elif isinstance(payload, dict):
        for key, value in payload.items():
            failures += validate_json_project_relative_references(
                report,
                value,
                path,
                check_id,
                label,
                f"{pointer}.{key}",
            )
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            failures += validate_json_project_relative_references(
                report,
                value,
                path,
                check_id,
                label,
                f"{pointer}[{index}]",
            )
    return failures


def display_path(root: Path, path: str | Path) -> str:
    path_obj = Path(path)
    try:
        return path_obj.resolve().relative_to(root.resolve()).as_posix()
    except Exception:  # noqa: BLE001
        return str(path_obj)


def read_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, f"JSON file not found: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"JSON parse failed: {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"JSON root must be an object: {path}"
    return data, None


def read_text_file(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, f"File not found: {path}"
    try:
        return path.read_text(encoding="utf-8"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"Failed to read file: {path}: {exc}"


def strip_sql_comments(sql: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n\r]*", " ", no_block)


def is_current_artifact(item: dict[str, Any]) -> bool:
    return (item.get("artifact_state") or "current") == "current" and item.get("status") != "superseded"


def issue_status_for_artifact(item: dict[str, Any], strict: bool = False) -> str:
    if is_current_artifact(item) or item.get("reusable") is True or item.get("kind") == "DASHBOARD":
        return "fail"
    return "warn"


def execution_contract_status_for_artifact(item: dict[str, Any]) -> str:
    """Return severity for SQL execution-policy drift found in existing assets.

    Structural repository invariants still use `issue_status_for_artifact`.
    Time/params contract drift is different: many retained artifacts predate the
    short-header/params-CTE policy and are still useful as draft repository
    records. Future saves are blocked by `save-sql`; existing draft assets warn
    until they are intentionally rewritten or promoted.
    """

    if not is_current_artifact(item):
        return "warn"
    status = str(item.get("status") or "").strip().lower()
    verification_status = str(item.get("verification_status") or "").strip().lower()
    if status in {"verified", "approved", "released", "production", "published"}:
        return "fail"
    if verification_status == "verified":
        return "fail"
    return "warn"


class HealthReport:
    def __init__(self, project: str, root: Path, strict: bool, scope: str = "full") -> None:
        self.project = project
        self.root = root
        self.strict = strict
        self.scope = scope
        self.checks: list[dict[str, Any]] = []
        self.legacy_knowledge_usage_paths: set[str] = set()
        self.deferred_history: dict[str, Any] = {}

    def add(self, check_id: str, status: str, message: str, path: str | Path = "") -> None:
        self.checks.append(
            {
                "id": check_id,
                "status": status,
                "message": message,
                "path": display_path(self.root, path) if path else "",
            }
        )

    def pass_check(self, check_id: str, message: str, path: str | Path = "") -> None:
        self.add(check_id, "pass", message, path)

    def warn(self, check_id: str, message: str, path: str | Path = "") -> None:
        self.add(check_id, "warn", message, path)

    def fail(self, check_id: str, message: str, path: str | Path = "") -> None:
        self.add(check_id, "fail", message, path)

    def record_legacy_knowledge_usage(self, path: str | Path) -> None:
        self.legacy_knowledge_usage_paths.add(display_path(self.root, path))

    def payload(self) -> dict[str, Any]:
        if self.legacy_knowledge_usage_paths and not any(
            item.get("id") == "knowledge_usage.legacy_unknown" for item in self.checks
        ):
            samples = sorted(self.legacy_knowledge_usage_paths)[:3]
            self.warn(
                "knowledge_usage.legacy_unknown",
                f"{len(self.legacy_knowledge_usage_paths)} legacy SQL version(s) predate explicit knowledge usage; "
                f"samples: {', '.join(samples)}",
                self.root,
            )
        warnings = [item for item in self.checks if item["status"] == "warn"]
        errors = [item for item in self.checks if item["status"] == "fail"]
        if errors:
            status = "fail"
        elif warnings:
            status = "warn"
        else:
            status = "pass"
        passed = sum(1 for item in self.checks if item["status"] == "pass")
        return {
            "project": self.project,
            "status": status,
            "root": str(self.root),
            "strict": self.strict,
            "scope": self.scope,
            "summary": {
                "checks": len(self.checks),
                "passed": passed,
                "warnings": len(warnings),
                "failures": len(errors),
            },
            "checks": self.checks,
            "warnings": warnings,
            "errors": errors,
            "deferred_history": copy.deepcopy(self.deferred_history),
        }


def exit_code_for_status(status: str) -> int:
    if status == "pass":
        return 0
    if status == "fail":
        return 1
    if status == "warn":
        return 2
    return 3


def validate_project_config(report: HealthReport, config: dict[str, Any] | None, path: Path) -> dict[str, Any]:
    if config is None:
        report.fail("project_config.exists", "project_config.json is required.", path)
        return {}
    report.pass_check("project_config.exists", "project_config.json exists and parses.", path)

    missing_fields = [field for field in REQUIRED_CONFIG_FIELDS if field not in config]
    if missing_fields:
        report.fail(
            "project_config.required_fields",
            "project_config.json is missing required fields: " + ", ".join(missing_fields),
            path,
        )
    else:
        report.pass_check("project_config.required_fields", "project_config.json contains required fields.", path)

    retired_service_fields = sorted(
        field
        for field in {"development_query", "browser_query_execution"}
        if field in config
    )
    if retired_service_fields:
        report.fail(
            "project_config.data_service_ownership",
            "Move direct service fields to the shared catalog and stage data_services.json: "
            + ", ".join(retired_service_fields),
            path,
        )
    else:
        report.pass_check(
            "project_config.data_service_ownership",
            "Physical services and stage bindings are separated from SQL semantics.",
            path,
        )

    dialect = config.get("sql_dialect")
    if dialect not in ALLOWED_DIALECTS:
        report.fail("project_config.sql_dialect", "sql_dialect must be Hive or StarRocks.", path)
    else:
        report.pass_check("project_config.sql_dialect", f"sql_dialect is configured as {dialect}.", path)

    if is_missing(config.get("query_engine")):
        report.fail("project_config.query_engine", "query_engine must be configured.", path)
    else:
        report.pass_check("project_config.query_engine", "query_engine is configured.", path)

    if is_missing(config.get("query_environment")):
        report.fail("project_config.query_environment", "query_environment must be configured.", path)
    else:
        report.pass_check("project_config.query_environment", "query_environment is configured.", path)

    if is_missing(config.get("dashboard_application")):
        report.fail("project_config.dashboard_application", "dashboard_application must be configured.", path)
    else:
        report.pass_check("project_config.dashboard_application", "dashboard_application is configured.", path)

    profile = config.get("table_naming_profile", {})
    if not isinstance(profile, dict) or is_missing(profile) or not profile.get("pattern"):
        report.fail(
            "project_config.table_naming_profile",
            "table_naming_profile must be configured with a physical table pattern.",
            path,
        )
    else:
        report.pass_check("project_config.table_naming_profile", "table_naming_profile is configured.", path)
        if profile.get("dialect") and dialect in ALLOWED_DIALECTS and profile.get("dialect") != dialect:
            report.fail(
                "project_config.table_profile_dialect",
                "table_naming_profile.dialect must match sql_dialect.",
                path,
            )
        if "{log_lower}" not in str(profile.get("pattern", "")):
            report.warn(
                "project_config.table_profile_pattern",
                "table_naming_profile.pattern does not contain {log_lower}; confirm this is intentional.",
                path,
            )

    policy = config.get("partition_policy", {})
    if not isinstance(policy, dict) or policy.get("strict_generation") is not True:
        report.fail(
            "project_config.partition_policy",
            "partition_policy.strict_generation must be true.",
            path,
        )
    elif policy.get("required_for_tlog") is True and is_missing(policy.get("partition_field")):
        report.fail(
            "project_config.partition_policy",
            "partition_policy.partition_field is required when required_for_tlog is true.",
            path,
        )
    else:
        report.pass_check("project_config.partition_policy", "partition_policy is strict and complete.", path)

    profile_name = profile.get("name", "") if isinstance(profile, dict) else ""
    query_environment_name = config.get("query_environment", {}).get("name", "") if isinstance(config.get("query_environment"), dict) else str(config.get("query_environment") or "")
    if profile_name == "demo_hive" and "tdbank" in f"{config.get('query_engine', '')} {query_environment_name}".lower():
        report.fail(
            "project_config.demo_hive_engine",
            "demo_hive is an Demo Hive event-time profile; do not configure it as TDBank.",
            path,
        )
    if profile_name == "demo_hive" and isinstance(policy, dict) and policy.get("name") != "demo_log_dt_event_date":
        report.fail(
            "project_config.demo_hive_partition_policy",
            "demo_hive must use partition_policy.name=demo_log_dt_event_date.",
            path,
        )
    if profile_name == "demo_starrocks" and isinstance(policy, dict) and policy.get("name") != "demo_log_dt_event_date":
        report.fail(
            "project_config.demo_starrocks_partition_policy",
            "demo_starrocks must use partition_policy.name=demo_log_dt_event_date.",
            path,
        )
    if profile_name == "demo_abtest_hive" and isinstance(policy, dict) and policy.get("name") != "tdbank_hourly":
        report.fail(
            "project_config.demo_abtest_hive_partition_policy",
            "demo_abtest_hive must use partition_policy.name=tdbank_hourly.",
            path,
        )

    if dialect == "StarRocks" and policy.get("requires_schema_confirmation") is not True:
        report.fail(
            "project_config.starrocks_schema_confirmation",
            "StarRocks projects must require schema/partition confirmation.",
            path,
        )
    if dialect == "StarRocks" and str(policy.get("partition_field", "")).lower() == "tdbank_imp_date":
        report.fail(
            "project_config.starrocks_time_policy",
            "StarRocks projects must not default to TDBank partition field tdbank_imp_date.",
            path,
        )
    if policy.get("business_time_required") is True and is_missing(policy.get("business_time_field")):
        report.fail(
            "project_config.business_time_field",
            "business_time_field is required when business_time_required is true.",
            path,
        )

    query_window = config.get("default_query_window")
    window_problems = validate_default_query_window(config) if isinstance(query_window, dict) else []
    if not isinstance(query_window, dict):
        report.warn(
            "project_config.default_query_window",
            "Project default query window is not configured; QUERY requests without explicit dates must ask for a date range.",
            path,
        )
    elif window_problems:
        report.fail(
            "project_config.default_query_window",
            " ".join(window_problems),
            path,
        )
    elif query_window.get("mode") == "missing":
        report.warn(
            "project_config.default_query_window",
            "Project start date is not configured; QUERY requests without explicit dates must ask for a date range.",
            path,
        )
    else:
        report.pass_check(
            "project_config.default_query_window",
            "QUERY requests without dates resolve to fixed project-start-through-yesterday bounds.",
            path,
        )

    if not isinstance(config.get("table_overrides"), dict):
        report.fail("project_config.table_overrides", "table_overrides must be an object.", path)
    else:
        report.pass_check("project_config.table_overrides", "table_overrides is an object.", path)

    try:
        repo = path.resolve().parent.parent.parent
        project_id = str(config.get("project_id") or path.parent.name)
        stage_services = data_service.load_stage(repo, project_id)
        data_service.load_catalog(repo)
        summaries = []
        for purpose, binding in stage_services["bindings"].items():
            binding_status = str(binding.get("status"))
            if binding_status == "confirmed":
                resolved = data_service.resolve(repo, project_id, purpose)
                summaries.append(f"{purpose}={resolved['service_id']}")
            else:
                summaries.append(f"{purpose}={binding_status}")
        report.pass_check(
            "project_config.data_services",
            "Explicit product/stage data-service bindings are valid: " + ", ".join(summaries),
            data_service.stage_path(repo, project_id),
        )
        planning_binding_path = path.parent / "planning" / "source_binding.json"
        if planning_binding_path.is_file():
            planning_binding, planning_error = read_json_file(planning_binding_path)
            if planning_error or not isinstance(planning_binding, dict):
                report.fail(
                    "project_config.stage_identity",
                    planning_error or "Planning source binding is invalid.",
                    planning_binding_path,
                )
            elif (
                planning_binding.get("product_id") != stage_services["product_id"]
                or planning_binding.get("stage_id") != stage_services["stage_id"]
            ):
                report.fail(
                    "project_config.stage_identity",
                    "Planning source and data-service bindings must use the same product/stage identity.",
                    planning_binding_path,
                )
            else:
                report.pass_check(
                    "project_config.stage_identity",
                    "Planning source and data services share one explicit product/stage identity.",
                    planning_binding_path,
                )
    except data_service.DataServiceError as error:
        report.fail("project_config.data_services", str(error), path)

    contract = config.get("generation_contract", {})
    required_true = [
        "strict_dialect_rules",
        "require_query_environment_for_query",
        "require_dashboard_application_for_dashboard",
        "block_formal_sql_when_config_missing",
    ]
    missing_contract = [key for key in required_true if not isinstance(contract, dict) or contract.get(key) is not True]
    if missing_contract:
        report.fail(
            "project_config.generation_contract",
            "generation_contract must set these fields to true: " + ", ".join(missing_contract),
            path,
        )
    else:
        report.pass_check("project_config.generation_contract", "generation_contract hard gates are enabled.", path)

    adapter_problems = adapter_config_problems(config)
    if adapter_problems:
        report.fail(
            "project_config.execution_adapters",
            "Execution adapter contract is invalid: " + "; ".join(adapter_problems),
            path,
        )
    elif "execution_adapters" in config:
        report.pass_check(
            "project_config.execution_adapters",
            "Fast/stable execution adapters are configured.",
            path,
        )

    identifier_problems = identifier_policy_config_problems(config)
    if identifier_problems:
        report.fail(
            "project_config.identifier_policy",
            "Identifier policy is invalid: " + "; ".join(identifier_problems),
            path,
        )
    elif config.get("identifier_policy"):
        report.pass_check(
            "project_config.identifier_policy",
            "Executor-specific exact-case identifier policy is configured.",
            path,
        )

    time_integrity_problems = time_integrity_config_problems(config)
    if time_integrity_problems:
        report.fail(
            "project_config.time_integrity_policy",
            "Time integrity policy is invalid: " + "; ".join(time_integrity_problems),
            path,
        )
    elif config.get("time_integrity_policy"):
        report.pass_check(
            "project_config.time_integrity_policy",
            "Time integrity policy is configured.",
            path,
        )

    return config


def load_concept_keys(report: HealthReport, root: Path) -> set[str]:
    registry_path = root.parent / "_rule_review" / "rule_concepts.json"
    registry, error = read_json_file(registry_path)
    if error:
        report.fail("concept_registry.exists", error, registry_path)
        return set()
    concepts = registry.get("concepts", [])
    if not isinstance(concepts, list):
        report.fail("concept_registry.shape", "rule_concepts.json concepts must be an array.", registry_path)
        return set()

    keys: set[str] = set()
    duplicates: set[str] = set()
    for item in concepts:
        if not isinstance(item, dict):
            continue
        key = slug_text(str(item.get("concept_key") or ""))
        if not key:
            continue
        if key in keys:
            duplicates.add(key)
        keys.add(key)

    if duplicates:
        report.fail(
            "concept_registry.duplicate_keys",
            "Duplicate concept_key values: " + ", ".join(sorted(duplicates)),
            registry_path,
        )
    else:
        report.pass_check("concept_registry.available", f"Registered concept keys: {len(keys)}.", registry_path)
    return keys


def validate_canonical_rules(report: HealthReport, root: Path, registered_keys: set[str]) -> None:
    rule_store = RuleStore(root)
    validation = rule_store.validate_store(
        require_no_legacy=True,
        require_activation_v2=True,
    )
    rule_path = rule_store.store_path
    if validation.get("status") != "ok":
        report.fail(
            "canonical_rules.store_v2",
            "Canonical Rule Store v2 validation failed: " + "; ".join(validation.get("errors", [])),
            rule_path,
        )
        return
    report.pass_check(
        "canonical_rules.store_v2",
        (
            "Canonical Rule Store v2 is valid: "
            f"{validation['counts']['concepts']} concepts, {validation['counts']['versions']} versions."
        ),
        rule_path,
    )
    rules_doc = rule_store.load_store()
    rules = rule_store.load_all_versions()
    if not isinstance(rules, list):
        report.fail("canonical_rules.shape", "Rule Store versions must resolve to an array.", rule_path)
        return

    failures = 0
    warnings = 0
    shared_event_signature_failures = 0
    authorization_contract = rules_doc.get("authorization_contract") if isinstance(rules_doc, dict) else {}
    authorization_enforced_at = ""
    if isinstance(authorization_contract, dict):
        authorization_enforced_at = str(authorization_contract.get("enforced_at") or "")
    try:
        authorization_amendments = amendment_index(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report.fail(
            "canonical_rules.authorization_amendments",
            f"Cannot load immutable authorization amendments: {exc}",
            rule_path,
        )
        authorization_amendments = {}
        failures += 1

    def authorization_required(rule: dict[str, Any]) -> bool:
        if not authorization_enforced_at:
            return False
        try:
            created = datetime.fromisoformat(str(rule.get("created_at") or "").replace("Z", "+00:00"))
            enforced = datetime.fromisoformat(authorization_enforced_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if enforced.tzinfo is None:
                enforced = enforced.replace(tzinfo=timezone.utc)
            return created.astimezone(timezone.utc) >= enforced.astimezone(timezone.utc)
        except ValueError:
            return True

    historical_manifest_cache: dict[Path, tuple[dict[str, Any], list[str]]] = {}
    active_dependency_cache: dict[tuple[str, str, tuple[str, ...]], str] = {}

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_id = str(rule.get("rule_id") or "unknown-rule")
        status = str(rule.get("status") or "")
        scope = str(rule.get("scope") or "")
        lifetime = str(rule.get("lifetime") or "")
        concept_key = slug_text(str(rule.get("concept_key") or ""))
        path_label = f"{display_path(root, rule_path)}#{rule_id}"
        source_evidence = str(rule.get("source_evidence") or "").strip()
        if authorization_required(rule):
            authorization = rule.get("change_authorization")
            store_meta = rule.get("_rule_store") if isinstance(rule.get("_rule_store"), dict) else {}
            amendment = authorization_amendments.get(
                (
                    str(store_meta.get("path") or ""),
                    str(store_meta.get("record_sha256") or ""),
                )
            )
            amended_authorization = (
                amendment.get("authorization")
                if isinstance(amendment, dict) and isinstance(amendment.get("authorization"), dict)
                else {}
            )
            valid_authorization = (
                isinstance(authorization, dict)
                and authorization.get("contract_version") == "rule_write_authorization_v1"
                and authorization.get("function_id") == "RULES"
                and authorization.get("explicit_user_selection") is True
                and re.fullmatch(r"[a-f0-9]{64}", str(authorization.get("user_request_sha256") or ""))
            )
            valid_amendment = (
                amended_authorization.get("contract_version") == "rule_write_authorization_v1"
                and amended_authorization.get("function_id") == "RULES"
                and amended_authorization.get("explicit_user_selection") is True
                and re.fullmatch(
                    r"[a-f0-9]{64}",
                    str(amended_authorization.get("user_request_sha256") or ""),
                )
            )
            if not (valid_authorization or valid_amendment):
                report.fail(
                    "canonical_rules.change_authorization",
                    f"Rule `{rule_id}` was written after authorization enforcement but lacks an explicit RULES change authorization audit.",
                    path_label,
                )
                failures += 1
        for match in local_absolute_references(source_evidence):
            report.fail(
                "canonical_rules.source_evidence_project_relative",
                f"Rule `{rule_id}` source_evidence must not reference local absolute path `{match}`; copy evidence into the project and use a project-relative path.",
                path_label,
            )
            failures += 1

        if scope.lower() == "global":
            report.fail(
                "canonical_rules.no_global_business_rule",
                f"Rule `{rule_id}` uses forbidden global scope.",
                path_label,
            )
            failures += 1

        if scope == "project" and lifetime == "persistent":
            severity = "warn" if status in {"superseded", "deprecated"} else "fail"
            if not concept_key:
                report.add(
                    "canonical_rules.concept_key_required",
                    severity,
                    f"Project persistent rule `{rule_id}` must have a concept_key.",
                    path_label,
                )
                if severity == "fail":
                    failures += 1
                else:
                    warnings += 1
            elif concept_key not in registered_keys:
                report.add(
                    "canonical_rules.concept_key_registered",
                    severity,
                    f"Rule `{rule_id}` uses unregistered concept_key `{concept_key}`.",
                    path_label,
                )
                if severity == "fail":
                    failures += 1
                else:
                    warnings += 1

        structured = rule.get("structured_definition") if isinstance(rule.get("structured_definition"), dict) else {}
        dependencies = structured.get("knowledge_dependencies", [])
        if dependencies is not None and not isinstance(dependencies, list):
            report.fail(
                "canonical_rules.knowledge_dependencies",
                f"Rule `{rule_id}` structured_definition.knowledge_dependencies must be an array.",
                path_label,
            )
            failures += 1
        elif isinstance(dependencies, list):
            seen_dependencies: set[tuple[str, str, str]] = set()
            allowed_roles = {
                "label_mapping",
                "classification",
                "filter_set",
                "parameter_source",
                "field_semantics",
                "authoring_reference",
            }
            for dependency in dependencies:
                if not isinstance(dependency, dict):
                    report.fail(
                        "canonical_rules.knowledge_dependencies",
                        f"Rule `{rule_id}` contains a non-object knowledge dependency.",
                        path_label,
                    )
                    failures += 1
                    continue
                dataset_id = str(dependency.get("dataset_id") or "")
                projection_id = str(dependency.get("projection_id") or "")
                semantic_role = str(dependency.get("semantic_role") or "")
                fields = dependency.get("fields")
                identity = (dataset_id, projection_id, semantic_role)
                problems = []
                if not re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", dataset_id):
                    problems.append("invalid dataset_id")
                if not re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", projection_id):
                    problems.append("invalid projection_id")
                if semantic_role not in allowed_roles:
                    problems.append("invalid semantic_role")
                if not isinstance(fields, list) or not fields or any(not isinstance(field, str) or not field for field in fields):
                    problems.append("fields must be a non-empty string array")
                elif len(fields) != len(set(fields)):
                    problems.append("fields must be unique")
                if not isinstance(dependency.get("required"), bool):
                    problems.append("required must be boolean")
                binding_policy = str(dependency.get("binding_policy") or "")
                if binding_policy not in {"active_project_binding", "exact"}:
                    problems.append("binding_policy must be active_project_binding or legacy exact")
                if binding_policy == "active_project_binding" and any(
                    dependency.get(field) not in {None, ""}
                    for field in ["dataset_version", "content_hash", "projection_sha256"]
                ):
                    problems.append("active_project_binding must not embed immutable version/hash pins")
                if identity in seen_dependencies:
                    problems.append("duplicate dataset/projection/role dependency")
                seen_dependencies.add(identity)
                if problems:
                    report.fail(
                        "canonical_rules.knowledge_dependencies",
                        f"Rule `{rule_id}` has invalid knowledge dependency: {'; '.join(problems)}.",
                        path_label,
                    )
                    failures += 1
                    continue
                dependency_problems = []
                if binding_policy == "exact":
                    for field in ["dataset_version", "content_hash", "projection_sha256"]:
                        value = str(dependency.get(field) or "")
                        if field == "dataset_version" and not re.fullmatch(r"kdv-[a-f0-9]{12}", value):
                            dependency_problems.append("dataset_version must pin one immutable kdv version")
                        elif field != "dataset_version" and not re.fullmatch(r"[a-f0-9]{64}", value):
                            dependency_problems.append(f"{field} must be a SHA-256 hash")
                    if not dependency_problems:
                        try:
                            repo_root = discover_repo_root(root)
                            manifest_path = (
                                repo_root
                                / "knowledge-base"
                                / "datasets"
                                / dataset_id
                                / str(dependency["dataset_version"])
                                / "manifest.json"
                            )
                            if manifest_path not in historical_manifest_cache:
                                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                                manifest_validation = validate_dataset_manifest(repo_root, manifest)
                                historical_manifest_cache[manifest_path] = (
                                    manifest,
                                    list(manifest_validation.get("problems", []) or []),
                                )
                            manifest, manifest_problems = historical_manifest_cache[manifest_path]
                            dependency_problems.extend(manifest_problems)
                            if manifest.get("version") != dependency.get("dataset_version"):
                                dependency_problems.append("dataset_version does not match its historical manifest")
                            if manifest.get("content_hash") != dependency.get("content_hash"):
                                dependency_problems.append("content_hash does not match its historical manifest")
                            projection = next(
                                (
                                    item
                                    for item in manifest.get("projections", []) or []
                                    if item.get("projection_id") == projection_id
                                ),
                                None,
                            )
                            if not projection:
                                dependency_problems.append(
                                    f"historical manifest lacks projection {projection_id}"
                                )
                            elif projection.get("sha256") != dependency.get("projection_sha256"):
                                dependency_problems.append(
                                    "projection_sha256 does not match its historical manifest"
                                )
                        except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
                            dependency_problems.append(
                                f"cannot validate historical exact dependency: {exc}"
                            )
                if status == "confirmed" and dependency.get("required"):
                    active_key = (dataset_id, projection_id, tuple(sorted(fields)))
                    if active_key not in active_dependency_cache:
                        try:
                            resolve_knowledge(
                                project_root=root,
                                dataset_id=dataset_id,
                                projection_id=projection_id,
                                usage_mode="authoring_reference",
                                fields=list(fields),
                                limit=1,
                            )
                            active_dependency_cache[active_key] = ""
                        except (ValueError, OSError, json.JSONDecodeError) as exc:
                            active_dependency_cache[active_key] = str(exc)
                    if active_dependency_cache[active_key]:
                        dependency_problems.append(
                            "active project binding is incompatible: "
                            + active_dependency_cache[active_key]
                        )
                if dependency_problems:
                    report.fail(
                        "canonical_rules.knowledge_dependency_binding",
                        f"Rule `{rule_id}` has an unresolved required knowledge dependency: "
                        + "; ".join(dependency_problems)
                        + ".",
                        path_label,
                    )
                    failures += 1

        if status == "confirmed":
            inline_mapping = bool(structured.get("level_mapping")) or any(
                isinstance(item, dict) and item.get("type") == "must_use_item_level_mapping"
                for item in (rule.get("activation_contract") or {}).get("hard_constraints", [])
            )
            if inline_mapping:
                report.fail(
                    "canonical_rules.no_large_inline_mapping",
                    (
                        f"Current confirmed rule `{rule_id}` contains an inline level/item mapping. "
                        "Move the mapping to a versioned project-bound knowledge dataset and keep only "
                        "the logical active-project-binding dependency in the rule."
                    ),
                    path_label,
                )
                failures += 1
            mapping_tables = [
                item
                for item in (structured.get("mapping_tables") or [])
                if isinstance(item, dict) and (item.get("rows") or [])
            ]
            if mapping_tables:
                report.fail(
                    "canonical_rules.no_inline_mapping_tables",
                    (
                        f"Current confirmed rule `{rule_id}` embeds mutable mapping rows in "
                        "structured_definition.mapping_tables. Move rows to a versioned knowledge "
                        "dataset and pin the exact dependency from the rule."
                    ),
                    path_label,
                )
                failures += 1
            required_dependencies = [
                item
                for item in (dependencies or [])
                if isinstance(item, dict) and item.get("required") is True
            ]
            if str(concept_key).endswith("-map") and not required_dependencies:
                report.fail(
                    "canonical_rules.mapping_requires_knowledge",
                    (
                        f"Current confirmed mapping rule `{rule_id}` has no required pinned knowledge "
                        "dependency. Mapping values must not live only in rule prose."
                    ),
                    path_label,
                )
                failures += 1

        contract = rule.get("activation_contract")
        if status == "confirmed" and isinstance(contract, dict):
            shared_logs = shared_logs_from_activation_contract(contract)
            event_signature = contract.get("event_signature")
            activation_policy = contract.get("activation_policy")
            reverse_policy = (
                str(activation_policy.get("reverse") or "disabled")
                if isinstance(activation_policy, dict)
                else "disabled"
            )
            if shared_logs and reverse_policy != "disabled" and not isinstance(event_signature, dict):
                report.fail(
                    "canonical_rules.shared_log_event_signature",
                    (
                        f"Confirmed rule `{rule_id}` declares shared source log(s) "
                        f"{', '.join(shared_logs)} but lacks activation_contract.event_signature; "
                        "runtime reverse matching cannot derive this boundary from prose."
                    ),
                    path_label,
                )
                failures += 1
                shared_event_signature_failures += 1
            elif (
                shared_logs
                and reverse_policy != "disabled"
                and not event_signature_has_exact_core(event_signature)
                and not event_signature_is_boundary_only(contract, event_signature)
            ):
                report.fail(
                    "canonical_rules.shared_log_event_signature_core",
                    (
                        f"Confirmed rule `{rule_id}` declares shared source log(s) "
                        f"{', '.join(shared_logs)} but event_signature lacks exact-match core. "
                        "Metric rules must declare required_metric_role(s), required_aggregation(s), "
                        "and predicate or required_field_role boundary evidence; "
                        "non-metric boundary rules must declare match_policy=boundary_only."
                    ),
                    path_label,
                )
                failures += 1
                shared_event_signature_failures += 1

    if failures == 0 and warnings == 0:
        report.pass_check(
            "canonical_rules.concept_keys",
            "All project persistent rules use registered concept_key values.",
            rule_path,
        )
    if shared_event_signature_failures == 0:
        report.pass_check(
            "canonical_rules.shared_log_event_signatures",
            "Confirmed shared-log rules declare exact event_signature cores or boundary-only policies.",
            rule_path,
        )


def validate_source_contracts(report: HealthReport, root: Path) -> None:
    sources_dir = root / "sources"
    if not sources_dir.exists():
        return
    checked = 0
    failures = 0
    for source_json_path in sorted(sources_dir.glob("*.json")):
        data, error = read_json_file(source_json_path)
        if error or not data:
            report.fail("sources.json_parse", error or "Source JSON is empty.", source_json_path)
            failures += 1
            continue
        checked += 1
        failures += validate_json_project_relative_references(
            report,
            data,
            source_json_path,
            "sources.project_relative_references",
            source_json_path.name,
        )
        for key in ["source_evidence", "schema_path", "source_file"]:
            value = str(data.get(key) or "").strip()
            if not value:
                continue
            if is_absolute_reference(value):
                report.fail(
                    "sources.evidence_project_relative",
                    f"{source_json_path.name} `{key}` must be project-relative, not local absolute path `{value}`.",
                    source_json_path,
                )
                failures += 1
                continue
            target = resolve_project_path(root, value)
            if not target.exists():
                report.fail(
                    "sources.evidence_exists",
                    f"{source_json_path.name} `{key}` references missing project file `{value}`.",
                    source_json_path,
                )
                failures += 1
    if checked and failures == 0:
        report.pass_check(
            "sources.evidence_project_relative",
            f"{checked} source JSON contract(s) use project-relative evidence paths.",
            sources_dir,
        )


def expected_meta_path(sql_path: Path) -> Path:
    return sql_path.with_name(sql_path.stem + ".meta.json")


def sql_path_from_meta_path(meta_path: Path) -> Path:
    name = meta_path.name
    if name.endswith(".meta.json"):
        return meta_path.with_name(name[: -len(".meta.json")] + ".sql")
    return meta_path.with_suffix(".sql")


def check_rel_exists(report: HealthReport, root: Path, check_id: str, value: str, message: str) -> bool:
    if not value:
        return True
    target = resolve_project_path(root, value)
    if target.exists():
        report.pass_check(check_id, message + " exists.", target)
        return True
    report.fail(check_id, message + f" does not exist: {value}", target)
    return False


def artifact_label(item: dict[str, Any]) -> str:
    version = item.get("version", "")
    version_text = f"v{int(version):03d}" if isinstance(version, int) else str(version or "unknown-version")
    return f"{item.get('kind', 'UNKNOWN')}/{item.get('slug', 'unknown-slug')}/{version_text}"


def normalize_table_key(value: Any) -> str:
    return str(value or "").strip().strip("`").lower()


def is_current_table(item: dict[str, Any]) -> bool:
    return (item.get("table_state") or "current") == "current" and item.get("status") not in {"superseded", "deprecated"}


def table_label(item: dict[str, Any]) -> str:
    version = item.get("version", "")
    version_text = f"v{int(version):03d}" if isinstance(version, int) else str(version or "unknown-version")
    return f"TABLE/{item.get('slug') or item.get('table_name') or 'unknown-table'}/{version_text}"


def current_table_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in manifest.get("intermediate_tables", []):
        if not isinstance(item, dict) or not is_current_table(item):
            continue
        for key in [item.get("table_name"), item.get("slug")]:
            normalized = normalize_table_key(key)
            if normalized:
                index[normalized] = item
    return index


def validate_spec_block(
    report: HealthReport,
    root: Path,
    item: dict[str, Any],
    sql_path: Path,
    meta: dict[str, Any],
    strict: bool,
    config: dict[str, Any],
) -> None:
    kind = str(item.get("kind") or "")
    markers = HEADER_MARKERS.get(kind)
    if not markers:
        report.warn("artifact.header_kind", f"{artifact_label(item)} has unknown kind `{kind}`.", sql_path)
        return
    sql_text, error = read_text_file(sql_path)
    if error or sql_text is None:
        report.fail("artifact.sql_readable", error or "SQL file is not readable.", sql_path)
        return

    start_marker, end_marker = markers
    starts = re.findall(rf"^\s*(?:/\*\s*)?{re.escape(start_marker)}\b", sql_text, flags=re.M)
    ends = re.findall(rf"^\s*{re.escape(end_marker)}\b", sql_text, flags=re.M)
    severity = issue_status_for_artifact(item, strict)
    if len(starts) != 1 or len(ends) != 1:
        report.add(
            "artifact.short_header",
            severity,
            f"{artifact_label(item)} must contain exactly one {start_marker} short header; found start={len(starts)}, end={len(ends)}.",
            sql_path,
        )
    else:
        report.pass_check("artifact.short_header", f"{artifact_label(item)} has the expected short header.", sql_path)
        header_text = sql_text.split(end_marker, 1)[0]
        header_lines = len(header_text.splitlines()) + 1
        budget = HEADER_LINE_BUDGET.get(kind, 80)
        if header_lines > budget:
            report.warn(
                "artifact.short_header_budget",
                f"{artifact_label(item)} short header has {header_lines} lines; budget is {budget}.",
                sql_path,
            )
        else:
            report.pass_check(
                "artifact.short_header_budget",
                f"{artifact_label(item)} short header is within the {budget}-line budget.",
                sql_path,
            )

    if has_full_spec_block(sql_text):
        report.add(
            "artifact.full_spec_block_removed",
            severity,
            f"{artifact_label(item)} still contains a legacy full @...SPEC block; formal SQL must use sidecar spec JSON.",
            sql_path,
        )
    else:
        report.pass_check("artifact.full_spec_block_removed", f"{artifact_label(item)} has no legacy full spec block.", sql_path)

    placeholders = sorted({match.group(0) for match in PLACEHOLDER_PATTERN.finditer(sql_text)})
    if placeholders:
        report.add(
            "artifact.spec_placeholders",
            severity,
            f"{artifact_label(item)} contains unresolved template placeholders: {', '.join(placeholders[:8])}.",
            sql_path,
        )
    else:
        report.pass_check("artifact.spec_placeholders", f"{artifact_label(item)} has no unresolved template placeholders.", sql_path)

    execution_contract_severity = execution_contract_status_for_artifact(item)
    validate_dialect_sql_patterns(report, item, sql_path, sql_text, config, execution_contract_severity)

    validate_sidecar_spec(report, root, item, sql_path, meta, severity)
    spec, spec_errors = load_sidecar_spec(root, item, sql_path)
    if spec_errors:
        return
    validate_performance_level_contract(report, item, sql_path, spec or {})

    if kind == "QUERY":
        problems = query_params_contract_problems(sql_text, config, spec or {})
        if problems:
            report.add(
                "artifact.query_params_contract",
                execution_contract_severity,
                f"{artifact_label(item)} must satisfy the top-params and project time contract: {'; '.join(problems)}",
                sql_path,
            )
        else:
            report.pass_check(
                "artifact.query_params_contract",
                f"{artifact_label(item)} has valid top params and project time handling.",
                sql_path,
            )

    if kind == "DASHBOARD":
        validate_dashboard_top_contract(report, item, sql_path, sql_text, spec or {}, severity)


def validate_sidecar_spec(
    report: HealthReport,
    root: Path,
    item: dict[str, Any],
    sql_path: Path,
    meta: dict[str, Any],
    severity: str,
) -> None:
    label = artifact_label(item)
    spec_path = spec_path_for_artifact(root, item, sql_path)
    spec_rel = normalize_rel(spec_path.relative_to(root)) if spec_path.is_absolute() and spec_path.exists() else normalize_rel(str(item.get("spec_path") or spec_path.relative_to(root)))
    spec, errors = load_sidecar_spec(root, item, sql_path)
    if errors:
        for error in errors:
            report.add("artifact.spec_sidecar", severity, f"{label} {error}", spec_path)
        return
    report.pass_check("artifact.spec_sidecar", f"{label} sidecar spec exists and parses.", spec_path)
    validate_json_project_relative_references(
        report,
        spec or {},
        spec_path,
        "artifact.spec_project_relative_references",
        f"{label} sidecar spec",
    )
    knowledge_references = (spec or {}).get("knowledge_references", [])
    if not isinstance(knowledge_references, list):
        report.add(
            "artifact.knowledge_references",
            severity,
            f"{label} knowledge_references must be an array.",
            spec_path,
        )
    else:
        reference_problems = [
            problem
            for reference in knowledge_references
            if isinstance(reference, dict)
            for problem in validate_knowledge_reference(
                root,
                reference,
                require_current_binding=False,
            )
        ]
        invalid_rows = sum(not isinstance(reference, dict) for reference in knowledge_references)
        if invalid_rows:
            reference_problems.append(f"{invalid_rows} knowledge reference row(s) are not objects")
        for problem in reference_problems:
            report.add("artifact.knowledge_reference_integrity", severity, f"{label}: {problem}", spec_path)
        if knowledge_references and not reference_problems:
            report.pass_check(
                "artifact.knowledge_reference_integrity",
                f"{label} preserves {len(knowledge_references)} immutable knowledge reference(s).",
                spec_path,
            )
    knowledge_usage = (spec or {}).get("knowledge_usage")
    provenance = (spec or {}).get("generation_provenance")
    generated_skill_version = str(
        provenance.get("skill_version") if isinstance(provenance, dict) else ""
    )
    if not isinstance(knowledge_usage, dict) or not knowledge_usage:
        if _version_tuple(generated_skill_version) >= (4, 156, 0):
            report.add(
                "artifact.knowledge_usage",
                severity,
                f"{label} must declare knowledge_usage for this generated SQL version.",
                spec_path,
            )
        else:
            report.record_legacy_knowledge_usage(spec_path)
    else:
        # Health audits the immutable version that was used; save/formalize owns current-binding gates.
        usage_problems = validate_knowledge_usage(
            root,
            knowledge_usage,
            knowledge_references,
            require_current_binding=False,
        )
        for problem in usage_problems:
            report.add("artifact.knowledge_usage", severity, f"{label}: {problem}", spec_path)
        if not usage_problems:
            report.pass_check(
                "artifact.knowledge_usage",
                f"{label} declares knowledge usage as {knowledge_usage.get('status')}.",
                spec_path,
            )

    if item.get("spec_storage") != SPEC_STORAGE:
        report.add("artifact.spec_storage", severity, f"{label} manifest spec_storage must be {SPEC_STORAGE}.", sql_path)
    if meta and meta.get("spec_storage") != SPEC_STORAGE:
        report.add("artifact.spec_storage", severity, f"{label} metadata spec_storage must be {SPEC_STORAGE}.", sql_path)

    if not item.get("spec_path"):
        report.add("artifact.spec_path", severity, f"{label} manifest is missing spec_path.", sql_path)
    elif normalize_rel(item.get("spec_path")) != spec_rel:
        report.warn("artifact.spec_path", f"{label} manifest spec_path differs from expected sidecar path.", sql_path)
    else:
        report.pass_check("artifact.spec_path", f"{label} manifest spec_path points to the sidecar.", spec_path)

    if meta and meta.get("spec_path") != item.get("spec_path"):
        report.warn("artifact.spec_path_meta", f"{label} metadata spec_path differs from manifest.", sql_path)

    meta_block = (spec or {}).get("spec_meta") or {}
    if str(meta_block.get("spec_version")) != "4.8":
        report.add("artifact.spec_version", severity, f"{label} spec_meta.spec_version must be 4.8.", spec_path)
    else:
        report.pass_check("artifact.spec_version", f"{label} spec version is 4.8.", spec_path)

    validate_generation_provenance(report, item, meta, spec or {}, sql_path)



def validate_generation_provenance(
    report: HealthReport,
    item: dict[str, Any],
    meta: dict[str, Any],
    spec: dict[str, Any],
    sql_path: Path,
) -> None:
    label = artifact_label(item)
    provenance = spec.get("generation_provenance")
    required = ["schema_version", "skill_name", "skill_version", "sql_spec_version", "workflow", "generated_by_script", "generated_at"]
    if not isinstance(provenance, dict) or not provenance:
        report.warn(
            "artifact.generation_provenance",
            f"{label} sidecar spec is missing generation_provenance; run the historical provenance migration before relying on generator-version audits.",
            sql_path,
        )
        return
    missing = [key for key in required if not provenance.get(key)]
    if missing:
        report.warn(
            "artifact.generation_provenance",
            f"{label} generation_provenance is missing field(s): {', '.join(missing)}.",
            sql_path,
        )
    else:
        report.pass_check(
            "artifact.generation_provenance",
            f"{label} records generator skill version {provenance.get('skill_version')} via {provenance.get('generated_by_script')}.",
            sql_path,
        )
    item_provenance = item.get("generation_provenance")
    if not isinstance(item_provenance, dict) or not item_provenance:
        report.warn("artifact.generation_provenance_manifest", f"{label} manifest record is missing generation_provenance.", sql_path)
    if meta and (not isinstance(meta.get("generation_provenance"), dict) or not meta.get("generation_provenance")):
        report.warn("artifact.generation_provenance_meta", f"{label} metadata file is missing generation_provenance.", sql_path)


def validate_dialect_sql_patterns(
    report: HealthReport,
    item: dict[str, Any],
    sql_path: Path,
    sql_text: str,
    config: dict[str, Any],
    severity: str,
) -> None:
    config, _ = effective_config_for_context(
        config,
        sql_text,
        item.get("execution_route"),
    )
    dialect = str(config.get("sql_dialect") or "")
    cleaned = strip_sql_comments(sql_text)
    cleaned_lower = cleaned.lower()
    privacy_transforms = sql_side_privacy_transforms(sql_text)
    if privacy_transforms:
        functions = ", ".join(sorted({item["function"] for item in privacy_transforms}))
        report.add(
            "artifact.sql_side_privacy_transform",
            severity,
            f"{artifact_label(item)} performs SQL-side de-identification ({functions}); remove it and let DA handle privacy.",
            sql_path,
        )
    else:
        report.pass_check(
            "artifact.sql_side_privacy_transform",
            f"{artifact_label(item)} leaves privacy handling to DA and contains no SQL-side hash/mask transform.",
            sql_path,
        )
    string_collect_blocks = native_hive_string_collect_patterns(cleaned) if uses_non_native_hive_execution(config) else []
    if string_collect_blocks:
        report.add(
            "artifact.non_native_hive_string_collect",
            severity,
            f"{artifact_label(item)} uses collect_list/collect_set for string sample aggregation on a non-native Hive execution path; use group_concat(CAST(expr AS string/varchar)) or group_concat(concat(...)).",
            sql_path,
        )
    else:
        report.pass_check(
            "artifact.non_native_hive_string_collect",
            f"{artifact_label(item)} avoids Hive-native collect_list/collect_set string aggregation on compatibility execution paths.",
            sql_path,
        )
    unsafe_midnight_blocks = unsafe_midnight_concat_patterns(cleaned)
    if unsafe_midnight_blocks:
        report.add(
            "artifact.unsafe_midnight_concat",
            severity,
            f"{artifact_label(item)} appends a fixed 00:00:00 suffix with concat(); DA parameters, cohort_date, or date_add expressions may already include time. Use the parameter/expression directly or date/to_date/date_add functions without string suffix concatenation.",
            sql_path,
        )
    else:
        report.pass_check(
            "artifact.unsafe_midnight_concat",
            f"{artifact_label(item)} avoids manual 00:00:00 suffix concatenation for DA/date parameters.",
            sql_path,
        )
    identifier_findings = identifier_policy_findings(cleaned, config)
    for finding in identifier_findings:
        report.add(
            f"artifact.{finding.get('code', 'identifier_policy')}",
            severity,
            f"{artifact_label(item)}: {finding.get('message', 'SQL identifier violates the execution policy.')}",
            sql_path,
        )
    if dialect != "Hive":
        return
    policy = config.get("partition_policy", {})
    policy = policy if isinstance(policy, dict) else {}
    if (
        policy.get("required_for_tlog") is not True
        and not str(policy.get("partition_field") or "").strip()
        and "tdbank_imp_date" in cleaned_lower
    ):
        report.add(
            "artifact.unconfigured_import_partition",
            severity,
            f"{artifact_label(item)} uses tdbank_imp_date, but the project partition policy is event-time only.",
            sql_path,
        )
    distinct_group_blocks = select_distinct_group_by_blocks(cleaned)
    if distinct_group_blocks:
        report.add(
            "artifact.hive_select_distinct_group_by",
            severity,
            f"{artifact_label(item)} uses SELECT DISTINCT and GROUP BY in the same SELECT block; use GROUP BY-only dedup for Hive execution-chain compatibility.",
            sql_path,
        )
    else:
        report.pass_check(
            "artifact.hive_select_distinct_group_by",
            f"{artifact_label(item)} avoids SELECT DISTINCT + GROUP BY in the same Hive query block.",
            sql_path,
        )
    unsafe_aliases = sorted({match.group(1).strip("`") for match in TDBANK_UNSAFE_PARAM_ALIAS_PATTERN.finditer(cleaned)})
    if unsafe_aliases:
        report.add(
            "artifact.hive_safe_param_aliases",
            severity,
            f"{artifact_label(item)} uses parser-sensitive Hive params CTE alias(es): {', '.join(unsafe_aliases)}. Use ts_start, ts_end, pt_start, and pt_end only when those fields are applicable.",
            sql_path,
        )
    else:
        report.pass_check(
            "artifact.hive_safe_param_aliases",
            f"{artifact_label(item)} uses Hive-safe executable parameter aliases.",
            sql_path,
        )


def validate_performance_level_contract(
    report: HealthReport,
    item: dict[str, Any],
    sql_path: Path,
    spec: dict[str, Any],
) -> None:
    kind = str(item.get("kind") or "")
    if kind not in {"QUERY", "DASHBOARD"}:
        return
    label = artifact_label(item)
    performance = spec.get("performance_level")
    if not isinstance(performance, dict):
        report.warn("artifact.performance_level", f"{label} sidecar spec has no performance_level object.", sql_path)
        return
    missing = [key for key in REQUIRED_PERFORMANCE_KEYS if key not in performance]
    if missing:
        report.warn(
            "artifact.performance_level_fields",
            f"{label} sidecar performance_level is missing preflight field(s): {', '.join(missing)}.",
            sql_path,
        )
    else:
        report.pass_check("artifact.performance_level_fields", f"{label} sidecar records performance preflight fields.", sql_path)


def validate_dashboard_top_contract(
    report: HealthReport,
    item: dict[str, Any],
    sql_path: Path,
    sql_text: str,
    spec: dict[str, Any],
    severity: str,
) -> None:
    try:
        from dashboard_review import validate_top_contract
    except Exception as exc:  # noqa: BLE001
        report.fail("artifact.dashboard_parser", f"dashboard_review parser is not available: {exc}", sql_path)
        return

    errors, warnings = validate_top_contract(spec, sql_text)
    if errors:
        report.add(
            "artifact.dashboard_contract",
            severity,
            f"{artifact_label(item)} dashboard contract errors: {'; '.join(errors)}",
            sql_path,
        )
    else:
        report.pass_check("artifact.dashboard_contract", f"{artifact_label(item)} dashboard contract passed.", sql_path)
    for warning in warnings:
        report.warn("artifact.dashboard_contract_warning", warning, sql_path)


def validate_manifest_refs(
    report: HealthReport,
    root: Path,
    manifest: dict[str, Any] | None,
    manifest_path: Path,
) -> dict[str, Any]:
    if manifest is None:
        report.fail("manifest.exists", "manifest.json is required.", manifest_path)
        return {}
    report.pass_check("manifest.exists", "manifest.json exists and parses.", manifest_path)
    if manifest.get("schema_version") != "project_manifest_v2":
        report.fail(
            "manifest.schema",
            "manifest.json must use compact project_manifest_v2; migrate legacy formal directories first.",
            manifest_path,
        )
    else:
        report.pass_check("manifest.schema", "Project manifest uses compact project_manifest_v2.", manifest_path)
    validate_json_project_relative_references(
        report,
        manifest,
        manifest_path,
        "manifest.project_relative_references",
        "manifest",
    )

    project_config_file = str(manifest.get("project_config_file") or "project_config.json")
    check_rel_exists(report, root, "manifest.project_config_file", project_config_file, "manifest.project_config_file")

    rule_store_contract = manifest.get("canonical_rule_store")
    if not isinstance(rule_store_contract, dict):
        report.fail(
            "manifest.canonical_rule_store",
            "manifest.json must declare canonical_rule_store v2; canonical_rule_file is no longer supported.",
            manifest_path,
        )
    else:
        for field in ("store", "activation_index", "definitions_root"):
            check_rel_exists(
                report,
                root,
                f"manifest.canonical_rule_store.{field}",
                str(rule_store_contract.get(field) or ""),
                f"manifest.canonical_rule_store.{field}",
            )

    repository_contract = manifest.get("formal_asset_repository")
    if not isinstance(repository_contract, dict):
        report.fail(
            "manifest.formal_asset_repository",
            "manifest.json must declare formal_asset_repository.",
            manifest_path,
        )
    else:
        expected_index = "formal_assets/index.json"
        if repository_contract.get("index") != expected_index:
            report.fail(
                "manifest.formal_asset_repository.index",
                f"formal_asset_repository.index must be `{expected_index}`.",
                manifest_path,
            )
        else:
            check_rel_exists(
                report,
                root,
                "manifest.formal_asset_repository.index",
                expected_index,
                "manifest.formal_asset_repository.index",
            )
    packages = manifest.get("packages")
    if not isinstance(packages, list):
        report.fail("manifest.packages_shape", "manifest.packages must be an array.", manifest_path)
    else:
        report.pass_check("manifest.packages_shape", f"manifest projects {len(packages)} Package records.", manifest_path)
    return manifest


def validate_project_index(report: HealthReport, root: Path) -> None:
    index_path = root / "index.md"
    if not index_path.is_file():
        report.fail("project_index.exists", "Project formal asset index is missing.", index_path)
        return
    expected = project_manifest_fingerprint(root)
    actual = project_index_manifest_fingerprint(root)
    if actual != expected:
        report.fail(
            "project_index.manifest_fingerprint",
            "Project formal asset index is stale; rebuild it from the current manifest.",
            index_path,
        )
        return
    report.pass_check(
        "project_index.manifest_fingerprint",
        "Project formal asset index matches the current manifest.",
        index_path,
    )


def validate_formal_asset_repository(
    report: HealthReport,
    root: Path,
    manifest: dict[str, Any],
) -> None:
    if manifest.get("schema_version") != "project_manifest_v2":
        return
    manifest_path = root / "manifest.json"
    try:
        indexed_packages = list_formal_asset_packages(root)
    except FormalAssetRepositoryError as exc:
        report.fail("formal_assets.index", str(exc), root / "formal_assets" / "index.json")
        return
    projected_packages = manifest.get("packages") if isinstance(manifest.get("packages"), list) else []
    if projected_packages != indexed_packages:
        report.fail(
            "formal_assets.project_projection",
            "manifest.packages is stale or differs from formal_assets/index.json.",
            manifest_path,
        )
    else:
        report.pass_check(
            "formal_assets.project_projection",
            f"Project manifest projects all {len(indexed_packages)} Formal Asset Packages.",
            manifest_path,
        )
    repository_contract = manifest.get("formal_asset_repository") if isinstance(manifest.get("formal_asset_repository"), dict) else {}
    if repository_contract.get("package_count") != len(indexed_packages):
        report.fail(
            "formal_assets.package_count",
            "formal_asset_repository.package_count does not match the repository index.",
            manifest_path,
        )
    else:
        report.pass_check(
            "formal_assets.package_count",
            f"Formal Asset Package count is {len(indexed_packages)}.",
            manifest_path,
        )

    legacy_directories = [name for name in ("query_sql", "dashboard_sql", "validations", "runs", "archive") if (root / name).exists()]
    if legacy_directories:
        report.fail(
            "formal_assets.legacy_directories",
            "project_manifest_v2 cannot coexist with legacy formal directories: "
            + ", ".join(legacy_directories),
            root,
        )
    else:
        report.pass_check(
            "formal_assets.legacy_directories",
            "No legacy query_sql/dashboard_sql/validations/runs/archive directories remain.",
            root,
        )

    for entry in indexed_packages:
        package_id = str(entry.get("package_id") or "")
        try:
            package = load_formal_asset_package(root, package_id)
        except FormalAssetRepositoryError as exc:
            report.fail("formal_assets.package", str(exc), root / str(entry.get("manifest_path") or ""))
            continue
        unsafe_members = [
            str(member.get("path") or "")
            for member in package.get("members", [])
            if not str(member.get("path") or "").startswith("formal_assets/")
            or "/query_workspace/" in f"/{str(member.get('path') or '')}"
        ]
        if unsafe_members:
            report.fail(
                "formal_assets.member_paths",
                f"{package_id} has member paths outside formal_assets: {', '.join(unsafe_members[:3])}",
                root / str(package.get("directory") or ""),
            )
        latest_receipt = str(package.get("latest_receipt") or "")
        if not latest_receipt:
            report.fail(
                "formal_assets.latest_receipt",
                f"{package_id} has no latest Package receipt.",
                root / str(package.get("directory") or ""),
            )
            continue
        validation = validate_formal_asset_receipt(root, latest_receipt)
        if validation.get("status") != "valid":
            report.fail(
                "formal_assets.latest_receipt",
                f"{package_id} receipt is invalid: {'; '.join(validation.get('problems') or [])}",
                root / latest_receipt,
            )
        else:
            report.pass_check(
                "formal_assets.latest_receipt",
                f"{package_id} latest receipt validates {validation.get('checked_file_count', 0)} paths.",
                root / latest_receipt,
            )


def validate_artifacts(
    report: HealthReport,
    root: Path,
    manifest: dict[str, Any],
    config: dict[str, Any],
    registered_keys: set[str],
    strict: bool,
    scope: str = "full",
) -> None:
    all_artifacts = [item for item in manifest.get("artifacts", []) if isinstance(item, dict)]
    artifacts = artifacts_for_scope(all_artifacts, scope)
    current_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in artifacts:
        if is_current_artifact(item):
            current_by_key.setdefault((str(item.get("kind") or ""), str(item.get("slug") or "")), []).append(item)

    duplicate_current = {
        key: items for key, items in current_by_key.items() if len(items) > 1
    }
    if duplicate_current:
        for (kind, slug), items in duplicate_current.items():
            paths = ", ".join(str(item.get("path") or "") for item in items)
            report.fail(
                "artifact.current_singleton",
                f"{kind}/{slug} has multiple current artifacts: {paths}",
                root,
            )
    else:
        report.pass_check("artifact.current_singleton", "Every kind+slug has at most one current artifact.", root / "manifest.json")

    for item in artifacts:
        validate_one_artifact(report, root, item, manifest, config, registered_keys, strict)
    validate_orphan_artifact_files(report, root, all_artifacts)
    validate_query_workspace(report, root, manifest, scope=scope)
    scoped_manifest = {**manifest, "artifacts": artifacts}
    validate_formal_query_origins(report, root, scoped_manifest)
    validate_unmanaged_sql_work(report, root)


def validate_orphan_artifact_files(report: HealthReport, root: Path, artifacts: list[dict[str, Any]]) -> None:
    registered_sql_paths = {
        normalize_rel(str(item.get("path") or ""))
        for item in artifacts
        if isinstance(item, dict) and item.get("path")
    }
    registered_meta_paths = {
        normalize_rel(str(Path(path).with_name(Path(path).stem + ".meta.json")))
        for path in registered_sql_paths
    }
    registered_spec_paths = {
        normalize_rel(str(item.get("spec_path") or Path(str(item.get("path") or "")).with_name(Path(str(item.get("path") or "")).stem + ".spec.json")))
        for item in artifacts
        if isinstance(item, dict) and item.get("path")
    }

    discovered_sql_paths: set[str] = set()
    discovered_meta_paths: set[str] = set()
    discovered_spec_paths: set[str] = set()
    for dirname in ARTIFACT_SCAN_DIRS:
        directory = root / dirname
        if not directory.exists():
            continue
        for sql_path in directory.rglob("*.sql"):
            discovered_sql_paths.add(normalize_rel(sql_path.relative_to(root)))
        for meta_path in directory.rglob("*.meta.json"):
            discovered_meta_paths.add(normalize_rel(meta_path.relative_to(root)))
        for spec_path in directory.rglob("*.spec.json"):
            discovered_spec_paths.add(normalize_rel(spec_path.relative_to(root)))

    orphan_sql = sorted(discovered_sql_paths - registered_sql_paths)
    orphan_meta = sorted(discovered_meta_paths - registered_meta_paths)
    orphan_spec = sorted(discovered_spec_paths - registered_spec_paths)
    dangling_meta = sorted(
        meta_path
        for meta_path in discovered_meta_paths
        if normalize_rel(sql_path_from_meta_path(root / meta_path).relative_to(root)) not in discovered_sql_paths
    )
    missing_meta = sorted(registered_meta_paths - discovered_meta_paths)
    missing_spec = sorted(registered_spec_paths - discovered_spec_paths)

    if orphan_sql:
        for path in orphan_sql:
            report.warn(
                "artifact.orphan_sql_file",
                "SQL file exists in an artifact directory but is not registered in manifest.artifacts.",
                resolve_project_path(root, path),
            )
    else:
        report.pass_check(
            "artifact.orphan_sql_file",
            "No unregistered SQL files found in formal artifact directories.",
            root,
        )

    if orphan_meta:
        for path in orphan_meta:
            report.warn(
                "artifact.orphan_meta_file",
                "Metadata file exists in an artifact directory but is not registered through manifest.artifacts.",
                resolve_project_path(root, path),
            )
    else:
        report.pass_check(
            "artifact.orphan_meta_file",
            "No unregistered metadata files found in formal artifact directories.",
            root,
        )

    if orphan_spec:
        for path in orphan_spec:
            report.warn(
                "artifact.orphan_spec_file",
                "Spec sidecar exists in an artifact directory but is not registered through manifest.artifacts.",
                resolve_project_path(root, path),
            )
    else:
        report.pass_check(
            "artifact.orphan_spec_file",
            "No unregistered spec sidecars found in formal artifact directories.",
            root,
        )

    for path in dangling_meta:
        report.warn(
            "artifact.meta_without_sql",
            "Metadata file exists without a sibling SQL file.",
            resolve_project_path(root, path),
        )

    # This summarizes the same condition already checked per artifact and keeps the reverse scan explicit.
    if missing_meta:
        for path in missing_meta:
            report.fail(
                "artifact.registered_meta_missing",
                "Manifest-registered SQL is missing its expected metadata file.",
                resolve_project_path(root, path),
            )
    elif registered_sql_paths:
        report.pass_check(
            "artifact.registered_meta_missing",
            "Every manifest-registered SQL has its expected metadata file.",
            root,
        )

    if missing_spec:
        for path in missing_spec:
            report.fail(
                "artifact.registered_spec_missing",
                "Manifest-registered SQL is missing its expected sidecar spec file.",
                resolve_project_path(root, path),
            )
    elif registered_sql_paths:
        report.pass_check(
            "artifact.registered_spec_missing",
            "Every manifest-registered SQL has its expected sidecar spec file.",
            root,
        )


def validate_query_workspace(
    report: HealthReport,
    root: Path,
    manifest: dict[str, Any],
    *,
    scope: str = "full",
) -> None:
    expected_ref = QUERY_WORKSPACE_INDEX_REL.as_posix()
    expected_view_ref = QUERY_WORKSPACE_HTML_REL.as_posix()
    if manifest.get("schema_version") == "project_manifest_v2":
        report.pass_check(
            "query_workspace.manifest_pointer",
            "Compact project_manifest_v2 keeps Workspace local and outside formal Package projection.",
            root / "manifest.json",
        )
    else:
        index_ref = str(manifest.get("query_workspace_index") or "")
        view_ref = str(manifest.get("query_workspace_view") or "")
        if index_ref != expected_ref or view_ref != expected_view_ref:
            report.fail(
                "query_workspace.manifest_pointer",
                "Legacy manifest query workspace pointers are invalid; migrate to project_manifest_v2.",
                root / "manifest.json",
            )

    index_path = root / QUERY_WORKSPACE_INDEX_REL
    if not index_path.is_file():
        report.pass_check(
            "query_workspace.local_state",
            "Project-local query workspace is not initialized in this checkout; the first QUERY save will create it.",
            index_path,
        )
        return

    view_path = root / QUERY_WORKSPACE_HTML_REL
    if not view_path.exists():
        report.fail("query_workspace.view", f"SQL workspace viewer is missing: {expected_view_ref}", view_path)
    else:
        view_text, view_error = read_text_file(view_path)
        viewer_text = str(view_text or "")
        if view_error or not any(
            marker in viewer_text
            for marker in ["query_workspace_view_v4", "query_workspace_view_v3", "query_workspace_view_v2", "query_workspace_view_v1"]
        ):
            report.fail("query_workspace.view", view_error or "SQL workspace viewer contract marker is missing.", view_path)
        elif "query_workspace_view_v4" not in viewer_text:
            report.warn(
                "query_workspace.view",
                "Legacy workspace viewer detected; the next workspace write or maintenance apply will replace it with the current dynamic shell.",
                view_path,
            )
        else:
            report.pass_check(
                "query_workspace.view",
                "SQL workspace viewer exists and is linked from the manifest.",
                view_path,
            )

    try:
        index = load_query_workspace_index(root)
        error = ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        index = None
        error = str(exc)
    if error or index is None:
        report.fail("query_workspace.index", error or "Query workspace index is missing.", index_path)
        return
    workspace_schema = index.get("schema_version")
    if workspace_schema not in {
        QUERY_WORKSPACE_INDEX_SCHEMA_VERSION,
        LEGACY_QUERY_WORKSPACE_INDEX_SCHEMA_VERSION,
    }:
        report.fail(
            "query_workspace.schema",
            f"query workspace schema must be {QUERY_WORKSPACE_INDEX_SCHEMA_VERSION}.",
            index_path,
        )
        return
    if workspace_schema == LEGACY_QUERY_WORKSPACE_INDEX_SCHEMA_VERSION:
        report.warn(
            "query_workspace.schema",
            "Legacy query workspace index is readable; the next workspace write or maintenance apply will upgrade it to the current schema.",
            index_path,
        )
    else:
        report.pass_check(
            "query_workspace.schema",
            f"Query workspace index uses {QUERY_WORKSPACE_INDEX_SCHEMA_VERSION}.",
            index_path,
        )

    path_problems = validate_query_workspace_paths(index)
    absolute_ref_failures = validate_json_project_relative_references(
        report,
        index,
        index_path,
        "query_workspace.absolute_references",
        "query workspace index",
    )
    if path_problems:
        for problem in path_problems:
            report.fail("query_workspace.relative_paths", problem, index_path)
    elif absolute_ref_failures == 0:
        report.pass_check("query_workspace.relative_paths", "Query workspace index stores project-relative paths only.", index_path)

    entries = index.get("entries")
    if not isinstance(entries, list):
        report.fail("query_workspace.entries_shape", "query workspace entries must be an array.", index_path)
        return

    all_query_ids = {
        str(item.get("query_id") or "")
        for item in entries
        if isinstance(item, dict) and str(item.get("query_id") or "")
    }
    organization_path = root / "query_workspace" / "organization.json"
    if organization_path.exists():
        organization, organization_error = read_json_file(organization_path)
        if organization_error or organization is None:
            report.fail(
                "query_workspace.organization",
                organization_error or "Query workspace organization overlay is invalid.",
                organization_path,
            )
        elif organization.get("schema_version") != "query_workspace_organization_v1":
            report.fail(
                "query_workspace.organization",
                "Query workspace organization schema must be query_workspace_organization_v1.",
                organization_path,
            )
        elif not isinstance(organization.get("entries"), dict):
            report.fail(
                "query_workspace.organization",
                "Query workspace organization entries must be an object keyed by query_id.",
                organization_path,
            )
        else:
            unknown_organization_ids = sorted(
                set(organization.get("entries", {})) - all_query_ids
            )
            if unknown_organization_ids:
                report.fail(
                    "query_workspace.organization",
                    "Organization overlay references unknown query ids: "
                    + ", ".join(unknown_organization_ids),
                    organization_path,
                )
            else:
                report.pass_check(
                    "query_workspace.organization",
                    "Semantic organization is a separate overlay over known query ids.",
                    organization_path,
                )
    indexed_versions = {
        (str(entry.get("query_id") or ""), int(version.get("version") or 0)): str(version.get("path") or "")
        for entry in entries
        if isinstance(entry, dict)
        for version in (entry.get("versions") if isinstance(entry.get("versions"), list) else [])
        if isinstance(version, dict)
    }
    indexed_version_fingerprints = {
        (str(entry.get("query_id") or ""), int(version.get("version") or 0)): str(version.get("sql_fingerprint") or "")
        for entry in entries
        if isinstance(entry, dict)
        for version in (entry.get("versions") if isinstance(entry.get("versions"), list) else [])
        if isinstance(version, dict)
    }
    indexed_workspace_outputs = {
        (
            str(entry.get("query_id") or ""),
            int(version.get("version") or 0),
            str(output.get("attachment_id") or ""),
        ): (entry, version, output)
        for entry in entries
        if isinstance(entry, dict)
        for version in (entry.get("versions") if isinstance(entry.get("versions"), list) else [])
        if isinstance(version, dict)
        for output in (version.get("derived_outputs") if isinstance(version.get("derived_outputs"), list) else [])
        if isinstance(output, dict)
    }

    formal_paths: set[str] = set()
    if manifest.get("schema_version") == "project_manifest_v2":
        for entry in manifest.get("packages", []) if isinstance(manifest.get("packages"), list) else []:
            if not isinstance(entry, dict) or not entry.get("package_id"):
                continue
            try:
                package = load_formal_asset_package(root, str(entry["package_id"]))
            except FormalAssetRepositoryError:
                continue
            formal_paths.update(
                normalize_rel(str(member.get("path") or ""))
                for member in package.get("members", [])
                if isinstance(member, dict) and member.get("role") == "query_sql" and member.get("path")
            )
    else:
        formal_paths = {
            normalize_rel(str(item.get("path") or ""))
            for item in manifest.get("artifacts", [])
            if isinstance(item, dict) and item.get("path")
        }
    query_ids: set[str] = set()
    fingerprints: dict[str, str] = {}
    warned_multi_result_attachments: set[str] = set()
    failures = len(path_problems) + absolute_ref_failures
    checked_versions = 0
    for entry in entries:
        if not isinstance(entry, dict):
            failures += 1
            report.fail("query_workspace.entry_shape", "Each query workspace entry must be an object.", index_path)
            continue
        query_id = str(entry.get("query_id") or "")
        if not query_id or query_id in query_ids:
            failures += 1
            report.fail("query_workspace.query_id", f"Missing or duplicate query_id: {query_id or '(empty)'}", index_path)
        query_ids.add(query_id)
        if not is_nonempty_text(entry.get("purpose"), 6):
            failures += 1
            report.fail("query_workspace.purpose", f"{query_id} needs a concise searchable purpose.", index_path)
        if entry.get("status") not in QUERY_STATUSES:
            failures += 1
            report.fail("query_workspace.status", f"{query_id} has invalid status `{entry.get('status')}`.", index_path)
        versions = entry.get("versions")
        if not isinstance(versions, list) or not versions:
            failures += 1
            report.fail("query_workspace.versions", f"{query_id} must have at least one version.", index_path)
            continue
        actual_output_count = sum(
            len(item.get("derived_outputs", []))
            for item in versions
            if isinstance(item, dict) and isinstance(item.get("derived_outputs"), list)
        )
        if int(entry.get("derived_output_count") or 0) != actual_output_count:
            failures += 1
            report.fail(
                "query_workspace.derived_output_count",
                f"{query_id} derived_output_count does not match its indexed versions.",
                index_path,
            )
        current_version = int(entry.get("current_version") or 0)
        current_rows = [item for item in versions if isinstance(item, dict) and int(item.get("version") or 0) == current_version]
        if len(current_rows) != 1 or str(current_rows[0].get("path") or "") != str(entry.get("current_path") or ""):
            failures += 1
            report.fail("query_workspace.current_version", f"{query_id} current version/path does not resolve to exactly one indexed version.", index_path)
        elif any(
            entry.get(key) != current_rows[0].get(key)
            for key in ("change_type", "coverage_relation")
        ):
            failures += 1
            report.fail(
                "query_workspace.current_contract",
                f"{query_id} family-level change contract must mirror its unique current version.",
                index_path,
            )
        family_branch = entry.get("branch_of") if isinstance(entry.get("branch_of"), dict) else None
        if family_branch is None:
            failures += 1
            report.fail("query_workspace.family_branch", f"{query_id} branch_of must be an object.", index_path)
        elif family_branch:
            branch_id = str(family_branch.get("query_id") or "")
            branch_version = int(family_branch.get("version") or 0)
            branch_path = str(family_branch.get("path") or "")
            if indexed_versions.get((branch_id, branch_version), "") != branch_path:
                failures += 1
                report.fail(
                    "query_workspace.family_branch",
                    f"{query_id} family branch source does not resolve to one indexed query version.",
                    index_path,
                )
        versions_to_validate = workspace_versions_for_scope(versions, current_version, scope)
        for version in versions_to_validate:
            if not isinstance(version, dict):
                failures += 1
                report.fail("query_workspace.version_shape", f"{query_id} contains a non-object version row.", index_path)
                continue
            checked_versions += 1
            label = f"{query_id} v{int(version.get('version') or 0):03d}"
            rel_sql = str(version.get("path") or "")
            rel_meta = str(version.get("meta_path") or "")
            rel_seed = str(version.get("formalize_seed_path") or "")
            change_type = str(version.get("change_type") or "")
            coverage_relation = str(version.get("coverage_relation") or "")
            change_summary = str(version.get("change_summary") or "").strip()
            branch_ref = version.get("branch_of") if isinstance(version.get("branch_of"), dict) else None
            if change_type not in QUERY_CHANGE_TYPES:
                failures += 1
                report.fail("query_workspace.change_type", f"{label} has invalid change_type `{change_type or '(empty)'}`.", index_path)
            if coverage_relation not in COVERAGE_RELATIONS:
                failures += 1
                report.fail(
                    "query_workspace.coverage_relation",
                    f"{label} has invalid coverage_relation `{coverage_relation or '(empty)'}`.",
                    index_path,
                )
            elif change_type in CHANGE_COVERAGE_MATRIX and coverage_relation not in CHANGE_COVERAGE_MATRIX[change_type]:
                failures += 1
                report.fail(
                    "query_workspace.change_contract",
                    f"{label} change_type={change_type} cannot use coverage_relation={coverage_relation}.",
                    index_path,
                )
            if len(change_summary) < 6:
                failures += 1
                report.fail("query_workspace.change_summary", f"{label} needs a concise change summary.", index_path)
            if branch_ref is None:
                failures += 1
                report.fail("query_workspace.branch_of", f"{label} branch_of must be an object, empty when not a branch.", index_path)
            elif change_type == "branch":
                branch_id = str(branch_ref.get("query_id") or "")
                branch_version = int(branch_ref.get("version") or 0)
                branch_path = str(branch_ref.get("path") or "")
                expected_branch_path = indexed_versions.get((branch_id, branch_version), "")
                if branch_id not in all_query_ids or not expected_branch_path or branch_path != expected_branch_path:
                    failures += 1
                    report.fail(
                        "query_workspace.branch_of",
                        f"{label} branch source does not resolve to one indexed query version.",
                        index_path,
                    )
            elif branch_ref:
                failures += 1
                report.fail(
                    "query_workspace.branch_of",
                    f"{label} stores branch_of but change_type is `{change_type}`.",
                    index_path,
                )
            if normalize_rel(rel_sql) in formal_paths:
                failures += 1
                report.fail("query_workspace.formal_boundary", f"{label} is also registered as a formal manifest artifact.", index_path)
            try:
                sql_path = resolve_query_workspace_path(root, rel_sql)
                meta_path = resolve_query_workspace_path(root, rel_meta)
            except ValueError as exc:
                failures += 1
                report.fail("query_workspace.path_boundary", f"{label} has an invalid project path: {exc}", index_path)
                continue
            if not sql_path.exists():
                failures += 1
                report.fail("query_workspace.sql_exists", f"{label} SQL file is missing: {rel_sql}", sql_path)
                continue
            if not meta_path.exists():
                failures += 1
                report.fail("query_workspace.meta_exists", f"{label} metadata file is missing: {rel_meta}", meta_path)
                continue
            sql_text, sql_error = read_text_file(sql_path)
            if sql_error or sql_text is None:
                failures += 1
                report.fail("query_workspace.sql_read", sql_error or f"Could not read {label}.", sql_path)
            else:
                actual_fingerprint = query_workspace_sql_fingerprint(sql_text)
                expected_fingerprint = str(version.get("sql_fingerprint") or "")
                if actual_fingerprint != expected_fingerprint:
                    failures += 1
                    report.fail("query_workspace.fingerprint", f"{label} SQL fingerprint does not match the index.", sql_path)
                prior = fingerprints.get(actual_fingerprint)
                if prior:
                    failures += 1
                    report.fail("query_workspace.duplicate_sql", f"{label} duplicates SQL fingerprint already stored by {prior}.", sql_path)
                else:
                    fingerprints[actual_fingerprint] = label
                privacy_transforms = sql_side_privacy_transforms(sql_text)
                if privacy_transforms:
                    functions = ", ".join(sorted({item["function"] for item in privacy_transforms}))
                    message = (
                        f"{label} performs SQL-side de-identification ({functions}); "
                        "remove it and let DA handle privacy."
                    )
                    if version.get("delivery_ready"):
                        failures += 1
                        report.fail("query_workspace.sql_side_privacy_transform", message, sql_path)
                    else:
                        report.warn("query_workspace.sql_side_privacy_transform", message, sql_path)
            knowledge_references = version.get("knowledge_references", [])
            if not isinstance(knowledge_references, list):
                failures += 1
                report.fail(
                    "query_workspace.knowledge_references",
                    f"{label} knowledge_references must be an array.",
                    index_path,
                )
                knowledge_references = []
            for reference in knowledge_references:
                if not isinstance(reference, dict):
                    failures += 1
                    report.fail(
                        "query_workspace.knowledge_references",
                        f"{label} contains a non-object knowledge reference.",
                        index_path,
                    )
                    continue
                for problem in validate_knowledge_reference(
                    root,
                    reference,
                    require_current_binding=False,
                ):
                    failures += 1
                    report.fail(
                        "query_workspace.knowledge_reference_integrity",
                        f"{label}: {problem}",
                        index_path,
                    )
            meta, meta_error = read_json_file(meta_path)
            if meta_error or meta is None:
                failures += 1
                report.fail("query_workspace.meta_parse", meta_error or f"Could not parse {label} metadata.", meta_path)
            else:
                if meta.get("schema_version") != QUERY_WORKSPACE_META_SCHEMA_VERSION:
                    failures += 1
                    report.fail("query_workspace.meta_schema", f"{label} metadata schema is invalid.", meta_path)
                if meta.get("query_id") != query_id or int(meta.get("version") or 0) != int(version.get("version") or 0):
                    failures += 1
                    report.fail("query_workspace.meta_identity", f"{label} metadata identity does not match the index.", meta_path)
                if any(
                    meta.get(key) != version.get(key)
                    for key in (
                        "change_type",
                        "coverage_relation",
                        "change_summary",
                        "branch_of",
                        "derived_outputs",
                        "knowledge_references",
                        "knowledge_usage",
                        "summary_plan",
                        "analysis_role",
                        "analysis_bundle",
                    )
                ):
                    failures += 1
                    report.fail(
                        "query_workspace.meta_change_contract",
                        f"{label} metadata change contract does not match the index.",
                        meta_path,
                    )
                knowledge_usage = version.get("knowledge_usage")
                provenance = meta.get("generation_provenance") if isinstance(meta.get("generation_provenance"), dict) else {}
                summary_plan = version.get("summary_plan") if isinstance(version.get("summary_plan"), dict) else {}
                analysis_role = str(version.get("analysis_role") or "")
                analysis_bundle = version.get("analysis_bundle") if isinstance(version.get("analysis_bundle"), dict) else {}
                if sql_text:
                    fact_bundle = build_sql_fact_bundle(sql_text, kind="QUERY", root=root)
                    grouped_metric_output = bool(
                        fact_bundle.get("metrics")
                        and fact_bundle.get("dimensions")
                        and (
                            (fact_bundle.get("performance") or {}).get("has_group_by")
                            or (fact_bundle.get("performance") or {}).get("has_aggregate")
                        )
                    )
                    if grouped_metric_output and not summary_plan and _version_tuple(provenance.get("skill_version")) >= (4, 179, 0):
                        failures += 1
                        report.fail(
                            "query_workspace.summary_plan",
                            f"{label} grouped metric output must persist summary_feasibility_v1.",
                            meta_path,
                        )
                    if summary_plan:
                        plan_problems = validate_summary_plan(
                            sql_text,
                            summary_plan,
                            role=analysis_role or ("grouped" if grouped_metric_output else "standalone"),
                            root=root,
                        )
                        if summary_plan.get("metric_contract_fingerprint") != summary_plan_fingerprint(summary_plan):
                            plan_problems.append("metric_contract_fingerprint is stale")
                        for problem in sorted(set(plan_problems)):
                            failures += 1
                            report.fail(
                                "query_workspace.summary_plan",
                                f"{label}: {problem}",
                                meta_path,
                            )
                        if summary_plan.get("routing") == "grouped_plus_overall":
                            bundle_ref = str(analysis_bundle.get("path") or "")
                            if not bundle_ref:
                                failures += 1
                                report.fail(
                                    "query_workspace.analysis_bundle",
                                    f"{label} requires a grouped/overall analysis bundle.",
                                    meta_path,
                                )
                            else:
                                try:
                                    bundle_path = resolve_query_workspace_path(root, bundle_ref)
                                    bundle, bundle_error = read_json_file(bundle_path)
                                except ValueError as exc:
                                    bundle, bundle_error = None, str(exc)
                                member = next(
                                    (
                                        item
                                        for item in (bundle or {}).get("members", [])
                                        if isinstance(item, dict)
                                        and item.get("query_id") == query_id
                                        and int(item.get("version") or 0) == int(version.get("version") or 0)
                                    ),
                                    None,
                                )
                                if (
                                    bundle_error
                                    or not bundle
                                    or bundle.get("schema_version") != "query_analysis_bundle_v1"
                                    or bundle.get("bundle_id") != analysis_bundle.get("bundle_id")
                                    or bundle.get("metric_contract_fingerprint") != summary_plan.get("metric_contract_fingerprint")
                                    or not member
                                    or member.get("sql_fingerprint") != version.get("sql_fingerprint")
                                    or member.get("role") != analysis_role
                                ):
                                    failures += 1
                                    report.fail(
                                        "query_workspace.analysis_bundle",
                                        f"{label} analysis bundle reference is missing, stale, or role-inconsistent.",
                                        meta_path,
                                    )
                                elif analysis_role == "grouped":
                                    members_by_role = {
                                        str(item.get("role") or ""): item
                                        for item in bundle.get("members", [])
                                        if isinstance(item, dict)
                                    }
                                    if set(members_by_role) != {"grouped", "overall"}:
                                        failures += 1
                                        report.fail(
                                            "query_workspace.analysis_bundle",
                                            f"{label} analysis bundle must contain exactly grouped and overall members.",
                                            bundle_path,
                                        )
                                    result_bindings = bundle.get("result_bindings") if isinstance(bundle.get("result_bindings"), dict) else {}
                                    for role, binding in result_bindings.items():
                                        expected_member = members_by_role.get(role, {})
                                        key = (
                                            str(binding.get("query_id") or ""),
                                            int(binding.get("version") or 0),
                                            str(binding.get("result_id") or ""),
                                        ) if isinstance(binding, dict) else ("", 0, "")
                                        resolved = indexed_workspace_outputs.get(key)
                                        if (
                                            not expected_member
                                            or key[:2]
                                            != (
                                                str(expected_member.get("query_id") or ""),
                                                int(expected_member.get("version") or 0),
                                            )
                                            or not resolved
                                            or resolved[2].get("kind") != "result_evidence"
                                            or str(binding.get("path") or "") != str(resolved[2].get("path") or "")
                                        ):
                                            failures += 1
                                            report.fail(
                                                "query_workspace.analysis_bundle_result",
                                                f"{label} bundle result binding `{role}` does not resolve to its exact SQL member.",
                                                bundle_path,
                                            )
                                    if bundle.get("status") in {"ready_for_visualization", "visualized"} and set(result_bindings) != {"grouped", "overall"}:
                                        failures += 1
                                        report.fail(
                                            "query_workspace.analysis_bundle_result",
                                            f"{label} bundle status requires both grouped and overall result bindings.",
                                            bundle_path,
                                        )
                                    visualization = bundle.get("visualization") if isinstance(bundle.get("visualization"), dict) else {}
                                    if bundle.get("status") == "visualized":
                                        visual_key = (
                                            str(visualization.get("query_id") or ""),
                                            int(visualization.get("version") or 0),
                                            str(visualization.get("attachment_id") or ""),
                                        )
                                        resolved_visual = indexed_workspace_outputs.get(visual_key)
                                        if (
                                            not resolved_visual
                                            or resolved_visual[2].get("kind")
                                            not in {"visualization", "analysis_workbook", "comparison_workbook"}
                                            or (
                                                visualization.get("kind")
                                                and visualization.get("kind") != resolved_visual[2].get("kind")
                                            )
                                            or resolved_visual[2].get("lineage_status") != "exact_results"
                                            or len(resolved_visual[2].get("source_results") or []) < 2
                                        ):
                                            failures += 1
                                            report.fail(
                                                "query_workspace.analysis_bundle_visualization",
                                                f"{label} visualized bundle does not resolve to one exact_results workbook.",
                                                bundle_path,
                                            )
                if not isinstance(knowledge_usage, dict) or not knowledge_usage:
                    if _version_tuple(provenance.get("skill_version")) >= (4, 156, 0):
                        failures += 1
                        report.fail(
                            "query_workspace.knowledge_usage",
                            f"{label} must declare knowledge_usage for this generated SQL version.",
                            meta_path,
                        )
                    else:
                        report.record_legacy_knowledge_usage(meta_path)
                else:
                    # A later project binding must not invalidate an already saved SQL version.
                    usage_problems = validate_knowledge_usage(
                        root,
                        knowledge_usage,
                        knowledge_references,
                        require_current_binding=False,
                    )
                    for problem in usage_problems:
                        failures += 1
                        report.fail(
                            "query_workspace.knowledge_usage",
                            f"{label}: {problem}",
                            meta_path,
                        )
                for problem in validate_query_workspace_paths(meta):
                    failures += 1
                    report.fail("query_workspace.meta_relative_paths", problem, meta_path)
                failures += validate_json_project_relative_references(
                    report,
                    meta,
                    meta_path,
                    "query_workspace.meta_absolute_references",
                    f"{label} metadata",
                )
            derived_outputs = version.get("derived_outputs")
            if not isinstance(derived_outputs, list):
                failures += 1
                report.fail(
                    "query_workspace.derived_outputs",
                    f"{label} derived_outputs must be an array.",
                    index_path,
                )
                derived_outputs = []
            seen_output_hashes: set[str] = set()
            seen_attachment_ids: set[str] = set()
            result_attachment_ids = {
                str(item.get("attachment_id") or "")
                for item in derived_outputs
                if isinstance(item, dict) and item.get("kind") == "result_evidence"
            }
            for output in derived_outputs:
                if not isinstance(output, dict):
                    failures += 1
                    report.fail("query_workspace.derived_output", f"{label} contains a malformed derived output.", index_path)
                    continue
                attachment_id = str(output.get("attachment_id") or "")
                output_hash = str(output.get("sha256") or "")
                output_path_ref = str(output.get("path") or "")
                if not attachment_id or attachment_id in seen_attachment_ids or output_hash in seen_output_hashes:
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_identity",
                        f"{label} has a missing or duplicate derived-output id/hash.",
                        index_path,
                    )
                seen_attachment_ids.add(attachment_id)
                seen_output_hashes.add(output_hash)
                try:
                    output_path = resolve_query_workspace_path(root, output_path_ref)
                except ValueError as exc:
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_path",
                        f"{label} derived output path is invalid: {exc}",
                        index_path,
                    )
                    continue
                if not output_path.exists():
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_exists",
                        f"{label} derived output is missing: {output_path_ref}",
                        output_path,
                    )
                elif query_workspace_file_sha256(output_path) != output_hash:
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_fingerprint",
                        f"{label} derived output hash does not match its index record.",
                        output_path,
                    )
                retention = output.get("retention")
                if not isinstance(retention, dict):
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_retention",
                        f"{label} derived output `{attachment_id}` has no retention contract.",
                        index_path,
                    )
                else:
                    actual_size = output_path.stat().st_size if output_path.exists() else 0
                    if retention.get("stored_sha256") != output_hash or int(retention.get("stored_size_bytes") or -1) != actual_size:
                        failures += 1
                        report.fail(
                            "query_workspace.derived_output_retention_fingerprint",
                            f"{label} derived output `{attachment_id}` retention size/hash does not match the stored file.",
                            output_path,
                        )
                    if output.get("source_sha256") != retention.get("source_sha256"):
                        failures += 1
                        report.fail(
                            "query_workspace.derived_output_source_fingerprint",
                            f"{label} derived output `{attachment_id}` source SHA-256 is inconsistent.",
                            index_path,
                        )
                    if output.get("kind") == "result_evidence":
                        if actual_size > RESULT_EVIDENCE_MAX_BYTES:
                            failures += 1
                            report.fail(
                                "query_workspace.result_evidence_size",
                                f"{label} result evidence exceeds the 10 MB managed-asset limit and must be sliced.",
                                output_path,
                            )
                        if retention.get("payload_role") != "sql_output_preview":
                            failures += 1
                            report.fail(
                                "query_workspace.result_evidence_role",
                                f"{label} result evidence must be retained as a SQL output preview.",
                                index_path,
                            )
                    elif retention.get("policy") != "full_reusable_output" or retention.get("is_sliced"):
                        failures += 1
                        report.fail(
                            "query_workspace.reusable_output_retention",
                            f"{label} `{output.get('kind')}` must be preserved in full as a reusable output.",
                            output_path,
                        )
                if str(output.get("source_sql_fingerprint") or "") != str(version.get("sql_fingerprint") or ""):
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_lineage",
                        f"{label} derived output is not bound to this SQL fingerprint.",
                        index_path,
                    )
                source_result_id = str(output.get("source_result_id") or "")
                lineage_status = str(output.get("lineage_status") or "")
                asset_state = str(output.get("asset_state") or "active")
                output_bundle_ref = (
                    output.get("analysis_bundle")
                    if isinstance(output.get("analysis_bundle"), dict)
                    else {}
                )
                if output_bundle_ref:
                    bundle_ref = str(output_bundle_ref.get("path") or "")
                    try:
                        bundle_path = resolve_query_workspace_path(root, bundle_ref)
                        output_bundle, output_bundle_error = read_json_file(bundle_path)
                    except ValueError as exc:
                        output_bundle, output_bundle_error = None, str(exc)
                    bundle_visualization = (
                        output_bundle.get("visualization")
                        if isinstance((output_bundle or {}).get("visualization"), dict)
                        else {}
                    )
                    if (
                        output_bundle_error
                        or not output_bundle
                        or output_bundle.get("schema_version") != "query_analysis_bundle_v1"
                        or output_bundle.get("bundle_id") != output_bundle_ref.get("bundle_id")
                        or output_bundle.get("metric_contract_fingerprint")
                        != output_bundle_ref.get("metric_contract_fingerprint")
                        or bundle_visualization.get("attachment_id") != attachment_id
                        or bundle_visualization.get("query_id") != query_id
                        or int(bundle_visualization.get("version") or 0)
                        != int(version.get("version") or 0)
                    ):
                        failures += 1
                        report.fail(
                            "query_workspace.derived_output_analysis_bundle",
                            f"{label} derived output `{attachment_id}` has a missing or stale analysis bundle reference.",
                            index_path,
                        )
                if asset_state not in {"active", "superseded", "discarded", "needs_review"}:
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_state",
                        f"{label} derived output `{attachment_id}` has invalid asset_state `{asset_state}`.",
                        index_path,
                    )
                if asset_state == "discarded" and not str(output.get("state_reason") or "").strip():
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_state",
                        f"{label} discarded output `{attachment_id}` must explain why it was discarded.",
                        index_path,
                    )
                superseded_by = output.get("superseded_by") if isinstance(output.get("superseded_by"), list) else []
                if asset_state == "superseded" and not superseded_by:
                    failures += 1
                    report.fail(
                        "query_workspace.derived_output_state",
                        f"{label} superseded output `{attachment_id}` must point to its replacement.",
                        index_path,
                    )
                for replacement in superseded_by:
                    replacement_path = str(replacement.get("path") or "") if isinstance(replacement, dict) else ""
                    key = (
                        str(replacement.get("query_id") or ""),
                        int(replacement.get("version") or 0),
                        str(replacement.get("attachment_id") or ""),
                    ) if isinstance(replacement, dict) else ("", 0, "")
                    resolved = indexed_workspace_outputs.get(key)
                    if not resolved or str(resolved[2].get("path") or "") != replacement_path:
                        failures += 1
                        report.fail(
                            "query_workspace.derived_output_state",
                            f"{label} replacement reference for `{attachment_id}` does not resolve.",
                            index_path,
                        )
                source_results = output.get("source_results") if isinstance(output.get("source_results"), list) else []
                resolved_results: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
                for reference in source_results:
                    key = (
                        str(reference.get("query_id") or ""),
                        int(reference.get("version") or 0),
                        str(reference.get("result_id") or ""),
                    ) if isinstance(reference, dict) else ("", 0, "")
                    resolved = indexed_workspace_outputs.get(key)
                    if not resolved or resolved[2].get("kind") != "result_evidence":
                        failures += 1
                        report.fail(
                            "query_workspace.result_lineage",
                            f"{label} source result reference for `{attachment_id}` does not resolve.",
                            index_path,
                        )
                        continue
                    _, result_version, result_output = resolved
                    expected = {
                        "sql_path": str(result_version.get("path") or ""),
                        "sql_fingerprint": str(result_version.get("sql_fingerprint") or ""),
                        "result_path": str(result_output.get("path") or ""),
                        "result_sha256": str(result_output.get("source_sha256") or result_output.get("sha256") or ""),
                    }
                    if any(str(reference.get(key) or "") != value for key, value in expected.items()):
                        failures += 1
                        report.fail(
                            "query_workspace.result_lineage",
                            f"{label} source result reference for `{attachment_id}` has stale path or fingerprint evidence.",
                            index_path,
                        )
                    resolved_results.append(resolved)
                if not lineage_status:
                    report.warn(
                        "query_workspace.result_lineage_legacy",
                        f"{label} derived output `{attachment_id}` predates result-level lineage; run sql_result_visualization.py migrate.",
                        index_path,
                    )
                elif output.get("kind") == "result_evidence":
                    if lineage_status != "result_evidence" or source_result_id != attachment_id:
                        failures += 1
                        report.fail(
                            "query_workspace.result_lineage",
                            f"{label} result evidence `{attachment_id}` must identify itself as the source result.",
                            index_path,
                        )
                    if source_results and (
                        len(source_results) != 1
                        or not isinstance(source_results[0], dict)
                        or source_results[0].get("result_id") != attachment_id
                    ):
                        failures += 1
                        report.fail(
                            "query_workspace.result_lineage",
                            f"{label} result evidence `{attachment_id}` must reference itself exactly once.",
                            index_path,
                        )
                elif lineage_status == "exact_result":
                    if source_results:
                        same_version = all(
                            str(ref.get("query_id") or "") == query_id
                            and int(ref.get("version") or 0) == int(version.get("version") or 0)
                            for ref in source_results
                        )
                        if len(source_results) != 1 or not same_version:
                            failures += 1
                            report.fail(
                                "query_workspace.result_lineage",
                                f"{label} exact_result `{attachment_id}` must use one result from this exact SQL version.",
                                index_path,
                            )
                    elif source_result_id not in result_attachment_ids:
                        failures += 1
                        report.fail(
                            "query_workspace.result_lineage",
                            f"{label} reusable output `{attachment_id}` references a result outside this exact SQL version.",
                            index_path,
                        )
                elif lineage_status == "exact_results" and len(source_results) < 2:
                    failures += 1
                    report.fail(
                        "query_workspace.result_lineage",
                        f"{label} exact_results `{attachment_id}` must reference at least two exact result files.",
                        index_path,
                    )
                elif lineage_status == "deterministic_transform":
                    transformation = output.get("transformation") if isinstance(output.get("transformation"), dict) else {}
                    if not source_results or transformation.get("contract_version") != "result_transformation_v1" or transformation.get("user_confirmed") is not True:
                        failures += 1
                        report.fail(
                            "query_workspace.result_lineage",
                            f"{label} deterministic transform `{attachment_id}` needs source results and user-confirmed transform evidence.",
                            index_path,
                        )
                elif lineage_status in {"sql_version_only", "unresolved_legacy"} and asset_state in {"active", "needs_review"}:
                    report.warn(
                        "query_workspace.result_lineage_unresolved",
                        f"{label} reusable output `{attachment_id}` is bound only to the SQL version, not one exact result file.",
                        index_path,
                    )
                related_query_keys: list[tuple[str, int]] = []
                for related in output.get("related_queries", []) if isinstance(output.get("related_queries"), list) else []:
                    if not isinstance(related, dict):
                        failures += 1
                        report.fail(
                            "query_workspace.derived_output_related_query",
                            f"{label} has a malformed related query reference.",
                            index_path,
                        )
                        continue
                    related_key = (str(related.get("query_id") or ""), int(related.get("version") or 0))
                    related_query_keys.append(related_key)
                    if (
                        indexed_versions.get(related_key, "") != str(related.get("path") or "")
                        or indexed_version_fingerprints.get(related_key, "") != str(related.get("sql_fingerprint") or "")
                    ):
                        failures += 1
                        report.fail(
                            "query_workspace.derived_output_related_query",
                            f"{label} related query reference does not resolve to one indexed SQL version.",
                            index_path,
                        )
                related_results_exist = any(
                    result_query_id == related_query_id
                    and result_version_number == related_version_number
                    and result_output.get("kind") == "result_evidence"
                    and str(result_output.get("asset_state") or "active") == "active"
                    for related_query_id, related_version_number in related_query_keys
                    for (result_query_id, result_version_number, _), (_, _, result_output)
                    in indexed_workspace_outputs.items()
                )
                if (
                    output.get("kind") in {"visualization", "analysis_workbook", "comparison_workbook"}
                    and asset_state == "active"
                    and lineage_status == "exact_result"
                    and related_results_exist
                    and attachment_id not in warned_multi_result_attachments
                ):
                    warned_multi_result_attachments.add(attachment_id)
                    report.warn(
                        "query_workspace.multi_result_lineage_candidate",
                        f"{label} reusable output `{attachment_id}` is bound to one result while a related query also has result evidence; confirm whether it needs one grouped/overall analysis bundle.",
                        index_path,
                    )
            if rel_seed:
                try:
                    seed_path = resolve_query_workspace_path(root, rel_seed)
                except ValueError as exc:
                    failures += 1
                    report.fail("query_workspace.seed_path_boundary", f"{label} has an invalid seed path: {exc}", index_path)
                    continue
                seed, seed_error = read_json_file(seed_path)
                if seed_error or seed is None:
                    failures += 1
                    report.fail("query_workspace.seed_parse", seed_error or f"Could not parse {label} seed.", seed_path)
                else:
                    if seed.get("knowledge_usage") != version.get("knowledge_usage"):
                        failures += 1
                        report.fail(
                            "query_workspace.seed_knowledge_usage",
                            f"{label} formalize seed knowledge_usage does not match the index.",
                            seed_path,
                        )
                    if seed.get("summary_plan", {}) != version.get("summary_plan", {}):
                        failures += 1
                        report.fail(
                            "query_workspace.seed_summary_plan",
                            f"{label} formalize seed summary_plan does not match the index.",
                            seed_path,
                        )
                    if seed.get("analysis_bundle", {}) != version.get("analysis_bundle", {}):
                        failures += 1
                        report.fail(
                            "query_workspace.seed_analysis_bundle",
                            f"{label} formalize seed analysis_bundle does not match the index.",
                            seed_path,
                        )
                    failures += validate_json_project_relative_references(
                        report,
                        seed,
                        seed_path,
                        "query_workspace.seed_absolute_references",
                        f"{label} formalize seed",
                    )
            status = str(version.get("status") or "")
            source_intake = version.get("source_intake") if isinstance(version.get("source_intake"), dict) else {}
            if source_intake:
                working_ref = str(source_intake.get("working_copy_path") or "")
                snapshot_ref = str(source_intake.get("source_snapshot_path") or "")
                if source_intake.get("external_input_immutable") is not True or source_intake.get("absolute_source_path_persisted") is not False:
                    failures += 1
                    report.fail(
                        "query_workspace.source_intake_contract",
                        f"{label} source intake must declare immutable external input and no persisted absolute path.",
                        meta_path,
                    )
                for intake_label, intake_ref in [("source snapshot", snapshot_ref), ("working copy", working_ref)]:
                    if not intake_ref:
                        continue
                    try:
                        intake_path = resolve_query_workspace_path(root, intake_ref)
                    except ValueError as exc:
                        failures += 1
                        report.fail("query_workspace.source_intake_path", f"{label} {intake_label} path is invalid: {exc}", meta_path)
                        continue
                    if not intake_path.exists():
                        failures += 1
                        report.fail("query_workspace.source_intake_path", f"{label} {intake_label} is missing: {intake_ref}", intake_path)
                if source_intake.get("contract_version") == "legacy_work_import_v1":
                    if not source_intake.get("legacy_source_path"):
                        failures += 1
                        report.fail(
                            "query_workspace.legacy_source",
                            f"{label} legacy migration is missing legacy_source_path.",
                            meta_path,
                        )
                    if source_intake.get("source_removed_after_verified_copy") is not True:
                        failures += 1
                        report.fail(
                            "query_workspace.legacy_source",
                            f"{label} legacy migration must confirm source removal after a verified copy.",
                            meta_path,
                        )
            legacy_refs = version.get("legacy_source_refs") if isinstance(version.get("legacy_source_refs"), list) else []
            for legacy_ref in legacy_refs:
                if not isinstance(legacy_ref, dict):
                    failures += 1
                    report.fail("query_workspace.legacy_source", f"{label} has a malformed legacy source reference.", meta_path)
                    continue
                if legacy_ref.get("contract_version") != "legacy_work_source_v1" or not legacy_ref.get("legacy_source_path"):
                    failures += 1
                    report.fail("query_workspace.legacy_source", f"{label} legacy source reference is incomplete.", meta_path)
                if legacy_ref.get("source_removed_after_verified_copy") is not True:
                    failures += 1
                    report.fail(
                        "query_workspace.legacy_source",
                        f"{label} legacy source was not cleaned after the indexed copy was verified.",
                        meta_path,
                    )
            gate_status = str((version.get("generation_gate") or {}).get("status") or "")
            delivery_ready = bool(version.get("delivery_ready"))
            if status in {"runnable", "result_confirmed", "promoted"} and (gate_status != "ok" or not delivery_ready):
                failures += 1
                report.fail(
                    "query_workspace.delivery_gate",
                    f"{label} is `{status}` but generation_gate.status is not ok or delivery_ready is false.",
                    meta_path,
                )
            formal_path = str(version.get("formal_artifact_path") or "")
            if status == "promoted" and (not formal_path or normalize_rel(formal_path) not in formal_paths):
                failures += 1
                report.fail("query_workspace.promotion_link", f"{label} is promoted but its formal QUERY path is missing from manifest.", meta_path)
    if failures == 0:
        report.pass_check(
            "query_workspace.index",
            f"Query workspace is consistent: {len(entries)} query families, {checked_versions} versions, no duplicate SQL fingerprints.",
            index_path,
        )


def validate_unmanaged_sql_work(report: HealthReport, root: Path) -> None:
    unmanaged: list[str] = []
    for sql_path in root.rglob("*.sql"):
        try:
            rel = sql_path.relative_to(root)
        except ValueError:
            continue
        lowered = [part.lower() for part in rel.parts]
        if len(lowered) >= 2 and lowered[0] == "query_workspace" and lowered[1] == "_working":
            continue
        if any(part in UNMANAGED_WORK_DIR_NAMES for part in lowered[:-1]):
            unmanaged.append(rel.as_posix())
    if unmanaged:
        examples = ", ".join(f"`{item}`" for item in unmanaged[:8])
        suffix = "" if len(unmanaged) <= 8 else f" and {len(unmanaged) - 8} more"
        report.fail(
            "query_workspace.unmanaged_sql",
            f"{len(unmanaged)} SQL work file(s) bypass the query workspace ({examples}{suffix}). Migrate or save them before reuse.",
            root,
        )
    else:
        report.pass_check(
            "query_workspace.unmanaged_sql",
            "No SQL files remain in unmanaged scratch/work/draft directories.",
            root,
        )


def _version_tuple(value: Any) -> tuple[int, int, int]:
    parts = [int(item) for item in re.findall(r"\d+", str(value or ""))[:3]]
    return tuple((parts + [0, 0, 0])[:3])  # type: ignore[return-value]


def _origin_required_for_artifact(item: dict[str, Any], spec: dict[str, Any]) -> bool:
    provenance = spec.get("generation_provenance") if isinstance(spec.get("generation_provenance"), dict) else {}
    if not provenance and isinstance(item.get("generation_provenance"), dict):
        provenance = item.get("generation_provenance") or {}
    workflow = str(provenance.get("workflow") or "").lower()
    generator = str(provenance.get("generated_by_script") or "").lower()
    if any(token in workflow or token in generator for token in ("historical", "backfill", "migrate")):
        return False
    return _version_tuple(provenance.get("skill_version")) >= (4, 145, 0)


def validate_formal_query_origins(report: HealthReport, root: Path, manifest: dict[str, Any]) -> None:
    """Cross-check formal QUERY lineage against the exact indexed workspace version."""

    index_path = root / QUERY_WORKSPACE_INDEX_REL
    try:
        index = load_query_workspace_index(root)
        index_error = ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        index = None
        index_error = str(exc)
    if index_error or not isinstance(index, dict):
        return
    versions_by_path: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    historical_without_origin: list[str] = []
    for entry in index.get("entries", []) if isinstance(index.get("entries"), list) else []:
        if not isinstance(entry, dict):
            continue
        for version in entry.get("versions", []) if isinstance(entry.get("versions"), list) else []:
            if isinstance(version, dict) and version.get("path"):
                versions_by_path[normalize_rel(str(version.get("path")))] = (entry, version)

    for item in manifest.get("artifacts", []) if isinstance(manifest.get("artifacts"), list) else []:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "QUERY":
            continue
        sql_path = resolve_project_path(root, str(item.get("path") or ""))
        spec, spec_errors = load_sidecar_spec(root, item, sql_path)
        if spec_errors or not isinstance(spec, dict):
            continue
        origin = spec.get("origin_query_workspace") if isinstance(spec.get("origin_query_workspace"), dict) else {}
        if not origin:
            status = "fail" if _origin_required_for_artifact(item, spec) else "warn"
            if status == "fail":
                report.fail(
                    "artifact.query_workspace_origin",
                    f"{artifact_label(item)} is missing origin_query_workspace.",
                    sql_path,
                )
            else:
                historical_without_origin.append(artifact_label(item))
            continue

        required = ["contract_version", "query_id", "version", "path", "meta_path", "source_sql_fingerprint", "purpose"]
        missing = [key for key in required if origin.get(key) in (None, "")]
        if missing:
            report.fail(
                "artifact.query_workspace_origin",
                f"{artifact_label(item)} origin_query_workspace is missing: {', '.join(missing)}.",
                sql_path,
            )
            continue
        rel_source = normalize_rel(str(origin.get("path") or ""))
        try:
            resolve_query_workspace_path(root, rel_source)
            resolve_query_workspace_path(root, str(origin.get("meta_path") or ""))
        except ValueError as exc:
            report.fail("artifact.query_workspace_origin", f"{artifact_label(item)} has an invalid workspace origin path: {exc}", sql_path)
            continue
        matched = versions_by_path.get(rel_source)
        if not matched:
            report.fail(
                "artifact.query_workspace_origin",
                f"{artifact_label(item)} references a workspace SQL version that is not indexed: {rel_source}.",
                sql_path,
            )
            continue
        entry, version = matched
        comparisons = {
            "query_id": entry.get("query_id"),
            "version": version.get("version"),
            "source_sql_fingerprint": version.get("sql_fingerprint"),
            "meta_path": version.get("meta_path"),
        }
        mismatches = [
            key
            for key, actual in comparisons.items()
            if str(origin.get(key) or "") != str(actual or "")
        ]
        formal_path = normalize_rel(str(item.get("path") or ""))
        promoted_links = {
            normalize_rel(str(version.get("formal_artifact_path") or "")),
            *{
                normalize_rel(str(value))
                for value in entry.get("formal_artifacts", []) or []
                if str(value or "").strip()
            },
        }
        if str(version.get("status") or "") != "promoted" or formal_path not in promoted_links:
            mismatches.append("promotion_link")

        meta, _ = read_json_file(expected_meta_path(sql_path))
        for surface_name, surface in [("manifest", item), ("metadata", meta or {})]:
            surface_origin = surface.get("origin_query_workspace") if isinstance(surface.get("origin_query_workspace"), dict) else {}
            if not surface_origin:
                mismatches.append(f"{surface_name}_origin")
                continue
            for key in ["query_id", "version", "path", "source_sql_fingerprint"]:
                if str(surface_origin.get(key) or "") != str(origin.get(key) or ""):
                    mismatches.append(f"{surface_name}.{key}")
        if mismatches:
            report.fail(
                "artifact.query_workspace_origin",
                f"{artifact_label(item)} workspace origin does not match the indexed/promotion state: {', '.join(sorted(set(mismatches)))}.",
                sql_path,
            )
        else:
            report.pass_check(
                "artifact.query_workspace_origin",
                f"{artifact_label(item)} traces to {entry.get('query_id')} v{version.get('version')} and the promotion link is complete.",
                sql_path,
            )
    if historical_without_origin:
        examples = ", ".join(historical_without_origin[:5])
        suffix = "" if len(historical_without_origin) <= 5 else f" and {len(historical_without_origin) - 5} more"
        report.warn(
            "artifact.query_workspace_origin_history",
            f"{len(historical_without_origin)} historical QUERY artifact(s) predate query-workspace lineage ({examples}{suffix}); do not fabricate origins.",
            root / "manifest.json",
        )


def validate_intermediate_tables(report: HealthReport, root: Path, manifest: dict[str, Any], strict: bool) -> None:
    rows = manifest.get("intermediate_tables", [])
    if rows is None:
        report.pass_check("intermediate_tables.index", "No intermediate table registry is present.", root / "manifest.json")
        return
    if not isinstance(rows, list):
        report.fail("intermediate_tables.index_shape", "manifest.intermediate_tables must be an array.", root / "manifest.json")
        return
    if not rows:
        report.pass_check("intermediate_tables.index", "No intermediate tables are registered.", root / "manifest.json")
        return

    current_by_key: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        if isinstance(item, dict) and is_current_table(item):
            for key in [item.get("table_name"), item.get("slug")]:
                normalized = normalize_table_key(key)
                if normalized:
                    current_by_key.setdefault(normalized, []).append(item)
    duplicate_keys = {key: items for key, items in current_by_key.items() if len(items) > 1}
    if duplicate_keys:
        for key, items in duplicate_keys.items():
            paths = ", ".join(str(item.get("path") or "") for item in items)
            report.fail("intermediate_table.current_singleton", f"{key} has multiple current intermediate-table records: {paths}", root / "manifest.json")
    else:
        report.pass_check("intermediate_table.current_singleton", "Every table name/slug has at most one current intermediate-table record.", root / "manifest.json")

    for item in rows:
        if not isinstance(item, dict):
            report.warn("intermediate_table.entry_shape", "Intermediate table entry must be an object.", root / "manifest.json")
            continue
        label = table_label(item)
        rel_path = str(item.get("path") or "")
        if not rel_path:
            report.fail("intermediate_table.path", f"{label} is missing path.", root / "manifest.json")
            continue
        sql_path = resolve_project_path(root, rel_path)
        if sql_path.exists():
            report.pass_check("intermediate_table.sql_exists", f"{label} SQL exists.", sql_path)
        else:
            report.fail("intermediate_table.sql_exists", f"{label} SQL file is missing.", sql_path)
            continue

        meta_path = expected_meta_path(sql_path)
        meta, meta_error = read_json_file(meta_path)
        if meta_error:
            report.fail("intermediate_table.meta_exists", f"{label} metadata JSON is missing or invalid: {meta_error}", meta_path)
            meta = {}
        else:
            report.pass_check("intermediate_table.meta_exists", f"{label} metadata JSON exists and parses.", meta_path)

        if strict and meta:
            for key in ["table_name", "slug", "version", "path"]:
                if meta.get(key) != item.get(key):
                    report.warn("intermediate_table.meta_matches_manifest", f"{label} metadata field `{key}` differs from manifest.", meta_path)

        availability = str(item.get("availability_status") or "unknown")
        source_mode = str(item.get("source_contract_mode") or "dual_path")
        if availability not in ALLOWED_TABLE_AVAILABILITY:
            report.fail("intermediate_table.availability_status", f"{label} has unsupported availability_status: {availability}.", sql_path)
        elif availability == "unknown" and is_current_table(item):
            report.warn("intermediate_table.availability_status", f"{label} availability is unknown; do not treat it as target-verified.", sql_path)
        else:
            report.pass_check("intermediate_table.availability_status", f"{label} availability_status is {availability}.", sql_path)

        if source_mode not in ALLOWED_TABLE_SOURCE_CONTRACT_MODES:
            report.fail("intermediate_table.source_contract_mode", f"{label} has unsupported source_contract_mode: {source_mode}.", sql_path)
        else:
            report.pass_check("intermediate_table.source_contract_mode", f"{label} source_contract_mode is {source_mode}.", sql_path)

        fallback_sources = item.get("fallback_source_tables", []) or []
        has_fallback = bool(item.get("fallback_policy") or fallback_sources or item.get("fallback_sql_reference"))
        if availability == "unavailable" and not has_fallback:
            report.fail("intermediate_table.fallback_contract", f"{label} is unavailable but has no fallback policy/source/sql reference.", sql_path)
        elif source_mode in {"dual_path", "intermediate_preferred"} and is_current_table(item) and not has_fallback:
            report.warn("intermediate_table.fallback_contract", f"{label} allows fallback but has no fallback policy/source/sql reference.", sql_path)
        elif has_fallback:
            report.pass_check("intermediate_table.fallback_contract", f"{label} has a fallback contract.", sql_path)

        if item.get("reusable") is True and not is_nonempty_text(item.get("reuse_notes")):
            report.fail("intermediate_table.reuse_notes", f"{label} is reusable but reuse_notes is empty.", sql_path)
        if availability == "available" and not item.get("validation_artifacts"):
            report.warn("intermediate_table.validation_artifacts", f"{label} is marked available without validation_artifacts.", sql_path)


def validate_artifact_intermediate_table_links(
    report: HealthReport,
    root: Path,
    item: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    refs = item.get("intermediate_tables", []) or []
    if not refs:
        return
    index = current_table_index(manifest)
    label = artifact_label(item)
    sql_path = resolve_project_path(root, str(item.get("path") or ""))
    for ref in refs:
        table = index.get(normalize_table_key(ref))
        if not table:
            report.fail("artifact.intermediate_table_registered", f"{label} references unregistered intermediate table: {ref}.", sql_path)
            continue
        availability = str(table.get("availability_status") or "unknown")
        if availability == "unavailable":
            report.fail("artifact.intermediate_table_available", f"{label} references unavailable intermediate table: {ref}; use fallback raw-log logic or update availability.", sql_path)
        elif availability == "unknown" and is_current_artifact(item):
            report.warn("artifact.intermediate_table_available", f"{label} references {ref} whose availability is unknown.", sql_path)
        else:
            report.pass_check("artifact.intermediate_table_available", f"{label} intermediate table {ref} is registered with availability {availability}.", sql_path)


def validate_one_artifact(
    report: HealthReport,
    root: Path,
    item: dict[str, Any],
    manifest: dict[str, Any],
    config: dict[str, Any],
    registered_keys: set[str],
    strict: bool,
) -> None:
    label = artifact_label(item)
    rel_path = str(item.get("path") or "")
    if not rel_path:
        report.fail("artifact.path", f"{label} is missing path.", root / "manifest.json")
        return

    sql_path = resolve_project_path(root, rel_path)
    if sql_path.exists():
        report.pass_check("artifact.sql_exists", f"{label} SQL exists.", sql_path)
    else:
        report.fail("artifact.sql_exists", f"{label} SQL file is missing.", sql_path)
        return

    meta_path = expected_meta_path(sql_path)
    meta, meta_error = read_json_file(meta_path)
    if meta_error:
        report.fail("artifact.meta_exists", f"{label} metadata JSON is missing or invalid: {meta_error}", meta_path)
        meta = {}
    else:
        report.pass_check("artifact.meta_exists", f"{label} metadata JSON exists and parses.", meta_path)
        validate_json_project_relative_references(
            report,
            meta,
            meta_path,
            "artifact.meta_project_relative_references",
            f"{label} metadata",
        )

    if strict and meta:
        for key in ["kind", "slug", "version", "path"]:
            if meta.get(key) != item.get(key):
                report.warn(
                    "artifact.meta_matches_manifest",
                    f"{label} metadata field `{key}` differs from manifest.",
                    meta_path,
                )

    validate_artifact_state(report, root, item, strict)
    validate_reuse_contract(report, item, sql_path)
    validate_artifact_links(report, root, item)
    validate_artifact_intermediate_table_links(report, root, item, manifest)
    validate_spec_block(report, root, item, sql_path, meta, strict, config)

    if strict and meta and is_current_artifact(item):
        validate_project_context_snapshot(report, item, config, meta_path)

    if item.get("kind") == "DASHBOARD":
        validate_dashboard_gate(report, root, item, manifest, registered_keys, sql_path)


def validate_artifact_state(report: HealthReport, root: Path, item: dict[str, Any], strict: bool) -> None:
    label = artifact_label(item)
    state = item.get("artifact_state") or "current"
    status = item.get("status") or ""
    sql_path = resolve_project_path(root, str(item.get("path") or ""))

    if state == "current" and status == "superseded":
        report.fail("artifact.state_consistency", f"{label} is current but status is superseded.", sql_path)
    elif state == "history" and status != "superseded":
        report.warn("artifact.state_consistency", f"{label} is history but status is not superseded.", sql_path)
    else:
        report.pass_check("artifact.state_consistency", f"{label} state/status are consistent.", sql_path)

    if status == "superseded" and item.get("reusable") is True:
        report.fail("artifact.superseded_not_reusable", f"{label} is superseded but still reusable.", sql_path)

    if state == "history" or status == "superseded":
        if not item.get("replaced_by"):
            report.warn("artifact.replaced_by", f"{label} is history/superseded but has no replaced_by path.", sql_path)

    for key in ["replaced_by", "branch_of"]:
        value = str(item.get(key) or "")
        if value:
            check_rel_exists(report, root, f"artifact.{key}", value, f"{label} {key}")

    supersedes = item.get("supersedes", [])
    if isinstance(supersedes, str):
        supersedes = [supersedes] if supersedes else []
    if isinstance(supersedes, list):
        for value in supersedes:
            if value:
                check_rel_exists(report, root, "artifact.supersedes", str(value), f"{label} supersedes")
    elif strict:
        report.warn("artifact.supersedes_shape", f"{label} supersedes must be an array or string.", sql_path)


def validate_reuse_contract(report: HealthReport, item: dict[str, Any], sql_path: Path) -> None:
    label = artifact_label(item)
    if item.get("reusable") is True:
        if is_nonempty_text(item.get("reuse_notes")):
            report.pass_check("artifact.reuse_notes", f"{label} reusable artifact has reuse_notes.", sql_path)
        else:
            report.fail("artifact.reuse_notes", f"{label} is reusable but reuse_notes is empty.", sql_path)


def validate_artifact_links(report: HealthReport, root: Path, item: dict[str, Any]) -> None:
    label = artifact_label(item)
    for key in ["linked_query", "linked_validation", "linked_run"]:
        value = str(item.get(key) or "")
        if value:
            check_rel_exists(report, root, f"artifact.{key}", value, f"{label} {key}")


def validate_project_context_snapshot(
    report: HealthReport,
    item: dict[str, Any],
    config: dict[str, Any],
    path: Path,
) -> None:
    context = item.get("project_context", {})
    if not isinstance(context, dict):
        report.warn("artifact.project_context", f"{artifact_label(item)} has no project_context snapshot.", path)
        return
    expected_config = config
    execution_profile = str(context.get("execution_profile") or "")
    if execution_profile:
        try:
            expected_config = materialize_profile_config(config, execution_profile)
        except ValueError:
            expected_config = config
    expected = {
        "project_id": expected_config.get("project_id", ""),
        "display_name": expected_config.get("display_name", ""),
        "sql_dialect": expected_config.get("sql_dialect", "missing"),
        "query_engine": expected_config.get("query_engine", "missing"),
        "table_naming_profile": (expected_config.get("table_naming_profile") or {}).get("name", "missing"),
        "partition_policy": (expected_config.get("partition_policy") or {}).get("name", "missing"),
    }
    mismatches = [key for key, value in expected.items() if context.get(key) != value]
    if mismatches:
        report.warn(
            "artifact.project_context_matches_config",
            f"{artifact_label(item)} project_context differs from current project_config fields: {', '.join(mismatches)}.",
            path,
        )
    else:
        report.pass_check(
            "artifact.project_context_matches_config",
            f"{artifact_label(item)} project_context matches current project_config.",
            path,
        )


def run_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for run in manifest.get("run_evidence", []):
        if not isinstance(run, dict):
            continue
        path = normalize_rel(str(run.get("path") or ""))
        if path:
            index[path] = run
    return index


def validate_run_evidence_contracts(report: HealthReport, root: Path, manifest: dict[str, Any]) -> None:
    checked = 0
    failures = 0
    for run in manifest.get("run_evidence", []):
        if not isinstance(run, dict):
            continue
        checked += 1
        run_path_value = str(run.get("path") or "")
        run_path = resolve_project_path(root, run_path_value) if run_path_value else root / "manifest.json"
        failures += validate_json_project_relative_references(
            report,
            run,
            run_path,
            "run_evidence.project_relative_references",
            f"run evidence `{run.get('run_id') or run_path_value or checked}`",
        )
        evidence_file = str(run.get("evidence_file") or "").strip()
        if evidence_file and is_absolute_reference(evidence_file):
            report.fail(
                "run_evidence.evidence_file_project_relative",
                f"Run evidence `{run.get('run_id') or checked}` evidence_file must be project-relative, not `{evidence_file}`.",
                run_path,
            )
            failures += 1
        elif evidence_file:
            ok, message = evidence_file_ok(root, run)
            if not ok:
                report.fail(
                    "run_evidence.evidence_file_exists",
                    f"Run evidence `{run.get('run_id') or checked}` has invalid evidence_file: {message}.",
                    run_path,
                )
                failures += 1
            else:
                evidence_path = resolve_project_path(root, evidence_file)
                retention = run.get("result_evidence_retention")
                if evidence_path.stat().st_size > RESULT_EVIDENCE_MAX_BYTES:
                    report.fail(
                        "run_evidence.result_size",
                        f"Run evidence `{run.get('run_id') or checked}` exceeds 10 MB and must be stored as a result slice.",
                        evidence_path,
                    )
                    failures += 1
                elif not isinstance(retention, dict):
                    report.warn(
                        "run_evidence.retention_contract",
                        f"Run evidence `{run.get('run_id') or checked}` predates the 10 MB result-retention contract.",
                        run_path,
                    )
                elif (
                    retention.get("payload_role") != "sql_output_preview"
                    or retention.get("stored_sha256") != query_workspace_file_sha256(evidence_path)
                    or retention.get("stored_size_bytes") != evidence_path.stat().st_size
                ):
                    report.fail(
                        "run_evidence.retention_contract",
                        f"Run evidence `{run.get('run_id') or checked}` retention metadata does not match its stored preview file.",
                        evidence_path,
                    )
                    failures += 1
        derived_outputs = run.get("derived_outputs")
        if derived_outputs is not None and not isinstance(derived_outputs, list):
            report.fail(
                "run_evidence.derived_outputs",
                f"Run evidence `{run.get('run_id') or checked}` derived_outputs must be an array.",
                run_path,
            )
            failures += 1
            derived_outputs = []
        for output in derived_outputs or []:
            if not isinstance(output, dict):
                report.fail(
                    "run_evidence.derived_output",
                    f"Run evidence `{run.get('run_id') or checked}` contains a malformed visualization asset.",
                    run_path,
                )
                failures += 1
                continue
            output_ref = str(output.get("path") or "")
            output_path = resolve_project_path(root, output_ref) if output_ref else run_path
            retention = output.get("retention") if isinstance(output.get("retention"), dict) else {}
            if (
                not output_ref
                or not output_path.is_file()
                or query_workspace_file_sha256(output_path) != str(output.get("sha256") or "")
                or retention.get("policy") != "full_reusable_output"
                or retention.get("is_sliced")
            ):
                report.fail(
                    "run_evidence.derived_output",
                    f"Run evidence `{run.get('run_id') or checked}` has an invalid or sliced reusable visualization.",
                    output_path,
                )
                failures += 1
            if (
                output.get("lineage_status") != "exact_result"
                or output.get("source_result_id") != (run.get("result_binding_id") or run.get("run_id"))
                or output.get("source_sql_fingerprint") != run.get("source_sql_fingerprint")
            ):
                report.fail(
                    "run_evidence.derived_output_lineage",
                    f"Run evidence `{run.get('run_id') or checked}` visualization is not bound to this exact result and SQL fingerprint.",
                    run_path,
                )
                failures += 1
    if checked and failures == 0:
        report.pass_check(
            "run_evidence.project_relative_references",
            f"{checked} run evidence record(s) use project-relative references.",
            root / "manifest.json",
        )


def linked_run(manifest: dict[str, Any], linked_run_path: str) -> dict[str, Any] | None:
    return run_index(manifest).get(normalize_rel(linked_run_path))


def evidence_file_ok(root: Path, run: dict[str, Any]) -> tuple[bool, str]:
    evidence_file = str(run.get("evidence_file") or "")
    if not evidence_file:
        return False, "missing evidence_file"
    if is_absolute_reference(evidence_file):
        return False, f"evidence_file must be project-relative, not local absolute path: {evidence_file}"
    evidence_path = resolve_project_path(root, evidence_file)
    if evidence_path.suffix.lower() not in RESULT_EXTENSIONS:
        return False, "evidence_file must be .csv or .xlsx"
    if not evidence_path.exists():
        return False, f"evidence_file does not exist: {evidence_file}"
    return True, ""


def validate_dashboard_gate(
    report: HealthReport,
    root: Path,
    item: dict[str, Any],
    manifest: dict[str, Any],
    registered_keys: set[str],
    sql_path: Path,
) -> None:
    label = artifact_label(item)
    status = str(item.get("verification_status") or "not_applicable")
    if status not in ALLOWED_DASHBOARD_VERIFICATION:
        report.fail(
            "dashboard.verification_status",
            f"{label} verification_status must be verified, unverified_skipped_run, or proxy_verified.",
            sql_path,
        )
        return

    run_path = str(item.get("linked_run") or "")
    run = linked_run(manifest, run_path) if run_path else None
    if not run:
        report.fail("dashboard.linked_run", f"{label} must link to run evidence.", sql_path)
        return

    if status == "verified":
        validate_verified_dashboard(report, root, item, run, sql_path)
    elif status == "unverified_skipped_run":
        validate_skipped_dashboard(report, item, run, sql_path)
    elif status == "proxy_verified":
        validate_proxy_dashboard(report, root, item, run, registered_keys, sql_path)


def validate_verified_dashboard(report: HealthReport, root: Path, item: dict[str, Any], run: dict[str, Any], sql_path: Path) -> None:
    label = artifact_label(item)
    failures = []
    if run.get("status") != "passed":
        failures.append("linked run status must be passed")
    if run.get("user_confirmed") is not True:
        failures.append("linked run must be user_confirmed=true")
    ok, message = evidence_file_ok(root, run)
    if not ok:
        failures.append(message)
    if not item.get("linked_query"):
        failures.append("linked_query is required")
    if not item.get("linked_validation"):
        failures.append("linked_validation is required")
    if failures:
        report.fail("dashboard.verified_gate", f"{label} verified gate failed: {'; '.join(failures)}", sql_path)
    else:
        report.pass_check("dashboard.verified_gate", f"{label} has passed run evidence and result file.", sql_path)


def validate_skipped_dashboard(report: HealthReport, item: dict[str, Any], run: dict[str, Any], sql_path: Path) -> None:
    label = artifact_label(item)
    failures = []
    if run.get("status") != "skipped":
        failures.append("linked run status must be skipped")
    if run.get("user_confirmed") is not True:
        failures.append("linked skipped run must be user_confirmed=true")
    for key in ["skip_reason", "risk_note", "future_verification_plan"]:
        if not is_nonempty_text(run.get(key), 8):
            failures.append(f"linked skipped run must include {key}")
    if not is_nonempty_text(item.get("verification_note")):
        failures.append("dashboard metadata must include verification_note")
    if not is_nonempty_text(item.get("future_verification_plan"), 8):
        failures.append("dashboard metadata must include future_verification_plan")
    tags = set(item.get("tags") or [])
    for tag in ["unvalidated", "no_result_file"]:
        if tag not in tags:
            failures.append(f"dashboard metadata tags must include {tag}")
    if failures:
        report.fail("dashboard.unverified_skipped_run_gate", f"{label} skipped gate failed: {'; '.join(failures)}", sql_path)
    else:
        report.pass_check("dashboard.unverified_skipped_run_gate", f"{label} has explicit skipped-run risk and future plan.", sql_path)


def validate_proxy_dashboard(
    report: HealthReport,
    root: Path,
    item: dict[str, Any],
    run: dict[str, Any],
    registered_keys: set[str],
    sql_path: Path,
) -> None:
    label = artifact_label(item)
    failures = []
    if run.get("status") != "proxy_verified":
        failures.append("linked run status must be proxy_verified")
    if run.get("user_confirmed") is not True:
        failures.append("linked proxy run must be user_confirmed=true")
    roles = [run.get("definition_project"), run.get("execution_project"), run.get("delivery_project")]
    if any(not is_nonempty_text(role) for role in roles):
        failures.append("proxy run must include definition/execution/delivery project")
    elif len(set(roles)) == 1:
        failures.append("proxy run must have at least one differing project role")
    concept_keys = [slug_text(str(key)) for key in run.get("concept_keys", []) if str(key).strip()]
    if not concept_keys:
        failures.append("proxy run must include concept_keys")
    missing_keys = [key for key in concept_keys if key not in registered_keys]
    if missing_keys:
        failures.append("proxy run concept_keys must be registered: " + ", ".join(missing_keys))
    if not is_nonempty_text(run.get("proxy_limitations"), 8):
        failures.append("proxy run must include proxy_limitations")
    if not is_nonempty_text(run.get("future_verification_plan"), 8):
        failures.append("proxy run must include future_verification_plan")
    ok, message = evidence_file_ok(root, run)
    if not ok:
        failures.append(message)
    if not is_nonempty_text(item.get("verification_note")):
        failures.append("dashboard metadata must include verification_note")
    if not is_nonempty_text(item.get("future_verification_plan"), 8):
        failures.append("dashboard metadata must include future_verification_plan")
    tags = set(item.get("tags") or [])
    for tag in ["proxy_verified", "needs_target_verification"]:
        if tag not in tags:
            failures.append(f"dashboard metadata tags must include {tag}")
    if failures:
        report.fail("dashboard.proxy_verified_gate", f"{label} proxy gate failed: {'; '.join(failures)}", sql_path)
    else:
        report.pass_check("dashboard.proxy_verified_gate", f"{label} has proxy evidence, limitations, and target verification plan.", sql_path)


def validate_project_knowledge(report: HealthReport, root: Path) -> None:
    bindings_path = root / "knowledge" / "bindings.json"
    if not bindings_path.exists():
        report.pass_check(
            "knowledge.bindings",
            "Project has no active knowledge datasets; config-table lookup remains unavailable until explicitly bound.",
            root,
        )
        return
    try:
        result = validate_knowledge_repository(discover_repo_root(root), root)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        report.fail("knowledge.bindings", f"Knowledge validation failed: {exc}", bindings_path)
        return
    for problem in result.get("problems", []):
        report.fail("knowledge.integrity", str(problem), bindings_path)
    for warning in result.get("warnings", []):
        report.warn("knowledge.integrity", str(warning), bindings_path)
    if not result.get("problems") and not result.get("warnings"):
        bindings = result.get("bindings", [])
        summary = ", ".join(
            f"{item.get('dataset_id')}@{item.get('dataset_version')}"
            for item in bindings
        )
        report.pass_check(
            "knowledge.integrity",
            f"Active knowledge bindings are content-addressed and valid: {summary or 'none'}.",
            bindings_path,
        )


def validate_project_planning_source(report: HealthReport, root: Path) -> None:
    binding_path = root / "planning" / "source_binding.json"
    if not binding_path.exists():
        report.pass_check(
            "planning_source.binding",
            "Project has no tracked planning-source binding.",
            root,
        )
        return
    try:
        result = planning_source.validate_active(
            argparse.Namespace(project=root.name),
            discover_repo_root(root),
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        report.fail(
            "planning_source.integrity",
            f"Planning-source validation failed: {exc}",
            binding_path,
        )
        return
    if result.get("status") != "pass":
        for problem in result.get("problems", []) or ["unknown planning-source integrity failure"]:
            report.fail("planning_source.integrity", str(problem), binding_path)
        return
    report.pass_check(
        "planning_source.integrity",
        f"Active release {result.get('active_release_id')} is sealed and verified across "
        f"{result.get('verified_file_count', 0)} files with no stale planning-backed Knowledge.",
        binding_path,
    )


def validate_project(root: Path, strict: bool, scope: str = "full") -> dict[str, Any]:
    project = root.name
    config_path = root / "project_config.json"
    config_data, _ = read_json_file(config_path)
    if config_data and config_data.get("project_id"):
        project = str(config_data["project_id"])
    report = HealthReport(project, root, strict, scope)

    if not root.exists() or not root.is_dir():
        report.fail("project_root.exists", f"Project root does not exist or is not a directory: {root}", root)
        return report.payload()
    report.pass_check("project_root.exists", "Project root exists.", root)

    config_error = read_json_file(config_path)[1]
    config = config_data
    if config_error:
        report.fail("project_config.exists", config_error, config_path)
        config = {}
    else:
        config = validate_project_config(report, config, config_path)

    manifest_path = root / "manifest.json"
    manifest, manifest_error = read_json_file(manifest_path)
    if manifest_error:
        report.fail("manifest.exists", manifest_error, manifest_path)
        manifest = {}
    else:
        manifest = validate_manifest_refs(report, root, manifest, manifest_path)
        validate_project_index(report, root)
        validate_formal_asset_repository(report, root, manifest)
    workspace_index, _ = read_json_file(root / QUERY_WORKSPACE_INDEX_REL)
    report.deferred_history = deferred_history_summary(
        [item for item in manifest.get("artifacts", []) if isinstance(item, dict)],
        [item for item in (workspace_index or {}).get("entries", []) if isinstance(item, dict)],
        scope,
    )

    registered_keys = load_concept_keys(report, root)
    validate_canonical_rules(
        report,
        root,
        registered_keys,
    )
    validate_source_contracts(report, root)
    validate_project_planning_source(report, root)
    validate_project_knowledge(report, root)
    if manifest.get("schema_version") == "project_manifest_v2":
        validate_query_workspace(report, root, manifest, scope=scope)
        validate_unmanaged_sql_work(report, root)
    else:
        validate_artifacts(report, root, manifest, config or {}, registered_keys, strict, scope)
    validate_intermediate_tables(report, root, manifest, strict)
    if manifest.get("schema_version") != "project_manifest_v2":
        validate_run_evidence_contracts(report, root, manifest)
    return report.payload()


def error_payload(root: Path | None, message: str, scope: str = "current") -> dict[str, Any]:
    project = root.name if root else "unknown"
    check = {
        "id": "runtime.error",
        "status": "fail",
        "message": message,
        "path": str(root or ""),
    }
    return {
        "project": project,
        "status": "error",
        "root": str(root or ""),
        "strict": False,
        "scope": scope,
        "summary": {"checks": 1, "passed": 0, "warnings": 0, "failures": 1},
        "checks": [check],
        "warnings": [],
        "errors": [check],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Project root, e.g. ./sql-projects/DEMO_ANALYTICS")
    parser.add_argument("--format", choices=["json", "summary"], default="summary")
    parser.add_argument(
        "--scope",
        choices=["current", "full"],
        default="current",
        help="Daily delivery checks current assets; release/migration audits use full history.",
    )
    parser.add_argument("--strict", action="store_true", help="Enable stricter health checks; warning exit code remains 2.")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-JSON chatter. JSON output is never suppressed.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    try:
        payload = validate_project(root, args.strict, args.scope)
        rendered = compact_health_payload(payload) if args.format == "summary" else payload
        print(json.dumps(rendered, ensure_ascii=False, indent=2))
        raise SystemExit(exit_code_for_status(payload["status"]))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        payload = error_payload(root, str(exc), args.scope)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(3)


if __name__ == "__main__":
    main()
