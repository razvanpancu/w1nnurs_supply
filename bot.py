import os
import json
import re
import html
import asyncio
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
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@yourusername")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/yourchannel")
BOT_USERNAME = os.getenv("BOT_USERNAME", "W1nnursSupplyBot").lstrip("@")
ANNOUNCEMENT_CHAT_ID = os.getenv("ANNOUNCEMENT_CHAT_ID", "").strip()
STOCK_WATCH_SECONDS = max(int(os.getenv("STOCK_WATCH_SECONDS", "300")), 60)

def parse_admin_ids(raw):
    # Accepts commas, spaces, brackets or quotes.
    return {int(x) for x in re.findall(r"\d+", raw or "")}

ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))

# Values stored only in memory. A Railway restart clears open price requests.
price_requests = {}
pending_admin_price = {}
request_counter = 0
stock_snapshot = {}
stock_snapshot_ready = False
reservation_sessions = {}
reservation_requests = {}
reservation_counter = 0
carts = {}
orders = {}
order_counter = 0


def google_client():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return gspread.authorize(creds)


def workbook():
    return google_client().open_by_key(SPREADSHEET_ID)


def get_tabs():
    """Return every visible worksheet dynamically. No brand list is hardcoded."""
    return [
        {"id": int(ws.id), "title": ws.title}
        for ws in workbook().worksheets()
        if not getattr(ws, "isSheetHidden", False)
    ]


def worksheet_by_id(sheet_id):
    return workbook().get_worksheet_by_id(int(sheet_id))


def stock_deep_link():
    return f"https://t.me/{BOT_USERNAME}?start=stock"


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Latest Drops", callback_data="latest")],
        [InlineKeyboardButton("📦 Available Stock", callback_data="brands:0")],
        [InlineKeyboardButton("🤝 W1NNURS Partners", callback_data="partners")],
        [InlineKeyboardButton("🛒 Order / Reserve", callback_data="order")],
        [InlineKeyboardButton("💬 Support", callback_data="support")],
    ])



def welcome_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 OPEN LIVE STOCK", url=stock_deep_link())],
        [
            InlineKeyboardButton("🛒 HOW TO ORDER", callback_data="welcome_order"),
            InlineKeyboardButton("💬 SUPPORT", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"),
        ],
        [InlineKeyboardButton("🔥 LATEST DROPS", url=CHANNEL_URL)],
    ])


async def is_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in ADMIN_IDS:
        return True

    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            return member.status in ("administrator", "creator")
        except Exception:
            return False

    return False


async def welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_or_owner(update, context):
        await update.message.reply_text(
            "⛔ Admin only.\n\n"
            f"Your Telegram ID: <code>{update.effective_user.id}</code>",
            parse_mode="HTML",
        )
        return

    text = (
        "🏆 <b>WELCOME TO W1NNURS RESELL CLUB</b>\n\n"
        "Private reseller community powered by W1NNURS.\n\n"
        "📦 Live wholesale stock\n"
        "💰 Personal reseller pricing\n"
        "🔥 New drops & restocks\n"
        "🔎 StockX & market research\n"
        "🚚 EU Shipping\n"
        "🤝 Direct support\n\n"
        "<b>HOW IT WORKS</b>\n"
        "1️⃣ Check the live stock\n"
        "2️⃣ Choose your product & size\n"
        "3️⃣ Request your personal price\n"
        "4️⃣ Receive the offer privately\n"
        "5️⃣ Reserve & resell\n\n"
        "🏆 <b>BUY. RESELL. WIN.</b>\n\n"
        "Everything you need is below 👇"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=welcome_keyboard(),
        disable_web_page_preview=True,
    )


def looks_like_size(header):
    """
    Automatically recognize common apparel / footwear size headers.
    Examples:
    XXS XS S M L XL XXL XXXL
    OS / ONE SIZE
    EU30 EU32 EU36 EU42
    US8 US8.5 UK7 UK7.5
    W30 W32 / 30W
    numeric shoe sizes: 5, 5.5, 6 ... 18
    """
    h = str(header or "").strip().upper().replace(" ", "")
    if not h:
        return False

    fixed = {
        "XXXS", "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "XXXXL",
        "OS", "OSFA", "ONESIZE", "ONE-SIZE",
    }
    if h in fixed:
        return True

    patterns = [
        r"EU\d{1,2}(?:[.,]\d)?",
        r"US\d{1,2}(?:[.,]\d)?",
        r"UK\d{1,2}(?:[.,]\d)?",
        r"W\d{1,2}",
        r"\d{1,2}W",
        r"\d{1,2}X\d{1,2}",
    ]
    if any(re.fullmatch(p, h) for p in patterns):
        return True

    # Bare numeric headers are treated as shoe sizes only in a reasonable range.
    try:
        n = float(h.replace(",", "."))
        return 1 <= n <= 20
    except Exception:
        return False


def to_qty(value):
    try:
        text = str(value or "").strip().replace(",", ".")
        if not text:
            return 0
        n = int(float(text))
        return max(n, 0)
    except Exception:
        return 0


def detect_product_column(headers):
    normalized = [str(x or "").strip().upper() for x in headers]
    aliases = {
        "PRODUCT NAME", "PRODUCT", "ITEM", "ITEM NAME", "NAME",
        "MODEL", "MODEL NAME", "DESCRIPTION",
    }
    for i, h in enumerate(normalized):
        if h in aliases:
            return i
    # Current catalog often has a blank or unusual header in column A.
    return 0


def detect_sku_column(headers):
    normalized = [str(x or "").strip().upper() for x in headers]
    aliases = {"SKU", "STYLE CODE", "STYLE", "CODE", "PRODUCT CODE"}
    for i, h in enumerate(normalized):
        if h in aliases:
            return i
    return None


