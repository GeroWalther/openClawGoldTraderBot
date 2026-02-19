import pytest
from unittest.mock import AsyncMock, patch


HEADERS = {"X-API-Key": "test_secret"}


@pytest.mark.asyncio
async def test_positions_requires_api_key(client):
    response = await client.get("/api/v1/positions/")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_positions_rejects_invalid_api_key(client):
    response = await client.get(
        "/api/v1/positions/",
        headers={"X-API-Key": "wrong_key"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_positions_returns_list(client):
    response = await client.get(
        "/api/v1/positions/",
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert "positions" in data


# --- Modify SL/TP tests ---


@pytest.mark.asyncio
@patch("app.services.telegram_notifier.Bot")
async def test_modify_sl_tp_both(mock_bot_cls, client, mock_ibkr_client):
    mock_bot_cls.return_value = AsyncMock()
    mock_ibkr_client.modify_sl_tp.return_value = {
        "old_sl": 2850.0,
        "old_tp": 2950.0,
        "new_sl": 2870.0,
        "new_tp": 2960.0,
    }

    response = await client.post(
        "/api/v1/positions/modify",
        json={
            "instrument": "XAUUSD",
            "direction": "BUY",
            "new_stop_loss": 2870.0,
            "new_take_profit": 2960.0,
            "reasoning": "Moving SL to breakeven",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "modified"
    assert data["instrument"] == "XAUUSD"
    assert data["direction"] == "BUY"
    assert data["old_stop_loss"] == 2850.0
    assert data["old_take_profit"] == 2950.0
    assert data["new_stop_loss"] == 2870.0
    assert data["new_take_profit"] == 2960.0


@pytest.mark.asyncio
@patch("app.services.telegram_notifier.Bot")
async def test_modify_sl_only(mock_bot_cls, client, mock_ibkr_client):
    mock_bot_cls.return_value = AsyncMock()
    mock_ibkr_client.modify_sl_tp.return_value = {
        "old_sl": 2850.0,
        "old_tp": 2950.0,
        "new_sl": 2870.0,
        "new_tp": None,
    }

    response = await client.post(
        "/api/v1/positions/modify",
        json={
            "instrument": "XAUUSD",
            "direction": "BUY",
            "new_stop_loss": 2870.0,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["new_stop_loss"] == 2870.0
    assert data["new_take_profit"] is None


@pytest.mark.asyncio
@patch("app.services.telegram_notifier.Bot")
async def test_modify_tp_only(mock_bot_cls, client, mock_ibkr_client):
    mock_bot_cls.return_value = AsyncMock()
    mock_ibkr_client.modify_sl_tp.return_value = {
        "old_sl": 2850.0,
        "old_tp": 2950.0,
        "new_sl": None,
        "new_tp": 2980.0,
    }

    response = await client.post(
        "/api/v1/positions/modify",
        json={
            "instrument": "XAUUSD",
            "direction": "SELL",
            "new_take_profit": 2980.0,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["new_stop_loss"] is None
    assert data["new_take_profit"] == 2980.0


@pytest.mark.asyncio
async def test_modify_no_sl_or_tp_returns_400(client):
    response = await client.post(
        "/api/v1/positions/modify",
        json={
            "instrument": "XAUUSD",
            "direction": "BUY",
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "at least one" in response.json()["detail"].lower()


@pytest.mark.asyncio
@patch("app.services.telegram_notifier.Bot")
async def test_modify_no_open_orders_returns_404(mock_bot_cls, client, mock_ibkr_client):
    mock_bot_cls.return_value = AsyncMock()
    mock_ibkr_client.modify_sl_tp.side_effect = RuntimeError(
        "No STP (stop-loss) order found for XAUUSD BUY"
    )

    response = await client.post(
        "/api/v1/positions/modify",
        json={
            "instrument": "XAUUSD",
            "direction": "BUY",
            "new_stop_loss": 2870.0,
        },
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert "stop-loss" in response.json()["detail"].lower()


# --- Trade Status tests ---


@pytest.mark.asyncio
async def test_status_returns_all_sections(client, mock_ibkr_client):
    mock_ibkr_client.get_open_positions.return_value = [
        {
            "instrument": "XAUUSD",
            "symbol": "XAUUSD",
            "size": 1.0,
            "direction": "BUY",
            "avg_cost": 2900.0,
            "unrealized_pnl": None,
            "size_unit": "oz",
        }
    ]
    mock_ibkr_client.get_open_orders.return_value = [
        {
            "orderId": 10,
            "parentId": 5,
            "orderType": "STP",
            "action": "SELL",
            "totalQuantity": 1.0,
            "lmtPrice": None,
            "auxPrice": 2850.0,
            "status": "PreSubmitted",
            "instrument": "XAUUSD",
        },
        {
            "orderId": 11,
            "parentId": 5,
            "orderType": "LMT",
            "action": "SELL",
            "totalQuantity": 1.0,
            "lmtPrice": 2950.0,
            "auxPrice": None,
            "status": "PreSubmitted",
            "instrument": "XAUUSD",
        },
    ]

    response = await client.get(
        "/api/v1/positions/status",
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert "positions" in data
    assert "open_orders" in data
    assert "account" in data
    assert "recent_trades" in data

    # Positions should be enriched with SL/TP and P&L
    pos = data["positions"][0]
    assert pos["stop_loss"] == 2850.0
    assert pos["take_profit"] == 2950.0
    assert pos["unrealized_pnl"] is not None
    assert pos["current_price"] is not None
