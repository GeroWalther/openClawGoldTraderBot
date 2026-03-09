"""Sensei V2 — Aggressive backtesting.

Tests:
- Shorter timeframes: H1, H4 (resampled from H1)
- Both LONG and SHORT (double bottom + double top)
- Relaxed consolidation parameters
- Multiple assets
"""

import numpy as np
import pandas as pd
import yfinance as yf


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def compute_sensei_signals(
    df: pd.DataFrame,
    conv_threshold: float = 10.0,
    conv_min_bars: int = 20,
    db_lookback: int = 5,
    db_tolerance: float = 5.0,
    db_min_bars: int = 5,
    db_max_bars: int = 80,
    both_directions: bool = True,
) -> pd.DataFrame:
    """Compute Sensei signals — LONG + SHORT."""

    df = df.copy()
    close = df["Close"].values
    low = df["Low"].values
    high = df["High"].values

    # SMAs
    df["sma5"] = df["Close"].rolling(5).mean()
    df["sma10"] = df["Close"].rolling(10).mean()
    df["sma20"] = df["Close"].rolling(20).mean()
    df["sma50"] = df["Close"].rolling(50).mean()
    df["atr"] = compute_atr(df, 14)

    # Consolidation: MA spread
    ma_cols = ["sma5", "sma10", "sma20", "sma50"]
    ma_df = df[ma_cols]
    ma_high = ma_df.max(axis=1)
    ma_low = ma_df.min(axis=1)
    df["ma_spread_pct"] = (ma_high - ma_low) / df["Close"] * 100

    df["is_converged"] = df["ma_spread_pct"] <= conv_threshold

    # Count consecutive converged bars
    conv_count = np.zeros(len(df))
    for i in range(1, len(df)):
        if df["is_converged"].iloc[i]:
            conv_count[i] = conv_count[i - 1] + 1
        else:
            conv_count[i] = 0
    df["conv_count"] = conv_count
    df["is_consolidating"] = df["conv_count"] >= conv_min_bars

    # --- Pivot lows (for LONG double bottom) ---
    df["pivot_low"] = np.nan
    for i in range(db_lookback, len(df) - db_lookback):
        is_pivot = True
        for j in range(1, db_lookback + 1):
            if low[i] > low[i - j] or low[i] > low[i + j]:
                is_pivot = False
                break
        if is_pivot:
            df.iloc[i, df.columns.get_loc("pivot_low")] = low[i]

    # --- Pivot highs (for SHORT double top) ---
    df["pivot_high"] = np.nan
    if both_directions:
        for i in range(db_lookback, len(df) - db_lookback):
            is_pivot = True
            for j in range(1, db_lookback + 1):
                if high[i] < high[i - j] or high[i] < high[i + j]:
                    is_pivot = False
                    break
            if is_pivot:
                df.iloc[i, df.columns.get_loc("pivot_high")] = high[i]

    # --- Double bottom (W pattern) — LONG ---
    df["w_found"] = False
    bot1_price, bot1_idx = np.nan, 0
    for i in range(len(df)):
        if not np.isnan(df["pivot_low"].iloc[i]):
            piv_price = df["pivot_low"].iloc[i]
            if not np.isnan(bot1_price):
                dist = i - bot1_idx
                pct_diff = abs(piv_price - bot1_price) / bot1_price * 100
                right_higher = piv_price > bot1_price
                in_consol = (
                    df["is_consolidating"].iloc[i]
                    or df["conv_count"].iloc[i] >= conv_min_bars * 0.5
                )
                if (
                    pct_diff <= db_tolerance
                    and dist >= db_min_bars
                    and dist <= db_max_bars
                    and right_higher
                    and in_consol
                ):
                    df.iloc[i, df.columns.get_loc("w_found")] = True
            bot1_price, bot1_idx = piv_price, i

    # --- Double top (M pattern) — SHORT ---
    df["m_found"] = False
    if both_directions:
        top1_price, top1_idx = np.nan, 0
        for i in range(len(df)):
            if not np.isnan(df["pivot_high"].iloc[i]):
                piv_price = df["pivot_high"].iloc[i]
                if not np.isnan(top1_price):
                    dist = i - top1_idx
                    pct_diff = abs(piv_price - top1_price) / top1_price * 100
                    right_lower = piv_price < top1_price
                    in_consol = (
                        df["is_consolidating"].iloc[i]
                        or df["conv_count"].iloc[i] >= conv_min_bars * 0.5
                    )
                    if (
                        pct_diff <= db_tolerance
                        and dist >= db_min_bars
                        and dist <= db_max_bars
                        and right_lower
                        and in_consol
                    ):
                        df.iloc[i, df.columns.get_loc("m_found")] = True
                top1_price, top1_idx = piv_price, i

    # Propagate w_found / m_found forward
    w_active = np.zeros(len(df), dtype=bool)
    m_active = np.zeros(len(df), dtype=bool)
    wa, ma = False, False
    for i in range(len(df)):
        if df["w_found"].iloc[i]:
            wa = True
        if df["m_found"].iloc[i]:
            ma = True
        w_active[i] = wa
        m_active[i] = ma

    # Entry signals
    df["cross_above"] = (df["Close"] > df["sma20"]) & (
        df["Close"].shift(1) <= df["sma20"].shift(1)
    )
    df["cross_below"] = (df["Close"] < df["sma20"]) & (
        df["Close"].shift(1) >= df["sma20"].shift(1)
    )

    df["signal_long"] = False
    df["signal_short"] = False

    for i in range(len(df)):
        consol_ok = df["is_consolidating"].iloc[i] or (
            i > 0 and df["is_consolidating"].iloc[i - 1]
        )

        # LONG
        if consol_ok and w_active[i] and df["cross_above"].iloc[i]:
            df.iloc[i, df.columns.get_loc("signal_long")] = True
            wa = False
            for j in range(i + 1, len(df)):
                if df["w_found"].iloc[j]:
                    break
                w_active[j] = False

        # SHORT
        if both_directions and consol_ok and m_active[i] and df["cross_below"].iloc[i]:
            df.iloc[i, df.columns.get_loc("signal_short")] = True
            ma = False
            for j in range(i + 1, len(df)):
                if df["m_found"].iloc[j]:
                    break
                m_active[j] = False

    return df