def load_sheet_stock(sheet_id):
    """
    Reads a sheet live and automatically detects:
    - product column
    - SKU column
    - all size columns
    Cost/Price/Info/Image columns are never exposed.
    """
    ws = worksheet_by_id(sheet_id)
    rows = ws.get_all_values()

    if not rows:
        return {"title": ws.title, "products": []}

    headers = rows[0]
    product_idx = detect_product_column(headers)
    sku_idx = detect_sku_column(headers)

    size_cols = []
    for col_idx, header in enumerate(headers):
        if col_idx == product_idx or col_idx == sku_idx:
            continue
        if looks_like_size(header):
            size_cols.append((col_idx, str(header).strip()))

    products = []
    for row_num, row in enumerate(rows[1:], start=2):
        name = row[product_idx].strip() if product_idx < len(row) else ""
        if not name:
            continue

        # Skip repeated header rows.
        if name.strip().upper() in {"PRODUCT NAME", "PRODUCT", "ITEM", "NAME"}:
            continue

        sku = ""
        if sku_idx is not None and sku_idx < len(row):
            sku = row[sku_idx].strip()

        sizes = {}
        for col_idx, size_label in size_cols:
            value = row[col_idx] if col_idx < len(row) else ""
            qty = to_qty(value)
            if qty > 0:
                sizes[size_label] = qty

        if sizes:
            products.append({
                "row": row_num,
                "name": name,
                "sku": sku,
                "sizes": sizes,
            })

    return {"title": ws.title, "products": products}


def research_urls(product_name):
    q = quote_plus(product_name)
    return {
        "stockx": f"https://stockx.com/search?s={q}",
        "google": f"https://www.google.com/search?q={q}",
        "images": f"https://www.google.com/search?tbm=isch&q={q}",
    }


def find_product(sheet_id, row_num):
    data = load_sheet_stock(sheet_id)
    for product in data["products"]:
        if product["row"] == int(row_num):
            return data["title"], product
    return data["title"], None


