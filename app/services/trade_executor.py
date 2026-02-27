from __future__ import annotations

import logging
import math

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.instruments import get_instrument
from app.models.trade import Trade, TradeStatus
from app.models.schemas import TradeSubmitRequest, TradeSubmitResponse
from app.services.ibkr_client import IBKRClient
from app.services.position_sizer import PositionSizer
from app.services.telegram_notifier import TelegramNotifier
from app.services.trade_validator import TradeValidator

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.atr_calculator import ATRCalculator
    from app.services.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Orchestrates: risk check -> price -> validate -> ATR -> size -> execute -> log -> notify."""

    def __init__(
        self,
        ibkr_client: IBKRClient,
        validator: TradeValidator,
        sizer: PositionSizer,
        db_session: AsyncSession,
        notifier: TelegramNotifier,
        settings: Settings,
        risk_manager: RiskManager | None = None,
        atr_calculator: ATRCalculator | None = None,
    ):
        self.ibkr = ibkr_client
        self.validator = validator
        self.sizer = sizer
        self.db = db_session
        self.notifier = notifier
        self.settings = settings
        self.risk_manager = risk_manager
        self.atr_calculator = atr_calculator

    async def submit_trade(self, request: TradeSubmitRequest) -> TradeSubmitResponse:
        # 0. Resolve instrument and order type
        instrument = get_instrument(request.instrument)
        order_type = (request.order_type or "MARKET").upper()
        is_pending = order_type in ("LIMIT", "STOP")

        # 0a. Validate entry_price for pending orders
        if is_pending and request.entry_price is None:
            return self._reject(
                request, instrument.key,
                f"entry_price is required for {order_type} orders",
            )

        # 1. Risk manager check (cooldown + daily limits)
        if self.risk_manager is not None:
            account_info = await self.ibkr.get_account_info()
            balance = account_info.get("NetLiquidation", 10000.0)
            can_trade, reason = await self.risk_manager.can_trade(self.db, balance)
            if not can_trade:
                return self._reject(request, instrument.key, reason)

        # 2. Get current price + record spread
        price_data = await self.ibkr.get_price(instrument.key)
        bid = price_data["bid"]
        ask = price_data["ask"]
        current_price = bid if request.direction == "SELL" else ask
        spread = (ask - bid) if (bid > 0 and ask > 0) else None

        if current_price <= 0:
            current_price = price_data["last"]
        if current_price <= 0:
            return self._reject(request, instrument.key, f"Cannot get current {instrument.display_name} price from IBKR")

        # Spread protection — reject if spread is too wide relative to stop
        if spread is not None and spread > 0 and request.stop_distance is not None and request.stop_distance > 0:
            spread_ratio = spread / request.stop_distance
            if spread_ratio > self.settings.max_spread_to_sl_ratio:
                return self._reject(
                    request, instrument.key,
                    f"Spread too wide: {spread:.2f} = {spread_ratio:.0%} of SL distance {request.stop_distance} "
                    f"(max {self.settings.max_spread_to_sl_ratio:.0%})",
                )

        # 2a. Validate entry_price direction for pending orders
        if is_pending:
            entry_price = request.entry_price
            if order_type == "LIMIT":
                if request.direction == "BUY" and entry_price >= current_price:
                    return self._reject(
                        request, instrument.key,
                        f"BUY LIMIT entry_price ({entry_price}) must be below current price ({current_price})",
                    )
                if request.direction == "SELL" and entry_price <= current_price:
                    return self._reject(
                        request, instrument.key,
                        f"SELL LIMIT entry_price ({entry_price}) must be above current price ({current_price})",
                    )
            elif order_type == "STOP":
                if request.direction == "BUY" and entry_price <= current_price:
                    return self._reject(
                        request, instrument.key,
                        f"BUY STOP entry_price ({entry_price}) must be above current price ({current_price})",
                    )
                if request.direction == "SELL" and entry_price >= current_price:
                    return self._reject(
                        request, instrument.key,
                        f"SELL STOP entry_price ({entry_price}) must be below current price ({current_price})",
                    )

        # For pending orders, SL/TP are calculated from entry_price
        reference_price = request.entry_price if is_pending else current_price
        expected_price = reference_price

        # 2b. Pre-fill ATR/default SL/TP so validator sees actual values
        need_sd = request.stop_distance is None and request.stop_level is None
        need_ld = request.limit_distance is None and request.limit_level is None
        if need_sd or need_ld:
            atr_sl, atr_tp = None, None
            if self.atr_calculator is not None:
                atr_result = self.atr_calculator.get_dynamic_sl_tp(instrument)
                if atr_result is not None:
                    atr_sl, atr_tp = atr_result
            updates = {}
            if need_sd:
                updates["stop_distance"] = atr_sl or instrument.default_sl_distance
            if need_ld:
                updates["limit_distance"] = atr_tp or instrument.default_tp_distance
            request = request.model_copy(update=updates)

        # 3. Validate (now includes session check)
        valid, message = await self.validator.validate(
            request, current_price, instrument,
            entry_price=request.entry_price if is_pending else None,
        )
        if not valid:
            trade = Trade(
                direction=request.direction,
                epic=instrument.key,
                size=0,
                status=TradeStatus.REJECTED,
                rejection_reason=message,
                source=request.source,
                claude_reasoning=request.reasoning,
                conviction=request.conviction,
                order_type=order_type,
                strategy=request.strategy,
            )
            self.db.add(trade)
            await self.db.commit()
            await self.db.refresh(trade)
            await self.notifier.send_rejection(trade, message)
            return TradeSubmitResponse(
                trade_id=trade.id,
                deal_id=None,
                instrument=instrument.key,
                status=TradeStatus.REJECTED,
                direction=request.direction,
                size=0,
                stop_distance=None,
                limit_distance=None,
                conviction=request.conviction,
                spread_at_entry=spread,
                order_type=order_type,
                entry_price=request.entry_price,
                strategy=request.strategy,
                message=message,
            )

        # 4. Convert absolute stop_level/limit_level to distances
        stop_distance = request.stop_distance
        limit_distance = request.limit_distance

        if stop_distance is None and request.stop_level is not None:
            sl_dist = abs(reference_price - request.stop_level)
            # Validate level is on the correct side
            if request.direction == "BUY" and request.stop_level >= reference_price:
                return self._reject(
                    request, instrument.key,
                    f"BUY stop_level ({request.stop_level}) must be below reference price ({reference_price})",
                )
            if request.direction == "SELL" and request.stop_level <= reference_price:
                return self._reject(
                    request, instrument.key,
                    f"SELL stop_level ({request.stop_level}) must be above reference price ({reference_price})",
                )
            stop_distance = round(sl_dist, 2)

        if limit_distance is None and request.limit_level is not None:
            tp_dist = abs(request.limit_level - reference_price)
            # Validate level is on the correct side
            if request.direction == "BUY" and request.limit_level <= reference_price:
                return self._reject(
                    request, instrument.key,
                    f"BUY limit_level ({request.limit_level}) must be above reference price ({reference_price})",
                )
            if request.direction == "SELL" and request.limit_level >= reference_price:
                return self._reject(
                    request, instrument.key,
                    f"SELL limit_level ({request.limit_level}) must be below reference price ({reference_price})",
                )
            limit_distance = round(tp_dist, 2)

        # ATR-based SL/TP defaults if user didn't specify distances
        if (stop_distance is None or limit_distance is None) and self.atr_calculator is not None:
            atr_result = self.atr_calculator.get_dynamic_sl_tp(instrument)
            if atr_result is not None:
                atr_sl, atr_tp = atr_result
                if stop_distance is None:
                    stop_distance = atr_sl
                if limit_distance is None:
                    limit_distance = atr_tp

        # Fall back to instrument defaults
        stop_distance = stop_distance or instrument.default_sl_distance
        limit_distance = limit_distance or instrument.default_tp_distance

        # 5. Conviction-based position sizing
        if request.size is None:
            if self.risk_manager is None:
                account_info = await self.ibkr.get_account_info()
            balance = account_info.get("NetLiquidation", 10000.0)
            size = await self.sizer.calculate(balance, stop_distance, instrument, conviction=request.conviction)
        else:
            size = request.size

        # 6. Calculate absolute TP price and SL price from reference_price
        if request.direction == "BUY":
            stop_price = reference_price - stop_distance
            tp_price = reference_price + limit_distance
        else:
            stop_price = reference_price + stop_distance
            tp_price = reference_price - limit_distance

        # Sanity check
        if any(math.isnan(v) or math.isinf(v) for v in (stop_price, tp_price, stop_distance)):
            return self._reject(
                request, instrument.key,
                f"Invalid price calculation (price={reference_price}, sd={stop_distance}, tp={tp_price})"
            )

        # 7. Execute — pending vs market
        try:
            if is_pending:
                # Pending order path
                if self.settings.partial_tp_enabled and self._can_split(size, instrument):
                    result = await self._execute_pending_partial_tp(
                        request.direction, size, request.entry_price, order_type,
                        stop_price, tp_price, stop_distance, instrument,
                    )
                else:
                    result = await self.ibkr.open_pending_position(
                        direction=request.direction,
                        size=size,
                        entry_price=request.entry_price,
                        order_type=order_type,
                        stop_price=stop_price,
                        take_profit_price=tp_price,
                        instrument_key=instrument.key,
                    )
                deal_id = result.get("dealId")
                status = TradeStatus.PENDING_ORDER
                message = f"Pending {order_type} order placed: order {deal_id}"
            else:
                # Market order path (existing behavior)
                if self.settings.partial_tp_enabled and self._can_split(size, instrument):
                    if request.strategy == "m5_scalp":
                        result = await self._execute_runner(
                            request.direction, size, stop_price,
                            stop_distance, instrument,
                        )
                    else:
                        result = await self._execute_partial_tp(
                            request.direction, size, stop_price, tp_price,
                            stop_distance, instrument,
                        )
                else:
                    result = await self.ibkr.open_position(
                        direction=request.direction,
                        size=size,
                        stop_distance=stop_distance,
                        take_profit_price=tp_price,
                        instrument_key=instrument.key,
                        stop_price=stop_price,
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
            fill_price = current_price if not is_pending else None
            status = TradeStatus.FAILED
            message = f"Execution failed: {e}"

        # 8. Log to DB
        if is_pending:
            db_entry_price = request.entry_price
        else:
            db_entry_price = fill_price
        # Runner trades (m5_scalp) have no fixed TP2 — monitor trails the SL
        db_take_profit = None if (request.strategy == "m5_scalp" and not is_pending) else tp_price
        trade = Trade(
            deal_id=deal_id,
            direction=request.direction,
            epic=instrument.key,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            stop_loss=stop_price,
            take_profit=db_take_profit,
            entry_price=db_entry_price,
            status=status,
            source=request.source,
            claude_reasoning=request.reasoning,
            conviction=request.conviction,
            expected_price=expected_price,
            actual_price=db_entry_price,
            spread_at_entry=spread,
            order_type=order_type,
            strategy=request.strategy,
        )
        self.db.add(trade)
        await self.db.commit()
        await self.db.refresh(trade)

        # 9. Notify via Telegram
        if is_pending:
            await self.notifier.send_pending_order_update(trade)
        else:
            await self.notifier.send_trade_update(trade)

        return TradeSubmitResponse(
            trade_id=trade.id,
            deal_id=deal_id,
            instrument=instrument.key,
            status=status,
            direction=request.direction,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            conviction=request.conviction,
            spread_at_entry=spread,
            order_type=order_type,
            entry_price=db_entry_price,
            strategy=request.strategy,
            message=message,
        )

    def _can_split(self, size: float, instrument) -> bool:
        """Check if position can be split for partial TP."""
        half = size * (self.settings.partial_tp_percent / 100.0)
        remainder = size - half

        if instrument.sec_type == "CASH":
            half = max(round(half / 1000) * 1000, 0)
            remainder = max(round(remainder / 1000) * 1000, 0)
        else:
            half = round(half)
            remainder = round(remainder)

        return half >= instrument.min_size and remainder >= instrument.min_size

    async def _execute_partial_tp(
        self, direction, size, stop_price, tp_price, stop_distance, instrument,
    ) -> dict:
        """Execute with partial TP: TP1 at 1R, TP2 at full TP."""
        tp1_size_raw = size * (self.settings.partial_tp_percent / 100.0)
        tp2_size_raw = size - tp1_size_raw

        if instrument.sec_type == "CASH":
            tp1_size = max(round(tp1_size_raw / 1000) * 1000, instrument.min_size)
            tp2_size = max(round(tp2_size_raw / 1000) * 1000, instrument.min_size)
        else:
            tp1_size = max(round(tp1_size_raw), int(instrument.min_size))
            tp2_size = max(round(tp2_size_raw), int(instrument.min_size))

        # TP1 at 1R distance
        r_distance = stop_distance * self.settings.partial_tp_r_multiple
        if direction == "BUY":
            tp1_price = stop_price + stop_distance + r_distance  # entry + 1R
        else:
            tp1_price = stop_price - stop_distance - r_distance  # entry - 1R

        return await self.ibkr.open_position_with_partial_tp(
            direction=direction,
            size=size,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp_price,
            tp1_size=tp1_size,
            tp2_size=tp2_size,
            instrument_key=instrument.key,
        )

    async def _execute_runner(
        self, direction, size, stop_price, stop_distance, instrument,
    ) -> dict:
        """Execute with runner: TP1 at 1R, no TP2 — monitor trails the SL."""
        tp1_size_raw = size * (self.settings.partial_tp_percent / 100.0)
        runner_size_raw = size - tp1_size_raw

        if instrument.sec_type == "CASH":
            tp1_size = max(round(tp1_size_raw / 1000) * 1000, instrument.min_size)
            runner_size = max(round(runner_size_raw / 1000) * 1000, instrument.min_size)
        else:
            tp1_size = max(round(tp1_size_raw), int(instrument.min_size))
            runner_size = max(round(runner_size_raw), int(instrument.min_size))

        # TP1 at 1R distance
        r_distance = stop_distance * self.settings.partial_tp_r_multiple
        if direction == "BUY":
            tp1_price = stop_price + stop_distance + r_distance  # entry + 1R
        else:
            tp1_price = stop_price - stop_distance - r_distance  # entry - 1R

        return await self.ibkr.open_position_with_runner(
            direction=direction,
            size=size,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp1_size=tp1_size,
            runner_size=runner_size,
            instrument_key=instrument.key,
        )

    async def _execute_pending_partial_tp(
        self, direction, size, entry_price, order_type,
        stop_price, tp_price, stop_distance, instrument,
    ) -> dict:
        """Execute pending order with partial TP: TP1 at 1R, TP2 at full TP."""
        tp1_size_raw = size * (self.settings.partial_tp_percent / 100.0)
        tp2_size_raw = size - tp1_size_raw

        if instrument.sec_type == "CASH":
            tp1_size = max(round(tp1_size_raw / 1000) * 1000, instrument.min_size)
            tp2_size = max(round(tp2_size_raw / 1000) * 1000, instrument.min_size)
        else:
            tp1_size = max(round(tp1_size_raw), int(instrument.min_size))
            tp2_size = max(round(tp2_size_raw), int(instrument.min_size))

        # TP1 at 1R distance from entry_price
        r_distance = stop_distance * self.settings.partial_tp_r_multiple
        if direction == "BUY":
            tp1_price = entry_price + r_distance
        else:
            tp1_price = entry_price - r_distance

        return await self.ibkr.open_pending_position_with_partial_tp(
            direction=direction,
            size=size,
            entry_price=entry_price,
            order_type=order_type,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp_price,
            tp1_size=tp1_size,
            tp2_size=tp2_size,
            instrument_key=instrument.key,
        )

    def _reject(self, request: TradeSubmitRequest, instrument_key: str, reason: str) -> TradeSubmitResponse:
        return TradeSubmitResponse(
            trade_id=0,
            deal_id=None,
            instrument=instrument_key,
            status=TradeStatus.REJECTED,
            direction=request.direction,
            size=0,
            stop_distance=None,
            limit_distance=None,
            conviction=request.conviction,
            message=reason,
        )
