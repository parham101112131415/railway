"""ماژول مدیریت callback_query ها (دکمه‌های شیشه‌ای).

پاسخ به کلیک‌های کاربر روی دکمه‌ها: تاریخچه، آمار، راهنما، مدیریت و
همهٔ قابلیت‌های جدید (فیلم مشابه، بازیگران، کارگردان، پوستر HD،
تریلر، فصل‌ها/قسمت‌ها…).
"""

from __future__ import annotations

import os

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.keyboards import (
    admin_keyboard,
    cast_list_keyboard,
    episode_keyboard,
    episodes_keyboard,
    history_keyboard,
    movie_card_keyboard,
    person_keyboard,
    seasons_keyboard,
    similar_keyboard,
    start_keyboard,
)
from bot.messages import format_movie_message, format_poster_caption
from database.cache import get_trailer_url, set_trailer_url
from database.sqlite import transaction
from services import movie_api, tmdb
from services.gemini import translate_to_farsi
from utils.logger import logger
from utils.validator import is_admin


def is_admin_user(user_id: int, admin_ids: list[int]) -> bool:
    """بررسی ادمین بودن کاربر."""
    return is_admin(user_id, admin_ids)


async def send_movie_card(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, info: dict
) -> None:
    """ارسال پوستر (با کپشن کوتاه) + متن کامل کارت با دکمه‌ها.

    پوستر فقط وقتی موجود باشد اول فرستاده می‌شود؛ سپس متن کامل با کیبورد.
    """
    text = format_movie_message(info)
    kb = movie_card_keyboard(info)
    # پوستر با بالاترین کیفیت (original / 4K) — به‌صورت خودکار همراه کارت فرستاده می‌شود
    poster_hd = info.get("poster_hd")
    if poster_hd:
        try:
            await context.bot.send_photo(
                chat_id=chat_id, photo=poster_hd, caption=format_poster_caption(info)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("خطا در ارسال پوستر HD: %s", exc)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مسیریابی کلیه callback_query ها."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    user_id = query.from_user.id if query.from_user else 0

    if data == "help":
        await _show_help(query)
    elif data == "about":
        await _show_about(query)
    elif data == "mystats":
        await _show_user_stats(query, user_id)
    elif data == "new_search":
        await _reset_state(query)
    elif data.startswith("history:"):
        await _show_history(query, user_id, data)
    elif data.startswith("admin_"):
        await _handle_admin(query, user_id, data)
    # ─── قابلیت‌های جدید ───
    elif data.startswith("similar:"):
        await _similar(query, data)
    elif data.startswith("actors:"):
        await _actors(query, data)
    elif data.startswith("actor:"):
        await _person_info(query, data, header="🎭 بازیگر")
    elif data.startswith("director:"):
        await _director(query, data)
    elif data.startswith("posterhd:"):
        await _poster_hd(query, data)
    elif data.startswith("trailer:"):
        await _trailer(query, data)
    elif data.startswith("movie:"):
        await _movie(query, data)
    elif data.startswith("seasons:"):
        await _seasons(query, data)
    elif data.startswith("season:"):
        await _season(query, data)
    elif data.startswith("ep:"):
        await _episode(query, data)
    else:
        await query.edit_message_text("این دکمه هنوز پشتیبانی نمی‌شود.")


# ─── توابع کمکی ───
def _parts(data: str) -> list[str]:
    """شکستن callback_data با امنیت."""
    return data.split(":")


async def _credits(kind: str, tmdb_id: int) -> dict:
    """اعتبارات (بازیگران/کارگردان) با Session مشترک."""
    async with aiohttp.ClientSession() as session:
        return await tmdb.credits(session, tmdb_id, kind) or {}


async def _send_text_or_photo(query, text: str, kb=None) -> None:
    """ارسال پرسش‌نامه: اگر عکس مستقیم موجود بود photo وگرنه متن عادی."""
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ─── نمایش راهنما ────────────────────────────────
def _show_help(query):
    text = (
        "📚 *راهنما*\n\n"
        "برای تشخیص فیلم کافیست لینک ریلز یا پست‌های که صحنهٔ یک فیلم/سریال "
        "داره رو بفرستید:\n\n"
        "🔗 `https://www.instagram.com/reel/...`\n\n"
        "من ویدیو رو دانلود، فریم‌هایش رو تحلیل و عنوانش رو برايت پیدا میکنم. "
    )
    return query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=start_keyboard()
    )


