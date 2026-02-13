#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import statistics as stats
from typing import List, Dict, Tuple
import requests

# =========================
# CONFIG (ENV)
# =========================
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()   # user id yoki group id (-100...)
SCAN_EVERY_SEC = int((os.getenv("SCAN_EVERY_SEC") or "180").strip())  # 3 min default

TOP_N = int((os.getenv("TOP_N") or "10").strip())  # top gainers count

# "Tasdiq" shartlari (ozgartirsa bo'ladi)
VOL_RATIO_MIN_1D = float((os.getenv("VOL_RATIO_MIN_1D") or "2.0").strip())   # 1D volume MA20 dan kamida 2x
IMPULSE_ATR_MIN_4H = float((os.getenv("IMPULSE_ATR_MIN_4H") or "1.2").strip()) # 4H body >= 1.2 ATR
REQUIRE_1D_BREAKOUT = (os.getenv("REQUIRE_1D_BREAKOUT", "1").strip() == "1")  # 1D prior-high breakout shartmi?
REQUIRE_4H_BREAKOUT_OR_IMPULSE = (os.getenv("REQUIRE_4H_BREAKOUT_OR_IMPULSE", "1").strip() == "1")

STATE_FILE = (os.getenv("STATE_FILE") or "state.json").strip()

# Binance endpoints fallback
BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

S = requests.Session()
S.headers.update({"User-Agent": "spot-top10-buy-alert/1.0"})


# =========================
# HELPERS
# =========================
def to_f(x) -> float:
    try:
        return float(x)
    except Exception:
        return float("nan")

def pct(prev: float, cur: float) -> float:
    if prev == 0 or math.isnan(prev) or math.isnan(cur):
        return float("nan")
    return (cur - prev) / prev * 100.0

def rolling_mean(xs: List[float], n: int) -> float:
    if n <= 0 or len(xs) < n:
        return float("nan")
    return stats.mean(xs[-n:])

def atr_simple(h: List[float], l: List[float], c: List[float], n: int = 14) -> float:
    if len(c) < n + 1:
        return float("nan")
    trs = []
    for i in range(-n, 0):
        hi = h[i]
        lo = l[i]
        prev_close = c[i - 1]
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
    return stats.mean(trs) if trs else float("nan")

def _get(path: str, params: dict, timeout=12):
    last_err = None
    for base in BASES:
        try:
            r = S.get(base + path, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All Binance endpoints failed: {last_err}")

def exchange_info() -> dict:
    return _get("/api/v3/exchangeInfo", {})

def tickers_24h() -> List[dict]:
    return _get("/api/v3/ticker/24hr", {})

def klines(symbol: str, interval: str, limit: int = 200) -> List[list]:
    return _get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

def parse_klines(raw: List[list]) -> Dict[str, List[float]]:
    o = [to_f(k[1]) for k in raw]
    h = [to_f(k[2]) for k in raw]
    l = [to_f(k[3]) for k in raw]
    c = [to_f(k[4]) for k in raw]
    v = [to_f(k[5]) for k in raw]
    return {"o": o, "h": h, "l": l, "c": c, "v": v}

def trend_hh_hl(h: List[float], l: List[float], lookback: int = 6) -> str:
    # Juda sodda struktur: HH/HL bo'lsa bullish, LH/LL bo'lsa bearish, aks holda range.
    if len(h) < lookback + 1:
        return "data yetarli emas"
    highs = h[-lookback:]
    lows = l[-lookback:]
    hh = all(highs[i] >= highs[i - 1] for i in range(1, len(highs)))
    hl = all(lows[i] >= lows[i - 1] for i in range(1, len(lows)))
    if hh and hl:
        return "HH/HL (bullish)"
    lh = all(highs[i] <= highs[i - 1] for i in range(1, len(highs)))
    ll = all(lows[i] <= lows[i - 1] for i in range(1, len(lows)))
    if lh and ll:
        return "LH/LL (bearish)"
    return "range/mixed"

def analyze_tf(tf_name: str, data: Dict[str, List[float]]) -> Dict[str, float]:
    o, h, l, c, v = data["o"], data["h"], data["l"], data["c"], data["v"]
    last_close = c[-1]
    prev_close = c[-2] if len(c) > 1 else float("nan")
    chg = pct(prev_close, last_close)

    vol_ma20 = rolling_mean(v, 20)
    vol_ratio = (v[-1] / vol_ma20) if (vol_ma20 and not math.isnan(vol_ma20) and vol_ma20 != 0) else float("nan")

    # breakout: close above prior N-bar high (excluding last bar)
    N = 55 if tf_name == "1w" else (30 if tf_name == "1d" else 60)
    if len(h) > (N + 1):
        prior_high = max(h[-(N + 1):-1])
    elif len(h) > 1:
        prior_high = max(h[:-1])
    else:
        prior_high = float("nan")

    breakout = 1.0 if (not math.isnan(prior_high) and last_close > prior_high) else 0.0

    atr = atr_simple(h, l, c, 14)
    impulse_atr = ((c[-1] - o[-1]) / atr) if (atr and not math.isnan(atr) and atr != 0) else float("nan")

    return {
        "tf_change_pct": chg,
        "vol_ratio": vol_ratio,
        "breakout": breakout,
        "prior_high": prior_high,
        "atr": atr,
        "impulse_atr": impulse_atr,
        "last_close": last_close,
        "last_open": o[-1],
    }

def pick_topN_usdt_spot(n: int) -> List[Tuple[str, float]]:
    info = exchange_info()
    spot_symbols = set()
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("isSpotTradingAllowed") is not True:
            continue
        sym = s.get("symbol", "")
        if sym.endswith("USDT"):
            spot_symbols.add(sym)

    t = tickers_24h()
    candidates = []
    for x in t:
        sym = x.get("symbol")
        if sym not in spot_symbols:
            continue
        change = to_f(x.get("priceChangePercent"))
        if math.isnan(change):
            continue
        candidates.append((sym, change))

    candidates.sort(key=lambda z: z[1], reverse=True)
    return candidates[:n]

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}}  # sent[symbol] = timestamp

