# Game Data Analysis Skills

**Codex를 위한 파일 기반 SQL 수명 주기와 설정 가능한 읽기 전용 데이터베이스 실행.**

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
| 프로젝트 자료 거버넌스 | 원본 텔레메트리, 기획 입력, 사람 확인 자료, 표준 규칙을 분리하여 버전 관리 |
| SQL 전달 | 생성하거나 수정할 때마다 변경 불가능한 `vNNN.sql` 버전으로 저장 |
| 환경별 실행 | 설정된 읽기 전용 DB-API 드라이버 또는 데이터베이스 CLI로 저장된 SQL 실행 |
| 외부 SQL 수집 | 전달받은 파일을 입력으로 취급하고 프로젝트 내부 복사본에서 작업 |
| 검색 가능한 이력 | 사람이 읽을 수 있는 제목, 목적, 태그, 방언, 경로, 콘텐츠 해시를 기록 |
| 수정 이력 관리 | 같은 분석 문제의 수정과 확장을 한 쿼리 패밀리에서 새 버전으로 관리 |
| 정확한 영수증 | 전달 전에 파일, 메타데이터, 색인, 현재 콘텐츠 해시를 검증 |
| 수명 주기 분류 | 임시 SQL, 재사용 SQL, 대시보드용 SQL을 구분 |

공개 사양판에는 회사별 스키마, 운영 테이블 이름, 자격 증명, 비공개 업무 규칙,
쿼리 결과 또는 내부 실행 환경 연동이 포함되지 않습니다.

## 프로젝트에 필요한 입력

| 필요한 정보 | Skill의 관리 방식 |
|---|---|
| 원본 텔레메트리 정의 | XML, JSON, YAML, Excel, CSV, 텍스트 등 원본 형식을 바꾸지 않고 `sources/raw/`에 복사하고 해시와 버전을 기록 |
| 데이터베이스와 SQL 방언 | 환경별 SQL 생성 방언을 선언하고 로컬 DB-API 또는 CLI 연결 정보는 Git 밖에서 관리 |
| 기획표와 설정표 | 원본을 `knowledge/planning/`에 보존하며, 자동으로 확정 규칙이 되지 않음 |
| 사람이 확인한 자료 | `knowledge/confirmed/`에 확인 버전, 확인자, 이유, 원본과의 관계를 저장 |
| 표준 업무 규칙 | 확인된 Base, 단위, 계산, 필터, 참조 자료를 `rules/definitions/`의 변경 불가능한 버전으로 저장 |

Skill은 이러한 프로젝트 사실을 만들어 내지 않습니다. 자료 소유권과 변경 이력을 보이게 하는 구조를
제공합니다. 전체 과정은 [프로젝트 온보딩 가이드](sql-engineering/references/project-onboarding.md)를 참조하세요.

## 프로젝트 생성 및 온보딩

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

`bootstrap`은 `sql-projects/example`과 비어 있는 텔레메트리, 지식, 규칙, SQL 카탈로그를 만듭니다.
다시 실행해도 등록된 내용을 삭제하지 않고 누락된 빈 구조만 복구합니다.

### 3. 프로젝트 자료 등록

원본 텔레메트리, 기획/설정표, 별도로 사람이 확인한 자료를 먼저 등록합니다. 그다음 데이터베이스 환경과
SQL 방언을 선언하고 사람이 명시적으로 확인한 규칙만 고정합니다.

```powershell
python .\sql-engineering\scripts\sql_workspace.py status `
  --root .\sql-projects\example
