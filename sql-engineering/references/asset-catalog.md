# All-Status Asset Catalog

## Purpose

`asset_catalog.py` creates one read-only index over every shared SQL Engineering asset. It does not publish, approve, promote, copy, or hide assets. Lifecycle and verification states are descriptive metadata only.

The catalog lets another tool copy or read structured assets without understanding the skill's internal folders and without writing back to the source workspace.

The standalone handoff and synchronization manual for external AI/Web implementers is `docs/READONLY_ASSET_CONSUMER_GUIDE.md`.

Use `asset_organization.json` for business navigation such as 新增、活跃、留存. Use `asset_group_registry.json` when the consumer needs one stable homepage row for a complete analytical question and its SQL versions, results, visualizations, validation, and Dashboard derivatives.

## Included Assets

- Every Formal Asset Package and every QUERY, evidence, validation, output, and Dashboard member, including current, history, archived, and migration-quarantine roles.
- Every registered intermediate-table contract and build SQL, including availability and fallback state.
- Query-derived results, analysis/comparison workbooks, visualizations, and exports attached through `derived_outputs`.
- Result lifecycle (`active`, `needs_review`, `superseded`, `discarded`), exact single/multi-result lineage, deterministic transformation evidence, and replacement relationships.
- Manifest run evidence and retained result files.
- Canonical Rule Store concepts and every immutable rule definition version, including confirmed, proposed, superseded, and deprecated states.
- Every registered immutable knowledge dataset version and every project binding.
- Project source/XML catalogs, SQL Review outputs, repository read models, Dashboard Review read models, and cross-project rule read models.
- The root platform overview, every maintained document under `docs/`, and the external read-only consumer manual.
- Consumer-facing schemas, taxonomy, and catalog/organization/group reference contracts needed to copy and interpret the snapshot.

Project-local `query_workspace/` trees are deliberately excluded in full: temporary SQL, metadata, formalize seeds, returned results, visualizations, indexes, viewers, and build caches stay on the machine that produced them. Credentials, caches, lock files, and build-temporary files are also excluded.

SQL result payloads are deliberately compact: `result_evidence` over 10 MB is represented by its managed slice and retention metadata, not by the complete raw dataset. Reusable analysis workbooks, comparisons, and visualizations remain complete catalog files. This is a payload-retention distinction, not a lifecycle or visibility filter.

Reusable workbook membership is explicit: only `analysis_workbook`, `comparison_workbook`, and `visualization` with XLSX media enter that surface. A result-evidence XLSX remains `result_evidence`, and an HTML visualization remains `other`. New reusable workbooks persist bounded `workbook_manifest_v1`; historical workbooks without one remain downloadable with `preview_status=not_available`. Catalog build consumes persisted manifests and never opens historical Excel files.

## Contract

The generated `sql_asset_catalog_v2` contains:

- `assets`: common identity, kind, project, title, summary, lifecycle, verification, version, provenance, facts, and file paths;
- `files`: repository-relative paths, existence, SHA-256, size, media type, roles, and owning assets;
- `relationships`: lineage and dependency edges such as `derived_from`, `has_derived_output`, `derived_from_result`, `has_visualization`, `references_rule`, `uses_knowledge`, `validated_by`, and `evidence_for`;
- `issues`: missing files, unsafe paths, duplicate identities, and other catalog diagnostics. Issues never remove the affected asset from visibility;
- `visibility_policy`: declares `all_managed_assets`, `status_is_descriptive_only=true`, no hidden shared lifecycle states, `local_workspace_included=false`, and `excluded_local_surfaces=["query_workspace"]`.

Every exact SQL asset exposes `facts.execution_delivery` using `execution_delivery_v1`. It includes the persisted engine, dialect, selected profile, routing role/status, bounded route evidence, and portable-template reference. Historical SQL without a route is `legacy_unlabeled`. Stable logical/variant IDs, exact variant members, and recommendation are populated only from valid `execution_variant_identity_v1`; identity conflicts become catalog issues and never trigger inferred grouping.

Every derived output exposes `facts.consumer_surface` and `facts.workbook_presentation`. For eligible XLSX files the presentation preserves the download path, workbook kind, preview state, and bounded manifest. File-level media type, size, SHA-256, and exact lineage remain in the normal catalog `files` and `relationships` arrays.

Paths are repository-relative. Absolute external input paths, local credentials, and `query_workspace/` paths are rejected rather than persisted.

## Build And Validate

```powershell
python .\sql-engineering\scripts\asset_catalog.py build `
  --projects-root .\sql-projects `
  --format json
```

Default output:

```text
sql-projects/_asset_catalog/asset_catalog.json
```

Build the snapshot periodically or immediately before an external synchronization. Do not attach a cross-project rebuild to every QUERY save; source indexes remain authoritative between catalog refreshes.

CLI output is a bounded status summary. The complete catalog is written only to the output file; consumers must not expect asset rows or workbook manifests on stdout. Organization scan is pageable, and registry refresh returns bounded issue/change samples while retaining the complete registry on disk.

This is an independent maintenance capability. QUERY generation, SQL formalization, Review, Validation, Dashboard delivery, repository viewer builds, and ordinary project saves must not invoke catalog build or documentation scanning. A stale snapshot is acceptable between explicit maintenance runs; slowing down daily SQL work is not.

Validate that every declared existing file still exists and matches its hash:

```powershell
python .\sql-engineering\scripts\asset_catalog.py validate `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --repo-root . `
  --format json
```

## Consumer Rules

A downstream read-only tool may display or copy every catalog asset. It decides its own filters but must not reinterpret lifecycle state as source authorization or write changes back. It must not scan project folders to recover excluded local workspaces.

Use `asset_catalog.json` for individual identity, files, and relationships. Join `asset_group_registry.groups[].member_asset_ids` for the stable analytical-group directory; never derive group numbers by sorting catalog rows during page build. Existing generated JSON such as `reviews/sql_repository.json` and `_rule_review/rule_dictionary.json` remains an optional presentation-friendly read model; it is not the cross-asset source of truth.

When copying an asset, copy the paths listed in its `file_paths`. A result asset path already points to the retained preview or slice; its `facts.retention` describes the original payload. Visualization and analysis paths point to complete reusable files. Follow relationships only when the consumer needs linked results, Dashboard derivatives, rules, or knowledge datasets. Verify SHA-256 after copying.

For SQL execution choices, read only `facts.execution_delivery`. Do not join variants by title, tag, source table, path, SQL similarity, or `branch_of`. For the workbook entry point, filter only `facts.consumer_surface=reusable_workbook` and `facts.workbook_presentation.eligible=true`; never filter by `.xlsx` suffix alone.

## Ownership

Each existing subsystem continues to own its facts:

- query workspace owns local temporary/history SQL and derived outputs without publishing them to Git or the shared catalog;
- Formal Asset Repository Package manifests own formal membership, lifecycle, and lineage; member spec/meta own one SQL version's facts;
- Rule Store owns rule versions and current pointers;
- knowledge catalog and project bindings own immutable datasets;
- review and repository builders own their generated read models.

The asset catalog aggregates only shared persisted facts. It never opens the local workspace, scans SQL to invent semantics, or invokes an LLM. Periodic semantic classification belongs to the organization overlay; immutable analytical-group numbering belongs to the separate group registry.

Platform documentation remains owned by `README.md` and `docs/`. The catalog records it as `documentation` assets and records machine-facing schemas/references as `consumer_contract` assets; it does not copy, rewrite, or publish those files.
