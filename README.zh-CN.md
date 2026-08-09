# Game Data Analysis Skills

**一套面向 Codex、以文件为准并支持可配置只读数据库执行的 SQL 生命周期。**

Game Data Analysis Skills 把对话里的 SQL 工作变成长期可用的项目文件。每一条生成或
修改后的 SQL 都会落盘、版本化、建立索引、支持检索，并通过精确路径交付；对话结束后，
别人仍然能找到它、理解它、继续修改它。

[English](README.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [Español](README.es.md) · [한국어](README.ko.md)

> 聊天里的 SQL 代码块用于解释；通过校验的 `vNNN.sql` 文件才是交付物。

## 它解决什么问题

聊天生成的 SQL 很容易丢。真正有用的查询经常被复制到另一个文件后继续修改，逐渐失去
版本历史、用途说明和结果对应关系；外部传入的 SQL 还可能被直接覆盖。过一段时间，没人
知道哪一版跑出了当时的结果。

这个 Skill 给 Codex 一套小而明确的工作空间契约：

| 能力 | 实际行为 |
|---|---|
| 工作空间初始化 | 自动创建稳定的 `sql-projects/` 结构和首个项目 |
| 项目资料治理 | 分开版本化埋点原始定义、策划输入、人工确认资料和固定口径 |
| SQL 文件交付 | 每次生成或修改都保存为不可变的 `vNNN.sql` |
| 按环境执行 | 通过配置好的只读 DB-API 驱动或数据库命令行执行已保存 SQL |
| 外部 SQL 导入 | 把外部文件视为输入，在项目内保存新版本，不覆盖原文件 |
| 可检索历史 | 保存人能读懂的标题、用途、标签、方言、路径和内容哈希 |
| 持续修改 | 同一分析问题保留在一个查询族中，通过新版本继续演进 |
| 精确回执 | 交付前核对 SQL 文件、元数据、索引和当前内容哈希 |
| 生命周期区分 | 明确区分临时查询、可复用查询和面向看板的 SQL |

公开规范版刻意不包含任何公司的项目配置、生产表名、内部口径、凭据、查询结果或内部
执行平台接入。

## 一个项目需要提供什么

| 必要资料 | Skill 如何管理 |
|---|---|
| 埋点原始定义 | XML、JSON、YAML、Excel、CSV、文本或其他格式原样复制到 `sources/raw/`，记录哈希和版本 |
| 数据库与 SQL 方言 | 按环境声明生成 SQL 使用的方言；本机 DB-API 或数据库客户端连接信息不进入 Git |
| 策划表和配置表 | 原件保存到 `knowledge/planning/`，它们只是证据，不会自动变成正确口径 |
| 已人工确认资料 | 在 `knowledge/confirmed/` 保存确认版本、确认人、原因以及与策划表的关系 |
| 固定业务口径 | 把已明确确认的 Base、粒度、算法、筛选和引用资料保存为 `rules/definitions/` 下的不可变版本 |

Skill 不会替项目编造这些事实，它负责让资料所有权和变化过程可见。完整步骤见
[项目接入手册](sql-engineering/references/project-onboarding.md)。

## 创建并接入项目

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

`bootstrap` 会初始化 `sql-projects/example`，同时创建空的埋点、知识、口径和 SQL 清单；
重复运行只补齐缺失的空结构，不会清空已登记内容。

### 3. 登记项目资料

先把埋点原始文件、策划/配置表和单独人工确认的资料交给 Codex 登记，再声明数据库环境和
SQL 方言，只固定用户明确确认的口径。最后运行：

```powershell
python .\sql-engineering\scripts\sql_workspace.py status `
  --root .\sql-projects\example
```

`query_context_ready=false` 表示还没有登记任何埋点原始定义。没有自动数据库连接并不阻断，
该项目会使用人工执行 SQL、返回结果文件的流程。

### 4. 直接用自然语言提需求

```text
$sql-engineering 生成一条 StarRocks SQL，按天统计去重登录用户数。
固定日期放在 params CTE，保存到 example 项目，并返回准确文件路径。
```

Codex 应读取项目配置，创建或复用查询族，保存类似
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql` 的版本，执行
receipt 校验，然后返回绝对文件路径。是否在数据库中实际执行必须单独说明，不能默认声称。

自动查询是可选能力。项目只登记环境名，真实连接配置保存在 Git 忽略的
`.sql-engineering/connections.local.json`。如果没有驱动、数据库客户端、密钥或连接配置，
Skill 会返回 `manual_required` 和准确 SQL 路径，请用户自行查询并把结果文件返回；不会点击
Chrome 或 DA 网页控制台。

## 常见用法

| 目标 | 示例 |
|---|---|
| 创建项目 | `$sql-engineering 创建 alpha 项目，SQL 方言为 StarRocks，并告诉我还缺哪些埋点、资料、口径和连接配置。` |
| 登记埋点 | `$sql-engineering 把这个 XML 原样登记为 PlayerLogin 的埋点来源定义。` |
| 登记策划证据 | `$sql-engineering 把这份模式配置表保存为策划输入，不要直接当成确认口径。` |
| 固定口径 | `$sql-engineering 把人工确认的日活定义固定为新的口径版本。` |
| 生成 SQL | `$sql-engineering 为这个项目生成每日活跃用户查询，并保存到文件。` |
| 修改外部 SQL | `$sql-engineering 导入这份 SQL，按项目方言修正，不要覆盖原文件。` |
| 查找旧查询 | `$sql-engineering 查找留存相关的历史 SQL，并说明各自用途。` |
| 继续扩展 | `$sql-engineering 在现有活跃用户查询族中增加平台维度。` |
| 保留有价值查询 | `$sql-engineering 这套逻辑已确认，保存为 retained 查询版本。` |
| 核对交付 | `$sql-engineering 校验这个 v003.sql 的 receipt，并返回准确路径。` |
| 直接查询 | `$sql-engineering 用已配置的开发数据库执行这条已保存查询。` |

## 生命周期

```text
需求
  -> 登记埋点原始定义
  -> 分开保存策划资料和人工确认资料
  -> 固定当前适用口径
  -> 选择数据库环境和 SQL 方言
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
    sources/
      source-catalog.json
      raw/<source>/vNNN.*      原样保存的埋点定义
    knowledge/
      planning/<item>/vNNN.*   策划表和配置表原件
      confirmed/<item>/vNNN.* 已人工确认资料
    rules/
      definitions/<rule>/vNNN.json
    context/                    非权威说明和平台手册
    sql-workspace/
      index.json               可搜索机器索引
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

三个下划线目录是跨项目扩展入口。项目内部把原始证据、人工确认、固定口径和可执行 SQL
分开保存，避免其中一种资料静默替代另一种。

## 命令参考

| 命令 | 用途 |
|---|---|
| `bootstrap` | 创建仓库结构，并可同时初始化首个项目 |
| `init` | 初始化一个独立项目 |
| `environment` | 把项目环境名映射到本地数据库连接配置 |
| `source` | 不改变格式地复制并登记埋点原始定义 |
| `knowledge` | 登记策划输入或单独的人工确认资料 |
| `rule` | 把明确确认的业务口径固定为新的不可变版本 |
| `status` | 显示项目还缺哪些来源、资料、口径和执行配置 |
| `save` | 保存新的不可变 SQL 版本并更新索引 |
| `search` | 检索标题、用途和标签 |
| `receipt` | 交付前校验某个准确 SQL 版本 |
| `sql_execute.py run` | 执行已保存的只读 SQL，或返回人工查询交接 |

可以直接使用仓库里的虚构示例
[`daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql)。
[AI 完整工作示例](sql-engineering/references/example.md) 展示了需求、命令、产物结构和最终
回复要求。

