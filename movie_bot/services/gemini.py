"""ماژول تشخیص فیلم با Gemini Vision.

فریم‌های استخراج‌شده به همراه پرامپت اختصاصی به مدل Gemini ارسال
می‌شوند تا عنوان فیلم یا سریال تشخیص داده شود و خروجی به صورت JSON
برگردد. تلاش مجدد با تأخیر نمایی برای خطاهای موقت انجام می‌شود.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_RETRIES,
    GEMINI_TIMEOUT,
)
import config as _cfg

# GROQ_API_KEY/GROQ_MODEL/GROQ_TIMEOUT اختیاری‌ان: اگه کاربر عمداً Groq رو از
# config.py حذف کرده باشه (یا هنوز اضافه نکرده)، به‌جای کرش کل ماژول (که همه‌ی
# قابلیت فیلم‌یاب رو خاموش می‌کنه)، فقط بک‌آپ Groq غیرفعال می‌مونه.
GROQ_API_KEY = getattr(_cfg, "GROQ_API_KEY", None)
GROQ_MODEL = getattr(_cfg, "GROQ_MODEL", "qwen/qwen3.6-27b")
GROQ_TIMEOUT = getattr(_cfg, "GROQ_TIMEOUT", 60)

from utils.logger import logger


class QuotaExceededError(RuntimeError):
    """سهمیه رایگان Gemini تمام شده؛ کاربر باید چند لحظه صبر کند."""

# پرامپت سیستم/دستورالعمل ارسال به مدل
_DETECTION_PROMPT = """
شما یک کارشناس تشخیص فیلم هستید. چند فریم تصویری از لینک اینستاگرام به شما
داده‌شده که حاوی صحنه‌ای از یک فیلم یا سریال یا کارتون است.

وظیفه شما: تشعمیل دقیق و کامل در یک پاسخ.

لطفا فقط یک شیء JSON بازگردانید، بدون هیچ متن اضافه‌ای در اطراف آن، به این شکل:

{
  "title": "نام فیلم یا سریال (لاتین)",
  "original_title": "عنوان اصلی به زبان خود فیلم",
  "type": "movie" یا "series" یا "unknown",
  "release_year": "سال انتشار یا null",
  "confidence": 0 تا 1,
  "alternative": ["نام احتمالی دوم", "نام احتمالی سوم"]
}

قواعد:
- اگر مطمئن بودید، confidence بالا بگیرید (مثلاً 0.9).
- اگر چند نام محتمل بود، همه را in alternative قرار دهید.
- عنوان و original_title حتماً به زبان لاتین (انگلیسی/اصلی) باشد.
- اگر هیچ‌چیز مشخص نبود، title را خالی و confidence=0 بگذارید.
"""


# پرامپت تشخیص با دقت بالاتر — وقتی کاربر نتیجهٔ قبلی را رد می‌کند.
# روی گزینه‌های جایگزین (alternative) تمرکز می‌کند و جزئیات بصری را دقیق‌تر بررسی می‌کند.
_DETECTION_PROMPT_HIGH = """
شما یک کارشناس بسیار دقیق تشخیص فیلم هستید. چند فریم تصویری از یک ریلز اینستاگرام
به شما داده‌شده که حاوی صحنه‌ای از یک فیلم، سریال یا کارتون است.

کاربر اعلام کرده نتیجهٔ قبلی اشتباه بوده؛ بنابراین:
- اگر گزینهٔ اولِ حدس قبلی اشتباه بود، احتمال‌های دوم و سوم (alternative) را جدی‌تر بررسی کن.
- به جزئیات بصری دقیق توجه کن: لباس، معماری، نژاد بازیگران، زبان تابلوها، سال‌های تقریبی
  سبک فیلم‌برداری، چهره‌های شناخته‌شده، لوگوهای استودیو.
- حدس خود را با دلیلِ کوتاهی که چرا این فیلم است همراه کن.