def resample_to_h4(df_h1: pd.DataFrame) -> pd.DataFrame:
    """Resample H1 OHLC to H4."""
    return df_h1.resample("4h").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna()


def backtest(
    df: pd.DataFrame,
    initial_balance: float = 10000.0,
    risk_percent: float = 5.0,
    sl_atr_mult: float = 1.0,
    tp_atr_mult: float = 2.0,
) -> dict:
    """Simulate trades from signals."""
    balance = initial_balance
    peak_balance = initial_balance
    max_dd = 0
    trades = []
    in_trade = False
    trade_dir = None

    for i in range(len(df)):
        if in_trade:
            if trade_dir == "LONG":
                if df["Low"].iloc[i] <= sl_price:
                    pnl = (sl_price - entry_price) * trade_size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - entry_bar, "dir": "LONG", "exit": "sl"})
                    in_trade = False
                elif df["High"].iloc[i] >= tp_price:
                    pnl = (tp_price - entry_price) * trade_size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - entry_bar, "dir": "LONG", "exit": "tp"})
                    in_trade = False
            else:  # SHORT
                if df["High"].iloc[i] >= sl_price:
                    pnl = (entry_price - sl_price) * trade_size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - entry_bar, "dir": "SHORT", "exit": "sl"})
                    in_trade = False
                elif df["Low"].iloc[i] <= tp_price:
                    pnl = (entry_price - tp_price) * trade_size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - entry_bar, "dir": "SHORT", "exit": "tp"})
                    in_trade = False

            # Track drawdown
            if balance > peak_balance:
                peak_balance = balance
            dd = (peak_balance - balance) / peak_balance * 100
            if dd > max_dd:
                max_dd = dd

        if not in_trade:
            atr = df["atr"].iloc[i]
            if np.isnan(atr) or atr <= 0:
                continue

            signal = None
            if df["signal_long"].iloc[i]:
                signal = "LONG"
            elif "signal_short" in df.columns and df["signal_short"].iloc[i]:
                signal = "SHORT"

            if signal:
                entry_price = df["Close"].iloc[i]
                entry_bar = i
                trade_dir = signal

                if signal == "LONG":
                    sl_price = entry_price - sl_atr_mult * atr
                    tp_price = entry_price + tp_atr_mult * atr
                else:
                    sl_price = entry_price + sl_atr_mult * atr
                    tp_price = entry_price - tp_atr_mult * atr

                risk_amount = balance * risk_percent / 100
                sl_dist = abs(entry_price - sl_price)
                if sl_dist > 0:
                    trade_size = risk_amount / sl_dist
                else:
                    continue
                in_trade = True

    if not trades:
        return None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_pnl = sum(t["pnl"] for t in wins) if wins else 0
    loss_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0
    longs = [t for t in trades if t["dir"] == "LONG"]
    shorts = [t for t in trades if t["dir"] == "SHORT"]

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "pf": round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else 999.99,
        "return_pct": round((balance - initial_balance) / initial_balance * 100, 1),
        "max_dd": round(max_dd, 1),
        "avg_bars": round(np.mean([t["bars"] for t in trades]), 1),
        "longs": len(longs),
        "shorts": len(shorts),
        "balance": round(balance, 2),
    }


