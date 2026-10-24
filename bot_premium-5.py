"""FF ID Store Bot — Telegram + FamGateway UPI auto-payment.

Changes vs bot__1_.py:
- Payment verification fixed: UTR no longer blocks delivery (marked N/A if absent)
- Immediate first poll right after order creation (no 3s wait)
- POLL_INTERVAL reduced to 2s for faster confirmation
- Colored reply-keyboard buttons ported from app.py (green/blue/red)
- Admin /checkorder <id> command for manual force-verify
- Admin /orders command to list all pending orders
- Better payment logging with full response dump on first paid signal
- Startup admin notification
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import random
import re
import re as _re
import time

import aiohttp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ["ADMIN_ID"])
FG_API_KEY  = os.environ["FG_API_KEY"]
FG_BASE_URL = "https://famgateway.in/api"

POLL_INTERVAL   = 2          # seconds between each status check
POLL_MAX_SECONDS = 360       # gateway order lives 300s; small grace on top

LOGO_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
LOGO_RATIO = 0.22

# Broadcast promo me dikhne wala "orders completed" count. SIRF wahi number likho jo sach ho.
# Railway env me ORDERS_COMPLETED="300+" (khali ya "0" rakhoge to ye line message me nahi aayegi).
BC_ORDERS  = os.getenv("ORDERS_COMPLETED", "300+")
BC_DELAY   = 0.6             # seconds between two broadcast sends (rate-limit safe)
# Force-join list yaha save hoti hai. Railway pe redeploy ke baad bhi bachi rahe iske liye
# Railway Volume mount karke DATA_DIR env set karo, ya FORCE_JOIN env me channels likh do.
_DATA_DIR  = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
FJ_FILE    = os.path.join(_DATA_DIR, "forcejoin.json")
DATA_FILE  = os.path.join(_DATA_DIR, "botdata.json")   # users + ban + super admins
BOT_NAME   = "𝘾𝘼𝙋𝙏𝘼𝙄𝙉 𝙓 𝙎𝙏𝙊𝙍𝙀"

# ─── STATES ────────────────────────────────────────────────────────────────────
ADMIN_WAIT_POST_ID = "ADMIN_WAIT_POST_ID"
ADMIN_WAIT_PRICE   = "ADMIN_WAIT_PRICE"
ADMIN_WAIT_CREDS   = "ADMIN_WAIT_CREDS"
ADMIN_EDIT_VALUE   = "ADMIN_EDIT_VALUE"
BUYER_WAIT_POST_ID = "BUYER_WAIT_POST_ID"
ADMIN_FJ_ADD       = "ADMIN_FJ_ADD"
ADMIN_BAN_ID       = "ADMIN_BAN_ID"
ADMIN_UNBAN_ID     = "ADMIN_UNBAN_ID"
ADMIN_SA_ADD       = "ADMIN_SA_ADD"
ADMIN_BC_ADD       = "ADMIN_BC_ADD"
ADMIN_SET_ICON     = "ADMIN_SET_ICON"

# ─── IN-MEMORY STORE ───────────────────────────────────────────────────────────
posts: dict         = {}   # {post_id: {"price": float, "creds": str}}
sold: set           = set()
reserved: dict      = {}   # {post_id: order_id | "creating"}
pending_orders: dict = {}  # {order_id: {"user_id","post_id","chat_id","msg_id"}}

user_state: dict  = {}
admin_temp: dict  = {}
users: dict       = {}     # {uid: {"name","username","banned"}}
super_admins: dict = {}   # {uid: name}
force_join: list  = []     # [{"id": int, "title": str, "link": str}]
_last_seen: dict  = {}     # {order_id: (pub_json, auth_json)} — log de-dup
_tasks: set       = set()
_http = None
bc_chats: dict    = {}     # {chat_id: {"title","type","link"}}  — broadcast destinations (persistent)
_bc_busy: bool    = False  # ek time pe sirf ek broadcast
_bc_last: dict    = {}     # last broadcast summary (memory only)


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
    s = f"Admin icons animated: `{len(anim)}/{len(ICON_KEYS)}`"
    if rest:
        s += f" (default/static: {', '.join(rest)} — /seticon se set karo)"
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
                                    text=f"⚠️ Premium emoji send fail ({name}):\n{err}\n\nUser ko normal message gaya.")
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
    """Bottom colored keyboard for private chat.

    Green  = buy action
    Blue   = navigation / info
    Red    = admin-only danger action
    """
    rows = [
        [style_btn("🛒 Buy FF ID", "success")],
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


def user_pending(uid: int):
    for oid, info in pending_orders.items():
        if info["user_id"] == uid:
            return oid
    return None


def release_order(order_id: str):
    info = pending_orders.pop(order_id, None)
    _last_seen.pop(order_id, None)
    if info and reserved.get(info["post_id"]) == order_id:
        reserved.pop(info["post_id"], None)
    return info


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    uid = update.effective_user.id
    u   = update.effective_user
    who = f"@{u.username}" if u.username else (u.first_name or "User")
    text = (
        f"🎮 <b>WELCOME {html.escape(who)}</b>\n"
        f"TO [ <b>{BOT_NAME}</b> ] BOT\n\n"
        "Apna kaam select karo:\n\n"
        "🛒 <b>Buy FF ID</b> — ID khareedne ke liye\n"
    )
    if is_admin(uid):
        text += "⚙️ <b>Admin Panel</b> — Posts manage karne ke liye\n"

    reply_kb = main_reply_kb(uid)

    if edit and update.callback_query:
        # Edit existing inline message and re-show the reply keyboard
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML"
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="👇 Neeche se select karo:",
            reply_markup=reply_kb,
        )
    elif update.message:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=text, parse_mode="HTML",
                reply_markup=reply_kb, message_effect_id=MESSAGE_EFFECT_ID,
            )
        except Exception:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_kb)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="👇 Neeche se select karo:",
            reply_markup=reply_kb,
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


# ─── START ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state.pop(update.effective_user.id, None)
    if await fj_gate(update, context):
        return
    await send_main_menu(update, context)


async def group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = context.bot.username
    if not username or not update.message:
        return
    await update.message.reply_text(
        "🛒 ID khareedne ke liye bot ko private me kholo 👇",
        reply_markup=InlineKeyboardMarkup(
            [[ibt_url("Open Bot", f"https://t.me/{username}")]]
        ),
    )


# ─── ADMIN PANEL ──────────────────────────────────────────────────────────────
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

    n_reserved = len([p for p in posts if p in reserved])
    n_avail    = len(posts) - n_reserved
    text = (
        "⚙️ *Admin Panel*\n\n"
        f"🟢 Available: *{n_avail}*\n"
        f"🟡 Payment chal raha: *{n_reserved}*\n"
        f"✅ Sold: *{len(sold)}*\n"
        f"📢 Force Join: *{len(force_join)}*\n"
        f"👥 Users: *{len(users)}*\n"
        f"📣 Broadcast Chats: *{len(bc_chats)}*\n\n"
        "Kya karna hai?"
    )
    # Delete / Orders / Force Join / Users / Broadcast: _icon = animated premium emoji (startup pe auto-pick)
    buttons = [
        [_inline_btn("➕ Add New Post", _sc=False, callback_data="admin_add")],
        [_inline_btn("📋 View All Posts", _sc=False, callback_data="admin_view")],
        [_inline_btn("✏️ Edit Post", _sc=False, callback_data="admin_edit_list")],
        [_inline_btn("🗑️ Delete Post", _sc=False, callback_data="admin_delete_list", _icon=_adm_icon("delete"))],
        [_inline_btn("📦 Pending Orders", _sc=False, callback_data="admin_orders", _icon=_adm_icon("orders"))],
        [_inline_btn("📢 Force Join", _sc=False, callback_data="admin_fj", _icon=_adm_icon("forcejoin"))],
        [_inline_btn("👥 Manage Users", _sc=False, callback_data="admin_users", _icon=_adm_icon("users"))],
        [_inline_btn("Broadcast", _sc=False, callback_data="admin_bc", _icon=_adm_icon("broadcast"))],
        [_inline_btn("🔙 Back", _sc=False, callback_data="main_menu")],
    ]
    markup = InlineKeyboardMarkup(buttons)

    if query:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)




# ─── USERS / BAN / SUPER ADMIN ────────────────────────────────────────────────
def _data_save():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf8") as fh:
            json.dump({"users": {str(k): v for k, v in users.items()},
                       "super_admins": {str(k): v for k, v in super_admins.items()},
                       "icons": dict(_ICON_OVERRIDES),
                       "bc_chats": {str(k): v for k, v in bc_chats.items()}},
                      fh, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)      # atomic: beech me crash ho to purana data safe
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


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har private update pe user save karta hai + banned user ko rok deta hai."""
    u = update.effective_user
    if not u or u.is_bot:
        return
    name, uname = u.full_name or "", u.username or ""
    rec = users.get(u.id)
    if rec is None:
        users[u.id] = {"name": name, "username": uname, "banned": False}
        _data_save()
    elif rec.get("name") != name or rec.get("username") != uname:
        rec["name"], rec["username"] = name, uname
        _data_save()
    if users[u.id].get("banned") and not is_admin(u.id):
        try:
            if update.callback_query:
                await update.callback_query.answer("🚫 Aapko ban kar diya gaya hai.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🚫 Aapko is bot se ban kar diya gaya hai.")
        except Exception:
            pass
        raise ApplicationHandlerStop


