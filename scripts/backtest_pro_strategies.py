"""Pro-level strategy backtester — 10 institutional/prop-style strategies.

Strategies modeled after real prop firm and institutional approaches:
- Session open plays, liquidity sweeps, fair value gaps
- Multi-timeframe confluence, order block concepts
- Volatility regime filters, Asian range breakouts

Usage: .venv/bin/python scripts/backtest_pro_strategies.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_scalp_indicators
from app.services.m5_scalp_scoring import M5ScalpScoringEngine


# ── Cost model ───────────────────────────────────────────────────────────
COSTS = {
    "NZDUSD=X": 0.00012, "EURUSD=X": 0.00010, "GBPUSD=X": 0.00014,
    "AUDUSD=X": 0.00012, "JPY=X": 0.015, "EURJPY=X": 0.020,
    "GC=F": 0.50, "BTC-USD": 30.0,
}
INITIAL = 50.0
RISK_PCT = 4.0
MIN_SIZE = {"NZDUSD=X": 1000, "EURUSD=X": 1000, "GBPUSD=X": 1000,
            "AUDUSD=X": 1000, "JPY=X": 1000, "EURJPY=X": 1000,
            "GC=F": 1, "BTC-USD": 0.001}
SIZE_RND = {"NZDUSD=X": 1000, "EURUSD=X": 1000, "GBPUSD=X": 1000,
            "AUDUSD=X": 1000, "JPY=X": 1000, "EURJPY=X": 1000,
            "GC=F": 1, "BTC-USD": 0.001}


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
# STRATEGY 0: Current M5 Scalp (baseline)
# ══════════════════════════════════════════════════════════════════════════

def strat_m5_scalp(sym, m5, h1):
    engine = M5ScalpScoringEngine()
    compute_indicators(m5); compute_scalp_indicators(m5); compute_indicators(h1)
    h1i = h1.index
    sigs = []; lb = -13; ld = None
    for i in range(50, len(m5)):
        t = m5.index[i]
        mask = h1i <= t
        if not mask.any(): continue
        hr = h1.loc[h1i[mask][-1]]
        tail = m5.iloc[max(0,i-5):i+1]
        r = engine.score(hr, tail, bar_time=t)
        if r["direction"] is None: continue
        d = r["direction"]
        if (i - lb) < 12 and ld == d: continue
        atr = m5["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0: continue
        sld = max(1.0 * atr, 0.0020 if "JPY" not in sym else 0.20)
        sigs.append((i, d, sld)); lb = i; ld = d
    return simulate(sigs, m5, sym, debounce=12)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 1: London Open Breakout (Kill Zone)
# ══════════════════════════════════════════════════════════════════════════

def strat_london_killzone(sym, df, _):
    """Trade the London open kill zone (7-9 UTC).

    Institutional money enters at London open. Wait for the first
    decisive move in the 7-9 UTC window that breaks the Asian session
    range (0-6 UTC high/low). Trade with that break, stop behind
    the Asian range.
    """
    compute_indicators(df)
    sigs = []

    for i in range(50, len(df)):
        if not hasattr(df.index[i], 'hour'):
            continue
        hour = df.index[i].hour

        # Only trade at London open (7-9 UTC)
        if hour < 7 or hour > 9:
            continue

        # Build Asian range (0-6 UTC same day)
        day = df.index[i].date()
        asian_mask = (df.index.date == day) & (df.index.hour >= 0) & (df.index.hour < 7)
        asian_bars = df.loc[asian_mask]
        if len(asian_bars) < 3:
            continue

        asian_high = asian_bars["high"].max()
        asian_low = asian_bars["low"].min()
        asian_range = asian_high - asian_low

        close = df["close"].iloc[i]
        atr = df["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0 or asian_range <= 0:
            continue

        # Break above Asian high → long
        if close > asian_high and close > df["sma20"].iloc[i]:
            sl_dist = close - asian_low  # Stop below Asian low
            sl_dist = min(sl_dist, 2.5 * atr)  # Cap at 2.5 ATR
            sigs.append((i, "BUY", sl_dist))
        # Break below Asian low → short
        elif close < asian_low and close < df["sma20"].iloc[i]:
            sl_dist = asian_high - close
            sl_dist = min(sl_dist, 2.5 * atr)
            sigs.append((i, "SELL", sl_dist))

    return simulate(sigs, df, sym, trail_r=1.0, debounce=8)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 2: Liquidity Sweep Reversal (Stop Hunt)
# ══════════════════════════════════════════════════════════════════════════

def strat_liquidity_sweep(sym, df, _):
    """Fade the liquidity sweep / stop hunt.

    Price sweeps the previous swing high/low (taking out stops),
    then reverses hard back inside the range = institutional
    accumulation. Enter on the reversal candle close.
    """
    compute_indicators(df)
    sigs = []

    # Track swing highs/lows (5-bar pivots)
    for i in range(55, len(df)):
        # Find recent swing high/low (last 20 bars, 5-bar pivot)
        lookback = df.iloc[max(0, i-25):i]
        if len(lookback) < 10:
            continue

        swing_high = lookback["high"].max()
        swing_low = lookback["low"].min()
        sh_idx = lookback["high"].idxmax()
        sl_idx = lookback["low"].idxmin()

        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        c = df["close"].iloc[i]
        o = df["open"].iloc[i]
        atr = df["atr"].iloc[i]

        if pd.isna(atr) or atr <= 0:
            continue

        # Bearish sweep: wick above swing high but close back below it
        # (price swept stops above then reversed)
        if h > swing_high and c < swing_high and c < o:
            # Confirm: RSI was elevated (distribution zone)
            rsi = df["rsi"].iloc[i]
            if not pd.isna(rsi) and rsi > 55:
                sl_dist = h - c + 0.3 * atr  # Stop above the sweep wick
                sigs.append((i, "SELL", min(sl_dist, 2.0 * atr)))

        # Bullish sweep: wick below swing low but close back above it
        if l < swing_low and c > swing_low and c > o:
            rsi = df["rsi"].iloc[i]
            if not pd.isna(rsi) and rsi < 45:
                sl_dist = c - l + 0.3 * atr
                sigs.append((i, "BUY", min(sl_dist, 2.0 * atr)))

    return simulate(sigs, df, sym, trail_r=1.2, debounce=5)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 3: Fair Value Gap (FVG) Fill
# ══════════════════════════════════════════════════════════════════════════

def strat_fvg_fill(sym, df, _):
    """Trade Fair Value Gap (imbalance) fills.

    FVG = gap between candle 1 high and candle 3 low (bullish) or
    candle 1 low and candle 3 high (bearish). When price returns to
    fill the gap, enter in the original impulse direction.
    """
    compute_indicators(df)
    sigs = []

    # Detect FVGs and store them
    fvgs = []  # (bar_idx, type, gap_top, gap_bottom, filled)

    for i in range(2, len(df)):
        h1 = df["high"].iloc[i-2]
        l3 = df["low"].iloc[i]
        l1 = df["low"].iloc[i-2]
        h3 = df["high"].iloc[i]
        c2 = df["close"].iloc[i-1]
        o2 = df["open"].iloc[i-1]

        if any(pd.isna(v) for v in [h1, l3, l1, h3, c2, o2]):
            continue

        # Bullish FVG: big up move, gap between candle 1 high and candle 3 low
        if l3 > h1 and c2 > o2:
            body = abs(c2 - o2)
            atr = df["atr"].iloc[i]
            if not pd.isna(atr) and body > 0.8 * atr:
                fvgs.append({"idx": i, "type": "bull", "top": l3, "bot": h1, "filled": False, "expire": i + 40})

        # Bearish FVG: big down move
        if h3 < l1 and c2 < o2:
            body = abs(o2 - c2)
            atr = df["atr"].iloc[i]
            if not pd.isna(atr) and body > 0.8 * atr:
                fvgs.append({"idx": i, "type": "bear", "top": l1, "bot": h3, "filled": False, "expire": i + 40})

    # Now scan for fills
    for i in range(50, len(df)):
        c = df["close"].iloc[i]
        atr = df["atr"].iloc[i]
        sma50 = df["sma50"].iloc[i]
        if pd.isna(atr) or pd.isna(sma50) or pd.isna(c):
            continue

        for fvg in fvgs:
            if fvg["filled"] or i <= fvg["idx"] + 2 or i > fvg["expire"]:
                continue

            if fvg["type"] == "bull" and c <= fvg["top"] and c >= fvg["bot"]:
                # Price returned to fill bullish FVG → long (if trend agrees)
                if c > sma50:
                    fvg["filled"] = True
                    sigs.append((i, "BUY", max(1.5 * atr, c - fvg["bot"])))
                    break

            elif fvg["type"] == "bear" and c >= fvg["bot"] and c <= fvg["top"]:
                if c < sma50:
                    fvg["filled"] = True
                    sigs.append((i, "SELL", max(1.5 * atr, fvg["top"] - c)))
                    break

    return simulate(sigs, df, sym, trail_r=1.0, debounce=4)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 4: Order Block Reaction
# ══════════════════════════════════════════════════════════════════════════

def strat_order_block(sym, df, _):
    """Trade reactions at order blocks (last opposing candle before impulse).

    An order block = the last bearish candle before a strong bullish move
    (or vice versa). When price returns to this zone, institutions defend it.
    """
    compute_indicators(df)
    sigs = []
    obs = []  # order blocks: (bar, type, zone_high, zone_low, expire)

    for i in range(3, len(df)):
        # Detect strong impulse move (>2 ATR in 3 bars)
        atr = df["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            continue

        move_up = df["close"].iloc[i] - df["low"].iloc[i-3]
        move_dn = df["high"].iloc[i-3] - df["close"].iloc[i]

        if move_up > 2.0 * atr:
            # Find last bearish candle in the 3-bar window
            for j in range(i-3, i):
                if df["close"].iloc[j] < df["open"].iloc[j]:
                    obs.append({
                        "type": "bull_ob",
                        "high": df["high"].iloc[j],
                        "low": df["low"].iloc[j],
                        "expire": i + 60,
                        "used": False,
                    })
                    break

        if move_dn > 2.0 * atr:
            for j in range(i-3, i):
                if df["close"].iloc[j] > df["open"].iloc[j]:
                    obs.append({
                        "type": "bear_ob",
                        "high": df["high"].iloc[j],
                        "low": df["low"].iloc[j],
                        "expire": i + 60,
                        "used": False,
                    })
                    break

    # Scan for price returning to order blocks
    for i in range(50, len(df)):
        c = df["close"].iloc[i]
        atr = df["atr"].iloc[i]
        rsi = df["rsi"].iloc[i]
        if pd.isna(atr) or pd.isna(c) or pd.isna(rsi):
            continue

        for ob in obs:
            if ob["used"] or i > ob["expire"]:
                continue

            if ob["type"] == "bull_ob" and c >= ob["low"] and c <= ob["high"]:
                if rsi < 55:  # Not already overbought
                    ob["used"] = True
                    sigs.append((i, "BUY", max(1.2 * atr, c - ob["low"] + 0.2 * atr)))
                    break

            elif ob["type"] == "bear_ob" and c >= ob["low"] and c <= ob["high"]:
                if rsi > 45:
                    ob["used"] = True
                    sigs.append((i, "SELL", max(1.2 * atr, ob["high"] - c + 0.2 * atr)))
                    break

    return simulate(sigs, df, sym, trail_r=1.0, debounce=4)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 5: Volatility Contraction Expansion (VCP)
# ══════════════════════════════════════════════════════════════════════════

def strat_vcp(sym, df, _):
    """Volatility Contraction Pattern — Mark Minervini style.

    Successive tighter ranges (ATR declining for 5+ bars) followed
    by expansion breakout. Classic institutional accumulation pattern.
    """
    compute_indicators(df)
    sigs = []

    # Track ATR contraction
    for i in range(55, len(df)):
        atr = df["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            continue

        # Check ATR has been declining for at least 5 bars
        contracting = True
        for j in range(1, 6):
            prev_atr = df["atr"].iloc[i-j]
            curr_atr = df["atr"].iloc[i-j+1]
            if pd.isna(prev_atr) or pd.isna(curr_atr) or curr_atr >= prev_atr * 1.05:
                contracting = False
                break

        if not contracting:
            continue

        # Now look for expansion: current bar range > 1.5x previous bar
        bar_range = df["high"].iloc[i] - df["low"].iloc[i]
        prev_range = df["high"].iloc[i-1] - df["low"].iloc[i-1]
        if pd.isna(prev_range) or prev_range <= 0:
            continue

        if bar_range > 1.5 * prev_range:
            c = df["close"].iloc[i]
            o = df["open"].iloc[i]
            sma20 = df["sma20"].iloc[i]
            sma50 = df["sma50"].iloc[i]

            if pd.isna(sma20) or pd.isna(sma50):
                continue

            # Bullish expansion + trend
            if c > o and c > sma20:
                sigs.append((i, "BUY", None))
            # Bearish expansion + trend
            elif c < o and c < sma20:
                sigs.append((i, "SELL", None))

    return simulate(sigs, df, sym, atr_sl_mult=1.5, trail_r=1.0, debounce=5)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 6: Multi-TF Momentum Confluence
# ══════════════════════════════════════════════════════════════════════════

def strat_mtf_momentum(sym, m15, h1):
    """Multi-timeframe momentum alignment.

    H1: MACD bullish + RSI > 50 + price > SMA50 (trend)
    M15: RSI pullback to 40-50 zone then bounces (entry timing)
    Both must agree on direction. The H1 is the "where" and
    M15 is the "when".
    """
    compute_indicators(m15)
    compute_indicators(h1)
    h1i = h1.index
    sigs = []

    for i in range(50, len(m15)):
        t = m15.index[i]
        mask = h1i <= t
        if not mask.any():
            continue
        hr = h1.loc[h1i[mask][-1]]

        # H1 conditions
        h1_macd = hr.get("macd")
        h1_sig = hr.get("macd_signal")
        h1_rsi = hr.get("rsi")
        h1_close = hr.get("close")
        h1_sma50 = hr.get("sma50")
        h1_sma20 = hr.get("sma20")

        if any(pd.isna(v) for v in [h1_macd, h1_sig, h1_rsi, h1_close, h1_sma50, h1_sma20]):
            continue

        h1_bull = h1_macd > h1_sig and h1_rsi > 50 and h1_close > h1_sma50 and h1_sma20 > h1_sma50
        h1_bear = h1_macd < h1_sig and h1_rsi < 50 and h1_close < h1_sma50 and h1_sma20 < h1_sma50

        if not h1_bull and not h1_bear:
            continue

        # M15 conditions — pullback entry
        m15_rsi = m15["rsi"].iloc[i]
        m15_close = m15["close"].iloc[i]
        m15_sma20 = m15["sma20"].iloc[i]

        if pd.isna(m15_rsi) or pd.isna(m15_close) or pd.isna(m15_sma20):
            continue

        # Check RSI was recently lower/higher (pullback happened)
        if i < 3:
            continue
        recent_rsi = [m15["rsi"].iloc[i-j] for j in range(1, 4) if not pd.isna(m15["rsi"].iloc[i-j])]
        if not recent_rsi:
            continue

        if h1_bull:
            # M15 RSI pulled back below 45 recently, now recovering above 50
            if min(recent_rsi) < 45 and m15_rsi > 50 and m15_close > m15_sma20:
                sigs.append((i, "BUY", None))

        elif h1_bear:
            if max(recent_rsi) > 55 and m15_rsi < 50 and m15_close < m15_sma20:
                sigs.append((i, "SELL", None))

    return simulate(sigs, m15, sym, atr_sl_mult=1.5, trail_r=1.0, debounce=8)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 7: NY Session Reversal (ICT Judas Swing)
# ══════════════════════════════════════════════════════════════════════════

def strat_ny_reversal(sym, df, _):
    """NY session reversal (Judas swing concept).

    The market often fakes out in one direction during early NY (13-14 UTC)
    then reverses. If London established a clear direction and early NY
    moves against it then fails → fade the fake move.
    """
    compute_indicators(df)
    sigs = []

    for i in range(50, len(df)):
        if not hasattr(df.index[i], 'hour'):
            continue
        hour = df.index[i].hour
        day = df.index[i].date()

        # Only look at 14-16 UTC (after initial NY fake is done)
        if hour < 14 or hour > 16:
            continue

        atr = df["atr"].iloc[i]
        sma20 = df["sma20"].iloc[i]
        if pd.isna(atr) or pd.isna(sma20) or atr <= 0:
            continue

        # London session direction (7-12 UTC)
        london = df.loc[(df.index.date == day) & (df.index.hour >= 7) & (df.index.hour < 13)]
        if len(london) < 3:
            continue
        london_dir = london["close"].iloc[-1] - london["open"].iloc[0]

        # Early NY move (13-14 UTC)
        early_ny = df.loc[(df.index.date == day) & (df.index.hour >= 13) & (df.index.hour < 14)]
        if len(early_ny) < 1:
            continue
        ny_move = early_ny["close"].iloc[-1] - early_ny["open"].iloc[0]

        # Judas swing: NY moved AGAINST London direction
        close = df["close"].iloc[i]

        if london_dir > 0.3 * atr and ny_move < -0.2 * atr:
            # London was bullish, NY faked bearish → go long
            if close > early_ny["low"].min():  # Reclaimed the NY low
                sigs.append((i, "BUY", max(1.5 * atr, close - early_ny["low"].min())))

        elif london_dir < -0.3 * atr and ny_move > 0.2 * atr:
            # London was bearish, NY faked bullish → go short
            if close < early_ny["high"].max():
                sigs.append((i, "SELL", max(1.5 * atr, early_ny["high"].max() - close)))

    return simulate(sigs, df, sym, trail_r=1.0, debounce=10)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 8: Power of 3 (AMD — Accumulation, Manipulation, Distribution)
# ══════════════════════════════════════════════════════════════════════════

def strat_power_of_3(sym, df, _):
    """AMD / Power of 3 — ICT concept.

    Daily candle structure:
    1. Accumulation: tight range early in session (Asian/early London)
    2. Manipulation: false breakout (sweep of range high or low)
    3. Distribution: real move in opposite direction

    We detect the manipulation phase and trade the distribution.
    """
    compute_indicators(df)
    sigs = []

    for i in range(50, len(df)):
        if not hasattr(df.index[i], 'hour'):
            continue
        hour = df.index[i].hour
        day = df.index[i].date()

        # Distribution phase: 9-12 UTC (late London, before NY)
        if hour < 9 or hour > 12:
            continue

        atr = df["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            continue

        # Accumulation range: 0-6 UTC (Asian session)
        accum = df.loc[(df.index.date == day) & (df.index.hour >= 0) & (df.index.hour < 7)]
        if len(accum) < 3:
            continue

        acc_high = accum["high"].max()
        acc_low = accum["low"].min()
        acc_range = acc_high - acc_low

        # Range must be tight (accumulation = tight range)
        if acc_range > 1.5 * atr or acc_range < 0.1 * atr:
            continue

        # Manipulation: 7-9 UTC bars swept above or below accumulation range
        manip = df.loc[(df.index.date == day) & (df.index.hour >= 7) & (df.index.hour < 9)]
        if len(manip) < 1:
            continue

        swept_high = manip["high"].max() > acc_high
        swept_low = manip["low"].min() < acc_low
        manip_close = manip["close"].iloc[-1]

        close = df["close"].iloc[i]
        sma50 = df["sma50"].iloc[i]
        if pd.isna(sma50):
            continue

        # Swept high but closed back inside → bearish manipulation → sell
        if swept_high and not swept_low and manip_close < acc_high:
            if close < acc_high and close < sma50:
                sl_dist = max(manip["high"].max() - close + 0.2 * atr, 1.0 * atr)
                sigs.append((i, "SELL", min(sl_dist, 2.5 * atr)))

        # Swept low but closed back inside → bullish manipulation → buy
        elif swept_low and not swept_high and manip_close > acc_low:
            if close > acc_low and close > sma50:
                sl_dist = max(close - manip["low"].min() + 0.2 * atr, 1.0 * atr)
                sigs.append((i, "BUY", min(sl_dist, 2.5 * atr)))

    return simulate(sigs, df, sym, trail_r=1.0, debounce=10)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 9: Pinbar Rejection at Key Levels
# ══════════════════════════════════════════════════════════════════════════

def strat_pinbar_key_level(sym, df, _):
    """Pinbar (hammer/shooting star) at S/R levels.

    A pinbar with a long wick (>2x body) at a 20-bar high/low
    signals institutional rejection. Very high win-rate pattern
    when combined with trend.
    """
    compute_indicators(df)
    sigs = []

    for i in range(50, len(df)):
        o = df["open"].iloc[i]
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        c = df["close"].iloc[i]
        atr = df["atr"].iloc[i]
        sma20 = df["sma20"].iloc[i]
        sma50 = df["sma50"].iloc[i]
        high_20 = df["high_20"].iloc[i]
        low_20 = df["low_20"].iloc[i]

        if any(pd.isna(v) for v in [o, h, l, c, atr, sma20, sma50, high_20, low_20]):
            continue
        if atr <= 0:
            continue

        body = abs(c - o)
        upper_wick = h - max(c, o)
        lower_wick = min(c, o) - l
        total_range = h - l

        if total_range < 0.3 * atr or body == 0:
            continue

        # Bullish pinbar: long lower wick, small body, near support
        if lower_wick > 2.0 * body and lower_wick > 0.6 * total_range:
            dist_to_support = (c - low_20) / atr
            if dist_to_support < 1.0 and sma20 > sma50:
                sigs.append((i, "BUY", max(lower_wick + 0.2 * atr, 1.0 * atr)))

        # Bearish pinbar: long upper wick, small body, near resistance
        elif upper_wick > 2.0 * body and upper_wick > 0.6 * total_range:
            dist_to_resist = (high_20 - c) / atr
            if dist_to_resist < 1.0 and sma20 < sma50:
                sigs.append((i, "SELL", max(upper_wick + 0.2 * atr, 1.0 * atr)))

    return simulate(sigs, df, sym, trail_r=1.2, debounce=4)


# ══════════════════════════════════════════════════════════════════════════
# PRO STRATEGY 10: Momentum Ignition (Volume Spike + Trend)
# ══════════════════════════════════════════════════════════════════════════

def strat_momentum_ignition(sym, df, _):
    """Momentum ignition — big volume spike + decisive close.

    When volume spikes >2x average AND the candle closes strongly
    in one direction AND trend agrees → ride the institutional flow.
    This is the "smart money just entered" signal.
    """
    compute_indicators(df)
    sigs = []

    if "volume" not in df.columns:
        return [], INITIAL, 0

    df["vol_sma20"] = df["volume"].rolling(20).mean()

    for i in range(50, len(df)):
        vol = df["volume"].iloc[i]
        vol_avg = df["vol_sma20"].iloc[i]
        c = df["close"].iloc[i]
        o = df["open"].iloc[i]
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        atr = df["atr"].iloc[i]
        sma20 = df["sma20"].iloc[i]
        sma50 = df["sma50"].iloc[i]
        rsi = df["rsi"].iloc[i]

        if any(pd.isna(v) for v in [vol, vol_avg, c, o, h, l, atr, sma20, sma50, rsi]):
            continue
        if vol_avg <= 0 or atr <= 0:
            continue

        vol_ratio = vol / vol_avg
        body = abs(c - o)
        total_range = h - l

        if total_range == 0:
            continue

        # Close strength: how much of the range is body (>70% = decisive)
        close_strength = body / total_range

        # Volume spike + decisive candle + trend agreement
        if vol_ratio > 2.0 and close_strength > 0.65:
            if c > o and c > sma20 and sma20 > sma50 and rsi < 75:
                sigs.append((i, "BUY", None))
            elif c < o and c < sma20 and sma20 < sma50 and rsi > 25:
                sigs.append((i, "SELL", None))

    return simulate(sigs, df, sym, atr_sl_mult=1.5, trail_r=1.0, debounce=4)


# ══════════════════════════════════════════════════════════════════════════
# REGISTRY & RUNNER
# ══════════════════════════════════════════════════════════════════════════

STRATEGIES = [
    ("M5 Scalp (baseline)",         strat_m5_scalp,           "5m",  "60d", True,
     "Your current live strategy — EMA9/21 + H1 trend gate"),
    ("London Kill Zone",            strat_london_killzone,     "1h",  "2y",  False,
     "Asian range breakout at London open (7-9 UTC)"),
    ("Liquidity Sweep Rev",         strat_liquidity_sweep,     "1h",  "2y",  False,
     "Fade the stop hunt: sweep past swing H/L then reverse"),
    ("Fair Value Gap Fill",         strat_fvg_fill,            "1h",  "2y",  False,
     "Enter on price returning to imbalance zones (FVG)"),
    ("Order Block Reaction",        strat_order_block,         "1h",  "2y",  False,
     "Trade at last opposing candle before impulse move"),
    ("VCP Breakout",                strat_vcp,                 "1h",  "2y",  False,
     "Volatility contraction → expansion breakout (Minervini)"),
    ("MTF Momentum Confluence",     strat_mtf_momentum,        "15m", "60d", True,
     "H1 trend + M15 RSI pullback entry (multi-timeframe)"),
    ("NY Reversal (Judas Swing)",   strat_ny_reversal,         "1h",  "2y",  False,
     "Fade early NY fake move that opposes London direction"),
    ("Power of 3 (AMD)",            strat_power_of_3,          "1h",  "2y",  False,
     "Accumulation → Manipulation → Distribution (ICT)"),
    ("Pinbar at Key Level",         strat_pinbar_key_level,    "1h",  "2y",  False,
     "Pinbar rejection at 20-bar S/R with trend filter"),
    ("Momentum Ignition",           strat_momentum_ignition,   "1h",  "2y",  False,
     "Volume spike >2x avg + decisive candle + trend"),
]

ASSETS = [
    ("NZDUSD=X", "NZD/USD"), ("EURUSD=X", "EUR/USD"),
    ("GBPUSD=X", "GBP/USD"), ("AUDUSD=X", "AUD/USD"),
    ("GC=F", "Gold"), ("BTC-USD", "BTC"),
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
    return m["mo"] * pfc * (1 - ddp) * sb


def run():
    print("=" * 95)
    print("PRO STRATEGY BACKTEST — 10 Institutional/Prop Strategies + Baseline")
    print("=" * 95)
    print(f"Capital: €{INITIAL:.0f} | Risk: {RISK_PCT}%/trade | Trailing SL at 1R")
    print()

    cache = {}
    results = []

    for sn, fn, iv, per, needh1, desc in STRATEGIES:
        print(f"\n{'─'*95}")
        print(f"  {sn}")
        print(f"  {desc}")
        print(f"  Timeframe: {iv} | Lookback: {per}")
        print()

        hdr = f"  {'Asset':<10s} | {'#':>5s} | {'T/mo':>5s} | {'WR%':>6s} | {'PF':>5s} | {'Return':>9s} | {'Mo/Ret':>9s} | {'MaxDD':>7s} | {'Sharpe':>6s} | {'Expect':>8s} | {'Final':>8s}"
        print(hdr)
        print(f"  {'-'*10}-+-{'-'*5}-+-{'-'*5}-+-{'-'*6}-+-{'-'*5}-+-{'-'*9}-+-{'-'*9}-+-{'-'*7}-+-{'-'*6}-+-{'-'*8}-+-{'-'*8}")

        for sym, aname in ASSETS:
            if iv == "5m" and sym in ("GC=F", "BTC-USD"):
                continue

            ck = f"{sym}_{iv}_{per}"
            if ck not in cache:
                cache[ck] = fetch(sym, per, iv)

            df = cache[ck].copy()
            if df.empty:
                print(f"  {aname:<10s} | {'nodata':>5s} |")
                continue

            h1 = None
            if needh1:
                hk = f"{sym}_1h_2y"
                if hk not in cache:
                    cache[hk] = fetch(sym, "2y", "1h")
                h1 = cache[hk].copy()

            try:
                tr, bal, mdd = fn(sym, df, h1)
            except Exception as e:
                print(f"  {aname:<10s} | ERROR: {e}")
                continue

            days = (df.index[-1] - df.index[0]).days
            months = max(days / 30, 0.5)
            m = metrics(tr, bal, mdd, months)

            if m is None:
                print(f"  {aname:<10s} | {len(tr):>5d} |  (too few trades)")
                continue

            results.append({"strat": sn, "asset": aname, "sym": sym, "m": m, "rank": rank(m)})

            print(f"  {aname:<10s} | {m['trades']:>5d} | {m['tmo']:>5.1f} | {m['wr']:>5.1f}% | {m['pf']:>5.2f} | {m['ret']:>+8.1f}% | {m['mo']:>+8.1f}% | {m['mdd']:>6.1f}% | {m['sharpe']:>6.2f} | €{m['exp']:>+6.3f} | €{m['bal']:>6.2f}")

    # ── RANKING ──────────────────────────────────────────────────────
    print(f"\n\n{'='*95}")
    print("TOP 20 OVERALL RANKING")
    print(f"{'='*95}")
    results.sort(key=lambda r: r["rank"], reverse=True)

    print(f"\n  {'#':>3s} | {'Strategy':<26s} | {'Asset':<10s} | {'Mo/Ret':>9s} | {'WR%':>6s} | {'PF':>5s} | {'MaxDD':>7s} | {'Sharpe':>6s} | {'Score':>8s}")
    print(f"  {'-'*3}-+-{'-'*26}-+-{'-'*10}-+-{'-'*9}-+-{'-'*6}-+-{'-'*5}-+-{'-'*7}-+-{'-'*6}-+-{'-'*8}")

    for i, r in enumerate(results[:20], 1):
        m = r["m"]
        tag = " ★" if "baseline" in r["strat"] and r["asset"] == "NZD/USD" else ""
        print(f"  {i:>3d} | {r['strat']:<26s} | {r['asset']:<10s} | {m['mo']:>+8.1f}% | {m['wr']:>5.1f}% | {m['pf']:>5.2f} | {m['mdd']:>6.1f}% | {m['sharpe']:>6.2f} | {r['rank']:>8.1f}{tag}")

    # ── BEST PER STRATEGY ────────────────────────────────────────────
    print(f"\n\n{'='*95}")
    print("BEST ASSET PER STRATEGY (sorted by composite score)")
    print(f"{'='*95}\n")

    best = {}
    for r in results:
        s = r["strat"]
        if s not in best or r["rank"] > best[s]["rank"]:
            best[s] = r

    for sn, r in sorted(best.items(), key=lambda x: x[1]["rank"], reverse=True):
        m = r["m"]
        tag = " ← LIVE" if "baseline" in sn else ""
        print(f"  {sn:<26s} → {r['asset']:<10s} | {m['mo']:>+.1f}%/mo | WR {m['wr']:.0f}% | PF {m['pf']:.2f} | DD {m['mdd']:.1f}% | Sharpe {m['sharpe']:.2f} | Score {r['rank']:.1f}{tag}")

    # ── KEY TAKEAWAYS ────────────────────────────────────────────────
    print(f"\n\n{'='*95}")
    print("VERDICT")
    print(f"{'='*95}\n")

    if results:
        top = results[0]
        baseline = next((r for r in results if "baseline" in r["strat"] and r["asset"] == "NZD/USD"), None)
        print(f"  #1 Strategy: {top['strat']} on {top['asset']}")
        print(f"     → {top['m']['mo']:+.1f}%/mo | WR {top['m']['wr']:.0f}% | PF {top['m']['pf']:.2f} | DD {top['m']['mdd']:.1f}%")
        if baseline:
            bm = baseline["m"]
            br = baseline["rank"]
            bl_rank = next(i for i, r in enumerate(results, 1) if r is baseline)
            print(f"\n  Your M5 Scalp ranks #{bl_rank}: {bm['mo']:+.1f}%/mo | WR {bm['wr']:.0f}% | PF {bm['pf']:.2f} | DD {bm['mdd']:.1f}%")
    print()


if __name__ == "__main__":
    run()
