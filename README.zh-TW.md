# Game Data Analysis Skills

**一套面向 Codex、以檔案為準並支援可設定唯讀資料庫執行的 SQL 生命週期。**

Game Data Analysis Skills 把對話裡的 SQL 工作變成長期可用的專案檔案。每一條產生或
修改後的 SQL 都會落盤、版本化、建立索引、支援檢索，並透過精確路徑交付；對話結束後，
其他人仍然能找到它、理解它、繼續修改它。

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [Español](README.es.md) · [한국어](README.ko.md)

> 聊天裡的 SQL 程式碼區塊用於解釋；通過校驗的 `vNNN.sql` 檔案才是交付物。

## 它解決什麼問題

聊天產生的 SQL 很容易遺失。真正有用的查詢經常被複製到另一個檔案後繼續修改，逐漸失去
版本歷史、用途說明和結果對應關係；外部傳入的 SQL 還可能被直接覆蓋。過一段時間，沒有人
知道哪一版跑出了當時的結果。

這個 Skill 給 Codex 一套小而明確的工作空間契約：

| 能力 | 實際行為 |
|---|---|
| 工作空間初始化 | 自動建立穩定的 `sql-projects/` 結構和第一個專案 |
| 專案資料治理 | 分開版本化埋點原始定義、策劃輸入、人工確認資料和固定口徑 |
| SQL 檔案交付 | 每次產生或修改都儲存為不可變的 `vNNN.sql` |
| 按環境執行 | 透過設定好的唯讀 DB-API 驅動程式或資料庫命令列執行已儲存 SQL |
| 外部 SQL 匯入 | 把外部檔案視為輸入，在專案內儲存新版本，不覆蓋原檔案 |
| 可檢索歷史 | 儲存人能讀懂的標題、用途、標籤、方言、路徑和內容雜湊 |
| 持續修改 | 同一分析問題保留在一個查詢族中，透過新版本繼續演進 |
| 精確回執 | 交付前核對 SQL 檔案、中繼資料、索引和目前內容雜湊 |
| 生命週期區分 | 明確區分臨時查詢、可重用查詢和面向看板的 SQL |

公開規範版刻意不包含任何公司的專案設定、生產資料表名稱、內部口徑、憑證、查詢結果或
內部執行平台接入。

## 一個專案需要提供什麼

| 必要資料 | Skill 如何管理 |
|---|---|
| 埋點原始定義 | XML、JSON、YAML、Excel、CSV、文字或其他格式原樣複製到 `sources/raw/`，記錄雜湊和版本 |
| 資料庫與 SQL 方言 | 按環境宣告產生 SQL 使用的方言；本機 DB-API 或資料庫用戶端連線資訊不進入 Git |
| 策劃表和設定表 | 原件儲存到 `knowledge/planning/`，它們只是證據，不會自動變成正確口徑 |
| 已人工確認資料 | 在 `knowledge/confirmed/` 儲存確認版本、確認人、原因以及與策劃表的關係 |
| 固定業務口徑 | 把已明確確認的 Base、粒度、演算法、篩選和引用資料儲存為 `rules/definitions/` 下的不可變版本 |

Skill 不會替專案編造這些事實，它負責讓資料所有權和變化過程可見。完整步驟請見
[專案接入手冊](sql-engineering/references/project-onboarding.md)。

## 建立並接入專案

### 1. 安裝 Skill

複製儲存庫，將 `sql-engineering/` 複製或連結到 Codex Skills 目錄：

```powershell
git clone https://github.com/thevenomsnake/game-data-analysis-skills.git
Set-Location .\game-data-analysis-skills
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

重新整理或重新啟動 Codex 後，可以在任務中使用 `$sql-engineering`。

### 2. 初始化工作空間

```powershell
python .\sql-engineering\scripts\sql_workspace.py bootstrap `
  --root . `
  --project-id example `
  --dialect starrocks
```

`bootstrap` 會初始化 `sql-projects/example`，同時建立空的埋點、知識、口徑和 SQL 清單；
重複執行只補齊缺少的空結構，不會清空已登記內容。

### 3. 登記專案資料