def _show_about(query):
    return query.edit_message_text(
        "🤖 *دربارهٔ ربات*\n\n"
        "این ربات با استفاده از قدرت Gemini و api های OMDB/TMDb "
        "فیلم‌ها رو از روی ریلز اینستاگرام تشخیص میده.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=start_keyboard(),
    )


async def _show_user_stats(query, user_id):
    with transaction() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM history WHERE user_id=?", (user_id,)
        ).fetchone()
        total = row[0] if row else 0
    await query.edit_message_text(
        f"بیدم ربات را به کار بردی {total} بار! 🎟️",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=start_keyboard(),
    )


async def _reset_state(query):
    return await query.edit_message_text(
        "📥 لینک ریلز یا پست اینستاگرام را بفرست و منتظر باش.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=None,
    )


async def _show_history(query, user_id, data):
    """نمایش تاریخچه با صفحه‌بندی."""
    try:
        page = int(data.split(":")[1])
    except (IndexError, ValueError):
        page = 0
    per_page = 8
    with transaction() as conn:
        rows = conn.execute(
            "SELECT title, media_type, year, searched_at FROM history "
            "WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, per_page, page * per_page),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM history WHERE user_id=?", (user_id,)
        ).fetchone()[0]

    if not rows:
        return await query.edit_message_text(
            "هنوز هیچ‌چیز جستجو نکرده‌اید. یه لینک بفرست!",
            reply_markup=start_keyboard(),
        )

    lines = [
        f"*تاریخچهٔ شما* (صفحه {page+1} از {max(1,(total+per_page-1)//per_page)})"
    ]
    for r in rows:
        icon = "📺" if r["media_type"] == "series" else "📽"
        yr = f" ({r['year']})" if r["year"] else ""
        lines.append(f"{icon} {r['title']}{yr}")
    total_pages = max(1, (total + per_page - 1) // per_page)
    reply_markup = history_keyboard(page, total_pages) if rows else start_keyboard()
    return await query.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
    )


async def _handle_admin(query, user_id, data):
    """مدیریت محافظا: فقط ادمین."""
    from config import ADMIN_IDS

    if not is_admin(user_id, ADMIN_IDS):
        return await query.edit_message_text("⛔ دسترسی ندارید.")
    if data == "admin_stats":
        return await _admin_stats(query)
    if data == "admin_users":
        return await _admin_users(query)
    if data == "admin_clean":
        from utils.cleanup import clean_old_files

        cleaned = clean_old_files()
        return await query.edit_message_text(f"🗑، {cleaned} فایل پاک شد.")
    if data == "admin_broadcast":
        return await query.edit_message_text(
            "📢 برای ارسال پیام گروهی مستند به فرم ارسال کنید."
            " یا بعداً پیاده‌سازی می‌شود."
        )


async def _admin_stats(query):
    with transaction() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        history = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    await query.edit_message_text(
        f"📊 *آمار عمومی*\n👥 کاربران: {users}\n🔍 جستجو: {history}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_keyboard(),
    )


async def _admin_users(query):
    with transaction() as conn:
        rows = conn.execute(
            "SELECT user_id, username, first_name FROM users "
            "ORDER BY joined_at DESC LIMIT 20"
        ).fetchall()
    if not rows:
        return await query.edit_message_text("کاربری ثبت نشده.")
    text = "\n".join(
        f"`{r['user_id']}` {r['first_name'] or ''} @{r['username'] or '-'}"
        for r in rows
    )
    await query.edit_message_text(
        f"*کاربران اخیر:*\n{text}", parse_mode=ParseMode.MARKDOWN
    )


