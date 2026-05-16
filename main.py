#!/usr/bin/env python3
"""
main.py — 台藝大爬蟲 CLI 入口
用法：
    python main.py              # 繼續上次進度（或從頭開始）
    python main.py --fresh      # 清空重新爬取
    python main.py --csv-only   # 不爬取，只重新產生 CSV
    python main.py --stats      # 顯示目前進度統計
    python main.py --help       # 說明
"""
import argparse
import logging
import sys
from pathlib import Path

# ── 設定 logging ──────────────────────────────────────────────────────────────
def setup_logging(verbose: bool = False):
    import config

    log_level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ]

    # 終端只顯示 WARNING 以上（進度由 rich 負責），檔案記錄全部
    handlers[0].setLevel(logging.WARNING if not verbose else logging.DEBUG)
    handlers[1].setLevel(logging.DEBUG)

    logging.basicConfig(level=log_level, format=fmt, datefmt=datefmt, handlers=handlers)

    # 降低第三方套件的雜訊
    for noisy in ("urllib3", "requests", "charset_normalizer", "bs4"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(
        prog="ntua-crawler",
        description="台灣藝術大學官方網站爬蟲 — 將頁面轉為 Markdown 並彙整清單 CSV",
    )
    parser.add_argument(
        "--fresh", "-f",
        action="store_true",
        help="清空所有舊進度，從種子 URL 重新開始",
    )
    parser.add_argument(
        "--csv-only",
        action="store_true",
        help="不執行爬取，只依現有 DB 重新輸出 inventory.csv",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="顯示目前爬取進度統計，不執行爬取",
    )
    parser.add_argument(
        "--depth", "-d",
        type=int,
        default=None,
        help=f"覆蓋最大深度設定（預設 {__import__('config').MAX_DEPTH}）",
    )
    parser.add_argument(
        "--max-pages", "-n",
        type=int,
        default=None,
        help=f"覆蓋最大頁面數限制（預設 {__import__('config').MAX_PAGES}）",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="覆蓋請求間隔秒數（預設 0.8 秒）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="顯示詳細 debug 日誌",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # 動態覆蓋設定
    import config
    if args.depth is not None:
        config.MAX_DEPTH = args.depth
    if args.max_pages is not None:
        config.MAX_PAGES = args.max_pages
    if args.delay is not None:
        config.REQUEST_DELAY = args.delay

    from crawler.state import StateManager
    from crawler.reporter import generate_csv, print_summary

    # ── 純統計模式 ──────────────────────────────────────────────────────────
    if args.stats:
        if not config.STATE_DB.exists():
            print("尚無爬取記錄，請先執行爬蟲。")
            return
        state = StateManager(config.STATE_DB)
        print_summary(state)
        return

    # ── 純 CSV 輸出模式 ─────────────────────────────────────────────────────
    if args.csv_only:
        if not config.STATE_DB.exists():
            print("尚無爬取記錄，請先執行爬蟲。")
            return
        state = StateManager(config.STATE_DB)
        count = generate_csv(state, config.CSV_OUTPUT)
        print(f"✅ CSV 已輸出：{config.CSV_OUTPUT}（{count} 筆）")
        return

    # ── 主爬取模式 ──────────────────────────────────────────────────────────
    from crawler.spider import Spider

    if not args.fresh and config.STATE_DB.exists():
        state = StateManager(config.STATE_DB)
        pending = state.get_pending_count()
        stats   = state.get_stats()
        done    = stats.get("done", 0)

        from rich.console import Console
        c = Console()
        c.print(
            f"\n[bold]發現現有進度[/bold]：已完成 [green]{done}[/green] 筆，"
            f"待處理 [yellow]{pending}[/yellow] 筆"
        )
        if pending > 0:
            c.print("[dim]將繼續上次進度（使用 --fresh 重新開始）[/dim]\n")
        else:
            c.print("[dim]所有項目已完成，重新執行將重新產生報表。[/dim]\n")

    spider = Spider()
    spider.run(fresh=args.fresh)


if __name__ == "__main__":
    main()
