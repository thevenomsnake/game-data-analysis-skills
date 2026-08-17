#!/usr/bin/env python3
"""Render the lightweight, searchable SQL query-workspace catalog."""

from __future__ import annotations

import copy
import html
from pathlib import Path
from typing import Any


VIEWER_SHELL_VERSION = "query_workspace_view_v4"


STATUS_LABELS = {
    "draft": "草稿",
    "runnable": "可运行",
    "run_failed": "运行失败",
    "result_confirmed": "结果已确认",
    "discarded": "已废弃",
    "archived": "历史归档",
    "promoted": "已正式保存",
}
CHANGE_TYPE_LABELS = {
    "new": "新查询族",
    "correction": "修正",
    "replacement": "替换",
    "superset": "完整扩张",
    "parameter_refresh": "参数刷新",
    "branch": "独立分支",
    "migration": "历史迁移",
}
COVERAGE_LABELS = {
    "same_contract": "同一问题与口径",
    "strict_superset": "新版完整覆盖旧版",
    "partial_overlap": "部分重叠",
    "different_contract": "不同 Base / 粒度 / 用途",
    "independent": "独立查询",
    "unknown": "历史关系未重建",
}
DERIVED_OUTPUT_LABELS = {
    "result_evidence": "查询结果",
    "analysis_workbook": "分析 Excel",
    "comparison_workbook": "对比 Excel",
    "visualization": "可视化",
    "export": "导出文件",
    "other": "其他产物",
}
USAGE_CLASS_LABELS = {
    "personal_diagnosis": "个人一次性排查",
    "reusable_diagnostic": "通用排查模板",
    "ad_hoc_analysis": "一次性专题分析",
    "reusable_analysis": "可复用分析查询",
    "recurring_delivery": "周期性交付",
    "unclassified": "未分类",
}


