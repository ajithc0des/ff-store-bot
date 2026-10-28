"""FF ID Store Bot — Telegram + FamGateway UPI auto-payment.

v6 adds on top of the previous premium bot (all existing features preserved):
- Persistent store (store.json): listings, orders, wallet ledger, promo codes, invoices, sell requests
- Catalogue with photos, Buy ID, one-ID-at-a-time navigation, tracked message cleanup
- Checkout with promo codes, UPI or wallet payment, one-minute progress countdown, invoices
- Wallet top-up (verified payments only) with an idempotent transaction ledger
- "Sell Your ID" flow: 5-20 photos + login details -> admin offer -> user accepts -> admin lists
- Admin: promo codes, wallet adjust, sell requests, inventory channel, broadcast, auto promotion
- Private backup group: scheduled backups + activity reports every 2 hours, validated restore
- Startup recovery of pending orders, graceful shutdown, owner-controlled verified updater
- Every user-facing text is English
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
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
from telegram import error as tge

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
APP_VERSION = "6.0.0"
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ["ADMIN_ID"])
FG_API_KEY  = os.environ["FG_API_KEY"]
FG_BASE_URL = "https://famgateway.in/api"

POLL_INTERVAL    = 2         # seconds between each status check
POLL_MAX_SECONDS = 360       # gateway order lives 300s; small grace on top
COUNTDOWN_SECONDS = 60       # progress countdown shown on the invoice (display only)

LOGO_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
LOGO_RATIO = 0.22
SCRIPT_PATH = os.path.abspath(__file__)

# Persistent files. On Railway mount a Volume and set DATA_DIR to it, otherwise data is lost on redeploy.
_DATA_DIR  = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
FJ_FILE    = os.path.join(_DATA_DIR, "forcejoin.json")
DATA_FILE  = os.path.join(_DATA_DIR, "botdata.json")   # users + ban + super admins + icons + broadcast chats
DB_FILE    = os.path.join(_DATA_DIR, "store.json")     # listings, orders, wallet, promos, sell requests, settings
BACKUP_DIR = os.path.join(_DATA_DIR, "backups")
UPDATE_STATE_FILE = os.path.join(_DATA_DIR, "update_state.json")
BOT_NAME   = "𝘾𝘼𝙋𝙏𝘼𝙄𝙉 𝙓 𝙎𝙏𝙊𝙍𝙀"

# Business settings (defaults — most of them can be edited by the owner inside the bot)
ORDERS_COMPLETED_DEFAULT = os.getenv("ORDERS_COMPLETED", "300+")   # only show a number that is true
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "")                       # e.g. https://t.me/yourusername
TOPUP_MIN    = float(os.getenv("TOPUP_MIN", "5"))
TOPUP_MAX    = float(os.getenv("TOPUP_MAX", "5000"))
TOPUP_PRESETS = [float(x) for x in re.split(r"[,\s]+", os.getenv("TOPUP_PRESETS", "5,50,100,500")) if x]
MIN_FINAL_PRICE = 1.0        # a discounted order can never go below this
BC_DELAY     = 0.6           # seconds between two broadcast sends (rate-limit safe)

SELL_MIN_IMAGES = 5
SELL_MAX_IMAGES = 20
SELL_ACTIVE_LIMIT = 3        # max open sell requests per user
ADMIN_MAX_IMAGES = 20

BACKUP_KEY   = os.getenv("BACKUP_KEY", "")                  # encrypts credentials inside backups
UPDATE_MANIFEST_URL = os.getenv("UPDATE_MANIFEST_URL", "")  # https URL of a JSON manifest (optional)
UPDATE_PUBKEY = os.getenv("UPDATE_PUBKEY", "")              # base64 Ed25519 public key (optional)

IST = timezone(timedelta(hours=5, minutes=30))

# ─── STATES ────────────────────────────────────────────────────────────────────
ADMIN_WAIT_POST_ID = "ADMIN_WAIT_POST_ID"
ADMIN_WAIT_PRICE   = "ADMIN_WAIT_PRICE"
ADMIN_WAIT_CREDS   = "ADMIN_WAIT_CREDS"
ADMIN_WAIT_PHOTOS  = "ADMIN_WAIT_PHOTOS"
ADMIN_WAIT_INFO    = "ADMIN_WAIT_INFO"
ADMIN_EDIT_VALUE   = "ADMIN_EDIT_VALUE"
ADMIN_EDIT_PHOTOS  = "ADMIN_EDIT_PHOTOS"
BUYER_WAIT_POST_ID = "BUYER_WAIT_POST_ID"
BUYER_WAIT_PROMO   = "BUYER_WAIT_PROMO"
BUYER_WAIT_TOPUP   = "BUYER_WAIT_TOPUP"
ADMIN_FJ_ADD       = "ADMIN_FJ_ADD"
ADMIN_BAN_ID       = "ADMIN_BAN_ID"
ADMIN_UNBAN_ID     = "ADMIN_UNBAN_ID"
ADMIN_SA_ADD       = "ADMIN_SA_ADD"
ADMIN_BC_ADD       = "ADMIN_BC_ADD"
ADMIN_BC_EDIT      = "ADMIN_BC_EDIT"
ADMIN_SET_ICON     = "ADMIN_SET_ICON"
ADMIN_PROMO_WIZ    = "ADMIN_PROMO_WIZ"
ADMIN_PROMO_EDIT   = "ADMIN_PROMO_EDIT"
ADMIN_BAL_ADJUST   = "ADMIN_BAL_ADJUST"
ADMIN_SELL_OFFER   = "ADMIN_SELL_OFFER"
ADMIN_SELL_LIST    = "ADMIN_SELL_LIST"
ADMIN_CHANNEL_SET  = "ADMIN_CHANNEL_SET"
ADMIN_BACKUP_CHAT  = "ADMIN_BACKUP_CHAT"
ADMIN_RESTORE_FILE = "ADMIN_RESTORE_FILE"
ADMIN_AP_INTERVAL  = "ADMIN_AP_INTERVAL"
ADMIN_BK_INTERVAL  = "ADMIN_BK_INTERVAL"
SELL_PHOTOS        = "SELL_PHOTOS"
SELL_PLATFORM      = "SELL_PLATFORM"
SELL_LOGIN         = "SELL_LOGIN"
SELL_PASS          = "SELL_PASS"
SELL_CONFIRM       = "SELL_CONFIRM"

# ─── IN-MEMORY STORE (persisted to store.json / botdata.json) ─────────────────
posts: dict          = {}   # {post_id: {"price","creds","images","info","created","source","channel_msg",...}}
sold: set            = set()
sold_meta: dict      = {}   # {post_id: {"order_id","uid","ts","price","channel_msg","channel_kind"}}
reserved: dict       = {}   # {post_id: order_id | "creating"}
pending_orders: dict = {}   # {order_id: {"user_id","post_id","chat_id","msg_id","kind","amount",...}}
order_log: dict      = {}   # finalized orders: {order_id: {...}}
invoices: dict       = {}   # {order_id: invoice record}
balances: dict       = {}   # {uid: float}
ledger: list         = []   # wallet transaction ledger (append-only)
ledger_keys: set     = set()
promos: dict         = {}   # {CODE: promo record}
sells: dict          = {}   # {sell_id: sell request}
counters: dict       = {"post": 0, "invoice": 0, "sell": 0, "ledger": 0, "adj": 0}
settings: dict       = {}   # filled by _default_settings()/_db_load()
activity: list       = []   # user activity events (capped)
progress: dict       = {}   # {uid: {"a": last action, "ts": ...}}

user_state: dict  = {}
admin_temp: dict  = {}
users: dict       = {}      # {uid: {"name","username","banned","first_seen","last_seen"}}
super_admins: dict = {}     # {uid: name}
force_join: list  = []      # [{"id": int, "title": str, "link": str}]
bc_chats: dict    = {}      # {chat_id: {"title","type","link","promo": bool,"last_promo": ts}}
_last_seen: dict  = {}      # {order_id: (pub_json, auth_json)} — log de-dup
_tasks: set       = set()
_http = None
_bc_busy: bool    = False   # only one broadcast at a time
_bc_last: dict    = {}      # last broadcast summary (memory only)

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
    """Emoji na ho to label ke role se premium icon chunta hai."""
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


def _split_btn(text: str):
    """Leading emoji -> uska premium ID (label se hata deta hai). Emoji na ho to role-icon."""
    m = _PREMIUM_RE.match(text)
    if m:
        rest = text[m.end():].strip()
        eid = _PREMIUM_MAP[m.group(0).replace("\ufe0f", "")]
        return (rest or text), (None if eid in _INVALID_IDS else eid)
    return text, _btn_icon(text)


def _btn_style(label: str) -> str:
    """Button ka color: danger=red, success=green, primary=blue."""
    l = label.lower()
    if any(w in l for w in ("back", "cancel", "delete", "remove", "reject", "decline", "stop")):
        return "danger"
    if any(w in l for w in ("buy", "add", "confirm", "approve", "paid", "pay", "done",
                            "again", "open", "yes", "available", "sold")):
        return "success"
    return "primary"


def _mk_btn(cls, base: dict, icon, style):
    """Button banata hai. Nayi PTB: native kwargs. Purani PTB: api_kwargs se same cheez
    (Telegram server tak icon_custom_emoji_id + style hamesha pahunchta hai)."""
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


def _inline_btn(text: str, _sc: bool = True, _style: str | None = None,
                _icon: str | None = None, **extra):
    """Inline button + premium icon + color.
    _icon = explicit custom-emoji ID (e.g. animated pick from _adm_icon); default icon override karta hai."""
    fx = stylish if _sc else (lambda t: t)
    label, eid = _split_btn(text)
    if _icon and _icon not in _INVALID_IDS:
        eid = _icon
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
_STICKER_META: dict = {}    # {custom_emoji_id: (glyph, is_animated)} — startup validation se bharta hai

# ── Admin-panel button icons (animated pick) ───────────────────────────────────
# Har key ke liye glyph-preference list. Startup pe Telegram ke sticker metadata se pehla
# *animated* (TGS/WEBM) custom emoji chuna jata hai. Override: env EMOJI_<KEY> ya owner /seticon.
ADMIN_ICON_SPECS = {
    "delete":    {"glyphs": ("🗑", "🚮", "❌"),            "env": "EMOJI_DELETE"},
    "orders":    {"glyphs": ("📦", "🧾", "📋", "⏳"),      "env": "EMOJI_ORDERS"},
    "forcejoin": {"glyphs": ("📢", "📣", "🔔", "📡"),      "env": "EMOJI_FORCEJOIN"},
    "users":     {"glyphs": ("👥", "👤", "🫂", "👨‍👩‍👧"),   "env": "EMOJI_USERS"},
    "broadcast": {"glyphs": ("📣", "📢", "📡", "🚀"),      "env": "EMOJI_BROADCAST"},
}
ICON_KEYS = tuple(ADMIN_ICON_SPECS)
_ADMIN_ICONS: dict = {}      # {key: custom_emoji_id} (resolved)
_ICON_OVERRIDES: dict = {}   # {key: custom_emoji_id} — /seticon se, botdata.json me persist


def _resolve_admin_icons():
    """Har admin button ke liye icon chunta hai: override -> animated glyph-match -> (kuch nahi = default)."""
    _ADMIN_ICONS.clear()
    used: set = set()
    for key, spec in ADMIN_ICON_SPECS.items():
        cand = (_ICON_OVERRIDES.get(key) or os.getenv(spec["env"], "")).strip()
        if cand and cand not in _INVALID_IDS:
            _ADMIN_ICONS[key] = cand
            used.add(cand)
            continue
        pick = None
        for g in spec["glyphs"]:
            g = g.replace("️", "")
            ids = [i for i, (gl, anim) in _STICKER_META.items() if gl == g and anim and i not in used]
            if ids:
                pick = ids[0]
                break
        if pick:
            _ADMIN_ICONS[key] = pick
            used.add(pick)


def _adm_icon(key: str):
    return _ADMIN_ICONS.get(key)


def _icon_is_animated(eid) -> bool:
    return bool(eid) and bool(_STICKER_META.get(eid, ("", False))[1])


def _icon_report() -> str:
    anim = [k for k in ICON_KEYS if _icon_is_animated(_ADMIN_ICONS.get(k))]
    rest = [k for k in ICON_KEYS if k not in anim]
    s = f"Admin icons animated: <code>{len(anim)}/{len(ICON_KEYS)}</code>"
    if rest:
        s += f" (default/static: {', '.join(rest)} — set them with /seticon)"
    return s

def _te(eid, fb="⭐"):
    if eid in _INVALID_IDS:          # galat ID poore message ko reject karwa deta hai
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

# Standard emoji -> premium id (sirf wahi jinka ID app.py me same emoji ke saath use hua tha)
_PREMIUM_MAP_RAW = {
    # ── IDs jo app.py me isi emoji ke saath use hue the ──
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
    # ── baaki sab emojis (app.py ke premium pool se) ──
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
    "➿": _LOOP_ID,
}
_PREMIUM_MAP = {k.replace("\ufe0f", ""): v for k, v in _PREMIUM_MAP_RAW.items()}
_PREMIUM_RE = _re.compile("|".join(_re.escape(k) + "\ufe0f?" for k in sorted(_PREMIUM_MAP, key=len, reverse=True)))


def _all_known_ids() -> set:
    ids = set(_PREMIUM_MAP_RAW.values()) | set(_BTN_EM.values()) | set(STICKER_POOL)
    ids |= {_LOOP_ID, _BLUE_RING, _BLUE_SHK, _BLUE_GLOW, _BLUE_STAR, _BLUE_DIAM, "6255767342916045646",
            _EM_WELCOME, _EM_HELLO, _EM_USER2, _EM_CMD, _EM_RULES, _EM_NOSPAM, _EM_NOABUSE,
            _EM_RESPECT, _EM_DEV}
    ids |= {e for _, e, _ in _ICON_RULES} | {e for e, _ in _ROTATE}
    return ids


async def _validate_premium_ids(bot):
    """Telegram se pucha jata hai kaunsi IDs sach me custom emoji hain, aur unka asli glyph kya hai.
    Galat IDs hata di jati hain; baaki emojis ko unke asli glyph wali premium ID se match kiya jata hai.
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
    _INVALID_IDS.clear(); _INVALID_IDS.update(set(ids) - set(valid))
    _STICKER_META.clear()

    glyph_ids: dict = {}
    def add(g, cid, st=None):
        g = (g or "").replace("\ufe0f", "")
        if g and cid not in glyph_ids.setdefault(g, []):
            glyph_ids[g].append(cid)
        if st is not None:
            # animated = TGS (is_animated) ya WEBM (is_video); static = WEBP
            _STICKER_META[cid] = (g, bool(getattr(st, "is_animated", False) or getattr(st, "is_video", False)))
    for st in valid.values():
        add(st.emoji, st.custom_emoji_id, st)
    for name in {st.set_name for st in valid.values() if st.set_name}:
        try:
            for st in (await bot.get_sticker_set(name)).stickers:
                if st.custom_emoji_id:
                    add(st.emoji, st.custom_emoji_id, st)
        except Exception:
            log.warning("sticker set %s read failed", name)

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
            unmatched.append(k)          # valid ID nahi -> normal emoji (message reject na ho)
    _PREMIUM_MAP.clear(); _PREMIUM_MAP.update(new_map)
    _PREMIUM_RE = _re.compile("|".join(_re.escape(k) + "\ufe0f?" for k in sorted(_PREMIUM_MAP, key=len, reverse=True)))
    return len(valid), len(ids), matched, unmatched


def _swap_emojis(text: str) -> str:
    """Plain (non-HTML-tag) text me standard emoji ko premium tg-emoji se badalta hai."""
    def rep(m):
        base = m.group(0).replace("\ufe0f", "")
        return _te(_PREMIUM_MAP[base], m.group(0))
    return _PREMIUM_RE.sub(rep, text)

def _swap_in_html(htm: str) -> str:
    """HTML ke tags / <code> / <pre> / <tg-emoji> ko chhode bina emoji swap."""
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
    """Pehli (title) line ko premium ➿ lines + ╭──💠✨ title 💫──╮ frame me daalta hai."""
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
    """Bot ke send/edit methods wrap karta hai.
    Stage 1: premium + frame  ->  Stage 2: premium bina frame  ->  Stage 3: original (plain).
    Premium fail ho to admin ko exact error bheja jata hai (ek error ek baar)."""
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
                                    text=f"⚠️ Premium emoji send failed ({name}):\n{err}\n\nThe user received a normal message.")
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



# ═══════════════════════════════════════════════════════════════════════════════
# 🗄️ PERSISTENT STORE — listings, orders, wallet, promo codes, invoices, sell requests
# ═══════════════════════════════════════════════════════════════════════════════
ACTIVITY_CAP = 4000
_db_dirty = False


def now() -> float:
    return time.time()


def _r2(x) -> float:
    return round(float(x) + 1e-9, 2)


def rupees(x) -> str:
    x = _r2(x)
    return f"₹{int(x):,}" if x == int(x) else f"₹{x:,.2f}"


def ist_str(ts, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    try:
        return datetime.fromtimestamp(float(ts), IST).strftime(fmt) + " IST"
    except Exception:
        return "N/A"


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=False)


def _default_settings() -> dict:
    return {
        "brand": BOT_NAME,
        "orders_completed": ORDERS_COMPLETED_DEFAULT,
        "support": SUPPORT_LINK,
        "channel": None,                      # {"id","title","link"} inventory channel
        "backup": {"chat": None, "enabled": True, "interval_h": 2, "last_ts": 0.0, "keep": 12,
                   "msg_ids": [], "last_ok": None, "last_err": None, "last_size": 0},
        "auto_promo": {"enabled": False, "interval_h": 3, "last_ts": 0.0},
        "report_ts": 0.0,
    }


def _merge_settings(loaded: dict) -> dict:
    base = _default_settings()
    for k, v in (loaded or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


settings.update(_default_settings())


def db_mark():
    global _db_dirty
    _db_dirty = True


def _db_snapshot(copy: bool = True, include_activity: bool = True) -> dict:
    d = {
        "posts": posts, "sold": sorted(sold), "sold_meta": sold_meta,
        "pending_orders": pending_orders, "order_log": order_log, "invoices": invoices,
        "balances": balances, "ledger": ledger, "promos": promos, "sells": sells,
        "counters": counters, "settings": settings,
    }
    if include_activity:
        d["activity"] = activity
        d["progress"] = progress
    return json.loads(json.dumps(d, ensure_ascii=False)) if copy else d


def _db_apply(d: dict):
    """Replace the in-memory store with a (validated) snapshot dict."""
    posts.clear();          posts.update(d.get("posts", {}))
    sold.clear();           sold.update(d.get("sold", []))
    sold_meta.clear();      sold_meta.update(d.get("sold_meta", {}))
    pending_orders.clear(); pending_orders.update(d.get("pending_orders", {}))
    order_log.clear();      order_log.update(d.get("order_log", {}))
    invoices.clear();       invoices.update(d.get("invoices", {}))
    balances.clear();       balances.update({int(k): float(v) for k, v in d.get("balances", {}).items()})
    ledger[:] = d.get("ledger", [])
    ledger_keys.clear()
    ledger_keys.update(f"{e['type']}:{e['ref']}" for e in ledger if e.get("ref"))
    promos.clear();         promos.update(d.get("promos", {}))
    sells.clear();          sells.update(d.get("sells", {}))
    counters.update({k: int(v) for k, v in d.get("counters", {}).items()})
    new_settings = _merge_settings(d.get("settings", {}))
    settings.clear();       settings.update(new_settings)
    if "activity" in d:
        activity[:] = d.get("activity", [])
        progress.clear()
        progress.update({int(k): v for k, v in d.get("progress", {}).items()})
    # reservations are always derived from pending purchase orders
    reserved.clear()
    for oid, info in pending_orders.items():
        if info.get("kind", "purchase") == "purchase" and info.get("post_id"):
            reserved[info["post_id"]] = oid


def _db_flush(force: bool = False):
    global _db_dirty
    if not (_db_dirty or force):
        return
    try:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf8") as fh:
            json.dump(_db_snapshot(copy=False), fh, ensure_ascii=False)
        os.replace(tmp, DB_FILE)            # atomic: a crash mid-write never corrupts the old file
        _db_dirty = False
    except Exception:
        log.exception("store save failed")


def _db_save():
    """Critical changes (orders, wallet, inventory) are written immediately."""
    db_mark()
    _db_flush()


def _db_load():
    try:
        with open(DB_FILE, encoding="utf8") as fh:
            d = json.load(fh)
        _db_apply(d)
        log.info("store loaded: %d posts, %d pending orders, %d wallets", len(posts), len(pending_orders), len(balances))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("store load failed — starting with an empty store (old file kept as store.json.bad)")
        try:
            shutil.copy(DB_FILE, DB_FILE + ".bad")
        except Exception:
            pass


def promo_sweep() -> int:
    """Deletes expired promo codes (a code held by a still-pending order is removed once that order ends)."""
    t = now()
    gone = [c for c, r in promos.items() if r.get("expires") and t > r["expires"] and promo_holds(c) == 0]
    for c in gone:
        promos.pop(c, None)
    if gone:
        log.info("expired promo codes deleted: %s", ", ".join(gone))
        _db_save()
    return len(gone)


async def _autosave_loop():
    n = 0
    while True:
        await asyncio.sleep(5)
        n += 1
        if n % 6 == 0:                       # every ~30 s
            try:
                promo_sweep()
            except Exception:
                log.exception("promo sweep failed")
        _db_flush()


# ─── Activity log (used by the 2-hour report) ────────────────────────────────
def log_event(uid: int, action: str, detail: str = ""):
    """Record what a user did. NEVER pass passwords or login credentials in `detail`."""
    t = now()
    activity.append({"ts": t, "uid": uid, "a": action, "d": str(detail)[:80]})
    if len(activity) > ACTIVITY_CAP:
        del activity[: len(activity) - ACTIVITY_CAP]
    progress[uid] = {"a": action, "ts": t}
    db_mark()


# ─── Wallet ledger ────────────────────────────────────────────────────────────
def ledger_add(uid: int, typ: str, amount: float, ref: str = "", note: str = ""):
    """Append a wallet transaction. Idempotent per (type, ref): returns None for a duplicate.
    Raises ValueError if the balance would go negative. No awaits -> atomic in asyncio."""
    key = f"{typ}:{ref}"
    if ref and key in ledger_keys:
        return None
    new_bal = _r2(balances.get(uid, 0.0) + amount)
    if new_bal < 0:
        raise ValueError("insufficient balance")
    balances[uid] = new_bal
    counters["ledger"] += 1
    entry = {"id": f"L{counters['ledger']:06d}", "ts": now(), "uid": uid, "type": typ,
             "amount": _r2(amount), "balance": new_bal, "ref": ref, "note": note[:80]}
    ledger.append(entry)
    if ref:
        ledger_keys.add(key)
    _db_save()
    return entry


def user_balance(uid: int) -> float:
    return _r2(balances.get(uid, 0.0))


# ─── IDs ─────────────────────────────────────────────────────────────────────
POST_ID_RE = re.compile(r"^[A-Za-z0-9-]{2,24}$")


def _pid_in_use(pid: str) -> bool:
    return (pid in posts or pid in sold or pid in sold_meta
            or any(i.get("post_id") == pid for i in pending_orders.values())
            or any(o.get("post_id") == pid for o in order_log.values()))


def new_post_id() -> str:
    """Bot-generated listing ID that never collides with listings, sold IDs, pending or past orders."""
    while True:
        counters["post"] += 1
        pid = f"FF{counters['post']:04d}"
        if not _pid_in_use(pid):
            db_mark()
            return pid


def avail_ids() -> list:
    """IDs a customer may buy right now, in creation order."""
    return [pid for pid, p in posts.items()
            if pid not in reserved and pid not in sold and not p.get("missing_creds")]


# ─── Promo codes & pricing (all validation happens on the server) ────────────
PROMO_CODE_RE = re.compile(r"^[A-Z0-9]{3,20}$")


def promo_norm(code) -> str:
    return re.sub(r"\s+", "", str(code or "")).upper()


def promo_holds(code: str, uid=None, ignore_order=None) -> int:
    """Pending (unpaid) orders that currently hold a redemption slot of this code."""
    n = 0
    for oid, info in pending_orders.items():
        if oid == ignore_order or info.get("promo") != code:
            continue
        if uid is None or info.get("user_id") == uid:
            n += 1
    return n


def promo_check(code, uid: int, price: float, ignore_order=None):
    """Returns (ok, reason, record). Does not modify anything."""
    code = promo_norm(code)
    promo_sweep()
    rec = promos.get(code)
    if not rec:
        return False, "This promo code does not exist.", None
    if not rec.get("enabled", True):
        return False, "This promo code is currently disabled.", rec
    t = now()
    if rec.get("starts") and t < rec["starts"]:
        return False, f"This promo code is not active yet (starts {ist_str(rec['starts'])}).", rec
    if rec.get("expires") and t > rec["expires"]:
        return False, "This promo code has expired.", rec
    used = len(rec.get("redemptions", []))
    if rec.get("max_total") is not None and used + promo_holds(code, None, ignore_order) >= rec["max_total"]:
        return False, "This promo code has reached its usage limit.", rec
    if rec.get("per_user") is not None:
        mine = sum(1 for r in rec.get("redemptions", []) if r.get("uid") == uid) + promo_holds(code, uid, ignore_order)
        if mine >= rec["per_user"]:
            return False, "You have already used this promo code the maximum number of times.", rec
    lo = max(rec.get("min_price") or 0, rec.get("min_order") or 0)
    hi = rec.get("max_price")
    if price + 1e-9 < lo or (hi is not None and price - 1e-9 > hi):
        if lo and hi is not None:
            why = f"This code is valid only for IDs priced between {rupees(lo)} and {rupees(hi)}."
        elif hi is not None:
            why = f"This code is valid only for IDs priced up to {rupees(hi)}."
        else:
            why = f"This code is valid only for IDs priced {rupees(lo)} or more."
        return False, why, rec
    if promo_discount(rec, price) <= 0:
        return False, "This code gives no discount on this ID.", rec
    return True, "", rec


def promo_discount(rec: dict, price: float) -> float:
    if rec.get("type") == "percent":
        d = price * float(rec.get("value", 0)) / 100.0
    else:
        d = float(rec.get("value", 0))
    d = min(d, price - MIN_FINAL_PRICE)           # final price can never drop below MIN_FINAL_PRICE
    return max(0.0, _r2(d))


def price_quote(pid: str, uid: int, code=None, ignore_order=None) -> dict:
    """Server-side price for a listing. The client never supplies prices or discounts."""
    p = posts.get(pid)
    if not p:
        return {"ok": False, "msg": "This ID is no longer available."}
    price = _r2(p["price"])
    q = {"ok": True, "pid": pid, "price": price, "discount": 0.0, "final": price,
         "code": None, "promo_error": "", "msg": ""}
    if code:
        ok, why, rec = promo_check(code, uid, price, ignore_order)
        if ok:
            d = promo_discount(rec, price)
            q.update(discount=d, final=_r2(price - d), code=rec["code"])
        else:
            q["promo_error"] = why
    return q


def promo_redeem(code, uid: int, order_id: str, discount: float):
    """Called ONLY after the purchase is completed. Idempotent per order."""
    rec = promos.get(promo_norm(code))
    if not rec:
        return
    if any(r.get("order_id") == order_id for r in rec.setdefault("redemptions", [])):
        return
    rec["redemptions"].append({"uid": uid, "order_id": order_id, "ts": now(), "discount": _r2(discount)})
    _db_save()


def promo_summary(rec: dict) -> str:
    val = f"{rec['value']:g}%" if rec.get("type") == "percent" else rupees(rec["value"])
    lines = [
        f"🎟 <b>{esc(rec['code'])}</b> — {val} off" + ("" if rec.get("enabled", True) else " <i>(disabled)</i>"),
        f"💰 Eligible price: {rupees(rec.get('min_price') or 0)} – "
        + (rupees(rec["max_price"]) if rec.get("max_price") is not None else "no maximum"),
        f"📦 Min order value: {rupees(rec.get('min_order') or 0)}",
        f"👥 Total uses: {len(rec.get('redemptions', []))}/" + (str(rec["max_total"]) if rec.get("max_total") is not None else "∞"),
        f"👤 Per user: " + (str(rec["per_user"]) if rec.get("per_user") is not None else "unlimited"),
        f"⏰ Starts: {ist_str(rec['starts']) if rec.get('starts') else 'immediately'}",
        f"⏳ Expires: {ist_str(rec['expires']) if rec.get('expires') else 'never'}",
    ]
    return "\n".join(lines)


def promo_generate_code(length: int = 8) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no confusing 0/O/1/I
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        if code not in promos:
            return code


# ─── Orders / invoices ───────────────────────────────────────────────────────
def order_log_add(order_id: str, kind: str, uid: int, post_id, price, discount, paid, method: str, utr, status="paid"):
    rec = {"order_id": order_id, "kind": kind, "uid": uid, "post_id": post_id, "price": _r2(price or 0),
           "discount": _r2(discount or 0), "paid": _r2(paid or 0), "method": method,
           "utr": (utr if utr and utr != "N/A" else None), "ts": now(), "status": status}
    order_log[order_id] = rec
    db_mark()
    return rec


def invoice_create(order_rec: dict, promo_code=None):
    """Idempotent: returns the existing invoice if this order already has one."""
    oid = order_rec["order_id"]
    inv = invoices.get(oid)
    if inv:
        return inv
    counters["invoice"] += 1
    u = users.get(order_rec["uid"], {})
    inv = {
        "no": f"INV-{datetime.now(IST):%Y%m%d}-{counters['invoice']:04d}",
        "order_id": oid, "uid": order_rec["uid"], "username": u.get("username") or "",
        "name": u.get("name") or "", "post_id": order_rec.get("post_id"),
        "desc": f"Free Fire ID {order_rec.get('post_id')}", "price": order_rec["price"],
        "discount": order_rec["discount"], "promo": promo_code, "paid": order_rec["paid"],
        "method": order_rec["method"], "utr": order_rec.get("utr"), "ts": order_rec["ts"], "status": "PAID",
    }
    invoices[oid] = inv
    _db_save()
    return inv


def _method_label(m: str) -> str:
    return {"upi": "UPI (FamGateway)", "balance": "Wallet balance"}.get(m, str(m))


def invoice_text(inv: dict) -> str:
    brand = settings.get("brand") or BOT_NAME
    who = f"@{esc(inv['username'])}" if inv.get("username") else esc(inv.get("name") or "User")
    lines = [
        "🧾 <b>PAYMENT RECEIPT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🔢 Invoice No: <code>{esc(inv['no'])}</code>",
        f"🏪 Store: {brand}",
        f"👤 Customer: {who} (<code>{inv['uid']}</code>)",
        f"🆔 Order ID: <code>{esc(inv['order_id'])}</code>",
        f"📌 Post ID: <code>{esc(inv.get('post_id'))}</code>",
        f"📦 Item: {esc(inv.get('desc'))}",
        f"💰 Item price: {rupees(inv['price'])}",
    ]
    if inv.get("discount"):
        lines.append(f"🎟 Discount ({esc(inv.get('promo') or 'promo')}): −{rupees(inv['discount'])}")
    lines += [
        f"✅ Amount paid: <b>{rupees(inv['paid'])}</b>",
        f"💳 Payment method: {esc(_method_label(inv['method']))}",
        f"🏦 UTR / reference: " + (f"<code>{esc(inv['utr'])}</code>" if inv.get("utr") else "Not provided"),
        f"⏰ Date: {ist_str(inv['ts'])}",
        f"📌 Status: <b>{esc(inv['status'])}</b>",
        "━━━━━━━━━━━━━━━━━━",
        "🙏 Thank you for your purchase!",
    ]
    if settings.get("support"):
        lines.append(f"💬 Support: {esc(settings['support'])}")
    return "\n".join(lines)


def user_orders(uid: int) -> list:
    return sorted((o for o in order_log.values() if o["uid"] == uid and o["kind"] == "purchase"
                   and o.get("status") == "paid"), key=lambda o: o["ts"], reverse=True)


def all_admin_ids() -> list:
    return [ADMIN_ID] + [i for i in super_admins if i != ADMIN_ID]

# ─── KEYBOARD BUILDERS ────────────────────────────────────────────────────────
def btn(label: str, cb: str, style: str | None = None, icon: str | None = None):
    """Inline callback button (premium icon + colour, label kept as typed)."""
    return _inline_btn(label, _sc=False, _style=style, _icon=icon, callback_data=cb)


def ubtn(label: str, url: str, style: str | None = None):
    return _inline_btn(label, _sc=False, _style=style, url=url)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


def prem(text: str) -> str:
    """HTML text with standard emojis swapped for premium custom emojis (for methods the patch does not wrap)."""
    return _premium_text(text, "HTML", False)[0]


def main_reply_kb(user_id: int) -> ReplyKeyboardMarkup:
    """Bottom colored keyboard for private chat.

    Green  = buy / wallet actions
    Blue   = navigation / info
    Red    = admin-only
    """
    rows = [
        [style_btn("🛒 Buy FF ID", "success"), style_btn("💸 Sell Your ID", "primary")],
        [style_btn("👤 My Account", "primary"), style_btn("📦 My Orders", "primary")],
        [style_btn("💰 Add Balance", "success")],
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


_REV_SC = {v: k for k, v in _SC_MAP.items()}


def _btn_key(text: str) -> str:
    """Normalise a reply-keyboard label (plain or small-caps, with/without emoji) to e.g. 'buyffid'."""
    s = "".join(_REV_SC.get(ch, ch) for ch in (text or ""))
    return re.sub(r"[^a-z]", "", s.lower())


MENU_KEYS = {
    "buyffid": "buy", "sellyourid": "sell", "myaccount": "account",
    "myorders": "orders", "addbalance": "topup", "adminpanel": "admin",
}


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in super_admins


def money(x) -> str:
    return f"{float(x):g}"


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


async def notify_admins(bot, text: str, markup=None, html_mode: bool = True):
    """Send to the owner and every super admin."""
    for aid in all_admin_ids():
        try:
            await bot.send_message(chat_id=aid, text=text, parse_mode="HTML" if html_mode else None,
                                   reply_markup=markup)
        except Exception as e:
            log.warning("notify admin %s failed: %r", aid, e)


def user_pending(uid: int):
    for oid, info in pending_orders.items():
        if info["user_id"] == uid:
            return oid
    return None


def release_order(order_id: str):
    info = pending_orders.pop(order_id, None)
    _last_seen.pop(order_id, None)
    stop_timer(order_id)
    if info and info.get("post_id") and reserved.get(info["post_id"]) == order_id:
        reserved.pop(info["post_id"], None)
    if info:
        _db_save()
    return info


async def edit_q(q, text: str, markup=None):
    """Edit a callback message — caption if it is a photo card, text otherwise. 'Not modified' is ignored."""
    try:
        if getattr(q.message, "photo", None):
            await q.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except tge.BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ─── MESSAGE LIFECYCLE (tracked cleanup) ──────────────────────────────────────
# Only messages the bot itself created for this user's current flow are tracked.
# The welcome/menu message, receipts, credentials and order confirmations are never auto-deleted.
ui: dict             = {}   # {uid: {"menu": id, "card": id, "card_kind": "photo"|"text", "temp": [ids]}}
checkout: dict       = {}   # {uid: {"pid": str, "code": str|None}}
photo_sessions: dict = {}   # {uid: {...}} — photo collection (sell / admin add / admin edit)
sell_draft: dict     = {}   # {uid: {...}} — sell request being filled in
_restore_stage: dict = {}   # {admin uid: staged restore}
_timers: dict        = {}   # {order_id: asyncio.Task} countdown tasks
_pollers: dict       = {}   # {order_id: asyncio.Task} payment pollers


def _ui(uid: int) -> dict:
    return ui.setdefault(uid, {"menu": None, "card": None, "card_kind": None, "temp": []})


def ui_track(uid: int, msg, slot: str = "temp"):
    if msg is None:
        return msg
    u = _ui(uid)
    if slot == "temp":
        u["temp"].append(msg.message_id)
        del u["temp"][:-30]
    elif slot == "card":
        u["card"] = msg.message_id
    elif slot == "menu":
        u["menu"] = msg.message_id
    return msg


async def ui_clean(bot, chat_id: int, uid: int, card: bool = True, temp: bool = True):
    """Delete this user's obsolete bot-generated flow messages. Never touches the welcome/menu message."""
    u = _ui(uid)
    ids = []
    if temp:
        ids += u["temp"]
        u["temp"] = []
    if card and u.get("card"):
        ids.append(u["card"])
        u["card"], u["card_kind"] = None, None
    for mid in ids:
        await safe_delete(bot, chat_id, mid)


async def send_temp(bot, chat_id: int, uid: int, text: str, markup=None, **kw):
    """Send a message that belongs to the user's current flow (cleaned up later)."""
    m = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup, **kw)
    return ui_track(uid, m)


def reset_flows(uid: int):
    """Abort any half-finished input flow of this user."""
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    photo_sessions.pop(uid, None)
    sell_draft.pop(uid, None)


# ─── WELCOME / MENU ───────────────────────────────────────────────────────────
def brand_html() -> str:
    return esc(settings.get("brand") or BOT_NAME)


def welcome_text(u, uid: int) -> str:
    name = u.first_name or (f"@{u.username}" if u.username else "there")
    text = (
        f"🎮 <b>Welcome, {esc(name)}!</b>\n"
        f"to [ <b>{brand_html()}</b> ]\n\n"
        "Browse available Free Fire IDs and place your order in a few taps — "
        "or sell your own ID to us.\n\n"
        "Use the menu buttons below:\n\n"
        "🛒 <b>Buy FF ID</b> — browse the catalogue\n"
        "💸 <b>Sell Your ID</b> — submit your account and get an offer\n"
        "👤 <b>My Account</b> — balance and stats\n"
        "📦 <b>My Orders</b> — receipts and history\n"
        "💰 <b>Add Balance</b> — top up your wallet\n"
    )
    if is_admin(uid):
        text += "⚙️ <b>Admin Panel</b> — manage the store\n"
    return text


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    u = update.effective_user
    uid = u.id
    chat_id = update.effective_chat.id
    bot = context.bot
    text = welcome_text(u, uid)
    reply_kb = main_reply_kb(uid)
    log_event(uid, "start")

    if edit and update.callback_query:
        # Turn the current message (e.g. the force-join prompt) into the welcome message and
        # attach the reply keyboard with one tiny follow-up message.
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")
            _ui(uid)["menu"] = update.callback_query.message.message_id
        except Exception:
            m = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            ui_track(uid, m, "menu")
        await send_temp(bot, chat_id, uid, "👇 Use the menu buttons below.", reply_kb)
        return

    await ui_clean(bot, chat_id, uid)
    old_menu = _ui(uid).get("menu")
    try:
        m = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                   reply_markup=reply_kb, message_effect_id=MESSAGE_EFFECT_ID)
    except Exception:
        m = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_kb)
    ui_track(uid, m, "menu")
    if old_menu and old_menu != m.message_id:
        await safe_delete(bot, chat_id, old_menu)      # replace the previous welcome message (bot-owned)


