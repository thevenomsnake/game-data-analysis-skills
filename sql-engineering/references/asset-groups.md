# Stable Asset Groups

The registry groups only shared catalog assets. Refresh removes legacy `:temporary_query:` members and roots, drops groups that contained only local workspace assets, and preserves an existing `AG-NNNN` when formal/shared members survive. Removed IDs are never reused.

Use this reference during explicit cross-project asset organization. Asset grouping is periodic maintenance and never runs inside QUERY, formalization, Review, Dashboard, or ordinary saves.

## Identity Model

`asset_group_registry.json` gives one analytical question or explicitly linked analysis bundle an immutable chronological ID:

```text
AG-0001  玩家平台属性新增留存
  QUERY versions: v001, v002
  returned results
  visualization workbook
  validation
  Dashboard derivative
```

The namespaces are intentionally different:

- `AG-0001` identifies the analytical asset group for its entire lifetime.
- `v001`, `v002` identify immutable versions inside one SQL family.
- `asset_id` continues to identify one catalog asset.

Assign a group ID once. Never renumber existing groups after a late historical import, deletion, status change, or display reorder. Use `display_order` for homepage ordering without changing identity.

## Group Boundary

Group assets only when persisted lineage says they answer one analytical question:

- versions of the same temporary or formal QUERY family;
- a temporary QUERY and its promoted formal QUERY;
- exact returned results and reusable workbooks derived from those results;
- validation and Dashboard derivatives of that QUERY;
- every exact member/result/output of an explicitly linked grouped/overall analysis bundle.

Do not group by title similarity, shared logs, or a broad topic such as 新增 or 留存. A materially different Base, grain, business question, or independently useful metric contract starts another group. A correction, date refresh, or same-contract replacement remains in the existing group as a later SQL version.

## Persistent Registry

The registry lives beside the catalog and organization overlay:

```text
sql-projects/_asset_catalog/
  asset_catalog.json
  asset_organization.json
  asset_group_registry.json
```

`asset_catalog.json` owns individual factual assets. `asset_organization.json` owns semantic categories. `asset_group_registry.json` owns stable group identity, membership, chronological sequence, and homepage directory metadata.

The builder uses only strong catalog relationships and same-family versions. If a new relationship connects two existing groups, it preserves both IDs and reports a review issue instead of silently merging them.

## Maintenance

Run after catalog and organization refresh:

```powershell
python .\sql-engineering\scripts\asset_group_registry.py scan `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --organization .\sql-projects\_asset_catalog\asset_organization.json `
  --format json

python .\sql-engineering\scripts\asset_group_registry.py refresh `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --organization .\sql-projects\_asset_catalog\asset_organization.json `
  --function-selection ASSET_ORGANIZATION `
  --user-request "定期整理全部资产并补充分组编号" `
  --format json
```

Run `validate` before publishing the directory. Consumers render one homepage directory row per `groups[]` item, sorted by `display_order`, and expand `member_asset_ids` by joining to `asset_catalog.assets[].asset_id`. Keep unassigned and cross-group issues visible for curation.
