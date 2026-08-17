# Health Checks

Run the focused check for the change:

```powershell
python .\tools\public_release.py validate --root .
python -m compileall -q .\sql-engineering\scripts .\setup\scripts
python .\setup\scripts\bootstrap_repo.py demo --root <temporary-workspace>
```

`project_validate.py` can audit a configured project with `--scope current` or `--scope full`.
It is read-only. A warning or missing optional database adapter is not proof that SQL executed.

Before a release, verify that no production result, credential, private endpoint, or absolute
machine path appears in the tree. `public_release.py` performs this scan and emits a deterministic
SHA-256 manifest.
