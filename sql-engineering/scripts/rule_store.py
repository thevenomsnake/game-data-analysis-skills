#!/usr/bin/env python3
"""Canonical Rule Store v2 storage, indexing, retrieval, and validation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


STORE_SCHEMA_VERSION = "canonical_rule_store_v2"
INDEX_SCHEMA_VERSION = "canonical_rule_activation_index_v2"
VERSION_SCHEMA_VERSION = "canonical_rule_version_v2"
DICTIONARY_SNAPSHOT_VERSION = "canonical_rule_dictionary_snapshot_v3"
ACTIVATION_CONTRACT_VERSION = "canonical_rule_activation_v2"

KNOWLEDGE_PIN_FIELDS = {"dataset_version", "content_hash", "projection_sha256"}

FORWARD_ACTIVATION_POLICIES = {"automatic", "explicit_only", "disabled"}
REVERSE_ACTIVATION_POLICIES = {"exact_only", "diagnostic_only", "disabled"}

STORE_RELATIVE_PATH = Path("rules/store.json")
INDEX_RELATIVE_PATH = Path("rules/activation-index.json")
DEFINITIONS_RELATIVE_PATH = Path("rules/definitions")
LEGACY_RELATIVE_PATH = Path("rules/canonical_rules.json")

CONCEPT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SELECTOR_LIST_FIELDS = (
    "domains",
    "source_logs",
    "source_fields",
    "metric_families",
    "grain",
    "excludes_when",
)


class RuleStoreError(RuntimeError):
    """Raised when a rule store is missing, corrupt, stale, or unsafe."""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def logical_knowledge_dependency(dependency: dict[str, Any]) -> dict[str, Any]:
    """Project one immutable dependency pin into the current binding contract."""

    logical = {
        key: copy.deepcopy(value)
        for key, value in dependency.items()
        if key not in KNOWLEDGE_PIN_FIELDS and key != "binding_policy"
    }
    logical["binding_policy"] = "active_project_binding"
    return logical


def _semantic_knowledge_dependency(dependency: dict[str, Any]) -> dict[str, Any]:
    logical = logical_knowledge_dependency(dependency)
    logical.pop("binding_policy", None)
    if isinstance(logical.get("fields"), list):
        logical["fields"] = sorted({str(value) for value in logical["fields"]})
    return logical


def _semantic_activation_contract(contract: Any) -> Any:
    if not isinstance(contract, dict):
        return copy.deepcopy(contract)
    normalized = copy.deepcopy(contract)
    constraints = []
    for raw in normalized.get("hard_constraints", []) or []:
        if not isinstance(raw, dict) or raw.get("type") != "must_use_knowledge_dependency":
            constraints.append(raw)
            continue
        constraint = {
            key: copy.deepcopy(value)
            for key, value in raw.items()
            if key not in KNOWLEDGE_PIN_FIELDS and key != "binding_policy"
        }
        if isinstance(constraint.get("fields"), list):
            constraint["fields"] = sorted({str(value) for value in constraint["fields"]})
        constraints.append(constraint)
    if "hard_constraints" in normalized:
        normalized["hard_constraints"] = constraints
    return normalized


def rule_semantic_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Return only business meaning and activation behavior for version identity."""

    structured = copy.deepcopy(
        record.get("structured_definition")
        if isinstance(record.get("structured_definition"), dict)
        else {}
    )
    if "knowledge_dependencies" in structured:
        structured["knowledge_dependencies"] = [
            _semantic_knowledge_dependency(item)
            for item in structured.get("knowledge_dependencies", []) or []
            if isinstance(item, dict)
        ]
    return {
        "concept_key": str(record.get("concept_key") or ""),
        "title": str(record.get("title") or ""),
        "content": str(record.get("content") or ""),
        "scope": str(record.get("scope") or ""),
        "lifetime": str(record.get("lifetime") or ""),
        "applies_to": str(record.get("applies_to") or ""),
        "decision_question": str(record.get("decision_question") or ""),
        "activation_contract": _semantic_activation_contract(record.get("activation_contract")),
        "structured_definition": structured,
    }


def rule_semantic_fingerprint(record: dict[str, Any]) -> str:
    return object_sha256(rule_semantic_payload(record))