def build_workspace_payload(
    root: Path,
    index: dict[str, Any],
    *,
    organization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    organization = organization if isinstance(organization, dict) else {}
    organized_entries = organization.get("entries")
    if not isinstance(organized_entries, dict):
        organized_entries = {}
    items: list[dict[str, Any]] = []
    for raw_entry in index.get("entries", []) if isinstance(index.get("entries"), list) else []:
        if not isinstance(raw_entry, dict):
            continue
        entry = copy.deepcopy(raw_entry)
        entry["organization"] = copy.deepcopy(
            organized_entries.get(str(entry.get("query_id") or ""), {})
        )
        entry["status_label"] = STATUS_LABELS.get(str(entry.get("status") or ""), str(entry.get("status") or ""))
        items.append(entry)
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {
        "schema_version": VIEWER_SHELL_VERSION,
        "project_id": index.get("project_id") or root.name,
        "project_name": index.get("project_name") or root.name,
        "updated_at": index.get("updated_at") or "",
        "status_labels": STATUS_LABELS,
        "change_type_labels": CHANGE_TYPE_LABELS,
        "coverage_labels": COVERAGE_LABELS,
        "derived_output_labels": DERIVED_OUTPUT_LABELS,
        "usage_class_labels": USAGE_CLASS_LABELS,
        "organization_updated_at": organization.get("updated_at") or "",
        "organization_clusters": copy.deepcopy(organization.get("clusters") or []),
        "items": items,
    }


def render_workspace_html(
    root: Path,
    index: dict[str, Any],
    *,
    sql_overrides: dict[str, str] | None = None,
) -> str:
    del root, sql_overrides
    project_name = str(index.get("project_name") or index.get("project_id") or "SQL Project")
    title = html.escape(f"{project_name} SQL 工作台")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="query-workspace-view" content="{VIEWER_SHELL_VERSION}">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f6f8;
      --surface: #ffffff;
      --line: #d9dee5;
      --line-strong: #b9c1cb;
      --text: #1f2933;
      --muted: #66717f;
      --accent: #176b5b;
      --accent-soft: #e7f3ef;
      --warn: #8a5a00;
      --warn-soft: #fff4d6;
      --danger: #a33a32;
      --danger-soft: #faecea;
      --info: #315d8a;
      --info-soft: #e9f1f8;
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-size: 14px; letter-spacing: 0; }}
    button, input, select {{ font: inherit; letter-spacing: 0; }}
    button {{ cursor: pointer; }}
    .topbar {{ height: 58px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 0 20px; background: var(--surface); border-bottom: 1px solid var(--line); }}
    .brand {{ min-width: 0; }}
    .brand h1 {{ margin: 0; font-size: 18px; font-weight: 650; }}
    .brand p {{ margin: 3px 0 0; color: var(--muted); font-size: 12px; }}
    .counts {{ color: var(--muted); white-space: nowrap; }}
    .layout {{ height: calc(100vh - 58px); display: grid; grid-template-columns: minmax(300px, 380px) minmax(0, 1fr); }}
    .sidebar {{ min-width: 0; background: var(--surface); border-right: 1px solid var(--line); display: flex; flex-direction: column; }}
    .filters {{ padding: 14px; border-bottom: 1px solid var(--line); display: grid; gap: 8px; }}
    .filters input, .filters select {{ width: 100%; height: 36px; border: 1px solid var(--line-strong); border-radius: 4px; background: #fff; color: var(--text); padding: 0 10px; }}
    .filter-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
    .filter-row.dates input {{ min-width: 0; }}
    .list {{ min-height: 0; overflow: auto; }}
    .item {{ width: 100%; min-height: 76px; padding: 12px 14px; text-align: left; border: 0; border-bottom: 1px solid #edf0f3; background: #fff; color: inherit; }}
    .item:hover {{ background: #f7f9fa; }}
    .item.active {{ background: var(--accent-soft); box-shadow: inset 3px 0 0 var(--accent); }}
    .item-title {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; font-weight: 650; }}
    .item-title span:first-child {{ min-width: 0; overflow-wrap: anywhere; }}
    .item-purpose {{ margin-top: 6px; color: var(--muted); font-size: 12px; line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .item-meta {{ margin-top: 7px; color: var(--info); font-size: 11px; }}
    .status {{ display: inline-flex; align-items: center; min-height: 22px; padding: 2px 7px; border-radius: 3px; background: #eef1f4; color: #45505d; font-size: 11px; font-weight: 600; white-space: nowrap; }}
    .status.runnable, .status.result_confirmed, .status.promoted {{ background: var(--accent-soft); color: var(--accent); }}
    .status.run_failed {{ background: var(--danger-soft); color: var(--danger); }}
    .status.archived, .status.discarded {{ background: var(--warn-soft); color: var(--warn); }}
    .content {{ min-width: 0; overflow: auto; padding: 22px 26px 44px; }}
    .empty {{ max-width: 680px; margin: 72px auto; color: var(--muted); text-align: center; }}
    .detail {{ max-width: 1180px; margin: 0 auto; }}
    .detail-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding-bottom: 18px; border-bottom: 1px solid var(--line); }}
    .detail-head h2 {{ margin: 0 0 8px; font-size: 24px; line-height: 1.3; overflow-wrap: anywhere; }}
    .detail-head p {{ margin: 0; color: var(--muted); line-height: 1.65; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .action {{ min-height: 36px; border: 1px solid var(--line-strong); border-radius: 4px; background: #fff; color: var(--text); padding: 0 12px; white-space: nowrap; }}
    .action.primary {{ border-color: var(--accent); background: var(--accent); color: #fff; }}
    .section {{ padding: 18px 0; border-bottom: 1px solid var(--line); }}
    .section h3 {{ margin: 0 0 12px; font-size: 15px; }}
    .facts {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 20px; }}
    .fact dt {{ color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
    .fact dd {{ margin: 0; line-height: 1.5; overflow-wrap: anywhere; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .chip {{ padding: 4px 7px; border: 1px solid var(--line); border-radius: 3px; background: #fff; line-height: 1.35; overflow-wrap: anywhere; }}
    .rows {{ display: grid; gap: 7px; }}
    .row {{ display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 14px; line-height: 1.5; }}
    .row-label {{ color: var(--muted); }}
    .version-list {{ display: grid; border: 1px solid var(--line); border-radius: 4px; overflow: hidden; }}
    .version-row {{ display: grid; grid-template-columns: 72px 92px 150px minmax(0, 1fr); gap: 12px; align-items: start; padding: 10px 12px; border-bottom: 1px solid var(--line); line-height: 1.45; }}
    .version-row:last-child {{ border-bottom: 0; }}
    .version-row.current {{ background: var(--accent-soft); }}
    .version-name {{ font-weight: 650; }}
    .current-mark {{ color: var(--accent); font-size: 11px; font-weight: 650; }}
    .output-list {{ display: grid; gap: 8px; }}
    .output-row {{ display: grid; grid-template-columns: 120px minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 4px; background: #fff; }}
    .output-kind {{ color: var(--muted); font-size: 12px; }}
    .output-title {{ font-weight: 650; margin-bottom: 3px; }}
    .output-purpose {{ color: var(--muted); font-size: 12px; line-height: 1.45; }}
    .output-link {{ color: var(--accent); text-decoration: none; font-weight: 650; white-space: nowrap; }}
    .sql-tools {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }}
    .sql-tools select {{ height: 34px; min-width: 150px; border: 1px solid var(--line-strong); border-radius: 4px; background: #fff; padding: 0 8px; }}
    pre {{ margin: 0; max-height: 580px; overflow: auto; padding: 16px; border: 1px solid #2e3742; border-radius: 4px; background: #20262e; color: #e8edf2; font: 12px/1.55 Consolas, "Courier New", monospace; white-space: pre; tab-size: 4; }}
    .read-error {{ color: var(--danger); }}
    .loading {{ color: var(--muted); padding: 10px 0; }}
    .curation {{ border-left: 3px solid var(--info); padding-left: 10px; }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 820px) {{
      .topbar {{ height: auto; min-height: 58px; align-items: flex-start; padding: 12px 14px; }}
      .counts {{ white-space: normal; text-align: right; }}
      .layout {{ height: auto; min-height: calc(100vh - 58px); grid-template-columns: 1fr; }}
      .sidebar {{ max-height: 46vh; border-right: 0; border-bottom: 1px solid var(--line); }}
      .content {{ padding: 18px 14px 36px; }}
      .detail-head {{ display: grid; }}
      .actions {{ justify-content: flex-start; }}
      .facts {{ grid-template-columns: 1fr 1fr; }}
      .row {{ grid-template-columns: 1fr; gap: 3px; }}
      .version-row {{ grid-template-columns: 64px 88px minmax(0, 1fr); }}
      .version-coverage {{ display: none; }}
      .output-row {{ grid-template-columns: 1fr; gap: 5px; }}
    }}
    @media (max-width: 480px) {{ .facts, .filter-row {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand"><h1>{title}</h1><p>临时查询、诊断 SQL 与正式保存来源的动态索引</p></div>
    <div class="counts" id="counts"></div>
  </header>
  <main class="layout">
    <aside class="sidebar">
      <div class="filters">
        <input id="search" type="search" placeholder="搜索用途、指标、日志或筛选" aria-label="搜索 SQL">
        <div class="filter-row">
          <select id="statusFilter" aria-label="按状态筛选"><option value="">全部状态</option></select>
          <select id="categoryFilter" aria-label="按主题筛选"><option value="">全部主题</option></select>
        </div>
        <div class="filter-row">
          <select id="logFilter" aria-label="按原始日志筛选"><option value="">全部原始日志</option></select>
          <select id="metricFilter" aria-label="按指标筛选"><option value="">全部指标</option></select>
        </div>
        <div class="filter-row dates">
          <input id="dateFrom" type="date" aria-label="更新时间从">
          <input id="dateTo" type="date" aria-label="更新时间到">
        </div>
      </div>
      <div class="list" id="list"></div>
    </aside>
    <section class="content" id="content"><div class="empty">选择一条 SQL 查看用途、口径线索和完整代码。</div></section>
  </main>
  <script>
    let payload = {{items: [], status_labels: {{}}, change_type_labels: {{}}, coverage_labels: {{}}, derived_output_labels: {{}}, usage_class_labels: {{}}}};
    const state = {{selectedId: '', version: 0, sqlCache: {{}}}};
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    const arr = value => Array.isArray(value) ? value.filter(Boolean) : [];
    const statusLabel = value => payload.status_labels[value] || value || '未知';
    const changeLabel = value => payload.change_type_labels[value] || value || '未记录';
    const coverageLabel = value => payload.coverage_labels[value] || value || '未记录';
    const outputLabel = value => payload.derived_output_labels[value] || value || '其他产物';
    const usageClassLabel = value => payload.usage_class_labels[value] || value || '未分类';
    const outputStateLabel = value => ({{active:'当前使用',needs_review:'待确认',superseded:'已被替代',discarded:'已废弃'}}[value || 'active'] || value);
    const lineageLabel = value => ({{result_evidence:'原始结果证据',exact_result:'绑定单个准确结果',exact_results:'绑定多个准确结果',deterministic_transform:'基于结果确定性转换',sql_version_only:'仅绑定 SQL 版本',unresolved_legacy:'历史来源待整理'}}[value] || value || '来源未记录');
    const timeCoverageText = output => {{
      if (output.kind !== 'result_evidence') return '';
      const coverage = output.result_time_coverage || {{}};
      const precision = coverage.precision === 'date' ? '（日期粒度）' : coverage.precision === 'datetime' ? '（时间戳粒度）' : '';
      if (coverage.actual_start || coverage.actual_end) return ` · 实际范围${{precision}}：${{coverage.actual_start || '未知'}} 至 ${{coverage.actual_end || '未知'}}`;
      if (coverage.required) return ' · 实际范围：待核对';
      return '';
    }};
    const unique = values => [...new Set(values.filter(Boolean))].sort((a,b) => String(a).localeCompare(String(b), 'zh-CN'));
    const logKey = value => String(value || '').split('【')[0].trim();
    const compactLog = value => {{
      const text = String(value || '').trim();
      const match = text.match(/^([^【]+)【([^】]+)】/);
      if (!match) return text;
      const shortName = match[2].split(/[。.]/)[0].trim();
      return `${{match[1].trim()}}【${{shortName}}】`;
    }};
    const topic = item => item.organization?.business_topic || item.business_category || '未分类';
    const summary = item => item.organization?.summary || item.purpose || '';
    const searchText = item => [item.query_id,item.title,item.purpose,item.business_question,item.status,item.change_type,item.coverage_relation,topic(item),item.analysis_type,item.usage_class,usageClassLabel(item.usage_class),item.grain,item.time_grain,item.organization?.curation_state,item.organization?.notes,...arr(item.source_logs),...arr(item.tables),...arr(item.metrics),...arr(item.dimensions),...arr(item.filters),...arr(item.tags),...arr(item.organization?.tags),...arr(item.versions).flatMap(v => [v.change_summary,v.change_type,v.coverage_relation,...arr(v.derived_outputs).flatMap(o => [o.title,o.purpose,o.kind,o.original_file_name])])].join(' ').toLowerCase();
    function fillSelect(id, values, labelFn = x => x) {{
      const select = $(id);
      select.length = 1;
      for (const value of unique(values)) select.insertAdjacentHTML('beforeend', `<option value="${{esc(value)}}">${{esc(labelFn(value))}}</option>`);
    }}
    function configureFilters() {{
      fillSelect('statusFilter', payload.items.map(x => x.status), statusLabel);
      fillSelect('categoryFilter', payload.items.map(topic));
      const logLabels = {{}};
      payload.items.flatMap(x => arr(x.source_logs)).forEach(value => {{
        const key = logKey(value);
        const label = compactLog(value);
        if (key && (!logLabels[key] || label.length < logLabels[key].length)) logLabels[key] = label;
      }});
      fillSelect('logFilter', Object.keys(logLabels), value => logLabels[value]);
      fillSelect('metricFilter', payload.items.flatMap(x => arr(x.metrics)));
      $('counts').textContent = `${{payload.items.length}} 条 SQL · 更新于 ${{payload.updated_at || '未知时间'}}`;
    }}
    function filteredItems() {{
      const query = $('search').value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
      const status = $('statusFilter').value;
      const category = $('categoryFilter').value;
      const log = $('logFilter').value;
      const metric = $('metricFilter').value;
      const dateFrom = $('dateFrom').value;
      const dateTo = $('dateTo').value;
      return payload.items.filter(item => {{
        const text = searchText(item);
        const updated = String(item.updated_at || '').slice(0, 10);
        return (!status || item.status === status)
          && (!category || topic(item) === category)
          && (!log || arr(item.source_logs).some(value => logKey(value) === log))
          && (!metric || arr(item.metrics).includes(metric))
          && (!dateFrom || updated >= dateFrom)
          && (!dateTo || updated <= dateTo)
          && query.every(token => text.includes(token));
      }});
    }}
    function renderList() {{
      const items = filteredItems();
      $('list').innerHTML = items.length ? items.map(item => `<button class="item ${{state.selectedId === item.query_id ? 'active' : ''}}" data-id="${{esc(item.query_id)}}"><div class="item-title"><span>${{esc(item.title)}}</span><span class="status ${{esc(item.status)}}">${{esc(statusLabel(item.status))}}</span></div><div class="item-purpose">${{esc(summary(item))}}</div><div class="item-meta">${{esc(topic(item))}} · ${{arr(item.source_logs).length}} 个原始日志</div></button>`).join('') : '<div class="empty">没有匹配的 SQL。</div>';
      $('list').querySelectorAll('[data-id]').forEach(node => node.addEventListener('click', () => selectItem(node.dataset.id)));
      if (items.length && !items.some(item => item.query_id === state.selectedId)) selectItem(items[0].query_id, false);
      if (!items.length) $('content').innerHTML = '<div class="empty">调整筛选条件后再试。</div>';
    }}
    function chips(values) {{ return arr(values).length ? `<div class="chips">${{arr(values).map(x => `<span class="chip">${{esc(x)}}</span>`).join('')}}</div>` : '<span class="muted">无</span>'; }}
    function rows(values, label) {{ return arr(values).length ? `<div class="rows">${{arr(values).map(x => `<div class="row"><div class="row-label">${{esc(label)}}</div><div>${{esc(x)}}</div></div>`).join('')}}</div>` : '<span class="muted">无</span>'; }}
    function selectedVersion(item) {{
      const versions = arr(item.versions);
      return versions.find(v => Number(v.version) === Number(state.version)) || versions.find(v => Number(v.version) === Number(item.current_version)) || versions.at(-1) || {{}};
    }}
    function selectItem(id, rerenderList = true) {{
      state.selectedId = id;
      const item = payload.items.find(x => x.query_id === id);
      if (!item) return;
      state.version = Number(item.current_version || arr(item.versions).at(-1)?.version || 0);
      renderDetail(item);
      if (rerenderList) renderList();
    }}
    function renderDetail(item) {{
      const version = selectedVersion(item);
      const source = version.source_intake || item.source_intake || {{}};
      const legacyRef = arr(version.legacy_source_refs).at(0) || arr(item.legacy_source_refs).at(0) || {{}};
      const sourceLabel = source.legacy_source_path || legacyRef.legacy_source_path || source.source_project_path || source.original_file_name || item.generation_provenance?.source || 'skill 生成/项目内创建';
      const versionOptions = arr(item.versions).map(v => `<option value="${{Number(v.version)}}" ${{Number(v.version) === Number(version.version) ? 'selected' : ''}}>v${{String(v.version).padStart(3,'0')}} · ${{esc(changeLabel(v.change_type))}}</option>`).join('');
      const branch = version.branch_of && Object.keys(version.branch_of).length ? version.branch_of : (item.branch_of || {{}});
      const branchLabel = branch.query_id ? `${{branch.query_id}} v${{String(branch.version || '').padStart(3,'0')}}` : '无';
      const versionHistory = arr(item.versions).slice().reverse().map(v => {{
        const current = Number(v.version) === Number(item.current_version);
        return `<div class="version-row ${{current ? 'current' : ''}}"><div><div class="version-name">v${{String(v.version).padStart(3,'0')}}</div>${{current ? '<div class="current-mark">唯一当前</div>' : ''}}</div><div>${{esc(changeLabel(v.change_type))}}</div><div class="version-coverage">${{esc(coverageLabel(v.coverage_relation))}}</div><div>${{esc(v.change_summary || '未记录变化说明')}}</div></div>`;
      }}).join('');
      const renderOutput = output => {{
        const href = String(output.path || '').startsWith('query_workspace/') ? `/api/query-workspace/output?path=${{encodeURIComponent(output.path)}}` : '';
        const stateLabel = outputStateLabel(output.asset_state || 'active');
        const sourceCount = arr(output.source_results).length;
        const sourceText = sourceCount ? `${{sourceCount}} 个结果来源` : lineageLabel(output.lineage_status);
        const reason = output.state_reason ? ` · ${{esc(output.state_reason)}}` : '';
        return `<div class="output-row"><div class="output-kind">${{esc(outputLabel(output.kind))}}<br><span class="muted">${{esc(stateLabel)}}</span></div><div><div class="output-title">${{esc(output.title)}}</div><div class="output-purpose">${{esc(output.purpose)}} · ${{esc(sourceText)}}${{timeCoverageText(output)}}${{reason}}</div></div>${{href ? `<a class="output-link" href="${{href}}" target="_blank" rel="noopener">打开文件</a>` : '<span class="muted">路径不可用</span>'}}</div>`;
      }};
      const activeOutputs = arr(version.derived_outputs).filter(output => !['superseded','discarded'].includes(output.asset_state || 'active'));
      const historicalOutputs = arr(version.derived_outputs).filter(output => ['superseded','discarded'].includes(output.asset_state));
      const outputRows = activeOutputs.map(renderOutput).join('');
      const historicalOutputRows = historicalOutputs.map(renderOutput).join('');
      const temporaryOverride = version.temporary_rule_override || item.temporary_rule_override || {{}};
      const temporaryOverrideSection = temporaryOverride.enabled ? `<section class="section curation"><h3>本次临时口径例外</h3><div class="rows"><div class="row"><div class="row-label">用户确认</div><div>${{esc(temporaryOverride.user_instruction || '已确认仅本查询使用')}}</div></div><div class="row"><div class="row-label">冲突口径</div><div>${{chips(temporaryOverride.conflicted_rule_ids)}}</div></div><div class="row"><div class="row-label">冲突原因</div><div>${{rows(temporaryOverride.conflict_reasons, '原因')}}</div></div><div class="row"><div class="row-label">后续处理</div><div>canonical rule 未修改；正式固化前必须通过 RULES 或 SKILL_EVOLUTION 解决。</div></div></div></section>` : '';
      $('content').innerHTML = `<div class="detail">
        <div class="detail-head"><div><h2>${{esc(item.title)}}</h2><p>${{esc(summary(item))}}</p></div><div class="actions"><button class="action" id="copyPath">复制路径</button><button class="action primary" id="copySql">复制完整 SQL</button></div></div>
        <section class="section"><h3>快速理解</h3><dl class="facts">
          <div class="fact"><dt>状态</dt><dd><span class="status ${{esc(item.status)}}">${{esc(statusLabel(item.status))}}</span></dd></div>
          <div class="fact"><dt>业务主题</dt><dd>${{esc(topic(item))}}</dd></div>
          <div class="fact"><dt>分析类型</dt><dd>${{esc(item.analysis_type || '未分类')}}</dd></div>
          <div class="fact"><dt>资产价值</dt><dd>${{esc(usageClassLabel(item.usage_class || 'unclassified'))}}</dd></div>
          <div class="fact"><dt>当前版本</dt><dd>v${{String(item.current_version || 0).padStart(3,'0')}}（唯一当前）</dd></div>
          <div class="fact"><dt>本次变化</dt><dd>${{esc(changeLabel(version.change_type || item.change_type))}}</dd></div>
          <div class="fact"><dt>覆盖关系</dt><dd>${{esc(coverageLabel(version.coverage_relation || item.coverage_relation))}}</dd></div>
          <div class="fact"><dt>结果粒度</dt><dd>${{esc(item.grain || '未识别')}}</dd></div>
          <div class="fact"><dt>时间粒度</dt><dd>${{esc(item.time_grain || 'none')}}</dd></div>
          <div class="fact"><dt>来源</dt><dd>${{esc(sourceLabel)}}</dd></div>
        </dl></section>
        ${{item.organization && Object.keys(item.organization).length ? `<section class="section curation"><h3>整理结论</h3><div class="rows"><div class="row"><div class="row-label">整理状态</div><div>${{esc(item.organization.curation_state || '未标记')}}</div></div><div class="row"><div class="row-label">依据</div><div>${{esc(item.organization.notes || '无额外说明')}}</div></div></div></section>` : ''}}
        ${{temporaryOverrideSection}}
        <section class="section"><h3>指标与维度</h3><div class="rows"><div class="row"><div class="row-label">指标</div><div>${{chips(item.metrics)}}</div></div><div class="row"><div class="row-label">维度</div><div>${{chips(item.dimensions)}}</div></div></div></section>
        <section class="section"><h3>日志与筛选</h3><div class="rows"><div class="row"><div class="row-label">原始日志</div><div>${{chips(unique(arr(item.source_logs).map(compactLog)))}}</div></div><div class="row"><div class="row-label">关键筛选</div><div>${{rows(item.filters, '条件')}}</div></div></div></section>
        <section class="section"><h3>生命周期</h3><div class="rows"><div class="row"><div class="row-label">查询族 ID</div><div>${{esc(item.query_id)}}</div></div><div class="row"><div class="row-label">当前文件</div><div>${{esc(item.current_path)}}</div></div><div class="row"><div class="row-label">分支来源</div><div>${{esc(branchLabel)}}</div></div><div class="row"><div class="row-label">正式资产</div><div>${{chips(item.formal_artifacts)}}</div></div></div></section>
        <section class="section"><h3>版本演进</h3><div class="version-list">${{versionHistory}}</div></section>
        <section class="section"><h3>结果与派生产物</h3>${{outputRows ? `<div class="output-list">${{outputRows}}</div>` : '<span class="muted">当前版本暂无活跃结果、Excel 或可视化。</span>'}}${{historicalOutputRows ? `<details><summary>查看已替代 / 已废弃产物（${{historicalOutputs.length}}）</summary><div class="output-list">${{historicalOutputRows}}</div></details>` : ''}}</section>
        <section class="section"><div class="sql-tools"><h3>完整 SQL</h3><select id="versionSelect" aria-label="选择 SQL 版本">${{versionOptions}}</select></div><p class="read-error" id="sqlError" hidden></p><pre id="sqlText">正在读取当前版本...</pre></section>
      </div>`;
      $('versionSelect').addEventListener('change', event => {{ state.version = Number(event.target.value); renderDetail(item); }});
      $('copySql').addEventListener('click', async () => {{
        const current = selectedVersion(item);
        const sql = await loadVersionSql(current);
        if (sql !== null) await copyText(sql, $('copySql'), '已复制 SQL');
      }});
      $('copyPath').addEventListener('click', () => copyText(selectedVersion(item).path || item.current_path || '', $('copyPath'), '已复制路径'));
      loadVersionSql(version, true);
    }}
    async function loadVersionSql(version, render = false) {{
      const path = String(version?.path || '');
      if (!path) return null;
      if (Object.prototype.hasOwnProperty.call(state.sqlCache, path)) {{
        if (render && $('sqlText')) $('sqlText').textContent = state.sqlCache[path];
        return state.sqlCache[path];
      }}
      try {{
        const response = await fetch(`/api/query-workspace/sql?path=${{encodeURIComponent(path)}}`, {{cache: 'no-store'}});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${{response.status}}`);
        state.sqlCache[path] = data.sql || '';
        if (render && selectedVersion(payload.items.find(x => x.query_id === state.selectedId) || {{}}).path === path && $('sqlText')) $('sqlText').textContent = state.sqlCache[path];
        return state.sqlCache[path];
      }} catch (error) {{
        if (render && $('sqlError')) {{ $('sqlError').hidden = false; $('sqlError').textContent = `SQL 读取失败：${{error.message}}`; $('sqlText').textContent = ''; }}
        return null;
      }}
    }}
    async function copyText(value, button, doneLabel) {{
      const original = button.textContent;
      try {{
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
        else {{ const area = document.createElement('textarea'); area.value = value; document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove(); }}
        button.textContent = doneLabel;
      }} catch (_) {{ button.textContent = '复制失败'; }}
      setTimeout(() => button.textContent = original, 1200);
    }}
    async function loadPayload() {{
      if (window.location.protocol === 'file:') {{
        $('content').innerHTML = '<div class="empty">此页面改为动态索引。请运行 <code>python sql-engineering/scripts/query_workspace_maintenance.py serve --root &lt;project-root&gt;</code> 后访问终端给出的地址。</div>';
        $('counts').textContent = '需要本地服务';
        return;
      }}
      try {{
        const response = await fetch('/api/query-workspace', {{cache: 'no-store'}});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${{response.status}}`);
        payload = data;
        configureFilters();
        renderList();
      }} catch (error) {{
        $('content').innerHTML = `<div class="empty">索引加载失败：${{esc(error.message)}}</div>`;
        $('counts').textContent = '加载失败';
      }}
    }}
    ['search','statusFilter','categoryFilter','logFilter','metricFilter','dateFrom','dateTo'].forEach(id => $(id).addEventListener(id === 'search' ? 'input' : 'change', renderList));
    loadPayload();
  </script>
</body>
</html>
"""
