from __future__ import annotations

from app.config import Settings
from app.instruments import InstrumentSpec
from app.models.schemas import TradeSubmitRequest

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.session_filter import SessionFilter


class TradeValidator:
    def __init__(self, settings: Settings, session_filter: SessionFilter | None = None):
        self.settings = settings
        self.session_filter = session_filter

    async def validate(
        self,
        request: TradeSubmitRequest,
        current_price: float,
        instrument: InstrumentSpec | None = None,
    ) -> tuple[bool, str]:
        errors: list[str] = []

        # Use instrument-specific bounds, or fall back to XAUUSD defaults
        if instrument is None:
            from app.instruments import get_instrument
            instrument = get_instrument(None)

        # Session check first — reject immediately if outside session
        if self.session_filter is not None:
            active, reason = self.session_filter.is_session_active(instrument)
            if not active:
                return False, reason

        if request.direction not in ("BUY", "SELL"):
            errors.append(f"Invalid direction: {request.direction}")

        if request.stop_distance is None and request.stop_level is None:
            errors.append("Stop loss is required")

        sd = request.stop_distance
        if sd is not None:
            if sd < instrument.min_stop_distance:
                errors.append(
                    f"Stop distance {sd} below min {instrument.min_stop_distance}"
                )
            if sd > instrument.max_stop_distance:
                errors.append(
                    f"Stop distance {sd} above max {instrument.max_stop_distance}"
                )

        # Risk:reward ratio check (minimum 1:1)
        if sd and request.limit_distance:
            rr = request.limit_distance / sd
            if rr < 1.0:
                errors.append(f"R:R ratio {rr:.2f} below minimum 1:1")

        if request.size is not None:
            if request.size > instrument.max_size:
                errors.append(
                    f"Size {request.size} exceeds max {instrument.max_size}"
                )
            if request.size < instrument.min_size:
                errors.append(
                    f"Size {request.size} below IBKR min {instrument.min_size}"
                )

        if errors:
            return False, "; ".join(errors)
        return True, "Validation passed"
