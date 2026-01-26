import os
import time
import json
import math
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict

BINANCE_BASE = "https://api.binance.com"
STATE_FILE = "state.json"

# ====== ENV ======
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = int((os.getenv("CHAT_ID") or "0").strip() or "0")  # user yoki group id (-100...)

# ====== SETTINGS ======
TOP_N = int(os.getenv("TOP_N", "10"))
INTERVAL = os.getenv("INTERVAL", "3m")          # 3m
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "200"))
SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "30"))

# doji: body/range <= 0.18
DOJI_BODY_TO_RANGE = float(os.getenv("DOJI_BODY_TO_RANGE", "0.18"))

MIN_BULLS_BEFORE_REVERSAL = int(os.getenv("MIN_BULLS_BEFORE_REVERSAL", "2"))
MIN_REVERSAL = int(os.getenv("MIN_REVERSAL", "1"))

# Bot tirikligini ko‘rsatish uchun (xohlasangiz 0 qiling)
HEARTBEAT_MIN = int(os.getenv("HEARTBEAT_MIN", "10"))

# ====== UTIL ======
def fmt(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def tv_url(symbol: str) -> str:
    # TradingView 3m
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}&interval=3"


def microlink_screenshot_bytes(url: str) -> Optional[bytes]:
    """Screenshot uchun Microlink (kalitsiz ishlaydi, ba'zida limit bo'lishi mumkin)."""
    try:
        r = requests.get(
            "https://api.microlink.io",
            params={
                "url": url,
                "screenshot": "true",
                "meta": "false",
                "embed": "screenshot.url",
                "viewport.width": "1280",
                "viewport.height": "720",
            },
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        shot = data.get("data", {}).get("screenshot", {}).get("url")
        if not shot:
            return None
        img = requests.get(shot, timeout=25)
        img.raise_for_status()
        return img.content
    except Exception:
        return None


# ====== BINANCE ======
class BinanceClient:
    def __init__(self):
        self.s = requests.Session()

    def get_24h_tickers(self) -> List[dict]:
        r = self.s.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
        r.raise_for_status()
        return r.json()

    def get_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        r = self.s.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()


def top_gainers_usdt(tickers: List[dict], n: int) -> List[str]:
    # Leveraged tokenlarni tashlab ketamiz
    bad = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    arr = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym.endswith(bad):
            continue
        try:
            chg = float(t.get("priceChangePercent", 0.0))
        except Exception:
            continue
        arr.append((sym, chg))
    arr.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in arr[:n]]


# ====== CANDLE ======
@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int

    @property
    def rng(self) -> float:
        return max(self.high - self.low, 1e-12)

    def is_bull(self) -> bool:
        return self.close > self.open

    def is_bear(self) -> bool:
        return self.close < self.open

    def is_doji(self) -> bool:
        return abs(self.close - self.open) / self.rng <= DOJI_BODY_TO_RANGE

    def color(self) -> str:
        return "green" if self.close >= self.open else "red"


def parse_klines(raw: List[list]) -> List[Candle]:
    out = []
    for k in raw:
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


# ====== STATE ======
@dataclass
class TradeState:
    in_trade: bool = False
    entry: float = 0.0
    sl: float = 0.0
    last_bull_low: float = 0.0
    symbol: str = ""
    since: int = 0  # signal time (ms)


@dataclass
class SignalMemory:
    last_setup_time: int = 0  # setup close_time
    last_level: float = 0.0


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
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ====== STRATEGY ======
def find_reversal_setup(closed: List[Candle]) -> Optional[dict]:
    """
    closed: faqat yopilgan shamlar (forming candle kiritilmaydi)
    Shart:
      - oldin MIN_BULLS_BEFORE_REVERSAL ta bull ketma-ket
      - keyin MIN_REVERSAL ta reversal (bear yoki doji)
      - setup level = reversal tugagan oxirgi sham high
      - Agar oxirgisi bear bo‘lsa: 'bear_end'
      - Agar oxirgisi doji bo‘lsa: 'doji_end'
    """
    if len(closed) < 30:
        return None

    end = len(closed) - 1
    j = end

    rev_idx = []
    while j >= 1 and (closed[j].is_bear() or closed[j].is_doji()):
        rev_idx.append(j)
        j -= 1

    if len(rev_idx) < MIN_REVERSAL:
        return None

    # undan oldin bull ketma-ket
    bull = 0
    k = rev_idx[-1] - 1
    while k >= 0 and closed[k].is_bull():
        bull += 1
        k -= 1

    if bull < MIN_BULLS_BEFORE_REVERSAL:
        return None

    last_rev = closed[end]
    kind = "bear_end" if last_rev.is_bear() else "doji_end"
    level = last_rev.high
    return {
        "kind": kind,
        "level": float(level),
        "setup_time": int(last_rev.close_time),
        "info": f"{bull} bull -> reversal ({len(rev_idx)}) ends {kind.upper()}",
    }


async def send_msg(app: Application, text: str, symbol: Optional[str] = None):
    if CHAT_ID == 0:
        return
    if symbol:
        link = tv_url(symbol)
        img = microlink_screenshot_bytes(link)
        if img:
            await app.bot.send_photo(chat_id=CHAT_ID, photo=img, caption=text)
            return
        await app.bot.send_message(chat_id=CHAT_ID, text=text + f"\n\n📈 Chart: {link}")
        return
    await app.bot.send_message(chat_id=CHAT_ID, text=text)