async def main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    reset_flows(q.from_user.id)
    await send_main_menu(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    reset_flows(uid)
    if await fj_gate(update, context):
        return
    await send_main_menu(update, context)
    args = context.args or []
    if args and args[0].startswith("buy_"):                  # deep link from the inventory channel
        await open_listing_by_id(update, context, args[0][4:])


async def group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = context.bot.username
    if not username or not update.message:
        return
    await update.message.reply_text(
        "🛒 Open the bot in a private chat to buy IDs 👇",
        reply_markup=InlineKeyboardMarkup(
            [[ibt_url("Open Bot", f"https://t.me/{username}")]]
        ),
    )


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves every private user + blocks banned users."""
    u = update.effective_user
    if not u or u.is_bot:
        return
    name, uname = u.full_name or "", u.username or ""
    t = now()
    rec = users.get(u.id)
    if rec is None:
        users[u.id] = {"name": name, "username": uname, "banned": False, "first_seen": t, "last_seen": t}
        _data_save()
    else:
        rec["last_seen"] = t
        if rec.get("name") != name or rec.get("username") != uname:
            rec["name"], rec["username"] = name, uname
            _data_save()
    if users[u.id].get("banned") and not is_admin(u.id):
        try:
            if update.callback_query:
                await update.callback_query.answer("🚫 You have been banned from this bot.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🚫 You have been banned from this bot.")
        except Exception:
            pass
        raise ApplicationHandlerStop


# ─── MY ACCOUNT / MY ORDERS / WALLET HISTORY ─────────────────────────────────
LEDGER_LABEL = {"topup": "Top-up", "purchase": "Purchase", "refund": "Refund", "admin_adjust": "Admin adjustment"}


def account_text(uid: int, u=None) -> str:
    rec = users.get(uid, {})
    orders = user_orders(uid)
    spent = sum(o["paid"] for o in orders)
    uname = rec.get("username") or (u.username if u else "")
    name = rec.get("name") or (u.full_name if u else "") or "User"
    since = ist_str(rec["first_seen"], "%d %b %Y") if rec.get("first_seen") else "Not available"
    return (
        "👤 <b>My Account</b>\n\n"
        f"🆔 User ID: <code>{uid}</code>\n"
        f"👤 Name: {esc(name)}" + (f" (@{esc(uname)})" if uname else "") + "\n"
        f"💰 Wallet balance: <b>{rupees(user_balance(uid))}</b>\n"
        f"📦 Completed orders: <b>{len(orders)}</b>\n"
        f"💸 Total spent: <b>{rupees(spent)}</b>\n"
        f"📅 Member since: {since}"
    )


async def account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if await fj_gate(update, context):
        return
    await ui_clean(context.bot, chat_id, uid)
    log_event(uid, "account")
    await send_temp(context.bot, chat_id, uid, account_text(uid, update.effective_user), kb(
        [btn("💰 Add Balance", "tp_menu", "success"), btn("📦 My Orders", "acct_orders", "primary")],
        [btn("🧾 Wallet History", "acct_hist", "primary")],
    ))


async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if await fj_gate(update, context):
        return
    await ui_clean(context.bot, chat_id, uid)
    await _send_orders(context.bot, chat_id, uid)


async def _send_orders(bot, chat_id: int, uid: int):
    log_event(uid, "orders")
    mine = [invoices[o["order_id"]] for o in user_orders(uid) if o["order_id"] in invoices][:10]
    if not mine:
        await send_temp(bot, chat_id, uid, "📦 <b>My Orders</b>\n\nYou have no completed orders yet.",
                        kb([btn("🛒 Browse IDs", "buy_start", "success")]))
        return
    rows = [[btn(f"🧾 {i['no'][-8:]} · {i['post_id']} · {rupees(i['paid'])}", f"inv_{i['no']}", "primary")]
            for i in mine]
    await send_temp(bot, chat_id, uid,
                    "📦 <b>My Orders</b>\n\nTap an order to open its receipt:", InlineKeyboardMarkup(rows))


async def acct_orders_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _send_orders(context.bot, q.message.chat_id, q.from_user.id)


async def inv_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    no = (q.data or "")[len("inv_"):]
    inv = next((i for i in invoices.values() if i["no"] == no and i["uid"] == uid), None)
    if not inv:
        await q.answer("Receipt not found.", show_alert=True)
        return
    await q.answer()
    log_event(uid, "invoice_view", no)
    await send_temp(context.bot, q.message.chat_id, uid, invoice_text(inv))


async def acct_hist_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    rows = [e for e in ledger if e["uid"] == uid][-10:][::-1]
    if not rows:
        text = "🧾 <b>Wallet History</b>\n\nNo wallet transactions yet."
    else:
        lines = [f"{'➕' if e['amount'] > 0 else '➖'} {LEDGER_LABEL.get(e['type'], e['type'])}: "
                 f"<b>{rupees(abs(e['amount']))}</b> → balance {rupees(e['balance'])}\n"
                 f"    <i>{ist_str(e['ts'], '%d %b, %I:%M %p')}</i>" for e in rows]
        text = "🧾 <b>Wallet History</b> (last 10)\n\n" + "\n".join(lines)
    await send_temp(context.bot, q.message.chat_id, uid, text)

# ═══════════════════════════════════════════════════════════════════════════════
# 🛒 BUYER SIDE — catalogue, checkout, promo codes, UPI + wallet payment, top-up, countdown
# ═══════════════════════════════════════════════════════════════════════════════
from types import SimpleNamespace


def _not_modified(e) -> bool:
    return "not modified" in str(e).lower()


# ─── CATALOGUE ────────────────────────────────────────────────────────────────
def card_caption(pid: str, pos: int = 0, total: int = 0, img_idx: int = 0) -> str:
    p = posts[pid]
    n_img = len(p.get("images") or [])
    lines = [
        "🎮 <b>Free Fire ID</b>", "",
        f"📌 Post ID: <code>{esc(pid)}</code>",
        f"💰 Price: <b>{rupees(p['price'])}</b>",
    ]
    if p.get("info"):
        lines.append(f"📝 {esc(p['info'])}")
    if n_img:
        lines.append(f"🖼 Photo {img_idx + 1}/{n_img}")
    lines += ["", "⚡ Credentials are delivered automatically after verified payment."]
    if total:
        lines.append(f"\n📋 Listing {pos}/{total}")
    return "\n".join(lines)


def card_markup(pid: str, pos: int, total: int, img_idx: int = 0) -> InlineKeyboardMarkup:
    n_img = len(posts[pid].get("images") or [])
    rows = []
    if total > 1:
        rows.append([btn("Prev", f"cat_p_{pid}", "primary"),
                     btn(f"📋 {pos}/{total}", "noop", "primary"),
                     btn("Next", f"cat_n_{pid}", "primary")])
    if n_img > 1:
        rows.append([btn(f"🖼 Photo {img_idx + 1}/{n_img} — next", f"cat_i_{pid}_{(img_idx + 1) % n_img}", "primary")])
    rows.append([btn("🛒 Buy ID", f"cat_buy_{pid}", "success")])
    rows.append([btn("🔢 Enter Post ID", "cat_enter", "primary"), btn("❌ Close", "cat_close", "danger")])
    return InlineKeyboardMarkup(rows)


async def show_card(bot, chat_id: int, uid: int, pid: str, img_idx: int = 0, edit_mid: int | None = None,
                    log: bool = True):
    """Show/refresh the single catalogue card of this user. Edits in place when possible so old cards never pile up."""
    ids = avail_ids()
    if pid not in ids:
        return
    pos, total = ids.index(pid) + 1, len(ids)
    p = posts[pid]
    imgs = p.get("images") or []
    img_idx = (img_idx % len(imgs)) if imgs else 0
    cap = card_caption(pid, pos, total, img_idx)
    markup = card_markup(pid, pos, total, img_idx)
    u = _ui(uid)
    mid = edit_mid or u.get("card")
    kind_new = "photo" if imgs else "text"
    if log:
        log_event(uid, "view_id", pid)

    if mid and u.get("card_kind") == kind_new:
        try:
            if kind_new == "photo":
                try:
                    await bot.edit_message_media(
                        chat_id=chat_id, message_id=mid, reply_markup=markup,
                        media=InputMediaPhoto(media=imgs[img_idx], caption=prem(cap), parse_mode="HTML"))
                except tge.BadRequest as e:
                    if _not_modified(e):
                        return
                    await bot.edit_message_media(
                        chat_id=chat_id, message_id=mid, reply_markup=markup,
                        media=InputMediaPhoto(media=imgs[img_idx], caption=cap, parse_mode="HTML"))
            else:
                await bot.edit_message_text(chat_id=chat_id, message_id=mid, text=cap,
                                            parse_mode="HTML", reply_markup=markup)
            u["card"] = mid
            return
        except tge.BadRequest as e:
            if _not_modified(e):
                return
            log.warning("card edit failed, resending: %r", e)
        except Exception as e:
            log.warning("card edit failed, resending: %r", e)

    if mid:
        await safe_delete(bot, chat_id, mid)
        u["card"], u["card_kind"] = None, None
    m = None
    if imgs:
        try:
            m = await bot.send_photo(chat_id=chat_id, photo=imgs[img_idx], caption=cap,
                                     parse_mode="HTML", reply_markup=markup)
            kind_new = "photo"
        except Exception as e:
            log.warning("card photo send failed for %s: %r", pid, e)
    if m is None:
        m = await bot.send_message(chat_id=chat_id, text=cap, parse_mode="HTML", reply_markup=markup)
        kind_new = "text"
    u["card"], u["card_kind"] = m.message_id, kind_new


async def edit_card(bot, chat_id: int, uid: int, text: str, markup=None):
    """Edit the caption/text of this user's current card (used by checkout)."""
    u = _ui(uid)
    mid = u.get("card")
    if not mid:
        return
    try:
        if u.get("card_kind") == "photo":
            await bot.edit_message_caption(chat_id=chat_id, message_id=mid, caption=text,
                                           parse_mode="HTML", reply_markup=markup)
        else:
            await bot.edit_message_text(chat_id=chat_id, message_id=mid, text=text,
                                        parse_mode="HTML", reply_markup=markup)
    except tge.BadRequest as e:
        if not _not_modified(e):
            log.warning("edit_card failed: %r", e)


def _sync_card(q, uid: int):
    """Make sure the tracked card is the message the user is actually pressing (e.g. after a restart)."""
    u = _ui(uid)
    u["card"] = q.message.message_id
    u["card_kind"] = "photo" if getattr(q.message, "photo", None) else "text"


def find_pid(text: str):
    t = (text or "").strip()
    if t in posts or t in sold:
        return t
    low = t.lower()
    for pid in list(posts) + list(sold):
        if pid.lower() == low:
            return pid
    return None


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the catalogue. Works from the reply keyboard and from inline 'Buy Again' buttons."""
    q = update.callback_query
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    bot = context.bot
    if q:
        await q.answer()
    if await fj_gate(update, context):
        return
    user_state.pop(uid, None)
    await ui_clean(bot, chat_id, uid)
    if user_pending(uid):
        await send_temp(bot, chat_id, uid,
                        "⏳ <b>You already have a pending order.</b>\n\n"
                        "Pay using the invoice above or cancel it with ❌ Cancel.", menu_button())
        return
    ids = avail_ids()
    if not ids:
        await send_temp(bot, chat_id, uid,
                        "😔 <b>No IDs are available right now.</b>\n\nPlease check back soon — "
                        "or sell your own ID with 💸 <b>Sell Your ID</b>.")
        return
    log_event(uid, "catalogue")
    await show_card(bot, chat_id, uid, ids[0])


async def open_listing(bot, chat_id: int, uid: int, text: str):
    """Show one listing by Post ID (typed by the user or opened from a deep link)."""
    pid = find_pid(text)
    log_event(uid, "search", (text or "")[:30])
    if pid is None:
        await send_temp(bot, chat_id, uid, f"❌ <b>Post ID <code>{esc(text[:30])}</code> was not found.</b>\n\n"
                                           "Please check the ID and try again.", menu_button())
    elif pid in sold:
        await send_temp(bot, chat_id, uid, "❌ <b>This ID has already been sold.</b>\n\n"
                                           "You can send another Post ID or browse the catalogue.", menu_button())
    elif pid in reserved:
        await send_temp(bot, chat_id, uid, "⏳ <b>Another buyer's payment for this ID is in progress.</b>\n\n"
                                           "Please try again in 5 minutes.", menu_button())
    else:
        await ui_clean(bot, chat_id, uid)
        await show_card(bot, chat_id, uid, pid)


async def open_listing_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE, pid_text: str):
    await open_listing(context.bot, update.effective_chat.id, update.effective_user.id, pid_text)


async def buyer_post_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != BUYER_WAIT_POST_ID:
        return
    user_state.pop(uid, None)
    if user_pending(uid):
        await send_temp(context.bot, update.effective_chat.id, uid,
                        "⏳ Please complete your pending order first, or tap ❌ Cancel.", menu_button())
        return
    await open_listing(context.bot, update.effective_chat.id, uid, update.message.text or "")


async def noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def in_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a text-input prompt (promo code, post ID, top-up amount...)."""
    q = update.callback_query
    uid = q.from_user.id
    await q.answer("Cancelled")
    user_state.pop(uid, None)
    await safe_delete(context.bot, q.message.chat_id, q.message.message_id)


async def cat_nav_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    direction = 1 if q.data.startswith("cat_n_") else -1
    pid = q.data[len("cat_n_"):]
    ids = avail_ids()
    _sync_card(q, uid)
    if not ids:
        await edit_q(q, "😔 <b>No IDs are available right now.</b>\n\nPlease check back soon!", None)
        return
    i = (ids.index(pid) + direction) % len(ids) if pid in ids else 0
    await show_card(context.bot, q.message.chat_id, uid, ids[i], edit_mid=q.message.message_id)


async def cat_img_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    try:
        pid, idx = q.data[len("cat_i_"):].rsplit("_", 1)
        idx = int(idx)
    except ValueError:
        return
    _sync_card(q, uid)
    if pid not in avail_ids():
        ids = avail_ids()
        if ids:
            await show_card(context.bot, q.message.chat_id, uid, ids[0], edit_mid=q.message.message_id)
        return
    await show_card(context.bot, q.message.chat_id, uid, pid, img_idx=idx, edit_mid=q.message.message_id, log=False)


async def cat_close_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    user_state.pop(uid, None)
    checkout.pop(uid, None)
    u = _ui(uid)
    if u.get("card") == q.message.message_id:
        u["card"], u["card_kind"] = None, None
    await safe_delete(context.bot, q.message.chat_id, q.message.message_id)


async def cat_enter_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    user_state[uid] = BUYER_WAIT_POST_ID
    await send_temp(context.bot, q.message.chat_id, uid,
                    "🔢 <b>Enter Post ID</b>\n\nSend the Post ID of the ID you want to buy "
                    "(for example the one shown in our channel).",
                    kb([btn("❌ Cancel", "in_cancel", "danger")]))


# ─── CHECKOUT ─────────────────────────────────────────────────────────────────
def checkout_view(uid: int):
    """(text, markup) for the user's checkout, or None if the listing is gone."""
    co = checkout.get(uid)
    if not co:
        return None
    qt = price_quote(co["pid"], uid, co.get("code"))
    if not qt["ok"]:
        return None
    if qt["promo_error"]:                       # a previously applied code stopped being valid
        co["code"] = None
    bal = user_balance(uid)
    lines = ["🛒 <b>Checkout</b>", "", f"📌 Post ID: <code>{esc(co['pid'])}</code>",
             f"💰 Price: {rupees(qt['price'])}"]
    if qt["code"]:
        lines.append(f"🎟 Promo <code>{esc(qt['code'])}</code>: −{rupees(qt['discount'])}")
    lines.append(f"✅ <b>You pay: {rupees(qt['final'])}</b>")
    lines.append(f"💼 Wallet balance: {rupees(bal)}")
    if qt["promo_error"]:
        lines.append(f"\n⚠️ {esc(qt['promo_error'])}")
    lines.append("\n<i>The final price is always calculated by the bot when you pay.</i>")
    rows = [[btn(f"✅ Pay {rupees(qt['final'])} via UPI", "co_upi", "success")]]
    if bal + 1e-9 >= qt["final"]:
        rows.append([btn(f"💰 Pay with Wallet ({rupees(bal)})", "co_bal", "success")])
    else:
        rows.append([btn("💰 Add Balance", "tp_menu", "primary")])
    rows.append([btn("❌ Remove Promo", "co_nopromo", "danger")] if qt["code"]
                else [btn("🎟 Apply Promo Code", "co_promo", "primary")])
    rows.append([btn("🔙 Back", "co_back", "danger")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cat_buy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    pid = q.data[len("cat_buy_"):]
    if user_pending(uid):
        await q.answer("You already have a pending order. Pay or cancel it first.", show_alert=True)
        return
    # re-validate availability — another customer may have reserved/bought it meanwhile
    if pid not in posts or pid in reserved or pid in sold or posts[pid].get("missing_creds"):
        await q.answer("Sorry, this ID is no longer available.", show_alert=True)
        _sync_card(q, uid)
        ids = avail_ids()
        if ids:
            await show_card(context.bot, q.message.chat_id, uid, ids[0], edit_mid=q.message.message_id)
        else:
            await edit_q(q, "😔 <b>No IDs are available right now.</b>", None)
        return
    await q.answer()
    _sync_card(q, uid)
    user_state.pop(uid, None)
    checkout[uid] = {"pid": pid, "code": None}
    log_event(uid, "checkout", pid)
    text, markup = checkout_view(uid)
    await edit_q(q, text, markup)


async def _refresh_checkout(bot, chat_id: int, uid: int):
    v = checkout_view(uid)
    if v:
        await edit_card(bot, chat_id, uid, v[0], v[1])


async def co_back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    user_state.pop(uid, None)
    co = checkout.pop(uid, None)
    _sync_card(q, uid)
    ids = avail_ids()
    if co and co["pid"] in ids:
        await show_card(context.bot, q.message.chat_id, uid, co["pid"], edit_mid=q.message.message_id, log=False)
    elif ids:
        await show_card(context.bot, q.message.chat_id, uid, ids[0], edit_mid=q.message.message_id)
    else:
        await edit_q(q, "😔 <b>No IDs are available right now.</b>", None)


async def co_promo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid not in checkout:
        await q.answer("This checkout expired. Please open the catalogue again.", show_alert=True)
        return
    await q.answer()
    _sync_card(q, uid)
    user_state[uid] = BUYER_WAIT_PROMO
    await send_temp(context.bot, q.message.chat_id, uid,
                    "🎟 <b>Promo code</b>\n\nSend your promo code now.",
                    kb([btn("❌ Cancel", "in_cancel", "danger")]))


async def co_nopromo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    co = checkout.get(uid)
    if not co:
        await q.answer("This checkout expired.", show_alert=True)
        return
    await q.answer("Promo removed")
    co["code"] = None
    _sync_card(q, uid)
    v = checkout_view(uid)
    if v:
        await edit_q(q, v[0], v[1])


async def promo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != BUYER_WAIT_PROMO:
        return
    chat_id = update.effective_chat.id
    bot = context.bot
    co = checkout.get(uid)
    code = promo_norm(update.message.text)
    if not co:
        user_state.pop(uid, None)
        await send_temp(bot, chat_id, uid, "⚠️ This checkout expired. Please open the catalogue again.")
        return
    log_event(uid, "promo_try", code[:20])
    qt = price_quote(co["pid"], uid, code if PROMO_CODE_RE.match(code or "") else None)
    if not qt["ok"]:
        user_state.pop(uid, None)
        await send_temp(bot, chat_id, uid, "⚠️ This ID is no longer available.")
        return
    if qt["code"]:
        user_state.pop(uid, None)
        co["code"] = qt["code"]
        await send_temp(bot, chat_id, uid,
                        "✅ <b>Promo code applied!</b>\n\n"
                        f"Original price: {rupees(qt['price'])}\n"
                        f"Discount: −{rupees(qt['discount'])}\n"
                        f"<b>Final payable amount: {rupees(qt['final'])}</b>")
        await _refresh_checkout(bot, chat_id, uid)
    else:
        why = qt["promo_error"] or "This promo code does not exist."
        await send_temp(bot, chat_id, uid,
                        f"❌ <b>Promo code not accepted</b>\n\n{esc(why)}\n\n"
                        "Send another code or tap ❌ Cancel.",
                        kb([btn("❌ Cancel", "in_cancel", "danger")]))


# ─── PAYMENT INVOICE (QR) + COUNTDOWN ────────────────────────────────────────
def invoice_caption(order_id: str, info: dict, remaining: int | None = None) -> str:
    kind = info.get("kind", "purchase")
    payable = info.get("payable")
    lines = ["🧾 <b>AUTO-PAY INVOICE</b>", "━━━━━━━━━━━━━━━━━━"]
    if kind == "purchase":
        lines.append(f"📌 Post ID  : <code>{esc(info.get('post_id'))}</code>")
        if info.get("discount"):
            lines.append(f"💰 Price    : {rupees(info['price'])}")
            lines.append(f"🎟 Promo {esc(info.get('promo'))}: −{rupees(info['discount'])}")
    else:
        lines.append("💰 <b>Wallet top-up</b>")
    lines += [f"✅ Amount   : <b>₹{esc(payable)}</b>", f"🆔 Order ID : <code>{esc(order_id)}</code>",
              "━━━━━━━━━━━━━━━━━━", ""]
    if remaining is not None and remaining > 0:
        filled = max(0, min(10, int(round((COUNTDOWN_SECONDS - remaining) / COUNTDOWN_SECONDS * 10))))
        bar = "▰" * filled + "▱" * (10 - filled)
        lines += [f"⏳ <b>Please wait while we prepare your order…</b>\n{bar}  {remaining}s",
                  "<i>This countdown is only a progress indicator — it is not a payment confirmation.</i>"]
    else:
        lines.append("🔄 <b>Waiting for your payment…</b>")
    lines += ["", "⏳ <b>The QR is valid for 5 minutes</b>",
              f"📱 Pay exactly <b>₹{esc(payable)}</b> — do not edit the amount or note",
              ("💎 Your credentials arrive automatically after payment" if kind == "purchase"
               else "💎 Your wallet is credited automatically after payment"),
              "🔄 <i>The bot checks every 2 seconds — you will be notified the moment payment is received.</i>"]
    return "\n".join(lines)


def _invoice_markup(order_id: str):
    return kb([btn("❌ Cancel", f"cancel_{order_id}", "danger")])


async def _edit_invoice(bot, info: dict, caption: str, order_id: str):
    try:
        if info.get("msg_kind", "photo") == "photo":
            await bot.edit_message_caption(chat_id=info["chat_id"], message_id=info["msg_id"], caption=caption,
                                           parse_mode="HTML", reply_markup=_invoice_markup(order_id))
        else:
            await bot.edit_message_text(chat_id=info["chat_id"], message_id=info["msg_id"], text=caption,
                                        parse_mode="HTML", reply_markup=_invoice_markup(order_id))
    except tge.RetryAfter:
        pass
    except tge.BadRequest as e:
        if not _not_modified(e):
            log.debug("invoice edit failed: %r", e)
    except Exception as e:
        log.debug("invoice edit failed: %r", e)


def start_timer(order_id: str, bot):
    t = _timers.get(order_id)
    if t and not t.done():
        return                                   # never two timers for one order
    _timers[order_id] = asyncio.create_task(_countdown(order_id, bot))


def stop_timer(order_id: str):
    t = _timers.pop(order_id, None)
    if t and not t.done():
        t.cancel()


async def _countdown(order_id: str, bot):
    """Updates the invoice caption for ~1 minute. Display only — never expires or releases anything."""
    try:
        while True:
            await asyncio.sleep(5)
            info = pending_orders.get(order_id)
            if not info or not info.get("msg_id"):
                return
            remaining = int(COUNTDOWN_SECONDS - (now() - info.get("created", now())))
            await _edit_invoice(bot, info, invoice_caption(order_id, info, remaining if remaining > 0 else None),
                                order_id)
            if remaining <= 0:
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("countdown error for %s", order_id)
    finally:
        if _timers.get(order_id) is asyncio.current_task():
            _timers.pop(order_id, None)


async def create_gateway_invoice(bot, chat_id: int, uid: int, kind: str, amount: float, post_id=None,
                                 price=None, discount: float = 0.0, promo=None) -> str:
    """Create a FamGateway order + QR invoice message, start poller & countdown. Returns order_id.
    Raises on failure (after releasing any reservation)."""
    payload = {
        "api_key": FG_API_KEY,
        "amount": amount,
        "customer_name": ((users.get(uid, {}).get("name") or "Buyer").split(" ")[0])[:40] or "Buyer",
    }
    data = await fg_create_order(payload)
    if data.get("status") != "success":
        raise RuntimeError(f"gateway said: {data}")
    od = data["data"]
    order_id, payable = od["order_id"], od["payable_amount"]
    qr_url, upi_intent = od["qr_url"], od.get("upi_intent")
    info = {"user_id": uid, "chat_id": chat_id, "post_id": post_id, "msg_id": None, "kind": kind,
            "amount": amount, "payable": payable, "price": price, "discount": discount, "promo": promo,
            "created": now(), "method": "upi", "msg_kind": "photo"}
    pending_orders[order_id] = info
    if kind == "purchase" and post_id:
        reserved[post_id] = order_id
    _db_save()

    caption = invoice_caption(order_id, info, COUNTDOWN_SECONDS)
    markup = _invoice_markup(order_id)
    qr_png = await asyncio.to_thread(make_branded_qr, upi_intent)
    sent = None
    try:
        for photo in ([qr_png] if qr_png else []) + [qr_url]:
            try:
                sent = await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption,
                                            parse_mode="HTML", reply_markup=markup)
                break
            except Exception:
                log.warning("QR photo send failed (%s)", "branded" if photo is qr_png else "gateway")
        if sent is None:
            info["msg_kind"] = "text"
            sent = await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=markup)
    except Exception:
        log.exception("invoice send failed")
        release_order(order_id)
        raise
    info["msg_id"] = sent.message_id
    _db_save()
    log_event(uid, "order_created", f"{kind} {post_id or ''} {rupees(amount)}".strip())
    start_timer(order_id, bot)
    start_poller(order_id, bot)
    return order_id


async def co_upi_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    chat_id = q.message.chat_id
    bot = context.bot
    co = checkout.get(uid)
    if not co:
        await q.answer("This checkout expired. Please open the catalogue again.", show_alert=True)
        return
    pid = co["pid"]
    if user_pending(uid):
        await q.answer("You already have a pending order. Pay or cancel it first.", show_alert=True)
        return
    qt = price_quote(pid, uid, co.get("code"))
    if not qt["ok"] or pid in reserved or pid in sold:
        await q.answer("Sorry, this ID is no longer available.", show_alert=True)
        checkout.pop(uid, None)
        return
    if qt["promo_error"]:                                       # re-validated at finalisation
        co["code"] = None
        await q.answer(f"Promo removed: {qt['promo_error']}", show_alert=True)
        _sync_card(q, uid)
        await _refresh_checkout(bot, chat_id, uid)
        return
    reserved[pid] = "creating"            # reserve right away — no await between the checks above and here
    await q.answer()
    _sync_card(q, uid)
    await edit_q(q, "⏳ <b>Please wait while we prepare your order…</b>\n\nThis usually takes a few seconds.", None)
    try:
        await create_gateway_invoice(bot, chat_id, uid, "purchase", qt["final"], post_id=pid,
                                     price=qt["price"], discount=qt["discount"], promo=qt["code"])
    except Exception as e:
        log.exception("create-order failed")
        if reserved.get(pid) == "creating":
            reserved.pop(pid, None)
        v = checkout_view(uid)
        await edit_q(q, "❌ <b>Could not generate the payment.</b>\n\nPlease try again in a moment.\n\n"
                        + (v[0] if v else ""), v[1] if v else None)
        await notify_admin(bot, f"⚠️ FamGateway order create failed: {e!r}"[:900])
        return
    checkout.pop(uid, None)
    u = _ui(uid)
    await safe_delete(bot, chat_id, u.get("card") or q.message.message_id)   # the invoice replaces the card
    u["card"], u["card_kind"] = None, None


async def co_bal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    chat_id = q.message.chat_id
    bot = context.bot
    co = checkout.get(uid)
    if not co:
        await q.answer("This checkout expired. Please open the catalogue again.", show_alert=True)
        return
    pid = co["pid"]
    if user_pending(uid):
        await q.answer("You already have a pending order. Pay or cancel it first.", show_alert=True)
        return
    # ── atomic section: nothing below awaits until the wallet is charged and the ID reserved ──
    qt = price_quote(pid, uid, co.get("code"))
    if not qt["ok"] or pid in reserved or pid in sold:
        await q.answer("Sorry, this ID is no longer available.", show_alert=True)
        checkout.pop(uid, None)
        return
    if qt["promo_error"]:
        co["code"] = None
        await q.answer(f"Promo removed: {qt['promo_error']}", show_alert=True)
        _sync_card(q, uid)
        await _refresh_checkout(bot, chat_id, uid)
        return
    final = qt["final"]
    order_id = "WAL-" + secrets.token_hex(5).upper()
    try:
        ledger_add(uid, "purchase", -final, ref=order_id, note=f"Post {pid}")
    except ValueError:
        await q.answer("Insufficient wallet balance. Please add balance first.", show_alert=True)
        return
    reserved[pid] = order_id
    pending_orders[order_id] = {"user_id": uid, "chat_id": chat_id, "post_id": pid, "msg_id": None,
                                "kind": "purchase", "amount": final, "payable": final, "price": qt["price"],
                                "discount": qt["discount"], "promo": qt["code"], "created": now(),
                                "method": "balance"}
    _db_save()
    # ── end atomic section ──
    await q.answer("Processing your order…")
    checkout.pop(uid, None)
    u = _ui(uid)
    await safe_delete(bot, chat_id, u.get("card") or q.message.message_id)
    u["card"], u["card_kind"] = None, None
    try:
        await deliver_order(order_id, {"utr": None, "sender": None}, SimpleNamespace(bot=bot))
    except Exception:
        log.exception("wallet purchase failed — refunding")
        if order_id not in order_log:
            release_order(order_id)
            try:
                ledger_add(uid, "refund", final, ref=order_id, note=f"Refund {pid}")
            except Exception:
                log.exception("refund failed")
            await bot.send_message(chat_id, "❌ Something went wrong. Your wallet was refunded — please try again.")
            await notify_admin(bot, f"⚠️ Wallet purchase {order_id} failed and was refunded.")


# ─── WALLET TOP-UP ────────────────────────────────────────────────────────────
def _fmt_amt(a: float) -> str:
    return f"{a:g}"


async def topup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Add Balance' — from the reply keyboard or an inline button."""
    q = update.callback_query
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    bot = context.bot
    if q:
        await q.answer()
    if await fj_gate(update, context):
        return
    user_state.pop(uid, None)
    if not q:
        await ui_clean(bot, chat_id, uid)
    if user_pending(uid):
        await send_temp(bot, chat_id, uid, "⏳ <b>You already have a pending payment.</b>\n\n"
                                           "Complete or cancel it first.", menu_button())
        return
    log_event(uid, "topup_menu")
    rows, row = [], []
    for a in TOPUP_PRESETS:
        if TOPUP_MIN <= a <= TOPUP_MAX:
            row.append(btn(f"💰 ₹{_fmt_amt(a)}", f"tp_a_{_fmt_amt(a)}", "success"))
            if len(row) == 2:
                rows.append(row)
                row = []
    if row:
        rows.append(row)
    rows.append([btn("✏️ Custom Amount", "tp_custom", "primary")])
    rows.append([btn("❌ Close", "in_cancel", "danger")])
    await send_temp(bot, chat_id, uid,
                    "💰 <b>Add Balance</b>\n\n"
                    f"Current balance: <b>{rupees(user_balance(uid))}</b>\n"
                    f"Choose an amount (min {rupees(TOPUP_MIN)}, max {rupees(TOPUP_MAX)}).\n\n"
                    "<i>Your balance is credited only after the payment is verified.</i>",
                    InlineKeyboardMarkup(rows))


async def _start_topup(bot, chat_id: int, uid: int, amount: float):
    if user_pending(uid):
        await send_temp(bot, chat_id, uid, "⏳ You already have a pending payment. Complete or cancel it first.",
                        menu_button())
        return
    if not (TOPUP_MIN <= amount <= TOPUP_MAX):
        await send_temp(bot, chat_id, uid, f"❌ Amount must be between {rupees(TOPUP_MIN)} and {rupees(TOPUP_MAX)}.")
        return
    await ui_clean(bot, chat_id, uid)
    wait = await send_temp(bot, chat_id, uid, "⏳ <b>Please wait while we prepare your payment…</b>")
    try:
        await create_gateway_invoice(bot, chat_id, uid, "topup", _r2(amount))
        log_event(uid, "topup_start", rupees(amount))
    except Exception as e:
        log.exception("top-up order failed")
        await send_temp(bot, chat_id, uid, "❌ Could not generate the payment. Please try again in a moment.")
        await notify_admin(bot, f"⚠️ FamGateway top-up order failed: {e!r}"[:900])
        return
    await safe_delete(bot, chat_id, wait.message_id)


async def tp_amt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        amount = float(q.data[len("tp_a_"):])
    except ValueError:
        return
    await _start_topup(context.bot, q.message.chat_id, q.from_user.id, amount)


async def tp_custom_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    user_state[uid] = BUYER_WAIT_TOPUP
    await send_temp(context.bot, q.message.chat_id, uid,
                    f"✏️ <b>Custom amount</b>\n\nSend the amount in ₹ (min {rupees(TOPUP_MIN)}, max {rupees(TOPUP_MAX)}).",
                    kb([btn("❌ Cancel", "in_cancel", "danger")]))


async def topup_amount_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != BUYER_WAIT_TOPUP:
        return
    raw = (update.message.text or "").strip().replace("₹", "")
    if not re.fullmatch(r"\d{1,6}(\.\d{1,2})?", raw):
        await send_temp(context.bot, update.effective_chat.id, uid, "❌ Please send a valid amount, e.g. <code>100</code>.",
                        kb([btn("❌ Cancel", "in_cancel", "danger")]))
        return
    amount = float(raw)
    if not (TOPUP_MIN <= amount <= TOPUP_MAX):
        await send_temp(context.bot, update.effective_chat.id, uid,
                        f"❌ Amount must be between {rupees(TOPUP_MIN)} and {rupees(TOPUP_MAX)}.",
                        kb([btn("❌ Cancel", "in_cancel", "danger")]))
        return
    user_state.pop(uid, None)
    await _start_topup(context.bot, update.effective_chat.id, uid, amount)


# ─── ORDER OUTCOMES ───────────────────────────────────────────────────────────
async def _send_credentials(bot, chat_id: int, order_id: str, pid: str, creds: str, utr, sender, method: str) -> bool:
    markup = kb([btn("📦 My Orders", "acct_orders", "primary")])
    e = html.escape
    utr_line = f"🏦 UTR     : <code>{e(str(utr))}</code>\n" if utr and utr != "N/A" else ""
    paid_by = "Wallet balance" if method == "balance" else (sender if sender and sender != "N/A" else "Not provided")
    html_text = (
        "✅ <b>PAYMENT VERIFIED!</b>\n\n"
        "🎮 <b>FF ID Credentials:</b>\n"
        f"<pre>{e(creds)}</pre>\n"
        f"📌 Post ID : <code>{e(pid)}</code>\n"
        f"🆔 Order   : <code>{e(order_id)}</code>\n"
        f"{utr_line}"
        f"👤 Paid by : {e(str(paid_by))}\n\n"
        "🙏 <b>Thank you for your purchase! Enjoy!</b>\n"
        "⚠️ Please take a screenshot of the credentials!"
    )
    plain = (f"✅ PAYMENT VERIFIED!\n\n🎮 FF ID Credentials:\n{creds}\n\nPost ID : {pid}\nOrder   : {order_id}\n"
             f"Paid by : {paid_by}\n\nThank you for your purchase! Enjoy!\nPlease take a screenshot of the credentials!")
    try:
        await bot.send_message(chat_id, html_text, parse_mode="HTML", reply_markup=markup)
        return True
    except Exception:
        log.exception("HTML delivery failed, retrying as plain text")
    try:
        await bot.send_message(chat_id, plain, reply_markup=markup)
        return True
    except Exception:
        log.exception("plain delivery failed too")
        return False


async def deliver_order(order_id: str, data: dict, context):
    """Payment confirmed -> finalise. Idempotent: a repeated verification event does nothing."""
    info = pending_orders.pop(order_id, None)
    if not info:
        return
    _last_seen.pop(order_id, None)
    stop_timer(order_id)
    if order_id in order_log:
        return
    bot = context.bot
    utr = data.get("utr") or "N/A"
    sender = data.get("sender") or "N/A"
    method = info.get("method", "upi")
    log.info("order %s PAID | kind=%s post=%s utr=%s sender=%s", order_id, info.get("kind"),
             info.get("post_id"), utr, sender)
    if info.get("msg_id"):
        await safe_delete(bot, info["chat_id"], info["msg_id"])
    if info.get("kind") == "topup":
        await _finish_topup(order_id, info, utr, sender, bot)
    else:
        await _finish_purchase(order_id, info, utr, sender, method, bot)


def _paid_amount(info: dict) -> float:
    try:
        return _r2(float(info.get("payable")))
    except (TypeError, ValueError):
        return _r2(info.get("amount") or 0)


async def _finish_purchase(order_id: str, info: dict, utr, sender, method: str, bot):
    pid, chat_id, uid = info["post_id"], info["chat_id"], info["user_id"]
    # ── synchronous part: record everything BEFORE any network call ──
    reserved.pop(pid, None)
    post = posts.pop(pid, None)
    sold.add(pid)
    paid = _paid_amount(info) if method == "upi" else _r2(info["amount"])
    if post is None:
        if method == "balance":
            ledger_add(uid, "refund", info["amount"], ref=order_id, note=f"Refund {pid}")
        order_log_add(order_id, "purchase", uid, pid, info.get("price"), info.get("discount"), paid, method, utr,
                      status="paid_no_item")
        _db_save()
        text = ("✅ Payment received, but the ID could not be released. An admin will contact you shortly."
                if method == "upi" else "✅ Your wallet was refunded because the ID is no longer available.")
        try:
            await bot.send_message(chat_id, text, reply_markup=menu_button())
        except Exception:
            log.exception("could not message buyer")
        await notify_admin(bot, f"⚠️ Paid order {order_id} (post {pid}, UTR {utr}) but post data is missing.")
        return
    sold_meta[pid] = {"order_id": order_id, "uid": uid, "ts": now(), "price": post["price"],
                      "channel_msg": post.get("channel_msg"), "channel_kind": post.get("channel_kind"),
                      "creds": post["creds"]}                       # kept until delivery is confirmed
    rec = order_log_add(order_id, "purchase", uid, pid, info.get("price"), info.get("discount"), paid, method, utr)
    rec["delivered"] = False
    if info.get("promo"):
        promo_redeem(info["promo"], uid, order_id, info.get("discount") or 0)     # only now is it redeemed
    inv = invoice_create(rec, info.get("promo"))
    _db_save()
    log_event(uid, "paid" if method == "upi" else "balance_purchase", pid)
    # ── delivery ──
    ok = await _send_credentials(bot, chat_id, order_id, pid, post["creds"], utr, sender, method)
    if ok:
        sold_meta.get(pid, {}).pop("creds", None)
        rec["delivered"] = True
        _db_save()
    else:
        await notify_admin(bot, f"⚠️ DELIVERY FAILED — buyer paid but the message was not sent.\n"
                                f"Buyer chat id: {chat_id}\nOrder: {order_id}\nUTR: {utr}\n\nCredentials:\n{post['creds']}")
    try:
        await bot.send_message(chat_id, invoice_text(inv), parse_mode="HTML")    # receipt: never auto-deleted
    except Exception:
        log.exception("receipt send failed")
    await notify_admins(bot, f"💸 <b>Sale!</b> Post <code>{esc(pid)}</code> | Order <code>{esc(order_id)}</code> | "
                             f"{rupees(paid)} via {esc(_method_label(method))} | Buyer <code>{uid}</code>")
    try:
        await channel_mark_sold(bot, pid)
    except Exception:
        log.exception("channel update failed")
    try:
        await sell_on_sold(bot, post, pid)
    except Exception:
        log.exception("seller notification failed")


async def _finish_topup(order_id: str, info: dict, utr, sender, bot):
    uid, chat_id, amount = info["user_id"], info["chat_id"], _r2(info["amount"])
    entry = ledger_add(uid, "topup", amount, ref=order_id, note="UPI top-up")     # idempotent per order
    order_log_add(order_id, "topup", uid, None, amount, 0, _paid_amount(info), "upi", utr)
    _db_save()
    log_event(uid, "topup_paid", rupees(amount))
    if entry is None:
        return                                                                  # duplicate verification event
    text = (
        "✅ <b>TOP-UP SUCCESSFUL!</b>\n\n"
        f"💰 Added: <b>{rupees(amount)}</b>\n"
        f"💼 New balance: <b>{rupees(entry['balance'])}</b>\n"
        f"🆔 Order: <code>{esc(order_id)}</code>\n"
        + (f"🏦 UTR: <code>{esc(utr)}</code>\n" if utr and utr != "N/A" else "")
        + f"⏰ {ist_str(now())}"
    )
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb(
            [btn("🛒 Browse IDs", "buy_start", "success")]))
    except Exception:
        log.exception("top-up confirmation failed")
    await notify_admins(bot, f"💰 Top-up {rupees(amount)} by <code>{uid}</code> (order <code>{esc(order_id)}</code>)")


