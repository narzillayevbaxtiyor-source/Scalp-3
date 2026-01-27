import os
import re
import time
import asyncio
import requests
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

# Signal keladigan guruh (source bot shu yerda yozadi)
GROUP_CHAT_ID = int((os.getenv("GROUP_CHAT_ID") or "0").strip() or "0")  # -100...

# Natija yuboriladigan DM (sening shaxsiy chat_id)
DM_CHAT_ID = int((os.getenv("DM_CHAT_ID") or "0").strip() or "0")  # masalan 6248061970

# Faqat shu botdan signal ol (ixtiyoriy). Masalan: HukmCrypto_bot
SIGNAL_BOT_USERNAME = (os.getenv("SIGNAL_BOT_USERNAME") or "").strip().lstrip("@")

# Binance
BINANCE_BASE_URL = (os.getenv("BINANCE_BASE_URL") or "https://data-api.binance.vision").strip()
PAIR_SUFFIX = (os.getenv("PAIR_SUFFIX") or "USDT").strip().upper()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
KLINES_LIMIT_3M = int(os.getenv("KLINES_LIMIT_3M", "150"))

COOLDOWN_SIGNAL_SECONDS = int(os.getenv("COOLDOWN_SIGNAL_SECONDS", "120"))
WATCH_EXPIRE_SECONDS = int(os.getenv("WATCH_EXPIRE_SECONDS", "10800"))  # 3 soat

# Nomerlar: 1..33
MAX_TRADE_ID = int(os.getenv("MAX_TRADE_ID", "33"))

SESSION = requests.Session()

TICKER_RE = re.compile(r"\b[A-Z0-9]{2,12}\b")
MUBOH_RE = re.compile(r"\bMUBOH\b", re.IGNORECASE)

# =========================
# BINANCE HELPERS
# =========================
def fetch_json(path: str, params=None):
    url = f"{BINANCE_BASE_URL}{path}"
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_klines(symbol: str, interval: str, limit: int):
    return fetch_json("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

def kline_to_ohlc(k) -> Tuple[int, float, float, float, float, int]:
    return int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])

def is_green(o: float, c: float) -> bool:
    return c > o

def is_red(o: float, c: float) -> bool:
    return c < o

# =========================
# TICKER/SYMBOL
# =========================
def ticker_to_symbol(ticker: str) -> str:
    t = ticker.strip().upper()
    if t.endswith(PAIR_SUFFIX):
        return t
    return f"{t}{PAIR_SUFFIX}"

def symbol_to_ticker(symbol: str) -> str:
    if symbol.endswith(PAIR_SUFFIX):
        return symbol[:-len(PAIR_SUFFIX)]
    return symbol

def extract_ticker(text: str) -> Optional[str]:
    if not text:
        return None
    tokens = TICKER_RE.findall(text.upper())
    if not tokens:
        return None

    # avval suffixsiz (BTC) topamiz
    for t in tokens:
        if 2 <= len(t) <= 12 and not t.endswith(PAIR_SUFFIX):
            return t

    # bo‘lmasa BTCUSDT
    for t in tokens:
        if 2 <= len(t) <= 12:
            return t

    return None

# =========================
# STRATEGY STATE
# =========================
@dataclass
class CoinState:
    # WAIT_GREEN_AFTER_RED -> UP_RUN -> WAIT_BREAK_RED_LOW -> PULLBACK -> IN_POSITION
    mode: str = "WAIT_GREEN_AFTER_RED"

    # Make a high:
    saw_red_before_green: bool = False
    last_red_low_for_makehigh: Optional[float] = None

    # Pullback:
    last_down_candle_high: Optional[float] = None

    # Candle tracking:
    last_closed_time: Optional[int] = None

    # Position:
    in_position: bool = False
    rise_count: int = 0
    last_rise_low: Optional[float] = None

    # Trade numbering:
    trade_id: Optional[int] = None

    # watch:
    added_ts: float = field(default_factory=time.time)

    # cooldown:
    last_signal_ts: Dict[str, float] = field(default_factory=dict)

def cooldown_ok(st: CoinState, key: str) -> bool:
    return (time.time() - st.last_signal_ts.get(key, 0.0)) >= COOLDOWN_SIGNAL_SECONDS

def mark_signal(st: CoinState, key: str):
    st.last_signal_ts[key] = time.time()

def reset_state(st: CoinState, keep_trade_id: bool = False):
    trade_id = st.trade_id if keep_trade_id else None
    st.mode = "WAIT_GREEN_AFTER_RED"
    st.saw_red_before_green = False
    st.last_red_low_for_makehigh = None
    st.last_down_candle_high = None
    st.last_closed_time = None
    st.in_position = False
    st.rise_count = 0
    st.last_rise_low = None
    st.trade_id = trade_id

# =========================
# WATCHLIST + GLOBAL COUNTER
# =========================
WATCH: Dict[str, CoinState] = {}
NEXT_TRADE_ID = 1

def alloc_trade_id() -> int:
    global NEXT_TRADE_ID
    tid = NEXT_TRADE_ID
    NEXT_TRADE_ID += 1
    if NEXT_TRADE_ID > MAX_TRADE_ID:
        NEXT_TRADE_ID = 1
    return tid

