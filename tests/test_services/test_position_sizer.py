import pytest

from app.instruments import get_instrument
from app.services.position_sizer import PositionSizer


@pytest.mark.asyncio
async def test_basic_sizing_xauusd(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    # max_risk_percent=3% of 10000 = 300. 300 / (50 * 1) = 6 oz
    # (no conviction → uses max_risk_percent)
    size = await sizer.calculate(account_balance=10000, stop_distance=50, instrument=instrument)
    assert size == 6.0


@pytest.mark.asyncio
async def test_capped_at_max_size(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    # 3% of 50000 = 1500. 1500 / 50 = 30 → capped at max=10
    size = await sizer.calculate(account_balance=50000, stop_distance=50, instrument=instrument)
    assert size == 10.0


@pytest.mark.asyncio
async def test_wide_stop_rounds_to_min(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    # 3% of 10000 = 300. 300 / 200 = 1.5 → rounds to 2
    size = await sizer.calculate(account_balance=10000, stop_distance=200, instrument=instrument)
    assert size == 2.0


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
    # 3% of 10000 = 300. 300 / (20 * 5) = 3 contracts
    size = await sizer.calculate(account_balance=10000, stop_distance=20, instrument=instrument)
    assert size == 3.0


@pytest.mark.asyncio
async def test_forex_rounds_to_nearest_1000(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("EURUSD")
    # 3% of 10000 = 300. 300 / (0.005 * 1) = 60000
    size = await sizer.calculate(account_balance=10000, stop_distance=0.005, instrument=instrument)
    assert size == 60000.0


@pytest.mark.asyncio
async def test_forex_small_balance_returns_min(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("EURUSD")
    # 3% of 100 = 3. 3 / 0.005 = 600 → rounds to 1000, but min=20000
    size = await sizer.calculate(account_balance=100, stop_distance=0.005, instrument=instrument)
    assert size == 20000.0


@pytest.mark.asyncio
async def test_btc_futures_sizing(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("BTC")
    # 3% of 100000 = 3000. 3000 / (2000 * 0.1) = 15 → capped at max=10
    size = await sizer.calculate(account_balance=100000, stop_distance=2000, instrument=instrument)
    assert size == 10.0


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
    assert size == 6.0


# --- Conviction-based sizing tests ---

@pytest.mark.asyncio
async def test_conviction_high_full_risk(settings):
    """HIGH conviction uses 1.5% risk."""
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=10000, stop_distance=50,
        instrument=instrument, conviction="HIGH",
    )
    # 1.5% of 10000 = 150 / 50 = 3
    assert size == 3.0


@pytest.mark.asyncio
async def test_conviction_medium_reduced_risk(settings):
    """MEDIUM conviction uses 1.0% risk."""
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=10000, stop_distance=50,
        instrument=instrument, conviction="MEDIUM",
    )
    # 1.0% of 10000 = 100 / 50 = 2
    assert size == 2.0


@pytest.mark.asyncio
async def test_conviction_low_reduced_risk(settings):
    """LOW conviction uses 0.75% risk."""
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=10000, stop_distance=50,
        instrument=instrument, conviction="LOW",
    )
    # 0.75% of 10000 = 75 / 50 = 1.5 → rounds to 2
    assert size == 2.0


@pytest.mark.asyncio
async def test_conviction_none_uses_default(settings):
    """None conviction falls through to max_risk_percent (backward compat)."""
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=10000, stop_distance=50,
        instrument=instrument, conviction=None,
    )
    assert size == 6.0


@pytest.mark.asyncio
async def test_conviction_disabled(settings):
    """When conviction_sizing_enabled=False, always use full risk."""
    settings.conviction_sizing_enabled = False
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=10000, stop_distance=50,
        instrument=instrument, conviction="LOW",
    )
    # Should ignore conviction and use full 3%
    assert size == 6.0


@pytest.mark.asyncio
async def test_conviction_with_larger_balance(settings):
    """Test conviction scaling with larger balance where difference is visible."""
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")

    size_high = await sizer.calculate(
        account_balance=50000, stop_distance=50,
        instrument=instrument, conviction="HIGH",
    )
    size_low = await sizer.calculate(
        account_balance=50000, stop_distance=50,
        instrument=instrument, conviction="LOW",
    )

    # HIGH: 1.5% of 50000 = 750 / 50 = 15 → capped at 10
    # LOW: 0.75% of 50000 = 375 / 50 = 7.5 → rounds to 8
    assert size_high == 10.0
    assert size_low == 8.0
