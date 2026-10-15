"""
Ruletka — сервер игры: Telegram-бот, оплата звёздами, баланс круток и админка.

Один процесс:
  • бот (aiogram, long polling): /start с рефералкой, кнопка игры, приём оплаты Stars, /paysupport, /stats
  • HTTP API для мини-приложения (aiohttp) на 127.0.0.1:8080, снаружи его открывает Caddy по HTTPS
  • SQLite-база игроков, круток и платежей

Настройки берутся из файла .env рядом (его создаёт install.sh).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, MenuButtonWebApp,
    Message, PreCheckoutQuery, WebAppInfo,
)

# ======================= настройки =======================

def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

load_env(Path(__file__).with_name(".env"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
GAME_URL = os.environ.get("GAME_URL", "https://gibrazone707-spec.github.io/baraban/")
DB_PATH = os.environ.get("DB_PATH", "/var/lib/ruletka/ruletka.db")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))

START_SPINS = 10      # крутки новому игроку
DAILY_SPINS = 3       # ежедневный бонус
REF_SPINS = 5         # за приглашённого друга
TZ = timezone(timedelta(hours=3))  # «день» для ежедневного бонуса считается по Москве

# Товары магазина. Должны совпадать с PACKS и BOOSTS в index.html
ITEMS = {
    "s10":  {"title": "10 круток",  "stars": 25,  "spins": 10},
    "s50":  {"title": "50 круток",  "stars": 100, "spins": 50},
    "s120": {"title": "120 круток", "stars": 200, "spins": 120},
    "s300": {"title": "300 круток", "stars": 450, "spins": 300},
    "b1":   {"title": "Удача ×2 на 24 часа", "stars": 49,  "boost_days": 1},
    "b7":   {"title": "Удача ×2 на 7 дней",  "stars": 199, "boost_days": 7},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ruletka")

# ======================= правила номеров (как в index.html) =======================

LET = "АВЕКМНОРСТУХ"
SPECIAL = ["АМР", "ЕКХ", "ВОР", "КММ", "ООО"]
REGIONS = (
    "01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 "
    "37 38 39 40 41 42 43 44 45 46 47 48 49 50 51 52 53 54 55 56 57 58 59 60 61 62 63 64 65 66 67 68 69 70 71 72 "
    "73 74 75 76 77 78 79 83 86 87 89 90 93 95 96 97 98 99 102 116 121 123 124 125 126 134 136 138 142 147 150 152 "
    "154 159 161 163 164 173 174 177 178 186 190 193 196 197 198 199 702 716 750 761 763 774 777 790 797 799 977"
).split()
PREMIUM_REG = ["77", "97", "99", "177", "197", "199", "777", "797", "799", "977"]
TIERS = [("common", 62.0), ("uncommon", 25.0), ("rare", 9.5), ("blat", 3.0), ("excl", 0.5)]
TIER_RANK = {t: i for i, (t, _) in enumerate(TIERS)}
TIER_NAME = {"common": "Обычный", "uncommon": "Необычный", "rare": "Редкий", "blat": "Блатной", "excl": "Эксклюзивный"}

_rng = secrets.SystemRandom()
rand = _rng.randrange
pick = _rng.choice


def classify(l: str, d: str) -> str:
    a, b, c = l
    n = int(d)
    same_l = a == b == c
    low = 1 <= n <= 9
    triple = d[0] == d[1] == d[2]
    if same_l and (low or triple):
        return "excl"
    if same_l or l in SPECIAL or low:
        return "blat"
    if triple or d in "0123456789" or d in "9876543210" or n % 100 == 0:
        return "rare"
    if d[0] == d[2] or b == c or a == b:
        return "uncommon"
    return "common"


def _rl():
    return pick(LET)


def _rdigits():
    return f"{1 + rand(999):03d}"


def _candidate(tier: str):
    x = _rl()
    if tier == "uncommon":
        if rand(2):
            a = rand(10)
            b = a
            while b == a:
                b = rand(10)
            return _rl() + _rl() + _rl(), f"{a}{b}{a}"
        return _rl() + x + x, _rdigits()
    if tier == "rare":
        k, s, m = 1 + rand(9), rand(8), rand(3)
        if m == 0:
            d = f"{k}{k}{k}"
        elif m == 1:
            d = ("0123456789" if rand(2) else "9876543210")[s:s + 3]
        else:
            d = f"{k}00"
        return _rl() + _rl() + _rl(), d
    if tier == "blat":
        m = rand(3)
        if m == 0:
            return x * 3, _rdigits()
        if m == 1:
            return pick(SPECIAL), _rdigits()
        return _rl() + _rl() + _rl(), f"00{1 + rand(9)}"
    if tier == "excl":
        k = 1 + rand(9)
        return x * 3, (f"00{k}" if rand(2) else f"{k}{k}{k}")
    return _rl() + _rl() + _rl(), _rdigits()


def odds(boost: bool):
    m = 2 if boost else 1
    o = [0.0] + [p * m for _, p in TIERS[1:]]
    o[0] = 100 - sum(o)
    return o


def roll_tier(boost: bool) -> str:
    o = odds(boost)
    r = rand(1_000_000) / 10_000
    for i in range(len(TIERS) - 1, 0, -1):
        if r < o[i]:
            return TIERS[i][0]
        r -= o[i]
    return "common"


def make_plate(boost: bool, region: str | None):
    tier = roll_tier(boost)
    l, d = _candidate(tier)
    for _ in range(400):
        if classify(l, d) == tier:
            break
        l, d = _candidate(tier)
    t = classify(l, d)
    if region in REGIONS:
        r = region
    elif t in ("excl", "blat") or (t == "rare" and rand(2)):
        r = pick(PREMIUM_REG)
    else:
        r = pick(REGIONS)
    return {"l": l, "d": d, "r": r, "t": t}

# ======================= база данных =======================

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT, first_name TEXT,
  created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  balance INTEGER NOT NULL DEFAULT 0,
  total_spins INTEGER NOT NULL DEFAULT 0,
  stars_spent INTEGER NOT NULL DEFAULT 0,
  ref_by INTEGER, last_daily TEXT, boost_until INTEGER NOT NULL DEFAULT 0,
  banned INTEGER NOT NULL DEFAULT 0, imported INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plates (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
  l TEXT NOT NULL, d TEXT NOT NULL, r TEXT NOT NULL, t TEXT NOT NULL,
  at INTEGER NOT NULL, imported INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS plates_user ON plates(user_id);
CREATE INDEX IF NOT EXISTS plates_tier ON plates(t, at);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, item TEXT NOT NULL,
  stars INTEGER NOT NULL, charge_id TEXT UNIQUE NOT NULL,
  created_at INTEGER NOT NULL, refunded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS payments_user ON payments(user_id);
""")


