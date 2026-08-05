# SQL Projects

This directory is the default workspace created and maintained by the public SQL Engineering
Skill.

- `_asset_catalog`: cross-project discovery outputs produced by optional extensions.
- `_review_inbox`: external SQL and evidence waiting for review or intake.
- `_rule_review`: rule dictionary and rule-review outputs produced by optional extensions.
- `<project-id>`: a normal project initialized by `sql_workspace.py bootstrap`.

The public core keeps the reserved directories stable but does not invent catalog, review, or
rule content. Run the bootstrap command from the repository README to create the first project.

Each project may declare named database environments in `.sql-engineering/project.json`. Actual
connection details belong in the ignored `.sql-engineering/connections.local.json` file or a local
path selected by `SQL_ENGINEERING_CONNECTIONS_FILE`; they do not belong in this directory's Git history.
