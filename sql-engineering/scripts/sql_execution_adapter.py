#!/usr/bin/env python3
"""Route and materialize one portable TLOG SQL template for one executor."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from asset_provenance import stamp_sql_generation
from capability_registry import command_function_ids
from execution_delivery import build_variant_identity, engine_key
from function_gate import (
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from sql_identifier_policy import config_problems as identifier_policy_config_problems
from sql_identifier_policy import policy_findings as identifier_policy_findings
from sql_identifier_policy import quote_required_identifiers
from sql_facts import execution_fingerprint
from sql_time_contract import (
    TIME_CONTRACT_VERSION,
    TIME_INTEGRITY_POLICY_VERSION,
    analyze_time_contract,
    analyze_time_integrity_contract,
    fixed_time_window,
    time_integrity_config_problems,
    time_integrity_match_expression,
    time_integrity_plan,
    time_integrity_policy_fingerprint,
)


ROUTE_SCHEMA_VERSION = "execution_route_v1"
TEMPLATE_CONTRACT_VERSION = "portable_tlog_sql_v1"
ADAPTER_CONTRACT_VERSION = "execution_adapters_v2"
TLOG_TOKEN_RE = re.compile(
    r"\{\{TLOG:([A-Za-z][A-Za-z0-9_]*):([A-Za-z][A-Za-z0-9_]*)\}\}"
)
TIME_FILTER_TOKEN_RE = re.compile(r"\{\{TLOG_TIME_FILTER:([A-Za-z][A-Za-z0-9_]*)\}\}")
DETAIL_FILTER_TOKEN_RE = re.compile(r"\{\{TLOG_DETAIL_TIME_FILTER:([A-Za-z][A-Za-z0-9_]*)\}\}")
INTEGRITY_FILTER_TOKEN_RE = re.compile(r"\{\{TLOG_TIME_INTEGRITY_FILTER:([A-Za-z][A-Za-z0-9_]*)\}\}")
SOURCE_RE = re.compile(
    r"\b(?:from|join)\s+`?([A-Za-z_][\w$]*)`?\s*\.\s*`?([A-Za-z_][\w$]*)`?",
    flags=re.I,
)
UNRESOLVED_TOKEN_RE = re.compile(r"\{\{[^{}]+\}\}")


def strip_sql_comments(sql: str) -> str:
    no_blocks = re.sub(r"/\*.*?\*/", " ", sql or "", flags=re.S)
    return re.sub(r"--[^\r\n]*", " ", no_blocks)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def execution_adapters(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    contract = config.get("execution_adapters")
    if not isinstance(contract, dict):
        return {}
    profiles = contract.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    return {
        str(profile_id): copy.deepcopy(profile)
        for profile_id, profile in profiles.items()
        if isinstance(profile, dict)
    }


def default_execution_profile(config: dict[str, Any]) -> str:
    contract = config.get("execution_adapters")
    if not isinstance(contract, dict):
        return ""
    return str(contract.get("default_profile") or "").strip()


def project_table_prefix(config: dict[str, Any]) -> str:
    contract = config.get("execution_adapters")
    if isinstance(contract, dict):
        value = str(contract.get("project_table_prefix") or "").strip().lower()
        if value:
            return value
    project_id = str(config.get("project_id") or "").strip().lower()
    return "demo" if project_id == "demo_analytics" else ""


def passthrough_tables(config: dict[str, Any]) -> set[str]:
    contract = config.get("execution_adapters")
    rows = contract.get("passthrough_tables", []) if isinstance(contract, dict) else []
    return {str(item).strip().lower() for item in rows if str(item).strip()}


def profile_table_pattern(config: dict[str, Any], profile: dict[str, Any]) -> str:
    pattern = str(profile.get("table_pattern") or "").strip()
    if pattern:
        return pattern
    database = str(profile.get("database") or "").strip()
    prefix = project_table_prefix(config)
    return f"{database}.{prefix}_dsl_{{log_lower}}_fht0" if database and prefix else ""


def materialize_profile_config(
    config: dict[str, Any],
    profile_id: str,
) -> dict[str, Any]:
    profiles = execution_adapters(config)
    profile = profiles.get(profile_id)
    if not profile:
        raise ValueError(f"Unknown execution profile: {profile_id}")
    effective = copy.deepcopy(config)
    effective["sql_dialect"] = profile.get("sql_dialect", effective.get("sql_dialect"))
    effective["query_engine"] = profile.get("query_engine", effective.get("query_engine"))
    effective["query_environment"] = copy.deepcopy(
        profile.get("query_environment", effective.get("query_environment"))
    )
    effective["partition_policy"] = copy.deepcopy(
        profile.get("partition_policy", effective.get("partition_policy"))
    )
    if "identifier_policy" in profile:
        effective["identifier_policy"] = copy.deepcopy(profile.get("identifier_policy") or {})
    if "time_integrity_policy" in profile:
        effective["time_integrity_policy"] = copy.deepcopy(
            profile.get("time_integrity_policy") or {}
        )
    effective["table_naming_profile"] = {
        "name": str(profile.get("table_profile_name") or profile_id),
        "dialect": str(profile.get("sql_dialect") or ""),
        "database": str(profile.get("database") or ""),
        "pattern": profile_table_pattern(config, profile),
        "description": str(profile.get("description") or ""),
        "status": "configured",
    }
    effective["selected_execution_profile"] = profile_id
    return effective


def adapter_config_problems(config: dict[str, Any]) -> list[str]:
    contract = config.get("execution_adapters")
    if contract is None:
        return []
    if not isinstance(contract, dict):
        return ["execution_adapters must be an object."]
    problems: list[str] = []
    if contract.get("contract_version") != ADAPTER_CONTRACT_VERSION:
        problems.append(
            f"execution_adapters.contract_version must be {ADAPTER_CONTRACT_VERSION}."
        )
    prefix = str(contract.get("project_table_prefix") or "").strip()
    if not prefix:
        problems.append("execution_adapters.project_table_prefix is required.")
    profiles = execution_adapters(config)
    if len(profiles) < 2:
        problems.append("execution_adapters requires at least fast and stable profiles.")
    default_profile = default_execution_profile(config)
    if default_profile not in profiles:
        problems.append("execution_adapters.default_profile must name a configured profile.")
    elif str(profiles[default_profile].get("sql_dialect") or "") != "StarRocks":
        problems.append("execution_adapters.default_profile must be a StarRocks profile; Hive requires explicit selection.")
    routing_policy = contract.get("routing_policy")
    if not isinstance(routing_policy, dict):
        problems.append("execution_adapters.routing_policy is required.")
    else:
        if routing_policy.get("primary_signal") != "structural_complexity_x_data_amplification":
            problems.append(
                "execution_adapters.routing_policy.primary_signal must be "
                "structural_complexity_x_data_amplification."
            )
        density_multipliers = routing_policy.get("density_multipliers")
        if not isinstance(density_multipliers, dict) or any(
            float(density_multipliers.get(band) or 0) <= 0
            for band in ["extreme", "high", "medium", "low", "unknown"]
        ):
            problems.append(
                "execution_adapters.routing_policy.density_multipliers requires positive "
                "extreme/high/medium/low/unknown values."
            )
        if float(routing_policy.get("score_to_stable") or 0) <= 0:
            problems.append(
                "execution_adapters.routing_policy.score_to_stable must be positive."
            )
        if int(routing_policy.get("minimum_structural_score_to_stable") or 0) <= 0:
            problems.append(
                "execution_adapters.routing_policy.minimum_structural_score_to_stable "
                "must be positive."
            )
        date_amplifier = routing_policy.get("date_amplifier")
        if not isinstance(date_amplifier, dict):
            problems.append("execution_adapters.routing_policy.date_amplifier is required.")
        elif (
            int(date_amplifier.get("base_days") or 0) <= 0
            or float(date_amplifier.get("exponent") or 0) <= 0
            or float(date_amplifier.get("max_multiplier") or 0) < 1
        ):
            problems.append(
                "execution_adapters.routing_policy.date_amplifier requires positive base_days/"
                "exponent and max_multiplier >= 1."
            )
    roles: set[str] = set()
    for profile_id, profile in profiles.items():
        role = str(profile.get("routing_role") or "").strip()
        roles.add(role)
        if role not in {"fast", "stable"}:
            problems.append(f"execution profile {profile_id} routing_role must be fast or stable.")
        if profile.get("sql_dialect") not in {"Hive", "StarRocks"}:
            problems.append(f"execution profile {profile_id} sql_dialect must be Hive or StarRocks.")
        if not str(profile.get("database") or "").strip():
            problems.append(f"execution profile {profile_id} database is required.")
        pattern = profile_table_pattern(config, profile)
        if "{log_lower}" not in pattern:
            problems.append(f"execution profile {profile_id} table_pattern must contain {{log_lower}}.")
        policy = profile.get("partition_policy")
        if not isinstance(policy, dict) or policy.get("strict_generation") is not True:
            problems.append(f"execution profile {profile_id} requires strict partition_policy.")
        profile_config = {
            "sql_dialect": profile.get("sql_dialect"),
            "query_engine": profile.get("query_engine"),
            "query_environment": profile.get("query_environment"),
            "partition_policy": policy or {},
            "identifier_policy": profile.get("identifier_policy") or {},
            "time_integrity_policy": profile.get(
                "time_integrity_policy",
                config.get("time_integrity_policy"),
            ),
        }
        problems.extend(
            identifier_policy_config_problems(
                profile_config,
                label=f"execution_adapters.profiles.{profile_id}.identifier_policy",
            )
        )
        problems.extend(
            time_integrity_config_problems(
                profile_config,
                label=f"execution_adapters.profiles.{profile_id}.time_integrity_policy",
            )
        )
    if not {"fast", "stable"}.issubset(roles):
        problems.append("execution_adapters must contain one fast and one stable routing role.")
    return problems


def _profile_source_regex(config: dict[str, Any], profile: dict[str, Any]) -> re.Pattern[str]:
    pattern = profile_table_pattern(config, profile).strip().lower().replace("`", "")
    escaped = re.escape(pattern)
    for token in ["log_lower", "log_name", "log_upper"]:
        escaped = escaped.replace(re.escape("{" + token + "}"), rf"(?P<{token}>[a-z0-9_]+)")
    return re.compile(rf"^{escaped}$", flags=re.I)


def _classify_physical_sources(
    sql: str,
    config: dict[str, Any],
) -> tuple[list[dict[str, str]], list[str]]:
    cleaned = strip_sql_comments(sql)
    profiles = execution_adapters(config)
    if profiles:
        profile_patterns = {
            profile_id: _profile_source_regex(config, profile)
            for profile_id, profile in profiles.items()
        }
    else:
        fixed_profile = config.get("table_naming_profile")
        fixed_profile = fixed_profile if isinstance(fixed_profile, dict) else {}
        pattern = str(fixed_profile.get("pattern") or "").strip()
        profile_patterns = (
            {"": _profile_source_regex(config, {"table_pattern": pattern})}
            if pattern
            else {}
        )
    if not profile_patterns:
        return [], []
    allowed_passthrough = passthrough_tables(config)
    rows: list[dict[str, str]] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for database, table in SOURCE_RE.findall(cleaned):
        full = f"{database}.{table}".lower()
        if full in seen:
            continue
        seen.add(full)
        if full in allowed_passthrough:
            continue
        matches = [
            (profile_id, pattern.match(full))
            for profile_id, pattern in profile_patterns.items()
        ]
        matches = [(profile_id, match) for profile_id, match in matches if match]
        if not matches:
            if re.search(r"_dsl_.+_fht0$", table, flags=re.I):
                unknown.append(f"{database}.{table}")
            continue
        profile_id, match = matches[0]
        groups = match.groupdict()
        rows.append(
            {
                "database": database,
                "table": table,
                "physical_table": f"{database}.{table}",
                "log_lower": str(
                    groups.get("log_lower")
                    or groups.get("log_name")
                    or groups.get("log_upper")
                    or ""
                ).lower(),
                "profile_id": profile_id,
            }
        )
    return rows, sorted(unknown)


def physical_tlog_sources(sql: str, config: dict[str, Any]) -> list[dict[str, str]]:
    rows, _ = _classify_physical_sources(sql, config)
    return rows


def detect_execution_profile(sql: str, config: dict[str, Any]) -> dict[str, Any]:
    profiles = execution_adapters(config)
    tlogs, unknown = _classify_physical_sources(sql, config)
    portable_tokens = [
        {"log_name": match.group(1), "alias": match.group(2)}
        for match in TLOG_TOKEN_RE.finditer(sql or "")
    ]
    if portable_tokens:
        return {
            "status": "portable",
            "selected_profile": "",
            "source_tlogs": portable_tokens,
            "passthrough_tables": [],
            "blockers": ["Portable SQL template must be materialized before delivery."],
        }
    selected = sorted(
        {
            str(row.get("profile_id") or "")
            for row in tlogs
            if str(row.get("profile_id") or "")
        }
    )
    sources = [f"{db}.{table}".lower() for db, table in SOURCE_RE.findall(strip_sql_comments(sql))]
    passthrough = sorted({source for source in sources if source in passthrough_tables(config)})
    if unknown:
        return {
            "status": "unknown_tlog_source",
            "selected_profile": "",
            "source_tlogs": tlogs,
            "passthrough_tables": passthrough,
            "blockers": [
                "TLOG source does not match the project execution environment: "
                + ", ".join(sorted(unknown))
            ],
        }
    if len(selected) > 1:
        return {
            "status": "mixed",
            "selected_profile": "",
            "source_tlogs": tlogs,
            "passthrough_tables": passthrough,
            "blockers": ["One SQL cannot mix TLOG execution profiles: " + ", ".join(selected)],
        }
    if selected:
        return {
            "status": "selected",
            "selected_profile": selected[0],
            "source_tlogs": tlogs,
            "passthrough_tables": passthrough,
            "blockers": [],
        }
    if tlogs:
        return {
            "status": "fixed",
            "selected_profile": "",
            "source_tlogs": tlogs,
            "passthrough_tables": passthrough,
            "blockers": [],
        }
    return {
        "status": "passthrough" if passthrough else "not_applicable",
        "selected_profile": "",
        "source_tlogs": tlogs,
        "passthrough_tables": passthrough,
        "blockers": [],
    }


def effective_config_for_sql(
    config: dict[str, Any],
    sql: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    detection = detect_execution_profile(sql, config)
    configured_profile = default_execution_profile(config)
    profiles = execution_adapters(config)
    if configured_profile and configured_profile in profiles:
        effective = materialize_profile_config(config, configured_profile)
        detected_profile = str(detection.get("selected_profile") or "")
        detection = copy.deepcopy(detection)
        detection["configured_profile"] = configured_profile
        detection["detected_profile"] = detected_profile
        if detected_profile and detected_profile != configured_profile:
            detection["status"] = "profile_mismatch"
            detection.setdefault("blockers", []).append(
                "Physical TLOG tables match execution profile "
                f"`{detected_profile}`, but the project default is `{configured_profile}`. "
                "Keep the configured StarRocks default or explicitly materialize the requested Hive profile."
            )
            detection["blockers"] = list(dict.fromkeys(detection["blockers"]))
        # The detected profile is diagnostic evidence only. Downstream callers
        # must use the configured profile from the effective config.
        detection["selected_profile"] = configured_profile
        return effective, detection
    return copy.deepcopy(config), detection


def route_sql_fingerprint(sql: str) -> str:
    """Fingerprint the executable body while ignoring the managed header."""

    return execution_fingerprint(sql)


def route_config_fingerprint(config: dict[str, Any]) -> str:
    """Identify the project execution context without persisting its contents."""

    execution_keys = (
        "project_id",
        "sql_dialect",
        "query_engine",
        "query_environment",
        "execution_adapters",
        "table_naming_profile",
        "table_overrides",
        "partition_policy",
        "time_integrity_policy",
        "identifier_policy",
    )
    payload = json.dumps(
        {key: (config or {}).get(key) for key in execution_keys},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def route_matches_context(
    route: dict[str, Any] | None,
    sql: str,
    config: dict[str, Any],
) -> bool:
    """Accept a supplied route only when it identifies this exact SQL/config pair."""

    if not isinstance(route, dict):
        return False
    selected_profile = str(route.get("selected_profile") or "").strip()
    configured_profile = default_execution_profile(config)
    selection_mode = str(route.get("selection_mode") or "").strip()
    if selection_mode == "explicit":
        profile_selection_matches = selected_profile in execution_adapters(config)
    elif not selected_profile and not route.get("source_tlogs"):
        profile_selection_matches = True
    elif configured_profile:
        profile_selection_matches = selected_profile == configured_profile
    else:
        profile_selection_matches = not selected_profile
    if profile_selection_matches and selected_profile:
        # A receipt is an identity record, not permission to reinterpret the
        # physical table. Recheck the cheap source/profile relationship before
        # trusting an explicit route supplied by a sidecar or workspace.
        source_text = strip_sql_comments(sql)
        if re.search(r"(?:_dsl_|tdbank|fht0)", source_text, flags=re.I):
            detected = detect_execution_profile(sql, config)
            if detected.get("status") in {"mixed", "unknown_tlog_source", "portable"}:
                profile_selection_matches = False
            elif (
                detected.get("status") == "selected"
                and str(detected.get("selected_profile") or "") != selected_profile
            ):
                profile_selection_matches = False
    return bool(
        profile_selection_matches
        and route.get("schema_version") == ROUTE_SCHEMA_VERSION
        and route.get("sql_fingerprint") == route_sql_fingerprint(sql)
        and route.get("config_fingerprint") == route_config_fingerprint(config)
        and isinstance(route.get("time_contract"), dict)
        and route.get("time_contract", {}).get("contract_version")
        == TIME_CONTRACT_VERSION
    )


def effective_config_from_route(
    config: dict[str, Any],
    route: dict[str, Any],
) -> dict[str, Any]:
    """Reconstruct the small profile projection needed by downstream gates."""

    profile_id = str(route.get("selected_profile") or "").strip()
    if profile_id and profile_id in execution_adapters(config):
        return materialize_profile_config(config, profile_id)
    effective = copy.deepcopy(config)
    for key in ("sql_dialect", "query_engine", "partition_policy", "identifier_policy"):
        if key in route:
            effective[key] = copy.deepcopy(route.get(key))
    return effective


def effective_config_for_context(
    config: dict[str, Any],
    sql: str,
    execution_route: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one exact persisted route, otherwise retain the project default."""

    if route_matches_context(execution_route, sql, config):
        route = execution_route or {}
        return effective_config_from_route(config, route), {
            "status": "persisted_route",
            "selected_profile": str(route.get("selected_profile") or ""),
            "source_tlogs": copy.deepcopy(route.get("source_tlogs") or []),
            "passthrough_tables": copy.deepcopy(route.get("passthrough_tables") or []),
            "blockers": [],
        }
    return effective_config_for_sql(config, sql)


