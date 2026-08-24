import asyncio
import base64
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import ip_address
from math import isfinite
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
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
RELEASE_LABEL = "search-back-button-1"
DEALS_CHANNEL_URL = "https://t.me/Dealhounduk"
BOT_PRIVATE_URL = "https://t.me/DealHoundUKBot"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
DEALS_CHANNEL_ID = os.getenv("DEALS_CHANNEL_ID", "").strip()
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID", "").strip()
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "").strip()
EBAY_CAMPAIGN_ID = os.getenv("EBAY_CAMPAIGN_ID", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = os.getenv("DATA_DIR", "/data").strip() or "/data"
DATABASE_PATH = os.path.join(DATA_DIR, "dealhound.db")

MAX_SEARCH_LENGTH = 200
MAX_TITLE_LENGTH = 180
MAX_FEEDBACK_LENGTH = 1000
MAX_CUSTOM_BUDGET = 1_000_000
MAX_FAVORITES_PER_USER = 25
EBAY_TIMEOUT_SECONDS = 12
EBAY_RESULT_LIMIT = 3
EBAY_MAX_RESULTS = 9
EBAY_MORE_COOLDOWN_SECONDS = 2.0

_ebay_token = ""
_ebay_token_expires_at = 0.0
_ebay_token_lock = threading.Lock()

RETAILER_STATUSES = [
    ("Currys", "🟠 Pending approval"),
    ("AO.com", "🟠 Pending approval"),
    ("Very", "🔴 Reapply later — requires 200 Awin sales/month"),
    ("The Range", "🟠 Pending approval"),
    ("Marks & Spencer", "🟠 Pending approval"),
    ("eBay UK", "🟢 Live search"),
    ("Amazon UK", "⚪ Coming later"),
]

CATEGORY_SEARCHES = {
    "tvs": ("📺 TVs", "4K Smart TV"),
    "laptops": ("💻 Laptops", "laptop"),
    "phones": ("📱 Phones", "smartphone"),
    "gaming": ("🎮 Gaming", "gaming console"),
    "lego": ("🧱 LEGO & Toys", "LEGO set"),
    "kitchen": ("🍳 Kitchen", "kitchen appliance"),
    "home": ("🏠 Home & Garden", "home and garden"),
    "beauty": ("💄 Health & Beauty", "health and beauty"),
}


def database_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def init_database() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with database_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                price TEXT NOT NULL,
                shipping TEXT NOT NULL,
                total TEXT NOT NULL,
                condition TEXT NOT NULL,
                url TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                saved_at TEXT NOT NULL,
                UNIQUE (telegram_user_id, item_id)
            )
            """
        )


def save_favorite(user_id: int, item: dict) -> tuple[str, int | None]:
    item_id = str(item.get("item_id", ""))[:200]
    title = str(item.get("title", ""))[:180]
    url = str(item.get("url", ""))[:2000]
    if not item_id or not title or not is_safe_ebay_url(urlparse(url)):
        raise ValueError("Invalid favorite item")
    values = (
        title,
        str(item.get("price", ""))[:30],
        str(item.get("shipping", ""))[:30],
        str(item.get("total", ""))[:30],
        str(item.get("condition", ""))[:80],
        url,
        str(item.get("image_url", ""))[:2000],
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    with database_connection() as connection:
        existing = connection.execute(
            "SELECT id FROM favorites WHERE telegram_user_id = ? AND item_id = ?",
            (user_id, item_id),
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE favorites
                SET title = ?, price = ?, shipping = ?, total = ?, condition = ?,
                    url = ?, image_url = ?, saved_at = ?
                WHERE id = ? AND telegram_user_id = ?
                """,
                values + (existing["id"], user_id),
            )
            return "existing", int(existing["id"])
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM favorites WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        if int(count) >= MAX_FAVORITES_PER_USER:
            return "limit", None
        cursor = connection.execute(
            """
            INSERT INTO favorites (
                telegram_user_id, item_id, title, price, shipping, total,
                condition, url, image_url, saved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, item_id) + values,
        )
        return "saved", int(cursor.lastrowid)


def load_favorites(user_id: int) -> list[dict]:
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, title, price, shipping, total, condition, url, image_url, saved_at
            FROM favorites
            WHERE telegram_user_id = ?
            ORDER BY saved_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, MAX_FAVORITES_PER_USER),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_favorite(user_id: int, favorite_id: int) -> bool:
    with database_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM favorites WHERE id = ? AND telegram_user_id = ?",
            (favorite_id, user_id),
        )
        return cursor.rowcount == 1


def clear_favorites(user_id: int) -> int:
    with database_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM favorites WHERE telegram_user_id = ?", (user_id,)
        )
        return cursor.rowcount


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
                InlineKeyboardButton("🔥 Today's deals", url=DEALS_CHANNEL_URL),
                InlineKeyboardButton("🛍 Categories", callback_data="categories"),
            ],
            [InlineKeyboardButton("❤️ Saved favourites", callback_data="favorites")],
            [
                InlineKeyboardButton("⏰ Price alerts", callback_data="alerts"),
                InlineKeyboardButton("ℹ️ Help", callback_data="help"),
            ],
            [
                InlineKeyboardButton("🏪 Retailers", callback_data="retailers"),
                InlineKeyboardButton("💬 Feedback", callback_data="feedback"),
            ],
        ]
    )


def deals_channel_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔥 Open DealHound UK deals", url=DEALS_CHANNEL_URL)]]
    )


def categories_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📺 TVs", callback_data="category:tvs"),
                InlineKeyboardButton("💻 Laptops", callback_data="category:laptops"),
            ],
            [
                InlineKeyboardButton("📱 Phones", callback_data="category:phones"),
                InlineKeyboardButton("🎮 Gaming", callback_data="category:gaming"),
            ],
            [
                InlineKeyboardButton("🧱 LEGO & Toys", callback_data="category:lego"),
                InlineKeyboardButton("🍳 Kitchen", callback_data="category:kitchen"),
            ],
            [
                InlineKeyboardButton("🏠 Home & Garden", callback_data="category:home"),
                InlineKeyboardButton("💄 Health & Beauty", callback_data="category:beauty"),
            ],
            [InlineKeyboardButton("🔎 Search for something else", callback_data="find")],
        ]
    )


async def send_categories(message) -> None:
    await message.reply_text(
        "🛍 <b>Browse categories</b>\n\n"
        "Choose a category, then set your maximum price and preferred condition.",
        parse_mode=ParseMode.HTML,
        reply_markup=categories_menu(),
    )


async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_categories(update.effective_message)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_workflow(context)
    text = (
        "🐶 *Welcome to DealHound UK!*\n\n"
        "I sniff out great UK prices from leading retailers.\n\n"
        "💬 Simply type a product name to begin\n"
        "🔎 Or tap Find a product for guidance\n"
        "🔥 Discover fresh deals\n"
        "⏰ Watch for price drops\n\n"
        "Try: `LEGO Millennium Falcon`\n\n"
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


def budget_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
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
            [InlineKeyboardButton("✏️ Enter your own amount", callback_data="budget:custom")],
            [InlineKeyboardButton("No maximum", callback_data="budget:any")],
        ]
    )


def condition_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
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


def parse_custom_budget(value: str) -> tuple[float, str] | None:
    cleaned = value.strip().replace("£", "").replace(",", "")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if not isfinite(amount) or amount <= 0 or amount > MAX_CUSTOM_BUDGET:
        return None
    label = f"£{amount:,.2f}".removesuffix(".00")
    return amount, label


async def begin_product_search(message, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    query = query.strip()
    if not query or len(query) > MAX_SEARCH_LENGTH:
        context.user_data["flow"] = "search_query"
        await message.reply_text(
            f"Please keep searches between 1 and {MAX_SEARCH_LENGTH} characters."
        )
        return
    generation = int(context.user_data.get("search_generation", 0)) + 1
    context.user_data["search_generation"] = generation
    context.user_data["search"] = {"query": query, "generation": generation}
    context.user_data["flow"] = "search_budget"
    await message.reply_text(
        f"🐶 Searching for: <b>{escape(query)}</b>\n\n"
        "What is your maximum price?",
        parse_mode=ParseMode.HTML,
        reply_markup=budget_menu(),
    )


async def edit_search_filters(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    search = context.user_data.get("search", {})
    query = search.get("query", "").strip()
    if not query:
        context.user_data["flow"] = "search_query"
        await message.reply_text(
            "That search has expired. What would you like me to find?"
        )
        return
    generation = int(context.user_data.get("search_generation", 0)) + 1
    context.user_data["search_generation"] = generation
    search["generation"] = generation
    search.pop("next_offset", None)
    search.pop("loading_more", None)
    context.user_data["flow"] = "search_budget"
    await message.reply_text(
        f"⚙️ <b>Change filters</b>\n\n"
        f"🔎 {escape(query)}\n\n"
        "Choose a new maximum price:",
        parse_mode=ParseMode.HTML,
        reply_markup=budget_menu(),
    )


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_workflow(context)
    if context.args:
        await begin_product_search(
            update.effective_message, context, " ".join(context.args)
        )
        return
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
        title = update.effective_message.text.strip()
        if not title or len(title) > MAX_TITLE_LENGTH:
            await update.effective_message.reply_text(
                f"Please enter a product title between 1 and {MAX_TITLE_LENGTH} characters."
            )
            return
        context.user_data["deal"]["title"] = title
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
    if flow == "feedback":
        await receive_feedback(update, context)
        return
    if flow == "search_custom_budget":
        parsed_budget = parse_custom_budget(update.effective_message.text)
        if not parsed_budget:
            await update.effective_message.reply_text(
                "Enter a valid maximum price, for example `750` or `£1,250`. ",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        amount, label = parsed_budget
        context.user_data.setdefault("search", {})["budget_value"] = amount
        context.user_data.setdefault("search", {})["budget"] = label
        context.user_data["flow"] = "search_condition"
        await update.effective_message.reply_text(
            "Which condition?", reply_markup=condition_menu()
        )
        return

    if flow is None:
        await begin_product_search(
            update.effective_message, context, update.effective_message.text
        )
        return

    if flow != "search_query":
        await update.effective_message.reply_text(
            "Tap *Find a product* or use /find to start a search.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(),
        )
        return

    await begin_product_search(
        update.effective_message, context, update.effective_message.text
    )


def ebay_access_token() -> str:
    global _ebay_token, _ebay_token_expires_at
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        raise RuntimeError("eBay credentials are not configured")

    with _ebay_token_lock:
        now = time.monotonic()
        if _ebay_token and now < _ebay_token_expires_at:
            return _ebay_token

        credentials = base64.b64encode(
            f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode("utf-8")
        ).decode("ascii")
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            }
        ).encode("ascii")
        request = Request(
            "https://api.ebay.com/identity/v1/oauth2/token",
            data=body,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urlopen(request, timeout=EBAY_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
        token = payload.get("access_token", "")
        if not token:
            raise RuntimeError("eBay did not return an access token")
        expires_in = max(int(payload.get("expires_in", 7200)), 120)
        _ebay_token = token
        _ebay_token_expires_at = now + expires_in - 60
        return token


def search_ebay(
    query: str, budget: int | None, condition: str, offset: int = 0
) -> tuple[list[dict], bool]:
    if not EBAY_CAMPAIGN_ID.isdigit() or len(EBAY_CAMPAIGN_ID) != 10:
        raise RuntimeError("eBay campaign ID is not configured correctly")

    filters = ["buyingOptions:{FIXED_PRICE}"]
    if budget is not None:
        filters.extend([f"price:[..{budget}]", "priceCurrency:GBP"])
    condition_filter = {
        "New": "NEW",
        "Used": "USED",
        "Refurbished": (
            "CERTIFIED_REFURBISHED|EXCELLENT_REFURBISHED|"
            "VERY_GOOD_REFURBISHED|GOOD_REFURBISHED|SELLER_REFURBISHED"
        ),
    }.get(condition)
    if condition_filter:
        filters.append(f"conditions:{{{condition_filter}}}")

    params = {
        "q": query,
        "limit": str(EBAY_RESULT_LIMIT),
        "offset": str(offset),
    }
    if filters:
        params["filter"] = ",".join(filters)
    request = Request(
        "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urlencode(params),
        headers={
            "Authorization": f"Bearer {ebay_access_token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            "X-EBAY-C-ENDUSERCTX": f"affiliateCampaignId={EBAY_CAMPAIGN_ID}",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=EBAY_TIMEOUT_SECONDS) as response:
        payload = json.load(response)

    results = []
    for item in payload.get("itemSummaries", []):
        item_id = str(item.get("itemId", ""))[:200]
        url = item.get("itemAffiliateWebUrl", "")
        parsed = urlparse(url)
        price = item.get("price", {})
        if (
            not item_id
            or not url
            or not is_safe_ebay_url(parsed)
            or price.get("currency") != "GBP"
        ):
            continue
        item_price = float(price["value"])
        shipping = "Check listing"
        shipping_value = None
        shipping_options = item.get("shippingOptions") or []
        if shipping_options:
            shipping_cost = shipping_options[0].get("shippingCost", {})
            if shipping_cost.get("currency") == "GBP":
                shipping_value = float(shipping_cost.get("value", 0))
                shipping = "Free" if shipping_value == 0 else f"£{shipping_value:,.2f}"
        total = (
            f"£{item_price + shipping_value:,.2f}"
            if shipping_value is not None
            else "Check listing"
        )
        image_url = item.get("image", {}).get("imageUrl", "")
        if not is_safe_ebay_image_url(urlparse(image_url)):
            image_url = ""
        results.append(
            {
                "item_id": item_id,
                "title": str(item.get("title", "eBay listing"))[:180],
                "price": f"£{item_price:,.2f}",
                "condition": str(item.get("condition", "Not specified"))[:80],
                "shipping": shipping,
                "total": total,
                "image_url": image_url,
                "url": url,
            }
        )
    return results, bool(payload.get("next"))


def is_safe_ebay_url(parsed) -> bool:
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return host == "ebay.co.uk" or host.endswith(".ebay.co.uk")


def is_safe_ebay_image_url(parsed) -> bool:
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return host == "ebayimg.com" or host.endswith(".ebayimg.com")


def result_card_caption(number: int, item: dict) -> str:
    return (
        f"🏷 <b>{number}. {escape(item['title'])}</b>\n\n"
        f"💷 Item price: <b>{escape(item['price'])}</b>\n"
        f"🚚 Delivery: {escape(item['shipping'])}\n"
        f"💰 Total delivered: <b>{escape(item['total'])}</b>\n"
        f"📦 Condition: {escape(item['condition'])}\n\n"
        "🛒 Buy It Now listing\n\n"
        "<i>Check the listing and final price before buying.</i>\n"
        "#Ad"
    )


def deal_share_text(item: dict) -> str:
    return (
        f"🐶 DealHound UK find\n\n"
        f"{item['title'][:120]}\n"
        f"Item price: {item['price']}\n"
        f"Delivery: {item['shipping']}\n"
        f"Total delivered: {item['total']}\n\n"
        "Buy It Now listing\n\n"
        "Check the listing and final price before buying. #Ad"
    )


def telegram_share_url(item: dict) -> str:
    return "https://t.me/share/url?" + urlencode(
        {"url": item["url"], "text": deal_share_text(item)}
    )


def whatsapp_share_url(item: dict) -> str:
    return "https://wa.me/?" + urlencode(
        {"text": f"{deal_share_text(item)}\n\n{item['url']}"}
    )


def favorite_card_caption(number: int, item: dict) -> str:
    return (
        f"❤️ <b>Saved favourite {number}</b>\n\n"
        f"🏷 <b>{escape(item['title'])}</b>\n"
        f"💷 Item price when saved: <b>{escape(item['price'])}</b>\n"
        f"🚚 Delivery when saved: {escape(item['shipping'])}\n"
        f"💰 Total when saved: <b>{escape(item['total'])}</b>\n"
        f"📦 Condition: {escape(item['condition'])}\n\n"
        "<i>Prices and availability can change. Check eBay before buying.</i>\n"
        "#Ad"
    )


async def send_favorites(message, user_id: int) -> None:
    if message.chat.type != "private":
        await message.reply_text(
            "🔐 Saved favourites are private. Open DealHound in a private chat to view them.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open DealHound privately", url=BOT_PRIVATE_URL)]]
            ),
        )
        return
    try:
        favorites = await asyncio.to_thread(load_favorites, user_id)
    except (sqlite3.Error, OSError):
        logger.exception("Could not load favorites")
        await message.reply_text("Saved favourites are temporarily unavailable.")
        return
    if not favorites:
        await message.reply_text(
            "❤️ <b>Your saved favourites</b>\n\n"
            "You haven't saved anything yet. Search for a product and tap "
            "<b>Save favourite</b> on a result card.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔎 Find a product", callback_data="find")]]
            ),
        )
        return
    await message.reply_text(
        f"❤️ <b>Your saved favourites ({len(favorites)})</b>\n\n"
        "Saved prices are a snapshot. Always check the current listing price.",
        parse_mode=ParseMode.HTML,
    )
    for number, item in enumerate(favorites, 1):
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🛒 Check current listing", url=item["url"])],
                [
                    InlineKeyboardButton(
                        "🗑 Remove",
                        callback_data=f"favorite_remove:{int(item['id'])}",
                    )
                ],
            ]
        )
        caption = favorite_card_caption(number, item)
        image_url = item.get("image_url", "")
        if image_url and is_safe_ebay_image_url(urlparse(image_url)):
            try:
                await message.reply_photo(
                    photo=image_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                continue
            except TelegramError:
                logger.warning("Telegram could not display a saved product image")
        await message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    await message.reply_text(
        "Manage your saved products:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🗑 Clear all favourites", callback_data="favorites_clear")],
                [InlineKeyboardButton("🔎 Find another product", callback_data="find")],
            ]
        ),
    )


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_favorites(update.effective_message, update.effective_user.id)


async def send_live_result(
    message, context: ContextTypes.DEFAULT_TYPE, offset: int = 0
) -> None:
    search = context.user_data.get("search", {})
    query = search.get("query", "product")
    budget = search.get("budget", "No maximum")
    condition = search.get("condition", "Any")
    if offset == 0:
        search.pop("next_offset", None)
        search["displayed_count"] = 0
        search["result_items"] = {}
        await message.reply_text("🐶 Searching live eBay UK listings…")
    else:
        await message.reply_text("🐶 Fetching three more eBay UK matches…")
    try:
        results, has_more = await asyncio.to_thread(
            search_ebay, query, search.get("budget_value"), condition, offset
        )
    except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, OSError):
        logger.exception("eBay search failed")
        await message.reply_text(
            "Sorry, eBay search is temporarily unavailable. Please try again shortly.",
            reply_markup=main_menu(),
        )
        context.user_data.pop("flow", None)
        return

    if not results and offset == 0:
        await message.reply_text(
            "I couldn't find a matching eBay UK listing with those filters. "
            "Try a broader search or choose Any condition.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⚙️ Change filters", callback_data="filters_edit")],
                    [InlineKeyboardButton("🔎 New search", callback_data="find")],
                    [InlineKeyboardButton("⬅️ Back to main menu", callback_data="menu_home")],
                ]
            ),
        )
        context.user_data.pop("flow", None)
        return
    if not results:
        search.pop("next_offset", None)
        await message.reply_text(
            "That was the end of the matching results.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⚙️ Change filters", callback_data="filters_edit")],
                    [InlineKeyboardButton("🔎 New search", callback_data="find")],
                    [InlineKeyboardButton("⬅️ Back to main menu", callback_data="menu_home")],
                ]
            ),
        )
        return

    if offset == 0:
        await message.reply_text(
            "🐶 <b>DealHound found these live eBay UK matches</b>\n\n"
            f"🔎 {escape(query)}\n"
            f"💷 Maximum: <b>{escape(str(budget))}</b>\n"
            f"📦 Condition: <b>{escape(condition)}</b>",
            parse_mode=ParseMode.HTML,
        )

    first_shown = int(search.get("displayed_count", 0)) + 1
    for number, item in enumerate(results, first_shown):
        search.setdefault("result_items", {})[str(number)] = item
        generation = int(search.get("generation", 0))
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🛒 View deal on eBay", url=item["url"])],
                [
                    InlineKeyboardButton(
                        "❤️ Save favourite",
                        callback_data=f"favorite_save:{generation}:{number}",
                    )
                ],
                [
                    InlineKeyboardButton("📨 Telegram", url=telegram_share_url(item)),
                    InlineKeyboardButton("💬 WhatsApp", url=whatsapp_share_url(item)),
                ],
            ]
        )
        caption = result_card_caption(number, item)
        if item["image_url"]:
            try:
                await message.reply_photo(
                    photo=item["image_url"],
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                continue
            except TelegramError:
                logger.warning("Telegram could not display an eBay product image")
        await message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    next_offset = offset + EBAY_RESULT_LIMIT
    can_show_more = has_more and next_offset < EBAY_MAX_RESULTS
    if can_show_more:
        search["next_offset"] = next_offset
    else:
        search.pop("next_offset", None)
    generation = int(search.get("generation", 0))
    controls = []
    if can_show_more:
        controls.append(
            [
                InlineKeyboardButton(
                    "➕ Show 3 more",
                    callback_data=f"results_more:{generation}:{next_offset}",
                )
            ]
        )
    controls.extend(
        [
            [InlineKeyboardButton("⚙️ Change filters", callback_data="filters_edit")],
            [InlineKeyboardButton("🔎 New search", callback_data="find")],
            [InlineKeyboardButton("⬅️ Back to main menu", callback_data="menu_home")],
        ]
    )
    last_shown = first_shown + len(results) - 1
    search["displayed_count"] = last_shown
    await message.reply_text(
        f"✅ <b>Showing results {first_shown}–{last_shown}</b>\n\n"
        "Results are supplied by eBay and may include close alternatives. "
        "Prices and availability can change.\n\n"
        "<i>Affiliate links may earn us a commission at no extra cost to you.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(controls),
    )
    context.user_data.pop("flow", None)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    manual_answer = query.data in ("deal_approve", "deal_reject") or query.data.startswith(
        ("results_more:", "favorite_save:", "favorite_remove:", "favorites_clear")
    )
    if not manual_answer:
        await query.answer()

    if query.data.startswith("favorite_save:"):
        parts = query.data.split(":")
        if len(parts) != 3:
            await query.answer("This save button is invalid.", show_alert=True)
            return
        try:
            generation = int(parts[1])
            result_number = int(parts[2])
        except ValueError:
            await query.answer("This save button is invalid.", show_alert=True)
            return
        search = context.user_data.get("search", {})
        item = search.get("result_items", {}).get(str(result_number))
        if generation != int(search.get("generation", -1)) or not item:
            await query.answer(
                "This result has expired. Run the search again to save it.", show_alert=True
            )
            return
        try:
            status, _ = await asyncio.to_thread(
                save_favorite, query.from_user.id, item
            )
        except (sqlite3.Error, OSError, ValueError):
            logger.exception("Could not save favorite")
            await query.answer("Could not save this favourite right now.", show_alert=True)
            return
        if status == "limit":
            await query.answer(
                f"You can save up to {MAX_FAVORITES_PER_USER} favourites.",
                show_alert=True,
            )
            return
        message = "Already saved — details refreshed." if status == "existing" else "Saved to favourites ❤️"
        await query.answer(message)
        return
    if query.data.startswith("favorite_remove:"):
        parts = query.data.split(":")
        try:
            favorite_id = int(parts[1]) if len(parts) == 2 else 0
        except ValueError:
            favorite_id = 0
        if favorite_id <= 0:
            await query.answer("This remove button is invalid.", show_alert=True)
            return
        try:
            removed = await asyncio.to_thread(
                delete_favorite, query.from_user.id, favorite_id
            )
        except (sqlite3.Error, OSError):
            logger.exception("Could not remove favorite")
            await query.answer("Could not remove this favourite right now.", show_alert=True)
            return
        if not removed:
            await query.answer("This favourite was already removed.")
            return
        await query.answer("Favourite removed.")
        try:
            if query.message.photo:
                await query.message.edit_caption("🗑 Favourite removed.")
            else:
                await query.message.edit_text("🗑 Favourite removed.")
        except TelegramError:
            logger.warning("Could not update a removed favorite card")
        return
    if query.data == "favorites_clear":
        await query.answer()
        await query.message.reply_text(
            "Delete all your saved favourites? This cannot be undone.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Yes, delete all", callback_data="favorites_clear_confirm"
                        ),
                        InlineKeyboardButton(
                            "❌ Cancel", callback_data="favorites_clear_cancel"
                        ),
                    ]
                ]
            ),
        )
        return
    if query.data == "favorites_clear_cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("Your saved favourites were not changed.")
        return
    if query.data == "favorites_clear_confirm":
        try:
            deleted = await asyncio.to_thread(clear_favorites, query.from_user.id)
        except (sqlite3.Error, OSError):
            logger.exception("Could not clear favorites")
            await query.answer("Could not clear favourites right now.", show_alert=True)
            return
        await query.answer("Favourites cleared.")
        await query.edit_message_text(f"🗑 Deleted {deleted} saved favourite(s).")
        return
    if query.data.startswith("results_more:"):
        parts = query.data.split(":")
        search = context.user_data.get("search", {})
        if len(parts) != 3:
            await query.answer("This button is no longer valid.", show_alert=True)
            return
        try:
            generation = int(parts[1])
            offset = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("This button is no longer valid.", show_alert=True)
            return
        expected_generation = int(search.get("generation", -1))
        expected_offset = search.get("next_offset")
        if (
            generation != expected_generation
            or offset != expected_offset
            or offset < EBAY_RESULT_LIMIT
            or offset >= EBAY_MAX_RESULTS
        ):
            await query.answer("These results have expired. Start a new search.", show_alert=True)
            return
        if search.get("loading_more"):
            await query.answer("More results are already loading.")
            return
        now = time.monotonic()
        if now - float(search.get("last_more_at", 0)) < EBAY_MORE_COOLDOWN_SECONDS:
            await query.answer("Please wait a moment before loading more results.")
            return
        search["loading_more"] = True
        search["last_more_at"] = now
        search.pop("next_offset", None)
        await query.answer("Finding more results…")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            logger.warning("Could not remove an old pagination button")
        try:
            await send_live_result(query.message, context, offset=offset)
        finally:
            search["loading_more"] = False
        return
    if query.data == "menu_home":
        clear_workflow(context)
        await query.message.reply_text(
            "🐶 <b>DealHound UK main menu</b>\n\n"
            "Type a product name or choose an option below:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
    elif query.data == "find":
        context.user_data["flow"] = "search_query"
        await query.message.reply_text(
            "🔎 What would you like me to find?\n\n"
            "For example: `Air fryer under £100`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif query.data == "filters_edit":
        await edit_search_filters(query.message, context)
    elif query.data == "favorites":
        await send_favorites(query.message, query.from_user.id)
    elif query.data.startswith("budget:"):
        value = query.data.split(":", 1)[1]
        if value == "custom":
            context.user_data["flow"] = "search_custom_budget"
            await query.message.reply_text(
                "✏️ Enter your maximum price.\n\nFor example: `750` or `£1,250`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        context.user_data.setdefault("search", {})["budget_value"] = (
            None if value == "any" else int(value)
        )
        context.user_data.setdefault("search", {})["budget"] = (
            "No maximum" if value == "any" else f"£{int(value):,}"
        )
        context.user_data["flow"] = "search_condition"
        await query.message.reply_text("Which condition?", reply_markup=condition_menu())
    elif query.data.startswith("condition:"):
        value = query.data.split(":", 1)[1]
        context.user_data.setdefault("search", {})["condition"] = value.title()
        await send_live_result(query.message, context)
    elif query.data == "deal_approve":
        await approve_deal(query, context)
    elif query.data == "deal_reject":
        if not is_admin(query.from_user.id):
            await query.answer("Owner only", show_alert=True)
            return
        await query.answer()
        context.user_data.pop("deal", None)
        context.user_data.pop("flow", None)
        await query.edit_message_text("❌ Deal rejected. Nothing was published.")
    elif query.data == "deals":
        await query.message.reply_text(
            "🔥 *Today's deals*\n\n"
            "Open the DealHound UK channel to see published deals and updates.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=deals_channel_button(),
        )
    elif query.data == "categories":
        await send_categories(query.message)
    elif query.data.startswith("category:"):
        slug = query.data.split(":", 1)[1]
        category = CATEGORY_SEARCHES.get(slug)
        if not category:
            await query.message.reply_text(
                "That category is unavailable. Please choose another.",
                reply_markup=categories_menu(),
            )
            return
        clear_workflow(context)
        await begin_product_search(query.message, context, category[1])
    elif query.data == "alerts":
        await query.message.reply_text(
            "⏰ *Price alerts*\n\nPrice alerts will activate after live retailer prices are connected.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif query.data == "alert_demo":
        await query.message.reply_text(
            "✅ The alert screen works. Saving live alerts is coming in the retailer phase."
        )
    elif query.data == "retailers":
        await send_retailers(query.message)
    elif query.data == "feedback":
        context.user_data["flow"] = "feedback"
        await query.message.reply_text(
            "💬 Send your suggestion or feedback in one message.\n\n"
            "It will be forwarded privately to the DealHound owner. Use /cancel to stop."
        )
    else:
        await help_command(update, context)


async def deals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🔥 *DealHound UK deals*\n\n"
        "Open the channel to see published deals and updates.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=deals_channel_button(),
    )


async def disclosure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "DealHound UK may use affiliate links. If you buy through one of these links, "
        "we may earn a commission at no additional cost to you. Prices can change, so "
        "always verify the final price with the retailer before purchasing."
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🐶 <b>About DealHound UK</b>\n\n"
        "DealHound is a UK shopping assistant being built to compare matching products "
        "across approved retailers, highlight total prices and provide price alerts.\n\n"
        "Retailer integrations are being activated one at a time after approval and testing.",
        parse_mode=ParseMode.HTML,
    )


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🔐 <b>DealHound Privacy</b>\n\n"
        "DealHound receives your Telegram user ID and the messages you send to the bot so it "
        "can respond and operate requested features. Searches are not sold. Feedback is "
        "forwarded privately to the bot owner and includes your Telegram display name and ID "
        "so a reply is possible.\n\n"
        "If you save a favourite, DealHound stores your numeric Telegram ID and a snapshot "
        "of that public product listing in encrypted persistent storage. It does not store "
        "your Telegram name with favourites. Saved items remain until you remove them or use "
        "Clear all favourites.\n\n"
        "Do not send passwords, payment-card information, API tokens or other sensitive data. "
        "Price-alert storage will be explained before that feature goes live.",
        parse_mode=ParseMode.HTML,
    )


def retailer_status_text() -> str:
    rows = "\n".join(f"{status} — {escape(name)}" for name, status in RETAILER_STATUSES)
    return (
        "🏪 <b>Retailer status</b>\n\n"
        f"{rows}\n\n"
        "Only retailers marked 🟢 Live will appear in real comparisons."
    )


async def send_retailers(message) -> None:
    await message.reply_text(retailer_status_text(), parse_mode=ParseMode.HTML)


async def retailers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_retailers(update.effective_message)


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["flow"] = "feedback"
    await update.effective_message.reply_text(
        "💬 Send your suggestion or feedback in one message.\n\n"
        "It will be forwarded privately to the DealHound owner. Use /cancel to stop."
    )


async def receive_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = update.effective_message.text.strip()
    if not value or len(value) > MAX_FEEDBACK_LENGTH:
        await update.effective_message.reply_text(
            f"Please keep feedback between 1 and {MAX_FEEDBACK_LENGTH} characters."
        )
        return
    if not ADMIN_TELEGRAM_ID:
        context.user_data.pop("flow", None)
        await update.effective_message.reply_text("Feedback is temporarily unavailable.")
        return
    user = update.effective_user
    sender = escape(user.full_name or "Unknown user")
    username = f"@{escape(user.username)}" if user.username else "No username"
    await context.bot.send_message(
        chat_id=ADMIN_TELEGRAM_ID,
        text=(
            "💬 <b>DealHound feedback</b>\n\n"
            f"From: {sender} ({username})\n"
            f"Telegram ID: <code>{user.id}</code>\n\n"
            f"{escape(value)}"
        ),
        parse_mode=ParseMode.HTML,
    )
    context.user_data.pop("flow", None)
    await update.effective_message.reply_text("✅ Thank you. Your feedback was sent privately.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    await message.reply_text(
        "ℹ️ *DealHound Help*\n\n"
        "/start — Main menu\n"
        "Type a product name — Start searching immediately\n"
        "/find — Guided product search\n"
        "/deals — Latest deals\n"
        "/favorites — View and remove saved products\n"
        "/categories — Browse shopping categories\n"
        "/retailers — Retailer connection status\n"
        "/feedback — Send a private suggestion\n"
        "/about — About DealHound\n"
        "/privacy — Privacy information\n"
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
    clear_workflow(context)
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_menu())


def clear_workflow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_search", None)
    context.user_data.pop("deal", None)
    context.user_data.pop("search", None)
    context.user_data.pop("flow", None)


async def receive_deal_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.effective_message.text.strip()
    parsed = urlparse(url)
    if not is_safe_public_url(parsed):
        await update.effective_message.reply_text(
            "Please paste a complete public retailer link beginning with `https://`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    context.user_data["deal"] = {
        "url": url,
        "retailer": retailer_name(parsed.netloc),
    }
    context.user_data["flow"] = "deal_title"
    await update.effective_message.reply_text("🏷 Send the product title.")


def is_safe_public_url(parsed) -> bool:
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local") or parsed.username or parsed.password:
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return True
    return address.is_global


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
        "Prices can change; verify before purchasing.</i>\n\n"
        "#Ad"
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
    await query.answer()
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
    app.add_handler(CommandHandler("categories", categories_command))
    app.add_handler(CommandHandler(["favorites", "favourites"], favorites_command))
    app.add_handler(CommandHandler("alerts", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("disclosure", disclosure))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("privacy", privacy_command))
    app.add_handler(CommandHandler("retailers", retailers_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("testdeal", test_deal))
    app.add_handler(CommandHandler("deal", deal_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, demo_search))
    return app


def main() -> None:
    init_database()
    threading.Thread(target=start_health_server, daemon=True).start()
    logger.info("Starting DealHound UK release %s", RELEASE_LABEL)
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
