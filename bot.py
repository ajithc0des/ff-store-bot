"""Captain X Store Bot — Telegram Free Fire ID store with FamGateway UPI auto-payment.

v5.0 — upgrade of bot_premium-4.py (every v4 feature is preserved):

  * Admin panel buttons (Delete Post / Pending Orders / Force Join / Manage Users) now always get a
    valid premium (animated when available) icon; owner can override with /seticon.
  * Persistent storage (posts/sold/orders no longer vanish on restart), atomic writes, encrypted creds.
  * Catalogue browsing with photos, one card at a time, edit-in-place navigation.
  * Checkout with promo codes, UPI or wallet balance, 60-second progress indicator.
  * Wallet / credit ledger (idempotent, double-spend safe), invoices / receipts.
  * Broadcast + 3-hour auto promotion to groups/channels (registry, permission checks, flood-safe).
  * Seller flow (users submit 5-20 images + login details, admin quotes a price, user accepts, admin lists).
  * Owner inventory channel, backups to a private group every 2 hours (+ restore), startup recovery,
    verified owner-controlled updates with rollback.

Environment variables
  BOT_TOKEN, ADMIN_ID, FG_API_KEY            required
  DATA_DIR                                   persistent folder (default: next to this file)
  DATA_KEY                                   Fernet key protecting stored credentials (recommended)
  SUPER_ADMINS, FORCE_JOIN                   optional, as before
  SUPPORT_LINK, BOT_LINK                     optional (shown on receipts / promo button)
  MIN_TOPUP (default 5), REFERRAL_BONUS (default 0)
  UPDATE_MANIFEST_URL, UPDATE_SECRET         optional, enables the verified updater
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import html
import io
import json
import logging
import os
import random
import re
import re as _re
import secrets
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

import aiohttp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
_RealIKB = InlineKeyboardButton

from telegram import error as tg_error
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ffstore")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_VERSION = "5.0.0"
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ["ADMIN_ID"])
FG_API_KEY  = os.environ["FG_API_KEY"]
FG_BASE_URL = "https://famgateway.in/api"

POLL_INTERVAL    = 2          # seconds between each status check
POLL_MAX_SECONDS = 360        # the gateway order lives 300s; small grace on top
DISPLAY_TIMER_SECONDS = 60    # purely visual progress indicator (never expires an order)
DISPLAY_TIMER_STEP    = 5     # seconds between progress edits (stays well inside edit rate limits)

SELF_PATH  = os.path.abspath(__file__)
LOGO_PATH  = os.path.join(os.path.dirname(SELF_PATH), "logo.png")
LOGO_RATIO = 0.22
# Persistent files. On Railway/containers mount a volume and set DATA_DIR, otherwise data is lost on redeploy.
_DATA_DIR  = os.getenv("DATA_DIR", os.path.dirname(SELF_PATH))
FJ_FILE    = os.path.join(_DATA_DIR, "forcejoin.json")     # force-join list      (unchanged format)
DATA_FILE  = os.path.join(_DATA_DIR, "botdata.json")       # users + super admins (unchanged format, extra fields)
APP_FILE   = os.path.join(_DATA_DIR, "appdata.json")       # posts, orders, ledger, promos, settings ...
KEY_FILE   = os.path.join(_DATA_DIR, "data.key")           # auto-generated when DATA_KEY is not set
BACKUP_DIR = os.path.join(_DATA_DIR, "backups")
UPDATE_STATE_FILE = os.path.join(_DATA_DIR, "update_state.json")
BOT_NAME   = "𝘾𝘼𝙋𝙏𝘼𝙄𝙉 𝙓 𝙎𝙏𝙊𝙍𝙀"

MIN_TOPUP      = float(os.getenv("MIN_TOPUP", "5"))
TOPUP_PRESETS  = [5, 10, 50, 100, 200, 500]
MIN_PAYABLE    = 1.0           # a discounted order can never go below this
REFERRAL_BONUS = float(os.getenv("REFERRAL_BONUS", "0"))
BACKUP_KEEP_LOCAL    = 20
BACKUP_KEEP_TELEGRAM = 24
IST = timezone(timedelta(hours=5, minutes=30))

# ─── STATES ────────────────────────────────────────────────────────────────────
ADMIN_WAIT_POST_ID = "ADMIN_WAIT_POST_ID"
ADMIN_WAIT_PRICE   = "ADMIN_WAIT_PRICE"
ADMIN_WAIT_PHOTO   = "ADMIN_WAIT_PHOTO"
ADMIN_WAIT_INFO    = "ADMIN_WAIT_INFO"
ADMIN_WAIT_CREDS   = "ADMIN_WAIT_CREDS"
ADMIN_EDIT_VALUE   = "ADMIN_EDIT_VALUE"
BUYER_WAIT_POST_ID = "BUYER_WAIT_POST_ID"
BUYER_WAIT_PROMO   = "BUYER_WAIT_PROMO"
ADMIN_FJ_ADD       = "ADMIN_FJ_ADD"
ADMIN_BAN_ID       = "ADMIN_BAN_ID"
ADMIN_UNBAN_ID     = "ADMIN_UNBAN_ID"
ADMIN_SA_ADD       = "ADMIN_SA_ADD"
WALLET_WAIT_AMOUNT = "WALLET_WAIT_AMOUNT"
ADMIN_BC_ADD       = "ADMIN_BC_ADD"
ADMIN_BC_EDIT      = "ADMIN_BC_EDIT"
ADMIN_PROMO_WIZ    = "ADMIN_PROMO_WIZ"
ADMIN_PROMO_EDIT   = "ADMIN_PROMO_EDIT"
ADMIN_CH_SET       = "ADMIN_CH_SET"
ADMIN_BK_SET       = "ADMIN_BK_SET"
ADMIN_RESTORE_FILE = "ADMIN_RESTORE_FILE"
ADMIN_QUOTE_PRICE  = "ADMIN_QUOTE_PRICE"
ADMIN_REJECT_REASON = "ADMIN_REJECT_REASON"
ADMIN_LIST_PRICE   = "ADMIN_LIST_PRICE"
ADMIN_SETTING_EDIT = "ADMIN_SETTING_EDIT"
ADMIN_ICON_WAIT    = "ADMIN_ICON_WAIT"
SELL_PHOTOS        = "SELL_PHOTOS"
SELL_LOGIN_ID      = "SELL_LOGIN_ID"
SELL_PASSWORD      = "SELL_PASSWORD"
SELL_EXTRA         = "SELL_EXTRA"

# ─── IN-MEMORY STORE (persisted through appdata.json / botdata.json / forcejoin.json) ──
posts: dict          = {}   # {post_id: {"price","creds","photo","info","created","ch_msg","status"}}
sold: set            = set()
reserved: dict       = {}   # {post_id: order_id | "creating"}
pending_orders: dict = {}   # {order_id: {...}}  persisted so a restart can reconcile with the gateway

user_state: dict   = {}
admin_temp: dict   = {}
users: dict        = {}     # {uid: {"name","username","banned","balance","joined",...}}
super_admins: dict = {}     # {uid: name}
force_join: list   = []     # [{"id": int, "title": str, "link": str}]
appdata: dict      = {}     # everything else (see _default_app)
_last_seen: dict   = {}     # {order_id: (pub_json, auth_json)} — log de-dup
_tasks: set        = set()
_timers: dict      = {}     # {order_id: asyncio.Task} — progress indicators (one per order)
_polling: set      = set()  # order ids that currently have a poller
_http = None
_dirty = False              # low-priority changes flushed by the autosave loop

flow_msgs: dict    = {}     # {uid: [(chat_id, msg_id)]} bot messages that are safe to clean up
welcome_msg: dict  = {}     # {uid: msg_id} the main menu message (never cleaned up by flows)
checkout: dict     = {}     # {uid: {"post_id","code","chat_id","card_msg"}}
sell_draft: dict   = {}     # {uid: {...}} seller submission in progress


def _default_app() -> dict:
    return {
        "version": 1,
        "ledger": [], "ledger_seq": 0, "ledger_refs": {},
        "invoices": {}, "invoice_seq": 0,
        "promos": {}, "redemptions": [], "holds": {},
        "groups": {}, "broadcasts": {},
        "submissions": {}, "sub_seq": 0,
        "activity": [],
        "order_seq": 0, "post_seq": 0,
        "settings": {
            "store_name": "",            # empty -> BOT_NAME
            "orders_claim": "300+",      # owner-confirmed figure shown in promotions; empty hides the line
            "bot_link": os.getenv("BOT_LINK", ""),
            "support_link": os.getenv("SUPPORT_LINK", ""),
            "promo_enabled": False, "promo_paused": False, "promo_interval_h": 3,
            "promo_last_ts": 0,
            "backup_chat": None, "backup_enabled": True, "backup_interval_h": 2,
            "backup_last_ts": 0, "backup_msgs": [],
            "inv_channel": None,
            "btn_icons": {},
            "min_topup": MIN_TOPUP,
        },
    }


# ─── ATOMIC FILE HELPERS ──────────────────────────────────────────────────────
def _atomic_write(path: str, data: bytes):
    """Write to a temp file, fsync, then atomically replace. Keeps the previous version as .prev."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    if os.path.exists(path):
        try:
            shutil.copyfile(path, f"{path}.prev")
        except OSError:
            pass
    os.replace(tmp, path)


def _dump(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str).encode("utf8")


def _read_json(path: str):
    """Load JSON; fall back to the .prev copy when the main file is corrupt. Returns None if absent."""
    for p in (path, f"{path}.prev"):
        try:
            with open(p, encoding="utf8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            continue
        except Exception:
            log.exception("could not read %s", p)
    return None


# ─── CREDENTIAL VAULT (encryption at rest) ────────────────────────────────────
_fernet = None


def _vault():
    """Fernet instance, or None if `cryptography` is not installed / the key is unusable."""
    global _fernet
    if _fernet is not None:
        return _fernet or None
    try:
        from cryptography.fernet import Fernet
    except Exception:
        log.warning("cryptography not installed — credentials are stored WITHOUT encryption and excluded from backups")
        _fernet = False
        return None
    key = os.getenv("DATA_KEY", "").strip().encode()
    if not key:
        try:
            with open(KEY_FILE, "rb") as fh:
                key = fh.read().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                os.makedirs(_DATA_DIR, exist_ok=True)
                fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(key)
            except Exception:
                log.exception("could not persist the generated data key")
    try:
        _fernet = Fernet(key)
    except Exception:
        log.error("DATA_KEY is not a valid Fernet key")
        _fernet = False
        return None
    return _fernet


def vault_enc(text: str) -> str:
    f = _vault()
    if f is None:
        return "plain:" + (text or "")
    return "enc:" + f.encrypt((text or "").encode("utf8")).decode()


def vault_dec(blob):
    """Returns the plaintext, or None when it cannot be decrypted with the current key."""
    if blob is None:
        return ""
    blob = str(blob)
    if blob.startswith("plain:"):
        return blob[6:]
    if blob.startswith("enc:"):
        f = _vault()
        if f is None:
            return None
        try:
            return f.decrypt(blob[4:].encode()).decode("utf8")
        except Exception:
            return None
    return blob     # legacy value without a prefix


# ─── PERSISTENCE ──────────────────────────────────────────────────────────────
def _data_save():
    """Users + super admins (botdata.json — same format as v4, users just carry more fields)."""
    try:
        _atomic_write(DATA_FILE, _dump({"users": {str(k): v for k, v in users.items()},
                                        "super_admins": {str(k): v for k, v in super_admins.items()}}))
    except Exception:
        log.exception("data save failed")


def _data_load():
    d = _read_json(DATA_FILE)
    if not isinstance(d, dict):
        return
    try:
        users.update({int(k): v for k, v in d.get("users", {}).items()})
        super_admins.update({int(k): v for k, v in d.get("super_admins", {}).items()})
    except Exception:
        log.exception("data load failed")


def app_save():
    """Posts / sold / pending orders / ledger / promos / settings ... (credentials encrypted)."""
    global _dirty
    try:
        d = dict(appdata)
        d["posts"] = {pid: {**p, "creds": vault_enc(p.get("creds", ""))} for pid, p in posts.items()}
        d["sold"] = sorted(sold)
        d["pending"] = dict(pending_orders)
        _atomic_write(APP_FILE, _dump(d))
        _dirty = False
    except Exception:
        log.exception("app save failed")


def save_all():
    _data_save()
    _fj_save()
    app_save()


def mark_dirty():
    global _dirty
    _dirty = True


def _merge_app(raw: dict | None) -> dict:
    base = _default_app()
    raw = raw or {}
    for k, v in raw.items():
        if k == "settings" and isinstance(v, dict):
            base["settings"].update(v)
        else:
            base[k] = v
    return base


def _install_app_state(app: dict, live_pending: dict | None = None):
    """Install a (merged) app dict into the live globals. Decrypts credentials with the current key."""
    posts.clear()
    for pid, p in (app.pop("posts", {}) or {}).items():
        creds = vault_dec(p.get("creds", ""))
        rec = {**p, "creds": creds if creds is not None else ""}
        if creds is None:
            rec["creds_locked"] = True          # cannot be decrypted with this key — never sold
        posts[pid] = rec
    sold.clear()
    sold.update(app.pop("sold", []) or [])
    pend = app.pop("pending", {}) or {}
    pending_orders.clear()
    pending_orders.update(live_pending if live_pending is not None else pend)
    appdata.clear()
    appdata.update(app)
    reserved.clear()
    for oid, info in pending_orders.items():
        if info.get("kind", "buy") == "buy" and info.get("post_id"):
            reserved[info["post_id"]] = oid


def app_load():
    _install_app_state(_merge_app(_read_json(APP_FILE)))
    for p in posts.values():
        p.setdefault("photo", None)
        p.setdefault("info", "")
        p.setdefault("created", time.time())
        p.setdefault("ch_msg", None)


def S() -> dict:
    """Settings shortcut."""
    return appdata["settings"]


# ─── USERS / ACTIVITY ─────────────────────────────────────────────────────────
def _urec_defaults() -> dict:
    return {"name": "", "username": "", "banned": False, "balance": 0.0, "joined": time.time(),
            "last_seen": time.time(), "last_stage": "start", "last_stage_ts": time.time(),
            "referrer": None, "refs": 0}


def urec(uid: int) -> dict:
    rec = users.setdefault(uid, {})
    for k, v in _urec_defaults().items():
        rec.setdefault(k, v)
    return rec


def log_event(uid: int, action: str, detail: str = ""):
    """Activity trail used by the 2-hour backup report ("where did each user get to")."""
    try:
        rec = urec(uid)
        rec["last_stage"], rec["last_stage_ts"] = action, time.time()
        act = appdata.setdefault("activity", [])
        act.append({"ts": time.time(), "uid": uid, "a": action, "d": detail[:80]})
        if len(act) > 3000:
            del act[:len(act) - 3000]
        mark_dirty()
    except Exception:
        log.exception("log_event failed")


# ─── LEDGER (wallet) — idempotent and double-spend safe ───────────────────────
# Everything below runs without awaiting, so on asyncio's single thread each call is atomic.
class InsufficientBalance(Exception):
    pass


def ledger_add(uid: int, kind: str, amount: float, ref: str = "", note: str = ""):
    """Append a ledger entry and update the balance. Idempotent per (kind, ref) when ref is given.
    Returns the entry, or None when the same (kind, ref) was already booked."""
    key = f"{kind}:{ref}" if ref else ""
    refs = appdata.setdefault("ledger_refs", {})
    if key and key in refs:
        return None
    rec = urec(uid)
    new_bal = round(float(rec.get("balance", 0)) + float(amount), 2)
    if new_bal < -0.0001:
        raise InsufficientBalance()
    rec["balance"] = new_bal
    appdata["ledger_seq"] = appdata.get("ledger_seq", 0) + 1
    entry = {"id": f"TX{appdata['ledger_seq']:06d}", "ts": time.time(), "uid": uid, "type": kind,
             "amount": round(float(amount), 2), "balance_after": new_bal, "ref": ref, "note": note[:120]}
    appdata.setdefault("ledger", []).append(entry)
    if key:
        refs[key] = entry["id"]
    return entry


def balance_of(uid: int) -> float:
    return round(float(urec(uid).get("balance", 0)), 2)


# ─── PROMO CODES ──────────────────────────────────────────────────────────────
PROMO_RE = _re.compile(r"^[A-Z0-9_-]{3,20}$")
_PROMO_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def promo_normalize(code: str) -> str:
    return _re.sub(r"\s+", "", code or "").upper()


def promo_gen_code() -> str:
    promos = appdata.setdefault("promos", {})
    while True:
        code = "CX-" + "".join(secrets.choice(_PROMO_ALPHABET) for _ in range(8))
        if code not in promos:
            return code


def promo_counts(code: str, uid: int, exclude_order: str | None = None):
    """(total_used, used_by_user) — completed redemptions plus in-flight holds."""
    tot = usr = 0
    for r in appdata.get("redemptions", []):
        if r["code"] == code:
            tot += 1
            usr += r["uid"] == uid
    for oid, h in appdata.get("holds", {}).items():
        if h["code"] == code and oid != exclude_order:
            tot += 1
            usr += h["uid"] == uid
    return tot, usr


def promo_evaluate(code_raw: str, uid: int, price: float, exclude_order: str | None = None) -> dict:
    """Server-side promo validation. Never trusts anything supplied by the user except the code text.
    Returns {"ok","reason","code","discount","final"}."""
    code = promo_normalize(code_raw)
    res = {"ok": False, "reason": "", "code": code, "discount": 0.0, "final": round(price, 2)}
    p = appdata.get("promos", {}).get(code)
    if not p:
        res["reason"] = "This promo code does not exist."
        return res
    now = time.time()
    if not p.get("enabled", True):
        res["reason"] = "This promo code is disabled."
    elif p.get("start") and now < p["start"]:
        res["reason"] = "This promo code is not active yet."
    elif p.get("expiry") and now > p["expiry"]:
        res["reason"] = "This promo code has expired."
    elif price < float(p.get("min_price") or 0):
        res["reason"] = f"This code needs an ID priced at least ₹{money(p['min_price'])}."
    elif p.get("max_price") is not None and price > float(p["max_price"]):
        res["reason"] = f"This code only works for IDs priced up to ₹{money(p['max_price'])}."
    elif p.get("min_order") is not None and price < float(p["min_order"]):
        res["reason"] = f"Minimum order value for this code is ₹{money(p['min_order'])}."
    else:
        tot, usr = promo_counts(code, uid, exclude_order)
        if p.get("max_total") is not None and tot >= int(p["max_total"]):
            res["reason"] = "This promo code has reached its redemption limit."
        elif usr >= int(p.get("max_per_user") or 1):
            res["reason"] = "You have already used this promo code the maximum number of times."
        else:
            if p["type"] == "percent":
                disc = round(price * float(p["value"]) / 100.0, 2)
            else:
                disc = round(float(p["value"]), 2)
            disc = min(disc, round(price - MIN_PAYABLE, 2))      # payable amount can never go below MIN_PAYABLE
            if disc <= 0:
                res["reason"] = "This price is too low for a discount."
            else:
                res.update(ok=True, discount=disc, final=round(price - disc, 2), reason="")
    return res


def promo_hold(order_id: str, code: str, uid: int):
    appdata.setdefault("holds", {})[order_id] = {"code": code, "uid": uid, "ts": time.time()}


def promo_release(order_id: str):
    appdata.get("holds", {}).pop(order_id, None)


def promo_redeem(order_id: str, code: str, uid: int, price: float, discount: float, final: float):
    """Convert a hold into a completed redemption — only after the purchase is finalised. Idempotent."""
    appdata.get("holds", {}).pop(order_id, None)
    reds = appdata.setdefault("redemptions", [])
    if any(r["order_id"] == order_id for r in reds):
        return
    reds.append({"code": code, "uid": uid, "order_id": order_id, "ts": time.time(),
                 "price": price, "discount": discount, "final": final})


def promo_describe(p: dict) -> str:
    val = f"{money(p['value'])}%" if p["type"] == "percent" else f"₹{money(p['value'])}"
    tot, _ = promo_counts(p["code"], 0)
    lim = f"/{p['max_total']}" if p.get("max_total") is not None else ""
    exp = fmt_dt(p["expiry"]) if p.get("expiry") else "no expiry"
    rng = f"₹{money(p.get('min_price') or 0)}–" + (f"₹{money(p['max_price'])}" if p.get("max_price") is not None else "any")
    state = "🟢 enabled" if p.get("enabled", True) else "🔴 disabled"
    return (f"<code>{html.escape(p['code'])}</code> — {val} off · price {rng} · used {tot}{lim} "
            f"· {p.get('max_per_user') or 1}/user · {exp} · {state}")


# ─── INVOICES / RECEIPTS ──────────────────────────────────────────────────────
def fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %I:%M %p IST")


def make_invoice(order_id: str, **fields) -> tuple[dict, bool]:
    """Idempotent: one invoice per verified order. Returns (invoice, created_now)."""
    invs = appdata.setdefault("invoices", {})
    if order_id in invs:
        return invs[order_id], False
    appdata["invoice_seq"] = appdata.get("invoice_seq", 0) + 1
    now = time.time()
    inv = {"number": f"INV-{datetime.fromtimestamp(now, IST):%Y%m%d}-{appdata['invoice_seq']:05d}",
           "order_id": order_id, "ts": now, "status": "PAID (verified)", **fields}
    invs[order_id] = inv
    return inv, True


def render_invoice(inv: dict) -> str:
    e = html.escape
    store = S().get("store_name") or BOT_NAME
    lines = [
        "🧾 <b>PAYMENT RECEIPT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🆔 Invoice : <code>{e(inv['number'])}</code>",
        f"📅 Date    : {e(fmt_dt(inv['ts']))}",
        f"👤 User    : <code>{inv['uid']}</code>" + (f" (@{e(inv['username'])})" if inv.get("username") else ""),
        f"🆔 Order   : <code>{e(inv['order_id'])}</code>",
    ]
    if inv.get("post_id"):
        lines.append(f"📌 Listing : <code>{e(inv['post_id'])}</code>")
    lines.append(f"📦 Item    : {e(inv.get('item', '-'))}")
    lines.append(f"💰 Price   : ₹{money(inv.get('price', 0))}")
    if inv.get("discount"):
        lines.append(f"🏷️ Discount: -₹{money(inv['discount'])}" + (f" ({e(inv['promo'])})" if inv.get("promo") else ""))
    lines += [
        f"💸 <b>Paid     : ₹{money(inv.get('paid', 0))}</b>",
        f"🏦 Method  : {e(inv.get('method', '-'))}",
        f"🔖 UTR/Ref : <code>{e(inv.get('utr') or 'Not provided')}</code>",
        f"✅ Status  : {e(inv['status'])}",
        "━━━━━━━━━━━━━━━━━━",
        f"🏪 {e(store)}",
    ]
    sup = S().get("support_link")
    if sup:
        lines.append(f"🙏 Support: {e(sup)}")
    return "\n".join(lines)


# ─── PREMIUM ICON RESOLUTION STATE (used by the styling engine below) ─────────
_FORCED_ICONS: dict = {}      # label key -> valid custom-emoji id
_FORCE_REPORT: dict = {}      # label key -> (id, glyph, animated, source)
_STICKER_INFO: dict = {}      # id -> (glyph, animated)
_GLYPH_IDS: dict = {}         # glyph -> [ids]
_EXTRA_ICON_IDS: set = set()  # owner supplied ids (validated at startup)
_GLYPH_CACHE: dict = {}

# label key -> glyph preference list (best semantic match first). Animated icons are preferred.
_BUTTON_GLYPHS = {
    "delete_post":        ["🗑", "🚮", "❌", "🧹", "⛔"],
    "pending_orders":     ["📦", "⏳", "🧾", "📋", "📭", "🕐"],
    "force_join":         ["📢", "📣", "🔔", "🔒", "🚪"],
    "manage_users":       ["👥", "👤", "🫂", "👑", "🧑"],
    "broadcast":          ["📣", "📢", "🚀", "📡", "🔔"],
    "promo_codes":        ["🏷", "🎟", "🎁", "💸", "🔥"],
    "owner_channel":      ["📡", "📢", "📺", "🔗", "💬"],
    "backup_restore":     ["💾", "🗄", "☁", "🔄", "📦"],
    "seller_submissions": ["💼", "📥", "📨", "🤝", "📋"],
    "sell_your_id":       ["💸", "💰", "🤝", "💼"],
    "my_profile":         ["👤", "🪪", "🧑", "⭐"],
    "wallet":             ["💰", "👛", "💳", "🪙"],
    "my_orders":          ["🧾", "📦", "📋", "🛍"],
    "updates":            ["🔄", "⬆", "🆙", "🚀"],
}


def _glyph_icon(glyph: str):
    """A valid custom-emoji id with this glyph (animated preferred), or None."""
    if glyph in _GLYPH_CACHE:
        return _GLYPH_CACHE[glyph]
    ids = [i for i in _GLYPH_IDS.get(glyph, []) if i in _VALID_IDS]
    anim = [i for i in ids if _STICKER_INFO.get(i, ("", False))[1]]
    res = (anim or ids or [None])[0]
    _GLYPH_CACHE[glyph] = res
    return res


def _resolve_forced_icons():
    """Pick a valid (animated when possible) premium icon for every named button. Called after validation."""
    _FORCED_ICONS.clear()
    _FORCE_REPORT.clear()
    _GLYPH_CACHE.clear()
    overrides = (appdata.get("settings") or {}).get("btn_icons", {}) or {}
    for key, glyphs in _BUTTON_GLYPHS.items():
        cid = overrides.get(key)
        if cid and cid in _VALID_IDS:
            g, anim = _STICKER_INFO.get(cid, ("?", False))
            _FORCED_ICONS[key] = cid
            _FORCE_REPORT[key] = (cid, g, anim, "owner")
            continue
        pick = None
        for g in glyphs:                                   # pass 1: animated, best glyph first
            i = _glyph_icon(g)
            if i and _STICKER_INFO.get(i, ("", False))[1]:
                pick = (i, g, True)
                break
        if pick is None:
            for g in glyphs:                               # pass 2: static of the best matching glyph
                i = _glyph_icon(g)
                if i:
                    pick = (i, g, False)
                    break
        if pick:
            _FORCED_ICONS[key] = pick[0]
            _FORCE_REPORT[key] = (pick[0], pick[1], pick[2], "auto")


# ─── LOW-LEVEL HELPERS ────────────────────────────────────────────────────────
def money(x) -> str:
    return f"{float(x):g}"


def is_owner(uid: int) -> bool:
    return uid == ADMIN_ID


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in super_admins


def _spawn(coro):
    """Run a coroutine in the background with a strong reference and error logging."""
    t = asyncio.create_task(coro)
    _tasks.add(t)

    def _done(task):
        _tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())
    t.add_done_callback(_done)
    return t

# ─── STYLING ──────────────────────────────────────────────────────────────────
# Small-caps map — identical to app.py _SC_MAP
_SC_MAP = dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴩǫʀꜱᴛᴜᴠᴡxʏᴢ"
))

def _sc(text: str) -> str:
    return "".join(
        _SC_MAP.get(ch.lower(), ch) if ch.isalpha() and ch.isascii() else ch
        for ch in str(text)
    )

def _sc_title(text: str) -> str:
    out = []
    for w in str(text).split(" "):
        if not w:
            out.append(w); continue
        i = next((k for k, ch in enumerate(w) if ch.isalpha() and ch.isascii()), None)
        if i is None:
            out.append(w); continue
        out.append(w[:i] + w[i].upper() + _sc(w[i + 1:]))
    return " ".join(out)

def stylish(text: str) -> str:
    """Small-caps Unicode transform — same as app.py."""
    return _sc_title(text)


def _btn_icon(label: str):
    """Pick a premium icon from the label's role when the label has no emoji."""
    l = label.lower()
    rules = [
        (("buy", "again", "purchase"), "buy"), (("admin", "panel"), "admin"),
        (("back",), "back"), (("home", "menu", "prev", "next", "page"), "next"), (("cancel", "delete"), "help"),
        (("add",), "redeem"), (("view", "post", "list", "order"), "gst"),
        (("edit", "price", "change"), "balance"), (("cred",), "leak"),
        (("open", "bot"), "telegram"),
    ]
    for words, key in rules:
        if any(w in l for w in words) and _BTN_EM[key] not in _INVALID_IDS:
            return _BTN_EM[key]
    pool = [i for i in STICKER_POOL if i not in _INVALID_IDS] or STICKER_POOL
    return pool[sum(map(ord, label)) % len(pool)]


_LEAD_EMOJI_RE = _re.compile(
    "^(?:[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF"
    "\u00A9\u00AE\u203C\u2049\u3030\u303D\u3297\u3299][\ufe0f\u200d]*)+\\s*")


