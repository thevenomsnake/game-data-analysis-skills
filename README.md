# Game Data Analysis Skills

Game Data Analysis Skills is a public Codex workspace for governed, file-backed SQL work. It
keeps query versions, rules, evidence, and delivery receipts understandable after the chat ends.

This repository contains no production results, private schemas, credentials, or organization
service configuration. Examples are fictional and run locally.

## Quick start

Requirements: Python 3.11+ and Git. No third-party Python dependency is needed for the first run.

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Refresh Codex, then use `$sql-engineering`. To exercise the storage contract without a database:

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "Counts distinct fictional users by date." `
  --kind temporary `
  --slug daily-active-users
```

The command writes an immutable `v001.sql`, metadata, and an index. Run `receipt` against the
returned path before sharing it. Automatic execution is optional; without a configured read-only
adapter, the skill returns a precise manual handoff.

## What is included

- Immutable SQL workspace storage and the original `sql_workspace.py` compatibility interface.
- Governed project, rule, knowledge, planning-source, review, validation, formal-asset, and
  result-lineage modules from the maintained Skill.
- Read-only local execution adapters and deterministic health/receipt checks.
- The standalone Excel report visualizer source, without any workbook or report data.
- Fictional examples, schemas, templates, tests, and public maintenance tooling.

## Setup flow

Use `$setup` or the script directly:

```powershell
python .\setup\scripts\bootstrap_repo.py status --root .
python .\setup\scripts\bootstrap_repo.py demo --root .
```

Setup is local-only. It never requires LDAP, GitLab, a DA console, or a production database.

## Development

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m unittest discover -s .\setup\scripts -p "test_*.py"
python .\tools\public_release.py validate --root .
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) for the public contracts.

Licensed under the Apache License 2.0.
