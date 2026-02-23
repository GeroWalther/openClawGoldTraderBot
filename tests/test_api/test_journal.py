import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.database import Base


@pytest_asyncio.fixture
async def journal_app(settings, mock_ibkr_client, mock_atr_calculator):
    """Create test app with journal table."""
    import os
    os.environ.update({
        "IBKR_HOST": settings.ibkr_host,
        "IBKR_PORT": str(settings.ibkr_port),
        "IBKR_CLIENT_ID": str(settings.ibkr_client_id),
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token,
        "TELEGRAM_CHAT_ID": settings.telegram_chat_id,
        "API_SECRET_KEY": settings.api_secret_key,
        "DATABASE_URL": settings.database_url,
    })

    from app.main import app

    app.state.settings = settings
    app.state.ibkr_client = mock_ibkr_client
    app.state.ibkr_connected = True
    app.state.atr_calculator = mock_atr_calculator

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.state.engine = engine
    app.state.async_session = async_sessionmaker(engine, expire_on_commit=False)

    yield app

    await engine.dispose()


@pytest_asyncio.fixture
async def journal_client(journal_app):
    transport = ASGITransport(app=journal_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


API_KEY = "test_secret"
HEADERS = {"X-API-Key": API_KEY}


class TestJournalAPI:

    @pytest.mark.asyncio
    async def test_create_journal_entry(self, journal_client):
        """POST /api/v1/journal should create an entry."""
        resp = await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={
                "instrument": "XAUUSD",
                "direction": "BUY",
                "conviction": "HIGH",
                "total_score": 16.5,
                "factors": {"d1_trend": 2},
                "reasoning": "Strong bullish setup",
                "trade_idea": {"stop_distance": 45, "limit_distance": 90},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] is not None
        assert data["instrument"] == "XAUUSD"
        assert data["direction"] == "BUY"
        assert data["conviction"] == "HIGH"
        assert data["total_score"] == 16.5
        assert data["outcome"] == "PENDING"

    @pytest.mark.asyncio
    async def test_create_no_trade_entry(self, journal_client):
        """NO_TRADE direction should work."""
        resp = await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={
                "instrument": "XAUUSD",
                "direction": "NO_TRADE",
                "total_score": 5.0,
                "reasoning": "Insufficient edge",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "SKIPPED"

    @pytest.mark.asyncio
    async def test_auth_required(self, journal_client):
        """Missing or wrong API key should return 401."""
        resp = await journal_client.post(
            "/api/v1/journal",
            headers={"X-API-Key": "wrong"},
            json={"instrument": "XAUUSD", "direction": "BUY", "total_score": 10.0},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_journal_entries(self, journal_client):
        """GET /api/v1/journal should list entries."""
        # Create some entries
        await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={"instrument": "XAUUSD", "direction": "BUY", "total_score": 15.0, "conviction": "HIGH"},
        )
        await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={"instrument": "MES", "direction": "SELL", "total_score": -12.0, "conviction": "MEDIUM"},
        )

        resp = await journal_client.get("/api/v1/journal", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_instrument(self, journal_client):
        """GET /api/v1/journal?instrument=XAUUSD should filter."""
        await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={"instrument": "XAUUSD", "direction": "BUY", "total_score": 15.0},
        )
        await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={"instrument": "MES", "direction": "SELL", "total_score": -12.0},
        )

        resp = await journal_client.get("/api/v1/journal?instrument=XAUUSD", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["instrument"] == "XAUUSD"

    @pytest.mark.asyncio
    async def test_get_journal_stats(self, journal_client):
        """GET /api/v1/journal/stats should return stats."""
        resp = await journal_client.get("/api/v1/journal/stats", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_analyses" in data
        assert "overall_win_rate" in data
        assert "per_conviction" in data

    @pytest.mark.asyncio
    async def test_update_outcome(self, journal_client):
        """PATCH /api/v1/journal/{id} should update outcome."""
        # Create entry
        resp = await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={"instrument": "XAUUSD", "direction": "BUY", "total_score": 15.0, "conviction": "HIGH"},
        )
        entry_id = resp.json()["id"]

        # Update outcome
        resp = await journal_client.patch(
            f"/api/v1/journal/{entry_id}?outcome=WIN&outcome_notes=Hit TP",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "WIN"
        assert data["outcome_notes"] == "Hit TP"

    @pytest.mark.asyncio
    async def test_link_trade(self, journal_client):
        """PATCH /api/v1/journal/{id}?linked_trade_id=X should link."""
        resp = await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={"instrument": "XAUUSD", "direction": "BUY", "total_score": 15.0},
        )
        entry_id = resp.json()["id"]

        resp = await journal_client.patch(
            f"/api/v1/journal/{entry_id}?linked_trade_id=42",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["linked_trade_id"] == 42

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_404(self, journal_client):
        """PATCH on nonexistent entry should return 404."""
        resp = await journal_client.patch(
            "/api/v1/journal/9999?outcome=WIN",
            headers=HEADERS,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_direction_rejected(self, journal_client):
        """Invalid direction should be rejected by validation."""
        resp = await journal_client.post(
            "/api/v1/journal",
            headers=HEADERS,
            json={"instrument": "XAUUSD", "direction": "INVALID", "total_score": 10.0},
        )
        assert resp.status_code == 422
