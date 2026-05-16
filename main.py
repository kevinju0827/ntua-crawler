#!/usr/bin/env python3
"""
main.py — 台藝大爬蟲 CLI 入口
用法：
    python main.py                    # 繼續上次進度（或從頭開始）
    python main.py --fresh            # 清空重新爬取
    python main.py --csv-only         # 不爬取，只重新產生 CSV
    python main.py --stats            # 顯示目前進度統計
    python main.py --workers 8        # 使用 8 個並發執行緒
    python main.py --no-ssl-verify    # 允許不安全 SSL 連線
    python main.py --help             # 說明
"""
import argparse
import logging


# ── 設定 logging ──────────────────────────────────────────────────────────────
def setup_logging(verbose: bool = False):
    import config
    from rich.logging import RichHandler

    log_level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    handlers = [
        RichHandler(
            level=logging.WARNING if not verbose else logging.DEBUG,
            show_time=True,
            show_level=True,
            omit_repeated_times=False,
            rich_tracebacks=True
        ),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ]
    # 終端只顯示 WARNING 以上（進度由 rich 負責），檔案記錄全部
    handlers[0].setLevel(logging.WARNING if not verbose else logging.DEBUG)
    handlers[1].setLevel(logging.DEBUG)

    logging.basicConfig(
        level=log_level, format=fmt, datefmt=datefmt, handlers=handlers
    )

    for noisy in ("urllib3", "requests", "charset_normalizer", "bs4", "playwright"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def main():
    import config  # 先載入，才能讀取預設值顯示於 help

    parser = argparse.ArgumentParser(
        prog="ntua-crawler",
        description="台灣藝術大學官方網站爬蟲 — 將頁面轉為 Markdown 並彙整清單 CSV (具備 SPA/CSR 動態網頁自動渲染能力)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python main.py                        # 繼續上次進度
  python main.py --fresh                # 清空重新開始
  python main.py --workers 8            # 8 執行緒加速
  python main.py --no-ssl-verify        # 允許憑證過期的系所網站
  python main.py --depth 5 --delay 0.3  # 限深度 5 層，加快請求間隔
  python main.py --stats                # 查看目前爬取進度
  python main.py --csv-only             # 重新輸出 CSV（不爬取）
        """,
    )

    # ── 模式選擇 ──────────────────────────────────────────────────────────────
    mode = parser.add_argument_group("執行模式")
    mode.add_argument(
        "--fresh", "-f",
        action="store_true",
        help="清空所有舊進度，從種子 URL 重新開始",
    )
    mode.add_argument(
        "--csv-only",
        action="store_true",
        help="不執行爬取，只依現有 DB 重新輸出 inventory.csv",
    )
    mode.add_argument(
        "--stats",
        action="store_true",
        help="顯示目前爬取進度統計，不執行爬取",
    )

    # ── 爬蟲參數 ──────────────────────────────────────────────────────────────
    crawl = parser.add_argument_group("爬蟲參數（覆蓋 config.py 設定）")
    crawl.add_argument(
        "--workers", "-w",
        type=int,
        default=None,
        metavar="N",
        help=f"並發執行緒數量（預設 {config.WORKERS}）",
    )
    crawl.add_argument(
        "--depth", "-d",
        type=int,
        default=None,
        metavar="N",
        help=f"最大爬取深度（預設 {config.MAX_DEPTH}，0 = 只爬種子頁）",
    )
    crawl.add_argument(
        "--max-pages", "-n",
        type=int,
        default=None,
        metavar="N",
        help=f"最多處理頁面數（預設 {config.MAX_PAGES}）",
    )
    crawl.add_argument(
        "--delay",
        type=float,
        default=None,
        metavar="秒",
        help=f"每個執行緒的請求間隔秒數（預設 {config.REQUEST_DELAY}）",
    )
    crawl.add_argument(
        "--page-size",
        type=int,
        default=None,
        metavar="MB",
        help=f"單頁最大下載大小 MB（預設 {config.MAX_PAGE_SIZE_MB}）",
    )

    # ── SSL 設定 ──────────────────────────────────────────────────────────────
    ssl_group = parser.add_argument_group("SSL / 安全性")
    ssl_group.add_argument(
        "--no-ssl-verify",
        action="store_true",
        help=(
            "停用 SSL 憑證驗證，允許連線至憑證過期或自簽名的系所網站。"
            "⚠️  僅建議在受信任的學術網路環境中使用。"
        ),
    )

    # ── 其他 ──────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="顯示詳細 debug 日誌（同時輸出至終端與 crawler.log）",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # ── 動態覆蓋 config 設定 ──────────────────────────────────────────────────
    if args.workers is not None:
        if args.workers < 1:
            parser.error("--workers 必須 >= 1")
        config.WORKERS = args.workers

    if args.depth is not None:
        config.MAX_DEPTH = args.depth

    if args.max_pages is not None:
        if args.max_pages < 1:
            parser.error("--max-pages 必須 >= 1")
        config.MAX_PAGES = args.max_pages

    if args.delay is not None:
        if args.delay < 0:
            parser.error("--delay 不可為負數")
        config.REQUEST_DELAY = args.delay

    if args.page_size is not None:
        if args.page_size < 1:
            parser.error("--page-size 必須 >= 1")
        config.MAX_PAGE_SIZE_MB = args.page_size
        config.MAX_DOC_SIZE_MB = args.page_size

    if args.no_ssl_verify:
        config.SSL_VERIFY = False

    # ── 純統計模式 ────────────────────────────────────────────────────────────
    if args.stats:
        if not config.STATE_DB.exists():
            print("尚無爬取記錄，請先執行爬蟲。")
            return
        from crawler.state import StateManager
        from crawler.reporter import print_summary
        print_summary(StateManager(config.STATE_DB))
        return

    # ── 純 CSV 輸出模式 ───────────────────────────────────────────────────────
    if args.csv_only:
        if not config.STATE_DB.exists():
            print("尚無爬取記錄，請先執行爬蟲。")
            return
        from crawler.state import StateManager
        from crawler.reporter import generate_csv
        state = StateManager(config.STATE_DB)
        count = generate_csv(state, config.CSV_OUTPUT)
        print(f"✅ CSV 已輸出：{config.CSV_OUTPUT}（{count} 筆）")
        return

    # ── 主爬取模式 ────────────────────────────────────────────────────────────
    from crawler.spider import Spider
    from crawler.state import StateManager
    from rich.console import Console
    c = Console()

    if not args.fresh and config.STATE_DB.exists():
        state = StateManager(config.STATE_DB)
        stats = state.get_stats()
        done = stats.get("done", 0)
        pending = state.get_pending_count()

        c.print(
            f"\n[bold]發現現有進度[/bold]：已完成 [green]{done}[/green] 筆，"
            f"待處理 [yellow]{pending}[/yellow] 筆"
        )
        if pending > 0:
            c.print("[dim]將繼續上次進度（使用 --fresh 重新開始）[/dim]\n")
        else:
            c.print("[dim]所有項目已完成，將重新產生報表。[/dim]\n")

    Spider().run(fresh=args.fresh)


if __name__ == "__main__":
    main()
