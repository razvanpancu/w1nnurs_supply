import os
import json
from decimal import Decimal
from urllib.parse import quote_plus

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID",
    "1uKVO5PFH_-vO5ZBr-GUJRUlfC0a1uw-iJaxxu2BsOfM",
)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@yourusername")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/yourchannel")
BOT_USERNAME = os.getenv("BOT_USERNAME", "W1nnursSupplyBot").lstrip("@")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

SIZE_COLUMNS = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]

BRAND_TABS = [
    ("ALO", "Alo"),
    ("ALOCS", "ALOCS"),
    ("BAPE", "BAPE"),
    ("CPFM", "CPFM"),
    ("DENIM TEARS", "DENIM TEARS"),
    ("KITH", "Kith"),
    ("NIKE", "NIKE CLO"),
    ("STUSSY", "STUSSY"),
    ("SUPREME", "SUPREME"),
    ("TRAVIS", "TRAVIS"),
    ("YZY", "YZY"),
]

price_requests = {}
pending_admin_price = {}
request_counter = 0


def get_spreadsheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Latest Drops", callback_data="latest")],
        [InlineKeyboardButton("📦 Available Stock", callback_data="brands")],
        [InlineKeyboardButton("🤝 W1NNURS Partners", callback_data="partners")],
        [InlineKeyboardButton("🛒 Order / Reserve", callback_data="order")],
        [InlineKeyboardButton("💬 Support", callback_data="support")],
    ])


def brand_menu():
    buttons, row = [], []
    for brand, sheet_name in BRAND_TABS:
        row.append(InlineKeyboardButton(brand, callback_data=f"brand:{sheet_name}:0"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="back")])
    return InlineKeyboardMarkup(buttons)


def qint(value):
    try:
        if value is None or str(value).strip() == "":
            return 0
        return int(float(str(value).replace(",", ".")))
    except Exception:
        return 0


def load_sheet_stock(sheet_name):
    ws = get_spreadsheet().worksheet(sheet_name)
    rows = ws.get_all_values()
    if not rows:
        return []

    headers = [str(x).strip().upper() for x in rows[0]]
    idx = {h: i for i, h in enumerate(headers) if h}

    # If PRODUCT NAME is missing, use column A.
    product_idx = idx.get("PRODUCT NAME", 0)
    sku_idx = idx.get("SKU")

    products = []
    for row_number, row in enumerate(rows[1:], start=2):
        name = row[product_idx].strip() if product_idx < len(row) else ""
        if not name or name.upper() == "PRODUCT NAME":
            continue

        sku = ""
        if sku_idx is not None and sku_idx < len(row):
            sku = row[sku_idx].strip()

        sizes = {}
        for size in SIZE_COLUMNS:
            if size in idx:
                cell_idx = idx[size]
                qty = qint(row[cell_idx] if cell_idx < len(row) else "")
                if qty > 0:
                    sizes[size] = qty

        if sizes:
            products.append({
                "sheet": sheet_name,
                "row": row_number,
                "name": name,
                "sku": sku,
                "sizes": sizes,
            })

    return products


def research_urls(product_name):
    q = quote_plus(product_name)
    return {
        "stockx": f"https://stockx.com/search?s={q}",
        "google": f"https://www.google.com/search?q={q}",
        "images": f"https://www.google.com/search?tbm=isch&q={q}",
    }


def stock_deep_link():
    return f"https://t.me/{BOT_USERNAME}?start=stock"


async def send_brands_message(message):
    await message.reply_text(
        "📦 *AVAILABLE STOCK*\n\nChoose a brand:",
        parse_mode="Markdown",
        reply_markup=brand_menu(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep link from group: https://t.me/W1nnursSupplyBot?start=stock
    if context.args and context.args[0].lower() == "stock":
        await send_brands_message(update.message)
        return

    await update.message.reply_text(
        "🏆 *W1NNURS SUPPLY*\n\n"
        "Welcome to W1NNURS SUPPLY.\n"
        "Your private access to reseller stock.\n\n"
        "Choose an option:",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type

    # In groups: keep stock/private pricing out of public chat.
    if chat_type in ("group", "supergroup"):
        await update.message.reply_text(
            "📦 *W1NNURS LIVE STOCK*\n\n"
            "Open the private catalog to check live stock, research products and request a personal price.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 OPEN LIVE STOCK", url=stock_deep_link())]
            ]),
        )
        return

    # In private chat: open brands immediately.
    await send_brands_message(update.message)


