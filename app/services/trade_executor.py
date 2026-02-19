import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.trade import Trade, TradeStatus
from app.models.schemas import TradeSubmitRequest, TradeSubmitResponse
from app.services.ibkr_client import IBKRClient
from app.services.position_sizer import PositionSizer
from app.services.telegram_notifier import TelegramNotifier
from app.services.trade_validator import TradeValidator

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Orchestrates: validate -> size -> execute -> log -> notify."""

    def __init__(
        self,
        ibkr_client: IBKRClient,
        validator: TradeValidator,
        sizer: PositionSizer,
        db_session: AsyncSession,
        notifier: TelegramNotifier,
        settings: Settings,
    ):
        self.ibkr = ibkr_client
        self.validator = validator
        self.sizer = sizer
        self.db = db_session
        self.notifier = notifier
        self.settings = settings

    async def submit_trade(self, request: TradeSubmitRequest) -> TradeSubmitResponse:
        # 1. Get current price
        price_data = await self.ibkr.get_gold_price()
        current_price = price_data["bid"] if request.direction == "SELL" else price_data["ask"]

        if current_price <= 0:
            current_price = price_data["last"]
        if current_price <= 0:
            return self._reject(request, "Cannot get current gold price from IBKR")

        # 2. Validate
        valid, message = await self.validator.validate(request, current_price)
        if not valid:
            trade = Trade(
                direction=request.direction,
                epic="XAUUSD",
                size=0,
                status=TradeStatus.REJECTED,
                rejection_reason=message,
                source=request.source,
                claude_reasoning=request.reasoning,
            )
            self.db.add(trade)
            await self.db.commit()
            await self.db.refresh(trade)
            await self.notifier.send_rejection(trade, message)
            return TradeSubmitResponse(
                trade_id=trade.id,
                deal_id=None,
                status=TradeStatus.REJECTED,
                direction=request.direction,
                size=0,
                stop_distance=None,
                limit_distance=None,
                message=message,
            )

        # 3. Calculate position size
        stop_distance = request.stop_distance or self.settings.default_sl_distance
        limit_distance = request.limit_distance or self.settings.default_tp_distance

        if request.size is None:
            account_info = await self.ibkr.get_account_info()
            balance = account_info.get("NetLiquidation", 10000.0)
            size = await self.sizer.calculate(balance, stop_distance)
        else:
            size = request.size

        # 4. Calculate absolute TP price and keep SL as trailing distance
        if request.direction == "BUY":
            stop_price = current_price - stop_distance  # for DB logging
            tp_price = current_price + limit_distance
        else:
            stop_price = current_price + stop_distance  # for DB logging
            tp_price = current_price - limit_distance

        # 5. Execute on IBKR (trailing stop uses distance, not absolute price)
        try:
            result = await self.ibkr.open_position(
                direction=request.direction,
                size=size,
                stop_distance=stop_distance,
                take_profit_price=tp_price,
            )
            deal_id = result.get("dealId")
            fill_price = result.get("fillPrice") or current_price
            status = (
                TradeStatus.EXECUTED
                if result.get("status") == "Filled"
                else TradeStatus.FAILED
            )
            message = f"Trade {result.get('status', 'unknown')}: order {deal_id}"
        except Exception as e:
            logger.exception("Trade execution failed")
            deal_id = None
            fill_price = current_price
            status = TradeStatus.FAILED
            message = f"Execution failed: {e}"

        # 6. Log to DB
        trade = Trade(
            deal_id=deal_id,
            direction=request.direction,
            epic="XAUUSD",
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            stop_loss=stop_price,
            take_profit=tp_price,
            entry_price=fill_price,
            status=status,
            source=request.source,
            claude_reasoning=request.reasoning,
        )
        self.db.add(trade)
        await self.db.commit()
        await self.db.refresh(trade)

        # 7. Notify via Telegram
        await self.notifier.send_trade_update(trade)

        return TradeSubmitResponse(
            trade_id=trade.id,
            deal_id=deal_id,
            status=status,
            direction=request.direction,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            message=message,
        )

    def _reject(self, request: TradeSubmitRequest, reason: str) -> TradeSubmitResponse:
        return TradeSubmitResponse(
            trade_id=0,
            deal_id=None,
            status=TradeStatus.REJECTED,
            direction=request.direction,
            size=0,
            stop_distance=None,
            limit_distance=None,
            message=reason,
        )
