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
async def test_forex_small_balance_returns_zero(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("EURUSD")
    # 3% of 100 = 3. 3 / 0.005 = 600 → rounds to 1000, but min=20000
    # Margin for 20000 EURUSD at 1:30 ≈ $667 > 80% of $100 → returns 0 (insufficient)
    size = await sizer.calculate(account_balance=100, stop_distance=0.005, instrument=instrument)
    assert size == 0.0


@pytest.mark.asyncio
async def test_forex_min_size_exceeds_risk_returns_zero(settings):
    """1000 NZDUSD × 0.003 stop = $3 loss > 3% of $50 = $1.50 budget → skip."""
    sizer = PositionSizer(settings)
    instrument = get_instrument("NZDUSD")
    size = await sizer.calculate(account_balance=50, stop_distance=0.003, instrument=instrument)
    assert size == 0.0


@pytest.mark.asyncio
async def test_forex_min_size_within_risk(settings):
    """$500 × 3% = $15 budget, raw_size = $15 / 0.003 = 5000 units (rounded to 1000)."""
    sizer = PositionSizer(settings)
    instrument = get_instrument("NZDUSD")
    size = await sizer.calculate(account_balance=500, stop_distance=0.003, instrument=instrument)
    assert size == 5000.0


@pytest.mark.asyncio
async def test_btc_cfd_sizing(settings):
    sizer = PositionSizer(settings)
    instrument = get_instrument("BTC")
    # BTC CFD: multiplier=1, min_size=0.01
    # 3% of 100000 = 3000. 3000 / (2000 * 1) = 1.5 → rounded to 0.01 step = 1.5
    size = await sizer.calculate(account_balance=100000, stop_distance=2000, instrument=instrument)
    assert size == 1.5


@pytest.mark.asyncio
async def test_btc_min_size_exceeds_risk_returns_zero(settings):
    """0.01 BTC × $50000 stop = $500 loss > 3% of $100 = $3 budget → skip."""
    sizer = PositionSizer(settings)
    instrument = get_instrument("BTC")
    size = await sizer.calculate(account_balance=100, stop_distance=50000, instrument=instrument)
    assert size == 0.0


@pytest.mark.asyncio
async def test_defaults_to_xauusd(settings):
    sizer = PositionSizer(settings)
    size = await sizer.calculate(account_balance=10000, stop_distance=50)
    assert size == 6.0


# --- Conviction-based sizing tests ---

@pytest.mark.asyncio
async def test_conviction_high_full_risk(settings):
    """HIGH conviction uses configured high_risk_pct."""
    settings.conviction_high_risk_pct = 3.0
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=10000, stop_distance=50,
        instrument=instrument, conviction="HIGH",
    )
    # 3.0% of 10000 = 300 / 50 = 6
    assert size == 6.0


@pytest.mark.asyncio
async def test_conviction_medium_reduced_risk(settings):
    """MEDIUM conviction uses its own risk_pct independent of HIGH."""
    settings.conviction_medium_risk_pct = 2.0
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=10000, stop_distance=50,
        instrument=instrument, conviction="MEDIUM",
    )
    # 2.0% of 10000 = 200 / 50 = 4
    assert size == 4.0


@pytest.mark.asyncio
async def test_conviction_low_reduced_risk(settings):
    """LOW conviction uses its own risk_pct."""
    settings.conviction_low_risk_pct = 1.0
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=10000, stop_distance=50,
        instrument=instrument, conviction="LOW",
    )
    # 1.0% of 10000 = 100 / 50 = 2
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
    # 3.0% of 10000 = 300 / 50 = 6
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
async def test_min_size_risk_floor_returns_zero(settings):
    """Skip trade when broker's min_size at this stop would exceed risk budget.

    Reproduces the €300 account / XAUUSD bug: 0.25% risk = $0.75,
    but XAUUSD min lot (1 oz) × $36 stop = $36 loss → 12% of account.
    Sizer must return 0 to refuse the trade.
    """
    settings.conviction_sizing_enabled = True
    settings.conviction_medium_risk_pct = 0.25
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")
    size = await sizer.calculate(
        account_balance=330, stop_distance=36.73,
        instrument=instrument, conviction="MEDIUM",
    )
    assert size == 0.0


@pytest.mark.asyncio
async def test_conviction_with_larger_balance(settings):
    """Conviction scaling produces different sizes on larger balances."""
    settings.conviction_high_risk_pct = 3.0
    settings.conviction_low_risk_pct = 1.0
    sizer = PositionSizer(settings)
    instrument = get_instrument("XAUUSD")

    size_high = await sizer.calculate(
        account_balance=50000, stop_distance=100,
        instrument=instrument, conviction="HIGH",
    )
    size_low = await sizer.calculate(
        account_balance=50000, stop_distance=100,
        instrument=instrument, conviction="LOW",
    )

    # HIGH: 3.0% of 50000 = 1500 / 100 = 15 → capped at 10 (max_size)
    # LOW:  1.0% of 50000 = 500 / 100 = 5
    assert size_high == 10.0
    assert size_low == 5.0
