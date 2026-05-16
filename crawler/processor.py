"""
crawler/processor.py — 內容處理器

標題系統設計：
  - 多來源收集候選標題（HTML meta、標題標籤、markitdown 標題、連結文字、URL）
  - 依評分選出最佳標題（不強制刪除特定文字，只做正規化）
  - 若所有候選均低於門檻，從 URL 生成識別性高的自訂名稱
"""
import hashlib
import io
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import unicodedata
from bs4 import BeautifulSoup

import config
from crawler.fetcher import FetchResult

logger = logging.getLogger(__name__)

# ── markitdown 引入（帶降級處理）────────────────────────────────────────────
try:
    from markitdown import MarkItDown

    # 每執行緒獨立實例，避免多執行緒共享狀態問題
    _md_local = threading.local()


    def _get_md() -> "MarkItDown":
        if not hasattr(_md_local, "instance"):
            _md_local.instance = MarkItDown()
        return _md_local.instance


    MARKITDOWN_AVAILABLE = True
except ImportError:
    MARKITDOWN_AVAILABLE = False
    logger.warning("markitdown 未安裝，將以 BeautifulSoup 純文字模式儲存頁面內容")


# ════════════════════════════════════════════════════════════════════════════
# 標題候選系統
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class TitleCandidate:
    """一個標題候選，攜帶來源資訊以供評分。"""
    text: str  # 正規化後的文字
    source: str  # 來源識別（用於 debug）
    base_score: float  # 來源基礎分（反映來源可信度）
    score: float = field(default=0.0, init=False)  # 最終評分（calculate_score 後填入）


# 各來源基礎分——反映「這個來源的標題通常有多可靠」
_SOURCE_BASE: dict[str, float] = {
    "og:title": 7.0,  # 網站作者明確設定的社群分享標題
    "markitdown:h1": 7.0,  # markitdown 從內容取出的第一個 # 標題
    "content_disposition": 7.0,  # HTTP 回應標頭中的顯式檔名
    "twitter:title": 6.5,
    "link_text": 6.0,  # 指向此文件的連結文字（人工撰寫）
    "html:title": 6.0,  # <title> 常帶有網站名稱後綴，需靠評分區分
    "markitdown:doc_h1": 6.0,  # 文件內容第一個標題
    "html:h1": 4.0,  # HTML 第一個 h1
    "html:h2": 4.0,
    "url:filename": 3.0,  # URL 最後一段去副檔名
    "url:path": 2.0,  # URL 路徑組合
}


def _normalize(raw: str) -> str:
    """基本正規化：Unicode NFKC、折疊空白、去頭尾空白。不刪除任何內容文字。"""
    text = unicodedata.normalize("NFKC", raw)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _cjk_count(text: str) -> int:
    """計算 CJK（中日韓）字元數量。"""
    return sum(
        1 for c in text
        if "\u4e00" <= c <= "\u9fff"  # CJK 統一表意文字
        or "\u3400" <= c <= "\u4dbf"  # CJK 擴展 A
        or "\uf900" <= c <= "\ufaff"  # CJK 相容表意文字
        or "\u3000" <= c <= "\u303f"  # CJK 符號與標點
    )


def _alnum_ratio(text: str) -> float:
    """文字中字母數字（含 CJK）佔比，衡量可讀性。"""
    if not text:
        return 0.0
    readable = sum(1 for c in text if c.isalnum() or _cjk_count(c) > 0)
    return readable / len(text)


# 用於記錄已出現過的最終標題，避免大量重複
_seen_titles_lock = threading.Lock()
_seen_titles_count: dict[str, int] = {}


def _register_seen_title(title: str):
    """註冊已被選用的標題，用於後續扣分"""
    with _seen_titles_lock:
        _seen_titles_count[title] = _seen_titles_count.get(title, 0) + 1


