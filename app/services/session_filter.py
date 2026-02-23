import logging
from datetime import datetime, timezone

from app.config import Settings
from app.instruments import InstrumentSpec

logger = logging.getLogger(__name__)


class SessionFilter:
    """Checks whether trading is allowed based on instrument session hours."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def is_session_active(
        self, instrument: InstrumentSpec, now: datetime | None = None
    ) -> tuple[bool, str]:
        """
        Check if current time falls within an active trading session.

        Returns (is_active, reason_string).
        """
        if not self.settings.session_filter_enabled:
            return True, "Session filter disabled"

        if now is None:
            now = datetime.now(timezone.utc)

        current_hour = now.hour

        # BTC: 24/7 but warn on weekends
        if not instrument.trading_sessions:
            if instrument.warn_low_liquidity and now.weekday() >= 5:
                return True, f"Warning: {instrument.key} weekend — low liquidity expected"
            return True, f"{instrument.key} trades 24/7"

        # Check each session
        for session in instrument.trading_sessions:
            if self._hour_in_range(current_hour, session.start_hour_utc, session.end_hour_utc):
                return True, f"{instrument.key} in {session.name} session ({session.start_hour_utc:02d}-{session.end_hour_utc:02d} UTC)"

        session_names = ", ".join(
            f"{s.name} ({s.start_hour_utc:02d}-{s.end_hour_utc:02d} UTC)"
            for s in instrument.trading_sessions
        )
        return False, f"{instrument.key} outside active sessions. Sessions: {session_names}. Current: {current_hour:02d}:00 UTC"

    @staticmethod
    def _hour_in_range(hour: int, start: int, end: int) -> bool:
        """Check if hour is within [start, end). Handles midnight wrap."""
        if start <= end:
            return start <= hour < end
        # Wraps past midnight (e.g., 22-06)
        return hour >= start or hour < end
