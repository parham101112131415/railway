"""
ربات بریج دانلودر - پل بین تلگرام و همان downloader.py که در ترمینال اجرا میشه.
همه منطق دانلود دقیقا همان downloader.py است؛ فقط رابط کاربری اینلاین کیبورد تلگرامه.
"""
import os
import re
import glob
import json
import time
import asyncio
import threading
import traceback
import subprocess
import shutil
import socket

import bridge_config as cfg
import yt_dlp

# downloader.py را لود ميكنيم (همان فايل ترمينالي)
import importlib.util
spec = importlib.util.spec_from_file_location("dl", cfg.DOWNLOADER_PATH)
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)

# ─── فیلم‌یاب: پایپ‌لاین کامل پروژه‌ی movie_bot (Gemini + TMDb/OMDb) ───
import sys
from pathlib import Path

MOVIE_BOT_DIR = os.environ.get("MOVIE_BOT_DIR", "/opt/bridge_bot/movie_bot")
try:
    if MOVIE_BOT_DIR not in sys.path:
        sys.path.insert(0, MOVIE_BOT_DIR)
    from services.downloader import download_instagram_reel as mv_download_reel
    from services.frame_extractor import extract_frames as mv_extract_frames
    from services.gemini import (
        identify_from_frames as mv_identify_from_frames,
        QuotaExceededError as MvQuotaExceededError,
        _rest_generate as mv_gemini_generate,
        _extract_json as mv_gemini_extract_json,
    )
    from services.movie_api import build_movie_info as mv_build_movie_info
    from bot.callbacks import (
        send_movie_card as mv_send_movie_card,
        handle_callback as mv_handle_callback,
    )
    from database.sqlite import init_db as mv_init_db
    from config import IMAGES_DIR as MV_IMAGES_DIR
    mv_init_db()
    MOVIE_OK = True
    print("[movie] ماژول فیلم‌یاب لود شد", flush=True)
    # پرامپت‌های متنیِ خلاصه/پاسخ (رونوشت کامل ویدیو) خیلی بلندترن از پرامپت
    # تشخیص فیلم و ممکنه بیشتر از ۶۰ ثانیه‌ی پیش‌فرض طول بکشن؛ تایم‌اوت مشترک
    # درخواست Gemini رو (بدون تغییر مدل/منطق انتخابش) بالاتر می‌بریم.
    try:
        _mv_gemini_mod = sys.modules.get("services.gemini")
        if _mv_gemini_mod is not None:
            _mv_gemini_mod.GEMINI_TIMEOUT = max(getattr(_mv_gemini_mod, "GEMINI_TIMEOUT", 60), 240)
    except Exception:
        pass
except Exception as _mv_err:
    print(f"[movie] لود ماژول فیلم‌یاب ناموفق: {type(_mv_err).__name__}: {_mv_err}", flush=True)
    MOVIE_OK = False

    class MvQuotaExceededError(Exception):
        pass

MOVIE_CB_PREFIXES = (
    "similar", "actors", "actor", "director",
    "posterhd", "trailer", "movie", "seasons", "season", "ep",
    "new_search", "history",
)

from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    TEHRAN_TZ = ZoneInfo("Asia/Tehran")
except Exception:
    TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
    InputMediaDocument, InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.constants import ParseMode
try:
    from telegram_split import send_file_smart, cleanup_path, split_file
    SPLIT_OK = True
except Exception:
    SPLIT_OK = False
    async def send_file_smart(bot, chat_id, path, caption=None, is_audio=False, chunk_size=None):
        with open(path, "rb") as f:
            return await bot.send_document(chat_id, document=f, caption=caption or "", read_timeout=3600, write_timeout=3600)
    def cleanup_path(path, also_parts=True):
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass

from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

URL_RE = dl.URL_RE

# کلید YouTube Data API v3 — اختیاریه (فقط برای غنی‌سازی خلاصه‌ی AI با توضیحات/فصل‌های
# رسمی ویدیو استفاده می‌شه، نه برای گرفتن رونوشت). اگه نبود، خلاصه‌ی AI فقط از روی
# زیرنویس/رونوشت کار می‌کنه. توی bridge_config.py اضافه کن:
#   YOUTUBE_DATA_API_KEY = "..."
YOUTUBE_DATA_API_KEY = getattr(cfg, "YOUTUBE_DATA_API_KEY", None) or os.environ.get("YOUTUBE_DATA_API_KEY")


# سرعت آپلود واقعی ایران معمولاً خیلی کمتر از خواندن دیسک است.
# نوار را با زمان + سرعت یادگرفته‌شده تخمین می‌زنیم (نه با .read فایل).


def _upload_speed_path():
    return os.path.join(_data_dir(), "upload_speed.json")


def get_assumed_upload_bps():
    """بایت بر ثانیه — پیش‌فرض ~20 KB/s، بعد از هر آپلود واقعی آپدیت می‌شه."""
    try:
        with open(_upload_speed_path(), encoding="utf-8") as f:
            data = json.load(f)
        bps = float(data.get("bps", 0))
        if 5 * 1024 <= bps <= 500 * 1024:
            return bps
    except Exception:
        pass
    return 20 * 1024


def save_measured_upload_bps(bps):
    try:
        bps = float(bps)
        if bps < 1024:
            return
        old = get_assumed_upload_bps()
        blended = max(5 * 1024, min(500 * 1024, 0.8 * bps + 0.2 * old))
        with open(_upload_speed_path(), "w", encoding="utf-8") as f:
            json.dump({"bps": blended, "last": bps, "ts": time.time()}, f)
    except Exception as e:
        print(f"[upload-speed] save failed: {e}", flush=True)


def _fmt_speed(bps):
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def _fmt_eta(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} ثانیه"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}:{s:02d}"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


class _UploadProgressFile:
    """فقط سازگاری با InputFile — پیشرفت واقعی زمان‌محور است، نه از روی read دیسک."""
    def __init__(self, f, total, on_progress=None):
        self._f = f
        self._total = total
        self._sent = 0
        self._on_progress = on_progress

    def read(self, n=-1):
        chunk = self._f.read(n)
        self._sent += len(chunk)
        return chunk

    def __getattr__(self, name):
        return getattr(self._f, name)

# وضعيت مكالمه هر كاربر: {chat_id: {"step":..., "inf":..., "job":...}}
SESS = {}
# ايونت‌هاي لغو دانلود: {cancel_id: asyncio.Event}
CANCEL_EVENTS = {}
# توقف موقت صف دانلود
QUEUE_PAUSED = False
QUEUE_PAUSE_EVENT = None  # asyncio.Event set when resume

# کش متن آهنگ: از قبل (وقتی آهنگ پیدا/فرستاده می‌شه) گرفته می‌شه تا دکمه‌ی
# «📝 متن آهنگ» فوری جواب بده، نه اینکه موقع زدن دکمه بره جستجو کنه.
LYRICS_CACHE = {}
_LYRICS_CACHE_ORDER = []
LYRICS_CACHE_MAX = 200

# جلوگیری از اجرای دوباره‌ی همون جاب (لوپ دانلود/آپلودِ همون لینک)
_LYRICS_BUSY = set()   # lyrics_id های در حال گرفتن متن
_SONG_BUSY = {}        # (chat_id, url) -> True : فلوی «تشخیص آهنگ» در حال اجراست
_MOVIE_BUSY = {}       # (chat_id, url) -> True : فلوی «شناسایی فیلم» در حال اجراست


def _strip_lyrics(text):
    """تیکهی تایمکد [mm:ss] رو از متن synced برمیداره."""
    text = str(text).strip()
    if "[" in text and "]" in text:
        lines = []
        for line in text.splitlines():
            line = re.sub(r"\[\d+:\d+[.\d]*\]", "", line).strip()
            if line:
                lines.append(line)
        text = "\n".join(lines)
    return text


async def _fetch_lrclib(params):
    """یه جستجوی lrclib با پارامترهای دلخواه (get یا search) — متن برمیگردونه یا None."""
    import aiohttp
    import urllib.parse as _up
    try:
        url = f"https://lrclib.net/api/{params['mode']}?{params['qs']}"
        async with aiohttp.ClientSession(trust_env=True) as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if params["mode"] == "get":
                        lyrics = (data or {}).get("plainLyrics") or (data or {}).get("syncedLyrics")
                        if lyrics and str(lyrics).strip():
                            return _strip_lyrics(lyrics)
                    elif isinstance(data, list) and data:
                        hit = data[0]
                        lyrics = hit.get("plainLyrics") or hit.get("syncedLyrics")
                        if lyrics and str(lyrics).strip():
                            return _strip_lyrics(lyrics)
    except Exception as e:
        print(f"[lyrics] lrclib {params.get('mode')}: {e}", flush=True)
    return None


_IR_LYRICS_SITES = ("systemmusic.ir", "melonmusic.ir", "krakenmusic.ir", "opxmusic.ir", "imimusic.ir",
                    "songsara.net", "musicdel.ir", "irtext.ir", "lachini.com", "lyricstranslate.com",
                    "tarafdari.com")
_IR_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/126.0.0.0 Safari/537.36")


def _ir_clean(body):
    body = re.sub(r"(?is)<script.*?</script>", " ", body)
    body = re.sub(r"(?is)<style.*?</style>", " ", body)
    body = re.sub(r"(?is)<!--.*?-->", " ", body)
    body = re.sub(r"(?is)<noscript.*?</noscript>", " ", body)
    return body


_IR_UI = re.compile(
    r"دانلود|دانلد|کیفیت|پخش آنلاین|پخش آهنگ|ام پی|مگابایت|128|320|تگ\b|برچسب|نظرات|دیدگاه|"
    r"اشتراک|کپی|کلیک|بیشتر|ادامه مطلب|ورود|ثبت نام|جستجو|دسته\b|تماس|متن آهنگ|کپشن|"
    r"دانلود آهنگ|ترانه سرا|خواننده|مدت زمان|بازدید|تیم ها|بازیکن|اخبار|درباره|ورزش|"
    r"کد خبر|خبرنگار|انتشار|گزارش", re.I)


def _ir_looks_like_verse(line):
    """خطِ بندِ متن باید تقریباً همه فارسی باشه، بدون عدد و بدون اسم ناوبری."""
    if re.search(r"[0-9\u06F0-\u06F9]", line):
        return False
    fa = len(re.findall(r"[\u0600-\u06FF]", line))
    tot = len(re.sub(r"\s", "", line))
    if tot == 0 or fa / tot < 0.55:
        return False
    rm = line.replace(" ", "")
    return len(rm) <= 90


def _ir_block_from(lines, start):
    # هدرِ صفحه (عنوان آهنگ) معمولاً همون خطِ شروع رو داره → از «دیده‌شده‌ها» حذفش کن
    # که بلاک زودتر از موقع نشکنه
    start_norm = re.sub(r"[^\u0600-\u06FF]+", "", lines[start]) if lines else ""
    seen_before = set(
        re.sub(r"[^\u0600-\u06FF]+", "", ln)
        for ln in lines[:start] if ln and re.sub(r"[^\u0600-\u06FF]+", "", ln) != start_norm
    )
    out, blank = [], 0
    for ln in lines[start:]:
        norm = re.sub(r"[^\u0600-\u06FF]+", "", ln)
        if _IR_UI.search(ln) or not norm or re.search(r"[0-9\u06F0-\u06F9]", ln):
            blank += 1
            if blank >= 2:
                break
            continue
        blank = 0
        if norm and norm in seen_before:  # بند آهنگِ دیگه/قبلی دوباره اومد = پایان این متن
            break
        out.append(ln)
        # سقف واقعاً بالا فقط برای جلوگیری از گرفتن کل صفحه (نه محدود کردن آهنگ‌های
        # بلند — خیلی از ترانه‌های فارسی راحت از ۳۴ خط (سقف قبلی) بیشترن).
        if len(out) >= 200:
            break
    return "\n".join(out)


