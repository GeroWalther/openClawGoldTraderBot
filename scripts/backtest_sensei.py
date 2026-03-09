"""Backtest the Sensei Strategy (底入れ買いスクリーナー).

LONG-only swing strategy:
1. Consolidation: SMA 5/10/20/50 converge within threshold% for N bars
2. Modified double bottom: right bottom higher than left (W pattern)
3. Entry: close crosses above SMA20

Exit: ATR-based (1.5x SL, 3x TP) or trailing stop.
"""

import numpy as np
import pandas as pd
import yfinance as yf


def compute_sensei_signals(
    df: pd.DataFrame,
    conv_threshold: float = 5.0,
    conv_min_bars: int = 63,  # 3 months
    db_lookback: int = 7,
    db_tolerance: float = 5.0,
    db_min_bars: int = 10,
    db_max_bars: int = 120,
) -> pd.DataFrame:
    """Compute Sensei strategy signals on daily OHLC data."""

    df = df.copy()
    close = df["Close"].values
    low = df["Low"].values
    high = df["High"].values

    # SMAs
    df["sma5"] = df["Close"].rolling(5).mean()
    df["sma10"] = df["Close"].rolling(10).mean()
    df["sma20"] = df["Close"].rolling(20).mean()
    df["sma50"] = df["Close"].rolling(50).mean()
    df["sma100"] = df["Close"].rolling(100).mean()
    df["sma200"] = df["Close"].rolling(200).mean()
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

    # Pivot lows (swing lows)
    df["pivot_low"] = np.nan
    for i in range(db_lookback, len(df) - db_lookback):
        is_pivot = True
        for j in range(1, db_lookback + 1):
            if low[i] > low[i - j] or low[i] > low[i + j]:
                is_pivot = False
                break
        if is_pivot:
            df.iloc[i, df.columns.get_loc("pivot_low")] = low[i]

    # Double bottom detection
    df["w_found"] = False
    bot1_price = np.nan
    bot1_idx = 0

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

            bot1_price = piv_price
            bot1_idx = i

    # Propagate w_found forward until consumed by entry
    w_active = False
    w_active_arr = np.zeros(len(df), dtype=bool)
    for i in range(len(df)):
        if df["w_found"].iloc[i]:
            w_active = True
        w_active_arr[i] = w_active
        # Will be reset on entry (below)

    # Entry: close crosses above SMA20
    df["cross_above"] = (df["Close"] > df["sma20"]) & (
        df["Close"].shift(1) <= df["sma20"].shift(1)
    )

    # Generate signals
    df["signal"] = False
    for i in range(len(df)):
        consol_ok = df["is_consolidating"].iloc[i] or (
            i > 0 and df["is_consolidating"].iloc[i - 1]
        )
        db_ok = w_active_arr[i]
        cross_ok = df["cross_above"].iloc[i]

        if consol_ok and db_ok and cross_ok:
            df.iloc[i, df.columns.get_loc("signal")] = True
            w_active = False  # Reset after entry
            # Update forward
            for j in range(i + 1, len(df)):
                if df["w_found"].iloc[j]:
                    break
                w_active_arr[j] = False

    return df


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def backtest_sensei(
    symbol: str,
    period: str = "5y",
    initial_balance: float = 10000.0,
    risk_percent: float = 3.0,
    sl_atr_mult: float = 1.5,
    tp_atr_mult: float = 3.0,
    conv_threshold: float = 5.0,
    conv_min_bars: int = 63,
):
    """Run Sensei strategy backtest on a given symbol."""
    df = yf.download(symbol, period=period, interval="1d", progress=False)
    if df.empty:
        return None

    # Flatten multi-level columns if needed
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = compute_sensei_signals(
        df,
        conv_threshold=conv_threshold,
        conv_min_bars=conv_min_bars,
    )

    # Simulate trades
    balance = initial_balance
    trades = []
    in_trade = False
    entry_price = 0
    sl_price = 0
    tp_price = 0
    entry_date = None
    trade_size = 0

    for i in range(len(df)):
        if in_trade:
            # Check SL/TP
            if df["Low"].iloc[i] <= sl_price:
                # Hit stop loss
                pnl = (sl_price - entry_price) * trade_size
                balance += pnl
                trades.append({
                    "entry_date": str(entry_date),
                    "exit_date": str(df.index[i]),
                    "entry_price": entry_price,
                    "exit_price": sl_price,
                    "pnl": round(pnl, 2),
                    "exit_reason": "sl",
                    "bars_held": i - entry_bar,
                })
                in_trade = False
            elif df["High"].iloc[i] >= tp_price:
                # Hit take profit
                pnl = (tp_price - entry_price) * trade_size
                balance += pnl
                trades.append({
                    "entry_date": str(entry_date),
                    "exit_date": str(df.index[i]),
                    "entry_price": entry_price,
                    "exit_price": tp_price,
                    "pnl": round(pnl, 2),
                    "exit_reason": "tp",
                    "bars_held": i - entry_bar,
                })
                in_trade = False

        if not in_trade and df["signal"].iloc[i]:
            atr = df["atr"].iloc[i]
            if np.isnan(atr) or atr <= 0:
                continue

            entry_price = df["Close"].iloc[i]
            sl_price = entry_price - sl_atr_mult * atr
            tp_price = entry_price + tp_atr_mult * atr
            entry_date = df.index[i]
            entry_bar = i

            # Position size based on risk
            risk_amount = balance * risk_percent / 100
            sl_distance = entry_price - sl_price
            if sl_distance > 0:
                trade_size = risk_amount / sl_distance
            else:
                continue

            in_trade = True

    # Results
    if not trades:
        return {
            "symbol": symbol,
            "total_trades": 0,
            "message": "No signals found",
        }

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    win_pnl = sum(t["pnl"] for t in wins) if wins else 0
    loss_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0

    return {
        "symbol": symbol,
        "period": period,
        "total_trades": len(trades),
        "winning": len(wins),
        "losing": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else 999.99,
        "total_return_pct": round((balance - initial_balance) / initial_balance * 100, 1),
        "final_balance": round(balance, 2),
        "avg_bars_held": round(np.mean([t["bars_held"] for t in trades]), 1),
        "trades": trades,
    }


