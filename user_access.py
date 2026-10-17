"""Per-user access control for bridge_bot.

Owner manages users from inside the bot (/users): add by numeric Telegram ID,
toggle per-feature permissions, remove. Stored in DATA_DIR/user_access.json.
"""

import json
import os
import re
import time

# (key, label, category) — category is used to group toggles in the UI.
PERM_DEFS = [
    # downloads per site
    ("dl_yt", "یوتیوب", "📥 دانلود"),
    ("dl_ig", "اینستاگرام", "📥 دانلود"),
    ("dl_tiktok", "تیک‌تاک", "📥 دانلود"),
    ("dl_other", "سایر سایت‌ها", "📥 دانلود"),
    # download extras
    ("dl_audio", "صدا MP3", "🎛 امکانات دانلود"),
    ("dl_clip", "برش ویدیو", "🎛 امکانات دانلود"),
    ("dl_subs", "زیرنویس", "🎛 امکانات دانلود"),
    ("dl_batch", "دانلود گروهی", "🎛 امکانات دانلود"),
    # AI
    ("ai_sum", "خلاصه ویدیو", "🤖 هوش مصنوعی"),
    ("ai_q", "پرسش از ویدیو", "🤖 هوش مصنوعی"),
    # voice
    ("voice", "تشخیص ویس", "🎤 تشخیص"),
    # share
    ("share", "لینک اشتراک", "🔗 اشتراک"),
    # movie
    ("movie", "فیلم‌یاب", "🎬 فیلم"),
    # personal
    ("history", "تاریخچه", "📚 شخصی"),
    ("library", "کتابخانه", "📚 شخصی"),
]

PERMS = {k: v for k, v, _ in PERM_DEFS}
PERM_CATS = {}
for k, v, c in PERM_DEFS:
    PERM_CATS.setdefault(c, []).append((k, v))

DL_SITE_PERMS = ("dl_yt", "dl_ig", "dl_tiktok", "dl_other")

# callback head -> required perm.
# "dl_any" = any download-site perm; "owner" = owner only.
_CB_PERM = {
    # generic download flow (after URL authorized)
    "findsong": "dl_any", "igms": "dl_any", "ms": "dl_any", "lyrics": "dl_any",
    "q": "dl_any", "qgo": "dl_any", "qmv": "dl_any", "queue": "dl_any",
    "pl": "dl_any", "prio": "dl_any", "cancel": "dl_any",
    "dup": "dl_any", "cmp": "dl_any", "open": "dl_any",
    "ch": "dl_any", "show_chapters": "dl_any",
    "ig_audio": "dl_any", "ig_video_menu": "dl_any", "audio_toggle": "dl_any",
    "music_search": "dl_any",
    "qt": "dl_any",  # refined below for audio/subs
    # audio-only / subtitles / clip / batch
    "clip": "dl_clip",
    "sub": "dl_subs", "sublang": "dl_subs", "subx": "dl_subs",
    "batch_mode": "dl_batch", "batch_all": "dl_batch", "batch_skip": "dl_batch",
    "bulk": "dl_batch",
    # AI
    "sum": "ai_sum",
    # share
    "sharepick": "share", "shareuses": "share", "shareexp": "share",
    # movie
    "ig_movie": "movie", "mv_retry": "movie",
    # personal
    "hist": "history",
    "lib": "library", "libsrch": "library",
}

_YT_RE = re.compile(r"(?:youtube\.com|youtu\.be)/", re.I)
_IG_RE = re.compile(r"(?:instagram\.com|instagr\.am)/", re.I)
_TT_RE = re.compile(r"(?:^|//|\.)tiktok\.com/", re.I)


def url_perm(url):
    """Which download perm a URL needs."""
    u = url or ""
    if _YT_RE.search(u):
        return "dl_yt"
    if _IG_RE.search(u):
        return "dl_ig"
    if _TT_RE.search(u):
        return "dl_tiktok"
    return "dl_other"


OWNER_IDS = set()  # set by bridge_bot from cfg.OWNER_ID
_DATA_DIR = None
_STORE = None


def _data_dir():
    global _DATA_DIR
    if _DATA_DIR is None:
        _DATA_DIR = os.environ.get("DATA_DIR", "/tmp/bridge_data")
    return _DATA_DIR


def _path():
    d = _data_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "user_access.json")


