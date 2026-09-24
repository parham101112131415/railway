#!/usr/bin/env python3
"""YOUTUBE / INSTAGRAM DOWNLOADER — VPS (Ubuntu) version"""
import os
import sys
import time
import re
import json
import shutil
import subprocess
import threading

try:
    import yt_dlp
except ImportError:
    print("yt-dlp نصب نیست. نصب کن:\npip install yt-dlp")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.environ.get(
    "DOWNLOAD_DIR",
    os.path.join(SCRIPT_DIR, "downloads"),
)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIES = os.path.join(SCRIPT_DIR, "cookies.txt")
INSTA_COOKIES = os.path.join(SCRIPT_DIR, "instagram_cookies.txt")
QUEUE_FILE = os.path.join(SCRIPT_DIR, ".dl_queue.json")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".dl_cache.json")

URL_RE = re.compile(
    r"^(https?://|www\.|youtu\.be/|m\.youtu|t\.co/|"
    r"(?:[a-z0-9-]+\.)+(?:com|net|org|ir|tv|io|co|be|me|gg|ru|gov|edu)/)",
    re.I,
)

# پروکسی اختیاری (برای سرور داخل ایران)
PROXY = os.environ.get("DL_PROXY", "").strip()  # مثال: socks5://127.0.0.1:10810

_cache = {}


def human_size(b):
    if not b:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


def tunnel_up():
    if not PROXY:
        return False
    try:
        host_port = PROXY.split("://", 1)[-1]
        host, port = host_port.rsplit(":", 1)
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect((host, int(port)))
            return True
        except OSError:
            return False
        finally:
            s.close()
    except Exception:
        return False


def ydl_base(cookiefile=None, use_cookies=True):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 90,
        "retries": 30,
        "fragment_retries": 30,
        "retry_sleep": 5,
        "buffersize": 1024 * 1024,
        "continuedl": True,
        "nopart": False,
        "legacy_ssl": True,
        # یوتیوب این اواخر بدون حل یه چالش JS خیلی وقتا هیچ فرمتی
        # برنمی‌گردونه («Requested format is not available»). deno رو
        # توی Dockerfile نصب کردیم؛ اینجا صریح می‌گیم ازش استفاده کنه.
        "js_runtimes": {"deno": {}},
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "web_embedded", "tv", "web"]
            }
        },
    }
    if use_cookies:
        cf = COOKIES if cookiefile is None else cookiefile
        if cf and os.path.isfile(cf) and os.path.getsize(cf) > 10:
            opts["cookiefile"] = cf
    if PROXY and tunnel_up():
        opts["proxy"] = PROXY
    return opts


def cached_path(url):
    return _cache.get(url)


def cache_set(url, path):
    _cache[url] = path


def is_instagram_url(url):
    if not url:
        return False
    return bool(
        re.search(
            r"(?:instagram\.com|instagr\.am)/(?:reel|reels|p|tv|stories|s/|highlights|share)",
            url,
            re.I,
        )
    ) or bool(
        re.search(
            r"(?:instagram\.com|instagr\.am)/[\w.]+/(?:reel|p)/",
            url,
            re.I,
        )
    )


def is_tiktok_url(url):
    """لینک تیک‌تاک (vt.tiktok.com / vm.tiktok.com / www.tiktok.com / tiktok.com)."""
    if not url:
        return False
    return bool(re.search(r"(?:^|//|\.)tiktok\.com/", url, re.I))


def is_reel_url(url):
    """ویدیوهای کوتاه شبیه ریلز: اینستاگرام یا تیک‌تاک (منوی «استخراج آهنگ» + نگه‌داشتن فایل برای لینک‌سازی)."""
    return is_instagram_url(url) or is_tiktok_url(url)


