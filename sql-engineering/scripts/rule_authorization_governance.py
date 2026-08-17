#!/usr/bin/env python3
"""Create hash-bound authorization amendments for immutable rule history."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from function_gate import require_explicit_rule_write_authorization  # noqa: E402
from rule_store import RuleStore, atomic_write_json  # noqa: E402


SCHEMA_VERSION = "canonical_rule_authorization_amendments_v1"
RELATIVE_PATH = Path("rules/governance/write-authorization-amendments.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_document(root: Path) -> dict[str, Any]:
    path = root / RELATIVE_PATH
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": root.name,
            "updated_at": "",
            "amendments": [],
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported authorization amendment schema: {value.get('schema_version')!r}")
    return value


def amendment_index(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    document = load_document(root)
    return {
        (str(item.get("path") or ""), str(item.get("record_sha256") or "")): item
        for item in document.get("amendments", []) or []
        if isinstance(item, dict)
    }


def amend(
    root: Path,
    rule_ids: list[str],
    *,
    function_selection: str,
    user_request: str,
    reason: str,
) -> dict[str, Any]:
    authorization = require_explicit_rule_write_authorization(
        function_selection,
        user_request=user_request,
        requested_status="confirmed",
    )
    requested = {str(item).strip() for item in rule_ids if str(item).strip()}
    if not requested:
        raise ValueError("At least one --rule-id is required")
    store = RuleStore(root)
    matches = [rule for rule in store.load_all_versions() if str(rule.get("rule_id") or "") in requested]
    found = {str(rule.get("rule_id") or "") for rule in matches}
    missing = sorted(requested - found)
    if missing:
        raise ValueError("Unknown rule id(s): " + ", ".join(missing))
    document = load_document(root)
    rows = {
        (str(item.get("path") or ""), str(item.get("record_sha256") or "")): item
        for item in document.get("amendments", []) or []
        if isinstance(item, dict)
    }
    added: list[dict[str, Any]] = []
    for rule in matches:
        if isinstance(rule.get("change_authorization"), dict) and rule["change_authorization"].get("contract_version"):
            continue
        store_meta = rule.get("_rule_store") or {}
        key = (str(store_meta.get("path") or ""), str(store_meta.get("record_sha256") or ""))
        if not all(key):
            raise ValueError(f"Rule {rule.get('rule_id')} is missing immutable store identity")
        if key in rows:
            continue
        item = {
            "rule_id": str(rule.get("rule_id") or ""),
            "concept_key": str(rule.get("concept_key") or ""),
            "rule_version": int(rule.get("version") or 0),
            "path": key[0],
            "record_sha256": key[1],
            "reason": reason,
            "authorization": authorization,
            "amended_at": now_iso(),
        }
        rows[key] = item
        added.append(item)
    document["project_id"] = root.name
    document["updated_at"] = now_iso()
    document["amendments"] = sorted(rows.values(), key=lambda item: (item["concept_key"], item["rule_version"]))
    path = root / RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, document)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "updated" if added else "unchanged",
        "project_id": root.name,
        "path": RELATIVE_PATH.as_posix(),
        "added": len(added),
        "total": len(document["amendments"]),
        "rule_ids": sorted({item["rule_id"] for item in added}),
        "user_request_sha256": hashlib.sha256(user_request.encode("utf-8")).hexdigest(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("amend", nargs="?")
    parser.add_argument("--root", required=True)
    parser.add_argument("--rule-id", action="append", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--function-selection", required=True)
    parser.add_argument("--user-request", required=True)
    parser.add_argument("--format", choices=["json"], default="json")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = amend(
        Path(args.root).resolve(),
        args.rule_id,
        function_selection=args.function_selection,
        user_request=args.user_request,
        reason=args.reason,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