# ====== BOT COMMANDS ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot ishlayapti.\n/id - chat id\n/ping - test\n/status - trade holati")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Bot tirik.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades: Dict[str, TradeState] = context.application.bot_data["trades"]
    active = [st for st in trades.values() if st.in_trade]
    if not active:
        await update.message.reply_text("Faol trade yo‘q.")
        return
    lines = []
    for st in active:
        lines.append(f"{st.symbol}: entry={fmt(st.entry)} SL={fmt(st.sl)} lastBullLow={fmt(st.last_bull_low)}")
    await update.message.reply_text("\n".join(lines))


# ====== SCANNER ======
async def scan_once(app: Application):
    client: BinanceClient = app.bot_data["binance"]
    trades: Dict[str, TradeState] = app.bot_data["trades"]
    mem: Dict[str, SignalMemory] = app.bot_data["mem"]

    # 1) top gainers
    try:
        tickers = client.get_24h_tickers()
        symbols = top_gainers_usdt(tickers, TOP_N)
    except Exception as e:
        print(f"[tickers] {e}")
        return

    print(f"[scan] symbols={symbols}")

    # 2) per symbol analyze
    for sym in symbols:
        try:
            candles = parse_klines(client.get_klines(sym, INTERVAL, KLINE_LIMIT))
        except Exception as e:
            print(f"[klines] {sym} {e}")
            continue
        if len(candles) < 50:
            continue

        # forming candle = last
        forming = candles[-1]
        # closed candles = up to -2
        closed = candles[:-1]
        last_closed = closed[-1]

        trades.setdefault(sym, TradeState(symbol=sym))
        mem.setdefault(sym, SignalMemory())
        st = trades[sym]
        m = mem[sym]

        # --- manage open trade ---
        if st.in_trade:
            # SL touch (forming low)
            if forming.low <= st.sl:
                await send_msg(app, f"🛑 STOP LOSS\n{sym}\nSL={fmt(st.sl)}", sym)
                st.in_trade = False
                save_state(trades, mem)
                continue

            # TP rule: after buy, consecutive green candles; if new green candle LOW goes below previous green LOW => TP
            if last_closed.is_bull():
                if st.last_bull_low == 0.0:
                    st.last_bull_low = last_closed.low
                else:
                    if last_closed.low < st.last_bull_low:
                        await send_msg(
                            app,
                            f"✅ TAKE PROFIT\n{sym}\nOld bull low={fmt(st.last_bull_low)} -> New={fmt(last_closed.low)}",
                            sym,
                        )
                        st.in_trade = False
                        st.last_bull_low = 0.0
                    else:
                        st.last_bull_low = last_closed.low

            save_state(trades, mem)
            continue

        # --- find setup (closed candles) ---
        setup = find_reversal_setup(closed)
        if not setup:
            continue

        level = float(setup["level"])
        setup_time = int(setup["setup_time"])

        # signal duplication guard
        if m.last_setup_time == setup_time and math.isclose(m.last_level, level, rel_tol=1e-12):
            continue

        # BUY trigger:
        # 1) reversal ended with bear, then when price breaks last bear HIGH (forming.high > level) => signal
        # 2) reversal ended with doji (red/green), next candle breaks doji high (forming.high > level) => signal
        if forming.high > level:
            entry = level
            sl = forming.low  # stop-loss = min of candle that broke the max (forming)
            text = (
                f"🟢 BUY SIGNAL\n"
                f"{sym} ({INTERVAL})\n"
                f"{setup['info']}\n"
                f"BUY={fmt(entry)} | SL={fmt(sl)}\n"
                f"TP: next bullish lows pasaysa"
            )
            await send_msg(app, text, sym)

            st.in_trade = True
            st.entry = entry
            st.sl = sl
            st.last_bull_low = 0.0
            st.since = int(time.time() * 1000)

            m.last_setup_time = setup_time
            m.last_level = level

            save_state(trades, mem)


async def scanner_loop(app: Application):
    print("✅ scanner_loop started")
    while True:
        try:
            await scan_once(app)
        except Exception as e:
            print(f"[scan_loop] {e}")
        await asyncio.sleep(SCAN_EVERY_SEC)


async def heartbeat_loop(app: Application):
    if HEARTBEAT_MIN <= 0:
        return
    while True:
        try:
            await send_msg(app, "✅ Alive: bot ishlayapti (heartbeat).")
        except Exception as e:
            print(f"[heartbeat] {e}")
        await asyncio.sleep(HEARTBEAT_MIN * 60)


async def post_init(app: Application):
    # webhook izlarini tozalash (polling uchun)
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ delete_webhook ok")
    except Exception as e:
        print(f"delete_webhook error: {e}")

    # background tasklar
    if app.bot_data.get("scanner_task") is None:
        app.bot_data["scanner_task"] = asyncio.create_task(scanner_loop(app))
    if app.bot_data.get("heartbeat_task") is None:
        app.bot_data["heartbeat_task"] = asyncio.create_task(heartbeat_loop(app))
    print("✅ background tasks created")


def build_app() -> Application:
    trades, mem = load_state()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.bot_data["binance"] = BinanceClient()
    app.bot_data["trades"] = trades
    app.bot_data["mem"] = mem
    app.bot_data["scanner_task"] = None
    app.bot_data["heartbeat_task"] = None

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    return app


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN yo‘q")
    if CHAT_ID == 0:
        raise SystemExit("CHAT_ID yo‘q yoki noto‘g‘ri")

    while True:
        try:
            app = build_app()
            print("✅ run_polling starting...")
            app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        except Conflict:
            # token boshqa joyda ham ishlayotgan bo‘lsa shunday bo‘ladi
            print("❌ CONFLICT: token bilan boshqa joyda bot ishlayapti. 30s kutaman...")
            time.sleep(30)
        except Exception as e:
            print(f"❌ FATAL: {e}. 10s dan keyin qayta start...")
            time.sleep(10)


if __name__ == "__main__":
    main()
