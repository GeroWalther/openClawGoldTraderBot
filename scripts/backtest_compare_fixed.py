"""Head-to-head: M5 Scalp vs M15 Sensei — FIXED position sizing (no compounding).

Uses fixed 3% of INITIAL balance per trade to get realistic expected monthly returns.
This removes compounding artifacts and shows true edge per trade.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_scalp_indicators, compute_sensei_indicators
from app.services.m5_scalp_scoring import M5ScalpScoringEngine, M5_SIGNAL_THRESHOLD
from app.services.m15_sensei_scoring import M15SenseiScoringEngine, M15_SENSEI_SIGNAL_THRESHOLD


def backtest_m5_scalp(m5_df, h1_df, risk_pct=3.0, initial=10000.0, debounce_bars=12):
    """M5 scalp: SL=1.0*ATR, no fixed TP, trailing (1R/0.5R). FIXED sizing."""
    engine = M5ScalpScoringEngine()
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)

    risk_amt = initial * risk_pct / 100  # FIXED $300 per trade
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
                    pnl = (sl - entry) * size
                    balance += pnl
                    trades.append({"pnl_r": (sl - entry) / sl_dist, "pnl": pnl, "dir": "BUY",
                                   "bars": i - ebar, "exit": "sl" if sl < entry else "trail_sl"})
                    in_trade = False
                elif (bh - entry) / sl_dist >= 1.0:
                    new_sl = bh - 0.5 * sl_dist
                    if new_sl > sl: sl = new_sl
            else:
                if bh >= sl:
                    pnl = (entry - sl) * size
                    balance += pnl
                    trades.append({"pnl_r": (entry - sl) / sl_dist, "pnl": pnl, "dir": "SELL",
                                   "bars": i - ebar, "exit": "sl" if sl > entry else "trail_sl"})
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
            if direction == "BUY":
                sl = entry - sl_dist
            else:
                sl = entry + sl_dist

            size = risk_amt / sl_dist if sl_dist > 0 else 0
            if size <= 0: continue
            in_trade = True
            last_bar = i
            last_dir = direction

    return trades, balance, initial, max_dd


def backtest_m15_sensei(m15_df, h1_df, risk_pct=3.0, initial=10000.0, debounce_bars=4):
    """M15 Sensei: SL=0.8*ATR, TP=3.0*ATR, trailing (1R/0.5R). FIXED sizing."""
    engine = M15SenseiScoringEngine()
    compute_indicators(m15_df)
    compute_sensei_indicators(m15_df)
    compute_indicators(h1_df)
    h1_df["sma100"] = h1_df["close"].rolling(100).mean()

    risk_amt = initial * risk_pct / 100  # FIXED $300 per trade
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
                    pnl = (sl - entry) * size
                    balance += pnl
                    r = (sl - entry) / sl_dist
                    trades.append({"pnl_r": r, "pnl": pnl, "dir": "BUY",
                                   "bars": i - ebar, "exit": "sl" if r < 0 else "trail_sl"})
                    in_trade = False
                elif bh >= tp:
                    pnl = (tp - entry) * size
                    balance += pnl
                    trades.append({"pnl_r": (tp - entry) / sl_dist, "pnl": pnl, "dir": "BUY",
                                   "bars": i - ebar, "exit": "tp"})
                    in_trade = False
                elif (bh - entry) / sl_dist >= 1.0:
                    new_sl = bh - 0.5 * sl_dist
                    if new_sl > sl: sl = new_sl
            else:
                if bh >= sl:
                    pnl = (entry - sl) * size
                    balance += pnl
                    r = (entry - sl) / sl_dist
                    trades.append({"pnl_r": r, "pnl": pnl, "dir": "SELL",
                                   "bars": i - ebar, "exit": "sl" if r < 0 else "trail_sl"})
                    in_trade = False
                elif bl <= tp:
                    pnl = (entry - tp) * size
                    balance += pnl
                    trades.append({"pnl_r": (entry - tp) / sl_dist, "pnl": pnl, "dir": "SELL",
                                   "bars": i - ebar, "exit": "tp"})
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
                tp = entry + 3.0 * atr
            else:
                sl = entry + sl_dist
                tp = entry - 3.0 * atr

            size = risk_amt / sl_dist if sl_dist > 0 else 0
            if size <= 0: continue
            in_trade = True
            last_bar = i
            last_dir = direction

    return trades, balance, initial, max_dd


def analyze(label, trades, balance, initial, max_dd, months):
    if not trades:
        return None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01

    # R-multiple analysis
    r_values = [t["pnl_r"] for t in trades]
    avg_r = np.mean(r_values)
    avg_win_r = np.mean([t["pnl_r"] for t in wins]) if wins else 0
    avg_loss_r = np.mean([t["pnl_r"] for t in losses]) if losses else 0

    tpm = len(trades) / max(months, 0.5)
    wr = len(wins) / len(trades) * 100

    # Expected R per trade
    expectancy_r = avg_r

    # Expected monthly return (% of account) = expectancy_R × risk% × trades_per_month
    risk_pct = 3.0
    expected_monthly_pct = expectancy_r * risk_pct * tpm

    # Consecutive losses
    mcl = 0
    consec = 0
    for t in trades:
        if t["pnl"] <= 0:
            consec += 1; mcl = max(mcl, consec)
        else:
            consec = 0

    exits = {}
    for t in trades:
        exits[t["exit"]] = exits.get(t["exit"], 0) + 1

    longs = sum(1 for t in trades if t["dir"] == "BUY")
    shorts = sum(1 for t in trades if t["dir"] == "SELL")

    ret = (balance - initial) / initial * 100

    return {
        "label": label,
        "trades": len(trades), "tpm": round(tpm, 1),
        "wr": round(wr, 1),
        "pf": round(w_pnl / l_pnl, 2),
        "ret": round(ret, 1),
        "dd": round(max_dd, 1),
        "avg_r": round(avg_r, 3),
        "avg_win_r": round(avg_win_r, 2),
        "avg_loss_r": round(avg_loss_r, 2),
        "expected_monthly_pct": round(expected_monthly_pct, 1),
        "mcl": mcl,
        "longs": longs, "shorts": shorts,
        "exits": exits,
    }


def run():
    instruments = {"BTC-USD": "BTC", "AUDUSD=X": "AUDUSD"}

    print("=" * 100)
    print("HEAD-TO-HEAD: M5 Scalp vs M15 Sensei  (FIXED position sizing, no compounding)")
    print("=" * 100)
    print("M5 Scalp:    SL=1.0*ATR, no TP, trailing (1R/0.5R), 3% risk per trade")
    print("M15 Sensei:  SL=0.8*ATR, TP=3.0*ATR, trailing (1R/0.5R), 3% risk per trade")
    print("Risk per trade: FIXED $300 (3% of $10k), no compounding")
    print()

    for sym, name in instruments.items():
        print(f"\n{'='*100}")
        print(f"  {name}")
        print(f"{'='*100}")

        try:
            m5_df = yf.download(sym, period="60d", interval="5m", progress=False)
            m15_df = yf.download(sym, period="60d", interval="15m", progress=False)
            h1_df = yf.download(sym, period="2y", interval="1h", progress=False)
            for df in [m5_df, m15_df, h1_df]:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
        except Exception as e:
            print(f"  Download failed: {e}")
            continue

        m5_days = (m5_df.index[-1] - m5_df.index[0]).days
        m15_days = (m15_df.index[-1] - m15_df.index[0]).days
        m5_months = max(m5_days / 30, 0.5)
        m15_months = max(m15_days / 30, 0.5)

        print(f"  M5: {len(m5_df)} bars ({m5_days} days)")
        print(f"  M15: {len(m15_df)} bars ({m15_days} days)")

        t1, b1, i1, d1 = backtest_m5_scalp(m5_df.copy(), h1_df.copy())
        r1 = analyze(f"{name} M5 Scalp", t1, b1, i1, d1, m5_months)

        t2, b2, i2, d2 = backtest_m15_sensei(m15_df.copy(), h1_df.copy())
        r2 = analyze(f"{name} M15 Sensei", t2, b2, i2, d2, m15_months)

        if not r1 or not r2:
            print("  No trades for one or both strategies")
            continue

        print(f"\n  {'Metric':<22s} | {'M5 Scalp':>14s} | {'M15 Sensei':>14s} | {'Winner':>12s}")
        print(f"  {'-'*22}-+-{'-'*14}-+-{'-'*14}-+-{'-'*12}")

        def row(metric, v1, v2, fmt, higher_wins=True):
            s1 = fmt.format(v1)
            s2 = fmt.format(v2)
            if higher_wins:
                w = "M5 SCALP" if v1 > v2 else ("M15 SENSEI" if v2 > v1 else "TIE")
            else:
                w = "M5 SCALP" if v1 < v2 else ("M15 SENSEI" if v2 < v1 else "TIE")
            print(f"  {metric:<22s} | {s1:>14s} | {s2:>14s} | {w:>12s}")

        row("Trades", r1["trades"], r2["trades"], "{}")
        row("Trades/month", r1["tpm"], r2["tpm"], "{:.1f}")
        row("Win Rate %", r1["wr"], r2["wr"], "{:.1f}%")
        row("Profit Factor", r1["pf"], r2["pf"], "{:.2f}")
        row("Avg Win (R)", r1["avg_win_r"], r2["avg_win_r"], "{:+.2f}R")
        row("Avg Loss (R)", r1["avg_loss_r"], r2["avg_loss_r"], "{:.2f}R", False)
        row("Expectancy (R/trade)", r1["avg_r"], r2["avg_r"], "{:+.3f}R")
        print(f"  {'-'*22}-+-{'-'*14}-+-{'-'*14}-+-{'-'*12}")
        row("EXPECTED MONTHLY %", r1["expected_monthly_pct"], r2["expected_monthly_pct"], "{:+.1f}%")
        print(f"  {'-'*22}-+-{'-'*14}-+-{'-'*14}-+-{'-'*12}")
        row("Return (fixed sizing)", r1["ret"], r2["ret"], "{:+.1f}%")
        row("Max DD %", r1["dd"], r2["dd"], "{:.1f}%", False)
        row("Max Consec Losses", r1["mcl"], r2["mcl"], "{}", False)
        rar1 = r1["ret"] / max(r1["dd"], 1)
        rar2 = r2["ret"] / max(r2["dd"], 1)
        row("Risk-Adj (Ret/DD)", rar1, rar2, "{:.2f}")
        print(f"  {'Longs/Shorts':<22s} | {r1['longs']}L/{r1['shorts']}S{'':>6s} | {r2['longs']}L/{r2['shorts']}S{'':>6s} |")
        print(f"  {'Exit breakdown':<22s} | {str(r1['exits']):>14s} | {str(r2['exits']):>14s} |")

        print(f"\n  VERDICT for {name}:", end=" ")
        if r1["expected_monthly_pct"] > r2["expected_monthly_pct"] * 1.1:
            print(f">> M5 SCALP ({r1['expected_monthly_pct']:+.1f}%/mo vs {r2['expected_monthly_pct']:+.1f}%/mo)")
        elif r2["expected_monthly_pct"] > r1["expected_monthly_pct"] * 1.1:
            print(f">> M15 SENSEI ({r2['expected_monthly_pct']:+.1f}%/mo vs {r1['expected_monthly_pct']:+.1f}%/mo)")
        else:
            print(f"Too close to call ({r1['expected_monthly_pct']:+.1f}% vs {r2['expected_monthly_pct']:+.1f}%)")


if __name__ == "__main__":
    run()
