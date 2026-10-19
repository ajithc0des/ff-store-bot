"""FF ID Store Bot — Telegram + FamGateway UPI auto-payment.

No database: posts / sold list / pending orders live in memory only
(a Railway redeploy or restart clears them).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
FG_API_KEY = os.environ["FG_API_KEY"]
FG_BASE_URL = "https://famgateway.in/api"

POLL_INTERVAL = 1        # 1 second — instant detection on payment confirm
POLL_MAX_SECONDS = 360   # gateway order lives 300s; small grace on top

# ─── STATES ────────────────────────────────────────────────────────────────────
ADMIN_WAIT_POST_ID = "ADMIN_WAIT_POST_ID"
ADMIN_WAIT_PRICE = "ADMIN_WAIT_PRICE"
ADMIN_WAIT_CREDS = "ADMIN_WAIT_CREDS"
ADMIN_EDIT_VALUE = "ADMIN_EDIT_VALUE"
BUYER_WAIT_POST_ID = "BUYER_WAIT_POST_ID"

# ─── TEMPORARY IN-MEMORY STORE ────────────────────────────────────────────────
posts: dict = {}            # {post_id: {"price": float, "creds": str}}  (available + reserved)
sold: set = set()           # post_ids that were paid for and delivered
reserved: dict = {}         # {post_id: order_id | "creating"}  payment in progress
pending_orders: dict = {}   # {order_id: {"user_id", "post_id", "chat_id", "msg_id"}}

user_state: dict = {}       # {user_id: state}
admin_temp: dict = {}       # {admin_id: {...}}

_last_seen: dict = {}       # {order_id: (status_a, status_b)}  for log de-duplication
_tasks: set = set()         # keep references to background poll tasks
_http = None


# ─── STYLING (ported from app.py) ─────────────────────────────────────────────
def stylish(text: str) -> str:
    """Convert text to Unicode small-caps — same as app.py."""
    return text.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ"
    )).strip()


def btn(text: str, callback_data: str, style: str = "primary") -> InlineKeyboardButton:
    """Styled inline button — applies stylish() Unicode text transform.
    style param kept for call-site readability but NOT passed to InlineKeyboardButton
    (ptb 21.6 does not support it and it crashes with TypeError)."""
    label = stylish(text)
    return InlineKeyboardButton(text=label, callback_data=callback_data)


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def money(x) -> str:
    return f"{float(x):g}"


def back_button(callback: str = "main_menu") -> list:
    return [[btn("🔙 Back", callback, style="danger")]]


def menu_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[btn("🏠 Main Menu", "main_menu", style="primary")]]
    )


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [[btn("🛒 Buy FF ID", "buy_start", style="success")]]
    if is_admin(user_id):
        buttons.append([btn("⚙️ Admin Panel", "admin_panel", style="primary")])
    return InlineKeyboardMarkup(buttons)


async def safe_delete(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:  # already deleted / too old — not important
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
    """Drop a pending order and free its post reservation. Returns the order info or None."""
    info = pending_orders.pop(order_id, None)
    _last_seen.pop(order_id, None)
    if info and reserved.get(info["post_id"]) == order_id:
        reserved.pop(info["post_id"], None)
    return info


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    uid = update.effective_user.id
    text = (
        "🎮 *FF ID Store Bot*\n\n"
        "Welcome! Apna kaam select karo:\n\n"
        "🛒 *Buy FF ID* — ID khareedne ke liye\n"
    )
    if is_admin(uid):
        text += "⚙️ *Admin Panel* — Posts manage karne ke liye\n"

    markup = main_menu_keyboard(uid)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)


# ─── FAMGATEWAY ───────────────────────────────────────────────────────────────
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
    """GET url and return parsed JSON, or None on any network / non-JSON problem."""
    try:
        async with http().get(url, params=params, headers=headers) as resp:
            body = await resp.text()
            try:
                return json.loads(body)
            except ValueError:
                log.warning("non-JSON from %s (HTTP %s): %s", url, resp.status, body[:200])
                return None
    except Exception as e:
        log.warning("request failed %s: %r", url, e)
        return None


def _is_paid(resp) -> bool:
    """Robust payment detection — handles every shape FamGateway might return.
    Checks boolean flags, 15+ status field names, nested structures, flat JSON.
    Ported from app.py."""
    if not isinstance(resp, dict):
        return False
    # Boolean fast-path flags
    for key in ("paid", "is_paid", "payment_received", "isPaid", "paymentReceived", "payment_verified"):
        if resp.get(key) is True:
            return True
    # String status fields — all names FamGateway has ever used
    for key in (
        "payment_status", "paymentStatus", "status", "verification",
        "order_status", "orderStatus", "paymentState", "state",
        "txn_status", "transaction_status",
    ):
        v = str(resp.get(key, "")).strip().lower()
        if v in (
            "paid", "success", "successful", "completed", "complete",
            "received", "captured", "verified", "confirmed",
            "payment received", "payment_received", "payment successful",
            "payment_successful",
        ):
            return True
    # Flat JSON string scan — catches weirdly nested / stringified responses
    flat = json.dumps(resp).lower()
    if ("payment received" in flat) or ("payment successful" in flat) \
       or ("payment success" in flat) or ("payment_received" in flat) \
       or ("payment_success" in flat):
        return True
    if ("payment" in flat) and ("paid" in flat):
        return True
    if '"success": true' in flat and "order" in flat and ("paid" in flat or "received" in flat):
        return True
    # Recurse into nested objects / lists
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
    """Check if FamGateway response indicates an expired / failed / cancelled order."""
    if not isinstance(resp, dict):
        return False
    for src in (resp, resp.get("data") if isinstance(resp.get("data"), dict) else {}):
        for key in ("status", "payment_status", "order_status", "state"):
            v = str(src.get(key, "")).strip().lower()
            if v in ("expired", "failed", "cancelled", "canceled", "timeout"):
                return True
    return False


def _extract_field(resp, *field_names) -> str | None:
    """Try multiple field names across top-level and nested 'data' key."""
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
    """Ask both FamGateway endpoints (public checkout-status + authoritative verify-order).

    Returns ("success", {"utr", "sender"}) | ("expired", {}) | ("pending", {}).
    Uses robust _is_paid() — catches all response shapes FamGateway might return.
    """
    ts = str(int(time.time() * 1000))  # cache-buster
    pub, auth = await asyncio.gather(
        _fetch(f"{FG_BASE_URL}/checkout-status.php", {"order_id": order_id, "_": ts}),
        _fetch(
            f"{FG_BASE_URL}/verify-order.php",
            {"order_id": order_id, "_": ts},
            {"X-Api-Key": FG_API_KEY},
        ),
    )

    # Log only when something changes (de-dup noise on 1s polling)
    seen = (
        json.dumps(pub, sort_keys=True) if pub else "",
        json.dumps(auth, sort_keys=True) if auth else "",
    )
    if _last_seen.get(order_id) != seen:
        _last_seen[order_id] = seen
        for name, raw in [("checkout-status", pub), ("verify-order", auth)]:
            shown = json.dumps(raw)[:300] if raw is not None else "no/invalid response"
            log.info("order %s | %s | %s", order_id, name, shown)

    # Check success — verify-order (auth) is more authoritative, check it first
    for resp in (auth, pub):
        if resp and _is_paid(resp):
            utr    = _extract_field(resp, "utr", "transaction_id", "txn_id", "ref_id", "rrn")
            sender = _extract_field(resp, "sender_name", "payer_name", "customer_name", "sender") or "N/A"
            return "success", {"utr": utr or "N/A", "sender": sender}

    # Check expired
    for resp in (pub, auth):
        if resp and _is_expired(resp):
            return "expired", {}

    return "pending", {}


# ─── START ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state.pop(update.effective_user.id, None)
    await send_main_menu(update, context)


# ─── ADMIN PANEL ──────────────────────────────────────────────────────────────
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    n_reserved = len([p for p in posts if p in reserved])
    n_avail = len(posts) - n_reserved
    text = (
        "⚙️ *Admin Panel*\n\n"
        f"🟢 Available: *{n_avail}*\n"
        f"🟡 Payment chal raha: *{n_reserved}*\n"
        f"✅ Sold: *{len(sold)}*\n\n"
        "Kya karna hai?"
    )
    buttons = [
        [InlineKeyboardButton("➕ Add New Post", callback_data="admin_add")],
        [InlineKeyboardButton("📋 View All Posts", callback_data="admin_view")],
        [InlineKeyboardButton("✏️ Edit Post", callback_data="admin_edit_list")],
        [InlineKeyboardButton("🗑️ Delete Post", callback_data="admin_delete_list")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


# ─── ADD POST ─────────────────────────────────────────────────────────────────
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
        "_(Jo post ki ID hai — e.g. `FF001`, `ID123`)_\n\n"
        "👇 Type karo ya paste karo:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel")),
    )


async def admin_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin just sent the Post ID."""
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
    """Admin just sent the price."""
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
    """Admin just sent the credentials."""
    uid = update.effective_user.id
    if not is_admin(uid) or user_state.get(uid) != ADMIN_WAIT_CREDS:
        return

    creds = update.message.text.strip()
    data = admin_temp.get(uid, {})
    post_id = data.get("post_id")
    price = data.get("price")

    if not post_id or not price:
        await update.message.reply_text("❌ Kuch error hua, dobara try karo.")
        user_state.pop(uid, None)
        return

    if post_id in reserved:
        await update.message.reply_text("⚠️ Is ID ka payment abhi chal raha hai, save nahi kiya. Thodi der baad dobara add karo.")
        user_state.pop(uid, None)
        admin_temp.pop(uid, None)
        return

    relisted = post_id in sold
    sold.discard(post_id)
    posts[post_id] = {"price": price, "creds": creds}
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)

    buttons = [
        [InlineKeyboardButton("➕ Add Another", callback_data="admin_add")],
        [InlineKeyboardButton("📋 View All Posts", callback_data="admin_view")],
        [InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")],
    ]
    note = "\n♻️ _Ye ID pehle sold thi — ab dobara available hai._" if relisted else ""
    await update.message.reply_text(
        f"✅ *Post Added Successfully!*\n\n"
        f"📌 Post ID: `{post_id}`\n"
        f"💰 Price: `₹{money(price)}`\n"
        f"🔐 Credentials: _saved_{note}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─── VIEW POSTS ───────────────────────────────────────────────────────────────
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
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
    )


# ─── DELETE POST ──────────────────────────────────────────────────────────────
async def admin_delete_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    if not posts:
        await query.edit_message_text(
            "📭 Koi post nahi hai.", reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
        )
        return

    buttons = []
    for pid, info in list(posts.items())[:50]:
        tag = "🟡" if pid in reserved else "🗑️"
        buttons.append([InlineKeyboardButton(f"{tag} {pid} — ₹{money(info['price'])}", callback_data=f"del_{pid}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

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
        text = f"⚠️ `{post_id}` ka payment abhi chal raha hai — delete nahi kar sakte. Thodi der baad try karo."
    elif post_id in posts:
        del posts[post_id]
        text = f"✅ Post `{post_id}` deleted!"
    else:
        text = "❌ Post nahi mila."

    buttons = [
        [InlineKeyboardButton("🗑️ Delete Another", callback_data="admin_delete_list")],
        [InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")],
    ]
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


# ─── EDIT POST ────────────────────────────────────────────────────────────────
async def admin_edit_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    if not posts:
        await query.edit_message_text(
            "📭 Koi post nahi hai.", reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
        )
        return

    buttons = []
    for pid, info in list(posts.items())[:50]:
        tag = "🟡" if pid in reserved else "✏️"
        buttons.append([InlineKeyboardButton(f"{tag} {pid} — ₹{money(info['price'])}", callback_data=f"edit_{pid}")])
    buttons.append([InlineKeyboardButton(f"{tag} {pid} — ₹{money(info['price'])}", callback_data=f"edit_{pid}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

    await query.edit_message_text(
        "✏️ *Konsi post edit karni hai?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
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
    buttons = [
        [InlineKeyboardButton("💰 Price Change", callback_data="editfield_price")],
        [InlineKeyboardButton("🔐 Credentials Change", callback_data="editfield_creds")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_edit_list")],
    ]
    await query.edit_message_text(
        f"✏️ *Edit Post:* `{post_id}`\n\n"
        f"💰 Current Price: ₹{money(info['price'])}\n"
        "🔐 Credentials: _stored_\n\n"
        "Kya change karna hai?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
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

    value = update.message.text.strip()
    data = admin_temp.get(uid, {})
    post_id = data.get("edit_post_id")
    field = data.get("edit_field")

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

    buttons = [
        [InlineKeyboardButton("✏️ Edit Another", callback_data="admin_edit_list")],
        [InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")],
    ]
    await update.message.reply_text(
        f"✅ Post `{post_id}` updated!\n\n💰 Price: ₹{money(posts[post_id]['price'])}\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─── BUYER FLOW ───────────────────────────────────────────────────────────────
async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if user_pending(uid):
        await query.edit_message_text(
            "⏳ *Aapka ek order pending hai.*\n\n"
            "Upar wale invoice se payment karo ya ❌ Cancel dabao.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(back_button("main_menu")),
        )
        return

    if not posts:
        await query.edit_message_text(
            "😔 *Abhi koi ID available nahi hai.*\n\nThodi der baad try karo!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(back_button("main_menu")),
        )
        return

    user_state[uid] = BUYER_WAIT_POST_ID
    await query.edit_message_text(
        "🛒 *FF ID Buy Karo*\n\n"
        "Post ID bhejo jo khareedni hai:\n\n"
        "_(Group me jo Post ID likhi hai wahi bhejo)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("main_menu")),
    )


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
            parse_mode="Markdown",
            reply_markup=menu_button(),
        )
        return

    if post_id not in posts:
        await update.message.reply_text(
            f"❌ *Post ID `{post_id}` nahi mila!*\n\nSahi ID bhejo ya admin se check karo.",
            parse_mode="Markdown",
            reply_markup=menu_button(),
        )
        return

    if post_id in reserved:
        await update.message.reply_text(
            "⏳ *Is ID ka payment abhi kisi aur buyer ka chal raha hai.*\n\n"
            "5 minute baad dobara try karo. Agar wo pay nahi karta to ID wapas available ho jayegi.",
            parse_mode="Markdown",
            reply_markup=menu_button(),
        )
        return

    # Reserve right away (no await between the checks above and this line,
    # so two buyers can never grab the same ID).
    reserved[post_id] = "creating"
    user_state.pop(uid, None)
    price = posts[post_id]["price"]
    chat_id = update.effective_chat.id

    wait_msg = await update.message.reply_text("⏳ Payment generating...")

    # ── create FamGateway order ──
    try:
        payload = {
            "api_key": FG_API_KEY,
            "amount": price,
            "customer_name": update.effective_user.first_name or "Buyer",
        }
        async with http().post(
            f"{FG_BASE_URL}/create-order", json=payload, headers={"X-Api-Key": FG_API_KEY}
        ) as resp:
            data = await resp.json(content_type=None)
        if data.get("status") != "success":
            raise RuntimeError(f"gateway said: {data}")
        od = data["data"]
        order_id = od["order_id"]
        payable_amt = od["payable_amount"]
        qr_url = od["qr_url"]
        checkout_url = od["checkout_url"]
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
        "msg_id": None,
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
        "💎 Payment ke baad credentials apne aap aa jayenge!"
    )
    # "Open Pay Page" hidden — payment is fully automatic via QR scan.
    # Bot polls every second and delivers credentials the moment payment confirms.
    _ = checkout_url  # kept in scope in case needed for admin logging
    buttons = InlineKeyboardMarkup(
        [
            [btn("❌ Cancel", f"cancel_{order_id}", style="danger")],
        ]
    )

    try:
        try:
            sent = await update.message.reply_photo(
                photo=qr_url, caption=invoice_text, parse_mode="Markdown", reply_markup=buttons
            )
        except Exception:
            log.exception("QR photo send failed, falling back to text invoice")
            sent = await update.message.reply_text(
                invoice_text, parse_mode="Markdown", reply_markup=buttons
            )
    except Exception:
        log.exception("invoice send failed")
        release_order(order_id)
        await wait_msg.edit_text("❌ Invoice bhej nahi paya. Dobara try karo.")
        return

    pending_orders[order_id]["msg_id"] = sent.message_id
    await safe_delete(context.bot, chat_id, wait_msg.message_id)

    task = asyncio.create_task(poll_payment(order_id, context))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ─── ORDER OUTCOMES ───────────────────────────────────────────────────────────
async def deliver_order(order_id: str, data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Payment confirmed: mark sold, remove the invoice, send credentials."""
    info = pending_orders.pop(order_id, None)
    if not info:  # already handled (poll task vs cancel button)
        return
    _last_seen.pop(order_id, None)

    post_id = info["post_id"]
    chat_id = info["chat_id"]
    bot = context.bot
    reserved.pop(post_id, None)
    post = posts.pop(post_id, None)
    sold.add(post_id)

    utr = data.get("utr") or "N/A"
    sender = data.get("sender") or "N/A"
    log.info("order %s PAID | post=%s utr=%s", order_id, post_id, utr)

    if info.get("msg_id"):
        await safe_delete(bot, chat_id, info["msg_id"])  # old QR / invoice goes away

    if post is None:  # should not happen, but never leave a paid buyer silent
        try:
            await bot.send_message(
                chat_id,
                "✅ Payment mil gaya, par ID abhi nikal nahi pa rahi. Admin aapse jald contact karega.",
                reply_markup=menu_button(),
            )
        except Exception:
            log.exception("could not message buyer")
        await notify_admin(bot, f"⚠️ Paid order {order_id} (post {post_id}, UTR {utr}) but post data missing.")
        return

    creds = post["creds"]
    e = html.escape
    html_text = (
        "✅ <b>PAYMENT VERIFIED!</b>\n\n"
        "🎮 <b>FF ID Credentials:</b>\n"
        f"<pre>{e(creds)}</pre>\n"
        f"📌 Post ID : <code>{e(post_id)}</code>\n"
        f"🆔 Order : <code>{e(order_id)}</code>\n"
        f"🏦 UTR : <code>{e(str(utr))}</code>\n"
        f"👤 Paid by : {e(str(sender))}\n\n"
        "🙏 <b>Thanks for purchase! Enjoy!</b>\n"
        "⚠️ Credentials ka screenshot le lo!"
    )
    plain_text = (
        "✅ PAYMENT VERIFIED!\n\n"
        f"🎮 FF ID Credentials:\n{creds}\n\n"
        f"📌 Post ID : {post_id}\n🆔 Order : {order_id}\n🏦 UTR : {utr}\n👤 Paid by : {sender}\n\n"
        "🙏 Thanks for purchase! Enjoy!\n⚠️ Credentials ka screenshot le lo!"
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
            reply_markup=InlineKeyboardMarkup(
                [[
                    btn("🔄 Buy Again", "buy_start", style="success"),
                    btn("🏠 Home", "main_menu", style="primary"),
                ]]
            ),
        )
    except Exception:
        log.exception("could not send expiry message")


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    order_id = query.data[len("cancel_"):]
    chat_id = query.message.chat_id
    info = pending_orders.get(order_id)

    if not info or info["user_id"] != uid:
        await query.answer("Ye order ab active nahi hai.", show_alert=True)
        await safe_delete(context.bot, chat_id, query.message.message_id)
        return

    await query.answer()

    # Last-chance check: if the buyer already paid, deliver instead of cancelling.
    status, data = await fg_status(order_id)
    if status == "success":
        await deliver_order(order_id, data, context)
        return

    if release_order(order_id) is None:  # poll task finished it while we were checking
        return
    await safe_delete(context.bot, chat_id, query.message.message_id)  # QR goes away
    await context.bot.send_message(
        chat_id,
        "❌ Order cancel ho gaya. Kisi aur ID ke liye Main Menu pe jao.",
        reply_markup=menu_button(),
    )


# ─── PAYMENT POLLING ──────────────────────────────────────────────────────────
async def poll_payment(order_id: str, context: ContextTypes.DEFAULT_TYPE):
    deadline = time.monotonic() + POLL_MAX_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        if order_id not in pending_orders:
            return  # cancelled / delivered elsewhere
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
    uid = update.effective_user.id
    state = user_state.get(uid)

    if state == ADMIN_WAIT_POST_ID:
        await admin_add_price(update, context)
    elif state == ADMIN_WAIT_PRICE:
        await admin_add_creds(update, context)
    elif state == ADMIN_WAIT_CREDS:
        await admin_add_finish(update, context)
    elif state == ADMIN_EDIT_VALUE:
        await admin_edit_value(update, context)
    elif state == BUYER_WAIT_POST_ID:
        await buyer_post_id(update, context)
    else:
        await send_main_menu(update, context)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("handler error", exc_info=context.error)


async def _shutdown(app: Application):
    if _http is not None and not _http.closed:
        await _http.close()


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).post_shutdown(_shutdown).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_add_start, pattern="^admin_add$"))
    app.add_handler(CallbackQueryHandler(admin_view, pattern="^admin_view$"))
    app.add_handler(CallbackQueryHandler(admin_delete_list, pattern="^admin_delete_list$"))
    app.add_handler(CallbackQueryHandler(admin_delete_confirm, pattern="^del_"))
    app.add_handler(CallbackQueryHandler(admin_edit_list, pattern="^admin_edit_list$"))
    app.add_handler(CallbackQueryHandler(admin_edit_select, pattern="^edit_"))
    app.add_handler(CallbackQueryHandler(admin_edit_field, pattern="^editfield_"))
    app.add_handler(CallbackQueryHandler(buy_start, pattern="^buy_start$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    app.add_error_handler(on_error)

    log.info("🚀 FF Store Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
