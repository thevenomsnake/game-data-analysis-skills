# SQL Result Visualization

Use this capability after a successful result file is attached to one known SQL version, or exact results are attached to one linked grouped/overall query bundle. Route presentation by `usage_class` unless the user explicitly chooses a format. It is independent from formalization, Dashboard SQL delivery, and periodic asset maintenance.

Visualize before retention decisions. A `personal_diagnosis` SQL may be discarded after its result is presented, while its exact result and report remain bound for traceability.

## Format Routing

| `usage_class` | Default presentation |
|---|---|
| `personal_diagnosis`, `reusable_diagnostic` | Self-contained diagnostic HTML |
| `ad_hoc_analysis`, `reusable_analysis`, `recurring_delivery` | Analytical `.xlsx` workbook |
| `unclassified` | Infer from the current question: Bug/incident/object diagnosis uses HTML; metric/trend/distribution analysis uses Excel |

An explicit user format overrides the default. Do not create both formats unless they serve different stated decisions.

Diagnostic HTML is an investigation report, not a spreadsheet rendered in a browser:

- Put the investigated object, time range, result status, concise finding, and strongest evidence in the first viewport.
- Prefer event timelines, state transitions, expected-versus-observed differences, anomaly markers, and compact evidence tables. A traditional chart is optional.
- For broad first-pass evidence, provide client-side filters for source/event, event time, state/result/change type, and free-text search; expose session/battle/server identifiers as filters when present. Filtering the report must not require another SQL run.
- Default to all relevant evidence visible or clearly counted. Do not reproduce the SQL mistake by preselecting one hypothesis and hiding the alternatives in the HTML.
- Separate observed facts, interpretation, unresolved questions, and next checks. Never claim a root cause that the result does not prove.
- Keep raw rows bounded; link readers back to the exact retained result instead of embedding a large export.
- Produce one offline `.html` file with no CDN, remote font, image, or network dependency. Escape every result value; never inject source values as raw HTML or executable JavaScript.
- Use a quiet operational layout with dense evidence, restrained color, square or lightly rounded sections, and no landing-page composition.

Bind the report to the exact result attachment:

```powershell
python scripts/sql_query_workspace.py attach-output `
  --root <project-root> --query-id <query-id> --version <n> `
  --file <diagnostic.html> --kind visualization --source-kind skill_generated `
  --source-result-id <result-attachment-id> `
  --title "<literal bug investigation title>" `
  --purpose "呈现本次排查事实、时间线、差异和待验证项" `
  --user-request "<verbatim request>" --function-selection RESULT_VISUALIZATION
```

Diagnostic HTML does not run workbook/chart/Base/style gates. It must still have `lineage_status=exact_result`, project-relative storage, a content hash, and the exact SQL fingerprint. The remaining workbook rules in this reference apply only to analytical Excel mode.

## Asset Model

These are not five peer asset types:

```text
temporary SQL -> optional formal QUERY -> optional DASHBOARD SQL
       |                  |                       |
       +---------- exact execution result -------+
                              |
                              +-> reusable visual Excel
```

- Temporary SQL is one immutable `query_workspace` version.
- Formal QUERY is the retained reusable business query.
- DASHBOARD SQL is a DA execution derivative of a formal QUERY, not a visualization.
- A SQL result is preview/evidence for one exact SQL fingerprint and parameter snapshot, not a reusable dataset.
- A visual Excel is a reusable product derived from one result. Changing only its layout or charts creates another attachment, never another SQL version.
- A grouped/overall visual Excel is derived from two exact results. It stores `source_results` for both members and `lineage_status=exact_results`; it is not attached loosely to the analysis title.

## Trigger

Route to `RESULT_VISUALIZATION` when all are true:

1. The user supplies or points to a returned `.csv` or `.xlsx` SQL result.
2. The exact temporary QUERY, formal QUERY, or DASHBOARD SQL version is known.
3. The user asks for Excel visualization, charts, or a visual analysis workbook.

