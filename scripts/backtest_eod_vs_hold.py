"""Backtest comparison: EOD close vs hold overnight, single vs multi-position.

Tests 4 variants on NZDUSD M5 scalp with ratchet SL:
1. BASELINE: single position, hold overnight
2. EOD CLOSE: single position, close at 20:55 UTC
3. MULTI-POS: new trade on every signal (max 3 concurrent)
4. MULTI-POS + EOD: multi-position with EOD close

All use: €150 account, 3% risk, 1000 max size, ratchet SL, threshold=6.
Swap cost: ~0.30€/night per 1000 units NZDUSD (based on IC Markets rates).
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
SWAP_PER_NIGHT_PER_1000 = 0.05  # €0.05 per 1000 units per night (IC Markets NZDUSD — short receives swap, minimal cost)
EOD_HOUR = 20  # Close at 20:xx UTC
EOD_MINUTE = 55
MAX_CONCURRENT = 3  # Max positions for multi-pos mode


def is_eod(bar_time):
    """Check if bar is at or past EOD close time."""
    return bar_time.hour > EOD_HOUR or (bar_time.hour == EOD_HOUR and bar_time.minute >= EOD_MINUTE)


def is_overnight(prev_time, curr_time):
    """Check if overnight boundary was crossed between two bars."""
    if prev_time is None:
        return False
    return prev_time.date() != curr_time.date()


def backtest(m5_df, h1_df, eod_close=False, multi_pos=False):
    engine = M5ScalpScoringEngine(signal_threshold=SIGNAL_THRESHOLD)
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)

    balance = INITIAL_BALANCE
    lowest_balance = INITIAL_BALANCE
    peak = INITIAL_BALANCE
    max_dd = 0
    trades = []
    equity_curve = [INITIAL_BALANCE]
    h1_index = h1_df.index

    # For single-position mode
    positions = []  # list of open position dicts
    last_bar = -DEBOUNCE_BARS - 1
    last_dir = None
    swap_total = 0.0
    eod_closes = 0
    prev_bar_time = None

    for i in range(50, len(m5_df)):
        bar_time = m5_df.index[i]
        bh, bl, bc = m5_df["high"].iloc[i], m5_df["low"].iloc[i], m5_df["close"].iloc[i]

        # --- Charge swap for overnight holds ---
        if is_overnight(prev_bar_time, bar_time) and positions:
            for pos in positions:
                swap_cost = SWAP_PER_NIGHT_PER_1000 * (pos["size"] / 1000)
                balance -= swap_cost
                swap_total += swap_cost

        # --- EOD close: force-close all positions at session end ---
        if eod_close and is_eod(bar_time) and positions:
            for pos in positions:
                if pos["dir"] == "BUY":
                    pnl = (bc - pos["entry"]) * pos["size"] - COST_PER_RT * pos["size"]
                else:
                    pnl = (pos["entry"] - bc) * pos["size"] - COST_PER_RT * pos["size"]
                balance += pnl
                trades.append({
                    "pnl": pnl, "dir": pos["dir"], "entry": pos["entry"], "exit": bc,
                    "sl_dist": pos["sl_dist"], "size": pos["size"],
                    "ratchet_level": pos["ratchet"], "exit_reason": "eod_close",
                    "time": bar_time,
                })
                eod_closes += 1
            positions.clear()

        # --- Manage open positions: check SL, ratchet ---
        closed_indices = []
        for pi, pos in enumerate(positions):
            sl_hit = False
            if pos["dir"] == "BUY":
                if bl <= pos["sl"]:
                    pnl = (pos["sl"] - pos["entry"]) * pos["size"] - COST_PER_RT * pos["size"]
                    sl_hit = True
                else:
                    profit_r = (bh - pos["entry"]) / pos["sl_dist"]
                    rl = max(0, int(math.floor(profit_r)))
                    if rl >= 1 and rl > pos["ratchet"]:
                        new_sl = pos["entry"] + rl * 0.5 * pos["sl_dist"]
                        if new_sl > pos["sl"]:
                            pos["sl"] = new_sl
                            pos["ratchet"] = rl
            else:
                if bh >= pos["sl"]:
                    pnl = (pos["entry"] - pos["sl"]) * pos["size"] - COST_PER_RT * pos["size"]
                    sl_hit = True
                else:
                    profit_r = (pos["entry"] - bl) / pos["sl_dist"]
                    rl = max(0, int(math.floor(profit_r)))
                    if rl >= 1 and rl > pos["ratchet"]:
                        new_sl = pos["entry"] - rl * 0.5 * pos["sl_dist"]
                        if new_sl < pos["sl"]:
                            pos["sl"] = new_sl
                            pos["ratchet"] = rl

            if sl_hit:
                balance += pnl
                trades.append({
                    "pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                    "exit": pos["sl"], "sl_dist": pos["sl_dist"], "size": pos["size"],
                    "ratchet_level": pos["ratchet"], "exit_reason": "sl",
                    "time": bar_time,
                })
                closed_indices.append(pi)

        for pi in reversed(closed_indices):
            positions.pop(pi)

        # --- Track equity ---
        if balance < lowest_balance:
            lowest_balance = balance
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
        equity_curve.append(balance)

        # --- Check for new entry ---
        can_enter = (not positions) if not multi_pos else (len(positions) < MAX_CONCURRENT)

        # Don't open new positions after EOD time (if eod_close mode)
        if eod_close and is_eod(bar_time):
            can_enter = False

        if can_enter:
            atr = m5_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                prev_bar_time = bar_time
                continue

            h1_mask = h1_index <= bar_time
            if not h1_mask.any():
                prev_bar_time = bar_time
                continue
            h1_row = h1_df.loc[h1_index[h1_mask][-1]]

            m5_tail = m5_df.iloc[max(0, i - 5):i + 1]
            result = engine.score(h1_row, m5_tail, bar_time=bar_time)

            if result["direction"] is not None:
                direction = result["direction"]

                # Same-direction debounce
                if (i - last_bar) < DEBOUNCE_BARS and last_dir == direction:
                    prev_bar_time = bar_time
                    continue

                # Don't stack same-direction positions
                if multi_pos and any(p["dir"] == direction for p in positions):
                    prev_bar_time = bar_time
                    continue

                entry = m5_df["close"].iloc[i]
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
                if actual_risk > balance * 0.5 or size <= 0:
                    prev_bar_time = bar_time
                    continue

                positions.append({
                    "dir": direction, "entry": entry, "sl": sl,
                    "sl_dist": sl_dist, "size": size, "ratchet": 0,
                })
                last_bar = i
                last_dir = direction

        prev_bar_time = bar_time

    # Close any remaining positions at last price
    if positions:
        last_close = m5_df["close"].iloc[-1]
        for pos in positions:
            if pos["dir"] == "BUY":
                pnl = (last_close - pos["entry"]) * pos["size"] - COST_PER_RT * pos["size"]
            else:
                pnl = (pos["entry"] - last_close) * pos["size"] - COST_PER_RT * pos["size"]
            balance += pnl
            trades.append({
                "pnl": pnl, "dir": pos["dir"], "entry": pos["entry"], "exit": last_close,
                "sl_dist": pos["sl_dist"], "size": pos["size"],
                "ratchet_level": pos["ratchet"], "exit_reason": "end_of_data",
                "time": m5_df.index[-1],
            })
        positions.clear()

    return {
        "trades": trades, "balance": balance, "lowest": lowest_balance,
        "max_dd": max_dd, "equity": equity_curve, "swap_total": swap_total,
        "eod_closes": eod_closes,
    }


def print_results(label, r, months):
    trades = r["trades"]
    if not trades:
        print(f"  {label}: No trades")
        return

    balance = r["balance"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
    ret = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    monthly = ret / months if months > 0 else 0
    wr = len(wins) / len(trades) * 100
    pf = w_pnl / l_pnl

    avg_win = w_pnl / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl"] for t in losses)) / len(losses) if losses else 0

    eod_count = sum(1 for t in trades if t.get("exit_reason") == "eod_close")
    sl_count = sum(1 for t in trades if t.get("exit_reason") == "sl")

    winner_ratchets = [t["ratchet_level"] for t in wins]
    avg_ratchet = np.mean(winner_ratchets) if winner_ratchets else 0

    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Trades:        {len(trades)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate:      {wr:.1f}%")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Avg Win:       €{avg_win:.4f} (avg ratchet: {avg_ratchet:.1f}R)")
    print(f"  Avg Loss:      €{avg_loss:.4f}")
    print(f"  Final Balance: €{balance:.2f} (from €{INITIAL_BALANCE:.0f})")
    print(f"  Lowest Balance:€{r['lowest']:.2f}")
    print(f"  Return:        {ret:+.1f}%")
    print(f"  Monthly:       {monthly:+.1f}%/month")
    print(f"  Max Drawdown:  {r['max_dd']:.1f}%")
    print(f"  Swap Fees:     €{r['swap_total']:.2f}")
    print(f"  Exits:         {sl_count} SL, {eod_count} EOD close")
    print(f"  Trades/day:    {len(trades) / (months * 21):.1f}")


def run():
    print("Downloading NZDUSD data...")
    m5_df = yf.download("NZDUSD=X", period="60d", interval="5m", progress=False)
    h1_df = yf.download("NZDUSD=X", period="2y", interval="1h", progress=False)

    for df in [m5_df, h1_df]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

    days = (m5_df.index[-1] - m5_df.index[0]).days
    months = max(days / 30, 0.5)

    print(f"Data: {len(m5_df)} M5 bars ({days} days, {months:.1f} months)")
    print(f"Setup: €{INITIAL_BALANCE} account, {RISK_PCT}% risk, "
          f"MAX_POS={MAX_POSITION_SIZE}, threshold={SIGNAL_THRESHOLD}")
    print(f"Swap cost: €{SWAP_PER_NIGHT_PER_1000:.2f}/night per 1000 units")

    # 1. Baseline: single position, hold overnight
    r1 = backtest(m5_df.copy(), h1_df.copy(), eod_close=False, multi_pos=False)
    print_results("1. BASELINE (single pos, hold overnight)", r1, months)

    # 2. EOD close: single position, close at 20:55
    r2 = backtest(m5_df.copy(), h1_df.copy(), eod_close=True, multi_pos=False)
    print_results("2. EOD CLOSE (single pos, close at 20:55)", r2, months)

    # 3. Multi-position: new trade on signal (max 3)
    r3 = backtest(m5_df.copy(), h1_df.copy(), eod_close=False, multi_pos=True)
    print_results("3. MULTI-POS (max 3, hold overnight)", r3, months)

    # 4. Multi-position + EOD close
    r4 = backtest(m5_df.copy(), h1_df.copy(), eod_close=True, multi_pos=True)
    print_results("4. MULTI-POS + EOD (max 3, close at 20:55)", r4, months)

    # Summary comparison
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY COMPARISON")
    print(f"{'=' * 60}")
    for label, r in [
        ("Baseline (hold)", r1),
        ("EOD Close", r2),
        ("Multi-Pos (hold)", r3),
        ("Multi + EOD", r4),
    ]:
        ret = (r["balance"] - INITIAL_BALANCE) / INITIAL_BALANCE * 100
        n = len(r["trades"])
        wr = len([t for t in r["trades"] if t["pnl"] > 0]) / n * 100 if n else 0
        print(f"  {label:20s}  €{r['balance']:7.2f}  {ret:+6.1f}%  "
              f"{n:3d} trades  {wr:4.1f}% WR  "
              f"DD:{r['max_dd']:4.1f}%  Swap:€{r['swap_total']:.2f}")


if __name__ == "__main__":
    run()