def _norm_label_key(label: str) -> str:
    return _re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _split_btn(text: str):
    """Leading emoji -> its premium ID (the emoji is removed from the label).

    Resolution order:
      1. owner override / forced icon for this exact label (settings -> btn_icons, resolved at startup)
      2. the emoji's own premium ID (when valid)
      3. ANY leading emoji whose ID is invalid/unknown -> a valid animated premium emoji with the
         same glyph (so a plain emoji never leaks into a button), else a role icon
    """
    m = _PREMIUM_RE.match(text)
    if m:
        rest = text[m.end():].strip()
        eid = _PREMIUM_MAP[m.group(0).replace("\ufe0f", "")]
        forced = _FORCED_ICONS.get(_norm_label_key(rest or text))
        if forced and forced not in _INVALID_IDS:
            return (rest or text), forced
        if eid not in _INVALID_IDS:
            return (rest or text), eid
    lm = _LEAD_EMOJI_RE.match(text)
    if lm:
        glyph = lm.group(0).strip().replace("\ufe0f", "").replace("\u200d", "")
        rest = text[lm.end():].strip() or text
        forced = _FORCED_ICONS.get(_norm_label_key(rest))
        if forced and forced not in _INVALID_IDS:
            return rest, forced
        return rest, (_glyph_icon(glyph) or _btn_icon(rest))
    forced = _FORCED_ICONS.get(_norm_label_key(text))
    if forced and forced not in _INVALID_IDS:
        return text, forced
    return text, _btn_icon(text)


def _btn_style(label: str) -> str:
    """Button colour: danger=red, success=green, primary=blue."""
    l = label.lower()
    if any(w in l for w in ("back", "cancel", "delete", "remove", "reject", "decline", "stop")):
        return "danger"
    if any(w in l for w in ("buy", "add", "confirm", "approve", "paid", "pay", "done",
                            "again", "open", "yes", "available", "sold")):
        return "success"
    return "primary"


def _mk_btn(cls, base: dict, icon, style):
    """Build a button. New PTB: native kwargs. Old PTB: the same via api_kwargs
    (icon_custom_emoji_id + style always reach the Telegram server)."""
    for native in (dict(icon_custom_emoji_id=icon, style=style),
                   dict(icon_custom_emoji_id=icon), dict(style=style)):
        native = {k: v for k, v in native.items() if v}
        try:
            return cls(**base, **native)
        except TypeError:
            continue
    api = {k: v for k, v in (("icon_custom_emoji_id", icon), ("style", style)) if v}
    try:
        return cls(**base, api_kwargs=api)
    except TypeError:
        return cls(**base)


def _make_ikb(label, eid, style, extra, fx):
    return _mk_btn(_RealIKB, dict(text=fx(label), **extra), eid, style)


def style_btn(text: str, style: str = "primary", emoji_id: str | None = None) -> KeyboardButton:
    """Colored reply-keyboard button + premium icon (success=green, primary=blue, danger=red)."""
    label, eid = _split_btn(text)
    style = style if style in ("primary", "success", "danger") else "primary"
    return _mk_btn(KeyboardButton, dict(text=stylish(label)), emoji_id or eid, style)


def _inline_btn(text: str, _sc: bool = True, _style: str | None = None, **extra):
    """Inline button + premium icon + color."""
    fx = stylish if _sc else (lambda t: t)
    label, eid = _split_btn(text)
    return _make_ikb(label, eid, _style or _btn_style(label), extra, fx)


def InlineKeyboardButton(text, *args, **kw):  # noqa: N802  (shadow: har button premium ho jaye)
    if args:
        kw.setdefault("callback_data", args[0])
    return _inline_btn(text, _sc=False, **kw)


def ibt(text: str, callback_data: str):
    """Styled inline button (callback) with premium icon."""
    return _inline_btn(text, callback_data=callback_data)


def ibt_url(text: str, url: str):
    return _inline_btn(text, url=url)


# ═══════════════════════════════════════════════════════════════════════════════
# 💎 PREMIUM EMOJIS / ICONS  (ported from app.py — IDs unchanged)
# ═══════════════════════════════════════════════════════════════════════════════
import random
import re as _re

_INVALID_IDS: set = set()   # startup pe Telegram se check hoke bhare jate hain
_VALID_IDS: set = set()

def _te(eid, fb="⭐"):
    if eid in _INVALID_IDS:          # one invalid ID makes Telegram reject the whole message
        return fb
    return f"<tg-emoji emoji-id='{eid}'>{fb}</tg-emoji>"

def em(emoji_id: str, fallback: str = "⭐") -> str:
    return (f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
            if emoji_id and "YOUR_" not in emoji_id and emoji_id not in _INVALID_IDS else fallback)

# Frame / decoration
_LOOP_ID    = "5465629669829128119"   # ➿
_BLUE_RING  = "6037622221625626773"   # 💠
_BLUE_SHK   = "6113891550788324241"   # ✨
_BLUE_GLOW  = "6039539366177541657"   # 💫
_BLUE_STAR  = "6147565374289220368"   # ✅
_BLUE_DIAM  = "6289741869962236795"   # 💎
_ANIM_PE    = "<tg-emoji emoji-id='6255767342916045646'>⚡</tg-emoji>"

# Welcome / group emojis
_EM_WELCOME = "4958830950604604428"   # 👋
_EM_HELLO   = "4956461073550017373"   # ✨
_EM_USER2   = "4958469026595472714"   # 👤
_EM_CMD     = "6147422674000808494"   # ⚙️ / 💡
_EM_RULES   = "4958724224962265918"   # 📋
_EM_NOSPAM  = "4958636483075376288"   # 🚫
_EM_NOABUSE = "4958472587123360612"   # 🛑
_EM_RESPECT = "4956525562483967357"   # ❤️
_EM_DEV     = "4958900559139570572"   # 👨‍💻

_ICON_RULES = [
    (("credit", "balance", "wallet", "coin", "pts"),    "5224257782013769471", "💰"),
    (("refer", "invite"),                               "6242498410822244114", "👥"),
    (("link",),                                         "4958689671950369798", "🔗"),
    (("premium", "plan", "vip", "price", "validity"),   "6285328937094485878", "👑"),
    (("owner", "admin", "manager", "support"),          "6204263190319081111", "👨‍💻"),
    (("user", "name", "group"),                         "5373012449597335010", "👤"),
    (("id", "code", "key", "txn", "utr"),               "5888781182249738113", "🆔"),
    (("date", "time", "expiry", "left", "delete"),      "5188311512791393083", "⏳"),
    (("status", "mode", "server", "state"),             "5219745609631674840", "⚡"),
]
_ROTATE = [("5431577498364158238", "📱"), ("5470135030393090150", "📸"),
           ("5406631276042002796", "🔥"), ("4958734459869332468", "⭐"),
           ("5372926953978341366", "❤️"), ("5197269100878907942", "💡")]

def _icon_for(key: str, idx: int = 0) -> str:
    tokens = _re.findall(r"[a-z]+", key.lower())
    for words, eid, fb in _ICON_RULES:
        if any(t.startswith(w) for w in words for t in tokens):
            return _te(eid, fb)
    eid, fb = _ROTATE[idx % len(_ROTATE)]
    return _te(eid, fb)

# Button icons (icon_custom_emoji_id)
_BTN_EM = {
    "num": "5431577498364158238", "telegram": "5188281597257789112",
    "bgmi": "5406631276042002796", "insta": "5470135030393090150",
    "target": "5190536202771524285", "bomber": "5406631276042002796",
    "spin": "5372849966689566579", "redeem": "5289722755871162900",
    "referral": "5190536202771524285", "buy": "5289722755871162900",
    "next": "5188326141450021801", "back": "5188326141450021801",
    "aadhar": "5188540580577179842", "aadharpan": "5190526783908244410",
    "vehicle": "5190594348038777921", "truecaller": "5190424211499281864",
    "leak": "5190849937247595090", "gst": "5190895201907927251",
    "pincode": "5190629317662500419", "family": "5190566778643702939",
    "admin": "6204263190319081111", "balance": "5224257782013769471",
    "help": "5190801365462446463", "support": "5190580157466830731",
}
STICKER_POOL = [
    "5190526783908244410", "5190594348038777921", "5190772172569733312",
    "5190424211499281864", "5190849937247595090", "5190801365462446463",
    "5188667999371948784", "5190577103745082477", "5190815057818184605",
    "5188540580577179842", "5188296252772613374", "5188191464160523568",
    "5188326141450021801", "5190850877845433996", "5188556420416564931",
    "5190787213545203401", "5190566778643702939", "5190580157466830731",
    "5190799248043567339", "5190895201907927251", "5190629317662500419",
    "5188319295272155979", "5190536202771524285", "5190835214099704949",
    "5289722755871162900", "5372849966689566579", "6237927637906364256",
]
def get_random_sticker():
    return random.choice(STICKER_POOL)

MESSAGE_EFFECT_ID = "5044134455711629726"

# Standard emoji -> premium id
_PREMIUM_MAP_RAW = {
    # ── IDs previously used with the same emoji ──
    "💰": "5224257782013769471", "⏳": "5188311512791393083",
    "🆔": "5888781182249738113", "👤": "5373012449597335010",
    "📱": "5431577498364158238", "💎": _BLUE_DIAM,
    "✅": _BLUE_STAR,            "📋": _EM_RULES,
    "⚙️": _EM_CMD,              "⚡": "5219745609631674840",
    "👑": "6285328937094485878", "🔗": "4958689671950369798",
    "👥": "6242498410822244114", "👨‍💻": "6204263190319081111",
    "📸": "5470135030393090150", "🔥": "5406631276042002796",
    "⭐": "4958734459869332468", "❤️": "5372926953978341366",
    "💡": "5197269100878907942", "👋": _EM_WELCOME, "✨": _BLUE_SHK,
    "💫": _BLUE_GLOW,            "💠": _BLUE_RING,   "🚫": _EM_NOSPAM,
    "🛑": _EM_NOABUSE,
    # ── remaining emojis (premium pool) ──
    "🛒": "5289722755871162900", "🔙": "5188326141450021801",
    "🏠": "5190772172569733312", "🎮": "5188281597257789112",
    "👇": "5190850877845433996", "🟢": "5190577103745082477",
    "🟡": "5190815057818184605", "➕": "5188667999371948784",
    "✏️": "5188296252772613374", "🗑️": "5188191464160523568",
    "📦": "5188556420416564931", "📭": "5190787213545203401",
    "❌": "5190799248043567339", "🔍": "5188319295272155979",
    "⏰": "5190835214099704949", "📝": "6237927637906364256",
    "⚠️": "5190801365462446463", "🔐": "5190536202771524285",
    "♻️": "5372849966689566579", "📌": "5190629317662500419",
    "😔": "5190424211499281864", "🧾": "5190895201907927251",
    "🔄": "5190526783908244410", "🏦": "5190594348038777921",
    "🙏": "5190580157466830731", "💸": "5190849937247595090",
    "🚀": "5188540580577179842", "🔧": "5190566778643702939",
    "📢": "5188281597257789112",
}
_PREMIUM_MAP = {k.replace("\ufe0f", ""): v for k, v in _PREMIUM_MAP_RAW.items()}
def _build_premium_re():
    """Regex of all mapped emojis. With an empty map it must match nothing (an empty pattern would match '')."""
    if not _PREMIUM_MAP:
        return _re.compile(r"(?!x)x")
    return _re.compile("|".join(_re.escape(k) + "\ufe0f?" for k in sorted(_PREMIUM_MAP, key=len, reverse=True)))


_PREMIUM_RE = _build_premium_re()


def _all_known_ids() -> set:
    ids = set(_PREMIUM_MAP_RAW.values()) | set(_BTN_EM.values()) | set(STICKER_POOL)
    ids |= {_LOOP_ID, _BLUE_RING, _BLUE_SHK, _BLUE_GLOW, _BLUE_STAR, _BLUE_DIAM, "6255767342916045646",
            _EM_WELCOME, _EM_HELLO, _EM_USER2, _EM_CMD, _EM_RULES, _EM_NOSPAM, _EM_NOABUSE,
            _EM_RESPECT, _EM_DEV}
    ids |= {e for _, e, _ in _ICON_RULES} | {e for e, _ in _ROTATE}
    ids |= set(_EXTRA_ICON_IDS)
    return ids


async def _validate_premium_ids(bot):
    """Ask Telegram which IDs are real custom emojis and what their glyph is.
    Invalid IDs are dropped; remaining emojis are matched to a premium ID with the same glyph.
    Returns (valid_count, total, matched_glyphs, unmatched_emojis) ya None."""
    global _PREMIUM_RE
    ids = sorted(_all_known_ids())
    valid = {}
    for i in range(0, len(ids), 200):
        try:
            for st in await bot.get_custom_emoji_stickers(ids[i:i + 200]):
                if st.custom_emoji_id:
                    valid[st.custom_emoji_id] = st
        except Exception:
            log.exception("premium id validation failed")
            return None
    _VALID_IDS.clear(); _VALID_IDS.update(valid)
    _STICKER_INFO.clear()
    for _cid, _st in valid.items():
        _STICKER_INFO[_cid] = ((_st.emoji or '').replace('\ufe0f', ''), bool(getattr(_st, 'is_animated', False) or getattr(_st, 'is_video', False)))
    _INVALID_IDS.clear(); _INVALID_IDS.update(set(ids) - set(valid))

    glyph_ids: dict = {}
    def add(g, cid):
        g = (g or "").replace("\ufe0f", "")
        if g and cid not in glyph_ids.setdefault(g, []):
            glyph_ids[g].append(cid)
    for st in valid.values():
        add(st.emoji, st.custom_emoji_id)
    # Telegram's own forum-topic icon pool is always made of real custom emojis
    try:
        for st in await bot.get_forum_topic_icon_stickers():
            if st.custom_emoji_id:
                _STICKER_INFO[st.custom_emoji_id] = ((st.emoji or '').replace('\ufe0f', ''), bool(getattr(st, 'is_animated', False) or getattr(st, 'is_video', False)))
                add(st.emoji, st.custom_emoji_id)
                _VALID_IDS.add(st.custom_emoji_id)
    except Exception:
        log.warning('forum icon stickers unavailable')
    for name in {st.set_name for st in valid.values() if st.set_name}:
        try:
            for st in (await bot.get_sticker_set(name)).stickers:
                if st.custom_emoji_id:
                    add(st.emoji, st.custom_emoji_id)
                    _STICKER_INFO.setdefault(st.custom_emoji_id, ((st.emoji or '').replace('\ufe0f', ''), bool(getattr(st, 'is_animated', False) or getattr(st, 'is_video', False))))
        except Exception:
            log.warning("sticker set %s read failed", name)
    _GLYPH_IDS.clear(); _GLYPH_IDS.update(glyph_ids)

    new_map, matched, unmatched = {}, 0, []
    for k, v in list(_PREMIUM_MAP.items()):
        cands = glyph_ids.get(k, [])
        if v in cands:
            new_map[k] = v; matched += 1
        elif cands:
            new_map[k] = cands[0]; matched += 1
        elif v in _VALID_IDS:
            new_map[k] = v; unmatched.append(k)
        else:
            unmatched.append(k)          # not a valid ID -> plain emoji (message is not rejected)
    _PREMIUM_MAP.clear(); _PREMIUM_MAP.update(new_map)
    _PREMIUM_RE = _build_premium_re()
    _resolve_forced_icons()
    return len(valid), len(ids), matched, unmatched


def _swap_emojis(text: str) -> str:
    """Swap standard emojis in plain (non-tag) text for premium tg-emoji tags."""
    def rep(m):
        base = m.group(0).replace("\ufe0f", "")
        return _te(_PREMIUM_MAP[base], m.group(0))
    return _PREMIUM_RE.sub(rep, text)

def _swap_in_html(htm: str) -> str:
    """Swap emojis without touching HTML tags / <code> / <pre> / <tg-emoji>."""
    out, skip = [], 0
    for tok in _re.split(r"(<[^>]*>)", htm):
        if tok.startswith("<") and tok.endswith(">"):
            low = tok.lower()
            if _re.match(r"<(code|pre|tg-emoji)\b", low):
                skip += 1
            elif _re.match(r"</(code|pre|tg-emoji)>", low):
                skip = max(0, skip - 1)
            out.append(tok)
        else:
            out.append(tok if skip else _swap_emojis(tok))
    return "".join(out)

