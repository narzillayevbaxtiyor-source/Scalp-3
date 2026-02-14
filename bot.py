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

SCAN_PRICE_SEC = float(os.getenv("SCAN_PRICE_SEC") or "3")
REFRESH_TOP_SEC = float(os.getenv("REFRESH_TOP_SEC") or "120")
REFRESH_KLINES_SEC = float(os.getenv("REFRESH_KLINES_SEC") or "60")

BINANCE_BASE = (os.getenv("BINANCE_BASE") or "https://data-api.binance.vision").strip()

NEAR_PCT = float(os.getenv("NEAR_PCT") or "0.01")      # 1% near prior-high
VOL_SPIKE_X = float(os.getenv("VOL_SPIKE_X") or "1.8") # 1H vol spike vs MA20

STATE_FILE = os.getenv("STATE_FILE") or "state_1h_prepump.json"

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
    volume: float
    close_time: int

def now_ms() -> int:
    return int(time.time() * 1000)

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
            "last_4h_closed_open": None,
            "last_1h_closed_open": None,

            "prior_high_4h": None,     # prior high level
            "last_price": None,

            # staged signals (spam qilmaslik)
            "setup_sent_for_prior": None,  # prior_high qiymati bilan bog'lab qo'yamiz
            "ready_sent_for_prior": None,
            "buy_sent_for_prior": None,
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
    async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=20)) as r:
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
            volume=float(k[5]),
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
    if len(candles) < 2:
        raise ValueError("Not enough candles")
    return candles[-2]

# ======================
# INDICATORS
# ======================
def true_range(curr: Candle, prev: Candle) -> float:
    return max(
        curr.high - curr.low,
        abs(curr.high - prev.close),
        abs(curr.low - prev.close),
    )

def atr14(candles_closed: List[Candle], period: int = 14) -> List[float]:
    # candles_closed: faqat yopilgan shamlar ketma-ketligi
    if len(candles_closed) < period + 2:
        return []
    trs = []
    for i in range(1, len(candles_closed)):
        trs.append(true_range(candles_closed[i], candles_closed[i-1]))
    # ATR = SMA(TR, period)
    out = []
    for i in range(period - 1, len(trs)):
        window = trs[i - (period - 1): i + 1]
        out.append(sum(window) / period)
    return out

def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

# ======================
# CORE LOGIC
# ======================
async def refresh_levels(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str) -> None:
    ss = sym_state(st, symbol)

    # 4H klines (prior-high + ATR squeeze)
    k4 = await get_klines(session, symbol, "4h", 200)
    closed4 = k4[:-1]  # forming candle chiqib ketadi
    if len(closed4) < 30:
        return

    last4 = closed4[-1]
    if ss["last_4h_closed_open"] != last4.open_time:
        ss["last_4h_closed_open"] = last4.open_time

        # prior high = oldingi high'lar ichidan (last closed'dan oldingi) maximum
        prior_high = max(c.high for c in closed4[:-1])
        ss["prior_high_4h"] = prior_high

        # prior_high o'zgarsa staged signal reset (spam emas, yangi setup)
        ss["setup_sent_for_prior"] = None
        ss["ready_sent_for_prior"] = None
        ss["buy_sent_for_prior"] = None

    # 1H klines (volume spike)
    k1 = await get_klines(session, symbol, "1h", 80)
    closed1 = k1[:-1]
    if len(closed1) < 30:
        return

    last1 = closed1[-1]
    ss["last_1h_closed_open"] = last1.open_time

    # cache ichida saqlab qo'yamiz (handle_signals tez bo'lsin)
    ss["_closed4_len"] = len(closed4)
    ss["_closed1_len"] = len(closed1)

    # ATR squeeze baholash uchun ATR ro'yxatini saqlaymiz
    atrs = atr14(closed4, 14)
    ss["_atr_list"] = atrs

    # 1H volume list
    ss["_vol1_list"] = [c.volume for c in closed1]

