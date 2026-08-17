# Game Data Analysis Skills

로컬에서 사용할 수 있는 공개 Codex SQL Skill입니다. [README.md](README.md) 또는
[README.zh-CN.md](README.zh-CN.md)를 읽고 다음을 실행하세요.

```powershell
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

허구 예제와 일반 도구만 포함하며 `BetterXml`, 운영 결과, 사설 스키마와 자격 증명은 포함하지 않습니다.
