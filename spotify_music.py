"""Spotify -> MP3 module for bridge_bot (merged single-bot).
Public music features: track/album/playlist links, text search, quality select (128/192/320),
per-user default quality (/settings), download progress, per-user download queue,
full track info captions, nightcore/slowed variants, similar songs, lyrics,
inline mode, admin stats (/stats).

Run: python3 bot.py   (token built in; BOT_TOKEN / ADMIN_ID env overrides work)
"""
import logging
import asyncio
import os
import re
import json
import tempfile
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime

try:
    import imageio_ffmpeg
    _FF = _FF
except Exception:
    import shutil as _shutil
    _FF = _shutil.which("ffmpeg") or "ffmpeg"
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      InlineQueryResultArticle, InputTextMessageContent)
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, InlineQueryHandler,
                          ContextTypes, filters)

FF = _FF
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

SP_RE = re.compile(r"open\.spotify\.com/(track|album|playlist|artist)/([A-Za-z0-9]+)")
YT_RE = re.compile(r"(youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})")
SC_RE = re.compile(r"soundcloud\.com/([A-Za-z0-9_.\-/]+)")
APPLE_RE = re.compile(r"music\.apple\.com/[a-z]{2}/(?:album|song)/[^/\s]+/(\d+)(?:\?i=(\d+))?")
DEEZER_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?track/(\d+)")
URL_RE = re.compile(r"https?://[^\s]+")

