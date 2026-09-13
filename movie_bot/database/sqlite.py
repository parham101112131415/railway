"""ماژول پایگاه داده SQLite با قفل‌گذاری امن.

مدیریت اتصال‌ها، ساخت جدول‌ها و عملیات پایه.
از `threading.Lock` برای جلوگیری از تداخل در نوشتن همزمان استفاده می‌شود.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from config import DB_PATH
from utils.logger import logger

# قفل سراسری برای اتصال‌های همزمان
_lock = threading.RLock()
_db_connection: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """بازگرداندن اتصال سراسری پایگاه داده (یک‌بار ساخته می‌شود)."""
    global _db_connection
    if _db_connection is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db_connection = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db_connection.row_factory = sqlite3.Row
        _db_connection.execute("PRAGMA journal_mode=WAL;")
        _db_connection.execute("PRAGMA foreign_keys=ON;")
    return _db_connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Context manager برای اجرای تراکنش امن با قفل."""
    conn = get_connection()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    """ساخت تمام جدول‌های مورد نیاز در صورت عدم وجود."""
    with transaction() as conn:
        conn.executescript(
            """
            -- ─── جدول کاربران ───
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                joined_at   TEXT NOT NULL,
                is_blocked  INTEGER NOT NULL DEFAULT 0
            );

            -- ─── جدول تاریخچه جستجو ───
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                instagram_url TEXT NOT NULL,
                title       TEXT,
                media_type  TEXT,
                year        TEXT,
                imdb_id     TEXT,
                searched_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            );

            -- ─── جدول کش تشخیص (لینک ← نتیجه) ───
            CREATE TABLE IF NOT EXISTS cache (
                reel_url    TEXT PRIMARY KEY,
                title       TEXT,
                media_type  TEXT,
                year        TEXT,
                imdb_id     TEXT,
                created_at  TEXT NOT NULL
            );

            -- ─── جدول آمار استفاده روزانه ───
            CREATE TABLE IF NOT EXISTS usage (
                user_id     INTEGER NOT NULL,
                day         TEXT NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );

            -- ─── جدول تنظیمات ربات ───
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            -- ─── کش لینک تریلر (media_key ← لینک یوتیوب) ───
            CREATE TABLE IF NOT EXISTS trailer_cache (
                media_key   TEXT PRIMARY KEY,
                url         TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            """
        )
    logger.info("پایگاه داده راه‌اندازی شد: %s", DB_PATH)


def now_iso() -> str:
    """زمان فعلی به فرمت ISO برای ذخیره در دیتابیس."""
    return datetime.now(timezone.utc).isoformat()
