"""Compare current ratchet (0.5R lock at 1R) vs Option B (BE at 1R, 0.5R lock at 2R).

Runs both variants on NZDUSD + AUDUSD M5 Scalp with identical signals.
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


def backtest(m5_df, h1_df, mode="current", lock_at_1r=0.5):
    """mode: 'current' = lock_at_1r lock at 1R, 'optionB' = lock_at_1r at 1R with offset.
    lock_at_1r: fraction of sl_dist to lock at 1R (0=BE, 0.5=current, 0.2=slight profit)."""
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
                        # At 1R: lock lock_at_1r × sl_dist
                        # At 2R+: lock_at_1r + (ratchet_level-1) * 0.5 × sl_dist
                        if ratchet_level == 1:
                            new_sl = entry + lock_at_1r * sl_dist
                        else:
                            new_sl = entry + (lock_at_1r + (ratchet_level - 1) * 0.5) * sl_dist
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
                        if ratchet_level == 1:
                            new_sl = entry - lock_at_1r * sl_dist
                        else:
                            new_sl = entry - (lock_at_1r + (ratchet_level - 1) * 0.5) * sl_dist
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


def print_results(label, trades, balance, lowest, max_dd, months):
    if not trades:
        print(f"  {label}: No trades")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
    ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    monthly = ret / months
    wr = len(wins) / len(trades) * 100
    pf = w_pnl / l_pnl

    avg_win = w_pnl / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl"] for t in losses)) / len(losses) if losses else 0

    winner_ratchets = [t["ratchet_level"] for t in wins]
    avg_ratchet = np.mean(winner_ratchets) if winner_ratchets else 0
    max_ratchet = max(winner_ratchets) if winner_ratchets else 0

    # Count winners at each ratchet level
    ratchet_dist = {}
    for t in trades:
        rl = t["ratchet_level"]
        if rl not in ratchet_dist:
            ratchet_dist[rl] = {"wins": 0, "losses": 0}
        if t["pnl"] > 0:
            ratchet_dist[rl]["wins"] += 1
        else:
            ratchet_dist[rl]["losses"] += 1

    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Trades:        {len(trades)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate:      {wr:.1f}%")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Avg Win:       EUR {avg_win:.4f}  (avg ratchet: {avg_ratchet:.1f}R, max: {max_ratchet}R)")
    print(f"  Avg Loss:      EUR {avg_loss:.4f}")
    print(f"  Final Balance: EUR {balance:.2f} (from EUR {INITIAL_BALANCE:.0f})")
    print(f"  Lowest Balance:EUR {lowest:.2f}")
    print(f"  Return:        {ret:+.1f}%")
    print(f"  Monthly:       {monthly:+.1f}%/month")
    print(f"  Max Drawdown:  {max_dd:.1f}%")
    print(f"  Trades/day:    {len(trades) / (months * 21):.1f}")

    print(f"\n  Ratchet distribution:")
    for rl in sorted(ratchet_dist.keys()):
        d = ratchet_dist[rl]
        total = d["wins"] + d["losses"]
        print(f"    {rl}R: {total} trades ({d['wins']}W / {d['losses']}L)")

    print(f"\n  Last 10 trades:")
    for t in trades[-10:]:
        tag = "W" if t["pnl"] > 0 else "L"
        r_info = f" (ratchet {t['ratchet_level']}R)" if t["ratchet_level"] > 0 else ""
        print(f"    [{tag}] {t['dir']} entry={t['entry']:.5f} exit={t['exit']:.5f} "
              f"pnl=EUR {t['pnl']:+.4f} size={t['size']:.0f}{r_info}")


def run_pair(symbol, label):
    print(f"\nDownloading {symbol} data...")
    m5_df = yf.download(symbol, period="60d", interval="5m", progress=False)
    h1_df = yf.download(symbol, period="2y", interval="1h", progress=False)

    for df in [m5_df, h1_df]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

    days = (m5_df.index[-1] - m5_df.index[0]).days
    months = max(days / 30, 0.5)

    print(f"Data: {len(m5_df)} M5 bars ({days} days, {months:.1f} months)")

    # Test different lock-at-1R levels
    levels = [
        (0.5, "CURRENT (0.5R lock at 1R)"),
        (0.0, "BE at 1R"),
        (0.1, "0.1R lock at 1R"),
        (0.2, "0.2R lock at 1R"),
        (0.3, "0.3R lock at 1R"),
    ]

    results = []
    for lock, desc in levels:
        t, b, l, d = backtest(m5_df.copy(), h1_df.copy(), lock_at_1r=lock)
        print_results(f"{label} — {desc}", t, b, l, d, months)
        ret = (b - INITIAL_BALANCE) / INITIAL_BALANCE * 100
        wins = [x for x in t if x["pnl"] > 0]
        losses = [x for x in t if x["pnl"] <= 0]
        w_pnl = sum(x["pnl"] for x in wins) if wins else 0
        l_pnl = abs(sum(x["pnl"] for x in losses)) if losses else 0.01
        pf = w_pnl / l_pnl
        results.append((lock, desc, ret, d, pf, len(t)))

    # Summary table
    print(f"\n{'=' * 70}")
    print(f"  {label} — SUMMARY")
    print(f"{'=' * 70}")
    print(f"  {'Lock@1R':>8}  {'Return':>8}  {'MaxDD':>6}  {'PF':>5}  {'Trades':>6}")
    print(f"  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*6}")
    for lock, desc, ret, dd, pf, cnt in results:
        print(f"  {lock:>7.1f}R  {ret:>+7.1f}%  {dd:>5.1f}%  {pf:>5.2f}  {cnt:>6}")


def main():
    print("=" * 60)
    print("  RATCHET COMPARISON: Lock level at 1R sweep")
    print("  Testing: 0R (BE), 0.1R, 0.2R, 0.3R, 0.5R (current)")
    print("=" * 60)

    run_pair("NZDUSD=X", "NZDUSD")
    run_pair("AUDUSD=X", "AUDUSD")


if __name__ == "__main__":
    main()
