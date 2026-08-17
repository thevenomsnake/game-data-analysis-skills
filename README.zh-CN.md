# Game Data Analysis Skills

Game Data Analysis Skills 是一个面向 Codex 的公开 SQL 工作区。它把查询版本、业务规则、
证据和交付回执保存为可追溯的项目文件，让对话结束后仍然能继续维护。

本仓库不包含生产结果、私有表结构、凭据或组织内部服务配置，示例全部是虚构数据。

## 快速开始

需要 Python 3.11+ 和 Git；首次运行不需要第三方 Python 依赖。

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

刷新 Codex 后使用 `$sql-engineering`。不连接数据库也可以先跑通文件交付流程：

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "按日期统计虚构用户的活跃数量。" `
  --kind temporary `
  --slug daily-active-users
```

命令会生成不可变的 `v001.sql`、元数据和索引。交付前对返回路径运行 `receipt`。没有只读
执行适配器时，Skill 会返回准确的人工执行交接，不会虚报已经跑数。

## 内容范围

- 不可变 SQL 工作台和兼容的 `sql_workspace.py` 接口。
- 持续维护的项目、口径、资料、策划源、Review、验证、正式资产和结果关系模块。
- 只读本地执行适配器、健康检查和 receipt 校验。
- Excel 报告可视化工具源码，不带任何工作簿或报告数据。
- 虚构示例、schema、模板、测试和公开版维护工具。

## Setup 流程

可以使用 `$setup`，也可以直接运行：

```powershell
python .\setup\scripts\bootstrap_repo.py status --root .
python .\setup\scripts\bootstrap_repo.py demo --root .
```

Setup 只在本地工作，不需要 LDAP、GitLab、DA 网页控制台或生产数据库。

## 开发校验

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m unittest discover -s .\setup\scripts -p "test_*.py"
python .\tools\public_release.py validate --root .
```

详见 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 和
[docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md)。

本项目使用 Apache License 2.0。
