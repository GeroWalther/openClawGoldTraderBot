"""4-factor M15 Bollinger Band Bounce scoring engine.

Range-specialist strategy: trades BB touch + reversal candle on M15.
No H1 trend gate needed — designed to trade when M5 scalp goes quiet
(neutral/ranging H1). Uses strict BB bandwidth filter + RSI extreme gate
to only take high-probability mean-reversion setups.

Factors:
  1. bb_touch      (3.0) — Price penetrated BB band + strong reversal candle (GATE)
  2. rsi_extreme   (2.5) — RSI confirms oversold/overbought (GATE — must agree)
  3. bb_squeeze    (1.5) — BB bandwidth narrow = range-bound (bonus, penalizes wide)
  4. session_quality (0.0) — Non-directional multiplier

Tuned for: low drawdown, modest but consistent profit, few trades.
SL: 1.2×ATR | TP: 2.5×ATR (R:R ~1:2)
"""

import logging
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger(__name__)

M15_BB_FACTOR_WEIGHTS = {
    "bb_touch": 3.0,           # BB penetration + reversal candle — primary GATE
    "rsi_extreme": 2.5,        # RSI extreme confirmation — secondary GATE
    "bb_squeeze": 1.5,         # BB bandwidth filter (narrow = good for bounce)
    "session_quality": 0.0,    # Non-directional gate — applied as score multiplier
}

M15_BB_MAX_SCORE = sum(2 * w for w in M15_BB_FACTOR_WEIGHTS.values())  # 14

M15_BB_SIGNAL_THRESHOLD = 8.0
M15_BB_HIGH_CONVICTION_THRESHOLD = 11.0


