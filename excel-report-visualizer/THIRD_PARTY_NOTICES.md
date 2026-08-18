# Third-Party Notices

The self-contained `index.html` bundles the following upstream browser libraries. Their license
notices are retained in the embedded distributions; this file records the dependency boundary for
future bundle updates.

## SheetJS Community Edition

- Project: [SheetJS](https://sheetjs.com/)
- Embedded banner: `xlsx.js (C) 2013-present SheetJS`
- License: Apache License 2.0
- Use: read and write local `.xlsx`/`.xls` workbooks in the browser

## Apache ECharts

- Project: [Apache ECharts](https://echarts.apache.org/)
- License: Apache License 2.0
- Use: render the offline report charts

When updating either embedded library, preserve its upstream license/copyright notice, record the
new version or commit here, run the workbook smoke, and recheck `tools/public_release.py validate`.
The visualizer does not send workbook contents to either project or to an external service.
