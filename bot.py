#!/usr/bin/env python3
# bot.py — single-file IVASMS OTP bot (Termux compatible)
# Features:
# - Uses admin IVASMS account (email/password in .env) and cookies.json (auto-created/refreshed)
# - Syncs numbers from IVASMS (portal/live/my_sms)
# - Assigns numbers to users on /get_number
# - Polls IVASMS for OTPs and credits the owner (earn per OTP)
# - Withdrawal request flow (user -> admin), admin approves with /approve <wid>
# - SQLite local DB (data.db) auto-created
# - Background tasks: OTP poll and cookie refresh
# - All configurable via .env

import os
import re
import json
import time
import asyncio
import sqlite3
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# --------------------------- Load env ---------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GROUP_ID = int(os.getenv("GROUP_ID", "0"))  # optional notification group
IVASMS_EMAIL = os.getenv("IVASMS_EMAIL")
IVASMS_PASSWORD = os.getenv("IVASMS_PASSWORD")

EARN_PER_SMS = float(os.getenv("EARN_PER_SMS", "1.0"))
MIN_WITHDRAWAL = float(os.getenv("MIN_WITHDRAWAL", "250.0"))

OTP_POLL_INTERVAL = int(os.getenv("OTP_POLL_INTERVAL", "30"))        # seconds
COOKIE_REFRESH_INTERVAL = int(os.getenv("COOKIE_REFRESH_INTERVAL", "86400"))  # seconds (24h)

BASE = "https://www.ivasms.com"
MY_SMS_URL = f"{BASE}/portal/live/my_sms"
LOGIN_URL = f"{BASE}/login"
COOKIES_FILE = "cookies.json"
DB_FILE = "data.db"

if not BOT_TOKEN or ADMIN_ID == 0 or not IVASMS_EMAIL or not IVASMS_PASSWORD:
    print("Please set BOT_TOKEN, ADMIN_ID, IVASMS_EMAIL, IVASMS_PASSWORD in .env")
    raise SystemExit(1)

# --------------------------- Database ---------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS earnings (
        user_id INTEGER PRIMARY KEY,
        balance REAL DEFAULT 0
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS available_numbers (
        number TEXT PRIMARY KEY,
        country TEXT,
        assigned_to INTEGER DEFAULT NULL,
        added_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS otps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT,
        otp TEXT,
        full_msg TEXT,
        service TEXT,
        country TEXT,
        fetched_at TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        method TEXT,
        target TEXT,
        status TEXT DEFAULT 'pending',
        requested_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

def get_conn():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

# DB helpers
def ensure_user(user_id: int, username: str | None):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    c.execute("INSERT OR IGNORE INTO earnings (user_id, balance) VALUES (?, ?)", (user_id, 0.0))
    conn.commit(); conn.close()

def get_balance(user_id: int) -> float:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT balance FROM earnings WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close()
    return float(row[0]) if row else 0.0

def credit_user(user_id: int, amount: float):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO earnings (user_id, balance) VALUES (?, ?)", (user_id, 0.0))
    c.execute("UPDATE earnings SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    conn.commit(); conn.close()

def debit_user(user_id: int, amount: float) -> bool:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT balance FROM earnings WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row or row[0] < amount:
        conn.close(); return False
    c.execute("UPDATE earnings SET balance = balance - ? WHERE user_id=?", (amount, user_id))
    conn.commit(); conn.close(); return True

def add_available_number(number: str, country: str = "UNKNOWN"):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO available_numbers (number, country) VALUES (?, ?)", (number, country))
    conn.commit(); conn.close()

def assign_number_to_user(user_id: int) -> str | None:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT number FROM available_numbers WHERE assigned_to IS NULL LIMIT 1")
    row = c.fetchone()
    if not row:
        conn.close(); return None
    number = row[0]
    c.execute("UPDATE available_numbers SET assigned_to = ? WHERE number=?", (user_id, number))
    conn.commit(); conn.close(); return number

def get_user_by_number(number: str) -> int | None:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT assigned_to FROM available_numbers WHERE number=?", (number,))
    row = c.fetchone(); conn.close()
    return row[0] if row else None

def otp_exists(number: str, otp: str) -> bool:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT 1 FROM otps WHERE number=? AND otp=?", (number, otp))
    exists = c.fetchone() is not None; conn.close(); return exists

def save_otp(number: str, otp: str, full_msg: str, service: str, country: str):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO otps (number, otp, full_msg, service, country, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
              (number, otp, full_msg, service, country, datetime.now(timezone.utc).isoformat()))
    conn.commit(); conn.close()

def create_withdrawal(user_id: int, amount: float, method: str, target: str) -> int:
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO withdrawals (user_id, amount, method, target) VALUES (?, ?, ?, ?)",
              (user_id, amount, method, target))
    wid = c.lastrowid; conn.commit(); conn.close(); return wid

def list_pending_withdrawals():
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT id, user_id, amount, method, target FROM withdrawals WHERE status='pending'")
    rows = c.fetchall(); conn.close(); return rows

def approve_withdrawal(wid: int) -> bool:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT user_id, amount FROM withdrawals WHERE id=? AND status='pending'", (wid,))
    row = c.fetchone()
    if not row:
        conn.close(); return False
    user_id, amount = row
    # check balance and deduct
    c.execute("SELECT balance FROM earnings WHERE user_id=?", (user_id,))
    bal_row = c.fetchone()
    if not bal_row or bal_row[0] < amount:
        conn.close(); return False
    c.execute("UPDATE earnings SET balance = balance - ? WHERE user_id=?", (amount, user_id))
    c.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wid,))
    conn.commit(); conn.close(); return True

