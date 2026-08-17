# Skill Operations

The public Skill source is `sql-engineering/`. Keep its frontmatter, references, schemas,
templates, and scripts coherent in one change.

## Local checks

```powershell
python -m compileall -q .\sql-engineering\scripts .\sql-engineering\tests
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python .\tools\public_release.py validate --root .
```

Use the smallest check that covers the change. The setup smoke should always be runnable from an
empty temporary folder and must never need a production connection.

## Release boundary

Only files in this public repository are released. Do not copy `BetterXml`, project data,
production results, credentials, private endpoints, or local workspaces. Generate a hash manifest
with `tools/public_release.py manifest` before publishing a tag.

Use `sql-engineering/scripts/collaboration_submit.py plan` to review a local change set. The
public command never pushes or calls a remote service.

## Runtime installation

The installed Codex copy must match the tracked `sql-engineering/` tree. Use the documented
`Copy-Item` command or `tools/deploy-skill.ps1`; do not edit the installed copy as a substitute for
source changes.
