import pytest
from datetime import datetime, timezone, timedelta

from app.models.trade import Trade, TradeStatus
from app.services.analytics import TradeAnalytics


@pytest.fixture
def analytics():
    return TradeAnalytics()


async def _create_trades(db_session, trade_data):
    """Helper to create closed trades with specified P&L values."""
    base_time = datetime.now(timezone.utc) - timedelta(days=30)
    for i, (pnl, epic) in enumerate(trade_data):
        trade = Trade(
            direction="BUY",
            epic=epic,
            size=1.0,
            status=TradeStatus.CLOSED,
            pnl=pnl,
            entry_price=2900.0,
            stop_distance=50.0,
            limit_distance=100.0,
            closed_at=base_time + timedelta(days=i),
            created_at=base_time + timedelta(days=i),
        )
        db_session.add(trade)
    await db_session.commit()


@pytest.mark.asyncio
async def test_empty_analytics(analytics, db_session):
    result = await analytics.calculate(db_session)
    assert result["total_trades"] == 0
    assert result["win_rate"] == 0.0


@pytest.mark.asyncio
async def test_win_rate(analytics, db_session):
    await _create_trades(db_session, [
        (100.0, "XAUUSD"),
        (-50.0, "XAUUSD"),
        (75.0, "XAUUSD"),
        (200.0, "XAUUSD"),
    ])
    result = await analytics.calculate(db_session)
    assert result["total_trades"] == 4
    assert result["winning_trades"] == 3
    assert result["losing_trades"] == 1
    assert result["win_rate"] == 75.0


@pytest.mark.asyncio
async def test_avg_win_loss(analytics, db_session):
    await _create_trades(db_session, [
        (100.0, "XAUUSD"),
        (200.0, "XAUUSD"),
        (-50.0, "XAUUSD"),
        (-30.0, "XAUUSD"),
    ])
    result = await analytics.calculate(db_session)
    assert result["avg_win"] == 150.0
    assert result["avg_loss"] == -40.0


@pytest.mark.asyncio
async def test_expectancy(analytics, db_session):
    # 2 wins of $100, 2 losses of -$50
    await _create_trades(db_session, [
        (100.0, "XAUUSD"),
        (-50.0, "XAUUSD"),
        (100.0, "XAUUSD"),
        (-50.0, "XAUUSD"),
    ])
    result = await analytics.calculate(db_session)
    # Expectancy = (0.5 * 100) + (0.5 * -50) = 25
    assert result["expectancy"] == 25.0


@pytest.mark.asyncio
async def test_profit_factor(analytics, db_session):
    await _create_trades(db_session, [
        (200.0, "XAUUSD"),
        (-100.0, "XAUUSD"),
    ])
    result = await analytics.calculate(db_session)
    # Profit factor = 200 / 100 = 2.0
    assert result["profit_factor"] == 2.0


@pytest.mark.asyncio
async def test_max_drawdown(analytics, db_session):
    await _create_trades(db_session, [
        (100.0, "XAUUSD"),
        (-50.0, "XAUUSD"),
        (-80.0, "XAUUSD"),
        (200.0, "XAUUSD"),
    ])
    result = await analytics.calculate(db_session)
    # Peak after first win: 100. Drop to 100-50-80 = -30. DD = 130
    assert result["max_drawdown"] == 130.0


@pytest.mark.asyncio
async def test_streaks(analytics, db_session):
    await _create_trades(db_session, [
        (100.0, "XAUUSD"),
        (50.0, "XAUUSD"),
        (75.0, "XAUUSD"),
        (-30.0, "XAUUSD"),
        (-20.0, "XAUUSD"),
    ])
    result = await analytics.calculate(db_session)
    assert result["max_win_streak"] == 3
    assert result["max_loss_streak"] == 2
    assert result["current_streak"] == -2  # Ending on losses


@pytest.mark.asyncio
async def test_per_instrument_breakdown(analytics, db_session):
    await _create_trades(db_session, [
        (100.0, "XAUUSD"),
        (-50.0, "XAUUSD"),
        (80.0, "EURUSD"),
        (120.0, "EURUSD"),
    ])
    result = await analytics.calculate(db_session)
    assert "XAUUSD" in result["per_instrument"]
    assert "EURUSD" in result["per_instrument"]
    assert result["per_instrument"]["XAUUSD"]["total_trades"] == 2
    assert result["per_instrument"]["EURUSD"]["total_trades"] == 2
    assert result["per_instrument"]["EURUSD"]["win_rate"] == 100.0


@pytest.mark.asyncio
async def test_filter_by_instrument(analytics, db_session):
    await _create_trades(db_session, [
        (100.0, "XAUUSD"),
        (80.0, "EURUSD"),
    ])
    result = await analytics.calculate(db_session, instrument="XAUUSD")
    assert result["total_trades"] == 1
    assert result["total_pnl"] == 100.0


@pytest.mark.asyncio
async def test_filter_by_date_range(analytics, db_session):
    await _create_trades(db_session, [
        (100.0, "XAUUSD"),
        (80.0, "XAUUSD"),
    ])
    # Filter to exclude old trades
    future = datetime.now(timezone.utc) + timedelta(days=1)
    result = await analytics.calculate(db_session, from_date=future)
    assert result["total_trades"] == 0
