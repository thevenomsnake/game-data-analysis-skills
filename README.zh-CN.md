# Game Data Analysis Skills

**一套面向 Codex、以文件为准的 SQL 生命周期。**

Game Data Analysis Skills 把对话里的 SQL 工作变成长期可用的项目文件。每一条生成或
修改后的 SQL 都会落盘、版本化、建立索引、支持检索，并通过精确路径交付；对话结束后，
别人仍然能找到它、理解它、继续修改它。

[English](README.md) · [日本語](README.ja.md) · [Español](README.es.md) · [한국어](README.ko.md)

> 聊天里的 SQL 代码块用于解释；通过校验的 `vNNN.sql` 文件才是交付物。

## 它解决什么问题

聊天生成的 SQL 很容易丢。真正有用的查询经常被复制到另一个文件后继续修改，逐渐失去
版本历史、用途说明和结果对应关系；外部传入的 SQL 还可能被直接覆盖。过一段时间，没人
知道哪一版跑出了当时的结果。

这个 Skill 给 Codex 一套小而明确的工作空间契约：

| 能力 | 实际行为 |
|---|---|
| 工作空间初始化 | 自动创建稳定的 `sql-projects/` 结构和首个项目 |
| SQL 文件交付 | 每次生成或修改都保存为不可变的 `vNNN.sql` |
| 外部 SQL 导入 | 把外部文件视为输入，在项目内保存新版本，不覆盖原文件 |
| 可检索历史 | 保存人能读懂的标题、用途、标签、方言、路径和内容哈希 |
| 持续修改 | 同一分析问题保留在一个查询族中，通过新版本继续演进 |
| 精确回执 | 交付前核对 SQL 文件、元数据、索引和当前内容哈希 |
| 生命周期区分 | 明确区分临时查询、可复用查询和面向看板的 SQL |

公开规范版刻意不包含任何公司的项目配置、生产表名、内部口径、凭据、查询结果或内部
执行平台接入。

## 三分钟开始

### 1. 安装 Skill

克隆仓库，将 `sql-engineering/` 复制或链接到 Codex Skills 目录：

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

刷新或重启 Codex 后，可以在任务中使用 `$sql-engineering`。

### 2. 初始化工作空间

```powershell
python .\sql-engineering\scripts\sql_workspace.py bootstrap `
  --root . `
  --project-id example `
  --dialect starrocks
```

仓库已经带有共享的 `_asset_catalog`、`_review_inbox`、`_rule_review` 目录骨架。
`bootstrap` 会补齐缺失目录并初始化 `sql-projects/example`；重复运行不会清空已有内容。

### 3. 直接用自然语言提需求

```text
$sql-engineering 生成一条 StarRocks SQL，按天统计去重登录用户数。
固定日期放在 params CTE，保存到 example 项目，并返回准确文件路径。
```

Codex 应读取项目配置，创建或复用查询族，保存类似
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql` 的版本，执行
receipt 校验，然后返回绝对文件路径。是否在数据库中实际执行必须单独说明，不能默认声称。

## 常见用法

| 目标 | 示例 |
|---|---|
| 生成 SQL | `$sql-engineering 为这个项目生成每日活跃用户查询，并保存到文件。` |
| 修改外部 SQL | `$sql-engineering 导入这份 SQL，按项目方言修正，不要覆盖原文件。` |
| 查找旧查询 | `$sql-engineering 查找留存相关的历史 SQL，并说明各自用途。` |
| 继续扩展 | `$sql-engineering 在现有活跃用户查询族中增加平台维度。` |
| 保留有价值查询 | `$sql-engineering 这套逻辑已确认，保存为 retained 查询版本。` |
| 核对交付 | `$sql-engineering 校验这个 v003.sql 的 receipt，并返回准确路径。` |

## 生命周期

```text
需求
  -> 读取项目和方言
  -> 保存临时 SQL 版本
  -> 在用户环境执行
  -> 修正或扩展为下一版本
  -> 可选升级为 retained 或 dashboard SQL
  -> 精确 delivery receipt
```

一个查询族代表一个分析问题。日期刷新、语法修正、同一问题的完整扩展继续使用同一个
查询族并生成新版本；Base、核心指标或要支持的决策发生变化时，新建查询族。

## 目录结构

```text
sql-projects/
  _asset_catalog/              预留的跨项目检索产物
  _review_inbox/               等待导入或审查的外部 SQL 和证据
  _rule_review/                预留的口径审查产物
  example/
    .sql-engineering/
      project.json             项目标识和方言
    sql-workspace/
      index.json               可搜索机器索引
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

三个下划线目录是稳定扩展入口。公开核心会创建目录，但不会伪造资产目录、Review 或口径
内容。

## 命令参考

| 命令 | 用途 |
|---|---|
| `bootstrap` | 创建仓库结构，并可同时初始化首个项目 |
| `init` | 初始化一个独立项目 |
| `save` | 保存新的不可变 SQL 版本并更新索引 |
| `search` | 检索标题、用途和标签 |
| `receipt` | 交付前校验某个准确 SQL 版本 |

可以直接使用仓库里的虚构示例
[`daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql)。
[AI 完整工作示例](sql-engineering/references/example.md) 展示了需求、命令、产物结构和最终
回复要求。

## 必须知道的边界

- 项目配置决定方言；Skill 不猜测数据表、分区字段、业务 ID 或指标口径。
- 外部 SQL 始终是不可变输入，修改结果保存到项目内。
- 已保存版本不会被覆盖；手工篡改会被 receipt 的哈希检查发现。
- 生命周期标签只描述用途，不等于 SQL 已经跑通或业务口径已经确认。
- 结果、可视化、验证和看板可以由治理扩展继续实现，但不会从一份 SQL 静默推断出来。
- 凭据、私有表结构、生产结果和本机绝对路径不能提交到仓库。

## 文档

| 主题 | 文档 |
|---|---|
| AI 工作流和硬边界 | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| 完整示例 | [`references/example.md`](sql-engineering/references/example.md) |
| 项目和目录契约 | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| 查询族生命周期 | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL 交付检查 | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| 贡献方式 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 安全规范 | [SECURITY.md](SECURITY.md) |

## 开发校验

公开版只使用 Python 标准库。

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py
```

本项目使用 [Apache License 2.0](LICENSE)。
