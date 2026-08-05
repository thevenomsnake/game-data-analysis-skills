# Project Contract

Initialize a project once:

```powershell
python <skill-root>/scripts/sql_workspace.py init `
  --root . `
  --project-id example `
  --dialect starrocks
```

This creates:

```text
.sql-engineering/
  project.json
sql-workspace/
  index.json
  temporary/<slug>/vNNN.sql
  retained/<slug>/vNNN.sql
  dashboard/<slug>/vNNN.sql
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
