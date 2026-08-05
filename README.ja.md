# Game Data Analysis Skills

**Codex のための、ファイルを正とする SQL ライフサイクル。**

Game Data Analysis Skills は、会話の中で生まれた SQL を永続的なプロジェクトファイルへ変換します。
生成または変更されたすべてのクエリを保存し、バージョン管理し、索引化して検索可能にし、正確な
パスで引き渡します。会話が終わった後でも、作業内容を理解して継続できます。

[English](README.md) · [简体中文](README.zh-CN.md) · [Español](README.es.md) · [한국어](README.ko.md)

> チャット内の SQL コードブロックは説明です。検証済みの `vNNN.sql` ファイルが成果物です。

## 解決する課題

チャットで作成した SQL は簡単に失われます。有用なクエリが別ファイルへコピーされ、履歴なしで
編集されたり、用途の説明や結果との対応関係が失われたりします。外部から渡された SQL が直接
上書きされることもあります。時間が経つと、どのバージョンが結果を生成したのか分からなくなります。

この Skill は Codex に、小さく明確で強制可能なワークスペース契約を与えます。

| 機能 | 実際の動作 |
|---|---|
| リポジトリ初期化 | 安定した `sql-projects/` 構造と最初のプロジェクトを作成 |
| SQL の引き渡し | 生成または変更のたびに、不変の `vNNN.sql` バージョンとして保存 |
| 外部 SQL の取り込み | 入力ファイルを変更せず、プロジェクト内のコピーで作業 |
| 検索可能な履歴 | 人が読めるタイトル、目的、タグ、方言、パス、内容ハッシュを記録 |
| 継続的な改訂 | 同じ分析課題を一つのクエリファミリーで管理し、新バージョンとして拡張 |
| 正確な受領確認 | 引き渡し前にファイル、メタデータ、索引、現在の内容ハッシュを検証 |
| ライフサイクル分類 | 一時 SQL、再利用 SQL、ダッシュボード向け SQL を区別 |

公開仕様版には、企業固有のスキーマ、本番テーブル名、認証情報、非公開の業務ルール、
クエリ結果、社内実行環境との連携は含まれません。

## 3 分で始める

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

リポジトリには共有の `_asset_catalog`、`_review_inbox`、`_rule_review` ディレクトリ骨格が
含まれています。`bootstrap` は不足したディレクトリを補い、`sql-projects/example` を初期化します。
再実行しても既存の内容は削除されません。

### 3. Codex に自然言語で依頼

```text
$sql-engineering StarRocks 用に、日別の重複しないログインユーザー数を集計する SQL を作成してください。
固定した日付範囲は params CTE に置き、example プロジェクトへ保存して、正確なファイルを返してください。
```

Codex はプロジェクトを確認し、クエリファミリーを作成または再利用して、たとえば
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql` を保存します。
その後 receipt を実行し、保存済みの絶対パスを返します。データベースでの実行結果は別に報告し、
実行済みだと推測してはいけません。

## よく使う依頼

| 目的 | 依頼例 |
|---|---|
| SQL を作成 | `$sql-engineering このプロジェクトの日次アクティブユーザークエリを作成して保存してください。` |
| 外部 SQL を修正 | `$sql-engineering この SQL を取り込み、プロジェクトの方言に修正し、元ファイルは上書きしないでください。` |
| 過去の作業を検索 | `$sql-engineering リテンションに関する保存済みクエリを探し、目的を要約してください。` |
| クエリを拡張 | `$sql-engineering 既存のアクティブユーザークエリファミリーにプラットフォーム次元を追加してください。` |
| 有用なクエリを保管 | `$sql-engineering 確認済みのロジックを retained クエリとして保存してください。` |
| 引き渡しを検証 | `$sql-engineering この v003.sql の receipt を確認し、正確なパスを返してください。` |

## ライフサイクル

```text
依頼
  -> プロジェクトと SQL 方言を確認
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
    sql-workspace/
      index.json               検索可能な機械索引
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

アンダースコアで始まる三つのディレクトリは安定した拡張点です。公開コアはディレクトリを作成しますが、
カタログ、レビュー、ルールの内容を捏造しません。

## コマンド一覧

| コマンド | 用途 |
|---|---|
| `bootstrap` | リポジトリ構造を作成し、必要なら最初のプロジェクトも初期化 |
| `init` | 一つの独立プロジェクトを初期化 |
| `save` | 新しい不変 SQL バージョンを保存して索引を更新 |
| `search` | タイトル、要約、タグを検索 |
| `receipt` | 引き渡し前に特定の SQL バージョンを検証 |

同梱の架空クエリ
[`daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql) を試せます。
[エージェントの実行例](sql-engineering/references/example.md) には、依頼、コマンド、期待される
ファイル、最終応答の契約が示されています。

## 設計上の境界

- SQL 方言はプロジェクト設定で選択します。Skill はテーブル、パーティション、業務 ID、指標定義を推測しません。
- 外部 SQL は不変の入力として扱い、変更版はプロジェクト内へ保存します。
- 保存済みバージョンを上書きしません。手動変更は receipt のハッシュ検証で検出されます。
- ライフサイクルラベルは用途を示すだけで、業務上の正しさや実行成功を証明しません。
- 結果、可視化、検証、ダッシュボードは統制された拡張で追加できますが、SQL だけから黙って推測しません。
- 認証情報、非公開スキーマ、本番結果、ローカル絶対パスをコミットしてはいけません。

## ドキュメント

| トピック | ドキュメント |
|---|---|
| エージェントのワークフローと厳守事項 | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| 完全な実行例 | [`references/example.md`](sql-engineering/references/example.md) |
| プロジェクトとディレクトリの契約 | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| クエリファミリーのライフサイクル | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL 引き渡し検証 | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| コントリビューション | [CONTRIBUTING.md](CONTRIBUTING.md) |
| セキュリティポリシー | [SECURITY.md](SECURITY.md) |

## 開発

公開版は Python 標準ライブラリだけを使用します。

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py
```

[Apache License 2.0](LICENSE) の下で提供されます。
