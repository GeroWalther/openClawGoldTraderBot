"""Sensei V3 — M5/M15/H1 aggressive short-timeframe backtesting.

Goal: 80%+ return per month.

Strategies tested:
A) Original Sensei: consolidation + double bottom/top + SMA20 cross
B) Simplified: consolidation breakout only (drop double bottom requirement)
C) Momentum: RSI oversold/overbought + SMA cross after squeeze
D) Mean reversion: Bollinger band touch + RSI divergence in consolidation

All with LONG + SHORT.
"""

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def compute_bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width_pct = (upper - lower) / mid * 100
    return mid, upper, lower, width_pct


# ──────────────────────────────────────────────────────────────
# Strategy A: Sensei (consolidation + double bottom/top + SMA20 cross)
# ──────────────────────────────────────────────────────────────
def strategy_sensei(df, conv_thresh=12, conv_bars=10, db_tol=8, db_lb=3, db_min=3, db_max=60):
    df = df.copy()
    close, low, high = df["Close"].values, df["Low"].values, df["High"].values

    df["sma5"] = df["Close"].rolling(5).mean()
    df["sma10"] = df["Close"].rolling(10).mean()
    df["sma20"] = df["Close"].rolling(20).mean()
    df["sma50"] = df["Close"].rolling(50).mean()
    df["atr"] = compute_atr(df, 14)

    ma_df = df[["sma5", "sma10", "sma20", "sma50"]]
    spread = (ma_df.max(axis=1) - ma_df.min(axis=1)) / df["Close"] * 100
    df["is_converged"] = spread <= conv_thresh

    conv_count = np.zeros(len(df))
    for i in range(1, len(df)):
        conv_count[i] = conv_count[i-1] + 1 if df["is_converged"].iloc[i] else 0
    df["conv_count"] = conv_count
    df["is_consolidating"] = conv_count >= conv_bars

    # Pivot lows & highs
    df["pivot_low"] = np.nan
    df["pivot_high"] = np.nan
    for i in range(db_lb, len(df) - db_lb):
        is_low = all(low[i] <= low[i-j] and low[i] <= low[i+j] for j in range(1, db_lb+1))
        is_high = all(high[i] >= high[i-j] and high[i] >= high[i+j] for j in range(1, db_lb+1))
        if is_low:
            df.iloc[i, df.columns.get_loc("pivot_low")] = low[i]
        if is_high:
            df.iloc[i, df.columns.get_loc("pivot_high")] = high[i]

    # Double bottom (W) and double top (M)
    w_active = np.zeros(len(df), dtype=bool)
    m_active = np.zeros(len(df), dtype=bool)

    bot1_p, bot1_i = np.nan, 0
    top1_p, top1_i = np.nan, 0
    for i in range(len(df)):
        if not np.isnan(df["pivot_low"].iloc[i]):
            p = df["pivot_low"].iloc[i]
            if not np.isnan(bot1_p):
                d = i - bot1_i
                pct = abs(p - bot1_p) / bot1_p * 100
                consol = conv_count[i] >= conv_bars * 0.5
                if pct <= db_tol and db_min <= d <= db_max and p > bot1_p and consol:
                    for j in range(i, min(i + db_max, len(df))):
                        w_active[j] = True
            bot1_p, bot1_i = p, i

        if not np.isnan(df["pivot_high"].iloc[i]):
            p = df["pivot_high"].iloc[i]
            if not np.isnan(top1_p):
                d = i - top1_i
                pct = abs(p - top1_p) / top1_p * 100
                consol = conv_count[i] >= conv_bars * 0.5
                if pct <= db_tol and db_min <= d <= db_max and p < top1_p and consol:
                    for j in range(i, min(i + db_max, len(df))):
                        m_active[j] = True
            top1_p, top1_i = p, i

    cross_above = (df["Close"] > df["sma20"]) & (df["Close"].shift(1) <= df["sma20"].shift(1))
    cross_below = (df["Close"] < df["sma20"]) & (df["Close"].shift(1) >= df["sma20"].shift(1))

    df["signal_long"] = False
    df["signal_short"] = False
    for i in range(len(df)):
        consol = df["is_consolidating"].iloc[i] or (i > 0 and df["is_consolidating"].iloc[i-1])
        if consol and w_active[i] and cross_above.iloc[i]:
            df.iloc[i, df.columns.get_loc("signal_long")] = True
        if consol and m_active[i] and cross_below.iloc[i]:
            df.iloc[i, df.columns.get_loc("signal_short")] = True
    return df


