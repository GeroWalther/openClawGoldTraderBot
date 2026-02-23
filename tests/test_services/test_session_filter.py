import pytest
from datetime import datetime, timezone

from app.instruments import get_instrument
from app.services.session_filter import SessionFilter


@pytest.mark.asyncio
async def test_xauusd_in_london_session(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("XAUUSD")
    # 10:00 UTC is within London session (07-16)
    now = datetime(2024, 3, 15, 10, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True
    assert "London" in reason


@pytest.mark.asyncio
async def test_xauusd_in_ny_session(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("XAUUSD")
    # 15:00 UTC is within NY session (13-21) and London overlap
    now = datetime(2024, 3, 15, 15, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True


@pytest.mark.asyncio
async def test_xauusd_outside_sessions(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("XAUUSD")
    # 03:00 UTC is outside all sessions
    now = datetime(2024, 3, 15, 3, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is False
    assert "outside active sessions" in reason


@pytest.mark.asyncio
async def test_btc_24_7(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("BTC")
    # Any weekday hour should work
    now = datetime(2024, 3, 15, 3, 0, tzinfo=timezone.utc)  # Friday 03:00
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True
    assert "24/7" in reason


@pytest.mark.asyncio
async def test_btc_weekend_warning(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("BTC")
    # Saturday
    now = datetime(2024, 3, 16, 10, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True
    assert "weekend" in reason.lower()


@pytest.mark.asyncio
async def test_eurjpy_tokyo_session(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("EURJPY")
    # 05:00 UTC is within Tokyo session (00-09)
    now = datetime(2024, 3, 15, 5, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True
    assert "Tokyo" in reason


@pytest.mark.asyncio
async def test_disabled_filter(settings):
    settings.session_filter_enabled = False
    sf = SessionFilter(settings)
    instrument = get_instrument("XAUUSD")
    # 03:00 UTC would normally be outside sessions
    now = datetime(2024, 3, 15, 3, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True
    assert "disabled" in reason.lower()


@pytest.mark.asyncio
async def test_eurusd_london_ny_session(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("EURUSD")
    # 14:00 UTC is within London+NY session (07-21)
    now = datetime(2024, 3, 15, 14, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True


@pytest.mark.asyncio
async def test_mes_us_market(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("MES")
    # 15:00 UTC is within US Market session (13-20)
    now = datetime(2024, 3, 15, 15, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True


@pytest.mark.asyncio
async def test_mes_outside_hours(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("MES")
    # 05:00 UTC is outside US Market hours
    now = datetime(2024, 3, 15, 5, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is False


@pytest.mark.asyncio
async def test_cadjpy_tokyo_session(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("CADJPY")
    # 05:00 UTC is within Tokyo session (00-09)
    now = datetime(2024, 3, 15, 5, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True
    assert "Tokyo" in reason


@pytest.mark.asyncio
async def test_cadjpy_london_ny_session(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("CADJPY")
    # 14:00 UTC is within London+NY session (07-21)
    now = datetime(2024, 3, 15, 14, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True


@pytest.mark.asyncio
async def test_usdjpy_tokyo_session(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("USDJPY")
    # 03:00 UTC is within Tokyo session (00-09)
    now = datetime(2024, 3, 15, 3, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True
    assert "Tokyo" in reason


@pytest.mark.asyncio
async def test_usdjpy_london_ny_session(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("USDJPY")
    # 15:00 UTC is within London+NY session (07-21)
    now = datetime(2024, 3, 15, 15, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is True


@pytest.mark.asyncio
async def test_cadjpy_outside_sessions(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("CADJPY")
    # 22:00 UTC is outside all sessions (Tokyo 00-09, London+NY 07-21)
    now = datetime(2024, 3, 15, 22, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is False
    assert "outside active sessions" in reason


@pytest.mark.asyncio
async def test_usdjpy_outside_sessions(settings):
    sf = SessionFilter(settings)
    instrument = get_instrument("USDJPY")
    # 22:00 UTC is outside all sessions
    now = datetime(2024, 3, 15, 22, 0, tzinfo=timezone.utc)
    active, reason = sf.is_session_active(instrument, now=now)
    assert active is False
    assert "outside active sessions" in reason