def calculate_score(candidate: TitleCandidate) -> float:
    """
    計算標題候選的總分，填入 candidate.score 並回傳。

    評分維度：
      1. 來源基礎分（已記錄於 base_score）
      2. 長度甜蜜點（8–70 字元最佳）
      3. CJK 字元密度（中文學術網站加分）
      4. 可讀性（可讀字元佔比低則扣分）
      5. 路徑 / URL 特徵扣分（包含斜線、http 等）
      6. 無效特徵扣分（純數字、純符號）
      7. 通用無意義詞扣分（index、首頁、下載 等）
    """
    text = candidate.text
    score = candidate.base_score
    length = len(text)

    # ── 重複出現扣分 ──────────────────────────────────
    with _seen_titles_lock:
        seen_count = _seen_titles_count.get(text, 0)

    if seen_count > 0:
        score -= (seen_count * 5.0)

    # ── 長度評分 ─────────────────────────────────────────
    if length == 0:
        candidate.score = -99.0
        return candidate.score
    elif length < 2:
        score -= 8
    elif length < 4:
        score -= 4
    elif length <= 7:
        score += 0  # 短但可接受
    elif length <= 70:
        score += 3  # 甜蜜點
    elif length <= 120:
        score += 1
    elif length <= 200:
        score -= 1
    else:
        score -= 3  # 過長，可能是摘要或雜訊

    # ── CJK 密度 ─────────────────────────────────────────
    cjk = _cjk_count(text)
    score += min(cjk * 0.4, 4.0)

    # ── 可讀性 ───────────────────────────────────────────
    ratio = _alnum_ratio(text)
    if ratio < 0.25:
        score -= 3  # 大量非字母數字（如 ----、====）
    elif ratio < 0.45:
        score -= 1

    # ── URL / 路徑特徵 ───────────────────────────────────
    if re.search(r"https?://", text):
        score -= 5
    elif "/" in text:
        score -= 2  # 路徑片段（如 about/index）
    if text.startswith(("www.", "http")):
        score -= 3

    # ── 純數字或純標點 ───────────────────────────────────
    if re.fullmatch(r"[\d\s]+", text):
        score -= 5  # 純數字：流水號、時間戳等
    if re.fullmatch(r"[^\w\u4e00-\u9fff]+", text):
        score -= 5  # 純符號

    # ── 通用無意義詞 ─────────────────────────────────────
    _GENERIC_WORDS = {
        "index", "index.html", "index.php", "index.asp", "index.aspx",
        "default", "default.html", "default.asp", "home", "main",
        "page", "pages", "untitled", "noname",
        "首頁", "網站首頁", "下載", "更多", "詳細", "點此",
        "click here", "read more", "more", "detail", "details",
    }
    if text.lower() in _GENERIC_WORDS:
        score -= 5  # 壓到門檻以下（除非來源基礎分極高）

    candidate.score = round(score, 2)
    return candidate.score


def _make(raw: str, source: str) -> Optional[TitleCandidate]:
    """正規化文字後建立候選；空字串則回傳 None。"""
    text = _normalize(raw)
    if not text:
        return None
    base = _SOURCE_BASE.get(source, 2.0)
    c = TitleCandidate(text=text, source=source, base_score=base)
    calculate_score(c)
    return c


def _first_md_heading(md_text: str) -> Optional[str]:
    """從 Markdown 文字中提取第一個 # 標題（任何層級）。"""
    for line in md_text.splitlines():
        m = re.match(r"^#{1,4}\s+(.+)", line)
        if m:
            return m.group(1).strip()
    return None


# ════════════════════════════════════════════════════════════════════════════
# URL 備用名稱生成（當所有候選分數均低於門檻時使用）
# ════════════════════════════════════════════════════════════════════════════

# 無意義的路徑節點（常見的 CMS / 框架預設路由）
_MEANINGLESS_SEGMENTS = frozenset({
    "index", "index.html", "index.php", "index.asp", "index.aspx",
    "default", "default.html", "default.asp", "default.aspx",
    "home", "main", "page", "pages", "content", "view",
    "zh-tw", "zh_tw", "zh", "tw", "cn", "en",
    "www", "web", "site",
})