DATA_DIR = os.environ.get("DATA_DIR", "/tmp/spotibot_data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
# ADMIN_ID replaced by OWNER_IDS (set by bridge_bot)

QUALITIES = ["128", "192", "320"]
DEFAULT_QUALITY = "192"

BOT_USERNAME = ""  # set by bridge_bot at startup
OWNER_IDS = set()  # set by bridge_bot from cfg.OWNER_ID


def _is_owner(user_id):
    return user_id in OWNER_IDS


async def _gate(update):
    """Reply سیکتیر for non-owners. Returns True if blocked."""
    uid = update.effective_user.id if update.effective_user else None
    if uid not in OWNER_IDS:
        try:
            if update.callback_query:
                await update.callback_query.answer("سیکتیر", show_alert=False)
            elif update.inline_query:
                await update.inline_query.answer([], cache_time=60)
            elif update.message:
                await update.message.reply_text("سیکتیر")
        except Exception:
            pass
        return True
    return False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spotibot")


# ---------------------------------------------------------------- storage ---
def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning("load %s failed: %s", path, e)
    return default


def _save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("save %s failed: %s", path, e)


def get_user_quality(user_id):
    s = _load_json(SETTINGS_FILE, {})
    return s.get(str(user_id), {}).get("quality")


def set_user_quality(user_id, quality):
    s = _load_json(SETTINGS_FILE, {})
    u = s.get(str(user_id), {})
    u["quality"] = quality
    s[str(user_id)] = u
    _save_json(SETTINGS_FILE, s)


def record_download(user_id, artist=None):
    st = _load_json(STATS_FILE, {"total_downloads": 0, "users": {}})
    st["total_downloads"] = st.get("total_downloads", 0) + 1
    users = st.setdefault("users", {})
    u = users.get(str(user_id), {"count": 0, "first": None, "last": None, "artists": {}})
    now = datetime.utcnow().isoformat(timespec="seconds")
    u["count"] += 1
    u["last"] = now
    if not u.get("first"):
        u["first"] = now
    if artist:
        arts = u.setdefault("artists", {})
        arts[artist] = arts.get(artist, 0) + 1
    users[str(user_id)] = u
    _save_json(STATS_FILE, st)


# ---------------------------------------------------------------- http -------
def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def _embed_json(kind, sid):
    """Return parsed __NEXT_DATA__ dict (or None)."""
    try:
        raw = http_get(f"https://open.spotify.com/embed/{kind}/{sid}").decode("utf-8", "ignore")
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', raw, re.S)
        if m:
            return json.loads(m.group(1))
    except Exception as e:
        log.warning("embed %s/%s failed: %s", kind, sid, e)
    return None


def _entity(d):
    try:
        return d["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception:
        return {}


def spotify_track_info(track_id):
    """Return dict with title, artist, cover_url, album, year, duration, track_no."""
    info = {"title": None, "artist": None, "cover_url": None,
            "album": None, "year": None, "duration": None, "track_no": None}
    d = _embed_json("track", track_id)
    ent = _entity(d) if d else {}
    if ent:
        info["title"] = ent.get("title") or ent.get("name")
        arts = ent.get("artists") or []
        info["artist"] = ", ".join(a.get("name", "") for a in arts) if arts else None
        alb = ent.get("album") or {}
        info["album"] = alb.get("name")
        dur = ent.get("duration_ms") or ent.get("duration")
        if dur:
            try:
                info["duration"] = fmt_duration(int(dur) // 1000 if int(dur) > 10000 else int(dur))
            except Exception:
                pass
        info["track_no"] = ent.get("track_number") or ent.get("trackNumber")
        rd = ent.get("release_date") or alb.get("release_date") or ""
        if rd and len(rd) >= 4:
            info["year"] = rd[:4]
    # cover + fallbacks via oembed
    try:
        raw = http_get(f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}").decode("utf-8", "ignore")
        od = json.loads(raw)
        info["cover_url"] = od.get("thumbnail_url")
        if not info["title"]:
            info["title"] = od.get("title")
        if not info["artist"]:
            info["artist"] = od.get("author_name") or "Unknown"
    except Exception as e:
        log.warning("oembed failed: %s", e)
        if not info["artist"]:
            info["artist"] = "Unknown"
    if not info["title"]:
        return None
    return info


def spotify_collection_tracks(kind, sid, limit=15):
    """Return (collection_name, [(title, artist), ...]) for album/playlist."""
    d = _embed_json(kind, sid)
    ent = _entity(d) if d else {}
    name = ent.get("title") or ent.get("name") or kind
    tracks = []

    def _pull(items):
        for it in items or []:
            t = it.get("track") or it
            title = t.get("title") or t.get("name")
            arts = t.get("artists") or []
            artist = ", ".join(a.get("name", "") for a in arts) if arts else ""
            if title:
                tracks.append((title, artist or "Unknown"))

    # try several known shapes
    for key in ("tracks", "items"):
        v = ent.get(key)
        if isinstance(v, dict):
            _pull(v.get("items"))
        elif isinstance(v, list):
            _pull(v)
    # nested data shapes
    try:
        data = d["props"]["pageProps"]["state"]["data"]
        for k in ("trackList", "tracks", "items"):
            v = data.get(k)
            if isinstance(v, list):
                _pull(v)
    except Exception:
        pass
    # dedupe, keep order
    seen, out = set(), []
    for t in tracks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return name, out[:limit]


def fmt_duration(sec):
    try:
        m, s = divmod(int(sec), 60)
        return f"{m}:{s:02d}"
    except Exception:
        return ""


def apple_music_info(url):
    """Extract track metadata from an Apple Music URL via iTunes Lookup API."""
    m = APPLE_RE.search(url)
    if not m:
        return None
    track_id = m.group(2) or m.group(1)
    try:
        raw = http_get(f"https://itunes.apple.com/lookup?id={track_id}&entity=song", timeout=20)
        d = json.loads(raw.decode("utf-8", "ignore"))
        res = (d.get("results") or [{}])[0]
        if not res.get("trackName"):
            return None
        art = (res.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        return {"title": res.get("trackName"), "artist": res.get("artistName") or "Unknown",
                "cover_url": art or None, "album": res.get("collectionName"),
                "year": (res.get("releaseDate") or "")[:4] or None,
                "duration": fmt_duration((res.get("trackTimeMillis") or 0) // 1000),
                "track_no": res.get("trackNumber"), "sid": f"am:{track_id}"}
    except Exception as e:
        log.warning("apple music lookup failed: %s", e)
    return None


def deezer_track_info(track_id):
    """Track metadata via public Deezer API (no auth)."""
    try:
        raw = http_get(f"https://api.deezer.com/track/{track_id}", timeout=20)
        d = json.loads(raw.decode("utf-8", "ignore"))
        if not d.get("title"):
            return None
        artist = (d.get("artist") or {}).get("name") or "Unknown"
        album = d.get("album") or {}
        return {"title": d.get("title"), "artist": artist,
                "cover_url": album.get("cover_big") or album.get("cover"),
                "album": album.get("title"),
                "year": (d.get("release_date") or "")[:4] or None,
                "duration": fmt_duration(d.get("duration") or 0),
                "track_no": d.get("track_position"), "sid": f"dz:{track_id}"}
    except Exception as e:
        log.warning("deezer lookup failed: %s", e)
    return None


def soundcloud_info(url):
    """Track metadata via yt-dlp (no download)."""
    try:
        r = subprocess.run(
            ["yt-dlp", url, "--print", "%(title)s|%(uploader)s|%(duration_string)s|%(thumbnail)s",
             "--no-playlist", "-q", "--js-runtimes", "deno,node"],
            capture_output=True, text=True, timeout=60)
        line = (r.stdout or "").strip().splitlines()
        if not line:
            return None
        parts = line[0].split("|")
        title = parts[0].strip() if len(parts) > 0 else ""
        if not title or title == "NA":
            return None
        # SoundCloud titles are often "Artist - Title"
        artist, t = "Unknown", title
        if " - " in title:
            artist, t = [x.strip() for x in title.split(" - ", 1)]
        import hashlib as _hl
        skey = _hl.md5(url.encode()).hexdigest()[:10]
        _pending["sc" + skey] = url
        return {"title": t, "artist": artist or "Unknown",
                "cover_url": parts[3].strip() if len(parts) > 3 and parts[3].strip() != "NA" else None,
                "album": None, "year": None,
                "duration": parts[2].strip() if len(parts) > 2 else None,
                "track_no": None, "sid": f"sc:{skey}", "sc_url": url}
    except Exception as e:
        log.warning("soundcloud info failed: %s", e)
    return None


def build_caption(info, quality):
    lines = [f"🎵 {info['artist']} – {info['title']}"]
    if info.get("album"):
        lines.append(f"💿 آلبوم: {info['album']}")
    meta = []
    if info.get("year"):
        meta.append(f"📅 {info['year']}")
    if info.get("duration"):
        meta.append(f"⏱ {info['duration']}")
    if info.get("track_no"):
        meta.append(f"🔢 ترک {info['track_no']}")
    if meta:
        lines.append(" | ".join(meta))
    lines.append(f"🎧 کیفیت: {quality}kbps")
    return "\n".join(lines)


# ---------------------------------------------------------------- yt-dlp ----
def download_mp3(query, out_mp3, quality="192", progress_cb=None, direct_url=None):
    """Download + convert to mp3. progress_cb(pct:int) called on milestones.
    If direct_url is given, download it instead of ytsearch1:query."""
    target = direct_url if direct_url else "ytsearch1:" + query
    base_cmd = ["yt-dlp", target, "-x", "--audio-format", "mp3",
                "--audio-quality", f"{quality}K",
                "--ffmpeg-location", FF, "-o", out_mp3.replace(".mp3", ".%(ext)s"),
                "--no-playlist", "--js-runtimes", "deno,node",
                "--newline", "--progress"]
    client_variants = [
        ["--extractor-args", "youtube:player_client=ios,android"],
        ["--extractor-args", "youtube:player_client=android,web"],
        [],
    ]
    last_err, milestones = "", {25, 50, 75}
    for extra in client_variants:
        try:
            p = subprocess.Popen(base_cmd + extra, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
                if m and progress_cb:
                    pct = int(float(m.group(1)))
                    hit = [x for x in milestones if pct >= x]
                    if hit:
                        for x in sorted(hit):
                            milestones.discard(x)
                        try:
                            progress_cb(max(hit))
                        except Exception:
                            pass
            p.wait(timeout=300)
        except Exception as e:
            last_err = str(e)[-500:]
            continue
        if os.path.exists(out_mp3):
            return out_mp3
        base = os.path.dirname(out_mp3)
        try:
            cands = [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".mp3")]
        except Exception:
            cands = []
        if cands:
            os.rename(cands[0], out_mp3)
            return out_mp3
        last_err = "no mp3 produced"
        log.warning("yt-dlp attempt %s produced nothing", extra)
    raise RuntimeError("yt-dlp failed: " + last_err[-300:])


def yt_flat_search(query, n=5):
    """Fast flat search -> [(title, uploader, duration_str, video_id)]."""
    cmd = ["yt-dlp", f"ytsearch{n}:" + query, "--flat-playlist",
           "--print", "%(title)s|%(uploader)s|%(duration_string)s|%(id)s",
           "--no-playlist", "-q", "--js-runtimes", "deno,node",
           "--extractor-args", "youtube:player_client=ios,android"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("|")
            if len(parts) == 4 and parts[3].strip():
                out.append(tuple(p.strip() for p in parts))
        return out
    except Exception as e:
        log.warning("flat search failed: %s", e)
        return []


def tag_mp3(path, info, cover_bytes=None):
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, APIC
    try:
        audio = MP3(path, ID3=ID3)
    except Exception:
        audio = MP3(path)
        audio.add_tags()
    for tag in ("TIT2", "TPE1", "TALB", "TDRC", "TRCK", "APIC"):
        audio.tags.delall(tag)
    audio.tags.add(TIT2(encoding=3, text=info["title"]))
    audio.tags.add(TPE1(encoding=3, text=info["artist"] or "Unknown"))
    if info.get("album"):
        audio.tags.add(TALB(encoding=3, text=info["album"]))
    if info.get("year"):
        audio.tags.add(TDRC(encoding=3, text=info["year"]))
    if info.get("track_no"):
        audio.tags.add(TRCK(encoding=3, text=str(info["track_no"])))
    if cover_bytes:
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
    audio.save()


def make_variant(in_mp3, out_mp3, tempo):
    """Nightcore (1.25) / slowed (0.8) via ffmpeg atempo."""
    cmd = [FF, "-y", "-i", in_mp3, "-filter:a", f"atempo={tempo}",
           "-b:a", "192k", out_mp3]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not os.path.exists(out_mp3):
        raise RuntimeError("ffmpeg variant failed: " + (r.stderr or "")[-200:])


# ---------------------------------------------------------------- lyrics ----
def get_lyrics(title, artist):
    q = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    try:
        d = json.loads(http_get(f"https://lrclib.net/api/get?{q}", timeout=20))
        txt = d.get("plainLyrics") or ""
        if txt.strip():
            return txt.strip()
    except Exception as e:
        log.warning("lrclib get failed: %s", e)
    try:
        d = json.loads(http_get(f"https://lrclib.net/api/search?{q}", timeout=20))
        if isinstance(d, list) and d:
            best = d[0]
            for it in d:
                if (it.get("trackName", "").lower() == title.lower()
                        and it.get("artistName", "").lower() == artist.lower()):
                    best = it
                    break
            txt = best.get("plainLyrics") or ""
            if txt.strip():
                return txt.strip()
    except Exception as e:
        log.warning("lrclib search failed: %s", e)
    return None


def get_synced_lyrics(title, artist):
    """Return LRC-format synced lyrics string or None (LRClib)."""
    q = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    for endpoint in (f"https://lrclib.net/api/get?{q}", f"https://lrclib.net/api/search?{q}"):
        try:
            d = json.loads(http_get(endpoint, timeout=20))
            items = d if isinstance(d, list) else [d]
            for it in items:
                lrc = (it.get("syncedLyrics") or "").strip()
                if lrc:
                    return lrc
        except Exception as e:
            log.warning("lrclib synced failed: %s", e)
    return None


def translate_text(text, src="en", dst="fa", chunk_size=450):
    """Translate long text via MyMemory (free, chunked). Returns str or None."""
    out = []
    # split into <=chunk_size pieces on line boundaries
    cur = ""
    pieces = []
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > chunk_size:
            pieces.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        pieces.append(cur)
    for p in pieces[:20]:  # cap to avoid API abuse
        try:
            q = urllib.parse.urlencode({"q": p, "langpair": f"{src}|{dst}"})
            d = json.loads(http_get(f"https://api.mymemory.translated.net/get?{q}", timeout=25))
            t = ((d.get("responseData") or {}).get("translatedText") or "").strip()
            out.append(t if t else p)
        except Exception as e:
            log.warning("translate chunk failed: %s", e)
            out.append(p)
    return "\n".join(out) if out else None


def chunk(text, n=4000):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out or [""]


# ---------------------------------------------------------------- queue -----
_user_locks = {}
_pending = {}  # short key -> info dict (for callbacks with long payloads)


def user_lock(uid):
    l = _user_locks.get(uid)
    if l is None:
        l = asyncio.Lock()
        _user_locks[uid] = l
    return l


# ---------------------------------------------------------------- helpers ---
def quality_keyboard(prefix, sid_or_query):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("128kbps", callback_data=f"{prefix}:128:{sid_or_query}"),
        InlineKeyboardButton("192kbps", callback_data=f"{prefix}:192:{sid_or_query}"),
        InlineKeyboardButton("320kbps", callback_data=f"{prefix}:320:{sid_or_query}"),
    ]])


async def send_track(chat_id, info, quality, ctx, reply_to=None, extra_buttons=None):
    """Download, tag, send one track. Returns True on success."""
    loop = asyncio.get_running_loop()
    cover_bytes = None
    if info.get("cover_url"):
        try:
            cover_bytes = await loop.run_in_executor(None, http_get, info["cover_url"])
        except Exception:
            pass
    tmpd = tempfile.mkdtemp(prefix="spot_", dir="/tmp")
    out = os.path.join(tmpd, "song.mp3")
    status = None
    try:
        if reply_to:
            status = await reply_to.reply_text(f"🎵 {info['artist']} – {info['title']}\n⏳ دانلود ({quality}kbps)…")
        last_pct = {"v": 0}

        def on_prog(pct):
            last_pct["v"] = pct

        await loop.run_in_executor(None, lambda: download_mp3(
            f"{info['artist']} {info['title']}", out, quality, on_prog,
            direct_url=info.get("sc_url") or (f"https://www.youtube.com/watch?v={info['ytid']}" if info.get("ytid") else None)))
        if status and last_pct["v"]:
            try:
                await status.edit_text(f"🎵 {info['artist']} – {info['title']}\n✅ دانلود شد، دارم می‌فرستم…")
            except Exception:
                pass
        await loop.run_in_executor(None, lambda: tag_mp3(out, info, cover_bytes))
        thumb_path = None
        if cover_bytes:
            try:
                thumb_path = os.path.join(tmpd, "cover.jpg")
                with open(thumb_path, "wb") as fh:
                    fh.write(cover_bytes)
            except Exception:
                thumb_path = None
        sid = info.get("sid", "")
        buttons = [
            [InlineKeyboardButton("متن آهنگ 📝", callback_data=f"lyr:{sid}"),
             InlineKeyboardButton("🌐 ترجمه", callback_data=f"tr:{sid}")],
            [InlineKeyboardButton("📄 فایل LRC", callback_data=f"lrc:{sid}"),
             InlineKeyboardButton("🎧 مشابه", callback_data=f"sim:{sid}")],
            [InlineKeyboardButton("⚡ تند", callback_data=f"nc:{quality}:{sid}"),
             InlineKeyboardButton("🐌 کند", callback_data=f"sl:{quality}:{sid}")],
            [InlineKeyboardButton("🔄 کیفیت دیگه", callback_data=f"rq:{sid}")],
        ]
        if extra_buttons:
            buttons.extend(extra_buttons)
        caption = build_caption(info, quality)
        with open(out, "rb") as f:
            kwargs = dict(audio=f, performer=info["artist"], title=info["title"],
                          caption=caption, reply_markup=InlineKeyboardMarkup(buttons))
            if thumb_path:
                with open(thumb_path, "rb") as tf:
                    kwargs["thumbnail"] = tf
                    await ctx.bot.send_audio(chat_id=chat_id, **kwargs)
            else:
                await ctx.bot.send_audio(chat_id=chat_id, **kwargs)
        record_download(chat_id, info.get("artist"))
        return True
    except Exception as e:
        log.exception("send_track failed")
        if reply_to:
            try:
                await reply_to.reply_text(f"نشد عشقم 😞 ({str(e)[:150]})")
            except Exception:
                pass
        return False
    finally:
        try:
            if status:
                await status.delete()
        except Exception:
            pass
        try:
            for p in (out, os.path.join(tmpd, "cover.jpg")):
                if p and os.path.exists(p):
                    os.remove(p)
            os.rmdir(tmpd)
        except Exception:
            pass


async def handle_track_request(message, sid, ctx, quality=None):
    """Download one Spotify track (queued per user)."""
    uid = message.from_user.id
    lock = user_lock(uid)
    queued = lock.locked()
    async with lock:
        loop = asyncio.get_running_loop()
        if not quality:
            quality = get_user_quality(uid)
        if quality:
            info = await loop.run_in_executor(None, spotify_track_info, sid)
            if not info:
                await message.reply_text("نتونستم اطلاعات آهنگ رو بگیرم 😕")
                return
            info["sid"] = sid
            await send_track(message.chat_id, info, quality, ctx, reply_to=message)
        else:
            info = await loop.run_in_executor(None, spotify_track_info, sid)
            if not info:
                await message.reply_text("نتونستم اطلاعات آهنگ رو بگیرم 😕")
                return
            await message.reply_text(
                f"🎵 {info['artist']} – {info['title']}\nکیفیت رو انتخاب کن:",
                reply_markup=quality_keyboard("mq", sid))


async def handle_search_request(message, query, ctx):
    """Text search -> quick Spotify-less flow via YouTube flat search."""
    uid = message.from_user.id
    lock = user_lock(uid)
    async with lock:
        loop = asyncio.get_running_loop()
        status = await message.reply_text(f"🔍 دارم «{query}» رو پیدا می‌کنم…")
        results = await loop.run_in_executor(None, yt_flat_search, query, 1)
        try:
            await status.delete()
        except Exception:
            pass
        if not results:
            await message.reply_text("چیزی پیدا نکردم 😕 یه جور دیگه بنویس")
            return
        title, uploader, dur, vid = results[0]
        info = {"title": title, "artist": uploader or "Unknown", "cover_url": None,
                "album": None, "year": None, "duration": dur, "track_no": None, "sid": f"yt:{vid}"}
        quality = get_user_quality(uid) or DEFAULT_QUALITY
        await send_track(message.chat_id, info, quality, ctx, reply_to=message)


# ---------------------------------------------------------------- commands --
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    arg = (ctx.args[0] if ctx.args else "")
    if arg.startswith("dl_"):
        # deep link from inline: dl_<youtube_id>
        vid = arg[3:]
        await update.message.reply_text("⏳ دارم آماده‌ش می‌کنم…")
        info = {"title": "YouTube audio", "artist": "Unknown", "cover_url": None,
                "album": None, "year": None, "duration": None, "track_no": None,
                "sid": f"yt:{vid}", "ytid": vid}
        quality = get_user_quality(update.message.from_user.id) or DEFAULT_QUALITY
        await send_track(update.message.chat_id, info, quality, ctx,
                         reply_to=update.message)
        return
    await update.message.reply_text(
        "سلام عشقم 🎵\n"
        "لینک بفرست (اسپاتیفای، اپل موزیک، دیزر، ساندکلاد، یوتیوب)\n"
        "یا فقط اسم آهنگ رو بنویس.\n"
        "می‌تونی چند تا لینک رو یه‌جا بفرستی!\n"
        "با /settings کیفیت پیش‌فرضت رو ست کن، با /mystats آمارت رو ببین.")


async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _gate(update):
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("128kbps", callback_data="set:128"),
        InlineKeyboardButton("192kbps", callback_data="set:192"),
        InlineKeyboardButton("320kbps", callback_data="set:320"),
    ]])
    cur = get_user_quality(update.message.from_user.id) or "تنظیم نشده (پیش‌فرض 192)"
    await update.message.reply_text(
        f"کیفیت پیش‌فرض فعلی: {cur}\nانتخاب کن:",
        reply_markup=kb)


