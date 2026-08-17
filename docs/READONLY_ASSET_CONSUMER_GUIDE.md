# SQL 资产只读复制与外部 Web 集成手册

> 适用版本：SQL Engineering Skill 4.229.0 / `sql_asset_catalog_v2` + `execution_delivery_v1` + `reusable_workbook_presentation_v1`
>
> 目标读者：在其他环境实现资料库 Web、搜索助手或离线镜像的 AI/工程人员
>
> 边界：本仓库只提供结构化原始资产和索引；外部系统负责复制、存储、检索、接口和页面，不得回写源仓库

## 1. 目标

外部工具不需要理解 `da_skills` 的内部工作流，也不需要读取 Query Workspace 或 Promotion Ledger。推荐通过版本化 Provider Snapshot 读取共享闭包：

```text
sql-projects/_asset_catalog/provider_manifest.json
sql-projects/_asset_catalog/provider_snapshot.json
```

Provider manifest 使用稳定 `repository_id`，Provider Snapshot 使用 `(repository_id, project_id, asset_id)` 作为外部身份。消费者应固定 Git commit 和 `snapshot_digest`，以只读方式解析快照列出的正式 Package 文件。

快照内部包含以下三份共享投影：

```text
sql-projects/_asset_catalog/asset_catalog.json
sql-projects/_asset_catalog/asset_organization.json
sql-projects/_asset_catalog/asset_group_registry.json
```

然后按目录中声明的仓库相对路径复制原始文件。

这里的“结果文件”是 SQL 输出预览，不是可复用数据集：超过 10 MB 的原始结果已经由源 skill 替换为可验证切片。Excel 分析、对比报告和可视化才是可复用产物，目录会引用其完整文件。

共享资产状态只用于展示和筛选。正式草稿、失败证据、正式/历史 SQL、提案规则和已验证资产都允许复制和展示。项目本地 `query_workspace/` 不属于外部消费面，不进入 Git 或这三份 JSON。

本仓库不负责：

- 提供 Web 服务或 API；
- 决定外部页面布局；
- 替外部系统建立数据库或搜索索引；
- 为外部系统筛选“可发布资产”；
- 接收外部系统对 SQL、口径或状态的回写。

## 2. 权威入口

| 内容 | 权威入口 | 说明 |
|---|---|---|
| Provider 快照与版本身份 | `sql-projects/_asset_catalog/provider_manifest.json` + `provider_snapshot.json` | 外部资料库的唯一只读入口；固定 Git commit 与 snapshot digest |
| 跨项目资产身份、文件和关系 | `sql-projects/_asset_catalog/asset_catalog.json` | Provider Snapshot 的共享投影之一 |
| 目录结构 schema | `sql-engineering/schemas/asset_catalog.json` | 校验 `sql_asset_catalog_v2` |
| 跨项目业务主题导航 | `sql-projects/_asset_catalog/asset_organization.json` | 按新增、活跃、留存等主题浏览；用 `asset_id` 连接 catalog |
| 语义导航 schema | `sql-engineering/schemas/asset_organization.json` | 校验 `sql_asset_organization_v2` |
| 稳定分析资产组目录 | `sql-projects/_asset_catalog/asset_group_registry.json` | 主页按 `AG-NNNN` 展示一个分析问题及其 SQL 版本、结果、可视化、验证和看板附件 |
| 资产组 schema | `sql-engineering/schemas/asset_group_registry.json` | 校验 `sql_asset_group_registry_v2` |
| SQL 执行交付投影 | `assets[].facts.execution_delivery` | 校验 `sql-engineering/schemas/execution_delivery.json` |
| 显式执行变体身份 | `execution_route_v1.execution_variant_identity` | 校验 `sql-engineering/schemas/execution_variant_identity.json`；仅上游显式写入 |
| 可复用工作簿投影 | `assets[].facts.workbook_presentation` | 校验 `sql-engineering/schemas/reusable_workbook_presentation.json` |
| 工作簿结构清单 | `workbook_presentation.workbook_manifest` | 校验 `sql-engineering/schemas/workbook_manifest.json`；允许旧资产为空对象 |
| 正式 SQL 友好读取模型 | `sql-projects/<PROJECT>/reviews/sql_repository.json` | 可用于快速构建正式查询浏览页 |
| 项目口径 | `sql-projects/<PROJECT>/rules/store.json` 和 `rules/definitions/` | 当前指针与不可变历史版本 |
| 跨项目口径友好读取模型 | `sql-projects/_rule_review/rule_dictionary.json` | 可用于快速构建口径浏览页 |
| 知识数据版本 | `knowledge-base/catalog.json` | 不可变数据集版本和 manifest |
| 项目知识绑定 | `sql-projects/<PROJECT>/knowledge/bindings.json` | 项目实际绑定的精确版本 |
| 平台与操作手册 | catalog 中 `asset_kind=documentation` | `facts.document_kind`、`audience`、`consumer_scope` 可用于导航 |
| 外部消费契约 | catalog 中 `asset_kind=consumer_contract` | Schema、taxonomy 与消费 reference 的版本化入口 |

