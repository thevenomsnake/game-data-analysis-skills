# Local Setup

The public setup flow is local-only and does not require a company account or database password.

```powershell
python .\setup\scripts\bootstrap_repo.py demo --root .
python .\sql-engineering\scripts\local_setup.py --repo-root . --project example status
python .\sql-engineering\scripts\local_setup.py --repo-root . --project example init --dialect starrocks
```

`status` reports whether the project exists and whether a database adapter is configured.
`init` creates the advanced project layout when it is missing. Configure a DB-API or CLI profile
only in the ignored `.sql-engineering/connections.local.json` file and keep its secret in a local
environment variable. The first run can remain `manual_required`.

Local source folders, planning snapshots, rules, SQL, and results remain user-owned project data;
the public repository ships no such files.