def info(url, playlist=False):
    if is_instagram_url(url):
        opts = ydl_base(cookiefile=INSTA_COOKIES)
    elif is_tiktok_url(url):
        # کوکی یوتیوب به درد تیک‌تاک نمی‌خوره
        opts = ydl_base(use_cookies=False)
    else:
        opts = ydl_base(cookiefile=None)

    if playlist:
        opts["noplaylist"] = False
        opts["extract_flat"] = True

    for attempt in range(5):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            if is_instagram_url(url) and "cookiefile" in opts and "http error 400" in str(e).lower():
                opts.pop("cookiefile", None)
                print("[downloader] کوکی کهنه اینستا — بدون کوکی ادامه", flush=True)
                continue
            if attempt < 4:
                time.sleep(4 + attempt * 4)
                continue
            raise


def scan_gallery(path):
    # روی VPS گالری اندروید نداریم — عمداً خالی
    return


def fetch_thumbnail(inf):
    thumb = inf.get("thumbnail")
    if not thumb:
        return None
    try:
        import urllib.request

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        title = (inf.get("title") or "thumb")[:50].replace("/", "_")
        out = os.path.join(DOWNLOAD_DIR, f"{title}.jpg")
        urllib.request.urlretrieve(thumb, out)
        return out
    except Exception:
        return None


