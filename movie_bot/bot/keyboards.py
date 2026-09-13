"""ماژول کیبوردهای درون‌خطی ربات movie_bot.

شامل کیبورد /start، صفحه‌بندی تاریخچه، کارت فیلم با تمام دکمه‌های
قراردادی (مشابه، بازیگران، کارگردان، پوستر HD، تریلر، فصل‌ها، تاریخچه…)
و کیبوردهای مدیریتی.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ─── دکمه‌های اصلی /start ───
def start_keyboard() -> InlineKeyboardMarkup:
    """کیبورد صفحه شروع."""
    keyboard = [
        [
            InlineKeyboardButton("ℹ️ راهنما", callback_data="help"),
            InlineKeyboardButton("📜 تاریخچه", callback_data="history:0"),
        ],
        [
            InlineKeyboardButton("🤖 دربارهٔ ربات", callback_data="about"),
            InlineKeyboardButton("🗓 آمار من", callback_data="mystats"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ─── برنامه‌پسوند صفحه‌بندی تاریخچه ─────────────────
def history_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """کیبورد صفحه‌بندی برای تاریخچه جستجو."""
    buttons: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"history:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"history:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔍 جستجوی جدید", callback_data="new_search")])
    return InlineKeyboardMarkup(buttons)


def _mk(id_: int | str, label: str) -> str:
    """ساخت callback_data امن برای شناسه‌های عددی TMDb."""
    return f"{label}:{id_}"


# ─── کارت فیلم ──────────────────────────────────────
def movie_card_keyboard(info: dict) -> InlineKeyboardMarkup:
    """دکمه‌های تمیز کارت فیلم — چینش مشخص چند ردیفی.

    ردیف‌ها: تریلر|مشابه  →  بازیگران|کارگردان  →  پوسترHD|تاریخچه
    سپس دکمه‌های URL: IMDb|TMDb و (برای سریال) فصل‌ها؛ در انتها جستجوی جدید.
    """
    kind = info.get("type_key") or "movie"
    tid = info.get("tmdb_id")
    buttons: list[list[InlineKeyboardButton]] = []

    row1: list[InlineKeyboardButton] = []
    if tid:
        if info.get("trailer_url"):
            row1.append(InlineKeyboardButton("▶️ تریلر", callback_data=f"trailer:{kind}:{tid}"))
        row1.append(InlineKeyboardButton("🎬 مشابه", callback_data=f"similar:{kind}:{tid}"))
    buttons.append(row1)

    row2: list[InlineKeyboardButton] = []
    if tid:
        row2.append(InlineKeyboardButton("🎭 بازیگران", callback_data=f"actors:{kind}:{tid}"))
        row2.append(InlineKeyboardButton("🎥 کارگردان", callback_data=f"director:{kind}:{tid}"))
    buttons.append(row2)

    row3: list[InlineKeyboardButton] = []
    if tid:
        pass
    row3.append(InlineKeyboardButton("📊 تاریخچه", callback_data="history:0"))
    buttons.append(row3)

    # لینک‌های واقعی IMDb / TMDb (مستقیم باز می‌شوند)
    link_row: list[InlineKeyboardButton] = []
    imdb_id = info.get("imdb_id")
    if imdb_id:
        link_row.append(
            InlineKeyboardButton("🔗 IMDb", url=f"https://www.imdb.com/title/{imdb_id}")
        )
    if tid:
        link_row.append(
            InlineKeyboardButton(
                "🔗 TMDb",
                url=f"https://www.themoviedb.org/{kind}/{tid}",
            )
        )
    if link_row:
        buttons.append(link_row)

    if info.get("type_key") == "tv" and tid:
        buttons.append(
            [InlineKeyboardButton("📺 فصل‌ها و قسمت‌ها", callback_data=f"seasons:{tid}")]
        )

    buttons.append([InlineKeyboardButton("🔄 جستجوی جدید", callback_data="new_search")])
    return InlineKeyboardMarkup(buttons)


def movie_card_keyboard(info: dict) -> InlineKeyboardMarkup:
    """دکمه‌های تمیز کارت فیلم — چینش مشخص چند ردیفی.

    ردیف‌ها: تریلر|مشابه  →  بازیگران|کارگردان  →  پوسترHD|تاریخچه
    سپس دکمه‌های URL: IMDb|TMDb و (برای سریال) فصل‌ها؛ در انتها بازگشت + جستجوی جدید.
    """
    kind = info.get("type_key") or "movie"
    tid = info.get("tmdb_id")
    buttons: list[list[InlineKeyboardButton]] = []

    row1: list[InlineKeyboardButton] = []
    if tid:
        if info.get("trailer_url"):
            row1.append(InlineKeyboardButton("▶️ تریلر", callback_data=f"trailer:{kind}:{tid}"))
        row1.append(InlineKeyboardButton("🎬 مشابه", callback_data=f"similar:{kind}:{tid}"))
    buttons.append(row1)

    row2: list[InlineKeyboardButton] = []
    if tid:
        row2.append(InlineKeyboardButton("🎭 بازیگران", callback_data=f"actors:{kind}:{tid}"))
        row2.append(InlineKeyboardButton("🎥 کارگردان", callback_data=f"director:{kind}:{tid}"))
    buttons.append(row2)

    row3: list[InlineKeyboardButton] = []
    if tid:
        pass
    row3.append(InlineKeyboardButton("📊 تاریخچه", callback_data="history:0"))
    buttons.append(row3)

    # لینک‌های واقعی IMDb / TMDb (مستقیم باز می‌شوند)
    link_row: list[InlineKeyboardButton] = []
    imdb_id = info.get("imdb_id")
    if imdb_id:
        link_row.append(
            InlineKeyboardButton("🔗 IMDb", url=f"https://www.imdb.com/title/{imdb_id}")
        )
    if tid:
        link_row.append(
            InlineKeyboardButton(
                "🔗 TMDb",
                url=f"https://www.themoviedb.org/{kind}/{tid}",
            )
        )
    if link_row:
        buttons.append(link_row)

    if info.get("type_key") == "tv" and tid:
        buttons.append(
            [InlineKeyboardButton("📺 فصل‌ها و قسمت‌ها", callback_data=f"seasons:{tid}")]
        )

    # بازگشت به منوی اصلی + جستجوی جدید
    buttons.append(
        [
            InlineKeyboardButton("🔙 بازگشت", callback_data="new_search"),
            InlineKeyboardButton("🔄 جستجوی جدید", callback_data="new_search"),
        ]
    )

    # دکمهٔ رد کردن نتیجه — اگر فیلم تشخیص‌داده‌شده اشتباه بود
    buttons.append(
        [InlineKeyboardButton("❌ اشتباهه — دوباره بگرد", callback_data="mv_retry")]
    )
    return InlineKeyboardMarkup(buttons)


def similar_keyboard(items: list[dict], kind: str, tmdb_id: int | None = None) -> InlineKeyboardMarkup:
    """کیبورد لیست ۵ فیلم مشابه — با دکمهٔ بازگشت به کارت فیلم."""
    buttons: list[list[InlineKeyboardButton]] = []
    for it in items[:5]:
        t_id = it.get("id")
        label = it.get("title") or it.get("name") or it.get("original_name") or "؟"
        year = (it.get("release_date") or it.get("first_air_date") or "")[:4]
        rating = it.get("vote_average") or ""
        text = f"🎬 {label} ({year})" if year else f"🎬 {label}"
        if rating:
            text += f" ⭐ {float(rating):.1f}"
        buttons.append([InlineKeyboardButton(text, callback_data=f"movie:{kind}:{t_id}")])
    nav: list[InlineKeyboardButton] = []
    if tmdb_id:
        nav.append(InlineKeyboardButton("🔙 اطلاعات فیلم", callback_data=f"movie:{kind}:{tmdb_id}"))
    nav.append(InlineKeyboardButton("🔄 جستجوی جدید", callback_data="new_search"))
    buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def cast_list_keyboard(cast: list[dict], kind: str, tmdb_id: int) -> InlineKeyboardMarkup:
    """کیبورد لیست بازیگران — برای هر بازیگر یک دکمهٔ جداگانه."""
    buttons: list[list[InlineKeyboardButton]] = []
    for c in cast:
        pid = c.get("id")
        if pid:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"👤 {c.get('name','')}",
                        callback_data=f"actor:{pid}:{kind}:{tmdb_id}",
                    )
                ]
            )
    buttons.append(
        [
            InlineKeyboardButton("🔙 اطلاعات فیلم", callback_data=f"movie:{kind}:{tmdb_id}"),
            InlineKeyboardButton("🔄 جستجوی جدید", callback_data="new_search"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def person_keyboard(person_id: int, back: str | None = None) -> InlineKeyboardMarkup:
    """کیبورد پروفایل بازیگر/کارگردان."""
    buttons: list[list[InlineKeyboardButton]] = []
    if back:
        buttons.append(
            [InlineKeyboardButton("🔙 بازگشت", callback_data=back)]
        )
    buttons.append([InlineKeyboardButton("🔄 جستجوی جدید", callback_data="new_search")])
    return InlineKeyboardMarkup(buttons)


def seasons_keyboard(
    seasons: list[dict], tmdb_id: int, series_title: str
) -> InlineKeyboardMarkup:
    """کیبورد لیست فصل‌خانه یک سریال."""
    buttons: list[list[InlineKeyboardButton]] = []
    for s in seasons:
        num = s.get("season_number")
        label = f"📺 فصل {num}"
        if s.get("name") and s["name"] not in ("", "Season 0"):
            label = s["name"]
        if s.get("episode_count"):
            label += f" — {s['episode_count']} قسمت"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"season:{tmdb_id}:{num}")]
        )
    buttons.append(
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"movie:tv:{tmdb_id}")]
    )
    return InlineKeyboardMarkup(buttons)


def episodes_keyboard(
    episodes: list[dict], tmdb_id: int, season_num: int
) -> InlineKeyboardMarkup:
    """کیبورد لیست قسمت‌های یک فصل."""
    buttons: list[list[InlineKeyboardButton]] = []
    for ep in episodes:
        en = ep.get("episode_number")
        name = ep.get("name") or ""
        rating = ep.get("vote_average") or 0
        label = f"💿 قسمت {en}"
        if name:
            label += f": {name}"
        if rating:
            label += f" ⭐ {float(rating):.1f}"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"ep:{tmdb_id}:{season_num}:{en}")]
        )
    buttons.append(
        [
            InlineKeyboardButton("🔙 فصل‌ها", callback_data=f"seasons:{tmdb_id}"),
            InlineKeyboardButton("🔄 جستجوی جدید", callback_data="new_search"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def episode_keyboard(tmdb_id: int, season_num: int) -> InlineKeyboardMarkup:
    """کیبورد زیر پیام جزئیات یک قسمت."""
    buttons = [
        [
            InlineKeyboardButton(
                "🔙 قسمت‌ها", callback_data=f"season:{tmdb_id}:{season_num}"
            )
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def trailer_keyboard(tmdb_id: int, kind: str) -> InlineKeyboardMarkup:
    """دکمهٔ تریلر فیلم."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ تریلر فیلم", callback_data=f"trailer:{kind}:{tmdb_id}"),
                InlineKeyboardButton("🔄 جستجوی جدید", callback_data="new_search"),
            ]
        ]
    )


# ─── دکمه‌های مدیریتی ─────────────────────────────
def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 آمار عمومی", callback_data="admin_stats"),
                InlineKeyboardButton("👥 لیست کاربرها", callback_data="admin_users"),
            ],
            [
                InlineKeyboardButton("🗑 پاک‌سازی", callback_data="admin_clean"),
                InlineKeyboardButton("🔔 پیام گروهی", callback_data="admin_broadcast"),
            ],
        ]
    )