import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_submit_trade_requires_api_key(client):
    response = await client.post(
        "/api/v1/trades/submit",
        json={"direction": "BUY", "stop_distance": 50, "limit_distance": 100},
    )
    # Missing header → 422
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_submit_trade_rejects_invalid_api_key(client):
    response = await client.post(
        "/api/v1/trades/submit",
        json={"direction": "BUY", "stop_distance": 50, "limit_distance": 100},
        headers={"X-API-Key": "wrong_key"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_submit_trade_invalid_direction(client):
    response = await client.post(
        "/api/v1/trades/submit",
        json={"direction": "INVALID", "stop_distance": 50, "limit_distance": 100},
        headers={"X-API-Key": "test_secret"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@patch("app.services.telegram_notifier.Bot")
async def test_submit_trade_success(mock_bot_cls, client, mock_ibkr_client):
    mock_bot = AsyncMock()
    mock_bot_cls.return_value = mock_bot

    response = await client.post(
        "/api/v1/trades/submit",
        json={
            "direction": "BUY",
            "stop_distance": 50,
            "limit_distance": 100,
            "size": 1,
        },
        headers={"X-API-Key": "test_secret"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "executed"
    assert data["direction"] == "BUY"
    assert data["size"] == 1
