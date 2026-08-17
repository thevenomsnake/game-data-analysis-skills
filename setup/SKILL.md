---
name: setup
description: Initialize the public Game Data Analysis Skills workspace locally.
metadata:
  short-description: Local setup for the public SQL Engineering Skill
  version: "1.0.0"
---

# Setup

Use this skill when a user downloads the public repository and wants a working local
workspace. It never asks for a company credential, never contacts a private service, and never
copies project data.

## First run

1. Choose an empty folder for the repository.
2. Run `setup/scripts/bootstrap_repo.py sync --root <folder>` or clone the GitHub repository.
3. Install `setup/` and `sql-engineering/` into the local Codex skills directory.
4. Run `setup/scripts/bootstrap_repo.py demo --root <folder>` to create a fictional project.
5. Use the generated project with `$sql-engineering`.

The setup command is idempotent. It refuses to overwrite a non-empty non-repository folder and
does not modify a dirty checkout. Database connections are optional; manual SQL handoff is enough
for the first run.

## Local commands

```powershell
python .\setup\scripts\bootstrap_repo.py status --root .
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
```

The `demo` command creates only configuration, empty catalogs, and a fictional query example.
It does not create results or connect to a database.

## Safety

The public workspace must not contain production SQL, result files, credentials, private table
definitions, or machine-specific absolute paths. Keep local connection profiles under the ignored
`.sql-engineering/` directory and provide secrets through the local environment only.