def save_state(st: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        # Telegram sozlanmagan bo'lsa, konsolga chiqaramiz
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELELEGRAM_CHAT_ID if False else TELEGRAM_CHAT_ID,  # safety no-op
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()

def passes_confirmation(aw: dict, ad: dict, a4: dict, w_tr: str, d_tr: str, h4_tr: str) -> Tuple[bool, List[str]]:
    reasons = []

    # Core: 1D volume
    vr1d = ad.get("vol_ratio", float("nan"))
    if not math.isnan(vr1d) and vr1d >= VOL_RATIO_MIN_1D:
        reasons.append(f"1D volume spike ~{vr1d:.2f}x (MA20)")
    else:
        reasons.append(f"1D volume past (~{vr1d:.2f}x), kerak >= {VOL_RATIO_MIN_1D:.2f}x")
        return (False, reasons)

    # 1D breakout (optional)
    if REQUIRE_1D_BREAKOUT:
        if ad.get("breakout") == 1.0:
            reasons.append("1D breakout: prior-high ustida close")
        else:
            reasons.append("1D breakout yo‘q")
            return (False, reasons)

    # 4H breakout yoki impuls (optional)
    if REQUIRE_4H_BREAKOUT_OR_IMPULSE:
        ok4 = False
        if a4.get("breakout") == 1.0:
            ok4 = True
            reasons.append("4H breakout: prior-high ustida close")
        imp = a4.get("impulse_atr", float("nan"))
        if (not ok4) and (not math.isnan(imp)) and (imp >= IMPULSE_ATR_MIN_4H):
            ok4 = True
            reasons.append(f"4H impuls kuchli: body~{imp:.2f} ATR (>= {IMPULSE_ATR_MIN_4H:.2f})")
        if not ok4:
            reasons.append(f"4H tasdiq yo‘q (breakout/impuls yetarli emas). impulse~{imp:.2f} ATR")
            return (False, reasons)

    # Trend filter: 1D va 4H bearish bo'lmasin
    if "bearish" in d_tr or "bearish" in h4_tr:
        reasons.append(f"Trend filter: 1D={d_tr}, 4H={h4_tr} (bearish) -> skip")
        return (False, reasons)

    reasons.append(f"Trend: 1W={w_tr}; 1D={d_tr}; 4H={h4_tr}")
    return (True, reasons)

def format_buy_message(sym: str, chg24: float, aw: dict, ad: dict, a4: dict, reasons: List[str]) -> str:
    return (
        f"✅ BUY: {sym}\n"
        f"24h gain: {chg24:+.2f}%\n\n"
        f"1W: chg={aw['tf_change_pct']:+.2f}% | vol~{aw['vol_ratio']:.2f}x | breakout={int(aw['breakout'])}\n"
        f"1D: chg={ad['tf_change_pct']:+.2f}% | vol~{ad['vol_ratio']:.2f}x | breakout={int(ad['breakout'])}\n"
        f"4H: chg={a4['tf_change_pct']:+.2f}% | vol~{a4['vol_ratio']:.2f}x | breakout={int(a4['breakout'])} | impulse~{a4['impulse_atr']:.2f} ATR\n\n"
        "Tasdiq sabablari:\n- " + "\n- ".join(reasons)
    )

# =========================
# MAIN LOOP
# =========================
def run_once(state: dict):
    top = pick_topN_usdt_spot(TOP_N)

    for sym, chg24 in top:
        try:
            # oldin yuborgan bo'lsa qayta yubormaymiz
            if sym in state.get("sent", {}):
                continue

            raw_w = klines(sym, "1w", 120)
            raw_d = klines(sym, "1d", 200)
            raw_4h = klines(sym, "4h", 200)

            w = parse_klines(raw_w)
            d = parse_klines(raw_d)
            h4 = parse_klines(raw_4h)

            aw = analyze_tf("1w", w)
            ad = analyze_tf("1d", d)
            a4 = analyze_tf("4h", h4)

            w_tr = trend_hh_hl(w["h"], w["l"], 6)
            d_tr = trend_hh_hl(d["h"], d["l"], 6)
            h4_tr = trend_hh_hl(h4["h"], h4["l"], 8)

            ok, reasons = passes_confirmation(aw, ad, a4, w_tr, d_tr, h4_tr)

            if ok:
                msg = format_buy_message(sym, chg24, aw, ad, a4, reasons)
                tg_send(msg)
                state.setdefault("sent", {})[sym] = int(time.time())
                save_state(state)

            time.sleep(0.12)  # yumshoq rate-limit

        except Exception as e:
            print(f"[WARN] {sym} error: {e}")

def main():
    # ogohlantirish (minimal)
    print("NOTE: Bu skript signal beradi, moliyaviy maslahat emas. Risk sizda.")

    st = load_state()

    # Telegram yo'q bo'lsa ham ishlaydi (console)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        print("Telegram ON")
    else:
        print("Telegram OFF (TELEGRAM_TOKEN/TELEGRAM_CHAT_ID yo'q) -> console mode")

    while True:
        start = time.time()
        run_once(st)
        elapsed = time.time() - start
        sleep_for = max(5, SCAN_EVERY_SEC - int(elapsed))
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
