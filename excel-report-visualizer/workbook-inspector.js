(() => {
  "use strict";

  const PREVIEW_ROWS = 10;
  const PREVIEW_COLUMNS = 12;
  const HEADER_SCAN_ROWS = 20;
  const ROLE_OPTIONS = [
    { value: "date", label: "日期" },
    { value: "dimension", label: "维度" },
    { value: "measure", label: "指标" }
  ];
  const state = {
    workbook: null,
    snapshot: null,
    selectedSheetId: null,
    root: null
  };

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function normalize(value) {
    return String(value ?? "")
      .trim()
      .toLowerCase()
      .replace(/[\s_\-（）()【】\[\]：:，,。.%％]/g, "");
  }

  function text(value) {
    if (value == null) return "";
    if (value instanceof Date && !Number.isNaN(value.getTime())) {
      return value.toISOString().slice(0, 10);
    }
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function isEmpty(value) {
    return text(value).trim() === "";
  }

  function columnName(index) {
    let value = index + 1;
    let result = "";
    while (value > 0) {
      const remainder = (value - 1) % 26;
      result = String.fromCharCode(65 + remainder) + result;
      value = Math.floor((value - 1) / 26);
    }
    return result;
  }

  function cellName(row, column) {
    return columnName(column) + String(row + 1);
  }

  function rangeFromRef(ref) {
    if (!ref || !window.XLSX) return null;
    try {
      return XLSX.utils.decode_range(ref);
    } catch (error) {
      return null;
    }
  }

  function rangeText(range) {
    if (!range) return "未知范围";
    return cellName(range.s.r, range.s.c) + ":" + cellName(range.e.r, range.e.c);
  }

  function rangeSize(range) {
    if (!range) return { rows: 0, columns: 0 };
    return { rows: range.e.r - range.s.r + 1, columns: range.e.c - range.s.c + 1 };
  }

  function readRows(sheet) {
    const range = rangeFromRef(sheet && sheet["!ref"]);
    if (!range) return [];
    return XLSX.utils.sheet_to_json(sheet, {
      header: 1,
      range: {
        s: { r: 0, c: 0 },
        e: {
          r: Math.min(range.e.r, 1199),
          c: Math.min(range.e.c, 63)
        }
      },
      defval: null,
      raw: true,
      blankrows: true
    });
  }

  function numericValue(value) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value !== "string") return null;
    const cleaned = value.trim().replace(/,/g, "").replace(/％/g, "%");
    if (!cleaned) return null;
    const percent = cleaned.endsWith("%");
    const number = Number(percent ? cleaned.slice(0, -1) : cleaned);
    return Number.isFinite(number) ? (percent ? number / 100 : number) : null;
  }

  function dateLike(value) {
    if (value instanceof Date) return !Number.isNaN(value.getTime());
    if (typeof value === "number") return value > 20000 && value < 80000;
    const candidate = text(value).trim()
      .replace(/[年/.]/g, "-")
      .replace(/月/g, "-")
      .replace(/日.*$/, "")
      .replace(/T.*$/, "");
    return /^\d{4}-\d{1,2}-\d{1,2}$/.test(candidate);
  }

  function valueKind(value) {
    if (isEmpty(value)) return null;
    if (dateLike(value)) return "date";
    if (numericValue(value) !== null) return "number";
    return "text";
  }

  function kindLabel(kind) {
    return { date: "日期", number: "数值", text: "文本" }[kind] || kind;
  }

  function typeLabel(type) {
    return { date: "日期", number: "数值", percent: "百分比", text: "文本" }[type] || type;
  }

  function inferType(header, samples) {
    const normalized = normalize(header);
    const values = samples.filter((value) => !isEmpty(value));
    const numericCount = values.filter((value) => numericValue(value) !== null).length;
    const dateCount = values.filter(dateLike).length;
    if (/(日期|时间|date|day|周|月份)/i.test(normalized) || (values.length && dateCount / values.length >= 0.7)) {
      return { type: "date", role: "date", unit: "" };
    }
    if (/(占比|比例|率|percent|%)$/i.test(normalized) || text(header).includes("占比")) {
      return { type: "percent", role: "measure", unit: "%" };
    }
    if (values.length && numericCount / values.length >= 0.7) {
      const unit = /(金额|收入|流水|元|价格|费用)/.test(text(header)) ? "元" : "";
      return { type: "number", role: "measure", unit };
    }
    return { type: "text", role: "dimension", unit: "" };
  }

  function headerCandidates(rows) {
    const candidates = [];
    const limit = Math.min(rows.length, HEADER_SCAN_ROWS);
    for (let index = 0; index < limit; index += 1) {
      const values = (rows[index] || []).filter((value) => !isEmpty(value));
      if (!values.length) continue;
      const unique = new Set(values.map(normalize)).size;
      const keywords = values.filter((value) =>
        /(日期|时间|人数|数量|金额|收入|占比|比例|率|名称|类型|渠道|平台|用户|模式|状态|平均|p50|ltv)/i.test(text(value))
      ).length;
      const numeric = values.filter((value) => numericValue(value) !== null).length;
      candidates.push({
        row: index + 1,
        score: unique * 2 + keywords * 3 - numeric,
        nonEmpty: values.length,
        preview: values.slice(0, 5).map(text)
      });
    }
    return candidates
      .sort((left, right) => right.score - left.score || left.row - right.row)
      .slice(0, 5)
      .sort((left, right) => left.row - right.row);
  }

  function buildSchema(sheet, rows, headerRow, dataStartRow) {
    const headerIndex = Math.max(0, headerRow - 1);
    const dataIndex = Math.max(headerIndex + 1, dataStartRow - 1);
    const headers = rows[headerIndex] || [];
    const dataRows = rows.slice(dataIndex);
    const seen = new Map();
    const diagnostics = [];
    const columns = headers.map((value, sourceIndex) => {
      const sourceHeader = text(value).trim();
      const normalizedHeader = normalize(sourceHeader);
      const keyBase = normalizedHeader || "column" + (sourceIndex + 1);
      const count = (seen.get(keyBase) || 0) + 1;
      seen.set(keyBase, count);
      if (!normalizedHeader) diagnostics.push({ severity: "warning", message: "第 " + (sourceIndex + 1) + " 列没有表头。" });
      if (count > 1) diagnostics.push({ severity: "warning", message: "表头“" + sourceHeader + "”重复。" });
      const samples = dataRows.slice(0, 80).map((row) => row[sourceIndex]);
      const kinds = [...new Set(samples.map(valueKind).filter(Boolean))];
      if (kinds.length > 1) {
        diagnostics.push({
          severity: "warning",
          message: "第 " + columnName(sourceIndex) + " 列（" + (sourceHeader || "未命名") + "）存在混合类型：" +
            kinds.map(kindLabel).join("、") + "。"
        });
      }
      const inferred = inferType(sourceHeader, samples);
      return {
        key: keyBase + (count > 1 ? "_" + count : ""),
        sourceHeader: sourceHeader || "未命名列 " + (sourceIndex + 1),
        aliases: sourceHeader ? [sourceHeader] : [],
        sourceIndex,
        ...inferred
      };
    });
    if (!columns.length) diagnostics.push({ severity: "error", message: "选定表头行没有可用列。" });
    const totalRows = dataRows.reduce((rowsWithTotals, row, index) => {
      const hasTotalMarker = row.some((value) => /^(合计|总计|小计|总和|total|subtotal)$/i.test(text(value).trim()));
      if (hasTotalMarker) rowsWithTotals.push(dataIndex + index + 1);
      return rowsWithTotals;
    }, []);
    if (totalRows.length) {
      const shownRows = totalRows.slice(0, 5).map((row) => "第 " + row + " 行").join("、");
      diagnostics.push({
        severity: "info",
        message: "检测到合计行（" + shownRows + (totalRows.length > 5 ? "等" : "") + "），当前按保留合计策略处理。"
      });
    }
    return {
      version: 1,
      id: sheet.id + "-schema",
      sheetId: sheet.id,
      sheetMatcher: { name: sheet.name, aliases: [] },
      headerRow,
      dataStartRow: Math.max(headerRow + 1, dataStartRow),
      columns,
      rowPolicy: { blank: "drop", preserveTotal: true },
      optional: false,
      parserVersion: "generic-v1",
      diagnostics
    };
  }

  function readSheetMeta(workbook, name, index) {
    const sheet = workbook.Sheets[name] || {};
    const loadedRange = rangeFromRef(sheet["!ref"]);
    const fullRange = rangeFromRef(sheet["!fullref"] || sheet["!ref"]);
    const loadedSize = rangeSize(loadedRange);
    const fullSize = rangeSize(fullRange);
    const rows = readRows(sheet);
    const candidates = headerCandidates(rows);
    const recommended = candidates[0] ? candidates[0].row : 1;
    const meta = {
      id: "sheet-" + index + "-" + normalize(name).replace(/[^a-z0-9\u4e00-\u9fff]+/g, "-").slice(0, 48),
      index,
      name,
      hidden: Boolean(workbook.Workbook && workbook.Workbook.Sheets && workbook.Workbook.Sheets[index] && workbook.Workbook.Sheets[index].Hidden),
      range: rangeText(fullRange),
      loadedRange: rangeText(loadedRange),
      rowCount: fullSize.rows,
      columnCount: fullSize.columns,
      loadedRowCount: loadedSize.rows,
      loadedColumnCount: loadedSize.columns,
      truncated: fullSize.rows > loadedSize.rows || fullSize.columns > loadedSize.columns,
      previewRows: rows.slice(0, PREVIEW_ROWS).map((row) => row.slice(0, PREVIEW_COLUMNS)),
      headerCandidates: candidates,
      schema: null,
      diagnostics: []
    };
    if (meta.hidden) meta.diagnostics.push({ severity: "info", message: "这是隐藏工作表。" });
    if (meta.truncated) {
      meta.diagnostics.push({
        severity: "warning",
        message: "当前只读取前 " + meta.loadedRowCount + " 行、" + meta.loadedColumnCount + " 列，原表范围为 " + meta.range + "。"
      });
    }
    if (!loadedRange) meta.diagnostics.push({ severity: "error", message: "工作表没有可读取的数据范围。" });
    if (!candidates.length) meta.diagnostics.push({ severity: "warning", message: "未找到明显的表头行，请手动选择。" });
    meta.schema = buildSchema(meta, rows, recommended, recommended + 1);
    return meta;
  }

  function buildSnapshot(workbook, sourceName, sourceHash) {
    const sheets = (workbook.SheetNames || []).map((name, index) => readSheetMeta(workbook, name, index));
    return {
      version: 1,
      sourceName: sourceName || "未命名工作簿",
      sourceHash: sourceHash || null,
      importedAt: new Date().toISOString(),
      sheets
    };
  }

  async function hashFile(file) {
    const fallback = "metadata:" + [file.name, file.size, file.lastModified].join(":");
    try {
      if (!window.crypto || !window.crypto.subtle) return fallback;
      const digest = await window.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      return "sha256:" + Array.from(new Uint8Array(digest))
        .map((value) => value.toString(16).padStart(2, "0"))
        .join("");
    } catch (error) {
      return fallback;
    }
  }

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }

  function ensureRoot() {
    if (state.root) return state.root;
    const anchor = document.querySelector(".report-controls");
    if (!anchor) return null;
    const root = element("section", "workbook-inspector");
    root.id = "workbookInspector";
    root.setAttribute("aria-labelledby", "workbookInspectorTitle");
    root.innerHTML = `
      <div class="workbook-inspector-heading">
        <div>
          <p class="workbook-inspector-eyebrow">工作簿检查</p>
          <h2 id="workbookInspectorTitle">所有工作表</h2>
          <p id="workbookInspectorSummary" class="workbook-inspector-summary"></p>
        </div>
        <label class="workbook-sheet-picker">选择工作表
          <select id="workbookSheetSelect" aria-label="选择工作表"></select>
        </label>
      </div>
      <div class="workbook-inspector-body">
        <div id="workbookSheetList" class="workbook-sheet-list" role="list" aria-label="工作表列表"></div>
        <div id="workbookSheetDetail" class="workbook-sheet-detail"></div>
      </div>`;
    anchor.after(root);
    root.addEventListener("click", (event) => {
      const button = event.target.closest("[data-sheet-id]");
      if (button) selectSheet(button.dataset.sheetId);
    });
    root.addEventListener("change", (event) => {
      if (event.target.id === "workbookSheetSelect") {
        selectSheet(event.target.value);
      } else if (event.target.matches("[data-schema-role]")) {
        updateSelectedRole(event.target);
      } else if (event.target.matches("[data-schema-field]")) {
        updateSelectedSchema();
      }
    });
    state.root = root;
    return root;
  }

  function selectedSheet() {
    return state.snapshot && state.snapshot.sheets.find((sheet) => sheet.id === state.selectedSheetId);
  }

  function renderSummary() {
    const summary = document.getElementById("workbookInspectorSummary");
    if (!summary || !state.snapshot) return;
    const truncated = state.snapshot.sheets.filter((sheet) => sheet.truncated).length;
    summary.textContent = state.snapshot.sourceName + " · " + state.snapshot.sheets.length +
      " 个工作表" + (truncated ? " · " + truncated + " 个工作表存在读取截断" : "");
  }

  function renderSheetList() {
    const list = document.getElementById("workbookSheetList");
    const picker = document.getElementById("workbookSheetSelect");
    if (!list || !picker || !state.snapshot) return;
    list.replaceChildren();
    picker.replaceChildren();
    state.snapshot.sheets.forEach((sheet) => {
      const option = element("option", "", sheet.name + (sheet.hidden ? " · 隐藏" : ""));
      option.value = sheet.id;
      picker.appendChild(option);

      const button = element("button", "workbook-sheet-item", sheet.name);
      button.type = "button";
      button.dataset.sheetId = sheet.id;
      button.setAttribute("role", "listitem");
      if (sheet.id === state.selectedSheetId) {
        button.setAttribute("aria-current", "true");
        button.classList.add("is-selected");
      }
      const meta = element("span", "workbook-sheet-item-meta",
        sheet.rowCount + " 行 · " + sheet.columnCount + " 列" + (sheet.truncated ? " · 已截取" : ""));
      button.appendChild(meta);
      list.appendChild(button);
    });
    picker.value = state.selectedSheetId || "";
  }

  function renderDiagnostics(sheet, container) {
    if (!sheet.diagnostics.length && !(sheet.schema && sheet.schema.diagnostics.length)) return;
    const title = element("h4", "workbook-detail-subtitle", "诊断");
    const list = element("ul", "workbook-diagnostics");
    [...sheet.diagnostics, ...(sheet.schema ? sheet.schema.diagnostics : [])].forEach((diagnostic) => {
      const item = element("li", "diagnostic-" + diagnostic.severity, diagnostic.message);
      list.appendChild(item);
    });
    container.append(title, list);
  }

  function renderSchema(sheet, container) {
    const schema = sheet.schema;
    const panel = element("div", "workbook-schema-panel");
    const title = element("h4", "workbook-detail-subtitle", "字段识别");
    const controls = element("div", "workbook-schema-controls");
    const headerLabel = element("label", "", "表头行");
    const headerSelect = document.createElement("select");
    headerSelect.dataset.schemaField = "headerRow";
    headerSelect.setAttribute("aria-label", "表头行");
    const candidateRows = new Set([...Array(Math.min(sheet.loadedRowCount || 1, HEADER_SCAN_ROWS)).keys()].map((value) => value + 1));
    sheet.headerCandidates.forEach((candidate) => candidateRows.add(candidate.row));
    [...candidateRows].sort((left, right) => left - right).forEach((row) => {
      const option = element("option", "", "第 " + row + " 行" + (sheet.headerCandidates.some((candidate) => candidate.row === row) ? " · 候选" : ""));
      option.value = String(row);
      option.selected = row === schema.headerRow;
      headerSelect.appendChild(option);
    });
    headerLabel.appendChild(headerSelect);
    const dataLabel = element("label", "", "数据起始行");
    const dataInput = document.createElement("input");
    dataInput.type = "number";
    dataInput.min = String(schema.headerRow + 1);
    dataInput.max = String(Math.max(schema.headerRow + 1, sheet.loadedRowCount));
    dataInput.value = String(schema.dataStartRow);
    dataInput.dataset.schemaField = "dataStartRow";
    dataInput.setAttribute("aria-label", "数据起始行");
    dataLabel.appendChild(dataInput);
    controls.append(headerLabel, dataLabel);
    panel.append(title, controls);

    const table = element("table", "workbook-schema-table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["列", "表头", "类型", "角色", "单位"].forEach((label) => headRow.appendChild(element("th", "", label)));
    head.appendChild(headRow);
    const body = document.createElement("tbody");
    schema.columns.forEach((column) => {
      const row = document.createElement("tr");
      [columnName(column.sourceIndex), column.sourceHeader, typeLabel(column.type)]
        .forEach((value) => row.appendChild(element("td", "", value)));
      const roleCell = document.createElement("td");
      const roleSelect = document.createElement("select");
      roleSelect.dataset.schemaRole = String(column.sourceIndex);
      roleSelect.setAttribute("aria-label", column.sourceHeader + "字段角色");
      ROLE_OPTIONS.forEach((role) => {
        const option = element("option", "", role.label);
        option.value = role.value;
        option.selected = role.value === column.role;
        roleSelect.appendChild(option);
      });
      roleCell.appendChild(roleSelect);
      row.append(roleCell, element("td", "", column.unit || "—"));
      body.appendChild(row);
    });
    table.append(head, body);
    panel.appendChild(table);
    container.appendChild(panel);
  }

  function renderPreview(sheet, container) {
    const title = element("h4", "workbook-detail-subtitle", "原始预览");
    const note = element("p", "workbook-preview-note", "显示前 " + PREVIEW_ROWS + " 行、" + PREVIEW_COLUMNS + " 列；单元格值未经过业务聚合。");
    const table = element("table", "workbook-preview-table");
    const rows = sheet.previewRows || [];
    const width = rows.reduce((max, row) => Math.max(max, row.length), 0);
    if (!rows.length || !width) {
      container.append(title, note, element("p", "workbook-empty", "当前工作表没有可预览内容。"));
      return;
    }
    const body = document.createElement("tbody");
    rows.forEach((row, rowIndex) => {
      const tr = document.createElement("tr");
      for (let columnIndex = 0; columnIndex < width; columnIndex += 1) {
        const cell = element(rowIndex === 0 ? "th" : "td", "", text(row[columnIndex]));
        cell.title = cell.textContent;
        tr.appendChild(cell);
      }
      body.appendChild(tr);
    });
    table.appendChild(body);
    container.append(title, note, table);
  }

  function renderDetail() {
    const container = document.getElementById("workbookSheetDetail");
    const sheet = selectedSheet();
    if (!container || !sheet) return;
    container.replaceChildren();
    const title = element("h3", "workbook-detail-title", sheet.name);
    const meta = element("p", "workbook-detail-meta",
      "第 " + (sheet.index + 1) + " 个工作表 · 原始范围 " + sheet.range + " · 已读取 " + sheet.loadedRange +
      (sheet.hidden ? " · 隐藏" : ""));
    container.append(title, meta);
    renderDiagnostics(sheet, container);
    renderSchema(sheet, container);
    renderPreview(sheet, container);
  }

  function render() {
    const root = ensureRoot();
    if (!root || !state.snapshot) return;
    root.hidden = false;
    renderSummary();
    renderSheetList();
    renderDetail();
  }

  function selectSheet(sheetId) {
    if (!state.snapshot || !state.snapshot.sheets.some((sheet) => sheet.id === sheetId)) return;
    state.selectedSheetId = sheetId;
    renderSheetList();
    renderDetail();
  }

  function updateSelectedSchema() {
    const sheet = selectedSheet();
    if (!sheet) return;
    const headerSelect = document.querySelector('[data-schema-field="headerRow"]');
    const dataInput = document.querySelector('[data-schema-field="dataStartRow"]');
    const headerRow = Number(headerSelect && headerSelect.value) || sheet.schema.headerRow;
    const dataStartRow = Number(dataInput && dataInput.value) || headerRow + 1;
    const source = state.workbook && state.workbook.Sheets[sheet.name];
    const rows = source ? readRows(source) : sheet.previewRows;
    sheet.schema = buildSchema(sheet, rows, headerRow, dataStartRow);
    renderDetail();
    window.dispatchEvent(new CustomEvent("report-workbook-change"));
  }

  function updateSelectedRole(control) {
    const sheet = selectedSheet();
    if (!sheet || !ROLE_OPTIONS.some((role) => role.value === control.value)) return;
    const sourceIndex = Number(control.dataset.schemaRole);
    const column = sheet.schema.columns.find((candidate) => candidate.sourceIndex === sourceIndex);
    if (!column) return;
    column.role = control.value;
    window.dispatchEvent(new CustomEvent("report-workbook-change"));
  }

  async function onWorkbookLoaded(workbook, sourceName, file) {
    try {
      const sourceHash = file ? await hashFile(file) : null;
      state.workbook = workbook;
      state.snapshot = buildSnapshot(workbook, sourceName, sourceHash);
      state.selectedSheetId = state.snapshot.sheets[0] ? state.snapshot.sheets[0].id : null;
      render();
      return clone(state.snapshot);
    } catch (error) {
      console.error("工作簿检查失败", error);
      return null;
    }
  }

  function getSnapshot() {
    return clone(state.snapshot);
  }

  function restoreSnapshot(snapshot) {
    if (!snapshot || !Array.isArray(snapshot.sheets)) return;
    state.workbook = null;
    state.snapshot = clone(snapshot);
    state.selectedSheetId = state.snapshot.sheets[0] ? state.snapshot.sheets[0].id : null;
    render();
  }

  window.reportWorkbench = { onWorkbookLoaded, getSnapshot, restoreSnapshot };
})();
