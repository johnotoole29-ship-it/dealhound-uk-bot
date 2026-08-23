import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("DealHoundUK")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
DEALS_CHANNEL_ID = os.getenv("DEALS_CHANNEL_ID", "").strip()
PORT = int(os.getenv("PORT", "8080"))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"DealHound UK is healthy")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server() -> None:
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("Health server listening on port %s", PORT)
    server.serve_forever()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔎 Find a product", callback_data="find")],
            [
                InlineKeyboardButton("🔥 Today's deals", callback_data="deals"),
                InlineKeyboardButton("🛍 Categories", callback_data="categories"),
            ],
            [
                InlineKeyboardButton("⏰ Price alerts", callback_data="alerts"),
                InlineKeyboardButton("ℹ️ Help", callback_data="help"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_search", None)
    text = (
        "🐶 *Welcome to DealHound UK!*\n\n"
        "I sniff out great UK prices from leading retailers.\n\n"
        "🔎 Search for a product\n"
        "🔥 Discover fresh deals\n"
        "⏰ Watch for price drops\n\n"
        "Choose an option below:"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu()
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"Your Telegram ID is: `{update.effective_user.id}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_search"] = True
    await update.effective_message.reply_text(
        "🔎 What would you like me to find?\n\n"
        "For example: `Samsung 55 inch TV under £600`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def demo_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.pop("awaiting_search", False):
        await update.effective_message.reply_text(
            "Tap *Find a product* or use /find to start a search.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(),
        )
        return

    query = update.effective_message.text.strip()
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("View example deal", url="https://www.ebay.co.uk/")],
            [InlineKeyboardButton("⏰ Alert me", callback_data="alert_demo")],
            [InlineKeyboardButton("🔎 Search again", callback_data="find")],
        ]
    )
    await update.effective_message.reply_text(
        "🐶 *DealHound searched for:*\n"
        f"`{escape_markdown(query)}`\n\n"
        "🏷 *Example matching result*\n"
        "💷 £449.00\n"
        "🏪 eBay UK\n"
        "📦 Condition: New\n\n"
        "_Demo result — live retailer prices will appear when the eBay "
        "developer account is connected._\n\n"
        "_Affiliate links may earn us a commission at no extra cost to you._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def escape_markdown(value: str) -> str:
    return value.replace("`", "'")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "find":
        context.user_data["awaiting_search"] = True
        await query.message.reply_text(
            "🔎 What would you like me to find?\n\n"
            "For example: `Air fryer under £100`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif query.data == "deals":
        await query.message.reply_text(
            "🔥 *Today's deals*\n\n"
            "The live deal feed is being prepared. Your approved channel deals will appear here too.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif query.data == "categories":
        await query.message.reply_text(
            "🛍 *Categories*\n\n📺 TVs\n💻 Computing\n🎮 Gaming\n🍳 Kitchen\n📱 Phones",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif query.data == "alerts":
        await query.message.reply_text(
            "⏰ *Price alerts*\n\nPrice alerts will activate after live retailer prices are connected.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif query.data == "alert_demo":
        await query.message.reply_text(
            "✅ The alert screen works. Saving live alerts is coming in the retailer phase."
        )
    else:
        await help_command(update, context)


async def deals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🔥 Live deals will appear here once the first retailer is connected."
    )


async def disclosure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "DealHound UK may use affiliate links. If you buy through one of these links, "
        "we may earn a commission at no additional cost to you. Prices can change, so "
        "always verify the final price with the retailer before purchasing."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    await message.reply_text(
        "ℹ️ *DealHound Help*\n\n"
        "/start — Main menu\n"
        "/find — Search for a product\n"
        "/deals — Latest deals\n"
        "/id — Show your Telegram ID\n"
        "/disclosure — Affiliate information",
        parse_mode=ParseMode.MARKDOWN,
    )


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_TELEGRAM_ID) and str(user_id) == ADMIN_TELEGRAM_ID


async def test_deal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("This command is owner-only.")
        return
    if not DEALS_CHANNEL_ID:
        await update.effective_message.reply_text(
            "DEALS_CHANNEL_ID has not been added to the hosting environment yet."
        )
        return

    text = (
        "🔥 *DEALHOUND TEST DEAL*\n\n"
        "📺 *Samsung 55-inch 4K Smart TV*\n\n"
        "💷 *£449.00*\n"
        "🏪 Example retailer\n"
        "🚚 Check delivery with the retailer\n\n"
        "_This is a test post — not a live offer._\n\n"
        "_Affiliate links may earn us a commission at no extra cost to you._"
    )
    await context.bot.send_message(
        chat_id=DEALS_CHANNEL_ID, text=text, parse_mode=ParseMode.MARKDOWN
    )
    await update.effective_message.reply_text("✅ Test deal posted to the channel.")


def build_application() -> Application:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is missing")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("deals", deals_command))
    app.add_handler(CommandHandler("categories", start))
    app.add_handler(CommandHandler("alerts", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("disclosure", disclosure))
    app.add_handler(CommandHandler("testdeal", test_deal))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, demo_search))
    return app


def main() -> None:
    threading.Thread(target=start_health_server, daemon=True).start()
    logger.info("Starting DealHound UK")
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
