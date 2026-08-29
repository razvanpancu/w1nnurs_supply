import os
import json
import re
import html
import asyncio
import sqlite3
import json
from pathlib import Path
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

DB_PATH = os.getenv("DB_PATH", "/data/w1nnurs_bot.db").strip()

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
    v161_track(update.effective_user.id, "OPEN_STOCK")
    v20_track(update.effective_user.id, "OPEN_STOCK")
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





def db_connect():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS carts (
                user_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()


def load_persistent_state():
    global carts, orders, order_counter
    init_db()
    carts = {}
    orders = {}
    order_counter = 0

    with db_connect() as conn:
        for user_id, payload in conn.execute("SELECT user_id, payload FROM carts"):
            try:
                carts[int(user_id)] = json.loads(payload)
            except Exception:
                pass

        for order_id, user_id, payload in conn.execute("SELECT order_id, user_id, payload FROM orders"):
            try:
                order = json.loads(payload)
                order["order_id"] = int(order_id)
                order["user_id"] = int(user_id)
                orders[int(order_id)] = order
                order_counter = max(order_counter, int(order_id))
            except Exception:
                pass

        row = conn.execute("SELECT value FROM meta WHERE key='order_counter'").fetchone()
        if row:
            try:
                order_counter = max(order_counter, int(row[0]))
            except Exception:
                pass


def save_cart(user_id):
    cart = carts.get(user_id, [])
    with db_connect() as conn:
        if cart:
            conn.execute(
                "INSERT INTO carts(user_id, payload) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
                (int(user_id), json.dumps(cart, ensure_ascii=False)),
            )
        else:
            conn.execute("DELETE FROM carts WHERE user_id=?", (int(user_id),))
        conn.commit()


def save_order(order_id):
    order = orders.get(order_id)
    if not order:
        return
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO orders(order_id, user_id, payload) VALUES(?, ?, ?) "
            "ON CONFLICT(order_id) DO UPDATE SET user_id=excluded.user_id, payload=excluded.payload",
            (
                int(order_id),
                int(order["user_id"]),
                json.dumps(order, ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('order_counter', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(order_id),),
        )
        conn.commit()


def delete_cart(user_id):
    carts[user_id] = []
    save_cart(user_id)



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
        save_cart(query.from_user.id)
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
        save_cart(query.from_user.id)

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
    save_order(order_id)
    delete_cart(query.from_user.id)

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
    save_order(order_id)
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
    save_order(order_id)
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
    save_order(order_id)
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




async def is_admin_or_owner_v12(update, context):
    if update.effective_user.id in ADMIN_IDS:
        return True
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(
                update.effective_chat.id, update.effective_user.id
            )
            return member.status in ("administrator", "creator")
        except Exception:
            return False
    return False








async def season_command_v17(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stats = v17_season_stats(user_id)
    ranking = v17_all_season_stats()
    rank = next((i for i, s in enumerate(ranking, 1) if s["user_id"] == user_id), None)

    lines = [
        "🏁 <b>W1NNURS SEASON</b>",
        f"<b>{v17_season_name()}</b>",
        "",
        f"⚡ Season Score: <b>{stats['score']}</b>",
        f"📦 Completed orders: <b>{stats['orders']}</b>",
        f"👕 Completed pieces: <b>{stats['pieces']}</b>",
        f"🏷 Brands: <b>{stats['brands']}</b>",
        f"🏆 Current rank: <b>#{rank}</b>" if rank else "🏆 Current rank: <b>Unranked</b>",
        "",
        "<i>Season Score resets every month. Permanent XP and league progress do not reset.</i>",
    ]

    buttons = [
        [InlineKeyboardButton("🏆 SEASON LEADERBOARD", callback_data="v17seasonboard")],
        [InlineKeyboardButton("🎯 MISSIONS", callback_data="v15league")],
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def render_season_board_v17(query):
    ranking = v17_all_season_stats()
    lines = [
        "🏆 <b>W1NNURS SEASON LEADERBOARD</b>",
        f"<b>{v17_season_name()}</b>",
        "",
    ]

    if not ranking:
        lines.append("No completed seasonal orders yet.")
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, stats in enumerate(ranking[:20], 1):
            icon = medals.get(i, f"{i}.")
            name = html.escape(v17_display_name(stats["user_id"]))
            lines.append(
                f"{icon} <b>{name}</b> — {stats['score']} pts\n"
                f"   {stats['pieces']} pcs • {stats['orders']} orders • {stats['brands']} brands"
            )

    lines.extend([
        "",
        "👑 <b>#1 = Season Champion</b>",
        "<i>Ranking resets automatically with the new month.</i>",
    ])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 REFRESH", callback_data="v17seasonboard")],
            [InlineKeyboardButton("🎯 MY MISSIONS", callback_data="v15league")],
        ]),
    )



async def advanced_command_v16(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats=v161_league_stats(update.effective_user.id)
    done=stats["advanced_missions"]
    beginner_done=all(stats["missions"].values())
    lines=["🔥 <b>W1NNURS ADVANCED MISSIONS</b>","",f"League: <b>{stats['league']}</b>",f"Total XP: <b>{stats['xp']}</b>",""]
    if not beginner_done:
        remaining=sum(1 for v in stats["missions"].values() if not v)
        lines += ["🔒 <b>ADVANCED MISSIONS LOCKED</b>","","Finish your Beginner Missions first.",f"Remaining beginner missions: <b>{remaining}</b>"]
    else:
        lines += ["🔓 <b>ADVANCED MISSIONS UNLOCKED</b>","<i>Harder objectives. XP & status progression.</i>",""]
        for key,title,desc,xp in V16_ADVANCED_MISSIONS:
            icon="✅" if done.get(key) else "⬜"
            lines.append(f"{icon} <b>{title}</b> • +{xp} XP\n   {desc} • <b>{v16_progress_text(update.effective_user.id,key)}</b>")
    await update.message.reply_text("\n".join(lines),parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎯 BEGINNER MISSIONS",callback_data="v15league")],
                                           [InlineKeyboardButton("🏆 MY PROFILE",callback_data="v15profile")]]))


async def league_command_v15(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = v161_league_stats(update.effective_user.id)
    done = stats["missions"]
    next_min, next_label, remaining = v15_next_level(stats["xp"])

    lines = [
        "🏆 <b>W1NNURS RESELLER LEAGUE</b>",
        "",
        f"League: <b>{stats['league']}</b>",
        f"XP: <b>{stats['xp']}</b>",
        "",
        "🎯 <b>BEGINNER MISSIONS</b>",
        "<i>These missions give XP & progress only.</i>",
        "",
    ]

    for key, title, description, xp in V161_BEGINNER_MISSIONS:
        icon = "✅" if done.get(key) else "⬜"
        lines.append(
            f"{icon} <b>{title}</b> • +{xp} XP\n"
            f"   {description} • <b>{v161_progress(update.effective_user.id, key)}</b>"
        )

    if next_min is not None:
        lines.extend([
            "",
            f"🚀 Next league: <b>{next_label}</b>",
            f"Need <b>{remaining} XP</b> more.",
        ])
    else:
        lines.extend(["", "👑 <b>Top league reached.</b>"])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 MY ORDERS", callback_data="myorders")],
            [InlineKeyboardButton("📈 TRY STOCKX", callback_data="v161research:stockx:streetwear")],
            [InlineKeyboardButton("🔎 TRY GOOGLE", callback_data="v161research:google:streetwear")],
            [InlineKeyboardButton("🖼 TRY IMAGES", callback_data="v161research:images:streetwear")],
            [InlineKeyboardButton("🔥 ADVANCED MISSIONS", callback_data="v16advanced")],
            [InlineKeyboardButton("🏆 MY PROFILE", callback_data="v15profile")],
        ]),
    )


