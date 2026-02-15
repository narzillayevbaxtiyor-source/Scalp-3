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

REFRESH_TOP_SEC = float(os.getenv("REFRESH_TOP_SEC") or "120")        # top gainers refresh
REFRESH_KLINES_SEC = float(os.getenv("REFRESH_KLINES_SEC") or "60")   # kline refresh
WS_RECONNECT_SEC = float(os.getenv("WS_RECONNECT_SEC") or "3")        # ws reconnect backoff
BINANCE_BASE = (os.getenv("BINANCE_BASE") or "https://data-api.binance.vision").strip()
BINANCE_WS_BASE = (os.getenv("BINANCE_WS_BASE") or "wss://stream.binance.com:9443").strip()

STATE_FILE = os.getenv("STATE_FILE") or "state_buy1_buy2_sell.json"

# Important: top update message should NOT go to Telegram
SEND_TOP_UPDATE_TO_TG = (os.getenv("SEND_TOP_UPDATE_TO_TG") or "0").strip() == "1"

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

def last_closed(candles: List[Candle]) -> Candle:
    if len(candles) < 2:
        raise ValueError("Not enough candles")
    return candles[-2]

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
            # last CLOSED candle ids
            "last_1d_closed_open": None,
            "last_4h_closed_open": None,
            "last_1h_closed_open": None,
            "last_15m_closed_open": None,

            # last CLOSED levels
            "last_1d_high": None,
            "last_4h_high": None,
            "last_1h_high": None,
            "last_15m_low": None,

            # BUY1 watch (from a specific 4h candle that closed above 1d high)
            "buy1_watch_4h_open": None,
            "buy1_watch_level": None,
            "buy1_sent_for_4h_open": None,   # prevent duplicates

            # BUY2 watch (from a specific 1h candle that closed above 4h high)
            "buy2_watch_1h_open": None,
            "buy2_watch_level": None,
            "buy2_sent_for_1h_open": None,   # prevent duplicates

            # position tracking (sell only after buy)
            "pos_active": False,
            "pos_tag": None,                 # "BUY1" or "BUY2"
            "pos_buy_ref": None,             # candle open_time reference
            "sell_sent_for_15m_open": None,  # prevent duplicates per 15m candle

            # realtime last price to detect immediate cross
            "last_price": None,
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
# BINANCE REST
# ======================
async def get_top_gainers(session: aiohttp.ClientSession, top_n: int) -> List[str]:
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
    data = await http_get_json(session, url)

    usdt: List[Tuple[str, float]] = []
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

# ======================
# CORE LOGIC (KLINES -> watch levels)
# ======================
async def refresh_klines_for_symbol(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str) -> None:
    ss = sym_state(st, symbol)

    # 1D
    d = await get_klines(session, symbol, "1d", 3)
    d_closed = last_closed(d)
    if ss["last_1d_closed_open"] != d_closed.open_time:
        ss["last_1d_closed_open"] = d_closed.open_time
        ss["last_1d_high"] = d_closed.high
        # optional: daily close log (telegramga yuborish shart emas)
        # await tg_send_text(session, f"📅 1D CLOSED | {symbol} | high={d_closed.high} close={d_closed.close}")

    # 4H
    h4 = await get_klines(session, symbol, "4h", 3)
    h4_closed = last_closed(h4)
    if ss["last_4h_closed_open"] != h4_closed.open_time:
        ss["last_4h_closed_open"] = h4_closed.open_time
        ss["last_4h_high"] = h4_closed.high

        # BUY1 arming condition:
        # "1D last CLOSED high dan tepada yopilgan 4H candle"
        if ss.get("last_1d_high") is not None and h4_closed.close > float(ss["last_1d_high"]):
            ss["buy1_watch_4h_open"] = h4_closed.open_time
            ss["buy1_watch_level"] = h4_closed.high
            # reset "sent" for this new watch
            # (faqat shu 4h candle uchun 1 marta)
            # buy1_sent_for_4h_open != this open -> allowed
        else:
            # agar shart bo'lmasa, watch saqlab qolmaymiz
            ss["buy1_watch_4h_open"] = None
            ss["buy1_watch_level"] = None

    # 1H
    h1 = await get_klines(session, symbol, "1h", 3)
    h1_closed = last_closed(h1)
    if ss["last_1h_closed_open"] != h1_closed.open_time:
        ss["last_1h_closed_open"] = h1_closed.open_time
        ss["last_1h_high"] = h1_closed.high

        # BUY2 arming condition:
        # "4H last CLOSED high dan tepada yopilgan 1H candle"
        if ss.get("last_4h_high") is not None and h1_closed.close > float(ss["last_4h_high"]):
            ss["buy2_watch_1h_open"] = h1_closed.open_time
            ss["buy2_watch_level"] = h1_closed.high
        else:
            ss["buy2_watch_1h_open"] = None
            ss["buy2_watch_level"] = None

    # 15m
    m15 = await get_klines(session, symbol, "15m", 3)
    m15_closed = last_closed(m15)
    if ss["last_15m_closed_open"] != m15_closed.open_time:
        ss["last_15m_closed_open"] = m15_closed.open_time
        ss["last_15m_low"] = m15_closed.low

# ======================
# SIGNALS (Realtime price cross)
# ======================
def crossed_up(prev_price: Optional[float], price: float, level: float) -> bool:
    if prev_price is None:
        # agar birinchi price kelganida level ustida bo'lsa, false (kesish momenti aniq emas)
        return False
    return prev_price < level <= price

