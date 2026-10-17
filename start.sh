#!/bin/bash
# Start script: optional local Telegram Bot API server + bridge bot.
#
# برای ارسال یک‌جای فایل‌های بالای ۵۰ مگ، این وریبل‌ها رو توی Railway ست کن:
#   TELEGRAM_LOCAL_API=1
#   TELEGRAM_API_ID=<از https://my.telegram.org بگیر>
#   TELEGRAM_API_HASH=<از https://my.telegram.org بگیر>

if [ "$TELEGRAM_LOCAL_API" = "1" ]; then
    if [ -z "$TELEGRAM_API_ID" ] || [ -z "$TELEGRAM_API_HASH" ]; then
        echo "[start] خطا: TELEGRAM_LOCAL_API=1 ولی TELEGRAM_API_ID یا TELEGRAM_API_HASH ست نشده!" >&2
        echo "[start] از https://my.telegram.org بگیر و توی Variables ست کن." >&2
        exit 1
    fi
    mkdir -p /var/lib/telegram-bot-api
    echo "[start] راه‌اندازی سرور محلی Bot API روی 127.0.0.1:8081 ..."
    telegram-bot-api \
        --api-id="$TELEGRAM_API_ID" \
        --api-hash="$TELEGRAM_API_HASH" \
        --local \
        --http-port=8081 \
        --dir=/var/lib/telegram-bot-api \
        >>/tmp/botapi.log 2>&1 &
    # صبر کن تا سرور بالا بیاد (حداکثر ۶۰ ثانیه)
    for i in $(seq 1 60); do
        if (echo > /dev/tcp/127.0.0.1/8081) 2>/dev/null; then
            echo "[start] سرور Bot API بالا اومد ✅"
            break
        fi
        if [ "$i" = "60" ]; then
            echo "[start] خطا: سرور Bot API بالا نیومد! لاگ:" >&2
            tail -20 /tmp/botapi.log >&2 || true
            exit 1
        fi
        sleep 1
    done
else
    echo "[start] حالت عادی (API ابری تلگرام — سقف ۵۰ مگ)"
fi

echo "[start] اجرای بریج‌بات ..."
exec python bridge_bot.py
