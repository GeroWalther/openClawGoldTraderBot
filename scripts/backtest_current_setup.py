"""Backtest NZDUSD M5 Scalp with CURRENT live setup.

€50 account, 4% risk, min stop 20 pips, no partial TP, costs included.
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


COST_PER_RT = 0.00012  # ~1.2 pip spread + slippage
ICM_MIN_SIZE = 1000
MIN_STOP = 0.0020      # 20 pips — current live setting


def backtest(m5_df, h1_df, risk_pct=4.0, initial=50.0, debounce_bars=12, label=""):
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

    for i in range(50, len(m5_df)):
        if in_trade:
            bh, bl = m5_df["high"].iloc[i], m5_df["low"].iloc[i]
            if trade_dir == "BUY":
                if bl <= sl:
                    pnl = (sl - entry) * size - COST_PER_RT * size
                    balance += pnl
                    trades.append({"pnl": pnl, "dir": "BUY"})
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
                    trades.append({"pnl": pnl, "dir": "SELL"})
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
            trade_dir = direction
            sl_dist = max(1.0 * atr, MIN_STOP)

            if direction == "BUY":
                sl = entry - sl_dist
            else:
                sl = entry + sl_dist

            risk_amt = balance * risk_pct / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0
            size = max(round(size / 1000) * 1000, ICM_MIN_SIZE)

            actual_risk = size * sl_dist
            if actual_risk > balance * 0.5:
                continue

            if size <= 0: continue
            in_trade = True
            last_bar = i
            last_dir = direction

    return trades, balance, initial, max_dd


def run():
    print("=" * 70)
    print("NZDUSD M5 SCALP — Current Live Setup")
    print("=" * 70)

    m5_df = yf.download("NZDUSD=X", period="60d", interval="5m", progress=False)
    h1_df = yf.download("NZDUSD=X", period="2y", interval="1h", progress=False)

    for df in [m5_df, h1_df]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

    days = (m5_df.index[-1] - m5_df.index[0]).days
    months = max(days / 30, 0.5)

    print(f"Data: {len(m5_df)} M5 bars ({days} days, {months:.1f} months)\n")

    configs = [
        ("Current: 20pip min, 4%", 0.0020, 4.0),
        ("Old: 5pip min, 4%", 0.0005, 4.0),
        ("15pip min, 4%", 0.0015, 4.0),
        ("30pip min, 4%", 0.0030, 4.0),
    ]

    print(f"  {'Config':<25s} | {'Trades':>7s} | {'WR%':>6s} | {'PF':>5s} | {'Final':>8s} | {'Return':>10s} | {'Monthly':>10s} | {'MaxDD':>7s}")
    print(f"  {'-'*25}-+-{'-'*7}-+-{'-'*6}-+-{'-'*5}-+-{'-'*8}-+-{'-'*10}-+-{'-'*10}-+-{'-'*7}")

    for label, min_stop, risk_pct in configs:
        global MIN_STOP
        MIN_STOP = min_stop

        trades, balance, initial, max_dd = backtest(m5_df.copy(), h1_df.copy(), risk_pct=risk_pct)

        if not trades:
            print(f"  {label:<25s} | {'0':>7s} |")
            continue

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        w_pnl = sum(t["pnl"] for t in wins) if wins else 0
        l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
        ret = (balance - initial) / initial * 100
        monthly = ret / months
        wr = len(wins) / len(trades) * 100
        pf = w_pnl / l_pnl

        marker = " <-- LIVE" if "Current" in label else ""
        print(f"  {label:<25s} | {len(trades):>7d} | {wr:>5.1f}% | {pf:>5.2f} | €{balance:>6.2f} | {ret:>+9.1f}% | {monthly:>+9.1f}% | {max_dd:>6.1f}%{marker}")


if __name__ == "__main__":
    run()