async def send_brands_message(message, page=0):
    tabs = get_tabs()

    if not tabs:
        await message.reply_text("📦 No catalog tabs found.")
        return

    per_page = 12
    total_pages = (len(tabs) + per_page - 1) // per_page
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page

    buttons = []
    row = []
    for tab in tabs[start:start + per_page]:
        row.append(
            InlineKeyboardButton(
                tab["title"][:28],
                callback_data=f"brand:{tab['id']}:0",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"brands:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"brands:{page+1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="back")])

    await message.reply_text(
        "📦 <b>AVAILABLE STOCK</b>\n\nChoose a brand:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def edit_brands(query, page=0):
    tabs = get_tabs()

    if not tabs:
        await query.edit_message_text("📦 No catalog tabs found.")
        return

    per_page = 12
    total_pages = (len(tabs) + per_page - 1) // per_page
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page

    buttons = []
    row = []
    for tab in tabs[start:start + per_page]:
        row.append(
            InlineKeyboardButton(
                tab["title"][:28],
                callback_data=f"brand:{tab['id']}:0",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"brands:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"brands:{page+1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="back")])

    await query.edit_message_text(
        "📦 <b>AVAILABLE STOCK</b>\n\nChoose a brand:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].lower() == "stock":
        await send_brands_message(update.message)
        return

    await update.message.reply_text(
        "🏆 <b>W1NNURS SUPPLY</b>\n\n"
        "Welcome to W1NNURS SUPPLY.\n"
        "Your private access to reseller stock.\n\n"
        "Choose an option:",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text(
            "📦 <b>W1NNURS LIVE STOCK</b>\n\n"
            "Open the private catalog to check live stock, research products "
            "and request a personal price.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 OPEN LIVE STOCK", url=stock_deep_link())]
            ]),
        )
        return

    await send_brands_message(update.message)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Your Telegram numeric ID is:\n<code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def show_brand_stock(query, sheet_id, page=0):
    try:
        data = load_sheet_stock(sheet_id)
    except Exception:
        await query.edit_message_text(
            "⚠️ I couldn't read this brand right now.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Brands", callback_data="brands:0")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="back")],
            ]),
        )
        return

    title = data["title"]
    items = data["products"]

    if not items:
        await query.edit_message_text(
            f"📦 <b>{html.escape(title)}</b>\n\nNo products with stock right now.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Brands", callback_data="brands:0")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="back")],
            ]),
        )
        return

    per_page = 8
    total_pages = (len(items) + per_page - 1) // per_page
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page

    buttons = []
    for product in items[start:start + per_page]:
        label = product["name"]
        if len(label) > 45:
            label = label[:42] + "..."
        buttons.append([
            InlineKeyboardButton(
                f"📦 {label}",
                callback_data=f"product:{sheet_id}:{product['row']}:{page}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"brand:{sheet_id}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"brand:{sheet_id}:{page+1}"))

    buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ Brands", callback_data="brands:0")])
    buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="back")])

    await query.edit_message_text(
        f"📦 <b>{html.escape(title)}</b>\n\n"
        f"{len(items)} products currently in stock.\n"
        "Select a product:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_product(query, sheet_id, row_num, brand_page):
    title, product = find_product(sheet_id, row_num)

    if not product:
        await query.answer(
            "Stock changed or this product is no longer available.",
            show_alert=True,
        )
        return

    urls = research_urls(product["name"])

    size_lines = "\n".join(
        f"• {html.escape(str(size))}: {qty} pcs"
        for size, qty in product["sizes"].items()
    )

    sku_line = (
        f"\nSKU: <code>{html.escape(product['sku'])}</code>"
        if product["sku"]
        else ""
    )

    buttons = []
    for size in product["sizes"]:
        # Callback remains comfortably under Telegram's 64-byte limit.
        buttons.append([
            InlineKeyboardButton(
                f"💰 Ask Price • {size}",
                callback_data=f"ask:{sheet_id}:{row_num}:{size}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🛒 Add to Order",
            callback_data=f"cartadd:{sheet_id}:{row_num}:{brand_page}",
        )
    ])
    buttons.append([
        InlineKeyboardButton("📈 Search StockX", url=urls["stockx"]),
        InlineKeyboardButton("🔎 Google", url=urls["google"]),
    ])
    buttons.append([
        InlineKeyboardButton("🖼 Google Images", url=urls["images"])
    ])
    buttons.append([
        InlineKeyboardButton(
            "⬅️ Back to Brand",
            callback_data=f"brand:{sheet_id}:{brand_page}",
        )
    ])
    buttons.append([InlineKeyboardButton("📦 All Brands", callback_data="brands:0")])
    buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="back")])

    await query.edit_message_text(
        f"📦 <b>{html.escape(product['name'])}</b>"
        f"{sku_line}\n\n"
        f"<b>Available:</b>\n{size_lines}\n\n"
        "💰 Price available on request.\n"
        "🔎 Use the research buttons for market references, photos and product information.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )




def get_cart(user_id):
    return carts.setdefault(user_id, [])


async def cart_choose_size(query, sheet_id, row_num, brand_page):
    title, product = find_product(sheet_id, row_num)
    if not product:
        await query.answer("This product is no longer available.", show_alert=True)
        return

    buttons = []
    for size, qty in product["sizes"].items():
        buttons.append([
            InlineKeyboardButton(
                f"{size} • {qty} pcs",
                callback_data=f"cartsize:{sheet_id}:{row_num}:{brand_page}:{size}",
            )
        ])
    buttons.append([
        InlineKeyboardButton("🛒 VIEW ORDER", callback_data="cartview"),
        InlineKeyboardButton("⬅️ Back", callback_data=f"product:{sheet_id}:{row_num}:{brand_page}"),
    ])

    await query.edit_message_text(
        f"🛒 <b>ADD TO ORDER</b>\n\n"
        f"<b>{html.escape(product['name'])}</b>\n"
        f"Brand: {html.escape(title)}\n\n"
        "Choose a size:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cart_choose_qty(query, sheet_id, row_num, brand_page, size):
    title, product = find_product(sheet_id, row_num)
    if not product or size not in product["sizes"]:
        await query.answer("This size is no longer available.", show_alert=True)
        return

    max_qty = min(int(product["sizes"][size]), 10)
    buttons, row = [], []
    for qty in range(1, max_qty + 1):
        row.append(InlineKeyboardButton(
            str(qty),
            callback_data=f"cartqty:{sheet_id}:{row_num}:{brand_page}:{size}:{qty}",
        ))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("⬅️ Back to Sizes", callback_data=f"cartadd:{sheet_id}:{row_num}:{brand_page}")
    ])

    await query.edit_message_text(
        f"🛒 <b>ADD TO ORDER</b>\n\n"
        f"<b>{html.escape(product['name'])}</b>\n"
        f"Size: <b>{html.escape(str(size))}</b>\n"
        f"Available: <b>{product['sizes'][size]} pcs</b>\n\n"
        "Choose quantity:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cart_add_item(query, sheet_id, row_num, brand_page, size, qty):
    title, product = find_product(sheet_id, row_num)
    if not product or size not in product["sizes"]:
        await query.answer("This size is no longer available.", show_alert=True)
        return

    available = int(product["sizes"][size])
    if qty < 1 or qty > available:
        await query.answer("Requested quantity is no longer available.", show_alert=True)
        return

    cart = get_cart(query.from_user.id)
    existing = next(
        (x for x in cart if x["sheet_id"] == sheet_id and x["row_num"] == row_num and x["size"] == size),
        None,
    )
    if existing:
        new_qty = existing["qty"] + qty
        if new_qty > available:
            await query.answer("Cart quantity would exceed current stock.", show_alert=True)
            return
        existing["qty"] = new_qty
        existing["available"] = available
    else:
        cart.append({
            "sheet_id": sheet_id,
            "row_num": row_num,
            "brand": title,
            "product": product["name"],
            "sku": product["sku"],
            "size": size,
            "qty": qty,
            "available": available,
        })

    await query.edit_message_text(
        "✅ <b>ADDED TO ORDER</b>\n\n"
        f"{html.escape(product['name'])}\n"
        f"Size: <b>{html.escape(str(size))}</b> • Qty: <b>{qty}</b>\n\n"
        f"Items in order: <b>{sum(x['qty'] for x in cart)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ CONTINUE SHOPPING", callback_data="brands:0")],
            [InlineKeyboardButton("🛒 VIEW ORDER", callback_data="cartview")],
        ]),
    )


