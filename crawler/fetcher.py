"""
crawler/fetcher.py — HTTP 抓取器
功能：
  - requests.Session 複用（Keep-Alive、Cookie 持久）
  - 自動重試（指數退避）
  - 每執行緒獨立速率限制（並發時各自計時，互不干擾）
  - 大型回應保護（超過設定大小則拒絕）
  - MIME 類型解析（判斷是網頁還是文件）
  - SSL 憑證驗證開關（支援憑證過期的系所網站）
"""
import logging
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

import chardet
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── 文件副檔名 → 類型標籤對應表 ──────────────────────────────────────────────
EXT_TO_TYPE: dict[str, str] = {
    ".pdf": "pdf",
    ".doc": "doc", ".docx": "docx",
    ".xls": "xls", ".xlsx": "xlsx",
    ".ppt": "ppt", ".pptx": "pptx",
    ".odt": "odt", ".ods": "ods", ".odp": "odp",
    ".zip": "zip", ".rar": "rar", ".7z": "7z",
    ".csv": "csv", ".txt": "txt",
}

MIME_TO_TYPE: dict[str, str] = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


@dataclass
class FetchResult:
    url: str  # 實際抓取的 URL（可能經重定向）
    original_url: str  # 原始請求 URL
    status_code: int
    content_type: str
    content: Optional[bytes]
    text: Optional[str]
    file_size: int
    url_type: str  # 'webpage' / 'pdf' / 'docx' ...
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 400


