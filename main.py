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
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

TOP_N = int(os.getenv("TOP_N") or "50")

# REST refresh pacing
REFRESH_TOP_SEC = float(os.getenv("REFRESH_TOP_SEC") or "120")
REFRESH_KLINES_SEC = float(os.getenv("REFRESH_KLINES_SEC") or "45")

STATE_FILE = os.getenv("STATE_FILE") or "state_buy1_buy2_sell.json"
BINANCE_BASE = (os.getenv("BINANCE_BASE") or "https://data-api.binance.vision").strip()

# WebSocket endpoint (Binance main stream)
BINANCE_WS_BASE = (os.getenv("BINANCE_WS_BASE") or "wss://stream.binance.com:9443").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN/TELEGRAM_TOKEN yoki TELEGRAM_CHAT_ID yo'q")

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

def last_closed(c: List[Candle]) -> Candle:
    if len(c) < 2:
        raise ValueError("Not enough candles")
    return c[-2]

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
    return {"symbols": {}, "top_symbols": [], "last_top_refresh_ms": 0}

def save_state(st: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def sym_state(st: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    s = st["symbols"].get(symbol)
    if not s:
        s = {
            # last closed candle ids
            "last_1d_open": None,
            "last_4h_open": None,
            "last_1h_open": None,
            "last_15m_open": None,

            # levels from last closed candles
            "d1_high": None,   # last closed 1D high
            "h4_high": None,   # last closed 4H high
            "m15_low": None,   # last closed 15m low

            # BUY1 arm: last closed 4H candle that closed above D1_HIGH
            "buy1_arm_4h_open": None,
            "buy1_level_high": None,   # that 4h candle HIGH

            # BUY2 arm: last closed 1H candle that closed above H4_HIGH
            "buy2_arm_1h_open": None,
            "buy2_level_high": None,   # that 1h candle HIGH (✅)

            # positions
            "pos1_active": False,
            "pos2_active": False,

            # sell gating (avoid spam)
            "sell1_sent_15m_open": None,
            "sell2_sent_15m_open": None,

            # buy gating (avoid spam)
            "buy1_sent_4h_open": None,
            "buy2_sent_1h_open": None,

            # ✅ for instant cross detection
            "prev_price": None,
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
# KLINE REFRESH (cached)
# ======================
async def refresh_levels(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str, cache: Dict[str, Any]) -> None:
    ss = sym_state(st, symbol)
    tnow = now_ms()

    async def pull(interval: str, limit: int) -> List[Candle]:
        key = f"{symbol}:{interval}"
        ent = cache.get(key)
        if ent and (tnow - ent["t"] < int(REFRESH_KLINES_SEC * 1000)):
            return ent["candles"]
        c = await get_klines(session, symbol, interval, limit)
        cache[key] = {"t": tnow, "candles": c}
        return c

    # 1D
    d = await pull("1d", 5)
    d_cl = last_closed(d)
    if ss["last_1d_open"] != d_cl.open_time:
        ss["last_1d_open"] = d_cl.open_time
        ss["d1_high"] = d_cl.high

        # 1D yangilanganda BUY1 arm qayta quriladi
        ss["buy1_arm_4h_open"] = None
        ss["buy1_level_high"] = None
        ss["buy1_sent_4h_open"] = None

    # 4H
    h4 = await pull("4h", 5)
    h4_cl = last_closed(h4)
    if ss["last_4h_open"] != h4_cl.open_time:
        ss["last_4h_open"] = h4_cl.open_time
        ss["h4_high"] = h4_cl.high

        # BUY1 arm sharti: 4H candle CLOSE > D1_HIGH
        if ss.get("d1_high") is not None and (h4_cl.close > ss["d1_high"]):
            ss["buy1_arm_4h_open"] = h4_cl.open_time
            ss["buy1_level_high"] = h4_cl.high

        # H4 yangilanganda BUY2 arm reset (yangi H4_HIGH bo'ladi)
        ss["buy2_arm_1h_open"] = None
        ss["buy2_level_high"] = None
        ss["buy2_sent_1h_open"] = None

    # 1H
    h1 = await pull("1h", 5)
    h1_cl = last_closed(h1)
    if ss["last_1h_open"] != h1_cl.open_time:
        ss["last_1h_open"] = h1_cl.open_time

        # BUY2 arm sharti: 1H candle CLOSE > H4_HIGH
        if ss.get("h4_high") is not None and (h1_cl.close > ss["h4_high"]):
            ss["buy2_arm_1h_open"] = h1_cl.open_time
            ss["buy2_level_high"] = h1_cl.high   # ✅ BUY2 level = 1H HIGH

    # 15m
    m15 = await pull("15m", 5)
    m15_cl = last_closed(m15)
    if ss["last_15m_open"] != m15_cl.open_time:
        ss["last_15m_open"] = m15_cl.open_time
        ss["m15_low"] = m15_cl.low

# ======================
# SIGNAL LOGIC (instant cross)
# ======================
def crossed_up(prev: Optional[float], now: float, level: float) -> bool:
    if prev is None:
        return now >= level
    return (prev < level) and (now >= level)

def crossed_down(prev: Optional[float], now: float, level: float) -> bool:
    if prev is None:
        return now < level
    return (prev >= level) and (now < level)

async def handle_signals(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str, price: float) -> None:
    ss = sym_state(st, symbol)
    prev = ss.get("prev_price")
    ss["prev_price"] = price

    d1_high = ss.get("d1_high")
    h4_high = ss.get("h4_high")
    m15_low = ss.get("m15_low")

    # ---- BUY 1 ----
    if (not ss["pos1_active"]) and ss.get("buy1_arm_4h_open") and ss.get("buy1_level_high"):
        arm_open = ss["buy1_arm_4h_open"]
        level = ss["buy1_level_high"]
        if (ss.get("buy1_sent_4h_open") != arm_open) and crossed_up(prev, price, level):
            ss["buy1_sent_4h_open"] = arm_open
            ss["pos1_active"] = True
            ss["sell1_sent_15m_open"] = None
            await tg_send_text(
                session,
                f"✅ BUY 1 | {symbol}\n"
                f"price={price}\n"
                f"D1_HIGH={d1_high}\n"
                f"Break 4H_cand_HIGH={level}"
            )

    # ---- BUY 2 (1H HIGH) ----
    if (not ss["pos2_active"]) and ss.get("buy2_arm_1h_open") and ss.get("buy2_level_high"):
        arm_open = ss["buy2_arm_1h_open"]
        level = ss["buy2_level_high"]  # ✅ 1H candle HIGH
        if (ss.get("buy2_sent_1h_open") != arm_open) and crossed_up(prev, price, level):
            ss["buy2_sent_1h_open"] = arm_open
            ss["pos2_active"] = True
            ss["sell2_sent_15m_open"] = None
            await tg_send_text(
                session,
                f"✅ BUY 2 | {symbol}\n"
                f"price={price}\n"
                f"H4_HIGH={h4_high}\n"
                f"Break 1H_cand_HIGH={level}"
            )

    # ---- SELL (pos1) ----
    if ss["pos1_active"] and m15_low is not None and ss.get("last_15m_open"):
        m15_open = ss["last_15m_open"]
        if crossed_down(prev, price, m15_low) and ss.get("sell1_sent_15m_open") != m15_open:
            ss["sell1_sent_15m_open"] = m15_open
            ss["pos1_active"] = False
            await tg_send_text(
                session,
                f"🟥 SELL (from BUY 1) | {symbol}\n"
                f"price={price}\n"
                f"last_closed_15m_LOW={m15_low}"
            )

    # ---- SELL (pos2) ----
    if ss["pos2_active"] and m15_low is not None and ss.get("last_15m_open"):
        m15_open = ss["last_15m_open"]
        if crossed_down(prev, price, m15_low) and ss.get("sell2_sent_15m_open") != m15_open:
            ss["sell2_sent_15m_open"] = m15_open
            ss["pos2_active"] = False
            await tg_send_text(
                session,
                f"🟥 SELL (from BUY 2) | {symbol}\n"
                f"price={price}\n"
                f"last_closed_15m_LOW={m15_low}"
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
            # xohlasang telegramga yuboriladigan qatorni ochib qo'yasan
            # await tg_send_text(session, f"✅ Top {TOP_N} gainers updated. Tracking: {len(syms)}")
            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Top refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(REFRESH_TOP_SEC)

async def loop_refresh_klines(session: aiohttp.ClientSession, st: Dict[str, Any], cache: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(2)
            continue
        for symbol in syms:
            try:
                await refresh_levels(session, st, symbol, cache)
            except Exception as e:
                print("kline refresh error", symbol, e)
        save_state(st)
        await asyncio.sleep(REFRESH_KLINES_SEC)

def build_ws_url(symbols: List[str]) -> str:
    # combined stream: /stream?streams=symbol@miniTicker/symbol@miniTicker
    streams = "/".join([f"{s.lower()}@miniticker" for s in symbols])
    return f"{BINANCE_WS_BASE}/stream?streams={streams}"

async def loop_ws_prices(session: aiohttp.ClientSession, st: Dict[str, Any], cache: Dict[str, Any]) -> None:
    backoff = 1.0
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(1)
            continue

        url = build_ws_url(syms)
        try:
            async with session.ws_connect(url, heartbeat=30, timeout=20) as ws:
                backoff = 1.0
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(msg.data)
                            data = payload.get("data") or {}
                            # miniTicker fields: s=Symbol, c=Close price
                            sym = data.get("s")
                            c = data.get("c")
                            if not sym or c is None:
                                continue
                            price = float(c)

                            # only if still tracked
                            if sym not in (st.get("top_symbols") or []):
                                continue

                            await handle_signals(session, st, sym, price)

                        except Exception as e:
                            # don't crash on parse errors
                            print("ws parse/signal error:", e)

                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

        except Exception as e:
            print("ws connect error:", type(e).__name__, e)

        # reconnect with backoff
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2.0, 30.0)

async def main():
    st = load_state()
    cache: Dict[str, Any] = {}

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=100, ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        await tg_send_text(
            session,
            "🚀 Bot started (WS instant): Top gainers | "
            "BUY1: (4H close > D1 high) then break 4H candle HIGH | "
            "BUY2: (1H close > H4 high) then break 1H candle HIGH | "
            "SELL: after BUY, break below last closed 15m LOW"
        )

        tasks = [
            asyncio.create_task(loop_refresh_top(session, st)),
            asyncio.create_task(loop_refresh_klines(session, st, cache)),
            asyncio.create_task(loop_ws_prices(session, st, cache)),
        ]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
