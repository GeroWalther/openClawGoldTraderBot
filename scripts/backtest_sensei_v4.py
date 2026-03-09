"""Sensei V4 — Quality-focused backtesting.

Goal: Find reliable, high-win-rate setups.
Filters: win rate >= 50%, PF >= 1.3, DD <= 30%

Strategy: Sensei with double bottom (W) + double top (M) for shorts.
Add quality filters:
- Trend confirmation: price above SMA100/200 for longs, below for shorts
- Volume confirmation (where available)
- RSI filter: not overbought for longs, not oversold for shorts
- Better R:R with tighter SL (0.8-1.0 ATR) and wider TP (2-3 ATR)
- Conservative risk: 2-3% per trade
"""

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")


def compute_atr(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def sensei_quality(
    df,
    conv_thresh=10,
    conv_bars=10,
    db_tol=6,
    db_lb=3,
    db_min=5,
    db_max=80,
    require_trend=True,
    rsi_filter=True,
    rsi_max_long=70,      # Don't buy if RSI above this
    rsi_min_short=30,     # Don't short if RSI below this
    sma_trend_period=100, # Trend filter SMA
):
    df = df.copy()
    low, high = df["Low"].values, df["High"].values

    df["sma5"] = df["Close"].rolling(5).mean()
    df["sma10"] = df["Close"].rolling(10).mean()
    df["sma20"] = df["Close"].rolling(20).mean()
    df["sma50"] = df["Close"].rolling(50).mean()
    df["sma_trend"] = df["Close"].rolling(sma_trend_period).mean()
    df["atr"] = compute_atr(df, 14)
    df["rsi"] = compute_rsi(df["Close"], 14)

    # Consolidation
    ma_df = df[["sma5", "sma10", "sma20", "sma50"]]
    spread = (ma_df.max(axis=1) - ma_df.min(axis=1)) / df["Close"] * 100
    df["is_converged"] = spread <= conv_thresh

    conv_count = np.zeros(len(df))
    for i in range(1, len(df)):
        conv_count[i] = conv_count[i-1] + 1 if df["is_converged"].iloc[i] else 0
    df["conv_count"] = conv_count
    df["is_consolidating"] = conv_count >= conv_bars

    # Pivot lows & highs
    df["pivot_low"] = np.nan
    df["pivot_high"] = np.nan
    for i in range(db_lb, len(df) - db_lb):
        if all(low[i] <= low[i-j] and low[i] <= low[i+j] for j in range(1, db_lb+1)):
            df.iloc[i, df.columns.get_loc("pivot_low")] = low[i]
        if all(high[i] >= high[i-j] and high[i] >= high[i+j] for j in range(1, db_lb+1)):
            df.iloc[i, df.columns.get_loc("pivot_high")] = high[i]

    # Double bottom (W) — right bottom higher
    w_active = np.zeros(len(df), dtype=bool)
    bot1_p, bot1_i = np.nan, 0
    for i in range(len(df)):
        if not np.isnan(df["pivot_low"].iloc[i]):
            p = df["pivot_low"].iloc[i]
            if not np.isnan(bot1_p):
                d = i - bot1_i
                pct = abs(p - bot1_p) / bot1_p * 100
                consol = conv_count[i] >= conv_bars * 0.4
                if pct <= db_tol and db_min <= d <= db_max and p > bot1_p and consol:
                    for j in range(i, min(i + db_max, len(df))):
                        w_active[j] = True
            bot1_p, bot1_i = p, i

    # Double top (M) — right top lower
    m_active = np.zeros(len(df), dtype=bool)
    top1_p, top1_i = np.nan, 0
    for i in range(len(df)):
        if not np.isnan(df["pivot_high"].iloc[i]):
            p = df["pivot_high"].iloc[i]
            if not np.isnan(top1_p):
                d = i - top1_i
                pct = abs(p - top1_p) / top1_p * 100
                consol = conv_count[i] >= conv_bars * 0.4
                if pct <= db_tol and db_min <= d <= db_max and p < top1_p and consol:
                    for j in range(i, min(i + db_max, len(df))):
                        m_active[j] = True
            top1_p, top1_i = p, i

    # SMA20 cross
    cross_above = (df["Close"] > df["sma20"]) & (df["Close"].shift(1) <= df["sma20"].shift(1))
    cross_below = (df["Close"] < df["sma20"]) & (df["Close"].shift(1) >= df["sma20"].shift(1))

    df["signal_long"] = False
    df["signal_short"] = False

    for i in range(len(df)):
        consol = df["is_consolidating"].iloc[i] or (i > 0 and df["is_consolidating"].iloc[i-1])
        if not consol:
            continue

        rsi_val = df["rsi"].iloc[i]
        price = df["Close"].iloc[i]
        sma_t = df["sma_trend"].iloc[i]

        # LONG: W pattern + cross above SMA20
        if w_active[i] and cross_above.iloc[i]:
            ok = True
            if require_trend and not np.isnan(sma_t) and price < sma_t:
                ok = False  # Price below trend — skip long
            if rsi_filter and not np.isnan(rsi_val) and rsi_val > rsi_max_long:
                ok = False  # Overbought — skip
            if ok:
                df.iloc[i, df.columns.get_loc("signal_long")] = True

        # SHORT: M pattern + cross below SMA20
        if m_active[i] and cross_below.iloc[i]:
            ok = True
            if require_trend and not np.isnan(sma_t) and price > sma_t:
                ok = False  # Price above trend — skip short
            if rsi_filter and not np.isnan(rsi_val) and rsi_val < rsi_min_short:
                ok = False  # Oversold — skip
            if ok:
                df.iloc[i, df.columns.get_loc("signal_short")] = True

    return df


def backtest(df, risk_pct=3.0, sl_mult=1.0, tp_mult=2.0, initial=10000.0,
             trailing=False, trail_activate_r=1.0, trail_distance_r=0.5):
    balance = initial
    peak = initial
    max_dd = 0
    trades = []
    in_trade = False
    equity_curve = []

    for i in range(len(df)):
        equity_curve.append(balance)

        if in_trade:
            if trade_dir == "LONG":
                cur_pnl_r = (df["High"].iloc[i] - entry) / sl_dist  # Best R this bar

                if df["Low"].iloc[i] <= sl:
                    pnl = (sl - entry) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "LONG",
                                   "exit": "sl" if pnl < 0 else "trail_sl"})
                    in_trade = False
                elif df["High"].iloc[i] >= tp:
                    pnl = (tp - entry) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "LONG", "exit": "tp"})
                    in_trade = False
                elif trailing and cur_pnl_r >= trail_activate_r:
                    # Move SL to entry + (current_high - entry) - trail_distance
                    new_sl = df["High"].iloc[i] - trail_distance_r * sl_dist
                    if new_sl > sl:
                        sl = new_sl
            else:
                cur_pnl_r = (entry - df["Low"].iloc[i]) / sl_dist

                if df["High"].iloc[i] >= sl:
                    pnl = (entry - sl) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "SHORT",
                                   "exit": "sl" if pnl < 0 else "trail_sl"})
                    in_trade = False
                elif df["Low"].iloc[i] <= tp:
                    pnl = (entry - tp) * size
                    balance += pnl
                    trades.append({"pnl": pnl, "bars": i - ebar, "dir": "SHORT", "exit": "tp"})
                    in_trade = False
                elif trailing and cur_pnl_r >= trail_activate_r:
                    new_sl = df["Low"].iloc[i] + trail_distance_r * sl_dist
                    if new_sl < sl:
                        sl = new_sl

            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        if not in_trade:
            atr = df["atr"].iloc[i]
            if np.isnan(atr) or atr <= 0:
                continue

            sig = None
            if df["signal_long"].iloc[i]:
                sig = "LONG"
            elif df["signal_short"].iloc[i]:
                sig = "SHORT"

            if sig:
                entry = df["Close"].iloc[i]
                ebar = i
                trade_dir = sig
                sl_dist = sl_mult * atr

                if sig == "LONG":
                    sl = entry - sl_dist
                    tp = entry + tp_mult * atr
                else:
                    sl = entry + sl_dist
                    tp = entry - tp_mult * atr

                risk_amt = balance * risk_pct / 100
                if sl_dist > 0:
                    size = risk_amt / sl_dist
                else:
                    continue
                in_trade = True

    if not trades:
        return None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    w_pnl = sum(t["pnl"] for t in wins) if wins else 0
    l_pnl = abs(sum(t["pnl"] for t in losses)) if losses else 0
    longs = sum(1 for t in trades if t["dir"] == "LONG")
    shorts = sum(1 for t in trades if t["dir"] == "SHORT")

    # Consecutive losses
    max_consec_loss = 0
    consec = 0
    for t in trades:
        if t["pnl"] <= 0:
            consec += 1
            max_consec_loss = max(max_consec_loss, consec)
        else:
            consec = 0

    return {
        "trades": len(trades),
        "wins": len(wins),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "pf": round(w_pnl / l_pnl, 2) if l_pnl > 0 else 999.99,
        "ret": round((balance - initial) / initial * 100, 1),
        "dd": round(max_dd, 1),
        "avg_bars": round(np.mean([t["bars"] for t in trades]), 1),
        "longs": longs,
        "shorts": shorts,
        "max_consec_loss": max_consec_loss,
        "avg_win": round(np.mean([t["pnl"] for t in wins]), 2) if wins else 0,
        "avg_loss": round(np.mean([t["pnl"] for t in losses]), 2) if losses else 0,
    }


