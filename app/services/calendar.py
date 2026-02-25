"""Economic calendar service — fetches ForexFactory calendar and scores risk.

Pure risk filter — can only subtract, never add. Absence of risk is neutral (0).

Scoring:
  High-impact event within 4h  → -2
  High-impact event 4-12h away → -1
  High-impact event >12h away  →  0  (no penalty, no bonus)
  No high-impact events        →  0  (neutral)
"""

import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Map instrument keys to relevant currency codes
INSTRUMENT_COUNTRIES: dict[str, list[str]] = {
    "XAUUSD": ["USD"],
    "MES": ["USD"],
    "IBUS500": ["USD"],
    "EURUSD": ["USD", "EUR"],
    "EURJPY": ["EUR", "JPY"],
    "CADJPY": ["CAD", "JPY"],
    "USDJPY": ["USD", "JPY"],
    "BTC": ["USD"],
}


class CalendarService:
    """Fetches economic calendar and scores event risk for instruments."""

    def __init__(self):
        self._cache: tuple[list[dict], float] | None = None
        self._cache_ttl = 3600  # 1 hour

    async def get_calendar_risk(self, instrument_key: str) -> dict:
        """Return calendar risk score and upcoming events for an instrument.

        Returns:
            dict with score (-2..+2), events list, and hours_to_next.
        """
        key = instrument_key.upper()
        currencies = INSTRUMENT_COUNTRIES.get(key, ["USD"])

        events = await self._fetch_events()
        if events is None:
            return {"score": 0, "events": [], "hours_to_next": None, "error": "fetch_failed"}

        now = datetime.now(timezone.utc)
        relevant = []

        for event in events:
            if event.get("impact", "").lower() != "high":
                continue
            # Match by currency
            event_country = event.get("country", "")
            if event_country not in currencies:
                continue

            # Parse event datetime
            event_dt = self._parse_event_time(event)
            if event_dt is None:
                continue

            hours_away = (event_dt - now).total_seconds() / 3600
            # Only care about events in the future or within last 1h (just happened)
            if hours_away < -1:
                continue

            relevant.append({
                "title": event.get("title", "Unknown"),
                "country": event_country,
                "date": event.get("date", ""),
                "time": event.get("time", ""),
                "hours_away": round(hours_away, 1),
                "impact": "high",
            })

        # Sort by proximity
        relevant.sort(key=lambda e: abs(e["hours_away"]))

        # Score based on nearest event — pure risk filter (0 to -2)
        if not relevant:
            score = 0  # No high-impact events = neutral
            hours_to_next = None
        else:
            nearest = relevant[0]["hours_away"]
            hours_to_next = round(nearest, 1)
            if nearest <= 4:
                score = -2  # Imminent event — dangerous
            elif nearest <= 12:
                score = -1  # Approaching — be cautious
            else:
                score = 0  # Far enough — no penalty

        return {
            "score": score,
            "events": relevant[:5],  # Top 5 nearest
            "hours_to_next": hours_to_next,
        }

    async def _fetch_events(self) -> list[dict] | None:
        """Fetch calendar events with caching and stale fallback."""
        now = time.monotonic()

        if self._cache is not None:
            data, ts = self._cache
            if now - ts < self._cache_ttl:
                return data

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(CALENDAR_URL)
                resp.raise_for_status()
                events = resp.json()
                self._cache = (events, now)
                return events
        except Exception as e:
            logger.warning("Calendar fetch failed: %s", e)
            # Stale cache fallback
            if self._cache is not None:
                logger.info("Using stale calendar cache")
                return self._cache[0]
            return None

    @staticmethod
    def _parse_event_time(event: dict) -> datetime | None:
        """Parse event date + time into UTC datetime."""
        date_str = event.get("date", "")
        time_str = event.get("time", "")

        if not date_str:
            return None

        # ForexFactory format: "2024-01-15" + "8:30am" (ET = UTC-5)
        try:
            if time_str and time_str.lower() not in ("", "all day", "tentative"):
                # Normalize time: "8:30am" -> "08:30AM"
                t = time_str.strip().upper()
                dt_str = f"{date_str} {t}"
                for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p"):
                    try:
                        dt = datetime.strptime(dt_str, fmt)
                        # Convert ET (UTC-5) to UTC
                        from datetime import timedelta
                        dt = dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)
                        return dt
                    except ValueError:
                        continue
                # If time parsing fails, treat as start of day
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                return dt.replace(hour=13, minute=0, tzinfo=timezone.utc)
            else:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                return dt.replace(hour=13, minute=0, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
