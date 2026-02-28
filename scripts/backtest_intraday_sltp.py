#!/usr/bin/env python3
"""Compare 4 intraday SL/TP approaches across XAUUSD and BTC.

Runs backtests for each approach, prints a ranked comparison table,
and recommends the best approach by composite score.

Usage:
    .venv/bin/python scripts/backtest_intraday_sltp.py
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.backtester import Backtester

INSTRUMENTS = ["XAUUSD", "BTC"]
STRATEGIES = [
    "intraday_pure_atr",
    "intraday_relaxed_rr",
    "intraday_atr_capped_sr",
    "intraday_hybrid",
]
PERIOD = "60d"

LABELS = {
    "intraday_pure_atr": "1. Pure ATR",
    "intraday_relaxed_rr": "2. Relaxed R:R",
    "intraday_atr_capped_sr": "3. ATR-capped S/R",
    "intraday_hybrid": "4. Hybrid",
}


def run_all():
    bt = Backtester()
    results = {}

    for inst in INSTRUMENTS:
        for strat in STRATEGIES:
            key = f"{inst}|{strat}"
            label = f"{inst} / {LABELS[strat]}"
            print(f"Running {label} ...", end=" ", flush=True)

            try:
                res = bt.run(
                    instrument_key=inst,
                    strategy=strat,
                    period=PERIOD,
                    initial_balance=10000.0,
                    risk_percent=3.0,
                    atr_sl_multiplier=1.5,
                    atr_tp_multiplier=3.0,
                    session_filter=True,
                    partial_tp=True,
                )
            except Exception as e:
                print(f"ERROR: {e}")
                results[key] = {"error": str(e)}
                continue

            if "error" in res:
                print(f"SKIP: {res['error']}")
                results[key] = res
                continue

            results[key] = res
            print(
                f"{res['total_trades']} trades, "
                f"WR {res['win_rate']:.1f}%, "
                f"PF {res['profit_factor']:.2f}, "
                f"Return {res['total_return_pct']:.1f}%"
            )

    return results


def print_comparison(results: dict):
    """Print side-by-side comparison table."""
    print("\n" + "=" * 90)
    print("INTRADAY SL/TP APPROACH COMPARISON (60-day backtest)")
    print("=" * 90)

    header = f"{'Approach':<22} {'Inst':<7} {'Trades':>6} {'WR%':>6} {'PF':>6} {'Expect':>8} {'MaxDD%':>7} {'Return%':>8}"
    print(header)
    print("-" * 90)

    rows = []
    for inst in INSTRUMENTS:
        for strat in STRATEGIES:
            key = f"{inst}|{strat}"
            res = results.get(key, {})
            if "error" in res:
                print(f"{LABELS[strat]:<22} {inst:<7} {'ERROR':>6}")
                continue

            trades = res.get("total_trades", 0)
            wr = res.get("win_rate", 0)
            pf = res.get("profit_factor", 0)
            exp = res.get("expectancy", 0)
            mdd = res.get("max_drawdown", 0)
            ret = res.get("total_return_pct", 0)

            print(f"{LABELS[strat]:<22} {inst:<7} {trades:>6} {wr:>6.1f} {pf:>6.2f} {exp:>8.2f} {mdd:>7.2f} {ret:>8.1f}")
            rows.append({
                "strat": strat,
                "inst": inst,
                "trades": trades,
                "wr": wr,
                "pf": pf,
                "exp": exp,
                "mdd": mdd,
                "ret": ret,
            })

    # Compute composite scores per strategy (averaged across instruments)
    print("\n" + "=" * 90)
    print("COMPOSITE RANKING (averaged across instruments)")
    print("=" * 90)

    strat_scores = {}
    for strat in STRATEGIES:
        strat_rows = [r for r in rows if r["strat"] == strat and r["trades"] > 0]
        if not strat_rows:
            strat_scores[strat] = -999
            continue

        # Composite: 30% PF + 25% WR-normalized + 25% return + 20% (1 - MDD-normalized)
        avg_pf = sum(r["pf"] for r in strat_rows) / len(strat_rows)
        avg_wr = sum(r["wr"] for r in strat_rows) / len(strat_rows)
        avg_ret = sum(r["ret"] for r in strat_rows) / len(strat_rows)
        avg_mdd = sum(r["mdd"] for r in strat_rows) / len(strat_rows)
        avg_trades = sum(r["trades"] for r in strat_rows) / len(strat_rows)

        # Normalize components (0-10 scale)
        pf_score = min(avg_pf, 3.0) / 3.0 * 10
        wr_score = min(avg_wr, 70) / 70 * 10
        ret_score = max(0, min(avg_ret + 20, 40)) / 40 * 10  # -20% to +20% → 0-10
        mdd_score = max(0, 10 - avg_mdd / 3)  # lower DD = better
        trade_penalty = 0 if avg_trades >= 5 else -3  # penalize too few trades

        composite = (
            0.30 * pf_score +
            0.25 * wr_score +
            0.25 * ret_score +
            0.20 * mdd_score +
            trade_penalty
        )
        strat_scores[strat] = round(composite, 2)

    ranked = sorted(strat_scores.items(), key=lambda x: x[1], reverse=True)
    print(f"{'Rank':<5} {'Approach':<25} {'Score':>8}")
    print("-" * 40)
    for rank, (strat, score) in enumerate(ranked, 1):
        marker = " ← WINNER" if rank == 1 else ""
        print(f"{rank:<5} {LABELS[strat]:<25} {score:>8.2f}{marker}")

    winner = ranked[0][0] if ranked else None
    print(f"\nRecommendation: Use '{winner}' approach for build_trade_payload")
    return winner


if __name__ == "__main__":
    results = run_all()
    winner = print_comparison(results)
