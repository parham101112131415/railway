"""ماژول هندلرهای اصلی ربات movie_bot.

شامل /start، پردازش لینک اینستاگرام، صف‌بندی وظایف هر کاربر، محدودیت
روزانه و دستورات مدیریتی.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import (
    ADMIN_IDS,
    IMAGES_DIR,
    INSTAGRAM_MATCH,
    MAX_DAILY_DOWNLOADS,
    START_TEXT,
)
from bot.keyboards import movie_card_keyboard, start_keyboard
# (سیستم کش کالا حذف شد؛ نتایج دیگر ذخیره نمی‌شوند)
from database.sqlite import init_db, now_iso, transaction
from services.downloader import download_instagram_reel
from services.frame_extractor import extract_frames
from services.gemini import QuotaExceededError, identify_from_frames
from services.movie_api import build_movie_info
from utils.logger import logger
from utils.validator import extract_instagram_link, is_admin

# ─── صف وظایف هر کاربر (یک پردازش در یک زمان) ───
_queue_queues: dict[int, asyncio.Queue[str]] = {}


def _get_queue(user_id: int) -> asyncio.Queue[str]:
    """بازگرداندن صف اختصاصی یک کاربر (ایجاد در صورت نبود)."""
    if user_id not in _queue_queues:
        _queue_queues[user_id] = asyncio.Queue()
    return _queue_queues[user_id]


def init_bot_tables() -> None:
    """راه‌اندازی جدول‌ها در شروع ربات."""
    init_db()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هندلر `/start`."""
    user = update.effective_user
    if user:
        # ثبت کاربر در دیتابیس
        with transaction() as conn:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, last_name, joined_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "username=excluded.username, first_name=excluded.first_name",
                (
                    user.id,
                    user.username,
                    user.first_name,
                    user.last_name,
                    now_iso(),
                ),
            )
    if update.message:
        await update.message.reply_text(
            START_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=start_keyboard()
        )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دریافت پیام کاربر، استخراج لینک و شروع پردازش."""
    message = update.message
    if not message or not message.text:
        if update.edited_message:
            return
        return

    text: str = message.text or ""
    link = extract_instagram_link(text)
    if not link:
        await message.reply_text(
            "🔗 لینک اینستاگرام ندیدم! یک ریلز یا پست از اینستاگرام بفرست."
        )
        return

    user = update.effective_user
    user_id = user.id if user else 0

    # چک سقف روزانه
    if not is_admin(user_id, ADMIN_IDS):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with transaction() as conn:
            row = conn.execute(
                "SELECT count FROM usage WHERE user_id=? AND day=?", (user_id, day)
            ).fetchone()
            today_count = row["count"] if row else 0
        if today_count >= MAX_DAILY_DOWNLOADS:
            await message.reply_text(
                f"⛔ امروز به حد مجاز ({MAX_DAILY_DOWNLOADS}) رسیدی. فردا دوباره بیا."
            )
            return

    # جنبش در صف کاربر
    queue = _get_queue(user_id)
    if queue.qsize() >= 1:
        await message.reply_text("🕒 یک درخواست در حال پردازش است؛ کمی صبر کن.")
        return
    await queue.put(link)

    # اجرای پردازش به صورت غیرهمزمان
    await asyncio.create_task(_process_job(update, context, user_id, link))


async def _process_job(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, link: str
) -> None:
    """پایپلاین کامل: دانلود → فریم → تشخیص → اطلاعات → پیام."""
    try:
        await _process_reel(update, context, user_id, link)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطا در پردازش ریلز %s: %s", link, exc)
        try:
            await update.effective_chat.send_message(
                "❌ یه خطای غیرمنتظره پیش اومد. لطفاً دوباره تلاش کن."
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        # خارج کردن لینک از صف و حذف صف اگر خالی شد
        try:
            _queue_queues[user_id].get_nowait()
        except Exception:  # noqa: BLE001
            pass
        if user_id in _queue_queues and _queue_queues[user_id].qsize() == 0:
            del _queue_queues[user_id]


async def _safe_chat_send(update: Update, chat_id: int, text: str) -> None:
    """ارسال پیام ساده به چت با خطای نرم (حتی وقتی ارسال نشود کرش نمی‌کند)."""
    try:
        await update.get_bot().send_message(chat_id, text)
    except Exception:  # noqa: BLE001
        pass


async def _process_reel(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, link: str
) -> None:
    """انجام واقعی مراحل پردازش یک ریلز."""
    message = update.message
    chat_id = update.effective_chat.id

    last_status_id: int | None = None
    last_status_text: str = ""
    status_lock = asyncio.Lock()

    async def status(text: str) -> None:
        nonlocal last_status_id, last_status_text
        if not message:
            return
        # متن تکراری (مثل هوک‌های هم‌زمان چند بخش) اصلاً ارسال نمی‌شود
        if text == last_status_text:
            return
        # قفل تا دو تسک هم‌زمان دو پیام نسازند
        async with status_lock:
            if text == last_status_text:
                return
            # ویرایش همان پیام قبلی (نه ارسال پیام جدید چندباره)
            if last_status_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=last_status_id, text=text
                    )
                    last_status_text = text
                    print(f"[STATUS] edit->{last_status_id}: {text[:40]!r}", flush=True)
                    return
                except Exception as exc:  # noqa: BLE001
                    if "not modified" in str(exc):
                        last_status_text = text
                        return
                    print(f"[STATUS] EDIT-FAIL {str(exc)[:80]} -> reply", flush=True)
                    # پیام حذف شده / نامعتبر شده‌است → پیام جدید
            try:
                sent = await message.reply_text(text)
                last_status_id = sent.message_id
                last_status_text = text
                print(f"[STATUS] reply->{sent.message_id}: {text[:40]!r}", flush=True)
            except Exception:  # noqa: BLE001
                print(f"[STATUS] REPLY-FAIL {text[:40]!r}", flush=True)

    await status("🔎 لینک دریافت شد؛ در حال شروع تشخیص…")

    # ۲) دانلود
    video_path = await download_instagram_reel(link, dest_dir=None, status_cb=status)
    if not video_path:
        await _safe_chat_send(update, chat_id, "❌ دانلود ناموفق بود. لینک را چک کن.")
        return

    # ۳) استخراج فریم (مؤقت؛ بعد از تشخیص پاک می‌شود)
    frames: list[Path] = []
    try:
        frames = extract_frames(video_path, IMAGES_DIR)
        if not frames:
            await _safe_chat_send(update, chat_id, "❌ فریمی استخراج نشد.")
            return

        # ۴) تشخیص با Gemini
        try:
            result = await identify_from_frames(frames)
        except QuotaExceededError:
            await _safe_chat_send(
                update, chat_id,
                "⏳ سهمیهٔ رایگان Gemini لحظه‌ای پر شده. لطفاً حدود یک دقیقه صبر کن و دوباره تلاش کن.",
            )
            return
        if not result or not result.get("title"):
            await _safe_chat_send(update, chat_id, "🤷‌‍♂️ نتوانستم فیلم را تشخیص دهم. تلاش کن.")
            return

        # ۵) ساخت اطلاعات نهایی
        info = await build_movie_info(result, reel_url=link)
        if not info:
            await _safe_chat_send(update, chat_id, "🤷‌‍♂️ اطلاعاتی برای این عنوان پیدا نشد.")
            return

        # ذخیره در تاریخچه و کش
        with transaction() as conn:
            conn.execute(
                "INSERT INTO history (user_id, instagram_url, title, media_type, "
                "year, imdb_id, searched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, link, info["title"], info.get("type_key") or "movie",
                 info.get("year") or "", info.get("imdb_id"), now_iso()),
            )
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            conn.execute(
                """INSERT INTO usage (user_id, day, count) VALUES (?, ?, 1)
                   ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1""",
                (user_id, day),
            )
        # (حذف شد: دیگر نتیجه‌ای کش نمی‌کنیم تا هر بار از نو جستجو شود)

        # حذف آخرین پیام وضعیت و ارسال نتیجه به‌جای آن
        if last_status_id:
            try:
                await context.bot.delete_message(chat_id, last_status_id)
            except Exception:  # noqa: BLE001
                pass
            last_status_id = None

        await _send_movie_result(context, chat_id, info)

        # اطلاع به مالک وقتی شخص دیگری درخواست داده (مثل bot.py)
        if ADMIN_IDS and not is_admin(user_id, ADMIN_IDS):
            try:
                owner_id = ADMIN_IDS[0]
                u = update.effective_user
                name = u.username if (u and u.username) else (
                    f"{u.first_name or ''} {u.last_name or ''}".strip() or str(user_id)
                )
                who = f"@{name}" if u and u.username else name
                await context.bot.send_message(
                    owner_id, f"📩 پیام جدید از {who} (ID: {user_id})\n🔗 {link}"
                )
                await _send_movie_result(context, owner_id, info)
            except Exception:  # noqa: BLE001
                pass
    finally:
        # پاک‌سازی موقت: ویدیو و فریم‌های استخراج‌شده ذخیره نمی‌مانند
        for p in frames:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            video_path.unlink(missing_ok=True)
        except OSError:
            pass


async def _send_movie_result(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, info: dict, message: Message | None = None
) -> None:
    """ارسال نتیجه نهایی: پوستر (کپشن کوتاه) + متن کامل با دکمه‌ها."""
    from bot.callbacks import send_movie_card

    await send_movie_card(context, chat_id, info)


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دستور /stats برای ادمین."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ دسترسی ندارید")
        return
    with transaction() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        hist = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    await update.message.reply_text(
        f"📊 *آمار* \n👥 کاربران: {users}\n🔎 جستجو: {hist}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """لیست کاربران (ادمین)."""
    if not _is_admin(update):
        return
    with transaction() as conn:
        rows = conn.execute(
            "SELECT user_id, username, first_name FROM users ORDER BY joined_at DESC LIMIT 20"
        ).fetchall()
    lines = "\n".join(
        f"`{r['user_id']}` @{(r['username'] or '-')} {(r['first_name'] or '')}" for r in rows
    )
    await update.message.reply_text(f"*کاربران:*\n{lines}", parse_mode=ParseMode.MARKDOWN)


async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دستور /broadcast پیام به همه؛ فرم: متن."""
    if not _is_admin(update):
        return
    text = (update.message.text or "").replace("/broadcast", "", 1).strip()
    if not text:
        await update.message.reply_text("فرمت:\n/broadcast متن پیام")
        return
    with transaction() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
    sent = 0
    for row in rows:
        try:
            await context.bot.send_message(row[0], text)
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    await update.message.reply_text(f"ارسال به {sent} کاربر انجام شد.")


async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """اقوت آخر لاگ برای ادمین."""
    if not _is_admin(update):
        return
    from config import LOG_FILE

    try:
        data = LOG_FILE.read_text(encoding="utf-8", errors="ignore")
        trimmed = "\n".join(data.splitlines()[-40:])
        await update.message.reply_text(f"<pre>{trimmed}</pre>", parse_mode=ParseMode.HTML)
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"خطا: {exc}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_TEXT, parse_mode=ParseMode.MARKDOWN)


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


