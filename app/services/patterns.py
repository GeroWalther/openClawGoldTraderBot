"""Chart pattern detection service.

Detects candlestick patterns, multi-bar chart patterns, trend structure,
and trendline quality from OHLCV DataFrames. Pure pandas/numpy — no
external dependencies.
"""

import numpy as np
import pandas as pd


# ── Shared pivot detection ─────────────────────────────────────────────


def _find_swing_pivots(
    highs: np.ndarray,
    lows: np.ndarray,
    pivot_range: int = 2,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Find swing high/low pivots as (index, value) tuples.

    A swing high at index i means highs[i] is the max within ±pivot_range.
    A swing low at index i means lows[i] is the min within ±pivot_range.
    """
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []

    for i in range(pivot_range, len(highs) - pivot_range):
        window_h = highs[i - pivot_range:i + pivot_range + 1]
        if highs[i] == window_h.max():
            swing_highs.append((i, float(highs[i])))
        window_l = lows[i - pivot_range:i + pivot_range + 1]
        if lows[i] == window_l.min():
            swing_lows.append((i, float(lows[i])))

    return swing_highs, swing_lows


def _get_tolerance(df: pd.DataFrame) -> float:
    """Return price tolerance for 'similar level' comparison.

    Uses ATR if available, otherwise 1.5% of mean close.
    """
    atr_col = df.get("atr")
    if atr_col is not None and not pd.isna(atr_col.iloc[-1]) and atr_col.iloc[-1] > 0:
        return float(atr_col.iloc[-1]) * 0.75
    return float(df["close"].mean()) * 0.015


def _detect_candlestick_patterns(df: pd.DataFrame) -> list[dict]:
    """Detect candlestick patterns from the last 3 bars.

    Returns list of detected patterns with name, type (bullish/bearish),
    and strength (1-2).
    """
    if len(df) < 3:
        return []

    patterns = []
    bars = df.iloc[-3:]
    curr = bars.iloc[-1]
    prev = bars.iloc[-2]
    prev2 = bars.iloc[-3]

    o, h, l, c = curr["open"], curr["high"], curr["low"], curr["close"]
    body = abs(c - o)
    full_range = h - l

    if full_range == 0:
        return []

    body_ratio = body / full_range
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l

    po, ph, pl, pc = prev["open"], prev["high"], prev["low"], prev["close"]
    prev_body = abs(pc - po)

    # Doji — tiny body relative to range
    if body_ratio < 0.1 and full_range > 0:
        patterns.append({"name": "doji", "type": "neutral", "strength": 1})

    # Bullish engulfing
    if (pc < po  # prev was bearish
            and c > o  # curr is bullish
            and o <= pc  # curr open <= prev close
            and c >= po):  # curr close >= prev open
        patterns.append({"name": "bullish_engulfing", "type": "bullish", "strength": 2})

    # Bearish engulfing
    if (pc > po  # prev was bullish
            and c < o  # curr is bearish
            and o >= pc  # curr open >= prev close
            and c <= po):  # curr close <= prev open
        patterns.append({"name": "bearish_engulfing", "type": "bearish", "strength": 2})

    # Hammer (bullish) — small body at top, long lower shadow
    if (body_ratio < 0.35
            and lower_shadow > body * 2
            and upper_shadow < body * 0.5):
        patterns.append({"name": "hammer", "type": "bullish", "strength": 1})

    # Shooting star (bearish) — small body at bottom, long upper shadow
    if (body_ratio < 0.35
            and upper_shadow > body * 2
            and lower_shadow < body * 0.5):
        patterns.append({"name": "shooting_star", "type": "bearish", "strength": 1})

    # Inverted hammer — small body at bottom, long upper shadow (after downtrend)
    if (body_ratio < 0.35
            and upper_shadow > body * 2
            and lower_shadow < body * 0.5
            and pc < po):  # previous bar was bearish
        patterns.append({"name": "inverted_hammer", "type": "bullish", "strength": 1})

    # Morning star (3-bar bullish reversal)
    prev2_body = abs(prev2["close"] - prev2["open"])
    if (prev2["close"] < prev2["open"]  # first bar bearish
            and prev_body < prev2_body * 0.3  # middle bar small
            and c > o  # third bar bullish
            and c > (prev2["open"] + prev2["close"]) / 2):  # closes above midpoint
        patterns.append({"name": "morning_star", "type": "bullish", "strength": 2})

    # Evening star (3-bar bearish reversal)
    if (prev2["close"] > prev2["open"]  # first bar bullish
            and prev_body < prev2_body * 0.3  # middle bar small
            and c < o  # third bar bearish
            and c < (prev2["open"] + prev2["close"]) / 2):  # closes below midpoint
        patterns.append({"name": "evening_star", "type": "bearish", "strength": 2})

    return patterns


def _detect_trend_structure(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Analyze trend structure using pivot-based HH/HL or LH/LL counting.

    Returns dict with trend (uptrend/downtrend/ranging) and strength (0-2).
    """
    if len(df) < lookback:
        return {"trend": "ranging", "strength": 0}

    window = df.iloc[-lookback:]
    sh, sl = _find_swing_pivots(window["high"].values, window["low"].values)

    if len(sh) < 2 or len(sl) < 2:
        return {"trend": "ranging", "strength": 0}

    swing_highs = [v for _, v in sh]
    swing_lows = [v for _, v in sl]

    # Count higher highs / lower lows
    hh_count = sum(1 for i in range(1, len(swing_highs)) if swing_highs[i] > swing_highs[i - 1])
    lh_count = sum(1 for i in range(1, len(swing_highs)) if swing_highs[i] < swing_highs[i - 1])
    hl_count = sum(1 for i in range(1, len(swing_lows)) if swing_lows[i] > swing_lows[i - 1])
    ll_count = sum(1 for i in range(1, len(swing_lows)) if swing_lows[i] < swing_lows[i - 1])

    bullish_points = hh_count + hl_count
    bearish_points = lh_count + ll_count
    total = bullish_points + bearish_points

    if total == 0:
        return {"trend": "ranging", "strength": 0}

    ratio = (bullish_points - bearish_points) / total

    if ratio > 0.5:
        strength = 2 if ratio > 0.75 else 1
        return {"trend": "uptrend", "strength": strength}
    elif ratio < -0.5:
        strength = 2 if ratio < -0.75 else 1
        return {"trend": "downtrend", "strength": strength}

    return {"trend": "ranging", "strength": 0}


def _detect_chart_patterns(df: pd.DataFrame, lookback: int = 60) -> list[dict]:
    """Detect multi-bar chart patterns from swing pivots.

    Looks for: double top/bottom, head & shoulders (+ inverse),
    triple top/bottom, bull/bear flags, rising/falling wedges.

    Returns list of detected patterns with name, type, and strength (1-2).
    """
    n = min(lookback, len(df))
    if n < 20:
        return []

    window = df.iloc[-n:]
    tolerance = _get_tolerance(df)
    current_close = float(window["close"].iloc[-1])

    sh, sl = _find_swing_pivots(window["high"].values, window["low"].values)
    patterns: list[dict] = []

    # ── Double Top ──────────────────────────────────────────────────
    # Two swing highs at similar level; confirmed if price below valley, forming otherwise
    if len(sh) >= 2:
        h1_idx, h1_val = sh[-2]
        h2_idx, h2_val = sh[-1]
        if abs(h1_val - h2_val) < tolerance and h2_idx > h1_idx:
            valley = float(window["low"].iloc[h1_idx:h2_idx + 1].min())
            confirmed = current_close < valley
            patterns.append({
                "name": "double_top", "type": "bearish",
                "strength": 2 if confirmed else 1,
                "status": "confirmed" if confirmed else "forming",
                "levels": {"peaks": round((h1_val + h2_val) / 2, 2), "neckline": round(valley, 2)},
            })

    # ── Double Bottom ───────────────────────────────────────────────
    if len(sl) >= 2:
        l1_idx, l1_val = sl[-2]
        l2_idx, l2_val = sl[-1]
        if abs(l1_val - l2_val) < tolerance and l2_idx > l1_idx:
            peak = float(window["high"].iloc[l1_idx:l2_idx + 1].max())
            confirmed = current_close > peak
            patterns.append({
                "name": "double_bottom", "type": "bullish",
                "strength": 2 if confirmed else 1,
                "status": "confirmed" if confirmed else "forming",
                "levels": {"troughs": round((l1_val + l2_val) / 2, 2), "neckline": round(peak, 2)},
            })

    # ── Triple Top ──────────────────────────────────────────────────
    if len(sh) >= 3:
        h1_idx, h1_val = sh[-3]
        h2_idx, h2_val = sh[-2]
        h3_idx, h3_val = sh[-1]
        if (abs(h1_val - h2_val) < tolerance
                and abs(h2_val - h3_val) < tolerance
                and abs(h1_val - h3_val) < tolerance):
            valley = float(window["low"].iloc[h1_idx:h3_idx + 1].min())
            confirmed = current_close < valley
            patterns.append({
                "name": "triple_top", "type": "bearish",
                "strength": 2 if confirmed else 1,
                "status": "confirmed" if confirmed else "forming",
                "levels": {"peaks": round((h1_val + h2_val + h3_val) / 3, 2), "neckline": round(valley, 2)},
            })

    # ── Triple Bottom ───────────────────────────────────────────────
    if len(sl) >= 3:
        l1_idx, l1_val = sl[-3]
        l2_idx, l2_val = sl[-2]
        l3_idx, l3_val = sl[-1]
        if (abs(l1_val - l2_val) < tolerance
                and abs(l2_val - l3_val) < tolerance
                and abs(l1_val - l3_val) < tolerance):
            peak = float(window["high"].iloc[l1_idx:l3_idx + 1].max())
            confirmed = current_close > peak
            patterns.append({
                "name": "triple_bottom", "type": "bullish",
                "strength": 2 if confirmed else 1,
                "status": "confirmed" if confirmed else "forming",
                "levels": {"troughs": round((l1_val + l2_val + l3_val) / 3, 2), "neckline": round(peak, 2)},
            })

    # ── Head & Shoulders ────────────────────────────────────────────
    # Three swing highs: middle is highest, outer two at similar levels
    if len(sh) >= 3:
        h1_idx, h1_val = sh[-3]
        h2_idx, h2_val = sh[-2]
        h3_idx, h3_val = sh[-1]
        if (h2_val > h1_val and h2_val > h3_val
                and abs(h1_val - h3_val) < tolerance):
            valley1 = float(window["low"].iloc[h1_idx:h2_idx + 1].min())
            valley2 = float(window["low"].iloc[h2_idx:h3_idx + 1].min())
            neckline = (valley1 + valley2) / 2
            confirmed = current_close < neckline
            patterns.append({
                "name": "head_and_shoulders", "type": "bearish",
                "strength": 2 if confirmed else 1,
                "status": "confirmed" if confirmed else "forming",
                "levels": {"head": round(h2_val, 2), "shoulders": round((h1_val + h3_val) / 2, 2),
                           "neckline": round(neckline, 2)},
            })

    # ── Inverse Head & Shoulders ────────────────────────────────────
    if len(sl) >= 3:
        l1_idx, l1_val = sl[-3]
        l2_idx, l2_val = sl[-2]
        l3_idx, l3_val = sl[-1]
        if (l2_val < l1_val and l2_val < l3_val
                and abs(l1_val - l3_val) < tolerance):
            peak1 = float(window["high"].iloc[l1_idx:l2_idx + 1].max())
            peak2 = float(window["high"].iloc[l2_idx:l3_idx + 1].max())
            neckline = (peak1 + peak2) / 2
            confirmed = current_close > neckline
            patterns.append({
                "name": "inverse_head_and_shoulders", "type": "bullish",
                "strength": 2 if confirmed else 1,
                "status": "confirmed" if confirmed else "forming",
                "levels": {"head": round(l2_val, 2), "shoulders": round((l1_val + l3_val) / 2, 2),
                           "neckline": round(neckline, 2)},
            })

    # ── Bull Flag ───────────────────────────────────────────────────
    # Strong upward pole (first 40%), followed by mild pullback/consolidation (last 60%)
    pole_end = n * 2 // 5
    flag_start = pole_end
    if pole_end >= 5 and n - flag_start >= 5:
        pole_data = window.iloc[:pole_end]
        flag_data = window.iloc[flag_start:]
        pole_move = float(pole_data["close"].iloc[-1] - pole_data["close"].iloc[0])
        flag_range = float(flag_data["high"].max() - flag_data["low"].min())
        pole_range = float(pole_data["high"].max() - pole_data["low"].min())

        if pole_move > 0 and pole_range > 0:
            # Flag should be narrow relative to pole, and slope gently down or sideways
            flag_slope = float(flag_data["close"].iloc[-1] - flag_data["close"].iloc[0])
            if flag_range < pole_range * 0.5 and flag_slope <= 0 and flag_slope > -pole_move * 0.5:
                patterns.append({
                    "name": "bull_flag", "type": "bullish", "strength": 1,
                })

    # ── Bear Flag ───────────────────────────────────────────────────
    if pole_end >= 5 and n - flag_start >= 5:
        pole_data = window.iloc[:pole_end]
        flag_data = window.iloc[flag_start:]
        pole_move = float(pole_data["close"].iloc[-1] - pole_data["close"].iloc[0])
        flag_range = float(flag_data["high"].max() - flag_data["low"].min())
        pole_range = float(pole_data["high"].max() - pole_data["low"].min())

        if pole_move < 0 and pole_range > 0:
            flag_slope = float(flag_data["close"].iloc[-1] - flag_data["close"].iloc[0])
            if flag_range < pole_range * 0.5 and flag_slope >= 0 and flag_slope < abs(pole_move) * 0.5:
                patterns.append({
                    "name": "bear_flag", "type": "bearish", "strength": 1,
                })

    # ── Rising Wedge (bearish) ──────────────────────────────────────
    # Both highs and lows trending up, but highs rising slower → converging
    if len(sh) >= 3 and len(sl) >= 3:
        sh_vals = [v for _, v in sh[-4:]]
        sl_vals = [v for _, v in sl[-4:]]
        if len(sh_vals) >= 2 and len(sl_vals) >= 2:
            high_slope = (sh_vals[-1] - sh_vals[0]) / max(len(sh_vals) - 1, 1)
            low_slope = (sl_vals[-1] - sl_vals[0]) / max(len(sl_vals) - 1, 1)
            # Both rising, but lows rising faster than highs → converging upward
            if high_slope > 0 and low_slope > 0 and low_slope > high_slope * 0.5:
                spread_start = sh_vals[0] - sl_vals[0]
                spread_end = sh_vals[-1] - sl_vals[-1]
                if spread_start > 0 and spread_end < spread_start * 0.75:
                    patterns.append({
                        "name": "rising_wedge", "type": "bearish", "strength": 1,
                    })

    # ── Falling Wedge (bullish) ─────────────────────────────────────
    if len(sh) >= 3 and len(sl) >= 3:
        sh_vals = [v for _, v in sh[-4:]]
        sl_vals = [v for _, v in sl[-4:]]
        if len(sh_vals) >= 2 and len(sl_vals) >= 2:
            high_slope = (sh_vals[-1] - sh_vals[0]) / max(len(sh_vals) - 1, 1)
            low_slope = (sl_vals[-1] - sl_vals[0]) / max(len(sl_vals) - 1, 1)
            # Both falling, but highs falling faster than lows → converging downward
            if high_slope < 0 and low_slope < 0 and high_slope < low_slope * 0.5:
                spread_start = sh_vals[0] - sl_vals[0]
                spread_end = sh_vals[-1] - sl_vals[-1]
                if spread_start > 0 and spread_end < spread_start * 0.75:
                    patterns.append({
                        "name": "falling_wedge", "type": "bullish", "strength": 1,
                    })

    return patterns


def _detect_trendline(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Compute linear regression trendline over closing prices.

    Returns slope direction, normalized distance from line, and R-squared.
    """
    if len(df) < lookback:
        return {"direction": "flat", "distance": 0.0, "r_squared": 0.0}

    closes = df["close"].iloc[-lookback:].values
    x = np.arange(lookback)

    try:
        coeffs = np.polyfit(x, closes, 1)
    except (np.linalg.LinAlgError, ValueError):
        return {"direction": "flat", "distance": 0.0, "r_squared": 0.0}

    slope, intercept = coeffs
    fitted = np.polyval(coeffs, x)

    # R-squared
    ss_res = np.sum((closes - fitted) ** 2)
    ss_tot = np.sum((closes - np.mean(closes)) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Normalize slope by ATR
    atr_col = df.get("atr")
    if atr_col is not None and not pd.isna(atr_col.iloc[-1]) and atr_col.iloc[-1] > 0:
        atr = float(atr_col.iloc[-1])
    else:
        atr = np.std(closes) if np.std(closes) > 0 else 1.0

    normalized_slope = slope / atr if atr > 0 else 0.0

    # Distance of current price from trendline (normalized by ATR)
    distance = (closes[-1] - fitted[-1]) / atr if atr > 0 else 0.0

    if normalized_slope > 0.1:
        direction = "up"
    elif normalized_slope < -0.1:
        direction = "down"
    else:
        direction = "flat"

    return {
        "direction": direction,
        "distance": round(float(distance), 3),
        "r_squared": round(float(max(0, min(1, r_squared))), 3),
    }


def detect_patterns(df: pd.DataFrame) -> dict:
    """Run all pattern detection on a D1 DataFrame.

    Returns dict with candlestick patterns, chart patterns, trend structure,
    trendline info, and a combined enhanced_chart_score (-2..+2).
    """
    candlesticks = _detect_candlestick_patterns(df)
    chart_patterns = _detect_chart_patterns(df)
    trend = _detect_trend_structure(df)
    trendline = _detect_trendline(df)

    # Combine into enhanced chart score
    score = 0.0

    # Candlestick contribution (strongest pattern, max ±0.5)
    bullish_cs = max((p["strength"] for p in candlesticks if p["type"] == "bullish"), default=0)
    bearish_cs = max((p["strength"] for p in candlesticks if p["type"] == "bearish"), default=0)
    score += max(-0.5, min(0.5, (bullish_cs - bearish_cs) * 0.25))

    # Chart pattern contribution (strongest pattern, max ±1.0)
    bullish_cp = max((p["strength"] for p in chart_patterns if p["type"] == "bullish"), default=0)
    bearish_cp = max((p["strength"] for p in chart_patterns if p["type"] == "bearish"), default=0)
    score += max(-1.0, min(1.0, (bullish_cp - bearish_cp) * 0.5))

    # Trend structure contribution (max ±0.25)
    if trend["trend"] == "uptrend":
        score += 0.125 * trend["strength"]
    elif trend["trend"] == "downtrend":
        score -= 0.125 * trend["strength"]

    # Trendline contribution (max ±0.25, weighted by R²)
    if trendline["direction"] == "up":
        score += 0.25 * trendline["r_squared"]
    elif trendline["direction"] == "down":
        score -= 0.25 * trendline["r_squared"]

    enhanced_chart_score = round(max(-2.0, min(2.0, score)), 2)

    return {
        "candlestick_patterns": candlesticks,
        "chart_patterns": chart_patterns,
        "trend_structure": trend,
        "trendline": trendline,
        "enhanced_chart_score": enhanced_chart_score,
    }
