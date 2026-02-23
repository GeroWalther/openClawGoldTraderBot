from app.config import Settings
from app.instruments import InstrumentSpec


class PositionSizer:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def calculate(
        self,
        account_balance: float,
        stop_distance: float,
        instrument: InstrumentSpec | None = None,
        conviction: str | None = None,
    ) -> float:
        """
        Risk-based position sizing.

        Formula: size = risk_amount / (stop_distance * multiplier)
        Rounding: whole numbers for FUT/CMDTY, nearest 1000 for CASH.

        Conviction scaling (when enabled):
          HIGH   → full risk% (default 1.0%)
          MEDIUM → 0.75× risk%
          LOW    → 0.50× risk%
          None   → full risk% (backward compatible)
        """
        if instrument is None:
            from app.instruments import get_instrument
            instrument = get_instrument(None)

        risk_pct = self._get_risk_percent(conviction)
        risk_amount = account_balance * (risk_pct / 100.0)

        if stop_distance <= 0:
            return instrument.min_size

        raw_size = risk_amount / (stop_distance * instrument.multiplier)
        size = min(raw_size, instrument.max_size)

        # Rounding depends on instrument type
        if instrument.sec_type == "CASH":
            # Round to nearest 1000 for forex
            size = max(round(size / 1000) * 1000, instrument.min_size)
        else:
            # Whole numbers for futures, commodities, CFDs
            size = max(round(size), int(instrument.min_size))

        return float(size)

    def _get_risk_percent(self, conviction: str | None) -> float:
        """Map conviction level to risk percentage."""
        if not self.settings.conviction_sizing_enabled or conviction is None:
            return self.settings.max_risk_percent

        conviction_upper = conviction.upper()
        if conviction_upper == "HIGH":
            return self.settings.conviction_high_risk_pct
        elif conviction_upper == "MEDIUM":
            return self.settings.conviction_medium_risk_pct
        elif conviction_upper == "LOW":
            return self.settings.conviction_low_risk_pct
        return self.settings.max_risk_percent