async def handle_signals(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str, price: float) -> None:
    ss = sym_state(st, symbol)
    ss["last_price"] = price

    prior_high = ss.get("prior_high_4h")
    if not prior_high:
        return

    # ---------- 1) SETUP: 4H squeeze (ATR low) ----------
    atrs: List[float] = ss.get("_atr_list") or []
    if atrs:
        # last ATR
        last_atr = atrs[-1]
        # threshold: bottom 20% of last 50 atr values
        window = atrs[-50:] if len(atrs) >= 50 else atrs[:]
        if len(window) >= 20:
            sorted_w = sorted(window)
            idx = max(0, int(len(sorted_w) * 0.20) - 1)
            thr = sorted_w[idx]

            # squeeze = last_atr <= thr
            if last_atr <= thr:
                if ss.get("setup_sent_for_prior") != prior_high:
                    ss["setup_sent_for_prior"] = prior_high
                    await tg_send_text(
                        session,
                        f"🟨 SETUP (4H squeeze)\n"
                        f"{symbol}\n"
                        f"prior_high_4h={prior_high}\n"
                        f"ATR14={last_atr:.6f} (low volatility)"
                    )

    # ---------- 2) READY: near prior-high + 1H vol spike ----------
    near_level = prior_high * (1.0 - NEAR_PCT)
    is_near = (price >= near_level) and (price < prior_high)

    vol_list: List[float] = ss.get("_vol1_list") or []
    vol_ma20 = sma(vol_list, 20)
    vol_last = vol_list[-1] if vol_list else None
    vol_spike = False
    vol_ratio = None
    if vol_ma20 and vol_last is not None and vol_ma20 > 0:
        vol_ratio = vol_last / vol_ma20
        vol_spike = vol_ratio >= VOL_SPIKE_X

    if is_near and vol_spike:
        if ss.get("ready_sent_for_prior") != prior_high:
            ss["ready_sent_for_prior"] = prior_high
            remain_pct = (prior_high - price) / prior_high * 100.0
            await tg_send_text(
                session,
                f"🟦 READY (near breakout + 1H vol spike)\n"
                f"{symbol}\n"
                f"price={price}\n"
                f"prior_high_4h={prior_high}\n"
                f"remaining={remain_pct:.2f}% (near={NEAR_PCT*100:.2f}%)\n"
                f"1H vol spike={vol_ratio:.2f}x (target {VOL_SPIKE_X}x)"
            )

    # ---------- 3) BUY: price breaks prior-high ----------
    if price >= prior_high:
        if ss.get("buy_sent_for_prior") != prior_high:
            ss["buy_sent_for_prior"] = prior_high
            await tg_send_text(
                session,
                f"✅ BUY (breakout)\n"
                f"{symbol}\n"
                f"price={price}\n"
                f"broke prior_high_4h={prior_high}"
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

            # Telegramga yubormaymiz (faqat log)
            print(f"✅ Top {TOP_N} gainers updated. Tracking: {len(syms)}")

            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Top refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(REFRESH_TOP_SEC)

async def loop_refresh_klines(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(2)
            continue
        for symbol in syms:
            try:
                await refresh_levels(session, st, symbol)
            except Exception as e:
                print("refresh_levels error", symbol, e)
        save_state(st)
        await asyncio.sleep(REFRESH_KLINES_SEC)

async def loop_prices(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(1)
            continue
        try:
            price_map = await get_price_map(session, syms)
            for symbol, price in price_map.items():
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
    connector = aiohttp.TCPConnector(limit=60, ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        await tg_send_text(
            session,
            "🚀 Bot started (1H pre-pump): Top50 gainers -> 4H prior-high + 4H squeeze -> READY (near + 1H vol spike) -> BUY (break)"
        )
        tasks = [
            asyncio.create_task(loop_refresh_top(session, st)),
            asyncio.create_task(loop_refresh_klines(session, st)),
            asyncio.create_task(loop_prices(session, st)),
        ]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