# --------------------------- Cookies & Login ---------------------------
def save_cookies_from_scraper(scraper):
    cookie_list = []
    for c in scraper.cookies:
        cookie_list.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "expires": c.expires,
            "secure": c.secure,
            "httpOnly": c._rest.get("HttpOnly", False) if hasattr(c, "_rest") else False
        })
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookie_list, f, indent=2)

def load_scraper_from_cookies():
    if not os.path.exists(COOKIES_FILE):
        return None
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookie_list = json.load(f)
    except Exception:
        return None
    s = cloudscraper.create_scraper()
    for c in cookie_list:
        try:
            s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        except Exception:
            try:
                s.cookies.set(c["name"], c["value"])
            except:
                pass
    return s

def login_and_get_scraper() -> cloudscraper.CloudScraper:
    # try cookies first
    s = load_scraper_from_cookies()
    if s:
        try:
            r = s.get(MY_SMS_URL, timeout=15)
            if r.status_code == 200 and ("otp" in r.text.lower() or "sms" in r.text.lower() or "my_sms" in r.url or "portal" in r.url):
                return s
        except Exception:
            pass
    # fallback: login by email+password
    s = cloudscraper.create_scraper()
    try:
        r = s.get(LOGIN_URL, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        token_input = soup.find("input", {"name": "_token"})
        csrf = token_input.get("value") if token_input else ""
        data = {"_token": csrf, "email": IVASMS_EMAIL, "password": IVASMS_PASSWORD}
        login_resp = s.post(LOGIN_URL, data=data, timeout=20, allow_redirects=True)
        r2 = s.get(MY_SMS_URL, timeout=15)
        if r2.status_code == 200 and ("otp" in r2.text.lower() or "sms" in r2.text.lower() or "my_sms" in r2.url or "portal" in r2.url):
            save_cookies_from_scraper(s)
            return s
        else:
            # still return scraper (may not be authenticated)
            return s
    except Exception as e:
        print("Login error:", e)
        return s

# --------------------------- Sync numbers ---------------------------
def sync_numbers_from_ivasms() -> int:
    s = login_and_get_scraper()
    if not s:
        return 0
    try:
        r = s.get(MY_SMS_URL, timeout=20)
        text = r.text
        found = re.findall(r"(?:\+?\d{6,15})", text)
        added = 0
        for num in set(found):
            add_available_number(num, "UNKNOWN")
            added += 1
        return added
    except Exception as e:
        print("sync_numbers error:", e)
        return 0

# --------------------------- OTP detection ---------------------------
OTP_RE = re.compile(r"\b(\d{4,8})\b")

def detect_service(text: str) -> str:
    t = (text or "").lower()
    services = ["whatsapp","facebook","telegram","google","instagram","tiktok","netflix","clerk"]
    for s in services:
        if s in t:
            return s.capitalize()
    return "Service"

async def process_incoming_otps_single_scraper(scraper):
    try:
        r = scraper.get(MY_SMS_URL, timeout=20)
        if r.status_code != 200:
            return []
        text = r.text
        results = []
        # strategy: for each occurrence of a number, look ahead/back for OTP within a window
        for m in re.finditer(r"(?:\+?\d{6,15})", text):
            number = m.group(0)
            start = m.end()
            window = text[start:start+400]
            otp_m = OTP_RE.search(window)
            if not otp_m:
                back_window = text[max(0, m.start()-200):m.start()]
                otp_m = OTP_RE.search(back_window)
            if not otp_m:
                continue
            otp = otp_m.group(1)
            if otp_exists(number, otp):
                continue
            # find small snippet of message
            snippet = (window[:300] or back_window[-300:]) if window or back_window else ""
            svc = detect_service(snippet)
            country = "UNKNOWN"
            save_otp(number, otp, snippet, svc, country)
            results.append((number, otp, snippet, svc, country))
        return results
    except Exception as e:
        print("process_incoming_otps_single_scraper error:", e)
        return []

# --------------------------- Bot & Handlers ---------------------------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# simple per-user state for withdraw input
user_states = {}

@dp.message(F.text == "/start")
async def cmd_start(m: types.Message):
    ensure_user(m.from_user.id, m.from_user.username)
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("🎁 Get Number", callback_data="get_number")],
        [types.InlineKeyboardButton("👤 Account", callback_data="account")],
        [types.InlineKeyboardButton("💰 Withdraw", callback_data="withdraw")]
    ])
    await m.answer(f"👋 Welcome! Earn ৳{EARN_PER_SMS:.2f} per OTP. Click below:", reply_markup=kb)