def now_ms() -> int:
    return int(time.time() * 1000)


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def get_user(uid: int):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def ensure_user(uid: int, username: str | None, first_name: str | None, ref_by: int | None = None):
    """Создаёт игрока при первом входе. Возвращает (строка игрока, создан_ли_сейчас)."""
    u = get_user(uid)
    t = now_ms()
    if u:
        db.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE id=?", (username, first_name, t, uid))
        return get_user(uid), False
    if ref_by == uid or (ref_by and not get_user(ref_by)):
        ref_by = None
    db.execute(
        "INSERT INTO users (id, username, first_name, created_at, last_seen, balance, ref_by) VALUES (?,?,?,?,?,?,?)",
        (uid, username, first_name, t, t, START_SPINS, ref_by),
    )
    if ref_by:
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (REF_SPINS, ref_by))
    return get_user(uid), True


def profile(u) -> dict:
    invited = db.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (u["id"],)).fetchone()[0]
    return {
        "balance": u["balance"], "total": u["total_spins"], "boost_until": u["boost_until"],
        "daily_available": u["last_daily"] != today(), "invited": invited,
        "is_admin": u["id"] in ADMIN_IDS, "banned": bool(u["banned"]), "imported": bool(u["imported"]),
        "start_spins": START_SPINS, "daily_spins": DAILY_SPINS, "ref_spins": REF_SPINS,
    }


