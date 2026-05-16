# 🕷️ 台藝大官網爬蟲 (NTUA Crawler)

自動爬取**國立臺灣藝術大學**官方網站（含各系所單位），將頁面轉換為 Markdown 格式並彙整為 CSV 清單。

---

## ✨ 功能特色

| 功能 | 說明 |
|------|------|
| 🔄 可中斷繼續 | 以 SQLite 記錄狀態，Ctrl+C 後重新執行自動續爬 |
| 🧠 智能域名判斷 | 自動偵測台藝大各系所獨立域名，不誤入外部網站 |
| 📄 Markdown 轉換 | 使用 Microsoft markitdown，支援 HTML/PDF/DOCX/XLSX/PPTX |
| 📊 CSV 清單 | 含標題、類型、連結、來源、大小等完整欄位 |
| 🌐 BFS 爬取 | 廣度優先，優先爬取淺層頁面 |
| ⚡ 速率限制 | 內建禮貌延遲，避免對伺服器造成負擔 |
| 📈 即時進度 | 終端進度條 + 完成後統計摘要 |

---

## 📁 專案結構

```
ntua_crawler/
├── main.py              # CLI 入口點
├── config.py            # 所有設定參數（修改此檔即可調整行為）
├── requirements.txt     # Python 相依套件
├── README.md
└── crawler/
    ├── __init__.py
    ├── state.py         # SQLite 狀態管理（可中斷續爬）
    ├── domain.py        # 智能域名白名單檢查器
    ├── fetcher.py       # HTTP 抓取器（重試、速率限制）
    ├── processor.py     # 內容處理（標題提取、Markdown 轉換）
    ├── spider.py        # 主爬蟲協調器
    └── reporter.py      # CSV 與摘要報表產生器
```

---

## 🚀 快速開始

### 1. 安裝相依套件

```bash
pip install -r requirements.txt
```

> **建議使用虛擬環境：**
> ```bash
> python -m venv .venv
> source .venv/bin/activate   # Windows: .venv\Scripts\activate
> pip install -r requirements.txt
> ```

### 2. 執行爬蟲

```bash
# 開始爬取（或繼續上次進度）
python main.py

# 強制清空重新爬取
python main.py --fresh

# 顯示目前進度統計（不執行爬取）
python main.py --stats

# 重新產生 CSV（不執行爬取）
python main.py --csv-only
```

### 3. 查看輸出

```
output/
├── inventory.csv     ← 所有頁面與文件的完整清單
├── state.db          ← 爬取狀態（SQLite）
├── crawler.log       ← 詳細日誌
├── pages/            ← 網頁轉換的 Markdown 檔案
│   └── www.ntua.edu.tw/
│       ├── index.md
│       ├── about/
│       │   └── index.md
│       └── ...
└── docs/             ← 文件轉換的 Markdown 檔案
    └── www.ntua.edu.tw/
        └── ...
```

---

## ⚙️ 設定說明

編輯 `config.py` 即可調整所有行為：

```python
# 起始種子網址
SEED_URLS = ["https://www.ntua.edu.tw"]

# 最大爬取深度（0 = 只爬種子頁）
MAX_DEPTH = 8

# 最多處理頁面數
MAX_PAGES = 5000

# 每次請求間隔秒數
REQUEST_DELAY = 0.8

# 是否下載並轉換文件（PDF/DOCX 等）
DOWNLOAD_DOCUMENTS = True

# 手動加入已知系所獨立域名
MANUAL_ALLOWED_DOMAINS = [
    # "artdept.example.com.tw",
]
```

---

## 📊 CSV 欄位說明

| 欄位 | 說明 |
|------|------|
| `id` | 流水號 |
| `title` | 頁面或文件標題（智能提取） |
| `type` | 類型（webpage / pdf / docx / xlsx / ...） |
| `url` | 完整連結 |
| `parent_url` | 此連結的來源頁面 |
| `depth` | 爬取深度（0 = 種子頁） |
| `status` | 狀態（done / error / skipped） |
| `content_type` | MIME 類型 |
| `file_size_kb` | 檔案大小（KB） |
| `local_path` | 本機儲存路徑（若有轉換） |
| `error_msg` | 錯誤訊息（若有） |
| `discovered_at` | 發現時間 |
| `processed_at` | 處理完成時間 |

---

## 🧠 智能域名判斷機制

爬蟲採用多層次策略判斷是否為台藝大相關網站：

1. **直接子域名**：`*.ntua.edu.tw` → 自動允許
2. **手動白名單**：`config.py` 中的 `MANUAL_ALLOWED_DOMAINS`
3. **內容關鍵字探測**：對未知域名發起請求，若頁面含有「國立臺灣藝術大學」等關鍵字達到門檻，自動加入白名單
4. **域名快取**：已判斷的域名（允許/拒絕）均快取，不重複探測

---

## ⏸️ 中斷與繼續

```bash
# 執行中按 Ctrl+C → 優雅停止（完成目前項目後停止）
python main.py

# 下次執行自動繼續：
python main.py
# 將顯示：「發現現有進度：已完成 XXX 筆，待處理 XXX 筆」
```

---

## 📋 CLI 選項

```
用法: python main.py [選項]

選項:
  --fresh, -f       清空所有舊進度，從種子 URL 重新開始
  --csv-only        不爬取，只重新輸出 inventory.csv
  --stats           顯示目前進度統計，不執行爬取
  --depth N, -d N   覆蓋最大深度（預設: 8）
  --max-pages N, -n N  覆蓋最大頁面數（預設: 5000）
  --delay SECS      覆蓋請求間隔（預設: 0.8 秒）
  --verbose, -v     顯示詳細 debug 日誌
  --help            顯示說明
```

---

## 📦 相依套件

| 套件 | 用途 |
|------|------|
| `requests` | HTTP 請求 |
| `beautifulsoup4` + `lxml` | HTML 解析 |
| `markitdown[all]` | HTML/PDF/DOCX 等轉 Markdown |
| `rich` | 終端進度條與格式化輸出 |
| `chardet` | 自動偵測網頁編碼（Big5/UTF-8）|
| `python-slugify` | 安全檔名產生（備用）|
