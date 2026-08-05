# Game Data Analysis Skills

**Codex のための、設定可能な読み取り専用データベース実行を備えたファイル主体の SQL ライフサイクル。**

Game Data Analysis Skills は、会話の中で生まれた SQL を永続的なプロジェクトファイルへ変換します。
生成または変更されたすべてのクエリを保存し、バージョン管理し、索引化して検索可能にし、正確な
パスで引き渡します。会話が終わった後でも、作業内容を理解して継続できます。

[English](README.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Español](README.es.md) · [한국어](README.ko.md)

> チャット内の SQL コードブロックは説明です。検証済みの `vNNN.sql` ファイルが成果物です。

## 解決する課題

チャットで作成した SQL は簡単に失われます。有用なクエリが別ファイルへコピーされ、履歴なしで
編集されたり、用途の説明や結果との対応関係が失われたりします。外部から渡された SQL が直接
上書きされることもあります。時間が経つと、どのバージョンが結果を生成したのか分からなくなります。

この Skill は Codex に、小さく明確で強制可能なワークスペース契約を与えます。

| 機能 | 実際の動作 |
|---|---|
| リポジトリ初期化 | 安定した `sql-projects/` 構造と最初のプロジェクトを作成 |
| プロジェクト資料の統制 | 元のテレメトリ、企画入力、人が確認した資料、正式ルールを分けてバージョン管理 |
| SQL の引き渡し | 生成または変更のたびに、不変の `vNNN.sql` バージョンとして保存 |
| 環境別の実行 | 設定済みの読み取り専用 DB-API ドライバーまたはデータベース CLI で保存済み SQL を実行 |
| 外部 SQL の取り込み | 入力ファイルを変更せず、プロジェクト内のコピーで作業 |
| 検索可能な履歴 | 人が読めるタイトル、目的、タグ、方言、パス、内容ハッシュを記録 |
| 継続的な改訂 | 同じ分析課題を一つのクエリファミリーで管理し、新バージョンとして拡張 |
| 正確な受領確認 | 引き渡し前にファイル、メタデータ、索引、現在の内容ハッシュを検証 |
| ライフサイクル分類 | 一時 SQL、再利用 SQL、ダッシュボード向け SQL を区別 |

公開仕様版には、企業固有のスキーマ、本番テーブル名、認証情報、非公開の業務ルール、
クエリ結果、社内実行環境との連携は含まれません。

## プロジェクトに必要な情報

| 必要な情報 | Skill での管理方法 |
|---|---|
| 元のテレメトリ定義 | XML、JSON、YAML、Excel、CSV、テキストなどを変更せず `sources/raw/` にコピーし、ハッシュとバージョンを記録 |
| データベースと SQL 方言 | 環境ごとに SQL 生成方言を宣言し、ローカルの DB-API または CLI 接続情報は Git の外で管理 |
| 企画表と設定表 | 原本を `knowledge/planning/` に保存。証拠であり、自動的に正しいルールにはならない |
| 人が確認した資料 | `knowledge/confirmed/` に確認版、確認者、理由、元資料との関係を保存 |
| 正式な業務ルール | 確認済みの Base、粒度、計算、フィルター、参照資料を `rules/definitions/` の不変バージョンとして保存 |

Skill はこれらのプロジェクト事実を作りません。資料の所有者と変更履歴を見える形にします。
完全な手順は [プロジェクト導入ガイド](sql-engineering/references/project-onboarding.md) を参照してください。

## プロジェクトを作成して導入する

### 1. Skill をインストール

このリポジトリをクローンし、`sql-engineering/` を Codex の Skills ディレクトリへコピーまたは
リンクします。

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Codex を再起動または更新すると、`$sql-engineering` として呼び出せます。

### 2. ワークスペースを初期化

```powershell
python .\sql-engineering\scripts\sql_workspace.py bootstrap `
  --root . `
  --project-id example `
  --dialect starrocks
