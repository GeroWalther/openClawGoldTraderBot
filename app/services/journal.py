"""Journal service for recording and analyzing AI trading analyses."""

import json
import logging
from datetime import datetime

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis_journal import AnalysisJournal

logger = logging.getLogger(__name__)


class JournalService:
    """CRUD and analytics for the analysis journal."""

    async def record_analysis(self, db_session: AsyncSession, data: dict) -> AnalysisJournal:
        """Save an analysis entry to the journal."""
        factors_json = json.dumps(data.get("factors", {})) if data.get("factors") else None
        trade_idea_json = json.dumps(data.get("trade_idea", {})) if data.get("trade_idea") else None

        entry = AnalysisJournal(
            instrument=data["instrument"].upper(),
            direction=data["direction"].upper(),
            conviction=data.get("conviction"),
            total_score=data["total_score"],
            factors=factors_json,
            reasoning=data.get("reasoning"),
            trade_idea=trade_idea_json,
            source=data.get("source", "krabbe"),
            outcome="PENDING" if data["direction"] in ("BUY", "SELL") else "SKIPPED",
        )
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        logger.info("Recorded journal entry #%d: %s %s (score=%.1f)", entry.id, entry.direction, entry.instrument, entry.total_score)
        return entry

    async def link_trade(self, db_session: AsyncSession, journal_id: int, trade_id: int) -> AnalysisJournal | None:
        """Link a journal entry to an executed trade."""
        entry = await db_session.get(AnalysisJournal, journal_id)
        if entry is None:
            return None
        entry.linked_trade_id = trade_id
        entry.outcome = "PENDING"
        await db_session.commit()
        await db_session.refresh(entry)
        return entry

    async def update_outcome(
        self, db_session: AsyncSession, journal_id: int, outcome: str, notes: str | None = None
    ) -> AnalysisJournal | None:
        """Record WIN/LOSS/SKIPPED after trade closes."""
        entry = await db_session.get(AnalysisJournal, journal_id)
        if entry is None:
            return None
        entry.outcome = outcome.upper()
        if notes:
            entry.outcome_notes = notes
        await db_session.commit()
        await db_session.refresh(entry)
        return entry

    async def get_journal(
        self,
        db_session: AsyncSession,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        instrument: str | None = None,
    ) -> list[AnalysisJournal]:
        """Retrieve journal entries with optional filters."""
        query = select(AnalysisJournal).order_by(AnalysisJournal.created_at.desc())

        if from_date:
            query = query.where(AnalysisJournal.created_at >= from_date)
        if to_date:
            query = query.where(AnalysisJournal.created_at <= to_date)
        if instrument:
            query = query.where(AnalysisJournal.instrument == instrument.upper())

        result = await db_session.execute(query)
        return list(result.scalars().all())

    async def get_journal_stats(self, db_session: AsyncSession) -> dict:
        """Calculate journal accuracy statistics.

        Returns win rate by conviction, avg score of winners vs losers,
        and score threshold accuracy.
        """
        # Fetch all entries with outcomes
        query = select(AnalysisJournal).where(
            AnalysisJournal.outcome.in_(["WIN", "LOSS"])
        )
        result = await db_session.execute(query)
        entries = list(result.scalars().all())

        if not entries:
            return {
                "total_analyses": 0,
                "total_with_outcome": 0,
                "overall_win_rate": 0.0,
                "per_conviction": {},
                "avg_score_winners": 0.0,
                "avg_score_losers": 0.0,
                "score_threshold_accuracy": {},
            }

        # Count total analyses
        total_query = select(sa_func.count(AnalysisJournal.id))
        total_result = await db_session.execute(total_query)
        total_analyses = total_result.scalar() or 0

        wins = [e for e in entries if e.outcome == "WIN"]
        losses = [e for e in entries if e.outcome == "LOSS"]

        overall_win_rate = len(wins) / len(entries) * 100 if entries else 0

        # Per-conviction breakdown
        per_conviction = {}
        for conv in ("HIGH", "MEDIUM", "LOW"):
            conv_entries = [e for e in entries if e.conviction == conv]
            conv_wins = [e for e in conv_entries if e.outcome == "WIN"]
            if conv_entries:
                per_conviction[conv] = {
                    "total": len(conv_entries),
                    "wins": len(conv_wins),
                    "losses": len(conv_entries) - len(conv_wins),
                    "win_rate": round(len(conv_wins) / len(conv_entries) * 100, 2),
                }

        # Avg score of winners vs losers
        avg_score_winners = (
            sum(abs(e.total_score) for e in wins) / len(wins) if wins else 0.0
        )
        avg_score_losers = (
            sum(abs(e.total_score) for e in losses) / len(losses) if losses else 0.0
        )

        # Score threshold accuracy (at various thresholds)
        thresholds = {}
        for threshold in (10, 12, 15):
            above = [e for e in entries if abs(e.total_score) >= threshold]
            above_wins = [e for e in above if e.outcome == "WIN"]
            if above:
                thresholds[f">={threshold}"] = {
                    "total": len(above),
                    "wins": len(above_wins),
                    "win_rate": round(len(above_wins) / len(above) * 100, 2),
                }

        return {
            "total_analyses": total_analyses,
            "total_with_outcome": len(entries),
            "overall_win_rate": round(overall_win_rate, 2),
            "per_conviction": per_conviction,
            "avg_score_winners": round(avg_score_winners, 2),
            "avg_score_losers": round(avg_score_losers, 2),
            "score_threshold_accuracy": thresholds,
        }
