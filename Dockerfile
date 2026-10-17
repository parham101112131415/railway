# ---- Stage 1: compile telegram-bot-api from source (Debian/glibc) ----
FROM ubuntu:24.04 AS botapi-builder
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git zlib1g-dev libssl-dev gperf \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --recursive --depth 1 https://github.com/tdlib/telegram-bot-api.git
WORKDIR /src/telegram-bot-api/build
RUN cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/botapi .. \
    && cmake --build . --target install -j"$(nproc)" \
    && strip /opt/botapi/bin/telegram-bot-api || true

# ---- Stage 2: main app ----
FROM python:3.11-slim

# ffmpeg برای دانلودر و برش/ادغام صدا و ویدیو لازمه
# libssl3/zlib1g برای باینری telegram-bot-api لازمه
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    unzip \
    libssl3 \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

# سرور محلی Bot API (برای ارسال یک‌جای فایل‌های بالای ۵۰ مگ)
COPY --from=botapi-builder /opt/botapi/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

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
