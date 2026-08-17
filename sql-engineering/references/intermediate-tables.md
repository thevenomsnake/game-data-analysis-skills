# Intermediate Table Contract

Use this reference whenever a task plans, registers, updates, reviews, or depends on project intermediate tables.

## Core Model

Intermediate tables are derived source contracts. They do not replace project canonical rules or XML/TLOG source catalogs.

Priority order for business truth:

1. User-confirmed project canonical rules.
2. Current user instruction for the task.
3. Registered intermediate table contract, only for declared fields/grain and only when the table is allowed by availability status.
4. XML/TLOG catalog structure and field names.
5. XML comments/descriptions and older generated SQL.

The same business口径 can have two physical implementations:

- `intermediate route`: downstream SQL reads a registered table.
- `raw-log fallback route`: downstream SQL rebuilds the same口径 from original logs/XML/catalog and confirmed rules.

The business meaning should stay consistent between routes. Differences caused by materialization, refresh, partition, missing columns, or validation gaps must be recorded in the table metadata or downstream SQL spec.

## Availability Contract

Each current intermediate table should carry these fields in `manifest.json` and its sibling `.meta.json`:

- `availability_status`: `available`, `unavailable`, or `unknown`.
- `availability_source`: `user_declared`, `detected`, `validation`, `manual_review`, or `not_checked`.
- `source_contract_mode`: `dual_path`, `intermediate_preferred`, `intermediate_only`, or `raw_logs_only`.
- `fallback_policy`: how to rebuild or proceed when the table cannot be used.
- `fallback_source_tables`: original logs/tables for fallback.
- `fallback_sql_reference`: SQL/spec path for fallback logic when available.
- `canonical_rule_refs`: concept keys or project rule ids whose business口径 the table implements.
- `xml_source_refs`: XML/catalog logs or fields used by the fallback route.
- `field_contract`: important table columns and meanings.
- `grain_contract`: one row means what, including dedup keys.

Status semantics:

- `available`: the table can be used in target environment for its declared contract. Prefer attaching validation evidence.
- `unknown`: registered/planned, but not target-verified. Do not present downstream SQL as target-verified solely because it reads this table.
- `unavailable`: do not use the table for new downstream SQL. Use fallback logic or ask the user to restore/verify the table.

If the user says a table is unavailable, treat that as authoritative current instruction. Run `update-table --availability-status unavailable --availability-source user_declared --fallback-required true` and record the reason. If a script or validation detects the table is unavailable, use `--availability-source detected|validation`.

## Generation Rules

Before generating SQL that could use a prepared layer:

1. Run `search-tables` for the project and inspect availability.
2. Use an intermediate table only when `availability_status=available`, or when the user explicitly accepts an `unknown` table risk.
3. If `availability_status=unavailable`, generate from fallback raw logs and cite the table as unavailable evidence.
4. If `source_contract_mode=intermediate_only`, ask before falling back unless the user explicitly says the table is unavailable.
5. If `source_contract_mode=raw_logs_only`, do not use the physical table even if it is registered.
6. Save downstream SQL with `--intermediate-tables` only for tables actually read by the SQL.
7. Put fallback route notes in the SQL spec `intermediate_tables[]` entry.

## Registration Rules

Current behavior is manual-first.

- Register a table from a user-provided detailed specification or after the user accepts a plan.
- Do not auto-create or auto-suggest tables from complexity alone yet.
- Save a new version for corrections to field contract, grain, partition, dependency, availability, or fallback route when the old definition should no longer be current.
- Use `update-table` for status-only changes such as temporary unavailability.
- Intermediate table metadata is not a canonical business rule. When the user confirms a reusable口径, save or update the project canonical rule separately and reference it from `canonical_rule_refs`.

## Command Pattern

```bash
req="[INTERMEDIATE_TABLE] register BASE raid intermediate table"
python scripts/sql_project.py save-table \
  --root ./sql-projects/DEMO_ANALYTICS \
  --table-name hy_idog_oss.tmp_sr_bpkg_514_first_raid \
  --title "BASE 抄家分析：目标玩家首次被抄家事件" \
  --sql-file ./work/tmp_sr_bpkg_514_first_raid.sql \
  --business-category raid_analysis \
  --analysis-type intermediate_build \
  --purpose "沉淀目标玩家首次被抄家事件，供下游分布、流失、回流分析复用。" \
  --grain "vOpenID" \
  --source-tables Territory,BattleLogInOut \
  --fallback-source-tables Territory,BattleLogInOut \
  --availability-status unknown \
  --availability-source user_declared \
  --source-contract-mode dual_path \
  --fallback-policy "中间表不可用时从 Territory + BattleLogInOut + 目标人群逻辑回退重建首次被抄家。" \
  --field-contract "first_raid_time/raid_battlesrvid/raid_match_type 表示目标玩家首次被抄家事件。" \
  --grain-contract "每个目标 vOpenID 最多一行首次被抄家事件。" \
  --reusable \
  --reuse-notes "可复用于 BASE 抄家分布、流失、回流分析；使用前需确认目标环境表已落地或走 fallback。" \
  --user-request "$req" --function-selection "[INTERMEDIATE_TABLE]"
```