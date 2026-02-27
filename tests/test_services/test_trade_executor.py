import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.schemas import TradeSubmitRequest
from app.models.trade import TradeStatus
from app.services.position_sizer import PositionSizer
from app.services.trade_executor import TradeExecutor
from app.services.trade_validator import TradeValidator


@pytest.mark.asyncio
@patch("app.services.trade_executor.logger")
async def test_executor_successful_trade(mock_logger, settings, db_session, mock_ibkr_client, mock_notifier):
    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
    )

    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100, size=1
    )
    response = await executor.submit_trade(request)

    assert response.status == TradeStatus.EXECUTED
    assert response.deal_id == "1"
    assert response.size == 1
    assert response.instrument == "XAUUSD"
    mock_notifier.send_trade_update.assert_called_once()


@pytest.mark.asyncio
async def test_executor_rejected_trade(settings, db_session, mock_ibkr_client, mock_notifier):
    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
    )

    # Stop distance below instrument minimum (XAUUSD min is 5.0)
    # Also triggers spread protection (spread too wide vs tiny SL)
    request = TradeSubmitRequest(direction="BUY", stop_distance=1.0, limit_distance=100)
    response = await executor.submit_trade(request)

    assert response.status == TradeStatus.REJECTED
    assert response.instrument == "XAUUSD"


@pytest.mark.asyncio
async def test_executor_ibkr_failure(settings, db_session, mock_ibkr_client, mock_notifier):
    mock_ibkr_client.open_position.side_effect = Exception("IBKR connection lost")

    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
    )

    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100, size=1
    )
    response = await executor.submit_trade(request)

    assert response.status == TradeStatus.FAILED
    assert "IBKR connection lost" in response.message


@pytest.mark.asyncio
@patch("app.services.trade_executor.logger")
async def test_executor_with_explicit_instrument(mock_logger, settings, db_session, mock_ibkr_client, mock_notifier):
    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
    )

    request = TradeSubmitRequest(
        instrument="XAUUSD",
        direction="BUY",
        stop_distance=50,
        limit_distance=100,
        size=1,
    )
    response = await executor.submit_trade(request)

    assert response.status == TradeStatus.EXECUTED
    assert response.instrument == "XAUUSD"


@pytest.mark.asyncio
@patch("app.services.trade_executor.logger")
async def test_executor_backward_compat_no_new_services(mock_logger, settings, db_session, mock_ibkr_client, mock_notifier):
    """TradeExecutor works without risk_manager and atr_calculator (backward compat)."""
    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
        risk_manager=None,
        atr_calculator=None,
    )

    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100, size=1
    )
    response = await executor.submit_trade(request)
    assert response.status == TradeStatus.EXECUTED


@pytest.mark.asyncio
async def test_executor_risk_manager_blocks_trade(settings, db_session, mock_ibkr_client, mock_notifier, mock_risk_manager):
    mock_risk_manager.can_trade.return_value = (False, "Cooldown active: 2 consecutive losses")

    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
        risk_manager=mock_risk_manager,
    )

    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100, size=1
    )
    response = await executor.submit_trade(request)

    assert response.status == TradeStatus.REJECTED
    assert "Cooldown active" in response.message


@pytest.mark.asyncio
@patch("app.services.trade_executor.logger")
async def test_executor_atr_provides_defaults(mock_logger, settings, db_session, mock_ibkr_client, mock_notifier, mock_atr_calculator):
    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
        atr_calculator=mock_atr_calculator,
    )

    # No stop/limit distances — ATR should provide limit_distance default
    # stop_level is required by validator; conversion gives stop_distance=45.0
    request = TradeSubmitRequest(
        direction="BUY", stop_distance=None, limit_distance=None, size=1,
        stop_level=2856.0,  # 2901 - 45 = 2856 → stop_distance=45.0
    )
    response = await executor.submit_trade(request)

    # ATR mock returns (45.0, 90.0)
    assert response.stop_distance == 45.0
    assert response.limit_distance == 90.0


@pytest.mark.asyncio
@patch("app.services.trade_executor.logger")
async def test_executor_conviction_in_response(mock_logger, settings, db_session, mock_ibkr_client, mock_notifier):
    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
    )

    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100,
        size=1, conviction="HIGH",
    )
    response = await executor.submit_trade(request)

    assert response.conviction == "HIGH"


@pytest.mark.asyncio
@patch("app.services.trade_executor.logger")
async def test_executor_spread_tracking(mock_logger, settings, db_session, mock_ibkr_client, mock_notifier):
    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)

    executor = TradeExecutor(
        ibkr_client=mock_ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=mock_notifier,
        settings=settings,
    )

    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100, size=1,
    )
    response = await executor.submit_trade(request)

    # bid=2900.50, ask=2901.00 → spread=0.50
    assert response.spread_at_entry == pytest.approx(0.5)
