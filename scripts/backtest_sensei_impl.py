"""Backtest the ACTUAL implemented M15 Sensei scoring engine.

Uses the real compute_indicators + compute_sensei_indicators + M15SenseiScoringEngine
to generate signals, then simulates trades with the same params as the bot
(SL=0.8*ATR, TP=3.0*ATR, trailing stop activate@1R trail@0.5R, 3% risk).

This validates that the production code matches backtest_sensei_v4's winning config.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_sensei_indicators
from app.services.m15_sensei_scoring import (
    M15SenseiScoringEngine,
    M15_SENSEI_SIGNAL_THRESHOLD,
    M15_SENSEI_HIGH_CONVICTION_THRESHOLD,
    M15_SENSEI_MAX_SCORE,
)


def backtest_implementation(
    m15_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    risk_pct: float = 3.0,
    sl_mult: float = 0.8,
    tp_mult: float = 3.0,
    initial: float = 10000.0,
    trailing: bool = True,
    trail_activate_r: float = 1.0,
    trail_distance_r: float = 0.5,
    debounce_bars: int = 4,  # 60min / 15min = 4 bars
):
    """Run backtest using the actual implemented scoring engine."""

    engine = M15SenseiScoringEngine()

    # Compute indicators on M15 (mirrors technical_analyzer.py)
    compute_indicators(m15_df)
    compute_sensei_indicators(m15_df)

    # Compute indicators on H1 (mirrors technical_analyzer.py)
    compute_indicators(h1_df)
    h1_df["sma100"] = h1_df["close"].rolling(100).mean()

    balance = initial
    peak = initial
    max_dd = 0
    trades = []
    in_trade = False
    last_trade_bar = -debounce_bars - 1
    last_trade_dir = None

    # Map M15 bars to their closest H1 bar
    h1_index = h1_df.index

    for i in range(100, len(m15_df)):  # Skip warmup
        # --- Manage open trade ---
        if in_trade:
            bar_high = m15_df["high"].iloc[i]
            bar_low = m15_df["low"].iloc[i]

            if trade_dir == "BUY":
                cur_pnl_r = (bar_high - entry) / sl_dist

                if bar_low <= sl:
                    pnl = (sl - entry) * size
                    balance += pnl
                    trades.append({
                        "pnl": pnl, "bars": i - ebar, "dir": "BUY",
                        "exit": "sl" if pnl < 0 else "trail_sl",
                        "conviction": trade_conviction, "score": trade_score,
                    })
                    in_trade = False
                elif bar_high >= tp:
                    pnl = (tp - entry) * size
                    balance += pnl
                    trades.append({
                        "pnl": pnl, "bars": i - ebar, "dir": "BUY",
                        "exit": "tp", "conviction": trade_conviction, "score": trade_score,
                    })
                    in_trade = False
                elif trailing and cur_pnl_r >= trail_activate_r:
                    new_sl = bar_high - trail_distance_r * sl_dist
                    if new_sl > sl:
                        sl = new_sl
            else:  # SELL
                cur_pnl_r = (entry - bar_low) / sl_dist

                if bar_high >= sl:
                    pnl = (entry - sl) * size
                    balance += pnl
                    trades.append({
                        "pnl": pnl, "bars": i - ebar, "dir": "SELL",
                        "exit": "sl" if pnl < 0 else "trail_sl",
                        "conviction": trade_conviction, "score": trade_score,
                    })
                    in_trade = False
                elif bar_low <= tp:
                    pnl = (entry - tp) * size
                    balance += pnl
                    trades.append({
                        "pnl": pnl, "bars": i - ebar, "dir": "SELL",
                        "exit": "tp", "conviction": trade_conviction, "score": trade_score,
                    })
                    in_trade = False
                elif trailing and cur_pnl_r >= trail_activate_r:
                    new_sl = bar_low + trail_distance_r * sl_dist
                    if new_sl < sl:
                        sl = new_sl

            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # --- Check for new signal ---
        if not in_trade:
            atr = m15_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                continue

            # Find matching H1 bar (latest H1 bar at or before this M15 bar)
            m15_time = m15_df.index[i]
            h1_mask = h1_index <= m15_time
            if not h1_mask.any():
                continue
            h1_row = h1_df.loc[h1_index[h1_mask][-1]]

            # Score using the actual engine (pass full M15 df up to current bar)
            m15_slice = m15_df.iloc[:i + 1]
            result = engine.score(h1_row, m15_slice)

            direction = result["direction"]
            conviction = result["conviction"]
            score = result["total_score"]

            if direction is None:
                continue

            # Debounce: skip same direction within N bars
            if (i - last_trade_bar) < debounce_bars and last_trade_dir == direction:
                continue

            entry = m15_df["close"].iloc[i]
            ebar = i
            trade_dir = direction
            trade_conviction = conviction
            trade_score = score
            sl_dist = sl_mult * atr

            if direction == "BUY":
                sl = entry - sl_dist
                tp = entry + tp_mult * atr
            else:
                sl = entry + sl_dist
                tp = entry - tp_mult * atr

            risk_amt = balance * risk_pct / 100
            if sl_dist > 0:
                size = risk_amt / sl_dist
            else:
                continue

            in_trade = True
            last_trade_bar = i
            last_trade_dir = direction

    return trades, balance, initial, max_dd


def print_results(label, trades, balance, initial, max_dd, months):
    if not trades:
        print(f"  {label}: No trades")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
    longs = sum(1 for t in trades if t["dir"] == "BUY")
    shorts = sum(1 for t in trades if t["dir"] == "SELL")
    ret = (balance - initial) / initial * 100
    monthly = ret / max(months, 0.5)

    # Consecutive losses
    max_consec = 0
    consec = 0
    for t in trades:
        if t["pnl"] <= 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    # Per-conviction breakdown
    conv_stats = {}
    for conv in ["HIGH", "MEDIUM"]:
        ct = [t for t in trades if t.get("conviction") == conv]
        if ct:
            cw = sum(1 for t in ct if t["pnl"] > 0)
            conv_stats[conv] = f"{cw}/{len(ct)} ({cw/len(ct)*100:.0f}%)"

    # Exit type breakdown
    exits = {}
    for t in trades:
        e = t.get("exit", "?")
        exits[e] = exits.get(e, 0) + 1

    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    print(f"  Trades:       {len(trades)} ({longs}L / {shorts}S)")
    print(f"  Trades/mo:    {len(trades)/max(months,0.5):.1f}")
    print(f"  Win Rate:     {len(wins)/len(trades)*100:.1f}%")
    print(f"  Profit Factor:{w_pnl/l_pnl:.2f}")
    print(f"  Return:       {ret:+.1f}%")
    print(f"  Monthly:      {monthly:+.1f}%/mo")
    print(f"  Max DD:       {max_dd:.1f}%")
    print(f"  Max Consec L: {max_consec}")
    print(f"  Avg Win:      ${np.mean([t['pnl'] for t in wins]):.2f}" if wins else "  Avg Win:      N/A")
    print(f"  Avg Loss:     ${np.mean([t['pnl'] for t in losses]):.2f}" if losses else "  Avg Loss:     N/A")
    print(f"  Avg Bars:     {np.mean([t['bars'] for t in trades]):.1f}")
    print(f"  Exits:        {exits}")
    if conv_stats:
        print(f"  Per Conviction: {conv_stats}")


def run():
    print("Backtesting ACTUAL M15 Sensei implementation")
    print("=" * 80)
    print("Using: compute_indicators + compute_sensei_indicators + M15SenseiScoringEngine")
    print(f"Params: SL=0.8*ATR, TP=3.0*ATR, trailing(activate@1R, trail@0.5R), 3% risk")
    print(f"Threshold: signal >= {M15_SENSEI_SIGNAL_THRESHOLD}, high conviction >= {M15_SENSEI_HIGH_CONVICTION_THRESHOLD}")
    print(f"Max score: {M15_SENSEI_MAX_SCORE}")
    print()

    symbols = {
        "BTC-USD": "BTC",
        "GC=F": "Gold",
        "EURUSD=X": "EURUSD",
        "AUDUSD=X": "AUDUSD",
        "GBPUSD=X": "GBPUSD",
    }

    print("Downloading data...")
    for sym, name in symbols.items():
        print(f"\n--- {name} ({sym}) ---")

        # Fetch M15 (60 days max from yfinance) and H1 (2 years for SMA100)
        try:
            m15_df = yf.download(sym, period="60d", interval="15m", progress=False)
            h1_df = yf.download(sym, period="2y", interval="1h", progress=False)

            if isinstance(m15_df.columns, pd.MultiIndex):
                m15_df.columns = m15_df.columns.get_level_values(0)
            if isinstance(h1_df.columns, pd.MultiIndex):
                h1_df.columns = h1_df.columns.get_level_values(0)

            # Normalize column names to lowercase
            m15_df.columns = [c.lower() for c in m15_df.columns]
            h1_df.columns = [c.lower() for c in h1_df.columns]

        except Exception as e:
            print(f"  Download failed: {e}")
            continue

        if len(m15_df) < 200:
            print(f"  Not enough M15 data ({len(m15_df)} bars)")
            continue
        if len(h1_df) < 100:
            print(f"  Not enough H1 data ({len(h1_df)} bars)")
            continue

        print(f"  M15: {len(m15_df)} bars ({m15_df.index[0]} to {m15_df.index[-1]})")
        print(f"  H1:  {len(h1_df)} bars ({h1_df.index[0]} to {h1_df.index[-1]})")

        days = (m15_df.index[-1] - m15_df.index[0]).days
        months = max(days / 30, 0.5)

        # Run with trailing (the winning config)
        trades, balance, initial, max_dd = backtest_implementation(
            m15_df.copy(), h1_df.copy(),
            risk_pct=3.0, sl_mult=0.8, tp_mult=3.0,
            trailing=True, trail_activate_r=1.0, trail_distance_r=0.5,
        )
        print_results(f"{name} M15 — Trailing (production config)", trades, balance, initial, max_dd, months)

        # Also run without trailing for comparison
        trades2, balance2, initial2, max_dd2 = backtest_implementation(
            m15_df.copy(), h1_df.copy(),
            risk_pct=3.0, sl_mult=0.8, tp_mult=3.0,
            trailing=False,
        )
        print_results(f"{name} M15 — No trailing (comparison)", trades2, balance2, initial2, max_dd2, months)

    # --- V4 comparison: run V4's sensei_quality directly on BTC M15 ---
    print(f"\n\n{'='*80}")
    print("COMPARISON: V4 sensei_quality() on same BTC data")
    print(f"{'='*80}")

    try:
        btc_m15 = yf.download("BTC-USD", period="60d", interval="15m", progress=False)
        if isinstance(btc_m15.columns, pd.MultiIndex):
            btc_m15.columns = btc_m15.columns.get_level_values(0)

        # V4 uses uppercase columns
        from scripts.backtest_sensei_v4 import sensei_quality, backtest

        df_sig = sensei_quality(
            btc_m15, conv_thresh=10, conv_bars=8, db_tol=6, db_lb=3,
            require_trend=True, rsi_filter=True,
        )
        r = backtest(
            df_sig, risk_pct=3.0, sl_mult=0.8, tp_mult=3.0,
            trailing=True, trail_activate_r=1.0, trail_distance_r=0.5,
        )
        if r:
            days = len(btc_m15) / 56  # ~56 M15 bars/day
            months = max(days / 21, 0.5)
            monthly = r["ret"] / months
            print(f"  V4 Result: {r['trades']} trades | WR {r['wr']}% | PF {r['pf']} | "
                  f"Return {r['ret']:+.1f}% | DD {r['dd']:.1f}% | ~{monthly:+.1f}%/mo")
        else:
            print("  V4: No trades")
    except Exception as e:
        print(f"  V4 comparison failed: {e}")


if __name__ == "__main__":
    run()
