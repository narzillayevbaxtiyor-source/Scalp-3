import os
import re
import json
import time
import math
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import Conflict

# ===================== ENV =====================
BINANCE_BASE = "https://api.binance.com"

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID = int((os.getenv("CHAT_ID") or "0").strip() or "0")

GROUP_ID_RAW = (os.getenv("GROUP_ID") or "").strip()  # HukmCrypto group (-100...)
GROUP_ID = int(GROUP_ID_RAW) if GROUP_ID_RAW else None

HUKM_BOT_USERNAME = (os.getenv("HUKM_BOT_USERNAME") or "HukmCrypto_bot").strip().lstrip("@")
USE_HUKM = (os.getenv("USE_HUKM") or "true").strip().lower() in ("1", "true", "yes", "on")

# ===================== SETTINGS =====================
TOP_N = 10
INTERVAL = "3m"
KLINE_LIMIT = 200

SCAN_EVERY_SEC = 30

DOJI_BODY_TO_RANGE = 0.18
MIN_BULLS_BEFORE_REVERSAL = 2
MIN_REVERSAL = 1
LEVEL_EPS = 0.0

STATE_FILE = "state.json"

# Hukm
HUKM_CACHE_TTL_SEC = 24 * 3600
HUKM_RATE_LIMIT_SEC = 2.0
HUKM_TIMEOUT_SEC = 90


# ===================== DATA =====================
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


@dataclass
class TradeState:
    in_trade: bool = False
    entry: float = 0.0
    sl: float = 0.0
    last_bull_low: float = 0.0


@dataclass
class SignalMemory:
    last_close_time: int = 0
    last_level: float = 0.0


@dataclass
class HukmVerdict:
    status: str = "unknown"  # allowed | blocked | unknown
    updated_at: int = 0


# ===================== BINANCE =====================
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


def top_gainers_usdt(tickers: List[dict], n: int) -> List[str]:
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


