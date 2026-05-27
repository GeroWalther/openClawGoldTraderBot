#!/usr/bin/env python3
from __future__ import annotations
"""
paper_pnl.py — simulate P&L on paper-mode AI trade decisions.

Reads ai/state/trade_reasoning/*.json (all decisions with action != PASS),
fetches historical bars from the gold-trader /api/v1/technicals endpoint,
simulates SL/TP hit sequence, computes P&L per trade.

Writes ai/state/paper_history.csv with one row per closed paper trade.
Can be run as a cron job (every 5 min) or called from ai_review.sh.

Usage:
    python3 paper_pnl.py           # incremental update (only new decisions)
    python3 paper_pnl.py --rebuild # rebuild entire history from scratch
"""
import os
import sys
import json
import csv
import glob
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

AI_DIR = Path(__file__).parent.parent.resolve()
PROJECT_DIR = AI_DIR.parent
REASONING_DIR = AI_DIR / "state" / "trade_reasoning"
HISTORY_FILE = AI_DIR / "state" / "paper_history.csv"
OPEN_PAPER_FILE = AI_DIR / "state" / "paper_open.json"

# Load API key from .env
ENV_FILE = PROJECT_DIR / ".env"
API_KEY = ""
BOT_URL = "http://localhost:8001"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("API_SECRET_KEY="):
            API_KEY = line.split("=", 1)[1].strip()

HEADERS = [
    "paper_id", "decision_timestamp", "closed_at", "instrument", "direction",
    "order_type", "entry_price", "actual_entry", "stop_loss", "take_profit",
    "size", "conviction", "strategy",
    "exit_price", "exit_reason",  # "TP" | "SL" | "OPEN"
    "pnl_pips", "pnl_pct", "pnl_eur",
    "r_multiple", "duration_minutes",
    "ai_reasoning"
]

# EUR PnL conversion factor per unit per 1.0 of raw price change.
# NOTE: pnl_pips in simulate_trade() is a RAW PRICE CHANGE (e.g. 0.0030 for FX),
# not a pip count. So this factor must convert raw_price_change × size → EUR.
# For USD-quoted instruments with size in base units: $ PnL = price_change × size,
# then × 0.92 to convert USD → EUR (~1.08 USD/EUR). Prior FX values were
# 10000× too small (treated as per-pip not per-price-unit) — fixed 2026-05-21.
USD_TO_EUR = 0.92
PIP_VALUE_PER_UNIT_EUR = {
    "XAUUSD": USD_TO_EUR,   # 1 oz × $1 move = $1 → €0.92
    "EURUSD": USD_TO_EUR,
    "AUDUSD": USD_TO_EUR,
    "NZDUSD": USD_TO_EUR,
    "GBPUSD": USD_TO_EUR,
    "BTC":    USD_TO_EUR,   # 1 BTC × $1 move = $1 → €0.92
    "BTCUSD": USD_TO_EUR,
    "US500":  USD_TO_EUR,
    "NAS100": USD_TO_EUR,
    "JPN225": USD_TO_EUR / 150.0,  # ¥-quoted, ~150 JPY/USD
}

# Default paper size per instrument (smallest sensible)
DEFAULT_PAPER_SIZE = {
    "XAUUSD": 1,       # 1 oz
    "EURUSD": 1000,    # 1k units
    "AUDUSD": 1000,
    "NZDUSD": 1000,
    "GBPUSD": 1000,
    "BTC":    0.01,
    "BTCUSD": 0.01,
    "US500":  1,       # 1 unit CFD
    "NAS100": 1,
    "JPN225": 1,
}


def api_get(endpoint: str) -> dict | list | None:
    url = f"{BOT_URL}{endpoint}"
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"api_get({endpoint}) failed: {e}", file=sys.stderr)
        return None


YAHOO_SYMBOL_MAP = {
    "XAUUSD": "GC=F", "EURUSD": "EURUSD=X", "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
    "EURJPY": "EURJPY=X", "CADJPY": "CADJPY=X",
    "BTC": "BTC-USD", "BTCUSD": "BTC-USD",
    "US500": "^GSPC", "NAS100": "^IXIC", "JPN225": "^N225",
}


