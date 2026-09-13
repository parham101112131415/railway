"""ماژول هماهنگ‌کنندهٔ سرویس‌های اطلاعات فیلم.

پس از تشخیص عنوان توسط Gemini، اطلاعات نهایی فقط از TMDb و OMDB
ساخته می‌شود (هیچ متنی از Gemini به کاربر نمایش داده نمی‌شود).
TMDb منبع اصلی و OMDB کامل‌کننده است؛ اگر TMDb نتیجه نداد، OMDB
به‌تنهایی نتیجه را می‌سازد.
"""

from __future__ import annotations

import asyncio

import aiohttp

from services import omdb, tmdb
from services.gemini import translate_to_farsi
from services.poster import download_poster
from utils.logger import logger

# تصویر جایگزین وقتی پوستری در دسترس نیست
FALLBACK_POSTER_URL = "https://via.placeholder.com/300x450/1a1a1a/ffffff?text=No+Poster"


def _human_runtime(minutes: str | int | None) -> str:
    """تبدیل دقیقه به «X ساعت و Y دقیقه» (یا فقط دقیقه/ساعت)."""
    if not minutes or str(minutes).lower() in {"n/a", "none", "نامشخص"}:
        return "نامشخص"
    try:
        total = int(str(minutes).replace("min", "").strip())
    except (TypeError, ValueError):
        return str(minutes)
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} ساعت و {m} دقیقه"
    if h:
        return f"{h} ساعت"
    return f"{m} دقیقه"


async def build_movie_info(detection: dict, reel_url: str | None = None) -> dict | None:
    """ساخت اطلاعات نهایی از روی خروجی JSON تشخیص Gemini.

    از `detection` علاوه بر title/original_title، لیست `alternative` هم
    استفاده می‌شود: اگر حدس اول در TMDb/OMDB پیدا نشد، حدس‌های بعدی هم
    امتحان می‌شوند (مهم برای انیمه/فیلم‌هایی که اسم اصلی و ترجمه‌شده فرق دارد).
    """
    title = detection.get("title") or detection.get("original_title")
    year = detection.get("release_year") or detection.get("year") or ""
    d_type = (detection.get("type") or "").lower()
    media_type: str | None = {"movie": "movie", "series": "tv"}.get(d_type)
    confidence = float(detection.get("confidence") or 0)

    if not title:
        return None

    # صف اسم‌ها برای امتحان: حدس اول، original_title (اگر فرق داشت)، بعد alternative ها
    candidates: list[str] = [title]
    orig = detection.get("original_title")
    if orig and orig != title:
        candidates.append(orig)
    for alt in detection.get("alternative") or []:
        alt = (alt or "").strip()
        if alt and alt not in candidates:
            candidates.append(alt)

    logger.info("کاندیدهای عنوان برای جستجو: %s", candidates)

    async with aiohttp.ClientSession() as session:
        for i, cand in enumerate(candidates):
            info = await _build(session, cand, media_type, year, confidence, reel_url)
            if info:
                if i > 0:
                    logger.info("با حدس جایگزین #%d پیدا شد: %s", i, cand)
                return info
        logger.warning("هیچ‌کدام از کاندیدها در TMDb/OMDB پیدا نشدند: %s", candidates)
        return None


async def build_movie_info_by_id(
    tmdb_id: int, tmdb_type: str, reel_url: str | None = None
) -> dict | None:
    """ساخت کارت کامل از روی شناسه TMDb (برای دکمه‌های بازگشت/مشابه)."""
    async with aiohttp.ClientSession() as session:
        d = await tmdb.details(session, tmdb_id, tmdb_type)
        if not d:
            return None
        title = (
            d.get("name")
            if tmdb_type == "tv"
            else d.get("title") or d.get("original_title")
        )
        year = (
            (d.get("first_air_date") or "")[:4]
            if tmdb_type == "tv"
            else (d.get("release_date") or "")[:4]
        )
        if not title:
            return None
        return await _build(
            session, title, tmdb_type, year, 0, reel_url, tmdb_id=tmdb_id
        )


