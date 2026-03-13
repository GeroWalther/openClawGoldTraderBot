"""Compare optimized M5 Scalp across all forex pairs + BTC.

Tests current optimized config (thresh=8, SL=2×ATR, relaxed limits)
on all available pairs + BTC, €1,000 account.
Also tests BTC with old settings (thresh=6, SL=1.5×ATR) vs new.
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
INITIAL = 1000.0


def is_eod(bar_time, hour=20, minute=55):
    return bar_time.hour > hour or (bar_time.hour == hour and bar_time.minute >= minute)


def is_overnight(prev_time, curr_time):
    if prev_time is None:
        return False
    return prev_time.date() != curr_time.date()


def backtest(m5_df, h1_df, initial, cfg):
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

    consecutive_losses = 0
    cooldown_until = None
    daily_losses = {}
    weekly_losses = {}

    spread = cfg["spread"]
    min_stop = cfg["min_stop"]
    min_size = cfg["min_size"]
    size_round = cfg["size_round"]
    max_size = cfg["max_size"]
    leverage = cfg["leverage"]
    swap_per_night = cfg.get("swap_per_night", 0.05)

    for i in range(50, len(m5_df)):
        bar_time = m5_df.index[i]
        bh, bl, bc = m5_df["high"].iloc[i], m5_df["low"].iloc[i], m5_df["close"].iloc[i]

        # Swap
        if is_overnight(prev_bar_time, bar_time) and pos:
            swap = swap_per_night * (pos["size"] / min_size)
            balance -= swap
            swap_total += swap

        # EOD close
        if cfg.get("eod_close", True) and is_eod(bar_time) and pos:
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
            trades.append({"pnl": pnl, "dir": pos["dir"], "reason": "eod",
                           "ratchet": pos["ratchet"], "date": str(bar_time)})
            pos = None

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
                    if cfg.get("cooldown_enabled", True) and consecutive_losses >= 2:
                        cooldown_until = bar_time + pd.Timedelta(minutes=10)
                else:
                    consecutive_losses = 0
                trades.append({"pnl": pnl, "dir": pos["dir"], "reason": "sl",
                               "ratchet": pos["ratchet"], "date": str(bar_time)})
                pos = None

        # Equity
        if balance < lowest: lowest = balance
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

        # New entry
        if pos is not None or (cfg.get("eod_close", True) and is_eod(bar_time)):
            prev_bar_time = bar_time
            continue

        atr = m5_df["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            prev_bar_time = bar_time
            continue

        # Cooldown
        if cfg.get("cooldown_enabled", True) and cooldown_until and bar_time < cooldown_until:
            prev_bar_time = bar_time
            continue

        # Daily loss limit
        daily_limit = cfg.get("daily_loss_pct", 6.0)
        day_loss = daily_losses.get(bar_time.date(), 0)
        if day_loss > 0 and day_loss >= balance * daily_limit / 100:
            prev_bar_time = bar_time
            continue

        # Weekly loss limit
        weekly_limit = cfg.get("weekly_loss_pct", 15.0)
        week_key = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
        week_loss = weekly_losses.get(week_key, 0)
        if week_loss > 0 and week_loss >= balance * weekly_limit / 100:
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
            atr_mult = cfg.get("atr_sl_mult", 2.0)
            sl_dist = max(atr_mult * atr, min_stop)

            # Spread filter
            if cfg.get("spread_filter", True):
                if spread / sl_dist > 0.40:
                    prev_bar_time = bar_time
                    continue

            # Conviction sizing
            risk_map = {"HIGH": 3.0, "MEDIUM": 2.25, "LOW": 1.5}
            risk_pct = risk_map.get(conviction, 2.25)
            risk_amt = balance * risk_pct / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0

            # Margin cap
            max_affordable = balance * 0.8 * leverage * EURUSD_RATE
            max_size_by_margin = max_affordable / entry if entry > 0 else 0
            size = min(size, max_size_by_margin)
            size = max(round(size / size_round) * size_round, min_size)
            size = min(size, max_size)

            if size * sl_dist > balance * 0.5 or size <= 0:
                prev_bar_time = bar_time
                continue

            sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
            pos = {"dir": direction, "entry": entry, "sl": sl,
                   "sl_dist": sl_dist, "size": size, "ratchet": 0}

        prev_bar_time = bar_time

    # Close remaining
    if pos:
        lc = m5_df["close"].iloc[-1]
        if pos["dir"] == "BUY":
            pnl = (lc - pos["entry"]) * pos["size"] - spread * pos["size"]
        else:
            pnl = (pos["entry"] - lc) * pos["size"] - spread * pos["size"]
        balance += pnl
        trades.append({"pnl": pnl, "dir": pos["dir"], "reason": "end",
                       "ratchet": pos.get("ratchet", 0), "date": str(m5_df.index[-1])})

    return {"trades": trades, "balance": balance, "lowest": lowest,
            "max_dd": max_dd, "swap_total": swap_total}


def _record_loss(pnl, bar_time, daily_losses, weekly_losses):
    if pnl < 0:
        loss = abs(pnl)
        daily_losses[bar_time.date()] = daily_losses.get(bar_time.date(), 0) + loss
        wk = (bar_time.isocalendar()[0], bar_time.isocalendar()[1])
        weekly_losses[wk] = weekly_losses.get(wk, 0) + loss


def summarize(label, r, months, initial):
    trades = r["trades"]
    if not trades:
        return {"label": label, "balance": initial, "ret": 0, "monthly": 0,
                "wr": 0, "pf": 0, "dd": 0, "trades": 0, "avg_win": 0, "avg_loss": 0}
    balance = r["balance"]
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
    ratcheted = sum(1 for t in trades if t["ratchet"] >= 1)
    eod = sum(1 for t in trades if t["reason"] == "eod")

    return {"label": label, "balance": balance, "ret": ret, "monthly": monthly,
            "wr": wr, "pf": pf, "dd": r["max_dd"], "trades": len(trades),
            "avg_win": avg_win, "avg_loss": avg_loss, "ratcheted": ratcheted,
            "eod": eod, "lowest": r["lowest"], "swap": r["swap_total"]}


def print_detail(s, months):
    print(f"\n{'=' * 70}")
    print(f"  {s['label']}")
    print(f"{'=' * 70}")
    if s["trades"] == 0:
        print("  NO TRADES")
        return
    print(f"  Trades: {s['trades']} | WR: {s['wr']:.1f}% | PF: {s['pf']:.2f}")
    print(f"  Avg Win: €{s['avg_win']:.2f} | Avg Loss: €{s['avg_loss']:.2f} | Ratio: {s['avg_win']/s['avg_loss']:.2f}x" if s['avg_loss'] > 0 else "")
    print(f"  Ratcheted: {s.get('ratcheted',0)}/{s['trades']} | EOD closes: {s.get('eod',0)}")
    print(f"  Balance: €{s['balance']:,.2f} (low: €{s.get('lowest', INITIAL):,.2f}) | Return: {s['ret']:+.1f}%")
    print(f"  Monthly: {s['monthly']:+.1f}%/mo | Max DD: {s['dd']:.1f}% | Swap: €{s.get('swap',0):.2f}")
    if s["monthly"] > 0:
        print(f"  Projections: 3mo €{INITIAL*(1+s['monthly']/100)**3:,.0f} | "
              f"6mo €{INITIAL*(1+s['monthly']/100)**6:,.0f} | "
              f"12mo €{INITIAL*(1+s['monthly']/100)**12:,.0f}")


def run():
    # ── Define pairs to test ──
    pairs = [
        ("NZDUSD", "NZDUSD=X", {
            "spread": 0.00012, "min_stop": 0.0005, "min_size": 1000,
            "size_round": 1000, "max_size": 500000, "leverage": 30,
            "swap_per_night": 0.05,
        }),
        ("AUDUSD", "AUDUSD=X", {
            "spread": 0.00012, "min_stop": 0.0005, "min_size": 1000,
            "size_round": 1000, "max_size": 500000, "leverage": 30,
            "swap_per_night": 0.05,
        }),
        ("EURUSD", "EURUSD=X", {
            "spread": 0.00010, "min_stop": 0.0005, "min_size": 1000,
            "size_round": 1000, "max_size": 500000, "leverage": 30,
            "swap_per_night": 0.04,
        }),
        ("GBPUSD", "GBPUSD=X", {
            "spread": 0.00014, "min_stop": 0.0005, "min_size": 1000,
            "size_round": 1000, "max_size": 500000, "leverage": 30,
            "swap_per_night": 0.06,
        }),
        ("USDJPY", "JPY=X", {
            "spread": 0.012, "min_stop": 0.05, "min_size": 1000,
            "size_round": 1000, "max_size": 500000, "leverage": 30,
            "swap_per_night": 0.03,
        }),
        ("EURJPY", "EURJPY=X", {
            "spread": 0.015, "min_stop": 0.05, "min_size": 1000,
            "size_round": 1000, "max_size": 500000, "leverage": 30,
            "swap_per_night": 0.04,
        }),
        ("CADJPY", "CADJPY=X", {
            "spread": 0.015, "min_stop": 0.05, "min_size": 1000,
            "size_round": 1000, "max_size": 500000, "leverage": 30,
            "swap_per_night": 0.04,
        }),
    ]

    # BTC separate
    btc_spec = {
        "spread": 30.0, "min_stop": 250.0, "min_size": 0.01,
        "size_round": 0.01, "max_size": 5.0, "leverage": 2,
        "swap_per_night": 0.50,
    }

    # ── Download all data ──
    print("Downloading data for all pairs...")
    data = {}
    for name, yahoo, _ in pairs:
        m5 = yf.download(yahoo, period="60d", interval="5m", progress=False)
        h1 = yf.download(yahoo, period="2y", interval="1h", progress=False)
        for df in [m5, h1]:
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
        if m5.empty or h1.empty:
            print(f"  {name}: NO DATA — skipping")
            continue
        days = (m5.index[-1] - m5.index[0]).days
        months = max(days / 30, 0.5)
        print(f"  {name}: {len(m5)} M5 bars, {days} days, {months:.1f} months")
        data[name] = (m5, h1, months)

    # BTC
    btc_m5 = yf.download("BTC-USD", period="60d", interval="5m", progress=False)
    btc_h1 = yf.download("BTC-USD", period="2y", interval="1h", progress=False)
    for df in [btc_m5, btc_h1]:
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]
    if not btc_m5.empty and not btc_h1.empty:
        btc_days = (btc_m5.index[-1] - btc_m5.index[0]).days
        btc_months = max(btc_days / 30, 0.5)
        print(f"  BTC:    {len(btc_m5)} M5 bars, {btc_days} days, {btc_months:.1f} months")
        data["BTC"] = (btc_m5, btc_h1, btc_months)

    # ── Current optimized config (thresh=8, SL=2×ATR, relaxed limits) ──
    optimized = {
        "signal_threshold": 8.0, "high_conviction_threshold": 11.0,
        "atr_sl_mult": 2.0, "eod_close": True,
        "cooldown_enabled": True, "spread_filter": True,
        "daily_loss_pct": 6.0, "weekly_loss_pct": 15.0,
    }

    # Old config for BTC comparison
    old_cfg = {
        "signal_threshold": 6.0, "high_conviction_threshold": 9.0,
        "atr_sl_mult": 1.5, "eod_close": True,
        "cooldown_enabled": True, "spread_filter": True,
        "daily_loss_pct": 6.0, "weekly_loss_pct": 15.0,
    }

    results = []

    # ── Test all forex pairs with optimized config ──
    print(f"\n\nRunning optimized config (thresh=8, SL=2×ATR, 6%/15% limits)...")
    for name, yahoo, spec in pairs:
        if name not in data:
            continue
        m5, h1, months = data[name]
        cfg = {**optimized, **spec}
        r = backtest(m5.copy(), h1.copy(), INITIAL, cfg)
        s = summarize(f"{name} — optimized", r, months, INITIAL)
        print_detail(s, months)
        results.append(s)

    # ── BTC with optimized config ──
    if "BTC" in data:
        m5, h1, months = data["BTC"]

        # New optimized
        cfg_new = {**optimized, **btc_spec}
        r = backtest(m5.copy(), h1.copy(), INITIAL, cfg_new)
        s = summarize("BTC — NEW (thresh=8, SL=2×ATR)", r, months, INITIAL)
        print_detail(s, months)
        results.append(s)

        # Old config
        cfg_old = {**old_cfg, **btc_spec}
        r = backtest(m5.copy(), h1.copy(), INITIAL, cfg_old)
        s = summarize("BTC — OLD (thresh=6, SL=1.5×ATR)", r, months, INITIAL)
        print_detail(s, months)
        results.append(s)

        # Old config no loss limits (previous best)
        cfg_old_nolim = {**old_cfg, **btc_spec,
                         "daily_loss_pct": 99.0, "weekly_loss_pct": 99.0}
        r = backtest(m5.copy(), h1.copy(), INITIAL, cfg_old_nolim)
        s = summarize("BTC — OLD + no loss limits", r, months, INITIAL)
        print_detail(s, months)
        results.append(s)

        # Optimized no loss limits
        cfg_new_nolim = {**optimized, **btc_spec,
                         "daily_loss_pct": 99.0, "weekly_loss_pct": 99.0}
        r = backtest(m5.copy(), h1.copy(), INITIAL, cfg_new_nolim)
        s = summarize("BTC — NEW + no loss limits", r, months, INITIAL)
        print_detail(s, months)
        results.append(s)

        # BTC no EOD (24/7 crypto, let it run)
        cfg_btc_noeod = {**optimized, **btc_spec,
                         "eod_close": False,
                         "daily_loss_pct": 99.0, "weekly_loss_pct": 99.0}
        r = backtest(m5.copy(), h1.copy(), INITIAL, cfg_btc_noeod)
        s = summarize("BTC — NEW + no EOD + no limits", r, months, INITIAL)
        print_detail(s, months)
        results.append(s)

    # ── COMPARISON TABLE ──
    print(f"\n\n{'=' * 95}")
    print(f"  COMPARISON TABLE — €{INITIAL:,.0f} account")
    print(f"{'=' * 95}")
    print(f"  {'#':>2} {'Pair':<38} {'Balance':>9} {'Ret':>7} {'Mo%':>7} {'#Tr':>4} {'WR':>5} {'PF':>5} {'DD':>5} {'W/L':>5}")
    print(f"  {'-'*2} {'-'*38} {'-'*9} {'-'*7} {'-'*7} {'-'*4} {'-'*5} {'-'*5} {'-'*5} {'-'*5}")

    for i, s in enumerate(results, 1):
        wl = f"{s['avg_win']/s['avg_loss']:.2f}" if s['avg_loss'] > 0 and s['trades'] > 0 else "N/A"
        print(f"  {i:>2} {s['label']:<38} €{s['balance']:>7,.0f} {s['ret']:>+6.1f}% {s['monthly']:>+6.1f}% {s['trades']:>4} {s['wr']:>4.0f}% {s['pf']:>4.2f} {s['dd']:>4.1f}% {wl:>5}")

    # Best forex
    fx_results = [s for s in results if "BTC" not in s["label"]]
    if fx_results:
        best_fx = max(fx_results, key=lambda s: s["monthly"])
        print(f"\n  BEST FOREX: {best_fx['label']} ({best_fx['monthly']:+.1f}%/mo, PF {best_fx['pf']:.2f})")

    # Best BTC
    btc_results = [s for s in results if "BTC" in s["label"]]
    if btc_results:
        best_btc = max(btc_results, key=lambda s: s["monthly"])
        print(f"  BEST BTC:   {best_btc['label']} ({best_btc['monthly']:+.1f}%/mo, PF {best_btc['pf']:.2f})")

    print()


if __name__ == "__main__":
    run()
