#!/usr/bin/env python3
"""Persist local, content-bound promotion decisions for Workspace queries."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "promotion_ledger_v2"
LEGACY_SCHEMA_VERSION = "promotion_ledger_v1"
LEDGER_RELATIVE_PATH = Path("query_workspace") / "promotion_ledger.json"
DECISIONS = {"promote", "deferred", "excluded"}
LEGACY_DECISIONS = {"keep_local", "excluded_from_sync"}
REVIEW_RULE = "resurface_when_content_fingerprint_changes"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class PromotionLedgerError(ValueError):
    """Raised when a decision or candidate violates the ledger contract."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_key(query_id: str, version: int) -> str:
    clean_query_id = str(query_id or "").strip().lower()
    if not re.fullmatch(r"qw-[a-z0-9-]{8,120}", clean_query_id):
        raise PromotionLedgerError(f"Invalid Workspace query id: {query_id}")
    if isinstance(version, bool) or int(version or 0) < 1:
        raise PromotionLedgerError("Workspace version must be a positive integer.")
    return f"{clean_query_id}@v{int(version):03d}"


def _workspace_member_path(project_root: Path, relative_path: str) -> tuple[str, Path]:
    normalized = str(relative_path or "").strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise PromotionLedgerError("Workspace members require project-relative paths.")
    path_parts = Path(normalized).parts
    if not path_parts or path_parts[0] != "query_workspace" or ".." in path_parts:
        raise PromotionLedgerError(
            f"Only managed Query Workspace members may enter a promotion decision: {normalized}"
        )
    root = project_root.resolve()
    workspace_root = (root / "query_workspace").resolve()
    resolved = (root / Path(*path_parts)).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise PromotionLedgerError(
            f"Workspace member resolves outside query_workspace: {normalized}"
        ) from exc
    if not resolved.is_file():
        raise PromotionLedgerError(f"Registered Workspace member is missing: {normalized}")
    return Path(*path_parts).as_posix(), resolved


