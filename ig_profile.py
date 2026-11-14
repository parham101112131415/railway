# -*- coding: utf-8 -*-
"""
ig_profile — گرفتن عکس پروفایل و پرونده‌ی کامل پیج اینستاگرام برای بات آهنگ.

موتورها (هر سه با هم مرج می‌شن تا «همه‌ی» اطلاعات جمع بشه):
  ۱) instagrapi با سشن (API موبایل): اطلاعات اصلی پیج + عکس ۶۴۰
  ۲) instaloader با سشن وب (کوکی): آدرس نسخه‌ی اصلیِ آپلودشده‌ی عکس (بدون سایز)
  ۳) instagram-cli (حساب وصل‌شده‌ی Meta): فیلدهای اضافه‌ی گراف — تولد، شهر،
     تحصیلات، شغل، وضعیت رابطه، کشور، تعداد دوستان و...
فیلد گمشده توی پرونده با --- نشون داده می‌شه، هیچ ردیفی حذف نمی‌شه.

توابع:
  extract_usernames(text)      → یوزرنیم‌ها از لینک پروفایل / @منشن
  fetch_profile(username)      → دیکشنری کامل پیج یا None
  download_pic(url)            → بایت‌های عکس یا None
  pic_extension(data)          → پسوند واقعی از روی بایت‌ها
  to_file_bytes(data)          → (بایت، پسوند) مناسب فایل تلگرام (webp→PNG)
  build_caption(prof)          → کپشن کوتاه زیر فایل عکس
  build_full_dossier(prof, pic_bytes) → پرونده‌ی کامل، قالب ثابت
"""
import io
import json
import os
import re
import subprocess
import threading
import time

IG_CLI = os.environ.get("IG_CLI_PATH", "/opt/hatch/bin/instagram-cli")
IG_CLI_ACCOUNT_ID = os.environ.get("IG_CLI_ACCOUNT_ID", "17841473345086070")
_SESSION_SETTINGS = os.environ.get(
    "IG_SESSION_SETTINGS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ig_session_settings.json"),
)
_COOKIES_FILE = os.environ.get(
    "IG_COOKIES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ig_cookies.txt"),
)
_IG_WEB_USERNAME = os.environ.get("IG_WEB_USERNAME", "rip._.parham")

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?")
_MENTION_RE = re.compile(r"@([A-Za-z0-9._]{2,30})")
_RESERVED_SEGMENTS = {
    "p", "reel", "reels", "stories", "explore", "accounts", "direct",
    "tv", "www", "about", "developer", "legal", "web", "graphql",
}

_cache = {}
_CACHE_TTL = 1800  # نیم ساعت

# ---- موتور سشن موبایل (instagrapi) ----
_cl = None
_cl_lock = threading.Lock()
_cl_dead = False


def _get_client():
    global _cl, _cl_dead
    if _cl_dead:
        return None
    if _cl is not None:
        return _cl
    if not os.path.isfile(_SESSION_SETTINGS):
        _cl_dead = True
        return None
    try:
        from instagrapi import Client
        c = Client()
        c.load_settings(_SESSION_SETTINGS)
        _cl = c
        return _cl
    except Exception:
        _cl_dead = True
        return None