@dp.callback_query(F.data == "get_number")
async def cb_get_number(q: types.CallbackQuery):
    ensure_user(q.from_user.id, q.from_user.username)
    num = assign_number_to_user(q.from_user.id)
    if not num:
        await q.message.edit_text("❌ No numbers available. Admin please run /sync to fetch from IVASMS.")
        return
    await q.message.edit_text(
        f"✅ Number Assigned Successfully!\n\n"
        f"📞 <code>{num}</code>\n"
        f"🌍 Country: UNKNOWN\n"
        f"📊 Total Numbers: 1/10\n\n"
        f"📨 OTP will be sent to the group and your inbox.",
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "account")
async def cb_account(q: types.CallbackQuery):
    ensure_user(q.from_user.id, q.from_user.username)
    bal = get_balance(q.from_user.id)
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT number FROM available_numbers WHERE assigned_to=?", (q.from_user.id,))
    rows = c.fetchall(); conn.close()
    numbers = [r[0] for r in rows]
    nums = "\n".join(f"• <code>{n}</code>" for n in numbers) if numbers else "None"
    await q.message.edit_text(f"👤 Your Account\n\n💰 Balance: ৳{bal:.2f}\n📱 Numbers:\n{nums}", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "withdraw")
async def cb_withdraw(q: types.CallbackQuery):
    ensure_user(q.from_user.id, q.from_user.username)
    bal = get_balance(q.from_user.id)
    if bal < MIN_WITHDRAWAL:
        await q.message.edit_text(f"❌ Minimum withdrawal is ৳{MIN_WITHDRAWAL:.2f}. Your balance: ৳{bal:.2f}")
        return
    await q.message.edit_text("📲 Send withdrawal info in the format:\n`method,number,amount`\nExample:\n`bkash,017XXXXXXXX,500`")
    user_states[q.from_user.id] = "awaiting_withdraw"

@dp.message(F.text.regexp(r"^(bkash|nagad|rocket|bank),\s*\d+,\s*\d+(\.\d+)?$", flags=re.I))
async def handle_withdraw_text(m: types.Message):
    state = user_states.get(m.from_user.id)
    if state != "awaiting_withdraw":
        return
    try:
        method, number, amount = [p.strip() for p in m.text.split(",")]
        amount = float(amount)
        bal = get_balance(m.from_user.id)
        if amount > bal:
            await m.reply("🚫 Insufficient balance.")
            user_states.pop(m.from_user.id, None)
            return
        wid = create_withdrawal(m.from_user.id, amount, method.lower(), number)
        await m.reply(f"✅ Withdrawal request #{wid} created for ৳{amount}. Admin will review.")
        try:
            await bot.send_message(ADMIN_ID,
                f"📥 New withdrawal request #{wid}\nUser: {m.from_user.id}\nMethod: {method}\nNumber: {number}\nAmount: ৳{amount}\nApprove with: /approve {wid}")
        except Exception:
            pass
    except Exception as e:
        await m.reply("❌ Could not parse the withdrawal. Use: method,number,amount")
    user_states.pop(m.from_user.id, None)

