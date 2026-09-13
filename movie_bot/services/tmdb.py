"""ماژول ارتباط با API رجا TMDb.

جستجوی فیلم/سریال، دریافت جزئیات کامل (تگلاین، بودجه، فروش، زبان‌ها،
شبکه، فصل‌ها و…)، اعتبارات (بازیگران/کارگردان)، ویدیوها (تریلر)،
مشابه‌ها، پروفایل بازیگر و فصل‌ها/قسمت‌ها — همه به‌صورت async.
"""

from __future__ import annotations

import aiohttp

from config import TMDB_API_KEY, TMDB_BASE_URL
from utils.logger import logger

_TIMEOUT = aiohttp.ClientTimeout(total=15)

# ─── نگاشت ژانر (به‌روز شده از راهنمای رسمی TMDb) ───
GENRE_EMOJI: dict[int, tuple[str, str]] = {
    28: ("🔫", "Action"),
    12: ("🏔", "Adventure"),
    16: ("🧚", "Animation"),
    35: ("😂", "Comedy"),
    80: ("💰", "Crime"),
    99: ("📖", "Documentary"),
    18: ("🎭", "Drama"),
    # 10751 در جدول movie و tv مشترک است
    10751: ("👪", "Family"),
    14: ("🧙", "Fantasy"),
    36: ("🏛️", "History"),
    27: ("😱", "Horror"),
    10402: ("🎵", "Music"),
    9648: ("🕵", "Mystery"),
    10749: ("❤️", "Romance"),
    878: ("🚀", "Sci-Fi"),
    10770: ("📺", "TV Movie"),
    53: ("🔥", "Thriller"),
    10752: ("⚔️", "War"),
    37: ("🤠", "Western"),
    10759: ("🔫", "Action & Adventure"),
    10762: ("🧒", "Kids"),
    10763: ("📰", "News"),
    10764: ("📺", "Reality"),
    10765: ("🚀", "Sci-Fi & Fantasy"),
    10766: ("🧼", "Soap"),
    10767: ("🎙️", "Talk"),
    10768: ("🏛️", "War & Politics"),
}

# نام‌های ایرانیِ رایج برای زبان‌ها
LANG_FA: dict[str, str] = {
    "en": "English", "fa": "Persian (فارسی)", "es": "Spanish", "fr": "French",
    "de": "German", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "ar": "Arabic", "hi": "Hindi", "ur": "Urdu", "tr": "Turkish",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "nl": "Dutch",
    "pl": "Polish", "sv": "Swedish", "no": "Norwegian", "da": "Danish",
    "fi": "Finnish", "el": "Greek", "he": "Hebrew", "id": "Indonesian",
    "th": "Thai", "vi": "Vietnamese", "uk": "Ukrainian", "cs": "Czech",
    "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian", "sr": "Serbian",
    "hr": "Croatian", "sk": "Slovak", "ta": "Tamil", "te": "Telugu",
    "ml": "Malayalam", "bn": "Bengali", "k00": "Kazakh", "az": "Azerbaijani",
}

# نام‌های نمایشیِ کشورها
COUNTRY_FA: dict[str, str] = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "IN": "India", "IR": "Iran", "TR": "Turkey",
    "AE": "UAE", "SA": "Saudi Arabia", "DE": "Germany", "FR": "France",
    "ES": "Spain", "IT": "Italy", "JP": "Japan", "KR": "South Korea",
    "CN": "China", "RU": "Russia", "BR": "Brazil", "MX": "Mexico",
    "AR": "Argentina", "PL": "Poland", "NL": "Netherlands", "SE": "Sweden",
    "NO": "Norway", "DK": "Denmark", "FI": "Finland", "IE": "Ireland",
    "CH": "Switzerland", "IL": "Israel", "EG": "Egypt", "GR": "Greece",
    "PT": "Portugal", "TH": "Thailand", "UA": "Ukraine", "ZA": "South Africa",
    "NG": "Nigeria", "ID": "Indonesia", "MY": "Malaysia", "PK": "Pakistan",
}


def genre_to_emoji_label(genres: list | None) -> str:
    """تبدیل لیس متصل: ایموجی + نام انگلیسی برای هر ژانر."""
    text = ""
    if not genres:
        return ""
    for g in genres:
        gid = g.get("id")
        name = (g.get("name") or "").strip()
        if gid in GENRE_EMOJI:
            em, en = GENRE_EMOJI[gid]
            text += (f"{em} {en}\n")
        else:
            text += (f"🎬 {name}\n")
    return text.rstrip("\n")


