import logging

from telegram import Bot

from app.config import Settings
from app.models.trade import Trade

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.bot = Bot(token=settings.telegram_bot_token)
        self.chat_id = settings.telegram_chat_id

    async def send_trade_update(self, trade: Trade):
        text = (
            f"Trade {trade.status.value.upper()}\n"
            f"Direction: {trade.direction}\n"
            f"Size: {trade.size} oz\n"
            f"Entry: ${trade.entry_price:.2f}\n"
            f"SL: ${trade.stop_loss:.2f} | TP: ${trade.take_profit:.2f}\n"
            f"Order: {trade.deal_id or 'N/A'}"
        )
        if trade.claude_reasoning:
            text += f"\n\nReasoning: {trade.claude_reasoning[:200]}"
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram trade update")

    async def send_rejection(self, trade: Trade, reason: str):
        text = (
            f"Trade REJECTED\n"
            f"Direction: {trade.direction}\n"
            f"Reason: {reason}"
        )
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram rejection")

    async def send_message(self, text: str):
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:
            logger.exception("Failed to send Telegram message")
