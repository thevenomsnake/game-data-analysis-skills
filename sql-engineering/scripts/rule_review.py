#!/usr/bin/env python3
"""Build and serve a cross-project canonical-rule review surface."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from function_gate import (
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_request,
    require_user_function_selection,
)
from capability_registry import command_function_ids
from project_rules import config_owned_rule_markers, has_v2_store, load_rules


STATE_VERSION = 1
DEFAULT_OUTPUT_REL = "_rule_review/rule_review.html"
DEFAULT_STATE_REL = "_rule_review/rule_review_state.json"
DEFAULT_CONCEPT_REGISTRY_REL = "_rule_review/rule_concepts.json"

PROJECT_ORDER = ["DEMO_EXPERIMENT", "DEMO_AB_TEST", "DEMO_ANALYTICS"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def slug_text(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def empty_concept_registry() -> dict:
    return {
        "version": 1,
        "updated_at": "",
        "description": "Cross-project口径 concept keys. Business rules remain project-scoped; this registry only indexes comparable project rules.",
        "concepts": [],
        "_issues": [],
    }


def load_concept_registry(path: Path) -> dict:
    registry = read_json(path, empty_concept_registry())
    registry.setdefault("version", 1)
    registry.setdefault("concepts", [])
    issues = []
    if not path.exists():
        issues.append(
            {
                "severity": "ERROR",
                "code": "missing_concept_registry",
                "message": f"Concept registry not found: {path}",
            }
        )
    normalized = []
    seen: set[str] = set()
    for item in registry["concepts"]:
        raw_key = str(item.get("concept_key") or item.get("key") or "").strip()
        if not raw_key:
            issues.append(
                {
                    "severity": "ERROR",
                    "code": "missing_registry_key",
                    "message": "A concept entry is missing concept_key.",
                }
            )
            continue
        key = slug_text(raw_key)
        if key in seen:
            issues.append(
                {
                    "severity": "ERROR",
                    "code": "duplicate_concept_key",
                    "concept_key": key,
                    "message": f"Duplicate concept_key `{key}` in rule_concepts.json.",
                }
            )
            continue
        seen.add(key)
        normalized.append(
            {
                "concept_key": key,
                "label": item.get("label") or key,
                "description": item.get("description", ""),
                "expected_projects": [str(project) for project in item.get("expected_projects", [])],
                "keywords": [str(keyword).lower() for keyword in item.get("keywords", [])],
                "status": item.get("status", "active"),
                "notes": item.get("notes", ""),
            }
        )
    registry["concepts"] = normalized
    registry["_issues"] = issues
    return registry


def concept_index(registry: dict) -> dict:
    by_key = {item["concept_key"]: item for item in registry.get("concepts", [])}
    return {"by_key": by_key, "concepts": registry.get("concepts", [])}


def rule_search_text(rule: dict) -> str:
    return " ".join(
        str(rule.get(key, ""))
        for key in ["rule_id", "title", "content", "applies_to", "source_evidence", "notes"]
    ).lower()


def suggest_concepts(rule: dict, index: dict) -> list[str]:
    text = rule_search_text(rule)
    suggestions = []
    for item in index["concepts"]:
        for keyword in item.get("keywords", []):
            if keyword and keyword in text:
                suggestions.append(item["concept_key"])
                break
    return suggestions


def concept_for_rule(rule: dict, index: dict) -> tuple[str, str, list[str]]:
    suggestions = suggest_concepts(rule, index)
    explicit_key = rule.get("concept_key") or rule.get("concept")
    if explicit_key:
        key = slug_text(str(explicit_key))
        source = "rule.concept_key" if key in index["by_key"] else "rule.concept_key_unregistered"
        return key, source, suggestions

    rule_id = str(rule.get("rule_id", ""))
    return f"unmapped-{slug_text(rule_id or rule.get('title', 'rule'))}", "missing_concept_key", suggestions


def concept_label(concept: str, rules: list[dict], index: dict) -> str:
    if concept in index["by_key"]:
        return index["by_key"][concept].get("label") or concept
    titles = [rule.get("title", "") for rule in rules if rule.get("title")]
    if concept.startswith("unmapped-"):
        return f"未登记 concept_key：{titles[0] if titles else concept}"
    return titles[0] if titles else concept


def concept_description(concept: str, index: dict) -> str:
    if concept in index["by_key"]:
        return index["by_key"][concept].get("description", "")
    if concept.startswith("unmapped-"):
        return "该项目口径尚未显式填写 concept_key；需要人工决定归入已有 concept_key 或新增概念 key。"
    return "该口径填写了未登记的 concept_key；需要先登记到 rule_concepts.json，或修正项目规则。"


def project_dirs(projects_root: Path, explicit: list[str] | None) -> list[Path]:
    if explicit:
        return [projects_root / item for item in explicit]
    dirs = [
        path for path in projects_root.iterdir()
        if path.is_dir()
        and not path.name.startswith("_")
        and (path / "manifest.json").exists()
        and has_v2_store(path)
    ]
    return sorted(dirs, key=lambda path: (PROJECT_ORDER.index(path.name) if path.name in PROJECT_ORDER else 99, path.name))


def load_project(project_root: Path) -> dict:
    manifest = read_json(project_root / "manifest.json", {})
    config = read_json(project_root / "project_config.json", {})
    rules = load_rules(project_root, status="confirmed") + load_rules(project_root, status="proposed")
    rules.extend(config_owned_rule_markers(project_root).values())
    return {
        "slug": project_root.name,
        "name": config.get("display_name") or manifest.get("project_name") or project_root.name,
        "root": str(project_root),
        "config": {
            "sql_dialect": config.get("sql_dialect", "missing"),
            "query_engine": config.get("query_engine", "missing"),
            "query_environment": config.get("query_environment", {}).get("name", config.get("query_environment", "missing")),
            "dashboard_application": config.get("dashboard_application", {}).get("name", config.get("dashboard_application", "missing")),
            "table_naming_profile": (config.get("table_naming_profile") or {}).get("name", "missing"),
            "player_login": (config.get("table_overrides") or {}).get("PlayerLogin", ""),
        },
        "rules": sorted(rules, key=lambda r: (str(r.get("rule_id", "")), int(r.get("version", 0)))),
    }


def latest_rule(rules: list[dict]) -> dict | None:
    active = [rule for rule in rules if rule.get("status") in {"confirmed", "proposed"}]
    rows = active or rules
    if not rows:
        return None
    status_rank = {"confirmed": 2, "proposed": 1, "superseded": 0, "deprecated": 0}
    return sorted(
        rows,
        key=lambda r: (
            status_rank.get(str(r.get("status", "")), 0),
            int(r.get("version", 0)),
            str(r.get("updated_at", "")),
        ),
    )[-1]


def rule_requires_concept_key(rule: dict) -> bool:
    return (
        rule.get("status") in {"confirmed", "proposed"}
        and rule.get("scope", "project") == "project"
        and rule.get("lifetime", "persistent") == "persistent"
    )


def validation_issue(severity: str, code: str, message: str, **extra) -> dict:
    item = {"severity": severity, "code": code, "message": message}
    item.update(extra)
    return item


def validate_payload(projects: list[dict], concepts: list[dict], registry: dict, index: dict) -> list[dict]:
    issues = list(registry.get("_issues", []))

    for project in projects:
        for rule in project["rules"]:
            explicit_key = str(rule.get("concept_key") or "").strip()
            if rule_requires_concept_key(rule) and not explicit_key:
                issues.append(
                    validation_issue(
                        "ERROR",
                        "missing_concept_key",
                        f"{project['slug']} rule `{rule.get('rule_id', '')}` is project/persistent but has no concept_key.",
                        project=project["slug"],
                        rule_id=rule.get("rule_id", ""),
                    )
                )
            if explicit_key and slug_text(explicit_key) not in index["by_key"]:
                issues.append(
                    validation_issue(
                        "ERROR",
                        "unregistered_concept_key",
                        f"{project['slug']} rule `{rule.get('rule_id', '')}` uses unregistered concept_key `{slug_text(explicit_key)}`.",
                        project=project["slug"],
                        rule_id=rule.get("rule_id", ""),
                        concept_key=slug_text(explicit_key),
                    )
                )

    by_concept = {item["concept"]: item for item in concepts}
    for concept in registry.get("concepts", []):
        row = by_concept.get(concept["concept_key"])
        expected_projects = concept.get("expected_projects", [])
        for project_slug in expected_projects:
            if not row or not row["project_cells"].get(project_slug, {}).get("present"):
                issues.append(
                    validation_issue(
                        "WARN",
                        "expected_project_missing",
                        f"Concept `{concept['concept_key']}` expects project `{project_slug}` but no matching rule is present.",
                        concept_key=concept["concept_key"],
                        project=project_slug,
                    )
                )

    for row in concepts:
        for project_slug, cell in row["project_cells"].items():
            current_versions = [
                rule
                for rule in cell.get("versions", [])
                if rule.get("status") == "confirmed"
            ]
            if len(current_versions) > 1:
                issues.append(
                    validation_issue(
                        "ERROR",
                        "multiple_current_versions",
                        f"Concept `{row['concept']}` has multiple current confirmed versions in `{project_slug}`.",
                        concept_key=row["concept"],
                        project=project_slug,
                        rule_ids=[rule.get("rule_id", "") for rule in current_versions],
                    )
                )
    return issues


def build_payload(projects_root: Path, explicit: list[str] | None, state_path: Path, concept_registry_path: Path) -> dict:
    projects = [load_project(path) for path in project_dirs(projects_root, explicit)]
    registry = load_concept_registry(concept_registry_path)
    index = concept_index(registry)
    groups: dict[str, dict] = {}
    unmapped_rules = []
    for project in projects:
        for rule in project["rules"]:
            concept, match_source, suggestions = concept_for_rule(rule, index)
            if match_source in {"missing_concept_key", "rule.concept_key_unregistered"}:
                unmapped_rules.append(
                    {
                        "project": project["slug"],
                        "rule_id": rule.get("rule_id", ""),
                        "title": rule.get("title", ""),
                        "match_source": match_source,
                        "suggested_concepts": suggestions,
                    }
                )
            groups.setdefault(concept, {"concept": concept, "rules": [], "projects": {}, "match_sources": set()})
            wrapped = dict(rule)
            wrapped["_project"] = project["slug"]
            wrapped["_concept_match_source"] = match_source
            wrapped["_concept_suggestions"] = suggestions
            groups[concept]["rules"].append(wrapped)
            groups[concept]["projects"].setdefault(project["slug"], []).append(rule)
            groups[concept]["match_sources"].add(match_source)

    concepts = []
    for concept, group in groups.items():
        project_cells = {}
        for project in projects:
            versions = group["projects"].get(project["slug"], [])
            current = latest_rule(versions)
            project_cells[project["slug"]] = {
                "present": bool(versions),
                "latest": current,
                "versions": sorted(versions, key=lambda r: int(r.get("version", 0))),
                "review_key": f"{concept}::{project['slug']}",
                "hash": stable_hash(versions),
            }
        concepts.append(
            {
                "concept": concept,
                "label": concept_label(concept, group["rules"], index),
                "description": concept_description(concept, index),
                "registry_status": "registered" if concept in index["by_key"] else "unregistered",
                "match_sources": sorted(group["match_sources"]),
                "project_cells": project_cells,
                "present_count": sum(1 for cell in project_cells.values() if cell["present"]),
                "statuses": sorted({cell["latest"].get("status") for cell in project_cells.values() if cell.get("latest")}),
            }
        )
    concepts.sort(key=lambda row: (-row["present_count"], row["label"], row["concept"]))
    validation_issues = validate_payload(projects, concepts, registry, index)
    return {
        "generated_at": now_iso(),
        "projects_root": str(projects_root),
        "state_path": str(state_path),
        "concept_registry_path": str(concept_registry_path),
        "projects": projects,
        "concepts": concepts,
        "registered_concepts": len(index["by_key"]),
        "unmapped_rules": unmapped_rules,
        "validation_issues": validation_issues,
        "state": read_json(state_path, {"version": STATE_VERSION, "items": {}}),
    }


def html_shell(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>口径 Review</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #17202a;
      --muted: #667085;
      --accent: #1f6feb;
      --ok: #147d4f;
      --warn: #a05a00;
      --bad: #b42318;
      --soft: #eef2f7;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Segoe UI", Arial, sans-serif; background: var(--bg); color: var(--text); }}
    header {{ height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 18px; border-bottom: 1px solid var(--line); background: var(--panel); }}
    h1 {{ font-size: 18px; margin: 0; font-weight: 650; }}
    button {{ border: 1px solid var(--line); background: var(--panel); color: var(--text); border-radius: 6px; padding: 8px 10px; cursor: pointer; }}
    button.primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
    button.danger {{ background: #fff4f2; border-color: #ffb4a8; color: var(--bad); }}
    main {{ display: grid; grid-template-columns: 330px 1fr; min-height: calc(100vh - 56px); }}
    aside {{ border-right: 1px solid var(--line); background: var(--panel); min-width: 0; }}
    .toolbar {{ padding: 12px; border-bottom: 1px solid var(--line); display: grid; gap: 8px; }}
    input, textarea {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; font: inherit; background: white; }}
    .list {{ overflow: auto; max-height: calc(100vh - 122px); }}
    .rule-item {{ padding: 12px; border-bottom: 1px solid var(--line); cursor: pointer; display: grid; gap: 7px; }}
    .rule-item.active {{ background: #eaf2ff; box-shadow: inset 3px 0 0 var(--accent); }}
    .rule-title {{ font-size: 14px; font-weight: 650; }}
    .rule-meta {{ font-size: 12px; color: var(--muted); }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 5px; }}
    .chip {{ display: inline-flex; align-items: center; min-height: 22px; padding: 2px 7px; border-radius: 999px; font-size: 12px; background: var(--soft); color: var(--muted); }}
    .chip.ok {{ background: #e8f6ef; color: var(--ok); }}
    .chip.warn {{ background: #fff3dd; color: var(--warn); }}
    .chip.bad {{ background: #fff0ee; color: var(--bad); }}
    .content {{ padding: 16px; overflow: auto; max-height: calc(100vh - 56px); }}
    .summary {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .metric, .section, .project-panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .metric {{ padding: 12px; }}
    .metric strong {{ display: block; font-size: 20px; margin-top: 4px; }}
    .section {{ margin-bottom: 14px; }}
    .section-head {{ padding: 12px 14px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; gap: 10px; align-items: center; }}
    .section h2 {{ margin: 0; font-size: 16px; }}
    .project-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; padding: 12px; }}
    .project-panel {{ padding: 12px; display: grid; gap: 10px; align-content: start; }}
    .project-panel.missing {{ background: #fbfcfe; border-style: dashed; }}
    .project-title {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; font-weight: 650; }}
    .kv {{ display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 8px; padding: 5px 0; border-bottom: 1px solid #edf0f5; font-size: 13px; }}
    .kv span:first-child {{ color: var(--muted); }}
    .pre {{ white-space: pre-wrap; word-break: break-word; line-height: 1.45; font-size: 13px; background: #f8fafc; border: 1px solid #edf0f5; border-radius: 6px; padding: 10px; }}
    .timeline {{ display: grid; gap: 8px; }}
    .timeline-row {{ border-left: 3px solid var(--line); padding-left: 9px; font-size: 13px; }}
    .review {{ display: grid; gap: 8px; }}
    .review-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .empty {{ color: var(--muted); font-size: 13px; padding: 8px 0; }}
    @media (max-width: 860px) {{
      main {{ grid-template-columns: 1fr; }}
      aside {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .list {{ max-height: 260px; }}
      .summary {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>口径 Review</h1>
    <div class="chips" id="headerMeta"></div>
  </header>
  <main>
    <aside>
      <div class="toolbar">
        <input id="search" placeholder="搜索口径 / 项目 / 状态">
        <div class="chips" id="projectChips"></div>
      </div>
      <div class="list" id="ruleList"></div>
    </aside>
    <section class="content">
      <div class="summary" id="summary"></div>
      <div id="detail"></div>
    </section>
  </main>
  <script>
    const payload = {data};
    const storageKey = 'rule-review::' + payload.projects_root;
    let state = JSON.parse(localStorage.getItem(storageKey) || JSON.stringify(payload.state || {{version: 1, items: {{}}}}));
    state.items = state.items || {{}};
    let selected = 0;
    const projectFilter = new Set();

    const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#039;'}}[ch]));
    const latestStatus = cell => cell.latest ? cell.latest.status : 'missing';
    const statusChip = status => {{
      if (status === 'confirmed') return '<span class="chip ok">confirmed</span>';
      if (status === 'proposed') return '<span class="chip warn">proposed</span>';
      if (status === 'superseded') return '<span class="chip">superseded</span>';
      if (status === 'deprecated') return '<span class="chip bad">deprecated</span>';
      if (status === 'approved') return '<span class="chip ok">已确认</span>';
      if (status === 'rejected') return '<span class="chip bad">有问题</span>';
      if (status === 'changed') return '<span class="chip warn">已变化</span>';
      if (status === 'unregistered') return '<span class="chip bad">未登记</span>';
      if (status === 'registered') return '<span class="chip ok">concept_key</span>';
      if (status === 'ERROR') return '<span class="chip bad">ERROR</span>';
      if (status === 'WARN') return '<span class="chip warn">WARN</span>';
      return '<span class="chip">空</span>';
    }};
    const reviewStatus = cell => {{
      const saved = state.items[cell.review_key];
      if (!saved) return 'pending';
      if (saved.hash && saved.hash !== cell.hash) return 'changed';
      return saved.status || 'pending';
    }};
    function saveLocal() {{
      state.updated_at = new Date().toISOString();
      localStorage.setItem(storageKey, JSON.stringify(state));
    }}
    async function persist() {{
      if (!location.protocol.startsWith('http')) return;
      try {{
        await fetch('/api/state', {{method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(state)}});
      }} catch (err) {{
        console.warn(err);
      }}
    }}
    function setReview(concept, project, status) {{
      const textarea = document.getElementById('note-' + CSS.escape(concept) + '-' + CSS.escape(project));
      const current = currentConcept().project_cells[project];
      state.items[current.review_key] = {{
        status,
        note: textarea ? textarea.value : '',
        hash: current.hash,
        concept,
        project,
        reviewed_at: new Date().toISOString()
      }};
      saveLocal();
      persist();
      render();
    }}
    function currentConcept() {{ return filteredConcepts()[selected] || payload.concepts[0]; }}
    function filteredConcepts() {{
      const q = document.getElementById('search')?.value.trim().toLowerCase() || '';
      return payload.concepts.filter(row => {{
        const text = [row.label, row.concept, ...payload.projects.map(p => p.name), ...Object.values(row.project_cells).map(c => c.latest ? [c.latest.rule_id, c.latest.title, c.latest.content, c.latest.status].join(' ') : '')].join(' ').toLowerCase();
        const projectOk = !projectFilter.size || Array.from(projectFilter).some(project => row.project_cells[project]?.present);
        return projectOk && (!q || text.includes(q));
      }});
    }}
    function renderHeader() {{
      const errorCount = (payload.validation_issues || []).filter(item => item.severity === 'ERROR').length;
      const warnCount = (payload.validation_issues || []).filter(item => item.severity === 'WARN').length;
      document.getElementById('headerMeta').innerHTML = `<span class="chip">${{esc(payload.generated_at)}}</span><span class="chip">${{payload.projects.length}} 项目</span><span class="chip">${{payload.concepts.length}} 口径</span><span class="chip">${{payload.registered_concepts}} concept keys</span><span class="chip">${{payload.unmapped_rules.length}} 未登记</span><span class="chip bad">${{errorCount}} errors</span><span class="chip warn">${{warnCount}} warnings</span><span class="chip">${{esc(payload.concept_registry_path)}}</span>`;
      document.getElementById('projectChips').innerHTML = payload.projects.map(project => `<button onclick="toggleProject('${{esc(project.slug)}}')" class="${{projectFilter.has(project.slug) ? 'primary' : ''}}">${{esc(project.name)}}</button>`).join('');
    }}
    function toggleProject(project) {{
      if (projectFilter.has(project)) projectFilter.delete(project); else projectFilter.add(project);
      selected = 0;
      render();
    }}
    function renderList() {{
      const rows = filteredConcepts();
      if (selected >= rows.length) selected = 0;
      document.getElementById('ruleList').innerHTML = rows.map((row, index) => {{
        const chips = statusChip(row.registry_status) + payload.projects.map(project => statusChip(latestStatus(row.project_cells[project.slug]))).join('');
        return `<div class="rule-item ${{index === selected ? 'active' : ''}}" onclick="selected=${{index}}; render()">
          <div class="rule-title">${{esc(row.label)}}</div>
          <div class="rule-meta">${{esc(row.concept)}} · 覆盖 ${{row.present_count}}/${{payload.projects.length}}</div>
          <div class="chips">${{chips}}</div>
        </div>`;
      }}).join('') || '<div class="empty" style="padding:12px;">没有匹配口径</div>';
    }}
    function renderSummary() {{
      const row = currentConcept();
      const present = row ? row.present_count : 0;
      const approved = row ? Object.values(row.project_cells).filter(cell => reviewStatus(cell) === 'approved').length : 0;
      const rejected = row ? Object.values(row.project_cells).filter(cell => reviewStatus(cell) === 'rejected').length : 0;
      document.getElementById('summary').innerHTML = `
        <div class="metric"><span>当前口径</span><strong>${{esc(row?.label || '')}}</strong></div>
        <div class="metric"><span>concept_key</span><strong>${{esc(row?.concept || '')}}</strong></div>
        <div class="metric"><span>人工 review</span><strong>${{approved}} 确认 / ${{rejected}} 有问题</strong></div>`;
    }}
    function ruleBlock(rule) {{
      if (!rule) return '<div class="empty">该项目暂无此口径</div>';
      const affected = Array.isArray(rule.affected_artifacts) ? rule.affected_artifacts.join(', ') : '';
      const suggestions = Array.isArray(rule._concept_suggestions) ? rule._concept_suggestions.join(', ') : '';
      return `
        <div class="kv"><span>rule_id</span><span>${{esc(rule.rule_id)}} v${{esc(rule.version)}}</span></div>
        <div class="kv"><span>concept_key</span><span>${{esc(rule.concept_key || '')}}</span></div>
        <div class="kv"><span>匹配来源</span><span>${{esc(rule._concept_match_source || '')}}</span></div>
        ${{suggestions ? '<div class="kv"><span>建议 key</span><span>' + esc(suggestions) + '</span></div>' : ''}}
        <div class="kv"><span>状态</span><span>${{statusChip(rule.status)}}</span></div>
        <div class="kv"><span>标题</span><span>${{esc(rule.title)}}</span></div>
        <div class="kv"><span>适用范围</span><span>${{esc(rule.applies_to || '')}}</span></div>
        <div class="kv"><span>source</span><span>${{esc(rule.source || '')}}</span></div>
        <div class="kv"><span>创建 / 更新</span><span>${{esc(rule.created_at || '')}} / ${{esc(rule.updated_at || '')}}</span></div>
        <div class="pre">${{esc(rule.content || '')}}</div>
        ${{rule.source_evidence ? '<div class="kv"><span>证据</span><span>' + esc(rule.source_evidence) + '</span></div>' : ''}}
        ${{rule.decision_question ? '<div class="kv"><span>待确认</span><span>' + esc(rule.decision_question) + '</span></div>' : ''}}
        ${{affected ? '<div class="kv"><span>影响资产</span><span>' + esc(affected) + '</span></div>' : ''}}
        ${{rule.notes ? '<div class="kv"><span>备注</span><span>' + esc(rule.notes) + '</span></div>' : ''}}`;
    }}
    function timeline(versions) {{
      if (!versions.length) return '<div class="empty">无变化记录</div>';
      return `<div class="timeline">${{versions.map(rule => `
        <div class="timeline-row">
          <strong>v${{esc(rule.version)}} · ${{esc(rule.status)}}</strong>
          <div>${{esc(rule.title || '')}}</div>
          <div class="rule-meta">${{esc(rule.created_at || '')}} · ${{esc(rule.updated_at || '')}}</div>
        </div>`).join('')}}</div>`;
    }}
    function renderDetail() {{
      const row = currentConcept();
      if (!row) {{
        document.getElementById('detail').innerHTML = '<div class="empty">没有口径</div>';
        return;
      }}
      document.getElementById('detail').innerHTML = `
        ${{renderIssues(row.concept)}}
        <div class="section">
          <div class="section-head"><h2>${{esc(row.label)}}</h2><div class="chips">${{statusChip(row.registry_status)}}<span class="chip">${{esc(row.concept)}}</span></div></div>
          <div style="padding: 12px 14px 0;">
            <div class="pre">${{esc(row.description || '')}}</div>
          </div>
          <div class="project-grid">
            ${{payload.projects.map(project => {{
              const cell = row.project_cells[project.slug];
              const latest = cell.latest;
              const saved = state.items[cell.review_key] || {{}};
              return `<div class="project-panel ${{cell.present ? '' : 'missing'}}">
                <div class="project-title"><span>${{esc(project.name)}}</span><span>${{statusChip(latestStatus(cell))}}</span></div>
                <div class="kv"><span>方言/引擎</span><span>${{esc(project.config.sql_dialect)}} / ${{esc(project.config.query_engine)}}</span></div>
                <div class="kv"><span>表名规则</span><span>${{esc(project.config.table_naming_profile)}}</span></div>
                <div class="kv"><span>PlayerLogin</span><span>${{esc(project.config.player_login || '未配置')}}</span></div>
                ${{ruleBlock(latest)}}
                <div>
                  <strong>变化</strong>
                  ${{timeline(cell.versions)}}
                </div>
                <div class="review">
                  <div class="chips">${{statusChip(reviewStatus(cell))}}</div>
                  <textarea id="note-${{esc(row.concept)}}-${{esc(project.slug)}}" rows="3" placeholder="人工 review 备注">${{esc(saved.note || '')}}</textarea>
                  <div class="review-actions">
                    <button class="primary" onclick="setReview('${{esc(row.concept)}}','${{esc(project.slug)}}','approved')">确认</button>
                    <button class="danger" onclick="setReview('${{esc(row.concept)}}','${{esc(project.slug)}}','rejected')">有问题</button>
                  </div>
                </div>
              </div>`;
            }}).join('')}}
          </div>
        </div>`;
    }}
    function renderIssues(concept) {{
      const items = (payload.validation_issues || []).filter(item => !item.concept_key || item.concept_key === concept);
      if (!items.length) return '';
      return `<div class="section">
        <div class="section-head"><h2>校验问题</h2><div class="chips">${{items.map(item => statusChip(item.severity)).join('')}}</div></div>
        <div style="padding: 12px 14px;">
          ${{items.map(item => `<div class="kv"><span>${{esc(item.code)}}</span><span>${{esc(item.message)}}</span></div>`).join('')}}
        </div>
      </div>`;
    }}
    function render() {{
      renderHeader();
      renderList();
      renderSummary();
      renderDetail();
    }}
    document.getElementById('search').addEventListener('input', () => {{ selected = 0; render(); }});
    render();
  </script>
</body>
</html>
"""


