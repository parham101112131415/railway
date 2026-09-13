"""ماژول اعتبارسنج ورودی‌ها.

بررسی لینک‌های اینستاگرام، متن‌های ورودی و پارامترها.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from config import INSTAGRAM_MATCH

_INSTA_PATTERN = re.compile(INSTAGRAM_MATCH, re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def extract_instagram_link(text: str) -> str | None:
    """استخراج اولین لینک اینستاگرام (reel یا post) از متن.

    هم لینک‌های معمولی و هم لینک‌های `instagram.com/reel/...` را می‌پذیرد.
    """
    for match in _URL_PATTERN.finditer(text):
        url = match.group(0).rstrip("،,.;:!؟?")
        if _INSTA_PATTERN.search(url):
            # نرمال‌سازی: حذف پارامترهای ردیابی اضافه برای یکسان بودن کلید کش
            parsed = urlparse(url)
            clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            return clean
    return None


def is_valid_instagram_url(url: str) -> bool:
    """بررسی اینکه یک لینک، لینک معتبر اینستاگرام است یا نه."""
    return bool(_INSTA_PATTERN.search(url))


def is_admin(user_id: int, admin_ids: list[int]) -> bool:
    """بررسی ادمین بودن یک کاربر."""
    return user_id in admin_ids


def sanitize_text(text: str, max_len: int = 4000) -> str:
    """کوتاه‌سازی و پاک‌سازی متن ورودی برای جلوگیری از پیام‌های غول‌پیکر."""
    text = text.strip()
    return text[:max_len]