def _readable_segments(path: str) -> list[str]:
    """
    將 URL path 轉成可讀的片段列表。
      - URL 解碼（%XX → 中文字等）
      - 去除副檔名
      - 過濾無意義節點（在替換分隔符前後各過濾一次）
      - 將 - 和 _ 替換為空格
    """
    decoded = unquote(path)
    parts = [p for p in decoded.split("/") if p]
    result = []
    for part in parts:
        # 去副檔名（先取 stem 再過濾，避免 index.html 漏網）
        stem_raw = re.sub(r"\.\w{1,6}$", "", part)
        # 先以原始 stem（含 - _）對照過濾集
        if stem_raw.lower() in _MEANINGLESS_SEGMENTS:
            continue
        if part.lower() in _MEANINGLESS_SEGMENTS:
            continue
        # 替換分隔符
        stem = re.sub(r"[-_]", " ", stem_raw).strip()
        # 替換後再過濾一次（如 zh-tw → zh tw 仍需被捨棄）
        if stem.lower() in _MEANINGLESS_SEGMENTS:
            continue
        if stem:
            result.append(stem)
    return result


def generate_url_title(url: str, url_type: str = "webpage") -> str:
    """
    從 URL 生成識別性高的備用名稱。

    策略：
      取路徑最後 3 個有意義片段，以「 / 」連接。
      若路徑完全無意義，嘗試 query string，最後用 域名 + hash。

    範例：
      https://www.ntua.edu.tw/zh-tw/about/index.html  → 「about」
      https://www.ntua.edu.tw/dep/music/intro.html    → 「dep / music / intro」
      https://www.ntua.edu.tw/upload/file/12345.pdf   → 「upload / file / 12345.pdf」
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or "unknown"
    short_host = re.sub(r"^www\.", "", hostname)

    if url_type != "webpage":
        # 文件：保留完整檔名（含副檔名）以確保識別性
        decoded = unquote(parsed.path)
        filename = decoded.rstrip("/").split("/")[-1]
        segments = _readable_segments("/".join(decoded.split("/")[:-1]))
        context = " / ".join(segments[-2:]) if segments else ""
        if context:
            return _normalize(f"{context} / {filename}")
        return _normalize(filename) or f"{short_host}_{hashlib.md5(url.encode()).hexdigest()[:6]}"

    # 網頁：取最後 3 個有意義片段
    segments = _readable_segments(parsed.path)
    if not segments:
        # 完全無有意義路徑，嘗試用 query string 關鍵值
        if parsed.query:
            q_pairs = [unquote(p) for p in parsed.query.split("&")
                       if not re.fullmatch(r"[a-z_]+=\d+", p)]  # 排除純 id=數字
            if q_pairs:
                q = " ".join(q_pairs)[:50]
                return _normalize(f"{short_host} {q}") or short_host
        h = hashlib.md5(url.encode()).hexdigest()[:6]
        return f"{short_host}_{h}"

    tail = segments[-3:]
    return _normalize(" / ".join(tail))


# ════════════════════════════════════════════════════════════════════════════
# 主要公開函式
# ════════════════════════════════════════════════════════════════════════════

# 採用某個來源的最低分門檻（低於此分的候選一律捨棄，改用 URL 備用名稱）
TITLE_SCORE_THRESHOLD = 4.0


def _pick_best(
        candidates: list[Optional[TitleCandidate]],
        url: str,
        url_type: str = "webpage",
) -> tuple[str, str]:
    """
    從候選列表選出最高分者。
    回傳 (title, source_debug_info)。
    若最高分仍低於門檻，改用 generate_url_title()。
    """
    valid = [c for c in candidates if c is not None]
    if not valid:
        t = generate_url_title(url, url_type)
        _register_seen_title(t)
        return t, "url_fallback(no candidates)"

    best = max(valid, key=lambda c: c.score)

    if best.score < TITLE_SCORE_THRESHOLD:
        t = generate_url_title(url, url_type)
        logger.debug(
            f"[標題] 所有候選分數不足（最高 {best.score:.1f} < {TITLE_SCORE_THRESHOLD}），"
            f"使用 URL 備用名稱: {t!r}"
        )
        _register_seen_title(t)
        return t, f"url_fallback(best_was:{best.source}={best.score:.1f})"

    logger.debug(f"[標題] 選用 {best.source}（{best.score:.1f} 分）: {best.text!r}")
    _register_seen_title(best.text)
    return best.text, best.source


def extract_title(
        soup: BeautifulSoup,
        url: str,
        md_text: Optional[str] = None,
) -> str:
    """
    網頁標題提取（評分系統）。

    收集來源（全部評分後選最高分）：
      og:title → twitter:title → markitdown 第一個 # 標題
      → html <title> → html <h1> → html <h2>
    若所有候選均低於 TITLE_SCORE_THRESHOLD，使用 generate_url_title()。

    Args:
        soup:    BeautifulSoup 解析後的物件
        url:     頁面 URL（用於評分參考與備用名稱生成）
        md_text: markitdown 轉換後的 Markdown 文字（可選，用於提取 # 標題）
    """
    candidates: list[Optional[TitleCandidate]] = []

    # 1. og:title
    tag = soup.find("meta", attrs={"property": "og:title"})
    if tag and tag.get("content"):
        candidates.append(_make(tag["content"], "og:title"))

    # 2. twitter:title
    tag = soup.find("meta", attrs={"name": "twitter:title"})
    if tag and tag.get("content"):
        candidates.append(_make(tag["content"], "twitter:title"))

    # 3. markitdown 轉換後的第一個 # 標題（最接近「文件實際標題」的內容）
    if md_text:
        heading = _first_md_heading(md_text)
        if heading:
            candidates.append(_make(heading, "markitdown:h1"))

    # 4. HTML <title>
    tag = soup.find("title")
    if tag:
        candidates.append(_make(tag.get_text(), "html:title"))

    # 5. HTML <h1>（第一個）
    tag = soup.find("h1")
    if tag:
        candidates.append(_make(tag.get_text(), "html:h1"))

    # 6. HTML <h2>（第一個，作為補充）
    tag = soup.find("h2")
    if tag:
        candidates.append(_make(tag.get_text(), "html:h2"))

    title, _src = _pick_best(candidates, url, "webpage")
    return title


def derive_document_title(
        url: str,
        link_text: Optional[str] = None,
        content_disposition: Optional[str] = None,
        md_text: Optional[str] = None,
) -> str:
    """
    文件標題提取（PDF / DOCX / XLSX / …，評分系統）。

    收集來源（全部評分後選最高分）：
      content_disposition 檔名 → link_text → markitdown 第一個標題 → URL 檔名

    Args:
        url:                 文件 URL
        link_text:           指向此文件的連結文字（若有）
        content_disposition: HTTP Content-Disposition 標頭值（若有）
        md_text:             markitdown 轉換後的 Markdown 文字（若有）
    """
    candidates: list[Optional[TitleCandidate]] = []

    # 1. Content-Disposition: attachment; filename="...XXX..."
    if content_disposition:
        # 支援 filename*=UTF-8''... 與 filename="..." 兩種格式
        m = re.search(r"filename\*=UTF-8''(.+)", content_disposition, re.IGNORECASE)
        if m:
            candidates.append(_make(unquote(m.group(1).strip()), "content_disposition"))
        else:
            m = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', content_disposition, re.IGNORECASE)
            if m:
                candidates.append(_make(m.group(1).strip(), "content_disposition"))

    # 2. 連結文字
    if link_text:
        candidates.append(_make(link_text, "link_text"))

    # 3. markitdown 轉換後第一個 # 標題
    if md_text:
        heading = _first_md_heading(md_text)
        if heading:
            candidates.append(_make(heading, "markitdown:doc_h1"))

    # 4. URL 檔名（含副檔名，保留以維持識別性）
    raw_path = unquote(urlparse(url).path)
    filename = raw_path.rstrip("/").split("/")[-1]
    if filename:
        candidates.append(_make(filename, "url:filename"))

    title, _src = _pick_best(candidates, url, "document")
    return title


# ════════════════════════════════════════════════════════════════════════════
# 連結提取（保留使用者自訂擴充：Vue/Nuxt 路由、data 屬性、script JSON、meta URL）
# ════════════════════════════════════════════════════════════════════════════

def extract_links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    """
    從 BeautifulSoup 物件中提取所有連結。
    支援標準 HTML、Vue/Nuxt 路由標籤、自定義 data 屬性以及腳本內的狀態資料。
    回傳 [(url, link_text), ...]。
    """
    links: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add_link(url_candidate: str, text: str = ""):
        """輔助函式：清理網址並過濾重複項目"""
        if not url_candidate or not isinstance(url_candidate, str):
            return
        url_candidate = url_candidate.strip()
        if not url_candidate or url_candidate.startswith(("javascript:", "mailto:", "tel:", "data:")):
            return
        full_url = urljoin(base_url, url_candidate).split("#")[0]
        if full_url and full_url not in seen:
            seen.add(full_url)
            links.append((full_url, text.strip()[:100]))

    # 1. 掃描標準與非標準的導航標籤
    target_tags = soup.find_all([
        "a", "area", "router-link", "nuxt-link",
        "div", "button", "li", "span",
    ])
    for tag in target_tags:
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

    # 3. Meta 標籤（Canonical Links 或 Open Graph URL）
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
            matches = re.findall(
                r'(?:href|url|to)["\']?\s*:\s*["\']([^"\']+)["\']',
                script.string,
                re.IGNORECASE,
            )
            for match in matches:
                if len(match) > 1 and not match.startswith(("{", "[")):
                    add_link(match, "script-data")

    return links


# ════════════════════════════════════════════════════════════════════════════
# 安全檔案名稱
# ════════════════════════════════════════════════════════════════════════════

def safe_filename(text: str, max_len: int = 80) -> str:
    """
    將任意字串轉為安全檔案名稱，保留 CJK 字元。
    替換 OS 保留字元為底線，合併連續底線。
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("_. ")
    return (text[:max_len] if len(text) > max_len else text) or "untitled"


