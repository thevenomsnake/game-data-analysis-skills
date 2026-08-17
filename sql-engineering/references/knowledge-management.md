# Knowledge Dataset Management

This reference owns config-table and reference-data lifecycle. It does not define metric logic.

## Contents

- [Domain Terms](#domain-terms)
- [Ownership Boundary](#ownership-boundary)
- [Storage Contract](#storage-contract)
- [Lifecycle](#lifecycle)
- [Usage Contract](#usage-contract)
- [SQL Workflow](#sql-workflow)
- [Commands](#commands)
- [Quality Gates](#quality-gates)

## Domain Terms

| Term | Meaning |
|---|---|
| 来源快照 | An immutable copy of one original Excel, code, XML, or other reference file. |
| 策划源版本 | One exact SVN revision manifest by default, or one embedded immutable folder release for a non-SVN source. It is source evidence, not query-time knowledge. |
| 候选资料目录 | A machine-local folder for unconfirmed code, TLOG documents, or external references. Complete planning folders use the dedicated planning-source space. |
| 资料投影 | A stable, deliberately selected subset extracted from a source. One workbook may produce multiple projections. |
| 使用契约 | What a projection explains, how it may be joined or compiled, and what it must not be used to infer. |
| 项目绑定 | The exact dataset version a project currently consumes. |
| 业务口径 | Base, event, numerator, denominator, deduplication, attribution, and metric semantics. |
| 机器契约 | Activation, matching, conflict, and validation rules used by tooling. |

Do not call every durable fact a 口径. IDs, labels, enum mappings, source-field meanings, and project parameters are knowledge data. A business rule may reference a knowledge dataset, but it does not duplicate that dataset.

## Ownership Boundary

Use the following authority order:

1. `project_config.json`: dialect, engine, partition/time execution policy.
2. `knowledge-base/`: immutable source snapshots, versioned reference data, and usage contracts.
3. `<project>/knowledge/bindings.json`: active project dataset versions.
4. `<project>/rules/store.json` + immutable definitions: business metric/event definitions only.
5. activation contracts: when a saved business rule applies to candidate SQL.
6. artifact specs: which versions and rules one SQL actually used.

QUERY, REVIEW, SQL_FORMALIZE, VALIDATION, and DASHBOARD are read-only consumers. They may resolve bound data and persist `knowledge_reference_v1`, but they never register, refresh, or bind a dataset.

Durable knowledge writes require an explicit `KNOWLEDGE` route and a user request that names a config-table/knowledge action. Discovering a missing mapping during QUERY is feedback, not write permission.

`PLANNING_SOURCE` owns SVN-revision/folder releases and project source bindings. Config-Excel projection specs resolve one exact file from that release. An SVN PSR materializes only the declared file into ignored local storage, verifies it against the release manifest, and then lets Knowledge preserve the bytes in its immutable source snapshot. `SOURCE_WORKSPACE` remains for other unmanaged candidates. Neither source layer creates Knowledge or authorizes SQL use; only `KNOWLEDGE` creates and binds datasets. Read `planning-source.md` and `source-workspace.md`.

## Storage Contract

```text
knowledge-base/
├── catalog.json
├── projection_specs/<dataset_id>.export.json
├── source_snapshots/
│   ├── config_tables/<dataset_id>/<snapshot_id>/
│   ├── manual_mappings/<dataset_id>/<snapshot_id>/
│   └── external_references/<dataset_id>/<snapshot_id>/
├── imports/<dataset_id>/
│   ├── <projection>.csv
│   └── vNNN.import.json
├── datasets/<dataset_id>/<dataset_version>/
│   ├── manifest.json
│   ├── diff.json
│   ├── extraction_spec.json
│   ├── usage_contract.json
│   └── projections/<projection_id>/
│       ├── data.csv
│       ├── preview.csv
│       ├── schema.json
│       └── profile.json
└── contracts/<dataset_id>.json

sql-projects/<project>/
├── planning/source_binding.json
└── knowledge/bindings.json

planning-sources/<product>/stages/<stage>/releases/<release_id>/
├── release.json
├── files.json
├── diff.json
└── files/<complete source tree>  # non-SVN/legacy releases only
```

Persist repository-relative paths only. Never persist a download/temp absolute path. Snapshot and dataset directories are immutable; refreshing creates another content-addressed version. Each dataset version snapshots both its extraction spec and usage contract, so historical SQL meaning cannot drift when the current contract evolves. Project activation changes only `bindings.json`.

CSV is the canonical first-version projection format because it is portable and diffable. A later adapter may add Parquet without changing the public register/resolve contract.

## Lifecycle

### 1. Intake

- For config Excel, resolve the original file through the project's exact sealed planning-source release. For SVN this means exporting that one file at the PSR revision and verifying its hash; other source kinds use their reviewed import contract.
- Copy the exact resolved file into a content-addressed Knowledge source snapshot.
- Require the extraction spec to be `active`; `draft` or `pending_review` specs cannot be registered as reviewed knowledge.
- Preserve original file name, SHA-256, extraction spec, adapter identity, and audit fingerprint.
- Never edit the snapshot.

### 2. Project

- Read the source once through a reviewed extraction spec.
- Produce one or more projections. Usage belongs to each projection, not to the workbook as a whole.
- Validate required columns, non-empty unique primary keys, schema, and profiles.

### 3. Diff

- Compare the new version with the catalog's previous version by projection primary key.
- Record added/removed/changed keys and columns in `diff.json`.
- A refresh does not activate itself.

### 4. Bind

- Human or product owner reviews the declared use and diff.
- `bind` activates one exact content hash for one project.
- Rollback binds the previous immutable version; it does not rewrite data.

### 5. Resolve

- Daily SQL work reads only the active project binding.
- Resolution returns rows plus `knowledge_reference_v1` containing dataset, version, hash, projection, fields, and usage mode.
- SQL workspace/formal specs persist that reference when the data affected SQL or post-processing.

## Usage Contract

Every dataset contract must state:

- business purpose and project scope;
- explicit exclusions, especially similarly named tables;
- projection IDs, primary keys, required/default/allowed fields;
- supported delivery modes and row limits;
- source-log field bindings with evidence;
- allowed and forbidden inference;
- refresh and approval policy.

Supported modes:

| Mode | Use |
|---|---|
| `inline_mapping` | Compile a small, bounded map into SQL. |
| `filter_set` | Compile a reviewed set of IDs into an SQL predicate. |
| `result_enrichment` | Return IDs from SQL, then add labels locally. |
| `authoring_reference` | Help explain or author SQL without changing output. |
| `materialized_dimension` | Join a large mapping only through an approved DA/derived table. |

Local source files are never Hive/StarRocks runtime tables. Do not emit a local file path in SQL. Mapping size controls delivery mode, not ownership: even a two-row mutable mapping belongs here. Large mappings must use result enrichment or an approved materialized dimension rather than a giant CASE expression.

## SQL Workflow

1. For planning-backed Knowledge, use only the current project binding. Source `check`/`sync` is a separate explicit PLANNING_SOURCE action; QUERY never advances a planning source.
2. Discover the project's active bindings from their usage-contract semantics; do not guess a dataset ID from memory.
3. Match the request against `purpose`, `business_domain`, projection description, `allowed_usages`, and `field_roles`, then select one projection and usage mode.
4. Resolve by project, dataset, projection, fields, keys, and usage mode.
5. Use only returned rows. A missing key is a warning or blocker; never invent a label.
6. Keep raw TLOG structure discovery based on XML/catalog. A knowledge dataset explains mutable references; it does not become an original log filter.
7. Persist the returned `knowledge_reference_v1` with the exact query version when it affects SQL or post-processing.
8. Every workspace/formal SQL version also stores `knowledge_usage_v1`: `used`, `not_used`, `not_available`, or history-only `legacy_unknown`.
9. Formalization carries the same reference and usage state into QUERY/DASHBOARD specs when the logic fingerprint still matches.
10. Repository and Review display the human purpose plus dataset version. Physical paths and hashes stay in Code View/spec.

Read-only discovery and resolution do not authorize knowledge writes:

```powershell
python .\sql-engineering\scripts\config_knowledge.py list `
  --root .\sql-projects\DEMO_ANALYTICS `
  --active-only `
  --format json
```

The listing is bounded contract metadata only; it does not read projection rows. After selecting a projection semantically, call `resolve`. Fall back to source inspection only when no active contract matches, the project binding is missing, or the requested key is absent. Source inspection may identify candidate evidence, but it cannot promote a mutable owner, contact, enum, or mapping to current project truth.

When a mapping is also used inside a business metric, the canonical rule declares `dataset_id`, `projection_id`, semantic role, required fields, and `binding_policy=active_project_binding`. Do not paste rows or KDV hashes into rule prose. The project binding owns the exact active version; the SQL artifact owns the exact `knowledge_reference_v1` it consumed.

Before writing a binding, classify the transition as `initial`, `provenance_only`, `projection_changed`, or `contract_changed`. Check every current rule that requires the dataset. Missing projections/fields, changed primary keys or required field semantics, and loss of `authoring_reference` block before activation. A compatible rebind records `knowledge_binding_impact_v1` with `rule_version_required=false`; only an independently confirmed business-semantic change creates a canonical-rule version.

## Commands

Register the reviewed first version through the Skill-owned deterministic projection adapter:

```powershell
python .\sql-engineering\scripts\config_knowledge.py register `
  --repo-root . `
  --dataset-id item_table_battle `
  --contract .\knowledge-base\contracts\item_table_battle.json `
  --adapter-spec .\knowledge-base\projection_specs\item_table_battle.export.json `
  --run-adapter `
  --function-selection KNOWLEDGE `
  --user-request "<verbatim request>" `
  --format json
```

Register a reviewed static mapping. The import spec uses `knowledge_static_import_v1`, repository-relative `source_file`, and one exact CSV source per declared projection:

```powershell
python .\sql-engineering\scripts\config_knowledge.py register `
  --repo-root . `
  --dataset-id creation_level_craft_item `
  --contract .\knowledge-base\contracts\creation_level_craft_item.json `
  --adapter-spec .\knowledge-base\imports\creation_level_craft_item\v001.import.json `
  --function-selection KNOWLEDGE `
  --user-request "<verbatim request>" `
  --format json
```

For ordinary `manual_mapping` and `external_reference` imports, registration reuses reviewed CSV projections and does not run an adapter. A reviewed C# generated constants source may instead declare `extractor_id=csharp_const_int_v1` and use `--run-adapter`. The extractor reads one named class's `public const int` values, blocks duplicate names/values or an unexpected row count, writes one CSV projection, and preserves the complete `.cs` as the immutable source snapshot.

```powershell
python .\sql-engineering\scripts\config_knowledge.py register `
  --repo-root . `
  --dataset-id entity_type_catalog `
  --contract .\knowledge-base\contracts\entity_type_catalog.json `
  --adapter-spec .\knowledge-base\imports\entity_type_catalog\v001.import.json `
  --run-adapter `
  --function-selection KNOWLEDGE `
  --user-request "新增代码文件实体类型资料并绑定 BASE" `
  --format json
```

Refresh after a reviewed Excel/spec update and execute the adapter once:

```powershell
python .\sql-engineering\scripts\config_knowledge.py refresh `
  --repo-root . `
  --dataset-id item_table_battle `
  --contract .\knowledge-base\contracts\item_table_battle.json `
  --adapter-spec .\knowledge-base\projection_specs\item_table_battle.export.json `
  --run-adapter `
  --function-selection KNOWLEDGE `
  --user-request "<verbatim request>" `
  --format json
```

Bind only after reviewing `diff.json`:

```powershell
python .\sql-engineering\scripts\config_knowledge.py bind `
  --root .\sql-projects\DEMO_ANALYTICS `
  --dataset-id item_table_battle `
  --reason "Approved for BattleItem ID explanation" `
  --function-selection KNOWLEDGE `
  --user-request "<verbatim request>" `
  --format json
```

Resolve from the active binding without opening Excel:

```powershell
python .\sql-engineering\scripts\config_knowledge.py resolve `
  --root .\sql-projects\DEMO_ANALYTICS `
  --dataset-id item_table_battle `
  --projection items `
  --usage-mode result_enrichment `
  --key-field id `
  --key 10010001 `
  --field id `
  --field name `
  --out item-10010001.json `
  --format json
```

Resolver receipts are write-safe: `--out` is a project-relative JSON name/path under `query_workspace/_working/knowledge_receipts/`. Absolute paths, parent traversal, and writes outside that directory are rejected. New references include requested/selected counts and hashes plus one `resolution_fingerprint`; lookup rows are not copied into formal specs.

## Quality Gates

Block registration or binding when:

- the extraction spec is not explicitly `active`;
- a persisted path is absolute or escapes the repository;
- source, projection data, projection schema/profile/preview, or resolution hashes do not match;
- required columns or primary keys are missing, empty, or duplicated;
- dataset/contract/project IDs disagree;
- the project is outside contract scope;
- a requested usage mode or field is not allowed;
- a refresh tries to overwrite an immutable version;
- materialized-dimension use lacks explicit approval.
- a required current rule dependency cannot resolve its projection and fields from the target active binding;
- a bind would remove a required projection/field, change its primary key/field semantics, or remove authoring access;
- a current rule duplicates mutable mapping rows that belong in knowledge, regardless of row count.

Warn or block SQL delivery when a used mapping lacks a persisted `knowledge_reference_v1`, the active binding changed after generation, or requested IDs are absent. Never silently fall back to stale Excel, canonical-rule prose, or guessed labels.
