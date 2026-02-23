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
        # v4 defaults
        session_filter_enabled=True,
        atr_enabled=True,
        cooldown_enabled=True,
        daily_loss_limit_enabled=True,
        conviction_sizing_enabled=True,
        partial_tp_enabled=True,
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
    client.open_position_with_partial_tp.return_value = {
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


@pytest.fixture
def mock_session_filter():
    sf = MagicMock()
    sf.is_session_active.return_value = (True, "Test session active")
    return sf


@pytest.fixture
def mock_atr_calculator():
    atr = MagicMock()
    atr.get_dynamic_sl_tp.return_value = (45.0, 90.0)
    return atr


@pytest.fixture
def mock_risk_manager():
    rm = AsyncMock()
    rm.can_trade.return_value = (True, "Risk checks passed")
    rm.check_cooldown.return_value = (True, "No cooldown")
    return rm


@pytest.fixture
def mock_macro_service():
    """Mock MacroDataService for krabbe_scored tests."""
    import pandas as pd
    import numpy as np

    service = MagicMock()

    # Create mock macro series
    dates = pd.date_range("2023-01-01", periods=250, freq="B")
    data = {
        "DX-Y.NYB": 104.0 + np.random.randn(250).cumsum() * 0.1,
        "^TNX": 4.2 + np.random.randn(250).cumsum() * 0.01,
        "^IRX": 3.8 + np.random.randn(250).cumsum() * 0.01,
        "SI=F": 28.0 + np.random.randn(250).cumsum() * 0.1,
        "^GSPC": 5100.0 + np.random.randn(250).cumsum() * 5,
        "CL=F": 78.0 + np.random.randn(250).cumsum() * 0.5,
    }
    macro_df = pd.DataFrame(data, index=dates)
    # Synthetic yield curve spread
    macro_df["yield_curve"] = macro_df["^TNX"] - macro_df["^IRX"]
    for col in list(macro_df.columns):
        macro_df[f"{col}_change5"] = macro_df[col].diff(5)
        macro_df[f"{col}_trend"] = (macro_df[col] > macro_df[col].shift(5)).astype(float)

    service.get_macro_series.return_value = macro_df
    service.get_macro_data.return_value = {
        "dxy": {"close": 104.0, "trend": "down", "change_5d": -0.5, "correlation": "inverse"},
    }
    service.get_instrument_correlations.return_value = {"DX-Y.NYB": "inverse", "^TNX": "inverse"}
    return service


@pytest.fixture
def mock_journal_service():
    """Mock JournalService for tests."""
    service = AsyncMock()
    service.record_analysis.return_value = MagicMock(
        id=1, instrument="XAUUSD", direction="BUY", conviction="HIGH",
        total_score=16.5, factors='{}', reasoning="Test", trade_idea=None,
        source="krabbe", linked_trade_id=None, outcome="PENDING",
        outcome_notes=None, created_at=None,
    )
    service.get_journal.return_value = []
    service.get_journal_stats.return_value = {
        "total_analyses": 0, "total_with_outcome": 0, "overall_win_rate": 0.0,
        "per_conviction": {}, "avg_score_winners": 0.0, "avg_score_losers": 0.0,
        "score_threshold_accuracy": {},
    }
    return service


@pytest_asyncio.fixture
async def test_app(settings, mock_ibkr_client, mock_atr_calculator):
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
    app.state.atr_calculator = mock_atr_calculator

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
