#!/usr/bin/env python3
"""Generation provenance helpers for SQL Engineering Skill assets."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
SKILL_MD = SKILL_ROOT / "SKILL.md"
PROVENANCE_SCHEMA_VERSION = "asset_generation_v1"
SQL_GENERATION_MARKER = "@SQL_GENERATION"
LDAP_CONFIG_KEY = "da-skills.ldapUsername"
LEGACY_LDAP_CONFIG_KEY = "da-skills.collaborationUsername"
LDAP_ENV = "DA_SKILLS_LDAP_USERNAME"
PUBLIC_IDENTITY_ENV = "DA_SKILLS_USER"
LDAP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SQL_GENERATION_RE = re.compile(
    rf"^[ \t]*--[ \t]*{re.escape(SQL_GENERATION_MARKER)}\b[^\r\n]*(?:\r?\n)?",
    flags=re.I | re.M,
)


class GenerationIdentityError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def skill_metadata() -> dict[str, str]:
    text = SKILL_MD.read_text(encoding="utf-8", errors="replace") if SKILL_MD.exists() else ""
    name_match = re.search(r"^name:\s*([^\r\n]+)", text, flags=re.M)
    version_match = re.search(r'^\s*version:\s*"?([^"\r\n]+)"?', text, flags=re.M)
    spec_match = re.search(r'^\s*spec-version:\s*"?([^"\r\n]+)"?', text, flags=re.M)
    return {
        "skill_name": (name_match.group(1).strip() if name_match else "sql-engineering"),
        "skill_version": (version_match.group(1).strip() if version_match else "unknown"),
        "sql_spec_version": (spec_match.group(1).strip() if spec_match else "unknown"),
    }


def _git_config(start: Path, key: str) -> str:
    location = start if start.is_dir() else start.parent
    completed = subprocess.run(
        ["git", "-C", str(location), "config", "--local", "--get", key],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


@lru_cache(maxsize=16)
def _generated_by_ldap(start_value: str) -> str:
    start = Path(start_value).resolve()
    candidates = [
        _git_config(start, "user.name"),
        os.environ.get(PUBLIC_IDENTITY_ENV, "").strip(),
        _git_config(start, LDAP_CONFIG_KEY),
        os.environ.get(LDAP_ENV, "").strip(),
        _git_config(SKILL_ROOT, LDAP_CONFIG_KEY),
        _git_config(start, LEGACY_LDAP_CONFIG_KEY),
        _git_config(SKILL_ROOT, LEGACY_LDAP_CONFIG_KEY),
    ]
    username = next((value for value in candidates if value), "")
    if not username:
        username = "local-user"
    if not LDAP_RE.fullmatch(username):
        raise GenerationIdentityError(
            f"Invalid SQL generator LDAP username `{username}`; expected letters, digits, dot, underscore, or hyphen."
        )
    return username


def generated_by_ldap(start: Path) -> str:
    return _generated_by_ldap(str(start.resolve()))


def sql_generation_comment(start: Path) -> str:
    metadata = skill_metadata()
    return (
        f"-- {SQL_GENERATION_MARKER} skill={metadata['skill_name']}; "
        f"skill_version={metadata['skill_version']}; generated_by_ldap={generated_by_ldap(start)}"
    )


def strip_sql_generation_comment(sql_text: str) -> str:
    return SQL_GENERATION_RE.sub("", str(sql_text or "")).lstrip("\r\n")


def stamp_sql_generation(start: Path, sql_text: str) -> str:
    body = strip_sql_generation_comment(sql_text)
    return f"{sql_generation_comment(start)}\n{body}"


def build_generation_provenance(
    *,
    generator_script: str,
    workflow: str,
    artifact_kind: str,
    generated_at: str | None = None,
    source: str = "generated",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = skill_metadata()
    provenance: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "skill_name": meta["skill_name"],
        "skill_version": meta["skill_version"],
        "sql_spec_version": meta["sql_spec_version"],
        "artifact_kind": artifact_kind,
        "workflow": workflow,
        "source": source,
        "generated_by_script": generator_script,
        "generated_at": generated_at or now_iso(),
    }
    if extra:
        provenance.update({key: value for key, value in extra.items() if value not in (None, "", [])})
    return provenance


def merge_generation_provenance(
    existing: dict[str, Any] | None,
    *,
    fallback_generator_script: str,
    fallback_workflow: str,
    artifact_kind: str,
    saved_at: str | None = None,
    saved_by_script: str = "sql_project.py",
) -> dict[str, Any]:
    fallback = build_generation_provenance(
        generator_script=fallback_generator_script,
        workflow=fallback_workflow,
        artifact_kind=artifact_kind,
        generated_at=saved_at,
    )
    clean_existing = {
        key: value
        for key, value in (existing or {}).items()
        if value not in (None, "", [])
    }
    provenance = {**fallback, **clean_existing}
    provenance.setdefault("schema_version", PROVENANCE_SCHEMA_VERSION)
    provenance["artifact_kind"] = artifact_kind or provenance.get("artifact_kind", "")
    if saved_at:
        provenance["saved_at"] = saved_at
    provenance["saved_by_script"] = saved_by_script
    return provenance


def apply_generation_provenance(spec: dict[str, Any], provenance: dict[str, Any]) -> None:
    spec["generation_provenance"] = provenance
    meta = spec.setdefault("spec_meta", {})
    if isinstance(meta, dict):
        meta["skill_name"] = provenance.get("skill_name", "sql-engineering")
        meta["skill_version"] = provenance.get("skill_version", "unknown")
        meta["generator_script"] = provenance.get("generated_by_script", "unknown")
        meta["generation_workflow"] = provenance.get("workflow", "unknown")


def provenance_from_sources(artifact: dict[str, Any] | None, spec: dict[str, Any] | None) -> dict[str, Any]:
    artifact = artifact or {}
    spec = spec or {}
    for value in (spec.get("generation_provenance"), artifact.get("generation_provenance")):
        if isinstance(value, dict) and value:
            return value
    spec_meta = spec.get("spec_meta") if isinstance(spec.get("spec_meta"), dict) else {}
    if spec_meta:
        meta = skill_metadata()
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "skill_name": spec_meta.get("skill_name") or meta["skill_name"],
            "skill_version": spec_meta.get("skill_version") or "unknown",
            "sql_spec_version": spec_meta.get("spec_version") or meta["sql_spec_version"],
            "artifact_kind": artifact.get("kind") or spec_meta.get("lifecycle_stage") or "",
            "workflow": spec_meta.get("generation_workflow") or "legacy_or_unknown",
            "source": "legacy_fallback",
            "generated_by_script": spec_meta.get("generator_script") or spec_meta.get("generated_by") or "unknown",
            "generated_at": spec_meta.get("generated_at") or artifact.get("created_at") or "",
            "saved_at": artifact.get("created_at") or "",
            "saved_by_script": "unknown",
        }
    return {}


def provenance_label(provenance: dict[str, Any] | None) -> str:
    if not provenance:
        return "生成来源未记录"
    skill = provenance.get("skill_name") or "sql-engineering"
    version = provenance.get("skill_version") or "unknown"
    spec = provenance.get("sql_spec_version") or "unknown"
    workflow = provenance.get("workflow") or "unknown"
    script = provenance.get("generated_by_script") or "unknown"
    source = provenance.get("source") or "generated"
    return f"{skill} v{version} / spec {spec} / {workflow} / {script} / {source}"
