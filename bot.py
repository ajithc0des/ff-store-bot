import os
import asyncio
import aiohttp
import re
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ["ADMIN_ID"])
FG_API_KEY  = os.environ["FG_API_KEY"]
FG_BASE_URL = "https://famgateway.in/api"

# ─── STATES ────────────────────────────────────────────────────────────────────
# Admin states
ADMIN_WAIT_POST_ID    = "ADMIN_WAIT_POST_ID"
ADMIN_WAIT_PRICE      = "ADMIN_WAIT_PRICE"
ADMIN_WAIT_CREDS      = "ADMIN_WAIT_CREDS"
ADMIN_EDIT_SELECT     = "ADMIN_EDIT_SELECT"
ADMIN_EDIT_FIELD      = "ADMIN_EDIT_FIELD"
ADMIN_EDIT_VALUE      = "ADMIN_EDIT_VALUE"

# Buyer states
BUYER_WAIT_POST_ID    = "BUYER_WAIT_POST_ID"

# ─── TEMPORARY IN-MEMORY STORE ────────────────────────────────────────────────
# { post_id: { "price": float, "creds": str } }
posts: dict = {}

# { order_id: { "user_id": int, "post_id": str, "chat_id": int, "msg_id": int } }
pending_orders: dict = {}

# User conversation state
user_state: dict = {}  # { user_id: state_name }
user_data_temp: dict = {}  # { user_id: {...} }

# Admin temp
admin_temp: dict = {}

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def back_button(callback: str = "main_menu") -> list:
    return [[InlineKeyboardButton("🔙 Back", callback_data=callback)]]

def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🛒 Buy FF ID", callback_data="buy_start")]
    ]
    if is_admin(user_id):
        buttons.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

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
    else:
        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

# ─── START ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state.pop(update.effective_user.id, None)
    await send_main_menu(update, context)