# ─── فیلم مشابه ──────────────────────────────────
async def _similar(query, data: str) -> None:
    try:
        _, kind, tid = data.split(":")
        tmdb_id = int(tid)
    except ValueError:
        return await query.edit_message_text("❌ اطلاعات ناقص.")
    bot = query.get_bot()
    async with aiohttp.ClientSession() as session:
        items = await tmdb.similar(session, tmdb_id, kind, limit=5)
    if not items:
        return await query.edit_message_text(
            "❌ فیلم مشابه‌ای پیدا نشد.",
            reply_markup=movie_card_keyboard(
                {"type_key": kind, "tmdb_id": tmdb_id, "trailer_url": ""}
            ),
        )
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    lines = ["🎬 *فیلم‌های مشابه*", ""]
    for i, it in enumerate(items[:5]):
        label = it.get("title") or it.get("name") or "؟"
        yr = (it.get("release_date") or it.get("first_air_date") or "")[:4]
        rating = it.get("vote_average") or 0
        row = f"{nums[i]} {label} ({yr})" if yr else f"{nums[i]} {label}"
        if rating:
            row += f" ⭐ {float(rating):.1f}"
        lines.append(row)
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=similar_keyboard(items, kind, tmdb_id),
    )


# ─── بازیگران ────────────────────────────────────
async def _actors(query, data: str) -> None:
    parts = _parts(data)
    if len(parts) < 3:
        return await query.edit_message_text("❌ اطلاعات ناقص.")
    kind, tid = parts[1], parts[2]
    try:
        tmdb_id = int(tid)
    except ValueError:
        return await query.edit_message_text("❌ شناسه نامعتبر.")
    cr = await _credits(kind, tmdb_id)
    cast = (cr.get("cast") or [])[:5]
    if not cast:
        return await query.edit_message_text("❌ بازیگری ثبت نشده.")
    lines = ["🎭 *بازیگران اصلی:*", ""]
    for c in cast:
        name = c.get("name") or c.get("original_name") or "؟"
        role = c.get("character") or ""
        lines.append(f"👤 {name} — {role or '—'}")
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cast_list_keyboard(cast, kind, tmdb_id),
    )


# ─── پروفایل شخص (بازیگر/کارگردان) ───────────────
async def _person_info(query, data: str, header: str) -> None:
    parts = _parts(data)
    try:
        person_id = int(parts[1])
    except (IndexError, ValueError):
        return await query.edit_message_text("❌ شناسه نامعتبر.")
    back = None
    # actor:{pid}:{kind}:{tid} → دکمهٔ بازگشت به کارت
    if len(parts) >= 4:
        try:
            back = f"movie:{parts[2]}:{int(parts[3])}"
        except ValueError:
            back = None
    await _show_person(query, person_id, header, back=back)