async def league_ranking_command_v15(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_or_owner_v12(update, context):
        await update.message.reply_text("Admin only.")
        return

    stats_list = v15_all_league_stats()
    lines = [
        "🏆 <b>W1NNURS LEAGUE — ADMIN</b>",
        "",
        f"Resellers: <b>{len(stats_list)}</b>",
        "",
    ]

    for idx, stats in enumerate(stats_list[:20], 1):
        lines.append(
            f"{idx}. <b>{html.escape(reseller_display_name_v14(stats))}</b> — "
            f"{stats['league']} • <b>{stats['xp']} XP</b>"
        )

    if not stats_list:
        lines.append("No reseller activity yet.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")



async def profile_command_v14(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "🏆 Open W1NNURS Supply Bot privately to view your reseller profile.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🏆 MY PROFILE",
                    url=f"https://t.me/{BOT_USERNAME}?start=profile"
                )]
            ]),
        )
        return

    stats = reseller_stats_v14(update.effective_user.id)
    next_min, next_label, remaining = next_tier_progress_v14(stats["pieces_completed"])

    lines = [
        "🏆 <b>W1NNURS RESELLER PROFILE</b>",
        "",
        f"Reseller: <b>{html.escape(reseller_display_name_v14(stats))}</b>",
        f"Tier: <b>{stats['tier']}</b>",
        "",
        f"📦 Orders: <b>{stats['orders_total']}</b>",
        f"✅ Completed: <b>{stats['orders_completed']}</b>",
        f"🔥 Active: <b>{stats['orders_active']}</b>",
        f"👕 Total pieces ordered: <b>{stats['pieces_total']}</b>",
        f"🏁 Completed pieces: <b>{stats['pieces_completed']}</b>",
    ]

    if stats["last_order_id"]:
        lines.append(f"🧾 Last order: <code>#O{stats['last_order_id']}</code>")

    if next_min is not None:
        lines.extend([
            "",
            f"🎯 Next tier: <b>{next_label}</b>",
            f"Need <b>{remaining}</b> more completed piece{'s' if remaining != 1 else ''}.",
        ])
    else:
        lines.extend([
            "",
            "👑 <b>Maximum reseller tier reached.</b>",
        ])

    buttons = [
        [InlineKeyboardButton("📋 MY ORDERS", callback_data="myorders")],
        [InlineKeyboardButton("📦 LIVE STOCK", callback_data="stock")],
    ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )



async def myorders_command_v12(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "📋 Open W1NNURS Supply Bot privately to view your orders.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📋 MY ORDERS",
                    url=f"https://t.me/{BOT_USERNAME}?start=orders"
                )]
            ]),
        )
        return

    class CommandQuery:
        def __init__(self, message, user):
            self.message = message
            self.from_user = user

        async def edit_message_text(self, *args, **kwargs):
            return await self.message.edit_text(*args, **kwargs)

        async def answer(self, *args, **kwargs):
            return None

    msg = await update.message.reply_text("Loading your orders...")
    await show_my_orders(CommandQuery(msg, update.effective_user))




ADMIN_ORDER_FILTERS = {
    "ACTIVE": ("PENDING_APPROVAL", "AWAITING_PAYMENT", "PAID", "PREPARING", "SHIPPED"),
    "PENDING": ("PENDING_APPROVAL",),
    "PAYMENT": ("AWAITING_PAYMENT",),
    "PAID": ("PAID",),
    "PREPARING": ("PREPARING",),
    "SHIPPED": ("SHIPPED",),
    "COMPLETED": ("COMPLETED",),
    "DECLINED": ("DECLINED",),
}


def admin_order_summary(order):
    status = ORDER_STATUS_LABELS.get(order.get("status"), order.get("status", "Unknown"))
    pieces = sum(int(x.get("qty", 0)) for x in order.get("items", []))
    reseller = (
        f"@{order.get('username')}"
        if order.get("username")
        else order.get("first_name") or str(order.get("user_id"))
    )
    return status, pieces, reseller