USERS_PER_PAGE = 6


async def _render_users(q, page: int):
    items = list(users.items())[::-1]            # naye users upar
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
        + ("\n".join(lines) if lines else "Abhi koi user nahi.")
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
        await q.answer("Admin ko ban nahi kar sakte.", show_alert=True)
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
        ("🚫 <b>Ban by ID</b>" if ban else "✅ <b>Unban by ID</b>") + "\n\nUser ki <b>chat ID</b> bhejo (sirf number).",
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
        await update.message.reply_text("❌ Sirf chat ID number bhejo (jaise 123456789).",
                                        reply_markup=InlineKeyboardMarkup(back_button("admin_users")))
        return
    tid, ban = int(raw), state == ADMIN_BAN_ID
    if tid == ADMIN_ID or tid in super_admins:
        await update.message.reply_text("❌ Admin ko ban nahi kar sakte.",
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
                      for i, (k, n) in enumerate(super_admins.items(), 1)) or "Koi nahi"
    text = (
        "👑 <b>Super Admins</b>\n\n"
        f"Owner: <code>{ADMIN_ID}</code>\n\n{lines}\n\n"
        "Super admin bot me ID add/edit/delete, orders, users aur force join manage kar sakta hai."
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
        await q.answer("Sirf owner ke liye.", show_alert=True)
        return
    await q.answer()
    user_state.pop(q.from_user.id, None)
    await _render_sa(q)


async def sa_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Sirf owner ke liye.", show_alert=True)
        return
    await q.answer()
    user_state[q.from_user.id] = ADMIN_SA_ADD
    await q.edit_message_text(
        "➕ <b>Super Admin Add</b>\n\nJise banana hai uski <b>chat ID</b> bhejo (sirf number).\n"
        "Behtar hai wo pehle bot ko /start kar le.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("sa_menu")),
    )


async def sa_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid) or user_state.get(uid) != ADMIN_SA_ADD:
        return
    raw = (update.message.text or "").strip()
    if not re.fullmatch(r"\d{5,}", raw):
        await update.message.reply_text("❌ Sirf chat ID number bhejo.",
                                        reply_markup=InlineKeyboardMarkup(back_button("sa_menu")))
        return
    tid = int(raw)
    if tid == ADMIN_ID or tid in super_admins:
        await update.message.reply_text("ℹ️ Ye pehle se admin hai.",
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
        await context.bot.send_message(tid, "👑 Aapko is bot ka Super Admin bana diya gaya hai. /start dabao.")
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
        await q.answer("Sirf owner ke liye.", show_alert=True)
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
    """@username / t.me link / -100 ID -> {"id","title","link"}. Galat ho to ValueError."""
    raw = raw.strip()
    m = re.match(r"^(?:https?://)?t\.me/(?!\+|joinchat)([A-Za-z0-9_]{4,})/?$", raw)
    if m:
        ident = "@" + m.group(1)
    elif raw.startswith("@") and len(raw) > 4:
        ident = raw
    elif re.fullmatch(r"-?\d{6,}", raw):
        ident = int(raw)
    else:
        raise ValueError(f"{raw} — format galat. @username, t.me link ya -100... ID bhejo.")
    try:
        chat = await bot.get_chat(ident)
    except Exception:
        raise ValueError(f"{raw} — chat nahi mili. Bot ko pehle wahan admin banao.")
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        is_adm = me.status in ("administrator", "creator")
    except Exception:
        is_adm = False
    if not is_adm:
        raise ValueError(f"{raw} — bot wahan admin nahi hai. Pehle admin banao, phir dobara bhejo.")
    link = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except Exception:
            raise ValueError(f"{raw} — invite link nahi ban payi. Bot ko 'Invite users' permission do.")
    return {"id": chat.id, "title": chat.title or str(chat.id), "link": link}


async def fj_missing(bot, uid: int) -> list:
    """Wo force-join chats jinme user abhi member nahi hai."""
    missing = []
    for e in force_join:
        try:
            mem = await bot.get_chat_member(e["id"], uid)
            if mem.status in ("left", "kicked") or (
                mem.status == "restricted" and not getattr(mem, "is_member", True)
            ):
                missing.append(e)
        except Exception as exc:  # bot hata diya gaya / permission nahi -> user ko block mat karo
            log.warning("force-join check failed for %s: %s", e.get("id"), exc)
    return missing


async def fj_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True = user blocked (join message bhej diya). Admin hamesha pass."""
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
        "📢 *Pehle join karo*\n\n"
        "Bot use karne ke liye neeche diye channel/group join karo, "
        "phir *Joined — Check* dabao."
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
        await q.answer("❌ Abhi sab join nahi kiya!", show_alert=True)
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
    lines = "\n".join(f"{i}. {_clean_md(e['title'])}" for i, e in enumerate(force_join, 1)) or "_Koi nahi_"
    text = (
        "📢 *Force Join*\n\n"
        f"Total: *{len(force_join)}*\n\n{lines}\n\n"
        "Delete karne ke liye 🗑️ dabao."
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
        "➕ *Channel/Group Add*\n\n"
        "Pehle bot ko us channel/group me *admin* banao.\n\n"
        "Phir bhejo (ek ya zyada, space/new line se alag):\n"
        "• `@username`\n• `https://t.me/username`\n• `-1001234567890` (private ke liye ID)",
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
            failed.append(f"{_clean_md(e['title'])} — pehle se added hai")
            continue
        force_join.append(e)
        added.append(_clean_md(e["title"]))
    _fj_save()
    text = ""
    if added:
        text += "✅ *Added:*\n" + "\n".join(f"• {t}" for t in added) + "\n\n"
    if failed:
        text += "❌ *Fail:*\n" + "\n".join(f"• {_clean_md(t)}" for t in failed) + "\n\n"
    if added:
        user_state.pop(uid, None)
    else:
        text += "Dobara bhejo ya Back dabao."
    await update.message.reply_text(
        text.strip() or "Kuch nahi mila — @username ya link bhejo.",
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
    """Custom emoji ID ko Telegram se verify karke save karta hai. Returns HTML reply."""
    try:
        stk = await bot.get_custom_emoji_stickers([eid])
    except Exception as e:
        return f"❌ Telegram se verify nahi ho paya: <code>{html.escape(repr(e)[:150])}</code>"
    if not stk:
        return "❌ Ye custom emoji ID valid nahi hai."
    st   = stk[0]
    anim = bool(getattr(st, "is_animated", False) or getattr(st, "is_video", False))
    _STICKER_META[eid] = ((st.emoji or "").replace("️", ""), anim)
    _INVALID_IDS.discard(eid)
    _VALID_IDS.add(eid)
    _ICON_OVERRIDES[key] = eid
    _data_save()
    _resolve_admin_icons()
    return (f"✅ <b>{html.escape(key)}</b> icon set: <code>{html.escape(eid)}</code> "
            f"({'animated' if anim else 'static — animated nahi hai'})\n\n"
            "Admin Panel dobara kholo.")


async def cmd_seticon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: /seticon [key] [emoji_id|reset] — admin button ka premium icon set karo."""
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
            "<code>/seticon delete</code> — phir koi premium emoji bhejo\n"
            "<code>/seticon delete 5188191464160523568</code> — direct ID\n"
            "<code>/seticon delete reset</code> — auto-pick pe wapas\n\n"
            f"Keys: {keys_txt}",
            parse_mode="HTML",
        )
        return
    key = args[0].lower()
    if key not in ADMIN_ICON_SPECS:
        await update.message.reply_text(f"❌ Galat key. Keys: {keys_txt}", parse_mode="HTML")
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
            await update.message.reply_text("❌ Emoji ID sirf number hoti hai.")
            return
        await update.message.reply_text(await _icon_set(context.bot, key, val), parse_mode="HTML")
        return
    user_state[uid] = ADMIN_SET_ICON
    admin_temp[uid] = {"icon_key": key}
    await update.message.reply_text(
        f"🎨 <b>{key}</b> ke liye ab ek <b>premium emoji</b> bhejo (ya uski ID).",
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
        await update.message.reply_text("❌ Koi premium emoji (ya uski ID) bhejo.")
        return
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)
    await update.message.reply_text(await _icon_set(context.bot, key, eid), parse_mode="HTML")


