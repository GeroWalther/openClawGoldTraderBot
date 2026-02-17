import pytest

from app.models.schemas import TradeSubmitRequest
from app.services.trade_validator import TradeValidator


@pytest.mark.asyncio
async def test_valid_buy_trade(settings):
    validator = TradeValidator(settings)
    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100
    )
    valid, msg = await validator.validate(request, current_price=2900.0)
    assert valid is True
    assert msg == "Validation passed"


@pytest.mark.asyncio
async def test_valid_sell_trade(settings):
    validator = TradeValidator(settings)
    request = TradeSubmitRequest(
        direction="SELL", stop_distance=30, limit_distance=60
    )
    valid, msg = await validator.validate(request, current_price=2900.0)
    assert valid is True


@pytest.mark.asyncio
async def test_rejects_missing_stop(settings):
    validator = TradeValidator(settings)
    request = TradeSubmitRequest(direction="BUY", limit_distance=100)
    valid, msg = await validator.validate(request, current_price=2900.0)
    assert valid is False
    assert "Stop loss is required" in msg


@pytest.mark.asyncio
async def test_rejects_stop_too_small(settings):
    validator = TradeValidator(settings)
    request = TradeSubmitRequest(
        direction="BUY", stop_distance=3, limit_distance=100
    )
    valid, msg = await validator.validate(request, current_price=2900.0)
    assert valid is False
    assert "below min" in msg


@pytest.mark.asyncio
async def test_rejects_stop_too_large(settings):
    validator = TradeValidator(settings)
    request = TradeSubmitRequest(
        direction="BUY", stop_distance=350, limit_distance=500
    )
    valid, msg = await validator.validate(request, current_price=2900.0)
    assert valid is False
    assert "above max" in msg


@pytest.mark.asyncio
async def test_rejects_bad_risk_reward(settings):
    validator = TradeValidator(settings)
    request = TradeSubmitRequest(
        direction="BUY", stop_distance=100, limit_distance=50
    )
    valid, msg = await validator.validate(request, current_price=2900.0)
    assert valid is False
    assert "R:R ratio" in msg


@pytest.mark.asyncio
async def test_rejects_oversized_position(settings):
    validator = TradeValidator(settings)
    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100, size=15.0
    )
    valid, msg = await validator.validate(request, current_price=2900.0)
    assert valid is False
    assert "exceeds max" in msg


@pytest.mark.asyncio
async def test_rejects_undersized_position(settings):
    validator = TradeValidator(settings)
    request = TradeSubmitRequest(
        direction="BUY", stop_distance=50, limit_distance=100, size=0.5
    )
    valid, msg = await validator.validate(request, current_price=2900.0)
    assert valid is False
    assert "below IBKR min" in msg
