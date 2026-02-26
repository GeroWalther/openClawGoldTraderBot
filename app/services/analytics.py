import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade import Trade, TradeStatus

logger = logging.getLogger(__name__)


class TradeAnalytics:
    """Full performance metrics from the Trade table."""

    async def calculate(
        self,
        db_session: AsyncSession,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        instrument: str | None = None,
        conviction: str | None = None,
        strategy: str | None = None,
    ) -> dict:
        """Calculate full analytics from closed trades."""
        trades = await self._fetch_trades(db_session, from_date, to_date, instrument, conviction, strategy)

        if not trades:
            return self._empty_result()

        wins = [t for t in trades if t.pnl is not None and t.pnl > 0]
        losses = [t for t in trades if t.pnl is not None and t.pnl < 0]
        breakevens = [t for t in trades if t.pnl is not None and t.pnl == 0]

        total = len(trades)
        win_count = len(wins)
        loss_count = len(losses)

        win_rate = (win_count / total * 100) if total > 0 else 0.0
        avg_win = sum(t.pnl for t in wins) / win_count if wins else 0.0
        avg_loss = sum(t.pnl for t in losses) / loss_count if losses else 0.0

        # Expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)
        win_pct = win_count / total if total > 0 else 0
        loss_pct = loss_count / total if total > 0 else 0
        expectancy = (win_pct * avg_win) + (loss_pct * avg_loss)

        # Profit factor = gross_profit / gross_loss
        gross_profit = sum(t.pnl for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

        total_pnl = sum(t.pnl for t in trades if t.pnl is not None)

        # Max drawdown
        max_drawdown = self._calculate_max_drawdown(trades)

        # Planned vs achieved R:R
        planned_rr, achieved_rr = self._calculate_rr(trades)

        # Streaks
        current_streak, max_win_streak, max_loss_streak = self._calculate_streaks(trades)

        # Per-instrument breakdown
        per_instrument = self._per_instrument_breakdown(trades)

        # Time-based P&L
        daily_pnl = self._time_pnl(trades, "day")
        weekly_pnl = self._time_pnl(trades, "week")
        monthly_pnl = self._time_pnl(trades, "month")

        # Per-conviction breakdown
        per_conviction = self._per_conviction_breakdown(trades)

        # Per-strategy breakdown
        per_strategy = self._per_strategy_breakdown(trades)

        return {
            "total_trades": total,
            "winning_trades": win_count,
            "losing_trades": loss_count,
            "win_rate": round(win_rate, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
            "total_pnl": round(total_pnl, 2),
            "max_drawdown": round(max_drawdown, 2),
            "planned_rr": round(planned_rr, 2) if planned_rr else None,
            "achieved_rr": round(achieved_rr, 2) if achieved_rr else None,
            "current_streak": current_streak,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "per_instrument": per_instrument,
            "per_conviction": per_conviction,
            "per_strategy": per_strategy,
            "daily_pnl": daily_pnl,
            "weekly_pnl": weekly_pnl,
            "monthly_pnl": monthly_pnl,
        }

    async def _fetch_trades(
        self,
        db_session: AsyncSession,
        from_date: datetime | None,
        to_date: datetime | None,
        instrument: str | None,
        conviction: str | None = None,
        strategy: str | None = None,
    ) -> list[Trade]:
        query = (
            select(Trade)
            .where(Trade.status == TradeStatus.CLOSED)
            .where(Trade.pnl.isnot(None))
        )
        if from_date:
            query = query.where(Trade.closed_at >= from_date)
        if to_date:
            query = query.where(Trade.closed_at <= to_date)
        if instrument:
            query = query.where(Trade.epic == instrument.upper())
        if conviction:
            query = query.where(Trade.conviction == conviction.upper())
        if strategy:
            query = query.where(Trade.strategy == strategy)
        query = query.order_by(Trade.closed_at.asc())

        result = await db_session.execute(query)
        return list(result.scalars().all())

    def _calculate_max_drawdown(self, trades: list[Trade]) -> float:
        """Calculate max drawdown from equity curve."""
        if not trades:
            return 0.0

        equity = 0.0
        peak = 0.0
        max_dd = 0.0

        for t in trades:
            if t.pnl is not None:
                equity += t.pnl
                if equity > peak:
                    peak = equity
                dd = peak - equity
                if dd > max_dd:
                    max_dd = dd

        return max_dd

    def _calculate_rr(self, trades: list[Trade]) -> tuple[float | None, float | None]:
        """Calculate planned vs achieved risk:reward ratios."""
        planned_rrs = []
        achieved_rrs = []

        for t in trades:
            if t.stop_distance and t.limit_distance and t.stop_distance > 0:
                planned_rrs.append(t.limit_distance / t.stop_distance)
            if t.pnl is not None and t.stop_distance and t.stop_distance > 0 and t.size and t.size > 0:
                risk_amount = t.stop_distance * t.size
                if risk_amount > 0:
                    achieved_rrs.append(t.pnl / risk_amount)

        planned = sum(planned_rrs) / len(planned_rrs) if planned_rrs else None
        achieved = sum(achieved_rrs) / len(achieved_rrs) if achieved_rrs else None
        return planned, achieved

    def _calculate_streaks(self, trades: list[Trade]) -> tuple[int, int, int]:
        """Calculate current streak, max win streak, max loss streak."""
        if not trades:
            return 0, 0, 0

        current = 0
        max_win = 0
        max_loss = 0
        win_streak = 0
        loss_streak = 0

        for t in trades:
            if t.pnl is not None and t.pnl > 0:
                win_streak += 1
                loss_streak = 0
                max_win = max(max_win, win_streak)
            elif t.pnl is not None and t.pnl < 0:
                loss_streak += 1
                win_streak = 0
                max_loss = max(max_loss, loss_streak)
            else:
                win_streak = 0
                loss_streak = 0

        # Current streak: positive = wins, negative = losses
        if win_streak > 0:
            current = win_streak
        elif loss_streak > 0:
            current = -loss_streak

        return current, max_win, max_loss

    def _per_instrument_breakdown(self, trades: list[Trade]) -> dict[str, dict]:
        """Group metrics by instrument."""
        by_instrument: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            by_instrument[t.epic].append(t)

        result = {}
        for instrument, inst_trades in by_instrument.items():
            wins = [t for t in inst_trades if t.pnl and t.pnl > 0]
            losses = [t for t in inst_trades if t.pnl and t.pnl < 0]
            total = len(inst_trades)
            result[instrument] = {
                "total_trades": total,
                "winning_trades": len(wins),
                "losing_trades": len(losses),
                "win_rate": round(len(wins) / total * 100, 2) if total > 0 else 0,
                "total_pnl": round(sum(t.pnl for t in inst_trades if t.pnl), 2),
                "avg_pnl": round(sum(t.pnl for t in inst_trades if t.pnl) / total, 2) if total > 0 else 0,
            }
        return result

    def _per_conviction_breakdown(self, trades: list[Trade]) -> dict[str, dict]:
        """Group metrics by conviction level (HIGH, MEDIUM, LOW)."""
        by_conviction: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            conv = t.conviction or "UNKNOWN"
            by_conviction[conv].append(t)

        result = {}
        for conv, conv_trades in by_conviction.items():
            wins = [t for t in conv_trades if t.pnl and t.pnl > 0]
            total = len(conv_trades)
            result[conv] = {
                "total_trades": total,
                "winning_trades": len(wins),
                "win_rate": round(len(wins) / total * 100, 2) if total > 0 else 0,
                "total_pnl": round(sum(t.pnl for t in conv_trades if t.pnl), 2),
            }
        return result

    def _per_strategy_breakdown(self, trades: list[Trade]) -> dict[str, dict]:
        """Group metrics by strategy (intraday, swing, m5_scalp)."""
        by_strategy: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            strat = t.strategy or "unknown"
            by_strategy[strat].append(t)

        result = {}
        for strat, strat_trades in by_strategy.items():
            wins = [t for t in strat_trades if t.pnl and t.pnl > 0]
            total = len(strat_trades)
            result[strat] = {
                "total_trades": total,
                "winning_trades": len(wins),
                "win_rate": round(len(wins) / total * 100, 2) if total > 0 else 0,
                "total_pnl": round(sum(t.pnl for t in strat_trades if t.pnl), 2),
            }
        return result

    def _time_pnl(self, trades: list[Trade], period: str) -> list[dict]:
        """Aggregate P&L by day/week/month."""
        grouped: dict[str, float] = defaultdict(float)

        for t in trades:
            if t.pnl is None or t.closed_at is None:
                continue
            dt = t.closed_at
            if period == "day":
                key = dt.strftime("%Y-%m-%d")
            elif period == "week":
                key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
            else:  # month
                key = dt.strftime("%Y-%m")
            grouped[key] += t.pnl

        return [
            {"period": k, "pnl": round(v, 2)}
            for k, v in sorted(grouped.items())
        ]

    def _empty_result(self) -> dict:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
            "planned_rr": None,
            "achieved_rr": None,
            "current_streak": 0,
            "max_win_streak": 0,
            "max_loss_streak": 0,
            "per_instrument": {},
            "per_conviction": {},
            "per_strategy": {},
            "daily_pnl": [],
            "weekly_pnl": [],
            "monthly_pnl": [],
        }