def url_to_filepath(url: str, base_dir: Path, suffix: str = ".md") -> Path:
    """
    將 URL 映射為本機檔案路徑（保留目錄結構）。
    以 URL 的 MD5 前 8 碼作為唯一後綴，同一 URL 永遠對應同一路徑，
    徹底解決多執行緒 TOCTOU 競態問題，不需要 exists() 判斷。
    """
    parsed = urlparse(url)
    host = safe_filename(parsed.hostname or "unknown")
    path_parts = [safe_filename(p) for p in parsed.path.split("/") if p]
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]

    if path_parts:
        dir_parts = path_parts[:-1]
        stem = path_parts[-1] or f"index_{url_hash}"
    else:
        dir_parts = []
        stem = f"index_{url_hash}"

    dest_dir = base_dir / host / Path(*dir_parts) if dir_parts else base_dir / host
    dest_dir.mkdir(parents=True, exist_ok=True)

    # stem + hash 後綴，保證唯一且可重現，不需要 exists() 判斷
    return dest_dir / f"{stem}_{url_hash}{suffix}"


# ════════════════════════════════════════════════════════════════════════════
# Markdown 轉換
# ════════════════════════════════════════════════════════════════════════════

def html_to_markdown(html: str, url: str) -> str:
    """
    HTML → Markdown（使用 markitdown convert_stream，不產生暫存檔）。
    降級：BeautifulSoup 純文字（已過濾 script / style / noscript）。
    """
    if not MARKITDOWN_AVAILABLE:
        return _html_to_text_fallback(html)
    try:
        result = _get_md().convert_stream(
            io.BytesIO(html.encode("utf-8", errors="replace")),
            file_extension=".html",
            url=url,
        )
        return result.text_content or ""
    except Exception as e:
        logger.debug(f"[markitdown HTML 轉換失敗] {url}: {e}")
        return _html_to_text_fallback(html)


