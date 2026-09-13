FROM python:3.11-slim

# ffmpeg برای دانلودر و برش/ادغام صدا و ویدیو لازمه
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

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

# مسیر داده/دانلود پیش‌فرض داخل خود کانتینر
ENV BOT_BASE=/app
ENV DOWNLOAD_DIR=/app/downloads
ENV MOVIE_BOT_DIR=/app/movie_bot
RUN mkdir -p /app/downloads

CMD ["python", "bridge_bot.py"]
