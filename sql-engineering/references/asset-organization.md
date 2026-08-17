# Cross-Project Asset Organization

This overlay covers only assets present in the shared asset catalog. Refresh permanently removes legacy `:temporary_query:` entries instead of preserving them as `catalog_missing`; local workspace curation remains in each project's `query_workspace/organization.json`.

Use this reference for periodic semantic organization across all SQL Engineering assets. Read `asset-catalog.md` first when the catalog itself is missing or stale.

## Responsibility Split

- `asset_catalog.json` is the complete factual inventory: identity, kind, status, paths, hashes, relationships, and persisted facts.
- `asset_organization.json` is a semantic overlay: business domain, topic, analysis type, summary, tags, confidence, and curation state.
- `asset_group_registry.json` gives each analytical question a stable `AG-0001` identity and homepage directory row. It consumes catalog relationships plus the organization overlay; it never replaces either source.
- Source SQL, retained result previews, reusable visualizations, rules, lifecycle states, and verification states are immutable inputs to semantic organization. Never rewrite them during curation. Oversized result slicing belongs to the separate deterministic `result_evidence_maintenance.py` capability.
- Workspace `organization.json` remains the detailed single-project query-family overlay. The cross-project overlay is the common navigation layer for external consumers.

The overlay covers every catalog asset. Draft, failed, discarded, history, proposed, superseded, and verified assets remain visible; status is never a publication filter.

## Periodic Workflow

Run this after a batch import/migration, before an external mirror refresh, or during a scheduled catalog tidy-up. Do not attach it to every QUERY save.

This workflow is deliberately independent from QUERY, SQL_FORMALIZE, REVIEW, VALIDATION, DASHBOARD, SQL_REPOSITORY, and ordinary save paths. Those paths may update their own authoritative indexes, but they never refresh this cross-project overlay or scan platform manuals. Catalog and organization staleness is handled by the next explicit maintenance run.

1. Rebuild `sql-projects/_asset_catalog/asset_catalog.json`.
2. Run `scan` to identify only new, changed, stale, or unclassified assets.
3. Run `refresh` to apply deterministic categories and relationship inheritance.
4. Ask an LLM or human to review only the remaining candidates and write a decisions file.
5. Run `apply`, then `validate`.
6. Run `asset_group_registry.py scan`, then `refresh` and `validate` to preserve existing group IDs and assign IDs only to new analytical groups.

```powershell
python .\sql-engineering\scripts\asset_catalog.py build `
  --projects-root .\sql-projects --format json

python .\sql-engineering\scripts\asset_organization.py scan `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json --format json

python .\sql-engineering\scripts\asset_organization.py refresh `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --function-selection ASSET_ORGANIZATION `
  --user-request "定期整理全部 SQL 资产" --format json

python .\sql-engineering\scripts\asset_group_registry.py refresh `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --organization .\sql-projects\_asset_catalog\asset_organization.json `
  --function-selection ASSET_ORGANIZATION `
  --user-request "定期整理全部 SQL 资产" --format json
```

`refresh` preserves current human/LLM classifications. If such an asset's semantic fingerprint changes, it keeps the old classification but marks it `stale_semantics`; it does not silently recategorize it.

## Deterministic And Semantic Work

Deterministic code handles:

- existing `business_category` and `business_topic` values;
- governance asset kinds such as rules, knowledge datasets, source catalogs, and review read models;
- platform documentation and consumer contracts, mapped to `资产治理 / 手册与平台文档` or `资产治理 / 消费与集成契约`;
- result, workbook, visualization, validation, and dashboard inheritance from their source query;
- catalog/asset fingerprints, coverage, stale detection, and schema validation.

LLM or human review handles:

- assets with no reliable saved category;
- ambiguous product meaning;
- changed assets whose old reviewed classification may no longer fit;
- concise semantic summaries and useful tags.

Do not ask an LLM to reclassify unchanged assets. Lifecycle-only changes are excluded from the semantic fingerprint.

Asset grouping is stricter than semantic classification. It uses same-family versions and strong persisted lineage only; titles, shared logs, and broad topics never create group identity. Read `asset-groups.md` for group boundaries, immutable numbering, late imports, and homepage consumption.

## Decision Contract

```json
{
  "schema_version": "sql_asset_organization_decisions_v1",
  "decisions": [
    {
      "asset_id": "DEMO_ANALYTICS:temporary_query:qw-example:v001",
      "business_domain_id": "user_lifecycle",
      "business_topic_id": "new_user",
      "analysis_type_id": "aggregate",
      "semantic_summary": "按新增日期统计新增玩家数",
      "tags": ["新增", "PlayerLogin"],
      "classification_source": "llm",
      "confidence": 0.94,
      "notes": "基于 SQL、结果字段和来源日志判断"
    }
  ]
}
```

Apply reviewed decisions:

```powershell
python .\sql-engineering\scripts\asset_organization.py apply `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --decisions-file .\work\asset-organization-decisions.json `
  --function-selection ASSET_ORGANIZATION `
  --user-request "应用本次资产语义整理结果" --format json
```

The taxonomy snapshot is embedded in `asset_organization.json` so a read-only external tool can render stable navigation without loading skill source. To change the taxonomy itself, use `SKILL_EVOLUTION`; do not improvise new domain IDs in a decisions file.

## Consumer Contract

Join `asset_organization.entries[asset_id]` to `asset_catalog.assets[].asset_id`.

- Build navigation from `navigation_path`.
- Search factual fields and source logs from the catalog.
- Show lifecycle and verification states from the catalog as labels only.
- Show `needs_semantic_review` or `stale_semantics` as curation warnings.
- Keep an unclassified asset visible under `其他 / 待整理`.
- Never infer that `current` organization means the underlying SQL is verified or formal.

For the homepage directory, iterate `asset_group_registry.groups[]` by `display_order`, show `group_id` and `display_title`, then expand `member_asset_ids` against the catalog. Keep `AG-` identity separate from SQL `vNNN` versions.