def build_html(projects_root: Path, output: Path, state_path: Path, concept_registry_path: Path, projects: list[str] | None) -> dict:
    payload = build_payload(projects_root, projects, state_path, concept_registry_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_shell(payload), encoding="utf-8")
    if not state_path.exists():
        write_json(state_path, payload["state"])
    return payload


def cmd_build(args) -> None:
    projects_root = Path(args.projects_root).resolve()
    output = Path(args.output).resolve() if args.output else projects_root / DEFAULT_OUTPUT_REL
    state_path = Path(args.state_file).resolve() if args.state_file else projects_root / DEFAULT_STATE_REL
    concept_registry_path = Path(args.concept_registry).resolve() if args.concept_registry else projects_root / DEFAULT_CONCEPT_REGISTRY_REL
    payload = build_html(projects_root, output, state_path, concept_registry_path, args.project)
    print(f"rule_review_html: {output}")
    print(f"rule_review_state: {state_path}")
    print(f"rule_concept_registry: {concept_registry_path}")
    print(f"projects: {len(payload['projects'])}")
    print(f"concepts: {len(payload['concepts'])}")
    print(f"unmapped_rules: {len(payload['unmapped_rules'])}")
    print(f"validation_errors: {sum(1 for item in payload['validation_issues'] if item.get('severity') == 'ERROR')}")
    print(f"validation_warnings: {sum(1 for item in payload['validation_issues'] if item.get('severity') == 'WARN')}")