def _md_to_html(t: str) -> str:
    """Telegram legacy Markdown (*b* _i_ `c` ```p``` [t](u)) -> HTML."""
    stash = []
    def keep(s):
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"
    t = _re.sub(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```",
                lambda m: keep(f"<pre>{html.escape(m.group(1))}</pre>"), t, flags=_re.S)
    t = _re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), t)
    t = html.escape(t, quote=False)
    t = _re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = _re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", t)
    t = _re.sub(r"(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])", r"<i>\1</i>", t)
    t = _swap_in_html(t)
    return _re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], t)

_TITLE_MD = _re.compile(r"^\W{0,4}\s*\*[^*\n]+\*")
_TITLE_HTML = _re.compile(r"^\W{0,4}\s*<b>")

def _frame(html_text: str) -> str:
    """Wrap the first (title) line in premium ➿ lines + a ╭──💠✨ title 💫──╮ frame."""
    first, sep, rest = html_text.partition("\n")
    loop = _te(_LOOP_ID, "➿") * 11
    top = (f"{loop}\n╭──{_te(_BLUE_RING, '💠')}{_te(_BLUE_SHK, '✨')} "
           f"{first.strip()} {_te(_BLUE_GLOW, '💫')}──╮\n{loop}")
    return top + sep + rest

def _premium_text(text, parse_mode, frame=False):
    """(new_text, new_parse_mode) — hamesha HTML + premium emojis (+ optional title frame)."""
    if text is None:
        return text, parse_mode
    pm = (str(parse_mode).lower() if parse_mode else None)
    if pm in (None, "none"):
        return _swap_in_html(html.escape(text, quote=False)), "HTML"
    if pm in ("markdown", "markdownv2"):
        out = _md_to_html(text)
        if frame and _TITLE_MD.match(text.lstrip()):
            out = _frame(out)
        return out, "HTML"
    if pm == "html":
        out = _swap_in_html(text)
        if frame and _TITLE_HTML.match(text.lstrip()):
            out = _frame(out)
        return out, "HTML"
    return text, parse_mode

_PREM_ERR_SEEN: set = set()

def _install_premium_patch():
    """Wrap the bot send/edit methods.
    Stage 1: premium + frame  ->  Stage 2: premium bina frame  ->  Stage 3: original (plain).
    If premium formatting fails the exact error is sent to the admin (once per error)."""
    from telegram import error as tg_error
    from telegram.ext import ExtBot

    orig_send = ExtBot.send_message

    def wrap(name, text_kw, pos, frame=False):
        orig = getattr(ExtBot, name)

        def build(args, kwargs, with_frame):
            a, kw = list(args), dict(kwargs)
            if text_kw in kw and kw[text_kw] is not None:
                kw[text_kw], kw["parse_mode"] = _premium_text(kw[text_kw], kw.get("parse_mode"), with_frame)
            elif pos is not None and len(a) > pos and isinstance(a[pos], str):
                a[pos], kw["parse_mode"] = _premium_text(a[pos], kw.get("parse_mode"), with_frame)
            else:
                return None
            return a, kw

        async def wrapper(self, *args, **kwargs):
            stages = []
            try:
                st1 = build(args, kwargs, frame)
                if st1 is None:
                    return await orig(self, *args, **kwargs)
                stages.append(st1)
                if frame:
                    stages.append(build(args, kwargs, False))
            except Exception:
                log.exception("premium build failed")
                return await orig(self, *args, **kwargs)
            last = None
            for a, kw in stages:
                try:
                    return await orig(self, *a, **kw)
                except tg_error.BadRequest as exc:
                    if "not modified" in str(exc).lower():
                        raise
                    last = exc
                    log.warning("premium %s failed (%s)", name, exc)
            err = str(last)[:200]
            if err not in _PREM_ERR_SEEN:
                _PREM_ERR_SEEN.add(err)
                try:
                    await orig_send(self, chat_id=ADMIN_ID,
                                    text=f"⚠️ Premium emoji send failed ({name}):\n{err}\n\nThe user received a normal message instead.")
                except Exception:
                    pass
            return await orig(self, *args, **kwargs)
        wrapper.__name__ = name
        setattr(ExtBot, name, wrapper)

    wrap("send_message", "text", 1, True)
    wrap("edit_message_text", "text", 0, True)
    wrap("send_photo", "caption", None)
    wrap("edit_message_caption", "caption", None)


def _ptb_ver() -> str:
    import telegram
    return getattr(telegram, "__version__", "?")


def _btn_mode() -> str:
    import inspect
    try:
        ok = "style" in inspect.signature(KeyboardButton.__init__).parameters
    except Exception:
        ok = False
    return "native" if ok else "api_kwargs"

# ─── KEYBOARD BUILDERS ────────────────────────────────────────────────────────
def main_reply_kb(user_id: int) -> ReplyKeyboardMarkup:
    """Bottom coloured keyboard for private chat.

    Green = buy / sell action, Blue = navigation / info, Red = admin-only.
    """
    rows = [
        [style_btn("🛒 Buy FF ID", "success"), style_btn("💸 Sell Your ID", "success")],
        [style_btn("👤 My Profile", "primary"), style_btn("💰 Wallet", "primary")],
        [style_btn("🧾 My Orders", "primary")],
    ]
    if is_admin(user_id):
        rows.append([style_btn("⚙️ Admin Panel", "danger")])
    try:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False, is_persistent=True)
    except TypeError:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def back_button(callback: str = "main_menu") -> list:
    return [[ibt("🔙 Back", callback)]]


def menu_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[ibt("🏠 Main Menu", "main_menu")]])


# Reply-keyboard text normalisation: the small-caps styling / missing emoji must not break routing.
_SC_REV = {v: k for k, v in _SC_MAP.items()}


def norm_btn_text(text: str) -> str:
    t = "".join(_SC_REV.get(ch, ch) for ch in (text or "")).lower()
    t = re.sub(r"^[^a-z0-9]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


# ─── HELPERS ──────────────────────────────────────────────────────────────────
async def safe_delete(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        log.debug("delete_message failed: %r", e)


async def notify_admin(bot, text: str):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception:
        log.exception("could not notify admin")


def all_admin_ids() -> list:
    return [ADMIN_ID] + [a for a in super_admins if a != ADMIN_ID]


async def notify_admins(bot, text: str, **kw):
    for aid in all_admin_ids():
        try:
            await bot.send_message(chat_id=aid, text=text, **kw)
        except Exception:
            log.warning("could not notify admin %s", aid)


def user_pending(uid: int):
    for oid, info in pending_orders.items():
        if info["user_id"] == uid:
            return oid
    return None


def _cancel_timer(order_id: str):
    t = _timers.pop(order_id, None)
    if t and not t.done():
        t.cancel()


def release_order(order_id: str):
    """Remove a pending order, free the reserved ID, release any promo hold and stop its timer."""
    info = pending_orders.pop(order_id, None)
    _last_seen.pop(order_id, None)
    _polling.discard(order_id)
    _cancel_timer(order_id)
    promo_release(order_id)
    if info and info.get("post_id") and reserved.get(info["post_id"]) == order_id:
        reserved.pop(info["post_id"], None)
    app_save()
    return info


# ─── MESSAGE LIFECYCLE ────────────────────────────────────────────────────────
# Only bot messages sent in a user's own private chat for the buying/selling flow are tracked here.
# The welcome/menu message, receipts, credentials and admin panels are never tracked, so they are never
# cleaned up. Cleanup only ever touches one user's own tracked messages.
def track_msg(uid: int, chat_id: int, msg_id: int):
    lst = flow_msgs.setdefault(uid, [])
    if (chat_id, msg_id) not in lst:
        lst.append((chat_id, msg_id))
    if len(lst) > 40:
        del lst[:len(lst) - 40]


def untrack_msg(uid: int, msg_id: int):
    flow_msgs[uid] = [(c, m) for c, m in flow_msgs.get(uid, []) if m != msg_id]


async def cleanup_flow(bot, uid: int, keep: tuple = ()):
    for chat_id, mid in flow_msgs.pop(uid, []):
        if mid in keep or mid == welcome_msg.get(uid):
            continue
        await safe_delete(bot, chat_id, mid)


async def send_tracked(bot, uid: int, chat_id: int, text: str, **kw):
    msg = await bot.send_message(chat_id=chat_id, text=text, **kw)
    track_msg(uid, chat_id, msg.message_id)
    return msg


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup=None, track: bool = True):
    """Edit the clicked message (callback) or send a new one (reply keyboard). Always HTML."""
    q = update.callback_query
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if q and q.message:
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return q.message
        except tg_error.BadRequest as exc:
            if "not modified" in str(exc).lower():
                return q.message
        except Exception:
            pass
        if q.message.message_id != welcome_msg.get(uid):
            await safe_delete(context.bot, chat_id, q.message.message_id)
            untrack_msg(uid, q.message.message_id)
    msg = await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    if track:
        track_msg(uid, chat_id, msg.message_id)
    return msg


async def close_msg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.message and q.message.message_id != welcome_msg.get(q.from_user.id):
        await safe_delete(context.bot, q.message.chat_id, q.message.message_id)
        untrack_msg(q.from_user.id, q.message.message_id)


# ─── WELCOME / MAIN MENU ──────────────────────────────────────────────────────
async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    u   = update.effective_user
    uid = u.id
    who = html.escape(u.first_name or (f"@{u.username}" if u.username else "User"))
    name = html.escape(S().get("store_name") or BOT_NAME)
    text = (
        f"🎮 <b>WELCOME, {who}!</b>\n"
        f"TO [ <b>{name}</b> ] BOT\n\n"
        "Browse available Free Fire IDs, order securely and receive your credentials right here in this chat.\n\n"
        "🛒 <b>Buy FF ID</b> — browse the catalogue\n"
        "💸 <b>Sell Your ID</b> — sell your Free Fire account to us\n"
        "👤 <b>My Profile</b> — your account details\n"
        "💰 <b>Wallet</b> — balance and top-up\n"
        "🧾 <b>My Orders</b> — your receipts\n"
    )
    if is_admin(uid):
        text += "⚙️ <b>Admin Panel</b> — manage posts, orders and users\n"

    reply_kb = main_reply_kb(uid)
    chat_id = update.effective_chat.id

    if edit and update.callback_query:
        # Edit the existing inline message and re-show the reply keyboard
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")
            welcome_msg[uid] = update.callback_query.message.message_id
        except tg_error.BadRequest:
            m = await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_kb)
            welcome_msg[uid] = m.message_id
        await send_tracked(context.bot, uid, chat_id, "👇 Choose an option below:", reply_markup=reply_kb)
        return

    old = welcome_msg.get(uid)
    try:
        m = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML",
            reply_markup=reply_kb, message_effect_id=MESSAGE_EFFECT_ID,
        )
    except Exception:
        m = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_kb)
    welcome_msg[uid] = m.message_id
    if old and old != m.message_id and (update.message and (update.message.text or "").startswith("/start")):
        await safe_delete(context.bot, chat_id, old)      # a fresh /start replaces the previous menu message


# ─── PROFILE / WALLET / MY ORDERS ─────────────────────────────────────────────
def user_stats(uid: int):
    invs = [i for i in appdata.get("invoices", {}).values()
            if i["uid"] == uid and i.get("type", "purchase") == "purchase"]
    return len(invs), round(sum(float(i.get("paid", 0)) for i in invs), 2)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    rec = urec(u.id)
    n_orders, spent = user_stats(u.id)
    log_event(u.id, "profile")
    uname = f"@{html.escape(u.username)}" if u.username else "—"
    link = f"https://t.me/{context.bot.username}?start=ref_{u.id}" if context.bot.username else "—"
    text = (
        "👤 <b>MY PROFILE</b>\n\n"
        f"📛 Name: <b>{html.escape(u.full_name or '—')}</b>\n"
        f"🔗 Username: {uname}\n"
        f"🆔 User ID: <code>{u.id}</code>\n"
        f"👑 Telegram Premium: <b>{'Yes' if getattr(u, 'is_premium', False) else 'No'}</b>\n"
        f"📅 Member since: {html.escape(fmt_dt(rec['joined']))}\n\n"
        f"💰 Balance: <b>₹{money(balance_of(u.id))}</b>\n"
        f"📦 Completed orders: <b>{n_orders}</b>\n"
        f"💸 Total spent: <b>₹{money(spent)}</b>\n"
        f"👥 Referrals: <b>{int(rec.get('refs', 0))}</b>\n\n"
        f"🔗 Your referral link:\n<code>{html.escape(link)}</code>"
    )
    await _reply(update, context, text, InlineKeyboardMarkup([[ibt("❌ Close", "close_msg")]]))


async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    log_event(uid, "wallet")
    last = [e for e in reversed(appdata.get("ledger", [])) if e["uid"] == uid][:5]
    hist = "\n".join(
        f"• {html.escape(fmt_dt(e['ts']))}: <b>{'+' if e['amount'] >= 0 else '−'}₹{money(abs(e['amount']))}</b> "
        f"({html.escape(e['type'])})" for e in last) or "No transactions yet."
    mt = float(S().get("min_topup", MIN_TOPUP))
    text = (
        "💰 <b>WALLET</b>\n\n"
        f"Available balance: <b>₹{money(balance_of(uid))}</b>\n\n"
        f"<b>Recent transactions</b>\n{hist}\n\n"
        f"Add balance via UPI (minimum ₹{money(mt)}). Balance is credited only after the payment is verified."
    )
    presets = [n for n in TOPUP_PRESETS if n >= mt]
    rows, row = [], []
    for n in presets:
        row.append(ibt(f"💰 Add ₹{n}", f"topup_{n}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([ibt("✏️ Custom Amount", "topup_custom")])
    rows.append([ibt("❌ Close", "close_msg")])
    await _reply(update, context, text, InlineKeyboardMarkup(rows))


async def show_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
    log_event(uid, "my_orders")
    invs = sorted((i for i in appdata.get("invoices", {}).values() if i["uid"] == uid),
                  key=lambda i: i["ts"], reverse=True)[:10]
    if not invs:
        await _reply(update, context, "🧾 <b>MY ORDERS</b>\n\nYou have no completed payments yet.",
                     InlineKeyboardMarkup([[ibt("❌ Close", "close_msg")]]))
        return
    rows = [[ibt(f"🧾 {i['number'][-9:]} · ₹{money(i.get('paid', 0))}", f"inv_{i['order_id']}")] for i in invs]
    rows.append([ibt("❌ Close", "close_msg")])
    await _reply(update, context, "🧾 <b>MY ORDERS</b>\n\nTap a receipt to view it again.", InlineKeyboardMarkup(rows))


async def inv_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    inv = appdata.get("invoices", {}).get(q.data[len("inv_"):])
    if not inv or (inv["uid"] != q.from_user.id and not is_admin(q.from_user.id)):
        await q.answer("Receipt not found.", show_alert=True)
        return
    await q.answer()
    await context.bot.send_message(q.message.chat_id, render_invoice(inv), parse_mode="HTML")   # not tracked: kept


# ─── START ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state.pop(uid, None)
    checkout.pop(uid, None)
    sell_draft.pop(uid, None)
    await cleanup_flow(context.bot, uid)
    log_event(uid, "start")
    if await fj_gate(update, context):
        return
    await send_main_menu(update, context)
    arg = (context.args[0] if context.args else "")
    if arg.startswith("buy_"):                      # deep link from the inventory channel
        await open_listing(update, context, arg[4:])


async def group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = context.bot.username
    if not username or not update.message:
        return
    await update.message.reply_text(
        "🛒 Open the bot in private chat to buy a Free Fire ID 👇",
        reply_markup=InlineKeyboardMarkup(
            [[ibt_url("Open Bot", f"https://t.me/{username}")]]
        ),
    )

# ─── HTTP / FAMGATEWAY ────────────────────────────────────────────────────────
def http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; FFStoreBot/1.0)",
                "Cache-Control": "no-cache",
            },
            timeout=aiohttp.ClientTimeout(total=12),
        )
    return _http


async def _fetch(url: str, params: dict, headers: dict | None = None):
    try:
        async with http().get(url, params=params, headers=headers) as resp:
            body = await resp.text()
            try:
                return json.loads(body)
            except ValueError:
                log.warning("non-JSON from %s (HTTP %s): %.200s", url, resp.status, body)
                return None
    except Exception as e:
        log.warning("request failed %s: %r", url, e)
        return None


_PAID_WORDS = {
    "paid", "success", "successful", "completed", "received",
    "captured", "verified", "confirmed", "payment received", "payment successful",
}


def _is_paid(resp) -> bool:
    """Strict payment detection — explicit boolean flag OR explicit status word only."""
    if not isinstance(resp, dict):
        return False
    for key in ("paid", "is_paid", "isPaid", "payment_received", "paymentReceived", "payment_verified"):
        if resp.get(key) is True:
            return True
    for key in (
        "payment_status", "paymentStatus", "status", "verification",
        "order_status", "orderStatus", "paymentState", "state",
        "txn_status", "transaction_status",
    ):
        if str(resp.get(key, "")).strip().lower() in _PAID_WORDS:
            return True
    for nest in ("data", "order", "result", "response", "payment"):
        nv = resp.get(nest)
        if isinstance(nv, dict) and _is_paid(nv):
            return True
        if isinstance(nv, list):
            for item in nv:
                if isinstance(item, dict) and _is_paid(item):
                    return True
    return False


def _is_expired(resp) -> bool:
    if not isinstance(resp, dict):
        return False
    for src in (resp, resp.get("data") if isinstance(resp.get("data"), dict) else {}):
        for key in ("status", "payment_status", "order_status", "state"):
            v = str(src.get(key, "")).strip().lower()
            if v in ("expired", "failed", "cancelled", "canceled", "timeout"):
                return True
    return False


def _extract_field(resp, *field_names) -> str | None:
    if not isinstance(resp, dict):
        return None
    for key in field_names:
        v = resp.get(key)
        if v and not isinstance(v, (dict, list)):
            return str(v)
    inner = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    for key in field_names:
        v = inner.get(key)
        if v and not isinstance(v, (dict, list)):
            return str(v)
    return None


async def fg_status(order_id: str):
    """Check both FamGateway endpoints.

    Returns ("success", {"utr", "sender"}) | ("expired", {}) | ("pending", {}).

    FIX: UTR is no longer required to confirm a paid order.
    If both auth AND pub agree it's paid, we deliver.
    If only one says paid without UTR, we still deliver but log a warning.
    """
    ts = str(int(time.time() * 1000))
    pub, auth = await asyncio.gather(
        _fetch(f"{FG_BASE_URL}/checkout-status.php", {"order_id": order_id, "_": ts}),
        _fetch(
            f"{FG_BASE_URL}/verify-order.php",
            {"order_id": order_id, "_": ts},
            {"X-Api-Key": FG_API_KEY},
        ),
    )

    # De-dup logging
    seen = (
        json.dumps(pub,  sort_keys=True) if pub  else "",
        json.dumps(auth, sort_keys=True) if auth else "",
    )
    if _last_seen.get(order_id) != seen:
        _last_seen[order_id] = seen
        for name, raw in [("checkout-status", pub), ("verify-order", auth)]:
            shown = json.dumps(raw)[:400] if raw is not None else "no/invalid response"
            log.info("order %s | %s | %s", order_id, name, shown)

    # ── Success check ──
    # verify-order (auth) is authoritative — check first
    for resp in (auth, pub):
        if resp and _is_paid(resp):
            utr    = _extract_field(resp, "utr", "rrn", "transaction_id", "txn_id")
            sender = _extract_field(resp, "sender_name", "payer_name", "customer_name", "sender") or "N/A"

            if not utr:
                # FamGateway sometimes confirms payment without including UTR.
                # We still deliver — mark UTR as N/A and warn admin.
                log.warning(
                    "order %s PAID but no UTR in response — delivering anyway | resp: %s",
                    order_id, json.dumps(resp)[:300]
                )
            return "success", {"utr": utr or "N/A", "sender": sender}

    # ── Expired / failed check ──
    for resp in (pub, auth):
        if resp and _is_expired(resp):
            return "expired", {}

    return "pending", {}


async def fg_create_order(payload: dict) -> dict:
    """POST /create-order with retry."""
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            async with http().post(
                f"{FG_BASE_URL}/create-order",
                json=payload,
                headers={"X-Api-Key": FG_API_KEY},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                return await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last_err = e
            log.warning("create-order attempt %s failed: %r", attempt, e)
            await asyncio.sleep(1)
    raise last_err  # type: ignore[misc]


def make_branded_qr(upi_link: str) -> bytes | None:
    try:
        if not upi_link or not os.path.exists(LOGO_PATH):
            return None
        import qrcode
        from PIL import Image, ImageDraw

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=12, border=4)
        qr.add_data(upi_link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")

        logo = Image.open(LOGO_PATH).convert("RGBA")
        target = int(img.width * LOGO_RATIO)
        logo.thumbnail((target, target), Image.LANCZOS)

        pad = max(6, target // 10)
        badge = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad), (0, 0, 0, 0))
        ImageDraw.Draw(badge).rounded_rectangle(
            (0, 0, badge.width - 1, badge.height - 1), radius=pad, fill=(255, 255, 255, 255)
        )
        badge.alpha_composite(logo, (pad, pad))
        img.alpha_composite(badge, ((img.width - badge.width) // 2, (img.height - badge.height) // 2))

        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG")
        return out.getvalue()
    except Exception:
        log.exception("branded QR failed — using gateway QR")
        return None

# ─── ADMIN PANEL ──────────────────────────────────────────────────────────────
def post_ready(pid: str) -> bool:
    """A listing is purchasable only when it has credentials and is not sold/draft/locked."""
    p = posts.get(pid)
    return bool(p and p.get("creds") and not p.get("creds_locked") and p.get("status", "live") == "live"
                and pid not in sold)


def available_ids() -> list:
    ids = [pid for pid in posts if post_ready(pid) and pid not in reserved]
    ids.sort(key=lambda x: (posts[x].get("created", 0), x))
    return ids


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Works from both inline callback AND reply-keyboard text trigger."""
    query = update.callback_query
    uid   = update.effective_user.id

    if not is_admin(uid):
        if query:
            await query.answer("No access.", show_alert=True)
        return

    if query:
        await query.answer()
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)

    n_reserved = len([p for p in posts if p in reserved])
    n_avail    = len(available_ids())
    n_draft    = len([p for p in posts.values() if p.get("status") == "draft"])
    n_subs     = len([s for s in appdata.get("submissions", {}).values() if s["status"] in ("pending", "quoted", "accepted")])
    text = (
        "⚙️ <b>Admin Panel</b>\n\n"
        f"🟢 Available: <b>{n_avail}</b>\n"
        f"🟡 Payment in progress: <b>{n_reserved}</b>\n"
        f"✅ Sold: <b>{len(sold)}</b>\n"
        f"📝 Drafts (no credentials yet): <b>{n_draft}</b>\n"
        f"📢 Force Join: <b>{len(force_join)}</b>\n"
        f"👥 Users: <b>{len(users)}</b>\n"
        f"💼 Open seller submissions: <b>{n_subs}</b>\n\n"
        "What would you like to do?"
    )
    buttons = [
        [_inline_btn("➕ Add New Post", _sc=False, callback_data="admin_add")],
        [_inline_btn("📋 View All Posts", _sc=False, callback_data="admin_view")],
        [_inline_btn("✏️ Edit Post", _sc=False, callback_data="admin_edit_list")],
        [_inline_btn("🗑️ Delete Post", _sc=False, callback_data="admin_delete_list")],
        [_inline_btn("📦 Pending Orders", _sc=False, callback_data="admin_orders")],
        [_inline_btn("📢 Force Join", _sc=False, callback_data="admin_fj")],
        [_inline_btn("👥 Manage Users", _sc=False, callback_data="admin_users")],
        [_inline_btn("📣 Broadcast", _sc=False, callback_data="adm_bc")],
        [_inline_btn("🏷️ Promo Codes", _sc=False, callback_data="adm_promo")],
        [_inline_btn("💼 Seller Submissions", _sc=False, callback_data="adm_subs")],
    ]
    if is_owner(uid):
        buttons += [
            [_inline_btn("📡 Owner Channel", _sc=False, callback_data="adm_ch")],
            [_inline_btn("💾 Backup & Restore", _sc=False, callback_data="adm_bk")],
            [_inline_btn("🔄 Updates", _sc=False, callback_data="adm_upd")],
            [_inline_btn("⚙️ Settings", _sc=False, callback_data="adm_set")],
        ]
    buttons.append([_inline_btn("🔙 Back", _sc=False, callback_data="main_menu")])
    markup = InlineKeyboardMarkup(buttons)

    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ─── USERS / BAN / SUPER ADMIN ────────────────────────────────────────────────
def _uname(rec: dict) -> str:
    return f"@{rec['username']}" if rec.get("username") else (rec.get("name") or "User")


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves the user on every private update and blocks banned users."""
    u = update.effective_user
    if not u or u.is_bot:
        return
    name, uname = u.full_name or "", u.username or ""
    rec = users.get(u.id)
    if rec is None:
        rec = users[u.id] = {}
        urec(u.id).update({"name": name, "username": uname})
        txt = (update.message.text or "") if update.message else ""
        m = re.match(r"^/start\s+ref_(\d+)$", txt.strip())
        if m:                                         # referral is recorded once, for brand-new users only
            rid = int(m.group(1))
            if rid != u.id and rid in users:
                users[u.id]["referrer"] = rid
                urec(rid)["refs"] = int(urec(rid).get("refs", 0)) + 1
        _data_save()
    else:
        urec(u.id)
        if rec.get("name") != name or rec.get("username") != uname:
            rec["name"], rec["username"] = name, uname
            _data_save()
        rec["last_seen"] = time.time()
        mark_dirty()
    if users[u.id].get("banned") and not is_admin(u.id):
        try:
            if update.callback_query:
                await update.callback_query.answer("🚫 You have been banned.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🚫 You have been banned from this bot.")
        except Exception:
            pass
        raise ApplicationHandlerStop


USERS_PER_PAGE = 6


async def _render_users(q, page: int):
    items = list(users.items())[::-1]            # newest users first
    pages = max(1, -(-len(items) // USERS_PER_PAGE))
    page  = max(0, min(page, pages - 1))
    chunk = items[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]
    n_ban = sum(1 for _, r in items if r.get("banned"))
    lines = []
    for i, (uid, r) in enumerate(chunk, page * USERS_PER_PAGE + 1):
        tag = " 👑" if (uid == ADMIN_ID or uid in super_admins) else (" 🚫" if r.get("banned") else "")
        lines.append(f"{i}. {html.escape(_uname(r))} — <code>{uid}</code>{tag}")
    text = (
        "👥 <b>Manage Users</b>\n\n"
        f"Total: <b>{len(items)}</b> | Banned: <b>{n_ban}</b> | Page <b>{page + 1}/{pages}</b>\n\n"
        + ("\n".join(lines) if lines else "No users yet.")
    )
    rows = []
    for uid, r in chunk:
        if uid == ADMIN_ID or uid in super_admins:
            continue
        short = re.sub(r"\s+", " ", _uname(r))[:18]
        if r.get("banned"):
            rows.append([_inline_btn(f"✅ Unban {short}", _sc=False, _style="success",
                                     callback_data=f"usr_t_{uid}_{page}")])
        else:
            rows.append([_inline_btn(f"🚫 Ban {short}", _sc=False, _style="danger",
                                     callback_data=f"usr_t_{uid}_{page}")])
    nav = []
    if page > 0:
        nav.append(_inline_btn("Prev", _sc=False, _style="primary", callback_data=f"usr_p_{page - 1}"))
    if page < pages - 1:
        nav.append(_inline_btn("Next", _sc=False, _style="primary", callback_data=f"usr_p_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_inline_btn("🚫 Ban by ID", _sc=False, _style="danger", callback_data="usr_banid"),
                 _inline_btn("✅ Unban by ID", _sc=False, _style="success", callback_data="usr_unbanid")])
    if is_owner(q.from_user.id):
        rows.append([_inline_btn("👑 Super Admins", _sc=False, _style="primary", callback_data="sa_menu")])
    rows.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state.pop(q.from_user.id, None)
    m = re.match(r"usr_p_(\d+)$", q.data or "")
    await _render_users(q, int(m.group(1)) if m else 0)


async def usr_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    _, _, tid, page = q.data.split("_")
    tid = int(tid)
    if tid == ADMIN_ID or tid in super_admins:
        await q.answer("You cannot ban an admin.", show_alert=True)
        return
    rec = urec(tid)
    rec["banned"] = not rec.get("banned")
    _data_save()
    await q.answer("🚫 Banned" if rec["banned"] else "✅ Unbanned")
    await _render_users(q, int(page))


async def usr_id_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    ban = q.data == "usr_banid"
    user_state[uid] = ADMIN_BAN_ID if ban else ADMIN_UNBAN_ID
    await q.edit_message_text(
        ("🚫 <b>Ban by ID</b>" if ban else "✅ <b>Unban by ID</b>") + "\n\nSend the user's <b>chat ID</b> (numbers only).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_users")),
    )


async def usr_id_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state.get(uid)
    if not is_admin(uid) or state not in (ADMIN_BAN_ID, ADMIN_UNBAN_ID):
        return
    raw = (update.message.text or "").strip()
    if not re.fullmatch(r"\d{5,}", raw):
        await update.message.reply_text("❌ Send only the numeric chat ID (e.g. 123456789).",
                                        reply_markup=InlineKeyboardMarkup(back_button("admin_users")))
        return
    tid, ban = int(raw), state == ADMIN_BAN_ID
    if tid == ADMIN_ID or tid in super_admins:
        await update.message.reply_text("❌ You cannot ban an admin.",
                                        reply_markup=InlineKeyboardMarkup(back_button("admin_users")))
        return
    rec = urec(tid)
    rec["banned"] = ban
    _data_save()
    user_state.pop(uid, None)
    await update.message.reply_text(
        f"{'🚫 Banned' if ban else '✅ Unbanned'}: <code>{tid}</code> ({html.escape(_uname(rec))})",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_users")),
    )


async def _render_sa(q):
    lines = "\n".join(f"{i}. {html.escape(n or 'User')} — <code>{k}</code>"
                      for i, (k, n) in enumerate(super_admins.items(), 1)) or "None"
    text = (
        "👑 <b>Super Admins</b>\n\n"
        f"Owner: <code>{ADMIN_ID}</code>\n\n{lines}\n\n"
        "A super admin can manage posts, orders, users, promo codes, broadcasts and force join."
    )
    rows = []
    for k, n in super_admins.items():
        short = re.sub(r"\s+", " ", n or str(k))[:18]
        rows.append([_inline_btn(f"🗑️ {short}", _sc=False, _style="danger", callback_data=f"sa_del_{k}")])
    rows.append([_inline_btn("➕ Add Super Admin", _sc=False, callback_data="sa_add")])
    rows.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_users")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def sa_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    user_state.pop(q.from_user.id, None)
    await _render_sa(q)


async def sa_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    user_state[q.from_user.id] = ADMIN_SA_ADD
    await q.edit_message_text(
        "➕ <b>Add Super Admin</b>\n\nSend the <b>chat ID</b> of the new super admin (numbers only).\n"
        "They should send /start to the bot first.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("sa_menu")),
    )


async def sa_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or user_state.get(uid) != ADMIN_SA_ADD:
        return
    raw = (update.message.text or "").strip()
    if not re.fullmatch(r"\d{5,}", raw):
        await update.message.reply_text("❌ Send only the numeric chat ID.",
                                        reply_markup=InlineKeyboardMarkup(back_button("sa_menu")))
        return
    tid = int(raw)
    if tid == ADMIN_ID or tid in super_admins:
        await update.message.reply_text("ℹ️ This user is already an admin.",
                                        reply_markup=InlineKeyboardMarkup(back_button("sa_menu")))
        return
    name = (users.get(tid) or {}).get("name") or ""
    if not name:
        try:
            c = await context.bot.get_chat(tid)
            name = c.full_name or c.first_name or ""
        except Exception:
            pass
    super_admins[tid] = name or str(tid)
    if tid in users:
        users[tid]["banned"] = False
    _data_save()
    user_state.pop(uid, None)
    try:
        await context.bot.send_message(tid, "👑 You have been made a Super Admin of this bot. Send /start.")
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Super Admin added: <code>{tid}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("sa_menu")),
    )


async def sa_del_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return
    super_admins.pop(int(q.data.split("_")[-1]), None)
    _data_save()
    await q.answer("🗑️ Removed")
    await _render_sa(q)


# ─── FORCE JOIN ───────────────────────────────────────────────────────────────
def _fj_load():
    d = _read_json(FJ_FILE)
    if isinstance(d, list):
        force_join[:] = [e for e in d if isinstance(e, dict) and "id" in e and "link" in e]


def _fj_save():
    try:
        _atomic_write(FJ_FILE, _dump(force_join))
    except Exception:
        log.exception("force-join save failed")


def _clean_md(t: str) -> str:
    return re.sub(r"[*_`\[\]]", " ", str(t)).strip()


async def _fj_resolve(bot, raw: str) -> dict:
    """@username / t.me link / -100 ID -> {"id","title","link"}. Raises ValueError on problems."""
    raw = raw.strip()
    m = re.match(r"^(?:https?://)?t\.me/(?!\+|joinchat)([A-Za-z0-9_]{4,})/?$", raw)
    if m:
        ident = "@" + m.group(1)
    elif raw.startswith("@") and len(raw) > 4:
        ident = raw
    elif re.fullmatch(r"-?\d{6,}", raw):
        ident = int(raw)
    else:
        raise ValueError(f"{raw} — invalid format. Send @username, a t.me link or a -100… ID.")
    try:
        chat = await bot.get_chat(ident)
    except Exception:
        raise ValueError(f"{raw} — chat not found. Make the bot an admin there first.")
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        is_adm = me.status in ("administrator", "creator")
    except Exception:
        is_adm = False
    if not is_adm:
        raise ValueError(f"{raw} — the bot is not an admin there. Make it admin, then send again.")
    link = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except Exception:
            raise ValueError(f"{raw} — could not create an invite link. Give the bot the 'Invite users' permission.")
    return {"id": chat.id, "title": chat.title or str(chat.id), "link": link}


async def fj_missing(bot, uid: int) -> list:
    """Force-join chats the user is not a member of yet."""
    missing = []
    for e in force_join:
        try:
            mem = await bot.get_chat_member(e["id"], uid)
            if mem.status in ("left", "kicked") or (
                mem.status == "restricted" and not getattr(mem, "is_member", True)
            ):
                missing.append(e)
        except Exception as exc:  # bot removed / no permission -> never block the user
            log.warning("force-join check failed for %s: %s", e.get("id"), exc)
    return missing


async def fj_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True = user blocked (join message sent). Admins always pass."""
    uid = update.effective_user.id
    if is_admin(uid) or not force_join:
        return False
    missing = await fj_missing(context.bot, uid)
    if not missing:
        return False
    rows = [[_inline_btn(f"📢 {_clean_md(e['title'])}", _sc=False, _style="primary", url=e["link"])]
            for e in missing]
    rows.append([_inline_btn("✅ Joined — Check", _sc=False, _style="success", callback_data="fj_check")])
    text = (
        "📢 <b>Join first</b>\n\n"
        "To use this bot, join the channels/groups below, then tap <b>Joined — Check</b>."
    )
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await context.bot.send_message(update.effective_chat.id, text,
                                           parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    return True


async def fj_check_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    missing = [] if is_admin(uid) else await fj_missing(context.bot, uid)
    if missing:
        await q.answer("❌ You have not joined everything yet!", show_alert=True)
        return
    await q.answer("✅ Verified!")
    await send_main_menu(update, context, edit=True)


async def admin_fj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = update.effective_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state.pop(uid, None)
    lines = "\n".join(f"{i}. {html.escape(_clean_md(e['title']))}" for i, e in enumerate(force_join, 1)) or "<i>None</i>"
    text = (
        "📢 <b>Force Join</b>\n\n"
        f"Total: <b>{len(force_join)}</b>\n\n{lines}\n\n"
        "Tap 🗑️ to remove one."
    )
    rows = [[_inline_btn(f"🗑️ {_clean_md(e['title'])}", _sc=False, _style="danger",
                         callback_data=f"fjdel_{e['id']}")] for e in force_join]
    rows.append([_inline_btn("➕ Add Channel/Group", _sc=False, callback_data="fj_add")])
    rows.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def fj_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = update.effective_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = ADMIN_FJ_ADD
    await q.edit_message_text(
        "➕ <b>Add Channel/Group</b>\n\n"
        "First make the bot an <b>admin</b> in that channel/group.\n\n"
        "Then send (one or more, separated by space/new line):\n"
        "• <code>@username</code>\n• <code>https://t.me/username</code>\n• <code>-1001234567890</code> (ID, for private chats)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_fj")),
    )


async def fj_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_FJ_ADD:
        return
    tokens = [t for t in re.split(r"[\s,]+", (update.message.text or "").strip()) if t]
    added, failed = [], []
    for tok in tokens:
        try:
            e = await _fj_resolve(context.bot, tok)
        except ValueError as exc:
            failed.append(str(exc))
            continue
        if any(x["id"] == e["id"] for x in force_join):
            failed.append(f"{_clean_md(e['title'])} — already added")
            continue
        force_join.append(e)
        added.append(_clean_md(e["title"]))
    _fj_save()
    text = ""
    if added:
        text += "✅ <b>Added:</b>\n" + "\n".join(f"• {html.escape(t)}" for t in added) + "\n\n"
    if failed:
        text += "❌ <b>Failed:</b>\n" + "\n".join(f"• {html.escape(_clean_md(t))}" for t in failed) + "\n\n"
    if added:
        user_state.pop(uid, None)
    else:
        text += "Send again or tap Back."
    await update.message.reply_text(
        text.strip() or "Nothing found — send a @username or link.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_fj")),
    )


async def fj_del_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    cid = q.data.split("_", 1)[1]
    force_join[:] = [e for e in force_join if str(e["id"]) != cid]
    _fj_save()
    await admin_fj(update, context)


# ─── ADMIN: PENDING ORDERS LIST ───────────────────────────────────────────────
async def admin_orders_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    if not pending_orders:
        await query.edit_message_text(
            "📭 There are no pending orders.",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
        )
        return

    lines = []
    for oid, info in pending_orders.items():
        what = f"Post: <code>{html.escape(str(info.get('post_id')))}</code>" if info.get("kind", "buy") == "buy" \
            else f"Top-up ₹{money(info.get('amount', 0))}"
        lines.append(f"• <code>{html.escape(oid)}</code>\n  {what} | User: <code>{info['user_id']}</code>")
    text = "📦 <b>Pending Orders:</b>\n\n" + "\n\n".join(lines[:20])
    if len(pending_orders) > 20:
        text += f"\n…and {len(pending_orders) - 20} more"
    text += "\n\n<i>Use /checkorder &lt;order_id&gt; to force-verify.</i>"

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
    )


# ─── ADMIN: COMMANDS ──────────────────────────────────────────────────────────
async def cmd_checkorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /checkorder <order_id> — manually trigger a payment check for a pending order."""
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/checkorder &lt;order_id&gt;</code>\n\nActive orders:\n" +
            ("\n".join(f"<code>{html.escape(oid)}</code>" for oid in list(pending_orders)[:20]) or "<i>none</i>"),
            parse_mode="HTML",
        )
        return

    order_id = args[0].strip()
    if order_id not in pending_orders:
        await update.message.reply_text(
            f"❌ Order <code>{html.escape(order_id)}</code> is not in the pending list.\n"
            "It was already delivered, expired or cancelled.",
            parse_mode="HTML",
        )
        return

    msg = await update.message.reply_text(f"🔍 Checking order <code>{html.escape(order_id)}</code>…", parse_mode="HTML")
    try:
        status, data = await fg_status(order_id)
    except Exception as e:
        await msg.edit_text(f"❌ FamGateway error: <code>{html.escape(repr(e))}</code>", parse_mode="HTML")
        return

    if status == "success":
        await msg.edit_text(
            f"✅ Payment confirmed! Delivering order <code>{html.escape(order_id)}</code>…\n"
            f"UTR: <code>{html.escape(str(data.get('utr', 'N/A')))}</code>",
            parse_mode="HTML",
        )
        await deliver_order(order_id, data, context)
    elif status == "expired":
        await msg.edit_text(f"⏰ Order <code>{html.escape(order_id)}</code> expired.", parse_mode="HTML")
        await expire_order(order_id, context)
    else:
        info = pending_orders.get(order_id, {})
        await msg.edit_text(
            f"⏳ Order <code>{html.escape(order_id)}</code> is still pending.\n"
            f"Post: <code>{html.escape(str(info.get('post_id')))}</code> | User: <code>{info.get('user_id')}</code>\n\n"
            "<i>Try again in a moment, or wait for the poller.</i>",
            parse_mode="HTML",
        )


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /orders — list all pending orders."""
    if not is_admin(update.effective_user.id):
        return

    if not pending_orders:
        await update.message.reply_text("📭 There are no pending orders.")
        return

    lines = [f"<code>{html.escape(oid)}</code> | {html.escape(str(info.get('post_id') or 'top-up'))} | uid=<code>{info['user_id']}</code>"
             for oid, info in list(pending_orders.items())[:30]]
    await update.message.reply_text("📦 <b>Pending Orders:</b>\n\n" + "\n".join(lines), parse_mode="HTML")


async def _bal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int):
    """Owner only: /addbal <user_id> <amount> [note]  |  /subbal <user_id> <amount> [note]"""
    if not is_owner(update.effective_user.id):
        return
    a = context.args or []
    try:
        tid, amt = int(a[0]), round(float(a[1]), 2)
        if amt <= 0 or tid not in users:
            raise ValueError
    except Exception:
        await update.message.reply_text("Usage: /addbal <user_id> <amount> [note]\n(the user must have started the bot)")
        return
    note = " ".join(a[2:])[:100] or "admin adjustment"
    try:
        e = ledger_add(tid, "admin_adjust", sign * amt, ref=f"adj-{int(time.time() * 1000)}", note=note)
    except InsufficientBalance:
        await update.message.reply_text("❌ The user's balance is lower than that amount.")
        return
    app_save()
    await update.message.reply_text(
        f"✅ {e['id']}: {'+' if sign > 0 else '−'}₹{money(amt)} for <code>{tid}</code>. New balance ₹{money(e['balance_after'])}.",
        parse_mode="HTML")


async def cmd_addbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _bal_cmd(update, context, +1)


async def cmd_subbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _bal_cmd(update, context, -1)


async def cmd_seticon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: /seticon <button>  then send ONE message containing the premium emoji you want.
    Buttons: delete_post, pending_orders, force_join, manage_users, broadcast, promo_codes, ..."""
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    key = (context.args[0].lower() if context.args else "")
    if key not in _BUTTON_GLYPHS:
        cur = "\n".join(f"• {k}: {_FORCE_REPORT[k][1]} {'(animated)' if _FORCE_REPORT[k][2] else '(static)'} [{_FORCE_REPORT[k][3]}]"
                        for k in _BUTTON_GLYPHS if k in _FORCE_REPORT)
        await update.message.reply_text(
            "Usage: /seticon <button>\nButtons: " + ", ".join(_BUTTON_GLYPHS) +
            "\n\nThen send one message that contains the premium emoji you want on that button.\n\nCurrent icons:\n" + (cur or "none resolved"))
        return
    user_state[uid] = ADMIN_ICON_WAIT
    admin_temp[uid] = {"icon_key": key}
    await update.message.reply_text(f"Send one message containing the premium emoji for “{key}”.")


