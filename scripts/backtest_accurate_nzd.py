"""NZDUSD M5 Scalp — accurate backtest matching LIVE setup exactly.

Matches:
- Conviction-based sizing: HIGH=3%, MEDIUM=2.25%, LOW=1.5%
- Signal threshold: 6, high conviction: 9
- SL: 1.5× ATR (atr_sl_multiplier), no TP (ratchet SL exit)
- Ratchet SL: tighten by 0.5×sl_dist each R level
- Spread filter: reject if spread > 40% of SL distance
- Cooldown: 2 consecutive losses → 10 min pause
- Daily loss limit: 3% of balance
- Weekly loss limit: 6% of balance
- Max 1 concurrent position per instrument
- EOD close at 20:55 UTC
- Debounce: 0 (disabled in live)
- Min size: 1000 units, round to 1000
- Margin: 1:30 leverage, 80% safety buffer
- Session quality multiplier applied to score
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


# ── Live config (from config.py + instruments.py) ──
SIGNAL_THRESHOLD = 6.0
HIGH_CONVICTION_THRESHOLD = 9.0
ATR_SL_MULTIPLIER = 1.5
MIN_STOP = 0.0005
MIN_SIZE = 1000
SIZE_ROUND = 1000
MAX_SIZE = 500000
LEVERAGE = 30
MARGIN_SAFETY = 0.8
MAX_SPREAD_TO_SL = 0.40
SPREAD_PIPS = 1.2  # typical NZDUSD spread ~1.2 pips
SPREAD = SPREAD_PIPS * 0.0001

# Risk per conviction
RISK_HIGH = 3.0
RISK_MEDIUM = 2.25
RISK_LOW = 1.5

# Cooldown
COOLDOWN_AFTER_LOSSES = 2
COOLDOWN_MINUTES = 10  # scalp cooldown
DEBOUNCE_BARS = 0  # disabled in live

# Loss limits
DAILY_LOSS_PCT = 3.0
WEEKLY_LOSS_PCT = 6.0

# EOD
EOD_HOUR = 20
EOD_MINUTE = 55

# Swap
SWAP_PER_NIGHT_PER_1000 = 0.05

EURUSD_RATE = 1.09


def is_eod(bar_time):
    return bar_time.hour > EOD_HOUR or (bar_time.hour == EOD_HOUR and bar_time.minute >= EOD_MINUTE)


def is_overnight(prev_time, curr_time):
    if prev_time is None:
        return False
    return prev_time.date() != curr_time.date()


def get_risk_pct(conviction):
    if conviction == "HIGH":
        return RISK_HIGH
    elif conviction == "MEDIUM":
        return RISK_MEDIUM
    return RISK_LOW


def backtest(m5_df, h1_df, initial, label, overrides=None):
    """Run backtest with optional parameter overrides for testing improvements."""
    cfg = {
        "signal_threshold": SIGNAL_THRESHOLD,
        "high_conviction_threshold": HIGH_CONVICTION_THRESHOLD,
        "atr_sl_mult": ATR_SL_MULTIPLIER,
        "cooldown_enabled": True,
        "cooldown_after": COOLDOWN_AFTER_LOSSES,
        "cooldown_minutes": COOLDOWN_MINUTES,
        "daily_loss_enabled": True,
        "weekly_loss_enabled": True,
        "spread_filter": True,
        "eod_close": True,
        "conviction_sizing": True,
    }
    if overrides:
        cfg.update(overrides)

    engine = M5ScalpScoringEngine(
        signal_threshold=cfg["signal_threshold"],
        high_conviction_threshold=cfg["high_conviction_threshold"],
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
    spread_rejects = 0
    cooldown_rejects = 0
    daily_rejects = 0
    weekly_rejects = 0

    # Cooldown state
    consecutive_losses = 0
    cooldown_until = None

    # Daily/weekly loss tracking
    daily_losses = {}  # date -> total loss
    weekly_losses = {}  # (year, week) -> total loss

    for i in range(50, len(m5_df)):
        bar_time = m5_df.index[i]
        bh, bl, bc = m5_df["high"].iloc[i], m5_df["low"].iloc[i], m5_df["close"].iloc[i]

        # Swap for overnight
        if is_overnight(prev_bar_time, bar_time) and pos:
            swap = SWAP_PER_NIGHT_PER_1000 * (pos["size"] / 1000)
            balance -= swap
            swap_total += swap

        # EOD close
        if cfg["eod_close"] and is_eod(bar_time) and pos:
            if pos["dir"] == "BUY":
                pnl = (bc - pos["entry"]) * pos["size"] - SPREAD * pos["size"]
            else:
                pnl = (pos["entry"] - bc) * pos["size"] - SPREAD * pos["size"]
            balance += pnl
            _record_loss(pnl, bar_time, daily_losses, weekly_losses)
            if pnl <= 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0
            trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                           "exit": bc, "size": pos["size"], "reason": "eod",
                           "ratchet": pos["ratchet"], "date": str(bar_time),
                           "conviction": pos.get("conviction", "")})
            pos = None
            eod_closes += 1

        # Manage position
        if pos:
            sl_hit = False
            if pos["dir"] == "BUY":
                if bl <= pos["sl"]:
                    pnl = (pos["sl"] - pos["entry"]) * pos["size"] - SPREAD * pos["size"]
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
                    pnl = (pos["entry"] - pos["sl"]) * pos["size"] - SPREAD * pos["size"]
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
                    if cfg["cooldown_enabled"] and consecutive_losses >= cfg["cooldown_after"]:
                        cooldown_until = bar_time + pd.Timedelta(minutes=cfg["cooldown_minutes"])
                else:
                    consecutive_losses = 0
                trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                               "exit": pos["sl"], "size": pos["size"], "reason": "sl",
                               "ratchet": pos["ratchet"], "date": str(bar_time),
                               "conviction": pos.get("conviction", "")})
                pos = None

        # Equity tracking
        if balance < lowest: lowest = balance
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

        # New entry
        if pos is None and not (cfg["eod_close"] and is_eod(bar_time)):
            atr = m5_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                prev_bar_time = bar_time
                continue

            # Cooldown check
            if cfg["cooldown_enabled"] and cooldown_until and bar_time < cooldown_until:
                cooldown_rejects += 1
                prev_bar_time = bar_time
                continue

            # Daily loss limit
            if cfg["daily_loss_enabled"]:
                day_key = bar_time.date()
                day_loss = daily_losses.get(day_key, 0)
                if day_loss > 0 and day_loss >= balance * DAILY_LOSS_PCT / 100:
                    daily_rejects += 1
                    prev_bar_time = bar_time
                    continue

            # Weekly loss limit
            if cfg["weekly_loss_enabled"]:
                week_key = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
                week_loss = weekly_losses.get(week_key, 0)
                if week_loss > 0 and week_loss >= balance * WEEKLY_LOSS_PCT / 100:
                    weekly_rejects += 1
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
                sl_dist = max(cfg["atr_sl_mult"] * atr, MIN_STOP)

                # Spread filter
                if cfg["spread_filter"]:
                    spread_ratio = SPREAD / sl_dist
                    if spread_ratio > MAX_SPREAD_TO_SL:
                        spread_rejects += 1
                        prev_bar_time = bar_time
                        continue

                # Conviction-based risk
                if cfg["conviction_sizing"]:
                    risk_pct = get_risk_pct(conviction)
                else:
                    risk_pct = RISK_HIGH  # flat 3%

                risk_amt = balance * risk_pct / 100
                size = risk_amt / sl_dist if sl_dist > 0 else 0

                # Margin cap (1:30, 80% safety)
                max_affordable = balance * MARGIN_SAFETY * LEVERAGE
                max_notional = max_affordable * EURUSD_RATE
                max_size_by_margin = max_notional / entry if entry > 0 else 0
                size = min(size, max_size_by_margin)

                # Round to 1000 units
                size = max(round(size / SIZE_ROUND) * SIZE_ROUND, MIN_SIZE)
                size = min(size, MAX_SIZE)

                actual_risk = size * sl_dist
                if actual_risk > balance * 0.5 or size <= 0:
                    prev_bar_time = bar_time
                    continue

                sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
                pos = {"dir": direction, "entry": entry, "sl": sl,
                       "sl_dist": sl_dist, "size": size, "ratchet": 0,
                       "conviction": conviction}

        prev_bar_time = bar_time

    # Close remaining
    if pos:
        last_close = m5_df["close"].iloc[-1]
        if pos["dir"] == "BUY":
            pnl = (last_close - pos["entry"]) * pos["size"] - SPREAD * pos["size"]
        else:
            pnl = (pos["entry"] - last_close) * pos["size"] - SPREAD * pos["size"]
        balance += pnl
        trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                       "exit": last_close, "size": pos["size"], "reason": "end",
                       "ratchet": pos.get("ratchet", 0), "date": str(m5_df.index[-1]),
                       "conviction": pos.get("conviction", "")})

    return {
        "trades": trades, "balance": balance, "lowest": lowest,
        "max_dd": max_dd, "swap_total": swap_total, "eod_closes": eod_closes,
        "spread_rejects": spread_rejects, "cooldown_rejects": cooldown_rejects,
        "daily_rejects": daily_rejects, "weekly_rejects": weekly_rejects,
        "label": label, "initial": initial,
    }


def _record_loss(pnl, bar_time, daily_losses, weekly_losses):
    if pnl < 0:
        loss = abs(pnl)
        day_key = bar_time.date()
        daily_losses[day_key] = daily_losses.get(day_key, 0) + loss
        week_key = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
        weekly_losses[week_key] = weekly_losses.get(week_key, 0) + loss


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
        return

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

    # Conviction breakdown
    high_trades = [t for t in trades if t.get("conviction") == "HIGH"]
    med_trades = [t for t in trades if t.get("conviction") == "MEDIUM"]

    print(f"  Trades:          {len(trades)} ({len(wins)}W / {len(losses)}L) — {len(trades)/months:.0f}/mo")
    print(f"  Win Rate:        {wr:.1f}%")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  Avg Win:         €{avg_win:.2f} | Avg Loss: €{avg_loss:.2f} | Ratio: {avg_win/avg_loss:.2f}x" if avg_loss > 0 else "")
    print(f"  Avg Position:    {avg_size:,.0f} units")
    print(f"  Final Balance:   €{balance:,.2f} (from €{initial:,.0f})")
    print(f"  Lowest Balance:  €{r['lowest']:,.2f}")
    print(f"  Return:          {ret:+.1f}%")
    print(f"  Monthly:         {monthly:+.1f}%/month")
    print(f"  Max Drawdown:    {r['max_dd']:.1f}%")
    print(f"  Swap Fees:       €{r['swap_total']:.2f}")
    print(f"  Exits:           {sl_count} SL, {eod_count} EOD close")
    print(f"  Conviction:      {len(high_trades)} HIGH, {len(med_trades)} MEDIUM")
    print(f"  Filtered out:    {r['spread_rejects']} spread, {r['cooldown_rejects']} cooldown, "
          f"{r['daily_rejects']} daily limit, {r['weekly_rejects']} weekly limit")

    # Monthly breakdown
    monthly_pnl = {}
    for t in trades:
        mo = t["date"][:7]
        monthly_pnl.setdefault(mo, {"pnl": 0, "n": 0, "wins": 0})
        monthly_pnl[mo]["pnl"] += t["pnl"]
        monthly_pnl[mo]["n"] += 1
        if t["pnl"] > 0:
            monthly_pnl[mo]["wins"] += 1

    print(f"\n  Monthly PnL:")
    running = initial
    for mo in sorted(monthly_pnl):
        d = monthly_pnl[mo]
        running += d["pnl"]
        mo_wr = d["wins"] / d["n"] * 100 if d["n"] > 0 else 0
        print(f"    {mo}: €{d['pnl']:>+9.2f}  {d['n']:>3} trades  {mo_wr:.0f}% WR  bal: €{running:,.2f}")

    # Projections
    if monthly > 0:
        print(f"\n  Projections ({monthly:+.1f}%/mo compounding):")
        for m, lbl in [(3, "3 months"), (6, "6 months"), (12, "12 months")]:
            proj = initial * (1 + monthly / 100) ** m
            print(f"    {lbl}: €{proj:,.2f}")

    return {"ret": ret, "monthly": monthly, "wr": wr, "pf": pf, "dd": r["max_dd"],
            "trades": len(trades), "balance": balance}


def run():
    print("Downloading NZDUSD data...")
    m5 = yf.download("NZDUSD=X", period="60d", interval="5m", progress=False)
    h1 = yf.download("NZDUSD=X", period="2y", interval="1h", progress=False)

    for df in [m5, h1]:
        if df.empty:
            print("NO DATA")
            return
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

    days = (m5.index[-1] - m5.index[0]).days
    months = max(days / 30, 0.5)

    print(f"Data: {len(m5)} M5 bars, {len(h1)} H1 bars ({days} days, {months:.1f} months)")

    # ── 1. CURRENT LIVE SETUP (baseline) ──
    r_live = backtest(m5.copy(), h1.copy(), 155.0,
                      "CURRENT LIVE SETUP (€155, exact config)")
    s_live = print_results(r_live, months)

    # Also test with €1,000 and €10,000
    r_1k = backtest(m5.copy(), h1.copy(), 1000.0,
                    "CURRENT SETUP — €1,000 account")
    s_1k = print_results(r_1k, months)

    r_10k = backtest(m5.copy(), h1.copy(), 10000.0,
                     "CURRENT SETUP — €10,000 account")
    s_10k = print_results(r_10k, months)

    # ── 2. IMPROVEMENTS TO TEST ──
    improvements = []

    # A. Lower threshold (more trades)
    r_a = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE A: Lower threshold (5 instead of 6)",
                   {"signal_threshold": 5.0, "high_conviction_threshold": 8.0})
    s_a = print_results(r_a, months)
    improvements.append(("A: Threshold=5", s_a))

    # B. Tighter SL (1.0× ATR instead of 1.5×)
    r_b = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE B: Tighter SL (1.0× ATR instead of 1.5×)",
                   {"atr_sl_mult": 1.0})
    s_b = print_results(r_b, months)
    improvements.append(("B: SL=1.0×ATR", s_b))

    # C. Wider SL (2.0× ATR)
    r_c = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE C: Wider SL (2.0× ATR)",
                   {"atr_sl_mult": 2.0})
    s_c = print_results(r_c, months)
    improvements.append(("C: SL=2.0×ATR", s_c))

    # D. No conviction sizing (flat 3%)
    r_d = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE D: Flat 3% risk (no conviction scaling)",
                   {"conviction_sizing": False})
    s_d = print_results(r_d, months)
    improvements.append(("D: Flat 3%", s_d))

    # E. No cooldown
    r_e = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE E: No cooldown after losses",
                   {"cooldown_enabled": False})
    s_e = print_results(r_e, months)
    improvements.append(("E: No cooldown", s_e))

    # F. No daily/weekly loss limits
    r_f = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE F: No daily/weekly loss limits",
                   {"daily_loss_enabled": False, "weekly_loss_enabled": False})
    s_f = print_results(r_f, months)
    improvements.append(("F: No loss limits", s_f))

    # G. No spread filter
    r_g = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE G: No spread filter",
                   {"spread_filter": False})
    s_g = print_results(r_g, months)
    improvements.append(("G: No spread filter", s_g))

    # H. Hold overnight (no EOD close)
    r_h = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE H: Hold overnight (no EOD close)",
                   {"eod_close": False})
    s_h = print_results(r_h, months)
    improvements.append(("H: No EOD close", s_h))

    # I. Best combo: lower threshold + tighter SL + flat risk + no cooldown
    r_i = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE I: Combo (thresh=5, SL=1.0×ATR, flat 3%, no cooldown)",
                   {"signal_threshold": 5.0, "high_conviction_threshold": 8.0,
                    "atr_sl_mult": 1.0, "conviction_sizing": False,
                    "cooldown_enabled": False})
    s_i = print_results(r_i, months)
    improvements.append(("I: Best combo", s_i))

    # J. Conservative combo: wider SL + conviction sizing + all safety
    r_j = backtest(m5.copy(), h1.copy(), 1000.0,
                   "IMPROVE J: Safe combo (thresh=7, SL=2.0×ATR, conviction sizing)",
                   {"signal_threshold": 7.0, "high_conviction_threshold": 10.0,
                    "atr_sl_mult": 2.0})
    s_j = print_results(r_j, months)
    improvements.append(("J: Safe combo", s_j))

    # ── SUMMARY ──
    print(f"\n\n{'=' * 70}")
    print(f"  COMPARISON TABLE — All on €1,000 account")
    print(f"{'=' * 70}")
    print(f"  {'Setup':<25} {'Balance':>10} {'Return':>8} {'Mo%':>7} {'Trades':>7} {'WR':>6} {'PF':>6} {'DD':>6}")
    print(f"  {'-'*25} {'-'*10} {'-'*8} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*6}")

    # Baseline
    if s_1k:
        print(f"  {'CURRENT (baseline)':<25} €{s_1k['balance']:>8,.2f} {s_1k['ret']:>+7.1f}% {s_1k['monthly']:>+6.1f}% {s_1k['trades']:>7} {s_1k['wr']:>5.1f}% {s_1k['pf']:>5.2f} {s_1k['dd']:>5.1f}%")

    for name, s in improvements:
        if s:
            print(f"  {name:<25} €{s['balance']:>8,.2f} {s['ret']:>+7.1f}% {s['monthly']:>+6.1f}% {s['trades']:>7} {s['wr']:>5.1f}% {s['pf']:>5.2f} {s['dd']:>5.1f}%")

    print()


if __name__ == "__main__":
    run()
