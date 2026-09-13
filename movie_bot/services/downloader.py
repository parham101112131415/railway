"""ماژول دانلود ویدیو با yt-dlp.

دانلود ریلز اینستاگرام با کیفیت حداکثری، با چند بار تلاش مجدد و
هندل کردن انواع خطا. دانلود به صورت هم‌زمان (async) انجام می‌شود.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Callable

import yt_dlp

from config import DOWNLOAD_RETRIES, DOWNLOAD_RETRY_DELAY, DOWNLOADS_DIR
from utils.logger import logger

# وضعیت‌های مختلف دانلود برای نمایش پیشرفت به کاربر (async، چون به تلگرام می‌فرستد)
StatusCallback = Callable[[str], Awaitable[None]]

# هدرهای لازم برای دور زدن محدودیت دسترسی اینستاگرام
_INSTA_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}

# کوکیِ لاگین‌شده‌ی اینستاگرام جهت دانلود پست‌های خصوصی/قفل‌شده
_COOKIES_FILE: Path = Path(__file__).resolve().parent.parent / "instagram_cookies.txt"


def _schedule_status(status_cb: StatusCallback, text: str) -> None:
    """برنامه‌ریزی ارسال پیام وضعیت روی حلقه‌ی در حال اجرا (for هوک sync)."""
    try:
        asyncio.get_running_loop().create_task(status_cb(text))
    except RuntimeError:
        pass


def _build_progress_hook(state: dict):
    """هوک yt-dlp فقط پر می‌کنه `state` رو (بدون ارسال پیام).

    ویرایش پیام تلگرام با یه حلقهٔ جداگانه انجام می‌شه
    (الگوی نوار پیشرفتِ دانلود یوتیوب توی bridge_bot) تا درصد
    بین دو استریم (ویدیو/صدا) عقب‌جلو نزنه.
    """
    def hook(d: dict) -> None:
        st = d.get("status")
        if st == "downloading" or st == "finished":
            state["downloaded"] = d.get("downloaded_bytes") or 0
            state["total"] = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            state["speed"] = d.get("speed") or 0
            state["eta"] = d.get("eta") or 0
            pc = d.get("_percent")
            if pc is not None:
                try:
                    if isinstance(pc, str) and pc.endswith("%"):
                        state["pct"] = float(pc[:-1])
                    elif isinstance(pc, (int, float)):
                        state["pct"] = float(pc)
                except Exception:
                    pass
    return hook



async def download_instagram_reel(
    url: str, dest_dir: Path | None = None, status_cb: StatusCallback | None = None
) -> Path | None:
    """دانلود یک ریلز اینستاگرام و بازگرداندن مسیر فایل نهایی.

    در صورت شکست تمام تلاش‌ها، `None` بازگردانده می‌شود.
    """
    return await _download_reel_core(url, dest_dir=dest_dir, status_cb=status_cb, use_cookies=True)


async def download_tiktok_reel(
    url: str, dest_dir: Path | None = None, status_cb: StatusCallback | None = None
) -> Path | None:
    """دانلود ویدیوی تیک‌تاک (بدون نیاز به کوکی اینستاگرام)."""
    return await _download_reel_core(url, dest_dir=dest_dir, status_cb=status_cb, use_cookies=False)


async def _download_reel_core(
    url: str,
    *,
    dest_dir: Path | None = None,
    status_cb: StatusCallback | None = None,
    use_cookies: bool = True,
) -> Path | None:
    """هستهٔ مشترک دانلود ویدیو (اینستاگرام / تیک‌تاک) با yt-dlp."""
    dest_dir = dest_dir or DOWNLOADS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    last_error: str | None = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        if status_cb:
            await status_cb(f"🔨 تلاش {attempt} از {DOWNLOAD_RETRIES} برای دانلود…")

        options = {
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "format": "bv*+ba/b",  # بهترین کیفیت صدا و تصویر
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": DOWNLOAD_RETRIES,
            "socket_timeout": 30,
            "http_headers": _INSTA_HEADERS,
        }
        if use_cookies and _COOKIES_FILE.exists():
            options["cookiefile"] = str(_COOKIES_FILE)

        dl_state = {"downloaded": 0, "total": 0, "speed": 0, "eta": 0, "pct": None, "done": False}
        progress_hook = _build_progress_hook(dl_state)
        if progress_hook:
            options["progress_hooks"] = [progress_hook]

        async def _progress_edit():
            last = ""
            while not dl_state["done"]:
                await asyncio.sleep(1)
                if dl_state["done"]:
                    break
                mb = dl_state["downloaded"] / 1048576
                speed = dl_state["speed"] or 0
                spd = f" ⚡ {speed / 1048576:.1f} MB/s" if speed else ""
                eta_file = 0
                if dl_state["eta"]:
                    eta_file = dl_state["eta"]
                elif speed and dl_state["total"]:
                    left = max(0, dl_state["total"] - dl_state["downloaded"])
                    eta_file = left / speed if speed else 0
                eta_txt = ""
                if eta_file:
                    m, s = divmod(int(eta_file), 60)
                    eta_txt = f"\n⏱ باقی: {m}:{s:02d}" if not m else f"\n⏱ باقی: {m}د {s:02d}ث"
                if dl_state.get("pct") is not None:
                    pct = dl_state["pct"]
                elif dl_state["total"]:
                    pct = dl_state["downloaded"] / dl_state["total"] * 100
                else:
                    pct = 0
                pct = min(pct, 100)
                filled = int(pct / 5)
                bar = "▓" * filled + "░" * (20 - filled)
                tmb = dl_state["total"] / 1048576 if dl_state["total"] else 0
                size_txt = f"({mb:.1f}/{tmb:.1f} MB)" if tmb else f"({mb:.1f} MB)"
                txt = f"⬇️ {bar} {pct:.1f}%\n{size_txt}{spd}{eta_txt}"
                if txt != last:
                    last = txt
                    try:
                        await status_cb(txt)
                    except Exception:
                        pass
            try:
                await status_cb(f"⬇️ {'▓' * 20} ۱۰۰.۰٪\nدر حال پردازش فایل…")
            except Exception:
                pass

        def _download_worker():
            """دانلود sync ای داخل thread تا حلقه‌ی اصلی آزاد بماند."""
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if not info:
                        return None
                    filename = ydl.prepare_filename(info)
                    filepath = Path(filename)
                    if info.get("ext") and info.get("ext") != "mp4":
                        filepath = filepath.with_suffix(f".{info['ext']}")
                    return filepath if filepath.exists() else None
            except Exception as exc:  # noqa: BLE001
                _download_worker.last_error = exc
                return None

        _download_worker.last_error = None
        editor = asyncio.create_task(_progress_edit())
        filepath = await asyncio.to_thread(_download_worker)
        dl_state["done"] = True
        await editor
        if filepath:
            if status_cb:
                await status_cb("✅ دانلود کامل شد!")
            return filepath
        if not filepath and not _download_worker.last_error:
            # دانلود انجام شده ولی نام فایل نامشخص است؛ جدیدترین mp4 را برمی‌داریم
            candidates = sorted(
                dest_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if candidates:
                filepath = candidates[0]
        if filepath:
            if status_cb:
                await status_cb("✅ دانلود کامل شد!")
            return filepath
        if _download_worker.last_error:
            last_error = str(_download_worker.last_error)
        logger.warning("تلاش %d/%d دانلود ناموفق: %s", attempt, DOWNLOAD_RETRIES, last_error)
        if attempt < DOWNLOAD_RETRIES:
            await asyncio.sleep(DOWNLOAD_RETRY_DELAY)

    logger.error("دانلود %s در %d تلاش ناموفق بود: %s", url, DOWNLOAD_RETRIES, last_error)
    return None
