import os
import re
import time
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

# Signalni qayerdan OLADI (guruh/chat ID)
# Masalan: -100xxxxxxxxxx
SOURCE_GROUP_ID = int((os.getenv("SOURCE_GROUP_ID") or "0").strip() or "0")

# Signalni qayerga YUBORADI (DM chat id: sizning user id yoki private chat id)
DM_CHAT_ID_RAW = (os.getenv("DM_CHAT_ID") or "").strip()
# DM_CHAT_ID xato bo'lmasin deb:
try:
    DM_CHAT_ID = int(DM_CHAT_ID_RAW)
except Exception:
    DM_CHAT_ID = 0

BINANCE_BASE_URL = (os.getenv("BINANCE_BASE_URL") or "https://data-api.binance.vision").strip()
PAIR_SUFFIX = (os.getenv("PAIR_SUFFIX") or "USDT").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "120"))

# Watchlist: guruhdan ticker kelgach, necha sekund kuzatadi
WATCH_TTL_SECONDS = int(os.getenv("WATCH_TTL_SECONDS", "7200"))  # 2 soat

# Parallel request limit
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "8"))

# Raqamlar 1..33
MAX_TRADE_NUM = int(os.getenv("MAX_TRADE_NUM", "33"))

# Guruhdagi xabar "MUBOH" so'zini o'z ichiga olishi shart
REQUIRE_MUBOH_WORD = (os.getenv("REQUIRE_MUBOH_WORD") or "1").strip()  # "1" bo'lsa yoqilgan

# Ticker regex (BTC, ETH, 1000PEPE, ...)
TICKER_RE = re.compile(r"^[A-Z0-9]{2,15}$")

# =========================
# DATA STRUCTURES
# =========================
def is_green(o: float, c: float) -> bool:
    return c > o

def is_red(o: float, c: float) -> bool:
    return c < o

def kline_to_ohlc(k) -> Tuple[int, float, float, float, float, int]:
    # openTime, open, high, low, close, closeTime
    return int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])

@dataclass
class SymState:
    # monitoring
    active: bool = True
    last_seen_ts: float = field(default_factory=lambda: time.time())

    # candle sync
    last_close_time: Optional[int] = None

    # strategy states:
    # WAIT_START -> IN_UPRUN -> WAIT_BREAK_RED_LOW -> IN_PULLBACK -> IN_POSITION
    mode: str = "WAIT_START"

    # make-a-high tracking
    had_red_before_green: bool = False
    last_red_low: Optional[float] = None

    # pullback tracking
    last_pullback_red_high: Optional[float] = None

    # position
    in_position: bool = False
    position_id: Optional[int] = None
    last_green_low_for_sell: Optional[float] = None

# =========================
# BINANCE CLIENT (aiohttp)
# =========================
class BinanceClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=25)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()

    async def fetch_klines(self, symbol: str, interval: str, limit: int) -> List:
        assert self.session is not None
        url = f"{self.base_url}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
        async with self.session.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json()

# =========================
# GLOBAL STATE
# =========================
WATCH: Dict[str, SymState] = {}
TRADE_COUNTER = 0  # 1..33 cyclic
LOCK = asyncio.Lock()

def next_trade_id() -> int:
    global TRADE_COUNTER
    TRADE_COUNTER += 1
    if TRADE_COUNTER > MAX_TRADE_NUM:
        TRADE_COUNTER = 1
    return TRADE_COUNTER

def extract_ticker_from_text(text: str) -> Optional[str]:
    """
    Guruhdagi xabardan ticker ajratamiz.
    Ko'p botlar birinchi qatorda "BTC" yoki "BTCUSDT" yoki "#BTC" beradi.
    Biz faqat oddiy ticker (BTC) ni olamiz.
    """
    t = (text or "").strip().upper()
    if not t:
        return None

    # MUBOH sharti
    if REQUIRE_MUBOH_WORD == "1":
        if "MUBOH" not in t:
            return None

    # birinchi tokenni olishga harakat qilamiz
    # masalan: "BTC\n\nMUBOH ✅" -> BTC
    # "#BTC MUBOH" -> BTC
    first_line = t.splitlines()[0].strip()
    first_line = first_line.replace("#", "").replace("$", "").strip()

    # ba'zan "BTCUSDT" bo'ladi -> suffixni kesib tashlaymiz
    if first_line.endswith(PAIR_SUFFIX):
        first_line = first_line[: -len(PAIR_SUFFIX)]

    # faqat ticker bo'lsa
    if TICKER_RE.match(first_line):
        return first_line

    # agar birinchi qatorda bo'lmasa, butun matndan qidiramiz:
    # (lekin ehtiyot: ko'p so'zlar bo'lishi mumkin)
    words = re.findall(r"[A-Z0-9]{2,15}", t)
    for w in words[:10]:
        ww = w
        if ww.endswith(PAIR_SUFFIX):
            ww = ww[: -len(PAIR_SUFFIX)]
        if TICKER_RE.match(ww):
            return ww

    return None