async def show_cart(query):
    cart = get_cart(query.from_user.id)
    if not cart:
        await query.edit_message_text(
            "🛒 <b>YOUR ORDER</b>\n\nYour order is empty.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 OPEN LIVE STOCK", callback_data="brands:0")]
            ]),
        )
        return

    lines = ["🛒 <b>YOUR ORDER</b>\n"]
    buttons = []
    for i, item in enumerate(cart):
        lines.append(
            f"\n<b>{i+1}. {html.escape(item['brand'])}</b>\n"
            f"{html.escape(item['product'])}\n"
            f"Size: <b>{html.escape(str(item['size']))}</b> • Qty: <b>{item['qty']}</b>"
        )
        buttons.append([
            InlineKeyboardButton(f"➖ {i+1}", callback_data=f"cartdec:{i}"),
            InlineKeyboardButton(f"➕ {i+1}", callback_data=f"cartinc:{i}"),
            InlineKeyboardButton(f"🗑 {i+1}", callback_data=f"cartremove:{i}"),
        ])

    lines.append(f"\n\nTotal pieces: <b>{sum(x['qty'] for x in cart)}</b>")
    buttons.extend([
        [InlineKeyboardButton("➕ CONTINUE SHOPPING", callback_data="brands:0")],
        [InlineKeyboardButton("✅ SUBMIT ORDER", callback_data="cartsubmit")],
        [InlineKeyboardButton("🗑 CLEAR ORDER", callback_data="cartclear")],
    ])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def submit_cart(query):
    global order_counter
    cart = get_cart(query.from_user.id)
    if not cart:
        await query.answer("Your order is empty.", show_alert=True)
        return

    # Recheck live stock before submitting
    refreshed = []
    for item in cart:
        title, product = find_product(item["sheet_id"], item["row_num"])
        if not product or item["size"] not in product["sizes"]:
            await query.answer(f"{item['product']} / {item['size']} is no longer available.", show_alert=True)
            return
        available = int(product["sizes"][item["size"]])
        if item["qty"] > available:
            await query.answer(
                f"Only {available} pcs left for {item['product']} / {item['size']}.",
                show_alert=True,
            )
            return
        refreshed.append(dict(item, available=available))

    order_counter += 1
    order_id = order_counter
    orders[order_id] = {
        "order_id": order_id,
        "user_id": query.from_user.id,
        "username": query.from_user.username or "",
        "first_name": query.from_user.first_name or "",
        "items": refreshed,
        "status": "PENDING_APPROVAL",
    }
    carts[query.from_user.id] = []

    requester = f"@{query.from_user.username}" if query.from_user.username else f"{query.from_user.first_name} ({query.from_user.id})"
    item_lines = []
    for i, item in enumerate(refreshed, 1):
        item_lines.append(
            f"{i}. <b>{html.escape(item['brand'])}</b> — {html.escape(item['product'])}\n"
            f"   Size: <b>{html.escape(str(item['size']))}</b> • Qty: <b>{item['qty']}</b>"
        )
    total = sum(x["qty"] for x in refreshed)

    admin_text = (
        "🛒 <b>NEW MULTI-BRAND ORDER</b>\n\n"
        f"Order: <code>#O{order_id}</code>\n"
        f"Reseller: {html.escape(requester)}\n\n"
        + "\n\n".join(item_lines)
        + f"\n\nTotal pieces: <b>{total}</b>\nStatus: <b>Pending Approval</b>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await query.get_bot().send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ APPROVE ORDER", callback_data=f"orderapprove:{order_id}"),
                        InlineKeyboardButton("❌ DECLINE", callback_data=f"orderdecline:{order_id}"),
                    ],
                    [InlineKeyboardButton("💬 OPEN USER", url=f"tg://user?id={query.from_user.id}")],
                ]),
            )
        except Exception:
            pass

    await query.edit_message_text(
        "✅ <b>ORDER SUBMITTED</b>\n\n"
        f"Order: <code>#O{order_id}</code>\n"
        f"Products: <b>{len(refreshed)}</b>\n"
        f"Total pieces: <b>{total}</b>\n\n"
        "W1NNURS will review and confirm your complete order.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 MY ORDERS", callback_data="myorders")],
            [InlineKeyboardButton("📦 CONTINUE SHOPPING", callback_data="brands:0")],
        ]),
    )


ORDER_STATUS_LABELS = {
    "PENDING_APPROVAL": "🕒 Pending Approval",
    "AWAITING_PAYMENT": "⏳ Awaiting Payment",
    "PAID": "💰 Paid",
    "PREPARING": "📦 Preparing",
    "SHIPPED": "🚚 Shipped",
    "COMPLETED": "🏁 Completed",
    "DECLINED": "❌ Declined",
}


async def show_my_orders(query):
    user_orders = [o for o in orders.values() if o["user_id"] == query.from_user.id]
    user_orders.sort(key=lambda x: x["order_id"], reverse=True)
    if not user_orders:
        await query.edit_message_text(
            "📋 <b>MY ORDERS</b>\n\nNo orders yet.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 OPEN LIVE STOCK", callback_data="brands:0")]]),
        )
        return

    lines = ["📋 <b>MY ORDERS</b>\n"]
    buttons = []
    for order in user_orders[-10:]:
        status = ORDER_STATUS_LABELS.get(order["status"], order["status"])
        pieces = sum(x["qty"] for x in order["items"])
        lines.append(f"\n<code>#O{order['order_id']}</code> • {len(order['items'])} products • {pieces} pcs\n<b>{status}</b>")
        buttons.append([InlineKeyboardButton(f"#O{order['order_id']} • Details", callback_data=f"orderdetails:{order['order_id']}")])
    buttons.append([InlineKeyboardButton("📦 OPEN LIVE STOCK", callback_data="brands:0")])
    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def show_order_details(query, order_id):
    order = orders.get(order_id)
    if not order or order["user_id"] != query.from_user.id:
        await query.answer("Order not found.", show_alert=True)
        return
    lines = [f"📋 <b>ORDER #O{order_id}</b>\n"]
    for i, item in enumerate(order["items"], 1):
        lines.append(
            f"\n{i}. <b>{html.escape(item['brand'])}</b>\n"
            f"{html.escape(item['product'])}\n"
            f"Size: <b>{html.escape(str(item['size']))}</b> • Qty: <b>{item['qty']}</b>"
        )
    lines.append(f"\n\nStatus: <b>{ORDER_STATUS_LABELS.get(order['status'], order['status'])}</b>")
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ MY ORDERS", callback_data="myorders")],
            [InlineKeyboardButton("💬 SUPPORT", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
        ]),
    )


