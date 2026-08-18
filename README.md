# Game Data Analysis Skills

[![Public validation](https://github.com/thevenomsnake/game-data-analysis-skills/actions/workflows/public-validation.yml/badge.svg)](https://github.com/thevenomsnake/game-data-analysis-skills/actions/workflows/public-validation.yml)

**A file-backed collection of pluggable Skills for game-data analysis in Codex.**

English · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md)

A useful query should still be useful after the chat ends. Game Data Analysis Skills keeps the
question, the SQL version, the rule sources, the result evidence, and the delivery decision close
enough to follow without turning every small analysis into a heavy release process. Use one Skill,
combine several, or let another tool compose the selected capabilities into its own workflow.

## What it helps you do

| Module | The job it handles |
| --- | --- |
| **Setup** | Start a local workspace, keep Git as the baseline, and choose GitHub, GitLab, self-hosted Git, SSH, local Git, SVN, or a local planning folder at install time. |
| **SQL workspace** | Save every query as an immutable, searchable version with metadata, content hashes, and an exact delivery receipt. |
| **Rules and knowledge** | Keep raw event definitions, planning inputs, confirmed references, and canonical business rules separate and traceable. |
| **Query lifecycle** | Move from requirement intake to query, validation, formal asset packages, and dashboard-ready derivatives without silently skipping evidence. |
| **Review and health** | Inspect SQL from product and code perspectives, run deterministic facts and quality checks, and catch drift before delivery. |
| **Results and lineage** | Bind results, visualizations, and workbooks to the exact SQL version that produced them. |
| **Execution surfaces** | Run a receipted SQL through a direct DB-API/CLI connection, a configured web query adapter, or an explicit manual handoff. |
| **Excel report visualizer** | Inspect a local workbook and turn a supported report shape into an offline, reusable presentation. The repository ships the tool, not anyone's workbook. |

## Install and try it

Requirements: Python 3.11+ and Git. The first run needs no third-party Python package.

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills

```

Configure the repository and planning source, then create the demo project:

```powershell
python .\setup\scripts\bootstrap_repo.py configure `
  --root . `
  --remote https://github.com/thevenomsnake/game-data-analysis-skills.git `
  --planning-provider none
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Refresh Codex, then use `$sql-engineering`. To prove the file-backed workflow without a database:

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "Counts distinct fictional users by date." `
  --kind temporary `
  --slug daily-active-users
```

The command returns an immutable `v001.sql` path. Run `receipt` on that exact path before sharing
it. With no database adapter configured, execution correctly ends as `manual_required` instead of
pretending that the query ran.

## Choose an execution surface

Initialize a formal project with an explicit execution intent:

```powershell
python .\sql-engineering\scripts\local_setup.py init `
  --repo-root . `
  --project example `
  --execution-surface direct
```

Use `direct` for a read-only DB-API or CLI profile, `web` for a project-local web adapter and the
user's Chrome session, or `manual` when the project is not ready to execute. The web adapter is
currently demonstrated for Deltaverse; other sites use the same versioned contract and validation
guide. The three routes never silently fall back to one another.

The collection exposes asset interfaces as files: Query Workspace indexes temporary SQL, Formal
Asset Package manifests hold shared SQL and derived assets, delivery receipts bind exact versions,
and Provider Snapshot/Catalog schemas give read-only consumers stable identities and hashes. See
the [execution surface and adapter guide](sql-engineering/references/execution-surfaces.md) and
[read-only asset consumer guide](docs/READONLY_ASSET_CONSUMER_GUIDE.md).

## Two integration interfaces

- **Codex Skill interface:** install `setup` and `sql-engineering`, refresh Codex, and use
  `$sql-engineering` for route selection and guided project work.
- **External Agent / third-party interface:** call the JSON CLI commands or read the documented
  file/schema interfaces directly. No Codex runtime is required.

The complete command/file contract is in [Integration interfaces](docs/INTEGRATION_INTERFACES.md).

## Choose your planning source

The repository remote and the planning source are separate choices:

```powershell
# Git-managed planning repository
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider git `
  --planning-url <git-planning-url> `
  --planning-branch main `
  --planning-id planning
python .\setup\scripts\bootstrap_repo.py planning-sync --root .

# SVN-managed source
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider svn `
  --planning-url <svn-url> `
  --planning-revision <revision>

# Existing local folder, never updated by setup
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider local `
  --planning-path <folder>
```

Use `--planning-provider none` when the project is not ready to bind a planning source. Provider,
URL, branch, revision, and local checkout metadata live in ignored `.local/`; credentials stay in
your native Git/SVN credential mechanism.

## From question to delivery

```text
question
  -> discover requirements, sources, and rules
  -> save a versioned workspace query
  -> verify its exact receipt
  -> optionally run one read-only query and attach the result
  -> review and validate
  -> explicitly formalize a reusable asset or dashboard derivative
```

Execution state, result presentation, and long-term asset value remain separate. A result does not
silently promote a query, and a lifecycle label does not claim correctness by itself.

## Safety and privacy

- The public tree contains fictional examples, not production SQL, results, private schemas, or credentials.
- External SQL is treated as input and is never overwritten in place.
- Automatic execution is read-only. Optional browser execution consumes an exact receipt through
  the Chrome plugin and the user's own session; no endpoint or credential is bundled here.
- `tools/public_release.py` validates the public tree and can produce a local SHA-256 manifest.

## Explore the modules

- [Setup onboarding](setup/references/onboarding.md)
- [SQL Engineering contract](sql-engineering/SKILL.md)
- [Project overview](docs/PROJECT_OVERVIEW.md)
- [User manual](docs/USER_MANUAL.md)
- [Planning-source providers](sql-engineering/references/planning-source.md)
- [Direct, web, and manual query execution](sql-engineering/references/execution-surfaces.md)
- [Public maintenance boundary](docs/PUBLIC_MAINTENANCE.md)
- [Offline Excel report visualizer](excel-report-visualizer/README.md)
- [Excel third-party notices](excel-report-visualizer/THIRD_PARTY_NOTICES.md)

## Roadmap

- Generate recurring reports on a schedule.
- Compare results across data assets to assess whether they are reasonable.
- Trace anomalies back to their source and investigate likely causes.

Licensed under the Apache License 2.0.