async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in OWNER_IDS:
        await update.message.reply_text("این دستور فقط برای مدیره 😌")
        return
    st = _load_json(STATS_FILE, {"total_downloads": 0, "users": {}})
    users = st.get("users", {})
    total = st.get("total_downloads", 0)
    top = sorted(users.items(), key=lambda kv: kv[1].get("count", 0), reverse=True)[:5]
    lines = [f"📊 آمار ربات", f"🎵 کل دانلودها: {total}", f"👥 کاربران: {len(users)}"]
    for uid, u in top:
        lines.append(f"• {uid}: {u.get('count', 0)} دانلود")
    await update.message.reply_text("\n".join(lines))


async def mystats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _gate(update):
        return
    uid = str(update.message.from_user.id)
    st = _load_json(STATS_FILE, {"total_downloads": 0, "users": {}})
    u = st.get("users", {}).get(uid, {"count": 0, "artists": {}})
    arts = sorted((u.get("artists") or {}).items(), key=lambda kv: kv[1], reverse=True)[:5]
    lines = ["📊 آمار تو", f"🎵 دانلودها: {u.get('count', 0)}"]
    if arts:
        lines.append("🎤 خواننده‌های پرتکرار:")
        for name, c in arts:
            lines.append(f"• {name}: {c}")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------- text ------