async def expire_order(order_id: str, context):
    info = release_order(order_id)
    if not info:
        return
    log.info("order %s expired", order_id)
    log_event(info["user_id"], "expired", info.get("post_id") or "topup")
    if info.get("msg_id"):
        await safe_delete(context.bot, info["chat_id"], info["msg_id"])
    again = ("buy_start", "🔄 Buy Again") if info.get("kind", "purchase") == "purchase" else ("tp_menu", "🔄 Try Again")
    try:
        await context.bot.send_message(
            info["chat_id"],
            "⏰ <b>Payment time is over!</b>\n\nThe order expired. If you already paid, it will still be "
            "credited once the gateway confirms it. Otherwise you can try again.",
            parse_mode="HTML",
            reply_markup=kb([btn(again[1], again[0], "success"), btn("🏠 Home", "main_menu", "primary")]),
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
    status, data = await fg_status(order_id)             # last-chance check before cancelling
    if status == "success":
        await deliver_order(order_id, data, context)
        return
    if release_order(order_id) is None:
        return
    log_event(uid, "cancelled", info.get("post_id") or "topup")
    await safe_delete(context.bot, chat_id, query.message.message_id)
    await context.bot.send_message(chat_id, "❌ Order cancelled. Tap below to browse again.",
                                   reply_markup=kb([btn("🛒 Browse IDs", "buy_start", "success")]))


# ─── PAYMENT POLLER ───────────────────────────────────────────────────────────
def start_poller(order_id: str, bot):
    t = _pollers.get(order_id)
    if t and not t.done():
        return                                           # never two pollers for one order
    task = asyncio.create_task(poll_payment(order_id, SimpleNamespace(bot=bot)))
    _pollers[order_id] = task
    _tasks.add(task)

    def _done(tk, oid=order_id):
        _tasks.discard(tk)
        if _pollers.get(oid) is tk:
            _pollers.pop(oid, None)
    task.add_done_callback(_done)


async def poll_payment(order_id: str, context):
    """Polls FamGateway every POLL_INTERVAL seconds; the first check is immediate.
    The deadline is derived from the order's creation time, so it also works after a restart."""
    info = pending_orders.get(order_id)
    deadline = (info.get("created", now()) if info else now()) + POLL_MAX_SECONDS
    first = True
    while now() < deadline:
        if not first:
            await asyncio.sleep(POLL_INTERVAL)
        first = False
        if order_id not in pending_orders:
            return                                       # cancelled / delivered by another code path
        try:
            status, data = await fg_status(order_id)
            if status == "success":
                await deliver_order(order_id, data, context)
                return
            if status == "expired":
                await expire_order(order_id, context)
                return
        except Exception:
            log.exception("poll error for %s", order_id)
    if order_id in pending_orders:                       # last look so a payment made at the very end is not lost
        try:
            status, data = await fg_status(order_id)
            if status == "success":
                await deliver_order(order_id, data, context)
                return
        except Exception:
            log.exception("final poll error for %s", order_id)
        await expire_order(order_id, context)


async def recover_pending_orders(bot):
    """Startup: finish interrupted deliveries and resume watching unpaid orders against the real gateway status."""
    ctx = SimpleNamespace(bot=bot)
    for oid, rec in list(order_log.items()):
        if rec.get("kind") == "purchase" and rec.get("status") == "paid" and rec.get("delivered") is False:
            pid = rec.get("post_id")
            creds = (sold_meta.get(pid) or {}).get("creds")
            chat_id = rec["uid"]
            if creds and await _send_credentials(bot, chat_id, oid, pid, creds, rec.get("utr"), None, rec["method"]):
                sold_meta[pid].pop("creds", None)
                rec["delivered"] = True
                inv = invoices.get(oid)
                if inv:
                    try:
                        await bot.send_message(chat_id, invoice_text(inv), parse_mode="HTML")
                    except Exception:
                        pass
                _db_save()
                log.info("re-delivered interrupted order %s", oid)
            else:
                await notify_admin(bot, f"⚠️ Order {oid} (post {pid}) is paid but was not delivered. Check manually.")
    for oid, info in list(pending_orders.items()):
        if info.get("method") == "balance":                # wallet purchase interrupted after the charge
            asyncio.create_task(deliver_order(oid, {"utr": None, "sender": None}, ctx))
            continue
        start_poller(oid, bot)
        if info.get("msg_id") and now() - info.get("created", 0) < COUNTDOWN_SECONDS:
            start_timer(oid, bot)
    if pending_orders:
        log.info("recovery: watching %d pending order(s)", len(pending_orders))

# ═══════════════════════════════════════════════════════════════════════════════
# 👥 USERS / FORCE JOIN / ICON SETTER / BROADCAST  (carried over from v5, translated to English)
# ═══════════════════════════════════════════════════════════════════════════════
def _data_save():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf8") as fh:
            json.dump({"users": {str(k): v for k, v in users.items()},
                       "super_admins": {str(k): v for k, v in super_admins.items()},
                       "icons": dict(_ICON_OVERRIDES),
                       "bc_chats": {str(k): v for k, v in bc_chats.items()}},
                      fh, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)      # atomic: a crash in the middle keeps the old data safe
    except Exception:
        log.exception("data save failed")


def _data_load():
    try:
        with open(DATA_FILE, encoding="utf8") as fh:
            d = json.load(fh)
        users.update({int(k): v for k, v in d.get("users", {}).items()})
        super_admins.update({int(k): v for k, v in d.get("super_admins", {}).items()})
        _ICON_OVERRIDES.update({k: v for k, v in d.get("icons", {}).items() if k in ADMIN_ICON_SPECS})
        bc_chats.update({int(k): v for k, v in d.get("bc_chats", {}).items()})
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("data load failed")



def is_owner(uid: int) -> bool:
    return uid == ADMIN_ID


def _uname(rec: dict) -> str:
    return f"@{rec['username']}" if rec.get("username") else (rec.get("name") or "User")




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
    rec = users.setdefault(tid, {"name": "", "username": "", "banned": False})
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
        await update.message.reply_text("❌ Please send a numeric chat ID (e.g. 123456789).",
                                        reply_markup=InlineKeyboardMarkup(back_button("admin_users")))
        return
    tid, ban = int(raw), state == ADMIN_BAN_ID
    if tid == ADMIN_ID or tid in super_admins:
        await update.message.reply_text("❌ You cannot ban an admin.",
                                        reply_markup=InlineKeyboardMarkup(back_button("admin_users")))
        return
    rec = users.setdefault(tid, {"name": "", "username": "", "banned": False})
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
        "A super admin can manage listings, orders, users, promo codes and force join."
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
        "➕ <b>Add Super Admin</b>\n\nSend the <b>chat ID</b> of the person you want to promote (numbers only).\n"
        "It is better if they have started the bot with /start first.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("sa_menu")),
    )


