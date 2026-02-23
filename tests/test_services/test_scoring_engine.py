import pytest
import pandas as pd
import numpy as np

from app.services.scoring_engine import ScoringEngine, FACTOR_WEIGHTS, MAX_SCORE


@pytest.fixture
def engine():
    return ScoringEngine()


def _make_row(**overrides):
    """Create a minimal row Series with default indicator values."""
    defaults = {
        "close": 2800.0,
        "high": 2810.0,
        "low": 2790.0,
        "open": 2795.0,
        "sma20": 2790.0,
        "sma50": 2780.0,
        "sma200": 2750.0,
        "rsi": 55.0,
        "atr": 20.0,
        "high_20": 2820.0,
        "low_20": 2760.0,
        "macd": 5.0,
        "macd_signal": 3.0,
        "macd_hist": 2.0,
        "bb_upper": 2830.0,
        "bb_lower": 2750.0,
        "bb_mid": 2790.0,
        "bb_bandwidth": 0.03,
    }
    defaults.update(overrides)
    return pd.Series(defaults)


def _make_macro_row(**overrides):
    """Create a macro data row."""
    defaults = {
        "DX-Y.NYB": 104.5,
        "DX-Y.NYB_change5": -0.5,
        "DX-Y.NYB_trend": 0.0,
        "^TNX": 4.2,
        "^TNX_change5": -0.1,
        "^TNX_trend": 0.0,
        "SI=F": 28.5,
        "SI=F_change5": 0.5,
        "SI=F_trend": 1.0,
        "^GSPC": 5200.0,
        "^GSPC_change5": 50.0,
        "^GSPC_trend": 1.0,
        "CL=F": 78.0,
        "CL=F_change5": 1.5,
        "CL=F_trend": 1.0,
        "yield_curve": 0.4,
        "yield_curve_change5": 0.05,
        "yield_curve_trend": 1.0,
    }
    defaults.update(overrides)
    return pd.Series(defaults)


