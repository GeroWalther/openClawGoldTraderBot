import pytest
from datetime import datetime, timezone, timedelta

from app.models.trade import Trade, TradeStatus
from app.services.risk_manager import RiskManager


@pytest.fixture
def risk_manager(settings):
    return RiskManager(settings)


async def _create_closed_trade(db_session, pnl, minutes_ago=0, epic="XAUUSD"):
    """Helper to create a closed trade with P&L."""
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    trade = Trade(
        direction="BUY",
        epic=epic,
        size=1.0,
        status=TradeStatus.CLOSED,
        pnl=pnl,
        closed_at=closed_at,
        entry_price=2900.0,
    )
    db_session.add(trade)
    await db_session.commit()
    return trade


@pytest.mark.asyncio
async def test_no_cooldown_with_no_losses(risk_manager, db_session):
    ok, reason = await risk_manager.check_cooldown(db_session)
    assert ok is True


@pytest.mark.asyncio
async def test_no_cooldown_with_one_loss(risk_manager, db_session):
    await _create_closed_trade(db_session, pnl=-50.0)
    ok, reason = await risk_manager.check_cooldown(db_session)
    assert ok is True
    assert "1 consecutive" in reason


@pytest.mark.asyncio
async def test_cooldown_after_two_losses(risk_manager, db_session):
    await _create_closed_trade(db_session, pnl=-50.0, minutes_ago=10)
    await _create_closed_trade(db_session, pnl=-30.0, minutes_ago=5)
    ok, reason = await risk_manager.check_cooldown(db_session)
    assert ok is False
    assert "Cooldown active" in reason


@pytest.mark.asyncio
async def test_cooldown_expires(risk_manager, db_session):
    # 2 losses but > 2 hours ago
    await _create_closed_trade(db_session, pnl=-50.0, minutes_ago=180)
    await _create_closed_trade(db_session, pnl=-30.0, minutes_ago=150)
    ok, reason = await risk_manager.check_cooldown(db_session)
    assert ok is True
    assert "expired" in reason.lower()


@pytest.mark.asyncio
async def test_cooldown_resets_on_win(risk_manager, db_session):
    await _create_closed_trade(db_session, pnl=-50.0, minutes_ago=30)
    await _create_closed_trade(db_session, pnl=100.0, minutes_ago=20)  # Win resets
    await _create_closed_trade(db_session, pnl=-25.0, minutes_ago=10)
    ok, reason = await risk_manager.check_cooldown(db_session)
    assert ok is True  # Only 1 consecutive loss after the win


@pytest.mark.asyncio
async def test_daily_trade_count_limit(risk_manager, db_session):
    # Create 5 executed trades today
    for _ in range(5):
        trade = Trade(
            direction="BUY", epic="XAUUSD", size=1.0,
            status=TradeStatus.EXECUTED, entry_price=2900.0,
        )
        db_session.add(trade)
    await db_session.commit()

    ok, reason = await risk_manager.can_trade(db_session, account_balance=10000.0)
    assert ok is False
    assert "Daily trade limit" in reason


@pytest.mark.asyncio
async def test_daily_loss_limit(risk_manager, db_session):
    # Create a big losing trade today (> 3% of 10000 = $300)
    await _create_closed_trade(db_session, pnl=-350.0)
    ok, reason = await risk_manager.can_trade(db_session, account_balance=10000.0)
    assert ok is False
    assert "Daily loss limit" in reason


@pytest.mark.asyncio
async def test_can_trade_all_clear(risk_manager, db_session):
    ok, reason = await risk_manager.can_trade(db_session, account_balance=10000.0)
    assert ok is True
    assert "Risk checks passed" in reason


@pytest.mark.asyncio
async def test_disabled_cooldown(settings, db_session):
    settings.cooldown_enabled = False
    rm = RiskManager(settings)
    await _create_closed_trade(db_session, pnl=-50.0, minutes_ago=5)
    await _create_closed_trade(db_session, pnl=-30.0, minutes_ago=2)
    ok, reason = await rm.check_cooldown(db_session)
    assert ok is True
    assert "disabled" in reason.lower()


@pytest.mark.asyncio
async def test_cooldown_status_dict(risk_manager, db_session):
    await _create_closed_trade(db_session, pnl=-50.0, minutes_ago=10)
    await _create_closed_trade(db_session, pnl=-30.0, minutes_ago=5)
    status = await risk_manager.get_cooldown_status(db_session, account_balance=10000.0)
    assert status["cooldown_active"] is True
    assert status["consecutive_losses"] == 2
    assert status["can_trade"] is False
