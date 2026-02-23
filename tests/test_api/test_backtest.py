import pytest
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np


def _mock_ohlc_data(num_bars=200):
    np.random.seed(42)
    dates = pd.date_range("2023-01-01", periods=num_bars, freq="B")
    base_price = 2800.0
    prices = [base_price]
    for i in range(1, num_bars):
        prices.append(prices[-1] + 0.5 + np.random.randn() * 15)
    close = np.array(prices)
    return pd.DataFrame({
        "date": dates,
        "open": close + np.random.randn(num_bars) * 5,
        "high": close + np.abs(np.random.randn(num_bars) * 20),
        "low": close - np.abs(np.random.randn(num_bars) * 20),
        "close": close,
        "volume": np.random.randint(1000, 10000, num_bars),
    })


@pytest.mark.asyncio
async def test_backtest_requires_auth(client):
    response = await client.post(
        "/api/v1/backtest",
        json={"instrument": "XAUUSD", "strategy": "sma_crossover"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_backtest_rejects_bad_key(client):
    response = await client.post(
        "/api/v1/backtest",
        json={"instrument": "XAUUSD", "strategy": "sma_crossover"},
        headers={"X-API-Key": "wrong_key"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_backtest_rejects_invalid_strategy(client):
    response = await client.post(
        "/api/v1/backtest",
        json={"instrument": "XAUUSD", "strategy": "invalid_strategy"},
        headers={"X-API-Key": "test_secret"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_backtest_sma_crossover(client):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data()

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        response = await client.post(
            "/api/v1/backtest",
            json={
                "instrument": "XAUUSD",
                "strategy": "sma_crossover",
                "period": "1y",
                "initial_balance": 10000,
            },
            headers={"X-API-Key": "test_secret"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["instrument"] == "XAUUSD"
    assert data["strategy"] == "sma_crossover"
    assert "total_trades" in data
    assert "equity_curve" in data
    assert "win_rate" in data
    assert "max_drawdown" in data


@pytest.mark.asyncio
async def test_backtest_response_shape(client):
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_ohlc_data()

    with patch("app.services.backtester.yf.Ticker", return_value=mock_ticker):
        response = await client.post(
            "/api/v1/backtest",
            json={"instrument": "XAUUSD", "strategy": "breakout"},
            headers={"X-API-Key": "test_secret"},
        )

    assert response.status_code == 200
    data = response.json()
    expected_keys = {
        "instrument", "strategy", "period", "initial_balance", "final_balance",
        "total_trades", "winning_trades", "losing_trades", "win_rate",
        "expectancy", "profit_factor", "max_drawdown", "total_return_pct",
        "trades", "equity_curve", "monthly_breakdown",
    }
    assert expected_keys.issubset(data.keys())
