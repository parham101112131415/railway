"""ماژول پاک‌سازی فایل‌های موقت.

حذف فایل‌های قدیمی از پوشه‌های downloads، images و posters به صورت
زمان‌بندی‌شده برای جلوگیری از پر شدن حافظه.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from config import (
    DOWNLOADS_DIR,
    IMAGES_DIR,
    LOGS_DIR,
    MAX_AGE_HOURS,
    POSTERS_DIR,
    CLEANUP_INTERVAL_MIN,
)
from utils.logger import logger

# پوشه‌هایی که شامل فایل‌های موقت هستند
_TEMP_DIRS: tuple[Path, ...] = (DOWNLOADS_DIR, IMAGES_DIR, POSTERS_DIR)


def clean_old_files(dirs: tuple[Path, ...] = _TEMP_DIRS,
                    max_age_hours: int = MAX_AGE_HOURS) -> int:
    """حذف فایل‌های قدیمی از پوشه‌های موقت.

    بازگرداندن تعداد فایل‌های حذف‌شده. پوشه `logs` هرگز پاک نمی‌شود.
    """
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for folder in dirs:
        if folder == LOGS_DIR or not folder.exists():
            continue
        for file in folder.iterdir():
            try:
                if file.is_file() and file.stat().st_mtime < cutoff:
                    file.unlink()
                    removed += 1
            except OSError as exc:
                logger.warning("خطا در حذف %s: %s", file, exc)
    if removed:
        logger.info("%d فایل موقت قدیمی پاک شد.", removed)
    return removed


async def run_periodic_cleanup(interval_min: int = CLEANUP_INTERVAL_MIN) -> None:
    """اجرای دوره‌ای پاک‌سازی در یک تسک پس‌زمینه."""
    while True:
        try:
            clean_old_files()
        except Exception as exc:  # noqa: BLE001
            logger.error("خطا در پاک‌سازی دوره‌ای: %s", exc)
        await asyncio.sleep(interval_min * 60)