def cmd_validate(args) -> None:
    projects_root = Path(args.projects_root).resolve()
    state_path = Path(args.state_file).resolve() if args.state_file else projects_root / DEFAULT_STATE_REL
    concept_registry_path = Path(args.concept_registry).resolve() if args.concept_registry else projects_root / DEFAULT_CONCEPT_REGISTRY_REL
    try:
        payload = build_payload(projects_root, args.project, state_path, concept_registry_path)
        result = rule_review_health_payload(payload, projects_root, concept_registry_path, args.strict)
    except Exception as exc:  # noqa: BLE001
        result = rule_review_error_payload(projects_root, str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code_for_status(result["status"]))


def exit_code_for_status(status: str) -> int:
    if status == "pass":
        return 0
    if status == "fail":
        return 1
    if status == "warn":
        return 2
    return 3


def rule_review_health_payload(payload: dict, projects_root: Path, concept_registry_path: Path, strict: bool) -> dict:
    checks = []
    issues = payload.get("validation_issues", [])
    for issue in issues:
        severity = issue.get("severity")
        status = "fail" if severity == "ERROR" else "warn"
        code = issue.get("code") or "issue"
        path_parts = [str(concept_registry_path)]
        if issue.get("project"):
            path_parts.append(str(issue["project"]))
        if issue.get("concept_key"):
            path_parts.append(str(issue["concept_key"]))
        checks.append(
            {
                "id": f"rule_review.{code}",
                "status": status,
                "message": issue.get("message", ""),
                "path": "#".join(path_parts),
            }
        )
    if not checks:
        checks.append(
            {
                "id": "rule_review.concept_key_coverage",
                "status": "pass",
                "message": "Rule concept registry and project concept_key coverage passed.",
                "path": str(concept_registry_path),
            }
        )

    warnings = [item for item in checks if item["status"] == "warn"]
    errors = [item for item in checks if item["status"] == "fail"]
    if errors:
        status = "fail"
    elif warnings:
        status = "warn"
    else:
        status = "pass"
    passed = sum(1 for item in checks if item["status"] == "pass")
    return {
        "project": "rule_review",
        "status": status,
        "projects_root": str(projects_root),
        "strict": strict,
        "summary": {
            "checks": len(checks),
            "passed": passed,
            "warnings": len(warnings),
            "failures": len(errors),
        },
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "details": {
            "projects": len(payload.get("projects", [])),
            "concepts": len(payload.get("concepts", [])),
            "registered_concepts": payload.get("registered_concepts", []),
            "unmapped_rules": len(payload.get("unmapped_rules", [])),
        },
    }


