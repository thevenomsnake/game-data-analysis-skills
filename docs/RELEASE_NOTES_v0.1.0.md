# Game Data Analysis Skills v0.1

This is the first public release of Game Data Analysis Skills: a file-backed collection of
pluggable capabilities for game-data analysis in Codex and in external AI-agent/tooling stacks.

## What is included

- `setup` for Git-based repository setup with GitHub, GitLab, self-hosted Git, SSH, local Git, and
  independent Git/SVN/local/none planning sources.
- `sql-engineering` for immutable SQL versions, receipts, source and rule lineage, validation,
  review, formal asset packages, result evidence, and Provider Snapshot/Catalog interfaces.
- Three explicit execution surfaces: direct read-only DB-API/CLI, a project-local web query adapter,
  or manual handoff when no automatic surface is configured.
- `web_query_adapter_v1`, including a Deltaverse example and a guide for adapting another website
  without storing credentials, cookies, or tokens.
- `excel-report-visualizer` for local workbook inspection and offline report presentation. No real
  workbook or production result is included.
- Two documented integration interfaces: the Codex Skill interface and the external
  AI-agent/third-party command-and-file interface.
- Six complete README locales: English, Simplified Chinese, Traditional Chinese, Japanese, Korean,
  and Spanish.
- GitHub Actions validation for the public boundary, Python 3.11/3.13, capability registry, Setup,
  SQL tests, README navigation, and maintenance tools.

## Getting started

Requirements: Python 3.11+ and Git. Clone the repository, run `setup/scripts/bootstrap_repo.py
demo`, install `setup/` and `sql-engineering/` into the local Codex skills directory when using
Codex, then read [`docs/INTEGRATION_INTERFACES.md`](INTEGRATION_INTERFACES.md) for the external-agent
path.

## Public boundary

This release contains generic capabilities and fictional examples only. It excludes personal tools,
production SQL and results, private schemas, credentials, internal endpoints, and local workspaces.
The Apache License 2.0 applies to this repository; bundled third-party notices are recorded under
`excel-report-visualizer/THIRD_PARTY_NOTICES.md`.

## Verification

- Setup tests: `7/7`
- Public SQL tests: `106/106`
- README navigation and maintenance tool tests: passed
- Six-locale Humanization `copy` checks: passed
- `tools/public_release.py validate`: passed
- `tools/public_sync.py audit`: passed against the maintained source tree