def rebase_execution_route_for_sql(
    sql: str,
    config: dict[str, Any],
    parent_route: dict[str, Any] | None,
) -> dict[str, Any]:
    """Carry a selected profile across deterministic SQL normalization."""

    if route_matches_context(parent_route, sql, config):
        return copy.deepcopy(parent_route or {})
    if not isinstance(parent_route, dict):
        return execution_route_for_sql(sql, config)

    profile_id = str(parent_route.get("selected_profile") or "").strip()
    profiles = execution_adapters(config)
    if not profile_id or profile_id not in profiles:
        return execution_route_for_sql(sql, config)

    effective = materialize_profile_config(config, profile_id)
    detection = detect_execution_profile(sql, config)
    route = execution_route_for_sql(
        sql,
        config,
        effective_config=effective,
        detection=detection,
    )
    route["selection_mode"] = str(parent_route.get("selection_mode") or "explicit")
    route["selected_profile"] = profile_id
    if detection.get("status") in {"mixed", "unknown_tlog_source", "portable"}:
        route["status"] = "blocked"
    elif (
        detection.get("status") == "selected"
        and str(detection.get("selected_profile") or "") != profile_id
    ):
        route["status"] = "blocked"
        route.setdefault("blockers", []).append(
            "Rebased execution route profile does not match the SQL physical TLOG sources."
        )
    route["blockers"] = list(dict.fromkeys(route.get("blockers", []) or []))
    return route