Do not trigger merely because a SQL has a Dashboard attachment. Do not formalize a temporary query unless the user separately asks to retain it.

## Workflow

1. Resolve the exact project-local SQL path. Block when only a title, “latest SQL”, or an ambiguous family is known.
2. Inspect the full returned result for columns, grain, row count, nulls, ratios, categories, dates, and useful comparisons. The same scan creates `result_time_coverage_v1`; never report the requested window as the observed window.
3. Load the Spreadsheets skill and create an `.xlsx` workbook with the approved spreadsheet authoring runtime.
4. Keep source data and presentation separate. Use a clear data sheet plus one or more analysis/chart sheets; use number/percent formats appropriate to the metric.
5. Define a compact chart contract before authoring: analytical question, chart family/variant, source range, category and series order, metric denominator/unit, axis domain, title/context band, and final viewport.
6. Include at least one decision-useful native Excel chart. Do not call a plain copied table a visualization.
7. Run the canonical pre-bind validator against the exact returned result(s). It reuses the binder audit; it is not a second style implementation:

```powershell
python scripts/validate_style.py <candidate-visualization.xlsx> `
  --result-file <returned-result.xlsx> `
  --format json
```

Repeat `--result-file` for grouped/overall bundles. Repair every blocker before continuing.

When the SQL window includes today, a result with `requirement_status` of
`not_observable` or `anomalous` is evidence only. Keep the file for
traceability, but do not confirm or formalize it. Date-level output is valid
when recorded as `precision=date`; it cannot be described as an intraday
cutoff. Scalar output should expose `实际数据开始时间` and
`实际数据结束时间`. See `time-integrity.md`.
8. Verify key ranges/formulas and render every sheet visually through the Spreadsheets skill. Inspect the actual exported viewport, not only chart-object existence.
9. Bind both files in one final step:

```powershell
python scripts/sql_result_visualization.py bind `
  --root <project-root> `
  --sql-path <exact-vNNN.sql> `
  --result-file <returned-result.xlsx> `
  --visualization-file <verified-visualization.xlsx> `
  --result-title "<result title>" `
  --result-purpose "<what this result proves or shows>" `
  --visualization-title "<visual workbook title>" `
  --visualization-purpose "<what a reader can compare or understand>" `
  --user-request "<verbatim request>" `
  --function-selection RESULT_VISUALIZATION `
  --format json
```

Use `--user-confirmed` only when the user also confirms result correctness. Returning a file alone records an observed result; it does not verify the SQL.

10. Finish only when the single-result or bundle receipt returns `status=ready`. Return clickable absolute paths for every SQL, retained result preview, and visual workbook.

At binding time, inspect each new reusable `.xlsx` once and persist its bounded
`workbook_manifest_v1`: sheet names/visibility, chart count/titles, and fixed
limits only. Do not persist cell values in the manifest. Record
`preview_status=not_available` when no separately generated static preview
exists; that state does not block binding or catalog visibility. Catalog refresh
must consume this persisted metadata and never reopen every historical workbook.

## Value-Only Refresh

Do not rebuild an existing visual workbook merely because result values changed. First run a deterministic assessment:

```powershell
python scripts/sql_result_visualization.py refresh-values `
  --root <project-root> `
  --sql-path <exact-new-vNNN.sql> `
  --base-visualization <managed-prior-visualization.xlsx> `
  --result-file <new-returned-result.xlsx> `
  --dry-run `
  --user-request "<verbatim request>" `
  --function-selection RESULT_VISUALIZATION `
  --format json