def _ir_extract(body, key):
    """از صفحهی سایت موزیک ایرانی، بندِ متن آهنگ رو جدا می‌کنه.

    همه‌ی جاهایی که key اومده رو امتحان می‌کنه (عنوان صفحه رو رد می‌کنه چون خطِ
    شروع باید شبیه مصرع باشه) و اولین بلوک معتبر (۲۰۰+ کاراکتر) رو برمی‌گردونه.
    """
    import html as _html
    txt = re.sub(r"<br\s*/?>", "\n", _ir_clean(body), flags=re.I)
    txt = re.sub(r"</(p|div|h\d|li|span)>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    lines = [ln.strip() for ln in _html.unescape(txt).splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if key not in ln or _IR_UI.search(ln):
            continue
        if not _ir_looks_like_verse(ln):
            continue
        text = _ir_block_from(lines, i)
        if len(text) >= 200:
            return text
    return None


async def _fetch_ir_site_lyrics(artist, title):
    """جایگزین MusicXMatch: متن کامل آهنگ از سایت‌های موزیک ایرانی (رایگان، بدون کلید).

    خیلی از آهنگ‌های فارسی توی جستجوی خود سایت‌ها نیستن ولی صفحه‌شون توی DDG ایندکس
    شده؛ پس اول DDG html، بعد صفحه رو می‌گیره و بند متن رو جدا می‌کنه. سرورهای این
    سایت‌ها داخلی‌ان، پس از IP ایران و وی‌پی‌ان هر دو باز می‌شن.
    """
    if not title:
        return None
    words = [w for w in title.strip().split() if re.search(r"[\u0600-\u06FF]", w)]
    # چند کلید برای پیدا کردن شروع بند: ۲ کلمهی اول فارسی، بعد ۲ کلمهی آخر
    # («هایده افسانه هستی» → اول «هایده افسانه»، بعد «افسانه هستی» = اسم واقعی آهنگ)
    if len(words) >= 3:
        # «هایده افسانه هستی» → اول «هایده افسانه»، بعد «افسانه هستی» (اسم واقعی آهنگ)
        keys = [" ".join(words[:2]), " ".join(words[-2:])]
    elif len(words) >= 2:
        keys = [" ".join(words[:2])]
    else:
        keys = [title.strip()]
    q = f"{title.strip()} {artist or ''} متن آهنگ".strip()
    return await asyncio.to_thread(_ir_site_sync, q, keys)


def _ir_site_sync(q, keys):
    """جستجو+دریافتِ هم‌زمان (تو thread) — curl_cffi چون DDG فینگرپرینت aiohttp رو چلنج می‌کنه."""
    try:
        import curl_cffi.requests as _cr
        s = _cr.Session(impersonate="chrome")
        s.headers.update({"User-Agent": _IR_UA, "Referer": "https://duckduckgo.com/",
                          "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"})
    except Exception as e:
        print(f"[lyrics] ir-site curl_cffi: {e}", flush=True)
        return None
    import urllib.parse as _up
    try:
        resp = s.get("https://html.duckduckgo.com/html/?q=" + _up.quote(q), timeout=12)
        body = resp.text
        candidates = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
            href = m.group(1)
            mu = re.search(r"uddg=([^&]+)", href)
            if mu:
                href = _up.unquote(mu.group(1))
            dom = re.search(r"https?://([^/]+)", href)
            if dom and any(sd in dom.group(1).lower() for sd in _IR_LYRICS_SITES):
                candidates.append(href)
    except Exception as e:
        print(f"[lyrics] ir-site search: {e}", flush=True)
        return None
    page = None
    if not candidates:
        # فالبک: جستجوی خود سایت‌ها (آهنگ‌هایی که توی ایندکس داخلیشون هست)
        try:
            for dom in _IR_LYRICS_SITES:
                u = f"https://{dom}/?s={_up.quote(key)}"
                r2 = s.get(u, timeout=8)
                if r2.status_code == 200 and key in r2.text:
                    candidates.append(u)
                    break
        except Exception:
            pass
    # ترتیب ترجیح: اول سایت‌های متن‌محور/سریع، بعد بقیه (مثل tarafdari که از IP خارجی فیلتره)
    prefer = ("lachini.com", "systemmusic.ir", "songsara.net", "musicdel.ir", "irtext.ir",
              "lyricstranslate.com", "krakenmusic.ir", "melonmusic.ir", "opxmusic.ir", "imimusic.ir",
              "tarafdari.com")
    try:
        candidates.sort(key=lambda u: next((i for i, d in enumerate(prefer) if d in u), 99))
    except Exception:
        pass
    for url in candidates[:4]:
        try:
            rp = s.get(url, timeout=10)
        except Exception as e:
            print(f"[lyrics] ir-site {url[:70]}: {e}", flush=True)
            continue
        if rp.status_code != 200:
            continue
        for k in keys:
            try:
                out = _ir_extract(rp.text, k)
            except Exception as e:
                print(f"[lyrics] ir-site extract({k}): {e}", flush=True)
                continue
            if out:
                return out
    return None


async def fetch_lyrics(artist, title, raw_title=None):
    """
    متن کامل آهنگ — رایگان، بدون کلید.
    اول lyrics.ovh، بعد lrclib.net (معمولاً متن کامل‌تر).
    """
    if not title:
        return None
    artist = (artist or "").strip()
    title = title.strip()
    import aiohttp
    import urllib.parse as _up

    # عنوان/خوانندهٔ جایگزین برای آهنگ‌های فارسی (لاتین ↔ فارسی)
    title_variants = [title]
    fa_only = " ".join(re.findall(r"[\u0600-\u06FF]+", title)).strip()
    if fa_only and fa_only not in title_variants:
        title_variants.append(fa_only)
    # حذف نام خواننده از داخل title اگر قاطی شده
    for a_try in filter(None, [artist, globals().get("_ARTIST_ALIASES", {}).get((artist or "").lower())]):
        if a_try and a_try in title:
            cleaned = title.replace(a_try, "").strip(" -–—|")
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if cleaned and cleaned not in title_variants:
                title_variants.insert(0, cleaned)

    artist_variants = []
    if artist:
        artist_variants.append(artist)
        alias = None
        try:
            alias = _ARTIST_ALIASES.get(artist.lower())
        except Exception:
            alias = None
        if alias and alias not in artist_variants:
            artist_variants.append(alias)
        # لاتین ← اگر فقط فارسی داریم
        inv = {v: k for k, v in list((_ARTIST_ALIASES or {}).items()) if v == artist}
        for lat in inv:
            if lat not in artist_variants:
                artist_variants.append(lat.title() if lat.islower() else lat)
    if not artist_variants:
        artist_variants = [""]

    # 1) lyrics.ovh — چند ترکیب
    for a in artist_variants[:3]:
        for t in title_variants[:3]:
            try:
                url = f"https://api.lyrics.ovh/v1/{_up.quote(a or 'unknown')}/{_up.quote(t)}"
                async with aiohttp.ClientSession(trust_env=True) as sess:
                    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            lyrics = (data or {}).get("lyrics")
                            if lyrics and lyrics.strip():
                                return lyrics.strip()
            except Exception as e:
                print(f"[lyrics] lyrics.ovh: {e}", flush=True)

    # 2) lrclib.net — plain lyrics کامل (get مستقیم) با چند ترکیب
    for a in artist_variants[:3]:
        for t in title_variants[:3]:
            hit = await _fetch_lrclib({
                "mode": "get",
                "qs": _up.urlencode({"artist_name": a or "Unknown", "track_name": t}),
            })
            if hit:
                return hit

    # 2b) جستجوی LRCLIB با چند شکل مختلف.
    lr_queries = []
    for a in artist_variants[:3]:
        for t in title_variants[:3]:
            for q in (f"{a} {t}".strip(), f"{t} {a}".strip(), t):
                q = re.sub(r"\s+", " ", q).strip()
                if q and q not in lr_queries:
                    lr_queries.append(q)
    for t in title_variants[:3]:
        simple_title = re.sub(r"[-–—|]", " ", t)
        simple_title = re.sub(r"\s+", " ", simple_title).strip()
        if simple_title and simple_title not in lr_queries:
            lr_queries.append(simple_title)

    for q in lr_queries:
        hit = await _fetch_lrclib({
            "mode": "search",
            "qs": _up.urlencode({"q": q}),
        })
        if hit:
            print(f"[lyrics] lrclib match for query: {q}", flush=True)
            return hit

    # 3) سایت‌های موزیک ایرانی — متن کامل آهنگ‌های فارسی
    for a in artist_variants[:3]:
        for t in title_variants[:3]:
            hit = await _fetch_ir_site_lyrics(a, t)
            if hit:
                return hit

    # 2c) بعضی کانالها وسط عنوان یه برچسب لاتین میذارن («Soghati | هایده - سوغاتی»).
    # وقتی عنوان فارسی / آرتیست فارسی جواب نداد، اون بخش لاتین رو هم به‌عنوان track امتحان کن —
    # lrclib برای عنوان لاتین معمولاً ایندکس بهتری داره.
    if raw_title:
        import re as _re
        latin = _re.search(r"[A-Za-z][A-Za-z0-9 .?!'-]{2,}", str(raw_title))
        if latin:
            lat_track = latin.group(0).strip()
            if lat_track and lat_track.lower() != title.lower():
                hit = await _fetch_lrclib({
                    "mode": "get",
                    "qs": _up.urlencode({"artist_name": artist or "Unknown", "track_name": lat_track}),
                })
                if hit:
                    return hit

    return None


async def send_full_lyrics(ctx, chat_id, lyrics_text, artist=None, title=None):
    """کل متن آهنگ رو می‌فرسته؛ اگه بلند بود چند پیام پشت‌سرهم."""
    if not lyrics_text or not str(lyrics_text).strip():
        await ctx.bot.send_message(chat_id, "❌ متن آهنگ پیدا نشد.")
        return
    text = str(lyrics_text).strip()
    header = "📝 متن آهنگ"
    if title:
        header += f" — {title}"
    if artist:
        header += f" ({artist})"
    header += ":\n\n"

    # محدودیت تلگرام ~4096؛ با حاشیه امن ۳۵۰۰
    max_chunk = 3500
    if len(header) + len(text) <= 4000:
        await ctx.bot.send_message(chat_id, header + text)
        return

    # چند پیام — کل متن بدون قطع شدن
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chunk:
            chunks.append(remaining)
            break
        # ترجیحاً از آخر خط بشکن
        cut = remaining.rfind("\n", 0, max_chunk)
        if cut < max_chunk // 2:
            cut = max_chunk
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")

    total = len(chunks)
    for i, chunk in enumerate(chunks):
        if i == 0:
            prefix = f"{header}(قسمت {i+1}/{total})\n"
        else:
            prefix = f"📝 ادامه متن آهنگ ({i+1}/{total}):\n\n"
        await ctx.bot.send_message(chat_id, prefix + chunk)
        if i < total - 1:
            await asyncio.sleep(0.3)


def _cache_lyrics(lyrics_text, label):
    """متن آهنگ رو تو کش می‌ذاره و یه آیدی کوتاه برای دکمه برمی‌گردونه."""
    lyrics_id = f"ly{len(_LYRICS_CACHE_ORDER)}_{abs(hash(label)) % 100000}"
    LYRICS_CACHE[lyrics_id] = lyrics_text
    _LYRICS_CACHE_ORDER.append(lyrics_id)
    while len(_LYRICS_CACHE_ORDER) > LYRICS_CACHE_MAX:
        old_id = _LYRICS_CACHE_ORDER.pop(0)
        LYRICS_CACHE.pop(old_id, None)
    return lyrics_id


def _cache_lyrics_ref(artist, title, lyrics_text=None, raw=None):
    """
    همیشه یه آیدی برای دکمه‌ی متن آهنگ می‌سازه.
    اگه متن از قبل باشه ذخیره می‌شه؛ وگرنه موقع زدن دکمه گرفته می‌شه.
    """
    label = f"{artist or ''}-{title or ''}-{time.time()}"
    lyrics_id = f"ly{len(_LYRICS_CACHE_ORDER)}_{abs(hash(label)) % 100000}"
    if lyrics_text:
        LYRICS_CACHE[lyrics_id] = lyrics_text
    else:
        LYRICS_CACHE[lyrics_id] = {
            "pending": True,
            "artist": (artist or "").strip(),
            "title": (title or "").strip(),
            "raw": (raw or "").strip(),
        }
    _LYRICS_CACHE_ORDER.append(lyrics_id)
    while len(_LYRICS_CACHE_ORDER) > LYRICS_CACHE_MAX:
        old_id = _LYRICS_CACHE_ORDER.pop(0)
        LYRICS_CACHE.pop(old_id, None)
    return lyrics_id

def _data_dir():
    """پوشه داده پایدار روی VPS."""
    base = getattr(dl, "DOWNLOAD_DIR", None) or getattr(cfg, "DOWNLOAD_DIR", None)
    if not base or not isinstance(base, str):
        base = os.environ.get("DOWNLOAD_DIR", "/opt/bridge_bot/downloads")
    path = os.path.join(base, "bot_data")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        path = getattr(dl, "SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
    return path


DATA_DIR = _data_dir()
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
HISTORY_MAX = 40
NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# انتخاب‌های قبلی برای هر لینک (کیفیت/آهنگ/...)
LINK_PREFS_FILE = os.path.join(DATA_DIR, "link_prefs.json")
LINK_PREFS_MAX = 200


def _canon_url(url):
    """لینک بدون پارامتر اضافی برای کلید ذخیره."""
    if not url:
        return ""
    u = url.strip()
    u = re.sub(r"[?&](si|feature|pp|t|list|index)=[^&]*", "", u)
    u = u.rstrip("?&")
    return u


def load_link_prefs():
    try:
        with open(LINK_PREFS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_link_pref(url, job):
    """انتخاب کاربر برای این لینک رو ذخیره می‌کنه."""
    key = _canon_url(url) or media_id_from_url(url, job.get("inf") if isinstance(job, dict) else None)
    if not key:
        return
    try:
        prefs = load_link_prefs()
        snap = {
            "url": url,
            "is_audio": bool(job.get("is_audio")),
            "fmt": job.get("fmt"),
            "ig_extract_song": bool(job.get("ig_extract_song")),
            "ig_kind": job.get("ig_kind"),
            "send_to_telegram": bool(job.get("send_to_telegram", False)),
            "caption": job.get("caption"),
            "tag_artist": job.get("tag_artist"),
            "tag_title": job.get("tag_title"),
            "label": _pref_label(job),
            "ts": time.time(),
        }
        prefs[key] = snap
        # نگه داشتن آخرین‌ها
        if len(prefs) > LINK_PREFS_MAX:
            items = sorted(prefs.items(), key=lambda x: x[1].get("ts", 0), reverse=True)
            prefs = dict(items[:LINK_PREFS_MAX])
        with open(LINK_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=0)
    except Exception as e:
        print(f"[prefs] save failed: {e}", flush=True)


def get_link_pref(url):
    key = _canon_url(url)
    if not key:
        return None
    prefs = load_link_prefs()
    return prefs.get(key)


def _pref_label(job):
    if job.get("ig_extract_song"):
        return "🎵 استخراج آهنگ"
    if job.get("is_audio"):
        return "🎵 فقط صدا (MP3)"
    fmt = job.get("fmt")
    if not fmt or fmt == "best":
        return "⭐ بهترین کیفیت ویدیو"
    return f"🎬 کیفیت {fmt}"


async def tg_call(fn, *args, **kwargs):
    """
    هر تماس تلگرام را تا وصل شدن دوباره تلاش می‌کند (TimedOut/NetworkError).
    """
    from telegram.error import BadRequest, NetworkError, TimedOut
    n = 0
    while True:
        try:
            return await fn(*args, **kwargs)
        except (TimedOut, NetworkError) as e:
            n += 1
            wait = min(30, 2 + n)
            print(f"[tg-retry] #{n} {type(e).__name__}: {e} → {wait}s", flush=True)
            await asyncio.sleep(wait)
        except BadRequest as e:
            # سقف سختِ خود تلگرام (۴۰۹۶) — متن را تکه‌تکه می‌فرستیم تا هیچ‌چیز حذف نشود
            if "too long" in str(e).lower() and args and isinstance(args[0], str):
                txt = args[0]
                rest = list(args[1:])
                head, tail = txt[:4000], txt[4000:]
                out = await fn(head, *rest, **{k: v for k, v in kwargs.items()
                                               if k != "reply_markup"})
                chat_id = getattr(getattr(out, "chat", None), "id", None)
                sender = getattr(getattr(out, "get_bot", lambda: None)(), "send_message", None)
                while tail and sender and chat_id:
                    chunk, tail = tail[:4000], tail[4000:]
                    kw = dict(kwargs) if not tail else {k: v for k, v in kwargs.items()
                                                        if k != "reply_markup"}
                    out = await sender(chat_id, chunk, **kw)
                return out
            raise
        except Exception:
            raise

# لینک اشتراک: t.me/<BOT_USERNAME>?start=s_XXXX
# فقط حروف/عدد/زیرخط — حداکثر ۶۴ کاراکتر (محدودیت تلگرام)
BOT_USERNAME = getattr(cfg, "BOT_USERNAME", "parham_youtube_downloader_bot").lstrip("@")
SHARE_FILE = os.path.join(DATA_DIR, "shares.json")
IG_CATALOG_FILE = os.path.join(DATA_DIR, "ig_catalog.json")
IG_CATALOG_MAX = 80

# آمار دانلودها برای گزارش هفتگی (feature 8)
STATS_FILE = os.path.join(DATA_DIR, "stats.json")


def _load_json_list(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_json_list(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[stats] ذخیره‌ی {path} ناموفق: {e}", flush=True)


def _detect_source(url):
    if not url:
        return "نامشخص"
    u = url.lower()
    if "instagram.com" in u:
        return "اینستاگرام"
    if "youtube.com" in u or "youtu.be" in u:
        return "یوتیوب"
    if "tiktok.com" in u:
        return "تیک‌تاک"
    if "twitter.com" in u or "x.com" in u:
        return "توییتر/X"
    if "pinterest.com" in u:
        return "پینترست"
    if "facebook.com" in u:
        return "فیسبوک"
    return "سایر"


def log_download_stat(chat_id, url, kind, size_bytes):
    """هر دانلود موفق رو برای گزارش هفتگی ثبت می‌کنه."""
    entries = _load_json_list(STATS_FILE)
    entries.append({
        "ts": _now_tehran().isoformat(),
        "chat_id": chat_id,
        "url": url,
        "source": _detect_source(url),
        "kind": kind,  # "audio" یا "video"
        "size_bytes": size_bytes or 0,
    })
    # فقط ۹۰ روز اخیر رو نگه دار که فایل بی‌نهایت بزرگ نشه
    cutoff = _now_tehran() - timedelta(days=90)
    cleaned = []
    for e in entries:
        try:
            if datetime.fromisoformat(e["ts"]) >= cutoff:
                cleaned.append(e)
        except Exception:
            continue
    _save_json_list(STATS_FILE, cleaned)


def build_stats_report(chat_id, days=7):
    """گزارش متنی آمار دانلود چند روز اخیر برای این چت رو می‌سازه."""
    entries = [e for e in _load_json_list(STATS_FILE) if e.get("chat_id") == chat_id]
    cutoff = _now_tehran() - timedelta(days=days)
    recent = []
    for e in entries:
        try:
            if datetime.fromisoformat(e["ts"]) >= cutoff:
                recent.append(e)
        except Exception:
            continue

    if not recent:
        return f"📊 تو {days} روز اخیر هیچ دانلودی ثبت نشده."

    total_count = len(recent)
    total_bytes = sum(e.get("size_bytes", 0) for e in recent)
    total_mb = total_bytes / 1048576
    total_gb = total_bytes / 1073741824

    by_source = {}
    by_day = {}
    audio_count = 0
    video_count = 0
    for e in recent:
        src = e.get("source", "نامشخص")
        by_source[src] = by_source.get(src, 0) + 1
        try:
            day = datetime.fromisoformat(e["ts"]).strftime("%Y-%m-%d")
        except Exception:
            day = "؟"
        by_day[day] = by_day.get(day, 0) + 1
        if e.get("kind") == "audio":
            audio_count += 1
        else:
            video_count += 1

    top_source = max(by_source.items(), key=lambda kv: kv[1]) if by_source else ("-", 0)
    top_day = max(by_day.items(), key=lambda kv: kv[1]) if by_day else ("-", 0)

    size_str = f"{total_gb:.2f} گیگابایت" if total_gb >= 1 else f"{total_mb:.1f} مگابایت"

    lines = [
        f"📊 *آمار {days} روز اخیر*\n",
        f"📥 تعداد کل دانلود: {total_count}",
        f"💾 حجم کل: {size_str}",
        f"🎵 آهنگ: {audio_count} | 🎬 ویدیو: {video_count}",
        f"🏆 بیشترین منبع: {top_source[0]} ({top_source[1]} تا)",
        f"📅 پرکارترین روز: {top_day[0]} ({top_day[1]} تا)\n",
        "🔻 تفکیک بر اساس منبع:",
    ]
    for src, cnt in sorted(by_source.items(), key=lambda kv: -kv[1]):
        lines.append(f"  • {src}: {cnt}")

    return "\n".join(lines)

MAIN_REPLY_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔗 ساخت لینک"), KeyboardButton("📜 تاریخچه")],
        [KeyboardButton("📚 کتابخانه"), KeyboardButton("📊 آمار")],
        [KeyboardButton("❌ لغو")],
    ],
    resize_keyboard=True,
)


def media_id_from_url(url, inf=None):
    """شناسه یکتای ویدیو برای تشخیص تکراری."""
    if not url and inf:
        url = inf.get("webpage_url") or inf.get("original_url") or ""
    u = (url or "").strip()
    if inf:
        vid = inf.get("id")
        if vid and (inf.get("extractor") or "").lower().startswith("youtube"):
            return f"yt:{vid}"
        if vid and "instagram" in (inf.get("extractor") or "").lower():
            return f"ig:{vid}"
        if vid:
            return f"{inf.get('extractor', 'x')}:{vid}"
    m = re.search(r"(?:youtu\.be/|v=|shorts/)([\w-]{6,})", u)
    if m:
        return f"yt:{m.group(1)}"
    m = re.search(r"instagram\.com/(?:reel|p|tv)/([\w-]+)", u)
    if m:
        return f"ig:{m.group(1)}"
    return f"url:{u[:120]}"


def find_duplicates(media_id, url=None):
    """از تاریخچه + کاتالوگ اینستا فایل‌های تکراری موجود رو پیدا می‌کنه."""
    found = []
    for e in load_history():
        mid = e.get("media_id") or media_id_from_url(e.get("url") or "")
        if mid == media_id or (url and e.get("url") == url):
            p = e.get("path")
            if p and os.path.exists(p):
                found.append({"source": "history", "path": p, "title": e.get("title"), "ts": e.get("ts")})
            elif e.get("file_id"):
                found.append({"source": "history", "file_id": e.get("file_id"), "title": e.get("title"), "ts": e.get("ts")})
    for e in load_ig_catalog():
        mid = e.get("media_id") or media_id_from_url(e.get("url") or "")
        if mid == media_id or (url and e.get("url") == url):
            p = e.get("path")
            if p and os.path.exists(p):
                found.append({"source": "catalog", "path": p, "title": e.get("title"), "ts": e.get("saved_at")})
    return found


def build_dup_kb(media_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 باز کردن فایل موجود", callback_data=f"dup:open:{media_id}")],
        [InlineKeyboardButton("🔗 ساخت لینک شیر از موجود", callback_data=f"dup:share:{media_id}")],
        [InlineKeyboardButton("⬇️ دوباره دانلود کن", callback_data=f"dup:redownload:{media_id}")],
        [InlineKeyboardButton("❌ بیخیال", callback_data="dup:cancel")],
    ])


def get_chapters(inf):
    """لیست فصل‌های یوتیوب: [{title, start, end}]"""
    ch = inf.get("chapters") or []
    out = []
    dur = inf.get("duration") or 0
    for i, c in enumerate(ch):
        start = float(c.get("start_time") or 0)
        end = c.get("end_time")
        if end is None:
            if i + 1 < len(ch):
                end = float(ch[i + 1].get("start_time") or start)
            else:
                end = float(dur) if dur else start + 60
        out.append({
            "title": (c.get("title") or f"فصل {i+1}")[:60],
            "start": start,
            "end": float(end),
        })
    return out


def build_chapters_kb(chapters, selected=None):
    selected = selected or set()
    kb = []
    for i, c in enumerate(chapters[:30]):
        mark = "✅ " if i in selected else "⬜ "
        m0, s0 = divmod(int(c["start"]), 60)
        m1, s1 = divmod(int(c["end"]), 60)
        label = f"{mark}{c['title']} ({m0}:{s0:02d}-{m1}:{s1:02d})"
        kb.append([InlineKeyboardButton(label[:64], callback_data=f"ch:{i}")])
    kb.append([
        InlineKeyboardButton("✅ همه فصل‌ها", callback_data="ch:all"),
        InlineKeyboardButton("⬜ هیچکدام", callback_data="ch:none"),
    ])
    n = len(selected)
    kb.append([InlineKeyboardButton(
        f"ادامه با {n} فصل" if n else "⏭ بدون فصل (کل ویدیو / تایم دستی)",
        callback_data="ch:go",
    )])
    return kb


def build_subs_kb(inf):
    """منوی زیرنویس — اولویت با فارسی."""
    auto = set((inf.get("automatic_captions") or {}).keys())
    man = set((inf.get("subtitles") or {}).keys())
    has_fa = any(x.startswith("fa") for x in auto | man)
    has_en = any(x.startswith("en") for x in auto | man)
    fa_note = "✅ موجود" if has_fa else ("از انگلیسی ترجمه" if has_en else "امتحان می‌کنیم")
    kb = [
        [InlineKeyboardButton(f"🇮🇷 زیرنویس فارسی نرم ({fa_note})", callback_data="subx:fa_soft")],
        [InlineKeyboardButton(f"🔥 زیرنویس فارسی چسبیده روی تصویر ({fa_note})", callback_data="subx:fa_burn")],
        [InlineKeyboardButton("بدون زیرنویس", callback_data="subx:no")],
    ]
    if has_en and not has_fa:
        kb.insert(2, [InlineKeyboardButton(
            "🌐 انگلیسی → فارسی (ترجمه رایگان) + چسبیده",
            callback_data="subx:smart_fa_tr",
        )])
    return InlineKeyboardMarkup(kb)


def _google_translate_fa(text, max_chunk=450):
    """ترجمه رایگان EN→FA بدون API key (endpoint عمومی گوگل)."""
    import urllib.request
    import urllib.parse
    if not text or not text.strip():
        return text
    parts = []
    buf = []
    n = 0
    for w in text.split():
        if n + len(w) + 1 > max_chunk and buf:
            parts.append(" ".join(buf))
            buf, n = [w], len(w)
        else:
            buf.append(w)
            n += len(w) + 1
    if buf:
        parts.append(" ".join(buf))
    out = []
    for p in parts:
        try:
            q = urllib.parse.urlencode({
                "client": "gtx", "sl": "en", "tl": "fa", "dt": "t", "q": p,
            })
            url = "https://translate.googleapis.com/translate_a/single?" + q
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            chunk = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
            out.append(chunk or p)
        except Exception:
            out.append(p)
        time.sleep(0.15)
    return " ".join(out)


def translate_sub_file_to_fa(path):
    """فایل srt/vtt انگلیسی را به فارسی ترجمه و srt جدید می‌سازد."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except Exception:
        return None
    lines = raw.splitlines()
    out_lines = []
    i = 0
    is_vtt = "WEBVTT" in raw[:20] or path.lower().endswith(".vtt")
    while i < len(lines):
        line = lines[i]
        # تایم‌کد
        if "-->" in line or (line.strip().isdigit() and not is_vtt):
            out_lines.append(line)
            i += 1
            text_block = []
            while i < len(lines) and lines[i].strip() and "-->" not in lines[i]:
                if lines[i].strip() not in ("WEBVTT",) and not lines[i].startswith("NOTE"):
                    text_block.append(re.sub(r"<[^>]+>", "", lines[i]).strip())
                i += 1
            joined = " ".join(t for t in text_block if t)
            if joined:
                fa = _google_translate_fa(joined)
                out_lines.append(fa)
            out_lines.append("")
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE") or not line.strip():
            if not is_vtt or line.startswith("WEBVTT"):
                out_lines.append(line)
            i += 1
            continue
        i += 1
    out_path = os.path.splitext(path)[0] + ".fa.srt"
    try:
        # خروجی srt ساده
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines))
        return out_path if os.path.getsize(out_path) > 20 else None
    except Exception as e:
        print(f"[sub-tr] {e}", flush=True)
        return None


async def burn_subtitle_ffmpeg(video_path, srt_path, out_path=None):
    """زیرنویس رو با ffmpeg روی ویدیو حک می‌کنه."""
    if not video_path or not srt_path or not os.path.exists(video_path) or not os.path.exists(srt_path):
        return None
    out_path = out_path or (os.path.splitext(video_path)[0] + "_sub.mp4")
    # escape path for subtitles filter
    srt_esc = srt_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"subtitles='{srt_esc}'",
        "-c:a", "copy", out_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=600)
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            return out_path
    except Exception as e:
        print(f"[burn-sub] {e}", flush=True)
    return None


def extract_caption_text(inf, prefer_langs=("fa", "en")):
    """متن زیرنویس خودکار از info yt-dlp (اگه از قبل extract شده باشه)."""
    for bag_name in ("subtitles", "automatic_captions"):
        bag = inf.get(bag_name) or {}
        for lang in prefer_langs:
            for key, entries in bag.items():
                if not key.startswith(lang):
                    continue
                if not entries:
                    continue
                # معمولاً فقط url دارن نه خود متن — برمی‌گردونیم lang برای دانلود بعدی
                return key
    return None


def _captions_to_text(raw, ext):
    """متن ساده از محتوای زیرنویس (vtt/srv/ttml/json3) استخراج می‌کنه."""
    ext = (ext or "vtt").lower()
    try:
        if ext == "json3":
            data = json.loads(raw)
            parts = []
            for ev in data.get("events") or []:
                for seg in ev.get("segs") or []:
                    t = seg.get("utf8")
                    if t:
                        parts.append(t)
            text = "".join(parts)
        else:
            # vtt / srv1 / srv2 / srv3 / ttml — پاک‌سازی ساده و مقاوم تگ‌ها/تایم‌کدها
            lines = []
            for line in raw.splitlines():
                if "-->" in line or line.strip().isdigit() or line.startswith("WEBVTT"):
                    continue
                line = re.sub(r"<[^>]+>", "", line).strip()
                if line:
                    lines.append(line)
            text = " ".join(lines)
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        return ""


async def _fetch_transcript_text(url, inf):
    """
    متن کامل زیرنویس/کپشن خودکار ویدیو رو برمی‌گردونه (بدون خلاصه‌سازی).

    به‌جای این‌که از دانلودر داخلی زیرنویس yt-dlp استفاده کنیم (که با چند
    زبان/فرمت پشت‌سرهم درخواست می‌زنه و رو یوتیوب زود ۴۲۹ می‌گیره)، فقط
    metadata رو می‌گیریم (بدون دانلود واقعی فایل) و خودمون یک درخواست HTTP
    ساده برای بهترین زیرنویس موجود می‌زنیم — با retry/backoff رو ۴۲۹.
    خروجی: (text, title, err)
    """
    try:
        cookiefile = dl.INSTA_COOKIES if dl.is_instagram_url(url) else dl.COOKIES
        opts = dl.ydl_base(cookiefile=cookiefile)
        opts.update({
            "skip_download": True,
            "quiet": True,
            "ignoreerrors": True,
        })

        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = None
        last_err = None
        for attempt in range(3):
            try:
                info = await asyncio.to_thread(_run)
                if info:
                    break
            except Exception as e:
                last_err = e
                msg = str(e)
                if "429" in msg or "Too Many Requests" in msg:
                    await asyncio.sleep(6 * (attempt + 1))
                    continue
                break
        if not info:
            return None, "", (str(last_err) if last_err else "اطلاعات ویدیو گرفته نشد.")

        title = info.get("title") or (inf or {}).get("title") or ""

        track_url, track_ext = None, "vtt"
        for bag_name in ("subtitles", "automatic_captions"):
            bag = info.get(bag_name) or {}
            for lang in ("fa", "fa-IR", "en", "en-US", "en-orig"):
                found = False
                for key, entries in bag.items():
                    if not key.startswith(lang) or not entries:
                        continue
                    chosen = next((e for e in entries if e.get("ext") == "vtt"), entries[0])
                    track_url, track_ext = chosen.get("url"), chosen.get("ext", "vtt")
                    found = True
                    break
                if found:
                    break
            if track_url:
                break

        if not track_url:
            return None, title, "زیرنویس/رونوشت برای این ویدیو پیدا نشد."

        import aiohttp
        raw = None
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(trust_env=True) as sess:
                    async with sess.get(track_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(6 * (attempt + 1))
                            continue
                        if resp.status != 200:
                            return None, title, f"دانلود زیرنویس ناموفق (HTTP {resp.status})."
                        raw = await resp.text()
                        break
            except Exception as e:
                last_err = e
                await asyncio.sleep(3 * (attempt + 1))
        if not raw:
            return None, title, (str(last_err) if last_err else "زیرنویس مکرر ۴۲۹ داد؛ چند دقیقه دیگه امتحان کن.")

        text = _captions_to_text(raw, track_ext)
        if not text:
            return None, title, "زیرنویس/رونوشت برای این ویدیو پیدا نشد."
        return text, title, None
    except Exception as e:
        return None, "", str(e)


def _simple_summary_from_text(text):
    """خلاصه‌ی ساده (بدون AI): اول + وسط + آخر متن."""
    if not text:
        return None
    words = text.split()
    if len(words) <= 80:
        summary = text
    else:
        head = " ".join(words[:40])
        mid = " ".join(words[len(words) // 2 - 20:len(words) // 2 + 20])
        tail = " ".join(words[-40:])
        summary = f"{head}\n…\n{mid}\n…\n{tail}"
    return summary[:1500]


async def summarize_from_subs(url, inf):
    """
    خلاصه‌ی ساده (بدون AI) — اول + وسط + آخر متن زیرنویس. فقط به‌عنوان
    fallback وقتی خلاصه‌ی هوشمند (AI) در دسترس نیست استفاده می‌شه.
    """
    text, _title, err = await _fetch_transcript_text(url, inf)
    if err or not text:
        return None, err
    return _simple_summary_from_text(text), None




YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def youtube_video_id(url):
    m = YOUTUBE_ID_RE.search(url or "")
    return m.group(1) if m else None


async def fetch_youtube_description_and_chapters(video_id):
    """
    توضیحات ویدیو + فصل‌هایی که خود سازنده تو توضیحات نوشته (اگه باشه) رو از
    YouTube Data API v3 می‌گیره — برای غنی‌کردن پرامپت خلاصه‌ی AI.
    اگه کلید تنظیم نشده باشه یا خطا بخوره، (None, []) برمی‌گردونه (بی‌خطر).
    """
    if not YOUTUBE_DATA_API_KEY or not video_id:
        return None, []
    import aiohttp
    try:
        params = {"part": "snippet", "id": video_id, "key": YOUTUBE_DATA_API_KEY}
        async with aiohttp.ClientSession(trust_env=True) as sess:
            async with sess.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params=params, timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    return None, []
                data = await resp.json()
        items = data.get("items") or []
        if not items:
            return None, []
        desc = (items[0].get("snippet") or {}).get("description") or ""
        chapters = []
        chap_re = re.compile(r"^\s*(\d{1,2}(?::\d{2}){1,2})\s*[-–—:]?\s*(.+)$")
        for line in desc.splitlines():
            m = chap_re.match(line)
            if m:
                chapters.append((m.group(1), m.group(2).strip()))
        return desc[:3000], chapters[:40]
    except Exception as e:
        print(f"[yt-data-api] {e}", flush=True)
        return None, []


def _prepare_transcript_for_ai(text, max_chars=200000):
    """
    برای رونوشت‌های خیلی طولانی، به‌جای بریدن خامِ فقط از اول (که باعث می‌شه
    پایان ویدیو -مثلاً کی برنده شد، امتیاز نهایی هرکس- از دست بره)، هم از اول
    هم از آخر رونوشت نگه می‌داریم. مدل‌های Gemini flash کانتکست خیلی بزرگ
    (حدود ۱ میلیون توکن) رو راحت پشتیبانی می‌کنن، پس max_chars بالا نگه‌داشته
    شده تا عملاً برای اکثر ویدیوها اصلاً بریده نشه و کل جزئیات (مثل امتیازهای
    وسط بازی) تو پرامپت بمونه؛ فقط برای رونوشت‌های واقعاً غول‌آسا یه سقف می‌ذاریم.
    """
    if not text or len(text) <= max_chars:
        return text or ""
    head_len = int(max_chars * 0.6)
    tail_len = max_chars - head_len
    return (
        text[:head_len]
        + "\n...[بخش میانی رونوشت به‌خاطر طولانی بودن حذف شد]...\n"
        + text[-tail_len:]
    )


_AI_SUMMARY_PROMPT = """
تو داری دقیقا همون کاری رو انجام میدی که قابلیت هوش مصنوعی خود یوتیوب («خلاصه‌ی ویدیو
با AI» / پنل Ask) انجام میده: از روی رونوشت واقعی ویدیو (نه از روی عنوان) یک خلاصه
دقیق و مفید بساز و چند سوال پیشنهادی مثل همون سوال‌های کوچیکی که یوتیوب زیر خلاصه
نشون میده تولید کن.

عنوان ویدیو: {title}
{extra}

رونوشت ویدیو (ممکنه بریده‌شده باشه):
\"\"\"{transcript}\"\"\"

فقط یک شیء JSON برگردون، بدون هیچ متن اضافه دور آن، دقیقا با این ساختار:
{{
  "summary": "خلاصه‌ی روان و کوتاه ویدیو در ۲ تا ۴ جمله، به فارسی",
  "key_points": ["نکته‌ی کلیدی ۱", "نکته‌ی کلیدی ۲", "..."],
  "questions": ["سوال پیشنهادی کوتاه ۱", "سوال پیشنهادی کوتاه ۲", "سوال پیشنهادی کوتاه ۳"]
}}

قواعد:
- key_points حداکثر ۶ مورد، هرکدوم یک جمله‌ی کوتاه.
- questions دقیقا ۳ سوال کوتاه و طبیعی که یک بیننده ممکنه بعد از دیدن خلاصه بپرسه
  (مثل «نتیجه‌گیری نهایی چیه؟»، «فرق X و Y چیه؟» بر اساس محتوای واقعی ویدیو).
- همه‌چیز به فارسی روان باشه، مگر اسم‌های خاص/فنی.
"""

_AI_ANSWER_PROMPT = """
تو داری مثل پنل «Ask» هوش مصنوعی خود یوتیوب، بر اساس رونوشت واقعی این ویدیو به
سوال کاربر جواب می‌دی. فقط از اطلاعاتی که تو رونوشت هست استفاده کن؛ اگه جواب تو
رونوشت نبود، صادقانه بگو که تو محتوای ویدیو به این موضوع اشاره نشده.

عنوان ویدیو: {title}

رونوشت ویدیو (ممکنه بریده‌شده باشه):
\"\"\"{transcript}\"\"\"

سوال کاربر: {question}

فقط یک شیء JSON برگردون، بدون هیچ متن اضافه دور آن، دقیقا با این ساختار:
{{"answer": "جواب کامل به فارسی روان (حداکثر ۶-۷ جمله)، بدون تکرار خود سوال"}}
"""


async def ai_summarize_video(url, inf):
    """
    خلاصه‌ی هوشمند (AI) از روی رونوشت واقعی ویدیو — دقیقا همون کاری که خلاصه‌ی
    AI خود یوتیوب انجام می‌ده. از همون پایپ‌لاین Gemini (+fallback خودکار Groq)ی
    که تو movie_bot هست استفاده می‌کنه، بدون اینکه مدل رو دستی انتخاب کنیم.
    خروجی: (result_dict یا None, transcript یا None, err یا None)
    result_dict = {"summary": str, "key_points": [...], "questions": [...]}
    """
    if not MOVIE_OK:
        return None, None, "ماژول هوش مصنوعی (movie_bot) لود نشده — نمی‌شه خلاصه‌ی AI ساخت."

    text, title, err = await _fetch_transcript_text(url, inf)
    if err or not text:
        return None, None, err or "رونوشت پیدا نشد."

    extra = ""
    vid = youtube_video_id(url)
    if vid:
        desc, chapters = await fetch_youtube_description_and_chapters(vid)
        if chapters:
            chs_txt = "\n".join(f"{t} — {label}" for t, label in chapters)
            extra += f"\nفصل‌های اعلام‌شده توسط سازنده (از توضیحات ویدیو):\n{chs_txt}\n"
        elif desc:
            extra += f"\nتوضیحات ویدیو:\n{desc[:800]}\n"

    prompt = _AI_SUMMARY_PROMPT.format(
        title=title or "نامشخص",
        extra=extra,
        transcript=_prepare_transcript_for_ai(text),
    )
    try:
        try:
            raw = await asyncio.to_thread(mv_gemini_generate, prompt, [])
        except MvQuotaExceededError:
            raise
        except Exception as e1:
            if "timed out" not in str(e1).lower() and "timeout" not in str(e1).lower():
                raise
            print(f"[ai-summary] تایم‌اوت، یک بار دیگه امتحان می‌کنیم: {e1}", flush=True)
            raw = await asyncio.to_thread(mv_gemini_generate, prompt, [])
        data = mv_gemini_extract_json(raw) or {}
        summary = str(data.get("summary") or "").strip()
        key_points = [str(x).strip() for x in (data.get("key_points") or []) if str(x).strip()][:6]
        questions = [str(x).strip() for x in (data.get("questions") or []) if str(x).strip()][:3]
        if not summary:
            return None, text, "پاسخ هوش مصنوعی خالی بود."
        return {"summary": summary, "key_points": key_points, "questions": questions, "title": title}, text, None
    except MvQuotaExceededError as e:
        return None, text, f"سهمیه‌ی رایگان هوش مصنوعی تمام شده: {e}"
    except Exception as e:
        return None, text, str(e)


async def ai_answer_question(question, transcript, title):
    if not MOVIE_OK:
        return None, "ماژول هوش مصنوعی (movie_bot) لود نشده."
    prompt = _AI_ANSWER_PROMPT.format(
        title=title or "نامشخص", transcript=_prepare_transcript_for_ai(transcript), question=question,
    )
    try:
        try:
            raw = await asyncio.to_thread(mv_gemini_generate, prompt, [])
        except MvQuotaExceededError:
            raise
        except Exception as e1:
            if "timed out" not in str(e1).lower() and "timeout" not in str(e1).lower():
                raise
            print(f"[ai-answer] تایم‌اوت، یک بار دیگه امتحان می‌کنیم: {e1}", flush=True)
            raw = await asyncio.to_thread(mv_gemini_generate, prompt, [])
        # سرویس Gemini همیشه با responseMimeType=json جواب می‌ده، پس همیشه سعی
        # کن اول JSON رو پارس کنی؛ اگه به هر دلیلی متن ساده بود، همون رو بگیر.
        data = mv_gemini_extract_json(raw)
        if isinstance(data, dict):
            answer = str(
                data.get("answer") or data.get("response") or data.get("summary")
                or data.get("text") or data.get("output") or ""
            ).strip()
        else:
            answer = str(raw or "").strip()
        return answer or None, None
    except MvQuotaExceededError as e:
        return None, f"سهمیه‌ی رایگان هوش مصنوعی تمام شده: {e}"
    except Exception as e:
        return None, str(e)


def _now_tehran():
    return datetime.now(TEHRAN_TZ)


def _tehran_str(dt=None):
    if dt is None:
        dt = _now_tehran()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(TEHRAN_TZ)
    else:
        dt = dt.astimezone(TEHRAN_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def load_ig_catalog():
    try:
        if os.path.exists(IG_CATALOG_FILE):
            with open(IG_CATALOG_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_ig_catalog_entry(entry):
    """entry: path, title, kind (song|reel), url"""
    try:
        cat = load_ig_catalog()
        entry = dict(entry)
        entry["saved_at"] = _now_tehran().isoformat()
        entry["id"] = entry.get("id") or f"ig{int(_now_tehran().timestamp())}{len(cat)}"
        cat.insert(0, entry)
        cat = cat[:IG_CATALOG_MAX]
        with open(IG_CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(cat, f, ensure_ascii=False, indent=2)
        return entry
    except Exception as e:
        print(f"[ig_catalog] save failed: {e}", flush=True)
        return entry


def update_ig_catalog_file_id(path, file_id):
    """شناسه فایل تلگرام را کنار فایل محلی ذخیره می‌کند تا لینک‌های بعدی دوباره آپلود نکنند."""
    if not path or not file_id:
        return False
    try:
        cat = load_ig_catalog()
        changed = False
        for e in cat:
            if e.get("path") == path:
                if e.get("file_id") != file_id:
                    e["file_id"] = file_id
                    changed = True
        if changed:
            with open(IG_CATALOG_FILE, "w", encoding="utf-8") as f:
                json.dump(cat, f, ensure_ascii=False, indent=2)
        return changed
    except Exception as e:
        print(f"[ig_catalog] file_id save failed: {e}", flush=True)
        return False


def get_ig_catalog_file_id(path):
    if not path:
        return None
    try:
        for e in load_ig_catalog():
            if e.get("path") == path and e.get("file_id"):
                return e.get("file_id")
    except Exception:
        pass
    return None


def load_shares():
    try:
        if os.path.exists(SHARE_FILE):
            with open(SHARE_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_shares(shares):
    try:
        with open(SHARE_FILE, "w", encoding="utf-8") as f:
            json.dump(shares, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[shares] save failed: {e}", flush=True)


def make_share_id():
    """شناسه معتبر برای start payload تلگرام: s_ + 10 کاراکتر [a-z0-9]"""
    import secrets
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    body = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"s_{body}"


def build_share_link(share_id):
    # فرمت رسمی تلگرام — بدون فاصله و کاراکتر اضافی
    return f"https://t.me/{BOT_USERNAME}?start={share_id}"


def create_share(path, title, kind, max_uses, expire_hours, password=None, file_id=None):
    """
    سهم جدید بساز و لینک معتبر برگردون.
    password: اگه باشه گیرنده باید رمز بزنه (فقط ریلز/آهنگ اینستا).
    """
    if not path or not os.path.exists(path):
        return None, "فایل روی گوشی پیدا نشد"
    shares = load_shares()
    for _ in range(20):
        sid = make_share_id()
        if sid not in shares:
            break
    else:
        return None, "نتونستم شناسه یکتا بسازم"
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=float(expire_hours))).isoformat()
    shares[sid] = {
        "path": path,
        "title": (title or "فایل")[:100],
        "kind": kind or "file",
        "max_uses": int(max_uses),
        "used": 0,
        "expires_at": expires_at,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "password": (str(password).strip() if password else None) or None,
        # اگر فایل قبلاً توسط همین ربات به تلگرام ارسال شده، از file_id استفاده کن
        # تا گیرنده دوباره آپلودش نکند.
        "file_id": file_id or get_ig_catalog_file_id(path),
    }
    save_shares(shares)
    return build_share_link(sid), None


def get_valid_share(share_id, password=None):
    """بررسی وجود، انقضا، سقف دانلود، رمز. برمی‌گردونه (share_dict, error_code, error_msg)."""
    if not share_id or not re.match(r"^s_[a-z0-9]{6,20}$", share_id):
        return None, "invalid", "لینک نامعتبره"
    shares = load_shares()
    rec = shares.get(share_id)
    if not rec:
        return None, "invalid", "این لینک پیدا نشد یا قبلاً حذف شده"
    try:
        exp = datetime.fromisoformat(rec["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            return None, "expired", "⏰ زمان استفاده از این لینک تموم شده."
    except Exception:
        return None, "invalid", "تاریخ انقضای لینک خراب است"
    if int(rec.get("used", 0)) >= int(rec.get("max_uses", 1)):
        return None, "limit", "🚫 ظرفیت استفاده از این لینک تموم شده."
    path = rec.get("path")
    if not path or not os.path.exists(path):
        return None, "missing", "فایل روی گوشی صاحب ربات دیگه نیست"
    need_pw = rec.get("password")
    if need_pw:
        if password is None:
            return rec, "need_password", "🔐 این لینک رمز داره. رمز رو بفرست."
        if str(password).strip() != str(need_pw).strip():
            return rec, "bad_password", "❌ رمز اشتباهه. دوباره بفرست."
    return rec, None, None


# کاتالوگ آهنگ‌های پیدا‌شده (جدا از ویدیو)
SONGS_FILE = os.path.join(DATA_DIR if "DATA_DIR" in dir() else ".", "found_songs.json")


def load_found_songs():
    try:
        p = os.path.join(_data_dir(), "found_songs.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_found_song(entry):
    try:
        p = os.path.join(_data_dir(), "found_songs.json")
        songs = load_found_songs()
        songs.insert(0, {
            **entry,
            "ts": time.time(),
        })
        songs = songs[:100]
        with open(p, "w", encoding="utf-8") as f:
            json.dump(songs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[songs] save failed: {e}", flush=True)


async def run_job_queue(update, ctx, jobs):
    """
    چند جاب پشت‌سرهم با نوار پیشرفت که ETA کل صف رو از سرعت واقعی دانلود نشون می‌ده.
    """
    if not jobs:
        return
    n = len(jobs)
    if n == 1:
        await run_single_job(update, ctx, jobs[0])
        return
    for i, job in enumerate(jobs):
        while QUEUE_PAUSED:
            await asyncio.sleep(2)
        rem = sum(_job_size_bytes(j) for j in jobs[i + 1 :])
        # اگه حجم معلوم نیست، حداقل با تعداد باقی‌مانده حساب می‌کنیم
        qinfo = {
            "idx": i + 1,
            "total": n,
            "remaining_bytes": rem,
            "remaining_jobs": n - i - 1,
        }
        await run_single_job(update, ctx, job, queue_info=qinfo)


def consume_share(share_id):
    shares = load_shares()
    rec = shares.get(share_id)
    if not rec:
        return
    rec["used"] = int(rec.get("used", 0)) + 1
    shares[share_id] = rec
    save_shares(shares)


def owner_only(func):
    async def w(update, ctx, *a, **k):
        uid = update.effective_user.id
        if uid not in cfg.OWNER_ID:
            await update.message.reply_text("⛔ فقط مالک دسترسی داره.")
            return
        return await func(update, ctx, *a, **k)
    return w


def load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_history_entry(entry):
    try:
        hist = load_history()
        hist.insert(0, entry)
        hist = hist[:HISTORY_MAX]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[history] save failed: {e}", flush=True)


def parse_time_to_sec(s):
    """'1:30' یا '90' یا '1:02:03' → ثانیه. None اگر نامعتبر."""
    s = (s or "").strip()
    if not s:
        return None
    if re.match(r"^\d+$", s):
        return int(s)
    parts = s.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def parse_clip_range(text):
    """'5:00-12:30' یا '5:00 12:30' → (start_sec, end_sec) یا None."""
    text = (text or "").strip().replace("–", "-").replace("—", "-")
    m = re.match(r"^(.+?)\s*[- ]\s*(.+)$", text)
    if not m:
        return None
    a, b = parse_time_to_sec(m.group(1)), parse_time_to_sec(m.group(2))
    if a is None or b is None or b <= a:
        return None
    return (a, b)


def _quality_menu_items(inf):
    formats = inf.get("formats", []) or []
    seen = set()
    menu = []
    order = sorted(
        {f.get("height") for f in formats if f.get("vcodec") != "none" and f.get("height")},
        reverse=True,
    )
    for res in order:
        for f in formats:
            if f.get("vcodec") != "none" and f.get("height") == res:
                if res in seen:
                    continue
                seen.add(res)
                fs = f.get("filesize") or f.get("filesize_approx")
                size_str = dl.human_size(fs) if fs and hasattr(dl, "human_size") else ""
                menu.append((res, f.get("ext"), f.get("format_id"), size_str))
                break
    return menu


def build_ig_action_kb(batch=False):
    """منوی اینستا — تک‌لینک و چندلینک یکسان."""
    kb = [
        [InlineKeyboardButton("🎵 استخراج آهنگ", callback_data="ig_audio")],
        [InlineKeyboardButton("🎬 شناسایی فیلم", callback_data="ig_movie")],
        [InlineKeyboardButton("⬇️ دانلود ویدیو", callback_data="q:best")],
        [InlineKeyboardButton("⭐ انتخاب کیفیت", callback_data="ig_video_menu")],
    ]
    if batch:
        kb.append([InlineKeyboardButton(
            "🎯 یک کیفیت برای همه لینک‌ها", callback_data="bulk:menu"
        )])
        kb.append([InlineKeyboardButton("⏭️ رد کردن", callback_data="batch_skip")])
    return kb


def build_quality_kb(inf, is_audio, selected=None, batch=False):
    """
    منوی کیفیت چندانتخابی — تک‌لینک و چندلینک یکسان.
    selected = set of keys مثل {'best', 'audio', '137', ...}
    """
    selected = selected or set()
    menu = _quality_menu_items(inf)
    kb = []
    for res, ext, fid, size_str in menu:
        mark = "✅ " if fid in selected else ""
        label = f"{mark}{res}p [{ext}]"
        if size_str:
            label += f" - {size_str}"
        kb.append([InlineKeyboardButton(label, callback_data=f"qt:{fid}")])
    best_mark = "✅ " if "best" in selected else ""
    kb.append([InlineKeyboardButton(f"{best_mark}⭐ بهترین کیفیت", callback_data="qt:best")])
    if not is_audio:
        audio_mark = "✅ " if "audio" in selected else ""
        kb.append([InlineKeyboardButton(f"{audio_mark}🎵 فقط صدا (MP3)", callback_data="qt:audio")])
        kb.append([InlineKeyboardButton("🔍 جستجوی موزیک", callback_data="music_search")])
        soft_mark = "✅ " if "subs_soft" in selected else ""
        burn_mark = "✅ " if "subs_fa" in selected else ""
        kb.append([InlineKeyboardButton(
            f"{soft_mark}📄 زیرنویس فارسی فایل جدا (.srt)", callback_data="qt:subs_soft"
        )])
        kb.append([InlineKeyboardButton(
            f"{burn_mark}🔥 زیرنویس چسبیده روی تصویر", callback_data="qt:subs_fa"
        )])
        chs = get_chapters(inf)
        if chs:
            kb.append([InlineKeyboardButton(f"📑 فصل‌های ویدیو ({len(chs)} تا)", callback_data="show_chapters")])
        kb.append([InlineKeyboardButton("🤖 خلاصه با هوش مصنوعی (AI)", callback_data="sum:video")])
        cmp_mark = "✅ " if "compress" in selected else ""
        kb.append([InlineKeyboardButton(
            f"{cmp_mark}🗜 فشرده‌سازی هوشمند بعد از دانلود", callback_data="qt:compress"
        )])
    n = len(selected)
    go_label = f"ادامه ✅ ({n} انتخاب)" if n else "ادامه ✅ (حداقل یکی انتخاب کن)"
    kb.append([InlineKeyboardButton(go_label, callback_data="qgo")])
    if batch:
        kb.append([InlineKeyboardButton("⏭️ رد کردن", callback_data="batch_skip")])
        kb.append([
            InlineKeyboardButton("⏸ توقف صف", callback_data="queue:pause"),
            InlineKeyboardButton("▶️ ادامه صف", callback_data="queue:resume"),
        ])
    return kb, menu


def _build_playlist_kb(items, selected):
    """دکمه‌های انتخاب از پلی‌لیست با تیک."""
    kb = []
    for i, it in enumerate(items[:50]):
        mark = "✅ " if i in selected else "⬜ "
        t = (it.get("title") or "بدون عنوان")[:40]
        dur = it.get("duration")
        dur_str = ""
        if dur:
            m, sec = divmod(int(dur), 60)
            dur_str = f" ({m}:{sec:02d})"
        kb.append([InlineKeyboardButton(f"{mark}{t}{dur_str}", callback_data=f"pl:{i}")])
    kb.append([
        InlineKeyboardButton("✅ همه", callback_data="pl:all"),
        InlineKeyboardButton("⬜ هیچکدام", callback_data="pl:none"),
    ])
    n = len(selected)
    kb.append([InlineKeyboardButton(f"ادامه ✅ ({n} ویدیو)", callback_data="pl:go")])
    return kb


def _build_priority_kb(jobs, order):
    """
    order = لیست ایندکس‌ها به ترتیب اولویت انتخاب‌شده.
    هر دکمه = یک جاب؛ با زدن، می‌ره تهِ order و عدد می‌گیره.
    """
    kb = []
    for i, job in enumerate(jobs):
        title = ((job.get("inf") or {}).get("title") or job.get("url") or "?")[:35]
        if i in order:
            pos = order.index(i)
            emoji = NUM_EMOJI[pos] if pos < len(NUM_EMOJI) else f"{pos+1}."
            label = f"{emoji} {title}"
        else:
            label = f"⬜ {title}"
        kb.append([InlineKeyboardButton(label, callback_data=f"prio:{i}")])
    n = len(order)
    if n >= len(jobs) and jobs:
        kb.append([InlineKeyboardButton("🚀 شروع دانلود با این ترتیب", callback_data="prio:start")])
    else:
        kb.append([InlineKeyboardButton(f"باقی‌مانده: {len(jobs)-n} — بزن تا اولویت بدی", callback_data="prio:noop")])
    kb.append([InlineKeyboardButton("🔄 ریست ترتیب", callback_data="prio:reset")])
    return kb


def build_meta_text(inf, with_desc=True):
    title = inf.get("title", "بدون عنوان")
    uploader = inf.get("channel") or inf.get("uploader") or "ناشناس"
    cid = inf.get("channel_id") or inf.get("uploader_id") or ""
    dur = inf.get("duration")
    views = inf.get("view_count")
    likes = inf.get("like_count")
    comments = inf.get("comment_count")
    desc = inf.get("description") or ""
    upload = inf.get("upload_date")
    if upload and len(upload) == 8:
        upload = f"{upload[0:4]}-{upload[4:6]}-{upload[6:8]}"
    lines = [f"📹 {title}"]
    lines.append(f"👤 {uploader}" + (f" @{cid}" if cid else ""))
    if views is not None:
        vc = f"👁 {views:,}"
        if likes is not None:
            vc += f" | 👍 {likes:,}"
        if comments is not None:
            vc += f" | 💬 {comments:,}"
        lines.append(vc)
    if upload:
        lines.append(f"📅 {upload}")
    if dur:
        m, s = divmod(int(dur), 60)
        lines.append(f"⏱ {m}:{s:02d}")
    # کپشن کامل و بی‌برش — اگر از حد تلگرام رد شد، tg_call خودش تکه‌تکه می‌فرستد
    if with_desc and desc.strip():
        lines.append(f"💬 کپشن:\n{desc.strip()}")
    return "\n".join(lines)


def extract_song_query(inf):
    """
    فقط وقتی متادیتای واقعیِ آهنگ موجود باشه یه عبارت جستجو برمی‌گردونه.
    هیچ‌وقت از روی عنوان/کپشن ویدیوی اینستاگرام حدس نمی‌زنه، چون کپشن معمولاً
    هیچ ربطی به اسم آهنگ نداره (اونجا اسم آهنگ نمی‌نویسن). اگه چیزی پیدا نشه None می‌ده.
    """
    track = (inf.get("track") or "").strip()
    artist = (inf.get("artist") or inf.get("album_artist") or "").strip()
    if track and artist:
        return f"{artist} {track}"
    if track:
        return track

    def _reject_if_hashtags(candidate):
        # هشتگ‌ها (#تگ #تگ...) هیچ‌وقت اسم آهنگ نیستن، حتی اگه بعد یه نماد
        # نُت موسیقی اومده باشن (خیلی از ریلزها کپشنشون فقط هشتگه)
        c = (candidate or "").strip()
        if not c:
            return None
        tokens = c.split()
        hashtag_tokens = sum(1 for tk in tokens if tk.startswith("#"))
        if "#" in c and (hashtag_tokens >= 1 or c.count("#") >= 2):
            return None
        return c

    desc = inf.get("description") or ""
    m = re.search(r"(?:Song|Track|Music|Audio|Listen to|Playing|Now playing)[\s:：\-—]+([^\n]{2,80})", desc, re.I)
    if m:
        candidate = _reject_if_hashtags(m.group(1))
        if candidate:
            return candidate
    m = re.search(r"[♫♪]\s*([^\n]{2,80})", desc)
    if m:
        candidate = _reject_if_hashtags(m.group(1))
        if candidate:
            return candidate
    return None


def _resolve_actual_audio_path(path, since_ts=None):
    """
    dl.download() بعضی وقتا مسیر/اسمی برمی‌گردونه که با فایل واقعی رو دیسک
    یکی نیست (بدون پوشه، پسوند فرق‌کرده، بعد از تبدیل صدا [id] از اسم حذف
    شده، و امثالش). به‌جای حدس زدن هر الگوی احتمالی اسم، اگه هیچ‌کدوم از
    راه‌های ساده جواب نداد، آخرین فایلی که تازه تو DOWNLOAD_DIR ساخته شده
    رو برمی‌داریم - این روش به شکل خاص اسم فایل وابسته نیست.
    """
    dl_dir = getattr(cfg, "DOWNLOAD_DIR", "")

    if path:
        p = path if os.path.isabs(path) else os.path.join(dl_dir, path) if dl_dir else path
        if os.path.exists(p):
            return p
        base = os.path.splitext(p)[0]
        for ext in (".mp3", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".flac", ".webm", ".mp4"):
            cand = base + ext
            if os.path.exists(cand):
                return cand
        matches = glob.glob(base + ".*")
        if matches:
            print(f"[resolve] با glob روی اسم پایه پیدا شد: {matches[0]}", flush=True)
            return matches[0]

    # فال‌بک نهایی: جدیدترین فایلی که تازه (بعد از شروع این دانلود) تو
    # DOWNLOAD_DIR ساخته/تغییر کرده - صرف‌نظر از اینکه اسمش چیه
    if since_ts and dl_dir and os.path.isdir(dl_dir):
        best, best_mtime = None, since_ts
        for name in os.listdir(dl_dir):
            full = os.path.join(dl_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                mtime = os.path.getmtime(full)
            except Exception:
                continue
            if mtime >= best_mtime:
                best_mtime, best = mtime, full
        if best:
            print(f"[resolve] با جدیدترین فایل پوشه‌ی دانلود پیدا شد: {best}", flush=True)
            return best

    print(f"[resolve] هیچ فایلی پیدا نشد (raw_path={path!r})", flush=True)
    return None


async def _shazam_recognize(audio_path):
    """منبع شناسایی دوم (رایگان، بدون نیاز به توکن): Shazam Web API.

    چون کتابخونه‌ی shazamio روی termux (کامپایل Rust) نصب نمی‌شه، اینجا
    مستقیماً با پروتکل Shazam کار می‌کنیم: یه تکه از صدا رو با ffmpeg برش
    می‌زنیم و با curl_cffi به endpoint تشخیص شزام می‌فرستیم. برای آهنگای
    فانک/بی‌کلام/ادیت‌شده که AudD ممکنه اشتباه بزنه یا اصلاً جواب نده،
    شزام اغلب نتیجه‌ی دقیق‌تری می‌ده.
    """
    import subprocess
    import json as _json
    from curl_cffi import requests as cffi_req

    if not audio_path or not os.path.exists(audio_path):
        return None

    # طول فایل
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        duration = float((out or b"0").decode(errors="ignore").strip() or 0)
    except Exception:
        duration = 0.0

    # شزام از یه بخش 10 ثانیه‌ای (از ثانیه 15) بهترین نتیجه رو می‌ده
    start = max(0.0, min(15.0, duration / 2 - 5)) if duration > 20 else 0.0
    clip = f"{audio_path}_shazam.wav"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", f"{start:.1f}", "-t", "10", "-i", audio_path,
            "-ac", "1", "-ar", "44100", "-f", "wav", clip,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if not os.path.exists(clip) or os.path.getsize(clip) < 1000:
            return None
    except Exception as e:
        print(f"[shazam] برش صدا ناموفق: {e}", flush=True)
        return None

    def _post():
        with open(clip, "rb") as fh:
            data = fh.read()
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            "Content-Type": "application/octet-stream",
            "Accept": "*/*",
        }
        try:
            r = cffi_req.post(
                "https://www.shazam.com/shazam/v2/en-US/iphone/-/recognize/web",
                data=data, headers=headers, timeout=25,
            )
            if r.status_code != 200:
                print(f"[shazam] HTTP {r.status_code}", flush=True)
                return None
            return r.content
        except Exception as e:
            print(f"[shazam] خطای درخواست: {e}", flush=True)
            return None

    try:
        raw = await asyncio.to_thread(_post)
    except Exception as e:
        print(f"[shazam] to_thread خطا: {e}", flush=True)
        raw = None
    finally:
        try:
            os.remove(clip)
        except Exception:
            pass

    if not raw:
        return None

    try:
        js = _json.loads(raw)
        track = (((js.get("matches") or [{}])[0].get("track") if js.get("matches")
                  else None) or js.get("track"))
        # فرمت‌های مختلف پاسخ شزام رو چک می‌کنیم
        if not track:
            track = (js.get("track") or {})
        if isinstance(track, dict):
            title = track.get("title")
            artist = track.get("subtitle") or track.get("artist")
            if title and artist:
                print(f"[shazam] تشخیص: {artist} - {title}", flush=True)
                return {"title": title.strip(), "artist": artist.strip(), "score": 1.0}
        # گاهی نتیجه توی matches[0].track نیست بلکه توی خودِ js.track
        if isinstance(js.get("track"), dict):
            t = js["track"]
            if t.get("title") and t.get("subtitle"):
                return {"title": t["title"].strip(), "artist": t["subtitle"].strip(), "score": 1.0}
    except Exception as e:
        print(f"[shazam] پارس جواب ناموفق: {e}", flush=True)
        return None

    print("[shazam] نتیجه‌ای پیدا نشد", flush=True)
    return None


async def _shazamapi_recognize(audio_path):
    """تشخیص Shazam با پکیج pure-Python `ShazamAPI`.

    این مسیر عمداً از shazamio استفاده نمی‌کند چون shazamio روی بعضی Termuxها
    به Rust/PyO3 وابسته می‌شود. ShazamAPI قدیمی‌تر است ولی wheel خالص Python دارد.
    چند پنجره‌ی 8 ثانیه‌ای بررسی می‌شود تا Reelهای ادیت‌شده شانس بیشتری داشته باشند.
    """
    if not audio_path or not os.path.exists(audio_path):
        return None

    try:
        from ShazamAPI import Shazam
    except Exception as e:
        print(
            f"[shazam-api] پکیج ShazamAPI نصب نیست/لود نشد: {e} | "
            f"نصب: pip install ShazamAPI pydub requests",
            flush=True,
        )
        return None

    # duration
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        duration = float((out or b"0").decode(errors="ignore").strip() or 0)
    except Exception:
        duration = 0.0

    if duration <= 0:
        starts = [0.0]
    else:
        # 8 ثانیه برای هر fingerprint؛ اول/یک‌سوم/وسط/آخر.
        max_start = max(0.0, duration - 8.0)
        starts = sorted(set([
            0.0,
            max_start * 0.33,
            max_start * 0.66,
            max_start,
        ]))

    clips = []

    def parse_response(resp):
        if not isinstance(resp, dict):
            return None
        # ساختار رایج ShazamAPI: matches[0].track
        matches = resp.get("matches") or []
        candidates = []
        for m in matches:
            if not isinstance(m, dict):
                continue
            t = m.get("track") or m
            if isinstance(t, dict):
                candidates.append(t)
        if isinstance(resp.get("track"), dict):
            candidates.append(resp["track"])

        for t in candidates:
            title = str(t.get("title") or "").strip()
            artist = str(t.get("subtitle") or t.get("artist") or "").strip()
            if title and artist:
                return {"title": title, "artist": artist, "score": 1.0}
        return None

    try:
        for idx, start in enumerate(starts):
            clip = f"{audio_path}.shazamapi_{idx}.mp3"
            clips.append(clip)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", audio_path,
                    "-t", "8", "-vn", "-ac", "1", "-ar", "16000",
                    "-codec:a", "libmp3lame", "-b:a", "128k", clip,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if not os.path.exists(clip) or os.path.getsize(clip) < 1000:
                    continue

                def recognize(path=clip):
                    # ShazamAPI has two incompatible public APIs in the wild:
                    # 0.0.2 uses Shazam(songData).recognizeSong(), while newer
                    # forks use Shazam().recognize_song(path). Support both.
                    gen = None

                    # Newer API (documented by the archived GitHub project).
                    try:
                        sh = Shazam()
                        method = getattr(sh, "recognize_song", None)
                        if callable(method):
                            gen = method(path)
                    except TypeError:
                        gen = None
                    except Exception:
                        gen = None

                    # Old PyPI 0.0.2 API: constructor receives bytes and the
                    # method is camelCase recognizeSong().
                    if gen is None:
                        with open(path, "rb") as fh:
                            song_data = fh.read()
                        sh = Shazam(song_data)
                        method = getattr(sh, "recognizeSong", None)
                        if not callable(method):
                            raise RuntimeError(
                                "نسخه ShazamAPI نه recognize_song دارد نه recognizeSong"
                            )
                        gen = method()

                    for item in gen:
                        # Both APIs yield (offset, response).
                        if isinstance(item, tuple) and len(item) == 2:
                            _offset, response = item
                        else:
                            response = item
                        result = parse_response(response)
                        if result:
                            return result
                    return None

                result = await asyncio.to_thread(recognize)
                if result:
                    print(
                        f"[shazam-api] window={idx} start={start:.1f}s → "
                        f"{result['artist']} - {result['title']}",
                        flush=True,
                    )
                    return result
                print(f"[shazam-api] window={idx}: match نشد", flush=True)
            except Exception as e:
                print(f"[shazam-api] window={idx} خطا: {type(e).__name__}: {e}", flush=True)

        # ─── سرعت عادی جواب نداد؛ ریلزهای فانک/ریمیکس/سریع‌شده معمولاً pitch و
        # tempo‌شون یکنواخت عوض شده (نه فقط tempo)، پس فینگرپرینت با نسخهٔ
        # اصلیِ آهنگ در دیتابیس شزام match نمی‌شه. با asetrate دو نسبت رایج
        # ادیت اینستاگرام رو امتحان می‌کنیم: ۱.۲۵× (برعکسِ «سریع‌شده») و
        # ۰.۸× (برعکسِ «آهسته‌شده»)، فقط روی یک پنجرهٔ میانی برای صرفه‌جویی در زمان.
        mid_start = starts[len(starts) // 2] if starts else 0.0
        for label, ratio in (("۱.۲۵× (اصلاح سریع‌شده)", 1.25), ("۰.۸× (اصلاح آهسته‌شده)", 0.8)):
            clip = f"{audio_path}.shazamapi_speed_{ratio}.mp3"
            clips.append(clip)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-ss", f"{mid_start:.2f}", "-i", audio_path,
                    "-t", "8", "-vn", "-ac", "1",
                    "-af", f"asetrate=16000*{ratio},aresample=16000",
                    "-codec:a", "libmp3lame", "-b:a", "128k", clip,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if not os.path.exists(clip) or os.path.getsize(clip) < 1000:
                    continue

                def recognize_speed(path=clip):
                    with open(path, "rb") as fh:
                        song_data = fh.read()
                    try:
                        sh = Shazam()
                        method = getattr(sh, "recognize_song", None)
                        gen = method(path) if callable(method) else None
                    except Exception:
                        gen = None
                    if gen is None:
                        sh = Shazam(song_data)
                        gen = sh.recognizeSong()
                    for item in gen:
                        response = item[1] if isinstance(item, tuple) and len(item) == 2 else item
                        result = parse_response(response)
                        if result:
                            return result
                    return None

                result = await asyncio.to_thread(recognize_speed)
                if result:
                    print(f"[shazam-api] speed={label} → {result['artist']} - {result['title']}", flush=True)
                    return result
                print(f"[shazam-api] speed={label}: match نشد", flush=True)
            except Exception as e:
                print(f"[shazam-api] speed={label} خطا: {type(e).__name__}: {e}", flush=True)
    finally:
        for clip in clips:
            try:
                if os.path.exists(clip):
                    os.remove(clip)
            except Exception:
                pass

    print("[shazam-api] نتیجه‌ای پیدا نشد", flush=True)
    return None


async def _acr_recognize(audio_path):
    """ACRCloud V1 audio recognition with correct HMAC-SHA1/Base64 signing.

    Important: ACRCloud V1 expects Base64(HMAC-SHA1(...)), NOT the hex digest.
    We also include sample_bytes and try several short windows because Reels
    often contain the useful music only in the middle/end of the clip.
    """
    host = str(getattr(cfg, "ACR_HOST", "") or "").strip()
    key = str(getattr(cfg, "ACR_ACCESS_KEY", "") or "").strip()
    secret = str(getattr(cfg, "ACR_ACCESS_SECRET", "") or "").strip()
    if not (host and key and secret):
        print("[acr] تنظیمات ACRCloud ست نشده، رد شد", flush=True)
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None

    try:
        import hmac, hashlib, base64, unicodedata
        import aiohttp
    except Exception as e:
        print(f"[acr] Import خطا: {e}", flush=True)
        return None

    # Users sometimes paste https://host or a trailing slash from the ACR panel.
    host = re.sub(r"^https?://", "", host, flags=re.I).strip().rstrip("/")
    endpoint = "/v1/identify"
    url = f"https://{host}{endpoint}"

    # Get duration once. ACRCloud recommends short samples and only recognizes
    # the first ~12 seconds of an uploaded sample.
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        duration = float((out or b"0").decode(errors="ignore").strip() or 0)
    except Exception:
        duration = 0.0

    # Three 10-second windows: beginning, middle and end. This is much more
    # reliable for Reels than sending only the first 15 seconds.
    if duration > 0:
        starts = [0.0]
        if duration > 14:
            starts.append(max(0.0, duration / 2.0 - 5.0))
        if duration > 22:
            starts.append(max(0.0, duration - 10.0))
    else:
        starts = [0.0, 10.0, 20.0]

    async def make_clip(start, idx):
        clip = f"{audio_path}.acr_{idx}.mp3"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", f"{max(0.0, start):.2f}",
                "-t", "10", "-i", audio_path,
                "-vn", "-ac", "1", "-ar", "22050", "-b:a", "48k", clip,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            if os.path.exists(clip) and os.path.getsize(clip) > 1000:
                return clip
        except Exception as e:
            print(f"[acr] برش پنجره {idx} ناموفق: {e}", flush=True)
        return None

    async def recognize_clip(clip, idx):
        try:
            with open(clip, "rb") as fh:
                audio_data = fh.read()
        except Exception as e:
            print(f"[acr] خواندن کلیپ {idx} ناموفق: {e}", flush=True)
            return None

        # ACRCloud V1 signature contract:
        # POST + /v1/identify + access_key + data_type + signature_version + timestamp
        # then Base64 of the raw HMAC-SHA1 digest.
        timestamp = str(int(time.time()))
        data_type = "audio"
        sig_version = "1"
        string_to_sign = "\n".join([
            "POST", endpoint, key, data_type, sig_version, timestamp
        ])
        digest = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")

        data = {
            "access_key": key,
            "sample_bytes": str(len(audio_data)),
            "timestamp": timestamp,
            "signature": signature,
            "data_type": data_type,
            "signature_version": sig_version,
        }

        print(
            f"[acr] window={idx} start={starts[idx]:.1f}s size={len(audio_data)} bytes",
            flush=True,
        )

        try:
            form = aiohttp.FormData()
            for k, v in data.items():
                form.add_field(k, v)
            form.add_field(
                "sample", audio_data,
                filename=f"reel_{idx}.mp3",
                content_type="audio/mpeg",
            )
            conn = aiohttp.TCPConnector(family=socket.AF_INET)
            timeout = aiohttp.ClientTimeout(total=75)
            async with aiohttp.ClientSession(connector=conn) as session:
                async with session.post(
                    url, data=form, timeout=timeout,
                    headers={"User-Agent": "bridge_bot/1.0"},
                ) as r:
                    raw = await r.read()
                    if r.status != 200:
                        print(f"[acr] HTTP {r.status}: {raw[:180]!r}", flush=True)
                        return None
        except Exception as e:
            print(f"[acr] خطای درخواست window={idx}: {type(e).__name__}: {e}", flush=True)
            return None

        try:
            js = json.loads(raw)
            status = js.get("status") or {}
            code = status.get("code")
            if code != 0:
                print(f"[acr] window={idx} کد {code}: {status.get('msg')}", flush=True)
                return None
            music = ((js.get("metadata") or {}).get("music") or [])
            if not music:
                print(f"[acr] window={idx}: آهنگی پیدا نشد", flush=True)
                return None

            # Pick the highest-confidence result instead of blindly taking [0].
            def confidence(item):
                sc = item.get("score")
                if isinstance(sc, dict):
                    sc = sc.get("confidence")
                try:
                    return float(sc or 0)
                except Exception:
                    return 0.0

            m = max(music, key=confidence)
            title = (m.get("title") or "").strip()
            artists = m.get("artists") or []
            artist = ((artists[0].get("name") if artists else "") or "").strip()
            score = confidence(m)
            if not title:
                return None
            print(f"[acr] window={idx} → {artist} - {title} (score={score:.1f})", flush=True)
            return {"title": title, "artist": artist, "score": score, "window": idx}
        except Exception as e:
            print(f"[acr] پارس جواب ناموفق window={idx}: {e} | raw={raw[:200]!r}", flush=True)
            return None

    results = []
    clips = []
    try:
        for idx, start in enumerate(starts):
            clip = await make_clip(start, idx)
            if not clip:
                continue
            clips.append(clip)
            res = await recognize_clip(clip, idx)
            if res:
                results.append(res)
                # Do NOT stop after one high-confidence ACR result.
                # ACR can occasionally return a false 100-point match on
                # short/edited Reel audio. All selected windows must be checked
                # so we can require cross-window agreement.
    finally:
        for clip in clips:
            try:
                if os.path.exists(clip):
                    os.remove(clip)
            except Exception:
                pass

    if not results:
        return None

    # Prefer the highest confidence, but if two windows agree on the same
    # normalized track, boost it slightly and use that consensus.
    def norm(v):
        v = unicodedata.normalize("NFKC", str(v or "").lower())
        v = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", v)
        v = re.sub(r"[^\w\u0600-\u06ff]+", " ", v, flags=re.UNICODE)
        return re.sub(r"\s+", " ", v).strip()

    grouped = {}
    for r in results:
        k = (norm(r.get("artist")), norm(r.get("title")))
        grouped.setdefault(k, []).append(r)
    best_group = max(
        grouped.values(),
        key=lambda rs: (
            len(rs),
            max(float(x.get("score") or 0) for x in rs),
        ),
    )

    # IMPORTANT: never trust a single ACRCloud hit for an Instagram Reel.
    # We already observed a false 100/100 result. Require the same track in
    # at least two independent windows. A false positive is worse than
    # returning "not found".
    if len(best_group) < 2:
        only = max(best_group, key=lambda x: float(x.get("score") or 0))
        print(
            f"[acr] تک‌نتیجه رد شد (جلوگیری از false-positive): "
            f"{only.get('artist')} - {only.get('title')} "
            f"(score={float(only.get('score') or 0):.1f})",
            flush=True,
        )
        return None

    best = max(
        best_group,
        key=lambda x: float(x.get("score") or 0),
    ).copy()
    best["score"] = min(100.0, float(best.get("score") or 0) + 10.0)
    print(
        f"[acr] consensus {len(best_group)} windows: "
        f"{best.get('artist')} - {best.get('title')} "
        f"(score={float(best.get('score') or 0):.1f})",
        flush=True,
    )
    return best


async def _audd_recognize(audio_path, token):
    """تشخیص مقاوم‌تر آهنگ از چند پنجره‌ی هم‌پوشان با AudD."""
    import aiohttp
    import unicodedata
    from collections import Counter

    if not audio_path or not os.path.exists(audio_path):
        return None

    # طول فایل را می‌گیریم تا برای ریلزهای کوتاه، offset نامعتبر نسازیم.
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        duration = float((out or b"0").decode(errors="ignore").strip() or 0)
    except Exception:
        duration = 0.0

    if duration > 0:
        points = sorted(set([0.0, duration*0.30, duration*0.60, duration*0.75]))[:4]
    else:
        points = [0.0, 6.0, 12.0, 18.0]

    def norm(s):
        s = unicodedata.normalize("NFKC", str(s or "").strip().lower())
        s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)
        s = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def key(r):
        return f"{norm(r.get('artist'))}|{norm(r.get('title'))}"

    hits = []

    async def one_window(start, dur, _retry=True):
        clip = f"{audio_path}_audd_{int(start*10)}.mp3"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", audio_path,
                "-t", f"{dur:.2f}", "-vn", "-ac", "2", "-ar", "44100",
                "-codec:a", "libmp3lame", "-b:a", "160k", clip,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if not os.path.exists(clip) or os.path.getsize(clip) <= 1000:
                return None
            form = aiohttp.FormData()
            form.add_field("api_token", token)
            form.add_field("return", "apple_music,spotify")
            with open(clip, "rb") as f:
                form.add_field("file", f, filename="reel_audio.mp3", content_type="audio/mpeg")
                async with aiohttp.ClientSession(trust_env=True) as sess:
                    async with sess.post("https://api.audd.io/", data=form,
                                         timeout=aiohttp.ClientTimeout(total=45)) as resp:
                        raw = await resp.text()
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
            result = (data or {}).get("result")
            if isinstance(result, dict) and (result.get("title") or "").strip():
                # AudD یک فیلد score می‌ده (۰ تا ۱) که نشون‌دهنده‌ی اطمینان
                # تشخیصه. برای آهنگای فانک/بی‌کلام/ادیت‌شده که fingerprint
                # ضعیفی دارن اغلب زیر ۰.۷ میاد و نشونه‌ی کاذب‌مثبت بودنه.
                try:
                    result["score"] = float(result.get("score") or 0.0)
                except Exception:
                    result["score"] = 0.0
                print(f"[audd] window ss={start:.1f}s → {result.get('artist')} - {result.get('title')} (score={result['score']:.2f})", flush=True)
                return result
            if isinstance(data, dict) and data.get("error"):
                print(f"[audd] window ss={start:.1f}s error: {data.get('error')}", flush=True)
        except (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError, asyncio.TimeoutError) as e:
            # خطای شبکه‌ای موقتی — یک بار دیگر امتحان کن قبل از ول کردن.
            print(f"[audd] window {start:.1f}s: خطای موقت شبکه ({e}) — تلاش دوباره...", flush=True)
            if _retry:
                await asyncio.sleep(1.5)
                return await one_window(start, dur, _retry=False)
        except Exception as e:
            print(f"[audd] window {start:.1f}s: {e}", flush=True)
        finally:
            try:
                if os.path.exists(clip): os.remove(clip)
            except Exception:
                pass
        return None

    # قبلاً 3 پنجره‌ی 20+ ثانیه‌ای و شرط 2/3 داشتیم؛ یک ریلز می‌تواند فقط
    # در یک پنجره‌ی تمیز match شود. اینجا 4 پنجره‌ی کوتاه‌تر و هم‌پوشان داریم.
    for start in points:
        if duration > 0 and start >= duration:
            continue
        dur = min(15.0, max(3.0, duration-start) if duration > 0 else 15.0)
        r = await one_window(start, dur)
        if r:
            hits.append(r)

    if not hits:
        print("[audd] هیچ پنجره‌ای match نشد", flush=True)
        return None

    counts = Counter(key(h) for h in hits)
    ranked = counts.most_common()

    # 2+ پنجره‌ی یکسان = اطمینان بالا. ولی فقط اگه score (اطمینان AudD) هم
    # قابل‌قبول باشه؛ در غیر این صورت این تشخیص احتمالاً غلطه (مخصوصاً برای
    # آهنگای فانک/بی‌کلام/ادیت‌شده که fingerprint ضعیفی دارن).
    AUDD_MIN_SCORE = 0.7
    if ranked and ranked[0][1] >= 2:
        best = ranked[0][0]
        best_hits = [h for h in hits if key(h) == best]
        best_scores = [h.get("score", 0.0) for h in best_hits]
        best_score = max(best_scores) if best_scores else 0.0
        if best_score < AUDD_MIN_SCORE:
            print(f"[audd] رد شد (score پایین {best_score:.2f} < {AUDD_MIN_SCORE}): {best_hits[0].get('artist')} - {best_hits[0].get('title')}", flush=True)
            return None
        for h in best_hits:
            if key(h) == best:
                print(f"[audd] confirmed {ranked[0][1]}/{len(hits)} → {h.get('artist')} - {h.get('title')}", flush=True)
                return h

    # یک match تنها هم معتبر است؛ ولی فقط اگر score (اطمینان AudD) بالا باشه،
    # وگرنه کاذب‌مثبتِ محتمل است (مخصوصاً ریلزهای ادیت‌شده/فانک/بی‌کلام).
    if len(hits) == 1:
        h = hits[0]
        if h.get("score", 0.0) >= 0.7:
            print(f"[audd] single match → {h.get('artist')} - {h.get('title')}", flush=True)
            return h
        print(f"[audd] single match با score پایین ({h.get('score', 0.0):.2f}) رد شد", flush=True)
        return None

    # اگر نتایج متناقض بودند، قبل از رأی‌گیری ساده یک نکته‌ی مهم را چک می‌کنیم:
    # در ریلز/ادیت‌ها ممکن است چند ثانیه‌ی اول صدای دیگری، دیالوگ یا موزیکِ روی
    # تصویر باشد و آهنگ اصلی فقط در انتهای کلیپ شنیده شود. در این حالت «۳ از ۴»
    # می‌تواند با رأی‌گیری ساده عملاً آهنگ اشتباه را برنده کند.
    # بنابراین دو پنجره‌ی مستقل از انتهای صدا می‌گیریم؛ اگر هر دو یک آهنگ را تأیید
    # کردند، آن نتیجه از رأی‌های پراکنده‌ی ابتدای ریلز معتبرتر است.
    if len(ranked) > 1 and duration > 0:
        tail_starts = []
        for offset in (8.0, 4.0):
            st = max(0.0, duration - offset)
            if not any(abs(st-x) < 0.5 for x in tail_starts):
                tail_starts.append(st)
        tail_hits = []
        for st in tail_starts:
            tail = await one_window(st, min(8.0, max(3.0, duration-st)))
            if tail:
                tail_hits.append(tail)
                print(f"[audd] tail verify ss={st:.1f}s → {tail.get('artist')} - {tail.get('title')}", flush=True)
        tail_counts = Counter(key(h) for h in tail_hits)
        if tail_counts:
            tail_best_key, tail_n = tail_counts.most_common(1)[0]
            if tail_n >= 2:
                chosen = next(h for h in reversed(tail_hits) if key(h) == tail_best_key)
                print(f"[audd] tail confirmed {tail_n}/{len(tail_hits)} → {chosen.get('artist')} - {chosen.get('title')}", flush=True)
                if chosen.get("score", 0.0) < AUDD_MIN_SCORE:
                    print(f"[audd] tail confirmed ولی score پایین ({chosen.get('score', 0.0):.2f}) → رد شد", flush=True)
                    return None
                return chosen
            # اگر فقط یک پنجره‌ی انتهایی جواب داد ولی همان نتیجه یکی از کاندیدهای
            # قبلی هم هست، آن را به‌عنوان تأیید مستقل در نظر می‌گیریم.
            if len(tail_hits) == 1 and key(tail_hits[0]) in counts:
                chosen = tail_hits[0]
                print(f"[audd] tail tie-break → {chosen.get('artist')} - {chosen.get('title')}", flush=True)
                return chosen

        # تأیید مرکزی را فقط بعد از بررسی انتهای کلیپ انجام بده.
        center_start = max(0.0, (duration * 0.5 - 6.0))
        verify_dur = min(15.0, max(3.0, duration-center_start))
        verify = await one_window(center_start, verify_dur)
        if verify and key(verify) in counts:
            print(f"[audd] center verification → {verify.get('artist')} - {verify.get('title')}", flush=True)
            return next(h for h in hits if key(h) == key(verify))

    print(f"[audd] ambiguous: {len(hits)} hits / {len(counts)} different results → lyrics fallback", flush=True)
    return None


async def _shazam_recognize(audio_path):
    """منبع شناسایی دوم (رایگان، بدون نیاز به توکن): Shazam Web API.

    مستقیماً با پروتکل Shazam کار می‌کنیم: یه تکه از صدا رو با ffmpeg برش
    می‌زنیم و با curl_cffi به endpoint تشخیص شزام می‌فرستیم. برای آهنگ‌های
    فارسی که AudD اغلب اصلاً جواب نمی‌ده یا اشتباه می‌زنه، شزام دیتابیس
    کامل‌تری داره و نتیجهٔ دقیق‌تری می‌ده — پس به‌عنوان فالبک اول (قبل از
    STT که خروجی کثیف فارسی میده) استفاده می‌شه.
    """
    import subprocess
    import json as _json
    try:
        from curl_cffi import requests as cffi_req
    except Exception as e:
        print(f"[shazam] curl_cffi در دسترس نیست: {e}", flush=True)
        return None

    if not audio_path or not os.path.exists(audio_path):
        return None

    # طول فایل
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        duration = float((out or b"0").decode(errors="ignore").strip() or 0)
    except Exception:
        duration = 0.0

    # شزام از یه بخش 10 ثانیه‌ای (از وسط) بهترین نتیجه رو می‌ده
    start = max(0.0, min(15.0, duration / 2 - 5)) if duration > 20 else 0.0
    clip = f"{audio_path}_shazam.wav"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", f"{start:.1f}", "-t", "10", "-i", audio_path,
            "-ac", "1", "-ar", "44100", "-f", "wav", clip,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if not os.path.exists(clip) or os.path.getsize(clip) < 1000:
            return None
    except Exception as e:
        print(f"[shazam] برش صدا ناموفق: {e}", flush=True)
        return None

    def _post():
        with open(clip, "rb") as fh:
            data = fh.read()
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            "Content-Type": "application/octet-stream",
            "Accept": "*/*",
        }
        try:
            r = cffi_req.post(
                "https://www.shazam.com/shazam/v2/en-US/iphone/-/recognize/web",
                data=data, headers=headers, timeout=25,
            )
            if r.status_code != 200:
                print(f"[shazam] HTTP {r.status_code}", flush=True)
                return None
            return r.content
        except Exception as e:
            print(f"[shazam] خطای درخواست: {e}", flush=True)
            return None

    try:
        raw = await asyncio.to_thread(_post)
    except Exception as e:
        print(f"[shazam] to_thread خطا: {e}", flush=True)
        raw = None
    finally:
        try:
            os.remove(clip)
        except Exception:
            pass

    if not raw:
        return None

    try:
        js = _json.loads(raw)
        track = (((js.get("matches") or [{}])[0].get("track") if js.get("matches")
                  else None) or js.get("track"))
        if not track:
            track = (js.get("track") or {})
        if isinstance(track, dict):
            title = track.get("title")
            artist = track.get("subtitle") or track.get("artist")
            if title and artist:
                print(f"[shazam] تشخیص: {artist} - {title}", flush=True)
                return {"title": title.strip(), "artist": artist.strip(), "score": 1.0}
        if isinstance(js.get("track"), dict):
            t = js["track"]
            if t.get("title") and t.get("subtitle"):
                return {"title": t["title"].strip(), "artist": t["subtitle"].strip(), "score": 1.0}
    except Exception as e:
        print(f"[shazam] پارس جواب ناموفق: {e}", flush=True)
        return None

    print("[shazam] نتیجه‌ای پیدا نشد", flush=True)
    return None


async def _transcribe_audio_for_lyrics(audio_path):
    """
    رونویسی صدا برای گرفتن متن شعر — فارسی یا هر زبان دیگه (خارجی/فانک هم می‌تونه باشه،
    پس زبان رو اجبار نمی‌کنیم، Whisper خودش auto-detect می‌کنه).
    اولویت: Groq Whisper با temperature=0 (کمتر حدس/هذیان‌گویی می‌کنه؛ مشکل قبلی که
    «گمشد» رو «گمشو» می‌شنید بیشتر به‌خاطر یه پنجرهٔ کوتاه/بی‌کیفیت بود، نه زبان)
    → وگرنه Google Web Speech رایگان (چند پنجرهٔ زمانی، نه فقط ۰ تا ۲۰ ثانیهٔ
    اول که اغلب موزیک بی‌کلامه).
    """
    # 1) Groq Whisper — کل فایل، بدون اجبار به زبان خاص (چون ممکنه آهنگ خارجی/فانک
    # هم باشه، نه فقط فارسی) — auto-detect خودش زبان رو تشخیص می‌ده. برای دقت
    # از temperature=0 استفاده می‌کنیم که کمتر حدس/هذیان‌گویی کنه.
    if getattr(cfg, "GROQ_API_KEY", None):
        try:
            segs = await _groq_transcribe(audio_path)
            if segs:
                text = " ".join((s.get("text") or "").strip() for s in segs if s.get("text"))
                if text and len(text.strip()) > 8:
                    print(f"[lyrics-fb] Groq transcript: {text[:120]}", flush=True)
                    return text.strip()
        except Exception as e:
            print(f"[lyrics-fb] Groq: {e}", flush=True)

    # 2) Google free via speech_recognition — چند پنجره (خیلی از ریلزها ۵-۸ ثانیهٔ
    # اول رو موزیک بی‌کلام/اینترو دارن، نه شعر)
    try:
        import speech_recognition as sr
        r = sr.Recognizer()
        best_text = None
        for start in ("0", "8", "20"):
            wav_path = audio_path + f"_sr_{start}.wav"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-ss", start, "-i", audio_path, "-t", "18",
                    "-ar", "16000", "-ac", "1", wav_path,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if not (os.path.exists(wav_path) and os.path.getsize(wav_path) > 2000):
                    continue

                def _recog(path=wav_path):
                    with sr.AudioFile(path) as source:
                        audio = r.record(source)
                    # اول فارسی، بعد انگلیسی
                    for lang in ("fa-IR", "en-US"):
                        try:
                            return r.recognize_google(audio, language=lang)
                        except Exception:
                            continue
                    return None

                text = await asyncio.to_thread(_recog)
                if text and len(text.strip()) > 8:
                    print(f"[lyrics-fb] Google STT (ss={start}): {text[:120]}", flush=True)
                    # اولین پنجره‌ای که متن معنادار داد کافیه؛ اگه بعداً طولانی‌تر
                    # پیدا شد جایگزینش می‌کنیم
                    if not best_text or len(text) > len(best_text):
                        best_text = text.strip()
                    if len(best_text) > 20:
                        break
            except Exception as e:
                print(f"[lyrics-fb] STT window {start}: {e}", flush=True)
            finally:
                try:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                except Exception:
                    pass
        if best_text:
            return best_text
    except ImportError:
        print("[lyrics-fb] speech_recognition نصب نیست — pip install SpeechRecognition", flush=True)
    except Exception as e:
        print(f"[lyrics-fb] STT ناموفق: {e}", flush=True)
    return None


def _lyrics_phrase_for_search(transcript):
    """از رونوشت، یه عبارت قابل جستجو برای پیدا کردن آهنگ می‌سازه."""
    if not transcript:
        return None
    # پاکسازی
    t = re.sub(r"\s+", " ", transcript).strip()
    # حذف کلمات خیلی کوتاه تکراری
    words = [w for w in re.split(r"[^\w\u0600-\u06FF]+", t) if len(w) > 1]
    if len(words) < 3:
        return t[:80] if len(t) >= 8 else None
    # ۳ تا ۱۰ کلمهٔ میانی معمولاً شعر مشخص‌تری می‌ده
    if len(words) <= 12:
        phrase = " ".join(words)
    else:
        mid = len(words) // 3
        phrase = " ".join(words[mid:mid + 10])
    return phrase.strip() or None


async def identify_song_from_url(url, on_status=None):
    """
    صدای ویدیو رو فقط یک‌بار دانلود می‌کنه (نه دوبار مثل قبل) و به ترتیب امتحان
    می‌کنه: ۱) اثرانگشت صدا با AudD (دقیق‌ترین، اگه AUDD_API_TOKEN ست باشه)
    ۲) رونویسی متن/شعر و جستجو از روی اون.
    on_status: تابع async اختیاری برای آپدیت پیام «در حال تشخیص...» بین مراحل.
    برمی‌گردونه (query, artist, title, source, confident) — source یکی از
    "audd"/"lyrics"/None و confident یعنی تشخیص رو با اطمینان بالا می‌دونیم
    (فقط وقتی AudD با score بالا تایید شده). وقتی confident=False هست،
    صدا‌زننده به‌جای دانلود مستقیم باید لیست کاندیدا نشون بده تا کاربر خودش
    انتخاب کنه (جلوگیری از دانلود آهنگ غلط مثل «Call My Name»).
    """
    if not url:
        return None, None, None, None, False
    print(f"[song-id] شروع تشخیص برای: {url}", flush=True)

    try:
        t0 = time.time() - 2
        raw_path = await asyncio.to_thread(dl.download, url, None, True, None, None)
    except Exception as e:
        print(f"[song-id] دانلود صدا ناموفق: {e}", flush=True)
        return None, None, None, None, False
    audio_path = _resolve_actual_audio_path(raw_path, since_ts=t0)
    if not audio_path:
        print(f"[song-id] دانلود صدا برای تشخیص چیزی نداد (raw_path={raw_path!r})", flush=True)
        return None, None, None, None
    print(f"[song-id] صدا دانلود شد: {audio_path}", flush=True)

    try:
        # مرحله ۱: اثرانگشت صدا (AudD) — فقط همین، مثل قبل که درست کار می‌کرد
        token = getattr(cfg, "AUDD_API_TOKEN", None)
        if token:
            result = await _audd_recognize(audio_path, token)
            if result:
                title = (result.get("title") or "").strip()
                artist = (result.get("artist") or "").strip()
                audd_score = result.get("score", 0.0)
                if title:
                    query = f"{artist} {title}".strip() if artist else title
                    low = (title + " " + artist).lower()
                    if any(w in low for w in ("remix", "mix", "edit", "bootleg", "funk", "sped up", "slowed")):
                        query = f"{query} official audio"
                    # score پایین فقط یک candidate تشخیصی است؛ هرگز به کاربر
                    # به‌عنوان آهنگ پیدا‌شده تحویل نمی‌دهیم. مخصوصاً score=0.
                    confident = audd_score >= 0.7
                    print(f"[song-id] AudD confident={confident} (score={audd_score:.2f})", flush=True)
                    if confident:
                        return query, artist, title, "audd", True
                    print("[song-id] AudD ضعیف بود → ادامه با رونویسی متن", flush=True)
        else:
            print("[song-id] AUDD_API_TOKEN ست نشده، رد شد", flush=True)

        # مرحله ۱.۵: شزام (فالبک رایگان، دیتابیس فارسی کامل‌تر از AudD).
        # فقط وقتی AudD قطعی نداد (score پایین / توکن نبود / نتیجهٔ متناقض)
        # اجرا میشه — و چون خروجی‌اش نام/خوانندهٔ تمیزه، آهنگ رو مستقیم و
        # درست دانلود می‌کنه (برخلاف STT فارسی که خروجی کثیف میده).
        shazam_res = await _shazamapi_recognize(audio_path)
        if shazam_res:
            title = (shazam_res.get("title") or "").strip()
            artist = (shazam_res.get("artist") or "").strip()
            if title:
                query = f"{artist} {title}".strip() if artist else title
                print(f"[song-id] Shazam confident → {artist} - {title}", flush=True)
                return query, artist, title, "shazam", True

        # مرحله ۱.۷: ACRCloud (سومین دیتابیس فینگرپرینت مستقل — رایگان تا سقف
        # ماهانه‌ی پلن Free — پوشش خیلی خوبی برای ریمیکس/بی‌کلام/موسیقی منطقه‌ای
        # داره). تابعش از قبل کامل نوشته شده بود ولی وصل نشده بود.
        acr_res = await _acr_recognize(audio_path)
        if acr_res:
            title = (acr_res.get("title") or "").strip()
            artist = (acr_res.get("artist") or "").strip()
            if title:
                query = f"{artist} {title}".strip() if artist else title
                print(f"[song-id] ACRCloud confident → {artist} - {title}", flush=True)
                return query, artist, title, "acrcloud", True

        # مرحله ۲: رونویسی متن/شعر (STT) و جستجو از روی اون.
        # چون STT (مخصوصاً فارسی) ممکنه خروجی کثیف بده، اینجا هرگز quiet
        # auto_pick=True نمی‌کنیم — confident=False برمی‌گردونیم تا لایه‌ی
        # بالادست (search_and_send_music_options) به‌جای دانلود کورکورانه،
        # لیست کاندیدا نشون بده و خود کاربر آهنگ درست رو تایید/انتخاب کنه.
        print("[song-id] AudD و Shazam و ACRCloud هر سه fail شدند → رونویسی متن/شعر", flush=True)
        if on_status:
            try:
                await on_status("📝 صدا رو نشناختم؛ دارم از روی شعر/متنش جستجو می‌کنم...")
            except Exception:
                pass
        transcript = await _transcribe_audio_for_lyrics(audio_path)
        phrase = _lyrics_phrase_for_search(transcript)
        if phrase:
            print(f"[song-id] عبارت جستجو از روی رونویسی: {phrase[:120]}", flush=True)
            return phrase, None, None, "lyrics", False

        print("[song-id] رونویسی هم چیزی نداد → از کاربر متن آهنگ می‌خوایم", flush=True)
        return None, None, None, None, False
    finally:
        try:
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception:
            pass

    return None, None, None, None, False


def build_spotify_search_link(query):
    """
    لینک جستجوی اسپاتیفای بدون نیاز به API/Premium/کلاینت‌آیدی.
    دقیق نیست (مستقیم به خود آهنگ نمی‌بره) ولی به صفحه‌ی نتایج جستجو می‌بره
    و خودت از همونجا آهنگ درست رو سیو می‌کنی.
    """
    if not query:
        return None
    import urllib.parse
    return f"https://open.spotify.com/search/{urllib.parse.quote(query)}"


async def send_spotify_link_if_available(chat_id, ctx, query):
    link = build_spotify_search_link(query)
    if not link:
        return
    await ctx.bot.send_message(chat_id, f"🟢 جستجوی این آهنگ تو اسپاتیفای:\n{link}")


def _clean_song_title(raw):
    """نویزِ روی عنوان یوتیوب رو تمیز می‌کنه مثلاً «Soghati | هایده - سوغاتی»
    می‌شه «هایده - سوغاتی». بعضی کانال‌ها یه برچسب قبل از «|» اضافه می‌کنن؛ اون
    بخش رو می‌اندازیم تا حدس «خواننده - آهنگ» و جستجوی متن آهنگ درست کار کنه."""
    if not raw:
        return raw
    s = str(raw).strip()
    if "|" in s:
        # بخش بعد از آخرین «|» معمولاً اسم واقعی آهنگه؛ برچسب قبلش رو دور بریز
        s = s.rsplit("|", 1)[-1].strip()
    # نرمال‌سازی فارسی: کشیدگی ـ و هٔ/ة خراب → ه (یه سری کانال عنوان رو خراب می‌ذارن)
    s = s.replace("\u0640", " ")        # ـ tatweel → فاصله (جداکننده)
    s = s.replace("\u0654", "")         # هٔ → ه (همزه بالای ه)
    s = s.replace("\u0629", "\u0647")   # ة → ه
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _guess_artist_title(clean, artist):
    """حدس «خواننده - آهنگ» از عنوان تمیز. جداکننده‌ها: " - "، – ، — ، و حتی -
    بدون فاصله (مثل «Hayedeh-Afsaneh Hasty»). اسم کانال رو هیچ‌وقت آرتیست نمی‌کنه."""
    for sep in (" - ", "–", "—", "-"):
        if sep in clean:
            parts = clean.split(sep, 1)
            a, t = parts[0].strip(), parts[1].strip()
            if a and t:
                return a, t  # آرتیستِ خودِ عنوان — نه اسم کانال
    return artist, clean


_ARTIST_ALIASES = {
    "viguen": "ویگن",
    "vigen": "ویگن",
    "viggen": "ویگن",
    "ویگن": "ویگن",
    "hayedeh": "هایده",
    "haideh": "هایده",
    "هایده": "هایده",
    "googoosh": "گوگوش",
    "gogoosh": "گوگوش",
    "گوگوش": "گوگوش",
    "ebi": "ابی",
    "ابی": "ابی",
    "dariush": "داریوش",
    "daryoush": "داریوش",
    "داریوش": "داریوش",
    "farhad": "فرهاد",
    "فرهاد": "فرهاد",
}


def _normalize_song_meta(artist, title, yt_title=None):
    """تمیزکاری artist/title برای تگ ID3 و جستجوی متن آهنگ.

    عنوان‌های یوتیوب ایرانی اغلب مخلوط لاتین+فارسی‌اند
    (مثل «Bekhatere Tou ویگن بخاطره تو»). بخش فارسی برای lyrics و تگ
    اولویت دارد؛ نام خواننده هم با alias نرمال می‌شود.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    yt_title = (yt_title or "").strip()

    # اگر title هنوز مخلوط است، بخش فارسی را جدا کن
    def _fa_part(s):
        words = re.findall(r"[\u0600-\u06FF]+", s or "")
        return " ".join(words).strip()

    def _latin_part(s):
        # کلمات لاتین بدون اسم خوانندهٔ تکراری
        words = re.findall(r"[A-Za-z][A-Za-z0-9']*", s or "")
        return " ".join(words).strip()

    fa_title = _fa_part(title)
    # گاهی خواننده هم داخل title فارسی آمده («ویگن بخاطره تو»)
    if fa_title:
        # اگر artist لاتین است و همان اسم داخل fa هست، artist را فارسی کن و از title حذف کن
        for lat, fa in _ARTIST_ALIASES.items():
            if fa and fa in fa_title.split():
                if not artist or artist.lower() in _ARTIST_ALIASES:
                    artist = fa
                # عنوان بدون نام خواننده
                fa_title = " ".join(w for w in fa_title.split() if w != fa).strip() or fa_title
                break
        if fa_title and (fa_title != title or re.search(r"[A-Za-z]", title or "")):
            title = fa_title

    # اگر title خالی ماند از yt_title فارسی بگیر
    if not title and yt_title:
        fa = _fa_part(yt_title)
        if fa:
            title = fa
        else:
            title = yt_title

    # نرمال‌سازی artist
    if artist:
        key = artist.lower().strip()
        if key in _ARTIST_ALIASES:
            artist = _ARTIST_ALIASES[key]
    elif yt_title:
        # حدس خواننده از روی عنوان یوتیوب
        low = yt_title.lower()
        for lat, fa in _ARTIST_ALIASES.items():
            if lat in low or fa in yt_title:
                artist = fa
                break

    return artist, title


