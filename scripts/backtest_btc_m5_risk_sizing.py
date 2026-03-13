"""BTC M5 Scalp + EOD Close — 3% risk-based sizing for €1k and €10k accounts."""

import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from app.services.indicators import compute_indicators, compute_scalp_indicators
from app.services.m5_scalp_scoring import M5ScalpScoringEngine

COST_PER_BTC = 30.0        # ~$30 spread+slippage per BTC
MIN_SIZE = 0.001            # 0.001 BTC minimum
SIZE_ROUND = 0.001
MIN_STOP = 250.0            # $250 min stop distance
SIGNAL_THRESHOLD = 6.0
RISK_PCT = 3.0
DEBOUNCE_BARS = 12
EOD_HOUR = 20
EOD_MINUTE = 55
SWAP_PER_BTC_PER_NIGHT = 0.50  # ~$0.50 per 0.001 BTC per night (rough estimate)
MAX_LEVERAGE = 2  # IC Markets retail BTC CFD leverage (1:2)
EURUSD_RATE = 1.09  # approximate for margin calc


def is_eod(bar_time):
    return bar_time.hour > EOD_HOUR or (bar_time.hour == EOD_HOUR and bar_time.minute >= EOD_MINUTE)


def is_overnight(prev_time, curr_time):
    if prev_time is None:
        return False
    return prev_time.date() != curr_time.date()


def backtest_btc_eod(m5_df, h1_df, initial_balance):
    engine = M5ScalpScoringEngine(signal_threshold=SIGNAL_THRESHOLD)
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)

    balance = initial_balance
    peak = initial_balance
    lowest = initial_balance
    max_dd = 0
    trades = []
    h1_index = h1_df.index

    pos = None  # single position
    last_bar = -DEBOUNCE_BARS - 1
    last_dir = None
    swap_total = 0.0
    eod_closes = 0
    prev_bar_time = None

    for i in range(50, len(m5_df)):
        bar_time = m5_df.index[i]
        bh, bl, bc = m5_df["high"].iloc[i], m5_df["low"].iloc[i], m5_df["close"].iloc[i]

        # Swap for overnight
        if is_overnight(prev_bar_time, bar_time) and pos:
            swap = SWAP_PER_BTC_PER_NIGHT * (pos["size"] / 0.001)
            balance -= swap
            swap_total += swap

        # EOD close
        if is_eod(bar_time) and pos:
            if pos["dir"] == "BUY":
                pnl = (bc - pos["entry"]) * pos["size"] - COST_PER_BTC * pos["size"]
            else:
                pnl = (pos["entry"] - bc) * pos["size"] - COST_PER_BTC * pos["size"]
            balance += pnl
            trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                           "exit": bc, "size": pos["size"], "reason": "eod",
                           "ratchet": pos["ratchet"], "date": str(bar_time)})
            pos = None
            eod_closes += 1

        # Manage open position
        if pos:
            sl_hit = False
            if pos["dir"] == "BUY":
                if bl <= pos["sl"]:
                    pnl = (pos["sl"] - pos["entry"]) * pos["size"] - COST_PER_BTC * pos["size"]
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
                    pnl = (pos["entry"] - pos["sl"]) * pos["size"] - COST_PER_BTC * pos["size"]
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
                trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                               "exit": pos["sl"], "size": pos["size"], "reason": "sl",
                               "ratchet": pos["ratchet"], "date": str(bar_time)})
                pos = None

        # Track equity
        if balance < lowest: lowest = balance
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

        # New entry (no position, not EOD)
        if pos is None and not is_eod(bar_time):
            atr = m5_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
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
                if (i - last_bar) < DEBOUNCE_BARS and last_dir == direction:
                    prev_bar_time = bar_time
                    continue

                entry = bc
                sl_dist = max(1.0 * atr, MIN_STOP)

                # 3% risk-based position sizing, capped by leverage
                risk_amt = balance * RISK_PCT / 100
                size = risk_amt / sl_dist if sl_dist > 0 else 0
                # Cap by margin: max notional = balance * leverage
                max_notional = balance * MAX_LEVERAGE * EURUSD_RATE  # EUR→USD
                max_size_by_margin = max_notional / entry if entry > 0 else 0
                size = min(size, max_size_by_margin)
                size = max(round(size / SIZE_ROUND) * SIZE_ROUND, MIN_SIZE)

                actual_risk = size * sl_dist
                if actual_risk > balance * 0.5 or size <= 0:
                    prev_bar_time = bar_time
                    continue

                sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
                pos = {"dir": direction, "entry": entry, "sl": sl,
                       "sl_dist": sl_dist, "size": size, "ratchet": 0}
                last_bar = i
                last_dir = direction

        prev_bar_time = bar_time

    # Close remaining at last price
    if pos:
        last_close = m5_df["close"].iloc[-1]
        if pos["dir"] == "BUY":
            pnl = (last_close - pos["entry"]) * pos["size"] - COST_PER_BTC * pos["size"]
        else:
            pnl = (pos["entry"] - last_close) * pos["size"] - COST_PER_BTC * pos["size"]
        balance += pnl
        trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                       "exit": last_close, "size": pos["size"], "reason": "end",
                       "ratchet": pos.get("ratchet", 0), "date": str(m5_df.index[-1])})

    return {
        "trades": trades, "balance": balance, "lowest": lowest,
        "max_dd": max_dd, "swap_total": swap_total, "eod_closes": eod_closes,
    }


