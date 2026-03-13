"""Compare M5 Scalp on BTC vs NZDUSD — head to head.

Tests the exact same M5 scalp scoring engine on both assets
with realistic costs for each.
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


def backtest_m5(symbol, label, cost, min_size, size_round, min_stop,
                risk_pct=4.0, initial=50.0, debounce_bars=12):
    engine = M5ScalpScoringEngine()

    m5 = yf.download(symbol, period="60d", interval="5m", progress=False)
    h1 = yf.download(symbol, period="2y", interval="1h", progress=False)

    for df in [m5, h1]:
        if df.empty:
            print(f"  {label}: NO DATA")
            return
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

    compute_indicators(m5)
    compute_scalp_indicators(m5)
    compute_indicators(h1)

    h1_index = h1.index
    balance = initial
    peak = initial
    max_dd = 0
    trades = []
    in_trade = False
    last_bar = -debounce_bars - 1
    last_dir = None
    equity_curve = []

    for i in range(50, len(m5)):
        if in_trade:
            bh, bl = m5["high"].iloc[i], m5["low"].iloc[i]
            if trade_dir == "BUY":
                if bl <= sl:
                    pnl = (sl - entry) * size - cost * size
                    balance += pnl
                    trades.append({"pnl": pnl, "dir": "BUY", "date": str(m5.index[i]),
                                   "entry": entry, "exit": sl, "size": size,
                                   "bars": i - entry_bar})
                    in_trade = False
                else:
                    pnl_r = (bh - entry) / sl_dist
                    if pnl_r >= 1.0:
                        new_sl = bh - 0.5 * sl_dist
                        if new_sl > sl:
                            sl = new_sl
            else:
                if bh >= sl:
                    pnl = (entry - sl) * size - cost * size
                    balance += pnl
                    trades.append({"pnl": pnl, "dir": "SELL", "date": str(m5.index[i]),
                                   "entry": entry, "exit": sl, "size": size,
                                   "bars": i - entry_bar})
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
            atr = m5["atr"].iloc[i]
            if pd.isna(atr) or atr <= 0:
                continue

            m5_time = m5.index[i]
            h1_mask = h1_index <= m5_time
            if not h1_mask.any():
                continue
            h1_row = h1.loc[h1_index[h1_mask][-1]]

            m5_tail = m5.iloc[max(0, i-5):i+1]
            result = engine.score(h1_row, m5_tail, bar_time=m5_time)

            if result["direction"] is None:
                continue
            direction = result["direction"]

            if (i - last_bar) < debounce_bars and last_dir == direction:
                continue

            entry = m5["close"].iloc[i]
            entry_bar = i
            trade_dir = direction
            sl_dist = max(1.0 * atr, min_stop)

            if direction == "BUY":
                sl = entry - sl_dist
            else:
                sl = entry + sl_dist

            risk_amt = balance * risk_pct / 100
            size = risk_amt / sl_dist if sl_dist > 0 else 0
            size = max(round(size / size_round) * size_round, min_size)

            actual_risk = size * sl_dist
            if actual_risk > balance * 0.5 or size <= 0:
                continue

            in_trade = True
            last_bar = i
            last_dir = direction

        equity_curve.append(balance)

    days = (m5.index[-1] - m5.index[0]).days
    months = max(days / 30, 0.5)

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  Data: {len(m5)} M5 bars, {len(h1)} H1 bars ({days} days, {months:.1f} months)")

    if not trades:
        print(f"  NO TRADES generated\n")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wp = sum(t["pnl"] for t in wins) if wins else 0
    lp = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    ret = (balance - initial) / initial * 100
    mo_ret = ret / months
    wr = len(wins) / len(trades) * 100
    pf = wp / lp
    aw = np.mean([t["pnl"] for t in wins]) if wins else 0
    al = np.mean([abs(t["pnl"]) for t in losses]) if losses else 0
    avg_bars = np.mean([t["bars"] for t in trades])
    pnls = [t["pnl"] for t in trades]
    sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls)) if np.std(pnls) > 0 else 0

    # Streak analysis
    max_win_streak = max_loss_streak = cur_w = cur_l = 0
    for t in trades:
        if t["pnl"] > 0:
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_win_streak = max(max_win_streak, cur_w)
        max_loss_streak = max(max_loss_streak, cur_l)

    # Monthly breakdown
    monthly_pnl = {}
    for t in trades:
        mo_key = t["date"][:7]
        monthly_pnl.setdefault(mo_key, 0)
        monthly_pnl[mo_key] += t["pnl"]

    print(f"\n  Trades: {len(trades)} ({len(trades)/months:.0f}/mo)")
    print(f"  Win Rate: {wr:.1f}%")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Sharpe Ratio: {sharpe:.2f}")
    print(f"  Final Balance: €{balance:.2f} (from €{initial:.2f})")
    print(f"  Total Return: {ret:+.1f}%")
    print(f"  Monthly Return: {mo_ret:+.1f}%/mo")
    print(f"  Max Drawdown: {max_dd:.1f}%")
    print(f"  Avg Win: €{aw:.3f} | Avg Loss: €{al:.3f}")
    print(f"  Avg Win/Loss Ratio: {aw/al:.2f}x" if al > 0 else "")
    print(f"  Avg Trade Duration: {avg_bars:.0f} bars ({avg_bars*5:.0f} min)")
    print(f"  Max Win Streak: {max_win_streak} | Max Loss Streak: {max_loss_streak}")
    print(f"  Buys: {sum(1 for t in trades if t['dir']=='BUY')} | Sells: {sum(1 for t in trades if t['dir']=='SELL')}")

    # Direction breakdown
    buy_trades = [t for t in trades if t["dir"] == "BUY"]
    sell_trades = [t for t in trades if t["dir"] == "SELL"]
    buy_wr = sum(1 for t in buy_trades if t["pnl"] > 0) / len(buy_trades) * 100 if buy_trades else 0
    sell_wr = sum(1 for t in sell_trades if t["pnl"] > 0) / len(sell_trades) * 100 if sell_trades else 0
    print(f"  BUY WR: {buy_wr:.1f}% ({len(buy_trades)} trades) | SELL WR: {sell_wr:.1f}% ({len(sell_trades)} trades)")

    # Monthly breakdown
    print(f"\n  Monthly PnL:")
    running = initial
    for mo_key in sorted(monthly_pnl):
        pnl = monthly_pnl[mo_key]
        running += pnl
        print(f"    {mo_key}: €{pnl:>+7.2f} (bal: €{running:.2f})")

    # Show last 15 trades
    print(f"\n  Last 15 trades:")
    print(f"  {'#':>3} | {'Date':<20} | {'Dir':<5} | {'PnL':>8} | {'Bars':>5}")
    print(f"  {'-'*3}-+-{'-'*20}-+-{'-'*5}-+-{'-'*8}-+-{'-'*5}")
    for i, t in enumerate(trades[-15:], len(trades)-14):
        print(f"  {i:>3} | {t['date'][:19]:<20} | {t['dir']:<5} | €{t['pnl']:>+7.3f} | {t['bars']:>5}")


def run():
    print("=" * 70)
    print("M5 SCALP: BTC vs NZDUSD — Head to Head")
    print("=" * 70)
    print("Same scoring engine, same risk %, same trailing SL logic")
    print()

    # NZDUSD — current live setup
    backtest_m5(
        symbol="NZDUSD=X",
        label="NZDUSD M5 Scalp (LIVE setup)",
        cost=0.00012,       # ~1.2 pip
        min_size=1000,      # 0.01 lots
        size_round=1000,
        min_stop=0.0020,    # 20 pips
    )

    # BTC — IC Markets CFD
    backtest_m5(
        symbol="BTC-USD",
        label="BTC M5 Scalp (IC Markets CFD)",
        cost=30.0,          # ~$30 spread + slippage
        min_size=0.001,     # 0.001 BTC
        size_round=0.001,
        min_stop=250.0,     # $250 min stop
    )

    # BTC with wider stop (BTC is volatile)
    backtest_m5(
        symbol="BTC-USD",
        label="BTC M5 Scalp — wider stop (1.5x ATR, $500 min)",
        cost=30.0,
        min_size=0.001,
        size_round=0.001,
        min_stop=500.0,
    )

    print(f"\n\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print("Compare the metrics above — key things to watch:")
    print("  - Win rate & profit factor (edge)")
    print("  - Max drawdown (risk)")
    print("  - Avg trade duration (BTC may hold longer)")
    print("  - Monthly return consistency")
    print()


if __name__ == "__main__":
    run()