def base_asset(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym


def fmt(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


# ===================== STATE =====================
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


# ===================== STRATEGY =====================
def breakout(level: float, c: Candle) -> bool:
    return c.high > (level + LEVEL_EPS)


def find_reversal(candles: List[Candle]) -> Optional[dict]:
    if len(candles) < 20:
        return None

    for end in range(len(candles) - 2, 5, -1):
        j = end
        rev_idx = []
        while j >= 1 and (candles[j].is_bear() or candles[j].is_doji()):
            rev_idx.append(j)
            j -= 1
        if len(rev_idx) < MIN_REVERSAL:
            continue

        bull = 0
        k = rev_idx[-1] - 1
        while k >= 0 and candles[k].is_bull():
            bull += 1
            k -= 1
        if bull < MIN_BULLS_BEFORE_REVERSAL:
            continue

        last_rev = candles[end]
        if last_rev.is_bear():
            return {"level": last_rev.high, "idx": end, "info": f"{bull} bull -> rev ends BEAR"}
        if last_rev.is_doji():
            return {"level": last_rev.high, "idx": end, "info": f"{bull} bull -> rev ends DOJI {last_rev.color()}"}
    return None


# ===================== SCREENSHOT =====================
def tv_url(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}&interval=3"


def microlink_screenshot_bytes(url: str) -> Optional[bytes]:
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


async def send_signal(app: Application, symbol: str, text: str):
    if CHAT_ID == 0:
        return
    link = tv_url(symbol)
    img = microlink_screenshot_bytes(link)
    if img:
        await app.bot.send_photo(chat_id=CHAT_ID, photo=img, caption=text)
    else:
        await app.bot.send_message(chat_id=CHAT_ID, text=text + f"\n\n📈 Chart: {link}")


# ===================== HUKM =====================
def hukm_cache_valid(v: HukmVerdict) -> bool:
    if v.status not in ("allowed", "blocked"):
        return False
    return (int(time.time()) - int(v.updated_at)) <= HUKM_CACHE_TTL_SEC


def is_muboh(txt: str) -> bool:
    t = (txt or "").upper()
    return ("MUBOH" in t) or ("MUBAH" in t) or ("HALOL" in t) or ("HALAL" in t) or ("مباح" in txt) or ("حلال" in txt)


def extract_token(txt: str) -> Optional[str]:
    if not txt:
        return None
    m = re.findall(r"\b[A-Z0-9]{2,10}\b", txt.upper())
    return m[0] if m else None


async def request_hukm(app: Application, token: str):
    if GROUP_ID is None:
        return
    now = time.time()
    last = app.bot_data.get("hukm_last_sent_at", 0.0)
    if (now - last) < HUKM_RATE_LIMIT_SEC:
        return

    msg = await app.bot.send_message(chat_id=GROUP_ID, text=token)
    app.bot_data["hukm_pending_by_msg"][msg.message_id] = token
    app.bot_data["hukm_pending_by_sym"][token] = int(time.time())
    app.bot_data["hukm_last_sent_at"] = now


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or GROUP_ID is None:
        return
    if msg.chat_id != GROUP_ID:
        return
    if not msg.from_user or (msg.from_user.username or "").lower() != HUKM_BOT_USERNAME.lower():
        return

    app = context.application
    hukm: Dict[str, HukmVerdict] = app.bot_data["hukm_cache"]
    pending_msg: Dict[int, str] = app.bot_data["hukm_pending_by_msg"]
    pending_sym: Dict[str, int] = app.bot_data["hukm_pending_by_sym"]

    token = None
    if msg.reply_to_message and msg.reply_to_message.message_id in pending_msg:
        token = pending_msg[msg.reply_to_message.message_id]
    if not token:
        guess = extract_token(msg.text or "")
        if guess and guess in pending_sym:
            token = guess
    if not token:
        return

    verdict = "allowed" if is_muboh(msg.text or "") else "blocked"
    hukm[token] = HukmVerdict(status=verdict, updated_at=int(time.time()))

    if msg.reply_to_message and msg.reply_to_message.message_id in pending_msg:
        pending_msg.pop(msg.reply_to_message.message_id, None)
    pending_sym.pop(token, None)

    save_state(app.bot_data["trades"], app.bot_data["mem"], hukm)


# ===================== COMMANDS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot ishlayapti. /status")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades: Dict[str, TradeState] = context.application.bot_data["trades"]
    lines = []
    for s, st in trades.items():
        if st.in_trade:
            lines.append(f"{s}: entry={fmt(st.entry)} SL={fmt(st.sl)} last_bull_low={fmt(st.last_bull_low)}")
    await update.message.reply_text("\n".join(lines) if lines else "Faol trade yo‘q.")


# ===================== SCAN LOOP =====================
async def scan_once(app: Application):
    client: BinanceClient = app.bot_data["binance"]
    trades: Dict[str, TradeState] = app.bot_data["trades"]
    mem: Dict[str, SignalMemory] = app.bot_data["mem"]
    hukm: Dict[str, HukmVerdict] = app.bot_data["hukm_cache"]
    pending_sym: Dict[str, int] = app.bot_data["hukm_pending_by_sym"]

    try:
        tickers = client.get_24h_tickers()
        symbols = top_gainers_usdt(tickers, TOP_N)
    except Exception:
        return

    allowed = []
    now = int(time.time())

    for sym in symbols:
        token = base_asset(sym)

        if USE_HUKM and GROUP_ID is not None:
            v = hukm.get(token, HukmVerdict())
            if hukm_cache_valid(v) and v.status == "allowed":
                allowed.append(sym)
                continue
            if hukm_cache_valid(v) and v.status == "blocked":
                continue

            last_req = pending_sym.get(token, 0)
            if last_req and (now - last_req) < HUKM_TIMEOUT_SEC:
                continue

            await request_hukm(app, token)
        else:
            allowed.append(sym)

    for sym in allowed:
        try:
            candles = parse_klines(client.get_klines(sym, INTERVAL, KLINE_LIMIT))
        except Exception:
            continue
        if len(candles) < 20:
            continue

        last = candles[-1]
        prev = candles[-2]

        trades.setdefault(sym, TradeState())
        mem.setdefault(sym, SignalMemory())
        st = trades[sym]
        m = mem[sym]

        # Manage trade
        if st.in_trade:
            if last.low <= st.sl:
                await send_signal(app, sym, f"🛑 STOP LOSS\n{sym} SL={fmt(st.sl)}")
                st.in_trade = False
                save_state(trades, mem, hukm)
                continue

            if last.is_bull():
                if st.last_bull_low == 0.0:
                    st.last_bull_low = last.low
                else:
                    if last.low < st.last_bull_low:
                        await send_signal(app, sym, f"✅ TAKE PROFIT\n{sym}\nPrev bull low={fmt(st.last_bull_low)} -> New={fmt(last.low)}")
                        st.in_trade = False
                    else:
                        st.last_bull_low = last.low

            save_state(trades, mem, hukm)
            continue

        # Find setup
        setup = find_reversal(candles)
        if not setup:
            continue

        level = float(setup["level"])
        idx = int(setup["idx"])
        trigger = candles[idx + 1]

        if trigger.close_time not in (last.close_time, prev.close_time):
            continue

        if breakout(level, trigger):
            if m.last_close_time == trigger.close_time and math.isclose(m.last_level, level, rel_tol=1e-12):
                continue

            entry = level
            sl = trigger.low
            tag = " (MUBOH)" if (USE_HUKM and GROUP_ID is not None) else ""
            text = (
                f"🟢 BUY SIGNAL{tag}\n"
                f"{sym} ({INTERVAL})\n"
                f"Setup: {setup['info']}\n"
                f"BUY={fmt(entry)} | SL={fmt(sl)}\n"
                f"TP: bullish low pasaysa"
            )
            await send_signal(app, sym, text)

            st.in_trade = True
            st.entry = entry
            st.sl = sl
            st.last_bull_low = 0.0

            m.last_close_time = trigger.close_time
            m.last_level = level

            save_state(trades, mem, hukm)


async def scanner_loop(app: Application):
    print("✅ scanner_loop started")
    while True:
        try:
            await scan_once(app)
        except Exception as e:
            print(f"[scan] {e}")
        await asyncio.sleep(SCAN_EVERY_SEC)


# ===================== BOOT =====================
async def post_init(app: Application):
    # 1) webhookni tozalash (polling uchun)
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ delete_webhook ok")
    except Exception as e:
        print(f"delete_webhook error: {e}")

    # 2) background scan
    if app.bot_data.get("scanner_task") is None:
        app.bot_data["scanner_task"] = asyncio.create_task(scanner_loop(app))
        print("✅ background task created")


def build_app() -> Application:
    trades, mem, hukm = load_state()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.bot_data["binance"] = BinanceClient()
    app.bot_data["trades"] = trades
    app.bot_data["mem"] = mem
    app.bot_data["hukm_cache"] = hukm
    app.bot_data["hukm_pending_by_msg"] = {}
    app.bot_data["hukm_pending_by_sym"] = {}
    app.bot_data["hukm_last_sent_at"] = 0.0
    app.bot_data["scanner_task"] = None

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_group_message))
    return app


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN yo‘q")
    if CHAT_ID == 0:
        raise SystemExit("CHAT_ID yo‘q yoki noto‘g‘ri")

    if USE_HUKM and GROUP_ID is None:
        print("⚠️ USE_HUKM=true, lekin GROUP_ID yo‘q. Hukm filter ishlamaydi (hamma coin analiz).")

    # Conflict bo‘lsa ham bot yiqilib ketmasin — qayta urinadi
    while True:
        try:
            app = build_app()
            print("✅ run_polling starting...")
            app.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
        except Conflict:
            print("❌ CONFLICT: token bilan boshqa joyda bot ishlayapti. 30s kutaman...")
            time.sleep(30)
        except Exception as e:
            print(f"❌ FATAL: {e}. 10s dan keyin qayta start...")
            time.sleep(10)


if __name__ == "__main__":
    main()
