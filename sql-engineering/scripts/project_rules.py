"""Project rule access facade backed only by Canonical Rule Store v2."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from rule_store import RuleStore, RuleStoreError


CONFIG_OWNERSHIP_POINTERS = {
    "event-time-field-dteventtime": ("partition_policy",),
    "izoneareaid-default": ("business_scope", "default_zone"),
    "project-launch-date": ("default_query_window", "project_start_date"),
    "server-zone-identifier-field": ("business_scope", "zone_identifier"),
}


def has_v2_store(project_root: Path | str) -> bool:
    return RuleStore(project_root).exists


def require_rule_store(project_root: Path | str) -> RuleStore:
    store = RuleStore(project_root)
    if not store.exists:
        raise RuleStoreError(
            f"Canonical Rule Store v2 is required: {store.store_path}. "
            "Legacy canonical_rules.json is migration input only."
        )
    return store


def rules_fingerprint(project_root: Path | str) -> str:
    return require_rule_store(project_root).load_index()["store_sha256"]


def load_rules(
    project_root: Path | str,
    *,
    status: str = "confirmed",
    include_history: bool = False,
) -> list[dict[str, Any]]:
    store = require_rule_store(project_root)
    if status == "all":
        if not include_history:
            return store.load_current() + store.load_proposed()
        return store.load_all_versions()
    if status in {"superseded", "deprecated"} and not include_history:
        return []
    return store.load_by_status(status)


def select_rule_records(
    project_root: Path | str,
    evidence: dict[str, Any],
    *,
    query_text: str,
    concept_keys: Iterable[str] | None = None,
    statuses: Iterable[str] = ("confirmed",),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return compact candidates and their bodies; v2 never loads unselected bodies."""

    store = require_rule_store(project_root)
    candidates = store.select_candidates(
        evidence,
        query_text=query_text,
        concept_keys=concept_keys,
        statuses=statuses,
    )
    return candidates, store.load_candidate_records(candidates)


def dictionary_snapshot(project_root: Path | str, *, include_history: bool = False) -> dict[str, Any]:
    return require_rule_store(project_root).build_dictionary_snapshot(include_history=include_history)


def _optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def _nested_value(payload: dict[str, Any], pointer: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in pointer:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def config_owned_rule_markers(project_root: Path | str) -> dict[str, dict[str, Any]]:
    """Return governed project-config ownership markers, never historical rule bodies."""

    root = Path(project_root).resolve()
    config = _optional_json(root / "project_config.json")
    receipt = _optional_json(root / "rules" / "governance" / "activation-v2-upgrade.json")
    markers: dict[str, dict[str, Any]] = {}
    for action in receipt.get("actions", []) or []:
        if not isinstance(action, dict) or action.get("action") != "retire_to_config":
            continue
        concept_key = str(action.get("concept_key") or "").strip()
        if not concept_key:
            continue
        pointer = CONFIG_OWNERSHIP_POINTERS.get(concept_key, ())
        value = _nested_value(config, pointer) if pointer else None
        pointer_text = "/" + "/".join(pointer) if pointer else ""
        value_text = (
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            if value is not None
            else "未提取到结构化值"
        )
        markers[concept_key] = {
            "record_kind": "project_config",
            "rule_id": f"config:{concept_key}",
            "concept_key": concept_key,
            "version": action.get("new_rule_version", ""),
            "status": "config_owned",
            "title": "已迁出业务口径，由项目配置接管",
            "content": f"当前配置：{value_text}",
            "source": "project_config.json",
            "source_evidence": (
                f"project_config.json#{pointer_text}; "
                "rules/governance/activation-v2-upgrade.json"
            ),
            "confirmed_by_user": True,
            "scope": "project",
            "lifetime": "persistent",
            "applies_to": "项目执行配置与 SQL 生成",
            "affected_artifacts": [],
            "decision_question": "",
            "created_at": "",
            "updated_at": str(config.get("updated_at") or receipt.get("created_at") or ""),
            "notes": "旧规则保留为不可变历史；当前值不再参与业务口径激活。",
            "config_path": "project_config.json",
            "config_pointer": pointer_text,
            "config_value": value,
            "migration_action": action,
        }
    return markers
