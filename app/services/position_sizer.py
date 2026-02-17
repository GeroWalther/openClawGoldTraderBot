from app.config import Settings


class PositionSizer:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def calculate(
        self,
        account_balance: float,
        stop_distance: float,
    ) -> float:
        """
        Risk-based position sizing for XAUUSD (troy ounces).

        For XAUUSD: 1 ounce = $1 per $1 move
        risk_amount = balance * risk%
        size (ounces) = risk_amount / stop_distance_usd
        """
        risk_amount = account_balance * (self.settings.max_risk_percent / 100.0)

        if stop_distance <= 0:
            return self.settings.min_position_size

        raw_size = risk_amount / stop_distance
        size = min(raw_size, self.settings.max_position_size)
        # IBKR minimum is 1 ounce, round to whole ounces
        size = max(round(size), int(self.settings.min_position_size))
        return float(size)