外部系统不要自行扫描文件名猜资产，也不要从 HTML 反向解析数据。HTML 是展示产物，JSON 和被其引用的原始文件才是机器入口。

Provider Snapshot 明确排除 `query_workspace/`、`promotion_ledger.json`、未登记文件 inventory、缓存、凭据和绝对路径。外部资料库不得通过本地 resolver 回写 Provider，也不得把 Workspace 当作第二个消费接口。

`asset_catalog.json` 决定“有什么”，`asset_organization.json` 决定“如何按业务主题找到”，`asset_group_registry.json` 决定“哪些分析资产共同构成一个主页目录项”。`AG-0001` 是永久组身份，SQL 的 `v001/v002` 仍是组内版本；页面重排只能改 `display_order`，不能重算或改写 `AG` 编号。

## 3. Catalog 数据模型

`asset_catalog.json` 包含四个核心数组。

### 3.1 `assets`

每个资产具有稳定 `asset_id`，并声明：

- `asset_kind`：资产类型；
- `project_id`：所属项目；
- `title`、`summary`：可展示文本；
- `lifecycle_state`：临时、草稿、当前、历史、提案等状态；
- `verification_state`：未验证、通过、代理验证、结果确认等状态；
- `version`：资产版本；
- `primary_path`、`file_paths`：组成该资产的原始文件；
- `generation_provenance`：生成 skill、版本、脚本和时间；
- `facts`：资产类型专属的结构化摘要。

不要用 `lifecycle_state` 或 `verification_state` 决定资产是否可见。它们只是外部页面的标签和筛选字段。

#### 3.1.1 精确 SQL 执行交付

每个共享 `query`、`validation`、`dashboard` 等精确 SQL 资产通过
`facts.execution_delivery` 暴露执行事实：

- `status=materialized|legacy_unlabeled`；
- `materialized_engine_key`、`sql_dialect`、`query_engine`；
- `profile_id`、`routing_role`、`route_status`；
- `execution_evidence`：有界路由原因、阻断项、来源计数和 exact SQL hash；
- `portable_template.available|contract|path`；
- 显式变体存在时的 `logical_revision_id`、`variant_group_id`、`variant_key`、
  `exact_variant_asset_ids` 和 `recommended_variant_asset_id`。

这些字段只投影已持久化的 `execution_route_v1`。外部消费者严禁根据标题、tag、数据库/表前缀、
SQL 文本、路径或 `branch_of` 推断引擎或双引擎关系。只有上游明确写入一致的
`execution_variant_identity_v1`，两个 exact asset 才能显示为同一逻辑 revision 的执行变体。
identity 不一致时 `variant_status=identity_conflict` 并产生 catalog issue，不得自动修复或合并。

`portable_template.available=true` 只表示存在可移植业务模板。若
`exact_variant_asset_ids` 没有两个物化目标，仍按 `materialized_engine_key` 展示单引擎。旧 SQL 没有
route 时保持 `legacy_unlabeled`，仍可查看、下载和加入索引。

#### 3.1.2 可复用工作簿与结果证据

派生资产通过 `facts.consumer_surface` 明确分面：

