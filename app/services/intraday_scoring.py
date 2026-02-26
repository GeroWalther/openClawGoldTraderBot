"""6-factor intraday/scalp scoring engine.

Focused on short timeframes (1H/15m) with no fundamental factors.
Designed for quick trades with tighter stops and faster signals.
"""

import logging
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger(__name__)

INTRADAY_FACTOR_WEIGHTS = {
    "h1_trend": 2.0,          # SMA20/50 alignment on 1H
    "h1_momentum": 1.5,       # MACD + RSI on 1H
    "m15_entry": 1.5,         # RSI extremes on 15m
    "sr_proximity": 1.0,      # Distance to 1H S/R levels
    "volatility": 1.0,        # ATR expansion + BB squeeze on 1H
    "session_quality": 0.0,   # Non-directional gate — applied as score multiplier, not additive
}

INTRADAY_MAX_SCORE = sum(2 * w for w in INTRADAY_FACTOR_WEIGHTS.values())  # 16

INTRADAY_SIGNAL_THRESHOLD = 5
INTRADAY_HIGH_CONVICTION_THRESHOLD = 8

# High-liquidity session windows (UTC hours)
HIGH_LIQUIDITY_SESSIONS = {
    "London": (7, 16),
    "New York": (13, 21),
    "London+NY overlap": (13, 16),
}