def _fetch_profile_session(username):
    cl = _get_client()
    if cl is None:
        return None
    with _cl_lock:
        u = cl.user_info_by_username(username)
    try:
        raw = u.model_dump()
    except Exception:
        raw = u.dict()
    links = []
    for l in (raw.get("bio_links") or []):
        if isinstance(l, dict):
            links.append(l.get("url") or l.get("link_url") or "")
        else:
            links.append(str(l))
    bc = raw.get("broadcast_channel")
    bc_str = ""
    if bc:
        if isinstance(bc, dict):
            bc_str = str(bc.get("id") or bc.get("title") or "") or "دارد"
        else:
            bc_str = "دارد"
    return {
        "username": raw.get("username") or username,
        "name": raw.get("full_name") or "",
        "bio": raw.get("biography") or "",
        "follower_count": raw.get("follower_count"),
        "following_count": raw.get("following_count"),
        "post_count": raw.get("media_count"),
        "is_private": bool(raw.get("is_private")),
        "is_verified": bool(raw.get("is_verified")),
        "is_anonymous_pic": False,
        "profile_pic_url": str(raw.get("profile_pic_url_hd") or raw.get("profile_pic_url") or ""),
        # فیلدهای اضافه‌ی موتور سشن
        "pk": raw.get("pk"),
        "account_type_code": raw.get("account_type"),
        "is_business": bool(raw.get("is_business")),
        "business_category": raw.get("business_category_name") or raw.get("category_name") or raw.get("category") or "",
        "external_url": raw.get("external_url") or "",
        "bio_links": [x for x in links if x],
        "public_email": raw.get("public_email") or "",
        "public_phone": ((raw.get("public_phone_country_code") or "") + " " + (raw.get("public_phone_number") or "")).strip(),
        "contact_phone": raw.get("contact_phone_number") or "",
        "business_contact_method": raw.get("business_contact_method") or "",
        "city_name": raw.get("city_name") or "",
        "address_street": raw.get("address_street") or "",
        "zip": raw.get("zip") or "",
        "latitude": raw.get("latitude"),
        "longitude": raw.get("longitude"),
        "interop_fbid": raw.get("interop_messaging_user_fbid"),
        "threads_username": raw.get("text_post_app_badge_label") or "",
        "broadcast_channel": bc_str,
    }


def _fetch_profile_cli(username):
    r = subprocess.run(
        [IG_CLI, "user-profile", "--account-id", IG_CLI_ACCOUNT_ID,
         "--username", username],
        capture_output=True, text=True, timeout=90,
    )
    if r.returncode != 0:
        return None
    data = json.loads(r.stdout)
    profiles = data.get("profiles") or []
    if not profiles:
        return None
    p = profiles[0]
    links = []
    for l in (p.get("bio_links") or []):
        if isinstance(l, dict):
            links.append(l.get("url") or l.get("link") or l.get("href") or "")
        else:
            links.append(str(l))
    bi = p.get("business_info") or {}
    if not isinstance(bi, dict):
        bi = {}
    return {
        "username": p.get("username") or username,
        "name": p.get("name") or "",
        "bio": p.get("bio") or "",
        "follower_count": p.get("follower_count"),
        "following_count": p.get("following_count"),
        "post_count": p.get("post_count"),
        "is_private": bool(p.get("is_private")),
        "is_verified": bool(p.get("is_verified")),
        "is_anonymous_pic": bool(p.get("has_anonymous_profile_picture")),
        "profile_pic_url": p.get("profile_pic_url") or "",
        # فیلدهای اضافه‌ی گراف (فقط این موتور داره)
        "graph_user_id": p.get("user_id"),
        "account_type_str": p.get("account_type") or "",
        "profile_url": p.get("profile_url") or "",
        "website": p.get("website") or "",
        "bio_links": [x for x in links if x],
        "birthday": p.get("birthday") or "",
        "current_city": p.get("current_city") or "",
        "hometown": p.get("hometown") or "",
        "education": p.get("education") or "",
        "work": p.get("work") or "",
        "relationship_status": p.get("relationship_status") or "",
        "country": p.get("country") or "",
        "friend_count": p.get("friend_count"),
        "business_info": bi,
    }


def _load_netscape_cookies(path):
    cookies = {}
    for line in open(path):
        line = line.rstrip("\n")
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


def _fetch_original_pic_url(username):
    """instaloader با سشن وب → آدرس نسخه‌ی اصلی عکس (بدون کوچیک‌سازی) یا None."""
    try:
        if not os.path.isfile(_COOKIES_FILE):
            return None
        ck = _load_netscape_cookies(_COOKIES_FILE)
        if not ck.get("sessionid"):
            return None
        import instaloader
        loader = instaloader.Instaloader(quiet=True, max_connection_attempts=1)
        sess = loader.context._session
        sess.cookies.clear()
        for k, v in ck.items():
            sess.cookies.set(k, v, domain=".instagram.com")
        loader.context.username = _IG_WEB_USERNAME
        prof = instaloader.Profile.from_username(loader.context, username)
        return prof.profile_pic_url or None
    except Exception:
        return None