| `consumer_surface` | 条件 | 用途 |
|---|---|---|
| `reusable_workbook` | `analysis_workbook`、`comparison_workbook`，或 `visualization` 且媒体为 XLSX | 可视化 Excel 一级入口 |
| `result_evidence` | SQL 返回结果证据，不论 CSV 还是 XLSX | 结果与证据，不进入可复用 Excel |
| `other` | HTML visualization、export 或其他派生产物 | 按原类型展示 |

不要按 `.xlsx` 后缀决定入口。只使用 `facts.workbook_presentation.eligible=true` 进入可复用 Excel。
新工作簿带有 `workbook_manifest_v1`，只包含工作表名称/可见性、图表数量/标题和固定上限，不含
单元格数据。旧工作簿没有 manifest 时仍然 eligible，`preview_status=not_available`，必须保留
`download_path` 和下载能力。缺少静态预览不能阻断快照同步，也不能触发消费端在线重算图表。

文件的媒体类型、大小、SHA-256 和路径仍以 catalog `files` 为准；SQL/result/workbook 的精确血缘
仍以 `relationships` 为准。不要用文件名、时间接近或目录相邻补造 lineage。

### 3.2 `files`

每个文件记录：

- 仓库相对 `path`；
- `exists`；
- `sha256`；
- `size_bytes`；
- `media_type`；
- 文件在资产中的 `roles`；
- 引用该文件的 `asset_ids`。

`files` 是复制文件和校验内容的依据。禁止将相对路径改写为源机器绝对路径后保存到外部元数据。

### 3.3 `relationships`

关系用于连接：

- Workspace SQL 与 Formal Asset Package 中的 Formal Query；
- QUERY 与 Dashboard、Validation、run evidence；
- SQL 与结果、Excel、可视化；新资产通过 `derived_from_result` / `has_visualization` 精确连接结果，不按文件名猜配对；
- SQL 与实际引用口径；
- SQL/口径与知识数据版本；
- 资产的前后版本、分支和 supersede 链路。

外部 Web 应按关系展示附件和上下游，不要根据 slug 或目录名重新猜关系。

### 3.4 `issues`

缺文件、旧引用或不安全路径会进入 `issues`。问题资产仍保留在 `assets` 中。

外部系统应展示诊断，但不能因为有 issue 就静默隐藏资产。

## 4. 源仓库准备

源仓库维护者在外部同步前生成最新快照：

```powershell
python .\sql-engineering\scripts\asset_catalog.py build `
  --projects-root .\sql-projects `
  --format json
```

然后校验路径和哈希：

```powershell
python .\sql-engineering\scripts\asset_catalog.py validate `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --repo-root . `
  --format json
```

`pass` 表示没有目录诊断；`warn` 表示目录可用但存在应展示的 issue；`fail` 表示目录结构、路径或已声明文件哈希不一致。

命令 stdout 只返回有界状态摘要和有限问题样例，完整 catalog 始终写入 JSON 文件。不要让调用方
从 stdout 接收完整资产数组或 workbook manifest。`asset_organization.py scan` 支持
`--offset/--limit` 分页；group registry 的完整成员和诊断只读取落盘文件。

目录按需或在外部同步前重建，不挂到 QUERY、固化、Review、Validation、Dashboard、仓库页或普通保存流程，避免拖慢日常 SQL 工作。文档扫描和链接校验也只在这次独立维护任务中发生。

随后刷新语义 overlay：

```powershell
python .\sql-engineering\scripts\asset_organization.py refresh `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --function-selection ASSET_ORGANIZATION `
  --user-request "外部同步前刷新资产语义整理" `
  --format json

python .\sql-engineering\scripts\asset_organization.py validate `
  --organization .\sql-projects\_asset_catalog\asset_organization.json `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --format json
```

外部页面用 `asset_id` 连接两个 JSON，左侧导航读取 `navigation_path`；`needs_semantic_review` 和 `stale_semantics` 只显示整理提醒，不能隐藏资产。

最后刷新稳定资产组目录：

```powershell
python .\sql-engineering\scripts\asset_group_registry.py refresh `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --organization .\sql-projects\_asset_catalog\asset_organization.json `
  --function-selection ASSET_ORGANIZATION `
  --user-request "外部同步前刷新稳定资产组目录" `
  --format json