def save_queue(urls):
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(urls, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ ذخیره صف رد شد: {e}")


def load_queue():
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _progress_hook(d):
    if d.get("status") == "downloading":
        _progress_hook.state.update(
            {
                "downloaded": d.get("downloaded_bytes", 0),
                "total": d.get("total_bytes") or d.get("total_bytes_estimate"),
                "speed": d.get("speed"),
                "active": True,
            }
        )
        total = _progress_hook.state["total"]
        downloaded = _progress_hook.state["downloaded"]
        speed = _progress_hook.state["speed"]
        if total:
            pct = downloaded / total * 100
            mb = downloaded / (1024 * 1024)
            tmb = total / (1024 * 1024)
            spd = f" | {speed/1024/1024:.1f} MB/s" if speed else ""
            eta = (total - downloaded) / speed if speed else 0
            em, es = divmod(int(eta), 60)
            eta_str = f" | باقی‌مانده: {em}:{es:02d}" if speed else ""
            print(f"\r⬇️ {pct:5.1f}%  ({mb:.1f}/{tmb:.1f} MB){spd}{eta_str}", end="", flush=True)
    elif d.get("status") == "finished":
        print("\n⚙️ در حال پردازش فایل...")


def _pick_format(fmt_code, is_audio, audio_idx=None):
    if is_audio:
        return "bestaudio/best"
    if audio_idx is not None:
        h = int(audio_idx)
        return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    if fmt_code and str(fmt_code).isdigit():
        return f"{fmt_code}+bestaudio/{fmt_code}/best"
    if fmt_code and "height" in str(fmt_code):
        return f"{fmt_code}/best"
    if fmt_code:
        return f"{fmt_code}/best"
    return "bestvideo+bestaudio/best"


def download(url, fmt_code, is_audio=False, sub_lang=None, audio_idx=None, progress_hook=None, section=None):
    if is_instagram_url(url):
        base = ydl_base(cookiefile=INSTA_COOKIES)
        client_tries = [None]
    elif is_tiktok_url(url):
        base = ydl_base(use_cookies=False)
        client_tries = [None]
    else:
        base = ydl_base(cookiefile=None)
        client_tries = [
            ["tv_embedded", "web_embedded"],
            ["tv_music"],
            ["tv"],
            ["web_safari"],
            ["web"],
        ]

    outtmpl = os.path.join(DOWNLOAD_DIR, "%(title).80s.%(ext)s")
    format_tries = [
        _pick_format(fmt_code, is_audio, audio_idx),
        "bestaudio/best" if is_audio else "bestvideo+bestaudio/best",
        "best",
    ]
    seen = set()
    format_tries = [f for f in format_tries if not (f in seen or seen.add(f))]

    _progress_hook.state = {"downloaded": 0, "total": 0, "speed": 0, "active": False}
    state = {"done": False}

    def _tick():
        while not state["done"]:
            time.sleep(1)
            if state["done"]:
                break
            s = _progress_hook.state
            if not s["active"] or not s["total"]:
                continue
            downloaded, total, speed = s["downloaded"], s["total"], s["speed"]
            pct = downloaded / total * 100
            mb, tmb = downloaded / (1024 * 1024), total / (1024 * 1024)
            spd = f" | {speed/1024/1024:.1f} MB/s" if speed else ""
            eta = (total - downloaded) / speed if speed else 0
            em, es = divmod(int(eta), 60)
            eta_str = f" | باقی‌مانده: {em}:{es:02d}" if speed else ""
            print(f"\r⬇️ {pct:5.1f}%  ({mb:.1f}/{tmb:.1f} MB){spd}{eta_str}", end="", flush=True)

    t = threading.Thread(target=_tick, daemon=True)
    t.start()

    last_err = None
    for clients in client_tries:
        for fmt in format_tries:
            opts = dict(base)
            opts.update(
                {
                    "progress_hooks": [progress_hook] if progress_hook else [_progress_hook],
                    "quiet": True,
                    "no_warnings": True,
                    "outtmpl": outtmpl,
                    "format": fmt,
                }
            )
            if clients:
                opts["extractor_args"] = {"youtube": {"player_client": clients}}
            if is_audio:
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}
                ]
            else:
                opts["merge_output_format"] = "mp4"
            if sub_lang:
                langs = [sub_lang]
                if sub_lang.startswith("fa"):
                    for extra in ("fa", "fa-IR", "fa-AF"):
                        if extra not in langs:
                            langs.append(extra)
                opts["writesubtitles"] = True
                opts["writeautomaticsub"] = True
                opts["subtitleslangs"] = langs
                opts["subtitlesformat"] = "srt/vtt/best"
            if section and len(section) == 2:
                start_t, end_t = float(section[0]), float(section[1])

                def _ranges(info_dict, ydl, _s=start_t, _e=end_t):
                    return [{"start_time": _s, "end_time": _e}]

                opts["download_ranges"] = _ranges
                opts["force_keyframes_at_cuts"] = True

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    state["done"] = True
                    if not info:
                        continue
                    if "requested_downloads" in info and info["requested_downloads"]:
                        p = info["requested_downloads"][0].get("filepath")
                        if p:
                            return p
                    path = ydl.prepare_filename(info)
                    if is_audio:
                        base_p = os.path.splitext(path)[0]
                        for ext in (".mp3", ".m4a", ".opus", ".ogg"):
                            if os.path.exists(base_p + ext):
                                return base_p + ext
                    if path and os.path.exists(path):
                        return path
                    return path
            except Exception as e:
                last_err = e
                err = str(e).lower()
                print(f"\n⚠️ فرمت «{fmt}» (client={clients}) نشد: {e}", flush=True)
                if is_instagram_url(url) and "http error 400" in err and "cookiefile" in opts:
                    opts.pop("cookiefile", None)
                    base = ydl_base(cookiefile=None, use_cookies=False)
                    print("🔄 بدون کوکی ادامه", flush=True)
                    continue
                if "format is not available" in err or "requested format" in err:
                    continue
                if "sign in" in err or "bot" in err:
                    continue
                try:
                    time.sleep(2)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        state["done"] = True
                        path = ydl.prepare_filename(info) if info else None
                        if path:
                            return path
                except Exception as e2:
                    last_err = e2
                    continue

    state["done"] = True
    if last_err:
        raise last_err
    raise RuntimeError("دانلود ناموفق بود")


def run_job(url, job):
    try:
        path = download(
            url,
            job["fmt"],
            is_audio=job["is_audio"],
            sub_lang=job.get("sub_lang"),
            audio_idx=job.get("audio_idx"),
            section=job.get("section"),
        )
        return path
    except Exception as e:
        print(f"❌ خطا در دانلود: {e}")
        return None