# ─── ADMIN: BROADCAST ─────────────────────────────────────────────────────────
# Telegram Bot API me "bot kin kin channel/group me admin hai" ki list nahi milti. Isliye:
#   1) my_chat_member updates se jaise hi bot kahin admin banta hai, chat apne aap registry me aati hai
#   2) pehle se admin ho to ➕ Add Chat (@username / link / -100 ID) ya 📢 Import Force Join
#   3) 🔄 Refresh se purani/dead chats hat jati hain
_BC_GONE = ("inaccessible", "bot admin nahi hai")


def _bc_text() -> str:
    """Promo message (HTML). Plain emojis hi rakhe hain — send_message patch inhe premium bana deta hai
    aur pehli line (title) ko ➿ blue-line frame me daal deta hai."""
    orders = (BC_ORDERS or "").strip()
    loop = "➿" * 11
    lines = [
        f"<b>{BOT_NAME}</b>",
        "",
        loop,
        "",
        "💎 <b>Free Fire ID Marketplace Bot</b>",
        "",
        "🎮 Is bot se aap ye sab kar sakte ho:",
        "",
        "🛒 Available Free Fire IDs browse karo",
        "⚡ Auto UPI payment — QR scan karo, payment verify hote hi ID mil jaati hai",
        "🔐 Credentials seedha bot me delivery",
        "📋 Post ID bhejo aur order karo — sab kuch bot me hi",
    ]
    if orders and orders != "0":
        lines += ["", f"📦 <b>{html.escape(orders)} Orders Completed!</b>"]
    lines += [
        "",
        loop,
        "",
        "🚀 <b>Neeche button dabao aur bot start karo!</b>",
        "",
        f"✨ <b>{BOT_NAME}</b>",
    ]
    return "\n".join(lines)


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
    raise ValueError(f"{raw} — format galat. @username, t.me link ya -100... ID bhejo.")


async def _safe_edit(q, text: str, markup=None):
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except tge.BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _bc_check(bot, chat_id):
    """(ok, reason, chat). Bot ka current membership + posting permission check karta hai."""
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
        return False, "rate limit — baad me try karo", chat
    if chat.type not in ("group", "supergroup", "channel"):
        return False, "group/channel nahi hai", chat
    if me.status == "creator":
        return True, "", chat
    if me.status != "administrator":
        return False, "bot admin nahi hai", chat
    if chat.type == "channel" and not getattr(me, "can_post_messages", False):
        return False, "post permission nahi hai", chat
    return True, "", chat


async def _bc_send_one(bot, chat_id, text: str, markup):
    """Returns (result, info): ok | failed | unknown | migrated.
    Retry SIRF RetryAfter pe (request reject hui thi, isliye duplicate nahi banega).
    Timeout/network error pe retry nahi — message pahunch chuka ho sakta hai."""
    for _ in range(3):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
            return "ok", ""
        except tge.RetryAfter as e:
            await asyncio.sleep(_ra_seconds(e) + 1)
        except tge.ChatMigrated as e:
            return "migrated", str(e.new_chat_id)
        except tge.Forbidden as e:
            return "failed", f"blocked / permission nahi: {str(e)[:80]}"
        except tge.BadRequest as e:
            return "failed", str(e)[:100]
        except tge.TimedOut:
            return "unknown", "timeout — delivery confirm nahi, duplicate se bachne ke liye retry nahi kiya"
        except tge.NetworkError as e:
            return "unknown", f"network error — retry nahi kiya ({str(e)[:60]})"
        except tge.TelegramError as e:
            return "failed", str(e)[:100]
    return "failed", "flood-wait: retries khatam"


