# Game Data Analysis Skills

ローカルで利用できる公開版 Codex SQL Skill です。詳細は [README.md](README.md) または
[README.zh-CN.md](README.zh-CN.md) を参照し、次を実行してください。

```powershell
python .\setup\scripts\bootstrap_repo.py demo --root .
python .\setup\scripts\bootstrap_repo.py configure --root . --planning-provider none
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

含まれるのは架空の例と汎用ツールだけで、`BetterXml`、本番結果、私有スキーマ、認証情報は含みません。