python .\sql-engineering\scripts\asset_group_registry.py validate `
  --catalog .\sql-projects\_asset_catalog\asset_catalog.json `
  --organization .\sql-projects\_asset_catalog\asset_organization.json `
  --format json
```

主页按 `groups[].display_order` 建目录，显示 `group_id + display_title`，再用 `member_asset_ids` 展开 catalog 资产。标题相似、共享日志或同属“留存”等宽泛主题都不能自动并组。

## 5. 外部工具复制范围

### 5.1 全量镜像

1. 复制 `asset_catalog.json`、`asset_organization.json` 和 `asset_group_registry.json`。
2. 遍历 `files`。
3. 对每个 `exists=true` 的记录，复制 `<source-repo>/<path>`。
4. 在目标环境保留相同的仓库相对目录结构。
5. 复制完成后重新计算 SHA-256，与目录值比较。
6. 校验全部通过后，再切换 Web 使用的新快照。

不要复制 catalog 没有声明的缓存、锁文件、凭据、临时构建目录或任何 `query_workspace/` 内容。

无需尝试寻找或恢复结果切片背后的完整原始数据。`result` 资产的 `facts.retention` 会给出原始大小、哈希、行数、字段和切片方式；`visualization`、`analysis_workbook`、`comparison_workbook` 的路径则必须完整复制，以便后续复用。

### 5.2 按资产复制

需要复制单个或一组资产时：

1. 从 `assets` 选择 `asset_id`。
2. 复制其全部 `file_paths`。
3. 根据需要遍历 `relationships`，追加结果、Dashboard、口径或知识数据依赖。
4. 从 `files` 获取每个路径的哈希并校验。

选择条件由外部产品决定，可以按项目、类型、日志、指标、状态、时间或全文搜索筛选。源 skill 不施加展示门槛。

## 6. 增量同步算法

外部环境保存上一次 catalog，并按文件 `path + sha256` 比较新快照：

| 情况 | 外部动作 |
|---|---|
| 新路径 | 复制文件并新增索引 |
| 路径相同、SHA-256 相同 | 跳过复制 |
| 路径相同、SHA-256 变化 | 复制到 staging，校验后替换 |
| 旧 catalog 存在、新 catalog 不存在 | 标记 `source_removed`；不要立即物理删除旧副本 |
| 新 catalog 中 `exists=false` | 保留资产和诊断，不伪造文件内容 |

资产级同步使用 `asset_id` 比较：

- 新 `asset_id`：新增资产；
- 同一 `asset_id` facts/state 改变：更新元数据；
- 旧 `asset_id` 消失：标记来源移除，保留上次快照供审计；
- 新版本通常拥有新的 `asset_id`，旧版本继续保留。

外部环境可以自行设置缓存保留期限，但不得把删除结果回写源仓库。

## 7. 原子同步

推荐使用快照目录：

```text
external-library/
  snapshots/
    <generated_at-or-commit>/
      asset_catalog.json
      sql-projects/
      knowledge-base/
  current -> snapshots/<latest-verified>/
```

同步顺序：

1. 创建 staging 快照。
2. 复制 catalog 和文件。
3. 校验全部 SHA-256。
4. 记录 Git transport 实际检出的 commit、Provider Snapshot 的 `generated_at` 和 `snapshot_digest`，以及 catalog generation provenance 中的 skill 版本。
5. 校验成功后原子切换 `current`。
6. 校验失败则继续使用上一份快照。

外部同步必须通过 Git transport 锁定一个已提交 commit，并从该 commit 的干净 checkout/export 构建目录；再把该 commit 与已校验的 `snapshot_digest` 配对保存。Provider v1 不包含 `source_control.commit` 或 `source_control.worktree_dirty`，消费者不得等待或伪造这些字段。Catalog 中的 `source_control` 只是生成时诊断信息，可能描述提交前的本地状态，不能替代消费者实际检出的 commit。

## 8. 外部 Web 建议读取方式

外部 Web 可以自行实现，但建议至少提供以下读取视图：

