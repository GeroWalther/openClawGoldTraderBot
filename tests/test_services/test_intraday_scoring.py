from unittest.mock import patch

import pytest
import pandas as pd

from app.services.intraday_scoring import (
    INTRADAY_FACTOR_WEIGHTS,
    INTRADAY_HIGH_CONVICTION_THRESHOLD,
    INTRADAY_MAX_SCORE,
    INTRADAY_SIGNAL_THRESHOLD,
    IntradayScoringEngine,
)


@pytest.fixture
def engine():
    return IntradayScoringEngine()


def _make_h1_row(**overrides):
    """Create a minimal 1H row Series with default indicator values."""
    defaults = {
        "close": 2800.0,
        "high": 2810.0,
        "low": 2790.0,
        "open": 2795.0,
        "sma20": 2790.0,
        "sma50": 2780.0,
        "rsi": 55.0,
        "atr": 15.0,
        "high_20": 2820.0,
        "low_20": 2760.0,
        "macd": 3.0,
        "macd_signal": 1.0,
        "macd_hist": 2.0,
        "bb_upper": 2830.0,
        "bb_lower": 2750.0,
        "bb_mid": 2790.0,
        "bb_bandwidth": 0.03,
    }
    defaults.update(overrides)
    return pd.Series(defaults)


def _make_m15_row(**overrides):
    """Create a minimal 15m row Series."""
    defaults = {
        "close": 2800.0,
        "rsi": 50.0,
    }
    defaults.update(overrides)
    return pd.Series(defaults)


class TestIntradayScoringEngine:

    def test_threshold_constants(self):
        """Verify intraday threshold constants."""
        assert INTRADAY_SIGNAL_THRESHOLD == 5
        assert INTRADAY_HIGH_CONVICTION_THRESHOLD == 8

    def test_max_score(self):
        """Max score = 2 * sum(weights) = 14 (session_quality weight=0, used as multiplier)."""
        assert INTRADAY_MAX_SCORE == 14.0

    def test_all_factors_present(self, engine):
        """All 6 factors should be present in the result."""
        h1 = _make_h1_row()
        m15 = _make_m15_row()
        result = engine.score(h1, m15)

        assert "factors" in result
        for name in INTRADAY_FACTOR_WEIGHTS:
            assert name in result["factors"], f"Missing factor: {name}"

    def test_factor_scores_within_range(self, engine):
        """Each factor score should be between -2 and +2."""
        h1 = _make_h1_row()
        m15 = _make_m15_row()
        result = engine.score(h1, m15)

        for name, score in result["factors"].items():
            assert -2.0 <= score <= 2.0, f"Factor {name} out of range: {score}"

    @patch.object(IntradayScoringEngine, '_score_session_quality', return_value=2.0)
    def test_bullish_setup_generates_buy(self, _mock_sq, engine):
        """Strong bullish 1H + 15m should produce BUY signal (prime session)."""
        h1 = _make_h1_row(
            close=2850.0, sma20=2840.0, sma50=2820.0,
            rsi=65.0, macd=5.0, macd_signal=2.0, macd_hist=3.0,
            high_20=2845.0, low_20=2780.0,
            bb_upper=2860.0, bb_lower=2810.0, bb_mid=2835.0,
            bb_bandwidth=0.018,
        )
        m15 = _make_m15_row(rsi=58.0)

        result = engine.score(h1, m15)

        assert result["total_score"] > 0
        assert result["direction"] == "BUY"

    @patch.object(IntradayScoringEngine, '_score_session_quality', return_value=2.0)
    def test_bearish_setup_generates_sell(self, _mock_sq, engine):
        """Strong bearish 1H + 15m should produce SELL signal (prime session)."""
        h1 = _make_h1_row(
            close=2700.0, sma20=2720.0, sma50=2740.0,
            rsi=35.0, macd=-5.0, macd_signal=-2.0, macd_hist=-3.0,
            high_20=2780.0, low_20=2705.0,
            bb_upper=2750.0, bb_lower=2700.0, bb_mid=2725.0,
            bb_bandwidth=0.018,
        )
        m15 = _make_m15_row(rsi=42.0)

        result = engine.score(h1, m15)

        assert result["total_score"] < 0
        assert result["direction"] == "SELL"

    def test_neutral_produces_no_trade(self, engine):
        """Mixed signals should produce no trade."""
        h1 = _make_h1_row(
            close=2800.0, sma20=2800.0, sma50=2800.0,
            rsi=50.0, macd=0.0, macd_signal=0.0, macd_hist=0.0,
        )
        m15 = _make_m15_row(rsi=50.0)

        result = engine.score(h1, m15)

        assert result["direction"] is None
        assert result["conviction"] is None

    def test_no_m15_data(self, engine):
        """Scoring should work without 15m data (m15_entry = 0)."""
        h1 = _make_h1_row()
        result = engine.score(h1, None)

        assert result["factors"]["m15_entry"] == 0.0
        assert isinstance(result["total_score"], float)

    def test_result_contains_max_score(self, engine):
        """Result should include max_score field."""
        h1 = _make_h1_row()
        result = engine.score(h1, _make_m15_row())

        assert result["max_score"] == INTRADAY_MAX_SCORE

    def test_h1_trend_bullish(self, engine):
        """SMA20 > SMA50 with price above both = bullish."""
        h1 = _make_h1_row(close=2850.0, sma20=2840.0, sma50=2820.0)
        score = engine._score_h1_trend(h1)
        assert score > 0

    def test_h1_trend_bearish(self, engine):
        """SMA20 < SMA50 with price below both = bearish."""
        h1 = _make_h1_row(close=2700.0, sma20=2720.0, sma50=2740.0)
        score = engine._score_h1_trend(h1)
        assert score < 0

    def test_m15_rsi_extremes(self, engine):
        """RSI extremes on 15m should give strong entry signals."""
        assert engine._score_m15_entry(_make_m15_row(rsi=20.0)) == 2.0
        assert engine._score_m15_entry(_make_m15_row(rsi=80.0)) == -2.0

    def test_sr_proximity_near_support(self, engine):
        """Close to 20-bar low should give positive score (buy zone)."""
        h1 = _make_h1_row(close=2765.0, low_20=2760.0, high_20=2820.0, atr=15.0)
        score = engine._score_sr_proximity(h1)
        assert score > 0

    def test_sr_proximity_near_resistance(self, engine):
        """Close to 20-bar high should give negative score (sell zone)."""
        h1 = _make_h1_row(close=2818.0, low_20=2760.0, high_20=2820.0, atr=15.0)
        score = engine._score_sr_proximity(h1)
        assert score < 0

    def test_nan_handling(self, engine):
        """NaN values should not crash."""
        h1 = _make_h1_row(sma50=float("nan"), macd=float("nan"))
        result = engine.score(h1, None)

        assert isinstance(result["total_score"], float)
        assert not pd.isna(result["total_score"])
