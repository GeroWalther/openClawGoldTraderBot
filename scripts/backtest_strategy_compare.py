"""Multi-strategy backtester — compare M5 scalp vs creative alternatives.

Tests across multiple assets (FX, Gold, BTC, S&P) and timeframes (M5, M15, H1, D1).
Each strategy uses ATR-based stops, trailing SL at 1R, and realistic costs.

Usage: .venv/bin/python scripts/backtest_strategy_compare.py
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


# ── Cost model per asset (round-trip spread + slippage) ──────────────────
COSTS = {
    "NZDUSD=X": 0.00012,
    "EURUSD=X": 0.00010,
    "GBPUSD=X": 0.00014,
    "AUDUSD=X": 0.00012,
    "JPY=X":    0.015,      # USDJPY: ~1.5 pip
    "EURJPY=X": 0.020,
    "GC=F":     0.50,       # Gold: ~$0.50
    "BTC-USD":  30.0,       # BTC: ~$30
    "ES=F":     0.50,       # S&P futures: ~$0.50
}

INITIAL_BALANCE = 50.0
RISK_PCT = 4.0
MIN_SIZE_MAP = {
    "NZDUSD=X": 1000, "EURUSD=X": 1000, "GBPUSD=X": 1000,
    "AUDUSD=X": 1000, "JPY=X": 1000, "EURJPY=X": 1000,
    "GC=F": 1, "BTC-USD": 0.001, "ES=F": 1,
}
SIZE_ROUND_MAP = {
    "NZDUSD=X": 1000, "EURUSD=X": 1000, "GBPUSD=X": 1000,
    "AUDUSD=X": 1000, "JPY=X": 1000, "EURJPY=X": 1000,
    "GC=F": 1, "BTC-USD": 0.001, "ES=F": 1,
}


# ── Data fetching ────────────────────────────────────────────────────────

def fetch_data(symbol, period, interval):
    """Fetch and normalize OHLCV data."""
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    return df


# ── Generic trade simulator ──────────────────────────────────────────────

def simulate_trades(signals, df, symbol, atr_sl_mult=1.5, trail_at_r=1.0,
                    initial=INITIAL_BALANCE, risk_pct=RISK_PCT, debounce_bars=6):
    """Run trades from signal list through the price data.

    Args:
        signals: list of (bar_index, direction, sl_distance_override_or_None)
        df: OHLCV DataFrame with 'high', 'low', 'close', 'atr' columns
        symbol: for cost/size lookup
    """
    cost = COSTS.get(symbol, 0.00015)
    min_size = MIN_SIZE_MAP.get(symbol, 1)
    size_round = SIZE_ROUND_MAP.get(symbol, 1)

    balance = initial
    peak = initial
    max_dd = 0
    trades = []
    in_trade = False
    last_exit_bar = -debounce_bars - 1

    # Sort signals by bar index
    signals = sorted(signals, key=lambda s: s[0])
    sig_idx = 0

    for i in range(len(df)):
        if in_trade:
            bh, bl = df["high"].iloc[i], df["low"].iloc[i]
            if trade_dir == "BUY":
                if bl <= sl:
                    pnl = (sl - entry) * size - cost * size
                    balance += pnl
                    trades.append({"pnl": pnl, "dir": "BUY", "bar": i,
                                   "entry": entry, "exit": sl, "size": size})
                    in_trade = False
                    last_exit_bar = i
                else:
                    pnl_r = (bh - entry) / sl_dist
                    if pnl_r >= trail_at_r:
                        new_sl = bh - 0.5 * sl_dist
                        if new_sl > sl:
                            sl = new_sl
            else:
                if bh >= sl:
                    pnl = (entry - sl) * size - cost * size
                    balance += pnl
                    trades.append({"pnl": pnl, "dir": "SELL", "bar": i,
                                   "entry": entry, "exit": sl, "size": size})
                    in_trade = False
                    last_exit_bar = i
                else:
                    pnl_r = (entry - bl) / sl_dist
                    if pnl_r >= trail_at_r:
                        new_sl = bl + 0.5 * sl_dist
                        if new_sl < sl:
                            sl = new_sl

            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # Check for new signals at this bar
        if not in_trade:
            while sig_idx < len(signals) and signals[sig_idx][0] <= i:
                sig_bar, sig_dir, sig_sl_override = signals[sig_idx]
                sig_idx += 1

                if sig_bar != i:
                    continue
                if (i - last_exit_bar) < debounce_bars:
                    continue

                atr = df["atr"].iloc[i]
                if pd.isna(atr) or atr <= 0:
                    continue

                entry = df["close"].iloc[i]
                trade_dir = sig_dir
                sl_dist = sig_sl_override if sig_sl_override else max(atr_sl_mult * atr, atr * 0.5)

                if sig_dir == "BUY":
                    sl = entry - sl_dist
                else:
                    sl = entry + sl_dist

                risk_amt = balance * risk_pct / 100
                size = risk_amt / sl_dist if sl_dist > 0 else 0

                # Round size
                if size_round >= 1:
                    size = max(round(size / size_round) * size_round, min_size)
                else:
                    size = max(round(size / size_round) * size_round, min_size)

                actual_risk = size * sl_dist
                if actual_risk > balance * 0.5 or size <= 0:
                    continue

                in_trade = True
                break

    return trades, balance, max_dd


# ══════════════════════════════════════════════════════════════════════════
# STRATEGIES
# ══════════════════════════════════════════════════════════════════════════

def strategy_m5_scalp(symbol, m5_df, h1_df):
    """Current M5 Scalp — EMA9/21 cross + H1 trend gate + RSI7 + BB."""
    engine = M5ScalpScoringEngine()
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)
    h1_index = h1_df.index

    signals = []
    last_bar = -13
    last_dir = None

    for i in range(50, len(m5_df)):
        m5_time = m5_df.index[i]
        h1_mask = h1_index <= m5_time
        if not h1_mask.any():
            continue
        h1_row = h1_df.loc[h1_index[h1_mask][-1]]
        m5_tail = m5_df.iloc[max(0, i-5):i+1]
        result = engine.score(h1_row, m5_tail, bar_time=m5_time)

        if result["direction"] is None:
            continue
        d = result["direction"]
        if (i - last_bar) < 12 and last_dir == d:
            continue

        atr = m5_df["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            continue
        sl_dist = max(1.0 * atr, 0.0020 if "JPY" not in symbol else 0.20)
        signals.append((i, d, sl_dist))
        last_bar = i
        last_dir = d

    return simulate_trades(signals, m5_df, symbol, debounce_bars=12)


def strategy_ema_ribbon(symbol, m5_df, h1_df):
    """EMA Ribbon M5 — 5/8/13/21 EMA alignment for strong trends.

    Enter when all 4 EMAs are stacked in order (5>8>13>21 for long).
    Exit on EMA5 crossing below EMA13 (or above for shorts).
    More selective than single EMA cross.
    """
    compute_indicators(m5_df)
    close = m5_df["close"]
    m5_df["ema5"] = close.ewm(span=5, adjust=False).mean()
    m5_df["ema8"] = close.ewm(span=8, adjust=False).mean()
    m5_df["ema13"] = close.ewm(span=13, adjust=False).mean()
    m5_df["ema21_r"] = close.ewm(span=21, adjust=False).mean()

    signals = []
    prev_stacked = 0  # 0=none, 1=bull, -1=bear

    for i in range(50, len(m5_df)):
        e5 = m5_df["ema5"].iloc[i]
        e8 = m5_df["ema8"].iloc[i]
        e13 = m5_df["ema13"].iloc[i]
        e21 = m5_df["ema21_r"].iloc[i]

        if any(pd.isna(v) for v in [e5, e8, e13, e21]):
            continue

        bull_stack = e5 > e8 > e13 > e21
        bear_stack = e5 < e8 < e13 < e21

        # Fresh stack formation
        if bull_stack and prev_stacked != 1:
            signals.append((i, "BUY", None))
            prev_stacked = 1
        elif bear_stack and prev_stacked != -1:
            signals.append((i, "SELL", None))
            prev_stacked = -1
        elif not bull_stack and not bear_stack:
            prev_stacked = 0

    return simulate_trades(signals, m5_df, symbol, atr_sl_mult=1.2, debounce_bars=10)


def strategy_rsi_mean_reversion(symbol, df, _unused):
    """RSI Mean Reversion H1 — buy oversold, sell overbought with BB confirmation.

    Enter long when RSI<25 AND price near lower BB.
    Enter short when RSI>75 AND price near upper BB.
    Classic mean-reversion on hourly.
    """
    compute_indicators(df)

    signals = []
    for i in range(50, len(df)):
        rsi = df["rsi"].iloc[i]
        close = df["close"].iloc[i]
        bb_lower = df["bb_lower"].iloc[i]
        bb_upper = df["bb_upper"].iloc[i]
        bb_range = bb_upper - bb_lower if not pd.isna(bb_upper) and not pd.isna(bb_lower) else 0

        if pd.isna(rsi) or bb_range <= 0:
            continue

        bb_pos = (close - bb_lower) / bb_range

        # Oversold + near lower BB
        if rsi < 25 and bb_pos < 0.15:
            signals.append((i, "BUY", None))
        elif rsi < 30 and bb_pos < 0.05:
            signals.append((i, "BUY", None))
        # Overbought + near upper BB
        elif rsi > 75 and bb_pos > 0.85:
            signals.append((i, "SELL", None))
        elif rsi > 70 and bb_pos > 0.95:
            signals.append((i, "SELL", None))

    return simulate_trades(signals, df, symbol, atr_sl_mult=2.0, trail_at_r=1.5, debounce_bars=4)


def strategy_bb_squeeze_breakout(symbol, df, _unused):
    """Bollinger Band Squeeze Breakout M15/H1 — enter on expansion after squeeze.

    Wait for BB bandwidth < 0.012 (tight squeeze), then enter on breakout
    above upper BB or below lower BB with volume confirmation.
    """
    compute_indicators(df)

    signals = []
    squeeze_count = 0

    for i in range(50, len(df)):
        bw = df["bb_bandwidth"].iloc[i]
        close = df["close"].iloc[i]
        bb_upper = df["bb_upper"].iloc[i]
        bb_lower = df["bb_lower"].iloc[i]
        volume = df["volume"].iloc[i] if "volume" in df.columns else 0
        vol_avg = df["volume"].iloc[max(0,i-20):i].mean() if "volume" in df.columns else 0

        if pd.isna(bw):
            continue

        if bw < 0.012:
            squeeze_count += 1
        else:
            if squeeze_count >= 5:  # Had a squeeze
                if not pd.isna(close) and not pd.isna(bb_upper) and not pd.isna(bb_lower):
                    vol_ok = pd.isna(vol_avg) or vol_avg == 0 or volume > vol_avg * 1.2
                    if close > bb_upper and vol_ok:
                        signals.append((i, "BUY", None))
                    elif close < bb_lower and vol_ok:
                        signals.append((i, "SELL", None))
            squeeze_count = 0

    return simulate_trades(signals, df, symbol, atr_sl_mult=1.5, trail_at_r=1.0, debounce_bars=4)


def strategy_macd_divergence(symbol, df, _unused):
    """MACD Histogram Reversal H1 — enter on histogram flip with trend.

    When MACD histogram flips from negative to positive (or vice versa)
    AND SMA20 confirms trend direction = enter.
    """
    compute_indicators(df)

    signals = []
    for i in range(51, len(df)):
        hist = df["macd_hist"].iloc[i]
        prev_hist = df["macd_hist"].iloc[i-1]
        close = df["close"].iloc[i]
        sma20 = df["sma20"].iloc[i]
        sma50 = df["sma50"].iloc[i]

        if any(pd.isna(v) for v in [hist, prev_hist, close, sma20, sma50]):
            continue

        # Histogram flip positive + price above SMA20 + SMA20 > SMA50
        if prev_hist < 0 and hist > 0 and close > sma20 and sma20 > sma50:
            signals.append((i, "BUY", None))
        # Histogram flip negative + price below SMA20 + SMA20 < SMA50
        elif prev_hist > 0 and hist < 0 and close < sma20 and sma20 < sma50:
            signals.append((i, "SELL", None))

    return simulate_trades(signals, df, symbol, atr_sl_mult=1.5, trail_at_r=1.0, debounce_bars=3)


def strategy_keltner_breakout(symbol, df, _unused):
    """Keltner Channel Breakout H1 — ATR-based channel breakout.

    Keltner = EMA20 ± 2*ATR. Enter on close outside channel with momentum.
    More adaptive than Bollinger (uses ATR not stddev).
    """
    compute_indicators(df)
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["kelt_upper"] = df["ema20"] + 2.0 * df["atr"]
    df["kelt_lower"] = df["ema20"] - 2.0 * df["atr"]

    signals = []
    for i in range(50, len(df)):
        close = df["close"].iloc[i]
        ku = df["kelt_upper"].iloc[i]
        kl = df["kelt_lower"].iloc[i]
        rsi = df["rsi"].iloc[i]

        if any(pd.isna(v) for v in [close, ku, kl, rsi]):
            continue

        # Breakout above Keltner + RSI not overbought
        if close > ku and rsi < 75:
            signals.append((i, "BUY", None))
        # Breakdown below Keltner + RSI not oversold
        elif close < kl and rsi > 25:
            signals.append((i, "SELL", None))

    return simulate_trades(signals, df, symbol, atr_sl_mult=1.5, trail_at_r=1.0, debounce_bars=3)


def strategy_triple_sma_trend(symbol, df, _unused):
    """Triple SMA Trend D1 — 20/50/200 alignment with pullback entry.

    Enter when all SMAs aligned AND price pulls back to SMA20 zone.
    Classic swing setup — fewer trades but higher quality.
    """
    compute_indicators(df)

    signals = []
    for i in range(200, len(df)):
        close = df["close"].iloc[i]
        sma20 = df["sma20"].iloc[i]
        sma50 = df["sma50"].iloc[i]
        sma200 = df["sma200"].iloc[i]
        rsi = df["rsi"].iloc[i]
        atr = df["atr"].iloc[i]

        if any(pd.isna(v) for v in [close, sma20, sma50, sma200, rsi, atr]):
            continue

        dist_to_sma20 = abs(close - sma20) / atr if atr > 0 else 99

        # Bull aligned + pullback to SMA20
        if sma20 > sma50 > sma200 and dist_to_sma20 < 0.5 and close > sma20 and rsi < 60:
            signals.append((i, "BUY", None))
        # Bear aligned + pullback to SMA20
        elif sma20 < sma50 < sma200 and dist_to_sma20 < 0.5 and close < sma20 and rsi > 40:
            signals.append((i, "SELL", None))

    return simulate_trades(signals, df, symbol, atr_sl_mult=2.0, trail_at_r=1.5, debounce_bars=3)


def strategy_inside_bar_breakout(symbol, df, _unused):
    """Inside Bar Breakout H1 — enter on breakout of inside bar range.

    An inside bar has a lower high and higher low than the previous bar.
    Trade the breakout direction with SMA50 trend filter.
    """
    compute_indicators(df)

    signals = []
    for i in range(51, len(df)):
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        ph = df["high"].iloc[i-1]
        pl = df["low"].iloc[i-1]
        close = df["close"].iloc[i]
        sma50 = df["sma50"].iloc[i]

        if any(pd.isna(v) for v in [h, l, ph, pl, close, sma50]):
            continue

        # Previous bar was an inside bar (i-1 range inside i-2)
        if i < 2:
            continue
        pph = df["high"].iloc[i-2]
        ppl = df["low"].iloc[i-2]
        if pd.isna(pph) or pd.isna(ppl):
            continue

        is_inside = ph < pph and pl > ppl
        if not is_inside:
            continue

        # Current bar breaks out of the inside bar range
        if close > ph and close > sma50:
            signals.append((i, "BUY", None))
        elif close < pl and close < sma50:
            signals.append((i, "SELL", None))

    return simulate_trades(signals, df, symbol, atr_sl_mult=1.2, trail_at_r=1.0, debounce_bars=3)


def strategy_vwap_bounce(symbol, df, _unused):
    """VWAP-like Bounce M15/H1 — mean reversion to session VWAP proxy.

    Uses cumulative VWAP (volume-weighted average price) as mean.
    Enter when price deviates >1.5 ATR from VWAP then reverts.
    """
    compute_indicators(df)

    # Compute rolling VWAP proxy (resets each session/day)
    df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
    # Use a rolling 20-bar VWAP instead for non-resetting version
    df["vwap20"] = (df["close"] * df["volume"]).rolling(20).sum() / df["volume"].rolling(20).sum()

    signals = []
    for i in range(50, len(df)):
        close = df["close"].iloc[i]
        vwap = df["vwap20"].iloc[i]
        atr = df["atr"].iloc[i]
        rsi = df["rsi"].iloc[i]

        if any(pd.isna(v) for v in [close, vwap, atr, rsi]) or atr == 0:
            continue

        deviation = (close - vwap) / atr

        # Price dropped well below VWAP → bounce long
        if deviation < -1.5 and rsi < 35:
            signals.append((i, "BUY", None))
        # Price pushed well above VWAP → short
        elif deviation > 1.5 and rsi > 65:
            signals.append((i, "SELL", None))

    return simulate_trades(signals, df, symbol, atr_sl_mult=1.5, trail_at_r=1.0, debounce_bars=4)


def strategy_donchian_breakout(symbol, df, _unused):
    """Donchian Channel Breakout H1 — turtle-style 20-bar breakout.

    Enter on new 20-bar high/low. Classic trend-following.
    Filter with SMA50 to only trade with the trend.
    """
    compute_indicators(df)

    signals = []
    for i in range(50, len(df)):
        close = df["close"].iloc[i]
        high_20 = df["high_20"].iloc[i]
        low_20 = df["low_20"].iloc[i]
        sma50 = df["sma50"].iloc[i]
        prev_close = df["close"].iloc[i-1]
        prev_high20 = df["high_20"].iloc[i-1]
        prev_low20 = df["low_20"].iloc[i-1]

        if any(pd.isna(v) for v in [close, high_20, low_20, sma50, prev_close, prev_high20, prev_low20]):
            continue

        # Fresh breakout above 20-bar high + above SMA50
        if close >= high_20 and prev_close < prev_high20 and close > sma50:
            signals.append((i, "BUY", None))
        # Fresh breakdown below 20-bar low + below SMA50
        elif close <= low_20 and prev_close > prev_low20 and close < sma50:
            signals.append((i, "SELL", None))

    return simulate_trades(signals, df, symbol, atr_sl_mult=2.0, trail_at_r=1.0, debounce_bars=5)


def strategy_engulfing_candle(symbol, df, _unused):
    """Engulfing Candle Pattern H1 — classic reversal pattern with trend.

    Bullish engulfing: current candle body fully engulfs previous bearish candle.
    Bearish engulfing: current candle body fully engulfs previous bullish candle.
    Filter: must agree with SMA20/50 trend direction.
    """
    compute_indicators(df)

    signals = []
    for i in range(51, len(df)):
        o = df["open"].iloc[i]
        c = df["close"].iloc[i]
        po = df["open"].iloc[i-1]
        pc = df["close"].iloc[i-1]
        sma20 = df["sma20"].iloc[i]
        sma50 = df["sma50"].iloc[i]

        if any(pd.isna(v) for v in [o, c, po, pc, sma20, sma50]):
            continue

        # Bullish engulfing: prev bearish, current bullish, current body engulfs prev
        prev_bearish = pc < po
        curr_bullish = c > o
        bull_engulf = prev_bearish and curr_bullish and c > po and o < pc

        # Bearish engulfing: prev bullish, current bearish, current body engulfs prev
        prev_bullish = pc > po
        curr_bearish = c < o
        bear_engulf = prev_bullish and curr_bearish and c < po and o > pc

        if bull_engulf and sma20 > sma50:
            signals.append((i, "BUY", None))
        elif bear_engulf and sma20 < sma50:
            signals.append((i, "SELL", None))

    return simulate_trades(signals, df, symbol, atr_sl_mult=1.5, trail_at_r=1.0, debounce_bars=3)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY REGISTRY
# ══════════════════════════════════════════════════════════════════════════

# (name, function, data_interval, data_period, needs_h1, description)
STRATEGIES = [
    ("M5 Scalp (current)",      strategy_m5_scalp,            "5m",  "60d", True,
     "EMA9/21 cross + H1 trend + RSI7 + BB on M5"),
    ("EMA Ribbon M5",           strategy_ema_ribbon,           "5m",  "60d", True,
     "5/8/13/21 EMA stack alignment on M5"),
    ("RSI MeanRev H1",          strategy_rsi_mean_reversion,   "1h",  "2y",  False,
     "RSI<25 + lower BB bounce / RSI>75 + upper BB reject"),
    ("BB Squeeze Breakout H1",  strategy_bb_squeeze_breakout,  "1h",  "2y",  False,
     "Enter on BB expansion after tight squeeze"),
    ("MACD Hist Flip H1",       strategy_macd_divergence,      "1h",  "2y",  False,
     "MACD histogram flip + SMA20/50 trend filter"),
    ("Keltner Breakout H1",     strategy_keltner_breakout,     "1h",  "2y",  False,
     "EMA20 ± 2*ATR channel breakout"),
    ("Triple SMA Pullback D1",  strategy_triple_sma_trend,     "1d",  "5y",  False,
     "20/50/200 aligned + pullback to SMA20"),
    ("Inside Bar Breakout H1",  strategy_inside_bar_breakout,  "1h",  "2y",  False,
     "Inside bar pattern breakout + SMA50 filter"),
    ("VWAP Bounce H1",          strategy_vwap_bounce,          "1h",  "2y",  False,
     "Mean reversion to 20-bar VWAP proxy"),
    ("Donchian Breakout H1",    strategy_donchian_breakout,    "1h",  "2y",  False,
     "20-bar high/low breakout (turtle) + SMA50 filter"),
    ("Engulfing Candle H1",     strategy_engulfing_candle,     "1h",  "2y",  False,
     "Engulfing candle reversal + trend agreement"),
]

# Assets to test
ASSETS = [
    ("NZDUSD=X", "NZD/USD"),
    ("EURUSD=X", "EUR/USD"),
    ("GBPUSD=X", "GBP/USD"),
    ("AUDUSD=X", "AUD/USD"),
    ("GC=F",     "Gold"),
    ("BTC-USD",  "BTC"),
]


def compute_metrics(trades, balance, initial, max_dd, months):
    """Compute standard metrics from trade list."""
    if not trades:
        return None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    ret = (balance - initial) / initial * 100
    monthly = ret / months if months > 0 else 0
    wr = len(wins) / len(trades) * 100
    pf = w_pnl / l_pnl

    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([abs(t["pnl"]) for t in losses]) if losses else 0.001
    expectancy = (wr/100 * avg_win) - ((1-wr/100) * avg_loss)

    # Sharpe-like ratio (using trade PnLs)
    pnls = [t["pnl"] for t in trades]
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls))
    else:
        sharpe = 0

    return {
        "trades": len(trades),
        "trades_mo": len(trades) / months if months > 0 else 0,
        "wr": wr,
        "pf": pf,
        "ret": ret,
        "monthly": monthly,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "expectancy": expectancy,
        "balance": balance,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


def rank_score(m):
    """Composite ranking score: weights profitability, risk-adjusted return, and consistency."""
    if m is None:
        return -999
    # Penalize strategies with < 5 trades
    if m["trades"] < 5:
        return -999
    # Composite: monthly return * profit factor * (1 - drawdown_penalty) * sharpe_bonus
    dd_penalty = min(m["max_dd"] / 100, 0.8)
    sharpe_bonus = max(0.5, min(2.0, 1.0 + m["sharpe"] * 0.1))
    pf_cap = min(m["pf"], 5.0)  # Cap PF to avoid outlier bias
    return m["monthly"] * pf_cap * (1 - dd_penalty) * sharpe_bonus


def run():
    print("=" * 90)
    print("MULTI-STRATEGY BACKTEST COMPARISON")
    print("=" * 90)
    print(f"Initial balance: €{INITIAL_BALANCE:.0f} | Risk: {RISK_PCT}% per trade")
    print()

    # Data cache
    data_cache = {}
    all_results = []

    for strat_name, strat_fn, interval, period, needs_h1, desc in STRATEGIES:
        print(f"\n{'─'*90}")
        print(f"Strategy: {strat_name}")
        print(f"  {desc}")
        print(f"  Timeframe: {interval} | Period: {period}")
        print()

        header = f"  {'Asset':<10s} | {'Trades':>6s} | {'T/mo':>5s} | {'WR%':>6s} | {'PF':>5s} | {'Return':>9s} | {'Mo/Ret':>9s} | {'MaxDD':>7s} | {'Sharpe':>6s} | {'Final':>8s}"
        print(header)
        print(f"  {'-'*10}-+-{'-'*6}-+-{'-'*5}-+-{'-'*6}-+-{'-'*5}-+-{'-'*9}-+-{'-'*9}-+-{'-'*7}-+-{'-'*6}-+-{'-'*8}")

        for symbol, asset_name in ASSETS:
            # Skip M5 strategies on non-FX (yfinance M5 data is limited)
            if interval == "5m" and symbol in ("GC=F", "BTC-USD", "ES=F"):
                continue

            cache_key = f"{symbol}_{interval}_{period}"
            if cache_key not in data_cache:
                data_cache[cache_key] = fetch_data(symbol, period, interval)

            df = data_cache[cache_key].copy()
            if df.empty:
                print(f"  {asset_name:<10s} | {'no data':>6s} |")
                continue

            h1_df = None
            if needs_h1:
                h1_key = f"{symbol}_1h_2y"
                if h1_key not in data_cache:
                    data_cache[h1_key] = fetch_data(symbol, "2y", "1h")
                h1_df = data_cache[h1_key].copy()

            try:
                trades, balance, max_dd = strat_fn(symbol, df, h1_df)
            except Exception as e:
                print(f"  {asset_name:<10s} | ERROR: {e}")
                continue

            days = (df.index[-1] - df.index[0]).days
            months = max(days / 30, 0.5)
            m = compute_metrics(trades, balance, INITIAL_BALANCE, max_dd, months)

            if m is None:
                print(f"  {asset_name:<10s} | {'0':>6s} |")
                continue

            all_results.append({
                "strategy": strat_name,
                "asset": asset_name,
                "symbol": symbol,
                "metrics": m,
                "rank": rank_score(m),
            })

            print(f"  {asset_name:<10s} | {m['trades']:>6d} | {m['trades_mo']:>5.1f} | {m['wr']:>5.1f}% | {m['pf']:>5.2f} | {m['ret']:>+8.1f}% | {m['monthly']:>+8.1f}% | {m['max_dd']:>6.1f}% | {m['sharpe']:>6.2f} | €{m['balance']:>6.2f}")

    # ── RANKING ──────────────────────────────────────────────────────────
    print(f"\n\n{'='*90}")
    print("OVERALL RANKING (by composite score: monthly return × profit factor × risk-adjusted)")
    print(f"{'='*90}")

    all_results.sort(key=lambda r: r["rank"], reverse=True)

    print(f"\n  {'#':>3s} | {'Strategy':<25s} | {'Asset':<10s} | {'Mo/Ret':>9s} | {'WR%':>6s} | {'PF':>5s} | {'MaxDD':>7s} | {'Sharpe':>6s} | {'Score':>8s}")
    print(f"  {'-'*3}-+-{'-'*25}-+-{'-'*10}-+-{'-'*9}-+-{'-'*6}-+-{'-'*5}-+-{'-'*7}-+-{'-'*6}-+-{'-'*8}")

    for rank, r in enumerate(all_results[:20], 1):
        m = r["metrics"]
        marker = " ★" if r["strategy"] == "M5 Scalp (current)" and r["asset"] == "NZD/USD" else ""
        print(f"  {rank:>3d} | {r['strategy']:<25s} | {r['asset']:<10s} | {m['monthly']:>+8.1f}% | {m['wr']:>5.1f}% | {m['pf']:>5.2f} | {m['max_dd']:>6.1f}% | {m['sharpe']:>6.2f} | {r['rank']:>8.1f}{marker}")

    # ── BEST per strategy ────────────────────────────────────────────────
    print(f"\n\n{'='*90}")
    print("BEST ASSET PER STRATEGY")
    print(f"{'='*90}\n")

    strat_best = {}
    for r in all_results:
        s = r["strategy"]
        if s not in strat_best or r["rank"] > strat_best[s]["rank"]:
            strat_best[s] = r

    for s_name, r in sorted(strat_best.items(), key=lambda x: x[1]["rank"], reverse=True):
        m = r["metrics"]
        print(f"  {s_name:<25s} → {r['asset']:<10s} | {m['monthly']:>+.1f}%/mo | WR {m['wr']:.0f}% | PF {m['pf']:.2f} | DD {m['max_dd']:.1f}% | Score {r['rank']:.1f}")

    print(f"\n{'='*90}")
    print("DONE")


if __name__ == "__main__":
    run()
