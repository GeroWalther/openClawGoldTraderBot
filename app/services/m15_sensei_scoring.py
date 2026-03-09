"""5-factor M15 Sensei scoring engine.

Sensei strategy: double bottom (W) / double top (M) patterns with
consolidation detection, SMA20 cross trigger, SMA100 trend filter,
and RSI veto. Designed for BTC M15 timeframe.

Signal logic mirrors backtest_sensei_v4.py:
  1. SMA20 cross determines proposed direction (cross above → BUY, below → SELL)
  2. Pattern gate: matching pattern must be active (W for BUY, M for SELL)
  3. Consolidation must be present
  4. Trend alignment and RSI are quality filters
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

M15_SENSEI_FACTOR_WEIGHTS = {
    "pattern_match": 3,          # Pattern agrees with SMA20 cross direction — GATE
    "consolidation_quality": 3,  # MA convergence sustained 8+ bars
    "sma20_cross": 2,            # Fresh SMA20 cross = full, aligned = half
    "trend_alignment": 2,        # Price vs SMA100 on H1
    "rsi_confirmation": 1,       # Veto filter: skip overbought longs / oversold shorts
}

M15_SENSEI_MAX_SCORE = sum(2 * w for w in M15_SENSEI_FACTOR_WEIGHTS.values())  # 22
M15_SENSEI_SIGNAL_THRESHOLD = 8
M15_SENSEI_HIGH_CONVICTION_THRESHOLD = 14


class M15SenseiScoringEngine:
    """5-factor scoring engine for M15 Sensei (W/M pattern) trades."""

    def score(
        self,
        h1_row: pd.Series,
        m15_df: pd.DataFrame,
    ) -> dict:
        """Score using Sensei pattern detection on M15 with H1 trend filter.

        V4 signal logic:
          BUY  = w_active AND cross_above AND consolidating
          SELL = m_active AND cross_below AND consolidating
        SMA20 cross determines direction; pattern must agree.
        """
        factors = {}

        # Step 1: SMA20 cross determines proposed direction
        sma20_raw = self._score_sma20_cross(m15_df)
        cross_dir = 1 if sma20_raw > 0 else (-1 if sma20_raw < 0 else 0)

        # Step 2: Pattern gate — matching pattern must be active
        w_active, m_active = self._get_pattern_state(m15_df)
        pattern_agrees = False
        if cross_dir == 1 and w_active:
            pattern_agrees = True
        elif cross_dir == -1 and m_active:
            pattern_agrees = True

        # Step 3: Consolidation gate
        is_consolidating = self._is_consolidating(m15_df)

        # Record factors for visibility
        factors["sma20_cross"] = sma20_raw
        factors["trend_alignment"] = self._score_trend_alignment(h1_row, m15_df)
        factors["rsi_confirmation"] = self._score_rsi(m15_df)
        factors["consolidation_quality"] = self._score_consolidation(m15_df) * cross_dir if cross_dir != 0 else 0.0

        # Pattern match: +2 if W+BUY or M+SELL, 0 otherwise
        if pattern_agrees:
            factors["pattern_match"] = 2.0 * cross_dir
        else:
            factors["pattern_match"] = 0.0

        # Gate: no signal unless pattern agrees + consolidating + SMA20 cross exists
        if not pattern_agrees or not is_consolidating or cross_dir == 0:
            return {
                "total_score": 0.0,
                "max_score": M15_SENSEI_MAX_SCORE,
                "conviction": None,
                "direction": None,
                "factors": factors,
            }

        # RSI veto
        rsi_val = factors["rsi_confirmation"]
        if cross_dir == 1 and rsi_val < 0:
            total_score = 0.0  # Overbought veto on long
        elif cross_dir == -1 and rsi_val > 0:
            total_score = 0.0  # Oversold veto on short
        else:
            # Trend alignment must not strongly oppose direction
            trend_dir = 1 if factors["trend_alignment"] > 0 else (-1 if factors["trend_alignment"] < 0 else 0)
            if trend_dir != 0 and trend_dir != cross_dir:
                total_score = 0.0  # Trend opposes direction (V4: require_trend)
            else:
                total_score = sum(
                    factors[f] * M15_SENSEI_FACTOR_WEIGHTS[f] for f in factors
                )

        total_score = round(total_score, 2)

        if total_score >= M15_SENSEI_SIGNAL_THRESHOLD:
            direction = "BUY"
        elif total_score <= -M15_SENSEI_SIGNAL_THRESHOLD:
            direction = "SELL"
        else:
            direction = None

        abs_score = abs(total_score)
        if abs_score >= M15_SENSEI_HIGH_CONVICTION_THRESHOLD:
            conviction = "HIGH"
        elif abs_score >= M15_SENSEI_SIGNAL_THRESHOLD:
            conviction = "MEDIUM"
        else:
            conviction = None

        return {
            "total_score": total_score,
            "max_score": M15_SENSEI_MAX_SCORE,
            "conviction": conviction,
            "direction": direction,
            "factors": factors,
        }

    def _get_pattern_state(self, m15_df: pd.DataFrame) -> tuple[bool, bool]:
        """Get W/M pattern active state from the latest bar."""
        if m15_df.empty:
            return False, False

        row = m15_df.iloc[-1]
        w = row.get("w_active", False)
        m = row.get("m_active", False)

        if pd.isna(w):
            w = False
        if pd.isna(m):
            m = False

        return bool(w), bool(m)

    def _is_consolidating(self, m15_df: pd.DataFrame) -> bool:
        """Check if consolidation is active (current or previous bar, matching V4)."""
        if m15_df.empty:
            return False

        row = m15_df.iloc[-1]
        is_consol = row.get("is_consolidating", False)
        if pd.isna(is_consol):
            is_consol = False

        if is_consol:
            return True

        # V4 also accepts previous bar
        if len(m15_df) >= 2:
            prev = m15_df.iloc[-2]
            prev_consol = prev.get("is_consolidating", False)
            if pd.isna(prev_consol):
                prev_consol = False
            return bool(prev_consol)

        return False

    def _score_consolidation(self, m15_df: pd.DataFrame) -> float:
        """MA convergence quality magnitude (unsigned). Range: 0 to 2."""
        if m15_df.empty:
            return 0.0

        row = m15_df.iloc[-1]
        conv_count = row.get("conv_count", 0)

        if pd.isna(conv_count):
            conv_count = 0

        if conv_count >= 12:
            return 2.0
        elif conv_count >= 8:
            return 1.5
        elif conv_count >= 4:
            return 1.0
        else:
            return 0.5  # Consolidation gate already passed, min contribution

    def _score_sma20_cross(self, m15_df: pd.DataFrame) -> float:
        """SMA20 cross detection. Only scores FRESH crosses (matching V4). Range: -2 to +2.

        V4 requires cross_above/cross_below on the exact bar for signal entry.
        We check last 3 bars to allow for scanner timing (runs every 15 min).
        No score for mere alignment — must have an actual cross event.
        """
        if m15_df.empty or len(m15_df) < 2:
            return 0.0

        # Check for fresh crossover in last 3 bars
        lookback = min(3, len(m15_df) - 1)
        for i in range(1, lookback + 1):
            prev = m15_df.iloc[-(i + 1)]
            curr = m15_df.iloc[-i]
            prev_close = prev.get("close")
            prev_sma20 = prev.get("sma20")
            curr_close = curr.get("close")
            curr_sma20 = curr.get("sma20")

            if any(pd.isna(v) for v in [prev_close, prev_sma20, curr_close, curr_sma20]):
                continue

            if prev_close <= prev_sma20 and curr_close > curr_sma20:
                return 2.0
            if prev_close >= prev_sma20 and curr_close < curr_sma20:
                return -2.0

        # No fresh cross — no signal (V4 requires the actual cross event)
        return 0.0

    def _score_trend_alignment(self, h1_row: pd.Series, m15_df: pd.DataFrame = None) -> float:
        """Price vs SMA100 trend filter. Range: -2 to +2.

        Uses M15 SMA100 if available (matching V4 which computes SMA100 on same timeframe).
        Falls back to H1 SMA100 for production where M15 data may be limited.
        """
        close = None
        sma100 = None

        # Prefer M15 SMA100 (matches V4: sma_trend on same timeframe)
        if m15_df is not None and not m15_df.empty:
            row = m15_df.iloc[-1]
            sma100 = row.get("sma100")
            close = row.get("close")

        # Fallback to H1 SMA100
        if pd.isna(sma100) or pd.isna(close):
            sma100 = h1_row.get("sma100")
            close = h1_row.get("close")

        if pd.isna(sma100) or pd.isna(close):
            return 0.0

        distance_pct = (close - sma100) / sma100 * 100 if sma100 != 0 else 0

        if distance_pct > 2.0:
            return 2.0
        elif distance_pct > 0.5:
            return 1.0
        elif distance_pct > 0:
            return 0.5
        elif distance_pct < -2.0:
            return -2.0
        elif distance_pct < -0.5:
            return -1.0
        elif distance_pct < 0:
            return -0.5

        return 0.0

    def _score_rsi(self, m15_df: pd.DataFrame) -> float:
        """RSI(14) veto filter. Range: -1 to +1."""
        if m15_df.empty:
            return 0.0

        row = m15_df.iloc[-1]
        rsi = row.get("rsi")

        if pd.isna(rsi):
            return 0.0

        if rsi > 70:
            return -1.0
        elif rsi > 65:
            return -0.5
        if rsi < 30:
            return 1.0
        elif rsi < 35:
            return 0.5

        return 0.0
