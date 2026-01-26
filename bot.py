import os
import time
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set

# =========================
# ENV / CONFIG
# =========================
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()  # group id: -100...

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env yo'q")
if not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_CHAT_ID env yo'q")

# 4H trigger
TRIGGER_PCT = float(os.getenv("TRIGGER_PCT", "0.20"))  # 0.20%
ARM_COOLDOWN_SEC = int(os.getenv("ARM_COOLDOWN_SEC", "900"))  # 15 min per symbol

# Candidate pool (tez ishlashi uchun)
CANDIDATE_COUNT = int(os.getenv("CANDIDATE_COUNT", "300"))  # top quoteVolume; 0 bo'lsa: hamma
CAND_REFRESH_SEC = int(os.getenv("CAND_REFRESH_SEC", "300"))  # 5 min

# Scanning
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "50"))
LOOP_SLEEP = float(os.getenv("LOOP_SLEEP", "5"))

# 3m strategy
IMPULSE_BULL_CANDLES = int(os.getenv("IMPULSE_BULL_CANDLES", "3"))
KLINES_LIMIT_3M = int(os.getenv("KLINES_LIMIT_3M", "120"))
PRICE_POLL_SEC = float(os.getenv("PRICE_POLL_SEC", "2.0"))

SIGNAL_COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "120"))
ARM_TTL_MIN = int(os.getenv("ARM_TTL_MIN", "240"))  # ARM bo'lib BUY topilmasa o'chadi (4 soat)

# Filters
BAD_PARTS = ("UPUSDT", "DOWNUSDT", "BULL", "BEAR", "3L", "3S", "5L", "5S")
STABLE_STABLE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT"}

SESSION = requests.Session()


# =========================
# TELEGRAM
# =========================
def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = SESSION.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=25)
    if r.status_code != 200:
        print("[TG SEND ERROR]", r.status_code, r.text)


# =========================
# BINANCE
# =========================
def fetch_json(url: str, params=None):
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_klines(symbol: str, interval: str, limit: int):
    return fetch_json(f"{BINANCE_BASE_URL}/api/v3/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    })

def fetch_price_map() -> Dict[str, float]:
    arr = fetch_json(f"{BINANCE_BASE_URL}/api/v3/ticker/price")
    return {x["symbol"]: float(x["price"]) for x in arr}

def fetch_24hr() -> List[dict]:
    return fetch_json(f"{BINANCE_BASE_URL}/api/v3/ticker/24hr")

def kline_to_ohlc(k) -> Tuple[int, float, float, float, float, int]:
    return int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])


# =========================
# SPOT USDT UNIVERSE
# =========================
def get_spot_usdt_symbols() -> Set[str]:
    info = fetch_json(f"{BINANCE_BASE_URL}/api/v3/exchangeInfo")
    allowed = set()
    for s in info.get("symbols", []):
        sym = s.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym in STABLE_STABLE or any(x in sym for x in BAD_PARTS):
            continue
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT":
            continue

        permissions = s.get("permissions", [])
        permission_sets = s.get("permissionSets", [])
        is_spot = ("SPOT" in permissions) or any(("SPOT" in ps) for ps in permission_sets)

        if is_spot:
            allowed.add(sym)
    return allowed

def build_candidates(allowed: Set[str]) -> List[str]:
    t24 = fetch_24hr()
    rows = []
    for t in t24:
        sym = t.get("symbol", "")
        if sym not in allowed:
            continue
        try:
            qv = float(t.get("quoteVolume", "0"))
        except:
            continue
        if qv <= 0:
            continue
        rows.append((sym, qv))

    rows.sort(key=lambda x: x[1], reverse=True)

    if CANDIDATE_COUNT <= 0:
        return [s for (s, _) in rows]  # hamma
    return [s for (s, _) in rows[:CANDIDATE_COUNT]]


# =========================
# STRATEGY STATE
# =========================
@dataclass
class CoinState:
    # 4H trigger spamdan saqlash
    last_arm_ts: float = 0.0

    # ARM -> shu coin scalp qilinadi
    armed: bool = False
    armed_at: float = 0.0

    # 3m strategy
    mode: str = "SEARCH_IMPULSE"  # SEARCH_IMPULSE -> WAIT_PULLBACK -> WAIT_BREAK -> IN_POSITION
    pullback_high: Optional[float] = None
    last_3m_close_time: Optional[int] = None
    last_closed_low: Optional[float] = None

    in_position: bool = False
    last_buy_ts: float = 0.0
    last_sell_ts: float = 0.0

def cooldown_ok(last_ts: float, cd: int) -> bool:
    return (time.time() - last_ts) >= cd

def impulse_detected(ohlc: List[Tuple[int, float, float, float, float, int]]) -> bool:
    n = IMPULSE_BULL_CANDLES
    if len(ohlc) < n + 2:
        return False
    last = ohlc[-n:]
    return all(c > o for (_, o, _, _, c, _) in last)

