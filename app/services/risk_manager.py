import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.trade import Trade, TradeStatus

logger = logging.getLogger(__name__)


class RiskManager:
    """Cooldown, daily/weekly loss limit enforcement."""

    def __init__(self, settings: Settings, ibkr_client=None):
        self.settings = settings
        self.ibkr_client = ibkr_client  # For unrealized PnL check

    async def can_trade(
        self, db_session: AsyncSession, account_balance: float,
        strategy: str | None = None,
    ) -> tuple[bool, str]:
        """
        Combined check: cooldown + daily trade count + daily P&L loss limit + weekly limit.

        Returns (can_trade, reason).
        """
        # Strategy-specific scalp cooldown (separate from main cooldown)
        if strategy == "m5_scalp":
            ok, reason = await self._check_scalp_cooldown(db_session)
            if not ok:
                return False, reason

        # Check cooldown
        ok, reason = await self.check_cooldown(db_session)
        if not ok:
            return False, reason

        # Check daily trade count
        ok, reason = await self._check_daily_trade_count(db_session)
        if not ok:
            return False, reason

        # Check daily P&L loss limit (includes unrealized)
        ok, reason = await self._check_daily_loss_limit(db_session, account_balance)
        if not ok:
            return False, reason

        # Check weekly P&L loss limit
        ok, reason = await self._check_weekly_loss_limit(db_session, account_balance)
        if not ok:
            return False, reason

        return True, "Risk checks passed"

    async def check_cooldown(
        self, db_session: AsyncSession
    ) -> tuple[bool, str]:
        """
        Check consecutive losses and enforce time-based cooldown.

        2 losses → 2h, 3 losses → 4h, etc. (base^(n-1) hours).
        Resets on a win.
        """
        if not self.settings.cooldown_enabled:
            return True, "Cooldown disabled"

        consecutive_losses = await self._count_consecutive_losses(db_session)

        if consecutive_losses < self.settings.cooldown_after_losses:
            return True, f"No cooldown ({consecutive_losses} consecutive losses)"

        # Calculate cooldown duration
        excess = consecutive_losses - self.settings.cooldown_after_losses
        cooldown_hours = self.settings.cooldown_hours_base * (2 ** excess)

        # Find the last closed trade time
        last_loss_time = await self._last_loss_time(db_session)
        if last_loss_time is None:
            return True, "No cooldown (no recent losses)"

        # Ensure timezone-aware comparison
        if last_loss_time.tzinfo is None:
            last_loss_time = last_loss_time.replace(tzinfo=timezone.utc)

        cooldown_end = last_loss_time + timedelta(hours=cooldown_hours)
        now = datetime.now(timezone.utc)

        if now < cooldown_end:
            remaining = (cooldown_end - now).total_seconds() / 60
            return False, (
                f"Cooldown active: {consecutive_losses} consecutive losses. "
                f"Wait {remaining:.0f} minutes ({cooldown_hours}h cooldown)"
            )

        return True, f"Cooldown expired ({consecutive_losses} consecutive losses, {cooldown_hours}h elapsed)"

    async def get_cooldown_status(
        self, db_session: AsyncSession, account_balance: float
    ) -> dict:
        """Get full cooldown/risk status for the API."""
        consecutive_losses = await self._count_consecutive_losses(db_session)
        daily_count = await self._get_daily_trade_count(db_session)
        daily_pnl = await self._get_daily_pnl(db_session)
        daily_loss_limit = account_balance * (self.settings.max_daily_loss_percent / 100)

        cooldown_active = False
        cooldown_reason = None
        cooldown_remaining = None

        if self.settings.cooldown_enabled and consecutive_losses >= self.settings.cooldown_after_losses:
            excess = consecutive_losses - self.settings.cooldown_after_losses
            cooldown_hours = self.settings.cooldown_hours_base * (2 ** excess)
            last_loss_time = await self._last_loss_time(db_session)
            if last_loss_time:
                if last_loss_time.tzinfo is None:
                    last_loss_time = last_loss_time.replace(tzinfo=timezone.utc)
                cooldown_end = last_loss_time + timedelta(hours=cooldown_hours)
                now = datetime.now(timezone.utc)
                if now < cooldown_end:
                    cooldown_active = True
                    cooldown_remaining = (cooldown_end - now).total_seconds() / 60
                    cooldown_reason = f"{consecutive_losses} consecutive losses → {cooldown_hours}h cooldown"

        can_trade = not cooldown_active
        if can_trade and self.settings.daily_loss_limit_enabled:
            if daily_count >= self.settings.max_daily_trades:
                can_trade = False
            if daily_pnl <= -daily_loss_limit:
                can_trade = False

        return {
            "can_trade": can_trade,
            "cooldown_active": cooldown_active,
            "cooldown_reason": cooldown_reason,
            "cooldown_remaining_minutes": cooldown_remaining,
            "consecutive_losses": consecutive_losses,
            "daily_trades_count": daily_count,
            "daily_trades_limit": self.settings.max_daily_trades,
            "daily_pnl": daily_pnl,
            "daily_loss_limit": daily_loss_limit,
        }

    async def _count_consecutive_losses(self, db_session: AsyncSession) -> int:
        """Count consecutive losses from most recent closed trades."""
        result = await db_session.execute(
            select(Trade)
            .where(Trade.status == TradeStatus.CLOSED)
            .where(Trade.pnl.isnot(None))
            .order_by(Trade.closed_at.desc())
            .limit(20)
        )
        trades = result.scalars().all()

        count = 0
        for trade in trades:
            if trade.pnl is not None and trade.pnl < 0:
                count += 1
            else:
                break
        return count

    async def _last_loss_time(self, db_session: AsyncSession) -> datetime | None:
        """Get the closed_at time of the most recent losing trade."""
        result = await db_session.execute(
            select(Trade.closed_at)
            .where(Trade.status == TradeStatus.CLOSED)
            .where(Trade.pnl < 0)
            .order_by(Trade.closed_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row

    async def _check_daily_trade_count(
        self, db_session: AsyncSession
    ) -> tuple[bool, str]:
        """Check if daily trade count limit is reached."""
        if not self.settings.daily_loss_limit_enabled:
            return True, "Daily limits disabled"

        count = await self._get_daily_trade_count(db_session)
        if count >= self.settings.max_daily_trades:
            return False, f"Daily trade limit reached: {count}/{self.settings.max_daily_trades}"
        return True, f"Daily trades: {count}/{self.settings.max_daily_trades}"

    async def _check_daily_loss_limit(
        self, db_session: AsyncSession, account_balance: float
    ) -> tuple[bool, str]:
        """Check if daily P&L loss limit is breached (closed + unrealized)."""
        if not self.settings.daily_loss_limit_enabled:
            return True, "Daily limits disabled"

        daily_pnl = await self._get_daily_pnl(db_session)

        # Include unrealized PnL from open positions
        unrealized = await self._get_unrealized_pnl()
        total_daily_pnl = daily_pnl + unrealized
        limit = account_balance * (self.settings.max_daily_loss_percent / 100)

        if total_daily_pnl <= -limit:
            return False, (
                f"Daily loss limit reached: ${total_daily_pnl:.2f} "
                f"(closed: ${daily_pnl:.2f}, unrealized: ${unrealized:.2f}, limit: -${limit:.2f})"
            )
        return True, f"Daily P&L: ${total_daily_pnl:.2f} (limit: -${limit:.2f})"

    async def _check_weekly_loss_limit(
        self, db_session: AsyncSession, account_balance: float
    ) -> tuple[bool, str]:
        """Check if weekly P&L loss limit is breached."""
        if not getattr(self.settings, "weekly_loss_limit_enabled", False):
            return True, "Weekly limits disabled"

        weekly_pnl = await self._get_weekly_pnl(db_session)
        unrealized = await self._get_unrealized_pnl()
        total_weekly_pnl = weekly_pnl + unrealized
        limit = account_balance * (getattr(self.settings, "max_weekly_loss_percent", 6.0) / 100)

        if total_weekly_pnl <= -limit:
            return False, (
                f"Weekly loss limit reached: ${total_weekly_pnl:.2f} "
                f"(closed: ${weekly_pnl:.2f}, unrealized: ${unrealized:.2f}, limit: -${limit:.2f})"
            )
        return True, f"Weekly P&L: ${total_weekly_pnl:.2f} (limit: -${limit:.2f})"

    async def _get_daily_trade_count(self, db_session: AsyncSession) -> int:
        """Count today's executed trades."""
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await db_session.execute(
            select(func.count(Trade.id))
            .where(Trade.status.in_([TradeStatus.EXECUTED, TradeStatus.CLOSED]))
            .where(Trade.created_at >= today_start)
        )
        return result.scalar_one() or 0

    async def _get_daily_pnl(self, db_session: AsyncSession) -> float:
        """Sum P&L of today's closed trades."""
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await db_session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0))
            .where(Trade.status == TradeStatus.CLOSED)
            .where(Trade.pnl.isnot(None))
            .where(Trade.closed_at >= today_start)
        )
        return float(result.scalar_one())

    async def _get_weekly_pnl(self, db_session: AsyncSession) -> float:
        """Sum P&L of this week's (Mon-Sun) closed trades."""
        now = datetime.now(timezone.utc)
        # Monday 00:00 UTC of current week
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result = await db_session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0))
            .where(Trade.status == TradeStatus.CLOSED)
            .where(Trade.pnl.isnot(None))
            .where(Trade.closed_at >= week_start)
        )
        return float(result.scalar_one())

    async def _check_scalp_cooldown(
        self, db_session: AsyncSession
    ) -> tuple[bool, str]:
        """
        M5 scalp-specific cooldown: exponential backoff after consecutive scalp losses.

        Mirrors backtest formula: cooldown_bars = 2 * 2^(n-2), with base=10 min.
        """
        if not self.settings.scalp_cooldown_enabled:
            return True, "Scalp cooldown disabled"

        consecutive = await self._count_consecutive_scalp_losses(db_session)

        if consecutive < self.settings.scalp_cooldown_after_losses:
            return True, f"No scalp cooldown ({consecutive} consecutive scalp losses)"

        # Exponential cooldown: base_minutes * 2^(excess)
        excess = consecutive - self.settings.scalp_cooldown_after_losses
        cooldown_minutes = self.settings.scalp_cooldown_minutes_base * (2 ** excess)

        # Find last scalp loss time
        last_time = await self._last_scalp_loss_time(db_session)
        if last_time is None:
            return True, "No scalp cooldown (no recent scalp losses)"

        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)

        cooldown_end = last_time + timedelta(minutes=cooldown_minutes)
        now = datetime.now(timezone.utc)

        if now < cooldown_end:
            remaining = (cooldown_end - now).total_seconds() / 60
            return False, (
                f"Scalp cooldown active: {consecutive} consecutive scalp losses. "
                f"Wait {remaining:.0f} min ({cooldown_minutes} min cooldown)"
            )

        return True, f"Scalp cooldown expired ({consecutive} consecutive scalp losses, {cooldown_minutes} min elapsed)"

    async def _count_consecutive_scalp_losses(self, db_session: AsyncSession) -> int:
        """Count consecutive losses from most recent closed m5_scalp trades."""
        result = await db_session.execute(
            select(Trade)
            .where(Trade.strategy == "m5_scalp")
            .where(Trade.status == TradeStatus.CLOSED)
            .where(Trade.pnl.isnot(None))
            .order_by(Trade.closed_at.desc())
            .limit(20)
        )
        trades = result.scalars().all()

        count = 0
        for trade in trades:
            if trade.pnl is not None and trade.pnl < 0:
                count += 1
            else:
                break
        return count

    async def _last_scalp_loss_time(self, db_session: AsyncSession) -> datetime | None:
        """Get the closed_at time of the most recent losing m5_scalp trade."""
        result = await db_session.execute(
            select(Trade.closed_at)
            .where(Trade.strategy == "m5_scalp")
            .where(Trade.status == TradeStatus.CLOSED)
            .where(Trade.pnl < 0)
            .order_by(Trade.closed_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_unrealized_pnl(self) -> float:
        """Get total unrealized PnL from IBKR open positions."""
        if self.ibkr_client is None:
            return 0.0
        try:
            status = await self.ibkr_client.get_positions_status()
            positions = status.get("positions", [])
            total = sum(
                float(p.get("unrealized_pnl", p.get("pnl", 0)) or 0)
                for p in positions
            )
            return total
        except Exception as e:
            logger.warning("Failed to get unrealized PnL: %s", e)
            return 0.0
