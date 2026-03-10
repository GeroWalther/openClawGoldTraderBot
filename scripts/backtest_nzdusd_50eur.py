"""Backtest NZDUSD M5 Scalp with €50 account, 5% risk, 1 trade/day limit.

Realistic simulation matching our live setup exactly.
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


COST_PER_RT = 0.00012  # NZDUSD: ~1.2 pip spread + slippage
ICM_MIN_SIZE = 1000    # IC Markets min 0.01 lots = 1000 units


def backtest(m5_df, h1_df, risk_pct=5.0, initial=50.0, debounce_bars=12, max_daily_trades=1):
    engine = M5ScalpScoringEngine()
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)

    balance = initial
    peak = initial
    max_dd = 0
    trades = []
    in_trade = False
    last_bar = -debounce_bars - 1
    last_dir = None
    h1_index = h1_df.index

    # Track daily trade count
    current_day = None
    daily_trades = 0

    for i in range(50, len(m5_df)):
        # Track trading day
        bar_day = m5_df.index[i].date()
        if bar_day != current_day:
            current_day = bar_day
            daily_trades = 0

        if in_trade:
            bh, bl = m5_df["high"].iloc[i], m5_df["low"].iloc[i]
            if trade_dir == "BUY":
                if bl <= sl:
                    pnl = (sl - entry) * size - COST_PER_RT * size
                    balance += pnl
                    trades.append({"pnl": pnl, "dir": "BUY", "date": str(bar_day),
                                   "entry": entry, "exit_price": sl, "size": size})
                    in_trade = False
                else:
                    pnl_r = (bh - entry) / sl_dist
                    if pnl_r >= 1.0:
                        new_sl = bh - 0.5 * sl_dist
                        if new_sl > sl:
                            sl = new_sl
            else:
                if bh >= sl:
                    pnl = (entry - sl) * size - COST_PER_RT * size
                    balance += pnl
                    trades.append({"pnl": pnl, "dir": "SELL", "date": str(bar_day),
                                   "entry": entry, "exit_price": sl, "size": size})
                    in_trade = False
                else:
                    pnl_r = (entry - bl) / sl_dist
                    if pnl_r >= 1.0:
                        new_sl = bl + 0.5 * sl_dist
                        if new_sl < sl:
                            sl = new_sl

            if balance > peak: peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

        if not in_trade:
            # Daily trade limit
            if daily_trades >= max_daily_trades:
                continue

            atr = m5_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                continue

            m5_time = m5_df.index[i]
            h1_mask = h1_index <= m5_time
            if not h1_mask.any():
                continue
            h1_row = h1_df.loc[h1_index[h1_mask][-1]]

            m5_tail = m5_df.iloc[max(0, i-5):i+1]
            result = engine.score(h1_row, m5_tail, bar_time=m5_time)

            if result["direction"] is None:
                continue
            direction = result["direction"]

            if (i - last_bar) < debounce_bars and last_dir == direction:
                continue

            entry = m5_df["close"].iloc[i]
            ebar = i
            trade_dir = direction
            sl_dist = max(1.0 * atr, 0.0005)

            if direction == "BUY":
                sl = entry - sl_dist
            else:
                sl = entry + sl_dist

            risk_amt = balance * risk_pct / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0

            # Round to nearest 1000 (forex), enforce IC Markets minimum
            size = max(round(size / 1000) * 1000, ICM_MIN_SIZE)

            if size <= 0: continue

            # Check if we can afford the actual risk at this size
            actual_risk = size * sl_dist
            if actual_risk > balance * 0.5:  # Don't risk more than 50% on a single trade
                continue

            in_trade = True
            last_bar = i
            last_dir = direction
            daily_trades += 1

    return trades, balance, initial, max_dd


def run():
    print("=" * 70)
    print("NZDUSD M5 SCALP — €50 Account, 5% Risk, 1 Trade/Day")
    print("=" * 70)
    print("Matching live setup: IC Markets, 1.2 pip cost, min 0.01 lots")
    print()

    m5_df = yf.download("NZDUSD=X", period="60d", interval="5m", progress=False)
    h1_df = yf.download("NZDUSD=X", period="2y", interval="1h", progress=False)

    for df in [m5_df, h1_df]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

    days = (m5_df.index[-1] - m5_df.index[0]).days
    months = max(days / 30, 0.5)

    print(f"Data: {len(m5_df)} M5 bars, {len(h1_df)} H1 bars ({days} days)")
    print()

    # Test with 1 trade/day (current setup)
    for max_trades in [1, 3, 9999]:
        label = f"{max_trades}/day" if max_trades < 9999 else "unlimited"
        trades, balance, initial, max_dd = backtest(
            m5_df.copy(), h1_df.copy(), max_daily_trades=max_trades
        )

        if not trades:
            print(f"  {label}: 0 trades")
            continue

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        w_pnl = sum(t["pnl"] for t in wins) if wins else 0
        l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
        ret = (balance - initial) / initial * 100
        monthly = ret / months
        wr = len(wins) / len(trades) * 100
        pf = w_pnl / l_pnl

        avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
        avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0

        # Typical position size
        sizes = [t["size"] for t in trades]
        avg_size = np.mean(sizes)

        print(f"  Max {label}:")
        print(f"    Trades: {len(trades)} ({len(trades)/months:.0f}/mo)")
        print(f"    Win Rate: {wr:.1f}%")
        print(f"    Profit Factor: {pf:.2f}")
        print(f"    Final Balance: €{balance:.2f} (from €{initial:.2f})")
        print(f"    Return: {ret:+.1f}% ({monthly:+.1f}%/mo)")
        print(f"    Max Drawdown: {max_dd:.1f}%")
        print(f"    Avg Win: €{avg_win:.2f} | Avg Loss: €{avg_loss:.2f}")
        print(f"    Avg Position: {avg_size:.0f} units ({avg_size/100000:.2f} lots)")
        print(f"    Lowest balance: €{initial * (1 - max_dd/100):.2f}")
        print()

    # Show first 10 trades for 1/day
    trades_1, _, _, _ = backtest(m5_df.copy(), h1_df.copy(), max_daily_trades=1)
    if trades_1:
        print("  First 10 trades (1/day limit):")
        print(f"  {'#':>3} | {'Date':<12} | {'Dir':<5} | {'Size':>8} | {'PnL':>8} | {'Balance':>8}")
        print(f"  {'-'*3}-+-{'-'*12}-+-{'-'*5}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}")
        bal = 50.0
        for i, t in enumerate(trades_1[:10]):
            bal += t["pnl"]
            print(f"  {i+1:>3} | {t['date']:<12} | {t['dir']:<5} | {t['size']:>7.0f} | €{t['pnl']:>+7.2f} | €{bal:>7.2f}")


if __name__ == "__main__":
    run()