async def make_song_download_job(chosen, artist=None, title=None):
    """
    جاب دانلود آهنگ + دکمه‌ی همیشگی «متن آهنگ».
    کپشن ویدیوی اینستا اینجا استفاده نمی‌شود.
    """
    chosen_url = chosen.get("url") or chosen.get("webpage_url") or chosen.get("id")
    if chosen_url and "http" not in str(chosen_url):
        chosen_url = f"https://www.youtube.com/watch?v={chosen_url}"
    t = (chosen.get("title") or title or "آهنگ").strip()
    uploader = (chosen.get("uploader") or chosen.get("channel") or artist or "").strip()
    lyrics_artist = (artist or "").strip()
    lyrics_title = (title or "").strip()
    t_clean = _clean_song_title(t)
    if not lyrics_title:
        lyrics_artist, lyrics_title = _guess_artist_title(t_clean, lyrics_artist)
        # بدون جداکننده، اسم کانال رو آرتیست نکن — عنوانِ تمیز خودش می‌مونه

    # عنوان‌های مخلوط لاتین+فارسی (مثل «Bekhatere Tou ویگن بخاطره تو») را
    # برای تگ و جستجوی متن تمیز کن: بخش فارسی اولویت دارد.
    lyrics_artist, lyrics_title = _normalize_song_meta(lyrics_artist, lyrics_title, t_clean)

    lyrics_text = await fetch_lyrics(lyrics_artist, lyrics_title, raw_title=t)
    # اگر با عنوان تمیز پیدا نشد، بخش فارسیِ خامِ عنوان یوتیوب را هم امتحان کن
    if not lyrics_text:
        fa_raw = " ".join(re.findall(r"[\u0600-\u06FF]+", t_clean or t or ""))
        if fa_raw and fa_raw != lyrics_title:
            lyrics_text = await fetch_lyrics(lyrics_artist, fa_raw, raw_title=t)
            if lyrics_text and not lyrics_title:
                lyrics_title = fa_raw
    lyrics_id = _cache_lyrics_ref(lyrics_artist, lyrics_title, lyrics_text, raw=t)
    extra_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 متن آهنگ", callback_data=f"lyrics:{lyrics_id}")]
    ])
    thumb = None
    try:
        thumb = chosen.get("thumbnail") or (chosen.get("thumbnails") or [{}])[-1].get("url")
    except Exception:
        pass
    display_title = lyrics_title or t_clean or t or "آهنگ"
    display_artist = lyrics_artist or ""
    return {
        "url": chosen_url,
        "fmt": None,
        "inf": chosen,
        "is_audio": True,
        "send_to_telegram": True,
        "ig_kind": "song",
        "extra_markup": extra_markup,
        "tag_artist": display_artist,
        "tag_title": display_title,
        "tag_cover_url": thumb,
        "caption": f"🎵 {display_artist + ' — ' if display_artist else ''}{display_title}".strip(),
    }


