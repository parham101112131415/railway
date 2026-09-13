"""ماژول قالب‌بندی پیام‌های خروجی برای تلگرام.

فرمت نهایی کارت فیلم دقیقاً طبق قرارداد UI پروژه ساخته می‌شود:
بخش‌بندی خوانا، فاصله‌گذاری منظم، جداکننده‌های تمیز و فقط محتوای
TMDb و OMDB (هیچ جملهٔ تولیدی Gemini نمایش داده نمی‌شود).
"""

from __future__ import annotations

from typing import Any

SEP = "━━━━━━━━━━━━━━━━━━━━"


def _esc(text: Any) -> str:
    """تبدیل امن به رشته و حذف فاصله‌های دورِ متن."""
    return str(text or "").strip()


# نگاشت رده سنی → سن مناسب (OMDb/TMDb → فارسی)
_AGE_MAP: dict[str, str] = {
    "g": "مناسب همه سنین",
    "pg": "مناسب بالای ۷ سال",
    "pg-13": "مناسب بالای ۱۳ سال",
    "r": "مناسب بالای ۱۷ سال",
    "nc-17": "مناسب بالای ۱۸ سال",
    "tv-g": "مناسب همه سنین",
    "tv-pg": "مناسب بالای ۷ سال",
    "tv-14": "مناسب بالای ۱۴ سال",
    "tv-ma": "مناسب بالای ۱۷ سال",
    "nr": "نامشخص",
    "unrated": "نامشخص",
    "not rated": "نامشخص",
    "n/a": "نامشخص",
}


def _age_fa(rated: str) -> str:
    """سنی که رده سنی نشان می‌دهد، به فارسی."""
    key = str(rated).strip().lower()
    return _AGE_MAP.get(key, "")


def format_poster_caption(info: dict) -> str:
    """کپشن کوتاه پوستر (زیر 1024 کاراکتر — محدودیت تلگرام)."""
    title = _esc(info.get("display_title") or info.get("title"))
    year = _esc(info.get("year"))
    rating = _esc(info.get("imdb_rating"))
    parts = [f"🎬 {title} ({year})" if year else f"🎬 {title}"]
    if rating:
        parts.append(f"⭐ IMDb: {rating}/10")
    return "\n".join(parts)


def format_movie_message(info: dict) -> str:
    """ساخت پیام کامل اطلاعات فیلم/سریال با فرمت تمیز قراردادی."""
    title = _esc(info.get("display_title") or info.get("title"))
    year = _esc(info.get("year"))
    is_tv = info.get("type_key") == "tv"

    rating = _esc(info.get("imdb_rating")) or "نامشخص"
    rotten = _esc(info.get("rotten")) or "ناموجود"
    actors = _esc(info.get("actors")) or "نامشخص"
    director = _esc(info.get("director")) or "نامشخص"
    writer = _esc(info.get("writer")) or "نامشخص"
    genre = _esc(info.get("genre_line")) or "نامشخص"
    runtime = _esc(info.get("runtime_line")) or "نامشخص"
    countries = _esc(info.get("country")) or "نامشخص"
    languages = _esc(info.get("language")) or "نامشخص"
    awards = _esc(info.get("awards")) or "نامشخص"
    rated = _esc(info.get("rated")) or "نامشخص"
    tagline = _esc(info.get("tagline"))
    tagline_fa = _esc(info.get("tagline_fa"))
    plot = _esc(info.get("plot")) or "نامشخص"
    plot_fa = _esc(info.get("plot_fa"))

    # رده سنی + سن مناسب فارسی (اگر قابل نگاشت بود)
    rated_age = _age_fa(rated)
    rated_line = rated
    if rated_age and rated_age != "نامشخص":
        rated_line = f"{rated} — {rated_age}"

    # ژانرها: یک خط مرتب (بدون خطوط جدا)
    genre_one_line = genre_oneline(genre) if genre != "نامشخص" else genre

    lines: list[str] = [f"🎬 {title} ({year})" if year else f"🎬 {title}"]
    lines.append(SEP)
    lines.append("")

    lines += [
        f"⭐ IMDb: {rating}/10",
        f"🍅 Rotten Tomatoes: {rotten}",
        "",
        f"🎭 بازیگران:\n{actors}",
        "",
        f"🎥 کارگردان:\n{director}",
        "",
        f"✍️ نویسنده:\n{writer}",
        "",
        f"🎞 ژانر:\n{genre_one_line}",
        "",
    ]

    if is_tv:
        seasons = _esc(info.get("seasons")) or "نامشخص"
        episodes = _esc(info.get("episodes")) or "نامشخص"
        status_tv = _esc(info.get("status_tv")) or "نامشخص"
        first_air = _esc(info.get("first_air")) or "نامشخص"
        last_air = _esc(info.get("last_air")) or "نامشخص"
        networks = _esc(info.get("networks")) or "نامشخص"
        series_block = [
            f"📺 تعداد فصل‌ها: {seasons}",
            f"🎞 تعداد قسمت‌ها: {episodes}",
            f"📡 وضعیت: {status_tv}",
            f"📅 اولین قسمت: {first_air}",
            f"📅 آخرین قسمت: {last_air}",
            f"🏢 شبکه: {networks}",
            f"⏱ میانگین زمان هر قسمت: {runtime}",
        ]
        lines += [*series_block, ""]
    else:
        lines += [f"⏱ مدت زمان:\n{runtime}", ""]

    lines += [
        f"🌍 کشور:\n{countries}",
        "",
        f"🗣 زبان:\n{languages}",
        "",
        f"🏆 جوایز:\n{awards}",
        "",
        f"🔞 رده سنی:\n{rated_line}",
        "",
    ]

    if tagline:
        # شعار: زبان اصلی + ترجمهٔ فارسی کنارش (اگر موجود بود)
        lines += [f"💬 شعار (انگلیسی):\n{tagline}", ""]
        if tagline_fa and tagline_fa != tagline:
            lines += [f"🖋 فارسی:\n{tagline_fa}", ""]

    # خلاصه: اگر ترجمهٔ فارسی موجود بود همان نمایش داده می‌شود
    plot_text = plot_fa if plot_fa and plot_fa != plot else plot
    lines += [f"📖 خلاصه:\n{plot_text[:600]}", ""]

    conf = info.get("confidence") or 0
    try:
        conf_pct = round(float(conf) * 100)
    except (TypeError, ValueError):
        conf_pct = 0

    lines += [
        SEP,
        f"🤖 اطمینان تشخیص: {conf_pct}٪",
    ]

    return "\n".join(lines)


def genre_oneline(genre: str) -> str:
    """تبدیل ژانر چندخطی به یک خط با جداکنندهٔ •."""
    parts = [ln.strip() for ln in genre.split("\n") if ln.strip()]
    if len(parts) <= 1:
        return genre
    return " • ".join(parts)