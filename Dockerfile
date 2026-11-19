# باینری telegram-bot-api از قبل کامپایل‌شده توی botapi-bin/ هست؛ کامپایل از سورس
# روی بیلدر Railway به سقف زمانی بیلد می‌خورد (DeadlineExceeded وسط کامپایل tdlib)
FROM python:3.11-slim

# ffmpeg برای دانلودر و برش/ادغام صدا و ویدیو لازمه
# libssl3/zlib1g/libstdc++6 برای باینری telegram-bot-api لازمه
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    unzip \
    libssl3 \
    zlib1g \
    libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# سرور محلی Bot API (برای ارسال یک‌جای فایل‌های بالای ۵۰ مگ)
COPY botapi-bin/telegram-bot-api /usr/local/bin/telegram-bot-api
RUN chmod +x /usr/local/bin/telegram-bot-api

# جاوااسکریپت‌ رانتایم (deno) — یوتیوب این اواخر خیلی وقت‌ها بدون حل یه
# چالش JS هیچ فرمتی برنمی‌گردونه (همون ارور «Requested format is not
# available» با همه‌ی کلاینت‌ها). yt-dlp خودش اگه deno رو تو PATH ببینه
# ازش استفاده می‌کنه.
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && mv /root/.deno/bin/deno /usr/local/bin/deno
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# مسیر داده/دانلود پیش‌فرض داخل خود کانتینر
ENV BOT_BASE=/app
ENV DOWNLOAD_DIR=/app/downloads
ENV MOVIE_BOT_DIR=/app/movie_bot
RUN mkdir -p /app/downloads /var/lib/telegram-bot-api

CMD ["/app/start.sh"]