def pic_extension(data):
    """پسوند درست فایل عکس از روی بایت‌هاش (webp/jpg/png)."""
    try:
        from PIL import Image
        fmt = Image.open(io.BytesIO(data)).format
        return {"WEBP": ".webp", "JPEG": ".jpg", "PNG": ".png"}.get(fmt, ".jpg")
    except Exception:
        return ".jpg"


def to_file_bytes(data):
    """(بایت، پسوند) مناسب ارسال به‌صورت فایل تلگرام برمی‌گردونه.

    تلگرام فایل webp رو استیکر نشون می‌ده و کپشنش رو قایم می‌کنه؛ برای همین
    webp بدون افت (lossless) به PNG تبدیل می‌شه — پیکسل‌ها عیناً همونن.
    بقیه فرمت‌ها دست‌نخورده برمی‌گردن.
    """
    ext = pic_extension(data)
    if ext == ".webp":
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue(), ".png"
        except Exception:
            return data, ext
    return data, ext


def _valid_username(u):
    if not u or not _USERNAME_RE.match(u):
        return False
    if u.startswith(".") or u.endswith(".") or ".." in u:
        return False
    return True


def extract_usernames(text):
    """یوزرنیم‌ها رو از لینک پروفایل اینستا و @منشن‌ها درمیاره (حداکثر ۵ تا)."""
    if not text:
        return []
    found = []
    for m in _URL_RE.finditer(text):
        seg = m.group(1)
        if seg.lower() in _RESERVED_SEGMENTS:
            continue
        if _valid_username(seg) and seg not in found:
            found.append(seg)
    for m in _MENTION_RE.finditer(text):
        u = m.group(1)
        if _valid_username(u) and u not in found:
            found.append(u)
    return found[:5]


def _merge(base, extra):
    """extra فقط جاهای خالی base رو پر می‌کنه (مقدار واقعی ۰ و False حفظ می‌شن)."""
    if base is None:
        return extra
    if extra is None:
        return base
    out = dict(base)
    for k, v in extra.items():
        cur = out.get(k)
        if cur is None or cur == "" or cur == []:
            if v is not None and v != "" and v != []:
                out[k] = v
    return out


def fetch_profile(username):
    """هر سه موتور مرج می‌شن: سشن (اصلی) + CLI (فیلدهای گراف) + عکس اصلی وب."""
    now = time.time()
    hit = _cache.get(username.lower())
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    prof = None
    try:
        prof = _fetch_profile_session(username)
    except Exception:
        prof = None
    try:
        cli = _fetch_profile_cli(username)
    except Exception:
        cli = None
    prof = _merge(prof, cli)
    if prof is not None:
        prof.update(_fetch_activity(prof.get("pk"), prof.get("post_count")))
        prof["wayback_first"] = _fetch_wayback(username)
        orig = _fetch_original_pic_url(username)
        if orig:
            prof["profile_pic_url"] = orig
        _cache[username.lower()] = (now, prof)
    return prof


# ---------- منابع اضافه: فعالیت، آرشیو، تاریخ عکس ----------

_IG_EPOCH_MS = 1318636800000