async def sa_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or user_state.get(uid) != ADMIN_SA_ADD:
        return
    raw = (update.message.text or "").strip()
    if not re.fullmatch(r"\d{5,}", raw):
        await update.message.reply_text("❌ Please send a numeric chat ID.",
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
        await context.bot.send_message(tid, "👑 You have been made a Super Admin of this bot. Press /start.")
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Super Admin add: <code>{tid}</code>",
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
    try:
        with open(FJ_FILE, encoding="utf8") as f:
            data = json.load(f)
        force_join[:] = [e for e in data if isinstance(e, dict) and "id" in e and "link" in e]
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("force-join load failed")


def _fj_save():
    try:
        with open(FJ_FILE, "w", encoding="utf8") as f:
            json.dump(force_join, f, ensure_ascii=False, indent=1)
    except Exception:
        log.exception("force-join save failed")


def _clean_md(t: str) -> str:
    return re.sub(r"[*_`\[\]]", " ", str(t)).strip()


async def _fj_resolve(bot, raw: str) -> dict:
    """@username / t.me link / -100 ID -> {"id","title","link"}. Raises ValueError when invalid."""
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
        raise ValueError(f"{raw} — the bot is not an admin there. Make it an admin first, then send again.")
    link = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except Exception:
            raise ValueError(f"{raw} — could not create an invite link. Give the bot the 'Invite users' permission.")
    return {"id": chat.id, "title": chat.title or str(chat.id), "link": link}


async def fj_missing(bot, uid: int) -> list:
    """Force-join chats the user has not joined yet."""
    missing = []
    for e in force_join:
        try:
            mem = await bot.get_chat_member(e["id"], uid)
            if mem.status in ("left", "kicked") or (
                mem.status == "restricted" and not getattr(mem, "is_member", True)
            ):
                missing.append(e)
        except Exception as exc:  # bot removed / no permission -> do not block the user
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
        "📢 *Please join first*\n\n"
        "To use this bot, join the channel(s)/group(s) below, "
        "then tap *Joined — Check*."
    )
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            await context.bot.send_message(update.effective_chat.id, text,
                                           parse_mode="Markdown", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
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
    lines = "\n".join(f"{i}. {_clean_md(e['title'])}" for i, e in enumerate(force_join, 1)) or "_None_"
    text = (
        "📢 *Force Join*\n\n"
        f"Total: *{len(force_join)}*\n\n{lines}\n\n"
        "Tap 🗑️ to remove an entry."
    )
    rows = [[_inline_btn(f"🗑️ {_clean_md(e['title'])}", _sc=False, _style="danger",
                         callback_data=f"fjdel_{e['id']}")] for e in force_join]
    rows.append([_inline_btn("➕ Add Channel/Group", _sc=False, callback_data="fj_add")])
    rows.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")])
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def fj_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = update.effective_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = ADMIN_FJ_ADD
    await q.edit_message_text(
        "➕ *Add Channel/Group*\n\n"
        "First make the bot an *admin* in that channel/group.\n\n"
        "Then send one or more (separated by space/new line):\n"
        "• `@username`\n• `https://t.me/username`\n• `-1001234567890` (ID for private chats)",
        parse_mode="Markdown",
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
            failed.append(f"{_clean_md(e['title'])} — is already added")
            continue
        force_join.append(e)
        added.append(_clean_md(e["title"]))
    _fj_save()
    text = ""
    if added:
        text += "✅ *Added:*\n" + "\n".join(f"• {t}" for t in added) + "\n\n"
    if failed:
        text += "❌ *Failed:*\n" + "\n".join(f"• {_clean_md(t)}" for t in failed) + "\n\n"
    if added:
        user_state.pop(uid, None)
    else:
        text += "Send again or tap Back."
    await update.message.reply_text(
        text.strip() or "Nothing found — send an @username or a link.",
        parse_mode="Markdown",
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


# ─── ADMIN: ICON SETTER (/seticon) ────────────────────────────────────────────
async def _icon_set(bot, key: str, eid: str) -> str:
    """Verifies a custom emoji ID with Telegram and saves it. Returns an HTML reply."""
    try:
        stk = await bot.get_custom_emoji_stickers([eid])
    except Exception as e:
        return f"❌ Telegram could not verify it: <code>{html.escape(repr(e)[:150])}</code>"
    if not stk:
        return "❌ This custom emoji ID is not valid."
    st   = stk[0]
    anim = bool(getattr(st, "is_animated", False) or getattr(st, "is_video", False))
    _STICKER_META[eid] = ((st.emoji or "").replace("️", ""), anim)
    _INVALID_IDS.discard(eid)
    _VALID_IDS.add(eid)
    _ICON_OVERRIDES[key] = eid
    _data_save()
    _resolve_admin_icons()
    return (f"✅ <b>{html.escape(key)}</b> icon set: <code>{html.escape(eid)}</code> "
            f"({'animated' if anim else 'static — not animated'})\n\n"
            "Open the Admin Panel again.")


async def cmd_seticon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: /seticon [key] [emoji_id|reset] — set the premium icon of an admin button."""
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    args = context.args or []
    keys_txt = ", ".join(f"<code>{k}</code>" for k in ICON_KEYS)
    if not args:
        lines = []
        for k in ICON_KEYS:
            eid = _ADMIN_ICONS.get(k)
            tag = "animated" if _icon_is_animated(eid) else ("custom" if eid else "default")
            lines.append(f"• <code>{k}</code> — {tag}" + (f" <code>{eid}</code>" if eid else ""))
        await update.message.reply_text(
            "🎨 <b>Admin Button Icons</b>\n\n" + "\n".join(lines) + "\n\n"
            "<code>/seticon delete</code> — then send any premium emoji\n"
            "<code>/seticon delete 5188191464160523568</code> — direct ID\n"
            "<code>/seticon delete reset</code> — back to auto-pick\n\n"
            f"Keys: {keys_txt}",
            parse_mode="HTML",
        )
        return
    key = args[0].lower()
    if key not in ADMIN_ICON_SPECS:
        await update.message.reply_text(f"❌ Invalid key. Keys: {keys_txt}", parse_mode="HTML")
        return
    if len(args) >= 2:
        val = args[1].strip()
        if val.lower() == "reset":
            _ICON_OVERRIDES.pop(key, None)
            _data_save()
            _resolve_admin_icons()
            await update.message.reply_text(f"♻️ <b>{key}</b> icon reset (auto-pick).", parse_mode="HTML")
            return
        if not re.fullmatch(r"\d{10,}", val):
            await update.message.reply_text("❌ An emoji ID is numeric only.")
            return
        await update.message.reply_text(await _icon_set(context.bot, key, val), parse_mode="HTML")
        return
    user_state[uid] = ADMIN_SET_ICON
    admin_temp[uid] = {"icon_key": key}
    await update.message.reply_text(
        f"🎨 <b>{key}</b> — now send a <b>premium emoji</b> (or its ID).",
        parse_mode="HTML",
    )


async def seticon_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or user_state.get(uid) != ADMIN_SET_ICON:
        return
    key = (admin_temp.get(uid) or {}).get("icon_key")
    eid = None
    for ent in (update.message.entities or []):
        if ent.type == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
            eid = ent.custom_emoji_id
            break
    if not eid:
        raw = (update.message.text or "").strip()
        if re.fullmatch(r"\d{10,}", raw):
            eid = raw
    if not key or not eid:
        await update.message.reply_text("❌ Please send a premium emoji (or its ID).")
        return
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    await update.message.reply_text(await _icon_set(context.bot, key, eid), parse_mode="HTML")


# ─── ADMIN: BROADCAST ─────────────────────────────────────────────────────────
# The Telegram Bot API cannot list the chats where the bot is an admin, so:
#   1) my_chat_member updates add a chat automatically the moment the bot becomes an admin there
#   2) if it is already an admin, use ➕ Add Chat (@username / link / -100 ID) or 📢 Import Force Join
#   3) 🔄 Refresh removes old/dead chats
_BC_GONE = ("inaccessible", "the bot is not an admin there")


def _bc_markup(bot):
    uname = getattr(bot, "username", None)
    if not uname:
        return None
    return InlineKeyboardMarkup([[ibt_url("🛒 Open Bot", f"https://t.me/{uname}")]])


def _ra_seconds(e) -> float:
    ra = getattr(e, "retry_after", 1)
    return float(ra.total_seconds() if hasattr(ra, "total_seconds") else ra)


def _bc_rec(chat) -> dict:
    return {
        "title": chat.title or str(chat.id),
        "type":  chat.type,
        "link":  f"https://t.me/{chat.username}" if getattr(chat, "username", None) else None,
    }


def _bc_ident(raw: str):
    raw = raw.strip()
    m = re.match(r"^(?:https?://)?t\.me/(?!\+|joinchat)([A-Za-z0-9_]{4,})/?$", raw)
    if m:
        return "@" + m.group(1)
    if raw.startswith("@") and len(raw) > 4:
        return raw
    if re.fullmatch(r"-?\d{6,}", raw):
        return int(raw)
    raise ValueError(f"{raw} — invalid format. Send @username, a t.me link or a -100… ID.")


async def _safe_edit(q, text: str, markup=None):
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except tge.BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _bc_check(bot, chat_id):
    """(ok, reason, chat). Checks the bot's current membership + posting permission."""
    chat = None
    me = None
    for _ in range(2):
        try:
            chat = await bot.get_chat(chat_id)
            me   = await bot.get_chat_member(chat_id, bot.id)
            break
        except tge.RetryAfter as e:
            await asyncio.sleep(_ra_seconds(e) + 1)
        except (tge.Forbidden, tge.BadRequest):
            return False, "inaccessible", chat
        except tge.TelegramError as e:
            return False, f"check error: {str(e)[:80]}", chat
    else:
        return False, "rate limit — try again later", chat
    if chat.type not in ("group", "supergroup", "channel"):
        return False, "not a group/channel", chat
    if me.status == "creator":
        return True, "", chat
    if me.status != "administrator":
        return False, "the bot is not an admin there", chat
    if chat.type == "channel" and not getattr(me, "can_post_messages", False):
        return False, "no permission to post", chat
    return True, "", chat


async def _bc_send_one(bot, chat_id, text: str, markup):
    """Returns (result, info): ok | failed | unknown | migrated.
    Retries ONLY on RetryAfter (the request was rejected, so no duplicate). No retry on timeout/network error — the message may already have arrived."""
    for _ in range(3):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
            return "ok", ""
        except tge.RetryAfter as e:
            await asyncio.sleep(_ra_seconds(e) + 1)
        except tge.ChatMigrated as e:
            return "migrated", str(e.new_chat_id)
        except tge.Forbidden as e:
            return "failed", f"blocked / no permission: {str(e)[:80]}"
        except tge.BadRequest as e:
            return "failed", str(e)[:100]
        except tge.TimedOut:
            return "unknown", "timeout — delivery not confirmed; not retried to avoid duplicates"
        except tge.NetworkError as e:
            return "unknown", f"network error — not retried ({str(e)[:60]})"
        except tge.TelegramError as e:
            return "failed", str(e)[:100]
    return "failed", "flood-wait: retries exhausted"


def _bc_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_inline_btn("🚀 Send Broadcast", _sc=False, _style="success", callback_data="bc_send")],
        [_inline_btn("📋 Preview Message", _sc=False, _style="primary", callback_data="bc_preview")],
        [_inline_btn("➕ Add Chat", _sc=False, _style="primary", callback_data="bc_add"),
         _inline_btn("🔄 Refresh List", _sc=False, _style="primary", callback_data="bc_refresh")],
        [_inline_btn("📢 Import Force Join", _sc=False, _style="primary", callback_data="bc_import")],
        [_inline_btn("🔁 Auto Promotion", _sc=False, _style="primary", callback_data="ap_menu")],
        [_inline_btn("🗑️ Remove Chat", _sc=False, _style="danger", callback_data="bc_rmlist")],
        [_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")],
    ])


async def _render_bc(q, note: str = ""):
    items = list(bc_chats.items())
    lines = [f"• {html.escape(r.get('title') or str(c))[:40]} <i>({html.escape(str(r.get('type', '?')))})</i>"
             for c, r in items[:25]]
    if len(items) > 25:
        lines.append(f"…and {len(items) - 25} more")
    last = ""
    if _bc_last:
        last = (f"\n\n🕒 Last broadcast: ✅ {_bc_last['ok']} | ⛔ {_bc_last['skipped']} | "
                f"❌ {_bc_last['failed']} | ❓ {_bc_last['unknown']}")
    text = (
        "📣 <b>Broadcast</b>\n\n"
        + (note + "\n\n" if note else "")
        + f"Registered chats: <b>{len(items)}</b>\n\n"
        + ("\n".join(lines) if lines else "<i>No chats yet.</i>")
        + last
        + "\n\n<i>Any channel/group where you make the bot an admin is added automatically. "
          "If it is already an admin, use ➕ Add Chat or 📢 Import Force Join.</i>"
    )
    await _safe_edit(q, text, _bc_menu_markup())


async def track_bot_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot kisi group/channel me admin bane ya hate -> registry apne aap update."""
    cm = update.my_chat_member
    if not cm:
        return
    chat = cm.chat
    if chat.type not in ("group", "supergroup", "channel"):
        return
    st = cm.new_chat_member.status
    if st in ("administrator", "creator"):
        new = chat.id not in bc_chats
        rec = _bc_rec(chat)
        rec["link"] = rec["link"] or (bc_chats.get(chat.id) or {}).get("link")
        for _k in ("promo", "last_promo"):
            if _k in (bc_chats.get(chat.id) or {}):
                rec[_k] = bc_chats[chat.id][_k]
        bc_chats[chat.id] = rec
        _data_save()
        if new:
            await notify_admin(context.bot, f"📣 Added to the broadcast list: {rec['title']} ({chat.type})")
    elif bc_chats.pop(chat.id, None) is not None:
        _data_save()
        await notify_admin(context.bot, f"📣 Removed from the broadcast list: {chat.title or chat.id} (the bot is no longer an admin)")


async def admin_bc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state.pop(q.from_user.id, None)
    await _render_bc(q)


async def bc_preview_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer("Sending preview…")
    try:
        await context.bot.send_message(
            chat_id=q.from_user.id, text=_bc_text(), parse_mode="HTML",
            reply_markup=_bc_markup(context.bot),
        )
    except Exception as e:
        await notify_admin(context.bot, f"⚠️ Preview failed: {e!r}"[:900])


async def bc_send_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    if _bc_busy:
        await q.answer("A broadcast is already running.", show_alert=True)
        return
    await q.answer()
    if not bc_chats:
        await _render_bc(q, "❌ No chats are registered. Use ➕ Add Chat or Import first.")
        return
    lines = [f"• {html.escape(r.get('title') or str(c))[:40]}" for c, r in list(bc_chats.items())[:25]]
    if len(bc_chats) > 25:
        lines.append(f"…and {len(bc_chats) - 25} more")
    await _safe_edit(
        q,
        "🚀 <b>Broadcast Confirm</b>\n\n"
        f"This promo message will be sent to <b>{len(bc_chats)}</b> chat(s):\n\n" + "\n".join(lines) + "\n\n"
        "<i>The bot's admin/post permission is checked in every chat before sending — "
        "chats without permission are skipped.</i>",
        InlineKeyboardMarkup([
            [_inline_btn("✅ Confirm Send", _sc=False, _style="success", callback_data="bc_go")],
            [_inline_btn("❌ Cancel", _sc=False, _style="danger", callback_data="admin_bc")],
        ]),
    )


async def bc_go_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _bc_busy
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    if _bc_busy:
        await q.answer("A broadcast is already running.", show_alert=True)
        return
    if not bc_chats:
        await q.answer("No chats.", show_alert=True)
        return
    _bc_busy = True            # set before any await — a double click cannot start two broadcasts
    await q.answer("🚀 Broadcast started")
    try:
        await _safe_edit(q, f"⏳ <b>Broadcast in progress…</b>\n\n0/{len(bc_chats)}", None)
    except Exception:
        pass
    task = asyncio.create_task(_bc_run(context.bot, q.message.chat_id, q.message.message_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _bc_run(bot, admin_chat: int, msg_id: int):
    """Background task: har chat check -> send -> report. Payment/order flow se bilkul alag."""
    global _bc_busy
    ok, skipped, gone, failed, unknown = [], [], [], [], []
    try:
        text   = _bc_text()
        markup = _bc_markup(bot)
        ids    = list(bc_chats)
        total  = len(ids)
        for n, cid in enumerate(ids, 1):
            rec   = bc_chats.get(cid) or {}
            title = rec.get("title") or str(cid)
            allowed, why, chat = await _bc_check(bot, cid)
            if chat is not None and chat.title and rec and chat.title != rec.get("title"):
                rec["title"] = title = chat.title
            if not allowed:
                (gone if why == "inaccessible" else skipped).append((title, why))
            else:
                res, info = await _bc_send_one(bot, cid, text, markup)
                if res == "migrated":            # group became a supergroup — the message was not sent
                    try:
                        new_id = int(info)
                        bc_chats[new_id] = bc_chats.pop(cid, rec)
                        res, info = await _bc_send_one(bot, new_id, text, markup)
                    except ValueError:
                        res, info = "failed", "chat migrated — invalid new ID"
                    if res == "migrated":
                        res, info = "failed", "chat migrated again"
                if res == "ok":
                    ok.append(title)
                elif res == "unknown":
                    unknown.append((title, info))
                else:
                    failed.append((title, info))
            if n % 4 == 0 and n != total:
                try:
                    await bot.edit_message_text(
                        chat_id=admin_chat, message_id=msg_id, parse_mode="HTML",
                        text=f"⏳ <b>Broadcast in progress…</b>\n\n{n}/{total}",
                    )
                except Exception:
                    pass
            await asyncio.sleep(BC_DELAY)
        _data_save()
    except Exception as e:
        log.exception("broadcast loop crashed")
        failed.append(("broadcast loop", repr(e)[:100]))
    finally:
        _bc_busy = False

    _bc_last.clear()
    _bc_last.update(ok=len(ok), skipped=len(skipped), failed=len(failed) + len(gone), unknown=len(unknown))

    def fmt(items, limit=10):
        out = [f"• {html.escape(str(t))[:35]} — {html.escape(str(r))[:90]}" for t, r in items[:limit]]
        if len(items) > limit:
            out.append(f"…and {len(items) - limit} more")
        return "\n".join(out)

    parts = [
        "📣 <b>Broadcast Complete</b>\n",
        f"✅ Sent: <b>{len(ok)}</b>",
        f"⛔ Skipped (no admin/post permission): <b>{len(skipped)}</b>",
        f"🚫 Inaccessible (bot removed?): <b>{len(gone)}</b>",
        f"❌ Failed: <b>{len(failed)}</b>",
        f"❓ Unknown (timeout — not retried, to avoid duplicates): <b>{len(unknown)}</b>",
    ]
    for label, items in (("⛔ Skipped", skipped), ("🚫 Inaccessible", gone),
                         ("❌ Failed", failed), ("❓ Unknown", unknown)):
        if items:
            parts.append(f"\n<b>{label}:</b>\n{fmt(items)}")
    if gone:
        parts.append("\n<i>Tap 🔄 Refresh List to remove dead chats.</i>")
    report = "\n".join(parts)[:4000]
    markup = InlineKeyboardMarkup(back_button("admin_bc"))
    try:
        await bot.edit_message_text(chat_id=admin_chat, message_id=msg_id, text=report,
                                    parse_mode="HTML", reply_markup=markup)
    except Exception:
        try:
            await bot.send_message(chat_id=admin_chat, text=report, parse_mode="HTML", reply_markup=markup)
        except Exception:
            log.exception("broadcast report send failed")


async def bc_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = ADMIN_BC_ADD
    await _safe_edit(
        q,
        "➕ <b>Add Broadcast Chat</b>\n\n"
        "The bot must first be an <b>admin</b> there (with post permission in channels).\n\n"
        "Then send one or more (separated by space/new line):\n"
        "• <code>@username</code>\n• <code>https://t.me/username</code>\n"
        "• <code>-1001234567890</code> (ID for private chats)",
        InlineKeyboardMarkup(back_button("admin_bc")),
    )


async def bc_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_BC_ADD:
        return
    tokens = [t for t in re.split(r"[\s,]+", (update.message.text or "").strip()) if t]
    added, failed = [], []
    for tok in tokens:
        try:
            ident = _bc_ident(tok)
        except ValueError as e:
            failed.append(str(e))
            continue
        try:
            chat = await context.bot.get_chat(ident)
        except Exception:
            failed.append(f"{tok} — chat not found (is the bot an admin there?)")
            continue
        ok, why, chat2 = await _bc_check(context.bot, chat.id)
        title = chat.title or tok
        if not ok:
            failed.append(f"{title} — {why}")
            continue
        if chat.id in bc_chats:
            failed.append(f"{title} — is already added")
            continue
        bc_chats[chat.id] = _bc_rec(chat2 or chat)
        added.append(title)
    _data_save()
    text = ""
    if added:
        text += "✅ <b>Added:</b>\n" + "\n".join(f"• {html.escape(t)}" for t in added) + "\n\n"
    if failed:
        text += "❌ <b>Fail:</b>\n" + "\n".join(f"• {html.escape(t)}" for t in failed) + "\n\n"
    if added:
        user_state.pop(uid, None)
    else:
        text += "Send again or tap Back."
    await update.message.reply_text(
        text.strip() or "Nothing found — send an @username or a link.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_bc")),
    )


async def bc_import_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer("Importing…")
    added, skipped = 0, []
    for e in list(force_join):
        if e["id"] in bc_chats:
            continue
        ok, why, chat = await _bc_check(context.bot, e["id"])
        if ok:
            bc_chats[e["id"]] = _bc_rec(chat) if chat is not None else {
                "title": e.get("title") or str(e["id"]), "type": "?", "link": e.get("link")}
            added += 1
        else:
            skipped.append(f"{html.escape(_clean_md(e.get('title') or e['id']))[:30]} — {html.escape(why)}")
        await asyncio.sleep(0.1)
    _data_save()
    note = f"📢 <b>{added}</b> chat(s) added from Force Join."
    if skipped:
        note += "\n⛔ Skipped:\n" + "\n".join(f"• {s}" for s in skipped[:8])
    await _render_bc(q, note)


async def bc_refresh_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer("🔄 Refreshing…")
    good, removed, warn = 0, [], []
    for cid in list(bc_chats):
        ok, why, chat = await _bc_check(context.bot, cid)
        rec = bc_chats.get(cid) or {}
        title = rec.get("title") or str(cid)
        if chat is not None:
            bc_chats[cid] = {**rec, **_bc_rec(chat), "link": _bc_rec(chat)["link"] or rec.get("link")}
            title = bc_chats[cid]["title"]
        if ok:
            good += 1
        elif why in _BC_GONE:
            bc_chats.pop(cid, None)
            removed.append(f"{html.escape(title)[:30]} — {html.escape(why)}")
        else:
            warn.append(f"{html.escape(title)[:30]} — {html.escape(why)}")
        await asyncio.sleep(0.1)
    _data_save()
    note = f"🔄 Refresh done — ✅ OK: <b>{good}</b>"
    if removed:
        note += "\n🗑️ Removed:\n" + "\n".join(f"• {s}" for s in removed[:8])
    if warn:
        note += "\n⚠️ Please check:\n" + "\n".join(f"• {s}" for s in warn[:8])
    await _render_bc(q, note)


async def bc_rmlist_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    if not bc_chats:
        await _render_bc(q, "📭 No chats to remove.")
        return
    rows = []
    for cid, r in list(bc_chats.items())[:40]:
        short = re.sub(r"\s+", " ", r.get("title") or str(cid))[:24]
        rows.append([_inline_btn(f"🗑️ {short}", _sc=False, _style="danger", callback_data=f"bc_rm_{cid}")])
    rows.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_bc")])
    await _safe_edit(q, "🗑️ <b>Which chat should be removed from the broadcast list?</b>", InlineKeyboardMarkup(rows))


async def bc_rm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    try:
        cid = int(q.data[len("bc_rm_"):])
    except ValueError:
        await q.answer()
        return
    rec = bc_chats.pop(cid, None)
    _data_save()
    await q.answer("🗑️ Removed" if rec else "It was not in the list")
    await _render_bc(q, f"🗑️ Removed: {html.escape((rec or {}).get('title') or str(cid))}" if rec else "")



# ═══════════════════════════════════════════════════════════════════════════════
# ⚙️ ADMIN — panel, posts, orders, promo codes, wallet, inventory channel, auto promotion
# ═══════════════════════════════════════════════════════════════════════════════
async def _gate(q, owner: bool = False) -> bool:
    """True if the caller may proceed. Answers the callback with an error otherwise."""
    ok = is_owner(q.from_user.id) if owner else is_admin(q.from_user.id)
    if not ok:
        await q.answer("No access." if not owner else "Owner only.", show_alert=True)
    return ok


def _admin_markup_back(cb: str = "admin_panel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(back_button(cb))


async def _admin_show(update: Update, context, text: str, markup):
    """Edit the pressed message, or send a fresh one when triggered from the reply keyboard."""
    q = update.callback_query
    if q:
        try:
            await edit_q(q, text, markup)
            return
        except Exception as e:
            log.debug("admin edit failed, sending new: %r", e)
        await context.bot.send_message(q.message.chat_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        uid = update.effective_user.id
        await ui_clean(context.bot, update.effective_chat.id, uid)
        await send_temp(context.bot, update.effective_chat.id, uid, text, markup)


# ─── ADMIN PANEL ──────────────────────────────────────────────────────────────
def _pending_sell_count() -> int:
    return sum(1 for s in sells.values() if s.get("status") in ("submitted", "offered", "accepted"))


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Works from the inline callback and from the reply-keyboard text trigger."""
    q = update.callback_query
    uid = update.effective_user.id
    if not is_admin(uid):
        if q:
            await q.answer("No access.", show_alert=True)
        return
    if q:
        await q.answer()
    reset_flows(uid)
    n_res = len([p for p in posts if p in reserved])
    text = (
        "⚙️ <b>Admin Panel</b>\n\n"
        f"🟢 Available: <b>{len(posts) - n_res}</b>\n"
        f"🟡 Payment in progress: <b>{n_res}</b>\n"
        f"✅ Sold: <b>{len(sold)}</b>\n"
        f"💸 Open sell requests: <b>{_pending_sell_count()}</b>\n"
        f"🎟 Promo codes: <b>{len(promos)}</b>\n"
        f"📢 Force Join: <b>{len(force_join)}</b>\n"
        f"👥 Users: <b>{len(users)}</b>\n"
        f"📣 Broadcast chats: <b>{len(bc_chats)}</b>\n\n"
        "What would you like to do?"
    )
    rows = [
        [btn("➕ Add New Post", "admin_add")],
        [btn("📋 View All Posts", "admin_view")],
        [btn("✏️ Edit Post", "admin_edit_list")],
        [btn("🗑️ Delete Post", "admin_delete_list", icon=_adm_icon("delete"))],
        [btn("📦 Pending Orders", "admin_orders", icon=_adm_icon("orders"))],
        [btn("💸 Sell Requests", "admin_sells"), btn("🎟 Promo Codes", "admin_promos")],
        [btn("💰 Adjust Balance", "admin_wallet"), btn("📺 Inventory Channel", "admin_channel")],
        [btn("📢 Force Join", "admin_fj", icon=_adm_icon("forcejoin"))],
        [btn("👥 Manage Users", "admin_users", icon=_adm_icon("users"))],
        [btn("Broadcast", "admin_bc", icon=_adm_icon("broadcast"))],
        [btn("💾 Backup & Reports", "admin_backup")],
        [btn("🔙 Back", "main_menu")],
    ]
    await _admin_show(update, context, text, InlineKeyboardMarkup(rows))


# ─── PENDING ORDERS ───────────────────────────────────────────────────────────
async def admin_orders_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    await _render_orders(q)


async def _render_orders(q):
    if not pending_orders:
        await edit_q(q, "📭 There are no pending orders.", _admin_markup_back())
        return
    lines, rows = [], []
    for oid, info in list(pending_orders.items())[:15]:
        what = f"post <code>{esc(info.get('post_id'))}</code>" if info.get("kind", "purchase") == "purchase" else "top-up"
        age = int(now() - info.get("created", now()))
        lines.append(f"• <code>{esc(oid)}</code>\n  {what} | {rupees(info.get('amount', 0))} | user <code>{info['user_id']}</code> | {age}s ago")
        rows.append([btn(f"🔍 Check {oid[-8:]}", f"ao_chk_{oid}", "primary")])
    text = "📦 <b>Pending Orders</b>\n\n" + "\n\n".join(lines)
    if len(pending_orders) > 15:
        text += f"\n\n…and {len(pending_orders) - 15} more"
    text += "\n\n<i>Tap Check to force-verify an order with the gateway.</i>"
    rows.append([btn("🔙 Back", "admin_panel")])
    await edit_q(q, text, InlineKeyboardMarkup(rows))


async def admin_check_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    oid = q.data[len("ao_chk_"):]
    if oid not in pending_orders:
        await q.answer("This order is no longer pending.", show_alert=True)
        return
    try:
        status, data = await fg_status(oid)
    except Exception as e:
        await q.answer(f"Gateway error: {e!r}"[:190], show_alert=True)
        return
    if status == "success":
        await deliver_order(oid, data, context)
        msg = "✅ Payment confirmed — order delivered."
    elif status == "expired":
        await expire_order(oid, context)
        msg = "⏰ Order expired."
    else:
        msg = "⏳ Still pending at the gateway."
    await q.answer(msg, show_alert=True)
    await _render_orders(q)


async def cmd_checkorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/checkorder <order_id> — force a payment check."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/checkorder &lt;order_id&gt;</code>\n\nActive orders:\n"
            + ("\n".join(f"<code>{esc(o)}</code>" for o in list(pending_orders)[:20]) or "<i>none</i>"),
            parse_mode="HTML")
        return
    oid = context.args[0].strip()
    if oid not in pending_orders:
        await update.message.reply_text(f"❌ Order <code>{esc(oid)}</code> is not in the pending list.\n"
                                        "It was already delivered, expired or cancelled.", parse_mode="HTML")
        return
    msg = await update.message.reply_text(f"🔍 Checking order <code>{esc(oid)}</code>…", parse_mode="HTML")
    try:
        status, data = await fg_status(oid)
    except Exception as e:
        await msg.edit_text(f"❌ FamGateway error: <code>{esc(repr(e))}</code>", parse_mode="HTML")
        return
    if status == "success":
        await msg.edit_text(f"✅ Payment confirmed! Delivering order <code>{esc(oid)}</code>…\n"
                            f"UTR: <code>{esc(data.get('utr', 'N/A'))}</code>", parse_mode="HTML")
        await deliver_order(oid, data, context)
    elif status == "expired":
        await msg.edit_text(f"⏰ Order <code>{esc(oid)}</code> expired.", parse_mode="HTML")
        await expire_order(oid, context)
    else:
        info = pending_orders.get(oid, {})
        await msg.edit_text(f"⏳ Order <code>{esc(oid)}</code> is still pending.\n"
                            f"Post: <code>{esc(info.get('post_id'))}</code> | User: <code>{info.get('user_id')}</code>\n\n"
                            "<i>Try again in a moment — the poller keeps checking automatically.</i>", parse_mode="HTML")


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not pending_orders:
        await update.message.reply_text("📭 There are no pending orders.")
        return
    lines = [f"<code>{esc(o)}</code> | post=<code>{esc(i.get('post_id'))}</code> | uid=<code>{i['user_id']}</code>"
             for o, i in list(pending_orders.items())[:30]]
    await update.message.reply_text("📦 <b>Pending Orders</b>\n\n" + "\n".join(lines), parse_mode="HTML")


