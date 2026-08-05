# SQL Engineering Skill

这是一个面向 Codex 的轻量 SQL 工程化 Skill，用来保证对话中生成或修改的 SQL
能够落盘、版本化、检索，并准确交付到具体文件。

## 核心能力

- 每条生成或修改后的 SQL 都保存为不可变的 `vNNN.sql`。
- 每个版本都有标题、简要用途、内容哈希和索引。
- 外部 SQL 只作为输入，不在原位置修改。
- 交付前生成精确回执，校验文件路径和内容哈希。
- 临时查询、可复用查询和看板 SQL 使用不同生命周期状态。

公共规范版不包含任何公司的项目配置、生产表名、业务口径、凭据、查询结果或内部
执行平台集成。GitHub 仓库可以继续保持 PRIVATE，但内容始终按未来可公开的标准校验。
它和私有分析工作区分别维护版本和 Git 历史，不是内部仓库镜像。

## 安装

克隆仓库后，将 `sql-engineering/` 复制或链接到 Codex Skills 目录：

```powershell
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

刷新 Codex 后，在任务里调用 `$sql-engineering`。

## 初始化项目

```powershell
python .\sql-engineering\scripts\sql_workspace.py init `
  --root .\example-project `
  --project-id example `
  --dialect starrocks
```

保存 SQL：

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\example-project `
  --sql-file .\query.sql `
  --title "每日活跃用户" `
  --summary "按日期统计去重活跃用户数。" `
  --kind temporary
```

完整的 AI 执行约束见 [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md)。

## 开发校验

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py
```

本项目使用 [Apache License 2.0](LICENSE)。
