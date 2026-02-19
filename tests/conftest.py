import os
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import Settings
from app.models.database import Base


@pytest.fixture
def settings():
    """Test settings with dummy values."""
    return Settings(
        ibkr_host="127.0.0.1",
        ibkr_port=4002,
        ibkr_client_id=99,
        max_risk_percent=1.0,
        telegram_bot_token="123456:ABC-test",
        telegram_chat_id="123456789",
        api_secret_key="test_secret",
        database_url="sqlite+aiosqlite:///:memory:",
        log_level="DEBUG",
    )


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.fixture
def mock_ibkr_client():
    client = AsyncMock()
    client.get_price.return_value = {
        "bid": 2900.50,
        "ask": 2901.00,
        "last": 2900.75,
    }
    # Backward-compat alias
    client.get_gold_price.return_value = {
        "bid": 2900.50,
        "ask": 2901.00,
        "last": 2900.75,
    }
    client.open_position.return_value = {
        "orderId": 1,
        "status": "Filled",
        "direction": "BUY",
        "size": 1.0,
        "fillPrice": 2901.00,
        "dealId": "1",
    }
    client.get_open_positions.return_value = []
    client.get_account_info.return_value = {
        "NetLiquidation": 10000.0,
        "TotalCashValue": 10000.0,
        "AvailableFunds": 8000.0,
    }
    return client


@pytest.fixture
def mock_notifier():
    notifier = AsyncMock()
    return notifier


@pytest_asyncio.fixture
async def test_app(settings, mock_ibkr_client):
    """Create a test FastAPI app with mocked dependencies."""
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

    # Override the lifespan by setting state directly
    app.state.settings = settings
    app.state.ibkr_client = mock_ibkr_client
    app.state.ibkr_connected = True

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.state.engine = engine
    app.state.async_session = async_sessionmaker(engine, expire_on_commit=False)

    yield app

    await engine.dispose()


@pytest_asyncio.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