# ─── INVENTORY CHANNEL ────────────────────────────────────────────────────────
def channel_caption(pid: str, p: dict) -> str:
    lines = [f"🎮 <b>Free Fire ID</b>", "", f"📌 Post ID: <code>{esc(pid)}</code>",
             f"💰 Price: <b>{rupees(p['price'])}</b>"]
    if p.get("info"):
        lines.append(f"📝 {esc(p['info'])}")
    lines += ["", "⚡ Tap <b>Buy</b> to purchase instantly inside the bot."]
    return "\n".join(lines)


def _channel_id():
    ch = settings.get("channel")
    return ch.get("id") if ch else None


async def channel_post(bot, pid: str):
    cid = _channel_id()
    p = posts.get(pid)
    if not cid or not p:
        return
    uname = getattr(bot, "username", None)
    markup = kb([ubtn("🛒 Buy this ID", f"https://t.me/{uname}?start=buy_{pid}", "success")]) if uname else None
    cap = channel_caption(pid, p)
    try:
        imgs = p.get("images") or []
        if imgs:
            m = await bot.send_photo(chat_id=cid, photo=imgs[0], caption=cap, parse_mode="HTML", reply_markup=markup)
            p["channel_kind"] = "photo"
        else:
            m = await bot.send_message(chat_id=cid, text=cap, parse_mode="HTML", reply_markup=markup)
            p["channel_kind"] = "text"
        p["channel_msg"] = m.message_id
        _db_save()
    except Exception as e:
        log.warning("channel post failed for %s: %r", pid, e)
        await notify_admin(bot, f"⚠️ Could not post {pid} to the inventory channel: {e!r}"[:400])


async def channel_refresh(bot, pid: str):
    cid, p = _channel_id(), posts.get(pid)
    if not cid or not p or not p.get("channel_msg"):
        return
    uname = getattr(bot, "username", None)
    markup = kb([ubtn("🛒 Buy this ID", f"https://t.me/{uname}?start=buy_{pid}", "success")]) if uname else None
    try:
        if p.get("channel_kind") == "photo":
            await bot.edit_message_caption(chat_id=cid, message_id=p["channel_msg"], caption=channel_caption(pid, p),
                                           parse_mode="HTML", reply_markup=markup)
        else:
            await bot.edit_message_text(chat_id=cid, message_id=p["channel_msg"], text=channel_caption(pid, p),
                                        parse_mode="HTML", reply_markup=markup)
    except tge.BadRequest as e:
        if not _not_modified(e):
            log.warning("channel refresh failed: %r", e)
    except Exception as e:
        log.warning("channel refresh failed: %r", e)


async def channel_remove(bot, p: dict):
    cid = _channel_id()
    if cid and p.get("channel_msg"):
        await safe_delete(bot, cid, p["channel_msg"])


async def channel_mark_sold(bot, pid: str):
    cid, meta = _channel_id(), sold_meta.get(pid) or {}
    if not cid or not meta.get("channel_msg"):
        return
    text = (f"🎮 <b>Free Fire ID</b>\n\n📌 Post ID: <code>{esc(pid)}</code>\n\n"
            "❌ <b>SOLD</b>")
    try:
        if meta.get("channel_kind") == "photo":
            await bot.edit_message_caption(chat_id=cid, message_id=meta["channel_msg"], caption=text,
                                           parse_mode="HTML", reply_markup=None)
        else:
            await bot.edit_message_text(chat_id=cid, message_id=meta["channel_msg"], text=text,
                                        parse_mode="HTML", reply_markup=None)
    except Exception as e:
        log.warning("channel sold-mark failed: %r", e)


async def admin_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    await _render_channel(q)


async def _render_channel(q):
    user_state.pop(q.from_user.id, None)
    ch = settings.get("channel")
    text = ("📺 <b>Inventory Channel</b>\n\n"
            + (f"Current: <b>{esc(ch['title'])}</b>\n\nEvery new listing is posted there automatically with a Buy button, "
               "and is marked SOLD after purchase." if ch else
               "No channel is set.\n\nMake the bot an admin of your channel (with post permission), then tap Set Channel.")
            + "\n\n<i>Note: Telegram bots cannot read a channel's old messages, so only listings added through the bot are tracked.</i>")
    rows = [[btn("✏️ Set Channel", "ch_set", "primary")]]
    if ch:
        rows.append([btn("📤 Post unposted listings", "ch_postall", "success")])
        rows.append([btn("🗑️ Remove Channel", "ch_rm", "danger")])
    rows.append([btn("🔙 Back", "admin_panel")])
    await edit_q(q, text, InlineKeyboardMarkup(rows))


async def ch_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    user_state[q.from_user.id] = ADMIN_CHANNEL_SET
    await edit_q(q, "📺 <b>Set Inventory Channel</b>\n\nSend the channel as <code>@username</code>, a t.me link or "
                    "<code>-100…</code> ID. The bot must be an admin with permission to post.",
                 _admin_markup_back("admin_channel"))


async def ch_set_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_CHANNEL_SET:
        return
    try:
        ident = _bc_ident(update.message.text or "")
    except ValueError as e:
        await update.message.reply_text(f"❌ {esc(e)}", parse_mode="HTML", reply_markup=_admin_markup_back("admin_channel"))
        return
    ok, why, chat = await _bc_check(context.bot, ident)
    if not ok or chat is None or chat.type != "channel":
        await update.message.reply_text(f"❌ Could not use this chat: {esc(why or 'it is not a channel')}.",
                                        parse_mode="HTML", reply_markup=_admin_markup_back("admin_channel"))
        return
    settings["channel"] = {"id": chat.id, "title": chat.title or str(chat.id),
                           "link": f"https://t.me/{chat.username}" if chat.username else None}
    _db_save()
    user_state.pop(uid, None)
    await update.message.reply_text(f"✅ Inventory channel set: <b>{esc(chat.title)}</b>", parse_mode="HTML",
                                    reply_markup=_admin_markup_back("admin_channel"))


async def ch_rm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    settings["channel"] = None
    _db_save()
    await q.answer("Removed")
    await _render_channel(q)


async def ch_postall_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer("Posting…")
    n = 0
    for pid, p in list(posts.items()):
        if not p.get("channel_msg") and pid not in reserved:
            await channel_post(context.bot, pid)
            n += 1
            await asyncio.sleep(BC_DELAY)
    await context.bot.send_message(q.message.chat_id, f"📤 Posted {n} listing(s) to the inventory channel.")


# ─── ADD POST ─────────────────────────────────────────────────────────────────
async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    uid = q.from_user.id
    reset_flows(uid)
    user_state[uid] = ADMIN_WAIT_POST_ID
    admin_temp[uid] = {"mode": "add"}
    await edit_q(q, "📝 <b>Post ID</b>\n\nSend a Post ID (letters/numbers, e.g. <code>FF001</code>) "
                    "or tap <b>Auto ID</b> to let the bot create one.",
                 InlineKeyboardMarkup([[btn("🤖 Auto ID", "adm_autoid", "success")], *back_button("admin_panel")]))


async def _ask_price(bot, chat_id: int, pid: str, uid: int):
    admin_temp[uid]["post_id"] = pid
    user_state[uid] = ADMIN_WAIT_PRICE
    await bot.send_message(chat_id, f"✅ Post ID: <code>{esc(pid)}</code>\n\n💰 <b>Enter the price in ₹</b> (number only, e.g. <code>499</code>).",
                           parse_mode="HTML", reply_markup=_admin_markup_back())


async def admin_autoid_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q) or user_state.get(uid) != ADMIN_WAIT_POST_ID:
        await q.answer()
        return
    await q.answer()
    await _ask_price(context.bot, q.message.chat_id, new_post_id(), uid)


async def admin_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_POST_ID:
        return
    pid = (update.message.text or "").strip()
    if not POST_ID_RE.match(pid):
        await update.message.reply_text("❌ Use 2–24 letters, numbers or dashes only.", reply_markup=_admin_markup_back())
        return
    if _pid_in_use(pid):
        await update.message.reply_text("⚠️ This Post ID is already used (listed, sold or part of an order). "
                                        "Send another one or tap Auto ID.", reply_markup=_admin_markup_back())
        return
    await _ask_price(context.bot, update.effective_chat.id, pid, uid)


async def admin_add_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_PRICE:
        return
    try:
        price = _r2(float((update.message.text or "").strip().replace("₹", "")))
        if price < MIN_FINAL_PRICE:
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"❌ Send a number of {rupees(MIN_FINAL_PRICE)} or more, e.g. <code>499</code>.", parse_mode="HTML")
        return
    admin_temp[uid]["price"] = price
    user_state[uid] = ADMIN_WAIT_CREDS
    await update.message.reply_text(
        f"✅ Price: <b>{rupees(price)}</b>\n\n🔐 <b>Paste the login credentials</b>\n"
        "<i>Any format, e.g. Email: abc@gmail.com | Pass: xyz123 — they are sent to the buyer after payment.</i>",
        parse_mode="HTML", reply_markup=_admin_markup_back())


def _photo_prompt(n: int) -> str:
    return (f"📷 <b>Photos</b> — {n}/{ADMIN_MAX_IMAGES} received\n\n"
            f"Send up to {ADMIN_MAX_IMAGES} photos of the ID (optional), then tap <b>Done</b>.")


def _photo_markup(skip: bool):
    return kb([btn("✅ Done" if not skip else "⏭ Skip photos", "adp_done", "success")], [btn("❌ Cancel", "admin_panel", "danger")])


async def admin_add_photos_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_CREDS:
        return
    creds = (update.message.text or "").strip()
    if not creds:
        await update.message.reply_text("❌ Please send the credentials as text.")
        return
    admin_temp[uid]["creds"] = creds
    admin_temp[uid]["images"] = []
    user_state[uid] = ADMIN_WAIT_PHOTOS
    await safe_delete(context.bot, update.effective_chat.id, update.message.message_id)   # keep credentials off the screen
    m = await context.bot.send_message(update.effective_chat.id, _photo_prompt(0), parse_mode="HTML",
                                       reply_markup=_photo_markup(True))
    admin_temp[uid]["pmsg"] = m.message_id


