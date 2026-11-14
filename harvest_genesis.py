#!/usr/bin/env python3
"""Harvester: drives @parham_test_1_bot (Genesis) with the owner's user
account, fetches the song, then forwards it to @parham_youtube_downloader_bot
so that bot mints its OWN Telegram file_id for the file (file_ids are
bot-scoped and cannot be copied directly).

Protocol with the main bot:
  success: send "/hvkey <key>" then forward the audio message.
  failure: send "/hvfail <key>".
Usage: harvest_genesis.py --key KEY --query "Artist Title"
       harvest_genesis.py --key KEY --link <instagram reel url>
       (link mode: خودِ لینک ریلز داده می‌شه به جنسیس؛ دکمه‌ی «شناسایی
       موسیقی» زده می‌شه تا جنسیس خودش آهنگ رو شناسایی و استخراج کنه.)
Runs under ~/workspace/.venv (Telethon) with the dedicated session copy
genesis_harvest.session (egress via ghost-xray SOCKS5 10808).
"""
import argparse, asyncio, os, re, sys, time

from telethon import TelegramClient, events
from telethon.sessions import StringSession
import socks

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "id-lookup-bot"))
ENV = {}
_env_file = os.path.join(BASE, ".env")
if os.path.exists(_env_file):
    for line in open(_env_file, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            ENV[k.strip()] = v.strip()
# روی ریلوی اون فایل نیست — API_ID/API_HASH از متغیرهای محیطی میان
for _k in ("API_ID", "API_HASH"):
    if os.environ.get(_k):
        ENV[_k] = os.environ[_k]

SESSION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "genesis_harvest")
GENESIS = "parham_test_1_bot"
OWNBOT = "parham_youtube_downloader_bot"


async def _fail(client, own, key, code, msg):
    try:
        await client.send_message(own, f"/hvfail {key}")
    except Exception:
        pass
    try:
        await client.disconnect()
    except Exception:
        pass
    print(msg, flush=True)
    return code


