import os
import json
import math
import time
import re
import requests
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BINANCE_BASE = "https://api.binance.com"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()            # sizning shaxsiy chat id yoki group id
GROUP_ID = os.getenv("GROUP_ID", "").strip()          # HukmCrypto bilan birga turgan GROUP chat_id: -100...
HUKM_BOT_USERNAME = os.getenv("HUKM_BOT_USERNAME", "HukmCrypto_bot").strip().lstrip("@")

# --- Strategy knobs ---
TOP_N = 10
INTERVAL = "3m"
KLINE_LIMIT = 200

MIN_BULLS_BEFORE_REVERSAL = 2
MIN_REVERSAL_BEAR_OR_DOJI = 1

DOJI_BODY_TO_RANGE = 0.18
LEVEL_EPS = 0.0

STATE_FILE = "state.json"

# --- Hukm check knobs ---
HUKM_CACHE_TTL_SEC = 24 * 3600      # 24 soat cache
HUKM_RATE_LIMIT_SEC = 2.0           # groupga 2 sekundda 1 tadan ko‘p so‘rov yubormaslik
HUKM_TIMEOUT_SEC = 60               # 60s ichida javob bo‘lmasa, keyingi aylanishda qayta so‘raydi


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
    status: str = "unknown"   # unknown | allowed | blocked
    updated_at: int = 0       # unix seconds


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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


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


# --- TradingView screenshot helpers ---
def tradingview_chart_url(symbol: str, interval: str = "3") -> str:
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}&interval={interval}"


def fetch_screenshot_bytes(url: str) -> Optional[bytes]:
    """
    Microlink screenshot endpoint (browser kerak emas).
    Fail bo‘lsa link yuborishga tushadi.
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


async def send_signal_with_chart(context: ContextTypes.DEFAULT_TYPE, sym: str, text: str):
    chart_link = tradingview_chart_url(sym, "3")
    img_bytes = fetch_screenshot_bytes(chart_link)

    if img_bytes:
        await context.bot.send_photo(chat_id=CHAT_ID, photo=img_bytes, caption=text)
    else:
        await context.bot.send_message(chat_id=CHAT_ID, text=text + f"\n\n📈 Chart: {chart_link}")


# ---------------- HUKM (group-based) ----------------

def hukm_cache_valid(v: HukmVerdict) -> bool:
    if v.status not in ("allowed", "blocked"):
        return False
    return (int(time.time()) - int(v.updated_at)) <= HUKM_CACHE_TTL_SEC


def is_muboh_text(txt: str) -> bool:
    t = (txt or "").upper()
    return "MUBOH" in t or "MUBAH" in t or "مباح" in t or "HALOL" in t or "HALAL" in t


def extract_symbol_guess(txt: str) -> Optional[str]:
    """
    fallback: matndan BTC/ETH kabi token topishga urinish
    """
    if not txt:
        return None
    m = re.findall(r"\b[A-Z0-9]{2,10}\b", txt.upper())
    if not m:
        return None
    # eng birinchi token
    return m[0]


async def request_hukm_for_symbol(context: ContextTypes.DEFAULT_TYPE, base: str) -> None:
    """
    Guruhga so‘rov yuboradi va message_id -> symbol map saqlaydi.
    """
    app = context.application
    now = time.time()
    last_sent = app.bot_data.get("hukm_last_sent_at", 0.0)
    if (now - last_sent) < HUKM_RATE_LIMIT_SEC:
        return

    # oddiy so‘rov: faqat symbol (ko‘p botlar shuni tushunadi)
    msg = await context.bot.send_message(chat_id=GROUP_ID, text=base)

    pending_by_msg: Dict[int, str] = app.bot_data["hukm_pending_by_msg"]
    pending_by_sym: Dict[str, int] = app.bot_data["hukm_pending_by_sym"]
    pending_by_msg[msg.message_id] = base
    pending_by_sym[base] = int(time.time())

    app.bot_data["hukm_last_sent_at"] = now


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Guruhda @HukmCrypto_bot javobini ushlab, MUBOH bo‘lsa coin allowed qiladi.
    """
    if not update.message:
        return

    msg = update.message

    # faqat GROUP_ID ichidagi xabarlar
    if str(msg.chat_id) != str(GROUP_ID):
        return

    # faqat Hukm botdan kelgan xabarlarni o‘qish
    from_user = msg.from_user
    if not from_user:
        return

    # username tekshiramiz (eng ishonchli)
    u = (from_user.username or "").lower()
    if u != HUKM_BOT_USERNAME.lower():
        return

    app = context.application
    hukm: Dict[str, HukmVerdict] = app.bot_data["hukm_cache"]
    pending_by_msg: Dict[int, str] = app.bot_data["hukm_pending_by_msg"]
    pending_by_sym: Dict[str, int] = app.bot_data["hukm_pending_by_sym"]

    base = None

    # eng yaxshi: reply_to_message orqali aniqlash
    if msg.reply_to_message and msg.reply_to_message.message_id in pending_by_msg:
        base = pending_by_msg[msg.reply_to_message.message_id]

    # fallback: matndan symbol topish
    if not base:
        guess = extract_symbol_guess(msg.text or "")
        if guess and guess in pending_by_sym:
            base = guess

    if not base:
        return

    verdict = "allowed" if is_muboh_text(msg.text or "") else "blocked"
    hukm[base] = HukmVerdict(status=verdict, updated_at=int(time.time()))

    # pending’dan tozalash
    # (reply id bo‘yicha bo‘lsa)
    if msg.reply_to_message and msg.reply_to_message.message_id in pending_by_msg:
        pending_by_msg.pop(msg.reply_to_message.message_id, None)
    pending_by_sym.pop(base, None)

    # xohlasangiz diagnostika (chatga yubormaymiz) — logga chiqadi:
    # print(f"HUKM {base} => {verdict}")