def _snowflake_pic_date(url):
    """تاریخ آپلود عکس فعلی از توکن اسنوفلیک اسم فایلش (واقعی، نه حدس)."""
    m = re.search(r"_(\d{16,})_n\.", url or "")
    if not m:
        return ""
    try:
        ts = (int(m.group(1)) >> 23) + _IG_EPOCH_MS
        import datetime
        dt = datetime.datetime.utcfromtimestamp(ts / 1000)
        if 2011 <= dt.year <= 2030:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def _fetch_activity(pk, media_count):
    """تاریخ اولین/آخرین پست از API موبایل — فقط وقتی پست داره و پیج بازه."""
    out = {"first_post_at": "", "last_post_at": ""}
    if not pk or not media_count:
        return out
    cl = _get_client()
    if cl is None:
        return out
    try:
        with _cl_lock:
            latest = cl.user_medias(pk, 1)
        if latest:
            t = getattr(latest[0], "taken_at", None)
            if t:
                out["last_post_at"] = t.strftime("%Y-%m-%d")
        if media_count <= 50:
            with _cl_lock:
                medias = cl.user_medias(pk, int(media_count))
            times = [m.taken_at for m in medias if getattr(m, "taken_at", None)]
            if times:
                out["first_post_at"] = min(times).strftime("%Y-%m-%d")
    except Exception:
        pass
    return out


def _fetch_wayback(username):
    """اولین اسنپ‌شات آرشیو archive.org از صفحه‌ی پروفایل — شاهد آرشیوی."""
    try:
        import requests
        r = requests.get(
            "http://web.archive.org/cdx/search/cdx",
            params={"url": f"instagram.com/{username}", "output": "json",
                    "fl": "timestamp", "filter": "statuscode:200",
                    "collapse": "timestamp:8", "limit": "1"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=12,
        )
        rows = r.json()
        if rows and len(rows) > 1 and rows[1]:
            ts = str(rows[1][0])
            return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    except Exception:
        pass
    return ""


def download_pic(url):
    """بایت‌های عکس پروفایل رو دانلود می‌کنه. خطا → None."""
    if not url:
        return None
    try:
        import requests
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None


def _fmt_num(n):
    if n is None:
        return "—"
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def build_caption(prof):
    lines = []
    title = f"📸 {prof['name']}" if prof.get("name") else "📸"
    lines.append(f"{title} | @{prof['username']}")
    lines.append(
        f"👥 فالوور {_fmt_num(prof.get('follower_count'))}"
        f" • فالووینگ {_fmt_num(prof.get('following_count'))}"
        f" • پست {_fmt_num(prof.get('post_count'))}"
    )
    flags = []
    flags.append("🔒 پرایوت" if prof.get("is_private") else "🌐 پابلیک")
    if prof.get("is_verified"):
        flags.append("✅ وریفای‌شده")
    if prof.get("is_anonymous_pic"):
        flags.append("🖼 عکس پیش‌فرض اینستا")
    lines.append(" • ".join(flags))
    if prof.get("bio"):
        lines.append(f"📝 {prof['bio']}")
    return "\n".join(lines)


# ---------- پرونده‌ی کامل (قالب ثابت — فیلد گمشده = ---) ----------

def _v(x):
    """مقدار نمایشی: خالی/None → ---"""
    if x is None or x == "" or x == [] or x == {}:
        return "---"
    return str(x)


_ACCOUNT_TYPES = {1: "شخصی", 2: "بیزینسی", 3: "کریتور"}


def _account_type(prof):
    s = prof.get("account_type_str")
    if s:
        return {"personal": "شخصی", "business": "بیزینسی", "creator": "کریتور"}.get(str(s).lower(), str(s))
    code = prof.get("account_type_code")
    if code in _ACCOUNT_TYPES:
        return _ACCOUNT_TYPES[code]
    if prof.get("is_business"):
        return "بیزینسی"
    return "---"


def _pic_media_id(url):
    m = re.search(r"/(\d+_\d+)_\d+_n\.", url or "")
    return m.group(1) if m else "---"


def _pic_meta(pic_bytes):
    if not pic_bytes:
        return "---"
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(pic_bytes))
        kb = len(pic_bytes) / 1024
        return f"{im.size[0]}×{im.size[1]} • {im.format} • {kb:.1f} KB"
    except Exception:
        return "---"


