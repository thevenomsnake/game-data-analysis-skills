# Game Data Analysis Skills

**面向 Codex 的可插拔游戏数据分析 Skills 合集，把 SQL、口径、证据和交付文件真正保存下来。**

[English](README.md) · 简体中文 · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md)

聊天里的查询很容易散掉：SQL 版本变了，口径来源找不到，结果也无法确认对应哪一版。这个
项目把这些关系放进文件和索引里，让一次排查可以继续演进成可复用的分析资产，同时保留
临时查询应有的轻量感。每个 Skill 都可以单独使用，也可以组合多个；调用方可以把选中的能力
接入自己的 AI 或工作流，不需要依赖某个产品层。

## 各模块负责什么

| 模块 | 解决的问题 |
| --- | --- |
| **Setup** | 安装时以 Git 为基础，配置 GitHub、GitLab、自建 Git、SSH 或本地 Git，并选择 Git、SVN、本地目录或暂不配置策划源。 |
| **SQL 工作台** | 每条查询保存为不可变、可检索的版本，附带元数据、内容哈希和精确交付 receipt。 |
| **口径与资料** | 分开管理原始埋点定义、策划输入、人工确认资料和 canonical rule，保留来源关系。 |
| **查询生命周期** | 从需求判定进入 QUERY，再到验证、正式资产包和看板派生；每一步都保留证据边界。 |
| **Review 与健康检查** | 同时检查业务含义和 SQL 结构，用确定性事实、质量门禁和健康检查尽早发现漂移。 |
| **结果与 lineage** | 把结果、可视化和工作簿绑定到实际产生它们的准确 SQL 版本。 |
| **执行面** | 对已生成 receipt 的 SQL 选择直接 DB-API/CLI、配置好的网页适配器，或明确的手动交付。 |
| **Excel 报告可视化** | 在本地检查约定格式的工作簿，生成可离线复用的报告页面；仓库只提供工具源码，不带任何工作簿。 |

## 安装并跑通第一条查询

需要 Python 3.11+ 和 Git；首次运行不需要第三方 Python 依赖。

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills

# Git 是默认传输方式；其他 Git 托管平台只需要替换 remote。
# 下一段命令会完成 provider 配置和 demo 初始化。
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

刷新 Codex 后使用 `$sql-engineering`。不连接数据库也能先跑通文件交付：

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "按日期统计虚构用户的活跃数量。" `
  --kind temporary `
  --slug daily-active-users
```

命令会返回不可变的 `v001.sql` 路径。交付前对准确路径运行 `receipt`。没有数据库适配器时，
执行结果会明确返回 `manual_required`，不会把“已生成”说成“已跑通”。

## 选择查询执行面

正式项目初始化时明确执行意图：

```powershell
python .\sql-engineering\scripts\local_setup.py init `
  --repo-root . `
  --project example `
  --execution-surface direct
```

`direct` 使用只读 DB-API 或 CLI；`web` 使用项目本地网页适配器和用户自己的 Chrome 登录态；
`manual` 表示暂不配置自动执行。当前提供 Deltaverse 网页适配示例，其他网站按同一版本化契约
和适配指南接入。三种执行面不会静默互相切换。

合集也提供稳定的文件资产接口：Query Workspace 索引临时 SQL，Formal Asset Package manifest
保存共享 SQL 和派生资产，receipt 绑定准确版本，Provider Snapshot/Catalog schema 为只读消费方
提供稳定身份和哈希。详见[执行面与网页适配指南](sql-engineering/references/execution-surfaces.md)
和[只读资产消费手册](docs/READONLY_ASSET_CONSUMER_GUIDE.md)。

## 两种接入接口

- **Codex Skill 接口**：安装 `setup` 和 `sql-engineering`，刷新 Codex 后使用 `$sql-engineering`，
  由 Skill 负责路由和引导项目工作。
- **外部 AI Agent / 第三方软件接口**：直接调用 JSON CLI，或读取约定的文件和 schema；不需要
  安装 Codex 运行时。

完整的命令与文件契约见[接入接口说明](docs/INTEGRATION_INTERFACES.md)。

## 策划源怎么选

仓库 Git remote 和策划源是两件事，分别配置：

```powershell
# Git 策划源
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider git `
  --planning-url <git-planning-url> `
  --planning-branch main `
  --planning-id planning
python .\setup\scripts\bootstrap_repo.py planning-sync --root .

# SVN 策划源
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider svn `
  --planning-url <svn-url> `
  --planning-revision <revision>

# 用户自己维护的本地目录
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider local `
  --planning-path <folder>
```

项目还没准备好时使用 `--planning-provider none`。provider、URL、分支、revision 和本地
checkout 信息保存在被忽略的 `.local/`；密码和 token 由 Git/SVN 自己的本地凭据机制管理，
不会写入配置文件。

## 从问题到交付

```text
问题
  -> 查找需求、来源和口径
  -> 保存一个版本化工作台查询
  -> 校验准确 receipt
  -> 可选：通过只读适配器执行并绑定结果
  -> Review 与验证
  -> 明确固化为可复用资产或看板派生
```

执行状态、结果展示和资产长期价值彼此独立。有结果不等于自动固化，生命周期标签也不替代
正确性证据。

## 安全边界

- 公开仓库只带虚构示例，不带生产 SQL、结果、私有表结构或凭据。
- 外部 SQL 作为输入保存，不会覆盖原文件。
- 自动执行必须是只读；可选浏览器执行只消费准确 receipt，并通过 Chrome 插件使用用户自己的登录态。
- `tools/public_release.py` 会扫描公开树，并可生成本地 SHA-256 清单。

## 继续阅读

- [Setup 接入手册](setup/references/onboarding.md)
- [SQL Engineering 合约](sql-engineering/SKILL.md)
- [项目总览](docs/PROJECT_OVERVIEW.md)
- [用户手册](docs/USER_MANUAL.md)
- [策划源 provider](sql-engineering/references/planning-source.md)
- [直接连接、网页查询与手动交付](sql-engineering/references/execution-surfaces.md)
- [公开维护边界](docs/PUBLIC_MAINTENANCE.md)
- [Excel 报告可视化](excel-report-visualizer/README.md)
- [Excel 第三方许可说明](excel-report-visualizer/THIRD_PARTY_NOTICES.md)

## 下一步

- 按设定周期自动生成报告。
- 跨资产对比数据结果，检查结果是否合理。
- 自动追溯异常来源并排查可能原因。

本项目使用 Apache License 2.0。