# =========================
# TELEGRAM INCOMING (ONLY bot messages containing MUBOH + ticker)
# =========================
def is_allowed_source(update: Update) -> bool:
    if not update.message:
        return False
    u = update.message.from_user
    if not u or not u.is_bot:
        return False
    if SIGNAL_BOT_USERNAME:
        if (u.username or "").lower() != SIGNAL_BOT_USERNAME.lower():
            return False
    return True

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # faqat belgilangan guruh
    if GROUP_CHAT_ID and update.message.chat_id != GROUP_CHAT_ID:
        return

    # faqat botdan
    if not is_allowed_source(update):
        return

    text = update.message.text.strip()

    # faqat MUBOH bo'lsa
    if not MUBOH_RE.search(text):
        return

    ticker = extract_ticker(text)
    if not ticker:
        return

    sym = ticker_to_symbol(ticker)
    st = WATCH.get(sym)
    if st is None:
        st = CoinState()
        WATCH[sym] = st
    else:
        reset_state(st)

    st.added_ts = time.time()

# =========================
# OUTGOING MESSAGE (DM)
# =========================
async def send_signal_dm(app: Application, trade_id: int, side: str, ticker: str):
    msg = f"{trade_id} {side} {ticker}"
    await app.bot.send_message(chat_id=DM_CHAT_ID, text=msg)

# =========================
# STRATEGY CORE (3m)
# =========================
async def analyze_symbol(symbol: str, st: CoinState, app: Application):
    kl = fetch_klines(symbol, "3m", KLINES_LIMIT_3M)
    ohlc = [kline_to_ohlc(k) for k in kl]

    prev = ohlc[-2]  # last closed candle
    cur = ohlc[-1]   # current candle (ongoing)

    _, po, ph, pl, pc, prev_ct = prev
    _, co, ch, cl, cc, cur_ct = cur

    # faqat yangi yopilgan 3m shamga bir marta ishlasin
    if st.last_closed_time == prev_ct:
        return
    st.last_closed_time = prev_ct

    ticker = symbol_to_ticker(symbol)

    # ========= SELL (faqat BUYdan keyin) =========
    if st.in_position:
        if is_green(po, pc):
            st.rise_count += 1
            st.last_rise_low = pl
        else:
            st.rise_count = 0
            st.last_rise_low = None

        # oxirgi yopilgan sham low ini narx kesssa
        if st.rise_count >= 1 and st.last_rise_low is not None:
            if cl < st.last_rise_low:
                if st.trade_id is not None and cooldown_ok(st, "SELL"):
                    await send_signal_dm(app, st.trade_id, "SELL", ticker)
                    mark_signal(st, "SELL")

                WATCH.pop(symbol, None)
        return

    # ========= MAKE A HIGH -> PULLBACK -> BUY =========

    # 1) qizil shamdan keyin 1-yashil sham: make-high start
    if st.mode == "WAIT_GREEN_AFTER_RED":
        if is_red(po, pc):
            st.saw_red_before_green = True
            return

        if st.saw_red_before_green and is_green(po, pc):
            st.mode = "UP_RUN"
            st.saw_red_before_green = False
        return

    # 2) UP_RUN: yashillar ketma-ket, birinchi qizil sham chiqqanda red_low eslab qolamiz
    if st.mode == "UP_RUN":
        if is_green(po, pc):
            return

        if is_red(po, pc):
            st.last_red_low_for_makehigh = pl
            st.mode = "WAIT_BREAK_RED_LOW"
        return

    # 3) narx shu qizil sham LOWini kesssa -> make-high end + pullback start
    if st.mode == "WAIT_BREAK_RED_LOW":
        red_low = st.last_red_low_for_makehigh
        if red_low is None:
            st.mode = "WAIT_GREEN_AFTER_RED"
            return

        if cl < red_low:
            st.mode = "PULLBACK"
            st.last_down_candle_high = None
        return

    # 4) PULLBACK: tushuvchi shamlar ketma-ketligida oxirgi yopilgan sham HIGH
    #    narx shu HIGHni kesssa -> BUY
    if st.mode == "PULLBACK":
        if is_red(po, pc):
            st.last_down_candle_high = ph
            return

        if st.last_down_candle_high is None:
            return

        if ch > st.last_down_candle_high:
            if cooldown_ok(st, "BUY"):
                if st.trade_id is None:
                    st.trade_id = alloc_trade_id()
                await send_signal_dm(app, st.trade_id, "BUY", ticker)
                mark_signal(st, "BUY")

            st.in_position = True
            st.mode = "IN_POSITION"
            st.rise_count = 0
            st.last_rise_low = None
        return

# =========================
# LOOP
# =========================
async def loop(app: Application):
    if DM_CHAT_ID == 0:
        print("❌ DM_CHAT_ID yo‘q")
        return

    await app.bot.send_message(chat_id=DM_CHAT_ID, text="✅ 3m bot ishga tushdi (DM).")

    while True:
        try:
            now = time.time()

            # expire watchlar
            for sym, st in list(WATCH.items()):
                if (now - st.added_ts) > WATCH_EXPIRE_SECONDS:
                    WATCH.pop(sym, None)

            # analiz
            for sym, st in list(WATCH.items()):
                await analyze_symbol(sym, st, app)

        except Exception as e:
            print("[LOOP ERROR]", e)

        await asyncio.sleep(POLL_SECONDS)

async def post_init(app: Application):
    app.create_task(loop(app))

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env yo‘q")
    if GROUP_CHAT_ID == 0:
        raise SystemExit("GROUP_CHAT_ID env yo‘q yoki 0")
    if DM_CHAT_ID == 0:
        raise SystemExit("DM_CHAT_ID env yo‘q yoki 0")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_group_message))

    print("Running 3m bot (group input -> DM output)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