def print_results(label, r, initial, months):
    trades = r["trades"]
    if not trades:
        print(f"  {label}: No trades")
        return

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
    avg_size = np.mean([t["size"] for t in trades])

    eod_count = sum(1 for t in trades if t.get("reason") == "eod")
    sl_count = sum(1 for t in trades if t.get("reason") == "sl")

    print(f"\n{'=' * 65}")
    print(f"  {label}")
    print(f"{'=' * 65}")
    print(f"  Trades:        {len(trades)} ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate:      {wr:.1f}%")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Avg Win:       €{avg_win:.2f} | Avg Loss: €{avg_loss:.2f}")
    print(f"  Avg Position:  {avg_size:.4f} BTC")
    print(f"  Final Balance: €{balance:.2f} (from €{initial:.0f})")
    print(f"  Lowest Balance:€{r['lowest']:.2f}")
    print(f"  Return:        {ret:+.1f}%")
    print(f"  Monthly:       {monthly:+.1f}%/month")
    print(f"  Max Drawdown:  {r['max_dd']:.1f}%")
    print(f"  Swap Fees:     €{r['swap_total']:.2f}")
    print(f"  Exits:         {sl_count} SL, {eod_count} EOD close")

    # Monthly breakdown
    monthly_pnl = {}
    for t in trades:
        mo = t["date"][:7]
        monthly_pnl.setdefault(mo, 0)
        monthly_pnl[mo] += t["pnl"]

    print(f"\n  Monthly PnL:")
    running = initial
    for mo in sorted(monthly_pnl):
        p = monthly_pnl[mo]
        running += p
        mo_ret = p / (running - p) * 100 if (running - p) > 0 else 0
        print(f"    {mo}: €{p:>+10.2f} ({mo_ret:+.1f}%)  bal: €{running:.2f}")

    # Projections
    print(f"\n  Growth projections ({monthly:+.1f}%/mo compounding):")
    for m, lbl in [(3, "3 months"), (6, "6 months"), (12, "12 months")]:
        proj = initial * (1 + monthly / 100) ** m
        print(f"    {lbl}: €{proj:,.2f}")

    # Last 10 trades
    print(f"\n  Last 10 trades:")
    print(f"  {'#':>3} | {'Date':<20} | {'Dir':<5} | {'Size':>8} | {'PnL':>10} | {'Exit':<4}")
    print(f"  {'-'*3}-+-{'-'*20}-+-{'-'*5}-+-{'-'*8}-+-{'-'*10}-+-{'-'*4}")
    for idx, t in enumerate(trades[-10:], len(trades) - 9):
        print(f"  {idx:>3} | {t['date'][:19]:<20} | {t['dir']:<5} | {t['size']:>8.4f} | €{t['pnl']:>+9.2f} | {t['reason']:<4}")


