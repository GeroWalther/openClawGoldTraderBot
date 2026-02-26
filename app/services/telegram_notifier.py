import logging

from telegram import Bot

from app.config import Settings
from app.instruments import INSTRUMENTS
from app.models.trade import Trade

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.bot = Bot(token=settings.telegram_bot_token)
        self.chat_id = settings.telegram_chat_id

    async def send_trade_update(self, trade: Trade):
        spec = INSTRUMENTS.get(trade.epic)
        name = spec.display_name if spec else trade.epic
        unit = spec.size_unit if spec else "units"

        strategy_line = f"\nStrategy: {trade.strategy}" if trade.strategy else ""
        text = (
            f"Trade {trade.status.value.upper()} — {name}\n"
            f"Direction: {trade.direction}\n"
            f"Size: {trade.size} {unit}\n"
            f"Entry: {trade.entry_price}\n"
            f"SL: {trade.stop_loss} | TP: {trade.take_profit}\n"
            f"Order: {trade.deal_id or 'N/A'}"
            f"{strategy_line}"
        )
        if trade.claude_reasoning:
            text += f"\n\nReasoning: {trade.claude_reasoning[:200]}"
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram trade update")

    async def send_rejection(self, trade: Trade, reason: str):
        spec = INSTRUMENTS.get(trade.epic)
        name = spec.display_name if spec else trade.epic

        strategy_line = f"\nStrategy: {trade.strategy}" if trade.strategy else ""
        text = (
            f"Trade REJECTED — {name}\n"
            f"Direction: {trade.direction}\n"
            f"Reason: {reason}"
            f"{strategy_line}"
        )
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram rejection")

    async def send_modify_update(
        self,
        instrument,
        direction: str,
        old_sl: float | None,
        old_tp: float | None,
        new_sl: float | None,
        new_tp: float | None,
    ):
        name = instrument.display_name if hasattr(instrument, "display_name") else str(instrument)
        lines = [f"SL/TP Modified — {name}", f"Direction: {direction}"]
        if new_sl is not None:
            old_str = f"{old_sl:.2f}" if old_sl is not None else "N/A"
            lines.append(f"Stop Loss: {old_str} → {new_sl:.2f}")
        if new_tp is not None:
            old_str = f"{old_tp:.2f}" if old_tp is not None else "N/A"
            lines.append(f"Take Profit: {old_str} → {new_tp:.2f}")
        try:
            await self.bot.send_message(chat_id=self.chat_id, text="\n".join(lines))
        except Exception:
            logger.exception("Failed to send Telegram modify update")

    async def send_pending_order_update(self, trade: Trade):
        spec = INSTRUMENTS.get(trade.epic)
        name = spec.display_name if spec else trade.epic
        unit = spec.size_unit if spec else "units"

        strategy_line = f"\nStrategy: {trade.strategy}" if trade.strategy else ""
        text = (
            f"PENDING {trade.order_type} ORDER — {name}\n"
            f"Direction: {trade.direction}\n"
            f"Size: {trade.size} {unit}\n"
            f"Entry Price: {trade.entry_price}\n"
            f"SL: {trade.stop_loss} | TP: {trade.take_profit}\n"
            f"Order: {trade.deal_id or 'N/A'}\n"
            f"Status: Waiting for price to reach {trade.entry_price}"
            f"{strategy_line}"
        )
        if trade.claude_reasoning:
            text += f"\n\nReasoning: {trade.claude_reasoning[:200]}"
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram pending order update")

    async def send_cancel_update(
        self,
        instrument,
        direction: str,
        cancelled_order_ids: list[int],
    ):
        name = instrument.display_name if hasattr(instrument, "display_name") else str(instrument)
        ids_str = ", ".join(str(oid) for oid in cancelled_order_ids)
        text = (
            f"ORDER CANCELLED — {name}\n"
            f"Direction: {direction}\n"
            f"Cancelled Order IDs: {ids_str}"
        )
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram cancel update")

    async def send_message(self, text: str):
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram message")
