"""Telegram command handler for /status and /pnl.

Uses python-telegram-bot v20+ Application with polling mode.
Only responds to the configured chat_id for security.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import async_sessionmaker
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import Settings
from app.instruments import INSTRUMENTS
from app.models.trade import Trade, TradeStatus
from app.services.ibkr_client import IBKRClient

logger = logging.getLogger(__name__)


class TelegramCommandHandler:
    def __init__(
        self,
        ibkr_client: IBKRClient,
        session_factory: async_sessionmaker,
        settings: Settings,
    ):
        self.ibkr = ibkr_client
        self.session_factory = session_factory
        self.settings = settings
        self._allowed_chat_id = str(settings.telegram_chat_id)
        self._app: Application | None = None

    async def start(self):
        """Build and start the Telegram bot polling."""
        self._app = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .build()
        )
        self._app.add_handler(CommandHandler("status", self._handle_status))
        self._app.add_handler(CommandHandler("trade_status", self._handle_status))
        self._app.add_handler(CommandHandler("pnl", self._handle_pnl))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("TelegramCommandHandler polling started")

    async def stop(self):
        """Stop the Telegram bot polling."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("TelegramCommandHandler stopped")

    def _authorized(self, update: Update) -> bool:
        return str(update.effective_chat.id) == self._allowed_chat_id

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return

        lines: list[str] = []

        # Open positions from IBKR
        try:
            positions = await self.ibkr.get_open_positions()
        except Exception:
            positions = []
            lines.append("(IBKR disconnected — positions unavailable)")

        if positions:
            lines.append("OPEN POSITIONS")
            lines.append("─" * 24)
            for pos in positions:
                spec = INSTRUMENTS.get(pos["instrument"])
                name = spec.display_name if spec else pos["instrument"]
                unit = spec.size_unit if spec else "units"
                lines.append(
                    f"{pos['direction']} {name}\n"
                    f"  Size: {abs(pos['size']):.0f} {unit}\n"
                    f"  Avg cost: {pos['avg_cost']:.2f}"
                )

            # Find matching DB trades for SL/TP info
            async with self.session_factory() as session:
                result = await session.execute(
                    select(Trade).where(Trade.status == TradeStatus.EXECUTED)
                )
                open_trades = result.scalars().all()

            for trade in open_trades:
                sl_str = f"{trade.stop_loss:.2f}" if trade.stop_loss else "N/A"
                tp_str = f"{trade.take_profit:.2f}" if trade.take_profit else "trailing"
                lines.append(f"  SL: {sl_str} | TP: {tp_str}")
        else:
            lines.append("No open positions")

        # Pending orders
        try:
            pending = await self.ibkr.get_pending_orders()
        except Exception:
            pending = []

        if pending:
            lines.append("")
            lines.append("PENDING ORDERS")
            lines.append("─" * 24)
            for order in pending:
                spec = INSTRUMENTS.get(order["instrument"])
                name = spec.display_name if spec else order["instrument"]
                lines.append(
                    f"{order['action']} {name} @ {order['entryPrice']}"
                )

        # Account info
        try:
            account = await self.ibkr.get_account_info()
            nlv = account.get("NetLiquidation", 0)
            lines.append("")
            lines.append(f"Account NLV: ${nlv:,.2f}")
        except Exception:
            pass

        # Last 5 closed trades
        async with self.session_factory() as session:
            result = await session.execute(
                select(Trade)
                .where(Trade.status == TradeStatus.CLOSED)
                .order_by(Trade.closed_at.desc())
                .limit(5)
            )
            recent = result.scalars().all()

        if recent:
            lines.append("")
            lines.append("RECENT CLOSES")
            lines.append("─" * 24)
            for t in recent:
                spec = INSTRUMENTS.get(t.epic)
                name = spec.display_name if spec else t.epic
                pnl_str = f"${t.pnl:+.2f}" if t.pnl is not None else "N/A"
                lines.append(f"{t.direction} {name} — {pnl_str}")

        await update.message.reply_text("\n".join(lines))

    async def _handle_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return

        # Today's closed trades (UTC)
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

        async with self.session_factory() as session:
            result = await session.execute(
                select(Trade).where(
                    and_(
                        Trade.status == TradeStatus.CLOSED,
                        Trade.closed_at >= today_start,
                    )
                ).order_by(Trade.closed_at.desc())
            )
            today_trades = result.scalars().all()

        if not today_trades:
            await update.message.reply_text("No closed trades today.")
            return

        wins = [t for t in today_trades if t.pnl is not None and t.pnl > 0]
        losses = [t for t in today_trades if t.pnl is not None and t.pnl <= 0]
        total_pnl = sum(t.pnl for t in today_trades if t.pnl is not None)

        lines = [
            "TODAY'S P&L",
            "─" * 24,
            f"Trades: {len(today_trades)} ({len(wins)}W / {len(losses)}L)",
            f"Total P&L: ${total_pnl:+.2f}",
            "",
        ]

        for t in today_trades:
            spec = INSTRUMENTS.get(t.epic)
            name = spec.display_name if spec else t.epic
            pnl_str = f"${t.pnl:+.2f}" if t.pnl is not None else "N/A"
            strategy_str = f" [{t.strategy}]" if t.strategy else ""
            lines.append(f"{t.direction} {name} — {pnl_str}{strategy_str}")

        await update.message.reply_text("\n".join(lines))
