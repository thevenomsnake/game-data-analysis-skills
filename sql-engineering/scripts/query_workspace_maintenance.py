#!/usr/bin/env python3
"""Curate and dynamically browse the indexed query workspace without mutating SQL."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import re
import sys
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from capability_registry import command_function_ids  # noqa: E402
from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_function_selection,
    require_user_request,
)
from query_workspace_viewer import (  # noqa: E402
    VIEWER_SHELL_VERSION,
    build_workspace_payload,
    render_workspace_html,
)
from sql_query_workspace import (  # noqa: E402
    INDEX_HTML_REL,
    _write_transaction,
    json_text,
    load_index,
    now_iso,
    resolve_project_path,
)


ORGANIZATION_REL = Path("query_workspace/organization.json")
ORGANIZATION_SCHEMA_VERSION = "query_workspace_organization_v1"
SCAN_SCHEMA_VERSION = "query_workspace_maintenance_scan_v1"
CURATION_STATES = {
    "reusable_candidate",
    "keep_history",
    "discard_candidate",
    "needs_summary",
    "duplicate_candidate",
    "reviewed",
}
CLASSIFICATION_SOURCES = {"llm", "human", "deterministic"}
FORBIDDEN_DECISION_FIELDS = {
    "status",
    "current_version",
    "current_path",
    "delivery_ready",
    "formal_artifacts",
    "discard_reason",
    "promoted",
}


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def index_fingerprint(index: dict[str, Any]) -> str:
    payload = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_organization(root: Path, index: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ORGANIZATION_SCHEMA_VERSION,
        "project_id": index.get("project_id") or root.name,
        "updated_at": "",
        "index_fingerprint": index_fingerprint(index),
        "entries": {},
        "clusters": [],
    }


def load_organization(root: Path, index: dict[str, Any] | None = None) -> dict[str, Any]:
    index = index or load_index(root)
    data = read_json(root / ORGANIZATION_REL, default_organization(root, index))
    if not isinstance(data, dict):
        raise ValueError(f"{ORGANIZATION_REL.as_posix()} must be a JSON object.")
    if data.get("schema_version") != ORGANIZATION_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported query workspace organization schema: {data.get('schema_version') or 'missing'}"
        )
    if not isinstance(data.get("entries"), dict) or not isinstance(data.get("clusters"), list):
        raise ValueError("Organization overlay requires object `entries` and array `clusters`.")
    return data


def normalized_tokens(entry: dict[str, Any]) -> set[str]:
    values: list[str] = []
    for key in ["business_category", "analysis_type", "usage_class", "grain", "time_grain"]:
        values.append(str(entry.get(key) or ""))
    for key in ["source_logs", "metrics", "dimensions", "filters", "tags"]:
        raw = entry.get(key)
        if isinstance(raw, list):
            values.extend(str(item or "") for item in raw)
    tokens: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", "", value).lower()
        if normalized:
            tokens.add(normalized)
    return tokens


def connected_clusters(edges: list[tuple[str, str]]) -> list[list[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        graph[left].add(right)
        graph[right].add(left)
    clusters: list[list[str]] = []
    seen: set[str] = set()
    for start in sorted(graph):
        if start in seen:
            continue
        stack = [start]
        group: list[str] = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            group.append(current)
            stack.extend(sorted(graph[current] - seen, reverse=True))
        if len(group) > 1:
            clusters.append(sorted(group))
    return clusters


def curation_seed(entry: dict[str, Any]) -> tuple[str, str]:
    status = str(entry.get("status") or "")
    usage_class = str(entry.get("usage_class") or "unclassified")
    purpose = str(entry.get("purpose") or "").strip()
    if len(purpose) < 8:
        return "needs_summary", "用途摘要过短，先补清楚这条 SQL 在算什么。"
    if status == "promoted":
        return "reusable_candidate", "已链接正式资产，保留为可复用查询来源。"
    if status in {"discarded", "run_failed", "archived"}:
        return "keep_history", "保留可检索历史，不自动改变生命周期状态。"
    if usage_class == "personal_diagnosis" and status == "result_confirmed":
        return "discard_candidate", "个人一次性排查已完成；保留结果 lineage，SQL 可退出主视图。"
    if usage_class == "ad_hoc_analysis" and status == "result_confirmed":
        return "keep_history", "一次性专题结果可保留阅读，SQL 不因有结果自动升级为复用资产。"
    if usage_class in {"reusable_diagnostic", "reusable_analysis", "recurring_delivery"}:
        return "reusable_candidate", "用途已声明为可复用排查、稳定分析或周期交付。"
    if status == "result_confirmed":
        return "reviewed", "结果已确认，但结果存在不等于 SQL 具有长期复用价值。"
    return "reviewed", "索引信息完整，等待定期语义分类。"


def scan_workspace(root: Path) -> dict[str, Any]:
    root = root.resolve()
    index = load_index(root)
    organization = load_organization(root, index)
    entries = [item for item in index.get("entries", []) if isinstance(item, dict)]
    diagnostics: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    feedback_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for entry in entries:
        query_id = str(entry.get("query_id") or "")
        current_path = str(entry.get("current_path") or "")
        try:
            current_file = resolve_project_path(root, current_path)
        except ValueError as exc:
            current_file = None
            diagnostics.append({"query_id": query_id, "type": "invalid_current_path", "message": str(exc)})
        if current_file is not None and not current_file.exists():
            diagnostics.append(
                {
                    "query_id": query_id,
                    "type": "missing_current_sql",
                    "message": f"Current SQL is missing: {current_path}",
                }
            )
        versions = [item for item in entry.get("versions", []) if isinstance(item, dict)]
        for version in versions:
            override = version.get("temporary_rule_override")
            if not isinstance(override, dict) or not override.get("enabled"):
                continue
            signature = str(override.get("conflict_signature") or "")
            if not signature:
                continue
            key = (query_id, signature)
            row = feedback_by_key.setdefault(
                key,
                {
                    "query_id": query_id,
                    "title": entry.get("title") or "",
                    "conflict_signature": signature,
                    "source_versions": [],
                    "conflicted_rule_ids": copy.deepcopy(
                        override.get("conflicted_rule_ids") or []
                    ),
                    "conflicted_concept_keys": copy.deepcopy(
                        override.get("conflicted_concept_keys") or []
                    ),
                    "user_instruction": override.get("user_instruction") or "",
                    "conflict_reasons": copy.deepcopy(override.get("conflict_reasons") or []),
                    "first_acknowledged_at": override.get("first_acknowledged_at") or "",
                    "last_acknowledged_at": override.get("acknowledged_at") or "",
                    "repeat_count": int(override.get("repeat_count") or 0),
                    "formalization_blocked": bool(override.get("formalization_blocked")),
                    "follow_up": copy.deepcopy(
                        override.get("follow_up")
                        or {
                            "status": "open",
                            "routes": ["RULES", "SKILL_EVOLUTION"],
                        }
                    ),
                },
            )
            row["source_versions"].append(int(version.get("version") or 0))
            row["source_versions"] = sorted(set(row["source_versions"]))
            row["last_acknowledged_at"] = max(
                str(row.get("last_acknowledged_at") or ""),
                str(override.get("acknowledged_at") or ""),
            )
            row["repeat_count"] = max(
                int(row.get("repeat_count") or 0),
                int(override.get("repeat_count") or 0),
            )
        current_versions = [
            item for item in versions if int(item.get("version") or 0) == int(entry.get("current_version") or 0)
        ]
        if len(current_versions) != 1:
            diagnostics.append(
                {
                    "query_id": query_id,
                    "type": "current_version_mismatch",
                    "message": "Exactly one version must match current_version.",
                }
            )
        seed_state, seed_reason = curation_seed(entry)
        summaries.append(
            {
                "query_id": query_id,
                "title": entry.get("title") or "",
                "purpose": entry.get("purpose") or "",
                "status": entry.get("status") or "",
                "updated_at": entry.get("updated_at") or "",
                "business_category": entry.get("business_category") or "",
                "usage_class": entry.get("usage_class") or "unclassified",
                "source_logs": entry.get("source_logs") or [],
                "metrics": entry.get("metrics") or [],
                "dimensions": entry.get("dimensions") or [],
                "filters": entry.get("filters") or [],
                "version_count": len(versions),
                "derived_output_count": int(entry.get("derived_output_count") or 0),
                "formal_artifact_count": len(entry.get("formal_artifacts") or []),
                "logic_fingerprint": entry.get("logic_fingerprint") or "",
                "existing_organization": copy.deepcopy(organization.get("entries", {}).get(query_id, {})),
                "curation_seed": {"state": seed_state, "reason": seed_reason},
            }
        )

    exact_groups: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        fingerprint = str(entry.get("logic_fingerprint") or "")
        if fingerprint:
            exact_groups[fingerprint].append(str(entry.get("query_id") or ""))

    clusters: list[dict[str, Any]] = []
    clustered_pairs: set[frozenset[str]] = set()
    for fingerprint, query_ids in sorted(exact_groups.items()):
        if len(query_ids) < 2:
            continue
        ids = sorted(query_ids)
        cluster_id = "qwc-logic-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:10]
        clusters.append(
            {
                "cluster_id": cluster_id,
                "relation": "same_logic_fingerprint",
                "confidence": 1.0,
                "query_ids": ids,
                "reason": "业务逻辑指纹相同，通常只是日期参数或查询族归档方式不同。",
            }
        )
        for index_left, left in enumerate(ids):
            for right in ids[index_left + 1 :]:
                clustered_pairs.add(frozenset({left, right}))

    near_edges: list[tuple[str, str]] = []
    entry_tokens = {str(item.get("query_id") or ""): normalized_tokens(item) for item in entries}
    query_ids = sorted(entry_tokens)
    for index_left, left in enumerate(query_ids):
        for right in query_ids[index_left + 1 :]:
            if frozenset({left, right}) in clustered_pairs:
                continue
            left_tokens = entry_tokens[left]
            right_tokens = entry_tokens[right]
            union = left_tokens | right_tokens
            shared = left_tokens & right_tokens
            similarity = len(shared) / len(union) if union else 0.0
            if len(shared) >= 3 and similarity >= 0.72:
                near_edges.append((left, right))
    for ids in connected_clusters(near_edges):
        key = "|".join(ids)
        clusters.append(
            {
                "cluster_id": "qwc-near-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10],
                "relation": "near_duplicate_candidate",
                "confidence": 0.72,
                "query_ids": ids,
                "reason": "原始日志、指标、维度和筛选高度重合，需要 LLM/人工判断是否应合并查询族。",
            }
        )

    governance_feedback = sorted(
        feedback_by_key.values(),
        key=lambda item: (str(item.get("last_acknowledged_at") or ""), item["query_id"]),
        reverse=True,
    )
    return {
        "schema_version": SCAN_SCHEMA_VERSION,
        "project_id": index.get("project_id") or root.name,
        "scanned_at": now_iso(),
        "index_fingerprint": index_fingerprint(index),
        "query_count": len(entries),
        "organization_entry_count": len(organization.get("entries", {})),
        "queries": summaries,
        "clusters": clusters,
        "governance_feedback": governance_feedback,
        "diagnostics": diagnostics,
        "policy": {
            "sql_mutated": False,
            "lifecycle_mutated": False,
            "low_confidence_actions_are_suggestions_only": True,
            "query_conflicts_never_mutate_skill_or_canonical_rules": True,
        },
    }


def normalize_decision(
    query_id: str,
    raw: dict[str, Any],
    *,
    known_ids: set[str],
) -> dict[str, Any]:
    forbidden = sorted(FORBIDDEN_DECISION_FIELDS & set(raw))
    if forbidden:
        raise ValueError(
            f"Organization decision for {query_id} cannot mutate lifecycle fields: {', '.join(forbidden)}"
        )
    topic = str(raw.get("business_topic") or "").strip()
    summary = re.sub(r"\s+", " ", str(raw.get("summary") or "").strip())
    state = str(raw.get("curation_state") or "reviewed").strip()
    source = str(raw.get("classification_source") or "llm").strip()
    confidence = float(raw.get("confidence", 0.0))
    if len(topic) < 2:
        raise ValueError(f"Organization decision for {query_id} requires business_topic.")
    if len(summary) < 6:
        raise ValueError(f"Organization decision for {query_id} requires a useful summary.")
    if state not in CURATION_STATES:
        raise ValueError(f"Unsupported curation_state for {query_id}: {state}")
    if source not in CLASSIFICATION_SOURCES:
        raise ValueError(f"Unsupported classification_source for {query_id}: {source}")
    if confidence < 0 or confidence > 1:
        raise ValueError(f"confidence for {query_id} must be between 0 and 1.")
    related_query_ids = [
        str(item).strip() for item in raw.get("related_query_ids", []) if str(item).strip()
    ]
    unknown_related = sorted(set(related_query_ids) - known_ids)
    if unknown_related:
        raise ValueError(
            f"Organization decision for {query_id} references unknown queries: "
            + ", ".join(unknown_related)
        )
    if query_id in related_query_ids:
        raise ValueError(f"Organization decision for {query_id} cannot relate a query to itself.")
    return {
        "business_topic": topic,
        "summary": summary,
        "curation_state": state,
        "confidence": confidence,
        "classification_source": source,
        "notes": re.sub(r"\s+", " ", str(raw.get("notes") or "").strip()),
        "tags": [str(item).strip() for item in raw.get("tags", []) if str(item).strip()],
        "related_query_ids": list(dict.fromkeys(related_query_ids)),
        "duplicate_cluster_id": str(raw.get("duplicate_cluster_id") or "").strip(),
        "reviewed_at": now_iso(),
    }


def apply_organization(root: Path, decisions: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    index = load_index(root)
    known_ids = {
        str(item.get("query_id") or "")
        for item in index.get("entries", [])
        if isinstance(item, dict)
    }
    raw_entries = decisions.get("entries")
    if isinstance(raw_entries, list):
        raw_entries = {
            str(item.get("query_id") or ""): item
            for item in raw_entries
            if isinstance(item, dict) and item.get("query_id")
        }
    if not isinstance(raw_entries, dict) or not raw_entries:
        raise ValueError("Decisions file must contain non-empty object/array `entries`.")
    unknown_ids = sorted(set(raw_entries) - known_ids)
    if unknown_ids:
        raise ValueError("Unknown query_id values: " + ", ".join(unknown_ids))

    organization = load_organization(root, index)
    merged_entries = copy.deepcopy(organization.get("entries") or {})
    for query_id, raw in raw_entries.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Organization decision for {query_id} must be an object.")
        merged_entries[query_id] = normalize_decision(query_id, raw, known_ids=known_ids)

    clusters = decisions.get("clusters", organization.get("clusters", []))
    if not isinstance(clusters, list):
        raise ValueError("Organization `clusters` must be an array.")
    normalized_clusters: list[dict[str, Any]] = []
    for raw_cluster in clusters:
        if not isinstance(raw_cluster, dict):
            raise ValueError("Each organization cluster must be an object.")
        cluster_id = str(raw_cluster.get("cluster_id") or "").strip()
        relation = str(raw_cluster.get("relation") or "related").strip()
        confidence = float(raw_cluster.get("confidence", 0.0))
        query_ids = list(
            dict.fromkeys(str(item) for item in raw_cluster.get("query_ids", []) if str(item))
        )
        if not cluster_id:
            raise ValueError("Organization cluster requires cluster_id.")
        if not relation:
            raise ValueError(f"Organization cluster {cluster_id} requires relation.")
        if confidence < 0 or confidence > 1:
            raise ValueError(f"Organization cluster {cluster_id} confidence must be between 0 and 1.")
        if len(query_ids) < 2:
            raise ValueError(f"Organization cluster {cluster_id} requires at least two query ids.")
        cluster_unknown = sorted(set(query_ids) - known_ids)
        if cluster_unknown:
            raise ValueError("Organization cluster references unknown queries: " + ", ".join(cluster_unknown))
        normalized_clusters.append(
            {
                "cluster_id": cluster_id,
                "label": str(raw_cluster.get("label") or "").strip(),
                "relation": relation,
                "confidence": confidence,
                "query_ids": query_ids,
                "notes": str(raw_cluster.get("notes") or "").strip(),
            }
        )

    updated = {
        "schema_version": ORGANIZATION_SCHEMA_VERSION,
        "project_id": index.get("project_id") or root.name,
        "updated_at": now_iso(),
        "index_fingerprint": index_fingerprint(index),
        "entries": merged_entries,
        "clusters": normalized_clusters,
    }
    shell = render_workspace_html(root, index)
    _write_transaction(
        {
            root / ORGANIZATION_REL: json_text(updated),
            root / INDEX_HTML_REL: shell,
        }
    )
    return {
        "status": "applied",
        "organization_path": ORGANIZATION_REL.as_posix(),
        "viewer_path": INDEX_HTML_REL.as_posix(),
        "updated_entry_count": len(raw_entries),
        "total_organized_entry_count": len(merged_entries),
        "cluster_count": len(normalized_clusters),
        "sql_mutated": False,
        "lifecycle_mutated": False,
    }


def indexed_paths(index: dict[str, Any]) -> tuple[set[str], set[str]]:
    sql_paths: set[str] = set()
    output_paths: set[str] = set()
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        for version in entry.get("versions", []):
            if not isinstance(version, dict):
                continue
            path = str(version.get("path") or "")
            if path:
                sql_paths.add(path.replace("\\", "/"))
            for output in version.get("derived_outputs", []):
                if isinstance(output, dict) and output.get("path"):
                    output_paths.add(str(output["path"]).replace("\\", "/"))
    return sql_paths, output_paths


def create_workspace_server(root: Path, host: str, port: int) -> ThreadingHTTPServer:
    root = root.resolve()
    load_index(root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "QueryWorkspace/2"

        def log_message(self, format_string: str, *args: Any) -> None:
            sys.stderr.write("query_workspace: " + (format_string % args) + "\n")

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            request = urlparse(self.path)
            if request.path in {"/", "/index.html", "/query_workspace/index.html"}:
                index = load_index(root)
                body = render_workspace_html(root, index).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if request.path == "/api/query-workspace":
                index = load_index(root)
                organization = load_organization(root, index)
                self.send_json(build_workspace_payload(root, index, organization=organization))
                return
            if request.path == "/api/query-workspace/health":
                scan = scan_workspace(root)
                self.send_json(
                    {
                        "status": "ok" if not scan["diagnostics"] else "warn",
                        "query_count": scan["query_count"],
                        "diagnostic_count": len(scan["diagnostics"]),
                        "viewer_contract": VIEWER_SHELL_VERSION,
                    }
                )
                return
            if request.path in {"/api/query-workspace/sql", "/api/query-workspace/output"}:
                params = parse_qs(request.query)
                relative = str((params.get("path") or [""])[0]).replace("\\", "/")
                index = load_index(root)
                sql_paths, output_paths = indexed_paths(index)
                allowed = sql_paths if request.path.endswith("/sql") else output_paths
                if relative not in allowed:
                    self.send_json({"error": "Path is not registered in the query workspace index."}, 404)
                    return
                try:
                    target = resolve_project_path(root, relative)
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
                    return
                if not target.exists():
                    self.send_json({"error": "Indexed file is missing."}, 404)
                    return
                if request.path.endswith("/sql"):
                    sql = target.read_text(encoding="utf-8-sig")
                    self.send_json(
                        {
                            "path": relative,
                            "sql": sql,
                            "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                        }
                    )
                    return
                body = target.read_bytes()
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                fallback_name = re.sub(r"[^A-Za-z0-9._-]", "_", target.name) or "download"
                encoded_name = quote(target.name, safe="")
                self.send_header(
                    "Content-Disposition",
                    f'inline; filename="{fallback_name}"; filename*=UTF-8\'\'{encoded_name}',
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_json({"error": "Not found"}, 404)

    return ThreadingHTTPServer((host, port), Handler)


def render_text(result: dict[str, Any]) -> str:
    lines = [f"status: {result.get('status', 'ok')}"]
    for key in [
        "query_count",
        "organization_entry_count",
        "updated_entry_count",
        "cluster_count",
        "organization_path",
        "viewer_path",
    ]:
        if key in result:
            lines.append(f"{key}: {result[key]}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Read-only inventory and duplicate/curation diagnostics")
    scan.add_argument("--root", required=True)
    scan.add_argument("--format", choices=["json", "text"], default="json")

    apply = sub.add_parser("apply", help="Apply reviewed semantic organization as a separate overlay")
    apply.add_argument("--root", required=True)
    apply.add_argument("--decisions-file", required=True)
    apply.add_argument("--format", choices=["json", "text"], default="json")
    add_function_gate_arguments(
        apply,
        selection_help="Optional route [QUERY_WORKSPACE_MAINTENANCE] or [PROJECT_ADMIN].",
    )

    serve = sub.add_parser("serve", help="Serve the live query workspace viewer")
    serve.add_argument("--root", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.command == "scan":
            result = scan_workspace(root)
            result["status"] = "ok" if not result["diagnostics"] else "warn"
        elif args.command == "apply":
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=command_function_ids("query_workspace_maintenance.py", "apply"),
                purpose="query workspace semantic organization",
            )
            require_user_request(args.user_request, purpose="query workspace semantic organization")
            decisions = read_json(Path(args.decisions_file).resolve(), {})
            if not isinstance(decisions, dict):
                raise ValueError("Decisions file must contain a JSON object.")
            result = apply_organization(root, decisions)
        else:
            server = create_workspace_server(root, args.host, args.port)
            address, port = server.server_address[:2]
            print(f"Query workspace: http://{address}:{port}/", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "error", "error": str(exc)}
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result), end="")
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
