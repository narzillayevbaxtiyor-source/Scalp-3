import os
import time
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ======================
# ENV
# ======================
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

TOP_N = int(os.getenv("TOP_N") or "50")

SCAN_PRICE_SEC = float(os.getenv("SCAN_PRICE_SEC") or "3")           # tezroq
REFRESH_TOP_SEC = float(os.getenv("REFRESH_TOP_SEC") or "120")
REFRESH_4H_SEC = float(os.getenv("REFRESH_4H_SEC") or "30")          # 4h pattern refresh

KLINE_LIMIT = int(os.getenv("KLINE_LIMIT") or "200")

STATE_FILE = os.getenv("STATE_FILE") or "state_make_high_ready_buy.json"
BINANCE_BASE = (os.getenv("BINANCE_BASE") or "https://data-api.binance.vision").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID yo'q")

# ======================
# DATA
# ======================
@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int

def now_ms() -> int:
    return int(time.time() * 1000)

def is_red(c: Candle) -> bool:
    return c.close < c.open

def is_green(c: Candle) -> bool:
    return c.close > c.open

# ======================
# STATE
# ======================
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "symbols": {},
        "top_symbols": [],
        "last_top_refresh_ms": 0,
    }

def save_state(st: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def sym_state(st: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    s = st["symbols"].get(symbol)
    if not s:
        s = {
            # 4h tracking
            "last_4h_closed_open": None,
            "last_4h_closed_high": None,

            # make-a-high start candle
            "mah_start_open": None,     # open_time of start candle
            "mah_min": None,            # low of start candle

            # signals gating
            "ready_for_mah_start": None,   # mah_start_open for which READY sent
            "buy_for_mah_start": None,     # mah_start_open for which BUY sent

            "ready_active": False,         # READY happened, now waiting for BUY
        }
        st["symbols"][symbol] = s
    return s

# ======================
# HTTP / TG
# ======================
async def http_get_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json()

async def tg_send_text(session: aiohttp.ClientSession, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
        await r.text()

# ======================
# BINANCE
# ======================
async def get_top_gainers(session: aiohttp.ClientSession, top_n: int) -> List[str]:
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
    data = await http_get_json(session, url)

    usdt = []
    for x in data:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym.endswith("BUSDUSDT") or sym.endswith("USDCUSDT"):
            continue
        if "UPUSDT" in sym or "DOWNUSDT" in sym or "BULLUSDT" in sym or "BEARUSDT" in sym:
            continue
        try:
            pct = float(x.get("priceChangePercent", "0") or "0")
        except Exception:
            continue
        usdt.append((sym, pct))

    usdt.sort(key=lambda t: t[1], reverse=True)
    return [s for s, _ in usdt[:top_n]]

async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int) -> List[Candle]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    raw = await http_get_json(session, url, params={"symbol": symbol, "interval": interval, "limit": str(limit)})
    out: List[Candle] = []
    for k in raw:
        out.append(Candle(
            open_time=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            close_time=int(k[6]),
        ))
    return out

async def get_price_map(session: aiohttp.ClientSession, symbols: List[str]) -> Dict[str, float]:
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    data = await http_get_json(session, url)
    wanted = set(symbols)
    mp: Dict[str, float] = {}
    for x in data:
        sym = x.get("symbol")
        if sym in wanted:
            mp[sym] = float(x["price"])
    return mp

def last_closed(candles: List[Candle]) -> Candle:
    # candles[-1] forming, candles[-2] last closed
    if len(candles) < 2:
        raise ValueError("Not enough candles")
    return candles[-2]

# ======================
# MAKE A HIGH DETECTOR
# ======================
def find_last_make_high_start(candles_4h: List[Candle]) -> Optional[Candle]:
    """
    Oxirgi red -> green transition ni topadi (oldingi qizil, keyingi yashil).
    Shu yashil sham - make a high START.
    """
    if len(candles_4h) < 5:
        return None

    closed = candles_4h[:-1]  # forming candle ni olib tashlaymiz
    # oxiridan qidiramiz
    for i in range(len(closed) - 1, 0, -1):
        prev = closed[i - 1]
        cur = closed[i]
        if is_red(prev) and is_green(cur):
            return cur
    return None

# ======================
# REFRESH 4H CONTEXT
# ======================
async def refresh_4h_context(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str) -> None:
    ss = sym_state(st, symbol)

    candles = await get_klines(session, symbol, "4h", KLINE_LIMIT)
    lc = last_closed(candles)

    # last closed 4h high update
    if ss["last_4h_closed_open"] != lc.open_time:
        ss["last_4h_closed_open"] = lc.open_time
        ss["last_4h_closed_high"] = lc.high

    # detect last make-high start
    start = find_last_make_high_start(candles)
    if not start:
        return

    # Agar yangi make-high start chiqsa: READY/BUT reset (faqat shu start uchun)
    if ss.get("mah_start_open") != start.open_time:
        ss["mah_start_open"] = start.open_time
        ss["mah_min"] = start.low

        ss["ready_for_mah_start"] = None
        ss["buy_for_mah_start"] = None
        ss["ready_active"] = False

# ======================
# SIGNALS
# ======================
async def handle_signals(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str, price: float) -> None:
    ss = sym_state(st, symbol)

    mah_start = ss.get("mah_start_open")
    mah_min = ss.get("mah_min")
    last4h_high = ss.get("last_4h_closed_high")
    last4h_open = ss.get("last_4h_closed_open")

    if not mah_start or not mah_min or not last4h_high or not last4h_open:
        return

    # 1) READY: narx make-high minimum (start candle low) ni kessa
    if price <= mah_min:
        if ss.get("ready_for_mah_start") != mah_start:
            ss["ready_for_mah_start"] = mah_start
            ss["ready_active"] = True
            await tg_send_text(
                session,
                f"🟨 READY | {symbol}\n"
                f"price={price}\n"
                f"make_high_min={mah_min}\n"
                f"mah_start_open={mah_start}"
            )

    # 2) BUY: READY dan keyin, narx oxirgi yopilgan 4H high ni kessa
    if ss.get("ready_active"):
        if price >= last4h_high:
            if ss.get("buy_for_mah_start") != mah_start:
                ss["buy_for_mah_start"] = mah_start
                ss["ready_active"] = False  # BUY dan keyin o'chiramiz
                await tg_send_text(
                    session,
                    f"🟦 BUY | {symbol}\n"
                    f"price={price}\n"
                    f"last_closed_4h_high={last4h_high}\n"
                    f"mah_start_open={mah_start}"
                )

# ======================
# LOOPS
# ======================
async def loop_refresh_top(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        try:
            syms = await get_top_gainers(session, TOP_N)
            st["top_symbols"] = syms
            st["last_top_refresh_ms"] = now_ms()
            await tg_send_text(session, f"✅ Top {TOP_N} gainers updated. Tracking={len(syms)}")
            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Top refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(REFRESH_TOP_SEC)

async def loop_refresh_4h(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(2)
            continue
        for symbol in syms:
            try:
                await refresh_4h_context(session, st, symbol)
            except Exception as e:
                print("4h refresh error", symbol, e)
        save_state(st)
        await asyncio.sleep(REFRESH_4H_SEC)

async def loop_prices(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(1)
            continue
        try:
            mp = await get_price_map(session, syms)
            for symbol, price in mp.items():
                try:
                    await handle_signals(session, st, symbol, price)
                except Exception as e:
                    print("signal error", symbol, e)
            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Price loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(SCAN_PRICE_SEC)

async def main():
    st = load_state()

    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=80, ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        await tg_send_text(session, "🚀 Bot started: Top50 | 4H make-high -> READY(low break) -> BUY(last closed 4H high break)")

        tasks = [
            asyncio.create_task(loop_refresh_top(session, st)),
            asyncio.create_task(loop_refresh_4h(session, st)),
            asyncio.create_task(loop_prices(session, st)),
        ]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
