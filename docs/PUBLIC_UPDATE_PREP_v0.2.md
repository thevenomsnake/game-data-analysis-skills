# Public Update Preparation: v0.2

Preparation record for the first major public update. This document records what was found in the
maintained source workspace and what can safely become a public capability release. It is a boundary
record, not a release claim. No source project data or internal workflow implementation is copied by
this document.

## Source Baseline

- Maintained source tip reviewed: `af3186ec` (`SQL Engineering` `4.231.0`, spec `4.11`)
- Maintained source working-tree delta reviewed on 2026-08-20: an uncommitted SQL-error recovery
  contract adds terminal `sql_error`, local-repair routing, new fingerprint/page rules, and a
  regression scenario. It is not yet a source commit and is not treated as a public release input.
- Public baseline: `main` at `c79b707`, first public release `v0.1.0`
- Source analysis and router smoke: `test_analysis_workflow` `11/11`; `test_workflow_router` `7/7`
- Source working-tree smoke after the SQL-error delta: `test_analysis_workflow` `12/12` and
  `test_workflow_router` `7/7`
- Source-only changes reviewed: analysis discovery, query design, work items, execution attempts,
  analysis audit lineage, workflow routing, and related asset-provider projections
- Current source-to-public audit intentionally blocks with these unreviewed source-only paths:
  `analysis-discovery.md`, `workflows.json`, `analysis_frontier.py`, `analysis_workflow.py`,
  `workflow_registry.py`, `workflow_router.py`, and the analysis/work-item schemas. This is the
  expected pre-implementation state, not a permission to copy them wholesale.

## Candidate Public Capability

The first major update should add a resumable analysis workflow while preserving the current public
interfaces:

```text
natural-language intent
  -> bounded discovery frontier
  -> confirmed Analysis Brief
  -> Query Design Brief
  -> resumable Query Work Item
  -> immutable Workspace SQL + receipt
  -> selected direct/web/manual execution surface
  -> exact result lineage
  -> optional formal asset package
```

The user-facing value is not a new product layer. It is stronger file contracts for any Codex or
external AI agent that needs to continue an analysis without replaying an entire conversation.

### Publicizable source slices

| Source capability | Public treatment |
|---|---|
| `analysis_frontier.py` | Copy as a pure, project-neutral frontier helper. |
| Analysis discovery and Query Design state | Port the resumable state machine and digest rules; keep state under ignored `query_workspace`. |
| Query Work Items and fresh-context handoff | Expose batch relations, blockers, checkpoints, inherited facts, and a bounded context JSON. |
| Analysis schemas | Add versioned schemas for discovery, index, batch, work item, context, design brief, execution attempt, and audit link. |
| SQL-error recovery | Port the provider-neutral terminal `sql_error` state, local-repair checkpoint, and new-fingerprint/new-attempt rule; keep site-specific page details behind the adapter. |
| SQL Workspace integration | Carry an optional analysis contract into SQL metadata and receipts; preserve explicit legacy compatibility for pre-v0.2 SQL. |
| Formal asset/provider integration | Add analysis audit members and relationships only when a formal package explicitly contains them. |
| External Agent interface | Document read-only route selection and context handoff through JSON CLI; do not require Codex runtime. |

## Exclusions

The following source material stays out of the public tree or must be rewritten before any reuse:

- Internal workflow registry/route names and internal collaboration transport as-is. A public router
  must use neutral public contracts and configurable providers.
- Real project IDs, internal GitLab URLs, DA page URLs, production result/catalog data, and source
  workspace files.
- Browser page-claim details that assume one internal site. They must consume the existing local
  `web_query_adapter_v1` contract instead of hard-coding a site.
- Internal response examples, credentials, LDAP identities, cookies, and operational permissions.

## Compatibility Rules

- Existing SQL without an analysis record remains usable and is labeled `legacy_unrecorded`.
- No existing Query Workspace path or Formal Asset Package is rewritten merely by installing v0.2.
- New generated natural-language queries require a confirmed Analysis Brief and Query Design Brief;
  explicit external SQL intake remains available.
- A changed business purpose, Base, metric, grain, deduplication, source, or relation creates a new
  immutable revision; old records remain traceable.
- Direct, web, and manual execution surfaces remain separate and never silently fall back.

## Required Public Files

Expected public additions/updates for the implementation candidate:

- `sql-engineering/references/analysis-discovery.md`
- `sql-engineering/scripts/analysis_frontier.py`
- `sql-engineering/scripts/analysis_workflow.py`
- `sql-engineering/schemas/analysis_*.json`
- `sql-engineering/schemas/query_work_item*.json`
- `sql-engineering/schemas/query_design_brief.json`
- `sql-engineering/schemas/query_execution_attempt.json`
- `sql-engineering/scripts/requirement_intake.py`
- `sql-engineering/scripts/sql_query_workspace.py`
- `sql-engineering/scripts/sql_formalize.py`
- asset catalog/provider/organization updates for optional analysis audit members
- focused public tests with fictional project names and no site-specific URLs

## Acceptance Gate

Before v0.2 can be tagged:

1. `tools/public_sync.py audit` passes against the maintained source with every source-only path
   reviewed.
2. New analysis state can start, append a frontier turn, confirm a brief, create a Work Item, emit
   fresh context, create/confirm a design brief, and block SQL generation until the digests match.
3. A legacy SQL path still receipts and executes exactly as in v0.1.
4. Direct/web/manual execution tests remain green; no browser adapter is invoked implicitly.
5. Analysis audit files and hashes remain project-relative and excluded from ordinary Workspace
   sharing unless explicitly formalized.
6. Public boundary, capability registry, README maintenance, Setup tests, and the full public SQL
   suite pass on Python 3.11 and 3.13.
7. Six README locales and the external-agent integration document describe the same facts.

The next implementation commit should be a candidate only. Release notes, a tag, and a GitHub Release
are created after the candidate passes this gate; no release is implied by this preparation record.
