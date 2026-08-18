# Contributing

Keep the public edition generic, reproducible, and safe to publish.

Install the public test dependency before running the complete suite:

```powershell
python -m pip install -r requirements-dev.txt
```

- Do not add production results, private schemas, credentials, internal endpoints, or local absolute paths.
- Keep examples fictional and place local run output under ignored directories.
- Update the relevant Skill reference and README when a public command or contract changes.
- Follow `docs/README_MAINTENANCE.md` and update the append-only README log in the same commit.
- Run the focused tests and `python tools/public_release.py validate --root .` before committing.
- When working from the maintained source, run `python tools/public_sync.py audit --source-root <maintained-source-root> --public-root .` and review every `changed_review` entry.
- Use the standard library unless a dependency is required by an optional local adapter.

Pull requests should explain the changed public behavior, the files intentionally excluded, and
the exact validation command used.