```

The fast path is eligible only when all of these remain unchanged:

- SQL `logic_fingerprint`; execution/date parameters may differ, but filters, formulas, joins, grain, dimensions, and output logic may not.
- Result columns and row count.
- One exact source-table footprint in the managed prior workbook, matched against its bound prior result.
- The bounded refresh cell budget; large structural changes return to normal workbook authoring.

When eligible, rerun with `--out <project-local-candidate.xlsx>`. The command replaces only source-table values, preserves formulas/charts/styles/conditional formatting, requests full Excel recalculation, and never overwrites the prior workbook. Numeric-looking metric values become Excel numbers; IDs, codes, dates/times, account/session keys, and meaningful leading zeros remain text. A percent sign is converted to a decimal value.

`rebuild_required` is a routing decision, not an error to bypass. Rebuild when SQL logic, fields, rows, source-table layout, or presentation requirements changed. After a candidate is produced, run the existing style audit, render it once, and use the normal `bind` command so the new workbook binds to the new exact result.

## Presentation Grammar

Treat an analysis workbook as `exact table + decision visual + necessary evidence`, not as a fixed report template.

- Keep the untouched or typed exact result on a source sheet and organize presentation sheets by analytical question. Do not force a fixed sheet count, fixed row numbers, or one chart beside every table.
- Let the table carry exact values, natural ordering, Base, and the metric-aware overall row. Let the chart carry only the comparison, shape, trend, or composition that is faster to understand visually.
- Source-sheet formulas are optional. Use them when the workbook has a verified recalculation path and formulas make refresh/audit materially better; otherwise write typed presentation values and rely on the persisted exact SQL/result binding. Never ship uncached formulas that leave tables or charts blank.
- Add a compact evidence band immediately below or beside the relevant table/chart only when a reader could otherwise make a wrong decision. Use literal labels: `读图` for a non-obvious encoding, `质量说明` for reliability limits, `排除说明` for material exclusions with counts, `结果Base` for the effective sample, and `口径提示` for a definitional boundary.
- Keep metadata, context, and evidence rows on one line by default. Set `wrap_text=false`, leave contiguous blank cells for natural text spill, and keep a compact fixed row height. If another populated cell would stop the spill, widen the value column or move the next key-value pair to another row; do not solve it by automatic wrapping. Compact metadata wrapping is a blocker; a deliberately wrapped long-form evidence note is allowed but enters review. Table headers may use deliberate, bounded wrapping when a compact column cannot remain readable.
- Do not repeat window, Base, source, or caveat text in every section when one attached workbook-level statement applies unchanged. Evidence bands are part of the analysis, not decorative grey rows or usage instructions.
- When the applicable project/user small-sample threshold is triggered, keep the exact values and Base visible but omit comparative marks for that section. State the reason briefly. If every candidate view is untrustworthy, return no reusable visualization rather than manufacturing a chart to satisfy the workflow.

## Chart Selection

Choose the chart from the question and metric structure. Horizontal stacked bars are one option, not the house default.

| Analytical question | Default presentation | Boundary |
|---|---|---|
| Compare one magnitude or rate across categories | Sorted horizontal bar for long labels; vertical bar for a short ordered set | Start magnitude axes at zero; table retains exact values and Base |
| Show parts of one proven whole across cohorts | Horizontal `100%` stacked bar plus exact-value table | Every row shares one complete denominator and reconciles to `100%` |
| Show change over time | Line chart; use columns for a few discrete periods | Preserve date order; no smoothed line that invents intermediate values |
| Show an ordered bucket distribution | Ordered bar/column chart plus table data bars when useful | Preserve bucket order; do not sort by value |
| Show retention/cohort structure | Light bounded heatmap for the matrix; companion line only for a useful cohort comparison | Keep text contrast safe and show Base; do not use dark unreadable endpoints |
| Compare treatment/control or segments | Clustered bars or lines with an adjacent absolute/lift table | Keep one stable color per group and name the comparison Base |
| Show funnel progression | Ordered horizontal bars with step conversion/drop-off in the table | Do not use decorative funnel shapes or mix incompatible denominators |
| Explain an additive delta | Waterfall only when the components reconcile exactly | Non-additive attribution stays a table or another honest comparison |
| Compare mean and percentiles | Plot every returned percentile; use separate or grouped marks when needed | Do not treat `mean - P50` as a universal additive decomposition |
| Tiny or non-comparable sample | Compact table with Base and reason | No comparative chart for that section |

Prefer direct labels when they remain legible and remove a redundant legend. Keep a bottom legend only when several series genuinely need a shared key. One chart may use one focal color plus neutral context; do not color every category merely because it exists.

Use color to distinguish semantic series, not individual rows. A single-series chart uses primary dark. Two to four peer series use the governed muted sequence (`#24445D`, `#6F96B8`, `#5B8279`, `#A6B0B9`) and retain that mapping everywhere in the workbook. Use orange only for one intentional highlight. If a chart has more than four peer series, keep one focal series and neutralize the rest or split the view. `chart_audits.color_review_recommended` records a multi-series chart whose materialized series colors remain unresolved or collapse to one color; it prompts visual review without blindly rejecting legitimate same-color constructions.

