#!/usr/bin/env python3
"""Maintain an incremental semantic overlay over the all-status asset catalog."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from asset_provenance import now_iso
from capability_registry import command_function_ids
from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)


SCHEMA_VERSION = "sql_asset_organization_v2"
DECISIONS_SCHEMA_VERSION = "sql_asset_organization_decisions_v1"
SUPPORTED_CATALOG_SCHEMAS = {"sql_asset_catalog_v1", "sql_asset_catalog_v2"}
DEFAULT_TAXONOMY = Path(__file__).resolve().parents[1] / "assets" / "default_asset_taxonomy.json"
DEFAULT_OUTPUT_NAME = "asset_organization.json"
CLASSIFIER_VERSION = "asset-semantic-classifier-v3"
MAX_SCAN_ITEMS = 100
CURATION_STATES = {"current", "needs_semantic_review", "stale_semantics", "catalog_missing"}
LOCAL_WORKSPACE_ASSET_TOKEN = ":temporary_query:"
LOCAL_PROMOTION_LEDGER_TOKEN = ":promotion_ledger:"
LEGACY_ANALYTICAL_ID_TOKENS = (
    ":query:",
    ":dashboard:",
    ":validation:",
    ":run_evidence:",
    ":result:",
    ":analysis_workbook:",
    ":comparison_workbook:",
    ":visualization:",
    ":derived_output:",
    ":export:",
)
CLASSIFICATION_SOURCES = {"deterministic", "inherited", "llm", "human"}

EXPLICIT_CATEGORY_MAP = {
    "new_user": ("user_lifecycle", "new_user"),
    "active_user": ("user_lifecycle", "active_user"),
    "retention": ("user_lifecycle", "retention"),
    "return_user": ("user_lifecycle", "return_user"),
    "churn": ("user_lifecycle", "churn"),
    "battle_behavior": ("battle_gameplay", "battle_behavior"),
    "game_mode": ("battle_gameplay", "game_mode"),
    "battle_server": ("battle_gameplay", "battle_server"),
    "economy": ("economy_resources", "economy"),
    "items": ("economy_resources", "items"),
    "commercialization": ("economy_resources", "commercialization"),
    "content_progression": ("progression", "content_progression"),
    "growth": ("progression", "growth"),
    "bp_analysis": ("progression", "battle_pass"),
    "battle_pass": ("progression", "battle_pass"),
    "social": ("social_team", "social"),
    "team": ("social_team", "team"),
    "funnel": ("funnel_conversion", "funnel"),
    "conversion": ("funnel_conversion", "conversion"),
    "ab_compare": ("operations_quality", "experiment_compare"),
    "experiment_compare": ("operations_quality", "experiment_compare"),
    "technical_quality": ("operations_quality", "technical_quality"),
    "ops_health": ("operations_quality", "technical_quality"),
    "operations": ("operations_quality", "operations"),
}

TEXT_CATEGORY_RULES = [
    (("新增", "新用户", "首登", "首日用户"), ("user_lifecycle", "new_user")),
    (("活跃", "dau", "mau"), ("user_lifecycle", "active_user")),
    (("留存", "次留", "三留", "七留"), ("user_lifecycle", "retention")),
    (("回流",), ("user_lifecycle", "return_user")),
    (("流失",), ("user_lifecycle", "churn")),
    (("战斗服", "battlesrvid"), ("battle_gameplay", "battle_server")),
    (("模式", "gamemode", "玩法"), ("battle_gameplay", "game_mode")),
    (("战斗", "对局"), ("battle_gameplay", "battle_behavior")),
    (("商业化", "付费", "商城", "抽奖"), ("economy_resources", "commercialization")),
    (("道具", "资源", "货币", "产出", "消耗"), ("economy_resources", "items")),
    (("经济",), ("economy_resources", "economy")),
    (("通行证", "赛季"), ("progression", "battle_pass")),
    (("成长", "养成", "等级"), ("progression", "growth")),
    (("任务", "进度", "章节", "内容"), ("progression", "content_progression")),
    (("组队", "队伍"), ("social_team", "team")),
    (("社交", "好友", "公会"), ("social_team", "social")),
    (("漏斗",), ("funnel_conversion", "funnel")),
    (("转化", "有效率"), ("funnel_conversion", "conversion")),
    (("abtest", "a/b", "实验", "对比"), ("operations_quality", "experiment_compare")),
    (("性能", "报错", "错误", "质量"), ("operations_quality", "technical_quality")),
]

GOVERNANCE_KIND_MAP = {
    "project": ("asset_governance", "project_metadata"),
    "source_catalog": ("asset_governance", "source_metadata"),
    "rule": ("asset_governance", "canonical_rule"),
    "rule_concept": ("asset_governance", "canonical_rule"),
    "rule_dictionary": ("asset_governance", "canonical_rule"),
    "rule_review": ("asset_governance", "canonical_rule"),
    "rule_concept_registry": ("asset_governance", "canonical_rule"),
    "knowledge_dataset": ("asset_governance", "knowledge_dataset"),
    "knowledge_binding": ("asset_governance", "knowledge_dataset"),
    "repository_read_model": ("asset_governance", "review_read_model"),
    "dashboard_review": ("asset_governance", "review_read_model"),
    "sql_review": ("asset_governance", "review_read_model"),
    "documentation": ("asset_governance", "documentation"),
    "consumer_contract": ("asset_governance", "integration_contract"),
}

CHILD_TO_PARENT_RELATIONS = {
    "derived_from",
    "derived_from_query",
    "derived_from_workspace",
    "result_of_run",
    "evidence_for",
    "previous_version",
    "branched_from",
    "member_of_package",
}
PARENT_TO_CHILD_RELATIONS = {
    "has_derived_output",
    "has_run_evidence",
    "has_result",
    "validated_by",
    "promoted_to",
    "next_version",
    "has_member",
    "has_current_member",
    "has_formal_query",
    "has_dashboard_delivery",
    "has_validation",
    "has_evidence",
}


def excluded_legacy_entry(asset_id: str) -> bool:
    normalized = clean_text(asset_id).lower()
    if LOCAL_WORKSPACE_ASSET_TOKEN in normalized or LOCAL_PROMOTION_LEDGER_TOKEN in normalized:
        return True
    return ":formal_asset_package:" not in normalized and any(
        token in normalized for token in LEGACY_ANALYTICAL_ID_TOKENS
    )


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return copy.deepcopy(default)
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return copy.deepcopy(default)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalized_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return sorted(value, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))


def taxonomy_index(taxonomy: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    domains: dict[str, dict[str, Any]] = {}
    topics: dict[str, tuple[str, dict[str, Any]]] = {}
    for domain in list_value(taxonomy.get("domains")):
        if not isinstance(domain, dict):
            continue
        domain_id = clean_text(domain.get("id"))
        domains[domain_id] = domain
        for topic in list_value(domain.get("topics")):
            if isinstance(topic, dict):
                topics[clean_text(topic.get("id"))] = (domain_id, topic)
    analysis_types = {
        clean_text(item.get("id")): item
        for item in list_value(taxonomy.get("analysis_types"))
        if isinstance(item, dict)
    }
    return domains, topics, analysis_types


def validate_taxonomy(taxonomy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if taxonomy.get("schema_version") != "sql_asset_taxonomy_v1":
        problems.append("taxonomy schema_version must be sql_asset_taxonomy_v1")
    domains, topics, analysis_types = taxonomy_index(taxonomy)
    if not domains or not topics:
        problems.append("taxonomy must define domains and topics")
    if "unclassified" not in topics or topics.get("unclassified", ("", {}))[0] != "other":
        problems.append("taxonomy must define other/unclassified")
    if "unknown" not in analysis_types:
        problems.append("taxonomy must define analysis type unknown")
    return problems


def asset_fingerprint(asset: dict[str, Any]) -> str:
    facts = dict_value(asset.get("facts"))
    semantic_fact_keys = (
        "business_category",
        "business_topic",
        "business_question",
        "analysis_type",
        "metrics",
        "dimensions",
        "filters",
        "source_logs",
        "tables",
        "grain",
        "time_grain",
        "tags",
        "dataset_id",
        "concept_key",
        "document_kind",
        "audience",
        "consumer_scope",
        "declared_skill_version",
        "contract_kind",
        "contract_version",
    )
    semantic_facts: dict[str, Any] = {}
    for key in semantic_fact_keys:
        value = facts.get(key)
        semantic_facts[key] = normalized_list(value) if isinstance(value, list) else value
    payload = {
        "asset_id": clean_text(asset.get("asset_id")),
        "asset_kind": clean_text(asset.get("asset_kind")),
        "project_id": clean_text(asset.get("project_id")),
        "formal_asset_id": clean_text(asset.get("formal_asset_id")),
        "formal_member_id": clean_text(asset.get("formal_member_id")),
        "title": clean_text(asset.get("title")),
        "summary": clean_text(asset.get("summary")),
        "facts": semantic_facts,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def labels_for(taxonomy: dict[str, Any], domain_id: str, topic_id: str) -> tuple[str, str]:
    domains, topics, _ = taxonomy_index(taxonomy)
    domain = domains.get(domain_id, domains.get("other", {}))
    topic_domain, topic = topics.get(topic_id, topics.get("unclassified", ("other", {})))
    if topic_domain != clean_text(domain.get("id")):
        domain = domains.get(topic_domain, domain)
    return clean_text(domain.get("label")), clean_text(topic.get("label"))


def infer_category(asset: dict[str, Any]) -> tuple[str, str, float, bool]:
    facts = dict_value(asset.get("facts"))
    explicit_values = [clean_text(facts.get("business_category")), clean_text(facts.get("business_topic"))]
    for value in explicit_values:
        key = value.lower().replace("-", "_").replace(" ", "_")
        if key in EXPLICIT_CATEGORY_MAP:
            domain_id, topic_id = EXPLICIT_CATEGORY_MAP[key]
            return domain_id, topic_id, 0.98, True
    explicit_text = " ".join(item for item in explicit_values if item).lower()
    if explicit_text:
        for keywords, classification in TEXT_CATEGORY_RULES:
            if any(keyword.lower() in explicit_text for keyword in keywords):
                return classification[0], classification[1], 0.9, True
    kind = clean_text(asset.get("asset_kind"))
    if kind in GOVERNANCE_KIND_MAP:
        domain_id, topic_id = GOVERNANCE_KIND_MAP[kind]
        return domain_id, topic_id, 0.99, True
    evidence_text = " ".join(
        [
            clean_text(asset.get("title")),
            clean_text(asset.get("summary")),
            clean_text(facts.get("business_question")),
        ]
    ).lower()
    for keywords, classification in TEXT_CATEGORY_RULES:
        if any(keyword.lower() in evidence_text for keyword in keywords):
            return classification[0], classification[1], 0.68, False
    return "other", "unclassified", 0.25, False


def infer_analysis_type(asset: dict[str, Any]) -> str:
    facts = dict_value(asset.get("facts"))
    explicit = clean_text(facts.get("analysis_type")).lower()
    text = " ".join((explicit, clean_text(asset.get("title")).lower(), clean_text(asset.get("summary")).lower()))
    rules = [
        (("retention", "留存", "次留"), "retention"),
        (("funnel", "漏斗"), "funnel"),
        (("distribution", "分布", "分桶", "占比"), "distribution"),
        (("detail", "明细"), "detail"),
        (("compare", "comparison", "对比", "实验"), "comparison"),
    ]
    for keywords, analysis_type in rules:
        if any(keyword in text for keyword in keywords):
            return analysis_type
    kind = clean_text(asset.get("asset_kind"))
    if kind == "dashboard":
        return "dashboard"
    if kind == "validation":
        return "validation"
    if kind in {"result", "analysis_workbook", "comparison_workbook", "visualization", "run_evidence"}:
        return "evidence"
    if kind in GOVERNANCE_KIND_MAP:
        return "governance"
    if kind in {"query", "formal_asset_package", "formal_asset_member", "temporary_query", "intermediate_table"}:
        return "aggregate"
    return "unknown"


def parent_map(catalog: dict[str, Any]) -> dict[str, list[str]]:
    parents: dict[str, list[str]] = defaultdict(list)
    for relation in list_value(catalog.get("relationships")):
        if not isinstance(relation, dict):
            continue
        source = clean_text(relation.get("source_asset_id"))
        target = clean_text(relation.get("target_asset_id"))
        kind = clean_text(relation.get("relation"))
        if not source or not target:
            continue
        if kind in CHILD_TO_PARENT_RELATIONS and target not in parents[source]:
            parents[source].append(target)
        elif kind in PARENT_TO_CHILD_RELATIONS and source not in parents[target]:
            parents[target].append(source)
    return parents


def semantic_summary(asset: dict[str, Any]) -> str:
    return clean_text(asset.get("summary")) or clean_text(asset.get("title"))


def make_entry(
    asset: dict[str, Any],
    taxonomy: dict[str, Any],
    *,
    domain_id: str,
    topic_id: str,
    analysis_type_id: str,
    confidence: float,
    source: str,
    curation_state: str,
    inherited_from: str = "",
    notes: str = "",
) -> dict[str, Any]:
    domain_label, topic_label = labels_for(taxonomy, domain_id, topic_id)
    _, _, analysis_types = taxonomy_index(taxonomy)
    analysis_label = clean_text(analysis_types.get(analysis_type_id, analysis_types.get("unknown", {})).get("label"))
    facts = dict_value(asset.get("facts"))
    tags = [clean_text(item) for item in list_value(facts.get("tags")) if clean_text(item)]
    return {
        "asset_fingerprint": asset_fingerprint(asset),
        "catalog_presence": True,
        "formal_asset_id": clean_text(asset.get("formal_asset_id")),
        "formal_member_id": clean_text(asset.get("formal_member_id")),
        "business_domain": {"id": domain_id, "label": domain_label},
        "business_topic": {"id": topic_id, "label": topic_label},
        "navigation_path": [domain_label, topic_label],
        "analysis_type": {"id": analysis_type_id, "label": analysis_label},
        "semantic_summary": semantic_summary(asset),
        "tags": sorted(set(tags)),
        "curation_state": curation_state,
        "classification_source": source,
        "confidence": round(max(0.0, min(1.0, float(confidence))), 3),
        "inherited_from_asset_id": inherited_from,
        "reviewed_at": now_iso(),
        "notes": notes,
    }


def build_summary(catalog: dict[str, Any], entries: dict[str, Any]) -> dict[str, Any]:
    state_counts = Counter()
    domain_counts = Counter()
    topic_counts = Counter()
    source_counts = Counter()
    catalog_ids = {
        clean_text(item.get("asset_id"))
        for item in list_value(catalog.get("assets"))
        if isinstance(item, dict)
    }
    for asset_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        state_counts[clean_text(entry.get("curation_state"))] += 1
        domain_counts[clean_text(dict_value(entry.get("business_domain")).get("id"))] += 1
        topic_counts[clean_text(dict_value(entry.get("business_topic")).get("id"))] += 1
        source_counts[clean_text(entry.get("classification_source"))] += 1
    return {
        "catalog_asset_count": len(catalog_ids),
        "organization_entry_count": len(entries),
        "covered_catalog_asset_count": sum(1 for asset_id in catalog_ids if asset_id in entries),
        "needs_semantic_review_count": state_counts["needs_semantic_review"] + state_counts["stale_semantics"],
        "catalog_missing_count": state_counts["catalog_missing"],
        "entries_by_curation_state": dict(sorted(state_counts.items())),
        "entries_by_domain": dict(sorted(domain_counts.items())),
        "entries_by_topic": dict(sorted(topic_counts.items())),
        "entries_by_classification_source": dict(sorted(source_counts.items())),
    }


def catalog_relative_path(catalog_path: Path) -> str:
    try:
        return catalog_path.resolve().relative_to(catalog_path.resolve().parents[2]).as_posix()
    except (ValueError, IndexError):
        return catalog_path.name


def build_payload(
    catalog: dict[str, Any],
    catalog_path: Path,
    taxonomy: dict[str, Any],
    existing: dict[str, Any] | None = None,
    *,
    catalog_fingerprint: str = "",
) -> dict[str, Any]:
    existing_entries = dict_value((existing or {}).get("entries"))
    existing_classifier = clean_text(dict_value((existing or {}).get("policy")).get("classifier_version"))
    assets = {
        clean_text(item.get("asset_id")): item
        for item in list_value(catalog.get("assets"))
        if isinstance(item, dict) and clean_text(item.get("asset_id"))
    }
    parents = parent_map(catalog)
    entries: dict[str, Any] = {}
    unresolved: set[str] = set()

    for asset_id, asset in assets.items():
        current_fingerprint = asset_fingerprint(asset)
        old = copy.deepcopy(dict_value(existing_entries.get(asset_id)))
        if (
            old
            and clean_text(old.get("asset_fingerprint")) == current_fingerprint
            and (
                clean_text(old.get("classification_source")) in {"human", "llm"}
                or existing_classifier == CLASSIFIER_VERSION
            )
        ):
            old["catalog_presence"] = True
            old["formal_asset_id"] = clean_text(asset.get("formal_asset_id"))
            old["formal_member_id"] = clean_text(asset.get("formal_member_id"))
            entries[asset_id] = old
            continue
        if old and clean_text(old.get("classification_source")) in {"human", "llm"}:
            old["catalog_presence"] = True
            old["formal_asset_id"] = clean_text(asset.get("formal_asset_id"))
            old["formal_member_id"] = clean_text(asset.get("formal_member_id"))
            old["curation_state"] = "stale_semantics"
            old["notes"] = clean_text(old.get("notes")) or "Asset semantics changed after the last reviewed classification."
            entries[asset_id] = old
            continue
        domain_id, topic_id, confidence, explicit = infer_category(asset)
        if topic_id != "unclassified" and (explicit or clean_text(asset.get("asset_kind")) in GOVERNANCE_KIND_MAP):
            entries[asset_id] = make_entry(
                asset,
                taxonomy,
                domain_id=domain_id,
                topic_id=topic_id,
                analysis_type_id=infer_analysis_type(asset),
                confidence=confidence,
                source="deterministic",
                curation_state="current",
            )
        else:
            unresolved.add(asset_id)

    for _ in range(4):
        changed = False
        for asset_id in list(unresolved):
            asset = assets[asset_id]
            for parent_id in parents.get(asset_id, []):
                parent = entries.get(parent_id)
                if not parent or dict_value(parent.get("business_topic")).get("id") == "unclassified":
                    continue
                domain_id = clean_text(dict_value(parent.get("business_domain")).get("id"))
                topic_id = clean_text(dict_value(parent.get("business_topic")).get("id"))
                entries[asset_id] = make_entry(
                    asset,
                    taxonomy,
                    domain_id=domain_id,
                    topic_id=topic_id,
                    analysis_type_id=infer_analysis_type(asset),
                    confidence=max(0.5, float(parent.get("confidence") or 0.5) - 0.02),
                    source="inherited",
                    curation_state="current" if parent.get("curation_state") == "current" else "needs_semantic_review",
                    inherited_from=parent_id,
                )
                unresolved.remove(asset_id)
                changed = True
                break
        if not changed:
            break

    for asset_id in unresolved:
        asset = assets[asset_id]
        domain_id, topic_id, confidence, _ = infer_category(asset)
        entries[asset_id] = make_entry(
            asset,
            taxonomy,
            domain_id=domain_id,
            topic_id=topic_id,
            analysis_type_id=infer_analysis_type(asset),
            confidence=confidence,
            source="deterministic",
            curation_state="needs_semantic_review",
        )

    for asset_id, old in existing_entries.items():
        if asset_id in assets or not isinstance(old, dict):
            continue
        if excluded_legacy_entry(asset_id):
            continue
        retained = copy.deepcopy(old)
        retained["catalog_presence"] = False
        retained.setdefault("formal_asset_id", "")
        retained.setdefault("formal_member_id", "")
        retained["curation_state"] = "catalog_missing"
        entries[asset_id] = retained

    generated_at = now_iso()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_model": "catalog_only",
        "generated_at": generated_at,
        "catalog_path": catalog_relative_path(catalog_path),
        "catalog_fingerprint": catalog_fingerprint or file_sha256(catalog_path),
        "taxonomy": copy.deepcopy(taxonomy),
        "policy": {
            "all_catalog_assets_visible": True,
            "lifecycle_state_is_descriptive_only": True,
            "organization_mutates_source_assets": False,
            "formal_asset_identity_mutated": False,
            "semantic_review_scope": "new_or_changed_assets_only",
            "classifier_version": CLASSIFIER_VERSION,
        },
        "summary": {},
        "entries": dict(sorted(entries.items())),
    }
    payload["summary"] = build_summary(catalog, payload["entries"])
    return payload


def decision_entry(
    asset: dict[str, Any],
    decision: dict[str, Any],
    taxonomy: dict[str, Any],
) -> dict[str, Any]:
    domains, topics, analysis_types = taxonomy_index(taxonomy)
    domain_id = clean_text(decision.get("business_domain_id"))
    topic_id = clean_text(decision.get("business_topic_id"))
    analysis_type_id = clean_text(decision.get("analysis_type_id")) or infer_analysis_type(asset)
    if domain_id not in domains:
        raise ValueError(f"Unknown business_domain_id: {domain_id}")
    if topic_id not in topics or topics[topic_id][0] != domain_id:
        raise ValueError(f"Topic {topic_id!r} does not belong to domain {domain_id!r}")
    if analysis_type_id not in analysis_types:
        raise ValueError(f"Unknown analysis_type_id: {analysis_type_id}")
    source = clean_text(decision.get("classification_source"))
    if source not in {"llm", "human"}:
        raise ValueError("Decisions require classification_source llm or human.")
    entry = make_entry(
        asset,
        taxonomy,
        domain_id=domain_id,
        topic_id=topic_id,
        analysis_type_id=analysis_type_id,
        confidence=float(decision.get("confidence") or 0),
        source=source,
        curation_state="current",
        notes=clean_text(decision.get("notes")),
    )
    summary = clean_text(decision.get("semantic_summary"))
    if summary:
        entry["semantic_summary"] = summary
    entry["tags"] = sorted({clean_text(item) for item in list_value(decision.get("tags")) if clean_text(item)})
    return entry


def apply_decisions(
    payload: dict[str, Any],
    catalog: dict[str, Any],
    decisions: dict[str, Any],
) -> dict[str, Any]:
    if decisions.get("schema_version") != DECISIONS_SCHEMA_VERSION:
        raise ValueError(f"Decisions schema_version must be {DECISIONS_SCHEMA_VERSION}.")
    taxonomy = dict_value(payload.get("taxonomy"))
    assets = {
        clean_text(item.get("asset_id")): item
        for item in list_value(catalog.get("assets"))
        if isinstance(item, dict)
    }
    changed: list[str] = []
    for decision in list_value(decisions.get("decisions")):
        if not isinstance(decision, dict):
            raise ValueError("Each decision must be an object.")
        asset_id = clean_text(decision.get("asset_id"))
        if asset_id not in assets:
            raise ValueError(f"Decision references unknown catalog asset: {asset_id}")
        payload["entries"][asset_id] = decision_entry(assets[asset_id], decision, taxonomy)
        changed.append(asset_id)
    payload["generated_at"] = now_iso()
    payload["summary"] = build_summary(catalog, dict_value(payload.get("entries")))
    return {"payload": payload, "changed_asset_ids": sorted(set(changed))}


def scan_payload(
    catalog: dict[str, Any],
    organization: dict[str, Any],
    current_catalog_fingerprint: str = "",
    *,
    offset: int = 0,
    limit: int = MAX_SCAN_ITEMS,
) -> dict[str, Any]:
    entries = dict_value(organization.get("entries"))
    assets = {
        clean_text(item.get("asset_id")): item
        for item in list_value(catalog.get("assets"))
        if isinstance(item, dict)
    }
    candidates: list[dict[str, Any]] = []
    status_counts = Counter()
    for asset_id, asset in assets.items():
        entry = dict_value(entries.get(asset_id))
        if not entry:
            status = "new"
        elif clean_text(entry.get("asset_fingerprint")) != asset_fingerprint(asset):
            status = "changed"
        elif clean_text(entry.get("curation_state")) in {"needs_semantic_review", "stale_semantics"}:
            status = clean_text(entry.get("curation_state"))
        else:
            status = "current"
        status_counts[status] += 1
        if status != "current":
            facts = dict_value(asset.get("facts"))
            current = {
                "business_domain": dict_value(entry.get("business_domain")),
                "business_topic": dict_value(entry.get("business_topic")),
                "analysis_type": dict_value(entry.get("analysis_type")),
                "semantic_summary": clean_text(entry.get("semantic_summary")),
                "curation_state": clean_text(entry.get("curation_state")),
                "classification_source": clean_text(entry.get("classification_source")),
                "confidence": entry.get("confidence", 0),
            }
            candidates.append(
                {
                    "asset_id": asset_id,
                    "change_status": status,
                    "asset_kind": clean_text(asset.get("asset_kind")),
                    "project_id": clean_text(asset.get("project_id")),
                    "title": clean_text(asset.get("title")),
                    "summary": clean_text(asset.get("summary")),
                    "business_category": clean_text(facts.get("business_category")),
                    "business_topic": clean_text(facts.get("business_topic")),
                    "metrics": compact_evidence(facts.get("metrics")),
                    "dimensions": compact_evidence(facts.get("dimensions")),
                    "source_logs": compact_evidence(facts.get("source_logs")),
                    "current_organization": current,
                }
            )
    all_missing = sorted(asset_id for asset_id in entries if asset_id not in assets)
    ordered_candidates = sorted(candidates, key=lambda item: (item["project_id"], item["asset_kind"], item["asset_id"]))
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 500))
    candidate_page = ordered_candidates[offset : offset + limit]
    missing_page = all_missing[:MAX_SCAN_ITEMS]
    status_counts["catalog_missing"] = len(all_missing)
    snapshot_changed = bool(
        current_catalog_fingerprint
        and clean_text(organization.get("catalog_fingerprint")) != current_catalog_fingerprint
    )
    return {
        "schema_version": "sql_asset_organization_scan_v1",
        "status": "warn" if ordered_candidates or all_missing or snapshot_changed else "pass",
        "catalog_snapshot_changed": snapshot_changed,
        "summary": dict(sorted(status_counts.items())),
        "candidate_count": len(ordered_candidates),
        "candidate_offset": offset,
        "candidate_limit": limit,
        "candidates_truncated": offset + len(candidate_page) < len(ordered_candidates),
        "candidates": candidate_page,
        "catalog_missing_asset_count": len(all_missing),
        "catalog_missing_asset_ids_truncated": len(all_missing) > len(missing_page),
        "catalog_missing_asset_ids": missing_page,
    }


def compact_evidence(value: Any, limit: int = 12) -> list[str]:
    compact: list[str] = []
    for item in list_value(value):
        if isinstance(item, dict):
            name = clean_text(item.get("name") or item.get("field") or item.get("label"))
            meaning = clean_text(item.get("business_meaning") or item.get("definition"))
            text = f"{name}: {meaning}" if name and meaning and meaning != name else (name or meaning)
        else:
            text = clean_text(item)
        if text and text not in compact:
            compact.append(text)
        if len(compact) >= limit:
            break
    return compact


def validate_organization(payload: dict[str, Any], catalog: dict[str, Any] | None = None) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    if payload.get("source_model") != "catalog_only":
        problems.append("source_model must be catalog_only")
    taxonomy = dict_value(payload.get("taxonomy"))
    problems.extend(validate_taxonomy(taxonomy))
    domains, topics, analysis_types = taxonomy_index(taxonomy)
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return problems + ["entries must be an object"]
    for asset_id, entry in entries.items():
        if not isinstance(entry, dict):
            problems.append(f"entry must be an object: {asset_id}")
            continue
        domain_id = clean_text(dict_value(entry.get("business_domain")).get("id"))
        topic_id = clean_text(dict_value(entry.get("business_topic")).get("id"))
        analysis_type_id = clean_text(dict_value(entry.get("analysis_type")).get("id"))
        if domain_id not in domains:
            problems.append(f"unknown domain for {asset_id}: {domain_id}")
        if topic_id not in topics or topics.get(topic_id, ("", {}))[0] != domain_id:
            problems.append(f"invalid topic for {asset_id}: {topic_id}")
        if analysis_type_id not in analysis_types:
            problems.append(f"invalid analysis type for {asset_id}: {analysis_type_id}")
        if clean_text(entry.get("curation_state")) not in CURATION_STATES:
            problems.append(f"invalid curation state for {asset_id}")
        if clean_text(entry.get("classification_source")) not in CLASSIFICATION_SOURCES:
            problems.append(f"invalid classification source for {asset_id}")
    if catalog is not None:
        if catalog.get("schema_version") not in SUPPORTED_CATALOG_SCHEMAS:
            problems.append("catalog uses an unsupported schema_version")
        catalog_ids = {
            clean_text(item.get("asset_id"))
            for item in list_value(catalog.get("assets"))
            if isinstance(item, dict)
        }
        absent = sorted(catalog_ids - set(entries))
        if absent:
            problems.append(f"organization is missing {len(absent)} catalog assets")
        catalog_by_id = {
            clean_text(item.get("asset_id")): item
            for item in list_value(catalog.get("assets"))
            if isinstance(item, dict)
        }
        for asset_id in set(entries) & set(catalog_by_id):
            entry = dict_value(entries.get(asset_id))
            asset = catalog_by_id[asset_id]
            if clean_text(entry.get("formal_asset_id")) != clean_text(asset.get("formal_asset_id")):
                problems.append(f"organization changed formal_asset_id for {asset_id}")
            if clean_text(entry.get("formal_member_id")) != clean_text(asset.get("formal_member_id")):
                problems.append(f"organization changed formal_member_id for {asset_id}")
    return problems


def default_organization_path(catalog_path: Path) -> Path:
    return catalog_path.parent / DEFAULT_OUTPUT_NAME


def render_summary(result: dict[str, Any]) -> str:
    lines = [f"status={result.get('status', 'unknown')}"]
    for key in ("catalog_path", "organization_path"):
        if result.get(key):
            lines.append(f"{key}={result[key]}")
    summary = dict_value(result.get("summary"))
    for key, value in summary.items():
        if not isinstance(value, dict):
            lines.append(f"{key}={value}")
    if result.get("changed_asset_ids"):
        lines.append(f"changed_asset_count={len(result['changed_asset_ids'])}")
    for problem in list_value(result.get("problems")):
        lines.append(f"- {problem}")
    return "\n".join(lines) + "\n"


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--organization", default="")
    parser.add_argument("--taxonomy-file", default="")
    parser.add_argument("--format", choices=["json", "summary"], default="summary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Report new, changed, stale, and unclassified assets")
    add_common_arguments(scan)
    scan.add_argument("--offset", type=int, default=0)
    scan.add_argument("--limit", type=int, default=MAX_SCAN_ITEMS)

    refresh = sub.add_parser("refresh", help="Refresh deterministic and inherited organization entries")
    add_common_arguments(refresh)
    refresh.add_argument("--output", default="")
    add_function_gate_arguments(refresh, selection_help="Use [ASSET_ORGANIZATION] for semantic asset organization.")

    apply = sub.add_parser("apply", help="Apply reviewed LLM or human semantic decisions")
    add_common_arguments(apply)
    apply.add_argument("--decisions-file", required=True)
    apply.add_argument("--output", default="")
    add_function_gate_arguments(apply, selection_help="Use [ASSET_ORGANIZATION] for semantic asset organization.")

    validate = sub.add_parser("validate", help="Validate an organization overlay")
    validate.add_argument("--organization", required=True)
    validate.add_argument("--catalog", default="")
    validate.add_argument("--format", choices=["json", "summary"], default="summary")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "validate":
            organization_path = Path(args.organization).resolve()
            organization = read_json(organization_path, {})
            catalog_path = Path(args.catalog).resolve() if args.catalog else None
            catalog = read_json(catalog_path, {}) if catalog_path else None
            problems = validate_organization(organization, catalog)
            if catalog_path and clean_text(organization.get("catalog_fingerprint")) != file_sha256(catalog_path):
                problems.append("organization catalog_fingerprint does not match the current catalog file")
            result = {
                "status": "fail" if problems else "pass",
                "organization_path": organization_path.as_posix(),
                "problems": problems,
                "summary": dict_value(organization.get("summary")),
            }
        else:
            catalog_path = Path(args.catalog).resolve()
            catalog = read_json(catalog_path, {})
            if catalog.get("schema_version") not in SUPPORTED_CATALOG_SCHEMAS:
                raise ValueError("Catalog uses an unsupported schema_version.")
            organization_path = Path(args.organization).resolve() if args.organization else default_organization_path(catalog_path)
            existing = read_json(organization_path, {})
            taxonomy_path = Path(args.taxonomy_file).resolve() if args.taxonomy_file else DEFAULT_TAXONOMY
            taxonomy = read_json(taxonomy_path, {})
            taxonomy_problems = validate_taxonomy(taxonomy)
            if taxonomy_problems:
                raise ValueError("; ".join(taxonomy_problems))
            if args.command == "scan":
                result = scan_payload(
                    catalog,
                    existing,
                    file_sha256(catalog_path),
                    offset=args.offset,
                    limit=args.limit,
                )
                result["catalog_path"] = catalog_path.as_posix()
                result["organization_path"] = organization_path.as_posix()
            else:
                require_user_function_selection(
                    args.function_selection,
                    user_request=args.user_request,
                    allowed_ids=command_function_ids("asset_organization.py", args.command),
                    purpose="cross-project asset semantic organization",
                )
                require_user_request(args.user_request, purpose="cross-project asset semantic organization")
                payload = build_payload(catalog, catalog_path, taxonomy, existing)
                changed: list[str] = []
                if args.command == "apply":
                    decisions = read_json(Path(args.decisions_file).resolve(), {})
                    applied = apply_decisions(payload, catalog, decisions)
                    payload = applied["payload"]
                    changed = applied["changed_asset_ids"]
                output = Path(args.output).resolve() if args.output else organization_path
                write_json(output, payload)
                problems = validate_organization(payload, catalog)
                result = {
                    "status": "fail" if problems else ("warn" if payload["summary"]["needs_semantic_review_count"] else "pass"),
                    "catalog_path": catalog_path.as_posix(),
                    "organization_path": output.as_posix(),
                    "summary": payload["summary"],
                    "changed_asset_ids": changed,
                    "problems": problems,
                }
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "fail", "problems": [str(exc)]}

    print(json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else render_summary(result), end="")
    return 1 if result.get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