async def admin_photo_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photos for 'add post' / 'edit post'. Albums arrive as separate updates, handled one by one."""
    uid = update.effective_user.id
    st = user_state.get(uid)
    if not is_admin(uid) or st not in (ADMIN_WAIT_PHOTOS, ADMIN_EDIT_PHOTOS):
        return False
    tmp = admin_temp.setdefault(uid, {})
    imgs = tmp.setdefault("images", [])
    fid = update.message.photo[-1].file_id
    if len(imgs) >= ADMIN_MAX_IMAGES:
        await update.message.reply_text(f"⚠️ Maximum {ADMIN_MAX_IMAGES} photos. Tap Done to continue.")
        return True
    if fid not in imgs:
        imgs.append(fid)
    try:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=tmp.get("pmsg"),
                                            text=_photo_prompt(len(imgs)), parse_mode="HTML",
                                            reply_markup=_photo_markup(False))
    except Exception:
        pass
    return True


async def admin_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q):
        return
    st = user_state.get(uid)
    tmp = admin_temp.get(uid, {})
    if st == ADMIN_WAIT_PHOTOS:
        await q.answer()
        user_state[uid] = ADMIN_WAIT_INFO
        await edit_q(q, "📝 <b>Description (optional)</b>\n\nSend a short description (e.g. level, rank, items) "
                        "or tap Skip.", kb([btn("⏭ Skip", "adi_skip", "primary")], [btn("❌ Cancel", "admin_panel", "danger")]))
    elif st == ADMIN_EDIT_PHOTOS:
        pid = tmp.get("edit_post_id")
        if pid not in posts:
            await q.answer("Post not found.", show_alert=True)
            return
        await q.answer("Photos updated")
        posts[pid]["images"] = list(tmp.get("images", []))[:ADMIN_MAX_IMAGES]
        user_state.pop(uid, None)
        admin_temp.pop(uid, None)
        _db_save()
        await edit_q(q, f"✅ Photos of <code>{esc(pid)}</code> updated ({len(posts[pid]['images'])}).",
                     kb([btn("✏️ Edit Another", "admin_edit_list")], [btn("⚙️ Admin Panel", "admin_panel")]))
    else:
        await q.answer()


async def _admin_finish_add(bot, chat_id: int, uid: int, info_text: str):
    tmp = admin_temp.get(uid, {})
    pid, price, creds = tmp.get("post_id"), tmp.get("price"), tmp.get("creds")
    if not pid or not price or not creds or _pid_in_use(pid):
        await bot.send_message(chat_id, "❌ Something went wrong or the Post ID was taken — please start again.",
                               reply_markup=_admin_markup_back())
        reset_flows(uid)
        return
    posts[pid] = {"price": price, "creds": creds, "images": list(tmp.get("images", []))[:ADMIN_MAX_IMAGES],
                  "info": info_text[:200], "created": now(), "source": "admin",
                  "channel_msg": None, "channel_kind": None}
    reset_flows(uid)
    _db_save()
    await bot.send_message(
        chat_id,
        f"✅ <b>Post added!</b>\n\n📌 Post ID: <code>{esc(pid)}</code>\n💰 Price: {rupees(price)}\n"
        f"🖼 Photos: {len(posts[pid]['images'])}\n🔐 Credentials: <i>saved</i>"
        + ("\n📺 Posting to the inventory channel…" if _channel_id() else ""),
        parse_mode="HTML",
        reply_markup=kb([btn("➕ Add Another", "admin_add")], [btn("📋 View All Posts", "admin_view")],
                        [btn("⚙️ Admin Panel", "admin_panel")]))
    await channel_post(bot, pid)


async def admin_info_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_INFO:
        return
    await _admin_finish_add(context.bot, update.effective_chat.id, uid, (update.message.text or "").strip())


async def admin_info_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    if user_state.get(q.from_user.id) == ADMIN_WAIT_INFO:
        await _admin_finish_add(context.bot, q.message.chat_id, q.from_user.id, "")


# ─── VIEW / DELETE / EDIT ─────────────────────────────────────────────────────
async def admin_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await _gate(q):
        return
    if not posts and not sold:
        await edit_q(q, "📭 <b>There are no posts yet.</b>\n\nAdd one first!", _admin_markup_back())
        return
    lines = []
    for pid, info in posts.items():
        tag = "🟡" if pid in reserved else "🟢"
        lines.append(f"{tag} <code>{esc(pid)}</code> — {rupees(info['price'])} · 🖼 {len(info.get('images') or [])}")
    for pid in sorted(sold)[-20:]:
        lines.append(f"✅ <code>{esc(pid)}</code> — SOLD")
    text = "📋 <b>All Posts</b>\n\n" + "\n".join(lines[:60])
    if len(lines) > 60:
        text += f"\n… and {len(lines) - 60} more"
    text += "\n\n🟢 available · 🟡 payment in progress · ✅ sold"
    await edit_q(q, text, _admin_markup_back())


async def admin_delete_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await _gate(q):
        return
    if not posts:
        await edit_q(q, "📭 There are no posts.", _admin_markup_back())
        return
    rows = [[btn(f"{'🟡' if pid in reserved else '🗑️'} {pid} — {rupees(info['price'])}", f"del_{pid}", "danger")]
            for pid, info in list(posts.items())[:50]]
    rows.append([btn("🔙 Back", "admin_panel")])
    await edit_q(q, "🗑️ <b>Which post do you want to delete?</b>", InlineKeyboardMarkup(rows))


async def admin_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await _gate(q):
        return
    pid = q.data[len("del_"):]
    if pid in reserved:
        text = f"⚠️ A payment for <code>{esc(pid)}</code> is in progress — it cannot be deleted now."
    elif pid in posts:
        p = posts.pop(pid)
        _db_save()
        await channel_remove(context.bot, p)
        text = f"✅ Post <code>{esc(pid)}</code> deleted!"
    else:
        text = "❌ Post not found."
    await edit_q(q, text, kb([btn("🗑️ Delete Another", "admin_delete_list")], [btn("⚙️ Admin Panel", "admin_panel")]))


async def admin_edit_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await _gate(q):
        return
    if not posts:
        await edit_q(q, "📭 There are no posts.", _admin_markup_back())
        return
    rows = [[btn(f"{'🟡' if pid in reserved else '✏️'} {pid} — {rupees(info['price'])}", f"edit_{pid}", "primary")]
            for pid, info in list(posts.items())[:50]]
    rows.append([btn("🔙 Back", "admin_panel")])
    await edit_q(q, "✏️ <b>Which post do you want to edit?</b>", InlineKeyboardMarkup(rows))


async def admin_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q):
        return
    pid = q.data[len("edit_"):]
    if pid not in posts:
        await q.answer("Post not found!", show_alert=True)
        return
    if pid in reserved:
        await q.answer("A payment for this ID is in progress.", show_alert=True)
        return
    await q.answer()
    reset_flows(uid)
    admin_temp[uid] = {"edit_post_id": pid}
    p = posts[pid]
    await edit_q(q, f"✏️ <b>Edit post</b> <code>{esc(pid)}</code>\n\n💰 Price: {rupees(p['price'])}\n"
                    f"🖼 Photos: {len(p.get('images') or [])}\n📝 Description: {esc(p.get('info') or '—')}\n"
                    "🔐 Credentials: <i>stored</i>\n\nWhat do you want to change?",
                 kb([btn("💰 Price", "editfield_price")], [btn("🔐 Credentials", "editfield_creds")],
                    [btn("📝 Description", "editfield_info")], [btn("🖼 Photos", "editfield_photos")],
                    [btn("🔙 Back", "admin_edit_list")]))


async def admin_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q):
        return
    await q.answer()
    field = q.data[len("editfield_"):]
    tmp = admin_temp.setdefault(uid, {})
    if tmp.get("edit_post_id") not in posts:
        await edit_q(q, "❌ Post not found.", _admin_markup_back())
        return
    tmp["edit_field"] = field
    if field == "photos":
        tmp["images"] = []
        user_state[uid] = ADMIN_EDIT_PHOTOS
        tmp["pmsg"] = q.message.message_id
        await edit_q(q, _photo_prompt(0) + "\n\n<i>The new photos will replace all existing ones.</i>", _photo_markup(False))
        return
    user_state[uid] = ADMIN_EDIT_VALUE
    label = {"price": "price (number only)", "creds": "credentials", "info": "description"}[field]
    await edit_q(q, f"✏️ Send the new <b>{label}</b>:", _admin_markup_back("admin_edit_list"))


async def admin_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_EDIT_VALUE:
        return
    value = (update.message.text or "").strip()
    tmp = admin_temp.get(uid, {})
    pid, field = tmp.get("edit_post_id"), tmp.get("edit_field")
    if not pid or not field or pid not in posts:
        await update.message.reply_text("❌ Something went wrong — please try again.")
        reset_flows(uid)
        return
    if pid in reserved:
        await update.message.reply_text("⚠️ A payment for this ID started meanwhile — try again later.")
        reset_flows(uid)
        return
    if field == "price":
        try:
            new_price = _r2(float(value.replace("₹", "")))
            if new_price < MIN_FINAL_PRICE:
                raise ValueError
        except ValueError:
            await update.message.reply_text(f"❌ Send a number of {rupees(MIN_FINAL_PRICE)} or more.")
            return
        posts[pid]["price"] = new_price
    elif field == "creds":
        if not value:
            await update.message.reply_text("❌ Credentials cannot be empty.")
            return
        posts[pid]["creds"] = value
        await safe_delete(context.bot, update.effective_chat.id, update.message.message_id)
    else:
        posts[pid]["info"] = value[:200]
    reset_flows(uid)
    _db_save()
    await channel_refresh(context.bot, pid)
    await context.bot.send_message(update.effective_chat.id,
                                   f"✅ Post <code>{esc(pid)}</code> updated!\n\n💰 Price: {rupees(posts[pid]['price'])}",
                                   parse_mode="HTML",
                                   reply_markup=kb([btn("✏️ Edit Another", "admin_edit_list")], [btn("⚙️ Admin Panel", "admin_panel")]))


# ─── WALLET ADJUSTMENT ────────────────────────────────────────────────────────
async def admin_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    user_state[q.from_user.id] = ADMIN_BAL_ADJUST
    await edit_q(q, "💰 <b>Adjust Balance</b>\n\nSend: <code>USER_ID AMOUNT [note]</code>\n"
                    "• <code>123456789 50 bonus</code> → add ₹50\n"
                    "• <code>123456789 -20 correction</code> → deduct ₹20\n"
                    "• <code>123456789</code> → just show the balance\n\n"
                    "<i>Every adjustment is written to the ledger and the user is notified.</i>",
                 _admin_markup_back())


async def admin_wallet_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_BAL_ADJUST:
        return
    parts = (update.message.text or "").strip().split(None, 2)
    if not parts or not re.fullmatch(r"\d{5,}", parts[0]):
        await update.message.reply_text("❌ Format: <code>USER_ID AMOUNT [note]</code>", parse_mode="HTML")
        return
    tid = int(parts[0])
    if len(parts) == 1:
        await update.message.reply_text(f"💼 Balance of <code>{tid}</code>: <b>{rupees(user_balance(tid))}</b>", parse_mode="HTML",
                                        reply_markup=_admin_markup_back())
        return
    try:
        amt = _r2(float(parts[1]))
        if amt == 0 or abs(amt) > 100000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Amount must be a non-zero number (max ±100000).")
        return
    note = (parts[2] if len(parts) > 2 else "admin adjustment")[:60]
    counters["adj"] += 1
    ref = f"ADJ{counters['adj']:05d}"
    try:
        entry = ledger_add(tid, "adjust", amt, ref=ref, note=note)
    except ValueError:
        await update.message.reply_text(f"❌ That would make the balance negative (current {rupees(user_balance(tid))}).")
        return
    log_event(tid, "balance_adjust", f"{amt:+g}")
    user_state.pop(uid, None)
    await update.message.reply_text(
        f"✅ Done. <code>{tid}</code>: {amt:+g} → new balance <b>{rupees(entry['balance'])}</b>\nRef: <code>{ref}</code>",
        parse_mode="HTML", reply_markup=_admin_markup_back())
    try:
        await context.bot.send_message(
            tid, f"💰 <b>Wallet updated</b>\n\n{'Added' if amt > 0 else 'Deducted'}: <b>{rupees(abs(amt))}</b>\n"
                 f"New balance: <b>{rupees(entry['balance'])}</b>\nReason: {esc(note)}", parse_mode="HTML")
    except Exception:
        pass


# ─── PROMO CODES (admin) ──────────────────────────────────────────────────────
async def admin_promos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    await _render_promos(q)


async def _render_promos(q):
    promo_sweep()
    reset_flows(q.from_user.id)
    rows = []
    for code, rec in list(promos.items())[:40]:
        val = f"{rec['value']:g}%" if rec.get("type") == "percent" else rupees(rec["value"])
        rows.append([btn(f"{'🟢' if rec.get('enabled', True) else '⚪'} {code} — {val}", f"pr_v_{code}", "primary")])
    rows.append([btn("➕ Create Promo Code", "pr_new", "success")])
    rows.append([btn("🔙 Back", "admin_panel")])
    await edit_q(q, "🎟 <b>Promo Codes</b>\n\n" + (f"{len(promos)} code(s). Tap one to manage it." if promos else
                                               "No promo codes yet. Create one!"), InlineKeyboardMarkup(rows))


async def pr_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    code = q.data[len("pr_v_"):]
    if code not in promos:
        await q.answer("Not found.", show_alert=True)
        return
    await q.answer()
    await _render_promo(q, code)


async def _render_promo(q, code: str):
    rec = promos[code]
    rows = [[btn("⏸ Disable" if rec.get("enabled", True) else "▶️ Enable", f"pr_t_{code}", "primary")],
            [btn("⏳ Set expiry (days)", f"pr_e_exp_{code}"), btn("👥 Set max uses", f"pr_e_max_{code}")],
            [btn("🗑️ Delete", f"pr_d_{code}", "danger")], [btn("🔙 Back", "admin_promos")]]
    await edit_q(q, "🎟 <b>Promo code</b>\n\n" + promo_summary(rec), InlineKeyboardMarkup(rows))


async def pr_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    rec = promos.get(q.data[len("pr_t_"):])
    if not rec:
        await q.answer("Not found.", show_alert=True)
        return
    rec["enabled"] = not rec.get("enabled", True)
    _db_save()
    await q.answer("Enabled" if rec["enabled"] else "Disabled")
    await _render_promo(q, rec["code"])


async def pr_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    code = q.data[len("pr_d_"):]
    await edit_q(q, f"🗑️ Delete promo code <b>{esc(code)}</b>?\n\n<i>Past redemptions stay in the order history.</i>",
                 kb([btn("✅ Yes, delete", f"pr_dd_{code}", "danger")], [btn("❌ No", f"pr_v_{code}", "primary")]))


async def pr_delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    promos.pop(q.data[len("pr_dd_"):], None)
    _db_save()
    await q.answer("Deleted")
    await _render_promos(q)


async def pr_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q):
        return
    m = re.match(r"pr_e_(exp|max)_(.+)$", q.data)
    if not m or m.group(2) not in promos:
        await q.answer("Not found.", show_alert=True)
        return
    await q.answer()
    user_state[uid] = ADMIN_PROMO_EDIT
    admin_temp[uid] = {"pe_field": m.group(1), "pe_code": m.group(2)}
    ask = ("Send the number of <b>days from now</b> until expiry (0 = never expires)." if m.group(1) == "exp"
           else "Send the <b>maximum total uses</b> (0 = unlimited).")
    await edit_q(q, "🎟 " + ask, _admin_markup_back(f"pr_v_{m.group(2)}"))


async def pr_edit_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_PROMO_EDIT:
        return
    tmp = admin_temp.get(uid, {})
    rec = promos.get(tmp.get("pe_code"))
    raw = (update.message.text or "").strip()
    if not rec or not re.fullmatch(r"\d{1,5}", raw):
        await update.message.reply_text("❌ Send a whole number (0 or more).")
        return
    n = int(raw)
    if tmp["pe_field"] == "exp":
        rec["expires"] = (now() + n * 86400) if n else None
    else:
        rec["max_total"] = n or None
    _db_save()
    reset_flows(uid)
    await update.message.reply_text("✅ Updated.\n\n" + promo_summary(rec), parse_mode="HTML",
                                    reply_markup=kb([btn("🔙 Back", f"pr_v_{rec['code']}")]))


# promo creation wizard ------------------------------------------------------
_PW_STEPS = ["type", "value", "min_price", "max_price", "max_total", "per_user", "days", "code"]


def _pw_prompt(step: str) -> tuple[str, bool]:
    """(text, skippable)"""
    return {
        "value": ("🔢 Send the <b>discount value</b> (a % between 1–90, or a flat ₹ amount).", False),
        "min_price": ("💰 <b>Minimum ID price</b> this code works for (₹). Skip = no minimum.", True),
        "max_price": ("💰 <b>Maximum ID price</b> this code works for (₹). Skip = no maximum.", True),
        "max_total": ("👥 <b>Maximum total uses</b> of this code. Skip = unlimited.", True),
        "per_user": ("👤 <b>Uses allowed per user</b>. Skip = unlimited.", True),
        "days": ("⏳ <b>Valid for how many days</b> from now? Skip = never expires.", True),
        "code": ("🎟 Send the <b>code text</b> (3–20 letters/numbers) or tap Auto-generate.", True),
    }[step]


async def _pw_ask(bot, chat_id: int, uid: int):
    step = admin_temp[uid]["pw"]["step"]
    text, skippable = _pw_prompt(step)
    if step == "code":
        rows = [[btn("🤖 Auto-generate", "pw_auto", "success")]]
    else:
        rows = [[btn("⏭ Skip", "pw_skip", "primary")]] if skippable else []
    rows.append([btn("❌ Cancel", "admin_promos", "danger")])
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def pr_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q):
        return
    await q.answer()
    reset_flows(uid)
    user_state[uid] = ADMIN_PROMO_WIZ
    admin_temp[uid] = {"pw": {"step": "type", "d": {}}}
    await edit_q(q, "🎟 <b>New promo code</b>\n\nChoose the discount type:",
                 kb([btn("％ Percentage", "pw_t_percent", "success"), btn("₹ Flat amount", "pw_t_flat", "success")],
                    [btn("❌ Cancel", "admin_promos", "danger")]))


async def pw_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q) or user_state.get(uid) != ADMIN_PROMO_WIZ:
        await q.answer()
        return
    await q.answer()
    pw = admin_temp[uid]["pw"]
    pw["d"]["type"] = q.data[len("pw_t_"):]
    pw["step"] = "value"
    await _pw_ask(context.bot, q.message.chat_id, uid)


def _pw_next(pw: dict):
    pw["step"] = _PW_STEPS[_PW_STEPS.index(pw["step"]) + 1]


async def _pw_store(bot, chat_id: int, uid: int, raw):
    """Store the answer to the current step (raw=None means skip) and move on / finish."""
    pw = admin_temp[uid]["pw"]
    d, step = pw["d"], pw["step"]
    try:
        if step == "value":
            v = float(raw)
            if d["type"] == "percent" and not (1 <= v <= 90):
                raise ValueError("percentage must be 1–90")
            if d["type"] == "flat" and v < 1:
                raise ValueError("amount must be at least ₹1")
            d["value"] = _r2(v)
        elif step in ("min_price", "max_price"):
            d[step] = None if raw is None else _r2(float(raw))
            if raw is not None and d[step] < 0:
                raise ValueError("cannot be negative")
            if d.get("min_price") is not None and d.get("max_price") is not None and d["max_price"] < d["min_price"]:
                d.pop(step)
                raise ValueError("maximum cannot be lower than the minimum")
        elif step in ("max_total", "per_user"):
            d[step] = None if raw is None else int(raw)
            if raw is not None and d[step] < 1:
                raise ValueError("must be at least 1")
        elif step == "days":
            d["expires"] = None if raw is None else now() + int(raw) * 86400
            if raw is not None and int(raw) < 1:
                raise ValueError("must be at least 1 day")
        elif step == "code":
            code = promo_generate_code() if raw is None else promo_norm(raw)
            if not PROMO_CODE_RE.match(code):
                raise ValueError("use 3–20 letters/numbers only")
            if code in promos:
                raise ValueError("this code already exists")
            d["code"] = code
    except (ValueError, TypeError) as e:
        await bot.send_message(chat_id, f"❌ Invalid: {esc(e) if str(e) else 'please send a number'}. Try again.", parse_mode="HTML")
        return
    if step == "code":
        rec = {"code": d["code"], "type": d["type"], "value": d["value"], "min_price": d.get("min_price") or 0,
               "max_price": d.get("max_price"), "min_order": 0, "max_total": d.get("max_total"),
               "per_user": d.get("per_user"), "starts": None, "expires": d.get("expires"),
               "enabled": True, "redemptions": [], "created": now()}
        promos[rec["code"]] = rec
        _db_save()
        reset_flows(uid)
        await bot.send_message(chat_id, "✅ <b>Promo code created!</b>\n\n" + promo_summary(rec), parse_mode="HTML",
                               reply_markup=kb([btn("🎟 Promo Codes", "admin_promos")], [btn("⚙️ Admin Panel", "admin_panel")]))
        return
    _pw_next(pw)
    await _pw_ask(bot, chat_id, uid)


async def pw_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_PROMO_WIZ or "pw" not in admin_temp.get(uid, {}):
        return
    if admin_temp[uid]["pw"]["step"] == "type":
        await update.message.reply_text("Please choose the discount type using the buttons above.")
        return
    await _pw_store(context.bot, update.effective_chat.id, uid, (update.message.text or "").strip().replace("₹", ""))


async def pw_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q) or user_state.get(uid) != ADMIN_PROMO_WIZ:
        await q.answer()
        return
    await q.answer()
    await _pw_store(context.bot, q.message.chat_id, uid, None)


async def pw_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q) or user_state.get(uid) != ADMIN_PROMO_WIZ:
        await q.answer()
        return
    await q.answer()
    if admin_temp[uid]["pw"]["step"] == "code":
        await _pw_store(context.bot, q.message.chat_id, uid, None)


# ─── PROMOTION TEXT + AUTO PROMOTION ──────────────────────────────────────────
def _bc_text() -> str:
    """Promo message (HTML). Plain emojis only — the send patch turns them into premium emojis and puts
    the first line (title) inside the blue ➿ frame."""
    brand = esc(settings.get("brand") or BOT_NAME)
    orders = str(settings.get("orders_completed") or "").strip()
    loop = "➿" * 11
    lines = [
        f"<b>{brand}</b>", "", loop, "",
        "💎 <b>Free Fire ID Marketplace Bot</b>", "",
        "🎮 With this bot you can:", "",
        "🛒 Browse available Free Fire IDs with photos",
        "⚡ Pay by UPI — scan the QR and your ID is delivered automatically after verified payment",
        "💰 Keep a wallet balance and pay in one tap",
        "🎟 Use promo codes for discounts",
        "💸 Sell your own Free Fire ID to us",
        "🔐 Credentials delivered straight inside the bot",
    ]
    if orders and orders != "0":
        lines += ["", f"📦 <b>{esc(orders)} Orders Completed!</b>"]
    lines += ["", loop, "", "🚀 <b>Tap the button below to start the bot!</b>", "", f"✨ <b>{brand}</b>"]
    return "\n".join(lines)


def _promo_chats() -> list:
    return [cid for cid, r in bc_chats.items() if r.get("promo", True)]


async def _auto_promo_run(bot, force: bool = False) -> dict:
    """Send the promo to every opted-in chat, skipping chats promoted too recently. Duplicate-safe."""
    global _bc_busy
    ap = settings["auto_promo"]
    out = {"ok": 0, "skipped": 0, "failed": 0}
    if _bc_busy:
        return out
    _bc_busy = True
    try:
        text, markup = _bc_text(), _bc_markup(bot)
        min_gap = max(0.5, ap["interval_h"] - 0.25) * 3600
        for cid in _promo_chats():
            rec = bc_chats.get(cid) or {}
            if not force and now() - rec.get("last_promo", 0) < min_gap:
                out["skipped"] += 1
                continue
            allowed, why, chat = await _bc_check(bot, cid)
            if not allowed:
                out["skipped"] += 1
                continue
            res, info = await _bc_send_one(bot, cid, text, markup)
            if res == "migrated":
                try:
                    new_id = int(info)
                    bc_chats[new_id] = bc_chats.pop(cid, rec)
                    cid = new_id
                    res, info = await _bc_send_one(bot, cid, text, markup)
                except ValueError:
                    res = "failed"
            if res == "ok":
                out["ok"] += 1
                (bc_chats.get(cid) or rec)["last_promo"] = now()    # recorded immediately — no duplicate after a restart
                _data_save()
            elif res == "unknown":
                (bc_chats.get(cid) or rec)["last_promo"] = now()    # delivery unknown: do not risk a duplicate
                out["failed"] += 1
            else:
                out["failed"] += 1
            await asyncio.sleep(BC_DELAY)
    except Exception:
        log.exception("auto promo failed")
    finally:
        _bc_busy = False
        ap["last_ts"] = now()
        _db_save()
    return out


async def auto_promo_loop(bot):
    while True:
        try:
            await asyncio.sleep(60)
            ap = settings.get("auto_promo") or {}
            if ap.get("enabled") and now() - ap.get("last_ts", 0) >= ap.get("interval_h", 3) * 3600:
                r = await _auto_promo_run(bot)
                log.info("auto promotion: %s", r)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("auto promo loop error")


def _ap_text() -> str:
    ap = settings["auto_promo"]
    n = len(_promo_chats())
    nxt = ""
    if ap.get("enabled"):
        nxt = f"\n⏭ Next run: {ist_str(ap.get('last_ts', 0) + ap['interval_h'] * 3600)}" if ap.get("last_ts") else "\n⏭ Next run: within a minute"
    return ("🔁 <b>Auto Promotion</b>\n\n"
            f"Status: <b>{'ON ✅' if ap.get('enabled') else 'OFF ⛔'}</b>\n"
            f"Interval: every <b>{ap['interval_h']:g} hour(s)</b>\n"
            f"Chats opted in: <b>{n}</b> of {len(bc_chats)}\n"
            f"Last run: {ist_str(ap['last_ts']) if ap.get('last_ts') else 'never'}{nxt}\n\n"
            "<i>The promotion message is the same one used by Broadcast. A chat is never promoted twice within the interval.</i>")


def _ap_markup() -> InlineKeyboardMarkup:
    ap = settings["auto_promo"]
    return kb([btn("⏸ Turn OFF" if ap.get("enabled") else "▶️ Turn ON", "ap_toggle", "danger" if ap.get("enabled") else "success")],
              [btn("⏱ Set interval", "ap_interval", "primary"), btn("💬 Chats", "ap_chats", "primary")],
              [btn("🚀 Send now", "ap_now", "success")], [btn("🔙 Back", "admin_bc")])


async def ap_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    user_state.pop(q.from_user.id, None)
    await edit_q(q, _ap_text(), _ap_markup())


async def ap_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    ap = settings["auto_promo"]
    ap["enabled"] = not ap.get("enabled")
    if ap["enabled"]:
        ap["last_ts"] = 0.0
    _db_save()
    await q.answer("Auto promotion " + ("ON" if ap["enabled"] else "OFF"))
    await edit_q(q, _ap_text(), _ap_markup())


async def ap_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    user_state[q.from_user.id] = ADMIN_AP_INTERVAL
    await edit_q(q, "⏱ Send the interval in <b>hours</b> (1–168, e.g. <code>3</code>).", _admin_markup_back("ap_menu"))


async def ap_interval_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_AP_INTERVAL:
        return
    try:
        h = float((update.message.text or "").strip())
        if not (1 <= h <= 168):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send a number of hours between 1 and 168.")
        return
    settings["auto_promo"]["interval_h"] = h
    _db_save()
    user_state.pop(uid, None)
    await update.message.reply_text(_ap_text(), parse_mode="HTML", reply_markup=_ap_markup())


async def ap_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    await _render_ap_chats(q)


async def _render_ap_chats(q):
    rows = [[btn(f"{'✅' if r.get('promo', True) else '⛔'} {re.sub(chr(10), ' ', (r.get('title') or str(c)))[:26]}",
                 f"ap_c_{c}", "success" if r.get("promo", True) else "danger")]
            for c, r in list(bc_chats.items())[:40]]
    rows.append([btn("🔙 Back", "ap_menu")])
    await edit_q(q, "💬 <b>Auto-promotion chats</b>\n\nTap a chat to switch it on/off for auto promotion.\n"
                    "<i>Manual Broadcast always goes to every registered chat.</i>" if bc_chats else
                 "📭 No chats registered yet.", InlineKeyboardMarkup(rows))


async def ap_chat_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    try:
        cid = int(q.data[len("ap_c_"):])
    except ValueError:
        await q.answer()
        return
    rec = bc_chats.get(cid)
    if rec is None:
        await q.answer("Chat not found.", show_alert=True)
        return
    rec["promo"] = not rec.get("promo", True)
    _data_save()
    await q.answer("ON" if rec["promo"] else "OFF")
    await _render_ap_chats(q)


async def ap_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    if _bc_busy:
        await q.answer("A broadcast is already running.", show_alert=True)
        return
    await q.answer("Sending…")
    r = await _auto_promo_run(context.bot, force=True)
    await edit_q(q, f"🚀 <b>Done</b> — sent: {r['ok']} | skipped: {r['skipped']} | failed: {r['failed']}\n\n" + _ap_text(),
                 _ap_markup())

# ═══════════════════════════════════════════════════════════════════════════════
# 💸 SELL YOUR ID — user submits photos + login details, admin offers a price, user accepts, admin lists
# ═══════════════════════════════════════════════════════════════════════════════
SELL_PLATFORMS = {
    "google": "Google", "facebook": "Facebook", "twitter": "Twitter / X", "apple": "Apple ID",
    "vk": "VK", "huawei": "Huawei", "other": "Other",
}
SELL_OPEN = ("submitted", "offered", "accepted")
SELL_STATUS_LABEL = {
    "submitted": "⏳ Waiting for admin review", "offered": "💵 Price offered — your answer needed",
    "accepted": "✅ Accepted — admin is listing your ID", "listed": "📢 Listed for sale",
    "sold": "💰 Sold", "declined": "❌ You declined the offer", "rejected": "🚫 Rejected by admin",
}


def _sell_cancel_kb(extra=None):
    rows = [list(extra)] if extra else []
    rows.append([btn("❌ Cancel", "sl_cancel", "danger")])
    return InlineKeyboardMarkup(rows)


def _sell_photo_text(n: int) -> str:
    need = max(0, SELL_MIN_IMAGES - n)
    return ("📸 <b>Sell Your Free Fire ID — Step 1/4</b>\n\n"
            f"Send <b>{SELL_MIN_IMAGES} to {SELL_MAX_IMAGES} screenshots/photos</b> of your ID "
            "(profile, level, rank, characters, items, etc.).\n\n"
            f"📷 Received: <b>{n}/{SELL_MAX_IMAGES}</b>"
            + (f"\n⚠️ Send at least {need} more." if need else "\n✅ You can tap <b>Done</b> now or send more.")
            + "\n\n<i>You can send several photos at once.</i>")


def _sell_photo_markup(n: int):
    rows = []
    if n >= SELL_MIN_IMAGES:
        rows.append([btn("✅ Done — next step", "sl_done", "success")])
    rows.append([btn("❌ Cancel", "sl_cancel", "danger")])
    return InlineKeyboardMarkup(rows)


async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    bot = context.bot
    if q:
        await q.answer()
    if await fj_gate(update, context):
        return
    reset_flows(uid)
    await ui_clean(bot, chat_id, uid)
    open_n = sum(1 for s in sells.values() if s["uid"] == uid and s["status"] in SELL_OPEN)
    if open_n >= SELL_ACTIVE_LIMIT:
        await send_temp(bot, chat_id, uid,
                        f"⚠️ <b>You already have {open_n} open sell requests.</b>\n\n"
                        "Please wait until one of them is completed before sending another.",
                        kb([btn("📄 My Sell Requests", "sl_my", "primary")], [btn("🏠 Main Menu", "main_menu")]))
        return
    log_event(uid, "sell_start")
    sell_draft[uid] = {"photos": [], "platform": None, "login": None}
    user_state[uid] = SELL_PHOTOS
    m = await send_temp(bot, chat_id, uid, _sell_photo_text(0), _sell_photo_markup(0))
    sell_draft[uid]["pmsg"] = m.message_id


async def sell_photo_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != SELL_PHOTOS or uid not in sell_draft:
        return False
    d = sell_draft[uid]
    fid = update.message.photo[-1].file_id
    if len(d["photos"]) >= SELL_MAX_IMAGES:
        await send_temp(context.bot, update.effective_chat.id, uid,
                        f"⚠️ Maximum {SELL_MAX_IMAGES} photos reached. Tap <b>Done</b> to continue.")
        return True
    if fid not in d["photos"]:
        d["photos"].append(fid)
    try:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=d.get("pmsg"),
                                            text=_sell_photo_text(len(d["photos"])), parse_mode="HTML",
                                            reply_markup=_sell_photo_markup(len(d["photos"])))
    except Exception:
        pass
    return True


async def sell_done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    d = sell_draft.get(uid)
    if not d or user_state.get(uid) != SELL_PHOTOS:
        await q.answer("This session expired. Please start again.", show_alert=True)
        return
    if len(d["photos"]) < SELL_MIN_IMAGES:
        await q.answer(f"Please send at least {SELL_MIN_IMAGES} photos (you sent {len(d['photos'])}).", show_alert=True)
        return
    await q.answer()
    user_state[uid] = SELL_PLATFORM
    rows, row = [], []
    for k, label in SELL_PLATFORMS.items():
        row.append(btn(label, f"sl_p_{k}", "primary"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("❌ Cancel", "sl_cancel", "danger")])
    await edit_q(q, f"🔑 <b>Step 2/4 — Login method</b>\n\nWith which account is your Free Fire ID linked?\n\n"
                    f"📷 Photos saved: <b>{len(d['photos'])}</b>", InlineKeyboardMarkup(rows))


async def sell_platform_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    d = sell_draft.get(uid)
    if not d or user_state.get(uid) != SELL_PLATFORM:
        await q.answer("This session expired. Please start again.", show_alert=True)
        return
    key = q.data[len("sl_p_"):]
    if key not in SELL_PLATFORMS:
        await q.answer()
        return
    await q.answer()
    if key == "other":
        d["await_platform_text"] = True
        await edit_q(q, "🔑 <b>Step 2/4 — Login method</b>\n\nType the name of the login method "
                        "(e.g. Guest, Twitter, Phone number).", _sell_cancel_kb())
        return
    d["platform"] = SELL_PLATFORMS[key]
    await _sell_ask_login(context.bot, q.message.chat_id, uid, q)


async def _sell_ask_login(bot, chat_id: int, uid: int, q=None):
    d = sell_draft[uid]
    user_state[uid] = SELL_LOGIN
    text = (f"📧 <b>Step 3/4 — Login ID</b>\n\nLogin method: <b>{esc(d['platform'])}</b>\n\n"
            "Send the <b>email address or mobile number</b> used to log in to this ID.")
    if q is not None:
        await edit_q(q, text, _sell_cancel_kb())
    else:
        await send_temp(bot, chat_id, uid, text, _sell_cancel_kb())


async def sell_platform_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    d = sell_draft.get(uid)
    if user_state.get(uid) != SELL_PLATFORM or not d or not d.get("await_platform_text"):
        return
    name = re.sub(r"\s+", " ", (update.message.text or "").strip())[:30]
    if len(name) < 2:
        await send_temp(context.bot, update.effective_chat.id, uid, "❌ Please type the login method name.")
        return
    d["platform"] = name
    d.pop("await_platform_text", None)
    await _sell_ask_login(context.bot, update.effective_chat.id, uid)


async def sell_login_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    d = sell_draft.get(uid)
    if user_state.get(uid) != SELL_LOGIN or not d:
        return
    val = (update.message.text or "").strip()
    if len(val) < 3 or len(val) > 120:
        await send_temp(context.bot, update.effective_chat.id, uid, "❌ Please send a valid email address or mobile number.")
        return
    d["login"] = val
    user_state[uid] = SELL_PASS
    await safe_delete(context.bot, update.effective_chat.id, update.message.message_id)
    await send_temp(context.bot, update.effective_chat.id, uid,
                    "🔐 <b>Step 4/4 — Password</b>\n\nSend the <b>password</b> of this ID.\n"
                    "<i>Your message is deleted from the chat right after the bot reads it. "
                    "It is only shared with the store admin.</i>", _sell_cancel_kb())


async def sell_pass_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    d = sell_draft.get(uid)
    if user_state.get(uid) != SELL_PASS or not d:
        return
    pw = (update.message.text or "").strip()
    await safe_delete(context.bot, update.effective_chat.id, update.message.message_id)
    if len(pw) < 1 or len(pw) > 120:
        await send_temp(context.bot, update.effective_chat.id, uid, "❌ Please send a valid password.")
        return
    d["password"] = pw
    user_state[uid] = SELL_CONFIRM
    masked = "•" * min(len(pw), 10)
    await ui_clean(context.bot, update.effective_chat.id, uid, card=False)
    await send_temp(
        context.bot, update.effective_chat.id, uid,
        "📋 <b>Please review your submission</b>\n\n"
        f"📷 Photos: <b>{len(d['photos'])}</b>\n"
        f"🔑 Login method: <b>{esc(d['platform'])}</b>\n"
        f"📧 Email / mobile: <code>{esc(d['login'])}</code>\n"
        f"🔐 Password: <code>{masked}</code>\n\n"
        "Tap <b>Submit</b> to send everything to the admin. The admin will review your ID and reply with a final price.",
        kb([btn("✅ Submit", "sl_submit", "success")], [btn("❌ Cancel", "sl_cancel", "danger")]))


async def sell_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer("Cancelled")
    reset_flows(uid)
    await ui_clean(context.bot, q.message.chat_id, uid)
    await context.bot.send_message(q.message.chat_id, "❌ Sell request cancelled.", reply_markup=menu_button())


async def _send_album(bot, chat_id: int, photos: list, caption: str | None = None):
    for i in range(0, len(photos), 10):
        chunk = photos[i:i + 10]
        media = [InputMediaPhoto(media=f, caption=(caption if (j == 0 and i == 0) else None)) for j, f in enumerate(chunk)]
        try:
            await bot.send_media_group(chat_id=chat_id, media=media)
        except Exception as e:
            log.warning("album send failed: %r", e)
            for f in chunk:
                try:
                    await bot.send_photo(chat_id=chat_id, photo=f)
                except Exception:
                    pass
        await asyncio.sleep(0.4)


def sell_admin_text(s: dict, creds: bool = True) -> str:
    who = f"@{esc(s['username'])}" if s.get("username") else esc(s.get("name") or "User")
    lines = [
        f"💸 <b>Sell request {esc(s['id'])}</b>",
        f"📌 Status: <b>{esc(s['status'])}</b>",
        f"👤 Seller: {who} (<code>{s['uid']}</code>)",
        f"⏰ Submitted: {ist_str(s['created'])}",
        f"📷 Photos: {len(s['photos'])}",
        f"🔑 Login method: {esc(s['platform'])}",
    ]
    if creds:
        lines += [f"📧 Email / mobile: <code>{esc(s['login'])}</code>", f"🔐 Password: <code>{esc(s['password'])}</code>"]
    if s.get("offer"):
        lines.append(f"💵 Offered price: <b>{rupees(s['offer']['price'])}</b>" + (f" — {esc(s['offer']['note'])}" if s['offer'].get('note') else ""))
    if s.get("post_id"):
        lines.append(f"📢 Listed as: <code>{esc(s['post_id'])}</code> at {rupees(s.get('list_price') or 0)}")
    return "\n".join(lines)


def sell_admin_markup(s: dict) -> InlineKeyboardMarkup:
    sid, st = s["id"], s["status"]
    rows = [[btn("🖼 View photos", f"sr_ph_{sid}", "primary")]]
    if st in ("submitted", "offered"):
        rows.append([btn("💵 Make / change offer" if st == "offered" else "💵 Make offer", f"sr_offer_{sid}", "success")])
        rows.append([btn("🚫 Reject", f"sr_rej_{sid}", "danger")])
    if st == "accepted":
        rows.append([btn("📢 List this ID for sale", f"sr_list_{sid}", "success")])
    rows.append([btn("🔙 Back", "admin_sells")])
    return InlineKeyboardMarkup(rows)


async def sell_submit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    bot = context.bot
    d = sell_draft.get(uid)
    if not d or user_state.get(uid) != SELL_CONFIRM or not d.get("password"):
        await q.answer("This session expired. Please start again.", show_alert=True)
        return
    if not (SELL_MIN_IMAGES <= len(d["photos"]) <= SELL_MAX_IMAGES):
        await q.answer(f"You need {SELL_MIN_IMAGES}–{SELL_MAX_IMAGES} photos.", show_alert=True)
        return
    # ── atomic: create the record, then clear the draft, before any await ──
    counters["sell"] += 1
    sid = f"S{counters['sell']:04d}"
    u = users.get(uid, {})
    s = {"id": sid, "uid": uid, "username": u.get("username", ""), "name": u.get("name", ""),
         "photos": list(d["photos"]), "platform": d["platform"], "login": d["login"], "password": d["password"],
         "status": "submitted", "created": now(), "offer": None, "post_id": None, "chat_id": q.message.chat_id}
    sells[sid] = s
    sell_draft.pop(uid, None)
    user_state.pop(uid, None)
    _db_save()
    log_event(uid, "sell_submitted", sid)
    await q.answer("Submitted!")
    await ui_clean(bot, q.message.chat_id, uid)
    await bot.send_message(q.message.chat_id,
                           f"✅ <b>Submitted!</b>\n\nYour request number is <code>{sid}</code>.\n"
                           "The admin will review your ID and send you a final price here. "
                           "You can then accept or decline it.", parse_mode="HTML",
                           reply_markup=kb([btn("📄 My Sell Requests", "sl_my", "primary")], [btn("🏠 Main Menu", "main_menu")]))
    for aid in all_admin_ids():
        try:
            await _send_album(bot, aid, s["photos"], f"Sell request {sid} — photos")
            await bot.send_message(aid, sell_admin_text(s), parse_mode="HTML", reply_markup=sell_admin_markup(s))
        except Exception as e:
            log.warning("could not notify admin %s of sell request: %r", aid, e)


async def sell_my_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    mine = sorted((s for s in sells.values() if s["uid"] == uid), key=lambda s: s["created"], reverse=True)[:10]
    if not mine:
        await context.bot.send_message(q.message.chat_id, "📭 You have no sell requests yet.", reply_markup=menu_button())
        return
    lines = []
    for s in mine:
        line = f"• <code>{s['id']}</code> — {SELL_STATUS_LABEL.get(s['status'], s['status'])}"
        if s.get("offer") and s["status"] in ("offered", "accepted", "listed", "sold"):
            line += f"\n   💵 Price: {rupees(s['offer']['price'])}"
        lines.append(line)
    await context.bot.send_message(q.message.chat_id, "📄 <b>Your sell requests</b>\n\n" + "\n\n".join(lines),
                                   parse_mode="HTML", reply_markup=menu_button())


async def sell_answer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User accepts or declines the admin's final price."""
    q = update.callback_query
    uid = q.from_user.id
    bot = context.bot
    accept = q.data.startswith("sl_acc_")
    sid = q.data[len("sl_acc_"):] if accept else q.data[len("sl_dec_"):]
    s = sells.get(sid)
    if not s or s["uid"] != uid:
        await q.answer("Request not found.", show_alert=True)
        return
    if s["status"] != "offered" or not s.get("offer"):
        await q.answer("This offer is no longer active.", show_alert=True)
        try:
            await edit_q(q, q.message.text_html if q.message.text else (q.message.caption or ""), None)
        except Exception:
            pass
        return
    price = s["offer"]["price"]
    s["status"] = "accepted" if accept else "declined"
    s["answered"] = now()
    _db_save()
    log_event(uid, "sell_accepted" if accept else "sell_declined", sid)
    await q.answer("Accepted" if accept else "Declined")
    if accept:
        await edit_q(q, f"✅ <b>You accepted the price of {rupees(price)}</b> for request <code>{sid}</code>.\n\n"
                        "The admin will list your ID for sale and message you here.", None)
    else:
        await edit_q(q, f"❌ <b>You declined the price of {rupees(price)}</b> for request <code>{sid}</code>.\n\n"
                        "Thank you — you can submit a new request any time.", None)
    await notify_admins(bot, f"{'✅' if accept else '❌'} Seller <code>{uid}</code> {'ACCEPTED' if accept else 'declined'} "
                             f"the offer of {rupees(price)} for <b>{sid}</b>.",
                        markup=sell_admin_markup(s) if accept else None)


