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
    ) -> float:
        """
        Risk-based position sizing.

        Formula: size = risk_amount / (stop_distance * multiplier)
        Rounding: whole numbers for FUT/CMDTY, nearest 1000 for CASH.
        """
        if instrument is None:
            from app.instruments import get_instrument
            instrument = get_instrument(None)

        risk_amount = account_balance * (self.settings.max_risk_percent / 100.0)

        if stop_distance <= 0:
            return instrument.min_size

        raw_size = risk_amount / (stop_distance * instrument.multiplier)
        size = min(raw_size, instrument.max_size)

        # Rounding depends on instrument type
        if instrument.sec_type == "CRYPTO":
            # Round to 4 decimal places for crypto
            size = max(round(size, 4), instrument.min_size)
        elif instrument.sec_type == "CASH":
            # Round to nearest 1000 for forex
            size = max(round(size / 1000) * 1000, instrument.min_size)
        else:
            # Whole numbers for futures, commodities, CFDs
            size = max(round(size), int(instrument.min_size))

        return float(size)
