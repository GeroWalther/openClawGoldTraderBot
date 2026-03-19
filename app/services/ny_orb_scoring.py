"""NY Opening Range Breakout (ORB) scoring engine.

Watches the first M15 candle after NY open (9:30 ET) to define the range,
then looks for breakout or false-breakout entries on M5 bars.
TP is always 2× SL. SL is placed at the opposite side of the range + buffer.
"""

import logging
from datetime import datetime, timezone, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

# NY open in UTC: 14:30 during EST (Nov-Mar), 13:30 during EDT (Mar-Nov)
# We detect DST dynamically per bar date.

NY_OPEN_ET_HOUR = 9
NY_OPEN_ET_MINUTE = 30

# Only trade breakouts within this window after NY open (minutes)
MAX_ENTRY_WINDOW_MINUTES = 120  # 2 hours after open

# Range size filters (relative to M5 ATR14)
MIN_RANGE_ATR_RATIO = 0.3   # Range too narrow = noise
MAX_RANGE_ATR_RATIO = 3.0   # Range too wide = SL too large

# Breakout confirmation: close must be at least this fraction of range beyond the level
BREAKOUT_THRESHOLD = 0.15  # 15% of range size beyond the boundary

# False breakout: wick beyond range but close inside, then next bar confirms reversal
FALSE_BO_WICK_MIN = 0.3  # Wick must extend at least 30% of range beyond boundary


def _is_dst(dt: datetime) -> bool:
    """Check if a given UTC datetime falls in US Eastern Daylight Time.

    US DST: Second Sunday of March 2:00 AM ET to First Sunday of November 2:00 AM ET.
    """
    year = dt.year

    # Second Sunday of March
    mar1 = datetime(year, 3, 1)
    dst_start_day = 8 + (6 - mar1.weekday()) % 7  # Second Sunday
    dst_start = datetime(year, 3, dst_start_day, 7, 0, tzinfo=timezone.utc)  # 2 AM ET = 7 AM UTC

    # First Sunday of November
    nov1 = datetime(year, 11, 1)
    dst_end_day = 1 + (6 - nov1.weekday()) % 7  # First Sunday
    dst_end = datetime(year, 11, dst_end_day, 6, 0, tzinfo=timezone.utc)  # 2 AM ET = 6 AM UTC (still in EDT)

    return dst_start <= dt.replace(tzinfo=timezone.utc) < dst_end


def _ny_open_utc(dt: datetime) -> tuple[int, int]:
    """Return (hour, minute) in UTC for NY open on a given date."""
    if _is_dst(dt):
        return (13, 30)  # EDT: UTC-4
    else:
        return (14, 30)  # EST: UTC-5


def identify_opening_range(
    m5_df: pd.DataFrame,
    bar_date: datetime,
) -> dict | None:
    """Identify the NY opening range from M5 bars for a given trading day.

    The range = high/low of the 3 M5 bars covering 9:30-9:45 ET.

    Returns dict with keys: range_high, range_low, range_size, range_start_idx, range_end_idx
    or None if the range cannot be identified.
    """
    ny_hour, ny_min = _ny_open_utc(bar_date)

    # Find M5 bars in the opening 15-minute window
    range_bars = []
    range_indices = []

    for i in range(len(m5_df)):
        row = m5_df.iloc[i]
        dt = row.get("date")
        if dt is None:
            dt = m5_df.index[i]
        if not hasattr(dt, "hour"):
            continue

        # Normalize to UTC
        if dt.tzinfo is not None:
            dt_utc = dt.astimezone(timezone.utc)
        else:
            dt_utc = dt.replace(tzinfo=timezone.utc)

        # Check if this bar is on the same calendar date
        bar_day = dt_utc.date() if hasattr(dt_utc, "date") else dt_utc
        target_day = bar_date.date() if hasattr(bar_date, "date") else bar_date
        if bar_day != target_day:
            continue

        # Check if bar falls in the 9:30-9:45 ET window (3 M5 bars)
        bar_minutes = dt_utc.hour * 60 + dt_utc.minute
        range_start_minutes = ny_hour * 60 + ny_min
        range_end_minutes = range_start_minutes + 15  # 15-minute opening range

        if range_start_minutes <= bar_minutes < range_end_minutes:
            range_bars.append(row)
            range_indices.append(i)

    if not range_bars:
        return None

    range_high = max(bar["high"] for bar in range_bars)
    range_low = min(bar["low"] for bar in range_bars)
    range_size = range_high - range_low

    if range_size <= 0:
        return None

    return {
        "range_high": range_high,
        "range_low": range_low,
        "range_size": range_size,
        "range_start_idx": range_indices[0],
        "range_end_idx": range_indices[-1],
    }


