# Local Setup

The public setup flow is local-only and does not require a company account or database password.

```powershell
python .\setup\scripts\bootstrap_repo.py demo --root .
python .\sql-engineering\scripts\local_setup.py --repo-root . --project example status
python .\sql-engineering\scripts\local_setup.py --repo-root . --project example init --dialect starrocks
# Or initialize the proven web surface in one step:
python .\sql-engineering\scripts\local_setup.py --repo-root . --project example init --execution-surface web
```

`status` reports whether the project exists and whether a database adapter is configured.
`init` creates the advanced project layout when it is missing. Choose direct execution by configuring
a DB-API or CLI profile in the ignored `.sql-engineering/connections.local.json`, or choose web
execution by copying and validating `web-query-adapter.deltaverse.json` as the ignored
`.sql-engineering/web-query-adapter.local.json`. Keep direct secrets in local environment variables;
the web route uses the user's own Chrome session. The first run can remain `manual_required`.

Local source folders, planning snapshots, rules, SQL, and results remain user-owned project data;
the public repository ships no such files.