def run():
    symbols = {
        "EURUSD=X": "EURUSD",
        "AUDUSD=X": "AUDUSD",
        "GBPUSD=X": "GBPUSD",
        "USDJPY=X": "USDJPY",
        "GC=F": "Gold",
        "SPY": "SPY",
        "QQQ": "QQQ",
        "NVDA": "NVDA",
        "MSFT": "MSFT",
        "AAPL": "AAPL",
        "TSLA": "TSLA",
        "BTC-USD": "BTC",
    }

    timeframes = {
        "5m": ("60d", 168),   # period, bars_per_day estimate
        "15m": ("60d", 56),
        "1h": ("2y", 24),
        "1d": ("5y", 1),
    }

    print("Downloading data...")
    cache = {}
    for sym in symbols:
        for tf, (period, _) in timeframes.items():
            try:
                d = yf.download(sym, period=period, interval=tf, progress=False)
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                if len(d) > 100:
                    cache[(sym, tf)] = d
            except Exception:
                pass
    print(f"Downloaded {len(cache)} datasets\n")

    # Configs: focus on quality
    configs = [
        # (conv_thresh, conv_bars, db_tol, db_lb, require_trend, rsi_filter, sl, tp, risk, trailing, label)
        # --- Conservative quality ---
        (8,  10, 5, 3, True,  True,  1.0, 2.0, 3, False, "Quality strict"),
        (10, 8,  6, 3, True,  True,  1.0, 2.0, 3, False, "Quality medium"),
        (12, 6,  8, 3, True,  True,  1.0, 2.5, 3, False, "Quality relaxed"),
        (10, 8,  6, 3, True,  True,  0.8, 2.0, 3, False, "Quality tight SL"),
        (10, 8,  6, 3, True,  True,  1.0, 3.0, 3, False, "Quality wide TP"),

        # --- With trailing stop ---
        (10, 8,  6, 3, True,  True,  1.0, 3.0, 3, True,  "Quality trailing"),
        (12, 6,  8, 3, True,  True,  1.0, 4.0, 3, True,  "Relaxed trailing"),
        (8,  10, 5, 3, True,  True,  0.8, 3.0, 3, True,  "Tight SL trailing"),

        # --- No trend filter (more trades) ---
        (10, 8,  6, 3, False, True,  1.0, 2.0, 3, False, "No trend filter"),
        (12, 6,  8, 3, False, True,  1.0, 2.5, 3, False, "No trend relaxed"),
        (10, 8,  6, 3, False, True,  1.0, 3.0, 3, True,  "No trend trailing"),

        # --- No RSI filter ---
        (10, 8,  6, 3, True,  False, 1.0, 2.0, 3, False, "No RSI filter"),
        (10, 8,  6, 3, False, False, 1.0, 2.0, 3, False, "No filters"),

        # --- Higher risk ---
        (10, 8,  6, 3, True,  True,  1.0, 2.0, 5, False, "Quality 5% risk"),
        (12, 6,  8, 3, True,  True,  1.0, 2.5, 5, False, "Relaxed 5% risk"),
        (10, 8,  6, 3, True,  True,  1.0, 3.0, 5, True,  "Trailing 5% risk"),

        # --- Very short consolidation for M5/M15 ---
        (15, 4,  8, 2, True,  True,  1.0, 2.0, 3, False, "Ultra short consol"),
        (20, 3, 10, 2, False, True,  1.0, 2.5, 3, False, "Micro consol"),
        (15, 4,  8, 2, True,  True,  1.0, 3.0, 3, True,  "Ultra short trail"),

        # --- Longer consolidation for quality ---
        (6,  15, 4, 4, True,  True,  1.0, 2.0, 3, False, "Long consol strict"),
        (8,  12, 5, 3, True,  True,  1.0, 2.5, 3, True,  "Long consol trail"),
    ]

    results = []
    for conv_t, conv_b, db_t, db_l, trend, rsi, sl_m, tp_m, risk, trail, label in configs:
        for sym, name in symbols.items():
            for tf, (_, bpd) in timeframes.items():
                key = (sym, tf)
                if key not in cache:
                    continue
                df = cache[key]

                try:
                    df_sig = sensei_quality(
                        df, conv_thresh=conv_t, conv_bars=conv_b,
                        db_tol=db_t, db_lb=db_l,
                        require_trend=trend, rsi_filter=rsi,
                    )
                    r = backtest(
                        df_sig, risk_pct=risk, sl_mult=sl_m, tp_mult=tp_m,
                        trailing=trail, trail_activate_r=1.0, trail_distance_r=0.5,
                    )
                    if r and r["trades"] >= 5:
                        days = len(df) / max(bpd, 1)
                        months = max(days / 21, 0.5)
                        monthly = r["ret"] / months
                        trades_per_mo = r["trades"] / months

                        results.append({
                            "sym": sym, "name": name, "tf": tf.upper(),
                            "label": label, "monthly": round(monthly, 1),
                            "tpm": round(trades_per_mo, 1),
                            **r,
                        })
                except Exception:
                    pass

    if not results:
        print("No results")
        return

    # ── QUALITY FILTER: WR >= 50%, PF >= 1.3, DD <= 30% ──
    quality = [r for r in results if r["wr"] >= 50 and r["pf"] >= 1.3 and r["dd"] <= 30]
    quality.sort(key=lambda x: x["monthly"], reverse=True)

    print("=" * 120)
    print("QUALITY RESULTS (Win Rate >= 50%, Profit Factor >= 1.3, Max DD <= 30%)")
    print("=" * 120)
    print(f"{'Strategy':22s} | {'TF':>3s} | {'Asset':>6s} | {'Tr':>4s} | {'Tr/mo':>5s} | {'WR%':>4s} | {'PF':>5s} | {'Ret%':>7s} | {'DD%':>4s} | {'$/mo':>7s} | {'MCL':>3s} | {'L/S':>5s}")
    print("-" * 120)
    for r in quality[:40]:
        ls = f"{r['longs']}/{r['shorts']}"
        print(
            f"{r['label']:22s} | {r['tf']:>3s} | {r['name']:>6s} | {r['trades']:4d} | {r['tpm']:5.1f} | "
            f"{r['wr']:4.0f} | {r['pf']:5.2f} | {r['ret']:6.1f}% | {r['dd']:3.0f}% | "
            f"{r['monthly']:+6.1f}% | {r['max_consec_loss']:3d} | {ls:>5s}"
        )

    # ── GOOD RESULTS: WR >= 45%, PF >= 1.2, DD <= 35% ──
    good = [r for r in results if r["wr"] >= 45 and r["pf"] >= 1.2 and r["dd"] <= 35]
    good.sort(key=lambda x: x["monthly"], reverse=True)

    print(f"\n{'=' * 120}")
    print("GOOD RESULTS (Win Rate >= 45%, Profit Factor >= 1.2, Max DD <= 35%)")
    print("=" * 120)
    print(f"{'Strategy':22s} | {'TF':>3s} | {'Asset':>6s} | {'Tr':>4s} | {'Tr/mo':>5s} | {'WR%':>4s} | {'PF':>5s} | {'Ret%':>7s} | {'DD%':>4s} | {'$/mo':>7s} | {'MCL':>3s} | {'L/S':>5s}")
    print("-" * 120)
    for r in good[:40]:
        ls = f"{r['longs']}/{r['shorts']}"
        print(
            f"{r['label']:22s} | {r['tf']:>3s} | {r['name']:>6s} | {r['trades']:4d} | {r['tpm']:5.1f} | "
            f"{r['wr']:4.0f} | {r['pf']:5.2f} | {r['ret']:6.1f}% | {r['dd']:3.0f}% | "
            f"{r['monthly']:+6.1f}% | {r['max_consec_loss']:3d} | {ls:>5s}"
        )

    # ── BEST PER ASSET ──
    print(f"\n{'=' * 120}")
    print("BEST CONFIG PER ASSET (from quality results)")
    print("=" * 120)
    seen = set()
    for r in quality:
        if r["name"] not in seen:
            seen.add(r["name"])
            print(
                f"  {r['name']:>6s} {r['tf']:>3s} | {r['label']:22s} | "
                f"{r['trades']} trades ({r['tpm']:.0f}/mo) | WR {r['wr']:.0f}% | PF {r['pf']:.2f} | "
                f"Return {r['ret']:+.1f}% | DD {r['dd']:.0f}% | ~{r['monthly']:+.1f}%/mo | "
                f"Max {r['max_consec_loss']} consec losses"
            )

    # ── REALISTIC MONTHLY PROJECTION ──
    print(f"\n{'=' * 120}")
    print("REALISTIC PROJECTION (quality + good, sorted by monthly)")
    print("=" * 120)
    combined = sorted(quality + [r for r in good if r not in quality],
                       key=lambda x: x["monthly"], reverse=True)
    for r in combined[:15]:
        status = "★" if r["wr"] >= 50 and r["pf"] >= 1.3 else "○"
        ls = f"{r['longs']}L/{r['shorts']}S"
        print(
            f"  {status} {r['name']:>6s} {r['tf']:>3s} | {r['label']:22s} | "
            f"~{r['monthly']:+.0f}%/mo | {r['tpm']:.0f} trades/mo | "
            f"WR {r['wr']:.0f}% | PF {r['pf']:.2f} | DD {r['dd']:.0f}% | {ls}"
        )


if __name__ == "__main__":
    run()