class IntradayScoringEngine:
    """6-factor scoring engine for intraday/scalp trades."""

    def score(
        self,
        h1_row: pd.Series,
        m15_row: pd.Series | None,
        trading_sessions: tuple = (),
    ) -> dict:
        """Score using 1H and 15m data.

        Args:
            h1_row: Latest 1H bar with computed indicators.
            m15_row: Latest 15m bar with computed indicators (may be None).
            trading_sessions: Instrument's trading sessions for session quality.

        Returns:
            dict with total_score, conviction, direction, and per-factor scores.
        """
        factors = {}

        factors["h1_trend"] = self._score_h1_trend(h1_row)
        factors["h1_momentum"] = self._score_h1_momentum(h1_row)
        factors["m15_entry"] = self._score_m15_entry(m15_row)
        factors["sr_proximity"] = self._score_sr_proximity(h1_row)
        factors["volatility"] = self._score_volatility(h1_row)
        factors["session_quality"] = self._score_session_quality(trading_sessions)

        total_score = sum(
            factors[f] * INTRADAY_FACTOR_WEIGHTS[f] for f in factors
        )

        # Apply session quality as a non-directional multiplier (gate)
        # Good session: full score. Marginal session: 70%. Bad session: 40%.
        sq = factors["session_quality"]
        if sq >= 2.0:
            session_mult = 1.0    # London+NY overlap
        elif sq >= 1.0:
            session_mult = 0.85   # Major session
        elif sq >= 0.0:
            session_mult = 0.70   # Active but not prime
        else:
            session_mult = 0.40   # Off-hours — heavily penalize
        total_score = total_score * session_mult

        total_score = round(total_score, 2)

        if total_score >= INTRADAY_SIGNAL_THRESHOLD:
            direction = "BUY"
        elif total_score <= -INTRADAY_SIGNAL_THRESHOLD:
            direction = "SELL"
        else:
            direction = None

        abs_score = abs(total_score)
        if abs_score >= INTRADAY_HIGH_CONVICTION_THRESHOLD:
            conviction = "HIGH"
        elif abs_score >= INTRADAY_SIGNAL_THRESHOLD:
            conviction = "MEDIUM"
        else:
            conviction = None

        return {
            "total_score": total_score,
            "max_score": INTRADAY_MAX_SCORE,
            "conviction": conviction,
            "direction": direction,
            "factors": factors,
        }

    def _score_h1_trend(self, row: pd.Series) -> float:
        """1H trend from SMA20/50 alignment + price position. Range: -2 to +2."""
        score = 0.0
        close = row.get("close")
        sma20 = row.get("sma20")
        sma50 = row.get("sma50")

        if any(pd.isna(v) for v in [close, sma20, sma50]):
            return 0.0

        # Price above/below SMA20
        if close > sma20:
            score += 0.5
        elif close < sma20:
            score -= 0.5

        # SMA20 > SMA50 (bullish)
        if sma20 > sma50:
            score += 0.5
        elif sma20 < sma50:
            score -= 0.5

        # Price above/below SMA50
        if close > sma50:
            score += 0.5
        elif close < sma50:
            score -= 0.5

        # SMA slope (SMA20 rising/falling approximation using price vs SMA)
        sma_spread = (sma20 - sma50) / sma50 * 100 if sma50 != 0 else 0
        if sma_spread > 0.1:
            score += 0.5
        elif sma_spread < -0.1:
            score -= 0.5

        return max(-2.0, min(2.0, score))

    def _score_h1_momentum(self, row: pd.Series) -> float:
        """1H momentum from MACD + RSI. Range: -2 to +2."""
        score = 0.0
        macd = row.get("macd")
        macd_signal = row.get("macd_signal")
        macd_hist = row.get("macd_hist")
        rsi = row.get("rsi")

        if not pd.isna(macd) and not pd.isna(macd_signal):
            if macd > macd_signal:
                score += 1.0
            elif macd < macd_signal:
                score -= 1.0

        if not pd.isna(macd_hist):
            if macd_hist > 0:
                score += 0.5
            elif macd_hist < 0:
                score -= 0.5

        if not pd.isna(rsi):
            if rsi > 60:
                score += 0.5
            elif rsi < 40:
                score -= 0.5

        return max(-2.0, min(2.0, score))

    def _score_m15_entry(self, row: pd.Series | None) -> float:
        """15m entry from RSI extremes. Range: -2 to +2."""
        if row is None:
            return 0.0

        rsi = row.get("rsi")
        if pd.isna(rsi):
            return 0.0

        # Strong oversold = buy signal
        if rsi < 25:
            return 2.0
        elif rsi < 30:
            return 1.0
        # Strong overbought = sell signal
        elif rsi > 75:
            return -2.0
        elif rsi > 70:
            return -1.0
        # Mild bias
        elif rsi > 55:
            return 0.5
        elif rsi < 45:
            return -0.5

        return 0.0

    def _score_sr_proximity(self, row: pd.Series) -> float:
        """S/R proximity — distance to 20-bar high/low as % of ATR. Range: -2 to +2."""
        close = row.get("close")
        high_20 = row.get("high_20")
        low_20 = row.get("low_20")
        atr = row.get("atr")

        if any(pd.isna(v) for v in [close, high_20, low_20, atr]) or atr == 0:
            return 0.0

        dist_to_high = (high_20 - close) / atr
        dist_to_low = (close - low_20) / atr

        # Near support (good for buying)
        if dist_to_low < 0.5:
            return 1.5
        elif dist_to_low < 1.0:
            return 0.5

        # Near resistance (good for selling)
        if dist_to_high < 0.5:
            return -1.5
        elif dist_to_high < 1.0:
            return -0.5

        return 0.0

    def _score_volatility(self, row: pd.Series) -> float:
        """Volatility from ATR expansion + BB squeeze. Range: -2 to +2."""
        score = 0.0
        bb_bandwidth = row.get("bb_bandwidth")
        bb_mid = row.get("bb_mid")
        bb_upper = row.get("bb_upper")
        bb_lower = row.get("bb_lower")
        close = row.get("close")

        if not pd.isna(bb_bandwidth):
            # Squeeze = potential breakout
            if bb_bandwidth < 0.02:
                if not pd.isna(bb_mid) and not pd.isna(close):
                    if close > bb_mid:
                        score += 1.0
                    else:
                        score -= 1.0
            # Expansion = trend continuation
            elif bb_bandwidth > 0.04:
                if not pd.isna(close) and not pd.isna(bb_upper) and not pd.isna(bb_lower):
                    if close > bb_upper:
                        score += 1.0
                    elif close < bb_lower:
                        score -= 1.0

        # BB position for directional bias
        if not pd.isna(close) and not pd.isna(bb_upper) and not pd.isna(bb_lower):
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                bb_pos = (close - bb_lower) / bb_range  # 0 to 1
                if bb_pos > 0.8:
                    score += 0.5
                elif bb_pos < 0.2:
                    score -= 0.5

        return max(-2.0, min(2.0, score))

    def _score_session_quality(self, trading_sessions: tuple = ()) -> float:
        """Session quality based on current UTC hour. Range: -2 to +2."""
        now = datetime.now(timezone.utc)
        hour = now.hour

        # Check if we're in London+NY overlap (best liquidity)
        if 13 <= hour < 16:
            return 2.0

        # Check if we're in any high-liquidity session
        for name, (start, end) in HIGH_LIQUIDITY_SESSIONS.items():
            if start <= hour < end:
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