async def icon_wait_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    key = (admin_temp.get(uid) or {}).get("icon_key")
    ents = [e for e in (update.message.entities or []) if e.type == "custom_emoji" and e.custom_emoji_id]
    if not ents or not key:
        await update.message.reply_text("I did not find a premium emoji in that message. Send one, or use /start to cancel.")
        return
    cid = ents[0].custom_emoji_id
    try:
        st = (await context.bot.get_custom_emoji_stickers([cid]))[0]
    except Exception:
        await update.message.reply_text("Telegram does not recognise that emoji ID.")
        return
    S().setdefault("btn_icons", {})[key] = cid
    _EXTRA_ICON_IDS.add(cid)
    _VALID_IDS.add(cid)
    _INVALID_IDS.discard(cid)
    _STICKER_INFO[cid] = ((st.emoji or "").replace("\ufe0f", ""), bool(getattr(st, "is_animated", False) or getattr(st, "is_video", False)))
    _resolve_forced_icons()
    app_save()
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    await update.message.reply_text(
        f"✅ Icon saved for “{key}” ({'animated' if _STICKER_INFO[cid][1] else 'static'}). Open the Admin Panel to see it.")

# ─── ADMIN: ADD POST ──────────────────────────────────────────────────────────
POST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def gen_post_id() -> str:
    """A unique listing ID that cannot clash with existing posts, sold IDs, orders or pending orders."""
    used = set(posts) | set(sold) | {i.get("post_id") for i in appdata.get("invoices", {}).values()}
    while True:
        appdata["post_seq"] = appdata.get("post_seq", 0) + 1
        pid = f"FF{appdata['post_seq']:04d}"
        if pid not in used:
            return pid


async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid):
        return

    user_state[uid] = ADMIN_WAIT_POST_ID
    admin_temp[uid] = {}

    await query.edit_message_text(
        "📝 <b>Enter the Post ID:</b>\n\n"
        "<i>(e.g. <code>FF001</code>, <code>ID123</code> — letters, numbers, - and _ only, max 32)</i>\n\n"
        "👇 Type it, or let the bot generate one:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[ibt("🎲 Auto-generate ID", "admin_autoid")]] + back_button("admin_panel")),
    )


async def admin_autoid_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_POST_ID:
        await q.answer()
        return
    await q.answer()
    await _ask_price(context, uid, q.message.chat_id, gen_post_id(), edit=q.message)


async def _ask_price(context, uid, chat_id, post_id, edit=None):
    admin_temp.setdefault(uid, {})["post_id"] = post_id
    user_state[uid] = ADMIN_WAIT_PRICE
    text = (f"✅ Post ID: <code>{html.escape(post_id)}</code>\n\n"
            "💰 <b>Now enter the price (INR):</b>\n\n<i>(numbers only — e.g. <code>499</code> or <code>1500</code>)</i>")
    markup = InlineKeyboardMarkup(back_button("admin_panel"))
    if edit is not None:
        try:
            await edit.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


async def admin_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_POST_ID:
        return

    post_id = update.message.text.strip()
    if not POST_ID_RE.match(post_id):
        await update.message.reply_text("❌ Use only letters, numbers, - and _ (max 32 characters).")
        return
    if post_id in reserved:
        await update.message.reply_text(
            "⚠️ A payment is in progress for this Post ID. Try again later or use another ID."
        )
        return
    await _ask_price(context, uid, update.effective_chat.id, post_id)


async def admin_add_photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_PRICE:
        return

    try:
        price = float(update.message.text.strip())
        if price <= 0 or price > 1_000_000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send a number greater than 0! e.g. <code>499</code>", parse_mode="HTML")
        return

    admin_temp[uid]["price"] = price
    user_state[uid] = ADMIN_WAIT_PHOTO
    await update.message.reply_text(
        f"✅ Price: <code>₹{money(price)}</code>\n\n"
        "🖼️ <b>Send a photo of the ID</b> (shown on the catalogue card), or tap Skip.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[ibt("⏭️ Skip photo", "admin_skip_photo")]] + back_button("admin_panel")),
    )


async def admin_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo upload for add-post / edit-photo."""
    uid = update.effective_user.id
    if not is_admin(uid):
        return False
    st = user_state.get(uid)
    fid = update.message.photo[-1].file_id
    if st == ADMIN_WAIT_PHOTO:
        admin_temp[uid]["photo"] = fid
        await _ask_info(update.message, uid)
        return True
    if st == ADMIN_EDIT_VALUE and (admin_temp.get(uid) or {}).get("edit_field") == "photo":
        pid = admin_temp[uid].get("edit_post_id")
        if pid in posts:
            posts[pid]["photo"] = fid
            app_save()
            await channel_sync(context.bot, pid)
            user_state.pop(uid, None)
            admin_temp.pop(uid, None)
            await update.message.reply_text(
                f"✅ Photo of <code>{html.escape(pid)}</code> updated.", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[ibt("✏️ Edit Another", "admin_edit_list")], [ibt("⚙️ Admin Panel", "admin_panel")]]))
        return True
    return False


async def _ask_info(message, uid):
    user_state[uid] = ADMIN_WAIT_INFO
    await message.reply_text(
        "ℹ️ <b>Send a short description</b> shown on the card (level, rank, skins…), or tap Skip.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[ibt("⏭️ Skip description", "admin_skip_info")]] + back_button("admin_panel")),
    )


async def admin_skip_photo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_PHOTO:
        await q.answer()
        return
    await q.answer()
    admin_temp[uid]["photo"] = None
    await _ask_info(q.message, uid)


async def admin_info_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_INFO:
        return
    admin_temp[uid]["info"] = update.message.text.strip()[:300]
    await _ask_creds(update.message, uid)


async def admin_skip_info_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_INFO:
        await q.answer()
        return
    await q.answer()
    admin_temp[uid]["info"] = ""
    await _ask_creds(q.message, uid)


async def _ask_creds(message, uid):
    user_state[uid] = ADMIN_WAIT_CREDS
    await message.reply_text(
        "🔐 <b>Now paste the login credentials:</b>\n\n"
        "<i>(Any format — e.g. <code>Email: abc@gmail.com | Pass: xyz123</code>)</i>\n"
        "<i>(The buyer receives these after payment. They are stored encrypted.)</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
    )


async def admin_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_CREDS:
        return

    creds   = update.message.text.strip()
    data    = admin_temp.get(uid, {})
    post_id = data.get("post_id")
    price   = data.get("price")

    if not post_id or not price or not creds:
        await update.message.reply_text("❌ Something went wrong, please try again.")
        user_state.pop(uid, None)
        return

    if post_id in reserved:
        await update.message.reply_text(
            "⚠️ A payment is in progress for this ID, nothing was saved. Add it again later."
        )
        user_state.pop(uid, None)
        admin_temp.pop(uid, None)
        return

    existing = posts.get(post_id) or {}
    relisted = post_id in sold
    sold.discard(post_id)
    posts[post_id] = {
        "price": price, "creds": creds, "photo": data.get("photo") or existing.get("photo"),
        "info": data.get("info", existing.get("info", "")), "created": existing.get("created", time.time()),
        "ch_msg": existing.get("ch_msg"), "status": "live",
    }
    if data.get("from_submission"):
        posts[post_id]["from_submission"] = data["from_submission"]
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    app_save()
    ch_note = await channel_sync(context.bot, post_id)

    note = "\n♻️ <i>This ID was sold before — it is available again.</i>" if relisted else ""
    await update.message.reply_text(
        f"✅ <b>Post Added Successfully!</b>\n\n"
        f"📌 Post ID: <code>{html.escape(post_id)}</code>\n"
        f"💰 Price: <code>₹{money(price)}</code>\n"
        f"🔐 Credentials: <i>saved (encrypted)</i>{note}{ch_note}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [_inline_btn("➕ Add Another", _sc=False, callback_data="admin_add")],
            [_inline_btn("📋 View All Posts", _sc=False, callback_data="admin_view")],
            [_inline_btn("⚙️ Admin Panel", _sc=False, callback_data="admin_panel")],
        ]),
    )


# ─── ADMIN: VIEW POSTS ────────────────────────────────────────────────────────
async def admin_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    if not posts and not sold:
        await query.edit_message_text(
            "📭 <b>There are no posts yet.</b>\n\nAdd one first!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
        )
        return

    lines = []
    for pid, info in posts.items():
        tag = "📝" if info.get("status") == "draft" or not info.get("creds") else ("🟡" if pid in reserved else "🟢")
        lines.append(f"{tag} <code>{html.escape(pid)}</code> — ₹{money(info['price'])}")
    for pid in sold:
        lines.append(f"✅ <code>{html.escape(pid)}</code> — SOLD")

    text = "📋 <b>All Posts:</b>\n\n" + "\n".join(lines[:60])
    if len(lines) > 60:
        text += f"\n… and {len(lines) - 60} more"
    text += "\n\n🟢 available · 🟡 payment in progress · ✅ sold · 📝 draft (needs credentials)"

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
    )


# ─── ADMIN: DELETE POST ───────────────────────────────────────────────────────
async def admin_delete_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    if not posts:
        await query.edit_message_text(
            "📭 There are no posts.",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
        )
        return

    buttons = []
    for pid, info in list(posts.items())[:50]:
        tag = "🟡" if pid in reserved else "🗑️"
        buttons.append([InlineKeyboardButton(f"{tag} {pid} — ₹{money(info['price'])}", callback_data=f"del_{pid}")])
    buttons.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")])

    await query.edit_message_text(
        "🗑️ <b>Which post do you want to delete?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    post_id = query.data[len("del_"):]
    if post_id in reserved:
        text = f"⚠️ A payment is in progress for <code>{html.escape(post_id)}</code> — it cannot be deleted now."
    elif post_id in posts:
        await channel_remove(context.bot, post_id)
        del posts[post_id]
        app_save()
        text = f"✅ Post <code>{html.escape(post_id)}</code> deleted!"
    else:
        text = "❌ Post not found."

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [_inline_btn("🗑️ Delete Another", _sc=False, callback_data="admin_delete_list")],
            [_inline_btn("⚙️ Admin Panel", _sc=False, callback_data="admin_panel")],
        ]),
    )


# ─── ADMIN: EDIT POST ─────────────────────────────────────────────────────────
async def admin_edit_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    if not posts:
        await query.edit_message_text(
            "📭 There are no posts.",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
        )
        return

    buttons = []
    for pid, info in list(posts.items())[:50]:
        tag = "🟡" if pid in reserved else "✏️"
        buttons.append([InlineKeyboardButton(f"{tag} {pid} — ₹{money(info['price'])}", callback_data=f"edit_{pid}")])
    buttons.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")])

    await query.edit_message_text(
        "✏️ <b>Which post do you want to edit?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    if not is_admin(uid):
        await query.answer()
        return

    post_id = query.data[len("edit_"):]
    if post_id not in posts:
        await query.answer("Post not found!", show_alert=True)
        return
    await query.answer()

    admin_temp[uid] = {"edit_post_id": post_id}
    info = posts[post_id]
    await query.edit_message_text(
        f"✏️ <b>Edit Post:</b> <code>{html.escape(post_id)}</code>\n\n"
        f"💰 Current Price: ₹{money(info['price'])}\n"
        f"🖼️ Photo: {'yes' if info.get('photo') else 'none'}\n"
        f"ℹ️ Info: {html.escape(info.get('info') or '—')}\n"
        "🔐 Credentials: <i>stored</i>\n\n"
        "What do you want to change?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [_inline_btn("💰 Price Change", _sc=False, callback_data="editfield_price")],
            [_inline_btn("🖼️ Photo Change", _sc=False, callback_data="editfield_photo")],
            [_inline_btn("ℹ️ Info Change", _sc=False, callback_data="editfield_info")],
            [_inline_btn("🔐 Credentials Change", _sc=False, callback_data="editfield_creds")],
            [_inline_btn("🔙 Back", _sc=False, callback_data="admin_edit_list")],
        ]),
    )


async def admin_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid):
        return

    field = query.data[len("editfield_"):]
    admin_temp.setdefault(uid, {})["edit_field"] = field
    user_state[uid] = ADMIN_EDIT_VALUE

    field_name = {"price": "price (numbers only)", "photo": "photo (send an image)",
                  "info": "description", "creds": "credentials"}.get(field, field)
    await query.edit_message_text(
        f"✏️ Enter the new <b>{field_name}</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_edit_list")),
    )


async def admin_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_EDIT_VALUE:
        return

    value   = update.message.text.strip()
    data    = admin_temp.get(uid, {})
    post_id = data.get("edit_post_id")
    field   = data.get("edit_field")

    if not post_id or not field or post_id not in posts:
        await update.message.reply_text("❌ Error, please try again.")
        user_state.pop(uid, None)
        return
    if field == "photo":
        await update.message.reply_text("🖼️ Please send an image (not text).")
        return

    if field == "price":
        try:
            new_price = float(value)
            if new_price <= 0 or new_price > 1_000_000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Send a number greater than 0!")
            return
        posts[post_id]["price"] = new_price
    elif field == "info":
        posts[post_id]["info"] = value[:300]
    else:
        if not value:
            await update.message.reply_text("❌ Credentials cannot be empty.")
            return
        posts[post_id]["creds"] = value
        posts[post_id].pop("creds_locked", None)
        if posts[post_id].get("status") == "draft":
            posts[post_id]["status"] = "live"

    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    app_save()
    await channel_sync(context.bot, post_id)

    await update.message.reply_text(
        f"✅ Post <code>{html.escape(post_id)}</code> updated!\n\n💰 Price: ₹{money(posts[post_id]['price'])}\n",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [_inline_btn("✏️ Edit Another", _sc=False, callback_data="admin_edit_list")],
            [_inline_btn("⚙️ Admin Panel", _sc=False, callback_data="admin_panel")],
        ]),
    )


# ═══ CATALOGUE ════════════════════════════════════════════════════════════════
def _card_caption(pid: str, idx: int, total: int) -> str:
    p = posts[pid]
    info = html.escape(p.get("info") or "")
    return (
        "🎮 <b>FF ID LISTING</b>\n"
        f"📌 Post ID: <code>{html.escape(pid)}</code>\n"
        f"💰 Price: <b>₹{money(p['price'])}</b>\n"
        + (f"ℹ️ {info}\n" if info else "")
        + f"\n✅ Available · {idx + 1} of {total}"
    )


def _card_markup(pid: str, total: int) -> InlineKeyboardMarkup:
    rows = [[ibt("🛒 Buy ID", f"cat_buy_{pid}")]]
    if total > 1:
        rows.append([ibt("⬅️ Prev", f"cat_prev_{pid}"), ibt("➡️ Next", f"cat_next_{pid}")])
    rows.append([ibt("🔎 Enter Post ID", "cat_enter"), ibt("🏠 Menu", "cat_close")])
    return InlineKeyboardMarkup(rows)


async def _put_card(context, uid: int, chat_id: int, cur_msg, text: str, photo, markup):
    """Show a card by editing `cur_msg` in place when its type matches; otherwise delete + resend.
    Returns the message that now carries the card."""
    bot = context.bot
    cur_is_photo = bool(cur_msg is not None and getattr(cur_msg, "photo", None))
    if cur_msg is not None and cur_msg.message_id != welcome_msg.get(uid):
        try:
            if photo and cur_is_photo:
                cap, pm = _premium_text(text, "HTML")
                await bot.edit_message_media(chat_id=chat_id, message_id=cur_msg.message_id,
                                             media=InputMediaPhoto(photo, caption=cap, parse_mode=pm), reply_markup=markup)
                return cur_msg
            if not photo and not cur_is_photo:
                await bot.edit_message_text(chat_id=chat_id, message_id=cur_msg.message_id, text=text,
                                            parse_mode="HTML", reply_markup=markup)
                return cur_msg
        except tg_error.BadRequest as exc:
            if "not modified" in str(exc).lower():
                return cur_msg
            log.info("card edit failed (%s) — resending", exc)
        except Exception:
            log.exception("card edit failed — resending")
        await safe_delete(bot, chat_id, cur_msg.message_id)
        untrack_msg(uid, cur_msg.message_id)
    if photo:
        try:
            m = await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            log.exception("card photo send failed — text card")
            m = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
    else:
        m = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
    track_msg(uid, chat_id, m.message_id)
    return m


async def _show_listing(update: Update, context: ContextTypes.DEFAULT_TYPE, pid: str | None, step: int = 0):
    """Render one catalogue card. `step` moves relative to `pid` (+1 next, -1 previous, wraps around)."""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    q = update.callback_query
    cur = q.message if q else None
    ids = available_ids()
    if not ids:
        text = "😔 <b>No IDs are available right now.</b>\n\nPlease check back soon!"
        if cur is not None and cur.message_id != welcome_msg.get(uid):
            await _put_card(context, uid, chat_id, cur, text, None,
                            InlineKeyboardMarkup([[ibt("🏠 Menu", "cat_close")]]))
        else:
            await send_tracked(context.bot, uid, chat_id, text, parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup([[ibt("🏠 Menu", "cat_close")]]))
        return
    if pid in ids:
        i = (ids.index(pid) + step) % len(ids)
    elif pid and step:                     # the listing vanished meanwhile — continue from the start
        i = 0
    else:
        i = 0
    cur_pid = ids[i]
    log_event(uid, "view_listing", cur_pid)
    card = await _put_card(context, uid, chat_id, cur if (cur is not None and cur.message_id in
                           {m for _, m in flow_msgs.get(uid, [])}) else None,
                           _card_caption(cur_pid, i, len(ids)), posts[cur_pid].get("photo"),
                           _card_markup(cur_pid, len(ids)))
    checkout.setdefault(uid, {})["card_msg"] = card.message_id
    checkout[uid]["chat_id"] = chat_id


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point from the reply keyboard 'Buy FF ID' or an inline Buy button."""
    query = update.callback_query
    uid   = update.effective_user.id
    if query:
        await query.answer()
    if await fj_gate(update, context):
        return
    user_state.pop(uid, None)
    await cleanup_flow(context.bot, uid)
    checkout.pop(uid, None)
    log_event(uid, "browse_catalogue")

    if user_pending(uid):
        await send_tracked(context.bot, uid, update.effective_chat.id,
                           "⏳ <b>You already have a pending order.</b>\n\n"
                           "Complete the payment in the invoice above or tap ❌ Cancel there.",
                           parse_mode="HTML", reply_markup=menu_button())
        return
    await _show_listing(update, context, None)


async def open_listing(update: Update, context: ContextTypes.DEFAULT_TYPE, pid: str):
    """Deep link / typed Post ID -> show that listing if it is available."""
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    if pid in sold:
        await send_tracked(context.bot, uid, chat_id, "❌ <b>This ID has already been sold.</b>", parse_mode="HTML", reply_markup=menu_button())
        return
    if pid not in posts or not post_ready(pid):
        await send_tracked(context.bot, uid, chat_id,
                           f"❌ <b>Post ID <code>{html.escape(pid)}</code> was not found.</b>\n\nSend the correct ID or browse the catalogue.",
                           parse_mode="HTML", reply_markup=menu_button())
        return
    if pid in reserved:
        await send_tracked(context.bot, uid, chat_id,
                           "⏳ <b>Another buyer is paying for this ID right now.</b>\n\nTry again in about 5 minutes.",
                           parse_mode="HTML", reply_markup=menu_button())
        return
    await cleanup_flow(context.bot, uid)
    log_event(uid, "view_listing", pid)
    ids = available_ids()
    card = await _put_card(context, uid, chat_id, None, _card_caption(pid, ids.index(pid), len(ids)),
                           posts[pid].get("photo"), _card_markup(pid, len(ids)))
    checkout[uid] = {"card_msg": card.message_id, "chat_id": chat_id}


async def cat_nav_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await fj_gate(update, context):
        return
    kind, _, pid = q.data.partition("_")[2].partition("_")
    await _show_listing(update, context, pid, +1 if kind == "next" else -1)


async def cat_enter_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    user_state[uid] = BUYER_WAIT_POST_ID
    m = await context.bot.send_message(q.message.chat_id,
                                       "🔎 <b>Send the Post ID</b> you want to buy:",
                                       parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup([[ibt("🔙 Back", "cat_cancel_enter")]]))
    track_msg(uid, q.message.chat_id, m.message_id)


async def cat_cancel_enter_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_state.pop(q.from_user.id, None)
    await safe_delete(context.bot, q.message.chat_id, q.message.message_id)
    untrack_msg(q.from_user.id, q.message.message_id)


async def buyer_post_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != BUYER_WAIT_POST_ID:
        return
    user_state.pop(uid, None)
    log_event(uid, "search_post_id", update.message.text.strip())
    await open_listing(update, context, update.message.text.strip())


async def cat_close_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    user_state.pop(uid, None)
    checkout.pop(uid, None)
    await cleanup_flow(context.bot, uid)          # the welcome/menu message is never touched
    if q.message and q.message.message_id != welcome_msg.get(uid):
        await safe_delete(context.bot, q.message.chat_id, q.message.message_id)


# ═══ CHECKOUT (promo, UPI / wallet) ═══════════════════════════════════════════
def _can_buy(uid: int, pid: str):
    """Re-validate availability. Returns (ok, error_message)."""
    if pid in sold:
        return False, "This ID has already been sold."
    if pid not in posts or not post_ready(pid):
        return False, "This ID is no longer available."
    if pid in reserved:
        return False, "Another buyer is paying for this ID right now. Try again in a few minutes."
    if user_pending(uid):
        return False, "You already have a pending order. Complete or cancel it first."
    return True, ""


def _checkout_view(uid: int, pid: str):
    """Returns (caption, markup, promo_result|None) built only from server-side data."""
    p = posts[pid]
    price = float(p["price"])
    co = checkout.get(uid) or {}
    code = co.get("code")
    promo = promo_evaluate(code, uid, price) if code else None
    if promo and not promo["ok"]:
        co.pop("code", None)
        promo = None
    final = promo["final"] if promo else round(price, 2)
    bal = balance_of(uid)
    lines = [
        "🧾 <b>CHECKOUT</b>",
        f"📌 Post ID: <code>{html.escape(pid)}</code>",
        f"💰 Price: ₹{money(price)}",
    ]
    if promo:
        lines += [f"🏷️ Promo <code>{html.escape(promo['code'])}</code>: −₹{money(promo['discount'])}",
                  f"💸 <b>Payable: ₹{money(final)}</b>"]
    else:
        lines.append(f"💸 <b>Payable: ₹{money(final)}</b>")
    lines.append(f"\n👛 Wallet balance: ₹{money(bal)}")
    rows = [[ibt("💳 Pay via UPI", f"co_upi_{pid}")]]
    if bal >= final:
        rows.append([ibt("💰 Pay with Balance", f"co_bal_{pid}")])
    rows.append([ibt("🏷️ Remove Promo" if promo else "🏷️ Apply Promo Code", f"co_rmpromo_{pid}" if promo else f"co_promo_{pid}")])
    rows.append([ibt("🔙 Back", f"cat_go_{pid}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows), promo


async def _edit_card(context, chat_id, msg, text, markup):
    """Edit text or caption of the card message, whichever it is."""
    try:
        if getattr(msg, "photo", None):
            await context.bot.edit_message_caption(chat_id=chat_id, message_id=msg.message_id, caption=text,
                                                   parse_mode="HTML", reply_markup=markup)
        else:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=text,
                                                parse_mode="HTML", reply_markup=markup)
    except tg_error.BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def cat_buy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Buy ID' pressed: validate inventory again, then open checkout on the same card."""
    q = update.callback_query
    uid = q.from_user.id
    pid = q.data[len("cat_buy_"):]
    if await fj_gate(update, context):
        await q.answer()
        return
    ok, err = _can_buy(uid, pid)
    if not ok:
        await q.answer(err, show_alert=True)
        await _show_listing(update, context, None)
        return
    await q.answer()
    log_event(uid, "checkout", pid)
    checkout[uid] = {"post_id": pid, "chat_id": q.message.chat_id, "card_msg": q.message.message_id}
    text, markup, _ = _checkout_view(uid, pid)
    await _edit_card(context, q.message.chat_id, q.message, text, markup)


async def cat_go_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_state.pop(uid, None)
    pid = q.data[len("cat_go_"):]
    checkout.pop(uid, None)
    await _show_listing(update, context, pid if pid in posts else None)


async def co_promo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    pid = q.data[len("co_promo_"):]
    await q.answer()
    co = checkout.setdefault(uid, {"post_id": pid, "chat_id": q.message.chat_id, "card_msg": q.message.message_id})
    co["post_id"] = pid
    user_state[uid] = BUYER_WAIT_PROMO
    m = await context.bot.send_message(q.message.chat_id, "🏷️ <b>Send your promo code:</b>", parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup([[ibt("🔙 Back", "co_promo_cancel")]]))
    co["prompt_msg"] = m.message_id
    track_msg(uid, q.message.chat_id, m.message_id)


async def co_promo_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_state.pop(q.from_user.id, None)
    await safe_delete(context.bot, q.message.chat_id, q.message.message_id)
    untrack_msg(q.from_user.id, q.message.message_id)


async def buyer_promo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != BUYER_WAIT_PROMO:
        return
    co = checkout.get(uid) or {}
    pid = co.get("post_id")
    chat_id = update.effective_chat.id
    user_state.pop(uid, None)
    if co.get("prompt_msg"):
        await safe_delete(context.bot, chat_id, co["prompt_msg"])
        untrack_msg(uid, co["prompt_msg"])
    if not pid or pid not in posts:
        await send_tracked(context.bot, uid, chat_id, "❌ That listing is no longer available.", reply_markup=menu_button())
        return
    code = promo_normalize(update.message.text or "")
    log_event(uid, "promo_try", code)
    res = promo_evaluate(code, uid, float(posts[pid]["price"]))
    if not res["ok"]:
        await send_tracked(context.bot, uid, chat_id, f"❌ {html.escape(res['reason'])}",
                           parse_mode="HTML")
        return
    co["code"] = res["code"]                      # only the code is stored; the discount is recomputed server-side
    text, markup, _ = _checkout_view(uid, pid)
    card_id = co.get("card_msg")
    if card_id:
        class _M:  # minimal stand-in carrying the message id and type
            message_id = card_id
            photo = bool(posts[pid].get("photo"))
        try:
            await _edit_card(context, chat_id, _M, text, markup)
        except Exception:
            log.exception("could not refresh checkout card")
    await send_tracked(context.bot, uid, chat_id,
                       f"✅ Promo applied: −₹{money(res['discount'])}\nOriginal ₹{money(posts[pid]['price'])} → "
                       f"payable <b>₹{money(res['final'])}</b>", parse_mode="HTML")


async def co_rmpromo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    pid = q.data[len("co_rmpromo_"):]
    await q.answer("Promo removed")
    (checkout.get(uid) or {}).pop("code", None)
    if pid in posts:
        text, markup, _ = _checkout_view(uid, pid)
        await _edit_card(context, q.message.chat_id, q.message, text, markup)


# ═══ PAYMENT: create order, invoice with progress indicator ═══════════════════
def _new_order_ref(prefix: str) -> str:
    appdata["order_seq"] = appdata.get("order_seq", 0) + 1
    return f"{prefix}{int(time.time())}{appdata['order_seq']:04d}"


def _bar(done: int, total: int, width: int = 10) -> str:
    n = int(round(width * done / max(total, 1)))
    return "▰" * n + "▱" * (width - n)


def build_invoice_caption(info: dict, remaining: int | None) -> str:
    kind = info.get("kind", "buy")
    head = "🧾 <b>AUTO-PAY INVOICE</b>" if kind == "buy" else "💰 <b>WALLET TOP-UP</b>"
    lines = [head, "━━━━━━━━━━━━━━━━━━"]
    if kind == "buy":
        lines.append(f"📌 Post ID  : <code>{html.escape(str(info['post_id']))}</code>")
        if info.get("discount"):
            lines.append(f"🏷️ Promo    : −₹{money(info['discount'])} (<code>{html.escape(info.get('code') or '')}</code>)")
    lines += [f"💰 Amount   : ₹{money(info['payable'])}",
              f"🆔 Order ID : <code>{html.escape(info['order_id'])}</code>",
              "━━━━━━━━━━━━━━━━━━", ""]
    if remaining is not None and remaining > 0:
        done = DISPLAY_TIMER_SECONDS - remaining
        lines += ["⏳ <b>Please wait while we prepare your order…</b>",
                  f"{_bar(done, DISPLAY_TIMER_SECONDS)}  <code>0:{remaining:02d}</code>",
                  "<i>This countdown is only a progress indicator — it does not mean the payment succeeded, "
                  "and your order is not cancelled when it ends.</i>", ""]
    else:
        lines += ["🔄 <b>Waiting for your payment…</b>",
                  "<i>The bot is checking the payment automatically.</i>", ""]
    lines += [f"📱 Pay exactly <b>₹{money(info['payable'])}</b> — do not edit the amount or note",
              "⏳ The QR stays valid for 5 minutes (gateway limit)",
              "💎 " + ("Your credentials arrive automatically after payment." if kind == "buy"
                       else "Your balance is credited automatically after payment.")]
    return "\n".join(lines)


def _cancel_markup(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[ibt("❌ Cancel", f"cancel_{order_id}")]])


async def _edit_invoice(bot, info: dict, remaining: int | None):
    cap = build_invoice_caption(info, remaining)
    try:
        if info.get("is_photo"):
            await bot.edit_message_caption(chat_id=info["chat_id"], message_id=info["msg_id"], caption=cap,
                                           parse_mode="HTML", reply_markup=_cancel_markup(info["order_id"]))
        else:
            await bot.edit_message_text(chat_id=info["chat_id"], message_id=info["msg_id"], text=cap,
                                        parse_mode="HTML", reply_markup=_cancel_markup(info["order_id"]))
        return True
    except tg_error.RetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        return True
    except tg_error.BadRequest as exc:
        return "not modified" in str(exc).lower()
    except Exception:
        return False


async def countdown_task(order_id: str, bot):
    """Visual 1-minute progress indicator on the invoice. It never expires or releases anything."""
    try:
        for remaining in range(DISPLAY_TIMER_SECONDS - DISPLAY_TIMER_STEP, -1, -DISPLAY_TIMER_STEP):
            await asyncio.sleep(DISPLAY_TIMER_STEP)
            info = pending_orders.get(order_id)
            if not info or not info.get("msg_id"):
                return
            if not await _edit_invoice(bot, info, remaining if remaining > 0 else None):
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("countdown failed for %s", order_id)
    finally:
        if _timers.get(order_id) is asyncio.current_task():
            _timers.pop(order_id, None)


def start_timer(order_id: str, bot):
    if order_id in _timers and not _timers[order_id].done():
        return                                           # never two timers on one order
    _timers[order_id] = _spawn(countdown_task(order_id, bot))


def start_poller(order_id: str, context_or_app):
    if order_id in _polling:
        return                                           # no duplicate pollers (restart / retry safe)
    _polling.add(order_id)
    _spawn(poll_payment(order_id, context_or_app))


async def create_gateway_invoice(context, uid: int, chat_id: int, name: str, amount: float,
                                 kind: str, extra: dict, wait_msg=None):
    """Create a FamGateway order, send the QR invoice and start poller + progress timer.
    Returns the order id, or None (everything released) on failure."""
    bot = context.bot
    try:
        data = await fg_create_order({"api_key": FG_API_KEY, "amount": amount, "customer_name": name or "Buyer"})
        if data.get("status") != "success":
            raise RuntimeError(f"gateway said: {data}")
        od = data["data"]
        order_id, payable, qr_url, upi_intent = od["order_id"], od["payable_amount"], od["qr_url"], od.get("upi_intent")
    except Exception as e:
        log.exception("create-order failed")
        if wait_msg:
            try:
                await wait_msg.edit_text("❌ Could not generate the payment. Please try again in a moment.")
            except Exception:
                pass
        await notify_admin(bot, f"⚠️ FamGateway order create failed: {e!r}"[:900])
        return None

    info = {"user_id": uid, "chat_id": chat_id, "msg_id": None, "kind": kind, "order_id": order_id,
            "payable": payable, "amount": amount, "created": time.time(), "is_photo": False, **extra}
    pending_orders[order_id] = info
    if kind == "buy":
        reserved[info["post_id"]] = order_id
        if info.get("code"):
            promo_hold(order_id, info["code"], uid)
    app_save()

    caption = build_invoice_caption(info, DISPLAY_TIMER_SECONDS)
    qr_png = await asyncio.to_thread(make_branded_qr, upi_intent)
    sent = None
    try:
        for photo in ([qr_png] if qr_png else []) + [qr_url]:
            try:
                sent = await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption,
                                            parse_mode="HTML", reply_markup=_cancel_markup(order_id))
                info["is_photo"] = True
                break
            except Exception:
                continue
        if sent is None:
            sent = await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML",
                                          reply_markup=_cancel_markup(order_id))
    except Exception:
        log.exception("invoice send failed")
        release_order(order_id)
        if wait_msg:
            try:
                await wait_msg.edit_text("❌ Could not send the invoice. Please try again.")
            except Exception:
                pass
        return None

    info["msg_id"] = sent.message_id
    track_msg(uid, chat_id, sent.message_id)
    app_save()
    if wait_msg:
        await safe_delete(bot, chat_id, wait_msg.message_id)
        untrack_msg(uid, wait_msg.message_id)
    start_poller(order_id, context)
    start_timer(order_id, bot)
    return order_id