# ---------- تخمین تاریخ ساخت از روی آیدی عددی (درون‌یابی لنگرها) ----------
# لنگرها: (pk, (سال, ماه, روز)) — تاریخ عضویت‌های مستند و عمومیِ اکانت‌های معروف.
# آیدی‌های اینستا تقریباً به ترتیب زمان ساخت تخصیص داده می‌شن؛ بین دو لنگر
# درون‌یابی خطی می‌شه و بازه‌ی عدم‌قطعیت = خود بازه‌ی لنگرها نمایش داده می‌شه.
_JOIN_ANCHORS = [
    (173560420, (2012, 10, 1)),
    (247944034, (2012, 11, 6)),
    (427553890, (2013, 6, 1)),
    (1550693326, (2014, 11, 12)),
    (3046235065, (2016, 3, 19)),
    (3132929984, (2017, 12, 14)),
    (6970394928, (2018, 1, 25)),
    (21388754496, (2019, 10, 15)),
    (21518784714, (2019, 11, 4)),
    (26686769135, (2020, 5, 1)),
    (42241297565, (2020, 9, 24)),
    (50344278754, (2021, 12, 6)),
    (50557904497, (2021, 12, 6)),
    (50742921191, (2021, 12, 6)),
    (51224656988, (2022, 7, 22)),
    (59092235490, (2023, 8, 1)),
    (72005443866, (2025, 7, 15)),
]
_ANCHOR_SRC = "درون‌یابی آیدی بین عضویت‌های مستند اکانت‌های معروف"


def _d2ord(t):
    import datetime
    return datetime.date(*t).toordinal()


def estimate_joined(pk):
    """تخمین تاریخ ساخت از pk → رشته‌ی نمایشی با بازه، یا "" اگه نشد."""
    if not pk or len(_JOIN_ANCHORS) < 2:
        return ""
    pts = sorted(_JOIN_ANCHORS)
    pk = int(pk)
    import datetime
    if pk <= pts[0][0]:
        lo, hi = pts[0], pts[1]
    elif pk >= pts[-1][0]:
        lo, hi = pts[-2], pts[-1]
    else:
        for i in range(len(pts) - 1):
            if pts[i][0] <= pk <= pts[i + 1][0]:
                lo, hi = pts[i], pts[i + 1]
                break
    if pk < pts[0][0]:
        return f"{datetime.date(*pts[0][1]).strftime('%Y-%m')} یا قدیمی‌تر (تخمینی)"
    span = hi[0] - lo[0]
    frac = 0.0 if span == 0 else (pk - lo[0]) / span
    est = _d2ord(lo[1]) + frac * (_d2ord(hi[1]) - _d2ord(lo[1]))
    est_d = datetime.date.fromordinal(int(round(est)))
    lo_d = datetime.date(*lo[1]).strftime("%Y-%m")
    hi_d = datetime.date(*hi[1]).strftime("%Y-%m")
    return f"~{est_d.strftime('%Y-%m')} (بین {lo_d} تا {hi_d} — تخمینی)"


def _ratio(prof):
    f, g = prof.get("follower_count"), prof.get("following_count")
    if f is None or not g:
        return "---"
    return f"{f / g:.2f}"