async def classify_url(url):
    """Return (action, payload) for a URL. action: track|collection|direct|search."""
    m = SP_RE.search(url)
    if m:
        kind, sid = m.group(1), m.group(2)
        if kind == "track":
            return ("sp_track", sid)
        if kind in ("album", "playlist"):
            return ("sp_collection", (kind, sid))
        return ("unsupported", "artist")
    m = APPLE_RE.search(url)
    if m:
        return ("apple", url)
    m = DEEZER_RE.search(url)
    if m:
        return ("deezer", m.group(1))
    m = SC_RE.search(url)
    if m:
        return ("soundcloud", url)
    m = YT_RE.search(url)
    if m:
        return ("youtube", m.group(2))
    return (None, None)


async def handle_single_url(message, url, ctx):
    """Handle one URL (any supported source)."""
    loop = asyncio.get_running_loop()
    action, payload = await classify_url(url)
    uid = message.from_user.id
    quality = get_user_quality(uid) or DEFAULT_QUALITY

    if action == "sp_track":
        await handle_track_request(message, payload, ctx)
        return True
    if action == "sp_collection":
        kind, sid = payload
        await handle_collection(message, kind, sid, ctx)
        return True
    if action == "unsupported":
        await message.reply_text("لینک آرتیست رو فعلاً ساپورت نمی‌کنم 🥺 لینک آهنگ بفرست.")
        return True
    if not action:
        return False
    # direct sources: serialize per user
    async with user_lock(uid):
        if action == "apple":
            info = await loop.run_in_executor(None, apple_music_info, payload)
            if not info:
                await message.reply_text("نتونستم اطلاعات لینک اپل موزیک رو بگیرم 😕")
                return True
            if not get_user_quality(uid):
                import uuid as _uuid
                key = _uuid.uuid4().hex[:8]
                _pending[key] = info
                await message.reply_text(
                    f"🎵 {info['artist']} – {info['title']}\nکیفیت رو انتخاب کن:",
                    reply_markup=quality_keyboard("mqam", key))
                return True
            await send_track(message.chat_id, info, quality, ctx, reply_to=message)
        elif action == "deezer":
            info = await loop.run_in_executor(None, deezer_track_info, payload)
            if not info:
                await message.reply_text("نتونستم اطلاعات لینک دیزر رو بگیرم 😕")
                return True
            await send_track(message.chat_id, info, quality, ctx, reply_to=message)
        elif action == "soundcloud":
            status = await message.reply_text("🎵 دارم اطلاعات ساندکلاد رو می‌گیرم…")
            info = await loop.run_in_executor(None, soundcloud_info, payload)
            try:
                await status.delete()
            except Exception:
                pass
            if not info:
                await message.reply_text("نتونستم لینک ساندکلاد رو بخونم 😕")
                return True
            await send_track(message.chat_id, info, quality, ctx, reply_to=message)
        elif action == "youtube":
            info = {"title": "YouTube audio", "artist": "Unknown", "cover_url": None,
                    "album": None, "year": None, "duration": None, "track_no": None,
                    "sid": f"yt:{payload}", "ytid": payload}
            await send_track(message.chat_id, info, quality, ctx, reply_to=message)
    return True


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    urls = URL_RE.findall(text)
    if urls:
        recognized = False
        if len(urls) > 1:
            await update.message.reply_text(f"📥 {len(urls)} لینک گرفتم، دونه‌دونه دانلود می‌کنم…")
        for url in urls:
            action, _ = await classify_url(url)
            if action:
                recognized = True
                await handle_single_url(update.message, url, ctx)
        if recognized:
            return
    # plain text -> search
    if len(text) < 2:
        await update.message.reply_text("یه چیزی بنویس دیگه 😅")
        return
    await handle_search_request(update.message, text, ctx)