# ──────────────────────────────────────────────────────────────
# Strategy B: Consolidation breakout (simplified — no double bottom needed)
# ──────────────────────────────────────────────────────────────
def strategy_breakout(df, conv_thresh=10, conv_bars=8, ema_fast=9, ema_slow=21):
    df = df.copy()
    df["ema_fast"] = df["Close"].ewm(span=ema_fast).mean()
    df["ema_slow"] = df["Close"].ewm(span=ema_slow).mean()
    df["sma20"] = df["Close"].rolling(20).mean()
    df["sma50"] = df["Close"].rolling(50).mean()
    df["atr"] = compute_atr(df, 14)

    ma_df = df[["ema_fast", "ema_slow", "sma20", "sma50"]]
    spread = (ma_df.max(axis=1) - ma_df.min(axis=1)) / df["Close"] * 100
    df["is_converged"] = spread <= conv_thresh

    conv_count = np.zeros(len(df))
    for i in range(1, len(df)):
        conv_count[i] = conv_count[i-1] + 1 if df["is_converged"].iloc[i] else 0
    df["conv_count"] = conv_count

    # Signal: was consolidating, EMA cross breaks out
    was_consol = pd.Series(conv_count >= conv_bars, index=df.index)
    # Allow signal if consolidation ended within last 3 bars
    recent_consol = was_consol | was_consol.shift(1).fillna(False) | was_consol.shift(2).fillna(False)

    ema_cross_up = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    ema_cross_down = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))

    df["signal_long"] = recent_consol & ema_cross_up
    df["signal_short"] = recent_consol & ema_cross_down
    return df


# ──────────────────────────────────────────────────────────────
# Strategy C: RSI momentum after squeeze
# ──────────────────────────────────────────────────────────────
def strategy_rsi_squeeze(df, bb_period=20, bb_squeeze_pct=2.0, rsi_period=14,
                          rsi_ob=65, rsi_os=35, conv_bars=5):
    df = df.copy()
    df["atr"] = compute_atr(df, 14)
    df["rsi"] = compute_rsi(df["Close"], rsi_period)
    _, bb_upper, bb_lower, bb_width = compute_bollinger(df["Close"], bb_period)
    df["bb_upper"] = bb_upper
    df["bb_lower"] = bb_lower
    df["bb_width"] = bb_width

    # Squeeze: BB width below threshold
    df["is_squeeze"] = df["bb_width"] <= bb_squeeze_pct

    squeeze_count = np.zeros(len(df))
    for i in range(1, len(df)):
        squeeze_count[i] = squeeze_count[i-1] + 1 if df["is_squeeze"].iloc[i] else 0

    was_squeezed = pd.Series(squeeze_count >= conv_bars, index=df.index)
    recent_squeeze = was_squeezed | was_squeezed.shift(1).fillna(False) | was_squeezed.shift(2).fillna(False)

    # Long: squeeze ended + RSI crosses above OS level + close > BB mid
    bb_mid = (bb_upper + bb_lower) / 2
    df["signal_long"] = recent_squeeze & (df["rsi"] > rsi_os) & (df["rsi"].shift(1) <= rsi_os)
    df["signal_short"] = recent_squeeze & (df["rsi"] < rsi_ob) & (df["rsi"].shift(1) >= rsi_ob)
    return df