async def admin_approve_order(query, order_id):
    order = orders.get(order_id)
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Admin only.", show_alert=True)
        return
    order["status"] = "AWAITING_PAYMENT"
    await query.get_bot().send_message(
        chat_id=order["user_id"],
        text=f"✅ <b>ORDER #O{order_id} APPROVED</b>\n\nStatus: <b>⏳ Awaiting Payment</b>\nContact W1NNURS to finalize payment.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 MY ORDERS", callback_data="myorders")],
            [InlineKeyboardButton("💬 CONTACT W1NNURS", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
        ]),
    )
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 PAID", callback_data=f"orderstatus:{order_id}:PAID"),
         InlineKeyboardButton("📦 PREPARING", callback_data=f"orderstatus:{order_id}:PREPARING")],
        [InlineKeyboardButton("🚚 SHIPPED", callback_data=f"orderstatus:{order_id}:SHIPPED"),
         InlineKeyboardButton("🏁 COMPLETED", callback_data=f"orderstatus:{order_id}:COMPLETED")],
    ]))
    await query.answer("Order approved.")


async def admin_decline_order(query, order_id):
    order = orders.get(order_id)
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Admin only.", show_alert=True)
        return
    order["status"] = "DECLINED"
    await query.get_bot().send_message(
        chat_id=order["user_id"],
        text=f"❌ <b>ORDER #O{order_id} NOT CONFIRMED</b>\n\nContact W1NNURS for alternatives or updated availability.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 SUPPORT", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")]]),
    )
    await query.edit_message_reply_markup(reply_markup=None)
    await query.answer("Order declined.")


async def admin_order_status(query, order_id, status):
    order = orders.get(order_id)
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Admin only.", show_alert=True)
        return
    if status not in ("PAID", "PREPARING", "SHIPPED", "COMPLETED"):
        await query.answer("Invalid status.", show_alert=True)
        return
    order["status"] = status
    await query.get_bot().send_message(
        chat_id=order["user_id"],
        text=f"📦 <b>ORDER UPDATE</b>\n\nOrder: <code>#O{order_id}</code>\nNew status: <b>{ORDER_STATUS_LABELS[status]}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 MY ORDERS", callback_data="myorders")]]),
    )
    await query.answer("Status updated.")



async def start_reservation(query, sheet_id, row_num, brand_page):
    title, product = find_product(sheet_id, row_num)
    if not product:
        await query.answer("This product is no longer available.", show_alert=True)
        return

    buttons = []
    for size, qty in product["sizes"].items():
        buttons.append([
            InlineKeyboardButton(
                f"{size} • {qty} pcs",
                callback_data=f"rsize:{sheet_id}:{row_num}:{brand_page}:{size}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Back to Product",
            callback_data=f"product:{sheet_id}:{row_num}:{brand_page}",
        )
    ])

    await query.edit_message_text(
        f"🛒 <b>RESERVE / ORDER</b>\n\n"
        f"<b>{html.escape(product['name'])}</b>\n"
        f"Brand: {html.escape(title)}\n\n"
        "Choose a size:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def choose_reservation_size(query, sheet_id, row_num, brand_page, size):
    title, product = find_product(sheet_id, row_num)
    if not product or size not in product["sizes"]:
        await query.answer("This size is no longer available.", show_alert=True)
        return

    max_qty = min(product["sizes"][size], 10)
    buttons = []
    row = []
    for qty in range(1, max_qty + 1):
        row.append(
            InlineKeyboardButton(
                str(qty),
                callback_data=f"rqty:{sheet_id}:{row_num}:{brand_page}:{size}:{qty}",
            )
        )
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Back to Sizes",
            callback_data=f"reserve:{sheet_id}:{row_num}:{brand_page}",
        )
    ])

    await query.edit_message_text(
        f"🛒 <b>RESERVE / ORDER</b>\n\n"
        f"<b>{html.escape(product['name'])}</b>\n"
        f"Size: <b>{html.escape(str(size))}</b>\n"
        f"Available: <b>{product['sizes'][size]} pcs</b>\n\n"
        "Choose quantity:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def confirm_reservation(query, sheet_id, row_num, brand_page, size, qty):
    global reservation_counter

    title, product = find_product(sheet_id, row_num)
    if not product or size not in product["sizes"]:
        await query.answer("This size is no longer available.", show_alert=True)
        return

    available = product["sizes"][size]
    if qty < 1 or qty > available:
        await query.answer("Requested quantity is no longer available.", show_alert=True)
        return

    reservation_counter += 1
    request_id = reservation_counter

    request = {
        "user_id": query.from_user.id,
        "username": query.from_user.username or "",
        "first_name": query.from_user.first_name or "",
        "brand": title,
        "product": product["name"],
        "sku": product["sku"],
        "size": size,
        "qty": qty,
        "available": available,
    }
    reservation_requests[request_id] = request

    requester = (
        f"@{query.from_user.username}"
        if query.from_user.username
        else f"{query.from_user.first_name} ({query.from_user.id})"
    )

    admin_text = (
        "🛒 <b>NEW RESERVATION REQUEST</b>\n\n"
        f"Reseller: {html.escape(requester)}\n"
        f"Brand: <b>{html.escape(title)}</b>\n"
        f"Product: <b>{html.escape(product['name'])}</b>\n"
        f"Size: <b>{html.escape(str(size))}</b>\n"
        f"Quantity: <b>{qty}</b>\n"
        f"Current stock: <b>{available}</b>\n"
        f"Request: <code>#R{request_id}</code>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await query.get_bot().send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "✅ Approve",
                            callback_data=f"rapprove:{request_id}",
                        ),
                        InlineKeyboardButton(
                            "❌ Decline",
                            callback_data=f"rdecline:{request_id}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "💬 Open User",
                            url=f"tg://user?id={query.from_user.id}",
                        )
                    ],
                ]),
            )
        except Exception:
            pass

    await query.edit_message_text(
        "✅ <b>RESERVATION REQUEST SENT</b>\n\n"
        f"{html.escape(product['name'])}\n"
        f"Size: <b>{html.escape(str(size))}</b>\n"
        f"Quantity: <b>{qty}</b>\n\n"
        "An admin will confirm availability and next steps.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Back to Stock", callback_data="brands:0")],
            [InlineKeyboardButton("💬 Support", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
        ]),
    )


