import pytest


@pytest.mark.asyncio
async def test_analytics_requires_auth(client):
    response = await client.get("/api/v1/analytics")
    assert response.status_code == 422  # Missing header


@pytest.mark.asyncio
async def test_analytics_rejects_bad_key(client):
    response = await client.get(
        "/api/v1/analytics",
        headers={"X-API-Key": "wrong_key"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_analytics_returns_empty(client):
    response = await client.get(
        "/api/v1/analytics",
        headers={"X-API-Key": "test_secret"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_trades"] == 0
    assert data["win_rate"] == 0.0


@pytest.mark.asyncio
async def test_analytics_with_date_params(client):
    response = await client.get(
        "/api/v1/analytics?from_date=2024-01-01&to_date=2024-12-31",
        headers={"X-API-Key": "test_secret"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_analytics_with_instrument_filter(client):
    response = await client.get(
        "/api/v1/analytics?instrument=XAUUSD",
        headers={"X-API-Key": "test_secret"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_cooldown_requires_auth(client):
    response = await client.get("/api/v1/analytics/cooldown")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_cooldown_returns_status(client):
    response = await client.get(
        "/api/v1/analytics/cooldown",
        headers={"X-API-Key": "test_secret"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "can_trade" in data
    assert "cooldown_active" in data
    assert "consecutive_losses" in data
    assert "daily_trades_count" in data


@pytest.mark.asyncio
async def test_analytics_response_shape(client):
    response = await client.get(
        "/api/v1/analytics",
        headers={"X-API-Key": "test_secret"},
    )
    data = response.json()
    expected_keys = {
        "total_trades", "winning_trades", "losing_trades", "win_rate",
        "avg_win", "avg_loss", "expectancy", "profit_factor", "total_pnl",
        "max_drawdown", "planned_rr", "achieved_rr", "current_streak",
        "max_win_streak", "max_loss_streak", "per_instrument",
        "daily_pnl", "weekly_pnl", "monthly_pnl",
    }
    assert expected_keys.issubset(data.keys())
