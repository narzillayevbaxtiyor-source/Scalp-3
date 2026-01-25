import os
import json
import math
import time
import re
import asyncio
import requests
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===================== ENV =====================
BINANCE_BASE = "https://api.binance.com"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID_RAW = os.getenv("CHAT_ID", "").strip()          # signal boradigan joy (user id yoki group id)
GROUP_ID_RAW = os.getenv("GROUP_ID", "").strip()        # HukmCrypto turgan group id (-100...)
HUKM_BOT_USERNAME = os.getenv("HUKM_BOT_USERNAME", "HukmCrypto_bot").strip().lstrip("@")

# Optional: hukm filter OFF qilish uchun USE_HUKM=false
USE_HUKM = os.getenv("USE_HUKM", "true").strip().lower() in ("1", "true", "yes", "on")


def parse_chat_id(v: str) -> Optional[int]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(v)
    except Exception:
        return None


CHAT_ID = parse_chat_id(CHAT_ID_RAW)
GROUP_ID = parse_chat_id(GROUP_ID_RAW)

# ===================== STRATEGY =====================
TOP_N = 10
INTERVAL = "3m"
KLINE_LIMIT = 200

MIN_BULLS_BEFORE_REVERSAL = 2
MIN_REVERSAL_BEAR_OR_DOJI = 1

DOJI_BODY_TO_RANGE = 0.18
LEVEL_EPS = 0.0

SCAN_EVERY_SEC = 30  # har 30 sekund tekshiradi (3m uchun normal)

STATE_FILE = "state.json"

# ===================== HUKM SETTINGS =====================
HUKM_CACHE_TTL_SEC = 24 * 3600   # 24 soat
HUKM_RATE_LIMIT_SEC = 2.0        # 2s da 1 tadan ko‘p so‘rov yubormaslik
HUKM_TIMEOUT_SEC = 90            # javob kelmasa 90s dan keyin qayta so‘raydi


# ===================== DATA STRUCTURES =====================
@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int

    @property
    def range(self) -> float:
        return max(self.high - self.low, 1e-12)

    def is_bull(self) -> bool:
        return self.close > self.open

    def is_bear(self) -> bool:
        return self.close < self.open

    def is_doji(self) -> bool:
        return abs(self.close - self.open) / self.range <= DOJI_BODY_TO_RANGE

    def color(self) -> str:
        return "green" if self.close >= self.open else "red"


@dataclass
class TradeState:
    in_trade: bool = False
    entry_level: float = 0.0
    entry_candle_close_time: int = 0
    sl: float = 0.0
    last_bull_low: float = 0.0


@dataclass
class SignalMemory:
    last_signal_close_time: int = 0
    last_level: float = 0.0


@dataclass
class HukmVerdict:
    status: str = "unknown"  # unknown | allowed | blocked
    updated_at: int = 0


# ===================== BINANCE CLIENT =====================
class BinanceClient:
    def __init__(self):
        self.s = requests.Session()

    def get_24h_tickers(self) -> List[dict]:
        r = self.s.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
        r.raise_for_status()
        return r.json()

    def get_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = self.s.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=20)
        r.raise_for_status()
        return r.json()


