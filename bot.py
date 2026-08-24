import os, json, logging
from decimal import Decimal
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1uKVO5PFH_-vO5ZBr-GUJRUlfC0a1uw-iJaxxu2BsOfM")
WORKSHEET_GID = int(os.getenv("WORKSHEET_GID", "323906646"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@yourusername")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/yourchannel")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

SIZE_COLUMNS = ["XXS","XS","S","M","L","XL","XXL","XXXL"]
price_requests = {}
pending_admin_price = {}
request_counter = 0

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Latest Drops", callback_data="latest")],
        [InlineKeyboardButton("📦 Available Stock", callback_data="stock:0")],
        [InlineKeyboardButton("🤝 W1NNURS Partners", callback_data="partners")],
        [InlineKeyboardButton("🛒 Order / Reserve", callback_data="order")],
        [InlineKeyboardButton("💬 Support", callback_data="support")]
    ])

def worksheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID).get_worksheet_by_id(WORKSHEET_GID)

def qint(v):
    try: return int(float(str(v).replace(",", ".")))
    except: return 0

def stock():
    rows = worksheet().get_all_values()
    headers = [str(x).strip().upper() for x in rows[0]]
    idx = {h:i for i,h in enumerate(headers) if h}
    pidx = idx["PRODUCT NAME"]
    sidx = idx.get("SKU")
    out = []
    for row in rows[1:]:
        name = row[pidx].strip() if pidx < len(row) else ""
        if not name or name.upper() == "PRODUCT NAME": continue
        sku = row[sidx].strip() if sidx is not None and sidx < len(row) else ""
        sizes = {}
        for s in SIZE_COLUMNS:
            if s in idx:
                n = qint(row[idx[s]] if idx[s] < len(row) else "")
                if n > 0: sizes[s] = n
        if sizes: out.append({"name":name,"sku":sku,"sizes":sizes})
    return out

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏆 *W1NNURS SUPPLY*\n\nWelcome to W1NNURS SUPPLY.\nYour private access to reseller stock.",
        parse_mode="Markdown", reply_markup=menu())

async def idcmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram numeric ID is:\n`{update.effective_user.id}`", parse_mode="Markdown")

