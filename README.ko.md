# Game Data Analysis Skills

**Codex를 위한 파일 기반 SQL 수명 주기.**

Game Data Analysis Skills는 대화에서 만든 SQL 작업을 오래 유지되는 프로젝트 파일로 바꿉니다.
생성하거나 수정한 모든 쿼리를 저장하고, 버전 관리하고, 색인화하여 검색할 수 있게 하며, 정확한
경로로 전달합니다. 대화가 끝난 뒤에도 작업 내용을 이해하고 계속 수정할 수 있습니다.

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [Español](README.es.md)

> 채팅의 SQL 코드 블록은 설명입니다. 검증된 `vNNN.sql` 파일이 실제 결과물입니다.

## 해결하는 문제

채팅에서 만든 SQL은 쉽게 사라집니다. 유용한 쿼리를 다른 파일로 복사한 뒤 이력 없이 수정하거나,
용도 설명 및 결과와의 연결이 끊어지는 경우가 많습니다. 나중에는 어떤 버전이 결과를 만들었는지,
외부 원본 파일을 덮어썼는지 알기 어렵습니다.

이 Skill은 Codex에 작고 명확하며 검증 가능한 작업 공간 계약을 제공합니다.

| 기능 | 실제 동작 |
|---|---|
| 저장소 초기화 | 안정적인 `sql-projects/` 구조와 첫 프로젝트를 생성 |
| SQL 전달 | 생성하거나 수정할 때마다 변경 불가능한 `vNNN.sql` 버전으로 저장 |
| 외부 SQL 수집 | 전달받은 파일을 입력으로 취급하고 프로젝트 내부 복사본에서 작업 |
| 검색 가능한 이력 | 사람이 읽을 수 있는 제목, 목적, 태그, 방언, 경로, 콘텐츠 해시를 기록 |
| 수정 이력 관리 | 같은 분석 문제의 수정과 확장을 한 쿼리 패밀리에서 새 버전으로 관리 |
| 정확한 영수증 | 전달 전에 파일, 메타데이터, 색인, 현재 콘텐츠 해시를 검증 |
| 수명 주기 분류 | 임시 SQL, 재사용 SQL, 대시보드용 SQL을 구분 |

공개 사양판에는 회사별 스키마, 운영 테이블 이름, 자격 증명, 비공개 업무 규칙,
쿼리 결과 또는 내부 실행 환경 연동이 포함되지 않습니다.

## 3분 안에 시작하기

### 1. Skill 설치

이 저장소를 복제한 다음 `sql-engineering/`을 Codex Skills 디렉터리에 복사하거나 연결합니다.

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Codex를 다시 시작하거나 새로 고치면 `$sql-engineering`으로 호출할 수 있습니다.

### 2. 작업 공간 초기화

```powershell
python .\sql-engineering\scripts\sql_workspace.py bootstrap `
  --root . `
  --project-id example `
  --dialect starrocks
```

저장소에는 공유 `_asset_catalog`, `_review_inbox`, `_rule_review` 디렉터리 골격이 포함되어
있습니다. `bootstrap`은 누락된 디렉터리를 복구하고 `sql-projects/example`을 초기화합니다.
다시 실행해도 기존 내용은 삭제하지 않습니다.

### 3. Codex에 자연어로 요청

```text
$sql-engineering 날짜별 고유 로그인 사용자 수를 집계하는 StarRocks 쿼리를 만들어 주세요.
고정 날짜 범위는 params CTE에 두고 example 프로젝트에 저장한 뒤 정확한 파일을 반환해 주세요.
```

Codex는 프로젝트를 확인하고 쿼리 패밀리를 만들거나 재사용한 뒤,
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql` 같은 버전을
저장해야 합니다. 이어서 receipt를 실행하고 저장된 절대 경로를 반환합니다. 데이터베이스 실행은
별도로 보고해야 하며, 실행했다고 추정해서는 안 됩니다.

## 자주 쓰는 요청

