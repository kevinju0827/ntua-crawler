"""
crawler/processor.py — 內容處理器
功能：
  - 從 HTML 提取連結（<a>、<frame>、<iframe>）
  - 智能提取頁面標題（og:title → <title> → <h1> → URL 路徑）
  - 使用 markitdown 將 HTML / PDF / Word 等轉換為 Markdown
  - 安全的檔案名稱產生（避免路徑穿越、特殊字元）
"""
import hashlib
import io
import logging
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import config
from crawler.fetcher import FetchResult

logger = logging.getLogger(__name__)

# ── markitdown 引入（帶降級處理）────────────────────────────────────────────
try:
    from markitdown import MarkItDown
    _md_converter = MarkItDown()
    MARKITDOWN_AVAILABLE = True
except ImportError:
    MARKITDOWN_AVAILABLE = False
    logger.warning("markitdown 未安裝，將以純文字模式儲存頁面內容")


# ── 標題提取 ─────────────────────────────────────────────────────────────────

def _clean_title(raw: str) -> str:
    """清理標題：去除多餘空白、換行，截斷過長文字。"""
    title = re.sub(r"\s+", " ", raw).strip()
    return title[:120] if len(title) > 120 else title


def extract_title(soup: BeautifulSoup, url: str, fallback: Optional[str] = None) -> str:
    """
    依優先序提取最佳標題：
    1. og:title (Open Graph)
    2. <title> 標籤
    3. <h1> 標籤
    4. URL 路徑最後一段
    5. fallback 參數
    6. URL 本身
    """
    # 1. og:title
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return _clean_title(og["content"])

    # 2. <title>
    title_tag = soup.find("title")
    if title_tag and title_tag.get_text(strip=True):
        return _clean_title(title_tag.get_text(strip=True))

    # 3. <h1>
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return _clean_title(h1.get_text(strip=True))

    # 4. URL 路徑
    path = urlparse(url).path.rstrip("/")
    if path:
        segment = path.split("/")[-1]
        segment = re.sub(r"\.\w{2,4}$", "", segment)  # 去副檔名
        segment = re.sub(r"[-_]", " ", segment).strip()
        if segment:
            return _clean_title(segment)

    return fallback or url


def derive_document_title(url: str, link_text: Optional[str] = None) -> str:
    """
    為文件（PDF/DOCX/…）產生可讀標題。
    優先使用連結文字，其次為檔案名稱，最後自動命名。
    """
    # 1. 連結文字
    if link_text:
        text = _clean_title(link_text)
        if len(text) > 3:
            return text

    # 2. URL 檔案名
    path = urlparse(url).path
    filename = path.split("/")[-1]
    if filename:
        # 去副檔名
        stem = re.sub(r"\.\w{2,5}$", "", filename)
        stem = re.sub(r"[-_]", " ", stem).strip()
        # 如果只是流水號或沒有意義，加上來源路徑輔助
        if re.fullmatch(r"\d+", stem) or len(stem) < 3:
            parent = "/".join(path.split("/")[-3:-1])
            stem = f"{parent}/{filename}" if parent else filename
        return _clean_title(stem)

    # 3. 哈希命名
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"文件_{h}"


# ── 連結提取 ─────────────────────────────────────────────────────────────────

def extract_links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    """
    從 BeautifulSoup 物件中提取所有連結。
    支援標準 HTML、Vue/Nuxt 路由標籤、自定義 data 屬性以及腳本內的狀態資料。
    回傳 [(url, link_text), ...]。
    """
    links = []
    seen = set()

    def add_link(url_candidate: str, text: str = ""):
        """輔助函式：清理網址並過濾重複項目"""
        if not url_candidate or not isinstance(url_candidate, str):
            return
        url_candidate = url_candidate.strip()

        # 過濾空值與非導航用途的協定
        if not url_candidate or url_candidate.startswith(("javascript:", "mailto:", "tel:", "data:")):
            return

        # 組合成絕對路徑並移除 fragment (錨點)
        full_url = urljoin(base_url, url_candidate).split("#")[0]
        if full_url and full_url not in seen:
            seen.add(full_url)
            # 限制連結文字長度，避免抓到整段文章
            clean_text = text.strip()[:100]
            links.append((full_url, clean_text))

    # 1. 掃描標準與非標準的導航標籤
    target_tags = soup.find_all(["a", "area", "router-link", "nuxt-link", "div", "button", "li", "span"])

    for tag in target_tags:
        # 尋找各種可能儲存 URL 的屬性
        href = (
                tag.get("href") or
                tag.get("to") or  # Vue / Nuxt 路由
                tag.get("data-href") or  # 自定義屬性
                tag.get("data-url") or
                tag.get("data-link")
        )
        if href:
            add_link(href, tag.get_text(strip=True))

    # 2. Frame 與 Iframe 資源
    for tag in soup.find_all(["frame", "iframe"], src=True):
        src = tag.get("src")
        if src and src.strip().startswith(("http://", "https://", "/")):
            add_link(src, f"iframe (title: {tag.get('title', 'N/A')})")

    # 3. Meta 標籤 (例如 Canonical Links 或 Open Graph URL)
    for tag in soup.find_all("meta"):
        prop = tag.get("property", "").lower()
        name = tag.get("name", "").lower()
        if "url" in prop or "url" in name:
            content = tag.get("content")
            if content:
                add_link(content, f"meta ({prop or name})")

    # 4. 從 <script> 中挖出隱藏在 JSON 狀態中的連結
    for script in soup.find_all("script"):
        if script.string:
            # 尋找鍵值為 href, url, to 後面的字串
            matches = re.findall(r'(?:href|url|to)["\']?\s*:\s*["\']([^"\']+)["\']', script.string, re.IGNORECASE)
            for match in matches:
                if len(match) > 1 and not match.startswith(("{", "[")):
                    add_link(match, "script-data")

    return links


