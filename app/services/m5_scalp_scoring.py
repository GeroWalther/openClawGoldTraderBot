"""4-factor M5 scalp scoring engine.

Focused on very short timeframes (M5) with H1 trend confirmation.
Designed for quick scalp trades running every 30 minutes.
"""

import logging
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger(__name__)

M5_FACTOR_WEIGHTS = {
    "h1_trend_gate": 2.5,      # SMA20/50 alignment on H1 — directional filter
    "m5_ema_cross": 2.0,       # EMA9/EMA21 cross on M5
    "m5_momentum": 1.5,        # RSI(7) on M5
    "m5_bb_position": 1.0,     # BB squeeze + price position within bands on M5
    "session_quality": 0.0,    # Non-directional gate — applied as score multiplier
}

M5_MAX_SCORE = sum(2 * w for w in M5_FACTOR_WEIGHTS.values())  # 14

M5_SIGNAL_THRESHOLD = 6
M5_HIGH_CONVICTION_THRESHOLD = 9


class M5ScalpScoringEngine:
    """4-factor scoring engine for M5 scalp trades."""

    def score(
        self,
        h1_row: pd.Series,
        m5_df: pd.DataFrame,
        trading_sessions: tuple = (),
        bar_time: datetime | None = None,
    ) -> dict:
        """Score using H1 trend gate and M5 entry signals.

        Args:
            h1_row: Latest H1 bar with computed indicators (sma20, sma50).
            m5_df: Last ~6 M5 bars with scalp indicators (ema9, ema21, rsi7, bb_*).
            trading_sessions: Instrument's trading sessions for session quality.
            bar_time: Optional bar timestamp for backtesting (uses current time if None).

        Returns:
            dict with total_score, conviction, direction, and per-factor scores.
        """
        factors = {}

        m5_row = m5_df.iloc[-1] if not m5_df.empty else None

        factors["h1_trend_gate"] = self._score_h1_trend_gate(h1_row)
        factors["m5_ema_cross"] = self._score_m5_ema_cross(m5_df)
        factors["m5_momentum"] = self._score_m5_momentum(m5_row)
        factors["m5_bb_position"] = self._score_m5_bb_position(m5_row)
        factors["session_quality"] = self._score_session_quality(trading_sessions, bar_time)

        # Check H1/M5 directional agreement
        h1_dir = 1 if factors["h1_trend_gate"] > 0 else (-1 if factors["h1_trend_gate"] < 0 else 0)
        m5_dir = 1 if factors["m5_ema_cross"] > 0 else (-1 if factors["m5_ema_cross"] < 0 else 0)

        # If H1 and M5 disagree on direction, zero out the score
        if h1_dir != 0 and m5_dir != 0 and h1_dir != m5_dir:
            total_score = 0.0
        else:
            total_score = sum(
                factors[f] * M5_FACTOR_WEIGHTS[f] for f in factors
            )

        # Apply session quality as a non-directional multiplier (stricter than intraday)
        sq = factors["session_quality"]
        if sq >= 2.0:
            session_mult = 1.0    # London+NY overlap
        elif sq >= 1.0:
            session_mult = 0.85   # Major session
        elif sq >= 0.0:
            session_mult = 0.55   # Active but not prime
        else:
            session_mult = 0.35   # Off-hours — heavily penalize scalps

        total_score = round(total_score * session_mult, 2)

        if total_score >= M5_SIGNAL_THRESHOLD:
            direction = "BUY"
        elif total_score <= -M5_SIGNAL_THRESHOLD:
            direction = "SELL"
        else:
            direction = None

        abs_score = abs(total_score)
        if abs_score >= M5_HIGH_CONVICTION_THRESHOLD:
            conviction = "HIGH"
        elif abs_score >= M5_SIGNAL_THRESHOLD:
            conviction = "MEDIUM"
        else:
            conviction = None

        return {
            "total_score": total_score,
            "max_score": M5_MAX_SCORE,
            "conviction": conviction,
            "direction": direction,
            "factors": factors,
        }

    def _score_h1_trend_gate(self, row: pd.Series) -> float:
        """H1 trend from SMA20/50 alignment. Must agree with M5 direction. Range: -2 to +2."""
        close = row.get("close")
        sma20 = row.get("sma20")
        sma50 = row.get("sma50")

        if any(pd.isna(v) for v in [close, sma20, sma50]):
            return 0.0

        score = 0.0

        # Price above/below SMA20
        if close > sma20:
            score += 0.5
        elif close < sma20:
            score -= 0.5

        # SMA20 > SMA50 (bullish structure)
        if sma20 > sma50:
            score += 0.5
        elif sma20 < sma50:
            score -= 0.5

        # Price above/below SMA50
        if close > sma50:
            score += 0.5
        elif close < sma50:
            score -= 0.5

        # SMA spread strength
        sma_spread = (sma20 - sma50) / sma50 * 100 if sma50 != 0 else 0
        if sma_spread > 0.1:
            score += 0.5
        elif sma_spread < -0.1:
            score -= 0.5

        return max(-2.0, min(2.0, score))

    def _score_m5_ema_cross(self, m5_df: pd.DataFrame) -> float:
        """M5 EMA9/EMA21 cross detection. Fresh cross (last 3 bars) = full score. Range: -2 to +2."""
        if m5_df.empty or len(m5_df) < 2:
            return 0.0

        row = m5_df.iloc[-1]
        ema9 = row.get("ema9")
        ema21 = row.get("ema21")

        if pd.isna(ema9) or pd.isna(ema21):
            return 0.0

        # Check for fresh crossover in last 3 bars
        fresh_cross = False
        cross_direction = 0
        lookback = min(3, len(m5_df) - 1)

        for i in range(1, lookback + 1):
            prev = m5_df.iloc[-(i + 1)]
            curr = m5_df.iloc[-i]
            prev_ema9 = prev.get("ema9")
            prev_ema21 = prev.get("ema21")
            curr_ema9 = curr.get("ema9")
            curr_ema21 = curr.get("ema21")

            if any(pd.isna(v) for v in [prev_ema9, prev_ema21, curr_ema9, curr_ema21]):
                continue

            # Bullish cross: EMA9 crosses above EMA21
            if prev_ema9 <= prev_ema21 and curr_ema9 > curr_ema21:
                fresh_cross = True
                cross_direction = 1
                break
            # Bearish cross: EMA9 crosses below EMA21
            if prev_ema9 >= prev_ema21 and curr_ema9 < curr_ema21:
                fresh_cross = True
                cross_direction = -1
                break

        if fresh_cross:
            return 2.0 * cross_direction

        # Already aligned (no fresh cross) = half score
        if ema9 > ema21:
            return 1.0
        elif ema9 < ema21:
            return -1.0

        return 0.0

    def _score_m5_momentum(self, row: pd.Series | None) -> float:
        """M5 RSI(7) momentum. Oversold bounce / overbought rejection. Range: -2 to +2."""
        if row is None:
            return 0.0

        rsi7 = row.get("rsi7")
        if pd.isna(rsi7):
            return 0.0

        # Strong oversold = buy signal
        if rsi7 < 20:
            return 2.0
        elif rsi7 < 30:
            return 1.5
        elif rsi7 < 40:
            return 0.5
        # Strong overbought = sell signal
        elif rsi7 > 80:
            return -2.0
        elif rsi7 > 70:
            return -1.5
        elif rsi7 > 60:
            return -0.5

        return 0.0

    def _score_m5_bb_position(self, row: pd.Series | None) -> float:
        """M5 Bollinger Band squeeze + price position. Range: -2 to +2."""
        if row is None:
            return 0.0

        score = 0.0
        bb_bandwidth = row.get("bb_bandwidth")
        bb_mid = row.get("bb_mid")
        bb_upper = row.get("bb_upper")
        bb_lower = row.get("bb_lower")
        close = row.get("close")

        if not pd.isna(bb_bandwidth):
            # Squeeze = potential breakout
            if bb_bandwidth < 0.015:
                if not pd.isna(bb_mid) and not pd.isna(close):
                    if close > bb_mid:
                        score += 1.0
                    else:
                        score -= 1.0

        # BB position for directional bias
        if not pd.isna(close) and not pd.isna(bb_upper) and not pd.isna(bb_lower):
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                bb_pos = (close - bb_lower) / bb_range  # 0 to 1
                if bb_pos > 0.85:
                    score += 1.0
                elif bb_pos > 0.7:
                    score += 0.5
                elif bb_pos < 0.15:
                    score -= 1.0
                elif bb_pos < 0.3:
                    score -= 0.5

        return max(-2.0, min(2.0, score))

    def _score_session_quality(self, trading_sessions: tuple = (), bar_time: datetime | None = None) -> float:
        """Session quality — stricter than intraday for scalps. Range: -2 to +2."""
        now = bar_time or datetime.now(timezone.utc)
        hour = now.hour

        # London+NY overlap (best liquidity for scalps)
        if 13 <= hour < 16:
            return 2.0

        # Major sessions
        if 7 <= hour < 16:   # London
            return 1.0
        if 13 <= hour < 21:  # New York (overlap already caught above)
            return 1.0

        # Outside major sessions — poor for scalping
        if trading_sessions:
            for session in trading_sessions:
                start, end = session.start_hour_utc, session.end_hour_utc
                if start <= end:
                    in_range = start <= hour < end
                else:
                    in_range = hour >= start or hour < end
                if in_range:
                    return 0.0

        return -1.0  # Market closed or low liquidity
