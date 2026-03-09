"""Telegram command handler for /status and /pnl.

Uses webhook mode via a FastAPI route — avoids polling conflicts with OpenClaw.
Only responds to the configured chat_id for security.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import async_sessionmaker
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import Settings
from app.instruments import INSTRUMENTS
from app.models.trade import Trade, TradeStatus
from app.services.ibkr_client import IBKRClient
from app.services.icmarkets_client import ICMarketsClient

logger = logging.getLogger(__name__)


class TelegramCommandHandler:
    def __init__(
        self,
        ibkr_client: IBKRClient,
        icm_client: ICMarketsClient | None = None,
        session_factory: async_sessionmaker = None,
        settings: Settings = None,
    ):
        self.ibkr = ibkr_client
        self.icm = icm_client
        self.session_factory = session_factory
        self.settings = settings
        self._allowed_chat_id = str(settings.telegram_chat_id)
        self._app: Application | None = None

    async def start(self):
        """Build Application with polling (pulls updates from Telegram)."""
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

        # Delete any old webhook so polling works
        await self._app.bot.delete_webhook()

        # Start polling in background
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("TelegramCommandHandler started (polling mode)")

    async def stop(self):
        """Stop polling and the Application."""
        if self._app:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("TelegramCommandHandler stopped")

    async def process_update(self, data: dict):
        """Process a raw Telegram update dict (called from FastAPI webhook route)."""
        if self._app is None:
            return
        update = Update.de_json(data, self._app.bot)
        await self._app.process_update(update)

    def _authorized(self, update: Update) -> bool:
        return str(update.effective_chat.id) == self._allowed_chat_id

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return

        lines: list[str] = []

        # --- IC Markets positions ---
        icm_positions = []
        if self.icm:
            try:
                icm_positions = await self.icm.get_open_positions()
            except Exception:
                lines.append("(IC Markets disconnected)")

        # Find matching DB trades for SL/TP info
        async with self.session_factory() as session:
            result = await session.execute(
                select(Trade).where(Trade.status == TradeStatus.EXECUTED)
            )
            open_trades = result.scalars().all()

        trade_map = {}
        for trade in open_trades:
            trade_map[(trade.epic, trade.direction)] = trade

        if icm_positions:
            lines.append("OPEN POSITIONS")
            lines.append("─" * 16)
            for pos in icm_positions:
                spec = INSTRUMENTS.get(pos["instrument"])
                name = spec.display_name if spec else pos["instrument"]
                unit = spec.size_unit if spec else "lots"
                size_fmt = f"{abs(pos['size']):.4f}" if abs(pos['size']) < 1 else f"{abs(pos['size']):.2f}"
                lines.append(
                    f"{pos['direction']} {name}\n"
                    f"  Size: {size_fmt} {unit}\n"
                    f"  Avg cost: {pos['avg_cost']:.5f}"
                )
                trade = trade_map.get((pos["instrument"], pos["direction"]))
                if trade:
                    sl_str = f"{trade.stop_loss:.5f}" if trade.stop_loss else "N/A"
                    tp_str = f"{trade.take_profit:.5f}" if trade.take_profit else "trailing"
                    lines.append(f"  SL: {sl_str} | TP: {tp_str}")
        else:
            lines.append("No open positions")

        # Pending orders
        pending = []
        if self.icm:
            try:
                pending = await self.icm.get_pending_orders()
            except Exception:
                pass

        if pending:
            lines.append("")
            lines.append("PENDING ORDERS")
            lines.append("─" * 16)
            for order in pending:
                spec = INSTRUMENTS.get(order["instrument"])
                name = spec.display_name if spec else order["instrument"]
                lines.append(
                    f"{order['action']} {name} @ {order['entryPrice']}"
                )

        # Account info
        if self.icm:
            try:
                icm_account = await self.icm.get_account_info()
                icm_balance = icm_account.get("NetLiquidation", 0)
                lines.append("")
                lines.append("ACCOUNT")
                lines.append("─" * 16)
                lines.append(f"IC Markets Equity: ${icm_balance:,.2f}")
            except Exception:
                pass

        # Today's P&L summary
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

        if today_trades:
            wins = [t for t in today_trades if t.pnl is not None and t.pnl > 0]
            losses = [t for t in today_trades if t.pnl is not None and t.pnl <= 0]
            total_pnl = sum(t.pnl for t in today_trades if t.pnl is not None)

            lines.append("")
            lines.append("TODAY'S P&L")
            lines.append("─" * 16)
            lines.append(f"Trades: {len(today_trades)} ({len(wins)}W / {len(losses)}L)")
            lines.append(f"Total P&L: ${total_pnl:+.2f}")
            lines.append("")
            for t in today_trades:
                spec = INSTRUMENTS.get(t.epic)
                name = spec.display_name if spec else t.epic
                pnl_str = f"${t.pnl:+.2f}" if t.pnl is not None else "N/A"
                strategy_str = f" [{t.strategy}]" if t.strategy else ""
                lines.append(f"  {t.direction} {name} — {pnl_str}{strategy_str}")
        else:
            # Show last 5 closed trades if nothing today
            async with self.session_factory() as session:
                result = await session.execute(
                    select(Trade)
                    .where(Trade.status == TradeStatus.CLOSED)
                    .order_by(Trade.closed_at.desc())
                    .limit(5)
                )
                recent = result.scalars().all()

            if recent:
                wins = [t for t in recent if t.pnl is not None and t.pnl > 0]
                losses = [t for t in recent if t.pnl is not None and t.pnl <= 0]
                total_pnl = sum(t.pnl for t in recent if t.pnl is not None)

                lines.append("")
                lines.append("RECENT CLOSES (no trades today)")
                lines.append("─" * 16)
                lines.append(f"Trades: {len(recent)} ({len(wins)}W / {len(losses)}L)")
                lines.append(f"Total P&L: ${total_pnl:+.2f}")
                lines.append("")
                for t in recent:
                    spec = INSTRUMENTS.get(t.epic)
                    name = spec.display_name if spec else t.epic
                    pnl_str = f"${t.pnl:+.2f}" if t.pnl is not None else "N/A"
                    strategy_str = f" [{t.strategy}]" if t.strategy else ""
                    lines.append(f"  {t.direction} {name} — {pnl_str}{strategy_str}")

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
            "─" * 16,
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