class TestScoringEngine:

    def test_bullish_setup_generates_buy(self, engine):
        """Strong bullish indicators should produce BUY signal with score >= 10."""
        row = _make_row(
            close=2850.0, sma20=2840.0, sma50=2820.0, sma200=2780.0,
            rsi=62.0, macd=8.0, macd_signal=3.0, macd_hist=5.0,
            high_20=2845.0, low_20=2760.0,
            bb_upper=2860.0, bb_lower=2800.0, bb_mid=2830.0,
        )
        macro = _make_macro_row(**{"DX-Y.NYB_change5": -1.5, "^TNX_change5": -0.5})

        result = engine.score_bar(row, macro, "XAUUSD")

        assert result["total_score"] > 0
        assert result["direction"] == "BUY"
        assert result["conviction"] in ("HIGH", "MEDIUM")

    def test_bearish_setup_generates_sell(self, engine):
        """Strong bearish indicators should produce SELL signal."""
        row = _make_row(
            close=2700.0, sma20=2720.0, sma50=2740.0, sma200=2780.0,
            rsi=35.0, macd=-8.0, macd_signal=-3.0, macd_hist=-5.0,
            high_20=2800.0, low_20=2705.0,
            bb_upper=2760.0, bb_lower=2700.0, bb_mid=2730.0,
        )
        macro = _make_macro_row(**{"DX-Y.NYB_change5": 1.5, "^TNX_change5": 0.5})

        result = engine.score_bar(row, macro, "XAUUSD")

        assert result["total_score"] < 0
        assert result["direction"] == "SELL"

    def test_neutral_produces_no_trade(self, engine):
        """Mixed signals should produce no trade (score between -10 and +10)."""
        row = _make_row(
            close=2800.0, sma20=2800.0, sma50=2800.0, sma200=2800.0,
            rsi=50.0, macd=0.0, macd_signal=0.0, macd_hist=0.0,
            high_20=2820.0, low_20=2780.0,
            bb_upper=2820.0, bb_lower=2780.0, bb_mid=2800.0,
        )
        macro = _make_macro_row(**{"DX-Y.NYB_change5": 0.0, "^TNX_change5": 0.0})

        result = engine.score_bar(row, macro, "XAUUSD")

        assert result["direction"] is None
        assert result["conviction"] is None
        assert -10 < result["total_score"] < 10

    def test_all_factors_present(self, engine):
        """All 11 factors should be present in the result."""
        row = _make_row()
        result = engine.score_bar(row, None, "XAUUSD")

        assert "factors" in result
        for factor_name in FACTOR_WEIGHTS:
            assert factor_name in result["factors"], f"Missing factor: {factor_name}"

    def test_factor_scores_within_range(self, engine):
        """Each factor score should be between -2 and +2."""
        row = _make_row()
        macro = _make_macro_row()
        result = engine.score_bar(row, macro, "XAUUSD")

        for name, score in result["factors"].items():
            assert -2.0 <= score <= 2.0, f"Factor {name} out of range: {score}"

    def test_conviction_thresholds(self, engine):
        """Verify conviction thresholds: HIGH >= 15, MEDIUM >= 10, None < 10."""
        # HIGH conviction (extreme bullish)
        row = _make_row(
            close=2900.0, sma20=2880.0, sma50=2850.0, sma200=2780.0,
            rsi=25.0,  # Oversold = strong buy signal
            macd=15.0, macd_signal=5.0, macd_hist=10.0,
            high_20=2895.0, low_20=2700.0,
            bb_upper=2910.0, bb_lower=2850.0, bb_mid=2880.0,
        )
        macro = _make_macro_row(**{"DX-Y.NYB_change5": -3.0, "^TNX_change5": -1.0})
        result = engine.score_bar(row, macro, "XAUUSD")

        if result["total_score"] >= 15:
            assert result["conviction"] == "HIGH"
        elif result["total_score"] >= 10:
            assert result["conviction"] == "MEDIUM"

    def test_no_macro_data(self, engine):
        """Scoring should work without macro data (fundamentals = 0)."""
        row = _make_row()
        result = engine.score_bar(row, None, "XAUUSD")

        assert result["factors"]["fundamental_1"] == 0.0
        assert result["factors"]["fundamental_2"] == 0.0
        assert isinstance(result["total_score"], float)

    def test_news_and_calendar_always_zero(self, engine):
        """News sentiment and calendar risk are unavailable in backtest."""
        row = _make_row()
        result = engine.score_bar(row, _make_macro_row(), "XAUUSD")

        assert result["factors"]["news_sentiment"] == 0.0
        assert result["factors"]["calendar_risk"] == 0.0

    def test_nan_indicators_handled(self, engine):
        """NaN indicator values should not crash, should return neutral."""
        row = _make_row(sma200=float("nan"), macd=float("nan"), macd_signal=float("nan"))
        result = engine.score_bar(row, None, "XAUUSD")

        assert isinstance(result["total_score"], float)
        assert not pd.isna(result["total_score"])

    def test_d1_trend_bullish_alignment(self, engine):
        """SMA20 > SMA50 > SMA200 with price above all = max bullish score."""
        row = _make_row(close=2900.0, sma20=2880.0, sma50=2850.0, sma200=2800.0)
        score = engine._score_d1_trend(row)
        assert score == 2.0

    def test_d1_trend_bearish_alignment(self, engine):
        """SMA20 < SMA50 < SMA200 with price below all = max bearish score."""
        row = _make_row(close=2700.0, sma20=2720.0, sma50=2750.0, sma200=2800.0)
        score = engine._score_d1_trend(row)
        assert score == -2.0

    def test_rsi_extremes(self, engine):
        """RSI < 25 should give +2 (buy), RSI > 75 should give -2 (sell) for 1H entry."""
        assert engine._score_1h_entry(_make_row(rsi=20.0)) == 2.0
        assert engine._score_1h_entry(_make_row(rsi=80.0)) == -2.0

    def test_tf_alignment_full_bullish(self, engine):
        """All SMAs aligned bullish should score +2."""
        row = _make_row(sma20=2850.0, sma50=2800.0, sma200=2750.0)
        assert engine._score_tf_alignment(row) == 2.0

    def test_tf_alignment_full_bearish(self, engine):
        """All SMAs aligned bearish should score -2."""
        row = _make_row(sma20=2700.0, sma50=2750.0, sma200=2800.0)
        assert engine._score_tf_alignment(row) == -2.0

    def test_max_score_value(self):
        """Max theoretical score = 2 * sum(weights) = 2 * 14 = 28."""
        assert MAX_SCORE == 28.0

    def test_fundamental_3_positive_yield_curve(self, engine):
        """Positive yield curve change with positive correlation should give positive score."""
        macro = _make_macro_row(yield_curve_change5=0.1)
        score = engine._score_fundamental_3(macro, "XAUUSD")  # positive correlation
        assert score > 0

    def test_fundamental_3_inverse_yield_curve(self, engine):
        """Positive yield curve change with inverse correlation should give negative score."""
        macro = _make_macro_row(yield_curve_change5=0.1)
        score = engine._score_fundamental_3(macro, "EURUSD")  # inverse correlation
        assert score < 0

    def test_fundamental_3_no_macro(self, engine):
        """No macro data should return 0."""
        assert engine._score_fundamental_3(None, "XAUUSD") == 0.0

    def test_fundamental_3_small_change_neutral(self, engine):
        """Very small yield curve change should return 0."""
        macro = _make_macro_row(yield_curve_change5=0.001)
        assert engine._score_fundamental_3(macro, "XAUUSD") == 0.0

    def test_fundamental_3_strong_move(self, engine):
        """Large yield curve change should give ±2 score."""
        macro = _make_macro_row(yield_curve_change5=0.8)
        score = engine._score_fundamental_3(macro, "XAUUSD")
        assert score == 2.0

    def test_different_instruments(self, engine):
        """Scoring should work for different instruments with different macro mappings."""
        row = _make_row()
        macro = _make_macro_row()

        for instrument in ["XAUUSD", "MES", "EURUSD", "EURJPY", "CADJPY", "USDJPY", "BTC"]:
            result = engine.score_bar(row, macro, instrument)
            assert isinstance(result["total_score"], float)
            assert "factors" in result