| 목적 | 요청 예시 |
|---|---|
| SQL 생성 | `$sql-engineering 이 프로젝트의 일간 활성 사용자 쿼리를 만들고 저장해 주세요.` |
| 외부 SQL 수정 | `$sql-engineering 이 SQL을 가져와 프로젝트 방언에 맞게 고치고 원본은 덮어쓰지 마세요.` |
| 이전 작업 검색 | `$sql-engineering 리텐션 관련 저장 쿼리를 찾아 목적을 요약해 주세요.` |
| 쿼리 확장 | `$sql-engineering 기존 활성 사용자 쿼리 패밀리에 플랫폼 차원을 추가해 주세요.` |
| 유용한 쿼리 보관 | `$sql-engineering 확인된 로직을 retained 쿼리 버전으로 저장해 주세요.` |
| 전달 검증 | `$sql-engineering 이 v003.sql의 receipt를 확인하고 정확한 경로를 반환해 주세요.` |

## 수명 주기

```text
요청
  -> 프로젝트와 SQL 방언 확인
  -> 임시 SQL 버전 저장
  -> 사용자 환경에서 실행
  -> 수정 또는 확장을 다음 버전으로 저장
  -> 필요하면 retained 또는 dashboard용 버전으로 승격
  -> 정확한 delivery receipt
```

하나의 쿼리 패밀리는 하나의 분석 질문을 나타냅니다. 날짜 갱신, 구문 수정, 같은 문제를 완전히
포함하는 확장은 해당 패밀리의 새 버전으로 유지합니다. Base, 핵심 지표 또는 지원할 의사결정이
달라지면 새 패밀리를 시작합니다.

## 작업 공간 구조

```text
sql-projects/
  _asset_catalog/              프로젝트 간 검색을 위한 확장 영역
  _review_inbox/               수집 또는 검토 대기 중인 외부 SQL과 증거
  _rule_review/                규칙 검토를 위한 확장 영역
  example/
    .sql-engineering/
      project.json             프로젝트 식별자와 SQL 방언
    sql-workspace/
      index.json               기계가 검색할 수 있는 색인
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

밑줄로 시작하는 세 디렉터리는 안정적인 확장 지점입니다. 공개 코어는 디렉터리를 만들지만,
카탈로그, 검토 또는 규칙 내용을 임의로 만들지 않습니다.

## 명령어 안내

| 명령어 | 용도 |
|---|---|
| `bootstrap` | 저장소 구조를 만들고 선택적으로 첫 프로젝트를 초기화 |
| `init` | 독립 프로젝트 하나를 초기화 |
| `save` | 변경 불가능한 새 SQL 버전을 저장하고 색인을 갱신 |
| `search` | 제목, 요약, 태그 검색 |
| `receipt` | 전달 전에 특정 SQL 버전을 검증 |

포함된 가상 쿼리
[`daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql)을 사용해 볼 수 있습니다.
[에이전트 실행 예제](sql-engineering/references/example.md)는 요청, 명령, 예상 파일, 최종 응답
계약을 보여 줍니다.

## 설계 경계

- 프로젝트 설정이 SQL 방언을 선택합니다. Skill은 테이블, 파티션, 업무 ID, 지표 정의를 추측하지 않습니다.
- 외부 SQL은 변경 불가능한 입력으로 유지하며, 수정본은 프로젝트 내부에 저장합니다.
- 저장된 버전은 덮어쓰지 않습니다. 수동 수정은 receipt의 해시 검사로 감지합니다.
- 수명 주기 라벨은 의도한 용도를 설명할 뿐, 업무 정확성이나 실행 성공을 증명하지 않습니다.
- 결과, 시각화, 검증, 대시보드는 관리되는 확장으로 추가할 수 있지만 SQL 파일만 보고 조용히 추론하지 않습니다.
- 자격 증명, 비공개 스키마, 운영 결과, 로컬 절대 경로를 커밋하면 안 됩니다.

## 문서

| 주제 | 문서 |
|---|---|
| 에이전트 워크플로와 필수 경계 | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| 전체 실행 예제 | [`references/example.md`](sql-engineering/references/example.md) |
| 프로젝트 및 디렉터리 계약 | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| 쿼리 패밀리 수명 주기 | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL 전달 검사 | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| 기여 규칙 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 보안 정책 | [SECURITY.md](SECURITY.md) |

## 개발

공개판은 Python 표준 라이브러리만 사용합니다.

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py
```

[Apache License 2.0](LICENSE)에 따라 배포됩니다.
