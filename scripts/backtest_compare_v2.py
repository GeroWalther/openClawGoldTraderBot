"""M15 Sensei: TP vs No-TP comparison + realistic cost modeling.

Tests:
1. M15 Sensei WITH TP (3.0*ATR) — current config
2. M15 Sensei WITHOUT TP — trail-only like M5 scalp
3. M5 Scalp — baseline comparison

All with spread/slippage costs modeled:
- BTC spread: ~$50-80 per entry+exit (IC Markets CFD)
- AUDUSD spread: ~0.00012 per entry+exit
- Slippage: 0.5x spread additional
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_scalp_indicators, compute_sensei_indicators
from app.services.m5_scalp_scoring import M5ScalpScoringEngine
from app.services.m15_sensei_scoring import M15SenseiScoringEngine


# Realistic IC Markets CFD costs per round-trip (entry + exit) in price units
# BTC: IC Markets tight spread ~$15-20, slippage ~$5 on market orders
# AUDUSD: IC Markets raw spread ~0.6 pips + $3.50 commission = ~0.00009 effective
COSTS = {
    "BTC": {"spread": 18.0, "slippage": 5.0},          # ~$23 total per RT
    "AUDUSD": {"spread": 0.00006, "slippage": 0.00003}, # ~0.9 pips total per RT
}


def backtest_m5_scalp(m5_df, h1_df, inst, risk_pct=3.0, initial=10000.0, debounce_bars=12):
    engine = M5ScalpScoringEngine()
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)

    cost = COSTS.get(inst, {"spread": 0, "slippage": 0})
    rt_cost = cost["spread"] + cost["slippage"]

    risk_amt = initial * risk_pct / 100
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
                    raw_pnl = (sl - entry) * size
                    pnl = raw_pnl - rt_cost * size  # deduct cost
                    balance += pnl
                    trades.append({"pnl_r": pnl / risk_amt, "pnl": pnl, "dir": "BUY",
                                   "bars": i - ebar, "exit": "sl" if sl < entry else "trail_sl",
                                   "cost": rt_cost * size})
                    in_trade = False
                elif (bh - entry) / sl_dist >= 1.0:
                    new_sl = bh - 0.5 * sl_dist
                    if new_sl > sl: sl = new_sl
            else:
                if bh >= sl:
                    raw_pnl = (entry - sl) * size
                    pnl = raw_pnl - rt_cost * size
                    balance += pnl
                    trades.append({"pnl_r": pnl / risk_amt, "pnl": pnl, "dir": "SELL",
                                   "bars": i - ebar, "exit": "sl" if sl > entry else "trail_sl",
                                   "cost": rt_cost * size})
                    in_trade = False
                elif (entry - bl) / sl_dist >= 1.0:
                    new_sl = bl + 0.5 * sl_dist
                    if new_sl < sl: sl = new_sl

            if balance > peak: peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

        if not in_trade:
            atr = m5_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0: continue
            m5_time = m5_df.index[i]
            h1_mask = h1_index <= m5_time
            if not h1_mask.any(): continue
            h1_row = h1_df.loc[h1_index[h1_mask][-1]]
            m5_tail = m5_df.iloc[max(0, i-5):i+1]
            result = engine.score(h1_row, m5_tail, bar_time=m5_time)
            if result["direction"] is None: continue
            direction = result["direction"]
            if (i - last_bar) < debounce_bars and last_dir == direction: continue

            entry = m5_df["close"].iloc[i]
            ebar = i
            trade_dir = direction
            sl_dist = 1.0 * atr
            sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
            size = risk_amt / sl_dist if sl_dist > 0 else 0
            if size <= 0: continue
            in_trade = True
            last_bar = i
            last_dir = direction

    return trades, balance, initial, max_dd


def backtest_m15_sensei(m15_df, h1_df, inst, use_tp=True, risk_pct=3.0, initial=10000.0, debounce_bars=4):
    engine = M15SenseiScoringEngine()
    compute_indicators(m15_df)
    compute_sensei_indicators(m15_df)
    compute_indicators(h1_df)
    h1_df["sma100"] = h1_df["close"].rolling(100).mean()

    cost = COSTS.get(inst, {"spread": 0, "slippage": 0})
    rt_cost = cost["spread"] + cost["slippage"]

    risk_amt = initial * risk_pct / 100
    balance = initial
    peak = initial
    max_dd = 0
    trades = []
    in_trade = False
    last_bar = -debounce_bars - 1
    last_dir = None
    h1_index = h1_df.index

    for i in range(100, len(m15_df)):
        if in_trade:
            bh, bl = m15_df["high"].iloc[i], m15_df["low"].iloc[i]
            if trade_dir == "BUY":
                if bl <= sl:
                    raw_pnl = (sl - entry) * size
                    pnl = raw_pnl - rt_cost * size
                    balance += pnl
                    trades.append({"pnl_r": pnl / risk_amt, "pnl": pnl, "dir": "BUY",
                                   "bars": i - ebar, "exit": "sl" if sl < entry else "trail_sl",
                                   "cost": rt_cost * size})
                    in_trade = False
                elif use_tp and bh >= tp:
                    raw_pnl = (tp - entry) * size
                    pnl = raw_pnl - rt_cost * size
                    balance += pnl
                    trades.append({"pnl_r": pnl / risk_amt, "pnl": pnl, "dir": "BUY",
                                   "bars": i - ebar, "exit": "tp", "cost": rt_cost * size})
                    in_trade = False
                elif (bh - entry) / sl_dist >= 1.0:
                    new_sl = bh - 0.5 * sl_dist
                    if new_sl > sl: sl = new_sl
            else:
                if bh >= sl:
                    raw_pnl = (entry - sl) * size
                    pnl = raw_pnl - rt_cost * size
                    balance += pnl
                    trades.append({"pnl_r": pnl / risk_amt, "pnl": pnl, "dir": "SELL",
                                   "bars": i - ebar, "exit": "sl" if sl > entry else "trail_sl",
                                   "cost": rt_cost * size})
                    in_trade = False
                elif use_tp and bl <= tp:
                    raw_pnl = (entry - tp) * size
                    pnl = raw_pnl - rt_cost * size
                    balance += pnl
                    trades.append({"pnl_r": pnl / risk_amt, "pnl": pnl, "dir": "SELL",
                                   "bars": i - ebar, "exit": "tp", "cost": rt_cost * size})
                    in_trade = False
                elif (entry - bl) / sl_dist >= 1.0:
                    new_sl = bl + 0.5 * sl_dist
                    if new_sl < sl: sl = new_sl

            if balance > peak: peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

        if not in_trade:
            atr = m15_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0: continue
            m15_time = m15_df.index[i]
            h1_mask = h1_index <= m15_time
            if not h1_mask.any(): continue
            h1_row = h1_df.loc[h1_index[h1_mask][-1]]
            m15_slice = m15_df.iloc[:i+1]
            result = engine.score(h1_row, m15_slice)
            if result["direction"] is None: continue
            direction = result["direction"]
            if (i - last_bar) < debounce_bars and last_dir == direction: continue

            entry = m15_df["close"].iloc[i]
            ebar = i
            trade_dir = direction
            sl_dist = 0.8 * atr
            if direction == "BUY":
                sl = entry - sl_dist
                tp = entry + 3.0 * atr if use_tp else float("inf")
            else:
                sl = entry + sl_dist
                tp = entry - 3.0 * atr if use_tp else float("-inf")

            size = risk_amt / sl_dist if sl_dist > 0 else 0
            if size <= 0: continue
            in_trade = True
            last_bar = i
            last_dir = direction

    return trades, balance, initial, max_dd


def analyze(trades, risk_amt, months):
    if not trades:
        return None
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
    r_vals = [t["pnl_r"] for t in trades]
    tpm = len(trades) / max(months, 0.5)
    total_cost = sum(t.get("cost", 0) for t in trades)
    mcl = 0; consec = 0
    for t in trades:
        if t["pnl"] <= 0: consec += 1; mcl = max(mcl, consec)
        else: consec = 0
    exits = {}
    for t in trades:
        exits[t["exit"]] = exits.get(t["exit"], 0) + 1

    return {
        "n": len(trades), "tpm": round(tpm, 1),
        "wr": round(len(wins)/len(trades)*100, 1),
        "pf": round(w_pnl/l_pnl, 2),
        "avg_win_r": round(np.mean([t["pnl_r"] for t in wins]), 2) if wins else 0,
        "avg_loss_r": round(np.mean([t["pnl_r"] for t in losses]), 2) if losses else 0,
        "exp_r": round(np.mean(r_vals), 3),
        "exp_mo": round(np.mean(r_vals) * 3.0 * tpm, 1),
        "total_cost": round(total_cost, 0),
        "cost_per_trade": round(total_cost / len(trades), 1),
        "dd": round(max([0] + [(max(0, sum(t["pnl"] for t in trades[:j+1]) - sum(t["pnl"] for t in trades[:k+1])) / 10000 * 100) for j in range(len(trades)) for k in range(j, min(j+30, len(trades)))]), 1) if trades else 0,
        "mcl": mcl,
        "exits": exits,
    }


def run():
    instruments = {"BTC-USD": "BTC", "AUDUSD=X": "AUDUSD"}

    print("=" * 110)
    print("M15 SENSEI: TP vs NO-TP + COST ANALYSIS")
    print("=" * 110)
    print(f"Costs modeled: BTC spread=${COSTS['BTC']['spread']}, slip=${COSTS['BTC']['slippage']} | "
          f"AUDUSD spread={COSTS['AUDUSD']['spread']}, slip={COSTS['AUDUSD']['slippage']}")
    print("Fixed sizing: $300 risk per trade (3% of $10k), no compounding")
    print()

    for sym, name in instruments.items():
        print(f"\n{'='*110}")
        print(f"  {name}")
        print(f"{'='*110}")

        try:
            m5_df = yf.download(sym, period="60d", interval="5m", progress=False)
            m15_df = yf.download(sym, period="60d", interval="15m", progress=False)
            h1_df = yf.download(sym, period="2y", interval="1h", progress=False)
            for df in [m5_df, m15_df, h1_df]:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
        except Exception as e:
            print(f"  Download failed: {e}"); continue

        days = (m15_df.index[-1] - m15_df.index[0]).days
        months = max(days / 30, 0.5)
        m5_days = (m5_df.index[-1] - m5_df.index[0]).days
        m5_months = max(m5_days / 30, 0.5)
        print(f"  Data: {days} days ({months:.1f} months), M5={len(m5_df)} bars, M15={len(m15_df)} bars")

        # Run all three variants
        t1, b1, _, d1 = backtest_m5_scalp(m5_df.copy(), h1_df.copy(), name)
        t2, b2, _, d2 = backtest_m15_sensei(m15_df.copy(), h1_df.copy(), name, use_tp=True)
        t3, b3, _, d3 = backtest_m15_sensei(m15_df.copy(), h1_df.copy(), name, use_tp=False)

        r1 = analyze(t1, 300, m5_months)
        r2 = analyze(t2, 300, months)
        r3 = analyze(t3, 300, months)

        if not r1 or not r2 or not r3:
            print("  Insufficient trades"); continue

        print(f"\n  {'Metric':<22s} | {'M5 Scalp':>16s} | {'Sensei+TP':>16s} | {'Sensei NO-TP':>16s}")
        print(f"  {'-'*22}-+-{'-'*16}-+-{'-'*16}-+-{'-'*16}")

        def row(m, v1, v2, v3, fmt, hi=True):
            s1, s2, s3 = fmt.format(v1), fmt.format(v2), fmt.format(v3)
            vals = [v1, v2, v3]
            best = max(vals) if hi else min(vals)
            markers = ["" for _ in range(3)]
            for j in range(3):
                if vals[j] == best and vals.count(best) == 1:
                    markers[j] = " <<<"
            print(f"  {m:<22s} | {s1+markers[0]:>16s} | {s2+markers[1]:>16s} | {s3+markers[2]:>16s}")

        row("Trades", r1["n"], r2["n"], r3["n"], "{}")
        row("Trades/month", r1["tpm"], r2["tpm"], r3["tpm"], "{:.1f}")
        row("Win Rate %", r1["wr"], r2["wr"], r3["wr"], "{:.1f}%")
        row("Profit Factor", r1["pf"], r2["pf"], r3["pf"], "{:.2f}")
        row("Avg Win (R)", r1["avg_win_r"], r2["avg_win_r"], r3["avg_win_r"], "{:+.2f}R")
        row("Avg Loss (R)", r1["avg_loss_r"], r2["avg_loss_r"], r3["avg_loss_r"], "{:.2f}R", False)
        row("Expectancy (R/trade)", r1["exp_r"], r2["exp_r"], r3["exp_r"], "{:+.3f}R")
        print(f"  {'-'*22}-+-{'-'*16}-+-{'-'*16}-+-{'-'*16}")
        row("EXPECTED MONTHLY %", r1["exp_mo"], r2["exp_mo"], r3["exp_mo"], "{:+.1f}%")
        print(f"  {'-'*22}-+-{'-'*16}-+-{'-'*16}-+-{'-'*16}")
        row("Max DD %", d1, d2, d3, "{:.1f}%", False)
        row("Max Consec Losses", r1["mcl"], r2["mcl"], r3["mcl"], "{}", False)
        row("Total Costs ($)", r1["total_cost"], r2["total_cost"], r3["total_cost"], "${:.0f}", False)
        row("Cost/Trade ($)", r1["cost_per_trade"], r2["cost_per_trade"], r3["cost_per_trade"], "${:.1f}", False)

        # Cost impact
        print(f"\n  Cost Impact:")
        for label, r, tpm in [("M5 Scalp", r1, r1["tpm"]), ("Sensei+TP", r2, r2["tpm"]), ("Sensei NO-TP", r3, r3["tpm"])]:
            cost_monthly = r["cost_per_trade"] * tpm
            cost_pct = cost_monthly / 10000 * 100
            print(f"    {label:14s}: ${cost_monthly:,.0f}/mo costs = {cost_pct:.1f}% of account/mo")

        print(f"\n  Exit breakdown:")
        for label, r in [("M5 Scalp", r1), ("Sensei+TP", r2), ("Sensei NO-TP", r3)]:
            print(f"    {label:14s}: {r['exits']}")

        # Final verdict
        print(f"\n  {'='*60}")
        exps = [(r1["exp_mo"], "M5 Scalp", d1), (r2["exp_mo"], "Sensei+TP", d2), (r3["exp_mo"], "Sensei NO-TP", d3)]
        exps.sort(key=lambda x: x[0], reverse=True)
        print(f"  RANKING for {name}:")
        for rank, (exp, label, dd) in enumerate(exps, 1):
            print(f"    {rank}. {label:14s}  →  {exp:+.1f}%/mo expected  (DD {dd:.1f}%)")


if __name__ == "__main__":
    run()
