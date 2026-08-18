# Game Data Analysis Skills

**Codex でゲームデータ分析を行うための、差し替え可能な Skills のコレクションです。**

[English](README.md) · [简体中文](README.zh-CN.md)

Game Data Analysis Skills は、個別に使うことも、必要なものだけ組み合わせることもできる
Skills の集合です。選んだ能力を別の AI やツールのワークフローへ組み込んで利用できます。
特定のプロダクト層を前提にしていません。

## できること

| モジュール | 扱うこと |
| --- | --- |
| **Setup** | Git を基本に、GitHub、GitLab、自ホスト Git、SSH、ローカル Git と、Git/SVN/ローカル/none の企画ソースを設定します。 |
| **SQL ワークスペース** | クエリを不変で検索可能なバージョンとして保存し、メタデータ、ハッシュ、正確な receipt を残します。 |
| **ルールと知識** | 生のイベント定義、企画入力、確認済み資料、canonical rules を分離して管理します。 |
| **クエリのライフサイクル** | 要件、QUERY、検証、正式なアセットパッケージ、Dashboard 派生物を証跡付きでつなぎます。 |
| **Review と health** | プロダクトの意味と SQL 構造を確認し、ドリフトを早く見つけます。 |
| **結果と lineage** | 結果、可視化、ワークブックを生成元の正確な SQL バージョンに結び付けます。 |
| **実行面** | receipt 済み SQL を DB-API/CLI、設定済み Web アダプター、または手動引き渡しで実行します。 |
| **Excel レポート可視化** | ローカルの対応ワークブックを検査し、オフラインで再利用できるレポートを作ります。 |

## インストールして最初のクエリを試す

必要なのは Python 3.11 以上と Git です。初回実行に追加の Python パッケージは必要ありません。

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

Codex を再読み込みして `$sql-engineering` を使います。データベースなしでもファイルの流れを
確認できます。

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "日付ごとの架空ユーザー数を集計します。" `
  --kind temporary `
  --slug daily-active-users
```

返された `v001.sql` に対して `receipt` を実行してから共有してください。実行面がない場合は
`manual_required` になり、生成しただけの SQL を実行済みとは扱いません。

## 実行面を初期化する

正式プロジェクトの実行意図を初期化時に選べます。

```powershell
python .\sql-engineering\scripts\local_setup.py init `
  --repo-root . `
  --project example `
  --execution-surface web
```

- `direct` はローカルの読み取り専用 DB-API または CLI。
- `web` は無視対象の `web_query_adapter_v1` と、ユーザー自身の Chrome セッション。
- `manual` は SQL ファイルをユーザーへ渡す運用です。

現在は Deltaverse の例を同梱しています。別の Web サイトは
[実行面と Web アダプターのガイド](sql-engineering/references/execution-surfaces.md)に沿って、
URL、UI の locator、完了条件、ダウンロード経路だけをローカル設定に追加します。自動ログインや
Cookie の保存は行いません。

## SQL とアセットの場所

- `sql-projects/<project>/query_workspace/` は一時・履歴 SQL です。`sql_query_workspace.py search`
  で検索でき、Git には入りません。
- `sql-projects/<project>/formal_assets/` は正式 SQL、結果、検証、Dashboard の共有パッケージです。
  `sql_repository.py build|serve` で読み取り専用の一覧を作れます。
- Provider Snapshot、Catalog schema、receipt は外部の読み取り専用ツール向けの安定した資産インターフェースです。

## 企画ソースを選ぶ

リポジトリの Git remote と企画ソースは別々に設定します。

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

準備ができていない場合は `--planning-provider none` を使います。認証情報は Git/SVN のローカル
機構に任せ、公開設定には保存しません。

## 安全とライセンス

公開ツリーには架空の例だけを含め、プロダクション SQL、結果、非公開スキーマ、認証情報は含めません。
外部 SQL は入力として不変保存します。Excel の埋め込みライブラリについては
[THIRD_PARTY_NOTICES.md](excel-report-visualizer/THIRD_PARTY_NOTICES.md)を参照してください。

## さらに読む

- [Setup onboarding](setup/references/onboarding.md)
- [SQL Engineering contract](sql-engineering/SKILL.md)
- [User manual](docs/USER_MANUAL.md)
- [Read-only asset consumer guide](docs/READONLY_ASSET_CONSUMER_GUIDE.md)
- [Public maintenance](docs/PUBLIC_MAINTENANCE.md)
- [Excel report visualizer](excel-report-visualizer/README.md)

## 今後の予定

- スケジュールに沿った定期レポート。
- データアセット間の結果比較。
- 異常の発生源の追跡と原因調査。

Apache License 2.0 の下で公開しています。