## Copy And Export Geometry

Design chart and table footprints for predictable copying into a report or slide without forcing one rigid worksheet layout.

- Use approximately `22–24 cm × 8–11 cm` for a full-width chart and `11–12.5 cm × 7–8.5 cm` for a half-width chart; adjust within those bands for label length and series count.
- Keep a copied table range independent from adjacent charts and blank columns. Titles and evidence bands use independent cells with `Center Across Selection` or continuous styling, never wide merges that enlarge the copied range.
- Keep chart title, unit, Base/window context, and source/as-of note attached closely enough that the chart remains interpretable when copied alone.
- Check the final workbook at normal Excel zoom and at the intended pasted size. A technically present title or legend that becomes unreadable after paste is still a visual failure.

## Grouped And Overall Bundle

QUERY generation creates `query_analysis_bundle_v1` only when the grouped result cannot produce an exact useful overall statistic. Do not create two SQLs unconditionally.

Attach results independently as they arrive:

```powershell
python scripts/sql_result_visualization.py attach-bundle-result `
  --root <project-root> --bundle <qab-id> --role grouped `
  --result-file <grouped-result.xlsx> --user-confirmed `
  --user-request "<verbatim request>" --function-selection RESULT_VISUALIZATION

python scripts/sql_result_visualization.py attach-bundle-result `
  --root <project-root> --bundle <qab-id> --role overall `
  --result-file <overall-result.xlsx> --user-confirmed `
  --user-request "<verbatim request>" --function-selection RESULT_VISUALIZATION
```

The first result remains attached with `awaiting_other_result`; never discard or reattach it to the other SQL. Build one workbook only after the bundle reports `ready_for_visualization`. It must contain separate `分组结果` and `整体结果` source sheets plus the presentation sheet, then bind it once:

```powershell
python scripts/sql_result_visualization.py bind-bundle `
  --root <project-root> --bundle <qab-id> `
  --visualization-file <verified-visualization.xlsx> `
  --visualization-title "<literal title>" `
  --visualization-purpose "<comparison purpose>" `
  --user-request "<verbatim request>" --function-selection RESULT_VISUALIZATION