async def handle_collection(message, kind, sid, ctx):
    uid = message.from_user.id
    lock = user_lock(uid)
    async with lock:
        loop = asyncio.get_running_loop()
        status = await message.reply_text(f"📀 دارم لیست {kind} رو می‌گیرم…")
        name, tracks = await loop.run_in_executor(None, spotify_collection_tracks, kind, sid)
        if not tracks:
            try:
                await status.edit_text("نتونستم ترک‌های لیست رو بگیرم 😕")
            except Exception:
                pass
            return
        try:
            await status.edit_text(f"📀 {name}\n{len(tracks)} آهنگ پیدا شد، شروع می‌کنم… 🎧")
        except Exception:
            pass
        quality = get_user_quality(uid) or DEFAULT_QUALITY
        ok, fail = 0, 0
        for i, (title, artist) in enumerate(tracks, 1):
            try:
                await status.edit_text(f"📀 {name}\n⏳ {i}/{len(tracks)}: {artist} – {title}")
            except Exception:
                pass
            info = {"title": title, "artist": artist, "cover_url": None,
                    "album": name if kind == "album" else None, "year": None,
                    "duration": None, "track_no": i, "sid": ""}
            if await send_track(message.chat_id, info, quality, ctx):
                ok += 1
            else:
                fail += 1
        try:
            await status.edit_text(f"📀 {name}\n✅ تموم شد: {ok} موفق، {fail} ناموفق")
        except Exception:
            pass


