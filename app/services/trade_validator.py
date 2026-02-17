from app.config import Settings
from app.models.schemas import TradeSubmitRequest


class TradeValidator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.min_stop_distance = 5.0  # USD per ounce
        self.max_stop_distance = 300.0  # USD per ounce

    async def validate(
        self, request: TradeSubmitRequest, current_price: float
    ) -> tuple[bool, str]:
        errors: list[str] = []

        if request.direction not in ("BUY", "SELL"):
            errors.append(f"Invalid direction: {request.direction}")

        if request.stop_distance is None and request.stop_level is None:
            errors.append("Stop loss is required")

        sd = request.stop_distance
        if sd is not None:
            if sd < self.min_stop_distance:
                errors.append(
                    f"Stop distance ${sd} below min ${self.min_stop_distance}"
                )
            if sd > self.max_stop_distance:
                errors.append(
                    f"Stop distance ${sd} above max ${self.max_stop_distance}"
                )

        # Risk:reward ratio check (minimum 1:1)
        if sd and request.limit_distance:
            rr = request.limit_distance / sd
            if rr < 1.0:
                errors.append(f"R:R ratio {rr:.2f} below minimum 1:1")

        if request.size is not None:
            if request.size > self.settings.max_position_size:
                errors.append(
                    f"Size {request.size}oz exceeds max {self.settings.max_position_size}oz"
                )
            if request.size < self.settings.min_position_size:
                errors.append(
                    f"Size {request.size}oz below IBKR min {self.settings.min_position_size}oz"
                )

        if errors:
            return False, "; ".join(errors)
        return True, "Validation passed"