```

The binder requires both exact result ids, validates each result schema against its SQL output, rejects parameter/source/filter/metric-contract drift captured by the bundle, and returns `sql_result_visualization_bundle_receipt_v1`.

## Grouped Summary

Every grouped table defaults to one meaningful bottom summary after its data rows and before source/as-of notes. Select the summary automatically from metric semantics, not from a fixed `SUM` template. Label it by its meaning: `合计`, `整体平均`, `加权平均`, `整体转化率`, `P50`, or another literal metric name. Do not duplicate an equivalent source-provided KPI, but still keep one clearly identifiable overall result for the grouped table.

| Metric type | Useful bottom summary |
|---|---|
| Additive count, times, amount, duration | `合计`: sum only when groups are non-overlapping and complete |
| Rate, ratio, conversion, retention | `整体…率`: total numerator divided by total denominator |
| Mean or per-user average | `整体平均` or `加权平均`: valid group weights, or total underlying sum divided by total count |
| Column-normalized distribution | Omit a redundant row of `100%`; prefer an exact source-level mean, median, or percentile when useful |
| Distinct users across overlapping groups | Source-level overall distinct count; never sum group distinct counts |
| Median, percentile, quantile | Recompute from source-level values; never average group statistics |

For a bucketed duration/death-count distribution, a useful summary is each segment's exact player-level average plus every source-provided percentile point when percentiles are present, not repeated `100%`. Compute it from the pre-bucket player metric in SQL or retain exact source-provided overall fields. Do not approximate with bucket midpoints unless the user explicitly asks and every open-ended bucket has a documented representative value.

Verify that the result contains the weights, numerator/denominator, player-level metric, or source-provided overall value needed by the summary. If not, stop finalization and request that the result SQL output the missing overall evidence; do not replace it with `100%`, a simple average, or an empty decorative row. A simple average of group averages is invalid unless all group weights are proven equal. Exclude the summary row from chart and conditional-format ranges unless it is the intended comparison.

### Percentile completeness

Treat the percentile/quantile columns returned for one visualized result or bundle as a closed metric family. If any percentile point is present, every available point must appear in at least one plotted chart series or category across the final workbook. Never select only P50 when the result also contains P75/P90/P95/P99 or other points.

The source table, chart title, subtitle, note, or legend text does not prove that a point was plotted. Splitting a dense family across clearly labeled companion charts is allowed, but the union of plotted series/categories must cover the full returned family. Do not invent percentile points absent from the result and do not replace tail points with an average.

Before binding, `sql_result_visualization.py` derives the available percentile family from every exact result column, resolves chart series/category label references, and blocks the workbook when any point is missing. Repair and rerender the workbook; do not bypass the gate by renaming the title.

### Base completeness

Treat Base as part of the evidence, not decoration. When a returned result contains ratio/rate fields, or the workbook contains a normalized composition, finalization requires an explicit same-grain Base/denominator field in the exact result and visible Base evidence on a sheet that contains the chart.

- One Base shared by the whole visual may appear in the subtitle, but its value must be provably common in the result.
- A Base that varies by cohort/date/bucket must appear as a nearby `n=` label or adjacent Base column at the same grain. Do not collapse it into one total.
- For people-based analysis, Base means the corresponding player/user/person count. For other ratios, use the entity or event denominator that defines the statistic.
- Base present only in a source-data sheet, distant note, hidden range, or external explanation does not pass.
- Keep numerator/denominator together when both are decision-useful, for example `18.4% (2,341 / 12,726)`.
- Add a small-sample warning when the business threshold is known and triggered. Do not encode Base through bubble area or bar width by default.

The deterministic audit proves field presence and nearby display. It cannot prove business denominator meaning from labels alone, so the receipt keeps manual checks for exact denominator, cohort/date/filter alignment, and small-sample policy. If the result lacks Base, repair QUERY and rerun it; never fabricate Base in Excel.

For a compact percentage table, start from `dimensions | Base | optional total penetration | percentage breakdown`. This is a default, not a universal column law. Keep an absolute numerator beside its percentage only when scale itself changes the decision, supports an operational action, or prevents a small-Base misread. Keep multiple Base columns only when the percentages genuinely use different denominators and a combined table remains easier to compare than separate tables. Otherwise remove repeated absolute/Base information. `base_coverage.display_layout` records density observations such as multiple Base columns, Base after ratios, or an absolute-plus-ratio pair; these are semantic review prompts, not automatic blockers.

## Distribution Encoding

Route conditional formatting by metric semantics so the encoding matches what the number means:

| Cell meaning | Default encoding |
|---|---|
| Percentage, rate, ratio, share, retention, conversion | Sequential color scale |
| Signed percentage-point/rate change where direction is meaningful | Diverging color scale centered at `0` |
| Non-negative count, amount, duration, frequency, or other comparable magnitude | Solid data bar with the exact value visible |
| Identifier, sort key, bucket boundary, date/time, label, Base shown only as context | No conditional formatting |

For percentages and rates:

- Use a restrained single-hue sequential scale from white/light blue to primary blue. Higher saturation means larger share, not “better.” Do not use Excel's default red-yellow-green scale for ordinary proportions.
- Use one shared scale across cells that readers are expected to compare. Preserve the declared source scale (`0..1` or `0..100`), anchor ordinary proportions at zero, and use one global body maximum or a documented semantic cap. Never auto-scale every column independently.
- Keep one shared percentage scale when cross-column magnitude is the comparison, as in several social-disconnection rates over the same player groups. Use different light series-linked scales only when color must map each table metric to a distinct chart series and readers are not expected to compare color intensity across those columns.
- Use a diverging negative-neutral-positive scale only for signed change metrics where direction has business meaning. Center it explicitly at zero; semantic red/green must not decorate unsigned shares.
- Keep one stable dark foreground across the scale and require at least `4.5:1` contrast at every declared endpoint. Excel color scales control fill and do not reliably flip the value font between dark and light, so repair an unsafe scale by lightening its endpoint rather than assigning white text that disappears at the light end.

For numeric magnitudes:

- For a compact table of non-negative, same-unit values, add solid, borderless, low-saturation data bars and keep `showValue=true`. Use `#AFC6D8` for an overall/focal column and `#D2DFE9` for segment/context columns.
- For a same-unit matrix, use one global scale across all comparable body cells. If the matrix is too dense or a companion chart already carries the comparison, limit bars to the decision-driving or overall column.
- When several retained Base columns map one-to-one to distinct chart series, use the governed pale paired tints to reinforce the mapping without competing with the percentages. When Base columns are simply comparable magnitudes, keep one shared pale data-bar color.
- Do not use ordinary data bars for mixed signs, incompatible units, identifiers, severe long tails that flatten most rows, or more than about 30 rows. Use a signed/dedicated visual, split view, documented transformation, or no conditional formatting.