# ---------------------------------------------------------------- callbacks -
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _gate(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    loop = asyncio.get_running_loop()

    if data.startswith("set:"):
        set_user_quality(q.from_user.id, data.split(":")[1])
        await q.edit_message_text(f"✅ کیفیت پیش‌فرضت شد {data.split(':')[1]}kbps 🎧")
        return

    if data.startswith("mq:"):
        # quality chosen for a track: q:<quality>:<sid>
        _, quality, sid = data.split(":", 2)
        await q.edit_message_text(f"⏳ دانلود با کیفیت {quality}kbps…")
        info = await loop.run_in_executor(None, spotify_track_info, sid)
        if not info:
            await q.message.reply_text("نتونستم اطلاعات آهنگ رو بگیرم 😕")
            return
        info["sid"] = sid
        await send_track(q.message.chat_id, info, quality, ctx, reply_to=q.message)
        return

    if data.startswith("mqam:"):
        # quality chosen for an Apple Music link: qam:<quality>:<key>
        _, quality, key = data.split(":", 2)
        await q.edit_message_text(f"⏳ دانلود با کیفیت {quality}kbps…")
        info = _pending.pop(key, None)
        if not info:
            await q.message.reply_text("منقضی شد، لینک رو دوباره بفرست 😕")
            return
        await send_track(q.message.chat_id, info, quality, ctx, reply_to=q.message)
        return

    if data.startswith("rq:"):
        # re-download with different quality
        sid = data.split(":", 1)[1]
        await q.message.reply_text("کیفیت جدید رو انتخاب کن:",
                                   reply_markup=quality_keyboard("mq", sid))
        return

    if data.startswith("lyr:"):
        sid = data.split(":", 1)[1]
        await q.message.reply_text("دارم متن رو پیدا می‌کنم… 📝")
        if sid.startswith("yt:"):
            await q.message.reply_text("برای این آهنگ اسم دقیق ندارم، متنش رو پیدا نکردم 😕")
            return
        info = await loop.run_in_executor(None, spotify_track_info, sid)
        if not info:
            await q.message.reply_text("نتونستم اسم آهنگ رو بگیرم 😕")
            return
        txt = await loop.run_in_executor(None, get_lyrics, info["title"], info["artist"])
        if not txt:
            await q.message.reply_text(f"متن «{info['title']}» پیدا نشد 😞")
            return
        for p in chunk(f"🎵 {info['artist']} – {info['title']}\n\n{txt}", 4000):
            await q.message.reply_text(p)
        return

    if data.startswith("tr:"):
        sid = data.split(":", 1)[1]
        await q.message.reply_text("🌐 دارم ترجمه می‌کنم…")
        title = artist = None
        if sid.startswith("yt:"):
            await q.message.reply_text("برای این آهنگ اسم دقیق ندارم 😕")
            return
        elif sid.startswith("am:"):
            info = await loop.run_in_executor(None, apple_music_info, f"https://music.apple.com/x/y/{sid[3:]}")
            if info:
                title, artist = info["title"], info["artist"]
        elif sid.startswith("dz:"):
            info = await loop.run_in_executor(None, deezer_track_info, sid[3:])
            if info:
                title, artist = info["title"], info["artist"]
        elif sid.startswith("sc:"):
            sc_url = _pending.get("sc" + sid[3:], "")
            info = await loop.run_in_executor(None, soundcloud_info, sc_url) if sc_url else None
            if info:
                title, artist = info["title"], info["artist"]
        else:
            info = await loop.run_in_executor(None, spotify_track_info, sid)
            if info:
                title, artist = info["title"], info["artist"]
        if not title:
            await q.message.reply_text("نتونستم اسم آهنگ رو بگیرم 😕")
            return
        txt = await loop.run_in_executor(None, get_lyrics, title, artist)
        if not txt:
            await q.message.reply_text("متنی پیدا نشد که ترجمه کنم 😕")
            return
        fa = await loop.run_in_executor(None, translate_text, txt)
        if not fa:
            await q.message.reply_text("ترجمه ناموفق بود 😕")
            return
        for p in chunk(f"🌐 ترجمه «{title}»\n\n{fa}", 4000):
            await q.message.reply_text(p)
        return

    if data.startswith("lrc:"):
        sid = data.split(":", 1)[1]
        await q.message.reply_text("📄 دارم فایل LRC رو می‌سازم…")
        title = artist = None
        if sid.startswith("yt:"):
            await q.message.reply_text("برای این آهنگ اسم دقیق ندارم 😕")
            return
        elif sid.startswith("dz:"):
            info = await loop.run_in_executor(None, deezer_track_info, sid[3:])
            if info:
                title, artist = info["title"], info["artist"]
        elif sid.startswith("sc:"):
            sc_url = _pending.get("sc" + sid[3:], "")
            info = await loop.run_in_executor(None, soundcloud_info, sc_url) if sc_url else None
            if info:
                title, artist = info["title"], info["artist"]
        else:
            info = await loop.run_in_executor(None, spotify_track_info, sid)
            if info:
                title, artist = info["title"], info["artist"]
        if not title:
            await q.message.reply_text("نتونستم اسم آهنگ رو بگیرم 😕")
            return
        lrc = await loop.run_in_executor(None, get_synced_lyrics, title, artist)
        if not lrc:
            await q.message.reply_text("متن همگام‌شده برای این آهنگ پیدا نشد 😕")
            return
        tmpd = tempfile.mkdtemp(prefix="lrc_", dir="/tmp")
        path = os.path.join(tmpd, f"{artist} - {title}.lrc".replace("/", "_")[:80])
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(lrc)
            with open(path, "rb") as f:
                await q.message.reply_document(document=f, filename=os.path.basename(path),
                                               caption=f"📄 {artist} – {title}")
        finally:
            try:
                os.remove(path)
                os.rmdir(tmpd)
            except Exception:
                pass
        return

    if data.startswith(("nc:", "sl:")):
        # nightcore / slowed variant of the last sent track
        # we re-download quickly then apply filter (cached by query)
        typ, quality, sid = data.split(":", 2)
        tempo = "1.25" if typ == "nc" else "0.8"
        label = "⚡ نسخه تند" if typ == "nc" else "🐌 نسخه کند"
        status = await q.message.reply_text(f"{label} در حال ساخت… ⏳")
        try:
            if sid.startswith("yt:"):
                query = None  # handled via ytid below
                info = {"title": "YouTube audio", "artist": "Unknown", "cover_url": None,
                        "album": None, "year": None, "duration": None, "track_no": None, "sid": sid}
            else:
                info = await loop.run_in_executor(None, spotify_track_info, sid)
                if not info:
                    await status.edit_text("نتونستم اطلاعات آهنگ رو بگیرم 😕")
                    return
                info["sid"] = sid
            tmpd = tempfile.mkdtemp(prefix="spotv_", dir="/tmp")
            src = os.path.join(tmpd, "src.mp3")
            out = os.path.join(tmpd, "variant.mp3")
            ytid = info.get("ytid")
            dl_query = f"{info['artist']} {info['title']}" if not ytid else None
            # download via yt-dlp directly (reuse helper with query or url)
            def _dl():
                import subprocess as sp
                target = f"https://www.youtube.com/watch?v={ytid}" if ytid else "ytsearch1:" + dl_query
                cmd = ["yt-dlp", target, "-x", "--audio-format", "mp3",
                       "--audio-quality", f"{quality}K", "--ffmpeg-location", FF,
                       "-o", src.replace(".mp3", ".%(ext)s"),
                       "--no-playlist", "-q", "--js-runtimes", "deno,node",
                       "--extractor-args", "youtube:player_client=ios,android"]
                sp.run(cmd, capture_output=True, timeout=300)
                if not os.path.exists(src):
                    base = os.path.dirname(src)
                    c = [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".mp3")]
                    if c:
                        os.rename(c[0], src)
            await loop.run_in_executor(None, _dl)
            if not os.path.exists(src):
                await status.edit_text("دانلود ناموفق بود 😕")
                return
            await loop.run_in_executor(None, lambda: make_variant(src, out, tempo))
            await loop.run_in_executor(None, lambda: tag_mp3(out, info))
            with open(out, "rb") as f:
                await q.message.reply_audio(
                    audio=f, performer=info["artist"], title=f"{info['title']} ({label})",
                    caption=f"🎵 {info['artist']} – {info['title']}\n{label} 🎧")
            await status.delete()
            record_download(q.message.chat_id)
        except Exception as e:
            log.exception("variant failed")
            try:
                await status.edit_text(f"نشد 😞 ({str(e)[:120]})")
            except Exception:
                pass
        finally:
            try:
                import shutil
                shutil.rmtree(tmpd, ignore_errors=True)
            except Exception:
                pass
        return

    if data.startswith("sim:"):
        sid = data.split(":", 1)[1]
        await q.message.reply_text("🎧 دارم آهنگ‌های مشابه رو پیدا می‌کنم…")
        if sid.startswith("yt:"):
            await q.message.reply_text("برای این آهنگ پیشنهاد مشابه ندارم 😕")
            return
        info = await loop.run_in_executor(None, spotify_track_info, sid)
        if not info or not info.get("artist"):
            await q.message.reply_text("نتونستم آرتیست رو پیدا کنم 😕")
            return
        results = await loop.run_in_executor(
            None, yt_flat_search, f"{info['artist']} top songs", 6)
        if not results:
            await q.message.reply_text("چیزی پیدا نکردم 😕")
            return
        kb = []
        for title, uploader, dur, vid in results[:6]:
            kb.append([InlineKeyboardButton(
                f"🎵 {title[:45]} ({dur})", callback_data=f"psim:{vid}")])
        await q.message.reply_text(f"🎧 مشابه «{info['artist']}»:",
                                   reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("psim:"):
        vid = data.split(":", 1)[1]
        info = {"title": "YouTube audio", "artist": "Unknown", "cover_url": None,
                "album": None, "year": None, "duration": None, "track_no": None,
                "sid": f"yt:{vid}", "ytid": vid}
        quality = get_user_quality(q.from_user.id) or DEFAULT_QUALITY
        await send_track(q.message.chat_id, info, quality, ctx, reply_to=q.message)
        return


# ---------------------------------------------------------------- inline ----
async def on_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _gate(update):
        return
    iq = update.inline_query
    query = (iq.query or "").strip()
    if len(query) < 2:
        return
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, yt_flat_search, query, 5)
    answers = []
    for i, (title, uploader, dur, vid) in enumerate(results):
        deep = f"https://t.me/{BOT_USERNAME}?start=dl_{vid}" if BOT_USERNAME else ""
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬇️ دانلود", url=deep)]]) if deep else None
        answers.append(InlineQueryResultArticle(
            id=f"{vid}_{i}", title=title[:60],
            description=f"{uploader} • {dur}",
            input_message_content=InputTextMessageContent(
                f"🎵 {title}\n👤 {uploader}\n⬇️ برای دانلود دکمه رو بزن:"),
            reply_markup=kb))
    try:
        await iq.answer(answers, cache_time=60)
    except Exception as e:
        log.warning("inline answer failed: %s", e)