def build_full_dossier(prof, pic_bytes=None):
    """پرونده‌ی کامل پیج — همه‌ی ردیف‌ها همیشه هستن، گمشده‌ها --- می‌شن."""
    uname = prof.get("username") or ""
    lat, lng = prof.get("latitude"), prof.get("longitude")
    coords = f"{lat}, {lng}" if lat is not None and lng is not None else "---"
    links = prof.get("bio_links") or []
    links_str = " | ".join(links) if links else "---"
    bi = prof.get("business_info") or {}
    bi_str = " | ".join(f"{k}: {v}" for k, v in bi.items() if v) if bi else "---"
    threads = prof.get("threads_username") or uname
    rows = [
        ("👤 نام کامل", _v(prof.get("name"))),
        ("🆔 یوزرنیم", f"@{uname}" if uname else "---"),
        ("🔢 آیدی عددی (pk)", _v(prof.get("pk"))),
        ("🪪 آیدی گراف (FB)", _v(prof.get("graph_user_id"))),
        ("🧵 آیدی تردز (interop)", _v(prof.get("interop_fbid"))),
        ("🏷 نوع اکانت", _account_type(prof)),
        ("🏢 دسته‌بندی بیزینس", _v(prof.get("business_category"))),
        ("📝 بایو", _v(prof.get("bio"))),
        ("🔗 وب‌سایت", _v(prof.get("website") or prof.get("external_url"))),
        ("🔗 لینک‌های بایو", links_str),
        ("👥 فالوور", _fmt_num(prof.get("follower_count")) if prof.get("follower_count") is not None else "---"),
        ("➡️ فالووینگ", _fmt_num(prof.get("following_count")) if prof.get("following_count") is not None else "---"),
        ("🎞 پست", _fmt_num(prof.get("post_count")) if prof.get("post_count") is not None else "---"),
        ("📅 تاریخ ساخت اکانت", _v(prof.get("joined_date") or estimate_joined(prof.get("pk")))),
        ("📸 اولین پست", _v(prof.get("first_post_at"))),
        ("📸 آخرین پست", _v(prof.get("last_post_at"))),
        ("👨‍👩‍👧 دوستان (فیسبوک)", _fmt_num(prof.get("friend_count")) if prof.get("friend_count") is not None else "---"),
        ("🔒 وضعیت پیج", "پرایوت" if prof.get("is_private") else "پابلیک"),
        ("✅ وریفای", "بله" if prof.get("is_verified") else "خیر"),
        ("🎂 تولد", _v(prof.get("birthday"))),
        ("📍 شهر فعلی", _v(prof.get("current_city"))),
        ("🏠 زادگاه", _v(prof.get("hometown"))),
        ("🎓 تحصیلات", _v(prof.get("education"))),
        ("💼 شغل", _v(prof.get("work"))),
        ("❤️ وضعیت رابطه", _v(prof.get("relationship_status"))),
        ("🌍 مکان ساخت اکانت (کشور)", _v(prof.get("country"))),
        ("🏙 شهر (بیزینس)", _v(prof.get("city_name"))),
        ("🗺 آدرس (بیزینس)", _v(prof.get("address_street"))),
        ("📮 کد پستی", _v(prof.get("zip"))),
        ("🧭 مختصات", coords),
        ("📧 ایمیل عمومی", _v(prof.get("public_email"))),
        ("📱 تلفن عمومی", _v(prof.get("public_phone"))),
        ("☎️ تلفن تماس", _v(prof.get("contact_phone"))),
        ("💬 روش تماس بیزینس", _v(prof.get("business_contact_method"))),
        ("🏢 اطلاعات بیزینس", bi_str),
        ("🧵 تردز", f"@{threads}" if threads else "---"),
        ("📺 کانال برادکست", _v(prof.get("broadcast_channel"))),
        ("🖼 آیدی مدیای عکس", _pic_media_id(prof.get("profile_pic_url"))),
        ("📐 فایل اصلی عکس", _pic_meta(pic_bytes)),
        ("👤 یوزرنیم‌های قبلی", _v(prof.get("former_usernames"))),
        ("⏰ تاریخ آپلود عکس فعلی", _v(_snowflake_pic_date(prof.get("profile_pic_url")))),
        ("🗄 اولین آرشیو (Wayback)", _v(prof.get("wayback_first"))),
        ("📊 نسبت فالوور به فالووینگ", _ratio(prof)),
        ("🧵 لینک تردز", f"https://www.threads.net/@{prof.get('threads_username')}" if prof.get("threads_username") else "---"),
        ("🌐 لینک پروفایل", prof.get("profile_url") or (f"https://www.instagram.com/{uname}" if uname else "---")),
        ("🔗 لینک مستقیم عکس", _v(prof.get("profile_pic_url"))),
    ]
    body = "\n".join(f"{label}: {value}" for label, value in rows)
    return f"📋 پرونده کامل اینستاگرام — @{uname}\n\n{body}"
