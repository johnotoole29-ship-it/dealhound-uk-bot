# DealHound UK Bot 🐶🔍

DealHound UK is a Telegram shopping assistant that will compare UK retailer prices and publish owner-approved affiliate deals to a Telegram channel.

## Current version

The first version provides:

- Interactive Telegram main menu
- Conversational product-search demo
- Deals, categories and price-alert screens
- Affiliate disclosure
- `/id` command for setup
- Owner-only `/testdeal` channel-post test
- Guided searches with maximum-price and condition filters
- Owner-only `/deal` creator for product links
- Private deal preview with Publish and Reject buttons
- HTTP health endpoint for Bunny Magic Containers
- Secure environment-variable configuration

Live retailer search will be connected after the relevant eBay, Awin/Currys and Amazon affiliate accounts are approved.

## Environment variables

| Variable | Required now | Purpose |
|---|---:|---|
| `TELEGRAM_TOKEN` | Yes | Token supplied by BotFather |
| `ADMIN_TELEGRAM_ID` | Yes | Restricts owner-only commands |
| `DEALS_CHANNEL_ID` | For test posts | Public `@channelusername` or numeric `-100...` channel ID |
| `PORT` | No | Health server port; defaults to `8080` |

Never commit the real token or other credentials to GitHub.

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