```

`bootstrap` は `sql-projects/example` と、空のテレメトリ、ナレッジ、ルール、SQL カタログを
作成します。再実行しても登録済みの内容は削除せず、不足した空構造だけを補います。

### 3. プロジェクト資料を登録

元のテレメトリ、企画/設定表、人が確認した資料を先に登録します。その後、データベース環境と
SQL 方言を宣言し、人が明示的に確認したルールだけを固定します。最後に次を実行します。

```powershell
python .\sql-engineering\scripts\sql_workspace.py status `
  --root .\sql-projects\example
```

`query_context_ready=false` は元のテレメトリ定義が未登録であることを示します。自動接続がなくても
問題はなく、その場合は SQL ファイルを手動実行用に引き渡します。

### 4. Codex に自然言語で依頼

```text
$sql-engineering StarRocks 用に、日別の重複しないログインユーザー数を集計する SQL を作成してください。
固定した日付範囲は params CTE に置き、example プロジェクトへ保存して、正確なファイルを返してください。
```

Codex はプロジェクトを確認し、クエリファミリーを作成または再利用して、たとえば
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql` を保存します。
その後 receipt を実行し、保存済みの絶対パスを返します。データベースでの実行結果は別に報告し、
実行済みだと推測してはいけません。

自動実行は任意です。プロジェクトには環境名だけを登録し、実際の接続設定は Git が無視する
`.sql-engineering/connections.local.json` に保存します。ドライバー、CLI、シークレット、接続設定が
ない場合、Skill は `manual_required` と正確な SQL パスを返し、ユーザーに実行と結果ファイルの
返却を依頼します。Chrome や DA の Web コンソールを操作することはありません。

## よく使う依頼

| 目的 | 依頼例 |
|---|---|
| プロジェクト作成 | `$sql-engineering StarRocks 方言で alpha プロジェクトを作成し、不足するテレメトリ、資料、ルール、接続設定を教えてください。` |
| テレメトリ登録 | `$sql-engineering この XML を PlayerLogin の元のテレメトリ定義として変更せず登録してください。` |
| 企画証拠の登録 | `$sql-engineering このモード設定表を企画入力として保存し、確認済みルールにはしないでください。` |
| ルール固定 | `$sql-engineering 人が確認した日次アクティブユーザー定義を新しい正式ルール版として固定してください。` |
| SQL を作成 | `$sql-engineering このプロジェクトの日次アクティブユーザークエリを作成して保存してください。` |
| 外部 SQL を修正 | `$sql-engineering この SQL を取り込み、プロジェクトの方言に修正し、元ファイルは上書きしないでください。` |
| 過去の作業を検索 | `$sql-engineering リテンションに関する保存済みクエリを探し、目的を要約してください。` |
| クエリを拡張 | `$sql-engineering 既存のアクティブユーザークエリファミリーにプラットフォーム次元を追加してください。` |
| 有用なクエリを保管 | `$sql-engineering 確認済みのロジックを retained クエリとして保存してください。` |
| 引き渡しを検証 | `$sql-engineering この v003.sql の receipt を確認し、正確なパスを返してください。` |
| 直接実行 | `$sql-engineering 設定済みの開発データベースで、この保存済みクエリを実行してください。` |

## ライフサイクル

```text
依頼
  -> 元のテレメトリ定義を登録
  -> 企画資料と人が確認した資料を分離
  -> 適用する正式ルールを固定
  -> データベース環境と SQL 方言を選択
  -> 一時 SQL バージョンを保存
  -> ユーザー環境で実行
  -> 修正または拡張を次のバージョンとして保存
  -> 必要に応じて retained または dashboard 向けへ昇格
  -> 正確な delivery receipt
```

一つのクエリファミリーは一つの分析課題を表します。日付更新、構文修正、同じ課題を包含する拡張は、
同じファミリーの新バージョンとして保存します。Base、主要指標、支援する意思決定が変わる場合は、
新しいファミリーを作成します。

## ワークスペース構造

```text
sql-projects/
  _asset_catalog/              プロジェクト横断検索の拡張領域
  _review_inbox/               取り込みまたはレビュー待ちの外部 SQL と証拠
  _rule_review/                ルールレビューの拡張領域
  example/
    .sql-engineering/
      project.json             プロジェクト識別子と SQL 方言
    sources/
      source-catalog.json
      raw/<source>/vNNN.*      変更しない元のテレメトリ定義
    knowledge/
      planning/<item>/vNNN.*   元の企画表と設定表
      confirmed/<item>/vNNN.* 人が確認した資料
    rules/
      definitions/<rule>/vNNN.json
    context/                    非正式なメモとプラットフォーム資料
    sql-workspace/
      index.json               検索可能な機械索引
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