def backtest_nzd_eod(m5_df, h1_df, initial_balance):
    """NZDUSD variant — different costs, sizing, leverage."""
    NZD_COST = 0.00012       # ~1.2 pip spread
    NZD_MIN_SIZE = 1000      # 0.01 lots
    NZD_SIZE_ROUND = 1000
    NZD_MIN_STOP = 0.0005    # 5 pips
    NZD_MAX_LEVERAGE = 30    # IC Markets retail FX leverage (1:30)
    NZD_SWAP_PER_NIGHT = 0.05  # €0.05/night per 1000 units

    engine = M5ScalpScoringEngine(signal_threshold=SIGNAL_THRESHOLD)
    compute_indicators(m5_df)
    compute_scalp_indicators(m5_df)
    compute_indicators(h1_df)

    balance = initial_balance
    peak = initial_balance
    lowest = initial_balance
    max_dd = 0
    trades = []
    h1_index = h1_df.index

    pos = None
    last_bar = -DEBOUNCE_BARS - 1
    last_dir = None
    swap_total = 0.0
    eod_closes = 0
    prev_bar_time = None

    for i in range(50, len(m5_df)):
        bar_time = m5_df.index[i]
        bh, bl, bc = m5_df["high"].iloc[i], m5_df["low"].iloc[i], m5_df["close"].iloc[i]

        # Swap
        if is_overnight(prev_bar_time, bar_time) and pos:
            swap = NZD_SWAP_PER_NIGHT * (pos["size"] / 1000)
            balance -= swap
            swap_total += swap

        # EOD close
        if is_eod(bar_time) and pos:
            if pos["dir"] == "BUY":
                pnl = (bc - pos["entry"]) * pos["size"] - NZD_COST * pos["size"]
            else:
                pnl = (pos["entry"] - bc) * pos["size"] - NZD_COST * pos["size"]
            balance += pnl
            trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                           "exit": bc, "size": pos["size"], "reason": "eod",
                           "ratchet": pos["ratchet"], "date": str(bar_time)})
            pos = None
            eod_closes += 1

        # Manage position
        if pos:
            sl_hit = False
            if pos["dir"] == "BUY":
                if bl <= pos["sl"]:
                    pnl = (pos["sl"] - pos["entry"]) * pos["size"] - NZD_COST * pos["size"]
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
                    pnl = (pos["entry"] - pos["sl"]) * pos["size"] - NZD_COST * pos["size"]
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
                trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                               "exit": pos["sl"], "size": pos["size"], "reason": "sl",
                               "ratchet": pos["ratchet"], "date": str(bar_time)})
                pos = None

        # Equity tracking
        if balance < lowest: lowest = balance
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

        # New entry
        if pos is None and not is_eod(bar_time):
            atr = m5_df["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
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
                if (i - last_bar) < DEBOUNCE_BARS and last_dir == direction:
                    prev_bar_time = bar_time
                    continue

                entry = bc
                sl_dist = max(1.0 * atr, NZD_MIN_STOP)

                # 3% risk sizing capped by leverage
                risk_amt = balance * RISK_PCT / 100
                size = risk_amt / sl_dist if sl_dist > 0 else 0
                # Leverage cap: max notional = balance * leverage (NZDUSD ~$0.58, so notional = size * price)
                max_notional = balance * NZD_MAX_LEVERAGE * EURUSD_RATE
                max_size_by_margin = max_notional / entry if entry > 0 else 0
                size = min(size, max_size_by_margin)
                size = max(round(size / NZD_SIZE_ROUND) * NZD_SIZE_ROUND, NZD_MIN_SIZE)

                actual_risk = size * sl_dist
                if actual_risk > balance * 0.5 or size <= 0:
                    prev_bar_time = bar_time
                    continue

                sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
                pos = {"dir": direction, "entry": entry, "sl": sl,
                       "sl_dist": sl_dist, "size": size, "ratchet": 0}
                last_bar = i
                last_dir = direction

        prev_bar_time = bar_time

    # Close remaining
    if pos:
        last_close = m5_df["close"].iloc[-1]
        if pos["dir"] == "BUY":
            pnl = (last_close - pos["entry"]) * pos["size"] - NZD_COST * pos["size"]
        else:
            pnl = (pos["entry"] - last_close) * pos["size"] - NZD_COST * pos["size"]
        balance += pnl
        trades.append({"pnl": pnl, "dir": pos["dir"], "entry": pos["entry"],
                       "exit": last_close, "size": pos["size"], "reason": "end",
                       "ratchet": pos.get("ratchet", 0), "date": str(m5_df.index[-1])})

    return {
        "trades": trades, "balance": balance, "lowest": lowest,
        "max_dd": max_dd, "swap_total": swap_total, "eod_closes": eod_closes,
    }


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
    print(f"Setup: 3% risk per trade, EOD close at 20:55 UTC, ratchet trailing SL")
    print(f"NZDUSD: ~1.2 pip spread, min 1000 units, 1:30 leverage")

    # €1,000 account
    r1 = backtest_nzd_eod(m5.copy(), h1.copy(), initial_balance=1000.0)
    print_results("NZDUSD M5 Scalp + EOD — €1,000 Account (3% risk)", r1, 1000.0, months)

    # €10,000 account
    r2 = backtest_nzd_eod(m5.copy(), h1.copy(), initial_balance=10000.0)
    print_results("NZDUSD M5 Scalp + EOD — €10,000 Account (3% risk)", r2, 10000.0, months)

    # Side by side
    print(f"\n{'=' * 65}")
    print(f"  COMPARISON: €1k vs €10k")
    print(f"{'=' * 65}")
    for lbl, r, init in [("€1,000", r1, 1000), ("€10,000", r2, 10000)]:
        n = len(r["trades"])
        ret = (r["balance"] - init) / init * 100
        wr = len([t for t in r["trades"] if t["pnl"] > 0]) / n * 100 if n else 0
        print(f"  {lbl:>8}  →  €{r['balance']:>12,.2f}  {ret:+6.1f}%  "
              f"{n} trades  {wr:.0f}% WR  DD:{r['max_dd']:.1f}%")


if __name__ == "__main__":
    run()