@dp.message(F.text == "/sync")
async def cmd_sync(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    added = sync_numbers_from_ivasms()
    await m.reply(f"✅ Synced {added} numbers from IVASMS.")

@dp.message(F.text == "/withdrawals")
async def cmd_withdrawals(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    rows = list_pending_withdrawals()
    if not rows:
        return await m.reply("✅ No pending withdrawals.")
    text = "📋 Pending withdrawals:\n\n"
    for wid, uid, amount, method, target in rows:
        text += f"ID:{wid} | User:{uid} | ৳{amount} | {method} {target}\n"
    await m.reply(text)

@dp.message(F.text.regexp(r"^/approve\s+\d+$"))
async def cmd_approve(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    wid = int(m.text.split()[1])
    ok = approve_withdrawal(wid)
    if ok:
        await m.reply(f"✅ Withdrawal #{wid} approved and balance deducted.")
        # notify user
        conn = get_conn(); c = conn.cursor()
        c.execute("SELECT user_id, amount FROM withdrawals WHERE id=?", (wid,))
        row = c.fetchone(); conn.close()
        if row:
            uid, amt = row
            try:
                await bot.send_message(uid, f"✅ Your withdrawal #{wid} for ৳{amt} was approved by admin.")
            except Exception:
                pass
    else:
        await m.reply("❌ Approval failed (invalid id or insufficient funds).")

@dp.message(F.text == "/stats")
async def cmd_stats(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM available_numbers"); total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM available_numbers WHERE assigned_to IS NULL"); free = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users"); users = c.fetchone()[0]
    conn.close()
    await m.reply(f"📊 Stats\nTotal numbers: {total}\nFree: {free}\nUsers: {users}")

# --------------------------- Background loops ---------------------------
async def otp_poll_loop():
    while True:
        try:
            scraper = login_and_get_scraper()
            if not scraper:
                await asyncio.sleep(OTP_POLL_INTERVAL); continue
            results = await process_incoming_otps_single_scraper(scraper)
            for number, otp, snippet, svc, country in results:
                msg = (
                    f"📱 <b>New OTP!</b>\n\n"
                    f"📞 <b>Number:</b> {number}\n"
                    f"🌍 <b>Country:</b> {country}\n"
                    f"🆔 <b>Provider:</b> {svc}\n"
                    f"🔑 <b>OTP Code:</b> <code>{otp}</code>\n"
                    f"📝 <b>Snippet:</b> {snippet[:300]}\n\n"
                    f"🎉 You have earned ৳{EARN_PER_SMS:.2f}!"
                )
                # send to group
                try:
                    if GROUP_ID:
                        await bot.send_message(GROUP_ID, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                except Exception:
                    pass
                # credit and inform owner
                owner = get_user_by_number(number)
                if owner:
                    credit_user(owner, EARN_PER_SMS)
                    try:
                        await bot.send_message(owner, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                    except Exception:
                        pass
                # notify admin
                try:
                    await bot.send_message(ADMIN_ID, f"📢 New OTP for {number}: {otp}")
                except Exception:
                    pass
        except Exception as e:
            print("otp_poll_loop error:", e)
        await asyncio.sleep(OTP_POLL_INTERVAL)

async def cookie_refresh_loop():
    while True:
        try:
            s = login_and_get_scraper()
            if s:
                save_cookies_from_scraper(s)
                print("[cookie_refresh] refreshed cookies at", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            print("cookie_refresh_loop error:", e)
        await asyncio.sleep(COOKIE_REFRESH_INTERVAL)

# --------------------------- Startup ---------------------------
async def on_startup():
    init_db()
    # ensure admin exists
    ensure_user(ADMIN_ID, "admin")
    # initial sync
    try:
        added = sync_numbers_from_ivasms()
        print(f"Initial sync added {added} numbers.")
    except Exception as e:
        print("Initial sync failed:", e)
    # start background tasks
    asyncio.create_task(otp_poll_loop())
    asyncio.create_task(cookie_refresh_loop())

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    dp.startup.register(on_startup)
    print("Bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")