async def co_upi_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pay via UPI: re-validate everything server-side, reserve the ID, create the gateway order."""
    q = update.callback_query
    uid = q.from_user.id
    pid = q.data[len("co_upi_"):]
    chat_id = q.message.chat_id
    ok, err = _can_buy(uid, pid)
    if not ok:
        await q.answer(err, show_alert=True)
        await _show_listing(update, context, None)
        return
    price = float(posts[pid]["price"])
    co = checkout.get(uid) or {}
    code, discount, final = None, 0.0, round(price, 2)
    if co.get("code"):
        res = promo_evaluate(co["code"], uid, price)
        if res["ok"]:
            code, discount, final = res["code"], res["discount"], res["final"]
        else:
            co.pop("code", None)
            await q.answer(f"Promo removed: {res['reason']}", show_alert=True)
            text, markup, _ = _checkout_view(uid, pid)
            await _edit_card(context, chat_id, q.message, text, markup)
            return
    await q.answer()
    reserved[pid] = "creating"                  # no await between the availability check and this line
    log_event(uid, "pay_upi", pid)
    wait = await send_tracked(context.bot, uid, chat_id, "⏳ <b>Please wait while we prepare your order…</b>", parse_mode="HTML")
    oid = await create_gateway_invoice(
        context, uid, chat_id, q.from_user.first_name or "Buyer", final, "buy",
        {"post_id": pid, "price": price, "discount": discount, "code": code}, wait_msg=wait)
    if oid is None:
        if reserved.get(pid) == "creating":
            reserved.pop(pid, None)
        return
    # the catalogue card has done its job — remove it (the invoice message replaces it)
    await safe_delete(context.bot, chat_id, q.message.message_id)
    untrack_msg(uid, q.message.message_id)
    checkout.pop(uid, None)


async def co_bal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pay with wallet balance. Validation, reservation and debit happen with no await in between."""
    q = update.callback_query
    uid = q.from_user.id
    pid = q.data[len("co_bal_"):]
    chat_id = q.message.chat_id
    ok, err = _can_buy(uid, pid)
    if not ok:
        await q.answer(err, show_alert=True)
        await _show_listing(update, context, None)
        return
    price = float(posts[pid]["price"])
    co = checkout.get(uid) or {}
    code, discount, final = None, 0.0, round(price, 2)
    if co.get("code"):
        res = promo_evaluate(co["code"], uid, price)
        if not res["ok"]:
            co.pop("code", None)
            await q.answer(f"Promo removed: {res['reason']}", show_alert=True)
            text, markup, _ = _checkout_view(uid, pid)
            await _edit_card(context, chat_id, q.message, text, markup)
            return
        code, discount, final = res["code"], res["discount"], res["final"]
    order_id = _new_order_ref("WAL")
    try:
        ledger_add(uid, "purchase", -final, ref=order_id, note=f"Post {pid}")
    except InsufficientBalance:
        await q.answer("Your balance is too low for this purchase.", show_alert=True)
        return
    await q.answer("✅ Paid from balance")
    pending_orders[order_id] = {"user_id": uid, "chat_id": chat_id, "msg_id": None, "kind": "buy", "order_id": order_id,
                                "post_id": pid, "price": price, "discount": discount, "code": code, "payable": final,
                                "amount": final, "method": "wallet", "created": time.time(), "is_photo": False}
    reserved[pid] = order_id
    if code:
        promo_hold(order_id, code, uid)
    app_save()
    log_event(uid, "pay_wallet", pid)
    await safe_delete(context.bot, chat_id, q.message.message_id)
    untrack_msg(uid, q.message.message_id)
    checkout.pop(uid, None)
    await deliver_order(order_id, {"utr": None, "sender": None}, context)


# ═══ WALLET TOP-UP ════════════════════════════════════════════════════════════
async def topup_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    arg = q.data[len("topup_"):]
    mt = float(S().get("min_topup", MIN_TOPUP))
    await q.answer()
    if arg == "custom":
        user_state[uid] = WALLET_WAIT_AMOUNT
        m = await context.bot.send_message(q.message.chat_id,
                                           f"✏️ <b>Send the amount to add</b> (minimum ₹{money(mt)}, numbers only):",
                                           parse_mode="HTML",
                                           reply_markup=InlineKeyboardMarkup([[ibt("🔙 Back", "co_promo_cancel")]]))
        track_msg(uid, q.message.chat_id, m.message_id)
        return
    await _start_topup(update, context, float(arg), q.message.chat_id)


async def wallet_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != WALLET_WAIT_AMOUNT:
        return
    mt = float(S().get("min_topup", MIN_TOPUP))
    try:
        amt = round(float(update.message.text.strip()), 2)
        if amt < mt or amt > 50000:
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"❌ Send a number between ₹{money(mt)} and ₹50000.")
        return
    user_state.pop(uid, None)
    await _start_topup(update, context, amt, update.effective_chat.id)


async def _start_topup(update, context, amount: float, chat_id: int):
    uid = update.effective_user.id
    u = update.effective_user
    mt = float(S().get("min_topup", MIN_TOPUP))
    if amount < mt:
        await context.bot.send_message(chat_id, f"❌ Minimum top-up is ₹{money(mt)}.")
        return
    if user_pending(uid):
        await send_tracked(context.bot, uid, chat_id,
                           "⏳ <b>You already have a pending payment.</b>\nComplete or cancel it first.",
                           parse_mode="HTML")
        return
    log_event(uid, "topup_start", f"₹{money(amount)}")
    wait = await send_tracked(context.bot, uid, chat_id, "⏳ <b>Please wait while we prepare your order…</b>", parse_mode="HTML")
    await create_gateway_invoice(context, uid, chat_id, u.first_name or "Buyer", amount, "topup", {}, wait_msg=wait)


# ═══ ORDER OUTCOMES ═══════════════════════════════════════════════════════════
async def _finish_topup(order_id: str, info: dict, data: dict, context):
    """Credit the wallet exactly once for a verified top-up and send a receipt."""
    bot, uid, chat_id = context.bot, info["user_id"], info["chat_id"]
    amount = float(info["amount"])
    utr = data.get("utr")
    utr = None if utr in (None, "", "N/A") else utr
    entry = ledger_add(uid, "topup", amount, ref=order_id, note="UPI top-up (verified)")     # idempotent per order
    inv, created = make_invoice(order_id, type="topup", uid=uid, username=urec(uid).get("username", ""),
                                item="Wallet top-up", price=amount, discount=0, paid=amount,
                                method="UPI (FamGateway)", utr=utr, post_id=None)
    app_save()
    log_event(uid, "topup_paid", f"₹{money(amount)}")
    if info.get("msg_id"):
        await safe_delete(bot, chat_id, info["msg_id"])
        untrack_msg(uid, info["msg_id"])
    if entry is None and not created:
        return
    try:
        await bot.send_message(chat_id, f"✅ <b>Top-up successful!</b>\n\n₹{money(amount)} added to your wallet.\n"
                               f"New balance: <b>₹{money(balance_of(uid))}</b>", parse_mode="HTML")
        await bot.send_message(chat_id, render_invoice(inv), parse_mode="HTML")
    except Exception:
        log.exception("could not message user after top-up")
    await notify_admin(bot, f"💰 Top-up ₹{money(amount)} | user {uid} | order {order_id} | UTR {utr or 'Not provided'}")


