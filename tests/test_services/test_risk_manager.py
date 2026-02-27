import pytest
from datetime import datetime, timezone, timedelta

from app.models.trade import Trade, TradeStatus
from app.services.risk_manager import RiskManager


@pytest.fixture
def risk_manager(settings):
    return RiskManager(settings)


async def _create_closed_trade(db_session, pnl, minutes_ago=0, epic="XAUUSD", strategy=None):
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
        strategy=strategy,
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
async def test_daily_trade_count_limit(settings, db_session):
    # Set a low limit for testing
    settings.max_daily_trades = 5
    rm = RiskManager(settings)
    for _ in range(5):
        trade = Trade(
            direction="BUY", epic="XAUUSD", size=1.0,
            status=TradeStatus.EXECUTED, entry_price=2900.0,
        )
        db_session.add(trade)
    await db_session.commit()

    ok, reason = await rm.can_trade(db_session, account_balance=10000.0)
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


# --- Scalp cooldown tests ---

@pytest.mark.asyncio
async def test_scalp_cooldown_blocks_after_two_losses(risk_manager, db_session):
    """2 consecutive m5_scalp losses → blocked for 10 min."""
    await _create_closed_trade(db_session, pnl=-20.0, minutes_ago=8, strategy="m5_scalp")
    await _create_closed_trade(db_session, pnl=-15.0, minutes_ago=3, strategy="m5_scalp")
    ok, reason = await risk_manager.can_trade(db_session, 10000.0, strategy="m5_scalp")
    assert ok is False
    assert "Scalp cooldown active" in reason


@pytest.mark.asyncio
async def test_scalp_cooldown_expires(risk_manager, db_session):
    """2 consecutive m5_scalp losses > 10 min ago → scalp cooldown passes."""
    await _create_closed_trade(db_session, pnl=-20.0, minutes_ago=30, strategy="m5_scalp")
    await _create_closed_trade(db_session, pnl=-15.0, minutes_ago=20, strategy="m5_scalp")
    # Test scalp-specific check (main cooldown would also fire for these losses)
    ok, reason = await risk_manager._check_scalp_cooldown(db_session)
    assert ok is True
    assert "expired" in reason.lower()


@pytest.mark.asyncio
async def test_scalp_cooldown_win_resets(risk_manager, db_session):
    """A scalp win resets the consecutive loss count."""
    await _create_closed_trade(db_session, pnl=-20.0, minutes_ago=15, strategy="m5_scalp")
    await _create_closed_trade(db_session, pnl=50.0, minutes_ago=10, strategy="m5_scalp")  # win
    await _create_closed_trade(db_session, pnl=-10.0, minutes_ago=3, strategy="m5_scalp")  # 1 loss after win
    ok, reason = await risk_manager.can_trade(db_session, 10000.0, strategy="m5_scalp")
    assert ok is True  # only 1 consecutive loss, threshold is 2


@pytest.mark.asyncio
async def test_scalp_cooldown_exponential(settings, db_session):
    """3 consecutive losses → 20 min cooldown (10 * 2^1)."""
    rm = RiskManager(settings)
    await _create_closed_trade(db_session, pnl=-20.0, minutes_ago=15, strategy="m5_scalp")
    await _create_closed_trade(db_session, pnl=-15.0, minutes_ago=10, strategy="m5_scalp")
    await _create_closed_trade(db_session, pnl=-10.0, minutes_ago=3, strategy="m5_scalp")
    ok, reason = await rm.can_trade(db_session, 10000.0, strategy="m5_scalp")
    assert ok is False
    assert "20 min cooldown" in reason


@pytest.mark.asyncio
async def test_scalp_cooldown_ignores_non_scalp_losses(risk_manager, db_session):
    """Non-scalp losses don't count toward scalp cooldown."""
    await _create_closed_trade(db_session, pnl=-20.0, minutes_ago=5, strategy="intraday")
    await _create_closed_trade(db_session, pnl=-15.0, minutes_ago=3, strategy="intraday")
    ok, reason = await risk_manager.can_trade(db_session, 10000.0, strategy="m5_scalp")
    # Scalp cooldown should pass (no scalp losses) — may still fail main cooldown
    # so we check the scalp-specific method directly
    ok2, reason2 = await risk_manager._check_scalp_cooldown(db_session)
    assert ok2 is True


@pytest.mark.asyncio
async def test_scalp_cooldown_disabled(settings, db_session):
    """Scalp cooldown disabled → always passes."""
    settings.scalp_cooldown_enabled = False
    rm = RiskManager(settings)
    await _create_closed_trade(db_session, pnl=-20.0, minutes_ago=3, strategy="m5_scalp")
    await _create_closed_trade(db_session, pnl=-15.0, minutes_ago=1, strategy="m5_scalp")
    ok, reason = await rm._check_scalp_cooldown(db_session)
    assert ok is True
    assert "disabled" in reason.lower()