def language_names(codes: list | None) -> str:
    """نمایش نام زبان‌ها از کدهای ISO 639-1 (فارسی/لاتین)."""
    if not codes:
        return ""
    seen: list[str] = []
    for c in codes:
        name = LANG_FA.get(c) or c
        if name not in seen:
            seen.append(name)
    return ", ".join(seen)


def country_names(codes: list | None) -> str:
    """نمایش نام کشورها از کدهای ISO 3166-1."""
    if not codes:
        return ""
    seen: list[str] = []
    for c in codes:
        name = COUNTRY_FA.get(c) or c
        if name not in seen:
            seen.append(name)
    return ", ".join(seen)


async def _get(
    session: aiohttp.ClientSession, url: str, params: dict | None = None
) -> dict | None:
    """اجرای یک درخواست GET و بازگرداندن پاسخ JSON امن."""
    try:
        params = dict(params or {})
        params.setdefault("api_key", TMDB_API_KEY)
        params.setdefault("language", "en-US")
        async with session.get(url, params=params, timeout=_TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.warning("TMDB پاسخ %s داد: %s", resp.status, url)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای شبکه TMDB: %s", exc)
        return None


async def search(
    session: aiohttp.ClientSession,
    query: str,
    media_type: str,
    year: str | int | None = None,
) -> dict | None:
    """جستجوی فیلم/سریال و بازگرداندن بهترین نتیجه (با سال در صورت وجود)."""
    if not query:
        return None
    if media_type == "series":
        url = f"{TMDB_BASE_URL}/search/tv"
    elif media_type == "movie":
        url = f"{TMDB_BASE_URL}/search/movie"
    else:
        url = f"{TMDB_BASE_URL}/search/multi"
    params: dict = {"query": query}
    if media_type == "movie" and year:
        params["year"] = str(year)
    elif media_type == "series" and year:
        params["first_air_date_year"] = str(year)
    data = await _get(session, url, params)
    results = (data or {}).get("results") or []
    return results[0] if results else None


async def details(
    session: aiohttp.ClientSession, tmdb_id: int, media_type: str
) -> dict | None:
    """جزئیات کامل یک فیلم/سریال (budget/revenue/tagline/زبان‌ها/فصل‌ها…)."""
    url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}"
    return await _get(session, url)


async def external_ids(
    session: aiohttp.ClientSession, tmdb_id: int, media_type: str
) -> dict | None:
    """شناسه‌های بیرونی (imdb_id) برای فیلم/سریال."""
    url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/external_ids"
    return await _get(session, url)


async def credits(
    session: aiohttp.ClientSession, tmdb_id: int, media_type: str
) -> dict | None:
    """اعتبارات (cast + crew) یک فیلم/سریال."""
    url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/credits"
    return await _get(session, url)


async def videos(
    session: aiohttp.ClientSession, tmdb_id: int, media_type: str
) -> list:
    """ویدیوهای رسمی (cotیه تریلر) — لیست خالی در صورت نبود."""
    url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/videos"
    data = await _get(session, url)
    return (data or {}).get("results") or []


async def similar(
    session: aiohttp.ClientSession, tmdb_id: int, media_type: str, limit: int = 5
) -> list:
    """5 فیلم/سریال مشابه از TMDb."""
    url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/similar"
    data = await _get(session, url)
    return ((data or {}).get("results") or [])[:limit]


async def person(
    session: aiohttp.ClientSession, person_id: int
) -> dict | None:
    """پروفایل یک شخص (تولد، محل تولد، بیوگرافی، عکس)."""
    url = f"{TMDB_BASE_URL}/person/{person_id}"
    return await _get(session, url)


async def person_credits(
    session: aiohttp.ClientSession, person_id: int
) -> dict | None:
    """فیلم‌شناسی یک شخص (cast + crew ترکیبی)."""
    url = f"{TMDB_BASE_URL}/person/{person_id}/combined_credits"
    return await _get(session, url)


async def season(
    session: aiohttp.ClientSession, tmdb_id: int, season_num: int
) -> dict | None:
    """اطلاعات یک فصل کامل (شامل قسمت‌ها)."""
    url = f"{TMDB_BASE_URL}/tv/{tmdb_id}/season/{season_num}"
    return await _get(session, url)


def pick_trailer(video_results: list) -> str | None:
    """انتخاب تریلر رسمی، در غیر این صورت اولین تریلر موجود."""
    trailers: list[str] = []
    for v in video_results:
        st = (v.get("site") or "").lower()
        if st != "youtube":
            continue
        vtype = (v.get("type") or "").lower()
        if vtype != "trailer":
            continue
        if v.get("official") and "trailer" in (v.get("name") or "").lower():
            trailers.insert(0, v["key"])
        else:
            trailers.append(v["key"])
    if trailers:
        return f"https://youtu.be/{trailers[0]}"
    return None