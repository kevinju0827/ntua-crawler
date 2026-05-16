"""
crawler/spider.py — 主要爬蟲協調器
職責：
  - 初始化各元件（State、Fetcher、DomainChecker、Processor）
  - 主爬取迴圈（支援 Ctrl+C 優雅停止）
  - 決策：每個 URL 要做什麼（爬取網頁 / 下載文件 / 跳過）
  - 定期輸出進度與統計資訊
"""
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

import config
from crawler.domain import DomainChecker
from crawler.fetcher import Fetcher, build_session, normalize_url
from crawler.processor import Processor
from crawler.reporter import generate_csv, print_summary
from crawler.state import StateManager, UrlStatus

logger = logging.getLogger(__name__)
console = Console()


class Spider:
    """
    台藝大網站爬蟲。
    使用 BFS（廣度優先）策略，優先爬取淺層頁面。

    執行方式：
        spider = Spider()
        spider.run()          # 從頭開始或繼續上次進度
        spider.run(fresh=True) # 強制清空重新爬
    """

    def __init__(self):
        # 確保輸出目錄存在
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        config.PAGES_DIR.mkdir(parents=True, exist_ok=True)
        config.DOCS_DIR.mkdir(parents=True, exist_ok=True)

        self.state     = StateManager(config.STATE_DB)
        self.session   = build_session()
        self.fetcher   = Fetcher(self.session)
        self.domain_checker = DomainChecker(self.state, self.session)
        self.processor = Processor(config.PAGES_DIR, config.DOCS_DIR)

        self._stop_flag = False
        self._processed = 0
        self._start_time = 0.0

        # 優雅停止：捕捉 SIGINT (Ctrl+C)
        signal.signal(signal.SIGINT, self._handle_interrupt)

    # ── 啟動與控制 ────────────────────────────────────────────────────────────

    def _handle_interrupt(self, signum, frame):
        console.print("\n[yellow]⚠️  收到中斷信號，完成目前項目後停止…[/yellow]")
        self._stop_flag = True

    def run(self, fresh: bool = False):
        """
        主入口。
        fresh=True 時清空 DB 並重新開始；否則繼續上次進度。
        """
        if fresh:
            if config.STATE_DB.exists():
                config.STATE_DB.unlink()
                console.print("[yellow]⚡ 已清除舊進度，重新開始[/yellow]")
            self.state = StateManager(config.STATE_DB)
            self.domain_checker = DomainChecker(self.state, self.session)

        # 重置殘留的 processing 狀態（上次異常中斷遺留）
        self.state.reset_stale_processing()

        # 種子 URL
        for url in config.SEED_URLS:
            norm = normalize_url(url)
            if self.state.add_url(norm, depth=0):
                logger.info(f"[種子] {norm}")

        pending = self.state.get_pending_count()
        if pending == 0:
            console.print("[green]✅ 所有 URL 已處理完畢，無待處理項目。[/green]")
            print_summary(self.state)
            generate_csv(self.state, config.CSV_OUTPUT)
            return

        console.print(
            Panel(
                f"[bold cyan]台藝大爬蟲啟動[/bold cyan]\n"
                f"待處理 URL: [yellow]{pending}[/yellow]\n"
                f"輸出目錄: [green]{config.OUTPUT_DIR.resolve()}[/green]\n"
                f"最大深度: {config.MAX_DEPTH}  /  最大頁面數: {config.MAX_PAGES}",
                title="🕷️  NTUA Crawler",
                border_style="blue",
            )
        )

        self._start_time = time.monotonic()
        self._crawl_loop()

        # 完成後輸出報表
        print_summary(self.state)
        count = generate_csv(self.state, config.CSV_OUTPUT)
        console.print(
            f"\n[bold green]📄 CSV 清單已儲存：[/bold green]{config.CSV_OUTPUT} "
            f"[dim]({count} 筆)[/dim]"
        )

    # ── 主迴圈 ────────────────────────────────────────────────────────────────

    def _crawl_loop(self):
        """BFS 主迴圈。"""
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        task = progress.add_task("爬取中…", total=config.MAX_PAGES)

        with progress:
            while not self._stop_flag:
                if self._processed >= config.MAX_PAGES:
                    console.print(
                        f"[yellow]⚠️  已達最大頁面數限制 ({config.MAX_PAGES})，停止爬取[/yellow]"
                    )
                    break

                item = self.state.get_next_pending()
                if item is None:
                    console.print("[green]✅ 所有 URL 已處理完畢[/green]")
                    break

                url   = item["url"]
                depth = item["depth"]

                # 超過最大深度
                if depth > config.MAX_DEPTH:
                    self.state.mark_skipped(url, f"超過最大深度 ({depth} > {config.MAX_DEPTH})")
                    continue

                progress.update(
                    task,
                    description=f"[cyan]{url[:70]}…" if len(url) > 70 else f"[cyan]{url}",
                    advance=1,
                    completed=self._processed,
                )

                self._process_url(url, depth, item.get("parent_url"))
                self._processed += 1

                # 每 50 筆自動存一次 CSV（checkpoint）
                if self._processed % 50 == 0:
                    generate_csv(self.state, config.CSV_OUTPUT)
                    stats = self.state.get_stats()
                    elapsed = time.monotonic() - self._start_time
                    rate = self._processed / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"[進度] 已處理 {self._processed} 筆 "
                        f"| 速率 {rate:.1f} 頁/秒 "
                        f"| 統計 {stats}"
                    )

    # ── 單一 URL 處理 ─────────────────────────────────────────────────────────

    def _process_url(self, url: str, depth: int, parent_url: Optional[str]):
        """處理單一 URL 的完整流程。"""

        # 1. 跳過條件檢查（協定、副檔名等）
        skip_reason = self.domain_checker.should_skip_url(url)
        if skip_reason:
            self.state.mark_skipped(url, skip_reason)
            return

        # 2. 域名白名單檢查
        if not self.domain_checker.is_allowed(url):
            self.state.mark_skipped(url, "外部網站")
            return

        # 3. 判斷是否為文件（根據副檔名）
        path_lower = urlparse(url).path.lower()
        is_document = any(path_lower.endswith(ext) for ext in config.DOCUMENT_EXTENSIONS)

        # 4. 抓取
        logger.debug(f"[抓取] {url}")
        result = self.fetcher.fetch(url, is_document=is_document)

        if not result.ok:
            self.state.mark_error(url, result.error or f"HTTP {result.status_code}")
            return

        # 5. 以最終 URL（重定向後）正規化
        final_url = normalize_url(result.url)
        if final_url != url and not self.state.is_known_url(final_url):
            self.state.add_url(final_url, depth=depth, parent_url=parent_url,
                               url_type=result.url_type)

        # 6. 依類型處理
        if result.url_type == "webpage":
            self._handle_webpage(result, depth)
        elif result.url_type in config.DOCUMENT_EXTENSIONS or is_document:
            self._handle_document(result, parent_url)
        else:
            # 其他類型：僅記錄
            self.state.mark_done(
                url,
                content_type=result.content_type,
                file_size=result.file_size,
                url_type=result.url_type,
            )

    def _handle_webpage(self, result, depth: int):
        """處理 HTML 網頁：轉 Markdown + 提取連結。"""
        try:
            title, local_path, links = self.processor.process_webpage(result)

            self.state.mark_done(
                result.original_url,
                title=title,
                local_path=local_path,
                content_type=result.content_type,
                file_size=result.file_size,
                url_type="webpage",
            )

            # 將新發現的連結加入佇列
            added = 0
            for link_url, link_text in links:
                norm = normalize_url(link_url, result.url)
                if not norm:
                    continue

                # 判斷類型
                path_lower = urlparse(norm).path.lower()
                link_type = "webpage"
                for ext, t in {
                    **{e: e.lstrip(".") for e in config.DOCUMENT_EXTENSIONS}
                }.items():
                    if path_lower.endswith(ext):
                        link_type = t
                        break

                if self.state.add_url(
                    norm,
                    depth=depth + 1,
                    parent_url=result.original_url,
                    url_type=link_type,
                    title=link_text if link_type != "webpage" else None,
                ):
                    added += 1

            logger.info(f"[✓ 網頁] {result.url[:80]} → +{added} 連結")

        except Exception as e:
            logger.exception(f"[錯誤] 處理網頁失敗: {result.url}")
            self.state.mark_error(result.original_url, str(e))

    def _handle_document(self, result, parent_url: Optional[str]):
        """處理文件：轉 Markdown + 記錄。"""
        try:
            # 取得連結文字（從 parent_url 頁面中查詢，簡化為用 URL）
            title, local_path = self.processor.process_document(result)

            self.state.mark_done(
                result.original_url,
                title=title,
                local_path=local_path,
                content_type=result.content_type,
                file_size=result.file_size,
                url_type=result.url_type,
            )
            logger.info(f"[✓ 文件] {result.url_type.upper()} — {title[:60]}")

        except Exception as e:
            logger.exception(f"[錯誤] 處理文件失敗: {result.url}")
            self.state.mark_error(result.original_url, str(e))