# ===================== STATE I/O =====================
def load_state() -> Tuple[Dict[str, TradeState], Dict[str, SignalMemory], Dict[str, HukmVerdict]]:
    if not os.path.exists(STATE_FILE):
        return {}, {}, {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        trades = {k: TradeState(**v) for k, v in raw.get("trades", {}).items()}
        mem = {k: SignalMemory(**v) for k, v in raw.get("mem", {}).items()}
        hukm = {k: HukmVerdict(**v) for k, v in raw.get("hukm", {}).items()}
        return trades, mem, hukm
    except Exception:
        return {}, {}, {}


def save_state(trades: Dict[str, TradeState], mem: Dict[str, SignalMemory], hukm: Dict[str, HukmVerdict]) -> None:
    raw = {
        "trades": {k: asdict(v) for k, v in trades.items()},
        "mem": {k: asdict(v) for k, v in mem.items()},
        "hukm": {k: asdict(v) for k, v in hukm.items()},
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ===================== HELPERS =====================
def parse_klines(kl: List[list]) -> List[Candle]:
    out = []
    for k in kl:
        out.append(
            Candle(
                open_time=int(k[0]),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                close_time=int(k[6]),
            )
        )
    return out


def top_gainers_usdt(tickers: List[dict], n: int) -> List[str]:
    bad_suffixes = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    filtered = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym.endswith(bad_suffixes):
            continue
        try:
            chg = float(t.get("priceChangePercent", 0.0))
        except Exception:
            continue
        filtered.append((sym, chg))
    filtered.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in filtered[:n]]


def base_asset(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym


def breakout_happened(level: float, candle: Candle) -> bool:
    return candle.high > (level + LEVEL_EPS)


def fmt(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def find_reversal_block(candles: List[Candle]) -> Optional[dict]:
    """
    1) Buqa shamlar ketma-ket (>=2)
    2) Qaytuvchi reversal (bear yoki doji) ketma-ket (>=1)
    Signal:
    - Agar reversal oxiri BEAR bo‘lsa: o‘sha bear HIGH yorilsa BUY
    - Agar reversal oxiri DOJI bo‘lsa: keyingi sham doji HIGH yorilsa BUY
    """
    if len(candles) < 20:
        return None

    for end in range(len(candles) - 2, 5, -1):
        j = end
        reversal_idxs = []
        while j >= 1 and (candles[j].is_bear() or candles[j].is_doji()):
            reversal_idxs.append(j)
            j -= 1

        if len(reversal_idxs) < MIN_REVERSAL_BEAR_OR_DOJI:
            continue

        bull_count = 0
        k = reversal_idxs[-1] - 1
        while k >= 0 and candles[k].is_bull():
            bull_count += 1
            k -= 1

        if bull_count < MIN_BULLS_BEFORE_REVERSAL:
            continue

        last_rev = candles[end]
        if last_rev.is_bear():
            return {
                "level_type": "last_bear_high",
                "level": last_rev.high,
                "reversal_last_index": end,
                "pivot_info": f"{bull_count} bull -> {len(reversal_idxs)} rev (ends with BEAR)",
            }

        if last_rev.is_doji():
            return {
                "level_type": "doji_high",
                "level": last_rev.high,
                "reversal_last_index": end,
                "pivot_info": f"{bull_count} bull -> {len(reversal_idxs)} rev (ends with DOJI {last_rev.color()})",
            }

    return None


# ===================== SCREENSHOT =====================
def tradingview_chart_url(symbol: str, interval: str = "3") -> str:
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}&interval={interval}"


def fetch_screenshot_bytes(url: str) -> Optional[bytes]:
    """
    Microlink screenshot endpoint (browser kerak emas).
    Fail bo‘lsa None qaytaradi (link yuboramiz).
    """
    try:
        api = "https://api.microlink.io"
        params = {
            "url": url,
            "screenshot": "true",
            "meta": "false",
            "embed": "screenshot.url",
            "viewport.width": "1280",
            "viewport.height": "720",
        }
        r = requests.get(api, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
        shot_url = data.get("data", {}).get("screenshot", {}).get("url")
        if not shot_url:
            return None
        img = requests.get(shot_url, timeout=25)
        img.raise_for_status()
        return img.content
    except Exception:
        return None


async def send_signal_with_chart(app: Application, sym: str, text: str):
    if CHAT_ID is None:
        return
    chart_link = tradingview_chart_url(sym, "3")
    img_bytes = fetch_screenshot_bytes(chart_link)

    if img_bytes:
        await app.bot.send_photo(chat_id=CHAT_ID, photo=img_bytes, caption=text)
    else:
        await app.bot.send_message(chat_id=CHAT_ID, text=text + f"\n\n📈 Chart: {chart_link}")


# ===================== HUKM FILTER =====================
def hukm_cache_valid(v: HukmVerdict) -> bool:
    if v.status not in ("allowed", "blocked"):
        return False
    return (int(time.time()) - int(v.updated_at)) <= HUKM_CACHE_TTL_SEC


def is_muboh_text(txt: str) -> bool:
    t = (txt or "").upper()
    return ("MUBOH" in t) or ("MUBAH" in t) or ("مباح" in t) or ("HALOL" in t) or ("HALAL" in t) or ("حلال" in t)


def extract_symbol_guess(txt: str) -> Optional[str]:
    if not txt:
        return None
    # BTC, ETH, SOL kabi tokenlarni topish
    m = re.findall(r"\b[A-Z0-9]{2,10}\b", txt.upper())
    if not m:
        return None
    return m[0]


async def request_hukm_for_symbol(app: Application, base: str) -> None:
    """
    GROUP_ID ga coin yuboradi.
    pending map saqlaydi: msg_id -> base
    """
    if GROUP_ID is None:
        return

    now = time.time()
    last_sent = app.bot_data.get("hukm_last_sent_at", 0.0)
    if (now - last_sent) < HUKM_RATE_LIMIT_SEC:
        return

    # oddiy so‘rov: token nomi
    msg = await app.bot.send_message(chat_id=GROUP_ID, text=base)

    pending_by_msg: Dict[int, str] = app.bot_data["hukm_pending_by_msg"]
    pending_by_sym: Dict[str, int] = app.bot_data["hukm_pending_by_sym"]

    pending_by_msg[msg.message_id] = base
    pending_by_sym[base] = int(time.time())

    app.bot_data["hukm_last_sent_at"] = now


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    GROUP_ID ichida @HukmCrypto_bot javobini o‘qiydi.
    'MUBOH' bo‘lsa allowed, bo‘lmasa blocked.
    """
    msg = update.message
    if not msg:
        return

    if GROUP_ID is None or str(msg.chat_id) != str(GROUP_ID):
        return

    if not msg.from_user:
        return

    u = (msg.from_user.username or "").lower()
    if u != HUKM_BOT_USERNAME.lower():
        return

    app = context.application
    hukm_cache: Dict[str, HukmVerdict] = app.bot_data["hukm_cache"]
    pending_by_msg: Dict[int, str] = app.bot_data["hukm_pending_by_msg"]
    pending_by_sym: Dict[str, int] = app.bot_data["hukm_pending_by_sym"]

    base = None

    # eng yaxshi: reply bo‘lsa
    if msg.reply_to_message and msg.reply_to_message.message_id in pending_by_msg:
        base = pending_by_msg[msg.reply_to_message.message_id]

    # fallback: matndan token topish
    if not base:
        guess = extract_symbol_guess(msg.text or "")
        if guess and guess in pending_by_sym:
            base = guess

    if not base:
        return

    verdict = "allowed" if is_muboh_text(msg.text or "") else "blocked"
    hukm_cache[base] = HukmVerdict(status=verdict, updated_at=int(time.time()))

    # pending tozalash
    if msg.reply_to_message and msg.reply_to_message.message_id in pending_by_msg:
        pending_by_msg.pop(msg.reply_to_message.message_id, None)
    pending_by_sym.pop(base, None)

    # persist
    save_state(app.bot_data["trades"], app.bot_data["mem"], hukm_cache)


# ===================== COMMANDS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot ishga tushdi.\n"
        "/status - trade holat\n"
        "/hukmstatus - hukm cache\n"
        "/help - qoida"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ishlash:\n"
        f"- Top{TOP_N} (24h gainers) -> 3m analiz\n"
        "- (Ixtiyoriy) HukmCrypto: javobida MUBOH bo‘lsa analizga kiradi\n"
        "Signal qoidasi:\n"
        "1) Buqa shamlar ketma-ket -> reversal (ayiq/doji)\n"
        "2) Oxirgi ayiq HIGH yorilsa BUY\n"
        "3) Reversal doji bilan tugasa: keyingi sham doji HIGH yorilsa BUY\n"
        "4) SL = breakout sham LOW\n"
        "5) TP = BUYdan keyin bullish shamlarda: yangi bullish LOW < oldingi bullish LOW bo‘lsa TP\n"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades: Dict[str, TradeState] = context.application.bot_data.get("trades", {})
    lines = []
    for sym, st in trades.items():
        if st.in_trade:
            lines.append(f"{sym}: IN_TRADE | entry={fmt(st.entry_level)} | SL={fmt(st.sl)} | last_bull_low={fmt(st.last_bull_low)}")
    await update.message.reply_text("\n".join(lines) if lines else "Faol trade yo‘q.")


async def cmd_hukmstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hukm: Dict[str, HukmVerdict] = context.application.bot_data.get("hukm_cache", {})
    if not hukm:
        await update.message.reply_text("Hukm cache bo‘sh.")
        return
    now = int(time.time())
    items = []
    for k, v in sorted(hukm.items()):
        age = now - int(v.updated_at or 0)
        items.append(f"{k}: {v.status} ({age}s old)")
    await update.message.reply_text("\n".join(items[:80]))


# ===================== CORE SCAN =====================
async def scan_once(app: Application):
    client: BinanceClient = app.bot_data["binance"]
    trades: Dict[str, TradeState] = app.bot_data["trades"]
    mem: Dict[str, SignalMemory] = app.bot_data["mem"]
    hukm_cache: Dict[str, HukmVerdict] = app.bot_data["hukm_cache"]
    pending_by_sym: Dict[str, int] = app.bot_data["hukm_pending_by_sym"]

    # 1) top gainers
    try:
        tickers = client.get_24h_tickers()
        symbols = top_gainers_usdt(tickers, TOP_N)
    except Exception:
        return

    # 2) hukm gating (agar yoqilgan bo‘lsa)
    allowed_symbols: List[str] = []
    now = int(time.time())

    for sym in symbols:
        b = base_asset(sym)

        if USE_HUKM and GROUP_ID is not None:
            v = hukm_cache.get(b, HukmVerdict())

            if hukm_cache_valid(v) and v.status == "allowed":
                allowed_symbols.append(sym)
                continue

            if hukm_cache_valid(v) and v.status == "blocked":
                continue

            # hukm so‘rash
            last_req = pending_by_sym.get(b, 0)
            if last_req and (now - last_req) < HUKM_TIMEOUT_SEC:
                continue

            await request_hukm_for_symbol(app, b)
        else:
            # hukm yo‘q: hammasi allowed
            allowed_symbols.append(sym)

    # 3) only allowed -> analyze
    for sym in allowed_symbols:
        try:
            kl = client.get_klines(sym, INTERVAL, KLINE_LIMIT)
            candles = parse_klines(kl)
        except Exception:
            continue

        if len(candles) < 20:
            continue

        last = candles[-1]
        prev = candles[-2]

        if sym not in trades:
            trades[sym] = TradeState()
        if sym not in mem:
            mem[sym] = SignalMemory()

        st = trades[sym]
        m = mem[sym]

        # ---- manage open trade
        if st.in_trade:
            # SL
            if last.low <= st.sl:
                text = (
                    f"🛑 STOP-LOSS HIT\n"
                    f"{sym} ({INTERVAL})\n"
                    f"SL={fmt(st.sl)} | candle_low={fmt(last.low)}"
                )
                await send_signal_with_chart(app, sym, text)
                st.in_trade = False
                save_state(trades, mem, hukm_cache)
                continue

            # TP: bullish low pasaysa
            if last.is_bull():
                if st.last_bull_low == 0.0:
                    st.last_bull_low = last.low
                else:
                    if last.low < st.last_bull_low:
                        text = (
                            f"✅ TAKE-PROFIT\n"
                            f"{sym} ({INTERVAL})\n"
                            f"Prev bull low={fmt(st.last_bull_low)} -> New bull low={fmt(last.low)}\n"
                            f"Entry={fmt(st.entry_level)} | SL={fmt(st.sl)}"
                        )
                        await send_signal_with_chart(app, sym, text)
                        st.in_trade = False
                    else:
                        st.last_bull_low = last.low

            save_state(trades, mem, hukm_cache)
            continue

        # ---- find pattern
        pattern = find_reversal_block(candles)
        if not pattern:
            continue

        level = float(pattern["level"])
        reversal_last_index = pattern["reversal_last_index"]
        level_type = pattern["level_type"]

        # trigger candle: reversal’dan keyingi sham
        trigger_candle = candles[reversal_last_index + 1]

        # faqat so‘nggi / oldingi yopilgan shamda signal bo‘lsin
        if trigger_candle.close_time not in (last.close_time, prev.close_time):
            continue

        if breakout_happened(level, trigger_candle):
            # duplicate prevention
            if m.last_signal_close_time == trigger_candle.close_time and math.isclose(m.last_level, level, rel_tol=1e-12):
                continue

            entry_level = level
            sl = trigger_candle.low

            tag = " (MUBOH)" if (USE_HUKM and GROUP_ID is not None) else ""
            text = (
                f"🟢 BUY SIGNAL{tag}\n"
                f"{sym} ({INTERVAL})\n"
                f"Setup: {pattern['pivot_info']}\n"
                f"Level ({level_type}) = {fmt(level)}\n"
                f"Break candle: H={fmt(trigger_candle.high)} L={fmt(trigger_candle.low)} C={fmt(trigger_candle.close)}\n"
                f"✅ BUY = {fmt(entry_level)}\n"
                f"🛡 SL = {fmt(sl)}\n"
                f"🎯 TP: bullish candles low pasaysa TP"
            )

            await send_signal_with_chart(app, sym, text)

            # set trade
            st.in_trade = True
            st.entry_level = entry_level
            st.entry_candle_close_time = trigger_candle.close_time
            st.sl = sl
            st.last_bull_low = 0.0

            m.last_signal_close_time = trigger_candle.close_time
            m.last_level = level

            save_state(trades, mem, hukm_cache)


# ===================== BACKGROUND LOOP =====================
async def scanner_loop(app: Application):
    # kichik log (Render logs uchun)
    print("✅ scanner_loop started")
    while True:
        try:
            await scan_once(app)
        except Exception as e:
            # bot yiqilmasin
            print(f"[scanner_loop] error: {e}")
        await asyncio.sleep(SCAN_EVERY_SEC)


async def post_init(app: Application):
    # background task start
    if app.bot_data.get("scanner_task") is None:
        app.bot_data["scanner_task"] = asyncio.create_task(scanner_loop(app))
        print("✅ background task created")


# ===================== MAIN =====================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env yo‘q.")
    if CHAT_ID is None:
        raise SystemExit("CHAT_ID env yo‘q yoki noto‘g‘ri.")
    # GROUP_ID majburiy emas (USE_HUKM true bo‘lsa tavsiya)
    if USE_HUKM and GROUP_ID is None:
        print("⚠️ USE_HUKM=true, lekin GROUP_ID yo‘q. Hukm filter o‘chadi (hammasi analiz).")

    trades, mem, hukm = load_state()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # shared state
    app.bot_data["binance"] = BinanceClient()
    app.bot_data["trades"] = trades
    app.bot_data["mem"] = mem
    app.bot_data["hukm_cache"] = hukm
    app.bot_data["hukm_pending_by_msg"] = {}  # msg_id -> symbol
    app.bot_data["hukm_pending_by_sym"] = {}  # symbol -> last_request_time
    app.bot_data["hukm_last_sent_at"] = 0.0
    app.bot_data["scanner_task"] = None

    # handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("hukmstatus", cmd_hukmstatus))

    # Hukm bot javoblari (group ichida)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_group_message))

    # run
    print("✅ Bot polling starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
