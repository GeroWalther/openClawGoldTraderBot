"""Compare ratchet progression schemes after 0.2R lock at 1R.

Tests different trailing step sizes after the initial 0.2R lock.
"""

import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_scalp_indicators
from app.services.m5_scalp_scoring import M5ScalpScoringEngine


COST_PER_RT = 0.00012
ICM_MIN_SIZE = 1000
MAX_POSITION_SIZE = 1000
MIN_STOP = 0.0005
SIGNAL_THRESHOLD = 6.0
RISK_PCT = 3.0
INITIAL_BALANCE = 150.0
DEBOUNCE_BARS = 12


def calc_ratchet_sl(ratchet_level, sl_dist, lock_1r, step):
    """Calculate how much to lock at a given ratchet level.
    lock_1r: fraction locked at 1R
    step: fraction locked per additional R level after 1R
    """
    if ratchet_level <= 0:
        return 0
    if ratchet_level == 1:
        return lock_1r * sl_dist
    return (lock_1r + (ratchet_level - 1) * step) * sl_dist


def backtest(m5_df, h1_df, lock_1r=0.2, step=0.5):
    engine = M5ScalpScoringEngine(signal_threshold=SIGNAL_THRESHOLD)
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)

    balance = INITIAL_BALANCE
    lowest_balance = INITIAL_BALANCE
    peak = INITIAL_BALANCE
    max_dd = 0
    trades = []
    in_trade = False
    last_bar = -DEBOUNCE_BARS - 1
    last_dir = None
    h1_index = h1_df.index

    for i in range(50, len(m5_df)):
        if in_trade:
            bh, bl = m5_df["high"].iloc[i], m5_df["low"].iloc[i]

            sl_hit = False
            if trade_dir == "BUY":
                if bl <= sl:
                    pnl = (sl - entry) * size - COST_PER_RT * size
                    sl_hit = True
                else:
                    profit_r = (bh - entry) / sl_dist
                    ratchet_level = max(0, int(math.floor(profit_r)))
                    if ratchet_level >= 1 and ratchet_level > current_ratchet:
                        new_sl = entry + calc_ratchet_sl(ratchet_level, sl_dist, lock_1r, step)
                        if new_sl > sl:
                            sl = new_sl
                            current_ratchet = ratchet_level
            else:  # SELL
                if bh >= sl:
                    pnl = (entry - sl) * size - COST_PER_RT * size
                    sl_hit = True
                else:
                    profit_r = (entry - bl) / sl_dist
                    ratchet_level = max(0, int(math.floor(profit_r)))
                    if ratchet_level >= 1 and ratchet_level > current_ratchet:
                        new_sl = entry - calc_ratchet_sl(ratchet_level, sl_dist, lock_1r, step)
                        if new_sl < sl:
                            sl = new_sl
                            current_ratchet = ratchet_level

            if sl_hit:
                balance += pnl
                trades.append({
                    "pnl": pnl, "dir": trade_dir, "entry": entry, "exit": sl,
                    "sl_dist": sl_dist, "size": size, "ratchet_level": current_ratchet,
                    "time": m5_df.index[i],
                })
                in_trade = False

            if balance < lowest_balance:
                lowest_balance = balance
            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

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

            if (i - last_bar) < DEBOUNCE_BARS and last_dir == direction:
                continue

            entry = m5_df["close"].iloc[i]
            trade_dir = direction
            sl_dist = max(1.0 * atr, MIN_STOP)

            if direction == "BUY":
                sl = entry - sl_dist
            else:
                sl = entry + sl_dist

            risk_amt = balance * RISK_PCT / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0
            size = max(round(size / 1000) * 1000, ICM_MIN_SIZE)
            size = min(size, MAX_POSITION_SIZE)

            actual_risk = size * sl_dist
            if actual_risk > balance * 0.5:
                continue
            if size <= 0:
                continue

            in_trade = True
            last_bar = i
            last_dir = direction
            current_ratchet = 0

    return trades, balance, lowest_balance, max_dd


