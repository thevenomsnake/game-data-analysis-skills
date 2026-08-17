# Local Source Workspace

Use this capability for folders that contain potentially useful code references, TLOG documents, or external reference files that have not been reviewed as project knowledge. Complete planning/config folders use `PLANNING_SOURCE`; do not configure them here.

## Boundary

The source workspace is local discovery, not a knowledge base:

```text
configured root
  -> discovered candidate
  -> selected_not_reviewed
  -> explicit KNOWLEDGE review/register
  -> optional project binding
```

Only `KNOWLEDGE` may create `knowledge-base/source_snapshots`, datasets, contracts, or project bindings. QUERY, REVIEW, FORMALIZE, and DASHBOARD must continue using active bindings only. A candidate file may explain what evidence is available, but it cannot supply a current mapping, owner, enum, or metric truth.

## Local Storage

```text
.local/source_roots.json
.local/source_workspace/catalog.json
.local/source_workspace/selections/<selection_id>.json
```

These files are Git-ignored. Only `source_roots.json` stores machine-specific absolute paths. Catalog candidates and selection receipts persist `root_id` plus paths relative to that root; they never persist the absolute root.

Do not add source roots to `project_config.json`. Project config is tracked and portable; machine folder addresses are neither.

## First Use

When a member asks to inspect an unmanaged code/document/reference folder and no matching root is configured, ask once for:

- the local folder;
- a stable root ID, such as `code-references`;
- source kind;
- optional project scope;
- whether subfolders should be scanned.

Then configure it:

```powershell
python .\sql-engineering\scripts\source_workspace.py configure `
  --repo-root . `
  --root-id code-references `
  --kind code_reference `
  --path <local-folder> `
  --project DEMO_ANALYTICS `
  --recursive `
  --user-request "初始化 DEMO_ANALYTICS 代码资料候选目录" `
  --function-selection SOURCE_WORKSPACE
```

A root inside this repository must be under `.local/`. External folders remain read-only; the tool never moves or edits their files.

## Discover And Select

Run `scan` only for an explicit source-discovery or knowledge-management request. Scans are extension-filtered, do not follow links, skip hidden/temp files, and stop at the configured file limit instead of writing a partial catalog.

```powershell
python .\sql-engineering\scripts\source_workspace.py scan `
  --repo-root . `
  --root-id code-references `
  --project DEMO_ANALYTICS `
  --user-request "扫描 DEMO_ANALYTICS 代码资料候选" `
  --function-selection SOURCE_WORKSPACE
```

Discovery records name, relative path, size, and modified time. It deliberately does not hash every workbook. Select one exact candidate to compute its content hash:

```powershell
python .\sql-engineering\scripts\source_workspace.py select `
  --repo-root . `
  --root-id code-references `
  --relative-path <root-relative-file> `
  --project DEMO_ANALYTICS `
  --user-request "选择这份代码文件作为资料候选" `
  --function-selection SOURCE_WORKSPACE
```

`selected_not_reviewed` still means unconfirmed. An explicit `KNOWLEDGE` request must inspect the source, decide projections and usage, copy the exact selected bytes into managed intake, register a version, review its diff, and bind only after approval.

Ordinary QUERY must not scan source roots automatically. `同步资料` also excludes `.local/` candidates; only reviewed knowledge and formal assets may enter Git.