# ---------------- Telegram commands ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot ishga tushdi.\n"
        "/status - holat\n"
        "/help - qoida\n"
        "/hukmstatus - hukm cache holati"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ishlash tartibi:\n"
        "0) Top10 chiqadi -> avval GROUP_ID ichida @HukmCrypto_bot dan hukm so‘raydi.\n"
        "   Javobida 'MUBOH' bo‘lsa -> analiz.\n"
        "1) Binance Top10 (24h % o'sish) -> 3m klines.\n"
        "2) Buqa shamlardan keyin reversal (ayiq/doji):\n"
        "   - oxirgi ayiq sham HIGH yorilsa BUY\n"
        "   - reversal doji bilan tugasa: keyingi sham doji HIGH yorilsa BUY\n"
        "3) SL = breakout bo‘lgan sham LOW.\n"
        "4) TP = BUYdan keyin bullish shamlar davomida: yangi bullish LOW < oldingi bullish LOW bo‘lsa TP.\n"
        "Signal bilan chart screenshot yuboriladi."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades: Dict[str, TradeState] = context.application.bot_data.get("trades", {})
    if not trades:
        await update.message.reply_text("Hozircha trade state yo'q.")
        return
    lines = []
    for sym, st in trades.items():
        if st.in_trade:
            lines.append(
                f"{sym}: IN_TRADE | entry={fmt(st.entry_level)} | SL={fmt(st.sl)} | last_bull_low={fmt(st.last_bull_low)}"
            )
    await update.message.reply_text("\n".join(lines) if lines else "Faol trade yo'q.")


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


# ---------------- Main scanning job ----------------

