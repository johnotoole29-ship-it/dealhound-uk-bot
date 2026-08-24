# DealHound UK Bot 🐶🔍

DealHound UK is a Telegram shopping assistant that searches live UK retailer listings, saves favourites, monitors genuine price drops and publishes owner-approved affiliate deals to a Telegram channel.

## Current version

The first version provides:

- Interactive Telegram main menu
- Live eBay UK Buy It Now search with price and condition filters
- Persistent saved favourites and genuine automatic price-drop alerts
- Product cards with delivery totals, Telegram and WhatsApp sharing
- Deals and interactive shopping categories
- Affiliate disclosure
- `/id` command for setup
- Owner-only `/testdeal` channel-post test
- Guided searches with maximum-price and condition filters
- Owner-only `/deal` creator for product links
- Private deal preview with Publish and Reject buttons
- Owner-checked Publish and Reject controls
- `/about`, `/privacy` and `/retailers` information screens
- Private `/feedback` forwarding to the configured owner
- Automatic `#Ad` label on deal cards
- Input length limits and safer public-link validation
- HTTP health endpoint for Bunny Magic Containers
- Secure environment-variable configuration

eBay UK is live. Other retailer integrations will be connected after their affiliate accounts are approved and tested.

## Environment variables

| Variable | Required now | Purpose |
|---|---:|---|
| `TELEGRAM_TOKEN` | Yes | Token supplied by BotFather |
| `ADMIN_TELEGRAM_ID` | Yes | Restricts owner-only commands |
| `DEALS_CHANNEL_ID` | For test posts | Public `@channelusername` or numeric `-100...` channel ID |
| `PORT` | No | Health server port; defaults to `8080` |
| `DATA_DIR` | No | Persistent database directory; defaults to `/data` |
| `ALERT_CHECK_INTERVAL_SECONDS` | No | Price-check interval; defaults to 6 hours and cannot be below 1 hour |
| `EBAY_CLIENT_ID` | For eBay | Production App ID |
| `EBAY_CLIENT_SECRET` | For eBay | Production Cert ID; keep secret |
| `EBAY_CAMPAIGN_ID` | For tracked links | 10-digit eBay Partner Network campaign ID |

Never commit the real token or other credentials to GitHub.

Price alerts monitor the exact eBay listing selected by the user. The bot sends a
private notification only when that listing reaches a new lowest item price since
the alert was activated. Delivery, availability and the retailer's final price
must still be checked before purchase.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_TOKEN="your-token"
python bot.py
```

## Telegram setup test

1. Start the bot and send `/start`.
2. Send `/id` and add the returned number as `ADMIN_TELEGRAM_ID`.
3. Add the deals channel username or numeric ID as `DEALS_CHANNEL_ID`.
4. Restart the container.
5. Send `/testdeal` privately to the bot.

The bot should publish a clearly marked test card in the channel.

## Manual deal workflow

Send `/deal` privately to the bot and follow the prompts for the retailer URL,
product title, current price and optional previous price. DealHound produces a
private preview. Nothing reaches the public channel until the owner presses
**Publish**.