def run_tests():
    symbols = {
        "EURUSD=X": "EUR/USD",
        "AUDUSD=X": "AUD/USD",
        "GBPUSD=X": "GBP/USD",
        "USDJPY=X": "USD/JPY",
        "GC=F": "Gold",
        "SPY": "S&P 500",
        "QQQ": "Nasdaq",
        "AAPL": "Apple",
        "MSFT": "Microsoft",
        "NVDA": "Nvidia",
    }

    # Parameter grid
    configs = [
        # (timeframe, conv_threshold, conv_min_bars, db_tolerance, db_lookback, both_dirs, sl_mult, tp_mult, label)
        ("1h", 8, 15, 5, 4, True, 1.0, 2.0, "H1 tight"),
        ("1h", 12, 10, 8, 4, True, 1.0, 2.0, "H1 relaxed"),
        ("1h", 15, 8, 10, 3, True, 1.0, 2.5, "H1 very relaxed"),
        ("1h", 10, 12, 6, 4, True, 0.8, 2.0, "H1 tight SL"),
        ("1h", 12, 10, 8, 4, False, 1.0, 2.0, "H1 long-only"),
        ("4h", 8, 8, 5, 3, True, 1.0, 2.0, "H4 tight"),
        ("4h", 12, 5, 8, 3, True, 1.0, 2.0, "H4 relaxed"),
        ("4h", 15, 4, 10, 2, True, 1.0, 2.5, "H4 very relaxed"),
        ("4h", 12, 5, 8, 3, False, 1.0, 2.0, "H4 long-only"),
        ("1d", 10, 20, 8, 5, True, 1.0, 2.0, "D1 both dirs"),
        ("1d", 7, 42, 5, 7, True, 1.5, 3.0, "D1 original+short"),
    ]

    # Download data
    print("Downloading data...")
    data_cache = {}
    for symbol in symbols:
        try:
            df_h1 = yf.download(symbol, period="2y", interval="1h", progress=False)
            if isinstance(df_h1.columns, pd.MultiIndex):
                df_h1.columns = df_h1.columns.get_level_values(0)
            if not df_h1.empty:
                data_cache[(symbol, "1h")] = df_h1
                # Resample to H4
                df_h4 = resample_to_h4(df_h1)
                if not df_h4.empty:
                    data_cache[(symbol, "4h")] = df_h4
        except Exception as e:
            print(f"  {symbol} H1 download failed: {e}")

        try:
            df_d1 = yf.download(symbol, period="5y", interval="1d", progress=False)
            if isinstance(df_d1.columns, pd.MultiIndex):
                df_d1.columns = df_d1.columns.get_level_values(0)
            if not df_d1.empty:
                data_cache[(symbol, "1d")] = df_d1
        except Exception as e:
            print(f"  {symbol} D1 download failed: {e}")

    print(f"\nData downloaded: {len(data_cache)} datasets\n")

    # Run all combinations
    results = []
    for cfg in configs:
        tf, conv_thresh, conv_bars, db_tol, db_lb, both, sl_m, tp_m, label = cfg
        for symbol, name in symbols.items():
            key = (symbol, tf)
            if key not in data_cache:
                continue
            df = data_cache[key]
            if len(df) < 100:
                continue

            try:
                df_sig = compute_sensei_signals(
                    df,
                    conv_threshold=conv_thresh,
                    conv_min_bars=conv_bars,
                    db_tolerance=db_tol,
                    db_lookback=db_lb,
                    both_directions=both,
                )
                r = backtest(df_sig, sl_atr_mult=sl_m, tp_atr_mult=tp_m)
                if r and r["trades"] >= 3:
                    results.append({
                        "symbol": symbol,
                        "name": name,
                        "config": label,
                        **r,
                    })
            except Exception:
                pass

    if not results:
        print("No results with >= 3 trades")
        return

    # Sort by return
    results.sort(key=lambda x: x["return_pct"], reverse=True)

    # Print top 40
    print(f"{'Config':20s} | {'Asset':10s} | {'Trades':>6s} | {'L/S':>5s} | {'Win%':>5s} | {'PF':>6s} | {'Return':>8s} | {'MaxDD':>6s} | {'Bars':>5s}")
    print("-" * 100)
    for r in results[:40]:
        ls = f"{r['longs']}/{r['shorts']}"
        print(
            f"{r['config']:20s} | {r['name']:10s} | {r['trades']:6d} | {ls:>5s} | "
            f"{r['win_rate']:5.1f} | {r['pf']:6.2f} | {r['return_pct']:7.1f}% | "
            f"{r['max_dd']:5.1f}% | {r['avg_bars']:5.1f}"
        )

    # Best per asset
    print("\n\n=== BEST CONFIG PER ASSET ===")
    print(f"{'Asset':10s} | {'Config':20s} | {'Trades':>6s} | {'Win%':>5s} | {'PF':>6s} | {'Return':>8s} | {'MaxDD':>6s}")
    print("-" * 80)
    seen = set()
    for r in results:
        if r["name"] not in seen:
            seen.add(r["name"])
            print(
                f"{r['name']:10s} | {r['config']:20s} | {r['trades']:6d} | "
                f"{r['win_rate']:5.1f} | {r['pf']:6.2f} | {r['return_pct']:7.1f}% | "
                f"{r['max_dd']:5.1f}%"
            )

    # Monthly return estimate for top results
    print("\n\n=== TOP 10 — MONTHLY RETURN ESTIMATE ===")
    for r in results[:10]:
        tf = r["config"].split()[0]
        if tf == "H1":
            trading_days = len(data_cache.get((r["symbol"], "1h"), [])) / 24
        elif tf == "H4":
            trading_days = len(data_cache.get((r["symbol"], "4h"), [])) / 6
        elif tf == "D1":
            trading_days = len(data_cache.get((r["symbol"], "1d"), []))
        else:
            trading_days = 250
        months = max(trading_days / 21, 1)
        monthly = r["return_pct"] / months
        print(
            f"  {r['name']:10s} {r['config']:20s} — {r['return_pct']:+.1f}% over {months:.0f}mo "
            f"= ~{monthly:+.1f}%/mo (PF {r['pf']:.2f}, DD {r['max_dd']:.1f}%)"
        )


if __name__ == "__main__":
    run_tests()