| 页面 | 主要数据 |
|---|---|
| 全部资产 | `assets`，按 project/kind/state/verification 筛选 |
| SQL 浏览 | SQL 资产的 `primary_path`、facts、关系和原始 SQL 文件 |
| SQL 执行版本 | `facts.execution_delivery` 的 exact engine/profile/route、显式 variant group 和推荐 exact asset |
| 查询家族 | temporary query 的版本、previous/next/branch/promoted 关系 |
| 结果与可视化 | result/workbook/visualization 资产及 `derived_from`/`evidence_for` |
| 可视化 Excel | 仅 `facts.consumer_surface=reusable_workbook`；显示 manifest 或无预览空态，并下载原始 XLSX |
| 口径 | rule concept、rule versions、current_definition 和 rule dictionary read model |
| 知识资料 | knowledge dataset、binding、projection CSV/schema/profile |
| 平台手册 | `documentation` 资产，按受众和文档类型筛选 |
| 集成契约 | `consumer_contract` 资产，定位 schema、taxonomy 和 reference |
| 资产详情 | 文件、SHA-256、provenance、所有上下游关系和 issues |

正式 SQL 页面可以优先读取 `sql_repository.json` 获得产品摘要，但仍应使用 asset catalog 定位原始文件和跨资产关系。

## 9. 安全与权限

- 外部工具对源仓库使用只读权限。
- 不提供任何回写 endpoint。
- 不复制 catalog 未声明的 `.env`、数据库密码、缓存、锁文件、本机临时目录或 `query_workspace/`。
- catalog 中只允许仓库相对路径；发现绝对路径应停止该文件复制并报告。
- SQL 中业务需要的标识按原始资产保存；隐私展示和访问控制由外部 Web 自己负责，不能改写源 SQL。
- 不根据文件内容自动修改 canonical rules、knowledge bindings 或生命周期状态。

## 10. 给接手 AI 的直接指令

可以把下面这段原样提供给另一个 AI：

```text
这是一个只读 SQL 资料库集成任务。源仓库不提供 API、服务端或前端，也不接受回写。

首先读取 sql-projects/_asset_catalog/asset_catalog.json，并按 sql-engineering/schemas/asset_catalog.json 理解结构。

再读取 asset_organization.json 和 asset_group_registry.json。主页按 asset_group_registry.groups 的 display_order 展示稳定 AG 编号；通过 member_asset_ids 连接 catalog。不要把 SQL vNNN 当成资产组编号，也不要在页面构建时按时间重新生成 AG 编号。

assets 是共享资产的全状态目录，formal draft、failed evidence、history、proposed、superseded 和 verified 都必须可见；状态只用于标签和筛选，不能作为隐藏或发布门槛。`visibility_policy.local_workspace_included=false` 且 `excluded_local_surfaces` 包含 `query_workspace`；消费者不得绕过目录扫描本地工作区。

files 中的 path 均相对源仓库根目录。复制 exists=true 的文件并校验 sha256。relationships 用于连接 SQL、版本、结果、Excel、可视化、Dashboard、Validation、口径和知识数据。不要扫描目录名猜关系，不要从 HTML 反向解析事实。

SQL 执行版本只读 assets[].facts.execution_delivery。没有 execution_route 的旧 SQL 显示未标注；只有显式且一致的 variant identity 才显示 StarRocks + Hive。不得按标题、tag、表名前缀、路径、SQL 文本或 branch_of 猜引擎、group 或推荐项。portable template 加单个物化 SQL 仍是单引擎。

可视化 Excel 只读 facts.consumer_surface=reusable_workbook 且 workbook_presentation.eligible=true。result evidence XLSX 和 HTML visualization 不进入。manifest 为空时显示 preview_status=not_available 并保留原始下载，不在线打开整本 Excel 生成伪预览。

增量同步按 path+sha256 判断文件变化，按 asset_id 判断资产变化。新 catalog 中消失的旧资产先标记 source_removed，不要立即物理删除。同步到 staging，哈希全部通过后再切换当前快照。

你负责在目标环境实现存储、搜索、API 和 Web；不得修改或回写源仓库资产。
```

## 11. 当前已知诊断

以最新本地快照为例，catalog 会把 knowledge catalog 指向但当前文件不存在的旧数据集版本记录为 `missing_asset_file`。这类诊断是源资产状态的一部分，外部页面应显示，不应导致其他资产同步失败。