def rule_review_error_payload(projects_root: Path, message: str) -> dict:
    check = {
        "id": "runtime.error",
        "status": "fail",
        "message": message,
        "path": str(projects_root),
    }
    return {
        "project": "rule_review",
        "status": "error",
        "projects_root": str(projects_root),
        "strict": False,
        "summary": {"checks": 1, "passed": 0, "warnings": 0, "failures": 1},
        "checks": [check],
        "warnings": [],
        "errors": [check],
    }


class RuleReviewHandler(BaseHTTPRequestHandler):
    projects_root: Path
    state_path: Path
    concept_registry_path: Path
    projects: list[str] | None

    def send_text(self, status: int, text: str, content_type: str) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            self.send_text(200, json.dumps(read_json(self.state_path, {"version": STATE_VERSION, "items": {}}), ensure_ascii=False), "application/json; charset=utf-8")
            return
        payload = build_payload(self.projects_root, self.projects, self.state_path, self.concept_registry_path)
        self.send_text(200, html_shell(payload), "text/html; charset=utf-8")

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/api/state":
            self.send_text(404, "not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        data.setdefault("version", STATE_VERSION)
        data.setdefault("items", {})
        data["updated_at"] = now_iso()
        write_json(self.state_path, data)
        self.send_text(200, json.dumps({"ok": True}, ensure_ascii=False), "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # noqa: A002
        sys.stderr.write("rule_review: " + (fmt % args) + "\n")


def cmd_serve(args) -> None:
    projects_root = Path(args.projects_root).resolve()
    state_path = Path(args.state_file).resolve() if args.state_file else projects_root / DEFAULT_STATE_REL
    concept_registry_path = Path(args.concept_registry).resolve() if args.concept_registry else projects_root / DEFAULT_CONCEPT_REGISTRY_REL
    state_path.parent.mkdir(parents=True, exist_ok=True)
    handler = type(
        "BoundRuleReviewHandler",
        (RuleReviewHandler,),
        {"projects_root": projects_root, "state_path": state_path, "concept_registry_path": concept_registry_path, "projects": args.project},
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{server.server_port}"
    print(f"rule_review_url: {url}")
    print(f"rule_review_state: {state_path}")
    print(f"rule_concept_registry: {concept_registry_path}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Generate static cross-project rule review HTML")
    build.add_argument("--projects-root", default="./sql-projects")
    build.add_argument("--output")
    build.add_argument("--state-file")
    build.add_argument("--concept-registry")
    build.add_argument("--project", action="append")
    add_function_gate_arguments(
        build,
        selection_help="Optional explicit rule review function route, such as 【跨项目口径审查】 or [RULE_REVIEW].",
    )
    build.set_defaults(func=cmd_build)

    validate = sub.add_parser("validate", help="Validate explicit rule concept_key coverage")
    validate.add_argument("--projects-root", default="./sql-projects")
    validate.add_argument("--state-file")
    validate.add_argument("--concept-registry")
    validate.add_argument("--project", action="append")
    validate.add_argument("--format", choices=["json"], default="json")
    validate.add_argument("--strict", action="store_true", help="Enable stricter checks; warning exit code remains 2.")
    validate.add_argument("--quiet", action="store_true", help="Suppress non-JSON chatter. JSON output is never suppressed.")
    validate.set_defaults(func=cmd_validate)

    serve = sub.add_parser("serve", help="Serve rule review HTML and persist manual review state")
    serve.add_argument("--projects-root", default="./sql-projects")
    serve.add_argument("--state-file")
    serve.add_argument("--concept-registry")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--project", action="append")
    serve.add_argument("--open", action="store_true")
    add_function_gate_arguments(
        serve,
        selection_help="Optional explicit rule review function route, such as 【跨项目口径审查】 or [RULE_REVIEW].",
    )
    serve.set_defaults(func=cmd_serve)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {"build", "serve"}:
        try:
            purpose = "cross-project rule review"
            require_user_function_selection(
                args.function_selection,
                user_request=args.user_request,
                allowed_ids=command_function_ids("rule_review.py", args.command),
                purpose=purpose,
            )
            require_user_request(args.user_request, purpose=purpose)
        except FunctionGateError as exc:
            exit_with_gate_error(parser, exc)
    args.func(args)


if __name__ == "__main__":
    main()
