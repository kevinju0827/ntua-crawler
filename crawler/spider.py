"""
crawler/spider.py — 主要爬蟲協調器
職責：
  - 初始化各元件（State、Fetcher、DomainChecker、Processor）
  - 以 ThreadPoolExecutor 並發爬取（預設 4 執行緒）
  - 兩階段抓取：預設快速請求，偵測到 SPA 網頁自動切換為 Playwright 渲染
  - BFS 廣度優先：主執行緒持續從 DB 取出 pending URL 派送給工作執行緒
  - 支援 Ctrl+C 優雅停止（完成已派送的任務後停止）
  - 定期 checkpoint 輸出 CSV
"""
import logging
import re
import signal
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

import config
from crawler.domain import DomainChecker
from crawler.fetcher import Fetcher, build_session, normalize_url
from crawler.processor import Processor
from crawler.reporter import generate_csv, print_summary
from crawler.state import StateManager

logger = logging.getLogger(__name__)
console = Console()


class Spider:
    """
    台藝大網站多執行緒爬蟲。

    架構：
      - 1 個主執行緒負責調度（從 DB 取 URL、判斷域名、派送任務）
      - N 個工作執行緒（config.WORKERS）負責 HTTP 抓取 + 內容處理
      - 每個工作執行緒有獨立的 requests.Session 與速率計時器
      - 具備 _needs_js_rendering() 探測，自動啟動 Playwright 渲染前端框架
      - DomainChecker 帶鎖共享（域名探測為 I/O 密集，可並發）
      - StateManager 以 SQLite WAL 模式支援多執行緒並發寫入

    執行方式：
        spider = Spider()
        spider.run()           # 繼續上次進度（或從頭開始）
        spider.run(fresh=True) # 強制清空重新爬
    """

    def __init__(self):
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        config.PAGES_DIR.mkdir(parents=True, exist_ok=True)
        config.DOCS_DIR.mkdir(parents=True, exist_ok=True)

        self.state = StateManager(config.STATE_DB)
        self.session = build_session()  # 調度執行緒用（域名探測）
        self.fetcher = Fetcher()  # 工作執行緒各自建 Session
        self.domain_checker = DomainChecker(self.state, self.session)
        self.processor = Processor(config.PAGES_DIR, config.DOCS_DIR)

        self._stop_flag = threading.Event()
        self._processed = 0
        self._processed_lock = threading.Lock()
        self._start_time = 0.0

        signal.signal(signal.SIGINT, self._handle_interrupt)

    # ── 啟動與控制 ────────────────────────────────────────────────────────────

    def _handle_interrupt(self, signum, frame):
        if not self._stop_flag.is_set():
            console.print(
                "\n[yellow]⚠️  收到中斷信號，等待目前工作執行緒完成後停止…[/yellow]"
                "\n[dim]（再次按 Ctrl+C 強制終止）[/dim]"
            )
            self._stop_flag.set()
        else:
            console.print("\n[red]強制終止[/red]")
            raise KeyboardInterrupt

    def run(self, fresh: bool = False):
        """主入口。fresh=True 時清空 DB 重新開始。"""
        if fresh:
            if config.STATE_DB.exists():
                config.STATE_DB.unlink()
                console.print("[yellow]⚡ 已清除舊進度，重新開始[/yellow]")
            self.state = StateManager(config.STATE_DB)
            self.domain_checker = DomainChecker(self.state, self.session)

        self.state.reset_stale_processing()

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

        workers = config.WORKERS
        console.print(
            Panel(
                f"[bold cyan]台藝大爬蟲啟動[/bold cyan]\n"
                f"並發執行緒 : [magenta]{workers}[/magenta]\n"
                f"輸出目錄   : [green]{config.OUTPUT_DIR.resolve()}[/green]\n"
                f"最大深度   : {config.MAX_DEPTH}  /  最大頁面數 : {config.MAX_PAGES}\n"
                f"SSL 驗證   : {'[green]開啟[/green]' if config.SSL_VERIFY else '[red]關閉（允許不安全連線）[/red]'}\n"
                f"單檔上限   : {config.MAX_PAGE_SIZE_MB} MB",
                title="🕷️  NTUA Crawler",
                border_style="blue",
            )
        )

        self._start_time = time.monotonic()
        self._crawl_loop(workers)

        print_summary(self.state)
        count = generate_csv(self.state, config.CSV_OUTPUT)
        console.print(
            f"\n[bold green]📄 CSV 清單已儲存：[/bold green]{config.CSV_OUTPUT} "
            f"[dim]({count} 筆)[/dim]"
        )

    # ── 輔助偵測方法 ─────────────────────────────────────────────────────────

    def _needs_js_rendering(self, text: str) -> bool:
        """
        簡易啟發式檢查：判斷此 HTML 是否為典型的 CSR (客戶端渲染) 網頁。
        若是，則值得花費效能開啟 Playwright 進行渲染。
        """
        if not text:
            return False

        # 常見的前端框架掛載點或特徵
        spa_signatures = [
            '<div id="app"></div>',
            '<div id="__nuxt"></div>',
            '<div id="__next"></div>',
            'id="__NUXT_DATA__"',
            '<noscript>You need to enable JavaScript',
        ]

        # 檢查是否包含特徵
        has_spa_sig = any(sig in text for sig in spa_signatures)

        # 如果 body 的內容異常的短 (< 1000 個字元)，通常也是只有 JS 的空殼
        body_match = re.search(r'<body[^>]*>(.*?)</body>', text, re.IGNORECASE | re.DOTALL)
        is_empty_body = body_match and len(body_match.group(1).strip()) < 1000

        return has_spa_sig or (is_empty_body and "<script" in text)

    # ── 並發主迴圈 ────────────────────────────────────────────────────────────

    def _crawl_loop(self, workers: int):
        """
        調度迴圈：主執行緒持續取 URL 並派送至執行緒池。

        滑動視窗策略：
          - 在途任務上限 = workers × 4（確保執行緒池常滿，不因調度延遲空轉）
          - 主執行緒輪詢間隔 50ms，幾乎無 CPU 開銷
          - 每 50 筆完成觸發 CSV checkpoint
        """
        progress = Progress(
            SpinnerColumn(),
            TextColumn("已執行: [green]{task.completed}/{task.total}[/green]"),  # 當前與最大頁面數
            TextColumn("跳過: [yellow]{task.fields[skipped]}[/yellow]"),  # 已跳過
            TextColumn("待辦: [magenta]{task.fields[pending]}[/magenta]"),  # 待執行
            TextColumn("速度: [cyan]{task.fields[speed_str]}[/cyan]"),  # 執行速度
            TimeElapsedColumn(),
            BarColumn(bar_width=3),
            TextColumn("[bold cyan]{task.description}"),  # 當前正在派送的 URL
            console=console,
            transient=False,
        )

        # 取得初始狀態
        initial_stats = self.state.get_stats()
        initial_pending = self.state.get_pending_count()

        # 註冊進度條任務，並定義我們自訂的欄位初始值
        task_id = progress.add_task(
            "準備中...",
            total=config.MAX_PAGES,
            skipped=initial_stats.get("skipped", 0),
            pending=initial_pending,
            speed_str="0.0 頁/秒"
        )

        in_flight: set[Future] = set()
        max_in_flight = workers * 4
        last_checkpoint = 0
        latest_url = ""  # 用來記錄當前派送的網址

        with progress, ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="crawler"
        ) as pool:
            while not self._stop_flag.is_set():

                # ── 收割已完成的任務 ──
                done_futures = {f for f in in_flight if f.done()}
                for f in done_futures:
                    in_flight.discard(f)
                    try:
                        f.result()
                    except Exception as e:
                        logger.error(f"[工作執行緒例外] {e}", exc_info=True)
                    with self._processed_lock:
                        self._processed += 1

                # 檢查是否已達最大頁面數限制
                with self._processed_lock:
                    current = self._processed
                if current >= config.MAX_PAGES:
                    console.print(
                        f"[yellow]⚠️  已達最大頁面數限制 ({config.MAX_PAGES})，"
                        f"等待 {len(in_flight)} 個任務完成後停止[/yellow]"
                    )
                    self._stop_flag.set()
                    break

                # ── 補充新任務 ──
                slots = max_in_flight - len(in_flight)
                dispatched = 0
                while dispatched < slots and not self._stop_flag.is_set():
                    item = self.state.get_next_pending()
                    if item is None:
                        break

                    url = item["url"]
                    latest_url = url
                    depth = item["depth"]

                    # 超過深度限制：主執行緒直接跳過，不佔工作執行緒資源
                    if depth > config.MAX_DEPTH:
                        self.state.mark_skipped(
                            url, f"超過最大深度 ({depth} > {config.MAX_DEPTH})"
                        )
                        with self._processed_lock:
                            self._processed += 1
                        continue

                    future = pool.submit(
                        self._process_url, url, depth, item.get("parent_url")
                    )
                    in_flight.add(future)
                    dispatched += 1

                # ── 結束條件：佇列空 + 沒有在途任務 ─────────────────────
                if not in_flight and self.state.get_pending_count() == 0:
                    console.print("[green]✅ 所有 URL 已處理完畢[/green]")
                    break

                # ── 動態更新進度條資訊 ──
                elapsed = time.monotonic() - self._start_time
                speed = current / elapsed if elapsed > 0 else 0
                stats = self.state.get_stats()

                # 截斷當前網址，避免破壞終端機排版 (只顯示後 40 字元)
                if latest_url:
                    parsed_path = latest_url.split("://")[-1]
                    short_url = f"...{parsed_path[-35:]}" if len(parsed_path) > 35 else parsed_path
                else:
                    short_url = "掃描中..."

                progress.update(
                    task_id,
                    completed=current,
                    skipped=stats.get("skipped", 0),
                    pending=self.state.get_pending_count(),
                    speed_str=f"{speed:.1f} 頁/秒",
                    description=short_url
                )

                # Checkpoint：每 50 筆存檔並印出日誌
                if current - last_checkpoint >= 50 and current > 0:
                    last_checkpoint = current
                    self._checkpoint(current)

                # 迴圈極短暫休息，避免佔用 100% CPU
                time.sleep(0.05)

            # ── 結束前等待剩餘任務 ──
            if in_flight:
                console.print(f"[dim]等待 {len(in_flight)} 個進行中任務完成…[/dim]")
                for f in as_completed(in_flight):
                    try:
                        f.result()
                    except Exception as e:
                        logger.error(f"[工作執行緒例外] {e}")
                    with self._processed_lock:
                        self._processed += 1
                    progress.update(task_id, completed=self._processed)

    def _checkpoint(self, processed: int):
        generate_csv(self.state, config.CSV_OUTPUT)
        elapsed = time.monotonic() - self._start_time
        rate = processed / elapsed if elapsed > 0 else 0
        stats = self.state.get_stats()
        logger.info(
            f"[Checkpoint] 已處理 {processed} 筆 | "
            f"速率 {rate:.1f} 頁/秒 | {stats}"
        )

    # ── 單一 URL 處理（工作執行緒中執行）────────────────────────────────────

    def _process_url(self, url: str, depth: int, parent_url: Optional[str]):
        """工作執行緒執行的完整處理流程。所有呼叫的方法均為執行緒安全。"""

        # 1. 跳過條件（協定、副檔名等）
        skip_reason = self.domain_checker.should_skip_url(url)
        if skip_reason:
            self.state.mark_skipped(url, skip_reason)
            return

        # 2. 域名白名單（含自動探測；DomainChecker 內部有鎖）
        if not self.domain_checker.is_allowed(url):
            self.state.mark_skipped(url, "外部網站")
            return

        # 3. 判斷是否為文件副檔名
        path_lower = urlparse(url).path.lower()
        is_document = any(path_lower.endswith(ext) for ext in config.DOCUMENT_EXTENSIONS)

        # 4. 抓取（每執行緒獨立 Session，速率計時器互不干擾）
        logger.debug(f"[抓取] {url}")
        result = self.fetcher.fetch(url, is_document=is_document)

        if not result.ok:
            self.state.mark_error(url, result.error or f"HTTP {result.status_code}")
            return

        # 5. SPA 動態渲染檢測（如果網頁內容特徵顯示為前端框架，則切換至 Playwright 渲染）
        if result.url_type == "webpage" and result.text:
            if self._needs_js_rendering(result.text):
                logger.info(f"[SPA 偵測] 發現前端渲染特徵，啟動無頭瀏覽器重抓: {url}")
                try:
                    rendered_result = self.fetcher.fetch_rendered(url)
                    if rendered_result.ok:
                        result = rendered_result
                except Exception as e:
                    logger.warning(f"[渲染跳過] 無法使用瀏覽器渲染 ({e})，退回靜態內容: {url}")

        # 6. 處理重定向後的最終 URL
        final_url = normalize_url(result.url)
        if final_url != url and not self.state.is_known_url(final_url):
            self.state.add_url(
                final_url, depth=depth, parent_url=parent_url,
                url_type=result.url_type,
            )

        # 7. 依類型分流
        doc_type_set = {e.lstrip(".") for e in config.DOCUMENT_EXTENSIONS}
        if result.url_type == "webpage":
            self._handle_webpage(result, depth)
        elif is_document or result.url_type in doc_type_set:
            self._handle_document(result)
        else:
            self.state.mark_done(
                url,
                content_type=result.content_type,
                file_size=result.file_size,
                url_type=result.url_type,
            )

    def _handle_webpage(self, result, depth: int):
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

            added = 0
            for link_url, link_text in links:
                norm = normalize_url(link_url, result.url)
                if not norm:
                    continue
                lp = urlparse(norm).path.lower()
                link_type = next(
                    (e.lstrip(".") for e in config.DOCUMENT_EXTENSIONS if lp.endswith(e)),
                    "webpage",
                )
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

    def _handle_document(self, result):
        try:
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
