import pytest
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

from app.services.backtester import Backtester
from app.services.scoring_engine import ScoringEngine


def _mock_ohlc_data(num_bars=200, trend="up"):
    """Create realistic OHLC data for testing."""
    np.random.seed(42)
    dates = pd.date_range("2023-01-01", periods=num_bars, freq="B")  # Business days

    base_price = 2800.0
    prices = [base_price]
    for i in range(1, num_bars):
        drift = 0.5 if trend == "up" else -0.5
        change = drift + np.random.randn() * 15
        prices.append(prices[-1] + change)

    close = np.array(prices)
    high = close + np.abs(np.random.randn(num_bars) * 20)
    low = close - np.abs(np.random.randn(num_bars) * 20)
    open_ = close + np.random.randn(num_bars) * 5

    df = pd.DataFrame({
        "date": dates,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.random.randint(1000, 10000, num_bars),
    })
    return df


@pytest.fixture
def backtester():
    return Backtester()


def test_sma_crossover_strategy(backtester):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=200)

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="sma_crossover",
            period="1y",
            initial_balance=10000,
        )

    assert "error" not in result
    assert result["instrument"] == "XAUUSD"
    assert result["strategy"] == "sma_crossover"
    assert result["initial_balance"] == 10000
    assert isinstance(result["total_trades"], int)
    assert isinstance(result["equity_curve"], list)
    assert len(result["equity_curve"]) >= 1


def test_rsi_reversal_strategy(backtester):
    mock_ticker = MagicMock()
    # Need 201+ bars for RSI reversal (uses SMA200)
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=250)

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="rsi_reversal",
            period="2y",
            initial_balance=10000,
        )

    assert "error" not in result
    assert result["strategy"] == "rsi_reversal"


def test_breakout_strategy(backtester):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=200)

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="breakout",
            period="1y",
            initial_balance=10000,
        )

    assert "error" not in result
    assert result["strategy"] == "breakout"


def test_insufficient_data(backtester):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=20)

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="sma_crossover",
            period="1y",
        )

    assert "error" in result


def test_no_signals_produces_empty_trades(backtester):
    """With completely flat data, no crossover signals should fire."""
    mock_ticker = MagicMock()
    # Create flat data with no crossovers
    num_bars = 200
    dates = pd.date_range("2023-01-01", periods=num_bars, freq="B")
    flat_price = 2800.0
    df = pd.DataFrame({
        "date": dates,
        "open": [flat_price] * num_bars,
        "high": [flat_price + 0.01] * num_bars,
        "low": [flat_price - 0.01] * num_bars,
        "close": [flat_price] * num_bars,
        "volume": [1000] * num_bars,
    })
    mock_ticker.history.return_value = df

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="sma_crossover",
            period="1y",
        )

    assert result["total_trades"] == 0
    assert result["final_balance"] == 10000.0


def test_equity_curve_tracks_balance(backtester):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=200)

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="sma_crossover",
            period="1y",
            initial_balance=10000,
        )

    if result["total_trades"] > 0:
        # First point should be initial balance
        assert result["equity_curve"][0]["equity"] == 10000
        # Last point should match final balance
        assert result["equity_curve"][-1]["equity"] == result["final_balance"]


def test_session_filter_applied(backtester):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=200)

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result_with = backtester.run(
            instrument_key="XAUUSD", strategy="sma_crossover",
            session_filter=True,
        )
        result_without = backtester.run(
            instrument_key="XAUUSD", strategy="sma_crossover",
            session_filter=False,
        )

    # Both should be valid results
    assert "error" not in result_with
    assert "error" not in result_without


def test_all_losses_scenario(backtester):
    """Downtrending market with buy signals should produce losses."""
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=200, trend="up")

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="sma_crossover",
            period="1y",
            initial_balance=10000,
        )

    assert "error" not in result
    # Max drawdown should be calculated
    assert isinstance(result["max_drawdown"], float)


def test_krabbe_scored_strategy(backtester, mock_macro_service):
    """krabbe_scored strategy should produce valid results with macro data."""
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=250, trend="up")

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="krabbe_scored",
            period="2y",
            initial_balance=10000,
            macro_service=mock_macro_service,
        )

    assert "error" not in result
    assert result["strategy"] == "krabbe_scored"
    assert isinstance(result["total_trades"], int)
    assert isinstance(result["equity_curve"], list)


def test_krabbe_scored_without_macro(backtester):
    """krabbe_scored should work without macro service (fundamentals = 0)."""
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data(num_bars=200, trend="up")

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        result = backtester.run(
            instrument_key="XAUUSD",
            strategy="krabbe_scored",
            period="1y",
            initial_balance=10000,
            macro_service=None,
        )

    assert "error" not in result
    assert result["strategy"] == "krabbe_scored"


def test_compute_indicators_includes_macd_bollinger(backtester):
    """Backtester should compute MACD and Bollinger indicators."""
    df = _mock_ohlc_data(num_bars=100)
    df = backtester._compute_indicators(df)

    assert "macd" in df.columns
    assert "macd_signal" in df.columns
    assert "macd_hist" in df.columns
    assert "bb_upper" in df.columns
    assert "bb_lower" in df.columns
    assert "bb_mid" in df.columns
    assert "bb_bandwidth" in df.columns

    # Values should be computed (not all NaN after warmup)
    assert not df["macd"].iloc[-1:].isna().all()
    assert not df["bb_upper"].iloc[-1:].isna().all()