async def _link_mode(client, gen, own, key, url):
    """لینک ریلز رو می‌ده به جنسیس، دکمه‌ی «🎵 شناسایی موسیقی 🎵» رو می‌زنه
    و آدیویی که جنسیس می‌سازه رو به بات خودمون فوروارد می‌کنه.
    بودجه‌ی کلی: ~۱۵۰ ثانیه؛ همه‌ی انتظارها فال‌بک نظرسنجی از تاریخچه دارن."""
    deadline = time.time() + 150
    state = {"floor": 0, "video": None, "audio": None}

    def _is_video(m):
        if getattr(m, "video", None):
            return True
        doc = getattr(m, "document", None)
        return bool(doc and (doc.mime_type or "").startswith("video"))

    def _note(m):
        if m is None or m.out:
            return
        if state["floor"] and m.id <= state["floor"]:
            return
        if m.audio:
            if state["audio"] is None:
                state["audio"] = m
        elif _is_video(m):
            cur = state["video"]
            # نسخه‌ی تازه‌ی همون پیام (مثلاً بعد از ادیت و اضافه‌شدن دکمه‌ها)
            # جای قبلی رو می‌گیره تا دکمه‌ها همیشه به‌روز باشن
            if cur is None or m.id == cur.id:
                state["video"] = m

    @client.on(events.NewMessage(from_users=gen))
    async def _lh1(ev):
        _note(ev.message)

    @client.on(events.MessageEdited(from_users=gen))
    async def _lh2(ev):
        _note(ev.message)

    sent = await client.send_message(gen, url)
    state["floor"] = sent.id
    print(f"[hv] link sent: {url} key={key} floor={sent.id}", flush=True)

    async def _poll():
        try:
            async for m in client.iter_messages(gen, limit=8):
                if state["floor"] and m.id > state["floor"]:
                    _note(m)
        except Exception:
            pass

    # فاز ۱: ویدیوی ریلز + دکمه‌ی شناسایی موسیقی
    clicked = False
    while time.time() < deadline and not clicked and state["audio"] is None:
        vm = state["video"]
        if vm is not None:
            try:
                btns = await vm.get_buttons()
            except Exception:
                btns = None
            if btns:
                for r, row in enumerate(btns):
                    hit = None
                    for c, b in enumerate(row):
                        if b.text and "شناسایی موسیقی" in b.text:
                            hit = (r, c, b.text)
                            break
                    if hit:
                        print(f"[hv] clicking identify button r{hit[0]} c{hit[1]}: {hit[2]!r}", flush=True)
                        try:
                            await vm.click(hit[0], hit[1])
                        except Exception as e:
                            print(f"[hv] identify click failed: {e!r}", flush=True)
                        clicked = True
                        break
        if not clicked:
            await _poll()
            await asyncio.sleep(1.5)

    if state["audio"] is None and not clicked:
        return await _fail(client, own, key, 5, "NO_VIDEO_OR_BUTTON")

    # فاز ۲: آدیوی ساخته‌شده توسط جنسیس
    while time.time() < deadline and state["audio"] is None:
        await _poll()
        if state["audio"] is None:
            await asyncio.sleep(1.5)

    audio_msg = state["audio"]
    if audio_msg is None:
        return await _fail(client, own, key, 3, "NO_AUDIO")

    await client.send_message(own, f"/hvkey {key}")
    await client.forward_messages(own, audio_msg)
    await asyncio.sleep(1)
    await client.disconnect()
    print("OK", flush=True)
    return 0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--query")
    ap.add_argument("--link")
    args = ap.parse_args()
    if not args.query and not args.link:
        ap.error("--query or --link is required")

    # سشن: روی ریلوی از متغیر مخفی GENESIS_SESSION_STRING (رشته‌ی سشن)،
    # اینجا از فایل genesis_harvest.session. پروکسی: پیش‌فرض ساکس محلی؛
    # حالت رشته‌ای (ریلوی) مستقیم وصل می‌شه مگر HARVEST_PROXY ست شده باشه.
    _sess_str = os.environ.get("GENESIS_SESSION_STRING")
    _session = StringSession(_sess_str) if _sess_str else SESSION
    _proxy = (socks.SOCKS5, "127.0.0.1", 10808)
    _hp = os.environ.get("HARVEST_PROXY")
    if _hp == "none" or (_sess_str and not _hp):
        _proxy = None
    elif _hp and ":" in _hp:
        _h, _p = _hp.rsplit(":", 1)
        _proxy = (socks.SOCKS5, _h, int(_p))
    client = TelegramClient(_session, int(ENV["API_ID"]), ENV["API_HASH"],
                            proxy=_proxy)
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED", flush=True)
        return 2
    gen = await client.get_entity(GENESIS)
    own = await client.get_entity(OWNBOT)

    if args.link:
        return await _link_mode(client, gen, own, args.key, args.link)

    got = asyncio.get_event_loop().create_future()
    state = {"list_msg": None}

    def _note(m):
        if m.audio:
            if not got.done():
                got.set_result(m)
        elif m.reply_markup and state["list_msg"] is None:
            state["list_msg"] = m

    @client.on(events.NewMessage(from_users=gen))
    async def h(ev):
        _note(ev.message)

    @client.on(events.MessageEdited(from_users=gen))
    async def h2(ev):
        _note(ev.message)

    clean_q = re.sub(r"[\(\[].*?[\)\]]", "", args.query)
    clean_q = re.sub(r"\s+", " ", clean_q).strip() or args.query
    await client.send_message(gen, clean_q)
    print(f"[hv] query sent: {clean_q!r} key={args.key}", flush=True)

    t0 = time.time()
    while time.time() - t0 < 8 and state["list_msg"] is None and not got.done():
        await asyncio.sleep(0.3)
    if state["list_msg"] is None and not got.done():
        # رویداد ممکنه جا افتاده باشه — از تاریخچه پیداش کن
        try:
            async for m in client.iter_messages(gen, limit=4):
                if not m.out:
                    _note(m)
                    if state["list_msg"] is not None or got.done():
                        break
        except Exception:
            pass
        print(f"[hv] after history check: list={state['list_msg'] is not None}", flush=True)

    if state["list_msg"] is not None and not got.done():
        try:
            btns = await state["list_msg"].get_buttons()
        except Exception:
            btns = None
        if btns:
            qw = [w.lower() for w in clean_q.split() if len(w) > 2]
            pick, best = (0, 0), -1
            for r, row in enumerate(btns):
                for c, b in enumerate(row):
                    tx = (b.text or "").lower()
                    score = sum(1 for w in qw if w in tx)
                    if score > best:
                        best, pick = score, (r, c)
            print(f"[hv] list buttons: {[[b.text for b in row] for row in btns]}", flush=True)
            need = 2 if len(qw) >= 2 else 1
            print(f"[hv] best score={best} need={need}", flush=True)
            if best < need:
                # جنسیس نتیجه مطمئن نداره — حدسی کلیک نکن، بات خودش دانلود می‌کنه
                print("[hv] NO confident match -> hvfail", flush=True)
                try:
                    await client.send_message(own, f"/hvfail {args.key}")
                except Exception:
                    pass
                await client.disconnect()
                print("NO_MATCH", flush=True)
                return 4
            print(f"[hv] clicking {pick}", flush=True)
            try:
                await state["list_msg"].click(pick[0], pick[1])
            except Exception as e:
                print(f"[hv] click failed: {e!r}", flush=True)

    print(f"[hv] list_seen={state['list_msg'] is not None}", flush=True)
    try:
        audio_msg = await asyncio.wait_for(got, timeout=45)
    except asyncio.TimeoutError:
        audio_msg = None
        try:
            async for m in client.iter_messages(gen, limit=4):
                if not m.out and m.audio:
                    audio_msg = m
                    break
        except Exception:
            pass
        if audio_msg is not None:
            await client.send_message(own, f"/hvkey {args.key}")
            await client.forward_messages(own, audio_msg)
            await asyncio.sleep(1)
            await client.disconnect()
            print("[hv] OK (history fallback)", flush=True)
            return 0
        try:
            await client.send_message(own, f"/hvfail {args.key}")
        except Exception:
            pass
        await client.disconnect()
        print("NO_AUDIO", flush=True)
        return 3

    await client.send_message(own, f"/hvkey {args.key}")
    await client.forward_messages(own, audio_msg)
    await asyncio.sleep(1)
    await client.disconnect()
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