async def hub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Alias useful in group.
    await stock_command(update, context)


async def idcmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Your Telegram numeric ID is:\n`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


async def show_brands(query):
    await query.edit_message_text(
        "📦 *AVAILABLE STOCK*\n\nChoose a brand:",
        parse_mode="Markdown",
        reply_markup=brand_menu(),
    )


async def show_brand_stock(query, sheet_name, page=0):
    try:
        items = load_sheet_stock(sheet_name)
    except Exception:
        await query.edit_message_text(
            "⚠️ I couldn't read this brand right now.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Brands", callback_data="brands")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="back")],
            ]),
        )
        return

    brand_display = next((b for b, s in BRAND_TABS if s == sheet_name), sheet_name)

    if not items:
        await query.edit_message_text(
            f"📦 *{brand_display}*\n\nNo products with stock right now.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Brands", callback_data="brands")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="back")],
            ]),
        )
        return

    per_page = 8
    total_pages = (len(items) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page

    buttons = []
    for i, product in enumerate(items[start_idx:start_idx + per_page], start=start_idx):
        label = product["name"]
        if len(label) > 45:
            label = label[:42] + "..."
        buttons.append([
            InlineKeyboardButton(
                f"📦 {label}",
                callback_data=f"product:{sheet_name}:{i}:{page}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"brand:{sheet_name}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"brand:{sheet_name}:{page+1}"))

    buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ Brands", callback_data="brands")])
    buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="back")])

    await query.edit_message_text(
        f"📦 *{brand_display}*\n\n"
        f"{len(items)} products currently in stock.\n"
        "Select a product:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_product(query, sheet_name, product_index, brand_page):
    items = load_sheet_stock(sheet_name)

    if product_index >= len(items):
        await query.answer("Stock changed. Please reopen the brand.", show_alert=True)
        return

    product = items[product_index]
    urls = research_urls(product["name"])

    size_lines = "\n".join(
        f"• {size}: {qty} pcs"
        for size, qty in product["sizes"].items()
    )

    sku_line = f"\nSKU: `{product['sku']}`" if product["sku"] else ""

    buttons = []
    for size in product["sizes"]:
        buttons.append([
            InlineKeyboardButton(
                f"💰 Ask Price • {size}",
                callback_data=f"ask:{sheet_name}:{product_index}:{size}",
            )
        ])

    buttons.append([
        InlineKeyboardButton("📈 Search StockX", url=urls["stockx"]),
        InlineKeyboardButton("🔎 Google", url=urls["google"]),
    ])
    buttons.append([InlineKeyboardButton("🖼 Google Images", url=urls["images"])])
    buttons.append([
        InlineKeyboardButton(
            "⬅️ Back to Brand",
            callback_data=f"brand:{sheet_name}:{brand_page}",
        )
    ])
    buttons.append([InlineKeyboardButton("📦 All Brands", callback_data="brands")])
    buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="back")])

    await query.edit_message_text(
        f"📦 *{product['name']}*"
        f"{sku_line}\n\n"
        f"*Available:*\n{size_lines}\n\n"
        "💰 Price available on request.\n"
        "🔎 Use the research buttons below for market references, photos and product information.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def ask_price(query, sheet_name, product_index, size):
    global request_counter

    items = load_sheet_stock(sheet_name)
    if product_index >= len(items) or size not in items[product_index]["sizes"]:
        await query.answer("This size is no longer available.", show_alert=True)
        return

    if not ADMIN_IDS:
        await query.answer("Admin is not configured yet.", show_alert=True)
        return

    product = items[product_index]
    request_counter += 1
    request_id = request_counter

    price_requests[request_id] = {
        "user_id": query.from_user.id,
        "username": query.from_user.username or "",
        "first_name": query.from_user.first_name or "",
        "sheet": sheet_name,
        "product": product["name"],
        "sku": product["sku"],
        "size": size,
        "available_qty": product["sizes"][size],
    }

    requester = (
        f"@{query.from_user.username}"
        if query.from_user.username
        else f"{query.from_user.first_name} ({query.from_user.id})"
    )

    for admin_id in ADMIN_IDS:
        try:
            await query.get_bot().send_message(
                chat_id=admin_id,
                text=(
                    "💰 *NEW PRICE REQUEST*\n\n"
                    f"Reseller: {requester}\n"
                    f"Brand/Sheet: *{sheet_name}*\n"
                    f"Product: *{product['name']}*\n"
                    f"Size: *{size}*\n"
                    f"Available: *{product['sizes'][size]} pcs*\n"
                    f"Request: `#{request_id}`"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💶 Reply with Price", callback_data=f"adminprice:{request_id}")],
                    [InlineKeyboardButton("💬 Open User", url=f"tg://user?id={query.from_user.id}")],
                ]),
            )
        except Exception:
            pass

    await query.answer("Price request sent ✅", show_alert=True)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "noop":
        await query.answer()
        return

    if data == "brands":
        await query.answer()
        await show_brands(query)
        return

    if data.startswith("brand:"):
        _, sheet_name, page = data.split(":", 2)
        await query.answer()
        await show_brand_stock(query, sheet_name, int(page))
        return

    if data.startswith("product:"):
        _, sheet_name, product_index, brand_page = data.split(":", 3)
        await query.answer()
        await show_product(query, sheet_name, int(product_index), int(brand_page))
        return

    if data.startswith("ask:"):
        _, sheet_name, product_index, size = data.split(":", 3)
        await ask_price(query, sheet_name, int(product_index), size)
        return

    if data.startswith("adminprice:"):
        request_id = int(data.split(":")[1])

        if query.from_user.id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return

        if request_id not in price_requests:
            await query.answer("This request expired.", show_alert=True)
            return

        pending_admin_price[query.from_user.id] = request_id
        await query.answer()
        await query.message.reply_text(
            "💶 Send the price you want to offer.\n\nExample: `72` or `72.50`",
            parse_mode="Markdown",
        )
        return

    await query.answer()

    if data == "back":
        await query.edit_message_text(
            "🏆 *W1NNURS SUPPLY*\n\nChoose an option:",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
    elif data == "latest":
        await query.edit_message_text(
            "🔥 *LATEST DROPS*\n\nSee the W1NNURS SUPPLY channel for the newest drops.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📲 Open Channel", url=CHANNEL_URL)],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )
    elif data == "partners":
        await query.edit_message_text(
            "🤝 *W1NNURS PARTNERS*\n\nPrivate reseller access for approved partners.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Contact", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )
    elif data == "order":
        await query.edit_message_text(
            "🛒 *ORDER / RESERVE*\n\nOpen Available Stock, choose a brand and request your personal price.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Available Stock", callback_data="brands")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )
    elif data == "support":
        await query.edit_message_text(
            f"💬 *W1NNURS SUPPORT*\n\n{SUPPORT_USERNAME}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Contact", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )


async def admin_price_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id

    if admin_id not in pending_admin_price:
        return

    request_id = pending_admin_price[admin_id]
    request = price_requests.get(request_id)

    if not request:
        pending_admin_price.pop(admin_id, None)
        await update.message.reply_text("Request expired.")
        return

    raw = update.message.text.strip().replace("€", "").replace(",", ".")

    try:
        price = Decimal(raw)
        if price <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text(
            "Send a valid price, e.g. `72` or `72.50`.",
            parse_mode="Markdown",
        )
        return

    pending_admin_price.pop(admin_id, None)
    price_display = f"{price:.2f}".rstrip("0").rstrip(".")

    await context.bot.send_message(
        chat_id=request["user_id"],
        text=(
            "🏆 *W1NNURS SUPPLY OFFER*\n\n"
            f"*{request['product']}*\n"
            f"Size: *{request['size']}*\n"
            f"Your price: *€{price_display} / pc*\n\n"
            "Contact W1NNURS to confirm quantity and shipping."
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Accept / Order",
                    url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}",
                ),
                InlineKeyboardButton(
                    "💬 Negotiate",
                    url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}",
                ),
            ]
        ]),
    )

    await update.message.reply_text(f"✅ Offer €{price_display}/pc sent.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON missing")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("stock", stock_command))
    app.add_handler(CommandHandler("hub", hub_command))
    app.add_handler(CommandHandler("id", idcmd))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_price_text))
    app.run_polling()


if __name__ == "__main__":
    main()