def fetch_bars(instrument: str, timeframe: str = "1m", days: int = 7) -> list[dict]:
    """Fetch 1-min bars directly from yfinance for maximum paper P&L accuracy.

    yfinance limits:
      - 1m bars available for last 7 days
      - 5m available for last 60 days
      - 15m available for last 60 days

    Falls back to 5m if 1m unavailable.
    """
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed, falling back to bot API", file=sys.stderr)
        return _fetch_bars_via_api(instrument)

    yahoo_sym = YAHOO_SYMBOL_MAP.get(instrument, instrument + "=X")
    period = f"{min(days, 7)}d" if timeframe == "1m" else f"{min(days, 60)}d"

    try:
        ticker = yf.Ticker(yahoo_sym)
        df = ticker.history(period=period, interval=timeframe, auto_adjust=False)
        if df is None or df.empty:
            if timeframe == "1m":
                return fetch_bars(instrument, "5m", 60)
            return []
        df.columns = [c.lower() for c in df.columns]
        bars = []
        for ts, row in df.iterrows():
            bars.append({
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "volume": float(row.get("volume", 0)),
            })
        return bars
    except Exception as e:
        print(f"yfinance fetch failed for {yahoo_sym}: {e}", file=sys.stderr)
        if timeframe == "1m":
            return fetch_bars(instrument, "5m", 60)
        return []


def _fetch_bars_via_api(instrument: str) -> list[dict]:
    """Fallback: use gold-trader technicals endpoint (5-min bars)."""
    data = api_get(f"/api/v1/technicals/{instrument}")
    if not data:
        return []
    tech = data.get("technicals", {})
    for tf in ["m5", "m15", "h1"]:
        bars_data = tech.get(tf, {}).get("bars")
        if bars_data:
            return bars_data
    return []


def simulate_trade(decision: dict, bars: list[dict]) -> dict:
    """Walk bars from decision time, check if SL or TP hit first."""
    inst = decision["instrument"]
    direction = decision["action"]
    entry_price = decision.get("entry_price")
    sl = decision["stop_loss"]
    tp = decision["take_profit"]
    order_type = decision.get("order_type", "MARKET")
    decision_ts = datetime.fromisoformat(decision["_decision_time"])

    # Filter to bars AFTER decision
    future_bars = []
    for b in bars:
        try:
            bar_ts = datetime.fromisoformat(b["timestamp"].replace("Z", "+00:00"))
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.replace(tzinfo=timezone.utc)
            if bar_ts >= decision_ts:
                future_bars.append((bar_ts, b))
        except Exception:
            continue

    if not future_bars:
        return {"exit_reason": "NO_DATA"}

    # Check spread at decision time (estimate: 0.5 pip for majors, 5-10 pips for gold/BTC)
    spread_buffer = 0.0
    if inst in ("XAUUSD",):
        spread_buffer = 0.20  # ~$0.20 spread on gold
    elif inst in ("BTC", "BTCUSD"):
        spread_buffer = 5.0
    elif inst in ("EURUSD", "AUDUSD", "NZDUSD", "GBPUSD"):
        spread_buffer = 0.00005
    elif inst in ("USDJPY", "EURJPY", "CADJPY"):
        spread_buffer = 0.005

    # Fill logic
    filled = False
    actual_entry = None
    fill_time = None

    if order_type == "MARKET":
        filled = True
        raw_fill = future_bars[0][1].get("open") or future_bars[0][1].get("close")
        # Apply spread: BUY pays ask (open + spread/2), SELL gets bid (open - spread/2)
        actual_entry = raw_fill + spread_buffer / 2 if direction == "BUY" else raw_fill - spread_buffer / 2
        fill_time = future_bars[0][0]
    else:  # LIMIT / STOP
        target = entry_price
        for bar_ts, b in future_bars:
            h, l = b.get("high", 0), b.get("low", 0)
            if order_type == "LIMIT":
                # BUY LIMIT fills when low <= entry; SELL LIMIT when high >= entry
                if (direction == "BUY" and l <= target) or (direction == "SELL" and h >= target):
                    filled = True
                    actual_entry = target
                    fill_time = bar_ts
                    break
            elif order_type == "STOP":
                # BUY STOP fills when high >= entry; SELL STOP when low <= entry
                if (direction == "BUY" and h >= target) or (direction == "SELL" and l <= target):
                    filled = True
                    actual_entry = target
                    fill_time = bar_ts
                    break

        # Pending expiry — 4h for market orders, 24h for others
        if not filled:
            expiry = decision_ts + timedelta(hours=24)
            if future_bars[-1][0] > expiry:
                return {"exit_reason": "EXPIRED", "actual_entry": None}
            return {"exit_reason": "OPEN", "actual_entry": None}

    # Walk forward from fill — check which hits first, SL or TP
    post_fill_bars = [(ts, b) for ts, b in future_bars if ts >= fill_time]

    for bar_ts, b in post_fill_bars[1:]:  # skip fill bar itself
        h, l = b.get("high", 0), b.get("low", 0)

        if direction == "BUY":
            hit_sl = l <= sl
            hit_tp = h >= tp
        else:
            hit_sl = h >= sl
            hit_tp = l <= tp

        if hit_sl and hit_tp:
            # Both hit in same bar — assume SL hit first (worst-case / honest)
            exit_price, exit_reason = sl, "SL"
        elif hit_sl:
            exit_price, exit_reason = sl, "SL"
        elif hit_tp:
            exit_price, exit_reason = tp, "TP"
        else:
            continue

        duration_min = (bar_ts - fill_time).total_seconds() / 60
        pnl_pips = (exit_price - actual_entry) if direction == "BUY" else (actual_entry - exit_price)
        r = abs(actual_entry - sl)
        r_multiple = pnl_pips / r if r > 0 else 0
        pnl_pct = (pnl_pips / actual_entry) * 100 if actual_entry else 0

        unit_value = PIP_VALUE_PER_UNIT_EUR.get(inst, 0.0001)
        size = DEFAULT_PAPER_SIZE.get(inst, 1000)
        pnl_eur = pnl_pips * size * unit_value

        return {
            "exit_reason": exit_reason,
            "actual_entry": actual_entry,
            "fill_time": fill_time.isoformat(),
            "exit_price": exit_price,
            "exit_time": bar_ts.isoformat(),
            "pnl_pips": round(pnl_pips, 6),
            "pnl_pct": round(pnl_pct, 4),
            "pnl_eur": round(pnl_eur, 2),
            "r_multiple": round(r_multiple, 2),
            "duration_minutes": round(duration_min, 1),
            "size": size,
        }

    # Neither hit yet — still open
    return {"exit_reason": "OPEN", "actual_entry": actual_entry, "fill_time": fill_time.isoformat(), "size": DEFAULT_PAPER_SIZE.get(inst, 1000)}


