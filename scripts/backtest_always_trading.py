"""Backtest strategies designed to trade even on quiet/ranging days.

Problem: M5 Scalp requires H1 trend alignment → zero trades on ranging days.
Goal: Find strategies that generate trades regardless of trend regime.

Tests 8 range-friendly strategies across 8 assets on multiple timeframes.

Usage: .venv/bin/python scripts/backtest_always_trading.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_scalp_indicators


# ── Cost model ───────────────────────────────────────────────────────────
COSTS = {
    "NZDUSD=X": 0.00012, "EURUSD=X": 0.00010, "GBPUSD=X": 0.00014,
    "AUDUSD=X": 0.00012, "JPY=X": 0.015, "EURJPY=X": 0.020,
    "CADJPY=X": 0.020, "GC=F": 0.50, "BTC-USD": 30.0, "ES=F": 0.50,
}
INITIAL = 50.0
RISK_PCT = 3.0
MIN_SIZE = {"NZDUSD=X": 1000, "EURUSD=X": 1000, "GBPUSD=X": 1000,
            "AUDUSD=X": 1000, "JPY=X": 1000, "EURJPY=X": 1000,
            "CADJPY=X": 1000, "GC=F": 1, "BTC-USD": 0.001, "ES=F": 1}
SIZE_RND = {"NZDUSD=X": 1000, "EURUSD=X": 1000, "GBPUSD=X": 1000,
            "AUDUSD=X": 1000, "JPY=X": 1000, "EURJPY=X": 1000,
            "CADJPY=X": 1000, "GC=F": 1, "BTC-USD": 0.001, "ES=F": 1}


def fetch(symbol, period, interval):
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    return df


def simulate(signals, df, symbol, atr_sl_mult=1.5, trail_r=1.0, debounce=6):
    cost = COSTS.get(symbol, 0.00015)
    msz = MIN_SIZE.get(symbol, 1)
    srnd = SIZE_RND.get(symbol, 1)

    bal = INITIAL
    peak = INITIAL
    mdd = 0
    trades = []
    in_trade = False
    last_exit = -debounce - 1

    signals = sorted(signals, key=lambda s: s[0])
    si = 0

    for i in range(len(df)):
        if in_trade:
            bh, bl = df["high"].iloc[i], df["low"].iloc[i]
            if tdir == "BUY":
                if bl <= sl:
                    pnl = (sl - entry) * sz - cost * sz
                    bal += pnl
                    trades.append({"pnl": pnl, "dir": "BUY", "bar": i})
                    in_trade = False; last_exit = i
                else:
                    pr = (bh - entry) / sld
                    if pr >= trail_r:
                        ns = bh - 0.5 * sld
                        if ns > sl: sl = ns
            else:
                if bh >= sl:
                    pnl = (entry - sl) * sz - cost * sz
                    bal += pnl
                    trades.append({"pnl": pnl, "dir": "SELL", "bar": i})
                    in_trade = False; last_exit = i
                else:
                    pr = (entry - bl) / sld
                    if pr >= trail_r:
                        ns = bl + 0.5 * sld
                        if ns < sl: sl = ns
            if bal > peak: peak = bal
            dd = (peak - bal) / peak * 100 if peak > 0 else 0
            if dd > mdd: mdd = dd

        if not in_trade:
            while si < len(signals) and signals[si][0] <= i:
                sb, sd, slo = signals[si]; si += 1
                if sb != i or (i - last_exit) < debounce: continue
                atr = df["atr"].iloc[i]
                if pd.isna(atr) or atr <= 0: continue

                entry = df["close"].iloc[i]
                tdir = sd
                sld = slo if slo else max(atr_sl_mult * atr, atr * 0.5)
                sl = entry - sld if sd == "BUY" else entry + sld

                risk = bal * RISK_PCT / 100
                sz = risk / sld if sld > 0 else 0
                sz = max(round(sz / srnd) * srnd, msz) if srnd >= 1 else max(round(sz / srnd) * srnd, msz)
                if sz * sld > bal * 0.5 or sz <= 0: continue
                in_trade = True; break

    return trades, bal, mdd


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 1: Mean Reversion RSI (no trend filter needed)
# ══════════════════════════════════════════════════════════════════════════

def strat_mean_revert_rsi(sym, df, _):
    """RSI mean reversion — works best in ranging markets.

    Buy when RSI < 25 (oversold), sell when RSI > 75 (overbought).
    No trend filter = trades in any regime. Uses BB middle as target.
    """
    compute_indicators(df)
    sigs = []

    for i in range(50, len(df)):
        rsi = df["rsi"].iloc[i]
        c = df["close"].iloc[i]
        atr = df["atr"].iloc[i]
        bb_mid = df["sma20"].iloc[i]

        if any(pd.isna(v) for v in [rsi, c, atr, bb_mid]) or atr <= 0:
            continue

        if rsi < 25:
            sigs.append((i, "BUY", 1.5 * atr))
        elif rsi > 75:
            sigs.append((i, "SELL", 1.5 * atr))

    return simulate(sigs, df, sym, trail_r=0.8, debounce=3)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 2: Bollinger Band Bounce (range-bound specialist)
# ══════════════════════════════════════════════════════════════════════════

def strat_bb_bounce(sym, df, _):
    """Bollinger Band bounce — the classic range trader.

    Price touches lower BB + bullish candle → buy (target: middle BB).
    Price touches upper BB + bearish candle → sell (target: middle BB).
    Works best when BB bandwidth is narrow (range-bound market).
    """
    compute_indicators(df)
    sigs = []

    for i in range(50, len(df)):
        c = df["close"].iloc[i]
        o = df["open"].iloc[i]
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        atr = df["atr"].iloc[i]
        bb_upper = df["bb_upper"].iloc[i]
        bb_lower = df["bb_lower"].iloc[i]
        bb_mid = df["sma20"].iloc[i]
        bw = df["bb_bandwidth"].iloc[i]

        if any(pd.isna(v) for v in [c, o, atr, bb_upper, bb_lower, bb_mid, bw]):
            continue
        if atr <= 0:
            continue

        # Only in non-trending (BB not too wide)
        if bw > 0.04:  # Skip when BB is very wide (strong trend)
            continue

        # Bullish: price touched/crossed lower BB, closed above it as bullish candle
        if l <= bb_lower and c > bb_lower and c > o:
            sl_dist = max(c - l + 0.2 * atr, 1.0 * atr)
            sigs.append((i, "BUY", min(sl_dist, 2.0 * atr)))

        # Bearish: price touched/crossed upper BB, closed below it as bearish candle
        elif h >= bb_upper and c < bb_upper and c < o:
            sl_dist = max(h - c + 0.2 * atr, 1.0 * atr)
            sigs.append((i, "SELL", min(sl_dist, 2.0 * atr)))

    return simulate(sigs, df, sym, trail_r=0.8, debounce=3)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 3: EMA Scalp (relaxed — no H1 gate)
# ══════════════════════════════════════════════════════════════════════════

def strat_ema_scalp_relaxed(sym, df, _):
    """EMA9/21 crossover WITHOUT the H1 trend gate.

    Like your current M5 scalp but removes the H1 requirement.
    Uses RSI + BB as confirmation instead. More trades, lower quality.
    """
    compute_indicators(df)
    compute_scalp_indicators(df)
    sigs = []

    for i in range(50, len(df)):
        c = df["close"].iloc[i]
        atr = df["atr"].iloc[i]

        ema9 = df.get("ema9")
        ema21 = df.get("ema21")
        rsi7 = df.get("rsi7")

        if ema9 is None or ema21 is None or rsi7 is None:
            continue

        e9 = ema9.iloc[i]
        e21 = ema21.iloc[i]
        r7 = rsi7.iloc[i]

        if any(pd.isna(v) for v in [c, atr, e9, e21, r7]) or atr <= 0:
            continue

        # Check for fresh EMA cross (within last 3 bars)
        cross_up = False
        cross_dn = False
        for j in range(1, min(4, i)):
            pe9 = ema9.iloc[i-j]
            pe21 = ema21.iloc[i-j]
            if pd.isna(pe9) or pd.isna(pe21):
                continue
            if pe9 <= pe21 and e9 > e21:
                cross_up = True
            if pe9 >= pe21 and e9 < e21:
                cross_dn = True

        # Buy: EMA cross up + RSI not overbought
        if cross_up and r7 < 70 and r7 > 30:
            sigs.append((i, "BUY", 1.5 * atr))

        # Sell: EMA cross down + RSI not oversold
        elif cross_dn and r7 > 30 and r7 < 70:
            sigs.append((i, "SELL", 1.5 * atr))

    return simulate(sigs, df, sym, trail_r=1.0, debounce=6)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 4: MACD Zero-Cross + RSI (medium frequency)
# ══════════════════════════════════════════════════════════════════════════

def strat_macd_zero_cross(sym, df, _):
    """MACD histogram sign change + RSI confirmation.

    MACD histogram crosses from negative to positive → buy.
    No trend gate needed. RSI as quality filter only (not directional).
    """
    compute_indicators(df)
    sigs = []

    for i in range(52, len(df)):
        c = df["close"].iloc[i]
        atr = df["atr"].iloc[i]
        macd = df["macd"].iloc[i]
        macd_sig = df["macd_signal"].iloc[i]
        hist = macd - macd_sig if not pd.isna(macd) and not pd.isna(macd_sig) else None
        rsi = df["rsi"].iloc[i]

        if hist is None or pd.isna(atr) or pd.isna(rsi) or atr <= 0:
            continue

        prev_macd = df["macd"].iloc[i-1]
        prev_sig = df["macd_signal"].iloc[i-1]
        if pd.isna(prev_macd) or pd.isna(prev_sig):
            continue
        prev_hist = prev_macd - prev_sig

        # MACD histogram crosses zero
        if prev_hist <= 0 and hist > 0 and rsi > 40 and rsi < 70:
            sigs.append((i, "BUY", 1.5 * atr))
        elif prev_hist >= 0 and hist < 0 and rsi > 30 and rsi < 60:
            sigs.append((i, "SELL", 1.5 * atr))

    return simulate(sigs, df, sym, trail_r=1.0, debounce=4)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 5: Stochastic RSI Oversold/Overbought Scalp
# ══════════════════════════════════════════════════════════════════════════

def strat_stoch_rsi_scalp(sym, df, _):
    """Stochastic RSI extreme scalp — high frequency mean reversion.

    Compute Stoch RSI (RSI of RSI), trade extremes with fast exits.
    Designed for M5/M15 timeframes where quick reversals are common.
    """
    compute_indicators(df)

    # Compute Stochastic RSI
    rsi = df["rsi"]
    period = 14
    stoch_rsi = (rsi - rsi.rolling(period).min()) / (rsi.rolling(period).max() - rsi.rolling(period).min())
    stoch_k = stoch_rsi.rolling(3).mean() * 100
    stoch_d = stoch_k.rolling(3).mean()

    sigs = []

    for i in range(55, len(df)):
        c = df["close"].iloc[i]
        atr = df["atr"].iloc[i]
        k = stoch_k.iloc[i]
        d = stoch_d.iloc[i]
        prev_k = stoch_k.iloc[i-1]
        prev_d = stoch_d.iloc[i-1]

        if any(pd.isna(v) for v in [c, atr, k, d, prev_k, prev_d]) or atr <= 0:
            continue

        # Buy: K crosses above D from oversold zone (<20)
        if prev_k < prev_d and k > d and k < 30 and prev_k < 20:
            sigs.append((i, "BUY", 1.2 * atr))

        # Sell: K crosses below D from overbought zone (>80)
        elif prev_k > prev_d and k < d and k > 70 and prev_k > 80:
            sigs.append((i, "SELL", 1.2 * atr))

    return simulate(sigs, df, sym, trail_r=0.8, debounce=3)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 6: Inside Bar Breakout (volatility expansion from compression)
# ══════════════════════════════════════════════════════════════════════════

def strat_inside_bar(sym, df, _):
    """Inside bar breakout — compression → expansion.

    An inside bar (range within previous bar) signals compression.
    Trade the breakout of the inside bar's range. Works in any regime.
    """
    compute_indicators(df)
    sigs = []

    for i in range(51, len(df)):
        # Check for inside bar at i-1
        prev_h = df["high"].iloc[i-2]
        prev_l = df["low"].iloc[i-2]
        ib_h = df["high"].iloc[i-1]
        ib_l = df["low"].iloc[i-1]

        if any(pd.isna(v) for v in [prev_h, prev_l, ib_h, ib_l]):
            continue

        # Inside bar: high lower and low higher than previous bar
        if ib_h < prev_h and ib_l > prev_l:
            c = df["close"].iloc[i]
            atr = df["atr"].iloc[i]

            if pd.isna(c) or pd.isna(atr) or atr <= 0:
                continue

            ib_range = ib_h - ib_l

            # Current bar breaks above inside bar high → buy
            if c > ib_h:
                sl_dist = max(c - ib_l, 1.0 * atr)
                sigs.append((i, "BUY", min(sl_dist, 2.5 * atr)))

            # Current bar breaks below inside bar low → sell
            elif c < ib_l:
                sl_dist = max(ib_h - c, 1.0 * atr)
                sigs.append((i, "SELL", min(sl_dist, 2.5 * atr)))

    return simulate(sigs, df, sym, trail_r=1.0, debounce=4)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 7: SMA20 Pullback (simple trend-following with loose gate)
# ══════════════════════════════════════════════════════════════════════════

def strat_sma20_pullback(sym, df, _):
    """SMA20 pullback — buy dips in uptrend, sell rallies in downtrend.

    Uses only SMA20 as the trend reference (much looser than SMA20+50 alignment).
    Price pulls back to SMA20, gets a bounce candle → enter.
    """
    compute_indicators(df)
    sigs = []

    for i in range(52, len(df)):
        c = df["close"].iloc[i]
        o = df["open"].iloc[i]
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        atr = df["atr"].iloc[i]
        sma20 = df["sma20"].iloc[i]
        rsi = df["rsi"].iloc[i]

        if any(pd.isna(v) for v in [c, o, h, l, atr, sma20, rsi]) or atr <= 0:
            continue

        dist_to_sma = abs(c - sma20) / atr

        # Price near SMA20 (within 0.5 ATR)
        if dist_to_sma > 0.8:
            continue

        # Determine recent trend by SMA20 slope
        sma20_prev = df["sma20"].iloc[i-5]
        if pd.isna(sma20_prev):
            continue
        sma_slope = (sma20 - sma20_prev) / atr

        # Uptrend: SMA rising, price bounced off SMA, bullish candle
        if sma_slope > 0.1 and c > o and l <= sma20 * 1.003 and rsi > 35 and rsi < 65:
            sigs.append((i, "BUY", 1.5 * atr))

        # Downtrend: SMA falling, price rejected at SMA, bearish candle
        elif sma_slope < -0.1 and c < o and h >= sma20 * 0.997 and rsi > 35 and rsi < 65:
            sigs.append((i, "SELL", 1.5 * atr))

    return simulate(sigs, df, sym, trail_r=1.0, debounce=4)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 8: Dual Timeframe RSI Divergence
# ══════════════════════════════════════════════════════════════════════════

def strat_rsi_divergence(sym, df, _):
    """RSI divergence — price makes new low but RSI makes higher low.

    Classic divergence signal works in all market conditions.
    No trend filter needed — divergence IS the signal.
    """
    compute_indicators(df)
    sigs = []

    lookback = 20

    for i in range(50 + lookback, len(df)):
        c = df["close"].iloc[i]
        atr = df["atr"].iloc[i]
        rsi = df["rsi"].iloc[i]

        if any(pd.isna(v) for v in [c, atr, rsi]) or atr <= 0:
            continue

        window = df.iloc[i-lookback:i+1]

        # Bullish divergence: price makes lower low, RSI makes higher low
        price_low_now = window["low"].iloc[-5:].min()
        price_low_prev = window["low"].iloc[:10].min()
        rsi_at_price_low_now = window["rsi"].iloc[-5:].min()
        rsi_at_price_low_prev = window["rsi"].iloc[:10].min()

        if (not pd.isna(rsi_at_price_low_now) and not pd.isna(rsi_at_price_low_prev)):
            if (price_low_now < price_low_prev and
                rsi_at_price_low_now > rsi_at_price_low_prev and
                rsi < 45 and c > df["open"].iloc[i]):
                sigs.append((i, "BUY", 1.5 * atr))
                continue

        # Bearish divergence: price makes higher high, RSI makes lower high
        price_high_now = window["high"].iloc[-5:].max()
        price_high_prev = window["high"].iloc[:10].max()
        rsi_at_price_high_now = window["rsi"].iloc[-5:].max()
        rsi_at_price_high_prev = window["rsi"].iloc[:10].max()

        if (not pd.isna(rsi_at_price_high_now) and not pd.isna(rsi_at_price_high_prev)):
            if (price_high_now > price_high_prev and
                rsi_at_price_high_now < rsi_at_price_high_prev and
                rsi > 55 and c < df["open"].iloc[i]):
                sigs.append((i, "SELL", 1.5 * atr))

    return simulate(sigs, df, sym, trail_r=1.0, debounce=5)


# ══════════════════════════════════════════════════════════════════════════
# REGISTRY & RUNNER
# ══════════════════════════════════════════════════════════════════════════

STRATEGIES = [
    ("Mean Revert RSI",       strat_mean_revert_rsi,    "Works in range: buy RSI<25, sell RSI>75"),
    ("BB Bounce",             strat_bb_bounce,           "Range specialist: trade BB touch + reversal candle"),
    ("EMA Scalp (no H1)",     strat_ema_scalp_relaxed,   "Your M5 scalp WITHOUT the H1 trend gate"),
    ("MACD Zero-Cross",       strat_macd_zero_cross,     "MACD histogram sign change + RSI filter"),
    ("Stoch RSI Scalp",       strat_stoch_rsi_scalp,     "High-freq mean reversion on Stoch RSI extremes"),
    ("Inside Bar Breakout",   strat_inside_bar,          "Compression → expansion: trade inside bar break"),
    ("SMA20 Pullback",        strat_sma20_pullback,      "Buy dips to SMA20 (looser than SMA20+50 gate)"),
    ("RSI Divergence",        strat_rsi_divergence,      "Price/RSI divergence — works in any regime"),
]

# More assets, more timeframes
CONFIGS = [
    # (symbol, display, interval, period, needs_h1_data)
    ("NZDUSD=X",  "NZD/USD",  "5m",  "60d"),
    ("NZDUSD=X",  "NZD/USD",  "15m", "60d"),
    ("NZDUSD=X",  "NZD/USD",  "1h",  "2y"),
    ("AUDUSD=X",  "AUD/USD",  "5m",  "60d"),
    ("AUDUSD=X",  "AUD/USD",  "15m", "60d"),
    ("AUDUSD=X",  "AUD/USD",  "1h",  "2y"),
    ("EURUSD=X",  "EUR/USD",  "5m",  "60d"),
    ("EURUSD=X",  "EUR/USD",  "15m", "60d"),
    ("EURUSD=X",  "EUR/USD",  "1h",  "2y"),
    ("GBPUSD=X",  "GBP/USD",  "5m",  "60d"),
    ("GBPUSD=X",  "GBP/USD",  "15m", "60d"),
    ("GBPUSD=X",  "GBP/USD",  "1h",  "2y"),
    ("JPY=X",     "USD/JPY",  "5m",  "60d"),
    ("JPY=X",     "USD/JPY",  "15m", "60d"),
    ("JPY=X",     "USD/JPY",  "1h",  "2y"),
    ("EURJPY=X",  "EUR/JPY",  "15m", "60d"),
    ("EURJPY=X",  "EUR/JPY",  "1h",  "2y"),
    ("CADJPY=X",  "CAD/JPY",  "15m", "60d"),
    ("CADJPY=X",  "CAD/JPY",  "1h",  "2y"),
    ("GC=F",      "Gold",     "1h",  "2y"),
    ("BTC-USD",   "BTC",      "15m", "60d"),
    ("BTC-USD",   "BTC",      "1h",  "2y"),
]


def metrics(trades, bal, mdd, months):
    if not trades or len(trades) < 3:
        return None
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wp = sum(t["pnl"] for t in wins) if wins else 0
    lp = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    ret = (bal - INITIAL) / INITIAL * 100
    mo = ret / months if months > 0 else 0
    wr = len(wins) / len(trades) * 100
    pf = wp / lp
    pnls = [t["pnl"] for t in trades]
    sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls)) if len(pnls) > 1 and np.std(pnls) > 0 else 0
    aw = np.mean([t["pnl"] for t in wins]) if wins else 0
    al = np.mean([abs(t["pnl"]) for t in losses]) if losses else 0.001
    exp = (wr/100 * aw) - ((1 - wr/100) * al)
    return {"trades": len(trades), "tmo": len(trades)/months if months > 0 else 0,
            "wr": wr, "pf": pf, "ret": ret, "mo": mo, "mdd": mdd,
            "sharpe": sharpe, "exp": exp, "bal": bal, "aw": aw, "al": al}


def rank(m):
    if m is None or m["trades"] < 5:
        return -999
    ddp = min(m["mdd"] / 100, 0.8)
    sb = max(0.5, min(2.0, 1.0 + m["sharpe"] * 0.1))
    pfc = min(m["pf"], 5.0)
    # Bonus for trade frequency (we want strategies that always trade)
    freq_bonus = min(m["tmo"] / 20, 2.0)  # Up to 2x bonus for 20+ trades/mo
    return m["mo"] * pfc * (1 - ddp) * sb * max(freq_bonus, 0.5)


def run():
    print("=" * 100)
    print("ALWAYS-TRADING STRATEGY BACKTEST")
    print("Finding strategies that generate trades even on quiet/ranging days")
    print("=" * 100)
    print(f"Capital: €{INITIAL:.0f} | Risk: {RISK_PCT}%/trade")
    print()

    cache = {}
    results = []

    for sn, fn, desc in STRATEGIES:
        print(f"\n{'─'*100}")
        print(f"  {sn}: {desc}")
        print()

        hdr = f"  {'Asset':<10s} {'TF':<4s} | {'#':>5s} | {'T/mo':>5s} | {'WR%':>6s} | {'PF':>5s} | {'Return':>9s} | {'Mo/Ret':>9s} | {'MaxDD':>7s} | {'Sharpe':>6s} | {'Final':>8s}"
        print(hdr)
        print(f"  {'-'*14}-+-{'-'*5}-+-{'-'*5}-+-{'-'*6}-+-{'-'*5}-+-{'-'*9}-+-{'-'*9}-+-{'-'*7}-+-{'-'*6}-+-{'-'*8}")

        for sym, aname, interval, period in CONFIGS:
            ck = f"{sym}_{interval}_{period}"
            if ck not in cache:
                cache[ck] = fetch(sym, period, interval)

            df = cache[ck].copy()
            if df.empty:
                continue

            try:
                tr, bal, mdd = fn(sym, df, None)
            except Exception as e:
                print(f"  {aname:<10s} {interval:<4s} | ERROR: {e}")
                continue

            days = (df.index[-1] - df.index[0]).days
            months = max(days / 30, 0.5)
            m = metrics(tr, bal, mdd, months)

            if m is None:
                if len(tr) > 0:
                    print(f"  {aname:<10s} {interval:<4s} | {len(tr):>5d} |  (too few)")
                continue

            results.append({"strat": sn, "asset": aname, "tf": interval, "sym": sym,
                           "period": period, "m": m, "rank": rank(m)})

            print(f"  {aname:<10s} {interval:<4s} | {m['trades']:>5d} | {m['tmo']:>5.1f} | {m['wr']:>5.1f}% | {m['pf']:>5.2f} | {m['ret']:>+8.1f}% | {m['mo']:>+8.1f}% | {m['mdd']:>6.1f}% | {m['sharpe']:>6.2f} | €{m['bal']:>6.2f}")

    # ── RANKING ──────────────────────────────────────────────────────
    print(f"\n\n{'='*100}")
    print("TOP 30 OVERALL RANKING (sorted by composite score: return × PF × (1-DD) × Sharpe × freq)")
    print(f"{'='*100}")
    results.sort(key=lambda r: r["rank"], reverse=True)

    print(f"\n  {'#':>3s} | {'Strategy':<22s} | {'Asset':<10s} {'TF':<4s} | {'T/mo':>5s} | {'Mo/Ret':>9s} | {'WR%':>6s} | {'PF':>5s} | {'MaxDD':>7s} | {'Sharpe':>6s} | {'Score':>8s}")
    print(f"  {'-'*3}-+-{'-'*22}-+-{'-'*14}-+-{'-'*5}-+-{'-'*9}-+-{'-'*6}-+-{'-'*5}-+-{'-'*7}-+-{'-'*6}-+-{'-'*8}")

    for i, r in enumerate(results[:30], 1):
        m = r["m"]
        print(f"  {i:>3d} | {r['strat']:<22s} | {r['asset']:<10s} {r['tf']:<4s} | {m['tmo']:>5.1f} | {m['mo']:>+8.1f}% | {m['wr']:>5.1f}% | {m['pf']:>5.2f} | {m['mdd']:>6.1f}% | {m['sharpe']:>6.2f} | {r['rank']:>8.1f}")

    # ── BEST PER STRATEGY ────────────────────────────────────────────
    print(f"\n\n{'='*100}")
    print("BEST CONFIG PER STRATEGY")
    print(f"{'='*100}\n")

    best = {}
    for r in results:
        s = r["strat"]
        if s not in best or r["rank"] > best[s]["rank"]:
            best[s] = r

    for sn, r in sorted(best.items(), key=lambda x: x[1]["rank"], reverse=True):
        m = r["m"]
        print(f"  {sn:<22s} → {r['asset']:<10s} {r['tf']:<4s} | {m['tmo']:>.0f} t/mo | {m['mo']:>+.1f}%/mo | WR {m['wr']:.0f}% | PF {m['pf']:.2f} | DD {m['mdd']:.1f}% | Score {r['rank']:.1f}")

    # ── BEST PER ASSET ──────────────────────────────────────────────
    print(f"\n\n{'='*100}")
    print("BEST STRATEGY PER ASSET (what to trade on each pair)")
    print(f"{'='*100}\n")

    best_asset = {}
    for r in results:
        k = f"{r['asset']} {r['tf']}"
        if k not in best_asset or r["rank"] > best_asset[k]["rank"]:
            best_asset[k] = r

    for k, r in sorted(best_asset.items(), key=lambda x: x[1]["rank"], reverse=True):
        m = r["m"]
        if r["rank"] > 0:
            print(f"  {k:<16s} → {r['strat']:<22s} | {m['tmo']:>.0f} t/mo | {m['mo']:>+.1f}%/mo | PF {m['pf']:.2f} | DD {m['mdd']:.1f}%")

    # ── HIGH FREQUENCY PICKS ────────────────────────────────────────
    print(f"\n\n{'='*100}")
    print("HIGHEST TRADE FREQUENCY (strategies that ALWAYS have signals)")
    print(f"{'='*100}\n")

    freq_sorted = sorted([r for r in results if r["m"]["pf"] > 1.0],
                        key=lambda r: r["m"]["tmo"], reverse=True)

    for i, r in enumerate(freq_sorted[:15], 1):
        m = r["m"]
        print(f"  {i:>2d}. {r['strat']:<22s} {r['asset']:<10s} {r['tf']:<4s} | {m['tmo']:>6.1f} t/mo | {m['mo']:>+.1f}%/mo | PF {m['pf']:.2f} | WR {m['wr']:.0f}%")

    # ── VERDICT ──────────────────────────────────────────────────────
    print(f"\n\n{'='*100}")
    print("VERDICT: Strategies to add for always-trading coverage")
    print(f"{'='*100}\n")

    if results:
        profitable = [r for r in results if r["m"]["pf"] > 1.1 and r["m"]["tmo"] > 5]
        if profitable:
            profitable.sort(key=lambda r: r["rank"], reverse=True)
            print("  Recommended additions (PF > 1.1, 5+ trades/mo):\n")
            for i, r in enumerate(profitable[:10], 1):
                m = r["m"]
                print(f"  {i}. {r['strat']} on {r['asset']} ({r['tf']}) — {m['tmo']:.0f} t/mo, {m['mo']:+.1f}%/mo, PF {m['pf']:.2f}, DD {m['mdd']:.1f}%")
        else:
            print("  No strategy combo met the threshold (PF>1.1, 5+ t/mo)")
    print()


if __name__ == "__main__":
    run()
