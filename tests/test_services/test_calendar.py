"""Tests for economic calendar service."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from app.services.calendar import CalendarService, INSTRUMENT_COUNTRIES


def _make_event(title: str, country: str, impact: str, hours_from_now: float) -> dict:
    """Create a mock calendar event at a specific time offset."""
    dt = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    # Convert UTC to ET (UTC-5) for ForexFactory format
    et_dt = dt - timedelta(hours=5)
    return {
        "title": title,
        "country": country,
        "impact": impact,
        "date": et_dt.strftime("%Y-%m-%d"),
        "time": et_dt.strftime("%-I:%M%p").lower(),
    }


class TestCalendarService:
    @pytest.mark.asyncio
    async def test_imminent_event_scores_minus_2(self):
        """Event within 4h should score -2."""
        service = CalendarService()
        events = [_make_event("NFP", "USD", "High", 2)]

        with patch.object(service, "_fetch_events", return_value=events):
            result = await service.get_calendar_risk("XAUUSD")

        assert result["score"] == -2
        assert len(result["events"]) == 1

    @pytest.mark.asyncio
    async def test_upcoming_event_scores_minus_1(self):
        """Event 4-12h away should score -1."""
        service = CalendarService()
        events = [_make_event("CPI", "USD", "High", 8)]

        with patch.object(service, "_fetch_events", return_value=events):
            result = await service.get_calendar_risk("XAUUSD")

        assert result["score"] == -1

    @pytest.mark.asyncio
    async def test_distant_event_scores_zero(self):
        """Event >12h away should score 0 (no penalty)."""
        service = CalendarService()
        events = [_make_event("GDP", "USD", "High", 24)]

        with patch.object(service, "_fetch_events", return_value=events):
            result = await service.get_calendar_risk("XAUUSD")

        assert result["score"] == 0

    @pytest.mark.asyncio
    async def test_no_events_scores_zero(self):
        """No high-impact events should score 0 (neutral)."""
        service = CalendarService()

        with patch.object(service, "_fetch_events", return_value=[]):
            result = await service.get_calendar_risk("XAUUSD")

        assert result["score"] == 0

    @pytest.mark.asyncio
    async def test_filters_by_currency(self):
        """EURUSD should match EUR and USD events, not JPY."""
        service = CalendarService()
        events = [
            _make_event("ECB Rate", "EUR", "High", 2),
            _make_event("BOJ Rate", "JPY", "High", 2),
        ]

        with patch.object(service, "_fetch_events", return_value=events):
            result = await service.get_calendar_risk("EURUSD")

        assert len(result["events"]) == 1
        assert result["events"][0]["country"] == "EUR"

    @pytest.mark.asyncio
    async def test_ignores_low_impact(self):
        """Low/medium impact events should be ignored."""
        service = CalendarService()
        events = [
            _make_event("Some Data", "USD", "Low", 2),
            _make_event("Some Other", "USD", "Medium", 2),
        ]

        with patch.object(service, "_fetch_events", return_value=events):
            result = await service.get_calendar_risk("XAUUSD")

        assert result["score"] == 0  # No high-impact = neutral
        assert len(result["events"]) == 0

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_zero(self):
        """Fetch failure should return score 0 with error."""
        service = CalendarService()

        with patch.object(service, "_fetch_events", return_value=None):
            result = await service.get_calendar_risk("XAUUSD")

        assert result["score"] == 0
        assert "error" in result

    @pytest.mark.asyncio
    async def test_caching(self):
        """Second call within TTL should use cache."""
        service = CalendarService()
        events = [_make_event("NFP", "USD", "High", 2)]
        service._cache = (events, __import__("time").monotonic())

        # Should return cached data without fetching
        result = await service.get_calendar_risk("XAUUSD")
        assert result["score"] == -2

    @pytest.mark.asyncio
    async def test_stale_cache_fallback(self):
        """If fetch fails, should fall back to stale cache."""
        service = CalendarService()
        events = [_make_event("NFP", "USD", "High", 2)]
        # Set cache with old timestamp so it's stale
        service._cache = (events, 0)

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPError("timeout")

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.side_effect = httpx.HTTPError("timeout")
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance
            result = await service.get_calendar_risk("XAUUSD")

        # Should still work with stale cache
        assert result["score"] == -2


class TestInstrumentCountries:
    def test_all_instruments_mapped(self):
        """All instruments should have country mappings."""
        from app.instruments import INSTRUMENTS
        for key in INSTRUMENTS:
            assert key in INSTRUMENT_COUNTRIES, f"Missing country mapping for {key}"

    def test_xauusd_is_usd(self):
        assert INSTRUMENT_COUNTRIES["XAUUSD"] == ["USD"]

    def test_eurusd_has_both(self):
        assert "USD" in INSTRUMENT_COUNTRIES["EURUSD"]
        assert "EUR" in INSTRUMENT_COUNTRIES["EURUSD"]

    def test_eurjpy_has_both(self):
        assert "EUR" in INSTRUMENT_COUNTRIES["EURJPY"]
        assert "JPY" in INSTRUMENT_COUNTRIES["EURJPY"]
