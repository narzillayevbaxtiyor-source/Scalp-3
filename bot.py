import os
import json
import math
import time
import re
import requests
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BINANCE_BASE = "https://api.binance.com"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # e.g. "6248061970"

# --- Strategy knobs ---
TOP_N = 10
INTERVAL = "3m"
KLINE_LIMIT = 200

MIN_BULLS_BEFORE_REVERSAL = 2
MIN_REVERSAL_BEAR_OR_DOJI = 1

DOJI_BODY_TO_RANGE = 0.18
LEVEL_EPS = 0.0

STATE_FILE = "state.json"

# --- Halal filter (Telegram channel) ---
HALAL_CHANNEL = "CrypoIslam"
HALAL_CHANNEL_URL = f"https://t.me/s/{HALAL_CHANNEL}"
HALAL_CACHE_SECONDS = 600  # 10 min cache, Telegramni ko‘p urmaslik uchun
HALAL_MIN_TEXT_CHARS = 40  # agar sahifa bo‘sh qaytsa, fallback qilish oson bo‘lsin


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
    last_bull_low: float = 0.0  # TP logic


@dataclass
class SignalMemory:
    last_signal_close_time: int = 0
    last_level: float = 0.0


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


def load_state() -> Tuple[Dict[str, TradeState], Dict[str, SignalMemory]]:
    if not os.path.exists(STATE_FILE):
        return {}, {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        trades = {k: TradeState(**v) for k, v in raw.get("trades", {}).items()}
        mem = {k: SignalMemory(**v) for k, v in raw.get("mem", {}).items()}
        return trades, mem
    except Exception:
        return {}, {}


def save_state(trades: Dict[str, TradeState], mem: Dict[str, SignalMemory]) -> None:
    raw = {
        "trades": {k: asdict(v) for k, v in trades.items()},
        "mem": {k: asdict(v) for k, v in mem.items()},
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


def breakout_happened(level: float, candle: Candle) -> bool:
    return candle.high > (level + LEVEL_EPS)


def fmt(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def base_asset_from_symbol(sym: str) -> str:
    # Assumption: spot USDT pairs
    if sym.endswith("USDT"):
        return sym[:-4]
    return sym


def find_reversal_block(candles: List[Candle]) -> Optional[dict]:
    """
    Pattern:
    - >= MIN_BULLS_BEFORE_REVERSAL bullish candles
    - then reversal candles (bear or doji)
    Signal rules:
    1) If reversal ends with BEAR: break that bear HIGH => BUY
    2) If reversal ends with DOJI: next candle breaks doji HIGH => BUY
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
    Uses Microlink screenshot endpoint (no browser needed).
    If it fails, return None and we fallback to sending a link.
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


# --- Halal filter (Telegram channel scraping with cache) ---
def load_halal_cache() -> dict:
    # simple in-file cache to survive restarts
    cache_file = "halal_cache.json"
    if not os.path.exists(cache_file):
        return {"ts": 0, "text": ""}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ts": 0, "text": ""}


def save_halal_cache(ts: int, text: str) -> None:
    cache_file = "halal_cache.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "text": text}, f, ensure_ascii=False)


def fetch_channel_text() -> str:
    """
    Fetches recent public posts text from https://t.me/s/<channel>
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; halal-filter-bot/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(HALAL_CHANNEL_URL, headers=headers, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # message texts are typically in div.tgme_widget_message_text
    texts = []
    for div in soup.select("div.tgme_widget_message_text"):
        t = div.get_text(" ", strip=True)
        if t:
            texts.append(t)
    joined = "\n".join(texts).upper()
    return joined


def get_halal_text_cached() -> str:
    c = load_halal_cache()
    now = int(time.time())
    if (now - int(c.get("ts", 0))) < HALAL_CACHE_SECONDS and len(c.get("text", "")) >= HALAL_MIN_TEXT_CHARS:
        return c["text"]

    try:
        text = fetch_channel_text()
        if len(text) >= HALAL_MIN_TEXT_CHARS:
            save_halal_cache(now, text)
            return text
    except Exception:
        pass

    # fallback to old cache if fetch fails
    return c.get("text", "")


def symbol_is_halal(base_symbol: str, halal_text: str) -> bool:
    """
    Very simple rule:
    - if base symbol appears as a whole word in channel text -> halal
    Example: BTC, ETH, SOL ...
    """
    if not halal_text:
        return False
    base_symbol = base_symbol.upper().strip()
    if not base_symbol:
        return False
    # whole-word match (avoid matching inside other words)
    pattern = r"\b" + re.escape(base_symbol) + r"\b"
    return re.search(pattern, halal_text) is not None


def filter_halal_symbols(symbols: List[str]) -> Tuple[List[str], List[str]]:
    """
    Returns (halal_symbols, rejected_symbols)
    """
    halal_text = get_halal_text_cached()
    halal = []
    rejected = []
    for sym in symbols:
        base = base_asset_from_symbol(sym)
        if symbol_is_halal(base, halal_text):
            halal.append(sym)
        else:
            rejected.append(sym)
    return halal, rejected


# --- Telegram commands ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot ishga tushdi.\n"
        "/status - holat\n"
        "/help - qoida\n"
        "/halalcheck - kanal bo‘yicha halal filter test"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Qoidalar:\n"
        "0) Top10 olingach, avval @CrypoIslam kanalida symbol bor-yo‘qligi tekshiriladi (halal filter).\n"
        "1) Binance Top10 (24h % o'sish) -> 3m klines.\n"
        "2) Buqa shamlardan keyin reversal (ayiq/doji):\n"
        "   - oxirgi ayiq sham HIGH yorilsa BUY\n"
        "   - reversal doji bilan tugasa: keyingi sham doji HIGH yorilsa BUY\n"
        "3) SL = breakout bo‘lgan sham LOW.\n"
        "4) TP = BUYdan keyin bullish shamlar davomida: yangi bullish LOW < oldingi bullish LOW bo‘lsa TP.\n"
        "Chart screenshot signal bilan yuboriladi."
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


async def cmd_halalcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # quick manual test from Telegram
    try:
        client: BinanceClient = context.application.bot_data["binance"]
        tickers = client.get_24h_tickers()
        top = top_gainers_usdt(tickers, TOP_N)
        halal, rej = filter_halal_symbols(top)
        await update.message.reply_text(
            f"Top{TOP_N}: {', '.join(top)}\n\n"
            f"✅ Halal (channel match): {', '.join(halal) if halal else 'None'}\n"
            f"❌ Rejected: {', '.join(rej) if rej else 'None'}\n\n"
            f"Manba: {HALAL_CHANNEL_URL}"
        )
    except Exception as e:
        await update.message.reply_text("Halal check xatolik berdi. Keyinroq qayta urinib ko‘ring.")


# --- Main scanning job ---
async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    client: BinanceClient = app.bot_data["binance"]
    trades: Dict[str, TradeState] = app.bot_data["trades"]
    mem: Dict[str, SignalMemory] = app.bot_data["mem"]

    try:
        tickers = client.get_24h_tickers()
        symbols = top_gainers_usdt(tickers, TOP_N)
    except Exception:
        return

    # ✅ HALAL FILTER HERE
    halal_symbols, _rejected = filter_halal_symbols(symbols)
    if not halal_symbols:
        return

    for sym in halal_symbols:
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
                save_state(trades, mem)
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

            save_state(trades, mem)
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

        # accept only very recent triggers
        if trigger_candle.close_time not in (last.close_time, prev.close_time):
            continue

        if breakout_happened(level, trigger_candle):
            if m.last_signal_close_time == trigger_candle.close_time and math.isclose(m.last_level, level, rel_tol=1e-12):
                continue

            entry_level = level
            sl = trigger_candle.low

            text = (
                f"🟢 BUY SIGNAL (HALAL FILTER PASSED)\n"
                f"{sym} ({INTERVAL})\n"
                f"Halal check: @{HALAL_CHANNEL}\n"
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

            save_state(trades, mem)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN va CHAT_ID env ni qo'ying. Masalan: BOT_TOKEN=... CHAT_ID=6248061970")

    trades, mem = load_state()

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["binance"] = BinanceClient()
    app.bot_data["trades"] = trades
    app.bot_data["mem"] = mem

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("halalcheck", cmd_halalcheck))

    app.job_queue.run_repeating(scanner_job, interval=30, first=5)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
