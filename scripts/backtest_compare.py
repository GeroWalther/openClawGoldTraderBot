"""Head-to-head: M5 Scalp vs M15 Sensei on BTC and AUDUSD.

Uses the ACTUAL implemented scoring engines to generate signals.
M5 Scalp: SL=1.0*ATR, no fixed TP, trailing (activate@1R, trail@0.5R), 3% risk
M15 Sensei: SL=0.8*ATR, TP=3.0*ATR, trailing (activate@1R, trail@0.5R), 3% risk
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_scalp_indicators, compute_sensei_indicators
from app.services.m5_scalp_scoring import M5ScalpScoringEngine, M5_SIGNAL_THRESHOLD, M5_HIGH_CONVICTION_THRESHOLD
from app.services.m15_sensei_scoring import M15SenseiScoringEngine, M15_SENSEI_SIGNAL_THRESHOLD, M15_SENSEI_HIGH_CONVICTION_THRESHOLD


def backtest_m5_scalp(m5_df, h1_df, risk_pct=3.0, initial=10000.0, debounce_bars=12):
    """Backtest M5 scalp: SL=1.0*ATR, no fixed TP, trailing stop."""
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
                pnl_r = (bh - entry) / sl_dist
                if bl <= sl:
                    pnl = (sl - entry) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "BUY",
                                   "exit": "sl" if pnl < 0 else "trail_sl"})
                    in_trade = False
                elif pnl_r >= 1.0:
                    new_sl = bh - 0.5 * sl_dist
                    if new_sl > sl:
                        sl = new_sl
            else:
                pnl_r = (entry - bl) / sl_dist
                if bh >= sl:
                    pnl = (entry - sl) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "SELL",
                                   "exit": "sl" if pnl < 0 else "trail_sl"})
                    in_trade = False
                elif pnl_r >= 1.0:
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
            sl_dist = 1.0 * atr

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


def backtest_m15_sensei(m15_df, h1_df, risk_pct=3.0, initial=10000.0, debounce_bars=4):
    """Backtest M15 Sensei: SL=0.8*ATR, TP=3.0*ATR, trailing stop."""
    engine = M15SenseiScoringEngine()
    compute_indicators(m15_df)
    compute_sensei_indicators(m15_df)
    compute_indicators(h1_df)
    h1_df["sma100"] = h1_df["close"].rolling(100).mean()

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
                pnl_r = (bh - entry) / sl_dist
                if bl <= sl:
                    pnl = (sl - entry) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "BUY",
                                   "exit": "sl" if pnl < 0 else "trail_sl"})
                    in_trade = False
                elif bh >= tp:
                    pnl = (tp - entry) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "BUY", "exit": "tp"})
                    in_trade = False
                elif pnl_r >= 1.0:
                    new_sl = bh - 0.5 * sl_dist
                    if new_sl > sl: sl = new_sl
            else:
                pnl_r = (entry - bl) / sl_dist
                if bh >= sl:
                    pnl = (entry - sl) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "SELL",
                                   "exit": "sl" if pnl < 0 else "trail_sl"})
                    in_trade = False
                elif bl <= tp:
                    pnl = (entry - tp) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "SELL", "exit": "tp"})
                    in_trade = False
                elif pnl_r >= 1.0:
                    new_sl = bl + 0.5 * sl_dist
                    if new_sl < sl: sl = new_sl

            if balance > peak: peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

        if not in_trade:
            atr = m15_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                continue

            m15_time = m15_df.index[i]
            h1_mask = h1_index <= m15_time
            if not h1_mask.any():
                continue
            h1_row = h1_df.loc[h1_index[h1_mask][-1]]

            m15_slice = m15_df.iloc[:i+1]
            result = engine.score(h1_row, m15_slice)

            if result["direction"] is None:
                continue
            direction = result["direction"]

            if (i - last_bar) < debounce_bars and last_dir == direction:
                continue

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

            risk_amt = balance * risk_pct / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0
            if size <= 0: continue
            in_trade = True
            last_bar = i
            last_dir = direction

    return trades, balance, initial, max_dd


def summarize(label, trades, balance, initial, max_dd, months):
    if not trades:
        return {"label": label, "trades": 0, "wr": 0, "pf": 0, "ret": 0, "monthly": 0, "dd": 0, "longs": 0, "shorts": 0, "mcl": 0}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
    ret = (balance - initial) / initial * 100
    monthly = ret / max(months, 0.5)
    longs = sum(1 for t in trades if t["dir"] == "BUY")
    shorts = sum(1 for t in trades if t["dir"] == "SELL")
    mcl = 0
    consec = 0
    for t in trades:
        if t["pnl"] <= 0:
            consec += 1; mcl = max(mcl, consec)
        else:
            consec = 0

    exits = {}
    for t in trades:
        e = t.get("exit", "?")
        exits[e] = exits.get(e, 0) + 1

    return {
        "label": label,
        "trades": len(trades),
        "tpm": round(len(trades) / max(months, 0.5), 1),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "pf": round(w_pnl / l_pnl, 2),
        "ret": round(ret, 1),
        "monthly": round(monthly, 1),
        "dd": round(max_dd, 1),
        "longs": longs,
        "shorts": shorts,
        "mcl": mcl,
        "avg_bars": round(np.mean([t["bars"] for t in trades]), 1),
        "exits": exits,
    }


def run():
    instruments = {
        "BTC-USD": "BTC",
        "AUDUSD=X": "AUDUSD",
    }

    print("=" * 100)
    print("HEAD-TO-HEAD: M5 Scalp vs M15 Sensei")
    print("=" * 100)
    print("M5 Scalp:    SL=1.0*ATR, no fixed TP, trailing (1R activate, 0.5R trail), 3% risk")
    print("M15 Sensei:  SL=0.8*ATR, TP=3.0*ATR, trailing (1R activate, 0.5R trail), 3% risk")
    print()
    print("Downloading data...")

    for sym, name in instruments.items():
        print(f"\n{'='*100}")
        print(f"  {name} ({sym})")
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

        print(f"  M5:  {len(m5_df)} bars")
        print(f"  M15: {len(m15_df)} bars")
        print(f"  H1:  {len(h1_df)} bars")

        if len(m5_df) < 200 or len(m15_df) < 200 or len(h1_df) < 100:
            print("  Not enough data, skipping")
            continue

        # Calculate period in months
        m5_days = (m5_df.index[-1] - m5_df.index[0]).days
        m15_days = (m15_df.index[-1] - m15_df.index[0]).days
        m5_months = max(m5_days / 30, 0.5)
        m15_months = max(m15_days / 30, 0.5)

        # Run M5 Scalp
        t1, b1, i1, d1 = backtest_m5_scalp(m5_df.copy(), h1_df.copy())
        r1 = summarize(f"{name} M5 Scalp", t1, b1, i1, d1, m5_months)

        # Run M15 Sensei
        t2, b2, i2, d2 = backtest_m15_sensei(m15_df.copy(), h1_df.copy())
        r2 = summarize(f"{name} M15 Sensei", t2, b2, i2, d2, m15_months)

        # Print comparison table
        print(f"\n  {'Metric':<20s} | {'M5 Scalp':>15s} | {'M15 Sensei':>15s} | {'Winner':>10s}")
        print(f"  {'-'*20}-+-{'-'*15}-+-{'-'*15}-+-{'-'*10}")

        def row(metric, v1, v2, fmt, higher_wins=True):
            s1 = fmt.format(v1)
            s2 = fmt.format(v2)
            if higher_wins:
                w = "M5 Scalp" if v1 > v2 else ("M15 Sensei" if v2 > v1 else "Tie")
            else:
                w = "M5 Scalp" if v1 < v2 else ("M15 Sensei" if v2 < v1 else "Tie")
            print(f"  {metric:<20s} | {s1:>15s} | {s2:>15s} | {w:>10s}")

        row("Trades", r1["trades"], r2["trades"], "{}", True)
        row("Trades/mo", r1.get("tpm",0), r2.get("tpm",0), "{:.1f}", True)
        row("Win Rate %", r1["wr"], r2["wr"], "{:.1f}%", True)
        row("Profit Factor", r1["pf"], r2["pf"], "{:.2f}", True)
        row("Return %", r1["ret"], r2["ret"], "{:+.1f}%", True)
        row("Monthly %", r1["monthly"], r2["monthly"], "{:+.1f}%", True)
        row("Max DD %", r1["dd"], r2["dd"], "{:.1f}%", False)
        row("Max Consec Loss", r1["mcl"], r2["mcl"], "{}", False)
        row("Avg Bars in Trade", r1.get("avg_bars",0), r2.get("avg_bars",0), "{:.1f}", False)
        print(f"  {'Longs/Shorts':<20s} | {r1['longs']}L/{r1['shorts']}S{'':>7s} | {r2['longs']}L/{r2['shorts']}S{'':>7s} |")
        print(f"  {'Exits':<20s} | {str(r1.get('exits',{})):>15s} | {str(r2.get('exits',{})):>15s} |")

        # Risk-adjusted return (return / DD)
        rar1 = r1["ret"] / max(r1["dd"], 1) if r1["dd"] > 0 else 0
        rar2 = r2["ret"] / max(r2["dd"], 1) if r2["dd"] > 0 else 0
        row("Risk-Adj Return", rar1, rar2, "{:.2f}", True)

        print(f"\n  VERDICT for {name}:", end=" ")
        if r1["monthly"] > r2["monthly"] and r1["dd"] <= r2["dd"] * 1.2:
            print("M5 Scalp wins (higher return, acceptable DD)")
        elif r2["monthly"] > r1["monthly"] and r2["dd"] <= r1["dd"] * 1.2:
            print("M15 Sensei wins (higher return, acceptable DD)")
        elif rar2 > rar1:
            print("M15 Sensei wins (better risk-adjusted return)")
        elif rar1 > rar2:
            print("M5 Scalp wins (better risk-adjusted return)")
        else:
            print("Too close to call")


if __name__ == "__main__":
    run()
