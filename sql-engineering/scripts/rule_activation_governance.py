#!/usr/bin/env python3
"""Prepare, audit, and transactionally apply canonical-rule activation v2 plans."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from capability_registry import command_function_ids
from function_gate import (
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from rule_store import (
    ACTIVATION_CONTRACT_VERSION,
    INDEX_RELATIVE_PATH,
    STORE_RELATIVE_PATH,
    RuleStore,
    RuleStoreError,
    activation_contract_source,
    activation_policy,
    atomic_write_json,
    file_sha256,
    now_iso,
    normalized_request_signatures,
    object_sha256,
    unique_strings,
)


PLAN_SCHEMA_VERSION = "canonical_rule_activation_plan_v2"
AMENDMENT_SCHEMA_VERSION = "canonical_rule_activation_amendment_v2"
RECEIPT_SCHEMA_VERSION = "canonical_rule_activation_governance_receipt_v2"
PLAN_RELATIVE_PATH = Path("rules/governance/activation-v2-plan.json")
RECEIPT_RELATIVE_PATH = Path("rules/governance/activation-v2-upgrade.json")
STAGING_RELATIVE_PATH = Path("rules/.activation-staging/activation-v2/project")
SEEDS_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "canonical-rule-activation-signature-seeds.json"
)

CONFIG_OWNED_CONCEPTS = {
    "event-time-field-dteventtime",
    "izoneareaid-default",
    "server-zone-identifier-field",
}
TECHNICAL_TERM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:\([^)]*\))?$")
PROJECT_TITLE_PREFIX_RE = re.compile(r"^RM[-_ ]?(?:AB_TEST|EXPERIMENT|BASE)\s*", flags=re.I)
OVERBROAD_ANY_OF_TERMS = {
    "登录",
    "登出",
    "登入",
    "入局",
    "出局",
    "局内",
    "战斗服",
    "模式",
    "玩家",
    "用户",
    "人数",
    "日期",
    "收入",
    "付费",
    "分摊",
    "段位",
    "活动",
    "是否有",
    "获得过",
    "获得数量",
    "获得量",
    "消耗量",
    "活跃判定",
    "累计时长",
    "分钟分布",
    "分布桶",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise RuleStoreError(f"Governance path must stay under the project root: {path}") from exc


def user_request_sha256(user_request: str) -> str:
    return hashlib.sha256(user_request.encode("utf-8")).hexdigest()


def load_signature_seeds() -> dict[str, list[dict[str, Any]]]:
    document = read_json(SEEDS_PATH)
    if document.get("schema_version") != "canonical_rule_activation_signature_seeds_v1":
        raise RuleStoreError(f"Unsupported signature seed contract: {SEEDS_PATH}")
    return document.get("concepts") or {}


def _technical_activation_terms(contract: dict[str, Any]) -> set[str]:
    values: list[str] = []
    for key in ("source_logs", "source_fields", "weak_terms"):
        values.extend(str(item) for item in contract.get(key, []) or [])
    source_signature = contract.get("source_signature") or {}
    for key in ("source_logs", "logs", "source_fields", "fields", "key_fields"):
        values.extend(str(item) for item in source_signature.get(key, []) or [])
    return {value.strip().lower() for value in values if value.strip()}


def strong_legacy_terms(contract: dict[str, Any]) -> list[str]:
    blocked = _technical_activation_terms(contract)
    result: list[str] = []
    for value in contract.get("must_have_any", []) or []:
        term = str(value or "").strip()
        lower = term.lower()
        if not term or lower in blocked:
            continue
        if TECHNICAL_TERM_RE.fullmatch(term) or re.fullmatch(r"[\d\s=<>+'.:-]+", term):
            continue
        if any(char.isdigit() for char in term) and not any("\u4e00" <= char <= "\u9fff" for char in term):
            continue
        result.append(term)
    return unique_strings(result)


def fallback_title_signature(title: str) -> list[dict[str, Any]]:
    value = PROJECT_TITLE_PREFIX_RE.sub("", str(title or "")).strip()
    value = re.sub(r"(?:基础事件)?口径$", "", value).strip()
    value = re.sub(r"规则$", "", value).strip()
    if len(value) < 4:
        return []
    return [{"label": value[:40], "any_of": [value]}]


def proposed_request_signatures(
    concept_key: str,
    rule: dict[str, Any],
    seeds: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if concept_key in seeds:
        return normalized_request_signatures({"request_signatures": seeds[concept_key]})
    contract = rule.get("activation_contract") or {}
    existing = normalized_request_signatures(contract)
    if existing:
        return existing
    terms = strong_legacy_terms(contract)
    if terms:
        return [{"label": str(rule.get("title") or concept_key)[:40], "any_of": terms}]
    return fallback_title_signature(str(rule.get("title") or ""))


def source_signature_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(contract.get("source_signature") or {})
    if source:
        return source
    logs = unique_strings(contract.get("source_logs", []) or [])
    fields = unique_strings(contract.get("source_fields", []) or [])
    return {
        key: value
        for key, value in {"source_logs": logs, "source_fields": fields}.items()
        if value
    }


def derive_default_zone(current_rules: list[dict[str, Any]]) -> int:
    candidates: set[int] = set()
    for rule in current_rules:
        if rule.get("concept_key") != "izoneareaid-default":
            continue
        text = " ".join(
            str(rule.get(key) or "") for key in ("title", "content", "notes")
        )
        for match in re.finditer(r"iZoneAreaID\D{0,20}(\d{4,8})", text, flags=re.I):
            candidates.add(int(match.group(1)))
    if len(candidates) != 1:
        raise RuleStoreError(
            "Could not derive exactly one default iZoneAreaID from the current project rule; "
            f"found {sorted(candidates)}."
        )
    return next(iter(candidates))


def config_owned_concepts(config: dict[str, Any]) -> set[str]:
    concepts = set(CONFIG_OWNED_CONCEPTS)
    default_window = config.get("default_query_window") or {}
    if default_window.get("mode") == "project_start_to_yesterday" and default_window.get("project_start_date"):
        concepts.add("project-launch-date")
    return concepts


def prepare_plan(root: Path, *, force: bool = False) -> dict[str, Any]:
    target = root / PLAN_RELATIVE_PATH
    if (root / RECEIPT_RELATIVE_PATH).exists():
        raise RuleStoreError(
            "The activation migration plan is already applied and immutable. "
            "Use an activation amendment for later changes."
        )
    if target.exists() and not force:
        raise RuleStoreError(f"Activation plan already exists: {target}. Use --force to replace it.")
    store = RuleStore(root)
    current_rules = store.load_current()
    config = read_json(root / "project_config.json")
    seeds = load_signature_seeds()
    retire = config_owned_concepts(config)
    concepts: dict[str, Any] = {}
    for rule in current_rules:
        concept_key = str(rule.get("concept_key") or "")
        if concept_key in retire:
            concepts[concept_key] = {
                "action": "retire_to_config",
                "rationale": "This value is an execution/project fact owned by project_config.json, not a business definition.",
            }
            continue
        contract = copy.deepcopy(rule.get("activation_contract") or {})
        signatures = proposed_request_signatures(concept_key, rule, seeds)
        source_signature = source_signature_from_contract(contract)
        event_signature = copy.deepcopy(contract.get("event_signature") or {})
        if concept_key == "game-mode-map":
            source_signature = {"source_fields": ["GameMode"]}
            event_signature = {
                "required_text_terms": ["GameMode"],
                "required_field_roles": [
                    {
                        "field": "GameMode",
                        "roles": ["predicate", "group_by", "final_dimension", "final_output"],
                    }
                ],
            }
        reverse = "exact_only" if event_signature else (
            "diagnostic_only" if source_signature else "disabled"
        )
        concepts[concept_key] = {
            "action": "upgrade",
            "rationale": "Replace content-derived fuzzy activation with explicit request and reverse-audit signatures.",
            "activation_policy": {
                "forward": "automatic" if signatures else "explicit_only",
                "reverse": reverse,
            },
            "request_signatures": signatures,
        }
        if source_signature:
            concepts[concept_key]["source_signature"] = source_signature
        if event_signature:
            concepts[concept_key]["event_signature"] = event_signature
        if contract.get("excludes_when"):
            concepts[concept_key]["excludes_when"] = unique_strings(
                contract.get("excludes_when", []) or []
            )
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "project_id": root.name,
        "generated_at": now_iso(),
        "config_updates": {
            "business_scope": {
                "contract_version": "project_business_scope_v1",
                "default_zone": {
                    "field": "iZoneAreaID",
                    "value": derive_default_zone(current_rules),
                    "parameter_alias": "zone_id",
                    "required_when_available": True,
                },
                "zone_identifier": {
                    "business_field": "iZoneAreaID",
                    "non_equivalent_fields": ["GameSvrId"],
                },
            }
        },
        "concepts": concepts,
    }
    atomic_write_json(target, plan)
    audit = audit_plan(root, target)
    return {"status": audit["status"], "plan": PLAN_RELATIVE_PATH.as_posix(), **audit}


def plan_problems(root: Path, plan: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        problems.append("Unsupported activation plan schema_version.")
    if plan.get("project_id") != root.name:
        problems.append("Activation plan project_id does not match the project root.")
    current = {str(rule.get("concept_key") or ""): rule for rule in RuleStore(root).load_current()}
    planned = plan.get("concepts") or {}
    missing = sorted(set(current) - set(planned))
    extra = sorted(set(planned) - set(current))
    if missing:
        problems.append("Plan is missing current concepts: " + ", ".join(missing))
    if extra:
        problems.append("Plan contains non-current concepts: " + ", ".join(extra))
    for concept_key, row in sorted(planned.items()):
        if not isinstance(row, dict):
            problems.append(f"{concept_key}: plan entry must be an object")
            continue
        action = row.get("action")
        if action not in {"upgrade", "retire_to_config"}:
            problems.append(f"{concept_key}: unsupported action {action!r}")
            continue
        if not str(row.get("rationale") or "").strip():
            problems.append(f"{concept_key}: rationale is required")
        if action == "upgrade":
            policy = row.get("activation_policy") or {}
            forward = policy.get("forward")
            reverse = policy.get("reverse")
            signatures = normalized_request_signatures(
                {"request_signatures": row.get("request_signatures", []) or []}
            )
            if forward not in {"automatic", "explicit_only", "disabled"}:
                problems.append(f"{concept_key}: invalid forward policy")
            if reverse not in {"exact_only", "diagnostic_only", "disabled"}:
                problems.append(f"{concept_key}: invalid reverse policy")
            if forward == "automatic" and not signatures:
                problems.append(f"{concept_key}: automatic activation requires request_signatures")
            for signature in signatures:
                broad = sorted(set(signature.get("any_of", []) or []) & OVERBROAD_ANY_OF_TERMS)
                if broad:
                    problems.append(
                        f"{concept_key}: overbroad any_of terms must be replaced by a phrase or all_of: "
                        + ", ".join(broad)
                    )
            if reverse != "disabled" and not (row.get("event_signature") or row.get("source_signature")):
                problems.append(f"{concept_key}: reverse activation requires an explicit source/event signature")
    business_scope = (plan.get("config_updates") or {}).get("business_scope") or {}
    if business_scope.get("contract_version") != "project_business_scope_v1":
        problems.append("config_updates.business_scope is missing or invalid")
    return problems


def audit_plan(root: Path, plan_path: Path) -> dict[str, Any]:
    plan = read_json(plan_path)
    receipt_path = root / RECEIPT_RELATIVE_PATH
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        receipt_plan = receipt.get("plan") or {}
        applied_problems: list[str] = []
        if receipt_plan.get("path") != relative_to_root(plan_path, root):
            applied_problems.append("Applied receipt points to a different plan path.")
        if receipt_plan.get("file_sha256") != file_sha256(plan_path):
            applied_problems.append("Applied activation plan changed after publication.")
        for action in receipt.get("actions", []) or []:
            target = root / str(action.get("path") or "")
            if not target.exists():
                applied_problems.append(f"Applied rule definition is missing: {action.get('path')}")
        validation = RuleStore(root).validate_store(
            require_no_legacy=True,
            require_activation_v2=True,
        )
        applied_problems.extend(validation.get("errors", []) or [])
        expected_scope = (plan.get("config_updates") or {}).get("business_scope") or {}
        actual_scope = read_json(root / "project_config.json").get("business_scope") or {}
        if expected_scope != actual_scope:
            applied_problems.append("project_config.business_scope no longer matches the applied plan.")
        actions = receipt.get("actions", []) or []
        return {
            "status": "ok" if not applied_problems else "error",
            "phase": "applied",
            "project_id": root.name,
            "plan": relative_to_root(plan_path, root),
            "plan_sha256": file_sha256(plan_path),
            "current_concepts": len(RuleStore(root).load_current()),
            "upgrade_count": sum(row.get("action") == "upgrade" for row in actions),
            "retire_to_config_count": sum(
                row.get("action") == "retire_to_config" for row in actions
            ),
            "problems": applied_problems,
        }
    problems = plan_problems(root, plan)
    actions = [row.get("action") for row in (plan.get("concepts") or {}).values() if isinstance(row, dict)]
    return {
        "status": "ok" if not problems else "error",
        "project_id": root.name,
        "plan": relative_to_root(plan_path, root),
        "plan_sha256": file_sha256(plan_path),
        "current_concepts": len(RuleStore(root).load_current()),
        "upgrade_count": actions.count("upgrade"),
        "retire_to_config_count": actions.count("retire_to_config"),
        "problems": problems,
    }


def _new_authorization(function_selection: str, request: str, status: str) -> dict[str, Any]:
    return {
        "contract_version": "rule_write_authorization_v1",
        "function_id": "RULES",
        "selection": function_selection,
        "requested_status": status,
        "user_request_sha256": user_request_sha256(request),
        "explicit_user_selection": True,
        "authorized_at": now_iso(),
    }


def _clean_record(rule: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(rule)
    result.pop("_rule_store", None)
    return result


def _next_rule_version(store: RuleStore, concept_key: str) -> int:
    concept = store.concept(concept_key)
    return int(concept.get("latest_rule_version") or 0) + 1


def _v2_contract(rule: dict[str, Any], plan_row: dict[str, Any]) -> dict[str, Any]:
    old = copy.deepcopy(rule.get("activation_contract") or {})
    old.pop("must_have_any", None)
    old.pop("weak_terms", None)
    old["contract_version"] = ACTIVATION_CONTRACT_VERSION
    old["status"] = "confirmed"
    old["activation_policy"] = copy.deepcopy(plan_row["activation_policy"])
    old["request_signatures"] = normalized_request_signatures(
        {"request_signatures": plan_row.get("request_signatures", []) or []}
    )
    for key in ("source_signature", "event_signature", "excludes_when"):
        if key in plan_row:
            old[key] = copy.deepcopy(plan_row[key])
    return old


def _version_record(
    store: RuleStore,
    rule: dict[str, Any],
    plan_row: dict[str, Any],
    *,
    plan_relative: str,
    function_selection: str,
    user_request: str,
) -> dict[str, Any]:
    concept_key = str(rule.get("concept_key") or "")
    result = _clean_record(rule)
    previous = f"{rule.get('rule_id')}@v{rule.get('version')}"
    action = plan_row["action"]
    result["version"] = _next_rule_version(store, concept_key)
    result["status"] = "deprecated" if action == "retire_to_config" else "confirmed"
    result["confirmed_by_user"] = True
    result["supersedes"] = previous
    result["created_at"] = now_iso()
    result["updated_at"] = result["created_at"]
    result["source"] = "rule_activation_governance.py"
    result["source_evidence"] = plan_relative
    result["change_authorization"] = _new_authorization(
        function_selection,
        user_request,
        result["status"],
    )
    note = str(result.get("notes") or "").strip()
    migration_note = (
        "Current value moved to project_config.json; the immutable prior rule remains as history."
        if action == "retire_to_config"
        else "Activation upgraded to explicit request signatures and isolated reverse SQL audit."
    )
    result["notes"] = " ".join(value for value in (note, migration_note) if value)
    if action == "upgrade":
        result["activation_contract"] = _v2_contract(rule, plan_row)
    return result


def _copy_stage_root(root: Path, stage_root: Path) -> None:
    stage_container = root / STAGING_RELATIVE_PATH.parts[0] / STAGING_RELATIVE_PATH.parts[1]
    if stage_container.exists():
        shutil.rmtree(stage_container)
    stage_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "manifest.json", stage_root / "manifest.json")
    shutil.copy2(root / "project_config.json", stage_root / "project_config.json")
    shutil.copytree(
        root / "rules",
        stage_root / "rules",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".activation-staging"),
    )


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".activation-tmp")
    shutil.copy2(source, temp)
    os.replace(temp, target)


def upgrade_plan(
    root: Path,
    plan_path: Path,
    *,
    function_selection: str,
    user_request: str,
) -> dict[str, Any]:
    audit = audit_plan(root, plan_path)
    if audit["status"] != "ok":
        raise RuleStoreError("Activation plan failed audit:\n- " + "\n- ".join(audit["problems"]))
    if audit.get("phase") == "applied":
        return {
            "status": "already_applied",
            "project_id": root.name,
            "receipt": RECEIPT_RELATIVE_PATH.as_posix(),
            "validation": RuleStore(root).validate_store(
                require_no_legacy=True,
                require_activation_v2=True,
            ),
        }
    plan = read_json(plan_path)
    original_store_sha = file_sha256(root / STORE_RELATIVE_PATH)
    original_config_sha = file_sha256(root / "project_config.json")
    original_paths = {
        str(ref.get("path") or "")
        for concept in (RuleStore(root).load_store().get("concepts") or {}).values()
        for ref in concept.get("versions", []) or []
    }
    stage_root = root / STAGING_RELATIVE_PATH
    _copy_stage_root(root, stage_root)
    stage_store = RuleStore(stage_root)
    plan_relative = relative_to_root(plan_path, root)
    actions: list[dict[str, Any]] = []
    current_rules = {str(rule.get("concept_key") or ""): rule for rule in stage_store.load_current()}
    for concept_key in sorted(current_rules):
        plan_row = plan["concepts"][concept_key]
        record = _version_record(
            stage_store,
            current_rules[concept_key],
            plan_row,
            plan_relative=plan_relative,
            function_selection=function_selection,
            user_request=user_request,
        )
        saved = stage_store.write_new_version(record)
        actions.append(
            {
                "concept_key": concept_key,
                "action": plan_row["action"],
                "previous_rule_id": current_rules[concept_key].get("rule_id"),
                "previous_rule_version": current_rules[concept_key].get("version"),
                "new_rule_version": record["version"],
                "new_store_version": saved["store_version"],
                "path": saved["path"],
                "record_sha256": saved["record_sha256"],
            }
        )
    config = read_json(stage_root / "project_config.json")
    config["business_scope"] = copy.deepcopy(plan["config_updates"]["business_scope"])
    config["updated_at"] = now_iso()
    atomic_write_json(stage_root / "project_config.json", config)
    validation = stage_store.validate_store(require_no_legacy=True, require_activation_v2=True)
    if validation.get("status") != "ok":
        raise RuleStoreError("Staged activation upgrade failed validation:\n- " + "\n- ".join(validation["errors"]))
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "verified",
        "project_id": root.name,
        "created_at": now_iso(),
        "authorization": _new_authorization(function_selection, user_request, "confirmed"),
        "plan": {"path": plan_relative, "file_sha256": file_sha256(plan_path)},
        "before": {
            "store_file_sha256": original_store_sha,
            "project_config_file_sha256": original_config_sha,
        },
        "after": {
            "store_file_sha256": file_sha256(stage_root / STORE_RELATIVE_PATH),
            "activation_index_file_sha256": file_sha256(stage_root / INDEX_RELATIVE_PATH),
            "project_config_file_sha256": file_sha256(stage_root / "project_config.json"),
        },
        "actions": actions,
        "validation": validation,
    }
    atomic_write_json(stage_root / RECEIPT_RELATIVE_PATH, receipt)

    if file_sha256(root / STORE_RELATIVE_PATH) != original_store_sha:
        raise RuleStoreError("Rule store changed while the governance transaction was staged.")
    if file_sha256(root / "project_config.json") != original_config_sha:
        raise RuleStoreError("Project config changed while the governance transaction was staged.")
    new_paths = [action["path"] for action in actions if action["path"] not in original_paths]
    for relative in new_paths:
        source = stage_root / relative
        target = root / relative
        if target.exists() and file_sha256(target) != file_sha256(source):
            raise RuleStoreError(f"Immutable definition collision during publish: {relative}")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for relative in (
        Path("project_config.json"),
        STORE_RELATIVE_PATH,
        INDEX_RELATIVE_PATH,
        RECEIPT_RELATIVE_PATH,
    ):
        _atomic_copy(stage_root / relative, root / relative)
    final_validation = RuleStore(root).validate_store(
        require_no_legacy=True,
        require_activation_v2=True,
    )
    if final_validation.get("status") != "ok":
        raise RuleStoreError("Published activation upgrade failed validation:\n- " + "\n- ".join(final_validation["errors"]))
    stage_container = root / STAGING_RELATIVE_PATH.parts[0] / STAGING_RELATIVE_PATH.parts[1]
    if stage_container.exists():
        shutil.rmtree(stage_container)
    return {
        "status": "ok",
        "project_id": root.name,
        "receipt": RECEIPT_RELATIVE_PATH.as_posix(),
        "plan_sha256": receipt["plan"]["file_sha256"],
        "upgraded": sum(action["action"] == "upgrade" for action in actions),
        "retired_to_config": sum(action["action"] == "retire_to_config" for action in actions),
        "validation": final_validation,
    }


def apply_amendment(
    root: Path,
    amendment_path: Path,
    *,
    function_selection: str,
    user_request: str,
) -> dict[str, Any]:
    amendment = read_json(amendment_path)
    if amendment.get("schema_version") != AMENDMENT_SCHEMA_VERSION:
        raise RuleStoreError("Unsupported activation amendment schema_version.")
    if amendment.get("project_id") != root.name:
        raise RuleStoreError("Activation amendment project_id does not match the project root.")
    concept_key = str(amendment.get("concept_key") or "")
    plan_row = {
        key: copy.deepcopy(value)
        for key, value in amendment.items()
        if key in {
            "activation_policy",
            "request_signatures",
            "source_signature",
            "event_signature",
            "excludes_when",
            "rationale",
        }
    }
    plan_row["action"] = "upgrade"
    problems = []
    policy = plan_row.get("activation_policy") or {}
    signatures = normalized_request_signatures(
        {"request_signatures": plan_row.get("request_signatures", []) or []}
    )
    if policy.get("forward") == "automatic" and not signatures:
        problems.append("automatic activation requires request_signatures")
    broad = sorted(
        {
            term
            for signature in signatures
            for term in signature.get("any_of", []) or []
            if term in OVERBROAD_ANY_OF_TERMS
        }
    )
    if broad:
        problems.append("overbroad any_of terms: " + ", ".join(broad))
    if problems:
        raise RuleStoreError("Activation amendment failed audit: " + "; ".join(problems))
    original_store = RuleStore(root)
    current = original_store.load_current([concept_key])
    if len(current) != 1:
        raise RuleStoreError(f"Activation amendment requires one current rule for {concept_key}.")
    if activation_contract_source(current[0]) != "stored_v2":
        raise RuleStoreError(f"Activation amendment requires an existing v2 contract for {concept_key}.")
    desired = _v2_contract(current[0], plan_row)
    if object_sha256(desired) == object_sha256(current[0].get("activation_contract") or {}):
        return {
            "status": "unchanged",
            "project_id": root.name,
            "concept_key": concept_key,
            "message": "Current activation contract already matches the amendment.",
        }
    original_store_sha = file_sha256(root / STORE_RELATIVE_PATH)
    stage_root = root / STAGING_RELATIVE_PATH
    _copy_stage_root(root, stage_root)
    stage_store = RuleStore(stage_root)
    stage_current = stage_store.load_current([concept_key])[0]
    record = _version_record(
        stage_store,
        stage_current,
        plan_row,
        plan_relative=relative_to_root(amendment_path, root),
        function_selection=function_selection,
        user_request=user_request,
    )
    saved = stage_store.write_new_version(record)
    validation = stage_store.validate_store(require_no_legacy=True, require_activation_v2=True)
    if validation.get("status") != "ok":
        raise RuleStoreError("Staged activation amendment failed validation:\n- " + "\n- ".join(validation["errors"]))
    receipt_relative = Path("rules/governance/receipts") / (
        f"{concept_key}-activation-v{record['version']:03d}.json"
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "verified",
        "project_id": root.name,
        "created_at": now_iso(),
        "authorization": _new_authorization(function_selection, user_request, "confirmed"),
        "amendment": {
            "path": relative_to_root(amendment_path, root),
            "file_sha256": file_sha256(amendment_path),
        },
        "action": {
            "concept_key": concept_key,
            "previous_rule_version": stage_current.get("version"),
            "new_rule_version": record["version"],
            **saved,
        },
        "validation": validation,
    }
    atomic_write_json(stage_root / receipt_relative, receipt)
    if file_sha256(root / STORE_RELATIVE_PATH) != original_store_sha:
        raise RuleStoreError("Rule store changed while the activation amendment was staged.")
    definition_target = root / saved["path"]
    if definition_target.exists():
        raise RuleStoreError(f"Immutable definition collision: {saved['path']}")
    definition_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stage_root / saved["path"], definition_target)
    for relative in (STORE_RELATIVE_PATH, INDEX_RELATIVE_PATH, receipt_relative):
        _atomic_copy(stage_root / relative, root / relative)
    final = RuleStore(root).validate_store(require_no_legacy=True, require_activation_v2=True)
    if final.get("status") != "ok":
        raise RuleStoreError("Published activation amendment failed validation:\n- " + "\n- ".join(final["errors"]))
    stage_container = root / STAGING_RELATIVE_PATH.parts[0] / STAGING_RELATIVE_PATH.parts[1]
    if stage_container.exists():
        shutil.rmtree(stage_container)
    return {
        "status": "ok",
        "project_id": root.name,
        "concept_key": concept_key,
        "new_rule_version": record["version"],
        "receipt": receipt_relative.as_posix(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "audit", "upgrade", "amend"):
        item = subparsers.add_parser(name)
        item.add_argument("--root", required=True)
        item.add_argument("--plan")
        item.add_argument("--format", choices=("json", "summary"), default="json")
        if name == "prepare":
            item.add_argument("--force", action="store_true")
        if name == "amend":
            item.add_argument("--amendment", required=True)
        add_function_gate_arguments(
            item,
            selection_help="Activation governance requires RULES or SKILL_EVOLUTION authorization.",
        )
    return parser.parse_args()


def print_result(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"status: {payload.get('status')}")
    print(f"project: {payload.get('project_id')}")
    for key in ("current_concepts", "upgrade_count", "retire_to_config_count", "upgraded", "retired_to_config"):
        if key in payload:
            print(f"{key}: {payload[key]}")
    for problem in payload.get("problems", []) or []:
        print(f"ERROR: {problem}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    root = Path(args.root).resolve()
    try:
        require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("rule_activation_governance.py", args.command),
            purpose="canonical rule activation governance",
        )
        if args.command in {"prepare", "upgrade", "amend"}:
            require_user_request(args.user_request, purpose="write canonical rule governance assets")
        plan_path = Path(args.plan).resolve() if args.plan else root / PLAN_RELATIVE_PATH
        relative_to_root(plan_path, root)
        if args.command == "prepare":
            payload = prepare_plan(root, force=bool(args.force))
        elif args.command == "audit":
            payload = audit_plan(root, plan_path)
        elif args.command == "upgrade":
            payload = upgrade_plan(
                root,
                plan_path,
                function_selection=str(args.function_selection or ""),
                user_request=str(args.user_request or ""),
            )
        else:
            amendment_path = Path(args.amendment).resolve()
            relative_to_root(amendment_path, root)
            payload = apply_amendment(
                root,
                amendment_path,
                function_selection=str(args.function_selection or ""),
                user_request=str(args.user_request or ""),
            )
    except (OSError, ValueError, RuleStoreError, json.JSONDecodeError) as exc:
        payload = {"status": "error", "project_id": root.name, "problems": [str(exc)]}
    print_result(payload, args.format)
    if payload.get("status") == "error":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
