"""
telegram_split.py
تقسیم واقعیِ فایل‌های ویدیو/صدای بزرگ به تکه‌های ~۴۵ مگابایتی با ffmpeg
(بدون ری‌انکود، stream copy — پس افت کیفیتی نداره) و ارسال هرکدوم به‌عنوان
یه پیام مدیای کامل و قابل‌پخش در تلگرام (نه فایل خام بایتی).

اگه فایل ویدیو/صدا نبود یا ffmpeg نتونست بشکونتش، به‌صورت fallback تقسیم
خام باینری انجام می‌شه (فقط برای همچین موارد نادری).
"""
import os
import asyncio
import subprocess


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


_LOCAL = os.environ.get("TELEGRAM_LOCAL_API", "0") == "1"
# سرور محلی Bot API تا ~۲ گیگ یک‌جا می‌فرسته؛ ابری فقط ۵۰ مگ.
DEFAULT_CHUNK_BYTES = _env_int(
    "SPLIT_CHUNK_BYTES", 1900 * 1024 * 1024 if _LOCAL else 45 * 1024 * 1024)
TELEGRAM_SAFE_LIMIT = _env_int(
    "TELEGRAM_MAX_BYTES", 1900 * 1024 * 1024 if _LOCAL else 50 * 1024 * 1024)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts"}
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".flac"}


def _ffprobe_duration(path):
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        d = float(out.stdout.strip())
        return d if d > 0 else None
    except Exception:
        return None


def _ffmpeg_segment_split(path, chunk_size, attempt=0):
    """تقسیم واقعی با ffmpeg (stream copy) — هر تکه یه فایل کامل و مستقل."""
    if attempt > 3:
        return None

    duration = _ffprobe_duration(path)
    size = os.path.getsize(path)
    if not duration:
        return None

    bytes_per_sec = size / duration
    if bytes_per_sec <= 0:
        return None

    # هر تلاش با حاشیه‌ی امنِ بیشتر (برای وقتی کی‌فریم‌ها باعث می‌شن تکه‌ها
    # از حد مجاز رد بشن)
    safety = 0.88 - (attempt * 0.12)
    segment_time = max(2, (chunk_size * safety) / bytes_per_sec)

    directory = os.path.dirname(path) or "."
    base, ext = os.path.splitext(os.path.basename(path))
    out_pattern = os.path.join(directory, f"{base}.part%03d{ext}")

    # پاک‌سازی باقی‌مونده‌ی تلاش قبلی
    for fn in os.listdir(directory):
        if fn.startswith(f"{base}.part") and fn.endswith(ext):
            try:
                os.remove(os.path.join(directory, fn))
            except Exception:
                pass

    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", path,
                "-map", "0", "-c", "copy",
                "-f", "segment", "-segment_time", f"{segment_time:.2f}",
                "-reset_timestamps", "1",
                out_pattern,
            ],
            capture_output=True, timeout=1800,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    parts = sorted(
        os.path.join(directory, fn)
        for fn in os.listdir(directory)
        if fn.startswith(f"{base}.part") and fn.endswith(ext)
    )
    if not parts:
        return None

    if any(os.path.getsize(p) > TELEGRAM_SAFE_LIMIT for p in parts):
        return _ffmpeg_segment_split(path, chunk_size, attempt=attempt + 1)

    return parts


def _raw_binary_split(path, chunk_size):
    """fallback: تقسیم خام باینری (فقط وقتی ffmpeg جواب نده)."""
    directory = os.path.dirname(path) or "."
    base_name = os.path.basename(path)
    size = os.path.getsize(path)
    total_parts = (size + chunk_size - 1) // chunk_size

    parts = []
    with open(path, "rb") as f:
        idx = 1
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            part_name = f"{base_name}.part{idx:02d}of{total_parts:02d}"
            part_path = os.path.join(directory, part_name)
            with open(part_path, "wb") as pf:
                pf.write(data)
            parts.append(part_path)
            idx += 1
    return parts, True  # True یعنی خامه (نیاز به چسبوندن دستی داره)


def split_file(path, chunk_size=None):
    """فایل رو می‌شکونه. برمی‌گردونه: (لیست مسیر تکه‌ها, is_raw)."""
    chunk_size = chunk_size or DEFAULT_CHUNK_BYTES
    size = os.path.getsize(path)
    if size <= chunk_size:
        return [path], False

    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
        parts = _ffmpeg_segment_split(path, chunk_size)
        if parts:
            return parts, False

    return _raw_binary_split(path, chunk_size)


async def send_file_smart(
    bot,
    chat_id,
    path,
    caption=None,
    is_audio=False,
    chunk_size=None,
    read_timeout=3600,
    write_timeout=3600,
    reply_markup=None,
):
    """فایل رو اگه کوچیک باشه یک‌جا، وگرنه تکه‌تکه (~۴۵ مگ هر تکه) می‌فرسته.
    هر تکه یه پیام ویدیو/صدای کامل و قابل‌پخشه (نه فایل خام برای دانلودکننده).
    آخرین پیام ارسال‌شده رو برمی‌گردونه.
    """
    size = os.path.getsize(path)
    chunk_size = chunk_size or DEFAULT_CHUNK_BYTES
    ext = os.path.splitext(path)[1].lower()

    async def _send_one(fpath, cap):
        with open(fpath, "rb") as f:
            if is_audio:
                return await bot.send_audio(
                    chat_id, audio=f, caption=cap or "",
                    reply_markup=reply_markup,
                    read_timeout=read_timeout, write_timeout=write_timeout,
                )
            if ext in VIDEO_EXTS:
                return await bot.send_video(
                    chat_id, video=f, caption=cap or "",
                    supports_streaming=True,
                    reply_markup=reply_markup,
                    read_timeout=read_timeout, write_timeout=write_timeout,
                )
            return await bot.send_document(
                chat_id, document=f, caption=cap or "",
                reply_markup=reply_markup,
                read_timeout=read_timeout, write_timeout=write_timeout,
            )

    if size <= TELEGRAM_SAFE_LIMIT:
        return await _send_one(path, caption)

    parts, is_raw = await asyncio.to_thread(split_file, path, chunk_size)
    total = len(parts)
    last_msg = None

    for i, part_path in enumerate(parts, start=1):
        part_caption = f"قسمت {i} از {total}"
        if is_raw and i == 1:
            part_caption += (
                "\n\nℹ️ این فایل خام تقسیم شده؛ برای ساخت فایل اصلی همه‌ی "
                "قسمت‌ها رو کنار هم بچسبون (cat در لینوکس/مک یا copy /b در ویندوز)."
            )
        last_msg = await _send_one(part_path, part_caption)
        try:
            if part_path != path:
                os.remove(part_path)
        except Exception:
            pass

    return last_msg


def cleanup_path(path, also_parts=True):
    """فایل اصلی (و در صورت باقی موندن، تکه‌های تقسیم‌شده‌ش) رو پاک می‌کنه."""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass

    if also_parts and path:
        try:
            directory = os.path.dirname(path) or "."
            base_name = os.path.splitext(os.path.basename(path))[0]
            for fn in os.listdir(directory):
                if fn.startswith(base_name) and ".part" in fn:
                    try:
                        os.remove(os.path.join(directory, fn))
                    except Exception:
                        pass
        except Exception:
            pass