def reset_3m(st: CoinState):
    st.armed = False
    st.mode = "SEARCH_IMPULSE"
    st.pullback_high = None
    st.last_3m_close_time = None
    st.last_closed_low = None
    st.in_position = False


# =========================
# 4H TRIGGER
# =========================
def check_4h_trigger(symbol: str, st: CoinState, price_map: Dict[str, float]) -> None:
    # per symbol cooldown
    if not cooldown_ok(st.last_arm_ts, ARM_COOLDOWN_SEC):
        return

    price = price_map.get(symbol)
    if price is None:
        return

    k = fetch_klines(symbol, "4h", 1)[0]  # current 4h candle
    _, o, h, l, c, _ = kline_to_ohlc(k)
    if h <= 0:
        return

    remain_pct = ((h - price) / h) * 100.0

    if 0 <= remain_pct <= TRIGGER_PCT:
        st.last_arm_ts = time.time()
        st.armed = True
        st.armed_at = time.time()
        st.mode = "SEARCH_IMPULSE"
        st.pullback_high = None
        st.last_3m_close_time = None
        st.last_closed_low = None
        st.in_position = False

        tg_send(
            f"🟡 <b>0.20% yaqin</b> | <b>{symbol}</b>\n"
            f"4H High: <b>{h}</b>\n"
            f"Narx: <b>{price}</b>\n"
            f"Qolgan: <b>{remain_pct:.4f}%</b>"
        )


# =========================
# 3m SCALP
# =========================
def run_3m(symbol: str, st: CoinState):
    if not st.armed:
        return

    # TTL: BUY topilmasa o'chadi
    if (time.time() - st.armed_at) > (ARM_TTL_MIN * 60) and not st.in_position:
        reset_3m(st)
        return

    kl = fetch_klines(symbol, "3m", KLINES_LIMIT_3M)
    ohlc = [kline_to_ohlc(k) for k in kl]

    prev = ohlc[-2]  # last closed
    cur = ohlc[-1]   # current
    _, po, ph, pl, pc, pct = prev
    _, co, ch, cl, cc, cct = cur

    # new 3m candle -> update state
    if st.last_3m_close_time != cct:
        st.last_3m_close_time = cct
        st.last_closed_low = pl

        if not st.in_position:
            if st.mode == "SEARCH_IMPULSE":
                if impulse_detected(ohlc):
                    st.mode = "WAIT_PULLBACK"
                    st.pullback_high = None

            elif st.mode == "WAIT_PULLBACK":
                # pullback: tushib yopildi
                if cc < pc:
                    st.mode = "WAIT_BREAK"
                    st.pullback_high = ch  # pullback candle high (oxirgi yopilgan)

            elif st.mode == "WAIT_BREAK":
                # pullback davom etsa, high yangilanadi
                if cc < pc:
                    st.pullback_high = max(st.pullback_high or ch, ch)

    # price-based triggers
    price = fetch_price_map().get(symbol)
    if price is None:
        return

    # BUY: pullback candle high break
    if (not st.in_position) and st.mode == "WAIT_BREAK" and st.pullback_high is not None:
        if price > st.pullback_high and cooldown_ok(st.last_buy_ts, SIGNAL_COOLDOWN_SEC):
            tg_send(f"📈 <b>BUY</b> <b>{symbol}</b>")
            st.last_buy_ts = time.time()
            st.in_position = True
            st.mode = "IN_POSITION"

    # SELL: faqat BUYdan keyin, last closed low break
    if st.in_position and st.last_closed_low is not None:
        if price < st.last_closed_low and cooldown_ok(st.last_sell_ts, SIGNAL_COOLDOWN_SEC):
            tg_send(f"📉 <b>SELL</b> <b>{symbol}</b>")
            st.last_sell_ts = time.time()
            reset_3m(st)


# =========================
# MAIN LOOP
# =========================
def main():
    tg_send("✅ 0.20% + 3m scalp bot ishga tushdi.")

    allowed = get_spot_usdt_symbols()
    candidates: List[str] = []
    last_cand_refresh = 0.0
    scan_index = 0

    states: Dict[str, CoinState] = {}

    last_price_poll = 0.0

    while True:
        try:
            now = time.time()

            # candidates refresh
            if (not candidates) or (now - last_cand_refresh > CAND_REFRESH_SEC):
                candidates = build_candidates(allowed)
                last_cand_refresh = now
                scan_index = 0

            # 1) scan 4H (batch)
            price_map = fetch_price_map()

            if candidates:
                for _ in range(min(SCAN_BATCH_SIZE, len(candidates))):
                    sym = candidates[scan_index]
                    scan_index = (scan_index + 1) % len(candidates)

                    st = states.get(sym)
                    if st is None:
                        st = CoinState()
                        states[sym] = st

                    check_4h_trigger(sym, st, price_map)

            # 2) run 3m on armed coins (tez-tez)
            if now - last_price_poll >= PRICE_POLL_SEC:
                last_price_poll = now
                for sym, st in list(states.items()):
                    if st.armed:
                        run_3m(sym, st)

        except Exception as e:
            print("[ERROR]", e)

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()
