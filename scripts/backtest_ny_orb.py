#!/usr/bin/env python3
"""Backtest NY Opening Range Breakout strategy across multiple instruments."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.backtester import Backtester


def main():
    bt = Backtester()

    instruments = [
        "EURUSD",
        "GBPUSD",
        "AUDUSD",
        "NZDUSD",
        "USDJPY",
        "XAUUSD",
        "IBUS500",
    ]

    period = "60d"  # M5 data limited to ~60 days on yfinance

    print("=" * 90)
    print("NY OPENING RANGE BREAKOUT — BACKTEST RESULTS")
    print(f"Period: {period} | TP = 2× SL | Risk: 3%")
    print("=" * 90)

    results = []
    for inst in instruments:
        print(f"\nRunning {inst}...", end=" ", flush=True)
        try:
            result = bt.run(
                instrument_key=inst,
                strategy="ny_orb",
                period=period,
                initial_balance=10000.0,
                risk_percent=3.0,
                session_filter=False,
                partial_tp=False,
            )
            if "error" in result:
                print(f"ERROR: {result['error']}")
                continue
            results.append(result)
            print(f"OK — {result['total_trades']} trades")
        except Exception as e:
            print(f"FAILED: {e}")

    # Summary table
    print("\n" + "=" * 90)
    print(f"{'Instrument':<12} {'Trades':>7} {'Wins':>6} {'Losses':>7} {'WR%':>7} "
          f"{'PF':>7} {'Expect':>8} {'MaxDD%':>8} {'Return%':>9} {'Final$':>10}")
    print("-" * 90)

    for r in results:
        print(
            f"{r['instrument']:<12} "
            f"{r['total_trades']:>7} "
            f"{r['winning_trades']:>6} "
            f"{r['losing_trades']:>7} "
            f"{r['win_rate']:>6.1f}% "
            f"{r['profit_factor']:>7.2f} "
            f"{r['expectancy']:>8.2f} "
            f"{r['max_drawdown']:>7.2f}% "
            f"{r['total_return_pct']:>8.2f}% "
            f"{r['final_balance']:>10.2f}"
        )

    print("=" * 90)

    # Show monthly breakdown for best performers
    profitable = [r for r in results if r['total_return_pct'] > 0]
    profitable.sort(key=lambda x: x['total_return_pct'], reverse=True)

    if profitable:
        print(f"\nTop performer: {profitable[0]['instrument']} "
              f"({profitable[0]['total_return_pct']:.1f}% return)")
        if profitable[0].get("monthly_breakdown"):
            print("\nMonthly breakdown:")
            for m in profitable[0]["monthly_breakdown"]:
                print(f"  {m['month']}: {m['trades']} trades, "
                      f"WR {m['win_rate']:.0f}%, PnL ${m['pnl']:.2f}")

    # Show trade details for each instrument
    for r in results:
        if not r.get("trades"):
            continue
        print(f"\n--- {r['instrument']} Trade Log ({len(r['trades'])} trades) ---")
        for t in r["trades"][:20]:  # Show first 20
            print(f"  {t['entry_date'][:16]} {t['direction']:>4} "
                  f"@ {t['entry_price']:.5f} → {t['exit_price']:.5f} "
                  f"SL={t['sl_price']:.5f} TP={t['tp_price']:.5f} "
                  f"PnL=${t['pnl']:>8.2f} [{t['exit_reason']}] {t['conviction']}")
        if len(r["trades"]) > 20:
            print(f"  ... and {len(r['trades']) - 20} more trades")


if __name__ == "__main__":
    main()