def build_session() -> requests.Session:
    """
    建立配有重試策略的 Session。
    若 config.SSL_VERIFY = False，則停用 SSL 驗證並壓制 InsecureRequestWarning。
    """
    if not config.SSL_VERIFY:
        # 全域壓制不安全連線警告（避免每次請求都印出警告洗版）
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")

    session = requests.Session()
    retry = Retry(
        total=config.MAX_RETRIES,
        backoff_factor=1.0,
        status_forcelist={429, 500, 502, 503, 504},
        allowed_methods={"GET", "HEAD"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(config.HEADERS)
    session.verify = config.SSL_VERIFY
    return session


def normalize_url(url: str, base: Optional[str] = None) -> str:
    """
    正規化 URL：
      - 補全相對路徑（base 不為 None 時）
      - 移除錨點（#fragment）
      - 統一 scheme 為小寫
    """
    if base:
        url = urljoin(base, url)
    parsed = urlparse(url)
    # 移除 fragment
    clean = parsed._replace(fragment="")
    return urlunparse(clean)


def _detect_type_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path.lower()
    for ext, t in EXT_TO_TYPE.items():
        if path.endswith(ext):
            return t
    return None


def _detect_type_from_mime(content_type: str) -> str:
    mime = content_type.split(";")[0].strip().lower()
    if mime in MIME_TO_TYPE:
        return MIME_TO_TYPE[mime]
    if "html" in mime or "xhtml" in mime:
        return "webpage"
    return "other"


def _decode_text(content: bytes, content_type: str) -> str:
    """嘗試多種編碼解碼 bytes→str，優先依 Content-Type 指定。"""
    # 從 Content-Type 取 charset
    charset = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"').strip("'")
            break

    # 嘗試順序：宣告編碼 → chardet 自動偵測 → utf-8 → big5
    candidates = []
    if charset:
        candidates.append(charset)
    detected = chardet.detect(content[:8192])
    if detected.get("encoding"):
        candidates.append(detected["encoding"])
    candidates += ["utf-8", "big5", "gb2312", "latin-1"]

    for enc in candidates:
        try:
            return content.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


# 每個執行緒獨立維護自己的最後請求時間（threading.local）
_thread_local = threading.local()


class Fetcher:
    """
    帶速率限制的 HTTP 抓取器。
    多執行緒安全：每個執行緒有獨立的 Session 與速率計時器，
    彼此的延遲互不干擾，可真正並發發出請求。
    """

    def __init__(self, session: Optional[requests.Session] = None):
        # session 參數為向下相容保留，多執行緒模式下每個執行緒自建 Session
        self._shared_session = session
        self._lock = threading.Lock()

    def _get_session(self) -> requests.Session:
        """
        取得當前執行緒專屬的 Session（懶建立）。
        使用「執行緒 id + Fetcher 物件 id」作為 key，
        確保即使 OS 執行緒被快速回收再建立時，
        新執行緒不會誤用舊 Session。
        """
        key = f"session_{id(self)}"
        if not hasattr(_thread_local, key):
            setattr(_thread_local, key, build_session())
        return getattr(_thread_local, key)

    def _rate_limit(self):
        """
        確保「同一執行緒」距離上次請求至少間隔 REQUEST_DELAY 秒。
        不同執行緒使用各自的計時器，互不阻塞。
        """
        last = getattr(_thread_local, "last_request_time", 0.0)
        elapsed = time.monotonic() - last
        if elapsed < config.REQUEST_DELAY:
            time.sleep(config.REQUEST_DELAY - elapsed)
        _thread_local.last_request_time = time.monotonic()

    def fetch(self, url: str, is_document: bool = False) -> FetchResult:
        """
        抓取單一 URL 並回傳 FetchResult。
        is_document=True 時會下載完整 bytes（用於文件轉換）。
        """
        self._rate_limit()
        session = self._get_session()
        max_bytes = (
            config.MAX_DOC_SIZE_MB * 1024 * 1024 if is_document
            else config.MAX_PAGE_SIZE_MB * 1024 * 1024
        )

        type_hint = _detect_type_from_url(url)
        try:
            resp = session.get(
                url,
                timeout=config.REQUEST_TIMEOUT,
                allow_redirects=True,
                stream=True,
            )

            content_type = resp.headers.get("Content-Type", "")
            url_type = type_hint or _detect_type_from_mime(content_type)

            # 大小限制
            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > max_bytes:
                logger.warning(f"[跳過] 檔案過大 ({content_length / 1024 / 1024:.1f} MB): {url}")
                return FetchResult(
                    url=resp.url, original_url=url,
                    status_code=resp.status_code,
                    content_type=content_type,
                    content=None, text=None, file_size=content_length,
                    url_type=url_type,
                    error=f"檔案過大 ({content_length} bytes)",
                )

            # 流式讀取（帶大小上限）
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(f"[截斷] 超過大小限制: {url}")
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
            file_size = len(content)

            text = None
            if url_type == "webpage":
                text = _decode_text(content, content_type)

            return FetchResult(
                url=resp.url, original_url=url,
                status_code=resp.status_code,
                content_type=content_type,
                content=content,
                text=text,
                file_size=file_size,
                url_type=url_type,
            )

        except requests.exceptions.Timeout:
            return FetchResult(
                url=url, original_url=url, status_code=0,
                content_type="", content=None, text=None, file_size=0,
                url_type=type_hint or "webpage", error="請求超時",
            )
        except requests.exceptions.ConnectionError as e:
            return FetchResult(
                url=url, original_url=url, status_code=0,
                content_type="", content=None, text=None, file_size=0,
                url_type=type_hint or "webpage", error=f"連線失敗: {e}",
            )
        except Exception as e:
            return FetchResult(
                url=url, original_url=url, status_code=0,
                content_type="", content=None, text=None, file_size=0,
                url_type=type_hint or "webpage", error=str(e),
            )

    def head(self, url: str) -> Optional[requests.Response]:
        """快速檢查 URL 是否可達（不下載 body）。"""
        self._rate_limit()
        try:
            return self._get_session().head(
                url, timeout=10, allow_redirects=True,
                verify=config.SSL_VERIFY,
            )
        except Exception:
            return None

    def _get_playwright_browser(self):
        """
        取得當前執行緒專屬的 Playwright 瀏覽器實例（懶建立）。
        避免每次請求都重開瀏覽器，節省大量效能。
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("未安裝 Playwright，無法使用渲染抓取。")

        key_pw = f"playwright_{id(self)}"
        key_browser = f"browser_{id(self)}"

        if not hasattr(_thread_local, key_pw):
            # 啟動 Playwright 並儲存在執行緒本地變數
            pw = sync_playwright().start()
            setattr(_thread_local, key_pw, pw)
            # 啟動 Chromium (無頭模式)，可以根據需求關閉圖片載入以加速
            browser = pw.chromium.launch(headless=True)
            setattr(_thread_local, key_browser, browser)

        return getattr(_thread_local, key_browser)

    def fetch_rendered(self, url: str) -> FetchResult:
        """
        使用真實瀏覽器 (Playwright) 開啟網頁，等待 JS 渲染完畢後再擷取 HTML。
        專門用來對付 Vue, React, Nuxt 等 CSR 網站。
        """
        self._rate_limit()

        try:
            browser = self._get_playwright_browser()
            # 建立新的瀏覽器上下文與分頁
            context = browser.new_context(
                ignore_https_errors=not config.SSL_VERIFY,
                user_agent=config.HEADERS.get("User-Agent")
            )
            page = context.new_page()

            logger.info(f"[渲染抓取] 正在渲染頁面: {url}")

            # 導航至頁面，等待網路閒置 (networkidle 代表 JS 已經抓完大部分 API 資料)
            response = page.goto(
                url,
                wait_until="networkidle",
                timeout=config.REQUEST_TIMEOUT * 1000  # Playwright 使用毫秒
            )

            # 取得渲染後的完整 HTML
            html_content = page.content()
            status_code = response.status if response else 200

            # 清理資源
            page.close()
            context.close()

            return FetchResult(
                url=page.url,
                original_url=url,
                status_code=status_code,
                content_type="text/html; charset=utf-8",
                content=html_content.encode("utf-8"),
                text=html_content,
                file_size=len(html_content),
                url_type="webpage"
            )

        except Exception as e:
            logger.error(f"[渲染抓取失敗] {url}: {e}")
            return FetchResult(
                url=url, original_url=url, status_code=0,
                content_type="", content=None, text=None, file_size=0,
                url_type="webpage", error=str(e),
            )
