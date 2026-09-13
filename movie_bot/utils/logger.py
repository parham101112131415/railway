"""ماژول لاگر مرکزی پروژه.

خروجی لاگ هم روی کنسول و هم داخل فایل logs/bot.log نوشته می‌شود.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from config import LOG_FILE, LOGS_DIR

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(name: str = "movie_bot") -> logging.Logger:
    """راه‌اندازی و بازگرداندن لاگر اصلی پروژه."""
    # جلوگیری از ساخت لاگر تکراری
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # ─── فرمت‌کننده مشترک ───
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # ─── هندلر کنسول ───
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ─── هندلر فایل (دورانی، حداکثر 5 مگابایت، 3 نسخه پشتیبان) ───
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


# لاگر سراسری قابل استفاده در تمام ماژول‌ها
logger: logging.Logger = setup_logger()
