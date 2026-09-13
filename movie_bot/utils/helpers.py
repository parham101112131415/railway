"""هلپرهای عمومی پروژه.

اتومبیل‌های کوچک برای کارهای رایج مثل تمیزکردن پیام و ساخت نام فایل.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def now_str() -> str:
    """زمان فعلی به صورت رشته برای اسم فایل."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_filename(text: str, max_len: int = 60) -> str:
    """تمیزکردن یک متن برای استفاده به عنوان نام فایل."""
    text = re.sub(r"[^A-Za-z0-9آ-ی ._-]", "", text)
    text = re.sub(r"\s+", "_", text)
    return (text or "file")[:max_len].strip("._")


def generate_id() -> str:
    """ساخت شناسه یکتای کوتاه."""
    return uuid.uuid4().hex[:12]


def build_filename(reel_url: str, ext: str = ".mp4") -> str:
    """ساخت نام فایل مرتب بر اساس زمان و شناسه یکتا."""
    slug = safe_filename(reel_url.split("/")[-1]) or unique_id()
    return f"{now_str()}_{slug}{ext}"


def unique_id(length: int = 8) -> str:
    """ساخت شناسه تصادفی کوتاه."""
    return uuid.uuid4().hex[:length]


def format_duration(seconds: float | None) -> str:
    """تبدیل ثانیه به فرمت خوانا مثل 2:34."""
    if seconds is None:
        return "نامشخص"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def safe_bool(value: Any, default: bool = False) -> bool:
    """تبدیل امن یک مقدار به بولی."""
    if value is None:
        return default
    return str(value).lower() in {"1", "true", "yes", "on", "بله", "آره"}