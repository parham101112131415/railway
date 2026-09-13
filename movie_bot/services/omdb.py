"""ماژول ارتباط با API OMDB.

دریافت اطلاعات تکمیلی مثل امتیاز IMDb، گیشه، جایزه‌ها، مدت زمان،
کشور، ژانر و بازیگران بر اساس عنوان یا شناسه imdb.
"""

from __future__ import annotations

import aiohttp

from config import OMDB_API_KEY, OMDB_BASE_URL
from utils.logger import logger

_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def _get(session: aiohttp.ClientSession, params: dict) -> dict | None:
    """درخواست aiohttp امن به OMDB."""
    try:
        async with session.get(OMDB_BASE_URL, params=params, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("Response") == "True":
                return data
            return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای شبکه OMDB: %s", exc)
        return None


async def search_by_title(
    session: aiohttp.ClientSession, title: str, media_type: str | None = None
) -> dict | None:
    """جستجو با عنوان و (اختیاری) نوع رسانه."""
    params: dict = {"apikey": OMDB_API_KEY, "t": title, "plot": "full"}
    if media_type in ("movie", "series"):
        params["type"] = media_type
    return await _get(session, params)


async def search_by_imdb(session: aiohttp.ClientSession, imdb_id: str) -> dict | None:
    """جستجو بر اساس شناسه imdb."""
    return await _get(session, {"apikey": OMDB_API_KEY, "i": imdb_id})


def extract_fields(data: dict) -> dict:
    """استخراج فیلدهای مهم از پاسخ OMDB به یک دیکشنری تمیز."""
    if not data:
        return {}
    return {
        "imdb_rating": data.get("imdbRating") or "",
        "metascore": data.get("Metascore") or "",
        "rotten": _rotten_score(data.get("Ratings")) or "",
        "awards": data.get("Awards") or "",
        "actors": data.get("Actors") or "",
        "director": data.get("Director") or "" ,
        "writer": data.get("Writer") or "",
        "runtime": data.get("Runtime") or "",
        "country": data.get("Country") or "",
        "genre": data.get("Genre") or "",
        "language": data.get("Language") or "",
        "rated": data.get("Rated") or "",
        "production": data.get("Production") or "",
        "boxoffice": data.get("BoxOffice") or "",
        "released": data.get("Released") or "",
        "type": data.get("Type") or "",
        "total_seasons": data.get("totalSeasons") or "",
        "plot": data.get("Plot") or "",
        "poster": data.get("Poster") or "",
        "imdb_id": data.get("imdbID") or "",
        "title": data.get("Title") or "",
        "year": data.get("Year") or "",
    }


def _rotten_score(ratings: list | None) -> str:
    """استخراج امتیاز Rotten Tomatoes از لیست Ratings در صورت وجود."""
    if not ratings:
        return ""
    for item in ratings:
        if item.get("Source") == "Rotten Tomatoes":
            return item.get("Value") or ""
    return ""