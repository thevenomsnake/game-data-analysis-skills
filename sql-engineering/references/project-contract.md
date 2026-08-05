# Project Contract

Initialize the repository once:

```powershell
python <skill-root>/scripts/sql_workspace.py bootstrap `
  --root <workspace-root> `
  --project-id example `
  --dialect starrocks
```

This creates:

```text
<workspace-root>/
  sql-projects/
    _asset_catalog/
    _review_inbox/
    _rule_review/
    example/
      .sql-engineering/
        project.json
      sql-workspace/
        index.json
        temporary/<slug>/vNNN.sql
        retained/<slug>/vNNN.sql
        dashboard/<slug>/vNNN.sql
```

The underscore directories are stable cross-project extension points:

- `_asset_catalog`: generated indexes for discovery tools.
- `_review_inbox`: external SQL and evidence awaiting review or intake.
- `_rule_review`: rule dictionary and rule-review outputs.

The public core creates these directories but does not invent catalog, review, or rule content.
Projects live beside them under `sql-projects/<project-id>`.

`bootstrap` is idempotent. It preserves existing files and reuses a matching project. It blocks
when the requested project ID or dialect conflicts with an existing project configuration.

For a standalone project outside this repository layout, `init` remains available:

```powershell
python <skill-root>/scripts/sql_workspace.py init `
  --root <project-root> `
  --project-id example `
  --dialect starrocks
```

`project.json` is the authority for project identity and dialect. Extend it with local table,
time, and rule contracts as needed, but do not hide those choices in the Skill.

Minimal configuration:

```json
{
  "schema_version": "sql_engineering_public_project_v1",
  "project_id": "example",
  "dialect": "starrocks"
}
```

The workspace index is machine generated. Edit SQL by creating a new saved version, not by
hand-editing index entries or old `vNNN.sql` files.