async def render_admin_orders(query, filter_name="ACTIVE", page=0):
    statuses = ADMIN_ORDER_FILTERS.get(filter_name, ADMIN_ORDER_FILTERS["ACTIVE"])
    filtered = [o for o in orders.values() if o.get("status") in statuses]
    filtered.sort(key=lambda o: int(o.get("order_id", 0)), reverse=True)

    per_page = 6
    page = max(page, 0)
    start = page * per_page
    chunk = filtered[start:start + per_page]

    lines = [
        "🏆 <b>W1NNURS ORDER DASHBOARD</b>",
        f"\nFilter: <b>{html.escape(filter_name.title())}</b>",
        f"Orders: <b>{len(filtered)}</b>\n",
    ]
    buttons = []

    if not chunk:
        lines.append("No orders in this section.")
    else:
        for order in chunk:
            status, pieces, reseller = admin_order_summary(order)
            lines.append(
                f"\n<code>#O{order['order_id']}</code> • "
                f"<b>{html.escape(reseller)}</b>\n"
                f"{len(order.get('items', []))} products • {pieces} pcs\n"
                f"{status}"
            )
            buttons.append([
                InlineKeyboardButton(
                    f"#O{order['order_id']} • Manage",
                    callback_data=f"admorder:{order['order_id']}:{filter_name}:{page}",
                )
            ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admorders:{filter_name}:{page-1}"))
    if start + per_page < len(filtered):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admorders:{filter_name}:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.extend([
        [
            InlineKeyboardButton("🕒 Pending", callback_data="admorders:PENDING:0"),
            InlineKeyboardButton("💳 Awaiting", callback_data="admorders:PAYMENT:0"),
        ],
        [
            InlineKeyboardButton("💰 Paid", callback_data="admorders:PAID:0"),
            InlineKeyboardButton("📦 Preparing", callback_data="admorders:PREPARING:0"),
        ],
        [
            InlineKeyboardButton("🚚 Shipped", callback_data="admorders:SHIPPED:0"),
            InlineKeyboardButton("🏁 Completed", callback_data="admorders:COMPLETED:0"),
        ],
        [
            InlineKeyboardButton("🔥 Active", callback_data="admorders:ACTIVE:0"),
            InlineKeyboardButton("❌ Declined", callback_data="admorders:DECLINED:0"),
        ],
    ])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )





# v15 — W1NNURS Reseller League
# Beginner missions intentionally give XP/progress only — no material reward.




# v20 — W1NNURS RESELLER OS
# Achievements + Hall of Fame + weekly challenges + streaks + watchlist
# + reseller dashboard + unlocks + upcoming stock foundation.

def v20_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS v20_activity (
        user_id INTEGER NOT NULL,
        activity_date TEXT NOT NULL,
        action TEXT NOT NULL,
        value TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(user_id, activity_date, action, value)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS v20_watchlist (
        user_id INTEGER NOT NULL,
        product_key TEXT NOT NULL,
        brand TEXT NOT NULL DEFAULT '',
        product TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, product_key)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS v20_hall_of_fame (
        season_key TEXT PRIMARY KEY,
        season_name TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        score INTEGER NOT NULL DEFAULT 0,
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS v20_badges (
        user_id INTEGER NOT NULL,
        badge_key TEXT NOT NULL,
        earned_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, badge_key)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS v20_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    conn.commit()
    return conn


def v20_track(user_id, action, value=""):
    from datetime import date
    try:
        conn = v20_db()
        conn.execute(
            "INSERT OR IGNORE INTO v20_activity(user_id,activity_date,action,value) VALUES(?,?,?,?)",
            (int(user_id), date.today().isoformat(), str(action), str(value or "")),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("v20 activity error:", e)


def v20_activity_days(user_id):
    conn = v20_db()
    rows = conn.execute(
        "SELECT DISTINCT activity_date FROM v20_activity WHERE user_id=? ORDER BY activity_date",
        (int(user_id),)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def v20_week_key(day=None):
    from datetime import date
    d = day or date.today()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def v20_week_actions(user_id):
    from datetime import datetime
    conn = v20_db()
    rows = conn.execute(
        "SELECT activity_date,action,value FROM v20_activity WHERE user_id=?",
        (int(user_id),)
    ).fetchall()
    conn.close()
    result = []
    current = v20_week_key()
    for d, action, value in rows:
        try:
            if v20_week_key(datetime.fromisoformat(d).date()) == current:
                result.append((d, action, value))
        except Exception:
            pass
    return result


V20_WEEKLY = [
    ("W_BRANDS", "🏷 Explore 5 brands", 5, "BRAND", 40),
    ("W_PRODUCTS", "👕 Check 15 products", 15, "PRODUCT", 60),
    ("W_RESEARCH", "📈 Use market research 5 times", 5, "RESEARCH", 50),
    ("W_CART", "🛒 Add 3 different products to cart", 3, "CART", 50),
]


def v20_weekly_state(user_id):
    actions = v20_week_actions(user_id)
    state = {}
    xp = 0
    for key, title, target, action, reward in V20_WEEKLY:
        if action == "RESEARCH":
            vals = {(a, v, d) for d, a, v in actions if a in ("STOCKX","GOOGLE","IMAGES")}
            count = len(vals)
        else:
            vals = {v or d for d, a, v in actions if a == action}
            count = len(vals)
        done = count >= target
        state[key] = (done, min(count, target), target, title, reward)
        if done:
            xp += reward
    return state, xp


def v20_streak(user_id):
    # Weekly activity streak: at least one meaningful tracked action in consecutive ISO weeks.
    from datetime import datetime, date, timedelta
    days = v20_activity_days(user_id)
    weeks = set()
    for d in days:
        try:
            dd = datetime.fromisoformat(d).date()
            iso = dd.isocalendar()
            weeks.add((iso.year, iso.week))
        except Exception:
            pass
    if not weeks:
        return 0
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    streak = 0
    cursor = current_monday
    while True:
        iso = cursor.isocalendar()
        if (iso.year, iso.week) in weeks:
            streak += 1
            cursor -= timedelta(days=7)
        else:
            break
    return streak


def v20_watch_add(user_id, product_key, brand, product):
    conn = v20_db()
    conn.execute(
        "INSERT OR REPLACE INTO v20_watchlist(user_id,product_key,brand,product) VALUES(?,?,?,?)",
        (int(user_id), str(product_key), str(brand), str(product))
    )
    conn.commit()
    conn.close()


def v20_watch_remove(user_id, product_key):
    conn = v20_db()
    conn.execute("DELETE FROM v20_watchlist WHERE user_id=? AND product_key=?",
                 (int(user_id), str(product_key)))
    conn.commit()
    conn.close()


def v20_watch_items(user_id):
    conn = v20_db()
    rows = conn.execute(
        "SELECT product_key,brand,product FROM v20_watchlist WHERE user_id=? ORDER BY created_at DESC",
        (int(user_id),)
    ).fetchall()
    conn.close()
    return rows


def v20_badge_catalog(user_id):
    permanent = reseller_stats_v14(user_id)
    season = v17_season_stats(user_id)
    onboarding, _ = v161_beginner_state(user_id)
    advanced, _ = v16_advanced_state(user_id)
    streak = v20_streak(user_id)
    completed_pieces = permanent.get("completed_pieces", 0)
    completed_orders = permanent.get("completed_orders", 0)

    definitions = [
        ("ONBOARDING", "🎓 Vault Graduate", all(onboarding.values())),
        ("FIRST_WIN", "🏁 First Win", completed_orders >= 1),
        ("PCS_5", "📦 5 PCS Club", completed_pieces >= 5),
        ("PCS_25", "📦 25 PCS Club", completed_pieces >= 25),
        ("PCS_50", "💪 50 PCS Club", completed_pieces >= 50),
        ("ADVANCED", "⚔️ Advanced Complete", bool(advanced) and all(advanced.values())),
        ("STREAK_2", "🔥 2 Week Streak", streak >= 2),
        ("STREAK_4", "🔥 4 Week Streak", streak >= 4),
        ("SEASON_TOP", "🏆 Season Player", season.get("score", 0) > 0),
    ]
    return definitions


def v20_sync_badges(user_id):
    conn = v20_db()
    for key, label, earned in v20_badge_catalog(user_id):
        if earned:
            conn.execute(
                "INSERT OR IGNORE INTO v20_badges(user_id,badge_key) VALUES(?,?)",
                (int(user_id), key)
            )
    conn.commit()
    rows = conn.execute(
        "SELECT badge_key FROM v20_badges WHERE user_id=? ORDER BY earned_at",
        (int(user_id),)
    ).fetchall()
    conn.close()
    earned_keys = {r[0] for r in rows}
    labels = {k: label for k, label, _ in v20_badge_catalog(user_id)}
    return [labels.get(k, k) for k in earned_keys]


def v20_unlocks(user_id):
    stats = v161_league_stats(user_id)
    xp = stats.get("xp", 0)
    onboarding, _ = v161_beginner_state(user_id)
    return [
        ("📦 Live Stock", True),
        ("🎯 Advanced Missions", all(onboarding.values())),
        ("🔥 Early Drop Preview", xp >= 350),
        ("👀 Upcoming Stock", xp >= 700),
        ("👑 Elite Area", xp >= 1200),
    ]


def v20_finalize_previous_season():
    # Safe archival: only records a previous season if a leaderboard snapshot
    # was explicitly stored in v20_meta by this version. No historical winner is invented.
    conn = v20_db()
    conn.close()


async def dashboard_command_v20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats = v161_league_stats(uid)
    season = v17_season_stats(uid)
    ranking = v17_all_season_stats()
    rank = next((i for i, s in enumerate(ranking, 1) if s["user_id"] == uid), None)
    badges = v20_sync_badges(uid)
    streak = v20_streak(uid)
    weekly, weekly_xp = v20_weekly_state(uid)
    watch_count = len(v20_watch_items(uid))
    beginner = stats.get("missions", {})
    advanced = stats.get("advanced_missions", {})

    lines = [
        "🏆 <b>W1NNURS RESELLER DASHBOARD</b>",
        "",
        f"🏅 League: <b>{html.escape(str(stats.get('league','Rookie League')))}</b>",
        f"⚡ Permanent XP: <b>{stats.get('xp',0)}</b>",
        f"🔥 Activity streak: <b>{streak} week{'s' if streak != 1 else ''}</b>",
        f"🏁 Season rank: <b>#{rank}</b>" if rank else "🏁 Season rank: <b>Unranked</b>",
        f"⚡ Season score: <b>{season.get('score',0)}</b>",
        "",
        "📊 <b>BUSINESS</b>",
        f"📦 Orders: <b>{stats.get('total_orders',0)}</b>",
        f"✅ Completed: <b>{stats.get('completed_orders',0)}</b>",
        f"👕 Completed pieces: <b>{stats.get('completed_pieces',0)}</b>",
        "",
        "🎯 <b>PROGRESS</b>",
        f"🎓 Beginner: <b>{sum(bool(x) for x in beginner.values())}/{len(beginner)}</b>",
        f"⚔️ Advanced: <b>{sum(bool(x) for x in advanced.values())}/{len(advanced)}</b>",
        f"📅 Weekly: <b>{sum(1 for x in weekly.values() if x[0])}/{len(weekly)}</b> (+{weekly_xp} XP)",
        f"❤️ Watchlist: <b>{watch_count}</b>",
        "",
        "🎖 <b>BADGES</b>",
        " ".join(badges[-6:]) if badges else "<i>No badges yet — explore the club.</i>",
    ]
    buttons = [
        [InlineKeyboardButton("📅 WEEKLY CHALLENGES", callback_data="v20weekly")],
        [InlineKeyboardButton("🎖 BADGES & UNLOCKS", callback_data="v20badges")],
        [InlineKeyboardButton("❤️ WATCHLIST", callback_data="v20watchlist")],
        [InlineKeyboardButton("🏆 SEASON", callback_data="v17seasonboard")],
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def weekly_command_v20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state, xp = v20_weekly_state(uid)
    lines = ["📅 <b>WEEKLY CHALLENGES</b>", f"<b>{v20_week_key()}</b>", ""]
    for _, (done, count, target, title, reward) in state.items():
        lines.append(f"{'✅' if done else '⬜'} {title} — <b>{count}/{target}</b> • +{reward} XP")
    lines += ["", f"⚡ Weekly XP unlocked: <b>{xp}</b>",
              "<i>Weekly challenges reset automatically each ISO week.</i>"]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def badges_command_v20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    badges = v20_sync_badges(uid)
    unlocks = v20_unlocks(uid)
    lines = ["🎖 <b>BADGES & UNLOCKS</b>", ""]
    lines.append("<b>Earned badges</b>")
    lines.extend([f"✅ {b}" for b in badges] or ["<i>No badges yet.</i>"])
    lines += ["", "<b>Club access</b>"]
    for label, unlocked in unlocks:
        lines.append(f"{'🔓' if unlocked else '🔒'} {label}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def watchlist_command_v20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = v20_watch_items(update.effective_user.id)
    lines = ["❤️ <b>MY WATCHLIST</b>", ""]
    if not items:
        lines.append("No watched products yet.")
    else:
        for _, brand, product in items[:30]:
            lines.append(f"• <b>{html.escape(brand)}</b> — {html.escape(product)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def halloffame_command_v20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = v20_db()
    rows = conn.execute(
        "SELECT season_name,user_id,score FROM v20_hall_of_fame ORDER BY season_key DESC LIMIT 24"
    ).fetchall()
    conn.close()
    lines = ["👑 <b>W1NNURS HALL OF FAME</b>", ""]
    if not rows:
        lines.append("<i>The first Season Champion will appear here after a season is archived.</i>")
    else:
        for season_name, uid, score in rows:
            lines.append(f"👑 <b>{html.escape(season_name)}</b> — {html.escape(v17_display_name(uid))} • {score} pts")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def upcoming_command_v20(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    unlocked = dict(v20_unlocks(uid)).get("👀 Upcoming Stock", False)
    if not unlocked:
        await update.message.reply_text(
            "🔒 <b>UPCOMING STOCK</b>\n\nReach <b>Dynasty League / 700 XP</b> to unlock this section.",
            parse_mode="HTML"
        )
        return
    await update.message.reply_text(
        "👀 <b>UPCOMING STOCK</b>\n\nNo upcoming drops have been published yet.\n"
        "<i>This area is ready for future admin-controlled previews.</i>",
        parse_mode="HTML"
    )


# v17 — W1NNURS SEASONS
# Seasonal competition is separate from permanent XP/tier progression.

def v17_season_key():
    from datetime import datetime
    now = datetime.now()
    return now.strftime("%Y-%m")


def v17_season_name():
    from datetime import datetime
    now = datetime.now()
    return now.strftime("%B %Y").upper()


def v17_season_orders(user_id):
    season = v17_season_key()
    result = []
    for order in orders.values():
        if int(order.get("user_id", 0)) != int(user_id):
            continue
        if order.get("status") != "COMPLETED":
            continue

        # Newer orders may have timestamps; older orders without a timestamp
        # still remain in permanent stats but do not inflate seasonal ranking.
        stamp = str(
            order.get("completed_at")
            or order.get("updated_at")
            or order.get("created_at")
            or ""
        )
        if stamp.startswith(season):
            result.append(order)
    return result


def v17_season_stats(user_id):
    completed = v17_season_orders(user_id)
    pieces = sum(
        int(item.get("qty", 0))
        for order in completed
        for item in order.get("items", [])
    )
    brands = {
        str(item.get("brand", "")).strip().lower()
        for order in completed
        for item in order.get("items", [])
        if str(item.get("brand", "")).strip()
    }
    # Score rewards both activity and volume, without replacing permanent XP.
    score = pieces * 10 + len(completed) * 25 + len(brands) * 15
    return {
        "user_id": int(user_id),
        "orders": len(completed),
        "pieces": pieces,
        "brands": len(brands),
        "score": score,
    }


def v17_all_season_stats():
    user_ids = {
        int(o.get("user_id", 0))
        for o in orders.values()
        if o.get("user_id")
    }
    stats = [v17_season_stats(uid) for uid in user_ids]
    stats = [s for s in stats if s["score"] > 0]
    stats.sort(key=lambda s: (s["score"], s["pieces"], s["orders"]), reverse=True)
    return stats


def v17_display_name(user_id):
    stats = reseller_stats_v14(user_id)
    return reseller_display_name_v14(stats)



# v16.1 — Onboarding Missions
# Beginner stage is exploration-only: NO order/submission/completion requirement.

V161_BEGINNER_MISSIONS = [
    ("OPEN_STOCK", "📦 Open The Vault", "Open Live Stock", 15),
    ("EXPLORE_BRANDS", "🏷 Brand Explorer", "Open 3 different brands", 25),
    ("EXPLORE_PRODUCTS", "👕 Product Explorer", "Open 5 different products", 30),
    ("STOCKX", "📈 Market Check", "Use StockX research", 20),
    ("GOOGLE", "🔎 Research Mode", "Use Google research", 20),
    ("IMAGES", "🖼 Visual Check", "Use Google Images research", 20),
    ("SIZE", "📏 Size Check", "Open a size selection", 20),
    ("ASK_PRICE", "💬 Price Curious", "Open Ask Price for a product", 25),
    ("CART", "🛒 Cart Training", "Add an item to your cart", 30),
]


def v161_init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS onboarding_progress (
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, action, value)
        )
    """)
    conn.commit()
    conn.close()


def v161_track(user_id, action, value=""):
    try:
        v161_init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO onboarding_progress(user_id, action, value) VALUES(?,?,?)",
            (int(user_id), str(action), str(value or "")),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("Onboarding tracking error:", e)


def v161_values(user_id, action):
    try:
        v161_init_db()
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT value FROM onboarding_progress WHERE user_id=? AND action=?",
            (int(user_id), str(action)),
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def v161_beginner_state(user_id):
    stock = bool(v161_values(user_id, "OPEN_STOCK"))
    brands = v161_values(user_id, "BRAND")
    products = v161_values(user_id, "PRODUCT")
    done = {
        "OPEN_STOCK": stock,
        "EXPLORE_BRANDS": len(brands) >= 3,
        "EXPLORE_PRODUCTS": len(products) >= 5,
        "STOCKX": bool(v161_values(user_id, "STOCKX")),
        "GOOGLE": bool(v161_values(user_id, "GOOGLE")),
        "IMAGES": bool(v161_values(user_id, "IMAGES")),
        "SIZE": bool(v161_values(user_id, "SIZE")),
        "ASK_PRICE": bool(v161_values(user_id, "ASK_PRICE")),
        "CART": bool(v161_values(user_id, "CART")),
    }
    xp = sum(xp for key, _, _, xp in V161_BEGINNER_MISSIONS if done.get(key))
    return done, xp


def v161_progress(user_id, key):
    if key == "EXPLORE_BRANDS":
        return f"{min(len(v161_values(user_id,'BRAND')),3)}/3"
    if key == "EXPLORE_PRODUCTS":
        return f"{min(len(v161_values(user_id,'PRODUCT')),5)}/5"
    return "1/1" if v161_beginner_state(user_id)[0].get(key) else "0/1"


def v161_league_stats(user_id):
    stats = reseller_stats_v14(user_id)
    missions, beginner_xp = v161_beginner_state(user_id)
    advanced_done, advanced_xp = v16_advanced_state(user_id)
    stats["missions"] = missions
    stats["advanced_missions"] = advanced_done
    stats["beginner_xp"] = beginner_xp
    stats["advanced_xp"] = advanced_xp
    stats["xp"] = beginner_xp + advanced_xp
    stats["league"] = v15_level(stats["xp"])
    return stats


V15_BEGINNER_MISSIONS = [
    ("FIRST_ORDER", "🛒 First Move", "Submit your first order", 25),
    ("FIRST_COMPLETE", "✅ First Win", "Complete your first order", 50),
    ("FIVE_PIECES", "👕 Starter Pack", "Complete 5 pieces", 75),
    ("TWO_BRANDS", "🏷 Brand Explorer", "Complete orders containing 2 different brands", 60),
    ("THREE_ORDERS", "🔥 Getting Serious", "Complete 3 orders", 100),
]


# v16 — Advanced Missions
V16_ADVANCED_MISSIONS = [
    ("TEN_PIECES","📦 Stock Builder","Complete 10 pieces",125),
    ("FIVE_ORDERS","🔥 Consistency","Complete 5 orders",150),
    ("FIVE_BRANDS","🌍 Brand Hunter","Complete products from 5 different brands",175),
    ("TWENTY_PIECES","💪 Volume Player","Complete 20 pieces",225),
    ("TEN_ORDERS","🏆 Serious Reseller","Complete 10 orders",300),
    ("FIFTY_PIECES","💎 Heavy Hitter","Complete 50 pieces",500),
]
def v16_activity(user_id):
    completed=v15_completed_orders(user_id)
    pieces=sum(int(i.get("qty",0)) for o in completed for i in o.get("items",[]))
    brands={str(i.get("brand","")).strip().lower() for o in completed for i in o.get("items",[]) if str(i.get("brand","")).strip()}
    return completed,pieces,brands
def v16_advanced_state(user_id):
    completed,pieces,brands=v16_activity(user_id)
    done={"TEN_PIECES":pieces>=10,"FIVE_ORDERS":len(completed)>=5,"FIVE_BRANDS":len(brands)>=5,
          "TWENTY_PIECES":pieces>=20,"TEN_ORDERS":len(completed)>=10,"FIFTY_PIECES":pieces>=50}
    return done,sum(xp for key,_,_,xp in V16_ADVANCED_MISSIONS if done.get(key))
def v16_total_league_stats(user_id):
    stats=v15_league_stats(user_id)
    done,axp=v16_advanced_state(user_id)
    stats["advanced_missions"]=done
    stats["xp"]=stats["xp"]+axp
    stats["league"]=v15_level(stats["xp"])
    return stats
def v16_progress_text(user_id,key):
    completed,pieces,brands=v16_activity(user_id)
    values={"TEN_PIECES":(pieces,10),"FIVE_ORDERS":(len(completed),5),"FIVE_BRANDS":(len(brands),5),
            "TWENTY_PIECES":(pieces,20),"TEN_ORDERS":(len(completed),10),"FIFTY_PIECES":(pieces,50)}
    cur,target=values[key]
    return f"{min(cur,target)}/{target}"


V15_LEVELS = [
    (1200, "👑 Elite League"),
    (700, "💎 Dynasty League"),
    (350, "🏆 Champion League"),
    (150, "🥇 Winner League"),
    (0, "🥉 Rookie League"),
]


def v15_completed_orders(user_id):
    return [
        o for o in orders.values()
        if int(o.get("user_id", 0)) == int(user_id)
        and o.get("status") == "COMPLETED"
    ]


def v15_non_declined_orders(user_id):
    return [
        o for o in orders.values()
        if int(o.get("user_id", 0)) == int(user_id)
        and o.get("status") != "DECLINED"
    ]


def v15_mission_state(user_id):
    completed = v15_completed_orders(user_id)
    submitted = v15_non_declined_orders(user_id)

    completed_pieces = sum(
        int(item.get("qty", 0))
        for o in completed
        for item in o.get("items", [])
    )
    brands = {
        str(item.get("brand", "")).strip().lower()
        for o in completed
        for item in o.get("items", [])
        if str(item.get("brand", "")).strip()
    }

    done = {
        "FIRST_ORDER": len(submitted) >= 1,
        "FIRST_COMPLETE": len(completed) >= 1,
        "FIVE_PIECES": completed_pieces >= 5,
        "TWO_BRANDS": len(brands) >= 2,
        "THREE_ORDERS": len(completed) >= 3,
    }

    xp = sum(xp for key, _, _, xp in V15_BEGINNER_MISSIONS if done.get(key))
    return done, xp


def v15_level(xp):
    for minimum, label in V15_LEVELS:
        if xp >= minimum:
            return label
    return "🥉 Rookie League"


def v15_next_level(xp):
    ascending = [
        (0, "🥉 Rookie League"),
        (150, "🥇 Winner League"),
        (350, "🏆 Champion League"),
        (700, "💎 Dynasty League"),
        (1200, "👑 Elite League"),
    ]
    for minimum, label in ascending:
        if xp < minimum:
            return minimum, label, minimum - xp
    return None, None, 0


def v15_league_stats(user_id):
    base = reseller_stats_v14(user_id)
    missions, xp = v15_mission_state(user_id)
    base["missions"] = missions
    base["xp"] = xp
    base["league"] = v15_level(xp)
    return base


def v15_all_league_stats():
    user_ids = {
        int(o.get("user_id", 0))
        for o in orders.values()
        if o.get("user_id")
    }
    stats = [v15_league_stats(uid) for uid in user_ids]
    stats.sort(
        key=lambda s: (
            int(s["xp"]),
            int(s["pieces_completed"]),
            int(s["orders_completed"]),
        ),
        reverse=True,
    )
    return stats



RESELLER_TIERS_V14 = [
    (60, "👑 W1NNURS Elite"),
    (30, "💎 Dynasty"),
    (15, "🏆 Champion"),
    (5, "🥇 Winner"),
    (0, "🥉 Member"),
]


def reseller_tier_v14(completed_pieces):
    completed_pieces = int(completed_pieces or 0)
    for minimum, label in RESELLER_TIERS_V14:
        if completed_pieces >= minimum:
            return label
    return "🥉 Member"


def reseller_stats_v14(user_id):
    user_orders = [
        o for o in orders.values()
        if int(o.get("user_id", 0)) == int(user_id)
    ]
    user_orders.sort(key=lambda o: int(o.get("order_id", 0)))

    non_declined = [o for o in user_orders if o.get("status") != "DECLINED"]
    completed = [o for o in user_orders if o.get("status") == "COMPLETED"]
    active = [
        o for o in user_orders
        if o.get("status") not in ("COMPLETED", "DECLINED")
    ]

    total_pieces = sum(
        int(item.get("qty", 0))
        for o in non_declined
        for item in o.get("items", [])
    )
    completed_pieces = sum(
        int(item.get("qty", 0))
        for o in completed
        for item in o.get("items", [])
    )

    latest = user_orders[-1] if user_orders else None
    sample = latest or (user_orders[0] if user_orders else None)

    return {
        "user_id": int(user_id),
        "username": (sample or {}).get("username"),
        "first_name": (sample or {}).get("first_name"),
        "orders_total": len(non_declined),
        "orders_completed": len(completed),
        "orders_active": len(active),
        "pieces_total": total_pieces,
        "pieces_completed": completed_pieces,
        "last_order_id": latest.get("order_id") if latest else None,
        "tier": reseller_tier_v14(completed_pieces),
    }


def all_reseller_stats_v14():
    user_ids = sorted({
        int(o.get("user_id", 0))
        for o in orders.values()
        if o.get("user_id")
    })
    stats = [reseller_stats_v14(uid) for uid in user_ids]
    stats.sort(
        key=lambda s: (
            int(s["pieces_completed"]),
            int(s["orders_completed"]),
            int(s["pieces_total"]),
        ),
        reverse=True,
    )
    return stats


def reseller_display_name_v14(stats):
    if stats.get("username"):
        return f"@{stats['username']}"
    if stats.get("first_name"):
        return str(stats["first_name"])
    return str(stats["user_id"])


def next_tier_progress_v14(completed_pieces):
    completed_pieces = int(completed_pieces or 0)
    ascending = [
        (0, "🥉 Member"),
        (5, "🥇 Winner"),
        (15, "🏆 Champion"),
        (30, "💎 Dynasty"),
        (60, "👑 W1NNURS Elite"),
    ]
    for minimum, label in ascending:
        if completed_pieces < minimum:
            return minimum, label, minimum - completed_pieces
    return None, None, 0



ORDER_STATUS_FLOW_V13 = [
    "PENDING_APPROVAL",
    "AWAITING_PAYMENT",
    "PAID",
    "PREPARING",
    "SHIPPED",
    "COMPLETED",
]


def next_order_status_v13(status):
    try:
        idx = ORDER_STATUS_FLOW_V13.index(status)
    except ValueError:
        return None
    if idx >= len(ORDER_STATUS_FLOW_V13) - 1:
        return None
    return ORDER_STATUS_FLOW_V13[idx + 1]


def previous_order_status_v13(status):
    try:
        idx = ORDER_STATUS_FLOW_V13.index(status)
    except ValueError:
        return None
    if idx <= 0:
        return None
    return ORDER_STATUS_FLOW_V13[idx - 1]


def status_button_label_v13(status):
    labels = {
        "AWAITING_PAYMENT": "⏳ AWAITING PAYMENT",
        "PAID": "💰 MARK AS PAID",
        "PREPARING": "📦 START PREPARING",
        "SHIPPED": "🚚 MARK AS SHIPPED",
        "COMPLETED": "🏁 COMPLETE ORDER",
    }
    return labels.get(status, status.replace("_", " ").title())


async def notify_order_status_v13(context, order):
    status = order.get("status", "")
    label = ORDER_STATUS_LABELS.get(status, status.replace("_", " ").title())
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"🏆 <b>W1NNURS ORDER UPDATE</b>\n\n"
                f"Order <code>#O{order['order_id']}</code>\n"
                f"Status: <b>{label}</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass



async def render_admin_order_detail(query, order_id, filter_name="ACTIVE", page=0):
    order = orders.get(order_id)
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return

    status, pieces, reseller = admin_order_summary(order)
    lines = [
        f"🧾 <b>ORDER #O{order_id}</b>",
        f"\nReseller: <b>{html.escape(reseller)}</b>",
        f"Status: <b>{status}</b>",
        f"Total pieces: <b>{pieces}</b>\n",
    ]

    for i, item in enumerate(order.get("items", []), 1):
        lines.append(
            f"\n{i}. <b>{html.escape(str(item.get('brand', '')))}</b>\n"
            f"{html.escape(str(item.get('product', '')))}\n"
            f"Size: <b>{html.escape(str(item.get('size', '')))}</b> • "
            f"Qty: <b>{int(item.get('qty', 0))}</b>"
        )

    buttons = []
    current_status = order.get("status")

    if current_status == "PENDING_APPROVAL":
        buttons.append([
            InlineKeyboardButton("✅ APPROVE", callback_data=f"orderapprove:{order_id}"),
            InlineKeyboardButton("❌ DECLINE", callback_data=f"orderdecline:{order_id}"),
        ])
    elif current_status not in ("DECLINED", "COMPLETED"):
        next_status = next_order_status_v13(current_status)
        previous_status = previous_order_status_v13(current_status)

        if next_status:
            buttons.append([
                InlineKeyboardButton(
                    status_button_label_v13(next_status),
                    callback_data=f"v13next:{order_id}:{filter_name}:{page}"
                )
            ])

        if previous_status:
            buttons.append([
                InlineKeyboardButton(
                    "↩️ PREVIOUS STATUS",
                    callback_data=f"v13prev:{order_id}:{filter_name}:{page}"
                )
            ])
    elif current_status == "COMPLETED":
        buttons.append([
            InlineKeyboardButton(
                "↩️ PREVIOUS STATUS",
                callback_data=f"v13prev:{order_id}:{filter_name}:{page}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("💬 OPEN USER", url=f"tg://user?id={order['user_id']}")
    ])
    buttons.append([
        InlineKeyboardButton("⬅️ BACK TO ORDERS", callback_data=f"admorders:{filter_name}:{page}")
    ])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )



async def render_resellers_dashboard_v14(query, page=0):
    stats_list = all_reseller_stats_v14()
    per_page = 8
    page = max(int(page), 0)
    start = page * per_page
    chunk = stats_list[start:start + per_page]

    lines = [
        "🏆 <b>W1NNURS RESELLER RANKING</b>",
        "",
        f"Resellers: <b>{len(stats_list)}</b>",
        "Ranking is based on completed pieces.",
        "",
    ]
    buttons = []

    if not chunk:
        lines.append("No reseller data yet.")
    else:
        for idx, stats in enumerate(chunk, start=start + 1):
            name = reseller_display_name_v14(stats)
            lines.append(
                f"{idx}. <b>{html.escape(name)}</b>\n"
                f"   {stats['tier']} • "
                f"{stats['pieces_completed']} completed pcs • "
                f"{stats['orders_completed']} completed orders"
            )
            buttons.append([
                InlineKeyboardButton(
                    f"{idx}. {name} • View",
                    callback_data=f"v14reseller:{stats['user_id']}:{page}"
                )
            ])

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️", callback_data=f"v14resellers:{page-1}")
        )
    if start + per_page < len(stats_list):
        nav.append(
            InlineKeyboardButton("➡️", callback_data=f"v14resellers:{page+1}")
        )
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("🔄 REFRESH", callback_data=f"v14resellers:{page}")
    ])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def render_reseller_detail_v14(query, user_id, page=0):
    stats = reseller_stats_v14(user_id)
    name = reseller_display_name_v14(stats)
    next_min, next_label, remaining = next_tier_progress_v14(stats["pieces_completed"])

    lines = [
        "👤 <b>RESELLER PROFILE</b>",
        "",
        f"Reseller: <b>{html.escape(name)}</b>",
        f"User ID: <code>{stats['user_id']}</code>",
        f"Tier: <b>{stats['tier']}</b>",
        "",
        f"📦 Orders: <b>{stats['orders_total']}</b>",
        f"✅ Completed: <b>{stats['orders_completed']}</b>",
        f"🔥 Active: <b>{stats['orders_active']}</b>",
        f"👕 Total pieces: <b>{stats['pieces_total']}</b>",
        f"🏁 Completed pieces: <b>{stats['pieces_completed']}</b>",
    ]

    if stats["last_order_id"]:
        lines.append(f"🧾 Last order: <code>#O{stats['last_order_id']}</code>")

    if next_min is not None:
        lines.extend([
            "",
            f"🎯 Next tier: <b>{next_label}</b>",
            f"Remaining: <b>{remaining}</b> completed pcs",
        ])

    buttons = [
        [InlineKeyboardButton("💬 OPEN USER", url=f"tg://user?id={stats['user_id']}")],
        [InlineKeyboardButton("⬅️ BACK TO RANKING", callback_data=f"v14resellers:{page}")],
    ]

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def resellers_command_v14(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_or_owner_v12(update, context):
        await update.message.reply_text("Admin only.")
        return

    class CommandQuery:
        def __init__(self, message, user):
            self.message = message
            self.from_user = user

        async def edit_message_text(self, *args, **kwargs):
            return await self.message.edit_text(*args, **kwargs)

        async def answer(self, *args, **kwargs):
            return None

    msg = await update.message.reply_text("Loading reseller ranking...")
    await render_resellers_dashboard_v14(
        CommandQuery(msg, update.effective_user), 0
    )



async def orders_dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_or_owner_v12(update, context):
        await update.message.reply_text("Admin only.")
        return

    class CommandQuery:
        def __init__(self, message, user):
            self.message = message
            self.from_user = user

        async def edit_message_text(self, *args, **kwargs):
            return await self.message.edit_text(*args, **kwargs)

        async def answer(self, *args, **kwargs):
            return None

    msg = await update.message.reply_text("Loading order dashboard...")
    await render_admin_orders(CommandQuery(msg, update.effective_user), "ACTIVE", 0)



async def dbstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed = update.effective_user.id in ADMIN_IDS
    if not allowed and update.effective_chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(
                update.effective_chat.id, update.effective_user.id
            )
            allowed = member.status in ("administrator", "creator")
        except Exception:
            pass

    if not allowed:
        await update.message.reply_text("Admin only.")
        return

    path = Path(DB_PATH)
    await update.message.reply_text(
        "💾 PERSISTENCE STATUS\n\n"
        f"DB: {DB_PATH}\n"
        f"Exists: {'YES' if path.exists() else 'NO'}\n"
        f"Saved carts: {len(carts)}\n"
        f"Saved orders: {len(orders)}\n"
        f"Last order ID: {order_counter}"
    )



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
        v161_track(query.from_user.id, "BRAND", data)
        v20_track(query.from_user.id, "BRAND", data)
        _, sheet_id, page = data.split(":", 2)
        await query.answer()
        await show_brand_stock(query, int(sheet_id), int(page))
        return

    if data.startswith("product:"):
        v161_track(query.from_user.id, "PRODUCT", data)
        v20_track(query.from_user.id, "PRODUCT", data)
        _, sheet_id, row_num, brand_page = data.split(":", 3)
        await query.answer()
        await show_product(
            query,
            int(sheet_id),
            int(row_num),
            int(brand_page),
        )
        return

    if data == "v20weekly":
        await query.answer()
        uid = query.from_user.id
        state, xp = v20_weekly_state(uid)
        lines = ["📅 <b>WEEKLY CHALLENGES</b>", f"<b>{v20_week_key()}</b>", ""]
        for _, (done, count, target, title, reward) in state.items():
            lines.append(f"{'✅' if done else '⬜'} {title} — <b>{count}/{target}</b> • +{reward} XP")
        lines += ["", f"⚡ Weekly XP unlocked: <b>{xp}</b>"]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏆 SEASON", callback_data="v17seasonboard")]]))
        return

    if data == "v20badges":
        await query.answer()
        uid = query.from_user.id
        badges = v20_sync_badges(uid)
        unlocks = v20_unlocks(uid)
        lines = ["🎖 <b>BADGES & UNLOCKS</b>", ""]
        lines += [f"✅ {b}" for b in badges] or ["<i>No badges yet.</i>"]
        lines += ["", "<b>Club access</b>"]
        lines += [f"{'🔓' if ok else '🔒'} {label}" for label, ok in unlocks]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")
        return

    if data == "v20watchlist":
        await query.answer()
        items = v20_watch_items(query.from_user.id)
        lines = ["❤️ <b>MY WATCHLIST</b>", ""]
        lines += [f"• <b>{html.escape(b)}</b> — {html.escape(p)}" for _,b,p in items[:30]] or ["No watched products yet."]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")
        return

    if data.startswith("v20watchadd:"):
        await query.answer("Added to watchlist ❤️")
        payload = data.split(":", 1)[1]
        v20_watch_add(query.from_user.id, payload, "", payload)
        v20_track(query.from_user.id, "WATCH", payload)
        return

    if data.startswith("v20watchremove:"):
        payload = data.split(":", 1)[1]
        v20_watch_remove(query.from_user.id, payload)
        await query.answer("Removed from watchlist")
        return

    if data == "v17seasonboard":
        await query.answer()
        await render_season_board_v17(query)
        return

    if data.startswith("v161research:"):
        parts = data.split(":", 2)
        kind = parts[1]
        query_text = parts[2] if len(parts) > 2 else ""
        import urllib.parse
        encoded = urllib.parse.quote_plus(query_text)
        if kind == "stockx":
            v161_track(query.from_user.id, "STOCKX")
            v20_track(query.from_user.id, "STOCKX", query_text)
            url = f"https://stockx.com/search?s={encoded}"
            label = "📈 OPEN STOCKX"
        elif kind == "google":
            v161_track(query.from_user.id, "GOOGLE")
            v20_track(query.from_user.id, "GOOGLE", query_text)
            url = f"https://www.google.com/search?q={encoded}"
            label = "🔎 OPEN GOOGLE"
        else:
            v161_track(query.from_user.id, "IMAGES")
            v20_track(query.from_user.id, "IMAGES", query_text)
            url = f"https://www.google.com/search?tbm=isch&q={encoded}"
            label = "🖼 OPEN GOOGLE IMAGES"
        await query.answer("Mission progress saved.")
        await query.message.reply_text(
            "Research unlocked 👇",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)]])
        )
        return

    if data == "v16advanced":
        await query.answer()
        stats=v161_league_stats(query.from_user.id)
        done=stats["advanced_missions"]
        beginner_done=all(stats["missions"].values())
        lines=["🔥 <b>W1NNURS ADVANCED MISSIONS</b>","",f"League: <b>{stats['league']}</b>",f"Total XP: <b>{stats['xp']}</b>",""]
        if not beginner_done:
            remaining=sum(1 for v in stats["missions"].values() if not v)
            lines += ["🔒 <b>ADVANCED MISSIONS LOCKED</b>","","Finish your Beginner Missions first.",f"Remaining beginner missions: <b>{remaining}</b>"]
        else:
            lines += ["🔓 <b>ADVANCED MISSIONS UNLOCKED</b>","<i>Harder objectives. XP & status progression.</i>",""]
            for key,title,desc,xp in V16_ADVANCED_MISSIONS:
                icon="✅" if done.get(key) else "⬜"
                lines.append(f"{icon} <b>{title}</b> • +{xp} XP\n   {desc} • <b>{v16_progress_text(query.from_user.id,key)}</b>")
        await query.edit_message_text("\n".join(lines),parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎯 BEGINNER MISSIONS",callback_data="v15league")],
                                               [InlineKeyboardButton("🏆 MY PROFILE",callback_data="v15profile")]]))
        return

    if data == "v15profile":
        await query.answer()
        stats = reseller_stats_v14(query.from_user.id)
        next_min, next_label, remaining = next_tier_progress_v14(stats["pieces_completed"])
        lines = [
            "🏆 <b>W1NNURS RESELLER PROFILE</b>",
            "",
            f"Reseller: <b>{html.escape(reseller_display_name_v14(stats))}</b>",
            f"Tier: <b>{stats['tier']}</b>",
            f"📦 Orders: <b>{stats['orders_total']}</b>",
            f"✅ Completed: <b>{stats['orders_completed']}</b>",
            f"👕 Completed pieces: <b>{stats['pieces_completed']}</b>",
        ]
        if next_min is not None:
            lines.extend(["", f"🎯 Next tier: <b>{next_label}</b> • {remaining} pcs remaining"])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 RESELLER LEAGUE", callback_data="v15league")]
            ]),
        )
        return

    if data == "v15league":
        await query.answer()
        stats = v15_league_stats(query.from_user.id)
        done = stats["missions"]
        next_min, next_label, remaining = v15_next_level(stats["xp"])
        lines = [
            "🏆 <b>W1NNURS RESELLER LEAGUE</b>",
            "",
            f"League: <b>{stats['league']}</b>",
            f"XP: <b>{stats['xp']}</b>",
            "",
            "🎯 <b>BEGINNER MISSIONS</b>",
            "<i>XP & progress only — no reward.</i>",
            "",
        ]
        for key, title, description, xp in V161_BEGINNER_MISSIONS:
            icon = "✅" if done.get(key) else "⬜"
            lines.append(f"{icon} <b>{title}</b> • +{xp} XP\n   {description} • <b>{v161_progress(query.from_user.id, key)}</b>")
        if next_min is not None:
            lines.extend(["", f"🚀 Next league: <b>{next_label}</b> • {remaining} XP remaining"])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏆 MY PROFILE", callback_data="v15profile")]
            ]),
        )
        return

    if data.startswith("v14resellers:"):
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return
        _, page_raw = data.split(":", 1)
        await query.answer()
        await render_resellers_dashboard_v14(query, int(page_raw))
        return

    if data.startswith("v14reseller:"):
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return
        _, user_id_raw, page_raw = data.split(":", 2)
        await query.answer()
        await render_reseller_detail_v14(query, int(user_id_raw), int(page_raw))
        return

    if data.startswith("v13next:"):
        _, order_id_raw, filter_name, page_raw = data.split(":", 3)
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return

        order_id = int(order_id_raw)
        order = orders.get(order_id)
        if not order:
            await query.answer("Order not found.", show_alert=True)
            return

        current = order.get("status")
        next_status = next_order_status_v13(current)
        if not next_status:
            await query.answer("No next status available.", show_alert=True)
            return

        order["status"] = next_status
        if next_status == "COMPLETED":
            from datetime import datetime
            order["completed_at"] = datetime.now().isoformat(timespec="seconds")
        order["updated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        save_order(order)
        await notify_order_status_v13(context, order)
        await query.answer("Status updated.")
        await render_admin_order_detail(query, order_id, filter_name, int(page_raw))
        return

    if data.startswith("v13prev:"):
        _, order_id_raw, filter_name, page_raw = data.split(":", 3)
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return

        order_id = int(order_id_raw)
        order = orders.get(order_id)
        if not order:
            await query.answer("Order not found.", show_alert=True)
            return

        current = order.get("status")
        previous_status = previous_order_status_v13(current)
        if not previous_status:
            await query.answer("No previous status available.", show_alert=True)
            return

        order["status"] = previous_status
        if current == "COMPLETED":
            order.pop("completed_at", None)
        order["updated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        save_order(order)
        await notify_order_status_v13(context, order)
        await query.answer("Status moved back.")
        await render_admin_order_detail(query, order_id, filter_name, int(page_raw))
        return

    if data.startswith("admorders:"):
        _, filter_name, page = data.split(":", 2)
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        await render_admin_orders(query, filter_name, int(page))
        return

    if data.startswith("admorder:"):
        _, order_id, filter_name, page = data.split(":", 3)
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        await render_admin_order_detail(query, int(order_id), filter_name, int(page))
        return

    if data.startswith("cartadd:"):
        v161_track(query.from_user.id, "CART")
        v20_track(query.from_user.id, "CART", data)
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
            save_cart(query.from_user.id)
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
                save_cart(query.from_user.id)
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
            save_cart(query.from_user.id)
        await query.answer("Quantity updated.")
        await show_cart(query)
        return

    if data == "cartclear":
        delete_cart(query.from_user.id)
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
    load_persistent_state()
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
    app.add_handler(CommandHandler("myorders", myorders_command_v12))
    app.add_handler(CommandHandler("profile", profile_command_v14))
    app.add_handler(CommandHandler("league", league_command_v15))
    app.add_handler(CommandHandler("season", season_command_v17))
    app.add_handler(CommandHandler("dashboard", dashboard_command_v20))
    app.add_handler(CommandHandler("weekly", weekly_command_v20))
    app.add_handler(CommandHandler("badges", badges_command_v20))
    app.add_handler(CommandHandler("watchlist", watchlist_command_v20))
    app.add_handler(CommandHandler("halloffame", halloffame_command_v20))
    app.add_handler(CommandHandler("upcoming", upcoming_command_v20))
    app.add_handler(CommandHandler("onboarding", league_command_v15))
    app.add_handler(CommandHandler("advanced", advanced_command_v16))
    app.add_handler(CommandHandler("leagueranking", league_ranking_command_v15))
    app.add_handler(CommandHandler("resellers", resellers_command_v14))
    app.add_handler(CommandHandler("orders", orders_dashboard_command))
    app.add_handler(CommandHandler("dbstatus", dbstatus_command))
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
