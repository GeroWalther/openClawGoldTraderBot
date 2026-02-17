import pytest


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
        headers={"X-API-Key": "test_secret"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "positions" in data
