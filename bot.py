import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@yourusername")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/yourchannel")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main_menu():
    keyboard = [
        [InlineKeyboardButton("🔥 Latest Drops", callback_data="latest_drops")],
        [InlineKeyboardButton("📦 Available Stock", callback_data="available_stock")],
        [InlineKeyboardButton("🤝 W1NNURS Partners", callback_data="partners")],
        [InlineKeyboardButton("🛒 Order / Reserve", callback_data="order")],
        [InlineKeyboardButton("💬 Support", callback_data="support")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏆 *W1NNURS SUPPLY*\n\n"
        "Welcome to W1NNURS SUPPLY.\n"
        "Your private access to reseller stock.\n\n"
        "Choose an option below:"
    )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏆 *W1NNURS SUPPLY*\n\nChoose an option:",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "latest_drops":
        text = (
            "🔥 *LATEST DROPS*\n\n"
            "New drops will be published in the W1NNURS SUPPLY channel.\n\n"
            f"👉 {CHANNEL_URL}"
        )
        buttons = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]

    elif query.data == "available_stock":
        text = (
            "📦 *AVAILABLE STOCK*\n\n"
            "Stock management is being prepared.\n"
            "For now, check the latest stock posted in the channel."
        )
        buttons = [
            [InlineKeyboardButton("📲 Open Channel", url=CHANNEL_URL)],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")],
        ]

    elif query.data == "partners":
        text = (
            "🤝 *W1NNURS PARTNERS*\n\n"
            "Private access for approved resellers.\n\n"
            "Partners can receive product information, listing materials, "
            "stock updates and reseller opportunities.\n\n"
            f"To apply, contact {SUPPORT_USERNAME}."
        )
        buttons = [
            [InlineKeyboardButton("💬 Apply / Contact", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")],
        ]

    elif query.data == "order":
        text = (
            "🛒 *ORDER / RESERVE*\n\n"
            "Send us the product name or stock ID, size and quantity you want.\n\n"
            f"Contact: {SUPPORT_USERNAME}"
        )
        buttons = [
            [InlineKeyboardButton("💬 Place Order", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")],
        ]

    elif query.data == "support":
        text = (
            "💬 *W1NNURS SUPPORT*\n\n"
            f"For orders, stock questions or partnership requests:\n{SUPPORT_USERNAME}"
        )
        buttons = [
            [InlineKeyboardButton("💬 Contact W1NNURS", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")],
        ]

    elif query.data == "back":
        await query.edit_message_text(
            "🏆 *W1NNURS SUPPLY*\n\nChoose an option:",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
        return

    else:
        text = "Unknown option."
        buttons = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Add it as an environment variable."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(handle_button))

    logger.info("W1NNURS SUPPLY bot is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
