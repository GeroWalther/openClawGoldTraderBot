import pytest

from app.instruments import get_instrument
from app.services.position_sizer import PositionSizer


@pytest.mark.asyncio
async def test_basic_sizing_xauusd(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    # 1% of 10000 = 100. 100 / (50 * 1) = 2 oz
    size = await sizer.calculate(account_balance=10000, stop_distance=50, instrument=instrument)
    assert size == 2.0


@pytest.mark.asyncio
async def test_capped_at_max_size(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    # 1% of 50000 = 500. 500 / 50 = 10, capped at max=10
    size = await sizer.calculate(account_balance=50000, stop_distance=50, instrument=instrument)
    assert size == 10.0


@pytest.mark.asyncio
async def test_wide_stop_rounds_to_min(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    # 1% of 10000 = 100. 100 / 200 = 0.5 → rounds to 1 (min)
    size = await sizer.calculate(account_balance=10000, stop_distance=200, instrument=instrument)
    assert size == 1.0


@pytest.mark.asyncio
async def test_zero_stop_returns_min(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(account_balance=10000, stop_distance=0, instrument=instrument)
    assert size == instrument.min_size


@pytest.mark.asyncio
async def test_mes_futures_multiplier(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("MES")
    # 1% of 10000 = 100. 100 / (20 * 5) = 1 contract
    size = await sizer.calculate(account_balance=10000, stop_distance=20, instrument=instrument)
    assert size == 1.0


@pytest.mark.asyncio
async def test_forex_rounds_to_nearest_1000(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("EURUSD")
    # 1% of 10000 = 100. 100 / (0.005 * 1) = 20000
    size = await sizer.calculate(account_balance=10000, stop_distance=0.005, instrument=instrument)
    assert size == 20000.0


@pytest.mark.asyncio
async def test_forex_small_balance_returns_min(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("EURUSD")
    # 1% of 100 = 1. 1 / 0.005 = 200 → rounds to 0, but min=20000
    size = await sizer.calculate(account_balance=100, stop_distance=0.005, instrument=instrument)
    assert size == 20000.0


@pytest.mark.asyncio
async def test_btc_futures_sizing(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("BTC")
    # 1% of 100000 = 1000. 1000 / (2000 * 5) = 0.1 → rounds to 1 (min)
    size = await sizer.calculate(account_balance=100000, stop_distance=2000, instrument=instrument)
    assert size == 1.0


@pytest.mark.asyncio
async def test_btc_min_size(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("BTC")
    # Tiny balance → should return min_size 1
    size = await sizer.calculate(account_balance=100, stop_distance=50000, instrument=instrument)
    assert size == 1.0


@pytest.mark.asyncio
async def test_defaults_to_xauusd(settings):
    sizer = PositionSizer(settings)
    size = await sizer.calculate(account_balance=10000, stop_distance=50)
    assert size == 2.0