async def _show_person(query, person_id: int, header: str, back: str | None = None) -> None:
    """پروفایل کامل یک شخص: عکس، تولد، محل، پاپولاریتی، آثار، بیوگرافی."""
    async with aiohttp.ClientSession() as session:
        p = await tmdb.person(session, person_id)
        if not p:
            return await query.edit_message_text("❌ اطلاعاتی پیدا نشد.")
        cr = await tmdb.person_credits(session, person_id) or {}
    name = p.get("name") or p.get("original_name") or "؟"
    dob = p.get("birthday") or "نامشخص"
    place = p.get("place_of_birth") or "نامشخص"
    try:
        pop = f"{float(p.get('popularity') or 0):.1f}"
    except (TypeError, ValueError):
        pop = "0"
    bio = (p.get("biography") or "").strip() or "بیوگرافی موجود نیست."
    works = sorted(
        list((cr.get("cast") or [])) + list((cr.get("crew") or [])),
        key=lambda w: w.get("popularity") or 0,
        reverse=True,
    )[:5]
    work_lines = []
    for w in works:
        t = w.get("title") or w.get("name") or "؟"
        yr = (w.get("release_date") or w.get("first_air_date") or "")[:4]
        work_lines.append(f"• {t} {f'({yr})' if yr else ''}")
    lines = [
        f"{header} *{name}*",
        "",
        f"🎂 تاریخ تولد: {dob}",
        f"🌍 محل تولد: {place}",
        f"⭐ پاپولاریتی: {pop}",
    ]
    if work_lines:
        lines += ["", "🎬 *معروفترین آثار:*", *work_lines]
    lines += ["", f"📖 *بیوگرافی:*\n{bio[:600]}"]
    text = "\n".join(lines)
    kb = person_keyboard(person_id, back=back)
    pp = p.get("profile_path")
    bot = query.get_bot()
    if pp:
        try:
            await bot.send_photo(
                chat_id=query.message.chat_id,
                photo=f"https://image.tmdb.org/t/p/w500{pp}",
                caption=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("خطا در ارسال عکس شخص: %s", exc)
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ─── کارگردان / سازنده ──────────────────────────────
async def _director(query, data: str) -> None:
    try:
        _, kind, tid = _parts(data)
        tmdb_id = int(tid)
    except ValueError:
        return await query.edit_message_text("❌ اطلاعات ناقص.")
    # سریال‌ها کارگردان واحد ندارن؛ ابتدا سازنده (Created By) را بررسی می‌کنیم
    if kind == "tv":
        async with aiohttp.ClientSession() as session:
            d = await tmdb.details(session, tmdb_id, "tv") or {}
        creators = d.get("created_by") or []
        if creators:
            pid = creators[0].get("id")
            if pid:
                return await _show_person(
                    query, pid, "🎬 سازندهٔ سریال", back=f"movie:{kind}:{tmdb_id}"
                )
    cr = await _credits(kind, tmdb_id)
    directors = [m for m in (cr.get("crew") or []) if (m.get("job") or "").lower() == "director"]
    if not directors:
        return await query.edit_message_text(
            "🎥 کارگردان پیدا نشد." if kind != "tv"
            else "🎬 سازنده یا کارگردانی ثبت نشده."
        )
    pid = directors[0].get("id")
    if not pid:
        return await query.edit_message_text("🎥 اطلاعات کارگردان موجود نیست.")
    await _show_person(query, pid, "🎥 کارگردان", back=f"movie:{kind}:{tmdb_id}")


# ─── پوستر HD ────────────────────────────────────
async def _poster_hd(query, data: str) -> None:
    try:
        _, kind, tid = data.split(":")
        tmdb_id = int(tid)
    except ValueError:
        return await query.answer("❌ اطلاعات ناقص", show_alert=True)
    async with aiohttp.ClientSession() as session:
        d = await tmdb.details(session, tmdb_id, kind)
    pp = (d or {}).get("poster_path") or ""
    if not pp:
        return await query.answer("🖼 پوستر موجود نیست", show_alert=True)
    title = (d.get("title") or d.get("name") or "") if d else ""
    bot = query.get_bot()
    await bot.send_photo(
        chat_id=query.message.chat_id,
        photo=f"https://image.tmdb.org/t/p/original{pp}",
        caption=f"🖼 پوستر HD — {title}",
    )


# ─── تریلر ───────────────────────────────────────
async def _trailer(query, data: str) -> None:
    try:
        _, kind, tid = data.split(":")
        tmdb_id = int(tid)
    except ValueError:
        return await query.answer("❌ اطلاعات ناقص", show_alert=True)
    key = f"{kind}:{tmdb_id}"
    url = get_trailer_url(key)
    if not url:
        try:
            async with aiohttp.ClientSession() as session:
                vids = await tmdb.videos(session, tmdb_id, kind)
            url = tmdb.pick_trailer(vids)
            if url:
                set_trailer_url(key, url)
        except Exception:  # noqa: BLE001
            url = None
    bot = query.get_bot()
    if url:
        await bot.send_message(
            chat_id=query.message.chat_id, text=f"🎥 تریلر فیلم:\n\n{url}"
        )
    else:
        await bot.send_message(
            chat_id=query.message.chat_id, text="❌ تریلری برای این فیلم پیدا نشد."
        )


# ─── بازنمایی کارت فیلم (بازگشت/مشابه) ────────────
async def _movie(query, data: str) -> None:
    try:
        _, kind, tid = data.split(":")
        tmdb_id = int(tid)
    except ValueError:
        return await query.answer("❌ اطلاعات ناقص", show_alert=True)
    info = await movie_api.build_movie_info_by_id(tmdb_id, kind)
    if not info:
        return await query.answer("❌ اطلاعات پیدا نشد", show_alert=True)
    await send_movie_card(query.get_bot(), query.message.chat_id, info)


# ─── فصل‌ها و قسمت‌ها ─────────────────────────────
async def _seasons(query, data: str) -> None:
    try:
        tmdb_id = int(_parts(data)[1])
    except (IndexError, ValueError):
        return await query.answer("❌ شناسه نامعتبر", show_alert=True)
    async with aiohttp.ClientSession() as session:
        d = await tmdb.details(session, tmdb_id, "tv")
    seasons = [
        s for s in (d or {}).get("seasons") or []
        if (s.get("season_number") or 0) > 0
    ]
    if not seasons:
        return await query.answer("📺 فصلی ثبت نشده", show_alert=True)
    title = (d.get("name") or d.get("original_name")) or ""
    lines = [f"📺 *فصل‌ها — {title}*", ""]
    for s in seasons:
        num = s.get("season_number")
        cnt = s.get("episode_count") or 0
        lines.append(f"• فصل {num} ({cnt} قسمت)")
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=seasons_keyboard(seasons, tmdb_id, title),
    )