# ---------------------------------------------------------------------------
# Merge interface for bridge_bot (single-bot mode).
# ---------------------------------------------------------------------------

# Callback prefixes owned by the music module (no collision with bridge_bot).
MUSIC_CB_PATTERN = r"^(set|mq|mqam|rq|lyr|tr|lrc|nc|sl|sim|psim):"

# Music link domains (YouTube intentionally excluded -> handled by bridge_bot).
_MUSIC_DOMAINS_RE = re.compile(
    r"https?://[^\s]*(open\.spotify\.com|music\.apple\.com|deezer\.com|soundcloud\.com)[^\s]*",
    re.IGNORECASE,
)

_MUSIC_ACTIONS = {"sp_track", "sp_collection", "apple", "deezer", "soundcloud", "unsupported"}


def music_url_filter():
    """PTB filter matching messages that contain music-service links."""
    from telegram.ext import filters as _f
    return _f.Regex(_MUSIC_DOMAINS_RE)


async def music_link_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _gate(update):
        return
    """Handle messages containing music links (public). Skips YouTube links."""
    text = (update.message.text or "").strip()
    urls = URL_RE.findall(text)
    music_urls = []
    for url in urls:
        action, _ = await classify_url(url)
        if action in _MUSIC_ACTIONS:
            music_urls.append(url)
    if not music_urls:
        return
    if len(music_urls) > 1:
        await update.message.reply_text(f"📥 {len(music_urls)} لینک موزیک گرفتم، دونه‌دونه دانلود می‌کنم… 🎵")
    for url in music_urls:
        await handle_single_url(update.message, url, ctx)


async def music_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _gate(update):
        return
    """/music <query> — text search for music (public)."""
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.message.reply_text("اسم آهنگ رو بعد از دستور بنویس:\n`/music tataloo`")
        return
    await handle_search_request(update.message, query, ctx)


async def handle_inline_deep_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE, vid: str):
    """/start dl_<youtube_id> from inline results (public)."""
    await update.message.reply_text("⏳ دارم آماده‌ش می‌کنم… 🎵")
    info = {"title": "YouTube audio", "artist": "Unknown", "cover_url": None,
            "album": None, "year": None, "duration": None, "track_no": None,
            "sid": f"yt:{vid}", "ytid": vid}
    quality = get_user_quality(update.message.from_user.id) or DEFAULT_QUALITY
    await send_track(update.message.chat_id, info, quality, ctx,
                     reply_to=update.message)