async def sell_on_sold(bot, post: dict, pid: str):
    """Called when a listing created from a sell request is purchased."""
    sid = post.get("sell_id")
    s = sells.get(sid) if sid else None
    if not s:
        return
    s["status"] = "sold"
    s["sold_ts"] = now()
    _db_save()
    try:
        await bot.send_message(s["uid"], f"🎉 <b>Great news!</b> Your ID (request <code>{esc(sid)}</code>) has been sold.\n"
                                         "The admin will contact you about the payout.", parse_mode="HTML")
    except Exception:
        pass
    await notify_admins(bot, f"💰 Sell request <b>{esc(sid)}</b> (listing <code>{esc(pid)}</code>) sold — "
                             f"pay the seller <code>{s['uid']}</code> the agreed {rupees((s.get('offer') or {}).get('price', 0))}.")


# ─── ADMIN SIDE ───────────────────────────────────────────────────────────────
async def admin_sells(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    reset_flows(q.from_user.id)
    openl = sorted((s for s in sells.values() if s["status"] in SELL_OPEN), key=lambda s: s["created"])
    recent = sorted((s for s in sells.values() if s["status"] not in SELL_OPEN), key=lambda s: s["created"], reverse=True)[:8]
    rows = []
    for s in openl[:25]:
        rows.append([btn(f"{s['id']} · {s['status']} · {s['platform'][:10]}", f"sr_v_{s['id']}", "primary")])
    for s in recent:
        rows.append([btn(f"{s['id']} · {s['status']}", f"sr_v_{s['id']}")])
    rows.append([btn("🔙 Back", "admin_panel")])
    await edit_q(q, "💸 <b>Sell Requests</b>\n\n"
                    f"Open: <b>{len(openl)}</b> (new / offered / accepted)\n"
                    "Tap a request to review it." if sells else "💸 <b>Sell Requests</b>\n\nNo requests yet.",
                 InlineKeyboardMarkup(rows))


async def sr_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    s = sells.get(q.data[len("sr_v_"):])
    if not s:
        await q.answer("Not found.", show_alert=True)
        return
    await q.answer()
    await edit_q(q, sell_admin_text(s), sell_admin_markup(s))


async def sr_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    s = sells.get(q.data[len("sr_ph_"):])
    if not s:
        await q.answer("Not found.", show_alert=True)
        return
    await q.answer("Sending photos…")
    await _send_album(context.bot, q.message.chat_id, s["photos"], f"Sell request {s['id']} — photos")


async def sr_offer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q):
        return
    s = sells.get(q.data[len("sr_offer_"):])
    if not s or s["status"] not in ("submitted", "offered"):
        await q.answer("This request cannot get an offer now.", show_alert=True)
        return
    await q.answer()
    reset_flows(uid)
    user_state[uid] = ADMIN_SELL_OFFER
    admin_temp[uid] = {"sid": s["id"]}
    await context.bot.send_message(
        q.message.chat_id,
        f"💵 <b>Final price for {s['id']}</b>\n\nSend the price in ₹ the store will pay the seller. "
        "You may add a short note after it.\nExample: <code>1200 Great account, price is final</code>",
        parse_mode="HTML", reply_markup=_admin_markup_back("admin_sells"))


async def sr_offer_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_SELL_OFFER:
        return
    s = sells.get((admin_temp.get(uid) or {}).get("sid"))
    parts = (update.message.text or "").strip().split(None, 1)
    try:
        price = _r2(float(parts[0].replace("₹", "")))
        if price < MIN_FINAL_PRICE:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(f"❌ Start your message with a price of {rupees(MIN_FINAL_PRICE)} or more.")
        return
    if not s or s["status"] not in ("submitted", "offered"):
        await update.message.reply_text("❌ This request can no longer receive an offer.")
        reset_flows(uid)
        return
    note = parts[1][:200] if len(parts) > 1 else ""
    s["offer"] = {"price": price, "note": note, "ts": now(), "by": uid}
    s["status"] = "offered"
    _db_save()
    reset_flows(uid)
    try:
        await context.bot.send_message(
            s["uid"],
            f"💵 <b>Final price for your ID</b> (request <code>{s['id']}</code>)\n\n"
            f"The store offers you <b>{rupees(price)}</b>." + (f"\n📝 {esc(note)}" if note else "") +
            "\n\nDo you accept this price? If you accept, the admin will list your ID for sale.",
            parse_mode="HTML",
            reply_markup=kb([btn(f"✅ Accept {rupees(price)}", f"sl_acc_{s['id']}", "success")],
                            [btn("❌ Decline", f"sl_dec_{s['id']}", "danger")]))
        sent = "✅ Offer sent to the seller."
    except Exception as e:
        sent = f"⚠️ Offer saved, but the seller could not be messaged ({e!r})."[:300]
    log_event(s["uid"], "sell_offered", s["id"])
    await update.message.reply_text(f"{sent}\n\n" + sell_admin_text(s), parse_mode="HTML", reply_markup=sell_admin_markup(s))


async def sr_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    s = sells.get(q.data[len("sr_rej_"):])
    if not s or s["status"] not in ("submitted", "offered"):
        await q.answer("This request cannot be rejected now.", show_alert=True)
        return
    s["status"] = "rejected"
    _db_save()
    await q.answer("Rejected")
    try:
        await context.bot.send_message(s["uid"], f"🚫 Sorry, your sell request <code>{s['id']}</code> was not accepted by the store.",
                                       parse_mode="HTML", reply_markup=menu_button())
    except Exception:
        pass
    await edit_q(q, sell_admin_text(s), sell_admin_markup(s))


async def sr_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q):
        return
    s = sells.get(q.data[len("sr_list_"):])
    if not s or s["status"] != "accepted":
        await q.answer("Only accepted requests can be listed.", show_alert=True)
        return
    await q.answer()
    reset_flows(uid)
    user_state[uid] = ADMIN_SELL_LIST
    admin_temp[uid] = {"sid": s["id"]}
    await context.bot.send_message(
        q.message.chat_id,
        f"📢 <b>List {s['id']} for sale</b>\n\nSend the <b>selling price</b> in ₹ for buyers "
        f"(the seller was offered {rupees(s['offer']['price'])}). Optionally add a description after the price.\n"
        "Example: <code>1999 Level 70, Elite pass</code>", parse_mode="HTML", reply_markup=_admin_markup_back("admin_sells"))


async def sr_list_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_SELL_LIST:
        return
    s = sells.get((admin_temp.get(uid) or {}).get("sid"))
    parts = (update.message.text or "").strip().split(None, 1)
    try:
        price = _r2(float(parts[0].replace("₹", "")))
        if price < MIN_FINAL_PRICE:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(f"❌ Start your message with a price of {rupees(MIN_FINAL_PRICE)} or more.")
        return
    if not s or s["status"] != "accepted":
        await update.message.reply_text("❌ This request can no longer be listed.")
        reset_flows(uid)
        return
    pid = new_post_id()
    creds = (f"Login method: {s['platform']}\nEmail / mobile: {s['login']}\nPassword: {s['password']}")
    posts[pid] = {"price": price, "creds": creds, "images": list(s["photos"])[:ADMIN_MAX_IMAGES],
                  "info": (parts[1][:200] if len(parts) > 1 else ""), "created": now(), "source": "sell",
                  "sell_id": s["id"], "seller": s["uid"], "channel_msg": None, "channel_kind": None}
    s["status"], s["post_id"], s["list_price"] = "listed", pid, price
    reset_flows(uid)
    _db_save()
    log_event(s["uid"], "sell_listed", s["id"])
    await update.message.reply_text(f"✅ Listed as <code>{pid}</code> at {rupees(price)}.", parse_mode="HTML",
                                    reply_markup=kb([btn("💸 Sell Requests", "admin_sells")], [btn("⚙️ Admin Panel", "admin_panel")]))
    try:
        await context.bot.send_message(s["uid"], f"📢 <b>Your ID is now listed for sale!</b>\n\nRequest <code>{s['id']}</code> · "
                                                 f"listing <code>{pid}</code>.\nWe will tell you as soon as it is sold.",
                                       parse_mode="HTML", reply_markup=menu_button())
    except Exception:
        pass
    await channel_post(context.bot, pid)

# ═══════════════════════════════════════════════════════════════════════════════
# 💾 BACKUP, 2-HOUR ACTIVITY REPORT, RESTORE, VERIFIED UPDATER
# ═══════════════════════════════════════════════════════════════════════════════
BACKUP_FORMAT = 1
BK_SECRET_FIELDS = (("posts", "creds"), ("sold_meta", "creds"), ("sells", "login"), ("sells", "password"))


def _fernet():
    """Fernet cipher derived from BACKUP_KEY, or None when no key / library is available."""
    if not BACKUP_KEY:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(BACKUP_KEY.encode()).digest()))
    except Exception:
        log.warning("cryptography is not installed — credentials will be excluded from backups")
        return None


def _protect_store(store: dict, f) -> bool:
    """Encrypt credential fields in a snapshot (or blank them when there is no key). Returns True if encrypted."""
    for coll, field in BK_SECRET_FIELDS:
        for rec in (store.get(coll) or {}).values():
            v = rec.get(field)
            if not v:
                continue
            rec[field] = ("enc:" + f.encrypt(str(v).encode()).decode()) if f else None
    return f is not None


def _unprotect_store(store: dict, f):
    """Reverse of _protect_store. Raises ValueError if an encrypted value cannot be read."""
    for coll, field in BK_SECRET_FIELDS:
        for rec in (store.get(coll) or {}).values():
            v = rec.get(field)
            if isinstance(v, str) and v.startswith("enc:"):
                if not f:
                    raise ValueError("this backup contains encrypted credentials — set the same BACKUP_KEY first")
                try:
                    rec[field] = f.decrypt(v[4:].encode()).decode()
                except Exception:
                    raise ValueError("BACKUP_KEY does not match this backup (cannot decrypt credentials)")
    for rec in (store.get("posts") or {}).values():
        if rec.get("creds") is None:
            rec["missing_creds"] = True              # excluded from the backup → not sellable until re-entered


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf8")


def build_backup() -> tuple[bytes, dict]:
    """Synchronous (no awaits) so the snapshot is consistent. Returns (gz bytes, meta)."""
    store = _db_snapshot(copy=True, include_activity=True)
    encrypted = _protect_store(store, _fernet())
    data = {
        "store": store,
        "botdata": {"users": {str(k): v for k, v in users.items()},
                    "super_admins": {str(k): v for k, v in super_admins.items()},
                    "icons": dict(_ICON_OVERRIDES),
                    "bc_chats": {str(k): v for k, v in bc_chats.items()}},
        "force_join": list(force_join),
    }
    meta = {"format": BACKUP_FORMAT, "app_version": APP_VERSION, "ts": now(), "sha256": hashlib.sha256(_canon(data)).hexdigest(),
            "credentials": "encrypted" if encrypted else "excluded",
            "counts": {"posts": len(posts), "orders": len(order_log), "users": len(users), "wallets": len(balances),
                       "promos": len(promos), "sells": len(sells)}}
    raw = gzip.compress(json.dumps({"meta": meta, "data": data}, ensure_ascii=False).encode("utf8"), 6)
    return raw, meta


def validate_backup(raw: bytes):
    """(ok, error, obj). Verifies gzip, JSON, version, structure and SHA-256 before anything is touched."""
    try:
        obj = json.loads(gzip.decompress(raw).decode("utf8"))
    except Exception:
        return False, "The file is not a valid backup (cannot read it).", None
    try:
        meta, data = obj["meta"], obj["data"]
        if meta.get("format") != BACKUP_FORMAT:
            return False, f"Unsupported backup format {meta.get('format')}.", None
        if hashlib.sha256(_canon(data)).hexdigest() != meta.get("sha256"):
            return False, "Integrity check failed — the file is damaged or was modified.", None
        st = data["store"]
        for k, typ in (("posts", dict), ("sold", list), ("order_log", dict), ("balances", dict), ("ledger", list),
                       ("promos", dict), ("counters", dict)):
            if not isinstance(st.get(k), typ):
                return False, f"Backup structure is invalid ({k}).", None
        if not isinstance(data["botdata"].get("users"), dict) or not isinstance(data.get("force_join"), list):
            return False, "Backup structure is invalid (users / force join).", None
    except (KeyError, TypeError, AttributeError):
        return False, "Backup structure is invalid.", None
    return True, "", obj


def _write_local_backup(raw: bytes, ts: float, prefix: str = "backup") -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"{prefix}_{datetime.fromtimestamp(ts, IST):%Y%m%d_%H%M%S}.json.gz")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    os.replace(tmp, path)
    keep = max(3, int(settings["backup"].get("keep", 12)))
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("backup_") and f.endswith(".json.gz"))
    for old in files[:-keep]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    return path


# ─── REPORT ───────────────────────────────────────────────────────────────────
STAGE = {
    "start": (0, "opened the bot"), "account": (1, "viewed their account"), "orders": (1, "viewed their orders"),
    "invoice_view": (1, "viewed an invoice"), "catalogue": (2, "browsed the catalogue"),
    "view_id": (3, "viewed an ID"), "search": (3, "searched for an ID"), "checkout": (4, "opened checkout"),
    "promo_try": (4, "tried a promo code"), "order_created": (5, "reached the payment page"),
    "expired": (5, "let the payment expire"), "cancelled": (5, "cancelled the payment"),
    "paid": (9, "✅ completed a purchase (UPI)"), "balance_purchase": (9, "✅ completed a purchase (wallet)"),
    "topup_menu": (2, "opened Add Balance"), "topup_start": (5, "reached a top-up payment page"),
    "topup_paid": (9, "✅ topped up the wallet"), "balance_adjust": (1, "wallet adjusted by admin"),
    "sell_start": (2, "started selling an ID"), "sell_submitted": (8, "📤 submitted a sell request"),
    "sell_offered": (8, "received a price offer"), "sell_accepted": (9, "✅ accepted a price offer"),
    "sell_declined": (8, "declined a price offer"), "sell_listed": (9, "ID listed for sale"),
}


def _who(uid: int) -> str:
    u = users.get(uid) or {}
    return f"@{esc(u['username'])}" if u.get("username") else (esc(u.get("name") or "User"))


def build_report(since: float, until: float | None = None) -> list[str]:
    until = until or now()
    ev = [e for e in activity if since <= e["ts"] <= until]
    od = [o for o in order_log.values() if since <= o["ts"] <= until]
    purchases = [o for o in od if o["kind"] == "purchase" and o.get("status") == "paid"]
    topups = [o for o in od if o["kind"] == "topup"]
    rev_upi = sum(o["paid"] for o in purchases if o["method"] == "upi")
    rev_wal = sum(o["paid"] for o in purchases if o["method"] == "balance")
    disc = sum(o["discount"] for o in purchases)
    active = {e["uid"] for e in ev if e["uid"] not in set(all_admin_ids())}
    new_users = [u for u, r in users.items() if since <= r.get("first_seen", 0) <= until]

    def uniq(*acts):
        return len({e["uid"] for e in ev if e["a"] in acts and e["uid"] in active})

    head = (f"📊 <b>Activity report</b>\n🕒 {ist_str(since)} → {ist_str(until)}\n"
            f"🏪 {esc(settings.get('brand') or BOT_NAME)} · v{APP_VERSION}\n")
    s1 = [
        "━━━━━━━━━━━━━━━━━━",
        "📦 <b>Orders</b>",
        f"• Purchases completed: <b>{len(purchases)}</b>  (UPI {rupees(rev_upi)} · wallet {rupees(rev_wal)})",
        f"• Wallet top-ups: <b>{len(topups)}</b>  ({rupees(sum(o['price'] for o in topups))})",
        f"• Orders created: {sum(1 for e in ev if e['a'] == 'order_created')} · expired: {sum(1 for e in ev if e['a'] == 'expired')}"
        f" · cancelled: {sum(1 for e in ev if e['a'] == 'cancelled')}",
        f"• Pending right now: <b>{len(pending_orders)}</b>",
        f"• Promo discount given: {rupees(disc)}",
    ]
    if purchases:
        s1.append("• Sold: " + ", ".join(f"<code>{esc(o['post_id'])}</code> ({rupees(o['paid'])})" for o in purchases[:12]))
    s2 = [
        "━━━━━━━━━━━━━━━━━━",
        "👥 <b>Users</b>",
        f"• New users: <b>{len(new_users)}</b> · active users: <b>{len(active)}</b> · total: {len(users)}",
        "<b>Funnel (unique users)</b>",
        f"• Browsed catalogue: {uniq('catalogue')} → viewed/searched an ID: {uniq('view_id', 'search')}",
        f"• Opened checkout: {uniq('checkout')} → payment page: {uniq('order_created')} → paid: {uniq('paid', 'balance_purchase')}",
        f"• Wallet: opened Add Balance {uniq('topup_menu')} → top-up paid {uniq('topup_paid')}",
        f"• Sell: started {uniq('sell_start')} → submitted {uniq('sell_submitted')}",
    ]
    best: dict = {}
    for e in ev:
        if e["uid"] not in active:
            continue
        rank = STAGE.get(e["a"], (1, e["a"]))[0]
        b = best.get(e["uid"])
        cnt = (b["n"] + 1) if b else 1
        if not b or rank >= b["rank"]:
            best[e["uid"]] = {"rank": rank, "a": e["a"], "d": e["d"], "ts": e["ts"], "n": cnt}
        else:
            b["n"] = cnt
    if best:
        s2.append("")
        s2.append("<b>How far each user went</b>")
        for uid, b in sorted(best.items(), key=lambda kv: (-kv[1]["rank"], -kv[1]["ts"]))[:20]:
            label = STAGE.get(b["a"], (0, b["a"]))[1]
            det = f" ({esc(b['d'])})" if b["d"] and b["a"] in ("view_id", "search", "checkout", "order_created", "paid", "balance_purchase", "sell_submitted", "sell_accepted") else ""
            s2.append(f"• {_who(uid)} <code>{uid}</code> — {label}{det} · {b['n']} actions · {ist_str(b['ts'], '%I:%M %p')}")
        if len(best) > 20:
            s2.append(f"…and {len(best) - 20} more")
    searches = {}
    for e in ev:
        if e["a"] in ("search", "view_id") and e["d"]:
            searches[e["d"]] = searches.get(e["d"], 0) + 1
    if searches:
        s2.append("\n<b>Most viewed / searched IDs</b>: " + ", ".join(
            f"<code>{esc(k)}</code> ×{v}" for k, v in sorted(searches.items(), key=lambda kv: -kv[1])[:8]))
    promo_tries = [e["d"] for e in ev if e["a"] == "promo_try"]
    if promo_tries:
        s2.append(f"🎟 Promo codes tried: {len(promo_tries)} (" + ", ".join(sorted({esc(x) for x in promo_tries})[:8]) + ")")
    new_sells = [s for s in sells.values() if since <= s["created"] <= until]
    avail = len(avail_ids())
    s3 = [
        "━━━━━━━━━━━━━━━━━━",
        "💸 <b>Sell requests</b>",
        f"• New: <b>{len(new_sells)}</b> · open now: <b>{_pending_sell_count()}</b> · listed: {sum(1 for s in sells.values() if s['status'] == 'listed')}",
        "━━━━━━━━━━━━━━━━━━",
        "💰 <b>Wallets &amp; stock</b>",
        f"• Wallet balance held (all users): <b>{rupees(sum(balances.values()))}</b> · top-ups in period: {rupees(sum(o['price'] for o in topups))}",
        f"• IDs available: <b>{avail}</b> · in payment: {len(reserved)} · sold (total): {len(sold)}" + ("  ⚠️ <b>No stock!</b>" if avail == 0 else ""),
    ]
    bk = settings["backup"]
    s3.append("━━━━━━━━━━━━━━━━━━")
    s3.append("💾 <b>Backup</b>: " + (f"last OK {ist_str(bk['last_ok'])} · {bk.get('last_size', 0) // 1024} KB" if bk.get("last_ok") else "no successful backup yet")
              + (f"\n⚠️ Last error: {esc(bk['last_err'])[:150]}" if bk.get("last_err") else ""))
    s3.append("\n<i>Passwords and credentials are never included in this report.</i>")
    text = "\n".join([head] + s1 + s2 + s3)
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    out.append(cur)
    return out


# ─── SCHEDULED BACKUP + REPORT ────────────────────────────────────────────────
_bk_busy = False


async def backup_cycle(bot, reason: str = "scheduled", send_report: bool = True) -> tuple[bool, str]:
    global _bk_busy
    bk = settings["backup"]
    chat = bk.get("chat")
    if _bk_busy:
        return False, "A backup is already running."
    if not chat:
        return False, "No backup group is set."
    _bk_busy = True
    try:
        t0 = now()
        raw, meta = build_backup()
        try:
            await asyncio.to_thread(_write_local_backup, raw, meta["ts"])
        except Exception as e:
            log.warning("local backup write failed: %r", e)
        cap = (f"💾 <b>Backup</b> ({esc(reason)})\n🕒 {ist_str(meta['ts'])}\n"
               f"📦 {meta['counts']['posts']} listings · {meta['counts']['orders']} orders · {meta['counts']['users']} users · "
               f"{meta['counts']['wallets']} wallets\n🔐 Credentials: {meta['credentials']}\n🔎 SHA-256: <code>{meta['sha256'][:16]}…</code>"
               + ("" if meta["credentials"] == "encrypted" else "\n⚠️ Set <b>BACKUP_KEY</b> (and install cryptography) to include credentials safely."))
        fname = f"backup_{datetime.fromtimestamp(meta['ts'], IST):%Y%m%d_%H%M%S}.json.gz"
        msg = await bot.send_document(chat_id=chat, document=io.BytesIO(raw), filename=fname, caption=cap, parse_mode="HTML")
        bk["last_ok"], bk["last_err"], bk["last_size"], bk["last_ts"] = now(), None, len(raw), now()
        ids = bk.setdefault("msg_ids", [])
        ids.append(msg.message_id)
        keep = max(3, int(bk.get("keep", 12)))
        while len(ids) > keep:
            await safe_delete(bot, chat, ids.pop(0))
        _db_save()
        if send_report:
            since = settings.get("report_ts") or (t0 - bk["interval_h"] * 3600)
            for part in build_report(since, t0):
                await bot.send_message(chat_id=chat, text=part, parse_mode="HTML")
                await asyncio.sleep(0.4)
            settings["report_ts"] = t0
            _db_save()
        return True, "Backup and report sent."
    except Exception as e:
        log.exception("backup failed")
        bk["last_err"] = repr(e)[:200]
        bk["last_ts"] = now() - bk["interval_h"] * 3600 + 600       # retry in ~10 minutes
        _db_save()
        await notify_admin(bot, f"⚠️ Backup failed: {e!r}"[:500] + "\nCheck that the bot is an admin of the backup group.")
        return False, f"Failed: {e!r}"[:300]
    finally:
        _bk_busy = False


async def backup_loop(bot):
    while True:
        try:
            await asyncio.sleep(60)
            bk = settings.get("backup") or {}
            if bk.get("enabled") and bk.get("chat") and now() - bk.get("last_ts", 0) >= bk.get("interval_h", 2) * 3600:
                await backup_cycle(bot, "scheduled")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("backup loop error")


# ─── ADMIN UI ─────────────────────────────────────────────────────────────────
def _bk_text() -> str:
    bk = settings["backup"]
    f = _fernet()
    return ("💾 <b>Backup &amp; Reports</b>\n\n"
            f"Backup group: <b>{esc(bk.get('chat_title') or bk['chat']) if bk.get('chat') else 'not set'}</b>\n"
            f"Automatic: <b>{'ON ✅' if bk.get('enabled') else 'OFF ⛔'}</b> — every <b>{bk['interval_h']:g} h</b>\n"
            f"Last backup: {ist_str(bk['last_ok']) if bk.get('last_ok') else 'never'}"
            + (f" ({bk.get('last_size', 0) // 1024} KB)" if bk.get("last_ok") else "") + "\n"
            + (f"⚠️ Last error: {esc(bk['last_err'])[:120]}\n" if bk.get("last_err") else "")
            + f"Credentials in backups: <b>{'encrypted (BACKUP_KEY set)' if f else 'EXCLUDED — set BACKUP_KEY'}</b>\n"
            f"Kept: last {bk['keep']} backups\n\n"
            "<b>How to set the group:</b> add the bot to your private group, make it admin, then send "
            "<code>/setbackup</code> inside that group (owner only). Or tap Set Group and send the group's ID.\n\n"
            "<i>Every cycle sends one backup file + an activity report.</i>")


def _bk_markup() -> InlineKeyboardMarkup:
    bk = settings["backup"]
    rows = [[btn("📤 Backup + report now", "bk_now", "success")],
            [btn("📊 Report only", "bk_report", "primary"), btn("⬇️ Download backup", "bk_dl", "primary")],
            [btn("⏸ Turn OFF" if bk.get("enabled") else "▶️ Turn ON", "bk_toggle", "danger" if bk.get("enabled") else "success"),
             btn("⏱ Interval", "bk_interval", "primary")],
            [btn("👥 Set Group", "bk_setchat", "primary")]]
    if is_owner_cache():
        rows.append([btn("♻️ Restore backup", "bk_restore", "danger")])
        rows.append([btn("🔄 Check for update", "bk_update", "primary")])
    rows.append([btn("🔙 Back", "admin_panel")])
    return InlineKeyboardMarkup(rows)


def is_owner_cache() -> bool:      # restore/update buttons are shown to everyone who opens the menu; actions re-check owner
    return True


async def admin_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    reset_flows(q.from_user.id)
    await edit_q(q, _bk_text(), _bk_markup())


async def _set_backup_chat(bot, chat_id, title=None):
    ok, why, chat = await _bc_check(bot, chat_id)
    if not ok or chat is None:
        return False, why or "chat not accessible"
    if chat.type not in ("group", "supergroup"):
        return False, "the backup chat must be a group"
    bk = settings["backup"]
    bk["chat"], bk["chat_title"], bk["msg_ids"] = chat.id, chat.title or str(chat.id), []
    bk["last_ts"] = 0.0
    _db_save()
    return True, chat.title or str(chat.id)


