"""Optimize M5 Scalp — NZDUSD vs USDJPY, tuned parameters.

Tests:
1. Relaxed daily/weekly loss limits
2. Higher threshold (7+)
3. Wider SL (2× ATR)
4. Smarter EOD: only close losers, let winners run overnight
5. NZDUSD vs USDJPY head to head
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


def is_eod(bar_time, hour=20, minute=55):
    return bar_time.hour > hour or (bar_time.hour == hour and bar_time.minute >= minute)


def is_overnight(prev_time, curr_time):
    if prev_time is None:
        return False
    return prev_time.date() != curr_time.date()


def backtest(m5_df, h1_df, initial, label, cfg):
    engine = M5ScalpScoringEngine(
        signal_threshold=cfg["signal_threshold"],
        high_conviction_threshold=cfg.get("high_conviction_threshold", cfg["signal_threshold"] + 3),
    )
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
    prev_bar_time = None
    swap_total = 0.0
    eod_closes = 0
    rejects = {"spread": 0, "cooldown": 0, "daily": 0, "weekly": 0}

    consecutive_losses = 0
    cooldown_until = None
    daily_losses = {}
    weekly_losses = {}

    spread = cfg.get("spread", 0.00012)
    min_stop = cfg.get("min_stop", 0.0005)
    min_size = cfg.get("min_size", 1000)
    size_round = cfg.get("size_round", 1000)
    max_size = cfg.get("max_size", 500000)
    leverage = cfg.get("leverage", 30)
    swap_per_night = cfg.get("swap_per_night", 0.05)

    for i in range(50, len(m5_df)):
        bar_time = m5_df.index[i]
        bh, bl, bc = m5_df["high"].iloc[i], m5_df["low"].iloc[i], m5_df["close"].iloc[i]

        # Swap
        if is_overnight(prev_bar_time, bar_time) and pos:
            swap = swap_per_night * (pos["size"] / (min_size or 1000))
            balance -= swap
            swap_total += swap

        # EOD close logic
        eod_hour = cfg.get("eod_hour", 20)
        eod_minute = cfg.get("eod_minute", 55)
        if cfg.get("eod_close", True) and is_eod(bar_time, eod_hour, eod_minute) and pos:
            # Smart EOD: only close if losing or breakeven, let winners run
            should_close = True
            if cfg.get("eod_smart", False):
                if pos["dir"] == "BUY":
                    unrealized = (bc - pos["entry"]) * pos["size"]
                else:
                    unrealized = (pos["entry"] - bc) * pos["size"]
                # Let winners run if ratchet has kicked in (>= 1R profit)
                if unrealized > 0 and pos["ratchet"] >= 1:
                    should_close = False

            if should_close:
                if pos["dir"] == "BUY":
                    pnl = (bc - pos["entry"]) * pos["size"] - spread * pos["size"]
                else:
                    pnl = (pos["entry"] - bc) * pos["size"] - spread * pos["size"]
                balance += pnl
                _record_loss(pnl, bar_time, daily_losses, weekly_losses)
                if pnl <= 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0
                trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                               "exit": bc, "size": pos["size"], "reason": "eod",
                               "ratchet": pos["ratchet"], "date": str(bar_time),
                               "conviction": pos.get("conviction", ""),
                               "bars_held": i - pos["entry_bar"]})
                pos = None
                eod_closes += 1

        # Manage position
        if pos:
            sl_hit = False
            if pos["dir"] == "BUY":
                if bl <= pos["sl"]:
                    pnl = (pos["sl"] - pos["entry"]) * pos["size"] - spread * pos["size"]
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
                    pnl = (pos["entry"] - pos["sl"]) * pos["size"] - spread * pos["size"]
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
                    if cfg.get("cooldown_enabled", True) and consecutive_losses >= cfg.get("cooldown_after", 2):
                        cooldown_until = bar_time + pd.Timedelta(minutes=cfg.get("cooldown_minutes", 10))
                else:
                    consecutive_losses = 0
                trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                               "exit": pos["sl"], "size": pos["size"], "reason": "sl",
                               "ratchet": pos["ratchet"], "date": str(bar_time),
                               "conviction": pos.get("conviction", ""),
                               "bars_held": i - pos["entry_bar"]})
                pos = None

        # Equity
        if balance < lowest: lowest = balance
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

        # New entry
        can_enter = pos is None
        if cfg.get("eod_close", True) and is_eod(bar_time, eod_hour, eod_minute):
            can_enter = False

        if can_enter:
            atr = m5_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                prev_bar_time = bar_time
                continue

            # Cooldown
            if cfg.get("cooldown_enabled", True) and cooldown_until and bar_time < cooldown_until:
                rejects["cooldown"] += 1
                prev_bar_time = bar_time
                continue

            # Daily loss limit
            daily_limit = cfg.get("daily_loss_pct", 3.0)
            if cfg.get("daily_loss_enabled", True):
                day_loss = daily_losses.get(bar_time.date(), 0)
                if day_loss > 0 and day_loss >= balance * daily_limit / 100:
                    rejects["daily"] += 1
                    prev_bar_time = bar_time
                    continue

            # Weekly loss limit
            weekly_limit = cfg.get("weekly_loss_pct", 6.0)
            if cfg.get("weekly_loss_enabled", True):
                week_key = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
                week_loss = weekly_losses.get(week_key, 0)
                if week_loss > 0 and week_loss >= balance * weekly_limit / 100:
                    rejects["weekly"] += 1
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
                conviction = result["conviction"]
                entry = bc
                atr_mult = cfg.get("atr_sl_mult", 1.5)
                sl_dist = max(atr_mult * atr, min_stop)

                # Spread filter
                if cfg.get("spread_filter", True):
                    max_spread_ratio = cfg.get("max_spread_to_sl", 0.40)
                    if spread / sl_dist > max_spread_ratio:
                        rejects["spread"] += 1
                        prev_bar_time = bar_time
                        continue

                # Conviction sizing
                if cfg.get("conviction_sizing", True):
                    risk_map = {"HIGH": 3.0, "MEDIUM": 2.25, "LOW": 1.5}
                    risk_pct = risk_map.get(conviction, 2.25)
                else:
                    risk_pct = 3.0

                risk_amt = balance * risk_pct / 100
                size = risk_amt / sl_dist if sl_dist > 0 else 0

                # Margin cap
                max_affordable = balance * 0.8 * leverage
                max_notional = max_affordable * EURUSD_RATE
                max_size_by_margin = max_notional / entry if entry > 0 else 0
                size = min(size, max_size_by_margin)
                size = max(round(size / size_round) * size_round, min_size)
                size = min(size, max_size)

                actual_risk = size * sl_dist
                if actual_risk > balance * 0.5 or size <= 0:
                    prev_bar_time = bar_time
                    continue

                sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
                pos = {"dir": direction, "entry": entry, "sl": sl,
                       "sl_dist": sl_dist, "size": size, "ratchet": 0,
                       "conviction": conviction, "entry_bar": i}

        prev_bar_time = bar_time

    # Close remaining
    if pos:
        lc = m5_df["close"].iloc[-1]
        if pos["dir"] == "BUY":
            pnl = (lc - pos["entry"]) * pos["size"] - spread * pos["size"]
        else:
            pnl = (pos["entry"] - lc) * pos["size"] - spread * pos["size"]
        balance += pnl
        trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                       "exit": lc, "size": pos["size"], "reason": "end",
                       "ratchet": pos.get("ratchet", 0), "date": str(m5_df.index[-1]),
                       "conviction": pos.get("conviction", ""),
                       "bars_held": len(m5_df) - 1 - pos["entry_bar"]})

    return {
        "trades": trades, "balance": balance, "lowest": lowest,
        "max_dd": max_dd, "swap_total": swap_total, "eod_closes": eod_closes,
        "rejects": rejects, "label": label, "initial": initial,
    }


def _record_loss(pnl, bar_time, daily_losses, weekly_losses):
    if pnl < 0:
        loss = abs(pnl)
        daily_losses[bar_time.date()] = daily_losses.get(bar_time.date(), 0) + loss
        wk = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
        weekly_losses[wk] = weekly_losses.get(wk, 0) + loss


def print_results(r, months):
    trades = r["trades"]
    label = r["label"]
    initial = r["initial"]
    balance = r["balance"]

    print(f"\n{'=' * 70}")
    print(f"  {label}")
    print(f"{'=' * 70}")

    if not trades:
        print(f"  NO TRADES")
        return None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0.01
    ret = (balance - initial) / initial * 100
    monthly = ret / months if months > 0 else 0
    wr = len(wins) / len(trades) * 100
    pf = w_pnl / l_pnl
    avg_win = w_pnl / len(wins) if wins else 0
    avg_loss = l_pnl / len(losses) if losses else 0
    avg_size = np.mean([t["size"] for t in trades])

    eod_count = sum(1 for t in trades if t.get("reason") == "eod")
    sl_count = sum(1 for t in trades if t.get("reason") == "sl")

    # Avg bars held for winners vs losers
    win_bars = np.mean([t["bars_held"] for t in wins]) if wins else 0
    loss_bars = np.mean([t["bars_held"] for t in losses]) if losses else 0

    # Ratchet stats
    ratchet_levels = [t["ratchet"] for t in trades]
    ratcheted = [t for t in trades if t["ratchet"] >= 1]

    # EOD P&L breakdown
    eod_trades = [t for t in trades if t.get("reason") == "eod"]
    eod_pnl = sum(t["pnl"] for t in eod_trades) if eod_trades else 0
    eod_wins = sum(1 for t in eod_trades if t["pnl"] > 0)

    rej = r["rejects"]
    high_trades = [t for t in trades if t.get("conviction") == "HIGH"]
    med_trades = [t for t in trades if t.get("conviction") == "MEDIUM"]

    print(f"  Trades:          {len(trades)} ({len(wins)}W / {len(losses)}L) — {len(trades)/months:.0f}/mo")
    print(f"  Win Rate:        {wr:.1f}%")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  Avg Win:         €{avg_win:.2f} | Avg Loss: €{avg_loss:.2f} | Ratio: {avg_win/avg_loss:.2f}x" if avg_loss > 0 else "")
    print(f"  Avg Bars Held:   Winners: {win_bars:.0f} ({win_bars*5:.0f}min) | Losers: {loss_bars:.0f} ({loss_bars*5:.0f}min)")
    print(f"  Ratcheted:       {len(ratcheted)}/{len(trades)} trades hit 1R+ (avg level: {np.mean(ratchet_levels):.1f}R)")
    print(f"  Avg Position:    {avg_size:,.0f} units")
    print(f"  Final Balance:   €{balance:,.2f} (from €{initial:,.0f})")
    print(f"  Lowest Balance:  €{r['lowest']:,.2f}")
    print(f"  Return:          {ret:+.1f}%")
    print(f"  Monthly:         {monthly:+.1f}%/month")
    print(f"  Max Drawdown:    {r['max_dd']:.1f}%")
    print(f"  Swap Fees:       €{r['swap_total']:.2f}")
    print(f"  Exits:           {sl_count} SL, {eod_count} EOD (EOD P&L: €{eod_pnl:+.2f}, {eod_wins}W/{len(eod_trades)-eod_wins}L)")
    print(f"  Conviction:      {len(high_trades)} HIGH, {len(med_trades)} MEDIUM")
    print(f"  Filtered:        {rej['spread']} spread, {rej['cooldown']} cooldown, "
          f"{rej['daily']} daily, {rej['weekly']} weekly")

    # Monthly breakdown
    monthly_pnl = {}
    for t in trades:
        mo = t["date"][:7]
        monthly_pnl.setdefault(mo, {"pnl": 0, "n": 0, "wins": 0})
        monthly_pnl[mo]["pnl"] += t["pnl"]
        monthly_pnl[mo]["n"] += 1
        if t["pnl"] > 0: monthly_pnl[mo]["wins"] += 1

    print(f"\n  Monthly PnL:")
    running = initial
    for mo in sorted(monthly_pnl):
        d = monthly_pnl[mo]
        running += d["pnl"]
        mo_wr = d["wins"] / d["n"] * 100 if d["n"] > 0 else 0
        print(f"    {mo}: €{d['pnl']:>+9.2f}  {d['n']:>3} trades  {mo_wr:.0f}% WR  bal: €{running:,.2f}")

    if monthly > 0:
        print(f"\n  Projections ({monthly:+.1f}%/mo compounding):")
        for m, lbl in [(3, "3 months"), (6, "6 months"), (12, "12 months")]:
            proj = initial * (1 + monthly / 100) ** m
            print(f"    {lbl}: €{proj:,.2f}")

    return {"ret": ret, "monthly": monthly, "wr": wr, "pf": pf, "dd": r["max_dd"],
            "trades": len(trades), "balance": balance, "label": label}


def run():
    # Download both pairs
    print("Downloading data...")
    nzd_m5 = yf.download("NZDUSD=X", period="60d", interval="5m", progress=False)
    nzd_h1 = yf.download("NZDUSD=X", period="2y", interval="1h", progress=False)
    jpy_m5 = yf.download("JPY=X", period="60d", interval="5m", progress=False)
    jpy_h1 = yf.download("JPY=X", period="2y", interval="1h", progress=False)

    for df in [nzd_m5, nzd_h1, jpy_m5, jpy_h1]:
        if df.empty:
            print("MISSING DATA")
            return
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

    nzd_days = (nzd_m5.index[-1] - nzd_m5.index[0]).days
    nzd_months = max(nzd_days / 30, 0.5)
    jpy_days = (jpy_m5.index[-1] - jpy_m5.index[0]).days
    jpy_months = max(jpy_days / 30, 0.5)

    print(f"NZDUSD: {len(nzd_m5)} M5 bars ({nzd_days} days, {nzd_months:.1f} months)")
    print(f"USDJPY: {len(jpy_m5)} M5 bars ({jpy_days} days, {jpy_months:.1f} months)")

    INITIAL = 155.0  # current account
    results = []

    # ── NZDUSD configs ──
    nzd_base = {
        "signal_threshold": 6.0, "high_conviction_threshold": 9.0,
        "atr_sl_mult": 1.5, "spread": 0.00012, "min_stop": 0.0005,
        "min_size": 1000, "size_round": 1000, "max_size": 500000,
        "leverage": 30, "swap_per_night": 0.05,
        "cooldown_enabled": True, "cooldown_after": 2, "cooldown_minutes": 10,
        "daily_loss_enabled": True, "daily_loss_pct": 3.0,
        "weekly_loss_enabled": True, "weekly_loss_pct": 6.0,
        "spread_filter": True, "max_spread_to_sl": 0.40,
        "eod_close": True, "eod_hour": 20, "eod_minute": 55,
        "conviction_sizing": True, "eod_smart": False,
    }

    # 1. Current live
    r = backtest(nzd_m5.copy(), nzd_h1.copy(), INITIAL,
                 "NZD — CURRENT LIVE", nzd_base)
    s = print_results(r, nzd_months)
    if s: results.append(s)

    # 2. Relaxed limits: 6% daily, 15% weekly
    cfg2 = {**nzd_base, "daily_loss_pct": 6.0, "weekly_loss_pct": 15.0}
    r = backtest(nzd_m5.copy(), nzd_h1.copy(), INITIAL,
                 "NZD — Relaxed limits (6% daily, 15% weekly)", cfg2)
    s = print_results(r, nzd_months)
    if s: results.append(s)

    # 3. Higher threshold + wider SL
    cfg3 = {**nzd_base, "signal_threshold": 7.0, "high_conviction_threshold": 10.0,
            "atr_sl_mult": 2.0}
    r = backtest(nzd_m5.copy(), nzd_h1.copy(), INITIAL,
                 "NZD — Threshold 7 + SL 2×ATR", cfg3)
    s = print_results(r, nzd_months)
    if s: results.append(s)

    # 4. All 3 improvements combined
    cfg4 = {**nzd_base, "signal_threshold": 7.0, "high_conviction_threshold": 10.0,
            "atr_sl_mult": 2.0, "daily_loss_pct": 6.0, "weekly_loss_pct": 15.0}
    r = backtest(nzd_m5.copy(), nzd_h1.copy(), INITIAL,
                 "NZD — ALL 3: thresh=7 + SL=2×ATR + relaxed limits", cfg4)
    s = print_results(r, nzd_months)
    if s: results.append(s)

    # 5. All 3 + smart EOD (let winners run overnight)
    cfg5 = {**cfg4, "eod_smart": True}
    r = backtest(nzd_m5.copy(), nzd_h1.copy(), INITIAL,
                 "NZD — ALL 3 + smart EOD (winners run overnight)", cfg5)
    s = print_results(r, nzd_months)
    if s: results.append(s)

    # 6. All 3 + no EOD at all (pure ratchet exit)
    cfg6 = {**cfg4, "eod_close": False}
    r = backtest(nzd_m5.copy(), nzd_h1.copy(), INITIAL,
                 "NZD — ALL 3 + no EOD (pure ratchet)", cfg6)
    s = print_results(r, nzd_months)
    if s: results.append(s)

    # 7. All 3 + no loss limits at all
    cfg7 = {**nzd_base, "signal_threshold": 7.0, "high_conviction_threshold": 10.0,
            "atr_sl_mult": 2.0, "daily_loss_enabled": False, "weekly_loss_enabled": False}
    r = backtest(nzd_m5.copy(), nzd_h1.copy(), INITIAL,
                 "NZD — thresh=7 + SL=2×ATR + NO loss limits", cfg7)
    s = print_results(r, nzd_months)
    if s: results.append(s)

    # 8. Threshold 8 (very selective)
    cfg8 = {**nzd_base, "signal_threshold": 8.0, "high_conviction_threshold": 11.0,
            "atr_sl_mult": 2.0, "daily_loss_pct": 6.0, "weekly_loss_pct": 15.0}
    r = backtest(nzd_m5.copy(), nzd_h1.copy(), INITIAL,
                 "NZD — Threshold 8 + SL=2×ATR + relaxed limits", cfg8)
    s = print_results(r, nzd_months)
    if s: results.append(s)

    # ── USDJPY configs ──
    jpy_base = {
        "signal_threshold": 6.0, "high_conviction_threshold": 9.0,
        "atr_sl_mult": 1.5, "spread": 0.012,  # ~1.2 pip for USDJPY (in JPY)
        "min_stop": 0.05, "min_size": 1000, "size_round": 1000,
        "max_size": 500000, "leverage": 30, "swap_per_night": 0.03,
        "cooldown_enabled": True, "cooldown_after": 2, "cooldown_minutes": 10,
        "daily_loss_enabled": True, "daily_loss_pct": 3.0,
        "weekly_loss_enabled": True, "weekly_loss_pct": 6.0,
        "spread_filter": True, "max_spread_to_sl": 0.40,
        "eod_close": True, "eod_hour": 20, "eod_minute": 55,
        "conviction_sizing": True, "eod_smart": False,
    }

    # 9. USDJPY current config
    r = backtest(jpy_m5.copy(), jpy_h1.copy(), INITIAL,
                 "JPY — Current config (baseline)", jpy_base)
    s = print_results(r, jpy_months)
    if s: results.append(s)

    # 10. USDJPY all 3 improvements
    cfg10 = {**jpy_base, "signal_threshold": 7.0, "high_conviction_threshold": 10.0,
             "atr_sl_mult": 2.0, "daily_loss_pct": 6.0, "weekly_loss_pct": 15.0}
    r = backtest(jpy_m5.copy(), jpy_h1.copy(), INITIAL,
                 "JPY — ALL 3: thresh=7 + SL=2×ATR + relaxed limits", cfg10)
    s = print_results(r, jpy_months)
    if s: results.append(s)

    # 11. USDJPY all 3 + smart EOD
    cfg11 = {**cfg10, "eod_smart": True}
    r = backtest(jpy_m5.copy(), jpy_h1.copy(), INITIAL,
                 "JPY — ALL 3 + smart EOD", cfg11)
    s = print_results(r, jpy_months)
    if s: results.append(s)

    # 12. USDJPY all 3 + no loss limits
    cfg12 = {**jpy_base, "signal_threshold": 7.0, "high_conviction_threshold": 10.0,
             "atr_sl_mult": 2.0, "daily_loss_enabled": False, "weekly_loss_enabled": False}
    r = backtest(jpy_m5.copy(), jpy_h1.copy(), INITIAL,
                 "JPY — thresh=7 + SL=2×ATR + NO loss limits", cfg12)
    s = print_results(r, jpy_months)
    if s: results.append(s)

    # 13. USDJPY threshold 8
    cfg13 = {**jpy_base, "signal_threshold": 8.0, "high_conviction_threshold": 11.0,
             "atr_sl_mult": 2.0, "daily_loss_pct": 6.0, "weekly_loss_pct": 15.0}
    r = backtest(jpy_m5.copy(), jpy_h1.copy(), INITIAL,
                 "JPY — Threshold 8 + SL=2×ATR + relaxed limits", cfg13)
    s = print_results(r, jpy_months)
    if s: results.append(s)

    # ── SUMMARY ──
    print(f"\n\n{'=' * 80}")
    print(f"  COMPARISON TABLE — All on €{INITIAL:.0f} account")
    print(f"{'=' * 80}")
    print(f"  {'#':>2} {'Setup':<45} {'Bal':>8} {'Ret':>7} {'Mo%':>7} {'#Tr':>4} {'WR':>5} {'PF':>5} {'DD':>5}")
    print(f"  {'-'*2} {'-'*45} {'-'*8} {'-'*7} {'-'*7} {'-'*4} {'-'*5} {'-'*5} {'-'*5}")

    for i, s in enumerate(results, 1):
        lbl = s["label"][:45]
        print(f"  {i:>2} {lbl:<45} €{s['balance']:>6.0f} {s['ret']:>+6.1f}% {s['monthly']:>+6.1f}% {s['trades']:>4} {s['wr']:>4.0f}% {s['pf']:>4.2f} {s['dd']:>4.1f}%")

    print()


if __name__ == "__main__":
    run()
