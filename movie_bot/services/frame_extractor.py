"""ماژول استخراج فریم از ویدیو با ffmpeg.

در Termux بسته‌ی `opencv` قابل نصب نیست (نیاز به build از سورس)، بنابراین
استخراج فریم‌ها با ffmpeg انجام می‌شود و کیفیت/یکنواختی فریم‌ها با
Pillow + numpy سنجیده می‌شود.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from config import FRAME_PERCENTAGES, IMAGES_DIR, MIN_FRAMES
from utils.logger import logger


def _duration_ffprobe(video_path: Path) -> float:
    """دریافت مدت زمان ویدیو به ثانیه با ffprobe."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(out.stdout.strip())
    except Exception as exc:  # noqa: BLE001
        logger.error("ffprobe مدت زمان ویدیو را نگرفت: %s", exc)
        return 0.0


def _ffmpeg_frame(video_path: Path, timestamp: float, out_path: Path) -> bool:
    """یادداشت یک فریم با ffmpeg در زمان مشخص و ذخیره خروجی PNG."""
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{timestamp:.4f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-q:v", "2",
                str(out_path),
            ],
            capture_output=True,
            timeout=120,
        )
        return r.returncode == 0 and out_path.exists()
    except Exception as exc:  # noqa: BLE001
        logger.error("ffmpeg فریم نگرفت (ts=%.2f): %s", timestamp, exc)
        return False


def _gray_array(png_path: Path) -> np.ndarray | None:
    """بارگذاری PNG و بازگرداندن آرایه‌ی خاکستری numpy."""
    try:
        with Image.open(png_path) as img:
            return np.asarray(img.convert("L"), dtype=np.float32)
    except Exception as exc:  # noqa: BLE001
        logger.error("PIL نتواست بخواند %s: %s", png_path.name, exc)
        return None


def _hist_of(gray: np.ndarray) -> np.ndarray:
    """هیستوگرام ۶۴بندی نرمال‌شده برای مقایسه‌ی تکرار فریم."""
    hist, _ = np.histogram(gray.ravel(), bins=64, range=(0, 256))
    hist = hist.astype(np.float32)
    total = hist.sum()
    if total > 0:
        hist = hist / total
    return hist


def _passes_quality(gray: np.ndarray) -> bool:
    """فریم تاریک/بیش‌روشن/یکنواخت نباشد (بدون توجه به تکراری بودن)."""
    mean_brightness = float(gray.mean())
    if mean_brightness < 20 or mean_brightness > 235:
        return False
    if float(gray.var()) < 40:
        return False
    return True


def _similarity(hist: np.ndarray, prev_hist: np.ndarray | None) -> float:
    """شباهت هیستوگرام فریم فعلی به فریم قبلیِ قبول‌شده (۰ تا ۱)."""
    if prev_hist is None:
        return 0.0
    if hist.size != prev_hist.size:
        return 0.0
    sim = float(np.corrcoef(prev_hist, hist)[0, 1])
    return sim if not np.isnan(sim) else 0.0


def extract_frames(
    video_path: Path,
    dest_dir: Path | None = None,
    percentages: tuple[float, ...] | None = None,
) -> list[Path]:
    """استخراج و فیلتر فریم‌های کلیدی و بازگرداندن مسیرهای PNG.

    - `percentages` درصدهای زمانی که فریم از آن‌جا گرفته می‌شود.
    - سعی می‌شود فریم‌ها از هم متنوع باشند (فیلتر تکراری بر اساس هیستوگرام)،
      اما این فیلتر هرگز باعث نمی‌شود تعداد نهایی از `MIN_FRAMES`
      (یا تعداد `percentages` ورودی، هر کدام کمتر بود) کمتر شود؛ اگر فریم‌های
      متنوعِ کافی پیدا نشد، از فریم‌های باکیفیتِ «شبیه به قبلی» به‌عنوان
      پشتیبان استفاده می‌شود تا هیچ‌وقت خروجی فقط ۱ فریم نشود.
    """
    dest_dir = dest_dir or IMAGES_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    percentages = percentages or FRAME_PERCENTAGES
    # هدف نهایی: حداقل MIN_FRAMES، ولی اگر ورودی (مثلاً در حالت دقت بالا)
    # درصدهای بیشتری خواسته، سقف رو با همون هماهنگ می‌کنیم.
    target = max(MIN_FRAMES, min(len(percentages), MIN_FRAMES * 2))

    duration = _duration_ffprobe(video_path)
    if duration <= 0:
        logger.error("مدت‌زمان ویدیو مشخص نشد؛ استخراج فریم لغو شد: %s", video_path)
        return []

    produced: list[Path] = []          # فریم‌های متنوع و قبول‌شده
    backup_pool: list[Path] = []       # فریم‌های باکیفیت ولی «شبیه قبلی» — پشتیبان
    prev_hist: np.ndarray | None = None

    def _try_frame(ts: float, out_path: Path) -> None:
        nonlocal prev_hist
        if not _ffmpeg_frame(video_path, ts, out_path):
            return
        gray = _gray_array(out_path)
        if gray is None or not _passes_quality(gray):
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        hist = _hist_of(gray)
        sim = _similarity(hist, prev_hist)
        if sim > 0.995:
            # فریم به‌قدر کافی باکیفیته، فقط شبیه فریم قبلیه — دور نریزش،
            # به‌عنوان پشتیبان نگه‌دار تا اگر فریم متنوع کم آمد ازش استفاده شود.
            backup_pool.append(out_path)
            return
        prev_hist = hist
        produced.append(out_path)

    for pct in percentages:
        ts = duration * pct / 100.0
        out_path = dest_dir / f"{video_path.stem}_frame_{int(pct):02d}.png"
        _try_frame(ts, out_path)

    # اگر تعداد فریمِ متنوع کافی نبود، فریم‌ها را با گام‌های میانه‌تر دوباره می‌گیریم
    if len(produced) < target:
        logger.info("فریم متنوع کافی نیست؛ تلاش برای گرفتن فریم‌های اضافی…")
        extra_needed = target - len(produced)
        step = 1.0 / (extra_needed + 1)
        for i in range(extra_needed):
            ts = duration * (i + 1) * step
            out_path = dest_dir / f"{video_path.stem}_extra_{i}.png"
            _try_frame(ts, out_path)

    # اگر باز هم به حد هدف نرسیدیم (مثلاً ویدیوی خیلی ساکن)، از فریم‌های
    # پشتیبانِ باکیفیت (حتی اگر شبیه هم باشند) پر می‌کنیم — همیشه بهتر از
    # فرستادن فقط ۱ فریم به مدل تشخیص است.
    if len(produced) < target and backup_pool:
        need = target - len(produced)
        produced.extend(backup_pool[:need])
        backup_pool = backup_pool[need:]

    # پاک‌سازی فریم‌های پشتیبانِ استفاده‌نشده تا روی دیسک باقی نمانند
    for p in backup_pool:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

    logger.info("استخراج %d فریم از %s انجام شد.", len(produced), video_path.name)
    return produced