async def approve_reservation(query, request_id):
    request = reservation_requests.get(request_id)
    if not request:
        await query.answer("Reservation expired.", show_alert=True)
        return

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Admin only.", show_alert=True)
        return

    try:
        await query.get_bot().send_message(
            chat_id=request["user_id"],
            text=(
                "✅ <b>RESERVATION APPROVED</b>\n\n"
                f"<b>{html.escape(request['product'])}</b>\n"
                f"Size: <b>{html.escape(str(request['size']))}</b>\n"
                f"Quantity: <b>{request['qty']}</b>\n\n"
                "Your reservation has been approved.\n"
                "Contact W1NNURS to finalize payment and shipping."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 CONTACT W1NNURS",
                        url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}",
                    )
                ]
            ]),
        )
    except Exception:
        pass

    await query.edit_message_text(
        query.message.text_html + "\n\n✅ <b>APPROVED</b>",
        parse_mode="HTML",
    )


async def decline_reservation(query, request_id):
    request = reservation_requests.get(request_id)
    if not request:
        await query.answer("Reservation expired.", show_alert=True)
        return

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Admin only.", show_alert=True)
        return

    try:
        await query.get_bot().send_message(
            chat_id=request["user_id"],
            text=(
                "❌ <b>RESERVATION NOT CONFIRMED</b>\n\n"
                f"<b>{html.escape(request['product'])}</b>\n"
                f"Size: <b>{html.escape(str(request['size']))}</b>\n"
                f"Quantity: <b>{request['qty']}</b>\n\n"
                "Contact W1NNURS for alternatives or updated availability."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 CONTACT W1NNURS",
                        url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}",
                    )
                ],
                [InlineKeyboardButton("📦 OPEN LIVE STOCK", url=stock_deep_link())],
            ]),
        )
    except Exception:
        pass

    await query.edit_message_text(
        query.message.text_html + "\n\n❌ <b>DECLINED</b>",
        parse_mode="HTML",
    )



