"""
crawler/state.py — SQLite 狀態管理器
支援隨時中斷、重新繼續的爬蟲核心。
所有 URL 狀態、已探索域名白名單均持久化至 state.db。
"""
import sqlite3
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class UrlStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"
    EXTERNAL = "external"


SCHEMA = """
         CREATE TABLE IF NOT EXISTS urls
         (
             id            INTEGER PRIMARY KEY AUTOINCREMENT,
             url           TEXT UNIQUE NOT NULL,
             title         TEXT,
             type          TEXT    DEFAULT 'webpage', -- webpage / pdf / docx / xlsx ...
             content_type  TEXT,                      -- MIME type
             status        TEXT    DEFAULT 'pending',
             depth         INTEGER DEFAULT 0,
             file_size     INTEGER,
             parent_url    TEXT,
             local_path    TEXT,                      -- 本機儲存路徑
             error_msg     TEXT,
             discovered_at TEXT,
             processed_at  TEXT,
             retry_count   INTEGER DEFAULT 0
         );

         CREATE TABLE IF NOT EXISTS allowed_domains
         (
             domain   TEXT PRIMARY KEY,
             source   TEXT, -- 'base' | 'manual' | 'auto-detected'
             added_at TEXT
         );

         CREATE INDEX IF NOT EXISTS idx_urls_status ON urls (status);
         CREATE INDEX IF NOT EXISTS idx_urls_depth ON urls (depth, id);
         CREATE INDEX IF NOT EXISTS idx_urls_type ON urls (type); \
         """


class StateManager:
    """
    執行緒安全的 SQLite 狀態管理器。
    每次 DB 操作使用獨立 Connection（避免跨執行緒共用），
    WAL 模式允許多個 reader + 1 writer 並存，大幅提升並發效能。
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # ── 私有工具 ─────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ── URL 操作 ─────────────────────────────────────────────────────────────

    def add_url(
            self,
            url: str,
            depth: int = 0,
            parent_url: Optional[str] = None,
            url_type: str = "webpage",
            title: Optional[str] = None,
    ) -> bool:
        """
        新增 URL 至待爬佇列。
        若已存在（不論狀態）則忽略，回傳 False；成功插入回傳 True。
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO urls
                       (url, depth, parent_url, type, title, status, discovered_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (url, depth, parent_url, url_type, title, self._now()),
            )
            return conn.total_changes > 0

    def get_next_pending(self) -> Optional[dict]:
        """
        以廣度優先（BFS）取出下一個待處理 URL，
        並原子性地將狀態改為 'processing'。
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT *
                   FROM urls
                   WHERE status = 'pending'
                   ORDER BY depth, id
                   LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE urls SET status = 'processing' WHERE id = ?",
                (row["id"],),
            )
            return dict(row)

    def mark_done(
            self,
            url: str,
            title: Optional[str] = None,
            local_path: Optional[str] = None,
            content_type: Optional[str] = None,
            file_size: Optional[int] = None,
            url_type: Optional[str] = None,
    ):
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE urls
                   SET status       = 'done',
                       title        = COALESCE(?, title),
                       local_path   = COALESCE(?, local_path),
                       content_type = COALESCE(?, content_type),
                       file_size    = COALESCE(?, file_size),
                       type         = COALESCE(?, type),
                       processed_at = ?
                   WHERE url = ?""",
                (title, local_path, content_type, file_size, url_type,
                 self._now(), url),
            )

    def mark_error(self, url: str, error_msg: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE urls
                   SET status      = 'error',
                       error_msg   = ?,
                       retry_count = retry_count + 1,
                       processed_at= ?
                   WHERE url = ?""",
                (error_msg, self._now(), url),
            )

    def mark_skipped(self, url: str, reason: Optional[str] = None):
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE urls
                   SET status      = 'skipped',
                       error_msg   = ?,
                       processed_at= ?
                   WHERE url = ?""",
                (reason, self._now(), url),
            )

    def reset_stale_processing(self):
        """
        程式異常中斷後，將殘留的 'processing' 狀態重置為 'pending'，
        確保下次繼續時不漏失這些 URL。
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE urls SET status = 'pending' WHERE status = 'processing'"
            )

    def is_known_url(self, url: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM urls WHERE url = ?", (url,)
            ).fetchone()
            return row is not None

    def get_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM urls GROUP BY status"
            ).fetchall()
            return {r[0]: r[1] for r in rows}

    def get_pending_count(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM urls WHERE status = 'pending'"
            ).fetchone()[0]

    def get_all_records(self) -> list[dict]:
        """取出全部記錄，用於最終輸出 CSV。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM urls ORDER BY depth , id "
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 域名白名單 ────────────────────────────────────────────────────────────

    def add_allowed_domain(self, domain: str, source: str = "auto-detected"):
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO allowed_domains (domain, source, added_at)
                   VALUES (?, ?, ?)""",
                (domain, source, self._now()),
            )

    def get_allowed_domains(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT domain FROM allowed_domains").fetchall()
            return [r[0] for r in rows]

    def has_allowed_domain(self, domain: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM allowed_domains WHERE domain = ?", (domain,)
            ).fetchone()
            return row is not None
