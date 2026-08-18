# Game Data Analysis Skills

**Codex에서 게임 데이터 분석을 수행하기 위한, 교체 가능한 Skills 모음입니다.**

[공식 사이트](https://fairy.sumimi.jp/) · [English](README.md) · [简体中文](README.zh-CN.md)

Game Data Analysis Skills는 각각 독립적으로 사용하거나 필요한 것만 조합할 수 있는 Skills의
컬렉션입니다. Fairy를 사용하지 않고도 이용할 수 있습니다. Fairy는 선택한 Skills를 팀 워크플로로
묶는 별도의 제품 계층이며 이 저장소의 필수 의존성이 아닙니다.

## 제공하는 기능

| 모듈 | 담당하는 일 |
| --- | --- |
| **Setup** | Git을 기본으로 GitHub, GitLab, 자체 호스팅 Git, SSH, 로컬 Git과 Git/SVN/로컬/none 기획 소스를 설정합니다. |
| **SQL 작업 공간** | 쿼리를 변경 불가능하고 검색 가능한 버전으로 저장하며 메타데이터, 해시, 정확한 receipt를 남깁니다. |
| **규칙과 지식** | 원시 이벤트 정의, 기획 입력, 확인된 참고 자료, canonical rules를 분리해 추적합니다. |
| **쿼리 수명 주기** | 요구사항, QUERY, 검증, 정식 자산 패키지, Dashboard 파생물을 증거와 함께 연결합니다. |
| **Review와 health** | 제품 의미와 SQL 구조를 함께 확인하고 드리프트를 조기에 찾습니다. |
| **결과와 lineage** | 결과, 시각화, 워크북을 실제로 만든 SQL 버전에 연결합니다. |
| **실행 표면** | receipt가 준비된 SQL을 DB-API/CLI, 웹 어댑터 또는 명시적 수동 전달로 실행합니다. |
| **Excel 보고서 시각화** | 로컬 워크북을 검사하고 오프라인에서 재사용할 보고서를 만듭니다. 실제 워크북은 포함하지 않습니다. |

## 설치하고 첫 쿼리 실행하기

Python 3.11 이상과 Git이 필요합니다. 첫 실행에는 추가 Python 패키지가 필요하지 않습니다.

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills

python .\setup\scripts\bootstrap_repo.py configure `
  --root . `
  --remote https://github.com/thevenomsnake/game-data-analysis-skills.git `
  --planning-provider none
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Codex를 새로고침한 뒤 `$sql-engineering`을 사용하세요. 데이터베이스 없이도 파일 기반 흐름을
확인할 수 있습니다.

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "날짜별 가상 사용자의 활성 수를 집계합니다." `
  --kind temporary `
  --slug daily-active-users
```

반환된 `v001.sql`에 `receipt`를 실행한 뒤 공유하세요. 실행 표면이 없으면 `manual_required`가
반환되며, 생성한 SQL을 실행했다고 주장하지 않습니다.

## 실행 표면 초기화

정식 프로젝트를 만들 때 실행 의도를 함께 지정할 수 있습니다.

```powershell
python .\sql-engineering\scripts\local_setup.py init `
  --repo-root . `
  --project example `
  --execution-surface web
```

- `direct`: 로컬 읽기 전용 DB-API 또는 CLI 프로필.
- `web`: 무시되는 `web_query_adapter_v1`와 사용자의 Chrome 세션.
- `manual`: 정확한 SQL 파일을 사용자에게 전달하는 방식.

현재 Deltaverse 예제를 제공합니다. 다른 웹사이트는 [실행 표면 및 웹 어댑터 가이드](sql-engineering/references/execution-surfaces.md)에
따라 URL, UI locator, 완료 신호, 다운로드 경로만 로컬 설정에 추가하세요. 로그인 자동화나 Cookie
저장은 하지 않습니다.

## SQL과 자산의 위치

- `sql-projects/<project>/query_workspace/`: 임시 및 이력 SQL. `sql_query_workspace.py search`로 찾으며 Git에 넣지 않습니다.
- `sql-projects/<project>/formal_assets/`: 정식 SQL, 결과, 검증, Dashboard 패키지. `sql_repository.py build|serve`로 읽기 전용 목록을 만듭니다.
- Provider Snapshot, Catalog schema, receipt: 외부 읽기 전용 소비자를 위한 안정적인 자산 인터페이스입니다.

## 기획 소스 선택

저장소의 Git remote와 기획 소스는 별도로 설정합니다.

```powershell
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider git `
  --planning-url <git-planning-url> `
  --planning-branch main `
  --planning-id planning
python .\setup\scripts\bootstrap_repo.py planning-sync --root .

python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider svn `
  --planning-url <svn-url> `
  --planning-revision <revision>

python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider local `
  --planning-path <folder>
```

아직 준비되지 않았다면 `--planning-provider none`을 사용하세요. 인증 정보는 Git/SVN의 로컬 자격
증명 메커니즘에 맡기며 공개 설정에 저장하지 않습니다.

## 안전과 라이선스

공개 트리에는 가상 예제만 포함하며 운영 SQL, 결과, 비공개 스키마와 자격 증명은 포함하지 않습니다.
외부 SQL은 입력으로 보존하고 원본을 덮어쓰지 않습니다. Excel 내장 라이브러리는
[THIRD_PARTY_NOTICES.md](excel-report-visualizer/THIRD_PARTY_NOTICES.md)에 기록되어 있습니다.

## 더 읽기

- [Setup onboarding](setup/references/onboarding.md)
- [SQL Engineering contract](sql-engineering/SKILL.md)
- [User manual](docs/USER_MANUAL.md)
- [Read-only asset consumer guide](docs/READONLY_ASSET_CONSUMER_GUIDE.md)
- [Public maintenance](docs/PUBLIC_MAINTENANCE.md)
- [Excel report visualizer](excel-report-visualizer/README.md)

## 다음 계획

- 일정에 따른 정기 보고서 생성.
- 데이터 자산 간 결과 비교.
- 이상 발생 원인 추적과 조사.

Apache License 2.0으로 배포합니다.