def run_pair(symbol, label, m5_df, h1_df):
    days = (m5_df.index[-1] - m5_df.index[0]).days
    months = max(days / 30, 0.5)

    print(f"\n{label}: {len(m5_df)} M5 bars ({days} days, {months:.1f} months)")

    schemes = [
        # (lock_1r, step, description)
        # Current live
        (0.5, 0.5, "CURRENT: 0.5R @ 1R, +0.5R/level"),
        # Option B variants with 0.2R lock, different steps
        (0.2, 0.5, "0.2R @ 1R, +0.5R/level (backtest winner)"),
        (0.2, 0.4, "0.2R @ 1R, +0.4R/level (tighter trail)"),
        (0.2, 0.3, "0.2R @ 1R, +0.3R/level (gentle trail)"),
        (0.2, 0.6, "0.2R @ 1R, +0.6R/level (aggressive trail)"),
        (0.2, 0.7, "0.2R @ 1R, +0.7R/level (very aggressive)"),
    ]

    results = []
    for lock_1r, step, desc in schemes:
        t, b, l, d = backtest(m5_df.copy(), h1_df.copy(), lock_1r=lock_1r, step=step)
        wins = [x for x in t if x["pnl"] > 0]
        losses = [x for x in t if x["pnl"] <= 0]
        w_pnl = sum(x["pnl"] for x in wins) if wins else 0
        l_pnl = abs(sum(x["pnl"] for x in losses)) if losses else 0.01
        pf = w_pnl / l_pnl
        ret = (b - INITIAL_BALANCE) / INITIAL_BALANCE * 100
        wr = len(wins) / len(t) * 100 if t else 0
        avg_ratchet = np.mean([x["ratchet_level"] for x in wins]) if wins else 0
        max_ratchet = max((x["ratchet_level"] for x in wins), default=0)
        results.append((lock_1r, step, desc, ret, d, pf, len(t), wr, avg_ratchet, max_ratchet))

        # Print SL schedule for this scheme
        if lock_1r == 0.2:
            schedule = []
            for rl in range(1, 6):
                locked = calc_ratchet_sl(rl, 1.0, lock_1r, step)
                schedule.append(f"{rl}R→{locked:.1f}R")
            # only print once per unique step
            pass

    # Summary table
    print(f"\n{'=' * 90}")
    print(f"  {label} — PROGRESSION COMPARISON")
    print(f"{'=' * 90}")
    print(f"  {'Scheme':<42} {'Ret':>7} {'MaxDD':>6} {'PF':>5} {'WR':>5} {'#':>4} {'AvgR':>5} {'MaxR':>5}")
    print(f"  {'-'*42} {'-'*7} {'-'*6} {'-'*5} {'-'*5} {'-'*4} {'-'*5} {'-'*5}")
    for lock_1r, step, desc, ret, dd, pf, cnt, wr, avgr, maxr in results:
        print(f"  {desc:<42} {ret:>+6.1f}% {dd:>5.1f}% {pf:>5.2f} {wr:>4.0f}% {cnt:>4} {avgr:>5.1f} {maxr:>5}")

    # Print SL schedules
    print(f"\n  SL lock schedule (in R-multiples):")
    print(f"  {'Scheme':<42} {'1R':>5} {'2R':>5} {'3R':>5} {'4R':>5} {'5R':>5}")
    print(f"  {'-'*42} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*5}")
    for lock_1r, step, desc, *_ in results:
        locks = [calc_ratchet_sl(rl, 1.0, lock_1r, step) for rl in range(1, 6)]
        print(f"  {desc:<42} {locks[0]:>5.2f} {locks[1]:>5.2f} {locks[2]:>5.2f} {locks[3]:>5.2f} {locks[4]:>5.2f}")


def main():
    print("=" * 60)
    print("  RATCHET PROGRESSION SWEEP")
    print("  Fixed: 0.2R lock at 1R")
    print("  Variable: trailing step size after 1R")
    print("=" * 60)

    for symbol, label in [("NZDUSD=X", "NZDUSD"), ("AUDUSD=X", "AUDUSD")]:
        print(f"\nDownloading {symbol}...")
        m5 = yf.download(symbol, period="60d", interval="5m", progress=False)
        h1 = yf.download(symbol, period="2y", interval="1h", progress=False)
        for df in [m5, h1]:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]
        if len(m5) > 0:
            run_pair(symbol, label, m5, h1)
        else:
            print(f"  {label}: Download failed, skipping")


if __name__ == "__main__":
    main()
