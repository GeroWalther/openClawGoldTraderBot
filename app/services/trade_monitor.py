"""Background trade close detection.

Polls IBKR positions every 30s and compares against DB trades with status=EXECUTED.
When a position disappears from IBKR, the trade is marked CLOSED with P&L calculated.
Also detects TP1 partial fills for m5_scalp runner trades.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.instruments import INSTRUMENTS
from app.models.trade import Trade, TradeStatus
from app.services.ibkr_client import IBKRClient
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class TradeCloseMonitor:
    def __init__(
        self,
        ibkr_client: IBKRClient,
        session_factory: async_sessionmaker,
        notifier: TelegramNotifier,
        settings: Settings,
        poll_interval: float = 30.0,
    ):
        self.ibkr = ibkr_client
        self.session_factory = session_factory
        self.notifier = notifier
        self.settings = settings
        self.poll_interval = poll_interval
        self._last_known_sizes: dict[int, float] = {}  # trade.id -> last seen position size

    async def run_forever(self):
        """Main loop — runs until cancelled."""
        logger.info("TradeCloseMonitor started (poll every %.0fs)", self.poll_interval)
        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                logger.info("TradeCloseMonitor cancelled")
                raise
            except Exception:
                logger.exception("TradeCloseMonitor error (will retry next cycle)")
            await asyncio.sleep(self.poll_interval)

    async def _check_once(self):
        """Single poll: compare DB trades vs IBKR positions."""
        # 1. Get open IBKR positions
        try:
            ibkr_positions = await self.ibkr.get_open_positions()
        except Exception:
            logger.warning("Cannot fetch IBKR positions — skipping cycle")
            return

        # Build lookup: (instrument, direction) -> total size
        position_map: dict[tuple[str, str], float] = {}
        for pos in ibkr_positions:
            key = (pos["instrument"], pos["direction"])
            position_map[key] = position_map.get(key, 0) + abs(pos["size"])

        # 2. Get all EXECUTED trades from DB
        async with self.session_factory() as session:
            result = await session.execute(
                select(Trade).where(Trade.status == TradeStatus.EXECUTED)
            )
            open_trades = result.scalars().all()

        if not open_trades:
            return

        for trade in open_trades:
            pos_key = (trade.epic, trade.direction)
            ibkr_size = position_map.get(pos_key, 0)

            if ibkr_size == 0:
                # Position fully closed
                await self._handle_close(trade)
            elif trade.id in self._last_known_sizes:
                last_size = self._last_known_sizes[trade.id]
                if ibkr_size < last_size and trade.strategy == "m5_scalp":
                    # Partial fill — TP1 hit on runner trade
                    await self._handle_tp1_hit(trade, ibkr_size)

            # Track current size for next cycle
            if ibkr_size > 0:
                self._last_known_sizes[trade.id] = ibkr_size
            else:
                self._last_known_sizes.pop(trade.id, None)

    async def _handle_close(self, trade: Trade):
        """Mark trade as CLOSED, calculate P&L, notify."""
        now = datetime.now(timezone.utc)

        # Get approximate close price
        try:
            price_data = await self.ibkr.get_price(trade.epic)
            close_price = price_data.get("last") or price_data.get("bid") or 0.0
        except Exception:
            close_price = 0.0
            logger.warning("Cannot get close price for %s — using 0", trade.epic)

        # Calculate P&L
        spec = INSTRUMENTS.get(trade.epic)
        multiplier = spec.multiplier if spec else 1.0
        if trade.entry_price and close_price:
            if trade.direction == "BUY":
                pnl = (close_price - trade.entry_price) * trade.size * multiplier
            else:
                pnl = (trade.entry_price - close_price) * trade.size * multiplier
        else:
            pnl = 0.0

        # Duration
        if trade.created_at:
            duration = now - trade.created_at.replace(tzinfo=timezone.utc)
            hours, remainder = divmod(int(duration.total_seconds()), 3600)
            minutes = remainder // 60
            if hours > 0:
                duration_str = f"{hours}h {minutes}m"
            else:
                duration_str = f"{minutes}m"
        else:
            duration_str = "unknown"

        # Update DB in a fresh session (guard against race conditions)
        async with self.session_factory() as session:
            result = await session.execute(
                select(Trade).where(Trade.id == trade.id)
            )
            db_trade = result.scalar_one_or_none()
            if db_trade is None or db_trade.status != TradeStatus.EXECUTED:
                return  # Already closed or gone

            db_trade.status = TradeStatus.CLOSED
            db_trade.pnl = round(pnl, 2)
            db_trade.closed_at = now
            await session.commit()

        logger.info(
            "Trade #%d CLOSED: %s %s — P&L: $%.2f — Duration: %s",
            trade.id, trade.epic, trade.direction, pnl, duration_str,
        )

        await self.notifier.send_close_update(trade, close_price, round(pnl, 2), duration_str)
        self._last_known_sizes.pop(trade.id, None)

    async def _handle_tp1_hit(self, trade: Trade, remaining_size: float):
        """Notify when TP1 fills on a runner trade (size decreased)."""
        spec = INSTRUMENTS.get(trade.epic)

        # Calculate TP1 price from entry + 1R
        if trade.stop_distance and trade.entry_price:
            r_distance = trade.stop_distance * self.settings.partial_tp_r_multiple
            if trade.direction == "BUY":
                tp1_price = trade.entry_price + r_distance
            else:
                tp1_price = trade.entry_price - r_distance
        else:
            tp1_price = 0.0

        logger.info(
            "TP1 HIT on trade #%d: %s %s — runner %.0f remaining",
            trade.id, trade.epic, trade.direction, remaining_size,
        )

        await self.notifier.send_tp1_hit_update(trade, tp1_price, remaining_size)