def load_decision(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
        if d.get("action") not in ("BUY", "SELL"):
            return None
        # Parse decision timestamp from filename: YYYYMMDD_HHMMSS_INST_ACTION.json
        fn = path.stem  # e.g. 20260414_164712_XAUUSD_BUY
        parts = fn.split("_")
        date_part = parts[0]
        time_part = parts[1]
        ts = datetime.strptime(f"{date_part}_{time_part}", "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        d["_decision_time"] = ts.isoformat()
        d["_paper_id"] = fn
        return d
    except Exception as e:
        print(f"Failed to parse {path}: {e}", file=sys.stderr)
        return None


def is_market_hours():
    """London+NY trading window: 07-21 UTC Mon-Fri."""
    now = datetime.now(timezone.utc)
    return now.weekday() < 5 and 7 <= now.hour < 21


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true", help="Rebuild history from scratch")
    parser.add_argument("--force", action="store_true", help="Run even outside market hours")
    args = parser.parse_args()

    if not args.force and not args.rebuild and not is_market_hours():
        # Silent exit outside market hours (gated like bash scripts)
        return

    REASONING_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing history
    existing_ids = set()
    if HISTORY_FILE.exists() and not args.rebuild:
        with open(HISTORY_FILE) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("exit_reason") in ("TP", "SL", "EXPIRED", "MANUAL_CLOSE"):
                    existing_ids.add(row["paper_id"])

    # Load all decisions
    decisions = []
    for p in sorted(glob.glob(str(REASONING_DIR / "*.json"))):
        d = load_decision(Path(p))
        if d and d["_paper_id"] not in existing_ids:
            decisions.append(d)

    if not decisions:
        print(f"No new paper decisions to process. Existing: {len(existing_ids)} closed trades.")
        # Still print summary
        if HISTORY_FILE.exists():
            print_summary()
        return

    print(f"Processing {len(decisions)} new paper decisions...")

    # Group by instrument, fetch bars once each
    bars_cache = {}
    results = []

    for d in decisions:
        inst = d["instrument"]
        if inst not in bars_cache:
            bars_cache[inst] = fetch_bars(inst, "1m", 7)  # 1-min from yfinance
        bars = bars_cache[inst]

        sim = simulate_trade(d, bars)

        row = {
            "paper_id": d["_paper_id"],
            "decision_timestamp": d["_decision_time"],
            "closed_at": sim.get("exit_time", ""),
            "instrument": inst,
            "direction": d["action"],
            "order_type": d.get("order_type", "MARKET"),
            "entry_price": d.get("entry_price", ""),
            "actual_entry": sim.get("actual_entry", ""),
            "stop_loss": d.get("stop_loss", ""),
            "take_profit": d.get("take_profit", ""),
            "size": sim.get("size", DEFAULT_PAPER_SIZE.get(inst, 1000)),
            "conviction": d.get("conviction", ""),
            "strategy": d.get("strategy", ""),
            "exit_price": sim.get("exit_price", ""),
            "exit_reason": sim.get("exit_reason", "OPEN"),
            "pnl_pips": sim.get("pnl_pips", ""),
            "pnl_pct": sim.get("pnl_pct", ""),
            "pnl_eur": sim.get("pnl_eur", ""),
            "r_multiple": sim.get("r_multiple", ""),
            "duration_minutes": sim.get("duration_minutes", ""),
            "ai_reasoning": (d.get("reasoning", "") or "")[:300].replace("\n", " "),
        }
        results.append(row)

    # Write CSV: always merge with existing data and dedupe by paper_id.
    # Keep the row with the highest-priority exit_reason (terminal > OPEN > NO_DATA).
    # Prevents the bug where every re-check of an OPEN trade appended another row.
    existing_rows = []
    if HISTORY_FILE.exists() and not args.rebuild:
        with open(HISTORY_FILE) as f:
            existing_rows = list(csv.DictReader(f))

    _PRIORITY = {"MANUAL_CLOSE": 4, "TP": 4, "SL": 4, "EXPIRED": 3, "OPEN": 2, "NO_DATA": 1}
    merged: dict[str, dict] = {}
    for r in existing_rows + results:
        pid = r["paper_id"]
        cur = merged.get(pid)
        if cur is None or _PRIORITY.get(r.get("exit_reason"), 0) > _PRIORITY.get(cur.get("exit_reason"), 0):
            merged[pid] = r

    with open(HISTORY_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        for row in sorted(merged.values(), key=lambda r: r.get("decision_timestamp", "")):
            writer.writerow(row)

    # Write open paper trades separately for management
    open_trades = [r for r in merged.values() if r["exit_reason"] == "OPEN"]
    OPEN_PAPER_FILE.write_text(json.dumps(open_trades, indent=2))

    closed = [r for r in results if r["exit_reason"] in ("TP", "SL")]
    print(f"\nProcessed: {len(results)} decisions")
    print(f"  Closed: {len(closed)} (TP:{sum(1 for r in closed if r['exit_reason']=='TP')} / SL:{sum(1 for r in closed if r['exit_reason']=='SL')})")
    print(f"  Still open: {len(open_trades)}")
    print(f"  No data: {sum(1 for r in results if r['exit_reason']=='NO_DATA')}")

    print_summary()


def print_summary():
    if not HISTORY_FILE.exists():
        return
    with open(HISTORY_FILE) as f:
        rows = list(csv.DictReader(f))

    closed = [r for r in rows if r["exit_reason"] in ("TP", "SL")]
    if not closed:
        print("\nNo closed paper trades yet.")
        return

    wins = [r for r in closed if r["exit_reason"] == "TP"]
    losses = [r for r in closed if r["exit_reason"] == "SL"]

    total_eur = sum(float(r.get("pnl_eur", 0) or 0) for r in closed)
    total_r = sum(float(r.get("r_multiple", 0) or 0) for r in closed)

    print(f"\n=== PAPER HISTORY SUMMARY ===")
    print(f"Total paper trades: {len(closed)}")
    print(f"Wins: {len(wins)} ({len(wins)*100/len(closed):.1f}%)")
    print(f"Losses: {len(losses)}")
    print(f"Net P&L: €{total_eur:.2f}")
    print(f"Net R: {total_r:.2f}R")
    print(f"Avg R: {total_r/len(closed):.2f}R per trade")
    print(f"\nBy instrument:")
    by_inst = {}
    for r in closed:
        inst = r["instrument"]
        if inst not in by_inst:
            by_inst[inst] = {"wins": 0, "losses": 0, "pnl": 0.0, "r": 0.0}
        if r["exit_reason"] == "TP":
            by_inst[inst]["wins"] += 1
        else:
            by_inst[inst]["losses"] += 1
        by_inst[inst]["pnl"] += float(r.get("pnl_eur", 0) or 0)
        by_inst[inst]["r"] += float(r.get("r_multiple", 0) or 0)

    for inst, s in sorted(by_inst.items()):
        total = s["wins"] + s["losses"]
        wr = s["wins"] * 100 / total if total else 0
        print(f"  {inst}: {s['wins']}W/{s['losses']}L ({wr:.0f}%) — €{s['pnl']:+.2f}  {s['r']:+.2f}R")


if __name__ == "__main__":
    main()
