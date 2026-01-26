import os
import re
import time
import requests
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

# =========================
# CONFIG
# =========================
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
# Shu chatda ishlasin (sizning kanal/guruh/pm chat_id)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Telegram polling
TG_POLL_SECONDS = int(os.getenv("TG_POLL_SECONDS", "2"))
TG_LONGPOLL_TIMEOUT = int(os.getenv("TG_LONGPOLL_TIMEOUT", "25"))

# Strategy
IMPULSE_BULL_CANDLES = int(os.getenv("IMPULSE_BULL_CANDLES", "3"))  # impuls uchun ketma-ket bullish
KLINES_LIMIT_3M = int(os.getenv("KLINES_LIMIT_3M", "120"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))                 # narx tekshirish tezligi

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "120"))        # BUY/SELL spamdan saqlash
ARM_TTL_MINUTES = int(os.getenv("ARM_TTL_MINUTES", "240"))          # signal xabari kelgach 3m set qidirish vaqti (default 4 soat)

# =========================
# BASIC CHECKS
# =========================
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env yo'q")
if not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_CHAT_ID env yo'q")

SESSION = requests.Session()

# =========================
# BINANCE HELPERS
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

def fetch_last_price(symbol: str) -> float:
    d = fetch_json(f"{BINANCE_BASE_URL}/api/v3/ticker/price", {"symbol": symbol})
    return float(d["price"])

def kline_to_ohlc(k) -> Tuple[int, float, float, float, float, int]:
    # openTime, open, high, low, close, closeTime
    return int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])

# =========================
# TELEGRAM HELPERS (send + receive)
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

def tg_get_updates(offset: Optional[int]) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {
        "timeout": TG_LONGPOLL_TIMEOUT,
        "allowed_updates": ["message"]
    }
    if offset is not None:
        params["offset"] = offset
    r = SESSION.get(url, params=params, timeout=TG_LONGPOLL_TIMEOUT + 10)
    r.raise_for_status()
    return r.json()

# xabardan SYMBOL ajratish: masalan BTCUSDT, ROSEUSDT ...
SYMBOL_RE = re.compile(r"\b([A-Z0-9]{3,20}USDT)\b")

def extract_symbol(text: str) -> Optional[str]:
    if not text:
        return None
    m = SYMBOL_RE.search(text.upper())
    return m.group(1) if m else None

# =========================
# STRATEGY STATE
# =========================
@dataclass
class TradeState:
    # signal botdan coin kelgach aktiv bo'ladi
    active: bool = False
    activated_at: float = 0.0

    # 3m setup bosqichlari
    mode: str = "SEARCH_IMPULSE"  # SEARCH_IMPULSE -> WAIT_PULLBACK -> WAIT_BREAK
    pullback_high: Optional[float] = None
    last_3m_close_time: Optional[int] = None

    # BUYdan keyin
    in_position: bool = False
    last_signal_ts_buy: float = 0.0
    last_signal_ts_sell: float = 0.0

    # SELL uchun: oxirgi yopilgan 3m candle low (trailing)
    last_closed_low: Optional[float] = None

def cooldown_ok(last_ts: float) -> bool:
    return (time.time() - last_ts) >= COOLDOWN_SECONDS

def impulse_detected(ohlc: List[Tuple[int, float, float, float, float, int]]) -> bool:
    n = IMPULSE_BULL_CANDLES
    if len(ohlc) < n + 2:
        return False
    last = ohlc[-n:]
    # ketma-ket bullish
    return all(c > o for (_, o, _, _, c, _) in last)