def execution_route_for_sql(
    sql: str,
    config: dict[str, Any],
    *,
    effective_config: dict[str, Any] | None = None,
    detection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(effective_config, dict) or not isinstance(detection, dict):
        effective, detection = effective_config_for_sql(config, sql)
    else:
        effective = copy.deepcopy(effective_config)
    detection_status = str(detection.get("status") or "")
    configured_profile = str(
        effective.get("selected_execution_profile")
        or default_execution_profile(config)
        or ""
    )
    profile_id = configured_profile if detection.get("source_tlogs") else ""
    profile = execution_adapters(config).get(profile_id, {})
    fixed_profile = effective.get("table_naming_profile") if isinstance(effective.get("table_naming_profile"), dict) else {}
    routing_role = str(
        profile.get("routing_role")
        or ("fixed" if detection_status == "fixed" else "passthrough")
    )
    if detection_status == "profile_mismatch":
        routing_reasons = [
            "Physical table profile is diagnostic evidence; configured project profile remains authoritative."
        ]
    elif profile_id and detection_status == "selected":
        routing_reasons = [
            "Configured default profile validated against the materialized TLOG table pattern."
        ]
    elif detection_status == "fixed":
        routing_reasons = ["Fixed project TLOG environment validated from table_naming_profile."]
    elif detection_status in {"passthrough", "not_applicable"}:
        routing_reasons = ["No routed TLOG source requires dual-engine selection."]
    else:
        routing_reasons = ["Execution profile detected from materialized TLOG table pattern."]
    # The full time contract owns both partition bounds and the optional
    # paired-clock check. Keep it on the route so downstream gates do not
    # independently re-analyze the same SQL.
    time_contract = analyze_time_contract(sql, effective)
    time_integrity_contract = time_contract.get("time_integrity") or {}
    time_integrity = copy.deepcopy(
        (time_integrity_contract.get("plan") or {})
    )
    blockers = [str(item) for item in detection.get("blockers", []) or []]
    blockers.extend(
        str(item.get("message") or "Time-integrity contract is not satisfied.")
        for item in time_integrity_contract.get("findings", []) or []
        if item.get("severity") == "blocker"
    )
    blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "status": "blocked" if blockers else "ready",
        "selection_mode": "configured_default" if profile_id else "project_configured",
        "selected_profile": profile_id,
        "routing_role": routing_role,
        "sql_dialect": str(effective.get("sql_dialect") or ""),
        "query_engine": str(effective.get("query_engine") or ""),
        "database": str(profile.get("database") or fixed_profile.get("database") or ""),
        "partition_policy": copy.deepcopy(effective.get("partition_policy") or {}),
        "identifier_policy": copy.deepcopy(effective.get("identifier_policy") or {}),
        "time_integrity": time_integrity,
        "time_contract": copy.deepcopy(time_contract),
        "sql_fingerprint": route_sql_fingerprint(sql),
        "config_fingerprint": route_config_fingerprint(config),
        "source_tlogs": copy.deepcopy(detection.get("source_tlogs") or []),
        "passthrough_tables": copy.deepcopy(detection.get("passthrough_tables") or []),
        "routing_reasons": routing_reasons,
        "volume_evidence": {},
        "blockers": blockers,
    }


