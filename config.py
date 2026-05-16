"""
config.py — 台藝大爬蟲設定檔
所有可調整的參數集中於此，修改後重新執行即可生效。
"""
from pathlib import Path

# ── 起始種子網址 ──────────────────────────────────────────────────────────────
SEED_URLS: list[str] = [
    "https://www.ntua.edu.tw",
]

# ── 台藝大核心域名 ─────────────────────────────────────────────────────────────
# 以此為基準，所有子網域皆自動允許
BASE_NTUA_DOMAIN = "ntua.edu.tw"

# 手動加入的關聯域名（已知的系所外部域名）
MANUAL_ALLOWED_DOMAINS: list[str] = [
    # 若已知某系所使用獨立域名，可手動補充於此，例如：
    # "artdept.example.com.tw",
]

# 頁面內容中用來判斷是否為台藝大關聯網站的關鍵字
NTUA_CONTENT_KEYWORDS: list[str] = [
    "國立臺灣藝術大學",
    "國立台灣藝術大學",
    "臺灣藝術大學",
    "台灣藝術大學",
    "台藝大",
    "臺藝大",
    "ntua.edu.tw",
    "NTUA",
]

# 若外部域名頁面中出現 N 個以上關鍵字，視為台藝大關聯網站並加入白名單
AFFILIATE_KEYWORD_THRESHOLD = 2

# ── 文件類型定義 ──────────────────────────────────────────────────────────────
# 這些副檔名會列入 CSV 清單，但不作為 HTML 繼續爬取
DOCUMENT_EXTENSIONS: set[str] = {
    ".pdf", ".doc", ".docx",
    ".xls", ".xlsx",
    ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    ".zip", ".rar", ".7z",
    ".csv", ".txt",
}

# 這些 MIME type 視為可轉 Markdown 的文件
CONVERTIBLE_MIME_TYPES: set[str] = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# 完全忽略的副檔名（圖片、影音、字體等）
IGNORE_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".mp4", ".avi", ".mov", ".wmv", ".mp3", ".wav", ".ogg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".exe", ".dmg", ".pkg", ".deb", ".rpm",
    ".json", ".xml", ".rss", ".atom",
}

# ── 輸出目錄設定 ──────────────────────────────────────────────────────────────
OUTPUT_DIR    = Path("output")
PAGES_DIR     = OUTPUT_DIR / "pages"    # 網頁轉換的 Markdown
DOCS_DIR      = OUTPUT_DIR / "docs"     # 下載的文件轉換的 Markdown
STATE_DB      = OUTPUT_DIR / "state.db" # SQLite 狀態資料庫
CSV_OUTPUT    = OUTPUT_DIR / "inventory.csv"
LOG_FILE      = OUTPUT_DIR / "crawler.log"

# ── 並發設定 ──────────────────────────────────────────────────────────────────
WORKERS             = 4          # 同時執行的爬取執行緒數量

# ── 爬蟲行為設定 ──────────────────────────────────────────────────────────────
MAX_DEPTH           = 999        # 最大爬取深度（預設近乎無限，僅由 MAX_PAGES 兜底）
MAX_PAGES           = 500_000    # 最多處理頁面數（防止磁碟爆滿的最終保護）
REQUEST_DELAY       = 0.8        # 每個執行緒各自的請求間隔秒數（禮貌延遲）
REQUEST_TIMEOUT     = 30         # 單一請求超時秒數
MAX_RETRIES         = 2          # 失敗重試次數
MAX_PAGE_SIZE_MB    = 512        # 單頁最大下載大小（MB，涵蓋絕大多數網頁與文件）

# 下載文件並轉換為 Markdown（True=下載並轉換, False=只記錄於 CSV）
DOWNLOAD_DOCUMENTS  = True
MAX_DOC_SIZE_MB     = 512        # 文件最大下載大小（MB，與頁面共用上限）

# ── SSL 設定 ──────────────────────────────────────────────────────────────────
# 設為 False 時忽略 SSL 憑證錯誤（允許連線至憑證過期/自簽名的系所網站）
# 警告：此選項會降低安全性，僅建議在受信任的學術網路環境中使用
SSL_VERIFY          = True

# ── HTTP 設定 ─────────────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (compatible; NTUACrawler/1.0; +https://www.ntua.edu.tw)"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