```

`query_context_ready=false`는 원본 텔레메트리 정의가 없다는 뜻입니다. 자동 연결이 없어도 되며,
이 경우 정확한 SQL 파일을 수동 실행용으로 전달합니다.

### 4. Codex에 자연어로 요청

```text
$sql-engineering 날짜별 고유 로그인 사용자 수를 집계하는 StarRocks 쿼리를 만들어 주세요.
고정 날짜 범위는 params CTE에 두고 example 프로젝트에 저장한 뒤 정확한 파일을 반환해 주세요.
```

Codex는 프로젝트를 확인하고 쿼리 패밀리를 만들거나 재사용한 뒤,
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql` 같은 버전을
저장해야 합니다. 이어서 receipt를 실행하고 저장된 절대 경로를 반환합니다. 데이터베이스 실행은
별도로 보고해야 하며, 실행했다고 추정해서는 안 됩니다.

자동 실행은 선택 기능입니다. 프로젝트에는 환경 이름만 등록하고 실제 연결 설정은 Git에서 제외되는
`.sql-engineering/connections.local.json`에 저장합니다. 드라이버, CLI, 비밀값 또는 연결 프로필이
없으면 Skill은 `manual_required`와 정확한 SQL 경로를 반환하고, 사용자에게 직접 실행한 결과 파일을
보내 달라고 요청합니다. Chrome이나 DA 웹 콘솔을 조작하지 않습니다.

## 자주 쓰는 요청

| 목적 | 요청 예시 |
|---|---|
| 프로젝트 생성 | `$sql-engineering StarRocks용 alpha 프로젝트를 만들고 부족한 텔레메트리, 자료, 규칙, 연결 설정을 알려 주세요.` |
| 텔레메트리 등록 | `$sql-engineering 이 XML을 PlayerLogin 원본 텔레메트리 정의로 변경 없이 등록해 주세요.` |
| 기획 증거 등록 | `$sql-engineering 이 모드 설정 워크북을 기획 입력으로 저장하고 확정 규칙으로 취급하지 마세요.` |
| 규칙 고정 | `$sql-engineering 사람이 확인한 일간 활성 사용자 정의를 새 표준 규칙 버전으로 고정해 주세요.` |
| SQL 생성 | `$sql-engineering 이 프로젝트의 일간 활성 사용자 쿼리를 만들고 저장해 주세요.` |
| 외부 SQL 수정 | `$sql-engineering 이 SQL을 가져와 프로젝트 방언에 맞게 고치고 원본은 덮어쓰지 마세요.` |
| 이전 작업 검색 | `$sql-engineering 리텐션 관련 저장 쿼리를 찾아 목적을 요약해 주세요.` |
| 쿼리 확장 | `$sql-engineering 기존 활성 사용자 쿼리 패밀리에 플랫폼 차원을 추가해 주세요.` |
| 유용한 쿼리 보관 | `$sql-engineering 확인된 로직을 retained 쿼리 버전으로 저장해 주세요.` |
| 전달 검증 | `$sql-engineering 이 v003.sql의 receipt를 확인하고 정확한 경로를 반환해 주세요.` |
| 직접 실행 | `$sql-engineering 설정된 개발 데이터베이스에서 이 저장 쿼리를 실행해 주세요.` |

## 수명 주기