class M15BBBounceScoringEngine:
    """4-factor scoring engine for M15 Bollinger Band Bounce trades."""

    def __init__(
        self,
        signal_threshold: float | None = None,
        high_conviction_threshold: float | None = None,
    ):
        self.signal_threshold = signal_threshold if signal_threshold is not None else M15_BB_SIGNAL_THRESHOLD
        self.high_conviction_threshold = high_conviction_threshold if high_conviction_threshold is not None else M15_BB_HIGH_CONVICTION_THRESHOLD

    def score(
        self,
        m15_df: pd.DataFrame,
        trading_sessions: tuple = (),
        bar_time: datetime | None = None,
    ) -> dict:
        """Score using M15 BB bounce signals.

        Strict gating:
          - bb_touch MUST fire (price must penetrate BB + reversal candle)
          - rsi_extreme MUST agree with bb_touch direction
          - BB bandwidth must not be too wide (trending market kills bounces)
        """
        factors = {}

        m15_row = m15_df.iloc[-1] if not m15_df.empty else None

        factors["bb_touch"] = self._score_bb_touch(m15_row)
        factors["rsi_extreme"] = self._score_rsi_extreme(m15_row)
        factors["bb_squeeze"] = self._score_bb_squeeze(m15_row, factors["bb_touch"])
        factors["session_quality"] = self._score_session_quality(trading_sessions, bar_time)

        bb_dir = 1 if factors["bb_touch"] > 0 else (-1 if factors["bb_touch"] < 0 else 0)
        rsi_dir = 1 if factors["rsi_extreme"] > 0 else (-1 if factors["rsi_extreme"] < 0 else 0)

        # GATE 1: BB touch must fire
        if bb_dir == 0:
            total_score = 0.0
        # GATE 2: RSI must agree (not just "not contradict" — must actively confirm)
        elif rsi_dir != bb_dir:
            total_score = 0.0
        # GATE 3: BB bandwidth must not be wide (trending = bounce fails)
        elif factors["bb_squeeze"] * bb_dir < 0:
            # bb_squeeze is negative in the signal direction = wide BB = reject
            total_score = 0.0
        else:
            total_score = sum(
                factors[f] * M15_BB_FACTOR_WEIGHTS[f] for f in factors
            )

        # Apply session quality multiplier
        sq = factors["session_quality"]
        if sq >= 2.0:
            session_mult = 1.0    # London+NY overlap
        elif sq >= 1.0:
            session_mult = 0.85   # Major session
        elif sq >= 0.0:
            session_mult = 0.55   # Active but not prime
        else:
            session_mult = 0.0    # Off-hours — block completely

        total_score = round(total_score * session_mult, 2)

        if total_score >= self.signal_threshold:
            direction = "BUY"
        elif total_score <= -self.signal_threshold:
            direction = "SELL"
        else:
            direction = None

        abs_score = abs(total_score)
        if abs_score >= self.high_conviction_threshold:
            conviction = "HIGH"
        elif abs_score >= self.signal_threshold:
            conviction = "MEDIUM"
        else:
            conviction = None

        return {
            "total_score": total_score,
            "max_score": M15_BB_MAX_SCORE,
            "conviction": conviction,
            "direction": direction,
            "factors": factors,
        }

    def _score_bb_touch(self, row: pd.Series | None) -> float:
        """BB penetration + reversal candle. GATE signal. Range: -2 to +2.

        Requires actual penetration (wick through BB) + close back inside
        as a reversal candle. "Just touched" is not enough.
        """
        if row is None:
            return 0.0

        close = row.get("close")
        open_ = row.get("open")
        high = row.get("high")
        low = row.get("low")
        bb_upper = row.get("bb_upper")
        bb_lower = row.get("bb_lower")
        atr = row.get("atr")

        if any(pd.isna(v) for v in [close, open_, high, low, bb_upper, bb_lower, atr]):
            return 0.0
        if atr <= 0:
            return 0.0

        body = abs(close - open_)
        bar_range = high - low
        if bar_range <= 0:
            return 0.0

        # Body must be meaningful (not a doji)
        body_ratio = body / bar_range
        if body_ratio < 0.25:
            return 0.0

        # Bullish: low penetrated below lower BB, closed above it, bullish candle
        if low < bb_lower and close > bb_lower and close > open_:
            penetration = (bb_lower - low) / atr
            if penetration > 0.3:
                return 2.0   # Deep penetration + strong reversal
            elif penetration > 0.05:
                return 1.5   # Clear penetration + reversal
            else:
                return 0.0   # Barely touched — skip

        # Bearish: high penetrated above upper BB, closed below it, bearish candle
        if high > bb_upper and close < bb_upper and close < open_:
            penetration = (high - bb_upper) / atr
            if penetration > 0.3:
                return -2.0
            elif penetration > 0.05:
                return -1.5
            else:
                return 0.0

        return 0.0

    def _score_rsi_extreme(self, row: pd.Series | None) -> float:
        """RSI extreme confirmation. GATE — must agree with BB touch. Range: -2 to +2.

        Stricter thresholds: only score at real extremes (RSI<30 or RSI>70).
        """
        if row is None:
            return 0.0

        rsi = row.get("rsi")
        if pd.isna(rsi):
            return 0.0

        # Oversold = buy confirmation (strict: must be < 35)
        if rsi < 20:
            return 2.0
        elif rsi < 25:
            return 1.5
        elif rsi < 30:
            return 1.0
        elif rsi < 35:
            return 0.5

        # Overbought = sell confirmation (strict: must be > 65)
        elif rsi > 80:
            return -2.0
        elif rsi > 75:
            return -1.5
        elif rsi > 70:
            return -1.0
        elif rsi > 65:
            return -0.5

        # RSI in neutral zone (35-65) → no confirmation
        return 0.0

    def _score_bb_squeeze(self, row: pd.Series | None, bb_touch_score: float) -> float:
        """BB bandwidth filter. Narrow = range = good for bounce. Range: -2 to +2.

        Stricter: rejects if bandwidth > 0.025 (was 0.040).
        Wide BB means trending → bounce strategy will fail.
        """
        if row is None:
            return 0.0

        bw = row.get("bb_bandwidth")
        if pd.isna(bw):
            return 0.0

        direction = 1 if bb_touch_score > 0 else (-1 if bb_touch_score < 0 else 0)
        if direction == 0:
            return 0.0

        # Tight squeeze = ideal for mean-reversion
        if bw < 0.008:
            return 2.0 * direction
        elif bw < 0.012:
            return 1.5 * direction
        elif bw < 0.018:
            return 1.0 * direction
        elif bw < 0.025:
            return 0.5 * direction
        else:
            # Wide BB = trending market, penalize (will trigger gate rejection)
            return -1.0 * direction

    def _score_session_quality(self, trading_sessions: tuple = (), bar_time: datetime | None = None) -> float:
        """Session quality. Range: -2 to +2."""
        now = bar_time or datetime.now(timezone.utc)
        hour = now.hour

        # London+NY overlap (best liquidity)
        if 13 <= hour < 16:
            return 2.0

        # Major sessions
        if 7 <= hour < 16:   # London
            return 1.0
        if 13 <= hour < 21:  # New York
            return 1.0

        # Outside major sessions
        if trading_sessions:
            for session in trading_sessions:
                start, end = session.start_hour_utc, session.end_hour_utc
                if start <= end:
                    in_range = start <= hour < end
                else:
                    in_range = hour >= start or hour < end
                if in_range:
                    return 0.0

        return -1.0
