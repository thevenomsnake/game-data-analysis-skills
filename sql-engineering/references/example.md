# Worked Example

Use this example when a person or another AI needs to understand what the Skill must produce.
The SQL uses fictional names and demonstrates storage and delivery, not a real database schema.

## User Request

> Create a StarRocks query that counts distinct login users by day for a fixed date range.
> Save it so I can find and revise it later.

## Agent Actions

1. Read the project contract and bootstrap the repository if `sql-projects/` is missing.
2. Replace the fictional source and fields in
   `assets/examples/daily-active-users.sql` with the user's project contract.
3. Keep fixed date values in `params` and save the SQL as a `temporary` query.
4. Run `receipt` against the exact saved `vNNN.sql` file.
5. Return the absolute `delivery_file` path. Do not deliver only a chat code block.

```powershell
python <skill-root>/scripts/sql_workspace.py bootstrap `
  --root <workspace-root> `
  --project-id example `
  --dialect starrocks

python <skill-root>/scripts/sql_workspace.py save `
  --root <workspace-root>/sql-projects/example `
  --sql-file <skill-root>/assets/examples/daily-active-users.sql `
  --title "Daily active users" `
  --summary "Counts distinct login users by activity date." `
  --kind temporary `
  --slug daily-active-users `
  --tag activity

python <skill-root>/scripts/sql_workspace.py receipt `
  --root <workspace-root>/sql-projects/example `
  --sql-file <absolute-saved-vNNN.sql>
```

## Expected Files

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
        temporary/
          daily-active-users/
            v001.sql
            v001.meta.json
```

`v001.sql` is the immutable runnable text. `v001.meta.json` records its title, summary, source
file name, source hash, and saved content hash. `index.json` makes the version searchable. A
later executable revision becomes `v002.sql`; it does not overwrite `v001.sql`.

## Expected Final Response

State that the SQL was saved, say whether it was executed, and link the exact absolute path
returned by the ready receipt. The response must not substitute a pasted SQL block for the file.
