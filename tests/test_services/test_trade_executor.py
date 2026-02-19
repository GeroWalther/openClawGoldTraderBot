import pytest
from unittest.mock import AsyncMock, patch

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

    # Missing stop loss
    request = TradeSubmitRequest(direction="BUY", limit_distance=100)
    response = await executor.submit_trade(request)

    assert response.status == TradeStatus.REJECTED
    assert response.instrument == "XAUUSD"
    assert "Stop loss is required" in response.message
    mock_notifier.send_rejection.assert_called_once()


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