async def _build(
    session: aiohttp.ClientSession,
    title: str,
    media_type: str | None,
    year: str,
    confidence: float = 0,
    reel_url: str | None = None,
    tmdb_id: int | None = None,
) -> dict | None:
    """پایپلاین واقعی: TMDb (در صورت موجود) + OMDB (تکمیل‌کننده)."""
    # ─── ۱) TMDb: جستجو یا استفاده از شناسه دانسته ───
    tmd: dict = {}
    tmdb_type: str | None = None
    if tmdb_id is not None and media_type in ("movie", "tv"):
        tmd = await tmdb.details(session, tmdb_id, media_type) or {}
        tmdb_type = media_type
    else:
        found: dict | None = None
        if media_type in ("movie", "tv"):
            found = await tmdb.search(session, title, media_type, year)
        else:
            for mt in ("movie", "tv"):
                found = await tmdb.search(session, title, mt, year)
                if found:
                    tmdb_type = mt
                    break
        if found:
            tmdb_type = tmdb_type or media_type
            tmd = await tmdb.details(session, found["id"], tmdb_type) or found

    # ─── استخراج فیلدهای TMDb ───
    tmdb_id_final: int | None = tmd.get("id") if tmd else None
    imdb_id = ""
    trailer_url: str | None = None
    cast_top: list[dict] = []
    director_names = ""
    if tmd and tmdb_id_final and tmdb_type:
        ext = await tmdb.external_ids(session, tmdb_id_final, tmdb_type)
        imdb_id = (ext or {}).get("imdb_id") or ""

        # -==> تریلر
        try:
            vids = await tmdb.videos(session, tmdb_id_final, tmdb_type)
            trailer_url = tmdb.pick_trailer(vids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("خطا در دریافت تریلر: %s", exc)

        # اعتبارات (بازیگران/کارگردان)
        try:
            cr = await tmdb.credits(session, tmdb_id_final, tmdb_type)
        except Exception as exc:  # noqa: BLE001
            cr = None
            logger.warning("خطا در اعتبارات TMDb: %s", exc)
        if cr:
            for c in (cr.get("cast") or [])[:5]:
                cast_top.append(
                    {
                        "id": c.get("id"),
                        "name": c.get("name") or c.get("original_name") or "",
                        "character": c.get("character") or "",
                    }
                )
            dnames = [
                m.get("name")
                for m in (cr.get("crew") or [])
                if (m.get("job") or "").strip().lower() == "director"
            ]
            director_names = ", ".join(dict.fromkeys([n for n in dnames if n]))

    # ─── ۲) OMDB: اول با imdb، بعد با عنوان ───
    omdb_data: dict | None = None
    if imdb_id:
        try:
            omdb_data = await omdb.search_by_imdb(session, imdb_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("خطا در OMDB (imdb): %s", exc)
    if not omdb_data:
        omdb_t = "series" if tmdb_type == "tv" else ("movie" if tmdb_type else None)
        try:
            omdb_data = await omdb.search_by_title(session, title, omdb_t)
        except Exception as exc:  # noqa: BLE001
            logger.warning("خطا در OMDB (title): %s", exc)

    om = omdb.extract_fields(omdb_data or {})

    # ─── ۳) ادغام نهایی (اولویت: TMDB برای فیلدهای روشن، OMDB برای متن) ───
    is_tv = tmdb_type == "tv" or (not tmdb_type and om.get("type") == "series")

    display_title = (
        om.get("title")
        or (tmd.get("original_title") if tmd else "")
        or (tmd.get("name") if tmd else "")
        or title
    )

    final_year = (
        (om.get("year") or "")
        or ((tmd.get("release_date") or tmd.get("first_air_date") or "")[:4] if tmd else "")
        or str(year)
    )
    final_imdb = om.get("imdb_id") or imdb_id
    if not tmdb_id_final and not final_imdb:
        return None

    # ژانر (ترجیح TMDb → ایموجی؛ fallback به متن OMDB)
    genre_line = ""
    if tmd and tmd.get("genres"):
        genre_line = tmdb.genre_to_emoji_label(tmd.get("genres"))
    elif om.get("genre"):
        genre_line = "".join(f"\n{m}" for m in _genre_emoji_from_text(om["genre"])) or om["genre"]

    # مدت زمان
    runtime_line = "نامشخص"
    if is_tv:
        ep_run = (tmd or {}).get("episode_run_time") or []
        if isinstance(ep_run, list) and ep_run:
            avg = round(sum(int(x) for x in ep_run) / len(ep_run))
            runtime_line = f"{avg} دقیقه"
        elif om.get("runtime") and om["runtime"] not in ("N/A", "n/a"):
            runtime_line = f"{om['runtime'].replace(' min', ' دقیقه')}"
    else:
        mins = (tmd or {}).get("runtime")
        runtime_line = _human_runtime(mins if mins else om.get("runtime"))

    # کشورها/زبان‌ها: اول OMDB (اسم کامل)، بعد TMDb
    countries = om.get("country") or (
        tmdb.country_names(
            [c.get("iso_3166_1") for c in (tmd or {}).get("production_countries") or []]
        )
    )
    languages = om.get("language") or (
        tmdb.language_names(
            [c.get("iso_639_1") for c in (tmd or {}).get("spoken_languages") or []]
        )
        if tmd
        else ""
    )

    # بودجه/فروش
    budget = ""
    revenue = ""
    if tmd:
        if tmd.get("budget"):
            budget = f"${tmd['budget']:,}"
        if tmd.get("revenue"):
            revenue = f"${tmd['revenue']:,}"

    production = om.get("production") or ", ".join(
        [c.get("name") for c in (tmd or {}).get("production_companies") or []][:3]
    )

    # سریال: فصل‌ها/قسمت‌ها/وضعیت/شبکه/اولین و آخرین قسمت
    seasons_count = episodes_count = status_tv = network = first_air = last_air = ""
    if is_tv:
        seasons_count = str((tmd or {}).get("number_of_seasons") or "")
        episodes_count = str((tmd or {}).get("number_of_episodes") or "")
        status_raw = (tmd or {}).get("status") or ""
        status_tv = {"Ended": "پایان یافته", "Returning Series": "در حال پخش",
                     "Canceled": "لغو شده", "In Production": "در حال تولید"}.get(
            status_raw, status_raw
        )
        first_air = (tmd or {}).get("first_air_date") or ""
        last_air = (tmd or {}).get("last_air_date") or ""
        networks = ", ".join(
            [n.get("name") for n in (tmd or {}).get("networks") or []][:3]
        )
        om_seasons = om.get("total_seasons") or ""
    else:
        last_air = ""
        networks = ""

    # انتخاب پوستر (اول OMDB بعد TMDB)
    poster_path_t = (tmd or {}).get("poster_path") or ""
    final_poster_url = (
        om.get("poster")
        or (f"https://image.tmdb.org/t/p/w500{poster_path_t}" if poster_path_t else "")
        or FALLBACK_POSTER_URL
    )
    poster_local = None
    try:
        poster_local = await download_poster(
            session, final_poster_url, display_title, final_imdb or str(tmdb_id_final or "")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("خطا در دانلود پوستر: %s", exc)

    poster_hd = f"https://image.tmdb.org/t/p/original{poster_path_t}" if poster_path_t else ""

    # ترجمهٔ شعار و خلاصه به فارسی (سقف زمانی سخت تا در صورت کندی/قطعی
    # سرویس‌های ترجمه، کل پایپ‌لاین معطل نماند و متن اصلی جایگزین شود)
    tagline_orig = (tmd or {}).get("tagline") or ""
    plot_orig = om.get("plot") or (tmd or {}).get("overview") or ""
    try:
        tagline_fa, plot_fa = await asyncio.wait_for(
            asyncio.to_thread(translate_to_farsi, tagline_orig, plot_orig),
            timeout=20,
        )
    except asyncio.TimeoutError:
        logger.warning("ترجمه بیش از حد طول کشید؛ متن اصلی استفاده شد.")
        tagline_fa, plot_fa = tagline_orig, plot_orig

    return {
        "title": title,
        "display_title": display_title,
        "original_title": (tmd or {}).get("original_title")
        or (tmd or {}).get("original_name")
        or "",
        "type_name": "TV Series" if is_tv else "Movie",
        "type_key": "tv" if is_tv else "movie",
        "year": final_year,
        "imdb_id": final_imdb,
        "tmdb_id": tmdb_id_final,
        "imdb_rating": om.get("imdb_rating"),
        "rotten": om.get("rotten"),
        "awards": om.get("awards"),
        "runtime_line": runtime_line,
        "country": countries,
        "genre_line": genre_line,
        "director": director_names or om.get("director"),
        "actors": om.get("actors"),
        "writer": om.get("writer"),
        "plot": plot_orig,
        "plot_fa": plot_fa,
        "tagline": tagline_orig,
        "tagline_fa": tagline_fa,
        "rated": om.get("rated"),
        "production": production or om.get("production") or "",
        "budget": budget,
        "revenue": revenue,
        "vote_count": str((tmd or {}).get("vote_count") or "") if tmd else "",
        "popularity": f"{float((tmd or {}).get('popularity') or 0):.1f}" if tmd else "",
        "release_date": (tmd or {}).get("release_date")
        or (tmd or {}).get("first_air_date")
        or om.get("released")
        or "",
        "seasons": seasons_count,
        "episodes": episodes_count,
        "status_tv": status_tv,
        "first_air": first_air,
        "last_air": last_air,
        "networks": networks,
        "cast_top": cast_top,
        "trailer_url": trailer_url,
        "poster_path": poster_local,
        "poster_hd": poster_hd,
        "confidence": confidence,
        "reel_url": reel_url,
    }


def _genre_emoji_from_text(text: str) -> list[str]:
    """تبدیل متن ژانر OMDB («Drama, Crime») به خطوط ایموجی‌دار."""
    lines: list[str] = []
    for item in [s.strip() for s in str(text).split(",")]:
        if not item:
            continue
        name = item.capitalize()
        mark = {"Action": "🔫", "Adventure": "🏔", "Comedy": "😂", "Drama": "🎭",
                "Horror": "😱", "Fantasy": "🧙", "Sci-Fi": "🚀", "Romance": "❤️",
                "Mystery": "🕵", "Family": "👪", "Thriller": "🔥", "Crime": "💰",
                "Animation": "🧚", "Documentary": "📖", "War": "⚔️",
                "Western": "🤠", "History": "🏛️", "Music": "🎵"}.get(name, "🎬")
        lines.append(f"{mark} {name}")
    return lines