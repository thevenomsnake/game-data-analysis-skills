# Game Data Analysis Skills

**A file-backed toolkit for game-data analysis in Codex.**

[Official site](https://fairy.sumimi.jp/) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md)

A useful query should still be useful after the chat ends. Game Data Analysis Skills keeps the
question, the SQL version, the rule sources, the result evidence, and the delivery decision close
enough to follow without turning every small analysis into a heavy release process.

## What it helps you do

| Module | The job it handles |
| --- | --- |
| **Setup** | Start a local workspace, keep Git as the baseline, and choose GitHub, GitLab, self-hosted Git, SSH, local Git, SVN, or a local planning folder at install time. |
| **SQL workspace** | Save every query as an immutable, searchable version with metadata, content hashes, and an exact delivery receipt. |
| **Rules and knowledge** | Keep raw event definitions, planning inputs, confirmed references, and canonical business rules separate and traceable. |
| **Query lifecycle** | Move from requirement intake to query, validation, formal asset packages, and dashboard-ready derivatives without silently skipping evidence. |
| **Review and health** | Inspect SQL from product and code perspectives, run deterministic facts and quality checks, and catch drift before delivery. |
| **Results and lineage** | Bind results, visualizations, and workbooks to the exact SQL version that produced them. |
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

## Roadmap

- Generate recurring reports on a schedule.
- Compare results across data assets to assess whether they are reasonable.
- Trace anomalies back to their source and investigate likely causes.

## Official site

The website brings the modules together with examples and product-facing guidance:

**[Visit fairy.sumimi.jp](https://fairy.sumimi.jp/)**

Licensed under the Apache License 2.0.
