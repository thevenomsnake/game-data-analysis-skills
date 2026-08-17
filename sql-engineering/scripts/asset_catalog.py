#!/usr/bin/env python3
"""Build package-backed read models over shared SQL Engineering assets."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from asset_provenance import build_generation_provenance, now_iso
from execution_delivery import build_execution_delivery, finalize_execution_deliveries
from workbook_manifest import reusable_workbook_presentation


SCHEMA_VERSION = "sql_asset_catalog_v2"
DEFAULT_OUTPUT = Path("_asset_catalog") / "asset_catalog.json"
MAX_RESPONSE_ITEMS = 100
PACKAGE_SCHEMA_VERSION = "formal_asset_package_v1"
PACKAGE_ROOT_NAME = "formal_assets"
PACKAGE_MANIFEST_NAME = "manifest.json"
PACKAGE_ASSET_KIND = "formal_asset_package"
PACKAGE_MEMBER_ASSET_KIND = "formal_asset_member"
EXCLUDED_ASSET_PARTS = {
    "query_workspace",
    "promotion_ledger",
    "_promotion_ledger",
    "archive",
}
LEGACY_FORMAL_ROOTS = {"query_sql", "dashboard_sql", "validations", "runs"}
FORMAL_KIND_MAP = {
    "QUERY": "query",
    "DASHBOARD": "dashboard",
    "VALIDATION": "validation",
}
OUTPUT_KIND_MAP = {
    "result_evidence": "result",
    "analysis_workbook": "analysis_workbook",
    "comparison_workbook": "comparison_workbook",
    "visualization": "visualization",
    "export": "export",
    "other": "derived_output",
}
PACKAGE_ROLE_KIND_MAP = {
    # Lifecycle emits semantic formal-query roles; migration preserves the
    # explicit SQL/spec/meta vocabulary. Keep both vocabularies equivalent at
    # the catalog seam so downstream consumers do not need role branching.
    "formal_query": "query",
    "formal_query_unverified": "query",
    "formal_query_sql": "query",
    "formal_query_spec": "formal_asset_member",
    "formal_query_meta": "formal_asset_member",
    "query_sql": "query",
    "historical_query_sql": "query",
    "query_spec": "formal_asset_member",
    "query_meta": "formal_asset_member",
    "dashboard_delivery": "dashboard",
    "dashboard_delivery_sql": "dashboard",
    "dashboard_delivery_spec": "formal_asset_member",
    "dashboard_delivery_meta": "formal_asset_member",
    "dashboard_sql": "dashboard",
    "dashboard_spec": "formal_asset_member",
    "dashboard_meta": "formal_asset_member",
    "validation": "validation",
    "validation_sql": "validation",
    "validation_spec": "formal_asset_member",
    "validation_meta": "formal_asset_member",
    "result_evidence": "result",
    "historical_result_evidence": "result",
    "run_record": "result",
    "run_evidence": "result",
    "analysis_workbook": "analysis_workbook",
    "comparison_workbook": "comparison_workbook",
    "visualization": "visualization",
    "export": "export",
    "derived_output": "derived_output",
    "historical_derived_output": "derived_output",
    "registered_output": "derived_output",
    "legacy_quarantine_evidence": "derived_output",
    "other": "derived_output",
}
PACKAGE_ROLE_RELATIONS = {
    "formal_query": "has_formal_query",
    "formal_query_unverified": "has_formal_query",
    "formal_query_sql": "has_formal_query",
    "formal_query_spec": "has_query_contract",
    "formal_query_meta": "has_query_metadata",
    "query_sql": "has_formal_query",
    "historical_query_sql": "has_formal_query",
    "query_spec": "has_query_contract",
    "query_meta": "has_query_metadata",
    "dashboard_delivery": "has_dashboard_delivery",
    "dashboard_delivery_sql": "has_dashboard_delivery",
    "dashboard_delivery_spec": "has_dashboard_contract",
    "dashboard_delivery_meta": "has_dashboard_metadata",
    "dashboard_sql": "has_dashboard_delivery",
    "dashboard_spec": "has_dashboard_contract",
    "dashboard_meta": "has_dashboard_metadata",
    "validation": "has_validation",
    "validation_sql": "has_validation",
    "validation_spec": "has_validation_contract",
    "validation_meta": "has_validation_metadata",
    "result_evidence": "has_evidence",
    "historical_result_evidence": "has_evidence",
    "run_record": "has_run_evidence",
    "run_evidence": "has_run_evidence",
    "analysis_workbook": "has_derived_output",
    "comparison_workbook": "has_derived_output",
    "visualization": "has_derived_output",
    "export": "has_derived_output",
    "derived_output": "has_derived_output",
    "historical_derived_output": "has_derived_output",
    "registered_output": "has_derived_output",
    "legacy_quarantine_evidence": "has_derived_output",
    "other": "has_derived_output",
}

REQUIRED_DOCUMENTATION = (
    "README.md",
    "docs/README.md",
    "docs/READONLY_ASSET_CONSUMER_GUIDE.md",
)
CONSUMER_CONTRACTS = (
    (
        "sql-engineering/schemas/asset_catalog.json",
        "asset-catalog-schema",
        "资产目录 Schema",
        "json_schema",
        "sql_asset_catalog_v2",
    ),
    (
        "sql-engineering/schemas/asset_organization.json",
        "asset-organization-schema",
        "资产语义组织 Schema",
        "json_schema",
        "sql_asset_organization_v2",
    ),
    (
        "sql-engineering/schemas/asset_group_registry.json",
        "asset-group-registry-schema",
        "稳定资产组注册表 Schema",
        "json_schema",
        "sql_asset_group_registry_v2",
    ),
    (
        "sql-engineering/schemas/formal_asset_package.json",
        "formal-asset-package-schema",
        "正式资产包 Schema",
        "json_schema",
        "formal_asset_package_v1",
    ),
    (
        "sql-engineering/schemas/formal_asset_receipt.json",
        "formal-asset-receipt-schema",
        "正式资产包回执 Schema",
        "json_schema",
        "formal_asset_repository_receipt_v1",
    ),
    (
        "sql-engineering/schemas/execution_variant_identity.json",
        "execution-variant-identity-schema",
        "SQL 执行变体显式身份 Schema",
        "json_schema",
        "execution_variant_identity_v1",
    ),
    (
        "sql-engineering/schemas/execution_delivery.json",
        "execution-delivery-schema",
        "SQL 执行交付消费投影 Schema",
        "json_schema",
        "execution_delivery_v1",
    ),
    (
        "sql-engineering/schemas/workbook_manifest.json",
        "workbook-manifest-schema",
        "可复用工作簿有界 Manifest Schema",
        "json_schema",
        "workbook_manifest_v1",
    ),
    (
        "sql-engineering/schemas/reusable_workbook_presentation.json",
        "reusable-workbook-presentation-schema",
        "可复用工作簿消费展示 Schema",
        "json_schema",
        "reusable_workbook_presentation_v1",
    ),
    (
        "sql-engineering/schemas/sql_result_visualization_receipt.json",
        "sql-result-visualization-receipt-schema",
        "SQL 结果与可视化绑定回执 Schema",
        "json_schema",
        "sql_result_visualization_receipt_v1",
    ),
    (
        "sql-engineering/schemas/result_lineage_decision.json",
        "result-lineage-decision-schema",
        "结果资产关系决策 Schema",
        "json_schema",
        "result_lineage_decision_v1",
    ),
    (
        "sql-engineering/assets/default_asset_taxonomy.json",
        "asset-taxonomy",
        "资产主题 Taxonomy",
        "taxonomy",
        "sql_asset_taxonomy_v1",
    ),
    (
        "sql-engineering/references/asset-catalog.md",
        "asset-catalog-reference",
        "资产目录消费协议",
        "consumer_reference",
        "sql_asset_catalog_v2",
    ),
    (
        "sql-engineering/references/asset-organization.md",
        "asset-organization-reference",
        "资产语义组织消费协议",
        "consumer_reference",
        "sql_asset_organization_v2",
    ),
    (
        "sql-engineering/references/asset-groups.md",
        "asset-groups-reference",
        "稳定资产组与主页目录消费协议",
        "consumer_reference",
        "sql_asset_group_registry_v2",
    ),
    (
        "sql-engineering/references/result-lineage-organization.md",
        "result-lineage-organization-reference",
        "结果资产关系整理协议",
        "consumer_reference",
        "result_lineage_decision_v1",
    ),
)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
SKILL_VERSION_RE = re.compile(r"SQL Engineering Skill\s+(\d+\.\d+\.\d+)", re.IGNORECASE)


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def safe_token(value: Any, fallback: str = "unknown") -> str:
    text = clean_text(value).replace("\\", "/").strip("/")
    return text.replace(":", "-") or fallback


def path_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return token or "document"


def markdown_title_summary(path: Path, text: str) -> tuple[str, str]:
    title = path.stem.replace("_", " ").replace("-", " ")
    summary_parts: list[str] = []
    in_code = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line or line.startswith(">"):
            continue
        if line.startswith("# ") and title == path.stem.replace("_", " ").replace("-", " "):
            title = line[2:].strip() or title
            continue
        if line.startswith("#") or line.startswith("|") or line.startswith("-") or re.match(r"^\d+\.\s", line):
            if summary_parts:
                break
            continue
        summary_parts.append(line)
        if len(" ".join(summary_parts)) >= 180:
            break
    summary = " ".join(summary_parts).strip()
    return title, summary[:240]


def documentation_profile(relative: str) -> dict[str, Any]:
    normalized = relative.replace("\\", "/")
    name = Path(normalized).name.upper()
    if normalized == "README.md":
        kind, audience, scope = "platform_overview", "all_users", "external_core"
    elif normalized == "docs/README.md":
        kind, audience, scope = "documentation_index", "all_users", "external_core"
    elif name == "READONLY_ASSET_CONSUMER_GUIDE.MD":
        kind, audience, scope = "consumer_manual", "external_consumer", "external_core"
    elif name == "USER_MANUAL.MD":
        kind, audience, scope = "user_manual", "sql_user", "platform_manual"
    elif name in {"SKILL_OPERATIONS.MD", "DOCUMENTATION_STANDARD.MD"}:
        kind, audience, scope = "operations_manual", "skill_maintainer", "platform_manual"
    elif normalized.startswith("docs/configuration/"):
        kind, audience, scope = "configuration_manual", "project_admin", "platform_manual"
    elif normalized.startswith("docs/decisions/"):
        kind, audience, scope = "architecture_decision", "skill_maintainer", "platform_manual"
    elif name == "PROJECT_OVERVIEW.MD":
        kind, audience, scope = "architecture_manual", "all_users", "platform_manual"
    elif name == "SQL_REVIEW_TOOL_DESIGN.MD":
        kind, audience, scope = "design_manual", "review_maintainer", "platform_manual"
    else:
        kind, audience, scope = "platform_manual", "platform_operator", "platform_manual"
    return {
        "document_kind": kind,
        "audience": audience,
        "consumer_scope": scope,
        "section": Path(normalized).parent.as_posix(),
    }


def derived_output_consumer_facts(kind: str, output: dict[str, Any]) -> dict[str, Any]:
    path = clean_text(output.get("path"))
    media_type = clean_text(output.get("media_type")) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    presentation = reusable_workbook_presentation(kind, media_type, path, output)
    if kind == "result":
        surface = "result_evidence"
    elif presentation["eligible"]:
        surface = "reusable_workbook"
    else:
        surface = "other"
    return {
        "consumer_surface": surface,
        "workbook_presentation": presentation,
    }


def markdown_link_targets(text: str) -> list[str]:
    targets: list[str] = []
    for match in MARKDOWN_LINK_RE.finditer(text):
        raw = match.group(1).strip()
        if raw.startswith("<") and ">" in raw:
            raw = raw[1 : raw.index(">")]
        elif " " in raw:
            raw = raw.split(" ", 1)[0]
        raw = unquote(raw.strip())
        if raw:
            targets.append(raw)
    return targets


def git_snapshot(repo_root: Path) -> dict[str, Any]:
    result = {"commit": "", "branch": "", "worktree_dirty": False}
    commands = {
        "commit": ["git", "rev-parse", "HEAD"],
        "branch": ["git", "branch", "--show-current"],
    }
    for key, command in commands.items():
        try:
            completed = subprocess.run(
                command,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                result[key] = completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        result["worktree_dirty"] = completed.returncode == 0 and bool(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def package_asset_id(project_id: str, package_id: str) -> str:
    return f"{project_id}:formal_asset_package:{package_id}"


def package_member_asset_id(project_id: str, package_id: str, member_id: str) -> str:
    return f"{project_id}:formal_asset_package:{package_id}:member:{member_id}"


def package_member_kind(role: str) -> str:
    return PACKAGE_ROLE_KIND_MAP.get(clean_text(role).lower(), PACKAGE_MEMBER_ASSET_KIND)


def package_current_member_ids(manifest: dict[str, Any]) -> set[str]:
    current = dict_value(manifest.get("current"))
    values = {
        clean_text(item)
        for item in list_value(current.get("member_ids"))
        if clean_text(item)
    }
    for rows in dict_value(current.get("by_role")).values():
        values.update(clean_text(item) for item in list_value(rows) if clean_text(item))
    return values


def package_semantic_facts(doc: dict[str, Any]) -> dict[str, Any]:
    repository_summary = dict_value(doc.get("repository_summary"))
    return {
        "business_category": clean_text(doc.get("business_category")),
        "business_topic": clean_text(repository_summary.get("business_topic")),
        "business_question": clean_text(repository_summary.get("business_question")),
        "analysis_type": clean_text(doc.get("analysis_type")),
        "metrics": list_value(repository_summary.get("metrics")) or list_value(doc.get("metrics")),
        "dimensions": list_value(repository_summary.get("dimensions")) or list_value(doc.get("dimensions")),
        "filters": list_value(repository_summary.get("filters")) or list_value(doc.get("filters")),
        "source_logs": list_value(repository_summary.get("source_logs")) or list_value(doc.get("source_logs")),
        "tables": list_value(doc.get("tables")),
        "grain": clean_text(repository_summary.get("grain")) or clean_text(doc.get("grain")),
        "time_grain": clean_text(doc.get("time_grain")),
        "tags": list_value(doc.get("tags")),
        "purpose": clean_text(repository_summary.get("purpose")),
    }


def merge_nonempty_facts(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key not in target or target[key] in (None, "", [], {}):
            if value not in (None, "", [], {}):
                target[key] = value


def excluded_catalog_path(value: str) -> bool:
    parts = tuple(part.lower() for part in Path(clean_text(value)).parts)
    if any(part in EXCLUDED_ASSET_PARTS or Path(part).stem in EXCLUDED_ASSET_PARTS for part in parts):
        return True
    return len(parts) >= 3 and parts[0] == "sql-projects" and parts[2] in LEGACY_FORMAL_ROOTS


class CatalogBuilder:
    def __init__(self, projects_root: Path) -> None:
        self.projects_root = projects_root.resolve()
        self.repo_root = self.projects_root.parent.resolve()
        self.assets: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.relationships: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.path_to_asset: dict[str, str] = {}
        self.rule_targets: dict[tuple[str, str], str] = {}
        self.knowledge_targets: dict[tuple[str, str], str] = {}

    def issue(self, code: str, message: str, *, path: str = "", asset_id: str = "") -> None:
        self.issues.append(
            {
                "code": code,
                "message": message,
                "path": path,
                "asset_id": asset_id,
            }
        )

    def repo_path(self, base: Path, value: Any) -> tuple[Path | None, str]:
        text = clean_text(value).replace("\\", "/")
        if not text:
            return None, ""
        candidate = Path(text)
        if candidate.is_absolute():
            self.issue("absolute_path_rejected", "Asset catalogs accept repository-relative paths only.", path=text)
            return None, ""
        if text.startswith("sql-projects/") or text.startswith("knowledge-base/"):
            absolute = self.repo_root / candidate
        else:
            absolute = base / candidate
        try:
            relative = absolute.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            self.issue("path_outside_repository", "Asset file resolves outside the repository.", path=text)
            return None, ""
        return absolute, relative

    def add_file(self, asset_id: str, base: Path, value: Any, role: str) -> str:
        absolute, relative = self.repo_path(base, value)
        if absolute is None or not relative:
            return ""
        row = self.files.get(relative)
        if row is None:
            try:
                exists = absolute.is_file()
            except OSError:
                exists = False
            row = {
                "path": relative,
                "exists": exists,
                "sha256": file_sha256(absolute) if exists else "",
                "size_bytes": absolute.stat().st_size if exists else 0,
                "media_type": mimetypes.guess_type(absolute.name)[0] or "application/octet-stream",
                "roles": [],
                "asset_ids": [],
            }
            self.files[relative] = row
            if not exists:
                self.issue("missing_asset_file", "Indexed asset file does not exist.", path=relative, asset_id=asset_id)
        if role and role not in row["roles"]:
            row["roles"].append(role)
        if asset_id not in row["asset_ids"]:
            row["asset_ids"].append(asset_id)
        asset = self.assets.get(asset_id)
        if asset is not None and relative not in asset["file_paths"]:
            asset["file_paths"].append(relative)
        return relative

    def add_asset(
        self,
        *,
        asset_id: str,
        asset_kind: str,
        project_id: str,
        title: str,
        summary: str,
        lifecycle_state: str,
        verification_state: str = "unknown",
        version: Any = "",
        source_index: str = "",
        generation_provenance: dict[str, Any] | None = None,
        facts: dict[str, Any] | None = None,
        created_at: str = "",
        updated_at: str = "",
        formal_asset_id: str = "",
        formal_member_id: str = "",
    ) -> dict[str, Any]:
        if asset_id in self.assets:
            self.issue("duplicate_asset_id", "Duplicate asset id was ignored.", asset_id=asset_id)
            return self.assets[asset_id]
        row = {
            "asset_id": asset_id,
            "asset_kind": asset_kind,
            "project_id": project_id,
            "formal_asset_id": formal_asset_id,
            "formal_member_id": formal_member_id,
            "title": title or asset_id,
            "summary": summary,
            "lifecycle_state": lifecycle_state or "unknown",
            "verification_state": verification_state or "unknown",
            "version": version,
            "primary_path": "",
            "file_paths": [],
            "source_index": source_index,
            "generation_provenance": generation_provenance or {},
            "facts": facts or {},
            "created_at": created_at,
            "updated_at": updated_at,
        }
        self.assets[asset_id] = row
        return row

    def set_primary(self, asset_id: str, base: Path, value: Any, role: str = "primary") -> str:
        relative = self.add_file(asset_id, base, value, role)
        if relative:
            self.assets[asset_id]["primary_path"] = relative
            self.path_to_asset.setdefault(relative, asset_id)
        return relative

    def relate(
        self,
        source_asset_id: str,
        relation: str,
        *,
        target_asset_id: str = "",
        target_path: str = "",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if not source_asset_id or (not target_asset_id and not target_path):
            return
        row = {
            "source_asset_id": source_asset_id,
            "relation": relation,
            "target_asset_id": target_asset_id,
            "target_path": target_path.replace("\\", "/"),
            "attributes": attributes or {},
        }
        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if all(json.dumps(item, ensure_ascii=False, sort_keys=True) != key for item in self.relationships):
            self.relationships.append(row)

    def add_project(self, project_root: Path) -> None:
        config = read_json(project_root / "project_config.json", {})
        project_id = clean_text(config.get("project_id")) or project_root.name
        project_asset_id = f"{project_id}:project"
        formal_index = project_root / PACKAGE_ROOT_NAME / "index.json"
        source_index = (
            self._repo_relative(formal_index)
            if formal_index.is_file()
            else self._repo_relative(project_root / "project_config.json")
        )
        self.add_asset(
            asset_id=project_asset_id,
            asset_kind="project",
            project_id=project_id,
            title=clean_text(config.get("display_name")) or project_id,
            summary=clean_text(dict_value(config.get("business_scope")).get("summary")),
            lifecycle_state="active",
            source_index=source_index,
            facts={
                "sql_dialect": clean_text(config.get("sql_dialect")),
                "query_engine": clean_text(config.get("query_engine")),
                "table_naming_profile": clean_text(config.get("table_naming_profile")),
            },
            updated_at=clean_text(config.get("updated_at")),
        )
        self.set_primary(project_asset_id, project_root, "project_config.json")
        for value, role in (
            ("manifest.json", "manifest"),
            (f"{PACKAGE_ROOT_NAME}/index.json", "formal_asset_repository_index"),
            ("context/project_brief.md", "project_context"),
            ("index.md", "project_index"),
        ):
            if (project_root / value).is_file():
                self.add_file(project_asset_id, project_root, value, role)

        self.add_rules(project_root, project_id)
        self.add_formal_asset_packages(project_root, project_id, project_asset_id)
        self.add_knowledge_bindings(project_root, project_id)
        self.add_source_catalog(project_root, project_id)
        self.add_project_read_models(project_root, project_id)

    def _repo_relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return ""

    def validate_document_links(self, path: Path, relative: str, asset_id: str, text: str) -> int:
        link_count = 0
        for target in markdown_link_targets(text):
            split = urlsplit(target)
            if split.scheme in {"http", "https", "mailto"} or target.startswith("#"):
                continue
            link_count += 1
            path_text = split.path.replace("\\", "/")
            if not path_text:
                continue
            if Path(path_text).is_absolute() or re.match(r"^[A-Za-z]:", path_text):
                self.issue(
                    "documentation_absolute_link",
                    "Documentation links must stay repository-relative.",
                    path=f"{relative} -> {target}",
                    asset_id=asset_id,
                )
                continue
            candidate = (path.parent / path_text).resolve()
            try:
                candidate.relative_to(self.repo_root)
            except ValueError:
                self.issue(
                    "documentation_link_outside_repository",
                    "Documentation link resolves outside the repository.",
                    path=f"{relative} -> {target}",
                    asset_id=asset_id,
                )
                continue
            if not candidate.exists():
                self.issue(
                    "documentation_broken_link",
                    "Documentation link target does not exist.",
                    path=f"{relative} -> {target}",
                    asset_id=asset_id,
                )
        return link_count

    def add_platform_documentation(self) -> None:
        docs_root = self.repo_root / "docs"
        if not docs_root.is_dir():
            return
        for relative in REQUIRED_DOCUMENTATION:
            if not (self.repo_root / relative).is_file():
                self.issue(
                    "required_documentation_missing",
                    "Required platform or consumer documentation is missing.",
                    path=relative,
                )

        paths: list[Path] = []
        document_asset_ids: list[str] = []
        root_readme = self.repo_root / "README.md"
        if root_readme.is_file():
            paths.append(root_readme)
        paths.extend(sorted(path for path in docs_root.rglob("*.md") if path.is_file()))
        index_asset_id = "GLOBAL:documentation:docs-readme"
        consumer_manual_id = "GLOBAL:documentation:docs-readonly-asset-consumer-guide"
        for path in paths:
            relative = self._repo_relative(path)
            asset_id = f"GLOBAL:documentation:{path_token(Path(relative).with_suffix('').as_posix())}"
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                self.issue(
                    "documentation_unreadable",
                    "Documentation file could not be read as UTF-8.",
                    path=relative,
                    asset_id=asset_id,
                )
                text = ""
            title, summary = markdown_title_summary(path, text)
            profile = documentation_profile(relative)
            version_match = SKILL_VERSION_RE.search(text)
            profile.update(
                {
                    "declared_skill_version": version_match.group(1) if version_match else "",
                    "link_count": self.validate_document_links(path, relative, asset_id, text),
                }
            )
            self.add_asset(
                asset_id=asset_id,
                asset_kind="documentation",
                project_id="GLOBAL",
                title=title,
                summary=summary,
                lifecycle_state="current",
                verification_state="documented",
                source_index="docs/README.md",
                facts=profile,
            )
            self.set_primary(asset_id, self.repo_root, relative, "documentation")
            document_asset_ids.append(asset_id)

        if index_asset_id in self.assets:
            for asset_id in document_asset_ids:
                if asset_id != index_asset_id:
                    self.relate(index_asset_id, "documents", target_asset_id=asset_id)

        for relative, token, title, contract_kind, contract_version in CONSUMER_CONTRACTS:
            path = self.repo_root / relative
            asset_id = f"GLOBAL:consumer_contract:{token}"
            if not path.is_file():
                self.issue(
                    "consumer_contract_missing",
                    "Required external-consumer contract file is missing.",
                    path=relative,
                    asset_id=asset_id,
                )
                continue
            self.add_asset(
                asset_id=asset_id,
                asset_kind="consumer_contract",
                project_id="GLOBAL",
                title=title,
                summary="供外部只读工具复制、校验和解释 SQL 资产目录。",
                lifecycle_state="current",
                verification_state="contract_defined",
                source_index="docs/READONLY_ASSET_CONSUMER_GUIDE.md",
                facts={
                    "contract_kind": contract_kind,
                    "contract_version": contract_version,
                    "audience": "external_consumer",
                    "consumer_scope": "external_core",
                },
            )
            self.set_primary(asset_id, self.repo_root, relative, "consumer_contract")
            if consumer_manual_id in self.assets:
                self.relate(consumer_manual_id, "references_contract", target_asset_id=asset_id)

    def add_formal_asset_packages(
        self,
        project_root: Path,
        project_id: str,
        project_asset_id: str,
    ) -> None:
        formal_root = project_root / PACKAGE_ROOT_NAME
        if not formal_root.is_dir():
            return
        for manifest_path in sorted(formal_root.glob(f"*/{PACKAGE_MANIFEST_NAME}")):
            manifest = read_json(manifest_path, {})
            manifest_relative = self._repo_relative(manifest_path)
            if manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
                self.issue(
                    "unsupported_formal_asset_package",
                    f"Package manifest must use {PACKAGE_SCHEMA_VERSION}.",
                    path=manifest_relative,
                )
                continue
            manifest_project_id = clean_text(manifest.get("project_id"))
            package_id = clean_text(manifest.get("package_id"))
            if not manifest_project_id or not package_id:
                self.issue(
                    "invalid_formal_asset_package_identity",
                    "Package manifest requires project_id and package_id.",
                    path=manifest_relative,
                )
                continue
            if manifest_project_id != project_id:
                self.issue(
                    "formal_asset_project_mismatch",
                    "Package project_id does not match its project configuration.",
                    path=manifest_relative,
                )
            package_root = manifest_path.parent.resolve()
            actual_directory = package_root.relative_to(project_root.resolve()).as_posix()
            declared_directory = clean_text(manifest.get("directory"))
            if declared_directory != actual_directory:
                self.issue(
                    "formal_asset_directory_mismatch",
                    "Package directory does not match the manifest location.",
                    path=manifest_relative,
                )

            package_id_value = package_asset_id(manifest_project_id, package_id)
            package_facts = {
                "package_id": package_id,
                "slug": clean_text(manifest.get("slug")),
                "revision": int(manifest.get("revision") or 0),
                "directory": actual_directory,
                "member_count": len(list_value(manifest.get("members"))),
                "current": copy.deepcopy(dict_value(manifest.get("current"))),
                "member_roles": [],
            }
            package = self.add_asset(
                asset_id=package_id_value,
                asset_kind=PACKAGE_ASSET_KIND,
                project_id=manifest_project_id,
                formal_asset_id=package_id,
                title=clean_text(manifest.get("title")) or package_id,
                summary=clean_text(manifest.get("summary")),
                lifecycle_state=clean_text(manifest.get("lifecycle_state")) or "current",
                verification_state=clean_text(manifest.get("verification_state")) or "unknown",
                version=int(manifest.get("revision") or 0),
                source_index=manifest_relative,
                generation_provenance=dict_value(manifest.get("generation_provenance")),
                facts=package_facts,
                created_at=clean_text(manifest.get("created_at")),
                updated_at=clean_text(manifest.get("updated_at")),
            )
            self.set_primary(package_id_value, project_root, actual_directory + "/manifest.json", "package_manifest")
            self.relate(project_asset_id, "has_formal_asset_package", target_asset_id=package_id_value)
            self.relate(package_id_value, "belongs_to_project", target_asset_id=project_asset_id)

            latest_receipt = clean_text(manifest.get("latest_receipt"))
            if latest_receipt:
                receipt_absolute, receipt_relative = self.repo_path(project_root, latest_receipt)
                if receipt_absolute is None or not receipt_absolute.resolve().is_relative_to(package_root):
                    self.issue(
                        "formal_asset_receipt_outside_package",
                        "Package receipt must remain inside its package directory.",
                        path=receipt_relative or latest_receipt,
                        asset_id=package_id_value,
                    )
                else:
                    self.add_file(package_id_value, project_root, latest_receipt, "package_receipt")

            current_ids = package_current_member_ids(manifest)
            member_assets: dict[str, str] = {}
            member_documents: list[tuple[str, dict[str, Any]]] = []
            role_counts: Counter[str] = Counter()
            for member in list_value(manifest.get("members")):
                if not isinstance(member, dict):
                    self.issue(
                        "invalid_formal_asset_member",
                        "Package members must be objects.",
                        path=manifest_relative,
                        asset_id=package_id_value,
                    )
                    continue
                member_id = clean_text(member.get("member_id"))
                role = clean_text(member.get("role")).lower()
                member_path = clean_text(member.get("path"))
                if not member_id or not role or not member_path:
                    self.issue(
                        "invalid_formal_asset_member",
                        "Package member requires member_id, role, and path.",
                        path=manifest_relative,
                        asset_id=package_id_value,
                    )
                    continue
                if member_id in member_assets:
                    self.issue(
                        "duplicate_formal_asset_member_id",
                        "Package member_id values must be unique.",
                        path=manifest_relative,
                        asset_id=package_id_value,
                    )
                    continue
                absolute, relative = self.repo_path(project_root, member_path)
                if absolute is None or not relative:
                    continue
                try:
                    absolute.resolve().relative_to(package_root)
                except ValueError:
                    self.issue(
                        "formal_asset_member_outside_package",
                        "Package members must be stored inside their package directory.",
                        path=relative,
                        asset_id=package_id_value,
                    )
                    continue

                document = read_json(absolute, {}) if absolute.suffix.lower() == ".json" else {}
                semantic_facts = package_semantic_facts(document)
                member_facts = {
                    "package_id": package_id,
                    "member_id": member_id,
                    "role": role,
                    "is_current": member_id in current_ids,
                    **dict_value(member.get("facts")),
                }
                merge_nonempty_facts(member_facts, semantic_facts)
                member_kind = package_member_kind(role)
                if member_kind in {
                    "result",
                    "analysis_workbook",
                    "comparison_workbook",
                    "visualization",
                    "export",
                    "derived_output",
                }:
                    output_contract = {**member, "path": relative}
                    member_facts.update(derived_output_consumer_facts(member_kind, output_contract))
                execution_route = dict_value(document.get("execution_route"))
                if member_kind in {"query", "dashboard", "validation"}:
                    member_facts["execution_delivery"] = build_execution_delivery(
                        package_member_asset_id(manifest_project_id, package_id, member_id),
                        execution_route,
                    )

                member_asset_id = package_member_asset_id(manifest_project_id, package_id, member_id)
                member_assets[member_id] = member_asset_id
                role_counts[role] += 1
                self.add_asset(
                    asset_id=member_asset_id,
                    asset_kind=member_kind,
                    project_id=manifest_project_id,
                    formal_asset_id=package_id,
                    formal_member_id=member_id,
                    title=(
                        clean_text(member.get("title"))
                        or clean_text(document.get("title"))
                        or f"{role}: {Path(member_path).name}"
                    ),
                    summary=(
                        clean_text(member.get("summary"))
                        or clean_text(dict_value(document.get("repository_summary")).get("purpose"))
                    ),
                    lifecycle_state=clean_text(member.get("lifecycle_state")) or "current",
                    verification_state=(
                        clean_text(member.get("verification_state"))
                        or clean_text(document.get("verification_status"))
                        or "unknown"
                    ),
                    version=member.get("version", document.get("version", "")),
                    source_index=manifest_relative,
                    generation_provenance=(
                        dict_value(document.get("generation_provenance"))
                        or dict_value(member.get("generation_provenance"))
                    ),
                    facts=member_facts,
                    created_at=clean_text(member.get("created_at")),
                    updated_at=clean_text(member.get("updated_at")),
                )
                stored_path = self.set_primary(member_asset_id, project_root, member_path, role)
                file_row = self.files.get(stored_path, {})
                expected_hash = clean_text(member.get("sha256"))
                if expected_hash and clean_text(file_row.get("sha256")) != expected_hash:
                    self.issue(
                        "formal_asset_member_hash_mismatch",
                        "Package member hash does not match its manifest.",
                        path=stored_path,
                        asset_id=member_asset_id,
                    )
                expected_size = member.get("size_bytes")
                if isinstance(expected_size, int) and int(file_row.get("size_bytes") or 0) != expected_size:
                    self.issue(
                        "formal_asset_member_size_mismatch",
                        "Package member size does not match its manifest.",
                        path=stored_path,
                        asset_id=member_asset_id,
                    )
                self.relate(package_id_value, "has_member", target_asset_id=member_asset_id, attributes={"role": role})
                self.relate(member_asset_id, "member_of_package", target_asset_id=package_id_value, attributes={"role": role})
                role_relation = PACKAGE_ROLE_RELATIONS.get(role)
                if role_relation:
                    self.relate(package_id_value, role_relation, target_asset_id=member_asset_id)
                if member_id in current_ids:
                    self.relate(package_id_value, "has_current_member", target_asset_id=member_asset_id, attributes={"role": role})
                if document:
                    member_documents.append((role, document))

            package_facts["member_roles"] = dict(sorted(role_counts.items()))
            unknown_current = sorted(current_ids - set(member_assets))
            if unknown_current:
                self.issue(
                    "formal_asset_current_member_missing",
                    "Package current pointers reference unknown member ids: " + ", ".join(unknown_current),
                    path=manifest_relative,
                    asset_id=package_id_value,
                )

            preferred_documents = [
                document
                for role, document in member_documents
                if role in {"query_meta", "query_spec"}
            ] or [document for _, document in member_documents]
            for document in preferred_documents:
                merge_nonempty_facts(package_facts, package_semantic_facts(document))
                self.add_rule_relations(
                    manifest_project_id,
                    package_id_value,
                    dict_value(document.get("repository_summary")),
                )
                self.add_knowledge_relations(package_id_value, document)
            package["summary"] = package["summary"] or clean_text(package_facts.get("purpose"))

            for edge in list_value(manifest.get("lineage")):
                if not isinstance(edge, dict):
                    continue
                source_member = clean_text(edge.get("from_member_id"))
                target_member = clean_text(edge.get("to_member_id"))
                relation = clean_text(edge.get("relation"))
                if source_member not in member_assets or target_member not in member_assets or not relation:
                    self.issue(
                        "formal_asset_lineage_member_missing",
                        "Package lineage must reference two known members.",
                        path=manifest_relative,
                        asset_id=package_id_value,
                    )
                    continue
                attributes = {"formal_asset_id": package_id}
                if clean_text(edge.get("note")):
                    attributes["note"] = clean_text(edge.get("note"))
                self.relate(
                    member_assets[source_member],
                    relation,
                    target_asset_id=member_assets[target_member],
                    attributes=attributes,
                )

    def _add_legacy_formal_assets(self, project_root: Path, project_id: str) -> None:
        """Migration-only reader retained for callers that explicitly invoke it."""
        manifest_path = project_root / "manifest.json"
        manifest = read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            return
        source_index = self._repo_relative(manifest_path)
        for artifact in list_value(manifest.get("artifacts")):
            if not isinstance(artifact, dict):
                continue
            raw_kind = clean_text(artifact.get("kind")).upper()
            kind = FORMAL_KIND_MAP.get(raw_kind, raw_kind.lower() or "formal_asset")
            slug = safe_token(artifact.get("slug"), Path(clean_text(artifact.get("path"))).parent.name)
            version = int(artifact.get("version") or 0)
            asset_id = f"{project_id}:{kind}:{slug}:v{version:03d}"
            spec = read_json(project_root / clean_text(artifact.get("spec_path")), {})
            meta_path_text = clean_text(artifact.get("meta_path"))
            if not meta_path_text and clean_text(artifact.get("path")).endswith(".sql"):
                meta_path_text = clean_text(artifact.get("path"))[:-4] + ".meta.json"
            meta = read_json(project_root / meta_path_text, {})
            execution_route = (
                dict_value(artifact.get("execution_route"))
                or dict_value(spec.get("execution_route"))
                or dict_value(meta.get("execution_route"))
            )
            provenance = dict_value(spec.get("generation_provenance")) or dict_value(meta.get("generation_provenance")) or dict_value(artifact.get("generation_provenance"))
            summary = clean_text(artifact.get("natural_language_intent")) or clean_text(artifact.get("content_summary"))
            repository_summary = dict_value(spec.get("repository_summary"))
            summary = summary or clean_text(repository_summary.get("purpose"))
            facts = {
                "slug": clean_text(artifact.get("slug")),
                "status": clean_text(artifact.get("status")),
                "artifact_state": clean_text(artifact.get("artifact_state")),
                "change_type": clean_text(artifact.get("change_type")),
                "business_category": clean_text(artifact.get("business_category")),
                "business_topic": clean_text(repository_summary.get("business_topic")),
                "business_question": clean_text(repository_summary.get("business_question")),
                "analysis_type": clean_text(artifact.get("analysis_type")),
                "metrics": list_value(repository_summary.get("metrics")) or list_value(artifact.get("metrics")),
                "dimensions": list_value(repository_summary.get("dimensions")) or list_value(artifact.get("dimensions")),
                "filters": list_value(repository_summary.get("filters")),
                "source_logs": list_value(repository_summary.get("source_logs")),
                "tables": list_value(artifact.get("tables")),
                "grain": clean_text(artifact.get("grain")),
                "reusable": bool(artifact.get("reusable")),
                "execution_delivery": build_execution_delivery(asset_id, execution_route),
            }
            self.add_asset(
                asset_id=asset_id,
                asset_kind=kind,
                project_id=project_id,
                title=clean_text(artifact.get("title")) or slug,
                summary=summary,
                lifecycle_state=clean_text(artifact.get("artifact_state")) or clean_text(artifact.get("status")),
                verification_state=clean_text(artifact.get("verification_status")) or "unknown",
                version=version,
                source_index=source_index,
                generation_provenance=provenance,
                facts=facts,
                created_at=clean_text(artifact.get("created_at")),
                updated_at=clean_text(artifact.get("updated_at")),
            )
            self.set_primary(asset_id, project_root, artifact.get("path"), "sql")
            self.add_file(asset_id, project_root, artifact.get("spec_path"), "spec")
            self.add_file(asset_id, project_root, meta_path_text, "metadata")
            for value in list_value(artifact.get("supersedes")):
                _, target_path = self.repo_path(project_root, value)
                self.relate(asset_id, "supersedes", target_path=target_path)
            for field, relation in (
                ("replaced_by", "replaced_by"),
                ("linked_query", "derived_from_query"),
                ("linked_validation", "validated_by"),
                ("linked_run", "has_run_evidence"),
            ):
                value = clean_text(artifact.get(field))
                if value:
                    _, target_path = self.repo_path(project_root, value)
                    self.relate(asset_id, relation, target_path=target_path)
            self.add_rule_relations(project_id, asset_id, repository_summary)
            self.add_knowledge_relations(asset_id, spec)
            self.add_knowledge_relations(asset_id, meta)

        for table in list_value(manifest.get("intermediate_tables")):
            if isinstance(table, dict):
                self.add_intermediate_table(project_root, project_id, source_index, table)

        for run in list_value(manifest.get("run_evidence")):
            if isinstance(run, dict):
                self.add_run(project_root, project_id, source_index, run)

    def add_intermediate_table(
        self,
        project_root: Path,
        project_id: str,
        source_index: str,
        table: dict[str, Any],
    ) -> None:
        slug = safe_token(table.get("slug"), Path(clean_text(table.get("path"))).parent.name)
        version = int(table.get("version") or 0)
        asset_id = f"{project_id}:intermediate_table:{slug}:v{version:03d}"
        self.add_asset(
            asset_id=asset_id,
            asset_kind="intermediate_table",
            project_id=project_id,
            title=clean_text(table.get("title")) or slug,
            summary=clean_text(table.get("purpose")),
            lifecycle_state=clean_text(table.get("table_state")) or clean_text(table.get("status")),
            verification_state=clean_text(table.get("availability_status")) or "unknown",
            version=version,
            source_index=source_index,
            generation_provenance=dict_value(table.get("generation_provenance")),
            facts={
                "table_name": clean_text(table.get("table_name")),
                "status": clean_text(table.get("status")),
                "table_type": clean_text(table.get("table_type")),
                "materialization": clean_text(table.get("materialization")),
                "grain": clean_text(table.get("grain")),
                "source_tables": list_value(table.get("source_tables")),
                "downstream_tables": list_value(table.get("downstream_tables")),
                "fallback_required": bool(table.get("fallback_required")),
                "source_contract_mode": clean_text(table.get("source_contract_mode")),
                "execution_delivery": build_execution_delivery(asset_id, {}),
            },
            created_at=clean_text(table.get("created_at")),
            updated_at=clean_text(table.get("updated_at")),
        )
        self.set_primary(asset_id, project_root, table.get("path"), "build_sql")
        path_text = clean_text(table.get("path"))
        if path_text.endswith(".sql"):
            self.add_file(asset_id, project_root, path_text[:-4] + ".meta.json", "metadata")
        for value in list_value(table.get("source_artifacts")):
            _, target_path = self.repo_path(project_root, value)
            self.relate(asset_id, "derived_from", target_path=target_path)
        for value in list_value(table.get("downstream_artifacts")):
            _, target_path = self.repo_path(project_root, value)
            self.relate(asset_id, "used_by", target_path=target_path)
        for concept_key in list_value(table.get("canonical_rule_refs")):
            concept_key = clean_text(concept_key)
            target = self.rule_targets.get((project_id, concept_key), "")
            self.relate(
                asset_id,
                "references_rule",
                target_asset_id=target,
                target_path="" if target else f"rule://{project_id}/{concept_key}",
            )

    def add_run(self, project_root: Path, project_id: str, source_index: str, run: dict[str, Any]) -> None:
        run_id = safe_token(run.get("run_id"), "run")
        asset_id = f"{project_id}:run_evidence:{run_id}"
        self.add_asset(
            asset_id=asset_id,
            asset_kind="run_evidence",
            project_id=project_id,
            title=clean_text(run.get("title")) or run_id,
            summary=clean_text(run.get("result_summary")) or clean_text(run.get("skip_reason")),
            lifecycle_state="recorded",
            verification_state=clean_text(run.get("status")) or "unknown",
            source_index=source_index,
            generation_provenance=dict_value(run.get("generation_provenance")),
            facts={
                "row_count": run.get("row_count"),
                "user_confirmed": bool(run.get("user_confirmed")),
                "result_file_type": clean_text(run.get("result_file_type")),
                "result_evidence_retention": dict_value(run.get("result_evidence_retention")),
                "definition_project": clean_text(run.get("definition_project")),
                "execution_project": clean_text(run.get("execution_project")),
                "delivery_project": clean_text(run.get("delivery_project")),
            },
            created_at=clean_text(run.get("created_at")),
        )
        self.set_primary(asset_id, project_root, run.get("path"), "run_record")
        source_path = clean_text(run.get("source_artifact")) or clean_text(run.get("sql_path"))
        if source_path:
            _, target_path = self.repo_path(project_root, source_path)
            self.relate(asset_id, "evidence_for", target_path=target_path)
        evidence_file = clean_text(run.get("evidence_file"))
        if evidence_file:
            result_id = f"{project_id}:result:run:{run_id}"
            self.add_asset(
                asset_id=result_id,
                asset_kind="result",
                project_id=project_id,
                title=f"{clean_text(run.get('title')) or run_id} 结果文件",
                summary=clean_text(run.get("result_summary")),
                lifecycle_state="attached",
                verification_state=clean_text(run.get("status")) or "unknown",
                source_index=source_index,
                generation_provenance=dict_value(run.get("generation_provenance")),
                facts={
                    "run_id": clean_text(run.get("run_id")),
                    "row_count": run.get("row_count"),
                    "result_file_type": clean_text(run.get("result_file_type")),
                    "checked_metrics": list_value(run.get("checked_metrics")),
                    "checked_dimensions": list_value(run.get("checked_dimensions")),
                    "user_confirmed": bool(run.get("user_confirmed")),
                    "retention": dict_value(run.get("result_evidence_retention")),
                    "consumer_surface": "result_evidence",
                    "workbook_presentation": reusable_workbook_presentation(
                        "result",
                        mimetypes.guess_type(evidence_file)[0] or "application/octet-stream",
                        evidence_file,
                        {},
                    ),
                },
                created_at=clean_text(run.get("created_at")),
            )
            self.set_primary(result_id, project_root, evidence_file, "result_file")
            self.add_file(asset_id, project_root, evidence_file, "result_file")
            self.relate(asset_id, "has_result", target_asset_id=result_id)
            self.relate(result_id, "result_of_run", target_asset_id=asset_id)
            if source_path:
                _, target_path = self.repo_path(project_root, source_path)
                self.relate(result_id, "evidence_for", target_path=target_path)
            for output in list_value(run.get("derived_outputs")):
                if isinstance(output, dict):
                    self.add_run_derived_output(
                        project_root,
                        project_id,
                        source_index,
                        asset_id,
                        result_id,
                        source_path,
                        output,
                    )

    def add_run_derived_output(
        self,
        project_root: Path,
        project_id: str,
        source_index: str,
        run_asset_id: str,
        result_asset_id: str,
        source_path: str,
        output: dict[str, Any],
    ) -> None:
        attachment_id = safe_token(
            output.get("attachment_id"),
            hashlib.sha256(clean_text(output.get("path")).encode()).hexdigest()[:12],
        )
        kind = OUTPUT_KIND_MAP.get(clean_text(output.get("kind")), "derived_output")
        asset_id = f"{run_asset_id}:{kind}:{attachment_id}"
        self.add_asset(
            asset_id=asset_id,
            asset_kind=kind,
            project_id=project_id,
            title=clean_text(output.get("title")) or attachment_id,
            summary=clean_text(output.get("purpose")),
            lifecycle_state="attached",
            verification_state="generated" if output.get("source_kind") == "skill_generated" else "user_result",
            source_index=source_index,
            generation_provenance=dict_value(output.get("generation_provenance")),
            facts={
                "attachment_id": clean_text(output.get("attachment_id")),
                "source_kind": clean_text(output.get("source_kind")),
                "media_type": clean_text(output.get("media_type")),
                "source_sql_fingerprint": clean_text(output.get("source_sql_fingerprint")),
                "source_result_id": clean_text(output.get("source_result_id")),
                "lineage_status": clean_text(output.get("lineage_status")),
                "declared_sha256": clean_text(output.get("sha256")),
                "source_sha256": clean_text(output.get("source_sha256")),
                "original_file_name": clean_text(output.get("original_file_name")),
                "retention": dict_value(output.get("retention")),
                **derived_output_consumer_facts(kind, output),
            },
            created_at=clean_text(output.get("created_at")),
        )
        self.set_primary(asset_id, project_root, output.get("path"), "reusable_output")
        self.relate(run_asset_id, "has_derived_output", target_asset_id=asset_id)
        self.relate(asset_id, "derived_from_result", target_asset_id=result_asset_id)
        self.relate(result_asset_id, "has_visualization", target_asset_id=asset_id)
        if source_path:
            _, target_path = self.repo_path(project_root, source_path)
            self.relate(asset_id, "evidence_for", target_path=target_path)

    def add_rule_relations(self, project_id: str, asset_id: str, summary: dict[str, Any]) -> None:
        rows = list_value(summary.get("applied_criteria")) + list_value(summary.get("canonical_rule_checks"))
        for row in rows:
            if not isinstance(row, dict):
                continue
            concept_key = clean_text(row.get("concept_key"))
            if not concept_key:
                continue
            target = self.rule_targets.get((project_id, concept_key), "")
            self.relate(
                asset_id,
                "references_rule",
                target_asset_id=target,
                target_path="" if target else f"rule://{project_id}/{concept_key}",
                attributes={"result": clean_text(row.get("result")), "rule_id": clean_text(row.get("rule_id"))},
            )

    def iter_knowledge_references(self, value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            dataset_id = clean_text(value.get("dataset_id"))
            version = clean_text(value.get("dataset_version")) or clean_text(value.get("version"))
            if dataset_id and version.startswith("kdv-"):
                yield value
            for nested in value.values():
                yield from self.iter_knowledge_references(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from self.iter_knowledge_references(nested)

    def add_knowledge_relations(self, asset_id: str, value: Any) -> None:
        seen: set[tuple[str, str]] = set()
        for reference in self.iter_knowledge_references(value):
            dataset_id = clean_text(reference.get("dataset_id"))
            version = clean_text(reference.get("dataset_version")) or clean_text(reference.get("version"))
            key = (dataset_id, version)
            if key in seen:
                continue
            seen.add(key)
            target = self.knowledge_targets.get(key, f"global:knowledge_dataset:{safe_token(dataset_id)}:{safe_token(version)}")
            self.relate(
                asset_id,
                "uses_knowledge",
                target_asset_id=target,
                attributes={
                    "projection_id": clean_text(reference.get("projection_id")),
                    "content_hash": clean_text(reference.get("content_hash")),
                },
            )

    def add_rules(self, project_root: Path, project_id: str) -> None:
        store_path = project_root / "rules" / "store.json"
        store = read_json(store_path, {})
        concepts = dict_value(store.get("concepts"))
        if not concepts:
            return
        source_index = self._repo_relative(store_path)
        for concept_key, concept in concepts.items():
            if not isinstance(concept, dict):
                continue
            concept_id = f"{project_id}:rule_concept:{safe_token(concept_key)}"
            current = dict_value(concept.get("current_confirmed"))
            self.add_asset(
                asset_id=concept_id,
                asset_kind="rule_concept",
                project_id=project_id,
                title=clean_text(concept_key),
                summary="",
                lifecycle_state="active" if current else "unconfirmed",
                verification_state="confirmed" if current else "unknown",
                source_index=source_index,
                facts={
                    "concept_key": clean_text(concept_key),
                    "latest_rule_version": concept.get("latest_rule_version"),
                    "current_rule_path": clean_text(current.get("path")),
                },
            )
            self.set_primary(concept_id, project_root, "rules/store.json", "rule_store")
            self.add_file(concept_id, project_root, "rules/activation-index.json", "activation_index")
            for version_row in list_value(concept.get("versions")):
                if not isinstance(version_row, dict):
                    continue
                path_text = clean_text(version_row.get("path"))
                definition = read_json(project_root / path_text, {})
                rule_version = int(version_row.get("rule_version") or definition.get("rule_version") or 0)
                store_version = int(version_row.get("store_version") or 0)
                immutable_version = store_version or int(Path(path_text).stem.lstrip("v") or 0)
                rule_id = f"{project_id}:rule:{safe_token(concept_key)}:v{immutable_version:03d}"
                self.rule_targets[(project_id, clean_text(concept_key))] = (
                    rule_id if path_text == clean_text(current.get("path")) else self.rule_targets.get((project_id, clean_text(concept_key)), "")
                )
                self.add_asset(
                    asset_id=rule_id,
                    asset_kind="rule",
                    project_id=project_id,
                    title=clean_text(definition.get("title")) or clean_text(version_row.get("rule_id")) or clean_text(concept_key),
                    summary=clean_text(definition.get("summary")) or clean_text(definition.get("definition"))[:500],
                    lifecycle_state=clean_text(version_row.get("effective_status")) or clean_text(definition.get("status")),
                    verification_state=clean_text(version_row.get("effective_status")) or clean_text(definition.get("status")),
                    version=immutable_version,
                    source_index=source_index,
                    generation_provenance=dict_value(definition.get("generation_provenance")),
                    facts={
                        "concept_key": clean_text(concept_key),
                        "rule_id": clean_text(version_row.get("rule_id")) or clean_text(definition.get("rule_id")),
                        "store_version": store_version,
                        "rule_version": rule_version,
                        "record_sha256": clean_text(version_row.get("record_sha256")),
                        "declared_file_sha256": clean_text(version_row.get("file_sha256")),
                    },
                    created_at=clean_text(version_row.get("created_at")) or clean_text(definition.get("created_at")),
                    updated_at=clean_text(definition.get("updated_at")),
                )
                self.set_primary(rule_id, project_root, path_text, "rule_definition")
                self.relate(rule_id, "version_of", target_asset_id=concept_id)
                self.add_knowledge_relations(rule_id, definition)
                if path_text == clean_text(current.get("path")):
                    self.relate(concept_id, "current_definition", target_asset_id=rule_id)

    def add_knowledge_bindings(self, project_root: Path, project_id: str) -> None:
        bindings_path = project_root / "knowledge" / "bindings.json"
        payload = read_json(bindings_path, {})
        source_index = self._repo_relative(bindings_path)
        for binding in list_value(payload.get("bindings")):
            if not isinstance(binding, dict):
                continue
            dataset_id = clean_text(binding.get("dataset_id"))
            version = clean_text(binding.get("dataset_version"))
            asset_id = f"{project_id}:knowledge_binding:{safe_token(dataset_id)}:{safe_token(version)}"
            self.add_asset(
                asset_id=asset_id,
                asset_kind="knowledge_binding",
                project_id=project_id,
                title=f"{dataset_id} {version}",
                summary=clean_text(binding.get("activation_reason")),
                lifecycle_state=clean_text(binding.get("state")) or "unknown",
                verification_state="bound",
                version=version,
                source_index=source_index,
                generation_provenance=dict_value(binding.get("generation_provenance")),
                facts={
                    "dataset_id": dataset_id,
                    "dataset_version": version,
                    "content_hash": clean_text(binding.get("content_hash")),
                    "contract_sha256": clean_text(binding.get("contract_sha256")),
                },
                created_at=clean_text(binding.get("activated_at")),
            )
            self.set_primary(asset_id, project_root, "knowledge/bindings.json", "binding_index")
            target = self.knowledge_targets.get((dataset_id, version), f"global:knowledge_dataset:{safe_token(dataset_id)}:{safe_token(version)}")
            self.relate(asset_id, "binds_knowledge", target_asset_id=target)
            self.relate(f"{project_id}:project", "has_knowledge_binding", target_asset_id=asset_id)

    def add_source_catalog(self, project_root: Path, project_id: str) -> None:
        catalog_path = project_root / "sources" / "xml_catalog.json"
        catalog = read_json(catalog_path, {})
        if not isinstance(catalog, dict) or not catalog:
            return
        asset_id = f"{project_id}:source_catalog:tlog"
        self.add_asset(
            asset_id=asset_id,
            asset_kind="source_catalog",
            project_id=project_id,
            title=f"{project_id} TLOG 字段目录",
            summary=f"{catalog.get('log_count', 0)} 个原始日志定义",
            lifecycle_state="current",
            verification_state="source_defined",
            source_index=self._repo_relative(catalog_path),
            facts={"log_count": catalog.get("log_count"), "source_file": clean_text(catalog.get("source_file"))},
            updated_at=clean_text(catalog.get("generated_at")),
        )
        self.set_primary(asset_id, project_root, "sources/xml_catalog.json", "xml_catalog")
        source_file = clean_text(catalog.get("source_file"))
        if source_file:
            self.add_file(asset_id, project_root, source_file, "xml_source")
        self.relate(f"{project_id}:project", "has_source_catalog", target_asset_id=asset_id)

    def add_project_read_models(self, project_root: Path, project_id: str) -> None:
        candidates = [
            ("reviews/sql_repository.json", "repository_read_model", "SQL 仓库读取模型", ["reviews/sql_repository.html"]),
            (
                "reviews/dashboard_review.json",
                "dashboard_review",
                "看板审查读取模型",
                ["reviews/dashboard_review.html", "reviews/dashboard_review_state.json"],
            ),
        ]
        for relative, kind, title, companions in candidates:
            path = project_root / relative
            if not path.is_file():
                continue
            asset_id = f"{project_id}:{kind}"
            payload = read_json(path, {})
            self.add_asset(
                asset_id=asset_id,
                asset_kind=kind,
                project_id=project_id,
                title=title,
                summary="供只读工具直接消费的生成视图",
                lifecycle_state="generated",
                verification_state="read_model",
                source_index=self._repo_relative(path),
                facts={"schema_version": clean_text(payload.get("schema") or payload.get("review_contract_version"))},
                updated_at=clean_text(payload.get("generated_at")),
            )
            self.set_primary(asset_id, project_root, relative, "read_model")
            for companion in companions:
                if (project_root / companion).is_file():
                    self.add_file(asset_id, project_root, companion, "read_model_companion")

    def add_knowledge_catalog(self) -> None:
        catalog_path = self.repo_root / "knowledge-base" / "catalog.json"
        catalog = read_json(catalog_path, {})
        if not isinstance(catalog, dict):
            return
        source_index = self._repo_relative(catalog_path)
        for dataset in list_value(catalog.get("datasets")):
            if not isinstance(dataset, dict):
                continue
            dataset_id = clean_text(dataset.get("dataset_id"))
            for version_row in list_value(dataset.get("versions")):
                if not isinstance(version_row, dict):
                    continue
                version = clean_text(version_row.get("version"))
                asset_id = f"global:knowledge_dataset:{safe_token(dataset_id)}:{safe_token(version)}"
                self.knowledge_targets[(dataset_id, version)] = asset_id
                manifest_path = clean_text(version_row.get("manifest_path"))
                manifest = read_json(self.repo_root / manifest_path, {})
                self.add_asset(
                    asset_id=asset_id,
                    asset_kind="knowledge_dataset",
                    project_id="GLOBAL",
                    title=clean_text(dataset.get("display_name")) or dataset_id,
                    summary=clean_text(manifest.get("description")),
                    lifecycle_state="versioned",
                    verification_state=clean_text(manifest.get("build_status")) or "registered",
                    version=version,
                    source_index=source_index,
                    generation_provenance=dict_value(manifest.get("generation_provenance")) or dict_value(catalog.get("generation_provenance")),
                    facts={
                        "dataset_id": dataset_id,
                        "dataset_version": version,
                        "content_hash": clean_text(version_row.get("content_hash")),
                        "source_snapshot_id": clean_text(version_row.get("source_snapshot_id")),
                    },
                    created_at=clean_text(version_row.get("registered_at")),
                )
                primary = self.set_primary(asset_id, self.repo_root, manifest_path, "dataset_manifest")
                if primary:
                    dataset_dir = (self.repo_root / primary).parent
                    for child in sorted(dataset_dir.rglob("*")):
                        if child.is_file():
                            self.add_file(asset_id, self.repo_root, self._repo_relative(child), "dataset_file")
                source_snapshot = manifest.get("source_snapshot")
                if isinstance(source_snapshot, dict):
                    for key in ("path", "snapshot_path", "file_path"):
                        if source_snapshot.get(key):
                            self.add_file(asset_id, self.repo_root, source_snapshot.get(key), "source_snapshot")

    def add_cross_project_read_models(self) -> None:
        candidates = [
            (
                self.projects_root / "_rule_review" / "rule_dictionary.json",
                "GLOBAL:rule_dictionary",
                "rule_dictionary",
                "跨项目口径字典读取模型",
                [self.projects_root / "_rule_review" / "rule_dictionary.html"],
            ),
            (
                self.projects_root / "_rule_review" / "rule_review_state.json",
                "GLOBAL:rule_review",
                "rule_review",
                "跨项目口径审查读取模型",
                [self.projects_root / "_rule_review" / "rule_review.html"],
            ),
            (
                self.projects_root / "_rule_review" / "rule_concepts.json",
                "GLOBAL:rule_concept_registry",
                "rule_concept_registry",
                "跨项目口径概念注册表",
                [],
            ),
        ]
        for path, asset_id, kind, title, companions in candidates:
            if not path.is_file():
                continue
            payload = read_json(path, {})
            self.add_asset(
                asset_id=asset_id,
                asset_kind=kind,
                project_id="GLOBAL",
                title=title,
                summary="供只读工具直接消费的跨项目生成视图",
                lifecycle_state="generated",
                verification_state=clean_text(payload.get("status")) or "read_model",
                source_index=self._repo_relative(path),
                facts={"schema_version": clean_text(payload.get("schema_version"))},
                updated_at=clean_text(payload.get("generated_at")),
            )
            self.set_primary(asset_id, self.repo_root, self._repo_relative(path), "read_model")
            for companion in companions:
                if companion.is_file():
                    self.add_file(asset_id, self.repo_root, self._repo_relative(companion), "read_model_companion")

    def add_sql_reviews(self) -> None:
        review_root = self.projects_root / "_review_inbox"
        if not review_root.is_dir():
            return
        for path in sorted(review_root.rglob("sql_review.json")):
            payload = read_json(path, {})
            project_dir = path.relative_to(review_root).parts[0] if path.relative_to(review_root).parts else "UNKNOWN"
            project_id = clean_text(payload.get("project")) or project_dir
            project_id = project_id.replace("-", "_") if project_id.startswith("EXAMPLE-") else project_id
            batch_rel = path.parent.relative_to(review_root).as_posix()
            digest = hashlib.sha256(batch_rel.encode("utf-8")).hexdigest()[:12]
            asset_id = f"{project_id}:sql_review:{digest}"
            summary = dict_value(payload.get("summary"))
            self.add_asset(
                asset_id=asset_id,
                asset_kind="sql_review",
                project_id=project_id,
                title=path.parent.name,
                summary=clean_text(summary.get("conclusion")) or f"{len(list_value(payload.get('items')))} 条 SQL 审查记录",
                lifecycle_state="generated",
                verification_state=clean_text(payload.get("review_output_model")) or clean_text(payload.get("schema_version")) or "unknown",
                source_index=self._repo_relative(path),
                facts={
                    "schema_version": clean_text(payload.get("schema_version")),
                    "item_count": len(list_value(payload.get("items"))),
                    "batch_path": self._repo_relative(path.parent),
                },
                updated_at=clean_text(payload.get("generated_at")),
            )
            self.set_primary(asset_id, self.repo_root, self._repo_relative(path), "review_json")
            for name in (
                "sql_review.html",
                "sql_review_product.md",
                "sql_review_code.md",
                "sql_review_summary.md",
            ):
                candidate = path.parent / name
                if candidate.is_file():
                    self.add_file(asset_id, self.repo_root, self._repo_relative(candidate), "review_output")

    def resolve_relationships(self) -> None:
        for relation in self.relationships:
            if relation.get("target_asset_id"):
                continue
            target_path = clean_text(relation.get("target_path"))
            target = self.path_to_asset.get(target_path, "")
            if target:
                relation["target_asset_id"] = target
        for file_row in self.files.values():
            file_row["roles"] = sorted(file_row["roles"])
            file_row["asset_ids"] = sorted(file_row["asset_ids"])
        for asset in self.assets.values():
            asset["file_paths"] = sorted(asset["file_paths"])

    def build(self) -> dict[str, Any]:
        self.add_platform_documentation()
        self.add_knowledge_catalog()
        project_roots = [
            path
            for path in sorted(self.projects_root.iterdir())
            if path.is_dir() and not path.name.startswith("_") and (path / "project_config.json").is_file()
        ]
        for project_root in project_roots:
            self.add_project(project_root)
        self.add_cross_project_read_models()
        self.add_sql_reviews()
        self.resolve_relationships()
        for problem in finalize_execution_deliveries(self.assets):
            self.issue(
                clean_text(problem.get("code")),
                clean_text(problem.get("message")),
                asset_id=clean_text((problem.get("asset_ids") or [""])[0]),
            )
        assets = sorted(self.assets.values(), key=lambda item: item["asset_id"])
        files = sorted(self.files.values(), key=lambda item: item["path"])
        relationships = sorted(
            self.relationships,
            key=lambda item: (
                item["source_asset_id"],
                item["relation"],
                item.get("target_asset_id", ""),
                item.get("target_path", ""),
            ),
        )
        kind_counts = Counter(item["asset_kind"] for item in assets)
        state_counts = Counter(item["lifecycle_state"] for item in assets)
        project_counts = Counter(item["project_id"] for item in assets)
        generated_at = now_iso()
        return {
            "schema_version": SCHEMA_VERSION,
            "source_model": "formal_asset_packages_v1",
            "generated_at": generated_at,
            "source_root": ".",
            "source_control": git_snapshot(self.repo_root),
            "generation_provenance": build_generation_provenance(
                generator_script="asset_catalog.py",
                workflow="package_backed_shared_asset_catalog",
                artifact_kind="ASSET_CATALOG",
                generated_at=generated_at,
                source="formal_asset_package_manifests_and_shared_governance",
            ),
            "visibility_policy": {
                "mode": "package_backed_shared_assets",
                "status_is_descriptive_only": True,
                "hidden_lifecycle_states": [],
                "local_workspace_included": False,
                "excluded_local_surfaces": ["query_workspace", "promotion_ledger"],
                "excluded_legacy_surfaces": [
                    "archive",
                    "query_sql",
                    "dashboard_sql",
                    "validations",
                    "runs",
                    "loose_formal_files",
                ],
                "excluded_non_assets": ["credentials", "cache", "locks", "build_temporary_files"],
            },
            "summary": {
                "asset_count": len(assets),
                "formal_asset_package_count": sum(
                    1 for item in assets if item.get("asset_kind") == PACKAGE_ASSET_KIND
                ),
                "formal_asset_member_count": sum(
                    1 for item in assets if clean_text(item.get("formal_member_id"))
                ),
                "file_count": len(files),
                "relationship_count": len(relationships),
                "issue_count": len(self.issues),
                "assets_by_kind": dict(sorted(kind_counts.items())),
                "assets_by_state": dict(sorted(state_counts.items())),
                "assets_by_project": dict(sorted(project_counts.items())),
            },
            "assets": assets,
            "files": files,
            "relationships": relationships,
            "issues": sorted(self.issues, key=lambda item: (item["code"], item["path"], item["asset_id"])),
        }


def validate_catalog(payload: dict[str, Any], repo_root: Path | None = None) -> list[str]:
    problems: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    if payload.get("source_model") != "formal_asset_packages_v1":
        problems.append("source_model must be formal_asset_packages_v1")
    assets = payload.get("assets")
    files = payload.get("files")
    relationships = payload.get("relationships")
    if not isinstance(assets, list):
        problems.append("assets must be an array")
        assets = []
    if not isinstance(files, list):
        problems.append("files must be an array")
        files = []
    if not isinstance(relationships, list):
        problems.append("relationships must be an array")
        relationships = []
    ids = [clean_text(item.get("asset_id")) for item in assets if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        problems.append("asset_id values must be unique")
    paths = [clean_text(item.get("path")) for item in files if isinstance(item, dict)]
    if len(paths) != len(set(paths)):
        problems.append("file paths must be unique")
    local_assets = [
        clean_text(item.get("asset_id"))
        for item in assets
        if isinstance(item, dict)
        and (
            clean_text(item.get("asset_kind")) == "temporary_query"
            or ":temporary_query:" in clean_text(item.get("asset_id"))
            or "promotion_ledger" in clean_text(item.get("asset_kind")).lower()
            or ":promotion_ledger:" in clean_text(item.get("asset_id")).lower()
        )
    ]
    if local_assets:
        problems.append("catalog must not include query workspace or promotion ledger assets")
    local_files = [path for path in paths if excluded_catalog_path(path)]
    if local_files:
        problems.append("catalog must not include local, legacy, archive, or loose formal files")
    local_relations = [
        item
        for item in relationships
        if isinstance(item, dict)
        and excluded_catalog_path(clean_text(item.get("target_path")))
    ]
    if local_relations:
        problems.append("catalog must not include relationships to excluded asset paths")
    for item in assets:
        if not isinstance(item, dict):
            continue
        kind = clean_text(item.get("asset_kind"))
        formal_asset_id = clean_text(item.get("formal_asset_id"))
        formal_member_id = clean_text(item.get("formal_member_id"))
        if kind == PACKAGE_ASSET_KIND and not formal_asset_id:
            problems.append(f"formal package asset is missing formal_asset_id: {item.get('asset_id')}")
        if formal_member_id and not formal_asset_id:
            problems.append(f"formal member is missing formal_asset_id: {item.get('asset_id')}")
        normalized_source = "/" + clean_text(item.get("source_index")).replace("\\", "/")
        if kind == PACKAGE_ASSET_KIND or formal_member_id:
            if "/formal_assets/" not in normalized_source or not normalized_source.endswith("/manifest.json"):
                problems.append(f"formal asset source_index must be a package manifest: {item.get('asset_id')}")
        if formal_member_id:
            normalized_primary = "/" + clean_text(item.get("primary_path")).replace("\\", "/")
            if "/formal_assets/" not in normalized_primary or "/members/" not in normalized_primary:
                problems.append(f"formal member primary_path must remain inside package members: {item.get('asset_id')}")
    for path in paths:
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            problems.append(f"file path must be repository-relative: {path!r}")
    if repo_root:
        repo_root = repo_root.resolve()
        for row in files:
            if not isinstance(row, dict) or not row.get("exists"):
                continue
            path = repo_root / clean_text(row.get("path"))
            if not path.is_file():
                problems.append(f"catalog file is missing: {row.get('path')}")
            elif clean_text(row.get("sha256")) != file_sha256(path):
                problems.append(f"catalog hash changed: {row.get('path')}")
    return problems


def write_catalog(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def serialized_payload(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def formal_asset_source_snapshot(projects_root: Path) -> dict[str, Any]:
    """Fingerprint the authoritative inputs used to build shared asset projections."""

    projects_root = projects_root.resolve()
    repo_root = projects_root.parent.resolve()
    source_files: dict[str, dict[str, Any]] = {}

    def add(path: Path, role: str) -> None:
        if not path.is_file():
            return
        try:
            relative = path.resolve().relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Formal asset source escaped the repository: {path}") from exc
        source_files[relative] = {
            "path": relative,
            "role": role,
            "sha256": file_sha256(path),
            "size_bytes": path.stat().st_size,
        }

    for project_root in sorted(projects_root.iterdir(), key=lambda path: path.name):
        formal_root = project_root / PACKAGE_ROOT_NAME
        if not project_root.is_dir() or project_root.name.startswith("_") or not formal_root.is_dir():
            continue
        add(formal_root / "index.json", "formal_asset_repository_index")
        for manifest_path in sorted(formal_root.glob("FA-*/manifest.json")):
            add(manifest_path, "formal_asset_package_manifest")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            latest_receipt = clean_text(manifest.get("latest_receipt"))
            if latest_receipt:
                add(project_root / Path(latest_receipt), "formal_asset_package_receipt")

    inputs = [source_files[path] for path in sorted(source_files)]
    digest_payload = json.dumps(
        inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": "formal_asset_read_model_source_v1",
        "algorithm": "sha256",
        "input_scope": [
            "sql-projects/<PROJECT>/formal_assets/index.json",
            "sql-projects/<PROJECT>/formal_assets/FA-*/manifest.json",
            "latest receipt referenced by each Package manifest",
        ],
        "input_order": "repository_relative_path_ordinal",
        "input_count": len(inputs),
        "digest": hashlib.sha256(digest_payload).hexdigest(),
    }


def refresh_shared_asset_read_models(
    projects_root: Path,
    *,
    catalog_path: Path | None = None,
    organization_path: Path | None = None,
    registry_path: Path | None = None,
    taxonomy_path: Path | None = None,
) -> dict[str, Any]:
    """Refresh Catalog -> Organization -> AG Registry from package facts in one call."""

    from asset_group_registry import (  # noqa: PLC0415
        build_registry,
        read_json as read_registry,
        validate_registry,
    )
    from asset_organization import (  # noqa: PLC0415
        DEFAULT_TAXONOMY,
        build_payload,
        read_json as read_organization,
        validate_organization,
        validate_taxonomy,
    )

    projects_root = projects_root.resolve()
    output_root = projects_root / DEFAULT_OUTPUT.parent
    catalog_path = (catalog_path or output_root / DEFAULT_OUTPUT.name).resolve()
    organization_path = (organization_path or output_root / "asset_organization.json").resolve()
    registry_path = (registry_path or output_root / "asset_group_registry.json").resolve()
    taxonomy_path = (taxonomy_path or DEFAULT_TAXONOMY).resolve()

    source_snapshot = formal_asset_source_snapshot(projects_root)
    catalog = CatalogBuilder(projects_root).build()
    catalog_bytes = serialized_payload(catalog)
    catalog_fingerprint = hashlib.sha256(catalog_bytes).hexdigest()
    taxonomy = read_organization(taxonomy_path, {})
    taxonomy_problems = validate_taxonomy(taxonomy)
    if taxonomy_problems:
        raise ValueError("; ".join(taxonomy_problems))
    existing_organization = read_organization(organization_path, {})
    organization = build_payload(
        catalog,
        catalog_path,
        taxonomy,
        existing_organization,
        catalog_fingerprint=catalog_fingerprint,
    )
    organization_bytes = serialized_payload(organization)
    organization_fingerprint = hashlib.sha256(organization_bytes).hexdigest()
    existing_registry = read_registry(registry_path, {})
    registry = build_registry(
        catalog,
        catalog_path,
        organization,
        organization_path,
        existing_registry,
        catalog_fingerprint=catalog_fingerprint,
        organization_fingerprint=organization_fingerprint,
    )
    problems = [
        *validate_catalog(catalog, projects_root.parent),
        *validate_organization(organization, catalog),
        *validate_registry(registry, catalog, organization),
    ]
    if problems:
        raise ValueError("; ".join(problems))

    payloads = {
        catalog_path: catalog_bytes,
        organization_path: organization_bytes,
        registry_path: serialized_payload(registry),
    }
    staged: list[tuple[Path, Path]] = []
    try:
        for destination, content in payloads.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            temporary.write_bytes(content)
            staged.append((temporary, destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()

    warnings = [*list_value(catalog.get("issues")), *list_value(registry.get("issues"))]
    repo_root = projects_root.parent.resolve()
    receipt_files = []
    for name, path in (
        ("catalog", catalog_path),
        ("organization", organization_path),
        ("asset_group_registry", registry_path),
    ):
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Shared read model escaped the repository: {path}") from exc
        receipt_files.append(
            {
                "name": name,
                "path": relative,
                "sha256": file_sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    refresh_receipt = {
        "schema_version": "shared_asset_read_models_refresh_v1",
        "status": "ready",
        "source_model": "formal_asset_packages_v1",
        "source_snapshot": source_snapshot,
        "refreshed_at": now_iso(),
        "files": receipt_files,
        "warning_count": len(warnings),
        "warnings": warnings[:MAX_RESPONSE_ITEMS],
    }
    refresh_receipt_path = output_root / "refresh_receipt.json"
    refresh_temporary = refresh_receipt_path.with_name(
        f".{refresh_receipt_path.name}.{os.getpid()}.tmp"
    )
    try:
        refresh_temporary.write_bytes(serialized_payload(refresh_receipt))
        os.replace(refresh_temporary, refresh_receipt_path)
    finally:
        if refresh_temporary.exists():
            refresh_temporary.unlink()
    return {
        "status": "warn" if warnings or organization["summary"]["needs_semantic_review_count"] else "pass",
        "source_model": "formal_asset_packages_v1",
        "source_snapshot": source_snapshot,
        "catalog_path": catalog_path.as_posix(),
        "organization_path": organization_path.as_posix(),
        "registry_path": registry_path.as_posix(),
        "catalog_summary": catalog["summary"],
        "organization_summary": organization["summary"],
        "registry_summary": registry["summary"],
        "warnings": warnings[:MAX_RESPONSE_ITEMS],
        "warning_count": len(warnings),
        "warnings_truncated": len(warnings) > MAX_RESPONSE_ITEMS,
        "refresh_receipt_path": refresh_receipt_path.as_posix(),
        "refresh_receipt": refresh_receipt,
    }


def bounded_response_items(value: Any) -> tuple[list[Any], int, bool]:
    rows = list_value(value)
    return rows[:MAX_RESPONSE_ITEMS], len(rows), len(rows) > MAX_RESPONSE_ITEMS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build the all-status read-only asset catalog")
    build.add_argument("--projects-root", required=True)
    build.add_argument("--output", default="")
    build.add_argument("--format", choices=["json", "summary"], default="summary")
    refresh = sub.add_parser("refresh", help="Refresh Catalog, Organization, and AG Registry from package manifests")
    refresh.add_argument("--projects-root", required=True)
    refresh.add_argument("--catalog-output", default="")
    refresh.add_argument("--organization-output", default="")
    refresh.add_argument("--registry-output", default="")
    refresh.add_argument("--taxonomy-file", default="")
    refresh.add_argument("--format", choices=["json", "summary"], default="summary")
    validate = sub.add_parser("validate", help="Validate paths and hashes in an existing catalog")
    validate.add_argument("--catalog", required=True)
    validate.add_argument("--repo-root", default="")
    validate.add_argument("--format", choices=["json", "summary"], default="summary")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "refresh":
        response = refresh_shared_asset_read_models(
            Path(args.projects_root),
            catalog_path=Path(args.catalog_output) if args.catalog_output else None,
            organization_path=Path(args.organization_output) if args.organization_output else None,
            registry_path=Path(args.registry_output) if args.registry_output else None,
            taxonomy_path=Path(args.taxonomy_file) if args.taxonomy_file else None,
        )
    elif args.command == "build":
        projects_root = Path(args.projects_root).resolve()
        builder = CatalogBuilder(projects_root)
        payload = builder.build()
        output = Path(args.output).resolve() if args.output else projects_root / DEFAULT_OUTPUT
        write_catalog(output, payload)
        issues, issue_count, issues_truncated = bounded_response_items(payload.get("issues"))
        response = {
            "status": "warn" if payload.get("issues") else "pass",
            "catalog_path": output.as_posix(),
            "summary": payload["summary"],
            "issues": issues,
            "issue_count": issue_count,
            "issues_truncated": issues_truncated,
        }
    else:
        catalog_path = Path(args.catalog).resolve()
        payload = read_json(catalog_path, {})
        repo_root = Path(args.repo_root).resolve() if args.repo_root else catalog_path.parents[2]
        problems = validate_catalog(payload, repo_root)
        warnings = list_value(payload.get("issues"))
        bounded_problems, problem_count, problems_truncated = bounded_response_items(problems)
        issues, issue_count, issues_truncated = bounded_response_items(warnings)
        response = {
            "status": "fail" if problems else ("warn" if warnings else "pass"),
            "catalog_path": catalog_path.as_posix(),
            "problems": bounded_problems,
            "problem_count": problem_count,
            "problems_truncated": problems_truncated,
            "issues": issues,
            "issue_count": issue_count,
            "issues_truncated": issues_truncated,
        }
    if args.format == "json":
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        print(f"status={response['status']}")
        if response.get("catalog_path"):
            print(f"catalog={response['catalog_path']}")
        if response.get("organization_path"):
            print(f"organization={response['organization_path']}")
        if response.get("registry_path"):
            print(f"registry={response['registry_path']}")
        if "summary" in response:
            for key, value in response["summary"].items():
                if not isinstance(value, dict):
                    print(f"{key}={value}")
        for problem in response.get("problems", []):
            print(f"- {problem}")
        if response.get("issues"):
            print(f"issues={response.get('issue_count', len(response['issues']))}")
    return 1 if response["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