def build_content_snapshot(project_root: Path, version_record: dict[str, Any]) -> dict[str, Any]:
    """Hash the indexed SQL, sidecars, and every registered immutable output."""

    if not isinstance(version_record, dict):
        raise PromotionLedgerError("Workspace version record must be an object.")
    query_id = str(version_record.get("query_id") or "")
    version = int(version_record.get("version") or 0)
    key = candidate_key(query_id, version)
    sql_relative, sql_path = _workspace_member_path(
        project_root, str(version_record.get("path") or "")
    )
    if sql_path.suffix.lower() != ".sql":
        raise PromotionLedgerError("The indexed Workspace member must be a SQL file.")

    members: list[dict[str, Any]] = [
        {
            "role": "indexed_sql",
            "path": sql_relative,
            "sha256": sha256_file(sql_path),
        }
    ]
    meta_relative, meta_path = _workspace_member_path(
        project_root, str(version_record.get("meta_path") or "")
    )
    if not meta_path.name.endswith(".meta.json"):
        raise PromotionLedgerError("The indexed Workspace metadata must be a .meta.json sidecar.")
    members.append(
        {
            "role": "query_meta",
            "path": meta_relative,
            "sha256": sha256_file(meta_path),
        }
    )
    formalize_seed_value = str(version_record.get("formalize_seed_path") or "")
    if formalize_seed_value:
        seed_relative, seed_path = _workspace_member_path(project_root, formalize_seed_value)
        if not seed_path.name.endswith(".formalize_seed.json"):
            raise PromotionLedgerError(
                "The indexed Workspace formalization seed must be a .formalize_seed.json sidecar."
            )
        members.append(
            {
                "role": "query_spec",
                "path": seed_relative,
                "sha256": sha256_file(seed_path),
            }
        )
    outputs = version_record.get("derived_outputs", [])
    if not isinstance(outputs, list):
        raise PromotionLedgerError("Workspace derived_outputs must be an array.")
    seen_paths = {str(item["path"]) for item in members}
    for position, output in enumerate(outputs):
        if not isinstance(output, dict):
            raise PromotionLedgerError("Every registered Workspace output must be an object.")
        output_relative, output_path = _workspace_member_path(
            project_root, str(output.get("path") or "")
        )
        if output_relative in seen_paths:
            raise PromotionLedgerError(
                f"Workspace promotion members contain a duplicate path: {output_relative}"
            )
        seen_paths.add(output_relative)
        members.append(
            {
                "role": "registered_output",
                "path": output_relative,
                "sha256": sha256_file(output_path),
                "kind": str(output.get("kind") or "other"),
                "attachment_id": str(output.get("attachment_id") or ""),
                "position": position,
            }
        )

    fingerprint_payload = {
        "query_id": query_id,
        "version": version,
        "workspace_role": str(
            version_record.get("workspace_role")
            or version_record.get("asset_role")
            or "unknown"
        ),
        "role_lineage": copy.deepcopy(version_record.get("role_lineage") or {}),
        "members": members,
    }
    fingerprint = hashlib.sha256(canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
    return {
        "candidate_key": key,
        "query_id": query_id,
        "version": version,
        "workspace_role": fingerprint_payload["workspace_role"],
        "role_lineage": copy.deepcopy(fingerprint_payload["role_lineage"]),
        "content_fingerprint": fingerprint,
        "members": members,
    }


def empty_ledger(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": SCHEMA_VERSION,
        "project_id": str(project_id or "").strip(),
        "updated_at": "",
        "entries": {},
    }


def ledger_path(project_root: Path) -> Path:
    return project_root.resolve() / LEDGER_RELATIVE_PATH


def load_ledger(project_root: Path, *, project_id: str = "") -> dict[str, Any]:
    path = ledger_path(project_root)
    if not path.exists():
        return empty_ledger(project_id or project_root.resolve().name)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionLedgerError(f"Promotion Ledger is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PromotionLedgerError("Unsupported or malformed Promotion Ledger.")
    schema_version = str(value.get("schema_version") or "")
    if schema_version == LEGACY_SCHEMA_VERSION:
        stored_project_id = str(value.get("project_id") or "")
        if project_id and stored_project_id and stored_project_id != project_id:
            raise PromotionLedgerError(
                f"Promotion Ledger belongs to `{stored_project_id}`, not `{project_id}`."
            )
        normalized = empty_ledger(project_id or str(value.get("project_id") or project_root.name))
        normalized["legacy_source_schema"] = LEGACY_SCHEMA_VERSION
        normalized["legacy_updated_at"] = str(value.get("updated_at") or "")
        legacy_entries = value.get("entries")
        if not isinstance(legacy_entries, dict):
            raise PromotionLedgerError("Legacy Promotion Ledger entries must be an object.")
        for key, raw in legacy_entries.items():
            if not isinstance(raw, dict):
                raise PromotionLedgerError(f"Legacy Promotion Ledger entry is not an object: {key}")
            item = copy.deepcopy(raw)
            legacy_decision = str(item.get("decision") or "")
            item["legacy_schema_version"] = LEGACY_SCHEMA_VERSION
            if legacy_decision == "keep_local":
                item["decision"] = "deferred"
                item["legacy_decision"] = legacy_decision
                item["requires_review"] = True
                item["review_rule"] = "legacy_decision_requires_reconfirmation"
            elif legacy_decision == "excluded_from_sync":
                item["decision"] = "excluded"
                item["legacy_decision"] = legacy_decision
                item["requires_review"] = True
                item["review_rule"] = "legacy_decision_requires_reconfirmation"
            elif legacy_decision not in DECISIONS:
                item["decision"] = "deferred"
                item["legacy_decision"] = legacy_decision
                item["requires_review"] = True
                item["review_rule"] = "legacy_decision_requires_reconfirmation"
            normalized["entries"][str(key)] = item
        return normalized
    if schema_version != SCHEMA_VERSION:
        raise PromotionLedgerError("Unsupported or malformed Promotion Ledger.")
    if not isinstance(value.get("entries"), dict):
        raise PromotionLedgerError("Promotion Ledger entries must be an object.")
    stored_project_id = str(value.get("project_id") or "")
    if project_id and stored_project_id and stored_project_id != project_id:
        raise PromotionLedgerError(
            f"Promotion Ledger belongs to `{stored_project_id}`, not `{project_id}`."
        )
    return value


def review_state(ledger: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    entries = ledger.get("entries") if isinstance(ledger, dict) else None
    if not isinstance(entries, dict):
        raise PromotionLedgerError("Promotion Ledger entries must be an object.")
    key = str(snapshot.get("candidate_key") or "")
    fingerprint = str(snapshot.get("content_fingerprint") or "")
    if not key or not SHA256_RE.fullmatch(fingerprint):
        raise PromotionLedgerError("Candidate snapshot is missing its stable key or fingerprint.")
    prior = entries.get(key)
    if not isinstance(prior, dict):
        return {
            "review_status": "new",
            "requires_review": True,
            "prior_decision": "",
            "prior_content_fingerprint": "",
        }
    prior_fingerprint = str(prior.get("content_fingerprint") or "")
    if prior_fingerprint == fingerprint and prior.get("requires_review") is not True:
        return {
            "review_status": "unchanged_skipped",
            "requires_review": False,
            "prior_decision": str(prior.get("decision") or ""),
            "prior_content_fingerprint": prior_fingerprint,
        }
    if prior_fingerprint == fingerprint and prior.get("requires_review") is True:
        return {
            "review_status": "legacy_requires_review",
            "requires_review": True,
            "prior_decision": str(prior.get("legacy_decision") or prior.get("decision") or ""),
            "prior_content_fingerprint": prior_fingerprint,
        }
    return {
        "review_status": "changed",
        "requires_review": True,
        "prior_decision": str(prior.get("decision") or ""),
        "prior_content_fingerprint": prior_fingerprint,
    }


def _validate_decision(
    snapshot: dict[str, Any],
    *,
    decision: str,
    reason: str,
    user_request: str,
    confirmed_by_user: bool,
) -> None:
    if decision not in DECISIONS:
        raise PromotionLedgerError(f"Unsupported promotion decision: {decision}")
    if confirmed_by_user is not True:
        raise PromotionLedgerError("Promotion decisions require explicit user confirmation.")
    if len(str(reason or "").strip()) < 4:
        raise PromotionLedgerError("A concrete promotion decision reason is required.")
    if len(str(user_request or "").strip()) < 4:
        raise PromotionLedgerError("The confirming user request must be recorded verbatim.")
    if not SHA256_RE.fullmatch(str(snapshot.get("content_fingerprint") or "")):
        raise PromotionLedgerError("A valid candidate content fingerprint is required.")
    members = snapshot.get("members")
    if (
        not isinstance(members, list)
        or not members
        or not isinstance(members[0], dict)
        or members[0].get("role") != "indexed_sql"
    ):
        raise PromotionLedgerError("A promotion decision requires an indexed SQL member snapshot.")


def decision_record(
    snapshot: dict[str, Any],
    *,
    decision: str,
    reason: str,
    user_request: str,
    confirmed_by_user: bool,
    confirmed_by: str = "user",
    confirmed_at: str = "",
    repository_receipt: dict[str, Any] | None = None,
    missing_conditions: list[str] | None = None,
    revisit_when: str = "",
) -> dict[str, Any]:
    _validate_decision(
        snapshot,
        decision=decision,
        reason=reason,
        user_request=user_request,
        confirmed_by_user=confirmed_by_user,
    )
    record = {
        "candidate_key": str(snapshot["candidate_key"]),
        "query_id": str(snapshot["query_id"]),
        "version": int(snapshot["version"]),
        "content_fingerprint": str(snapshot["content_fingerprint"]),
        "workspace_role": str(snapshot.get("workspace_role") or "unknown"),
        "role_lineage": copy.deepcopy(snapshot.get("role_lineage") or {}),
        "members": copy.deepcopy(snapshot["members"]),
        "decision": decision,
        "reason": str(reason).strip(),
        "user_request": str(user_request).strip(),
        "confirmed_by_user": True,
        "confirmed_by": str(confirmed_by or "user").strip() or "user",
        "confirmed_at": str(confirmed_at or now_iso()),
        "review_rule": REVIEW_RULE,
    }
    if decision == "deferred":
        conditions = sorted(
            {
                str(item).strip()
                for item in (missing_conditions or [])
                if str(item).strip()
            }
        )
        if not conditions:
            raise PromotionLedgerError("Deferred decisions require at least one missing condition.")
        revisit = str(revisit_when or "").strip()
        if len(revisit) < 4:
            raise PromotionLedgerError("Deferred decisions require a concrete revisit rule.")
        record["missing_conditions"] = conditions
        record["revisit_when"] = revisit
    elif missing_conditions or revisit_when:
        raise PromotionLedgerError("Missing conditions and revisit rules apply only to deferred decisions.")
    if repository_receipt is not None:
        if decision != "promote":
            raise PromotionLedgerError(
                "Only a promote decision may retain a Formal Asset Repository receipt."
            )
        if (
            not isinstance(repository_receipt, dict)
            or repository_receipt.get("schema_version")
            != "formal_asset_repository_receipt_v1"
            or repository_receipt.get("status") != "ready"
            or not str(repository_receipt.get("package_id") or "")
            or not str(repository_receipt.get("receipt_id") or "")
            or not isinstance(repository_receipt.get("files"), list)
        ):
            raise PromotionLedgerError("Promote requires a valid Formal Asset Repository receipt.")
        record["repository_receipt"] = copy.deepcopy(repository_receipt)
    return record


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def record_decision(
    project_root: Path,
    snapshot: dict[str, Any],
    *,
    decision: str,
    reason: str,
    user_request: str,
    confirmed_by_user: bool,
    confirmed_by: str = "user",
    confirmed_at: str = "",
    project_id: str = "",
    repository_receipt: dict[str, Any] | None = None,
    missing_conditions: list[str] | None = None,
    revisit_when: str = "",
) -> dict[str, Any]:
    """Atomically upsert one explicit decision without touching Workspace members."""

    record = decision_record(
        snapshot,
        decision=decision,
        reason=reason,
        user_request=user_request,
        confirmed_by_user=confirmed_by_user,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        repository_receipt=repository_receipt,
        missing_conditions=missing_conditions,
        revisit_when=revisit_when,
    )
    ledger = load_ledger(project_root, project_id=project_id)
    key = record["candidate_key"]
    if ledger["entries"].get(key) == record:
        return {
            "status": "unchanged",
            "path": LEDGER_RELATIVE_PATH.as_posix(),
            "record": copy.deepcopy(record),
        }
    ledger["project_id"] = str(project_id or ledger.get("project_id") or project_root.name)
    ledger["updated_at"] = str(record["confirmed_at"])
    ledger["entries"][key] = copy.deepcopy(record)
    _atomic_write_json(ledger_path(project_root), ledger)
    return {
        "status": "recorded",
        "path": LEDGER_RELATIVE_PATH.as_posix(),
        "record": record,
    }