async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    client: BinanceClient = app.bot_data["binance"]
    trades: Dict[str, TradeState] = app.bot_data["trades"]
    mem: Dict[str, SignalMemory] = app.bot_data["mem"]
    hukm_cache: Dict[str, HukmVerdict] = app.bot_data["hukm_cache"]
    pending_by_sym: Dict[str, int] = app.bot_data["hukm_pending_by_sym"]

    try:
        tickers = client.get_24h_tickers()
        symbols = top_gainers_usdt(tickers, TOP_N)
    except Exception:
        return

    # 1) Hukm tekshiruv: faqat MUBOH bo‘lganlar analizga kiradi
    allowed_symbols = []
    now = int(time.time())

    for sym in symbols:
        b = base_asset(sym)

        v = hukm_cache.get(b, HukmVerdict())
        if hukm_cache_valid(v) and v.status == "allowed":
            allowed_symbols.append(sym)
            continue

        # agar blocked bo‘lsa (cache valid) -> skip
        if hukm_cache_valid(v) and v.status == "blocked":
            continue

        # cache yo‘q / eskirgan bo‘lsa -> hukm so‘raymiz
        last_req = pending_by_sym.get(b, 0)
        if last_req and (now - last_req) < HUKM_TIMEOUT_SEC:
            continue  # hali javob kutilyapti

        # so‘rov yuborish
        await request_hukm_for_symbol(context, b)

    # 2) Faqat MUBOH bo‘lganlar analiz
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

        # ---- Manage open trade ----
        if st.in_trade:
            if last.low <= st.sl:
                text = (
                    f"🛑 STOP-LOSS HIT\n"
                    f"{sym} ({INTERVAL})\n"
                    f"SL={fmt(st.sl)} | candle_low={fmt(last.low)}"
                )
                await send_signal_with_chart(context, sym, text)
                st.in_trade = False
                save_state(trades, mem, hukm_cache)
                continue

            # TP rule
            if last.is_bull():
                if st.last_bull_low == 0.0:
                    st.last_bull_low = last.low
                else:
                    if last.low < st.last_bull_low:
                        text = (
                            f"✅ TAKE-PROFIT\n"
                            f"{sym} ({INTERVAL})\n"
                            f"Bullish candle made LOWER LOW.\n"
                            f"Prev bull low={fmt(st.last_bull_low)} -> New bull low={fmt(last.low)}\n"
                            f"Entry={fmt(st.entry_level)} | SL={fmt(st.sl)}"
                        )
                        await send_signal_with_chart(context, sym, text)
                        st.in_trade = False
                    else:
                        st.last_bull_low = last.low

            save_state(trades, mem, hukm_cache)
            continue

        # ---- Find pattern + breakout ----
        pattern = find_reversal_block(candles)
        if not pattern:
            continue

        level = float(pattern["level"])
        reversal_last_index = pattern["reversal_last_index"]
        level_type = pattern["level_type"]

        next_candle = candles[reversal_last_index + 1]
        trigger_candle = next_candle

        if trigger_candle.close_time not in (last.close_time, prev.close_time):
            continue

        if breakout_happened(level, trigger_candle):
            if m.last_signal_close_time == trigger_candle.close_time and math.isclose(m.last_level, level, rel_tol=1e-12):
                continue

            entry_level = level
            sl = trigger_candle.low

            text = (
                f"🟢 BUY SIGNAL (MUBOH)\n"
                f"{sym} ({INTERVAL})\n"
                f"Setup: {pattern['pivot_info']}\n"
                f"Level ({level_type}) = {fmt(level)}\n"
                f"Break candle: H={fmt(trigger_candle.high)} L={fmt(trigger_candle.low)} C={fmt(trigger_candle.close)}\n"
                f"✅ BUY = {fmt(entry_level)}\n"
                f"🛡 SL = {fmt(sl)}\n"
                f"🎯 TP: bullish candles davomida, yangi bullish LOW < oldingi bullish LOW bo‘lsa TP"
            )

            await send_signal_with_chart(context, sym, text)

            st.in_trade = True
            st.entry_level = entry_level
            st.entry_candle_close_time = trigger_candle.close_time
            st.sl = sl
            st.last_bull_low = 0.0

            m.last_signal_close_time = trigger_candle.close_time
            m.last_level = level

            save_state(trades, mem, hukm_cache)


def main():
    if not BOT_TOKEN or not CHAT_ID or not GROUP_ID:
        raise SystemExit(
            "Env vars kerak: BOT_TOKEN, CHAT_ID, GROUP_ID.\n"
            "GROUP_ID - HukmCrypto_bot bilan turgan guruh chat_id (minus bilan)."
        )

    trades, mem, hukm = load_state()

    app = Application.builder().token(BOT_TOKEN).build()

    # shared objects
    app.bot_data["binance"] = BinanceClient()
    app.bot_data["trades"] = trades
    app.bot_data["mem"] = mem

    # hukm state (in-memory, plus persisted)
    app.bot_data["hukm_cache"] = hukm
    app.bot_data["hukm_pending_by_msg"] = {}  # message_id -> symbol
    app.bot_data["hukm_pending_by_sym"] = {}  # symbol -> last_request_time
    app.bot_data["hukm_last_sent_at"] = 0.0

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("hukmstatus", cmd_hukmstatus))

    # group message listener (Hukm bot javoblari uchun)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_group_message))

    # scanner
    app.job_queue.run_repeating(scanner_job, interval=30, first=5)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
