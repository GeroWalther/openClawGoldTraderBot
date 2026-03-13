"""Telegram command handler for /status and /pnl.

Uses webhook mode via a FastAPI route — avoids polling conflicts with OpenClaw.
Only responds to the configured chat_id for security.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import async_sessionmaker
from telegram import Bot, BotCommand, Update
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
            .connect_timeout(20)
            .read_timeout(20)
            .build()
        )
        self._app.add_handler(CommandHandler("status", self._handle_status))
        self._app.add_handler(CommandHandler("trade_status", self._handle_status))
        self._app.add_handler(CommandHandler("pnl", self._handle_pnl))
        self._app.add_handler(CommandHandler("lastsignal", self._handle_lastsignal))
        self._app.add_handler(CommandHandler("closeall", self._handle_closeall))
        self._app.add_handler(CommandHandler("closeone", self._handle_closeone))

        await self._app.initialize()
        await self._app.start()

        # Delete any old webhook so polling works
        await self._app.bot.delete_webhook()

        # Register command menu with Telegram
        await self._app.bot.set_my_commands([
            BotCommand("status", "Open positions, account & P&L"),
            BotCommand("pnl", "Today's closed trades and P&L"),
            BotCommand("lastsignal", "Last scan result and scores"),
            BotCommand("closeall", "Emergency: close ALL positions"),
            BotCommand("closeone", "Close one position: /closeone NZDUSD SELL"),
        ])

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
        cs = "€"

        # --- IC Markets positions ---
        icm_positions = []
        if self.icm:
            try:
                icm_positions = await self.icm.get_open_positions()
            except Exception as e:
                logger.error("IC Markets get_open_positions failed: %s", e)
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
                pnl = pos.get("unrealized_pnl")
                current = pos.get("current_price")
                pnl_str = f"{pnl:+.2f}{cs}" if pnl is not None else "N/A"
                price_str = f"{current:.5f}" if current else "N/A"
                lines.append(
                    f"{pos['direction']} {name}\n"
                    f"  Size: {size_fmt} {unit}\n"
                    f"  Entry: {pos['avg_cost']:.5f}\n"
                    f"  Now: {price_str} | P&L: {pnl_str}"
                )
                trade = trade_map.get((pos["instrument"], pos["direction"]))
                if trade:
                    sl_str = f"{trade.stop_loss:.5f}" if trade.stop_loss else "N/A"
                    tp_str = f"{trade.take_profit:.5f}" if trade.take_profit else "trailing"
                    sl_line = f"  SL: {sl_str} | TP: {tp_str}"
                    if trade.stop_loss and trade.entry_price and trade.size:
                        mult = spec.multiplier if spec else 1
                        if pos["direction"] == "BUY":
                            locked = (trade.stop_loss - trade.entry_price) * trade.size * mult
                        else:
                            locked = (trade.entry_price - trade.stop_loss) * trade.size * mult
                        sl_line += f"\n  Locks in: {locked:+.2f}{cs} if SL hit"
                    lines.append(sl_line)
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
                lines.append(f"IC Markets Balance: {icm_balance:,.2f}{cs}")
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
            lines.append(f"Total P&L: {total_pnl:+.2f}{cs}")
            lines.append("")
            for t in today_trades:
                spec = INSTRUMENTS.get(t.epic)
                name = spec.display_name if spec else t.epic
                pnl_str = f"{t.pnl:+.2f}{cs}" if t.pnl is not None else "N/A"
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
                lines.append(f"Total P&L: {total_pnl:+.2f}{cs}")
                lines.append("")
                for t in recent:
                    spec = INSTRUMENTS.get(t.epic)
                    name = spec.display_name if spec else t.epic
                    pnl_str = f"{t.pnl:+.2f}{cs}" if t.pnl is not None else "N/A"
                    strategy_str = f" [{t.strategy}]" if t.strategy else ""
                    lines.append(f"  {t.direction} {name} — {pnl_str}{strategy_str}")

        await update.message.reply_text("\n".join(lines))

    async def _handle_lastsignal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return

        journal_dir = Path(__file__).parent.parent.parent / "journal"
        strategies = [
            ("M5 Scalp", "scalp"),
            ("M15 Sensei", "sensei"),
            ("Intraday", "intraday"),
            ("Swing", "swing"),
        ]

        lines: list[str] = ["LAST SIGNALS", "─" * 16]

        for label, folder in strategies:
            # Collect scan files: per-instrument files (latest_scan_INST.json)
            # falling back to single latest_scan.json
            scan_files = sorted(journal_dir.glob(f"{folder}/latest_scan_*.json"))
            if not scan_files:
                single = journal_dir / folder / "latest_scan.json"
                scan_files = [single] if single.exists() else []

            for scan_file in scan_files:
                if not scan_file.exists():
                    continue

                try:
                    data = json.loads(scan_file.read_text())
                except Exception:
                    continue

                # File modification time = when the scan ran
                mtime = datetime.fromtimestamp(scan_file.stat().st_mtime, tz=timezone.utc)
                age_min = (datetime.now(timezone.utc) - mtime).total_seconds() / 60

                if "error" in data:
                    lines.append(f"\n{label} ({age_min:.0f}m ago)")
                    lines.append(f"  ERROR: {data['error']}")
                    continue

                scoring = data.get("scoring", {})
                score = scoring.get("total_score", "?")
                max_score = scoring.get("max_score", "?")
                direction = scoring.get("direction") or "NO TRADE"
                conviction = scoring.get("conviction") or "-"
                price = data.get("price", {}).get("current", "?")
                instrument = data.get("display_name") or data.get("instrument", "?")
                session = data.get("session", {}).get("current", "?")

                lines.append(f"\n{label} — {instrument} ({age_min:.0f}m ago)")
                lines.append(f"  Score: {score}/{max_score}")
                lines.append(f"  Signal: {direction} ({conviction})")
                lines.append(f"  Price: {price}")
                lines.append(f"  Session: {session}")

                # Show scoring factors
                factors = scoring.get("factors", {})
                if factors:
                    factor_strs = [f"{k}={v:+.1f}" if isinstance(v, (int, float)) else f"{k}={v}"
                                   for k, v in factors.items()]
                    lines.append(f"  Factors: {', '.join(factor_strs)}")

        if len(lines) == 2:
            lines.append("\nNo scan data found")

        await update.message.reply_text("\n".join(lines))

    async def _close_position(self, instrument_key: str, direction: str, size: float) -> dict:
        """Close a single position and update DB. Returns result dict."""
        spec = INSTRUMENTS.get(instrument_key)
        broker = self.icm if spec and spec.broker == "icmarkets" else self.ibkr
        result = await broker.close_position(direction, size, instrument_key=instrument_key)
        close_price = result.get("fillPrice")

        # Use broker-reported P&L (in account currency, includes swap/commission)
        pnl = result.get("pnl")

        # Update DB
        async with self.session_factory() as session:
            stmt = (
                select(Trade)
                .where(Trade.direction == direction)
                .where(Trade.epic == instrument_key)
                .where(Trade.status == TradeStatus.EXECUTED)
                .order_by(Trade.id.desc())
                .limit(1)
            )
            row = await session.execute(stmt)
            trade = row.scalar_one_or_none()
            if trade:
                if pnl is None and close_price and trade.entry_price:
                    # Fallback: calculate from price difference
                    mult = spec.multiplier if spec else 1
                    if direction == "BUY":
                        pnl = (close_price - trade.entry_price) * size * mult
                    else:
                        pnl = (trade.entry_price - close_price) * size * mult
                trade.status = TradeStatus.CLOSED
                trade.pnl = pnl
                trade.closed_at = datetime.now(timezone.utc)
                await session.commit()

        # Clean up ratchet state
        ratchet_file = Path(os.environ.get("JOURNAL_DIR", "/app/journal")) / "monitors" / f"ratchet_{instrument_key}_{direction}.json"
        ratchet_file.unlink(missing_ok=True)

        name = spec.display_name if spec else instrument_key
        return {"name": name, "direction": direction, "size": size, "close_price": close_price, "pnl": pnl}

    async def _handle_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Emergency: close ALL open positions."""
        if not self._authorized(update):
            return

        positions = []
        if self.icm:
            try:
                positions = await self.icm.get_open_positions()
            except Exception as e:
                await update.message.reply_text(f"Failed to fetch positions: {e}")
                return

        if not positions:
            await update.message.reply_text("No open positions to close.")
            return

        cs = "€"
        lines = [f"Closing {len(positions)} position(s)...", ""]
        total_pnl = 0.0

        for pos in positions:
            inst = pos["instrument"]
            direction = pos["direction"]
            size = abs(pos["size"])
            try:
                result = await self._close_position(inst, direction, size)
                pnl = result["pnl"]
                pnl_str = f"{pnl:+.2f}{cs}" if pnl is not None else "N/A"
                lines.append(f"CLOSED {result['direction']} {result['name']} — {pnl_str}")
                if pnl is not None:
                    total_pnl += pnl
            except Exception as e:
                lines.append(f"FAILED {direction} {inst}: {e}")

        lines.append(f"\nTotal P&L: {total_pnl:+.2f}{cs}")
        await update.message.reply_text("\n".join(lines))

    async def _handle_closeone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Emergency: close one position. Usage: /closeone NZDUSD SELL"""
        if not self._authorized(update):
            return

        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /closeone NZDUSD SELL [size]")
            return

        instrument_key = args[0].upper()
        direction = args[1].upper()
        size_override = float(args[2]) if len(args) > 2 else None

        # Find the position
        positions = []
        if self.icm:
            try:
                positions = await self.icm.get_open_positions(instrument_key=instrument_key)
            except Exception as e:
                await update.message.reply_text(f"Failed to fetch positions: {e}")
                return

        matching = [p for p in positions if p["direction"] == direction]
        if not matching:
            await update.message.reply_text(f"No open {direction} {instrument_key} position found.")
            return

        pos = matching[0]
        size = size_override or abs(pos["size"])
        cs = "€"

        try:
            result = await self._close_position(instrument_key, direction, size)
            pnl = result["pnl"]
            pnl_str = f"{pnl:+.2f}{cs}" if pnl is not None else "N/A"
            cp_str = f"{result['close_price']:.5f}" if result["close_price"] else "N/A"
            await update.message.reply_text(
                f"CLOSED {result['direction']} {result['name']}\n"
                f"Size: {size}\n"
                f"Close: {cp_str}\n"
                f"P&L: {pnl_str}"
            )
        except Exception as e:
            await update.message.reply_text(f"Failed to close: {e}")

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

        cs = "€"
        lines = [
            "TODAY'S P&L",
            "─" * 16,
            f"Trades: {len(today_trades)} ({len(wins)}W / {len(losses)}L)",
            f"Total P&L: {total_pnl:+.2f}{cs}",
            "",
        ]

        for t in today_trades:
            spec = INSTRUMENTS.get(t.epic)
            name = spec.display_name if spec else t.epic
            pnl_str = f"{t.pnl:+.2f}{cs}" if t.pnl is not None else "N/A"
            strategy_str = f" [{t.strategy}]" if t.strategy else ""
            lines.append(f"{t.direction} {name} — {pnl_str}{strategy_str}")

        await update.message.reply_text("\n".join(lines))
