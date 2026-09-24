# تنظیمات ربات بریج دانلودر — نسخه Railway
# مقادیر پیش‌فرض همون توکن/کلیدهای قبلی‌ان (مثل نسخه VPS) و بدون نیاز به
# ست‌کردن چیزی روی Railway کار می‌کنن. اگه بعداً خواستی، هر کدوم رو می‌تونی
# با یه Environment Variable هم‌نام override کنی.

import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8848609316:AAFwYfe4Av3Qw_9XbNWoEDJdvqOyhGUyGOs"
# فقط این آیدی‌ها مجازند (می‌تونی با کاما جدا و در Variables عوض کنی، مثل: 111,222)
_owner_env = os.environ.get("OWNER_ID", "")
if _owner_env.strip():
    OWNER_ID = {int(x) for x in _owner_env.replace(" ", "").split(",") if x}
else:
    OWNER_ID = {8055210419, 8905260615}

# دسترسی محدود: فقط ریلز اینستا → دانلود مستقیم + دکمه «پیدا کردن آهنگ».
# بقیه‌ی فیچرها (کیفیت، فیلم‌یاب، لینک‌سازی، ...) براشون قفله.
# (8905260615 موقتاً اضافه شده که پرهام ببینه راکی چی می‌بینه — بعد تست بردار!)
_limited_env = os.environ.get("LIMITED_USER_IDS", "")
if _limited_env.strip():
    LIMITED_USERS = {int(x) for x in _limited_env.replace(" ", "").split(",") if x}
else:
    LIMITED_USERS = {5341492953, 8905260615}

# مسیرها روی کانتینر Railway — پیش‌فرض داخل خود پروژه، همیشه قابل‌نوشتنه
BASE_DIR = os.environ.get("BOT_BASE", "/app")
DOWNLOADER_PATH = os.path.join(BASE_DIR, "downloader.py")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# روی Railway (خارج از ایران) معمولاً پروکسی لازم نیست.
PROXY_URL = os.environ.get("PROXY_URL", "")

# کلیدهای سرویس‌های جانبی (اختیاری) — هرکدوم رو نداری خالی بذار، فیچر مربوطه خاموش می‌مونه
AUDD_API_TOKEN = os.environ.get("AUDD_API_TOKEN", "f9b21e8fb14b9dbda1e213cccd8dc2e3")
ACR_HOST = os.environ.get("ACR_HOST", "identify-eu-west-1.acrcloud.com")
ACR_ACCESS_KEY = os.environ.get("ACR_ACCESS_KEY", "06109520263592466557ed7f8afac7fe")
ACR_ACCESS_SECRET = os.environ.get("ACR_ACCESS_SECRET", "HNrVku2BdWJ0J96kdiJ71jyMxjr0mMCOOq5mX6ql")
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "apify_api_piDgZl07Z3DCaihmw2oqdWckjLdAqg1df34I")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
YOUTUBE_DATA_API_KEY = os.environ.get("YOUTUBE_DATA_API_KEY", "AIzaSyCQpfcVpwpZg_glpZZWccb7Q-1vYHcER-s")

# محدودیت تلگرام برای ربات: ۵۰ مگ — تکه‌ها را حدود ۴۵ مگ می‌فرستیم.
# اگه TELEGRAM_LOCAL_API=1 باشه (سرور محلی Bot API)، سقف میره تا ~۱۹۰۰ مگ و
# فایل‌ها یک‌جا و کامل ارسال می‌شن (مثل NextSaverBot) — بدون تکه‌تکه.
TELEGRAM_LOCAL_API = os.environ.get("TELEGRAM_LOCAL_API", "0") == "1"
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "http://127.0.0.1:8081").rstrip("/")
_LOCAL_MAX = 1900 * 1024 * 1024
TELEGRAM_MAX_BYTES = int(os.environ.get(
    "TELEGRAM_MAX_BYTES", _LOCAL_MAX if TELEGRAM_LOCAL_API else 50 * 1024 * 1024))
SPLIT_CHUNK_BYTES = int(os.environ.get(
    "SPLIT_CHUNK_BYTES", _LOCAL_MAX if TELEGRAM_LOCAL_API else 45 * 1024 * 1024))

# بعد از ارسال موفق، فایل لوکال پاک شود تا دیسک پر نشود
DELETE_AFTER_SEND = os.environ.get("DELETE_AFTER_SEND", "1") != "0"

# جاروی خودکار دیسک: فایل‌های دانلود قدیمی‌تر از این ساعت در شروع ربات پاک
# می‌شن (به‌جز فایل‌های کاتالوگ اینستا که برای لینک‌های اشتراک نگه داشته می‌شن).
# یوتیوب و بقیه بعد از ارسال هم پاک می‌شن، پس volume پر نمی‌شه.
CLEANUP_AFTER_HOURS = float(os.environ.get("CLEANUP_AFTER_HOURS", "6"))