async def _season(query, data: str) -> None:
    try:
        _, tid, snum = data.split(":")
        tmdb_id, season_num = int(tid), int(snum)
    except ValueError:
        return await query.answer("❌ اطلاعات ناقص", show_alert=True)
    async with aiohttp.ClientSession() as session:
        se = await tmdb.season(session, tmdb_id, season_num)
    episodes = (se or {}).get("episodes") or []
    if not episodes:
        return await query.answer("🎞 قسمتی ثبت نشده", show_alert=True)
    name = se.get("name") or f"فصل {season_num}"
    lines = [f"📺 *{name}* — {len(episodes)} قسمت", ""]
    for ep in episodes:
        en = ep.get("episode_number")
        ep_name = ep.get("name") or ""
        lines.append(f"💿 {en}. {ep_name or '—'}")
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=episodes_keyboard(episodes, tmdb_id, season_num),
    )


async def _episode(query, data: str) -> None:
    try:
        _, ts, ss, es = data.split(":")
        tmdb_id, season_num, ep_num = int(ts), int(ss), int(es)
    except ValueError:
        return await query.answer("❌ اطلاعات ناقص", show_alert=True)
    async with aiohttp.ClientSession() as session:
        se = await tmdb.season(session, tmdb_id, season_num)
    ep = None
    for e in (se or {}).get("episodes") or []:
        if e.get("episode_number") == ep_num:
            ep = e
            break
    if not ep:
        return await query.answer("❌ قسمت پیدا نشد", show_alert=True)
    name = ep.get("name") or "—"
    air = ep.get("air_date") or "نامشخص"
    rating = float(ep.get("vote_average") or 0)
    runtime = ep.get("runtime")
    runtime_s = f"{runtime} دقیقه" if runtime else "نامشخص"
    over = (ep.get("overview") or "").strip()
    if over:
        try:
            over = translate_to_farsi("", over)[1] or over
        except Exception:
            pass
    over = over or "خلاصه‌ای موجود نیست."
    text = (
        f"💿 *قسمت {ep_num}: {name}*\n\n"
        f"📅 تاریخ پخش: {air}\n"
        f"⭐ امتیاز: {rating:.1f}/10\n"
        f"⏱ مدت: {runtime_s}\n\n"
        f"📖 {over[:500]}"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=episode_keyboard(tmdb_id, season_num),
    )