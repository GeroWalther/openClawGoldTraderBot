"""Scan forex pairs for M5 scalp: find low-DD pair for system testing.

Tests M5 scalp (actual scoring engine) on major forex pairs with costs.
Goal: find a boring, reliable pair with low DD and some profit.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_scalp_indicators
from app.services.m5_scalp_scoring import M5ScalpScoringEngine, M5_SIGNAL_THRESHOLD


# Costs per round-trip (spread + slippage) in price units
COSTS = {
    "EURUSD": 0.00012,   # 1.2 pip spread
    "GBPUSD": 0.00016,   # 1.6 pip
    "AUDUSD": 0.00009,   # 0.9 pip
    "NZDUSD": 0.00012,   # 1.2 pip
    "USDCAD": 0.00016,   # 1.6 pip
    "USDCHF": 0.00014,   # 1.4 pip
    "EURGBP": 0.00014,   # 1.4 pip
}


def backtest_m5(m5_df, h1_df, cost_per_rt, risk_pct=3.0, initial=10000.0, debounce_bars=12):
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
                    pnl = (sl - entry) * size - cost_per_rt * size
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
                    pnl = (entry - sl) * size - cost_per_rt * size
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
            ebar = i
            trade_dir = direction
            sl_dist = max(1.0 * atr, 0.0005)  # min stop for forex

            if direction == "BUY":
                sl = entry - sl_dist
            else:
                sl = entry + sl_dist

            risk_amt = balance * risk_pct / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0
            if size <= 0: continue
            in_trade = True
            last_bar = i
            last_dir = direction

    return trades, balance, initial, max_dd


def run():
    pairs = {
        "EURUSD=X": ("EURUSD", COSTS["EURUSD"]),
        "GBPUSD=X": ("GBPUSD", COSTS["GBPUSD"]),
        "AUDUSD=X": ("AUDUSD", COSTS["AUDUSD"]),
        "NZDUSD=X": ("NZDUSD", COSTS["NZDUSD"]),
        "USDCAD=X": ("USDCAD", COSTS["USDCAD"]),
        "USDCHF=X": ("USDCHF", COSTS["USDCHF"]),
        "EURGBP=X": ("EURGBP", COSTS["EURGBP"]),
    }

    print("=" * 90)
    print("M5 SCALP FOREX SCAN — Finding low-DD pair for system testing")
    print("=" * 90)
    print("SL=1.0*ATR, trail (1R activate, 0.5R trail), 3% risk, with spreads")
    print()

    results = []

    for sym, (name, cost) in pairs.items():
        print(f"  Downloading {name}...", end=" ", flush=True)
        try:
            m5_df = yf.download(sym, period="60d", interval="5m", progress=False)
            h1_df = yf.download(sym, period="2y", interval="1h", progress=False)

            for df in [m5_df, h1_df]:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
        except Exception as e:
            print(f"FAILED: {e}")
            continue

        if len(m5_df) < 200 or len(h1_df) < 100:
            print(f"not enough data (M5={len(m5_df)}, H1={len(h1_df)})")
            continue

        days = (m5_df.index[-1] - m5_df.index[0]).days
        months = max(days / 30, 0.5)

        trades, balance, initial, max_dd = backtest_m5(m5_df.copy(), h1_df.copy(), cost)

        if not trades:
            print("0 trades")
            results.append({"name": name, "trades": 0, "ret": 0, "monthly": 0, "dd": 0, "wr": 0, "pf": 0})
            continue

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        w_pnl = sum(t["pnl"] for t in wins) if wins else 0
        l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
        ret = (balance - initial) / initial * 100
        monthly = ret / months
        wr = len(wins) / len(trades) * 100
        pf = w_pnl / l_pnl

        print(f"{len(trades)} trades, {wr:.0f}% WR, PF {pf:.2f}, {ret:+.1f}% ({monthly:+.1f}%/mo), DD {max_dd:.1f}%")

        results.append({
            "name": name, "trades": len(trades), "ret": ret, "monthly": monthly,
            "dd": max_dd, "wr": wr, "pf": pf, "months": months
        })

    print(f"\n{'=' * 90}")
    print(f"  {'Pair':<10s} | {'Trades':>7s} | {'WR%':>6s} | {'PF':>6s} | {'Return':>10s} | {'Monthly':>10s} | {'Max DD':>8s} | {'Score':>6s}")
    print(f"  {'-'*10}-+-{'-'*7}-+-{'-'*6}-+-{'-'*6}-+-{'-'*10}-+-{'-'*10}-+-{'-'*8}-+-{'-'*6}")

    for r in sorted(results, key=lambda x: (-x["monthly"] if x["dd"] < 50 else -999)):
        # Score: monthly return / max(DD, 1) — higher is better risk-adjusted
        score = r["monthly"] / max(r["dd"], 1) if r["dd"] > 0 else 0
        flag = " <-- BEST" if r == sorted(results, key=lambda x: x["monthly"] / max(x["dd"], 1) if x["dd"] > 0 and x["trades"] > 5 else -999)[-1] and r["trades"] > 5 else ""
        print(f"  {r['name']:<10s} | {r['trades']:>7d} | {r['wr']:>5.1f}% | {r['pf']:>6.2f} | {r['ret']:>+9.1f}% | {r['monthly']:>+9.1f}% | {r['dd']:>7.1f}% | {score:>6.2f}{flag}")

    print(f"\n  Score = Monthly% / MaxDD% (higher = better risk-adjusted)")
    print(f"  Looking for: positive monthly, DD < 50%, decent score")


if __name__ == "__main__":
    run()