# =========================
# STRATEGY (3m)
# =========================
async def process_symbol(app: Application, bz: BinanceClient, ticker: str, st: SymState):
    symbol = f"{ticker}{PAIR_SUFFIX}"

    try:
        kl = await bz.fetch_klines(symbol, "3m", KLINES_LIMIT)
    except Exception as e:
        # Agar symbol yo'q bo'lsa yoki API muammo bo'lsa, jim o'tamiz
        return

    ohlc = [kline_to_ohlc(k) for k in kl]
    if len(ohlc) < 5:
        return

    # current closed candle = last element (Binance klines: last can be current forming;
    # lekin limit katta bo'lsa ham, eng oxirgi kline ko'pincha ongoing bo'ladi.
    # Biz xavfsizligi uchun: prev_closed = ohlc[-2], curr = ohlc[-1]
    prev = ohlc[-2]
    cur = ohlc[-1]

    _, po, ph, pl, pc, pct = prev
    _, co, ch, cl, cc, cct = cur

    # only once per new 3m close time
    if st.last_close_time == pct:
        return
    st.last_close_time = pct

    # 0) Position bo'lsa: SELL qoidasi
    if st.in_position:
        # O'suvchi shamlar ketma-ketligida oxirgi yopilgan yashil sham low sini saqlaymiz
        if is_green(po, pc):
            st.last_green_low_for_sell = pl

        # Narx (current candle low) oxirgi yashil sham low dan pastga ketsa -> SELL
        if st.last_green_low_for_sell is not None and cl < st.last_green_low_for_sell:
            pid = st.position_id or 0
            await app.bot.send_message(
                chat_id=DM_CHAT_ID,
                text=f"{pid} SELL {ticker}",
            )
            # reset
            st.in_position = False
            st.position_id = None
            st.last_green_low_for_sell = None
            st.mode = "WAIT_START"
            st.had_red_before_green = False
            st.last_red_low = None
            st.last_pullback_red_high = None
        return

    # 1) WAIT_START: qizil shamdan keyin yashil sham paydo bo'lishi
    if st.mode == "WAIT_START":
        # avval qizil sham ko'rinsin
        if is_red(po, pc):
            st.had_red_before_green = True
            return

        # qizildan keyin yashil keldi -> make-a-high boshlanishi
        if st.had_red_before_green and is_green(po, pc):
            st.mode = "IN_UPRUN"
            st.last_red_low = None
            return
        return

    # 2) IN_UPRUN: yashil shamlar ketma-ket. Qizil sham chiqqanda WAIT_BREAK_RED_LOW ga o'tamiz
    if st.mode == "IN_UPRUN":
        if is_green(po, pc):
            return

        if is_red(po, pc):
            # shu qizil sham (prev closed) -> uning LOW sini eslab qolamiz
            st.last_red_low = pl
            st.mode = "WAIT_BREAK_RED_LOW"
            return

        # doji bo'lsa jim
        return

    # 3) WAIT_BREAK_RED_LOW: narx shu qizil sham LOW ini kesib o'tsa (low-break)
    if st.mode == "WAIT_BREAK_RED_LOW":
        if st.last_red_low is None:
            st.mode = "WAIT_START"
            st.had_red_before_green = False
            return

        # current candle low red_low dan past bo'lsa -> make-a-high tugadi va pullback boshlandi
        if cl < st.last_red_low:
            st.mode = "IN_PULLBACK"
            st.last_pullback_red_high = None
        return

    # 4) IN_PULLBACK: tushuvchi (qizil) shamlar ketma-ketligida
    # Oxirgi yopilgan qizil sham HIGH ini trigger qilib qo'yamiz,
    # narx (current high) shuni kesib o'tsa -> BUY
    if st.mode == "IN_PULLBACK":
        # Agar prev sham qizil bo'lsa, trigger yangilanadi
        if is_red(po, pc):
            st.last_pullback_red_high = ph

        # BUY sharti: current candle high > last closed red candle high
        if st.last_pullback_red_high is not None and ch > st.last_pullback_red_high:
            async with LOCK:
                pid = next_trade_id()
            st.in_position = True
            st.position_id = pid
            st.last_green_low_for_sell = None
            st.mode = "IN_POSITION"

            await app.bot.send_message(
                chat_id=DM_CHAT_ID,
                text=f"{pid} BUY {ticker}",
            )
        return

# =========================
# BACKGROUND LOOP
# =========================
async def scanner_loop(app: Application):
    # env check
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN yo'q")
        return
    if SOURCE_GROUP_ID == 0:
        print("❌ SOURCE_GROUP_ID yo'q (guruh ID ni kiriting)")
        return
    if DM_CHAT_ID == 0:
        print("❌ DM_CHAT_ID noto‘g‘ri yoki yo‘q (faqat raqam bo‘lsin)")
        return

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with BinanceClient(BINANCE_BASE_URL) as bz:
        while True:
            try:
                now = time.time()

                # expired tickersni o'chirish
                expired = []
                for tkr, st in WATCH.items():
                    if (now - st.last_seen_ts) > WATCH_TTL_SECONDS:
                        expired.append(tkr)
                for tkr in expired:
                    WATCH.pop(tkr, None)

                # copy list
                tickers = list(WATCH.keys())
                if not tickers:
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                async def one(tkr: str):
                    async with sem:
                        st = WATCH.get(tkr)
                        if not st:
                            return
                        await process_symbol(app, bz, tkr, st)

                await asyncio.gather(*(one(t) for t in tickers))

            except Exception as e:
                print("[scanner error]", e)

            await asyncio.sleep(POLL_SECONDS)

# =========================
# TELEGRAM HANDLER
# =========================
async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    chat = update.effective_chat
    if not chat:
        return

    # faqat SOURCE_GROUP_ID dan olamiz
    if chat.id != SOURCE_GROUP_ID:
        return

    ticker = extract_ticker_from_text(msg.text)
    if not ticker:
        return

    # watchlistga qo'shamiz / refresh qilamiz
    st = WATCH.get(ticker)
    if st is None:
        st = SymState(active=True)
        WATCH[ticker] = st
    st.last_seen_ts = time.time()

# =========================
# MAIN
# =========================
async def post_init(app: Application):
    # scanner loopni ishga tushiramiz
    app.create_task(scanner_loop(app))
    print("✅ 3m bot ishga tushdi (v20). Guruhdan MUBOH tickerni kutyapman...")

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN yo'q")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_group_message))

    # v20 run_polling
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