فقط یک شیء JSON بازگردانید، بدون هیچ متن اضافه‌ای:
{
  "title": "نام فیلم یا سریال (لاتین)",
  "original_title": "عنوان اصلی به زبان خود فیلم",
  "type": "movie" یا "series" یا "unknown",
  "release_year": "سال انتشار یا null",
  "confidence": 0 تا 1,
  "reason": "دلیل تشخیص کوتاه",
  "alternative": ["نام احتمالی دوم", "نام احتمالی سوم"]
}

قواعد:
- اگر مطمئن بودید confidence بالا (مثلاً 0.9) وگرنه صادقانه عدد بزنید.
- اگر چند نام محتمل بود همه را in alternative قرار دهید.
- عنوان و original_title حتماً به لاتین باشد.
- اگر هیچ‌چیز مشخص نبود، title را خالی و confidence=0 بگذارید.
"""

# الگوی JSON برای استخراج از پاسخ مدل (برای مواردی که مدل متن اضافه می‌آید)
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """استخراج یک object JSON از پاسخ متن مدل."""
    if not text:
        return None
    # اول بلوک کد json
    block = _JSON_BLOCK_RE.search(text)
    if block:
        text = block.group(1)
    else:
        obj = _JSON_OBJECT_RE.search(text)
        if obj:
            text = obj.group(0)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # تلاش نهایی: حذف کاماهای دنباله‌دار اشتباه
        cleaned = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.error("پاسخ مدل JSON معتبر نبود: %r", text[:200])
            return None


def _frame_to_jpeg(fp: Path, max_width: int = 480, quality: int = 70) -> bytes:
    """کوچک‌سازی فریم قبل از ارسال به Gemini (کاهش شدید حجم درخواست)."""
    from io import BytesIO

    from PIL import Image

    with Image.open(fp) as img:
        img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, max(1, round(img.height * ratio))))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


_AUTO_MODEL_CACHE: dict = {}  # {"candidates": [...], "good": "modelname", "bad": {...}}


def _auto_candidates() -> list[str]:
    """لیست مدل‌های موجود حساب رو می‌گیره و بر اساس اولویت مرتب می‌کنه (کش می‌شه)."""
    import requests

    if "candidates" in _AUTO_MODEL_CACHE:
        return _AUTO_MODEL_CACHE["candidates"]

    try:
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": GEMINI_API_KEY},
            timeout=GEMINI_TIMEOUT,
        )
        resp.raise_for_status()
        models = resp.json().get("models", [])
        # فقط مدل‌های متنیِ عمومی که برای چت/JSON مناسبن — نه مدل‌های تخصصیِ
        # تولید عکس/صدا/ویدیو/embedding/TTS که فنیاً «generateContent» دارن ولی
        # برای درخواست ما (پرامپت متنی → JSON) اصلاً کاربردی نیستن و یا خطای
        # عجیب می‌دن یا رفتار غیرمنتظره دارن.
        _BAD_PATTERNS = (
            "image", "vision", "embed", "aqa", "tts", "audio", "speech",
            "video", "live", "native-audio", "computer-use", "robotics",
            "learnlm", "gemma", "veo", "imagen",
        )
        candidates = [
            m["name"].split("/")[-1]
            for m in models
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
            and not any(p in m["name"].split("/")[-1].lower() for p in _BAD_PATTERNS)
        ]

        def _rank(name: str) -> tuple:
            n = name.lower()
            return (
                0 if ("flash" in n and "lite" not in n and "preview" not in n and "exp" not in n) else
                1 if "flash" in n else
                2,
                name,
            )

        candidates.sort(key=_rank)
        if not candidates:
            candidates = ["gemini-flash-latest"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("لیست مدل‌های Gemini گرفته نشد (%s)؛ fallback به gemini-flash-latest.", exc)
        candidates = ["gemini-flash-latest"]

    _AUTO_MODEL_CACHE["candidates"] = candidates
    _AUTO_MODEL_CACHE.setdefault("bad", set())
    return candidates


def _next_auto_model() -> str | None:
    """مدل بعدیِ کش‌نشده/بد را از لیست کاندیدها برمی‌گرداند."""
    good = _AUTO_MODEL_CACHE.get("good")
    if good and good not in _AUTO_MODEL_CACHE.get("bad", set()):
        return good
    bad = _AUTO_MODEL_CACHE.setdefault("bad", set())
    for c in _auto_candidates():
        if c not in bad:
            return c
    return None


def _mark_auto_model_bad(name: str) -> None:
    _AUTO_MODEL_CACHE.setdefault("bad", set()).add(name)
    if _AUTO_MODEL_CACHE.get("good") == name:
        _AUTO_MODEL_CACHE.pop("good", None)


def _mark_auto_model_good(name: str) -> None:
    _AUTO_MODEL_CACHE["good"] = name


def _rest_generate(prompt: str, images: list[tuple[str, bytes]], model: str | None = None) -> str:
    """فراخوانی مستقیم REST گوگل Gemini بدون SDK (که روی Termux build نمی‌شود).

    `model` اختیاریه؛ اگه ندی از GEMINI_MODEL پیش‌فرض استفاده می‌شه — و اگه
    GEMINI_MODEL خودش "auto" باشه، بین مدل‌های موجود حساب یکی امتحان می‌شه؛
    اگه ۴۰۴ داد (مدل بازنشسته/در دسترس‌نبوده)، خودکار می‌ره سراغ مدل بعدی تو
    لیست (نه همون یکی) تا یکی جواب بده.
    """
    import base64

    import requests

    parts: list[dict] = [{"text": prompt}]
    for mime_type, data in images:
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(data).decode("ascii"),
                }
            }
        )
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    def _post(use_model: str):
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{use_model}:generateContent"
        )
        return requests.post(
            endpoint, params={"key": GEMINI_API_KEY}, json=payload, timeout=GEMINI_TIMEOUT,
        )

    fixed_model = model or GEMINI_MODEL
    if fixed_model != "auto":
        resp = _post(fixed_model)
    else:
        resp = None
        tried = []
        for _ in range(len(_auto_candidates()) + 1):
            use_model = _next_auto_model()
            if not use_model or use_model in tried:
                break
            tried.append(use_model)
            logger.info("Gemini مدل انتخاب‌شده: %s", use_model)
            resp = _post(use_model)
            if resp.status_code == 404:
                logger.warning("مدل %s در دسترس نیست (۴۰۴)؛ مدل بعدی امتحان می‌شود.", use_model)
                _mark_auto_model_bad(use_model)
                continue
            _mark_auto_model_good(use_model)
            break
        if resp is None:
            raise RuntimeError("هیچ مدل Gemini‌ای برای این کلید در دسترس نیست.")

    if resp.status_code in (401, 403):
        raise RuntimeError(
            "کلید GEMINI_API_KEY نامعتبر است؛ یک کلید تازه از AI Studio بگیر و بذار."
        )
    if resp.status_code == 429:
        tail = (GEMINI_API_KEY or "")[-6:]
        raise QuotaExceededError(f"سهمیه رایگان Gemini تمام شده است. (کلید ختم‌شونده به …{tail})")
    resp.raise_for_status()
    tree = resp.json()
    try:
        return tree["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Gemini پاسخ غیرمنتظره: {str(tree)[:200]}")


def _rest_generate_groq(prompt: str, images: list[tuple[str, bytes]]) -> str:
    """فراخوانی Groq (Llama 4 Scout — ویژن) به‌عنوان بک‌آپ Gemini.

    Groq یک API سازگار با فرمت OpenAI دارد؛ سهمیهٔ رایگانش
    (۳۰ درخواست/دقیقه، ۱۰۰۰ درخواست/روز) خیلی بیشتر از Gemini فعلیه.
    """
    import base64

    import requests

    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY تنظیم نشده — به .env اضافه کن.")

    # مدل ویژن Groq فعلاً حداکثر ۵ عکس در هر درخواست قبول می‌کند.
    images = images[:5]

    content: list[dict] = [{"type": "text", "text": prompt}]
    for mime_type, data in images:
        b64 = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64}"},
            }
        )

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.7,
        "reasoning_effort": "none",
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=GROQ_TIMEOUT,
    )
    if resp.status_code == 429:
        raise QuotaExceededError("سهمیه رایگان Groq هم تمام شده است.")
    if resp.status_code >= 400:
        raise RuntimeError(f"Groq {resp.status_code}: {resp.text[:500]}")
    tree = resp.json()
    try:
        return tree["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Groq پاسخ غیرمنتظره: {str(tree)[:200]}")


# پرامپت ترجمه: شعار و خلاصهٔ فیلم به فارسی روان
_TRANSLATE_PROMPT = """
تو یک مترجم حرفه‌ای انگلیسی به فارسی هستی که خلاصه‌های فیلم را ترجمه می‌کند.
دو فیلد زیر (شعار فیلم و خلاصهٔ داستان) را به فارسی روان و طبیعی ترجمه کن.
خروجی فقط یک شیء JSON بدون هیچ متن اضافه:
{"tagline_fa": "ترجمهٔ شعار یا خالی", "plot_fa": "ترجمهٔ خلاصه یا خالی"}
قوانین:
- ترجمه ادبی و طبیعی باشد، نه تحت‌اللفظی.
- نام‌های جغرافیایی و تاریخی پراستفاده را فارسی بنویس (فرانسه، آلمان، نازی، آمریکا، روسیه، ژاپن، لندن، پاریس و…).
- نام شخصیت‌ها و عنوان فیلم به لاتین می‌ماند.
- اگر فیلدی خالی بود مقدار آن را در JSON خالی بگذار.
"""


def _mymemory_fa(text: str) -> str:
    """ترجمهٔ رایگان MyMemory (انگلیسی → فارسی)."""
    import requests

    resp = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text, "langpair": "en|fa"},
        timeout=8,
    )
    resp.raise_for_status()
    out = resp.json().get("responseData", {}).get("translatedText") or ""
    return str(out).strip()


def _google_fa(text: str) -> str:
    """ترجمهٔ رایگان Google (endpoint غیررسمی gtx)."""
    import requests

    resp = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "en", "tl": "fa", "dt": "t", "q": text},
        timeout=8,
    )
    resp.raise_for_status()
    parts = resp.json()[0]
    return "".join(seg[0] for seg in parts if seg and seg[0]).strip()


def _fallback_fa(text: str) -> str:
    """ترجمه با مترجم آزاد؛ در شکست، متن اصلی برمی‌گردد."""
    if not text:
        return ""
    try:
        if out := _mymemory_fa(text):
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("MyMemory ناموفق: %s", exc)
    try:
        if out := _google_fa(text):
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google ترجمه ناموفق: %s", exc)
    return text


def translate_to_farsi(tagline: str, plot: str) -> tuple[str, str]:
    """ترجمهٔ شعار و خلاصهٔ فیلم به فارسی.

    زنجیره: ۱) Groq (سریع و در دسترس)  ۲) Gemini  ۳) MyMemory  ۴) Google
    ۵) متن اصلی. کاملاً ایمن — در هر نوع شکست متن اصلی برمی‌گردد.
    """
    tagline = (tagline or "").strip()
    plot = (plot or "").strip()
    if not (tagline or plot):
        return tagline, plot

    prompt = (
        _TRANSLATE_PROMPT
        + f"\nشعار (tagline): {tagline or '—'}\nخلاصه (overview): {plot or '—'}"
    )

    if GROQ_API_KEY:
        try:
            text = _rest_generate_groq(prompt, [])
            data = _extract_json(text) or {}
            tagline_fa = str(data.get("tagline_fa") or "").strip()
            plot_fa = str(data.get("plot_fa") or "").strip()
            if tagline_fa or plot_fa:
                return (tagline_fa or tagline), (plot_fa or plot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ترجمه Groq انجام نشد (%s)؛ تلاش Gemini.", exc)

    try:
        text = _rest_generate(prompt, [])
        data = _extract_json(text) or {}
        tagline_fa = str(data.get("tagline_fa") or "").strip()
        plot_fa = str(data.get("plot_fa") or "").strip()
        if tagline_fa or plot_fa:
            return (tagline_fa or tagline), (plot_fa or plot)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ترجمه Gemini انجام نشد (%s)؛ تلاش مترجم رایگان.", exc)
    return _fallback_fa(tagline), _fallback_fa(plot)


async def identify_from_frames(
    frame_paths: list[Path], precision: str = "low"
) -> dict | None:
    """ارسال فریم‌ها به Gemini و بازگرداندن نتیجه تشخیص.

    precision:
      - "low"  → پرامپت معمولی (اولین تلاش)
      - "high" → پرامپت دقیق‌تر (وقتی کاربر نتیجهٔ قبلی را رد کرده)

    در صورت شکست همه تلاش‌ها `None` برمی‌گرداند.
    """
    if not frame_paths:
        logger.warning("چیزی به تشخیص خبر نگرفت؛ فریم خالی است.")
        return None

    prompt = _DETECTION_PROMPT_HIGH if precision == "high" else _DETECTION_PROMPT

    # بارگذاری تصاویر
    images: list[tuple[str, bytes]] = []
    for fp in frame_paths[:10]:
        if fp.exists():
            images.append(("image/jpeg", _frame_to_jpeg(fp)))

    if not images:
        logger.error("هیچ یه تصویر قابل خواندن نبود.")
        return None

    last_error: str | None = None
    gemini_quota_hit = False
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            await asyncio.sleep(min(2 * (attempt - 1), 6))
            text = await asyncio.to_thread(
                _rest_generate, prompt, images
            )
            result = _extract_json(text)
            if result and result.get("title"):
                return result
            last_error = "پاسخ بدون عنوان تشخیص داده شد."
            logger.warning("مـچ result بدون title: %r", text[:200])
        except asyncio.CancelledError:
            raise
        except QuotaExceededError:
            # سهمیهٔ Gemini پر شده — بدون هدر رفتن روی ری‌ترای، مستقیم برو سراغ Groq.
            gemini_quota_hit = True
            logger.warning("سهمیه Gemini پر شد؛ سوییچ به Groq (بک‌آپ).")
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("تلاش %d/%d Gemini ناموفق: %s", attempt, GEMINI_RETRIES, last_error)
            await asyncio.sleep(3 * attempt)

    # ─── بک‌آپ: اگر Gemini شکست خورد (به‌خصوص سهمیهٔ پر) با Groq امتحان کن ───
    if GROQ_API_KEY:
        try:
            text = await asyncio.to_thread(_rest_generate_groq, prompt, images)
            result = _extract_json(text)
            if result and result.get("title"):
                return result
            last_error = "پاسخ Groq بدون عنوان بود."
            logger.warning("نتیجهٔ Groq بدون title: %r", text[:200])
        except asyncio.CancelledError:
            raise
        except QuotaExceededError as exc:
            last_error = str(exc)
            logger.warning("سهمیه Groq هم پر شد: %s", last_error)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("Groq (بک‌آپ) هم ناموفق: %s", last_error)
    elif gemini_quota_hit:
        last_error = "سهمیه رایگان Gemini تمام شده است."

    if last_error and "سهمیه" in last_error:
        raise QuotaExceededError(last_error)
    logger.error("تشخیص (Gemini+Groq) ناموفق: %s", last_error)
    return None