For every conditional-format range, preserve natural bucket order, keep exact formatted values visible, exclude headers and summary/total rows, ignore blanks/`NULL`, and apply only one encoding per cell. `validate_style.py` reads the actual workbook color-scale rules and cell foregrounds; an unresolved endpoint or any foreground/background pair below `4.5:1` blocks with `VIS-CONTRAST-001`. Color scales and data bars supplement the table; they do not replace a chart when shape, trend, or multi-series comparison is the decision surface.

## Composition Charts And Chart QA

Use a horizontal 100% stacked bar when the question is how one complete denominator splits across ordered parts for several cohorts. Use a grouped bar or exact table when the series do not share one denominator or do not form one complete whole.

- Reconcile every plotted cohort to `100%` within the precision of the source. Do not normalize incomplete or incompatible rows merely to make the bars equal length.
- Prefer native `percentStacked`. If the renderer only supports a regular stacked bar over already-normalized `0..1` values, set the value axis explicitly to `min=0`, `max=1`, and a percentage number format. Never leave a normalized composition axis on automatic scaling; a `120%` endpoint is a hard failure.
- Preserve semantic bucket order for tenure, level, duration, and ordinal states. Put an overall/weighted reference first or last and distinguish it from cohort rows without changing the shared scale.
- Name series as literal states such as `造物0级`, `造物1级`, `造物2级`, and `造物3级及以上`; do not rely on ambiguous interval shorthand alone. Use a light-to-dark sequential role palette when the states are ordered.
- Keep a compact exact-value table available when small segments matter. Use direct percentage labels only when they remain legible; do not force labels into slivers or hide the only exact values behind color.

Every chart must pass both deterministic package audit and rendered-viewport QA:

1. Give every chart one visible literal title. Set title overlay to false and reserve vertical space above the plot; a title touching or covering the first mark is a hard failure.
2. Put unit, denominator/Base, cohort/date window, and material filter context in a visible subtitle or evidence band attached to the chart. Do not encode that context only in a distant notes sheet.
3. Set number formats and honest domains explicitly where automatic axes can change meaning. Normalized composition is always `0–100%`; ordinary magnitude bars start at zero unless a documented chart family permits a focused scale.
4. Confirm category order, series order, legend order, source ranges, and summary-row exclusion against the source table. A visually plausible transposition is still a failure.
5. Render the final exported sheet and check at normal zoom for clipped text, title/legend/mark collisions, excess empty space, unreadable colors, and values extending beyond declared bounds.

`sql_result_visualization.py` rejects missing chart titles, plot-overlaid chart titles, and normalized stacked charts without an explicit `0..1` percentage value axis. Treat a rejected workbook as an authoring defect; repair and render it again before binding.

## Workbook Ergonomics

Treat merge behavior by semantic role, not as a workbook-wide ban or a decoration default.

- On a chart-containing presentation sheet, vertically merge a repeated parent-dimension label across its contiguous child rows when that makes the hierarchy easier to scan. For example, one `战斗服数量=1` cell may span the rows `游玩天数=1`、`游玩天数=2`、`游玩天数=3-4`. Center the label vertically and keep the merge entirely inside the table footprint.
- A nested table header may merge horizontally across its populated child-header columns, such as one `累计时长分位点` header above `P10/P25/P50/P75/P90`. Keep at least one independent table column outside the merged header group so it cannot masquerade as a workbook-wide title.
- Do not merge merely because adjacent cells look similar. The merged label must describe one exact parent group, every covered row must retain populated child/detail cells to its right, and the range must not cross a header, subtotal, total, weighted-average, percentile, or unrelated group row.
- Keep source/result sheets unmerged. Their cells remain independently filterable, sortable, and reusable; semantic grouping belongs only on the presentation sheet.
- Do not horizontally merge workbook titles, chart titles, subtitles, evidence bands, source/as-of notes, or decorative whitespace. A wide title merge can enlarge a copied table selection and pull an adjacent chart or empty columns into a screenshot. Use Excel `Center Across Selection` (`horizontal=centerContinuous`) for centered titles, or place a left-aligned title only in the first cell and continue fill/borders across independent cells.
- Do not enable automatic wrapping for metadata, context, evidence, source, or as-of rows. Their default presentation is a single line that can overflow across intentionally blank cells. Use explicit bounded wrapping only for table headers or a deliberate long-form note whose row is designed for it.
- Table-internal vertical group merges do not authorize merged metric values or blank placeholders. Keep metrics, totals, formulas, and chart source ranges independently addressable.

Binding records allowed table-group merges and blocks unsupported merges. The deterministic allowlist accepts multi-row, single-column parent labels with populated detail cells on every covered row, plus single-row nested header groups whose next row contains one populated child header per merged column. Both forms require a chart-containing presentation sheet and must remain inside a larger table. Any workbook/title-band merge, source-sheet merge, summary merge, empty merge, rectangular multi-row/multi-column merge, or merge without supporting child cells fails `VIS-LAYOUT-001`.

## Institutional Report Style

### Governed adoption

`assets/viz_tokens.json` is the stable source for the already-approved role palette, conditional-format foreground/contrast floor, number formats, and Base evidence vocabulary. `validate_style.py` and the final binder consume the same audit path, so package/chart/Base/contrast rules cannot drift between preflight and persistence.

Font-family mandates, a universal four-color ceiling, fixed `3x` card indexing, delta-only annotations, and unconfirmed sequential/diverging palettes remain candidate guidance rather than hard gates. Promote them only after an explicit house-style decision, runnable authoring support, and focused regression evidence. Keep spreadsheet authoring in the Spreadsheets skill; this capability owns evidence completeness and deterministic binding, not a duplicate workbook framework.

This system adapts recurring patterns observed in public Goldman Sachs, Morgan Stanley, and JPMorgan Chase reports and research charts; it is not a bank brand template. Public sources do not establish reusable official color values or fonts. The research basis is recorded in `docs/research/professional-data-report-visual-style.md`.

