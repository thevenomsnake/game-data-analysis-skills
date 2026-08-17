#!/usr/bin/env python3
"""Manage local SQL engineering project artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_explicit_rule_write_authorization,
    require_user_request,
    require_user_function_selection,
)
from asset_provenance import (
    apply_generation_provenance,
    merge_generation_provenance,
    stamp_sql_generation,
)
from spec_utils import (
    SPEC_STORAGE,
    build_short_header,
    has_full_spec_block,
    read_json_object,
    replace_or_prepend_short_header,
    set_spec_version,
    write_json_object,
)
from sql_time_contract import (
    analyze_time_contract,
    literal_value,
    params_cte_expressions,
    time_contract_problem_messages,
)
from capability_registry import command_function_ids, command_routes
from query_window import DEFAULT_TIMEZONE_OFFSET, validate_default_query_window
from temporary_rule_override import (
    build_temporary_rule_override,
    request_authorizes_temporary_override,
)
from rule_stage_policy import (
    LIFECYCLE_STAGES,
    constraint_applies_to_stage,
    normalize_lifecycle_stage,
    partition_constraints_for_stage,
)
from sql_facts import (
    analyze_sql_file,
    execution_fingerprint,
    extract_fields,
    extract_final_select_list,
    extract_partition_fields,
    extract_tables,
    extract_target_tables,
    final_select_exposes_raw_ids,
    infer_analysis_type,
    infer_business_category,
    infer_grain,
    infer_tags,
    infer_time_grain,
    is_metric_expression,
    is_tlog_source_table,
    parse_select_expression,
    sql_side_privacy_transforms,
    split_top_level_csv,
    strip_sql_comments,
    top_level_keyword_positions,
)
from sql_query_workspace import (
    INDEX_HTML_REL as QUERY_WORKSPACE_HTML_REL,
    INDEX_REL as QUERY_WORKSPACE_INDEX_REL,
    ensure_workspace as ensure_query_workspace,
    find_query_reference as find_query_workspace_reference,
    load_index as load_query_workspace_index,
    mark_promoted as mark_query_workspace_promoted,
    origin_contract as query_workspace_origin_contract,
)
from formal_asset_repository import (
    FormalAssetRepositoryError,
    apply_plan as apply_formal_asset_plan,
    list_packages as list_formal_asset_packages,
    load_package as load_formal_asset_package,
    plan_package as plan_formal_asset_package,
)
from sql_execution_adapter import (
    adapter_config_problems,
    default_execution_profile,
    effective_config_for_context,
    effective_config_for_sql,
    effective_config_from_route,
    execution_route_for_file,
    execution_route_for_sql,
    materialize_profile_config,
    route_config_fingerprint,
    route_matches_context,
    route_sql_fingerprint,
)
from sql_identifier_policy import config_problems as identifier_policy_config_problems
from sql_time_contract import time_integrity_config_problems
from sql_identifier_policy import policy_findings as identifier_policy_findings
from subject_identity import validate_subject_identity_policy
from result_evidence_retention import prepare_result_evidence, write_retained_result
from project_rules import has_v2_store, load_rules, select_rule_records
from rule_store import (
    INDEX_RELATIVE_PATH as RULE_INDEX_RELATIVE_PATH,
    STORE_RELATIVE_PATH as RULE_STORE_RELATIVE_PATH,
    RuleStore,
    activation_contract_source,
    activation_policy,
    initialize_empty_store,
    request_signal_evidence,
    request_signature_matches,
)
from rule_application import (
    application_class,
    build_inheritance_contract,
    build_request_envelope,
    build_rule_application,
    inherited_rule_references,
    rule_reference,
)


ARTIFACT_DIRS = {
    "QUERY": "query_sql",
    "DASHBOARD": "dashboard_sql",
    "VALIDATION": "validations",
}
ARTIFACT_KINDS = ["QUERY", "DASHBOARD", "VALIDATION"]
PROJECT_MANIFEST_SCHEMA_VERSION = "project_manifest_v2"
FORMAL_ASSET_ROOT_REL = Path("formal_assets")
FORMAL_ASSET_INDEX_REL = FORMAL_ASSET_ROOT_REL / "index.json"
FORMAL_ASSET_MIGRATION_MAP_REL = FORMAL_ASSET_ROOT_REL / "migration-map.v1.json"
FORMAL_MEMBER_PREFIX = {
    "QUERY": "query",
    "DASHBOARD": "dashboard",
    "VALIDATION": "validation",
}
FORMAL_SQL_ROLES = {
    "QUERY": frozenset({"formal_query", "formal_query_unverified", "formal_query_sql", "query_sql"}),
    "DASHBOARD": frozenset({"dashboard_delivery_sql", "dashboard_sql"}),
    "VALIDATION": frozenset({"validation_sql"}),
}
FORMAL_SPEC_ROLES = {
    "QUERY": frozenset({"formal_query_spec", "query_spec"}),
    "DASHBOARD": frozenset({"dashboard_delivery_spec", "dashboard_spec"}),
    "VALIDATION": frozenset({"validation_spec"}),
}
FORMAL_META_ROLES = {
    "QUERY": frozenset({"formal_query_meta", "query_meta"}),
    "DASHBOARD": frozenset({"dashboard_delivery_meta", "dashboard_meta"}),
    "VALIDATION": frozenset({"validation_meta"}),
}
LEGACY_FORMAL_DIRS = frozenset({"query_sql", "dashboard_sql", "validations", "runs", "archive"})
INTERMEDIATE_TABLE_DIR = "intermediate_tables"
PROJECT_CONFIG_FILE = "project_config.json"
PROJECT_INDEX_FILE = "index.md"
PROJECT_INDEX_MANIFEST_RE = re.compile(r"^- Manifest SHA-256: `([0-9a-f]{64})`$", re.M)
RULE_CONCEPT_REGISTRY_REL = Path("_rule_review") / "rule_concepts.json"
SOURCE_TITLE_PREFIX_RE = re.compile(r"^(?:\s*\d{1,4}\s*[\.\、．,，\)）\]】]\s*)+")
SOURCE_EXTENSION_RE = re.compile(r"\.(?:sql|csv|txt|xlsx)\s*$", re.I)


DEFAULT_BUSINESS_CATEGORY = "uncategorized"
DEFAULT_ANALYSIS_TYPE = "unspecified"
RULE_STATUSES = ["confirmed", "proposed", "superseded", "deprecated"]
RULE_CONTEXT_STOP_TERMS = {
    "sql",
    "query",
    "review",
    "validation",
    "dashboard",
    "看板",
    "报表",
    "统计",
    "数据",
    "指标",
    "生成",
    "创建",
    "rm",
    "obt",
    "cbt3",
    "abtest",
    "demo_analytics",
    "demo_experiment",
    "demo_abtest",
    "example-obt",
    "example-cbt3",
    "example-abtest",
}
RULE_CONTEXT_WEAK_TERMS = {
    "人",
    "人数",
    "人次",
    "玩家",
    "用户",
    "角色",
    "参与",
    "参与人数",
    "参与玩家",
    "参与玩家数",
    "去重",
    "去重人数",
    "去重玩家",
    "同粒度",
    "粒度",
    "数量",
    "次数",
    "时长",
    "占比",
    "比例",
    "明细",
    "分布",
    "趋势",
    "汇总",
    "vopenid",
    "roleid",
    "openid",
    "dteventtime",
    "dteventdate",
    "dtEventTime".lower(),
    "izoneareaid",
    "gamemode",
}
BUSINESS_CONCEPT_ALIASES = {
    "new_user": [
        "新增用户",
        "新增玩家",
        "新增人数",
        "新增日期",
        "新进用户",
        "新进玩家",
        "新进人数",
        "注册用户",
        "注册玩家",
        "玩家注册",
        "首登",
    ],
}
RULE_CONTEXT_PHRASE_TERMS = [
    "MatchEnd",
    "MatchSucess",
    "MatchSuccess",
    "ClientMatchClickTime",
    "MatchBeginTime",
    "MatchDuration",
    "BattleLogInOut",
    "BattleMission",
    "BattleDeathResurrection",
    "PlayerLogin",
    "PlayerRegister",
    "PlayerLogout",
    "RoomMatch",
    "MatchBegin",
    "Territory",
    "Damage",
    "匹配等待时长",
    "匹配耗时",
    "匹配阶段",
    "匹配结果",
    "假匹配",
    "客户端匹配",
    "服务端匹配",
    "取消匹配",
    "匹配成功",
    "匹配失败",
    "抄家",
    "被抄家",
    "遇袭",
    "被遇袭",
    "领地攻击",
    "被击杀",
    "击杀",
    "留存",
    "活跃",
    *BUSINESS_CONCEPT_ALIASES["new_user"],
    "模式PCU",
    "模式DAU",
    "累计非挂机时长",
]
RULE_CONTEXT_WEAK_ONLY_MAX_SCORE = 3
RULE_CONTEXT_SOURCE_GATED_DOMAINS = {
    "matching",
    "territory",
    "damage",
    "battle",
    "mission",
    "pcu",
    "rank",
    "mode_attribution",
}
REVERSE_AUDIT_GENERIC_FIELDS = {
    "vopenid",
    "openid",
    "roleid",
    "izoneareaid",
    "gamesvrid",
    "battlesrvid",
    "dteventtime",
    "dteventdate",
}
REVERSE_AUDIT_SHARED_LOGS = {
    "battleitem",
    "battleloginout",
    "damage",
}
BOUNDARY_ONLY_EVENT_SIGNATURE_POLICIES = {
    "boundaryonly",
    "partialonly",
    "diagnosticonly",
}
ID_RANGE_FIELD_CATEGORIES = {
    "battleitemchangesourceid": "item_source_id",
    "battleitemid": "item_id",
    "battleitemtemplateid": "item_id",
    "itemtemplateid": "item_id",
    "itemid": "item_id",
    "templateid": "item_id",
    "rewardid": "item_id",
    "propid": "item_id",
    "gamemode": "mode_id",
    "modeid": "mode_id",
    "matchmode": "mode_id",
    "izoneareaid": "zone_id",
    "zoneid": "zone_id",
    "zone_area_id": "zone_id",
    "gamesvrid": "game_server_id",
    "battlesrvid": "battle_server_id",
    "battlemissionid": "mission_id",
    "battlemissionsubid": "mission_id",
    "missionid": "mission_id",
    "missionsubid": "mission_id",
    "totalactiveduration": "duration_range",
    "onlinetime": "duration_range",
    "matchduration": "duration_range",
    "battlelogduration": "duration_range",
    "battlemissionbattleduration": "duration_range",
    "dteventtime": "time_range",
    "dteventdate": "time_range",
    "tdbank_imp_date": "partition_range",
}
ID_RANGE_OPERATORS = {"=", "IN", ">=", "<=", ">", "<"}
RANGE_OPERATORS = {">=", "<=", ">", "<"}
INTENT_LOG_NAMES = [
    "MatchEnd",
    "MatchBegin",
    "RoomMatch",
    "BattleLogInOut",
    "BattleLoginOut",
    "BattleMission",
    "PlayerLogin",
    "PlayerLogout",
    "Territory",
    "Damage",
    "OnlineCnt",
    "PlayerRegister",
]
INTENT_FIELD_NAMES = [
    "MatchSucess",
    "MatchSuccess",
    "ClientMatchClickTime",
    "MatchBeginTime",
    "MatchDuration",
    "vOpenID",
    "OpenID",
    "RoleID",
    "iZoneAreaID",
    "GameSvrId",
    "GameMode",
    "MatchMode",
    "BattleSrvId",
    "BattleTeamId",
    "BattleLogDuration",
    "TotalActiveDuration",
    "BattleMissionId",
    "BattleMissionComplete",
    "BattleMissionBattleDuration",
    "TerritoryOwnerID",
    "TerritoyOwnerTeamID",
    "TerritotyDamageSourceEntityType",
    "TerritoyDamageSourceRoleID",
    "TerritoryReasonForChange",
    "TerritoyConstructionID",
    "DamageTargetRoleID",
    "DamageSourceRoleID",
    "DamageTargetVRoleID",
    "DamageSourceVRoleID",
    "DamageTargetDead",
    "KillSourceVRoleID",
    "DamageSourceEntityType",
    "BattleItemChangeType",
    "BattleItemFlowType",
    "BattleItemChangeSource",
    "BattleItemDelta",
    "BattleItemChangeSourceId",
    "DeathTime",
    "dtEventTime",
    "dteventtime",
    "dteventdate",
]
INTENT_DOMAIN_SIGNALS = {
    "matching": [
        "matchend",
        "matchbegin",
        "roommatch",
        "matchsucess",
        "matchsuccess",
        "clientmatchclicktime",
        "matchbegintime",
        "matchduration",
        "匹配等待",
        "匹配耗时",
        "匹配阶段",
        "匹配结果",
        "匹配看板",
        "匹配分析",
        "假匹配",
        "服务端匹配",
    ],
    "territory": [
        "territory",
        "territoryownerid",
        "territotyconstructionid",
        "territoryreasonforchange",
        "territoydamagesourceroleid",
        "领地",
        "遇袭",
        "被遇袭",
        "抄家",
        "被抄家",
    ],
    "damage": [
        "damage",
        "damagetargetroleid",
        "damagesourceroleid",
        "击杀",
        "被击杀",
        "伤害",
    ],
    "retention": [
        "retention",
        "cohort",
        "留存",
        "次留",
        "三留",
        "七留",
        "活跃",
        "日活",
    ],
    "new_user": [
        *BUSINESS_CONCEPT_ALIASES["new_user"],
        "first_login",
        "cohort_date",
    ],
    "battle": [
        "battleloginout",
        "battlelogduration",
        "totalactive",
        "模式dau",
        "模式日活",
        "模式活跃",
        "战斗",
        "局内",
    ],
    "mission": [
        "battlemission",
        "battlemissionid",
        "battlemissioncomplete",
        "任务",
        "每日任务",
        "周任务",
    ],
    "rank": ["rank", "段位", "赛季段位"],
    "pcu": ["pcu", "并发", "在线峰值", "onlinecnt"],
}
INTENT_METRIC_FAMILY_SIGNALS = {
    "dedup_user_count": [
        "参与人数",
        "参与玩家数",
        "去重玩家",
        "去重人数",
        "用户数",
        "玩家数",
        "人数",
        "count(distinct",
        "vopenid",
    ],
    "duration": [
        "duration",
        "time",
        "时长",
        "耗时",
        "等待",
        "间隔",
        "tot活",
        "totalactiveduration",
        "battlelogduration",
        "matchduration",
    ],
    "rate": ["rate", "ratio", "占比", "比例", "率"],
    "bucket": ["bucket", "分桶", "区间", "分布"],
    "detail": ["detail", "明细", "列表", "清单"],
    "count": ["count(", "次数", "事件数", "总数"],
}
INTENT_GRAIN_SIGNALS = {
    "user": ["玩家", "用户", "vopenid", "openid", "roleid", "角色"],
    "event": ["事件", "次数", "日志", "matchend", "territory", "battlemission"],
    "day": ["日期", "天", "日", "dteventdate", "stat_date", "自然日"],
    "mode": ["模式", "gamemode", "matchmode"],
    "server": ["区服", "服务器", "战斗服", "izoneareaid", "gamesvrid", "battlesrvid"],
}
CONCEPT_DOMAIN_HINTS = {
    "match": "matching",
    "matching": "matching",
    "territory": "territory",
    "raid": "territory",
    "damage": "damage",
    "kill": "damage",
    "retention": "retention",
    "active": "retention",
    "battle": "battle",
    "mission": "mission",
    "rank": "rank",
    "pcu": "pcu",
    "dau": "retention",
}
ARTIFACT_STATES = ["current", "history", "archived"]
CHANGE_TYPES = [
    "auto",
    "new",
    "clarification",
    "correction",
    "replacement",
    "superset",
    "branch",
    "promotion",
    "refresh",
]
REPLACEMENT_CHANGE_TYPES = {"clarification", "correction", "replacement", "superset", "refresh"}
TABLE_STATES = ["current", "history", "archived"]
TABLE_CHANGE_TYPES = [
    "auto",
    "new",
    "clarification",
    "correction",
    "replacement",
    "schema_change",
    "partition_change",
    "dependency_change",
    "refresh_change",
    "branch",
]
TABLE_REPLACEMENT_CHANGE_TYPES = {
    "clarification",
    "correction",
    "replacement",
    "schema_change",
    "partition_change",
    "dependency_change",
    "refresh_change",
}
TABLE_TYPES = ["intermediate", "snapshot", "lookup", "mart", "temp"]
MATERIALIZATIONS = ["physical_table", "partitioned_table", "view", "temp_table", "cte"]
TABLE_LIFECYCLES = ["session", "artifact", "project", "persistent"]
REFRESH_MODES = ["manual", "ad_hoc", "hourly", "daily", "partitioned", "scheduled"]
TABLE_AVAILABILITY_STATUSES = ["unknown", "available", "unavailable"]
TABLE_AVAILABILITY_SOURCES = ["not_checked", "user_declared", "detected", "validation", "manual_review"]
TABLE_SOURCE_CONTRACT_MODES = ["dual_path", "intermediate_preferred", "intermediate_only", "raw_logs_only"]
RUN_STATUSES = ["observed", "passed", "warning", "failed", "blocked", "skipped", "proxy_verified"]
VERIFICATION_STATUSES = ["not_applicable", "verified", "unverified_skipped_run", "proxy_verified"]
RESULT_FILE_EXTENSIONS = {".csv", ".xlsx"}
QUERY_ZONE_PARAM_ALIASES = {
    "zone_id",
    "zone_ids",
    "zone_area_id",
    "zone_area_ids",
    "izoneareaid",
    "game_svr_id",
    "game_svr_ids",
    "gamesvrid",
}
QUERY_TIME_PARAM_ALIASES = {"ts_start", "ts_end"}
QUERY_PARTITION_PARAM_ALIASES = {"pt_start", "pt_end"}
SUPPORTED_DIALECTS = {
    "hive": "Hive",
    "starrocks": "StarRocks",
}
TABLE_NAMING_PROFILES = {
    "demo_abtest_hive": {
        "name": "demo_abtest_hive",
        "dialect": "Hive",
        "database": "demo_warehouse",
        "pattern": "demo_warehouse.demo_dsl_{log_lower}_fht0",
        "description": "DEMO-AB_TEST Hive/TDBank TLOG naming profile.",
    },
    "demo_hive": {
        "name": "demo_hive",
        "dialect": "Hive",
        "database": "demo_log",
        "pattern": "demo_log.demo_dsl_{log_lower}_fht0",
        "description": "Demo Hive event-time TLOG naming profile.",
    },
    "demo_starrocks": {
        "name": "demo_starrocks",
        "dialect": "StarRocks",
        "database": "demo_log",
        "pattern": "demo_log.demo_dsl_{log_lower}_fht0",
        "description": "Demo StarRocks TLOG naming profile.",
    },
}
DEFAULT_PARTITION_POLICIES = {
    "Hive": {
        "name": "tdbank_hourly",
        "required_for_tlog": True,
        "partition_field": "tdbank_imp_date",
        "partition_format": "YYYYMMDDHH",
        "business_time_field": "dtEventTime",
        "business_time_required": True,
        "strict_generation": True,
    },
    "StarRocks": {
        "name": "starrocks_event_time",
        "required_for_tlog": False,
        "partition_field": "",
        "partition_format": "",
        "business_time_field": "dteventdate",
        "business_time_required": True,
        "strict_generation": True,
        "requires_schema_confirmation": True,
    },
}
DEFAULT_HIVE_IDENTIFIER_POLICY = {
    "quote_style": "backtick",
    "case_sensitive_fields": ["dtEventTime"],
}
RMCN_LOG_PARTITION_POLICY = {
    "name": "demo_log_dt_event_date",
    "required_for_tlog": True,
    "partition_field": "dtEventDate",
    "partition_format": "date_or_datetime",
    "partition_bounds": "inclusive",
    "whole_day_filter_mode": "partition_only",
    "business_time_field": "dtEventTime",
    "business_time_required": False,
    "business_time_required_when": "detailed_time_logic",
    "detail_time_bounds": "inclusive",
    "strict_generation": True,
}
RMCN_LOG_STARROCKS_PARTITION_POLICY = {
    **RMCN_LOG_PARTITION_POLICY,
    "requires_schema_confirmation": True,
}
TABLE_PROFILE_PARTITION_POLICIES = {
    "demo_abtest_hive": DEFAULT_PARTITION_POLICIES["Hive"],
    "demo_hive": RMCN_LOG_PARTITION_POLICY,
    "demo_starrocks": RMCN_LOG_STARROCKS_PARTITION_POLICY,
}
DEFAULT_TABLE_OVERRIDES = {
    "demo_hive": {
        "PlayerLogin": "demo_log.demo_dsl_playerlogin_fht0",
    },
    "demo_starrocks": {
        "PlayerLogin": "demo_log.demo_dsl_playerlogin_fht0",
    },
    "demo_abtest_hive": {
        "PlayerLogin": "demo_warehouse.demo_dsl_playerlogin_fht0",
    },
}
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def clean_source_title(value: str) -> str:
    return SOURCE_EXTENSION_RE.sub("", str(value or "").strip()).strip()


def strip_source_prefix(value: str) -> str:
    original = clean_source_title(value)
    text = original
    while text:
        next_text = SOURCE_TITLE_PREFIX_RE.sub("", text).strip()
        if next_text == text:
            break
        text = next_text
    return text or original


def slugify(value: str, fallback: str = "artifact") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if slug:
        return slug
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8] if value else "empty"
    return f"{fallback}-{digest}"


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def csv_or_inferred(value: str | None, inferred: list[str]) -> list[str]:
    explicit = parse_csv(value)
    return explicit or inferred


def text_or_inferred(value: str | None, inferred: str) -> str:
    return value if value else inferred


def unique_in_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        cleaned = value.strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def listify(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def contract_list_values(source: dict, *keys: str) -> list:
    values: list = []
    for key in keys:
        values.extend(listify(source.get(key)))
    return values


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text_if_missing(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def formal_asset_index(root: Path) -> dict:
    index = read_json(root / FORMAL_ASSET_INDEX_REL, {})
    if index.get("schema_version") != "formal_asset_repository_index_v1":
        raise SystemExit(
            f"Formal Asset Repository index is missing or invalid: {root / FORMAL_ASSET_INDEX_REL}"
        )
    if not isinstance(index.get("packages"), list):
        raise SystemExit("Formal Asset Repository index packages must be an array.")
    return index


def initialize_formal_asset_repository(root: Path, project_id: str) -> dict:
    index_path = root / FORMAL_ASSET_INDEX_REL
    if index_path.is_file():
        index = formal_asset_index(root)
        existing_project = str(index.get("project_id") or "")
        if existing_project and existing_project != project_id:
            raise SystemExit(
                f"Formal Asset Repository belongs to project_id={existing_project}, not {project_id}."
            )
        return index
    index = {
        "schema_version": "formal_asset_repository_index_v1",
        "project_id": project_id,
        "updated_at": now_iso(),
        "packages": [],
    }
    _write_json_atomic(index_path, index)
    return index


def compact_project_manifest(root: Path, manifest: dict | None = None) -> dict:
    current = dict(manifest if manifest is not None else read_json(manifest_path(root), {}))
    index = formal_asset_index(root)
    for key in (
        "artifacts",
        "artifact_counters",
        "run_evidence",
        "query_workspace_index",
        "query_workspace_view",
    ):
        current.pop(key, None)
    current.update(
        {
            "schema_version": PROJECT_MANIFEST_SCHEMA_VERSION,
            "updated_at": now_iso(),
            "formal_asset_repository": {
                "index": FORMAL_ASSET_INDEX_REL.as_posix(),
                "migration_map": FORMAL_ASSET_MIGRATION_MAP_REL.as_posix(),
                "package_count": len(index.get("packages", [])),
            },
            "packages": index.get("packages", []),
        }
    )
    return current


def sync_compact_project_manifest(root: Path, manifest: dict | None = None) -> dict:
    compact = compact_project_manifest(root, manifest)
    _write_json_atomic(manifest_path(root), compact)
    return compact


def _reject_legacy_archive_path(root: Path, path: Path, *, label: str) -> None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return
    if relative.parts and relative.parts[0].lower() == "archive":
        raise SystemExit(
            f"{label} cannot read from the legacy archive directory. "
            "Resolve it through formal_asset_migration.py first."
        )


def _package_member(manifest: dict, reference: str, *, roles: set[str] | None = None) -> dict:
    normalized = str(reference or "").replace("\\", "/")
    matches = [
        item
        for item in manifest.get("members", [])
        if isinstance(item, dict)
        and (item.get("member_id") == normalized or item.get("path") == normalized)
        and (roles is None or item.get("role") in roles)
    ]
    if not matches:
        role_text = f" with role in {sorted(roles)}" if roles else ""
        raise SystemExit(f"Package member not found{role_text}: {reference}")
    if len(matches) > 1:
        raise SystemExit(f"Package member reference is ambiguous: {reference}")
    return matches[0]


def _package_member_json(root: Path, member: dict) -> dict:
    path = root / Path(str(member.get("path") or ""))
    payload = read_json(path, {})
    if not payload:
        raise SystemExit(f"Package member is not a readable JSON object: {member.get('path')}")
    return payload


def _formal_run_status(root: Path, package: dict, reference: str) -> str:
    member = _package_member(package, reference, roles={"run_meta", "run_record"})
    if member.get("role") == "run_meta":
        return str(_package_member_json(root, member).get("status") or "")
    path = root / Path(str(member.get("path") or ""))
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("- status:"):
                return line.split(":", 1)[1].strip()
    return ""


def _load_package_context(root: Path, package_id: str) -> dict:
    try:
        return load_formal_asset_package(root, package_id)
    except FormalAssetRepositoryError as exc:
        raise SystemExit(str(exc)) from exc


def _resolve_formal_package_context(root: Path, args, kind: str) -> tuple[str | None, dict | None]:
    package_id = str(getattr(args, "package_id", None) or "").strip().upper()
    new_package = bool(getattr(args, "new_package", False))
    if bool(package_id) == new_package:
        raise SystemExit("Formal SQL requires exactly one Package context: --package-id FA-NNNN or --new-package.")
    if new_package:
        if kind != "QUERY":
            raise SystemExit("Only a formal QUERY may start a new Package; attach Validation/Dashboard to --package-id.")
        return None, None
    return package_id, _load_package_context(root, package_id)


def _formal_member_version(package: dict | None, kind: str) -> int:
    roles = FORMAL_SQL_ROLES[kind]
    versions: list[int] = []
    for member in (package or {}).get("members", []):
        if not isinstance(member, dict) or member.get("role") not in roles:
            continue
        match = re.search(r"/v(\d{3})\.sql$", str(member.get("path") or ""))
        if match:
            versions.append(int(match.group(1)))
    return max(versions, default=0) + 1


def _formal_member_updates_for_replacement(package: dict | None, kind: str) -> tuple[list[dict], list[dict]]:
    roles = FORMAL_SQL_ROLES[kind] | FORMAL_SPEC_ROLES[kind] | FORMAL_META_ROLES[kind]
    current_ids = set(((package or {}).get("current") or {}).get("member_ids") or [])
    current_members = [
        item
        for item in (package or {}).get("members", [])
        if isinstance(item, dict) and item.get("member_id") in current_ids and item.get("role") in roles
    ]
    updates = [
        {"member_id": str(item["member_id"]), "lifecycle_state": "history"}
        for item in current_members
    ]
    return updates, current_members


def _formal_member_path(package_directory: str, target_path: str) -> str:
    return f"{package_directory}/members/{target_path}"


def project_config_path(root: Path) -> Path:
    return root / PROJECT_CONFIG_FILE


def rule_concept_registry_path(root: Path) -> Path:
    return root.parent / RULE_CONCEPT_REGISTRY_REL


def registered_concept_keys(root: Path) -> set[str]:
    registry_path = rule_concept_registry_path(root)
    if not registry_path.exists():
        return set()
    registry = read_json(registry_path, {"concepts": []})
    return {
        slugify(str(item.get("concept_key", "")), "concept")
        for item in registry.get("concepts", [])
        if item.get("concept_key")
    }


def concept_key_required(scope: str, lifetime: str) -> bool:
    return scope == "project" and lifetime == "persistent"


def normalize_kind(value: str) -> str:
    key = value.upper()
    if key not in ARTIFACT_DIRS:
        raise SystemExit(f"Unsupported kind: {value}")
    return key


def display_kind(kind: str) -> str:
    return kind


def require_project(root: Path) -> None:
    if not manifest_path(root).exists():
        raise SystemExit(f"Project is not initialized: {root}")


def normalize_dialect(value: str | None) -> str:
    if not value:
        return "missing"
    key = value.strip().lower().replace("_", "").replace("-", "")
    if key in {"hive", "tdbankhive", "tdbank"}:
        return "Hive"
    if key in {"starrocks", "starrock"}:
        return "StarRocks"
    raise SystemExit(f"Unsupported SQL dialect: {value}")


def normalize_table_profile(value: str | None) -> str:
    if not value or value == "missing":
        return "missing"
    key = value.strip()
    if key not in TABLE_NAMING_PROFILES:
        raise SystemExit(
            f"Unsupported table naming profile: {value}. "
            f"Expected one of: {', '.join(sorted(TABLE_NAMING_PROFILES))}"
        )
    return key


def missing_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return missing_value(value.get("name")) or value.get("status") == "missing"
    if isinstance(value, str):
        return value.strip().lower() in {"", "missing", "unknown", "todo", "tbd", "null", "none"}
    return False


def default_partition_policy(dialect: str, table_profile_name: str = "missing") -> dict:
    policy = TABLE_PROFILE_PARTITION_POLICIES.get(table_profile_name) or DEFAULT_PARTITION_POLICIES.get(dialect, {})
    return dict(policy)


def default_identifier_policy(dialect: str) -> dict:
    return dict(DEFAULT_HIVE_IDENTIFIER_POLICY) if dialect == "Hive" else {}


def named_config(name: str | None, *, status: str | None = None, notes: str = "") -> dict:
    value = name or "missing"
    return {
        "name": value,
        "status": status or ("missing" if missing_value(value) else "configured"),
        "notes": notes,
    }


def default_project_config(root: Path, project_name: str, args=None) -> dict:
    project_id = getattr(args, "project_id", None) or root.name
    display_name = getattr(args, "display_name", None) or project_name
    dialect = normalize_dialect(getattr(args, "dialect", None))
    table_profile_name = normalize_table_profile(getattr(args, "table_profile", None))
    if table_profile_name != "missing" and dialect == "missing":
        dialect = TABLE_NAMING_PROFILES[table_profile_name]["dialect"]
    profile = TABLE_NAMING_PROFILES.get(table_profile_name, {})
    project_start_date = getattr(args, "project_start_date", None) or ""
    default_window_mode = getattr(args, "default_query_window_mode", None) or (
        "project_start_to_yesterday" if project_start_date else "missing"
    )
    return {
        "version": 1,
        "project_id": project_id,
        "display_name": display_name,
        "sql_dialect": dialect,
        "query_engine": getattr(args, "query_engine", None) or "missing",
        "query_environment": named_config(getattr(args, "query_environment", None)),
        "dashboard_application": named_config(getattr(args, "dashboard_application", None)),
        "data_services_file": "data_services.json",
        "table_naming_profile": {
            **profile,
            "name": table_profile_name,
            "status": "missing" if table_profile_name == "missing" else "configured",
        },
        "partition_policy": default_partition_policy(dialect, table_profile_name),
        **({"identifier_policy": default_identifier_policy(dialect)} if dialect == "Hive" else {}),
        "default_query_window": {
            "mode": default_window_mode,
            "project_start_date": project_start_date,
            "timezone_offset": getattr(args, "default_query_timezone_offset", None)
            or DEFAULT_TIMEZONE_OFFSET,
            "materialization": "fixed_literals",
        },
        "table_overrides": DEFAULT_TABLE_OVERRIDES.get(table_profile_name, {}).copy(),
        "generation_contract": {
            "strict_dialect_rules": True,
            "require_query_environment_for_query": True,
            "require_dashboard_application_for_dashboard": True,
            "block_formal_sql_when_config_missing": True,
        },
        "updated_at": now_iso(),
    }


def read_project_config(root: Path) -> dict:
    return read_json(project_config_path(root), {})


def write_project_config(root: Path, config: dict) -> None:
    config["updated_at"] = now_iso()
    write_json(project_config_path(root), config)


def apply_config_args(config: dict, args) -> dict:
    if getattr(args, "project_id", None):
        config["project_id"] = args.project_id
    if getattr(args, "display_name", None):
        config["display_name"] = args.display_name
    if getattr(args, "dialect", None):
        config["sql_dialect"] = normalize_dialect(args.dialect)
        profile_name = config.get("table_naming_profile", {}).get("name", "missing")
        config["partition_policy"] = default_partition_policy(config["sql_dialect"], profile_name)
        if config["sql_dialect"] == "Hive":
            config["identifier_policy"] = default_identifier_policy("Hive")
        else:
            config.pop("identifier_policy", None)
    if getattr(args, "query_engine", None):
        config["query_engine"] = args.query_engine
    if getattr(args, "query_environment", None) is not None:
        config["query_environment"] = named_config(args.query_environment)
    if getattr(args, "dashboard_application", None) is not None:
        config["dashboard_application"] = named_config(args.dashboard_application)
    if any(
        getattr(args, field, None) is not None
        for field in ["default_query_window_mode", "project_start_date", "default_query_timezone_offset"]
    ):
        window = config.setdefault(
            "default_query_window",
            {
                "mode": "missing",
                "project_start_date": "",
                "timezone_offset": DEFAULT_TIMEZONE_OFFSET,
                "materialization": "fixed_literals",
            },
        )
        if getattr(args, "default_query_window_mode", None) is not None:
            window["mode"] = args.default_query_window_mode
        if getattr(args, "project_start_date", None) is not None:
            window["project_start_date"] = args.project_start_date
            if getattr(args, "default_query_window_mode", None) is None:
                window["mode"] = "project_start_to_yesterday"
        if getattr(args, "default_query_timezone_offset", None) is not None:
            window["timezone_offset"] = args.default_query_timezone_offset
        if window.get("mode") == "missing":
            window["project_start_date"] = ""
        window["materialization"] = "fixed_literals"
    if getattr(args, "table_profile", None):
        profile_name = normalize_table_profile(args.table_profile)
        profile = TABLE_NAMING_PROFILES[profile_name]
        config["table_naming_profile"] = {**profile, "status": "configured"}
        config["sql_dialect"] = profile["dialect"]
        config["partition_policy"] = default_partition_policy(profile["dialect"], profile_name)
        if profile["dialect"] == "Hive":
            config["identifier_policy"] = default_identifier_policy("Hive")
        else:
            config.pop("identifier_policy", None)
        overrides = config.setdefault("table_overrides", {})
        for log_name, table_name in DEFAULT_TABLE_OVERRIDES.get(profile_name, {}).items():
            overrides.setdefault(log_name, table_name)
    for override in getattr(args, "table_override", None) or []:
        if "=" not in override:
            raise SystemExit("--table-override must use LogName=database.table")
        log_name, table_name = override.split("=", 1)
        if not log_name.strip() or not table_name.strip():
            raise SystemExit("--table-override must use LogName=database.table")
        config.setdefault("table_overrides", {})[log_name.strip()] = table_name.strip()
    contract = config.setdefault("generation_contract", {})
    contract.setdefault("strict_dialect_rules", True)
    contract.setdefault("require_query_environment_for_query", True)
    contract.setdefault("require_dashboard_application_for_dashboard", True)
    contract.setdefault("block_formal_sql_when_config_missing", True)
    return config


def ensure_project_config(root: Path, project_name: str, args=None) -> dict:
    config = read_project_config(root)
    if not config:
        config = default_project_config(root, project_name, args)
    if args is not None:
        config = apply_config_args(config, args)
    write_project_config(root, config)
    return config


def config_display_value(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "missing")
    return str(value or "missing")


def validate_project_config(config: dict, kind: str = "QUERY") -> list[str]:
    problems: list[str] = []
    dialect = config.get("sql_dialect")
    profile = config.get("table_naming_profile", {})
    profile_name = profile.get("name", "") if isinstance(profile, dict) else ""
    policy = config.get("partition_policy", {})
    policy = policy if isinstance(policy, dict) else {}
    if dialect not in {"Hive", "StarRocks"}:
        problems.append("sql_dialect must be configured as Hive or StarRocks.")
    if missing_value(profile) or not profile.get("pattern"):
        problems.append("table_naming_profile with a physical table pattern is required.")
    if missing_value(config.get("query_engine")):
        problems.append("query_engine is required before saving formal SQL.")
    if kind in {"QUERY", "VALIDATION", "DASHBOARD"} and missing_value(config.get("query_environment")):
        problems.append("query_environment is required before saving formal SQL.")
    if kind == "DASHBOARD" and missing_value(config.get("dashboard_application")):
        problems.append("dashboard_application is required before saving dashboard SQL.")
    if not policy or policy.get("strict_generation") is not True:
        problems.append("strict partition/time policy is required for the selected dialect.")
    if profile_name == "demo_hive" and "tdbank" in f"{config.get('query_engine', '')} {config_display_value(config.get('query_environment'))}".lower():
        problems.append("demo_hive is an Demo Hive event-time profile; do not configure it as TDBank.")
    if profile_name == "demo_hive" and policy.get("name") != "demo_log_dt_event_date":
        problems.append("demo_hive must use partition_policy.name=demo_log_dt_event_date.")
    if profile_name == "demo_starrocks" and policy.get("name") != "demo_log_dt_event_date":
        problems.append("demo_starrocks must use partition_policy.name=demo_log_dt_event_date.")
    if profile_name == "demo_abtest_hive" and policy.get("name") != "tdbank_hourly":
        problems.append("demo_abtest_hive must use partition_policy.name=tdbank_hourly.")
    if dialect == "StarRocks" and policy.get("requires_schema_confirmation") is not True:
        problems.append("StarRocks projects must require schema/partition confirmation.")
    if dialect == "StarRocks" and str(policy.get("partition_field", "")).lower() == "tdbank_imp_date":
        problems.append("StarRocks projects must not default to TDBank partition field tdbank_imp_date.")
    if policy.get("business_time_required") is True and missing_value(policy.get("business_time_field")):
        problems.append("business_time_field is required when business_time_required is true.")
    if "default_query_window" in config:
        problems.extend(validate_default_query_window(config))
    problems.extend(adapter_config_problems(config))
    problems.extend(identifier_policy_config_problems(config))
    problems.extend(time_integrity_config_problems(config))
    problems.extend(validate_subject_identity_policy(config))
    return problems


def project_context_snapshot(
    config: dict,
    candidate_sql: str = "",
    *,
    execution_route: dict | None = None,
) -> dict:
    effective = config
    route = {}
    if candidate_sql:
        if route_matches_context(execution_route, candidate_sql, config):
            route = execution_route
            effective = effective_config_from_route(config, route)
        else:
            effective, detection = effective_config_for_sql(config, candidate_sql)
            route = execution_route_for_sql(
                candidate_sql,
                config,
                effective_config=effective,
                detection=detection,
            )
    profile = effective.get("table_naming_profile", {})
    return {
        "project_id": effective.get("project_id", ""),
        "display_name": effective.get("display_name", ""),
        "sql_dialect": effective.get("sql_dialect", "missing"),
        "query_engine": effective.get("query_engine", "missing"),
        "query_environment": config_display_value(effective.get("query_environment")),
        "dashboard_application": config_display_value(effective.get("dashboard_application")),
        "table_naming_profile": profile.get("name", "missing"),
        "partition_policy": effective.get("partition_policy", {}).get("name", "missing"),
        "execution_profile": route.get("selected_profile", ""),
        "execution_route_status": route.get("status", "not_evaluated"),
    }


def resolve_physical_table(config: dict, log_name: str, execution_profile: str = "") -> str:
    cleaned = log_name.strip()
    if not cleaned:
        raise SystemExit("Log name is required.")
    if "." in cleaned and "{" not in cleaned and "}" not in cleaned:
        return cleaned
    effective = config
    selected = execution_profile or default_execution_profile(config)
    if selected:
        effective = materialize_profile_config(config, selected)
    overrides = effective.get("table_overrides", {})
    for key, value in overrides.items():
        if key.lower() == cleaned.lower():
            return value
    profile = effective.get("table_naming_profile", {})
    pattern = profile.get("pattern")
    if not pattern:
        raise SystemExit("Project table_naming_profile.pattern is missing.")
    return pattern.format(
        log_name=cleaned,
        log_lower=cleaned.lower(),
        log_upper=cleaned.upper(),
    )


def cmd_init(args) -> None:
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    display_name = getattr(args, "display_name", None)
    project_name = display_name or args.project_name or root.name

    existing_manifest = read_json(manifest_path(root), {})
    if existing_manifest and existing_manifest.get("schema_version") != PROJECT_MANIFEST_SCHEMA_VERSION:
        raise SystemExit(
            "Existing project uses the legacy formal layout. Run formal_asset_migration.py; "
            "init never rewrites query_sql/dashboard_sql/validations/runs/archive in place."
        )

    for directory in [
        "context",
        "rules",
        "sources",
        "reviews",
        INTERMEDIATE_TABLE_DIR,
        "conversations",
    ]:
        (root / directory).mkdir(parents=True, exist_ok=True)

    project_config = ensure_project_config(root, project_name, args)
    initialize_formal_asset_repository(root, str(project_config.get("project_id") or root.name))
    manifest = existing_manifest
    if not manifest:
        manifest = {
            "schema_version": PROJECT_MANIFEST_SCHEMA_VERSION,
            "project_name": project_name,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "intermediate_table_counters": {},
            "intermediate_tables": [],
            "canonical_rule_store": {
                "contract_version": "canonical_rule_store_v2",
                "store": RULE_STORE_RELATIVE_PATH.as_posix(),
                "activation_index": RULE_INDEX_RELATIVE_PATH.as_posix(),
                "definitions_root": "rules/definitions",
            },
            "project_config_file": PROJECT_CONFIG_FILE,
            "taxonomy_version": 1,
        }
    else:
        manifest.setdefault("project_config_file", PROJECT_CONFIG_FILE)
        if display_name or args.project_name:
            manifest["project_name"] = project_name
        manifest["updated_at"] = now_iso()
    sync_compact_project_manifest(root, manifest)
    ensure_query_workspace(root, update_manifest=False)

    if not has_v2_store(root):
        initialize_empty_store(root, str(project_name or root.name))

    write_text_if_missing(
        root / "context" / "project_brief.md",
        f"# {project_name}\n\n## Scope\n\nDescribe the project, product, environments, and data questions here.\n",
    )
    write_text_if_missing(
        root / "rules" / "rule_change_log.md",
        "# Rule Change Log\n\nConfirmed rule changes should be summarized here when useful.\n",
    )
    rebuild_index(root)
    print(f"Initialized project: {root}")


def cmd_show_config(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    config = read_project_config(root)
    if not config:
        raise SystemExit(f"Project config not found: {project_config_path(root)}")
    print(json.dumps(config, ensure_ascii=False, indent=2))


def cmd_set_config(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    manifest = read_json(manifest_path(root), {})
    project_name = manifest.get("project_name", root.name)
    config = read_project_config(root) or default_project_config(root, project_name)
    config = apply_config_args(config, args)
    write_project_config(root, config)
    manifest["project_config_file"] = PROJECT_CONFIG_FILE
    manifest["updated_at"] = now_iso()
    write_json(manifest_path(root), manifest)
    rebuild_index(root)
    print(f"Updated project config: {project_config_path(root)}")


def cmd_resolve_table(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    config = read_project_config(root)
    if not config:
        raise SystemExit(f"Project config not found: {project_config_path(root)}")
    print(resolve_physical_table(config, args.log_name, getattr(args, "execution_profile", "")))


def next_rule_version(rules, rule_id: str) -> int:
    versions = [r.get("version", 0) for r in rules if r.get("rule_id") == rule_id]
    return max(versions, default=0) + 1


def cmd_add_rule(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    try:
        change_authorization = require_explicit_rule_write_authorization(
            getattr(args, "function_selection", None),
            user_request=getattr(args, "user_request", None),
            requested_status=args.status,
        )
    except FunctionGateError as exc:
        raise SystemExit(str(exc)) from exc
    if args.status == "confirmed" and not args.confirmed_by_user:
        raise SystemExit(
            "Confirmed rules require --confirmed-by-user after explicit user confirmation."
        )

    rule_id = slugify(args.rule_id or args.title, "rule")
    concept_key = slugify(args.concept_key, "concept") if args.concept_key else ""
    if concept_key_required(args.scope, args.lifetime):
        if not concept_key:
            raise SystemExit(
                "Project persistent canonical rules require --concept-key. "
                "Register the concept in sql-projects/_rule_review/rule_concepts.json first."
            )
        if concept_key not in registered_concept_keys(root):
            raise SystemExit(
                f"Concept key `{concept_key}` is not registered in {rule_concept_registry_path(root)}."
            )
    if not has_v2_store(root):
        raise SystemExit("Canonical Rule Store v2 is required. Run the explicit legacy migration first.")
    rules = RuleStore(root).load_versions(concept_key) if concept_key else []
    version = next_rule_version(rules, rule_id)

    activation_contract = None
    if getattr(args, "activation_contract_file", None):
        activation_contract = json.loads(Path(args.activation_contract_file).read_text(encoding="utf-8"))
    if getattr(args, "activation_contract_json", None):
        activation_contract = json.loads(args.activation_contract_json)
    if activation_contract is not None and not isinstance(activation_contract, dict):
        raise SystemExit("activation_contract must be a JSON object.")

    structured_definition = None
    if getattr(args, "structured_definition_file", None):
        structured_definition = json.loads(
            Path(args.structured_definition_file).read_text(encoding="utf-8")
        )
    if getattr(args, "structured_definition_json", None):
        structured_definition = json.loads(args.structured_definition_json)
    if structured_definition is not None and not isinstance(structured_definition, dict):
        raise SystemExit("structured_definition must be a JSON object.")

    record = {
        "rule_id": rule_id,
        "concept_key": concept_key,
        "version": version,
        "status": args.status,
        "title": args.title,
        "content": args.content,
        "source": args.source,
        "source_evidence": args.source_evidence or "",
        "confirmed_by_user": bool(args.confirmed_by_user),
        "scope": args.scope,
        "lifetime": args.lifetime,
        "applies_to": args.applies_to,
        "affected_artifacts": parse_csv(args.affected_artifacts),
        "decision_question": args.decision_question or "",
        "supersedes": args.supersedes or "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "notes": args.notes or "",
        "change_authorization": change_authorization,
    }
    if activation_contract:
        record["activation_contract"] = activation_contract
    if structured_definition:
        record["structured_definition"] = structured_definition
    RuleStore(root).write_new_version(record)

    append_rule_log(root, record)
    rebuild_index(root)
    print(f"Saved {args.status} rule {rule_id} v{version}")


def append_rule_log(root: Path, record: dict) -> None:
    log = root / "rules" / "rule_change_log.md"
    line = (
        f"- {record['created_at']} `{record['status']}` "
        f"`{record['rule_id']}` concept=`{record.get('concept_key', '') or 'missing'}` "
        f"v{record['version']}: {record['title']}\n"
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line)


def artifact_dir(root: Path, kind: str, slug: str) -> Path:
    raise SystemExit(
        "Legacy formal SQL directories are write-disabled. "
        "Use FormalAssetRepository with an explicit Package context."
    )


def next_artifact_version(directory: Path) -> int:
    versions = []
    if directory.exists():
        for path in directory.glob("v*.sql"):
            match = re.fullmatch(r"v(\d{3})\.sql", path.name)
            if match:
                versions.append(int(match.group(1)))
    return max(versions, default=0) + 1


def is_current_artifact(item: dict) -> bool:
    state = item.get("artifact_state") or "current"
    return state == "current" and item.get("status") != "superseded"


def artifact_meta_path(root: Path, item: dict) -> Path:
    sql_path = root / item["path"]
    return sql_path.with_name(sql_path.stem + ".meta.json")


def write_artifact_meta(root: Path, item: dict) -> None:
    write_json(artifact_meta_path(root, item), item)


def normalize_table_name(value: str) -> str:
    return value.strip().strip("`").lower()


def table_dir(root: Path, slug: str) -> Path:
    return root / INTERMEDIATE_TABLE_DIR / slugify(slug, "table")


def is_current_table(item: dict) -> bool:
    state = item.get("table_state") or "current"
    return state == "current" and item.get("status") not in {"superseded", "deprecated"}


def table_meta_path(root: Path, item: dict) -> Path:
    sql_path = root / item["path"]
    return sql_path.with_name(sql_path.stem + ".meta.json")


def write_table_meta(root: Path, item: dict) -> None:
    write_json(table_meta_path(root, item), item)


def resolve_table_change_type(requested: str, version: int) -> str:
    if requested == "auto":
        return "new" if version == 1 else "replacement"
    if requested == "new" and version > 1:
        raise SystemExit(
            "This intermediate table already exists. Use --change-type correction/"
            "replacement/schema_change/etc., or create a new table with --change-type branch."
        )
    if requested == "branch" and version > 1:
        raise SystemExit(
            "Branch tables must use a new table name or slug. Pass --branch-of <source table> "
            "to link the branch to its source."
        )
    return requested


def resolve_change_type(requested: str, version: int) -> str:
    if requested == "auto":
        return "new" if version == 1 else "replacement"
    if requested == "new" and version > 1:
        raise SystemExit(
            "This slug already exists. Use --change-type clarification/correction/replacement/"
            "superset for the same SQL family, or --change-type branch with a new slug."
        )
    if requested == "branch" and version > 1:
        raise SystemExit(
            "Branch artifacts must use a new slug. Pass --branch-of <source SQL path> "
            "to link the branch to its source."
        )
    return requested


def linked_run_status(root: Path, linked_run: str) -> str:
    if not linked_run:
        return ""
    run_path = root / linked_run
    if run_path.exists():
        for line in run_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("- status:"):
                return line.split(":", 1)[1].strip()
    manifest = read_json(manifest_path(root), {})
    for item in manifest.get("run_evidence", []):
        if item.get("path") == linked_run or item.get("run_id") == linked_run:
            return item.get("status", "")
    return ""


def find_matching_paren(text: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    index = open_index
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def extract_top_params_cte(sql: str) -> tuple[str, int, int] | None:
    cleaned = strip_sql_comments(sql)
    match = re.match(r"\s*with\s+params\s+as\s*\(", cleaned, flags=re.I)
    if not match:
        return None
    open_index = cleaned.find("(", match.start())
    close_index = find_matching_paren(cleaned, open_index)
    if close_index < 0:
        return None
    return cleaned[open_index + 1 : close_index], open_index, close_index


def params_cte_aliases(sql: str) -> set[str]:
    cte = extract_top_params_cte(sql)
    if not cte:
        return set()
    body = cte[0]
    return {
        match.group(1).strip("`").lower()
        for match in re.finditer(r"\bas\s+`?([a-zA-Z_][\w]*)`?", body, flags=re.I)
    }


def remove_top_params_cte_body(sql: str) -> str:
    cleaned = strip_sql_comments(sql)
    cte = extract_top_params_cte(sql)
    if not cte:
        return cleaned
    _, open_index, close_index = cte
    return cleaned[: open_index + 1] + " ... " + cleaned[close_index:]


def query_spec_parameter_names(spec_doc: dict | None) -> set[str]:
    params = (spec_doc or {}).get("parameters", [])
    if not isinstance(params, list):
        return set()
    return {
        str(item.get("name", "")).strip().lower()
        for item in params
        if isinstance(item, dict) and item.get("name")
    }


def has_zone_filter(sql_without_params_body: str) -> bool:
    return bool(
        re.search(
            r"\b(?:where|and|or)\b[^;\n]{0,220}\b(?:izoneareaid|gamesvrid|game_svr_id|zone_area_id)\b\s*(?:=|in\b)",
            sql_without_params_body,
            flags=re.I,
        )
    )


def has_direct_zone_literal(sql_without_params_body: str) -> bool:
    return bool(
        re.search(
            r"\b(?:where|and|or)\b[^;\n]{0,220}\b(?:izoneareaid|gamesvrid|game_svr_id|zone_area_id)\b\s*(?:=|in\b)\s*(?:\(?\s*\d|'[^']+')",
            sql_without_params_body,
            flags=re.I,
        )
    )


def has_direct_time_literal(sql_without_params_body: str, field_names: list[str]) -> bool:
    for field in field_names:
        if not field:
            continue
        escaped = re.escape(field)
        if re.search(
            rf"\b{escaped}\b\s*(?:>=|>|<=|<|between)\s*'?\d{{4}}[-/]?\d{{2}}[-/]?\d{{2}}",
            sql_without_params_body,
            flags=re.I,
        ):
            return True
    return False


def query_params_contract_problems(sql: str, config: dict, spec_doc: dict | None = None) -> list[str]:
    problems: list[str] = []
    aliases = params_cte_aliases(sql)
    if not aliases:
        problems.append(
            "Formal QUERY SQL must start with a top `params AS (...)` CTE before business CTEs. "
            "Move date/time and business filter literals there before save-sql."
        )
        return problems

    tables = extract_tables(sql)
    has_tlog = any(is_tlog_source_table(table) for table in tables)
    policy = config.get("partition_policy", {}) if isinstance(config, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    business_time_field = str(policy.get("business_time_field") or "")
    sql_body = remove_top_params_cte_body(sql)

    required_aliases: set[str] = set()
    if has_tlog and policy.get("business_time_required") is True:
        required_aliases.update(QUERY_TIME_PARAM_ALIASES)
    if has_tlog and policy.get("required_for_tlog") is True:
        required_aliases.update(QUERY_PARTITION_PARAM_ALIASES)
    if has_zone_filter(sql_body):
        if not (aliases & QUERY_ZONE_PARAM_ALIASES):
            problems.append(
                "QUERY SQL filters zone/server fields such as iZoneAreaID/GameSvrId, "
                "but the top params CTE has no zone alias such as `zone_id`."
            )
        if has_direct_zone_literal(sql_body):
            problems.append(
                "QUERY SQL must not hard-code iZoneAreaID/GameSvrId values in WHERE. "
                "Put the value in `params` and compare to `p.zone_id` or an equivalent params alias."
            )
    missing_aliases = sorted(required_aliases - aliases)
    if missing_aliases:
        problems.append(
            "QUERY params CTE is missing required alias(es): " + ", ".join(missing_aliases) + "."
        )

    partition_field = str(policy.get("partition_field") or "")
    time_fields = unique_in_order([business_time_field, partition_field, "dtEventTime", "dtEventDate", "dteventdate"])
    if has_tlog and has_direct_time_literal(sql_body, time_fields):
        problems.append(
            "QUERY SQL must not hard-code time/partition literals in WHERE. "
            "Put them in `params` using `pt_start`/`pt_end` for partition dates and `ts_start`/`ts_end` only when detailed event time is needed."
        )

    spec_names = query_spec_parameter_names(spec_doc)
    if spec_doc is not None and required_aliases and not required_aliases.issubset(spec_names):
        missing_spec = sorted(required_aliases - spec_names)
        problems.append(
            "QUERY sidecar `parameters` is missing params CTE alias(es): " + ", ".join(missing_spec) + "."
        )
    if spec_doc is not None and (aliases & QUERY_ZONE_PARAM_ALIASES) and not (spec_names & QUERY_ZONE_PARAM_ALIASES):
        problems.append(
            "QUERY sidecar `parameters` must document the zone/server params alias used by the SQL."
        )
    declared_mode = ""
    if isinstance(spec_doc, dict):
        time_contract = spec_doc.get("time_contract")
        if isinstance(time_contract, dict):
            declared_mode = str(time_contract.get("mode") or "")
    problems.extend(time_contract_problem_messages(sql, config, declared_mode=declared_mode))
    return problems


def recommend_change_type(prior: dict, current: dict, note: str) -> dict:
    note_text = note.lower()
    replacement_words = [
        "漏",
        "忘",
        "错",
        "修正",
        "纠正",
        "补充筛选",
        "没说清",
        "口径",
        "filter",
        "where",
        "fix",
        "correct",
        "missing",
    ]
    branch_words = [
        "分组",
        "拆分",
        "再看",
        "对比",
        "另",
        "新增维度",
        "group",
        "segment",
        "breakdown",
        "variant",
        "branch",
    ]
    superset_words = [
        "完整覆盖",
        "保留原",
        "新增指标",
        "扩张",
        "扩展",
        "superset",
    ]
    prior_dims = set(item.lower() for item in prior.get("dimensions", []))
    current_dims = set(item.lower() for item in current.get("dimensions", []))
    prior_metrics = set(item.lower() for item in prior.get("metrics", []))
    current_metrics = set(item.lower() for item in current.get("metrics", []))
    prior_tables = set(item.lower() for item in prior.get("tables", []))
    current_tables = set(item.lower() for item in current.get("tables", []))

    if any(word in note_text for word in superset_words):
        return {
            "recommendation": "superset",
            "reason": "The change explicitly retains the old answer while adding same-contract output.",
            "question": "",
        }
    if any(word in note_text for word in replacement_words):
        return {
            "recommendation": "replacement",
            "reason": "The change note reads like a correction, missing filter, or clarified definition.",
            "question": "",
        }
    if any(word in note_text for word in branch_words):
        return {
            "recommendation": "ask",
            "reason": "The change looks like a new grouping, segment, or analytical view.",
            "question": "Should the previous SQL remain as a current reusable artifact, or should this replace it?",
        }
    if current_dims != prior_dims and current_metrics == prior_metrics and current_tables == prior_tables:
        return {
            "recommendation": "ask",
            "reason": "Metrics and tables are stable but dimensions changed, so both versions may have value.",
            "question": "Is this a branch for another view, or a correction of the original output grain?",
        }
    if (
        prior_metrics
        and prior_metrics < current_metrics
        and current_dims == prior_dims
        and current_tables == prior_tables
        and str(current.get("grain") or "") == str(prior.get("grain") or "")
    ):
        return {
            "recommendation": "superset",
            "reason": "The same tables, dimensions, and grain retain every prior metric and add more metrics.",
            "question": "",
        }
    if current_metrics != prior_metrics or current_tables != prior_tables:
        return {
            "recommendation": "ask",
            "reason": "Tables or metrics changed, so the artifact intent may have changed.",
            "question": "Should this be saved as a new branch/new artifact, or replace the old SQL?",
        }
    return {
        "recommendation": "replacement",
        "reason": "The SQL shape is materially the same, so this is likely the latest corrected version.",
        "question": "",
    }


def cmd_describe_sql(args) -> None:
    source = Path(args.sql_file).resolve()
    if not source.exists():
        raise SystemExit(f"SQL file not found: {source}")
    kind = normalize_kind(args.kind)
    current = analyze_sql_file(source, kind)
    result = {"sql_file": str(source), "kind": display_kind(kind), "analysis": current}
    seed_result: dict | None = None
    if args.prior_sql:
        prior_source = Path(args.prior_sql).resolve()
        if not prior_source.exists():
            raise SystemExit(f"Prior SQL file not found: {prior_source}")
        prior = analyze_sql_file(prior_source, kind)
        result["prior_sql_file"] = str(prior_source)
        result["prior_analysis"] = prior
        result["change_decision"] = recommend_change_type(prior, current, args.change_note or "")
    if getattr(args, "write_formalize_seed", False):
        if kind != "QUERY":
            raise SystemExit("--write-formalize-seed is only supported for QUERY SQL.")
        if not getattr(args, "root", ""):
            raise SystemExit("--root <project-root> is required with --write-formalize-seed.")
        purpose = "sql_project.py describe-sql --write-formalize-seed"
        try:
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=command_function_ids(
                    "sql_project.py", "describe-sql-write-formalize-seed"
                ),
                purpose=purpose,
            )
            require_user_request(args.user_request, purpose=purpose)
        except FunctionGateError as exc:
            raise SystemExit(str(exc)) from exc
        command = [
            sys.executable,
            str(Path(__file__).resolve().parent / "sql_formalize_seed.py"),
            "--root",
            str(Path(args.root).resolve()),
            "--sql-file",
            str(source),
            "--title",
            args.seed_title or source.stem,
            "--format",
            "json",
            "--user-request",
            args.user_request,
            "--function-selection",
            args.function_selection or "QUERY",
        ]
        if args.slug:
            command.extend(["--slug", args.slug])
        if args.formalize_seed_output:
            command.extend(["--output", str(Path(args.formalize_seed_output).resolve())])
        if args.allow_incomplete_project_config:
            command.append("--allow-incomplete-project-config")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        try:
            seed_result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            seed_result = {"status": "error", "blockers": [proc.stderr.strip() or proc.stdout.strip() or "sql_formalize_seed.py emitted non-JSON output"]}
        if proc.returncode != 0:
            blockers = seed_result.get("blockers") if isinstance(seed_result, dict) else None
            raise SystemExit("Failed to write formalize seed:\n- " + "\n- ".join(str(item) for item in (blockers or [proc.stderr.strip() or "unknown error"])))
        result["formalize_seed"] = seed_result
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    analysis = result["analysis"]
    print(f"summary: {analysis['content_summary']}")
    print(f"business_category: {analysis['business_category']}")
    print(f"analysis_type: {analysis['analysis_type']}")
    print(f"target_tables: {','.join(analysis['target_tables'])}")
    print(f"tables: {','.join(analysis['tables'])}")
    print(f"partition_fields: {','.join(analysis['partition_fields'])}")
    print(f"metrics: {','.join(analysis['metrics'])}")
    print(f"dimensions: {','.join(analysis['dimensions'])}")
    print(f"grain: {analysis['grain']}")
    print(f"time_grain: {analysis['time_grain']}")
    print(f"reuse_candidate: {str(analysis['reuse_candidate']).lower()}")
    if analysis["warnings"]:
        print(f"warnings: {'; '.join(analysis['warnings'])}")
    if "change_decision" in result:
        decision = result["change_decision"]
        print(f"change_recommendation: {decision['recommendation']}")
        print(f"change_reason: {decision['reason']}")
        if decision["question"]:
            print(f"confirmation_question: {decision['question']}")
    if seed_result:
        print(f"formalize_seed_status: {seed_result.get('status')}")
        print(f"formalize_seed_output: {seed_result.get('output')}")


def infer_registered_intermediate_tables(manifest: dict, source_tables: list[str]) -> list[str]:
    normalized_sources = {normalize_table_name(table) for table in source_tables}
    matched = []
    for item in manifest.get("intermediate_tables", []):
        if not is_current_table(item):
            continue
        table_name = item.get("table_name", "")
        slug = item.get("slug", "")
        if normalize_table_name(table_name) in normalized_sources or normalize_table_name(slug) in normalized_sources:
            matched.append(table_name or slug)
    return unique_in_order(matched)


def cmd_save_sql(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    kind = normalize_kind(args.kind)
    package_id, package = _resolve_formal_package_context(root, args, kind)
    source_title = clean_source_title(args.title)
    stable_title = strip_source_prefix(source_title)

    source = Path(args.sql_file).resolve()
    if not source.exists():
        raise SystemExit(f"SQL file not found: {source}")
    _reject_legacy_archive_path(root, source, label="Formal SQL source")
    if not args.spec_file:
        raise SystemExit("Formal SQL artifacts require --spec-file <query|validation|dashboard spec JSON>.")
    spec_source = Path(args.spec_file).resolve()
    if not spec_source.exists():
        raise SystemExit(f"Spec file not found: {spec_source}")
    try:
        spec_doc = read_json_object(spec_source)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Spec file must be a JSON object: {spec_source}: {exc}") from exc
    sql_text = source.read_text(encoding="utf-8")
    if has_full_spec_block(sql_text):
        raise SystemExit(
            "Formal SQL must use a short header plus sidecar spec. "
            "Move the full @...SPEC block into --spec-file before save-sql."
        )
    inferred = analyze_sql_file(source, kind) if args.auto_metadata else {}
    business_category = args.business_category
    if args.auto_metadata and business_category == DEFAULT_BUSINESS_CATEGORY:
        business_category = inferred.get("business_category", DEFAULT_BUSINESS_CATEGORY)
    analysis_type = args.analysis_type
    if args.auto_metadata and analysis_type == DEFAULT_ANALYSIS_TYPE:
        analysis_type = inferred.get("analysis_type", DEFAULT_ANALYSIS_TYPE)
    if kind == "DASHBOARD":
        if package is None:
            raise SystemExit("Dashboard artifacts require an existing --package-id.")
        if not args.linked_query:
            raise SystemExit("Dashboard artifacts require --linked-query <Package query member id/path>.")
        if not args.linked_validation:
            raise SystemExit("Dashboard artifacts require --linked-validation <Package validation member id/path>.")
        if not args.linked_run:
            raise SystemExit("Dashboard artifacts require --linked-run <Package run member id/path>.")
        linked_query_member = _package_member(package, args.linked_query, roles=set(FORMAL_SQL_ROLES["QUERY"]))
        linked_validation_member = _package_member(package, args.linked_validation, roles={"validation_sql"})
        linked_run_member = _package_member(package, args.linked_run, roles={"run_meta", "run_record"})
        if args.verification_status == "not_applicable":
            raise SystemExit(
                "Dashboard artifacts require --verification-status verified, proxy_verified, or unverified_skipped_run."
            )
        run_status = _formal_run_status(root, package, str(linked_run_member["member_id"]))
        if args.verification_status == "verified" and run_status == "proxy_verified":
            raise SystemExit(
                "Dashboard artifacts linked to proxy_verified run evidence cannot be saved as verified."
            )
        if args.verification_status == "proxy_verified" and run_status and run_status != "proxy_verified":
            raise SystemExit(
                f"Dashboard verification_status proxy_verified requires linked run status proxy_verified; linked run is {run_status}."
            )
        if args.verification_status == "unverified_skipped_run":
            if not args.verification_note:
                raise SystemExit("Unverified dashboard artifacts require --verification-note.")
            if not args.future_verification_plan:
                raise SystemExit(
                    "Unverified dashboard artifacts require --future-verification-plan."
                )
        if args.verification_status == "proxy_verified":
            if not args.verification_note:
                raise SystemExit("Proxy-verified dashboard artifacts require --verification-note.")
            if not args.future_verification_plan:
                raise SystemExit(
                    "Proxy-verified dashboard artifacts require --future-verification-plan for target verification."
                )
    project_config = read_project_config(root)
    if not project_config:
        raise SystemExit(
            f"Project config is required before saving formal SQL: {project_config_path(root)}"
        )
    config_problems = validate_project_config(project_config, kind)
    if config_problems:
        raise SystemExit(
            "Project config is incomplete for formal SQL:\n- "
            + "\n- ".join(config_problems)
        )
    artifact_execution_route = execution_route_for_file(source, sql_text, project_config)
    if artifact_execution_route.get("status") != "ready":
        raise SystemExit(
            "Formal SQL execution route is not deliverable:\n- "
            + "\n- ".join(
                str(item)
                for item in artifact_execution_route.get("blockers", []) or ["execution route not ready"]
            )
        )
    spec_doc["execution_route"] = artifact_execution_route
    workspace_reference: dict | None = None
    if kind == "QUERY":
        declared_origin = spec_doc.get("origin_query_workspace") if isinstance(spec_doc.get("origin_query_workspace"), dict) else {}
        if declared_origin:
            declared_path = str(declared_origin.get("path") or "")
            if not declared_path:
                raise SystemExit("QUERY origin_query_workspace.path is required.")
            try:
                workspace_reference = find_query_workspace_reference(root, root / declared_path, match_fingerprint=False)
            except (OSError, ValueError) as exc:
                raise SystemExit(f"QUERY origin_query_workspace cannot be resolved: {exc}") from exc
            if not workspace_reference:
                raise SystemExit(
                    "QUERY origin_query_workspace must reference an indexed query_workspace SQL version."
                )
            for key in ["query_id", "version", "source_sql_fingerprint"]:
                expected = declared_origin.get(key)
                actual_key = "sql_fingerprint" if key == "source_sql_fingerprint" else key
                actual = workspace_reference.get(actual_key)
                if expected not in (None, "") and str(expected) != str(actual):
                    raise SystemExit(
                        f"QUERY origin_query_workspace.{key} does not match query_workspace/index.json."
                    )
        else:
            workspace_reference = find_query_workspace_reference(root, source)
            if not workspace_reference:
                raise SystemExit(
                    "Formal QUERY SQL must originate from an indexed project-local query workspace version. "
                    "Run `sql_query_workspace.py save` first, then pass that SQL/spec to save-sql."
                )
        spec_doc["origin_query_workspace"] = query_workspace_origin_contract(workspace_reference)
        if workspace_reference.get("status") not in {"runnable", "result_confirmed", "promoted"} or not workspace_reference.get("delivery_ready"):
            raise SystemExit(
                "Formal QUERY source must be a delivery-ready query workspace version with generation_gate.status=ok."
            )
        effective_query_config, _ = effective_config_for_context(
            project_config,
            sql_text,
            workspace_reference.get("execution_route"),
        )
        params_problems = query_params_contract_problems(sql_text, effective_query_config, spec_doc)
        if params_problems:
            raise SystemExit(
                "Formal QUERY SQL must be normalized to a top params CTE before saving:\n- "
                + "\n- ".join(params_problems)
            )
        repository_summary = spec_doc.get("repository_summary") if isinstance(spec_doc.get("repository_summary"), dict) else {}
        required_summary_fields = [
            "display_title",
            "business_topic",
            "purpose",
            "business_question",
            "base_population",
            "grain",
            "metrics",
            "metric_groups",
            "dimensions",
            "filters",
            "source_logs",
            "logic_summary",
            "applied_criteria",
            "canonical_rule_status",
            "canonical_rule_checks",
            "result_evidence",
        ]
        missing_summary_keys = [
            field
            for field in required_summary_fields
            if field not in repository_summary
        ]
        non_empty_summary_fields = [
            "display_title",
            "business_topic",
            "purpose",
            "business_question",
            "base_population",
            "grain",
            "metrics",
            "metric_groups",
            "dimensions",
            "filters",
            "source_logs",
            "logic_summary",
            "applied_criteria",
            "canonical_rule_status",
        ]
        empty_summary_fields = [
            field
            for field in non_empty_summary_fields
            if repository_summary.get(field) in (None, "", [])
        ]
        if missing_summary_keys or empty_summary_fields:
            raise SystemExit(
                "Formal QUERY specs require repository_summary before save-sql. "
                "Generate it during retention/promotion so the SQL repository page can explain the asset without reading raw review output.\n- "
                + "\n- ".join(
                    f"repository_summary.{field}"
                    for field in [*missing_summary_keys, *empty_summary_fields]
                )
            )

    slug = slugify(args.slug or stable_title, "artifact")
    version = _formal_member_version(package, kind)
    manifest = read_json(manifest_path(root), {})
    intermediate_tables = csv_or_inferred(
        args.intermediate_tables,
        infer_registered_intermediate_tables(manifest, inferred.get("tables", [])),
    )
    change_type = resolve_change_type(args.change_type, version)
    if change_type == "branch" and not args.branch_of:
        raise SystemExit("Branch artifacts require --branch-of <Package member id/path>.")
    if change_type == "branch" and package is not None:
        raise SystemExit("A branch starts a new Package; use --new-package instead of an existing --package-id.")
    tags = csv_or_inferred(args.tags, inferred.get("tags", []))
    if kind == "DASHBOARD" and args.verification_status == "unverified_skipped_run":
        tags = unique_in_order(tags + ["unvalidated", "no_result_file"])
    if kind == "DASHBOARD" and args.verification_status == "proxy_verified":
        tags = unique_in_order(tags + ["proxy_verified", "needs_target_verification"])

    created_at = now_iso()
    member_updates: list[dict] = []
    superseded_items: list[dict] = []
    if change_type in REPLACEMENT_CHANGE_TYPES:
        member_updates, superseded_items = _formal_member_updates_for_replacement(package, kind)
    supersedes = [
        str(item.get("path") or "")
        for item in superseded_items
        if item.get("role") in FORMAL_SQL_ROLES[kind]
    ]

    generation_provenance = merge_generation_provenance(
        spec_doc.get("generation_provenance") if isinstance(spec_doc.get("generation_provenance"), dict) else None,
        fallback_generator_script="sql_project.py",
        fallback_workflow="save-sql",
        artifact_kind=kind,
        saved_at=created_at,
        saved_by_script="sql_project.py",
    )

    prefix = FORMAL_MEMBER_PREFIX[kind]
    sql_target = f"{prefix}/v{version:03d}.sql"
    spec_target = f"{prefix}/v{version:03d}.spec.json"
    meta_target = f"{prefix}/v{version:03d}.meta.json"
    sql_member_id = f"{prefix}-v{version:03d}-sql"
    spec_member_id = f"{prefix}-v{version:03d}-spec"
    meta_member_id = f"{prefix}-v{version:03d}-meta"
    package_title = str((package or {}).get("title") or stable_title)
    linked_query_value = str(args.linked_query or "")
    linked_validation_value = str(args.linked_validation or "")
    linked_run_value = str(args.linked_run or "")
    lineage: list[dict] = [
        {"relation": "describes", "from_member_id": spec_member_id, "to_member_id": sql_member_id},
        {"relation": "describes", "from_member_id": meta_member_id, "to_member_id": sql_member_id},
    ]
    for old in superseded_items:
        if old.get("role") == f"{prefix}_sql":
            lineage.append(
                {
                    "relation": "supersedes",
                    "from_member_id": sql_member_id,
                    "to_member_id": str(old["member_id"]),
                }
            )
    if package is not None and linked_query_value:
        linked_query = _package_member(package, linked_query_value, roles=set(FORMAL_SQL_ROLES["QUERY"]))
        linked_query_value = str(linked_query["path"])
        relation = "derived_from" if kind == "DASHBOARD" else "validates"
        lineage.append(
            {
                "relation": relation,
                "from_member_id": sql_member_id,
                "to_member_id": str(linked_query["member_id"]),
            }
        )
    if package is not None and linked_validation_value:
        linked_validation = _package_member(package, linked_validation_value, roles={"validation_sql"})
        linked_validation_value = str(linked_validation["path"])
    if package is not None and linked_run_value:
        linked_run = _package_member(package, linked_run_value, roles={"run_meta", "run_record"})
        linked_run_value = str(linked_run["path"])
        lineage.append(
            {
                "relation": "supported_by",
                "from_member_id": sql_member_id,
                "to_member_id": str(linked_run["member_id"]),
            }
        )

    formal_root = root / FORMAL_ASSET_ROOT_REL
    formal_root.mkdir(parents=True, exist_ok=True)
    receipt: dict
    with tempfile.TemporaryDirectory(prefix=".sql-project-save-", dir=formal_root) as temporary:
        staging = Path(temporary)
        staged_sql = staging / f"v{version:03d}.sql"
        staged_spec = staging / f"v{version:03d}.spec.json"
        staged_meta = staging / f"v{version:03d}.meta.json"
        staged_sql.write_text(sql_text, encoding="utf-8")
        write_json_object(staged_spec, spec_doc)
        write_json(staged_meta, {"pending_package_identity": True})
        new_members = [
            {
                "member_id": sql_member_id,
                "source_path": staged_sql,
                "target_path": sql_target,
                "role": f"{prefix}_sql",
            },
            {
                "member_id": spec_member_id,
                "source_path": staged_spec,
                "target_path": spec_target,
                "role": f"{prefix}_spec",
            },
            {
                "member_id": meta_member_id,
                "source_path": staged_meta,
                "target_path": meta_target,
                "role": f"{prefix}_meta",
            },
        ]
        try:
            provisional = plan_formal_asset_package(
                root,
                title=package_title,
                package_id=package_id,
                slug=slug if package_id is None else None,
                members=[*member_updates, *new_members],
                lineage=lineage,
            )
        except FormalAssetRepositoryError as exc:
            raise SystemExit(str(exc)) from exc
        rel_sql = _formal_member_path(provisional.package_directory, sql_target)
        rel_spec = _formal_member_path(provisional.package_directory, spec_target)
        rel_meta = _formal_member_path(provisional.package_directory, meta_target)
        metadata = {
            "kind": kind,
            "package_id": provisional.package_id,
            "member_id": sql_member_id,
            "slug": slug,
            "version": version,
            "title": stable_title,
            "source_title": source_title if source_title != stable_title else "",
            "status": args.status,
            "artifact_state": "current",
            "change_type": change_type,
            "supersedes": supersedes,
            "replaced_by": "",
            "branch_of": args.branch_of or "",
            "change_reason": args.change_reason or "",
            "path": rel_sql,
            "spec_path": rel_spec,
            "meta_path": rel_meta,
            "spec_storage": SPEC_STORAGE,
            "header_contract_version": "1",
            "generation_provenance": generation_provenance,
            "project_context": project_context_snapshot(project_config, sql_text),
            "execution_route": artifact_execution_route,
            "business_category": business_category,
            "analysis_type": analysis_type,
            "tags": tags,
            "metrics": csv_or_inferred(args.metrics, inferred.get("metrics", [])),
            "dimensions": csv_or_inferred(args.dimensions, inferred.get("dimensions", [])),
            "tables": csv_or_inferred(args.tables, inferred.get("tables", [])),
            "intermediate_tables": intermediate_tables,
            "grain": text_or_inferred(args.grain, inferred.get("grain", "")),
            "time_grain": text_or_inferred(args.time_grain, inferred.get("time_grain", "")),
            "reusable": bool(args.reusable),
            "reuse_candidate": bool(inferred.get("reuse_candidate", False)),
            "reuse_notes": args.reuse_notes or inferred.get("reuse_notes", ""),
            "content_summary": inferred.get("content_summary", ""),
            "auto_metadata": bool(args.auto_metadata),
            "auto_metadata_warnings": inferred.get("warnings", []),
            "natural_language_intent": args.intent or "",
            "linked_query": linked_query_value,
            "linked_validation": linked_validation_value,
            "linked_run": linked_run_value,
            "verification_status": args.verification_status,
            "verification_note": args.verification_note or "",
            "future_verification_plan": args.future_verification_plan or "",
            "created_at": created_at,
            "notes": args.notes or "",
        }
        if kind == "QUERY" and workspace_reference:
            metadata["origin_query_workspace"] = query_workspace_origin_contract(workspace_reference)
        set_spec_version(spec_doc)
        apply_generation_provenance(spec_doc, generation_provenance)
        staged_sql.write_text(
            stamp_sql_generation(
                root,
                replace_or_prepend_short_header(
                    kind,
                    sql_text,
                    build_short_header(root, metadata, spec_doc, rel_spec),
                ),
            ),
            encoding="utf-8",
        )
        write_json_object(staged_spec, spec_doc)
        write_json(staged_meta, metadata)
        try:
            final_plan = plan_formal_asset_package(
                root,
                title=package_title,
                package_id=package_id,
                slug=slug if package_id is None else None,
                members=[*member_updates, *new_members],
                lineage=lineage,
            )
            if (
                final_plan.package_id != provisional.package_id
                or final_plan.package_directory != provisional.package_directory
            ):
                raise FormalAssetRepositoryError("Formal Package identity changed while preparing SQL; retry save-sql.")
            receipt = apply_formal_asset_plan(final_plan)
        except FormalAssetRepositoryError as exc:
            raise SystemExit(str(exc)) from exc

    if kind == "QUERY" and workspace_reference:
        try:
            mark_query_workspace_promoted(root, workspace_reference, rel_sql)
        except ValueError as exc:
            raise SystemExit(f"Formal QUERY was saved but query workspace promotion linkage failed: {exc}") from exc
    rebuild_index(root)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


def find_artifact(manifest: dict, kind: str, slug: str, version: int | None) -> dict:
    matches = [
        item
        for item in manifest.get("artifacts", [])
        if item.get("kind") == kind and item.get("slug") == slug
    ]
    if not matches:
        raise SystemExit(f"Artifact not found: {kind} {slug}")
    if version is not None:
        for item in matches:
            if item.get("version") == version:
                return item
        raise SystemExit(f"Artifact version not found: {kind} {slug} v{version:03d}")
    return sorted(matches, key=lambda item: item.get("version", 0))[-1]


def update_if_present(target: dict, key: str, value) -> None:
    if value is not None:
        target[key] = value


def cmd_update_artifact(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    package_id = str(getattr(args, "package_id", None) or "").strip().upper()
    member_id = str(getattr(args, "member_id", None) or "").strip()
    if not package_id or not member_id:
        raise SystemExit("update-artifact requires --package-id FA-NNNN and --member-id.")
    package = _load_package_context(root, package_id)
    member = _package_member(package, member_id)
    kind = str(getattr(args, "kind", None) or "").upper()
    if kind:
        normalize_kind(kind)
        expected_roles = FORMAL_SQL_ROLES[kind]
        if member.get("role") not in expected_roles:
            raise SystemExit(
                f"Package member {member_id} has role={member.get('role')}, not one of {sorted(expected_roles)}."
            )
    unsupported = [
        name
        for name in (
            "status",
            "change_type",
            "branch_of",
            "change_reason",
            "replaced_by",
            "supersedes",
            "business_category",
            "analysis_type",
            "tags",
            "metrics",
            "dimensions",
            "tables",
            "intermediate_tables",
            "grain",
            "time_grain",
            "reusable",
            "reuse_notes",
            "intent",
            "linked_query",
            "linked_validation",
            "linked_run",
            "verification_status",
            "verification_note",
            "future_verification_plan",
            "notes",
        )
        if getattr(args, name, None) not in (None, "")
    ]
    if unsupported:
        raise SystemExit(
            "Formal Package members are immutable; metadata edits require a new saved member version. "
            f"Unsupported in-place fields: {', '.join(unsupported)}"
        )
    member_state = getattr(args, "artifact_state", None)
    package_state = getattr(args, "package_state", None) or package.get("lifecycle_state") or "current"
    if not member_state and not getattr(args, "title", None) and not getattr(args, "package_state", None):
        raise SystemExit("update-artifact requires --artifact-state, --package-state, or --title.")
    member_updates = (
        [{"member_id": member_id, "lifecycle_state": member_state}]
        if member_state
        else []
    )
    try:
        plan = plan_formal_asset_package(
            root,
            package_id=package_id,
            title=str(getattr(args, "title", None) or package.get("title") or package_id),
            lifecycle_state=str(package_state),
            members=member_updates,
        )
        receipt = apply_formal_asset_plan(plan)
    except FormalAssetRepositoryError as exc:
        raise SystemExit(str(exc)) from exc
    rebuild_index(root)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


def find_table(manifest: dict, table_name: str | None, slug: str | None, version: int | None) -> dict:
    normalized_name = normalize_table_name(table_name or "")
    normalized_slug = slugify(slug or table_name or "", "table")
    matches = []
    for item in manifest.get("intermediate_tables", []):
        if table_name and normalize_table_name(item.get("table_name", "")) == normalized_name:
            matches.append(item)
        elif slug and item.get("slug") == normalized_slug:
            matches.append(item)
    if not matches:
        label = table_name or slug
        raise SystemExit(f"Intermediate table not found: {label}")
    if version is not None:
        for item in matches:
            if item.get("version") == version:
                return item
        label = table_name or slug
        raise SystemExit(f"Intermediate table version not found: {label} v{version:03d}")
    return sorted(matches, key=lambda item: item.get("version", 0))[-1]


def cmd_save_table(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    source = Path(args.sql_file).resolve()
    if not source.exists():
        raise SystemExit(f"SQL file not found: {source}")

    inferred = analyze_sql_file(source, "QUERY") if args.auto_metadata else {}
    inferred_targets = inferred.get("target_tables", [])
    table_name = args.table_name or (inferred_targets[0] if inferred_targets else "")
    if not table_name:
        raise SystemExit("Intermediate table name is required; pass --table-name or use SQL with CREATE/INSERT target.")

    slug = slugify(args.slug or table_name, "table")
    directory = table_dir(root, slug)
    directory.mkdir(parents=True, exist_ok=True)
    version = next_artifact_version(directory)
    sql_name = f"v{version:03d}.sql"
    meta_name = f"v{version:03d}.meta.json"
    destination = directory / sql_name
    shutil.copyfile(source, destination)
    rel_sql = destination.relative_to(root).as_posix()

    manifest = read_json(manifest_path(root), {})
    tables = manifest.setdefault("intermediate_tables", [])
    change_type = resolve_table_change_type(args.change_type, version)
    if change_type == "branch" and not args.branch_of:
        raise SystemExit("Branch intermediate tables require --branch-of <source table name or path>.")

    created_at = now_iso()
    supersedes: list[str] = []
    superseded_items: list[dict] = []
    if change_type in TABLE_REPLACEMENT_CHANGE_TYPES:
        for item in tables:
            same_slug = item.get("slug") == slug
            same_name = normalize_table_name(item.get("table_name", "")) == normalize_table_name(table_name)
            if (same_slug or same_name) and is_current_table(item):
                item["table_state"] = "history"
                item["status"] = "superseded"
                item["replaced_by"] = rel_sql
                item["replaced_at"] = created_at
                item["reusable"] = False
                supersedes.append(item.get("path", ""))
                superseded_items.append(item)

    business_category = args.business_category
    if args.auto_metadata and business_category == DEFAULT_BUSINESS_CATEGORY:
        business_category = inferred.get("business_category", DEFAULT_BUSINESS_CATEGORY)
    analysis_type = args.analysis_type
    if args.auto_metadata and analysis_type == DEFAULT_ANALYSIS_TYPE:
        analysis_type = inferred.get("analysis_type", "intermediate_build")

    metadata = {
        "table_name": table_name,
        "slug": slug,
        "version": version,
        "title": args.title or table_name,
        "status": args.status,
        "table_state": "current",
        "change_type": change_type,
        "supersedes": [path for path in supersedes if path],
        "replaced_by": "",
        "branch_of": args.branch_of or "",
        "change_reason": args.change_reason or "",
        "path": rel_sql,
        "table_type": args.table_type,
        "materialization": args.materialization,
        "lifecycle": args.lifecycle,
        "business_category": business_category,
        "analysis_type": analysis_type,
        "purpose": args.purpose or inferred.get("content_summary", ""),
        "grain": text_or_inferred(args.grain, inferred.get("grain", "")),
        "time_grain": text_or_inferred(args.time_grain, inferred.get("time_grain", "")),
        "partition_fields": csv_or_inferred(args.partition_fields, inferred.get("partition_fields", [])),
        "primary_keys": parse_csv(args.primary_keys),
        "source_tables": csv_or_inferred(args.source_tables, inferred.get("source_tables", [])),
        "source_artifacts": parse_csv(args.source_artifacts),
        "downstream_artifacts": parse_csv(args.downstream_artifacts),
        "downstream_tables": parse_csv(args.downstream_tables),
        "metrics": csv_or_inferred(args.metrics, inferred.get("metrics", [])),
        "dimensions": csv_or_inferred(args.dimensions, inferred.get("dimensions", [])),
        "tags": csv_or_inferred(args.tags, inferred.get("tags", [])),
        "refresh_mode": args.refresh_mode,
        "refresh_params": args.refresh_params or "",
        "retention_days": args.retention_days,
        "availability_status": args.availability_status,
        "availability_source": args.availability_source,
        "availability_note": args.availability_note or "",
        "unavailable_reason": args.unavailable_reason or "",
        "last_availability_check": args.last_availability_check or "",
        "source_contract_mode": args.source_contract_mode,
        "fallback_required": bool(args.fallback_required),
        "fallback_policy": args.fallback_policy or "",
        "fallback_source_tables": parse_csv(args.fallback_source_tables),
        "fallback_source_artifacts": parse_csv(args.fallback_source_artifacts),
        "fallback_sql_reference": args.fallback_sql_reference or "",
        "canonical_rule_refs": parse_csv(args.canonical_rule_refs),
        "xml_source_refs": parse_csv(args.xml_source_refs),
        "field_contract": args.field_contract or "",
        "grain_contract": args.grain_contract or "",
        "source_contract_note": args.source_contract_note or "",
        "owner": args.owner or "",
        "reusable": bool(args.reusable),
        "reuse_candidate": bool(inferred.get("reuse_candidate", False)),
        "reuse_notes": args.reuse_notes or inferred.get("reuse_notes", ""),
        "validation_artifacts": parse_csv(args.validation_artifacts),
        "quality_notes": args.quality_notes or "",
        "content_summary": inferred.get("content_summary", ""),
        "auto_metadata": bool(args.auto_metadata),
        "auto_metadata_warnings": inferred.get("warnings", []),
        "created_at": created_at,
        "notes": args.notes or "",
    }
    write_json(directory / meta_name, metadata)

    tables.append(metadata)
    manifest["updated_at"] = now_iso()
    counters = manifest.setdefault("intermediate_table_counters", {})
    counters[slug] = version
    write_json(manifest_path(root), manifest)
    for item in superseded_items:
        write_table_meta(root, item)
    rebuild_index(root)
    print(f"Saved intermediate table {table_name} v{version:03d}: {rel_sql}")


def cmd_update_table(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    manifest = read_json(manifest_path(root), {})
    item = find_table(manifest, args.table_name, args.slug, args.version)

    update_if_present(item, "title", args.title)
    update_if_present(item, "status", args.status)
    update_if_present(item, "table_state", args.table_state)
    update_if_present(item, "change_type", args.change_type)
    update_if_present(item, "branch_of", args.branch_of)
    update_if_present(item, "change_reason", args.change_reason)
    update_if_present(item, "replaced_by", args.replaced_by)
    update_if_present(item, "table_type", args.table_type)
    update_if_present(item, "materialization", args.materialization)
    update_if_present(item, "lifecycle", args.lifecycle)
    update_if_present(item, "business_category", args.business_category)
    update_if_present(item, "analysis_type", args.analysis_type)
    update_if_present(item, "purpose", args.purpose)
    update_if_present(item, "grain", args.grain)
    update_if_present(item, "time_grain", args.time_grain)
    update_if_present(item, "refresh_mode", args.refresh_mode)
    update_if_present(item, "refresh_params", args.refresh_params)
    update_if_present(item, "retention_days", args.retention_days)
    update_if_present(item, "availability_status", args.availability_status)
    update_if_present(item, "availability_source", args.availability_source)
    update_if_present(item, "availability_note", args.availability_note)
    update_if_present(item, "unavailable_reason", args.unavailable_reason)
    update_if_present(item, "last_availability_check", args.last_availability_check)
    update_if_present(item, "source_contract_mode", args.source_contract_mode)
    update_if_present(item, "fallback_policy", args.fallback_policy)
    update_if_present(item, "fallback_sql_reference", args.fallback_sql_reference)
    update_if_present(item, "field_contract", args.field_contract)
    update_if_present(item, "grain_contract", args.grain_contract)
    update_if_present(item, "source_contract_note", args.source_contract_note)
    update_if_present(item, "owner", args.owner)
    update_if_present(item, "reuse_notes", args.reuse_notes)
    update_if_present(item, "quality_notes", args.quality_notes)
    update_if_present(item, "notes", args.notes)
    if args.partition_fields is not None:
        item["partition_fields"] = parse_csv(args.partition_fields)
    if args.primary_keys is not None:
        item["primary_keys"] = parse_csv(args.primary_keys)
    if args.source_tables is not None:
        item["source_tables"] = parse_csv(args.source_tables)
    if args.source_artifacts is not None:
        item["source_artifacts"] = parse_csv(args.source_artifacts)
    if args.downstream_artifacts is not None:
        item["downstream_artifacts"] = parse_csv(args.downstream_artifacts)
    if args.downstream_tables is not None:
        item["downstream_tables"] = parse_csv(args.downstream_tables)
    if args.metrics is not None:
        item["metrics"] = parse_csv(args.metrics)
    if args.dimensions is not None:
        item["dimensions"] = parse_csv(args.dimensions)
    if args.tags is not None:
        item["tags"] = parse_csv(args.tags)
    if args.supersedes is not None:
        item["supersedes"] = parse_csv(args.supersedes)
    update_csv_if_present(item, "fallback_source_tables", args.fallback_source_tables)
    update_csv_if_present(item, "fallback_source_artifacts", args.fallback_source_artifacts)
    update_csv_if_present(item, "canonical_rule_refs", args.canonical_rule_refs)
    update_csv_if_present(item, "xml_source_refs", args.xml_source_refs)
    if args.validation_artifacts is not None:
        item["validation_artifacts"] = parse_csv(args.validation_artifacts)
    if args.fallback_required is not None:
        item["fallback_required"] = args.fallback_required.lower() == "true"
    if args.reusable is not None:
        item["reusable"] = args.reusable.lower() == "true"

    manifest["updated_at"] = now_iso()
    write_json(manifest_path(root), manifest)
    if table_meta_path(root, item).exists():
        write_table_meta(root, item)
    rebuild_index(root)
    print(f"Updated intermediate table {item.get('table_name')} v{item.get('version'):03d}")


def cmd_save_note(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    slug = slugify(args.slug or args.title, "note")
    timestamp = now_stamp()
    filename = f"{timestamp}_{slug}.md"
    destination = root / "conversations" / filename
    destination.parent.mkdir(parents=True, exist_ok=True)

    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")

    body = [
        f"# {args.title}",
        "",
        f"- kind: {args.kind}",
        f"- created_at: {now_iso()}",
        "",
        content.strip(),
        "",
    ]
    destination.write_text("\n".join(body), encoding="utf-8")

    rel_note = destination.relative_to(root).as_posix()
    manifest = read_json(manifest_path(root), {})
    notes = manifest.setdefault("notes", [])
    notes.append(
        {
            "kind": args.kind,
            "title": args.title,
            "path": rel_note,
            "created_at": now_iso(),
        }
    )
    manifest["updated_at"] = now_iso()
    write_json(manifest_path(root), manifest)
    rebuild_index(root)
    print(f"Saved note: {rel_note}")


def markdown_value(value, empty: str = "无") -> str:
    if value is None:
        return empty
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip()) or empty
    text = str(value).strip()
    return text or empty


def cmd_save_run(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    package_id = str(getattr(args, "package_id", None) or "").strip().upper()
    if not package_id:
        raise SystemExit("save-run requires --package-id FA-NNNN.")
    package = _load_package_context(root, package_id)
    source_member = _package_member(
        package,
        args.source_artifact,
        roles=set(FORMAL_SQL_ROLES["QUERY"] | FORMAL_SQL_ROLES["DASHBOARD"]),
    )
    source_sql = root / Path(str(source_member["path"]))
    if not source_sql.is_file():
        raise SystemExit(f"Package source SQL member is missing: {source_member['path']}")
    if args.sql_path:
        requested_sql = str(args.sql_path).replace("\\", "/")
        if requested_sql not in {str(source_member["member_id"]), str(source_member["path"])}:
            raise SystemExit("--sql-path must identify the same Package SQL member as --source-artifact.")
        _reject_legacy_archive_path(root, root / Path(requested_sql), label="Run SQL reference")
    evidence_source = Path(args.evidence_file).resolve() if args.evidence_file else None
    result_file_type = ""
    if args.status in {"observed", "passed", "proxy_verified"}:
        if args.status == "observed" and args.user_confirmed:
            raise SystemExit("Observed result evidence is not a user-confirmed validation result; use passed instead.")
        if not args.user_confirmed:
            if args.status != "observed":
                raise SystemExit(f"{args.status} run evidence requires --user-confirmed after user confirmation.")
        if not evidence_source:
            raise SystemExit(f"{args.status} run evidence requires --evidence-file with a real .csv or .xlsx result file.")
        result_file_type = evidence_source.suffix.lower()
        if result_file_type not in RESULT_FILE_EXTENSIONS:
            allowed = ", ".join(sorted(RESULT_FILE_EXTENSIONS))
            raise SystemExit(f"{args.status} run evidence requires result file type: {allowed}.")
    if args.status == "proxy_verified":
        if not args.definition_project or not args.execution_project or not args.delivery_project:
            raise SystemExit("Proxy verification requires --definition-project, --execution-project, and --delivery-project.")
        if args.execution_project == args.delivery_project and args.execution_project == args.definition_project:
            raise SystemExit("Proxy verification requires at least one project role to differ.")
        if not parse_csv(args.concept_keys):
            raise SystemExit("Proxy verification requires --concept-keys for the口径 checked across projects.")
        if not args.proxy_limitations or len(args.proxy_limitations.strip()) < 8:
            raise SystemExit("Proxy verification requires a specific --proxy-limitations note.")
        if not args.future_verification_plan or len(args.future_verification_plan.strip()) < 8:
            raise SystemExit("Proxy verification requires --future-verification-plan for target-environment verification.")
    if args.status == "skipped":
        if not args.user_confirmed:
            raise SystemExit("Skipped run evidence requires --user-confirmed after explicit user request.")
        if not args.skip_reason or len(args.skip_reason.strip()) < 8:
            raise SystemExit("Skipped run evidence requires a specific --skip-reason.")
        if not args.risk_note or len(args.risk_note.strip()) < 8:
            raise SystemExit("Skipped run evidence requires a specific --risk-note.")
        if not args.future_verification_plan or len(args.future_verification_plan.strip()) < 8:
            raise SystemExit("Skipped run evidence requires a specific --future-verification-plan.")

    timestamp = now_stamp()
    slug = slugify(args.slug or args.title or args.source_artifact, "run")
    run_id = f"{timestamp}_{slug}"
    record_target = f"runs/{run_id}.md"
    meta_target = f"runs/{run_id}.meta.json"
    record_member_id = f"run-{timestamp.lower()}-{slug}-record"
    meta_member_id = f"run-{timestamp.lower()}-{slug}-meta"
    created_at = now_iso()
    record = {
        "run_id": run_id,
        "package_id": package_id,
        "title": args.title or f"Run evidence for {args.source_artifact}",
        "source_artifact": str(source_member["member_id"]),
        "sql_path": str(source_member["path"]),
        "status": args.status,
        "row_count": args.row_count,
        "checked_metrics": parse_csv(args.checked_metrics),
        "checked_dimensions": parse_csv(args.checked_dimensions),
        "sample_fields": parse_csv(args.sample_fields),
        "result_summary": args.result_summary or "",
        "issues": args.issues or "",
        "user_confirmed": bool(args.user_confirmed),
        "skip_reason": args.skip_reason or "",
        "risk_note": args.risk_note or "",
        "future_verification_plan": args.future_verification_plan or "",
        "definition_project": args.definition_project or "",
        "execution_project": args.execution_project or "",
        "delivery_project": args.delivery_project or "",
        "concept_keys": parse_csv(args.concept_keys),
        "proxy_limitations": args.proxy_limitations or "",
        "confirmed_by": args.confirmed_by or "",
        "evidence_file": "",
        "result_file_type": result_file_type,
        "result_evidence_retention": {},
        "created_at": created_at,
        "notes": args.notes or "",
    }
    formal_root = root / FORMAL_ASSET_ROOT_REL
    formal_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sql-project-run-", dir=formal_root) as temporary:
        staging = Path(temporary)
        staged_record = staging / f"{run_id}.md"
        staged_meta = staging / f"{run_id}.meta.json"
        members: list[dict] = [
            {
                "member_id": record_member_id,
                "source_path": staged_record,
                "target_path": record_target,
                "role": "run_record",
            },
            {
                "member_id": meta_member_id,
                "source_path": staged_meta,
                "target_path": meta_target,
                "role": "run_meta",
            },
        ]
        lineage: list[dict] = [
            {
                "relation": "evidence_for",
                "from_member_id": record_member_id,
                "to_member_id": str(source_member["member_id"]),
            },
            {
                "relation": "describes",
                "from_member_id": meta_member_id,
                "to_member_id": record_member_id,
            },
        ]
        if evidence_source:
            if not evidence_source.exists():
                raise SystemExit(f"Evidence file not found: {evidence_source}")
            _reject_legacy_archive_path(root, evidence_source, label="Run evidence source")
            retained_result = prepare_result_evidence(evidence_source)
            staged_evidence = staging / f"{run_id}{retained_result.suffix}"
            write_retained_result(retained_result, staged_evidence)
            evidence_target = f"runs/{run_id}{retained_result.suffix}"
            evidence_member_id = f"run-{timestamp.lower()}-{slug}-result"
            evidence_rel = _formal_member_path(str(package["directory"]), evidence_target)
            record["evidence_file"] = evidence_rel
            record["result_file_type"] = retained_result.suffix
            record["result_evidence_retention"] = retained_result.retention
            record["row_count"] = (
                args.row_count
                if args.row_count is not None
                else retained_result.retention.get("source_row_count")
            )
            record["sample_fields"] = parse_csv(args.sample_fields) or list(
                retained_result.retention.get("columns") or []
            )
            members.append(
                {
                    "member_id": evidence_member_id,
                    "source_path": staged_evidence,
                    "target_path": evidence_target,
                    "role": "result_evidence",
                }
            )
            lineage.append(
                {
                    "relation": "result_for",
                    "from_member_id": evidence_member_id,
                    "to_member_id": record_member_id,
                }
            )
            source_kind = (
                "dashboard"
                if source_member.get("role") in FORMAL_SQL_ROLES["DASHBOARD"]
                else "query"
            )
            record.update(
                {
                    "contract_version": "sql_result_binding_v1",
                    "result_binding_id": record["run_id"],
                    "sql_asset_kind": source_kind,
                    "source_sql_fingerprint": execution_fingerprint(source_sql.read_text(encoding="utf-8-sig")),
                    "parameter_snapshot": analyze_sql_file(source_sql, kind=source_kind.upper()).get("params") or {},
                    "derived_outputs": [],
                }
            )
        record["path"] = _formal_member_path(str(package["directory"]), record_target)
        record["meta_path"] = _formal_member_path(str(package["directory"]), meta_target)
        body = [
            f"# {record['title']}",
            "",
            f"- run_id: {record['run_id']}",
            f"- package_id: {record['package_id']}",
            f"- source_artifact: {record['source_artifact']}",
            f"- sql_path: {markdown_value(record['sql_path'])}",
            f"- status: {record['status']}",
            f"- row_count: {markdown_value(record['row_count'])}",
            f"- checked_metrics: {markdown_value(record['checked_metrics'])}",
            f"- checked_dimensions: {markdown_value(record['checked_dimensions'])}",
            f"- sample_fields: {markdown_value(record['sample_fields'])}",
            f"- user_confirmed: {str(record['user_confirmed']).lower()}",
            f"- skip_reason: {markdown_value(record['skip_reason'])}",
            f"- risk_note: {markdown_value(record['risk_note'])}",
            f"- future_verification_plan: {markdown_value(record['future_verification_plan'])}",
            f"- definition_project: {markdown_value(record['definition_project'])}",
            f"- execution_project: {markdown_value(record['execution_project'])}",
            f"- delivery_project: {markdown_value(record['delivery_project'])}",
            f"- concept_keys: {markdown_value(record['concept_keys'])}",
            f"- proxy_limitations: {markdown_value(record['proxy_limitations'])}",
            f"- confirmed_by: {markdown_value(record['confirmed_by'])}",
            f"- evidence_file: {markdown_value(record['evidence_file'])}",
            f"- result_file_type: {markdown_value(record['result_file_type'])}",
            f"- result_evidence_retention: {markdown_value(record['result_evidence_retention'])}",
            f"- created_at: {record['created_at']}",
            "",
            "## Result Summary",
            "",
            record["result_summary"],
            "",
            "## Issues",
            "",
            record["issues"],
            "",
            "## Notes",
            "",
            record["notes"],
            "",
        ]
        staged_record.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
        write_json(staged_meta, record)
        try:
            plan = plan_formal_asset_package(
                root,
                package_id=package_id,
                title=str(package.get("title") or package_id),
                members=members,
                lineage=lineage,
            )
            receipt = apply_formal_asset_plan(plan)
        except FormalAssetRepositoryError as exc:
            raise SystemExit(str(exc)) from exc
    rebuild_index(root)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


def cmd_run_report(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    package_id = str(getattr(args, "package_id", None) or "").strip().upper()
    if not package_id:
        raise SystemExit("run-report requires --package-id FA-NNNN.")
    package = _load_package_context(root, package_id)
    rows = [
        _package_member_json(root, member)
        for member in package.get("members", [])
        if isinstance(member, dict) and member.get("role") == "run_meta"
    ]
    if args.source_artifact:
        rows = [item for item in rows if item.get("source_artifact") == args.source_artifact]
    if args.status:
        rows = [item for item in rows if item.get("status") == args.status]
    rows = sorted(rows, key=lambda item: item.get("created_at", ""))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("No run evidence.")
        return
    for item in rows:
        print(
            f"{item.get('run_id')} | {item.get('status')} | "
            f"confirmed={str(item.get('user_confirmed', False)).lower()} | "
            f"source={item.get('source_artifact')} | evidence={item.get('evidence_file', '')} | "
            f"path={item.get('path')}"
        )


def active_rules(root: Path) -> list[dict]:
    return load_rules(root, status="confirmed")


def rule_search_text(rule: dict) -> str:
    parts = [
        rule.get("rule_id", ""),
        rule.get("concept_key", ""),
        rule.get("title", ""),
        rule.get("content", ""),
        rule.get("source_evidence", ""),
        rule.get("applies_to", ""),
        rule.get("notes", ""),
        rule.get("status", ""),
    ]
    parts.extend(rule.get("affected_artifacts", []) or [])
    return " ".join(str(part) for part in parts)


def query_terms(value: str) -> list[str]:
    source = value or ""
    raw_terms = re.findall(r"[A-Za-z][A-Za-z0-9_]*|\d+|[\u4e00-\u9fff]+", source)
    terms: list[str] = []
    for raw in raw_terms:
        term = raw.strip().lower()
        if not term:
            continue
        terms.append(term)
    source_lower = source.lower()
    for phrase in RULE_CONTEXT_PHRASE_TERMS:
        if phrase.lower() in source_lower:
            terms.append(phrase.lower())
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        if len(term) == 1 and not term.isdigit():
            continue
        if term in RULE_CONTEXT_STOP_TERMS:
            continue
        if term not in seen:
            unique.append(term)
            seen.add(term)
    return unique


def is_weak_rule_context_term(term: str) -> bool:
    normalized = term.lower().strip()
    if normalized in RULE_CONTEXT_WEAK_TERMS:
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]{1,2}", normalized):
        return True
    return False


def rule_term_matches(text: str, term: str) -> bool:
    if not term:
        return False
    if re.fullmatch(r"[a-z][a-z0-9_]*", term):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text))
    if re.fullmatch(r"\d+", term):
        return bool(re.search(rf"(?<!\d){re.escape(term)}(?!\d)", text))
    if re.search(r"[a-z0-9]", term) and re.search(r"[\u4e00-\u9fff]", term):
        return normalize_signal(term) in normalize_signal(text)
    return term in text


def score_rule_detail(rule: dict, terms: list[str]) -> dict:
    high_signal_text = " ".join(
        str(rule.get(key, ""))
        for key in ["rule_id", "concept_key", "title", "applies_to"]
    ).lower()
    support_text = " ".join(
        str(rule.get(key, ""))
        for key in ["content", "source_evidence", "notes"]
    ).lower()
    score = 0
    strong_score = 0
    weak_score = 0
    matched_terms: list[dict] = []
    for term in terms:
        weak = is_weak_rule_context_term(term)
        numeric_only = bool(re.fullmatch(r"\d+", term))
        weight = 0
        surface = "none"
        if rule_term_matches(high_signal_text, term):
            surface = "high_signal"
            if weak:
                weight = 1
            elif numeric_only:
                weight = 4
            else:
                weight = 10 if re.fullmatch(r"[a-z][a-z0-9_]*", term) else 8
        elif not numeric_only and rule_term_matches(support_text, term):
            surface = "support"
            weight = 0 if weak else (4 if re.fullmatch(r"[a-z][a-z0-9_]*", term) else 3)
        if not weight:
            continue
        score += weight
        if weak:
            weak_score += weight
        else:
            strong_score += weight
        matched_terms.append({"term": term, "surface": surface, "strength": "weak" if weak else "strong", "weight": weight})
    if score > 0 and rule.get("status") == "confirmed":
        score += 2
    if strong_score > 0:
        relevance = "active"
    elif score > 0:
        relevance = "weak_candidate"
    else:
        relevance = "none"
    return {
        "score": score,
        "strong_score": strong_score,
        "weak_score": weak_score,
        "relevance": relevance,
        "matched_terms": matched_terms,
    }


def score_rule(rule: dict, terms: list[str]) -> int:
    return int(score_rule_detail(rule, terms).get("score", 0))


def normalize_signal(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def is_generic_source_field(value: str) -> bool:
    return normalize_signal(value) in REVERSE_AUDIT_GENERIC_FIELDS


def contains_signal(text: str, signal: str) -> bool:
    if not signal:
        return False
    signal_lower = str(signal).lower()
    if re.fullmatch(r"[a-z][a-z0-9_]*", signal_lower):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(signal_lower)}(?![a-z0-9_])", text))
    return signal_lower in text


def append_signal(values: list[str], value: str) -> None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return
    key = cleaned.lower()
    if key not in {item.lower() for item in values}:
        values.append(cleaned)


def read_rule_concept_registry(root: Path) -> dict:
    path = rule_concept_registry_path(root)
    if not path.exists():
        return {"concepts": [], "by_key": {}}
    registry = read_json(path, {"concepts": []})
    by_key = {
        str(item.get("concept_key", "")).strip(): item
        for item in registry.get("concepts", [])
        if item.get("concept_key")
    }
    registry["by_key"] = by_key
    return registry


def scan_intent_source(text: str) -> dict:
    lower = (text or "").lower()
    source_logs: list[str] = []
    source_fields: list[str] = []
    domains: list[str] = []
    metric_families: list[str] = []
    grain: list[str] = []
    evidence: list[dict] = []

    for log_name in INTENT_LOG_NAMES:
        if contains_signal(lower, log_name):
            canonical = "BattleLogInOut" if log_name.lower() == "battleloginout" else log_name
            append_signal(source_logs, canonical)
            evidence.append({"type": "source_log", "value": canonical})

    for physical_log in extract_sql_log_names(text):
        append_signal(source_logs, physical_log)
        evidence.append({"type": "source_log", "value": physical_log})

    for field_name in INTENT_FIELD_NAMES:
        if contains_signal(lower, field_name):
            append_signal(source_fields, field_name)
            evidence.append({"type": "source_field", "value": field_name})

    for domain, signals in INTENT_DOMAIN_SIGNALS.items():
        if any(contains_signal(lower, signal) for signal in signals):
            append_signal(domains, domain)
            evidence.append({"type": "domain", "value": domain})

    log_lowers = {item.lower() for item in source_logs}
    if log_lowers & {"matchend", "matchbegin", "roommatch"}:
        append_signal(domains, "matching")
    if "territory" in log_lowers:
        append_signal(domains, "territory")
    if "damage" in log_lowers:
        append_signal(domains, "damage")
    if "battledeathresurrection" in log_lowers:
        append_signal(domains, "damage")
    if log_lowers & {"battleloginout", "battleloginout"}:
        append_signal(domains, "battle")
    if "battlemission" in log_lowers:
        append_signal(domains, "mission")
    if log_lowers & {"playerlogin", "playerlogout"}:
        append_signal(domains, "retention")

    for family, signals in INTENT_METRIC_FAMILY_SIGNALS.items():
        if any(str(signal).lower() in lower for signal in signals):
            append_signal(metric_families, family)
            evidence.append({"type": "metric_family", "value": family})

    for grain_name, signals in INTENT_GRAIN_SIGNALS.items():
        if any(str(signal).lower() in lower for signal in signals):
            append_signal(grain, grain_name)
            evidence.append({"type": "grain", "value": grain_name})

    return {
        "domains": domains,
        "source_logs": source_logs,
        "source_fields": source_fields,
        "metric_families": metric_families,
        "grain": grain,
        "evidence": evidence,
    }


def apply_concept_hints(frame: dict, concept_key: str, concept_registry: dict) -> None:
    concept_key = str(concept_key or "").strip()
    if not concept_key:
        return
    lower = concept_key.lower()
    for hint, domain in CONCEPT_DOMAIN_HINTS.items():
        if hint in lower:
            append_signal(frame["domains"], domain)
    concept = concept_registry.get("by_key", {}).get(concept_key) or {}
    concept_text = " ".join(
        [concept_key, str(concept.get("label", ""))]
        + [str(item) for item in concept.get("keywords", []) or []]
    ).lower()
    concept_frame = scan_intent_source(concept_text)
    for key in ["domains", "source_logs", "source_fields", "metric_families", "grain"]:
        for value in concept_frame.get(key, []):
            append_signal(frame[key], value)


def build_intent_frame(
    *,
    query: str = "",
    metric: str = "",
    table: str = "",
    concept_key: str = "",
    candidate_sql: str = "",
    concept_registry: dict | None = None,
) -> dict:
    request_text = " ".join([query or "", metric or "", table or "", concept_key or ""])
    request_frame = scan_intent_source(request_text)
    apply_concept_hints(request_frame, concept_key, concept_registry or {"by_key": {}})
    sql_frame = scan_intent_source(candidate_sql or "")
    has_candidate_sql = bool((candidate_sql or "").strip())
    return {
        "domains": merge_frame_values(request_frame["domains"], sql_frame["domains"]),
        "source_logs": merge_frame_values(request_frame["source_logs"], sql_frame["source_logs"]),
        "source_fields": merge_frame_values(request_frame["source_fields"], sql_frame["source_fields"]),
        "metric_families": merge_frame_values(request_frame["metric_families"], sql_frame["metric_families"]),
        "grain": merge_frame_values(request_frame["grain"], sql_frame["grain"]),
        "request_evidence": request_frame["evidence"],
        "request_observed": {
            "source_logs": request_frame["source_logs"],
            "source_fields": request_frame["source_fields"],
            "domains": request_frame["domains"],
            "metric_families": request_frame["metric_families"],
            "grain": request_frame["grain"],
        },
        "candidate_sql_observed": {
            "source_logs": sql_frame["source_logs"],
            "source_fields": sql_frame["source_fields"],
            "domains": sql_frame["domains"],
        },
        "activation_basis": "request_plus_candidate_sql_observed" if has_candidate_sql else "request_metric_table_concept_only",
    }


def infer_hard_constraints_from_contract(contract: dict, rule: dict) -> list[dict]:
    constraints: list[dict] = []
    for item in contract.get("hard_constraints", []) or []:
        if not isinstance(item, dict):
            continue
        constraint = dict(item)
        constraint.setdefault("rule_id", rule.get("rule_id", ""))
        constraint.setdefault("concept_key", rule.get("concept_key", ""))
        constraint.setdefault("title", rule.get("title", ""))
        constraints.append(constraint)
    return constraints


def get_activation_contract(rule: dict) -> tuple[dict, str]:
    contract = rule.get("activation_contract")
    source = activation_contract_source(rule)
    if source == "stored_v2":
        return contract, "stored_v2"
    return {}, f"{source}_not_active"


def normalized_set(values: list[str]) -> set[str]:
    return {normalize_signal(item) for item in values if str(item or "").strip()}


def merge_frame_values(primary: list[str], secondary: list[str]) -> list[str]:
    values: list[str] = []
    for item in (primary or []) + (secondary or []):
        append_signal(values, str(item))
    return values


def hard_constraint_required_logs(contract: dict) -> list[str]:
    logs: list[str] = []
    for item in contract.get("hard_constraints", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"must_use_log", "do_not_substitute_log"}:
            append_signal(logs, str(item.get("log") or item.get("expected_log") or ""))
        if item.get("type") in {"must_use_battlesrvid_join_for_mode_attribution"}:
            append_signal(logs, str(item.get("join_log") or ""))
    return logs


def event_signature_required_logs(contract: dict) -> list[str]:
    signature = contract.get("event_signature") or {}
    logs: list[str] = []
    for value in listify(signature.get("required_logs") or signature.get("required_log")):
        append_signal(logs, str(value))
    return logs


def candidate_sql_source_gate(
    rule: dict,
    contract: dict,
    intent_frame: dict,
) -> dict | None:
    observed = (intent_frame.get("candidate_sql_observed", {}) or {})
    observed_logs = normalized_set(observed.get("source_logs", []) or [])
    if not observed_logs:
        return None
    observed_domains = normalized_set(observed.get("domains", []) or [])
    contract_gated_domains = normalized_set(contract.get("domains", []) or []) & RULE_CONTEXT_SOURCE_GATED_DOMAINS
    if contract_gated_domains and not (contract_gated_domains & observed_domains):
        return {
            "active": False,
            "reason": "candidate_sql_domain_mismatch",
            "matched_contract_evidence": [
                {
                    "type": "candidate_sql_domains",
                    "value": ", ".join(sorted(observed_domains)) or "none",
                },
                {
                    "type": "rule_source_gated_domains",
                    "value": ", ".join(sorted(contract_gated_domains)),
                },
            ],
        }

    must_logs = normalized_set(hard_constraint_required_logs(contract))
    if must_logs and not must_logs.issubset(observed_logs):
        return {
            "active": False,
            "reason": "candidate_sql_missing_required_source_log",
            "matched_contract_evidence": [
                {
                    "type": "candidate_sql_source_logs",
                    "value": ", ".join(sorted(observed_logs)),
                }
            ],
        }

    signature_logs = normalized_set(event_signature_required_logs(contract))
    if signature_logs and not (signature_logs & observed_logs):
        return {
            "active": False,
            "reason": "candidate_sql_source_log_mismatch",
            "matched_contract_evidence": [
                {
                    "type": "candidate_sql_source_logs",
                    "value": ", ".join(sorted(observed_logs)),
                },
                {
                    "type": "event_signature_required_logs",
                    "value": ", ".join(sorted(signature_logs)),
                },
            ],
        }

    contract_logs = normalized_set(contract.get("source_logs", []) or [])
    if not must_logs and contract_logs and not (contract_logs & observed_logs):
        return {
            "active": False,
            "reason": "candidate_sql_source_log_mismatch",
            "matched_contract_evidence": [
                {
                    "type": "candidate_sql_source_logs",
                    "value": ", ".join(sorted(observed_logs)),
                }
            ],
        }
    return None


def activation_contract_decision(
    rule: dict,
    detail: dict,
    intent_frame: dict,
    query_text: str,
    *,
    explicit_evidence: list[dict] | None = None,
    enforce_candidate_source_gate: bool = False,
) -> dict:
    contract, source = get_activation_contract(rule)
    exclusion_signatures = contract.get("request_exclusions") or []
    if not exclusion_signatures:
        exclusion_terms = [str(item) for item in contract.get("excludes_when", []) or [] if str(item or "").strip()]
        exclusion_signatures = [
            {
                "label": "contract request exclusion",
                "any_of": exclusion_terms,
                "all_of": [],
                "none_of": [],
            }
        ] if exclusion_terms else []
    exclusion_matches = request_signature_matches(exclusion_signatures, query_text)
    if exclusion_matches:
        return {
            "active": False,
            "excluded": True,
            "reason": "explicit_request_exclusion",
            "contract": contract,
            "contract_source": source,
            "matched_contract_evidence": [
                {
                    "type": "current_user_request_quote",
                    **quote,
                    "signature_label": str(item.get("label") or ""),
                }
                for item in exclusion_matches
                for quote in item.get("evidence_quotes", []) or []
            ],
        }
    matched_signatures = request_signature_matches(
        contract.get("request_signatures", []) or [],
        query_text,
    )
    raw_matched_quotes = [
        quote
        for item in matched_signatures
        for quote in item.get("evidence_quotes", []) or []
    ]
    matched_quotes: list[dict] = []
    for quote in sorted(
        raw_matched_quotes,
        key=lambda item: (int(item.get("start") or 0), -int(item.get("end") or 0)),
    ):
        start = int(quote.get("start") or 0)
        end = int(quote.get("end") or start)
        if any(
            int(existing.get("start") or 0) <= start
            and int(existing.get("end") or 0) >= end
            for existing in matched_quotes
        ):
            continue
        matched_quotes.append(quote)
    negated_quotes: list[dict] = []
    for quote in matched_quotes:
        start = int(quote.get("start") or 0)
        end = int(quote.get("end") or start)
        prefix_start = max(0, start - 12)
        suffix_end = min(len(query_text), end + 8)
        prefix = query_text[prefix_start:start]
        suffix = query_text[end:suffix_end]
        if re.search(r"(?:不要按|不要以|不要用|不要看|不要统计|不要限制|不要限定|不要|无需|不需要|不能|不可|不再|不按|不以|不用|不看|不统计|不限制|不限定|排除|去掉|删除|未)\s*$", prefix, flags=re.I) or re.match(
            r"^\s*(?:不要|无需|不需要|不能|不可|不限制|不限定|排除|去掉|删除)",
            suffix,
            flags=re.I,
        ):
            negated_quotes.append(
                {
                    "type": "current_user_request_negative_scope",
                    "signal": quote.get("signal", ""),
                    "quote": query_text[prefix_start:suffix_end].strip(),
                    "start": prefix_start,
                    "end": suffix_end,
                }
            )
    if matched_quotes and len(negated_quotes) == len(matched_quotes):
        return {
            "active": False,
            "excluded": True,
            "reason": "current_request_negates_scope",
            "contract": contract,
            "contract_source": source,
            "matched_contract_evidence": negated_quotes,
        }
    if explicit_evidence:
        return {
            "active": True,
            "excluded": False,
            "reason": "explicit_rule_or_concept_request",
            "contract": contract,
            "contract_source": source,
            "matched_contract_evidence": list(explicit_evidence),
        }
    if source != "stored_v2":
        return {
            "active": False,
            "excluded": False,
            "reason": "missing_v2_activation_contract",
            "contract": contract,
            "contract_source": source,
            "matched_contract_evidence": [],
        }
    policy = activation_policy(contract)
    if policy["forward"] != "automatic":
        return {
            "active": False,
            "excluded": False,
            "reason": f"forward_policy_{policy['forward']}",
            "contract": contract,
            "contract_source": source,
            "matched_contract_evidence": [],
        }
    if not matched_signatures:
        return {
            "active": False,
            "excluded": False,
            "reason": "request_signature_not_matched",
            "contract": contract,
            "contract_source": source,
            "matched_contract_evidence": [],
        }

    return {
        "active": True,
        "excluded": False,
        "reason": "request_signature_match",
        "contract": contract,
        "contract_source": source,
        "matched_contract_evidence": [
            {
                "type": "current_user_request_quote",
                **quote,
                "signature_label": str(item.get("label") or ""),
            }
            for item in matched_signatures
            for quote in item.get("evidence_quotes", []) or []
        ],
    }


def explicit_rule_selection_evidence(
    rule: dict,
    query_text: str,
    *,
    requested_concept_keys: set[str],
    requested_rule_id: str,
) -> list[dict]:
    """Prove that a CLI selector came from the current user message."""

    signals: list[tuple[str, str]] = []
    concept_key = str(rule.get("concept_key") or "").strip()
    rule_id = str(rule.get("rule_id") or "").strip()
    if concept_key and concept_key in requested_concept_keys:
        signals.append(("concept_key", concept_key))
    if requested_rule_id and rule_id == requested_rule_id:
        signals.append(("rule_id", rule_id))

    evidence: list[dict] = []
    for selector_type, signal in signals:
        match = request_signal_evidence(query_text, signal)
        if match is None:
            continue
        evidence.append(
            {
                "type": "current_user_explicit_rule_selection",
                "selector_type": selector_type,
                **match,
            }
        )
    return evidence


def rule_summary_payload(rule: dict, score: int, detail: dict, args, *, relevance: str) -> dict:
    return {
        "rule_id": rule.get("rule_id", ""),
        "version": rule.get("version", 0),
        "status": rule.get("status", ""),
        "concept_key": rule.get("concept_key", ""),
        "title": rule.get("title", ""),
        "applies_to": rule.get("applies_to", ""),
        "decision_question": rule.get("decision_question", ""),
        "relevance_score": score,
        "relevance": relevance,
        "strong_score": detail.get("strong_score", 0),
        "weak_score": detail.get("weak_score", 0),
        "matched_terms": detail.get("matched_terms", []),
        "content_excerpt": re.sub(r"\s+", " ", rule.get("content", "")).strip()[: args.excerpt_chars],
    }


def extract_rule_log_constraints(rule: dict) -> list[dict]:
    text = rule_search_text(rule)
    constraints: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for pattern in [
        r"只看\s*([A-Za-z][A-Za-z0-9_]*)\s*日志",
        r"使用\s*([A-Za-z][A-Za-z0-9_]*)\s*日志",
        r"事件日志[:：]\s*([A-Za-z][A-Za-z0-9_]*)",
    ]:
        for match in re.finditer(pattern, text):
            log_name = match.group(1)
            key = ("must_use_log", log_name.lower())
            if key in seen:
                continue
            seen.add(key)
            constraints.append(
                {
                    "type": "must_use_log",
                    "log": log_name,
                    "rule_id": rule.get("rule_id", ""),
                    "concept_key": rule.get("concept_key", ""),
                    "title": rule.get("title", ""),
                    "reason": match.group(0),
                }
            )
    if "不得用其他日志替代" in text or "不能用其他日志替代" in text:
        for constraint in list(constraints):
            if constraint.get("type") != "must_use_log":
                continue
            constraints.append(
                {
                    "type": "do_not_substitute_log",
                    "expected_log": constraint["log"],
                    "rule_id": rule.get("rule_id", ""),
                    "concept_key": rule.get("concept_key", ""),
                    "title": rule.get("title", ""),
                    "reason": "规则声明不得用其他日志替代该结果口径。",
                }
            )
    return constraints


def extract_sql_log_names(sql_text: str) -> list[str]:
    sql_no_comments = re.sub(r"--.*", "", sql_text or "")
    sql_no_comments = re.sub(r"/\*.*?\*/", "", sql_no_comments, flags=re.DOTALL)
    logs: list[str] = []
    for match in re.finditer(r"\b(?:[A-Za-z0-9_]+\.)?[A-Za-z0-9_]*_dsl_([A-Za-z0-9_]+)_fht0\b", sql_no_comments, re.IGNORECASE):
        logs.append(match.group(1))
    for match in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z][A-Za-z0-9_.]*)", sql_no_comments, re.IGNORECASE):
        source = match.group(1).split(".")[-1]
        source = re.sub(r"[^A-Za-z0-9_].*$", "", source)
        physical = re.search(r"_dsl_([A-Za-z0-9_]+)_fht0$", source, re.IGNORECASE)
        if physical:
            logs.append(physical.group(1))
        elif source.lower().startswith(("ads_", "dwd_", "dws_", "dim_")):
            logs.append(source)
        elif re.match(r"^[A-Z][A-Za-z0-9]{2,}$", source):
            logs.append(source)
        elif source.lower() in {"matchend", "matchbegin", "roommatch"}:
            logs.append(source)
    seen: set[str] = set()
    unique: list[str] = []
    for log in logs:
        normalized = log.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(log)
    return unique


def sql_identifier_leaf(value: str) -> str:
    cleaned = str(value or "").strip()
    cleaned = re.sub(r"^[`\"\[]|[`\"\]]$", "", cleaned)
    cleaned = re.sub(
        r"(?is)^cast\s*\(\s*([A-Za-z_][A-Za-z0-9_.`\"\[\]]*)\s+as\s+[A-Za-z0-9_()]+\s*\)$",
        r"\1",
        cleaned,
    )
    cleaned = re.sub(r"(?is)^(?:date|timestamp|string|int|bigint|double)\s*\(\s*([^)]+)\s*\)$", r"\1", cleaned)
    cleaned = cleaned.strip("`\"[] ")
    if "." in cleaned:
        cleaned = cleaned.split(".")[-1]
    return cleaned.strip("`\"[] ")


def normalize_sql_value(value: str) -> str:
    cleaned = str(value or "").strip().rstrip(",;")
    cleaned = cleaned.strip("`\"[] ")
    if len(cleaned) >= 2 and cleaned[0] == "'" and cleaned[-1] == "'":
        cleaned = cleaned[1:-1]
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def normalize_predicate_operator(operator: str) -> str:
    op = str(operator or "").strip().upper()
    if op == "!=":
        return "<>"
    return op


def normalize_predicate_text(predicate: str) -> str:
    text = str(predicate or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(
        r"(?is)cast\s*\(\s*([A-Za-z_][A-Za-z0-9_.`\"\[\]]*)\s+as\s+[A-Za-z0-9_()]+\s*\)",
        r"\1",
        text,
    )
    in_match = re.search(
        r"(?is)([A-Za-z_][A-Za-z0-9_.`\"\[\]]*)\s+IN\s*\(([^)]*)\)",
        text,
    )
    if in_match:
        field = sql_identifier_leaf(in_match.group(1))
        values = ",".join(
            normalize_sql_value(item)
            for item in in_match.group(2).split(",")
            if str(item or "").strip()
        )
        return f"{normalize_signal(field)}|IN|{values}"
    match = re.search(
        r"(?is)([A-Za-z_][A-Za-z0-9_.`\"\[\]]*)\s*(=|<>|!=|>=|<=|>|<)\s*('.*?'|[A-Za-z0-9_.:-]+)",
        text,
    )
    if not match:
        return normalize_signal(text)
    field = sql_identifier_leaf(match.group(1))
    op = normalize_predicate_operator(match.group(2))
    value = normalize_sql_value(match.group(3))
    return f"{normalize_signal(field)}|{op}|{value}"


def extract_sql_predicates(sql_text: str) -> list[dict]:
    sql_no_comments = strip_sql_comments(sql_text)
    predicates: list[dict] = []
    seen: set[str] = set()
    patterns = [
        r"(?is)(?:cast\s*\(\s*)?([A-Za-z_][A-Za-z0-9_.`\"\[\]]*)(?:\s+as\s+[A-Za-z0-9_()]+\s*\))?\s*(=|<>|!=|>=|<=|>|<)\s*('.*?'|[A-Za-z0-9_.:-]+)",
        r"(?is)([A-Za-z_][A-Za-z0-9_.`\"\[\]]*)\s+IN\s*\(([^)]*)\)",
    ]
    for match in re.finditer(patterns[0], sql_no_comments):
        field = sql_identifier_leaf(match.group(1))
        op = normalize_predicate_operator(match.group(2))
        value = normalize_sql_value(match.group(3))
        key = f"{normalize_signal(field)}|{op}|{value}"
        if key in seen:
            continue
        seen.add(key)
        predicates.append(
            {
                "field": field,
                "operator": op,
                "value": value,
                "normalized": key,
                "text": f"{field} {op} {value}",
            }
        )
    for match in re.finditer(patterns[1], sql_no_comments):
        field = sql_identifier_leaf(match.group(1))
        values = [
            normalize_sql_value(item)
            for item in match.group(2).split(",")
            if str(item or "").strip()
        ]
        key = f"{normalize_signal(field)}|IN|{','.join(values)}"
        if key in seen:
            continue
        seen.add(key)
        predicates.append(
            {
                "field": field,
                "operator": "IN",
                "value": values,
                "normalized": key,
                "text": f"{field} IN ({', '.join(values)})",
            }
        )
    return predicates


def predicate_value_shape(predicate: dict) -> str:
    operator = str(predicate.get("operator") or "").upper()
    if operator == "IN":
        return "set"
    if operator in RANGE_OPERATORS:
        return "range"
    return "equality"


def classify_id_range_predicate(predicate: dict) -> dict | None:
    field = str(predicate.get("field") or "").strip()
    if not field:
        return None
    field_norm = normalize_signal(field)
    operator = str(predicate.get("operator") or "").upper()
    category = ID_RANGE_FIELD_CATEGORIES.get(field_norm, "")
    if not category and operator in RANGE_OPERATORS:
        category = "numeric_range"
    if not category or operator not in ID_RANGE_OPERATORS:
        return None
    value = predicate.get("value")
    row = {
        "category": category,
        "field": field,
        "operator": operator,
        "value": value,
        "value_shape": predicate_value_shape(predicate),
        "normalized": predicate.get("normalized") or normalize_predicate_text(predicate.get("text", "")),
        "text": predicate.get("text") or "",
        "source": "predicate",
    }
    if category in {"item_id", "item_source_id", "mode_id", "zone_id", "game_server_id", "battle_server_id", "mission_id"}:
        row["id_type"] = category
    return row


def extract_id_range_evidence(predicates: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for predicate in predicates:
        row = classify_id_range_predicate(predicate)
        if not row:
            continue
        key = "|".join(
            [
                str(row.get("category") or ""),
                str(row.get("field") or "").lower(),
                str(row.get("operator") or ""),
                json.dumps(row.get("value"), ensure_ascii=False, sort_keys=True),
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def normalize_aggregation_signature(function_name: str, field: str, *, distinct: bool = False) -> str:
    func = str(function_name or "").upper()
    normalized_field = sql_identifier_leaf(field)
    if func == "RATIO":
        return "RATIO"
    if func == "COUNT" and distinct:
        return f"COUNT_DISTINCT({normalized_field})"
    if func == "COUNT" and normalize_signal(normalized_field) in {"*", "1"}:
        return "COUNT(*)"
    return f"{func}({normalized_field})"


def extract_sql_aggregations(sql_text: str) -> list[dict]:
    sql_no_comments = strip_sql_comments(sql_text)
    aggregations: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"(?is)\b(SUM|COUNT|MAX|MIN|AVG)\s*\(\s*(DISTINCT\s+)?([^)]+?)\s*\)"
        r"(?:\s+AS\s+(?:`([^`]+)`|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_]*)))?"
    )
    for match in pattern.finditer(sql_no_comments):
        func = match.group(1).upper()
        distinct = bool(match.group(2))
        raw_field = match.group(3).strip()
        field = sql_identifier_leaf(raw_field)
        alias = next((item for item in match.groups()[3:] if item), "")
        signature = normalize_aggregation_signature(func, field, distinct=distinct)
        key = "|".join([signature.lower(), str(alias).lower()])
        if key in seen:
            continue
        seen.add(key)
        aggregations.append(
            {
                "function": func,
                "field": field,
                "distinct": distinct,
                "alias": alias,
                "signature": signature,
                "text": match.group(0).strip(),
            }
        )
    return aggregations


def extract_final_metrics(sql_text: str) -> list[dict]:
    select_list = extract_final_select_list(sql_text)
    if not select_list:
        return extract_final_metric_aliases(sql_text)
    metrics: list[dict] = []
    seen: set[str] = set()
    for part in split_top_level_csv(select_list):
        parsed = parse_select_expression(part)
        alias = parsed.get("alias", "")
        expression = parsed.get("expression", "")
        if not alias:
            leaf = sql_identifier_leaf(expression)
            alias = leaf if leaf and normalize_signal(leaf) != normalize_signal(expression) else ""
        key = "|".join([str(alias).lower(), str(expression).lower()])
        if key in seen:
            continue
        seen.add(key)
        expr_aggs = extract_sql_aggregations(expression)
        metrics.append(
            {
                "alias": alias,
                "expression": expression,
                "text": parsed.get("text", part),
                "aggregation_signatures": [item.get("signature", "") for item in expr_aggs],
            }
        )
    return metrics


def extract_ratio_aggregations(final_metrics: list[dict]) -> list[dict]:
    ratios: list[dict] = []
    seen: set[str] = set()
    for metric in final_metrics:
        alias = str(metric.get("alias") or "")
        expression = str(metric.get("expression") or metric.get("text") or "")
        if not expression:
            continue
        if "/" not in expression and not re.search(r"(?is)(rate|ratio|占比|比例|率)", alias):
            continue
        key = "|".join([alias.lower(), expression.lower()])
        if key in seen:
            continue
        seen.add(key)
        ratios.append(
            {
                "function": "RATIO",
                "field": "",
                "distinct": False,
                "alias": alias,
                "signature": "RATIO",
                "text": expression,
                "source": "final_metric",
            }
        )
    return ratios


SQL_ALIAS_SOURCE_SKIP_TOKENS = {
    "as",
    "and",
    "or",
    "case",
    "when",
    "then",
    "else",
    "end",
    "cast",
    "coalesce",
    "if",
    "ifnull",
    "nullif",
    "sum",
    "count",
    "distinct",
    "max",
    "min",
    "avg",
    "bigint",
    "int",
    "double",
    "string",
    "timestamp",
    "date",
    "from",
    "where",
    "group",
    "by",
}


def expression_source_fields(expression: str, alias: str = "") -> list[str]:
    alias_norm = normalize_signal(alias)
    fields: list[str] = []
    for token in re.findall(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)", expression or ""):
        token_norm = normalize_signal(token)
        if not token_norm or token_norm == alias_norm or token_norm in SQL_ALIAS_SOURCE_SKIP_TOKENS:
            continue
        if token.isdigit():
            continue
        append_signal(fields, token)
    return fields


def extract_select_alias_sources(sql_text: str) -> dict[str, dict[str, list]]:
    sql_no_comments = strip_sql_comments(sql_text)
    alias_sources: dict[str, dict[str, list]] = {}
    for match in re.finditer(r"\bselect\b(.*?)\bfrom\b", sql_no_comments, flags=re.I | re.S):
        for expression in split_top_level_csv(match.group(1)):
            parsed = parse_select_expression(expression)
            alias = str(parsed.get("alias") or "").strip()
            if not alias:
                continue
            body = str(parsed.get("expression") or "")
            fields = expression_source_fields(body, alias)
            aggregations = extract_sql_aggregations(body)
            if not fields and not aggregations:
                continue
            key = normalize_signal(alias)
            existing = alias_sources.setdefault(key, {"fields": [], "aggregations": []})
            for source in fields:
                append_signal(existing["fields"], source)
            seen_aggregations = {
                str(item.get("signature") or "").lower()
                for item in existing["aggregations"]
                if isinstance(item, dict)
            }
            for aggregation in aggregations:
                signature = str(aggregation.get("signature") or "").lower()
                if signature in seen_aggregations:
                    continue
                seen_aggregations.add(signature)
                existing["aggregations"].append(aggregation)
    return alias_sources


def extract_select_alias_source_fields(sql_text: str) -> dict[str, list[str]]:
    return {
        alias: list((sources or {}).get("fields", []))
        for alias, sources in extract_select_alias_sources(sql_text).items()
    }


def extract_final_metric_aggregations(sql_text: str, final_metrics: list[dict]) -> list[dict]:
    alias_sources = extract_select_alias_sources(sql_text)
    aggregations: list[dict] = []
    seen: set[str] = set()

    def append_final_aggregation(item: dict, source: str, key_suffix: str) -> None:
        row = dict(item)
        row["source"] = source
        key = "|".join([str(row.get("signature", "")).lower(), str(row.get("alias", "")).lower(), key_suffix])
        if key in seen:
            return
        seen.add(key)
        aggregations.append(row)

    def append_alias_source_aggregations(alias_field: str, final_alias: str, via: str) -> None:
        sources = alias_sources.get(normalize_signal(alias_field), {})
        for upstream in sources.get("aggregations", []) if isinstance(sources, dict) else []:
            if not isinstance(upstream, dict):
                continue
            row = dict(upstream)
            row["alias"] = final_alias or row.get("alias", "")
            row["source"] = "final_metric_alias_source_aggregation"
            row["lineage_via"] = via
            append_final_aggregation(row, row["source"], "upstream_agg")

    for metric in final_metrics:
        alias = str(metric.get("alias") or "")
        expression = str(metric.get("expression") or metric.get("text") or "")
        direct_aggregations = extract_sql_aggregations(expression)
        if not direct_aggregations:
            append_alias_source_aggregations(sql_identifier_leaf(expression), alias, sql_identifier_leaf(expression))
        for item in direct_aggregations:
            item = dict(item)
            item["alias"] = alias or item.get("alias", "")
            append_final_aggregation(item, "final_metric", "final")
            append_alias_source_aggregations(str(item.get("field", "")), alias, str(item.get("field", "")))
            field_key = normalize_signal(item.get("field", ""))
            sources = alias_sources.get(field_key, {})
            for source_field in sources.get("fields", []) if isinstance(sources, dict) else []:
                if normalize_signal(source_field) == field_key:
                    continue
                lineaged = dict(item)
                lineaged["field"] = source_field
                lineaged["signature"] = normalize_aggregation_signature(
                    str(item.get("function") or ""),
                    source_field,
                    distinct=bool(item.get("distinct")),
                )
                lineaged["source"] = "final_metric_alias_lineage"
                lineaged["lineage_via"] = item.get("field", "")
                append_final_aggregation(lineaged, lineaged["source"], "lineage")
    for item in extract_ratio_aggregations(final_metrics):
        item = dict(item)
        item["source"] = "final_metric"
        key = "|".join([str(item.get("signature", "")).lower(), str(item.get("alias", "")).lower(), "ratio"])
        if key in seen:
            continue
        seen.add(key)
        aggregations.append(item)
    return aggregations


def infer_sql_metric_roles(sql_text: str, aggregations: list[dict]) -> list[str]:
    roles: list[str] = []
    sql_lower = (sql_text or "").lower()
    has_quantity_sum = any(
        item.get("function") == "SUM" and normalize_signal(item.get("field", "")) == "battleitemdelta"
        for item in aggregations
    )
    has_duration_max = any(
        item.get("function") in {"MAX", "SUM"}
        and normalize_signal(item.get("field", "")) in {"totalactiveduration", "onlinetime", "matchduration"}
        for item in aggregations
    )
    has_battle_server_time_anchor = any(
        item.get("function") == "MIN"
        and normalize_signal(item.get("field", "")) in {"dteventtime", "dteventdate"}
        for item in aggregations
    ) and contains_signal(sql_lower, "BattleSrvId")
    if has_quantity_sum:
        append_signal(roles, "quantity")
    if has_duration_max or has_battle_server_time_anchor or any(term in sql_lower for term in ["duration", "时长", "耗时"]):
        append_signal(roles, "duration")
    craft_progression_hint = bool(
        re.search(r"(?is)(craft_level|creation_level|progress_level|造物等级|养成等级|制造等级|制作等级)", sql_text or "")
        and sql_contains_term(sql_text or "", "BattleItemChangeSource = 'Craft'")
    )
    if craft_progression_hint:
        append_signal(roles, "classification")
        append_signal(roles, "progression_level")
    for item in aggregations:
        func = item.get("function")
        alias_lower = str(item.get("alias") or "").lower()
        field_norm = normalize_signal(item.get("field", ""))
        if field_norm in {"gamemode", "matchmode"} and func in {"MAX", "MIN"}:
            append_signal(roles, "classification")
        if field_norm in {"gamemode", "matchmode"} and func == "COUNT" and item.get("distinct"):
            append_signal(roles, "classification")
            append_signal(roles, "support_count")
        if func == "RATIO":
            append_signal(roles, "ratio")
            if re.search(r"(penetration|hit|presence|是否|渗透|命中|获得)", alias_lower):
                append_signal(roles, "penetration")
            continue
        if func == "COUNT" and item.get("distinct"):
            if has_quantity_sum and re.search(r"(support|sample|player_cnt|user_cnt|cnt|人数|玩家数|战斗服数|样本)", alias_lower):
                append_signal(roles, "support_count")
            elif re.search(r"(penetration|rate|hit|presence|是否|渗透|命中|获得人数|获得玩家)", alias_lower):
                append_signal(roles, "penetration")
                append_signal(roles, "presence")
            elif has_quantity_sum:
                append_signal(roles, "support_count")
            else:
                append_signal(roles, "presence")
        elif func == "COUNT":
            if field_norm in {"*", "1"}:
                append_signal(roles, "event_count")
            else:
                append_signal(roles, "support_count" if has_quantity_sum else "event_count")
        elif func in {"MAX", "MIN"} and (
            field_norm in {"craftlevel", "creationlevel", "progresslevel"}
            or re.search(r"(?is)(craft|creation|progress|level|造物|养成|制造|制作|等级)", alias_lower)
        ):
            append_signal(roles, "classification")
            append_signal(roles, "progression_level")
    if (
        any(role == "duration" for role in roles)
        and re.search(r"(?is)(bucket|bucket_order|分桶|桶|档位|区间|分布)", sql_text or "")
    ):
        append_signal(roles, "distribution_bucket")
        append_signal(roles, "classification")
    if re.search(r"(?is)(/|rate|ratio|占比|比例|率)", sql_text or ""):
        append_signal(roles, "ratio")
        if any(role in roles for role in ["presence", "penetration"]):
            append_signal(roles, "penetration")
    return roles


def extract_final_metric_aliases(sql_text: str) -> list[dict]:
    sql_no_comments = strip_sql_comments(sql_text)
    aliases: list[dict] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"(?is)\bAS\s+(?:`([^`]+)`|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9_]*))",
        sql_no_comments,
    ):
        alias = next((item for item in match.groups() if item), "")
        key = alias.lower()
        if not alias or key in seen:
            continue
        seen.add(key)
        aliases.append({"alias": alias})
    return aliases


def extract_group_by_fields(sql_text: str) -> list[str]:
    sql_no_comments = strip_sql_comments(sql_text)
    fields: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"(?is)\bGROUP\s+BY\b(.*?)(?=\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|\bUNION\b|\)|$)",
        sql_no_comments,
    ):
        for expression in split_top_level_csv(match.group(1)):
            leaf = sql_identifier_leaf(expression)
            if not leaf:
                continue
            key = normalize_signal(leaf)
            if key in seen:
                continue
            seen.add(key)
            fields.append(leaf)
    return fields


def build_field_role_evidence(
    predicates: list[dict],
    aggregations: list[dict],
    final_metric_aggregations: list[dict],
    final_metrics: list[dict],
    group_by_fields: list[str],
    id_range_evidence: list[dict],
) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(field: Any, role: str, source: str) -> None:
        leaf = sql_identifier_leaf(str(field or ""))
        role = str(role or "").strip()
        if not leaf or not role:
            return
        key = (normalize_signal(leaf), normalize_signal(role), str(source or ""))
        if key in seen:
            return
        seen.add(key)
        rows.append({"field": leaf, "role": role, "source": source})

    for predicate in predicates:
        add(predicate.get("field"), "predicate", "predicate")
    for item in id_range_evidence:
        add(item.get("field"), str(item.get("category") or "id_or_range"), "id_range_evidence")
    for field in group_by_fields:
        add(field, "group_by", "group_by")
    for aggregation in aggregations:
        add(aggregation.get("field"), "aggregation", "aggregation")
    for aggregation in final_metric_aggregations:
        add(aggregation.get("field"), "final_aggregation", "final_metric")
    for metric in final_metrics:
        expression = str(metric.get("expression") or metric.get("text") or "")
        if not expression:
            continue
        if extract_sql_aggregations(expression):
            for field in expression_source_fields(expression, str(metric.get("alias") or "")):
                add(field, "final_metric", "final_select")
            continue
        leaf = sql_identifier_leaf(expression)
        if leaf:
            add(leaf, "final_dimension", "final_select")
            add(leaf, "final_output", "final_select")
        for field in expression_source_fields(expression, str(metric.get("alias") or "")):
            add(field, "final_output", "final_select")
    return rows


def extract_sql_evidence(sql_text: str) -> dict:
    frame = scan_intent_source(sql_text)
    predicates = extract_sql_predicates(sql_text)
    final_metrics = extract_final_metrics(sql_text)
    final_metric_aggregations = extract_final_metric_aggregations(sql_text, final_metrics)
    aggregations = extract_sql_aggregations(sql_text)
    existing_aggregation_keys = {str(item.get("signature", "")).lower() + "|" + str(item.get("alias", "")).lower() for item in aggregations}
    for item in extract_ratio_aggregations(final_metrics):
        key = str(item.get("signature", "")).lower() + "|" + str(item.get("alias", "")).lower()
        if key in existing_aggregation_keys:
            continue
        existing_aggregation_keys.add(key)
        aggregations.append(item)
    roles = infer_sql_metric_roles(sql_text, aggregations)
    final_metric_roles = infer_sql_metric_roles(sql_text, final_metric_aggregations)
    id_range_evidence = extract_id_range_evidence(predicates)
    group_by_fields = extract_group_by_fields(sql_text)
    field_role_evidence = build_field_role_evidence(
        predicates,
        aggregations,
        final_metric_aggregations,
        final_metrics,
        group_by_fields,
        id_range_evidence,
    )
    return {
        "sql_text_lower": (sql_text or "").lower(),
        "source_logs": frame.get("source_logs", []),
        "source_fields": frame.get("source_fields", []),
        "domains": frame.get("domains", []),
        "predicates": predicates,
        "predicate_signatures": [item["normalized"] for item in predicates],
        "ids_and_ranges": id_range_evidence,
        "id_range_evidence": id_range_evidence,
        "aggregations": aggregations,
        "aggregation_signatures": [item["signature"] for item in aggregations],
        "metric_roles": roles,
        "final_metric_aggregations": final_metric_aggregations,
        "final_metric_aggregation_signatures": [item["signature"] for item in final_metric_aggregations],
        "final_metric_roles": final_metric_roles,
        "final_metrics": final_metrics,
        "group_by_fields": group_by_fields,
        "field_role_evidence": field_role_evidence,
    }


def sql_contains_term(sql_text: str, term: str) -> bool:
    if not term:
        return False
    return contains_signal((sql_text or "").lower(), str(term))


def contract_source_signature(contract: dict) -> dict:
    signature = contract.get("source_signature")
    if not isinstance(signature, dict):
        signature = {}
    logs = list(signature.get("source_logs") or signature.get("logs") or contract.get("source_logs") or [])
    fields = list(signature.get("source_fields") or signature.get("fields") or contract.get("source_fields") or [])
    key_fields = list(signature.get("key_fields") or signature.get("dedup_key_fields") or [])
    fields.extend(key_fields)
    required_terms = list(signature.get("required_terms") or [])
    required_terms.extend(signature.get("required_conditions") or [])
    required_terms.extend(signature.get("aggregation_terms") or [])
    required_terms.extend(signature.get("dedup_terms") or [])
    for constraint in contract.get("hard_constraints", []) or []:
        if not isinstance(constraint, dict):
            continue
        if constraint.get("type") == "must_use_field":
            append_signal(fields, str(constraint.get("field") or ""))
            append_signal(logs, str(constraint.get("log") or ""))
        if constraint.get("type") == "must_use_log":
            append_signal(logs, str(constraint.get("log") or ""))
    return {
        "source_logs": unique_in_order([str(item) for item in logs if str(item or "").strip()]),
        "source_fields": unique_in_order([str(item) for item in fields if str(item or "").strip()]),
        "key_fields": unique_in_order([str(item) for item in key_fields if str(item or "").strip()]),
        "required_terms": unique_in_order([str(item) for item in required_terms if str(item or "").strip()]),
    }


def normalize_required_aggregation(value: str, signature: dict) -> list[str]:
    term = str(value or "").strip()
    if not term:
        return []
    normalized_term = term.upper().replace(" ", "")
    aggs: list[str] = []
    direct = re.search(r"(?is)\b(SUM|COUNT|MAX|MIN|AVG)\s*\(\s*(DISTINCT\s+)?([^)]+?)\s*\)", term)
    if direct:
        aggs.append(
            normalize_aggregation_signature(
                direct.group(1),
                direct.group(3),
                distinct=bool(direct.group(2)),
            )
        )
        return aggs
    if normalized_term in {"COUNTDISTINCT", "COUNT+DISTINCT"} or ("COUNT" in normalized_term and "DISTINCT" in normalized_term):
        aggs.append("COUNT_DISTINCT")
        return aggs
    if normalized_term in {"SUM", "MAX", "MIN", "AVG", "COUNT"}:
        fields = signature.get("key_fields") or signature.get("source_fields") or []
        non_generic_fields = [
            field
            for field in fields
            if not is_generic_source_field(str(field))
        ]
        if len(non_generic_fields) == 1:
            aggs.append(normalize_aggregation_signature(normalized_term, non_generic_fields[0]))
        else:
            aggs.append(normalized_term)
    return aggs


def derive_metric_roles_from_contract(contract: dict) -> list[str]:
    families = normalized_set(contract.get("metric_families", []) or [])
    roles: list[str] = []
    if "quantitysum" in families or "quantity" in families:
        append_signal(roles, "quantity")
    if "penetration" in families:
        append_signal(roles, "penetration")
        append_signal(roles, "presence")
    if "eventcount" in families:
        append_signal(roles, "event_count")
    if "gameduration" in families:
        append_signal(roles, "duration")
    return roles


def normalize_field_role_requirement(value) -> dict | None:
    if not value:
        return None
    if isinstance(value, dict):
        field = str(value.get("field") or value.get("name") or "").strip()
        roles = contract_list_values(value, "roles", "role", "any_roles", "any_role")
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if ":" in text:
            field, role_text = text.split(":", 1)
            roles = [item.strip() for item in re.split(r"[|,，、/]", role_text) if item.strip()]
        else:
            field = text
            roles = []
    if not field:
        return None
    roles = unique_in_order([str(item) for item in roles if str(item or "").strip()])
    return {"field": field, "roles": roles}


def contract_event_signature(contract: dict) -> dict:
    explicit = contract.get("event_signature")
    has_explicit_signature = isinstance(explicit, dict)
    if isinstance(explicit, dict):
        source = explicit
    else:
        source = {}
    source_signature = contract_source_signature(contract)
    required_logs = contract_list_values(source, "required_logs", "required_log")
    source_signature_logs = listify(source_signature.get("source_logs"))
    if not has_explicit_signature or not required_logs:
        required_logs.extend(source_signature_logs)

    required_predicates = contract_list_values(
        source,
        "required_predicates",
        "required_predicate",
        "required_conditions",
        "required_condition",
    )
    required_aggregations = contract_list_values(source, "required_aggregations", "required_aggregation")
    required_any_aggregations = contract_list_values(source, "required_any_aggregations", "required_any_aggregation")
    required_roles = contract_list_values(source, "required_metric_roles", "required_metric_role")
    required_any_roles = contract_list_values(source, "required_any_metric_roles", "required_any_metric_role")
    required_field_roles = [
        item
        for item in (
            normalize_field_role_requirement(value)
            for value in contract_list_values(source, "required_field_roles", "required_field_role")
        )
        if item
    ]
    required_text_terms = contract_list_values(source, "required_text_terms", "required_text_term")
    required_any_text_terms = contract_list_values(
        source,
        "required_any_text_terms",
        "required_any_text_term",
    )
    incompatible_predicates = contract_list_values(source, "incompatible_predicates", "incompatible_predicate")
    incompatible_roles = contract_list_values(source, "incompatible_metric_roles", "incompatible_metric_role")
    incompatible_concepts = contract_list_values(
        source,
        "incompatible_concept_keys",
        "incompatible_concept_key",
        "mutually_exclusive_concept_keys",
        "mutually_exclusive_concept_key",
    )
    match_policy = (
        source.get("match_policy")
        or source.get("match_scope")
        or contract.get("match_policy")
        or contract.get("match_scope")
        or ""
    )

    source_required_terms = []
    if not has_explicit_signature:
        source_required_terms = [str(item) for item in source_signature.get("required_terms", []) or []]
    has_count_distinct_terms = (
        any(normalize_signal(item) == "count" for item in source_required_terms)
        and any(normalize_signal(item) == "distinct" for item in source_required_terms)
    )
    if has_count_distinct_terms:
        required_aggregations.append("COUNT_DISTINCT")
    for term in source_required_terms:
        predicate = normalize_predicate_text(str(term))
        if "|" in predicate:
            required_predicates.append(str(term))
        aggs = [] if has_count_distinct_terms and normalize_signal(term) in {"count", "distinct"} else normalize_required_aggregation(str(term), source_signature)
        required_aggregations.extend(aggs)
        if "|" not in predicate and not aggs and normalize_signal(term) not in {"count", "distinct"}:
            required_text_terms.append(str(term))
    negative = contract.get("negative_signature")
    if isinstance(negative, dict):
        incompatible_predicates.extend(contract_list_values(negative, "predicates", "predicate"))
        incompatible_predicates.extend(contract_list_values(negative, "required_terms", "required_term"))
        incompatible_predicates.extend(contract_list_values(negative, "source_fields", "source_field"))
        incompatible_roles.extend(contract_list_values(negative, "metric_roles", "metric_role"))

    for item in contract.get("hard_constraints", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "must_use_log":
            required_logs.append(str(item.get("log") or ""))
        if item.get("type") == "must_filter":
            required_predicates.append(str(item.get("condition") or ""))
        if item.get("type") == "must_scope_field":
            required_field_roles.append(
                {
                    "field": str(item.get("field") or ""),
                    "roles": ["predicate", "group_by", "final_dimension", "final_output"],
                }
            )
        if item.get("type") == "must_aggregate":
            required_aggregations.append(str(item.get("expression") or ""))
        if item.get("type") == "must_not_use_field":
            incompatible_predicates.append(str(item.get("field") or ""))
        if item.get("type") == "must_aggregate_by_player_battle_max_then_sum":
            field = str(item.get("field") or "")
            if field:
                required_aggregations.append(f"MAX({field})")

    if not required_roles and not required_any_roles:
        required_roles.extend(derive_metric_roles_from_contract(contract))

    return {
        "required_logs": unique_in_order([str(item) for item in required_logs if str(item or "").strip()]),
        "required_predicates": unique_in_order([str(item) for item in required_predicates if str(item or "").strip()]),
        "required_predicate_signatures": unique_in_order(
            [
                normalize_predicate_text(str(item))
                for item in required_predicates
                if "|" in normalize_predicate_text(str(item))
            ]
        ),
        "required_metric_roles": unique_in_order([str(item) for item in required_roles if str(item or "").strip()]),
        "required_any_metric_roles": unique_in_order([str(item) for item in required_any_roles if str(item or "").strip()]),
        "required_aggregations": unique_in_order([str(item) for item in required_aggregations if str(item or "").strip()]),
        "required_any_aggregations": unique_in_order([str(item) for item in required_any_aggregations if str(item or "").strip()]),
        "required_field_roles": required_field_roles,
        "required_text_terms": unique_in_order([str(item) for item in required_text_terms if str(item or "").strip()]),
        "required_any_text_terms": unique_in_order([str(item) for item in required_any_text_terms if str(item or "").strip()]),
        "incompatible_predicates": unique_in_order([str(item) for item in incompatible_predicates if str(item or "").strip()]),
        "incompatible_predicate_signatures": unique_in_order(
            [
                normalize_predicate_text(str(item))
                for item in incompatible_predicates
                if "|" in normalize_predicate_text(str(item))
            ]
        ),
        "incompatible_metric_roles": unique_in_order([str(item) for item in incompatible_roles if str(item or "").strip()]),
        "incompatible_concept_keys": unique_in_order([str(item) for item in incompatible_concepts if str(item or "").strip()]),
        "match_policy": str(match_policy or ""),
        "source": "stored_event_signature" if has_explicit_signature else "derived_from_activation_contract",
    }


def aggregation_requirement_matches(required: str, observed_aggregations: list[dict]) -> bool:
    required_text = str(required or "").strip()
    if not required_text:
        return True
    normalized_required = required_text.upper().replace(" ", "")
    for item in observed_aggregations:
        signature = str(item.get("signature") or "")
        signature_compact = signature.upper().replace(" ", "")
        if normalized_required == signature_compact:
            return True
        if normalized_required == str(item.get("function") or "").upper():
            return True
        if normalized_required == "COUNT_DISTINCT" and item.get("function") == "COUNT" and item.get("distinct"):
            return True
        direct = normalize_required_aggregation(required_text, {"source_fields": [item.get("field", "")]})
        if any(candidate.upper().replace(" ", "") == signature_compact for candidate in direct):
            return True
    return False


def event_signature_match(signature: dict, evidence: dict, *, shared_log_match: bool = False) -> dict:
    observed_logs = normalized_set(evidence.get("source_logs", []) or [])
    observed_predicates = set(evidence.get("predicate_signatures", []) or [])
    observed_roles = normalized_set(
        evidence.get("final_metric_roles", []) if shared_log_match and evidence.get("final_metric_roles") is not None else evidence.get("metric_roles", [])
    )
    observed_aggregations = (
        evidence.get("final_metric_aggregations", [])
        if shared_log_match and evidence.get("final_metric_aggregations") is not None
        else evidence.get("aggregations", [])
    ) or []

    required_logs = normalized_set(signature.get("required_logs", []) or [])
    required_predicates = set(signature.get("required_predicate_signatures", []) or [])
    required_roles = normalized_set(signature.get("required_metric_roles", []) or [])
    required_any_roles = normalized_set(signature.get("required_any_metric_roles", []) or [])
    required_aggregations = signature.get("required_aggregations", []) or []
    required_any_aggregations = signature.get("required_any_aggregations", []) or []
    required_field_roles = signature.get("required_field_roles", []) or []
    required_text_terms = [str(item) for item in signature.get("required_text_terms", []) or []]
    required_any_text_terms = [str(item) for item in signature.get("required_any_text_terms", []) or []]
    incompatible_predicates = set(signature.get("incompatible_predicate_signatures", []) or [])
    incompatible_roles = normalized_set(signature.get("incompatible_metric_roles", []) or [])
    boundary_only = normalize_signal(signature.get("match_policy", "")) in BOUNDARY_ONLY_EVENT_SIGNATURE_POLICIES
    sql_text_lower = str(evidence.get("sql_text_lower") or "")

    matched_logs = sorted(required_logs & observed_logs)
    missing_logs = sorted(required_logs - observed_logs) if required_logs else []
    missing_predicates = sorted(required_predicates - observed_predicates)
    matched_predicates = sorted(required_predicates & observed_predicates)
    matched_roles = sorted(required_roles & observed_roles)
    missing_roles = sorted(required_roles - observed_roles)
    matched_any_roles = sorted(required_any_roles & observed_roles)
    missing_any_roles = sorted(required_any_roles) if required_any_roles and not matched_any_roles else []
    missing_aggregations = [
        item
        for item in required_aggregations
        if not aggregation_requirement_matches(str(item), observed_aggregations)
    ]
    matched_aggregations = [
        item
        for item in required_aggregations
        if aggregation_requirement_matches(str(item), observed_aggregations)
    ]
    matched_any_aggregations = [
        item
        for item in required_any_aggregations
        if aggregation_requirement_matches(str(item), observed_aggregations)
    ]
    missing_any_aggregations = list(required_any_aggregations) if required_any_aggregations and not matched_any_aggregations else []
    observed_field_roles = {
        (normalize_signal(item.get("field")), normalize_signal(item.get("role")))
        for item in evidence.get("field_role_evidence", []) or []
        if isinstance(item, dict) and item.get("field") and item.get("role")
    }
    matched_field_roles: list[dict] = []
    missing_field_roles: list[dict] = []
    for requirement in required_field_roles:
        if not isinstance(requirement, dict):
            continue
        field = str(requirement.get("field") or "").strip()
        field_norm = normalize_signal(field)
        roles = [str(item) for item in requirement.get("roles", []) or [] if str(item or "").strip()]
        role_norms = [normalize_signal(item) for item in roles]
        if not role_norms:
            hit = any(observed_field == field_norm for observed_field, _ in observed_field_roles)
        else:
            hit = any((field_norm, role_norm) in observed_field_roles for role_norm in role_norms)
        if hit:
            matched_field_roles.append({"field": field, "roles": roles})
        else:
            missing_field_roles.append({"field": field, "roles": roles})
    missing_text_terms = [
        item
        for item in required_text_terms
        if not sql_contains_term(sql_text_lower, item)
    ]
    matched_text_terms = [
        item
        for item in required_text_terms
        if sql_contains_term(sql_text_lower, item)
    ]
    matched_any_text_terms = [
        item
        for item in required_any_text_terms
        if sql_contains_term(sql_text_lower, item)
    ]
    missing_any_text_terms = list(required_any_text_terms) if required_any_text_terms and not matched_any_text_terms else []
    incompatible_hits = sorted(incompatible_predicates & observed_predicates)
    incompatible_role_hits = sorted(incompatible_roles & observed_roles)

    if required_logs and not matched_logs:
        return {
            "strength": "",
            "reason": "required_log_not_observed",
            "matched_evidence": [],
            "missing_evidence": [
                {"type": "required_log", "value": item}
                for item in missing_logs
            ],
        }

    complete = (
        not missing_logs
        and not missing_predicates
        and not missing_roles
        and not missing_any_roles
        and not missing_aggregations
        and not missing_any_aggregations
        and not missing_field_roles
        and not missing_text_terms
        and not missing_any_text_terms
        and not incompatible_hits
        and not incompatible_role_hits
    )
    has_structural_requirements = bool(
        required_predicates
        or required_roles
        or required_any_roles
        or required_aggregations
        or required_any_aggregations
        or required_field_roles
        or required_text_terms
        or required_any_text_terms
    )
    matched_structural_evidence = bool(
        matched_predicates
        or matched_roles
        or matched_any_roles
        or matched_aggregations
        or matched_any_aggregations
        or matched_field_roles
        or matched_text_terms
        or matched_any_text_terms
    )
    has_any_signature = bool(required_logs or has_structural_requirements)
    has_boundary_evidence_requirement = bool(required_predicates or required_field_roles)
    matched_boundary_evidence = bool(matched_predicates or matched_field_roles)
    shared_log_has_core_signature = (
        not shared_log_match
        or bool((required_roles or required_any_roles) and (required_aggregations or required_any_aggregations))
    )
    if not has_any_signature:
        strength = ""
        reason = ""
    elif boundary_only and (matched_logs or matched_structural_evidence):
        strength = "partial" if matched_structural_evidence else "weak"
        reason = "boundary_only_event_signature"
    elif complete and (
        not shared_log_match
        or (
            has_structural_requirements
            and shared_log_has_core_signature
            and has_boundary_evidence_requirement
            and matched_boundary_evidence
        )
    ):
        strength = "exact"
        reason = "event_signature_match"
    elif matched_logs or matched_structural_evidence:
        strength = "partial" if has_structural_requirements and matched_structural_evidence else "weak"
        reason = "event_signature_incomplete"
    else:
        strength = "weak" if shared_log_match and matched_logs else ""
        reason = "shared_log_without_event_signature" if strength else ""

    missing_evidence: list[dict] = []
    for item in missing_logs:
        missing_evidence.append({"type": "required_log", "value": item})
    for item in missing_predicates:
        missing_evidence.append({"type": "required_predicate", "value": item})
    for item in missing_roles:
        missing_evidence.append({"type": "required_metric_role", "value": item})
    for item in missing_any_roles:
        missing_evidence.append({"type": "required_any_metric_role", "value": item})
    for item in missing_aggregations:
        missing_evidence.append({"type": "required_aggregation", "value": item})
    for item in missing_any_aggregations:
        missing_evidence.append({"type": "required_any_aggregation", "value": item})
    for item in missing_field_roles:
        role_text = ",".join(item.get("roles") or [])
        missing_evidence.append({"type": "required_field_role", "value": f"{item.get('field')}:{role_text}" if role_text else item.get("field")})
    for item in missing_text_terms:
        missing_evidence.append({"type": "required_text_term", "value": item})
    for item in missing_any_text_terms:
        missing_evidence.append({"type": "required_any_text_term", "value": item})
    for item in incompatible_hits:
        missing_evidence.append({"type": "incompatible_predicate_present", "value": item})
    for item in incompatible_role_hits:
        missing_evidence.append({"type": "incompatible_metric_role_present", "value": item})

    matched_evidence: list[dict] = []
    for item in matched_logs:
        matched_evidence.append({"type": "source_log", "value": item})
    for item in matched_predicates:
        matched_evidence.append({"type": "predicate", "value": item})
    for item in matched_roles:
        matched_evidence.append({"type": "metric_role", "value": item})
    for item in matched_any_roles:
        matched_evidence.append({"type": "metric_role", "value": item})
    for item in matched_aggregations:
        matched_evidence.append({"type": "aggregation", "value": item})
    for item in matched_any_aggregations:
        matched_evidence.append({"type": "aggregation", "value": item})
    for item in matched_field_roles:
        role_text = ",".join(item.get("roles") or [])
        matched_evidence.append({"type": "field_role", "value": f"{item.get('field')}:{role_text}" if role_text else item.get("field")})
    for item in matched_text_terms:
        matched_evidence.append({"type": "text_term", "value": item})
    for item in matched_any_text_terms:
        matched_evidence.append({"type": "any_text_term", "value": item})

    return {
        "strength": strength,
        "reason": reason,
        "matched_evidence": matched_evidence,
        "missing_evidence": missing_evidence,
    }


def mismatch_conflicts_with_active_constraints(active_rule_rows: list[dict], match: dict) -> bool:
    matched_values = [
        str(item.get("value") or "")
        for item in match.get("matched_evidence", []) or []
        if item.get("value")
    ]
    matched_norms = {normalize_signal(value) for value in matched_values}
    matched_predicates = {
        str(item.get("value") or "")
        for item in match.get("matched_evidence", []) or []
        if item.get("type") == "predicate" and item.get("value")
    }
    matched_roles = {
        normalize_signal(item.get("value"))
        for item in match.get("matched_evidence", []) or []
        if item.get("type") == "metric_role" and item.get("value")
    }
    matched_logs = {
        normalize_signal(item.get("value"))
        for item in match.get("matched_evidence", []) or []
        if item.get("type") == "source_log"
    }
    reverse_concept = normalize_signal(match.get("concept_key", ""))
    for row in active_rule_rows:
        signature = row.get("event_signature") if isinstance(row.get("event_signature"), dict) else {}
        incompatible_concepts = normalized_set(signature.get("incompatible_concept_keys", []) or [])
        if reverse_concept and reverse_concept in incompatible_concepts:
            return True
        incompatible_roles = normalized_set(signature.get("incompatible_metric_roles", []) or [])
        if incompatible_roles and (incompatible_roles & matched_roles):
            return True
        incompatible_predicates = set(signature.get("incompatible_predicate_signatures", []) or [])
        if incompatible_predicates and (incompatible_predicates & matched_predicates):
            return True
        for constraint in row.get("constraints", []) or []:
            ctype = constraint.get("type")
            if ctype == "must_not_use_log":
                forbidden = normalize_signal(constraint.get("log", ""))
                if forbidden and forbidden in matched_logs:
                    return True
            if ctype == "must_not_use_field":
                forbidden = normalize_signal(constraint.get("field", ""))
                if forbidden and any(forbidden in value for value in matched_norms):
                    return True
            if ctype == "do_not_substitute_log":
                expected = normalize_signal(constraint.get("expected_log", ""))
                if expected and any(log != expected for log in matched_logs):
                    return True
    return False


def sql_field_hits(sql_text: str, fields: list[str]) -> list[str]:
    hits: list[str] = []
    for field in fields:
        if sql_contains_term(sql_text, field):
            append_signal(hits, str(field))
    return hits


def source_metric_audit(candidate_sql: str, rules: list[dict], concept_registry: dict) -> dict:
    if not (candidate_sql or "").strip():
        return {"status": "not_run", "observed": {}, "matches": []}
    observed_evidence = extract_sql_evidence(candidate_sql)
    observed_logs = normalized_set(observed_evidence.get("source_logs", []) or [])
    matches: list[dict] = []
    for rule in rules:
        contract, contract_source = get_activation_contract(rule)
        policy = activation_policy(contract) if contract_source == "stored_v2" else {"forward": "explicit_only", "reverse": "disabled"}
        if policy["reverse"] == "disabled":
            continue
        signature = contract_source_signature(contract)
        event_signature = contract_event_signature(contract)
        expected_logs = normalized_set(signature.get("source_logs", []) or [])
        expected_fields = normalized_set(signature.get("source_fields", []) or [])
        explicit_key_fields = normalized_set(signature.get("key_fields", []) or [])
        matched_logs = sorted(expected_logs & observed_logs)
        shared_log_match = bool(matched_logs and (set(matched_logs) & REVERSE_AUDIT_SHARED_LOGS))
        matched_fields = sql_field_hits(candidate_sql, signature.get("source_fields", []) or [])
        matched_key_fields = [
            field
            for field in matched_fields
            if not is_generic_source_field(field)
            or normalize_signal(field) in explicit_key_fields
        ]
        missing_terms = [
            term
            for term in signature.get("required_terms", []) or []
            if not sql_contains_term(candidate_sql, term)
        ]
        evidence: list[dict] = []
        for item in matched_logs:
            evidence.append({"type": "source_log", "value": item})
        for item in matched_fields:
            evidence.append({"type": "source_field", "value": item})

        strength = ""
        reason = ""
        event_match = event_signature_match(event_signature, observed_evidence, shared_log_match=shared_log_match)
        if event_match.get("strength"):
            strength = str(event_match.get("strength") or "")
            reason = str(event_match.get("reason") or "")
            evidence = event_match.get("matched_evidence", []) or evidence
            missing_evidence = event_match.get("missing_evidence", [])
        else:
            missing_evidence = [{"type": "required_term", "value": term} for term in missing_terms]
            if expected_logs and not matched_logs:
                strength = ""
                reason = "required_source_log_not_observed"
            elif expected_logs and matched_logs and expected_fields and matched_key_fields:
                if missing_terms:
                    strength = "partial"
                    reason = "source_log_and_field_match_but_signature_terms_missing"
                else:
                    strength = "exact"
                    reason = "source_log_and_field_signature_match"
            elif expected_logs and matched_logs and not expected_fields:
                strength = "weak"
                reason = "source_log_only_match"
            elif expected_logs and matched_logs:
                strength = "weak"
                reason = "source_log_match_without_key_field"
            elif not expected_logs and expected_fields and matched_key_fields:
                strength = "partial" if not missing_terms else "weak"
                reason = "source_field_match_without_log_signature"

        if shared_log_match and strength == "exact" and reason != "event_signature_match":
            strength = "partial"
            reason = "shared_log_requires_event_signature_for_exact"

        if not strength:
            continue
        matches.append(
            {
                "rule_id": rule.get("rule_id", ""),
                "version": rule.get("version", 0),
                "status": rule.get("status", ""),
                "concept_key": rule.get("concept_key", ""),
                "title": rule.get("title", ""),
                "strength": strength,
                "reason": reason,
                "activation_contract_source": contract_source,
                "reverse_policy": policy["reverse"],
                "event_signature_source": event_signature.get("source", ""),
                "matched_evidence": evidence,
                "missing_evidence": missing_evidence,
            }
        )
    strength_order = {"exact": 0, "partial": 1, "weak": 2}
    matches.sort(key=lambda item: (strength_order.get(item.get("strength", ""), 9), item.get("rule_id", "")))
    return {
        "status": "ok" if matches else "no_source_metric_matches",
        "observed": {
            "source_logs": observed_evidence.get("source_logs", []),
            "source_fields": observed_evidence.get("source_fields", []),
            "domains": observed_evidence.get("domains", []),
            "predicates": observed_evidence.get("predicates", []),
            "aggregations": observed_evidence.get("aggregations", []),
            "aggregation_signatures": observed_evidence.get("aggregation_signatures", []),
            "metric_roles": observed_evidence.get("metric_roles", []),
            "final_metric_aggregations": observed_evidence.get("final_metric_aggregations", []),
            "final_metric_aggregation_signatures": observed_evidence.get("final_metric_aggregation_signatures", []),
            "final_metric_roles": observed_evidence.get("final_metric_roles", []),
            "final_metrics": observed_evidence.get("final_metrics", []),
            "group_by_fields": observed_evidence.get("group_by_fields", []),
            "field_role_evidence": observed_evidence.get("field_role_evidence", []),
            "ids_and_ranges": observed_evidence.get("ids_and_ranges", []),
            "id_range_evidence": observed_evidence.get("id_range_evidence", []),
        },
        "matches": matches,
    }


def name_logic_mismatch_rows(active_rule_rows: list[dict], reverse_audit: dict) -> list[dict]:
    if not active_rule_rows or not isinstance(reverse_audit, dict):
        return []
    forward_concepts = {str(item.get("concept_key") or "") for item in active_rule_rows if item.get("concept_key")}
    forward_rule_ids = {str(item.get("rule_id") or "") for item in active_rule_rows if item.get("rule_id")}
    rows: list[dict] = []
    for match in reverse_audit.get("matches", []) or []:
        if match.get("strength") != "exact":
            continue
        concept = str(match.get("concept_key") or "")
        rule_id = str(match.get("rule_id") or "")
        if concept in forward_concepts or rule_id in forward_rule_ids:
            continue
        severity = (
            "blocker"
            if match.get("reverse_policy") == "exact_only"
            and mismatch_conflicts_with_active_constraints(active_rule_rows, match)
            else "diagnostic"
        )
        rows.append(
            {
                "type": "name_logic_mismatch",
                "severity": severity,
                "forward_concept_keys": sorted(forward_concepts),
                "forward_rule_ids": sorted(forward_rule_ids),
                "reverse_concept_key": concept,
                "reverse_rule_id": rule_id,
                "reverse_rule_title": match.get("title", ""),
                "reason": (
                    "用户请求正向口径与候选 SQL 的日志/字段签名指向不同且互斥的 confirmed 口径。"
                    if severity == "blocker"
                    else "候选 SQL exact 命中另一个 confirmed 口径；当前未发现与正向 hard constraints 的明确互斥，作为诊断输出。"
                ),
                "reverse_evidence": match.get("matched_evidence", []),
            }
        )
    return rows


def scan_lower_bound_matches(candidate_sql: str, constraint: dict) -> bool:
    field = str(constraint.get("field") or "").strip()
    expected_value = str(constraint.get("value") or "").strip()
    if not field or not expected_value:
        return False
    cleaned = strip_sql_comments(candidate_sql)
    escaped_field = re.escape(field)
    escaped_value = re.escape(expected_value)
    literal_pattern = (
        rf"\b(?:[A-Za-z_][\w$]*\.)?`?{escaped_field}`?\s*>=\s*"
        rf"(?:date\s*\(\s*)?'{escaped_value}'\s*\)?"
    )
    if re.search(literal_pattern, cleaned, flags=re.I):
        return True

    for alias, expression in params_cte_expressions(candidate_sql).items():
        if literal_value(expression) != expected_value:
            continue
        escaped_alias = re.escape(alias)
        parameter_pattern = (
            rf"\b(?:[A-Za-z_][\w$]*\.)?`?{escaped_field}`?\s*>=\s*"
            rf"(?:[A-Za-z_][\w$]*\.)?`?{escaped_alias}`?\b"
        )
        if re.search(parameter_pattern, cleaned, flags=re.I):
            return True
    return False


def candidate_rule_check(candidate_sql: str, constraints: list[dict], *, mode: str = "generation") -> dict:
    candidate_logs = extract_sql_log_names(candidate_sql)
    candidate_log_lowers = {log.lower() for log in candidate_logs}
    evidence = extract_sql_evidence(candidate_sql)
    candidate_predicates = set(evidence.get("predicate_signatures", []) or [])
    candidate_aggregations = evidence.get("final_metric_aggregations", []) or []
    blockers: list[dict] = []
    warnings: list[dict] = []

    must_logs = [item for item in constraints if item.get("type") == "must_use_log"]
    for constraint in must_logs:
        expected = str(constraint.get("log", ""))
        if expected and expected.lower() not in candidate_log_lowers:
            blockers.append(
                {
                    "type": "missing_required_log",
                    "expected_log": expected,
                    "actual_logs": candidate_logs,
                    "rule_id": constraint.get("rule_id", ""),
                    "message": f"相关 confirmed 口径要求使用 {expected} 日志，但候选 SQL 未读取该日志。",
                }
            )
    for constraint in constraints:
        if constraint.get("type") != "must_use_field":
            continue
        expected_field = str(constraint.get("field", ""))
        expected_log = str(constraint.get("log", ""))
        if expected_field and not sql_contains_term(candidate_sql, expected_field):
            blockers.append(
                {
                    "type": "missing_required_field",
                    "expected_field": expected_field,
                    "expected_log": expected_log,
                    "actual_logs": candidate_logs,
                    "rule_id": constraint.get("rule_id", ""),
                    "message": f"相关 confirmed 口径要求使用字段 {expected_field}，但候选 SQL 未引用该字段。",
                }
            )
    for constraint in constraints:
        if constraint.get("type") != "must_filter":
            continue
        condition = str(constraint.get("condition", ""))
        normalized = normalize_predicate_text(condition)
        if "|" in normalized and normalized not in candidate_predicates:
            blockers.append(
                {
                    "type": "missing_required_filter",
                    "expected_condition": condition,
                    "actual_logs": candidate_logs,
                    "rule_id": constraint.get("rule_id", ""),
                    "message": f"相关 confirmed 口径要求过滤条件 {condition}，但候选 SQL 未体现该条件。",
                }
            )
    for constraint in constraints:
        if constraint.get("type") != "must_scan_from_date":
            continue
        if scan_lower_bound_matches(candidate_sql, constraint):
            continue
        expected_field = str(constraint.get("field") or "")
        expected_value = str(constraint.get("value") or "")
        blockers.append(
            {
                "type": "missing_required_scan_start",
                "expected_field": expected_field,
                "expected_condition": f"{expected_field} >= {expected_value}",
                "actual_logs": candidate_logs,
                "rule_id": constraint.get("rule_id", ""),
                "concept_key": constraint.get("concept_key", ""),
                "reason": constraint.get("reason", ""),
                "message": (
                    f"相关 confirmed 口径要求从 {expected_value} 起扫描 {expected_field}，"
                    "候选 SQL 未通过顶部参数或直接谓词体现该下界。"
                ),
            }
        )
    for constraint in constraints:
        if constraint.get("type") != "must_scope_field":
            continue
        expected_field = str(constraint.get("field", "")).strip()
        expected_roles = [
            normalize_signal(item)
            for item in listify(constraint.get("roles") or ["predicate", "group_by", "final_dimension", "final_output"])
            if str(item or "").strip()
        ]
        if not expected_field:
            continue
        observed_roles = [
            item
            for item in evidence.get("field_role_evidence", []) or []
            if normalize_signal(item.get("field")) == normalize_signal(expected_field)
            and (not expected_roles or normalize_signal(item.get("role")) in expected_roles)
        ]
        if not observed_roles:
            blockers.append(
                {
                    "type": "missing_scope_field",
                    "expected_field": expected_field,
                    "expected_roles": expected_roles,
                    "actual_logs": candidate_logs,
                    "rule_id": constraint.get("rule_id", ""),
                    "concept_key": constraint.get("concept_key", ""),
                    "message": f"相关 confirmed 口径要求声明字段 {expected_field} 的统计范围，但候选 SQL 未在筛选、分组或最终输出中体现该字段。",
                }
            )
    for constraint in constraints:
        if constraint.get("type") != "must_aggregate":
            continue
        expression = str(constraint.get("expression", ""))
        if expression and not aggregation_requirement_matches(expression, candidate_aggregations):
            blockers.append(
                {
                    "type": "missing_required_aggregation",
                    "expected_expression": expression,
                    "actual_logs": candidate_logs,
                    "rule_id": constraint.get("rule_id", ""),
                    "message": f"相关 confirmed 口径要求最终输出指标聚合 {expression}，但候选 SQL 最终指标未体现该聚合。",
                }
            )
    for constraint in constraints:
        if constraint.get("type") != "must_not_use_log":
            continue
        forbidden = str(constraint.get("log", ""))
        if forbidden and forbidden.lower() in candidate_log_lowers:
            blockers.append(
                {
                    "type": "forbidden_log",
                    "forbidden_log": forbidden,
                    "actual_logs": candidate_logs,
                    "rule_id": constraint.get("rule_id", ""),
                    "message": f"相关 confirmed 口径禁止使用 {forbidden} 日志；候选 SQL 读取了该日志。",
                }
            )
    for constraint in constraints:
        if constraint.get("type") != "must_not_use_field":
            continue
        forbidden_field = str(constraint.get("field", ""))
        if forbidden_field and sql_contains_term(candidate_sql, forbidden_field):
            blockers.append(
                {
                    "type": "forbidden_field",
                    "forbidden_field": forbidden_field,
                    "actual_logs": candidate_logs,
                    "rule_id": constraint.get("rule_id", ""),
                    "message": f"相关 confirmed 口径禁止使用字段 {forbidden_field}；候选 SQL 引用了该字段。",
                }
            )
    for constraint in constraints:
        if constraint.get("type") != "must_use_authoritative_table":
            continue
        expected_table = str(constraint.get("table", "")).strip()
        if not expected_table:
            continue
        expected_leaf = expected_table.split(".")[-1].lower()
        has_authoritative = expected_table.lower() in candidate_sql.lower() or expected_leaf in candidate_log_lowers
        proxy_logs = [
            str(item.get("log") or "")
            for item in constraints
            if item.get("type") == "proxy_source_allowed_for_temporary" and item.get("log")
        ]
        proxy_hits = [log for log in proxy_logs if log.lower() in candidate_log_lowers]
        temporary_proxy_allowed = "temporary" in {
            str(item or "").strip().lower()
            for item in listify(constraint.get("unless_status"))
        }
        if not has_authoritative and mode == "temporary" and temporary_proxy_allowed:
            if proxy_hits:
                warnings.append(
                    {
                        "type": "temporary_proxy_source",
                        "expected_table": expected_table,
                        "proxy_logs": proxy_hits,
                        "actual_logs": candidate_logs,
                        "rule_id": constraint.get("rule_id", ""),
                        "concept_key": constraint.get("concept_key", ""),
                        "message": (
                            f"临时 SQL 使用声明的代理来源 {', '.join(proxy_hits)}；"
                            f"正式交付仍需替换为 {expected_table}。"
                        ),
                    }
                )
            else:
                blockers.append(
                    {
                        "type": "missing_allowed_temporary_proxy_source",
                        "expected_table": expected_table,
                        "expected_proxy_logs": proxy_logs,
                        "actual_logs": candidate_logs,
                        "rule_id": constraint.get("rule_id", ""),
                        "concept_key": constraint.get("concept_key", ""),
                        "enforce_in_temporary": True,
                        "message": (
                            f"该指标临时查询只能使用权威表 {expected_table} 或规则声明的代理来源 "
                            f"{', '.join(proxy_logs) or '（未配置）'}；候选 SQL 使用了其他来源。"
                        ),
                    }
                )
            continue
        if not has_authoritative:
            blockers.append(
                {
                    "type": "missing_authoritative_table",
                    "expected_table": expected_table,
                    "proxy_logs": proxy_hits,
                    "actual_logs": candidate_logs,
                    "rule_id": constraint.get("rule_id", ""),
                    "message": (
                        f"相关 confirmed 口径要求正式交付使用权威表 {expected_table}；候选 SQL 未读取该表。"
                        + (f" 当前只看到代理来源 {', '.join(proxy_hits)}，只能作为临时/代理验证。" if proxy_hits else "")
                    ),
                }
            )
    for constraint in constraints:
        if constraint.get("type") != "do_not_substitute_log":
            continue
        expected = str(constraint.get("expected_log", ""))
        expected_lower = expected.lower()
        substitutes = [
            log
            for log in candidate_logs
            if "match" in log.lower() and log.lower() != expected_lower
        ]
        if substitutes:
            blockers.append(
                {
                    "type": "forbidden_log_substitution",
                    "expected_log": expected,
                    "actual_logs": substitutes,
                    "rule_id": constraint.get("rule_id", ""),
                    "message": f"相关 confirmed 口径要求 {expected}，且不得用其他匹配日志替代；候选 SQL 使用了 {', '.join(substitutes)}。",
                }
            )
    return {
        "candidate_logs": candidate_logs,
        "blockers": blockers,
        "warnings": warnings,
        "status": "conflict" if blockers else "ok",
    }


def project_execution_contract_check(
    candidate_sql: str,
    config: dict,
    *,
    execution_route: dict | None = None,
) -> dict:
    """Check project execution constraints, reusing a route when available."""

    if route_matches_context(execution_route, candidate_sql, config):
        route = execution_route
        effective_config = effective_config_from_route(config, route)
    else:
        effective_config, detection = effective_config_for_sql(config, candidate_sql)
        route = execution_route_for_sql(
            candidate_sql,
            config,
            effective_config=effective_config,
            detection=detection,
        )
    time_contract = route.get("time_contract") if isinstance(route.get("time_contract"), dict) else None
    if time_contract is None:
        # Legacy route receipts do not carry the bundled contract yet.
        time_contract = analyze_time_contract(candidate_sql, effective_config)
    blockers = [
        {
            "type": finding.get("code", "project_time_contract"),
            "rule_id": "project_config.partition_policy",
            "concept_key": "project-time-contract",
            "message": finding.get("message", "项目时间契约不满足。"),
            "source": "project_config",
        }
        for finding in time_contract.get("findings", []) or []
    ]
    for message in route.get("blockers", []) or []:
        blockers.append(
            {
                "type": "execution_profile_contract",
                "rule_id": "project_config.execution_adapters",
                "concept_key": "project-execution-profile",
                "message": str(message),
                "source": "project_config",
            }
        )
    identifier_findings = identifier_policy_findings(candidate_sql, effective_config)
    for finding in identifier_findings:
        blockers.append(
            {
                "type": finding.get("code", "identifier_policy"),
                "rule_id": "project_config.identifier_policy",
                "concept_key": "project-identifier-policy",
                "message": finding.get("message", "SQL 标识符不满足执行环境契约。"),
                "source": "project_config",
            }
        )
    business_scope = config.get("business_scope") or {}
    default_zone = business_scope.get("default_zone") or {}
    zone_identifier = business_scope.get("zone_identifier") or {}
    scope_findings: list[dict] = []
    if business_scope.get("contract_version") == "project_business_scope_v1":
        expected_zone = default_zone.get("value")
        parameter_alias = str(default_zone.get("parameter_alias") or "zone_id")
        params = params_cte_expressions(candidate_sql)
        if parameter_alias in params and expected_zone is not None:
            numeric_match = re.search(r"(?<![A-Za-z0-9_])-?\d+(?![A-Za-z0-9_])", params[parameter_alias])
            actual = numeric_match.group(0) if numeric_match else ""
            if actual and int(actual) != int(expected_zone):
                scope_findings.append(
                    {
                        "type": "project_default_zone_mismatch",
                        "message": f"项目默认大区为 iZoneAreaID={expected_zone}，但 params.{parameter_alias}={actual}。",
                    }
                )
        sql_without_comments = strip_sql_comments(candidate_sql)
        for match in re.finditer(
            r"(?is)\biZoneAreaID\b\s*=\s*['\"]?(\d+)['\"]?",
            sql_without_comments,
        ):
            actual = int(match.group(1))
            if expected_zone is not None and actual != int(expected_zone):
                scope_findings.append(
                    {
                        "type": "project_default_zone_mismatch",
                        "message": f"项目默认大区为 iZoneAreaID={expected_zone}，但 SQL 使用 iZoneAreaID={actual}。",
                    }
                )
        for field in zone_identifier.get("non_equivalent_fields", []) or []:
            escaped = re.escape(str(field))
            patterns = (
                rf"(?is)\b{escaped}\b\s+(?:AS\s+)?(?:`?{re.escape(parameter_alias)}`?|`?zone_id`?)\b",
                rf"(?is)\b{escaped}\b\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?`?{re.escape(parameter_alias)}`?\b",
                rf"(?is)\biZoneAreaID\b\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?`?{escaped}`?\b",
            )
            if any(re.search(pattern, sql_without_comments) for pattern in patterns):
                scope_findings.append(
                    {
                        "type": "non_equivalent_zone_identifier",
                        "message": f"{field} 不是业务大区字段 iZoneAreaID，不能别名或比较成 {parameter_alias}/zone_id。",
                    }
                )
    for finding in scope_findings:
        blockers.append(
            {
                **finding,
                "rule_id": "project_config.business_scope",
                "concept_key": "project-business-scope",
                "source": "project_config",
            }
        )
    privacy_transforms = sql_side_privacy_transforms(candidate_sql)
    if privacy_transforms:
        functions = sorted({item["function"] for item in privacy_transforms})
        blockers.append(
            {
                "type": "sql_side_privacy_transform",
                "rule_id": "skill.sql_privacy_handling",
                "concept_key": "sql-privacy-handling",
                "functions": functions,
                "expressions": [item["expression"] for item in privacy_transforms],
                "message": (
                    "SQL 禁止为了脱敏使用 MD5/SHA/HASH/BASE64/AES/MASK 等变换；"
                    "保留业务所需原始字段，由 DA 侧统一处理隐私。"
                ),
                "source": "sql_engineering_skill",
            }
        )
    return {
        "status": "conflict" if blockers else ("ok" if time_contract.get("status") != "not_applicable" else "not_applicable"),
        "sql_fingerprint": route_sql_fingerprint(candidate_sql),
        "config_fingerprint": route_config_fingerprint(config),
        "blockers": blockers,
        "warnings": [],
        "time_contract": time_contract,
        "execution_route": route,
        "identifier_contract": {
            "status": "conflict" if identifier_findings else "ok",
            "findings": identifier_findings,
            "config": effective_config.get("identifier_policy") or {},
        },
        "privacy_contract": {
            "status": "conflict" if privacy_transforms else "ok",
            "handling_owner": "DA",
            "sql_side_deidentification_forbidden": True,
            "transforms": privacy_transforms,
        },
        "business_scope_contract": {
            "status": "conflict" if scope_findings else (
                "ok" if business_scope.get("contract_version") == "project_business_scope_v1" else "not_configured"
            ),
            "findings": scope_findings,
            "config": business_scope,
        },
    }


def compose_generation_gate(candidate_check: dict | None, project_contract_check: dict | None) -> dict:
    if candidate_check is None and project_contract_check is None:
        return {"status": "not_run", "blockers": [], "warnings": []}
    blockers: list = []
    warnings: list = []
    for check in [candidate_check, project_contract_check]:
        if not isinstance(check, dict):
            continue
        blockers.extend(check.get("blockers", []) or [])
        warnings.extend(check.get("warnings", []) or [])
    statuses = {
        str(check.get("status") or "")
        for check in [candidate_check, project_contract_check]
        if isinstance(check, dict)
    }
    if "error" in statuses:
        status = "error"
    elif blockers or "conflict" in statuses:
        status = "conflict"
    else:
        status = "ok"
    return {
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "checks": {
            "canonical_rules": str((candidate_check or {}).get("status") or "not_run"),
            "project_execution_contract": str((project_contract_check or {}).get("status") or "not_run"),
        },
    }


def _rule_context_result(args, *, candidate_sql: str = "") -> dict:
    root = Path(args.root).resolve()
    require_project(root)
    project_config = read_project_config(root)
    execution_route = getattr(args, "execution_route", None)
    mode = getattr(args, "mode", "generation") or "generation"
    user_request = str(
        getattr(args, "user_request", "")
        or getattr(args, "query", "")
        or ""
    ).strip()
    if mode == "temporary" and not request_authorizes_temporary_override(user_request):
        raise SystemExit(
            "Temporary rule override mode requires the current verbatim user message to explicitly "
            "scope the exception to this query or confirm this one-query override."
        )
    lifecycle_stage = normalize_lifecycle_stage(
        getattr(args, "lifecycle_stage", None),
        mode=mode,
    )
    request_envelope = build_request_envelope(
        user_request,
        function_id="RULE_CONTEXT",
        lifecycle_stage=lifecycle_stage,
    )
    inheritance_contract = getattr(args, "inheritance_contract", None)
    if not isinstance(inheritance_contract, dict):
        inheritance_contract = build_inheritance_contract()
    parent_application = getattr(args, "parent_rule_application", None)
    inherited_candidates, application_diagnostics = inherited_rule_references(
        parent_application if isinstance(parent_application, dict) else None,
        inheritance_contract,
    )
    inherited_by_concept = {
        str(item.get("concept_key") or ""): item
        for item in inherited_candidates
        if str(item.get("concept_key") or "")
    }
    concept_registry = read_rule_concept_registry(root)
    requested_concept_keys = {
        item.strip()
        for item in re.split(r"[,，;；\s]+", str(args.concept_key or ""))
        if item.strip()
    }
    requested_rule_id = str(getattr(args, "rule_id", "") or "").strip()
    candidate_identifiers = requested_concept_keys | set(inherited_by_concept)
    if requested_rule_id:
        candidate_identifiers.add(requested_rule_id)
    selection_limit = max(1, int(args.limit or 0), len(candidate_identifiers))
    request_text = user_request
    terms = query_terms(request_text)
    intent_frame = build_intent_frame(
        query=request_text,
        metric="",
        table="",
        concept_key="",
        candidate_sql="",
        concept_registry=concept_registry,
    )
    intent_frame["retrieval_hints"] = {
        "metric": str(getattr(args, "metric", "") or ""),
        "table": str(getattr(args, "table", "") or ""),
    }
    for concept_key in sorted(requested_concept_keys):
        apply_concept_hints(intent_frame, concept_key, concept_registry)
    if candidate_sql:
        sql_frame = scan_intent_source(candidate_sql)
        intent_frame["candidate_sql_observed"] = {
            "source_logs": sql_frame["source_logs"],
            "source_fields": sql_frame["source_fields"],
            "domains": sql_frame["domains"],
        }
        intent_frame["activation_basis"] = "request_metric_table_concept_with_candidate_sql_observed_for_validation"
    if args.status in {"superseded", "deprecated"}:
        rules = load_rules(root, status=args.status, include_history=True)
    elif args.status == "all":
        rules = load_rules(root, status="all", include_history=True)
    else:
        _, rules = select_rule_records(
            root,
            intent_frame,
            query_text=request_text,
            concept_keys=candidate_identifiers,
            statuses=(args.status,),
        )
    candidate_rows: list[tuple[int, dict, dict]] = []
    for rule in rules:
        if args.rule_id and rule.get("rule_id") != args.rule_id:
            continue
        if requested_concept_keys and rule.get("concept_key") not in requested_concept_keys:
            continue
        detail = score_rule_detail(rule, terms)
        score = int(detail.get("score", 0))
        selection_hint = bool(
            requested_rule_id
            or str(rule.get("concept_key") or "") in requested_concept_keys
        )
        explicit_evidence = explicit_rule_selection_evidence(
            rule,
            request_text,
            requested_concept_keys=requested_concept_keys,
            requested_rule_id=requested_rule_id,
        )
        contract_candidate = False
        inherited_candidate = str(rule.get("concept_key") or "") in inherited_by_concept
        if not explicit_evidence and not inherited_candidate and score < args.min_score:
            preliminary = activation_contract_decision(
                rule,
                detail,
                intent_frame,
                request_text,
                enforce_candidate_source_gate=(mode == "formalize" and bool(candidate_sql)),
            )
            contract_candidate = bool(preliminary.get("active") or preliminary.get("excluded"))
        if score >= args.min_score or selection_hint or inherited_candidate or contract_candidate:
            candidate_rows.append((score, rule, detail))
    candidate_rows.sort(key=lambda item: (-item[0], item[1].get("rule_id", ""), -int(item[1].get("version", 0) or 0)))

    candidate_rules: list[dict] = []
    for score, rule, detail in candidate_rows[:selection_limit]:
        candidate_rules.append(rule_summary_payload(rule, score, detail, args, relevance="candidate"))

    constraints: list[dict] = []
    active_rule_rows: list[dict] = []
    rejected_rules: list[dict] = []
    excluded_rules: list[dict] = []
    applied_rule_references: list[dict] = []
    inherited_rule_references_payload: list[dict] = []
    excluded_rule_references: list[dict] = []
    parent_application_sha256 = str(
        (parent_application or {}).get("application_sha256")
        if isinstance(parent_application, dict)
        else ""
    )
    parent_asset = (
        inheritance_contract.get("parent_asset")
        if isinstance(inheritance_contract.get("parent_asset"), dict)
        else {}
    )
    for score, rule, detail in candidate_rows:
        concept_key = str(rule.get("concept_key") or "")
        explicit_evidence = explicit_rule_selection_evidence(
            rule,
            request_text,
            requested_concept_keys=requested_concept_keys,
            requested_rule_id=requested_rule_id,
        )
        decision = activation_contract_decision(
            rule,
            detail,
            intent_frame,
            request_text,
            explicit_evidence=explicit_evidence,
            enforce_candidate_source_gate=(mode == "formalize" and bool(candidate_sql) and not explicit_evidence),
        )
        parent_reference = inherited_by_concept.get(concept_key)
        inherited = False
        if (
            not decision["active"]
            and not decision.get("excluded")
            and parent_reference
        ):
            if int(parent_reference.get("version") or 0) == int(rule.get("version") or 0):
                inherited = True
                decision = {
                    "active": True,
                    "excluded": False,
                    "reason": "structured_parent_application",
                    "contract": get_activation_contract(rule)[0],
                    "contract_source": get_activation_contract(rule)[1],
                    "matched_contract_evidence": [
                        {
                            "type": "structured_parent_application",
                            "parent_application_sha256": parent_application_sha256,
                            "inheritance_mode": str(inheritance_contract.get("mode") or "none"),
                        }
                    ],
                }
            else:
                application_diagnostics.append(
                    {
                        "type": "parent_rule_version_not_inherited",
                        "concept_key": concept_key,
                        "parent_version": int(parent_reference.get("version") or 0),
                        "current_version": int(rule.get("version") or 0),
                    }
                )
        if decision["active"]:
            contract_constraints = infer_hard_constraints_from_contract(decision["contract"], rule)
            for constraint in contract_constraints:
                constraint.setdefault("rule_id", rule.get("rule_id", ""))
                constraint.setdefault("concept_key", rule.get("concept_key", ""))
                constraint.setdefault("title", rule.get("title", ""))
                constraints.append(constraint)
            payload = rule_summary_payload(rule, score, detail, args, relevance="active")
            payload.update(
                {
                    "activation_reason": decision["reason"],
                    "activation_contract_source": decision.get("contract_source", ""),
                    "matched_contract_evidence": decision.get("matched_contract_evidence", []),
                    "event_signature": contract_event_signature(decision["contract"]),
                    "constraints": contract_constraints,
                    "application_source": "structured_parent_application" if inherited else (
                        "explicit_selection" if explicit_evidence else "current_user_request"
                    ),
                }
            )
            active_rule_rows.append(payload)
            reference = rule_reference(
                rule,
                source=payload["application_source"],
                evidence=decision.get("matched_contract_evidence", []),
                parent_application_sha256=parent_application_sha256 if inherited else "",
                parent_asset=parent_asset if inherited else None,
            )
            if inherited:
                inherited_rule_references_payload.append(reference)
            else:
                applied_rule_references.append(reference)
        elif decision.get("excluded"):
            payload = rule_summary_payload(rule, score, detail, args, relevance="excluded")
            payload.update(
                {
                    "reason": decision["reason"],
                    "activation_contract_source": decision.get("contract_source", ""),
                    "matched_contract_evidence": decision.get("matched_contract_evidence", []),
                }
            )
            excluded_rules.append(payload)
            excluded_rule_references.append(
                rule_reference(
                    rule,
                    source="current_user_request_exclusion",
                    evidence=decision.get("matched_contract_evidence", []),
                )
            )
        else:
            payload = rule_summary_payload(rule, score, detail, args, relevance="rejected")
            payload.update(
                {
                    "reason": decision["reason"],
                    "activation_contract_source": decision.get("contract_source", ""),
                    "matched_contract_evidence": decision.get("matched_contract_evidence", []),
                }
            )
            rejected_rules.append(payload)

    active_rules_payload = active_rule_rows[:selection_limit]
    active_rule_ids = {(item.get("rule_id"), item.get("version")) for item in active_rules_payload}
    applied_rule_references = [
        item
        for item in applied_rule_references
        if (item.get("rule_id"), item.get("version")) in active_rule_ids
    ]
    inherited_rule_references_payload = [
        item
        for item in inherited_rule_references_payload
        if (item.get("rule_id"), item.get("version")) in active_rule_ids
    ]
    constraints = [
        constraint
        for constraint in constraints
        if (constraint.get("rule_id"), next(
            (item.get("version") for item in active_rules_payload if item.get("rule_id") == constraint.get("rule_id")),
            None,
        )) in active_rule_ids
    ]
    deduped_constraints: list[dict] = []
    seen_constraints: set[str] = set()
    for constraint in constraints:
        key_payload = {
            name: value
            for name, value in constraint.items()
            if name not in {"reason", "title"}
        }
        key = json.dumps(key_payload, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen_constraints:
            continue
        seen_constraints.add(key)
        deduped_constraints.append(constraint)
    constraints = deduped_constraints
    constraints, inactive_stage_constraints = partition_constraints_for_stage(
        constraints,
        lifecycle_stage,
    )
    for active_rule in active_rules_payload:
        all_rule_constraints = list(active_rule.get("constraints", []) or [])
        active_rule["constraints_all_stages"] = all_rule_constraints
        active_rule["constraints"] = [
            constraint
            for constraint in all_rule_constraints
            if constraint_applies_to_stage(constraint, lifecycle_stage)
        ]

    reverse_audit = source_metric_audit(candidate_sql, rules, concept_registry) if candidate_sql else {
        "status": "not_run",
        "observed": {},
        "matches": [],
    }
    mismatches = name_logic_mismatch_rows(active_rules_payload, reverse_audit)
    candidate_check = candidate_rule_check(candidate_sql, constraints, mode=mode) if candidate_sql else None
    active_concepts = {
        str(item.get("concept_key") or "")
        for item in active_rules_payload
        if str(item.get("concept_key") or "")
    }
    excluded_concepts = {
        str(item.get("concept_key") or "")
        for item in excluded_rules
        if str(item.get("concept_key") or "")
    }
    rules_by_concept = {
        str(item.get("concept_key") or ""): item
        for item in rules
        if str(item.get("concept_key") or "")
    }
    unrequested_scope_mutations: list[dict] = []
    for match in reverse_audit.get("matches", []) or []:
        concept_key = str(match.get("concept_key") or "")
        if match.get("strength") != "exact" or concept_key in active_concepts:
            continue
        rule = rules_by_concept.get(concept_key) or {}
        contract, contract_source = get_activation_contract(rule)
        policy = str(contract.get("unrequested_sql_policy") or "diagnostic")
        if contract_source != "stored_v2" or application_class(contract) != "intent_required":
            continue
        severity = "blocker" if policy == "block" and mode in {"generation", "formalize"} else "diagnostic"
        mutation = {
            "type": "excluded_rule_still_implemented" if concept_key in excluded_concepts else "unrequested_scope_mutation",
            "severity": severity,
            "rule_id": str(rule.get("rule_id") or ""),
            "concept_key": concept_key,
            "rule_title": str(rule.get("title") or ""),
            "message": (
                f"Candidate SQL implements optional rule `{concept_key}` without a current-request or "
                "eligible structured-parent application."
                if concept_key not in excluded_concepts
                else f"Candidate SQL still implements explicitly excluded rule `{concept_key}`."
            ),
            "reverse_evidence": match.get("matched_evidence", []),
        }
        unrequested_scope_mutations.append(mutation)
    if candidate_check and unrequested_scope_mutations:
        candidate_check.setdefault("diagnostics", []).extend(unrequested_scope_mutations)
        blocking_mutations = [
            item for item in unrequested_scope_mutations if item.get("severity") == "blocker"
        ]
        if blocking_mutations:
            candidate_check.setdefault("blockers", []).extend(blocking_mutations)
            candidate_check["status"] = "conflict"
    project_contract_check = (
        project_execution_contract_check(
            candidate_sql,
            project_config,
            execution_route=execution_route if isinstance(execution_route, dict) else None,
        )
        if candidate_sql
        else None
    )
    project_time_contract = (
        project_contract_check.get("time_contract", {})
        if project_contract_check
        else {
            "status": "not_run",
            "mode": "not_run",
            "findings": [],
            "facts": {},
        }
    )
    blocker_mismatches = [item for item in mismatches if item.get("severity") == "blocker"]
    diagnostic_mismatches = [item for item in mismatches if item.get("severity") != "blocker"]
    temporary_override: dict = {}
    diagnostic_only_mode = mode == "temporary"
    if candidate_check and blocker_mismatches and not diagnostic_only_mode:
        candidate_check.setdefault("blockers", [])
        candidate_check["blockers"].extend(blocker_mismatches)
        candidate_check["status"] = "conflict"
    if candidate_check and diagnostic_mismatches and not diagnostic_only_mode:
        candidate_check.setdefault("diagnostics", [])
        candidate_check["diagnostics"].extend(diagnostic_mismatches)
    if candidate_check and mismatches and diagnostic_only_mode:
        candidate_check.setdefault("diagnostics", [])
        candidate_check["diagnostics"].extend(mismatches)
    if candidate_check and mode == "temporary":
        candidate_check.setdefault("diagnostics", [])
        existing_blockers = list(candidate_check.get("blockers", []) or [])
        retained_blockers = [
            item
            for item in existing_blockers
            if isinstance(item, dict)
            and item.get("type") in {"unrequested_scope_mutation", "excluded_rule_still_implemented"}
        ]
        downgraded_blockers = [item for item in existing_blockers if item not in retained_blockers]
        for blocker in downgraded_blockers:
            diagnostic = dict(blocker)
            diagnostic["severity"] = "diagnostic"
            diagnostic["temporary_mode_source"] = "explicit_user_override"
            candidate_check["diagnostics"].append(diagnostic)
        candidate_check["downgraded_rule_conflict_count"] = len(downgraded_blockers)
        candidate_check["blockers"] = retained_blockers
        temporary_override = build_temporary_rule_override(
            user_request=user_request,
            blockers=downgraded_blockers + blocker_mismatches,
            acknowledged_at=now_iso(),
        )
        if temporary_override:
            candidate_check.setdefault("warnings", []).append(
                {
                    "type": "temporary_rule_override",
                    "conflict_signature": temporary_override["conflict_signature"],
                    "conflicted_rule_ids": temporary_override["conflicted_rule_ids"],
                    "message": (
                        "本次明确为临时 SQL，已按当前用户说明继续；canonical 口径未修改，"
                        "正式固化前必须解决这些差异。"
                    ),
                }
            )
        candidate_check["temporary_note"] = (
            "Explicit temporary-query mode downgrades canonical business-rule conflicts only. "
            "Project execution, privacy, correctness, and performance gates remain blocking."
        )
        candidate_check["status"] = "conflict" if retained_blockers else "ok"
    if candidate_check and mode == "formalize":
        candidate_check["formalize_note"] = (
            "Reverse source audit matches remain diagnostics during formalization unless they exact-match "
            "a structurally incompatible active rule or violate active hard constraints."
        )
    rejected_summary = [
        {
            "rule_id": item.get("rule_id", ""),
            "concept_key": item.get("concept_key", ""),
            "title": item.get("title", ""),
            "reason": item.get("reason", ""),
            "activation_contract_source": item.get("activation_contract_source", ""),
        }
        for item in rejected_rules[:selection_limit]
    ]
    evidenced_selector_signals = {
        str(evidence.get("signal") or "")
        for reference in applied_rule_references
        for evidence in reference.get("evidence", []) or []
        if evidence.get("type") == "current_user_explicit_rule_selection"
    }
    selector_hints = [
        ("concept_key", value)
        for value in sorted(requested_concept_keys)
    ]
    if requested_rule_id:
        selector_hints.append(("rule_id", requested_rule_id))
    selector_diagnostics = [
        {
            "type": "selector_is_retrieval_hint_only",
            "selector_type": selector_type,
            "value": value,
            "reason": "selector_not_found_in_current_user_message",
        }
        for selector_type, value in selector_hints
        if value not in evidenced_selector_signals
    ]
    rule_application = build_rule_application(
        request_envelope=request_envelope,
        mode=mode,
        lifecycle_stage=lifecycle_stage,
        applied_rules=applied_rule_references,
        inherited_rules=inherited_rule_references_payload,
        excluded_rules=excluded_rule_references,
        diagnostics=[
            *application_diagnostics,
            *selector_diagnostics,
            *unrequested_scope_mutations,
        ],
        inheritance_contract=inheritance_contract,
    )
    generation_gate = compose_generation_gate(candidate_check, project_contract_check)
    status = generation_gate.get("status") if candidate_sql else ("ok" if active_rules_payload else "no_active_rules")
    result = {
        "project_root": ".",
        "mode": mode,
        "lifecycle_stage": lifecycle_stage,
        "requested_concept_keys": sorted(requested_concept_keys),
        "query": user_request,
        "request_envelope": request_envelope,
        "rule_application": rule_application,
        "terms": terms,
        "intent_frame": intent_frame,
        "status": status,
        "active_rules": active_rules_payload,
        "applied_rules": active_rules_payload,
        "inherited_rules": inherited_rule_references_payload,
        "excluded_rules": excluded_rules,
        "candidate_rules": candidate_rules,
        "rejected_rules": rejected_rules[:selection_limit],
        "rejected_rules_summary": rejected_summary,
        "hard_constraints": constraints,
        "inactive_stage_constraints": inactive_stage_constraints,
        "candidate_sql_check": candidate_check,
        "project_contract_check": project_contract_check,
        "generation_gate": generation_gate,
        "temporary_rule_override": temporary_override,
        "forward_rule_context": {
            "status": "ok" if active_rules_payload else "no_active_rules",
            "active_rules": active_rules_payload,
            "hard_constraints": constraints,
            "candidate_rules": candidate_rules,
            "rejected_rules_summary": rejected_summary,
        },
        "reverse_source_audit": reverse_audit,
        "source_metric_audit": reverse_audit,
        "name_logic_mismatches": mismatches,
        "unrequested_scope_mutations": unrequested_scope_mutations,
        "project_time_contract": project_time_contract,
    }
    return result


def evaluate_rule_context(
    *,
    root: Path | str,
    user_request: str = "",
    candidate_sql: str = "",
    mode: str = "generation",
    lifecycle_stage: str | None = None,
    concept_keys: list[str] | None = None,
    rule_id: str | None = None,
    status: str = "confirmed",
    parent_rule_application: dict | None = None,
    inheritance_contract: dict | None = None,
    execution_route: dict | None = None,
    limit: int = 8,
    min_score: int = 4,
    excerpt_chars: int = 600,
) -> dict:
    args = argparse.Namespace(
        root=str(root),
        user_request=str(user_request or ""),
        query="",
        metric="",
        table="",
        concept_key=",".join(
            str(item).strip() for item in (concept_keys or []) if str(item).strip()
        ) or None,
        rule_id=rule_id,
        status=status,
        mode=mode,
        lifecycle_stage=lifecycle_stage,
        limit=limit,
        min_score=min_score,
        excerpt_chars=excerpt_chars,
        parent_rule_application=parent_rule_application,
        inheritance_contract=inheritance_contract or build_inheritance_contract(),
        execution_route=execution_route,
    )
    return _rule_context_result(args, candidate_sql=candidate_sql)


def cmd_rule_context(args) -> None:
    candidate_sql = ""
    if args.candidate_sql:
        candidate_sql = Path(args.candidate_sql).read_text(encoding="utf-8-sig")
    parent_application = None
    if getattr(args, "parent_rule_application", None):
        parent_application = read_json_object(Path(args.parent_rule_application))
    inheritance_contract = build_inheritance_contract(
        getattr(args, "inheritance_mode", "none"),
        change_type=getattr(args, "change_type", ""),
        coverage_relation=getattr(args, "coverage_relation", ""),
        same_execution_fingerprint=bool(getattr(args, "same_execution_fingerprint", False)),
        same_logic_contract=bool(getattr(args, "same_logic_contract", False)),
    )
    result = evaluate_rule_context(
        root=args.root,
        user_request=str(getattr(args, "user_request", "") or getattr(args, "query", "") or ""),
        candidate_sql=candidate_sql,
        mode=args.mode,
        lifecycle_stage=args.lifecycle_stage,
        concept_keys=[
            item.strip()
            for item in re.split(r"[,，;；\s]+", str(args.concept_key or ""))
            if item.strip()
        ],
        rule_id=args.rule_id,
        status=args.status,
        parent_rule_application=parent_application,
        inheritance_contract=inheritance_contract,
        limit=args.limit,
        min_score=args.min_score,
        excerpt_chars=args.excerpt_chars,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    active_rules_payload = result.get("active_rules", []) or []
    candidate_rules = result.get("candidate_rules", []) or []
    candidate_check = result.get("candidate_sql_check")
    if not active_rules_payload:
        print("No active rules found.")
        if candidate_rules:
            print("# Candidate Rules (diagnostic only)")
            for item in candidate_rules:
                print(f"- `{item['rule_id']}` score={item['relevance_score']}: {item['title']}")
        return
    print("# Active Canonical Rules")
    for item in active_rules_payload:
        print(
            f"- `{item['rule_id']}` v{item['version']} [{item['status']}] "
            f"score={item['relevance_score']} strong={item['strong_score']} "
            f"activation={item['activation_reason']}: {item['title']}"
        )
        if item["constraints"]:
            for constraint in item["constraints"]:
                if constraint["type"] == "must_use_log":
                    print(f"  - must_use_log: {constraint['log']} ({constraint['reason']})")
                elif constraint["type"] == "must_not_use_log":
                    print(f"  - must_not_use_log: {constraint['log']} ({constraint.get('reason', '')})")
                elif constraint["type"] == "do_not_substitute_log":
                    print(f"  - do_not_substitute_log: expected {constraint['expected_log']}")
    if candidate_check:
        print("# Candidate SQL Check")
        print(f"- status: {candidate_check['status']}")
        print(f"- candidate_logs: {', '.join(candidate_check['candidate_logs']) or 'none'}")
        for blocker in candidate_check["blockers"]:
            print(f"- BLOCKER: {blocker['message']}")


def artifact_search_text(item: dict) -> str:
    parts = [
        item.get("kind", ""),
        item.get("slug", ""),
        item.get("title", ""),
        item.get("status", ""),
        item.get("path", ""),
        item.get("business_category", ""),
        item.get("analysis_type", ""),
        item.get("grain", ""),
        item.get("time_grain", ""),
        item.get("reuse_notes", ""),
        item.get("natural_language_intent", ""),
        item.get("notes", ""),
    ]
    for key in ["tags", "metrics", "dimensions", "tables", "intermediate_tables"]:
        parts.extend(item.get(key, []) or [])
    return " ".join(str(part) for part in parts).lower()


def artifact_matches(item: dict, args) -> bool:
    if not args.include_history and not is_current_artifact(item):
        return False
    if args.kind and item.get("kind") != normalize_kind(args.kind):
        return False
    if args.business_category and item.get("business_category") != args.business_category:
        return False
    if args.analysis_type and item.get("analysis_type") != args.analysis_type:
        return False
    if args.reusable and not item.get("reusable"):
        return False
    for key, expected in [
        ("tags", args.tag),
        ("metrics", args.metric),
        ("tables", args.table),
        ("intermediate_tables", args.intermediate_table),
    ]:
        if expected and expected not in (item.get(key, []) or []):
            return False
    if args.query:
        text = artifact_search_text(item)
        tokens = [token.lower() for token in re.split(r"\s+", args.query) if token.strip()]
        if not all(token in text for token in tokens):
            return False
    return True


def project_manifest_fingerprint(root: Path) -> str:
    return hashlib.sha256(manifest_path(root).read_bytes()).hexdigest()


def project_index_manifest_fingerprint(root: Path) -> str:
    index_path = root / PROJECT_INDEX_FILE
    if not index_path.is_file():
        return ""
    match = PROJECT_INDEX_MANIFEST_RE.search(index_path.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def project_index_matches_manifest(root: Path) -> bool:
    try:
        return project_index_manifest_fingerprint(root) == project_manifest_fingerprint(root)
    except OSError:
        return False


def rebuild_index(root: Path) -> None:
    manifest = read_json(manifest_path(root), {})
    rules = []
    if has_v2_store(root):
        rule_snapshot = RuleStore(root).build_dictionary_snapshot(include_history=False)
        rules = [
            rule
            for concept in rule_snapshot.get("concepts", []) or []
            for rule in concept.get("current", []) or []
        ]
    package_entries = [item for item in manifest.get("packages", []) if isinstance(item, dict)]
    package_manifests: list[dict] = []
    for entry in package_entries:
        package_id = str(entry.get("package_id") or "")
        if not package_id:
            continue
        try:
            package_manifests.append(load_formal_asset_package(root, package_id))
        except FormalAssetRepositoryError:
            package_manifests.append(entry)
    tables = manifest.get("intermediate_tables", [])
    current_tables = [item for item in tables if is_current_table(item)]
    history_tables = [item for item in tables if not is_current_table(item)]
    state_counts = {
        state: sum(1 for item in package_manifests if (item.get("lifecycle_state") or "current") == state)
        for state in ARTIFACT_STATES
    }
    kind_counts = {
        kind: sum(
            1
            for package in package_manifests
            for member in package.get("members", [])
            if isinstance(member, dict)
            and member.get("role") in FORMAL_SQL_ROLES[kind]
        )
        for kind in ARTIFACT_KINDS
    }
    lines = [
        f"# {manifest.get('project_name', root.name)}",
        "",
        f"Updated: {now_iso()}",
        "",
        "## Formal Asset Repository",
        "",
        "- Source of truth: `formal_assets/FA-NNNN-<slug>/manifest.json`",
        f"- Manifest SHA-256: `{project_manifest_fingerprint(root)}`",
        "- Canonical storage: `formal_assets/`",
        (
            f"- Packages: total={len(package_manifests)}; SQL members: QUERY={kind_counts['QUERY']}, "
            f"VALIDATION={kind_counts['VALIDATION']}, DASHBOARD={kind_counts['DASHBOARD']}"
        ),
        (
            f"- Lifecycle: current={state_counts['current']}, history={state_counts['history']}, "
            f"archived={state_counts['archived']}"
        ),
        "- `archived` is a Package/member lifecycle state, not a separate formal-asset directory.",
        "",
        "## Project Config",
        "",
    ]
    config = read_project_config(root)
    if config:
        context = project_context_snapshot(config)
        for key in [
            "project_id",
            "display_name",
            "sql_dialect",
            "query_engine",
            "query_environment",
            "dashboard_application",
            "table_naming_profile",
            "partition_policy",
        ]:
            lines.append(f"- `{key}`: {context.get(key, 'missing')}")
    else:
        lines.append("- Missing `project_config.json`; formal SQL generation must be blocked.")

    lines.extend([
        "",
        "## Active Confirmed Rules",
        "",
    ])
    if rules:
        for rule in sorted(rules, key=lambda r: (r.get("rule_id", ""), r.get("version", 0))):
            lines.append(
                f"- `{rule['rule_id']}` v{rule.get('semantic_version', rule['version'])}: "
                f"{rule['title']}"
            )
    else:
        lines.append("- No confirmed rules yet.")

    lines.extend(["", "## Current Intermediate Tables", ""])
    if current_tables:
        for item in sorted(current_tables, key=lambda x: (x.get("table_name", ""), x.get("version", 0))):
            partitions = ", ".join(item.get("partition_fields", []) or [])
            sources = ", ".join(item.get("source_tables", []) or [])
            fallback_sources = ", ".join(item.get("fallback_source_tables", []) or [])
            downstream = ", ".join(item.get("downstream_artifacts", []) or [])
            branch = f", branch of: `{item['branch_of']}`" if item.get("branch_of") else ""
            lines.append(
                f"- `{item['table_name']}` v{item['version']:03d}: {item.get('title', '')} "
                f"-> `{item['path']}` [grain=`{item.get('grain', '')}`; "
                f"availability=`{item.get('availability_status', 'unknown')}`; "
                f"mode=`{item.get('source_contract_mode', 'dual_path')}`; "
                f"partitions=`{partitions}`; refresh=`{item.get('refresh_mode', '')}`; "
                f"sources=`{sources}`; fallback_sources=`{fallback_sources}`; "
                f"downstream=`{downstream}`]{branch}"
            )
    else:
        lines.append("- No current intermediate tables yet.")

    lines.extend(["", "## Intermediate Table History", ""])
    if history_tables:
        for item in sorted(history_tables, key=lambda x: (x.get("table_name", ""), x.get("version", 0))):
            replaced_by = f", replaced by: `{item['replaced_by']}`" if item.get("replaced_by") else ""
            reason = f", reason: {item['change_reason']}" if item.get("change_reason") else ""
            lines.append(
                f"- `{item['table_name']}` v{item['version']:03d} "
                f"[{item.get('table_state', 'history')}/{item.get('status', '')}] "
                f"-> `{item['path']}`{replaced_by}{reason}"
            )
    else:
        lines.append("- No historical intermediate tables yet.")

    lines.extend(["", "## Formal Asset Packages", ""])
    if package_manifests:
        for package in sorted(package_manifests, key=lambda item: str(item.get("package_id") or "")):
            current = package.get("current") if isinstance(package.get("current"), dict) else {}
            role_counts = {
                role: len(member_ids)
                for role, member_ids in (current.get("by_role") or {}).items()
                if isinstance(member_ids, list)
            }
            role_text = ", ".join(f"{role}={count}" for role, count in sorted(role_counts.items())) or "no current members"
            lines.append(
                f"- `{package.get('package_id')}` [{package.get('lifecycle_state', 'current')}] "
                f"{package.get('title', '')} revision={package.get('revision', 0)} "
                f"({role_text}) -> `{package.get('directory', '')}/manifest.json`"
            )
    else:
        lines.append("- No Formal Asset Packages yet.")

    run_members = [
        member
        for package in package_manifests
        for member in package.get("members", [])
        if isinstance(member, dict) and member.get("role") == "run_meta"
    ]
    lines.extend(["", "## User Run Evidence", ""])
    if run_members:
        for member in sorted(run_members, key=lambda item: str(item.get("created_at") or "")):
            lines.append(
                f"- `{member.get('member_id')}` [{member.get('lifecycle_state', 'current')}] "
                f"-> `{member.get('path')}`"
            )
    else:
        lines.append("- No packaged run evidence yet.")

    notes = manifest.get("notes", [])
    lines.extend(["", "## Conversation Notes", ""])
    if notes:
        for note in notes:
            lines.append(f"- `{note['kind']}` {note['title']} -> `{note['path']}`")
    else:
        lines.append("- No saved conversation notes yet.")

    lines.append("")
    (root / PROJECT_INDEX_FILE).write_text("\n".join(lines), encoding="utf-8")


def cmd_rebuild_index(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    rebuild_index(root)
    print(f"Rebuilt index: {root / 'index.md'}")


def cmd_show_rules(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    status = args.status
    rows = load_rules(
        root,
        status=status,
        include_history=status in {"all", "superseded", "deprecated"},
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def rule_status_label(status: str) -> str:
    labels = {
        "confirmed": "Active confirmed rules",
        "proposed": "Pending proposed rules",
        "superseded": "Superseded rule history",
        "deprecated": "Deprecated rules",
    }
    return labels.get(status, status)


def format_rule_line(rule: dict) -> str:
    scope = rule.get("scope", "project")
    lifetime = rule.get("lifetime", "persistent")
    applies_to = rule.get("applies_to", "")
    affected = ",".join(rule.get("affected_artifacts", []) or [])
    concept_key = rule.get("concept_key", "")
    suffix = f" concept_key={concept_key or 'missing'} scope={scope} lifetime={lifetime}"
    if applies_to:
        suffix += f" applies_to={applies_to}"
    if affected:
        suffix += f" affected={affected}"
    return (
        f"- `{rule.get('rule_id')}` v{rule.get('version')}: "
        f"{rule.get('title')} | {rule.get('content')}{suffix}"
    )


def cmd_rule_report(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    if args.concept_key:
        rules = RuleStore(root).load_versions(args.concept_key)
        if args.status != "all":
            rules = [rule for rule in rules if rule.get("status") == args.status]
    else:
        rules = load_rules(
            root,
            status=args.status,
            include_history=args.status in {"all", "superseded", "deprecated"} or bool(args.rule_id),
        )
    if args.rule_id:
        rules = [rule for rule in rules if rule.get("rule_id") == args.rule_id]
    rules = sorted(rules, key=lambda r: (r.get("rule_id", ""), r.get("version", 0)))

    if args.json:
        print(json.dumps(rules, ensure_ascii=False, indent=2))
        return

    if not rules:
        print("No matching rules.")
        return

    if args.rule_id or args.concept_key:
        print(f"# Rule history: {args.rule_id or args.concept_key}")
        for rule in rules:
            print(format_rule_line(rule))
            if rule.get("decision_question"):
                print(f"  decision_question: {rule['decision_question']}")
            if rule.get("notes"):
                print(f"  notes: {rule['notes']}")
        return

    grouped: dict[str, list[dict]] = {status: [] for status in RULE_STATUSES}
    for rule in rules:
        grouped.setdefault(rule.get("status", "unknown"), []).append(rule)

    for status in RULE_STATUSES:
        rows = grouped.get(status, [])
        if not rows:
            continue
        print(f"# {rule_status_label(status)}")
        for rule in rows:
            print(format_rule_line(rule))
        print("")


def table_search_text(item: dict) -> str:
    parts = [
        item.get("table_name", ""),
        item.get("slug", ""),
        item.get("title", ""),
        item.get("status", ""),
        item.get("table_state", ""),
        item.get("change_type", ""),
        item.get("path", ""),
        item.get("table_type", ""),
        item.get("materialization", ""),
        item.get("lifecycle", ""),
        item.get("business_category", ""),
        item.get("analysis_type", ""),
        item.get("purpose", ""),
        item.get("grain", ""),
        item.get("time_grain", ""),
        item.get("refresh_mode", ""),
        item.get("availability_status", ""),
        item.get("availability_source", ""),
        item.get("availability_note", ""),
        item.get("unavailable_reason", ""),
        item.get("last_availability_check", ""),
        item.get("source_contract_mode", ""),
        item.get("fallback_policy", ""),
        item.get("fallback_sql_reference", ""),
        item.get("field_contract", ""),
        item.get("grain_contract", ""),
        item.get("source_contract_note", ""),
        item.get("reuse_notes", ""),
        item.get("quality_notes", ""),
        item.get("content_summary", ""),
        item.get("notes", ""),
    ]
    for key in [
        "partition_fields",
        "primary_keys",
        "source_tables",
        "source_artifacts",
        "downstream_artifacts",
        "downstream_tables",
        "metrics",
        "dimensions",
        "tags",
        "validation_artifacts",
        "fallback_source_tables",
        "fallback_source_artifacts",
        "canonical_rule_refs",
        "xml_source_refs",
    ]:
        parts.extend(item.get(key, []) or [])
    return " ".join(str(part) for part in parts).lower()


def table_matches(item: dict, args) -> bool:
    if not args.include_history and not is_current_table(item):
        return False
    if args.table_name and normalize_table_name(item.get("table_name", "")) != normalize_table_name(args.table_name):
        return False
    if args.business_category and item.get("business_category") != args.business_category:
        return False
    if args.table_type and item.get("table_type") != args.table_type:
        return False
    if args.materialization and item.get("materialization") != args.materialization:
        return False
    if args.lifecycle and item.get("lifecycle") != args.lifecycle:
        return False
    if args.availability_status and item.get("availability_status", "unknown") != args.availability_status:
        return False
    if args.source_contract_mode and item.get("source_contract_mode", "dual_path") != args.source_contract_mode:
        return False
    if args.source_table and args.source_table not in (item.get("source_tables", []) or []):
        return False
    if args.downstream_artifact and args.downstream_artifact not in (item.get("downstream_artifacts", []) or []):
        return False
    if args.fallback_source_table and args.fallback_source_table not in (item.get("fallback_source_tables", []) or []):
        return False
    if args.canonical_rule_ref and args.canonical_rule_ref not in (item.get("canonical_rule_refs", []) or []):
        return False
    if args.xml_source_ref and args.xml_source_ref not in (item.get("xml_source_refs", []) or []):
        return False
    if args.reusable and not item.get("reusable"):
        return False
    if args.query:
        text = table_search_text(item)
        tokens = [token.lower() for token in re.split(r"\s+", args.query) if token.strip()]
        if not all(token in text for token in tokens):
            return False
    return True


def cmd_search_tables(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    manifest = read_json(manifest_path(root), {})
    rows = [
        item
        for item in manifest.get("intermediate_tables", [])
        if table_matches(item, args)
    ]
    rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("No matching intermediate tables.")
        return
    for item in rows:
        source_tables = ",".join(item.get("source_tables", []) or [])
        fallback_tables = ",".join(item.get("fallback_source_tables", []) or [])
        downstream_artifacts = ",".join(item.get("downstream_artifacts", []) or [])
        partitions = ",".join(item.get("partition_fields", []) or [])
        reusable = "reusable" if item.get("reusable") else "not_reusable"
        print(
            f"{item.get('table_name')} v{item.get('version'):03d} | "
            f"{item.get('title')} | {item.get('table_state', 'current')} | "
            f"availability={item.get('availability_status', 'unknown')} | "
            f"mode={item.get('source_contract_mode', 'dual_path')} | "
            f"{item.get('table_type')} | {item.get('materialization')} | "
            f"{item.get('refresh_mode')} | {reusable} | grain={item.get('grain', '')} | "
            f"partitions={partitions} | sources={source_tables} | fallback_sources={fallback_tables} | "
            f"downstream={downstream_artifacts} | path={item.get('path')}"
        )


def cmd_table_report(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    manifest = read_json(manifest_path(root), {})
    rows = manifest.get("intermediate_tables", [])
    if args.table_name or args.slug:
        rows = [find_table(manifest, args.table_name, args.slug, args.version)]
    elif not args.include_history:
        rows = [item for item in rows if is_current_table(item)]
    rows = sorted(rows, key=lambda item: (item.get("table_name", ""), item.get("version", 0)))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("No intermediate tables.")
        return
    for item in rows:
        print(
            f"# {item.get('table_name')} v{item.get('version'):03d} "
            f"[{item.get('table_state', 'current')}/{item.get('status', '')}]"
        )
        print(f"path: {item.get('path')}")
        print(f"purpose: {item.get('purpose', '')}")
        print(f"grain: {item.get('grain', '')}")
        print(f"availability: {item.get('availability_status', 'unknown')} ({item.get('availability_source', 'not_checked')})")
        if item.get("unavailable_reason"):
            print(f"unavailable_reason: {item.get('unavailable_reason')}")
        print(f"source_contract_mode: {item.get('source_contract_mode', 'dual_path')}")
        if item.get("fallback_policy"):
            print(f"fallback_policy: {item.get('fallback_policy')}")
        print(f"partition_fields: {','.join(item.get('partition_fields', []) or [])}")
        print(f"source_tables: {','.join(item.get('source_tables', []) or [])}")
        print(f"fallback_source_tables: {','.join(item.get('fallback_source_tables', []) or [])}")
        print(f"canonical_rule_refs: {','.join(item.get('canonical_rule_refs', []) or [])}")
        print(f"xml_source_refs: {','.join(item.get('xml_source_refs', []) or [])}")
        print(f"downstream_artifacts: {','.join(item.get('downstream_artifacts', []) or [])}")
        if item.get("replaced_by"):
            print(f"replaced_by: {item.get('replaced_by')}")
        if item.get("branch_of"):
            print(f"branch_of: {item.get('branch_of')}")
        print("")


def cmd_search_artifacts(args) -> None:
    root = Path(args.root).resolve()
    require_project(root)
    manifest = read_json(manifest_path(root), {})
    rows = [
        item
        for item in manifest.get("artifacts", [])
        if artifact_matches(item, args)
    ]
    rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("No matching artifacts.")
        return
    for item in rows:
        tags = ",".join(item.get("tags", []) or [])
        metrics = ",".join(item.get("metrics", []) or [])
        tables = ",".join(item.get("tables", []) or [])
        intermediate_tables = ",".join(item.get("intermediate_tables", []) or [])
        reusable = "reusable" if item.get("reusable") else "not_reusable"
        state = item.get("artifact_state", "current")
        change = item.get("change_type", "new")
        verification = item.get("verification_status", "not_applicable")
        lineage = ""
        if item.get("branch_of"):
            lineage = f" | branch_of={item.get('branch_of')}"
        if item.get("replaced_by"):
            lineage = f" | replaced_by={item.get('replaced_by')}"
        print(
            f"{display_kind(item.get('kind'))} {item.get('slug')} v{item.get('version'):03d} | "
            f"{item.get('title')} | {item.get('business_category', DEFAULT_BUSINESS_CATEGORY)} | "
            f"{item.get('analysis_type', DEFAULT_ANALYSIS_TYPE)} | {state} | {change} | {verification} | {reusable} | "
            f"metrics={metrics} | tables={tables} | intermediate_tables={intermediate_tables} | "
            f"tags={tags} | path={item.get('path')}"
            f"{lineage}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize a SQL project folder")
    init.add_argument("--root", required=True)
    init.add_argument("--project-name")
    init.add_argument("--project-id")
    init.add_argument("--display-name")
    init.add_argument("--dialect", choices=["Hive", "StarRocks", "hive", "starrocks"])
    init.add_argument("--table-profile", choices=sorted(TABLE_NAMING_PROFILES))
    init.add_argument("--query-engine")
    init.add_argument("--query-environment")
    init.add_argument("--dashboard-application")
    init.add_argument("--project-start-date")
    init.add_argument(
        "--default-query-window-mode",
        choices=["project_start_to_yesterday", "missing"],
    )
    init.add_argument("--default-query-timezone-offset", default=DEFAULT_TIMEZONE_OFFSET)
    init.add_argument("--table-override", action="append", default=[])
    init.set_defaults(func=cmd_init)

    show_config = sub.add_parser("show-config", help="Show project execution and naming config")
    show_config.add_argument("--root", required=True)
    show_config.set_defaults(func=cmd_show_config)

    set_config = sub.add_parser("set-config", help="Update project execution and naming config")
    set_config.add_argument("--root", required=True)
    set_config.add_argument("--project-id")
    set_config.add_argument("--display-name")
    set_config.add_argument("--dialect", choices=["Hive", "StarRocks", "hive", "starrocks"])
    set_config.add_argument("--table-profile", choices=sorted(TABLE_NAMING_PROFILES))
    set_config.add_argument("--query-engine")
    set_config.add_argument("--query-environment")
    set_config.add_argument("--dashboard-application")
    set_config.add_argument("--project-start-date")
    set_config.add_argument(
        "--default-query-window-mode",
        choices=["project_start_to_yesterday", "missing"],
    )
    set_config.add_argument("--default-query-timezone-offset")
    set_config.add_argument("--table-override", action="append", default=[])
    set_config.set_defaults(func=cmd_set_config)

    resolve_table = sub.add_parser("resolve-table", help="Resolve a TLOG log name to the project physical table")
    resolve_table.add_argument("--root", required=True)
    resolve_table.add_argument("--execution-profile", default="")
    resolve_table.add_argument("log_name")
    resolve_table.set_defaults(func=cmd_resolve_table)

    add_rule = sub.add_parser("add-rule", help="Add or update a canonical business rule")
    add_rule.add_argument("--root", required=True)
    add_rule.add_argument("--rule-id")
    add_rule.add_argument("--concept-key", help="Registered cross-project concept key for project/persistent rules")
    add_rule.add_argument("--title", required=True)
    add_rule.add_argument("--content", required=True)
    add_rule.add_argument("--source", default="oral")
    add_rule.add_argument("--source-evidence")
    add_rule.add_argument("--status", choices=["proposed", "confirmed", "deprecated"], default="proposed")
    add_rule.add_argument("--confirmed-by-user", action="store_true")
    add_rule.add_argument("--scope", choices=["session", "artifact", "project"], default="project")
    add_rule.add_argument("--lifetime", choices=["temporary", "persistent"], default="persistent")
    add_rule.add_argument("--applies-to", default="QUERY,DASHBOARD")
    add_rule.add_argument("--affected-artifacts", default="")
    add_rule.add_argument("--decision-question")
    add_rule.add_argument("--supersedes")
    add_rule.add_argument("--notes")
    add_rule.add_argument("--activation-contract-json", help="Optional JSON object defining structured rule activation conditions")
    add_rule.add_argument("--activation-contract-file", help="Optional JSON file defining structured rule activation conditions")
    add_rule.add_argument("--structured-definition-json", help="Optional JSON object for the human-readable structured rule contract")
    add_rule.add_argument("--structured-definition-file", help="Optional JSON file for the human-readable structured rule contract")
    add_rule.set_defaults(func=cmd_add_rule)

    save_sql = sub.add_parser("save-sql", help="Save a versioned SQL artifact")
    save_sql.add_argument("--root", required=True)
    save_sql_package = save_sql.add_mutually_exclusive_group(required=True)
    save_sql_package.add_argument("--package-id", help="Existing Formal Asset Package id, such as FA-0001")
    save_sql_package.add_argument("--new-package", action="store_true", help="Create a new Package for this formal QUERY")
    save_sql.add_argument("--kind", choices=ARTIFACT_KINDS, required=True)
    save_sql.add_argument("--slug")
    save_sql.add_argument("--title", required=True)
    save_sql.add_argument("--sql-file", required=True)
    save_sql.add_argument("--spec-file", help="JSON sidecar spec for the formal artifact")
    save_sql.add_argument("--status", default="draft")
    save_sql.add_argument("--business-category", default=DEFAULT_BUSINESS_CATEGORY)
    save_sql.add_argument("--analysis-type", default=DEFAULT_ANALYSIS_TYPE)
    save_sql.add_argument("--tags", default="")
    save_sql.add_argument("--metrics", default="")
    save_sql.add_argument("--dimensions", default="")
    save_sql.add_argument("--tables", default="")
    save_sql.add_argument("--intermediate-tables", default="")
    save_sql.add_argument("--grain", default="")
    save_sql.add_argument("--time-grain", default="")
    save_sql.add_argument("--reusable", action="store_true")
    save_sql.add_argument("--reuse-notes")
    save_sql.add_argument("--intent")
    save_sql.add_argument("--linked-query")
    save_sql.add_argument("--linked-validation")
    save_sql.add_argument("--linked-run")
    save_sql.add_argument("--verification-status", choices=VERIFICATION_STATUSES, default="not_applicable")
    save_sql.add_argument("--verification-note")
    save_sql.add_argument("--future-verification-plan")
    save_sql.add_argument("--change-type", choices=CHANGE_TYPES, default="auto")
    save_sql.add_argument("--branch-of", default="")
    save_sql.add_argument("--change-reason", default="")
    save_sql.add_argument("--auto-metadata", dest="auto_metadata", action="store_true", default=True)
    save_sql.add_argument("--no-auto-metadata", dest="auto_metadata", action="store_false")
    save_sql.add_argument("--notes")
    save_sql.set_defaults(func=cmd_save_sql)

    update_artifact = sub.add_parser("update-artifact", help="Update discovery metadata for a saved SQL artifact")
    update_artifact.add_argument("--root", required=True)
    update_artifact.add_argument("--package-id", required=True)
    update_artifact.add_argument("--member-id", required=True)
    update_artifact.add_argument("--kind", choices=ARTIFACT_KINDS)
    update_artifact.add_argument("--slug")
    update_artifact.add_argument("--version", type=int)
    update_artifact.add_argument("--title")
    update_artifact.add_argument("--status")
    update_artifact.add_argument("--artifact-state", choices=ARTIFACT_STATES)
    update_artifact.add_argument("--package-state", choices=ARTIFACT_STATES)
    update_artifact.add_argument("--change-type", choices=[item for item in CHANGE_TYPES if item != "auto"])
    update_artifact.add_argument("--branch-of")
    update_artifact.add_argument("--change-reason")
    update_artifact.add_argument("--replaced-by")
    update_artifact.add_argument("--supersedes")
    update_artifact.add_argument("--business-category")
    update_artifact.add_argument("--analysis-type")
    update_artifact.add_argument("--tags")
    update_artifact.add_argument("--metrics")
    update_artifact.add_argument("--dimensions")
    update_artifact.add_argument("--tables")
    update_artifact.add_argument("--intermediate-tables")
    update_artifact.add_argument("--grain")
    update_artifact.add_argument("--time-grain")
    update_artifact.add_argument("--reusable", choices=["true", "false"])
    update_artifact.add_argument("--reuse-notes")
    update_artifact.add_argument("--intent")
    update_artifact.add_argument("--linked-query")
    update_artifact.add_argument("--linked-validation")
    update_artifact.add_argument("--linked-run")
    update_artifact.add_argument("--verification-status", choices=VERIFICATION_STATUSES)
    update_artifact.add_argument("--verification-note")
    update_artifact.add_argument("--future-verification-plan")
    update_artifact.add_argument("--notes")
    update_artifact.set_defaults(func=cmd_update_artifact)

    save_table = sub.add_parser("save-table", help="Save a versioned intermediate table build SQL")
    save_table.add_argument("--root", required=True)
    save_table.add_argument("--table-name")
    save_table.add_argument("--slug")
    save_table.add_argument("--title")
    save_table.add_argument("--sql-file", required=True)
    save_table.add_argument("--status", default="draft")
    save_table.add_argument("--table-type", choices=TABLE_TYPES, default="intermediate")
    save_table.add_argument("--materialization", choices=MATERIALIZATIONS, default="partitioned_table")
    save_table.add_argument("--lifecycle", choices=TABLE_LIFECYCLES, default="project")
    save_table.add_argument("--business-category", default=DEFAULT_BUSINESS_CATEGORY)
    save_table.add_argument("--analysis-type", default=DEFAULT_ANALYSIS_TYPE)
    save_table.add_argument("--purpose")
    save_table.add_argument("--grain", default="")
    save_table.add_argument("--time-grain", default="")
    save_table.add_argument("--partition-fields", default="")
    save_table.add_argument("--primary-keys", default="")
    save_table.add_argument("--source-tables", default="")
    save_table.add_argument("--source-artifacts", default="")
    save_table.add_argument("--downstream-artifacts", default="")
    save_table.add_argument("--downstream-tables", default="")
    save_table.add_argument("--metrics", default="")
    save_table.add_argument("--dimensions", default="")
    save_table.add_argument("--tags", default="")
    save_table.add_argument("--refresh-mode", choices=REFRESH_MODES, default="manual")
    save_table.add_argument("--refresh-params")
    save_table.add_argument("--retention-days", type=int)
    save_table.add_argument("--availability-status", choices=TABLE_AVAILABILITY_STATUSES, default="unknown")
    save_table.add_argument("--availability-source", choices=TABLE_AVAILABILITY_SOURCES, default="not_checked")
    save_table.add_argument("--availability-note")
    save_table.add_argument("--unavailable-reason")
    save_table.add_argument("--last-availability-check")
    save_table.add_argument("--source-contract-mode", choices=TABLE_SOURCE_CONTRACT_MODES, default="dual_path")
    save_table.add_argument("--fallback-required", action="store_true")
    save_table.add_argument("--fallback-policy")
    save_table.add_argument("--fallback-source-tables", default="")
    save_table.add_argument("--fallback-source-artifacts", default="")
    save_table.add_argument("--fallback-sql-reference")
    save_table.add_argument("--canonical-rule-refs", default="")
    save_table.add_argument("--xml-source-refs", default="")
    save_table.add_argument("--field-contract")
    save_table.add_argument("--grain-contract")
    save_table.add_argument("--source-contract-note")
    save_table.add_argument("--owner")
    save_table.add_argument("--reusable", action="store_true")
    save_table.add_argument("--reuse-notes")
    save_table.add_argument("--validation-artifacts", default="")
    save_table.add_argument("--quality-notes")
    save_table.add_argument("--change-type", choices=TABLE_CHANGE_TYPES, default="auto")
    save_table.add_argument("--branch-of", default="")
    save_table.add_argument("--change-reason", default="")
    save_table.add_argument("--auto-metadata", dest="auto_metadata", action="store_true", default=True)
    save_table.add_argument("--no-auto-metadata", dest="auto_metadata", action="store_false")
    save_table.add_argument("--notes")
    save_table.set_defaults(func=cmd_save_table)

    update_table = sub.add_parser("update-table", help="Update metadata for a registered intermediate table")
    update_table.add_argument("--root", required=True)
    update_table.add_argument("--table-name")
    update_table.add_argument("--slug")
    update_table.add_argument("--version", type=int)
    update_table.add_argument("--title")
    update_table.add_argument("--status")
    update_table.add_argument("--table-state", choices=TABLE_STATES)
    update_table.add_argument("--change-type", choices=[item for item in TABLE_CHANGE_TYPES if item != "auto"])
    update_table.add_argument("--branch-of")
    update_table.add_argument("--change-reason")
    update_table.add_argument("--replaced-by")
    update_table.add_argument("--supersedes")
    update_table.add_argument("--table-type", choices=TABLE_TYPES)
    update_table.add_argument("--materialization", choices=MATERIALIZATIONS)
    update_table.add_argument("--lifecycle", choices=TABLE_LIFECYCLES)
    update_table.add_argument("--business-category")
    update_table.add_argument("--analysis-type")
    update_table.add_argument("--purpose")
    update_table.add_argument("--grain")
    update_table.add_argument("--time-grain")
    update_table.add_argument("--partition-fields")
    update_table.add_argument("--primary-keys")
    update_table.add_argument("--source-tables")
    update_table.add_argument("--source-artifacts")
    update_table.add_argument("--downstream-artifacts")
    update_table.add_argument("--downstream-tables")
    update_table.add_argument("--metrics")
    update_table.add_argument("--dimensions")
    update_table.add_argument("--tags")
    update_table.add_argument("--refresh-mode", choices=REFRESH_MODES)
    update_table.add_argument("--refresh-params")
    update_table.add_argument("--retention-days", type=int)
    update_table.add_argument("--availability-status", choices=TABLE_AVAILABILITY_STATUSES)
    update_table.add_argument("--availability-source", choices=TABLE_AVAILABILITY_SOURCES)
    update_table.add_argument("--availability-note")
    update_table.add_argument("--unavailable-reason")
    update_table.add_argument("--last-availability-check")
    update_table.add_argument("--source-contract-mode", choices=TABLE_SOURCE_CONTRACT_MODES)
    update_table.add_argument("--fallback-required", choices=["true", "false"])
    update_table.add_argument("--fallback-policy")
    update_table.add_argument("--fallback-source-tables")
    update_table.add_argument("--fallback-source-artifacts")
    update_table.add_argument("--fallback-sql-reference")
    update_table.add_argument("--canonical-rule-refs")
    update_table.add_argument("--xml-source-refs")
    update_table.add_argument("--field-contract")
    update_table.add_argument("--grain-contract")
    update_table.add_argument("--source-contract-note")
    update_table.add_argument("--owner")
    update_table.add_argument("--reusable", choices=["true", "false"])
    update_table.add_argument("--reuse-notes")
    update_table.add_argument("--validation-artifacts")
    update_table.add_argument("--quality-notes")
    update_table.add_argument("--notes")
    update_table.set_defaults(func=cmd_update_table)

    save_note = sub.add_parser("save-note", help="Save a conversation summary or decision note")
    save_note.add_argument("--root", required=True)
    save_note.add_argument("--kind", choices=["conversation", "decision", "assumption", "handoff"], default="conversation")
    save_note.add_argument("--slug")
    save_note.add_argument("--title", required=True)
    save_note.add_argument("--content", default="")
    save_note.add_argument("--content-file")
    save_note.set_defaults(func=cmd_save_note)

    save_run = sub.add_parser("save-run", help="Save user-executed query SQL run evidence")
    save_run.add_argument("--root", required=True)
    save_run.add_argument("--package-id", required=True)
    save_run.add_argument("--source-artifact", required=True)
    save_run.add_argument("--sql-path")
    save_run.add_argument("--slug")
    save_run.add_argument("--title")
    save_run.add_argument("--status", choices=RUN_STATUSES, required=True)
    save_run.add_argument("--row-count", type=int)
    save_run.add_argument("--checked-metrics", default="")
    save_run.add_argument("--checked-dimensions", default="")
    save_run.add_argument("--sample-fields", default="")
    save_run.add_argument("--result-summary", default="")
    save_run.add_argument("--issues", default="")
    save_run.add_argument("--user-confirmed", action="store_true")
    save_run.add_argument("--skip-reason")
    save_run.add_argument("--risk-note")
    save_run.add_argument("--future-verification-plan")
    save_run.add_argument("--definition-project")
    save_run.add_argument("--execution-project")
    save_run.add_argument("--delivery-project")
    save_run.add_argument("--concept-keys", default="")
    save_run.add_argument("--proxy-limitations")
    save_run.add_argument("--confirmed-by")
    save_run.add_argument("--evidence-file")
    save_run.add_argument("--notes")
    save_run.set_defaults(func=cmd_save_run)

    run_report = sub.add_parser("run-report", help="Show saved user run evidence")
    run_report.add_argument("--root", required=True)
    run_report.add_argument("--package-id", required=True)
    run_report.add_argument("--source-artifact")
    run_report.add_argument("--status", choices=RUN_STATUSES)
    run_report.add_argument("--json", action="store_true")
    run_report.set_defaults(func=cmd_run_report)

    index = sub.add_parser("rebuild-index", help="Rebuild project index.md")
    index.add_argument("--root", required=True)
    index.set_defaults(func=cmd_rebuild_index)

    show = sub.add_parser("show-rules", help="Print canonical rules")
    show.add_argument("--root", required=True)
    show.add_argument("--status", choices=["all", "proposed", "confirmed", "deprecated", "superseded"], default="confirmed")
    show.set_defaults(func=cmd_show_rules)

    report = sub.add_parser("rule-report", help="Show active, pending, and historical canonical rules")
    report.add_argument("--root", required=True)
    report.add_argument("--status", choices=["all", "proposed", "confirmed", "deprecated", "superseded"], default="all")
    report.add_argument("--rule-id")
    report.add_argument("--concept-key", help="Load history for one concept without reading all rule bodies")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_rule_report)

    rule_context = sub.add_parser("rule-context", help="Find relevant canonical rules and check candidate SQL against hard rule constraints")
    rule_context.add_argument("--root", required=True)
    rule_context.add_argument(
        "--user-request",
        "--query",
        dest="user_request",
        default="",
        help="Verbatim current user message; the legacy --query spelling is an alias to the same typed input.",
    )
    rule_context.add_argument("--metric", default="", help=argparse.SUPPRESS)
    rule_context.add_argument("--table", default="", help=argparse.SUPPRESS)
    rule_context.add_argument("--concept-key")
    rule_context.add_argument("--rule-id")
    rule_context.add_argument("--status", choices=["all", "proposed", "confirmed", "deprecated", "superseded"], default="confirmed")
    rule_context.add_argument("--candidate-sql", help="Optional SQL file to check against recalled hard constraints")
    rule_context.add_argument("--parent-rule-application", help="Structured parent rule_application_v1 JSON; never inferred from title or SQL comments")
    rule_context.add_argument(
        "--inheritance-mode",
        choices=[
            "none",
            "same_contract_revision",
            "lifecycle_promotion_exact_sql",
            "dashboard_derivative_same_contract",
        ],
        default="none",
    )
    rule_context.add_argument("--change-type", default="")
    rule_context.add_argument("--coverage-relation", default="")
    rule_context.add_argument("--same-execution-fingerprint", action="store_true")
    rule_context.add_argument("--same-logic-contract", action="store_true")
    rule_context.add_argument(
        "--mode",
        choices=["temporary", "generation", "review", "formalize"],
        default="generation",
        help=(
            "Rule-context caller mode. Temporary reports candidate SQL conflicts as diagnostics; "
            "formalize keeps unrelated reverse matches diagnostic, but blocks active hard constraints "
            "and structurally incompatible exact matches."
        ),
    )
    rule_context.add_argument(
        "--lifecycle-stage",
        choices=sorted(LIFECYCLE_STAGES),
        help=(
            "Asset stage used to select stage-scoped hard constraints. "
            "Defaults from --mode when omitted."
        ),
    )
    rule_context.add_argument("--limit", type=int, default=8)
    rule_context.add_argument("--min-score", type=int, default=4)
    rule_context.add_argument("--excerpt-chars", type=int, default=600)
    rule_context.add_argument("--json", action="store_true")
    rule_context.set_defaults(func=cmd_rule_context)

    table_report = sub.add_parser("table-report", help="Show current and historical intermediate tables")
    table_report.add_argument("--root", required=True)
    table_report.add_argument("--table-name")
    table_report.add_argument("--slug")
    table_report.add_argument("--version", type=int)
    table_report.add_argument("--include-history", action="store_true")
    table_report.add_argument("--json", action="store_true")
    table_report.set_defaults(func=cmd_table_report)

    describe = sub.add_parser("describe-sql", help="Infer discovery metadata and change guidance from SQL")
    describe.add_argument("--sql-file", required=True)
    describe.add_argument("--kind", choices=ARTIFACT_KINDS, default="QUERY")
    describe.add_argument("--prior-sql")
    describe.add_argument("--change-note", default="")
    describe.add_argument("--root", help="Project root, required when writing a formalize seed")
    describe.add_argument("--write-formalize-seed", action="store_true", help="Also write <sql-stem>.formalize_seed.json for this temporary QUERY SQL")
    describe.add_argument("--formalize-seed-output", help="Explicit formalize seed output path")
    describe.add_argument("--seed-title", help="Title to store in the formalize seed; defaults to SQL filename")
    describe.add_argument("--slug", help="Future formalization slug hint for the seed")
    describe.add_argument("--allow-incomplete-project-config", action="store_true")
    describe.add_argument("--json", action="store_true")
    add_function_gate_arguments(
        describe,
        selection_help="Optional explicit function route when --write-formalize-seed is used, such as [QUERY] or 【查询SQL】.",
    )
    describe.set_defaults(func=cmd_describe_sql)

    search_tables = sub.add_parser("search-tables", help="Search registered intermediate tables")
    search_tables.add_argument("--root", required=True)
    search_tables.add_argument("--query")
    search_tables.add_argument("--table-name")
    search_tables.add_argument("--business-category")
    search_tables.add_argument("--table-type", choices=TABLE_TYPES)
    search_tables.add_argument("--materialization", choices=MATERIALIZATIONS)
    search_tables.add_argument("--lifecycle", choices=TABLE_LIFECYCLES)
    search_tables.add_argument("--availability-status", choices=TABLE_AVAILABILITY_STATUSES)
    search_tables.add_argument("--source-contract-mode", choices=TABLE_SOURCE_CONTRACT_MODES)
    search_tables.add_argument("--source-table")
    search_tables.add_argument("--downstream-artifact")
    search_tables.add_argument("--fallback-source-table")
    search_tables.add_argument("--canonical-rule-ref")
    search_tables.add_argument("--xml-source-ref")
    search_tables.add_argument("--reusable", action="store_true")
    search_tables.add_argument("--include-history", action="store_true")
    search_tables.add_argument("--limit", type=int, default=20)
    search_tables.add_argument("--json", action="store_true")
    search_tables.set_defaults(func=cmd_search_tables)

    search = sub.add_parser("search-artifacts", help="Search saved SQL artifacts by metadata")
    search.add_argument("--root", required=True)
    search.add_argument("--query")
    search.add_argument("--kind", choices=ARTIFACT_KINDS)
    search.add_argument("--business-category")
    search.add_argument("--analysis-type")
    search.add_argument("--tag")
    search.add_argument("--metric")
    search.add_argument("--table")
    search.add_argument("--intermediate-table")
    search.add_argument("--reusable", action="store_true")
    search.add_argument("--include-history", action="store_true")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search_artifacts)

    for command, allowed_ids in command_routes("sql_project.py").items():
        if command in {"*", "describe-sql-write-formalize-seed"}:
            continue
        command_parser = sub.choices.get(command)
        if command_parser:
            add_function_gate_arguments(
                command_parser,
                selection_help=(
                    "Optional explicit function route for this asset-changing command, such as [QUERY] or 【查询SQL】."
                ),
            )
            command_parser.set_defaults(function_gate_allowed_ids=allowed_ids)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    allowed_ids = getattr(args, "function_gate_allowed_ids", None)
    if allowed_ids:
        purpose = f"sql_project.py {args.command}"
        try:
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=allowed_ids,
                purpose=purpose,
            )
            require_user_request(args.user_request, purpose=purpose)
        except FunctionGateError as exc:
            exit_with_gate_error(parser, exc)
    args.func(args)


if __name__ == "__main__":
    main()