# =========================
# CORE LOGIC PER SYMBOL
# =========================
def step_3m(symbol: str, st: TradeState):
    """Signal kelgan coin uchun 3m set qidiradi va BUY/SELL beradi."""
    if not st.active:
        return

    # TTL: juda uzoq active bo'lib qolmasin (BUY bo'lmasa ham)
    if (time.time() - st.activated_at) > (ARM_TTL_MINUTES * 60) and not st.in_position:
        st.active = False
        st.mode = "SEARCH_IMPULSE"
        st.pullback_high = None
        st.last_3m_close_time = None
        return

    # 3m klines (oxirgi yopilgan candle ma'lumotlari uchun)
    kl = fetch_klines(symbol, "3m", KLINES_LIMIT_3M)
    ohlc = [kline_to_ohlc(k) for k in kl]

    prev = ohlc[-2]  # oxirgi yopilgan candle
    cur = ohlc[-1]   # hozirgi candle (close_time bor)
    _, po, ph, pl, pc, pct = prev
    _, co, ch, cl, cc, cct = cur

    # candle yangilanganini tekshirish (yangi 3m yopilganda update)
    if st.last_3m_close_time != cct:
        st.last_3m_close_time = cct
        # SELL trailing uchun last closed low ni doim yangilab boramiz (BUYdan keyin ishlaydi)
        st.last_closed_low = pl

        # BUYdan oldingi setup logika candle-close asosida
        if not st.in_position:
            if st.mode == "SEARCH_IMPULSE":
                if impulse_detected(ohlc):
                    st.mode = "WAIT_PULLBACK"
                    st.pullback_high = None

            elif st.mode == "WAIT_PULLBACK":
                # pullback: close pasaydi (tushib keluvchi sham)
                if cc < pc:
                    st.mode = "WAIT_BREAK"
                    # oxirgi yopilgan pullback candle HIGH
                    st.pullback_high = ch

            elif st.mode == "WAIT_BREAK":
                # pullback davom etsa, oxirgi yopilgan pullback candle high-ni yangilab boramiz
                if cc < pc:
                    st.pullback_high = max(st.pullback_high or ch, ch)

    # Har doim (tez-tez) price bilan breakout/breakdown tekshiramiz
    price = fetch_last_price(symbol)

    # BUY: narx pullback_high ni kesib o'tsa
    if (not st.in_position) and st.mode == "WAIT_BREAK" and st.pullback_high is not None:
        if price > st.pullback_high and cooldown_ok(st.last_signal_ts_buy):
            tg_send(f"📈 <b>BUY</b> <b>{symbol}</b>")
            st.last_signal_ts_buy = time.time()
            st.in_position = True
            # BUY bo'lgach o'sish bosqichi
            # st.last_closed_low allaqachon prev low bilan turadi

    # SELL: faqat BUYdan keyin, narx oxirgi yopilgan candle low'ini yangilasa
    if st.in_position and st.last_closed_low is not None:
        if price < st.last_closed_low and cooldown_ok(st.last_signal_ts_sell):
            tg_send(f"📉 <b>SELL</b> <b>{symbol}</b>")
            st.last_signal_ts_sell = time.time()

            # SELLdan keyin: hammasini reset, yana jim
            st.active = False
            st.in_position = False
            st.mode = "SEARCH_IMPULSE"
            st.pullback_high = None
            st.last_3m_close_time = None
            st.last_closed_low = None

# =========================
# MAIN
# =========================
def main():
    offset = None

    # Har symbolga state
    states: Dict[str, TradeState] = {}

    # Start msg (xohlamasangiz o'chirib qo'ying)
    tg_send("✅ Scalp bot ishga tushdi. Signal botdan coin kelsa, faqat o‘sha coinda 3m BUY/SELL beradi.")

    last_tg_poll = 0.0

    while True:
        now = time.time()

        # 1) Telegramdan signal bot xabarlarini o'qish
        if now - last_tg_poll >= TG_POLL_SECONDS:
            last_tg_poll = now
            try:
                data = tg_get_updates(offset)
                if data.get("ok"):
                    for upd in data.get("result", []):
                        offset = upd["update_id"] + 1
                        msg = upd.get("message", {})
                        chat = msg.get("chat", {})
                        chat_id = str(chat.get("id", ""))

                        # faqat belgilangan chat_id
                        if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                            continue

                        text = msg.get("text", "") or ""
                        sym = extract_symbol(text)
                        if sym:
                            st = states.get(sym)
                            if st is None:
                                st = TradeState()
                                states[sym] = st

                            # Signal keldi -> shu coinda 3m set qidirish boshlanadi
                            st.active = True
                            st.activated_at = time.time()
                            st.mode = "SEARCH_IMPULSE"
                            st.pullback_high = None
                            st.last_3m_close_time = None
                            st.in_position = False
                            st.last_closed_low = None
            except Exception as e:
                print("[TG ERROR]", e)

        # 2) Aktiv bo'lgan coinlarda 3m strategiya
        for sym, st in list(states.items()):
            if st.active:
                try:
                    step_3m(sym, st)
                except Exception as e:
                    print("[STRATEGY ERROR]", sym, e)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