先把埋點原始檔案、策劃/設定表和單獨人工確認的資料交給 Codex 登記，再宣告資料庫環境和
SQL 方言，只固定使用者明確確認的口徑。最後執行：

```powershell
python .\sql-engineering\scripts\sql_workspace.py status `
  --root .\sql-projects\example
```

`query_context_ready=false` 表示還沒有登記任何埋點原始定義。沒有自動資料庫連線並不阻斷，
該專案會使用人工執行 SQL、回傳結果檔案的流程。

### 4. 直接用自然語言提出需求

```text
$sql-engineering 產生一條 StarRocks SQL，按日統計去重登入使用者數。
固定日期放在 params CTE，儲存到 example 專案，並回傳準確檔案路徑。
```

Codex 應讀取專案設定，建立或重用查詢族，儲存類似
`sql-projects/example/sql-workspace/temporary/daily-active-users/v001.sql` 的版本，執行
receipt 校驗，然後回傳絕對檔案路徑。是否在資料庫中實際執行必須另外說明，不能預設聲稱。

自動查詢是可選能力。專案只登記環境名稱，真實連線設定儲存在 Git 忽略的
`.sql-engineering/connections.local.json`。如果沒有驅動程式、資料庫用戶端、金鑰或連線設定，
Skill 會回傳 `manual_required` 和準確 SQL 路徑，請使用者自行查詢並回傳結果檔案；不會點擊
Chrome 或 DA 網頁控制台。

## 常見用法

| 目標 | 範例 |
|---|---|
| 建立專案 | `$sql-engineering 建立 alpha 專案，SQL 方言為 StarRocks，並告訴我還缺哪些埋點、資料、口徑和連線設定。` |
| 登記埋點 | `$sql-engineering 把這個 XML 原樣登記為 PlayerLogin 的埋點來源定義。` |
| 登記策劃證據 | `$sql-engineering 把這份模式設定表儲存為策劃輸入，不要直接當成確認口徑。` |
| 固定口徑 | `$sql-engineering 把人工確認的日活定義固定為新的口徑版本。` |
| 產生 SQL | `$sql-engineering 為這個專案產生每日活躍使用者查詢，並儲存到檔案。` |
| 修改外部 SQL | `$sql-engineering 匯入這份 SQL，按專案方言修正，不要覆蓋原檔案。` |
| 尋找舊查詢 | `$sql-engineering 尋找留存相關的歷史 SQL，並說明各自用途。` |
| 繼續擴充 | `$sql-engineering 在現有活躍使用者查詢族中增加平台維度。` |
| 保留有價值查詢 | `$sql-engineering 這套邏輯已確認，儲存為 retained 查詢版本。` |
| 核對交付 | `$sql-engineering 校驗這個 v003.sql 的 receipt，並回傳準確路徑。` |
| 直接查詢 | `$sql-engineering 使用已設定的開發資料庫執行這條已儲存查詢。` |

## 生命週期

```text
需求
  -> 登記埋點原始定義
  -> 分開儲存策劃資料和人工確認資料
  -> 固定目前適用口徑
  -> 選擇資料庫環境和 SQL 方言
  -> 儲存臨時 SQL 版本
  -> 在使用者環境執行
  -> 修正或擴充為下一版本
  -> 可選升級為 retained 或 dashboard SQL
  -> 精確 delivery receipt
```

一個查詢族代表一個分析問題。日期更新、語法修正、同一問題的完整擴充繼續使用同一個
查詢族並產生新版本；Base、核心指標或要支援的決策發生變化時，建立新查詢族。

## 目錄結構

```text
sql-projects/
  _asset_catalog/              預留的跨專案檢索產物
  _review_inbox/               等待匯入或審查的外部 SQL 和證據
  _rule_review/                預留的口徑審查產物
  example/
    .sql-engineering/
      project.json             專案標識和方言
    sources/
      source-catalog.json
      raw/<source>/vNNN.*      原樣儲存的埋點定義
    knowledge/
      planning/<item>/vNNN.*   策劃表和設定表原件
      confirmed/<item>/vNNN.*  已人工確認資料
    rules/
      definitions/<rule>/vNNN.json
    context/                    非權威說明和平台手冊
    sql-workspace/
      index.json               可搜尋機器索引
      temporary/<slug>/
        v001.sql
        v001.meta.json
      retained/<slug>/
      dashboard/<slug>/