async def deliver_order(order_id: str, data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Payment confirmed → mark sold, redeem promo, send credentials + receipt. Claims the order atomically."""
    info = pending_orders.pop(order_id, None)
    if not info:
        return
    _last_seen.pop(order_id, None)
    _polling.discard(order_id)
    _cancel_timer(order_id)

    if info.get("kind") == "topup":
        await _finish_topup(order_id, info, data, context)
        return

    post_id = info["post_id"]
    chat_id = info["chat_id"]
    uid     = info["user_id"]
    bot     = context.bot
    wallet  = info.get("method") == "wallet"

    reserved.pop(post_id, None)
    post = posts.pop(post_id, None)
    sold.add(post_id)

    utr    = data.get("utr")
    utr    = None if utr in (None, "", "N/A") else str(utr)
    sender = data.get("sender") or "N/A"
    log.info("order %s PAID | post=%s utr=%s sender=%s method=%s", order_id, post_id, utr, sender, "wallet" if wallet else "upi")
    app_save()

    if info.get("msg_id"):
        await safe_delete(bot, chat_id, info["msg_id"])
        untrack_msg(uid, info["msg_id"])
    await cleanup_flow(bot, uid)

    if post is None:
        sold.discard(post_id)
        app_save()
        if wallet:                                   # nothing was delivered → give the money back
            ledger_add(uid, "refund", float(info["payable"]), ref=order_id, note=f"Post {post_id} unavailable")
            promo_release(order_id)
            app_save()
            msg = "⚠️ This ID is no longer available, so your wallet payment was refunded."
        else:
            msg = "✅ Payment received, but the ID could not be retrieved. An admin will contact you shortly."
        try:
            await bot.send_message(chat_id, msg, reply_markup=menu_button())
        except Exception:
            log.exception("could not message buyer")
        await notify_admin(bot, f"⚠️ Order {order_id} (post {post_id}, UTR {utr or 'Not provided'}) — post data missing"
                                f"{' (wallet payment refunded)' if wallet else ''}.")
        return

    promo_redeem(order_id, info.get("code") or "", uid, float(info["price"]), float(info.get("discount") or 0),
                 float(info["payable"])) if info.get("code") else None
    inv, _ = make_invoice(
        order_id, type="purchase", uid=uid, username=urec(uid).get("username", ""), post_id=post_id,
        item="Free Fire ID" + (f" — {post['info']}" if post.get("info") else ""),
        price=float(info["price"]), discount=float(info.get("discount") or 0), promo=info.get("code"),
        paid=float(info["payable"]), method="Wallet balance" if wallet else "UPI (FamGateway)", utr=utr)
    rec = urec(uid)
    rec["last_purchase"] = time.time()
    # optional referral reward on the referred user's first verified purchase
    ref = rec.get("referrer")
    if REFERRAL_BONUS > 0 and ref and ref in users and ledger_add(ref, "referral", REFERRAL_BONUS, ref=f"ref:{uid}",
                                                                   note=f"Referral: first purchase by {uid}"):
        pass
    app_save()
    log_event(uid, "purchase_done", post_id)
    await channel_mark_sold(bot, post_id, post)

    creds = post["creds"]
    e     = html.escape
    html_text = (
        "✅ <b>PAYMENT VERIFIED!</b>\n\n"
        "🎮 <b>FF ID Credentials:</b>\n"
        f"<pre>{e(creds)}</pre>\n"
        f"📌 Post ID : <code>{e(post_id)}</code>\n"
        f"🆔 Order   : <code>{e(order_id)}</code>\n"
        f"🏦 UTR     : <code>{e(utr or 'Not provided')}</code>\n"
        f"👤 Paid by : {e(str(sender))}\n\n"
        "🙏 <b>Thanks for your purchase! Enjoy!</b>\n"
        "⚠️ Take a screenshot of the credentials!"
    )
    plain_text = (
        "✅ PAYMENT VERIFIED!\n\n"
        f"🎮 FF ID Credentials:\n{creds}\n\n"
        f"📌 Post ID : {post_id}\n🆔 Order   : {order_id}\n🏦 UTR     : {utr or 'Not provided'}\n"
        f"👤 Paid by : {sender}\n\n🙏 Thanks for your purchase! Enjoy!\n⚠️ Take a screenshot of the credentials!"
    )
    try:
        await bot.send_message(chat_id, html_text, parse_mode="HTML", reply_markup=menu_button())
    except Exception:
        log.exception("HTML delivery failed, retrying as plain text")
        try:
            await bot.send_message(chat_id, plain_text, reply_markup=menu_button())
        except Exception:
            log.exception("plain delivery failed too")
            await notify_admin(
                bot,
                f"⚠️ DELIVERY FAILED — the buyer paid but the message was not sent.\n"
                f"Buyer chat id: {chat_id}\nOrder: {order_id}\nUTR: {utr or 'Not provided'}\n\nCredentials:\n{creds}",
            )
    try:                                              # receipt: a separate, kept message
        await bot.send_message(chat_id, render_invoice(inv), parse_mode="HTML")
    except Exception:
        log.exception("could not send receipt")

    await notify_admin(
        bot,
        f"💸 Sale! Post {post_id} | Order {order_id} | ₹{money(info['payable'])} | UTR {utr or 'Not provided'} "
        f"| Buyer {chat_id} | {'wallet' if wallet else 'UPI'}",
    )


async def expire_order(order_id: str, context: ContextTypes.DEFAULT_TYPE):
    info = release_order(order_id)
    if not info:
        return
    log.info("order %s expired", order_id)
    uid = info["user_id"]
    if info.get("msg_id"):
        await safe_delete(context.bot, info["chat_id"], info["msg_id"])
        untrack_msg(uid, info["msg_id"])
    try:
        await context.bot.send_message(
            info["chat_id"],
            "⏰ <b>Payment time is over!</b>\n\nThe order expired. You can try again.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [ibt("🔄 Buy Again", "buy_start"), ibt("🏠 Home", "main_menu")],
            ]),
        )
    except Exception:
        log.exception("could not send expiry message")


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    uid      = query.from_user.id
    order_id = query.data[len("cancel_"):]
    chat_id  = query.message.chat_id
    info     = pending_orders.get(order_id)

    if not info or info["user_id"] != uid:
        await query.answer("This order is no longer active.", show_alert=True)
        await safe_delete(context.bot, chat_id, query.message.message_id)
        return

    await query.answer()

    # Last-chance check before cancelling
    status, data = await fg_status(order_id)
    if status == "success":
        await deliver_order(order_id, data, context)
        return

    if release_order(order_id) is None:
        return

    await safe_delete(context.bot, chat_id, query.message.message_id)
    untrack_msg(uid, query.message.message_id)
    await context.bot.send_message(
        chat_id,
        "❌ Order cancelled. Go to the Main Menu to pick another ID.",
        reply_markup=menu_button(),
    )


# ─── PAYMENT POLLER ───────────────────────────────────────────────────────────
async def poll_payment(order_id: str, ctx):
    """
    Polls FamGateway every POLL_INTERVAL seconds until the order is paid, expired or cancelled.
    The first check is immediate. The deadline follows the order's real creation time, so a poller
    resumed after a restart does not outlive the gateway order.
    """
    info0 = pending_orders.get(order_id) or {}
    deadline = time.monotonic() + max(5.0, POLL_MAX_SECONDS - (time.time() - info0.get("created", time.time())))
    first_check = True
    try:
        while time.monotonic() < deadline:
            if not first_check:
                await asyncio.sleep(POLL_INTERVAL)
            first_check = False

            if order_id not in pending_orders:
                return  # cancelled / delivered by another code path

            try:
                status, data = await fg_status(order_id)
                if status == "success":
                    await deliver_order(order_id, data, ctx)
                    return
                if status == "expired":
                    await expire_order(order_id, ctx)
                    return
            except Exception:
                log.exception("poll error for %s", order_id)

        if order_id in pending_orders:                         # final authoritative check before expiring
            status, data = await fg_status(order_id)
            if status == "success":
                await deliver_order(order_id, data, ctx)
            else:
                await expire_order(order_id, ctx)
    except asyncio.CancelledError:
        raise
    finally:
        _polling.discard(order_id)

# ═══ DESTINATION REGISTRY (groups / channels) ═════════════════════════════════
# Telegram has no API that lists every chat a bot is admin in. The registry is filled by (a) my_chat_member
# updates, which Telegram sends when the bot is added/removed/promoted, and (b) the owner adding chats by hand.
def grp() -> dict:
    return appdata.setdefault("groups", {})


async def check_dest(bot, chat_id) -> dict:
    """Live check of membership + posting permission. Never raises."""
    res = {"ok": False, "reason": "", "kind": "error", "title": str(chat_id), "type": "?", "username": None}
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:
        res["reason"] = f"inaccessible: {str(exc)[:80]}"
        res["kind"] = "inaccessible"
        return res
    res.update(title=chat.title or str(chat.id), type=chat.type, username=chat.username)
    if chat.type not in ("group", "supergroup", "channel"):
        res["reason"] = "not a group or channel"
        return res
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except Exception as exc:
        res["reason"] = f"cannot read bot membership: {str(exc)[:80]}"
        res["kind"] = "inaccessible"
        return res
    st = me.status
    if st in ("left", "kicked"):
        res["reason"], res["kind"] = "bot is not a member", "inaccessible"
    elif chat.type == "channel":
        if st == "creator" or (st == "administrator" and getattr(me, "can_post_messages", False)):
            res["ok"] = True
        else:
            res["reason"], res["kind"] = "bot lacks the 'Post messages' permission", "perm"
    else:
        perms = getattr(chat, "permissions", None)
        if st in ("creator", "administrator"):
            res["ok"] = True
        elif st == "restricted" and not getattr(me, "can_send_messages", True):
            res["reason"], res["kind"] = "bot is muted in this group", "perm"
        elif perms is not None and perms.can_send_messages is False:
            res["reason"], res["kind"] = "members cannot send messages here", "perm"
        else:
            res["ok"] = True
    return res


def grp_apply_check(chat_id, chk: dict, source: str | None = None):
    key = str(chat_id)
    g = grp().setdefault(key, {"id": chat_id, "source": source or "auto", "promo": True, "added": time.time()})
    g.update(title=chk["title"], type=chk["type"], username=chk["username"], can_post=bool(chk["ok"]),
             reason=chk["reason"], checked=time.time(), active=chk["kind"] != "inaccessible" or g.get("active", True))
    if chk["kind"] == "inaccessible":
        g["active"] = False
    else:
        g["active"] = True
    if source == "owner":
        g["source"] = "owner"
    return g


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram tells us whenever the bot is added to / removed from / promoted in a group or channel."""
    cmu = update.my_chat_member
    if not cmu or cmu.chat.type not in ("group", "supergroup", "channel"):
        return
    cid = cmu.chat.id
    if cmu.new_chat_member.status in ("left", "kicked"):
        g = grp().get(str(cid))
        if g:
            g.update(active=False, can_post=False, reason="bot was removed", checked=time.time())
        else:
            grp()[str(cid)] = {"id": cid, "title": cmu.chat.title or str(cid), "type": cmu.chat.type,
                               "username": cmu.chat.username, "source": "auto", "promo": True, "active": False,
                               "can_post": False, "reason": "bot was removed", "checked": time.time(), "added": time.time()}
    else:
        grp_apply_check(cid, await check_dest(context.bot, cid))
    app_save()


def eligible_dests(only_promo: bool = False) -> list:
    out = []
    for key, g in grp().items():
        if g.get("active") and g.get("can_post") and (g.get("promo", True) or not only_promo):
            out.append(g["id"])
    return out


# ═══ PROMOTION MESSAGE ═════════════════════════════════════════════════════════
def build_promo_text() -> str:
    """Same premium-emoji + framed-title treatment as every other bot message (the send wrapper converts
    the plain emojis to premium ones and frames the title line)."""
    s = S()
    name = html.escape(s.get("store_name") or BOT_NAME)
    claim = (s.get("orders_claim") or "").strip()
    line = _te(_LOOP_ID, "➿") * 11
    parts = [
        f"<b>{name}</b>", "",
        "💎 Welcome to our Free Fire ID marketplace!", "",
        "⚡ Explore available FF ID listings and place orders directly through our bot.", "",
    ]
    if claim:
        parts += [f"📦 <b>{html.escape(claim)} Orders Completed!</b>", ""]
    parts += [
        "🔐 Easy ordering process",
        "💎 Premium user experience",
        "⚡ Organized order management", "",
        line, "",
        "🚀 Open the bot to explore available IDs and services!", "",
        f"✨ <b>{name}</b>",
    ]
    return "\n".join(parts)


def promo_markup(bot) -> InlineKeyboardMarkup | None:
    link = S().get("bot_link") or (f"https://t.me/{bot.username}" if getattr(bot, "username", None) else "")
    if not link:
        return None
    return InlineKeyboardMarkup([[ibt_url("🚀 Open Bot", link)]])


# ═══ BROADCAST ENGINE ═════════════════════════════════════════════════════════
async def _send_one(bot, cid, text, markup):
    """Send once. Returns (status, detail, message_id, new_chat_id).
    A timeout is ambiguous (the message may have been delivered), so it is NEVER retried automatically."""
    for _ in range(3):
        try:
            m = await bot.send_message(chat_id=cid, text=text, parse_mode="HTML", reply_markup=markup)
            return "sent", "", m.message_id, None
        except tg_error.RetryAfter as exc:                    # flood wait: Telegram did NOT deliver — safe to retry
            await asyncio.sleep(float(exc.retry_after) + 1)
        except tg_error.ChatMigrated as exc:
            return "failed", f"chat migrated to {exc.new_chat_id}", None, exc.new_chat_id
        except tg_error.Forbidden as exc:
            return "failed", f"blocked / forbidden: {str(exc)[:80]}", None, None
        except tg_error.BadRequest as exc:
            return "failed", f"rejected: {str(exc)[:80]}", None, None
        except (tg_error.TimedOut, tg_error.NetworkError) as exc:
            return "uncertain", f"network timeout (not retried to avoid a duplicate): {str(exc)[:60]}", None, None
        except Exception as exc:
            return "failed", repr(exc)[:100], None, None
    return "failed", "flood limit hit repeatedly", None, None


async def broadcast_run(bot, kind: str, dest_ids: list, bc_id: str | None = None, progress=None) -> dict:
    """Send the promo to each destination at most once per broadcast id (persisted — restart / retry safe)."""
    bcs = appdata.setdefault("broadcasts", {})
    bc_id = bc_id or f"{kind}-{int(time.time())}"
    bc = bcs.setdefault(bc_id, {"id": bc_id, "ts": time.time(), "kind": kind, "results": {}})
    text, markup = build_promo_text(), promo_markup(bot)
    for n, cid in enumerate(dest_ids, 1):
        key = str(cid)
        prev = bc["results"].get(key)
        if prev and prev["status"] in ("sent", "sending", "uncertain"):
            continue                                            # duplicate protection
        chk = await check_dest(bot, cid)
        grp_apply_check(cid, chk)
        if not chk["ok"]:
            bc["results"][key] = {"status": "skipped" if chk["kind"] == "perm" else "failed",
                                  "detail": chk["reason"], "ts": time.time(), "title": chk["title"]}
        else:
            bc["results"][key] = {"status": "sending", "ts": time.time(), "title": chk["title"], "detail": ""}
            app_save()                                          # persisted BEFORE sending: a crash cannot cause a resend
            status, detail, mid, newid = await _send_one(bot, cid, text, markup)
            bc["results"][key].update(status=status, detail=detail, msg_id=mid, ts=time.time())
            if status == "failed" and "forbidden" in detail:
                grp().get(key, {}).update(active=False, can_post=False, reason=detail)
            if newid:
                g = grp().pop(key, None)
                if g:
                    g["id"] = newid
                    grp()[str(newid)] = g
        app_save()
        if progress:
            await progress(n, len(dest_ids))
        await asyncio.sleep(1.5)                                # stay well under Telegram's per-chat/global limits
    return bc


def bc_report(bc: dict) -> str:
    res = bc["results"]
    cnt = {k: sum(1 for r in res.values() if r["status"] == k) for k in ("sent", "failed", "skipped", "uncertain")}
    lines = [f"📣 <b>Broadcast report</b>",
             f"✅ Sent: <b>{cnt['sent']}</b> · ❌ Failed/inaccessible: <b>{cnt['failed']}</b> · "
             f"⚠️ Skipped (no permission): <b>{cnt['skipped']}</b> · ❓ Uncertain: <b>{cnt['uncertain']}</b>"]
    for r in list(res.values()):
        if r["status"] != "sent":
            lines.append(f"• {html.escape(str(r.get('title')))} — {r['status']}: {html.escape(str(r.get('detail') or ''))}")
    if cnt["uncertain"]:
        lines.append("\n<i>Uncertain = the request timed out; it may or may not have been delivered. It was not resent.</i>")
    return "\n".join(lines)[:4000]


# ═══ BROADCAST UI ═════════════════════════════════════════════════════════════
async def adm_bc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state.pop(q.from_user.id, None)
    total = len(grp())
    elig = len(eligible_dests())
    s = S()
    auto = "🟢 ON" if s["promo_enabled"] and not s["promo_paused"] else ("⏸ paused" if s["promo_enabled"] else "🔴 OFF")
    text = (
        "📣 <b>Broadcast</b>\n\n"
        f"Registered destinations: <b>{total}</b>\n"
        f"Eligible (bot can post): <b>{elig}</b>\n"
        f"Auto promotion every {s['promo_interval_h']}h: <b>{auto}</b>\n\n"
        "The bot registers a group/channel automatically when it is added there, and you can add more by hand."
    )
    rows = [
        [ibt("📤 Send Broadcast", "bc_send")],
        [ibt("📋 Destinations", "bc_list"), ibt("➕ Add Destination", "bc_add")],
        [ibt("🔄 Refresh All", "bc_refresh"), ibt("👁 Preview", "bc_preview")],
        [ibt("✏️ Edit Template", "bc_tpl"), ibt("⏰ Auto Promotion", "bc_auto")],
        [ibt("🔙 Back", "admin_panel")],
    ]
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def bc_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    items = list(grp().values())[:20]
    lines, rows = [], []
    for g in items:
        icon = "✅" if g.get("active") and g.get("can_post") else ("⚠️" if g.get("active") else "❌")
        lines.append(f"{icon} {html.escape(str(g.get('title')))} <i>({g.get('type')}, {g.get('source')})</i>"
                     + (f"\n    ↳ {html.escape(str(g.get('reason')))}" if g.get("reason") else ""))
        short = _clean_md(str(g.get("title")))[:14]
        rows.append([ibt(f"{'🔔' if g.get('promo', True) else '🔕'} {short}", f"bc_a_{g['id']}"),
                     ibt("🗑 Remove", f"bc_r_{g['id']}")])
    text = ("📋 <b>Destinations</b>\n\n" + ("\n".join(lines) or "None yet.") +
            "\n\n🔔/🔕 = included in / excluded from auto promotion.")
    rows.append([ibt("🔙 Back", "adm_bc")])
    await q.edit_message_text(text[:4000], parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def bc_toggle_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    g = grp().get(q.data[len("bc_a_"):])
    if g:
        g["promo"] = not g.get("promo", True)
        app_save()
    await bc_list(update, context)


async def bc_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    grp().pop(q.data[len("bc_r_"):], None)
    app_save()
    await bc_list(update, context)


async def bc_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = ADMIN_BC_ADD
    await q.edit_message_text(
        "➕ <b>Add Destination</b>\n\nMake the bot an admin there first, then send (one or more):\n"
        "• <code>@username</code>\n• <code>https://t.me/username</code>\n• <code>-1001234567890</code>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_button("adm_bc")))


async def bc_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_BC_ADD:
        return
    out = []
    for tok in [t for t in re.split(r"[\s,]+", (update.message.text or "").strip()) if t]:
        m = re.match(r"^(?:https?://)?t\.me/(?!\+|joinchat)([A-Za-z0-9_]{4,})/?$", tok)
        ident = ("@" + m.group(1)) if m else (tok if tok.startswith("@") else (int(tok) if re.fullmatch(r"-?\d{6,}", tok) else None))
        if ident is None:
            out.append(f"❌ {html.escape(tok)} — invalid format")
            continue
        try:
            chat = await context.bot.get_chat(ident)
        except tg_error.TelegramError:
            out.append(f"❌ {html.escape(tok)} — not accessible (add the bot first)")
            continue
        chk = await check_dest(context.bot, chat.id)
        grp_apply_check(chat.id, chk, source="owner")
        out.append(f"{'✅' if chk['ok'] else '⚠️'} {html.escape(chk['title'])} — " + ("ready" if chk["ok"] else html.escape(chk["reason"])))
    app_save()
    user_state.pop(uid, None)
    await update.message.reply_text("\n".join(out) or "Nothing found.", parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(back_button("adm_bc")))


async def bc_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer("Checking…")
    ok = bad = 0
    for g in list(grp().values()):
        chk = await check_dest(context.bot, g["id"])
        grp_apply_check(g["id"], chk)
        ok += bool(chk["ok"])
        bad += not chk["ok"]
        await asyncio.sleep(0.3)
    app_save()
    await q.edit_message_text(f"🔄 <b>Refreshed</b>\n\n✅ Ready: <b>{ok}</b>\n⚠️ Not ready: <b>{bad}</b>",
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_button("adm_bc")))


async def bc_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    await context.bot.send_message(q.message.chat_id, build_promo_text(), parse_mode="HTML",
                                   reply_markup=promo_markup(context.bot))


def _sel(uid) -> set:
    t = admin_temp.setdefault(uid, {})
    if "bc_sel" not in t:
        t["bc_sel"] = set(eligible_dests())
    return t["bc_sel"]


async def bc_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    if q.data == "bc_send":
        admin_temp.setdefault(uid, {}).pop("bc_sel", None)
    elig = eligible_dests()
    sel = _sel(uid)
    if q.data == "bc_sel_all":
        sel.update(elig)
    elif q.data == "bc_sel_none":
        sel.clear()
    elif q.data.startswith("bc_t_"):
        cid = int(q.data[len("bc_t_"):])
        sel.symmetric_difference_update({cid})
    sel.intersection_update(elig)
    if not elig:
        await q.edit_message_text("⚠️ No eligible destinations. Add the bot as admin in a group/channel or use Add Destination.",
                                  reply_markup=InlineKeyboardMarkup(back_button("adm_bc")))
        return
    rows = []
    for cid in elig[:30]:
        g = grp()[str(cid)]
        rows.append([ibt(f"{'✅' if cid in sel else '⬜'} {_clean_md(str(g.get('title')))[:28]}", f"bc_t_{cid}")])
    rows.append([ibt("☑️ Select All", "bc_sel_all"), ibt("⬜ Clear", "bc_sel_none")])
    rows.append([ibt(f"➡️ Continue ({len(sel)})", "bc_go")])
    rows.append([ibt("🔙 Back", "adm_bc")])
    await q.edit_message_text("📤 <b>Choose destinations</b>\n\nOnly chats where the bot can currently post are listed.",
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def bc_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    sel = _sel(uid)
    if not sel:
        await q.answer("Select at least one destination.", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text(
        f"📤 <b>Confirm broadcast</b>\n\nThe promotion message will be sent to <b>{len(sel)}</b> destination(s). "
        "Permissions are re-checked right before each send.\n\nUse 👁 Preview in the Broadcast menu to see the message.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[ibt("✅ Confirm & Send", "bc_confirm")], [ibt("🔙 Back", "bc_send")]]))


async def bc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    sel = sorted(_sel(uid))
    admin_temp.get(uid, {}).pop("bc_sel", None)
    if not sel:
        await q.answer("Nothing selected.", show_alert=True)
        return
    await q.answer("Sending…")
    bc_id = f"manual-{int(time.time())}-{uid}"
    chat_id, mid = q.message.chat_id, q.message.message_id
    await q.edit_message_text(f"📤 Sending to {len(sel)} destination(s)…", parse_mode="HTML")

    async def job():
        last = [0.0]

        async def progress(n, tot):
            if time.time() - last[0] > 4 or n == tot:
                last[0] = time.time()
                try:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=mid, text=f"📤 Sending… {n}/{tot}")
                except Exception:
                    pass
        bc = await broadcast_run(context.bot, "manual", sel, bc_id=bc_id, progress=progress)
        await context.bot.send_message(chat_id, bc_report(bc), parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(back_button("adm_bc")))
    _spawn(job())


# ─── Auto promotion ───────────────────────────────────────────────────────────
async def bc_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    s = S()
    d = q.data
    if d == "bc_auto_on":
        s["promo_enabled"], s["promo_paused"] = True, False
        s["promo_last_ts"] = s.get("promo_last_ts") or time.time()      # first post one interval from now
    elif d == "bc_auto_off":
        s["promo_enabled"] = False
    elif d == "bc_auto_pause":
        s["promo_paused"] = not s["promo_paused"]
    elif d.startswith("bc_int_"):
        s["promo_interval_h"] = int(d[len("bc_int_"):])
    app_save()
    await q.answer()
    state = "🟢 running" if s["promo_enabled"] and not s["promo_paused"] else ("⏸ paused" if s["promo_enabled"] else "🔴 disabled")
    nxt = ""
    if s["promo_enabled"] and not s["promo_paused"]:
        nxt = f"\nNext send: {fmt_dt(s.get('promo_last_ts', 0) + s['promo_interval_h'] * 3600)}"
    text = (f"⏰ <b>Auto Promotion</b>\n\nStatus: <b>{state}</b>\nInterval: every <b>{s['promo_interval_h']}h</b>{nxt}\n"
            f"Destinations: <b>{len(eligible_dests(True))}</b> (🔔 chats where the bot can post)\n\n"
            "Uses the same message as manual broadcast. Sent at most once per destination per slot, "
            "and the schedule survives restarts.")
    rows = [[ibt("🔴 Disable" if s["promo_enabled"] else "🟢 Enable", "bc_auto_off" if s["promo_enabled"] else "bc_auto_on")]]
    if s["promo_enabled"]:
        rows.append([ibt("▶️ Resume" if s["promo_paused"] else "⏸ Pause", "bc_auto_pause")])
    rows.append([ibt(f"{'✅ ' if s['promo_interval_h'] == h else ''}{h}h", f"bc_int_{h}") for h in (1, 3, 6, 12)])
    rows.append([ibt("🔙 Back", "adm_bc")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def promo_loop(app):
    """Single scheduler. State lives in settings (promo_last_ts), so restarts neither double-send nor lose the schedule."""
    while True:
        wait = 30.0
        try:
            s = S()
            if s["promo_enabled"] and not s["promo_paused"]:
                interval = max(1, int(s["promo_interval_h"])) * 3600
                due = s.get("promo_last_ts", 0) + interval
                now = time.time()
                if now >= due:
                    s["promo_last_ts"] = now                       # persisted first -> no double run after a crash
                    app_save()
                    dests = eligible_dests(only_promo=True)
                    if dests:
                        bc = await broadcast_run(app.bot, "auto", dests, bc_id=f"auto-{int(now // interval)}")
                        bad = sum(1 for r in bc["results"].values() if r["status"] != "sent")
                        if bad:
                            await notify_admin(app.bot, f"⚠️ Auto promotion: {bad} destination(s) not delivered. See Broadcast → Destinations.")
                    continue
                wait = min(due - now, 60)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("promo loop error")
        await asyncio.sleep(max(5.0, wait))


# ═══ SETTINGS / TEMPLATE EDIT ═════════════════════════════════════════════════
_SETTING_LABELS = {
    "store_name": ("Store name", True),
    "orders_claim": ("Completed-orders text (e.g. 300+). Send - to hide the line. Only keep a number that is TRUE.", True),
    "bot_link": ("Bot link used on the promo button (https://t.me/…)", True),
    "support_link": ("Support link/contact shown on receipts", False),
    "min_topup": ("Minimum wallet top-up in ₹", False),
}


async def bc_tpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    s = S()
    text = ("✏️ <b>Promo template</b>\n\n"
            f"Store name: <code>{html.escape(s.get('store_name') or BOT_NAME)}</code>\n"
            f"Orders text: <code>{html.escape(s.get('orders_claim') or '(hidden)')}</code>\n"
            f"Bot link: <code>{html.escape(s.get('bot_link') or 'auto')}</code>\n\n"
            "⚠️ Only show an orders figure that is accurate.")
    rows = [[ibt("🏪 Store name", "set_e_store_name")], [ibt("📦 Orders text", "set_e_orders_claim")],
            [ibt("🔗 Bot link", "set_e_bot_link")], [ibt("🔙 Back", "adm_bc")]]
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def adm_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    s = S()
    text = ("⚙️ <b>Settings</b>\n\n"
            f"Store name: <code>{html.escape(s.get('store_name') or BOT_NAME)}</code>\n"
            f"Orders text: <code>{html.escape(s.get('orders_claim') or '(hidden)')}</code>\n"
            f"Bot link: <code>{html.escape(s.get('bot_link') or 'auto')}</code>\n"
            f"Support: <code>{html.escape(s.get('support_link') or '—')}</code>\n"
            f"Min top-up: <code>₹{money(s.get('min_topup', MIN_TOPUP))}</code>")
    rows = [[ibt("🏪 Store name", "set_e_store_name"), ibt("📦 Orders text", "set_e_orders_claim")],
            [ibt("🔗 Bot link", "set_e_bot_link"), ibt("🙏 Support", "set_e_support_link")],
            [ibt("💰 Min top-up", "set_e_min_topup")], [ibt("🔙 Back", "admin_panel")]]
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def set_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    key = q.data[len("set_e_"):]
    if key not in _SETTING_LABELS or not (is_admin(uid) if _SETTING_LABELS[key][1] else is_owner(uid)):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = ADMIN_SETTING_EDIT
    admin_temp[uid] = {"set_key": key}
    await q.edit_message_text(f"✏️ <b>{html.escape(_SETTING_LABELS[key][0])}</b>\n\nSend the new value:",
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_button("admin_panel")))


async def set_edit_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    key = (admin_temp.get(uid) or {}).get("set_key")
    if user_state.get(uid) != ADMIN_SETTING_EDIT or not key:
        return
    val = (update.message.text or "").strip()
    if key == "min_topup":
        try:
            v = float(val)
            if v < 1 or v > 50000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Send a number between 1 and 50000.")
            return
        S()[key] = v
    elif key == "bot_link":
        if val != "-" and not re.match(r"^https://t\.me/[A-Za-z0-9_]{4,}", val):
            await update.message.reply_text("❌ Send a link like https://t.me/YourBot (or - to reset).")
            return
        S()[key] = "" if val == "-" else val
    else:
        S()[key] = "" if val == "-" else val[:100]
    app_save()
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    await update.message.reply_text("✅ Saved.", reply_markup=InlineKeyboardMarkup(back_button("admin_panel")))


# ═══ OWNER INVENTORY CHANNEL ══════════════════════════════════════════════════
def _ch():
    return S().get("inv_channel")


def _ch_caption(pid: str, p: dict, sold_flag: bool = False) -> str:
    info = html.escape(p.get("info") or "")
    return (f"🎮 <b>FF ID {'— SOLD' if sold_flag else 'FOR SALE'}</b>\n"
            f"📌 Post ID: <code>{html.escape(pid)}</code>\n"
            f"💰 Price: <b>₹{money(p['price'])}</b>\n" + (f"ℹ️ {info}\n" if info else "") +
            ("\n❌ This ID has been sold." if sold_flag else "\n👇 Tap below to buy in the bot."))


async def channel_sync(bot, pid: str) -> str:
    """Create/update the channel post for a listing. Returns a note for the admin ('' when no channel)."""
    ch = _ch()
    p = posts.get(pid)
    if not ch or not p or not post_ready(pid):
        return ""
    markup = InlineKeyboardMarkup([[ibt_url("🛒 Buy ID", f"https://t.me/{bot.username}?start=buy_{pid}")]])
    text, photo = _ch_caption(pid, p), p.get("photo")
    try:
        if p.get("ch_msg"):
            try:
                if photo:
                    await bot.edit_message_caption(chat_id=ch["id"], message_id=p["ch_msg"], caption=text,
                                                   parse_mode="HTML", reply_markup=markup)
                else:
                    await bot.edit_message_text(chat_id=ch["id"], message_id=p["ch_msg"], text=text,
                                                parse_mode="HTML", reply_markup=markup)
                return "\n📡 Channel post updated."
            except tg_error.BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return ""
                await safe_delete(bot, ch["id"], p["ch_msg"])          # type changed / message gone -> repost
        m = (await bot.send_photo(chat_id=ch["id"], photo=photo, caption=text, parse_mode="HTML", reply_markup=markup)
             if photo else await bot.send_message(chat_id=ch["id"], text=text, parse_mode="HTML", reply_markup=markup))
        p["ch_msg"], p["bot_posted"] = m.message_id, True
        app_save()
        return "\n📡 Posted to the inventory channel."
    except Exception as exc:
        log.warning("channel sync failed for %s: %r", pid, exc)
        return f"\n⚠️ Channel update failed: {html.escape(str(exc)[:80])}"


async def channel_mark_sold(bot, pid: str, post: dict):
    ch = _ch()
    if not ch or not post or not post.get("ch_msg"):
        return
    appdata.setdefault("ch_sold_msgs", []).append(post["ch_msg"])
    try:
        if post.get("photo"):
            await bot.edit_message_caption(chat_id=ch["id"], message_id=post["ch_msg"],
                                           caption=_ch_caption(pid, post, True), parse_mode="HTML", reply_markup=None)
        else:
            await bot.edit_message_text(chat_id=ch["id"], message_id=post["ch_msg"],
                                        text=_ch_caption(pid, post, True), parse_mode="HTML", reply_markup=None)
    except Exception as exc:
        log.warning("channel sold-mark failed: %r", exc)


async def channel_remove(bot, pid: str):
    ch, p = _ch(), posts.get(pid)
    if ch and p and p.get("ch_msg"):
        await safe_delete(bot, ch["id"], p["ch_msg"])


async def adm_ch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    user_state.pop(uid, None)
    ch = _ch()
    if q.data == "ch_rm":
        S()["inv_channel"] = None
        app_save()
        ch = None
    text = ("📡 <b>Owner / Inventory Channel</b>\n\n" +
            (f"Channel: <b>{html.escape(ch['title'])}</b> (<code>{ch['id']}</code>)\n" if ch else "No channel configured.\n") +
            "\nListings are posted here automatically (with a Buy button) and marked SOLD after a sale.\n\n"
            "<b>Import from the channel:</b> post a photo with a caption containing\n"
            "<code>Post ID: FF123</code> and <code>Price: 499</code>. The bot registers it as a draft listing "
            "(no duplicates) and asks you to add the credentials. Telegram bots cannot read old channel history, "
            "so only posts made after the channel was linked are seen.")
    rows = [[ibt("✏️ Set / Change Channel", "ch_set")]]
    if ch:
        rows += [[ibt("🔍 Test Access", "ch_test"), ibt("📤 Publish All Listings", "ch_pub")], [ibt("🗑 Remove Channel", "ch_rm")]]
    rows.append([ibt("🔙 Back", "admin_panel")])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def ch_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return
    ch = _ch()
    if q.data == "ch_set":
        await q.answer()
        user_state[uid] = ADMIN_CH_SET
        await q.edit_message_text("📡 Make the bot an admin of the channel (with <b>Post messages</b>), then send its "
                                  "<code>@username</code>, a t.me link or the numeric ID.", parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(back_button("adm_ch")))
        return
    if not ch:
        await q.answer("No channel configured.", show_alert=True)
        return
    if q.data == "ch_test":
        chk = await check_dest(context.bot, ch["id"])
        await q.answer(("✅ Bot can post there." if chk["ok"] else f"❌ {chk['reason']}")[:190], show_alert=True)
        return
    if q.data == "ch_pub":
        await q.answer("Publishing…")
        n = 0
        for pid in available_ids():
            if not posts[pid].get("ch_msg"):
                await channel_sync(context.bot, pid)
                n += 1
                await asyncio.sleep(1.2)
        await q.edit_message_text(f"📤 Published <b>{n}</b> new listing(s) (existing posts are not duplicated).",
                                  parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_button("adm_ch")))


async def ch_set_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or user_state.get(uid) != ADMIN_CH_SET:
        return
    raw = (update.message.text or "").strip()
    m = re.match(r"^(?:https?://)?t\.me/(?!\+|joinchat)([A-Za-z0-9_]{4,})/?$", raw)
    ident = ("@" + m.group(1)) if m else (raw if raw.startswith("@") else (int(raw) if re.fullmatch(r"-?\d{6,}", raw) else None))
    if ident is None:
        await update.message.reply_text("❌ Send @username, a t.me link or a -100… ID.")
        return
    try:
        chat = await context.bot.get_chat(ident)
    except tg_error.TelegramError:
        await update.message.reply_text("❌ Channel not found. Add the bot as admin first.")
        return
    chk = await check_dest(context.bot, chat.id)
    if chat.type != "channel" or not chk["ok"]:
        await update.message.reply_text(f"❌ {'This is not a channel.' if chat.type != 'channel' else chk['reason']}")
        return
    S()["inv_channel"] = {"id": chat.id, "title": chat.title or str(chat.id), "username": chat.username}
    app_save()
    user_state.pop(uid, None)
    await update.message.reply_text(f"✅ Channel linked: <b>{html.escape(chat.title or '')}</b>", parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(back_button("adm_ch")))


_PID_RX = re.compile(r"(?:post\s*id|id)\s*[:#=\-]\s*([A-Za-z0-9_-]{2,32})", re.I)
_PRICE_RX = re.compile(r"(?:price|₹|rs\.?)\s*[:=]?\s*₹?\s*(\d+(?:\.\d+)?)", re.I)


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registers / updates listings from posts made in the configured channel (explicit, duplicate-free)."""
    msg = update.channel_post or update.edited_channel_post
    ch = _ch()
    if not msg or not ch or msg.chat.id != ch["id"]:
        return
    if msg.message_id in appdata.get("ch_sold_msgs", []) or any(p.get("ch_msg") == msg.message_id and p.get("bot_posted") for p in posts.values()):
        return                                           # the bot's own listing posts are never re-imported
    text = msg.caption or msg.text or ""
    mp, mpr = _PID_RX.search(text), _PRICE_RX.search(text)
    if not mp or not mpr:
        return
    pid, price = mp.group(1), float(mpr.group(1))
    if price <= 0 or pid in sold:
        return
    photo = msg.photo[-1].file_id if msg.photo else None
    info = re.sub(r"\s+", " ", _PRICE_RX.sub("", _PID_RX.sub("", text))).strip()[:300]
    existing = posts.get(pid)
    if existing:
        if existing.get("ch_msg") not in (None, msg.message_id) or pid in reserved:
            return                                       # a different channel message / payment running: do not touch
        existing.update(price=price, ch_msg=msg.message_id)
        if photo:
            existing["photo"] = photo
        if info:
            existing["info"] = info
        app_save()
        return
    posts[pid] = {"price": price, "creds": "", "photo": photo, "info": info, "created": time.time(),
                  "ch_msg": msg.message_id, "status": "draft", "imported": True}
    app_save()
    await notify_admins(context.bot, f"📥 Imported draft listing <code>{html.escape(pid)}</code> — ₹{money(price)} from the channel.\n"
                        "Add the credentials to make it purchasable.", parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[ibt("🔐 Add credentials", f"drf_{pid}")]]))


async def drf_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    pid = q.data[len("drf_"):]
    if not is_admin(uid) or pid not in posts:
        await q.answer("Not available.", show_alert=True)
        return
    await q.answer()
    admin_temp[uid] = {"edit_post_id": pid, "edit_field": "creds"}
    user_state[uid] = ADMIN_EDIT_VALUE
    await context.bot.send_message(q.message.chat_id, f"🔐 Send the credentials for <code>{html.escape(pid)}</code>:", parse_mode="HTML")


# ═══ PROMO CODE ADMIN ═════════════════════════════════════════════════════════
def parse_when(s: str):
    """'7' (days from now) or 'YYYY-MM-DD HH:MM' (IST) -> timestamp. Raises ValueError."""
    s = s.strip()
    if re.fullmatch(r"\d{1,4}", s):
        return time.time() + int(s) * 86400
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST).timestamp()
        except ValueError:
            pass
    raise ValueError


async def adm_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    promos = list(appdata.get("promos", {}).values())[-15:]
    rows = [[ibt(f"{'🟢' if p.get('enabled', True) else '🔴'} {p['code']}", f"pm_v|{p['code']}")] for p in promos]
    rows.append([ibt("✍️ New Manual Code", "pm_new_m"), ibt("🎲 New Auto Code", "pm_new_a")])
    rows.append([ibt("🔙 Back", "admin_panel")])
    await q.edit_message_text(f"🏷️ <b>Promo Codes</b>\n\nTotal: <b>{len(appdata.get('promos', {}))}</b>. Tap a code to view usage or edit it.",
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


_PW_STEPS = ["code", "type", "value", "min_price", "max_price", "min_order", "max_total", "per_user", "start", "expiry", "stack"]


async def pm_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    auto = q.data == "pm_new_a"
    admin_temp[uid] = {"pw": {"data": {"auto": auto}, "step": "type" if auto else "code"}}
    if auto:
        admin_temp[uid]["pw"]["data"]["code"] = promo_gen_code()
    user_state[uid] = ADMIN_PROMO_WIZ
    await _pw_prompt(context, uid, q.message.chat_id)


async def _pw_prompt(context, uid, chat_id):
    pw = admin_temp[uid]["pw"]
    step, d = pw["step"], pw["data"]
    skip = [ibt("⏭️ Skip", "pm_skip")]
    cancel = [ibt("❌ Cancel", "adm_promo")]
    prompts = {
        "code": ("✍️ Send the code name (3–20 characters: A–Z, 0–9, - or _):", [cancel]),
        "type": (f"Code: <code>{html.escape(d.get('code', ''))}</code>\n\nChoose the discount type:",
                 [[ibt("％ Percentage", "pm_type_percent"), ibt("₹ Fixed amount", "pm_type_fixed")], cancel]),
        "value": ("Send the discount value (percentage 1–99, or rupees):", [cancel]),
        "min_price": ("Minimum eligible ID price in ₹ (send a number, or Skip for none):", [skip, cancel]),
        "max_price": ("Maximum eligible ID price in ₹ (number, or Skip for no maximum):", [skip, cancel]),
        "min_order": ("Minimum order value in ₹ (number, or Skip):", [skip, cancel]),
        "max_total": ("Maximum total redemptions (whole number, or Skip for unlimited):", [skip, cancel]),
        "per_user": ("Maximum redemptions per user (whole number, or Skip for 1):", [skip, cancel]),
        "start": ("Start date/time in IST — <code>YYYY-MM-DD HH:MM</code> (or Skip to start now):", [skip, cancel]),
        "expiry": ("Expiry — <code>YYYY-MM-DD HH:MM</code> IST, or a number of days from now (or Skip for no expiry):", [skip, cancel]),
        "stack": ("Can this code be combined with other discounts?\n<i>(The store currently has no other discounts; the flag is saved for the future.)</i>",
                  [[ibt("✅ Yes", "pm_stack_y"), ibt("🚫 No", "pm_stack_n")], cancel]),
    }
    text, rows = prompts[step]
    await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def _pw_advance(context, uid, chat_id):
    pw = admin_temp[uid]["pw"]
    i = _PW_STEPS.index(pw["step"]) + 1
    if i >= len(_PW_STEPS):
        d = pw["data"]
        code = d["code"]
        promos = appdata.setdefault("promos", {})
        if code in promos:
            await context.bot.send_message(chat_id, "❌ That code already exists.")
            return
        promos[code] = {"code": code, "type": d["type"], "value": d["value"], "min_price": d.get("min_price", 0),
                        "max_price": d.get("max_price"), "min_order": d.get("min_order"), "max_total": d.get("max_total"),
                        "max_per_user": d.get("per_user", 1), "start": d.get("start"), "expiry": d.get("expiry"),
                        "enabled": True, "stackable": d.get("stack", False), "category": None,
                        "created": time.time(), "created_by": uid, "auto": d.get("auto", False)}
        app_save()
        user_state.pop(uid, None)
        admin_temp.pop(uid, None)
        await context.bot.send_message(chat_id, "✅ <b>Promo code created</b>\n\n" + promo_describe(promos[code]),
                                       parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_button("adm_promo")))
        return
    pw["step"] = _PW_STEPS[i]
    await _pw_prompt(context, uid, chat_id)


async def pm_wizard_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    pw = (admin_temp.get(uid) or {}).get("pw")
    if not is_admin(uid) or not pw:
        await q.answer()
        return
    await q.answer()
    d, step = pw["data"], pw["step"]
    if q.data.startswith("pm_type_") and step == "type":
        d["type"] = q.data[len("pm_type_"):]
    elif q.data.startswith("pm_stack_") and step == "stack":
        d["stack"] = q.data.endswith("_y")
    elif q.data == "pm_skip" and step in ("min_price", "max_price", "min_order", "max_total", "per_user", "start", "expiry"):
        pass
    else:
        return
    await safe_delete(context.bot, q.message.chat_id, q.message.message_id)
    await _pw_advance(context, uid, q.message.chat_id)


async def pm_wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pw = (admin_temp.get(uid) or {}).get("pw")
    if not is_admin(uid) or user_state.get(uid) != ADMIN_PROMO_WIZ or not pw:
        return
    step, d, raw = pw["step"], pw["data"], (update.message.text or "").strip()
    reply = update.message.reply_text
    try:
        if step == "code":
            code = promo_normalize(raw)
            if not PROMO_RE.match(code):
                await reply("❌ Use 3–20 characters: A–Z, 0–9, - or _.")
                return
            if code in appdata.get("promos", {}):
                await reply("❌ That code already exists. Choose another.")
                return
            d["code"] = code
        elif step == "value":
            v = float(raw)
            if d["type"] == "percent" and not (1 <= v <= 99) or d["type"] == "fixed" and not (0 < v <= 100000):
                raise ValueError
            d["value"] = round(v, 2)
        elif step in ("min_price", "max_price", "min_order"):
            v = float(raw)
            if v < 0 or v > 1_000_000 or (step == "max_price" and v < float(d.get("min_price") or 0)):
                raise ValueError
            d[step] = v
        elif step in ("max_total", "per_user"):
            v = int(raw)
            if v < 1 or v > 1_000_000:
                raise ValueError
            d[step] = v
        elif step in ("start", "expiry"):
            ts = parse_when(raw)
            if step == "expiry" and ts <= (d.get("start") or time.time()):
                raise ValueError
            d[step] = ts
        else:
            return
    except ValueError:
        await reply("❌ Invalid value. Please check the format and try again.")
        return
    await _pw_advance(context, uid, update.effective_chat.id)


async def pm_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state.pop(uid, None)
    code = q.data.split("|", 1)[1]
    p = appdata.get("promos", {}).get(code)
    if not p:
        await q.edit_message_text("Code not found.", reply_markup=InlineKeyboardMarkup(back_button("adm_promo")))
        return
    reds = [r for r in appdata.get("redemptions", []) if r["code"] == code]
    holds = [h for h in appdata.get("holds", {}).values() if h["code"] == code]
    last = "\n".join(f"• {html.escape(fmt_dt(r['ts']))} — user <code>{r['uid']}</code> — −₹{money(r['discount'])}" for r in reds[-5:]) or "none"
    text = (f"🏷️ <b>Promo details</b>\n\n{promo_describe(p)}\n\nRedeemed: <b>{len(reds)}</b> · in progress: <b>{len(holds)}</b>\n"
            f"Combinable: {'yes' if p.get('stackable') else 'no'} · Created {html.escape(fmt_dt(p['created']))}\n\n<b>Last redemptions</b>\n{last}")
    rows = [[ibt("🔴 Disable" if p.get("enabled", True) else "🟢 Enable", f"pm_t|{code}")],
            [ibt("✏️ Value", f"pm_e|value|{code}"), ibt("✏️ Max total", f"pm_e|max_total|{code}")],
            [ibt("✏️ Per user", f"pm_e|max_per_user|{code}"), ibt("✏️ Expiry", f"pm_e|expiry|{code}")],
            [ibt("🔙 Back", "adm_promo")]]
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def pm_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    code = q.data.split("|", 1)[1]
    p = appdata.get("promos", {}).get(code)
    if p:
        p["enabled"] = not p.get("enabled", True)
        app_save()
    q.data = f"pm_v|{code}"
    await pm_view(update, context)


async def pm_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    _, field, code = q.data.split("|", 2)
    user_state[uid] = ADMIN_PROMO_EDIT
    admin_temp[uid] = {"pe": {"field": field, "code": code}}
    hint = {"value": "the new discount value", "max_total": "the new total limit (number, or - for unlimited)",
            "max_per_user": "the new per-user limit (number)", "expiry": "a new expiry (YYYY-MM-DD HH:MM IST, days, or - for none)"}[field]
    await context.bot.send_message(q.message.chat_id, f"✏️ Send {hint}:")


async def pm_edit_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pe = (admin_temp.get(uid) or {}).get("pe")
    if not is_admin(uid) or user_state.get(uid) != ADMIN_PROMO_EDIT or not pe:
        return
    p = appdata.get("promos", {}).get(pe["code"])
    raw = (update.message.text or "").strip()
    if not p:
        user_state.pop(uid, None)
        return
    try:
        f = pe["field"]
        if f == "value":
            v = float(raw)
            if p["type"] == "percent" and not (1 <= v <= 99) or p["type"] == "fixed" and not (0 < v <= 100000):
                raise ValueError
            p["value"] = round(v, 2)
        elif f in ("max_total", "max_per_user"):
            if raw == "-" and f == "max_total":
                p["max_total"] = None
            else:
                v = int(raw)
                if v < 1:
                    raise ValueError
                p[f] = v
        else:
            p["expiry"] = None if raw == "-" else parse_when(raw)
    except ValueError:
        await update.message.reply_text("❌ Invalid value, try again.")
        return
    app_save()
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    await update.message.reply_text("✅ Updated.\n\n" + promo_describe(p), parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(back_button("adm_promo")))

# ═══ SELLER FLOW (users sell their Free Fire ID to the store) ═════════════════
SELL_MIN_PHOTOS, SELL_MAX_PHOTOS = 5, 20
SELL_PLATFORMS = {"google": "Google", "facebook": "Facebook", "twitter": "Twitter / X", "other": "Other"}


def _subs() -> dict:
    return appdata.setdefault("submissions", {})


async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if await fj_gate(update, context):
        return
    await cleanup_flow(context.bot, uid)
    open_n = sum(1 for s in _subs().values() if s["uid"] == uid and s["status"] in ("pending", "quoted", "accepted"))
    if open_n >= 3:
        await send_tracked(context.bot, uid, chat_id, "⚠️ You already have 3 open submissions. Please wait for them to be reviewed.", parse_mode="HTML")
        return
    sell_draft[uid] = {"photos": [], "uniq": set(), "platform": None, "login": None, "pw": None, "extra": "",
                       "msg": None, "last_edit": 0.0}
    user_state[uid] = SELL_PHOTOS
    log_event(uid, "sell_start")
    m = await send_tracked(
        context.bot, uid, chat_id,
        "💸 <b>SELL YOUR FREE FIRE ID</b>\n\n"
        f"1️⃣ Send <b>{SELL_MIN_PHOTOS} to {SELL_MAX_PHOTOS} photos</b> of your ID (lobby, level, characters, items…). "
        "Send them one by one or as an album.\n"
        "2️⃣ Then choose the login type and enter the login details.\n"
        "3️⃣ Our admin reviews it and sends you a price offer. You accept or decline.\n\n"
        f"📸 Photos received: <b>0/{SELL_MAX_PHOTOS}</b>",
        parse_mode="HTML", reply_markup=_sell_photo_markup(0))
    sell_draft[uid]["msg"] = m.message_id


def _sell_photo_markup(n: int) -> InlineKeyboardMarkup:
    rows = []
    if n >= SELL_MIN_PHOTOS:
        rows.append([ibt(f"✅ Done ({n} photos)", "sell_done")])
    rows.append([ibt("❌ Cancel", "sell_cancel")])
    return InlineKeyboardMarkup(rows)


async def sell_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    d = sell_draft.get(uid)
    if user_state.get(uid) != SELL_PHOTOS or not d:
        return False
    ph = update.message.photo[-1]
    if ph.file_unique_id in d["uniq"]:
        return True
    if len(d["photos"]) >= SELL_MAX_PHOTOS:
        if not d.get("warned"):
            d["warned"] = True
            await update.message.reply_text(f"⚠️ Maximum {SELL_MAX_PHOTOS} photos reached. Tap Done to continue.")
        return True
    d["uniq"].add(ph.file_unique_id)
    d["photos"].append(ph.file_id)
    n = len(d["photos"])
    if time.time() - d["last_edit"] > 1.2 or n in (SELL_MIN_PHOTOS, SELL_MAX_PHOTOS):
        d["last_edit"] = time.time()
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=d["msg"], parse_mode="HTML",
                text=("💸 <b>SELL YOUR FREE FIRE ID</b>\n\n"
                      f"📸 Photos received: <b>{n}/{SELL_MAX_PHOTOS}</b>\n"
                      + (f"Send more, or tap <b>Done</b>." if n >= SELL_MIN_PHOTOS else f"Send at least {SELL_MIN_PHOTOS - n} more.")),
                reply_markup=_sell_photo_markup(n))
        except Exception:
            pass
    return True


async def sell_done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    d = sell_draft.get(uid)
    if not d or user_state.get(uid) != SELL_PHOTOS:
        await q.answer("This session has ended. Start again from the menu.", show_alert=True)
        return
    if len(d["photos"]) < SELL_MIN_PHOTOS:
        await q.answer(f"Send at least {SELL_MIN_PHOTOS} photos first.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = "SELL_PLATFORM"
    rows = [[ibt(label, f"sell_pf_{k}")] for k, label in SELL_PLATFORMS.items()]
    rows.append([ibt("❌ Cancel", "sell_cancel")])
    await q.edit_message_text(f"✅ <b>{len(d['photos'])} photos saved.</b>\n\n🔑 Which account is the Free Fire ID linked to?",
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def sell_platform_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    d = sell_draft.get(uid)
    if not d or user_state.get(uid) != "SELL_PLATFORM":
        await q.answer("This session has ended.", show_alert=True)
        return
    await q.answer()
    d["platform"] = SELL_PLATFORMS.get(q.data[len("sell_pf_"):], "Other")
    user_state[uid] = SELL_LOGIN_ID
    await q.edit_message_text(f"📧 <b>{html.escape(d['platform'])} login</b>\n\nSend the <b>email ID or mobile number</b> of the account:",
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[ibt("❌ Cancel", "sell_cancel")]]))


async def sell_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st, d = user_state.get(uid), sell_draft.get(uid)
    if not d:
        user_state.pop(uid, None)
        return
    chat_id, raw = update.effective_chat.id, (update.message.text or "").strip()
    cancel = InlineKeyboardMarkup([[ibt("❌ Cancel", "sell_cancel")]])
    if st == SELL_LOGIN_ID:
        if not (3 <= len(raw) <= 120):
            await update.message.reply_text("❌ Send a valid email ID or mobile number.")
            return
        d["login"] = raw
        user_state[uid] = SELL_PASSWORD
        await safe_delete(context.bot, chat_id, update.message.message_id)   # your own login message, removed from your chat for safety
        await send_tracked(context.bot, uid, chat_id, "🔐 Now send the <b>password</b> of that account:", parse_mode="HTML", reply_markup=cancel)
    elif st == SELL_PASSWORD:
        if not (1 <= len(raw) <= 120):
            await update.message.reply_text("❌ Send the password (max 120 characters).")
            return
        d["pw"] = raw
        user_state[uid] = SELL_EXTRA
        await safe_delete(context.bot, chat_id, update.message.message_id)
        await send_tracked(context.bot, uid, chat_id,
                           "ℹ️ Add any details (level, rank, skins, guns…) or tap Skip.", parse_mode="HTML",
                           reply_markup=InlineKeyboardMarkup([[ibt("⏭️ Skip", "sell_skip")], [ibt("❌ Cancel", "sell_cancel")]]))
    elif st == SELL_EXTRA:
        d["extra"] = raw[:400]
        await _sell_confirm(context, uid, chat_id)


async def sell_skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if user_state.get(uid) != SELL_EXTRA or uid not in sell_draft:
        await q.answer()
        return
    await q.answer()
    await _sell_confirm(context, uid, q.message.chat_id)


async def _sell_confirm(context, uid, chat_id):
    d = sell_draft[uid]
    user_state[uid] = "SELL_CONFIRM"
    await send_tracked(
        context.bot, uid, chat_id,
        "📋 <b>CHECK YOUR SUBMISSION</b>\n\n"
        f"📸 Photos: <b>{len(d['photos'])}</b>\n🔑 Login type: <b>{html.escape(d['platform'])}</b>\n"
        f"📧 Login: <code>{html.escape(d['login'])}</code>\n🔐 Password: <code>{'•' * min(len(d['pw']), 12)}</code>\n"
        f"ℹ️ Details: {html.escape(d['extra'] or '—')}\n\n"
        "By submitting you confirm that you own this account and are allowed to sell it. "
        "The details are sent only to our admins.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[ibt("✅ Submit", "sell_submit")], [ibt("❌ Cancel", "sell_cancel")]]))


async def sell_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer("Cancelled")
    sell_draft.pop(uid, None)
    user_state.pop(uid, None)
    await cleanup_flow(context.bot, uid)
    if q.message and q.message.message_id != welcome_msg.get(uid):
        await safe_delete(context.bot, q.message.chat_id, q.message.message_id)


def _sub_text(s: dict, with_creds: bool) -> str:
    e = html.escape
    t = (f"💼 <b>Seller submission {e(s['id'])}</b> — <b>{e(s['status'])}</b>\n"
         f"👤 {e(s.get('name') or '')} @{e(s.get('username') or '—')} (<code>{s['uid']}</code>)\n"
         f"📅 {e(fmt_dt(s['ts']))}\n📸 Photos: {len(s['photos'])}\n🔑 {e(s['platform'])}\n")
    if with_creds:
        login, pw = vault_dec(s.get("login")), vault_dec(s.get("pw"))
        t += f"📧 Login: <code>{e(login) if login else 'wiped / unavailable'}</code>\n🔐 Password: <code>{e(pw) if pw else 'wiped / unavailable'}</code>\n"
    t += f"ℹ️ {e(s.get('extra') or '—')}\n"
    if s.get("offer"):
        t += f"💰 Offer: ₹{money(s['offer'])}\n"
    if s.get("post_id"):
        t += f"📌 Listed as: <code>{e(s['post_id'])}</code>\n"
    return t


def _sub_buttons(s: dict) -> InlineKeyboardMarkup:
    sid, rows = s["id"], []
    if s["status"] in ("pending", "quoted"):
        rows.append([ibt("💰 Quote Price" if s["status"] == "pending" else "💰 Change Offer", f"sq_{sid}"), ibt("❌ Reject", f"sr_{sid}")])
    if s["status"] == "accepted":
        rows.append([ibt("📋 List This ID", f"sl_{sid}")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([[ibt("🔙 Back", "adm_subs")]])


async def sell_submit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    d = sell_draft.get(uid)
    if not d or user_state.get(uid) != "SELL_CONFIRM":
        await q.answer("This session has ended.", show_alert=True)
        return
    await q.answer("Submitted!")
    appdata["sub_seq"] = appdata.get("sub_seq", 0) + 1
    sid = f"SUB{appdata['sub_seq']:04d}"
    u = q.from_user
    s = {"id": sid, "uid": uid, "name": u.full_name, "username": u.username or "", "photos": d["photos"],
         "platform": d["platform"], "login": vault_enc(d["login"]), "pw": vault_enc(d["pw"]), "extra": d["extra"],
         "status": "pending", "ts": time.time(), "offer": None}
    _subs()[sid] = s
    app_save()
    sell_draft.pop(uid, None)
    user_state.pop(uid, None)
    log_event(uid, "sell_submitted", sid)
    await cleanup_flow(context.bot, uid)
    if q.message and q.message.message_id != welcome_msg.get(uid):
        await safe_delete(context.bot, q.message.chat_id, q.message.message_id)
    await context.bot.send_message(q.message.chat_id,
                                   f"✅ <b>Submitted!</b>\n\nReference: <code>{sid}</code>\nOur admin will review your ID and send you a price offer here.",
                                   parse_mode="HTML")
    for aid in all_admin_ids():
        try:
            for i in range(0, len(s["photos"]), 10):
                await context.bot.send_media_group(aid, [InputMediaPhoto(f) for f in s["photos"][i:i + 10]])
        except Exception:
            log.exception("could not send submission photos to admin %s", aid)     # the details below must still arrive
        try:
            await context.bot.send_message(aid, "📥 <b>NEW SELLER SUBMISSION</b>\n\n" + _sub_text(s, True), parse_mode="HTML",
                                           reply_markup=_sub_buttons(s))
        except Exception:
            log.exception("could not deliver submission to admin %s", aid)


async def adm_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    subs = sorted(_subs().values(), key=lambda s: s["ts"], reverse=True)[:15]
    rows = [[ibt(f"{s['id']} · {s['status']} · {(s.get('username') or s['name'] or '')[:12]}", f"sv_{s['id']}")] for s in subs]
    rows.append([ibt("🔙 Back", "admin_panel")])
    await q.edit_message_text("💼 <b>Seller Submissions</b>\n\n" + ("Tap one to open it." if subs else "No submissions yet."),
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def sub_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    s = _subs().get(q.data[len("sv_"):])
    if not is_admin(q.from_user.id) or not s:
        await q.answer("Not found.", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text(_sub_text(s, s["status"] in ("pending", "quoted", "accepted")), parse_mode="HTML",
                              reply_markup=InlineKeyboardMarkup(list(_sub_buttons(s).inline_keyboard) + [[ibt("🔙 Back", "adm_subs")]]))


async def sub_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sq_ (quote), sr_ (reject), sl_ (list) buttons."""
    q = update.callback_query
    uid = q.from_user.id
    act, sid = q.data[:2], q.data[3:]
    s = _subs().get(sid)
    if not is_admin(uid) or not s:
        await q.answer("Not found.", show_alert=True)
        return
    ok_status = {"sq": ("pending", "quoted"), "sr": ("pending", "quoted"), "sl": ("accepted",)}[act]
    if s["status"] not in ok_status:
        await q.answer(f"Already {s['status']}.", show_alert=True)
        return
    await q.answer()
    admin_temp[uid] = {"sid": sid}
    if act == "sq":
        user_state[uid] = ADMIN_QUOTE_PRICE
        txt = f"💰 Send the <b>price offer in ₹</b> for {sid} (numbers only):"
    elif act == "sr":
        user_state[uid] = ADMIN_REJECT_REASON
        txt = f"❌ Send a short reason for rejecting {sid} (or - for none):"
    else:
        user_state[uid] = ADMIN_LIST_PRICE
        txt = f"📋 Send the <b>selling price in ₹</b> to list {sid} for buyers:"
    await context.bot.send_message(q.message.chat_id, txt, parse_mode="HTML")


def _wipe_sub(s: dict):
    s["login"] = s["pw"] = None


async def sub_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = user_state.get(uid)
    sid = (admin_temp.get(uid) or {}).get("sid")
    s = _subs().get(sid or "")
    if not is_admin(uid) or not s:
        user_state.pop(uid, None)
        return
    raw = (update.message.text or "").strip()
    if st in (ADMIN_QUOTE_PRICE, ADMIN_LIST_PRICE):
        try:
            price = round(float(raw), 2)
            if price <= 0 or price > 1_000_000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Send a number greater than 0.")
            return
    if st == ADMIN_QUOTE_PRICE:
        if s["status"] not in ("pending", "quoted"):
            user_state.pop(uid, None)
            return
        s["offer"], s["status"] = price, "quoted"
        app_save()
        user_state.pop(uid, None)
        try:
            await context.bot.send_message(
                s["uid"], f"💰 <b>PRICE OFFER</b> for submission <code>{sid}</code>\n\nWe can buy your ID for <b>₹{money(price)}</b>.\nDo you accept?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[ibt("✅ Accept", f"so_a_{sid}"), ibt("❌ Decline", f"so_d_{sid}")]]))
            note = "The seller has been notified."
        except Exception:
            note = "⚠️ Could not message the seller (they may have blocked the bot)."
        await update.message.reply_text(f"✅ Offer ₹{money(price)} saved for {sid}. {note}")
    elif st == ADMIN_REJECT_REASON:
        s["status"] = "rejected"
        _wipe_sub(s)
        app_save()
        user_state.pop(uid, None)
        try:
            await context.bot.send_message(s["uid"], f"❌ Submission <code>{sid}</code> was not accepted."
                                           + (f"\nReason: {html.escape(raw)}" if raw and raw != "-" else ""), parse_mode="HTML")
        except Exception:
            pass
        await update.message.reply_text(f"✅ {sid} rejected and the stored login details were wiped.")
    elif st == ADMIN_LIST_PRICE:
        if s["status"] != "accepted":
            user_state.pop(uid, None)
            return
        login, pw = vault_dec(s.get("login")), vault_dec(s.get("pw"))
        if not login or not pw:
            await update.message.reply_text("❌ The stored login details are unavailable (encryption key changed?).")
            user_state.pop(uid, None)
            return
        pid = gen_post_id()
        posts[pid] = {"price": price, "creds": f"Login type: {s['platform']}\nLogin: {login}\nPassword: {pw}",
                      "photo": s["photos"][0], "info": (s.get("extra") or "")[:300], "created": time.time(),
                      "ch_msg": None, "status": "live", "from_submission": sid}
        s["status"], s["post_id"] = "listed", pid
        _wipe_sub(s)                                   # credentials now live only in the (encrypted) listing
        app_save()
        user_state.pop(uid, None)
        admin_temp.pop(uid, None)
        note = await channel_sync(context.bot, pid)
        try:
            await context.bot.send_message(s["uid"], f"🎉 Your ID (<code>{sid}</code>) is now listed for sale. Thank you!", parse_mode="HTML")
        except Exception:
            pass
        await update.message.reply_text(f"✅ Listed as <code>{pid}</code> at ₹{money(price)}.{note}", parse_mode="HTML")


async def sell_offer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Seller accepts / declines the admin's price offer."""
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split("_")
    accept, sid = parts[1] == "a", "_".join(parts[2:])
    s = _subs().get(sid)
    if not s or s["uid"] != uid or s["status"] != "quoted":
        await q.answer("This offer is no longer active.", show_alert=True)
        return
    await q.answer()
    if accept:
        s["status"] = "accepted"
        app_save()
        await q.edit_message_text(f"✅ You accepted ₹{money(s['offer'])} for <code>{sid}</code>. Our admin will list the ID and contact you.",
                                  parse_mode="HTML")
        await notify_admins(context.bot, f"✅ Seller accepted ₹{money(s['offer'])} for {sid}. List it now:",
                            reply_markup=InlineKeyboardMarkup([[ibt("📋 List This ID", f"sl_{sid}")]]))
    else:
        s["status"] = "declined"
        _wipe_sub(s)
        app_save()
        await q.edit_message_text(f"❌ You declined the offer for <code>{sid}</code>. Your login details were deleted from our system.",
                                  parse_mode="HTML")
        await notify_admins(context.bot, f"❌ Seller declined the offer for {sid}.")


# ═══ BACKUP / RESTORE ═════════════════════════════════════════════════════════
BACKUP_FORMAT, BACKUP_VERSION = "ffstore-backup", 1


def _export_state() -> dict:
    """Everything worth restoring. No bot token / API keys / env secrets. Credentials appear only as ciphertext;
    credentials that are not encrypted (no `cryptography`) are omitted entirely."""
    app = {k: v for k, v in appdata.items() if k != "holds"}
    pout = {}
    for pid, p in posts.items():
        enc = vault_enc(p.get("creds", ""))
        pout[pid] = {**p, "creds": None if enc.startswith("plain:") else enc}
    sub_out = {}
    for sid, s in _subs().items():
        s2 = dict(s)
        for k in ("login", "pw"):
            if isinstance(s2.get(k), str) and s2[k].startswith("plain:"):
                s2[k] = None
        sub_out[sid] = s2
    app = {**app, "posts": pout, "sold": sorted(sold), "submissions": sub_out}
    return {"users": {str(k): v for k, v in users.items()}, "super_admins": {str(k): v for k, v in super_admins.items()},
            "force_join": list(force_join), "app": app}


def _canon(data) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf8")


def build_backup() -> tuple[bytes, dict]:
    data = json.loads(_canon(_export_state()))
    doc = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION, "bot_version": BOT_VERSION,
           "created": datetime.now(IST).isoformat(timespec="seconds"), "sha256": hashlib.sha256(_canon(data)).hexdigest(), "data": data}
    raw = gzip.compress(json.dumps(doc, ensure_ascii=False, default=str).encode("utf8"))
    return raw, doc


def validate_backup(raw: bytes) -> dict:
    try:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
        doc = json.loads(raw.decode("utf8"))
    except Exception:
        raise ValueError("The file is not a valid backup (cannot be read).")
    if not isinstance(doc, dict) or doc.get("format") != BACKUP_FORMAT:
        raise ValueError("This is not a backup made by this bot.")
    if int(doc.get("version", 0)) > BACKUP_VERSION:
        raise ValueError("This backup was made by a newer version of the bot.")
    data = doc.get("data")
    if not isinstance(data, dict) or not all(k in data for k in ("users", "super_admins", "force_join", "app")):
        raise ValueError("The backup is missing required sections.")
    if hashlib.sha256(_canon(data)).hexdigest() != doc.get("sha256"):
        raise ValueError("Integrity check failed — the file was modified or corrupted.")
    if not isinstance(data["app"], dict) or not isinstance(data["users"], dict):
        raise ValueError("The backup structure is invalid.")
    return doc


def _prune_local():
    try:
        files = sorted((f for f in os.listdir(BACKUP_DIR) if f.endswith(".json.gz")),
                       key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)))
        for f in files[:-BACKUP_KEEP_LOCAL]:
            os.remove(os.path.join(BACKUP_DIR, f))
    except Exception:
        log.exception("local backup pruning failed")


def activity_summary(since: float) -> str:
    e = html.escape
    now = time.time()
    invs = [i for i in appdata.get("invoices", {}).values() if i["ts"] >= since]
    purchases = [i for i in invs if i.get("type", "purchase") == "purchase"]
    topups = [i for i in invs if i.get("type") == "topup"]
    new_users = [(u, r) for u, r in users.items() if r.get("joined", 0) >= since]
    subs_new = [s for s in _subs().values() if s["ts"] >= since]
    act = [a for a in appdata.get("activity", []) if a["ts"] >= since]
    by_user = {}
    for a in act:
        by_user[a["uid"]] = a
    lines = [
        f"🕒 {e(fmt_dt(now))} · window: last {max(1, round((now - since) / 3600, 1))}h",
        f"🛒 Paid orders: <b>{len(purchases)}</b> (₹{money(sum(float(i.get('paid', 0)) for i in purchases))}) · 💰 Top-ups: <b>{len(topups)}</b> (₹{money(sum(float(i.get('paid', 0)) for i in topups))})",
        f"⏳ Pending orders now: <b>{len(pending_orders)}</b> · 👤 New users: <b>{len(new_users)}</b> · 💼 New submissions: <b>{len(subs_new)}</b>",
        f"📊 Totals — users {len(users)} · available IDs {len(available_ids())} · sold {len(sold)} · wallet balances ₹{money(sum(float(r.get('balance', 0)) for r in users.values()))}",
        "", "👥 <b>Active users (how far each one got)</b>",
    ]
    for uid, a in list(by_user.items())[-20:]:
        r = users.get(uid, {})
        lines.append(f"• {e(_uname(r))} <code>{uid}</code> — {e(a['a'])}" + (f" ({e(a['d'])})" if a["d"] else "") + f" · {e(datetime.fromtimestamp(a['ts'], IST).strftime('%H:%M'))}")
    if not by_user:
        lines.append("• no activity in this window")
    asks = [a for a in act if a["a"] in ("message", "promo_try", "search_post_id")][-12:]
    if asks:
        lines += ["", "💬 <b>What users typed / asked</b>"]
        lines += [f"• {e(_uname(users.get(a['uid'], {})))}: {e(a['a'])} — {e(a['d'])}" for a in asks]
    return "\n".join(lines)


async def _send_long(bot, chat_id, text: str):
    while text:
        cut = text.rfind("\n", 0, 3900) if len(text) > 3900 else len(text)
        cut = cut if cut > 0 else min(len(text), 3900)
        await bot.send_message(chat_id, text[:cut], parse_mode="HTML")
        text = text[cut:].lstrip("\n")


async def run_backup(bot, reason: str = "scheduled") -> tuple[bool, str]:
    s = S()
    chat = s.get("backup_chat")
    since = s.get("backup_last_ts") or (time.time() - 7200)
    try:
        raw, doc = build_backup()
    except Exception as exc:
        log.exception("backup build failed")
        return False, f"could not build the backup: {exc!r}"[:200]
    name = f"ffstore-backup-{datetime.now(IST):%Y%m%d-%H%M%S}.json.gz"
    try:                                                       # always keep a local copy too
        _atomic_write(os.path.join(BACKUP_DIR, name), raw)
        _prune_local()
    except Exception:
        log.exception("local backup write failed")
    if not chat:
        return False, "no backup chat configured (saved locally only)"
    chk = await check_dest(bot, chat)
    if not chk["ok"]:
        return False, f"cannot post to the backup chat: {chk['reason']}"
    if len(raw) > 45 * 1024 * 1024:
        return False, "backup file is larger than Telegram's 50 MB limit (saved locally only)"
    ids = []
    try:
        m1 = await bot.send_message(chat, f"💾 <b>BACKUP ({html.escape(reason)})</b>\n\n" + activity_summary(since)[:3700], parse_mode="HTML")
        ids.append(m1.message_id)
        m2 = await bot.send_document(chat, document=io.BytesIO(raw), filename=name, caption=(
            f"🗂 {name}\nSHA-256: {doc['sha256'][:16]}… · v{BOT_VERSION}\n"
            "Contains encrypted credentials — keep DATA_KEY safe; restore with Admin → Backup & Restore."))
        ids.append(m2.message_id)
    except Exception as exc:
        log.exception("backup send failed")
        return False, f"sending failed: {str(exc)[:150]}"
    s["backup_last_ts"] = time.time()
    msgs = s.setdefault("backup_msgs", [])
    msgs.append({"chat": chat, "ids": ids, "ts": time.time()})
    while len(msgs) > BACKUP_KEEP_TELEGRAM:                    # retention on the Telegram side (best effort)
        old = msgs.pop(0)
        for mid in old["ids"]:
            await safe_delete(bot, old["chat"], mid)
    app_save()
    return True, f"{name} ({len(raw) // 1024} KB)"


async def backup_loop(app):
    fails = 0
    while True:
        wait = 60.0
        try:
            s = S()
            if s.get("backup_chat") and s.get("backup_enabled"):
                interval = int(float(s.get("backup_interval_h", 2)) * 3600)
                due = s.get("backup_last_ts", 0) + interval
                if time.time() >= due:
                    ok, msg = await run_backup(app.bot, "scheduled")
                    if ok:
                        fails = 0
                    else:
                        fails += 1
                        s["backup_last_ts"] = time.time() - interval + 600       # retry in ~10 minutes
                        if fails in (1, 6):
                            await notify_admin(app.bot, f"⚠️ Scheduled backup FAILED: {msg}")
                    continue
                wait = min(due - time.time(), 60)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("backup loop error")
        await asyncio.sleep(max(5.0, wait))


async def adm_bk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return
    s, d = S(), q.data
    user_state.pop(uid, None)
    if d == "bk_tog":
        s["backup_enabled"] = not s["backup_enabled"]
        app_save()
    elif d.startswith("bk_int_"):
        s["backup_interval_h"] = int(d[len("bk_int_"):])
        app_save()
    elif d == "bk_now":
        await q.answer("Backing up…")
        ok, msg = await run_backup(context.bot, "manual")
        await q.edit_message_text(("✅ Backup sent: " if ok else "❌ Backup failed: ") + html.escape(msg), parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(back_button("adm_bk")))
        return
    await q.answer()
    try:
        n_local = len([f for f in os.listdir(BACKUP_DIR) if f.endswith(".json.gz")])
    except OSError:
        n_local = 0
    last = fmt_dt(s["backup_last_ts"]) if s.get("backup_last_ts") else "never"
    text = ("💾 <b>Backup &amp; Restore</b>\n\n"
            f"Backup chat: <code>{s.get('backup_chat') or 'not set'}</code>\n"
            f"Automatic: <b>{'ON' if s['backup_enabled'] else 'OFF'}</b> every <b>{s['backup_interval_h']}h</b>\n"
            f"Last backup: {html.escape(last)}\nLocal copies: {n_local} (keeps {BACKUP_KEEP_LOCAL})\n"
            f"Encryption available: <b>{'yes' if _vault() else 'NO — install cryptography'}</b>\n\n"
            "Each backup = a summary message (orders, users and where they got to) + a verified, versioned file. "
            "Make the bot an admin in a <b>private</b> group first.")
    rows = [[ibt("✏️ Set Backup Chat", "bk_set"), ibt("💾 Backup Now", "bk_now")],
            [ibt("🔴 Turn OFF" if s["backup_enabled"] else "🟢 Turn ON", "bk_tog")],
            [ibt(f"{'✅ ' if s['backup_interval_h'] == h else ''}{h}h", f"bk_int_{h}") for h in (1, 2, 6, 12)],
            [ibt("♻️ Restore", "bk_restore")], [ibt("🔙 Back", "admin_panel")]]
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def bk_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = ADMIN_BK_SET
    await q.edit_message_text("✏️ Create a <b>private</b> group, add the bot and make it admin, then send the group's numeric ID "
                              "(<code>-100…</code>) or <code>@username</code>.\nTip: add @userinfobot / @getidsbot to the group to see its ID.",
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_button("adm_bk")))


async def bk_set_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or user_state.get(uid) != ADMIN_BK_SET:
        return
    raw = (update.message.text or "").strip()
    ident = raw if raw.startswith("@") else (int(raw) if re.fullmatch(r"-?\d{6,}", raw) else None)
    if ident is None:
        await update.message.reply_text("❌ Send a numeric ID (-100…) or @username.")
        return
    chk = await check_dest(context.bot, ident)
    if not chk["ok"]:
        await update.message.reply_text(f"❌ The bot cannot post there: {chk['reason']}")
        return
    chat = await context.bot.get_chat(ident)
    S()["backup_chat"] = chat.id
    app_save()
    user_state.pop(uid, None)
    warn = "\n⚠️ This chat is PUBLIC — backups hold user data. Use a private group." if chat.username else ""
    await update.message.reply_text(f"✅ Backup chat set: <b>{html.escape(chat.title or '')}</b>.{warn}\nSending a first backup…", parse_mode="HTML")
    ok, msg = await run_backup(context.bot, "first backup")
    await update.message.reply_text(("✅ " if ok else "❌ ") + html.escape(msg), parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(back_button("adm_bk")))


async def bk_restore_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = ADMIN_RESTORE_FILE
    await q.edit_message_text("♻️ <b>Restore</b>\n\nUpload a backup file (<code>.json.gz</code>) in this chat. It is fully validated first; "
                              "nothing changes until you confirm.", parse_mode="HTML",
                              reply_markup=InlineKeyboardMarkup(back_button("adm_bk")))


async def restore_file_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or user_state.get(uid) != ADMIN_RESTORE_FILE:
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 30 * 1024 * 1024:
        await update.message.reply_text("❌ File too large (limit 30 MB).")
        return
    try:
        raw = bytes(await (await context.bot.get_file(doc.file_id)).download_as_bytearray())
        parsed = validate_backup(raw)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {html.escape(str(exc))}", parse_mode="HTML")
        return
    except Exception:
        log.exception("restore download failed")
        await update.message.reply_text("❌ Could not download the file.")
        return
    admin_temp[uid] = {"restore_doc": parsed}
    d, a = parsed["data"], parsed["data"]["app"]
    await update.message.reply_text(
        "✅ <b>Backup is valid</b> (integrity verified)\n\n"
        f"Created: {html.escape(parsed['created'])} · bot v{html.escape(str(parsed['bot_version']))}\n"
        f"Users: <b>{len(d['users'])}</b> · Listings: <b>{len(a.get('posts', {}))}</b> · Sold: <b>{len(a.get('sold', []))}</b>\n"
        f"Ledger entries: <b>{len(a.get('ledger', []))}</b> · Promo codes: <b>{len(a.get('promos', {}))}</b> · Invoices: <b>{len(a.get('invoices', {}))}</b>\n\n"
        "⚠️ Confirming REPLACES the current data (a pre-restore snapshot is saved first). Pending payments in progress are kept.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[ibt("✅ Confirm Restore", "bk_rs_yes")], [ibt("❌ Cancel", "adm_bk")]]))


def apply_restore(parsed: dict):
    snap, _ = build_backup()
    _atomic_write(os.path.join(BACKUP_DIR, f"pre-restore-{datetime.now(IST):%Y%m%d-%H%M%S}.json.gz"), snap)
    d = parsed["data"]
    live_pending, live_holds = dict(pending_orders), dict(appdata.get("holds", {}))
    new_users = {int(k): v for k, v in d["users"].items()}
    new_sa = {int(k): v for k, v in d["super_admins"].items()}
    new_fj = [e for e in d["force_join"] if isinstance(e, dict) and "id" in e and "link" in e]
    app = _merge_app(json.loads(json.dumps(d["app"])))
    users.clear(); users.update(new_users)
    super_admins.clear(); super_admins.update(new_sa)
    force_join[:] = new_fj
    _install_app_state(app, live_pending=live_pending)
    appdata["holds"] = live_holds
    save_all()


async def bk_restore_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return
    parsed = (admin_temp.get(uid) or {}).get("restore_doc")
    if not parsed:
        await q.answer("Upload a backup first.", show_alert=True)
        return
    await q.answer("Restoring…")
    try:
        apply_restore(parsed)
    except Exception:
        log.exception("restore failed")
        _data_load(); app_load()
        await q.edit_message_text("❌ Restore failed — the previous data was kept (see logs).", reply_markup=InlineKeyboardMarkup(back_button("adm_bk")))
        return
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    locked = sum(1 for p in posts.values() if p.get("creds_locked"))
    nocreds = sum(1 for p in posts.values() if not p.get("creds"))
    await q.edit_message_text(
        "✅ <b>Restore complete.</b>\n\n" + (f"⚠️ {locked} listing(s) could not be decrypted with the current DATA_KEY and stay unavailable.\n" if locked else "")
        + (f"⚠️ {nocreds} listing(s) have no credentials (backup was made without encryption) — re-add them.\n" if nocreds else ""),
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_button("adm_bk")))


# ═══ VERIFIED UPDATES (owner-controlled, hash + HMAC checked, rollback) ═══════
def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


async def upd_manifest() -> dict:
    url, secret = os.getenv("UPDATE_MANIFEST_URL", ""), os.getenv("UPDATE_SECRET", "")
    if not url.startswith("https://") or not secret:
        raise ValueError("Set UPDATE_MANIFEST_URL (https) and UPDATE_SECRET to enable updates.")
    async with http().get(url) as r:
        man = json.loads(await r.text())
    ver, durl, sha, sig = str(man.get("version", "")), str(man.get("url", "")), str(man.get("sha256", "")), str(man.get("sig", ""))
    if not (ver and durl.startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", sha)):
        raise ValueError("The manifest is malformed.")
    good = hmac.new(secret.encode(), f"{ver}|{durl}|{sha}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        raise ValueError("Manifest signature is invalid — update refused.")
    return {"version": ver, "url": durl, "sha256": sha}


async def upd_apply(bot) -> str:
    man = await upd_manifest()
    if _ver(man["version"]) <= _ver(BOT_VERSION):
        return f"Already up to date (v{BOT_VERSION})."
    async with http().get(man["url"], timeout=aiohttp.ClientTimeout(total=60)) as r:
        code = await r.read()
    if len(code) > 3_000_000 or hashlib.sha256(code).hexdigest() != man["sha256"]:
        raise ValueError("Downloaded file failed the SHA-256 check — update refused.")
    src = code.decode("utf8")
    compile(src, "bot_update", "exec")                               # syntax check only — nothing is executed
    if f'BOT_VERSION = "{man["version"]}"' not in src:
        raise ValueError("The file's BOT_VERSION does not match the manifest.")
    await run_backup(bot, "pre-update") if S().get("backup_chat") else None
    save_all()
    bak = f"{SELF_PATH}.bak-{BOT_VERSION}"
    shutil.copyfile(SELF_PATH, bak)
    tmp = SELF_PATH + ".new"
    with open(tmp, "wb") as fh:
        fh.write(code)
    os.replace(tmp, SELF_PATH)
    _atomic_write(UPDATE_STATE_FILE, _dump({"pending_confirm": True, "boots": 0, "prev": bak, "to": man["version"]}))
    return f"Updated to v{man['version']}. Restarting…"


def _restart():
    os.execv(sys.executable, [sys.executable] + sys.argv)


def update_boot_check():
    """If a freshly installed version keeps crashing before it confirms itself, roll back automatically."""
    st = _read_json(UPDATE_STATE_FILE)
    if not isinstance(st, dict) or not st.get("pending_confirm"):
        return
    st["boots"] = int(st.get("boots", 0)) + 1
    if st["boots"] >= 3 and st.get("prev") and os.path.exists(st["prev"]):
        shutil.copyfile(st["prev"], SELF_PATH)
        _atomic_write(UPDATE_STATE_FILE, _dump({"pending_confirm": False, "rolled_back": True}))
        _restart()
    _atomic_write(UPDATE_STATE_FILE, _dump(st))


async def confirm_update_task():
    await asyncio.sleep(90)
    st = _read_json(UPDATE_STATE_FILE)
    if isinstance(st, dict) and st.get("pending_confirm"):
        st["pending_confirm"] = False
        _atomic_write(UPDATE_STATE_FILE, _dump(st))
        log.info("update confirmed healthy")


async def adm_upd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    text = (f"🔄 <b>Updates</b>\n\nInstalled: <b>v{BOT_VERSION}</b>\n\n"
            "Updates come from your own signed manifest (HTTPS + SHA-256 + HMAC). The old file is kept for rollback, "
            "your data and secrets are never touched. On hosts with an ephemeral disk (e.g. Railway without a volume) "
            "redeploy from your repository instead.")
    rows = [[ibt("🔍 Check for Update", "upd_check")], [ibt("⏪ Roll Back", "upd_rb_ask")], [ibt("🔙 Back", "admin_panel")]]
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def upd_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    d = q.data
    try:
        if d == "upd_check":
            man = await upd_manifest()
            newer = _ver(man["version"]) > _ver(BOT_VERSION)
            rows = ([[ibt("⬇️ Install v" + man["version"], "upd_go")]] if newer else []) + [[ibt("🔙 Back", "adm_upd")]]
            await q.edit_message_text(f"Latest: <b>v{html.escape(man['version'])}</b> — " + ("update available." if newer else "you are up to date."),
                                      parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        elif d == "upd_go":
            await q.edit_message_text("⏳ Downloading and verifying…")
            msg = await upd_apply(context.bot)
            await q.edit_message_text(html.escape(msg), parse_mode="HTML")
            if msg.startswith("Updated"):
                await asyncio.sleep(1)
                _restart()
        elif d == "upd_rb_ask":
            baks = sorted((f for f in os.listdir(os.path.dirname(SELF_PATH)) if f.startswith(os.path.basename(SELF_PATH) + ".bak-")))
            await q.edit_message_text("⏪ Roll back to the previous version?" if baks else "No previous version is stored.",
                                      reply_markup=InlineKeyboardMarkup(([[ibt("✅ Roll Back", "upd_rb")]] if baks else []) + back_button("adm_upd")))
        elif d == "upd_rb":
            folder = os.path.dirname(SELF_PATH)
            baks = sorted((os.path.join(folder, f) for f in os.listdir(folder) if f.startswith(os.path.basename(SELF_PATH) + ".bak-")),
                          key=os.path.getmtime)
            if not baks:
                await q.edit_message_text("No previous version is stored.")
                return
            save_all()
            shutil.copyfile(baks[-1], SELF_PATH)
            await q.edit_message_text("⏪ Rolled back. Restarting…")
            await asyncio.sleep(1)
            _restart()
    except Exception as exc:
        await q.edit_message_text(f"❌ {html.escape(str(exc))[:300]}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_button("adm_upd")))


# ═══ MAIN MENU CALLBACK / ROUTERS ═════════════════════════════════════════════
async def main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    user_state.pop(uid, None)
    checkout.pop(uid, None)
    await cleanup_flow(context.bot, uid, keep=(query.message.message_id,))
    await send_main_menu(update, context, edit=True)


_ROUTES = {}   # filled in main(): normalised button text -> handler


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes text: reply-keyboard buttons first, then the state machine."""
    uid   = update.effective_user.id
    state = user_state.get(uid)
    text  = (update.message.text or "").strip()

    handler = _ROUTES.get(norm_btn_text(text))
    if handler and not (state in (SELL_PASSWORD, SELL_LOGIN_ID) and len(text) > 20):
        user_state.pop(uid, None)
        sell_draft.pop(uid, None)
        checkout.pop(uid, None)
        if handler is not admin_panel:
            await cleanup_flow(context.bot, uid)
        await handler(update, context)
        return

    if state == ADMIN_ICON_WAIT and is_owner(uid):
        await icon_wait_finish(update, context)
    elif state == ADMIN_WAIT_POST_ID:
        await admin_add_price(update, context)
    elif state == ADMIN_WAIT_PRICE:
        await admin_add_photo_step(update, context)
    elif state == ADMIN_WAIT_PHOTO:
        await update.message.reply_text("🖼️ Please send a photo, or tap Skip.")
    elif state == ADMIN_WAIT_INFO:
        await admin_info_step(update, context)
    elif state == ADMIN_WAIT_CREDS:
        await admin_add_finish(update, context)
    elif state == ADMIN_EDIT_VALUE:
        await admin_edit_value(update, context)
    elif state == ADMIN_FJ_ADD:
        await fj_add_finish(update, context)
    elif state in (ADMIN_BAN_ID, ADMIN_UNBAN_ID):
        await usr_id_finish(update, context)
    elif state == ADMIN_SA_ADD:
        await sa_add_finish(update, context)
    elif state == ADMIN_BC_ADD:
        await bc_add_finish(update, context)
    elif state == ADMIN_PROMO_WIZ:
        await pm_wizard_text(update, context)
    elif state == ADMIN_PROMO_EDIT:
        await pm_edit_finish(update, context)
    elif state == ADMIN_CH_SET:
        await ch_set_finish(update, context)
    elif state == ADMIN_BK_SET:
        await bk_set_finish(update, context)
    elif state in (ADMIN_QUOTE_PRICE, ADMIN_REJECT_REASON, ADMIN_LIST_PRICE):
        await sub_admin_text(update, context)
    elif state == ADMIN_SETTING_EDIT:
        await set_edit_finish(update, context)
    elif state == BUYER_WAIT_POST_ID:
        await buyer_post_id(update, context)
    elif state == BUYER_WAIT_PROMO:
        await buyer_promo_input(update, context)
    elif state == WALLET_WAIT_AMOUNT:
        await wallet_amount_input(update, context)
    elif state in (SELL_LOGIN_ID, SELL_PASSWORD, SELL_EXTRA):
        await sell_text_step(update, context)
    elif state == SELL_PHOTOS:
        await update.message.reply_text("📸 Please send photos of your ID (or tap Cancel).")
    else:
        log_event(uid, "message", text)
        if welcome_msg.get(uid):
            await send_tracked(context.bot, uid, update.effective_chat.id, "👇 Please use the menu buttons below.")
        else:
            await send_main_menu(update, context)


async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await admin_photo_received(update, context):
        return
    await sell_photo(update, context)


async def document_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await restore_file_received(update, context)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("handler error", exc_info=context.error)


# ═══ STARTUP / RECOVERY / SHUTDOWN ════════════════════════════════════════════
class _Ctx:
    """Minimal stand-in for ContextTypes (deliver/expire/poll only need .bot)."""
    def __init__(self, bot):
        self.bot = bot


async def _reconcile_order(order_id: str, ctx: _Ctx):
    """After a restart, ask the gateway what really happened to every pending order."""
    info = pending_orders.get(order_id)
    if not info:
        return
    try:
        if info.get("method") == "wallet":                       # wallet orders were already debited; just finish delivery
            await deliver_order(order_id, {"utr": None, "sender": None}, ctx)
            return
        status, data = await fg_status(order_id)
        if status == "success":
            await deliver_order(order_id, data, ctx)
        elif status == "expired":
            await expire_order(order_id, ctx)
        else:
            start_poller(order_id, ctx)
    except Exception:
        log.exception("reconcile failed for %s", order_id)
        start_poller(order_id, ctx)


async def autosave_loop():
    while True:
        await asyncio.sleep(30)
        if _dirty:
            _data_save()
            app_save()


async def _shutdown(app: Application):
    for t in list(_timers.values()):
        t.cancel()
    for t in list(_tasks):
        t.cancel()
    try:
        save_all()
    except Exception:
        log.exception("final save failed")
    if _http is not None and not _http.closed:
        await _http.close()


async def _on_startup(app: Application):
    """Load everything, validate premium IDs, reconcile pending payments, start background jobs (once)."""
    _fj_load()
    _data_load()
    app_load()
    for tok in re.split(r"[\s,]+", os.getenv("SUPER_ADMINS", "")):
        if tok.isdigit() and int(tok) != ADMIN_ID:
            super_admins.setdefault(int(tok), tok)
    for uid in list(users):
        urec(uid)
    _EXTRA_ICON_IDS.update(v for v in S().get("btn_icons", {}).values() if v)
    try:
        vres = await _validate_premium_ids(app.bot)
    except Exception:
        log.exception("validate failed")
        vres = None
    for tok in re.split(r"[\s,]+", os.getenv("FORCE_JOIN", "")):
        if not tok:
            continue
        try:
            e = await _fj_resolve(app.bot, tok)
            if not any(x["id"] == e["id"] for x in force_join):
                force_join.append(e)
        except Exception as exc:
            log.warning("FORCE_JOIN env %s skipped: %s", tok, exc)
    # a broadcast send that was in flight when the process died may or may not have been delivered: never resend it
    interrupted = 0
    for bc in appdata.get("broadcasts", {}).values():
        for r in bc.get("results", {}).values():
            if r.get("status") == "sending":
                r["status"], r["detail"] = "uncertain", "interrupted by a restart (not resent)"
                interrupted += 1
    for k in list(appdata.get("broadcasts", {}))[:-30]:          # keep the registry of old broadcasts bounded
        appdata["broadcasts"].pop(k, None)
    save_all()

    ctx = _Ctx(app.bot)
    for oid in list(pending_orders):
        _spawn(_reconcile_order(oid, ctx))
    if not app.bot_data.get("bg_started"):                        # exactly one set of background jobs
        app.bot_data["bg_started"] = True
        _spawn(autosave_loop())
        _spawn(promo_loop(app))
        _spawn(backup_loop(app))
        _spawn(confirm_update_task())

    key_lines = []
    for k in ("delete_post", "pending_orders", "force_join", "manage_users", "broadcast"):
        if k in _FORCE_REPORT:
            cid, g, anim, src = _FORCE_REPORT[k]
            key_lines.append(f"• {k.replace('_', ' ')}: {g} {'animated' if anim else 'static'} ({src})")
        else:
            key_lines.append(f"• {k.replace('_', ' ')}: no valid icon found — use /seticon {k}")
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🚀 <b>FF Store Bot v{BOT_VERSION} started!</b>\n\n"
                f"🟢 Listings: <code>{len(available_ids())}</code> available · sold <code>{len(sold)}</code> · users <code>{len(users)}</code>\n"
                f"⏳ Pending orders reconciled: <code>{len(pending_orders)}</code>\n"
                f"📢 Force join: <code>{len(force_join)}</code>\n"
                f"🧩 PTB <code>{_ptb_ver()}</code> — buttons: <code>{_btn_mode()}</code>\n"
                + (f"🎯 Premium IDs valid: <code>{vres[0]}/{vres[1]}</code> | emoji matched: <code>{vres[2]}</code>\n" if vres else "⚠️ Premium ID check failed\n")
                + "🎨 Button icons:\n" + "\n".join(key_lines) + "\n"
                + f"🔐 Credential encryption: <b>{'on' if _vault() else 'OFF (install cryptography)'}</b>\n"
                + (f"⚠️ {interrupted} interrupted broadcast send(s) marked uncertain.\n" if interrupted else "")
                + f"💾 Backups: <b>{'on' if S().get('backup_chat') and S().get('backup_enabled') else 'not configured'}</b> · "
                  f"📣 Auto promo: <b>{'on' if S()['promo_enabled'] and not S()['promo_paused'] else 'off'}</b>\n"
                "💎 Auto-payment polling every 2 seconds"
            ),
            parse_mode="HTML",
        )
    except Exception:
        log.warning("startup notify failed")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    update_boot_check()
    _install_premium_patch()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_on_startup)
        .post_shutdown(_shutdown)
        .build()
    )

    _ROUTES.update({
        "buy ff id": buy_start, "sell your id": sell_start, "my profile": show_profile,
        "wallet": show_wallet, "my orders": show_my_orders, "admin panel": admin_panel,
    })
    private = filters.ChatType.PRIVATE
    CB = CallbackQueryHandler

    # Every private update: track the user + ban check (first)
    app.add_handler(MessageHandler(private & filters.ALL, track_user), group=-2)
    app.add_handler(CB(track_user), group=-2)

    # Commands
    app.add_handler(CommandHandler("start",        start,           filters=private))
    app.add_handler(CommandHandler("start",        group_start,     filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("checkorder",   cmd_checkorder,  filters=private))
    app.add_handler(CommandHandler("orders",       cmd_orders,      filters=private))
    app.add_handler(CommandHandler("addbal",       cmd_addbal,      filters=private))
    app.add_handler(CommandHandler("subbal",       cmd_subbal,      filters=private))
    app.add_handler(CommandHandler("seticon",      cmd_seticon,     filters=private))

    # Group / channel tracking
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, channel_post_handler))

    # Inline callbacks — admin
    for pat, fn in [
        (r"^main_menu$", main_menu_cb), (r"^admin_panel$", admin_panel), (r"^admin_orders$", admin_orders_cb),
        (r"^admin_add$", admin_add_start), (r"^admin_autoid$", admin_autoid_cb), (r"^admin_skip_photo$", admin_skip_photo_cb),
        (r"^admin_skip_info$", admin_skip_info_cb), (r"^admin_view$", admin_view),
        (r"^admin_delete_list$", admin_delete_list), (r"^del_", admin_delete_confirm),
        (r"^admin_edit_list$", admin_edit_list), (r"^edit_", admin_edit_select), (r"^editfield_", admin_edit_field),
        (r"^(admin_users|usr_p_\d+)$", admin_users), (r"^usr_t_\d+_\d+$", usr_toggle), (r"^usr_(ban|unban)id$", usr_id_start),
        (r"^sa_menu$", sa_menu), (r"^sa_add$", sa_add_start), (r"^sa_del_\d+$", sa_del_cb),
        (r"^admin_fj$", admin_fj), (r"^fj_add$", fj_add_start), (r"^fjdel_", fj_del_cb), (r"^fj_check$", fj_check_cb),
        # broadcast
        (r"^adm_bc$", adm_bc), (r"^bc_list$", bc_list), (r"^bc_a_", bc_toggle_auto), (r"^bc_r_", bc_remove),
        (r"^bc_add$", bc_add_start), (r"^bc_refresh$", bc_refresh), (r"^bc_preview$", bc_preview),
        (r"^(bc_send|bc_sel_all|bc_sel_none|bc_t_-?\d+)$", bc_send), (r"^bc_go$", bc_go), (r"^bc_confirm$", bc_confirm),
        (r"^bc_auto", bc_auto), (r"^bc_int_\d+$", bc_auto), (r"^bc_tpl$", bc_tpl),
        (r"^adm_set$", adm_set), (r"^set_e_", set_edit_start),
        # promo codes
        (r"^adm_promo$", adm_promo), (r"^pm_new_[ma]$", pm_new), (r"^pm_(type_|stack_|skip)", pm_wizard_cb),
        (r"^pm_v\|", pm_view), (r"^pm_t\|", pm_toggle), (r"^pm_e\|", pm_edit_start),
        # channel / backup / updates / sellers
        (r"^adm_ch$", adm_ch), (r"^ch_rm$", adm_ch), (r"^ch_(set|test|pub)$", ch_actions), (r"^drf_", drf_cb),
        (r"^(adm_bk|bk_tog|bk_now|bk_int_\d+)$", adm_bk), (r"^bk_set$", bk_set_start), (r"^bk_restore$", bk_restore_start),
        (r"^bk_rs_yes$", bk_restore_confirm), (r"^adm_upd$", adm_upd), (r"^upd_", upd_actions),
        (r"^adm_subs$", adm_subs), (r"^sv_", sub_view_cb), (r"^s[qrl]_", sub_admin_action),
    ]:
        app.add_handler(CB(fn, pattern=pat))

    # Inline callbacks — customer
    for pat, fn in [
        (r"^buy_start$", buy_start), (r"^cat_(prev|next)_", cat_nav_cb), (r"^cat_buy_", cat_buy_cb), (r"^cat_go_", cat_go_cb),
        (r"^cat_enter$", cat_enter_cb), (r"^cat_cancel_enter$", cat_cancel_enter_cb), (r"^cat_close$", cat_close_cb),
        (r"^co_promo_cancel$", co_promo_cancel_cb), (r"^co_promo_", co_promo_cb), (r"^co_rmpromo_", co_rmpromo_cb),
        (r"^co_upi_", co_upi_cb), (r"^co_bal_", co_bal_cb), (r"^topup_", topup_cb), (r"^cancel_", cancel_order),
        (r"^close_msg$", close_msg_cb), (r"^inv_", inv_cb),
        (r"^sell_done$", sell_done_cb), (r"^sell_pf_", sell_platform_cb), (r"^sell_skip$", sell_skip_cb),
        (r"^sell_cancel$", sell_cancel_cb), (r"^sell_submit$", sell_submit_cb), (r"^so_[ad]_", sell_offer_cb),
    ]:
        app.add_handler(CB(fn, pattern=pat))

    # Messages
    app.add_handler(MessageHandler(filters.PHOTO & private, photo_router))
    app.add_handler(MessageHandler(filters.Document.ALL & private, document_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, message_router))

    app.add_error_handler(on_error)

    log.info("🚀 FF Store Bot v%s starting", BOT_VERSION)
    app.run_polling(drop_pending_updates=True,
                    allowed_updates=["message", "edited_message", "callback_query", "my_chat_member", "channel_post", "edited_channel_post"])


if __name__ == "__main__":
    main()
