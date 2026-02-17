import pytest

from app.services.position_sizer import PositionSizer


@pytest.mark.asyncio
async def test_basic_sizing(settings):
    sizer = PositionSizer(settings)
    # 1% of 10000 = 100. 100 / 50 = 2 oz
    size = await sizer.calculate(account_balance=10000, stop_distance=50)
    assert size == 2.0


@pytest.mark.asyncio
async def test_large_balance(settings):
    sizer = PositionSizer(settings)
    # 1% of 50000 = 500. 500 / 50 = 10 oz, capped at max_position_size=10
    size = await sizer.calculate(account_balance=50000, stop_distance=50)
    assert size == 10.0


@pytest.mark.asyncio
async def test_wide_stop(settings):
    sizer = PositionSizer(settings)
    # 1% of 10000 = 100. 100 / 200 = 0.5 → rounds to 1 (IBKR min)
    size = await sizer.calculate(account_balance=10000, stop_distance=200)
    assert size == 1.0  # IBKR minimum 1 ounce


@pytest.mark.asyncio
async def test_minimum_size(settings):
    sizer = PositionSizer(settings)
    # Very small balance → rounds to IBKR minimum of 1 ounce
    size = await sizer.calculate(account_balance=100, stop_distance=200)
    assert size == 1.0


@pytest.mark.asyncio
async def test_zero_stop_returns_min(settings):
    sizer = PositionSizer(settings)
    size = await sizer.calculate(account_balance=10000, stop_distance=0)
    assert size == settings.min_position_size