async def extract_video_frame(video_path, out_path, timestamp="00:00:01"):
    """یه فریم از ویدیو با ffmpeg می‌گیره و مسیرش رو برمی‌گردونه (یا None)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", timestamp, "-i", video_path, "-frames:v", "1", "-q:v", "2", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        return None
    except Exception as e:
        print(f"[frame] استخراج فریم ناموفق: {e}", flush=True)
        return None


async def upload_to_catbox(path):
    """آپلود یه عکس به catbox.moe (رایگان، بدون توکن) برای گرفتن یه لینک عمومی."""
    try:
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("reqtype", "fileupload")
        with open(path, "rb") as f:
            form.add_field("fileToUpload", f, filename=os.path.basename(path))
            async with aiohttp.ClientSession(trust_env=True) as sess:
                async with sess.post(
                    "https://catbox.moe/user/api.php",
                    data=form,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    timeout=aiohttp.ClientTimeout(total=45),
                ) as resp:
                    status = resp.status
                    text = (await resp.text()).strip()
        if text.startswith("http"):
            return text
        print(f"[catbox] آپلود ناموفق (status={status}): {text!r}", flush=True)
        return None
    except Exception as e:
        print(f"[catbox] خطا: {type(e).__name__}: {e!r}", flush=True)
        return None


async def _lyrics_to_song_guess(text):
    """
    از روی یه تکه شعر (حتی ناقص/جابه‌جا/با کلمات کمی فرق‌دار با متن اصلی)،
    با Gemini (همون سرویسی که برای فیلم‌یاب استفاده می‌شه) حدس می‌زنه واقعاً
    کدوم آهنگه — درک معنایی مدل زبانی خیلی مقاوم‌تر از جستجوی کلمه‌به‌کلمهٔ
    یوتیوبه برای متن ناقص/پارافریز.
    خروجی: (queries: list[str], confident: bool) — queries چند تا حدس
    «خواننده عنوان» برای جستجوی یوتیوب. اگه Gemini در دسترس نبود یا شکست
    خورد، لیست خالی برمی‌گرده تا caller خودش fallback بزنه.
    """
    text = (text or "").strip()
    if not (MOVIE_OK and text and "mv_gemini_generate" in globals()):
        print(
            f"[lyrics-guess] رد شد — MOVIE_OK={MOVIE_OK}, "
            f"has_gemini={'mv_gemini_generate' in globals()}, has_text={bool(text)}",
            flush=True,
        )
        return [], False
    prompt = (
        "این یه تکه شعر/ترانه‌ست که کاربر از حافظه (شاید ناقص، جابه‌جا یا با چند "
        "کلمه فرق با متن اصلی) تایپ کرده. باید دقیقاً بفهمی این آهنگِ *واقعیِ کدومه* "
        "و خواننده‌ش کیه.\n\n"
        "قوانین مهم (رعایت نکردنشون یعنی جواب غلط):\n"
        "۱. فقط بر اساس چیزی که مطمئنی جواب بده؛ بین خواننده‌های کلاسیک ایرانی "
        "(مثلاً ویگن، فرهاد مهراد، داریوش، ابی، هایده، گوگوش، عارف و امثالشون) "
        "به‌خاطر سبک/فضای مشابه اشتباه نگیر — هر کدوم ترانه‌های مشخص خودشونو دارن.\n"
        "۲. قبل از جواب دادن، تو ذهنت مرور کن این مصرع‌ها دقیقاً مال کدوم ترانه و "
        "کدوم خواننده‌ست؛ اگه مطمئن نیستی کدوم خواننده می‌خونتش، عنوان رو بدون "
        "خواننده (artist خالی) برگردون تا بعداً جستجوی عمومی بشه، نه یه خوانندهٔ حدسی.\n"
        "۳. اگه اصلاً این شعر رو نمی‌شناسی، آرایه‌ی guesses رو خالی بذار — حدس "
        "الکی نزن.\n\n"
        "فقط یک شیء JSON با این ساختار برگردون، بدون هیچ توضیح اضافه:\n"
        '{"guesses": [{"artist": "...", "title": "..."}, ...]}\n'
        "حداکثر ۳ حدس، از مطمئن‌ترین به کم‌مطمئن‌ترین.\n\n"
        f"متن کاربر:\n{text}"
    )
    try:
        raw = await asyncio.to_thread(mv_gemini_generate, prompt, [])
        parsed = mv_gemini_extract_json(raw) or {}
        guesses = parsed.get("guesses") or []
        queries = []
        for g in guesses:
            artist = (g.get("artist") or "").strip()
            title = (g.get("title") or "").strip()
            if title:
                q = f"{artist} {title}".strip()
                if q not in queries:
                    queries.append(q)
        print(f"[lyrics-guess] Gemini حدس زد: {queries}", flush=True)
        return queries, bool(queries)
    except Exception as e:
        print(f"[lyrics-guess] خطا: {type(e).__name__}: {e}", flush=True)
        return [], False


def _clean_lyrics_query(text):
    """پاک‌سازی علامت‌های نگارشی فارسی/عمومی که جستجوی یوتیوب رو نویزی می‌کنن."""
    text = re.sub(r"[؛،٫!؟?.…«»\"'‌]+", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()



async def _job_from_music_pick(chat_id, ctx, chosen):
    """از انتخاب کاربر در لیست نتایج موزیک، جاب کامل با تگ/کاور/متن بساز.

    برخلاف مسیر قبلی که فقط make_song_download_job(chosen) می‌زد و پروفایل
    (عنوان/خواننده/کاور) خالی می‌ماند، اینجا:
    ۱) حدس ذخیره‌شده از متن کاربر (سشن)
    ۲) parse عنوان یوتیوب
    ۳) نرمال‌سازی فارسی
    را ترکیب می‌کنیم و کاور را هم مثل auto_pick می‌فرستیم.
    """
    t = (chosen.get("title") or "").strip()
    t_clean = _clean_song_title(t)
    artist, title = _guess_artist_title(t_clean, "")
    sess = SESS.get(chat_id) or {}
    if sess.get("_song_guess_artist"):
        artist = sess["_song_guess_artist"]
    if sess.get("_song_guess_title"):
        title = sess["_song_guess_title"]
    artist, title = _normalize_song_meta(artist, title, t_clean)

    thumb = None
    try:
        thumb = chosen.get("thumbnail") or (chosen.get("thumbnails") or [{}])[-1].get("url")
    except Exception:
        pass

    # کاور جدا — مثل مسیر تشخیص خودکار با صدا
    if thumb:
        try:
            cap = f"🎵 {(artist + ' — ') if artist else ''}{title or t_clean or t}"
            await ctx.bot.send_photo(chat_id, photo=thumb, caption=cap)
        except Exception as e:
            print(f"[music-pick] ارسال کاور ناموفق: {e}", flush=True)

    job = await make_song_download_job(chosen, artist=artist or None, title=title or None)
    if thumb:
        job["tag_cover_url"] = thumb
    return job

async def search_song_from_lyrics_text(chat_id, ctx, text, cb_prefix, result_key):
    """
    جستجوی قوی آهنگ از روی یه تکه شعر که کاربر (احتمالاً ناقص/تقریبی) فرستاده.
    ترتیب تلاش:
    ۱) حدس معنایی با مدل زبانی (Gemini) — به‌جای جستجوی کلمه‌به‌کلمه، خودِ آهنگ
       رو از روی معنای متن حدس می‌زنه؛ برای شعر جابه‌جا/ناقص خیلی مقاوم‌تره.
       اگه چند حدس داد، نتایج یوتیوب همه‌شون رو با هم جمع می‌کنه.
    ۲) اگه مدل در دسترس نبود/هیچی نداد، جستجوی مستقیمِ خودِ متن (پاک‌سازی‌شده
       از علامت‌های نگارشی + «آهنگ» به انتها اضافه می‌شه تا نتیجه‌ها موزیک باشن نه شعرخونی).
    در هر دو حالت یه لیست انتخابی نشون داده می‌شه (نه دانلود خودکار) چون این یه حدسه.
    حدس artist/title برای تگ ID3 و متن آهنگ در سشن نگه داشته می‌شود.
    """
    cleaned = _clean_lyrics_query(text)
    queries, confident = await _lyrics_to_song_guess(text)
    if not queries:
        # مدل در دسترس نبود یا حدسی نداشت — مستقیم خود متن رو با بایاس «آهنگ» بزن.
        queries = [f"{cleaned} آهنگ"]

    # بهترین حدس artist/title را برای وقتی کاربر از لیست انتخاب می‌کند ذخیره کن
    # تا تگ و متن آهنگ مثل مسیر تشخیص صوتی درست پر شوند.
    sess = SESS.setdefault(chat_id, {})
    sess.pop("_song_guess_artist", None)
    sess.pop("_song_guess_title", None)
    first = (queries[0] or "").strip()
    if first:
        # queries معمولاً «خواننده عنوان» هستند
        parts = first.split(None, 1)
        if len(parts) == 2:
            a, t = parts[0].strip(), parts[1].strip()
            # اگر بخش اول اسم شناخته‌شدهٔ خواننده است
            if a.lower() in _ARTIST_ALIASES or a in _ARTIST_ALIASES.values() or len(a) <= 20:
                sess["_song_guess_artist"] = _ARTIST_ALIASES.get(a.lower(), a)
                sess["_song_guess_title"] = t
            else:
                sess["_song_guess_title"] = first
        else:
            sess["_song_guess_title"] = first
        print(
            f"[lyrics-guess] سشن: artist={sess.get('_song_guess_artist')!r} "
            f"title={sess.get('_song_guess_title')!r}",
            flush=True,
        )

    await search_and_send_music_options(
        chat_id, ctx, queries[0], cb_prefix=cb_prefix, result_key=result_key,
        extra_queries=queries[1:], count=8,
        artist=sess.get("_song_guess_artist"),
        title=sess.get("_song_guess_title"),
    )


async def search_and_send_music_options(chat_id, ctx, search_query, cb_prefix, result_key, update=None, auto_pick=False, artist=None, title=None, extra_queries=None, count=5):
    """
    جستجوی یوتیوب برای یک عبارت مشخص.
    auto_pick=True: چون آهنگ خودمون (از متادیتا یا AudD) تشخیص داده شده، به‌جای نمایش
    لیست، مستقیم مرتبط‌ترین نتیجه (اولین نتیجه‌ی جستجو) رو دانلود می‌کنه.
    auto_pick=False: لیست ۵تایی نشون می‌ده تا خودت انتخاب کنی (برای وقتی خودت دستی
    اسم آهنگ رو تایپ کردی و معلوم نیست دقیقاً منظورت کدومه).
    artist/title: اگه از قبل (مثلاً از AudD) اسم دقیق خواننده/آهنگ رو داریم، برای
    گرفتن متن آهنگ و تگ‌گذاری ID3 استفاده می‌شه (دقیق‌تر از حدس‌زدن رو عنوان یوتیوب).
    extra_queries: حدس‌های جایگزین (مثلاً از تشخیص شعر با LLM) که نتایجشون هم به
    همین لیست اضافه می‌شه تا شانس پیدا کردن آهنگ درست بیشتر بشه.
    count: تعداد نتیجه‌ی هر جستجو (ytsearchN).
    """
    msg = await ctx.bot.send_message(chat_id, f"🔍 جستجوی آهنگ: {search_query}")

    # نکته‌ی مهم: برای «جستجو» فقط عنوان/مدت‌زمان/کاور لازمه، نه فرمت‌های دانلود.
    # قبلاً extract_info بدون extract_flat هر کدوم از ۵ نتیجه رو کامل پردازش
    # می‌کرد (یعنی فرمت‌هاشم می‌رفت چک می‌کرد) — اگه فقط یکی از اون ۵ تا (مثلاً
    # پخش زنده/محدودیت سنی/فرمت خاص) مشکل داشت، کل جستجو با خطای «Requested
    # format is not available» شکست می‌خورد، حتی اگه ۴ تای دیگه سالم بودن.
    # با extract_flat فقط اطلاعات صفحه‌ی نتایج جستجو خونده می‌شه، بدون باز کردن
    # تک‌تک ویدیوها — نه دیگه اون خطا، نه نیاز به کوکی برای خود جستجو.
    async def _try_search(q, use_cookies):
        opts = dl.ydl_base(use_cookies=use_cookies)
        opts["default_search"] = f"ytsearch{count}"
        opts["noplaylist"] = True
        opts["extract_flat"] = "in_playlist"
        opts["ignoreerrors"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = await asyncio.to_thread(ydl.extract_info, f"ytsearch{count}:{q}", False)
        return result

    async def _search_one(q):
        found = []
        err = None
        for use_cookies in (False, True):
            try:
                result = await _try_search(q, use_cookies)
                found = [e for e in (result.get("entries") if result else []) if e]
                if found:
                    err = None
                    break
            except Exception as e:
                err = e
                print(f"[music] جستجوی یوتیوب ناموفق (q={q!r}, cookies={use_cookies}): {e}", flush=True)
                continue
        return found, err

    entries = []
    last_err = None
    seen_ids = set()
    for q in [search_query, *(extra_queries or [])]:
        found, err = await _search_one(q)
        last_err = last_err or err
        for e in found:
            eid = e.get("id") or e.get("url")
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            entries.append(e)
        if len(entries) >= count and not extra_queries:
            break

    if last_err and not entries:
        await msg.edit_text(f"❌ خطا در جستجو: {last_err}")
        return

    if not entries:
        await msg.edit_text("❌ آهنگی پیدا نشد. اسم دقیق‌تری امتحان کن.")
        return

    # فیلم/سریال/کلیپ‌های دوبله معمولاً خیلی طولانی‌ترن یا کلمات مشخصی تو عنوانشونه —
    # اینا رو کنار می‌ذاریم تا جستجوی گنگ (مثل عبارت نصفه‌نیمهٔ رونویسی‌شده) به‌جای
    # آهنگ، یه فیلم/سریال دوبله برنگردونه
    NON_MUSIC_HINTS = ("engdub", "دوبله", "زیرنویس فارسی", "قسمت", "فصل", "تریلر", "trailer", "episode", "فیلم کامل")

    def _looks_like_music(e):
        dur = e.get("duration")
        title_low = (e.get("title") or "").lower()
        if dur and (dur < 25 or dur > 8 * 60):
            return False
        if any(h in title_low for h in NON_MUSIC_HINTS):
            return False
        return True

    music_entries = [e for e in entries if _looks_like_music(e)]
    if music_entries:
        entries = music_entries

    try:
        if auto_pick and update is not None:
            # اولویت‌بندی: اگه artist/title تشخیص‌داده‌شده داریم، فقط نتایجی که
            # عنوانشون با اون همخوانی دارن رو قبول کن. وگرنه اولین نتیجه رو
            # برمی‌داره که باعث می‌شه آهنگ کاملاً غلط دانلود شه (مثل «Call My
            # Name» به‌جای چیزی که AudD تشخیص داده بود).
            chosen = None
            if artist or title:
                q = f"{artist} {title}".strip().lower()
                target_words = [w for w in re.split(r"\s+", q) if len(w) > 2]
                for e in entries:
                    t_low = (e.get("title") or "").lower()
                    if any(w in t_low for w in target_words):
                        chosen = e
                        break
            if not chosen:
                chosen = entries[0]
            t = chosen.get("title") or "بدون عنوان"
            uploader = chosen.get("channel") or chosen.get("uploader") or "ناشناس"
            duration = chosen.get("duration")
            dur_str = ""
            if duration:
                m, s = divmod(int(duration), 60)
                dur_str = f"\n⏱ {m}:{s:02d}"
            caption = f"🎵 {t}\n👤 {uploader}{dur_str}"
            chosen_url = chosen.get("url") or chosen.get("webpage_url") or chosen.get("id")
            if chosen_url and "http" not in str(chosen_url):
                chosen_url = f"https://www.youtube.com/watch?v={chosen_url}"
            if not chosen_url:
                await msg.edit_text("❌ لینک این نتیجه پیدا نشد. یه اسم دیگه امتحان کن.")
                return
            # کاور رو بفرست
            thumb_url = chosen.get("thumbnail")
            if not thumb_url:
                try:
                    thumb_url = (chosen.get("thumbnails") or [{}])[-1].get("url")
                except Exception:
                    thumb_url = None
            try:
                if thumb_url:
                    await msg.edit_text(f"🎵 پیدا شد: {t}\n🖼 ارسال کاور...")
                    await ctx.bot.send_photo(chat_id, photo=thumb_url, caption=caption)
                else:
                    await msg.edit_text(f"🎵 {t}")
            except Exception as e:
                print(f"[music] ارسال کاور ناموفق: {e}", flush=True)

            # اسم خواننده/آهنگ برای متن آهنگ و تگ ID3: اول از AudD (دقیق‌تر)، وگرنه
            # حدس از رو عنوان یوتیوب (الگوی معمول "خواننده - اسم آهنگ")
            lyrics_artist, lyrics_title = artist, title
            if not lyrics_title:
                t_clean = _clean_song_title(t)
                lyrics_artist, lyrics_title = _guess_artist_title(t_clean, lyrics_artist)

            # همیشه دکمه‌ی «متن آهنگ» زیر فایل — متن از قبل یا موقع زدن دکمه
            lyrics_text = None
            if lyrics_title:
                lyrics_text = await fetch_lyrics(lyrics_artist, lyrics_title, raw_title=t)
            lyrics_id = _cache_lyrics_ref(lyrics_artist, lyrics_title, lyrics_text, raw=t)
            extra_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 متن آهنگ", callback_data=f"lyrics:{lyrics_id}")]
            ])

            # دانلود و ارسال فایل صوتی + دکمه‌ی متن آهنگ
            if chosen_url:
                await msg.edit_text(f"🎵 {t}\n⬇️ در حال دانلود...")
                job = await make_song_download_job(
                    chosen if isinstance(chosen, dict) else {"url": chosen_url, "title": t},
                    artist=lyrics_artist,
                    title=lyrics_title,
                )
                if thumb_url:
                    job["tag_cover_url"] = thumb_url
                sess = SESS.get(chat_id) or {}
                if sess.get("mode") == "batch":
                    # چند لینک: بره تو صف و لینک بعدی
                    sess.setdefault("jobs", []).append(job)
                    sess["idx"] = sess.get("idx", 0) + 1
                    await process_next_in_batch(update, ctx)
                else:
                    await run_single_job(update, ctx, job)
                    await send_spotify_link_if_available(chat_id, ctx, search_query)
            return

        kb = []
        for i, e in enumerate(entries[:5]):
            t = e.get("title") or "بدون عنوان"
            dur = e.get("duration")
            dur_str = ""
            if dur:
                m_s, sec = divmod(int(dur), 60)
                dur_str = f" ({m_s}:{sec:02d})"
            kb.append([InlineKeyboardButton(f"🎵 {t}{dur_str}", callback_data=f"{cb_prefix}:{i}")])
        kb.append([InlineKeyboardButton("❌ لغو", callback_data=f"{cb_prefix}:cancel")])
        SESS.setdefault(chat_id, {})[result_key] = entries[:5]
        await msg.edit_text(f"🎵 نتایج جستجو برای:\n{search_query}\n\nیکی رو انتخاب کن:",
                            reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        await msg.edit_text(f"❌ خطا بعد از پیدا کردن نتیجه: {e}")


async def deliver_share(update, ctx, share_id, password=None):
    """کسی لینک t.me/bot?start=s_xxx رو باز کرده → فایل رو براش بفرست (با رمز اختیاری)."""
    chat_id = update.effective_chat.id
    rec, err_code, err = get_valid_share(share_id, password=password)
    if err_code == "need_password":
        SESS[chat_id] = {"mode": "share_pw", "share_id": share_id}
        await update.message.reply_text(
            f"🔐 این لینک رمز داره.\n"
            f"📦 {rec.get('title') or 'فایل'}\n\n"
            f"رمز رو همین‌جا بفرست:"
        )
        return
    if err_code == "bad_password":
        SESS[chat_id] = {"mode": "share_pw", "share_id": share_id}
        await update.message.reply_text("❌ رمز اشتباهه. دوباره بفرست:")
        return
    if err:
        await update.message.reply_text(err)
        return
    path = rec["path"]
    title = rec.get("title") or "فایل"
    kind = rec.get("kind") or "file"
    is_audio_file = kind == "song" or path.lower().endswith((".mp3", ".m4a", ".opus", ".ogg"))
    try:
        await update.message.reply_text(f"⏳ در حال ارسال: {title}")
        sent_msg = None
        cached_file_id = rec.get("file_id")

        if cached_file_id:
            try:
                if is_audio_file:
                    sent_msg = await ctx.bot.send_audio(chat_id, audio=cached_file_id, caption=f"🎵 {title}")
                else:
                    sent_msg = await ctx.bot.send_document(chat_id, document=cached_file_id, caption=f"🎬 {title}")
            except Exception as e:
                print(f"[share] ارسال با file_id ناموفق، آپلود از نو: {e}", flush=True)
                sent_msg = None

        if sent_msg is None:
            # ممکن است این لینک قبل از ساخته شدن share ایجاد شده باشد؛
            # file_id را از کاتالوگ هم امتحان کن.
            cached_from_catalog = get_ig_catalog_file_id(path)
            if cached_from_catalog:
                try:
                    if is_audio_file:
                        sent_msg = await ctx.bot.send_audio(chat_id, audio=cached_from_catalog, caption=f"🎵 {title}")
                    else:
                        sent_msg = await ctx.bot.send_document(chat_id, document=cached_from_catalog, caption=f"🎬 {title}")
                    print(f"[share] ارسال مستقیم با file_id کاتالوگ؛ بدون آپلود مجدد", flush=True)
                except Exception as e:
                    print(f"[share] file_id کاتالوگ ناموفق: {e}", flush=True)
                    sent_msg = None

        if sent_msg is None:
            with open(path, "rb") as f:
                if is_audio_file:
                    try:
                        sent_msg = await ctx.bot.send_audio(
                            chat_id, audio=f, caption=f"🎵 {title}",
                            read_timeout=3600, write_timeout=3600,
                        )
                    except Exception:
                        f.seek(0)
                        sent_msg = await ctx.bot.send_document(
                            chat_id, document=f, caption=f"🎬 {title}",
                            read_timeout=3600, write_timeout=3600,
                        )
                else:
                    sent_msg = await ctx.bot.send_document(
                        chat_id, document=f, caption=f"🎬 {title}",
                        read_timeout=3600, write_timeout=3600,
                    )
            new_file_id = None
            if sent_msg.audio:
                new_file_id = sent_msg.audio.file_id
            elif sent_msg.document:
                new_file_id = sent_msg.document.file_id
            if new_file_id:
                shares = load_shares()
                if share_id in shares:
                    shares[share_id]["file_id"] = new_file_id
                    save_shares(shares)

        consume_share(share_id)
        left = int(rec.get("max_uses", 1)) - int(rec.get("used", 0)) - 1
        await update.message.reply_text(
            f"✅ ارسال شد.\n"
            f"باقی‌مانده دانلود این لینک: {max(0, left)}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ ارسال ناموفق: {e}")


async def start_cmd(update, ctx):
    args = ctx.args or []
    is_owner = update.effective_user and update.effective_user.id in cfg.OWNER_ID
    # لینک اشتراک: /start s_xxxxxxxxxx — برای همه بازه
    if args:
        payload = (args[0] or "").strip()
        if payload.startswith("s_"):
            await deliver_share(update, ctx, payload)
            return
        # payload ناشناخته
        if not is_owner:
            await update.message.reply_text("سیکتیر")
            return
        if payload:
            await update.message.reply_text(
                "❌ این لینک معتبر نیست یا منقضی شده.\n"
                f"شناسه: `{payload}`",
            )
            return
    # /start ساده (منو اصلی) — فقط مالک
    if not is_owner:
        await update.message.reply_text("سیکتیر")
        return

    text = (
        "👋🏻 سلام\n\n"
        "📥 من می‌تونم ویدیو و عکس دانلود کنم از:\n\n"
        "🌐 YouTube\n"
        "🌐 Instagram (ریلز / پست / استوری / هایلایت)\n"
        "🌐 TikTok\n"
        "🌐 Pinterest\n"
        "🌐 VK\n"
        "🌐 Facebook\n"
        "🌐 Twitter / X\n"
        "🌐 Likee\n"
        "🌐 Snapchat\n"
        "🎵 Music\n\n"
        "• لینک بفرست برای دانلود 📤\n"
        "• ویس بفرست تا آهنگ رو تشخیص بدم 🎤\n\n"
        "از دکمه‌های پایین صفحه استفاده کن:\n"
        "🔗 ساخت لینک — اشتراک موقت ریلز/آهنگ اینستا\n"
        "📜 تاریخچه — دانلودهای قبلی\n"
        "📊 آمار — گزارش هفتگی دانلودها\n"
        "❌ لغو — توقف عملیات جاری"
    )
    await update.message.reply_text(text, reply_markup=MAIN_REPLY_KB)


async def cancel_cmd(update, ctx):
    SESS.pop(update.effective_chat.id, None)
    await update.message.reply_text("❌ لغو شد.")


async def queue_cmd(update, ctx):
    q = dl.load_queue() if hasattr(dl, "load_queue") else []
    if q:
        await update.message.reply_text("📋 صف ذخیره‌شده:\n" + "\n".join(q))
    else:
        await update.message.reply_text("صفت خالیه.")


async def stats_cmd(update, ctx):
    chat_id = update.effective_chat.id
    report = build_stats_report(chat_id, days=7)
    await update.message.reply_text(report, parse_mode="Markdown")


async def history_cmd(update, ctx):
    hist = load_history()
    if not hist:
        await update.message.reply_text("📭 تاریخچه خالیه.")
        return
    lines = []
    kb = []
    for i, h in enumerate(hist[:15]):
        title = (h.get("title") or "بدون عنوان")[:40]
        sz = h.get("size_mb")
        sz_str = f" · {sz:.1f}MB" if sz else ""
        kind = "🎵" if h.get("is_audio") else "🎬"
        lines.append(f"{i+1}. {kind} {title}{sz_str}")
        kb.append([InlineKeyboardButton(
            f"🔄 {i+1}. {title[:30]}",
            callback_data=f"hist:{i}",
        )])
    await update.message.reply_text(
        "📜 آخرین دانلودها:\n" + "\n".join(lines) + "\n\nبرای دانلود دوباره بزن:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def library_search_cmd(update, ctx, query):
    """جستجو در کتابخانه / تاریخچه / آهنگ‌های پیدا‌شده."""
    q = (query or "").strip().lower()
    if not q:
        await update.message.reply_text(
            "🔍 جستجو در کتابخانه:\n"
            "بنویس: `جستجو اسم آهنگ` یا `🔍 اسم`\n"
            "یا تاریخ مثل `2026-08`",
            parse_mode="Markdown",
        )
        return
    hits = []
    for e in load_history():
        t = (e.get("title") or "") + " " + (e.get("url") or "")
        ts = e.get("ts") or 0
        date_s = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
        if q in t.lower() or q in date_s:
            hits.append(("hist", e.get("title"), e.get("path"), date_s))
    for e in load_ig_catalog():
        t = (e.get("title") or "") + " " + (e.get("kind") or "")
        if q in t.lower():
            hits.append(("ig", e.get("title"), e.get("path"), e.get("kind")))
    for e in load_found_songs():
        t = f"{e.get('artist','')} {e.get('title','')}"
        if q in t.lower():
            hits.append(("song", t, e.get("path"), "آهنگ"))
    if not hits:
        await update.message.reply_text("چیزی پیدا نشد.")
        return
    lines = [f"🔍 نتایج برای «{query}»:\n"]
    kb = []
    SESS.setdefault(update.effective_chat.id, {})["lib_search"] = []
    for i, (src, title, path, extra) in enumerate(hits[:15]):
        lines.append(f"{i+1}. {(title or 'بدون عنوان')[:40]} ({extra})")
        if path and os.path.exists(path):
            SESS[update.effective_chat.id]["lib_search"].append(path)
            kb.append([InlineKeyboardButton(
                f"📤 {i+1}. {(title or 'فایل')[:30]}", callback_data=f"libsrch:{i}"
            )])
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb) if kb else None,
    )


async def library_cmd(update, ctx):
    """کتابخانه: تاریخچه + کاتالوگ با فایل موجود روی گوشی."""
    chat_id = update.effective_chat.id
    items = []
    seen = set()
    for e in load_history():
        p = e.get("path")
        if not p or not os.path.exists(p) or p in seen:
            continue
        seen.add(p)
        items.append({
            "path": p,
            "title": e.get("title") or os.path.basename(p),
            "kind": "audio" if e.get("is_audio") else "video",
            "ts": e.get("ts") or "",
        })
    for e in load_ig_catalog():
        p = e.get("path")
        if not p or not os.path.exists(p) or p in seen:
            continue
        seen.add(p)
        items.append({
            "path": p,
            "title": e.get("title") or os.path.basename(p),
            "kind": e.get("kind") or "file",
            "ts": e.get("saved_at") or "",
        })
    if not items:
        await update.message.reply_text("📚 کتابخانه خالیه — هنوز فایل ذخیره‌شده‌ای نیست.")
        return
    SESS.setdefault(chat_id, {})["lib_items"] = items[:40]
    kb = []
    lines = ["📚 *کتابخانه* — بزن تا بفرستم:\n"]
    for i, it in enumerate(items[:25]):
        icon = "🎵" if it["kind"] in ("audio", "song") else "🎬"
        lines.append(f"{i+1}. {icon} {it['title'][:40]}")
        kb.append([InlineKeyboardButton(f"{icon} {it['title'][:35]}", callback_data=f"lib:send:{i}")])
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


async def _ask_share_expiry(update, ctx, chat_id):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("۱ ساعت", callback_data="shareexp:1"),
            InlineKeyboardButton("۶ ساعت", callback_data="shareexp:6"),
        ],
        [
            InlineKeyboardButton("۱۲ ساعت", callback_data="shareexp:12"),
            InlineKeyboardButton("۱ روز", callback_data="shareexp:24"),
        ],
        [
            InlineKeyboardButton("۳ روز", callback_data="shareexp:72"),
            InlineKeyboardButton("۷ روز", callback_data="shareexp:168"),
        ],
        [InlineKeyboardButton("✍️ دلخواه (ساعت)", callback_data="shareexp:custom")],
        [InlineKeyboardButton("❌ لغو", callback_data="sharepick:cancel")],
    ])
    await ctx.bot.send_message(chat_id, "⏱ لینک بعد از چقدر منقضی بشه؟", reply_markup=kb)


async def _finalize_share_create(update, ctx, chat_id, expire_hours, password=None):
    s = SESS.get(chat_id) or {}
    item = s.pop("share_item", None)
    max_uses = s.pop("share_max_uses", 1)
    password = password if password is not None else s.pop("share_password", None)
    s.pop("awaiting_share_exp", None)
    s.pop("awaiting_share_uses", None)
    s.pop("awaiting_share_pw", None)
    if not item:
        await ctx.bot.send_message(chat_id, "❌ چیزی انتخاب نشده.")
        return
    # فقط ریلز و آهنگ
    kind = item.get("kind") or "reel"
    if kind not in ("song", "reel"):
        kind = "reel"
    link, err = create_share(
        item.get("path"),
        item.get("title"),
        kind,
        max_uses,
        expire_hours,
        password=password,
        file_id=item.get("file_id") or get_ig_catalog_file_id(item.get("path")),
    )
    if err:
        await ctx.bot.send_message(chat_id, f"❌ {err}")
        return
    kind_txt = "🎵 آهنگ" if kind == "song" else "🎬 ریلز"
    exp_txt = f"{expire_hours:g} ساعت" if expire_hours < 24 else f"{expire_hours/24:g} روز"
    # HTML و نه Markdown: تو Markdown آندرلاین‌های یوزرنیم ربات و آیدی لینک (s_xxx)
    # به‌عنوان ایتالیک خورده می‌شن و لینک خراب تحویل می‌ره
    import html as _h
    ttl = _h.escape((item.get("title") or "")[:60])
    lnk = _h.escape(link)
    if password:
        body = (
            f"✅ لینک موقت ساخته شد\n\n"
            f"{kind_txt}: {ttl}\n"
            f"👥 سقف دانلود: {max_uses} نفر\n"
            f"⏱ انقضا: {exp_txt}\n"
            f"🔐 رمز: <code>{_h.escape(str(password))}</code>\n\n"
            f"🔗 {lnk}\n\n"
            f"گیرنده باید رمز رو بزنه تا فایل بیاد."
        )
    else:
        body = (
            f"✅ لینک موقت ساخته شد\n\n"
            f"{kind_txt}: {ttl}\n"
            f"👥 سقف دانلود: {max_uses} نفر\n"
            f"⏱ انقضا: {exp_txt}\n"
            f"🔓 بدون رمز\n\n"
            f"🔗 {lnk}\n\n"
            f"هرکی این لینک رو باز کنه، فایل براش ارسال می‌شه."
        )
    await ctx.bot.send_message(chat_id, body, reply_markup=MAIN_REPLY_KB,
                               parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def show_share_catalog(update, ctx):
    """لیست آهنگ‌ها و ریلزهای اینستا برای ساخت لینک."""
    cat = load_ig_catalog()
    # فقط فایل‌های موجود
    items = [c for c in cat if c.get("path") and os.path.exists(c["path"])]
    if not items:
        await update.message.reply_text(
            "📭 هنوز ریلز یا آهنگی از اینستا ذخیره نشده.\n"
            "اول از اینستا دانلود/استخراج کن، بعد دوباره بزن.",
            reply_markup=MAIN_REPLY_KB,
        )
        return
    SESS.setdefault(update.effective_chat.id, {})["_share_items"] = items[:30]
    kb = []
    for i, it in enumerate(items[:30]):
        kind = "🎵" if it.get("kind") == "song" else "🎬"
        title = (it.get("title") or "بدون عنوان")[:28]
        try:
            ts = _tehran_str(datetime.fromisoformat(it["saved_at"])) if it.get("saved_at") else "?"
        except Exception:
            ts = "?"
        kb.append([InlineKeyboardButton(
            f"{kind} {title} · {ts}",
            callback_data=f"sharepick:{i}",
        )])
    kb.append([InlineKeyboardButton("❌ بستن", callback_data="sharepick:cancel")])
    await update.message.reply_text(
        "🔗 کدوم فایل رو می‌خوای لینک موقت بسازی؟\n"
        "(ساعت به وقت ایران 🇮🇷)",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def handle_link(update, ctx):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    print(f"[link] got: {text[:80]}", flush=True)

    s0 = SESS.get(chat_id)

    # دکمه کیبورد پایین
    if text in ("🔗 ساخت لینک", "ساخت لینک"):
        await show_share_catalog(update, ctx)
        return
    if text == "📜 تاریخچه":
        await history_cmd(update, ctx)
        return
    if text == "📊 آمار":
        await stats_cmd(update, ctx)
        return
    if text in ("📚 کتابخانه", "کتابخانه"):
        await library_cmd(update, ctx)
        return
    if text.startswith("🔍") or text.startswith("/search ") or text.startswith("جستجو "):
        q = text
        for pref in ("🔍", "/search ", "جستجو "):
            if q.startswith(pref):
                q = q[len(pref):].strip()
        await library_search_cmd(update, ctx, q)
        return
    if text == "❌ لغو":
        await cancel_cmd(update, ctx)
        return

    # سوال دلخواه درباره‌ی ویدیو (بعد از زدن «سوال دلخواه بپرس» زیر خلاصه‌ی AI)
    if s0 and s0.get("awaiting_ai_question"):
        s0.pop("awaiting_ai_question", None)
        ctxinfo = s0.get("_ai_ctx") or {}
        if not ctxinfo.get("transcript"):
            await update.message.reply_text("رونوشت این ویدیو دیگه در دسترس نیست؛ دوباره روی «خلاصه با هوش مصنوعی» بزن.")
            return
        wait = await update.message.reply_text("🤖 در حال فکر کردن...")
        answer, err = await ai_answer_question(text, ctxinfo.get("transcript"), ctxinfo.get("title"))
        if err or not answer:
            await wait.edit_text(f"❌ جواب داده نشد: {err or 'خطا'}")
        else:
            await wait.edit_text(f"❓ {text}\n\n🤖 {answer}")
        return

    # رمز لینک اشتراک
    if s0 and s0.get("mode") == "share_pw":
        sid = s0.get("share_id")
        SESS.pop(chat_id, None)
        await deliver_share(update, ctx, sid, password=text)
        return

    # رمز هنگام ساخت لینک
    if s0 and s0.get("awaiting_share_pw"):
        s0.pop("awaiting_share_pw", None)
        if text.strip() in ("نه", "no", "-", "بدون", "بدون رمز"):
            s0["share_password"] = None
        else:
            s0["share_password"] = text.strip()
        exp = s0.pop("pending_share_exp", 24)
        await _finalize_share_create(update, ctx, chat_id, exp, password=s0.get("share_password"))
        return

    # منتظر تعداد دانلود دلخواه برای لینک
    if s0 and s0.get("awaiting_share_uses"):
        s0.pop("awaiting_share_uses", None)
        try:
            n = int(text.strip())
            if n < 1 or n > 100:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("عدد بین ۱ تا ۱۰۰ بفرست.")
            s0["awaiting_share_uses"] = True
            return
        s0["share_max_uses"] = n
        await _ask_share_expiry(update, ctx, chat_id)
        return

    # منتظر ساعت انقضای دلخواه
    if s0 and s0.get("awaiting_share_exp"):
        s0.pop("awaiting_share_exp", None)
        try:
            h = float(text.strip().replace(",", "."))
            if h <= 0 or h > 24 * 30:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("تعداد ساعت رو عدد بفرست (مثلاً 48 برای ۲ روز).")
            s0["awaiting_share_exp"] = True
            return
        s0["pending_share_exp"] = h
        s0["awaiting_share_pw"] = True
        await update.message.reply_text(
            "🔐 رمز برای لینک می‌خوای؟\nیه رمز بفرست، یا بنویس «نه» برای بدون رمز."
        )
        return

    # منتظر بازه زمانی کلیپ
    if s0 and s0.get("awaiting_clip"):
        pending = s0.pop("awaiting_clip")
        low = text.strip().lower()
        if low in ("no", "n", "خیر", "نه", "کل", "full", "-"):
            # بعد از بازه → منوی زیرنویس
            if len(pending) == 1 and not pending[0].get("is_audio"):
                s0["_pending_job"] = pending[0]
                await update.message.reply_text(
                    "💬 زیرنویس چطور باشه؟",
                    reply_markup=build_subs_kb(pending[0].get("inf") or {}),
                )
            else:
                for job in pending:
                    await finalize_job(update, ctx, job)
            return
        rng = parse_clip_range(text)
        if not rng:
            s0["awaiting_clip"] = pending
            await update.message.reply_text(
                "❌ فرمت درست نیست.\nمثال: `5:00-12:30` یا `90-180`\n"
                "یا دکمه «کل ویدیو» رو بزن.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 کل ویدیو", callback_data="clip:full")],
                ]),
            )
            return
        for job in pending:
            job["section"] = rng
        if len(pending) == 1 and not pending[0].get("is_audio"):
            s0["_pending_job"] = pending[0]
            await update.message.reply_text(
                "💬 زیرنویس چطور باشه؟",
                reply_markup=build_subs_kb(pending[0].get("inf") or {}),
            )
        else:
            for job in pending:
                await finalize_job(update, ctx, job)
        return

    # اگه منتظر بودیم کاربر خودش اسم آهنگ/تکه‌شعر رو دستی بفرسته
    if s0 and s0.get("awaiting_song_query") and not URL_RE.match(text):
        cb_prefix = s0.pop("awaiting_song_query")
        result_key = "_ig_music_results" if cb_prefix == "igms" else "_music_results"
        await search_song_from_lyrics_text(chat_id, ctx, text, cb_prefix=cb_prefix, result_key=result_key)
        return

    # چندتا لینک — فاصله یا خط جدید
    raw_parts = re.split(r"[\s\n\r]+", text)
    links = []
    seen = set()
    for t in raw_parts:
        t = t.strip().rstrip(".,;،؛")
        if not t:
            continue
        if URL_RE.match(t) and t not in seen:
            links.append(t)
            seen.add(t)
    if not links and URL_RE.match(text.strip()):
        links = [text.strip()]
    if not links:
        await update.message.reply_text("لینکی پیدا نشد.")
        return
    if len(links) > 1:
        SESS[chat_id] = {
            "mode": "batch_choice",
            "links": links,
            "idx": 0,
            "jobs": [],
            "selected_q": set(),
        }
        await update.message.reply_text(
            f"📋 {len(links)} لینک داری\n\n"
            "چطور انتخاب کیفیت باشه؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🎯 یک کیفیت برای همه لینک‌ها",
                    callback_data="batch_mode:all",
                )],
                [InlineKeyboardButton(
                    "📋 انتخاب جدا برای هر لینک",
                    callback_data="batch_mode:each",
                )],
            ]),
        )
        return
    await start_single(update, ctx, links[0])


def _queue_label(url, i):
    short = url if len(url) <= 45 else url[:42] + "…"
    return f"{i+1}. {short}"


def build_batch_all_quality_kb():
    """کیفیت یکسان برای همه لینک‌های صف."""
    return [
        [InlineKeyboardButton("⭐ بهترین کیفیت هر ویدیو", callback_data="batch_all:best")],
        [
            InlineKeyboardButton("1080p", callback_data="batch_all:1080"),
            InlineKeyboardButton("720p", callback_data="batch_all:720"),
        ],
        [
            InlineKeyboardButton("480p", callback_data="batch_all:480"),
            InlineKeyboardButton("360p", callback_data="batch_all:360"),
        ],
        [InlineKeyboardButton("🎵 فقط صدا (MP3) همه", callback_data="batch_all:audio")],
        [InlineKeyboardButton("❌ لغو", callback_data="batch_all:cancel")],
    ]


def _fmt_for_height(height):
    """فرمت yt-dlp: بهترین تا سقف height (اگه نداشت، پایین‌تر)."""
    h = int(height)
    return (
        f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/"
        f"bestvideo[height<={h}]/best"
    )


async def apply_quality_to_all_links(update, ctx, links, quality_key):
    """
    برای همه لینک‌ها جاب بساز با یک کیفیت مشترک، بعد برو اولویت/دانلود.
    quality_key: best | audio | 1080 | 720 | 480 | 360
    """
    chat_id = update.effective_chat.id
    is_audio = quality_key == "audio"
    if quality_key == "best":
        fmt = None
        label = "بهترین کیفیت هر ویدیو"
    elif quality_key == "audio":
        fmt = None
        label = "فقط صدا (MP3)"
    else:
        try:
            h = int(quality_key)
        except ValueError:
            h = 720
        fmt = _fmt_for_height(h)
        label = f"{h}p (یا بهترین پایین‌تر)"

    wait = await ctx.bot.send_message(
        chat_id,
        f"⏳ دارم برای {len(links)} لینک اطلاعات می‌گیرم...\nکیفیت: {label}",
    )
    jobs = []
    errors = []
    for i, url in enumerate(links):
        try:
            inf = await asyncio.wait_for(asyncio.to_thread(dl.info, url), timeout=40)
        except Exception as e:
            errors.append(f"{i+1}. {url[:40]}… → {e}")
            continue
        job = {
            "url": url,
            "inf": inf or {},
            "fmt": fmt,
            "is_audio": is_audio,
        }
        if url and dl.is_instagram_url(url):
            job["send_to_telegram"] = True
            job["ig_kind"] = "song" if is_audio else "reel"
        try:
            save_link_pref(url, job)
        except Exception:
            pass
        jobs.append(job)
        try:
            await wait.edit_text(
                f"⏳ اطلاعات ({i+1}/{len(links)})\nکیفیت: {label}\n✅ آماده: {len(jobs)}"
            )
        except Exception:
            pass

    if not jobs:
        try:
            await wait.edit_text("❌ هیچ لینکی آماده نشد.\n" + "\n".join(errors[:5]))
        except Exception:
            pass
        SESS.pop(chat_id, None)
        return

    if hasattr(dl, "save_queue"):
        try:
            dl.save_queue([j.get("url") for j in jobs if j.get("url")])
        except Exception:
            pass

    summary = f"✅ {len(jobs)} جاب با کیفیت «{label}»\n"
    if errors:
        summary += f"⚠️ رد شده: {len(errors)}\n"
    if len(jobs) == 1:
        try:
            await wait.edit_text(summary + "🚀 شروع دانلود...")
        except Exception:
            pass
        SESS.pop(chat_id, None)
        await run_single_job(update, ctx, jobs[0])
        return

    SESS[chat_id] = {
        "mode": "priority",
        "links": links,
        "jobs": jobs,
        "idx": len(links),
        "prio_order": [],
        "selected_q": set(),
    }
    try:
        await wait.edit_text(
            summary
            + "به ترتیب اولویت بزن (اولین = 1️⃣):"
        )
    except Exception:
        await ctx.bot.send_message(chat_id, summary)
    await ctx.bot.send_message(
        chat_id,
        f"📋 {len(jobs)} فایل — اولویت‌بندی:",
        reply_markup=InlineKeyboardMarkup(_build_priority_kb(jobs, [])),
    )


def build_queue_kb(links):
    kb = []
    for i, url in enumerate(links):
        row = []
        if i > 0:
            row.append(InlineKeyboardButton("⬆️", callback_data=f"qmv:up:{i}"))
        if i < len(links) - 1:
            row.append(InlineKeyboardButton("⬇️", callback_data=f"qmv:down:{i}"))
        row.append(InlineKeyboardButton("❌", callback_data=f"qmv:del:{i}"))
        kb.append(row)
    kb.append([InlineKeyboardButton(f"▶️ شروع دانلود ({len(links)} تا)", callback_data="qmv:start")])
    kb.append([InlineKeyboardButton("🗑 لغو همه", callback_data="qmv:cancelall")])
    return kb


async def show_queue_editor(update, ctx, chat_id, edit_message=None):
    s = SESS.get(chat_id)
    if not s or not s.get("links"):
        return
    links = s["links"]
    text = "📋 *صف دانلود* — با ⬆️⬇️ ترتیب رو عوض کن، با ❌ حذف کن، بعد بزن شروع:\n\n"
    text += "\n".join(_queue_label(u, i) for i, u in enumerate(links))
    kb = InlineKeyboardMarkup(build_queue_kb(links))
    if edit_message:
        try:
            await edit_message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return
        except Exception:
            pass
    await ctx.bot.send_message(chat_id, text, reply_markup=kb, parse_mode="Markdown")


async def start_single(update, ctx, url):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("در حال دریافت اطلاعات ویدیو... ⏳")
    for attempt in range(2):
        try:
            inf = await asyncio.wait_for(asyncio.to_thread(dl.info, url), timeout=40)
        except asyncio.TimeoutError:
            if attempt < 1:
                await asyncio.sleep(5)
                continue
            await msg.edit_text("❌ خطا در دریافت اطلاعات: زمان زیادی طول کشید (احتمالاً مشکل شبکه/پروکسی)")
            return
        except Exception as e:
            if attempt < 1:
                await asyncio.sleep(5)
                continue
            await msg.edit_text(f"❌ خطا در دریافت اطلاعات: {e}")
            return
        break

    # انتخاب قبلی همین لینک؟
    pref = get_link_pref(url)
    if pref and pref.get("label"):
        SESS[chat_id] = {
            "mode": "pref_confirm",
            "url": url,
            "inf": inf,
            "pref": pref,
            "is_audio": False,
            "selected_q": set(),
        }
        try:
            await tg_call(
                msg.edit_text,
                f"💾 قبلاً برای این لینک انتخاب کرده بودی:\n"
                f"➡️ {pref.get('label')}\n\n"
                f"📹 {(inf.get('title') or '')[:80]}\n\n"
                f"با همون ادامه بدم؟",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("بله ✅", callback_data="pref:yes")],
                    [InlineKeyboardButton("خیر ❌", callback_data="pref:no")],
                ]),
            )
        except Exception as e:
            print(f"[pref] show failed: {e}", flush=True)
        return

    # تشخیص تکراری قبل از دانلود
    mid = media_id_from_url(url, inf)
    dups = find_duplicates(mid, url)
    if dups:
        SESS[chat_id] = {
            "mode": "dup_check",
            "url": url,
            "inf": inf,
            "media_id": mid,
            "dups": dups,
        }
        p0 = dups[0]
        exists = p0.get("path") or "file_id"
        await tg_call(
            msg.edit_text,
            f"♻️ این محتوا قبلاً دانلود شده!\n"
            f"📹 {(inf.get('title') or '')[:80]}\n"
            f"📁 {exists}\n\n"
            f"چی کار کنم؟",
            reply_markup=build_dup_kb(mid),
        )
        return

    # پلی‌لیست یوتیوب؟ → انتخابی
    # کاروسل اینستا؟ → دانلود همه و ارسال آلبوم
    entries = inf.get("entries")
    if entries and dl.is_instagram_url(url):
        await msg.edit_text(f"🖼 کاروسل اینستا ({len(list(entries))} اسلاید) — در حال دانلود...")
        media = []
        paths_saved = []
        for e in entries:
            eurl = e.get("url") or e.get("webpage_url") or e.get("id")
            if not eurl:
                continue
            try:
                p = await asyncio.to_thread(dl.download, eurl if "http" in str(eurl) else url, None, False, None, None)
                p = _resolve_actual_audio_path(p) or p
                if p and os.path.exists(p):
                    paths_saved.append(p)
                    dl.scan_gallery(p)
                    ext = os.path.splitext(p)[1].lower()
                    if ext in (".jpg", ".jpeg", ".png", ".webp"):
                        media.append(InputMediaPhoto(open(p, "rb")))
                    else:
                        media.append(InputMediaDocument(open(p, "rb")))
            except Exception as ex:
                print(f"[carousel] slide failed: {ex}", flush=True)
        if media:
            try:
                # تلگرام حداکثر ۱۰ تا در هر گروه
                for i in range(0, len(media), 10):
                    await ctx.bot.send_media_group(chat_id, media[i:i+10])
            except Exception as ex:
                print(f"[carousel] send failed: {ex}", flush=True)
                for p in paths_saved:
                    try:
                        with open(p, "rb") as f:
                            await ctx.bot.send_document(chat_id, document=f)
                    except Exception:
                        pass
            for p in paths_saved:
                save_ig_catalog_entry({
                    "path": p,
                    "title": (inf.get("title") or "کاروسل اینستا")[:80],
                    "kind": "reel",
                    "url": url,
                })
            await msg.edit_text(f"✅ کاروسل ارسال شد ({len(paths_saved)} فایل)")
        else:
            await msg.edit_text("❌ هیچ اسلایدی دانلود نشد.")
        return
    if entries:
        pl_items = []
        for e in entries:
            eurl = e.get("url") or e.get("webpage_url") or e.get("id")
            if eurl and "http" not in str(eurl):
                eurl = f"https://www.youtube.com/watch?v={eurl}"
            if eurl:
                pl_items.append({
                    "url": eurl,
                    "title": e.get("title") or e.get("id") or "بدون عنوان",
                    "duration": e.get("duration"),
                })
        if not pl_items:
            await msg.edit_text("❌ ویدیویی توی پلی‌لیست پیدا نشد.")
            return
        SESS[chat_id] = {
            "mode": "playlist_pick",
            "pl_items": pl_items,
            "pl_selected": set(range(len(pl_items))),
        }
        await msg.edit_text(
            f"🎞 پلی‌لیست ({len(pl_items)} ویدیو)\n"
            "تیک بزن کدوم‌ها رو می‌خوای، بعد «ادامه»:",
            reply_markup=InlineKeyboardMarkup(_build_playlist_kb(pl_items, set(range(len(pl_items))))),
        )
        return
    SESS[chat_id] = {"mode": "single", "url": url, "inf": inf, "is_audio": False, "selected_q": set()}
    if dl.is_instagram_url(url):
        await tg_call(
            msg.edit_text,
            build_meta_text(inf) + "\n\n🎵 اینستاگرام — انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(build_ig_action_kb(batch=False)),
        )
        return
    kb, _ = build_quality_kb(inf, False, set(), batch=False)
    await tg_call(
        msg.edit_text,
        build_meta_text(inf) + "\n\nکیفیت را انتخاب کن (می‌تونی چندتا تیک بزنی):",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def process_next_in_batch(update, ctx, edit_msg=None):
    chat_id = update.effective_chat.id
    s = SESS.get(chat_id)
    if not s:
        return
    if s["idx"] >= len(s["links"]):
        # همه کیفیت‌ها انتخاب شدن → اولویت‌بندی
        if not s.get("jobs"):
            await ctx.bot.send_message(chat_id, "هیچ فایلی برای دانلود نموند.")
            SESS.pop(chat_id, None)
            return
        if hasattr(dl, "save_queue"):
            dl.save_queue([j.get("url") for j in s["jobs"] if j.get("url")])
        if len(s["jobs"]) == 1:
            await ctx.bot.send_message(chat_id, "🚀 شروع دانلود...")
            await run_single_job(update, ctx, s["jobs"][0])
            SESS.pop(chat_id, None)
            return
        s["mode"] = "priority"
        s["prio_order"] = []
        await ctx.bot.send_message(
            chat_id,
            f"📋 {len(s['jobs'])} فایل آماده‌ست.\n"
            "به ترتیب اولویت بزن (اولین ضربه = 1️⃣، بعدی = 2️⃣، ...):",
            reply_markup=InlineKeyboardMarkup(_build_priority_kb(s["jobs"], [])),
        )
        return
    url = s["links"][s["idx"]]
    msg = await tg_call(ctx.bot.send_message, chat_id, f"[{s['idx']+1}/{len(s['links'])}] دریافت اطلاعات...")
    inf = None
    last_err = None
    for attempt in range(2):
        try:
            inf = await asyncio.wait_for(asyncio.to_thread(dl.info, url), timeout=40)
            break
        except asyncio.TimeoutError:
            last_err = "زمان زیادی طول کشید (احتمالاً مشکل شبکه/پروکسی)"
        except Exception as e:
            last_err = e
        if attempt < 1:
            await asyncio.sleep(5)
    if inf is None:
        await tg_call(msg.edit_text, f"❌ {url}\n{last_err}")
        s["idx"] += 1
        return await process_next_in_batch(update, ctx)

    # انتخاب قبلی برای این لینک در صف؟
    pref = get_link_pref(url)
    if pref and pref.get("label"):
        s["_cur"] = {"url": url, "inf": inf}
        s["pref"] = pref
        s["mode"] = "batch"  # نگه دار
        await tg_call(
            msg.edit_text,
            f"[{s['idx']+1}/{len(s['links'])}]\n"
            f"💾 قبلاً انتخاب کرده بودی: {pref.get('label')}\n"
            f"📹 {(inf.get('title') or '')[:80]}\n\n"
            f"با همون ادامه بدم؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("بله ✅", callback_data="pref:yes")],
                [InlineKeyboardButton("خیر ❌", callback_data="pref:no")],
                [InlineKeyboardButton("⏭️ رد کردن", callback_data="batch_skip")],
            ]),
        )
        return

    s["_cur"] = {"url": url, "inf": inf}
    s["is_audio"] = False
    s["selected_q"] = set()
    nlabel = f"[{s['idx']+1}/{len(s['links'])}]\n"
    if dl.is_instagram_url(url):
        await tg_call(
            msg.edit_text,
            nlabel + build_meta_text(inf) + "\n\n🎵 اینستاگرام — انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(build_ig_action_kb(batch=True)),
        )
    else:
        kb, _ = build_quality_kb(inf, False, set(), batch=True)
        await tg_call(
            msg.edit_text,
            nlabel + build_meta_text(inf) + "\n\nکیفیت را انتخاب کن (چندتا تیک بزن):",
            reply_markup=InlineKeyboardMarkup(kb),
        )


async def tag_mp3_file(path, title=None, artist=None, album=None, cover_url=None):
    """
    اسم/خواننده/آلبوم/کاور رو تو تگ ID3 فایل mp3 می‌نویسه (با mutagen) تا تو
    گالری/پلیر گوشی درست نمایش داده بشه. برای فرمت‌های غیر mp3 کاری نمی‌کنه.
    نیاز به: pip install mutagen
    """
    if not path or not path.lower().endswith(".mp3") or not os.path.exists(path):
        return
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, APIC
    except ImportError:
        print("[tag] کتابخونه‌ی mutagen نصب نیست (pip install mutagen)", flush=True)
        return

    def _do_tag():
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        if title:
            tags.delall("TIT2")
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=album))
        tags.save(path, v2_version=3)

    await asyncio.to_thread(_do_tag)

    if cover_url:
        try:
            import aiohttp
            async with aiohttp.ClientSession(trust_env=True) as sess:
                async with sess.get(cover_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        mime = resp.headers.get("Content-Type", "image/jpeg")

                        def _do_cover():
                            tags = ID3(path)
                            tags.delall("APIC")
                            tags.add(APIC(
                                encoding=3, mime=mime, type=3,
                                desc="Cover", data=img_bytes,
                            ))
                            tags.save(path, v2_version=3)

                        await asyncio.to_thread(_do_cover)
        except Exception as e:
            print(f"[tag] دانلود/ثبت کاور ناموفق: {e}", flush=True)


def _srt_timestamp(seconds):
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


async def _groq_transcribe(audio_path, language=None):
    """
    Whisper روی Groq با timestamp در سطح segment و word.
    word timestamp برای هماهنگ‌کردن متن با Speaker Diarization استفاده می‌شود.
    """
    key = getattr(cfg, "GROQ_API_KEY", None)
    if not key:
        return None
    import aiohttp
    try:
        with open(audio_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=os.path.basename(audio_path), content_type="audio/mpeg")
            form.add_field("model", "whisper-large-v3")
            form.add_field("response_format", "verbose_json")
            form.add_field("temperature", "0")
            # زمان‌بندی کلمه‌ای؛ اگر API آن را پشتیبانی کند، برای دقت زیرنویس استفاده می‌شود.
            form.add_field("timestamp_granularities[]", "segment")
            form.add_field("timestamp_granularities[]", "word")
            if language:
                form.add_field("language", language)
            async with aiohttp.ClientSession(trust_env=True) as sess:
                async with sess.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    data=form,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    status = resp.status
                    data = await resp.json()
        if status != 200:
            print(f"[subs] Whisper خطا (status={status}): {data}", flush=True)
            return None
        return data.get("segments") or []
    except Exception as e:
        print(f"[subs] Whisper ناموفق: {type(e).__name__}: {e}", flush=True)
        return None


def _get_hf_token():
    """توکن Hugging Face برای pyannote؛ اختیاری است ولی برای تشخیص گوینده لازم است."""
    return (
        getattr(cfg, "HF_TOKEN", None)
        or getattr(cfg, "HUGGINGFACE_TOKEN", None)
        or os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_TOKEN")
    )


def _speaker_diarize_sync(audio_path):
    """
    تشخیص اینکه در هر بازه چه کسی صحبت می‌کند.
    pyannote اختیاری است؛ در صورت نبود dependency/token، None برمی‌گرداند.
    خروجی: [{start, end, speaker}, ...]
    """
    token = _get_hf_token()
    if not token:
        print("[subs] HF_TOKEN تنظیم نشده؛ Speaker Diarization رد شد.", flush=True)
        return None
    try:
        from pyannote.audio import Pipeline
        try:
            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=token,
            )
        except TypeError:
            # نسخه‌های جدید pyannote ممکن است token را با نام دیگری بخواهند.
            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=token,
            )
        diarization = pipeline(audio_path)
        turns = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            turns.append({
                "start": float(turn.start),
                "end": float(turn.end),
                "speaker": str(speaker),
            })
        print(f"[subs] Speaker Diarization: {len(turns)} turn(s)", flush=True)
        return turns or None
    except Exception as e:
        print(f"[subs] Speaker Diarization ناموفق: {type(e).__name__}: {e}", flush=True)
        return None


def _speaker_at_time(t, diarization):
    """گوینده‌ای که در زمان t فعال است؛ در صورت همپوشانی، بیشترین overlap را انتخاب می‌کند."""
    if not diarization:
        return None
    best = None
    best_score = 0.0
    for d in diarization:
        if d["start"] <= t <= d["end"]:
            # برای نقطه، همین بازه کافی است؛ بازه‌های همپوشان با نزدیک‌ترین مرکز انتخاب می‌شوند.
            center = (d["start"] + d["end"]) / 2.0
            score = 1.0 / (1.0 + abs(t - center))
            if score > best_score:
                best_score = score
                best = d["speaker"]
    return best


def _make_speaker_cues(segments, diarization):
    """
    Whisper segmentها را به cueهای کوتاه‌تر بر اساس تغییر گوینده تبدیل می‌کند.
    اگر word timestamp موجود باشد، متن در محل تغییر گوینده شکسته می‌شود.
    در غیر این صورت، کل segment به گوینده غالب نسبت داده می‌شود.
    """
    if not segments:
        return []
    if not diarization:
        return [
            {"start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
             "text": (s.get("text") or "").strip(), "speaker": None}
            for s in segments if (s.get("text") or "").strip()
        ]

    cues = []
    for seg in segments:
        seg_start = float(seg.get("start", 0) or 0)
        seg_end = float(seg.get("end", seg_start) or seg_start)
        words = seg.get("words") or []

        if words:
            current = None
            buf = []
            cue_start = None
            cue_end = None
            for w in words:
                text = (w.get("word") or w.get("text") or "").strip()
                if not text:
                    continue
                ws = float(w.get("start", seg_start) or seg_start)
                we = float(w.get("end", ws) or ws)
                speaker = _speaker_at_time((ws + we) / 2.0, diarization) or "SPEAKER_00"
                if current is None:
                    current = speaker
                    cue_start = ws
                elif speaker != current:
                    joined = " ".join(buf).strip()
                    if joined and cue_end is not None:
                        cues.append({"start": cue_start, "end": cue_end, "text": joined, "speaker": current})
                    current = speaker
                    buf = []
                    cue_start = ws
                buf.append(text)
                cue_end = we
            joined = " ".join(buf).strip()
            if joined:
                cues.append({"start": cue_start if cue_start is not None else seg_start,
                             "end": cue_end if cue_end is not None else seg_end,
                             "text": joined, "speaker": current or "SPEAKER_00"})
        else:
            # fallback: گوینده‌ای که بیشترین همپوشانی با segment دارد.
            best_speaker = "SPEAKER_00"
            best_overlap = 0.0
            for d in diarization:
                overlap = max(0.0, min(seg_end, d["end"]) - max(seg_start, d["start"]))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = d["speaker"]
            text = (seg.get("text") or "").strip()
            if text:
                cues.append({"start": seg_start, "end": seg_end, "text": text, "speaker": best_speaker})

    # cueهای خیلی کوتاه/هم‌گوینده را ادغام می‌کنیم تا زیرنویس تکه‌تکه نشود.
    merged = []
    for cue in cues:
        if not merged:
            merged.append(cue)
            continue
        prev = merged[-1]
        gap = cue["start"] - prev["end"]
        if cue["speaker"] == prev["speaker"] and gap <= 0.35 and len(prev["text"]) + len(cue["text"]) <= 90:
            prev["text"] = (prev["text"] + " " + cue["text"]).strip()
            prev["end"] = cue["end"]
        else:
            merged.append(cue)
    return merged

async def _groq_translate_batch(texts):
    """کل جمله‌ها رو یه‌جا (نه تک‌تک) به فارسی ترجمه می‌کنه تا سریع و کم‌هزینه باشه."""
    key = getattr(cfg, "GROQ_API_KEY", None)
    if not key or not texts:
        return texts
    import aiohttp
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    prompt = (
        "این جمله‌های زیرنویس رو به فارسی روان و طبیعی ترجمه کن. فقط و فقط یه آرایه‌ی JSON "
        "از رشته‌ها برگردون، دقیقاً به همون تعداد و همون ترتیب، بدون هیچ توضیح اضافه:\n\n"
        f"{numbered}"
    )
    try:
        async with aiohttp.ClientSession(trust_env=True) as sess:
            async with sess.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json={
                    "model": "openai/gpt-oss-120b",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                data = await resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
        translated = json.loads(content)
        if isinstance(translated, list) and len(translated) == len(texts):
            return translated
        print(f"[subs] ترجمه تعداد نامنطبق برگردوند ({len(translated) if isinstance(translated, list) else '?'} به‌جای {len(texts)})", flush=True)
        return texts
    except Exception as e:
        print(f"[subs] ترجمه ناموفق: {type(e).__name__}: {e}", flush=True)
        return texts


async def generate_and_send_subtitled_video(update, ctx, chat_id, url, inf):
    """
    زیرنویس فارسی بدون اجبار به GROQ:
    1) اول زیرنویس/کپشن خودکار یوتیوب (fa یا en)
    2) اگه en بود → ترجمه رایگان به فارسی
    3) چسباندن با ffmpeg
    4) فقط اگه GROQ_API_KEY باشه، به‌عنوان fallback از Whisper استفاده می‌شه
    """
    status_msg = await ctx.bot.send_message(chat_id, "⬇️ دانلود ویدیو + زیرنویس یوتیوب...")
    srt_path = None
    video_path = None
    try:
        t0 = time.time() - 2
        # دانلود با تلاش برای fa، وگرنه en
        auto = (inf or {}).get("automatic_captions") or {}
        man = (inf or {}).get("subtitles") or {}
        all_langs = list(man.keys()) + list(auto.keys())
        fa_lang = next((L for L in all_langs if L.startswith("fa")), None)
        en_lang = next((L for L in all_langs if L.startswith("en")), None)
        sub_lang = fa_lang or en_lang or "fa"
        video_path = await asyncio.to_thread(
            dl.download, url, None, False, sub_lang, None
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ دانلود ناموفق: {e}")
        return
    video_path = _resolve_actual_audio_path(video_path, since_ts=t0) or video_path
    if not video_path or not os.path.exists(video_path):
        await status_msg.edit_text("❌ دانلود ویدیو ناموفق بود.")
        return

    folder = os.path.dirname(video_path) or "."
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    sub_candidates = []
    for ext in (".srt", ".vtt", ".ass"):
        sub_candidates.extend(glob.glob(os.path.join(folder, base_name + "*" + ext)))
    sub_candidates = sorted(set(sub_candidates), key=os.path.getmtime, reverse=True)
    if sub_candidates:
        srt_path = sub_candidates[0]
        # اگه فارسی نبود ترجمه کن
        if fa_lang is None and en_lang is not None:
            await status_msg.edit_text("🌐 ترجمه زیرنویس به فارسی (بدون API)...")
            fa_sub = await asyncio.to_thread(translate_sub_file_to_fa, srt_path)
            if fa_sub:
                srt_path = fa_sub

    # اگر GROQ + HF_TOKEN داشته باشیم، مسیر دقیق‌تر را اجرا می‌کنیم:
    # Whisper + word timestamps + Speaker Diarization.
    # در این حالت حتی اگر YouTube caption داشته باشد، برای تشخیص گوینده از صدای خود ویدیو استفاده می‌کنیم.
    speaker_mode = bool(getattr(cfg, "GROQ_API_KEY", None) and _get_hf_token())
    if speaker_mode:
        await status_msg.edit_text("🎙️ تشخیص گوینده + زمان‌بندی دقیق زیرنویس...")
        audio_path = video_path + "_speech.mp3"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "libmp3lame",
                "-ar", "16000", "-ac", "1", "-ab", "64k", audio_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as e:
            await status_msg.edit_text(f"❌ استخراج صدا ناموفق: {e}")
            return

        try:
            segments = await _groq_transcribe(audio_path) if os.path.exists(audio_path) else None
            diarization = await asyncio.to_thread(_speaker_diarize_sync, audio_path) if segments else None
        finally:
            try:
                os.remove(audio_path)
            except Exception:
                pass

        if segments:
            cues = _make_speaker_cues(segments, diarization)
            if cues:
                await status_msg.edit_text(f"🌐 ترجمه {len(cues)} بخش زیرنویس...")
                texts = [c["text"] for c in cues]
                translated = []
                for t in texts:
                    translated.append(await asyncio.to_thread(_google_translate_fa, t) if t else "")

                srt_path = video_path + "_fa_speakers.srt"
                with open(srt_path, "w", encoding="utf-8") as f:
                    for i, cue in enumerate(cues):
                        start_ts = _srt_timestamp(cue["start"])
                        end_ts = _srt_timestamp(max(cue["end"], cue["start"] + 0.20))
                        txt = translated[i] if i < len(translated) else cue["text"]
                        speaker = cue.get("speaker")
                        # نام کوتاه و ثابت برای خوانایی: SPEAKER_00 -> گوینده ۱
                        if speaker and speaker.startswith("SPEAKER_"):
                            try:
                                n = int(speaker.split("_")[-1]) + 1
                                label = f"گوینده {n}"
                            except Exception:
                                label = speaker
                            txt = f"[{label}] {txt}"
                        f.write(f"{i+1}\n{start_ts} --> {end_ts}\n{txt}\n\n")

    # fallback قدیمی: فقط وقتی Speaker Diarization در دسترس نیست و زیرنویس یوتیوب هم نداریم.
    if not srt_path and getattr(cfg, "GROQ_API_KEY", None):
        await status_msg.edit_text("🎧 زیرنویس یوتیوب نبود — تشخیص گفتار با Groq...")
        audio_path = video_path + "_speech.mp3"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "libmp3lame",
                "-ar", "16000", "-ab", "64k", audio_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as e:
            await status_msg.edit_text(f"❌ استخراج صدا ناموفق: {e}")
            return
        segments = await _groq_transcribe(audio_path) if os.path.exists(audio_path) else None
        try:
            os.remove(audio_path)
        except Exception:
            pass
        if segments:
            await status_msg.edit_text(f"🌐 ترجمه {len(segments)} جمله...")
            texts = [seg.get("text", "").strip() for seg in segments]
            translated = []
            for t in texts:
                translated.append(await asyncio.to_thread(_google_translate_fa, t) if t else "")
            srt_path = video_path + "_fa.srt"
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(segments):
                    start_ts = _srt_timestamp(seg.get("start", 0))
                    end_ts = _srt_timestamp(seg.get("end", 0))
                    txt = translated[i] if i < len(translated) else texts[i]
                    f.write(f"{i+1}\n{start_ts} --> {end_ts}\n{txt}\n\n")

    if not srt_path or not os.path.exists(srt_path):
        await status_msg.edit_text(
            "❌ زیرنویس پیدا نشد.\n"
            "این ویدیو احتمالاً کپشن خودکار یوتیوب نداره.\n"
            "(اختیاری: اگه GROQ_API_KEY بذاری از روی صدا می‌سازه — لازم نیست)"
        )
        return

    await status_msg.edit_text("🔥 چسباندن زیرنویس فارسی روی ویدیو...")
    out_path = video_path + "_subtitled.mp4"
    escaped_srt = srt_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", video_path,
            "-vf", f"subtitles='{escaped_srt}':force_style='FontSize=20,Alignment=2,MarginV=25'",
            "-c:a", "copy", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            print(f"[subs] ffmpeg burn خطا: {stderr.decode(errors='ignore')[-800:]}", flush=True)
            await status_msg.edit_text("❌ چسبوندن زیرنویس شکست خورد.")
            return
    except Exception as e:
        await status_msg.edit_text(f"❌ چسبوندن زیرنویس ناموفق: {e}")
        return

    sz = os.path.getsize(out_path)
    await status_msg.edit_text(f"📤 در حال ارسال ویدیوی زیرنویس‌دار ({sz/1048576:.1f} MB)...")
    try:
        with open(out_path, "rb") as f:
            await ctx.bot.send_video(
                chat_id, video=f, caption="🎬 با زیرنویس فارسی خودکار",
                read_timeout=3600, write_timeout=3600,
            )
        await status_msg.delete()
        try:
            log_download_stat(chat_id, url, "video", sz)
        except Exception:
            pass
    except Exception as e:
        await status_msg.edit_text(f"❌ ارسال ناموفق: {e}\nفایل: {out_path}")
    finally:
        for p in (video_path, srt_path, out_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


async def execute_ig_song_extract(update, ctx, url, inf):
    """تشخیص + دانلود آهنگ اینستا (برای تک‌لینک یا وقتی از صف اجرا می‌شه)."""
    chat_id = update.effective_chat.id
    key = (chat_id, url)
    if key in _SONG_BUSY:
        # همون لینک قبلاً در حال پردازشه — دوباره اجرا نکن (لوپ)
        print(f"[song-id] هم‌اکنون در حال پردازشه، رد شد: {url[:60]}", flush=True)
        try:
            await ctx.bot.send_message(
                chat_id, "⏳ همین الان دارم روش کار می‌کنم — وقتی تموم شد می‌فرستم ☕"
            )
        except Exception:
            pass
        return False
    _SONG_BUSY[key] = True
    try:
        search_query = extract_song_query(inf or {})
        if search_query:
            await search_and_send_music_options(
                chat_id, ctx, search_query, cb_prefix="igms",
                result_key="_ig_music_results", update=update, auto_pick=True,
            )
            return True

        wait_msg = await ctx.bot.send_message(chat_id, "🎧 در حال شنیدن صدای ویدیو...")

        async def _status(text):
            try:
                await wait_msg.edit_text(text)
            except Exception:
                pass

        search_query, audd_artist, audd_title, src, confident = await identify_song_from_url(url, on_status=_status)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        if search_query:
            # وقتی تشخیص قطعی نیست (score پایین / تشخیص از روی متن)، به‌جای
            # دانلود کورکورانه، لیست کاندیدا نشون بده تا کاربر خودش آهنگ درست
            # رو انتخاب کنه (جلوگیری از دانلود آهنگ غلط).
            await search_and_send_music_options(
                chat_id, ctx, search_query, cb_prefix="igms",
                result_key="_ig_music_results", update=update,
                auto_pick=confident,
                artist=audd_artist, title=audd_title,
            )
            return True

        SESS.setdefault(chat_id, {})["awaiting_song_query"] = "igms"
        await ctx.bot.send_message(
            chat_id,
            "🎵 از هیچ‌کدوم از سه منبع (AudD، شزام، ACRCloud) نتونستم آهنگ رو تشخیص بدم.\n\n"
            "برای پیدا کردنش می‌تونی خودت از لینک‌های زیر جستجو کنی:\n"
            "🔎 یوتیوب: https://www.youtube.com/results?search_query=\n"
            "🟢 اسپاتیفای: https://open.spotify.com/search/\n\n"
            "یا اگه یه تکه از شعرش رو یادته (حتی ناقص/تقریبی) بفرست تا خودم دقیق‌تر پیداش کنم 🔍",
        )
        return False
    finally:
        _SONG_BUSY.pop(key, None)


def _job_size_bytes(job):
    """حجم تقریبی جاب از info."""
    inf = job.get("inf") or {}
    return int(inf.get("filesize") or inf.get("filesize_approx") or 0)


async def run_single_job(update, ctx, job, queue_info=None):
    """
    queue_info (اختیاری): {"idx": 1, "total": 5, "remaining_bytes": 123}
    برای نمایش زمان کل باقی‌ماندهٔ صف روی نوار پیشرفت.
    """
    chat_id = update.effective_chat.id
    # جاب معوق استخراج آهنگ اینستا (از صف چندلینک)
    if job.get("ig_extract_song"):
        await execute_ig_song_extract(update, ctx, job.get("url"), job.get("inf") or {})
        return None
    url = job["url"]
    cancel_event = asyncio.Event()
    job["_cancel_id"] = f"{chat_id}:{id(job)}"
    CANCEL_EVENTS[job["_cancel_id"]] = cancel_event
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ cancel", callback_data=f"cancel:{job['_cancel_id']}")]])
    q_hdr = ""
    if queue_info and queue_info.get("total", 0) > 1:
        q_hdr = f"📋 {queue_info['idx']}/{queue_info['total']}\n"
    prog_msg = await ctx.bot.send_message(chat_id, f"{q_hdr}⬇️ 0.0%", reply_markup=kb)

    state = {"done": False, "downloaded": 0, "total": 0, "speed": 0, "eta": 0}

    def _eta_str(sec):
        if not sec or sec < 0:
            return "—"
        sec = int(sec)
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    async def progress_edit():
        last = ""
        while not state["done"]:
            await asyncio.sleep(1)
            if state["done"]:
                break
            mb = state["downloaded"] / 1048576
            speed = state["speed"] or 0
            spd = f" | {_fmt_speed(speed)}" if speed else ""
            # ETA این فایل از سرعت واقعی yt-dlp
            file_eta = state["eta"] or 0
            if not file_eta and speed and state["total"]:
                left = max(0, state["total"] - state["downloaded"])
                file_eta = left / speed if speed else 0
            eta_file = _eta_str(file_eta) if (state["total"] or file_eta) else "—"

            # ETA کل صف با همون سرعت لحظه‌ای
            eta_queue_txt = ""
            if speed and speed > 1024:
                rem_cur = max(0, (state["total"] or 0) - (state["downloaded"] or 0))
                if not state["total"]:
                    rem_cur = max(0, _job_size_bytes(job) - (state["downloaded"] or 0))
                if queue_info and queue_info.get("total", 0) > 1:
                    rem_rest = int(queue_info.get("remaining_bytes") or 0)
                    # اگه حجم بقیه صفره، از حجم این فایل × تعداد باقی تخمین بزن
                    if rem_rest <= 0:
                        per = state["total"] or _job_size_bytes(job) or 0
                        rem_rest = per * int(queue_info.get("remaining_jobs") or 0)
                    eta_q = (rem_cur + rem_rest) / speed
                    eta_queue_txt = (
                        f"\n⏱ این فایل: {eta_file}"
                        f" | کل صف: {_eta_str(eta_q)}"
                        f" ({queue_info['idx']}/{queue_info['total']})"
                    )
                else:
                    eta_queue_txt = f"\n⏱ باقی: {eta_file}"

            est_total = _job_size_bytes(job)
            if state.get("pct") is not None:
                pct = state["pct"]
            elif state["total"]:
                pct = state["downloaded"] / state["total"] * 100
            elif est_total:
                pct = state["downloaded"] / est_total * 100
            else:
                pct = 0
            filled = int(min(pct, 100) / 5)
            bar = "▓" * filled + "░" * (20 - filled)
            tmb = (state["total"] or est_total) / 1048576
            hdr = ""
            if queue_info and queue_info.get("total", 0) > 1:
                hdr = f"📋 {queue_info['idx']}/{queue_info['total']}  "
            size_txt = f"({mb:.1f}/{tmb:.1f} MB)" if tmb else f"({mb:.1f} MB)"
            txt = f"{hdr}⬇️ {bar} {pct:.1f}%\n{size_txt}{spd}{eta_queue_txt}"
            if txt != last:
                last = txt
                try:
                    await prog_msg.edit_text(txt, reply_markup=kb)
                except Exception:
                    pass
        try:
            await prog_msg.edit_text(
                f"{q_hdr}⬇️ {'▓' * 20} ۱۰۰.۰٪\nدر حال پردازش/ادغام فایل…", reply_markup=kb)
        except Exception:
            pass

    def ydl_hook(d):
        if cancel_event.is_set():
            raise Exception("__cancel__")
        if d.get("status") == "downloading":
            state["downloaded"] = d.get("downloaded_bytes", 0)
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


    # دانلود مستقیم — ری‌ترای توی downloader.py هندل میشه (با continuedl)
    path = None
    state["downloaded"] = 0
    state["total"] = 0
    state["done"] = False
    task = asyncio.create_task(progress_edit())
    t0 = time.time() - 2
    try:
        path = await asyncio.to_thread(
            dl.download, url, job.get("fmt"), is_audio=job.get("is_audio", False),
            sub_lang=job.get("sub_lang"), progress_hook=ydl_hook,
            section=job.get("section"),
        )
    except Exception as e:
        if str(e) == "__cancel__":
            state["done"] = True
            try:
                await task
            except Exception:
                pass
            await prog_msg.edit_text("🚫 دانلود لغو شد.")
            CANCEL_EVENTS.pop(job.get("_cancel_id"), None)
            return
        state["done"] = True
        await task
        await prog_msg.edit_text(f"❌ دانلود ناموفق بود: {e}")
        return
    state["done"] = True
    await task

    if path:
        resolved = _resolve_actual_audio_path(path, since_ts=t0)
        if resolved:
            path = resolved
            dl.scan_gallery(path)
        else:
            # fallback: آخرین فایل تو دایرکتوری دانلود
            dl_dir = getattr(cfg, "DOWNLOAD_DIR", "")
            if dl_dir and os.path.isdir(dl_dir):
                files = sorted(
                    [os.path.join(dl_dir, f) for f in os.listdir(dl_dir)],
                    key=os.path.getmtime, reverse=True
                )
                if files:
                    path = files[0]
                    print(f"[done] fallback to newest file: {path}", flush=True)
        # کاور + اطلاعات ویدیو همیشه فرستاده می‌شه
        th = dl.fetch_thumbnail(job.get("inf") or {})
        if th:
            try:
                cap = build_meta_text(job.get("inf") or {}, with_desc=False)
                await ctx.bot.send_photo(chat_id, photo=open(th, "rb"),
                                         caption=cap or "🖼 کاور")
            except Exception:
                pass
        # کپشن کامل جدا از کاور می‌ره: سقف کپشن عکس در تلگرام ۱۰۲۴ کاراکتره،
        # پس داخل کپشن عکس جا نمی‌شه و باید پیام مستقل باشه.
        desc_full = ((job.get("inf") or {}).get("description") or "").strip()
        if desc_full:
            body = f"💬 کپشن:\n{desc_full}"
            while body:
                cut = body.rfind("\n", 0, 3500) if len(body) > 3500 else len(body)
                if cut < 1750:
                    cut = min(3500, len(body))
                part, body = body[:cut], body[cut:].lstrip("\n")
                try:
                    await tg_call(ctx.bot.send_message, chat_id, part)
                except Exception:
                    break

        # زیرنویس فارسی: ترجمه EN→FA (اگه لازم) + burn-in
        if path and not job.get("is_audio") and (job.get("burn_subs") or job.get("translate_fa")):
            folder = os.path.dirname(path) or "."
            base_name = os.path.splitext(os.path.basename(path))[0]
            sub_candidates = []
            for ext in (".srt", ".vtt", ".ass"):
                sub_candidates.extend(glob.glob(os.path.join(folder, base_name + "*" + ext)))
            # فقط فایل‌های تازه همین ویدیو
            sub_candidates = sorted(set(sub_candidates), key=os.path.getmtime, reverse=True)
            sub_file = sub_candidates[0] if sub_candidates else None
            if job.get("translate_fa") and sub_file:
                try:
                    await prog_msg.edit_text("🌐 ترجمه زیرنویس به فارسی...")
                except Exception:
                    pass
                fa_sub = await asyncio.to_thread(translate_sub_file_to_fa, sub_file)
                if fa_sub:
                    sub_file = fa_sub
            if job.get("burn_subs") and sub_file:
                try:
                    await prog_msg.edit_text("🔥 چسباندن زیرنویس فارسی روی ویدیو...")
                except Exception:
                    pass
                burned = await burn_subtitle_ffmpeg(path, sub_file)
                if burned:
                    path = burned
                    dl.scan_gallery(path)
                else:
                    print("[burn-sub] ffmpeg ناموفق", flush=True)
            # زیرنویس نرم = فایل جدا تا هر وقت خواستی روشنش کنی
            if job.get("soft_subs") and sub_file and os.path.exists(sub_file):
                try:
                    await prog_msg.edit_text("📄 ارسال فایل زیرنویس جدا (.srt)...")
                except Exception:
                    pass
                try:
                    with open(sub_file, "rb") as sf:
                        await ctx.bot.send_document(
                            chat_id, document=sf,
                            caption="📄 زیرنویس — تو پلیر روشن/خاموشش کن",
                            filename=os.path.basename(sub_file) if sub_file.endswith(".srt") else "subs_fa.srt",
                        )
                except Exception as e:
                    print(f"[soft-sub] send failed: {e}", flush=True)
            elif not sub_file and (job.get("burn_subs") or job.get("soft_subs")):
                print("[sub] فایل زیرنویس کنار ویدیو پیدا نشد", flush=True)

        # فشرده‌سازی هوشمند
        if path and job.get("compress") and not job.get("is_audio"):
            try:
                await prog_msg.edit_text("🗜 فشرده‌سازی هوشمند...")
            except Exception:
                pass
            base, _ext = os.path.splitext(path)
            out_c = base + "_cmp.mp4"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", path,
                    "-vf", "scale=-2:720",
                    "-c:v", "libx264", "-crf", "28", "-preset", "fast",
                    "-c:a", "aac", "-b:a", "128k",
                    out_c,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if os.path.exists(out_c) and os.path.getsize(out_c) > 1000:
                    path = out_c
                    dl.scan_gallery(path)
            except Exception as e:
                print(f"[compress] {e}", flush=True)

        # تگ‌گذاری خودکار ID3 (اسم/خواننده/کاور) برای فایل‌های صوتی آهنگ
        if path and job.get("is_audio") and (job.get("tag_title") or job.get("tag_artist")):
            try:
                await tag_mp3_file(
                    path,
                    title=job.get("tag_title"),
                    artist=job.get("tag_artist"),
                    album=job.get("tag_album"),
                    cover_url=job.get("tag_cover_url"),
                )
            except Exception as e:
                print(f"[tag] تگ‌گذاری ID3 ناموفق: {e}", flush=True)
            try:
                save_found_song({
                    "title": job.get("tag_title"),
                    "artist": job.get("tag_artist"),
                    "path": path,
                    "url": url,
                })
            except Exception:
                pass

        # توقف موقت صف
        global QUEUE_PAUSED
        while QUEUE_PAUSED:
            try:
                await prog_msg.edit_text("⏸ صف متوقف است — دکمه «ادامه صف» را بزن")
            except Exception:
                pass
            await asyncio.sleep(2)

        sz = os.path.getsize(path) if path and os.path.exists(path) else 0
        mb = sz / (1024 * 1024)
        title = ((job.get("inf") or {}).get("title") or "بدون عنوان")[:80]
        try:
            save_history_entry({
                "title": title,
                "url": url,
                "path": path,
                "size_mb": round(mb, 2),
                "is_audio": bool(job.get("is_audio")),
                "fmt": job.get("fmt"),
                "media_id": media_id_from_url(url, job.get("inf")),
                "ts": _now_tehran().isoformat(),
            })
        except Exception as e:
            print(f"[history] save failed: {e}", flush=True)
        # کاتالوگ اینستا برای ساخت لینک موقت (ریلز + آهنگ استخراج‌شده)
        # نکته: این بلاک قبلاً تو try/except نبود — اگه یه خط وسطش (مثلاً
        # media_id_from_url) خطا می‌داد، کل ذخیره‌سازی کاتالوگ بی‌صدا از دست
        # می‌رفت و تو «قسمت لینک» اون فایل اصلاً دیده نمی‌شد
        try:
            is_ig = bool(job.get("ig_kind")) or bool(url and dl.is_instagram_url(url))
            if path and os.path.exists(path) and is_ig:
                kind = job.get("ig_kind") or ("song" if job.get("is_audio") else "reel")
                save_ig_catalog_entry({
                    "path": path,
                    "title": title,
                    "kind": kind,
                    "url": url,
                    "media_id": media_id_from_url(url, job.get("inf")),
                })
                print(f"[ig_catalog] ذخیره شد: kind={kind} title={title[:40]!r}", flush=True)
                # ریلز اینستا همیشه تو چت هم بره
                if kind == "reel" or (url and dl.is_instagram_url(url) and not job.get("is_audio")):
                    job["send_to_telegram"] = True
            else:
                print(
                    f"[ig_catalog] رد شد — path={bool(path)} exists={path and os.path.exists(path)} "
                    f"ig_kind={job.get('ig_kind')!r} is_instagram={url and dl.is_instagram_url(url)}",
                    flush=True,
                )
        except Exception as e:
            print(f"[ig_catalog] save failed: {e}", flush=True)

        # شناسه کوتاه برای دکمه‌های باز/فشرده
        file_key = f"{chat_id}:{abs(hash(path)) % 10**8}"
        SESS.setdefault("_files", {})[file_key] = path

        # روی VPS همیشه به تلگرام می‌فرستیم (ذخیره روی گوشی نداریم)
        send_to_tg = True
        if False:
            return

        # ارسال با نوار پیشرفت زمان‌محور (نه read دیسک)
        max_retries = 5
        extra_markup = job.get("extra_markup")
        sent_msg = None
        kind_label = "آهنگ" if job.get("is_audio") else "فایل"

        for attempt in range(max_retries):
            assumed_bps = get_assumed_upload_bps()
            t_start = time.time()
            upload_state = {"done": False, "t0": t_start, "bps": assumed_bps}

            async def upload_progress_edit():
                last = ""
                while not upload_state["done"]:
                    await asyncio.sleep(1.2)
                    if upload_state["done"]:
                        break
                    elapsed = max(0.1, time.time() - upload_state["t0"])
                    bps = upload_state["bps"] or (20 * 1024)
                    est_sent = min(elapsed * bps, sz * 0.97)
                    pct = min(est_sent / max(sz, 1) * 100, 97.0)
                    filled = int(pct / 5)
                    bar = "▓" * filled + "░" * (20 - filled)
                    mb_ = est_sent / 1048576
                    tmb = sz / 1048576
                    remain = max(0, (sz - est_sent) / bps)
                    txt = (
                        f"📤 آپلود {kind_label}\n"
                        f"{bar} {pct:.0f}%\n"
                        f"({mb_:.1f}/{tmb:.1f} MB) | {_fmt_speed(bps)}\n"
                        f"⏱ باقیمانده حدودی: {_fmt_eta(remain)}"
                    )
                    if txt != last:
                        last = txt
                        try:
                            await prog_msg.edit_text(txt)
                        except Exception:
                            pass

            upload_task = asyncio.create_task(upload_progress_edit())
            try:
                print(f"[done] path={path} size={sz} attempt={attempt+1} bps≈{assumed_bps:.0f}", flush=True)
                try:
                    await prog_msg.edit_text(
                        f"📤 شروع آپلود {kind_label} ({sz / 1048576:.1f} MB)\n"
                        f"سرعت تخمینی: {_fmt_speed(assumed_bps)}"
                    )
                except Exception:
                    pass

                audio_caption = job.get("caption") or (
                    f"🎵 {job.get('tag_title')}" if job.get("tag_title") else "✅ دانلود تمام شد"
                )
                # اگر بزرگ‌تر از سقف تلگرام باشد با تقسیم ارسال کن
                # (ابری: ۵۰MB — سرور محلی: ~۱۹۰۰MB، یعنی عملاً یک‌جا)
                if sz > getattr(cfg, "TELEGRAM_MAX_BYTES", 50 * 1024 * 1024) and SPLIT_OK:
                    try:
                        await prog_msg.edit_text(
                            f"📦 فایل بزرگ ({sz/1048576:.1f} MB) — تقسیم و ارسال تکه‌های ~۴۵MB…"
                        )
                    except Exception:
                        pass
                    sent_msg = await send_file_smart(
                        ctx.bot, chat_id, path,
                        caption="✅ دانلود تمام شد",
                        is_audio=bool(job.get("is_audio")),
                        chunk_size=getattr(cfg, "SPLIT_CHUNK_BYTES", 45*1024*1024),
                    )
                    upload_state["done"] = True
                    try:
                        await upload_task
                    except Exception:
                        pass
                    elapsed = max(0.5, time.time() - t_start)
                    real_bps = sz / elapsed
                    save_measured_upload_bps(real_bps)
                    try:
                        await prog_msg.edit_text(
                            f"✅ ارسال چندتکه‌ای تمام شد\n"
                            f"📦 {sz/1048576:.1f} MB\n"
                            f"⚡️ سرعت واقعی آپلود: {_fmt_speed(real_bps)}\n"
                            f"⏱ {int(elapsed)} ثانیه"
                        )
                    except Exception:
                        pass
                    break

                with open(path, "rb") as f:
                    pf = _UploadProgressFile(f, sz)
                    if job.get("is_audio"):
                        try:
                            sent_msg = await ctx.bot.send_audio(
                                chat_id, audio=pf,
                                caption=audio_caption,
                                title=job.get("tag_title") or None,
                                performer=job.get("tag_artist") or None,
                                reply_markup=extra_markup,
                                read_timeout=3600, write_timeout=3600,
                            )
                        except Exception as ea:
                            print(f"[done] send_audio failed: {ea}", flush=True)
                            f.seek(0)
                            pf = _UploadProgressFile(f, sz)
                            sent_msg = await ctx.bot.send_document(
                                chat_id, document=pf,
                                caption=audio_caption,
                                reply_markup=extra_markup,
                                read_timeout=3600, write_timeout=3600,
                            )
                    else:
                        sent_msg = await ctx.bot.send_document(
                            chat_id, document=pf,
                            caption="✅ دانلود تمام شد",
                            reply_markup=extra_markup,
                            read_timeout=3600, write_timeout=3600,
                        )

                elapsed = max(0.5, time.time() - t_start)
                real_bps = sz / elapsed
                save_measured_upload_bps(real_bps)
                upload_state["done"] = True
                try:
                    await upload_task
                except Exception:
                    pass
                try:
                    await prog_msg.edit_text(
                        f"✅ {kind_label} ارسال شد\n"
                        f"📦 {sz / 1048576:.1f} MB\n"
                        f"⚡️ سرعت واقعی آپلود: {_fmt_speed(real_bps)}\n"
                        f"⏱ {int(elapsed)} ثانیه"
                    )
                except Exception:
                    pass
                # مهم: Telegram بعد از اولین آپلود یک file_id پایدار می‌دهد.
                # آن را ذخیره می‌کنیم تا لینک‌های اشتراک بعدی بدون آپلود مجدد ارسال شوند.
                try:
                    sent_file_id = None
                    if sent_msg:
                        if getattr(sent_msg, "audio", None):
                            sent_file_id = sent_msg.audio.file_id
                        elif getattr(sent_msg, "video", None):
                            sent_file_id = sent_msg.video.file_id
                        elif getattr(sent_msg, "document", None):
                            sent_file_id = sent_msg.document.file_id
                    if sent_file_id:
                        job["file_id"] = sent_file_id
                        update_ig_catalog_file_id(path, sent_file_id)
                        print(f"[telegram-cache] file_id ذخیره شد برای {os.path.basename(path)}", flush=True)
                except Exception as e:
                    print(f"[telegram-cache] ذخیره file_id ناموفق: {e}", flush=True)
                print(f"[done] sent ok real_bps={real_bps:.0f} elapsed={elapsed:.1f}s", flush=True)
                try:
                    # فایل‌های کاتالوگ اینستا (ریلز/آهنگ) برای «ساخت لینک» نگه داشته
                    # می‌شن — پاک کردنشون بعد ارسال، لیست لینک رو همیشه خالی می‌کرد.
                    _is_ig_keep = bool(job.get("ig_kind")) or bool(
                        url and dl.is_instagram_url(url)
                    )
                    if (not _is_ig_keep) and getattr(cfg, "DELETE_AFTER_SEND", True) and path and os.path.isfile(path):
                        cleanup_path(path)
                        print(f"[cleanup] حذف فایل لوکال: {path}", flush=True)
                    elif _is_ig_keep:
                        print(f"[cleanup] نگه داشته شد برای لینک: {path}", flush=True)
                except Exception as _ce:
                    print(f"[cleanup] {_ce}", flush=True)
                try:
                    log_download_stat(chat_id, url, "audio" if job.get("is_audio") else "video", sz)
                except Exception as e:
                    print(f"[stats] ثبت آمار ناموفق: {e}", flush=True)
                break
            except Exception as e:
                upload_state["done"] = True
                try:
                    await upload_task
                except Exception:
                    pass
                print(f"[done] attempt {attempt+1} failed: {type(e).__name__}: {e}", flush=True)
                traceback.print_exc()
                if attempt < max_retries - 1:
                    try:
                        await prog_msg.edit_text(f"🔄 تلاش مجدد ارسال ({attempt+2}/{max_retries})...")
                    except Exception:
                        pass
                    await asyncio.sleep(5)
                    continue
                try:
                    await prog_msg.edit_text(
                        f"❌ ارسال فایل در تلگرام ناموفق بود (بعد از {max_retries} تلاش).\n"
                        f"فایل در مسیر زیر ذخیره شده:\n{path}"
                    )
                except Exception:
                    pass
                # فایلِ ارسال‌نشده هم نمونه تا volume پر نشه (به‌جز فایل اینستا
                # که برای لینک‌های اشتراک نگه داشته می‌شه).
                try:
                    _is_ig_keep = bool((job or {}).get("ig_kind")) or bool(
                        url and dl.is_instagram_url(url)
                    )
                    if (not _is_ig_keep) and getattr(cfg, "DELETE_AFTER_SEND", True) and path and os.path.isfile(path):
                        cleanup_path(path)
                        print(f"[cleanup] حذف فایل ارسال‌نشده: {path}", flush=True)
                except Exception:
                    pass
    else:
        try:
            await prog_msg.edit_text("❌ دانلود ناموفق بود.")
        except Exception:
            pass
    return sent_msg


async def button(update, ctx):
    q = update.callback_query
    # فقط مالک — بقیه «سیکتیر»
    if not q.from_user or q.from_user.id not in cfg.OWNER_ID:
        try:
            await q.answer("سیکتیر", show_alert=False)
        except Exception:
            pass
        return
    chat_id = q.message.chat_id
    data = q.data or ""
    s = SESS.get(chat_id)

    # فقط یک‌بار answer — دوبار زدن باعث می‌شه دکمه‌ها «کار نکنن»
    _answered = {"v": False}

    async def _ans(text=None, alert=False):
        if _answered["v"]:
            return
        try:
            await q.answer(text=text, show_alert=alert)
            _answered["v"] = True
        except Exception:
            _answered["v"] = True

    await _ans()

    # ─── دکمه‌های کارت فیلم (مشابه‌ها، بازیگران، تریلر، فصل‌ها ...) → ماژول movie_bot ───
    if MOVIE_OK and data.split(":", 1)[0] in MOVIE_CB_PREFIXES:
        try:
            await mv_handle_callback(update, ctx)
        except Exception as e:
            print(f"[movie] کال‌بک کارت خطا داد: {type(e).__name__}: {e}", flush=True)
            try:
                await ctx.bot.send_message(chat_id, "❌ این دکمه خطا داد.")
            except Exception:
                pass
        return

    # ─── چند لینک: یک کیفیت برای همه / جدا ───
    if data.startswith("batch_mode:"):
        mode = data.split(":", 1)[1]
        links = (s or {}).get("links") or []
        if not links:
            await ctx.bot.send_message(chat_id, "لینکی نیست — دوباره بفرست")
            return
        try:
            await q.message.delete()
        except Exception:
            pass
        if mode == "each":
            SESS[chat_id] = {
                "mode": "batch",
                "links": links,
                "idx": 0,
                "jobs": [],
                "selected_q": set(),
            }
            await ctx.bot.send_message(
                chat_id,
                f"📋 {len(links)} لینک — برای هر کدوم جدا انتخاب کن:",
            )
            await process_next_in_batch(update, ctx)
            return
        if mode == "all":
            SESS[chat_id] = {
                "mode": "batch_all",
                "links": links,
                "idx": 0,
                "jobs": [],
                "selected_q": set(),
            }
            await ctx.bot.send_message(
                chat_id,
                f"🎯 یک کیفیت برای هر {len(links)} لینک:\n"
                "اگه ویدیویی اون کیفیت رو نداشت، بهترین پایین‌تر گرفته می‌شه.",
                reply_markup=InlineKeyboardMarkup(build_batch_all_quality_kb()),
            )
            return
        return

    if data.startswith("batch_all:"):
        key = data.split(":", 1)[1]
        links = (s or {}).get("links") or []
        try:
            await q.message.delete()
        except Exception:
            pass
        if key == "cancel":
            SESS.pop(chat_id, None)
            await ctx.bot.send_message(chat_id, "❌ لغو شد.")
            return
        if not links:
            await ctx.bot.send_message(chat_id, "لینکی نیست")
            return
        await apply_quality_to_all_links(update, ctx, links, key)
        return

    # توقف / ادامه صف
    if data == "queue:pause":
        global QUEUE_PAUSED
        QUEUE_PAUSED = True
        await ctx.bot.send_message(chat_id, "⏸ صف متوقف شد. برای ادامه «▶️ ادامه صف» رو بزن.")
        return
    if data == "queue:resume":
        QUEUE_PAUSED = False
        await ctx.bot.send_message(chat_id, "▶️ صف ادامه پیدا کرد.")
        return

    # یک کیفیت برای همه لینک‌های صف
    if data.startswith("bulk:"):
        action = data[5:]
        if action == "menu":
            try:
                await q.edit_message_text(
                    "🎯 یک کیفیت برای *همه* لینک‌های باقی‌مانده انتخاب کن:\n"
                    "• بهترین = هر ویدیو بالاترین کیفیت خودش\n"
                    "• 1080/720/... = همون رزولوشن (اگه نداشت پایین‌تر)",
                    reply_markup=InlineKeyboardMarkup(build_bulk_quality_kb()),
                    parse_mode="Markdown",
                )
            except Exception:
                await ctx.bot.send_message(
                    chat_id,
                    "🎯 یک کیفیت برای همه لینک‌ها:",
                    reply_markup=InlineKeyboardMarkup(build_bulk_quality_kb()),
                )
            return
        if action == "back":
            cur = (s or {}).get("_cur") or {}
            inf = cur.get("inf") or (s or {}).get("inf") or {}
            url = cur.get("url") or (s or {}).get("url") or ""
            is_batch = (s or {}).get("mode") == "batch" or bool((s or {}).get("links"))
            if url and dl.is_instagram_url(url):
                kb = build_ig_action_kb(batch=is_batch)
            else:
                kb, _ = build_quality_kb(inf, False, set(), batch=is_batch)
            try:
                await q.edit_message_text(
                    build_meta_text(inf) + "\n\nکیفیت را انتخاب کن:",
                    reply_markup=InlineKeyboardMarkup(kb),
                )
            except Exception:
                await ctx.bot.send_message(
                    chat_id, "برگشت به منو:", reply_markup=InlineKeyboardMarkup(kb)
                )
            return
        if action in ("best", "1080", "720", "480", "360", "audio", "ig_song"):
            try:
                await q.message.delete()
            except Exception:
                pass
            await apply_bulk_quality_to_batch(update, ctx, action)
            return
        return

    # نتیجه جستجوی کتابخانه
    if data.startswith("libsrch:"):
        try:
            idx = int(data.split(":")[1])
        except ValueError:
            return
        paths = (s or {}).get("lib_search") or []
        if idx >= len(paths):
            return
        path = paths[idx]
        if not path or not os.path.exists(path):
            await ctx.bot.send_message(chat_id, "فایل روی گوشی نیست")
            return
        try:
            with open(path, "rb") as f:
                if path.lower().endswith((".mp3", ".m4a", ".ogg", ".flac")):
                    await ctx.bot.send_audio(chat_id, audio=f)
                else:
                    await ctx.bot.send_document(chat_id, document=f)
        except Exception as e:
            await ctx.bot.send_message(chat_id, f"خطا: {e}")
        return

    # ─── انتخاب قبلی لینک ───
    if data in ("pref:yes", "pref:no"):
        if not s:
            await ctx.bot.send_message(chat_id, "منقضی شده — لینک رو دوباره بفرست")
            return
        url = s.get("url") or (s.get("_cur") or {}).get("url")
        inf = s.get("inf") or (s.get("_cur") or {}).get("inf")
        pref = s.get("pref") or {}
        try:
            await q.message.delete()
        except Exception:
            pass

        if data == "pref:yes" and pref:
            job = {
                "url": url,
                "inf": inf,
                "is_audio": bool(pref.get("is_audio")),
                "fmt": pref.get("fmt"),
                "ig_extract_song": bool(pref.get("ig_extract_song")),
                "ig_kind": pref.get("ig_kind"),
                "send_to_telegram": bool(pref.get("send_to_telegram", False)),
                "caption": pref.get("caption"),
                "tag_artist": pref.get("tag_artist"),
                "tag_title": pref.get("tag_title"),
            }
            await finalize_job(update, ctx, job)
            return

        # خیر → منوی معمولی
        if s.get("mode") == "batch" or s.get("links"):
            s["mode"] = "batch"
            s["_cur"] = {"url": url, "inf": inf}
            s["is_audio"] = False
            s["selected_q"] = set()
            s.pop("pref", None)
            idx = s.get("idx", 0)
            total = len(s.get("links") or [])
            nlabel = f"[{idx+1}/{total}]\n"
            if dl.is_instagram_url(url or ""):
                await tg_call(
                    ctx.bot.send_message,
                    chat_id,
                    nlabel + build_meta_text(inf or {}) + "\n\n🎵 اینستاگرام — انتخاب کن:",
                    reply_markup=InlineKeyboardMarkup(build_ig_action_kb(batch=True)),
                )
            else:
                kb, _ = build_quality_kb(inf or {}, False, set(), batch=True)
                await tg_call(
                    ctx.bot.send_message,
                    chat_id,
                    nlabel + build_meta_text(inf or {}) + "\n\nکیفیت را انتخاب کن:",
                    reply_markup=InlineKeyboardMarkup(kb),
                )
            return

        SESS[chat_id] = {"mode": "single", "url": url, "inf": inf, "is_audio": False, "selected_q": set()}
        if dl.is_instagram_url(url or ""):
            await tg_call(
                ctx.bot.send_message,
                chat_id,
                build_meta_text(inf or {}) + "\n\n🎵 اینستاگرام — انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(build_ig_action_kb(batch=False)),
            )
        else:
            kb, _ = build_quality_kb(inf or {}, False, set(), batch=False)
            await tg_call(
                ctx.bot.send_message,
                chat_id,
                build_meta_text(inf or {}) + "\n\nکیفیت را انتخاب کن (می‌تونی چندتا تیک بزنی):",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        return

    # ─── ویرایش صف دانلود (فیچر ۱: صف با اولویت) ───
    if data.startswith("qmv:"):
        parts = data.split(":")
        action = parts[1]
        if not s or not s.get("links"):
            await ctx.bot.send_message(chat_id, "این صف دیگه معتبر نیست. لینک‌ها رو دوباره بفرست.")
            return
        links = s["links"]

        if action == "cancelall":
            SESS.pop(chat_id, None)
            try:
                await q.message.edit_text("🗑 صف لغو شد.")
            except Exception:
                pass
            return

        if action == "start":
            try:
                await q.message.delete()
            except Exception:
                pass
            await process_next_in_batch(update, ctx)
            return

        idx = int(parts[2])
        if action == "up" and idx > 0:
            links[idx - 1], links[idx] = links[idx], links[idx - 1]
        elif action == "down" and idx < len(links) - 1:
            links[idx + 1], links[idx] = links[idx], links[idx + 1]
        elif action == "del":
            if len(links) <= 1:
                SESS.pop(chat_id, None)
                try:
                    await q.message.edit_text("🗑 صف خالی شد و لغو شد.")
                except Exception:
                    pass
                return
            links.pop(idx)

        await show_queue_editor(update, ctx, chat_id, edit_message=q.message)
        return

    # ─── متن آهنگ — کامل، حتی اگه چند پیام بشه ───
    if data.startswith("lyrics:"):
        lyrics_id = data.split(":", 1)[1]
        entry = LYRICS_CACHE.get(lyrics_id)
        if entry is None:
            await q.answer("❌ متن آهنگ دیگه در دسترس نیست.", show_alert=True)
            return
        artist = title = None
        if isinstance(entry, dict) and entry.get("pending"):
            if lyrics_id in _LYRICS_BUSY:
                await q.answer("⏳ همین الان دارم می‌گیرمش — یک لحظه صبر کن ☕")
                return
            artist = entry.get("artist") or ""
            title = entry.get("title") or ""
            raw = entry.get("raw") or title
            # پیام ماندگار «در حال جستجو» — نه اینکه موقع زدن دکمه هیچی نیاد
            try:
                status_msg = await ctx.bot.send_message(
                    chat_id,
                    f"🔍 دارم دنبال متن کامل آهنگ می‌گردم"
                    + (f" برای «{title}»" if title else "")
                    + " — یک لحظه...",
                )
            except Exception as e:
                print(f"[lyrics] status send: {e}", flush=True)
                status_msg = None
            await q.answer("🔍 در حال جستجو...")
            _LYRICS_BUSY.add(lyrics_id)
            try:
                lyrics_text = await fetch_lyrics(artist, title, raw_title=raw)
            finally:
                _LYRICS_BUSY.discard(lyrics_id)
            if lyrics_text:
                LYRICS_CACHE[lyrics_id] = lyrics_text
                if status_msg is not None:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
            else:
                fail_text = (
                    f"❌ متن آهنگ پیدا نشد"
                    + (f" برای «{title}»" if title else "")
                    + (f" — {artist}" if artist else "")
                    + "."
                )
                if status_msg is not None:
                    try:
                        await status_msg.edit_text(fail_text)
                        return
                    except Exception:
                        pass
                await ctx.bot.send_message(chat_id, fail_text)
                return
        else:
            lyrics_text = entry if isinstance(entry, str) else None
            if not lyrics_text:
                await q.answer("❌ متن آهنگ خالیه.", show_alert=True)
                return
        await q.answer()
        await send_full_lyrics(ctx, chat_id, lyrics_text, artist=artist, title=title)
        return

    # ─── کل ویدیو (بازه زمانی) ───
    if data == "clip:full":
        pending = (s or {}).pop("awaiting_clip", None) if s else None
        if not pending:
            await q.answer("چیزی در انتظار نیست", show_alert=True)
            return
        try:
            await q.message.delete()
        except Exception:
            pass
        if len(pending) == 1 and not pending[0].get("is_audio"):
            SESS.setdefault(chat_id, {})["_pending_job"] = pending[0]
            await ctx.bot.send_message(
                chat_id,
                "💬 زیرنویس چطور باشه؟",
                reply_markup=build_subs_kb(pending[0].get("inf") or {}),
            )
        else:
            for job in pending:
                await finalize_job(update, ctx, job)
        return

    # ─── ساخت لینک موقت ───
    if data.startswith("sharepick:"):
        action = data.split(":", 1)[1]
        if action == "cancel":
            try:
                await q.message.delete()
            except Exception:
                pass
            SESS.pop(chat_id, None)
            await ctx.bot.send_message(chat_id, "لغو شد.", reply_markup=MAIN_REPLY_KB)
            return
        try:
            idx = int(action)
        except ValueError:
            return
        items = (s or {}).get("_share_items") or []
        if idx < 0 or idx >= len(items):
            await q.answer("آیتم نامعتبر", show_alert=True)
            return
        SESS.setdefault(chat_id, {})["share_item"] = items[idx]
        try:
            await q.message.delete()
        except Exception:
            pass
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("۱ نفر", callback_data="shareuses:1"),
                InlineKeyboardButton("۲ نفر", callback_data="shareuses:2"),
            ],
            [
                InlineKeyboardButton("۳ نفر", callback_data="shareuses:3"),
                InlineKeyboardButton("۵ نفر", callback_data="shareuses:5"),
            ],
            [InlineKeyboardButton("✍️ دلخواه", callback_data="shareuses:custom")],
            [InlineKeyboardButton("❌ لغو", callback_data="sharepick:cancel")],
        ])
        title = (items[idx].get("title") or "")[:40]
        await ctx.bot.send_message(
            chat_id,
            f"👥 چند نفر بتونن دانلود کنن؟\n📁 {title}",
            reply_markup=kb,
        )
        return

    if data.startswith("shareuses:"):
        val = data.split(":", 1)[1]
        if val == "custom":
            SESS.setdefault(chat_id, {})["awaiting_share_uses"] = True
            try:
                await q.message.edit_text("تعداد نفر رو عدد بفرست (۱ تا ۱۰۰):")
            except Exception:
                await ctx.bot.send_message(chat_id, "تعداد نفر رو عدد بفرست (۱ تا ۱۰۰):")
            return
        try:
            n = int(val)
        except ValueError:
            return
        SESS.setdefault(chat_id, {})["share_max_uses"] = n
        try:
            await q.message.delete()
        except Exception:
            pass
        await _ask_share_expiry(update, ctx, chat_id)
        return

    if data.startswith("shareexp:"):
        val = data.split(":", 1)[1]
        if val == "custom":
            SESS.setdefault(chat_id, {})["awaiting_share_exp"] = True
            try:
                await q.message.edit_text("چند ساعت دیگه منقضی بشه؟ (عدد بفرست، مثلاً 48)")
            except Exception:
                await ctx.bot.send_message(chat_id, "چند ساعت دیگه منقضی بشه؟ (عدد بفرست)")
            return
        try:
            hours = float(val)
        except ValueError:
            return
        try:
            await q.message.delete()
        except Exception:
            pass
        # بعد از انقضا → رمز اختیاری
        SESS.setdefault(chat_id, {})["pending_share_exp"] = hours
        SESS[chat_id]["awaiting_share_pw"] = True
        await ctx.bot.send_message(
            chat_id,
            "🔐 رمز برای لینک می‌خوای؟\n"
            "یه رمز بفرست، یا بنویس «نه» برای بدون رمز.",
        )
        return

    # این‌ها حتی بدون سشن فعال کار می‌کنن
    if data.startswith("open:"):
        key = data[5:]
        path = (SESS.get("_files") or {}).get(key)
        if not path or not os.path.exists(path):
            await q.answer("فایل پیدا نشد", show_alert=True)
            return
        try:
            subprocess.run(
                ["am", "start", "-a", "android.intent.action.VIEW",
                 "-d", "file://" + path, "-t", "*/*"],
                stderr=subprocess.DEVNULL, timeout=10,
            )
            await q.answer("باز شد 📂")
        except Exception as e:
            await q.answer(f"خطا: {e}", show_alert=True)
        return

    if data.startswith("cmp:"):
        key = data[5:]
        path = (SESS.get("_files") or {}).get(key)
        if not path or not os.path.exists(path):
            await q.answer("فایل پیدا نشد", show_alert=True)
            return
        await q.answer("در حال فشرده‌سازی...")
        wait = await ctx.bot.send_message(chat_id, "🗜 در حال فشرده‌سازی با ffmpeg...")
        base, ext = os.path.splitext(path)
        out = base + "_compressed.mp4"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", path,
                "-vf", "scale=-2:720",
                "-c:v", "libx264", "-crf", "28", "-preset", "fast",
                "-c:a", "aac", "-b:a", "128k",
                out,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if os.path.exists(out) and os.path.getsize(out) > 0:
                dl.scan_gallery(out)
                old_mb = os.path.getsize(path) / 1048576
                new_mb = os.path.getsize(out) / 1048576
                fk = f"{chat_id}:{abs(hash(out)) % 10**8}"
                SESS.setdefault("_files", {})[fk] = out
                await wait.edit_text(
                    f"✅ فشرده شد\n"
                    f"📦 قبل: {old_mb:.1f} MB → بعد: {new_mb:.1f} MB\n"
                    f"📁 {out}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📂 باز کردن", callback_data=f"open:{fk}")],
                    ]),
                )
            else:
                await wait.edit_text("❌ فشرده‌سازی ناموفق بود.")
        except Exception as e:
            await wait.edit_text(f"❌ خطا در فشرده‌سازی: {e}")
        return

    if data.startswith("hist:"):
        try:
            idx = int(data[5:])
        except ValueError:
            return
        hist = load_history()
        if idx < 0 or idx >= len(hist):
            await q.answer("پیدا نشد", show_alert=True)
            return
        h = hist[idx]
        url = h.get("url")
        if not url:
            await q.answer("لینک نیست", show_alert=True)
            return
        await q.answer("در حال آماده‌سازی...")
        # start_single از update.message استفاده می‌کنه؛ برای callback یه فیک بساز
        class _FakeMsg:
            async def reply_text(self, *a, **k):
                return await ctx.bot.send_message(chat_id, *a, **k)
        class _FakeUpdate:
            effective_chat = type("C", (), {"id": chat_id})()
            message = _FakeMsg()
        await start_single(_FakeUpdate(), ctx, url)
        return

    if not s:
        # فقط برای دکمه‌هایی که به سشن نیاز دارن
        if data.startswith(("qt:", "q:", "qgo", "ig_", "batch_", "ch:", "sub", "pl:", "prio", "dup:")):
            await ctx.bot.send_message(
                chat_id,
                "⏱ این منو منقضی شده.\nلینک‌ها رو دوباره بفرست تا دکمه‌ها کار کنن.",
            )
        return

    if data == "audio_toggle":
        s["is_audio"] = not s.get("is_audio", False)
        cur = s.get("_cur") or {}
        inf = cur.get("inf") or s.get("inf")
        kb, _ = build_quality_kb(inf, s["is_audio"], batch=(s.get("mode") == "batch"))
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "batch_skip":
        try:
            await q.message.delete()
        except Exception:
            pass
        s["idx"] = s.get("idx", 0) + 1
        await process_next_in_batch(update, ctx)
        return

    # ─── چندانتخابی کیفیت ───
    if data.startswith("qt:"):
        # شامل qt:subs_fa — فقط تیک می‌خوره و می‌ره تو صف با «ادامه»
        key = data[3:]
        sel = s.setdefault("selected_q", set())
        if key in sel:
            sel.discard(key)
        else:
            sel.add(key)
        cur = s.get("_cur") or {}
        inf = cur.get("inf") or s.get("inf")
        kb, _ = build_quality_kb(
            inf, s.get("is_audio", False), sel, batch=(s.get("mode") == "batch")
        )
        try:
            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return

    if data == "qgo":
        sel = s.get("selected_q") or set()
        if not sel:
            await q.answer("حداقل یکی رو تیک بزن", show_alert=True)
            return
        cur = s.get("_cur") or {}
        base_url = cur.get("url") or s.get("url")
        inf = cur.get("inf") or s.get("inf")
        jobs = []
        auto = (inf or {}).get("automatic_captions") or {}
        man = (inf or {}).get("subtitles") or {}
        all_langs = list(man.keys()) + list(auto.keys())
        fa_lang = next((L for L in all_langs if L.startswith("fa")), None)
        en_lang = next((L for L in all_langs if L.startswith("en")), None)

        # فلگ‌های مشترک روی همه جاب‌های این انتخاب
        want_soft = "subs_soft" in sel
        want_burn = "subs_fa" in sel
        want_compress = "compress" in sel
        for key in sel:
            if key in ("subs_soft", "subs_fa", "compress"):
                continue  # فلگ‌ها جدا اعمال می‌شن
            job = {"url": base_url, "inf": inf, "fmt": None, "is_audio": False}
            if key == "best":
                job["fmt"] = None
            elif key == "audio":
                job["is_audio"] = True
                job["fmt"] = None
            else:
                job["fmt"] = key
            if want_soft or want_burn:
                job["sub_lang"] = fa_lang or en_lang or "fa"
                job["translate_fa"] = not bool(fa_lang)
                job["soft_subs"] = want_soft
                job["burn_subs"] = want_burn
            if want_compress:
                job["compress"] = True
            # فقط ریلز اینستا و آهنگ آپلود می‌شن — یوتیوب فقط روی گوشی ذخیره می‌شه
            if base_url and dl.is_instagram_url(base_url):
                job["send_to_telegram"] = True
                job["ig_kind"] = "song" if job["is_audio"] else "reel"
            jobs.append(job)
        # اگه فقط فلگ زیرنویس/فشرده بدون کیفیت — بهترین + فلگ
        if not jobs and (want_soft or want_burn or want_compress):
            job = {"url": base_url, "inf": inf, "fmt": None, "is_audio": False}
            if want_soft or want_burn:
                job["sub_lang"] = fa_lang or en_lang or "fa"
                job["translate_fa"] = not bool(fa_lang)
                job["soft_subs"] = want_soft
                job["burn_subs"] = want_burn
            if want_compress:
                job["compress"] = True
            if base_url and dl.is_instagram_url(base_url):
                job["send_to_telegram"] = True
                job["ig_kind"] = "reel"
            jobs.append(job)
        try:
            await q.message.delete()
        except Exception:
            pass
        only_video = all(not j["is_audio"] for j in jobs)
        if s.get("mode") != "batch" and only_video:
            SESS[chat_id]["awaiting_clip"] = jobs
            await ctx.bot.send_message(
                chat_id,
                "✂️ بازه زمانی می‌خوای؟\n"
                "مثال بفرست: `5:00-12:30` یا `90-180`\n"
                "یا دکمه زیر رو بزن:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 کل ویدیو", callback_data="clip:full")],
                ]),
            )
            return
        if s.get("mode") == "batch":
            s.setdefault("jobs", []).extend(jobs)
            s["idx"] = s.get("idx", 0) + 1
            await process_next_in_batch(update, ctx)
        else:
            for job in jobs:
                await finalize_job(update, ctx, job)
        return

    # تک‌ضربه از منوی اینستا / کیفیت سریع (بهترین / صدا)
    if data.startswith("q:"):
        if not s:
            await q.answer("منقضی شده — لینک‌ها رو دوباره بفرست", show_alert=True)
            return
        sel = data[2:]
        cur = s.get("_cur") or {}
        base_url = cur.get("url") or s.get("url")
        inf = cur.get("inf") or s.get("inf")
        if not base_url:
            await q.answer("❌ لینک نیست", show_alert=True)
            return
        job = {"is_audio": False, "fmt": None, "inf": inf, "url": base_url}
        if sel == "best":
            job["fmt"] = None
        elif sel == "audio":
            job["is_audio"] = True
            job["fmt"] = None
        else:
            job["fmt"] = sel
        is_ig = bool(base_url and dl.is_instagram_url(base_url))
        if is_ig:
            job["send_to_telegram"] = True
            job["ig_kind"] = "song" if job["is_audio"] else "reel"
        try:
            await q.message.delete()
        except Exception:
            pass
        # batch یا اینستا → بدون فصل/کلیپ/زیرنویس، مستقیم صف/دانلود
        if s.get("mode") == "batch" or is_ig:
            await finalize_job(update, ctx, job)
            return
        if not job["is_audio"]:
            chs = get_chapters(inf or {})
            if chs:
                SESS[chat_id]["_pending_job"] = job
                SESS[chat_id]["chapters"] = chs
                SESS[chat_id]["ch_selected"] = set()
                await ctx.bot.send_message(
                    chat_id,
                    f"📑 این ویدیو {len(chs)} فصل داره.\n"
                    "فصل‌هایی که می‌خوای رو انتخاب کن (چندتا می‌تونی):\n"
                    "یا «بدون فصل» برای کل ویدیو / تایم دستی.",
                    reply_markup=InlineKeyboardMarkup(build_chapters_kb(chs, set())),
                )
                return
            SESS[chat_id]["awaiting_clip"] = [job]
            await ctx.bot.send_message(
                chat_id,
                "✂️ بازه زمانی می‌خوای؟\n"
                "مثال بفرست: `5:00-12:30` یا `90-180`\n"
                "یا دکمه زیر رو بزن:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 کل ویدیو", callback_data="clip:full")],
                ]),
            )
            return
        SESS[chat_id]["_pending_job"] = job
        await ctx.bot.send_message(
            chat_id,
            "💬 زیرنویس چطور باشه؟",
            reply_markup=build_subs_kb(inf or {}),
        )
        return

    # ─── انتخاب از پلی‌لیست ───
    if data.startswith("pl:"):
        action = data[3:]
        items = s.get("pl_items") or []
        selected = s.setdefault("pl_selected", set())
        if action == "all":
            selected.clear()
            selected.update(range(len(items)))
        elif action == "none":
            selected.clear()
        elif action == "go":
            if not selected:
                await q.answer("حداقل یکی رو انتخاب کن", show_alert=True)
                return
            links = [items[i]["url"] for i in sorted(selected) if i < len(items)]
            SESS[chat_id] = {"mode": "batch", "links": links, "idx": 0, "jobs": [], "selected_q": set()}
            try:
                await q.message.delete()
            except Exception:
                pass
            await process_next_in_batch(update, ctx)
            return
        else:
            try:
                idx = int(action)
            except ValueError:
                return
            if idx in selected:
                selected.discard(idx)
            else:
                selected.add(idx)
        try:
            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(_build_playlist_kb(items, selected))
            )
        except Exception:
            pass
        return

    # ─── اولویت صف ───
    if data.startswith("prio:"):
        action = data[5:]
        jobs = s.get("jobs") or []
        order = s.setdefault("prio_order", [])
        if action == "reset":
            order.clear()
        elif action == "noop":
            await q.answer("روی ویدیوها بزن تا اولویت بدی")
            return
        elif action == "start":
            if len(order) < len(jobs):
                await q.answer("همه رو اولویت‌بندی کن", show_alert=True)
                return
            ordered = [jobs[i] for i in order]
            try:
                await q.message.delete()
            except Exception:
                pass
            await ctx.bot.send_message(
                chat_id,
                f"🚀 شروع دانلود {len(ordered)} فایل\n"
                f"⏱ زمان کل صف روی نوار پیشرفت با سرعت واقعی می‌آد.",
            )
            SESS.pop(chat_id, None)
            await run_job_queue(update, ctx, ordered)
            return
        else:
            try:
                idx = int(action)
            except ValueError:
                return
            if idx in order:
                order.remove(idx)
            else:
                order.append(idx)
        try:
            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(_build_priority_kb(jobs, order))
            )
        except Exception:
            pass
        return

    # ─── Instagram: پیدا کردن آهنگ کامل ───
    if data == "ig_audio":
        if not s:
            await ctx.bot.send_message(chat_id, "منقضی شده — لینک‌ها رو دوباره بفرست")
            return
        # در batch اطلاعات تو _cur است، در تک‌لینک تو s
        cur = s.get("_cur") or {}
        inf = cur.get("inf") or s.get("inf")
        url = cur.get("url") or s.get("url")
        if not inf or not url:
            await ctx.bot.send_message(chat_id, "❌ اطلاعات ویدیو نیست — دوباره لینک بفرست")
            return

        try:
            await q.message.delete()
        except Exception:
            pass

        # چند لینک: فقط تو صف بذار — الان دانلود/تشخیص نکن
        if s.get("mode") == "batch":
            job = {
                "url": url,
                "inf": inf,
                "is_audio": True,
                "ig_extract_song": True,
                "send_to_telegram": True,
                "ig_kind": "song",
            }
            try:
                save_link_pref(url, job)
            except Exception:
                pass
            s.setdefault("jobs", []).append(job)
            n = s.get("idx", 0) + 1
            total = len(s.get("links") or [])
            await tg_call(
                ctx.bot.send_message,
                chat_id,
                f"✅ استخراج آهنگ به صف اضافه شد ({n}/{total})\n"
                f"بعد از انتخاب همه‌ی لینک‌ها، یکی‌یکی دانلود می‌شه.",
            )
            s["idx"] = n
            await process_next_in_batch(update, ctx)
            return

        # تک‌لینک: همان لحظه تشخیص و دانلود
        await execute_ig_song_extract(update, ctx, url, inf)
        return

    # ─── Instagram: شناسایی فیلم با جستجوی تصویری واقعی گوگل لنز (SerpApi) ───
    if data == "ig_movie":
        cur = s.get("_cur") or {}
        inf = cur.get("inf") or s.get("inf")
        url = cur.get("url") or s.get("url")
        if not inf or not url:
            await ctx.bot.send_message(chat_id, "❌ اطلاعات ویدیو نیست — دوباره لینک بفرست")
            return
        if not MOVIE_OK:
            await ctx.bot.send_message(chat_id, "❌ ماژول فیلم‌یاب لود نشده — لاگ ربات رو چک کن.")
            return

        _key = (chat_id, url)
        if _key in _MOVIE_BUSY:
            await q.answer("⏳ همین الان دارم روش کار می‌کنم — صبر کن ☕")
            return

        await q.answer("🎬 در حال آماده‌سازی...")
        try:
            await q.message.delete()
        except Exception:
            pass
        wait_msg = await ctx.bot.send_message(chat_id, "🔎 لینک دریافت شد؛ در حال شروع تشخیص…")

        async def _mv_status(text):
            try:
                await wait_msg.edit_text(text)
            except Exception:
                pass

        _MOVIE_BUSY[_key] = True
        video_path = None
        frames = []
        try:
            # ۱) دانلود ریلز — دقیقاً همان پایپ‌لاین movie_bot
            video_path = await mv_download_reel(url, dest_dir=None, status_cb=_mv_status)
            if not video_path:
                await _mv_status("❌ دانلود ناموفق بود. لینک را چک کن.")
                return

            # ۲) استخراج فریم (مؤقت؛ بعد از تشخیص پاک می‌شود)
            await _mv_status("🖼 دارم فریم‌های کلیدی رو در می‌آرم...")
            frames = await asyncio.to_thread(mv_extract_frames, Path(video_path), MV_IMAGES_DIR)
            if not frames:
                await _mv_status("❌ فریمی استخراج نشد.")
                return
            # __MV_TAIL__

            await _mv_status(f"🤖 {len(frames)} فریم رفت برای شناسایی با هوش مصنوعی...")
            try:
                detection = await mv_identify_from_frames(frames)
                print(f"[movie] تشخیص: {detection}", flush=True)
            except MvQuotaExceededError:
                await _mv_status(
                    "⚠️ سهمیهٔ رایگان Gemini پر شده — کمی بعد دوباره امتحان کن."
                )
                return
            except Exception as e:
                print(f"[movie] شناسایی ناموفق: {type(e).__name__}: {e}", flush=True)
                await _mv_status("❌ شناسایی فیلم شکست خورد.")
                return
            if not detection:
                await _mv_status("❌ نتونستم فیلم رو تشخیص بدم — یه ریلز واضح‌تر بفرست.")
                return

            await _mv_status("📚 دارم اطلاعات فیلم رو از TMDb می‌گیرم...")
            try:
                info = await mv_build_movie_info(detection, url)
            except Exception as e:
                print(f"[movie] ساخت کارت ناموفق: {type(e).__name__}: {e}", flush=True)
                info = None
            if not info:
                await _mv_status("❌ اطلاعات این فیلم تو دیتابیس پیدا نشد.")
                return

            try:
                await wait_msg.delete()
            except Exception:
                pass
            await mv_send_movie_card(ctx, chat_id, info)
        finally:
            _MOVIE_BUSY.pop(_key, None)
            for p in frames:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
            if video_path:
                try:
                    Path(video_path).unlink(missing_ok=True)
                except Exception:
                    pass
        return

    # ─── رد کردن نتیجهٔ شناسایی و تلاش دوباره با دقت بالاتر ───
    if data == "mv_retry":
        url = s.get("_cur", {}).get("url") or s.get("url")
        if not url:
            await q.answer("❌ لینک ویدیو پیدا نشد — دوباره بفرست", show_alert=True)
            return
        if not MOVIE_OK:
            await q.answer("❌ ماژول فیلم‌یاب لود نشده", show_alert=True)
            return

        _key = (chat_id, url)
        if _key in _MOVIE_BUSY:
            await q.answer("⏳ همین الان دارم روش کار می‌کنم — صبر کن ☕")
            return

        await q.answer("🔁 دوباره با دقت بالاتر می‌گردم...")
        try:
            await q.message.delete()
        except Exception:
            pass
        wait_msg = await ctx.bot.send_message(
            chat_id, "🔁 در حال جستجوی دقیق‌تر — این بار با تمرکز روی گزینه‌های جایگزین…"
        )

        async def _mv_status2(text):
            try:
                await wait_msg.edit_text(text)
            except Exception:
                pass

        _MOVIE_BUSY[_key] = True
        video_path = None
        frames = []
        try:
            video_path = await mv_download_reel(url, dest_dir=None, status_cb=_mv_status2)
            if not video_path:
                await _mv_status2("❌ دانلود ناموفق بود. لینک را چک کن.")
                return

            await _mv_status2("🖼 فریم‌های بیشتری (با پوشش کامل‌تر) در می‌آرم…")
            # در حالت دقت بالا فریم‌های بیشتری می‌گیریم
            _HIGH_PCT = tuple(i * (100.0 / 20) for i in range(1, 20))
            frames = await asyncio.to_thread(
                mv_extract_frames, Path(video_path), MV_IMAGES_DIR, _HIGH_PCT
            )
            if not frames:
                await _mv_status2("❌ فریمی استخراج نشد.")
                return

            await _mv_status2(f"🤖 {len(frames)} فریم (دقت بالا) رفت برای شناسایی…")
            try:
                detection = await mv_identify_from_frames(frames, precision="high")
            except MvQuotaExceededError:
                await _mv_status2("⚠️ سهمیهٔ رایگان Gemini پر شده — کمی بعد دوباره امتحان کن.")
                return
            except Exception as e:
                print(f"[movie-retry] شناسایی ناموفق: {type(e).__name__}: {e}", flush=True)
                await _mv_status2("❌ شناسایی فیلم شکست خورد.")
                return
            if not detection:
                await _mv_status2("❌ باز هم نتونستم فیلم رو تشخیص بدم — یه ریلز واضح‌تر بفرست.")
                return

            await _mv_status2("📚 اطلاعات فیلم رو از TMDb می‌گیرم…")
            try:
                info = await mv_build_movie_info(detection, url)
            except Exception as e:
                print(f"[movie-retry] ساخت کارت ناموفق: {type(e).__name__}: {e}", flush=True)
                info = None
            if not info:
                await _mv_status2("❌ اطلاعات این فیلم تو دیتابیس پیدا نشد.")
                return

            try:
                await wait_msg.delete()
            except Exception:
                pass
            await mv_send_movie_card(ctx, chat_id, info)
        finally:
            _MOVIE_BUSY.pop(_key, None)
            for p in frames:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
            if video_path:
                try:
                    Path(video_path).unlink(missing_ok=True)
                except Exception:
                    pass
        return

    if data == "ig_video_menu":
        if not s:
            await q.answer("منقضی شده", show_alert=True)
            return
        cur = s.get("_cur") or {}
        inf = cur.get("inf") or s.get("inf")
        if not inf:
            await q.answer("❌ اطلاعات ویدیو نیست", show_alert=True)
            return
        s["selected_q"] = set()
        kb, _ = build_quality_kb(inf, False, set(), batch=(s.get("mode") == "batch"))
        try:
            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await ctx.bot.send_message(
                chat_id,
                "⭐ کیفیت ویدیو رو انتخاب کن:",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        return

    if data.startswith("igms:"):
        if not s:
            await q.answer("منقضی شده", show_alert=True)
            return
        sel = data[5:]
        if sel == "cancel":
            try:
                await q.message.delete()
            except Exception:
                pass
            # در batch فقط این یکی رو رد کن، بقیه بمونن
            if s.get("mode") == "batch":
                s["idx"] = s.get("idx", 0) + 1
                await process_next_in_batch(update, ctx)
            else:
                SESS.pop(chat_id, None)
            return
        results = s.get("_ig_music_results") or []
        try:
            idx = int(sel)
        except ValueError:
            return
        if idx >= len(results):
            return
        chosen = results[idx]
        chosen_url = chosen.get("url") or chosen.get("webpage_url") or chosen.get("id")
        if not chosen_url:
            await q.answer("❌ لینک این نتیجه پیدا نشد", show_alert=True)
            return
        await q.answer("🎵 دانلود آهنگ...")
        try:
            await q.message.delete()
        except Exception:
            pass
        job = await _job_from_music_pick(chat_id, ctx, chosen)
        if s.get("mode") == "batch":
            # مثل یوتیوب: بره تو صف جاب‌ها و لینک بعدی
            s.setdefault("jobs", []).append(job)
            s["idx"] = s.get("idx", 0) + 1
            await process_next_in_batch(update, ctx)
        else:
            await run_single_job(update, ctx, job)
            await send_spotify_link_if_available(chat_id, ctx, chosen.get("title") or "")
        return

    if data == "music_search":
        if not s:
            await ctx.bot.send_message(chat_id, "منقضی شده — لینک رو دوباره بفرست")
            return
        cur = s.get("_cur") or {}
        inf = cur.get("inf") or s.get("inf")
        url = cur.get("url") or s.get("url")
        if not inf or not url:
            await ctx.bot.send_message(chat_id, "❌ اطلاعات ویدیو نیست — دوباره لینک بفرست")
            return
        search_query = extract_song_query(inf)
        if search_query:
            await q.answer("🔍 در حال جستجو...")
            await search_and_send_music_options(chat_id, ctx, search_query, cb_prefix="ms", result_key="_music_results", update=update, auto_pick=True)
            return

        _key = (chat_id, url)
        if _key in _SONG_BUSY:
            await q.answer("⏳ همین الان دارم روش کار می‌کنم — صبر کن ☕")
            return
        await q.answer("🎧 دارم تشخیص می‌دم...")
        wait_msg = await ctx.bot.send_message(chat_id, "🎧 در حال شنیدن صدای ویدیو...")

        async def _status(text):
            try:
                await wait_msg.edit_text(text)
            except Exception:
                pass

        _SONG_BUSY[_key] = True
        try:
            search_query, audd_artist, audd_title, src, confident = await identify_song_from_url(url, on_status=_status)
        finally:
            _SONG_BUSY.pop(_key, None)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        if search_query:
            await search_and_send_music_options(
                chat_id, ctx, search_query, cb_prefix="ms",
                result_key="_music_results", update=update,
                auto_pick=confident,
                artist=audd_artist, title=audd_title,
            )
            return
        SESS.setdefault(chat_id, {})["awaiting_song_query"] = "ms"
        await ctx.bot.send_message(
            chat_id,
            "🎵 نه از اثر انگشت صدا و نه از روی متن/شعر نتونستم آهنگ رو پیدا کنم.\n\n"
            "اگه اسم آهنگ یا چند کلمه از شعرش رو می‌دونی بفرست تا جستجو کنم 🔍",
        )
        return

    if data.startswith("ms:"):
        sel = data[3:]
        if sel == "cancel":
            try:
                await q.message.delete()
            except Exception:
                pass
            SESS.pop(chat_id, None)
            return
        results = s.get("_music_results") or []
        idx = int(sel)
        if idx >= len(results):
            return
        chosen = results[idx]
        chosen_url = chosen.get("url") or chosen.get("webpage_url") or chosen.get("id")
        if not chosen_url:
            await q.answer("❌ لینک پیدا نشد", show_alert=True)
            return
        if "http" not in str(chosen_url):
            chosen_url = f"https://www.youtube.com/watch?v={chosen_url}"
        await q.answer("🎵 در حال دانلود موزیک...")
        try:
            await q.message.delete()
        except Exception:
            pass
        job = await _job_from_music_pick(chat_id, ctx, chosen)
        sess = SESS.get(chat_id) or {}
        if sess.get("mode") == "batch":
            sess.setdefault("jobs", []).append(job)
            sess["idx"] = sess.get("idx", 0) + 1
            await process_next_in_batch(update, ctx)
        else:
            await run_single_job(update, ctx, job)
            await send_spotify_link_if_available(chat_id, ctx, chosen.get("title") or "")
        return

    if data.startswith("sub:"):
        job = s.pop("_pending_job", None)
        if not job:
            return
        if data == "sub:yes":
            cur = s.get("_cur") or {}
            inf_src = cur.get("inf") or s.get("inf") or {}
            langs = sorted(set(list((inf_src.get("subtitles") or {}).keys()) +
                               list((inf_src.get("automatic_captions") or {}).keys())))
            kb = [[InlineKeyboardButton(l, callback_data=f"sublang:{l}")] for l in langs[:12]]
            await q.edit_message_text("زبان زیرنویس را انتخاب کن:",
                                      reply_markup=InlineKeyboardMarkup(kb))
            return
        else:
            await finalize_job(update, ctx, job, del_msg=q.message)
            return

    if data.startswith("sublang:"):
        job = s.pop("_pending_job", None)
        if job:
            job["sub_lang"] = data.split(":", 1)[1]
            await finalize_job(update, ctx, job, del_msg=q.message)
        return

    # ─── زیرنویس فارسی ───
    if data.startswith("subx:"):
        job = s.pop("_pending_job", None)
        if not job:
            await q.answer("منقضی شده", show_alert=True)
            return
        mode = data.split(":", 1)[1]
        inf_src = job.get("inf") or s.get("inf") or {}
        auto = (inf_src.get("automatic_captions") or {})
        man = (inf_src.get("subtitles") or {})
        all_langs = list(man.keys()) + list(auto.keys())

        def _pick_lang(prefix):
            for L in all_langs:
                if L.startswith(prefix):
                    return L
            return None

        if mode == "no":
            job.pop("sub_lang", None)
            job.pop("burn_subs", None)
            job.pop("translate_fa", None)
        elif mode in ("fa_soft", "soft", "smart_fa"):
            # همیشه اول فارسی
            job["sub_lang"] = _pick_lang("fa") or "fa"
            job["burn_subs"] = False
            job["translate_fa"] = not bool(_pick_lang("fa"))  # اگه fa نبود بعداً از en ترجمه
            if not _pick_lang("fa") and _pick_lang("en"):
                job["sub_lang"] = _pick_lang("en")
                job["translate_fa"] = True
            await q.answer("🇮🇷 زیرنویس فارسی (نرم)", show_alert=False)
        elif mode in ("fa_burn", "burn"):
            job["sub_lang"] = _pick_lang("fa") or "fa"
            job["burn_subs"] = True
            job["translate_fa"] = not bool(_pick_lang("fa"))
            if not _pick_lang("fa") and _pick_lang("en"):
                job["sub_lang"] = _pick_lang("en")
                job["translate_fa"] = True
            await q.answer("🔥 فارسی چسبیده روی ویدیو", show_alert=False)
        elif mode == "smart_fa_tr":
            job["sub_lang"] = _pick_lang("en") or "en"
            job["burn_subs"] = True
            job["translate_fa"] = True
            await q.answer("در حال ترجمه انگلیسی → فارسی...", show_alert=False)
        await finalize_job(update, ctx, job, del_msg=q.message)
        return

    # ─── فصل‌های یوتیوب ───
    if data.startswith("ch:") or data == "show_chapters":
        if data == "show_chapters":
            inf = (s.get("_cur") or {}).get("inf") or s.get("inf") or {}
            chs = get_chapters(inf)
            if not chs:
                await q.answer("فصلی نداره", show_alert=True)
                return
            SESS[chat_id]["chapters"] = chs
            SESS[chat_id]["ch_selected"] = set()
            # job هنوز از کیفیت نیومده — فقط نمایش
            await q.edit_message_text(
                f"📑 {len(chs)} فصل:\n(بعد از انتخاب کیفیت می‌تونی فصل دانلود کنی)",
                reply_markup=InlineKeyboardMarkup(build_chapters_kb(chs, set())),
            )
            return
        action = data[3:]
        chs = s.get("chapters") or []
        selected = s.setdefault("ch_selected", set())
        if action == "all":
            selected.clear()
            selected.update(range(len(chs)))
        elif action == "none":
            selected.clear()
        elif action == "go":
            job = s.pop("_pending_job", None)
            if not job:
                await q.answer("منقضی", show_alert=True)
                return
            if selected:
                # هر فصل یک جاب جدا
                try:
                    await q.message.delete()
                except Exception:
                    pass
                ordered = sorted(selected)
                await ctx.bot.send_message(chat_id, f"🚀 دانلود {len(ordered)} فصل...")
                for i in ordered:
                    if i >= len(chs):
                        continue
                    c = chs[i]
                    j = dict(job)
                    j["section"] = (c["start"], c["end"])
                    j["chapter_title"] = c["title"]
                    await run_single_job(update, ctx, j)
                SESS.pop(chat_id, None)
                return
            # بدون فصل → بازه دستی یا زیرنویس
            try:
                await q.message.delete()
            except Exception:
                pass
            SESS[chat_id]["awaiting_clip"] = [job]
            await ctx.bot.send_message(
                chat_id,
                "✂️ بازه زمانی می‌خوای؟\nمثال: `5:00-12:30`\nیا کل ویدیو:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 کل ویدیو", callback_data="clip:full")],
                ]),
            )
            return
        else:
            try:
                idx = int(action)
            except ValueError:
                return
            if idx in selected:
                selected.remove(idx)
            else:
                selected.add(idx)
        try:
            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(build_chapters_kb(chs, selected))
            )
        except Exception:
            pass
        return

    # ─── تکراری ───
    if data.startswith("dup:"):
        action = data.split(":")[1]
        if action == "cancel":
            SESS.pop(chat_id, None)
            try:
                await q.message.delete()
            except Exception:
                pass
            return
        mid = data.split(":")[2] if len(data.split(":")) > 2 else s.get("media_id")
        dups = s.get("dups") or find_duplicates(mid)
        if action == "open":
            if not dups:
                await q.answer("فایل پیدا نشد", show_alert=True)
                return
            p = dups[0].get("path")
            if p and os.path.exists(p):
                try:
                    with open(p, "rb") as f:
                        if p.lower().endswith((".mp3", ".m4a", ".ogg")):
                            await ctx.bot.send_audio(chat_id, audio=f, caption="📂 از فایل قبلی")
                        else:
                            await ctx.bot.send_document(chat_id, document=f, caption="📂 از فایل قبلی")
                except Exception as e:
                    await q.answer(str(e)[:100], show_alert=True)
            else:
                await q.answer("مسیر فایل نیست", show_alert=True)
            return
        if action == "share":
            if not dups or not dups[0].get("path"):
                await q.answer("فایل برای شیر نیست", show_alert=True)
                return
            SESS[chat_id] = {
                "share_pick": {
                    "path": dups[0]["path"],
                    "title": dups[0].get("title") or "فایل",
                    "kind": "file",
                }
            }
            await ctx.bot.send_message(chat_id, "تعداد کاربر لینک؟", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1", callback_data="shareuses:1"),
                 InlineKeyboardButton("2", callback_data="shareuses:2"),
                 InlineKeyboardButton("3", callback_data="shareuses:3"),
                 InlineKeyboardButton("5", callback_data="shareuses:5")],
                [InlineKeyboardButton("دلخواه", callback_data="shareuses:custom")],
            ]))
            return
        if action == "redownload":
            url = s.get("url")
            inf = s.get("inf")
            if not url:
                return
            SESS[chat_id] = {"mode": "single", "url": url, "inf": inf, "selected_q": set()}
            kb, _ = build_quality_kb(inf, False, set())
            try:
                await q.edit_message_text(
                    build_meta_text(inf) + "\n\nکیفیت رو انتخاب کن:",
                    reply_markup=InlineKeyboardMarkup(kb),
                )
            except Exception:
                await ctx.bot.send_message(chat_id, build_meta_text(inf), reply_markup=InlineKeyboardMarkup(kb))
            return
        return

    # ─── خلاصه با هوش مصنوعی (AI) — دقیقا مثل خلاصه/Ask خود یوتیوب ───
    if data == "sum:video":
        if s is None:
            s = {}
            SESS[chat_id] = s
        url = (s.get("_cur") or {}).get("url") or s.get("url")
        inf = (s.get("_cur") or {}).get("inf") or s.get("inf") or {}
        if not url:
            await q.answer("لینک نیست", show_alert=True)
            return
        await q.answer("در حال ساخت خلاصه‌ی هوشمند...")
        wait = await ctx.bot.send_message(chat_id, "🤖 دارم از روی رونوشت واقعی ویدیو خلاصه‌ی هوشمند می‌سازم...")
        result, transcript, err = await ai_summarize_video(url, inf)
        if err or not result:
            # اگه AI شکست خورد (مثلاً سهمیه تموم شده)، از همون رونوشتی که قبلاً
            # گرفته شده (اگه گرفته شده باشه) خلاصه‌ی ساده بساز — بدون درخواست
            # دوباره به یوتیوب (که ریسک ۴۲۹ رو دوبرابر می‌کنه)
            simple = _simple_summary_from_text(transcript) if transcript else None
            if simple:
                await wait.edit_text(
                    f"⚠️ خلاصه‌ی AI ممکن نشد ({err or 'خطا'})؛ خلاصه‌ی ساده:\n\n{simple}"
                )
            else:
                await wait.edit_text(f"❌ خلاصه نشد: {err or 'رونوشت نبود'}")
            return
        s["_ai_ctx"] = {
            "transcript": transcript,
            "title": result.get("title") or "",
            "questions": result.get("questions") or [],
        }
        text = f"🤖 خلاصه‌ی هوشمند ویدیو:\n\n{result['summary']}"
        if result.get("key_points"):
            pts = "\n".join(f"• {p}" for p in result["key_points"])
            text += f"\n\n📌 نکات کلیدی:\n{pts}"
        kb = []
        for i, ques in enumerate(result.get("questions") or []):
            label = ques if len(ques) <= 60 else ques[:57] + "…"
            kb.append([InlineKeyboardButton(f"❓ {label}", callback_data=f"sumq:{i}")])
        kb.append([InlineKeyboardButton("✍️ سوال دلخواه بپرس", callback_data="sumq:custom")])
        kb.append([InlineKeyboardButton("🔁 دوباره خلاصه کن", callback_data="sum:video")])
        await wait.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        return

    # ─── جواب به یکی از سوال‌های پیشنهادی زیر خلاصه‌ی AI ───
    if data.startswith("sumq:"):
        if s is None:
            await q.answer("این خلاصه منقضی شده، دوباره بزن رو «خلاصه با هوش مصنوعی».", show_alert=True)
            return
        ctxinfo = s.get("_ai_ctx") or {}
        arg = data.split(":", 1)[1]
        if arg == "custom":
            await q.answer()
            s["awaiting_ai_question"] = True
            await ctx.bot.send_message(chat_id, "✍️ سوالت درباره‌ی این ویدیو رو بنویس و بفرست:")
            return
        try:
            idx = int(arg)
            question = (ctxinfo.get("questions") or [])[idx]
        except (ValueError, IndexError):
            await q.answer("این سوال دیگه در دسترس نیست.", show_alert=True)
            return
        if not ctxinfo.get("transcript"):
            await q.answer("رونوشت این ویدیو دیگه در دسترس نیست.", show_alert=True)
            return
        await q.answer("در حال پاسخ...")
        wait = await ctx.bot.send_message(chat_id, f"❓ {question}\n\n🤖 در حال فکر کردن...")
        answer, err = await ai_answer_question(question, ctxinfo.get("transcript"), ctxinfo.get("title"))
        if err or not answer:
            await wait.edit_text(f"❌ جواب داده نشد: {err or 'خطا'}")
        else:
            await wait.edit_text(f"❓ {question}\n\n🤖 {answer}")
        return

    # ─── کتابخانه ───
    if data.startswith("lib:"):
        action = data.split(":", 1)[1]
        if action.startswith("send:"):
            idx = int(action.split(":")[1])
            items = s.get("lib_items") or []
            if idx < len(items):
                p = items[idx].get("path")
                if p and os.path.exists(p):
                    try:
                        with open(p, "rb") as f:
                            if p.lower().endswith((".mp3", ".m4a", ".ogg", ".flac")):
                                await ctx.bot.send_audio(chat_id, audio=f)
                            else:
                                await ctx.bot.send_document(chat_id, document=f)
                    except Exception as e:
                        await q.answer(str(e)[:120], show_alert=True)
                else:
                    await q.answer("فایل روی گوشی نیست", show_alert=True)
        return

    if data.startswith("cancel:"):
        cid = data[len("cancel:"):]
        ev = CANCEL_EVENTS.get(cid)
        if ev:
            ev.set()
            await q.edit_message_text("🚫 در حال لغو...")
        return


async def finalize_job(update, ctx, job, del_msg=None):
    chat_id = update.effective_chat.id
    # حذف خودکار پيام «زيرنويس بذارم؟»
    if del_msg:
        try:
            await del_msg.delete()
        except Exception:
            pass
    # ذخیره انتخاب برای دفعات بعد
    try:
        if job.get("url"):
            save_link_pref(job["url"], job)
    except Exception as e:
        print(f"[prefs] {e}", flush=True)
    s = SESS.get(chat_id)
    if s and s.get("mode") == "batch":
        s["jobs"].append(job)
        s["idx"] += 1
        await process_next_in_batch(update, ctx)
    else:
        # تكي
        SESS.pop(chat_id, None)
        await run_single_job(update, ctx, job)


async def handle_voice(update, ctx):
    """ویس → AudD → جستجو و دانلود آهنگ."""
    chat_id = update.effective_chat.id
    token = getattr(cfg, "AUDD_API_TOKEN", None)
    if not token:
        await update.message.reply_text(
            "❌ توکن AudD تنظیم نیست.\n"
            "AUDD_API_TOKEN رو تو bridge_config.py بذار."
        )
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    wait = await update.message.reply_text("🎤 در حال تشخیص آهنگ از ویس...")
    tmp = os.path.join(
        getattr(cfg, "DOWNLOAD_DIR", getattr(dl, "DOWNLOAD_DIR", "/tmp")),
        f"voice_{chat_id}_{int(datetime.now().timestamp())}.ogg",
    )
    try:
        tg_file = await voice.get_file()
        await tg_file.download_to_drive(tmp)
        # AudD
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("api_token", token)
        with open(tmp, "rb") as f:
            form.add_field("file", f, filename="voice.ogg", content_type="audio/ogg")
            async with aiohttp.ClientSession(trust_env=True) as sess:
                async with sess.post(
                    "https://api.audd.io/",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
        result = (data or {}).get("result")
        if not result or not (result.get("title") or "").strip():
            await wait.edit_text("❌ آهنگی تشخیص داده نشد. ویس واضح‌تر بفرست.")
            return
        title = (result.get("title") or "").strip()
        artist = (result.get("artist") or "").strip()
        query = f"{artist} {title}".strip() if artist else title
        await wait.edit_text(f"🎵 پیدا شد: {query}\n🔍 در حال جستجو...")
        await search_and_send_music_options(
            chat_id, ctx, query,
            cb_prefix="ms", result_key="_music_results",
            update=update, auto_pick=True,
        )
    except Exception as e:
        await wait.edit_text(f"❌ خطا در تشخیص: {e}")
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


XRAY_CONFIG_PATH = os.path.join(
    getattr(dl, "SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__))),
    "xray_tunnel_config.json",
)
_xray_process = None


def _port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start_internal_tunnel():
    """
    به‌جای وابستگی به اپ v2box (که باید دستی روشن باشه)، خود ربات با xray-core
    و یکی از کانفیگ‌های ذخیره‌شده (با انتخاب خودکار کمترین پینگ بین چند سرور
    و fallback خودکار) یه تونل محلی رو پورت 10810 می‌سازه. اگه از قبل چیزی رو
    این پورت گوش می‌ده (مثلاً خود v2box دستی روشنه)، دست بهش نمی‌زنیم.
    """
    global _xray_process
    if _port_open("127.0.0.1", 10810):
        print("[tunnel] یه چیزی از قبل رو پورت 10810 هست (احتمالاً v2box) - همون استفاده می‌شه.", flush=True)
        return True

    xray_bin = shutil.which("xray")
    if not xray_bin:
        print(
            "[tunnel] باینری xray پیدا نشد. یا v2box رو دستی روشن کن، یا xray رو نصب کن "
            "(pkg install xray یا از github.com/XTLS/Xray-core دانلود کن).",
            flush=True,
        )
        return False
    if not os.path.exists(XRAY_CONFIG_PATH):
        print(f"[tunnel] فایل کانفیگ پیدا نشد: {XRAY_CONFIG_PATH}", flush=True)
        return False

    try:
        _xray_process = subprocess.Popen(
            [xray_bin, "run", "-c", XRAY_CONFIG_PATH],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[tunnel] اجرای xray ناموفق: {e}", flush=True)
        return False

    for _ in range(20):
        if _port_open("127.0.0.1", 10810):
            print("[tunnel] تونل داخلی xray بالا اومد (127.0.0.1:10810).", flush=True)
            return True
        time.sleep(0.5)
    print("[tunnel] بعد از ۱۰ ثانیه تونل هنوز آماده نشد؛ همه‌ی سرورهای داخل کانفیگ شاید خراب/بلاک باشن.", flush=True)
    return False


def stop_internal_tunnel():
    global _xray_process
    if _xray_process:
        try:
            _xray_process.terminate()
        except Exception:
            pass
        _xray_process = None


def _sweep_stale_downloads():
    """جاروی شروع ربات: فایل‌های قدیمی پوشه دانلود پاک می‌شن تا volume پر نشه.

    فایل‌های کاتالوگ اینستا (برای لینک‌های اشتراک) دست نمی‌خورن؛ بقیه
    (یوتیوب و...) که از CLEANUP_AFTER_HOURS ساعت قدیمی‌ترن حذف می‌شن.
    """
    try:
        max_age = float(getattr(cfg, "CLEANUP_AFTER_HOURS", 6)) * 3600
    except (TypeError, ValueError):
        max_age = 6 * 3600
    if max_age <= 0:
        return
    try:
        keep = set()
        for e in load_ig_catalog():
            p = (e or {}).get("path")
            if p:
                keep.add(os.path.abspath(p))
        ddir = getattr(cfg, "DOWNLOAD_DIR", None) or os.path.join(
            getattr(cfg, "BASE_DIR", "/app"), "downloads")
        now = time.time()
        n, freed = 0, 0
        for root, _dirs, files in os.walk(ddir):
            for fn in files:
                fp = os.path.abspath(os.path.join(root, fn))
                if fp in keep:
                    continue
                try:
                    if now - os.path.getmtime(fp) < max_age:
                        continue
                    freed += os.path.getsize(fp)
                    os.remove(fp)
                    n += 1
                except Exception:
                    pass
        if n:
            print(f"[sweep] {n} فایل قدیمی پاک شد ({freed/1048576:.0f}MB آزاد شد).", flush=True)
    except Exception as exc:
        print(f"[sweep] خطا: {exc}", flush=True)


def main():
    # نسخه مستقیم: بدون تونل داخلی xray
    # ترافیک از VPN/فیلترشکن سیستم گوشی رد می‌شه
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(k, None)

    _sweep_stale_downloads()

    def build_app():
        from telegram.request import HTTPXRequest
        # حالت سرور محلی Bot API (آپلود یک‌جای تا ~۲ گیگ): تایم‌اوت‌ها بلندتر.
        _local = bool(getattr(cfg, "TELEGRAM_LOCAL_API", False))
        _big = 1800.0 if _local else 15.0
        # تایم‌اوت بلندتر برای شبکه ضعیف ایران
        req = HTTPXRequest(
            connect_timeout=15.0,
            read_timeout=_big,
            write_timeout=_big,
            pool_timeout=15.0,
        )
        b = (
            Application.builder()
            .token(cfg.BOT_TOKEN)
            .request(req)
            .get_updates_request(req)
            .concurrent_updates(True)
        )
        if _local:
            _base = str(getattr(cfg, "LOCAL_API_URL", "http://127.0.0.1:8081")).rstrip("/")
            b = b.base_url(f"{_base}/bot").base_file_url(f"{_base}/file/bot")
        a = b.build()
        owner_f = filters.User(user_id=list(cfg.OWNER_ID))
        a.add_handler(CommandHandler("start", start_cmd))
        a.add_handler(CallbackQueryHandler(button))
        a.add_handler(MessageHandler((filters.VOICE | filters.AUDIO) & owner_f, handle_voice))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & owner_f, handle_link))

        # غیر از مالک: فقط جوابِ «سیکتیر»
        # استثنا: کسی که لینک اشتراک رو باز کرده و منتظر رمزه — باید رمزش خونده بشه
        # و فایل براش بره، وگرنه لینک اشتراک برای غیرِ مالک همیشه به «سیکتیر» می‌خورد.
        async def stranger_gate(update, ctx):
            if not update.message:
                return
            chat_id = update.effective_chat.id
            s0 = SESS.get(chat_id)
            if s0 and s0.get("mode") == "share_pw" and (update.message.text or "").strip():
                sid = s0.get("share_id")
                SESS.pop(chat_id, None)
                try:
                    await deliver_share(update, ctx, sid,
                                        password=update.message.text.strip())
                except Exception as e:
                    print(f"[share] تحویل به غریبه ناموفق: {e}", flush=True)
                return
            try:
                await update.message.reply_text("سیکتیر")
            except Exception:
                pass

        a.add_handler(MessageHandler(filters.ALL & ~owner_f, stranger_gate))

        async def on_error(update, context):
            from telegram.error import NetworkError, TimedOut, Conflict
            err = context.error
            print(f"[error-handler] {type(err).__name__}: {err}", flush=True)
            if isinstance(err, (NetworkError, TimedOut)):
                # فقط لاگ؛ tg_call و _runner دوباره تلاش می‌کنن
                return
            if isinstance(err, Conflict):
                print("[error-handler] conflict — نمونهٔ دیگری از ربات در حال اجراست", flush=True)
                return
            # بقیه خطاها
            try:
                if update and getattr(update, "effective_chat", None):
                    await context.bot.send_message(
                        update.effective_chat.id,
                        f"⚠️ خطای موقت: {type(err).__name__}\nدوباره تلاش می‌کنم...",
                    )
            except Exception:
                pass

        a.add_error_handler(on_error)
        return a

    # انتقال فایل‌های قدیمی کنار اسکریپت به bot_data ثابت
    try:
        old_dir = getattr(dl, "SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))
        mapping = {
            ".dl_history.json": HISTORY_FILE,
            ".dl_link_prefs.json": LINK_PREFS_FILE,
            ".dl_shares.json": SHARE_FILE,
            ".dl_ig_catalog.json": IG_CATALOG_FILE,
            ".dl_stats.json": STATS_FILE,
            ".dl_upload_speed.json": _upload_speed_path(),
        }
        for old_name, new_path in mapping.items():
            old_path = os.path.join(old_dir, old_name)
            if os.path.isfile(old_path) and not os.path.isfile(new_path):
                try:
                    shutil.copy2(old_path, new_path)
                    print(f"[data] منتقل شد: {old_name} → {new_path}", flush=True)
                except Exception as e:
                    print(f"[data] انتقال {old_name} ناموفق: {e}", flush=True)
    except Exception as e:
        print(f"[data] migrate: {e}", flush=True)

    print(f"ربات بریج دانلودر روشن شد (حالت مستقیم — VPN گوشی).", flush=True)
    print(f"💾 داده‌های دائمی: {DATA_DIR}", flush=True)

    async def _runner(app):
        import asyncio as _asyncio
        from telegram.error import NetworkError, TimedOut, Conflict
        while True:
            try:
                async with app:
                    await app.start()
                    try:
                        # یوزرنیم واقعی ربات رو مستقیم از تلگرام می‌گیریم — دیگه
                        # به BOT_USERNAME تو کانفیگ (که چندبار غلط تایپ شده بود) وابسته نیست
                        try:
                            me = await app.bot.get_me()
                            if me and me.username:
                                global BOT_USERNAME
                                BOT_USERNAME = me.username
                                print(f"[startup] یوزرنیم ربات: @{BOT_USERNAME}", flush=True)
                        except Exception as e:
                            print(f"[startup] get_me ناموفق، از BOT_USERNAME کانفیگ استفاده می‌شه: {e}", flush=True)
                        # delete_webhook هم با retry
                        while True:
                            try:
                                await app.bot.delete_webhook(drop_pending_updates=True)
                                break
                            except (NetworkError, TimedOut) as e:
                                print(f"[reconnect] delete_webhook: {e}", flush=True)
                                await _asyncio.sleep(5)
                        await app.updater.start_polling(
                            drop_pending_updates=True,
                            allowed_updates=["message", "callback_query"],
                            timeout=10,
                            bootstrap_retries=0,
                        )
                        while app.updater.running:
                            await _asyncio.sleep(1)
                    finally:
                        try:
                            await app.stop()
                        except Exception:
                            pass
                # polling تموم شد بدون ارور شبکه → خروج
                break
            except (NetworkError, TimedOut) as e:
                print(f"[reconnect] {e} — دوباره وصل می‌شم...", flush=True)
                await _asyncio.sleep(5)
                continue
            except Conflict as e:
                print(f"[conflict] یه نمونه‌ی دیگه از این ربات در حال اجراست: {e}", flush=True)
                await _asyncio.sleep(15)
                continue
            except Exception as e:
                print(f"[runner] {type(e).__name__}: {e} — ۵ ثانیه بعد دوباره...", flush=True)
                await _asyncio.sleep(5)
                continue

    import asyncio as _asyncio
    _asyncio.run(_runner(build_app()))


if __name__ == "__main__":
    main()
