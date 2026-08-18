# Game Data Analysis Skills

**面向 Codex 的可插拔遊戲資料分析 Skills 合集，把 SQL、口徑、證據和交付檔案保存下來。**

[English](README.md) · [简体中文](README.zh-CN.md)

Game Data Analysis Skills 是一組可以獨立使用、也可以組合使用的 Skills。只使用一個、選幾個
一起用，或由其他工具把選定能力接成自己的工作流程都可以；這個合集不依賴特定產品層。

一次查詢不該在聊天結束後就失去上下文。這個合集把問題、SQL 版本、資料來源、口徑、結果證據
和交付決定放在可追溯的檔案與索引裡，同時保留臨時查詢需要的輕量流程。

## 這個合集能做什麼

| 模組 | 負責的工作 |
| --- | --- |
| **Setup** | 以 Git 為基礎，設定 GitHub、GitLab、自架 Git、SSH 或本機 Git，並獨立選擇 Git、SVN、本機資料夾或暫不設定企劃來源。 |
| **SQL 工作區** | 把每條查詢保存成不可變、可搜尋的版本，附上中繼資料、內容雜湊和精確 receipt。 |
| **口徑與知識** | 分開保存原始事件定義、企劃輸入、人工確認參考和 canonical rules，保留來源關係。 |
| **查詢生命週期** | 從需求判定走到 QUERY、驗證、正式資產套件和 Dashboard 派生，不能無聲跳過證據。 |
| **Review 與健康檢查** | 從產品意義和 SQL 結構兩個角度檢查，及早發現漂移。 |
| **結果與 lineage** | 將結果、視覺化和活頁簿綁定到真正產生它們的 SQL 版本。 |
| **執行面** | 對已通過 receipt 的 SQL 選擇直接 DB-API/CLI、網頁適配器，或明確的手動交付。 |
| **Excel 報告視覺化** | 在本機檢查支援的活頁簿並產生可離線使用的報告；倉庫不附帶任何實際活頁簿。 |

## 安裝並跑通第一條查詢

需要 Python 3.11 以上和 Git；第一次執行不需要第三方 Python 套件。

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

重新整理 Codex 後使用 `$sql-engineering`。不連接資料庫也能先驗證檔案流程：

```powershell
python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "按日期統計虛構使用者的活躍數量。" `
  --kind temporary `
  --slug daily-active-users
```

命令會回傳不可變的 `v001.sql` 路徑。交付前對同一條路徑執行 `receipt`。沒有自動執行面時，
結果會是 `manual_required`，不會把「已產生 SQL」說成「已執行」。

## 選擇執行面

正式專案初始化時可以先表達執行意圖：

```powershell
python .\sql-engineering\scripts\local_setup.py init `
  --repo-root . `
  --project example `
  --execution-surface direct
```

- `direct`：使用專案本機的唯讀 DB-API 或 CLI 設定。
- `web`：使用被忽略的 `web_query_adapter_v1` 和使用者自己的 Chrome 工作階段。
- `manual`：暫不設定自動執行，回傳準確 SQL 檔案給使用者。

目前附有 Deltaverse 網頁適配範例；其他網站按照[執行面與網頁適配指南](sql-engineering/references/execution-surfaces.md)
建立自己的本機設定。三種執行面不會靜默互相切換。

## SQL 與資產放在哪裡

- `sql-projects/<project>/query_workspace/`：臨時、歷史和可繼續修改的 SQL；用
  `sql_query_workspace.py search` 查找，這個目錄不進 Git。
- `sql-projects/<project>/formal_assets/`：正式 SQL、結果、驗證和 Dashboard 套件；用
  `sql_repository.py build|serve` 建立唯讀檢視。
- Provider Snapshot、Catalog schema 和 receipt：給外部只讀消費者使用的穩定身份、路徑和雜湊介面。

## 企劃來源

倉庫 Git remote 和企劃來源分開設定：

```powershell
# Git 企劃來源
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider git `
  --planning-url <git-planning-url> `
  --planning-branch main `
  --planning-id planning
python .\setup\scripts\bootstrap_repo.py planning-sync --root .

# SVN 企劃來源
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider svn `
  --planning-url <svn-url> `
  --planning-revision <revision>

# 使用者管理的本機資料夾
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider local `
  --planning-path <folder>
```

尚未準備好時使用 `--planning-provider none`。provider、URL、分支、revision 和本機 checkout
資訊保存在被忽略的 `.local/`；密碼和 token 交給 Git/SVN 原生憑證機制。

## 安全與許可

- 公開樹只包含虛構範例，不包含生產 SQL、結果、私有表結構或憑證。
- 外部 SQL 會先保存成不可變輸入，不會覆寫原始檔案。
- direct 與 web 執行都必須是唯讀；網頁執行不自動登入、不保存 Cookie 或 token。
- Excel 內嵌套件的授權見 [THIRD_PARTY_NOTICES.md](excel-report-visualizer/THIRD_PARTY_NOTICES.md)。

## 繼續閱讀

- [Setup 接入手冊](setup/references/onboarding.md)
- [SQL Engineering 合約](sql-engineering/SKILL.md)
- [使用手冊](docs/USER_MANUAL.md)
- [執行面與網頁適配指南](sql-engineering/references/execution-surfaces.md)
- [只讀資產消費手冊](docs/READONLY_ASSET_CONSUMER_GUIDE.md)
- [公開維護邊界](docs/PUBLIC_MAINTENANCE.md)
- [Excel 報告視覺化](excel-report-visualizer/README.md)

## 後續規劃

- 依排程產生週期性報告。
- 跨資產比較結果，檢查是否合理。
- 追溯異常來源並調查可能原因。

本專案使用 Apache License 2.0。