# ──────────────────────────────────────────────────────────────
# Strategy D: Bollinger bounce in consolidation
# ──────────────────────────────────────────────────────────────
def strategy_bb_bounce(df, bb_period=20, conv_thresh=8, conv_bars=6, rsi_period=14):
    df = df.copy()
    df["atr"] = compute_atr(df, 14)
    df["rsi"] = compute_rsi(df["Close"], rsi_period)
    df["sma20"] = df["Close"].rolling(20).mean()
    df["sma50"] = df["Close"].rolling(50).mean()
    _, bb_upper, bb_lower, bb_width = compute_bollinger(df["Close"], bb_period)
    df["bb_upper"] = bb_upper
    df["bb_lower"] = bb_lower

    ema9 = df["Close"].ewm(span=9).mean()
    ema21 = df["Close"].ewm(span=21).mean()
    ma_df = pd.DataFrame({"a": ema9, "b": ema21, "c": df["sma20"], "d": df["sma50"]})
    spread = (ma_df.max(axis=1) - ma_df.min(axis=1)) / df["Close"] * 100
    df["is_converged"] = spread <= conv_thresh

    conv_count = np.zeros(len(df))
    for i in range(1, len(df)):
        conv_count[i] = conv_count[i-1] + 1 if df["is_converged"].iloc[i] else 0

    consol = pd.Series(conv_count >= conv_bars, index=df.index)

    # Long: price touches lower BB in consolidation + RSI < 40
    df["signal_long"] = consol & (df["Low"] <= bb_lower) & (df["Close"] > bb_lower) & (df["rsi"] < 40)
    # Short: price touches upper BB in consolidation + RSI > 60
    df["signal_short"] = consol & (df["High"] >= bb_upper) & (df["Close"] < bb_upper) & (df["rsi"] > 60)
    return df


# ──────────────────────────────────────────────────────────────
# Backtester
# ──────────────────────────────────────────────────────────────
def backtest(df, risk_pct=5.0, sl_mult=1.0, tp_mult=2.0, initial=10000.0, max_concurrent=1):
    balance = initial
    peak = initial
    max_dd = 0
    trades = []
    in_trade = False

    for i in range(len(df)):
        if in_trade:
            if trade_dir == "LONG":
                if df["Low"].iloc[i] <= sl:
                    pnl = (sl - entry) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "LONG"})
                    in_trade = False
                elif df["High"].iloc[i] >= tp:
                    pnl = (tp - entry) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "LONG"})
                    in_trade = False
            else:
                if df["High"].iloc[i] >= sl:
                    pnl = (entry - sl) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "SHORT"})
                    in_trade = False
                elif df["Low"].iloc[i] <= tp:
                    pnl = (entry - tp) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "SHORT"})
                    in_trade = False

            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        if not in_trade:
            atr = df["atr"].iloc[i] if not np.isnan(df["atr"].iloc[i]) else 0
            if atr <= 0:
                continue

            sig = None
            if df["signal_long"].iloc[i]:
                sig = "LONG"
            elif df["signal_short"].iloc[i]:
                sig = "SHORT"

            if sig:
                entry = df["Close"].iloc[i]
                ebar = i
                trade_dir = sig
                if sig == "LONG":
                    sl = entry - sl_mult * atr
                    tp = entry + tp_mult * atr
                else:
                    sl = entry + sl_mult * atr
                    tp = entry - tp_mult * atr

                risk_amt = balance * risk_pct / 100
                sl_dist = abs(entry - sl)
                if sl_dist > 0:
                    size = risk_amt / sl_dist
                else:
                    continue
                in_trade = True

    if not trades:
        return None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0
    longs = sum(1 for t in trades if t["dir"] == "LONG")
    shorts = sum(1 for t in trades if t["dir"] == "SHORT")

    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "pf": round(w_pnl / l_pnl, 2) if l_pnl > 0 else 999.99,
        "ret": round((balance - initial) / initial * 100, 1),
        "dd": round(max_dd, 1),
        "avg_bars": round(np.mean([t["bars"] for t in trades]), 1),
        "longs": longs,
        "shorts": shorts,
    }


