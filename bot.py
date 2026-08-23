import logging
import os
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

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
    context.user_data["flow"] = "search_query"
    await update.effective_message.reply_text(
        "🔎 What would you like me to find?\n\n"
        "For example: `Samsung 55 inch TV under £600`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def demo_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("flow")

    if flow == "deal_url":
        await receive_deal_url(update, context)
        return
    if flow == "deal_title":
        context.user_data["deal"]["title"] = update.effective_message.text.strip()
        context.user_data["flow"] = "deal_price"
        await update.effective_message.reply_text(
            "💷 What is the current price?\n\nExample: `449.00`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if flow == "deal_price":
        await receive_deal_price(update, context)
        return
    if flow == "deal_old_price":
        await receive_deal_old_price(update, context)
        return

    if flow != "search_query":
        await update.effective_message.reply_text(
            "Tap *Find a product* or use /find to start a search.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(),
        )
        return

    query = update.effective_message.text.strip()
    context.user_data["search"] = {"query": query}
    context.user_data["flow"] = "search_budget"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("£50", callback_data="budget:50"),
                InlineKeyboardButton("£100", callback_data="budget:100"),
                InlineKeyboardButton("£250", callback_data="budget:250"),
            ],
            [
                InlineKeyboardButton("£500", callback_data="budget:500"),
                InlineKeyboardButton("£1,000", callback_data="budget:1000"),
            ],
            [InlineKeyboardButton("No maximum", callback_data="budget:any")],
        ]
    )
    await update.effective_message.reply_text(
        f"🐶 Searching for: *{escape_markdown(query)}*\n\n"
        "What is your maximum price?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def send_demo_result(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    search = context.user_data.get("search", {})
    query = search.get("query", "product")
    budget = search.get("budget", "No maximum")
    condition = search.get("condition", "Any")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("View example deal", url="https://www.ebay.co.uk/")],
            [InlineKeyboardButton("⏰ Alert me", callback_data="alert_demo")],
            [InlineKeyboardButton("🔎 Search again", callback_data="find")],
        ]
    )
    await message.reply_text(
        "🐶 *DealHound searched for:*\n"
        f"`{escape_markdown(query)}`\n\n"
        f"💷 Maximum: *{escape_markdown(str(budget))}*\n"
        f"📦 Condition: *{escape_markdown(condition)}*\n\n"
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
    context.user_data.pop("flow", None)


def escape_markdown(value: str) -> str:
    return value.replace("`", "'")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "find":
        context.user_data["flow"] = "search_query"
        await query.message.reply_text(
            "🔎 What would you like me to find?\n\n"
            "For example: `Air fryer under £100`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif query.data.startswith("budget:"):
        value = query.data.split(":", 1)[1]
        context.user_data.setdefault("search", {})["budget"] = (
            "No maximum" if value == "any" else f"£{int(value):,}"
        )
        context.user_data["flow"] = "search_condition"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("New", callback_data="condition:new"),
                    InlineKeyboardButton("Refurbished", callback_data="condition:refurbished"),
                ],
                [
                    InlineKeyboardButton("Used", callback_data="condition:used"),
                    InlineKeyboardButton("Any", callback_data="condition:any"),
                ],
            ]
        )
        await query.message.reply_text("Which condition?", reply_markup=keyboard)
    elif query.data.startswith("condition:"):
        value = query.data.split(":", 1)[1]
        context.user_data.setdefault("search", {})["condition"] = value.title()
        await send_demo_result(query.message, context)
    elif query.data == "deal_approve":
        await approve_deal(query, context)
    elif query.data == "deal_reject":
        context.user_data.pop("deal", None)
        context.user_data.pop("flow", None)
        await query.edit_message_text("❌ Deal rejected. Nothing was published.")
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


