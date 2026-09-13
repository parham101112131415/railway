"""ماژول دانلود پوستر فیلم.

دانلود پوستر از OMDB (یا TMDb) و ذخیره در پوشه posters برای ارسال
سپس به تلگرام. اگر فایل از قبل موجود باشد از کاش استفاده می‌شود.
"""

from __future__ import annotations

import aiohttp
from pathlib import Path

from config import POSTERS_DIR
from utils.helpers import safe_filename
from utils.logger import logger


async def download_poster(
    session: aiohttp.ClientSession, poster_url: str, title: str, imdb_id: str | None = None
) -> Path | None:
    """دریافت پوستر و بازگرداندن مسیر فایل ذخیره‌شده."""
    if not poster_url or poster_url.lower() == "n/a":
        return None

    POSTERS_DIR.mkdir(parents=True, exist_ok=True)
    # نام فایل بر اساس imdb یا عنوان
    key = safe_filename(imdb_id or title, 40)
    out_path = POSTERS_DIR / f"{key}.jpg"

    # اگر قبلاً دانلود شده همین فایل را برمی‌گردانیم
    if out_path.exists():
        return out_path

    # درخواست زمان‌بندی با هدر مرورگر
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        async with session.get(poster_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                logger.warning("دانلود پوستر ناموفق: %s", resp.status)
                return None
            out_path.write_bytes(await resp.read())
            return out_path
    except Exception as exc:  # noqa: BLE001
        logger.error("خطا در دانلود پوستر %s: %s", poster_url[:60], exc)
        return None