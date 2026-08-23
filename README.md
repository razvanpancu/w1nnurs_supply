# W1NNURS SUPPLY Bot

Telegram bot starter for W1NNURS SUPPLY.

## Features in v1
- Main menu
- Latest Drops
- Available Stock
- W1NNURS Partners
- Order / Reserve
- Support
- Links users to the W1NNURS channel and support account

## Railway setup

1. Create a **private GitHub repository**.
2. Upload all files from this folder to the repository.
3. In Railway, create a new project and choose **Deploy from GitHub repo**.
4. Select the private repository.
5. Go to **Variables** and add:

   - `TELEGRAM_BOT_TOKEN` = your token from BotFather
   - `SUPPORT_USERNAME` = your Telegram username, including `@`
   - `CHANNEL_URL` = the full link to your W1NNURS SUPPLY channel

6. Railway should use the `Procfile` and start:

   `python bot.py`

7. Open Telegram and send `/start` to your bot.

## Important
Never commit your real Telegram bot token to GitHub.
Keep the repository private.
If a token is ever exposed, regenerate it immediately in BotFather.

## Next version
The next version can add:
- Partner approval
- Unique product IDs
- Live stock quantities
- Reserve / Sold workflow
- Admin-only commands
- Automatic SOLD OUT notifications
- Partner database