async def deal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("This command is owner-only.")
        return
    context.user_data["deal"] = {}
    context.user_data["flow"] = "deal_url"
    await update.effective_message.reply_text(
        "🔗 Paste the full product URL from Amazon, eBay, Currys or another retailer.\n\n"
        "Use /cancel at any time to stop."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("deal", None)
    context.user_data.pop("search", None)
    context.user_data.pop("flow", None)
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_menu())


async def receive_deal_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.effective_message.text.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        await update.effective_message.reply_text(
            "That does not look like a complete URL. Please paste a link beginning with `https://`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    context.user_data["deal"] = {
        "url": url,
        "retailer": retailer_name(parsed.netloc),
    }
    context.user_data["flow"] = "deal_title"
    await update.effective_message.reply_text("🏷 Send the product title.")


def retailer_name(host: str) -> str:
    host = host.lower()
    if "amazon." in host:
        return "Amazon UK"
    if "ebay." in host:
        return "eBay UK"
    if "currys." in host:
        return "Currys"
    return host.removeprefix("www.")


def clean_price(value: str) -> str | None:
    cleaned = value.strip().replace("£", "").replace(",", "")
    try:
        price = float(cleaned)
    except ValueError:
        return None
    if price <= 0:
        return None
    return f"£{price:,.2f}"


async def receive_deal_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    price = clean_price(update.effective_message.text)
    if not price:
        await update.effective_message.reply_text("Please enter a valid price, for example `449.00`.", parse_mode=ParseMode.MARKDOWN)
        return
    context.user_data["deal"]["price"] = price
    context.user_data["flow"] = "deal_old_price"
    await update.effective_message.reply_text(
        "📉 What was the previous price?\n\n"
        "Send a price such as `599.00`, or send `skip` if it cannot be verified.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def receive_deal_old_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = update.effective_message.text.strip()
    if value.lower() != "skip":
        old_price = clean_price(value)
        if not old_price:
            await update.effective_message.reply_text("Enter a valid price or send `skip`.", parse_mode=ParseMode.MARKDOWN)
            return
        context.user_data["deal"]["old_price"] = old_price
    context.user_data.pop("flow", None)
    await show_deal_preview(update.effective_message, context)


def deal_card(deal: dict, preview: bool = False) -> str:
    heading = "👀 <b>PRIVATE DEAL PREVIEW</b>" if preview else "🔥 <b>DEAL FOUND</b>"
    old_line = ""
    if deal.get("old_price"):
        old_line = f"\n📉 Previous price: <s>{escape(deal['old_price'])}</s>"
    return (
        f"{heading}\n\n"
        f"<b>{escape(deal['title'])}</b>\n\n"
        f"💷 <b>{escape(deal['price'])}</b>{old_line}\n"
        f"🏪 {escape(deal['retailer'])}\n"
        "🚚 Check delivery and final price with the retailer\n\n"
        "<i>Affiliate link: we may earn a commission at no extra cost to you. "
        "Prices can change; verify before purchasing.</i>"
    )


async def show_deal_preview(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    deal = context.user_data["deal"]
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Publish", callback_data="deal_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="deal_reject"),
        ], [InlineKeyboardButton("🔗 Open product", url=deal["url"])]]
    )
    await message.reply_text(
        deal_card(deal, preview=True),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def approve_deal(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(query.from_user.id):
        await query.answer("Owner only", show_alert=True)
        return
    deal = context.user_data.get("deal")
    if not deal:
        await query.edit_message_text("This preview has expired. Start again with /deal.")
        return
    if not DEALS_CHANNEL_ID:
        await query.message.reply_text("DEALS_CHANNEL_ID is not configured.")
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛒 View deal", url=deal["url"])]]
    )
    await context.bot.send_message(
        chat_id=DEALS_CHANNEL_ID,
        text=deal_card(deal),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    context.user_data.pop("deal", None)
    await query.edit_message_text("✅ Deal published to the channel.")


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
    app.add_handler(CommandHandler("deal", deal_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, demo_search))
    return app


def main() -> None:
    threading.Thread(target=start_health_server, daemon=True).start()
    logger.info("Starting DealHound UK")
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