class NYORBScoringEngine:
    """NY Opening Range Breakout scoring engine.

    Identifies opening range from first M15 candle after NY open,
    then scores M5 bars for breakout or false-breakout entries.
    """

    def __init__(
        self,
        tp_sl_ratio: float = 2.0,
        min_range_atr: float = MIN_RANGE_ATR_RATIO,
        max_range_atr: float = MAX_RANGE_ATR_RATIO,
        max_entry_window: int = MAX_ENTRY_WINDOW_MINUTES,
    ):
        self.tp_sl_ratio = tp_sl_ratio
        self.min_range_atr = min_range_atr
        self.max_range_atr = max_range_atr
        self.max_entry_window = max_entry_window

    def score_bar(
        self,
        m5_row: pd.Series,
        m5_prev: pd.Series | None,
        opening_range: dict,
        atr: float,
        bar_time: datetime,
    ) -> dict:
        """Score a single M5 bar for ORB entry.

        Returns dict with: signal ("breakout_long", "breakout_short",
        "false_bo_long", "false_bo_short", or None), conviction, sl_dist, tp_dist.
        """
        result = {
            "signal": None,
            "direction": None,
            "conviction": None,
            "sl_dist": 0.0,
            "tp_dist": 0.0,
            "score": 0.0,
        }

        rng = opening_range
        range_size = rng["range_size"]

        # Range quality check
        if atr <= 0:
            return result
        range_atr_ratio = range_size / atr
        if range_atr_ratio < self.min_range_atr or range_atr_ratio > self.max_range_atr:
            return result

        close = m5_row["close"]
        high = m5_row["high"]
        low = m5_row["low"]
        rng_high = rng["range_high"]
        rng_low = rng["range_low"]
        rng_mid = (rng_high + rng_low) / 2

        # Check time window
        ny_hour, ny_min = _ny_open_utc(bar_time)
        range_start_minutes = ny_hour * 60 + ny_min
        if bar_time.tzinfo is not None:
            bt_utc = bar_time.astimezone(timezone.utc)
        else:
            bt_utc = bar_time.replace(tzinfo=timezone.utc)
        bar_minutes = bt_utc.hour * 60 + bt_utc.minute
        minutes_since_open = bar_minutes - range_start_minutes
        if minutes_since_open < 15 or minutes_since_open > self.max_entry_window:
            return result

        threshold = range_size * BREAKOUT_THRESHOLD
        buffer = range_size * 0.1 + atr * 0.1  # SL buffer beyond range

        # --- BREAKOUT LONG ---
        if close > rng_high + threshold:
            sl_dist = (close - rng_low) + buffer  # SL below range low
            tp_dist = sl_dist * self.tp_sl_ratio

            # Conviction based on strength
            breakout_strength = (close - rng_high) / range_size
            if breakout_strength > 0.5:
                conviction = "HIGH"
                score = 8.0
            else:
                conviction = "MEDIUM"
                score = 6.0

            result.update({
                "signal": "breakout_long",
                "direction": "BUY",
                "conviction": conviction,
                "sl_dist": sl_dist,
                "tp_dist": tp_dist,
                "score": score,
            })
            return result

        # --- BREAKOUT SHORT ---
        if close < rng_low - threshold:
            sl_dist = (rng_high - close) + buffer
            tp_dist = sl_dist * self.tp_sl_ratio

            breakout_strength = (rng_low - close) / range_size
            if breakout_strength > 0.5:
                conviction = "HIGH"
                score = 8.0
            else:
                conviction = "MEDIUM"
                score = 6.0

            result.update({
                "signal": "breakout_short",
                "direction": "SELL",
                "conviction": conviction,
                "sl_dist": sl_dist,
                "tp_dist": tp_dist,
                "score": score,
            })
            return result

        # --- FALSE BREAKOUT (fade) ---
        if m5_prev is not None:
            prev_high = m5_prev["high"]
            prev_low = m5_prev["low"]
            prev_close = m5_prev["close"]

            # False breakout HIGH: prev bar wicked above range, current bar closes back inside
            if prev_high > rng_high + range_size * FALSE_BO_WICK_MIN and prev_close > rng_low:
                if close < rng_high and close < prev_close:
                    # Reversal confirmed — go short
                    sl_dist = (rng_high - close) + buffer + (prev_high - rng_high)
                    tp_dist = sl_dist * self.tp_sl_ratio

                    result.update({
                        "signal": "false_bo_short",
                        "direction": "SELL",
                        "conviction": "MEDIUM",
                        "sl_dist": sl_dist,
                        "tp_dist": tp_dist,
                        "score": 5.0,
                    })
                    return result

            # False breakout LOW: prev bar wicked below range, current bar closes back inside
            if prev_low < rng_low - range_size * FALSE_BO_WICK_MIN and prev_close < rng_high:
                if close > rng_low and close > prev_close:
                    sl_dist = (close - rng_low) + buffer + (rng_low - prev_low)
                    tp_dist = sl_dist * self.tp_sl_ratio

                    result.update({
                        "signal": "false_bo_long",
                        "direction": "BUY",
                        "conviction": "MEDIUM",
                        "sl_dist": sl_dist,
                        "tp_dist": tp_dist,
                        "score": 5.0,
                    })
                    return result

        return result
