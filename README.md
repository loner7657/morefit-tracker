# More Fit 人潮自動統計系統（七張店・古亭店）

自動記錄 **More Fit 七張店、古亭店** 的館內即時人數，並透過單頁式 HTML 儀表板獨立分析**「平日晚間」**與**「週末全天」**的人潮高低峰時段，全程雲端自動運作、免開機、\$0 免費。

---

## 核心架構與設計特點

- **雙時段自動分流 + 高密度批次採集**
  - **平日（週一至週五）**：`17:30 – 21:00`（鎖定下班尖峰與進退場趨勢）
  - **週末（週六至週日）**：`08:00 – 21:00`（涵蓋早、中、下午至晚間全天 13 小時變化）
  - **採集頻率**：每 **15 分鐘** 喚醒一次工作流程，由 `fetch.py` 在區間內對齊時鐘 **每 3 分鐘抓取一筆即時數據**（共 5 筆），區間結束時**合併為單次 Git Commit 存檔**，每小時僅產生 4 次存檔，完全避開 GitHub Pages 每小時 10 次部署上限。
- **「平日晚間 / 週末全天」雙分頁獨立視覺化**
  - **平日晚間分頁**：並排比較七張店（上限 400 人）與古亭店（上限 450 人）在 `18:00–19:00` vs `19:00–20:00` 哪個時段人較少，搭配 `17:30–21:00` 曲線圖與週一～週五對照表。
  - **週末全天分頁**：自動從全天 13 個整點區間中抓出 **「推薦離峰 Top 3（最少人）」** 與 **「擁擠尖峰 Top 3（建議避開）」**，搭配 `08:00–21:00` 全天曲線圖與 13 小時逐時熱度表。
- **零外部套件依賴、支援離線與線上觀看**
  - `fetch.py` 僅使用 Python 內建標準函式庫（免 `pip install`）。
  - `index.html` 支援透過 **GitHub Pages 瀏覽器書籤直看**，也支援在電腦本機**直接雙擊開啟**，並內建「預覽累積效果 (2 週模擬)」一鍵預覽平日與週末完整圖表。

---

## 專案檔案結構

```text
.
├── .github/
│   └── workflows/
│       └── tracker.yml         # GitHub Actions 排程設定 (含平日晚間與週末全天排程)
├── data/
│   ├── capacity_history.csv    # 歷史累積原始數據 (UTF-8 BOM，Excel 可直接開啟)
│   └── capacity_data.js        # 自動同步產生的 JS 資料封裝 (供本機雙擊 index.html 離線讀取)
├── fetch.py                    # 採集腳本 (自動判斷平日/週末時段、每 3 分鐘採樣、15 分鐘批次結束)
├── index.html                  # 雙分頁統計視覺化儀表板 (平日晚間 + 週末全天)
├── more_fit.md                 # 專案規格書 (PRD & Spec)
└── README.md                   # 操作與雲端部署說明
```

---

## 5 分鐘雲端上線步驟（完成後永久免開機）

### 1. 建立 GitHub 公開儲存庫並推送檔案
在 GitHub 建立一個 **Public（公開）** 儲存庫（公開儲存庫享有 GitHub Actions 執行時間完全免費無上限，以及免費 GitHub Pages），接著在專案資料夾執行：

```powershell
git init
git branch -M main
git add .
git commit -m "feat: MoreFit weekday & weekend capacity tracker"
git remote add origin https://github.com/loner7657/morefit-tracker.git
git push -u origin main
```

### 2. 開啟 GitHub Pages（取得專屬統計網頁網址）
1. 進入 GitHub 儲存庫 → 上方 **Settings** → 左側選單 **Pages**。
2. **Build and deployment** > **Source** 選擇 `Deploy from a branch`。
3. Branch 選擇 `main`、資料夾選擇 `/ (root)`，點擊 **Save**。
4. 約 1 分鐘後即可透過以下網址開啟儀表板（建議加入電腦書籤）：
   https://loner7657.github.io/morefit-tracker/

### 3. 手動測試雲端寫入功能
1. 點擊儲存庫上方 **Actions** → 左側選擇 **MoreFit Capacity Tracker**。
2. 點擊右側 **Run workflow**，勾選 **`忽略時段限制，立即強制抓取單筆紀錄並存檔（測試用）`**，按下綠鈕執行。
3. 約 5 秒跑完顯示綠勾，且 `data/capacity_history.csv` 自動新增一筆最新紀錄即代表權限與流程完全正常！

### 4. 綁定免費 `cron-job.org` 精準每 15 分鐘喚醒（選用強化）
GitHub 內建 `schedule` 已自動啟用；若希望避開尖峰延遲與 60 天閒置暫停機制，可在免費的 [cron-job.org](https://cron-job.org/) 建立兩組排程精準喚醒：

1. **取得 GitHub Fine-grained Token：**
   - GitHub 右上角頭像 → **Settings** → 左下角 **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**。
   - **Repository access** 選 `Only select repositories` 並勾選 `morefit-tracker`。
   - **Repository permissions** 將 **Actions** 設為 `Read and write`，產生並複製 Token（`github_pat_...`）。
2. **在 `cron-job.org` 建立 2 組排程（共用相同 URL 與 Header）：**
   - **共用目標 URL**：
     ```text
     https://api.github.com/repos/loner7657/morefit-tracker/actions/workflows/tracker.yml/dispatches
     ```
   - **排程一（平日晚間）**：時區選 `Asia/Taipei`，Custom Cron 填入 `*/15 17-21 * * 1-5`（`fetch.py` 會自動過濾 `17:00` 與 `17:15`）。
   - **排程二（週末全天）**：時區選 `Asia/Taipei`，Custom Cron 填入 `*/15 8-21 * * 0,6`。
   - 兩組排程的 **Advanced** 分頁皆設定：
     - **Request method**：`POST`
     - **Headers** 新增三筆：
       - `Accept`: `application/vnd.github+json`
       - `Authorization`: `Bearer <貼上你的 github_pat_...>`
       - `Content-Type`: `application/json`
     - **Request body**：
       ```json
       {"ref":"main"}
       ```
   - 點擊 **TEST RUN** 確認回傳 `204 No Content` 後儲存即完成！

---

## 指令參考（選用）

若在有安裝 Python 3 的環境手動測試 `fetch.py`：

```bash
# 依正常排程規則執行（自動判斷平日 17:30-21:00 或週末 08:00-21:00，啟動 15 分鐘批次每 3 分鐘抓一次）
python3 fetch.py

# 僅抓取當下單筆紀錄（受平日/週末時段限制）
python3 fetch.py --once

# 忽略星期與時段限制，立即強制抓取單筆紀錄並更新 CSV/JS（測試用）
python3 fetch.py --force
```