# ─── ADMIN PANEL ──────────────────────────────────────────────────────────────
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if not is_admin(uid):
        await query.answer("❌ Access denied!", show_alert=True)
        return

    post_count = len(posts)
    text = (
        f"⚙️ *Admin Panel*\n\n"
        f"📦 Total Posts: *{post_count}*\n\n"
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
    if not is_admin(uid): return

    user_state[uid] = ADMIN_WAIT_POST_ID
    admin_temp[uid] = {}

    await query.edit_message_text(
        "📝 *Post ID enter karo:*\n\n"
        "_(Jo post ki ID hai — e.g. `FF001`, `ID123`)_\n\n"
        "👇 Type karo ya paste karo:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
    )

async def admin_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    if user_state.get(uid) != ADMIN_WAIT_POST_ID: return

    post_id = update.message.text.strip()
    admin_temp[uid]["post_id"] = post_id
    user_state[uid] = ADMIN_WAIT_PRICE

    await update.message.reply_text(
        f"✅ Post ID: `{post_id}`\n\n"
        "💰 *Ab price enter karo (INR me):*\n\n"
        "_(sirf number — e.g. `499` ya `1500`)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
    )

async def admin_add_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    if user_state.get(uid) != ADMIN_WAIT_PRICE: return

    text = update.message.text.strip()
    try:
        price = float(text)
    except ValueError:
        await update.message.reply_text("❌ Sirf number dalo! e.g. `499`", parse_mode="Markdown")
        return

    admin_temp[uid]["price"] = price
    user_state[uid] = ADMIN_WAIT_CREDS

    await update.message.reply_text(
        f"✅ Price: `₹{price}`\n\n"
        "🔐 *Ab login credentials paste karo:*\n\n"
        "_(Koi bhi format — e.g. `Email: abc@gmail.com | Pass: xyz123`)_\n"
        "_(Ye payment ke baad buyer ko milega)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
    )

async def admin_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    if user_state.get(uid) != ADMIN_WAIT_CREDS: return

    creds = update.message.text.strip()
    data = admin_temp.get(uid, {})
    post_id = data.get("post_id")
    price = data.get("price")

    if not post_id or not price:
        await update.message.reply_text("❌ Kuch error hua, dobara try karo.")
        user_state.pop(uid, None)
        return

    posts[post_id] = {"price": price, "creds": creds}
    user_state.pop(uid, None)
    admin_temp.pop(uid, None)

    buttons = [
        [InlineKeyboardButton("➕ Add Another", callback_data="admin_add")],
        [InlineKeyboardButton("📋 View All Posts", callback_data="admin_view")],
        [InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")],
    ]
    await update.message.reply_text(
        f"✅ *Post Added Successfully!*\n\n"
        f"📌 Post ID: `{post_id}`\n"
        f"💰 Price: `₹{price}`\n"
        f"🔐 Credentials: _saved securely_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ─── VIEW POSTS ───────────────────────────────────────────────────────────────
async def admin_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid): return

    if not posts:
        await query.edit_message_text(
            "📭 *Abhi koi post nahi hai.*\n\nPehle add karo!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
        )
        return

    text = "📋 *All Posts:*\n\n"
    for pid, info in posts.items():
        text += f"📌 `{pid}` — ₹{info['price']}\n"
    text += f"\n*Total: {len(posts)} posts*"

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
    )

# ─── DELETE POST ──────────────────────────────────────────────────────────────
async def admin_delete_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid): return

    if not posts:
        await query.edit_message_text(
            "📭 Koi post nahi hai.",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
        )
        return

    buttons = []
    for pid in posts:
        buttons.append([InlineKeyboardButton(f"🗑️ {pid} — ₹{posts[pid]['price']}", callback_data=f"del_{pid}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

    await query.edit_message_text(
        "🗑️ *Konsi post delete karni hai?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def admin_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid): return

    post_id = query.data.replace("del_", "")
    if post_id in posts:
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
    uid = query.from_user.id
    if not is_admin(uid): return

    if not posts:
        await query.edit_message_text(
            "📭 Koi post nahi hai.",
            reply_markup=InlineKeyboardMarkup(back_button("admin_panel"))
        )
        return

    buttons = []
    for pid in posts:
        buttons.append([InlineKeyboardButton(f"✏️ {pid} — ₹{posts[pid]['price']}", callback_data=f"edit_{pid}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

    await query.edit_message_text(
        "✏️ *Konsi post edit karni hai?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def admin_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid): return

    post_id = query.data.replace("edit_", "")
    if post_id not in posts:
        await query.answer("Post nahi mila!", show_alert=True)
        return

    admin_temp[uid] = {"edit_post_id": post_id}

    info = posts[post_id]
    buttons = [
        [InlineKeyboardButton("💰 Price Change", callback_data=f"editfield_price")],
        [InlineKeyboardButton("🔐 Credentials Change", callback_data=f"editfield_creds")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_edit_list")],
    ]
    await query.edit_message_text(
        f"✏️ *Edit Post:* `{post_id}`\n\n"
        f"💰 Current Price: ₹{info['price']}\n"
        f"🔐 Credentials: _stored_\n\n"
        "Kya change karna hai?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def admin_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid): return

    field = query.data.replace("editfield_", "")
    admin_temp[uid]["edit_field"] = field
    user_state[uid] = ADMIN_EDIT_VALUE

    field_name = "price (sirf number)" if field == "price" else "credentials"
    await query.edit_message_text(
        f"✏️ Naya *{field_name}* enter karo:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("admin_edit_list"))
    )

async def admin_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    if user_state.get(uid) != ADMIN_EDIT_VALUE: return

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
            posts[post_id]["price"] = float(value)
        except ValueError:
            await update.message.reply_text("❌ Sirf number dalo!")
            return
    else:
        posts[post_id]["creds"] = value

    user_state.pop(uid, None)
    admin_temp.pop(uid, None)

    buttons = [
        [InlineKeyboardButton("✏️ Edit Another", callback_data="admin_edit_list")],
        [InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")],
    ]
    await update.message.reply_text(
        f"✅ Post `{post_id}` updated!\n\n"
        f"💰 Price: ₹{posts[post_id]['price']}\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ─── BUYER FLOW ───────────────────────────────────────────────────────────────
async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if not posts:
        await query.edit_message_text(
            "😔 *Abhi koi ID available nahi hai.*\n\nThodi der baad try karo!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(back_button("main_menu"))
        )
        return

    user_state[uid] = BUYER_WAIT_POST_ID
    await query.edit_message_text(
        "🛒 *FF ID Buy Karo*\n\n"
        "Post ID bhejo jo khareedni hai:\n\n"
        "_(Admin ne jo ID batai hai ya group me post ki hai)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(back_button("main_menu"))
    )

async def buyer_post_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_state.get(uid) != BUYER_WAIT_POST_ID: return

    post_id = update.message.text.strip()

    if post_id not in posts:
        await update.message.reply_text(
            f"❌ *Post ID `{post_id}` nahi mila!*\n\n"
            "Sahi ID bhejo ya admin se check karo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="buy_start")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")],
            ])
        )
        return

    user_state.pop(uid, None)
    price = posts[post_id]["price"]

    # Creating FamGateway order
    wait_msg = await update.message.reply_text("⏳ Payment generating...")

    async with aiohttp.ClientSession() as session:
        payload = {
            "api_key": FG_API_KEY,
            "amount": price,
            "customer_name": update.effective_user.first_name or "Buyer",
        }
        try:
            async with session.post(f"{FG_BASE_URL}/create-order", json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
        except Exception as e:
            await wait_msg.edit_text(f"❌ Payment gateway error: {e}\n\nThodi der baad try karo.")
            return

    if data.get("status") != "success":
        await wait_msg.edit_text(
            f"❌ Order create nahi hua.\n_{data.get('message', 'Unknown error')}_",
            parse_mode="Markdown"
        )
        return

    od = data["data"]
    order_id    = od["order_id"]
    payable_amt = od["payable_amount"]
    qr_url      = od["qr_url"]
    checkout_url = od["checkout_url"]

    # Store pending order
    pending_orders[order_id] = {
        "user_id": uid,
        "post_id": post_id,
        "chat_id": update.effective_chat.id,
    }

    invoice_text = (
        f"🧾 *AUTO-PAY INVOICE*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 Post ID  : `{post_id}`\n"
        f"💰 Amount   : ₹{payable_amt}\n"
        f"🆔 Order ID : `{order_id}`\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"⏳ *QR 5 minute tak valid hai*\n"
        f"💎 Payment ke baad credentials auto-milenge!\n"
        f"📱 ₹1 exact daal — UPI note me Order ID hai"
    )

    buttons = [
        [InlineKeyboardButton("🔗 Open Pay Page", url=checkout_url)],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{order_id}")],
    ]

    await wait_msg.delete()

    # Send QR image + invoice
    await update.message.reply_photo(
        photo=qr_url,
        caption=invoice_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    # Start polling for payment in background
    asyncio.create_task(poll_payment(order_id, context))

# ─── CANCEL ORDER ─────────────────────────────────────────────────────────────
async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    order_id = query.data.replace("cancel_", "")
    if order_id in pending_orders and pending_orders[order_id]["user_id"] == uid:
        del pending_orders[order_id]

    await query.edit_message_caption(
        "❌ *Order cancelled.*\n\nKisi aur ID ke liye Main Menu pe jao.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
        ]])
    )

# ─── PAYMENT POLLING ──────────────────────────────────────────────────────────
async def poll_payment(order_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Poll FamGateway every 4 seconds for up to 5 minutes (300s)."""
    timeout = 300
    interval = 4
    elapsed = 0

    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval

        if order_id not in pending_orders:
            return  # Cancelled

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{FG_BASE_URL}/checkout-status.php?order_id={order_id}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
        except Exception:
            continue

        status = data.get("status", "pending")

        if status == "success":
            order_info = pending_orders.pop(order_id, None)
            if not order_info:
                return

            post_id = order_info["post_id"]
            chat_id = order_info["chat_id"]
            user_id = order_info["user_id"]
            creds = posts.get(post_id, {}).get("creds", "N/A")
            utr = data.get("utr", "N/A")
            sender = data.get("sender_name", "N/A")

            success_text = (
                f"✅ *PAYMENT VERIFIED!*\n\n"
                f"🎮 *FF ID Credentials:*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{creds}\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"📌 Post ID : `{post_id}`\n"
                f"🆔 Order   : `{order_id}`\n"
                f"🏦 UTR     : `{utr}`\n"
                f"👤 Paid by : {sender}\n\n"
                f"🙏 *Thanks for purchase! Enjoy!*\n"
                f"⚠️ Credentials screenshot le lo!"
            )

            await context.bot.send_message(
                chat_id=chat_id,
                text=success_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
                ]])
            )
            return

        elif status == "expired":
            order_info = pending_orders.pop(order_id, None)
            if not order_info:
                return

            await context.bot.send_message(
                chat_id=order_info["chat_id"],
                text=(
                    "⏰ *Payment timeout!*\n\n"
                    "Order expire ho gaya. Dobara try karo."
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Buy Again", callback_data="buy_start"),
                    InlineKeyboardButton("🏠 Home", callback_data="main_menu"),
                ]])
            )
            return

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
        # Default: show main menu
        await send_main_menu(update, context)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))

    # Callback handlers
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

    # Message handler (all text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    print("🚀 FF Store Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