def runtime_rule_view(record: dict[str, Any]) -> dict[str, Any]:
    """Resolve current rules by logical Knowledge dependency without rewriting history."""

    result = copy.deepcopy(record)
    legacy_pins: list[dict[str, Any]] = []
    structured = result.get("structured_definition")
    if isinstance(structured, dict) and isinstance(structured.get("knowledge_dependencies"), list):
        dependencies = []
        for dependency in structured.get("knowledge_dependencies", []) or []:
            if not isinstance(dependency, dict):
                continue
            pins = {
                key: copy.deepcopy(dependency.get(key))
                for key in KNOWLEDGE_PIN_FIELDS
                if dependency.get(key) not in {None, ""}
            }
            if pins:
                legacy_pins.append(
                    {
                        "dataset_id": str(dependency.get("dataset_id") or ""),
                        "projection_id": str(dependency.get("projection_id") or ""),
                        **pins,
                    }
                )
            dependencies.append(logical_knowledge_dependency(dependency))
        structured["knowledge_dependencies"] = dependencies

    contract = result.get("activation_contract")
    if isinstance(contract, dict) and isinstance(contract.get("hard_constraints"), list):
        contract["hard_constraints"] = [
            logical_knowledge_dependency(item)
            if isinstance(item, dict) and item.get("type") == "must_use_knowledge_dependency"
            else item
            for item in contract.get("hard_constraints", []) or []
        ]
    if legacy_pins:
        result.setdefault("_rule_store", {})["legacy_knowledge_pins"] = legacy_pins
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise RuleStoreError(f"Missing rule-store file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuleStoreError(f"Invalid JSON in rule-store file {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def normalize_signal(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _normalized_signal_offsets(value: Any) -> tuple[str, list[int]]:
    text = str(value or "")
    normalized: list[str] = []
    offsets: list[int] = []
    for offset, char in enumerate(text):
        lowered = char.lower()
        if re.fullmatch(r"[a-z0-9\u4e00-\u9fff]", lowered):
            normalized.append(lowered)
            offsets.append(offset)
    return "".join(normalized), offsets


def request_signal_evidence(query_text: str, signal: str) -> dict[str, Any] | None:
    normalized_query, offsets = _normalized_signal_offsets(query_text)
    normalized_signal = normalize_signal(signal)
    if not normalized_query or not normalized_signal:
        return None
    start = normalized_query.find(normalized_signal)
    if start < 0:
        return None
    end_index = start + len(normalized_signal) - 1
    if end_index >= len(offsets):
        return None
    source_start = offsets[start]
    source_end = offsets[end_index] + 1
    return {
        "signal": str(signal),
        "quote": str(query_text)[source_start:source_end],
        "start": source_start,
        "end": source_end,
    }


def unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = normalize_signal(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _listify(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def activation_contract_source(rule: dict[str, Any]) -> str:
    contract = rule.get("activation_contract")
    if not isinstance(contract, dict) or not contract:
        return "missing"
    if contract.get("contract_version") == ACTIVATION_CONTRACT_VERSION:
        return "stored_v2"
    return "legacy"


def activation_policy(contract: dict[str, Any] | None) -> dict[str, str]:
    value = (contract or {}).get("activation_policy")
    value = value if isinstance(value, dict) else {}
    forward = str(value.get("forward") or "explicit_only")
    reverse = str(value.get("reverse") or "disabled")
    if forward not in FORWARD_ACTIVATION_POLICIES:
        forward = "explicit_only"
    if reverse not in REVERSE_ACTIVATION_POLICIES:
        reverse = "disabled"
    return {"forward": forward, "reverse": reverse}


def normalized_request_signatures(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in (contract or {}).get("request_signatures", []) or []:
        if not isinstance(raw, dict):
            continue
        row = {
            "label": str(raw.get("label") or ""),
            "any_of": unique_strings(raw.get("any_of", []) or []),
            "all_of": unique_strings(raw.get("all_of", []) or []),
            "none_of": unique_strings(raw.get("none_of", []) or []),
        }
        if row["any_of"] or row["all_of"]:
            rows.append(row)
    return rows


def request_signature_matches(
    signatures: Iterable[dict[str, Any]],
    query_text: str,
) -> list[dict[str, Any]]:
    normalized_query = normalize_signal(query_text)
    if not normalized_query:
        return []
    matches: list[dict[str, Any]] = []
    for signature in signatures:
        any_of = unique_strings(signature.get("any_of", []) or [])
        all_of = unique_strings(signature.get("all_of", []) or [])
        none_of = unique_strings(signature.get("none_of", []) or [])
        if any(normalize_signal(value) in normalized_query for value in none_of):
            continue
        matched_any = [
            evidence
            for value in any_of
            if (evidence := request_signal_evidence(query_text, value)) is not None
        ]
        matched_all = [
            evidence
            for value in all_of
            if (evidence := request_signal_evidence(query_text, value)) is not None
        ]
        any_ok = not any_of or bool(matched_any)
        all_ok = len(matched_all) == len(all_of)
        if any_ok and all_ok and (any_of or all_of):
            row = copy.deepcopy(signature)
            row["evidence_quotes"] = matched_any + matched_all
            matches.append(row)
    return matches


def _signature_values(signature: dict[str, Any], singular: str, plural: str) -> list[str]:
    return unique_strings(_listify(signature.get(plural)) + _listify(signature.get(singular)))


def activation_contract_problems(rule: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    contract = rule.get("activation_contract")
    if activation_contract_source(rule) != "stored_v2":
        return ["current rule is missing canonical_rule_activation_v2"]
    policy = activation_policy(contract)
    raw_policy = contract.get("activation_policy") or {}
    if raw_policy.get("forward") not in FORWARD_ACTIVATION_POLICIES:
        problems.append("activation_policy.forward is invalid")
    if raw_policy.get("reverse") not in REVERSE_ACTIVATION_POLICIES:
        problems.append("activation_policy.reverse is invalid")
    if contract.get("application_class") not in {None, "intent_required", "explicit_only", "audit_only"}:
        problems.append("application_class is invalid")
    if contract.get("unrequested_sql_policy") not in {None, "diagnostic", "block"}:
        problems.append("unrequested_sql_policy is invalid")
    signatures = normalized_request_signatures(contract)
    if policy["forward"] == "automatic" and not signatures:
        problems.append("automatic forward activation requires request_signatures")
    if policy["reverse"] != "disabled":
        event_signature = contract.get("event_signature") or {}
        source_signature = contract.get("source_signature") or {}
        has_reverse_core = bool(
            event_signature
            or source_signature.get("source_logs")
            or source_signature.get("source_fields")
        )
        if not has_reverse_core:
            problems.append("enabled reverse activation requires event_signature or source_signature")
    return problems


def build_activation_selector(rule: dict[str, Any]) -> dict[str, Any]:
    """Build an index selector only from an explicit activation contract."""

    contract = rule.get("activation_contract")
    contract = copy.deepcopy(contract) if isinstance(contract, dict) else {}
    source = activation_contract_source(rule)
    policy = activation_policy(contract if source == "stored_v2" else None)
    required_logs: list[str] = []
    for constraint in contract.get("hard_constraints", []) or []:
        if not isinstance(constraint, dict):
            continue
        if constraint.get("type") in {"must_use_log", "do_not_substitute_log"}:
            required_logs.append(constraint.get("log") or constraint.get("expected_log"))
        if constraint.get("type") == "must_use_battlesrvid_join_for_mode_attribution":
            required_logs.append(constraint.get("join_log"))

    event_signature = contract.get("event_signature") or {}
    source_signature = contract.get("source_signature") or {}
    reverse_logs = _signature_values(event_signature, "required_log", "required_logs")
    reverse_logs.extend(source_signature.get("source_logs", []) or [])
    reverse_fields = source_signature.get("source_fields", []) or []

    selector = {
        "activation_contract_source": source,
        "activation_policy": policy,
        "request_signatures": normalized_request_signatures(contract) if source == "stored_v2" else [],
        "required_logs": unique_strings(required_logs),
        "event_signature": copy.deepcopy(event_signature) if policy["reverse"] != "disabled" else {},
        "reverse_selector": {
            "required_logs": unique_strings(reverse_logs) if policy["reverse"] != "disabled" else [],
            "required_fields": unique_strings(reverse_fields) if policy["reverse"] != "disabled" else [],
        },
    }
    for field in SELECTOR_LIST_FIELDS:
        selector[field] = (
            unique_strings(contract.get(field, []) or [])
            if source == "stored_v2" and policy["forward"] == "automatic"
            else []
        )
    return selector


def compact_reference(reference: dict[str, Any]) -> dict[str, Any]:
    return {
        "store_version": int(reference.get("store_version") or 0),
        "rule_id": str(reference.get("rule_id") or ""),
        "rule_version": int(reference.get("rule_version") or 0),
        "effective_status": str(reference.get("effective_status") or ""),
        "path": str(reference.get("path") or ""),
        "record_sha256": str(reference.get("record_sha256") or ""),
        "file_sha256": str(reference.get("file_sha256") or ""),
    }


class RuleStore:
    """Single authority for Canonical Rule Store v2 access."""

    def __init__(self, project_root: Path | str):
        self.root = Path(project_root).resolve()
        self.store_path = self.root / STORE_RELATIVE_PATH
        self.index_path = self.root / INDEX_RELATIVE_PATH
        self.definitions_root = self.root / DEFINITIONS_RELATIVE_PATH
        self._store: dict[str, Any] | None = None
        self._index: dict[str, Any] | None = None
        self._record_cache: dict[str, dict[str, Any]] = {}

    @property
    def exists(self) -> bool:
        return self.store_path.exists() and self.index_path.exists()

    def _safe_project_path(self, relative: str) -> Path:
        candidate = (self.root / Path(relative)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise RuleStoreError(f"Rule-store path escapes the project root: {relative}") from exc
        return candidate

    def load_store(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._store is None:
            value = read_json(self.store_path)
            if value.get("schema_version") != STORE_SCHEMA_VERSION:
                raise RuleStoreError(
                    f"Unsupported rule-store schema in {self.store_path}: "
                    f"{value.get('schema_version')!r}"
                )
            self._store = value
        return self._store

    def load_index(self, *, refresh: bool = False, verify_store: bool = True) -> dict[str, Any]:
        if refresh or self._index is None:
            value = read_json(self.index_path)
            if value.get("schema_version") != INDEX_SCHEMA_VERSION:
                raise RuleStoreError(
                    f"Unsupported activation-index schema in {self.index_path}: "
                    f"{value.get('schema_version')!r}"
                )
            self._index = value
        if verify_store:
            expected = object_sha256(self.load_store())
            actual = str(self._index.get("store_sha256") or "")
            if actual != expected:
                raise RuleStoreError(
                    "Activation index is stale or belongs to another store: "
                    f"expected {expected}, found {actual or 'missing'}"
                )
        return self._index

    def concept(self, concept_key: str) -> dict[str, Any]:
        return copy.deepcopy((self.load_store().get("concepts") or {}).get(concept_key) or {})

    def _load_reference(self, reference: dict[str, Any], *, effective_status: str | None = None) -> dict[str, Any]:
        relative = str(reference.get("path") or "")
        if not relative:
            raise RuleStoreError("Rule version reference is missing path.")
        expected_prefix = DEFINITIONS_RELATIVE_PATH.as_posix() + "/"
        if not relative.startswith(expected_prefix):
            raise RuleStoreError(f"Rule version is outside definitions/: {relative}")
        if relative not in self._record_cache:
            path = self._safe_project_path(relative)
            document = read_json(path)
            if document.get("schema_version") != VERSION_SCHEMA_VERSION:
                raise RuleStoreError(f"Unsupported rule version schema: {relative}")
            record = document.get("record")
            if not isinstance(record, dict):
                raise RuleStoreError(f"Rule version has no record object: {relative}")
            expected_record_hash = str(reference.get("record_sha256") or "")
            actual_record_hash = object_sha256(record)
            if expected_record_hash != actual_record_hash:
                raise RuleStoreError(
                    f"Rule record hash mismatch for {relative}: "
                    f"expected {expected_record_hash}, found {actual_record_hash}"
                )
            expected_file_hash = str(reference.get("file_sha256") or "")
            actual_file_hash = file_sha256(path)
            if expected_file_hash != actual_file_hash:
                raise RuleStoreError(
                    f"Rule file hash mismatch for {relative}: "
                    f"expected {expected_file_hash}, found {actual_file_hash}"
                )
            self._record_cache[relative] = copy.deepcopy(record)
        result = copy.deepcopy(self._record_cache[relative])
        status = effective_status or str(reference.get("effective_status") or "")
        if status:
            result["status"] = status
        result["_rule_store"] = {
            "store_version": int(reference.get("store_version") or 0),
            "path": relative,
            "record_sha256": str(reference.get("record_sha256") or ""),
        }
        return result

    def load_current(self, concept_keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
        concepts = self.load_store().get("concepts") or {}
        requested = set(concept_keys or concepts.keys())
        rows: list[dict[str, Any]] = []
        for concept_key in sorted(requested):
            concept = concepts.get(concept_key) or {}
            reference = concept.get("current_confirmed")
            if isinstance(reference, dict) and reference:
                rows.append(runtime_rule_view(self._load_reference(reference, effective_status="confirmed")))
        return rows

    def load_proposed(self, concept_keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
        concepts = self.load_store().get("concepts") or {}
        requested = set(concept_keys or concepts.keys())
        rows: list[dict[str, Any]] = []
        for concept_key in sorted(requested):
            concept = concepts.get(concept_key) or {}
            for reference in concept.get("proposed_versions", []) or []:
                rows.append(runtime_rule_view(self._load_reference(reference, effective_status="proposed")))
        return rows

    def load_versions(self, concept_key: str) -> list[dict[str, Any]]:
        concept = self.concept(concept_key)
        if not concept:
            return []
        return [
            self._load_reference(reference, effective_status=str(reference.get("effective_status") or ""))
            for reference in concept.get("versions", []) or []
        ]

    def load_by_status(self, status: str) -> list[dict[str, Any]]:
        if status == "confirmed":
            return self.load_current()
        if status == "proposed":
            return self.load_proposed()
        rows: list[dict[str, Any]] = []
        for concept_key in sorted((self.load_store().get("concepts") or {}).keys()):
            rows.extend(
                rule
                for rule in self.load_versions(concept_key)
                if rule.get("status") == status
            )
        return rows

    def load_all_versions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for concept_key in sorted((self.load_store().get("concepts") or {}).keys()):
            rows.extend(self.load_versions(concept_key))
        return rows

    def select_candidates(
        self,
        evidence: dict[str, Any] | None = None,
        *,
        query_text: str = "",
        concept_keys: Iterable[str] | None = None,
        statuses: Iterable[str] = ("confirmed",),
    ) -> list[dict[str, Any]]:
        """Select compact index entries; this method never reads rule bodies."""

        frame = evidence or {}
        requested = {str(value).strip() for value in concept_keys or [] if str(value).strip()}
        allowed_statuses = {str(value) for value in statuses}
        candidate_observed = frame.get("candidate_sql_observed") or {}
        candidate_logs = {
            normalize_signal(value)
            for value in candidate_observed.get("source_logs", []) or []
            if normalize_signal(value)
        }
        candidate_fields = {
            normalize_signal(value)
            for value in candidate_observed.get("source_fields", []) or []
            if normalize_signal(value)
        }
        rows: list[tuple[int, dict[str, Any]]] = []
        for entry in self.load_index().get("entries", []) or []:
            if entry.get("status") not in allowed_statuses:
                continue
            concept_key = str(entry.get("concept_key") or "")
            selector_hint = concept_key in requested or str(entry.get("rule_id") or "") in requested
            selector = entry.get("selector") or {}
            policy = selector.get("activation_policy") or {}
            reasons: list[str] = []
            score = 0
            if selector_hint:
                score = 10000
                reasons.append("selector_hint")
            forward_matches = []
            if policy.get("forward") == "automatic":
                forward_matches = request_signature_matches(
                    selector.get("request_signatures", []) or [],
                    query_text,
                )
            if forward_matches:
                score = max(score, 100 + len(forward_matches))
                reasons.append("forward_request_signature")

            reverse_selector = selector.get("reverse_selector") or {}
            reverse_logs = {
                normalize_signal(value)
                for value in reverse_selector.get("required_logs", []) or []
                if normalize_signal(value)
            }
            reverse_fields = {
                normalize_signal(value)
                for value in reverse_selector.get("required_fields", []) or []
                if normalize_signal(value)
            }
            reverse_match = bool(
                policy.get("reverse") != "disabled"
                and (
                    (reverse_logs and reverse_logs & candidate_logs)
                    or (not reverse_logs and reverse_fields and reverse_fields & candidate_fields)
                )
            )
            if reverse_match:
                score = max(score, 10)
                reasons.append("reverse_sql_structure")
            if score <= 0:
                continue
            selected = copy.deepcopy(entry)
            selected["selection_score"] = score
            selected["selection_reasons"] = reasons
            if forward_matches:
                selected["matched_request_signatures"] = forward_matches
            rows.append((score, selected))
        rows.sort(
            key=lambda item: (
                -item[0],
                str(item[1].get("concept_key") or ""),
                str(item[1].get("rule_id") or ""),
            )
        )
        return [entry for _, entry in rows]

    def load_candidate_records(self, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            relative = str(entry.get("path") or "")
            if not relative or relative in seen:
                continue
            seen.add(relative)
            rows.append(
                runtime_rule_view(
                    self._load_reference(entry, effective_status=str(entry.get("status") or ""))
                )
            )
        return rows

    def _build_index_document(
        self,
        store: dict[str, Any],
        selector_builder: Callable[[dict[str, Any]], dict[str, Any]] = build_activation_selector,
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for concept_key, concept in sorted((store.get("concepts") or {}).items()):
            references: list[dict[str, Any]] = []
            current = concept.get("current_confirmed")
            if isinstance(current, dict) and current:
                references.append(current)
            references.extend(concept.get("proposed_versions", []) or [])
            for reference in references:
                record = self._load_reference(reference, effective_status=str(reference.get("effective_status") or ""))
                entries.append(
                    {
                        **compact_reference(reference),
                        "concept_key": concept_key,
                        "status": str(reference.get("effective_status") or ""),
                        "selector": selector_builder(record),
                    }
                )
        return {
            "schema_version": INDEX_SCHEMA_VERSION,
            "project_id": str(store.get("project_id") or self.root.name),
            "store_sha256": object_sha256(store),
            "updated_at": str(store.get("updated_at") or ""),
            "entries": entries,
        }

    def rebuild_activation_index(
        self,
        selector_builder: Callable[[dict[str, Any]], dict[str, Any]] = build_activation_selector,
    ) -> dict[str, Any]:
        store = self.load_store(refresh=True)
        document = self._build_index_document(store, selector_builder)
        atomic_write_json(self.index_path, document)
        self._index = document
        return copy.deepcopy(document)

    def write_new_version(
        self,
        record: dict[str, Any],
        *,
        selector_builder: Callable[[dict[str, Any]], dict[str, Any]] = build_activation_selector,
    ) -> dict[str, Any]:
        """Append one immutable version and move lifecycle pointers without rewriting history."""

        concept_key = str(record.get("concept_key") or "").strip()
        if not CONCEPT_KEY_RE.fullmatch(concept_key):
            raise RuleStoreError(f"Invalid or missing concept_key: {concept_key!r}")
        status = str(record.get("status") or "")
        if status not in {"confirmed", "proposed", "deprecated"}:
            raise RuleStoreError(f"New rule version requires confirmed/proposed/deprecated status, found {status!r}")
        store = copy.deepcopy(self.load_store(refresh=True))
        concepts = store.setdefault("concepts", {})
        concept = copy.deepcopy(
            concepts.get(concept_key)
            or {
                "concept_key": concept_key,
                "latest_store_version": 0,
                "latest_rule_version": 0,
                "current_confirmed": None,
                "proposed_versions": [],
                "deprecated_versions": [],
                "versions": [],
            }
        )
        if status == "confirmed":
            current_reference = concept.get("current_confirmed")
            if isinstance(current_reference, dict) and current_reference:
                current_record = self._load_reference(
                    current_reference,
                    effective_status="confirmed",
                )
                if rule_semantic_fingerprint(current_record) == rule_semantic_fingerprint(record):
                    raise RuleStoreError(
                        f"Confirmed rule `{concept_key}` has no business-semantic change. "
                        "Update the project Knowledge binding instead; a KDV, source, audit, "
                        "or provenance change must not create a canonical-rule version."
                    )
        store_version = int(concept.get("latest_store_version") or 0) + 1
        relative = (
            DEFINITIONS_RELATIVE_PATH / concept_key / f"v{store_version:03d}.json"
        ).as_posix()
        target = self._safe_project_path(relative)
        if target.exists():
            raise RuleStoreError(f"Immutable rule version already exists: {target}")
        document = {
            "schema_version": VERSION_SCHEMA_VERSION,
            "project_id": str(store.get("project_id") or self.root.name),
            "concept_key": concept_key,
            "store_version": store_version,
            "record": copy.deepcopy(record),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, document)
        reference = {
            "store_version": store_version,
            "rule_id": str(record.get("rule_id") or ""),
            "rule_version": int(record.get("version") or 0),
            "effective_status": status,
            "path": relative,
            "record_sha256": object_sha256(record),
            "file_sha256": file_sha256(target),
            "created_at": str(record.get("created_at") or ""),
        }
        if status == "confirmed":
            for old in concept.get("versions", []) or []:
                if old.get("effective_status") in {"confirmed", "proposed"}:
                    old["effective_status"] = "superseded"
            concept["current_confirmed"] = None
            concept["proposed_versions"] = []
            concept["current_confirmed"] = compact_reference(reference)
        elif status == "proposed":
            concept.setdefault("proposed_versions", []).append(compact_reference(reference))
        else:
            for old in concept.get("versions", []) or []:
                if old.get("effective_status") == "confirmed":
                    old["effective_status"] = "superseded"
            concept["current_confirmed"] = None
            concept.setdefault("deprecated_versions", []).append(compact_reference(reference))
        concept.setdefault("versions", []).append(reference)
        concept["latest_store_version"] = store_version
        concept["latest_rule_version"] = max(
            int(concept.get("latest_rule_version") or 0),
            int(record.get("version") or 0),
        )
        concepts[concept_key] = concept
        store["updated_at"] = now_iso()
        authorization = record.get("change_authorization")
        if isinstance(authorization, dict) and authorization.get("contract_version"):
            store.setdefault(
                "authorization_contract",
                {
                    "contract_version": authorization["contract_version"],
                    "enforced_at": str(authorization.get("authorized_at") or store["updated_at"]),
                    "rule_writes_require_explicit_rules_selection": True,
                },
            )
            if not store["authorization_contract"]:
                store["authorization_contract"] = {
                    "contract_version": authorization["contract_version"],
                    "enforced_at": str(authorization.get("authorized_at") or store["updated_at"]),
                    "rule_writes_require_explicit_rules_selection": True,
                }

        self._store = store
        self._record_cache[relative] = copy.deepcopy(record)
        index = self._build_index_document(store, selector_builder)
        atomic_write_json(self.store_path, store)
        atomic_write_json(self.index_path, index)
        self._index = index
        return {
            "status": "saved",
            "concept_key": concept_key,
            "store_version": store_version,
            "rule_version": int(record.get("version") or 0),
            "path": relative,
            "record_sha256": reference["record_sha256"],
            "file_sha256": reference["file_sha256"],
        }

    def validate_store(
        self,
        *,
        require_no_legacy: bool = True,
        require_activation_v2: bool = False,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        counts = {"concepts": 0, "versions": 0, "confirmed": 0, "proposed": 0, "deprecated": 0, "superseded": 0}
        try:
            store = self.load_store(refresh=True)
        except RuleStoreError as exc:
            return {"status": "error", "errors": [str(exc)], "warnings": [], "counts": counts}
        if require_no_legacy and (self.root / LEGACY_RELATIVE_PATH).exists():
            errors.append(f"Legacy canonical rule file still exists: {LEGACY_RELATIVE_PATH.as_posix()}")
        concepts = store.get("concepts")
        if not isinstance(concepts, dict):
            errors.append("store.concepts must be an object.")
            concepts = {}
        counts["concepts"] = len(concepts)
        referenced_paths: set[str] = set()
        for concept_key, concept in concepts.items():
            if not CONCEPT_KEY_RE.fullmatch(str(concept_key)):
                errors.append(f"Invalid concept key in store: {concept_key!r}")
            versions = concept.get("versions", []) or []
            store_versions: set[int] = set()
            for reference in versions:
                counts["versions"] += 1
                status = str(reference.get("effective_status") or "")
                if status in counts:
                    counts[status] += 1
                store_version = int(reference.get("store_version") or 0)
                if store_version <= 0 or store_version in store_versions:
                    errors.append(f"Duplicate/invalid store version for {concept_key}: {store_version}")
                store_versions.add(store_version)
                relative = str(reference.get("path") or "")
                if relative in referenced_paths:
                    errors.append(f"Rule version path is referenced more than once: {relative}")
                referenced_paths.add(relative)
                try:
                    record = self._load_reference(reference, effective_status=status)
                except RuleStoreError as exc:
                    errors.append(str(exc))
                    continue
                if record.get("concept_key") != concept_key:
                    errors.append(f"Concept mismatch for {relative}: {record.get('concept_key')!r}")
                if str(record.get("rule_id") or "") != str(reference.get("rule_id") or ""):
                    errors.append(f"Rule id mismatch for {relative}")
                if int(record.get("version") or 0) != int(reference.get("rule_version") or 0):
                    errors.append(f"Rule version mismatch for {relative}")
            expected_latest = max(store_versions, default=0)
            if int(concept.get("latest_store_version") or 0) != expected_latest:
                errors.append(f"latest_store_version mismatch for {concept_key}")
            current = concept.get("current_confirmed")
            confirmed_refs = [item for item in versions if item.get("effective_status") == "confirmed"]
            if len(confirmed_refs) > 1:
                errors.append(f"More than one confirmed version for {concept_key}")
            if confirmed_refs and not isinstance(current, dict):
                errors.append(f"Missing current_confirmed pointer for {concept_key}")
            if isinstance(current, dict) and current:
                if not any(
                    item.get("path") == current.get("path") and item.get("effective_status") == "confirmed"
                    for item in versions
                ):
                    errors.append(f"current_confirmed pointer is invalid for {concept_key}")
                if require_activation_v2:
                    try:
                        current_record = self._load_reference(current, effective_status="confirmed")
                    except RuleStoreError as exc:
                        errors.append(str(exc))
                    else:
                        for problem in activation_contract_problems(current_record):
                            errors.append(f"Activation contract {concept_key}: {problem}")
            for pointer_name, expected_status in (
                ("proposed_versions", "proposed"),
                ("deprecated_versions", "deprecated"),
            ):
                for pointer in concept.get(pointer_name, []) or []:
                    if not any(
                        item.get("path") == pointer.get("path")
                        and item.get("effective_status") == expected_status
                        for item in versions
                    ):
                        errors.append(f"Invalid {pointer_name} pointer for {concept_key}: {pointer.get('path')}")
        if self.definitions_root.exists():
            actual_paths = {
                path.relative_to(self.root).as_posix()
                for path in self.definitions_root.rglob("v*.json")
                if path.is_file()
            }
            for orphan in sorted(actual_paths - referenced_paths):
                errors.append(f"Unreferenced immutable rule version: {orphan}")
            for missing in sorted(referenced_paths - actual_paths):
                errors.append(f"Referenced rule version is missing: {missing}")
        try:
            index = self.load_index(refresh=True, verify_store=True)
            expected_entries = counts["confirmed"] + counts["proposed"]
            if len(index.get("entries", []) or []) != expected_entries:
                errors.append(
                    f"Activation index entry count mismatch: expected {expected_entries}, "
                    f"found {len(index.get('entries', []) or [])}"
                )
            for entry in index.get("entries", []) or []:
                selector = entry.get("selector") or {}
                for field in ("title", "content", "description", "rule_body", "rule_text"):
                    if field in entry:
                        errors.append(
                            f"Activation index contains forbidden display text {field}: {entry.get('concept_key')}"
                        )
                for field in (
                    "search_terms",
                    "must_have_any",
                    "weak_terms",
                    "title",
                    "content",
                    "description",
                    "rule_body",
                    "rule_text",
                ):
                    if field in selector:
                        errors.append(
                            f"Activation index contains forbidden prose or fuzzy selector {field}: {entry.get('concept_key')}"
                        )
        except RuleStoreError as exc:
            errors.append(str(exc))
        return {
            "status": "ok" if not errors else "error",
            "errors": errors,
            "warnings": warnings,
            "counts": counts,
            "store_sha256": object_sha256(store),
            "paths": {
                "store": STORE_RELATIVE_PATH.as_posix(),
                "activation_index": INDEX_RELATIVE_PATH.as_posix(),
                "definitions": DEFINITIONS_RELATIVE_PATH.as_posix(),
            },
        }

    def build_dictionary_snapshot(self, *, include_history: bool = False) -> dict[str, Any]:
        concepts_payload: list[dict[str, Any]] = []
        store = self.load_store()
        for concept_key, concept in sorted((store.get("concepts") or {}).items()):
            identity_by_path: dict[str, dict[str, Any]] = {}
            semantic_versions: dict[str, int] = {}
            fingerprint_counts: dict[str, int] = {}
            references = sorted(
                concept.get("versions", []) or [],
                key=lambda item: int(item.get("store_version") or 0),
            )
            for reference in references:
                record = self._load_reference(
                    reference,
                    effective_status=str(reference.get("effective_status") or ""),
                )
                fingerprint = rule_semantic_fingerprint(record)
                if fingerprint not in semantic_versions:
                    semantic_versions[fingerprint] = len(semantic_versions) + 1
                fingerprint_counts[fingerprint] = fingerprint_counts.get(fingerprint, 0) + 1
                identity_by_path[str(reference.get("path") or "")] = {
                    "semantic_version": semantic_versions[fingerprint],
                    "semantic_fingerprint": fingerprint,
                    "record_version": int(record.get("version") or 0),
                    "record_store_version": int(reference.get("store_version") or 0),
                }

            def dictionary_record(reference: dict[str, Any], status: str) -> dict[str, Any]:
                record = runtime_rule_view(
                    self._load_reference(reference, effective_status=status)
                )
                identity = identity_by_path.get(str(reference.get("path") or ""), {})
                record.update(identity)
                fingerprint = str(identity.get("semantic_fingerprint") or "")
                record["technical_revision_count"] = fingerprint_counts.get(fingerprint, 1)
                return record

            current = []
            current_ref = concept.get("current_confirmed")
            if isinstance(current_ref, dict) and current_ref:
                current.append(dictionary_record(current_ref, "confirmed"))
            proposed = [
                dictionary_record(reference, "proposed")
                for reference in concept.get("proposed_versions", []) or []
            ]
            history_summary = [
                {
                    **compact_reference(reference),
                    "created_at": str(reference.get("created_at") or ""),
                    **identity_by_path.get(str(reference.get("path") or ""), {}),
                }
                for reference in concept.get("versions", []) or []
                if reference.get("effective_status") not in {"confirmed", "proposed"}
            ]
            payload = {
                "concept_key": concept_key,
                "current": current,
                "proposed": proposed,
                "history_summary": history_summary,
                "version_count": len(concept.get("versions", []) or []),
                "semantic_version_count": len(semantic_versions),
                "latest_store_version": int(concept.get("latest_store_version") or 0),
            }
            if include_history:
                payload["history"] = self.load_versions(concept_key)
            concepts_payload.append(payload)
        return {
            "schema_version": DICTIONARY_SNAPSHOT_VERSION,
            "project_id": str(store.get("project_id") or self.root.name),
            "store_sha256": object_sha256(store),
            "include_history": include_history,
            "concepts": concepts_payload,
        }


def empty_store(project_id: str, *, authorization_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "project_id": project_id,
        "updated_at": now_iso(),
        "authorization_contract": copy.deepcopy(authorization_contract or {}),
        "concepts": {},
    }


def initialize_empty_store(project_root: Path | str, project_id: str) -> RuleStore:
    store = RuleStore(project_root)
    if store.store_path.exists() or store.index_path.exists() or store.definitions_root.exists():
        raise RuleStoreError(f"Rule store already exists under {store.root}")
    document = empty_store(project_id)
    atomic_write_json(store.store_path, document)
    store._store = document
    store.rebuild_activation_index()
    return store