# ── 安全檔案名 ───────────────────────────────────────────────────────────────

def safe_filename(text: str, max_len: int = 80) -> str:
    """
    將任意字串轉換為安全的檔案名稱。
    保留 CJK 字元（台灣中文），替換特殊字元為底線。
    """
    # Unicode 正規化
    text = unicodedata.normalize("NFKC", text)
    # 替換路徑分隔符與危險字元
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    # 多個底線合併
    text = re.sub(r"_+", "_", text)
    text = text.strip("_. ")
    if len(text) > max_len:
        text = text[:max_len]
    return text or "untitled"


def url_to_filepath(url: str, base_dir: Path, suffix: str = ".md") -> Path:
    """
    將 URL 映射為本機檔案路徑，保留目錄結構。
    例：https://www.ntua.edu.tw/about/index.html
        → base_dir/www.ntua.edu.tw/about/index.html.md
    """
    parsed = urlparse(url)
    host = safe_filename(parsed.hostname or "unknown")
    path_parts = [p for p in parsed.path.split("/") if p]

    if path_parts:
        # 目錄部分
        dir_parts = path_parts[:-1]
        filename   = safe_filename(path_parts[-1])
    else:
        dir_parts = []
        filename  = "index"

    # 若 filename 太短或為空，使用 query hash
    if not filename or filename in (".", ".."):
        h = hashlib.md5(url.encode()).hexdigest()[:8]
        filename = f"page_{h}"

    dest_dir = base_dir / host / Path(*dir_parts) if dir_parts else base_dir / host
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 避免衝突
    candidate = dest_dir / (filename + suffix)
    if candidate.exists():
        h = hashlib.md5(url.encode()).hexdigest()[:6]
        candidate = dest_dir / (f"{filename}_{h}" + suffix)
    return candidate


# ── Markdown 轉換 ─────────────────────────────────────────────────────────────

def html_to_markdown(html: str, url: str) -> str:
    """
    HTML → Markdown（使用 markitdown）。
    若 markitdown 不可用，退回 BeautifulSoup 純文字。
    """
    if not MARKITDOWN_AVAILABLE:
        soup = BeautifulSoup(html, "lxml")
        return soup.get_text(separator="\n", strip=True)

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".html", mode="w", encoding="utf-8", delete=False
        ) as f:
            f.write(html)
            tmp_path = f.name

        result = _md_converter.convert(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)
        return result.text_content or ""
    except Exception as e:
        logger.debug(f"[markitdown HTML 轉換失敗] {url}: {e}")
        soup = BeautifulSoup(html, "lxml")
        return soup.get_text(separator="\n", strip=True)


def bytes_to_markdown(content: bytes, url: str, url_type: str) -> Optional[str]:
    """
    二進位文件（PDF/DOCX/…）→ Markdown（使用 markitdown）。
    """
    if not MARKITDOWN_AVAILABLE:
        return None

    ext_map = {
        "pdf":  ".pdf",
        "doc":  ".doc",  "docx": ".docx",
        "xls":  ".xls",  "xlsx": ".xlsx",
        "ppt":  ".ppt",  "pptx": ".pptx",
        "odt":  ".odt",  "ods":  ".ods",  "odp": ".odp",
    }
    ext = ext_map.get(url_type, ".bin")

    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(content)
            tmp_path = f.name

        result = _md_converter.convert(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)
        return result.text_content or None
    except Exception as e:
        logger.debug(f"[markitdown 文件轉換失敗] {url}: {e}")
        return None


# ── 主要處理流程 ───────────────────────────────────────────────────────────────

class Processor:
    """
    處理單一抓取結果：
      - 網頁 → 提取標題、連結、轉 Markdown
      - 文件 → 提取標題、轉 Markdown（若設定啟用）
    """

    def __init__(self, pages_dir: Path, docs_dir: Path):
        self.pages_dir = pages_dir
        self.docs_dir  = docs_dir
        pages_dir.mkdir(parents=True, exist_ok=True)
        docs_dir.mkdir(parents=True, exist_ok=True)

    def process_webpage(
        self, result: FetchResult
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """
        處理網頁。
        回傳 (title, local_path, [(link_url, link_text), ...])
        """
        soup = BeautifulSoup(result.text or "", "lxml")
        title = extract_title(soup, result.url)
        links = extract_links(soup, result.url)
        md    = html_to_markdown(result.text or "", result.url)

        # 加入頁面頭部 metadata
        header = (
            f"# {title}\n\n"
            f"**來源：** {result.url}\n\n"
            f"---\n\n"
        )
        final_md = header + md

        # 儲存
        dest = url_to_filepath(result.url, self.pages_dir, ".md")
        dest.write_text(final_md, encoding="utf-8")
        logger.debug(f"[網頁] 儲存 → {dest}")

        return title, str(dest), links

    def process_document(
        self,
        result: FetchResult,
        link_text: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        """
        處理文件。
        回傳 (title, local_path or None)
        """
        title = derive_document_title(result.url, link_text)

        if not config.DOWNLOAD_DOCUMENTS or not result.content:
            return title, None

        # 嘗試轉換為 Markdown
        md = bytes_to_markdown(result.content, result.url, result.url_type)
        if md:
            header = (
                f"# {title}\n\n"
                f"**來源：** {result.url}\n"
                f"**類型：** {result.url_type.upper()}\n\n"
                f"---\n\n"
            )
            final_md = header + md
            dest = url_to_filepath(result.url, self.docs_dir, ".md")
            dest.write_text(final_md, encoding="utf-8")
            logger.debug(f"[文件] 轉換完成 → {dest}")
            return title, str(dest)

        return title, None
