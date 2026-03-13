"""Test NZDUSD + AUDUSD running simultaneously.

Key question: do they take the same trades at the same time (correlated)?
If so, dual = 2× risk. If uncorrelated, dual = diversification + more profit.
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

EURUSD_RATE = 1.09


def is_eod(bar_time):
    return bar_time.hour > 20 or (bar_time.hour == 20 and bar_time.minute >= 55)


def is_overnight(prev_time, curr_time):
    if prev_time is None:
        return False
    return prev_time.date() != curr_time.date()


def _record_loss(pnl, bar_time, daily_losses, weekly_losses):
    if pnl < 0:
        loss = abs(pnl)
        daily_losses[bar_time.date()] = daily_losses.get(bar_time.date(), 0) + loss
        wk = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
        weekly_losses[wk] = weekly_losses.get(wk, 0) + loss


def backtest_dual(pairs_data, initial, risk_per_pair_pct=2.25):
    """Run multiple pairs on a shared account with shared risk limits.

    Each pair gets its own position slot but shares the balance and loss limits.
    risk_per_pair_pct: risk % per trade per pair (split from total).
    """
    engine = M5ScalpScoringEngine(signal_threshold=8.0, high_conviction_threshold=11.0)

    # Prepare data
    for name, d in pairs_data.items():
        compute_indicators(d["m5"])
        compute_scalp_indicators(d["m5"])
        compute_indicators(d["h1"])

    balance = initial
    peak = initial
    lowest = initial
    max_dd = 0
    all_trades = []
    daily_losses = {}
    weekly_losses = {}
    consecutive_losses = 0
    cooldown_until = None

    # Per-pair state
    positions = {name: None for name in pairs_data}

    # Build unified timeline from all M5 bars
    all_times = set()
    for name, d in pairs_data.items():
        all_times.update(d["m5"].index[50:])
    all_times = sorted(all_times)

    prev_bar_time = None
    for bar_time in all_times:
        # Swap
        if prev_bar_time and is_overnight(prev_bar_time, bar_time):
            for name, pos in positions.items():
                if pos:
                    swap = 0.05 * (pos["size"] / 1000)
                    balance -= swap

        for name, d in pairs_data.items():
            m5 = d["m5"]
            h1 = d["h1"]
            spec = d["spec"]

            if bar_time not in m5.index:
                continue
            i = m5.index.get_loc(bar_time)
            if i < 50:
                continue

            bh, bl, bc = m5["high"].iloc[i], m5["low"].iloc[i], m5["close"].iloc[i]
            pos = positions[name]

            # EOD close
            if is_eod(bar_time) and pos:
                if pos["dir"] == "BUY":
                    pnl = (bc - pos["entry"]) * pos["size"] - spec["spread"] * pos["size"]
                else:
                    pnl = (pos["entry"] - bc) * pos["size"] - spec["spread"] * pos["size"]
                balance += pnl
                _record_loss(pnl, bar_time, daily_losses, weekly_losses)
                if pnl <= 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0
                all_trades.append({"pnl": pnl, "pair": name, "dir": pos["dir"],
                                   "reason": "eod", "date": str(bar_time),
                                   "ratchet": pos["ratchet"]})
                positions[name] = None
                pos = None

            # Manage position
            if pos:
                sl_hit = False
                if pos["dir"] == "BUY":
                    if bl <= pos["sl"]:
                        pnl = (pos["sl"] - pos["entry"]) * pos["size"] - spec["spread"] * pos["size"]
                        sl_hit = True
                    else:
                        pr = (bh - pos["entry"]) / pos["sl_dist"]
                        rl = max(0, int(math.floor(pr)))
                        if rl >= 1 and rl > pos["ratchet"]:
                            new_sl = pos["entry"] + rl * 0.5 * pos["sl_dist"]
                            if new_sl > pos["sl"]:
                                pos["sl"] = new_sl
                                pos["ratchet"] = rl
                else:
                    if bh >= pos["sl"]:
                        pnl = (pos["entry"] - pos["sl"]) * pos["size"] - spec["spread"] * pos["size"]
                        sl_hit = True
                    else:
                        pr = (pos["entry"] - bl) / pos["sl_dist"]
                        rl = max(0, int(math.floor(pr)))
                        if rl >= 1 and rl > pos["ratchet"]:
                            new_sl = pos["entry"] - rl * 0.5 * pos["sl_dist"]
                            if new_sl < pos["sl"]:
                                pos["sl"] = new_sl
                                pos["ratchet"] = rl

                if sl_hit:
                    balance += pnl
                    _record_loss(pnl, bar_time, daily_losses, weekly_losses)
                    if pnl <= 0:
                        consecutive_losses += 1
                        if consecutive_losses >= 2:
                            cooldown_until = bar_time + pd.Timedelta(minutes=10)
                    else:
                        consecutive_losses = 0
                    all_trades.append({"pnl": pnl, "pair": name, "dir": pos["dir"],
                                       "reason": "sl", "date": str(bar_time),
                                       "ratchet": pos["ratchet"]})
                    positions[name] = None

            # Equity
            if balance < lowest: lowest = balance
            if balance > peak: peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

            # New entry
            if positions[name] is not None or is_eod(bar_time):
                continue

            atr = m5["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                continue

            # Cooldown (shared)
            if cooldown_until and bar_time < cooldown_until:
                continue

            # Daily/weekly limits (shared)
            day_loss = daily_losses.get(bar_time.date(), 0)
            if day_loss > 0 and day_loss >= balance * 6.0 / 100:
                continue
            wk = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
            week_loss = weekly_losses.get(wk, 0)
            if week_loss > 0 and week_loss >= balance * 15.0 / 100:
                continue

            h1_index = h1.index
            h1_mask = h1_index <= bar_time
            if not h1_mask.any():
                continue
            h1_row = h1.loc[h1_index[h1_mask][-1]]
            m5_tail = m5.iloc[max(0, i - 5):i + 1]
            result = engine.score(h1_row, m5_tail, bar_time=bar_time)

            if result["direction"] is None:
                continue

            direction = result["direction"]
            conviction = result["conviction"]
            entry = bc
            sl_dist = max(2.0 * atr, spec["min_stop"])

            # Spread filter
            if spec["spread"] / sl_dist > 0.40:
                continue

            # Risk sizing — use per-pair risk
            risk_map = {"HIGH": 3.0, "MEDIUM": risk_per_pair_pct, "LOW": risk_per_pair_pct * 0.67}
            risk_pct = risk_map.get(conviction, risk_per_pair_pct)
            risk_amt = balance * risk_pct / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0

            max_affordable = balance * 0.8 * 30 * EURUSD_RATE
            max_size = max_affordable / entry if entry > 0 else 0
            size = min(size, max_size)
            size = max(round(size / 1000) * 1000, 1000)
            size = min(size, 500000)

            if size * sl_dist > balance * 0.5 or size <= 0:
                continue

            sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
            positions[name] = {"dir": direction, "entry": entry, "sl": sl,
                               "sl_dist": sl_dist, "size": size, "ratchet": 0}

        prev_bar_time = bar_time

    # Close remaining
    for name, pos in positions.items():
        if pos:
            m5 = pairs_data[name]["m5"]
            spec = pairs_data[name]["spec"]
            lc = m5["close"].iloc[-1]
            if pos["dir"] == "BUY":
                pnl = (lc - pos["entry"]) * pos["size"] - spec["spread"] * pos["size"]
            else:
                pnl = (pos["entry"] - lc) * pos["size"] - spec["spread"] * pos["size"]
            balance += pnl
            all_trades.append({"pnl": pnl, "pair": name, "dir": pos["dir"],
                               "reason": "end", "date": str(m5.index[-1]),
                               "ratchet": pos.get("ratchet", 0)})

    return {"trades": all_trades, "balance": balance, "lowest": lowest,
            "max_dd": max_dd}


def backtest_single(m5_df, h1_df, initial, spec):
    """Single pair backtest for comparison."""
    engine = M5ScalpScoringEngine(signal_threshold=8.0, high_conviction_threshold=11.0)
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)

    balance = initial
    peak = initial
    lowest = initial
    max_dd = 0
    trades = []
    h1_index = h1_df.index
    pos = None
    consecutive_losses = 0
    cooldown_until = None
    daily_losses = {}
    weekly_losses = {}
    prev_bar_time = None

    for i in range(50, len(m5_df)):
        bar_time = m5_df.index[i]
        bh, bl, bc = m5_df["high"].iloc[i], m5_df["low"].iloc[i], m5_df["close"].iloc[i]

        if is_overnight(prev_bar_time, bar_time) and pos:
            balance -= 0.05 * (pos["size"] / 1000)

        if is_eod(bar_time) and pos:
            if pos["dir"] == "BUY":
                pnl = (bc - pos["entry"]) * pos["size"] - spec["spread"] * pos["size"]
            else:
                pnl = (pos["entry"] - bc) * pos["size"] - spec["spread"] * pos["size"]
            balance += pnl
            _record_loss(pnl, bar_time, daily_losses, weekly_losses)
            if pnl <= 0: consecutive_losses += 1
            else: consecutive_losses = 0
            trades.append({"pnl": pnl, "reason": "eod", "date": str(bar_time)})
            pos = None

        if pos:
            sl_hit = False
            if pos["dir"] == "BUY":
                if bl <= pos["sl"]:
                    pnl = (pos["sl"] - pos["entry"]) * pos["size"] - spec["spread"] * pos["size"]
                    sl_hit = True
                else:
                    pr = (bh - pos["entry"]) / pos["sl_dist"]
                    rl = max(0, int(math.floor(pr)))
                    if rl >= 1 and rl > pos["ratchet"]:
                        ns = pos["entry"] + rl * 0.5 * pos["sl_dist"]
                        if ns > pos["sl"]: pos["sl"] = ns; pos["ratchet"] = rl
            else:
                if bh >= pos["sl"]:
                    pnl = (pos["entry"] - pos["sl"]) * pos["size"] - spec["spread"] * pos["size"]
                    sl_hit = True
                else:
                    pr = (pos["entry"] - bl) / pos["sl_dist"]
                    rl = max(0, int(math.floor(pr)))
                    if rl >= 1 and rl > pos["ratchet"]:
                        ns = pos["entry"] - rl * 0.5 * pos["sl_dist"]
                        if ns < pos["sl"]: pos["sl"] = ns; pos["ratchet"] = rl
            if sl_hit:
                balance += pnl
                _record_loss(pnl, bar_time, daily_losses, weekly_losses)
                if pnl <= 0:
                    consecutive_losses += 1
                    if consecutive_losses >= 2:
                        cooldown_until = bar_time + pd.Timedelta(minutes=10)
                else: consecutive_losses = 0
                trades.append({"pnl": pnl, "reason": "sl", "date": str(bar_time)})
                pos = None

        if balance < lowest: lowest = balance
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

        if pos is not None or is_eod(bar_time):
            prev_bar_time = bar_time
            continue
        atr = m5_df["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            prev_bar_time = bar_time
            continue
        if cooldown_until and bar_time < cooldown_until:
            prev_bar_time = bar_time
            continue
        dl = daily_losses.get(bar_time.date(), 0)
        if dl > 0 and dl >= balance * 6.0 / 100:
            prev_bar_time = bar_time
            continue
        wk = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
        wl = weekly_losses.get(wk, 0)
        if wl > 0 and wl >= balance * 15.0 / 100:
            prev_bar_time = bar_time
            continue

        h1_mask = h1_index <= bar_time
        if not h1_mask.any():
            prev_bar_time = bar_time
            continue
        h1_row = h1_df.loc[h1_index[h1_mask][-1]]
        m5_tail = m5_df.iloc[max(0, i - 5):i + 1]
        result = engine.score(h1_row, m5_tail, bar_time=bar_time)
        if result["direction"] is None:
            prev_bar_time = bar_time
            continue

        direction = result["direction"]
        conviction = result["conviction"]
        entry = bc
        sl_dist = max(2.0 * atr, spec["min_stop"])
        if spec["spread"] / sl_dist > 0.40:
            prev_bar_time = bar_time
            continue

        risk_map = {"HIGH": 3.0, "MEDIUM": 2.25, "LOW": 1.5}
        risk_pct = risk_map.get(conviction, 2.25)
        risk_amt = balance * risk_pct / 100
        size = risk_amt / sl_dist if sl_dist > 0 else 0
        max_aff = balance * 0.8 * 30 * EURUSD_RATE
        size = min(size, max_aff / entry if entry > 0 else 0)
        size = max(round(size / 1000) * 1000, 1000)
        size = min(size, 500000)
        if size * sl_dist > balance * 0.5 or size <= 0:
            prev_bar_time = bar_time
            continue

        sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
        pos = {"dir": direction, "entry": entry, "sl": sl,
               "sl_dist": sl_dist, "size": size, "ratchet": 0}
        prev_bar_time = bar_time

    if pos:
        lc = m5_df["close"].iloc[-1]
        if pos["dir"] == "BUY":
            pnl = (lc - pos["entry"]) * pos["size"] - spec["spread"] * pos["size"]
        else:
            pnl = (pos["entry"] - lc) * pos["size"] - spec["spread"] * pos["size"]
        balance += pnl
        trades.append({"pnl": pnl, "reason": "end", "date": str(m5_df.index[-1])})

    return {"trades": trades, "balance": balance, "lowest": lowest, "max_dd": max_dd}


def print_summary(label, r, initial, months):
    trades = r["trades"]
    if not trades:
        print(f"  {label}: NO TRADES")
        return
    balance = r["balance"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
    ret = (balance - initial) / initial * 100
    monthly = ret / months
    wr = len(wins) / len(trades) * 100
    pf = w_pnl / l_pnl
    aw = w_pnl / len(wins) if wins else 0
    al = l_pnl / len(losses) if losses else 0

    # Per-pair breakdown for dual
    pair_trades = {}
    for t in trades:
        p = t.get("pair", "single")
        pair_trades.setdefault(p, []).append(t)

    print(f"\n{'=' * 70}")
    print(f"  {label}")
    print(f"{'=' * 70}")
    print(f"  Trades: {len(trades)} ({len(wins)}W / {len(losses)}L) — {len(trades)/months:.0f}/mo")
    print(f"  Win Rate: {wr:.1f}% | PF: {pf:.2f}")
    print(f"  Avg Win: €{aw:.2f} | Avg Loss: €{al:.2f} | Ratio: {aw/al:.2f}x" if al > 0 else "")
    print(f"  Balance: €{balance:,.2f} (from €{initial:,.0f}, low: €{r['lowest']:,.2f})")
    print(f"  Return: {ret:+.1f}% | Monthly: {monthly:+.1f}%/mo | Max DD: {r['max_dd']:.1f}%")

    if len(pair_trades) > 1:
        print(f"\n  Per-pair breakdown:")
        for p in sorted(pair_trades):
            pt = pair_trades[p]
            pw = [t for t in pt if t["pnl"] > 0]
            pp = sum(t["pnl"] for t in pt)
            pwr = len(pw) / len(pt) * 100
            print(f"    {p}: {len(pt)} trades ({len(pw)}W/{len(pt)-len(pw)}L) "
                  f"WR:{pwr:.0f}% P&L:€{pp:+.2f}")

        # Correlation: how many bars had BOTH pairs in a position?
        # Check same-direction trades within 30 min of each other
        overlap = 0
        for i, t1 in enumerate(trades):
            if t1.get("pair") == list(pair_trades.keys())[0]:
                for t2 in trades:
                    if t2.get("pair") != t1.get("pair") and t2.get("dir") == t1.get("dir"):
                        d1 = pd.Timestamp(t1["date"][:19])
                        d2 = pd.Timestamp(t2["date"][:19])
                        if abs((d1 - d2).total_seconds()) < 1800:
                            overlap += 1
                            break
        print(f"\n  Same-direction overlap (within 30min): {overlap} trades")

    # Monthly breakdown
    monthly_pnl = {}
    for t in trades:
        mo = t["date"][:7]
        monthly_pnl.setdefault(mo, {"pnl": 0, "n": 0, "w": 0})
        monthly_pnl[mo]["pnl"] += t["pnl"]
        monthly_pnl[mo]["n"] += 1
        if t["pnl"] > 0: monthly_pnl[mo]["w"] += 1

    print(f"\n  Monthly PnL:")
    running = initial
    for mo in sorted(monthly_pnl):
        d = monthly_pnl[mo]
        running += d["pnl"]
        wr = d["w"] / d["n"] * 100 if d["n"] > 0 else 0
        print(f"    {mo}: €{d['pnl']:>+9.2f}  {d['n']:>3} trades  {wr:.0f}% WR  bal: €{running:,.2f}")

    if monthly > 0:
        print(f"\n  Projections ({monthly:+.1f}%/mo):")
        for m, l in [(3, "3mo"), (6, "6mo"), (12, "12mo")]:
            print(f"    {l}: €{initial*(1+monthly/100)**m:,.0f}")

    return {"label": label, "balance": balance, "ret": ret, "monthly": monthly,
            "wr": wr, "pf": pf, "dd": r["max_dd"], "trades": len(trades)}


def run():
    INITIAL = 1000.0

    print("Downloading data...")
    nzd_m5 = yf.download("NZDUSD=X", period="60d", interval="5m", progress=False)
    nzd_h1 = yf.download("NZDUSD=X", period="2y", interval="1h", progress=False)
    aud_m5 = yf.download("AUDUSD=X", period="60d", interval="5m", progress=False)
    aud_h1 = yf.download("AUDUSD=X", period="2y", interval="1h", progress=False)

    for df in [nzd_m5, nzd_h1, aud_m5, aud_h1]:
        if df.empty:
            print("MISSING DATA")
            return
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

    days = (nzd_m5.index[-1] - nzd_m5.index[0]).days
    months = max(days / 30, 0.5)
    print(f"NZDUSD: {len(nzd_m5)} bars | AUDUSD: {len(aud_m5)} bars | {days} days ({months:.1f} months)")

    nzd_spec = {"spread": 0.00012, "min_stop": 0.0005}
    aud_spec = {"spread": 0.00012, "min_stop": 0.0005}

    # 1. NZDUSD alone
    r1 = backtest_single(nzd_m5.copy(), nzd_h1.copy(), INITIAL, nzd_spec)
    s1 = print_summary("NZDUSD alone (full 2.25% risk)", r1, INITIAL, months)

    # 2. AUDUSD alone
    r2 = backtest_single(aud_m5.copy(), aud_h1.copy(), INITIAL, aud_spec)
    s2 = print_summary("AUDUSD alone (full 2.25% risk)", r2, INITIAL, months)

    # 3. Both pairs — full risk each (2.25%)
    pairs_full = {
        "NZDUSD": {"m5": nzd_m5.copy(), "h1": nzd_h1.copy(), "spec": nzd_spec},
        "AUDUSD": {"m5": aud_m5.copy(), "h1": aud_h1.copy(), "spec": aud_spec},
    }
    r3 = backtest_dual(pairs_full, INITIAL, risk_per_pair_pct=2.25)
    s3 = print_summary("BOTH pairs — full risk (2.25% each)", r3, INITIAL, months)

    # 4. Both pairs — half risk each (1.125% per pair = same total exposure)
    pairs_half = {
        "NZDUSD": {"m5": nzd_m5.copy(), "h1": nzd_h1.copy(), "spec": nzd_spec},
        "AUDUSD": {"m5": aud_m5.copy(), "h1": aud_h1.copy(), "spec": aud_spec},
    }
    r4 = backtest_dual(pairs_half, INITIAL, risk_per_pair_pct=1.125)
    s4 = print_summary("BOTH pairs — half risk (1.125% each, same total)", r4, INITIAL, months)

    # 5. Both pairs — 1.5% each (moderate)
    pairs_mod = {
        "NZDUSD": {"m5": nzd_m5.copy(), "h1": nzd_h1.copy(), "spec": nzd_spec},
        "AUDUSD": {"m5": aud_m5.copy(), "h1": aud_h1.copy(), "spec": aud_spec},
    }
    r5 = backtest_dual(pairs_mod, INITIAL, risk_per_pair_pct=1.5)
    s5 = print_summary("BOTH pairs — moderate risk (1.5% each)", r5, INITIAL, months)

    # Summary
    print(f"\n\n{'=' * 75}")
    print(f"  FINAL COMPARISON — €{INITIAL:,.0f} account")
    print(f"{'=' * 75}")
    print(f"  {'Setup':<45} {'Bal':>8} {'Ret':>7} {'Mo%':>7} {'#Tr':>4} {'WR':>5} {'PF':>5} {'DD':>5}")
    print(f"  {'-'*45} {'-'*8} {'-'*7} {'-'*7} {'-'*4} {'-'*5} {'-'*5} {'-'*5}")
    for s in [s1, s2, s3, s4, s5]:
        if s:
            print(f"  {s['label'][:45]:<45} €{s['balance']:>6,.0f} {s['ret']:>+6.1f}% {s['monthly']:>+6.1f}% {s['trades']:>4} {s['wr']:>4.0f}% {s['pf']:>4.2f} {s['dd']:>4.1f}%")
    print()


if __name__ == "__main__":
    run()
