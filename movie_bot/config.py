"""فایل کانفیگ مرکزی پروژه movie_bot.

تمام کلیدها و متغیرهای محیطی فقط از این ماژول خوانده می‌شوند.
هیچ فایل دیگری نباید مستقیم به .env دسترسی داشته باشد.
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# ─── مسیرهای پایه پروژه ───────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).resolve().parent
ENV_FILE: Path = BASE_DIR / ".env"

# اگر فایل .env وجود نداشت، هنگام اولین اجرا با مقادیر پیش‌فرض ساخته می‌شود.
if not ENV_FILE.exists():
    ENV_FILE.write_text(
        "\n".join(
            [
                "BOT_TOKEN=",
                "GEMINI_API_KEY=",
                "TMDB_API_KEY=",
                "OMDB_API_KEY=",
                "ADMIN_ID=",
                "",
            ]
        ),
        encoding="utf-8",
    )

# بارگذاری متغیرهای محیطی
load_dotenv(ENV_FILE, override=False)


def _get(name: str, default: str = "") -> str:
    """خواندن امن یک متغیر محیطی."""
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    """خواندن امن یک متغیر محیطی از نوع عدد صحیح."""
    raw = _get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    """خواندن امن یک متغیر محیطی از نوع بولی."""
    return _get(name, "false").lower() in {"1", "true", "yes", "on"}


# ─── توکن ربات تلگرام ─────────────────────────────────────────────────────────
BOT_TOKEN: str = _get("BOT_TOKEN")

# ─── کلیدهای API سرویس‌های خارجی ───────────────────────────────────────────────
GEMINI_API_KEY: str = _get("GEMINI_API_KEY", "AQ.Ab8RN6K4QR-d28tqOggnIRy0thuVizirgYPmYkKJhjHJ_sLD9w")
TMDB_API_KEY: str = _get("TMDB_API_KEY", "2115bc3c440cecbe3830793378dd90b1")
OMDB_API_KEY: str = _get("OMDB_API_KEY", "12dfb428")

# ─── آیدی ادمین(ها) ────────────────────────────────────────────────────────────
ADMIN_IDS: list[int] = [
    int(x)
    for x in _get("ADMIN_ID", "8055210419,8905260615").replace("،", ",").split(",")
    if x.strip().isdigit()
]

# ─── تنظیمات Geminaily ─────────────────────────────────────────────────────────
# مدل پایدار و رسمی Gemini برای تشخیص چندرسانه‌ای
GEMINI_MODEL: str = "auto"
GEMINI_RETRIES: int = 3
GEMINI_TIMEOUT: int = 60


# ─── تنظیمات دانلود ────────────────────────────────────────────────────────────
MAX_DAILY_DOWNLOADS: int = 20      # سقف دانلود روزانه برای هر کاربر
DOWNLOAD_RETRIES: int = 3          # تعداد تلاش‌های دانلود
DOWNLOAD_RETRY_DELAY: int = 2      # تأخیر بین تلاش‌ها (ثانیه)

# ─── تنظیمات استخراج فریم ─────────────────────────────────────────────────────
FRAME_PERCENTAGES: tuple[float, ...] = (5, 15, 25, 35, 50, 65, 80, 95)
MIN_FRAMES: int = 5                # حداقل فریم برای ارسال به مدل

# ─── تنظیمات پاک‌سازی ──────────────────────────────────────────────────────────
MAX_AGE_HOURS: int = 6             # حذف فایل‌های قدیمی‌تر از 6 ساعت
CLEANUP_INTERVAL_MIN: int = 30    # اجرای پاک‌ساز هر 30 دقیقه

# ─── مسیرهای پوشه‌ها ───────────────────────────────────────────────────────────
DOWNLOADS_DIR: Path = BASE_DIR / "downloads"
IMAGES_DIR: Path = BASE_DIR / "images"
POSTERS_DIR: Path = BASE_DIR / "posters"
LOGS_DIR: Path = BASE_DIR / "logs"
DATA_DIR: Path = BASE_DIR / "database"
DB_PATH: Path = DATA_DIR / "moviebot.db"
LOG_FILE: Path = LOGS_DIR / "bot.log"

# ساخت پوشه‌های لازم در صورت عدم وجود
for _d in (DOWNLOADS_DIR, IMAGES_DIR, POSTERS_DIR, LOGS_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ─── آدرس‌های API ──────────────────────────────────────────────────────────────
TMDB_BASE_URL: str = "https://api.themoviedb.org/3"
OMDB_BASE_URL: str = "https://www.omdbapi.com"
INSTAGRAM_MATCH: str = r"instagram\.com/(?:reel|p)/"

# ─── پیام شروع (خوش‌آمد) ───────────────────────────────────────────────────────
START_TEXT: str = (
    "🎬 *فیلم‌یاب* 🎬\n\n"
    "سلام! لینک ریلز یا پست اینستاگرام رو برام بفرست\n"
    "تا فیلم یا سریالش رو برات تشخیص بدم و اطلاعاتش رو نشون بدم 🔍"
)