if __name__ == "__main__":
    # Test on various symbols
    symbols = {
        # Gold
        "GC=F": "Gold Futures",
        # Major stocks
        "AAPL": "Apple",
        "MSFT": "Microsoft",
        "GOOGL": "Alphabet",
        "AMZN": "Amazon",
        "TSLA": "Tesla",
        "NVDA": "Nvidia",
        "META": "Meta",
        # ETFs
        "SPY": "S&P 500 ETF",
        "QQQ": "Nasdaq ETF",
        # Forex (daily)
        "AUDUSD=X": "AUD/USD",
        "EURUSD=X": "EUR/USD",
        "GBPUSD=X": "GBP/USD",
    }

    print(f"{'Symbol':12s} | {'Name':16s} | {'Trades':>6s} | {'Win%':>5s} | {'PF':>6s} | {'Return%':>8s} | {'Avg Hold':>8s}")
    print("-" * 85)

    for symbol, name in symbols.items():
        r = backtest_sensei(symbol, period="5y", initial_balance=10000.0)
        if r is None or r["total_trades"] == 0:
            print(f"{symbol:12s} | {name:16s} | {'NO SIGNALS':>6s}")
        else:
            print(
                f"{symbol:12s} | {name:16s} | {r['total_trades']:6d} | {r['win_rate']:5.1f} | {r['profit_factor']:6.2f} | {r['total_return_pct']:7.1f}% | {r['avg_bars_held']:6.1f}d"
            )

    # Also test with relaxed consolidation (2 months instead of 3)
    print("\n--- Relaxed consolidation (2 months, 7% threshold) ---")
    print(f"{'Symbol':12s} | {'Name':16s} | {'Trades':>6s} | {'Win%':>5s} | {'PF':>6s} | {'Return%':>8s} | {'Avg Hold':>8s}")
    print("-" * 85)

    for symbol, name in symbols.items():
        r = backtest_sensei(symbol, period="5y", initial_balance=10000.0, conv_threshold=7.0, conv_min_bars=42)
        if r is None or r["total_trades"] == 0:
            print(f"{symbol:12s} | {name:16s} | {'NO SIGNALS':>6s}")
        else:
            print(
                f"{symbol:12s} | {name:16s} | {r['total_trades']:6d} | {r['win_rate']:5.1f} | {r['profit_factor']:6.2f} | {r['total_return_pct']:7.1f}% | {r['avg_bars_held']:6.1f}d"
            )