async def ask_price(query, sheet_id, row_num, size):
    global request_counter

    title, product = find_product(sheet_id, row_num)

    if not product or size not in product["sizes"]:
        await query.answer(
            "This size is no longer available.",
            show_alert=True,
        )
        return

    if not ADMIN_IDS:
        await query.answer("Admin is not configured yet.", show_alert=True)
        return

    request_counter += 1
    request_id = request_counter

    price_requests[request_id] = {
        "user_id": query.from_user.id,
        "username": query.from_user.username or "",
        "first_name": query.from_user.first_name or "",
        "brand": title,
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

    admin_text = (
        "💰 <b>NEW PRICE REQUEST</b>\n\n"
        f"Reseller: {html.escape(requester)}\n"
        f"Brand: <b>{html.escape(title)}</b>\n"
        f"Product: <b>{html.escape(product['name'])}</b>\n"
        f"Size: <b>{html.escape(str(size))}</b>\n"
        f"Available: <b>{product['sizes'][size]} pcs</b>\n"
        f"Request: <code>#{request_id}</code>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await query.get_bot().send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "💶 Reply with Price",
                            callback_data=f"adminprice:{request_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "💬 Open User",
                            url=f"tg://user?id={query.from_user.id}",
                        )
                    ],
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

    if data == "welcome_order":
        await query.answer()
        await query.message.reply_text(
            "🛒 <b>HOW TO ORDER</b>\n\n"
            "1️⃣ Open Live Stock\n"
            "2️⃣ Choose a brand\n"
            "3️⃣ Open the product you want\n"
            "4️⃣ Select your size and press <b>Ask Price</b>\n"
            "5️⃣ An admin sends you a personal offer\n"
            "6️⃣ Accept or contact us to negotiate\n\n"
            "For bulk orders, message W1NNURS directly.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 OPEN LIVE STOCK", url=stock_deep_link())],
                [InlineKeyboardButton("💬 CONTACT W1NNURS", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
            ]),
        )
        return

    if data.startswith("brands:"):
        page = int(data.split(":", 1)[1])
        await query.answer()
        await edit_brands(query, page)
        return

    if data.startswith("brand:"):
        _, sheet_id, page = data.split(":", 2)
        await query.answer()
        await show_brand_stock(query, int(sheet_id), int(page))
        return

    if data.startswith("product:"):
        _, sheet_id, row_num, brand_page = data.split(":", 3)
        await query.answer()
        await show_product(
            query,
            int(sheet_id),
            int(row_num),
            int(brand_page),
        )
        return

    if data.startswith("cartadd:"):
        _, sheet_id, row_num, brand_page = data.split(":", 3)
        await query.answer()
        await cart_choose_size(query, int(sheet_id), int(row_num), int(brand_page))
        return

    if data.startswith("cartsize:"):
        _, sheet_id, row_num, brand_page, size = data.split(":", 4)
        await query.answer()
        await cart_choose_qty(query, int(sheet_id), int(row_num), int(brand_page), size)
        return

    if data.startswith("cartqty:"):
        _, sheet_id, row_num, brand_page, size, qty = data.split(":", 5)
        await query.answer()
        await cart_add_item(query, int(sheet_id), int(row_num), int(brand_page), size, int(qty))
        return

    if data == "cartview":
        await query.answer()
        await show_cart(query)
        return

    if data.startswith("cartremove:"):
        idx = int(data.split(":", 1)[1])
        cart = get_cart(query.from_user.id)
        if 0 <= idx < len(cart):
            cart.pop(idx)
        await query.answer("Removed.")
        await show_cart(query)
        return

    if data.startswith("cartinc:"):
        idx = int(data.split(":", 1)[1])
        cart = get_cart(query.from_user.id)
        if 0 <= idx < len(cart):
            item = cart[idx]
            _, product = find_product(item["sheet_id"], item["row_num"])
            available = int(product["sizes"].get(item["size"], 0)) if product else 0
            if item["qty"] < available:
                item["qty"] += 1
                await query.answer("Quantity updated.")
            else:
                await query.answer("No more stock available.", show_alert=True)
        await show_cart(query)
        return

    if data.startswith("cartdec:"):
        idx = int(data.split(":", 1)[1])
        cart = get_cart(query.from_user.id)
        if 0 <= idx < len(cart):
            if cart[idx]["qty"] > 1:
                cart[idx]["qty"] -= 1
            else:
                cart.pop(idx)
        await query.answer("Quantity updated.")
        await show_cart(query)
        return

    if data == "cartclear":
        carts[query.from_user.id] = []
        await query.answer("Order cleared.")
        await show_cart(query)
        return

    if data == "cartsubmit":
        await query.answer()
        await submit_cart(query)
        return

    if data == "myorders":
        await query.answer()
        await show_my_orders(query)
        return

    if data.startswith("orderdetails:"):
        order_id = int(data.split(":", 1)[1])
        await query.answer()
        await show_order_details(query, order_id)
        return

    if data.startswith("orderapprove:"):
        await admin_approve_order(query, int(data.split(":", 1)[1]))
        return

    if data.startswith("orderdecline:"):
        await admin_decline_order(query, int(data.split(":", 1)[1]))
        return

    if data.startswith("orderstatus:"):
        _, order_id, status = data.split(":", 2)
        await admin_order_status(query, int(order_id), status)
        return

    if data.startswith("reserve:"):
        _, sheet_id, row_num, brand_page = data.split(":", 3)
        await query.answer()
        await start_reservation(query, int(sheet_id), int(row_num), int(brand_page))
        return

    if data.startswith("rsize:"):
        _, sheet_id, row_num, brand_page, size = data.split(":", 4)
        await query.answer()
        await choose_reservation_size(
            query, int(sheet_id), int(row_num), int(brand_page), size
        )
        return

    if data.startswith("rqty:"):
        _, sheet_id, row_num, brand_page, size, qty = data.split(":", 5)
        await query.answer()
        await confirm_reservation(
            query, int(sheet_id), int(row_num), int(brand_page), size, int(qty)
        )
        return

    if data.startswith("rapprove:"):
        request_id = int(data.split(":", 1)[1])
        await query.answer()
        await approve_reservation(query, request_id)
        return

    if data.startswith("rdecline:"):
        request_id = int(data.split(":", 1)[1])
        await query.answer()
        await decline_reservation(query, request_id)
        return

    if data.startswith("ask:"):
        _, sheet_id, row_num, size = data.split(":", 3)
        await ask_price(query, int(sheet_id), int(row_num), size)
        return

    if data.startswith("adminprice:"):
        request_id = int(data.split(":", 1)[1])

        if query.from_user.id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return

        if request_id not in price_requests:
            await query.answer("This request expired.", show_alert=True)
            return

        pending_admin_price[query.from_user.id] = request_id
        await query.answer()
        await query.message.reply_text(
            "💶 Send the price you want to offer.\n\n"
            "Example: <code>72</code> or <code>72.50</code>",
            parse_mode="HTML",
        )
        return

    await query.answer()

    if data == "back":
        await query.edit_message_text(
            "🏆 <b>W1NNURS SUPPLY</b>\n\nChoose an option:",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    elif data == "latest":
        await query.edit_message_text(
            "🔥 <b>LATEST DROPS</b>\n\n"
            "See the W1NNURS SUPPLY channel for the newest drops.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📲 Open Channel", url=CHANNEL_URL)],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )

    elif data == "partners":
        await query.edit_message_text(
            "🤝 <b>W1NNURS PARTNERS</b>\n\n"
            "Private reseller access for approved partners.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 Contact",
                        url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}",
                    )
                ],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )

    elif data == "order":
        await query.edit_message_text(
            "🛒 <b>ORDER / RESERVE</b>\n\n"
            "Open Available Stock, choose a brand and request your personal price.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Available Stock", callback_data="brands:0")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )

    elif data == "support":
        await query.edit_message_text(
            f"💬 <b>W1NNURS SUPPORT</b>\n\n{html.escape(SUPPORT_USERNAME)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💬 Contact",
                        url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}",
                    )
                ],
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
            "Send a valid price, e.g. <code>72</code> or <code>72.50</code>.",
            parse_mode="HTML",
        )
        return

    pending_admin_price.pop(admin_id, None)
    price_display = f"{price:.2f}".rstrip("0").rstrip(".")

    await context.bot.send_message(
        chat_id=request["user_id"],
        text=(
            "🏆 <b>W1NNURS SUPPLY OFFER</b>\n\n"
            f"<b>{html.escape(request['product'])}</b>\n"
            f"Size: <b>{html.escape(str(request['size']))}</b>\n"
            f"Your price: <b>€{price_display} / pc</b>\n\n"
            "Contact W1NNURS to confirm quantity and shipping."
        ),
        parse_mode="HTML",
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

    await update.message.reply_text(
        f"✅ Offer €{price_display}/pc sent."
    )



def resolve_announcement_chat():
    """
    Preferred: ANNOUNCEMENT_CHAT_ID in Railway.
    It may be a numeric chat ID (-100...) or @publicusername.
    Fallback: if CHANNEL_URL is https://t.me/publicusername, use @publicusername.
    """
    if ANNOUNCEMENT_CHAT_ID:
        value = ANNOUNCEMENT_CHAT_ID
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        return value if value.startswith("@") else f"@{value}"

    m = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]+)/*", CHANNEL_URL.strip())
    if m:
        return f"@{m.group(1)}"

    return None


def build_live_snapshot():
    """
    Snapshot key: sheet_id:row_number.
    We intentionally store only public stock information; prices are never read.
    """
    snap = {}
    for tab in get_tabs():
        try:
            data = load_sheet_stock(tab["id"])
        except Exception:
            continue

        for product in data["products"]:
            key = f"{tab['id']}:{product['row']}"
            snap[key] = {
                "brand": data["title"],
                "product": product["name"],
                "sku": product["sku"],
                "sizes": dict(product["sizes"]),
            }
    return snap


def compare_stock(old, new):
    """
    NEW DROP = product did not exist in previous live-stock snapshot.
    RESTOCK = an existing product gained a size or its positive quantity increased.
    """
    changes = []

    for key, item in new.items():
        before = old.get(key)

        if before is None:
            changes.append({
                "type": "NEW DROP",
                "brand": item["brand"],
                "product": item["product"],
                "sizes": item["sizes"],
            })
            continue

        increased = {}
        old_sizes = before.get("sizes", {})
        for size, qty in item["sizes"].items():
            previous = int(old_sizes.get(size, 0))
            if qty > previous:
                increased[size] = qty - previous

        if increased:
            changes.append({
                "type": "RESTOCK",
                "brand": item["brand"],
                "product": item["product"],
                "sizes": item["sizes"],
                "added": increased,
            })

    return changes


def format_stock_change(change):
    if change["type"] == "NEW DROP":
        sizes = " • ".join(
            f"{html.escape(str(size))}: {qty}"
            for size, qty in change["sizes"].items()
        )
        return (
            "🔥 <b>NEW DROP</b>\n\n"
            f"🏷 <b>{html.escape(change['brand'])}</b>\n"
            f"📦 <b>{html.escape(change['product'])}</b>\n\n"
            f"Available: {sizes}\n\n"
            "Live now in W1NNURS SUPPLY. 🏆"
        )

    added = " • ".join(
        f"{html.escape(str(size))}: +{qty}"
        for size, qty in change["added"].items()
    )
    current = " • ".join(
        f"{html.escape(str(size))}: {qty}"
        for size, qty in change["sizes"].items()
    )
    return (
        "♻️ <b>RESTOCK</b>\n\n"
        f"🏷 <b>{html.escape(change['brand'])}</b>\n"
        f"📦 <b>{html.escape(change['product'])}</b>\n\n"
        f"Added: {added}\n"
        f"Current stock: {current}\n\n"
        "Back in stock now. 🏆"
    )


async def stock_watch_loop(app):
    global stock_snapshot, stock_snapshot_ready

    # Give Telegram/Railway a moment to finish startup.
    await asyncio.sleep(8)

    while True:
        try:
            current = await asyncio.to_thread(build_live_snapshot)

            # First successful scan becomes the baseline: no spam on deploy/restart.
            if not stock_snapshot_ready:
                stock_snapshot = current
                stock_snapshot_ready = True
            else:
                changes = compare_stock(stock_snapshot, current)
                stock_snapshot = current

                target = resolve_announcement_chat()
                if target and changes:
                    # Safety cap prevents a large spreadsheet edit from flooding Telegram.
                    visible = changes[:10]
                    for change in visible:
                        try:
                            await app.bot.send_message(
                                chat_id=target,
                                text=format_stock_change(change),
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("📦 OPEN LIVE STOCK", url=stock_deep_link())]
                                ]),
                                disable_web_page_preview=True,
                            )
                            await asyncio.sleep(1)
                        except Exception as exc:
                            print(f"Stock announcement failed: {exc}")

                    if len(changes) > 10:
                        try:
                            await app.bot.send_message(
                                chat_id=target,
                                text=(
                                    f"🔥 <b>{len(changes) - 10} more stock updates</b>\n\n"
                                    "Open Live Stock to see the full catalog."
                                ),
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("📦 OPEN LIVE STOCK", url=stock_deep_link())]
                                ]),
                            )
                        except Exception:
                            pass

        except Exception as exc:
            print(f"Stock watch error: {exc}")

        await asyncio.sleep(STOCK_WATCH_SECONDS)


async def post_init(application):
    application.create_task(stock_watch_loop(application))




async def watch_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    target = resolve_announcement_chat()
    target_text = html.escape(str(target)) if target else "NOT CONFIGURED"
    await update.message.reply_text(
        "📡 <b>STOCK WATCH STATUS</b>\n\n"
        f"Target: <code>{target_text}</code>\n"
        f"Check interval: <b>{STOCK_WATCH_SECONDS // 60} min</b>\n"
        f"Baseline loaded: <b>{'YES' if stock_snapshot_ready else 'STARTING...'}</b>\n\n"
        "The first scan creates a baseline and does not post old stock.",
        parse_mode="HTML",
    )



def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON missing")
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID missing")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("stock", stock_command))
    app.add_handler(CommandHandler("hub", stock_command))
    app.add_handler(CommandHandler("welcome", welcome_command))
    app.add_handler(CommandHandler("watchstatus", watch_status_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            admin_price_text,
        )
    )

    app.run_polling()


if __name__ == "__main__":
    main()