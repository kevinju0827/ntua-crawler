"""
crawler/domain.py — 智能域名檢查器
核心邏輯：判斷一個 URL 是否屬於台藝大的網路範疇。

策略（優先序）：
  1. 若域名為 ntua.edu.tw 或其子域名 → 直接允許
  2. 若域名在手動白名單 → 直接允許
  3. 若域名已被自動偵測加入白名單 → 允許
  4. 若域名為新的未知域名 → 下載頁面並檢查內容關鍵字
     → 達到門檻則加入白名單；否則標記為外部網站
"""
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import requests

import config
from crawler.state import StateManager

logger = logging.getLogger(__name__)

# 已確認為外部網站的域名（快取，避免重複請求）
_rejected_domains: set[str] = set()


def _extract_domain(url: str) -> Optional[str]:
    """從 URL 中提取域名（不含 port）。"""
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return None


def _is_ntua_subdomain(domain: str) -> bool:
    """判斷是否為 ntua.edu.tw 或其子域名。"""
    return domain == config.BASE_NTUA_DOMAIN or \
           domain.endswith("." + config.BASE_NTUA_DOMAIN)


def _is_manual_allowed(domain: str) -> bool:
    """判斷是否在手動白名單中。"""
    return domain in config.MANUAL_ALLOWED_DOMAINS


def _count_ntua_keywords(text: str) -> int:
    """計算頁面文字中台藝大關鍵字出現次數。"""
    count = 0
    text_lower = text.lower()
    for kw in config.NTUA_CONTENT_KEYWORDS:
        if kw.lower() in text_lower:
            count += 1
    return count


class DomainChecker:
    """
    智能域名檢查器，搭配 StateManager 共用白名單快取。
    採用 requests.Session 複用連線，偵測速度更快。
    """

    def __init__(self, state: StateManager, session: requests.Session):
        self.state = state
        self.session = session
        # 啟動時把已存 DB 的白名單載入記憶體快取
        self._allowed_cache: set[str] = set(state.get_allowed_domains())
        # 初始化基礎域名與手動白名單
        self._seed_allowed()

    def _seed_allowed(self):
        """將基礎域名與手動白名單寫入 DB 與快取。"""
        self.state.add_allowed_domain(config.BASE_NTUA_DOMAIN, "base")
        self._allowed_cache.add(config.BASE_NTUA_DOMAIN)
        for d in config.MANUAL_ALLOWED_DOMAINS:
            self.state.add_allowed_domain(d, "manual")
            self._allowed_cache.add(d)

    def _add_to_whitelist(self, domain: str):
        logger.info(f"[域名] 新增白名單: {domain}")
        self.state.add_allowed_domain(domain, "auto-detected")
        self._allowed_cache.add(domain)

    def is_allowed(self, url: str) -> bool:
        """
        主要公開方法：判斷此 URL 是否允許爬取。
        對未知域名會發起一次 HEAD/GET 請求進行內容探測。
        """
        domain = _extract_domain(url)
        if not domain:
            return False

        # 快速路徑 1：ntua 子域名
        if _is_ntua_subdomain(domain):
            if domain not in self._allowed_cache:
                self._add_to_whitelist(domain)
            return True

        # 快速路徑 2：手動白名單
        if _is_manual_allowed(domain):
            if domain not in self._allowed_cache:
                self._add_to_whitelist(domain)
            return True

        # 快速路徑 3：已快取的允許域名
        if domain in self._allowed_cache:
            return True

        # 快速路徑 4：已確認的外部域名
        if domain in _rejected_domains:
            return False

        # 未知域名：進行內容探測
        return self._probe_domain(url, domain)

    def _probe_domain(self, url: str, domain: str) -> bool:
        """
        對未知域名的首頁發起請求，檢查頁面內容是否含台藝大關鍵字。
        為避免干擾正常爬取，設定較短的超時。
        """
        probe_url = f"https://{domain}/"
        logger.debug(f"[域名探測] {domain}")
        try:
            resp = self.session.get(
                probe_url,
                timeout=10,
                allow_redirects=True,
                stream=False,
            )
            # 追蹤重定向後的最終域名
            final_domain = _extract_domain(resp.url)
            if final_domain and _is_ntua_subdomain(final_domain):
                self._add_to_whitelist(domain)
                if final_domain != domain:
                    self._add_to_whitelist(final_domain)
                return True

            text = resp.text[:50_000]  # 只看前 50KB
            hit = _count_ntua_keywords(text)
            if hit >= config.AFFILIATE_KEYWORD_THRESHOLD:
                logger.info(
                    f"[域名探測] {domain} 命中 {hit} 個關鍵字 → 加入白名單"
                )
                self._add_to_whitelist(domain)
                return True
            else:
                logger.debug(f"[域名探測] {domain} 非台藝大關聯網站 (命中 {hit})")
                _rejected_domains.add(domain)
                return False
        except Exception as e:
            logger.debug(f"[域名探測] {domain} 探測失敗: {e}")
            _rejected_domains.add(domain)
            return False

    def should_skip_url(self, url: str) -> Optional[str]:
        """
        除域名外的其他跳過條件。
        回傳跳過原因字串；若允許則回傳 None。
        """
        parsed = urlparse(url)

        # 只允許 http / https
        if parsed.scheme not in ("http", "https"):
            return f"非 HTTP 協定: {parsed.scheme}"

        # 忽略錨點連結
        if parsed.fragment and not parsed.path and not parsed.query:
            return "純錨點連結"

        # 副檔名過濾
        path_lower = parsed.path.lower()
        for ext in config.IGNORE_EXTENSIONS:
            if path_lower.endswith(ext):
                return f"忽略副檔名: {ext}"

        return None  # 允許