アンダースコアで始まるディレクトリはプロジェクト横断の拡張点です。プロジェクト内では元の証拠、
人の確認、正式ルール、実行 SQL を分離し、相互に暗黙置換されないようにします。

## コマンド一覧

| コマンド | 用途 |
|---|---|
| `bootstrap` | リポジトリ構造を作成し、必要なら最初のプロジェクトも初期化 |
| `init` | 一つの独立プロジェクトを初期化 |
| `environment` | プロジェクトの環境名をローカルのデータベース接続プロファイルへ対応付け |
| `source` | 元の形式を変えずにテレメトリ定義をコピーして登録 |
| `knowledge` | 企画入力または人が確認した資料を登録 |
| `rule` | 明示的に確認された業務ルールを新しい不変バージョンとして固定 |
| `status` | 不足するソース、資料、ルール、実行設定を表示 |
| `save` | 新しい不変 SQL バージョンを保存して索引を更新 |
| `search` | タイトル、要約、タグを検索 |
| `receipt` | 引き渡し前に特定の SQL バージョンを検証 |
| `sql_execute.py run` | 保存済みの読み取り専用 SQL を実行、または手動実行へ引き渡し |

同梱の架空クエリ
[`daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql) を試せます。
[エージェントの実行例](sql-engineering/references/example.md) には、依頼、コマンド、期待される
ファイル、最終応答の契約が示されています。

## 設計上の境界

- SQL 方言はプロジェクト設定で選択します。Skill はテーブル、パーティション、業務 ID、指標定義を推測しません。
- プロジェクトコンテキストは任意で、明示的に宣言します。Skill は個人のナレッジベースに依存せず、
  不足するスキーマ情報は保存済みの読み取り専用データベースクエリで確認できます。
- 自動実行は DB-API またはデータベース CLI だけを使用します。ブラウザーや DA Web コンソールの
  自動操作は対応せず、設定がなければ手動実行へ移ります。
- 外部 SQL は不変の入力として扱い、変更版はプロジェクト内へ保存します。
- 保存済みバージョンを上書きしません。手動変更は receipt のハッシュ検証で検出されます。
- ライフサイクルラベルは用途を示すだけで、業務上の正しさや実行成功を証明しません。
- 結果、可視化、検証、ダッシュボードは統制された拡張で追加できますが、SQL だけから黙って推測しません。
- 認証情報、非公開スキーマ、本番結果、ローカル絶対パスをコミットしてはいけません。

## ドキュメント

| トピック | ドキュメント |
|---|---|
| エージェントのワークフローと厳守事項 | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| 新規プロジェクトの入力と導入手順 | [`references/project-onboarding.md`](sql-engineering/references/project-onboarding.md) |
| 完全な実行例 | [`references/example.md`](sql-engineering/references/example.md) |
| プロジェクトとディレクトリの契約 | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| クエリファミリーのライフサイクル | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL 引き渡し検証 | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| データベース環境と実行 | [`references/database-execution.md`](sql-engineering/references/database-execution.md) |
| 接続方法と SQL 方言 | [`references/dialects.md`](sql-engineering/references/dialects.md) |
| コントリビューション | [CONTRIBUTING.md](CONTRIBUTING.md) |
| セキュリティポリシー | [SECURITY.md](SECURITY.md) |

## 開発

公開コアは Python 標準ライブラリだけを使用します。DB-API 実行時は、ローカル接続設定で
ユーザーが選択したデータベースドライバーを読み込みます。

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py .\sql-engineering\scripts\sql_execute.py
```

[Apache License 2.0](LICENSE) の下で提供されます。
