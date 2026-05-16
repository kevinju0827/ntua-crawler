"""
crawler/reporter.py — 報表產生器
產生：
  1. inventory.csv — 所有已探索 URL 的清單
  2. 爬取摘要（統計資訊）
"""
import csv
import logging
from datetime import datetime
from pathlib import Path

from crawler.state import StateManager

logger = logging.getLogger(__name__)

# CSV 欄位定義
CSV_FIELDS = [
    "id",
    "title",        # 頁面或文件標題
    "type",         # webpage / pdf / docx / xlsx / ...
    "url",          # 完整連結
    "parent_url",   # 來源頁面
    "depth",        # 爬取深度
    "status",       # done / error / skipped / external
    "content_type", # MIME type
    "file_size_kb", # 大小（KB，四捨五入）
    "local_path",   # 本機儲存路徑（若有）
    "error_msg",    # 錯誤訊息（若有）
    "discovered_at",
    "processed_at",
]


def generate_csv(state: StateManager, output_path: Path) -> int:
    """
    將 StateManager 中的所有記錄輸出為 CSV。
    回傳總列數。
    """
    records = state.get_all_records()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        # utf-8-sig 讓 Excel 正確辨識 BOM
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()

        for rec in records:
            row = dict(rec)
            # 計算 KB
            size = row.get("file_size") or 0
            row["file_size_kb"] = round(size / 1024, 1) if size else ""
            writer.writerow(row)

    logger.info(f"[CSV] 輸出完成：{output_path}（{len(records)} 筆）")
    return len(records)


def print_summary(state: StateManager):
    """在終端顯示爬取統計摘要（使用 rich）。"""
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
    except ImportError:
        _plain_summary(state)
        return

    stats = state.get_stats()
    total = sum(stats.values())

    table = Table(title="📊 台藝大爬蟲 — 執行摘要", show_header=True)
    table.add_column("狀態", style="bold")
    table.add_column("數量", justify="right")
    table.add_column("百分比", justify="right")

    status_labels = {
        "done":       "✅ 成功",
        "error":      "❌ 錯誤",
        "skipped":    "⏭  跳過",
        "pending":    "⏳ 待處理",
        "processing": "🔄 處理中",
        "external":   "🌐 外部",
    }

    for status, label in status_labels.items():
        count = stats.get(status, 0)
        if count:
            pct = f"{count/total*100:.1f}%" if total else "-"
            table.add_row(label, str(count), pct)

    table.add_row("─" * 8, "─" * 6, "─" * 7)
    table.add_row("[bold]合計[/bold]", f"[bold]{total}[/bold]", "100%")

    console.print()
    console.print(table)

    # 依類型細分（只看 done 的）
    records = state.get_all_records()
    done = [r for r in records if r["status"] == "done"]
    type_counts: dict[str, int] = {}
    for r in done:
        t = r.get("type") or "other"
        type_counts[t] = type_counts.get(t, 0) + 1

    if type_counts:
        t2 = Table(title="📂 成功項目類型分佈")
        t2.add_column("類型", style="cyan")
        t2.add_column("數量", justify="right")
        for t, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
            t2.add_row(t, str(cnt))
        console.print(t2)
        console.print()


def _plain_summary(state: StateManager):
    stats = state.get_stats()
    print("\n===== 爬取摘要 =====")
    for status, count in stats.items():
        print(f"  {status:12s}: {count}")
    print("=" * 20)