async def cmd_setbackup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner sends /setbackup inside the private backup group."""
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not update.effective_user or not is_owner(update.effective_user.id):
        return
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Send this command inside the private backup group (the bot must be an admin there).")
        return
    ok, info = await _set_backup_chat(context.bot, chat.id)
    if ok:
        await msg.reply_text("✅ This group is now the backup group. The first backup and report will arrive within a minute, "
                             "then every {:g} hours.".format(settings["backup"]["interval_h"]))
    else:
        await msg.reply_text(f"❌ Could not use this group: {info}. Make the bot an admin and try again.")


async def bk_setchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    user_state[q.from_user.id] = ADMIN_BACKUP_CHAT
    await edit_q(q, "👥 Send the backup group's ID (like <code>-1001234567890</code>), its @username or t.me link.\n\n"
                    "Easier: add the bot to the group, make it admin and send <code>/setbackup</code> there.",
                 _admin_markup_back("admin_backup"))


async def bk_chat_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_BACKUP_CHAT:
        return
    try:
        ident = _bc_ident(update.message.text or "")
    except ValueError as e:
        await update.message.reply_text(f"❌ {esc(e)}", parse_mode="HTML")
        return
    ok, info = await _set_backup_chat(context.bot, ident)
    if not ok:
        await update.message.reply_text(f"❌ {esc(info)}", parse_mode="HTML")
        return
    user_state.pop(uid, None)
    await update.message.reply_text(f"✅ Backup group set: <b>{esc(info)}</b>\nThe first backup will arrive within a minute.",
                                    parse_mode="HTML", reply_markup=_admin_markup_back("admin_backup"))


async def bk_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer("Working…")
    ok, msg = await backup_cycle(context.bot, "manual")
    await edit_q(q, ("✅ " if ok else "❌ ") + esc(msg) + "\n\n" + _bk_text(), _bk_markup())


async def bk_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer("Building report…")
    since = settings.get("report_ts") or (now() - 2 * 3600)
    for part in build_report(since):
        await context.bot.send_message(q.message.chat_id, part, parse_mode="HTML")


async def bk_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer("Preparing…")
    raw, meta = build_backup()
    await context.bot.send_document(q.message.chat_id, io.BytesIO(raw),
                                    filename=f"backup_{datetime.fromtimestamp(meta['ts'], IST):%Y%m%d_%H%M%S}.json.gz",
                                    caption=f"💾 Backup — credentials {meta['credentials']}")


async def bk_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    bk = settings["backup"]
    bk["enabled"] = not bk.get("enabled")
    _db_save()
    await q.answer("ON" if bk["enabled"] else "OFF")
    await edit_q(q, _bk_text(), _bk_markup())


async def bk_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q):
        return
    await q.answer()
    user_state[q.from_user.id] = ADMIN_BK_INTERVAL
    await edit_q(q, "⏱ Send the backup/report interval in <b>hours</b> (1–24, default 2).", _admin_markup_back("admin_backup"))


async def bk_interval_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_BK_INTERVAL:
        return
    try:
        h = float((update.message.text or "").strip())
        if not (1 <= h <= 24):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send a number of hours between 1 and 24.")
        return
    settings["backup"]["interval_h"] = h
    _db_save()
    user_state.pop(uid, None)
    await update.message.reply_text(_bk_text(), parse_mode="HTML", reply_markup=_bk_markup())


# ─── RESTORE (owner only, validated, with confirmation) ───────────────────────
async def bk_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q, owner=True):
        return
    await q.answer()
    user_state[q.from_user.id] = ADMIN_RESTORE_FILE
    await edit_q(q, "♻️ <b>Restore backup</b>\n\nSend the backup file (<code>backup_….json.gz</code>) here as a document.\n"
                    "It is fully validated first; nothing changes until you confirm.\n\n"
                    "<i>Running payment/sell conversations are not restored — only stored data.</i>",
                 _admin_markup_back("admin_backup"))


async def restore_doc_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or user_state.get(uid) != ADMIN_RESTORE_FILE:
        return False
    doc = update.message.document
    if not doc or (doc.file_size or 0) > 20 * 1024 * 1024:
        await update.message.reply_text("❌ Please send a backup file (max 20 MB).")
        return True
    try:
        f = await context.bot.get_file(doc.file_id)
        raw = bytes(await f.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(f"❌ Could not download the file: {e!r}"[:300])
        return True
    ok, err, obj = validate_backup(raw)
    if ok:
        try:                                                 # make sure credentials can be read before offering to restore
            _unprotect_store(json.loads(json.dumps(obj["data"]["store"])), _fernet())
        except ValueError as e:
            ok, err = False, str(e)
    if not ok:
        await update.message.reply_text(f"❌ <b>Restore refused</b>\n\n{esc(err)}", parse_mode="HTML")
        return True
    m, c = obj["meta"], obj["meta"]["counts"]
    _restore_stage[uid] = {"obj": obj, "ts": now()}
    await update.message.reply_text(
        "✅ <b>Backup file is valid</b>\n\n"
        f"🕒 Created: {ist_str(m['ts'])}\n📦 {c['posts']} listings · {c['orders']} orders · {c['users']} users · "
        f"{c['wallets']} wallets · {c['promos']} promos · {c['sells']} sell requests\n🔐 Credentials: {m['credentials']}\n\n"
        "⚠️ Restoring <b>replaces all current data</b>. A safety copy of the current data is saved first.\n"
        "Restore now?", parse_mode="HTML",
        reply_markup=kb([btn("✅ Yes, restore", "bk_restore_yes", "danger")], [btn("❌ No", "admin_backup", "primary")]))
    return True


async def bk_restore_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not await _gate(q, owner=True):
        return
    st = _restore_stage.pop(uid, None)
    if not st or now() - st["ts"] > 600:
        await q.answer("The restore request expired. Send the file again.", show_alert=True)
        return
    await q.answer("Restoring…")
    obj = st["obj"]
    try:
        raw_now, _ = build_backup()
        await asyncio.to_thread(_write_local_backup, raw_now, now(), "backup_prerestore")
    except Exception as e:
        log.warning("pre-restore safety copy failed: %r", e)
    # ── apply synchronously (no awaits) ──
    try:
        data = json.loads(json.dumps(obj["data"]))
        store = data["store"]
        _unprotect_store(store, _fernet())
        for t in list(_timers.values()) + list(_pollers.values()):
            t.cancel()
        _timers.clear()
        _pollers.clear()
        keep_bk = dict(settings.get("backup") or {})          # keep this server's backup-group wiring
        _db_apply(store)
        settings["backup"].update({k: keep_bk[k] for k in ("chat", "chat_title", "msg_ids", "enabled", "interval_h", "keep") if k in keep_bk})
        bd = data["botdata"]
        users.clear();        users.update({int(k): v for k, v in bd.get("users", {}).items()})
        super_admins.clear(); super_admins.update({int(k): v for k, v in bd.get("super_admins", {}).items()})
        bc_chats.clear();     bc_chats.update({int(k): v for k, v in bd.get("bc_chats", {}).items()})
        _ICON_OVERRIDES.clear(); _ICON_OVERRIDES.update({k: v for k, v in bd.get("icons", {}).items() if k in ADMIN_ICON_SPECS})
        force_join[:] = [e for e in data.get("force_join", []) if isinstance(e, dict) and "id" in e and "link" in e]
        checkout.clear(); user_state.clear(); admin_temp.clear(); photo_sessions.clear(); sell_draft.clear()
        _resolve_admin_icons()
        _db_flush(force=True)
        _data_save()
        _fj_save()
    except Exception as e:
        log.exception("restore failed")
        await edit_q(q, f"❌ Restore failed: {esc(repr(e))[:300]}\n\nA safety copy of the previous data is in the backups folder.",
                     _admin_markup_back("admin_backup"))
        return
    await recover_pending_orders(context.bot)
    await edit_q(q, f"✅ <b>Restore complete</b>\n\n{len(posts)} listings · {len(order_log)} orders · {len(users)} users · "
                    f"{len(balances)} wallets restored.", _admin_markup_back("admin_backup"))


# ─── VERIFIED UPDATER (owner-controlled; never silent) ────────────────────────
def _ver_tuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4])


async def _fetch_manifest():
    if not UPDATE_MANIFEST_URL.startswith("https://"):
        raise RuntimeError("UPDATE_MANIFEST_URL is not set (must be an https URL).")
    async with http().get(UPDATE_MANIFEST_URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        man = json.loads(await r.text())
    for k in ("version", "url", "sha256"):
        if not man.get(k):
            raise RuntimeError(f"manifest is missing '{k}'")
    if not str(man["url"]).startswith("https://"):
        raise RuntimeError("update URL must be https")
    return man


async def bk_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q, owner=True):
        return
    await q.answer("Checking…")
    try:
        man = await _fetch_manifest()
    except Exception as e:
        await edit_q(q, f"🔄 <b>Updates</b>\n\nCurrent version: <b>{APP_VERSION}</b>\n❌ {esc(e)}", _admin_markup_back("admin_backup"))
        return
    newer = _ver_tuple(man["version"]) > _ver_tuple(APP_VERSION)
    rows = ([[btn(f"⬇️ Install {man['version']}", "bk_update_yes", "success")]] if newer else []) + back_button("admin_backup")
    await edit_q(q, f"🔄 <b>Updates</b>\n\nCurrent: <b>{APP_VERSION}</b> · Available: <b>{esc(man['version'])}</b>\n"
                    + ("A newer version is available. The download is verified (SHA-256"
                       + (" + signature" if UPDATE_PUBKEY else "") + ") and syntax-checked before it replaces the running file. "
                       "The bot restarts and rolls back automatically if it fails to start.\n\n"
                       + (f"📝 {esc(man.get('notes', ''))[:300]}" if man.get("notes") else "")
                       if newer else "You are up to date."), InlineKeyboardMarkup(rows))


async def bk_update_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _gate(q, owner=True):
        return
    await q.answer("Downloading…")
    try:
        man = await _fetch_manifest()
        if _ver_tuple(man["version"]) <= _ver_tuple(APP_VERSION):
            raise RuntimeError("no newer version")
        async with http().get(man["url"], timeout=aiohttp.ClientTimeout(total=60)) as r:
            r.raise_for_status()
            body = await r.read()
        if len(body) > 5 * 1024 * 1024:
            raise RuntimeError("file is unexpectedly large")
        if hashlib.sha256(body).hexdigest().lower() != str(man["sha256"]).lower():
            raise RuntimeError("SHA-256 mismatch — download rejected")
        if UPDATE_PUBKEY:
            sig = man.get("signature")
            if not sig:
                raise RuntimeError("manifest has no signature but UPDATE_PUBKEY is set")
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(base64.b64decode(UPDATE_PUBKEY)).verify(base64.b64decode(sig), body)
        src = body.decode("utf8")
        compile(src, "update.py", "exec")
        if f'APP_VERSION = "{man["version"]}"' not in src:
            raise RuntimeError("the file's APP_VERSION does not match the manifest")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        prev = os.path.join(BACKUP_DIR, f"bot_prev_{APP_VERSION}.py")
        shutil.copy(SCRIPT_PATH, prev)
        tmp = SCRIPT_PATH + ".new"
        with open(tmp, "wb") as fh:
            fh.write(body)
        _db_flush(force=True)
        _data_save()
        with open(UPDATE_STATE_FILE, "w", encoding="utf8") as fh:
            json.dump({"from": APP_VERSION, "to": man["version"], "prev": prev, "ts": now(), "boots": 0, "pending": True}, fh)
        os.replace(tmp, SCRIPT_PATH)
    except Exception as e:
        log.exception("update failed")
        await edit_q(q, f"❌ <b>Update rejected</b>\n\n{esc(e)}\n\nNothing was changed.", _admin_markup_back("admin_backup"))
        return
    await edit_q(q, f"✅ Update to <b>{esc(man['version'])}</b> verified and installed. Restarting now…", None)
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def updater_boot_check():
    """Called first thing in main(): count boots of a freshly updated file; roll back after repeated failures."""
    try:
        with open(UPDATE_STATE_FILE, encoding="utf8") as fh:
            st = json.load(fh)
    except Exception:
        return None
    if not st.get("pending"):
        return None
    st["boots"] = st.get("boots", 0) + 1
    if st["boots"] > 3 and os.path.exists(st.get("prev", "")):
        log.error("updated version failed to start — rolling back to %s", st["from"])
        shutil.copy(st["prev"], SCRIPT_PATH)
        st.update(pending=False, rolled_back=True)
        with open(UPDATE_STATE_FILE, "w", encoding="utf8") as fh:
            json.dump(st, fh)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    with open(UPDATE_STATE_FILE, "w", encoding="utf8") as fh:
        json.dump(st, fh)
    return st


async def updater_confirm_healthy(bot, st):
    """After 90 s of healthy running, the new version is confirmed and the owner is told."""
    await asyncio.sleep(90)
    try:
        st["pending"] = False
        with open(UPDATE_STATE_FILE, "w", encoding="utf8") as fh:
            json.dump(st, fh)
        await notify_admin(bot, f"✅ Updated {st.get('from')} → {st.get('to')} and running normally.")
    except Exception:
        log.exception("update confirmation failed")

# ═══════════════════════════════════════════════════════════════════════════════
# 🚦 ROUTING, STARTUP / SHUTDOWN, MAIN
# ═══════════════════════════════════════════════════════════════════════════════
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    reset_flows(uid)
    checkout.pop(uid, None)
    await ui_clean(context.bot, update.effective_chat.id, uid)
    await send_main_menu(update, context)


# state -> handler for plain text input
TEXT_STATES = {
    ADMIN_WAIT_POST_ID: admin_add_price, ADMIN_WAIT_PRICE: admin_add_creds, ADMIN_WAIT_CREDS: admin_add_photos_prompt,
    ADMIN_WAIT_INFO: admin_info_in, ADMIN_EDIT_VALUE: admin_edit_value,
    ADMIN_FJ_ADD: fj_add_finish, ADMIN_BAN_ID: usr_id_finish, ADMIN_UNBAN_ID: usr_id_finish,
    ADMIN_SA_ADD: sa_add_finish, ADMIN_BC_ADD: bc_add_finish, ADMIN_SET_ICON: seticon_finish,
    ADMIN_PROMO_WIZ: pw_in, ADMIN_PROMO_EDIT: pr_edit_in, ADMIN_BAL_ADJUST: admin_wallet_in,
    ADMIN_SELL_OFFER: sr_offer_in, ADMIN_SELL_LIST: sr_list_in, ADMIN_CHANNEL_SET: ch_set_finish,
    ADMIN_BACKUP_CHAT: bk_chat_in, ADMIN_AP_INTERVAL: ap_interval_in, ADMIN_BK_INTERVAL: bk_interval_in,
    BUYER_WAIT_POST_ID: buyer_post_id, BUYER_WAIT_PROMO: promo_input, BUYER_WAIT_TOPUP: topup_amount_in,
    SELL_PLATFORM: sell_platform_text, SELL_LOGIN: sell_login_in, SELL_PASS: sell_pass_in,
}
# states that wait for something other than text -> short reminder
HINT_STATES = {
    ADMIN_WAIT_PHOTOS: "📷 Please send photos, or tap the Done / Skip button above.",
    ADMIN_EDIT_PHOTOS: "📷 Please send photos, or tap the Done button above.",
    SELL_PHOTOS: "📷 Please send your ID photos (5–20), then tap Done.",
    SELL_CONFIRM: "Please tap Submit or Cancel above.",
    ADMIN_RESTORE_FILE: "Please send the backup file as a document.",
}


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes text messages: reply-keyboard buttons first, then the input state machine."""
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    key = MENU_KEYS.get(_btn_key(text)) if 0 < len(text) <= 22 else None
    if key:
        if key == "admin":
            if is_admin(uid):
                await admin_panel(update, context)
            return
        reset_flows(uid)
        checkout.pop(uid, None)
        await {"buy": buy_start, "sell": sell_start, "account": account_cmd,
               "orders": orders_cmd, "topup": topup_cmd}[key](update, context)
        return

    state = user_state.get(uid)
    handler = TEXT_STATES.get(state)
    if handler:
        await handler(update, context)
    elif state in HINT_STATES:
        await send_temp(context.bot, update.effective_chat.id, uid, HINT_STATES[state])
    else:
        await send_main_menu(update, context)


async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await sell_photo_in(update, context):
        return
    await admin_photo_in(update, context)


async def doc_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await restore_doc_in(update, context)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("handler error", exc_info=context.error)


async def _shutdown(app: Application):
    log.info("shutting down — saving data")
    for t in list(_tasks) + list(_timers.values()) + list(_pollers.values()):
        t.cancel()
    _db_flush(force=True)
    _data_save()
    if _http is not None and not _http.closed:
        await _http.close()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


_UPDATE_ST = None


async def _on_startup(app: Application):
    bot = app.bot
    _fj_load()
    _data_load()
    _db_load()
    for tok in re.split(r"[\s,]+", os.getenv("SUPER_ADMINS", "")):
        if tok.isdigit() and int(tok) != ADMIN_ID:
            super_admins.setdefault(int(tok), tok)
    _data_save()
    try:
        vres = await _validate_premium_ids(bot)
    except Exception:
        log.exception("premium id validation failed")
        vres = None
    try:
        _resolve_admin_icons()
    except Exception:
        log.exception("admin icon resolve failed")
    for tok in re.split(r"[\s,]+", os.getenv("FORCE_JOIN", "")):
        if not tok:
            continue
        try:
            e = await _fj_resolve(bot, tok)
            if not any(x["id"] == e["id"] for x in force_join):
                force_join.append(e)
        except Exception as exc:
            log.warning("FORCE_JOIN env %s skipped: %s", tok, exc)
    _fj_save()

    # startup recovery: finish interrupted deliveries and resume watching unpaid orders
    try:
        await recover_pending_orders(bot)
    except Exception:
        log.exception("recovery failed")

    _spawn(_autosave_loop())
    _spawn(auto_promo_loop(bot))
    _spawn(backup_loop(bot))
    if _UPDATE_ST:
        _spawn(updater_confirm_healthy(bot, _UPDATE_ST))

    bk = settings["backup"]
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🚀 <b>FF Store Bot started</b> — v{APP_VERSION}\n\n"
                f"🟢 Listings: <code>{len(posts)}</code> · sold: <code>{len(sold)}</code>\n"
                f"⏳ Pending orders resumed: <code>{len(pending_orders)}</code>\n"
                f"💼 Wallets: <code>{len(balances)}</code> · promo codes: <code>{len(promos)}</code>\n"
                f"📢 Force join: <code>{len(force_join)}</code> · 📣 broadcast chats: <code>{len(bc_chats)}</code>\n"
                f"💾 Backup group: <b>{'set' if bk.get('chat') else 'NOT set — send /setbackup in your private group'}</b>"
                f" · credentials in backups: <b>{'encrypted' if _fernet() else 'excluded (set BACKUP_KEY)'}</b>\n"
                f"🧩 PTB <code>{_ptb_ver()}</code> — buttons: <code>{_btn_mode()}</code>\n"
                f"🎨 {_icon_report()}\n"
                + (f"🎯 Premium IDs valid: <code>{vres[0]}/{vres[1]}</code> | emoji matched: <code>{vres[2]}</code>\n"
                   + (f"⚠️ No glyph match: {' '.join(vres[3])}\n" if vres[3] else "")
                   if vres else "⚠️ Premium ID check could not run\n")
                + "💎 Payment polling: every 2 seconds\n🔧 /checkorder &lt;id&gt; — force verify an order"
            ),
            parse_mode="HTML",
        )
    except Exception:
        log.warning("startup notification failed")


def _install_answer_guard():
    """A second answer() on the same callback query must never crash a handler."""
    from telegram import CallbackQuery
    orig = CallbackQuery.answer

    async def answer(self, *a, **k):
        try:
            return await orig(self, *a, **k)
        except tge.BadRequest as e:
            log.debug("callback answer ignored: %s", e)
            return False
    CallbackQuery.answer = answer


def main():
    global _UPDATE_ST
    _install_answer_guard()
    _UPDATE_ST = updater_boot_check()
    _install_premium_patch()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_on_startup)
        .post_shutdown(_shutdown)
        .build()
    )

    private = filters.ChatType.PRIVATE
    groups = filters.ChatType.GROUPS
    A = app.add_handler
    CB = CallbackQueryHandler

    # every private update: track the user + ban check (first)
    A(MessageHandler(private & filters.ALL, track_user), group=-2)
    A(CB(track_user), group=-2)

    # commands
    A(CommandHandler("start", start, filters=private))
    A(CommandHandler("start", group_start, filters=groups))
    A(CommandHandler("cancel", cmd_cancel, filters=private))
    A(CommandHandler("checkorder", cmd_checkorder, filters=private))
    A(CommandHandler("orders", cmd_orders, filters=private))
    A(CommandHandler("seticon", cmd_seticon, filters=private))
    A(CommandHandler("setbackup", cmd_setbackup))

    # navigation / account
    A(CB(main_menu_cb, pattern=r"^main_menu$"))
    A(CB(buy_start, pattern=r"^buy_start$"))
    A(CB(topup_cmd, pattern=r"^tp_menu$"))
    A(CB(acct_orders_cb, pattern=r"^acct_orders$"))
    A(CB(acct_hist_cb, pattern=r"^acct_hist$"))
    A(CB(inv_view_cb, pattern=r"^inv_"))
    A(CB(noop_cb, pattern=r"^noop$"))
    A(CB(in_cancel_cb, pattern=r"^in_cancel$"))
    A(CB(fj_check_cb, pattern=r"^fj_check$"))

    # catalogue / checkout / payment
    A(CB(cat_nav_cb, pattern=r"^cat_[pn]_"))
    A(CB(cat_img_cb, pattern=r"^cat_i_"))
    A(CB(cat_buy_cb, pattern=r"^cat_buy_"))
    A(CB(cat_close_cb, pattern=r"^cat_close$"))
    A(CB(cat_enter_cb, pattern=r"^cat_enter$"))
    A(CB(co_upi_cb, pattern=r"^co_upi$"))
    A(CB(co_bal_cb, pattern=r"^co_bal$"))
    A(CB(co_promo_cb, pattern=r"^co_promo$"))
    A(CB(co_nopromo_cb, pattern=r"^co_nopromo$"))
    A(CB(co_back_cb, pattern=r"^co_back$"))
    A(CB(cancel_order, pattern=r"^cancel_"))
    A(CB(tp_amt_cb, pattern=r"^tp_a_"))
    A(CB(tp_custom_cb, pattern=r"^tp_custom$"))

    # sell flow (user)
    A(CB(sell_done_cb, pattern=r"^sl_done$"))
    A(CB(sell_platform_cb, pattern=r"^sl_p_"))
    A(CB(sell_cancel_cb, pattern=r"^sl_cancel$"))
    A(CB(sell_submit_cb, pattern=r"^sl_submit$"))
    A(CB(sell_my_cb, pattern=r"^sl_my$"))
    A(CB(sell_answer_cb, pattern=r"^sl_(acc|dec)_"))

    # admin: panel, posts, orders
    A(CB(admin_panel, pattern=r"^admin_panel$"))
    A(CB(admin_orders_cb, pattern=r"^admin_orders$"))
    A(CB(admin_check_cb, pattern=r"^ao_chk_"))
    A(CB(admin_add_start, pattern=r"^admin_add$"))
    A(CB(admin_autoid_cb, pattern=r"^adm_autoid$"))
    A(CB(admin_photos_done, pattern=r"^adp_done$"))
    A(CB(admin_info_skip, pattern=r"^adi_skip$"))
    A(CB(admin_view, pattern=r"^admin_view$"))
    A(CB(admin_delete_list, pattern=r"^admin_delete_list$"))
    A(CB(admin_delete_confirm, pattern=r"^del_"))
    A(CB(admin_edit_list, pattern=r"^admin_edit_list$"))
    A(CB(admin_edit_select, pattern=r"^edit_"))
    A(CB(admin_edit_field, pattern=r"^editfield_"))
    # admin: users / force join
    A(CB(admin_users, pattern=r"^(admin_users|usr_p_\d+)$"))
    A(CB(usr_toggle, pattern=r"^usr_t_\d+_\d+$"))
    A(CB(usr_id_start, pattern=r"^usr_(ban|unban)id$"))
    A(CB(sa_menu, pattern=r"^sa_menu$"))
    A(CB(sa_add_start, pattern=r"^sa_add$"))
    A(CB(sa_del_cb, pattern=r"^sa_del_\d+$"))
    A(CB(admin_fj, pattern=r"^admin_fj$"))
    A(CB(fj_add_start, pattern=r"^fj_add$"))
    A(CB(fj_del_cb, pattern=r"^fjdel_"))
    # admin: broadcast + auto promotion
    A(CB(admin_bc, pattern=r"^admin_bc$"))
    A(CB(bc_send_cb, pattern=r"^bc_send$"))
    A(CB(bc_go_cb, pattern=r"^bc_go$"))
    A(CB(bc_preview_cb, pattern=r"^bc_preview$"))
    A(CB(bc_add_start, pattern=r"^bc_add$"))
    A(CB(bc_import_cb, pattern=r"^bc_import$"))
    A(CB(bc_refresh_cb, pattern=r"^bc_refresh$"))
    A(CB(bc_rmlist_cb, pattern=r"^bc_rmlist$"))
    A(CB(bc_rm_cb, pattern=r"^bc_rm_-?\d+$"))
    A(CB(ap_menu, pattern=r"^ap_menu$"))
    A(CB(ap_toggle, pattern=r"^ap_toggle$"))
    A(CB(ap_interval, pattern=r"^ap_interval$"))
    A(CB(ap_chats, pattern=r"^ap_chats$"))
    A(CB(ap_chat_toggle, pattern=r"^ap_c_-?\d+$"))
    A(CB(ap_now, pattern=r"^ap_now$"))
    A(ChatMemberHandler(track_bot_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    # admin: promo codes / wallet / sell requests / channel / backup
    A(CB(admin_promos, pattern=r"^admin_promos$"))
    A(CB(pr_new, pattern=r"^pr_new$"))
    A(CB(pr_view, pattern=r"^pr_v_"))
    A(CB(pr_toggle, pattern=r"^pr_t_"))
    A(CB(pr_delete, pattern=r"^pr_d_"))
    A(CB(pr_delete_yes, pattern=r"^pr_dd_"))
    A(CB(pr_edit_start, pattern=r"^pr_e_"))
    A(CB(pw_type, pattern=r"^pw_t_(percent|flat)$"))
    A(CB(pw_skip, pattern=r"^pw_skip$"))
    A(CB(pw_auto, pattern=r"^pw_auto$"))
    A(CB(admin_wallet, pattern=r"^admin_wallet$"))
    A(CB(admin_sells, pattern=r"^admin_sells$"))
    A(CB(sr_view, pattern=r"^sr_v_"))
    A(CB(sr_photos, pattern=r"^sr_ph_"))
    A(CB(sr_offer_start, pattern=r"^sr_offer_"))
    A(CB(sr_reject, pattern=r"^sr_rej_"))
    A(CB(sr_list_start, pattern=r"^sr_list_"))
    A(CB(admin_channel, pattern=r"^admin_channel$"))
    A(CB(ch_set_start, pattern=r"^ch_set$"))
    A(CB(ch_rm_cb, pattern=r"^ch_rm$"))
    A(CB(ch_postall_cb, pattern=r"^ch_postall$"))
    A(CB(admin_backup, pattern=r"^admin_backup$"))
    A(CB(bk_now, pattern=r"^bk_now$"))
    A(CB(bk_report, pattern=r"^bk_report$"))
    A(CB(bk_download, pattern=r"^bk_dl$"))
    A(CB(bk_toggle, pattern=r"^bk_toggle$"))
    A(CB(bk_interval, pattern=r"^bk_interval$"))
    A(CB(bk_setchat, pattern=r"^bk_setchat$"))
    A(CB(bk_restore, pattern=r"^bk_restore$"))
    A(CB(bk_restore_yes, pattern=r"^bk_restore_yes$"))
    A(CB(bk_update, pattern=r"^bk_update$"))
    A(CB(bk_update_yes, pattern=r"^bk_update_yes$"))

    # messages
    A(MessageHandler(private & filters.PHOTO, photo_router))
    A(MessageHandler(private & filters.Document.ALL, doc_router))
    A(MessageHandler(filters.TEXT & ~filters.COMMAND & private, message_router))

    app.add_error_handler(on_error)
    log.info("🚀 FF Store Bot v%s starting — payment poll every %ds", APP_VERSION, POLL_INTERVAL)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
