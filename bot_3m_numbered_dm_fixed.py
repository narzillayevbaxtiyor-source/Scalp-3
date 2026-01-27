import os
import re
import time
import asyncio
import requests
from typing import Dict

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

# ======================
# CONFIG
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
DM_CHAT_ID = int(os.getenv("DM_CHAT_ID", "0"))

BINANCE_URL = "https://data-api.binance.vision"
INTERVAL = "3m"
KLINE_LIMIT = 120
POLL_SEC = 10

# only messages like: BTC\nMUBOH
TICKER_RE = re.compile(r"^[A-Z]{2,10}$")

# ======================
# STATE
# ======================
coin_numbers: Dict[str, int] = {}
active_positions: Dict[str, bool] = {}
counter = 1

# ======================
# BINANCE
# ======================
def fetch_klines(symbol: str):
    url = f"{BINANCE_URL}/api/v3/klines"
    r = requests.get(url, params={
        "symbol": symbol + "USDT",
        "interval": INTERVAL,
        "limit": KLINE_LIMIT
    }, timeout=10)
    r.raise_for_status()
    return r.json()

# ======================
# STRATEGY
# ======================
def is_green(k): return float(k[4]) > float(k[1])
def is_red(k): return float(k[4]) < float(k[1])

def analyze(symbol: str):
    kl = fetch_klines(symbol)

    # last closed candles
    prev = kl[-2]
    cur = kl[-1]

    # SELL: green sequence broken
    if active_positions.get(symbol):
        if is_red(cur) and float(cur[3]) < float(prev[3]):
            return "SELL"

    # BUY logic
    if not active_positions.get(symbol):
        if is_red(prev) and is_green(cur):
            return None  # start make-high

        if is_red(cur) and float(cur[3]) < float(prev[3]):
            return None  # pullback start

        if is_green(cur) and float(cur[2]) > float(prev[2]):
            return "BUY"

    return None

# ======================
# TELEGRAM HANDLER
# ======================
async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global counter

    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    if not update.message or not update.message.text:
        return

    lines = update.message.text.strip().upper().splitlines()
    if len(lines) < 2:
        return

    ticker = lines[0].strip()
    status = lines[1].strip()

    if not TICKER_RE.match(ticker):
        return
    if status != "MUBOH":
        return

    if ticker not in coin_numbers:
        coin_numbers[ticker] = counter
        counter += 1
        active_positions[ticker] = False

    await context.bot.send_message(
        chat_id=DM_CHAT_ID,
        text=f"📥 TICKER QABUL QILINDI: {ticker} ({coin_numbers[ticker]})"
    )

# ======================
# LOOP
# ======================
async def scanner(app: Application):
    while True:
        for symbol, num in coin_numbers.items():
            try:
                signal = analyze(symbol)
                if signal == "BUY":
                    active_positions[symbol] = True
                    await app.bot.send_message(
                        chat_id=DM_CHAT_ID,
                        text=f"🟢 {num} BUY — {symbol}"
                    )
                elif signal == "SELL":
                    active_positions[symbol] = False
                    await app.bot.send_message(
                        chat_id=DM_CHAT_ID,
                        text=f"🔴 {num} SELL — {symbol}"
                    )
            except Exception as e:
                print("SCAN ERROR:", e)

        await asyncio.sleep(POLL_SEC)

# ======================
# MAIN
# ======================
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), on_group_message)
    )

    asyncio.create_task(scanner(app))

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