def crossed_down(prev_price: Optional[float], price: float, level: float) -> bool:
    if prev_price is None:
        return False
    return prev_price >= level > price

async def handle_realtime_price(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str, price: float) -> None:
    ss = sym_state(st, symbol)
    prev = ss.get("last_price")
    ss["last_price"] = price

    # --- BUY 1: price crosses 4H watch high (only if a 4H candle closed above last 1D high) ---
    buy1_level = ss.get("buy1_watch_level")
    buy1_open = ss.get("buy1_watch_4h_open")
    if buy1_level is not None and buy1_open is not None:
        if ss.get("buy1_sent_for_4h_open") != buy1_open:
            if crossed_up(prev, price, float(buy1_level)):
                ss["buy1_sent_for_4h_open"] = buy1_open

                # position active (sell only after buy)
                ss["pos_active"] = True
                ss["pos_tag"] = "BUY1"
                ss["pos_buy_ref"] = buy1_open
                ss["sell_sent_for_15m_open"] = None

                await tg_send_text(
                    session,
                    f"✅ BUY 1 | {symbol}\n"
                    f"price={price}\n"
                    f"Break 4H_cand_HIGH={buy1_level}"
                )

    # --- BUY 2: price crosses 1H watch high (only if a 1H candle closed above last 4H high) ---
    buy2_level = ss.get("buy2_watch_level")
    buy2_open = ss.get("buy2_watch_1h_open")
    if buy2_level is not None and buy2_open is not None:
        if ss.get("buy2_sent_for_1h_open") != buy2_open:
            # IMPORTANT: only 1H high break matters (H4 highni kesmasin)
            if crossed_up(prev, price, float(buy2_level)):
                ss["buy2_sent_for_1h_open"] = buy2_open

                ss["pos_active"] = True
                ss["pos_tag"] = "BUY2"
                ss["pos_buy_ref"] = buy2_open
                ss["sell_sent_for_15m_open"] = None

                await tg_send_text(
                    session,
                    f"✅ BUY 2 | {symbol}\n"
                    f"price={price}\n"
                    f"Break 1H_cand_HIGH={buy2_level}\n"
                    f"H4_HIGH={ss.get('last_4h_high')}"
                )

    # --- SELL: only after BUY, when price crosses below last CLOSED 15m low ---
    if ss.get("pos_active"):
        m15_low = ss.get("last_15m_low")
        m15_open = ss.get("last_15m_closed_open")
        if m15_low is not None and m15_open is not None:
            if ss.get("sell_sent_for_15m_open") != m15_open:
                if crossed_down(prev, price, float(m15_low)):
                    ss["sell_sent_for_15m_open"] = m15_open

                    await tg_send_text(
                        session,
                        f"🟥 SELL | {symbol}\n"
                        f"price={price}\n"
                        f"Break 15M_low={m15_low}\n"
                        f"from={ss.get('pos_tag')}"
                    )

                    # close position
                    ss["pos_active"] = False
                    ss["pos_tag"] = None
                    ss["pos_buy_ref"] = None

# ======================
# LOOPS
# ======================
async def loop_refresh_top(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        try:
            syms = await get_top_gainers(session, TOP_N)
            st["top_symbols"] = syms
            st["last_top_refresh_ms"] = now_ms()

            msg = f"✅ Top {TOP_N} gainers updated. Tracking: {len(syms)}"
            print(msg)  # ✅ ishlayversin (log)
            if SEND_TOP_UPDATE_TO_TG:
                await tg_send_text(session, msg)  # ❌ default OFF

            save_state(st)
        except Exception as e:
            print("Top refresh error:", type(e).__name__, e)
        await asyncio.sleep(REFRESH_TOP_SEC)

async def loop_refresh_klines(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(2)
            continue
        for symbol in syms:
            try:
                await refresh_klines_for_symbol(session, st, symbol)
            except Exception as e:
                print("kline refresh error", symbol, type(e).__name__, e)
        save_state(st)
        await asyncio.sleep(REFRESH_KLINES_SEC)

async def loop_ws_prices(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    """
    ✅ Uses global stream: /ws/!miniTicker@arr
    This avoids 451 + avoids stream limit issues.
    """
    url = f"{BINANCE_WS_BASE}/ws/!miniTicker@arr"
    backoff = WS_RECONNECT_SEC

    while True:
        try:
            print("WS connecting:", url)
            async with session.ws_connect(url, heartbeat=30) as ws:
                print("WS connected ✅")
                backoff = WS_RECONNECT_SEC

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        arr = json.loads(msg.data)

                        # filter only current top symbols
                        top = set(st.get("top_symbols") or [])
                        if not top:
                            continue

                        for data in arr:
                            sym = data.get("s")
                            if sym not in top:
                                continue
                            try:
                                price = float(data.get("c"))
                            except Exception:
                                continue
                            try:
                                await handle_realtime_price(session, st, sym, price)
                            except Exception as e:
                                print("signal error", sym, type(e).__name__, e)

                        # periodic state save (cheap)
                        save_state(st)

                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

        except Exception as e:
            print("ws connect error:", type(e).__name__, e)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)

# ======================
# MAIN
# ======================
async def main():
    st = load_state()

    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=100)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        await tg_send_text(session, "🚀 Bot started: BUY1(4H>1D) + BUY2(1H>4H) + SELL(15m low) | WS=!miniTicker@arr")

        tasks = [
            asyncio.create_task(loop_refresh_top(session, st)),
            asyncio.create_task(loop_refresh_klines(session, st)),
            asyncio.create_task(loop_ws_prices(session, st)),
        ]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