Build hierarchy before adding color. Workbook and chart titles must be literal, stable descriptions of the analytical object, such as `新进用户单战斗服最大时长分析` and `单战斗服最大时长分布`. A chart title identifies metric × dimension/segment, with period only when needed; it must not be a finding, recommendation, or directional claim. Put units, Base, and observation window in a subtitle. Put a supported insight in an optional `观察：...` annotation below the title, never in place of it. Keep source, as-of date, and footnotes with the visual.

Use this Excel role palette; these are project adaptation tokens, not Goldman brand colors:

| Role | Default | Use |
|---|---|---|
| Canvas | `#FFFFFF` | Sheet and chart background |
| Section background | `#F4F5F6` | Sparse section separation, not every cell |
| Ink | `#17212B` | Titles, totals, primary text |
| Secondary text | `#5F6B76` | Units, sources, as-of dates, notes |
| Divider/grid | `#D9DEE3` | Thin horizontal rules and essential gridlines |
| Primary series | `#6F96B8` | Current or decision-driving series |
| Primary dark | `#24445D` | Total line, latest key value, one focal comparison |
| Context series | `#B8C1C9` | History, benchmark, de-emphasized comparison |
| Positive | `#2F7669` | Positive state only |
| Attention | `#9A7427` | Warning/attention state only |
| Negative | `#9B4A43` | Negative state only |

Do not assign six equal-weight colors merely because six series exist. Start with one primary series and neutral context; expand to at most three or four categorical colors only when categories have equal analytical importance. Keep one category/metric on the same role color across every sheet. Semantic green, amber, and red never decorate arbitrary categories.

Use white tables with bold dark headers, thin horizontal rules, right-aligned numbers, and consistent decimals. Avoid full-cell grids and large dark header bands unless the workbook already has an approved house style. Make `总计` the strongest row with dark ink, bold type, and a top rule; do not use a saturated fill. In charts, give Total the darkest/thickest line and direct label, put bar totals above bars, and keep component labels inside only when legible.

Avoid Office-default rainbow colors, decorative gradients, 3D charts, glossy effects, heavy shadows, dark chart backgrounds, colored worksheet canvases, and a different accent for every cell. Approved percentage color scales are the exception: keep them restrained, semantic, and limited to comparable table cells. Before delivery, inspect the workbook in grayscale or mentally remove color: labels, ordering, line styles, and annotations must still communicate the result.

## Storage

- Workspace SQL stores result and visualization attachments on the exact version in `query_workspace/index.json` and its version meta.
- Formal QUERY/DASHBOARD stores the result as manifest run evidence and the workbook under that result binding.
- Raw result evidence follows the 10 MB rule: full at or below 10 MB, verified slice above 10 MB.
- Visual Excel is reusable and always stored in full, even above 10 MB.
- Persist project-relative paths, original file names, and hashes; never persist the external absolute intake path.

A single-result reusable output records one exact `source_result_id` and `lineage_status=exact_result`. A grouped/overall output records both exact `source_results`, `lineage_status=exact_results`, and its analysis-bundle reference. Every output retains `source_sql_fingerprint`; the catalog exposes `derived_from_result` and `has_visualization` relationships.

## Historical Migration

Run migration only as an explicit maintenance/evolution task:

```powershell
python scripts/sql_result_visualization.py migrate `
  --root <project-root> --dry-run `
  --user-request "<verbatim request>" `
  --function-selection PROJECT_ADMIN `
  --format json
```

The migration binds a reusable output only when its SQL version has exactly one result attachment. Multiple or missing results become `unresolved_legacy`; never infer result lineage from similar file names.

For a user-confirmed older grouped/overall pair, create the exact bundle first, then use the RESULT_LINEAGE_ORGANIZATION `adopt_bundle` action. Do not reattach or duplicate result files merely to satisfy the new contract.
