# Contributing

Keep the public edition generic, reproducible, and safe to publish.

- Do not add production results, private schemas, credentials, internal endpoints, or local absolute paths.
- Keep examples fictional and place local run output under ignored directories.
- Update the relevant Skill reference and README when a public command or contract changes.
- Run the focused tests and `python tools/public_release.py validate --root .` before committing.
- Use the standard library unless a dependency is required by an optional local adapter.

Pull requests should explain the changed public behavior, the files intentionally excluded, and
the exact validation command used.