def _load():
    global _STORE
    if _STORE is not None:
        return _STORE
    _STORE = {"users": {}}
    try:
        p = _path()
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("users"), dict):
                _STORE = data
    except Exception:
        pass
    return _STORE


def _save():
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(_STORE, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def is_owner(uid):
    try:
        return uid is not None and int(uid) in OWNER_IDS
    except Exception:
        return False


def is_managed(uid):
    try:
        return str(int(uid)) in _load()["users"]
    except Exception:
        return False


def get_perms(uid):
    if is_owner(uid):
        return set(PERMS)
    try:
        u = _load()["users"].get(str(int(uid)))
        if u:
            return set(u.get("perms", ())) & set(PERMS)
    except Exception:
        pass
    return set()


def has_perm(uid, perm):
    if perm == "dl_any":
        return has_any_dl(uid)
    return perm in get_perms(uid)


def has_any_dl(uid):
    return bool(get_perms(uid) & set(DL_SITE_PERMS))


def list_users():
    return _load()["users"]


def add_user(uid, perms=(), name=""):
    uid = str(int(uid))
    users = _load()["users"]
    users[uid] = {
        "perms": sorted(set(perms) & set(PERMS)),
        "name": name or "",
        "added": time.strftime("%Y-%m-%d"),
    }
    _save()


def remove_user(uid):
    users = _load()["users"]
    if str(int(uid)) in users:
        del users[str(int(uid))]
        _save()


def set_perms(uid, perms):
    uid = str(int(uid))
    users = _load()["users"]
    if uid in users:
        users[uid]["perms"] = sorted(set(perms) & set(PERMS))
        _save()


def toggle_perm(uid, perm):
    uid = str(int(uid))
    users = _load()["users"]
    if uid in users and perm in PERMS:
        p = set(users[uid].get("perms", ()))
        if perm in p:
            p.discard(perm)
        else:
            p.add(perm)
        users[uid]["perms"] = sorted(p)
        _save()
        return sorted(p)
    return None


def cb_perm(data):
    """Required perm for a callback. 'owner' = owner-only, 'dl_any' = any dl site."""
    data = data or ""
    # qt:audio -> dl_audio, qt:subs_* -> dl_subs, else dl_any
    if data.startswith("qt:"):
        arg = data[3:].split(":", 1)[0]
        if arg == "audio":
            return "dl_audio"
        if arg.startswith("subs"):
            return "dl_subs"
        return "dl_any"
    # sumq:custom -> asking a question needs ai_q
    if data.startswith("sumq:"):
        if data.split(":", 1)[1].split(":", 1)[0] == "custom":
            return "ai_q"
        return "ai_sum"
    head = data.split(":", 1)[0]
    if head.startswith("ig_") and head not in _CB_PERM:
        return "dl_any"
    return _CB_PERM.get(head, "owner")


def perms_label(perms):
    if not perms:
        return "—"
    return "، ".join(PERMS.get(p, p) for p in sorted(perms))


# legacy -> new perm expansion (one-time migration)
_LEGACY_MAP = {
    "dl": ["dl_yt", "dl_ig", "dl_tiktok", "dl_other",
           "dl_audio", "dl_clip", "dl_subs", "dl_batch"],
    "ig": ["dl_ig"],
    "voice": ["voice"],
    "ai": ["ai_sum", "ai_q"],
    "share": ["share"],
    "movie": ["movie"],
}


def _expand(perms):
    out = set()
    for p in perms or ():
        if p in _LEGACY_MAP:
            out.update(_LEGACY_MAP[p])
        elif p in PERMS:
            out.add(p)
    return sorted(out)


def migrate_limited(limited_ids):
    """One-time: import legacy LIMITED_USERS as dl_ig users."""
    users = _load()["users"]
    changed = False
    for uid in limited_ids or ():
        try:
            key = str(int(uid))
        except Exception:
            continue
        if key not in users and not is_owner(uid):
            users[key] = {"perms": ["dl_ig"], "name": "",
                          "added": time.strftime("%Y-%m-%d"), "migrated": True}
            changed = True
    # expand any legacy perm sets already stored
    for key, info in users.items():
        old = info.get("perms", ())
        new = _expand(old)
        if sorted(old) != new:
            info["perms"] = new
            changed = True
    if changed:
        _save()