def run():
    symbols = {
        "EURUSD=X": "EURUSD",
        "AUDUSD=X": "AUDUSD",
        "GBPUSD=X": "GBPUSD",
        "USDJPY=X": "USDJPY",
        "GC=F": "Gold",
        "SPY": "SPY",
        "QQQ": "QQQ",
        "NVDA": "NVDA",
        "MSFT": "MSFT",
        "AAPL": "AAPL",
        "TSLA": "TSLA",
        "BTC-USD": "BTC",
    }

    timeframes = ["5m", "15m", "1h"]
    periods = {"5m": "60d", "15m": "60d", "1h": "2y"}

    print("Downloading data...")
    cache = {}
    for sym in symbols:
        for tf in timeframes:
            try:
                d = yf.download(sym, period=periods[tf], interval=tf, progress=False)
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                if len(d) > 100:
                    cache[(sym, tf)] = d
            except Exception:
                pass
    print(f"Downloaded {len(cache)} datasets\n")

    # Strategy configs: (strategy_fn, params_dict, sl_mult, tp_mult, risk_pct, label)
    configs = [
        # Strategy A: Sensei
        (strategy_sensei, {"conv_thresh": 10, "conv_bars": 6, "db_tol": 8, "db_lb": 3}, 1.0, 2.0, 5, "Sensei relaxed"),
        (strategy_sensei, {"conv_thresh": 15, "conv_bars": 4, "db_tol": 10, "db_lb": 2}, 1.0, 2.5, 5, "Sensei very loose"),
        (strategy_sensei, {"conv_thresh": 8, "conv_bars": 8, "db_tol": 6, "db_lb": 3}, 0.8, 2.0, 5, "Sensei tight SL"),
        (strategy_sensei, {"conv_thresh": 12, "conv_bars": 5, "db_tol": 8, "db_lb": 2}, 1.0, 3.0, 5, "Sensei wide TP"),
        (strategy_sensei, {"conv_thresh": 20, "conv_bars": 3, "db_tol": 12, "db_lb": 2}, 1.0, 2.0, 8, "Sensei ultra loose"),

        # Strategy B: Consolidation breakout
        (strategy_breakout, {"conv_thresh": 8, "conv_bars": 6}, 1.0, 2.0, 5, "Breakout tight"),
        (strategy_breakout, {"conv_thresh": 12, "conv_bars": 4}, 1.0, 2.0, 5, "Breakout relaxed"),
        (strategy_breakout, {"conv_thresh": 15, "conv_bars": 3}, 1.0, 2.5, 5, "Breakout loose"),
        (strategy_breakout, {"conv_thresh": 10, "conv_bars": 5}, 0.7, 2.0, 8, "Breakout tight SL hi risk"),
        (strategy_breakout, {"conv_thresh": 8, "conv_bars": 8}, 1.0, 3.0, 5, "Breakout wide TP"),

        # Strategy C: RSI squeeze
        (strategy_rsi_squeeze, {"bb_squeeze_pct": 2.0, "conv_bars": 4, "rsi_ob": 65, "rsi_os": 35}, 1.0, 2.0, 5, "RSI squeeze std"),
        (strategy_rsi_squeeze, {"bb_squeeze_pct": 3.0, "conv_bars": 3, "rsi_ob": 60, "rsi_os": 40}, 1.0, 2.5, 5, "RSI squeeze loose"),
        (strategy_rsi_squeeze, {"bb_squeeze_pct": 1.5, "conv_bars": 5, "rsi_ob": 70, "rsi_os": 30}, 0.8, 2.0, 8, "RSI squeeze tight"),
        (strategy_rsi_squeeze, {"bb_squeeze_pct": 4.0, "conv_bars": 3, "rsi_ob": 55, "rsi_os": 45}, 1.0, 2.0, 8, "RSI squeeze very loose"),

        # Strategy D: BB bounce
        (strategy_bb_bounce, {"conv_thresh": 8, "conv_bars": 5}, 1.0, 2.0, 5, "BB bounce tight"),
        (strategy_bb_bounce, {"conv_thresh": 12, "conv_bars": 3}, 1.0, 2.0, 5, "BB bounce relaxed"),
        (strategy_bb_bounce, {"conv_thresh": 15, "conv_bars": 3}, 0.8, 2.5, 8, "BB bounce loose hi risk"),
        (strategy_bb_bounce, {"conv_thresh": 10, "conv_bars": 4}, 1.0, 3.0, 5, "BB bounce wide TP"),
    ]

    results = []
    for strat_fn, params, sl_m, tp_m, risk, label in configs:
        for sym, name in symbols.items():
            for tf in timeframes:
                key = (sym, tf)
                if key not in cache:
                    continue
                df = cache[key]
                try:
                    df_sig = strat_fn(df, **params)
                    r = backtest(df_sig, risk_pct=risk, sl_mult=sl_m, tp_mult=tp_m)
                    if r and r["trades"] >= 3:
                        # Estimate months
                        if tf == "5m":
                            bars_per_day = 12 * 14  # ~14 trading hours * 12 bars/hr
                            days = len(df) / bars_per_day
                        elif tf == "15m":
                            bars_per_day = 4 * 14
                            days = len(df) / bars_per_day
                        else:
                            days = len(df) / 24
                        months = max(days / 21, 0.5)
                        monthly = r["ret"] / months

                        results.append({
                            "name": name,
                            "tf": tf.upper().replace("M", "m"),
                            "label": label,
                            "monthly": round(monthly, 1),
                            **r,
                        })
                except Exception:
                    pass

    if not results:
        print("No results")
        return

    # Sort by monthly return
    results.sort(key=lambda x: x["monthly"], reverse=True)

    # Filter: PF >= 1.1 to avoid pure noise
    good = [r for r in results if r["pf"] >= 1.1]

    print(f"{'Strategy':25s} | {'TF':>3s} | {'Asset':>6s} | {'Trades':>6s} | {'L/S':>5s} | {'Win%':>5s} | {'PF':>5s} | {'Total%':>7s} | {'DD%':>5s} | {'$/mo':>7s} | {'Bars':>4s}")
    print("-" * 120)
    for r in good[:50]:
        ls = f"{r['longs']}/{r['shorts']}"
        print(
            f"{r['label']:25s} | {r['tf']:>3s} | {r['name']:>6s} | {r['trades']:6d} | {ls:>5s} | "
            f"{r['win_rate']:5.1f} | {r['pf']:5.2f} | {r['ret']:6.1f}% | {r['dd']:4.1f}% | "
            f"{r['monthly']:+6.1f}% | {r['avg_bars']:4.1f}"
        )

    # Best per asset + timeframe
    print("\n\n=== BEST PER ASSET+TIMEFRAME (PF >= 1.2, monthly >= 20%) ===")
    print(f"{'Asset':>6s} | {'TF':>3s} | {'Strategy':25s} | {'Trades':>6s} | {'Win%':>5s} | {'PF':>5s} | {'Total%':>7s} | {'DD%':>5s} | {'$/mo':>7s}")
    print("-" * 100)
    seen = set()
    elite = [r for r in results if r["pf"] >= 1.2 and r["monthly"] >= 20]
    for r in elite:
        k = (r["name"], r["tf"])
        if k not in seen:
            seen.add(k)
            print(
                f"{r['name']:>6s} | {r['tf']:>3s} | {r['label']:25s} | {r['trades']:6d} | "
                f"{r['win_rate']:5.1f} | {r['pf']:5.2f} | {r['ret']:6.1f}% | {r['dd']:4.1f}% | "
                f"{r['monthly']:+6.1f}%"
            )

    # Top 15 with 80%+/mo
    print("\n\n=== CANDIDATES FOR 80%+/MONTH ===")
    monsters = [r for r in results if r["monthly"] >= 80 and r["pf"] >= 1.15]
    if monsters:
        for r in monsters[:15]:
            ls = f"{r['longs']}L/{r['shorts']}S"
            print(
                f"  {r['name']:>6s} {r['tf']:>3s} | {r['label']:25s} | "
                f"{r['trades']} trades ({ls}) | Win {r['win_rate']:.0f}% | PF {r['pf']:.2f} | "
                f"Return {r['ret']:+.0f}% | DD {r['dd']:.0f}% | ~{r['monthly']:+.0f}%/mo"
            )
    else:
        print("  No configs hit 80%/mo with PF >= 1.15")
        print("  Closest:")
        close = [r for r in results if r["pf"] >= 1.1 and r["monthly"] >= 30]
        for r in close[:10]:
            ls = f"{r['longs']}L/{r['shorts']}S"
            print(
                f"  {r['name']:>6s} {r['tf']:>3s} | {r['label']:25s} | "
                f"{r['trades']} trades ({ls}) | Win {r['win_rate']:.0f}% | PF {r['pf']:.2f} | "
                f"Return {r['ret']:+.0f}% | DD {r['dd']:.0f}% | ~{r['monthly']:+.0f}%/mo"
            )


if __name__ == "__main__":
    run()
