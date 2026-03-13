import logging

from app.config import Settings
from app.instruments import InstrumentSpec

logger = logging.getLogger(__name__)

# Approximate leverage ratios for margin estimation
_LEVERAGE = {
    "CASH": 30,     # 1:30 for major forex (retail)
    "CFD": 20,      # 1:20 for indices/commodities CFD
    "CMDTY": 20,
    "FUT": 15,
}


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

        Returns 0 if account balance is too small for the minimum position size
        (prevents NOT_ENOUGH_MONEY errors on small accounts).

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
        elif instrument.min_size < 1:
            # Fractional sizing (e.g. BTC CFD with min_size=0.01)
            step = instrument.min_size
            size = max(round(size / step) * step, instrument.min_size)
            size = round(size, 8)  # Avoid float precision issues
        else:
            # Whole numbers for futures, commodities
            size = max(round(size), int(instrument.min_size))

        # Apply max position size cap if configured
        if self.settings.max_position_size > 0 and size > self.settings.max_position_size:
            logger.info(
                "Position capped by max_position_size: %.0f → %.0f %s",
                size, self.settings.max_position_size, instrument.key,
            )
            size = self.settings.max_position_size

        # Margin check: verify account can afford the position.
        # Prevents NOT_ENOUGH_MONEY errors.
        if account_balance > 0:
            leverage = _LEVERAGE.get(instrument.sec_type, 20)
            estimated_margin = size / leverage
            # Cap size to what the account can afford (80% of balance as safety)
            max_affordable = account_balance * 0.8 * leverage
            if size > max_affordable:
                if instrument.sec_type == "CASH":
                    size = max(round(max_affordable / 1000) * 1000, instrument.min_size)
                elif instrument.min_size < 1:
                    step = instrument.min_size
                    size = max(round(max_affordable / step) * step, instrument.min_size)
                    size = round(size, 8)
                else:
                    size = max(round(max_affordable), int(instrument.min_size))
                logger.info(
                    "Position capped by margin: balance $%.2f, max affordable %.0f, using %.0f %s",
                    account_balance, max_affordable, size, instrument.key,
                )
            # Final check: can we afford even min_size?
            min_margin = instrument.min_size / leverage
            if min_margin > account_balance * 0.8:
                logger.warning(
                    "Position size 0: balance $%.2f too small for min_size %.0f %s "
                    "(estimated margin $%.2f)",
                    account_balance, instrument.min_size, instrument.key, min_margin,
                )
                return 0.0

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