## 下一步

- 按设定周期自动生成报告。
- 跨资产对比数据结果，检查结果是否合理。
- 自动追溯异常来源并排查可能原因。

## 必须知道的边界

- 项目配置决定方言；Skill 不猜测数据表、分区字段、业务 ID 或指标口径。
- 项目资料是可选且必须显式声明的；Skill 不依赖个人知识库。缺少表结构时，可通过已保存的
  只读数据库查询检查元数据或枚举。
- 自动执行只支持 DB-API 或数据库命令行客户端，不支持浏览器和 DA 网页控制台；缺少配置时
  正常转为人工查询。
- 外部 SQL 始终是不可变输入，修改结果保存到项目内。
- 已保存版本不会被覆盖；手工篡改会被 receipt 的哈希检查发现。
- 生命周期标签只描述用途，不等于 SQL 已经跑通或业务口径已经确认。
- 结果、可视化、验证和看板可以由治理扩展继续实现，但不会从一份 SQL 静默推断出来。
- 凭据、私有表结构、生产结果和本机绝对路径不能提交到仓库。

## 文档

| 主题 | 文档 |
|---|---|
| AI 工作流和硬边界 | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| 新项目资料和接入流程 | [`references/project-onboarding.md`](sql-engineering/references/project-onboarding.md) |
| 完整示例 | [`references/example.md`](sql-engineering/references/example.md) |
| 项目和目录契约 | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| 查询族生命周期 | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL 交付检查 | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| 数据库环境和执行 | [`references/database-execution.md`](sql-engineering/references/database-execution.md) |
| 数据库连接方式与 SQL 方言 | [`references/dialects.md`](sql-engineering/references/dialects.md) |
| 贡献方式 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 安全规范 | [SECURITY.md](SECURITY.md) |

## 开发校验

公开版核心只使用 Python 标准库；DB-API 自动查询会加载用户在本机连接配置中选择的数据库驱动。

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py .\sql-engineering\scripts\sql_execute.py
```

本项目使用 [Apache License 2.0](LICENSE)。