def apply_item(uid: int, item_id: str, sign: int = 1):
    item = ITEMS[item_id]
    if "spins" in item:
        db.execute("UPDATE users SET balance=MAX(0, balance+?) WHERE id=?", (sign * item["spins"], uid))
    else:
        ms = item["boost_days"] * 86_400_000
        if sign > 0:
            db.execute("UPDATE users SET boost_until=MAX(boost_until, ?)+? WHERE id=?", (now_ms(), ms, uid))
        else:
            db.execute("UPDATE users SET boost_until=MAX(0, boost_until-?) WHERE id=?", (ms, uid))

# ======================= бот =======================

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if BOT_TOKEN else None
dp = Dispatcher()


def play_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎰 Крутить барабан", web_app=WebAppInfo(url=GAME_URL))]])


def esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    ref = None
    if command.args and command.args.startswith("ref") and command.args[3:].isdigit():
        ref = int(command.args[3:])
    u, created = ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name, ref)
    if created and u["ref_by"]:
        try:
            await bot.send_message(u["ref_by"], f"🎉 По вашей ссылке пришёл новый игрок. Вам начислено {REF_SPINS} круток!")
        except Exception:
            pass
    await m.answer(
        "<b>Ruletka</b> — барабан российских автономеров.\n\n"
        "Крути, собирай коллекцию и лови редкие комбинации: зеркальные, с тремя семёрками, блатные.\n"
        f"На старте у тебя {u['balance']} круток, каждый день ещё +{DAILY_SPINS} бесплатно.",
        reply_markup=play_kb(),
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(m: Message, command: CommandObject):
    if not command.args:
        await m.answer(
            "Если с оплатой что-то пошло не так, напишите одним сообщением:\n"
            "<code>/paysupport</code> и после пробела опишите проблему.\n\n"
            "Администратор ответит вам в этом чате."
        )
        return
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                aid,
                f"🆘 Обращение по оплате от <code>{m.from_user.id}</code> "
                f"(@{esc(m.from_user.username) or '—'}):\n\n{esc(command.args)}",
            )
        except Exception:
            pass
    await m.answer("Сообщение передано администратору. Ответим здесь в ближайшее время.")


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    s = stats_summary()
    await m.answer(
        f"👥 Игроков: <b>{s['users']}</b> (+{s['users_today']} сегодня)\n"
        f"🎮 Играли сегодня: <b>{s['active_today']}</b>, за 7 дней: <b>{s['active_7d']}</b>\n"
        f"🎰 Круток всего: <b>{s['spins']}</b>, сегодня: <b>{s['spins_today']}</b>\n"
        f"⭐ Звёзд получено: <b>{s['stars']}</b> (покупок: {s['payments']}), сегодня: <b>{s['stars_today']}</b>\n\n"
        "Подробная статистика во вкладке «Админ» в игре.",
        reply_markup=play_kb(),
    )


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try:
        item_id, uid = q.invoice_payload.split(":")
        item = ITEMS[item_id]
        ok = q.currency == "XTR" and q.total_amount == item["stars"] and int(uid) == q.from_user.id
    except Exception:
        ok = False
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Не удалось оформить покупку. Откройте магазин заново и попробуйте ещё раз.")


