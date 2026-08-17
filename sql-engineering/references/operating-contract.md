# Operating Contract

This reference contains the public safety rules. Lifecycle procedures live in
`project-workflow.md`; route definitions live in `capabilities.json`.

## Before writing

1. Select a capability from the registry.
2. Identify the project and read its `project_config.json` when SQL depends on project facts.
3. Read the configured dialect, table naming, time policy, and active rule/knowledge bindings.
4. Preserve the verbatim user request in asset-changing receipts.
5. Use the route's own receipt and validation; do not claim success from a console message alone.

## Hard blockers

Return `BLOCKED` when project context, dialect, time bounds, rule evidence, file lineage, or
required output contracts are missing. Also block:

- SQL that mutates data or performs privacy masking/de-identification;
- a saved version whose metadata, index, or content hash disagrees;
- a formal artifact without its sidecar spec and exact source lineage;
- a result or visualization that cannot identify the exact SQL version that produced it;
- a credential, private endpoint, production result, or machine absolute path in a public tree.

## Rules and knowledge

Planning files are evidence, not truth. Confirmed knowledge is registered separately and canonical
rules require an explicit RULES request. Ordinary QUERY, REVIEW, VALIDATION, and DASHBOARD work is
read-only with respect to rules and knowledge. Never infer a business definition from a table name,
title, or general model knowledge.

## Storage and execution

- Keep every executable SQL version in the project workspace and never overwrite an earlier version.
- Add one managed `@SQL_GENERATION` header; the public edition uses the local Git user or the
  `DA_SKILLS_USER` environment variable, falling back to `local-user`.
- Execute only a single read-only statement through a configured DB-API or CLI adapter.
- If no adapter or credential is available, return `manual_required` with the exact SQL path.
- Keep result files, visualizations, validation, and formal packages as separate objects linked by
  content hash and version.

## Public execution boundary

The public package has no browser or organization-console adapter. A user may add a local adapter
for their own environment, but it must be read-only and must not place secrets in tracked files.