async def show_stock(q, page=0):
    items = stock()
    if not items:
        await q.edit_message_text("📦 No stock available.", reply_markup=menu()); return
    per=8; pages=(len(items)+per-1)//per; page=max(0,min(page,pages-1)); start=page*per
    buttons=[]
    for i,p in enumerate(items[start:start+per], start=start):
        buttons.append([InlineKeyboardButton("📦 "+p["name"][:45], callback_data=f"product:{i}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("◀️", callback_data=f"stock:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page<pages-1: nav.append(InlineKeyboardButton("▶️", callback_data=f"stock:{page+1}"))
    buttons += [nav,[InlineKeyboardButton("⬅️ Main Menu", callback_data="back")]]
    await q.edit_message_text("📦 *AVAILABLE STOCK*\n\nLive inventory. Select a product:",
                              parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_product(q, i):
    items=stock()
    if i>=len(items): await q.answer("Stock changed.", show_alert=True); return
    p=items[i]
    lines="\n".join(f"• {s}: {n} pcs" for s,n in p["sizes"].items())
    sku=f"\nSKU: `{p['sku']}`" if p["sku"] else ""
    buttons=[[InlineKeyboardButton(f"💰 Ask Price • {s}", callback_data=f"ask:{i}:{s}")] for s in p["sizes"]]
    buttons += [[InlineKeyboardButton("📦 Back to Stock", callback_data="stock:0")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")]]
    await q.edit_message_text(f"📦 *{p['name']}*{sku}\n\n*Available:*\n{lines}\n\n💰 Price available on request.",
                              parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def ask_price(q, i, size):
    global request_counter
    items=stock()
    if i>=len(items) or size not in items[i]["sizes"]:
        await q.answer("No longer available.", show_alert=True); return
    if not ADMIN_IDS:
        await q.answer("Admin not configured yet.", show_alert=True); return
    p=items[i]; request_counter+=1; rid=request_counter
    price_requests[rid]={"user_id":q.from_user.id,"username":q.from_user.username or "",
                         "product":p["name"],"size":size}
    who=f"@{q.from_user.username}" if q.from_user.username else str(q.from_user.id)
    for aid in ADMIN_IDS:
        await q.get_bot().send_message(
            aid, f"💰 *NEW PRICE REQUEST*\n\nReseller: {who}\nProduct: *{p['name']}*\nSize: *{size}*\nRequest: `#{rid}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💶 Reply with Price", callback_data=f"adminprice:{rid}")]]))
    await q.answer("Price request sent ✅", show_alert=True)

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; d=q.data
    if d=="noop": await q.answer(); return
    if d.startswith("stock:"): await q.answer(); await show_stock(q,int(d.split(":")[1])); return
    if d.startswith("product:"): await q.answer(); await show_product(q,int(d.split(":")[1])); return
    if d.startswith("ask:"):
        _,i,s=d.split(":",2); await ask_price(q,int(i),s); return
    if d.startswith("adminprice:"):
        rid=int(d.split(":")[1])
        if q.from_user.id not in ADMIN_IDS: await q.answer("Admin only.", show_alert=True); return
        pending_admin_price[q.from_user.id]=rid
        await q.answer(); await q.message.reply_text("Send the price, e.g. `72` or `72.50`", parse_mode="Markdown"); return
    await q.answer()
    if d=="back":
        await q.edit_message_text("🏆 *W1NNURS SUPPLY*",parse_mode="Markdown",reply_markup=menu())
    elif d=="latest":
        await q.edit_message_text("🔥 *LATEST DROPS*\n\nSee the channel for new drops.",parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📲 Open Channel",url=CHANNEL_URL)],[InlineKeyboardButton("⬅️ Main Menu",callback_data="back")]]))
    elif d=="partners":
        await q.edit_message_text("🤝 *W1NNURS PARTNERS*\n\nPrivate reseller access.",parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contact",url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],[InlineKeyboardButton("⬅️ Main Menu",callback_data="back")]]))
    elif d=="order":
        await q.edit_message_text("🛒 Open Available Stock and request your personal price.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 Available Stock",callback_data="stock:0")],[InlineKeyboardButton("⬅️ Main Menu",callback_data="back")]]))
    elif d=="support":
        await q.edit_message_text(f"💬 Support: {SUPPORT_USERNAME}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contact",url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],[InlineKeyboardButton("⬅️ Main Menu",callback_data="back")]]))

async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    aid=update.effective_user.id
    if aid not in pending_admin_price: return
    rid=pending_admin_price[aid]; req=price_requests.get(rid)
    raw=update.message.text.strip().replace("€","").replace(",",".")
    try:
        price=Decimal(raw)
        if price<=0: raise ValueError
    except:
        await update.message.reply_text("Send a valid price, e.g. 72 or 72.50"); return
    pending_admin_price.pop(aid,None)
    disp=f"{price:.2f}".rstrip("0").rstrip(".")
    await context.bot.send_message(
        req["user_id"],
        f"🏆 *W1NNURS SUPPLY OFFER*\n\n*{req['product']}*\nSize: *{req['size']}*\nYour price: *€{disp} / pc*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Accept",url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"),
                                            InlineKeyboardButton("💬 Negotiate",url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")]]))
    await update.message.reply_text(f"✅ Offer €{disp}/pc sent.")

def main():
    if not BOT_TOKEN or not GOOGLE_SERVICE_ACCOUNT_JSON: raise RuntimeError("Missing Railway variables")
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("menu",start))
    app.add_handler(CommandHandler("id",idcmd))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,admin_text))
    app.run_polling()

if __name__=="__main__":
    main()