def route_receipt_path(sql_path: Path) -> Path:
    return sql_path.with_suffix(".execution-route.json")


def execution_route_for_file(
    sql_path: Path,
    sql: str,
    config: dict[str, Any],
    *,
    precomputed_route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if route_matches_context(precomputed_route, sql, config):
        return copy.deepcopy(precomputed_route)
    receipt_path = route_receipt_path(sql_path)
    if not receipt_path.exists():
        return execution_route_for_sql(sql, config)
    effective, _ = effective_config_for_sql(config, sql)
    current_policy_fingerprint = time_integrity_policy_fingerprint(effective)
    current_plan = time_integrity_plan(sql, effective)
    try:
        receipt = read_json(receipt_path, {})
    except (OSError, json.JSONDecodeError):
        receipt = {}
    expected_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    if (
        isinstance(receipt, dict)
        and receipt.get("schema_version") == ROUTE_SCHEMA_VERSION
        and receipt.get("status") == "ready"
        and receipt.get("rendered_sql_sha256") == expected_hash
        and (
            not receipt.get("sql_fingerprint")
            or receipt.get("sql_fingerprint") == route_sql_fingerprint(sql)
        )
        and (
            not receipt.get("config_fingerprint")
            or receipt.get("config_fingerprint") == route_config_fingerprint(config)
        )
        and isinstance(receipt.get("time_integrity"), dict)
        and receipt.get("time_integrity", {}).get("contract_version")
        == TIME_INTEGRITY_POLICY_VERSION
        and receipt.get("time_integrity", {}).get("policy_fingerprint")
        == current_policy_fingerprint
        and receipt.get("time_integrity", {}).get("window", {}).get("as_of_date")
        == current_plan.get("window", {}).get("as_of_date")
        and receipt.get("time_integrity", {}).get("actual_range_required")
        == current_plan.get("actual_range_required")
        and receipt.get("time_integrity", {})
        .get("actual_range_output", {})
        .get("status")
        == current_plan.get("actual_range_output", {}).get("status")
    ):
        receipt = copy.deepcopy(receipt)
        receipt.pop("route_receipt", None)
        return receipt
    return execution_route_for_sql(sql, config)


def _volume_reference(config: dict[str, Any], root: Path | None) -> dict[str, Any]:
    contract = config.get("execution_adapters")
    relative = str(contract.get("volume_reference") or "") if isinstance(contract, dict) else ""
    if not relative or root is None:
        return {}
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return {}
    return read_json(path, {})


def _template_complexity(template: str) -> dict[str, int]:
    cleaned = strip_sql_comments(template).lower()
    log_names = [match.group(1).lower() for match in TLOG_TOKEN_RE.finditer(template)]
    repeated_log_scan_count = len(log_names) - len(set(log_names))
    return {
        "join_count": len(re.findall(r"\bjoin\b", cleaned)),
        "window_count": len(re.findall(r"\bover\s*\(", cleaned)),
        "count_distinct_count": len(re.findall(r"\bcount\s*\(\s*distinct\b", cleaned)),
        "union_count": len(re.findall(r"\bunion(?:\s+all)?\b", cleaned)),
        "cte_count": len(re.findall(r"(?:\bwith\b|,)\s*[a-z_][\w$]*\s+as\s*\(", cleaned)),
        "repeated_log_scan_count": repeated_log_scan_count,
    }


def plan_template_route(
    template: str,
    config: dict[str, Any],
    *,
    start_date: str,
    end_date: str,
    requested_profile: str = "auto",
    root: Path | None = None,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        raise ValueError("end_date must be on or after start_date.")
    scan_days = (end - start).days + 1
    token_rows = [
        {"log_name": match.group(1), "log_lower": match.group(1).lower(), "alias": match.group(2)}
        for match in TLOG_TOKEN_RE.finditer(template)
    ]
    if not token_rows:
        raise ValueError("Portable SQL requires {{TLOG:LogName:alias}} tokens.")
    aliases = {row["alias"].lower() for row in token_rows}
    filter_aliases = {match.group(1).lower() for match in TIME_FILTER_TOKEN_RE.finditer(template)}
    missing_filters = sorted(aliases - filter_aliases)
    if missing_filters:
        raise ValueError(
            "Every TLOG alias requires {{TLOG_TIME_FILTER:alias}}: " + ", ".join(missing_filters)
        )
    complexity = _template_complexity(template)
    score = (
        1
        + len(token_rows) * 2
        + complexity["join_count"] * 3
        + complexity["window_count"] * 4
        + complexity["count_distinct_count"] * 2
        + max(0, complexity["cte_count"] - 3)
        + complexity["repeated_log_scan_count"] * 3
    )
    reference = _volume_reference(config, root)
    density_by_log = {
        str(row.get("log_lower") or "").lower(): row
        for row in reference.get("logs", [])
        if isinstance(row, dict)
    }
    density_rank = {"low": 0, "medium": 1, "high": 2, "extreme": 3}
    evidence_rows = [density_by_log[row["log_lower"]] for row in token_rows if row["log_lower"] in density_by_log]
    max_band = max(
        (str(row.get("density_band") or "low") for row in evidence_rows),
        key=lambda item: density_rank.get(item, -1),
        default="unknown",
    )
    routing_policy = (config.get("execution_adapters") or {}).get("routing_policy") or {}
    density_multipliers = routing_policy.get("density_multipliers") or {}
    source_scan_rows: list[dict[str, Any]] = []
    for token_row in token_rows:
        evidence = density_by_log.get(token_row["log_lower"], {})
        band = str(evidence.get("density_band") or "unknown")
        multiplier = float(
            density_multipliers.get(band)
            or density_multipliers.get("unknown")
            or 1.0
        )
        source_row = {
            **token_row,
            "density_band": band,
            "density_multiplier": multiplier,
            "scan_days": scan_days,
        }
        source_scan_rows.append(source_row)
    density_multiplier = max(
        (float(row["density_multiplier"]) for row in source_scan_rows),
        default=float(density_multipliers.get("unknown") or 1.0),
    )
    date_policy = routing_policy.get("date_amplifier") or {}
    base_days = max(1, int(date_policy.get("base_days") or 7))
    exponent = float(date_policy.get("exponent") or 0.5)
    max_date_multiplier = max(1.0, float(date_policy.get("max_multiplier") or 4.0))
    date_multiplier = min(
        max_date_multiplier,
        max(1.0, (scan_days / base_days) ** exponent),
    )
    amplified_score = round(score * density_multiplier * date_multiplier, 6)
    minimum_structural_score = int(
        routing_policy.get("minimum_structural_score_to_stable") or 8
    )
    stable_threshold = float(routing_policy.get("score_to_stable") or 20)
    reasons: list[str] = []
    if requested_profile != "auto":
        if requested_profile not in execution_adapters(config):
            raise ValueError(f"Unknown execution profile: {requested_profile}")
        selected = requested_profile
        reasons.append("User explicitly selected the execution profile.")
        selection_mode = "explicit"
    else:
        selected = default_execution_profile(config)
        profiles = execution_adapters(config)
        if selected not in profiles:
            raise ValueError("Default execution profile is not configured.")
        if str(profiles[selected].get("sql_dialect") or "") != "StarRocks":
            raise ValueError(
                "Default execution profile must be StarRocks; select Hive explicitly when the user requests Hive."
            )
        crosses_hive_advisory = (
            score >= minimum_structural_score
            and amplified_score >= stable_threshold
        )
        if crosses_hive_advisory:
            reasons.append(
                f"Configured StarRocks default retained. Complexity {amplified_score:g} reaches the "
                f"Hive advisory threshold {stable_threshold:g}, but diagnostics never change the executor; "
                "Hive requires explicit user selection."
            )
        else:
            reasons.append(
                "Configured StarRocks default selected; Hive requires explicit user selection."
            )
        selection_mode = "auto"
    effective = materialize_profile_config(config, selected)
    profile = execution_adapters(config)[selected]
    logical_window = fixed_time_window(
        start_date,
        end_date,
        effective,
        as_of_date=as_of_date,
        basis="adapter_route",
    )
    time_integrity = time_integrity_plan(
        template,
        effective,
        as_of_date=as_of_date,
        portable_aliases=[row["alias"] for row in token_rows],
        window_override=logical_window,
    )
    blockers: list[str] = []
    if (
        time_integrity.get("actual_range_required")
        and (time_integrity.get("actual_range_output") or {}).get("status")
        != "observable"
    ):
        blockers.append(
            "A query window that includes today must expose an observed date/time field or both "
            "`实际数据开始时间` and `实际数据结束时间`; requested params are not evidence."
        )
    return {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "status": "blocked" if blockers else "ready",
        "selection_mode": selection_mode,
        "selected_profile": selected,
        "routing_role": str(profile.get("routing_role") or ""),
        "sql_dialect": str(effective.get("sql_dialect") or ""),
        "query_engine": str(effective.get("query_engine") or ""),
        "database": str(profile.get("database") or ""),
        "partition_policy": copy.deepcopy(effective.get("partition_policy") or {}),
        "logical_window": {
            "start_date": start_date,
            "end_date": end_date,
            "scan_days": scan_days,
            "as_of_date": logical_window.get("as_of_date", ""),
            "today_included": logical_window.get("today_included", False),
        },
        "time_integrity": time_integrity,
        "source_tlogs": token_rows,
        "passthrough_tables": sorted(passthrough_tables(config)),
        "routing_reasons": reasons,
        "complexity": {**complexity, "score": score},
        "scan_assessment": {
            "model": "complexity_data_amplification_v2",
            "primary_signal": "structural_complexity_x_data_amplification",
            "structural_score": score,
            "density_multiplier": round(density_multiplier, 6),
            "density_basis": "maximum_source_density",
            "date_multiplier": round(date_multiplier, 6),
            "date_amplifier": {
                "base_days": base_days,
                "exponent": exponent,
                "max_multiplier": max_date_multiplier,
            },
            "amplified_complexity_score": amplified_score,
            "minimum_structural_score_to_stable": minimum_structural_score,
            "score_to_stable": stable_threshold,
            "complexity_role": "diagnostic_only",
            "date_role": "diagnostic_multiplier_only",
            "route_decision": "configured_starrocks_default_or_explicit_profile",
            "hive_requires_explicit_selection": True,
            "crosses_hive_advisory": bool(
                score >= minimum_structural_score
                and amplified_score >= stable_threshold
            ),
            "sources": source_scan_rows,
        },
        "volume_evidence": {
            "usage": str(reference.get("usage") or "not_available"),
            "absolute_count_usable": bool(reference.get("absolute_count_usable", False)),
            "max_density_band": max_band,
            "matched_logs": evidence_rows,
            "reference_path": str(
                ((config.get("execution_adapters") or {}).get("volume_reference") or "")
            ),
        },
        "blockers": blockers,
    }


def _partition_literals(profile: dict[str, Any], start_date: str, end_date: str) -> tuple[str, str]:
    policy = profile.get("partition_policy") if isinstance(profile.get("partition_policy"), dict) else {}
    value_format = str(policy.get("partition_format") or "").lower()
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if value_format == "yyyymmddhh":
        return start.strftime("%Y%m%d00"), end.strftime("%Y%m%d23")
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def render_portable_sql(template: str, config: dict[str, Any], route: dict[str, Any]) -> str:
    if route.get("status") != "ready":
        raise ValueError("Execution route is not ready.")
    profile_id = str(route.get("selected_profile") or "")
    profile = execution_adapters(config).get(profile_id)
    if not profile:
        raise ValueError(f"Execution profile is missing: {profile_id}")
    logical = route.get("logical_window") or {}
    start_date = str(logical.get("start_date") or "")
    end_date = str(logical.get("end_date") or "")
    pt_start, pt_end = _partition_literals(profile, start_date, end_date)
    policy = profile.get("partition_policy") if isinstance(profile.get("partition_policy"), dict) else {}
    partition_field = str(policy.get("partition_field") or "").strip()
    business_time_field = str(policy.get("business_time_field") or "").strip()
    effective_config = materialize_profile_config(config, profile_id)
    integrity_plan = route.get("time_integrity")
    if not isinstance(integrity_plan, dict):
        integrity_plan = time_integrity_plan(
            template,
            effective_config,
            as_of_date=str(logical.get("as_of_date") or "") or None,
            portable_aliases=[match.group(2) for match in TLOG_TOKEN_RE.finditer(template)],
            window_override=fixed_time_window(
                start_date,
                end_date,
                effective_config,
                as_of_date=str(logical.get("as_of_date") or "") or None,
                basis="adapter_route",
            ),
        )
    explicit_integrity_aliases = {
        match.group(1).casefold() for match in INTEGRITY_FILTER_TOKEN_RE.finditer(template)
    }
    pattern = profile_table_pattern(config, profile)
    aliases: set[str] = set()

    def table_replacement(match: re.Match[str]) -> str:
        log_name, alias = match.groups()
        aliases.add(alias.lower())
        return pattern.format(log_lower=log_name.lower(), log_name=log_name, log_upper=log_name.upper()) + f" AS {alias}"

    rendered = TLOG_TOKEN_RE.sub(table_replacement, template)

    def time_replacement(match: re.Match[str]) -> str:
        alias = match.group(1)
        if alias.lower() not in aliases:
            raise ValueError(f"Time filter alias has no matching TLOG token: {alias}")
        predicate = (
            f"{alias}.{partition_field} >= (SELECT pt_start FROM params)\n"
            f"      AND {alias}.{partition_field} <= (SELECT pt_end FROM params)"
        )
        if (
            integrity_plan.get("apply_match_filter")
            and alias.casefold() not in explicit_integrity_aliases
        ):
            predicate += "\n      AND " + time_integrity_match_expression(
                alias,
                effective_config,
            )
        return predicate

    rendered = TIME_FILTER_TOKEN_RE.sub(time_replacement, rendered)

    def detail_replacement(match: re.Match[str]) -> str:
        alias = match.group(1)
        if not business_time_field:
            raise ValueError(f"Execution profile {profile_id} has no business_time_field.")
        return (
            f"{alias}.{business_time_field} >= (SELECT ts_start FROM params)\n"
            f"      AND {alias}.{business_time_field} <= (SELECT ts_end FROM params)"
        )

    rendered = DETAIL_FILTER_TOKEN_RE.sub(detail_replacement, rendered)

    def integrity_replacement(match: re.Match[str]) -> str:
        alias = match.group(1)
        if alias.lower() not in aliases:
            raise ValueError(f"Time-integrity filter alias has no matching TLOG token: {alias}")
        return time_integrity_match_expression(alias, effective_config)

    rendered = INTEGRITY_FILTER_TOKEN_RE.sub(integrity_replacement, rendered)
    replacements = {
        "{{PT_START}}": pt_start,
        "{{PT_END}}": pt_end,
        "{{TS_START}}": f"{start_date} 00:00:00",
        "{{TS_END}}": f"{end_date} 23:59:59",
        "{{PARTITION_FIELD}}": partition_field,
        "{{EXECUTION_PROFILE}}": profile_id,
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    rendered = quote_required_identifiers(rendered, effective_config)
    identifier_findings = identifier_policy_findings(rendered, effective_config)
    if identifier_findings:
        raise ValueError(
            "Identifier policy failed after rendering: "
            + "; ".join(str(item.get("message") or "") for item in identifier_findings)
        )
    unresolved = sorted(set(UNRESOLVED_TOKEN_RE.findall(rendered)))
    if unresolved:
        raise ValueError("Unresolved portable SQL tokens: " + ", ".join(unresolved))
    time_contract = analyze_time_integrity_contract(
        rendered,
        effective_config,
        as_of_date=str(logical.get("as_of_date") or "") or None,
    )
    time_blockers = [
        str(item.get("message") or "Time-integrity contract is not satisfied.")
        for item in time_contract.get("findings", []) or []
        if item.get("severity") == "blocker"
    ]
    if time_blockers:
        raise ValueError(
            "Time-integrity contract failed after rendering: "
            + "; ".join(time_blockers)
        )
    return rendered.rstrip() + "\n"


def _safe_output_path(root: Path, value: str, template_path: Path, profile_id: str) -> Path:
    if value:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("--out must be a project-relative path without parent traversal.")
    else:
        relative = Path("query_workspace") / "_working" / "execution_adapter" / f"{template_path.stem}.{profile_id}.sql"
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Rendered SQL output must stay inside the project root.") from exc
    return resolved


def _project_relative_optional(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["route", "render"]:
        item = sub.add_parser(name)
        item.add_argument("--root", required=True)
        item.add_argument("--template-sql", required=True)
        item.add_argument("--start-date", required=True)
        item.add_argument("--end-date", required=True)
        item.add_argument(
            "--as-of-date",
            default="",
            help="Optional YYYY-MM-DD override used only to classify whether the fixed window includes today.",
        )
        item.add_argument("--profile", default="auto")
        item.add_argument("--logical-revision-id", default="")
        item.add_argument("--variant-group-id", default="")
        item.add_argument("--recommended-variant", action="store_true")
        item.add_argument("--format", choices=["json", "text"], default="json")
        if name == "render":
            item.add_argument("--out", default="")
        add_function_gate_arguments(item, selection_help="Allowed route: QUERY or SQL_FORMALIZE.")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--root", required=True)
    inspect.add_argument("--sql-file", required=True)
    inspect.add_argument("--format", choices=["json", "text"], default="json")
    add_function_gate_arguments(inspect, selection_help="Allowed route: QUERY, SQL_FORMALIZE, REVIEW, or PROJECT_ADMIN.")
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("sql_execution_adapter.py", args.command),
            purpose="SQL execution adapter routing",
        )
        if args.command == "render":
            require_user_request(args.user_request, purpose="render project-local executable SQL")
    except Exception as exc:  # pragma: no cover
        exit_with_gate_error(parser, exc)
    root = Path(args.root).resolve()
    config = read_json(root / "project_config.json", {})
    if args.command == "inspect":
        sql = Path(args.sql_file).resolve().read_text(encoding="utf-8-sig")
        payload = execution_route_for_sql(sql, config)
    else:
        template_path = Path(args.template_sql).resolve()
        template = template_path.read_text(encoding="utf-8-sig")
        payload = plan_template_route(
            template,
            config,
            start_date=args.start_date,
            end_date=args.end_date,
            requested_profile=args.profile,
            root=root,
            as_of_date=args.as_of_date or None,
        )
        variant_identity = build_variant_identity(
            logical_revision_id=args.logical_revision_id,
            variant_group_id=args.variant_group_id,
            variant_key=engine_key(payload) if (args.logical_revision_id or args.variant_group_id or args.recommended_variant) else "",
            recommended=args.recommended_variant,
        )
        if variant_identity:
            payload["execution_variant_identity"] = variant_identity
        if args.command == "render":
            sql = stamp_sql_generation(root, render_portable_sql(template, config, payload))
            out_path = _safe_output_path(root, args.out, template_path, payload["selected_profile"])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(sql, encoding="utf-8", newline="\n")
            effective = materialize_profile_config(config, payload["selected_profile"])
            payload["time_contract"] = analyze_time_contract(sql, effective)
            payload["sql_fingerprint"] = route_sql_fingerprint(sql)
            payload["config_fingerprint"] = route_config_fingerprint(config)
            payload["rendered_sql"] = out_path.relative_to(root).as_posix()
            payload["rendered_sql_sha256"] = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            payload["template_contract"] = TEMPLATE_CONTRACT_VERSION
            payload["portable_template_path"] = _project_relative_optional(root, template_path)
            receipt_path = route_receipt_path(out_path)
            receipt_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            payload["route_receipt"] = receipt_path.relative_to(root).as_posix()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"status: {payload.get('status')}")
        print(f"profile: {payload.get('selected_profile') or 'passthrough'}")
        for reason in payload.get("routing_reasons", []):
            print(f"reason: {reason}")
        if payload.get("rendered_sql"):
            print(f"rendered_sql: {payload['rendered_sql']}")
    if payload.get("status") != "ready":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