def _html_to_text_fallback(html: str) -> str:
    """BeautifulSoup 降級方案：去除 script / style / noscript 後取純文字。"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def bytes_to_markdown(content: bytes, url: str, url_type: str) -> Optional[str]:
    """
    二進位文件（PDF / DOCX / …）→ Markdown（使用 markitdown convert_stream）。
    """
    if not MARKITDOWN_AVAILABLE:
        return None

    ext_map = {
        "pdf": ".pdf", "doc": ".doc", "docx": ".docx",
        "xls": ".xls", "xlsx": ".xlsx",
        "ppt": ".ppt", "pptx": ".pptx",
        "odt": ".odt", "ods": ".ods", "odp": ".odp",
    }
    ext = ext_map.get(url_type, "")

    try:
        result = _get_md().convert_stream(
            io.BytesIO(content),
            file_extension=ext,
            url=url,
        )
        return result.text_content or None
    except Exception as e:
        logger.debug(f"[markitdown 文件轉換失敗] {url}: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# 主要處理器類別
# ════════════════════════════════════════════════════════════════════════════

class Processor:
    """
    處理單一抓取結果：
      - 網頁 → 提取標題（評分系統）、連結、轉 Markdown
      - 文件 → 提取標題（評分系統）、轉 Markdown（若設定啟用）
    """

    def __init__(self, pages_dir: Path, docs_dir: Path):
        self.pages_dir = pages_dir
        self.docs_dir = docs_dir
        pages_dir.mkdir(parents=True, exist_ok=True)
        docs_dir.mkdir(parents=True, exist_ok=True)

    def process_webpage(
            self, result: FetchResult
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """
        處理網頁。先轉 Markdown，再用 Markdown 的第一個標題輔助評分。
        回傳 (title, local_path, [(link_url, link_text), ...])
        """
        html = result.text or ""
        soup = BeautifulSoup(html, "lxml")

        # 先轉換 Markdown（讓 markitdown:h1 候選也能參與標題評分）
        md = html_to_markdown(html, result.url)

        title = extract_title(soup, result.url, md_text=md)
        links = extract_links(soup, result.url)

        header = (
            f"# {title}\n\n"
            f"**來源：** {result.url}\n\n"
            f"---\n\n"
        )
        dest = url_to_filepath(result.url, self.pages_dir, ".md")
        dest.write_text(header + md, encoding="utf-8")
        logger.debug(f"[網頁] 儲存 → {dest.name}")

        return title, str(dest), links

    def process_document(
            self,
            result: FetchResult,
            link_text: Optional[str] = None,
            content_disposition: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        """
        處理文件。若能轉換 Markdown，先轉換再用其第一個標題輔助命名。
        回傳 (title, local_path or None)
        """
        md: Optional[str] = None
        if config.DOWNLOAD_DOCUMENTS and result.content:
            md = bytes_to_markdown(result.content, result.url, result.url_type)

        title = derive_document_title(
            url=result.url,
            link_text=link_text,
            content_disposition=content_disposition,
            md_text=md,
        )

        if md:
            header = (
                f"# {title}\n\n"
                f"**來源：** {result.url}\n"
                f"**類型：** {result.url_type.upper()}\n\n"
                f"---\n\n"
            )
            dest = url_to_filepath(result.url, self.docs_dir, ".md")
            dest.write_text(header + md, encoding="utf-8")
            logger.debug(f"[文件] 轉換完成 → {dest.name}")
            return title, str(dest)

        return title, None