```text
요청
  -> 원본 텔레메트리 등록
  -> 기획 자료와 사람이 확인한 자료 분리
  -> 적용할 표준 규칙 고정
  -> 데이터베이스 환경과 SQL 방언 선택
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
    sources/
      source-catalog.json
      raw/<source>/vNNN.*      변경하지 않은 원본 텔레메트리 정의
    knowledge/
      planning/<item>/vNNN.*   원본 기획표와 설정표
      confirmed/<item>/vNNN.* 사람이 확인한 자료
    rules/
      definitions/<rule>/vNNN.json
    context/                    비권위 메모와 플랫폼 문서
    sql-workspace/
      index.json               기계가 검색할 수 있는 색인
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

밑줄로 시작하는 디렉터리는 프로젝트 간 확장 지점입니다. 프로젝트 안에서는 원본 증거, 사람의 확인,
표준 규칙, 실행 SQL을 분리하여 서로 조용히 대체되지 않게 합니다.

## 명령어 안내

| 명령어 | 용도 |
|---|---|
| `bootstrap` | 저장소 구조를 만들고 선택적으로 첫 프로젝트를 초기화 |
| `init` | 독립 프로젝트 하나를 초기화 |
| `environment` | 프로젝트 환경 이름을 로컬 데이터베이스 연결 프로필에 연결 |
| `source` | 원본 형식을 바꾸지 않고 텔레메트리 정의를 복사하고 등록 |
| `knowledge` | 기획 입력 또는 사람이 확인한 자료를 등록 |
| `rule` | 명시적으로 확인된 업무 규칙을 새 변경 불가능한 버전으로 고정 |
| `status` | 부족한 소스, 자료, 규칙, 실행 설정을 표시 |
| `save` | 변경 불가능한 새 SQL 버전을 저장하고 색인을 갱신 |
| `search` | 제목, 요약, 태그 검색 |
| `receipt` | 전달 전에 특정 SQL 버전을 검증 |
| `sql_execute.py run` | 저장된 읽기 전용 SQL을 실행하거나 수동 실행으로 인계 |

포함된 가상 쿼리
[`daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql)을 사용해 볼 수 있습니다.
[에이전트 실행 예제](sql-engineering/references/example.md)는 요청, 명령, 예상 파일, 최종 응답
계약을 보여 줍니다.

## 설계 경계

- 프로젝트 설정이 SQL 방언을 선택합니다. Skill은 테이블, 파티션, 업무 ID, 지표 정의를 추측하지 않습니다.
- 프로젝트 컨텍스트는 선택 사항이며 명시적으로 선언합니다. Skill은 개인 지식 저장소에 의존하지 않고,
  부족한 스키마 정보는 저장된 읽기 전용 데이터베이스 쿼리로 확인할 수 있습니다.
- 자동 실행은 DB-API 또는 데이터베이스 명령줄 클라이언트만 사용합니다. 브라우저와 DA 웹 콘솔 자동화는
  지원하지 않으며, 설정이 없으면 수동 실행으로 전환합니다.
- 외부 SQL은 변경 불가능한 입력으로 유지하며, 수정본은 프로젝트 내부에 저장합니다.
- 저장된 버전은 덮어쓰지 않습니다. 수동 수정은 receipt의 해시 검사로 감지합니다.
- 수명 주기 라벨은 의도한 용도를 설명할 뿐, 업무 정확성이나 실행 성공을 증명하지 않습니다.
- 결과, 시각화, 검증, 대시보드는 관리되는 확장으로 추가할 수 있지만 SQL 파일만 보고 조용히 추론하지 않습니다.
- 자격 증명, 비공개 스키마, 운영 결과, 로컬 절대 경로를 커밋하면 안 됩니다.

## 문서

| 주제 | 문서 |
|---|---|
| 에이전트 워크플로와 필수 경계 | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| 새 프로젝트 입력과 온보딩 과정 | [`references/project-onboarding.md`](sql-engineering/references/project-onboarding.md) |
| 전체 실행 예제 | [`references/example.md`](sql-engineering/references/example.md) |
| 프로젝트 및 디렉터리 계약 | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| 쿼리 패밀리 수명 주기 | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL 전달 검사 | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| 데이터베이스 환경 및 실행 | [`references/database-execution.md`](sql-engineering/references/database-execution.md) |
| 연결 방식과 SQL 방언 | [`references/dialects.md`](sql-engineering/references/dialects.md) |
| 기여 규칙 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 보안 정책 | [SECURITY.md](SECURITY.md) |

## 개발

공개 코어는 Python 표준 라이브러리만 사용합니다. DB-API 실행 시에는 사용자의 로컬 연결 프로필에서
선택한 데이터베이스 드라이버를 불러옵니다.

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py .\sql-engineering\scripts\sql_execute.py
```

[Apache License 2.0](LICENSE)에 따라 배포됩니다.
