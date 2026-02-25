"""Tests for technical analysis endpoints and services."""

import numpy as np
import pandas as pd
import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock

from app.services.indicators import compute_indicators
from app.services.technical_analyzer import (
    TechnicalAnalyzer,
    _resample_to_4h,
    _classify_trend,
    _sma_alignment,
    _macd_crossover,
    _build_timeframe_block,
    _session_info,
)


# ── indicator tests ─────────────────────────────────────────────────


def _make_ohlcv(n: int = 250, base_price: float = 100.0) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame for testing."""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = base_price + np.random.randn(n).cumsum()
    high = close + np.abs(np.random.randn(n))
    low = close - np.abs(np.random.randn(n))
    open_ = close + np.random.randn(n) * 0.5
    volume = np.random.randint(1000, 10000, n)
    return pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }, index=dates)


class TestComputeIndicators:
    def test_adds_all_columns(self):
        df = _make_ohlcv()
        result = compute_indicators(df)
        expected_cols = [
            "sma20", "sma50", "sma200", "rsi", "atr",
            "high_20", "low_20",
            "macd", "macd_signal", "macd_hist",
            "bb_mid", "bb_upper", "bb_lower", "bb_bandwidth",
        ]
        for col in expected_cols:
            assert col in result.columns, f"Missing column: {col}"

    def test_rsi_range(self):
        df = _make_ohlcv()
        compute_indicators(df)
        rsi = df["rsi"].dropna()
        assert (rsi >= 0).all() and (rsi <= 100).all()

    def test_atr_positive(self):
        df = _make_ohlcv()
        compute_indicators(df)
        atr = df["atr"].dropna()
        assert (atr > 0).all()

    def test_bollinger_bands_order(self):
        df = _make_ohlcv()
        compute_indicators(df)
        valid = df.dropna(subset=["bb_upper", "bb_lower"])
        assert (valid["bb_upper"] >= valid["bb_lower"]).all()

    def test_macd_signal_not_all_nan(self):
        df = _make_ohlcv()
        compute_indicators(df)
        assert df["macd_signal"].dropna().shape[0] > 200

    def test_mutates_in_place(self):
        df = _make_ohlcv()
        result = compute_indicators(df)
        assert result is df


# ── helper function tests ───────────────────────────────────────────


class TestResampleTo4H:
    def test_resample_reduces_rows(self):
        dates = pd.date_range("2024-01-02", periods=100, freq="1h")
        df = pd.DataFrame({
            "open": np.ones(100),
            "high": np.ones(100) * 2,
            "low": np.ones(100) * 0.5,
            "close": np.ones(100),
            "volume": np.ones(100) * 100,
        }, index=dates)
        result = _resample_to_4h(df)
        assert len(result) < len(df)
        assert len(result) == 25

    def test_empty_input(self):
        result = _resample_to_4h(pd.DataFrame())
        assert result.empty


class TestClassifyTrend:
    def test_bullish(self):
        row = pd.Series({"close": 110, "sma20": 105, "sma50": 100, "sma200": 95})
        assert _classify_trend(row) == "bullish"

    def test_bearish(self):
        row = pd.Series({"close": 90, "sma20": 95, "sma50": 100, "sma200": 105})
        assert _classify_trend(row) == "bearish"

    def test_neutral(self):
        row = pd.Series({"close": 102, "sma20": 105, "sma50": 100, "sma200": 95})
        assert _classify_trend(row) == "neutral"

    def test_missing_data(self):
        row = pd.Series({"close": 100, "sma20": float("nan"), "sma50": 100})
        assert _classify_trend(row) == "neutral"


class TestSmaAlignment:
    def test_full_bullish(self):
        row = pd.Series({"sma20": 110, "sma50": 100, "sma200": 90})
        assert _sma_alignment(row) == "20>50>200"

    def test_full_bearish(self):
        row = pd.Series({"sma20": 90, "sma50": 100, "sma200": 110})
        assert _sma_alignment(row) == "200>50>20"

    def test_no_sma200(self):
        row = pd.Series({"sma20": 110, "sma50": 100, "sma200": float("nan")})
        assert _sma_alignment(row) == "20>50"


class TestMacdCrossover:
    def test_bullish(self):
        row = pd.Series({"macd": 1.0, "macd_signal": 0.5})
        assert _macd_crossover(row) == "bullish"

    def test_bearish(self):
        row = pd.Series({"macd": -0.5, "macd_signal": 0.5})
        assert _macd_crossover(row) == "bearish"

    def test_nan(self):
        row = pd.Series({"macd": float("nan"), "macd_signal": 0.5})
        assert _macd_crossover(row) == "neutral"


# ── TechnicalAnalyzer tests ─────────────────────────────────────────


def _mock_fetch_ohlcv(symbol, period, interval):
    """Return synthetic OHLCV data for any symbol."""
    n = 250 if interval == "1d" else (200 if period == "1mo" else 100)
    freq = "B" if interval == "1d" else "1h"
    dates = pd.date_range("2024-01-01", periods=n, freq=freq)
    np.random.seed(hash(symbol + period + interval) % (2**31))
    base = 2900 if "GC" in symbol else 100
    close = base + np.random.randn(n).cumsum()
    return pd.DataFrame({
        "open": close + np.random.randn(n) * 0.5,
        "high": close + np.abs(np.random.randn(n)),
        "low": close - np.abs(np.random.randn(n)),
        "close": close,
        "volume": np.random.randint(1000, 10000, n),
    }, index=dates)


class TestTechnicalAnalyzer:
    @pytest.mark.asyncio
    async def test_analyze_returns_all_sections(self):
        analyzer = TechnicalAnalyzer()
        with patch(
            "app.services.technical_analyzer._fetch_ohlcv",
            side_effect=_mock_fetch_ohlcv,
        ), patch.object(
            analyzer._macro_service, "get_macro_data", return_value={}
        ), patch.object(
            analyzer._macro_service, "get_macro_series", return_value=pd.DataFrame()
        ):
            result = await analyzer.analyze("XAUUSD")

        assert "error" not in result, f"Unexpected error: {result}"
        assert result["instrument"] == "XAUUSD"
        assert "price" in result
        assert "technicals" in result
        assert "scoring" in result
        assert "session" in result
        assert "summary" in result
        assert "d1" in result["technicals"]

    @pytest.mark.asyncio
    async def test_analyze_unknown_instrument(self):
        analyzer = TechnicalAnalyzer()
        result = await analyzer.analyze("UNKNOWN")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_analyze_caches_results(self):
        analyzer = TechnicalAnalyzer()
        call_count = 0

        def counting_fetch(symbol, period, interval):
            nonlocal call_count
            call_count += 1
            return _mock_fetch_ohlcv(symbol, period, interval)

        with patch(
            "app.services.technical_analyzer._fetch_ohlcv",
            side_effect=counting_fetch,
        ), patch.object(
            analyzer._macro_service, "get_macro_data", return_value={}
        ), patch.object(
            analyzer._macro_service, "get_macro_series", return_value=pd.DataFrame()
        ):
            result1 = await analyzer.analyze("XAUUSD")
            first_count = call_count
            result2 = await analyzer.analyze("XAUUSD")

        # Second call should hit cache — no additional fetches
        assert call_count == first_count
        assert result1["timestamp"] == result2["timestamp"]

    @pytest.mark.asyncio
    async def test_scan_all_returns_all_instruments(self):
        analyzer = TechnicalAnalyzer()
        with patch(
            "app.services.technical_analyzer._fetch_ohlcv",
            side_effect=_mock_fetch_ohlcv,
        ), patch.object(
            analyzer._macro_service, "get_macro_data", return_value={}
        ), patch.object(
            analyzer._macro_service, "get_macro_series", return_value=pd.DataFrame()
        ):
            result = await analyzer.scan_all()

        assert "instruments" in result
        assert result["instrument_count"] == 8  # All instruments
        instrument_keys = {i["instrument"] for i in result["instruments"]}
        assert "XAUUSD" in instrument_keys
        assert "MES" in instrument_keys

    @pytest.mark.asyncio
    async def test_scoring_has_factors(self):
        analyzer = TechnicalAnalyzer()
        with patch(
            "app.services.technical_analyzer._fetch_ohlcv",
            side_effect=_mock_fetch_ohlcv,
        ), patch.object(
            analyzer._macro_service, "get_macro_data", return_value={}
        ), patch.object(
            analyzer._macro_service, "get_macro_series", return_value=pd.DataFrame()
        ):
            result = await analyzer.analyze("XAUUSD")

        scoring = result.get("scoring", {})
        assert "total_score" in scoring
        assert "max_score" in scoring
        assert scoring["max_score"] == 26
        assert "factors" in scoring

    @pytest.mark.asyncio
    async def test_graceful_degradation_on_fetch_failure(self):
        analyzer = TechnicalAnalyzer()

        def failing_fetch(symbol, period, interval):
            if interval == "1d":
                return _mock_fetch_ohlcv(symbol, period, interval)
            raise Exception("Network error")

        with patch(
            "app.services.technical_analyzer._fetch_ohlcv",
            side_effect=failing_fetch,
        ), patch.object(
            analyzer._macro_service, "get_macro_data", return_value={}
        ), patch.object(
            analyzer._macro_service, "get_macro_series", return_value=pd.DataFrame()
        ):
            result = await analyzer.analyze("XAUUSD")

        # Should still have D1 data even if H1/H4 failed
        assert "error" not in result
        assert "d1" in result.get("technicals", {})


# ── API endpoint tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_technicals_endpoint_auth(client):
    """Technicals endpoint requires valid API key."""
    resp = await client.get(
        "/api/v1/technicals/XAUUSD",
        headers={"X-API-Key": "wrong_key"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_technicals_scan_endpoint_auth(client):
    """Scan endpoint requires valid API key."""
    resp = await client.get(
        "/api/v1/technicals/scan",
        headers={"X-API-Key": "wrong_key"},
    )
    assert resp.status_code == 401
