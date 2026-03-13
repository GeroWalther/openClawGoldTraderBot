"""Backtest NZDUSD M5 Scalp — exact live setup with ratchet SL exit.

€150 account, 3% risk, MAX_POSITION_SIZE=1000, min stop 5 pips,
ratchet SL: tighten by 0.5×sl_dist each 1R of profit (floor-based).
Signal threshold=6, 12-bar same-direction debounce.
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


COST_PER_RT = 0.00012  # ~1.2 pip spread + slippage
ICM_MIN_SIZE = 1000
MAX_POSITION_SIZE = 1000
MIN_STOP = 0.0005      # 5 pips minimum
SIGNAL_THRESHOLD = 6.0
RISK_PCT = 3.0
INITIAL_BALANCE = 150.0
DEBOUNCE_BARS = 12


def backtest(m5_df, h1_df, max_pos_size=None):
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
    in_trade = False
    last_bar = -DEBOUNCE_BARS - 1
    last_dir = None
    h1_index = h1_df.index

    for i in range(50, len(m5_df)):
        if in_trade:
            bh, bl = m5_df["high"].iloc[i], m5_df["low"].iloc[i]

            # Check SL hit
            sl_hit = False
            if trade_dir == "BUY":
                if bl <= sl:
                    pnl = (sl - entry) * size - COST_PER_RT * size
                    sl_hit = True
                else:
                    # Ratchet: floor-based, tighten 0.5R each 1R
                    profit_r = (bh - entry) / sl_dist
                    ratchet_level = max(0, int(math.floor(profit_r)))
                    if ratchet_level >= 1 and ratchet_level > current_ratchet:
                        new_sl = entry + ratchet_level * 0.5 * sl_dist
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
                        new_sl = entry - ratchet_level * 0.5 * sl_dist
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
            equity_curve.append(balance)

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

            # Same-direction debounce
            if (i - last_bar) < DEBOUNCE_BARS and last_dir == direction:
                continue

            entry = m5_df["close"].iloc[i]
            trade_dir = direction
            sl_dist = max(1.0 * atr, MIN_STOP)

            if direction == "BUY":
                sl = entry - sl_dist
            else:
                sl = entry + sl_dist

            # Position sizing: risk-based, capped by MAX_POSITION_SIZE
            risk_amt = balance * RISK_PCT / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0
            size = max(round(size / 1000) * 1000, ICM_MIN_SIZE)
            cap = max_pos_size if max_pos_size is not None else MAX_POSITION_SIZE
            if cap > 0:
                size = min(size, cap)

            # Skip if risk too high for balance
            actual_risk = size * sl_dist
            if actual_risk > balance * 0.5:
                continue

            if size <= 0:
                continue
            in_trade = True
            last_bar = i
            last_dir = direction
            current_ratchet = 0

    return trades, balance, lowest_balance, max_dd, equity_curve


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

    # Avg ratchet level for winners
    winner_ratchets = [t["ratchet_level"] for t in wins]
    avg_ratchet = np.mean(winner_ratchets) if winner_ratchets else 0

    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Trades:       {len(trades)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate:     {wr:.1f}%")
    print(f"  Profit Factor:{pf:.2f}")
    print(f"  Avg Win:      €{avg_win:.4f}  (avg ratchet level: {avg_ratchet:.1f}R)")
    print(f"  Avg Loss:     €{avg_loss:.4f}")
    print(f"  Final Balance:€{balance:.2f} (from €{INITIAL_BALANCE:.0f})")
    print(f"  Lowest Balance:€{lowest:.2f}")
    print(f"  Return:       {ret:+.1f}%")
    print(f"  Monthly:      {monthly:+.1f}%/month")
    print(f"  Max Drawdown: {max_dd:.1f}%")
    print(f"  Trades/day:   {len(trades) / (months * 21):.1f}")

    # Last 10 trades
    print(f"\n  Last 10 trades:")
    for t in trades[-10:]:
        emoji = "W" if t["pnl"] > 0 else "L"
        r_info = f" (ratchet {t['ratchet_level']}R)" if t["ratchet_level"] > 0 else ""
        print(f"    [{emoji}] {t['dir']} entry={t['entry']:.5f} exit={t['exit']:.5f} "
              f"pnl=€{t['pnl']:+.4f} size={t['size']:.0f}{r_info}")


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
          f"MAX_POSITION_SIZE={MAX_POSITION_SIZE}, threshold={SIGNAL_THRESHOLD}")

    # Run with current live settings
    trades, balance, lowest, max_dd, eq = backtest(m5_df.copy(), h1_df.copy())
    print_results("LIVE SETUP (ratchet SL, 1000 cap)", trades, balance, lowest, max_dd, months)

    # Also test without position cap (what we'd get with proper sizing)
    trades2, balance2, lowest2, max_dd2, eq2 = backtest(m5_df.copy(), h1_df.copy(), max_pos_size=0)
    print_results("UNCAPPED (ratchet SL, risk-based sizing)", trades2, balance2, lowest2, max_dd2, months)


if __name__ == "__main__":
    run()
