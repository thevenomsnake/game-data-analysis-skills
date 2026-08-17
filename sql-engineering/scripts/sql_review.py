#!/usr/bin/env python3
"""Batch review project-aware SQL files and write Markdown reports next to them."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sql_project import (  # noqa: E402
    DEFAULT_ANALYSIS_TYPE,
    DEFAULT_BUSINESS_CATEGORY,
    REVERSE_AUDIT_SHARED_LOGS,
    build_intent_frame,
    contract_event_signature,
    event_signature_match,
    extract_sql_evidence,
    extract_tables,
    extract_target_tables,
    infer_analysis_type,
    infer_business_category,
    infer_grain,
    infer_time_grain,
    is_metric_expression as base_is_metric_expression,
    scan_intent_source,
    split_top_level_csv,
    strip_sql_comments,
    unique_in_order,
)
from project_rules import has_v2_store, load_rules, select_rule_records  # noqa: E402
from function_gate import (  # noqa: E402
    FunctionGateError,
    add_function_gate_arguments,
    exit_with_gate_error,
    require_user_request,
    require_user_function_selection,
)
from performance_preflight import analyze_performance  # noqa: E402
from capability_registry import command_function_ids  # noqa: E402
from config_knowledge import resolve_knowledge  # noqa: E402
from sql_facts import build_sql_fact_bundle, sql_side_privacy_transforms  # noqa: E402
from sql_review_evidence import build_evidence_bundle  # noqa: E402
from sql_review_product_agent import generate_product_view, generate_product_views_batch  # noqa: E402


DEFAULT_INBOX_ROOT = Path("sql-projects") / "_review_inbox"
RESULT_EXTENSIONS = {".csv", ".txt", ".xlsx"}
RESULT_PRIORITY = {".xlsx": 0, ".csv": 1, ".txt": 2}
DEFAULT_SAMPLE_ROWS = 5
REVIEW_DIMENSIONS = [
    ("logic", "逻辑/口径"),
    ("code_quality", "代码质量"),
    ("evidence", "结果证据"),
    ("dashboard_fit", "看板适配"),
    ("deployment_gate", "部署门禁"),
]
REVIEW_CARD_SCHEMA_VERSION = "review_card_v1"
SQL_REVIEW_SCHEMA_VERSION = "sql_review_v14"
MAX_REVIEW_CARD_ISSUES = 4
MAX_REVIEW_CARD_STEPS = 4
MAX_MARKDOWN_SHARED_RULES = 30


def default_product_review_command() -> str:
    env_command = os.environ.get("SQL_REVIEW_PRODUCT_AGENT_COMMAND", "").strip()
    if env_command:
        return env_command
    if os.environ.get("SQL_REVIEW_DISABLE_CODEX_AGENT", "").strip() in {"1", "true", "TRUE", "yes"}:
        return ""
    if not shutil.which("codex"):
        return ""
    wrapper = SCRIPT_DIR / "sql_review_codex_product_agent.py"
    if not wrapper.exists():
        return ""
    return f'"{sys.executable}" "{wrapper}"'
MAX_MARKDOWN_UNIQUE_RULES_PER_SQL = 12

METRIC_ALIAS_PATTERNS = [
    r"^(?:dau|mau|wau|acu|pcu)$",
    r"(?:^|_)(?:cnt|count|uv|pv)$",
    r"(?:_cnt|_count|_uv|_pv)$",
    r"(?:_rate|_ratio|_pct|_percent)$",
    r"^(?:avg|sum|min|max|p\d{1,2})_",
    r"(?:_avg|_sum)$",
    r"(?:_users|_players)$",
    r"(?:人数|用户数|玩家数|次数|数量|个数|人均|占比|比例|比率|百分比|转化率|完成率|留存率|渗透率|组队率|时长|耗时|均值|平均|分位)",
]
DIMENSION_ALIAS_PATTERNS = [
    r"(?:^|_)(?:date|day|hour|month|week)$",
    r"(?:_date|_day|_hour|_month|_week)$",
    r"(?:_id|_name|_type|_level|_bucket|_flag|_order|_no|_idx|_index)$",
    r"(?:^|_)(?:mode|status|result|segment|group|category)$",
    r"(?:日期|时间|人群|类型|类别|名称|模式|等级|标签|分组|分桶|区间|阶段|步骤|排序)",
    r"^is_",
    r"(?:^|_)team_num$",
    r"(?:^|_)team_number$",
    r"^step_(?:order|name)$",
]


@dataclass
class RuleCandidate:
    kind: str
    text: str
    normalized: str


@dataclass
class QualityFinding:
    severity: str
    message: str


@dataclass
class ResultFileReview:
    path: Path
    file_type: str
    row_count: int | None
    columns: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, str]] = field(default_factory=list)
    status: str = "missing"
    note: str = ""
    alternatives: list[Path] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)
    order_mismatch: bool = False


@dataclass
class Constraint:
    field: str
    operator: str
    values: tuple[str, ...]


@dataclass
class CanonicalRule:
    rule_id: str
    concept_key: str
    version: int
    status: str
    title: str
    content: str
    applies_to: str
    normalized: str
    constraints: list[Constraint]
    tokens: set[str]
    raw: dict = field(default_factory=dict)


@dataclass
class RuleCheck:
    rule_id: str
    status: str
    result: str
    message: str
    evidence: str
    concept_key: str = ""
    title: str = ""
    rule_summary: str = ""


@dataclass
class ProjectContext:
    root: Path | None
    project_id: str
    display_name: str
    sql_dialect: str
    query_engine: str
    query_environment: str
    dashboard_application: str
    table_profile_name: str
    table_pattern: str
    table_database: str
    table_overrides: dict[str, str] = field(default_factory=dict)
    canonical_rules: list[CanonicalRule] = field(default_factory=list)
    config: dict = field(default_factory=dict)


@dataclass
class ReviewRoleContext:
    definition: ProjectContext | None = None
    delivery: ProjectContext | None = None
    execution_projects: list[ProjectContext] = field(default_factory=list)
    known_projects: list[ProjectContext] = field(default_factory=list)
    file_role_map: dict = field(default_factory=dict)
    execution_selection_explicit: bool = False


@dataclass
class ExecutionInference:
    project: ProjectContext | None
    confidence: str
    reason: str
    source: str = "auto"


@dataclass
class FileReview:
    path: Path
    sql: str
    tables: list[str]
    target_tables: list[str]
    metrics: list[str]
    dimensions: list[str]
    business_category: str
    analysis_type: str
    grain: str
    time_grain: str
    parameters: list[str]
    final_fields: list[str]
    cte_count: int
    join_count: int
    has_count_distinct: bool
    has_window_function: bool
    has_global_order_by: bool
    sql_facts: dict = field(default_factory=dict)
    rules: list[RuleCandidate] = field(default_factory=list)
    findings: list[QualityFinding] = field(default_factory=list)
    rule_checks: list[RuleCheck] = field(default_factory=list)
    business_filters: list[dict] = field(default_factory=list)
    business_filter_mappings: dict[str, dict[str, str]] = field(default_factory=dict)
    result_file: ResultFileReview | None = None
    execution_project: ProjectContext | None = None
    execution_inference_confidence: str = "unknown"
    execution_inference_reason: str = ""
    execution_inference_source: str = "auto"
    review_stage: str = "pure_sql"
    evidence_status: str = "missing_result_file"
    query_review_status: str = "needs_result_file"
    deployment_readiness: str = "blocked"
    deployment_notes: list[str] = field(default_factory=list)
    delivery_table_mismatches: list[str] = field(default_factory=list)
    checked_concept_keys: list[str] = field(default_factory=list)
    proxy_limitations: list[str] = field(default_factory=list)
    future_target_verification_plan: str = ""
    grade: str = "A"
    performance_preflight: dict = field(default_factory=dict)
    product_view: dict = field(default_factory=dict)
    product_review_evidence: dict = field(default_factory=dict)
    execution_evidence: dict = field(default_factory=dict)
    result_pairing_method: str = ""


@dataclass
class MetricBusinessContext:
    definitions: dict[str, str] = field(default_factory=dict)
    cte_comments: dict[str, str] = field(default_factory=dict)
    cte_lineage: dict[str, dict] = field(default_factory=dict)
    source_aliases: dict[str, str] = field(default_factory=dict)
    bucket_definitions: dict[str, str] = field(default_factory=dict)
    base_description: str = ""
    duration_logic: str = ""
    output_description: str = ""
    title: str = ""
    comment_lines: list[str] = field(default_factory=list)


@dataclass
class ProductConcepts:
    base: str = ""
    conclusion: str = ""
    scope: list[str] = field(default_factory=list)
    logic_steps: list[str] = field(default_factory=list)
    walkthrough_sections: list[dict] = field(default_factory=list)
    filter_cards: list[dict] = field(default_factory=list)
    metric_overrides: dict[str, dict] = field(default_factory=dict)
    review_checks: list[str] = field(default_factory=list)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_sql(path: Path) -> str:
    for encoding in ["utf-8-sig", "utf-8", "gb18030"]:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def read_text_file(path: Path) -> str:
    for encoding in ["utf-8-sig", "utf-8", "gb18030"]:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def detect_delimiter(path: Path, default: str = ",") -> str:
    for line in read_text_file(path).splitlines():
        if line.strip():
            return "\t" if "\t" in line else ","
    return default


def read_delimited_result(path: Path, delimiter: str, sample_limit: int) -> ResultFileReview:
    rows = []
    text = read_text_file(path)
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    try:
        columns = [str(item).strip() for item in next(reader)]
    except StopIteration:
        return ResultFileReview(path=path, file_type=path.suffix.lower().lstrip("."), row_count=0, status="empty")
    row_count = 0
    for values in reader:
        if not values or not any(str(value).strip() for value in values):
            continue
        row_count += 1
        if len(rows) < sample_limit:
            rows.append({columns[index] if index < len(columns) else f"col_{index + 1}": str(value) for index, value in enumerate(values)})
    return ResultFileReview(
        path=path,
        file_type=path.suffix.lower().lstrip("."),
        row_count=row_count,
        columns=columns,
        sample_rows=rows,
        status="loaded",
    )


def xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        xml = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml)
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = []
    for item in root.findall("x:si", ns):
        texts = [node.text or "" for node in item.findall(".//x:t", ns)]
        strings.append("".join(texts))
    return strings


def excel_col_index(reference: str) -> int:
    letters = re.match(r"([A-Z]+)", reference.upper())
    if not letters:
        return 0
    value = 0
    for char in letters.group(1):
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def result_cell_text(value: str) -> str:
    return str(value or "").strip()


def result_row_width(values: list[str]) -> int:
    width = len(values)
    while width > 0 and not result_cell_text(values[width - 1]):
        width -= 1
    return width


def result_row_non_empty(values: list[str]) -> int:
    return sum(1 for value in values if result_cell_text(value))


def result_row_text_tokens(values: list[str]) -> int:
    return sum(1 for value in values if re.search(r"[A-Za-z_\u4e00-\u9fff]", result_cell_text(value)))


def choose_xlsx_header_index(matrix: list[list[str]]) -> int:
    search_rows = min(len(matrix), 20)
    best_index = 0
    best_score = (-1, -1, 0)
    for index in range(search_rows):
        values = matrix[index]
        non_empty = result_row_non_empty(values)
        if non_empty <= 1:
            continue
        text_tokens = result_row_text_tokens(values)
        width = result_row_width(values)
        score = (non_empty, text_tokens, width)
        if score > best_score:
            best_index = index
            best_score = score
    return best_index


def unique_result_columns(columns: list[str]) -> list[str]:
    result: list[str] = []
    counts: dict[str, int] = defaultdict(int)
    for index, column in enumerate(columns):
        name = result_cell_text(column) or f"col_{index + 1}"
        counts[name] += 1
        if counts[name] > 1:
            name = f"{name}_{counts[name]}"
        result.append(name)
    return result


def xlsx_display_columns(matrix: list[list[str]], header_index: int) -> list[str]:
    width = max(result_row_width(row) for row in matrix[header_index : header_index + 1] or matrix)
    header_start = max(0, header_index - 3)
    header_rows = matrix[header_start : header_index + 1]
    filled_rows: list[list[str]] = []
    for row in header_rows:
        filled: list[str] = []
        current = ""
        for index in range(width):
            value = result_cell_text(row[index] if index < len(row) else "")
            if value:
                current = value
            filled.append(current)
        filled_rows.append(filled)
    columns: list[str] = []
    for index in range(width):
        parts = unique_in_order(
            [
                filled[index]
                for filled in filled_rows
                if index < len(filled) and filled[index] and filled[index] != "总计"
            ]
        )
        columns.append(" / ".join(parts[-4:]) if parts else f"col_{index + 1}")
    return unique_result_columns(columns)


def read_xlsx_result(path: Path, sample_limit: int) -> ResultFileReview:
    with zipfile.ZipFile(path) as zf:
        shared = xlsx_shared_strings(zf)
        sheet_names = [name for name in zf.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")]
        if not sheet_names:
            return ResultFileReview(path=path, file_type="xlsx", row_count=0, status="empty", note="No worksheets found.")
        root = ET.fromstring(zf.read(sorted(sheet_names)[0]))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    matrix: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", ns):
        values: list[str] = []
        for cell in row.findall("x:c", ns):
            index = excel_col_index(cell.attrib.get("r", ""))
            while len(values) <= index:
                values.append("")
            cell_type = cell.attrib.get("t")
            value_node = cell.find("x:v", ns)
            if cell_type == "inlineStr":
                text_node = cell.find(".//x:t", ns)
                values[index] = text_node.text if text_node is not None else ""
            elif value_node is None:
                values[index] = ""
            elif cell_type == "s":
                shared_index = int(value_node.text or 0)
                values[index] = shared[shared_index] if shared_index < len(shared) else ""
            else:
                values[index] = value_node.text or ""
        if values and any(value.strip() for value in values):
            matrix.append(values)
    if not matrix:
        return ResultFileReview(path=path, file_type="xlsx", row_count=0, status="empty")
    header_index = choose_xlsx_header_index(matrix)
    columns = xlsx_display_columns(matrix, header_index)
    sample_rows = []
    for values in matrix[header_index + 1 : header_index + 1 + sample_limit]:
        sample_rows.append({columns[index]: values[index] if index < len(values) else "" for index in range(len(columns))})
    return ResultFileReview(
        path=path,
        file_type="xlsx",
        row_count=max(0, len(matrix) - header_index - 1),
        columns=columns,
        sample_rows=sample_rows,
        status="loaded",
    )


def read_result_file(path: Path, sample_limit: int) -> ResultFileReview:
    suffix = path.suffix.lower()
    try:
        if suffix == ".xlsx":
            return read_xlsx_result(path, sample_limit)
        if suffix == ".txt":
            return read_delimited_result(path, detect_delimiter(path), sample_limit)
        if suffix == ".csv":
            return read_delimited_result(path, ",", sample_limit)
    except Exception as exc:  # noqa: BLE001
        return ResultFileReview(
            path=path,
            file_type=suffix.lstrip("."),
            row_count=None,
            status="read_error",
            note=str(exc),
        )
    return ResultFileReview(path=path, file_type=suffix.lstrip("."), row_count=None, status="unsupported")


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).strip()


def clip_text(value: str, limit: int = 240) -> str:
    text = compact(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def normalize_business_key(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = text.replace("比例", "rate").replace("占比", "rate").replace("比率", "rate")
    text = text.replace("数量", "cnt").replace("人数", "user_cnt").replace("用户数", "user_cnt")
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", text)


def business_key_variants(value: str) -> list[str]:
    raw = value.strip().strip("`").strip('"').strip("'")
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    candidates = [raw]
    without_paren = re.sub(r"[（(].*?[）)]", "", raw).strip()
    if without_paren and without_paren != raw:
        candidates.append(without_paren)
    suffixes = [
        "_ratio",
        "_rate",
        "_pct",
        "_percent",
        "_cnt",
        "_count",
        "_user_cnt",
        "_user_count",
        "_num",
    ]
    for suffix in suffixes:
        if raw.lower().endswith(suffix):
            candidates.append(raw[: -len(suffix)])
    if raw.lower().endswith("_cnt"):
        candidates.append(raw[: -len("_cnt")] + "_user_cnt")
        candidates.append(raw[: -len("_cnt")] + "_count")
    if raw.lower().endswith("_count"):
        candidates.append(raw[: -len("_count")] + "_cnt")
        candidates.append(raw[: -len("_count")] + "_user_cnt")
    if raw.lower().endswith("_user_cnt"):
        candidates.append(raw[: -len("_user_cnt")] + "_cnt")
        candidates.append(raw[: -len("_user_cnt")] + "_count")
    chinese_suffixes = [
        "用户数量",
        "用户总量",
        "用户数",
        "玩家数量",
        "玩家数",
        "人数",
        "数量",
        "总量",
        "占比",
        "比例",
        "比率",
        "率",
    ]
    for suffix in chinese_suffixes:
        if raw.endswith(suffix) and len(raw) > len(suffix):
            candidates.append(raw[: -len(suffix)])
    if "_" in raw:
        candidates.append(raw.replace("_", ""))
    return unique_in_order(key for key in (normalize_business_key(item) for item in candidates) if key)


def clean_comment_line(line: str) -> str:
    text = line.strip()
    text = re.sub(r"^/\*+", "", text)
    text = re.sub(r"\*/$", "", text)
    text = re.sub(r"^\s*\*+", "", text)
    text = re.sub(r"^\s*--\s?", "", text)
    text = text.strip()
    if not text or re.fullmatch(r"[=\-_*<>◀▶\s]+", text):
        return ""
    return text


def extract_comment_lines(sql: str) -> list[str]:
    lines: list[str] = []
    for match in re.finditer(r"/\*(.*?)\*/", sql, flags=re.S):
        for raw_line in match.group(1).splitlines():
            cleaned = clean_comment_line(raw_line)
            if cleaned:
                lines.append(cleaned)
    for raw_line in re.findall(r"--[^\n\r]*", sql):
        cleaned = clean_comment_line(raw_line)
        if cleaned:
            lines.append(cleaned)
    return unique_in_order(lines)


def strip_comment_numbering(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^[\-*•·]\s*", "", text)
    text = re.sub(r"^\d+\s*[.、]\s*", "", text)
    return text.strip()


def split_comment_definition(line: str) -> tuple[str, str]:
    text = strip_comment_numbering(line)
    match = re.match(r"^(.{1,80}?)(?:\s*(?:=|：|:)\s*)(.+)$", text)
    if not match:
        return "", ""
    label = match.group(1).strip(" -\t")
    description = compact(match.group(2))
    if not label or not description:
        return "", ""
    return label, description


def inline_business_definition_parts(text: str) -> list[tuple[str, str]]:
    cleaned = strip_comment_numbering(text)
    cleaned = re.sub(
        r"^(?:指标口径|指标定义|指标说明|输出指标|口径|分类规则|分类口径|业务规则)\s*[：:]\s*",
        "",
        cleaned,
        flags=re.I,
    )
    parts = [part.strip() for part in re.split(r"[;；]\s*", cleaned) if part.strip()]
    definitions: list[tuple[str, str]] = []
    for part in parts:
        match = re.match(
            r"^\s*[-•]?\s*([A-Za-z_\u4e00-\u9fff][\w/\u4e00-\u9fff-]*)(?:\s*[（(]([^）)]+)[）)])?\s*(?:=|：|:)\s*(.+)$",
            part,
            flags=re.I,
        )
        if not match:
            continue
        label = match.group(1).strip()
        label_note = (match.group(2) or "").strip()
        description = compact(match.group(3))
        if not label or not description:
            continue
        definitions.append((label, description))
        if label_note:
            definitions.append((label_note, description))
    return definitions


def comment_section_definitions(comment_lines: list[str]) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_label = ""
    current_items: list[str] = []

    def flush() -> None:
        nonlocal current_label, current_items
        if current_label and current_items:
            sections.append((current_label, "；".join(current_items[:8])))
        current_label = ""
        current_items = []

    for raw_line in comment_lines:
        text = strip_comment_numbering(raw_line).strip()
        if not text:
            continue
        label, description = split_comment_definition(text)
        if label and description:
            flush()
            sections.append((label, description))
            continue
        heading = re.match(r"^([A-Za-z0-9_\u4e00-\u9fff /（）()&×+\-]{1,60})[：:]\s*$", text)
        if heading:
            flush()
            current_label = heading.group(1).strip()
            continue
        if current_label:
            current_items.append(text)
    flush()
    return sections


def add_business_definition(definitions: dict[str, str], label: str, description: str) -> None:
    label = label.strip()
    description = clip_text(description, 500)
    if not label or not description:
        return
    ignored_labels = {
        "业务说明",
        "创建日期",
        "目标平台",
        "数据库",
        "时间",
        "时间范围",
        "分区范围",
        "统计粒度",
        "说明",
        "指标",
    }
    if label in ignored_labels:
        return
    for key in business_key_variants(label):
        definitions.setdefault(key, description)
    match = re.match(r"^([A-Za-z][\w/]+)\s*[（(]([^）)]+)[）)]$", label)
    if match:
        definitions.setdefault(normalize_business_key(match.group(1)), description)
        definitions.setdefault(normalize_business_key(match.group(2)), description)


def extract_cte_comments(sql: str) -> dict[str, str]:
    comments: dict[str, str] = {}
    pending: list[str] = []
    in_block_comment = False
    for raw_line in sql.splitlines():
        stripped = raw_line.strip()
        starts_block = "/*" in stripped
        ends_block = "*/" in stripped
        if stripped.startswith("--") or in_block_comment or starts_block:
            cleaned = strip_comment_numbering(clean_comment_line(stripped))
            if cleaned:
                pending.append(cleaned)
            if starts_block and not ends_block:
                in_block_comment = True
            if ends_block:
                in_block_comment = False
            continue
        match = re.match(r"^\s*([a-zA-Z_][\w]*)\s+as\s*\(", stripped, flags=re.I)
        if match and pending:
            comments[match.group(1).lower()] = clip_text("；".join(pending[-3:]), 500)
            pending = []
            continue
        if stripped and not stripped.startswith(("/*", "*", "*/")):
            pending = []
    return comments


def final_from_segment(sql: str) -> str:
    cleaned = strip_sql_comments(sql)
    depth = 0
    quote: str | None = None
    last_from: int | None = None
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and keyword_at(cleaned, index, "from"):
            last_from = index + len("from")
            index += len("from")
            continue
        index += 1
    if last_from is None:
        return ""
    stop = len(cleaned)
    depth = 0
    quote = None
    index = last_from
    stop_keywords = ["where", "group", "having", "order", "limit", "union", "qualify"]
    while index < len(cleaned):
        char = cleaned[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and any(keyword_at(cleaned, index, keyword) for keyword in stop_keywords):
            stop = index
            break
        index += 1
    return cleaned[last_from:stop].strip()


def source_references_from_segment(segment: str) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    if not segment:
        return references
    pattern = re.compile(
        r"\b(?:from|join|cross\s+join|left\s+join|right\s+join|inner\s+join|full\s+join)\s+"
        r"(`?[a-zA-Z_][\w.]*`?)(?:\s+(?:as\s+)?(`?[a-zA-Z_][\w]*`?))?",
        flags=re.I,
    )
    reserved = {"on", "where", "group", "order", "having", "limit", "join", "left", "right", "inner", "full", "cross"}
    for match in pattern.finditer("from " + segment):
        source = normalize_identifier(match.group(1))
        alias = normalize_identifier(match.group(2) or source)
        if not source or alias in reserved:
            continue
        references.append((alias, source))
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in references:
        if item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


def final_source_references(sql: str) -> list[tuple[str, str]]:
    return source_references_from_segment(final_from_segment(sql))


def final_source_aliases(sql: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for alias, source in final_source_references(sql):
        aliases[alias] = source
        aliases[source] = source
    return aliases


def find_matching_paren(text: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    index = open_index
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def extract_cte_blocks(sql: str) -> dict[str, str]:
    cleaned = strip_sql_comments(sql)
    with_match = re.search(r"\bwith\b", cleaned, flags=re.I)
    if not with_match:
        return {}
    blocks: dict[str, str] = {}
    index = with_match.end()
    cte_pattern = re.compile(
        r"\s*([a-zA-Z_][\w]*)\s*(?:\([^)]*\)\s*)?as\s*\(",
        flags=re.I | re.S,
    )
    while index < len(cleaned):
        match = cte_pattern.match(cleaned, index)
        if not match:
            break
        name = normalize_identifier(match.group(1))
        open_index = match.end() - 1
        close_index = find_matching_paren(cleaned, open_index)
        if close_index < 0:
            break
        blocks[name] = cleaned[open_index + 1 : close_index].strip()
        index = close_index + 1
        while index < len(cleaned) and cleaned[index].isspace():
            index += 1
        if index < len(cleaned) and cleaned[index] == ",":
            index += 1
            continue
        break
    return blocks


def extract_group_by_fields(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    stop = r"(?=\bhaving\b|\border\s+by\b|\blimit\b|\bunion\b|$)"
    matches = list(re.finditer(r"\bgroup\s+by\b(.*?)" + stop, cleaned, flags=re.I | re.S))
    if not matches:
        return []
    fields = [compact(item) for item in split_top_level_csv(matches[-1].group(1)) if compact(item)]
    return fields[:12]


def physical_source_tables(tables: list[str], cte_names: set[str]) -> list[str]:
    result: list[str] = []
    for table in tables:
        last_part = normalize_identifier(table.rsplit(".", 1)[-1])
        if last_part in cte_names:
            continue
        result.append(table)
    return unique_in_order(result)


def log_label_from_table(table: str) -> str:
    name = table.strip("`").split(".")[-1]
    match = re.search(r"_dsl_([a-z0-9_]+?)(?:_fht0)?$", name, flags=re.I)
    if match:
        return match.group(1)
    return name


LOG_DISPLAY_NAMES = {
    "playerlogin": "PlayerLogin",
    "playerlogout": "PlayerLogout",
    "playerregister": "PlayerRegister",
    "openapp": "OpenApp",
    "opencgend": "OpenCgEnd",
    "loginblocked": "LoginBlocked",
    "patchbegin": "PatchBegin",
    "shaderwarmupend": "ShaderWarmupEnd",
    "patchend": "PatchEnd",
    "channellogin": "ChannelLogin",
    "logincgend": "LoginCgEnd",
    "personalprofilecreated": "PersonalProfileCreated",
    "connectbattleserver": "ConnectBattleServer",
    "ranksystem": "RankSystem",
    "battleloginout": "BattleLoginOut",
    "battleitem": "BattleItem",
    "battlemission": "BattleMission",
    "matchend": "MatchEnd",
    "playerquestion": "PlayerQuestion",
    "teambuild": "TeamBuild",
    "teamcreate": "TeamCreate",
}


LOG_CHINESE_FALLBACK = {
    "playerlogin": "玩家登录",
    "playerlogout": "玩家登出",
    "playerregister": "玩家注册",
    "openapp": "打开应用",
    "opencgend": "开场 CG 结束",
    "loginblocked": "登录阻塞",
    "patchbegin": "补丁开始",
    "shaderwarmupend": "Shader 预热结束",
    "patchend": "补丁结束",
    "channellogin": "渠道登录",
    "logincgend": "登录 CG 结束",
    "personalprofilecreated": "个人资料创建",
    "connectbattleserver": "连接战斗服",
    "ranksystem": "段位系统",
    "battleloginout": "局内登录登出",
    "battleitem": "局内道具",
    "battlemission": "局内任务",
    "matchend": "匹配结束",
    "playerquestion": "玩家答题",
    "teambuild": "组队",
    "teamcreate": "创建队伍",
}

XML_LOG_CATALOG_CACHE: dict[str, dict[str, str]] = {}


def clean_log_desc(desc: str) -> str:
    text = compact(desc)
    text = re.sub(r"^[（(]?必填[）)]?", "", text).strip()
    text = text.replace("登陆", "登录")
    text = re.split(r"[。；;]", text, maxsplit=1)[0].strip()
    text = re.sub(r"事件类型[:：].*$", "", text).strip()
    return clip_text(text, 40)


def log_catalog_from_project(project: ProjectContext | None) -> dict[str, str]:
    if not project or not project.root:
        return {}
    path = project.root / "sources" / "xml_catalog.json"
    cache_key = str(path.resolve())
    if cache_key in XML_LOG_CATALOG_CACHE:
        return XML_LOG_CATALOG_CACHE[cache_key]
    catalog: dict[str, str] = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        XML_LOG_CATALOG_CACHE[cache_key] = {}
        return {}
    for item in payload.get("logs", []):
        name = str(item.get("name", "")).strip()
        desc = clean_log_desc(str(item.get("desc", "")))
        if not name or not desc:
            continue
        normalized = normalize_identifier(name).replace("_", "")
        catalog[normalized] = desc
    XML_LOG_CATALOG_CACHE[cache_key] = catalog
    return catalog


def log_catalog_from_roles(roles: ReviewRoleContext | None) -> dict[str, str]:
    if not roles:
        return {}
    catalog: dict[str, str] = {}
    for project in [roles.definition, roles.delivery, *roles.execution_projects]:
        for key, value in log_catalog_from_project(project).items():
            catalog.setdefault(key, value)
    return catalog


def display_log_name(value: str, catalog: dict[str, str] | None = None) -> str:
    label = log_label_from_table(value)
    normalized = normalize_identifier(label).replace("_", "")
    english = LOG_DISPLAY_NAMES.get(normalized) or (label[:1].upper() + label[1:] if label else value)
    chinese = (catalog or {}).get(normalized) or LOG_CHINESE_FALLBACK.get(normalized, "")
    return f"{english}【{chinese}】" if chinese else english


def known_log_display_name(value: str, catalog: dict[str, str] | None = None) -> str:
    label = log_label_from_table(value)
    normalized = normalize_identifier(label).replace("_", "")
    is_physical_tlog = "_dsl_" in value.lower()
    if normalized not in LOG_DISPLAY_NAMES and normalized not in (catalog or {}) and normalized not in LOG_CHINESE_FALLBACK and not is_physical_tlog:
        return ""
    return display_log_name(value, catalog)


def business_source_logs(review: FileReview, roles: ReviewRoleContext | None = None) -> list[str]:
    catalog = log_catalog_from_roles(roles)
    return unique_in_order(
        item
        for item in (known_log_display_name(table, catalog) for table in review.tables if table)
        if item
    )


def table_story(tables: list[str]) -> str:
    if not tables:
        return "未识别来源日志/表"
    parts = []
    for table in tables[:5]:
        log_label = log_label_from_table(table)
        parts.append(f"{table}（日志 {log_label}）" if log_label and log_label != table else table)
    suffix = f"，另 {len(tables) - 5} 张" if len(tables) > 5 else ""
    return "、".join(parts) + suffix


def technical_condition(condition: str) -> bool:
    text = normalize_rule_text(condition)
    return any(
        field in text
        for field in [
            "tdbank_imp_date",
            "tdbankimpdate",
            "dteventtime",
            "stat_date",
            "start_date",
            "end_date",
            "ts_start",
            "ts_end",
            "pt_start",
            "pt_end",
            "start_partition",
            "end_partition",
        ]
    )


def business_condition_story(conditions: list[str], limit: int = 4) -> list[str]:
    stories = []
    for condition in conditions:
        if technical_condition(condition):
            continue
        stories.append(condition_to_business(condition))
    return unique_in_order(stories)[:limit]


def cte_lineage_infos(sql: str, cte_comments: dict[str, str]) -> dict[str, dict]:
    blocks = extract_cte_blocks(sql)
    cte_names = set(blocks)
    infos: dict[str, dict] = {}
    for name, body in blocks.items():
        select_expressions: dict[str, str] = {}
        for alias, _, expression_without_alias in final_select_items(body):
            if alias:
                select_expressions[normalize_identifier(alias)] = expression_without_alias
        infos[name] = {
            "name": name,
            "comment": cte_comments.get(name, ""),
            "source_tables": physical_source_tables(extract_tables(body), cte_names),
            "referenced_ctes": [
                normalize_identifier(table.rsplit(".", 1)[-1])
                for table in extract_tables(body)
                if normalize_identifier(table.rsplit(".", 1)[-1]) in cte_names
            ],
            "where_conditions": extract_where_conditions(body),
            "join_conditions": extract_join_conditions(body),
            "group_by": extract_group_by_fields(body),
            "select_expressions": select_expressions,
        }
    for name in list(infos):
        if infos[name].get("source_tables"):
            continue
        infos[name]["source_tables"] = recursive_cte_source_tables(name, infos)
    return infos


def recursive_cte_source_tables(name: str, infos: dict[str, dict], seen: set[str] | None = None) -> list[str]:
    seen = seen or set()
    if name in seen:
        return []
    seen.add(name)
    info = infos.get(name, {})
    tables = list(info.get("source_tables", []))
    for ref in info.get("referenced_ctes", []):
        tables.extend(recursive_cte_source_tables(ref, infos, seen))
    return unique_in_order(tables)


def qualified_identifier_parts(value: str) -> tuple[str, str]:
    text = strip_wrapping_parens(value.strip()).strip("`")
    match = re.fullmatch(
        r"(?:(`?[a-zA-Z_][\w]*`?)\.)?`?([a-zA-Z_][\w]*)`?",
        text,
    )
    if not match:
        return "", ""
    qualifier = normalize_identifier(match.group(1) or "")
    field = normalize_identifier(match.group(2))
    return qualifier, field


def cte_name_for_qualifier(qualifier: str, context: MetricBusinessContext) -> str:
    if not qualifier:
        return ""
    source = normalize_identifier(context.source_aliases.get(qualifier, qualifier))
    return source if source in context.cte_lineage else ""


def unique_cte_for_field(field: str, context: MetricBusinessContext) -> str:
    matches = [
        name
        for name, info in context.cte_lineage.items()
        if field in info.get("select_expressions", {})
    ]
    return matches[0] if len(matches) == 1 else ""


def cte_operand_card(
    role: str,
    expression: str,
    definitions: dict[str, list[str]],
    context: MetricBusinessContext,
    metric_alias: str = "",
) -> dict:
    lookup_expression = unwrap_formula_expression(strip_wrapping_parens(expression))
    lookup_expression = re.sub(r"^(?:1(?:\.0+)?|100(?:\.0+)?)\s*\*\s*", "", lookup_expression, flags=re.I).strip()
    qualifier, field = qualified_identifier_parts(lookup_expression)
    cte_name = cte_name_for_qualifier(qualifier, context)
    if not cte_name and field:
        cte_name = unique_cte_for_field(field, context)
    info = context.cte_lineage.get(cte_name, {})
    cte_expression = info.get("select_expressions", {}).get(field, "") if info else ""
    if not info:
        resolved, lineage = resolve_metric_expression(metric_alias, expression, definitions)
        description, source = describe_operand_business(resolved, definitions, context, metric_alias, seen={field} if field else set())
        return {
            "role": role,
            "operand": clip_text(expression, 240),
            "source_step": "",
            "source_tables": [],
            "group_by": [],
            "business_filters": [],
            "field_expression": clip_text(resolved, 300),
            "story": description,
            "lineage": [clip_text(item, 240) for item in lineage],
            "source": source,
        }
    expression_for_description = cte_expression or expression
    description, source = describe_operand_business(
        expression_for_description,
        definitions,
        context,
        metric_alias,
        seen={field} if field else set(),
    )
    count_star_description = cte_count_star_business_description(info, context)
    if count_star_description and re.search(r"\bcount\s*\(\s*(?:\*|1)\s*\)", expression_for_description, flags=re.I):
        description = count_star_description
        source = "static_inference"
    group_by = info.get("group_by", [])
    group_story = f"按 {', '.join(group_by[:6])} 聚合" if group_by else "未识别显式 GROUP BY"
    filter_stories = business_condition_story(info.get("where_conditions", []) + info.get("join_conditions", []))
    comment = info.get("comment", "")
    prefix = f"步骤 `{cte_name}`"
    if qualifier and qualifier != cte_name:
        prefix += f"（最终 SELECT 中别名 `{qualifier}`）"
    story_parts = [
        f"{prefix}读取 {table_story(info.get('source_tables', []))}",
        group_story,
        f"字段 `{field or expression}` 表示{description}",
    ]
    if comment:
        story_parts.insert(1, f"步骤说明：{comment}")
    if filter_stories:
        story_parts.append("业务筛选：" + "；".join(filter_stories))
    return {
        "role": role,
        "operand": clip_text(expression, 240),
        "source_step": cte_name,
        "source_tables": info.get("source_tables", [])[:8],
        "group_by": group_by[:8],
        "business_filters": filter_stories,
        "field_expression": clip_text(expression_for_description, 300),
        "story": "；".join(story_parts),
        "lineage": [f"{field} := {clip_text(expression_for_description, 240)}"] if expression_for_description else [],
        "source": "cte_comment" if comment else source,
    }


def cte_count_star_business_description(info: dict, context: MetricBusinessContext) -> str:
    refs = [ref for ref in info.get("referenced_ctes", []) if ref in context.cte_lineage]
    if len(refs) != 1:
        return ""
    upstream = context.cte_lineage.get(refs[0], {})
    upstream_group = [normalize_identifier(item) for item in upstream.get("group_by", [])]
    current_group = {normalize_identifier(item) for item in info.get("group_by", [])}
    counted_fields = [field for field in upstream_group if field not in current_group]
    if not counted_fields:
        return ""
    if any(field in {"vopenid", "openid"} for field in counted_fields):
        counted = "按 vOpenID 去重的玩家数"
    elif any(field == "deviceid" for field in counted_fields):
        counted = "按 DeviceId 去重的设备数"
    else:
        counted = "按 " + "、".join(friendly_identifier(field) for field in counted_fields[:4]) + " 去重后的记录数"
    if info.get("group_by"):
        return f"在每个 {'、'.join(info.get('group_by', [])[:4])} 内统计上游 `{refs[0]}` 中{counted}"
    return f"统计整个窗口内上游 `{refs[0]}` 中{counted}"


def describe_cte_operand_business(
    expression: str,
    definitions: dict[str, list[str]],
    context: MetricBusinessContext,
    metric_alias: str = "",
) -> tuple[str, str]:
    qualifier, field = qualified_identifier_parts(expression)
    cte_name = cte_name_for_qualifier(qualifier, context) if qualifier else unique_cte_for_field(field, context)
    if not cte_name:
        return "", ""
    card = cte_operand_card("operand", expression, definitions, context, metric_alias)
    return card.get("story", ""), card.get("source", "static_inference")


def metric_business_context(sql: str) -> MetricBusinessContext:
    comment_lines = extract_comment_lines(sql)
    cte_comments = extract_cte_comments(sql)
    cte_lineage = cte_lineage_infos(sql, cte_comments)
    definitions: dict[str, str] = {}
    bucket_definitions = extract_bucket_definitions(sql)
    base_description = ""
    duration_logic = extract_duration_logic(comment_lines, cte_comments) or infer_duration_algorithm(sql)
    output_description = ""
    title = ""
    base_definition_keys = {
        normalize_business_key("Base"),
        normalize_business_key("Base 用户"),
        normalize_business_key("Base 用户定义"),
        normalize_business_key("Base 人群"),
        normalize_business_key("全集"),
        normalize_business_key("全量"),
        normalize_business_key("统计全集"),
        normalize_business_key("统计对象"),
        normalize_business_key("基础人群"),
        normalize_business_key("基础记录"),
    }
    for line in comment_lines + list(cte_comments.values()):
        for inline_label, inline_description in inline_business_definition_parts(line):
            add_business_definition(definitions, inline_label, inline_description)
        label, description = split_comment_definition(line)
        if not label:
            continue
        normalized_label = normalize_business_key(label)
        if normalized_label in base_definition_keys:
            base_description = base_description or description
            add_business_definition(definitions, label, description)
            continue
        if normalized_label == normalize_business_key("输出"):
            output_description = output_description or description
            continue
        if normalized_label == normalize_business_key("指标"):
            title = title or description
            continue
        if normalized_label in {
            normalize_business_key("指标口径"),
            normalize_business_key("指标定义"),
            normalize_business_key("指标说明"),
            normalize_business_key("输出指标"),
        }:
            for inline_label, inline_description in inline_business_definition_parts(description):
                add_business_definition(definitions, inline_label, inline_description)
            continue
        add_business_definition(definitions, label, description)
    for label, description in comment_section_definitions(comment_lines):
        normalized_label = normalize_business_key(label)
        if normalized_label in base_definition_keys:
            base_description = base_description or description
        elif normalized_label == normalize_business_key("输出"):
            output_description = output_description or description
        elif normalized_label == normalize_business_key("指标"):
            title = title or description
        add_business_definition(definitions, label, description)
    return MetricBusinessContext(
        definitions=definitions,
        cte_comments=cte_comments,
        cte_lineage=cte_lineage,
        source_aliases=final_source_aliases(sql),
        bucket_definitions=bucket_definitions,
        base_description=base_description,
        duration_logic=duration_logic,
        output_description=output_description,
        title=title,
        comment_lines=comment_lines,
    )


def extract_duration_logic(comment_lines: list[str], cte_comments: dict[str, str]) -> str:
    candidates = comment_lines + list(cte_comments.values())
    for line in candidates:
        text = compact(line)
        if "战斗时长" in text and ("MAX" in text.upper() or "max" in text or "BattleSrvId" in text):
            return clip_text(text, 260)
    for line in candidates:
        text = compact(line)
        if "累计" in text and "时长" in text:
            return clip_text(text, 260)
    return ""


def infer_duration_algorithm(sql: str) -> str:
    text = strip_sql_comments(sql)
    lowered = text.lower()
    if "onlinetime" in lowered and "seconds_sub" in lowered:
        return (
            "在线时长：使用 PlayerLogout.OnlineTime；由登出时间减 OnlineTime 反推出登入日，"
            "先按玩家日汇总整段在线秒数，再进入均值/分位统计。"
        )
    if "totalactiveduration" in lowered and "lag(" in lowered and re.search(r"greatest\s*\(\s*cum_sec\s*-", lowered):
        return (
            "战斗时长：先按玩家和日期取 MAX(TotalActiveDuration) 作为累计非挂机时长，"
            "再用当天累计值减前一天累计值，负数按 0 处理；首日需多读前一天作为差分基准。"
        )
    if "totalactiveduration" in lowered and "battlesrvid" in lowered and re.search(r"sum\s*\(\s*[a-z_][\w.]*max", lowered):
        return (
            "累计战斗时长：先按玩家 + BattleSrvId 取 MAX(TotalActiveDuration)，"
            "再跨 BattleSrvId 求和，避免同一战斗服内累计值重复相加。"
        )
    if "totalactiveduration" in lowered and "max(" in lowered:
        return (
            "战斗时长：使用 TotalActiveDuration 累计字段并取 MAX；需要确认是否还按 BattleSrvId 拆开后再汇总，"
            "以及单位换算是否符合业务口径。"
        )
    if "matchduration" in lowered:
        return (
            "匹配耗时：使用 MatchDuration 单条匹配上报耗时；通常按记录计算平均值、分位点或耗时分桶，"
            "需要确认负值/空值处理。"
        )
    return ""


def extract_bucket_definitions(sql: str) -> dict[str, str]:
    definitions: dict[str, str] = {}
    text = strip_sql_comments(sql)
    pattern = re.compile(
        r"when\s+(?P<condition>.*?)\s+then\s+'(?P<bucket>H\d+P?|[^']+)'",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(text):
        bucket = match.group("bucket")
        condition = compact(match.group("condition"))
        if "total_active_dur" in condition.lower() or "duration" in condition.lower() or "时长" in condition:
            definitions[bucket] = duration_bucket_condition_to_business(condition)
    return definitions


def duration_bucket_condition_to_business(condition: str) -> str:
    text = compact(condition)
    match = re.search(r"<=\s*(\d+(?:\.\d+)?)", text)
    if match:
        return f"累计常规服战斗时长 <= {match.group(1)} 小时"
    match = re.search(r">\s*(\d+(?:\.\d+)?)", text)
    if match:
        return f"累计常规服战斗时长 > {match.group(1)} 小时"
    return condition_to_business(text)


def strip_wrapping_parens(value: str) -> str:
    text = value.strip()
    changed = True
    while changed and text.startswith("(") and text.endswith(")"):
        changed = False
        depth = 0
        quote: str | None = None
        balanced_outer = True
        for index, char in enumerate(text):
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in {"'", '"', "`"}:
                quote = char
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    balanced_outer = False
                    break
        if balanced_outer and depth == 0:
            text = text[1:-1].strip()
            changed = True
    return text


def normalize_rule_text(value: str) -> str:
    text = strip_wrapping_parens(compact(value)).lower()
    text = text.replace("`", "")
    text = re.sub(r"\b[a-z_][\w]*\.", "", text)
    text = re.sub(r"\s*(>=|<=|<>|!=|=|>|<|\+|-|\*|/)\s*", r" \1 ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_value(value: str) -> str:
    return value.strip().strip("'\"`").lower()


def extract_constraints(text: str) -> list[Constraint]:
    constraints: list[Constraint] = []
    cleaned = text.replace("`", "")
    for match in re.finditer(
        r"\b([a-zA-Z_][\w]*)\b\s*(=|!=|<>|>=|<=|>|<)\s*('?[\w:.-]+'?)",
        cleaned,
        flags=re.I,
    ):
        constraints.append(
            Constraint(
                field=match.group(1).lower(),
                operator=match.group(2).upper(),
                values=(normalize_value(match.group(3)),),
            )
        )
    for match in re.finditer(
        r"\b([a-zA-Z_][\w]*)\b\s+in\s*\((.*?)\)",
        cleaned,
        flags=re.I | re.S,
    ):
        values = tuple(
            normalize_value(item)
            for item in split_top_level_csv(match.group(2))
            if normalize_value(item)
        )
        if values:
            constraints.append(
                Constraint(field=match.group(1).lower(), operator="IN", values=values)
            )
    return constraints


def extract_rule_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"\b[a-zA-Z_][\w]{2,}\b", text)
        if token.lower() not in {"and", "or", "the", "with", "null", "true", "false"}
    }


def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def project_label(project: ProjectContext | None) -> str:
    if not project:
        return "unknown"
    return project.display_name or project.project_id or (project.root.name if project.root else "unknown")


def project_ref_keys(project: ProjectContext) -> set[str]:
    keys = {
        project.project_id,
        project.display_name,
        project.root.name if project.root else "",
        str(project.root) if project.root else "",
    }
    return {key.lower().replace("-", "_") for key in keys if key}


def project_config_value(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("status") or "")
    return str(value or "")


def canonical_rules_from_records(records: list[dict]) -> list[CanonicalRule]:
    result: list[CanonicalRule] = []
    for item in records:
        text = " ".join(
            [
                item.get("title", ""),
                item.get("content", ""),
                item.get("applies_to", ""),
                item.get("notes", ""),
            ]
        )
        result.append(
            CanonicalRule(
                rule_id=item.get("rule_id", ""),
                concept_key=item.get("concept_key", ""),
                version=int(item.get("version", 0) or 0),
                status=item.get("status", ""),
                title=item.get("title", ""),
                content=item.get("content", ""),
                applies_to=item.get("applies_to", ""),
                normalized=normalize_rule_text(text),
                constraints=extract_constraints(text),
                tokens=extract_rule_tokens(text),
                raw=item if isinstance(item, dict) else {},
            )
        )
    return result


def load_project_rules(project_root: Path | None) -> tuple[str, list[CanonicalRule]]:
    if not project_root:
        return "", []
    root = project_root.resolve()
    if not root.exists():
        raise SystemExit(f"Project root not found: {root}")
    project_name = root.name
    manifest_file = root / "manifest.json"
    if manifest_file.exists():
        try:
            project_name = json.loads(manifest_file.read_text(encoding="utf-8")).get("project_name") or project_name
        except json.JSONDecodeError:
            pass
    if not has_v2_store(root):
        raise SystemExit(f"Canonical Rule Store v2 is required: {root / 'rules' / 'store.json'}")
    return project_name, []


def select_project_rules_for_sql(project: ProjectContext | None, sql: str) -> list[CanonicalRule]:
    if not project or not project.root:
        return []
    frame = build_intent_frame(concept_registry={"by_key": {}})
    sql_frame = scan_intent_source(sql)
    frame["candidate_sql_observed"] = {
        "source_logs": sql_frame["source_logs"],
        "source_fields": sql_frame["source_fields"],
        "domains": sql_frame["domains"],
        "metric_families": sql_frame["metric_families"],
        "grain": sql_frame["grain"],
    }
    _, records = select_rule_records(
        project.root,
        frame,
        query_text="",
        statuses=("confirmed", "proposed"),
    )
    return canonical_rules_from_records(records)


def load_project_context(project_root: Path | None) -> ProjectContext | None:
    if not project_root:
        return None
    root = project_root.resolve()
    if not root.exists():
        raise SystemExit(f"Project root not found: {root}")
    project_name, canonical_rules = load_project_rules(root)
    manifest = read_json_file(root / "manifest.json")
    config = read_json_file(root / "project_config.json")
    profile = config.get("table_naming_profile") if isinstance(config.get("table_naming_profile"), dict) else {}
    table_overrides = config.get("table_overrides") if isinstance(config.get("table_overrides"), dict) else {}
    return ProjectContext(
        root=root,
        project_id=str(config.get("project_id") or manifest.get("project_id") or root.name),
        display_name=str(config.get("display_name") or manifest.get("project_name") or project_name or root.name),
        sql_dialect=str(config.get("sql_dialect") or ""),
        query_engine=str(config.get("query_engine") or ""),
        query_environment=project_config_value(config.get("query_environment")),
        dashboard_application=project_config_value(config.get("dashboard_application")),
        table_profile_name=str(profile.get("name") or ""),
        table_pattern=str(profile.get("pattern") or ""),
        table_database=str(profile.get("database") or ""),
        table_overrides={str(key): str(value) for key, value in table_overrides.items()},
        canonical_rules=canonical_rules,
        config=config,
    )


def dedupe_projects(projects: list[ProjectContext]) -> list[ProjectContext]:
    seen: set[str] = set()
    result: list[ProjectContext] = []
    for project in projects:
        key = str(project.root or project.project_id).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(project)
    return result


def discover_project_contexts(projects_root: Path) -> list[ProjectContext]:
    if not projects_root.exists():
        return []
    projects: list[ProjectContext] = []
    for child in sorted(projects_root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if not (child / "project_config.json").exists():
            continue
        context = load_project_context(child)
        if context:
            projects.append(context)
    return projects


def project_for_ref(ref: str, projects: list[ProjectContext]) -> ProjectContext | None:
    normalized = ref.strip().lower().replace("-", "_")
    if not normalized:
        return None
    for project in projects:
        if normalized in project_ref_keys(project):
            return project
    path = Path(ref)
    if path.exists():
        return load_project_context(path)
    return None


def infer_inbox_project_root(input_paths: list[Path], inbox_root: Path) -> Path | None:
    inbox = inbox_root.resolve()
    projects_root = inbox.parent
    for path in input_paths:
        try:
            relative = path.resolve().relative_to(inbox)
        except ValueError:
            continue
        if not relative.parts:
            continue
        candidate = projects_root / relative.parts[0]
        if (candidate / "project_config.json").exists():
            return candidate
    return None


def load_file_role_map(value: str | None) -> dict:
    if not value:
        return {}
    candidate = Path(value)
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return json.loads(value)


def file_role_override(path: Path, role_map: dict) -> str:
    if not role_map:
        return ""
    files = role_map.get("files") if isinstance(role_map.get("files"), dict) else role_map
    default = role_map.get("default") if isinstance(role_map.get("default"), (str, dict)) else ""
    path_text = path.as_posix()
    name = path.name
    for pattern, value in files.items():
        if pattern in {"files", "default"}:
            continue
        if fnmatch.fnmatch(path_text, pattern) or fnmatch.fnmatch(name, pattern):
            if isinstance(value, dict):
                return str(value.get("execution_project") or value.get("execution_project_root") or "")
            return str(value)
    if isinstance(default, dict):
        return str(default.get("execution_project") or default.get("execution_project_root") or "")
    return str(default or "")


def table_pattern_regex(pattern: str) -> re.Pattern[str] | None:
    if not pattern:
        return None
    escaped = re.escape(pattern.lower())
    escaped = escaped.replace(re.escape("{log_lower}"), r"[a-z0-9_]+")
    return re.compile(rf"^{escaped}$")


def table_matches_project(table: str, project: ProjectContext) -> bool:
    normalized = table.strip("`").lower()
    overrides = {value.lower() for value in project.table_overrides.values()}
    if normalized in overrides:
        return True
    pattern = table_pattern_regex(project.table_pattern)
    if pattern:
        return bool(pattern.match(normalized))
    return bool(project.table_database and normalized.startswith(project.table_database.lower() + "."))


def matched_projects_for_table(table: str, projects: list[ProjectContext]) -> list[ProjectContext]:
    return [project for project in projects if table_matches_project(table, project)]


def table_profile_mismatches(review: FileReview, delivery: ProjectContext | None, projects: list[ProjectContext]) -> list[str]:
    if not delivery:
        return []
    mismatches: list[str] = []
    for table in review.tables:
        if not has_tlog_table([table]):
            continue
        if table_matches_project(table, delivery):
            continue
        matched = [project_label(project) for project in matched_projects_for_table(table, projects)]
        suffix = f"matched_profile={', '.join(matched)}" if matched else "matched_profile=unknown"
        mismatches.append(f"{table} -> not delivery profile `{project_label(delivery)}` ({suffix})")
    return unique_in_order(mismatches)


def score_execution_project(review: FileReview, project: ProjectContext, delivery: ProjectContext | None) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    table_matches = [table for table in review.tables if table_matches_project(table, project)]
    if table_matches:
        score += 5
        reasons.append("physical table profile")
    return score, unique_in_order(reasons)


def infer_execution_project(review: FileReview, roles: ReviewRoleContext) -> ExecutionInference:
    override = file_role_override(review.path, roles.file_role_map)
    if override:
        project = project_for_ref(override, roles.known_projects)
        if project:
            return ExecutionInference(project, "high", f"file_role_map override: {override}", "file_role_map")
        return ExecutionInference(None, "low", f"file_role_map override did not match a loaded project: {override}", "file_role_map")
    candidates = roles.execution_projects or roles.known_projects
    if not candidates:
        return ExecutionInference(None, "low", "no explicit or table-backed execution project evidence")
    if roles.execution_selection_explicit and len(candidates) == 1:
        return ExecutionInference(candidates[0], "high", "explicit execution project selection", "explicit")
    scored = []
    for project in candidates:
        score, reasons = score_execution_project(review, project, roles.delivery)
        scored.append((score, project, reasons))
    scored.sort(key=lambda item: (item[0], 1 if roles.delivery and item[1].project_id == roles.delivery.project_id else 0), reverse=True)
    best_score, best_project, best_reasons = scored[0]
    if best_score <= 0:
        return ExecutionInference(None, "low", "no physical table profile match; execution project remains unresolved")
    same_score = [item for item in scored if item[0] == best_score]
    if len(same_score) > 1:
        tied_projects = ", ".join(project_label(item[1]) for item in same_score)
        return ExecutionInference(
            None,
            "low",
            "ambiguous execution project tie: "
            + tied_projects
            + "; shared table profiles require --file-role-map or explicit execution evidence",
        )
    confidence = "high" if best_score >= 5 else "medium"
    return ExecutionInference(best_project, confidence, ", ".join(best_reasons) or "scored project evidence")


def summarize_canonical_rule(rule: CanonicalRule) -> str:
    return clip_text(compact(rule.content), 260)


def game_mode_mapping_from_project(project_root: Path | None) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    if not project_root:
        return mapping
    dependency = None
    for rule in load_rules(project_root, status="confirmed"):
        if str(rule.get("concept_key") or "") != "game-mode-map":
            continue
        structured = rule.get("structured_definition") if isinstance(rule.get("structured_definition"), dict) else {}
        dependency = next(
            (
                item
                for item in structured.get("knowledge_dependencies", []) or []
                if isinstance(item, dict) and "mode_id" in (item.get("fields") or [])
            ),
            None,
        )
        break
    if not dependency:
        return mapping
    fields = [str(item) for item in dependency.get("fields", []) or [] if str(item)]
    try:
        resolved = resolve_knowledge(
            project_root=project_root,
            dataset_id=str(dependency.get("dataset_id") or ""),
            projection_id=str(dependency.get("projection_id") or ""),
            usage_mode="authoring_reference",
            fields=fields,
            limit=200,
        )
    except (ValueError, OSError, json.JSONDecodeError):
        return mapping
    for row in resolved.get("rows", []) or []:
        mode_id = str(row.get("mode_id") or "").strip()
        if not mode_id:
            continue
        mapping[mode_id] = {
            "name": compact(row.get("mode_name", "")),
            "category": compact(row.get("mode_category", "")),
            "dataset_id": str(dependency.get("dataset_id") or ""),
            "dataset_version": str((resolved.get("reference") or {}).get("dataset_version") or ""),
            "rule_status": "knowledge_bound",
        }
    return mapping


BUSINESS_FILTER_FIELD_LABELS = {
    "gamemode": "模式 ID",
    "game_mode": "模式 ID",
    "gamemodeid": "模式 ID",
    "game_mode_id": "模式 ID",
    "mode_id": "模式 ID",
    "izoneareaid": "区服 ID",
    "zone_id": "区服 ID",
    "gamesvrid": "游戏服 ID",
    "battlesrvid": "战斗服 ID",
    "max_team_number": "最大队伍人数",
    "team_number": "队伍人数",
    "team_num": "队伍人数",
    "itemid": "道具 ID",
    "item_id": "道具 ID",
    "propid": "道具 ID",
    "prop_id": "道具 ID",
    "goodsid": "道具 ID",
    "goods_id": "道具 ID",
    "match_duration_sec": "匹配耗时",
    "total_active_dur": "累计活跃时长",
    "reason": "原因/事件类型",
    "eventid": "事件 ID",
}


def canonical_filter_field(field: str) -> str:
    return normalize_identifier(field).replace("_", "")


def business_filter_field_label(field: str) -> str:
    canonical = canonical_filter_field(field)
    if canonical in {"gamemode", "gamemodeid", "modeid"}:
        return "模式 ID"
    if canonical in {"izoneareaid", "zoneid"}:
        return "区服 ID"
    if canonical in {"gamesvrid"}:
        return "游戏服 ID"
    if canonical in {"battlesrvid"}:
        return "战斗服 ID"
    if "team" in canonical and ("num" in canonical or "number" in canonical):
        return "队伍人数"
    if "item" in canonical or "prop" in canonical or "goods" in canonical:
        return "道具 ID"
    if "duration" in canonical or "dur" in canonical:
        return "时长条件"
    return BUSINESS_FILTER_FIELD_LABELS.get(normalize_identifier(field), field)


def business_filter_kind(field: str) -> str:
    canonical = canonical_filter_field(field)
    if canonical in {"tdbankimpdate", "dteventtime", "statdate", "dtstatdate"}:
        return "technical_time"
    if canonical in {"gamemode", "gamemodeid", "modeid"}:
        return "game_mode"
    if canonical in {"izoneareaid", "zoneid"}:
        return "zone"
    if canonical in {"gamesvrid"}:
        return "game_server"
    if canonical in {"battlesrvid"}:
        return "battle_server"
    if "team" in canonical and ("num" in canonical or "number" in canonical):
        return "team_size"
    if "item" in canonical or "prop" in canonical or "goods" in canonical:
        return "item"
    if "duration" in canonical or "dur" in canonical:
        return "duration"
    return "business_filter"


def value_is_fixed_literal(value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    if text.startswith("${") and text.endswith("}"):
        return False
    if "." in text:
        return False
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", text) or re.fullmatch(r"[A-Z0-9_-]+", text, flags=re.I))


def business_filter_scope_label(scope: str) -> str:
    labels = {
        "base_filter": "Base 级筛选",
        "metric_filter": "指标内条件",
        "join_mapping": "关联/归因条件",
    }
    return labels.get(scope, scope or "业务条件")


def business_filter_value_mode(fixed_values: list[str], dynamic_values: list[str]) -> str:
    if fixed_values and dynamic_values:
        return "mixed"
    if fixed_values:
        return "fixed_id_range"
    if dynamic_values:
        return "dynamic_or_parameterized"
    return "unknown"


def business_filter_prefix(scope: str, label: str) -> str:
    if scope == "metric_filter":
        return f"该指标分子/条件只统计{label}"
    if scope == "join_mapping":
        return f"关联/归因条件限定{label}"
    return f"Base 只包含{label}"


def business_filter_pass_criteria(kind: str, scope: str, label: str, unknown_values: list[str]) -> str:
    prefix = "该指标" if scope == "metric_filter" else "Base"
    if kind == "game_mode":
        if unknown_values:
            return "不能直接通过：存在未登记 GameMode ID，需要补模式名称/大类映射或让需求方确认。"
        return f"通过标准：{prefix} 的 GameMode ID、中文模式名和模式大类都符合业务目标。"
    if kind == "item":
        return f"通过标准：{prefix} 的道具 ID 范围完整，必要时有道具名称/类型映射。"
    if kind == "team_size":
        return f"通过标准：{prefix} 的队伍人数范围符合指标定义，比如单人/双人/多人是否拆分正确。"
    if kind == "duration":
        return f"通过标准：{prefix} 的时长字段、单位、边界和分桶含义都明确。"
    if scope == "join_mapping":
        return f"通过标准：关联右侧字段来源可信，且不会意外过滤或重复放大 {label}。"
    return f"通过标准：{prefix} 的 {label} 取值就是业务要看的范围，临时筛选已明确标注。"


def constraint_business_description(
    constraint: Constraint,
    mode_mapping: dict[str, dict[str, str]],
    scope: str = "base_filter",
    source: str = "WHERE/JOIN/CASE condition",
) -> dict:
    field = constraint.field
    values = [str(value) for value in constraint.values]
    kind = business_filter_kind(field)
    label = business_filter_field_label(field)
    value_text = ", ".join(values)
    fixed_values = [value for value in values if value_is_fixed_literal(value)]
    dynamic_values = [value for value in values if value not in fixed_values]
    mapping_rows: list[dict[str, str]] = []
    unknown_values: list[str] = []
    if dynamic_values and not fixed_values:
        if scope == "join_mapping":
            business_effect = f"{label} 通过 {value_text} 做动态关联/归因，不是固定 ID 范围"
        elif scope == "metric_filter":
            business_effect = f"该指标通过 {label} = {value_text} 做动态或参数化条件，不是固定 ID 范围"
        else:
            business_effect = f"{label} 通过 {value_text} 做动态或参数化约束，不是固定 ID 范围"
        if kind == "game_mode":
            how_to_judge = (
                "确认右侧字段/参数的来源是否是可信模式配置或用户输入；如果要审固定模式范围，必须展开出实际 GameMode ID 和名称/大类。"
            )
        else:
            how_to_judge = f"确认右侧字段/参数的来源是否定义了本指标的 {label} 范围；需要固定范围时应补出实际取值。"
        return {
            "field": field,
            "label": label,
            "kind": kind,
            "scope": scope,
            "scope_label": business_filter_scope_label(scope),
            "operator": constraint.operator,
            "values": values,
            "value_mode": business_filter_value_mode(fixed_values, dynamic_values),
            "fixed_id_range": False,
            "business_effect": business_effect,
            "mapping": [],
            "unknown_values": [],
            "dynamic_values": dynamic_values,
            "how_to_judge": how_to_judge,
            "pass_criteria": business_filter_pass_criteria(kind, scope, label, []),
            "source": source,
        }
    if kind == "game_mode":
        for value in fixed_values:
            mapped = mode_mapping.get(value)
            if mapped:
                mapping_rows.append(
                    {
                        "value": value,
                        "name": mapped["name"],
                        "category": mapped["category"],
                        "rule_status": mapped["rule_status"],
                    }
                )
            else:
                unknown_values.append(value)
    if kind == "game_mode":
        mapped_text = "，".join(f"{row['value']}={row['name']}/{row['category']}" for row in mapping_rows)
        if unknown_values:
            mapped_text = (mapped_text + "；" if mapped_text else "") + "未配置：" + "，".join(unknown_values)
        if dynamic_values:
            mapped_text = (mapped_text + "；" if mapped_text else "") + "动态值：" + "，".join(dynamic_values)
        business_effect = f"{business_filter_prefix(scope, label)} {value_text}" + (f"（{mapped_text}）" if mapped_text else "")
        how_to_judge = "确认这些 GameMode ID 是否就是本指标要看的模式范围；若有未配置 ID，不能猜名称/大类，必须补映射或向用户确认。"
    elif kind == "item":
        business_effect = f"{business_filter_prefix(scope, label)} {value_text}"
        how_to_judge = "确认这些道具 ID 是否完整覆盖业务要看的道具范围；缺少道具名称/类型映射时，需要 SQL 作者或需求方补充。"
    elif kind in {"team_size", "duration"}:
        business_effect = f"{business_filter_prefix(scope, label)} {constraint.operator} {value_text}"
        how_to_judge = f"确认 {label} 的边界是否就是这个指标的业务定义；注意上下界、单位和是否包含边界。"
    else:
        business_effect = f"{business_filter_prefix(scope, label)} {constraint.operator} {value_text}"
        how_to_judge = f"确认 {label} 的取值是否就是本指标的业务范围；若是临时筛选，应在结论中标注。"
    return {
        "field": field,
        "label": label,
        "kind": kind,
        "scope": scope,
        "scope_label": business_filter_scope_label(scope),
        "operator": constraint.operator,
        "values": values,
        "value_mode": business_filter_value_mode(fixed_values, dynamic_values),
        "fixed_id_range": bool(fixed_values),
        "business_effect": business_effect,
        "mapping": mapping_rows,
        "unknown_values": unknown_values,
        "dynamic_values": dynamic_values,
        "how_to_judge": how_to_judge,
        "pass_criteria": business_filter_pass_criteria(kind, scope, label, unknown_values),
        "source": source,
    }


def extract_business_filters_from_conditions(
    conditions: list[str],
    mode_mapping: dict[str, dict[str, str]],
    scope: str,
    source: str,
) -> list[dict]:
    filters: list[dict] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    important_kinds = {"game_mode", "zone", "game_server", "battle_server", "item", "team_size", "duration"}
    for condition in conditions:
        for constraint in extract_constraints(condition):
            kind = business_filter_kind(constraint.field)
            if kind not in important_kinds:
                continue
            key = (scope, constraint.field, constraint.operator, constraint.values)
            if key in seen:
                continue
            seen.add(key)
            item = constraint_business_description(constraint, mode_mapping, scope=scope, source=source)
            item["condition"] = condition
            filters.append(item)
    return filters


def extract_business_filters_for_review(review: FileReview, project_root: Path | None = None) -> list[dict]:
    mode_mapping = game_mode_mapping_from_project(project_root)
    review.business_filter_mappings = mode_mapping
    constants = extract_constant_aliases(review.sql)
    base_filters = extract_business_filters_from_conditions(
        expand_business_conditions(extract_where_conditions(review.sql), constants),
        mode_mapping,
        scope="base_filter",
        source="WHERE base condition",
    )
    join_filters = extract_business_filters_from_conditions(
        expand_business_conditions(extract_join_conditions(review.sql), constants),
        mode_mapping,
        scope="join_mapping",
        source="JOIN attribution condition",
    )
    return unique_business_filters(base_filters + join_filters)


def unique_business_filters(filters: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for item in filters:
        key = (
            str(item.get("scope", "")),
            str(item.get("field", "")),
            str(item.get("operator", "")),
            tuple(str(value) for value in item.get("values", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def metric_business_filters_from_conditions(
    conditions: list[str],
    mode_mapping: dict[str, dict[str, str]],
    constants: dict[str, str] | None = None,
) -> list[dict]:
    return extract_business_filters_from_conditions(
        expand_business_conditions(conditions, constants or {}),
        mode_mapping,
        scope="metric_filter",
        source="CASE/IF metric condition",
    )


def make_rule_check(rule: CanonicalRule, result: str, message: str, evidence: str) -> RuleCheck:
    return RuleCheck(
        rule_id=rule.rule_id,
        status=rule.status,
        result=result,
        message=message,
        evidence=evidence,
        concept_key=rule.concept_key,
        title=rule.title,
        rule_summary=summarize_canonical_rule(rule),
    )


def rule_activation_contract(rule: CanonicalRule) -> dict:
    contract = rule.raw.get("activation_contract") if isinstance(rule.raw, dict) else {}
    return contract if isinstance(contract, dict) else {}


def rule_event_signature(rule: CanonicalRule) -> dict:
    contract = rule_activation_contract(rule)
    if not contract:
        return {}
    signature = contract_event_signature(contract)
    return signature if isinstance(signature, dict) else {}


def rule_has_event_signature(rule: CanonicalRule) -> bool:
    contract = rule_activation_contract(rule)
    return isinstance(contract.get("event_signature"), dict)


def review_sql_evidence(review: FileReview) -> dict:
    cached = getattr(review, "_normalized_sql_evidence", None)
    if isinstance(cached, dict):
        return cached
    evidence = extract_sql_evidence(review.sql)
    setattr(review, "_normalized_sql_evidence", evidence)
    return evidence


def event_signature_shared_log_match(signature: dict, evidence: dict) -> bool:
    expected_logs = {normalize_business_key(str(item)) for item in signature.get("required_logs", []) or []}
    observed_logs = {normalize_business_key(str(item)) for item in evidence.get("source_logs", []) or []}
    return bool((expected_logs & observed_logs) & REVERSE_AUDIT_SHARED_LOGS)


def event_signature_values(items: list[dict], evidence_type: str) -> list[str]:
    return [
        str(item.get("value") or "")
        for item in items
        if isinstance(item, dict) and item.get("type") == evidence_type and item.get("value")
    ]


def event_signature_review_check(rule: CanonicalRule, review: FileReview) -> RuleCheck | None:
    if not rule_has_event_signature(rule):
        return None
    signature = rule_event_signature(rule)
    if not signature:
        return make_rule_check(rule, "not_relevant", "规则未声明可用 event_signature。", "missing event_signature")
    evidence = review_sql_evidence(review)
    match = event_signature_match(
        signature,
        evidence,
        shared_log_match=event_signature_shared_log_match(signature, evidence),
    )
    reason = str(match.get("reason") or "")
    matched = match.get("matched_evidence", []) or []
    missing = match.get("missing_evidence", []) or []
    if reason == "required_log_not_observed":
        return make_rule_check(rule, "not_relevant", "SQL 未读取该口径要求的来源日志。", "event_signature required_log")

    matched_types = {str(item.get("type") or "") for item in matched if isinstance(item, dict)}
    signature_has_predicate_core = bool(signature.get("required_predicate_signatures"))
    signature_has_role_core = bool(signature.get("required_metric_roles") or signature.get("required_any_metric_roles"))
    signature_has_aggregation_core = bool(signature.get("required_aggregations") or signature.get("required_any_aggregations"))
    signature_has_field_role_core = bool(signature.get("required_field_roles"))
    predicate_core_ok = not signature_has_predicate_core or "predicate" in matched_types
    role_core_ok = not signature_has_role_core or "metric_role" in matched_types
    aggregation_core_ok = not signature_has_aggregation_core or "aggregation" in matched_types
    field_role_core_ok = not signature_has_field_role_core or "field_role" in matched_types

    incompatible_roles = event_signature_values(missing, "incompatible_metric_role_present")
    if incompatible_roles:
        if not event_signature_values(matched, "metric_role"):
            return make_rule_check(
                rule,
                "not_relevant",
                "SQL 的主指标角色与该口径互斥："
                + "、".join(incompatible_roles),
                "event_signature incompatible metric role",
            )
    incompatible_predicates = event_signature_values(missing, "incompatible_predicate_present")
    if incompatible_predicates:
        if not (predicate_core_ok and role_core_ok and aggregation_core_ok and field_role_core_ok):
            return make_rule_check(
                rule,
                "not_relevant",
                "SQL 出现该规则的禁用条件，但没有命中该口径的主谓词、字段角色、指标角色和聚合核心；"
                "不作为本 SQL 使用口径冲突展示。",
                "event_signature incompatible predicate without core",
            )
        result = "conflict" if rule.status == "confirmed" else "proposed_conflict"
        return make_rule_check(
            rule,
            result,
            "SQL 使用了该口径明确禁止的条件："
            + "、".join(incompatible_predicates),
            "event_signature incompatible predicate",
        )

    if match.get("strength") == "exact":
        evidence_text = "；".join(f"{item.get('type')}={item.get('value')}" for item in matched[:8])
        return make_rule_check(rule, "matched", "SQL 事件签名与已保存口径一致。", evidence_text)

    matched_roles = event_signature_values(matched, "metric_role")
    if matched_roles:
        missing_text = "、".join(
            f"{item.get('type')}={item.get('value')}"
            for item in missing
            if isinstance(item, dict)
            and item.get("type") in {"required_predicate", "required_metric_role", "required_aggregation", "required_text_term"}
        )
        result = "needs_manual_check" if rule.status == "confirmed" else "proposed_conflict"
        return make_rule_check(
            rule,
            result,
            "SQL 主指标角色命中该保存口径，但事件签名证据不完整"
            + (f"；缺少：{missing_text}" if missing_text else "。"),
            "event_signature partial",
        )

    return make_rule_check(rule, "not_relevant", "未命中该口径的主指标角色；不作为本 SQL 使用口径展示。", "event_signature non-core")


def check_project_rules(review: FileReview, canonical_rules: list[CanonicalRule]) -> list[RuleCheck]:
    """Return only saved criteria proven by the current SQL event signature.

    Request-time activation is owned by rule-context. A review file does not
    carry that request reliably, so title, prose, token overlap, and shared-log
    similarity must never activate a rule here.
    """
    if not canonical_rules:
        return []
    checks: list[RuleCheck] = []
    for rule in canonical_rules:
        if rule.status not in {"confirmed", "proposed"}:
            continue
        contract = rule_activation_contract(rule)
        if contract.get("contract_version") != "canonical_rule_activation_v2":
            continue
        policy = contract.get("activation_policy")
        reverse_policy = (
            str(policy.get("reverse") or "disabled")
            if isinstance(policy, dict)
            else "disabled"
        )
        if reverse_policy == "disabled":
            continue
        event_check = event_signature_review_check(rule, review)
        if not event_check or event_check.result in {"not_relevant", "needs_manual_check", "proposed_conflict"}:
            continue
        checks.append(event_check)
    return checks


def split_top_level_and(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and value[index : index + 3].lower() == "and":
            before = value[index - 1] if index else " "
            after = value[index + 3] if index + 3 < len(value) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                item = value[start:index].strip()
                if item:
                    items.append(item)
                start = index + 3
                index += 3
                continue
        index += 1
    tail = value[start:].strip()
    if tail:
        items.append(tail)
    return items


def condition_looks_atomic(value: str) -> bool:
    text = compact(value)
    if not text or len(text) > 320:
        return False
    if text.count("(") != text.count(")"):
        return False
    lowered = text.lower()
    if re.search(r"\)\s*,\s*[a-z_][\w]*\s+as\s*\(", lowered):
        return False
    if re.search(r"\)\s+select\b", lowered):
        return False
    if re.search(r"\bfrom\b\s+[a-z_][\w.]*", lowered) and not re.search(r"\(\s*select\b", lowered):
        return False
    return True


def trim_condition_sql_tail(value: str) -> str:
    text = compact(value)
    for pattern in [
        r"\)\s+select\b",
        r"\)\s*,\s*[a-z_][\w]*\s+as\s*\(",
    ]:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        candidate = text[: match.start()].strip()
        if candidate and candidate.count("(") == candidate.count(")"):
            return candidate
    return text


def extract_where_conditions(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    stop = (
        r"(?=\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|\bunion\b|"
        r"\bqualify\b|;\s*$|$)"
    )
    conditions: list[str] = []
    for match in re.finditer(r"\bwhere\b(.*?)" + stop, cleaned, flags=re.I | re.S):
        for item in split_top_level_and(match.group(1)):
            text = trim_condition_sql_tail(item)
            if condition_looks_atomic(text):
                conditions.append(text)
    return unique_in_order(conditions)


def extract_join_conditions(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    stop = (
        r"(?=\b(?:left|right|inner|full|cross)?\s*join\b|\bwhere\b|"
        r"\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|\bunion\b|$)"
    )
    conditions: list[str] = []
    for match in re.finditer(r"\bon\b(.*?)" + stop, cleaned, flags=re.I | re.S):
        for item in split_top_level_and(match.group(1)):
            text = compact(item)
            if condition_looks_atomic(text):
                conditions.append(text)
    return unique_in_order(conditions)


def keyword_at(value: str, index: int, keyword: str) -> bool:
    end = index + len(keyword)
    if value[index:end].lower() != keyword:
        return False
    before = value[index - 1] if index else " "
    after = value[end] if end < len(value) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def select_segments(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    segments: list[str] = []
    depth = 0
    quote: str | None = None
    select_start: int | None = None
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and keyword_at(cleaned, index, "select"):
            select_start = index + len("select")
            index += len("select")
            continue
        if depth == 0 and select_start is not None and keyword_at(cleaned, index, "from"):
            segment = cleaned[select_start:index].strip()
            if segment:
                segments.append(segment)
            select_start = None
            index += len("from")
            continue
        index += 1
    return segments


def all_select_segments(sql: str) -> list[str]:
    cleaned = strip_sql_comments(sql)
    segments: list[str] = []
    depth = 0
    quote: str | None = None
    select_start: int | None = None
    select_depth = 0
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if keyword_at(cleaned, index, "select"):
            select_start = index + len("select")
            select_depth = depth
            index += len("select")
            continue
        if select_start is not None and depth == select_depth and keyword_at(cleaned, index, "from"):
            segment = cleaned[select_start:index].strip()
            if segment:
                segments.append(segment)
            select_start = None
            index += len("from")
            continue
        index += 1
    return segments


def final_select_segment(sql: str) -> str:
    segments = select_segments(sql)
    if segments:
        return segments[-1]
    cleaned = strip_sql_comments(sql)
    matches = list(re.finditer(r"\bselect\b(.*?)\bfrom\b", cleaned, flags=re.I | re.S))
    return matches[-1].group(1) if matches else ""


def select_output_alias(expression: str) -> str:
    expr = expression.strip()
    match = re.search(
        r"\bas\s+(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)'|([\w\u4e00-\u9fff][\w\u4e00-\u9fff]*))\s*$",
        expr,
        flags=re.I,
    )
    if match:
        return next(group for group in match.groups() if group)
    match = re.search(r"(?:^|\.|`)([a-zA-Z_][\w]*)`?\s*$", expr)
    if match:
        alias = match.group(1)
        if alias.lower() not in {"case", "when", "then", "else", "end", "null"}:
            return alias
    parts = re.split(r"\s+", expr)
    if len(parts) >= 2 and re.match(r"^[\w\u4e00-\u9fff]+$", parts[-1]):
        return parts[-1]
    return ""


def strip_output_alias(expression: str, alias: str) -> str:
    expr = expression.strip()
    if not alias:
        return compact(expr)
    escaped = re.escape(alias)
    patterns = [
        rf"\bas\s+`?{escaped}`?\s*$",
        rf"\bas\s+\"{escaped}\"\s*$",
        rf"\bas\s+'{escaped}'\s*$",
        rf"\s+`?{escaped}`?\s*$",
    ]
    for pattern in patterns:
        updated = re.sub(pattern, "", expr, flags=re.I).strip()
        if updated != expr:
            return compact(updated)
    return compact(expr)


def final_select_items(sql: str) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    for expression in split_top_level_csv(final_select_segment(sql)):
        alias = select_output_alias(expression)
        items.append((alias, compact(expression), strip_output_alias(expression, alias)))
    return items


def expanded_final_select_items(
    sql: str,
    cte_blocks: dict[str, str] | None = None,
    seen_ctes: set[str] | None = None,
) -> list[tuple[str, str, str]]:
    blocks = cte_blocks if cte_blocks is not None else extract_cte_blocks(sql)
    seen = seen_ctes or set()
    sources = final_source_references(sql)
    expanded: list[tuple[str, str, str]] = []
    for alias, expression, expression_without_alias in final_select_items(sql):
        raw = (expression_without_alias or expression).strip()
        star_match = re.fullmatch(r"(?:(`?[a-zA-Z_][\w]*`?)\.)?\*", raw)
        if not star_match:
            expanded.append((alias, expression, expression_without_alias))
            continue
        qualifier = normalize_identifier(star_match.group(1) or "")
        matched_sources = [
            source
            for source_alias, source in sources
            if not qualifier or qualifier in {source_alias, source}
        ]
        added = False
        for source in matched_sources:
            cte_name = normalize_identifier(source.rsplit(".", 1)[-1])
            body = blocks.get(cte_name)
            if not body or cte_name in seen:
                continue
            expanded.extend(expanded_final_select_items(body, blocks, seen | {cte_name}))
            added = True
        if not added:
            expanded.append((alias, expression, expression_without_alias))
    return expanded


def extract_final_fields(sql: str) -> list[str]:
    fields: list[str] = []
    for alias, expression, _ in expanded_final_select_items(sql):
        output_name = alias or strip_wrapping_parens(expression).strip().strip("`").strip('"').strip("'") or expression[:80]
        fields.append(output_name)
    return unique_in_order(fields)


def alias_matches_any(alias: str, patterns: list[str]) -> bool:
    normalized = normalize_identifier(alias)
    return any(re.search(pattern, normalized, flags=re.I) for pattern in patterns)


def expression_has_aggregation(expression: str) -> bool:
    return bool(
        re.search(
            r"\b(count|sum|avg|min|max|percentile(?:_approx)?|approx_count_distinct)\s*\(",
            expression,
            flags=re.I,
        )
    )


def expression_has_metric_arithmetic(expression: str) -> bool:
    text = unwrap_formula_expression(expression)
    return bool(split_top_level_operator(text, "/") or expression_has_aggregation(text))


def is_review_dimension_alias(alias: str) -> bool:
    return alias_matches_any(alias, DIMENSION_ALIAS_PATTERNS)


def is_review_metric_alias(alias: str) -> bool:
    return alias_matches_any(alias, METRIC_ALIAS_PATTERNS)


def alias_has_strong_metric_term(alias: str) -> bool:
    return bool(
        re.search(
            r"(?:人数|用户数|玩家数|次数|数量|个数|人均|占比|比例|比率|百分比|转化率|完成率|留存率|组队率|渗透率|时长|耗时|均值|平均|分位|_rate|_ratio|_pct|_percent)",
            alias,
            flags=re.I,
        )
    )


def alias_has_measure_metric_term(alias: str) -> bool:
    return bool(
        re.search(
            r"(?:人数|用户数|玩家数|次数|数量|个数|人均|占比|比例|比率|百分比|转化率|完成率|留存率|组队率|渗透率|均值|平均|分位|_rate|_ratio|_pct|_percent)",
            alias,
            flags=re.I,
        )
    )


def alias_has_strong_dimension_term(alias: str) -> bool:
    return bool(
        re.search(
            r"(?:区间|分组|分桶|桶|标签|阶段|步骤|排序|名称|_bucket|_group|_label|_tag|_stage|_step)",
            alias,
            flags=re.I,
        )
    )


def is_review_metric_expression(expression: str, alias: str) -> bool:
    if not alias:
        return False
    if is_review_dimension_alias(alias) and not expression_has_aggregation(expression):
        if (
            is_review_metric_alias(alias)
            and alias_has_strong_metric_term(alias)
            and (
                not alias_has_strong_dimension_term(alias)
                or (alias_has_measure_metric_term(alias) and not re.search(r"(?:区间|分组|排序|标签|阶段|步骤)$", alias))
            )
        ):
            return True
        return False
    if is_review_metric_alias(alias):
        return True
    if expression_has_metric_arithmetic(expression):
        return True
    if base_is_metric_expression(expression, alias) and not is_review_dimension_alias(alias):
        return True
    return False


def is_review_metric_with_definitions(expression: str, alias: str, definitions: dict[str, list[str]]) -> bool:
    if is_review_metric_expression(expression, alias):
        return True
    resolved, _ = resolve_metric_expression(alias, expression, definitions)
    return expression_has_metric_arithmetic(resolved) or is_review_metric_expression(resolved, alias)


def extract_metric_rules(sql: str) -> list[str]:
    rules: list[str] = []
    definitions = alias_definitions(sql)
    for alias, expression, expression_without_alias in expanded_final_select_items(sql):
        if alias and is_review_metric_with_definitions(expression_without_alias, alias, definitions):
            rules.append(f"{alias} := {compact(expression)}")
    return unique_in_order(rules)


def extract_review_fields(sql: str) -> tuple[list[str], list[str]]:
    metrics: list[str] = []
    dimensions: list[str] = []
    definitions = alias_definitions(sql)
    for alias, expression, expression_without_alias in expanded_final_select_items(sql):
        output_name = alias or strip_wrapping_parens(expression_without_alias or expression).strip().strip("`").strip('"').strip("'")
        if not output_name:
            continue
        if is_review_metric_with_definitions(expression_without_alias or expression, output_name, definitions):
            metrics.append(output_name)
        else:
            dimensions.append(output_name)
    return unique_in_order(metrics), unique_in_order(dimensions)


def normalize_identifier(value: str) -> str:
    text = value.strip().strip("`").strip('"').strip("'")
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.strip("`").lower()


def plain_identifier(value: str) -> str:
    text = value.strip()
    if re.match(r"^(?:[a-zA-Z_][\w]*\.)?`?[a-zA-Z_][\w]*`?$", text):
        return normalize_identifier(text)
    return ""


def alias_definitions(sql: str) -> dict[str, list[str]]:
    definitions: dict[str, list[str]] = defaultdict(list)
    for segment in all_select_segments(sql):
        for alias, expression, expression_without_alias in [
            (select_output_alias(item), compact(item), strip_output_alias(item, select_output_alias(item)))
            for item in split_top_level_csv(segment)
        ]:
            if not alias:
                continue
            normalized = normalize_identifier(alias)
            if expression_without_alias and plain_identifier(expression_without_alias) != normalized:
                definitions[normalized].append(expression_without_alias)
            elif expression:
                definitions[normalized].append(expression)
    return {key: unique_in_order(values) for key, values in definitions.items()}


def unwrap_formula_expression(expression: str) -> str:
    text = strip_wrapping_parens(expression)
    changed = True
    while changed:
        changed = False
        match = re.match(r"^case\b.*?\belse\b(.*)\bend$", text, flags=re.I | re.S)
        if match:
            text = strip_wrapping_parens(match.group(1))
            changed = True
            continue
        for function_name in ["round", "coalesce", "nvl"]:
            match = re.match(rf"^{function_name}\s*\((.*)\)$", text, flags=re.I | re.S)
            if match:
                args = split_top_level_csv(match.group(1))
                if args:
                    text = strip_wrapping_parens(args[0])
                    changed = True
                    break
        if changed:
            continue
        match = re.match(r"^cast\s*\((.*)\s+as\s+[a-zA-Z0-9_<>(),\s]+\)$", text, flags=re.I | re.S)
        if match:
            text = strip_wrapping_parens(match.group(1))
            changed = True
            continue
        match = re.match(r"^if\s*\((.*)\)$", text, flags=re.I | re.S)
        if match:
            args = split_top_level_csv(match.group(1))
            if len(args) >= 3:
                text = strip_wrapping_parens(args[-1])
                changed = True
    return text


def split_top_level_operator(expression: str, operator: str) -> tuple[str, str] | None:
    text = strip_wrapping_parens(expression)
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            continue
        if depth == 0 and char == operator:
            left = text[:index].strip()
            right = text[index + 1 :].strip()
            if left and right:
                return left, right
    return None


def function_args(expression: str, function_names: list[str]) -> list[str]:
    text = expression
    names = "|".join(re.escape(name) for name in function_names)
    for match in re.finditer(rf"\b(?:{names})\s*\(", text, flags=re.I):
        start = match.end() - 1
        depth = 0
        quote: str | None = None
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in {"'", '"', "`"}:
                quote = char
                continue
            if char == "(":
                depth += 1
                continue
            if char == ")":
                depth -= 1
                if depth == 0:
                    return split_top_level_csv(text[start + 1 : index])
    return []


def is_numeric_constant(expression: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", strip_wrapping_parens(expression).strip()))


def preferred_definition_candidate(candidates: list[str]) -> str:
    non_self = [item for item in candidates if item]
    for item in reversed(non_self):
        if expression_has_aggregation(item):
            return item
    for item in reversed(non_self):
        if re.search(r"\b(case\s+when|/|\+|-|\*)\b", item, flags=re.I):
            return item
    return non_self[-1] if non_self else ""


def resolve_metric_expression(alias: str, expression: str, definitions: dict[str, list[str]]) -> tuple[str, list[str]]:
    trail: list[str] = []
    current = strip_wrapping_parens(expression)
    seen: set[str] = set()
    for _ in range(6):
        identifier = plain_identifier(current)
        if not identifier or identifier in seen:
            break
        seen.add(identifier)
        candidates = [item for item in definitions.get(identifier, []) if plain_identifier(item) != identifier]
        if not candidates:
            break
        next_expression = preferred_definition_candidate(candidates)
        if not next_expression:
            break
        trail.append(f"{identifier} := {compact(next_expression)}")
        current = strip_wrapping_parens(next_expression)
    return compact(current), trail


def extract_case_conditions(expression: str) -> list[str]:
    return unique_in_order(
        compact(match.group(1))
        for match in re.finditer(r"\bwhen\b(.*?)\bthen\b", expression, flags=re.I | re.S)
        if compact(match.group(1))
    )


def summarize_operand(expression: str, definitions: dict[str, list[str]]) -> str:
    resolved, trail = resolve_metric_expression("", expression, definitions)
    text = re.sub(r"^(?:1(?:\.0+)?|100(?:\.0+)?)\s*\*\s*", "", resolved, flags=re.I).strip()
    args = function_args(text, ["percentile", "percentile_approx"])
    if len(args) >= 2:
        percentile = percentile_label(args[1])
        return f"{compact(simplified_sql_value_expression(args[0]))} 的{percentile}分位值"
    match = re.search(r"\bcount\s*\(\s*distinct\s+([^)]+)\)", text, flags=re.I)
    if match:
        return f"按 {compact(match.group(1))} 去重计数"
    match = re.search(r"\bcount\s*\(\s*(?:\*|1)\s*\)", text, flags=re.I)
    if match:
        return "行数"
    match = re.search(r"\bsum\s*\(\s*case\s+when\s+(.*?)\s+then\s+(.*?)\s+else\s+(.*?)\s+end\s*\)", text, flags=re.I | re.S)
    if match:
        return f"满足 {compact(match.group(1))} 的条件求和"
    args = function_args(text, ["sum"])
    if args:
        return f"{compact(simplified_sql_value_expression(args[0]))} 求和"
    args = function_args(text, ["avg"])
    if args:
        return f"{compact(simplified_sql_value_expression(args[0]))} 平均值"
    if trail:
        return f"{clip_text(expression)} -> {clip_text(text)}"
    return clip_text(text)


def business_definition_for(context: MetricBusinessContext, name: str) -> str:
    for key in business_key_variants(name):
        if key in context.definitions:
            return context.definitions[key]
    return ""


def friendly_identifier(value: str) -> str:
    normalized = normalize_identifier(value)
    labels = {
        "vopenid": "玩家 vOpenID",
        "openid": "玩家 OpenID",
        "roleid": "角色 ID",
        "deviceid": "设备 ID",
        "stat_date": "统计日期",
        "active_date": "活跃日期",
        "register_date": "注册日期",
        "max_team_number": "最大队伍人数",
        "team_number": "队伍人数",
        "gamemode": "玩法模式",
        "izoneareaid": "区服 iZoneAreaID",
        "battlesrvid": "战斗服 BattleSrvId",
        "tdbank_imp_date": "导入分区 tdbank_imp_date",
        "dteventtime": "业务事件时间",
    }
    return labels.get(normalized, value.strip())


def metric_count_noun(alias: str, expression: str = "") -> str:
    text = f"{alias} {expression}".lower()
    raw_text = f"{alias} {expression}"
    if "quit" in text:
        return "跳出人数"
    if "match" in text:
        return "匹配次数"
    if "battle" in text:
        return "战斗次数"
    if "login" in text:
        return "登录用户数"
    if "人数" in raw_text or "用户" in raw_text or "玩家" in raw_text:
        return "用户数"
    if "次数" in raw_text:
        return "次数"
    if "user" in text or "openid" in text or "玩家" in text or "用户" in text:
        return "用户数"
    if "day" in text or "date" in text:
        return "记录数"
    return "记录数"


def condition_to_business(condition: str) -> str:
    text = compact(condition)
    replacements = {
        r"\bmax_team_number\b": "最大队伍人数",
        r"\bteam_number\b": "队伍人数",
        r"\bvopenid\b": "玩家 vOpenID",
        r"\bactive_date\b": "活跃日期",
        r"\bgamemode\b": "玩法模式",
        r"\bizoneareaid\b": "区服 iZoneAreaID",
        r"\bresult_type\b": "匹配结果",
        r"\bmatch_duration_sec\b": "匹配耗时秒数",
        r"\bmatch_mode_id\b": "匹配模式",
        r"\bbucket_code\b": "分桶",
        r"\bhas_special_mode\b": "是否进入其他模式",
        r"\bhas_regular_mode\b": "是否进入常规模式",
    }
    for pattern, label in replacements.items():
        text = re.sub(pattern, label, text, flags=re.I)
    text = re.sub(r"\bis\s+not\s+null\b", "非空", text, flags=re.I)
    text = re.sub(r"\bis\s+null\b", "为空", text, flags=re.I)
    text = re.sub(r"\s*=\s*", " 为 ", text)
    text = re.sub(r"\s*>\s*", " 大于 ", text)
    text = re.sub(r"\s*<\s*", " 小于 ", text)
    text = re.sub(r"\s+in\s*\(", " 属于 (", text, flags=re.I)
    return text


def bucket_condition_business(condition: str, context: MetricBusinessContext) -> str:
    match = re.search(r"\bbucket_code\b\s*=\s*'([^']+)'", condition, flags=re.I)
    if not match:
        return ""
    bucket = match.group(1)
    bucket_description = context.bucket_definitions.get(bucket)
    if not bucket_description:
        upper_bucket = bucket.upper()
        plus_match = re.fullmatch(r"H(\d+)P", upper_bucket)
        if plus_match:
            bucket_description = f"累计常规服战斗时长 > {int(plus_match.group(1))} 小时"
    if not bucket_description:
        return f"分桶为 {bucket}"
    if context.duration_logic:
        return f"{bucket_description}（累计时长算法：{context.duration_logic}）"
    return bucket_description


def metric_condition_card(
    condition: str,
    context: MetricBusinessContext,
    structured_filters: list[dict],
) -> dict:
    bucket_business = bucket_condition_business(condition, context)
    if bucket_business:
        business_effect = bucket_business
    else:
        matching_effects = [
            item.get("business_effect", "")
            for item in structured_filters
            if item.get("condition") == condition and item.get("business_effect")
        ]
        business_effect = "；".join(matching_effects) or condition_to_business(condition)
    return {
        "condition": clip_text(condition, 300),
        "business_effect": clip_text(business_effect, 300),
        "how_to_judge": "确认这条条件是否正是该指标分子/分桶/步骤的业务定义；尤其核对 ID 范围、边界是否包含、字段单位和 NULL 处理。",
        "pass_criteria": "通过标准：条件能被业务方用中文复述，且 SQL 条件、映射口径、结果样例三者一致。",
        "source": "CASE/IF metric condition",
    }


def percentile_label(value: str) -> str:
    try:
        number = float(value)
    except ValueError:
        return value
    if number <= 1:
        number *= 100
    return f"P{int(round(number))}"


def simplified_sql_value_expression(expression: str) -> str:
    text = strip_wrapping_parens(compact(expression))
    match = re.match(r"^cast\s*\((.*)\s+as\s+[a-zA-Z0-9_<>(),\s]+\)$", text, flags=re.I | re.S)
    if match:
        return simplified_sql_value_expression(match.group(1))
    return text


def ratio_description_parts(description: str) -> tuple[str, str] | None:
    text = compact(description)
    text = re.split(r"[。；;，,]", text, maxsplit=1)[0].strip()
    separator = "÷" if "÷" in text else "/" if "/" in text else ""
    if not separator:
        return None
    left, right = [part.strip() for part in text.split(separator, 1)]
    if not left or not right:
        return None
    labels = {
        "本步": "本步指标值",
        "上一步": "上一步指标值",
        "首步": "首步指标值",
    }
    left = labels.get(left, left)
    right = labels.get(right, right)
    return left, right


def weak_business_phrase(value: str) -> bool:
    text = compact(value)
    if not text:
        return True
    if re.fullmatch(r"[a-zA-Z_][\w.]*", text):
        return True
    return False


def source_comment_for_identifier(identifier: str, context: MetricBusinessContext) -> str:
    normalized = normalize_identifier(identifier)
    source = context.source_aliases.get(normalized, normalized)
    return context.cte_comments.get(source, "")


def describe_operand_business(
    expression: str,
    definitions: dict[str, list[str]],
    context: MetricBusinessContext,
    metric_alias: str = "",
    seen: set[str] | None = None,
) -> tuple[str, str]:
    seen = seen or set()
    text = unwrap_formula_expression(strip_wrapping_parens(expression))
    text = re.sub(r"^(?:1(?:\.0+)?|100(?:\.0+)?)\s*\*\s*", "", text, flags=re.I).strip()
    _, field = qualified_identifier_parts(text)
    if field and field not in seen:
        cte_description, cte_source = describe_cte_operand_business(text, definitions, context, metric_alias)
        if cte_description:
            return cte_description, cte_source
    identifier = plain_identifier(text)
    if identifier:
        direct = business_definition_for(context, identifier)
        if direct:
            return direct, "sql_comment"
        cte_comment = source_comment_for_identifier(text, context)
        if cte_comment:
            return cte_comment, "cte_comment"
        if identifier not in seen and definitions.get(identifier):
            seen.add(identifier)
            return describe_operand_business(definitions[identifier][-1], definitions, context, metric_alias, seen)

    match = re.search(r"\bcount\s*\(\s*distinct\s+([^)]+)\)", text, flags=re.I)
    if match:
        field = friendly_identifier(match.group(1))
        if normalize_identifier(match.group(1)) in {"vopenid", "openid"}:
            return f"按 {field} 去重的玩家数", "static_inference"
        return f"按 {field} 去重的数量", "static_inference"

    args = function_args(text, ["percentile", "percentile_approx"])
    if len(args) >= 2:
        field = friendly_identifier(simplified_sql_value_expression(args[0]))
        return f"Base 中字段「{field}」的{percentile_label(args[1])}分位值", "static_inference"

    match = re.search(r"\bcount\s*\(\s*(?:\*|1)\s*\)", text, flags=re.I)
    if match:
        noun = metric_count_noun(metric_alias, text)
        if context.base_description:
            return f"Base 中的{noun}（{context.base_description}）", "static_inference"
        return f"当前 Base 行集中的{noun}", "static_inference"

    match = re.search(r"\bsum\s*\(\s*case\s+when\s+(.*?)\s+then\s+(.*?)\s+else\s+(.*?)\s+end\s*\)", text, flags=re.I | re.S)
    if match:
        noun = metric_count_noun(metric_alias, text)
        bucket_description = bucket_condition_business(match.group(1), context)
        if bucket_description:
            return f"Base 中满足「{bucket_description}」的{noun}", "static_inference"
        return f"Base 中满足「{condition_to_business(match.group(1))}」的{noun}", "static_inference"

    args = function_args(text, ["sum"])
    if args:
        return f"Base 中字段「{friendly_identifier(simplified_sql_value_expression(args[0]))}」的求和值", "static_inference"

    args = function_args(text, ["avg"])
    if args:
        return f"Base 中字段「{friendly_identifier(simplified_sql_value_expression(args[0]))}」的平均值", "static_inference"

    return clip_text(text), "needs_manual_confirmation"


def metric_label(alias: str) -> str:
    text = alias.strip()
    match = re.match(r"team(\d+)_user_cnt$", text, flags=re.I)
    if match:
        return f"{match.group(1)}人队用户数"
    match = re.match(r"team(\d+)_rate$", text, flags=re.I)
    if match:
        return f"{match.group(1)}人队用户占比"
    return text


def combine_sources(*sources: str) -> str:
    ordered = ["sql_comment", "cte_comment", "static_inference", "needs_manual_confirmation"]
    present = {source for source in sources if source}
    for source in ordered:
        if source in present:
            return source
    return "needs_manual_confirmation"


def build_metric_business_descriptions(
    alias: str,
    calculation_type: str,
    resolved_expression: str,
    formula_expression: str,
    division: tuple[str, str] | None,
    definitions: dict[str, list[str]],
    context: MetricBusinessContext,
) -> dict[str, str]:
    subject_alias = metric_subject_from_alias(alias)
    direct_definition = business_definition_for(context, alias) or business_definition_for(context, subject_alias)
    base_description = context.base_description
    source = "sql_comment" if direct_definition else ""

    if division and calculation_type != "unit_conversion":
        numerator_description, numerator_source = describe_operand_business(division[0], definitions, context, alias)
        denominator_description, denominator_source = describe_operand_business(division[1], definitions, context, alias)
        direct_ratio_parts = ratio_description_parts(direct_definition) if direct_definition else None
        if direct_ratio_parts:
            if numerator_source == "needs_manual_confirmation" or weak_business_phrase(numerator_description):
                numerator_description = direct_ratio_parts[0]
            if denominator_source == "needs_manual_confirmation" or weak_business_phrase(denominator_description):
                denominator_description = direct_ratio_parts[1]
        if not base_description and denominator_description:
            base_description = f"以分母作为比率基准：{denominator_description}"
        formula_description = direct_definition or f"{numerator_description} ÷ {denominator_description}"
        business_definition = direct_definition or (
            f"{metric_label(alias)}：分子是{numerator_description}；分母是{denominator_description}。"
        )
        source = source or combine_sources(numerator_source, denominator_source)
    elif division and calculation_type == "unit_conversion":
        numerator_description, numerator_source = describe_operand_business(division[0], definitions, context, alias)
        denominator_description = "不适用（除以常数用于单位换算，不是业务分母）"
        if not base_description:
            base_description = source_comment_for_identifier(alias, context) or "按 SQL WHERE/JOIN 过滤后的当前结果集"
        formula_description = direct_definition or f"{numerator_description}，再除以 {clip_text(division[1], 80)} 做单位换算"
        business_definition = direct_definition or f"{metric_label(alias)}：{formula_description}。"
        source = source or numerator_source
    elif calculation_type == "percentile":
        numerator_description, numerator_source = describe_operand_business(resolved_expression, definitions, context, alias)
        denominator_description = "不适用（分位值指标没有业务分母）"
        if not base_description:
            base_description = source_comment_for_identifier(alias, context) or "按 SQL WHERE/JOIN 过滤后的当前结果集"
        formula_description = direct_definition or numerator_description
        business_definition = direct_definition or f"{metric_label(alias)}：{numerator_description}。"
        source = source or numerator_source
    else:
        numerator_description, numerator_source = describe_operand_business(resolved_expression, definitions, context, alias)
        denominator_description = "不适用（非比率/均值指标）"
        if calculation_type == "average":
            denominator_description = "参与平均值计算的非空记录"
        if direct_definition:
            numerator_description = direct_definition
        if not base_description:
            base_description = source_comment_for_identifier(alias, context) or "按 SQL WHERE/JOIN 过滤后的当前结果集"
        formula_description = direct_definition or numerator_description
        business_definition = direct_definition or f"{metric_label(alias)}：{numerator_description}。"
        source = source or numerator_source

    if not base_description:
        base_description = "需要人工确认 Base 人群/记录范围"
        source = source or "needs_manual_confirmation"

    return {
        "business_definition": business_definition,
        "base_description": base_description,
        "numerator_description": numerator_description,
        "denominator_description": denominator_description,
        "formula_description": formula_description,
        "description_source": source or "static_inference",
    }


def metric_logic_items(review: FileReview) -> list[dict]:
    definitions = alias_definitions(review.sql)
    business_context = metric_business_context(review.sql)
    constants = extract_constant_aliases(review.sql)
    global_filters = extract_where_conditions(review.sql)
    joins = extract_join_conditions(review.sql)
    metric_rules = [rule for rule in review.rules if rule.kind == "metric"]
    filter_rules = [rule for rule in review.rules if rule.kind == "filter"]
    join_rules = [rule for rule in review.rules if rule.kind == "join"]
    items: list[dict] = []
    for alias, expression, expression_without_alias in expanded_final_select_items(review.sql):
        if not alias:
            alias = strip_wrapping_parens(expression_without_alias or expression).strip().strip("`").strip('"').strip("'")
        if not alias or not is_review_metric_with_definitions(expression_without_alias, alias, definitions):
            continue
        resolved_expression, lineage = resolve_metric_expression(alias, expression_without_alias, definitions)
        formula_expression = unwrap_formula_expression(resolved_expression)
        division = split_top_level_operator(formula_expression, "/")
        numerator = ""
        denominator = ""
        base_population = ""
        metric_filters = extract_case_conditions(resolved_expression)
        metric_business_filters = metric_business_filters_from_conditions(
            metric_filters,
            review.business_filter_mappings,
            constants,
        )
        metric_condition_cards = [
            metric_condition_card(condition, business_context, metric_business_filters)
            for condition in metric_filters
        ]
        calculation_type = "derived_metric"
        confidence = "medium"
        needs_manual_confirmation = False
        if division and is_numeric_constant(division[1]):
            numerator = summarize_operand(division[0], definitions)
            denominator = "not_applicable_unit_conversion"
            base_population = "rows after WHERE/JOIN filters"
            calculation_type = "unit_conversion"
        elif division:
            numerator = summarize_operand(division[0], definitions)
            denominator = summarize_operand(division[1], definitions)
            base_population = f"denominator: {denominator}"
            calculation_type = "ratio"
        elif re.search(r"\bcount\s*\(\s*distinct\b", resolved_expression, flags=re.I):
            numerator = summarize_operand(resolved_expression, definitions)
            denominator = "not_applicable"
            base_population = f"{numerator} after WHERE/JOIN filters"
            calculation_type = "count_distinct"
        elif re.search(r"\bcount\s*\(\s*(?:\*|1)\s*\)", resolved_expression, flags=re.I):
            numerator = "row count"
            denominator = "not_applicable"
            base_population = "rows after WHERE/JOIN filters"
            calculation_type = "row_count"
        elif re.search(r"\bsum\s*\(\s*case\s+when\b", resolved_expression, flags=re.I):
            numerator = summarize_operand(resolved_expression, definitions)
            denominator = "not_applicable"
            base_population = "rows after WHERE/JOIN filters; numerator applies CASE condition"
            calculation_type = "conditional_sum"
        elif re.search(r"\bsum\s*\(", resolved_expression, flags=re.I):
            numerator = summarize_operand(resolved_expression, definitions)
            denominator = "not_applicable"
            base_population = "rows after WHERE/JOIN filters"
            calculation_type = "sum"
        elif re.search(r"\bpercentile(?:_approx)?\s*\(", resolved_expression, flags=re.I):
            numerator = summarize_operand(resolved_expression, definitions)
            denominator = "not_applicable"
            base_population = "rows after WHERE/JOIN filters"
            calculation_type = "percentile"
        elif re.search(r"\bavg\s*\(", resolved_expression, flags=re.I):
            numerator = summarize_operand(resolved_expression, definitions)
            denominator = "non-null rows in average expression"
            base_population = f"denominator: {denominator}"
            calculation_type = "average"
        else:
            numerator = summarize_operand(resolved_expression, definitions)
            denominator = "unknown"
            base_population = "needs_manual_confirmation"
            confidence = "low"
            needs_manual_confirmation = True
        business = build_metric_business_descriptions(
            alias=alias,
            calculation_type=calculation_type,
            resolved_expression=resolved_expression,
            formula_expression=formula_expression,
            division=division,
            definitions=definitions,
            context=business_context,
        )
        if division and calculation_type != "unit_conversion":
            source_steps = [
                cte_operand_card("numerator", division[0], definitions, business_context, alias),
                cte_operand_card("denominator", division[1], definitions, business_context, alias),
            ]
        elif division and calculation_type == "unit_conversion":
            source_steps = [
                cte_operand_card("value", division[0], definitions, business_context, alias),
                {
                    "role": "unit_conversion_constant",
                    "operand": clip_text(division[1], 240),
                    "source_step": "",
                    "source_tables": [],
                    "group_by": [],
                    "business_filters": [],
                    "field_expression": clip_text(division[1], 300),
                    "story": "这是单位换算常数，不是业务分母。",
                    "lineage": [],
                    "source": "static_inference",
                },
            ]
        else:
            source_steps = [
                cte_operand_card("metric_value", formula_expression, definitions, business_context, alias)
            ]
        base_filter_effects = [
            item["business_effect"]
            for item in review.business_filters
            if item.get("scope") == "base_filter"
        ]
        if base_filter_effects:
            business["base_description"] = (
                business["base_description"].rstrip("。")
                + "；Base 级筛选："
                + "；".join(base_filter_effects[:8])
            )
        if metric_condition_cards and calculation_type in {"conditional_sum", "derived_metric"}:
            business["formula_description"] = (
                business["formula_description"].rstrip("。")
                + "；指标内条件："
                + "；".join(card["business_effect"] for card in metric_condition_cards[:4])
            )
        if business["description_source"] == "sql_comment":
            confidence = "high"
            needs_manual_confirmation = False
        elif business["description_source"] == "cte_comment" and confidence == "medium":
            confidence = "high"
        if business["description_source"] == "needs_manual_confirmation":
            confidence = "low"
            needs_manual_confirmation = True
        related_checks = [
            {
                "rule_id": check.rule_id,
                "concept_key": check.concept_key,
                "title": check.title,
                "status": check.status,
                "result": check.result,
                "message": check.message,
                "rule_summary": check.rule_summary,
            }
            for check in review.rule_checks
            if normalize_identifier(alias) in normalize_rule_text(f"{check.evidence} {check.message}")
            or any(token in normalize_rule_text(f"{check.evidence} {check.message}") for token in extract_rule_tokens(resolved_expression))
        ]
        items.append(
            {
                "metric": alias,
                "calculation_type": calculation_type,
                "expression": clip_text(expression, 500),
                "resolved_expression": clip_text(resolved_expression, 500),
                "lineage": [clip_text(item, 300) for item in lineage],
                "business_definition": business["business_definition"],
                "base_population": business["base_description"],
                "numerator": business["numerator_description"],
                "denominator": business["denominator_description"],
                "formula": business["formula_description"],
                "description_source": business["description_source"],
                "source_steps": source_steps,
                "formula_expression": clip_text(formula_expression, 500),
                "base_expression": base_population,
                "numerator_expression": numerator,
                "denominator_expression": denominator,
                "metric_filters": [clip_text(item) for item in metric_filters],
                "base_business_filters": [
                    item for item in review.business_filters if item.get("scope") == "base_filter"
                ][:12],
                "join_business_filters": [
                    item for item in review.business_filters if item.get("scope") == "join_mapping"
                ][:12],
                "metric_business_filters": metric_business_filters[:12],
                "business_filters": unique_business_filters(
                    [
                        item for item in review.business_filters if item.get("scope") == "base_filter"
                    ][:8]
                    + metric_business_filters[:8]
                ),
                "metric_condition_cards": metric_condition_cards[:12],
                "global_filters": [clip_text(item) for item in global_filters[:12]],
                "join_logic": [clip_text(item) for item in joins[:8]],
                "related_inferred_metric_rules": [clip_text(rule.text) for rule in metric_rules if normalize_identifier(alias) in rule.normalized][:5],
                "related_filter_rules": [clip_text(rule.text) for rule in filter_rules[:8]],
                "related_join_rules": [clip_text(rule.text) for rule in join_rules[:5]],
                "related_saved_rule_checks": related_checks[:8],
                "confidence": confidence,
                "needs_manual_confirmation": needs_manual_confirmation,
            }
        )
    return items


def split_business_path_text(value: str) -> list[str]:
    text = compact(value)
    if not text:
        return []
    for separator in ["→", "->", "=>"]:
        if separator in text:
            return [clip_text(item.strip(" -→>="), 180) for item in text.split(separator) if item.strip(" -→>=")]
    return [clip_text(text.strip(" -→>="), 240)]


def extract_metric_calculation_path(sql: str) -> list[str]:
    context = metric_business_context(sql)
    steps: list[str] = []
    for line in context.comment_lines:
        text = strip_comment_numbering(line)
        label, description = split_comment_definition(text)
        normalized_label = normalize_business_key(label)
        if normalized_label in {
            normalize_business_key("计算逻辑"),
            normalize_business_key("计算路径"),
        }:
            steps.extend(split_business_path_text(description))
            continue
        if normalized_label in {
            normalize_business_key("Base"),
            normalize_business_key("输出"),
            normalize_business_key("分子"),
            normalize_business_key("分母"),
        }:
            steps.append(f"{label}：{description}")
            continue
        if text.lstrip().startswith(("→", "->", "=>")):
            steps.extend(split_business_path_text(text))
            continue
        if any(keyword in text for keyword in ["活跃", "新增", "回流", "组队判定", "常规服", "去重", "剔除"]):
            steps.append(text)
    for cte_name, comment in context.cte_comments.items():
        if any(skip in comment for skip in ["配置区结束", "参数"]):
            continue
        label = cte_name
        steps.append(f"{label}: {comment}")
    if not steps:
        sources = sorted(set(context.source_aliases.values()))
        if sources:
            steps.append("最终 SELECT 来自：" + ", ".join(sources[:5]))
    return unique_in_order(steps)[:12]


def common_metric_base(metrics: list[dict]) -> str:
    bases = [item.get("base_population", "") for item in metrics if item.get("base_population")]
    if not bases:
        return "未识别，需要人工确认 Base"
    counts = Counter(bases)
    return counts.most_common(1)[0][0]


def dimension_role(alias: str) -> str:
    normalized = normalize_identifier(alias)
    if normalized in {"stat_date", "active_date", "register_date"} or normalized.endswith(("_date", "_day", "_hour")):
        return "时间维度"
    if normalized.endswith("_bucket"):
        return "分桶维度"
    if normalized.endswith("_name"):
        return "展示名称维度"
    if normalized.endswith("_id"):
        return "ID 维度"
    if normalized.endswith("_type") or normalized.endswith("_status") or normalized.endswith("_result"):
        return "分类维度"
    if normalized.endswith("_level"):
        return "等级/层级维度"
    if normalized.endswith("_order") or normalized.endswith("_no"):
        return "排序/步骤维度"
    if normalized.startswith("is_"):
        return "布尔标记维度"
    if "mode" in normalized:
        return "模式维度"
    if "team" in normalized:
        return "队伍维度"
    return "分组维度"


def dimension_description(alias: str, context: MetricBusinessContext) -> tuple[str, str, str]:
    direct = business_definition_for(context, alias)
    if direct:
        return direct, "sql_comment", "high"
    cte_comment = source_comment_for_identifier(alias, context)
    if cte_comment:
        return cte_comment, "cte_comment", "medium"
    role = dimension_role(alias)
    label = friendly_identifier(alias)
    return f"{label}：用于拆分结果的{role}。", "static_inference", "medium"


def metric_dimension_cards(review: FileReview, context: MetricBusinessContext) -> list[dict]:
    cards: list[dict] = []
    for dimension in review.dimensions:
        description, source, confidence = dimension_description(dimension, context)
        cards.append(
            {
                "field": dimension,
                "role": dimension_role(dimension),
                "description": description,
                "source": source,
                "confidence": confidence,
            }
        )
    return cards


def grouping_summary(review: FileReview) -> str:
    if not review.dimensions:
        return "未识别明确分组字段，可能是整体汇总。"
    return "按 " + "、".join(review.dimensions[:10]) + " 分组输出。"


def comment_value(comment_lines: list[str], labels: list[str]) -> str:
    normalized_labels = {normalize_business_key(label) for label in labels}
    for line in comment_lines:
        label, description = split_comment_definition(line)
        if normalize_business_key(label) in normalized_labels and description:
            return description
    return ""


def extract_funnel_comment_steps(sql: str) -> list[str]:
    lines = extract_comment_lines(sql)
    steps: list[str] = []
    collecting = False
    for line in lines:
        text = strip_comment_numbering(line).strip()
        if not collecting:
            if "漏斗顺序" in text or "步骤顺序" in text:
                collecting = True
                _, tail = split_comment_definition(text)
                if tail:
                    steps.extend(split_business_path_text(tail))
                continue
        else:
            if re.match(r"^\d+\s*[.、]", line) or "各事件首次" in text or "业务说明" in text:
                break
            cleaned = text.strip(" -→>=")
            if cleaned and not any(sep in cleaned for sep in ["：", ":"]) and len(cleaned) <= 80:
                steps.append(cleaned)
                continue
            if "→" in text or "->" in text:
                steps.extend(split_business_path_text(text))
                continue
            break
    return unique_in_order(steps)


def extract_steps_cte_rows(sql: str) -> dict[int, dict[str, str]]:
    cleaned = strip_sql_comments(sql)
    rows: dict[int, dict[str, str]] = {}
    first_pattern = re.compile(
        r"select\s+(?P<order>\d+)\s+as\s+step_order\s*,\s*'(?P<name>[^']+)'\s+as\s+step_name\s*,\s*"
        r"(?P<count>[a-zA-Z_][\w]*)\s+as\s+step_cnt\s*,\s*(?P<prev>[a-zA-Z_][\w]*)\s+as\s+prev_cnt\s*,\s*"
        r"(?P<first>[a-zA-Z_][\w]*)\s+as\s+first_cnt",
        flags=re.I | re.S,
    )
    union_pattern = re.compile(
        r"union\s+all\s+select\s+(?P<order>\d+)\s*,\s*'(?P<name>[^']+)'\s*,\s*"
        r"(?P<count>[a-zA-Z_][\w]*)\s*,\s*(?P<prev>[a-zA-Z_][\w]*)\s*,\s*(?P<first>[a-zA-Z_][\w]*)\s+from\b",
        flags=re.I | re.S,
    )
    for pattern in [first_pattern, union_pattern]:
        for match in pattern.finditer(cleaned):
            order = int(match.group("order"))
            rows[order] = {
                "order": str(order),
                "step_name": match.group("name"),
                "count_alias": match.group("count"),
                "prev_count_alias": match.group("prev"),
                "first_count_alias": match.group("first"),
            }
    map_first_pattern = re.compile(
        r"select\s+(?P<order>\d+)\s+as\s+(?:step_order|step_no)\s*,\s*'(?P<name>[^']+)'\s+as\s+"
        r"(?:step_name|stage_name|task_name)",
        flags=re.I | re.S,
    )
    map_union_pattern = re.compile(
        r"union\s+all\s+select\s+(?P<order>\d+)\s*,\s*'(?P<name>[^']+)'\b",
        flags=re.I | re.S,
    )
    for match in map_first_pattern.finditer(cleaned):
        order = int(match.group("order"))
        rows.setdefault(
            order,
            {
                "order": str(order),
                "step_name": match.group("name"),
                "count_alias": f"cnt{order}",
                "prev_count_alias": f"cnt{max(1, order - 1)}",
                "first_count_alias": "cnt1",
            },
        )
        cte_tail = cleaned[match.end():]
        end_match = re.search(r"\n\s*\)\s*,", cte_tail)
        segment = cte_tail[: end_match.start()] if end_match else cte_tail[:4000]
        for union_match in map_union_pattern.finditer(segment):
            union_order = int(union_match.group("order"))
            rows.setdefault(
                union_order,
                {
                    "order": str(union_order),
                    "step_name": union_match.group("name"),
                    "count_alias": f"cnt{union_order}",
                    "prev_count_alias": f"cnt{max(1, union_order - 1)}",
                    "first_count_alias": "cnt1",
                },
            )
    stage_first_pattern = re.compile(
        r"select\s+(?P<stage>\d+)\s+as\s+stage_id\s*,\s*(?P<order>\d+)\s+as\s+step_no",
        flags=re.I | re.S,
    )
    stage_union_pattern = re.compile(
        r"union\s+all\s+select\s+(?P<stage>\d+)\s*,\s*(?P<order>\d+)\b",
        flags=re.I | re.S,
    )
    for match in stage_first_pattern.finditer(cleaned):
        order = int(match.group("order"))
        rows.setdefault(
            order,
            {
                "order": str(order),
                "step_name": f"stage_id={match.group('stage')}",
                "count_alias": f"cnt{order}",
                "prev_count_alias": f"cnt{max(1, order - 1)}",
                "first_count_alias": "cnt1",
            },
        )
        cte_tail = cleaned[match.end():]
        end_match = re.search(r"\n\s*\)\s*,", cte_tail)
        segment = cte_tail[: end_match.start()] if end_match else cte_tail[:8000]
        for union_match in stage_union_pattern.finditer(segment):
            union_order = int(union_match.group("order"))
            rows.setdefault(
                union_order,
                {
                    "order": str(union_order),
                    "step_name": f"stage_id={union_match.group('stage')}",
                    "count_alias": f"cnt{union_order}",
                    "prev_count_alias": f"cnt{max(1, union_order - 1)}",
                    "first_count_alias": "cnt1",
                },
            )
    return rows


def extract_funnel_source_steps(sql: str) -> dict[int, dict[str, str]]:
    cleaned = strip_sql_comments(sql)
    sources: dict[int, dict[str, str]] = {}
    pattern = re.compile(
        r"\b(?P<cte>s(?P<order>\d+))\s+as\s*\(\s*select\s+deviceid\s*,\s*"
        r"min\s*\(\s*dteventtime\s*\)\s+as\s+(?P<time_alias>t\d+).*?"
        r"\bfrom\s+(?P<table>[a-zA-Z_][\w.]*).*?\bgroup\s+by\s+deviceid\s*\)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(cleaned):
        order = int(match.group("order"))
        table = match.group("table")
        sources[order] = {
            "source_cte": match.group("cte"),
            "source_table": table,
            "first_time_alias": match.group("time_alias"),
            "source_log": table.split(".")[-1],
        }
    return sources


def extract_funnel_reach_conditions(sql: str) -> dict[int, str]:
    cleaned = strip_sql_comments(sql)
    conditions: dict[int, str] = {}
    pattern = re.compile(
        r"case\s+when\s+(?P<condition>.*?)\s+then\s+1\s+else\s+0\s+end\s+as\s+c(?P<order>\d+)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(cleaned):
        order = int(match.group("order"))
        conditions[order] = compact(match.group("condition"))
    return conditions


def extract_funnel_review(review: FileReview, metrics: list[dict] | None = None) -> dict:
    sql = review.sql
    comment_lines = extract_comment_lines(sql)
    comment_steps = extract_funnel_comment_steps(sql)
    step_rows = extract_steps_cte_rows(sql)
    source_rows = extract_funnel_source_steps(sql)
    reach_conditions = extract_funnel_reach_conditions(sql)
    output_fields = {normalize_identifier(field) for field in review.final_fields}
    metric_names = {normalize_identifier(item.get("metric", "")) for item in (metrics or [])}
    is_funnel = bool(
        comment_steps
        or step_rows
        or source_rows
        or {"step_order", "step_name"}.issubset(output_fields)
        or {"step_cnt", "conv_from_prev", "conv_from_first"} & metric_names
    )
    if not is_funnel:
        return {"detected": False, "steps": []}

    max_order = max([*step_rows.keys(), *source_rows.keys(), len(comment_steps)] or [0])
    steps: list[dict[str, str]] = []
    for order in range(1, max_order + 1):
        row = step_rows.get(order, {})
        source = source_rows.get(order, {})
        step_name = row.get("step_name") or (comment_steps[order - 1] if order <= len(comment_steps) else f"step_{order}")
        source_table = source.get("source_table", "")
        time_alias = source.get("first_time_alias", f"t{order}")
        if order == 1:
            reach_rule = f"首步 Base：窗口内上报 {step_name} 且 DeviceId 非空的设备；取最早 dtEventTime。"
        else:
            current_condition = reach_conditions.get(order)
            if current_condition:
                reach_rule = f"必须已严格到达前 {order - 1} 步，且本步满足 {condition_to_business(current_condition)}。"
            else:
                reach_rule = f"必须已严格到达前 {order - 1} 步，且本步首次时间不早于上一漏斗步骤。"
        steps.append(
            {
                "order": order,
                "step_name": step_name,
                "source_table": source_table,
                "source_cte": source.get("source_cte", ""),
                "first_time_alias": time_alias,
                "count_alias": row.get("count_alias", f"cnt{order}"),
                "prev_count_alias": row.get("prev_count_alias", f"cnt{max(1, order - 1)}"),
                "first_count_alias": row.get("first_count_alias", "cnt1"),
                "reach_rule": reach_rule,
                "how_to_judge": (
                    "核对该步骤是否应该出现在漏斗顺序里；确认来源表、DeviceId 去重、首次时间和与上一阶段的顺序条件都正确。"
                ),
                "pass_criteria": (
                    "步骤名称和业务顺序被确认；来源表正确；设备只按 DeviceId 去重一次；本步人数不能大于上一严格到达步骤。"
                ),
            }
        )

    first_step = steps[0]["step_name"] if steps else "首步"
    last_step = steps[-1]["step_name"] if steps else "末步"
    dedup = comment_value(comment_lines, ["去重维度"]) or "DeviceId 非空；每步取窗口内 MIN(dtEventTime) 作为首次到达时间"
    time_window = comment_value(comment_lines, ["时间范围", "时间"])
    partition_window = comment_value(comment_lines, ["分区范围"])
    strict_rule = ""
    for line in comment_lines:
        if "严格顺序" in line:
            strict_rule = strip_comment_numbering(line)
            break
    if not strict_rule:
        strict_rule = "设备计入第 k 步时，必须满足第 1..k 步均到达，且相邻步骤首次时间非递减。"

    return {
        "detected": True,
        "type": "strict_order_funnel",
        "summary": f"{len(steps)} 步严格顺序漏斗：{first_step} -> {last_step}。",
        "base": f"首步 {first_step} 的非空 DeviceId 设备集合；后续每一步只在已严格到达前序步骤的设备内继续统计。",
        "dedup_grain": dedup,
        "time_window": time_window,
        "partition_window": partition_window,
        "strict_order_rule": strict_rule,
        "step_count_metric": "step_cnt：满足严格顺序到达当前步骤的去重设备数。",
        "conversion_metrics": [
            "conv_from_prev：当前步骤 step_cnt / 上一步 step_cnt，prev_cnt=0 时输出 0。",
            f"conv_from_first：当前步骤 step_cnt / 首步 {first_step} step_cnt，first_cnt=0 时输出 0。",
        ],
        "steps": steps,
        "how_to_review": [
            "先逐行确认漏斗步骤顺序是否就是业务期望的登录链路。",
            "再确认每个步骤的来源表是否对应正确事件，且每表都按 DeviceId 取首次上报时间。",
            "然后检查严格顺序条件：后一步首次时间必须不早于前一步，断链后不能继续计入后续步骤。",
            "最后用结果样例检查 step_cnt 应该随步骤不增，两个转化率应能解释为 0 到 1 之间的比例。",
        ],
        "pass_criteria": [
            "步骤顺序、来源表、去重粒度、时间窗和分区窗都被确认。",
            "每步人数是严格到达该步的设备数，不是单表独立去重人数。",
            "相邻转化率和首步转化率的分子分母含义明确，除零保护符合预期。",
        ],
    }


def extract_generic_bucket_definitions(sql: str) -> dict[str, list[dict[str, str]]]:
    definitions: dict[str, list[dict[str, str]]] = defaultdict(list)
    text = strip_sql_comments(sql)
    pattern = re.compile(
        r"case\s+(?P<body>.*?)\s+end\s+(?:as\s+)?`?(?P<alias>[a-zA-Z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)`?",
        flags=re.I | re.S,
    )
    when_pattern = re.compile(
        r"when\s+(?P<condition>.*?)\s+then\s+(?P<value>'[^']+'|\"[^\"]+\"|[a-zA-Z_][\w]*)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(text):
        alias = match.group("alias")
        if not alias or "bucket" not in normalize_identifier(alias) and "分桶" not in alias:
            continue
        for when_match in when_pattern.finditer(match.group("body")):
            bucket_value = when_match.group("value").strip("'\"")
            condition = compact(when_match.group("condition"))
            definitions[alias].append(
                {
                    "bucket": bucket_value,
                    "condition": condition,
                    "business_effect": bucket_condition_business(f"bucket_code = '{bucket_value}'", metric_business_context(sql))
                    if bucket_value.upper().startswith("H")
                    else condition_to_business(condition),
                    "how_to_judge": "确认该分桶的边界、单位和是否包含边界；分桶之间不能重叠，最好能覆盖完整业务范围。",
                    "pass_criteria": "分桶名称、SQL 条件、结果展示顺序和业务解释一致；边界值能被明确归入某一桶。",
                }
            )
    return dict(definitions)


def distribution_review(review: FileReview, context: MetricBusinessContext) -> dict:
    generic_buckets = extract_generic_bucket_definitions(review.sql)
    bucket_fields = [
        field
        for field in review.dimensions + review.final_fields
        if "bucket" in normalize_identifier(field) or "分桶" in field or normalize_identifier(field).endswith(("level", "range"))
    ]
    bucket_cards: list[dict] = []
    for field in unique_in_order(bucket_fields):
        definitions = generic_buckets.get(field, [])
        if not definitions and context.bucket_definitions and normalize_identifier(field) in {"bucket_code", "duration_bucket"}:
            definitions = [
                {
                    "bucket": bucket,
                    "condition": description,
                    "business_effect": description,
                    "how_to_judge": "确认该时长分桶的单位、边界和累计时长算法是否符合业务口径。",
                    "pass_criteria": "分桶边界清楚且互斥，结果样例中的桶顺序和标签能被业务方理解。",
                }
                for bucket, description in context.bucket_definitions.items()
            ]
        bucket_cards.append(
            {
                "field": field,
                "definitions": definitions[:24],
                "how_to_review": "先确认分桶字段是不是业务要看的分布维度，再逐个核对分桶边界、单位、排序和覆盖范围。",
                "pass_criteria": "分桶互斥、边界明确、名称能解释结果；每个指标都能说明是在每个桶内统计什么 Base。",
            }
        )
    return {
        "detected": bool(bucket_cards),
        "type": "distribution_or_bucket",
        "summary": "按分桶/区间维度观察指标分布。" if bucket_cards else "",
        "bucket_cards": bucket_cards,
        "how_to_review": [
            "先看分桶字段和分桶定义，而不是先看 SQL 表达式。",
            "确认每个桶的业务含义、上下界、单位和是否互斥。",
            "再看每个桶内的指标 Base、分子/分母和结果样例量级。",
        ],
        "pass_criteria": [
            "分桶定义完整且没有重叠或明显缺口。",
            "桶内指标口径和整体 Base 一致，除非 SQL 明确声明例外。",
            "输出字段足以让审核者知道每行属于哪个桶和哪个业务维度。",
        ],
    }


def detect_business_review_pattern(review: FileReview, metrics: list[dict], funnel: dict, distribution: dict) -> tuple[str, str]:
    text = " ".join(extract_comment_lines(review.sql) + review.final_fields + review.dimensions + review.metrics).lower()
    chinese_text = " ".join(extract_comment_lines(review.sql) + review.final_fields + review.dimensions + review.metrics)
    if funnel.get("detected"):
        return "funnel", "漏斗/步骤转化"
    if distribution.get("detected") or "分布" in chinese_text or "bucket" in text:
        return "distribution", "分布/分桶"
    if any(keyword in chinese_text for keyword in ["留存", "回流"]):
        return "retention", "留存/回流"
    if any(keyword in chinese_text for keyword in ["转化率", "转化"]) or any(item.get("calculation_type") == "ratio" for item in metrics):
        return "conversion_or_rate", "转化/比率"
    if review.dimensions and metrics:
        return "breakdown", "分组汇总"
    if review.analysis_type in {"detail_check", "sample"}:
        return "detail", "明细/样例"
    return "generic_metric", "通用指标"


def pattern_review_cards(
    pattern_id: str,
    review: FileReview,
    metrics: list[dict],
    context: MetricBusinessContext,
    funnel: dict,
    distribution: dict,
) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    if pattern_id == "funnel":
        cards.extend(
            [
                {
                    "name": "漏斗步骤顺序",
                    "what_to_check": funnel.get("summary") or "漏斗步骤未完整抽出，需要人工看 SQL 注释或步骤映射 CTE。",
                    "how_to_judge": "逐步确认步骤是否符合真实业务路径；不要只看最终 step_cnt 和转化率。",
                    "pass_criteria": "每一步名称、顺序、来源事件和是否允许跳步都被确认。",
                },
                {
                    "name": "到达规则",
                    "what_to_check": funnel.get("strict_order_rule") or "检查每一步是否要求前序步骤已到达。",
                    "how_to_judge": "看 SQL 是否用首次时间和累积 reach 标记保证严格顺序；断链后不应继续计入后续步骤。",
                    "pass_criteria": "本步人数不能超过上一严格到达步骤；转化率分子分母清楚。",
                },
            ]
        )
    elif pattern_id == "distribution":
        cards.extend(
            [
                {
                    "name": "分桶/区间定义",
                    "what_to_check": "；".join(card.get("field", "") for card in distribution.get("bucket_cards", [])) or "未识别分桶字段。",
                    "how_to_judge": "先核对桶的业务含义、单位、上下界、是否互斥，再看每桶指标。",
                    "pass_criteria": "每个结果行能明确归属到一个桶；边界值不会落入多个桶或漏掉。",
                },
                {
                    "name": "桶内指标",
                    "what_to_check": "确认每个桶内统计的是人数、次数、时长、占比还是分位值。",
                    "how_to_judge": "看指标卡的 Base、分子、分母是否仍按桶内人群/记录计算。",
                    "pass_criteria": "桶定义和指标 Base 一致，没有把全量分母误用于桶内指标。",
                },
            ]
        )
    elif pattern_id == "retention":
        cards.extend(
            [
                {
                    "name": "Cohort / Base",
                    "what_to_check": "新增、常驻、回流等人群如何定义，以及每个 cohort 的起止时间。",
                    "how_to_judge": "先确认 Base 是本期新增、本期活跃、回流首日用户还是其他人群；再确认去重粒度。",
                    "pass_criteria": "Base 人群、互斥优先级、日期窗口和去重口径都能用中文复述。",
                },
                {
                    "name": "回访/留存窗口",
                    "what_to_check": "分子是否是在后续窗口仍活跃/回访/回流的玩家。",
                    "how_to_judge": "核对日期偏移、历史回看窗口、沉默阈值和是否多次回流只取首次。",
                    "pass_criteria": "分子与分母属于同一 cohort，窗口偏移明确，前置历史不足的风险被说明。",
                },
            ]
        )
    elif pattern_id == "conversion_or_rate":
        cards.append(
            {
                "name": "分子/分母一致性",
                "what_to_check": "所有比率、转化率、占比的分子和分母。",
                "how_to_judge": "逐个确认分子分母是否同一时间窗、同一 Base、同一去重粒度；除零保护是否符合展示预期。",
                "pass_criteria": "每个率都能说明“谁除以谁”，结果值范围能解释为比例或百分比。",
            }
        )
    elif pattern_id == "breakdown":
        cards.extend(
            [
                {
                    "name": "分组维度",
                    "what_to_check": grouping_summary(review),
                    "how_to_judge": "确认这些维度就是业务要拆看的视角；合计行、日期、模式、桶等维度不会混淆粒度。",
                    "pass_criteria": "每一行代表什么可以一句话说清楚，且不会重复计数。",
                },
                {
                    "name": "分组内 Base",
                    "what_to_check": common_metric_base(metrics),
                    "how_to_judge": "确认每个分组内指标都沿用同一 Base，除非 SQL 明确声明例外。",
                    "pass_criteria": "不同分组之间可比较，指标口径没有随分组暗中变化。",
                },
            ]
        )
    elif pattern_id == "detail":
        cards.append(
            {
                "name": "明细用途",
                "what_to_check": "输出字段是否足以定位业务问题；需要玩家标识时保留原值，禁止 SQL 侧哈希或掩码。",
                "how_to_judge": "先确认这是抽样/排查明细，不是可沉淀指标；再看 LIMIT、时间窗和筛选是否足够窄。",
                "pass_criteria": "明细字段能解释排查目的，样例范围可控，隐私处理留给 DA。",
            }
        )
    else:
        cards.append(
            {
                "name": "业务问题闭环",
                "what_to_check": "业务问题、Base、维度、指标、筛选是否能串成一句可审核的话。",
                "how_to_judge": "先让 SQL 作者补自然语言口径，再用结果样例看量级是否合理。",
                "pass_criteria": "审核人不看 SQL 也能判断每个指标是否回答了需求。",
            }
        )
    if context.duration_logic:
        cards.append(
            {
                "name": "时长算法",
                "what_to_check": context.duration_logic,
                "how_to_judge": "确认字段单位、累计/单次含义、是否先按人/天/战斗服聚合，以及 MAX、SUM、差分顺序是否符合业务口径。",
                "pass_criteria": "时长算法能解释结果量级；不会把累计字段逐行相加造成重复，也不会漏掉无记录用户的 0 值处理。",
            }
        )
    if review.business_filters:
        cards.append(
            {
                "name": "关键筛选 / ID 范围",
                "what_to_check": "；".join(item.get("business_effect", "") for item in review.business_filters[:8]),
                "how_to_judge": "确认 GameMode、iZoneAreaID、BattleSrvId、道具/任务 ID 等筛选是否属于业务定义，映射关系是否正确，未知 ID 不能猜。",
                "pass_criteria": "固定筛选、参数筛选和指标内 CASE 条件的作用层级清楚；ID 映射被确认或标注待确认。",
            }
        )
    return cards


def business_review_summary(review: FileReview, metrics: list[dict], context: MetricBusinessContext) -> dict:
    funnel = extract_funnel_review(review, metrics)
    distribution = distribution_review(review, context)
    pattern_id, pattern_label = detect_business_review_pattern(review, metrics, funnel, distribution)
    title = context.title or review.path.stem
    metric_names = [item.get("metric", "") for item in metrics]
    base = common_metric_base(metrics)
    dimensions = review.dimensions or []
    primary_objects = [
        {
            "name": "业务问题",
            "what_to_check": f"这份 SQL 是否真的在回答「{title}」这个问题。",
            "how_to_judge": "先用一句中文复述需求，再看输出维度和指标是否能回答这个问题。",
            "pass_criteria": "业务方能不看 SQL 也理解每行结果代表什么，以及每个指标怎么算。",
        },
        {
            "name": "Base",
            "what_to_check": base,
            "how_to_judge": "确认统计对象是谁、从哪一步/哪类事件开始、排除了谁、是否去重。",
            "pass_criteria": "Base 人群/记录范围、去重粒度、时间窗都明确。",
        },
        {
            "name": "维度/粒度",
            "what_to_check": "、".join(dimensions) if dimensions else "整体汇总，无显式维度。",
            "how_to_judge": "确认每一行结果的粒度，避免把步骤、桶、日期、模式等维度混在一起。",
            "pass_criteria": "输出粒度能被一句话说明，且不会造成重复计数或漏计。",
        },
        {
            "name": "指标",
            "what_to_check": "、".join(metric_names) if metric_names else "未识别最终指标。",
            "how_to_judge": "逐个看指标卡里的业务口径、分子、分母、计算和条件。",
            "pass_criteria": "每个指标都有可复述的业务含义；低置信度指标必须补口径。",
        },
    ]
    if review.business_filters:
        primary_objects.append(
            {
                "name": "关键筛选",
                "what_to_check": "；".join(item.get("business_effect", "") for item in review.business_filters[:6]),
                "how_to_judge": "确认这些筛选是 Base 级、指标级还是 JOIN 归因级；特别关注 GameMode、区服、道具/任务 ID。",
                "pass_criteria": "筛选作用层级正确，固定值/动态值/未知值都能被业务方解释。",
            }
        )
    pattern_guides = {
        "funnel": [
            "先审漏斗步骤顺序和来源表，再审严格顺序条件，最后审人数和转化率。",
            "不要只看最终三个指标名；漏斗每一步本身就是审核对象。",
        ],
        "distribution": [
            "先审分桶/区间定义，再审每个桶内统计的指标。",
            "重点看边界、单位、桶是否互斥，以及结果是否需要排序字段。",
        ],
        "retention": [
            "先审 cohort/Base，再审回访/回流窗口和分子定义。",
            "重点看日期偏移、是否排除当日新增、是否重复计数。",
        ],
        "conversion_or_rate": [
            "先审分子和分母是否属于同一时间窗、同一 Base、同一去重粒度。",
            "再审除零保护和结果值是否能解释为比例。",
        ],
        "breakdown": [
            "先审分组维度是否就是业务要拆看的视角。",
            "再审每个分组内指标是否沿用同一 Base。",
        ],
        "detail": [
            "先审明细字段是否能定位问题；不要在 SQL 里对玩家标识做 MD5/哈希/掩码。",
            "再审 LIMIT、时间窗、筛选条件是否足够窄。",
        ],
        "generic_metric": [
            "先审业务问题、Base、维度、指标四件事。",
            "静态推断弱的地方必须让 SQL 作者或业务方补口径。",
        ],
    }
    return {
        "pattern_id": pattern_id,
        "pattern_label": pattern_label,
        "business_question": f"{title}：{grouping_summary(review)}输出 {len(metrics)} 个指标。",
        "primary_review_objects": primary_objects,
        "pattern_cards": pattern_review_cards(pattern_id, review, metrics, context, funnel, distribution),
        "pattern_review_order": pattern_guides.get(pattern_id, pattern_guides["generic_metric"]),
        "funnel_review": funnel,
        "distribution_review": distribution,
        "duration_logic": context.duration_logic,
        "reviewer_takeaway": (
            "先判断业务分析形态，再审该形态的核心对象；SQL 表达式只作为证据，不是审核入口。"
        ),
    }


def metric_reviewer_question(metric: dict) -> str:
    if metric.get("needs_manual_confirmation") or metric.get("description_source") == "needs_manual_confirmation":
        return "请补充这个最终字段的业务角色：它是指标、维度、标签还是展示字段，并说明 Base、计算对象和分子/分母是否适用。"
    calc_type = metric.get("calculation_type")
    if calc_type == "ratio":
        return f"确认分子「{metric.get('numerator')}」和分母「{metric.get('denominator')}」是否就是业务要看的比率。"
    if calc_type == "unit_conversion":
        return "这是单位换算，不是分子/分母口径；确认原始字段单位和换算常数是否正确。"
    if calc_type in {"row_count", "count_distinct", "conditional_sum"}:
        return f"确认 Base「{metric.get('base_population')}」是否就是这个数量指标的统计范围。"
    return "确认该指标解释是否符合业务口径。"


def metric_how_to_review(metric: dict) -> str:
    filter_hint = ""
    if metric.get("base_business_filters") or metric.get("metric_business_filters") or metric.get("metric_condition_cards"):
        filter_hint = " 同时核对 Base 级筛选和指标内条件是否被放在正确层级。"
    calc_type = metric.get("calculation_type")
    if calc_type == "ratio":
        return (
            "先确认这个比率的业务问题，再分别核对分子、分母是否来自同一 Base、同一时间窗、同一去重粒度；"
            "最后看 SQL 追溯中的分子/分母表达式是否支撑这两句话。"
            + filter_hint
        )
    if calc_type == "unit_conversion":
        return (
            "先确认原始字段的业务含义和单位，再确认除以常数只是单位换算；"
            "不要把换算常数当成业务分母。"
            + filter_hint
        )
    if calc_type in {"row_count", "count_distinct", "conditional_sum"}:
        return (
            "先确认 Base 人群/记录范围，再确认计数口径是按人去重、按行计数，还是满足条件后求和；"
            "有 CASE 条件时要逐条确认条件是否就是业务定义。"
            + filter_hint
        )
    if calc_type == "average":
        return "先确认参与平均的记录范围和字段单位，再确认 NULL/异常值是否会影响均值。" + filter_hint
    if calc_type == "percentile":
        return "先确认参与分位计算的 Base 和字段单位，再确认分位点是否符合业务要看的尾部/中位水平。" + filter_hint
    return "先把指标用一句中文业务话术说清楚，再对照 SQL 表达式、过滤条件和结果样例确认是否一致。" + filter_hint


def metric_pass_criteria(metric: dict) -> str:
    filter_suffix = ""
    if metric.get("metric_business_filters") or metric.get("metric_condition_cards"):
        filter_suffix = "；指标内筛选/CASE 条件的 ID 范围、映射和边界已确认"
    elif metric.get("base_business_filters"):
        filter_suffix = "；Base 级筛选的 ID 范围和映射已确认"
    if metric.get("needs_manual_confirmation") or metric.get("confidence") == "low":
        return "不能直接通过：需要业务方或 SQL 作者补充自然语言口径，至少说明 Base、分子/分母或字段单位。"
    calc_type = metric.get("calculation_type")
    if calc_type == "ratio":
        return "通过标准：分子、分母、时间窗、去重粒度都被确认一致；结果值范围合理，通常应能解释为 0-1 或百分比" + filter_suffix + "。"
    if calc_type == "unit_conversion":
        return "通过标准：原始字段单位和换算常数明确，换算后数值量级与结果样例相符" + filter_suffix + "。"
    if calc_type in {"row_count", "count_distinct", "conditional_sum"}:
        return "通过标准：Base 范围明确，计数粒度明确，结果样例不会出现明显重复计数或漏计" + filter_suffix + "。"
    if calc_type == "average":
        return "通过标准：平均对象、字段单位、异常值处理方式明确，样例值量级合理" + filter_suffix + "。"
    if calc_type == "percentile":
        return "通过标准：分位字段、分位点、Base 范围明确，样例值量级合理" + filter_suffix + "。"
    return "通过标准：业务解释、SQL 表达式和结果样例三者能互相解释，没有未确认的关键假设" + filter_suffix + "。"


def metric_review_summary(review: FileReview) -> dict:
    context = metric_business_context(review.sql)
    metrics = metric_logic_items(review)
    dimension_cards = metric_dimension_cards(review, context)
    calculation_path = extract_metric_calculation_path(review.sql)
    business_review = business_review_summary(review, metrics, context)
    title = context.title or review.path.stem
    if context.output_description:
        summary = f"{title}：{context.output_description}"
    elif metrics:
        summary = f"{title}：{grouping_summary(review)}输出 {len(metrics)} 个指标，核心 Base 为「{common_metric_base(metrics)}」。"
    else:
        summary = f"{title}：未从最终 SELECT 识别到指标，需人工查看输出字段。"
    cards: list[dict] = []
    for metric in metrics:
        cards.append(
            {
                "metric": metric["metric"],
                "business_definition": metric["business_definition"],
                "base": metric["base_population"],
                "numerator": metric["numerator"],
                "denominator": metric["denominator"],
                "calculation": metric["formula"],
                "source": metric["description_source"],
                "confidence": metric["confidence"],
                "source_steps": metric.get("source_steps", []),
                "how_to_review": metric_how_to_review(metric),
                "pass_criteria": metric_pass_criteria(metric),
                "reviewer_question": metric_reviewer_question(metric),
                "business_filters": metric["business_filters"],
                "base_business_filters": metric["base_business_filters"],
                "metric_business_filters": metric["metric_business_filters"],
                "join_business_filters": metric["join_business_filters"],
                "metric_conditions": metric["metric_condition_cards"],
                "related_saved_rule_checks": metric["related_saved_rule_checks"],
                "sql_trace": {
                    "formula_expression": metric["formula_expression"],
                    "numerator_expression": metric["numerator_expression"],
                    "denominator_expression": metric["denominator_expression"],
                    "base_expression": metric["base_expression"],
                },
            }
        )
    questions: list[str] = []
    for card in cards:
        if card["source"] == "needs_manual_confirmation" or card["confidence"] == "low":
            questions.append(f"`{card['metric']}` 需要产品口径确认：{card['reviewer_question']}")
    for check in review.rule_checks:
        if check.result in {"conflict", "proposed_conflict", "needs_manual_check"}:
            questions.append(humanize_rule_check(check))
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        questions.append("当前结果来自代理环境，目标项目验证前不能把指标视为已验证。")
    if review.result_file is None:
        questions.append("缺少结果文件，无法用实际列和值样例核对指标。")
    elif result_is_field_mismatch(review):
        questions.append("结果文件列与 SQL 输出不一致，先确认是否同一版 SQL 跑出的结果。")
    return {
        "summary": summary,
        "grouping": grouping_summary(review),
        "common_base": common_metric_base(metrics),
        "calculation_path": calculation_path,
        "business_review": business_review,
        "dimension_cards": dimension_cards,
        "metric_cards": cards,
        "review_questions": unique_in_order(questions)[:10],
    }


def render_metric_review_summary(review: FileReview) -> str:
    summary = metric_review_summary(review)
    lines = [
        "#### 指标逻辑审核",
        "",
        f"- 一句话说明: {summary['summary']}",
        f"- 分组方式: {summary['grouping']}",
        f"- 核心 Base: {summary['common_base']}",
        "",
        render_business_review_markdown(summary.get("business_review", {})),
        "",
        "##### 计算路径",
        "",
    ]
    if summary["calculation_path"]:
        lines.extend(f"{index}. {step}" for index, step in enumerate(summary["calculation_path"], 1))
    else:
        lines.append("- 未识别到可读计算路径。")
    lines.extend(["", "##### 分组/维度字段", ""])
    if summary["dimension_cards"]:
        for card in summary["dimension_cards"]:
            lines.extend(
                [
                    f"- **{card['field']}**：{card['description']}",
                    f"  - 角色: {card['role']}",
                    f"  - 来源/置信度: `{card['source']}` / `{card['confidence']}`",
                ]
            )
    else:
        lines.append("- 未识别明确分组字段。")
    lines.extend(["", "##### 指标审核卡", ""])
    if summary["metric_cards"]:
        for card in summary["metric_cards"]:
            lines.extend(
                [
                    f"- **{card['metric']}**",
                    f"  - 业务口径: {card['business_definition']}",
                    f"  - Base: {card['base']}",
                    f"  - 分子: {card['numerator']}",
                    f"  - 分母: {card['denominator']}",
                    f"  - 计算: {card['calculation']}",
                    "  - 分子/分母来源步骤:",
                ]
            )
            for line in render_source_steps_markdown(card.get("source_steps", [])).splitlines():
                lines.append(f"    {line}")
            lines.extend(
                [
                    f"  - Base 级筛选: {business_filter_effect_list(card.get('base_business_filters', []), '未识别 Base 级核心业务筛选')}",
                    f"  - 指标内筛选/ID范围: {business_filter_effect_list(card.get('metric_business_filters', []), '未识别指标内核心 ID 筛选')}",
                    f"  - 指标条件: {metric_condition_effect_list(card.get('metric_conditions', []), '未识别 CASE/IF 指标条件')}",
                    f"  - 关联/归因条件: {business_filter_effect_list(card.get('join_business_filters', []), '未识别关联/归因业务条件')}",
                    f"  - 怎么看: {card['how_to_review']}",
                    f"  - 通过标准: {card['pass_criteria']}",
                    f"  - 审核问题: {card['reviewer_question']}",
                    f"  - 来源/置信度: `{card['source']}` / `{card['confidence']}`",
                ]
            )
            if card["related_saved_rule_checks"]:
                lines.append("  - 相关保存口径:")
                for check in card["related_saved_rule_checks"]:
                    hydrated_check = rule_check_from_payload(check)
                    lines.extend(
                        [
                            f"    - {check.get('title') or check.get('rule_id')}: {check.get('rule_summary') or check.get('message') or ''}",
                            f"      - 怎么判断: {rule_check_judgement_method(hydrated_check)}",
                        ]
                    )
    else:
        lines.append("- 未识别到指标。")
    lines.extend(["", "##### 需要人工确认", ""])
    if summary["review_questions"]:
        lines.extend(f"- {item}" for item in summary["review_questions"])
    else:
        lines.append("- 暂无额外确认项；仍建议抽看结果样例是否符合业务预期。")
    return "\n".join(lines)


def render_judgement_guide_markdown(review: FileReview, roles: ReviewRoleContext) -> str:
    guide = review_judgement_guide(review, roles)
    lines = [
        "#### 怎么审核这份 SQL",
        "",
        f"- 审核目标: {guide['goal']}",
        f"- 判断顺序: {guide['decision_order']}",
    ]
    if guide["current_blockers"]:
        lines.append("- 当前先处理: " + "；".join(guide["current_blockers"]))
    if guide["current_confirmations"]:
        lines.append("- 当前需确认: " + "；".join(guide["current_confirmations"]))
    if guide["checked_rule_topics"]:
        lines.append("- 涉及保存口径: " + "；".join(guide["checked_rule_topics"]))
    lines.extend(["", "| 审核项 | 看什么 | 怎么判断 | 通过标准 |", "|---|---|---|---|"])
    for item in guide["checks"]:
        lines.append(
            f"| {markdown_cell(item['name'])} | {markdown_cell(item['look_at'])} | "
            f"{markdown_cell(item['how_to_judge'])} | {markdown_cell(item['pass_criteria'])} |"
        )
    return "\n".join(lines)


def render_business_filters_markdown(filters: list[dict]) -> str:
    if not filters:
        return "- 未识别到 GameMode/iZoneAreaID/BattleSrvId/道具ID 等核心业务筛选。"
    lines = [
        "| 作用范围 | 筛选 | 业务影响 | 映射/未知值 | 怎么判断 | 通过标准 | SQL 条件 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in filters:
        mapping_parts = []
        for row in item.get("mapping", []):
            mapping_parts.append(f"{row.get('value')}={row.get('name')}/{row.get('category')}")
        unknown = item.get("unknown_values", [])
        if unknown:
            mapping_parts.append("未知：" + "，".join(unknown))
        dynamic = item.get("dynamic_values", [])
        if dynamic:
            mapping_parts.append("动态值：" + "，".join(dynamic))
        mapping_text = "；".join(mapping_parts) or "无映射"
        lines.append(
            f"| {markdown_cell(item.get('scope_label') or item.get('scope') or '')} | "
            f"{markdown_cell(item.get('label') or item.get('field'))} | "
            f"{markdown_cell(item.get('business_effect', ''))} | "
            f"{markdown_cell(mapping_text)} | "
            f"{markdown_cell(item.get('how_to_judge', ''))} | "
            f"{markdown_cell(item.get('pass_criteria', ''))} | "
            f"`{markdown_cell(item.get('condition', ''))}` |"
        )
    return "\n".join(lines)


def render_source_steps_markdown(source_steps: list[dict]) -> str:
    if not source_steps:
        return "- 未识别明确来源步骤。"
    lines = [
        "| 角色 | 来源步骤 | 来源日志/表 | 聚合粒度 | 人话解释 |",
        "|---|---|---|---|---|",
    ]
    role_labels = {
        "numerator": "分子",
        "denominator": "分母",
        "value": "换算值",
        "metric_value": "指标值",
        "unit_conversion_constant": "换算常数",
        "operand": "操作数",
    }
    for step in source_steps:
        role = role_labels.get(step.get("role", ""), step.get("role", ""))
        source_tables = "、".join(step.get("source_tables", [])) or "无"
        group_by = "、".join(step.get("group_by", [])) or "未识别"
        lines.append(
            f"| {markdown_cell(role)} | `{markdown_cell(step.get('source_step', '') or '-')}` | "
            f"{markdown_cell(source_tables)} | {markdown_cell(group_by)} | {markdown_cell(step.get('story', ''))} |"
        )
    return "\n".join(lines)


def business_filter_effect_list(filters: list[dict], empty: str) -> str:
    effects = [
        item.get("business_effect", "")
        for item in filters
        if item.get("business_effect")
    ]
    return "；".join(effects) or empty


def metric_condition_effect_list(conditions: list[dict], empty: str) -> str:
    effects = [
        item.get("business_effect", "")
        for item in conditions
        if item.get("business_effect")
    ]
    return "；".join(effects) or empty


def render_funnel_review_markdown(funnel: dict) -> str:
    if not funnel.get("detected"):
        return ""
    lines = [
        "##### 漏斗步骤审核",
        "",
        f"- 漏斗类型: {funnel.get('type', '')}",
        f"- 摘要: {funnel.get('summary', '')}",
        f"- Base: {funnel.get('base', '')}",
        f"- 去重/首次到达: {funnel.get('dedup_grain', '')}",
        f"- 时间窗: {funnel.get('time_window') or '未识别'}",
        f"- 分区窗: {funnel.get('partition_window') or '未识别'}",
        f"- 严格顺序: {funnel.get('strict_order_rule', '')}",
        f"- 人数指标: {funnel.get('step_count_metric', '')}",
        "- 转化指标:",
    ]
    lines.extend(f"  - {item}" for item in funnel.get("conversion_metrics", []))
    lines.extend(
        [
            "",
            "| 步骤 | 事件/步骤名 | 来源表 | 首次时间字段 | 到达条件 | 怎么审 | 通过标准 |",
            "|---:|---|---|---|---|---|---|",
        ]
    )
    for step in funnel.get("steps", []):
        lines.append(
            f"| {step.get('order')} | {markdown_cell(step.get('step_name', ''))} | "
            f"`{markdown_cell(step.get('source_table', '') or 'unknown')}` | "
            f"`{markdown_cell(step.get('first_time_alias', ''))}` | "
            f"{markdown_cell(step.get('reach_rule', ''))} | "
            f"{markdown_cell(step.get('how_to_judge', ''))} | "
            f"{markdown_cell(step.get('pass_criteria', ''))} |"
        )
    if funnel.get("how_to_review"):
        lines.extend(["", "- 怎么审核漏斗:"])
        lines.extend(f"  - {item}" for item in funnel.get("how_to_review", []))
    return "\n".join(lines)


def render_distribution_review_markdown(distribution: dict) -> str:
    if not distribution.get("detected"):
        return ""
    lines = [
        "##### 分布/分桶审核",
        "",
        f"- 摘要: {distribution.get('summary', '')}",
        "",
    ]
    for card in distribution.get("bucket_cards", []):
        lines.extend(
            [
                f"- 分桶字段: `{card.get('field', '')}`",
                f"  - 怎么审: {card.get('how_to_review', '')}",
                f"  - 通过标准: {card.get('pass_criteria', '')}",
            ]
        )
        definitions = card.get("definitions", [])
        if definitions:
            lines.extend(
                [
                    "  - 分桶定义:",
                    "    | 桶 | 含义/条件 | 怎么审 | 通过标准 |",
                    "    |---|---|---|---|",
                ]
            )
            for item in definitions[:24]:
                lines.append(
                    "    "
                    f"| {markdown_cell(item.get('bucket', ''))} | "
                    f"{markdown_cell(item.get('business_effect') or item.get('condition', ''))} | "
                    f"{markdown_cell(item.get('how_to_judge', ''))} | "
                    f"{markdown_cell(item.get('pass_criteria', ''))} |"
                )
    return "\n".join(lines)


def render_business_review_markdown(review_payload: dict) -> str:
    if not review_payload:
        return ""
    lines = [
        "##### 业务逻辑审核入口",
        "",
        f"- 分析形态: **{review_payload.get('pattern_label', '未知')}** (`{review_payload.get('pattern_id', '')}`)",
        f"- 业务问题: {review_payload.get('business_question', '')}",
        f"- 审核提醒: {review_payload.get('reviewer_takeaway', '')}",
        "",
        "| 审核对象 | 看什么 | 怎么判断 | 通过标准 |",
        "|---|---|---|---|",
    ]
    for item in review_payload.get("primary_review_objects", []):
        lines.append(
            f"| {markdown_cell(item.get('name', ''))} | {markdown_cell(item.get('what_to_check', ''))} | "
            f"{markdown_cell(item.get('how_to_judge', ''))} | {markdown_cell(item.get('pass_criteria', ''))} |"
        )
    if review_payload.get("pattern_cards"):
        lines.extend(
            [
                "",
                "##### 当前分析形态重点卡",
                "",
                "| 重点 | 看什么 | 怎么判断 | 通过标准 |",
                "|---|---|---|---|",
            ]
        )
        for item in review_payload.get("pattern_cards", []):
            lines.append(
                f"| {markdown_cell(item.get('name', ''))} | {markdown_cell(item.get('what_to_check', ''))} | "
                f"{markdown_cell(item.get('how_to_judge', ''))} | {markdown_cell(item.get('pass_criteria', ''))} |"
            )
    if review_payload.get("pattern_review_order"):
        lines.extend(["", "- 当前形态审核顺序:"])
        lines.extend(f"  - {item}" for item in review_payload.get("pattern_review_order", []))
    if review_payload.get("duration_logic"):
        lines.extend(["", f"- 时长算法: {review_payload.get('duration_logic')}"])
    funnel_text = render_funnel_review_markdown(review_payload.get("funnel_review", {}))
    if funnel_text:
        lines.extend(["", funnel_text])
    distribution_text = render_distribution_review_markdown(review_payload.get("distribution_review", {}))
    if distribution_text:
        lines.extend(["", distribution_text])
    return "\n".join(lines)


def extract_parameters(sql: str) -> list[str]:
    return unique_in_order(re.findall(r"\$\{([a-zA-Z_][\w]*)\}", sql))


def extract_constant_aliases(sql: str) -> dict[str, str]:
    cleaned = strip_sql_comments(sql)
    constants: dict[str, str] = {}
    cte_pattern = re.compile(
        r"\b(?:params?|parameters?|config|cfg)\s+as\s*\((?P<body>.*?)\)\s*(?:,|\bselect\b)",
        flags=re.I | re.S,
    )
    for cte_match in cte_pattern.finditer(cleaned):
        body = cte_match.group("body")
        for match in re.finditer(
            r"(?P<value>'[^']*'|\"[^\"]*\"|\d+(?:\.\d+)?)\s+(?:as\s+)?`?(?P<alias>[a-zA-Z_][\w]*)`?",
            body,
            flags=re.I,
        ):
            constants[match.group("alias").lower()] = match.group("value")
    return constants


def expand_constant_condition(condition: str, constants: dict[str, str]) -> list[str]:
    expanded_condition = condition
    changed = False
    matches = list(re.finditer(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)\b", condition))
    parameter_aliases = {"p", "param", "params", "parameter", "parameters", "cfg", "config"}
    for match in reversed(matches):
        qualifier = match.group(1).lower()
        alias = match.group(2).lower()
        if qualifier not in parameter_aliases:
            continue
        if alias not in constants:
            continue
        value = constants[alias]
        expanded_condition = expanded_condition[: match.start()] + value + expanded_condition[match.end() :]
        changed = True
    if changed and expanded_condition != condition:
        return [compact(expanded_condition)]
    return []


def expand_business_conditions(conditions: list[str], constants: dict[str, str]) -> list[str]:
    expanded: list[str] = []
    for condition in conditions:
        replacements = expand_constant_condition(condition, constants)
        expanded.extend(replacements or [condition])
    return unique_in_order(expanded)


def count_ctes(sql: str) -> int:
    cleaned = strip_sql_comments(sql)
    return len(re.findall(r"\b[a-zA-Z_][\w]*\s+as\s*\(", cleaned, flags=re.I))


def has_tlog_table(tables: list[str]) -> bool:
    return any("_dsl_" in table.lower() or "tdbank" in table.lower() for table in tables)


def partition_findings(sql: str, tables: list[str], project_config: dict | None = None) -> list[QualityFinding]:
    if not has_tlog_table(tables):
        return []
    text = strip_sql_comments(sql).lower()
    partition_policy = (project_config or {}).get("partition_policy") if isinstance(project_config, dict) else {}
    partition_policy = partition_policy if isinstance(partition_policy, dict) else {}
    partition_required = partition_policy.get("required_for_tlog") is True
    partition_field = str(partition_policy.get("partition_field") or "").strip().lower()
    findings: list[QualityFinding] = []
    if partition_required:
        if not partition_field:
            findings.append(QualityFinding("BLOCKER", "TLOG/TDW 项目配置要求分区过滤，但未配置 partition_field。"))
        else:
            has_partition = partition_field in text
            escaped = re.escape(partition_field)
            has_lower = bool(re.search(rf"\b{escaped}\b\s*(?:>=|between\b)", text))
            has_upper = bool(re.search(rf"\b{escaped}\b\s*(?:<=|between\b)", text))
            if not has_partition:
                findings.append(QualityFinding("BLOCKER", f"TLOG/TDW 表缺少 `{partition_field}` 分区过滤。"))
            elif not (has_lower and has_upper):
                findings.append(QualityFinding("WARN", f"`{partition_field}` 分区过滤不完整，建议同时有上下界。"))
            if re.search(rf"\b(substr|from_unixtime|date_format)\s*\(\s*{escaped}\b", text):
                findings.append(QualityFinding("WARN", f"`{partition_field}` 被函数包裹，可能影响分区裁剪。"))
    unsafe_aliases = sorted(
        {
            match.group(1).strip("`")
            for match in re.finditer(
                r"\bas\s+(`?(?:start_partition|end_partition|partition|end)`?)\b",
                text,
                flags=re.I,
            )
        }
    )
    if unsafe_aliases:
        findings.append(
            QualityFinding(
                "BLOCKER",
                "Hive 可执行 SQL 使用了解析器敏感 params 别名："
                + ", ".join(unsafe_aliases)
                + "；请改用 `pt_start`、`pt_end`、`ts_start`、`ts_end`。",
            )
        )
    return findings


def predicate_syntax_findings(sql: str) -> list[QualityFinding]:
    cleaned = strip_sql_comments(sql)
    text = re.sub(r"\s+", " ", cleaned).strip()
    findings: list[QualityFinding] = []

    comparison_pattern = re.compile(
        r"(?P<field>(?:`[^`]+`|[a-zA-Z_][\w]*)(?:\.(?:`[^`]+`|[a-zA-Z_][\w]*))?)"
        r"\s*(?P<operator>=|<>|!=|>=|<=|>|<)\s*"
        r"(?=(?:\)|,|;|\b(?:and|or|group|order|having|limit|union|where|join|on)\b|$))",
        flags=re.I,
    )
    for match in comparison_pattern.finditer(text):
        snippet = compact(text[max(0, match.start() - 40) : min(len(text), match.end() + 40)])
        findings.append(
            QualityFinding(
                "BLOCKER",
                f"疑似不完整 SQL 条件：`{match.group('field')} {match.group('operator')}` 缺少右侧取值。context: {snippet}",
            )
        )

    empty_in_pattern = re.compile(
        r"(?P<field>(?:`[^`]+`|[a-zA-Z_][\w]*)(?:\.(?:`[^`]+`|[a-zA-Z_][\w]*))?)\s+"
        r"(?P<operator>not\s+in|in)\s*\(\s*\)",
        flags=re.I,
    )
    for match in empty_in_pattern.finditer(text):
        snippet = compact(text[max(0, match.start() - 40) : min(len(text), match.end() + 40)])
        findings.append(
            QualityFinding(
                "BLOCKER",
                f"疑似不完整 SQL 条件：`{match.group('field')} {compact(match.group('operator')).upper()} ()` 为空列表。context: {snippet}",
            )
        )

    trailing_operator_pattern = re.compile(
        r"\b(?P<operator>between|like|regexp|rlike)\s*"
        r"(?=(?:\)|,|;|\b(?:and|or|group|order|having|limit|union|where|join|on)\b|$))",
        flags=re.I,
    )
    for match in trailing_operator_pattern.finditer(text):
        snippet = compact(text[max(0, match.start() - 40) : min(len(text), match.end() + 40)])
        findings.append(
            QualityFinding(
                "BLOCKER",
                f"疑似不完整 SQL 条件：`{match.group('operator').upper()}` 后缺少取值。context: {snippet}",
            )
        )

    seen: set[str] = set()
    unique_findings: list[QualityFinding] = []
    for finding in findings:
        if finding.message in seen:
            continue
        seen.add(finding.message)
        unique_findings.append(finding)
    return unique_findings


def performance_findings(review: FileReview, project_config: dict | None = None) -> list[QualityFinding]:
    result = analyze_performance(
        sql=review.sql,
        project_config=project_config or {},
        mode="review",
        artifact_kind="QUERY",
        sql_facts=review.sql_facts,
    )
    review.performance_preflight = result
    findings: list[QualityFinding] = []
    for blocker in result.get("blockers", []):
        findings.append(QualityFinding("WARN", f"performance_preflight: {blocker}"))
    tier = str(result.get("tier") or "")
    if tier in {"L2_perf_deep", "L3_perf_blocking"} and not result.get("blockers"):
        top_triggers = result.get("triggers", [])[:5]
        findings.append(
            QualityFinding(
                "WARN",
                "performance_preflight: "
                f"{tier}; score={result.get('score', 0)}; "
                f"triggers={'; '.join(top_triggers) if top_triggers else 'none'}",
            )
        )
    elif tier == "L1_perf_standard":
        for detail in result.get("trigger_details", []):
            if detail.get("code") in {"detail_without_limit", "count_distinct", "window_function", "repeated_large_table_scan"}:
                findings.append(QualityFinding("WARN", f"performance_preflight: {detail.get('message', '')}"))
    return findings


def quality_findings(review: FileReview, project_config: dict | None = None) -> list[QualityFinding]:
    sql = review.sql
    text = strip_sql_comments(sql).lower()
    final_segment = final_select_segment(sql)
    findings: list[QualityFinding] = []
    findings.extend(performance_findings(review, project_config))
    findings.extend(predicate_syntax_findings(sql))
    if not final_segment:
        findings.append(QualityFinding("BLOCKER", "无法识别最终 SELECT 输出字段。"))
    privacy_transforms = sql_side_privacy_transforms(sql)
    if privacy_transforms:
        functions = ", ".join(sorted({item["function"] for item in privacy_transforms}))
        findings.append(
            QualityFinding(
                "BLOCKER",
                f"SQL 侧脱敏被禁止：检测到 {functions}；业务需要的标识保持原值，由 DA 侧处理隐私。",
            )
        )
    return findings


def grade_from_findings(review: FileReview) -> str:
    if any(item.severity == "BLOCKER" for item in review.findings):
        return "D"
    warn_count = sum(1 for item in review.findings if item.severity == "WARN")
    if warn_count >= 4:
        return "C"
    if warn_count or review.join_count >= 2 or review.cte_count >= 5:
        return "B"
    return "A"


def apply_project_rule_checks(
    review: FileReview,
    canonical_rules: list[CanonicalRule],
    project_root: Path | None = None,
) -> None:
    review.business_filters = extract_business_filters_for_review(review, project_root)
    review.rule_checks = check_project_rules(review, canonical_rules)
    for check in review.rule_checks:
        if check.result == "conflict":
            review.findings.append(
                QualityFinding(
                    "BLOCKER",
                    f"SQL inferred口径 conflicts with confirmed project rule `{check.rule_id}`: {check.message}",
                )
            )
        elif check.result == "proposed_conflict":
            review.findings.append(
                QualityFinding(
                    "WARN",
                    f"SQL inferred口径 conflicts with proposed project rule `{check.rule_id}`: {check.message}",
                )
            )
    review.grade = grade_from_findings(review)


def normalize_column_name(value: str) -> str:
    return value.strip().strip("`").lower()


def compare_result_columns(review: FileReview, result: ResultFileReview) -> None:
    if result.status != "loaded":
        review.findings.append(QualityFinding("WARN", f"结果文件 `{result.path.name}` 读取状态为 `{result.status}`：{result.note or 'no detail'}"))
        return
    if not result.columns:
        review.findings.append(QualityFinding("WARN", f"结果文件 `{result.path.name}` 未识别到列名。"))
        return
    expected = [normalize_column_name(field) for field in review.final_fields]
    actual = [normalize_column_name(column) for column in result.columns]
    if not expected:
        review.findings.append(QualityFinding("WARN", "无法识别 SQL 最终输出字段，无法和结果文件列名对比。"))
        return
    result.missing_columns = [field for field, key in zip(review.final_fields, expected) if key not in actual]
    result.extra_columns = [column for column, key in zip(result.columns, actual) if key not in expected]
    comparable_actual = [key for key in actual if key in expected]
    result.order_mismatch = not result.missing_columns and not result.extra_columns and expected != comparable_actual
    if result.missing_columns:
        review.findings.append(
            QualityFinding("WARN", f"结果文件缺少 SQL 输出字段：{', '.join(result.missing_columns)}。")
        )
    if result.extra_columns:
        review.findings.append(
            QualityFinding("WARN", f"结果文件包含 SQL 未声明输出字段：{', '.join(result.extra_columns)}。")
        )
    if result.order_mismatch:
        review.findings.append(QualityFinding("WARN", "结果文件列顺序与 SQL 最终 SELECT 字段顺序不一致。"))


def attach_result_file(
    review: FileReview,
    candidates: list[Path],
    sample_limit: int,
    pairing_method: str = "exact_stem",
) -> None:
    review.result_pairing_method = pairing_method if candidates else "missing"
    if not candidates:
        review.result_file = None
        review.findings.append(QualityFinding("WARN", "missing_result_file: 未找到同目录同名的 .xlsx/.csv/.txt 查询结果文件。"))
        review.grade = grade_from_findings(review)
        return
    ordered = sorted(candidates, key=lambda path: (RESULT_PRIORITY.get(path.suffix.lower(), 99), path.name.lower()))
    result = read_result_file(ordered[0], sample_limit)
    result.alternatives = ordered[1:]
    if result.alternatives:
        review.findings.append(
            QualityFinding(
                "WARN",
                "同名结果文件存在多个，已按优先级选择 "
                f"`{result.path.name}`；其他文件：{', '.join(path.name for path in result.alternatives)}。",
            )
        )
    compare_result_columns(review, result)
    review.result_file = result
    review.grade = grade_from_findings(review)


def result_is_field_mismatch(review: FileReview) -> bool:
    result = review.result_file
    return bool(result and (result.missing_columns or result.extra_columns or result.order_mismatch))


def concept_rules_by_key(project: ProjectContext | None) -> dict[str, list[CanonicalRule]]:
    grouped: dict[str, list[CanonicalRule]] = defaultdict(list)
    if not project:
        return grouped
    for rule in project.canonical_rules:
        if rule.status in {"confirmed", "proposed"} and rule.concept_key:
            grouped[rule.concept_key].append(rule)
    return grouped


def applied_definition_rules(review: FileReview, project: ProjectContext | None) -> list[CanonicalRule]:
    if not project:
        return []
    applied_keys = {
        check.concept_key
        for check in review.rule_checks
        if check.concept_key and check.result in {"matched", "conflict"}
    }
    return [
        rule
        for rule in project.canonical_rules
        if rule.status in {"confirmed", "proposed"} and rule.concept_key in applied_keys
    ]


def rules_equivalent(left: CanonicalRule, right: CanonicalRule) -> bool:
    left_text = normalize_rule_text(" ".join([left.title, left.content, left.applies_to]))
    right_text = normalize_rule_text(" ".join([right.title, right.content, right.applies_to]))
    return left.status == right.status and left_text == right_text


def compare_proxy_concepts(review: FileReview, roles: ReviewRoleContext) -> None:
    definition = roles.definition
    execution = review.execution_project
    if not definition or not execution or definition.project_id == execution.project_id:
        return
    execution_rules = concept_rules_by_key(execution)
    definition_rules = applied_definition_rules(review, definition)
    checked: list[str] = []
    limitations: list[str] = []
    for rule in definition_rules:
        concept_key = rule.concept_key
        checked.append(concept_key)
        peer_rules = execution_rules.get(concept_key, [])
        if not peer_rules:
            limitations.append(
                f"定义项目 `{project_label(definition)}` 的口径「{rule.title or concept_key}」在执行项目 `{project_label(execution)}` 没有登记；"
                f"只能作为代理证据，不能当目标项目已验证。（内部 concept_key: `{concept_key}`）"
            )
            continue
        if not any(rules_equivalent(rule, peer) for peer in peer_rules):
            peer_status = ", ".join(f"{peer.title or peer.rule_id}:{peer.status}" for peer in peer_rules)
            limitations.append(
                f"定义项目 `{project_label(definition)}` 的口径「{rule.title or concept_key}」与执行项目 `{project_label(execution)}` "
                f"同概念口径记录不同（执行项目记录：{peer_status}）；需要人工确认差异是否影响本 SQL。"
                f"（内部 concept_key: `{concept_key}`）"
            )
    review.checked_concept_keys = unique_in_order(review.checked_concept_keys + checked)
    review.proxy_limitations = unique_in_order(review.proxy_limitations + limitations)


def compute_evidence_status(review: FileReview, roles: ReviewRoleContext) -> str:
    if review.result_file is None:
        return "missing_result_file"
    if review.result_file.status != "loaded":
        return f"result_{review.result_file.status}"
    if result_is_field_mismatch(review):
        return "field_mismatch"
    if review.execution_project is None:
        return "execution_project_unresolved"
    delivery = roles.delivery
    execution = review.execution_project
    if delivery and execution and delivery.project_id == execution.project_id:
        return "target_reviewed"
    return "proxy_reviewed_needs_target_verification"


def same_project(left: ProjectContext | None, right: ProjectContext | None) -> bool:
    return bool(left and right and left.project_id == right.project_id)


def infer_review_stage_for_review(review: FileReview, roles: ReviewRoleContext) -> str:
    if review.execution_project is None:
        return "execution_unresolved"
    if not (roles.definition or roles.delivery):
        return "pure_sql"
    if roles.delivery and review.execution_project and not same_project(roles.delivery, review.execution_project):
        return "proxy_execution"
    if roles.definition and review.execution_project and not same_project(roles.definition, review.execution_project):
        return "proxy_execution"
    if roles.delivery and not review.execution_project and any(
        not same_project(roles.delivery, project) for project in roles.execution_projects
    ):
        return "proxy_execution"
    if roles.definition and roles.delivery and not same_project(roles.definition, roles.delivery):
        return "proxy_execution"
    return "target_execution"


def compute_query_review_status(review: FileReview) -> str:
    if any(item.severity == "BLOCKER" for item in review.findings):
        return "needs_sql_fix"
    if review.evidence_status == "missing_result_file":
        return "needs_result_file"
    if review.evidence_status.startswith("result_"):
        return "result_read_issue"
    if review.evidence_status == "field_mismatch":
        return "needs_result_alignment"
    if review.evidence_status == "execution_project_unresolved":
        return "needs_execution_context"
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        if review.delivery_table_mismatches or review.proxy_limitations:
            return "proxy_evidence_with_conversion_requirements"
        return "proxy_evidence_needs_target_verification"
    if review.evidence_status == "target_reviewed":
        return "target_evidence_reviewed"
    return "reviewed_sql_only"


def compute_deployment_readiness(review: FileReview) -> str:
    if any(item.severity == "BLOCKER" for item in review.findings):
        return "blocked"
    if review.delivery_table_mismatches:
        return "needs_rewrite"
    if review.evidence_status in {"proxy_reviewed_needs_target_verification", "execution_project_unresolved"}:
        return "needs_target_verification"
    if review.evidence_status in {"missing_result_file", "field_mismatch"} or review.evidence_status.startswith("result_"):
        return "blocked"
    return "ready"


def performance_project_config(review: FileReview, roles: ReviewRoleContext) -> dict:
    for project in [review.execution_project, roles.definition, roles.delivery]:
        if project and project.config:
            return project.config
    return {}


def apply_role_analysis(review: FileReview, roles: ReviewRoleContext) -> None:
    inference = infer_execution_project(review, roles)
    review.execution_project = inference.project
    if review.execution_project and review.execution_project.root:
        review.execution_project.canonical_rules = select_project_rules_for_sql(
            review.execution_project,
            review.sql,
        )
    review.execution_inference_confidence = inference.confidence
    review.execution_inference_reason = inference.reason
    review.execution_inference_source = inference.source
    review.delivery_table_mismatches = table_profile_mismatches(review, roles.delivery, roles.known_projects)
    compare_proxy_concepts(review, roles)
    review.evidence_status = compute_evidence_status(review, roles)
    review.review_stage = infer_review_stage_for_review(review, roles)
    review.future_target_verification_plan = (
        f"在 `{project_label(roles.delivery)}` 目标环境使用目标项目表名/profile 重新执行 SQL，核对行数、关键指标、维度样例和结果列后，再升级为 target reviewed。"
        if roles.delivery
        else "补充目标部署项目后，在目标环境重新执行并核对结果。"
    )
    review.findings = quality_findings(review, performance_project_config(review, roles)) + review.findings
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        review.findings.append(
            QualityFinding(
                "WARN",
                "结果文件来自显式代理执行项目，只能支持代理证据审查，必须标记为 "
                "`proxy_reviewed_needs_target_verification`，不得视为目标项目已验证。",
            )
        )
    if review.delivery_table_mismatches:
        mismatch_note = (
            "SQL 使用的物理表不完全符合 delivery 项目表名 profile；这是未来转目标项目查询/看板或部署前的改写要求，"
            "不代表代理查询本身没有 review 价值。"
        )
        review.findings.append(
            QualityFinding(
                "WARN",
                mismatch_note,
            )
        )
    for limitation in review.proxy_limitations:
        review.findings.append(QualityFinding("WARN", f"proxy concept limitation: {limitation}"))
    review.query_review_status = compute_query_review_status(review)
    review.deployment_readiness = compute_deployment_readiness(review)
    review.grade = grade_from_findings(review)


def collect_rules(review: FileReview) -> list[RuleCandidate]:
    rules: list[RuleCandidate] = []
    constants = extract_constant_aliases(review.sql)
    for condition in extract_where_conditions(review.sql):
        rules.append(RuleCandidate("filter", condition, normalize_rule_text(condition)))
        for expanded in expand_constant_condition(condition, constants):
            rules.append(RuleCandidate("filter", expanded, normalize_rule_text(expanded)))
    for condition in extract_join_conditions(review.sql):
        rules.append(RuleCandidate("join", condition, normalize_rule_text(condition)))
        for expanded in expand_constant_condition(condition, constants):
            rules.append(RuleCandidate("join", expanded, normalize_rule_text(expanded)))
    for metric in extract_metric_rules(review.sql):
        rules.append(RuleCandidate("metric", metric, normalize_rule_text(metric)))
    if review.grain:
        text = f"output grain := {review.grain}"
        rules.append(RuleCandidate("grain", text, normalize_rule_text(text)))
    deduped: dict[str, RuleCandidate] = {}
    for rule in rules:
        deduped.setdefault(f"{rule.kind}:{rule.normalized}", rule)
    return list(deduped.values())


def review_sql_file(path: Path, project_root: Path | None = None) -> FileReview:
    sql = read_sql(path)
    sql_facts = build_sql_fact_bundle(sql, kind="QUERY", root=project_root)
    analysis = sql_facts["analysis"]
    tables = sql_facts["source_tables"]
    target_tables = sql_facts["target_tables"]
    metrics = list(analysis.get("metrics", []))
    dimensions = list(analysis.get("dimensions", []))
    business_category = str(analysis.get("business_category") or DEFAULT_BUSINESS_CATEGORY)
    analysis_type = str(analysis.get("analysis_type") or DEFAULT_ANALYSIS_TYPE)
    grain = str(analysis.get("grain") or "")
    time_grain = str(analysis.get("time_grain") or "")
    text = strip_sql_comments(sql).lower()
    performance_structure = sql_facts.get("performance") or {}
    review = FileReview(
        path=path,
        sql=sql,
        tables=tables,
        target_tables=target_tables,
        metrics=metrics,
        dimensions=dimensions,
        business_category=business_category,
        analysis_type=analysis_type,
        grain=grain,
        time_grain=time_grain,
        parameters=extract_parameters(sql),
        final_fields=sql_facts["final_fields"],
        cte_count=int(performance_structure.get("cte_count") or 0),
        join_count=int(performance_structure.get("join_count") or 0),
        has_count_distinct=bool(performance_structure.get("count_distinct_count")),
        has_window_function=bool(performance_structure.get("window_function_count")),
        has_global_order_by=bool(re.search(r"\border\s+by\b", text)),
        sql_facts=sql_facts,
    )
    review.rules = collect_rules(review)
    return review


def markdown_list(items: list[str]) -> str:
    if not items:
        return "- none"
    return "\n".join(f"- `{item}`" for item in items)


def markdown_findings(items: list[QualityFinding]) -> str:
    if not items:
        return "- No static quality findings."
    return "\n".join(f"- **{item.severity}**: {item.message}" for item in items)


def render_performance_preflight_markdown(result: dict) -> str:
    if not result:
        return "- performance_preflight: not available"
    lines = [
        f"- status: `{result.get('status', 'unknown')}`",
        f"- tier: `{result.get('tier', 'unknown')}`",
        f"- score: `{result.get('score', 0)}`",
        f"- full_guide_required: `{str(bool(result.get('full_guide_required'))).lower()}`",
        f"- required_references: `{', '.join(result.get('required_references', [])) or 'none'}`",
        f"- optimization_hint: {result.get('optimization_hint', '')}",
    ]
    triggers = result.get("triggers", [])
    lines.append("- triggers:")
    lines.extend(f"  - {item}" for item in triggers[:12]) if triggers else lines.append("  - none")
    blockers = result.get("blockers", [])
    lines.append("- blockers:")
    lines.extend(f"  - {item}" for item in blockers) if blockers else lines.append("  - none")
    return "\n".join(lines)


def markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown_sample(rows: list[dict[str, str]], limit: int = 5) -> str:
    if not rows:
        return "- no sample rows"
    columns = list(rows[0].keys())
    lines = [
        "| " + " | ".join(markdown_cell(column) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(markdown_cell(row.get(column, "")) for column in columns) + " |")
    return "\n".join(lines)


def result_status(review: FileReview) -> str:
    if review.result_file is None:
        return "missing_result_file"
    if review.result_file.status != "loaded":
        return f"result_{review.result_file.status}"
    if review.result_file.missing_columns or review.result_file.extra_columns or review.result_file.order_mismatch:
        return "field_mismatch"
    return "matched"


def issue_priority_value(priority: str) -> int:
    order = {"P0": 0, "P1": 1, "P2": 2, "INFO": 3}
    return order.get(priority, 9)


def add_review_issue(
    issues: list[dict],
    code: str,
    priority: str,
    title: str,
    detail: str,
    source: str,
) -> None:
    clean_detail = clip_text(detail, 320)
    key = (code, clean_detail)
    if any((item["code"], item["detail"]) == key for item in issues):
        return
    issues.append(
        {
            "code": code,
            "priority": priority,
            "title": title,
            "detail": clean_detail,
            "source": source,
        }
    )


def rule_check_human_topic(check: RuleCheck) -> str:
    if check.title:
        return check.title
    if check.rule_summary:
        return check.rule_summary.split("：", 1)[0]
    return check.rule_id


def rule_check_judgement_method(check: RuleCheck) -> str:
    text = " ".join([check.rule_id, check.concept_key, check.title, check.rule_summary]).lower()
    if "game-mode" in text or "gamemode" in text:
        return (
            "看 SQL 中 GameMode/gameModeID 的过滤、CASE 映射和输出名称；"
            "确认每个 ID 的名称/大类都来自已保存映射，未配置 ID 不能被猜成某个大类。"
        )
    if "izoneareaid" in text:
        return (
            "看 WHERE/JOIN 中是否出现 iZoneAreaID；如果出现，确认取值是否等于本项目默认值，"
            "或是否有用户明确说明本次例外。"
        )
    if "match-duration" in text or "match duration" in text or "匹配耗时" in text:
        return (
            "看匹配耗时字段是否包含客户端点击到服务端开始的等待时长，以及服务端 MatchDuration；"
            "确认没有只使用单段 MatchDuration。"
        )
    if "battlesrvid" in text:
        return (
            "看局内表缺少 GameMode 时是否通过 BattleSrvId 做模式归因；"
            "确认映射来源和目标模式名称/大类一致。"
        )
    return "对照该口径摘要，查看 SQL 的 WHERE、JOIN、CASE、最终 SELECT 是否显式实现；无法静态确认时要求 SQL 作者说明。"


def rule_check_pass_criteria(check: RuleCheck) -> str:
    if check.result == "matched":
        return "通过：知识库口径已被静态证据自动核对通过；人工只需抽看结果样例和边界值。"
    if check.result == "conflict":
        return "不能通过：必须改 SQL、改需求说明，或由用户明确确认本次允许违反已确认口径。"
    if check.result == "proposed_conflict":
        return "暂不通过：需要确认 proposed 口径是否采纳，或说明为什么本 SQL 不适用。"
    if check.result == "needs_manual_check":
        return "未自动通过：SQL 命中该口径主题，但静态证据不足以证明已覆盖；需要补 SQL 注释/输出字段或确认该口径不适用。"
    return "需要人工给出是否通过的判断。"


def rule_check_from_payload(value: dict) -> RuleCheck:
    return RuleCheck(
        rule_id=value.get("rule_id", ""),
        status=value.get("status", ""),
        result=value.get("result", ""),
        message=value.get("message", ""),
        evidence=value.get("evidence", ""),
        concept_key=value.get("concept_key", ""),
        title=value.get("title", ""),
        rule_summary=value.get("rule_summary", ""),
    )


def humanize_rule_check(check: RuleCheck) -> str:
    label = f"「{rule_check_human_topic(check)}」"
    summary = f" 口径含义：{check.rule_summary}" if check.rule_summary else ""
    if check.result == "conflict":
        return f"与已确认项目口径 {label} 冲突：{check.message}{summary}"
    if check.result == "proposed_conflict":
        return f"与 proposed 项目口径 {label} 不一致：{check.message}{summary}"
    if check.result == "needs_manual_check":
        return f"自动核对证据不足：SQL 命中项目口径 {label}，但未找到足够静态证据证明已覆盖。{summary}"
    return f"已自动核对匹配项目口径 {label}。{summary}"


def humanize_proxy_limitation(value: str) -> str:
    concept_match = re.search(r"`([^`]+)`", value)
    concept = concept_match.group(1) if concept_match else "相关口径"
    if "is missing in execution" in value:
        return f"代理执行项目没有登记 `{concept}`，只能把结果当代理证据，不能当目标项目已验证。"
    if "differs between definition" in value:
        return f"代理执行项目与定义项目的 `{concept}` 口径记录不同，需要人工确认差异是否影响本 SQL。"
    return value


def finding_to_review_issue(finding: QualityFinding) -> dict:
    message = finding.message
    priority = "P0" if finding.severity == "BLOCKER" else "P2"
    code = "technical_warning"
    title = "技术提醒"
    detail = message
    if "conflicts with confirmed project rule" in message:
        code = "confirmed_rule_conflict"
        title = "口径冲突"
        detail = re.sub(r"^SQL inferred口径 conflicts with confirmed project rule `([^`]+)`: ", r"与已确认项目口径 `\1` 冲突：", message)
        priority = "P0"
    elif "conflicts with proposed project rule" in message:
        code = "proposed_rule_conflict"
        title = "待确认口径差异"
        detail = re.sub(r"^SQL inferred口径 conflicts with proposed project rule `([^`]+)`: ", r"与 proposed 项目口径 `\1` 不一致：", message)
        priority = "P1"
    elif re.search(r"TLOG/TDW 表缺少 `([^`]+)` 分区过滤", message):
        field = re.search(r"TLOG/TDW 表缺少 `([^`]+)` 分区过滤", message).group(1)
        code = "missing_partition"
        title = "缺少分区裁剪"
        detail = f"TLOG/TDW 表缺少项目配置分区字段 `{field}` 过滤；先补上下界，再重新跑结果。"
        priority = "P0"
    elif re.search(r"`([^`]+)` 分区过滤不完整", message):
        code = "incomplete_partition"
        title = "分区上下界不完整"
        detail = "分区过滤不完整，建议同时提供开始和结束分区，避免扫描范围不可控。"
        priority = "P1"
    elif re.search(r"`([^`]+)` 被函数包裹", message):
        code = "partition_wrapped"
        title = "分区字段被函数包裹"
        detail = "分区字段被函数包裹，可能影响分区裁剪。"
        priority = "P1"
    elif "生产 SQL 不应使用 `SELECT *`" in message:
        code = "select_star"
        title = "禁止 SELECT *"
        detail = "生产/沉淀 SQL 需要显式列出字段。"
        priority = "P0"
    elif "SQL 侧脱敏被禁止" in message:
        code = "sql_side_privacy_transform"
        title = "禁止 SQL 侧脱敏"
        detail = "删除 MD5/SHA/HASH/BASE64/AES/MASK 等脱敏表达式；业务所需标识保持原值，隐私由 DA 侧统一处理。"
        priority = "P0"
    elif "疑似不完整 SQL 条件" in message:
        code = "incomplete_predicate"
        title = "SQL 条件不完整"
        detail = "存在形如 `iZoneAreaID =`、空 IN 列表或 LIKE/BETWEEN 缺值的条件；SQL 很可能无法正确执行或过滤口径缺失。"
        priority = "P0"
    elif "结果文件缺少 SQL 输出字段" in message or "结果文件包含 SQL 未声明输出字段" in message or "结果文件列顺序" in message:
        code = "result_column_mismatch"
        title = "结果列不匹配"
        detail = "结果文件列与 SQL 最终 SELECT 不一致；需要确认是 SQL 版本不一致、结果文件配错，还是输出字段命名需要统一。"
        priority = "P1"
    elif "missing_result_file" in message:
        code = "missing_result_file"
        title = "缺少结果文件"
        detail = "未找到同目录同名查询结果文件，无法核对结果形态和样例。"
        priority = "P1"
    elif "结果文件来自代理执行项目" in message:
        code = "proxy_evidence"
        title = "代理跑数证据"
        detail = "结果来自代理执行环境，只能证明该环境可跑和结果形态可参考，不能证明目标项目已验证。"
        priority = "P2"
    elif "SQL 使用的物理表不完全符合 delivery 项目表名 profile" in message:
        code = "delivery_table_rewrite"
        title = "后续需改目标项目表名"
        detail = "当前 SQL 表名与交付项目表名规则不完全一致；转目标项目查询或看板前需要改表名并重跑。"
        priority = "P2"
    elif message.startswith("proxy concept limitation:"):
        code = "proxy_rule_limitation"
        title = "代理口径限制"
        detail = humanize_proxy_limitation(message)
        priority = "P2"
    elif "JOIN 数量较多" in message or "CTE 层数较多" in message:
        code = "complexity"
        title = "复杂度偏高"
        detail = "SQL 结构较复杂；若后续会复用或频繁跑，建议考虑拆解或沉淀中间表。"
        priority = "P2"
    elif "COUNT(DISTINCT" in message:
        code = "heavy_distinct"
        title = "去重计数成本"
        detail = "存在 COUNT DISTINCT，后续正式化前需要关注性能或预聚合。"
        priority = "P2"
    elif "窗口函数" in message:
        code = "window_function"
        title = "窗口函数需核对"
        detail = "窗口函数需要确认分区键、排序键和数据量是否合理。"
        priority = "P2"
    elif "全局 `ORDER BY`" in message:
        code = "global_order_by"
        title = "全局排序成本"
        detail = "存在全局 ORDER BY，若不是最终展示必需，建议减少排序成本。"
        priority = "P2"
    return {
        "code": code,
        "priority": priority,
        "title": title,
        "detail": clip_text(detail, 320),
        "source": finding.severity,
    }


def metric_confirmation_issues(review: FileReview) -> list[dict]:
    issues: list[dict] = []
    weak_metrics = [
        item["metric"]
        for item in metric_logic_items(review)
        if item.get("needs_manual_confirmation") or item.get("description_source") == "needs_manual_confirmation"
    ]
    if weak_metrics:
        add_review_issue(
            issues,
            "metric_logic_needs_confirmation",
            "P1",
            "指标口径需确认",
            "以下指标只能从表达式弱推断，建议人工确认业务含义、Base、分子/分母："
            + ", ".join(weak_metrics[:6]),
            "metric_logic",
        )
    return issues


def review_issues(review: FileReview) -> list[dict]:
    issues: list[dict] = []
    for check in review.rule_checks:
        if check.result == "conflict":
            add_review_issue(issues, "confirmed_rule_conflict", "P0", "口径冲突", humanize_rule_check(check), "rule_check")
        elif check.result == "proposed_conflict":
            add_review_issue(issues, "proposed_rule_conflict", "P1", "待确认口径差异", humanize_rule_check(check), "rule_check")
        elif check.result == "needs_manual_check":
            add_review_issue(issues, "rule_needs_manual_check", "P1", "自动核对证据不足", humanize_rule_check(check), "rule_check")
    for finding in review.findings:
        issue = finding_to_review_issue(finding)
        add_review_issue(issues, issue["code"], issue["priority"], issue["title"], issue["detail"], issue["source"])
    if review.result_file is None:
        add_review_issue(issues, "missing_result_file", "P1", "缺少结果文件", "补同目录同名 `.csv/.xlsx/.txt` 结果文件后再核对结果形态。", "result_file")
    elif result_is_field_mismatch(review):
        issue_parts = []
        if review.result_file.missing_columns:
            issue_parts.append("结果缺少：" + ", ".join(review.result_file.missing_columns[:8]))
        if review.result_file.extra_columns:
            issue_parts.append("结果多出：" + ", ".join(review.result_file.extra_columns[:8]))
        if review.result_file.order_mismatch:
            issue_parts.append("字段顺序不一致")
        add_review_issue(
            issues,
            "result_column_mismatch",
            "P1",
            "结果列不匹配",
            "；".join(issue_parts) or "结果文件列与 SQL 输出字段不一致。",
            "result_file",
        )
    issues.extend(metric_confirmation_issues(review))
    return sorted(issues, key=lambda item: (issue_priority_value(item["priority"]), item["code"], item["detail"]))


def result_brief(review: FileReview) -> str:
    result = review.result_file
    if result is None:
        return "无结果文件"
    rows = result.row_count if result.row_count is not None else "未知行数"
    if result_is_field_mismatch(review):
        return f"有结果文件（{rows} 行），但列不匹配"
    if result.status != "loaded":
        return f"结果文件读取状态：{result.status}"
    return f"结果文件已读取（{rows} 行，{len(result.columns)} 列）"


def reviewer_card(review: FileReview, roles: ReviewRoleContext) -> dict:
    issues = review_issues(review)
    issue_codes = {item["code"] for item in issues}
    p0 = [item for item in issues if item["priority"] == "P0"]
    p1 = [item for item in issues if item["priority"] == "P1"]
    if "confirmed_rule_conflict" in issue_codes:
        bucket = "P0_RULE_CONFLICT"
        title = "先处理口径冲突"
        action = "不要直接复用；先改 SQL 或确认本批次是否允许例外。"
        severity = "fail"
    elif "missing_partition" in issue_codes or "select_star" in issue_codes or "sql_side_privacy_transform" in issue_codes:
        bucket = "P0_SQL_FIX"
        title = "先修 SQL 再重跑"
        action = "补齐阻断项后重新跑数，再回来看结果证据。"
        severity = "fail"
    elif p0:
        bucket = "P0_SQL_FIX"
        title = "先修 SQL 再重跑"
        action = "先修复 P0 SQL 阻断项，再重新生成或重跑结果文件。"
        severity = "fail"
    elif "result_column_mismatch" in issue_codes:
        bucket = "P1_RESULT_ALIGNMENT"
        title = "先对齐 SQL 与结果文件"
        action = "确认结果文件是否来自同版 SQL；必要时重跑或改输出字段。"
        severity = "warn"
    elif "missing_result_file" in issue_codes:
        bucket = "P1_RESULT_REQUIRED"
        title = "补结果文件"
        action = "补同名结果文件后再判断结果形态和字段。"
        severity = "warn"
    elif "incomplete_partition" in issue_codes or "partition_wrapped" in issue_codes:
        bucket = "P1_SQL_CLEANUP"
        title = "补全 SQL 技术项"
        action = "补齐分区上下界或裁剪写法后，再决定是否复用。"
        severity = "warn"
    elif p1:
        bucket = "P1_CONFIRM_LOGIC"
        title = "人工确认口径"
        action = "先确认项目口径、指标含义或字段映射，再决定是否复用。"
        severity = "warn"
    elif review.evidence_status == "proxy_reviewed_needs_target_verification":
        bucket = "P2_PROXY_USABLE"
        title = "可作为代理证据"
        action = "可以参考当前跑数形态；转目标项目或看板前必须目标环境重跑验证。"
        severity = "warn"
    elif issues:
        bucket = "P2_REVIEW_NOTES"
        title = "有非阻断提醒"
        action = "可继续人工审核，同时处理性能或可维护性提醒。"
        severity = "warn"
    else:
        bucket = "PASS_REVIEWED_QUERY"
        title = "未发现主要阻断"
        action = "可进入复用评估或下一步生命周期。"
        severity = "pass"

    blockers = p0[:MAX_REVIEW_CARD_ISSUES]
    confirmations = p1[:MAX_REVIEW_CARD_ISSUES]
    notes = [item for item in issues if item["priority"] in {"P2", "INFO"}][:MAX_REVIEW_CARD_ISSUES]
    steps = []
    if blockers:
        steps.append("先处理 P0 阻断：" + "；".join(item["title"] for item in blockers[:2]))
    if confirmations:
        steps.append("再确认 P1 项：" + "；".join(item["title"] for item in confirmations[:2]))
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        steps.append("记录为代理证据，目标项目上线/看板化前重新跑数。")
    if not steps:
        steps.append(action)
    if review.result_file and not result_is_field_mismatch(review) and review.result_file.status == "loaded":
        steps.append("抽看结果样例和关键指标是否符合业务预期。")
    steps = unique_in_order(steps)[:MAX_REVIEW_CARD_STEPS]
    why_parts = []
    if blockers:
        why_parts.append("存在阻断项")
    if confirmations:
        why_parts.append("存在需人工确认项")
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        why_parts.append("当前结果是代理环境证据")
    if review.delivery_table_mismatches:
        why_parts.append("后续转目标项目需要改表名/profile")
    if not why_parts:
        why_parts.append("静态检查未发现主要阻断")
    return {
        "schema_version": REVIEW_CARD_SCHEMA_VERSION,
        "bucket": bucket,
        "severity": severity,
        "priority": bucket.split("_", 1)[0] if bucket.startswith("P") else "PASS",
        "title": title,
        "action": action,
        "why": "；".join(why_parts),
        "reviewer_steps": steps,
        "blockers": blockers,
        "confirmations": confirmations,
        "notes": notes,
        "result_brief": result_brief(review),
        "execution_brief": f"{project_label(review.execution_project)} / {review.execution_inference_confidence}",
    }


def review_card_counts(reviews: list[FileReview], roles: ReviewRoleContext) -> Counter:
    return Counter(reviewer_card(review, roles)["bucket"] for review in reviews)


def checked_rule_topics(review: FileReview) -> list[str]:
    topics: list[str] = []
    checked = set(review.checked_concept_keys)
    for check in review.rule_checks:
        if not checked or check.concept_key in checked:
            topics.append(rule_check_human_topic(check))
    return unique_in_order(topics)


def review_judgement_guide(review: FileReview, roles: ReviewRoleContext) -> dict:
    stage_goal = (
        "这是看板/部署 SQL：重点判断是否满足目标项目上线门禁。"
        if is_dashboard_sql(review)
        else "这是查询 SQL/代理跑数材料：重点判断逻辑口径和结果形态是否可信，不把它当成已可部署看板。"
    )
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        stage_goal += " 当前结果属于代理环境证据，只能支持逻辑和形态审核，目标项目仍需重跑验证。"

    checks: list[dict[str, str]] = [
        {
            "name": "阶段和证据等级",
            "look_at": "看 definition/execution/delivery 项目、evidence_status 和结果文件是否同名匹配。",
            "how_to_judge": "先确认这批 SQL 是查询材料、代理跑数，还是正式看板 SQL；代理结果不能当目标项目已验证。",
            "pass_criteria": "阶段说法与用户目标一致；若是代理跑数，报告明确保留目标项目重跑计划。",
        },
        {
            "name": "指标口径",
            "look_at": "先看“指标逻辑审核”中的一句话说明、核心 Base、计算路径和每张指标卡。",
            "how_to_judge": "每个指标都要能用中文复述：统计谁、在什么范围内、分子是谁、分母是谁、怎么算。",
            "pass_criteria": "业务话术、SQL 追溯和结果样例能互相解释；低置信度或只靠表达式推断的指标必须人工确认。",
        },
        {
            "name": "业务筛选/ID范围",
            "look_at": "看业务筛选卡里的模式 ID、区服 ID、战斗服 ID、道具 ID 等条件，以及是否有映射名称/大类。",
            "how_to_judge": "确认这些筛选是否就是指标 Base 的业务范围；GameMode 有映射时要核对 ID、名称、大类，未知 ID 需要补映射或向用户确认。",
            "pass_criteria": "筛选范围、ID 含义、映射关系都能被业务方复述；未知或临时筛选在结论中明确标注。",
        },
        {
            "name": "分组/维度",
            "look_at": "看分组方式、维度字段和最终输出字段。",
            "how_to_judge": "确认这些维度是否就是审核者要拆分的视角；ID、名称、分桶、排序字段是否成对且含义明确。",
            "pass_criteria": "输出粒度清楚，不会因为漏分组、错分桶或多余字段改变指标解释。",
        },
        {
            "name": "项目口径",
            "look_at": "看“保存口径核对”里的口径名称、口径含义和判断方法，而不是内部 rule/concept key。",
            "how_to_judge": "逐条对照 SQL 的 WHERE/JOIN/CASE/SELECT；冲突必须先改 SQL 或让用户确认例外。",
            "pass_criteria": "confirmed 口径无冲突；proposed 或需人工核对的口径有明确处理结论。",
        },
        {
            "name": "结果证据",
            "look_at": "看结果文件行数、列名、样例和最终 SELECT 输出列的对齐情况。",
            "how_to_judge": "列名/顺序要能证明结果来自同一版 SQL；样例值要与 Base、分组和指标量级相符。",
            "pass_criteria": "同名结果文件可读取，列与 SQL 输出一致；代理环境结果被标成代理证据。",
        },
        {
            "name": "代码质量",
            "look_at": "看 P0/P1 finding，尤其是不完整条件、分区裁剪、SQL 侧脱敏、SELECT *、复杂度。",
            "how_to_judge": "P0 先修后重跑；P1 至少在复用或沉淀前处理；P2 作为维护性建议。",
            "pass_criteria": "没有 P0 blocker；正式沉淀前分区、最终输出字段和性能风险可解释。"
        },
    ]
    blockers = [item["title"] for item in reviewer_card(review, roles)["blockers"][:3]]
    confirmations = [item["title"] for item in reviewer_card(review, roles)["confirmations"][:3]]
    return {
        "goal": stage_goal,
        "decision_order": "先判阶段/证据 -> 再判指标口径 -> 再看项目口径 -> 再看结果样例 -> 最后处理代码质量和维护性。",
        "checks": checks,
        "current_blockers": blockers,
        "current_confirmations": confirmations,
        "checked_rule_topics": checked_rule_topics(review),
        "business_filters": review.business_filters,
    }


def review_bucket_label(bucket: str) -> str:
    labels = {
        "P0_RULE_CONFLICT": "P0 口径冲突",
        "P0_SQL_FIX": "P0 先修 SQL",
        "P1_RESULT_ALIGNMENT": "P1 对齐结果文件",
        "P1_RESULT_REQUIRED": "P1 补结果文件",
        "P1_SQL_CLEANUP": "P1 补全 SQL 技术项",
        "P1_CONFIRM_LOGIC": "P1 人工确认口径",
        "P2_PROXY_USABLE": "P2 代理证据可参考",
        "P2_REVIEW_NOTES": "P2 非阻断提醒",
        "PASS_REVIEWED_QUERY": "可进入下一步",
    }
    return labels.get(bucket, bucket)


def review_bucket_sort_key(bucket: str) -> tuple[int, str]:
    order = {
        "P0_RULE_CONFLICT": 0,
        "P0_SQL_FIX": 1,
        "P1_RESULT_ALIGNMENT": 2,
        "P1_RESULT_REQUIRED": 3,
        "P1_SQL_CLEANUP": 4,
        "P1_CONFIRM_LOGIC": 5,
        "P2_PROXY_USABLE": 6,
        "P2_REVIEW_NOTES": 7,
        "PASS_REVIEWED_QUERY": 8,
    }
    return (order.get(bucket, 99), bucket)


def status_for_findings(findings: list[QualityFinding]) -> str:
    if any(item.severity == "BLOCKER" for item in findings):
        return "fail"
    if any(item.severity == "WARN" for item in findings):
        return "warn"
    return "pass"


def is_dashboard_sql(review: FileReview) -> bool:
    text = review.sql.lower()
    return "@dashboard_sql_spec" in text or "dashboard_sql" in review.path.as_posix().lower()


def logic_dimension(review: FileReview, roles: ReviewRoleContext) -> dict:
    conflicts = [check for check in review.rule_checks if check.result == "conflict"]
    proposed_conflicts = [check for check in review.rule_checks if check.result == "proposed_conflict"]
    manual_checks = [check for check in review.rule_checks if check.result == "needs_manual_check"]
    active_rules = [
        rule
        for rule in (roles.definition.canonical_rules if roles.definition else [])
        if rule.status in {"confirmed", "proposed"}
    ]
    items: list[str] = []
    if conflicts:
        items.extend(humanize_rule_check(check) for check in conflicts)
        return {"status": "fail", "value": "confirmed_rule_conflict", "items": items}
    if proposed_conflicts:
        items.extend(humanize_rule_check(check) for check in proposed_conflicts)
    if manual_checks:
        items.extend(humanize_rule_check(check) for check in manual_checks[:5])
    if not active_rules:
        items.append("no saved project rules loaded; logic review is SQL-internal only")
    if not review.tables:
        items.append("no source table detected")
    if not review.metrics and not review.dimensions:
        items.append("metrics/dimensions are weakly inferred")
    if items:
        return {"status": "warn", "value": "needs_logic_review", "items": items}
    return {"status": "pass", "value": "logic_review_passed", "items": ["no saved-rule conflict detected"]}


def code_quality_dimension(review: FileReview) -> dict:
    status = status_for_findings(review.findings)
    if review.grade == "C" and status == "pass":
        status = "warn"
    items = [f"{item.severity}: {item.message}" for item in review.findings[:8]]
    if not items:
        items = ["no static quality blockers"]
    return {"status": status, "value": f"grade_{review.grade}", "items": items}


def evidence_dimension(review: FileReview) -> dict:
    status = "pass"
    if review.evidence_status in {"missing_result_file", "proxy_reviewed_needs_target_verification"}:
        status = "warn"
    if review.evidence_status == "field_mismatch" or review.evidence_status.startswith("result_"):
        status = "fail"
    items = [f"evidence_status: {review.evidence_status}"]
    if review.result_file:
        items.append(f"result_file: {review.result_file.path.name}")
        items.append(f"rows: {review.result_file.row_count if review.result_file.row_count is not None else 'unknown'}")
        if review.result_file.missing_columns:
            items.append("missing columns: " + ", ".join(review.result_file.missing_columns))
        if review.result_file.extra_columns:
            items.append("extra columns: " + ", ".join(review.result_file.extra_columns))
        if review.result_file.order_mismatch:
            items.append("column order mismatch")
    else:
        items.append("same-stem result file is missing")
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        items.append("proxy evidence cannot replace target-project verification")
    return {"status": status, "value": review.evidence_status, "items": items}


def dashboard_fit_dimension(review: FileReview) -> dict:
    items: list[str] = []
    status = "pass"
    if any(item.severity == "BLOCKER" for item in review.findings):
        status = "fail"
        items.append("SQL quality blockers must be fixed before dashboard conversion")
    if not review.final_fields:
        status = "fail"
        items.append("final SELECT fields are not detectable")
    if review.result_file is None:
        status = "warn" if status != "fail" else status
        items.append("result file is missing; dashboard shape cannot be fully checked")
    elif result_status(review) == "field_mismatch":
        status = "warn" if status != "fail" else status
        items.append("result columns differ from final SELECT fields")
    if not review.metrics:
        status = "warn" if status != "fail" else status
        items.append("dashboard metrics are weakly inferred")
    if review.delivery_table_mismatches:
        status = "warn" if status != "fail" else status
        items.append("delivery table/profile rewrite will be needed for target project")
    if not items:
        items.append("query output appears usable as dashboard-conversion input")
    value = {
        "pass": "dashboard_input_ready",
        "warn": "needs_dashboard_design_or_evidence",
        "fail": "not_ready_for_dashboard_conversion",
    }[status]
    return {"status": status, "value": value, "items": items}


def deployment_gate_dimension(review: FileReview) -> dict:
    if not is_dashboard_sql(review):
        return {
            "status": "not_applicable",
            "value": "not_applicable_query_sql",
            "items": ["this appears to be query SQL, not dashboard/deployment SQL"],
        }
    readiness = review.deployment_readiness
    status = "pass" if readiness == "ready" else "fail"
    if readiness in {"needs_rewrite", "needs_target_verification"}:
        status = "warn"
    items = [f"deployment_readiness: {readiness}"]
    if review.delivery_table_mismatches:
        items.extend(review.delivery_table_mismatches)
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        items.append("target-project verification is still required")
    return {"status": status, "value": readiness, "items": items}


def review_dimensions(review: FileReview, roles: ReviewRoleContext) -> dict[str, dict]:
    return {
        "logic": logic_dimension(review, roles),
        "code_quality": code_quality_dimension(review),
        "evidence": evidence_dimension(review),
        "dashboard_fit": dashboard_fit_dimension(review),
        "deployment_gate": deployment_gate_dimension(review),
    }


def dimension_status_counts(reviews: list[FileReview], roles: ReviewRoleContext) -> dict[str, Counter]:
    counts = {key: Counter() for key, _ in REVIEW_DIMENSIONS}
    for review in reviews:
        dims = review_dimensions(review, roles)
        for key in counts:
            counts[key][dims[key]["status"]] += 1
    return counts


def next_focus_for(review: FileReview, roles: ReviewRoleContext) -> str:
    card = reviewer_card(review, roles)
    if card["blockers"]:
        return card["blockers"][0]["title"] + "：" + card["blockers"][0]["detail"]
    if card["confirmations"]:
        return card["confirmations"][0]["title"] + "：" + card["confirmations"][0]["detail"]
    if card["notes"]:
        return card["notes"][0]["title"] + "：" + card["notes"][0]["detail"]
    return card["action"]


def legacy_next_focus_for(review: FileReview, roles: ReviewRoleContext) -> str:
    dims = review_dimensions(review, roles)
    priority = ["logic", "code_quality", "evidence", "dashboard_fit", "deployment_gate"]
    for key in priority:
        if dims[key]["status"] == "fail":
            return dims[key]["items"][0] if dims[key]["items"] else dims[key]["value"]
    for key in priority:
        if dims[key]["status"] == "warn":
            return dims[key]["items"][0] if dims[key]["items"] else dims[key]["value"]
    return "no immediate blocker; keep as reviewed material or proceed to the next lifecycle step"


def render_result_file(review: FileReview, directory: Path) -> str:
    result = review.result_file
    if result is None:
        return "- status: `missing_result_file`\n- note: 未找到同目录同名 `.xlsx/.csv/.txt` 查询结果文件。"
    lines = [
        f"- status: `{result.status}`",
        f"- file: `{relative_name(directory, result.path)}`",
        f"- file_type: `{result.file_type}`",
        f"- row_count: `{result.row_count if result.row_count is not None else 'unknown'}`",
        f"- columns: `{', '.join(result.columns) or 'none'}`",
    ]
    if result.note:
        lines.append(f"- note: {result.note}")
    if result.alternatives:
        lines.append(f"- alternative_result_files: `{', '.join(path.name for path in result.alternatives)}`")
    if result.missing_columns:
        lines.append(f"- missing_columns: `{', '.join(result.missing_columns)}`")
    if result.extra_columns:
        lines.append(f"- extra_columns: `{', '.join(result.extra_columns)}`")
    if result.order_mismatch:
        lines.append("- order_mismatch: `true`")
    lines.extend(["", "Sample rows:", "", markdown_sample(result.sample_rows)])
    return "\n".join(lines)


def format_status_counts(counts: Counter) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={counts[key]}" for key in sorted(counts))


def render_role_context(roles: ReviewRoleContext) -> str:
    execution_candidates = ", ".join(project_label(project) for project in roles.execution_projects) or "none"
    return "\n".join(
        [
            "- review_entry: `SQL审查`",
            "- review_output_model: `logic + code_quality + evidence + dashboard_fit + deployment_gate`",
            f"- definition_project: `{project_label(roles.definition)}`",
            f"- delivery_project: `{project_label(roles.delivery)}`",
            f"- execution_project_candidates: `{execution_candidates}`",
            "- evidence_policy: `proxy execution results are evidence status, not a separate user-selected review type`",
            "- deployment_policy: `deployment gate is not_applicable for query SQL and only evaluated for dashboard/deployment SQL`",
        ]
    )


def render_file_role_context(review: FileReview, roles: ReviewRoleContext) -> str:
    dims = review_dimensions(review, roles)
    lines = [
        f"- definition_project: `{project_label(roles.definition)}`",
        f"- inferred_execution_project: `{project_label(review.execution_project)}`",
        f"- execution_inference: `{review.execution_inference_confidence}` ({review.execution_inference_reason or 'none'})",
        f"- delivery_project: `{project_label(roles.delivery)}`",
        f"- evidence_status: `{review.evidence_status}`",
        f"- logic: `{dims['logic']['status']}` / `{dims['logic']['value']}`",
        f"- code_quality: `{dims['code_quality']['status']}` / `{dims['code_quality']['value']}`",
        f"- evidence: `{dims['evidence']['status']}` / `{dims['evidence']['value']}`",
        f"- dashboard_fit: `{dims['dashboard_fit']['status']}` / `{dims['dashboard_fit']['value']}`",
        f"- deployment_gate: `{dims['deployment_gate']['status']}` / `{dims['deployment_gate']['value']}`",
    ]
    if roles.delivery and dims["deployment_gate"]["status"] == "not_applicable":
        lines.append(
            "- future_delivery_note: 当前按查询 SQL 审查；delivery 表名/profile、目标环境跑数和看板契约在后续转换或部署阶段处理。"
        )
    if review.delivery_table_mismatches:
        lines.append("- delivery_conversion_requirements:")
        lines.extend(f"  - {item}" for item in review.delivery_table_mismatches)
    if review.checked_concept_keys:
        lines.append(f"- checked_concept_keys: `{', '.join(review.checked_concept_keys)}`")
    if review.proxy_limitations:
        lines.append("- proxy_limitations:")
        lines.extend(f"  - {item}" for item in review.proxy_limitations)
    if review.evidence_status == "proxy_reviewed_needs_target_verification":
        lines.append(f"- future_target_verification_plan: {review.future_target_verification_plan}")
    return "\n".join(lines)


def relative_name(base: Path, path: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def rule_display(rule: RuleCandidate) -> str:
    return rule.text.replace("|", "\\|")


def render_rule_checks(checks: list[RuleCheck]) -> str:
    if not checks:
        return "- No saved project rules matched or conflicted in static review."
    lines = [
        "| 口径 | 含义 | 检查结果 | 怎么判断 | 通过标准 | 内部索引 |",
        "|---|---|---|---|---|---|",
    ]
    order = {"conflict": 0, "proposed_conflict": 1, "needs_manual_check": 2, "matched": 3}
    for check in sorted(checks, key=lambda item: (order.get(item.result, 9), item.rule_id)):
        topic = markdown_cell(rule_check_human_topic(check))
        summary = markdown_cell(check.rule_summary or "未记录摘要")
        result = markdown_cell(check.message or check.result)
        method = markdown_cell(rule_check_judgement_method(check))
        criteria = markdown_cell(rule_check_pass_criteria(check))
        index = markdown_cell(f"{check.rule_id} / {check.concept_key or 'no_concept_key'} / {check.status}:{check.result}")
        lines.append(
            f"| {topic} | {summary} | {result} | {method} | {criteria} | `{index}` |"
        )
    return "\n".join(lines)


def render_metric_logic(review: FileReview) -> str:
    items = metric_logic_items(review)
    if not items:
        return "- No metric logic inferred from final SELECT."
    lines = [
        "| Metric | 指标业务口径 | Base / 分母基准 | 分子说明 | 分母说明 | 计算说明 | Source | Confidence |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            f"| `{item['metric']}` | {markdown_cell(item['business_definition'])} | {markdown_cell(item['base_population'])} | "
            f"{markdown_cell(item['numerator'])} | {markdown_cell(item['denominator'])} | "
            f"{markdown_cell(item['formula'])} | `{item['description_source']}` | `{item['confidence']}` |"
        )
    lines.extend(["", "SQL evidence, filters, and related口径:"])
    for item in items:
        lines.append(f"- `{item['metric']}`")
        lines.append(f"  - calculation_type: `{item['calculation_type']}`")
        lines.append(f"  - formula_expression: `{item['formula_expression']}`")
        if item["numerator_expression"] or item["denominator_expression"]:
            lines.append(
                f"  - expression_trace: numerator=`{item['numerator_expression']}`; "
                f"denominator=`{item['denominator_expression']}`; base=`{item['base_expression']}`"
            )
        if item["metric_filters"]:
            lines.append(f"  - metric_filters: `{'; '.join(item['metric_filters'])}`")
        if item["global_filters"]:
            lines.append(f"  - base_filters: `{'; '.join(item['global_filters'][:5])}`")
        if item["lineage"]:
            lines.append(f"  - lineage: `{'; '.join(item['lineage'][:4])}`")
        if item.get("source_steps"):
            lines.append("  - source_steps:")
            for step in item.get("source_steps", []):
                lines.append(
                    f"    - {step.get('role')}: {step.get('story')} "
                    f"(operand=`{step.get('operand')}`, expression=`{step.get('field_expression')}`)"
                )
        if item["related_saved_rule_checks"]:
            rules = "; ".join(
                f"{check['rule_id']}:{check['result']} - {check.get('title') or ''} - {check.get('rule_summary') or check.get('message') or ''}"
                for check in item["related_saved_rule_checks"]
            )
            lines.append(f"  - saved_rule_checks: `{rules}`")
        if item["needs_manual_confirmation"]:
            lines.append("  - needs_manual_confirmation: `true`")
    return "\n".join(lines)


def cte_human_steps(review: FileReview) -> list[str]:
    context = metric_business_context(review.sql)
    steps: list[str] = []
    for name, info in context.cte_lineage.items():
        if len(steps) >= 14:
            break
        if not info.get("source_tables") and not info.get("comment") and not info.get("select_expressions"):
            continue
        parts = [f"步骤 `{name}`"]
        if info.get("comment"):
            parts.append(str(info.get("comment")))
        if info.get("source_tables"):
            parts.append("读取 " + table_story(info.get("source_tables", [])))
        if info.get("group_by"):
            parts.append("按 " + "、".join(info.get("group_by", [])[:6]) + " 聚合")
        filter_stories = business_condition_story(info.get("where_conditions", []) + info.get("join_conditions", []), limit=3)
        if filter_stories:
            parts.append("业务筛选：" + "；".join(filter_stories))
        output_aliases = list(info.get("select_expressions", {}).keys())[:6]
        if output_aliases:
            parts.append("产出字段：" + "、".join(output_aliases))
        steps.append("；".join(parts) + "。")
    if steps:
        return steps
    return extract_metric_calculation_path(review.sql)


def product_sql_date_literal(sql: str, alias: str) -> str:
    pattern = rf"(?:date\s+)?['\"](\d{{4}}-\d{{2}}-\d{{2}})['\"]\s+(?:as\s+)?`?{re.escape(alias)}`?"
    match = re.search(pattern, sql, flags=re.I)
    return match.group(1) if match else ""


def product_date_window(sql: str) -> str:
    start = product_sql_date_literal(sql, "start_date")
    end = product_sql_date_literal(sql, "end_date")
    if start and end:
        return f"{start} 至 {end}"
    if start:
        return f"{start} 起"
    if end:
        return f"截至 {end}"
    return "当前查询时间窗口"


def product_table_short_name(table: str, catalog: dict[str, str] | None = None) -> str:
    if has_tlog_table([table]):
        return display_log_name(table, catalog)
    name = table.strip("`").rsplit(".", 1)[-1]
    if "conf_pack" in name.lower():
        return "conf_pack 标签表"
    if "tag" in name.lower():
        return f"{name} 标签表"
    return name


PRODUCT_FIELD_DISPLAY_NAMES = {
    "izoneareaid": "iZoneAreaID",
    "gamesvrid": "GameSvrId",
    "gamemode": "GameMode",
    "gamemodeid": "GameMode",
    "battlesrvid": "BattleSrvId",
    "uniquebattleid": "UniqueBattleID",
    "vopenid": "vOpenID",
    "openid": "OpenID",
    "dteventtime": "dtEventTime",
    "dteventdate": "dtEventDate",
    "totalactiveduration": "TotalActiveDuration",
    "onlinetime": "OnlineTime",
    "matchduration": "MatchDuration",
    "battlemissionid": "BattleMissionId",
    "battlemissionsubid": "BattleMissionSubId",
    "battlemissioncomplete": "BattleMissionComplete",
    "battleitemid": "BattleItemId",
    "battleitemdelta": "BattleItemDelta",
    "templateid": "TemplateId",
    "deltavalue": "DeltaValue",
    "itemid": "ItemId",
    "propid": "PropId",
}

PRODUCT_FIELD_CHINESE_LABELS = {
    "izoneareaid": "区服/大区ID",
    "gamesvrid": "游戏服ID",
    "gamemode": "玩法模式",
    "gamemodeid": "玩法模式",
    "battlesrvid": "战斗服ID",
    "uniquebattleid": "唯一战斗ID",
    "vopenid": "玩家OpenID",
    "openid": "玩家OpenID",
    "roleid": "角色ID",
    "teamid": "队伍ID",
    "battleteamid": "战斗内队伍ID",
    "teamnum": "队伍人数",
    "id": "记录/阶段ID",
    "registerresult": "报名结果",
    "territorylevel": "报名时情报等级",
    "teamgold": "队伍结算金币",
    "stagegold": "报名阶段发放金币",
    "battleitemdelta": "道具/金币变化量",
    "battleitemchangesource": "道具变化来源",
    "airdroptype": "空投类型",
    "goldcoinairdropboxids": "空投箱ID",
    "territoyconstructionid": "领地建筑ID",
    "totalactiveduration": "累计非挂机时长",
    "onlinetime": "在线时长",
    "matchduration": "匹配耗时",
    "dteventtime": "事件时间",
    "dteventdate": "事件日期",
}


def product_field_display_name(value: str) -> str:
    canonical = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).lower()
    return PRODUCT_FIELD_DISPLAY_NAMES.get(canonical, str(value or "").strip("`"))


def product_field_with_chinese(value: str) -> str:
    english = product_field_display_name(value)
    canonical = re.sub(r"[^A-Za-z0-9]", "", english or str(value or "")).lower()
    chinese = PRODUCT_FIELD_CHINESE_LABELS.get(canonical, "")
    return f"{english}（{chinese}）" if chinese else english


def product_field_list_with_chinese(values: list[str]) -> list[str]:
    return unique_in_order(product_field_with_chinese(value) for value in values if str(value).strip())


def product_value_looks_like_field_ref(value: str) -> bool:
    cleaned = str(value or "").strip().strip("'\"`")
    if not cleaned:
        return False
    normalized = cleaned.replace("`", "")
    if re.fullmatch(r"[A-Za-z_][\w]*\.[A-Za-z_][\w]*", normalized):
        return True
    return re.sub(r"[^A-Za-z0-9]", "", normalized).lower() in PRODUCT_FIELD_DISPLAY_NAMES


def strip_product_sql_aliases(value: str) -> str:
    text = str(value or "")

    def replace_alias(match: re.Match[str]) -> str:
        return product_field_display_name(match.group("field"))

    text = re.sub(r"(?<![\w`])`?[A-Za-z_][\w]*`?\s*\.\s*`?(?P<field>[A-Za-z_][\w]*)`?", replace_alias, text)
    for label in sorted(set(PRODUCT_FIELD_DISPLAY_NAMES.values()), key=len, reverse=True):
        text = re.sub(
            rf"{re.escape(label)}(?:\s*[、,，]\s*{re.escape(label)})+",
            label,
            text,
        )
    return text


def product_filter_value_text(values: list[str]) -> str:
    return "、".join(str(value).strip("'\"") for value in values if str(value).strip("'\""))


def product_scope_summaries(review: FileReview) -> list[str]:
    grouped_literals: dict[str, list[str]] = defaultdict(list)
    grouped_field_refs: dict[str, list[str]] = defaultdict(list)
    for item in review.business_filters:
        if item.get("kind") not in {"zone", "game_server"}:
            continue
        values = [str(value).strip("'\"") for value in item.get("values", []) if str(value).strip("'\"")]
        if not values:
            continue
        for value in values:
            if product_value_looks_like_field_ref(value):
                grouped_field_refs["游戏服/大区 ID"].append(product_field_display_name(value.rsplit(".", 1)[-1]))
            else:
                grouped_literals["游戏服/大区 ID"].append(strip_product_sql_aliases(value))
    rows: list[str] = []
    for label, values in grouped_literals.items():
        if values:
            rows.append(f"只看{label}：{product_filter_value_text(unique_in_order(values))}")
    for label, values in grouped_field_refs.items():
        if label in grouped_literals and grouped_literals[label]:
            continue
        field_text = product_filter_value_text(unique_in_order(values)) or "对应业务字段"
        rows.append(f"按{label}（{field_text}）对齐相关日志")
    return unique_in_order(rows)


def product_mode_value_text(values: list[str], mode_mapping: dict[str, dict[str, str]]) -> str:
    pieces: list[str] = []
    for value in values:
        cleaned = str(value).strip("'\"")
        mapped = mode_mapping.get(cleaned)
        if mapped:
            pieces.append(f"{cleaned}（{mapped.get('name')}/{mapped.get('category')}）")
        else:
            pieces.append(cleaned)
    return "、".join(pieces)


def product_game_mode_condition(condition: str, mode_mapping: dict[str, dict[str, str]]) -> str:
    constraints = [
        item
        for item in extract_constraints(condition)
        if business_filter_kind(item.field) == "game_mode"
    ]
    if not constraints:
        return clean_human_business_text(condition)
    parts: list[str] = []
    for constraint in constraints:
        values = [str(value).strip("'\"") for value in constraint.values]
        value_text = product_mode_value_text(values, mode_mapping)
        if constraint.operator.lower() == "in":
            parts.append(f"GameMode 属于 {value_text}")
        elif constraint.operator in {"=", "=="}:
            parts.append(f"GameMode 为 {value_text}")
        else:
            parts.append(f"GameMode {constraint.operator} {value_text}")
    return "；".join(parts)


def product_flag_label(alias: str) -> str:
    normalized = normalize_identifier(alias)
    labels = {
        "has_normal": "常规模式",
        "has_regular": "常规模式",
        "has_fast": "快速模式",
        "has_speed": "快速模式",
        "has_newbie": "新手服",
        "has_tutorial": "新手服",
    }
    return labels.get(normalized, friendly_identifier(alias))


def product_flag_definitions(sql: str, mode_mapping: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    definitions: dict[str, dict[str, str]] = {}
    cleaned = strip_sql_comments(sql)
    pattern = re.compile(
        r"case\s+when\s+(?P<condition>.*?)\s+then\s+1\s+else\s+0\s+end\s*\)\s+as\s+`?(?P<alias>has_[a-zA-Z_][\w]*)`?",
        flags=re.I | re.S,
    )
    # Most source SQL wraps these flags in MAX(...). Keep a second, looser pattern for plain CASE aliases.
    loose_pattern = re.compile(
        r"case\s+when\s+(?P<condition>.*?)\s+then\s+1\s+else\s+0\s+end\s+as\s+`?(?P<alias>has_[a-zA-Z_][\w]*)`?",
        flags=re.I | re.S,
    )
    for match in list(pattern.finditer(cleaned)) + list(loose_pattern.finditer(cleaned)):
        alias = normalize_identifier(match.group("alias"))
        condition = compact(match.group("condition"))
        definitions[alias] = {
            "label": product_flag_label(alias),
            "condition": product_game_mode_condition(condition, mode_mapping),
        }
    return definitions


def product_describe_flag_condition(condition: str, flag_defs: dict[str, dict[str, str]]) -> str:
    text = compact(condition)
    parts: list[str] = []
    for match in re.finditer(r"\b(has_[a-zA-Z_][\w]*)\b\s*=\s*([01])", text, flags=re.I):
        alias = normalize_identifier(match.group(1))
        label = flag_defs.get(alias, {}).get("label") or product_flag_label(alias)
        parts.append(label if match.group(2) == "1" else f"未命中{label}")
    if parts:
        return "、".join(parts)
    return clean_human_business_text(text)


def product_case_label_definitions(sql: str, alias: str, flag_defs: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    cleaned = strip_sql_comments(sql)
    definitions: list[dict[str, str]] = []
    pattern = re.compile(
        rf"\bcase\b(?P<body>.*?)\bend\s+as\s+`?{re.escape(alias)}`?",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(cleaned):
        body = match.group("body")
        for when_match in re.finditer(r"\bwhen\b(?P<condition>.*?)\bthen\s+'(?P<label>[^']+)'", body, flags=re.I | re.S):
            label = when_match.group("label")
            condition = product_describe_flag_condition(when_match.group("condition"), flag_defs)
            definitions.append({"label": label, "meaning": condition})
        else_match = re.search(r"\belse\s+'(?P<label>[^']+)'", body, flags=re.I | re.S)
        if else_match:
            definitions.append(
                {
                    "label": else_match.group("label"),
                    "meaning": "其他未命中上述分桶的玩家；需要确认这个兜底名称是否符合业务理解。",
                }
            )
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in definitions:
        key = f"{item['label']}:{item['meaning']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def product_literal_values_for_alias(sql: str, alias: str) -> list[str]:
    pattern = rf"'([^']+)'\s+(?:as\s+)?`?{re.escape(alias)}`?"
    return unique_in_order(match.group(1) for match in re.finditer(pattern, sql, flags=re.I))


def product_tag_source(context: MetricBusinessContext, catalog: dict[str, str] | None = None) -> str:
    for info in context.cte_lineage.values():
        select_fields = " ".join(info.get("select_expressions", {}).keys()).lower()
        tables = info.get("source_tables", [])
        if "cbttype" not in select_fields and not any("conf_pack" in table.lower() for table in tables):
            continue
        if tables:
            return product_table_short_name(tables[0], catalog)
    return "标签表"


def product_segment_definitions(sql: str, context: MetricBusinessContext, catalog: dict[str, str] | None = None) -> list[dict[str, str]]:
    labels = product_literal_values_for_alias(sql, "crowd_type")
    if not labels:
        return []
    tag_source = product_tag_source(context, catalog)
    definitions: list[dict[str, str]] = []
    for label in labels:
        if label == "整体":
            meaning = "全部 Base 玩家。"
        elif "老" in label and "玩家" in label:
            meaning = f"{tag_source} 中 cbttype 有值的 Base 玩家。"
        elif "新" in label and "玩家" in label:
            meaning = f"{tag_source} 无记录，或 cbttype 为空/NULL 的 Base 玩家。"
        else:
            meaning = "按 SQL 中对应条件划分的 Base 玩家。"
        definitions.append({"label": label, "meaning": meaning})
    return definitions


def product_retention_windows(sql: str) -> list[dict[str, str]]:
    windows: list[dict[str, str]] = []
    for label, offset, day_name in [
        ("1d", "1", "次留"),
        ("3d", "2", "三留"),
        ("7d", "6", "七留"),
    ]:
        if re.search(rf"ret(?:ention)?_{label}", sql, flags=re.I) or re.search(
            rf"cohort_date\s*\+\s*interval\s+{offset}\s+day",
            sql,
            flags=re.I,
        ):
            windows.append(
                {
                    "label": label,
                    "name": day_name,
                    "offset": offset,
                    "meaning": f"{day_name}：cohort_date + {offset} 天仍活跃的玩家。",
                }
            )
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in windows:
        if item["label"] in seen:
            continue
        seen.add(item["label"])
        deduped.append(item)
    return deduped


def product_has_login_logout_active(sql: str) -> bool:
    lower = sql.lower()
    return "playerlogin" in lower and "playerlogout" in lower and "active_days" in lower


def product_metric_base_for_review(concepts: ProductConcepts, fallback: str) -> str:
    return concepts.base or fallback or "当前查询的 Base"


def product_metric_overrides(review: FileReview, concepts: ProductConcepts) -> dict[str, dict]:
    base = product_metric_base_for_review(concepts, "")
    has_retention = any(normalize_identifier(metric).startswith("retention_") for metric in review.metrics)
    bucket_scope = "同一日期、同一人群下所有首日进度分桶"
    if "is_summary" in review.sql.lower() and "summary_output" in review.sql.lower():
        bucket_scope += "；汇总行则按同一人群的全部日期累计"
    overrides: dict[str, dict] = {}
    if any(normalize_identifier(metric) == "new_users" for metric in review.metrics):
        overrides["new_users"] = {
            "metric": "新增用户数",
            "business_definition": "当前结果行对应日期、人群、首日进度分桶内的窗口新增玩家数。",
            "base": base,
            "numerator": "落在当前日期 × 人群 × 首日进度分桶内的 Base 玩家，按玩家去重。",
            "denominator": "不适用（这是人数指标）。",
            "calculation": "对当前分组内玩家去重计数。",
            "how_to_review": "先确认 Base 是否真的是窗口新增玩家，再确认人群和首日进度分桶是否互斥且完整。",
            "pass_criteria": "每个玩家在同一人群下只落入一个首日进度分桶；新增人数与结果样例量级一致。",
            "confidence": "high",
        }
    if any(normalize_identifier(metric) == "progress_share" for metric in review.metrics):
        overrides["progress_share"] = {
            "metric": "首日进度占比",
            "business_definition": "当前首日进度分桶在对应人群中的新增用户占比。",
            "base": base,
            "numerator": "当前首日进度分桶的新增用户数。",
            "denominator": f"{bucket_scope}的新增用户数总和。",
            "calculation": "当前分桶新增用户数 / 同口径人群内全部分桶新增用户数。",
            "how_to_review": "重点确认分母不是全表总人数，而是同日期、同人群内的所有首日进度分桶总人数；汇总行按人群累计。",
            "pass_criteria": "同一日期、同一人群下所有首日进度占比加总应接近 100%；汇总行同理按人群加总。",
            "confidence": "high",
        }
    if has_retention:
        active_definition = "PlayerLogin【玩家登录】或 PlayerLogout【玩家登出】任一命中即视为活跃" if product_has_login_logout_active(review.sql) else "后续窗口内再次活跃"
        retention_specs = {
            "retention_1d_users": ("次留人数", "cohort_date + 1 天", "次留"),
            "retention_1d_rate": ("次留率", "cohort_date + 1 天", "次留"),
            "retention_3d_users": ("三留人数", "cohort_date + 2 天", "三留"),
            "retention_3d_rate": ("三留率", "cohort_date + 2 天", "三留"),
        }
        for alias, (display, offset_text, label) in retention_specs.items():
            if not any(normalize_identifier(metric) == alias for metric in review.metrics):
                continue
            is_rate = alias.endswith("_rate")
            overrides[alias] = {
                "metric": display,
                "business_definition": f"{label}：当前分组 Base 玩家在 {offset_text} 仍活跃的{'比例' if is_rate else '人数'}。",
                "base": base,
                "numerator": f"当前分组内在 {offset_text} 仍活跃的玩家；活跃口径为{active_definition}。",
                "denominator": "可计算该留存窗口的当前分组新增用户数。" if is_rate else "不适用（这是人数指标；对应留存率分母为可计算 cohort 的新增用户数）。",
                "calculation": f"{label}人数 / 可计算 {label} 的新增用户数。" if is_rate else f"对满足 {offset_text} 活跃的玩家求和/计数。",
                "how_to_review": "先确认留存窗口偏移是否符合需求，再确认活跃日志、可计算日期剔除规则和分母 cohort 是否一致。",
                "pass_criteria": f"{label}分子只来自同一 Base 的后续活跃玩家；不可计算日期不应进入留存率分母。",
                "confidence": "high",
            }
    return overrides


def product_one_sentence(review: FileReview, concepts: ProductConcepts, fallback: str) -> str:
    dimensions = {normalize_identifier(item) for item in review.dimensions}
    metrics = {normalize_identifier(item) for item in review.metrics}
    if {"crowd_type", "first_day_progress"}.issubset(dimensions) and (
        "progress_share" in metrics or any(item.startswith("retention_") for item in metrics)
    ):
        return "统计窗口新增玩家按人群和首日进度分桶拆分后的新增人数、进度占比、次留/三留。"
    if concepts.base and review.metrics:
        metric_names = "、".join(human_metric_display_name(metric) for metric in review.metrics[:6])
        return f"围绕「{concepts.base}」输出 {metric_names}。"
    return fallback


def product_parse_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def product_date_plus(value: str, days: int) -> str:
    parsed = product_parse_date(value)
    if not parsed:
        return ""
    return (parsed + timedelta(days=days)).strftime("%Y-%m-%d")


def product_section(
    title: str,
    paragraphs: list[str] | None = None,
    headers: list[str] | None = None,
    rows: list[list[str]] | None = None,
    bullets: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "paragraphs": [clip_text(item, 900) for item in (paragraphs or []) if item],
        "table": {
            "headers": headers or [],
            "rows": [[clip_text(cell, 500) for cell in row] for row in (rows or [])],
        },
        "bullets": [clip_text(item, 700) for item in (bullets or []) if item],
    }


def top_comment_outline(sql: str, catalog: dict[str, str] | None = None) -> list[dict[str, Any]]:
    outline: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in top_comment_lines(sql):
        text = clean_human_business_text(strip_comment_numbering(raw_line), catalog).strip()
        if not text or re.fullmatch(r"[=\-_*<>◀▶\s]+", text):
            continue
        label, description = split_comment_definition(text)
        if label and description:
            current = {
                "label": clean_human_business_text(label, catalog),
                "description": clean_human_business_text(description, catalog),
                "bullets": [],
            }
            outline.append(current)
            continue
        heading = re.match(r"^([A-Za-z0-9_\u4e00-\u9fff /（）()&×+\-]{1,60})[：:]\s*$", text)
        if heading:
            current = {
                "label": clean_human_business_text(heading.group(1), catalog),
                "description": "",
                "bullets": [],
            }
            outline.append(current)
            continue
        if current is not None:
            current.setdefault("bullets", []).append(clean_human_business_text(text, catalog))
        else:
            outline.append({"label": "", "description": text, "bullets": []})
    return [
        item
        for item in outline
        if item.get("description") or item.get("bullets") or item.get("label") in {"指标", "标题"}
    ][:40]


def comment_outline_text(item: dict[str, Any]) -> str:
    pieces = []
    label = str(item.get("label") or "").strip()
    description = str(item.get("description") or "").strip()
    if label and description:
        pieces.append(f"{label}：{description}")
    elif description:
        pieces.append(description)
    elif label:
        pieces.append(label)
    pieces.extend(str(value).strip() for value in item.get("bullets", []) or [] if str(value).strip())
    return "；".join(pieces)


def comment_outline_find(outline: list[dict[str, Any]], *keywords: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in outline:
        text = comment_outline_text(item)
        if any(keyword and keyword in text for keyword in keywords):
            result.append(item)
    return result


def comment_outline_first_by_label(outline: list[dict[str, Any]], *labels: str) -> dict[str, Any] | None:
    wanted = {normalize_business_key(label) for label in labels if label}
    for item in outline:
        label = normalize_business_key(str(item.get("label") or ""))
        if label in wanted:
            return item
    for item in outline:
        label = str(item.get("label") or "")
        if any(raw and raw in label for raw in labels):
            return item
    return None


def summarize_comment_outline_for_logic(outline: list[dict[str, Any]]) -> list[str]:
    skip_labels = {"指标", "标题", "目标平台", "平台", "库", "数据库", "说明", "备注"}
    rows: list[str] = []
    for item in outline:
        label = str(item.get("label") or "").strip()
        if label in skip_labels:
            continue
        text = comment_outline_text(item)
        if not text:
            continue
        if any(token in text for token in ["时间", "窗口", "模式", "GameMode", "区服", "正式服", "有效结算", "观察期"]):
            rows.append(text)
    return unique_in_order(rows)[:10]


def product_humanize_filter_text(value: str, catalog: dict[str, str] | None = None) -> str:
    text = clean_human_business_text(strip_product_sql_aliases(value), catalog)
    text = re.sub(r"\bGameMode\s+IN\s*\(([^)]+)\)", lambda m: "GameMode 属于 " + m.group(1).replace(",", "、"), text, flags=re.I)
    text = re.sub(r"\biZoneAreaID\s*=\s*'?([0-9]+)'?", r"只看区服 \1", text, flags=re.I)
    for field in sorted(PRODUCT_FIELD_CHINESE_LABELS, key=len, reverse=True):
        display = product_field_with_chinese(field)
        text = re.sub(rf"\b{re.escape(product_field_display_name(field))}\b(?!（)", display, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def business_scope_from_filters(review: FileReview, catalog: dict[str, str] | None = None) -> list[str]:
    rows: list[str] = []
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in review.business_filters:
        if item.get("scope") not in {"base_filter", "metric_filter"}:
            continue
        label = clean_human_business_text(item.get("label") or item.get("field") or "筛选", catalog)
        effect = product_humanize_filter_text(item.get("business_effect") or item.get("condition") or "", catalog)
        if not effect:
            continue
        grouped[(str(item.get("kind") or ""), label)].append(effect)
    for (_, label), effects in grouped.items():
        merged = "；".join(unique_in_order(effects)[:4])
        if merged:
            rows.append(f"{label}：{merged}")
    return unique_in_order(rows)[:10]


def logic_review_title(review: FileReview, outline: list[dict[str, Any]], context: MetricBusinessContext) -> str:
    for item in outline:
        label = str(item.get("label") or "")
        if label in {"指标", "标题"} and item.get("description"):
            return str(item.get("description"))
    return context.title or review.path.stem


def sql_has_duration_logic(sql: str, context: MetricBusinessContext) -> bool:
    lowered = sql.lower()
    comment_text = " ".join(context.comment_lines)
    return bool(
        context.duration_logic
        or "totalactiveduration" in lowered
        or "onlinetime" in lowered
        or "matchduration" in lowered
        or any(token in comment_text for token in ["时长", "非挂机", "在线时长", "耗时"])
    )


def source_logs_for_patterns(review: FileReview, patterns: list[str], catalog: dict[str, str] | None = None) -> list[str]:
    result: list[str] = []
    for table in review.tables:
        normalized = normalize_identifier(log_label_from_table(table)).replace("_", "")
        if any(pattern in normalized for pattern in patterns):
            result.append(display_log_name(table, catalog))
    return unique_in_order(result)


def register_result_condition(sql: str, outline: list[dict[str, Any]]) -> str:
    text = strip_sql_comments(sql)
    comment_text = " ".join(comment_outline_text(item) for item in outline)
    match = re.search(r"\bRegisterResult\b\s*=\s*'?(\d+)'?", text, flags=re.I)
    if match:
        value = match.group(1)
        return f"CampaignRegisterResult.RegisterResult（报名结果）= {value} 的报名记录计入。"
    if "不限 RegisterResult" in comment_text or "成功/失败/取消都算" in comment_text:
        return "CampaignRegisterResult 中出现报名队伍即算参与报名；不限 RegisterResult（报名结果），成功/失败/取消都计入。"
    return "CampaignRegisterResult 中出现报名记录即作为报名事件；SQL 未识别到 RegisterResult（报名结果）成功条件。"


def field_present(sql: str, field: str) -> bool:
    return bool(re.search(rf"\b{re.escape(field)}\b", sql, flags=re.I))


def present_field_labels(sql: str, fields: list[str]) -> list[str]:
    return product_field_list_with_chinese([field for field in fields if field_present(sql, field)])


def infer_sql_event_contracts(review: FileReview, outline: list[dict[str, Any]], context: MetricBusinessContext, catalog: dict[str, str] | None = None) -> list[dict[str, Any]]:
    sql = review.sql
    lowered = sql.lower()
    comment_text = " ".join(comment_outline_text(item) for item in outline)
    contracts: list[dict[str, Any]] = []
    has_registration = "campaignregisterresult" in lowered
    if has_registration:
        success = bool(re.search(r"\bregisterresult\b\s*=\s*'?1'?", lowered) or "成功报名" in comment_text)
        event_name = "成功报名判定" if success else "参与报名判定"
        source_logs = source_logs_for_patterns(review, ["campaignregisterresult", "battleloginout"], catalog)
        first_rule = ""
        if re.search(r"first_(?:success_)?register_ts|min\s*\(", lowered, flags=re.I) or "首次" in comment_text:
            first_rule = "多次报名时按 SQL 中的 MIN/first_register_ts 取首次报名时点。"
        contracts.append(
            {
                "event_id": f"E{len(contracts) + 1}",
                "event_name": event_name,
                "event_family": "报名/夺榜",
                "source_logs_or_tables": source_logs or ["CampaignRegisterResult", "BattleLogInOut"],
                "event_condition": register_result_condition(sql, outline),
                "id_or_mapping": (
                    "CampaignRegisterResult.BattleSrvId（战斗服ID）+ TeamId（队伍ID）回连 BattleLogInOut.BattleSrvId（战斗服ID）+ BattleTeamId（战斗内队伍ID）。"
                    if "teamid" in lowered and "battleteamid" in lowered
                    else "按 SQL 中的 BattleSrvId/队伍或玩家字段关联报名记录。"
                ),
                "statistic_object": (
                    "CampaignRegisterResult 是队伍粒度；回挂后按 RoleID（角色ID）/vOpenID（玩家OpenID）去重统计玩家。"
                    if "teamid" in lowered
                    else "按 SQL 输出指标对应的队伍或玩家粒度去重。"
                ),
                "first_or_final_rule": first_rule,
                "join_or_backfill_rule": (
                    "用报名队伍回溯得到队伍成员，再限制到 BattleLogInOut 的模式、区服、时间范围。"
                    if "battleloginout" in lowered
                    else ""
                ),
                "source_fields": present_field_labels(sql, ["RegisterResult", "BattleSrvId", "TeamId", "BattleTeamId", "RoleID", "vOpenID", "TeamNum"]),
                "product_interpretation": "这张 SQL 的报名类指标先判定报名队伍，再根据需要回挂到玩家或队伍统计对象。",
                "business_risk": "重点核对是否要“不限报名结果”还是只要成功报名，以及 TeamId 回挂是否会漏队员或串战斗服。",
                "sql_evidence_refs": ["顶部口径注释", "CampaignRegisterResult", "BattleLogInOut JOIN"],
                "confidence": "high" if comment_outline_find(outline, "报名", "RegisterResult") else "medium",
            }
        )
    if "campaignsettlement" in lowered:
        contracts.append(
            {
                "event_id": f"E{len(contracts) + 1}",
                "event_name": "已结算战斗服/结算队伍判定",
                "event_family": "结算",
                "source_logs_or_tables": source_logs_for_patterns(review, ["campaignsettlement", "battleloginout"], catalog) or ["CampaignSettlement"],
                "event_condition": "CampaignSettlement 中出现有效结算记录的 BattleSrvId（战斗服ID）/TeamId（队伍ID）；常见有效条件为 Id（记录/阶段ID）<> 0。",
                "id_or_mapping": "BattleSrvId（战斗服ID）对齐战斗服；需要模式时通过 BattleLogInOut 的 BattleSrvId（战斗服ID）回推主 GameMode（玩法模式）。",
                "statistic_object": "战斗服或结算队伍，按 SQL 的 BattleSrvId（战斗服ID）/TeamId（队伍ID）/Id（记录/阶段ID）粒度去重或聚合。",
                "first_or_final_rule": "若 SQL 计算 open_ts/absolute_day，则以 BattleLogInOut.dtEventTime（事件时间）首次上报时间作为战斗服开服日。",
                "join_or_backfill_rule": "结算表无 GameMode（玩法模式）时，按 SQL 的关联键回连模式来源；任何展示层模式合并都必须有 SQL 或显式规则证据。",
                "source_fields": present_field_labels(sql, ["BattleSrvId", "TeamId", "Id", "TeamGold", "dtEventTime", "GameMode"]),
                "product_interpretation": "只在有结算的夺榜战斗服内统计后续报名、空投或金币指标。",
                "business_risk": "重点核对结算成立条件、主 GameMode 推断和展示层模式合并是否都有明确证据。",
                "sql_evidence_refs": ["顶部口径注释", "CampaignSettlement", "BattleLogInOut 模式回推"],
                "confidence": "high" if comment_outline_find(outline, "结算", "有效结算") else "medium",
            }
        )
    if "battleitemchangesource" in lowered and "airdrop" in lowered:
        contracts.append(
            {
                "event_id": f"E{len(contracts) + 1}",
                "event_name": "获取空投箱内物品判定",
                "event_family": "空投",
                "source_logs_or_tables": source_logs_for_patterns(review, ["battleitem"], catalog) or ["BattleItem"],
                "event_condition": "BattleItem.BattleItemChangeSource（道具变化来源）= 'AirDrop'；通常只看拾取/新获得，不把流转计入。",
                "id_or_mapping": "按 BattleSrvId（战斗服ID）、BattleTeamId（战斗内队伍ID）/TeamId（队伍ID）和玩家字段归属到战斗服、队伍或报名玩家。",
                "statistic_object": "按 SQL 指标统计玩家、队伍或物品数量；人数类通常按 RoleID（角色ID）/vOpenID（玩家OpenID）去重。",
                "first_or_final_rule": "",
                "join_or_backfill_rule": "空投拾取事件按 BattleSrvId/TeamId 与结算战斗服、报名队伍或模式归属表对齐。",
                "source_fields": present_field_labels(sql, ["BattleItemChangeSource", "BattleItemDelta", "BattleSrvId", "BattleTeamId", "RoleID"]),
                "product_interpretation": "这里的空投获取不是报名本身，而是报名/结算范围内玩家或队伍实际拿到空投物品。",
                "business_risk": "重点核对 BattleItemDelta 正负号、是否只统计新增拾取，以及是否限制在报名玩家/队伍内。",
                "sql_evidence_refs": ["BattleItemChangeSource = 'AirDrop'"],
                "confidence": "high",
            }
        )
    if "goldairdrop" in lowered or "airdroptype" in lowered:
        contracts.append(
            {
                "event_id": f"E{len(contracts) + 1}",
                "event_name": "空投箱投放判定",
                "event_family": "空投",
                "source_logs_or_tables": source_logs_for_patterns(review, ["goldairdrop"], catalog) or ["GoldAirdrop"],
                "event_condition": "使用 GoldAirdrop；若存在 AirdropType（空投类型）= 1，则只统计对应空投箱投放/生成记录。",
                "id_or_mapping": "按 BattleSrvId（战斗服ID）和 GoldCoinAirDropBoxIds（空投箱ID）/投放时间归属到战斗服绝对天数。",
                "statistic_object": "空投箱 ID 或投放记录；按 SQL 中 GoldCoinAirDropBoxIds（空投箱ID）/Id（记录/阶段ID）口径计数。",
                "first_or_final_rule": "",
                "join_or_backfill_rule": "通过 BattleSrvId 与战斗服开服日、主模式和结算服范围对齐。",
                "source_fields": present_field_labels(sql, ["AirdropType", "GoldCoinAirDropBoxIds", "BattleSrvId", "dtEventTime"]),
                "product_interpretation": "这里统计空投箱投放供给侧数量，不是玩家拾取数量。",
                "business_risk": "重点核对 GoldCoinAirDropBoxIds 是否一行一个箱 ID，或需要拆分数组/列表。",
                "sql_evidence_refs": ["GoldAirdrop / AirdropType"],
                "confidence": "medium",
            }
        )
    if any(token in comment_text for token in ["金币", "领地柜", "系统回收", "马太"]) or any(token in lowered for token in ["teamgold", "battleitemdelta", "territoyconstructionid"]):
        contracts.append(
            {
                "event_id": f"E{len(contracts) + 1}",
                "event_name": "金币获得/结算/放入领地柜判定",
                "event_family": "金币",
                "source_logs_or_tables": source_logs_for_patterns(review, ["campaignsettlement", "campaignregisterresult", "battleitem", "goldairdrop"], catalog),
                "event_condition": "按 SQL 中 TeamGold（队伍结算金币）、stageGold（报名阶段发放金币）、BattleItemDelta（道具/金币变化量）、领地柜/回收相关条件识别金币生成、结算或沉淀。",
                "id_or_mapping": "通常按 BattleSrvId（战斗服ID）+ TeamId（队伍ID）+ 绝对天数对齐报名、结算、空投和领地柜事件。",
                "statistic_object": "队伍金币、金币数量或队伍/玩家渗透；数量类重点看 SUM/ABS(BattleItemDelta（道具/金币变化量）)，队伍类看 TeamId（队伍ID）去重。",
                "first_or_final_rule": "结算类指标以 CampaignSettlement.TeamGold（队伍结算金币）为结算结果；领地柜类指标看 dtEventTime（事件时间）是否在报名/抢夺/结算窗口内。",
                "join_or_backfill_rule": "跨日志金币口径需要 BattleSrvId（战斗服ID）、TeamId（队伍ID）、报名阶段/绝对天数保持一致。",
                "source_fields": present_field_labels(sql, ["TeamGold", "stageGold", "BattleItemDelta", "TerritoyConstructionID", "TeamId", "BattleSrvId"]),
                "product_interpretation": "金币类 SQL 的核心不是单一日志，而是生成、结算、放入领地柜或回收之间的口径对齐。",
                "business_risk": "重点核对金币正负号、ABS 使用、时间先后关系和 TeamId/BattleSrvId 是否同粒度。",
                "sql_evidence_refs": ["顶部金币口径注释", "金币相关字段"],
                "confidence": "medium",
            }
        )
    if sql_has_duration_logic(sql, context):
        contracts.append(
            {
                "event_id": f"E{len(contracts) + 1}",
                "event_name": "累计非挂机时长计算",
                "event_family": "时长",
                "source_logs_or_tables": source_logs_for_patterns(review, ["battleloginout"], catalog) or ["BattleLogInOut"],
                "event_condition": "使用 BattleLogInOut.TotalActiveDuration（累计非挂机时长）/OnlineTime（在线时长）/MatchDuration（匹配耗时）等时长字段，按 SQL 声明的时点和范围截断。",
                "id_or_mapping": "常见粒度为 RoleID（角色ID）+ BattleSrvId（战斗服ID）；报名时长分布会限制在首次报名时间之前及当时。",
                "statistic_object": "玩家累计非挂机时长；通常先在 RoleID（角色ID）+ BattleSrvId（战斗服ID）粒度取 MAX，再跨战斗服汇总。",
                "first_or_final_rule": context.duration_logic or "按 SQL 中的 MAX/SUM/分桶表达式计算时长。",
                "join_or_backfill_rule": "时长分桶再回到报名玩家、成功报名玩家或未成功报名玩家集合统计人数占比。",
                "source_fields": present_field_labels(sql, ["TotalActiveDuration", "OnlineTime", "MatchDuration", "RoleID", "BattleSrvId"]),
                "product_interpretation": "时长是分桶或归因变量，不应覆盖报名/空投/金币事件本身的判定。",
                "business_risk": "重点核对累计字段是否先取 MAX，是否按报名时点截断，以及未算出时长的玩家是否归 0 桶。",
                "sql_evidence_refs": ["时长字段/顶部时长口径"],
                "confidence": "high" if context.duration_logic else "medium",
            }
        )
    return contracts


def final_metric_expression_map(sql: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for alias, expression, expression_without_alias in expanded_final_select_items(sql):
        key = alias or strip_wrapping_parens(expression_without_alias or expression).strip("`'\" ")
        if key:
            result[key] = compact(expression_without_alias or expression)
    return result


def logic_metric_comment_for_alias(alias: str, outline: list[dict[str, Any]]) -> str:
    normalized_alias = normalize_business_key(alias)
    candidates: list[str] = []
    for item in outline:
        text = comment_outline_text(item)
        if not text:
            continue
        label = str(item.get("label") or "")
        normalized_label = normalize_business_key(label)
        if normalized_label in {normalized_alias, normalize_business_key(metric_subject_from_alias(alias))}:
            candidates.append(text)
            continue
        if any(token in alias for token in ["玩家数", "人数", "队伍数", "数量"]) and any(
            token in label for token in ["率", "占比", "比例", "渗透率"]
        ):
            continue
        if any(token in alias for token in ["率", "占比", "比例"]) and label in {"指标", "标题"}:
            continue
        if alias and alias in text:
            candidates.append(text)
            continue
    return candidates[0] if candidates else ""


def infer_metric_role(alias: str, expression: str, outline: list[dict[str, Any]]) -> tuple[str, str, str]:
    alias_text = alias or ""
    expr_lower = expression.lower()
    denominator_item = comment_outline_first_by_label(outline, "分母")
    numerator_item = comment_outline_first_by_label(outline, "分子")
    rate_item = comment_outline_first_by_label(outline, "渗透率", "占比", "比例", "率")
    denominator_text = comment_outline_text(denominator_item) if denominator_item else ""
    numerator_text = comment_outline_text(numerator_item) if numerator_item else ""
    rate_text = comment_outline_text(rate_item) if rate_item else ""
    if any(token in alias_text for token in ["总玩家数", "总用户数", "分母", "常规模式玩家数"]):
        denominator = "不适用（这是分母人数本身）。"
        numerator = denominator_text or "观察范围内 Base 去重对象。"
        formula = "COUNT(DISTINCT ...) 或对应 Base 计数。"
        return numerator, denominator, formula
    if any(token in alias_text for token in ["渗透率", "占比", "比例", "率"]):
        numerator = numerator_text or "满足目标事件/条件的去重对象。"
        denominator = denominator_text or "对应 Base 总量。"
        formula = rate_text or strip_product_sql_aliases(expression)
        return numerator, denominator, formula
    if any(token in alias_text for token in ["报名玩家数", "报名人数", "获取", "拾取", "投放", "金币", "前5%", "队伍数"]):
        numerator = numerator_text or logic_metric_comment_for_alias(alias_text, outline) or "满足该事件定义的统计对象。"
        return numerator, "不适用（数量/金额指标）。", strip_product_sql_aliases(expression)
    if "count(distinct" in expr_lower:
        return "按表达式中的 DISTINCT 对象去重计数。", "不适用（数量指标）。", strip_product_sql_aliases(expression)
    if re.search(r"\bsum\s*\(", expr_lower):
        return "对表达式对应的数量/金额求和。", "不适用（求和指标）。", strip_product_sql_aliases(expression)
    if re.search(r"\bavg\s*\(", expr_lower):
        return "对表达式对应的值求平均。", "参与平均的记录/战斗服/队伍。", strip_product_sql_aliases(expression)
    return logic_metric_comment_for_alias(alias_text, outline) or "按最终 SELECT 表达式计算。", "", strip_product_sql_aliases(expression)


def infer_logic_metric_rows(review: FileReview, outline: list[dict[str, Any]], metric_review: dict, catalog: dict[str, str] | None = None) -> list[dict[str, Any]]:
    expression_map = final_metric_expression_map(review.sql)
    metric_lookup = {normalize_identifier(item.get("metric", "")): item for item in metric_review.get("metric_cards", [])}
    rows: list[dict[str, Any]] = []
    for alias, expression in expression_map.items():
        normalized = normalize_identifier(alias)
        if alias in review.dimensions and normalized not in metric_lookup:
            continue
        if normalized not in metric_lookup and not is_review_metric_with_definitions(expression, alias, alias_definitions(review.sql)):
            continue
        numerator, denominator, formula = infer_metric_role(alias, expression, outline)
        metric_card = metric_lookup.get(normalized, {})
        use_static = bool(metric_card.get("description_source") in {"sql_comment", "cte_comment"})
        rows.append(
            {
                "metric_name": alias,
                "business_meaning": clean_human_business_text(
                    logic_metric_comment_for_alias(alias, outline)
                    or metric_card.get("business_definition", "")
                    or product_output_field_meaning(alias, metric_business_context(review.sql), review.sql)[1],
                    catalog,
                ),
                "numerator": clean_human_business_text((metric_card.get("numerator") if use_static else "") or numerator, catalog),
                "denominator": clean_human_business_text((metric_card.get("denominator") if use_static else "") or denominator, catalog),
                "formula": product_humanize_filter_text((metric_card.get("calculation") if use_static else "") or formula, catalog),
                "dedup_key": logic_dedup_key_for_expression(expression, review.sql),
                "grain": grouping_summary(review),
                "sql_expression": clip_text(strip_product_sql_aliases(expression), 300),
            }
        )
    return rows[:16]


def logic_dedup_key_for_expression(expression: str, sql: str) -> str:
    keys = [
        product_field_display_name(match.group(1))
        for match in re.finditer(r"count\s*\(\s*distinct\s+`?(?:[A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)`?", expression, flags=re.I)
    ]
    if keys:
        return "、".join(unique_in_order(keys))
    lowered = sql.lower()
    if "roleid" in lowered:
        return "RoleID（SQL 中常见玩家去重键）"
    if "vopenid" in lowered:
        return "vOpenID（SQL 中常见玩家去重键）"
    if "teamid" in lowered:
        return "TeamId/BattleTeamId（SQL 中常见队伍键）"
    return "按当前结果粒度聚合"


def build_sql_logic_review(
    review: FileReview,
    metric_review: dict,
    context: MetricBusinessContext,
    catalog: dict[str, str] | None = None,
) -> dict[str, Any]:
    outline = top_comment_outline(review.sql, catalog)
    event_contracts = infer_sql_event_contracts(review, outline, context, catalog)
    metric_rows = infer_logic_metric_rows(review, outline, metric_review, catalog)
    title = logic_review_title(review, outline, context)
    key_steps = [
        comment_outline_text(item)
        for item in outline
        if str(item.get("label") or "") not in {"指标", "标题", "目标平台"}
        and any(token in comment_outline_text(item) for token in ["Base", "分母", "分子", "成功报名", "参与报名", "空投", "金币", "结算", "时长", "渗透率", "占比", "比例"])
    ]
    if not key_steps:
        key_steps = business_logic_steps(review, metric_review, context, catalog)
    return {
        "title": title,
        "summary": product_one_sentence(review, ProductConcepts(conclusion=title), metric_review.get("summary", "")),
        "scope": summarize_comment_outline_for_logic(outline) or business_scope_from_filters(review, catalog) or business_scope_summary(review, catalog),
        "key_steps": unique_in_order(key_steps)[:14],
        "metrics": metric_rows,
        "events": event_contracts,
        "comment_outline": [
            {
                "label": item.get("label", ""),
                "description": item.get("description", ""),
                "bullets": item.get("bullets", []),
            }
            for item in outline[:24]
        ],
    }


def logic_events_as_contracts(logic_review: dict[str, Any]) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for index, event in enumerate(logic_review.get("events") or [], 1):
        if not isinstance(event, dict):
            continue
        item = dict(event)
        item.setdefault("event_id", f"E{index}")
        item.setdefault("event_name", f"事件{index}")
        item.setdefault("event_family", "")
        item.setdefault("source_logs_or_tables", [])
        item.setdefault("event_condition", "")
        item.setdefault("id_or_mapping", "")
        item.setdefault("statistic_object", "")
        item.setdefault("first_or_final_rule", "")
        item.setdefault("join_or_backfill_rule", "")
        item.setdefault("source_fields", [])
        item.setdefault("product_interpretation", "")
        item.setdefault("business_risk", "")
        item.setdefault("sql_evidence_refs", [])
        item.setdefault("confidence", "medium")
        contracts.append(item)
    return contracts


def logic_events_as_index(logic_review: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in logic_events_as_contracts(logic_review):
        rows.append(
            {
                "event_id": event.get("event_id", ""),
                "event_name": event.get("event_name", ""),
                "source_logs_or_tables": event.get("source_logs_or_tables", []),
                "event_condition": event.get("event_condition", ""),
                "statistic_object": event.get("statistic_object", ""),
                "source_fields": event.get("source_fields", []),
                "risk_summary": event.get("business_risk", ""),
                "confidence": event.get("confidence", ""),
            }
        )
    return rows


def logic_metrics_as_summary(logic_review: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    event_refs = [event.get("event_id") for event in logic_review.get("events", []) if isinstance(event, dict) and event.get("event_id")]
    for metric in logic_review.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        formula = str(metric.get("formula") or "")
        metric_name = str(metric.get("metric_name") or "")
        metric_type = "比率指标" if any(token in metric_name + formula for token in ["率", "占比", "比例", "/"]) else "数量/金额指标"
        rows.append(
            {
                "metric_name": metric_name,
                "business_meaning": metric.get("business_meaning", ""),
                "metric_type": metric_type,
                "calculation": formula,
                "key_conditions": logic_review.get("scope", [])[:6],
                "numerator": metric.get("numerator", ""),
                "denominator": metric.get("denominator", ""),
                "dedup_key": metric.get("dedup_key", ""),
                "grain": metric.get("grain", ""),
                "event_refs": event_refs,
                "risk_refs": [],
                "confidence": "high",
                "review_status": "按 SQL 注释/最终 SELECT 自动拆解",
            }
        )
    return rows


def logic_metrics_as_cards(logic_review: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    event_refs = [event.get("event_id") for event in logic_review.get("events", []) if isinstance(event, dict) and event.get("event_id")]
    event_sources = unique_in_order(
        source
        for event in logic_review.get("events", [])
        if isinstance(event, dict)
        for source in event.get("source_logs_or_tables", []) or []
    )
    event_fields = unique_in_order(
        field
        for event in logic_review.get("events", [])
        if isinstance(event, dict)
        for field in event.get("source_fields", []) or []
    )
    for metric in logic_review.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        formula = str(metric.get("formula") or "")
        metric_name = str(metric.get("metric_name") or "")
        metric_type = "比率指标" if any(token in metric_name + formula for token in ["率", "占比", "比例", "/"]) else "数量/金额指标"
        cards.append(
            {
                "metric_name": metric_name,
                "business_meaning": metric.get("business_meaning", ""),
                "metric_type": metric_type,
                "calculation": formula,
                "key_conditions": logic_review.get("scope", [])[:8],
                "numerator": metric.get("numerator", ""),
                "denominator": metric.get("denominator", ""),
                "dedup_key": metric.get("dedup_key", ""),
                "aggregation_dimensions": [] if "整体汇总" in str(metric.get("grain", "")) else [metric.get("grain", "")],
                "row_grain_explanation": metric.get("grain", ""),
                "event_refs": event_refs,
                "risk_refs": [],
                "risk_notes": [],
                "source_logs_fields": [
                    {
                        "role": "关键字段",
                        "source_logs_or_tables": event_sources,
                        "field_expression": "、".join(event_fields) or "见事件/行为判定",
                        "business_story": "字段名保留英文原名并补中文含义，用于和 SQL 对账。",
                        "group_by": [],
                    }
                ],
                "metric_filters": [],
                "metric_confirmations": [],
                "standard_rule_alignment": "本卡按 SQL 顶部注释、事件判定和最终 SELECT 自动拆解；已保存规则冲突另见风险/动作区。",
                "sql_evidence_refs": ["SQL 逻辑拆解", "顶部口径注释", "最终 SELECT"],
                "confidence": "high",
            }
        )
    return cards


def logic_review_story_cards(logic_review: dict[str, Any]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    if logic_review.get("scope"):
        cards.append(
            {
                "title": "公共筛选范围",
                "body": "；".join(str(item) for item in logic_review.get("scope", [])[:8]),
                "evidence_ref": "SQL 顶部口径注释 / WHERE",
            }
        )
    if logic_review.get("key_steps"):
        cards.append(
            {
                "title": "判定路径",
                "body": "；".join(str(item) for item in logic_review.get("key_steps", [])[:8]),
                "evidence_ref": "SQL 顶部口径注释 / CTE 路径",
            }
        )
    return cards


def logic_review_path_cards(logic_review: dict[str, Any]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for metric in logic_review.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        cards.append(
            {
                "metric_name": str(metric.get("metric_name") or ""),
                "title": str(metric.get("metric_name") or ""),
                "body": str(metric.get("business_meaning") or ""),
                "formula": str(metric.get("formula") or ""),
                "base": str(metric.get("denominator") or metric.get("numerator") or ""),
                "caveat": "字段名和关键事件见 SQL 逻辑拆解。",
            }
        )
    return cards


def product_final_field_rows(review: FileReview) -> list[list[str]]:
    context = metric_business_context(review.sql)
    field_meanings = {
        "stat_date": ("统计日期", "每日行对应 cohort_date；汇总行展示为“汇总”。"),
        "cohort_date": ("首登日期", "玩家在窗口内第一次 PlayerLogin【玩家登录】的日期。"),
        "crowd_type": ("人群类型", "整体 / 老玩家 / 新玩家。"),
        "first_day_progress": ("首日进度标签", "根据首登当天 BattleLoginOut【局内登录登出】的 GameMode 命中情况划分。"),
        "new_users": ("新增用户数", "当前日期、人群、首日进度标签下的窗口新增玩家数。"),
        "progress_share": ("首日进度占比", "当前进度标签人数 / 同日期同人群总人数；汇总行为当前标签窗口总人数 / 同人群窗口总人数。"),
        "retention_1d_users": ("次留人数", "当前分组内 cohort_date + 1 天仍活跃的玩家数。"),
        "retention_1d_rate": ("次留率", "次留人数 / 可计算次留的当前分组新增用户数。"),
        "retention_3d_users": ("三留人数", "当前分组内 cohort_date + 2 天仍活跃的玩家数，也就是第 3 个自然日活跃。"),
        "retention_3d_rate": ("三留率", "三留人数 / 可计算三留的当前分组新增用户数。"),
    }
    rows: list[list[str]] = []
    for field in review.final_fields:
        normalized = normalize_identifier(field)
        if normalized in field_meanings:
            meaning, detail = field_meanings[normalized]
        else:
            meaning, detail = product_output_field_meaning(field, context, review.sql)
        rows.append([field, meaning, detail])
    return rows


def product_output_field_meaning(field: str, context: MetricBusinessContext, sql: str) -> tuple[str, str]:
    direct = business_definition_for(context, field)
    label = friendly_identifier(field)
    if direct:
        return label, clean_human_business_text(direct)

    text = field.strip()
    if "回流用户首日组队率" in text:
        return "回流用户首日组队率", "回流首日进入常规服的回流用户中，首日发生过组队行为的用户占比。"
    if "回流用户首日组队数" in text:
        return "回流首日组队用户数", "Base 用户中，回流首日在常规服内发生过组队行为的去重玩家数。"
    if "回流用户数" in text:
        return "回流用户数", "符合 Base 条件的回流玩家数；通常按玩家去重。"
    if "首日组队时长占比区间" in text:
        return "首日组队时长占比分桶", "按“首日常规模式组队时长 / 首日累计非挂机时长”划分的占比区间。"
    if "首日累计非挂机时长区间" in text:
        return "首日累计非挂机时长分桶", "按回流首日常规模式累计非挂机时长划分的时长区间。"
    if "用户占比" in text:
        if "partition by" in sql.lower() and "active_duration_bucket" in sql.lower():
            return "分桶内用户占比", "当前组队时长占比桶的人数 / 同一累计非挂机时长桶下的总人数。"
        return "用户占比", "当前分组用户数 / 对应 Base 或同层级总用户数。"

    if any(token in text for token in ["区间", "分桶", "bucket"]):
        if "时长" in text:
            return label, "用于拆分结果的时长分桶字段；重点核对单位、上下界和是否互斥。"
        if "占比" in text or "比例" in text:
            return label, "用于拆分结果的占比区间字段；重点核对分母和区间边界。"
        return label, "用于拆分结果的分桶字段；重点核对每个桶的业务含义和边界。"
    if any(token in text for token in ["率", "占比", "比例"]):
        subject = metric_subject_from_alias(text)
        return label, f"{subject}在对应 Base 或分组中的比例；重点核对分子、分母和是否同粒度。"
    if any(token in text for token in ["人数", "用户数", "玩家数", "数量"]):
        return label, "当前 Base 或分组内的数量指标；重点核对是否按玩家去重以及筛选范围。"
    if any(token in text for token in ["日期", "时间", "date"]):
        return label, "时间维度字段；重点核对统计窗口、归属日期和是否含汇总行。"
    return label, "最终输出字段；当前只能识别字段名，需结合 SQL 注释或结果样例补充业务含义。"


def product_key_filter_rows(
    review: FileReview,
    context: MetricBusinessContext,
    catalog: dict[str, str] | None = None,
) -> list[list[str]]:
    rows: list[list[str]] = []
    date_window = product_date_window(review.sql)
    lower = review.sql.lower()
    if "first_login_window" in lower and "playerlogin" in lower:
        rows.append(["首登用户", "PlayerLogin【玩家登录】", "决定 cohort / 窗口新增 Base。"])
        rows.append(["首登日期", date_window, "决定用户归属到哪个窗口内首登日。"])
    for scope in product_scope_summaries(review):
        rows.append(["服务器/大区", scope.replace("只看", ""), "限定所有相关日志的统计范围。"])
    segments = product_segment_definitions(review.sql, context, catalog)
    if segments:
        rows.append(["新老玩家", "cbttype 标签", "有值=老玩家；无记录/空/NULL=新玩家。"])
    if "battleloginout" in lower:
        rows.append(["首日战斗行为", "BattleLoginOut【局内登录登出】", "只看首登当天是否进入指定 battle gamemode。"])
    if product_has_login_logout_active(review.sql):
        active_end = product_date_plus(product_sql_date_literal(review.sql, "end_date"), 2)
        active_window = f"{product_sql_date_literal(review.sql, 'start_date')} 至 {active_end}" if active_end else "start_date 至 end_date + 2"
        rows.append(["活跃判断", "PlayerLogin【玩家登录】 UNION PlayerLogout【玩家登出】", "登录或登出任一命中即视为当天活跃。"])
        rows.append(["活跃扫描日期", active_window, "为了计算 cohort_date + 2 的三留，活跃日志会多扫 2 天。"])
    deduped: list[list[str]] = []
    seen: set[str] = set()
    for row in rows:
        key = "|".join(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def product_review_risk_points(
    review: FileReview,
    bucket_defs: list[dict[str, str]],
) -> list[str]:
    sql = review.sql
    points: list[str] = []
    lower = sql.lower()
    if "first_login_window" in lower and "min(date" in lower and "playerlogin" in lower:
        points.append("窗口新增口径：当前是“窗口内首次登录”，不是历史首次登录；如果需求是历史新增，需要向窗口前追溯历史登录。")
    if (
        "active_days" in lower
        and re.search(r"end_date\s+from\s+params\)\s*\+\s*interval\s+2\s+day", lower)
        and "can_calc_1d" in lower
        and re.search(r"cohort_date\s*\+\s*interval\s+1\s+day\s*<=\s*\(\s*select\s+end_date", lower)
    ):
        points.append("end_date 语义：活跃日志扫到 end_date + 2，但可计算日期仍用 end_date 判断，留存输出会偏保守；需要确认 end_date 是 cohort 截止日还是观察截止日。")
    label_map = {item["label"]: item["meaning"] for item in bucket_defs}
    if "B_只有常规" in label_map and "新手服" not in label_map["B_只有常规"]:
        points.append("标签命名：B_只有常规 实际是“有常规、无快速”，没有排除新手服；如果“只有”要严格排除新手服，需要额外条件。")
    if "C_只有快速" in label_map and "新手服" not in label_map["C_只有快速"]:
        points.append("标签命名：C_只有快速 实际是“有快速、无常规”，没有排除新手服；如果“只有”要严格排除新手服，需要额外条件。")
    if "E_没新手服" in label_map:
        points.append("兜底标签：E_没新手服 实际覆盖未命中常规/快速/新手服、只有其他 gamemode、或首日没有 battle 记录的玩家；名称可能需要改得更准确。")
    return unique_in_order(points)


def product_saved_rule_rows(review: FileReview) -> list[list[str]]:
    rows: list[list[str]] = []
    for check in review.rule_checks:
        if check.result in {"not_relevant"}:
            continue
        topic = check.title or check.rule_summary or check.concept_key or check.rule_id
        if not topic:
            continue
        if check.result in {"match", "matched"}:
            judgement = "已自动核对通过：当前 SQL 与已保存项目口径一致。"
        elif check.result in {"conflict", "proposed_conflict"}:
            judgement = "自动核对发现冲突：当前 SQL 与已保存项目口径不一致，需要先解决。"
        elif check.result == "needs_manual_check":
            judgement = "未自动通过：当前 SQL 命中相关口径，但静态证据不足以判断已覆盖。"
        else:
            judgement = check.message or check.result
        rows.append(
            [
                clean_human_business_text(topic),
                clean_human_business_text(check.rule_summary or check.message),
                clean_human_business_text(judgement),
            ]
        )
    return rows[:12]


def build_product_walkthrough(
    review: FileReview,
    concepts: ProductConcepts,
    context: MetricBusinessContext,
    catalog: dict[str, str] | None = None,
) -> list[dict]:
    sections: list[dict] = []
    start = product_sql_date_literal(review.sql, "start_date")
    end = product_sql_date_literal(review.sql, "end_date")
    date_window = product_date_window(review.sql)
    flag_defs = product_flag_definitions(review.sql, review.business_filter_mappings)
    bucket_defs = product_case_label_definitions(review.sql, "first_day_progress", flag_defs)
    segments = product_segment_definitions(review.sql, context, catalog)
    retention_windows = product_retention_windows(review.sql)

    if concepts.conclusion:
        sections.append(product_section("先看结论", [concepts.conclusion]))
    if review.final_fields:
        sections.append(
            product_section(
                "最终回答什么问题",
                [
                    concepts.conclusion or product_one_sentence(review, concepts, ""),
                    "最终输出粒度：" + (" + ".join(review.dimensions) if review.dimensions else "整体汇总"),
                ],
                ["指标/字段", "含义", "口径"],
                product_final_field_rows(review),
            )
        )
    if start or end:
        date_rows = [
            ["start_date", start or "未识别", "cohort / 查询窗口开始日期"],
            ["end_date", end or "未识别", "cohort / 查询窗口截止日期"],
        ]
        if end and product_has_login_logout_active(review.sql):
            date_rows.append(["活跃观察扫描", f"{start or 'start_date'} 至 {product_date_plus(end, 2) or 'end_date + 2'}", "用于计算三留所需的后续活跃。"])
        sections.append(
            product_section(
                "时间窗口口径",
                [f"当前窗口是 {date_window}，含首尾日期。"],
                ["日期项", "当前值", "作用"],
                date_rows,
            )
        )
    if concepts.base:
        sections.append(
            product_section(
                "Base / cohort 口径",
                [concepts.base],
                bullets=[
                    "cohort_date = 玩家在窗口内第一次 PlayerLogin【玩家登录】的日期。",
                    "这不是历史首次登录；窗口前已经登录过的玩家，如果窗口内再次登录，仍会被归到窗口内首登日。",
                ] if "first_login_window" in review.sql.lower() else [],
            )
        )
    if segments:
        sections.append(
            product_section(
                "新老玩家口径",
                [f"通过 {product_tag_source(context, catalog)} 区分新老玩家。"],
                ["人群", "判断逻辑"],
                [[item["label"], item["meaning"]] for item in segments],
                ["理想情况下：整体 = 老玩家 + 新玩家；如果标签表同一玩家有多条冲突记录，需要单独核对是否会重复归类。"],
            )
        )
    if flag_defs:
        sections.append(
            product_section(
                "首日战斗行为口径",
                ["只看玩家 cohort_date 当天的 BattleLoginOut【局内登录登出】记录，用于判断首日进入过哪些战斗模式。"],
                ["开关", "GameMode / 条件", "含义"],
                [[alias, item["condition"], item["label"]] for alias, item in flag_defs.items()],
            )
        )
    if bucket_defs:
        sections.append(
            product_section(
                "首日进度标签口径",
                ["每个 Base 玩家按首登当天的模式命中情况落入一个首日进度标签。"],
                ["标签", "真实含义"],
                [[item["label"], item["meaning"]] for item in bucket_defs],
            )
        )
    if retention_windows:
        active_definition = "PlayerLogin【玩家登录】或 PlayerLogout【玩家登出】任一命中即活跃" if product_has_login_logout_active(review.sql) else "后续窗口内再次活跃"
        sections.append(
            product_section(
                "留存口径",
                [active_definition],
                ["指标窗口", "含义"],
                [[item["name"], item["meaning"]] for item in retention_windows],
                ["三留在当前规则里是 cohort_date + 2 天活跃，也就是第 3 个自然日留存。"],
            )
        )
    if any(normalize_identifier(metric) in {"new_users", "progress_share"} or normalize_identifier(metric).startswith("retention_") for metric in review.metrics):
        metric_rows: list[list[str]] = []
        for metric in review.metrics:
            normalized = normalize_identifier(metric)
            override = concepts.metric_overrides.get(normalized, {})
            if not override:
                continue
            metric_rows.append([
                override.get("metric", metric),
                override.get("numerator", ""),
                override.get("denominator", ""),
                override.get("calculation", ""),
            ])
        if metric_rows:
            sections.append(
                product_section(
                    "每日和汇总指标口径",
                    ["每日行按日期、人群、首日进度标签聚合；汇总行按整段窗口累计。"],
                    ["指标", "分子", "分母", "计算"],
                    metric_rows,
                    ["汇总留存率只应使用已经跑到观察日的 cohort 进入分母；否则不要把未跑到日期当作 0 留存。"],
                )
            )
    key_filter_rows = product_key_filter_rows(review, context, catalog)
    if key_filter_rows:
        sections.append(
            product_section(
                "关键筛选条件汇总",
                [],
                ["位置", "筛选/来源", "影响"],
                key_filter_rows,
            )
        )
    saved_rule_rows = product_saved_rule_rows(review)
    if saved_rule_rows:
        sections.append(
            product_section(
                "已保存项目口径核对",
                ["这里不是展示内部 key，而是告诉审核人：当前 SQL 触达了哪些已保存口径，以及是否需要处理冲突。"],
                ["口径主题", "已保存口径摘要", "当前 SQL 判断"],
                saved_rule_rows,
            )
        )
    risk_points = product_review_risk_points(review, bucket_defs)
    if risk_points:
        sections.append(product_section("主要核对点", bullets=risk_points))
    return sections


def build_product_concepts(
    review: FileReview,
    context: MetricBusinessContext,
    catalog: dict[str, str] | None = None,
) -> ProductConcepts:
    concepts = ProductConcepts()
    date_window = product_date_window(review.sql)
    scope_lines = product_scope_summaries(review)
    lower = review.sql.lower()
    if "first_login_window" in lower and "cohort_date" in lower and "playerlogin" in lower:
        scope_tail = f"；{'；'.join(scope_lines)}" if scope_lines else ""
        concepts.base = f"在 {date_window} 内首次触发 PlayerLogin【玩家登录】 的玩家（cohort_date 为窗口内首登日）{scope_tail}。"
        concepts.logic_steps.append(f"确定 Base：{concepts.base}")
    elif context.base_description:
        scope_tail = f"；{'；'.join(scope_lines)}" if scope_lines else ""
        concepts.base = clean_human_business_text(context.base_description + scope_tail, catalog)
        concepts.logic_steps.append(f"确定 Base：{concepts.base}")
    elif "return_users" in lower and ("regular" in lower or "常规" in "".join(context.comment_lines)):
        scope_tail = f"；{'；'.join(scope_lines)}" if scope_lines else ""
        concepts.base = clean_human_business_text(
            "统计期内首次回流，且回流首日进入常规模式局内的玩家" + scope_tail,
            catalog,
        )
        concepts.logic_steps.append(f"确定 Base：{concepts.base}")
    elif scope_lines:
        concepts.base = "当前查询对象；" + "；".join(scope_lines) + "。"
    concepts.scope.extend(scope_lines)

    segments = product_segment_definitions(review.sql, context, catalog)
    if segments:
        segment_text = "；".join(f"{item['label']}={item['meaning'].rstrip('。')}" for item in segments)
        concepts.logic_steps.append(f"人群拆分：{segment_text}。")
        concepts.filter_cards.append(
            {
                "label": "人群拆分",
                "scope": "分类规则",
                "business_effect": segment_text,
                "how_to_judge": "确认整体、老玩家、新玩家是否正是需求要看的三个人群，尤其核对 cbttype 有值/为空的含义。",
                "pass_criteria": "三个人群定义能被业务方复述；整体是全量 Base，老/新玩家拆分不会重复或漏掉。",
            }
        )

    mode_mapping = review.business_filter_mappings
    flag_defs = product_flag_definitions(review.sql, mode_mapping)
    bucket_defs = product_case_label_definitions(review.sql, "first_day_progress", flag_defs)
    if bucket_defs:
        flag_text = "；".join(
            f"{item.get('label')}={item.get('condition')}"
            for item in flag_defs.values()
            if item.get("condition")
        )
        if flag_text:
            concepts.logic_steps.append(f"首日进度的模式范围：{flag_text}。")
        bucket_text = "；".join(f"{item['label']}={item['meaning']}" for item in bucket_defs)
        concepts.logic_steps.append(f"首日进度分桶：{bucket_text}。")
        concepts.filter_cards.append(
            {
                "label": "首日进度分桶",
                "scope": "指标分桶",
                "business_effect": bucket_text,
                "how_to_judge": "确认每个分桶名称、GameMode 范围和互斥优先级是否符合产品理解；兜底桶要特别确认命名。",
                "pass_criteria": "每个 Base 玩家首日只能落入一个进度桶，所有桶合起来覆盖全部 Base 玩家。",
            }
        )

    retention_windows = product_retention_windows(review.sql)
    if retention_windows:
        active_definition = "PlayerLogin【玩家登录】或 PlayerLogout【玩家登出】任一命中即活跃" if product_has_login_logout_active(review.sql) else "后续窗口内再次活跃"
        window_text = "；".join(item["meaning"] for item in retention_windows)
        concepts.logic_steps.append(f"留存判断：{active_definition}；{window_text}不可计算的日期输出 NULL 或不进入留存率分母。")
        concepts.filter_cards.append(
            {
                "label": "留存窗口",
                "scope": "指标口径",
                "business_effect": f"{active_definition}；{window_text}",
                "how_to_judge": "确认次留/三留的日期偏移、活跃定义和不可计算日期处理是否符合需求。",
                "pass_criteria": "留存分子和分母来自同一个 cohort；未跑到的日期不会被当成 0 留存。",
            }
        )

    if "summary_output" in lower or "9999-12-31" in lower:
        concepts.logic_steps.append("结果同时输出每日行和汇总行；汇总行用“汇总”展示日期，按人群和首日进度累计。")

    concepts.metric_overrides = product_metric_overrides(review, concepts)
    concepts.conclusion = product_one_sentence(review, concepts, "")
    if "first_login_window" in lower or bucket_defs or segments:
        concepts.review_checks = [
            "先确认 Base：是不是窗口内首登/新增玩家，以及 SQL 当前固定范围是否正确。",
            "再确认人群拆分：整体、老玩家、新玩家的标签表和 cbttype 规则是否符合需求。",
            "再确认首日进度分桶：GameMode ID、桶名称和兜底桶是否正确。",
            "最后确认指标：新增人数、进度占比、次留/三留的分子分母和结果样例量级是否一致。",
        ]
    elif "回流" in "".join(context.comment_lines) or "return_users" in lower:
        concepts.review_checks = [
            "先确认 Base：回流用户、回流首日、常规模式范围和去重粒度是否符合需求。",
            "再确认回流判定：活跃来源、SQL 当前沉默阈值、回溯窗口和多次回流处理是否正确。",
            "再确认归因范围：BattleSrvId/UniqueBattleID、日期和常规模式 JOIN 是否会漏算或放大。",
            "最后确认指标：组队人数、时长分桶、分子/分母和结果样例量级是否一致。",
        ]
    else:
        concepts.review_checks = [
            "先确认这份查询最终回答的业务问题和 Base。",
            "再确认分组/粒度、关键筛选和指标分子分母。",
            "最后用结果文件列名、样例值和量级核对输出是否可解释。",
        ]
    concepts.walkthrough_sections = build_product_walkthrough(review, concepts, context, catalog)
    return concepts


def top_comment_lines(sql: str) -> list[str]:
    statement_match = re.search(r"^\s*(?:with|select|insert|create)\b", sql, flags=re.I | re.M)
    prefix = sql[: statement_match.start()] if statement_match else sql[:5000]
    return extract_comment_lines(prefix)


def looks_like_cte_or_sql_trace(value: str) -> bool:
    text = compact(value)
    lower = text.lower()
    return bool(
        re.search(r"步骤\s*`[^`]+`", text)
        or "产出字段" in text
        or "partition by" in lower
        or "未识别显式 group by" in lower
        or re.search(r"\b[a-z][\w]*\.[a-z_][\w]*\b", lower)
        or re.search(r"\b(?:from|join|group by|order by|select)\b", lower)
        or "demo_log." in lower
        or "demo_warehouse." in lower
    )


def strip_physical_tables_for_humans(value: str, catalog: dict[str, str] | None = None) -> str:
    def replace_table(match: re.Match[str]) -> str:
        return display_log_name(match.group(0), catalog)

    return re.sub(
        r"\b(?:[a-zA-Z_][\w]*\.)?[a-zA-Z_][\w]*_dsl_[a-zA-Z0-9_]+?_fht0\b",
        replace_table,
        value,
    )


def enrich_bare_log_names(value: str, catalog: dict[str, str] | None = None) -> str:
    text = value
    # Long names first so LoginCGEnd is not affected by shorter fragments.
    names = sorted(set(LOG_DISPLAY_NAMES.values()), key=len, reverse=True)
    for english in names:
        normalized = normalize_identifier(english).replace("_", "")
        display = display_log_name(english, catalog)
        pattern = rf"\b{re.escape(english)}\b(?!【)"
        text = re.sub(pattern, display, text)
    return text


HUMAN_FIELD_LABELS = {
    "step_order": "步骤序号",
    "step_name": "漏斗步骤",
    "stat_date": "日期",
    "reg_date": "注册日期",
    "register_date": "注册日期",
    "active_date": "活跃日期",
    "cohort_date": "首登日期",
    "stat_date": "日期",
    "crowd_type": "人群",
    "crowd_sort": "人群排序",
    "first_day_progress": "首日进度",
    "progress_sort": "进度排序",
    "new_users": "新增用户数",
    "progress_share": "首日进度占比",
    "retention_1d_users": "次留人数",
    "retention_1d_rate": "次留率",
    "retention_3d_users": "三留人数",
    "retention_3d_rate": "三留率",
    "cbttype": "CBT 老玩家标签",
    "rank_level": "段位等级",
    "rank_log": "段位记录",
    "active_users": "活跃玩家",
    "user_latest_level": "玩家最新段位",
    "login_base": "登录 Base",
    "user_day": "用户日",
    "max_team_number": "最大队伍人数",
    "team_number": "队伍人数",
    "team_user_cnt": "组队用户数",
    "match_mode_id": "匹配模式ID",
    "match_mode_name": "匹配模式名称",
    "match_team_num": "匹配队伍人数",
    "match_battle_server_type": "匹配战斗服类型",
    "match_battle_server_type_name": "匹配战斗服类型名称",
    "is_multi_mode": "是否混合模式",
    "is_multi_mode_name": "是否混合模式名称",
    "duration_bucket": "时长区间",
    "battle_mission_stage_id": "任务阶段ID",
    "step_no": "步骤序号",
    "real_participate_users": "实际参与人数",
    "mode_id": "模式ID",
    "mode_name": "模式名称",
    "mode_category": "模式大类",
    "vOpenID": "玩家ID",
    "vopenid_da": "玩家ID",
    "gamesvrid": "游戏服/大区ID",
}


def replace_human_field_labels(value: str) -> str:
    text = value
    for raw, label in sorted(HUMAN_FIELD_LABELS.items(), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(rf"\b{re.escape(raw)}\b", label, text, flags=re.I)
    return text


def clean_human_business_text(value: str, catalog: dict[str, str] | None = None) -> str:
    text = strip_comment_numbering(compact(value))
    if not text:
        return ""
    text = text.replace("`", "")
    text = text.replace("=>", "表示")
    text = text.replace(">=", "大于等于").replace("<=", "小于等于").replace("<>", "不等于")
    text = strip_physical_tables_for_humans(text, catalog)
    text = enrich_bare_log_names(text, catalog)
    text = strip_product_sql_aliases(text)
    text = replace_human_field_labels(text)
    text = strip_product_sql_aliases(text)
    text = re.sub(r"[（(]\s*PARTITION\s+BY[^）)]*[）)]", "", text, flags=re.I)
    text = re.sub(r"\bPARTITION\s+BY\s+[a-zA-Z_][\w]*(?:\s*,\s*[a-zA-Z_][\w]*)*", "按对应业务分组", text, flags=re.I)
    text = text.replace("PlayerRegister【玩家注册】 表", "PlayerRegister【玩家注册】")
    text = text.replace("PlayerLogin【玩家登录】 表", "PlayerLogin【玩家登录】")
    text = text.replace("PlayerLogin【玩家登录】 的 vOpenID 去重", "PlayerLogin【玩家登录】 的玩家去重")
    text = text.replace("PlayerRegister【玩家注册】 的 vOpenID 去重", "PlayerRegister【玩家注册】 的玩家去重")
    text = text.replace("玩家去重玩家", "玩家去重")
    text = re.sub(r"\b(?:login_day|register_day|class_base|class_daily|class_total)\b", "对应业务步骤", text, flags=re.I)
    text = text.replace("对应业务步骤 读取范围", "登录历史读取范围")
    text = text.replace("对应业务步骤 向", "登录历史向")
    text = text.replace("按 SQL WHERE/JOIN 过滤后的当前结果集", "当前查询的 Base")
    text = text.replace("当前 SQL 的 Base", "当前查询的 Base")
    text = text.replace("SQL 作者", "查询作者")
    text = text.replace("同一版 SQL", "同一版查询")
    text = text.replace("SQL 输出", "查询输出")
    text = text.replace("是否已被 SQL 覆盖", "是否已被当前查询覆盖")
    text = text.replace("查询 跑出", "查询跑出")
    text = text.replace("unknown/未配置", "未配置")
    text = re.sub(r"\bbattleloginout\b(?!【)", "BattleLogInOut【战斗登录登出】", text, flags=re.I)
    text = text.replace("Base 级筛选：Base 只包含", "只看")
    text = re.sub(r"只看\s+", "只看", text)
    text = re.sub(r"\s*=\s*", "：", text, count=1) if re.match(r"^[^=：:]{1,30}\s*=", text) else text
    text = text.replace("小于 大于 ''", "非空")
    return clip_text(text, 420)


def trim_sentence_end(value: str) -> str:
    return value.rstrip("。；;，, ")


def human_top_business_lines(sql: str, catalog: dict[str, str] | None = None) -> list[str]:
    ignored_prefixes = (
        "平台",
        "库",
        "数据库",
        "目标平台",
        "创建日期",
        "口径说明",
        "备注",
        "说明",
    )
    lines: list[str] = []
    for raw_line in top_comment_lines(sql):
        line = clean_human_business_text(raw_line, catalog)
        if not line:
            continue
        if re.fullmatch(r"[=\-_*<>◀▶\s]+", line):
            continue
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9/（）()&×+\- ]{1,30}[：:]", line):
            continue
        if any(line.startswith(prefix + "：") or line == prefix for prefix in ignored_prefixes):
            continue
        if "大表只扫" in line or "字段裁剪" in line or "分区裁剪" in line:
            continue
        if line.startswith("数据源：") or "必要字段" in line or "字段：" in line:
            continue
        if looks_like_cte_or_sql_trace(line) and not any(
            keyword in line
            for keyword in ["PlayerLogin", "PlayerRegister", "全集", "新增", "回流", "常驻", "占比", "漏斗", "分桶", "时长"]
        ):
            continue
        lines.append(line)
    return unique_in_order(lines)[:14]


def denominator_from_comments(context: MetricBusinessContext, fallback: str, catalog: dict[str, str] | None = None) -> str:
    for name in ["占比分母", "分母", "Base", "全集"]:
        value = business_definition_for(context, name)
        if value:
            return clean_human_business_text(value, catalog)
    return clean_human_business_text(fallback, catalog)


def metric_subject_from_alias(alias: str) -> str:
    text = alias.strip()
    for suffix in [
        "用户数量",
        "用户总量",
        "用户数",
        "玩家数量",
        "玩家数",
        "人数",
        "数量",
        "总量",
        "占比",
        "比例",
        "比率",
        "率",
        "_rate",
        "_ratio",
        "_pct",
        "_percent",
        "_cnt",
        "_count",
        "_uv",
    ]:
        if text.lower().endswith(suffix.lower()) and len(text) > len(suffix):
            text = text[: -len(suffix)].strip("_ -")
            break
    return human_metric_subject_alias(text)


def human_metric_subject_alias(value: str) -> str:
    text = value.strip()
    normalized = normalize_identifier(text)
    aliases = {
        "new": "新增",
        "new_user": "新增",
        "new_users": "新增",
        "register": "新增",
        "registered": "新增",
        "resident": "常驻",
        "stay": "常驻",
        "retained": "常驻",
        "reflow": "回流",
        "return": "回流",
        "returning": "回流",
        "backflow": "回流",
        "active": "当日活跃",
        "dau": "当日活跃",
    }
    if normalized in aliases:
        return aliases[normalized]
    for token, label in [
        ("reflow", "回流"),
        ("return", "回流"),
        ("resident", "常驻"),
        ("new", "新增"),
        ("register", "新增"),
        ("active", "当日活跃"),
        ("dau", "当日活跃"),
    ]:
        if re.search(rf"(?:^|_){token}(?:_|$)", normalized):
            return label
    return text


def metric_ratio_terms(metric: str) -> tuple[str, str, str]:
    text = metric.lower()
    raw = metric
    if "上一步" in raw or "prev" in text:
        return "当前步骤到达人数", "上一漏斗步骤到达人数", "当前步骤到达人数 / 上一漏斗步骤到达人数"
    if "首步" in raw or "first" in text:
        return "当前步骤到达人数", "首步到达人数", "当前步骤到达人数 / 首步到达人数"
    if "转化" in raw:
        return "达到目标步骤或满足完成定义的用户数", "对应 Base 用户数", "达到目标步骤或满足完成定义的用户数 / 对应 Base 用户数"
    if "占比" in raw or "比例" in raw or "rate" in text or "ratio" in text:
        subject = metric_subject_from_alias(metric)
        return subject, "Base 总量", f"{subject} / Base 总量"
    return "需要确认分子", "需要确认分母", "需要确认分子 / 分母"


def human_metric_display_name(metric: str) -> str:
    direct_map = {
        "新增用户数": "新增用户数",
        "首日进度占比": "首日进度占比",
        "次留人数": "次留人数",
        "次留率": "次留率",
        "三留人数": "三留人数",
        "三留率": "三留率",
    }
    if metric in direct_map:
        return direct_map[metric]
    normalized = normalize_identifier(metric)
    alias_map = {
        "step_user_cnt": "步骤到达人数",
        "conv_from_prev": "相对上一步转化率",
        "conv_from_first": "相对首步转化率",
        "rank_level": "段位等级",
        "user_cnt": "用户数",
        "team_user_cnt": "组队用户数",
        "reg_date": "注册日期",
        "step_no": "步骤序号",
        "real_participate_users": "实际参与人数",
    }
    if normalized in alias_map:
        return alias_map[normalized]
    subject = metric_subject_from_alias(metric)
    if subject == metric:
        return metric
    if re.search(r"(?:^|_)(?:rate|ratio|pct|percent)$", normalized) or any(
        word in metric for word in ["占比", "比例", "比率", "率"]
    ):
        return f"{subject}占比"
    if re.search(r"(?:^|_)(?:cnt|count|uv|num)$", normalized) or any(
        word in metric for word in ["人数", "用户数", "数量", "总量"]
    ):
        return f"{subject}用户数"
    return subject


def bad_human_metric_piece(value: str) -> bool:
    text = compact(value)
    if not text:
        return True
    return bool(
        looks_like_cte_or_sql_trace(text)
        or "字段 `" in text
        or "未识别显式 GROUP BY" in text
        or re.search(r"\b(?:lag|first_value|over|partition by|rows between|nullif|cast)\s*\(", text, flags=re.I)
    )


def product_view_blocked_text(value: str) -> bool:
    text = compact(value)
    if not text:
        return True
    lower = text.lower()
    if "口径解释置信度低" in text or "这个指标缺少可靠自然语言口径" in text:
        return True
    blocked_tokens = [
        "tdbank_imp_date",
        "dteventtime",
        "hivevar",
        "source_step",
        "source_tables",
        "formula_expression",
        "denominator_expression",
        "numerator_expression",
        "base_expression",
        "demo_log.",
        "demo_warehouse.",
        "named_struct",
    ]
    if any(token in lower for token in blocked_tokens):
        return True
    if " cte" in lower or "cte " in lower or lower.endswith("cte"):
        return True
    if re.search(r"\b(?:params|mode_map|regular_mode_map|server_type_map|result_map)\s+cte\b", text, flags=re.I):
        return True
    if "${" in text:
        return True
    if ">" in text or "<" in text:
        return True
    if re.search(r"[A-Za-z][A-Za-z0-9_]*\s*=", text):
        return True
    if re.search(r"\bt\d+\b", text) and re.search(r"(大于|小于|>=|<=|>|<)", text):
        return True
    if looks_like_cte_or_sql_trace(text):
        return True
    return False


def product_view_safe_items(items: list[str]) -> list[str]:
    return unique_in_order(item for item in items if item and not product_view_blocked_text(item))


def product_view_confirmation_texts(story: dict) -> list[str]:
    items: list[str] = []

    def add(metric_name: object, question: object, reason: object = "", evidence_ref: object = "") -> None:
        metric = str(metric_name or "").strip()
        q = str(question or "").strip()
        reason_text = str(reason or "").strip()
        evidence = str(evidence_ref or "").strip()
        if not q:
            return
        prefix = f"{metric}: " if metric else ""
        suffix_parts = [part for part in [reason_text, evidence] if part]
        suffix = f"（{'；'.join(suffix_parts)}）" if suffix_parts else ""
        items.append(prefix + q + suffix)

    for item in story.get("shared_confirmations", []) or []:
        if isinstance(item, dict):
            add(item.get("metric_name"), item.get("question"), item.get("reason"), item.get("evidence_ref"))
        else:
            add("", item)
    for card in story.get("metric_cards", []) or []:
        if not isinstance(card, dict):
            continue
        metric_name = card.get("metric_name", "")
        for item in card.get("metric_confirmations", []) or []:
            if isinstance(item, dict):
                add(metric_name or item.get("metric_name"), item.get("question"), item.get("reason"), item.get("evidence_ref"))
            else:
                add(metric_name, item)
    for row in story.get("metric_overview", []) or []:
        if not isinstance(row, dict):
            continue
        confidence = str(row.get("confidence") or "").strip().lower()
        main_risk = str(row.get("main_risk") or "").strip()
        if main_risk and confidence in {"low", "medium"}:
            add(row.get("metric_name"), main_risk)
    output_contract = story.get("output_contract") or {}
    if isinstance(output_contract, dict):
        warning = str(output_contract.get("warning") or "").strip()
        if warning:
            add("结果证据", warning)
    return product_view_safe_items(items)[:12]


def product_next_focus(story: dict, review: FileReview | None = None, roles: ReviewRoleContext | None = None) -> str:
    confirmations = product_view_confirmation_texts(story)
    if confirmations:
        return confirmations[0]
    output_contract = story.get("output_contract") or {}
    if isinstance(output_contract, dict):
        product_check = str(output_contract.get("product_check") or "").strip()
        if product_check and not product_view_blocked_text(product_check):
            return product_check
    metric_paths = story.get("metric_path_cards") or []
    if metric_paths:
        first = metric_paths[0]
        if isinstance(first, dict):
            body = str(first.get("body") or first.get("formula") or "").strip()
            metric_name = str(first.get("metric_name") or first.get("title") or "").strip()
            text = f"{metric_name}: {body}" if metric_name and body else body or metric_name
            if text and not product_view_blocked_text(text):
                return text
    sentence = str(story.get("one_sentence") or "").strip()
    if sentence and not product_view_blocked_text(sentence):
        return sentence
    if review is not None and roles is not None:
        return next_focus_for(review, roles)
    return "查看指标卡片，核对分子、分母、去重对象、聚合维度和结果证据。"


def sanitize_human_metric_piece(value: str, fallback: str, catalog: dict[str, str] | None = None) -> str:
    cleaned = clean_human_business_text(value, catalog)
    return fallback if bad_human_metric_piece(cleaned) else cleaned


def sanitize_product_review_guidance(value: str, fallback: str, catalog: dict[str, str] | None = None) -> str:
    cleaned = clean_human_business_text(value, catalog)
    if not cleaned:
        return fallback
    replacements = {
        "SQL 表达式": "计算口径",
        "SQL表达式": "计算口径",
        "SQL 追溯中的": "",
        "SQL追溯中的": "",
        "SQL 追溯": "口径证据",
        "SQL追溯": "口径证据",
        "CASE/IF": "指标内条件",
        "CASE": "条件",
        "IF": "条件",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)
    cleaned = cleaned.replace("表达式", "口径")
    cleaned = cleaned.replace("指标内筛选/条件 条件", "指标内筛选/条件")
    cleaned = cleaned.replace("条件 条件", "条件")
    return cleaned


def human_metric_story(
    card: dict,
    context: MetricBusinessContext,
    common_base: str,
    catalog: dict[str, str] | None = None,
) -> dict:
    metric = str(card.get("metric", ""))
    subject = metric_subject_from_alias(metric)
    direct = business_definition_for(context, metric) or business_definition_for(context, subject)
    normalized_metric = normalize_identifier(metric)
    is_rate = bool(
        re.search(r"(占比|比例|比率|率|_rate|_ratio|_pct|_percent)$", metric, flags=re.I)
        or normalized_metric in {"conv_from_prev", "conv_from_first"}
    )
    fallback_numerator, fallback_denominator, fallback_calculation = metric_ratio_terms(metric)
    base = sanitize_human_metric_piece(
        context.base_description or common_base or card.get("base", ""),
        "需要确认 Base 人群/记录范围",
        catalog,
    )
    denominator = sanitize_human_metric_piece(card.get("denominator", ""), fallback_denominator, catalog)
    numerator = sanitize_human_metric_piece(card.get("numerator", ""), fallback_numerator, catalog)
    calculation = sanitize_human_metric_piece(card.get("calculation", ""), fallback_calculation, catalog)
    business_definition = sanitize_human_metric_piece(
        card.get("business_definition", ""),
        f"{metric}：{calculation}。",
        catalog,
    )
    if direct and is_rate:
        denominator = denominator_from_comments(context, base, catalog)
        numerator = trim_sentence_end(clean_human_business_text(direct, catalog))
        calculation = f"{subject} / {denominator}" if denominator else f"{subject} 占 Base 的比例"
        business_definition = f"{metric}：{subject} 在 Base 中的占比。"
    elif direct:
        numerator = trim_sentence_end(clean_human_business_text(direct, catalog))
        denominator = "不适用"
        calculation = "按业务对象去重/汇总计数；具体去重粒度见 Base 和输出粒度。" if not is_rate else calculation
        business_definition = f"{metric}：{numerator}。"
    elif "活跃" in metric and ("总量" in metric or "用户数" in metric) and context.base_description:
        numerator = clean_human_business_text(context.base_description, catalog)
        denominator = "不适用"
        calculation = "按玩家去重计数。"
        business_definition = f"{metric}：{numerator}。"
    display_metric = human_metric_display_name(metric)
    if display_metric != metric:
        business_definition = re.sub(rf"^{re.escape(metric)}\s*[：:]", f"{display_metric}：", business_definition)
    if normalized_metric == "conv_from_prev":
        numerator = "本步到达人数"
        denominator = "上一步到达人数"
        calculation = "本步到达人数 / 上一步到达人数"
        business_definition = f"{display_metric}：当前漏斗步骤相对上一漏斗步骤的转化率。"
    elif normalized_metric == "conv_from_first":
        numerator = "本步到达人数"
        denominator = "首步到达人数"
        calculation = "本步到达人数 / 首步到达人数"
        business_definition = f"{display_metric}：当前漏斗步骤相对首步的累计转化率。"
    return {
        "metric": display_metric,
        "business_definition": business_definition,
        "base": base,
        "numerator": numerator,
        "denominator": denominator,
        "calculation": calculation,
        "how_to_review": sanitize_product_review_guidance(
            card.get("how_to_review", ""),
            "先确认业务问题、Base、分子、分母和结果样例是否互相解释。",
            catalog,
        ),
        "pass_criteria": sanitize_product_review_guidance(
            card.get("pass_criteria", ""),
            "业务解释、计算口径和结果样例三者一致，没有未确认的关键假设。",
            catalog,
        ),
        "confidence": str(card.get("confidence", "")),
    }


def business_scope_summary(review: FileReview, catalog: dict[str, str] | None = None) -> list[str]:
    items: list[str] = []
    for item in review.business_filters:
        if item.get("scope") != "base_filter":
            continue
        label = item.get("label") or item.get("field") or "筛选"
        values = [str(value) for value in item.get("values", [])]
        mapping = item.get("mapping", [])
        mapped_values = []
        for row in mapping:
            mapped_values.append(f"{row.get('value')}（{row.get('name')}/{row.get('category')}）")
        unknown_values = [str(value) for value in item.get("unknown_values", [])]
        dynamic_values = [str(value) for value in item.get("dynamic_values", [])]
        if mapped_values:
            value_text = "、".join(mapped_values)
        elif values:
            value_text = "、".join(values)
        else:
            value_text = item.get("business_effect", "")
        if unknown_values:
            value_text += f"；未配置映射：{'、'.join(unknown_values)}"
        if dynamic_values:
            value_text += f"；动态值：{'、'.join(dynamic_values)}"
        if value_text:
            items.append(f"只看 {label} {item.get('operator', '=')} {value_text}")
    return unique_in_order(clean_human_business_text(item, catalog) for item in items if item)[:10]


def business_logic_steps(
    review: FileReview,
    metric_review: dict,
    context: MetricBusinessContext,
    catalog: dict[str, str] | None = None,
) -> list[str]:
    lines = human_top_business_lines(review.sql, catalog)
    business = metric_review.get("business_review", {})
    funnel = business.get("funnel_review", {})
    if funnel.get("detected"):
        lines.append(clean_human_business_text(funnel.get("summary", ""), catalog))
        lines.append(clean_human_business_text(funnel.get("base", ""), catalog))
        for step in funnel.get("steps", [])[:16]:
            source = display_log_name(step.get("source_table", ""), catalog) if step.get("source_table") else "未识别日志"
            lines.append(
                clean_human_business_text(
                    f"第 {step.get('order')} 步：{step.get('step_name')}；原始日志 {source}；{step.get('reach_rule')}",
                    catalog,
                )
            )
    distribution = business.get("distribution_review", {})
    if distribution.get("detected") and not lines:
        lines.append(clean_human_business_text(distribution.get("summary", ""), catalog))
        for card in distribution.get("bucket_cards", [])[:6]:
            definitions = card.get("definitions", [])
            if definitions:
                brief = "；".join(
                    f"{item.get('bucket')}：{item.get('business_effect') or item.get('condition')}"
                    for item in definitions[:8]
                )
                lines.append(clean_human_business_text(f"{card.get('field')} 分桶：{brief}", catalog))
    if context.duration_logic:
        lines.append(f"时长算法：{clean_human_business_text(context.duration_logic, catalog)}")
    if not lines:
        base = clean_human_business_text(context.base_description or metric_review.get("common_base", ""), catalog)
        if base:
            lines.append(f"先确定 Base：{base}")
        for card in metric_review.get("metric_cards", [])[:6]:
            story = human_metric_story(card, context, metric_review.get("common_base", ""), catalog)
            if story.get("business_definition"):
                lines.append(story["business_definition"])
    return product_view_safe_items(lines)[:18]


def product_filter_review_focus(item: dict, catalog: dict[str, str] | None = None) -> str:
    kind = str(item.get("kind", ""))
    scope = str(item.get("scope", ""))
    label = clean_human_business_text(item.get("label") or item.get("field") or "筛选", catalog)
    unknown_values = [str(value) for value in item.get("unknown_values", [])]
    dynamic_values = [str(value) for value in item.get("dynamic_values", [])]
    if unknown_values:
        return f"先补 {label} 映射或让需求方确认：{', '.join(unknown_values)}。"
    if kind == "game_mode":
        return "核对模式 ID、中文模式名和模式大类是否就是本指标范围。"
    if kind in {"zone", "game_server"}:
        return "确认这是本批次目标区服/大区，不是临时测试范围。"
    if kind == "team_size":
        return "确认单人/多人边界是否符合业务定义，尤其是否包含等号。"
    if kind == "duration":
        return "确认单位、上下界和分桶边界是否明确。"
    if dynamic_values and scope == "join_mapping":
        return f"核对关联来源是否可靠，避免 JOIN 放大或漏掉 {label}。"
    if dynamic_values:
        return f"核对 {label} 的动态来源；需要固定范围时应展开取值。"
    return "核对该筛选是否属于业务口径，而不是临时调试条件。"


def unique_product_filter_cards(filters: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for item in filters:
        key = (
            str(item.get("label", "")),
            str(item.get("scope", "")),
            str(item.get("business_effect", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def build_product_view(review: FileReview, roles: ReviewRoleContext | None = None) -> dict:
    if review.product_view:
        return review.product_view
    roles = roles or ReviewRoleContext()
    metric_review = metric_review_summary(review)
    context = metric_business_context(review.sql)
    catalog = log_catalog_from_roles(roles)
    concepts = build_product_concepts(review, context, catalog)
    logic_review = build_sql_logic_review(review, metric_review, context, catalog)
    business = metric_review.get("business_review", {})
    metric_stories = []
    for card in metric_review.get("metric_cards", []):
        override = concepts.metric_overrides.get(normalize_identifier(str(card.get("metric", ""))))
        metric_stories.append(
            human_metric_story(
                {**card, **override} if override else card,
                context,
                concepts.base or metric_review.get("common_base", ""),
                catalog,
            )
        )
    filters = [
        {
            "label": clean_human_business_text(item.get("label") or item.get("field") or "", catalog),
            "scope": clean_human_business_text(item.get("scope_label") or item.get("scope") or "", catalog),
            "business_effect": clean_human_business_text(item.get("business_effect", ""), catalog),
            "review_focus": product_filter_review_focus(item, catalog),
        }
        for item in review.business_filters[:18]
    ]
    filters.extend(concepts.filter_cards)
    for item in filters:
        item.setdefault("review_focus", "核对该条件是否属于当前业务问题的核心口径。")
    filters = unique_product_filter_cards(filters)
    overridden_metric_names = set(concepts.metric_overrides)
    raw_questions = [
        clean_human_business_text(item, catalog)
        for item in (
            list(metric_review.get("review_questions", []))
        + ([] if concepts.walkthrough_sections else [
            item.get("what_to_check", "")
            for item in business.get("pattern_cards", [])
            if "未识别" in item.get("what_to_check", "") or "确认" in item.get("how_to_judge", "")
        ])
        )
    ]
    review_questions = []
    for question in raw_questions:
        normalized_question = normalize_identifier(question)
        if any(metric_name in normalized_question for metric_name in overridden_metric_names) and "口径解释置信度低" in question:
            continue
        review_questions.append(question)
    review_questions = product_view_safe_items(review_questions + product_performance_review_points(review, catalog))[:12]
    story_base = concepts.base or clean_human_business_text(context.base_description or metric_review.get("common_base", ""), catalog)
    fallback_logic_steps = business_logic_steps(review, metric_review, context, catalog)
    story_logic_steps = product_view_safe_items(concepts.logic_steps or fallback_logic_steps)[:18]
    scope_items = business_scope_summary(review, catalog)
    if concepts.scope:
        concept_scope_text = " ".join(concepts.scope)
        scope_items = [
            item
            for item in scope_items
            if not any(value in concept_scope_text for value in re.findall(r"\b\d{3,}\b", item))
        ]
    return {
        "title": review.path.stem,
        "one_sentence": clean_human_business_text(
            concepts.conclusion or product_one_sentence(review, concepts, metric_review.get("summary", "")),
            catalog,
        ),
        "business_question": clean_human_business_text(business.get("business_question", ""), catalog),
        "analysis_pattern": business.get("pattern_label", ""),
        "source_logs": business_source_logs(review, roles),
        "business_scope": product_view_safe_items(concepts.scope + scope_items),
        "base": story_base,
        "grouping": clean_human_business_text(metric_review.get("grouping", ""), catalog),
        "logic_review": logic_review,
        "logic_steps": story_logic_steps,
        "walkthrough_sections": concepts.walkthrough_sections,
        "metrics": metric_stories,
        "key_filters": filters,
        "reviewer_should_check": unique_in_order(concepts.review_checks + business.get("pattern_review_order", [])),
        "unknowns_to_confirm": review_questions,
        "evidence_note": result_brief(review),
        "project_roles": {
            "definition_project": project_label(roles.definition),
            "execution_project": project_label(review.execution_project),
            "delivery_project": project_label(roles.delivery),
            "evidence_status": review.evidence_status,
        },
    }


def product_performance_review_points(review: FileReview, catalog: dict[str, str] | None = None) -> list[str]:
    points: list[str] = []
    for detail in review.performance_preflight.get("trigger_details", []):
        code = detail.get("code")
        if code == "ratio_after_detail_join_risk":
            points.append("确认分子和分母是否先按同一业务粒度聚合，再计算比例；避免 JOIN 后明细行放大。")
        elif code == "raw_large_log_join":
            points.append("确认多个大日志之间的 JOIN 是否会让同一个玩家、战斗或事件重复计数。")
        elif code == "battlesrvid_without_anti_crossing_key":
            points.append("确认 BattleSrvId 是否只在正确区服、模式、日期或 UniqueBattleID 范围内匹配，避免串战斗服。")
        elif code == "raw_cumulative_duration_sum":
            points.append("确认累计时长字段是否先在正确战斗/玩家粒度取 MAX 或做差，再求和。")
        elif code == "unobservable_retention_zero":
            points.append("确认留存不可观测的日期窗口是否不计入分母，不能把未知窗口当成 0。")
    return product_view_safe_items(clean_human_business_text(item, catalog) for item in points)


def review_sql_hash(review: FileReview) -> str:
    return hashlib.sha256(review.sql.encode("utf-8", errors="replace")).hexdigest()


def product_metric_compat_from_card(card: dict) -> dict:
    return {
        "metric": str(card.get("metric_name") or ""),
        "business_definition": str(card.get("business_meaning") or ""),
        "base": str(card.get("row_grain_explanation") or ""),
        "numerator": str(card.get("numerator") or ""),
        "denominator": str(card.get("denominator") or ""),
        "calculation": str(card.get("calculation") or ""),
        "how_to_review": "核对业务含义、分子、分母、去重对象、聚合维度和结果样例是否一致。",
        "pass_criteria": "业务定义、SQL 证据、结果列和值样例可以互相解释。",
        "confidence": str(card.get("confidence") or ""),
    }


def normalize_generated_product_view(product_view: dict, static_view: dict) -> dict:
    normalized = dict(product_view)
    raw_unknowns = product_view.get("unknowns_to_confirm", []) if isinstance(product_view.get("unknowns_to_confirm"), list) else []
    for key in [
        "title",
        "one_sentence",
        "business_question",
        "analysis_pattern",
        "source_logs",
        "business_scope",
        "base",
        "grouping",
        "logic_review",
        "logic_steps",
        "walkthrough_sections",
        "key_filters",
        "reviewer_should_check",
        "unknowns_to_confirm",
        "evidence_note",
        "project_roles",
    ]:
        if key not in normalized:
            normalized[key] = static_view.get(key, [] if key.endswith("s") else "")
    normalized.setdefault("metric_overview", [])
    normalized.setdefault("metric_cards", [])
    normalized.setdefault("dimension_overview", [])
    normalized.setdefault("common_filters", normalized.get("key_filters", []))
    normalized.setdefault("shared_confirmations", [])
    normalized.setdefault("evidence_sections", [])
    normalized.setdefault("execution_evidence", {})
    normalized.setdefault("business_story_cards", [])
    normalized.setdefault("metric_path_cards", [])
    normalized.setdefault("output_contract", {})
    static_logic = static_view.get("logic_review", {})
    generated_logic = normalized.get("logic_review") if isinstance(normalized.get("logic_review"), dict) else {}
    if static_logic:
        merged_logic = dict(static_logic)
        if generated_logic:
            for key, value in generated_logic.items():
                if value:
                    merged_logic[key] = value
        normalized["logic_review"] = merged_logic
        evidence_sections = normalized.get("evidence_sections")
        if not isinstance(evidence_sections, list):
            evidence_sections = []
        logic_items = []
        if merged_logic.get("summary"):
            logic_items.append("summary: " + str(merged_logic.get("summary")))
        logic_items.extend(str(item) for item in (merged_logic.get("scope") or [])[:12])
        logic_items.extend(str(item) for item in (merged_logic.get("key_steps") or [])[:12])
        if logic_items and not any(section.get("title") == "静态 SQL 逻辑证据" for section in evidence_sections if isinstance(section, dict)):
            evidence_sections.append(
                {
                    "title": "静态 SQL 逻辑证据",
                    "default_collapsed": True,
                    "summary": "脚本抽取的辅助逻辑证据；产品口径以指标卡、事件契约和风险登记为准。",
                    "items": logic_items[:40],
                }
            )
        normalized["evidence_sections"] = evidence_sections
    if not normalized.get("metrics") and normalized.get("metric_cards"):
        normalized["metrics"] = [product_metric_compat_from_card(card) for card in normalized["metric_cards"]]
    normalized.setdefault("metrics", static_view.get("metrics", []))
    normalized["project_roles"] = static_view.get("project_roles", normalized.get("project_roles", {}))
    conclusion = normalized.get("conclusion") if isinstance(normalized.get("conclusion"), dict) else {}
    normalized["conclusion"] = {
        "status": conclusion.get("status") or ("needs_confirmation" if normalized.get("shared_confirmations") else "pass"),
        "business_question": conclusion.get("business_question") or normalized.get("business_question") or normalized.get("one_sentence", ""),
        "analysis_pattern": conclusion.get("analysis_pattern") or normalized.get("analysis_pattern", ""),
        "base": conclusion.get("base") or normalized.get("base", ""),
        "grouping": conclusion.get("grouping") or normalized.get("grouping", ""),
        "evidence_status": conclusion.get("evidence_status") or normalized.get("project_roles", {}).get("evidence_status", ""),
        "semantic_review_status": normalized.get("semantic_review_status", "unknown"),
    }
    status = str(normalized.get("semantic_review_status") or "").strip().lower()
    if status in {"llm", "llm_cached"}:
        normalized["unknowns_to_confirm"] = product_view_safe_items(
            [str(item) for item in raw_unknowns] + product_view_confirmation_texts(normalized)
        )[:12]
    else:
        normalized["unknowns_to_confirm"] = product_view_safe_items(
            [str(item) for item in normalized.get("unknowns_to_confirm", []) or []]
        )[:12]
    return normalized


def hydrate_product_review(
    root: Path,
    review: FileReview,
    roles: ReviewRoleContext,
    *,
    product_review_mode: str,
    product_review_command: str,
    product_review_cache_dir: Path | None,
) -> None:
    static_view = build_product_view(review, roles)
    code_view = build_code_view(root, review, roles)
    evidence = build_evidence_bundle(
        item_path=relative_name(root, review.path),
        item_name=review.path.name,
        sql_hash=review_sql_hash(review),
        sql_text=review.sql,
        static_product_view=static_view,
        code_view=code_view,
        dimensions=review_dimensions(review, roles),
    )
    product_view = generate_product_view(
        evidence,
        mode=product_review_mode,
        agent_command=product_review_command,
        cache_dir=product_review_cache_dir,
    )
    review.product_review_evidence = evidence
    review.product_view = normalize_generated_product_view(product_view, static_view)


def hydrate_product_reviews(
    root: Path,
    reviews: list[FileReview],
    roles: ReviewRoleContext,
    *,
    product_review_mode: str,
    product_review_command: str,
    product_review_cache_dir: Path | None,
) -> None:
    evidences: list[dict] = []
    static_views: list[dict] = []
    for review in reviews:
        static_view = build_product_view(review, roles)
        code_view = build_code_view(root, review, roles)
        evidence = build_evidence_bundle(
            item_path=relative_name(root, review.path),
            item_name=review.path.name,
            sql_hash=review_sql_hash(review),
            sql_text=review.sql,
            static_product_view=static_view,
            code_view=code_view,
            dimensions=review_dimensions(review, roles),
        )
        review.product_review_evidence = evidence
        evidences.append(evidence)
        static_views.append(static_view)
    product_views = generate_product_views_batch(
        evidences,
        mode=product_review_mode,
        agent_command=product_review_command,
        cache_dir=product_review_cache_dir,
    )
    for review, product_view, static_view in zip(reviews, product_views, static_views, strict=False):
        review.product_view = normalize_generated_product_view(product_view, static_view)

def build_code_view(root: Path, review: FileReview, roles: ReviewRoleContext) -> dict:
    return {
        "reviewer_card": reviewer_card(review, roles),
        "review_guide": review_judgement_guide(review, roles),
        "role_context": {
            "definition_project": project_label(roles.definition),
            "review_stage": review.review_stage,
            "query_review_status": review.query_review_status,
            "deployment_readiness": review.deployment_readiness,
            "execution_project": project_label(review.execution_project),
            "execution_inference_confidence": review.execution_inference_confidence,
            "execution_inference_reason": review.execution_inference_reason,
            "delivery_project": project_label(roles.delivery),
            "evidence_status": review.evidence_status,
            "future_target_verification_plan": review.future_target_verification_plan,
            "delivery_table_mismatches": review.delivery_table_mismatches,
            "proxy_limitations": review.proxy_limitations,
            "checked_concept_keys": review.checked_concept_keys,
        },
        "execution_evidence": review.execution_evidence,
        "sql_summary": {
            "business_category": review.business_category or DEFAULT_BUSINESS_CATEGORY,
            "analysis_type": review.analysis_type or DEFAULT_ANALYSIS_TYPE,
            "grain": review.grain,
            "time_grain": review.time_grain,
            "source_tables": review.tables,
            "target_tables": review.target_tables,
            "metrics": review.metrics,
            "dimensions": review.dimensions,
            "parameters": review.parameters,
            "final_fields": review.final_fields,
            "current_sql_role": "review_subject",
            "result_pairing_method": review.result_pairing_method,
            "cte_count": review.cte_count,
            "join_count": review.join_count,
            "has_count_distinct": review.has_count_distinct,
            "has_window_function": review.has_window_function,
            "has_global_order_by": review.has_global_order_by,
            "grade": review.grade,
            "performance_tier": review.performance_preflight.get("tier", ""),
            "performance_score": review.performance_preflight.get("score", 0),
        },
        "performance_preflight": review.performance_preflight,
        "sql_facts": review.sql_facts,
        "business_filters": review.business_filters,
        "metric_review_trace": metric_review_summary(review),
        "metric_logic": metric_logic_items(review),
        "result_file": result_payload(root, review),
        "findings": [{"severity": item.severity, "message": item.message} for item in review.findings],
        "rule_checks": [
            {
                "rule_id": check.rule_id,
                "status": check.status,
                "result": check.result,
                "message": check.message,
                "evidence": check.evidence,
                "concept_key": check.concept_key,
                "title": check.title,
                "rule_summary": check.rule_summary,
            }
            for check in review.rule_checks
        ],
    }


def render_product_walkthrough_markdown(sections: list[dict]) -> str:
    if not sections:
        return "- 未抽取到可核对口径拆解；请 SQL 作者补 Base、分类规则、指标分子/分母和关键筛选。"
    lines: list[str] = []
    for section in sections:
        title = section.get("title", "口径拆解")
        lines.extend([f"#### {title}", ""])
        for paragraph in section.get("paragraphs", []):
            lines.extend([str(paragraph), ""])
        table = section.get("table", {})
        headers = table.get("headers", [])
        rows = table.get("rows", [])
        if headers and rows:
            lines.append("| " + " | ".join(markdown_cell(item) for item in headers) + " |")
            lines.append("| " + " | ".join("---" for _ in headers) + " |")
            for row in rows:
                padded = list(row) + [""] * max(0, len(headers) - len(row))
                lines.append("| " + " | ".join(markdown_cell(item) for item in padded[: len(headers)]) + " |")
            lines.append("")
        bullets = section.get("bullets", [])
        if bullets:
            lines.extend(f"- {item}" for item in bullets)
            lines.append("")
    return "\n".join(lines).rstrip()


def render_product_view_markdown(review: FileReview, roles: ReviewRoleContext) -> str:
    story = build_product_view(review, roles)
    conclusion = story.get("conclusion") if isinstance(story.get("conclusion"), dict) else {}
    execution_evidence = story.get("execution_evidence") or {}
    business_story_cards = story.get("business_story_cards") or []
    metric_path_cards = story.get("metric_path_cards") or []
    output_contract = story.get("output_contract") or {}
    event_contracts = story.get("event_contracts") or []
    event_index = story.get("event_index") or []
    risk_register = story.get("risk_register") or []
    metric_summary_table = story.get("metric_summary_table") or []
    review_actions = story.get("review_actions") or []
    metric_cards = story.get("metric_cards") or []
    metric_overview = story.get("metric_overview") or []
    common_filters = story.get("common_filters") or story.get("key_filters") or []
    shared_confirmations = story.get("shared_confirmations") or []
    evidence_sections = story.get("evidence_sections") or []
    walkthrough_sections = story.get("walkthrough_sections") or []
    def contract_values(values: Any) -> list[str]:
        rows: list[str] = []
        for value in values or []:
            if isinstance(value, dict):
                text = first_non_empty(
                    value.get("field"),
                    value.get("name"),
                    value.get("label"),
                    value.get("metric_name"),
                    value.get("title"),
                    value.get("value"),
                )
            else:
                text = str(value)
            if text:
                rows.append(str(text))
        return rows
    def join_refs(values: Any) -> str:
        return "、".join(str(value) for value in (values or []) if str(value))
    lines = [
        f"## `{review.path.name}`",
        "",
        "### 结论",
        "",
        f"- 状态: `{conclusion.get('status', '') or 'unknown'}`",
        f"- 一句话: {clean_human_business_text(story['one_sentence'])}",
        f"- 分析形态: {conclusion.get('analysis_pattern') or story['analysis_pattern'] or '未识别'}",
        f"- 业务问题: {clean_human_business_text(conclusion.get('business_question') or story['business_question'])}",
        f"- Base: {clean_human_business_text(conclusion.get('base') or story.get('base', ''))}",
        f"- 使用的原始日志: {'、'.join(story.get('source_logs', [])) or '未识别'}",
        f"- 分组/粒度: {conclusion.get('grouping') or story['grouping']}",
        f"- 结果证据: {conclusion.get('evidence_status') or story['evidence_note']}",
        f"- 产品语义审查: `{story.get('semantic_review_status', 'unknown')}` {story.get('semantic_review_note', '')}".rstrip(),
        "",
    ]
    if execution_evidence:
        lines.extend(
            [
                "### 执行与结果证据",
                "",
                f"- 审查对象: `{execution_evidence.get('review_subject', '')}`，当前文件角色 `{execution_evidence.get('current_sql_role', '')}`",
                f"- SQL 文件: {markdown_cell('、'.join(execution_evidence.get('sql_files', [])) or 'none')}",
                f"- 结果文件: {markdown_cell('、'.join(execution_evidence.get('result_files', [])) or execution_evidence.get('selected_result_file', '') or 'none')}",
                f"- 结果配对: `{execution_evidence.get('result_pairing_method', '')}` / `{execution_evidence.get('result_status', '')}` / rows=`{execution_evidence.get('result_rows', 'unknown')}`",
                f"- 执行/交付项目: `{execution_evidence.get('execution_project', '')}` / `{execution_evidence.get('delivery_project', '')}`",
                f"- 证据范围: `{execution_evidence.get('evidence_status', '')}`",
                "",
            ]
        )
    if risk_register:
        lines.extend(["### 风险登记表", ""])
        lines.extend(
            [
                "| 风险 | 等级 | 冲突/待确认对象 | SQL 当前做法 | 标准/期望 | 差异 | 影响指标 | 下一步 | 证据 |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for risk in risk_register:
            lines.append(
                f"| {markdown_cell((risk.get('risk_id') or '') + ' ' + (risk.get('title') or ''))} | "
                f"{markdown_cell(risk.get('severity', ''))} | "
                f"{markdown_cell(risk.get('conflict_object', ''))} | "
                f"{markdown_cell(risk.get('sql_current', ''))} | "
                f"{markdown_cell(risk.get('expected_or_standard', ''))} | "
                f"{markdown_cell(risk.get('difference', ''))} | "
                f"{markdown_cell(join_refs(risk.get('affected_metrics', [])))} | "
                f"{markdown_cell(risk.get('action', ''))} | "
                f"{markdown_cell(join_refs(risk.get('evidence_refs', [])))} |"
            )
        lines.append("")
    if metric_summary_table:
        lines.extend(["### 指标总表", ""])
        lines.extend(
            [
                "| 指标 | 类型 | 计算 | 关键口径 | 分子 | 分母 | 去重 | 粒度 | 事件 | 风险 | 置信度 | 状态 |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for item in metric_summary_table:
            lines.append(
                f"| {markdown_cell(item.get('metric_name', ''))} | {markdown_cell(item.get('metric_type', ''))} | "
                f"{markdown_cell(item.get('calculation', ''))} | {markdown_cell(join_refs(item.get('key_conditions', [])))} | "
                f"{markdown_cell(item.get('numerator', ''))} | "
                f"{markdown_cell(item.get('denominator', ''))} | {markdown_cell(item.get('dedup_key', ''))} | "
                f"{markdown_cell(item.get('grain', ''))} | {markdown_cell(join_refs(item.get('event_refs', [])))} | "
                f"{markdown_cell(join_refs(item.get('risk_refs', [])))} | {markdown_cell(item.get('confidence', ''))} | "
                f"{markdown_cell(item.get('review_status', ''))} |"
            )
        lines.append("")
    if review_actions:
        lines.extend(["### 审查动作", ""])
        lines.extend(["| 动作 | 来源 | 负责人 | 为什么 |", "|---|---|---|---|"])
        for action in review_actions:
            lines.append(
                f"| {markdown_cell(action.get('action', ''))} | {markdown_cell(action.get('source_ref', ''))} | "
                f"{markdown_cell(action.get('owner_hint', ''))} | {markdown_cell(action.get('why', ''))} |"
            )
        lines.append("")
    if walkthrough_sections:
        lines.extend(["### 模型收口口径拆解", ""])
        for section in walkthrough_sections:
            lines.extend([f"#### {section.get('title', '口径拆解')}", ""])
            for paragraph in section.get("paragraphs", []) or []:
                lines.extend([str(paragraph), ""])
            table = section.get("table") or {}
            headers = table.get("headers") or []
            rows = table.get("rows") or []
            if headers and rows:
                lines.append("| " + " | ".join(markdown_cell(item) for item in headers) + " |")
                lines.append("|" + "|".join("---" for _ in headers) + "|")
                for row in rows:
                    values = list(row or [])
                    lines.append("| " + " | ".join(markdown_cell(values[index] if index < len(values) else "") for index, _ in enumerate(headers)) + " |")
                lines.append("")
            for bullet in section.get("bullets", []) or []:
                lines.append(f"- {bullet}")
            lines.append("")
    if business_story_cards:
        lines.extend(["### 业务口径故事", ""])
        for card in business_story_cards:
            lines.extend(
                [
                    f"#### {card.get('title', '口径卡')}",
                    "",
                    f"{card.get('body', '')}",
                    "",
                ]
            )
    if metric_path_cards:
        lines.extend(["### 指标路径", ""])
        for card in metric_path_cards:
            lines.extend(
                [
                    f"#### {card.get('title') or card.get('metric_name') or '指标'}",
                    "",
                    f"- 口径: {card.get('body', '')}",
                    f"- 计算: {card.get('formula', '')}",
                    f"- Base/分母: {card.get('base', '')}",
                    f"- 注意: {card.get('caveat', '')}",
                    "",
                ]
            )
    if output_contract:
        lines.extend(
            [
                "### 输出与产品核对",
                "",
                f"- 输出字段: {markdown_cell('、'.join(contract_values(output_contract.get('fields', []))) or '未识别')}",
                f"- 结果列: {markdown_cell('、'.join(contract_values(output_contract.get('result_columns', []))) or '未读取')}",
                f"- 产品核对: {output_contract.get('product_check', '')}",
                f"- 注意: {output_contract.get('warning', '')}",
                "",
            ]
        )
    if event_contracts:
        if event_index:
            lines.extend(["### 事件口径索引", ""])
            lines.extend(["| 事件 | 本源日志/表 | 成立条件 | 统计对象 | 风险 | 置信度 |", "|---|---|---|---|---|---|"])
            for event in event_index:
                lines.append(
                    f"| {markdown_cell((event.get('event_id') or '') + ' ' + (event.get('event_name') or ''))} | "
                    f"{markdown_cell(join_refs(event.get('source_logs_or_tables', [])))} | "
                    f"{markdown_cell(event.get('event_condition', ''))} | "
                    f"{markdown_cell(event.get('statistic_object', ''))} | "
                    f"{markdown_cell(event.get('risk_summary', ''))} | "
                    f"{markdown_cell(event.get('confidence', ''))} |"
                )
            lines.append("")
        lines.extend(["### 事件口径契约", ""])
        lines.extend(
            [
                "| 事件/口径 | 本源日志/表 | 成立条件 | ID/映射 | 统计对象/去重 | 首次/归因规则 | SQL 证据 |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for contract in event_contracts:
            evidence_refs = contract.get("sql_evidence_refs", []) or []
            evidence_snippets = [
                item.get("snippet", "")
                for item in contract.get("sql_evidence", []) or []
                if isinstance(item, dict) and item.get("snippet")
            ]
            lines.append(
                f"| {markdown_cell(contract.get('event_name', ''))} | "
                f"{markdown_cell('、'.join(contract.get('source_logs_or_tables', [])))} | "
                f"{markdown_cell(contract.get('event_condition', ''))} | "
                f"{markdown_cell(contract.get('id_or_mapping', ''))} | "
                f"{markdown_cell(contract.get('statistic_object', ''))} | "
                f"{markdown_cell(contract.get('first_or_final_rule', '') or contract.get('join_or_backfill_rule', ''))} | "
                f"{markdown_cell('；'.join(evidence_refs[:8] + evidence_snippets[:3]))} |"
            )
        lines.append("")
    if not metric_summary_table:
        lines.extend(["### 指标总览", ""])
    if metric_overview and not metric_summary_table:
        lines.extend(
            [
                "| 指标 | 类型 | 口径状态 | 主要风险/判断 | 置信度 | 待确认 |",
                "|---|---|---|---|---|---:|",
            ]
        )
        for item in metric_overview:
            lines.append(
                f"| {markdown_cell(item.get('metric_name', ''))} | {markdown_cell(item.get('metric_type', ''))} | "
                f"{markdown_cell(item.get('review_status', ''))} | {markdown_cell(item.get('main_risk', ''))} | "
                f"{markdown_cell(item.get('confidence', ''))} | {item.get('confirmation_count', 0)} |"
            )
    elif not metric_summary_table:
        lines.append("- 未识别到最终指标候选。")
    lines.extend(["", "### 指标卡片", ""])
    if metric_cards:
        for metric in metric_cards:
            lines.extend(
                [
                    f"#### {metric.get('metric_name', '未命名指标')}",
                    "",
                    f"- 指标含义: {metric.get('business_meaning', '')}",
                    f"- 指标类型: {metric.get('metric_type', '')}",
                    f"- 关键口径条件: {join_refs(metric.get('key_conditions', [])) or '未识别'}",
                    f"- 最终计算: {metric.get('calculation', '')}",
                    f"- 分子: {metric.get('numerator', '')}",
                    f"- 分母: {metric.get('denominator', '')}",
                    f"- 去重对象: {metric.get('dedup_key', '')}",
                    f"- 聚合维度: {'、'.join(metric.get('aggregation_dimensions', [])) or '整体汇总'}",
                    f"- 行粒度: {metric.get('row_grain_explanation', '')}",
                    f"- 标准口径: {metric.get('standard_rule_alignment', '')}",
                    f"- 事件引用: {join_refs(metric.get('event_refs', [])) or '未绑定'}",
                    f"- 风险引用: {join_refs(metric.get('risk_refs', [])) or '未绑定'}",
                    f"- 风险说明: {join_refs(metric.get('risk_notes', [])) or '无'}",
                    f"- 置信度: `{metric.get('confidence', '')}`",
                    "",
                ]
            )
            source_rows = metric.get("source_logs_fields", [])
            if source_rows:
                lines.extend(["| 来源角色 | 本源日志/表 | 本源字段/口径证据 | 业务说明 | 粒度 |", "|---|---|---|---|---|"])
                for row in source_rows:
                    lines.append(
                        f"| {markdown_cell(row.get('role', ''))} | "
                        f"{markdown_cell('、'.join(row.get('source_logs_or_tables', [])))} | "
                        f"{markdown_cell(row.get('field_expression', ''))} | "
                        f"{markdown_cell(row.get('business_story', ''))} | "
                        f"{markdown_cell('、'.join(row.get('group_by', [])))} |"
                    )
                lines.append("")
            if metric.get("metric_filters"):
                lines.extend(["| 指标内条件 | 条件口径 |", "|---|---|"])
                for row in metric.get("metric_filters", []):
                    lines.append(
                        f"| {markdown_cell(row.get('label', ''))} | {markdown_cell(row.get('business_effect', ''))} |"
                    )
                lines.append("")
            if metric.get("metric_confirmations"):
                lines.append("- 本指标待确认:")
                for item in metric.get("metric_confirmations", []):
                    lines.append(f"  - {item.get('question', '')}: {item.get('reason', '')}")
                lines.append("")
    else:
        lines.append("- 未识别到指标卡；请查看代码视角的最终 SELECT 和结果字段证据。")
    lines.extend(["", "### 指标公共筛选范围", ""])
    if common_filters:
        lines.extend(["| 筛选 | 作用范围 | 当前口径 | 审核重点 |", "|---|---|---|---|"])
        for item in common_filters:
            lines.append(
                f"| {markdown_cell(item.get('label', ''))} | {markdown_cell(item.get('scope', ''))} | "
                f"{markdown_cell(item.get('business_effect', ''))} | {markdown_cell(item.get('review_focus', ''))} |"
            )
    else:
        lines.append("- 未识别到 GameMode/iZoneAreaID/BattleSrvId/道具ID 等核心业务筛选。")
    lines.extend(["", "### 待确认项", ""])
    if shared_confirmations:
        lines.extend(["| 指标 | 问题 | 原因 | 证据 |", "|---|---|---|---|"])
        for item in shared_confirmations:
            lines.append(
                f"| {markdown_cell(item.get('metric_name', ''))} | {markdown_cell(item.get('question', ''))} | "
                f"{markdown_cell(item.get('reason', ''))} | {markdown_cell(item.get('evidence_ref', ''))} |"
            )
    else:
        lines.append("- 暂无额外确认项；仍建议抽看结果样例量级。")
    lines.extend(["", "### SQL 证据折叠区", ""])
    if evidence_sections:
        for section in evidence_sections:
            lines.extend(
                [
                    f"<details><summary>{markdown_cell(section.get('title', 'SQL 证据'))}</summary>",
                    "",
                    f"- 摘要: {section.get('summary', '')}",
                    "",
                ]
            )
            for item in section.get("items", []):
                lines.append(f"- {item}")
            lines.extend(["", "</details>", ""])
    else:
        lines.append("- 证据详见 `sql_review_code.md` 的代码视角。")
    return "\n".join(lines).rstrip() + "\n"


def render_product_report(
    directory: Path,
    reviews: list[FileReview],
    project_name: str,
    project_root: Path | None,
    roles: ReviewRoleContext,
) -> str:
    lines = [
        "# SQL Review Product View",
        "",
        f"- directory: `{directory}`",
        f"- project: `{project_name or 'not specified'}`",
        f"- project_root: `{project_root if project_root else 'not provided'}`",
        f"- generated_at: `{now_iso()}`",
        "- purpose: 给业务/DA/人工 reviewer 看，专注这份 SQL 到底在算什么、Base 是谁、指标口径和筛选是否正确。代码细节见 `sql_review_code.md` 和 `sql_review.json`。",
        "",
        "## 先看这里",
        "",
        "| SQL File | 一句话 | Base | 需要确认 |",
        "|---|---|---|---|",
    ]
    for review in sorted(reviews, key=lambda item: item.path.name.lower()):
        story = build_product_view(review, roles)
        lines.append(
            f"| `{relative_name(directory, review.path)}` | {markdown_cell(story['one_sentence'])} | "
            f"{markdown_cell(story['base'])} | {markdown_cell('；'.join(story['unknowns_to_confirm'][:3]) or '暂无')} |"
        )
    lines.append("")
    for review in sorted(reviews, key=lambda item: item.path.name.lower()):
        lines.append(render_product_view_markdown(review, roles))
    return "\n".join(lines).rstrip() + "\n"


def render_product_summary_report(
    root: Path,
    reviews: list[FileReview],
    project_name: str,
    project_root: Path | None,
    roles: ReviewRoleContext,
) -> str:
    lines = [
        "# SQL Review Product Summary",
        "",
        f"- batch_root: `{root}`",
        f"- project: `{project_name or 'not specified'}`",
        f"- project_root: `{project_root if project_root else 'not provided'}`",
        f"- generated_at: `{now_iso()}`",
        "- purpose: 批次级产品视角摘要。完整代码视角见 `sql_review_code.md`、`sql_review.json`、`sql_review.html`。",
        "",
        "| SQL File | 分析形态 | 一句话 | Base | 关键确认 |",
        "|---|---|---|---|---|",
    ]
    for review in sorted(reviews, key=lambda item: item.path.as_posix().lower()):
        story = build_product_view(review, roles)
        confirmations = product_view_confirmation_texts(story)
        lines.append(
            f"| `{relative_name(root, review.path)}` | {markdown_cell(story['analysis_pattern'])} | "
            f"{markdown_cell(story['one_sentence'])} | {markdown_cell(story['base'])} | "
            f"{markdown_cell('；'.join(confirmations[:3]) or '暂无')} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def markdown_issue_list(items: list[dict], empty: str = "无") -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- **{item['priority']} {item['title']}**：{item['detail']}" for item in items)


def render_reviewer_card_markdown(review: FileReview, roles: ReviewRoleContext) -> str:
    card = reviewer_card(review, roles)
    lines = [
        f"- 审核结论: **{review_bucket_label(card['bucket'])}**",
        f"- 先做什么: {card['action']}",
        f"- 为什么: {card['why']}",
        f"- 执行环境: `{card['execution_brief']}`",
        f"- 结果证据: {card['result_brief']}",
        "- 审核步骤:",
    ]
    lines.extend(f"  - {step}" for step in card["reviewer_steps"])
    lines.extend(["- 阻断项:", markdown_issue_list(card["blockers"])])
    lines.extend(["- 待确认项:", markdown_issue_list(card["confirmations"])])
    lines.extend(["- 非阻断提醒:", markdown_issue_list(card["notes"])])
    return "\n".join(lines)


def render_reviewer_queue(
    root: Path,
    reviews: list[FileReview],
    roles: ReviewRoleContext,
    include_files: bool = True,
) -> str:
    grouped: dict[str, list[FileReview]] = defaultdict(list)
    for review in reviews:
        grouped[reviewer_card(review, roles)["bucket"]].append(review)
    lines = [
        "## 审核者先看这里",
        "",
        "- 这份报告按“查询 SQL / 代理跑数材料”审核；不是正式看板部署审核。",
        "- 处理顺序固定为：P0 阻断 -> P1 证据/口径确认 -> P2 代理验证和可维护性 -> 进入下一生命周期。",
        "- 底层机器推断和 raw SQL 规则放在 Appendix，日常审核先看本节和逐文件审核卡。",
        "",
        "### 处理队列",
        "",
        "| 队列 | 数量 | 文件 |",
        "|---|---:|---|",
    ]
    for bucket in sorted(grouped, key=review_bucket_sort_key):
        files = ", ".join(f"`{relative_name(root, review.path)}`" for review in grouped[bucket][:8])
        if len(grouped[bucket]) > 8:
            files += f"，另 {len(grouped[bucket]) - 8} 个"
        lines.append(f"| {review_bucket_label(bucket)} | {len(grouped[bucket])} | {files or 'none'} |")
    if not grouped:
        lines.append("| 无 | 0 | none |")
    if include_files:
        lines.extend(
            [
                "",
                "### 逐文件审核卡",
                "",
                "| SQL File | 审核结论 | 先做什么 | 为什么 | 结果证据 |",
                "|---|---|---|---|---|",
            ]
        )
        for review in sorted(reviews, key=lambda item: item.path.as_posix().lower()):
            card = reviewer_card(review, roles)
            lines.append(
                f"| `{relative_name(root, review.path)}` | **{review_bucket_label(card['bucket'])}** | "
                f"{markdown_cell(card['action'])} | {markdown_cell(card['why'])} | {markdown_cell(card['result_brief'])} |"
            )
    return "\n".join(lines)


def review_action_queue(root: Path, reviews: list[FileReview], roles: ReviewRoleContext) -> list[dict]:
    grouped: dict[str, list[FileReview]] = defaultdict(list)
    for review in reviews:
        grouped[reviewer_card(review, roles)["bucket"]].append(review)
    queue: list[dict] = []
    for bucket in sorted(grouped, key=review_bucket_sort_key):
        bucket_reviews = sorted(grouped[bucket], key=lambda item: item.path.as_posix().lower())
        cards = [reviewer_card(review, roles) for review in bucket_reviews]
        product_actions = [
            product_next_focus(build_product_view(review, roles), review, roles)
            for review in bucket_reviews
        ]
        queue.append(
            {
                "bucket": bucket,
                "label": review_bucket_label(bucket),
                "count": len(bucket_reviews),
                "files": [relative_name(root, review.path) for review in bucket_reviews],
                "top_actions": unique_in_order(product_actions + [card["action"] for card in cards])[:5],
            }
        )
    return queue


def render_code_report(
    directory: Path,
    reviews: list[FileReview],
    min_shared: int,
    project_name: str = "",
    project_root: Path | None = None,
    canonical_rules: list[CanonicalRule] | None = None,
    orphan_results: list[Path] | None = None,
    roles: ReviewRoleContext | None = None,
) -> str:
    roles = roles or ReviewRoleContext()
    canonical_rules = canonical_rules or []
    orphan_results = orphan_results or []
    rule_to_files: dict[str, set[Path]] = defaultdict(set)
    rule_by_key: dict[str, RuleCandidate] = {}
    for review in reviews:
        for rule in review.rules:
            key = f"{rule.kind}:{rule.normalized}"
            rule_to_files[key].add(review.path)
            rule_by_key.setdefault(key, rule)
    shared_keys = {
        key
        for key, files in rule_to_files.items()
        if len(files) >= min_shared
    }
    grade_counts = Counter(review.grade for review in reviews)
    blocker_count = sum(
        1
        for review in reviews
        for finding in review.findings
        if finding.severity == "BLOCKER"
    )
    warn_count = sum(
        1
        for review in reviews
        for finding in review.findings
        if finding.severity == "WARN"
    )
    rule_check_counts = Counter(
        check.result
        for review in reviews
        for check in review.rule_checks
    )
    result_counts = Counter(result_status(review) for review in reviews)
    evidence_counts = Counter(review.evidence_status for review in reviews)
    dimension_counts = dimension_status_counts(reviews, roles)
    active_rule_count = sum(1 for rule in canonical_rules if rule.status in {"confirmed", "proposed"})
    review_mode = "project-aware SQL review" if active_rule_count else "pure SQL review"
    project_note = (
        "Loaded saved project rules and checked SQL inferred口径 against them."
        if active_rule_count
        else "No saved project constraints were loaded; downgraded to pure SQL-only review."
    )

    lines = [
        "# SQL Review Code View",
        "",
        f"- directory: `{directory}`",
        f"- project: `{project_name or 'not specified'}`",
        f"- project_root: `{project_root if project_root else 'not provided'}`",
        f"- generated_at: `{now_iso()}`",
        f"- sql_file_count: `{len(reviews)}`",
        "- purpose: 给 SQL/工程/AI 治理看，专注 SQL 质量、证据链、字段对齐、口径 trace、性能和隐私。",
        f"- review_method: {review_mode}; inferred口径 must be confirmed before becoming canonical rules.",
        f"- project_rule_note: {project_note}",
        f"- evidence_status: target_reviewed={evidence_counts.get('target_reviewed', 0)}, proxy_reviewed_needs_target_verification={evidence_counts.get('proxy_reviewed_needs_target_verification', 0)}, field_mismatch={evidence_counts.get('field_mismatch', 0)}, missing={evidence_counts.get('missing_result_file', 0)}",
        f"- review_dimensions: logic={format_status_counts(dimension_counts['logic'])}; code_quality={format_status_counts(dimension_counts['code_quality'])}; evidence={format_status_counts(dimension_counts['evidence'])}; dashboard_fit={format_status_counts(dimension_counts['dashboard_fit'])}; deployment_gate={format_status_counts(dimension_counts['deployment_gate'])}",
        "",
        "## Role Context",
        "",
        render_role_context(roles),
        "",
        "## Overall Quality",
        "",
        f"- grades: A={grade_counts.get('A', 0)}, B={grade_counts.get('B', 0)}, C={grade_counts.get('C', 0)}, D={grade_counts.get('D', 0)}",
        f"- blockers: {blocker_count}",
        f"- warnings: {warn_count}",
        f"- result_files: matched={result_counts.get('matched', 0)}, field_mismatch={result_counts.get('field_mismatch', 0)}, missing={result_counts.get('missing_result_file', 0)}, read_issue={sum(count for status, count in result_counts.items() if status.startswith('result_'))}",
        f"- orphan_result_files: {len(orphan_results)}",
        "",
        "## Project Rule Check",
        "",
    ]
    if active_rule_count:
        lines.extend(
            [
                f"- active_saved_rules: `{active_rule_count}`",
                f"- matched: `{rule_check_counts.get('matched', 0)}`",
                f"- conflicts: `{rule_check_counts.get('conflict', 0)}`",
                f"- proposed_conflicts: `{rule_check_counts.get('proposed_conflict', 0)}`",
                f"- needs_manual_check: `{rule_check_counts.get('needs_manual_check', 0)}`",
                "",
            ]
        )
        for review in sorted(reviews, key=lambda item: item.path.name.lower()):
            lines.extend(
                [
                    f"### `{relative_name(directory, review.path)}`",
                    "",
                    render_rule_checks(review.rule_checks),
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "- No project canonical rules were available for conflict checking.",
                "- This report only reviews SQL-internal quality, logic, and inferred口径.",
                "",
            ]
        )

    lines.extend(["## Five-Part Review Matrix", ""])
    lines.append("| SQL File | 逻辑/口径 | 代码质量 | 结果证据 | 看板适配 | 部署门禁 | Inferred Execution | Next Focus |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for review in sorted(reviews, key=lambda item: item.path.name.lower()):
        dims = review_dimensions(review, roles)
        next_focus = next_focus_for(review, roles)
        lines.append(
            f"| `{relative_name(directory, review.path)}` | `{dims['logic']['status']}` / `{dims['logic']['value']}` | "
            f"`{dims['code_quality']['status']}` / `{dims['code_quality']['value']}` | "
            f"`{dims['evidence']['status']}` / `{dims['evidence']['value']}` | "
            f"`{dims['dashboard_fit']['status']}` / `{dims['dashboard_fit']['value']}` | "
            f"`{dims['deployment_gate']['status']}` / `{dims['deployment_gate']['value']}` | "
            f"`{project_label(review.execution_project)}` | {markdown_cell(next_focus)} |"
        )
    lines.append("")

    lines.extend(["## Result File Coverage", ""])
    lines.append("| SQL File | Result Status | Result File | Rows | Column Issues |")
    lines.append("|---|---|---|---|---|")
    for review in sorted(reviews, key=lambda item: item.path.name.lower()):
        result = review.result_file
        if result is None:
            result_file = "none"
            rows = "unknown"
            issues = "missing_result_file"
        else:
            result_file = relative_name(directory, result.path)
            rows = result.row_count if result.row_count is not None else "unknown"
            issue_parts = []
            if result.missing_columns:
                issue_parts.append("missing: " + ", ".join(result.missing_columns))
            if result.extra_columns:
                issue_parts.append("extra: " + ", ".join(result.extra_columns))
            if result.order_mismatch:
                issue_parts.append("order_mismatch")
            if result.status != "loaded":
                issue_parts.append(result.status)
            issues = "; ".join(issue_parts) or "none"
        lines.append(
            f"| `{relative_name(directory, review.path)}` | `{result_status(review)}` | `{markdown_cell(result_file)}` | `{rows}` | {markdown_cell(issues)} |"
        )
    lines.extend(["", "### Orphan Result Files", ""])
    if orphan_results:
        for path in sorted(orphan_results, key=lambda item: item.as_posix().lower()):
            lines.append(f"- orphan_result_file: `{relative_name(directory, path)}`")
    else:
        lines.append("- none")
    lines.append("")

    lines.extend(
        [
        "## Merged Business Rules",
        "",
        "### Shared Rules",
        "",
        ]
    )
    if shared_keys:
        lines.append("| Type | Rule | SQL Files |")
        lines.append("|---|---|---|")
        for key in sorted(shared_keys, key=lambda item: (rule_by_key[item].kind, rule_by_key[item].normalized)):
            rule = rule_by_key[key]
            files = ", ".join(
                f"`{relative_name(directory, path)}`"
                for path in sorted(rule_to_files[key], key=lambda item: item.as_posix().lower())
            )
            lines.append(f"| `{rule.kind}` | {rule_display(rule)} | {files} |")
    else:
        lines.append("- No repeated rule candidates found in this directory.")

    lines.extend(["", "### Unique Rules By SQL", ""])
    for review in sorted(reviews, key=lambda item: item.path.name.lower()):
        unique_rules = [
            rule
            for rule in review.rules
            if f"{rule.kind}:{rule.normalized}" not in shared_keys
        ]
        lines.append(f"#### `{relative_name(directory, review.path)}`")
        if unique_rules:
            for rule in unique_rules:
                lines.append(f"- `{rule.kind}`: {rule.text}")
        else:
            lines.append("- No unique rule candidates beyond shared rules.")
        lines.append("")

    lines.extend(["## SQL Details", ""])
    for review in sorted(reviews, key=lambda item: item.path.name.lower()):
        lines.extend(
            [
                f"### `{relative_name(directory, review.path)}`",
                "",
                "#### Role Context",
                "",
                render_file_role_context(review, roles),
                "",
                "#### SQL Summary",
                "",
                f"- inferred_category: `{review.business_category or DEFAULT_BUSINESS_CATEGORY}`",
                f"- inferred_analysis_type: `{review.analysis_type or DEFAULT_ANALYSIS_TYPE}`",
                f"- grain: `{review.grain}`",
                f"- time_grain: `{review.time_grain}`",
                f"- target_tables: `{', '.join(review.target_tables) or 'none'}`",
                f"- source_tables: `{', '.join(review.tables) or 'unknown'}`",
                f"- parameters: `{', '.join(review.parameters) or 'none'}`",
                f"- cte_count: `{review.cte_count}`",
                f"- join_count: `{review.join_count}`",
                f"- performance_tier: `{review.performance_preflight.get('tier', 'unknown')}`",
                f"- performance_score: `{review.performance_preflight.get('score', 0)}`",
                "",
                "#### Performance Preflight",
                "",
                render_performance_preflight_markdown(review.performance_preflight),
                "",
                "#### Business Filter SQL Evidence",
                "",
                render_business_filters_markdown(review.business_filters),
                "",
                "#### Metric Expression And Lineage Trace",
                "",
                render_metric_logic(review),
                "",
                "#### Dimensions",
                "",
                markdown_list(review.dimensions),
                "",
                "#### Final Output Fields",
                "",
                markdown_list(review.final_fields),
                "",
                "#### Query Result File",
                "",
                render_result_file(review, directory),
                "",
                "#### Quality Review",
                "",
                f"- grade: `{review.grade}`",
                f"- has_count_distinct: `{str(review.has_count_distinct).lower()}`",
                f"- has_window_function: `{str(review.has_window_function).lower()}`",
                f"- has_global_order_by: `{str(review.has_global_order_by).lower()}`",
                "",
                markdown_findings(review.findings),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def resolve_input_paths(paths: list[Path], inbox_root: Path) -> list[Path]:
    resolved_paths = []
    for path in paths:
        if path.is_absolute():
            resolved_paths.append(path.resolve())
            continue
        inbox_candidate = inbox_root / path
        if inbox_candidate.exists():
            resolved_paths.append(inbox_candidate.resolve())
            continue
        resolved_paths.append(path.resolve())
    return resolved_paths


def discover_sql_files(paths: list[Path], pattern: str, recursive: bool) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved.is_file():
            if resolved.suffix.lower() == ".sql":
                files.append(resolved)
            continue
        if not resolved.is_dir():
            raise SystemExit(f"Path not found: {path}")
        iterator = resolved.rglob(pattern) if recursive else resolved.glob(pattern)
        files.extend(item.resolve() for item in iterator if item.is_file() and item.suffix.lower() == ".sql")
    return sorted(set(files), key=lambda item: item.as_posix().lower())


def discover_result_files(paths: list[Path], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved.is_file():
            if resolved.suffix.lower() in RESULT_EXTENSIONS:
                files.append(resolved)
            elif resolved.suffix.lower() == ".sql":
                files.extend(
                    item.resolve()
                    for item in resolved.parent.glob(f"{resolved.stem}.*")
                    if item.is_file() and item.suffix.lower() in RESULT_EXTENSIONS
                )
            continue
        if not resolved.is_dir():
            raise SystemExit(f"Path not found: {path}")
        iterator = resolved.rglob("*") if recursive else resolved.glob("*")
        files.extend(item.resolve() for item in iterator if item.is_file() and item.suffix.lower() in RESULT_EXTENSIONS)
    return sorted(set(files), key=lambda item: item.as_posix().lower())


def result_key(path: Path) -> tuple[str, str]:
    return (path.parent.resolve().as_posix().lower(), path.stem.lower())


def map_result_files(result_files: list[Path]) -> dict[tuple[str, str], list[Path]]:
    grouped: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for path in result_files:
        grouped[result_key(path)].append(path)
    return grouped


def review_evidence_for(path: Path) -> dict:
    return {
        "current_role": "review_subject",
        "review_subject": "current_sql",
        "result_evidence_role": "missing_result",
        "sql_files": [path.name],
        "result_files": [],
    }


def orphan_result_files(result_files: list[Path], sql_files: list[Path]) -> list[Path]:
    sql_keys = {result_key(path) for path in sql_files}
    return [path for path in result_files if result_key(path) not in sql_keys]


def summary_root_for(paths: list[Path]) -> Path:
    bases = [path if path.is_dir() else path.parent for path in paths]
    if not bases:
        return Path.cwd()
    if len(bases) == 1:
        return bases[0]
    return Path(os.path.commonpath([str(path) for path in bases]))


def render_summary_report(
    root: Path,
    reviews: list[FileReview],
    orphan_results: list[Path],
    project_name: str,
    project_root: Path | None,
    roles: ReviewRoleContext | None = None,
) -> str:
    roles = roles or ReviewRoleContext()
    status_counts = Counter(result_status(review) for review in reviews)
    grade_counts = Counter(review.grade for review in reviews)
    evidence_counts = Counter(review.evidence_status for review in reviews)
    dimension_counts = dimension_status_counts(reviews, roles)
    lines = [
        "# SQL Review Summary",
        "",
        f"- batch_root: `{root}`",
        f"- project: `{project_name or 'not specified'}`",
        f"- project_root: `{project_root if project_root else 'not provided'}`",
        "- review_entry: `SQL审查`",
        "- review_output_model: `product_view + code_view`",
        f"- definition_project: `{project_label(roles.definition)}`",
        f"- delivery_project: `{project_label(roles.delivery)}`",
        f"- generated_at: `{now_iso()}`",
        f"- sql_file_count: `{len(reviews)}`",
        f"- orphan_result_files: `{len(orphan_results)}`",
        f"- result_status: matched={status_counts.get('matched', 0)}, field_mismatch={status_counts.get('field_mismatch', 0)}, missing={status_counts.get('missing_result_file', 0)}",
        f"- evidence_status: target_reviewed={evidence_counts.get('target_reviewed', 0)}, proxy_reviewed_needs_target_verification={evidence_counts.get('proxy_reviewed_needs_target_verification', 0)}, field_mismatch={evidence_counts.get('field_mismatch', 0)}, missing={evidence_counts.get('missing_result_file', 0)}",
        f"- review_dimensions: logic={format_status_counts(dimension_counts['logic'])}; code_quality={format_status_counts(dimension_counts['code_quality'])}; evidence={format_status_counts(dimension_counts['evidence'])}; dashboard_fit={format_status_counts(dimension_counts['dashboard_fit'])}; deployment_gate={format_status_counts(dimension_counts['deployment_gate'])}",
        f"- grades: A={grade_counts.get('A', 0)}, B={grade_counts.get('B', 0)}, C={grade_counts.get('C', 0)}, D={grade_counts.get('D', 0)}",
        "",
        render_reviewer_queue(root, reviews, roles),
        "",
        "## SQL Files",
        "",
        "| SQL File | 逻辑/口径 | 代码质量 | 结果证据 | 看板适配 | 部署门禁 | Execution | Result File | Rows | Next Focus |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for review in sorted(reviews, key=lambda item: item.path.as_posix().lower()):
        result = review.result_file
        result_file = relative_name(root, result.path) if result else "none"
        rows = result.row_count if result and result.row_count is not None else "unknown"
        dims = review_dimensions(review, roles)
        story = build_product_view(review, roles)
        lines.append(
            f"| `{relative_name(root, review.path)}` | `{dims['logic']['status']}` | `{dims['code_quality']['status']}` / `{review.grade}` | "
            f"`{dims['evidence']['status']}` / `{dims['evidence']['value']}` | `{dims['dashboard_fit']['status']}` | "
            f"`{dims['deployment_gate']['status']}` / `{dims['deployment_gate']['value']}` | `{project_label(review.execution_project)}` | "
            f"`{markdown_cell(result_file)}` | `{rows}` | {markdown_cell(product_next_focus(story, review, roles))} |"
        )
    lines.extend(["", "## Orphan Result Files", ""])
    if orphan_results:
        for path in sorted(orphan_results, key=lambda item: item.as_posix().lower()):
            lines.append(f"- `{relative_name(root, path)}`")
    else:
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def result_payload(root: Path, review: FileReview) -> dict:
    result = review.result_file
    if result is None:
        return {
            "status": "missing_result_file",
            "path": "",
            "pairing_method": review.result_pairing_method or "missing",
            "file_type": "",
            "row_count": None,
            "columns": [],
            "sample_rows": [],
            "missing_columns": [],
            "extra_columns": [],
            "order_mismatch": False,
            "alternatives": [],
            "note": "未找到同目录同名结果文件。",
        }
    return {
        "status": result.status,
        "path": relative_name(root, result.path),
        "pairing_method": review.result_pairing_method or "exact_stem",
        "file_type": result.file_type,
        "row_count": result.row_count,
        "columns": result.columns,
        "sample_rows": result.sample_rows,
        "missing_columns": result.missing_columns,
        "extra_columns": result.extra_columns,
        "order_mismatch": result.order_mismatch,
        "alternatives": [relative_name(root, path) for path in result.alternatives],
        "note": result.note,
    }


def review_payload(
    root: Path,
    reviews: list[FileReview],
    orphan_results: list[Path],
    project_name: str,
    project_root: Path | None,
    roles: ReviewRoleContext,
) -> dict:
    dimension_counts = dimension_status_counts(reviews, roles)
    items = []
    for review in sorted(reviews, key=lambda item: item.path.as_posix().lower()):
        product_view = build_product_view(review, roles)
        code_view = build_code_view(root, review, roles)
        items.append(
            {
                "path": relative_name(root, review.path),
                "name": review.path.name,
                "grade": review.grade,
                "dimensions": review_dimensions(review, roles),
                "next_focus": product_next_focus(product_view, review, roles),
                "product_digest": {
                    "confirmations": product_view_confirmation_texts(product_view),
                    "semantic_review_status": product_view.get("semantic_review_status", ""),
                },
                "product_view": product_view,
                "product_review_evidence": review.product_review_evidence,
                "code_view": code_view,
            }
        )
    return {
        "schema_version": SQL_REVIEW_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "batch_root": str(root),
        "project": project_name or "not specified",
        "project_root": str(project_root) if project_root else "",
        "review_entry": "SQL审查",
        "review_output_model": ["product_view", "code_view"],
        "action_queue": review_action_queue(root, reviews, roles),
        "role_context": {
            "definition_project": project_label(roles.definition),
            "delivery_project": project_label(roles.delivery),
            "execution_project_candidates": [project_label(project) for project in roles.execution_projects],
        },
        "summary": {
            "sql_file_count": len(reviews),
            "orphan_result_files": len(orphan_results),
            "review_card_counts": dict(review_card_counts(reviews, roles)),
            "dimension_counts": {key: dict(counts) for key, counts in dimension_counts.items()},
            "grade_counts": dict(Counter(review.grade for review in reviews)),
            "evidence_counts": dict(Counter(review.evidence_status for review in reviews)),
        },
        "items": items,
        "orphan_result_files": [relative_name(root, path) for path in sorted(orphan_results, key=lambda item: item.as_posix().lower())],
    }


def render_json_report(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_html_viewer(payload: dict | None, api_url: str | None = None) -> str:
    if payload is None:
        payload = {
            "schema_version": SQL_REVIEW_SCHEMA_VERSION,
            "project": "",
            "batch_root": "",
            "generated_at": "",
            "summary": {"sql_file_count": 0, "dimension_counts": {}},
            "action_queue": [],
            "items": [],
        }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    api_url_data = json.dumps(api_url, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SQL Review</title>
  <style>
    :root {{ --bg:#f6f7f9; --panel:#fff; --line:#d9dee7; --text:#17202a; --muted:#667085; --accent:#1f6feb; --ok:#147d4f; --warn:#a05a00; --bad:#b42318; --na:#667085; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:"Segoe UI", Arial, sans-serif; background:var(--bg); color:var(--text); }}
    header {{ height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 16px; border-bottom:1px solid var(--line); background:var(--panel); }}
    h1 {{ margin:0; font-size:18px; }}
    main {{ display:grid; grid-template-columns:minmax(320px,420px) minmax(860px,1fr); min-height:calc(100vh - 56px); }}
    aside {{ border-right:1px solid var(--line); background:var(--panel); min-width:0; }}
    .toolbar {{ padding:12px; border-bottom:1px solid var(--line); display:grid; gap:8px; }}
    input, select {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:8px 10px; font:inherit; background:white; }}
    .list {{ overflow:auto; max-height:calc(100vh - 122px); }}
    .item {{ padding:12px; border-bottom:1px solid var(--line); cursor:pointer; display:grid; gap:7px; }}
    .item.active {{ background:#eaf2ff; box-shadow:inset 3px 0 0 var(--accent); }}
    .title {{ font-weight:650; font-size:14px; word-break:break-word; }}
    .path {{ color:var(--muted); font-size:12px; word-break:break-word; }}
    .content {{ padding:14px; overflow:auto; max-height:calc(100vh - 56px); min-width:0; }}
    .summary {{ display:grid; grid-template-columns:repeat(5,minmax(130px,1fr)); gap:8px; margin-bottom:12px; }}
    .metric, .section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
    .metric {{ padding:10px; }}
    .metric strong {{ display:block; font-size:18px; margin-top:4px; }}
    .section {{ margin-bottom:12px; }}
    .section h2 {{ margin:0; padding:12px 14px; border-bottom:1px solid var(--line); font-size:16px; }}
    .body {{ padding:12px 14px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:10px; }}
    .kv {{ display:grid; grid-template-columns:130px minmax(0,1fr); gap:8px; padding:5px 0; border-bottom:1px solid #edf0f5; font-size:13px; }}
    .kv span:first-child {{ color:var(--muted); }}
    .chips {{ display:flex; gap:5px; flex-wrap:wrap; }}
    .chip {{ display:inline-flex; align-items:center; min-height:22px; padding:2px 7px; border-radius:999px; background:#eef2f7; color:var(--muted); font-size:12px; }}
    .tabs {{ display:flex; gap:8px; margin:0 0 12px; }}
    .tab {{ border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:6px; padding:8px 12px; cursor:pointer; font:inherit; }}
    .tab.active {{ border-color:var(--accent); background:#eaf2ff; color:var(--accent); font-weight:650; }}
    .pass {{ background:#e8f6ef; color:var(--ok); }}
    .warn {{ background:#fff3dd; color:var(--warn); }}
    .fail {{ background:#fff0ee; color:var(--bad); }}
    .not_applicable {{ background:#eef2f7; color:var(--na); }}
    .table-scroll {{ width:100%; overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    .wide-table {{ min-width:1480px; table-layout:fixed; }}
    .metric-logic-table {{ min-width:1560px; }}
    .metric-logic-table col.metric-col {{ width:180px; }}
    .metric-logic-table col.definition-col {{ width:260px; }}
    .metric-logic-table col.base-col {{ width:220px; }}
    .metric-logic-table col.numerator-col {{ width:220px; }}
    .metric-logic-table col.denominator-col {{ width:220px; }}
    .metric-logic-table col.formula-col {{ width:220px; }}
    .metric-logic-table col.source-col {{ width:140px; }}
    th, td {{ padding:7px 8px; border-bottom:1px solid #edf0f5; text-align:left; vertical-align:top; line-height:1.45; }}
    .wide-table th, .wide-table td {{ overflow-wrap:break-word; word-break:normal; }}
    .metric-name-cell, .metric-name-head {{ min-width:160px; overflow-wrap:normal; word-break:keep-all; }}
    .metric-name-cell strong {{ display:block; min-width:120px; }}
    .risk-list {{ display:grid; gap:10px; }}
    .risk-card {{ border:1px solid var(--line); border-radius:8px; background:#fff; padding:12px; }}
    .risk-card-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:10px; }}
    .risk-card-head strong {{ font-size:15px; }}
    .risk-card-head p {{ margin:4px 0 0; color:var(--muted); line-height:1.55; }}
    .risk-fields {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:8px 12px; }}
    .risk-field {{ border-top:1px solid #edf0f5; padding-top:8px; min-width:0; }}
    .risk-field span:first-child {{ display:block; color:var(--muted); font-size:12px; margin-bottom:3px; }}
    .risk-field p {{ margin:0; overflow-wrap:anywhere; line-height:1.55; }}
    .important-kv {{ background:#fffaf0; border:1px solid #fde8bd; border-radius:8px; padding:8px 10px; margin:8px 0; }}
    .logic-first {{ border-color:#b8c7dd; }}
    .logic-first h2 {{ background:#f3f7fc; }}
    .logic-path {{ margin:10px 0; }}
    .logic-path h3, .logic-first h3 {{ margin:12px 0 8px; font-size:14px; }}
    .logic-path ol {{ margin:0; padding-left:22px; }}
    .logic-path li {{ margin:5px 0; line-height:1.6; }}
    .logic-cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:10px; margin-bottom:10px; }}
    .logic-card {{ border:1px solid var(--line); border-radius:8px; background:#fff; padding:10px 12px; }}
    .logic-card h3 {{ margin:0 0 8px; font-size:14px; }}
    .logic-card .kv {{ grid-template-columns:88px minmax(0,1fr); }}
    .condition-list {{ margin:0; padding-left:18px; }}
    .condition-list li {{ margin:2px 0; line-height:1.55; overflow-wrap:anywhere; }}
    .source-card-list {{ display:grid; gap:10px; margin-top:8px; }}
    .source-card {{ border:1px solid var(--line); border-radius:8px; background:#fff; padding:10px 12px; }}
    .source-card h3 {{ margin:0 0 8px; font-size:14px; }}
    .source-card-main {{ margin:0 0 10px; line-height:1.6; overflow-wrap:anywhere; }}
    .source-meta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px 12px; }}
    .source-meta div {{ border-top:1px solid #edf0f5; padding-top:7px; min-width:0; }}
    .source-meta label {{ display:block; color:var(--muted); font-size:12px; margin-bottom:3px; }}
    .source-meta p {{ margin:0; line-height:1.5; overflow-wrap:anywhere; }}
    th {{ color:var(--muted); font-weight:600; }}
    ul {{ margin:0; padding-left:18px; }}
    .empty {{ color:var(--muted); font-size:13px; }}
    @media (max-width: 900px) {{ main {{ grid-template-columns:1fr; }} .summary {{ grid-template-columns:1fr 1fr; }} .list {{ max-height:280px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>SQL Review</h1>
    <div class="chips" id="headerMeta"></div>
  </header>
  <main>
    <aside>
      <div class="toolbar">
        <input id="search" placeholder="搜索 SQL / 表 / 指标 / 问题">
        <select id="statusFilter">
          <option value="">全部状态</option>
          <option value="fail">有 fail</option>
          <option value="warn">有 warn</option>
          <option value="not_applicable">部署门禁不适用</option>
        </select>
      </div>
      <div class="list" id="list"></div>
    </aside>
    <section class="content">
      <div class="summary" id="summary"></div>
      <div id="actionQueue"></div>
      <div id="detail"></div>
    </section>
  </main>
  <script>
    let payload = {data};
    const reviewApiUrl = {api_url_data};
    let selected = 0;
    let viewMode = 'product';
    function esc(value) {{ return String(value ?? '').replace(/[&<>"']/g, m => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#039;'}}[m])); }}
    function chip(status, text) {{ return `<span class="chip ${{esc(status)}}">${{esc(text || status)}}</span>`; }}
    function dimChips(item) {{
      const labels = {{logic:'逻辑', code_quality:'质量', evidence:'证据', dashboard_fit:'看板', deployment_gate:'部署'}};
      return Object.entries(labels).map(([key,label]) => chip(item.dimensions[key].status, label + ':' + item.dimensions[key].status)).join('');
    }}
    function hasStatus(item, status) {{ return Object.values(item.dimensions).some(dim => dim.status === status); }}
    function filtered() {{
      const q = document.getElementById('search').value.trim().toLowerCase();
      const status = document.getElementById('statusFilter').value;
      return (payload.items || []).filter(item => {{
        const product = item.product_view || {{}};
        const code = item.code_view || {{}};
        const summary = code.sql_summary || {{}};
        const metricTrace = code.metric_review_trace || {{}};
        const metricHay = (code.metric_logic||[]).flatMap(m => [
          m.metric, m.business_definition, m.base_population, m.numerator, m.denominator, m.formula,
          m.formula_expression, m.numerator_expression, m.denominator_expression,
          ...(m.global_filters||[]), ...(m.metric_filters||[]),
          ...(m.metric_business_filters||[]).flatMap(f => [f.field, f.label, f.business_effect, f.condition, ...(f.values||[]), ...(f.mapping||[]).flatMap(x => [x.value, x.name, x.category])]),
          ...(m.base_business_filters||[]).flatMap(f => [f.field, f.label, f.business_effect, f.condition, ...(f.values||[])]),
          ...(m.join_business_filters||[]).flatMap(f => [f.field, f.label, f.business_effect, f.condition, ...(f.values||[])]),
          ...(m.metric_condition_cards||[]).flatMap(c => [c.business_effect, c.condition])
        ]);
        const reviewHay = [
          product.one_sentence, product.business_question, product.analysis_pattern,
          product.base, product.grouping, ...(product.source_logs||[]),
          product.logic_review?.title, product.logic_review?.summary,
          ...(product.logic_review?.scope||[]), ...(product.logic_review?.key_steps||[]),
          ...(product.logic_review?.events||[]).flatMap(e => [
            e.event_id, e.event_name, e.event_family, e.event_condition, e.id_or_mapping,
            e.statistic_object, e.first_or_final_rule, e.join_or_backfill_rule,
            e.product_interpretation, e.business_risk, ...(e.source_fields||[]),
            ...(e.source_logs_or_tables||[]), ...(e.sql_evidence_refs||[])
          ]),
          ...(product.logic_review?.metrics||[]).flatMap(m => [
            m.metric_name, m.business_meaning, m.numerator, m.denominator, m.formula,
            m.dedup_key, m.grain, m.sql_expression
          ]),
          ...(product.logic_review?.comment_outline||[]).flatMap(o => [o.label, o.description, ...(o.bullets||[])]),
          ...(product.business_scope||[]), ...(product.logic_steps||[]),
          product.execution_evidence?.review_subject, product.execution_evidence?.current_sql_role,
          product.execution_evidence?.result_evidence_role, product.execution_evidence?.selected_result_file,
          product.execution_evidence?.execution_project, product.execution_evidence?.delivery_project,
          product.execution_evidence?.evidence_status, ...(product.execution_evidence?.sql_files||[]),
          ...(product.execution_evidence?.result_files||[]),
          ...(product.business_story_cards||[]).flatMap(c => [c.title, c.body, c.evidence_ref]),
          ...(product.metric_path_cards||[]).flatMap(c => [c.metric_name, c.title, c.body, c.formula, c.base, c.caveat]),
          product.output_contract?.product_check, product.output_contract?.warning,
          ...(product.output_contract?.fields||[]), ...(product.output_contract?.result_columns||[]),
          ...(product.event_contracts||[]).flatMap(e => [
            e.event_id, e.event_name, e.event_family, e.event_condition, e.id_or_mapping, e.statistic_object,
            e.first_or_final_rule, e.join_or_backfill_rule, e.product_interpretation, e.business_risk,
            ...(e.source_logs_or_tables||[]), ...(e.source_fields||[]), ...(e.sql_evidence_refs||[]),
            ...(e.sql_evidence||[]).flatMap(x => [x.ref, x.snippet])
          ]),
          ...(product.event_index||[]).flatMap(e => [
            e.event_id, e.event_name, e.event_condition, e.statistic_object, e.risk_summary,
            ...(e.source_logs_or_tables||[]), ...(e.source_fields||[])
          ]),
          ...(product.risk_register||[]).flatMap(r => [
            r.risk_id, r.title, r.severity, r.description, r.conflict_object, r.sql_current,
            r.expected_or_standard, r.difference, r.impact, r.action,
            ...(r.affected_metrics||[]), ...(r.evidence_refs||[])
          ]),
          ...(product.metric_summary_table||[]).flatMap(m => [
            m.metric_name, m.metric_type, m.business_meaning, m.calculation, m.numerator,
            m.denominator, m.dedup_key, m.grain, m.confidence, m.review_status,
            ...(m.key_conditions||[]), ...(m.event_refs||[]), ...(m.risk_refs||[])
          ]),
          ...(product.review_actions||[]).flatMap(a => [a.action_id, a.source_ref, a.owner_hint, a.action, a.why]),
          ...(product.metric_overview||[]).flatMap(m => [m.metric_name, m.metric_type, m.review_status, m.main_risk, m.confidence]),
          ...(product.metric_cards||[]).flatMap(m => [
            m.metric_name, m.business_meaning, m.metric_type, m.calculation, m.numerator, m.denominator,
            m.dedup_key, m.row_grain_explanation, m.standard_rule_alignment, m.confidence,
            ...(m.aggregation_dimensions||[]), ...(m.key_conditions||[]), ...(m.event_refs||[]), ...(m.risk_refs||[]), ...(m.risk_notes||[]),
            ...(m.source_logs_fields||[]).flatMap(s => [s.role, s.field_expression, s.business_story, ...(s.source_logs_or_tables||[]), ...(s.group_by||[])]),
            ...(m.metric_filters||[]).flatMap(f => [f.label, f.business_effect, f.condition]),
            ...(m.metric_confirmations||[]).flatMap(c => [c.metric_name, c.question, c.reason, c.evidence_ref]),
            ...(m.sql_evidence_refs||[])
          ]),
          ...(product.common_filters||[]).flatMap(f => [f.label, f.scope, f.business_effect, f.review_focus, f.condition]),
          ...(product.shared_confirmations||[]).flatMap(c => [c.metric_name, c.question, c.reason, c.evidence_ref]),
          ...(product.evidence_sections||[]).flatMap(s => [s.title, s.summary, ...(s.items||[])]),
          ...(product.metrics||[]).flatMap(m => [
            m.metric, m.business_definition, m.base, m.numerator, m.denominator, m.calculation,
            m.how_to_review, m.pass_criteria
          ]),
          ...(product.key_filters||[]).flatMap(f => [f.label, f.scope, f.business_effect, f.review_focus]),
          ...(product.unknowns_to_confirm||[]),
          code.reviewer_card?.title, code.reviewer_card?.action, code.reviewer_card?.why,
          metricTrace.summary, metricTrace.common_base, metricTrace.grouping,
          ...(metricTrace.calculation_path||[]),
          metricTrace.business_review?.pattern_label,
          metricTrace.business_review?.business_question,
          ...(metricTrace.business_review?.pattern_review_order||[]),
          metricTrace.business_review?.duration_logic,
          ...(metricTrace.business_review?.primary_review_objects||[]).flatMap(o => [o.name, o.what_to_check, o.how_to_judge, o.pass_criteria]),
          ...(metricTrace.business_review?.pattern_cards||[]).flatMap(o => [o.name, o.what_to_check, o.how_to_judge, o.pass_criteria]),
          ...(metricTrace.business_review?.funnel_review?.steps||[]).flatMap(s => [s.step_name, s.source_table, s.reach_rule, s.how_to_judge]),
          ...(metricTrace.business_review?.distribution_review?.bucket_cards||[]).flatMap(b => [b.field, ...(b.definitions||[]).flatMap(d => [d.bucket, d.business_effect, d.condition])]),
          ...(code.business_filters||[]).flatMap(f => [f.field, f.label, f.business_effect, f.how_to_judge, f.condition, ...(f.values||[]), ...(f.unknown_values||[]), ...(f.mapping||[]).flatMap(m => [m.value, m.name, m.category])]),
          ...(metricTrace.dimension_cards||[]).flatMap(d => [d.field, d.role, d.description]),
          ...(metricTrace.metric_cards||[]).flatMap(m => [
            m.metric, m.business_definition, m.base, m.numerator, m.denominator, m.calculation, m.reviewer_question,
            ...(m.business_filters||[]).flatMap(f => [f.field, f.label, f.business_effect, f.condition, ...(f.values||[])]),
            ...(m.base_business_filters||[]).flatMap(f => [f.field, f.label, f.business_effect, f.condition, ...(f.values||[])]),
            ...(m.metric_business_filters||[]).flatMap(f => [f.field, f.label, f.business_effect, f.condition, ...(f.values||[])]),
            ...(m.join_business_filters||[]).flatMap(f => [f.field, f.label, f.business_effect, f.condition, ...(f.values||[])]),
            ...(m.metric_conditions||[]).flatMap(c => [c.business_effect, c.condition])
          ]),
          ...(metricTrace.review_questions||[])
        ];
        const hay = [item.path, item.name, item.next_focus, ...(summary.source_tables||[]), ...(summary.metrics||[]), ...(summary.final_fields||[]), ...metricHay, ...reviewHay, ...(code.findings||[]).map(f => f.message)].join(' ').toLowerCase();
        if (q && !hay.includes(q)) return false;
        if (status === 'not_applicable') return item.dimensions.deployment_gate.status === 'not_applicable';
        if (status && !hasStatus(item, status)) return false;
        return true;
      }});
    }}
    function renderHeader() {{
      const summary = payload.summary || {{}};
      document.getElementById('headerMeta').innerHTML = [
        chip('', payload.project || 'SQL Review'),
        chip('', (summary.sql_file_count || 0) + ' SQL'),
        chip('', payload.generated_at || ''),
        chip('', 'root: ' + (payload.batch_root || ''))
      ].join('');
    }}
    function renderSummary() {{
      const labels = {{logic:'逻辑/口径', code_quality:'代码质量', evidence:'结果证据', dashboard_fit:'看板适配', deployment_gate:'部署门禁'}};
      const summary = payload.summary || {{}};
      const dimensionCounts = summary.dimension_counts || {{}};
      document.getElementById('summary').innerHTML = Object.entries(labels).map(([key,label]) => {{
        const counts = dimensionCounts[key] || {{}};
        return `<div class="metric"><span>${{esc(label)}}</span><strong>${{Object.entries(counts).map(([s,c]) => s + '=' + c).join(' / ') || 'none'}}</strong></div>`;
      }}).join('');
    }}
    function renderActionQueue() {{
      const queue = payload.action_queue || [];
      const rows = queue.map(item => '<tr><td><strong>' + esc(item.label || item.bucket) + '</strong></td><td>' + esc(item.count || 0) + '</td><td>' + esc((item.files || []).slice(0, 8).join(', ')) + '</td><td>' + list(item.top_actions) + '</td></tr>').join('');
      document.getElementById('actionQueue').innerHTML = '<div class="section"><h2>处理队列</h2><div class="body"><table><thead><tr><th>队列</th><th>数量</th><th>文件</th><th>优先动作</th></tr></thead><tbody>' + (rows || '<tr><td colspan="4" class="empty">无</td></tr>') + '</tbody></table></div></div>';
    }}
    function renderList() {{
      const rows = filtered();
      if (selected >= rows.length) selected = 0;
      document.getElementById('list').innerHTML = rows.map((item, idx) => `<div class="item ${{idx===selected?'active':''}}" onclick="selected=${{idx}}; render()">
        <div class="title">${{esc(item.name)}}</div>
        <div class="path">${{esc(item.path)}}</div>
        <div class="chips">${{dimChips(item)}}</div>
      </div>`).join('') || '<div class="empty" style="padding:12px;">没有匹配 SQL</div>';
    }}
    function renderDim(item, key, label) {{
      const dim = item.dimensions[key];
      return `<div class="section"><h2>${{esc(label)}} ${{chip(dim.status, dim.status)}} ${{chip('', dim.value)}}</h2><div class="body"><ul>${{(dim.items||[]).map(v => '<li>' + esc(v) + '</li>').join('') || '<li>none</li>'}}</ul></div></div>`;
    }}
    function renderTableRows(rows) {{
      if (!rows || !rows.length) return '<div class="empty">无样例</div>';
      const cols = Object.keys(rows[0]);
      return '<table><thead><tr>' + cols.map(c => '<th>' + esc(c) + '</th>').join('') + '</tr></thead><tbody>' +
        rows.map(row => '<tr>' + cols.map(c => '<td>' + esc(row[c]) + '</td>').join('') + '</tr>').join('') + '</tbody></table>';
    }}
    function list(values) {{
      const rows = (values || []).filter(Boolean);
      if (!rows.length) return '<span class="empty">none</span>';
      return '<ul>' + rows.map(v => '<li>' + esc(v) + '</li>').join('') + '</ul>';
    }}
    function renderIssueList(items) {{
      const rows = items || [];
      if (!rows.length) return '<span class="empty">无</span>';
      return '<ul>' + rows.map(v => '<li><strong>' + esc(v.priority + ' ' + v.title) + '</strong>：' + esc(v.detail) + '</li>').join('') + '</ul>';
    }}
    function renderSavedRuleChecks(checks) {{
      const rows = checks || [];
      if (!rows.length) return '<span class="empty">无</span>';
      return '<ul>' + rows.map(r => '<li><strong>' + esc((r.title || r.rule_id || '') + ' [' + (r.result || '') + ']') + '</strong>' +
        (r.rule_summary ? '<br>口径含义：' + esc(r.rule_summary) : '') +
        (r.message ? '<br>检查结果：' + esc(r.message) : '') +
        (r.evidence ? '<br>证据：' + esc(r.evidence) : '') +
        '<br><span class="empty">内部索引（可忽略）：' + esc(r.rule_id || '') + (r.concept_key ? ' / ' + esc(r.concept_key) : '') + '</span>' +
        '</li>').join('') + '</ul>';
    }}
    function renderBusinessFilters(filters) {{
      const rows = filters || [];
      if (!rows.length) return '<div class="empty">未识别到 GameMode/iZoneAreaID/BattleSrvId/道具ID 等核心业务筛选。</div>';
      const trs = rows.map(f => {{
        const mapping = (f.mapping || []).map(m => esc(m.value) + '=' + esc(m.name) + '/' + esc(m.category));
        if ((f.unknown_values || []).length) mapping.push('未知：' + (f.unknown_values || []).map(esc).join('，'));
        if ((f.dynamic_values || []).length) mapping.push('动态值：' + (f.dynamic_values || []).map(esc).join('，'));
        return '<tr><td>' + esc(f.scope_label || f.scope || '') + '</td>' +
          '<td><strong>' + esc(f.label || f.field || '') + '</strong><br><span class="empty">' + esc(f.field || '') + '</span></td>' +
          '<td>' + esc(f.business_effect || '') + '</td>' +
          '<td>' + (mapping.join('<br>') || '<span class="empty">无映射</span>') + '</td>' +
          '<td>' + esc(f.how_to_judge || '') + '</td>' +
          '<td>' + esc(f.pass_criteria || '') + '</td>' +
          '<td>' + esc(f.condition || '') + '</td></tr>';
      }}).join('');
      return '<div class="table-scroll"><table class="wide-table"><thead><tr><th>作用范围</th><th>筛选</th><th>业务影响</th><th>映射/未知值</th><th>怎么判断</th><th>通过标准</th><th>SQL 条件</th></tr></thead><tbody>' + trs + '</tbody></table></div>';
    }}
    function renderJudgementGuide(item) {{
      const guide = item.code_view?.review_guide || {{}};
      const checks = guide.checks || [];
      const rows = checks.map(v => '<tr><td><strong>' + esc(v.name || '') + '</strong></td><td>' + esc(v.look_at || '') + '</td><td>' + esc(v.how_to_judge || '') + '</td><td>' + esc(v.pass_criteria || '') + '</td></tr>').join('');
      return '<div class="section"><h2>怎么审核这份 SQL</h2><div class="body">' +
        '<div class="kv"><span>审核目标</span><span>' + esc(guide.goal || '') + '</span></div>' +
        '<div class="kv"><span>判断顺序</span><span>' + esc(guide.decision_order || '') + '</span></div>' +
        '<div class="kv"><span>当前先处理</span><span>' + list(guide.current_blockers) + '</span></div>' +
        '<div class="kv"><span>当前需确认</span><span>' + list(guide.current_confirmations) + '</span></div>' +
        '<div class="kv"><span>涉及保存口径</span><span>' + list(guide.checked_rule_topics) + '</span></div>' +
        '<div class="table-scroll"><table class="wide-table"><thead><tr><th>审核项</th><th>看什么</th><th>怎么判断</th><th>通过标准</th></tr></thead><tbody>' +
        (rows || '<tr><td colspan="4" class="empty">无</td></tr>') +
        '</tbody></table></div>' +
        '</div></div>';
    }}
    function renderBusinessFilterSection(item) {{
      return '<div class="section"><h2>业务筛选 / ID 范围 SQL 证据</h2><div class="body">' + renderBusinessFilters(item.code_view?.business_filters) + '</div></div>';
    }}
    function renderViewTabs() {{
      return '<div class="tabs">' +
        '<button class="tab ' + (viewMode === 'product' ? 'active' : '') + '" onclick="viewMode=\\'product\\'; render()">产品视角</button>' +
        '<button class="tab ' + (viewMode === 'code' ? 'active' : '') + '" onclick="viewMode=\\'code\\'; render()">代码视角</button>' +
        '</div>';
    }}
    function renderSourceStepsHtml(steps) {{
      const rows = steps || [];
      if (!rows.length) return '<div class="empty">未识别明确来源步骤。</div>';
      const roleLabels = {{numerator:'分子', denominator:'分母', value:'换算值', metric_value:'指标值', unit_conversion_constant:'换算常数', operand:'操作数'}};
      const trs = rows.map(s => '<tr><td><strong>' + esc(roleLabels[s.role] || s.role || '') + '</strong></td>' +
        '<td>' + esc(s.source_step || '-') + '</td>' +
        '<td>' + esc((s.source_tables || []).join('、') || '无') + '</td>' +
        '<td>' + esc((s.group_by || []).join('、') || '未识别') + '</td>' +
        '<td>' + esc(s.story || '') + '</td></tr>').join('');
      return '<div class="table-scroll"><table class="wide-table"><thead><tr><th>角色</th><th>来源步骤</th><th>来源日志/表</th><th>聚合粒度</th><th>人话解释</th></tr></thead><tbody>' + trs + '</tbody></table></div>';
    }}
    function renderProductWalkthrough(sections) {{
      const rows = sections || [];
      if (!rows.length) return '';
      return rows.map(section => {{
        const paragraphs = (section.paragraphs || []).map(p => '<p>' + esc(p) + '</p>').join('');
        const table = section.table || {{}};
        let tableHtml = '';
        if ((table.headers || []).length && (table.rows || []).length) {{
          tableHtml = '<div class="table-scroll"><table class="wide-table"><thead><tr>' +
            (table.headers || []).map(h => '<th>' + esc(h) + '</th>').join('') +
            '</tr></thead><tbody>' +
            (table.rows || []).map(row => '<tr>' + (table.headers || []).map((_, idx) => '<td>' + esc((row || [])[idx] || '') + '</td>').join('') + '</tr>').join('') +
            '</tbody></table></div>';
        }}
        const bullets = list(section.bullets || []);
        return '<div class="section" style="margin:10px 0;"><h2>' + esc(section.title || '口径拆解') + '</h2><div class="body">' +
          paragraphs + tableHtml + ((section.bullets || []).length ? bullets : '') + '</div></div>';
      }}).join('');
    }}
    function renderEvidencePackage(item, story) {{
      const ev = story.execution_evidence || {{}};
      if (!Object.keys(ev).length) return '';
      const result = item.code_view?.result_file || {{}};
      const sampleRows = result.sample_rows || [];
      const cols = ((result.columns || []).length ? result.columns : Object.keys(sampleRows[0] || {{}})).slice(0, 12);
      const preview = sampleRows.length && cols.length
        ? '<div class="table-scroll"><table class="wide-table"><thead><tr>' + cols.map(c => '<th>' + esc(c) + '</th>').join('') + '</tr></thead><tbody>' +
          sampleRows.slice(0, 8).map(row => '<tr>' + cols.map(c => '<td>' + esc(row[c] ?? '') + '</td>').join('') + '</tr>').join('') +
          '</tbody></table></div>'
        : '<div class="empty">没有可展示的结果样例；请看结果文件状态和列匹配信息。</div>';
      return '<div class="section"><h2>执行与结果证据</h2><div class="body">' +
        '<div class="grid">' +
          '<div class="metric"><span>审查对象</span><strong>' + esc(ev.review_subject || ev.current_sql_role || 'unknown') + '</strong><div class="empty">当前文件角色：' + esc(ev.current_sql_role || '') + '</div></div>' +
          '<div class="metric"><span>结果证据</span><strong>' + esc(ev.result_status || 'unknown') + '</strong><div class="empty">rows=' + esc(ev.result_rows ?? 'unknown') + ' / ' + esc(ev.result_pairing_method || '') + '</div></div>' +
          '<div class="metric"><span>证据范围</span><strong>' + esc(ev.evidence_status || ev.result_evidence_role || 'unknown') + '</strong><div class="empty">执行：' + esc(ev.execution_project || 'unknown') + ' / 交付：' + esc(ev.delivery_project || 'unknown') + '</div></div>' +
        '</div>' +
        '<div class="kv"><span>SQL 文件</span><span>' + list(ev.sql_files) + '</span></div>' +
        '<div class="kv"><span>结果文件</span><span>' + list(ev.result_files && ev.result_files.length ? ev.result_files : [ev.selected_result_file]) + '</span></div>' +
        '<h3 style="font-size:14px;margin:14px 0 6px;">查询结果预览</h3>' + preview +
        '</div></div>';
    }}
    function renderBusinessStoryCards(story) {{
      const cards = story.business_story_cards || [];
      if (!cards.length) return '';
      return '<div class="section"><h2>业务口径故事</h2><div class="body"><div class="grid">' + cards.map(card =>
        '<div class="metric"><span>' + esc(card.title || '口径卡') + '</span><strong style="font-size:14px;line-height:1.6;font-weight:500;">' + esc(card.body || '') + '</strong><div class="empty">' + esc(card.evidence_ref || '') + '</div></div>'
      ).join('') + '</div></div></div>';
    }}
    function renderMetricPathCards(story) {{
      const cards = story.metric_path_cards || [];
      if (!cards.length) return '';
      return '<div class="section"><h2>指标路径</h2><div class="body"><div class="grid">' + cards.map(card =>
        '<div class="metric"><span>' + esc(card.metric_name || card.title || '指标') + '</span><strong style="font-size:14px;line-height:1.55;">' + esc(card.body || '') + '</strong>' +
        '<div class="kv"><span>计算</span><span>' + esc(card.formula || '') + '</span></div>' +
        '<div class="kv"><span>Base/分母</span><span>' + esc(card.base || '') + '</span></div>' +
        '<div class="empty">' + esc(card.caveat || '') + '</div></div>'
      ).join('') + '</div></div></div>';
    }}
    function renderOutputContract(story) {{
      const contract = story.output_contract || {{}};
      if (!Object.keys(contract).length) return '';
      return '<div class="section"><h2>输出与产品核对</h2><div class="body">' +
        '<div class="kv"><span>输出字段</span><span>' + esc((contract.fields || []).join('、') || '未识别') + '</span></div>' +
        '<div class="kv"><span>结果列</span><span>' + esc((contract.result_columns || []).join('、') || '未读取') + '</span></div>' +
        '<div class="kv"><span>产品核对</span><span>' + esc(contract.product_check || '') + '</span></div>' +
        '<div class="kv"><span>注意</span><span>' + esc(contract.warning || '') + '</span></div>' +
        '</div></div>';
    }}
    function renderLogicReview(story) {{
      const logic = story.logic_review || {{}};
      if (!Object.keys(logic).length) return '';
      const scope = logic.scope || [];
      const steps = logic.key_steps || [];
      const events = logic.events || [];
      const metrics = logic.metrics || [];
      const eventCards = events.length ? '<div class="logic-cards">' + events.map(e =>
        '<article class="logic-card"><h3>' + esc(e.event_name || '事件判定') + '</h3>' +
        '<div class="kv"><span>怎么判定</span><span>' + esc(e.event_condition || '') + '</span></div>' +
        '<div class="kv"><span>ID/回挂</span><span>' + esc(e.id_or_mapping || '') + '</span></div>' +
        '<div class="kv"><span>统计对象</span><span>' + esc(e.statistic_object || '') + '</span></div>' +
        '<div class="kv"><span>关键字段</span><span>' + list(e.source_fields || []) + '</span></div>' +
        '<div class="kv"><span>归因/时点</span><span>' + esc([e.first_or_final_rule, e.join_or_backfill_rule].filter(Boolean).join('；')) + '</span></div>' +
        '<div class="empty">' + esc(e.business_risk || '') + '</div></article>'
      ).join('') + '</div>' : '<div class="empty">未识别明确事件判定；看下方指标和折叠证据。</div>';
      const metricRows = metrics.map(m =>
        '<tr><td><strong>' + esc(m.metric_name || '') + '</strong><br><span class="empty">' + esc(m.business_meaning || '') + '</span></td>' +
        '<td>' + esc(m.numerator || '') + '</td>' +
        '<td>' + esc(m.denominator || '') + '</td>' +
        '<td>' + esc(m.formula || '') + '</td>' +
        '<td>' + esc(m.dedup_key || '') + '</td>' +
        '<td>' + esc(m.grain || '') + '</td></tr>'
      ).join('');
      const outline = (logic.comment_outline || []).length ? '<details><summary>SQL 注释原始口径</summary><ul>' +
        (logic.comment_outline || []).map(o => '<li><strong>' + esc(o.label || '') + '</strong>' +
          (o.description ? '：' + esc(o.description) : '') +
          ((o.bullets || []).length ? '<ul>' + (o.bullets || []).map(b => '<li>' + esc(b) + '</li>').join('') + '</ul>' : '') +
        '</li>').join('') + '</ul></details>' : '';
      return '<div class="section logic-first"><h2>SQL 逻辑拆解</h2><div class="body">' +
        '<div class="kv"><span>业务问题</span><span>' + esc(logic.title || story.one_sentence || '') + '</span></div>' +
        (logic.summary ? '<div class="kv"><span>一句话</span><span>' + esc(logic.summary) + '</span></div>' : '') +
        '<div class="kv important-kv"><span>公共范围</span><span>' + list(scope) + '</span></div>' +
        (steps.length ? '<div class="logic-path"><h3>判定路径</h3><ol>' + steps.map(s => '<li>' + esc(s) + '</li>').join('') + '</ol></div>' : '') +
        '<h3>事件/行为怎么判定</h3>' + eventCards +
        '<h3>指标怎么算</h3><div class="table-scroll"><table class="wide-table"><thead><tr><th>指标</th><th>分子</th><th>分母</th><th>公式</th><th>去重</th><th>粒度</th></tr></thead><tbody>' +
        (metricRows || '<tr><td colspan="6" class="empty">未从最终 SELECT 识别到指标；请看折叠证据。</td></tr>') +
        '</tbody></table></div>' + outline +
        '</div></div>';
    }}
    function renderEventContracts(story) {{
      const contracts = story.event_contracts || [];
      if (!contracts.length) return '';
      const trs = contracts.map(e => {{
        const evidenceRefs = e.sql_evidence_refs || [];
        const evidenceSnippets = (e.sql_evidence || []).map(x => [x.ref, x.snippet].filter(Boolean).join('：')).filter(Boolean);
        return '<tr><td><strong>' + esc(e.event_name || '') + '</strong><br><span class="empty">' + esc(e.event_family || '') + '</span></td>' +
          '<td>' + esc((e.source_logs_or_tables || []).join('、')) + '</td>' +
          '<td>' + esc(e.event_condition || '') + '</td>' +
          '<td>' + esc(e.id_or_mapping || '') + '</td>' +
          '<td>' + esc(e.statistic_object || '') + '</td>' +
          '<td>' + esc([e.first_or_final_rule, e.join_or_backfill_rule].filter(Boolean).join('；')) + '</td>' +
          '<td>' + esc((e.source_fields || []).join('、')) + '</td>' +
          '<td>' + esc(e.product_interpretation || '') + '<br><span class="empty">' + esc(e.business_risk || '') + '</span></td>' +
          '<td>' + esc(evidenceRefs.concat(evidenceSnippets).slice(0, 12).join('；')) + '</td>' +
          '<td>' + esc(e.confidence || '') + '</td></tr>';
      }}).join('');
      return '<div class="section"><h2>事件口径契约</h2><div class="body"><div class="table-scroll"><table class="wide-table"><thead><tr><th>事件/口径</th><th>本源日志/表</th><th>成立条件</th><th>ID/映射</th><th>统计对象/去重</th><th>首次/归因规则</th><th>本源字段</th><th>产品解释/风险</th><th>SQL 证据</th><th>置信度</th></tr></thead><tbody>' +
        (trs || '<tr><td colspan="10" class="empty">未识别行为事件契约。</td></tr>') +
        '</tbody></table></div></div></div>';
    }}
    function renderRefChips(values) {{
      const refs = (values || []).filter(Boolean);
      if (!refs.length) return '<span class="empty">无</span>';
      return '<span class="chips">' + refs.map(ref => '<span class="chip">' + esc(ref) + '</span>').join('') + '</span>';
    }}
    function renderSeverity(value) {{
      const v = String(value || '').toLowerCase();
      const cls = v === 'high' ? 'fail' : (v === 'medium' ? 'warn' : '');
      return '<span class="chip ' + cls + '">' + esc(value || 'low') + '</span>';
    }}
    function renderRiskField(label, value) {{
      if (!value) return '';
      return '<div class="risk-field"><span>' + esc(label) + '</span><p>' + esc(value) + '</p></div>';
    }}
    function renderRiskRegister(story) {{
      const rows = story.risk_register || [];
      if (!rows.length) return '';
      const cards = rows.map(r =>
        '<article class="risk-card"><div class="risk-card-head"><div><strong>' + esc((r.risk_id || '') + ' ' + (r.title || '风险')) + '</strong>' +
        (r.description ? '<p>' + esc(r.description) + '</p>' : '') + '</div>' + renderSeverity(r.severity) + '</div>' +
        '<div class="risk-fields">' +
        renderRiskField('冲突/待确认对象', r.conflict_object || '') +
        renderRiskField('SQL 当前做法', r.sql_current || '') +
        renderRiskField('标准/期望口径', r.expected_or_standard || '') +
        renderRiskField('差异', r.difference || '') +
        renderRiskField('影响', r.impact || '') +
        renderRiskField('下一步动作', r.action || '') +
        '<div class="risk-field"><span>影响指标</span><p>' + renderRefChips(r.affected_metrics || []) + '</p></div>' +
        '<div class="risk-field"><span>证据</span><p>' + renderRefChips(r.evidence_refs || []) + '</p></div>' +
        '</div></article>'
      ).join('');
      return '<div class="section"><h2>风险登记表</h2><div class="body"><div class="risk-list">' + cards + '</div></div></div>';
    }}
    function renderKeyConditions(values) {{
      const rows = (values || []).filter(Boolean);
      if (!rows.length) return '<span class="empty">未识别</span>';
      return '<ul class="condition-list">' + rows.map(v => '<li>' + esc(v) + '</li>').join('') + '</ul>';
    }}
    function renderMetricSummaryTable(story) {{
      const rows = story.metric_summary_table || [];
      if (!rows.length) return '';
      const trs = rows.map(m =>
        '<tr><td><strong>' + esc(m.metric_name || '') + '</strong><br><span class="empty">' + esc(m.business_meaning || '') + '</span></td>' +
        '<td>' + esc(m.metric_type || '') + '</td>' +
        '<td>' + esc(m.calculation || '') + '</td>' +
        '<td>' + renderKeyConditions(m.key_conditions || []) + '</td>' +
        '<td>' + esc(m.numerator || '') + '</td>' +
        '<td>' + esc(m.denominator || '') + '</td>' +
        '<td>' + esc(m.dedup_key || '') + '</td>' +
        '<td>' + esc(m.grain || '') + '</td>' +
        '<td>' + renderRefChips(m.event_refs || []) + '</td>' +
        '<td>' + renderRefChips(m.risk_refs || []) + '</td>' +
        '<td>' + esc(m.confidence || '') + '</td>' +
        '<td>' + esc(m.review_status || '') + '</td></tr>'
      ).join('');
      return '<div class="section"><h2>指标总表</h2><div class="body"><div class="table-scroll"><table class="wide-table"><thead><tr><th>指标</th><th>类型</th><th>计算</th><th>关键口径</th><th>分子</th><th>分母</th><th>去重</th><th>粒度</th><th>事件</th><th>风险</th><th>置信度</th><th>状态</th></tr></thead><tbody>' +
        trs + '</tbody></table></div></div></div>';
    }}
    function renderEventIndex(story) {{
      const rows = story.event_index || [];
      if (!rows.length) return '';
      const trs = rows.map(e =>
        '<tr><td><strong>' + esc(e.event_id || '') + '</strong><br>' + esc(e.event_name || '') + '</td>' +
        '<td>' + esc((e.source_logs_or_tables || []).join('、')) + '</td>' +
        '<td>' + esc(e.event_condition || '') + '</td>' +
        '<td>' + esc(e.statistic_object || '') + '</td>' +
        '<td>' + esc((e.source_fields || []).join('、')) + '</td>' +
        '<td>' + esc(e.risk_summary || '') + '</td>' +
        '<td>' + esc(e.confidence || '') + '</td></tr>'
      ).join('');
      return '<div class="section"><h2>事件口径索引</h2><div class="body"><div class="table-scroll"><table class="wide-table"><thead><tr><th>事件</th><th>本源日志/表</th><th>成立条件</th><th>统计对象</th><th>本源字段</th><th>风险</th><th>置信度</th></tr></thead><tbody>' +
        trs + '</tbody></table></div></div></div>';
    }}
    function renderReviewActions(story) {{
      const rows = story.review_actions || [];
      if (!rows.length) return '';
      const trs = rows.map(a =>
        '<tr><td><strong>' + esc(a.action_id || '') + '</strong><br>' + esc(a.action || '') + '</td>' +
        '<td>' + esc(a.source_ref || '') + '</td><td>' + esc(a.owner_hint || '') + '</td><td>' + esc(a.why || '') + '</td></tr>'
      ).join('');
      return '<div class="section"><h2>审查动作</h2><div class="body"><div class="table-scroll"><table class="wide-table"><thead><tr><th>动作</th><th>来源</th><th>负责人</th><th>为什么</th></tr></thead><tbody>' +
        trs + '</tbody></table></div></div></div>';
    }}
    function renderProductOverview(story) {{
      const rows = story.metric_overview || [];
      const conclusion = story.conclusion || {{}};
      const table = (story.metric_summary_table || []).length ? '' : '<div class="table-scroll"><table class="wide-table"><thead><tr><th>指标</th><th>类型</th><th>口径状态</th><th>主要风险/判断</th><th>置信度</th><th>待确认</th></tr></thead><tbody>' +
        (rows.map(m => '<tr><td><strong>' + esc(m.metric_name || '') + '</strong></td><td>' + esc(m.metric_type || '') + '</td><td>' + esc(m.review_status || '') + '</td><td>' + esc(m.main_risk || '') + '</td><td>' + esc(m.confidence || '') + '</td><td>' + esc(m.confirmation_count ?? 0) + '</td></tr>').join('') ||
        '<tr><td colspan="6" class="empty">未识别到最终指标候选。</td></tr>') +
        '</tbody></table></div>';
      return '<div class="section"><h2>指标总览 ' + chip('', story.semantic_review_status || 'unknown') + '</h2><div class="body">' +
        '<div class="kv"><span>结论状态</span><span>' + esc(conclusion.status || 'unknown') + '</span></div>' +
        '<div class="kv"><span>一句话</span><span>' + esc(story.one_sentence || '') + '</span></div>' +
        '<div class="kv"><span>分析形态</span><span>' + esc(conclusion.analysis_pattern || story.analysis_pattern || '未识别') + '</span></div>' +
        '<div class="kv"><span>业务问题</span><span>' + esc(conclusion.business_question || story.business_question || '') + '</span></div>' +
        '<div class="kv important-kv"><span>Base</span><span>' + esc(conclusion.base || story.base || '') + '</span></div>' +
        '<div class="kv"><span>使用的原始日志</span><span>' + list(story.source_logs) + '</span></div>' +
        '<div class="kv"><span>分组/粒度</span><span>' + esc(conclusion.grouping || story.grouping || '') + '</span></div>' +
        '<div class="kv"><span>结果证据</span><span>' + esc(conclusion.evidence_status || story.evidence_note || '') + '</span></div>' +
        (story.semantic_review_note ? '<div class="kv"><span>审查说明</span><span>' + esc(story.semantic_review_note) + '</span></div>' : '') +
        table + '</div></div>';
    }}
    function renderProductMetricSourceRows(rows) {{
      const sourceRows = rows || [];
      if (!sourceRows.length) return '<div class="empty">未识别明确来源字段。</div>';
      return '<div class="source-card-list">' + sourceRows.map((s, idx) =>
        '<article class="source-card"><h3>' + esc(s.role || ('来源 ' + (idx + 1))) + '</h3>' +
        '<p class="source-card-main">' + esc(s.business_story || '未补充业务说明，完整血缘见代码视角。') + '</p>' +
        '<div class="source-meta">' +
        '<div><label>本源日志/表</label><p>' + esc((s.source_logs_or_tables || []).join('、') || '未识别') + '</p></div>' +
        '<div><label>本源字段/口径证据</label><p>' + esc(s.field_expression || '完整字段血缘见代码视角') + '</p></div>' +
        '<div><label>粒度</label><p>' + esc((s.group_by || []).join('、') || '未识别') + '</p></div>' +
        '</div></article>'
      ).join('') + '</div>';
    }}
    function renderProductMetricFilters(rows) {{
      const filters = rows || [];
      if (!filters.length) return '<div class="empty">本指标没有识别到独立于公共范围的指标内条件。</div>';
      const trs = filters.map(f => '<tr><td>' + esc(f.label || '') + '</td><td>' + esc(f.business_effect || '') + '</td></tr>').join('');
      return '<div class="table-scroll"><table class="wide-table"><thead><tr><th>指标内条件</th><th>条件口径</th></tr></thead><tbody>' + trs + '</tbody></table></div>';
    }}
    function renderProductMetricConfirmations(rows) {{
      const confirmations = rows || [];
      if (!confirmations.length) return '<div class="empty">本指标暂无额外待确认项。</div>';
      return '<ul>' + confirmations.map(c => '<li><strong>' + esc(c.question || '') + '</strong>：' + esc(c.reason || '') + '<br><span class="empty">' + esc(c.evidence_ref || '') + '</span></li>').join('') + '</ul>';
    }}
    function renderProductMetricCards(story) {{
      const cards = story.metric_cards || [];
      if (!cards.length) return '<div class="section"><h2>指标卡片</h2><div class="body empty">未识别到指标卡。</div></div>';
      return '<div class="section"><h2>指标卡片</h2><div class="body">' + cards.map(m => '<div class="section" style="margin:10px 0;"><h2>' + esc(m.metric_name || '未命名指标') + ' ' + chip('', m.metric_type || '') + '</h2><div class="body">' +
        '<div class="kv"><span>指标含义</span><span>' + esc(m.business_meaning || '') + '</span></div>' +
        '<div class="kv important-kv"><span>关键口径条件</span><span>' + renderKeyConditions(m.key_conditions || []) + '</span></div>' +
        '<div class="kv"><span>最终计算</span><span>' + esc(m.calculation || '') + '</span></div>' +
        '<div class="kv"><span>分子</span><span>' + esc(m.numerator || '') + '</span></div>' +
        '<div class="kv"><span>分母</span><span>' + esc(m.denominator || '') + '</span></div>' +
        '<div class="kv"><span>去重对象</span><span>' + esc(m.dedup_key || '') + '</span></div>' +
        '<div class="kv"><span>聚合维度</span><span>' + esc((m.aggregation_dimensions || []).join('、') || '整体汇总') + '</span></div>' +
        '<div class="kv"><span>行粒度</span><span>' + esc(m.row_grain_explanation || '') + '</span></div>' +
        '<div class="kv"><span>事件口径</span><span>' + renderRefChips(m.event_refs || []) + '</span></div>' +
        '<div class="kv"><span>关联风险</span><span>' + renderRefChips(m.risk_refs || []) + (m.risk_notes && m.risk_notes.length ? '<div class="empty">' + esc(m.risk_notes.join('；')) + '</div>' : '') + '</span></div>' +
        '<div class="kv"><span>标准口径</span><span>' + esc(m.standard_rule_alignment || '') + '</span></div>' +
        '<div class="kv"><span>置信度</span><span>' + esc(m.confidence || '') + '</span></div>' +
        '<details><summary>来源日志和字段</summary>' + renderProductMetricSourceRows(m.source_logs_fields) + '</details>' +
        '<details><summary>指标内条件</summary>' + renderProductMetricFilters(m.metric_filters) + '</details>' +
        '<details><summary>本指标待确认</summary>' + renderProductMetricConfirmations(m.metric_confirmations) + '</details>' +
        '</div></div>').join('') + '</div></div>';
    }}
    function renderCommonFilterCards(story) {{
      const filters = story.common_filters || story.key_filters || [];
      const rows = filters.map(f => '<tr><td><strong>' + esc(f.label || '') + '</strong></td><td>' + esc(f.scope || '') + '</td><td>' + esc(f.business_effect || '') + '</td><td>' + esc(f.review_focus || '') + '</td></tr>').join('');
      return '<div class="section"><h2>指标公共筛选范围</h2><div class="body"><div class="table-scroll"><table class="wide-table"><thead><tr><th>筛选</th><th>作用范围</th><th>当前口径</th><th>审核重点</th></tr></thead><tbody>' +
        (rows || '<tr><td colspan="4" class="empty">未识别到核心业务筛选。</td></tr>') + '</tbody></table></div></div></div>';
    }}
    function renderSharedConfirmations(story) {{
      const rows = story.shared_confirmations || [];
      const trs = rows.map(c => '<tr><td><strong>' + esc(c.metric_name || '') + '</strong></td><td>' + esc(c.question || '') + '</td><td>' + esc(c.reason || '') + '</td><td>' + esc(c.evidence_ref || '') + '</td></tr>').join('');
      return '<div class="section"><h2>待确认项</h2><div class="body"><div class="table-scroll"><table class="wide-table"><thead><tr><th>指标</th><th>问题</th><th>原因</th><th>证据</th></tr></thead><tbody>' +
        (trs || '<tr><td colspan="4" class="empty">暂无额外待确认项；仍建议抽看结果样例量级。</td></tr>') + '</tbody></table></div></div></div>';
    }}
    function renderProductEvidenceSections(story) {{
      const sections = story.evidence_sections || [];
      if (!sections.length) return '<div class="section"><h2>SQL 证据折叠区</h2><div class="body empty">证据详见代码视角。</div></div>';
      return '<div class="section"><h2>SQL 证据折叠区</h2><div class="body">' + sections.map(section => '<details style="margin:8px 0;"><summary><strong>' + esc(section.title || 'SQL 证据') + '</strong> <span class="empty">' + esc(section.summary || '') + '</span></summary>' + list(section.items) + '</details>').join('') + '</div></div>';
    }}
    function renderProductSemanticBlocker(story) {{
      const status = String(story.semantic_review_status || '').toLowerCase();
      if (status === 'llm' || status === 'llm_cached') return '';
      return '<div class="section"><h2>产品语义闭环未完成 ' + chip('fail', status || 'missing') + '</h2><div class="body">' +
        '<div class="kv"><span>状态</span><span>当前不是有效产品审查结果，只能作为调试证据包查看。</span></div>' +
        '<div class="kv"><span>原因</span><span>' + esc(story.semantic_review_note || '未获得 LLM 产品语义审查。') + '</span></div>' +
        '<div class="kv"><span>下一步</span><span>重新运行 SQL Review 并启用 product review command；不要把本页当成业务口径结论。</span></div>' +
        '</div></div>';
    }}
    function renderProductView(item) {{
      const story = item.product_view || {{}};
      return renderProductSemanticBlocker(story) +
        renderEvidencePackage(item, story) +
        renderProductOverview(story) +
        renderRiskRegister(story) +
        renderMetricSummaryTable(story) +
        renderReviewActions(story) +
        renderProductMetricCards(story) +
        renderEventIndex(story) +
        renderEventContracts(story) +
        renderCommonFilterCards(story) +
        renderSharedConfirmations(story) +
        renderOutputContract(story) +
        renderProductWalkthrough(story.walkthrough_sections) +
        renderBusinessStoryCards(story) +
        renderMetricPathCards(story) +
        renderProductEvidenceSections(story);
    }}
    function renderMetricConditions(conditions) {{
      const rows = conditions || [];
      if (!rows.length) return '<div class="empty">未识别 CASE/IF 指标条件。</div>';
      const trs = rows.map(c => '<tr><td>' + esc(c.business_effect || '') + '</td>' +
        '<td>' + esc(c.how_to_judge || '') + '</td>' +
        '<td>' + esc(c.pass_criteria || '') + '</td>' +
        '<td>' + esc(c.condition || '') + '</td></tr>').join('');
      return '<div class="table-scroll"><table class="wide-table"><thead><tr><th>条件含义</th><th>怎么判断</th><th>通过标准</th><th>SQL 条件</th></tr></thead><tbody>' + trs + '</tbody></table></div>';
    }}
    function renderFunnelReview(funnel) {{
      if (!funnel || !funnel.detected) return '';
      const stepRows = (funnel.steps || []).map(s => '<tr><td>' + esc(s.order || '') + '</td>' +
        '<td><strong>' + esc(s.step_name || '') + '</strong></td>' +
        '<td>' + esc(s.source_table || 'unknown') + '</td>' +
        '<td>' + esc(s.first_time_alias || '') + '</td>' +
        '<td>' + esc(s.reach_rule || '') + '</td>' +
        '<td>' + esc(s.how_to_judge || '') + '</td>' +
        '<td>' + esc(s.pass_criteria || '') + '</td></tr>').join('');
      return '<div class="section" style="margin:10px 0;"><h2>漏斗步骤审核</h2><div class="body">' +
        '<div class="kv"><span>摘要</span><span>' + esc(funnel.summary || '') + '</span></div>' +
        '<div class="kv"><span>Base</span><span>' + esc(funnel.base || '') + '</span></div>' +
        '<div class="kv"><span>去重/首次到达</span><span>' + esc(funnel.dedup_grain || '') + '</span></div>' +
        '<div class="kv"><span>时间窗</span><span>' + esc(funnel.time_window || '未识别') + '</span></div>' +
        '<div class="kv"><span>分区窗</span><span>' + esc(funnel.partition_window || '未识别') + '</span></div>' +
        '<div class="kv"><span>严格顺序</span><span>' + esc(funnel.strict_order_rule || '') + '</span></div>' +
        '<div class="kv"><span>人数指标</span><span>' + esc(funnel.step_count_metric || '') + '</span></div>' +
        '<div class="kv"><span>转化指标</span><span>' + list(funnel.conversion_metrics) + '</span></div>' +
        '<div class="kv"><span>怎么审</span><span>' + list(funnel.how_to_review) + '</span></div>' +
        '<div class="table-scroll"><table class="wide-table"><thead><tr><th>步骤</th><th>事件/步骤名</th><th>来源表</th><th>首次时间字段</th><th>到达条件</th><th>怎么判断</th><th>通过标准</th></tr></thead><tbody>' +
        (stepRows || '<tr><td colspan="7" class="empty">未识别步骤</td></tr>') +
        '</tbody></table></div></div></div>';
    }}
    function renderDistributionReview(distribution) {{
      if (!distribution || !distribution.detected) return '';
      const cards = (distribution.bucket_cards || []).map(card => {{
        const defs = (card.definitions || []).map(d => '<tr><td>' + esc(d.bucket || '') + '</td><td>' + esc(d.business_effect || d.condition || '') + '</td><td>' + esc(d.how_to_judge || '') + '</td><td>' + esc(d.pass_criteria || '') + '</td></tr>').join('');
        return '<div class="section" style="margin:10px 0;"><h2>分桶字段 ' + esc(card.field || '') + '</h2><div class="body">' +
          '<div class="kv"><span>怎么审</span><span>' + esc(card.how_to_review || '') + '</span></div>' +
          '<div class="kv"><span>通过标准</span><span>' + esc(card.pass_criteria || '') + '</span></div>' +
          '<div class="table-scroll"><table class="wide-table"><thead><tr><th>桶</th><th>含义/条件</th><th>怎么判断</th><th>通过标准</th></tr></thead><tbody>' +
          (defs || '<tr><td colspan="4" class="empty">未识别明确分桶定义</td></tr>') +
          '</tbody></table></div></div></div>';
      }}).join('');
      return '<div class="section" style="margin:10px 0;"><h2>分布/分桶审核</h2><div class="body">' +
        '<div class="kv"><span>摘要</span><span>' + esc(distribution.summary || '') + '</span></div>' +
        '<div class="kv"><span>怎么审</span><span>' + list(distribution.how_to_review) + '</span></div>' +
        cards + '</div></div>';
    }}
    function renderBusinessReview(review) {{
      const business = review.business_review || {{}};
      const objects = business.primary_review_objects || [];
      const rows = objects.map(o => '<tr><td><strong>' + esc(o.name || '') + '</strong></td><td>' + esc(o.what_to_check || '') + '</td><td>' + esc(o.how_to_judge || '') + '</td><td>' + esc(o.pass_criteria || '') + '</td></tr>').join('');
      const patternCards = business.pattern_cards || [];
      const patternRows = patternCards.map(o => '<tr><td><strong>' + esc(o.name || '') + '</strong></td><td>' + esc(o.what_to_check || '') + '</td><td>' + esc(o.how_to_judge || '') + '</td><td>' + esc(o.pass_criteria || '') + '</td></tr>').join('');
      return '<div class="section" style="margin:10px 0;"><h2>业务逻辑审核入口 ' + chip('', business.pattern_label || '未知形态') + '</h2><div class="body">' +
        '<div class="kv"><span>业务问题</span><span>' + esc(business.business_question || '') + '</span></div>' +
        '<div class="kv"><span>审核提醒</span><span>' + esc(business.reviewer_takeaway || '') + '</span></div>' +
        '<div class="kv"><span>当前形态怎么审</span><span>' + list(business.pattern_review_order) + '</span></div>' +
        '<div class="kv"><span>时长算法</span><span>' + esc(business.duration_logic || '无时长算法或未识别') + '</span></div>' +
        '<div class="table-scroll"><table class="wide-table"><thead><tr><th>审核对象</th><th>看什么</th><th>怎么判断</th><th>通过标准</th></tr></thead><tbody>' +
        (rows || '<tr><td colspan="4" class="empty">未识别业务审核对象</td></tr>') +
        '</tbody></table></div>' +
        '<h3 style="font-size:14px;margin:14px 0 6px;">当前分析形态重点卡</h3>' +
        '<div class="table-scroll"><table class="wide-table"><thead><tr><th>重点</th><th>看什么</th><th>怎么判断</th><th>通过标准</th></tr></thead><tbody>' +
        (patternRows || '<tr><td colspan="4" class="empty">未识别形态重点卡</td></tr>') +
        '</tbody></table></div>' +
        renderFunnelReview(business.funnel_review) +
        renderDistributionReview(business.distribution_review) +
        '</div></div>';
    }}
    function renderReviewerCard(item) {{
      const card = item.code_view?.reviewer_card || {{}};
      return '<div class="section"><h2>审核卡 ' + chip(card.severity || '', card.title || '') + '</h2><div class="body">' +
        '<div class="kv"><span>先做什么</span><span>' + esc(card.action || '') + '</span></div>' +
        '<div class="kv"><span>为什么</span><span>' + esc(card.why || '') + '</span></div>' +
        '<div class="kv"><span>结果证据</span><span>' + esc(card.result_brief || '') + '</span></div>' +
        '<div class="kv"><span>审核步骤</span><span>' + list(card.reviewer_steps) + '</span></div>' +
        '<div class="kv"><span>阻断项</span><span>' + renderIssueList(card.blockers) + '</span></div>' +
        '<div class="kv"><span>待确认项</span><span>' + renderIssueList(card.confirmations) + '</span></div>' +
        '</div></div>';
    }}
    function renderMetricReview(item) {{
      const review = item.code_view?.metric_review_trace || {{}};
      const cards = review.metric_cards || [];
      const dimensions = review.dimension_cards || [];
      const dimensionCards = dimensions.length ? dimensions.map(card => '<div class="section" style="margin:10px 0;"><h2>' + esc(card.field) + '</h2><div class="body">' +
        '<div class="kv"><span>字段角色</span><span>' + esc(card.role || '') + '</span></div>' +
        '<div class="kv"><span>说明</span><span>' + esc(card.description || '') + '</span></div>' +
        '<div class="kv"><span>来源</span><span>' + esc((card.source || '') + ' / ' + (card.confidence || '')) + '</span></div>' +
        '</div></div>').join('') : '<div class="empty">未识别明确分组字段。</div>';
      const metricCards = cards.length ? cards.map(card => '<div class="section" style="margin:10px 0;"><h2>' + esc(card.metric) + '</h2><div class="body">' +
        '<div class="kv"><span>业务口径</span><span>' + esc(card.business_definition || '') + '</span></div>' +
        '<div class="kv"><span>Base</span><span>' + esc(card.base || '') + '</span></div>' +
        '<div class="kv"><span>分子</span><span>' + esc(card.numerator || '') + '</span></div>' +
        '<div class="kv"><span>分母</span><span>' + esc(card.denominator || '') + '</span></div>' +
        '<div class="kv"><span>计算</span><span>' + esc(card.calculation || '') + '</span></div>' +
        '<div class="kv"><span>分子/分母来源步骤</span><span>' + renderSourceStepsHtml(card.source_steps) + '</span></div>' +
        '<div class="kv"><span>Base 级筛选</span><span>' + renderBusinessFilters(card.base_business_filters) + '</span></div>' +
        '<div class="kv"><span>指标内筛选</span><span>' + renderBusinessFilters(card.metric_business_filters) + '</span></div>' +
        '<div class="kv"><span>指标条件</span><span>' + renderMetricConditions(card.metric_conditions) + '</span></div>' +
        '<div class="kv"><span>关联/归因条件</span><span>' + renderBusinessFilters(card.join_business_filters) + '</span></div>' +
        '<div class="kv"><span>怎么看</span><span>' + esc(card.how_to_review || '') + '</span></div>' +
        '<div class="kv"><span>通过标准</span><span>' + esc(card.pass_criteria || '') + '</span></div>' +
        '<div class="kv"><span>审核问题</span><span>' + esc(card.reviewer_question || '') + '</span></div>' +
        '<div class="kv"><span>相关保存口径</span><span>' + renderSavedRuleChecks(card.related_saved_rule_checks) + '</span></div>' +
        '<div class="kv"><span>来源</span><span>' + esc((card.source || '') + ' / ' + (card.confidence || '')) + '</span></div>' +
        '</div></div>').join('') : '<div class="empty">未识别到指标。</div>';
      return '<div class="section"><h2>指标逻辑审核</h2><div class="body">' +
        '<div class="kv"><span>一句话说明</span><span>' + esc(review.summary || '') + '</span></div>' +
        '<div class="kv"><span>分组方式</span><span>' + esc(review.grouping || '') + '</span></div>' +
        '<div class="kv"><span>核心 Base</span><span>' + esc(review.common_base || '') + '</span></div>' +
        renderBusinessReview(review) +
        '<div class="kv"><span>计算路径</span><span>' + list(review.calculation_path) + '</span></div>' +
        '<div class="kv"><span>需要确认</span><span>' + list(review.review_questions) + '</span></div>' +
        '<h3 style="font-size:14px;margin:14px 0 6px;">分组/维度字段</h3>' +
        dimensionCards +
        '<h3 style="font-size:14px;margin:14px 0 6px;">指标卡</h3>' +
        metricCards +
        '</div></div>';
    }}
    function renderMetricLogic(item) {{
      const metrics = item.code_view?.metric_logic || [];
      if (!metrics.length) return '<div class="section"><h2>指标口径与计算逻辑</h2><div class="body empty">未从最终 SELECT 推断出指标逻辑。</div></div>';
      const colgroup = '<colgroup><col class="metric-col"><col class="definition-col"><col class="base-col"><col class="numerator-col"><col class="denominator-col"><col class="formula-col"><col class="source-col"></colgroup>';
      const table = '<div class="table-scroll"><table class="wide-table metric-logic-table">' + colgroup + '<thead><tr><th class="metric-name-head">指标</th><th>指标业务口径</th><th>Base / 分母基准</th><th>分子说明</th><th>分母说明</th><th>计算说明</th><th>来源/置信度</th></tr></thead><tbody>' +
        metrics.map(m => '<tr><td class="metric-name-cell"><strong>' + esc(m.metric) + '</strong><br><span class="empty">' + esc(m.calculation_type) + '</span></td><td>' + esc(m.business_definition || '') + '</td><td>' + esc(m.base_population) + '</td><td>' + esc(m.numerator) + '</td><td>' + esc(m.denominator) + '</td><td>' + esc(m.formula) + '</td><td>' + esc(m.description_source || '') + '<br>' + esc(m.confidence) + (m.needs_manual_confirmation ? '<br>' + chip('warn','需确认') : '') + '</td></tr>').join('') +
        '</tbody></table></div>';
      const detail = metrics.map(m => '<h3 style="font-size:14px;margin:12px 0 6px;">' + esc(m.metric) + '</h3>' +
        '<div class="kv"><span>SQL公式</span><span>' + esc(m.formula_expression || '') + '</span></div>' +
        '<div class="kv"><span>表达式追溯</span><span>' + esc('numerator=' + (m.numerator_expression || '') + '; denominator=' + (m.denominator_expression || '') + '; base=' + (m.base_expression || '')) + '</span></div>' +
        '<div class="kv"><span>血缘展开</span><span>' + list(m.lineage) + '</span></div>' +
        '<div class="kv"><span>来源步骤</span><span>' + renderSourceStepsHtml(m.source_steps) + '</span></div>' +
        '<div class="kv"><span>指标内过滤</span><span>' + list(m.metric_filters) + '</span></div>' +
        '<div class="kv"><span>指标条件卡</span><span>' + renderMetricConditions(m.metric_condition_cards) + '</span></div>' +
        '<div class="kv"><span>指标内业务筛选</span><span>' + renderBusinessFilters(m.metric_business_filters) + '</span></div>' +
        '<div class="kv"><span>Base过滤</span><span>' + list(m.global_filters) + '</span></div>' +
        '<div class="kv"><span>Base业务筛选</span><span>' + renderBusinessFilters(m.base_business_filters) + '</span></div>' +
        '<div class="kv"><span>JOIN口径</span><span>' + list(m.join_logic) + '</span></div>' +
        '<div class="kv"><span>关联/归因业务条件</span><span>' + renderBusinessFilters(m.join_business_filters) + '</span></div>' +
        '<div class="kv"><span>保存口径核对</span><span>' + renderSavedRuleChecks(m.related_saved_rule_checks) + '</span></div>'
      ).join('');
      return '<div class="section"><h2>指标口径与计算逻辑</h2><div class="body">' + table + detail + '</div></div>';
    }}
    function renderCodeView(item, result) {{
      const code = item.code_view || {{}};
      const summary = code.sql_summary || {{}};
      const role = code.role_context || {{}};
      const perf = code.performance_preflight || {{}};
      return `
        ${{renderReviewerCard(item)}}
        ${{renderBusinessFilterSection(item)}}
        <div class="section"><h2>${{esc(item.name)}}</h2><div class="body">
          <div class="kv"><span>路径</span><span>${{esc(item.path)}}</span></div>
          <div class="kv"><span>下一步重点</span><span>${{esc(item.next_focus)}}</span></div>
          <div class="kv"><span>自动阶段</span><span>${{esc(role.review_stage)}}</span></div>
          <div class="kv"><span>查询状态</span><span>${{esc(role.query_review_status)}}</span></div>
          <div class="kv"><span>执行项目</span><span>${{esc(role.execution_project)}}（${{esc(role.execution_inference_reason)}}）</span></div>
          <div class="kv"><span>交付项目</span><span>${{esc(role.delivery_project)}}</span></div>
        </div></div>
        <div class="grid">
          ${{renderDim(item,'logic','逻辑/口径审查')}}
          ${{renderDim(item,'code_quality','代码质量审查')}}
          ${{renderDim(item,'evidence','结果证据审查')}}
          ${{renderDim(item,'dashboard_fit','看板适配审查')}}
          ${{renderDim(item,'deployment_gate','部署门禁审查')}}
        </div>
        <div class="section"><h2>SQL 摘要</h2><div class="body">
          <div class="kv"><span>表</span><span>${{esc((summary.source_tables||[]).join(', ') || 'unknown')}}</span></div>
          <div class="kv"><span>指标</span><span>${{esc((summary.metrics||[]).join(', ') || 'none')}}</span></div>
          <div class="kv"><span>维度</span><span>${{esc((summary.dimensions||[]).join(', ') || 'none')}}</span></div>
          <div class="kv"><span>输出字段</span><span>${{esc((summary.final_fields||[]).join(', ') || 'none')}}</span></div>
          <div class="kv"><span>复杂度</span><span>CTE=${{esc(summary.cte_count)}}; JOIN=${{esc(summary.join_count)}}; grade=${{esc(summary.grade)}}</span></div>
        </div></div>
        <div class="section"><h2>性能 Preflight</h2><div class="body">
          <div class="kv"><span>等级</span><span>${{esc(perf.tier || summary.performance_tier || 'unknown')}}</span></div>
          <div class="kv"><span>分数</span><span>${{esc(perf.score ?? summary.performance_score ?? 0)}}</span></div>
          <div class="kv"><span>状态</span><span>${{esc(perf.status || '')}}</span></div>
          <div class="kv"><span>参考</span><span>${{esc((perf.required_references||[]).join(', ') || 'none')}}</span></div>
          <div class="kv"><span>优化提示</span><span>${{esc(perf.optimization_hint || '')}}</span></div>
          <h3>触发原因</h3>
          <ul>${{(perf.triggers||[]).map(t => '<li>' + esc(t) + '</li>').join('') || '<li>none</li>'}}</ul>
          <h3>阻断项</h3>
          <ul>${{(perf.blockers||[]).map(t => '<li>' + esc(t) + '</li>').join('') || '<li>none</li>'}}</ul>
        </div></div>
        ${{renderMetricLogic(item)}}
        <div class="section"><h2>结果文件</h2><div class="body">
          <div class="kv"><span>文件</span><span>${{esc(result.path || 'none')}}</span></div>
          <div class="kv"><span>状态</span><span>${{esc(result.status || '')}}</span></div>
          <div class="kv"><span>行数</span><span>${{esc(result.row_count ?? 'unknown')}}</span></div>
          <div class="kv"><span>列</span><span>${{esc((result.columns||[]).join(', ') || 'none')}}</span></div>
          ${{renderTableRows(result.sample_rows)}}
        </div></div>
        <div class="section"><h2>Findings</h2><div class="body"><ul>${{(code.findings||[]).map(f => '<li><strong>' + esc(f.severity) + '</strong>: ' + esc(f.message) + '</li>').join('') || '<li>none</li>'}}</ul></div></div>`;
    }}
    function renderDetail() {{
      const item = filtered()[selected];
      if (!item) {{ document.getElementById('detail').innerHTML = '<div class="empty">没有 SQL</div>'; return; }}
      const result = item.code_view?.result_file || {{}};
      document.getElementById('detail').innerHTML = renderViewTabs() + (viewMode === 'product' ? renderProductView(item) : renderCodeView(item, result));
    }}
    function renderLoading(message) {{
      renderHeader();
      renderSummary();
      document.getElementById('actionQueue').innerHTML = '';
      document.getElementById('list').innerHTML = '<div class="empty" style="padding:12px;">' + esc(message) + '</div>';
      document.getElementById('detail').innerHTML = '<div class="section"><h2>动态读取</h2><div class="body empty">' + esc(message) + '</div></div>';
    }}
    function render() {{ renderHeader(); renderSummary(); renderActionQueue(); renderList(); renderDetail(); }}
    document.getElementById('search').addEventListener('input', () => {{ selected = 0; render(); }});
    document.getElementById('statusFilter').addEventListener('change', () => {{ selected = 0; render(); }});
    if (reviewApiUrl) {{
      renderLoading('正在读取最新 sql_review.json...');
      fetch(reviewApiUrl, {{cache:'no-store'}})
        .then(response => {{
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return response.json();
        }})
        .then(data => {{
          payload = data || payload;
          selected = 0;
          render();
        }})
        .catch(error => {{
          renderLoading('读取失败：' + error.message);
        }});
    }} else {{
      render();
    }}
  </script>
</body>
</html>
"""


def build_role_context(args: argparse.Namespace, input_paths: list[Path], inbox_root: Path) -> ReviewRoleContext:
    legacy_project_root = Path(args.project_root).resolve() if args.project_root else None
    explicit_role_args = bool(args.definition_project_root or args.delivery_project_root or args.execution_project_root)
    projects_root = inbox_root.parent.resolve()

    if legacy_project_root and not explicit_role_args:
        project = load_project_context(legacy_project_root)
        projects = [project] if project else []
        return ReviewRoleContext(
            definition=project,
            delivery=project,
            execution_projects=projects,
            known_projects=projects,
            file_role_map=load_file_role_map(args.file_role_map),
            execution_selection_explicit=True,
        )

    inferred_project_root = infer_inbox_project_root(input_paths, inbox_root)
    definition_root = Path(args.definition_project_root).resolve() if args.definition_project_root else inferred_project_root
    delivery_root = Path(args.delivery_project_root).resolve() if args.delivery_project_root else (inferred_project_root or definition_root)
    definition = load_project_context(definition_root) if definition_root else None
    delivery = load_project_context(delivery_root) if delivery_root else definition

    explicit_execution = [
        context
        for context in (load_project_context(Path(item).resolve()) for item in args.execution_project_root)
        if context
    ]
    discovered = discover_project_contexts(projects_root)
    known_projects = dedupe_projects([project for project in [definition, delivery] if project] + explicit_execution + discovered)
    execution_projects = explicit_execution or known_projects
    return ReviewRoleContext(
        definition=definition,
        delivery=delivery,
        execution_projects=dedupe_projects(execution_projects),
        known_projects=known_projects,
        file_role_map=load_file_role_map(args.file_role_map),
        execution_selection_explicit=bool(explicit_execution),
    )


class SqlReviewHandler(BaseHTTPRequestHandler):
    review_root: Path
    json_name: str
    html_name: str

    def review_json_path(self) -> Path:
        return self.review_root / self.json_name

    def send_text(self, status: int, content: str, content_type: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_text(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def load_review_payload(self) -> dict:
        json_path = self.review_json_path()
        if not json_path.exists():
            raise FileNotFoundError(str(json_path))
        return json.loads(json_path.read_text(encoding="utf-8"))

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html", "/sql_review.html", f"/{self.html_name}"}:
            self.send_text(200, render_html_viewer(None, api_url="/api/review"), "text/html; charset=utf-8")
            return
        if path == "/api/review":
            try:
                self.send_json(200, self.load_review_payload())
            except FileNotFoundError as exc:
                self.send_json(
                    404,
                    {
                        "status": "error",
                        "message": f"review JSON not found: {exc}",
                        "expected_path": str(self.review_json_path()),
                    },
                )
            except json.JSONDecodeError as exc:
                self.send_json(
                    500,
                    {
                        "status": "error",
                        "message": f"review JSON is invalid: {exc}",
                        "expected_path": str(self.review_json_path()),
                    },
                )
            return
        self.send_text(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, format, *args):  # noqa: A002
        sys.stderr.write("sql_review: " + (format % args) + "\n")


def cmd_serve(args: argparse.Namespace) -> None:
    if len(args.paths) != 1:
        raise SystemExit("--serve expects exactly one review root directory or sql_review.json path")
    target = Path(args.paths[0]).resolve()
    if not target.exists():
        raise SystemExit(f"Review root or JSON not found: {target}")
    if target.is_file():
        review_root = target.parent
        json_name = target.name
    else:
        review_root = target
        json_name = args.json_name
    handler = type(
        "BoundSqlReviewHandler",
        (SqlReviewHandler,),
        {
            "review_root": review_root,
            "json_name": json_name,
            "html_name": args.html_name,
        },
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"sql_review_url: http://{args.host}:{server.server_port}/sql_review.html")
    print(f"sql_review_api: http://{args.host}:{server.server_port}/api/review")
    print(f"sql_review_json: {review_root / json_name}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="SQL files or directories to review")
    parser.add_argument("--project-root", help="SQL project root containing Canonical Rule Store v2")
    parser.add_argument("--definition-project-root", help="Project root whose canonical rules define the business intent")
    parser.add_argument("--execution-project-root", action="append", default=[], help="Project root where SQL/result files may have been executed; repeatable")
    parser.add_argument("--delivery-project-root", help="Project root where reviewed SQL is intended to be deployed")
    parser.add_argument("--file-role-map", help="JSON string or JSON file mapping SQL file/glob to execution_project")
    add_function_gate_arguments(
        parser,
        selection_help=(
            "Optional explicit SQL review function route: 【SQL审查】, [REVIEW], or REVIEW."
        ),
    )
    parser.add_argument("--project-name", help="Project label to show when no project root is available")
    parser.add_argument("--inbox-root", default=str(DEFAULT_INBOX_ROOT), help="Default root for relative review inbox paths")
    parser.add_argument("--recursive", dest="recursive", action="store_true", default=True, help="Recursively scan directories (default)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="Scan only the provided directories")
    parser.add_argument("--pattern", default="*.sql", help="Glob pattern for directory scans")
    parser.add_argument("--code-name", default="sql_review_code.md", help="Code-view Markdown report filename")
    parser.add_argument("--product-name", default="sql_review_product.md", help="Product-view Markdown report filename")
    parser.add_argument("--summary-name", default="sql_review_summary.md", help="Batch summary Markdown filename")
    parser.add_argument("--json-name", default="sql_review.json", help="Machine-readable review JSON filename at batch root")
    parser.add_argument("--html-name", default="sql_review.html", help="Interactive review HTML filename at batch root")
    parser.add_argument("--serve", action="store_true", help="Serve a read-only dynamic review viewer for an existing sql_review.json")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --serve")
    parser.add_argument("--port", type=int, default=0, help="Port for --serve; 0 chooses a free port")
    default_product_command = default_product_review_command()
    default_product_mode = os.environ.get(
        "SQL_REVIEW_PRODUCT_MODE",
        "llm",
    )
    parser.add_argument(
        "--product-review-mode",
        choices=["llm", "evidence-only", "off"],
        default=default_product_mode,
        help="Product-view semantic review mode. llm uses --product-review-command, SQL_REVIEW_PRODUCT_AGENT_COMMAND, or the bundled Codex wrapper when available; evidence-only renders deterministic evidence cards.",
    )
    parser.add_argument(
        "--product-review-command",
        default=default_product_command,
        help="Optional command that reads evidence JSON from stdin and writes product_view JSON to stdout.",
    )
    parser.add_argument(
        "--allow-product-review-downgrade",
        action="store_true",
        default=os.environ.get("SQL_REVIEW_ALLOW_PRODUCT_DOWNGRADE", "").strip().lower() in {"1", "true", "yes"},
        help="Allow evidence-only/off or fallback output. Intended only for offline fixtures and explicit debugging.",
    )
    parser.add_argument(
        "--product-review-cache-dir",
        default=os.environ.get("SQL_REVIEW_PRODUCT_CACHE_DIR", ""),
        help="Optional cache directory for accepted LLM product_view JSON.",
    )
    parser.add_argument("--min-shared", type=int, default=2, help="Minimum file count for shared rule merging")
    parser.add_argument("--sample-rows", type=int, default=DEFAULT_SAMPLE_ROWS, help="Rows to sample from paired result files")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.serve:
        cmd_serve(args)
        return
    try:
        purpose = "SQL review"
        function_selection = require_user_function_selection(
            args.function_selection,
            user_request=args.user_request,
            allowed_ids=command_function_ids("sql_review.py"),
            purpose=purpose,
        )
        require_user_request(args.user_request, purpose=purpose)
    except FunctionGateError as exc:
        exit_with_gate_error(parser, exc)
    if args.product_review_mode != "llm" and not args.allow_product_review_downgrade:
        raise SystemExit(
            "BLOCKED: SQL Review product view must run LLM semantic review. "
            "Use --product-review-mode llm, or add --allow-product-review-downgrade only for explicit offline tests."
        )
    if args.product_review_mode == "llm" and not args.product_review_command:
        raise SystemExit(
            "BLOCKED: --product-review-mode llm requires a product review command. "
            "Install/enable Codex CLI wrapper or set SQL_REVIEW_PRODUCT_AGENT_COMMAND."
        )
    inbox_root = Path(args.inbox_root).resolve()
    input_paths = resolve_input_paths([Path(item) for item in args.paths], inbox_root)
    roles = build_role_context(args, input_paths, inbox_root)
    project_root = roles.definition.root if roles.definition else None
    project_name = project_label(roles.definition) if roles.definition else ""
    canonical_rules = roles.definition.canonical_rules if roles.definition else []
    selected_rule_union: dict[tuple[str, int, str], CanonicalRule] = {
        (rule.rule_id, rule.version, rule.status): rule for rule in canonical_rules
    }
    if args.project_name:
        project_name = args.project_name
    sql_files = discover_sql_files(input_paths, args.pattern, args.recursive)
    result_files = discover_result_files(input_paths, args.recursive)
    if not sql_files and not result_files:
        raise SystemExit("No SQL or result files found.")
    results_by_key = map_result_files(result_files)
    orphan_results = orphan_result_files(result_files, sql_files)
    orphan_results_by_dir: dict[Path, list[Path]] = defaultdict(list)
    for path in orphan_results:
        orphan_results_by_dir[path.parent].append(path)
    grouped: dict[Path, list[Path]] = defaultdict(list)
    for sql_file in sql_files:
        grouped[sql_file.parent].append(sql_file)
    for directory in orphan_results_by_dir:
        grouped.setdefault(directory, [])
    all_reviews: list[FileReview] = []
    reviews_by_directory: dict[Path, list[FileReview]] = {}
    for directory, files in sorted(grouped.items(), key=lambda item: item[0].as_posix().lower()):
        reviews = [review_sql_file(path, project_root) for path in files]
        for review in reviews:
            review.execution_evidence = review_evidence_for(review.path)
            exact_results = results_by_key.get(result_key(review.path), [])
            attach_result_file(
                review,
                exact_results,
                max(1, args.sample_rows),
                "exact_stem" if exact_results else "missing",
            )
            if review.result_file:
                review.execution_evidence["result_evidence_role"] = "exact_result"
                review.execution_evidence["result_files"] = [review.result_file.path.name]
            review_rules = select_project_rules_for_sql(roles.definition, review.sql)
            if roles.definition:
                roles.definition.canonical_rules = review_rules
            for rule in review_rules:
                selected_rule_union[(rule.rule_id, rule.version, rule.status)] = rule
            apply_project_rule_checks(
                review,
                review_rules,
                roles.definition.root if roles.definition else None,
            )
            apply_role_analysis(review, roles)
        all_reviews.extend(reviews)
        reviews_by_directory[directory] = reviews
    canonical_rules = list(selected_rule_union.values())
    if roles.definition:
        roles.definition.canonical_rules = canonical_rules
    summary_root = summary_root_for(input_paths)
    product_review_cache_dir = (
        Path(args.product_review_cache_dir).resolve()
        if args.product_review_cache_dir
        else (summary_root / ".sql_review_product_cache" if args.product_review_mode == "llm" and args.product_review_command else None)
    )
    hydrate_product_reviews(
        summary_root,
        all_reviews,
        roles,
        product_review_mode=args.product_review_mode,
        product_review_command=args.product_review_command,
        product_review_cache_dir=product_review_cache_dir,
    )
    if args.product_review_mode == "llm" and not args.allow_product_review_downgrade:
        bad_reviews = []
        for review in all_reviews:
            product_view = review.product_view or {}
            status = product_view.get("semantic_review_status")
            if status not in {"llm", "llm_cached"}:
                note = str(product_view.get("semantic_review_note") or "")
                bad_reviews.append(f"{review.path.name}: {status or 'missing'} {note[:240]}")
        if bad_reviews:
            preview = "\n".join(f"- {item}" for item in bad_reviews[:12])
            raise SystemExit(
                "BLOCKED: LLM product semantic review did not complete for every SQL; "
                "refusing to write evidence-only product output.\n"
                f"{preview}"
            )
    for directory, reviews in sorted(reviews_by_directory.items(), key=lambda item: item[0].as_posix().lower()):
        code_report = render_code_report(
            directory,
            reviews,
            max(1, args.min_shared),
            project_name=project_name,
            project_root=project_root,
            canonical_rules=canonical_rules,
            orphan_results=orphan_results_by_dir.get(directory, []),
            roles=roles,
        )
        code_output_path = directory / args.code_name
        code_output_path.write_text(code_report, encoding="utf-8")
        print(f"Wrote code review: {code_output_path}")
        product_output_path = directory / args.product_name
        product_output_path.write_text(
            render_product_report(directory, reviews, project_name, project_root, roles),
            encoding="utf-8",
        )
        print(f"Wrote product review: {product_output_path}")
    summary_path = summary_root / args.summary_name
    summary_path.write_text(
        render_summary_report(summary_root, all_reviews, orphan_results, project_name, project_root, roles=roles),
        encoding="utf-8",
    )
    print(f"Wrote review summary: {summary_path}")
    payload = review_payload(summary_root, all_reviews, orphan_results, project_name, project_root, roles)
    json_path = summary_root / args.json_name
    json_path.write_text(render_json_report(payload), encoding="utf-8")
    print(f"Wrote review JSON: {json_path}")
    html_path = summary_root / args.html_name
    html_path.write_text(render_html_viewer(payload), encoding="utf-8")
    print(f"Wrote review HTML: {html_path}")


if __name__ == "__main__":
    main()