```

三個底線目錄是跨專案擴充入口。專案內部把原始證據、人工確認、固定口徑和可執行 SQL
分開儲存，避免其中一種資料靜默替代另一種。

## 命令參考

| 命令 | 用途 |
|---|---|
| `bootstrap` | 建立儲存庫結構，並可同時初始化第一個專案 |
| `init` | 初始化一個獨立專案 |
| `environment` | 把專案環境名稱對應到本機資料庫連線設定 |
| `source` | 不改變格式地複製並登記埋點原始定義 |
| `knowledge` | 登記策劃輸入或單獨的人工確認資料 |
| `rule` | 把明確確認的業務口徑固定為新的不可變版本 |
| `status` | 顯示專案還缺哪些來源、資料、口徑和執行設定 |
| `save` | 儲存新的不可變 SQL 版本並更新索引 |
| `search` | 檢索標題、用途和標籤 |
| `receipt` | 交付前校驗某個準確 SQL 版本 |
| `sql_execute.py run` | 執行已儲存的唯讀 SQL，或回傳人工查詢交接 |

可以直接使用儲存庫裡的虛構範例
[`daily-active-users.sql`](sql-engineering/assets/examples/daily-active-users.sql)。
[AI 完整工作範例](sql-engineering/references/example.md) 展示了需求、命令、產物結構和最終
回覆要求。

## 必須知道的邊界

- 專案設定決定方言；Skill 不猜測資料表、分區欄位、業務 ID 或指標口徑。
- 專案資料是可選且必須明確宣告的；Skill 不依賴個人知識庫。缺少資料表結構時，可透過
  已儲存的唯讀資料庫查詢檢查中繼資料或列舉值。
- 自動執行只支援 DB-API 或資料庫命令列用戶端，不支援瀏覽器和 DA 網頁控制台；缺少設定時
  正常轉為人工查詢。
- 外部 SQL 始終是不可變輸入，修改結果儲存到專案內。
- 已儲存版本不會被覆蓋；手動竄改會被 receipt 的雜湊檢查發現。
- 生命週期標籤只描述用途，不等於 SQL 已經跑通或業務口徑已經確認。
- 結果、視覺化、驗證和看板可以由治理擴充繼續實作，但不會從一份 SQL 靜默推斷出來。
- 憑證、私有資料表結構、生產結果和本機絕對路徑不能提交到儲存庫。

## 文件

| 主題 | 文件 |
|---|---|
| AI 工作流程和硬邊界 | [`sql-engineering/SKILL.md`](sql-engineering/SKILL.md) |
| 新專案資料和接入流程 | [`references/project-onboarding.md`](sql-engineering/references/project-onboarding.md) |
| 完整範例 | [`references/example.md`](sql-engineering/references/example.md) |
| 專案和目錄契約 | [`references/project-contract.md`](sql-engineering/references/project-contract.md) |
| 查詢族生命週期 | [`references/workflow.md`](sql-engineering/references/workflow.md) |
| SQL 交付檢查 | [`references/sql-quality.md`](sql-engineering/references/sql-quality.md) |
| 資料庫環境和執行 | [`references/database-execution.md`](sql-engineering/references/database-execution.md) |
| 資料庫連線方式與 SQL 方言 | [`references/dialects.md`](sql-engineering/references/dialects.md) |
| 貢獻方式 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 安全規範 | [SECURITY.md](SECURITY.md) |

## 開發校驗

公開版核心只使用 Python 標準函式庫；DB-API 自動查詢會載入使用者在本機連線設定中選擇的
資料庫驅動程式。

```powershell
python -m unittest discover -s .\sql-engineering\tests -p "test_*.py"
python -m py_compile .\sql-engineering\scripts\sql_workspace.py .\sql-engineering\scripts\sql_execute.py
```

本專案使用 [Apache License 2.0](LICENSE)。