def _bc_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_inline_btn("🚀 Send Broadcast", _sc=False, _style="success", callback_data="bc_send")],
        [_inline_btn("📋 Preview Message", _sc=False, _style="primary", callback_data="bc_preview")],
        [_inline_btn("➕ Add Chat", _sc=False, _style="primary", callback_data="bc_add"),
         _inline_btn("🔄 Refresh List", _sc=False, _style="primary", callback_data="bc_refresh")],
        [_inline_btn("📢 Import Force Join", _sc=False, _style="primary", callback_data="bc_import")],
        [_inline_btn("🗑️ Remove Chat", _sc=False, _style="danger", callback_data="bc_rmlist")],
        [_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")],
    ])


async def _render_bc(q, note: str = ""):
    items = list(bc_chats.items())
    lines = [f"• {html.escape(r.get('title') or str(c))[:40]} <i>({html.escape(str(r.get('type', '?')))})</i>"
             for c, r in items[:25]]
    if len(items) > 25:
        lines.append(f"…aur {len(items) - 25} more")
    last = ""
    if _bc_last:
        last = (f"\n\n🕒 Last broadcast: ✅ {_bc_last['ok']} | ⛔ {_bc_last['skipped']} | "
                f"❌ {_bc_last['failed']} | ❓ {_bc_last['unknown']}")
    text = (
        "📣 <b>Broadcast</b>\n\n"
        + (note + "\n\n" if note else "")
        + f"Registered chats: <b>{len(items)}</b>\n\n"
        + ("\n".join(lines) if lines else "<i>Abhi koi chat nahi.</i>")
        + last
        + "\n\n<i>Bot ko jis channel/group me admin banaoge wo apne aap list me aa jayega. "
          "Pehle se admin ho to ➕ Add Chat ya 📢 Import Force Join use karo.</i>"
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
        bc_chats[chat.id] = rec
        _data_save()
        if new:
            await notify_admin(context.bot, f"📣 Broadcast list me add hua: {rec['title']} ({chat.type})")
    elif bc_chats.pop(chat.id, None) is not None:
        _data_save()
        await notify_admin(context.bot, f"📣 Broadcast list se hata: {chat.title or chat.id} (bot ab admin nahi)")


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
    await q.answer("Preview bheja ja raha hai…")
    try:
        await context.bot.send_message(
            chat_id=q.from_user.id, text=_bc_text(), parse_mode="HTML",
            reply_markup=_bc_markup(context.bot),
        )
    except Exception as e:
        await notify_admin(context.bot, f"⚠️ Preview send fail: {e!r}"[:900])


async def bc_send_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    if _bc_busy:
        await q.answer("Broadcast pehle se chal raha hai.", show_alert=True)
        return
    await q.answer()
    if not bc_chats:
        await _render_bc(q, "❌ Koi chat registered nahi hai. Pehle ➕ Add Chat ya Import karo.")
        return
    lines = [f"• {html.escape(r.get('title') or str(c))[:40]}" for c, r in list(bc_chats.items())[:25]]
    if len(bc_chats) > 25:
        lines.append(f"…aur {len(bc_chats) - 25} more")
    await _safe_edit(
        q,
        "🚀 <b>Broadcast Confirm</b>\n\n"
        f"Ye promo message <b>{len(bc_chats)}</b> chat(s) me jayega:\n\n" + "\n".join(lines) + "\n\n"
        "<i>Send se pehle har chat me bot ki admin/post permission check hogi — "
        "jahan permission nahi hogi wo skip hogi.</i>",
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
        await q.answer("Broadcast pehle se chal raha hai.", show_alert=True)
        return
    if not bc_chats:
        await q.answer("Koi chat nahi.", show_alert=True)
        return
    _bc_busy = True            # await se pehle set — double-click se duplicate broadcast nahi banega
    await q.answer("🚀 Broadcast start")
    try:
        await _safe_edit(q, f"⏳ <b>Broadcast chal raha hai…</b>\n\n0/{len(bc_chats)}", None)
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
                if res == "migrated":            # group supergroup ban gaya — message gaya nahi tha
                    try:
                        new_id = int(info)
                        bc_chats[new_id] = bc_chats.pop(cid, rec)
                        res, info = await _bc_send_one(bot, new_id, text, markup)
                    except ValueError:
                        res, info = "failed", "chat migrate hui — ID invalid"
                    if res == "migrated":
                        res, info = "failed", "chat dobara migrate hui"
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
                        text=f"⏳ <b>Broadcast chal raha hai…</b>\n\n{n}/{total}",
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
            out.append(f"…aur {len(items) - limit} more")
        return "\n".join(out)

    parts = [
        "📣 <b>Broadcast Complete</b>\n",
        f"✅ Sent: <b>{len(ok)}</b>",
        f"⛔ Skipped (admin/post permission nahi): <b>{len(skipped)}</b>",
        f"🚫 Inaccessible (bot hata diya gaya?): <b>{len(gone)}</b>",
        f"❌ Failed: <b>{len(failed)}</b>",
        f"❓ Unknown (timeout — retry nahi kiya, duplicate se bachne ke liye): <b>{len(unknown)}</b>",
    ]
    for label, items in (("⛔ Skipped", skipped), ("🚫 Inaccessible", gone),
                         ("❌ Failed", failed), ("❓ Unknown", unknown)):
        if items:
            parts.append(f"\n<b>{label}:</b>\n{fmt(items)}")
    if gone:
        parts.append("\n<i>Dead chats hatane ke liye 🔄 Refresh List dabao.</i>")
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
        "➕ <b>Broadcast Chat Add</b>\n\n"
        "Bot pehle us channel/group me <b>admin</b> ho (channel me post permission ke saath).\n\n"
        "Phir bhejo (ek ya zyada, space/new line se alag):\n"
        "• <code>@username</code>\n• <code>https://t.me/username</code>\n"
        "• <code>-1001234567890</code> (private ke liye ID)",
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
            failed.append(f"{tok} — chat nahi mili (bot wahan admin hai?)")
            continue
        ok, why, chat2 = await _bc_check(context.bot, chat.id)
        title = chat.title or tok
        if not ok:
            failed.append(f"{title} — {why}")
            continue
        if chat.id in bc_chats:
            failed.append(f"{title} — pehle se added hai")
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
        text += "Dobara bhejo ya Back dabao."
    await update.message.reply_text(
        text.strip() or "Kuch nahi mila — @username ya link bhejo.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(back_button("admin_bc")),
    )


async def bc_import_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer("Import ho raha hai…")
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
    note = f"📢 Force Join se <b>{added}</b> chat add hui."
    if skipped:
        note += "\n⛔ Skip:\n" + "\n".join(f"• {s}" for s in skipped[:8])
    await _render_bc(q, note)


async def bc_refresh_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer("🔄 Refresh ho raha hai…")
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
        note += "\n🗑️ Hataya gaya:\n" + "\n".join(f"• {s}" for s in removed[:8])
    if warn:
        note += "\n⚠️ Dhyan do:\n" + "\n".join(f"• {s}" for s in warn[:8])
    await _render_bc(q, note)


async def bc_rmlist_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("No access.", show_alert=True)
        return
    await q.answer()
    if not bc_chats:
        await _render_bc(q, "📭 Hatane ke liye koi chat nahi.")
        return
    rows = []
    for cid, r in list(bc_chats.items())[:40]:
        short = re.sub(r"\s+", " ", r.get("title") or str(cid))[:24]
        rows.append([_inline_btn(f"🗑️ {short}", _sc=False, _style="danger", callback_data=f"bc_rm_{cid}")])
    rows.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_bc")])
    await _safe_edit(q, "🗑️ <b>Konsi chat broadcast list se hatani hai?</b>", InlineKeyboardMarkup(rows))


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
    await q.answer("🗑️ Removed" if rec else "Pehle se nahi thi")
    await _render_bc(q, f"🗑️ Hataya: {html.escape((rec or {}).get('title') or str(cid))}" if rec else "")


# ─── ADMIN: PENDING ORDERS LIST ───────────────────────────────────────────────
async def admin_orders_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    if not pending_orders:
        await query.edit_message_text(
            "📭 Koi pending order nahi hai.",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
        )
        return

    lines = []
    for oid, info in pending_orders.items():
        lines.append(
            f"• `{oid}`\n"
            f"  Post: `{info['post_id']}` | User: `{info['user_id']}`"
        )
    text = "📦 *Pending Orders:*\n\n" + "\n\n".join(lines[:20])
    if len(pending_orders) > 20:
        text += f"\n…aur {len(pending_orders) - 20} more"
    text += "\n\n_/checkorder <order_id> se force verify karo_"

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
    )


# ─── ADMIN: FORCE CHECK ORDER ─────────────────────────────────────────────────
async def cmd_checkorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /checkorder <order_id>
    Manually triggers a payment check for a pending order.
    Useful when the bot missed the automatic confirmation.
    """
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/checkorder <order_id>`\n\n"
            "Active orders:\n" +
            ("\n".join(f"`{oid}`" for oid in list(pending_orders)[:20]) or "_koi nahi_"),
            parse_mode="Markdown",
        )
        return

    order_id = args[0].strip()
    if order_id not in pending_orders:
        await update.message.reply_text(
            f"❌ Order `{order_id}` pending list me nahi hai.\n"
            "Ya to deliver ho gaya, expire ho gaya, ya cancel ho gaya.",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text(f"🔍 Checking order `{order_id}`…", parse_mode="Markdown")
    try:
        status, data = await fg_status(order_id)
    except Exception as e:
        await msg.edit_text(f"❌ FamGateway error: `{e!r}`", parse_mode="Markdown")
        return

    if status == "success":
        await msg.edit_text(
            f"✅ Payment confirmed! Delivering order `{order_id}`…\n"
            f"UTR: `{data.get('utr', 'N/A')}`",
            parse_mode="Markdown",
        )
        await deliver_order(order_id, data, context)
    elif status == "expired":
        await msg.edit_text(f"⏰ Order `{order_id}` expired.", parse_mode="Markdown")
        await expire_order(order_id, context)
    else:
        info = pending_orders.get(order_id, {})
        await msg.edit_text(
            f"⏳ Order `{order_id}` still pending.\n"
            f"Post: `{info.get('post_id')}` | User: `{info.get('user_id')}`\n\n"
            "_Try again thodi der baad, ya wait karo poller ke liye._",
            parse_mode="Markdown",
        )


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /orders — list all pending orders."""
    if not is_admin(update.effective_user.id):
        return

    if not pending_orders:
        await update.message.reply_text("📭 Koi pending order nahi hai.")
        return

    lines = [f"`{oid}` | post=`{info['post_id']}` | uid=`{info['user_id']}`"
             for oid, info in list(pending_orders.items())[:30]]
    await update.message.reply_text(
        "📦 *Pending Orders:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


# ─── ADMIN: ADD POST ──────────────────────────────────────────────────────────
async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid):
        return

    user_state[uid] = ADMIN_WAIT_POST_ID
    admin_temp[uid] = {}

    await query.edit_message_text(
        "📝 *Post ID enter karo:*\n\n"
        "_(e.g. `FF001`, `ID123`)_\n\n"
        "👇 Type karo ya paste karo:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
    )


async def admin_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_POST_ID:
        return

    post_id = update.message.text.strip()
    if post_id in reserved:
        await update.message.reply_text(
            "⚠️ Is Post ID ka payment abhi chal raha hai. Thodi der baad try karo ya koi aur ID dalo."
        )
        return

    admin_temp[uid]["post_id"] = post_id
    user_state[uid] = ADMIN_WAIT_PRICE

    await update.message.reply_text(
        f"✅ Post ID: `{post_id}`\n\n"
        "💰 *Ab price enter karo (INR me):*\n\n"
        "_(sirf number — e.g. `499` ya `1500`)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
    )


async def admin_add_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_PRICE:
        return

    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Sirf number dalo (0 se zyada)! e.g. `499`", parse_mode="Markdown")
        return

    admin_temp[uid]["price"] = price
    user_state[uid] = ADMIN_WAIT_CREDS

    await update.message.reply_text(
        f"✅ Price: `₹{money(price)}`\n\n"
        "🔐 *Ab login credentials paste karo:*\n\n"
        "_(Koi bhi format — e.g. `Email: abc@gmail.com | Pass: xyz123`)_\n"
        "_(Ye payment ke baad buyer ko milega)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
    )


async def admin_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_CREDS:
        return

    creds  = update.message.text.strip()
    data   = admin_temp.get(uid, {})
    post_id = data.get("post_id")
    price   = data.get("price")

    if not post_id or not price:
        await update.message.reply_text("❌ Kuch error hua, dobara try karo.")
        user_state.pop(uid, None)
        return

    if post_id in reserved:
        await update.message.reply_text(
            "⚠️ Is ID ka payment abhi chal raha hai, save nahi kiya. Thodi der baad dobara add karo."
        )
        user_state.pop(uid, None)
        admin_temp.pop(uid, None)
        return

    relisted = post_id in sold
    sold.discard(post_id)
    posts[post_id] = {"price": price, "creds": creds}
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)

    note = "\n♻️ _Ye ID pehle sold thi — ab dobara available hai._" if relisted else ""
    await update.message.reply_text(
        f"✅ *Post Added Successfully!*\n\n"
        f"📌 Post ID: `{post_id}`\n"
        f"💰 Price: `₹{money(price)}`\n"
        f"🔐 Credentials: _saved_{note}",
        parse_mode="Markdown",
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
            "📭 *Abhi koi post nahi hai.*\n\nPehle add karo!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
        )
        return

    lines = []
    for pid, info in posts.items():
        tag = "🟡" if pid in reserved else "🟢"
        lines.append(f"{tag} `{pid}` — ₹{money(info['price'])}")
    for pid in sold:
        lines.append(f"✅ `{pid}` — SOLD")

    text = "📋 *All Posts:*\n\n" + "\n".join(lines[:60])
    if len(lines) > 60:
        text += f"\n… aur {len(lines) - 60} more"
    text += "\n\n🟢 available · 🟡 payment chal raha · ✅ sold"

    await query.edit_message_text(
        text, parse_mode="Markdown",
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
            "📭 Koi post nahi hai.",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
        )
        return

    buttons = []
    for pid, info in list(posts.items())[:50]:
        tag = "🟡" if pid in reserved else "🗑️"
        buttons.append([InlineKeyboardButton(f"{tag} {pid} — ₹{money(info['price'])}", callback_data=f"del_{pid}")])
    buttons.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")])

    await query.edit_message_text(
        "🗑️ *Konsi post delete karni hai?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    post_id = query.data[len("del_"):]
    if post_id in reserved:
        text = f"⚠️ `{post_id}` ka payment abhi chal raha hai — delete nahi kar sakte."
    elif post_id in posts:
        del posts[post_id]
        text = f"✅ Post `{post_id}` deleted!"
    else:
        text = "❌ Post nahi mila."

    await query.edit_message_text(
        text, parse_mode="Markdown",
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
            "📭 Koi post nahi hai.",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
        )
        return

    buttons = []
    for pid, info in list(posts.items())[:50]:
        tag = "🟡" if pid in reserved else "✏️"
        buttons.append([InlineKeyboardButton(f"{tag} {pid} — ₹{money(info['price'])}", callback_data=f"edit_{pid}")])
    buttons.append([_inline_btn("🔙 Back", _sc=False, callback_data="admin_panel")])

    await query.edit_message_text(
        "✏️ *Konsi post edit karni hai?*",
        parse_mode="Markdown",
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
        await query.answer("Post nahi mila!", show_alert=True)
        return
    await query.answer()

    admin_temp[uid] = {"edit_post_id": post_id}
    info = posts[post_id]
    await query.edit_message_text(
        f"✏️ *Edit Post:* `{post_id}`\n\n"
        f"💰 Current Price: ₹{money(info['price'])}\n"
        "🔐 Credentials: _stored_\n\n"
        "Kya change karna hai?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [_inline_btn("💰 Price Change", _sc=False, callback_data="editfield_price")],
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

    field_name = "price (sirf number)" if field == "price" else "credentials"
    await query.edit_message_text(
        f"✏️ Naya *{field_name}* enter karo:",
        parse_mode="Markdown",
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
        await update.message.reply_text("❌ Error hua, dobara try karo.")
        user_state.pop(uid, None)
        return

    if field == "price":
        try:
            new_price = float(value)
            if new_price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Sirf number dalo (0 se zyada)!")
            return
        posts[post_id]["price"] = new_price
    else:
        posts[post_id]["creds"] = value

    user_state.pop(uid, None)
    admin_temp.pop(uid, None)

    await update.message.reply_text(
        f"✅ Post `{post_id}` updated!\n\n💰 Price: ₹{money(posts[post_id]['price'])}\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [_inline_btn("✏️ Edit Another", _sc=False, callback_data="admin_edit_list")],
            [_inline_btn("⚙️ Admin Panel", _sc=False, callback_data="admin_panel")],
        ]),
    )


# ─── BUYER FLOW ───────────────────────────────────────────────────────────────
async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Works from both inline callback AND reply-keyboard text trigger."""
    query = update.callback_query
    uid   = update.effective_user.id

    if query:
        await query.answer()

    if await fj_gate(update, context):
        return

    if user_pending(uid):
        text   = (
            "⏳ *Aapka ek order pending hai.*\n\n"
            "Upar wale invoice se payment karo ya ❌ Cancel dabao."
        )
        markup = InlineKeyboardMarkup(back_button("main_menu"))
        if query:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if not posts:
        text   = "😔 *Abhi koi ID available nahi hai.*\n\nThodi der baad try karo!"
        markup = InlineKeyboardMarkup(back_button("main_menu"))
        if query:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    user_state[uid] = BUYER_WAIT_POST_ID
    text   = (
        "🛒 *FF ID Buy Karo*\n\n"
        "Post ID bhejo jo khareedni hai:\n\n"
        "_(Group me jo Post ID likhi hai wahi bhejo)_"
    )
    markup = InlineKeyboardMarkup(back_button("main_menu"))
    if query:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def buyer_post_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != BUYER_WAIT_POST_ID:
        return

    post_id = update.message.text.strip()

    if user_pending(uid):
        user_state.pop(uid, None)
        await update.message.reply_text(
            "⏳ Pehle apna pending order complete karo ya ❌ Cancel dabao.",
            reply_markup=menu_button(),
        )
        return

    if post_id in sold:
        await update.message.reply_text(
            "❌ *Ye ID already sold ho chuki hai.*\n\nKoi aur Post ID bhej sakte ho.",
            parse_mode="Markdown", reply_markup=menu_button(),
        )
        return

    if post_id not in posts:
        await update.message.reply_text(
            f"❌ *Post ID `{post_id}` nahi mila!*\n\nSahi ID bhejo ya admin se check karo.",
            parse_mode="Markdown", reply_markup=menu_button(),
        )
        return

    if post_id in reserved:
        await update.message.reply_text(
            "⏳ *Is ID ka payment abhi kisi aur buyer ka chal raha hai.*\n\n"
            "5 minute baad dobara try karo.",
            parse_mode="Markdown", reply_markup=menu_button(),
        )
        return

    # Reserve immediately — no await between check and this line
    reserved[post_id] = "creating"
    user_state.pop(uid, None)

    price   = posts[post_id]["price"]
    chat_id = update.effective_chat.id

    wait_msg = await update.message.reply_text("⏳ Payment generating...")

    # ── Create FamGateway order ──
    try:
        payload = {
            "api_key":       FG_API_KEY,
            "amount":        price,
            "customer_name": update.effective_user.first_name or "Buyer",
        }
        data = await fg_create_order(payload)
        if data.get("status") != "success":
            raise RuntimeError(f"gateway said: {data}")
        od            = data["data"]
        order_id      = od["order_id"]
        payable_amt   = od["payable_amount"]
        qr_url        = od["qr_url"]
        checkout_url  = od["checkout_url"]
        upi_intent    = od.get("upi_intent")
    except Exception as e:
        log.exception("create-order failed")
        if reserved.get(post_id) == "creating":
            reserved.pop(post_id, None)
        await wait_msg.edit_text("❌ Payment generate nahi ho paya. Thodi der baad dobara try karo.")
        await notify_admin(context.bot, f"⚠️ FamGateway order create failed: {e!r}"[:900])
        return

    pending_orders[order_id] = {
        "user_id": uid,
        "post_id": post_id,
        "chat_id": chat_id,
        "msg_id":  None,
    }
    reserved[post_id] = order_id

    invoice_text = (
        "🧾 *AUTO-PAY INVOICE*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 Post ID  : `{post_id}`\n"
        f"💰 Amount   : ₹{payable_amt}\n"
        f"🆔 Order ID : `{order_id}`\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ *QR 5 minute tak valid hai*\n"
        f"📱 Exact *₹{payable_amt}* hi pay karo — amount ya note edit mat karna\n"
        "💎 Payment ke baad credentials apne aap aa jayenge!\n"
        "🔄 _Bot har 2 second me check karta hai — payment hote hi mil jayega_"
    )
    buttons = InlineKeyboardMarkup([
        [ibt("❌ Cancel", f"cancel_{order_id}")],
    ])

    qr_png = await asyncio.to_thread(make_branded_qr, upi_intent)

    try:
        try:
            try:
                if qr_png is None:
                    raise ValueError("no branded QR")
                sent = await update.message.reply_photo(
                    photo=qr_png, caption=invoice_text,
                    parse_mode="Markdown", reply_markup=buttons,
                )
            except Exception:
                sent = await update.message.reply_photo(
                    photo=qr_url, caption=invoice_text,
                    parse_mode="Markdown", reply_markup=buttons,
                )
        except Exception:
            log.exception("QR photo send failed, falling back to text invoice")
            sent = await update.message.reply_text(
                invoice_text, parse_mode="Markdown", reply_markup=buttons,
            )
    except Exception:
        log.exception("invoice send failed")
        release_order(order_id)
        await wait_msg.edit_text("❌ Invoice bhej nahi paya. Dobara try karo.")
        return

    pending_orders[order_id]["msg_id"] = sent.message_id
    await safe_delete(context.bot, chat_id, wait_msg.message_id)

    # ── Start background payment poller ──
    task = asyncio.create_task(poll_payment(order_id, context))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ─── ORDER OUTCOMES ───────────────────────────────────────────────────────────
async def deliver_order(order_id: str, data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Payment confirmed → mark sold, delete invoice, send credentials."""
    info = pending_orders.pop(order_id, None)
    if not info:
        return
    _last_seen.pop(order_id, None)

    post_id = info["post_id"]
    chat_id = info["chat_id"]
    bot     = context.bot

    reserved.pop(post_id, None)
    post = posts.pop(post_id, None)
    sold.add(post_id)

    utr    = data.get("utr") or "N/A"
    sender = data.get("sender") or "N/A"
    log.info("order %s PAID | post=%s utr=%s sender=%s", order_id, post_id, utr, sender)

    if info.get("msg_id"):
        await safe_delete(bot, chat_id, info["msg_id"])

    if post is None:
        try:
            await bot.send_message(
                chat_id,
                "✅ Payment mil gaya, par ID abhi nikal nahi pa rahi. Admin aapse jald contact karega.",
                reply_markup=menu_button(),
            )
        except Exception:
            log.exception("could not message buyer")
        await notify_admin(
            bot,
            f"⚠️ Paid order {order_id} (post {post_id}, UTR {utr}) but post data missing.",
        )
        return

    creds = post["creds"]
    e     = html.escape
    html_text = (
        "✅ <b>PAYMENT VERIFIED!</b>\n\n"
        "🎮 <b>FF ID Credentials:</b>\n"
        f"<pre>{e(creds)}</pre>\n"
        f"📌 Post ID : <code>{e(post_id)}</code>\n"
        f"🆔 Order   : <code>{e(order_id)}</code>\n"
        f"🏦 UTR     : <code>{e(str(utr))}</code>\n"
        f"👤 Paid by : {e(str(sender))}\n\n"
        "🙏 <b>Thanks for purchase! Enjoy!</b>\n"
        "⚠️ Credentials ka screenshot le lo!"
    )
    plain_text = (
        "✅ PAYMENT VERIFIED!\n\n"
        f"🎮 FF ID Credentials:\n{creds}\n\n"
        f"📌 Post ID : {post_id}\n"
        f"🆔 Order   : {order_id}\n"
        f"🏦 UTR     : {utr}\n"
        f"👤 Paid by : {sender}\n\n"
        "🙏 Thanks for purchase! Enjoy!\n"
        "⚠️ Credentials ka screenshot le lo!"
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
                f"⚠️ DELIVERY FAILED — buyer paid but message not sent.\n"
                f"Buyer chat id: {chat_id}\nOrder: {order_id}\nUTR: {utr}\n\nCredentials:\n{creds}",
            )

    # Notify admin of the sale
    await notify_admin(
        bot,
        f"💸 Sale! Post `{post_id}` | Order `{order_id}` | UTR `{utr}` | Buyer `{chat_id}`",
    )


async def expire_order(order_id: str, context: ContextTypes.DEFAULT_TYPE):
    info = release_order(order_id)
    if not info:
        return
    log.info("order %s expired", order_id)
    if info.get("msg_id"):
        await safe_delete(context.bot, info["chat_id"], info["msg_id"])
    try:
        await context.bot.send_message(
            info["chat_id"],
            "⏰ *Payment time khatam!*\n\nOrder expire ho gaya. Dobara try kar sakte ho.",
            parse_mode="Markdown",
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
        await query.answer("Ye order ab active nahi hai.", show_alert=True)
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
    await context.bot.send_message(
        chat_id,
        "❌ Order cancel ho gaya. Kisi aur ID ke liye Main Menu pe jao.",
        reply_markup=menu_button(),
    )


# ─── PAYMENT POLLER ───────────────────────────────────────────────────────────
async def poll_payment(order_id: str, context: ContextTypes.DEFAULT_TYPE):
    """
    Polls FamGateway every POLL_INTERVAL seconds.

    KEY FIX: First check happens immediately (no initial sleep),
    so if the buyer pays before the first scheduled tick, we catch it instantly.
    """
    deadline    = time.monotonic() + POLL_MAX_SECONDS
    first_check = True  # skip the sleep on the very first iteration

    while time.monotonic() < deadline:
        if not first_check:
            await asyncio.sleep(POLL_INTERVAL)
        first_check = False

        if order_id not in pending_orders:
            return  # cancelled / delivered by another code path

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

    await expire_order(order_id, context)


# ─── MAIN MENU CALLBACK ───────────────────────────────────────────────────────
async def main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_state.pop(query.from_user.id, None)
    await send_main_menu(update, context, edit=True)


# ─── MESSAGE ROUTER ───────────────────────────────────────────────────────────
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes text messages: reply-keyboard triggers + state machine."""
    uid   = update.effective_user.id
    state = user_state.get(uid)
    text  = (update.message.text or "").strip()

    # ── Reply-keyboard trigger words ──
    if text in ("🛒 Buy FF ID", stylish("🛒 Buy FF ID"), "Buy FF ID",
                "ʙᴜʏ ꜰꜰ ɪᴅ", "🛒 ʙᴜʏ ꜰꜰ ɪᴅ", stylish("Buy FF ID")):
        user_state.pop(uid, None)
        await buy_start(update, context)
        return

    if text in ("⚙️ Admin Panel", stylish("⚙️ Admin Panel"), "Admin Panel",
                "ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", "⚙️ ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", stylish("Admin Panel")):
        user_state.pop(uid, None)
        await admin_panel(update, context)
        return

    # ── State machine ──
    if state == ADMIN_WAIT_POST_ID:
        await admin_add_price(update, context)
    elif state == ADMIN_WAIT_PRICE:
        await admin_add_creds(update, context)
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
    elif state == ADMIN_SET_ICON:
        await seticon_finish(update, context)
    elif state == BUYER_WAIT_POST_ID:
        await buyer_post_id(update, context)
    else:
        await send_main_menu(update, context)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("handler error", exc_info=context.error)


async def _shutdown(app: Application):
    if _http is not None and not _http.closed:
        await _http.close()


async def _on_startup(app: Application):
    """Notify admin on boot."""
    _fj_load()
    _data_load()
    for tok in re.split(r"[\s,]+", os.getenv("SUPER_ADMINS", "")):
        if tok.isdigit() and int(tok) != ADMIN_ID:
            super_admins.setdefault(int(tok), tok)
    _data_save()
    try:
        vres = await _validate_premium_ids(app.bot)
    except Exception:
        log.exception("validate failed")
        vres = None
    try:
        _resolve_admin_icons()      # Delete/Orders/ForceJoin/Users/Broadcast ke animated icons
    except Exception:
        log.exception("admin icon resolve failed")
    for tok in re.split(r"[\s,]+", os.getenv("FORCE_JOIN", "")):
        if not tok:
            continue
        try:
            e = await _fj_resolve(app.bot, tok)
            if not any(x["id"] == e["id"] for x in force_join):
                force_join.append(e)
        except Exception as exc:
            log.warning("FORCE_JOIN env %s skipped: %s", tok, exc)
    _fj_save()
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🚀 *FF Store Bot started!*\n\n"
                f"🟢 Posts in memory: `{len(posts)}`\n"
                f"📢 Force join: `{len(force_join)}`\n"
                f"🧩 PTB `{_ptb_ver()}` — buttons: `{_btn_mode()}`\n"
                f"📣 Broadcast chats: `{len(bc_chats)}`\n"
                f"🎨 {_icon_report()}\n"
                + (f"🎯 Premium IDs valid: `{vres[0]}/{vres[1]}` | emoji matched: `{vres[2]}`\n"
                   + (f"⚠️ Glyph match nahi: {' '.join(vres[3])}\n" if vres[3] else "")
                   if vres else "⚠️ Premium ID check nahi ho paya\n")
                +
                "💎 Auto-payment polling: every 2 seconds\n"
                "🔧 /checkorder <id> — force verify any order"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        log.warning("startup notify failed")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    _install_premium_patch()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_on_startup)
        .post_shutdown(_shutdown)
        .build()
    )

    private = filters.ChatType.PRIVATE

    # Har update pe user track + ban check (sabse pehle)
    app.add_handler(MessageHandler(private & filters.ALL, track_user), group=-2)
    app.add_handler(CallbackQueryHandler(track_user), group=-2)

    # Commands
    app.add_handler(CommandHandler("start",        start,           filters=private))
    app.add_handler(CommandHandler("start",        group_start,     filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("checkorder",   cmd_checkorder,  filters=private))
    app.add_handler(CommandHandler("orders",       cmd_orders,      filters=private))
    app.add_handler(CommandHandler("seticon",      cmd_seticon,     filters=private))

    # Inline callbacks
    app.add_handler(CallbackQueryHandler(main_menu_cb,       pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(admin_panel,        pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_orders_cb,    pattern="^admin_orders$"))
    app.add_handler(CallbackQueryHandler(admin_add_start,    pattern="^admin_add$"))
    app.add_handler(CallbackQueryHandler(admin_view,         pattern="^admin_view$"))
    app.add_handler(CallbackQueryHandler(admin_delete_list,  pattern="^admin_delete_list$"))
    app.add_handler(CallbackQueryHandler(admin_delete_confirm, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(admin_edit_list,    pattern="^admin_edit_list$"))
    app.add_handler(CallbackQueryHandler(admin_edit_select,  pattern="^edit_"))
    app.add_handler(CallbackQueryHandler(admin_edit_field,   pattern="^editfield_"))
    app.add_handler(CallbackQueryHandler(admin_users,        pattern=r"^(admin_users|usr_p_\d+)$"))
    app.add_handler(CallbackQueryHandler(usr_toggle,         pattern=r"^usr_t_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(usr_id_start,       pattern=r"^usr_(ban|unban)id$"))
    app.add_handler(CallbackQueryHandler(sa_menu,            pattern="^sa_menu$"))
    app.add_handler(CallbackQueryHandler(sa_add_start,       pattern="^sa_add$"))
    app.add_handler(CallbackQueryHandler(sa_del_cb,          pattern=r"^sa_del_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_fj,           pattern="^admin_fj$"))
    app.add_handler(CallbackQueryHandler(fj_add_start,       pattern="^fj_add$"))
    app.add_handler(CallbackQueryHandler(fj_del_cb,          pattern="^fjdel_"))
    app.add_handler(CallbackQueryHandler(fj_check_cb,        pattern="^fj_check$"))
    app.add_handler(CallbackQueryHandler(buy_start,          pattern="^buy_start$"))
    app.add_handler(CallbackQueryHandler(cancel_order,       pattern="^cancel_"))

    # Broadcast
    app.add_handler(CallbackQueryHandler(admin_bc,           pattern="^admin_bc$"))
    app.add_handler(CallbackQueryHandler(bc_send_cb,         pattern="^bc_send$"))
    app.add_handler(CallbackQueryHandler(bc_go_cb,           pattern="^bc_go$"))
    app.add_handler(CallbackQueryHandler(bc_preview_cb,      pattern="^bc_preview$"))
    app.add_handler(CallbackQueryHandler(bc_add_start,       pattern="^bc_add$"))
    app.add_handler(CallbackQueryHandler(bc_import_cb,       pattern="^bc_import$"))
    app.add_handler(CallbackQueryHandler(bc_refresh_cb,      pattern="^bc_refresh$"))
    app.add_handler(CallbackQueryHandler(bc_rmlist_cb,       pattern="^bc_rmlist$"))
    app.add_handler(CallbackQueryHandler(bc_rm_cb,           pattern=r"^bc_rm_-?\d+$"))
    # Bot kisi group/channel me admin bane/hate -> broadcast registry auto-update
    app.add_handler(ChatMemberHandler(track_bot_chat, ChatMemberHandler.MY_CHAT_MEMBER))

    # Text messages
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & private,
        message_router,
    ))

    app.add_error_handler(on_error)

    log.info("🚀 FF Store Bot starting — payment poll every %ds", POLL_INTERVAL)
    # ALL_TYPES: my_chat_member updates (broadcast registry) pakka aate rahein
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
