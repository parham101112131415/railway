"""Per-user access control for bridge_bot.

Owner manages users from inside the bot (/users): add by numeric Telegram ID,
toggle per-feature permissions, remove. Stored in DATA_DIR/user_access.json.

Permissions:
    dl    — full downloader (YouTube, Instagram, TikTok, ...)
    ig    — Instagram reels only (legacy limited-user flow)
    voice — voice/audio song recognition
    ai    — AI features (video summary, questions)
    share — share-link creation
    movie — movie finder
"""

import json
import os
import time

PERMS = {
    "dl": "📥 دانلود",
    "ig": "📸 فقط ریلز اینستا",
    "voice": "🎤 تشخیص ویس",
    "ai": "🤖 هوش مصنوعی",
    "share": "🔗 لینک اشتراک",
    "movie": "🎬 فیلم‌یاب",
}

# callback head -> required perm ("owner" = owner only)
_CB_PERM = {
    # downloader flow
    "findsong": "dl", "igms": "dl", "ms": "dl", "lyrics": "dl",
    "q": "dl", "qt": "dl", "qgo": "dl", "qmv": "dl", "queue": "dl",
    "batch_mode": "dl", "batch_all": "dl", "batch_skip": "dl", "bulk": "dl",
    "clip": "dl", "dup": "dl", "pl": "dl", "prio": "dl", "cancel": "dl",
    "cmp": "dl", "open": "dl", "ch": "dl", "show_chapters": "dl",
    "sub": "dl", "sublang": "dl", "subx": "dl",
    "ig_audio": "dl", "ig_video_menu": "dl", "audio_toggle": "dl",
    "music_search": "dl",
    # AI
    "sum": "ai", "sumq": "ai",
    # share links
    "sharepick": "share", "shareuses": "share", "shareexp": "share",
    # movie finder
    "ig_movie": "movie", "mv_retry": "movie",
}

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
    """Return set of perm keys. Owner gets all."""
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
    return perm in get_perms(uid)


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
    """Required perm for a callback. 'owner' = owner-only."""
    data = data or ""
    head = data.split(":", 1)[0]
    if head.startswith("ig_") and head not in ("ig_audio", "ig_video_menu", "ig_movie"):
        return "dl"
    return _CB_PERM.get(head, "owner")


def perms_label(perms):
    if not perms:
        return "—"
    return "، ".join(PERMS.get(p, p) for p in sorted(perms))


def migrate_limited(limited_ids):
    """One-time: import legacy LIMITED_USERS as ig-perm users."""
    users = _load()["users"]
    changed = False
    for uid in limited_ids or ():
        try:
            key = str(int(uid))
        except Exception:
            continue
        if key not in users and not is_owner(uid):
            users[key] = {"perms": ["ig"], "name": "", "added": time.strftime("%Y-%m-%d"),
                          "migrated": True}
            changed = True
    if changed:
        _save()