@dp.message(F.successful_payment)
async def on_paid(m: Message):
    sp = m.successful_payment
    item_id, uid = sp.invoice_payload.split(":")
    uid = int(uid)
    if item_id not in ITEMS:
        log.error("Оплата неизвестного товара %s от %s", item_id, uid)
        return
    ensure_user(uid, m.from_user.username, m.from_user.first_name)
    try:
        db.execute(
            "INSERT INTO payments (user_id, item, stars, charge_id, created_at) VALUES (?,?,?,?,?)",
            (uid, item_id, sp.total_amount, sp.telegram_payment_charge_id, now_ms()),
        )
    except sqlite3.IntegrityError:
        return  # этот платёж уже учтён
    apply_item(uid, item_id)
    db.execute("UPDATE users SET stars_spent=stars_spent+? WHERE id=?", (sp.total_amount, uid))
    await m.answer(f"✅ Оплата прошла: {ITEMS[item_id]['title']}. Возвращайтесь в игру!", reply_markup=play_kb())
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f"⭐ +{sp.total_amount} звёзд: {ITEMS[item_id]['title']} от "
                                        f"<code>{uid}</code> (@{esc(m.from_user.username) or '—'})")
        except Exception:
            pass

# ======================= API для мини-приложения =======================

def check_init_data(init_data: str):
    """Проверяет подпись initData от Telegram. Возвращает данные пользователя или None."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        recv = data.pop("hash", "")
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not recv or not hmac.compare_digest(calc, recv):
            return None
        if time.time() - int(data.get("auth_date", "0")) > 7 * 86400:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


def j(data, status=200):
    return web.json_response(data, status=status)


@web.middleware
async def mw(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            if request.path.startswith("/api/"):
                auth = request.headers.get("Authorization", "")
                tu = check_init_data(auth[4:]) if auth.startswith("tma ") else None
                if not tu:
                    resp = j({"error": "unauthorized"}, 401)
                else:
                    u, _ = ensure_user(int(tu["id"]), tu.get("username"), tu.get("first_name"))
                    if u["banned"] and request.path != "/api/me":
                        resp = j({"error": "banned"}, 403)
                    elif request.path.startswith("/api/admin/") and u["id"] not in ADMIN_IDS:
                        resp = j({"error": "forbidden"}, 403)
                    else:
                        request["user"] = u
                        resp = await handler(request)
            else:
                resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        except Exception:
            log.exception("API error")
            resp = j({"error": "server"}, 500)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def body(request):
    try:
        return await request.json()
    except Exception:
        return {}


async def api_me(request):
    return j(profile(get_user(request["user"]["id"])))


async def api_spin(request):
    b = await body(request)
    n = 10 if b.get("n") == 10 else 1
    region = b.get("region") if b.get("region") in REGIONS else None
    uid = request["user"]["id"]
    u = get_user(uid)
    if u["balance"] < n:
        return j({"error": "no_spins", "balance": u["balance"]}, 402)
    boost = u["boost_until"] > now_ms()
    t = now_ms()
    plates = [make_plate(boost, region) for _ in range(n)]
    db.execute("BEGIN")
    db.execute("UPDATE users SET balance=balance-?, total_spins=total_spins+?, last_seen=? WHERE id=?", (n, n, t, uid))
    db.executemany(
        "INSERT INTO plates (user_id, l, d, r, t, at) VALUES (?,?,?,?,?,?)",
        [(uid, p["l"], p["d"], p["r"], p["t"], t) for p in plates],
    )
    db.execute("COMMIT")
    for p in plates:
        p["l"] = list(p["l"])
        p["at"] = t
    return j({"plates": plates, **profile(get_user(uid))})


async def api_daily(request):
    uid = request["user"]["id"]
    cur = db.execute("UPDATE users SET balance=balance+?, last_daily=? WHERE id=? AND (last_daily IS NULL OR last_daily<>?)",
                     (DAILY_SPINS, today(), uid, today()))
    if cur.rowcount == 0:
        return j({"error": "already", **profile(get_user(uid))}, 409)
    return j({"bonus": DAILY_SPINS, **profile(get_user(uid))})


async def api_invoice(request):
    b = await body(request)
    item_id = b.get("item")
    if item_id not in ITEMS:
        return j({"error": "item"}, 400)
    item = ITEMS[item_id]
    uid = request["user"]["id"]
    desc = (f"{item['spins']} круток для Ruletka. Не сгорают." if "spins" in item
            else "Шанс редких номеров в два раза выше на время действия.")
    link = await bot.create_invoice_link(
        title=item["title"], description=desc, payload=f"{item_id}:{uid}",
        currency="XTR", prices=[LabeledPrice(label=item["title"], amount=item["stars"])],
    )
    return j({"invoice_url": link})


def _valid_plate(p):
    try:
        l, d, r = "".join(p["l"]), str(p["d"]), str(p["r"])
        return len(l) == 3 and all(c in LET for c in l) and len(d) == 3 and d.isdigit() and d != "000" and r in REGIONS
    except Exception:
        return False


async def api_import(request):
    """Один раз переносит коллекцию игрока из телефона на сервер (для статистики)."""
    uid = request["user"]["id"]
    if request["user"]["imported"]:
        return j({"imported": 0})
    b = await body(request)
    items = [p for p in (b.get("plates") or [])[:1000] if isinstance(p, dict) and _valid_plate(p)]
    rows = []
    for p in items:
        l, d = "".join(p["l"]), str(p["d"])
        at = int(p.get("at") or 0)
        rows.append((uid, l, d, str(p["r"]), classify(l, d), at if 0 < at <= now_ms() else now_ms()))
    db.execute("BEGIN")
    db.executemany("INSERT INTO plates (user_id, l, d, r, t, at, imported) VALUES (?,?,?,?,?,?,1)", rows)
    db.execute("UPDATE users SET imported=1 WHERE id=?", (uid,))
    db.execute("COMMIT")
    return j({"imported": len(rows)})

# ======================= админка =======================

def day_start_ms(days_ago=0) -> int:
    d = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return int(d.timestamp() * 1000)


def stats_summary():
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0] or 0
    t0, w0, m0 = day_start_ms(0), day_start_ms(6), day_start_ms(29)
    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "users_today": q("SELECT COUNT(*) FROM users WHERE created_at>=?", t0),
        "users_7d": q("SELECT COUNT(*) FROM users WHERE created_at>=?", w0),
        "active_today": q("SELECT COUNT(*) FROM users WHERE last_seen>=?", t0),
        "active_7d": q("SELECT COUNT(*) FROM users WHERE last_seen>=?", w0),
        "payers": q("SELECT COUNT(DISTINCT user_id) FROM payments WHERE refunded=0"),
        "spins": q("SELECT COALESCE(SUM(total_spins),0) FROM users"),
        "spins_today": q("SELECT COUNT(*) FROM plates WHERE imported=0 AND at>=?", t0),
        "stars": q("SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0"),
        "stars_today": q("SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0 AND created_at>=?", t0),
        "stars_30d": q("SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0 AND created_at>=?", m0),
        "payments": q("SELECT COUNT(*) FROM payments WHERE refunded=0"),
        "refunds": q("SELECT COUNT(*) FROM payments WHERE refunded=1"),
    }


async def admin_stats(request):
    s = stats_summary()
    days = []
    for i in range(13, -1, -1):
        a, b = day_start_ms(i), day_start_ms(i - 1) if i else day_start_ms(0) + 86_400_000
        days.append({
            "day": datetime.fromtimestamp(a / 1000, TZ).strftime("%d.%m"),
            "users": db.execute("SELECT COUNT(*) FROM users WHERE created_at>=? AND created_at<?", (a, b)).fetchone()[0],
            "stars": db.execute("SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0 AND created_at>=? AND created_at<?", (a, b)).fetchone()[0],
            "spins": db.execute("SELECT COUNT(*) FROM plates WHERE imported=0 AND at>=? AND at<?", (a, b)).fetchone()[0],
        })
    tiers = {t: 0 for t, _ in TIERS}
    for row in db.execute("SELECT t, COUNT(*) c FROM plates WHERE imported=0 GROUP BY t"):
        tiers[row["t"]] = row["c"]
    rare = [dict(r) for r in db.execute(
        "SELECT p.l, p.d, p.r, p.t, p.at, p.user_id, u.username, u.first_name FROM plates p "
        "LEFT JOIN users u ON u.id=p.user_id WHERE p.t IN ('blat','excl') AND p.imported=0 ORDER BY p.id DESC LIMIT 15")]
    items = [dict(r) for r in db.execute(
        "SELECT item, COUNT(*) n, SUM(stars) stars FROM payments WHERE refunded=0 GROUP BY item ORDER BY stars DESC")]
    for it in items:
        it["title"] = ITEMS.get(it["item"], {}).get("title", it["item"])
    return j({**s, "days": days, "tiers": tiers, "rare": rare, "items": items})


USER_COLS = "id, username, first_name, created_at, last_seen, balance, total_spins, stars_spent, ref_by, banned, boost_until"


async def admin_users(request):
    qs = request.query.get("q", "").strip().lstrip("@")
    sort = {"new": "created_at", "paid": "stars_spent", "spins": "total_spins", "seen": "last_seen"}.get(request.query.get("sort"), "created_at")
    sql = f"SELECT {USER_COLS}, (SELECT COUNT(*) FROM users r WHERE r.ref_by=users.id) invited FROM users"
    args = []
    if qs:
        sql += " WHERE CAST(id AS TEXT) LIKE ? OR username LIKE ? OR first_name LIKE ?"
        args = [f"%{qs}%"] * 3
    sql += f" ORDER BY {sort} DESC LIMIT 100"
    return j({"users": [dict(r) for r in db.execute(sql, args)]})


async def admin_user(request):
    try:
        uid = int(request.query.get("id", "0"))
    except ValueError:
        return j({"error": "id"}, 400)
    u = db.execute(f"SELECT {USER_COLS} FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        return j({"error": "not_found"}, 404)
    tiers = {t: 0 for t, _ in TIERS}
    for row in db.execute("SELECT t, COUNT(*) c FROM plates WHERE user_id=? GROUP BY t", (uid,)):
        tiers[row["t"]] = row["c"]
    best = db.execute(
        "SELECT l, d, r, t, at FROM plates WHERE user_id=? ORDER BY CASE t WHEN 'excl' THEN 4 WHEN 'blat' THEN 3 "
        "WHEN 'rare' THEN 2 WHEN 'uncommon' THEN 1 ELSE 0 END DESC, at DESC LIMIT 1", (uid,)).fetchone()
    pays = [dict(r) for r in db.execute("SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,))]
    for p in pays:
        p["title"] = ITEMS.get(p["item"], {}).get("title", p["item"])
    invited = [dict(r) for r in db.execute(
        "SELECT id, username, first_name, created_at FROM users WHERE ref_by=? ORDER BY created_at DESC LIMIT 50", (uid,))]
    return j({"user": dict(u), "tiers": tiers, "best": dict(best) if best else None, "payments": pays, "invited": invited})


async def admin_grant(request):
    b = await body(request)
    try:
        uid, n = int(b["id"]), int(b["spins"])
    except Exception:
        return j({"error": "params"}, 400)
    if not get_user(uid) or abs(n) > 100_000:
        return j({"error": "params"}, 400)
    db.execute("UPDATE users SET balance=MAX(0, balance+?) WHERE id=?", (n, uid))
    if n > 0 and b.get("notify"):
        try:
            await bot.send_message(uid, f"🎁 Вам начислено {n} круток!", reply_markup=play_kb())
        except Exception:
            pass
    return j({"balance": get_user(uid)["balance"]})


async def admin_ban(request):
    b = await body(request)
    try:
        uid = int(b["id"])
    except Exception:
        return j({"error": "params"}, 400)
    if uid in ADMIN_IDS:
        return j({"error": "self"}, 400)
    db.execute("UPDATE users SET banned=? WHERE id=?", (1 if b.get("ban") else 0, uid))
    return j({"banned": bool(b.get("ban"))})


async def admin_payments(request):
    rows = [dict(r) for r in db.execute(
        "SELECT p.*, u.username, u.first_name FROM payments p LEFT JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 100")]
    for p in rows:
        p["title"] = ITEMS.get(p["item"], {}).get("title", p["item"])
    return j({"payments": rows})


async def admin_refund(request):
    b = await body(request)
    p = db.execute("SELECT * FROM payments WHERE id=?", (b.get("payment_id"),)).fetchone()
    if not p:
        return j({"error": "not_found"}, 404)
    if p["refunded"]:
        return j({"error": "already"}, 409)
    try:
        await bot.refund_star_payment(user_id=p["user_id"], telegram_payment_charge_id=p["charge_id"])
    except TelegramBadRequest as e:
        return j({"error": "telegram", "detail": str(e)}, 400)
    db.execute("UPDATE payments SET refunded=1 WHERE id=?", (p["id"],))
    db.execute("UPDATE users SET stars_spent=MAX(0, stars_spent-?) WHERE id=?", (p["stars"], p["user_id"]))
    if p["item"] in ITEMS:
        apply_item(p["user_id"], p["item"], sign=-1)
    return j({"ok": True})


broadcast_state = {"running": False, "sent": 0, "failed": 0, "total": 0}


async def _broadcast(text: str):
    ids = [r[0] for r in db.execute("SELECT id FROM users WHERE banned=0")]
    broadcast_state.update(running=True, sent=0, failed=0, total=len(ids))
    for uid in ids:
        try:
            await bot.send_message(uid, text, reply_markup=play_kb())
            broadcast_state["sent"] += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(uid, text, reply_markup=play_kb())
                broadcast_state["sent"] += 1
            except Exception:
                broadcast_state["failed"] += 1
        except (TelegramForbiddenError, TelegramBadRequest, Exception):
            broadcast_state["failed"] += 1
        await asyncio.sleep(0.05)
    broadcast_state["running"] = False


async def admin_broadcast(request):
    b = await body(request)
    text = str(b.get("text") or "").strip()
    if not text:
        return j(broadcast_state)
    if broadcast_state["running"]:
        return j({"error": "running", **broadcast_state}, 409)
    if len(text) > 3500:
        return j({"error": "long"}, 400)
    asyncio.create_task(_broadcast(esc(text)))
    await asyncio.sleep(0)
    return j(broadcast_state)


async def health(request):
    return j({"ok": True})


def make_app():
    app = web.Application(middlewares=[mw])
    r = app.router
    r.add_get("/", health)
    r.add_post("/api/me", api_me)
    r.add_post("/api/spin", api_spin)
    r.add_post("/api/daily", api_daily)
    r.add_post("/api/invoice", api_invoice)
    r.add_post("/api/import", api_import)
    r.add_get("/api/admin/stats", admin_stats)
    r.add_get("/api/admin/users", admin_users)
    r.add_get("/api/admin/user", admin_user)
    r.add_post("/api/admin/grant", admin_grant)
    r.add_post("/api/admin/ban", admin_ban)
    r.add_get("/api/admin/payments", admin_payments)
    r.add_post("/api/admin/refund", admin_refund)
    r.add_post("/api/admin/broadcast", admin_broadcast)
    r.add_route("OPTIONS", "/{tail:.*}", health)
    return app

# ======================= запуск =======================

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN в файле .env")
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    log.info("API слушает %s:%s", HOST, PORT)
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=GAME_URL)))
    except Exception as e:
        log.warning("Не удалось поставить кнопку меню: %s", e)
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.get_me()
    log.info("Бот @%s запущен", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(main())
