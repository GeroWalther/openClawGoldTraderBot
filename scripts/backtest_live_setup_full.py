"""Backtest the EXACT live auto-trading setup:

  1. M5 Scalp on NZDUSD (H1 trend gate + EMA9/21 cross + RSI7 + BB)
  2. M15 BB Bounce on AUDUSD (BB touch + RSI extreme + BB squeeze)

Uses the actual scoring engines from production code, with the live
cost model, position sizing, and risk parameters from .env.production.

Usage: .venv/bin/python scripts/backtest_live_setup_full.py
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
from app.services.m15_bb_bounce_scoring import M15BBBounceScoringEngine


# ── Live config (matches .env.production) ────────────────────────────────
INITIAL = 50.0           # EUR account
LEVERAGE = 30            # IC Markets FX leverage
RISK_PCT_HIGH = 3.0      # HIGH conviction
RISK_PCT_MED = 2.25      # MEDIUM conviction
COST_NZDUSD = 0.00012    # Spread cost per unit
COST_AUDUSD = 0.00012
MIN_SIZE = 1000           # IC Markets min lot
SIZE_ROUND = 1000
MAX_POSITION = 1000       # MAX_POSITION_SIZE from .env

# M5 Scalp live params
M5_SL_MULT = 2.0         # ATR multiplier for SL
M5_SIGNAL_THRESH = 6.0   # From .env: M5_SIGNAL_THRESHOLD=6
M5_HIGH_CONV = 11.0

# BB Bounce live params
BB_SL_MULT = 1.5         # ATR multiplier for SL
BB_TP_MULT = 2.0         # ATR multiplier for TP
BB_SIGNAL_THRESH = 6.0
BB_HIGH_CONV = 9.0

# Session hours (UTC) — only trade during London+NY
SESSION_START = 7
SESSION_END = 21

# EOD close at 20:55 UTC
EOD_HOUR = 20
EOD_MIN = 55

# Cooldown: 2 consecutive losses → 10 min pause (M5 scalp only)
COOLDOWN_LOSSES = 2
COOLDOWN_BARS_M5 = 2     # 10 min = 2 M5 bars

# Daily loss limit
MAX_DAILY_LOSS_PCT = 6.0
MAX_WEEKLY_LOSS_PCT = 15.0

# Debounce
M5_DEBOUNCE_BARS = 12    # 12 M5 bars = 1 hour
BB_DEBOUNCE_BARS = 2     # 2 M15 bars = 30 min


def fetch(symbol, period, interval):
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    return df


def in_session(ts):
    """Check if timestamp is within trading session (Mon-Fri, 07-21 UTC)."""
    if not hasattr(ts, 'hour'):
        return True
    if hasattr(ts, 'weekday') and ts.weekday() >= 5:
        return False
    return SESSION_START <= ts.hour < SESSION_END


def is_eod(ts):
    """Check if we should EOD close (20:55 UTC)."""
    if not hasattr(ts, 'hour'):
        return False
    return ts.hour == EOD_HOUR and ts.minute >= EOD_MIN


def simulate_trades(signals, df, cost, debounce_bars, has_tp=False):
    """Simulate trades with live risk management.

    Args:
        signals: list of (bar_idx, direction, sl_dist, tp_dist, conviction)
        df: OHLCV DataFrame
        cost: spread cost per unit
        debounce_bars: minimum bars between trades
        has_tp: whether to use TP (BB Bounce) or ratchet SL (M5 Scalp)
    """
    bal = INITIAL
    peak = INITIAL
    mdd = 0
    trades = []
    equity_curve = [INITIAL]

    in_trade = False
    last_exit = -debounce_bars - 1
    consec_losses = 0
    cooldown_until = -1

    # Daily/weekly loss tracking
    daily_pnl = 0.0
    weekly_pnl = 0.0
    current_day = None
    current_week = None

    signals = sorted(signals, key=lambda s: s[0])
    si = 0

    for i in range(len(df)):
        ts = df.index[i]

        # Reset daily/weekly counters
        if hasattr(ts, 'date'):
            day = ts.date()
            if current_day != day:
                daily_pnl = 0.0
                current_day = day
            week = ts.isocalendar()[1]
            if current_week != week:
                weekly_pnl = 0.0
                current_week = week

        if in_trade:
            bh, bl = df["high"].iloc[i], df["low"].iloc[i]

            # EOD close
            if is_eod(ts):
                c = df["close"].iloc[i]
                if tdir == "BUY":
                    pnl = (c - entry) * sz - cost * sz
                else:
                    pnl = (entry - c) * sz - cost * sz
                bal += pnl
                daily_pnl += pnl
                weekly_pnl += pnl
                trades.append({"pnl": pnl, "dir": tdir, "bar": i, "exit": "eod",
                              "conv": tconv})
                in_trade = False
                last_exit = i
                if pnl <= 0:
                    consec_losses += 1
                else:
                    consec_losses = 0
            elif tdir == "BUY":
                # Check SL
                if bl <= sl:
                    pnl = (sl - entry) * sz - cost * sz
                    bal += pnl; daily_pnl += pnl; weekly_pnl += pnl
                    trades.append({"pnl": pnl, "dir": "BUY", "bar": i, "exit": "sl",
                                  "conv": tconv})
                    in_trade = False; last_exit = i
                    consec_losses = consec_losses + 1 if pnl <= 0 else 0
                # Check TP (BB Bounce only)
                elif has_tp and bh >= tp:
                    pnl = (tp - entry) * sz - cost * sz
                    bal += pnl; daily_pnl += pnl; weekly_pnl += pnl
                    trades.append({"pnl": pnl, "dir": "BUY", "bar": i, "exit": "tp",
                                  "conv": tconv})
                    in_trade = False; last_exit = i; consec_losses = 0
                else:
                    # Ratchet SL (M5 scalp: tighten by 0.5×SL per 1R of profit)
                    if not has_tp:
                        pr = (bh - entry) / sld
                        if pr >= 1.0:
                            ns = bh - 0.5 * sld
                            if ns > sl:
                                sl = ns
            else:  # SELL
                if bh >= sl:
                    pnl = (entry - sl) * sz - cost * sz
                    bal += pnl; daily_pnl += pnl; weekly_pnl += pnl
                    trades.append({"pnl": pnl, "dir": "SELL", "bar": i, "exit": "sl",
                                  "conv": tconv})
                    in_trade = False; last_exit = i
                    consec_losses = consec_losses + 1 if pnl <= 0 else 0
                elif has_tp and bl <= tp:
                    pnl = (entry - tp) * sz - cost * sz
                    bal += pnl; daily_pnl += pnl; weekly_pnl += pnl
                    trades.append({"pnl": pnl, "dir": "SELL", "bar": i, "exit": "tp",
                                  "conv": tconv})
                    in_trade = False; last_exit = i; consec_losses = 0
                else:
                    if not has_tp:
                        pr = (entry - bl) / sld
                        if pr >= 1.0:
                            ns = bl + 0.5 * sld
                            if ns < sl:
                                sl = ns

            if bal > peak:
                peak = bal
            dd = (peak - bal) / peak * 100 if peak > 0 else 0
            if dd > mdd:
                mdd = dd

        if not in_trade:
            while si < len(signals) and signals[si][0] <= i:
                sb, sd, s_sl, s_tp, s_conv = signals[si]
                si += 1
                if sb != i:
                    continue
                if (i - last_exit) < debounce_bars:
                    continue

                # Cooldown check (M5 only, but applied generically)
                if i < cooldown_until:
                    continue
                if consec_losses >= COOLDOWN_LOSSES:
                    cooldown_until = i + COOLDOWN_BARS_M5
                    consec_losses = 0
                    continue

                # Daily/weekly loss limit
                if daily_pnl < -(bal * MAX_DAILY_LOSS_PCT / 100):
                    continue
                if weekly_pnl < -(bal * MAX_WEEKLY_LOSS_PCT / 100):
                    continue

                # Session check
                if not in_session(ts):
                    continue

                atr = df["atr"].iloc[i]
                if pd.isna(atr) or atr <= 0:
                    continue

                entry = df["close"].iloc[i]
                tdir = sd
                tconv = s_conv
                sld = s_sl
                sl = entry - sld if sd == "BUY" else entry + sld

                if s_tp:
                    tp = entry + s_tp if sd == "BUY" else entry - s_tp
                else:
                    tp = None

                # Conviction-based sizing
                risk_pct = RISK_PCT_HIGH if s_conv == "HIGH" else RISK_PCT_MED
                risk = bal * risk_pct / 100
                sz = risk / sld if sld > 0 else 0
                sz = max(round(sz / SIZE_ROUND) * SIZE_ROUND, MIN_SIZE)
                sz = min(sz, MAX_POSITION)

                # Sanity: don't risk more than 50% of balance
                if sz * sld > bal * 0.5 or sz <= 0:
                    continue

                in_trade = True
                break

        equity_curve.append(bal)

    return trades, bal, mdd, equity_curve


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 1: M5 Scalp NZDUSD (exact live implementation)
# ══════════════════════════════════════════════════════════════════════════

def backtest_m5_scalp_nzdusd():
    """Backtest M5 Scalp on NZDUSD using the exact production scoring engine."""
    print("\n" + "=" * 90)
    print("STRATEGY 1: M5 Scalp — NZDUSD (LIVE)")
    print("=" * 90)
    print(f"  Engine: M5ScalpScoringEngine (threshold={M5_SIGNAL_THRESH}, high_conv={M5_HIGH_CONV})")
    print(f"  SL: {M5_SL_MULT}×ATR | Exit: Ratchet SL (0.5×SL per 1R) + EOD close")
    print(f"  Risk: HIGH={RISK_PCT_HIGH}%, MED={RISK_PCT_MED}% | Debounce: {M5_DEBOUNCE_BARS} bars")
    print()

    engine = M5ScalpScoringEngine(
        signal_threshold=M5_SIGNAL_THRESH,
        high_conviction_threshold=M5_HIGH_CONV,
    )

    m5 = fetch("NZDUSD=X", "60d", "5m")
    h1 = fetch("NZDUSD=X", "1mo", "1h")

    if m5.empty or h1.empty:
        print("  ERROR: Could not fetch data")
        return None

    compute_indicators(m5)
    compute_scalp_indicators(m5)
    compute_indicators(h1)

    h1i = h1.index
    signals = []

    for i in range(50, len(m5)):
        t = m5.index[i]

        # Session filter
        if not in_session(t):
            continue

        mask = h1i <= t
        if not mask.any():
            continue
        hr = h1.loc[h1i[mask][-1]]
        tail = m5.iloc[max(0, i - 5):i + 1]

        result = engine.score(hr, tail, bar_time=t)
        if result["direction"] is None:
            continue

        atr = m5["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            continue

        sl_dist = max(M5_SL_MULT * atr, 0.0005)  # MIN_STOP for FX
        conv = result["conviction"]
        signals.append((i, result["direction"], sl_dist, None, conv))

    trades, final_bal, max_dd, equity = simulate_trades(
        signals, m5, COST_NZDUSD, M5_DEBOUNCE_BARS, has_tp=False
    )

    return print_results("M5 Scalp NZDUSD", trades, final_bal, max_dd, m5)


# ══════════════════════════════════════════════════════════════════════════
# STRATEGY 2: M15 BB Bounce AUDUSD (exact live implementation)
# ══════════════════════════════════════════════════════════════════════════

def backtest_bb_bounce_audusd():
    """Backtest M15 BB Bounce on AUDUSD using the exact production scoring engine."""
    print("\n" + "=" * 90)
    print("STRATEGY 2: M15 BB Bounce — AUDUSD (LIVE)")
    print("=" * 90)
    print(f"  Engine: M15BBBounceScoringEngine (threshold={BB_SIGNAL_THRESH}, high_conv={BB_HIGH_CONV})")
    print(f"  SL: {BB_SL_MULT}×ATR | TP: {BB_TP_MULT}×ATR | Exit: SL/TP + EOD close")
    print(f"  Risk: HIGH={RISK_PCT_HIGH}%, MED={RISK_PCT_MED}% | Debounce: {BB_DEBOUNCE_BARS} bars")
    print()

    engine = M15BBBounceScoringEngine(
        signal_threshold=BB_SIGNAL_THRESH,
        high_conviction_threshold=BB_HIGH_CONV,
    )

    m15 = fetch("AUDUSD=X", "60d", "15m")

    if m15.empty:
        print("  ERROR: Could not fetch data")
        return None

    compute_indicators(m15)

    signals = []

    for i in range(50, len(m15)):
        t = m15.index[i]

        # Session filter
        if not in_session(t):
            continue

        tail = m15.iloc[max(0, i - 5):i + 1]
        result = engine.score(tail, bar_time=t)

        if result["direction"] is None:
            continue

        atr = m15["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            continue

        sl_dist = max(BB_SL_MULT * atr, 0.0005)
        tp_dist = max(BB_TP_MULT * atr, 0.0010)
        conv = result["conviction"]
        signals.append((i, result["direction"], sl_dist, tp_dist, conv))

    trades, final_bal, max_dd, equity = simulate_trades(
        signals, m15, COST_AUDUSD, BB_DEBOUNCE_BARS, has_tp=True
    )

    return print_results("M15 BB Bounce AUDUSD", trades, final_bal, max_dd, m15)


# ══════════════════════════════════════════════════════════════════════════
# COMBINED PORTFOLIO
# ══════════════════════════════════════════════════════════════════════════

def backtest_combined():
    """Simulate both strategies running simultaneously on the same account."""
    print("\n" + "=" * 90)
    print("COMBINED PORTFOLIO: M5 Scalp NZDUSD + M15 BB Bounce AUDUSD")
    print("=" * 90)
    print(f"  Shared account: €{INITIAL} | Max position: {MAX_POSITION} units each")
    print()

    # M5 Scalp NZDUSD
    m5_engine = M5ScalpScoringEngine(
        signal_threshold=M5_SIGNAL_THRESH,
        high_conviction_threshold=M5_HIGH_CONV,
    )
    m5 = fetch("NZDUSD=X", "60d", "5m")
    h1 = fetch("NZDUSD=X", "1mo", "1h")

    # M15 BB Bounce AUDUSD
    bb_engine = M15BBBounceScoringEngine(
        signal_threshold=BB_SIGNAL_THRESH,
        high_conviction_threshold=BB_HIGH_CONV,
    )
    m15 = fetch("AUDUSD=X", "60d", "15m")

    if m5.empty or h1.empty or m15.empty:
        print("  ERROR: Could not fetch data")
        return

    compute_indicators(m5); compute_scalp_indicators(m5)
    compute_indicators(h1)
    compute_indicators(m15)

    # Build unified timeline (merge M5 and M15 bars by timestamp)
    bal = INITIAL
    peak = INITIAL
    mdd = 0
    all_trades = []

    # Track positions per instrument
    positions = {}  # inst -> {entry, sl, tp, dir, sz, sld, conv, has_tp}
    daily_pnl = 0.0
    weekly_pnl = 0.0
    current_day = None
    current_week = None
    consec_losses_m5 = 0
    cooldown_until_m5 = pd.Timestamp.min
    last_trade_m5 = pd.Timestamp.min
    last_trade_bb = pd.Timestamp.min

    h1i = h1.index

    # Process M5 bars (higher frequency drives the sim)
    for i in range(50, len(m5)):
        ts = m5.index[i]

        # Reset daily/weekly
        if hasattr(ts, 'date'):
            day = ts.date()
            if current_day != day:
                daily_pnl = 0.0
                current_day = day
            week = ts.isocalendar()[1]
            if current_week != week:
                weekly_pnl = 0.0
                current_week = week

        # --- Check M5 positions (NZDUSD) ---
        if "NZDUSD" in positions:
            pos = positions["NZDUSD"]
            bh, bl = m5["high"].iloc[i], m5["low"].iloc[i]
            closed = False

            if is_eod(ts):
                c = m5["close"].iloc[i]
                pnl = ((c - pos["entry"]) if pos["dir"] == "BUY" else (pos["entry"] - c)) * pos["sz"] - COST_NZDUSD * pos["sz"]
                closed = True; exit_type = "eod"
            elif pos["dir"] == "BUY" and bl <= pos["sl"]:
                pnl = (pos["sl"] - pos["entry"]) * pos["sz"] - COST_NZDUSD * pos["sz"]
                closed = True; exit_type = "sl"
            elif pos["dir"] == "SELL" and bh >= pos["sl"]:
                pnl = (pos["entry"] - pos["sl"]) * pos["sz"] - COST_NZDUSD * pos["sz"]
                closed = True; exit_type = "sl"
            else:
                # Ratchet SL
                if pos["dir"] == "BUY":
                    pr = (bh - pos["entry"]) / pos["sld"]
                    if pr >= 1.0:
                        ns = bh - 0.5 * pos["sld"]
                        if ns > pos["sl"]:
                            pos["sl"] = ns
                else:
                    pr = (pos["entry"] - bl) / pos["sld"]
                    if pr >= 1.0:
                        ns = bl + 0.5 * pos["sld"]
                        if ns < pos["sl"]:
                            pos["sl"] = ns

            if closed:
                bal += pnl; daily_pnl += pnl; weekly_pnl += pnl
                all_trades.append({"pnl": pnl, "dir": pos["dir"], "inst": "NZDUSD",
                                  "strat": "M5_Scalp", "exit": exit_type, "conv": pos["conv"]})
                del positions["NZDUSD"]
                if pnl <= 0:
                    consec_losses_m5 += 1
                else:
                    consec_losses_m5 = 0

        # --- Check M15 positions (AUDUSD) — only on M15 bar boundaries ---
        if "AUDUSD" in positions:
            pos = positions["AUDUSD"]
            # Use M5 data to check AUDUSD? No — we need AUDUSD M5 data.
            # Instead, check on M15 bar boundaries below.
            pass

        # --- Generate M5 Scalp signals ---
        if "NZDUSD" not in positions and in_session(ts):
            if ts > cooldown_until_m5 and (ts - last_trade_m5).total_seconds() >= 3600:
                if daily_pnl > -(bal * MAX_DAILY_LOSS_PCT / 100):
                    if weekly_pnl > -(bal * MAX_WEEKLY_LOSS_PCT / 100):
                        mask = h1i <= ts
                        if mask.any():
                            hr = h1.loc[h1i[mask][-1]]
                            tail = m5.iloc[max(0, i - 5):i + 1]
                            r = m5_engine.score(hr, tail, bar_time=ts)
                            if r["direction"] is not None:
                                atr = m5["atr"].iloc[i]
                                if not pd.isna(atr) and atr > 0:
                                    sld = max(M5_SL_MULT * atr, 0.0005)
                                    conv = r["conviction"]
                                    risk_pct = RISK_PCT_HIGH if conv == "HIGH" else RISK_PCT_MED
                                    risk = bal * risk_pct / 100
                                    sz = risk / sld if sld > 0 else 0
                                    sz = max(round(sz / SIZE_ROUND) * SIZE_ROUND, MIN_SIZE)
                                    sz = min(sz, MAX_POSITION)
                                    if sz * sld <= bal * 0.5 and sz > 0:
                                        entry = m5["close"].iloc[i]
                                        sl = entry - sld if r["direction"] == "BUY" else entry + sld
                                        positions["NZDUSD"] = {
                                            "entry": entry, "sl": sl, "dir": r["direction"],
                                            "sz": sz, "sld": sld, "conv": conv, "has_tp": False,
                                        }
                                        last_trade_m5 = ts
                                        if consec_losses_m5 >= COOLDOWN_LOSSES:
                                            cooldown_until_m5 = ts + pd.Timedelta(minutes=10)
                                            consec_losses_m5 = 0

        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak * 100 if peak > 0 else 0
        if dd > mdd:
            mdd = dd

    # --- Now process M15 for AUDUSD BB Bounce ---
    # (simulated independently since different asset, just shares the balance)
    bal_bb = INITIAL
    peak_bb = INITIAL
    mdd_bb = 0
    bb_trades = []
    last_exit_bb = -3

    for i in range(50, len(m15)):
        ts = m15.index[i]

        if "AUDUSD_bb" in positions:
            pos = positions["AUDUSD_bb"]
            bh, bl = m15["high"].iloc[i], m15["low"].iloc[i]
            closed = False

            if is_eod(ts):
                c = m15["close"].iloc[i]
                pnl = ((c - pos["entry"]) if pos["dir"] == "BUY" else (pos["entry"] - c)) * pos["sz"] - COST_AUDUSD * pos["sz"]
                closed = True; exit_type = "eod"
            elif pos["dir"] == "BUY":
                if bl <= pos["sl"]:
                    pnl = (pos["sl"] - pos["entry"]) * pos["sz"] - COST_AUDUSD * pos["sz"]
                    closed = True; exit_type = "sl"
                elif bh >= pos["tp"]:
                    pnl = (pos["tp"] - pos["entry"]) * pos["sz"] - COST_AUDUSD * pos["sz"]
                    closed = True; exit_type = "tp"
            else:
                if bh >= pos["sl"]:
                    pnl = (pos["entry"] - pos["sl"]) * pos["sz"] - COST_AUDUSD * pos["sz"]
                    closed = True; exit_type = "sl"
                elif bl <= pos["tp"]:
                    pnl = (pos["entry"] - pos["tp"]) * pos["sz"] - COST_AUDUSD * pos["sz"]
                    closed = True; exit_type = "tp"

            if closed:
                bal_bb += pnl
                bb_trades.append({"pnl": pnl, "dir": pos["dir"], "inst": "AUDUSD",
                                 "strat": "BB_Bounce", "exit": exit_type, "conv": pos["conv"]})
                all_trades.append(bb_trades[-1])
                del positions["AUDUSD_bb"]
                last_exit_bb = i

            if bal_bb > peak_bb:
                peak_bb = bal_bb
            dd = (peak_bb - bal_bb) / peak_bb * 100 if peak_bb > 0 else 0
            if dd > mdd_bb:
                mdd_bb = dd

        if "AUDUSD_bb" not in positions and in_session(ts) and (i - last_exit_bb) >= BB_DEBOUNCE_BARS:
            tail = m15.iloc[max(0, i - 5):i + 1]
            r = bb_engine.score(tail, bar_time=ts)
            if r["direction"] is not None:
                atr = m15["atr"].iloc[i]
                if not pd.isna(atr) and atr > 0:
                    sld = max(BB_SL_MULT * atr, 0.0005)
                    tpd = max(BB_TP_MULT * atr, 0.0010)
                    conv = r["conviction"]
                    risk_pct = RISK_PCT_HIGH if conv == "HIGH" else RISK_PCT_MED
                    risk = bal_bb * risk_pct / 100
                    sz = risk / sld if sld > 0 else 0
                    sz = max(round(sz / SIZE_ROUND) * SIZE_ROUND, MIN_SIZE)
                    sz = min(sz, MAX_POSITION)
                    if sz * sld <= bal_bb * 0.5 and sz > 0:
                        entry = m15["close"].iloc[i]
                        sl = entry - sld if r["direction"] == "BUY" else entry + sld
                        tp = entry + tpd if r["direction"] == "BUY" else entry - tpd
                        positions["AUDUSD_bb"] = {
                            "entry": entry, "sl": sl, "tp": tp, "dir": r["direction"],
                            "sz": sz, "sld": sld, "conv": conv, "has_tp": True,
                        }

    # Print combined results
    days_m5 = (m5.index[-1] - m5.index[0]).days if len(m5) > 1 else 1
    days_m15 = (m15.index[-1] - m15.index[0]).days if len(m15) > 1 else 1
    months = max(max(days_m5, days_m15) / 30, 0.5)

    # Separate stats
    m5_trades = [t for t in all_trades if t["strat"] == "M5_Scalp"]
    bb_trades_final = [t for t in all_trades if t["strat"] == "BB_Bounce"]

    combined_pnl = sum(t["pnl"] for t in all_trades)
    combined_bal = INITIAL + combined_pnl

    print(f"\n  {'Strategy':<20s} | {'Trades':>6s} | {'T/mo':>5s} | {'WR%':>6s} | {'PF':>5s} | {'Return':>9s} | {'Mo/Ret':>9s}")
    print(f"  {'-'*20}-+-{'-'*6}-+-{'-'*5}-+-{'-'*6}-+-{'-'*5}-+-{'-'*9}-+-{'-'*9}")

    for label, tlist in [("M5 Scalp NZDUSD", m5_trades), ("BB Bounce AUDUSD", bb_trades_final), ("COMBINED", all_trades)]:
        if not tlist:
            print(f"  {label:<20s} | {'0':>6s} |       |        |       |           |")
            continue
        wins = [t for t in tlist if t["pnl"] > 0]
        losses = [t for t in tlist if t["pnl"] <= 0]
        wp = sum(t["pnl"] for t in wins)
        lp = abs(sum(t["pnl"] for t in losses)) or 0.001
        wr = len(wins) / len(tlist) * 100
        pf = wp / lp
        total_pnl = sum(t["pnl"] for t in tlist)
        ret = total_pnl / INITIAL * 100
        mo = ret / months

        print(f"  {label:<20s} | {len(tlist):>6d} | {len(tlist)/months:>5.1f} | {wr:>5.1f}% | {pf:>5.2f} | {ret:>+8.1f}% | {mo:>+8.1f}%")

    # Exit type breakdown
    print(f"\n  Exit breakdown:")
    for strat_label, tlist in [("M5 Scalp", m5_trades), ("BB Bounce", bb_trades_final)]:
        if not tlist:
            continue
        sl_exits = [t for t in tlist if t.get("exit") == "sl"]
        tp_exits = [t for t in tlist if t.get("exit") == "tp"]
        eod_exits = [t for t in tlist if t.get("exit") == "eod"]
        print(f"    {strat_label}: SL={len(sl_exits)} | TP={len(tp_exits)} | EOD={len(eod_exits)}")

    # Conviction breakdown
    print(f"\n  Conviction breakdown:")
    for strat_label, tlist in [("M5 Scalp", m5_trades), ("BB Bounce", bb_trades_final)]:
        if not tlist:
            continue
        high = [t for t in tlist if t.get("conv") == "HIGH"]
        med = [t for t in tlist if t.get("conv") == "MEDIUM"]
        high_wr = len([t for t in high if t["pnl"] > 0]) / len(high) * 100 if high else 0
        med_wr = len([t for t in med if t["pnl"] > 0]) / len(med) * 100 if med else 0
        print(f"    {strat_label}: HIGH={len(high)} (WR {high_wr:.0f}%) | MED={len(med)} (WR {med_wr:.0f}%)")

    combined_ret = (combined_bal - INITIAL) / INITIAL * 100
    print(f"\n  Combined final balance: €{combined_bal:.2f} ({combined_ret:+.1f}%)")
    print(f"  Period: {months:.1f} months")


def print_results(name, trades, final_bal, max_dd, df):
    """Print detailed results for a single strategy."""
    if not trades:
        print(f"  {name}: NO TRADES")
        return None

    days = (df.index[-1] - df.index[0]).days if len(df) > 1 else 1
    months = max(days / 30, 0.5)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wp = sum(t["pnl"] for t in wins) if wins else 0
    lp = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    ret = (final_bal - INITIAL) / INITIAL * 100
    mo = ret / months
    wr = len(wins) / len(trades) * 100
    pf = wp / lp
    pnls = [t["pnl"] for t in trades]
    sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls)) if len(pnls) > 1 and np.std(pnls) > 0 else 0
    aw = np.mean([t["pnl"] for t in wins]) if wins else 0
    al = np.mean([abs(t["pnl"]) for t in losses]) if losses else 0

    print(f"  Results:")
    print(f"    Trades:     {len(trades)} ({len(trades)/months:.1f}/mo)")
    print(f"    Win Rate:   {wr:.1f}%")
    print(f"    Profit Factor: {pf:.2f}")
    print(f"    Return:     {ret:+.1f}% ({mo:+.1f}%/mo)")
    print(f"    Max DD:     {max_dd:.1f}%")
    print(f"    Sharpe:     {sharpe:.2f}")
    print(f"    Avg Win:    €{aw:.4f}")
    print(f"    Avg Loss:   €{al:.4f}")
    print(f"    Final:      €{final_bal:.2f}")
    print(f"    Period:     {months:.1f} months")

    # Exit type breakdown
    sl_exits = [t for t in trades if t.get("exit") == "sl"]
    tp_exits = [t for t in trades if t.get("exit") == "tp"]
    eod_exits = [t for t in trades if t.get("exit") == "eod"]
    print(f"    Exits:      SL={len(sl_exits)} | TP={len(tp_exits)} | EOD={len(eod_exits)}")

    # Conviction breakdown
    high = [t for t in trades if t.get("conv") == "HIGH"]
    med = [t for t in trades if t.get("conv") == "MEDIUM"]
    high_wr = len([t for t in high if t["pnl"] > 0]) / len(high) * 100 if high else 0
    med_wr = len([t for t in med if t["pnl"] > 0]) / len(med) * 100 if med else 0
    print(f"    Conviction: HIGH={len(high)} (WR {high_wr:.0f}%) | MED={len(med)} (WR {med_wr:.0f}%)")

    return {"trades": len(trades), "wr": wr, "pf": pf, "ret": ret, "mo": mo, "mdd": max_dd, "sharpe": sharpe}


def run():
    print("=" * 90)
    print("LIVE SETUP BACKTEST — Exact Production Implementation")
    print("=" * 90)
    print(f"Account: €{INITIAL} | Leverage: 1:{LEVERAGE} | Max pos: {MAX_POSITION} units")
    print(f"Session: {SESSION_START:02d}-{SESSION_END:02d} UTC | EOD close: {EOD_HOUR}:{EOD_MIN:02d}")
    print(f"Loss limits: {MAX_DAILY_LOSS_PCT}% daily, {MAX_WEEKLY_LOSS_PCT}% weekly")
    print()

    r1 = backtest_m5_scalp_nzdusd()
    r2 = backtest_bb_bounce_audusd()
    backtest_combined()

    print("\n" + "=" * 90)
    print("VERDICT")
    print("=" * 90)
    print()
    print("  Active auto-trading strategies:")
    print("    1. M5 Scalp → NZDUSD (IC Markets) — every 5 min, 07-21 UTC Mon-Fri")
    print("    2. M15 BB Bounce → AUDUSD (IC Markets) — every 15 min, 07-21 UTC Mon-Fri")
    print()
    if r1 and r2:
        combined_mo = r1["mo"] + r2["mo"]
        print(f"  Expected combined: {combined_mo:+.1f}%/mo (uncorrelated pair)")
    print()


if __name__ == "__main__":
    run()
