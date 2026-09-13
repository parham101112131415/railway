"""ماژول کش تشخیص فیلم.

بازگرداندن نتیجه تشخیص از روی لینک ریلز (بدون پردازش مجدد)
برای جلوگیری از پردازش تکراری ریلزهای یکسان.
"""

from __future__ import annotations

import sqlite3

from database.sqlite import now_iso, transaction
from utils.logger import logger

# حداکثر عمر کش به روز
CACHE_TTL_DAYS: int = 7


def get_cache(reel_url: str) -> dict[str, str] | None:
    """بازگرداندن نتیجه کش‌شده برای یک لینک ریلز."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT title, media_type, year, imdb_id, created_at "
            "FROM cache WHERE reel_url = ?",
            (reel_url,),
        ).fetchone()
    if row is None:
        return None

    # حذف ورودی‌های منقضی
    try:
        from datetime import datetime

        created = datetime.fromisoformat(row["created_at"])
        age_days = (datetime.now(created.tzinfo) - created).days
        if age_days > CACHE_TTL_DAYS:
            delete_cache(reel_url)
            return None
    except ValueError:
        pass

    return {
        "title": row["title"],
        "media_type": row["media_type"],
        "year": row["year"],
        "imdb_id": row["imdb_id"],
    }


def set_cache(
    reel_url: str,
    title: str,
    media_type: str,
    year: str,
    imdb_id: str | None = None,
) -> None:
    """ذخیره نتیجه تشخیص برای یک لینک ریلز."""
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache "
            "(reel_url, title, media_type, year, imdb_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (reel_url, title, media_type, year, imdb_id, now_iso()),
        )
    logger.debug("کش ذخیره شد برای: %s", reel_url)


def delete_cache(reel_url: str) -> None:
    """حذف یک ورودی کش."""
    with transaction() as conn:
        conn.execute("DELETE FROM cache WHERE reel_url = ?", (reel_url,))


def purge_expired_cache() -> int:
    """حذف تمام ورودی‌های کش منقضی و بازگرداندن تعداد حذف‌شده."""
    from datetime import datetime, timedelta

    cutoff = datetime.now().astimezone() - timedelta(days=CACHE_TTL_DAYS)
    with transaction() as conn:
        cur = conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff.isoformat(),))
        deleted = cur.rowcount
    if deleted:
        logger.info("%d ورودی کش منقضی حذف شد.", deleted)
    return deleted


def count_cache() -> int:
    """تعداد ورودی‌های موجود در کش."""
    with transaction() as conn:
        row = conn.execute("SELECT COUNT(*) FROM cache").fetchone()
    return int(row[0]) if row else 0


# ─── کش لینک تریلر (برای جلوگیری از درخواست دوباره به TMDb) ───

def get_trailer_url(media_key: str) -> str | None:
    """بازگرداندن لینک تریلر کش‌شده برای یک فیلم/سریال."""
    try:
        with transaction() as conn:
            row = conn.execute(
                "SELECT url FROM trailer_cache WHERE media_key = ?", (media_key,)
            ).fetchone()
    except Exception:  # noqa: BLE001 - جدول هنوز ساخته نشده باشد
        return None
    return row["url"] if row else None


def set_trailer_url(media_key: str, url: str) -> None:
    """ذخیره لینک تریلر در کش."""
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO trailer_cache (media_key, url, created_at) "
                "VALUES (?, ?, ?)",
                (media_key, url, now_iso()),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("خطا در ذخیره کش تریلر: %s", exc)
