"""Backtest ALL 4 live/ready strategies with exact production scoring engines:

  1. M5 Scalp NZDUSD (thresh=8, SL=2×ATR, ratchet)
  2. M5 Scalp AUDUSD (thresh=8, SL=2×ATR, ratchet)
  3. M5 Scalp BTC    (thresh=6, SL=1.5×ATR, ratchet) — disabled but ready
  4. M15 BB Bounce AUDUSD (thresh=8, SL=1.2×ATR, TP=2.5×ATR)

Usage: .venv/bin/python scripts/backtest_all_live.py
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


# ── Live config ──────────────────────────────────────────────────────────
INITIAL = 50.0
COSTS = {"NZDUSD=X": 0.00012, "AUDUSD=X": 0.00012, "BTC-USD": 30.0}
MIN_SIZE = {"NZDUSD=X": 1000, "AUDUSD=X": 1000, "BTC-USD": 0.01}
SIZE_RND = {"NZDUSD=X": 1000, "AUDUSD=X": 1000, "BTC-USD": 0.01}
MAX_POS = {"NZDUSD=X": 1000, "AUDUSD=X": 1000, "BTC-USD": 0.05}
RISK_HIGH = 3.0
RISK_MED = 2.25
MAX_DAILY_LOSS = 6.0
MAX_WEEKLY_LOSS = 15.0
COOLDOWN_LOSSES = 2
COOLDOWN_BARS = 2  # 10 min for M5


def fetch(symbol, period, interval):
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    return df


def in_session(ts):
    if not hasattr(ts, 'hour'):
        return True
    if hasattr(ts, 'weekday') and ts.weekday() >= 5:
        return False
    return 7 <= ts.hour < 21


def is_eod(ts):
    if not hasattr(ts, 'hour'):
        return False
    return ts.hour == 20 and ts.minute >= 55


def simulate(signals, df, sym, debounce, has_tp=False):
    cost = COSTS.get(sym, 0.00015)
    msz = MIN_SIZE.get(sym, 1000)
    srnd = SIZE_RND.get(sym, 1000)
    mpos = MAX_POS.get(sym, 1000)

    bal = INITIAL; peak = INITIAL; mdd = 0
    trades = []; in_trade = False; last_exit = -debounce - 1
    consec_losses = 0; cooldown_until = -1
    daily_pnl = 0.0; weekly_pnl = 0.0
    current_day = None; current_week = None

    signals = sorted(signals, key=lambda s: s[0])
    si = 0

    for i in range(len(df)):
        ts = df.index[i]
        if hasattr(ts, 'date'):
            d = ts.date()
            if current_day != d: daily_pnl = 0.0; current_day = d
            w = ts.isocalendar()[1]
            if current_week != w: weekly_pnl = 0.0; current_week = w

        if in_trade:
            bh, bl = df["high"].iloc[i], df["low"].iloc[i]
            closed = False; pnl = 0

            if is_eod(ts):
                c = df["close"].iloc[i]
                pnl = ((c - entry) if tdir == "BUY" else (entry - c)) * sz - cost * sz
                closed = True; etype = "eod"
            elif tdir == "BUY":
                if bl <= sl:
                    pnl = (sl - entry) * sz - cost * sz; closed = True; etype = "sl"
                elif has_tp and bh >= tp:
                    pnl = (tp - entry) * sz - cost * sz; closed = True; etype = "tp"
                elif not has_tp:
                    pr = (bh - entry) / sld
                    if pr >= 1.0:
                        ns = bh - 0.5 * sld
                        if ns > sl: sl = ns
            else:
                if bh >= sl:
                    pnl = (entry - sl) * sz - cost * sz; closed = True; etype = "sl"
                elif has_tp and bl <= tp:
                    pnl = (entry - tp) * sz - cost * sz; closed = True; etype = "tp"
                elif not has_tp:
                    pr = (entry - bl) / sld
                    if pr >= 1.0:
                        ns = bl + 0.5 * sld
                        if ns < sl: sl = ns

            if closed:
                bal += pnl; daily_pnl += pnl; weekly_pnl += pnl
                trades.append({"pnl": pnl, "dir": tdir, "exit": etype, "conv": tconv})
                in_trade = False; last_exit = i
                consec_losses = consec_losses + 1 if pnl <= 0 else 0

            if bal > peak: peak = bal
            dd = (peak - bal) / peak * 100 if peak > 0 else 0
            if dd > mdd: mdd = dd

        if not in_trade:
            while si < len(signals) and signals[si][0] <= i:
                sb, sd, s_sl, s_tp, s_conv = signals[si]; si += 1
                if sb != i or (i - last_exit) < debounce: continue
                if i < cooldown_until: continue
                if consec_losses >= COOLDOWN_LOSSES:
                    cooldown_until = i + COOLDOWN_BARS; consec_losses = 0; continue
                if daily_pnl < -(bal * MAX_DAILY_LOSS / 100): continue
                if weekly_pnl < -(bal * MAX_WEEKLY_LOSS / 100): continue
                if not in_session(ts): continue

                atr = df["atr"].iloc[i]
                if pd.isna(atr) or atr <= 0: continue

                entry = df["close"].iloc[i]; tdir = sd; tconv = s_conv
                sld = s_sl
                sl = entry - sld if sd == "BUY" else entry + sld
                tp = (entry + s_tp if sd == "BUY" else entry - s_tp) if s_tp else None

                risk_pct = RISK_HIGH if s_conv == "HIGH" else RISK_MED
                risk = bal * risk_pct / 100
                sz = risk / sld if sld > 0 else 0
                if srnd >= 1:
                    sz = max(round(sz / srnd) * srnd, msz)
                else:
                    sz = max(round(sz / srnd) * srnd, msz)
                sz = min(sz, mpos)
                if sz * sld > bal * 0.5 or sz <= 0: continue
                in_trade = True; break

    return trades, bal, mdd


def print_results(name, trades, bal, mdd, df):
    if not trades:
        print(f"  {name}: NO TRADES\n")
        return None

    days = (df.index[-1] - df.index[0]).days if len(df) > 1 else 1
    months = max(days / 30, 0.5)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wp = sum(t["pnl"] for t in wins) if wins else 0
    lp = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    ret = (bal - INITIAL) / INITIAL * 100
    mo = ret / months
    wr = len(wins) / len(trades) * 100
    pf = wp / lp
    pnls = [t["pnl"] for t in trades]
    sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls)) if len(pnls) > 1 and np.std(pnls) > 0 else 0

    sl_n = len([t for t in trades if t.get("exit") == "sl"])
    tp_n = len([t for t in trades if t.get("exit") == "tp"])
    eod_n = len([t for t in trades if t.get("exit") == "eod"])
    high_n = [t for t in trades if t.get("conv") == "HIGH"]
    med_n = [t for t in trades if t.get("conv") == "MEDIUM"]
    high_wr = len([t for t in high_n if t["pnl"] > 0]) / len(high_n) * 100 if high_n else 0
    med_wr = len([t for t in med_n if t["pnl"] > 0]) / len(med_n) * 100 if med_n else 0

    print(f"  {name}")
    print(f"    Trades:  {len(trades):>4d} ({len(trades)/months:.0f}/mo)  |  WR: {wr:.1f}%  |  PF: {pf:.2f}  |  Sharpe: {sharpe:.2f}")
    print(f"    Return:  {ret:>+7.1f}% ({mo:+.1f}%/mo)  |  Max DD: {mdd:.1f}%  |  Final: €{bal:.2f}")
    print(f"    Exits:   SL={sl_n}  TP={tp_n}  EOD={eod_n}  |  HIGH={len(high_n)}(WR {high_wr:.0f}%)  MED={len(med_n)}(WR {med_wr:.0f}%)")
    print()
    return {"name": name, "trades": len(trades), "tmo": len(trades)/months, "wr": wr, "pf": pf,
            "ret": ret, "mo": mo, "mdd": mdd, "sharpe": sharpe, "bal": bal}


def run():
    print("=" * 90)
    print("ALL STRATEGIES BACKTEST — Exact Production Implementation")
    print("=" * 90)
    print(f"Account: €{INITIAL} | Risk: HIGH={RISK_HIGH}%, MED={RISK_MED}%")
    print(f"Session: 07-21 UTC Mon-Fri | EOD close 20:55 | Loss limits: {MAX_DAILY_LOSS}%d / {MAX_WEEKLY_LOSS}%w")
    print()

    results = []

    # ── 1. M5 Scalp NZDUSD ──────────────────────────────────────────
    print("─" * 90)
    print("  M5 Scalp NZDUSD — thresh=8, SL=2.0×ATR, ratchet exit")
    print("─" * 90)

    engine_fx = M5ScalpScoringEngine(signal_threshold=8.0, high_conviction_threshold=11.0)
    m5_nzd = fetch("NZDUSD=X", "60d", "5m")
    h1_nzd = fetch("NZDUSD=X", "1mo", "1h")

    if not m5_nzd.empty and not h1_nzd.empty:
        compute_indicators(m5_nzd); compute_scalp_indicators(m5_nzd); compute_indicators(h1_nzd)
        h1i = h1_nzd.index; sigs = []
        for i in range(50, len(m5_nzd)):
            t = m5_nzd.index[i]
            if not in_session(t): continue
            mask = h1i <= t
            if not mask.any(): continue
            hr = h1_nzd.loc[h1i[mask][-1]]
            tail = m5_nzd.iloc[max(0, i-5):i+1]
            r = engine_fx.score(hr, tail, bar_time=t)
            if r["direction"] is None: continue
            atr = m5_nzd["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0: continue
            sld = max(2.0 * atr, 0.0005)
            sigs.append((i, r["direction"], sld, None, r["conviction"]))
        tr, bal, mdd = simulate(sigs, m5_nzd, "NZDUSD=X", debounce=12)
        r = print_results("M5 Scalp NZDUSD", tr, bal, mdd, m5_nzd)
        if r: results.append(r)

    # ── 2. M5 Scalp AUDUSD ──────────────────────────────────────────
    print("─" * 90)
    print("  M5 Scalp AUDUSD — thresh=8, SL=2.0×ATR, ratchet exit")
    print("─" * 90)

    m5_aud = fetch("AUDUSD=X", "60d", "5m")
    h1_aud = fetch("AUDUSD=X", "1mo", "1h")

    if not m5_aud.empty and not h1_aud.empty:
        compute_indicators(m5_aud); compute_scalp_indicators(m5_aud); compute_indicators(h1_aud)
        h1i = h1_aud.index; sigs = []
        for i in range(50, len(m5_aud)):
            t = m5_aud.index[i]
            if not in_session(t): continue
            mask = h1i <= t
            if not mask.any(): continue
            hr = h1_aud.loc[h1i[mask][-1]]
            tail = m5_aud.iloc[max(0, i-5):i+1]
            r = engine_fx.score(hr, tail, bar_time=t)
            if r["direction"] is None: continue
            atr = m5_aud["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0: continue
            sld = max(2.0 * atr, 0.0005)
            sigs.append((i, r["direction"], sld, None, r["conviction"]))
        tr, bal, mdd = simulate(sigs, m5_aud, "AUDUSD=X", debounce=12)
        r = print_results("M5 Scalp AUDUSD", tr, bal, mdd, m5_aud)
        if r: results.append(r)

    # ── 3. M5 Scalp BTC ─────────────────────────────────────────────
    print("─" * 90)
    print("  M5 Scalp BTC — thresh=6, SL=1.5×ATR, ratchet exit (currently DISABLED)")
    print("─" * 90)

    engine_btc = M5ScalpScoringEngine(signal_threshold=6.0, high_conviction_threshold=9.0)
    m5_btc = fetch("BTC-USD", "60d", "5m")
    h1_btc = fetch("BTC-USD", "1mo", "1h")

    if not m5_btc.empty and not h1_btc.empty:
        compute_indicators(m5_btc); compute_scalp_indicators(m5_btc); compute_indicators(h1_btc)
        h1i = h1_btc.index; sigs = []
        for i in range(50, len(m5_btc)):
            t = m5_btc.index[i]
            # BTC trades 24/7, no session filter
            mask = h1i <= t
            if not mask.any(): continue
            hr = h1_btc.loc[h1i[mask][-1]]
            tail = m5_btc.iloc[max(0, i-5):i+1]
            r = engine_btc.score(hr, tail, bar_time=t)
            if r["direction"] is None: continue
            atr = m5_btc["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0: continue
            sld = max(1.5 * atr, 250.0)  # BTC min stop $250
            sigs.append((i, r["direction"], sld, None, r["conviction"]))
        tr, bal, mdd = simulate(sigs, m5_btc, "BTC-USD", debounce=12)
        r = print_results("M5 Scalp BTC", tr, bal, mdd, m5_btc)
        if r: results.append(r)

    # ── 4. M15 BB Bounce AUDUSD ──────────────────────────────────────
    print("─" * 90)
    print("  M15 BB Bounce AUDUSD — thresh=8, SL=1.2×ATR, TP=2.5×ATR (currently DISABLED)")
    print("─" * 90)

    bb_engine = M15BBBounceScoringEngine(signal_threshold=8.0, high_conviction_threshold=11.0)
    m15_aud = fetch("AUDUSD=X", "60d", "15m")

    if not m15_aud.empty:
        compute_indicators(m15_aud)
        sigs = []
        for i in range(50, len(m15_aud)):
            t = m15_aud.index[i]
            if not in_session(t): continue
            tail = m15_aud.iloc[max(0, i-5):i+1]
            r = bb_engine.score(tail, bar_time=t)
            if r["direction"] is None: continue
            atr = m15_aud["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0: continue
            sld = max(1.2 * atr, 0.0005)
            tpd = max(2.5 * atr, 0.0010)
            sigs.append((i, r["direction"], sld, tpd, r["conviction"]))
        tr, bal, mdd = simulate(sigs, m15_aud, "AUDUSD=X", debounce=2, has_tp=True)
        r = print_results("M15 BB Bounce AUDUSD", tr, bal, mdd, m15_aud)
        if r: results.append(r)

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("SUMMARY")
    print("=" * 90)
    print(f"\n  {'Strategy':<25s} | {'T/mo':>5s} | {'WR%':>6s} | {'PF':>5s} | {'Mo/Ret':>9s} | {'MaxDD':>7s} | {'Sharpe':>6s} | {'Status':<10s}")
    print(f"  {'-'*25}-+-{'-'*5}-+-{'-'*6}-+-{'-'*5}-+-{'-'*9}-+-{'-'*7}-+-{'-'*6}-+-{'-'*10}")

    status_map = {
        "M5 Scalp NZDUSD": "LIVE",
        "M5 Scalp AUDUSD": "LIVE",
        "M5 Scalp BTC": "READY",
        "M15 BB Bounce AUDUSD": "DISABLED",
    }

    for r in results:
        status = status_map.get(r["name"], "?")
        verdict = "✓" if r["pf"] > 1.0 else "✗"
        print(f"  {r['name']:<25s} | {r['tmo']:>5.0f} | {r['wr']:>5.1f}% | {r['pf']:>5.2f} | {r['mo']:>+8.1f}% | {r['mdd']:>6.1f}% | {r['sharpe']:>6.2f} | {status:<10s} {verdict}")

    # Combined live estimate
    live = [r for r in results if status_map.get(r["name"]) == "LIVE"]
    if live:
        combined_mo = sum(r["mo"] for r in live)
        combined_tmo = sum(r["tmo"] for r in live)
        max_mdd = max(r["mdd"] for r in live)
        print(f"\n  Combined LIVE: ~{combined_mo:+.1f}%/mo, ~{combined_tmo:.0f} trades/mo, worst DD {max_mdd:.1f}%")

    print()


if __name__ == "__main__":
    run()
