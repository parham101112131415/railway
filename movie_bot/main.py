"""نقطهٔ ورود ربات movie_bot.

راه‌اندازی لاگر، دیتابیس، نصب وابستگی‌ها در صورت نیاز و اجرای
polling ربات تلگرام.
"""

from __future__ import annotations

# ─── اجرای اولیه پیش از هر ایمپورت نیازمند dotenv ───
import config  # noqa: F401 - بارگذاری .env و ساخت پوشه‌ها

from utils.logger import logger


def _banner() -> str:
    """نمایش متن خوش‌آمد در آغازه."""
    return (
        "\n"
        "🎬 ──────────────────────────────────────────\n"
        "   movie_bot | فیلم‌یاب از ریلز اینستاگرام\n"
        "───────────────────────────────────────────"
    )


def main() -> None:
    """اجرای اصلی ربات."""
    from database.sqlite import init_db
    from utils.installer import ensure_dependencies

    # بررسی و نصب وابستگی‌ها
    try:
        ok = ensure_dependencies(auto_install=True)
        if not ok:
            logger.warning("نصب وابستگی‌ها نافرم؛ ممکن است ربات خطا بدهد.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("خطا در نصب وابستگی‌ها (ادامه می‌دهیم): %s", exc)

    # راه‌اندازی دیتابیس
    init_db()
    logger.info("دیتابیس آماده شد.")

    from telegram import Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    from bot.callbacks import handle_callback
    from bot.handlers import (
        broadcast_handler,
        help_command,
        logs_handler,
        on_message,
        stats_handler,
        start,
        users_handler,
    )

    if not config.BOT_TOKEN:
        logger.error("توکن ربات در .env تنظیم نشده است.")
        return

    app = Application.builder().token(config.BOT_TOKEN).build()

    # ثبت هندلرها
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("users", users_handler))
    app.add_handler(CommandHandler("broadcast", broadcast_handler))
    app.add_handler(CommandHandler("logs", logs_handler))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # پاک‌ساز دوره‌ای موقت با job_queue (روی حلقه‌ی خودِ app اجرا می‌شود)
    from config import CLEANUP_INTERVAL_MIN
    from utils.cleanup import clean_old_files

    async def _cleanup_job(_: Application) -> None:
        clean_old_files()

    app.job_queue.run_repeating(
        _cleanup_job, interval=CLEANUP_INTERVAL_MIN * 60, first=0
    )

    logger.info("ربات movie_bot راه‌اندازی شد؛ در حال polling…")

    # run_polling حلقه‌ی خود را مدیریت می‌کند؛ بدون asyncio.run بیرونی
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=10,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    print(_banner())
    try:
        main()
    except KeyboardInterrupt:
        logger.info("ربات متوقف شد.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطای بحرانی: %s", exc)