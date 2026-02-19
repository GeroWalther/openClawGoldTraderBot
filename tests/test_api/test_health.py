import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "trader-bot"
    assert "instruments" in data
    assert len(data["instruments"]) == 6
    keys = [i["key"] for i in data["instruments"]]
    assert "XAUUSD" in keys
    assert "MES" in keys
    assert "EURUSD" in keys
