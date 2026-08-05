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
      sources/
        source-catalog.json
        raw/<source-slug>/vNNN.<original-format>
      knowledge/
        knowledge-catalog.json
        planning/<knowledge-slug>/vNNN.<original-format>
        confirmed/<knowledge-slug>/vNNN.<original-format>
      rules/
        rule-catalog.json
        definitions/<concept-key>/vNNN.json
      context/
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

The public core creates these directories and empty machine catalogs but does not invent source,
knowledge, review, or rule content.
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

`project.json` is the authority for project identity, catalog locations, dialect, and execution
environment routing. Do not hide those choices in the Skill.

Minimal configuration:

```json
{
  "schema_version": "sql_engineering_public_project_v1",
  "project_id": "example",
  "dialect": "starrocks",
  "source_catalog": "sources/source-catalog.json",
  "knowledge_catalog": "knowledge/knowledge-catalog.json",
  "rule_catalog": "rules/rule-catalog.json",
  "context_paths": []
}
```

Optional project context and execution routing:

```json
{
  "context_paths": [
    "context/schema.md",
    "context/platform-manual.md"
  ],
  "execution": {
    "default_environment": "development",
    "environments": {
      "development": {
        "dialect": "starrocks",
        "connection_profile": "development-starrocks"
      }
    }
  }
}
```

Catalog paths are generated and project-relative. `context_paths` and `execution` are optional;
context paths must not point into one person's home directory or private Skill installation.
Configure environments with `sql_workspace.py environment`; keep actual connection details in
the ignored local file described in `database-execution.md`.

The source, knowledge, and rule catalogs are machine managed. Add content through the `source`,
`knowledge`, and explicit `rule` commands. Read `project-onboarding.md` for evidence ownership and
the complete setup sequence.

The workspace index is machine generated. Edit SQL by creating a new saved version, not by
hand-editing index entries or old `vNNN.sql` files.
