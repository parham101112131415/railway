"""ماژول نصب خودکار وابستگی‌ها.

تشخیص سیستم‌عامل (Termux / Linux / Windows) و نصب پیش‌نیازهای سیستم و
پکیج‌های پایتون در صورت نیاز.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

from utils.logger import logger

# بسته‌های پایتون مورد نیاز پروژه
# opencv-python-headless و google-generativeai حذف شدند: در Termux ARM64
# قابل build نیستند؛ استخراج فریم با ffmpeg و تشخیص با REST مستقیم انجام می‌شود.
REQUIRED_PACKAGES: tuple[str, ...] = (
    "python-telegram-bot>=22.0,<23.0",
    "yt-dlp",
    "numpy",
    "requests",
    "aiohttp",
    "python-dotenv",
    "Pillow",
)


def is_termux() -> bool:
    """تشخیص اینکه روی ترمکس اجرا می‌شویم یا نه."""
    return "com.termux" in platform.platform().lower() or Path(
        "/data/data/com.termux/files/usr"
    ).exists()


def _run(cmd: list[str]) -> bool:
    """اجرای یک فرمان سیستم و بازگرداندن موفقیت آن."""
    try:
        logger.info("اجرای فرمان: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001 - خطای نصب را می‌پوشانیم
        logger.error("خطا در اجرای فرمان %s: %s", cmd, exc)
        return False


def install_system_deps() -> bool:
    """نصب پیش‌نیازهای سیستم‌عامل (ffmpeg و ابزارهای لازم)."""
    if is_termux():
        # بسته‌های پایه ترمکس
        if not _run(["pkg", "update", "-y"]):
            logger.warning("pkg update ناموفق بود؛ ادامه می‌دهیم.")
        return _run(["pkg", "install", "ffmpeg", "python", "-y"])
    if shutil.which("apt-get"):
        return _run(["apt-get", "update", "-y"]) and _run(
            ["apt-get", "install", "ffmpeg", "-y"]
        )
    # در ویندوز/مک فرض بر نصب بودن ffmpeg است
    return shutil.which("ffmpeg") is not None


def pip_install(packages: tuple[str, ...]) -> bool:
    """نصب پکیج‌های پایتون از طریق pip."""
    return _run([sys.executable, "-m", "pip", "install", "--upgrade", *packages])


def ensure_dependencies(auto_install: bool = True) -> bool:
    """بررسی وجود وابستگی‌ها و نصب خودکار در صورت نیاز."""
    missing: list[str] = []
    for pkg in REQUIRED_PACKAGES:
        name = pkg.split(">=")[0].split(",")[0]
        try:
            __import__(name.replace("-", "_"))
        except ImportError:
            missing.append(pkg)

    if not missing:
        logger.info("تمام وابستگی‌های پایتون نصب هستند.")
        return True

    if not auto_install:
        logger.warning("وابستگی‌های نصب‌نشده: %s", ", ".join(missing))
        return False

    logger.info("نصب وابستگی‌های جا مانده: %s", ", ".join(missing))
    return pip_install(tuple(missing))
