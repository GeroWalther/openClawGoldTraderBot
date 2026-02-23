import pytest
import pytest_asyncio
from datetime import datetime

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.database import Base
from app.models.analysis_journal import AnalysisJournal
from app.services.journal import JournalService


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.fixture
def journal_service():
    return JournalService()


def _sample_analysis(direction="BUY", conviction="HIGH", score=16.5, instrument="XAUUSD"):
    return {
        "instrument": instrument,
        "direction": direction,
        "conviction": conviction,
        "total_score": score,
        "factors": {"d1_trend": 2, "4h_momentum": 1.5, "1h_entry": 1},
        "reasoning": "Strong bullish D1 trend with macro support",
        "trade_idea": {"stop_distance": 45, "limit_distance": 90},
    }


class TestJournalService:

    @pytest.mark.asyncio
    async def test_record_analysis(self, db_session, journal_service):
        """Should create a journal entry and return it."""
        data = _sample_analysis()
        entry = await journal_service.record_analysis(db_session, data)

        assert entry.id is not None
        assert entry.instrument == "XAUUSD"
        assert entry.direction == "BUY"
        assert entry.conviction == "HIGH"
        assert entry.total_score == 16.5
        assert entry.outcome == "PENDING"
        assert entry.source == "krabbe"

    @pytest.mark.asyncio
    async def test_record_no_trade_sets_skipped(self, db_session, journal_service):
        """NO_TRADE analyses should have outcome SKIPPED."""
        data = _sample_analysis(direction="NO_TRADE", conviction=None, score=5.0)
        entry = await journal_service.record_analysis(db_session, data)

        assert entry.outcome == "SKIPPED"

    @pytest.mark.asyncio
    async def test_link_trade(self, db_session, journal_service):
        """Should link a journal entry to a trade ID."""
        entry = await journal_service.record_analysis(db_session, _sample_analysis())
        updated = await journal_service.link_trade(db_session, entry.id, trade_id=42)

        assert updated.linked_trade_id == 42
        assert updated.outcome == "PENDING"

    @pytest.mark.asyncio
    async def test_link_trade_nonexistent(self, db_session, journal_service):
        """Linking nonexistent entry should return None."""
        result = await journal_service.link_trade(db_session, 9999, trade_id=42)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_outcome(self, db_session, journal_service):
        """Should update outcome to WIN/LOSS."""
        entry = await journal_service.record_analysis(db_session, _sample_analysis())
        updated = await journal_service.update_outcome(db_session, entry.id, "WIN", "Hit TP at 1.8R")

        assert updated.outcome == "WIN"
        assert updated.outcome_notes == "Hit TP at 1.8R"

    @pytest.mark.asyncio
    async def test_update_outcome_nonexistent(self, db_session, journal_service):
        """Updating nonexistent entry should return None."""
        result = await journal_service.update_outcome(db_session, 9999, "WIN")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_journal_all(self, db_session, journal_service):
        """Should return all journal entries."""
        await journal_service.record_analysis(db_session, _sample_analysis())
        await journal_service.record_analysis(db_session, _sample_analysis(direction="SELL", score=-12.0))

        entries = await journal_service.get_journal(db_session)
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_get_journal_filter_by_instrument(self, db_session, journal_service):
        """Should filter by instrument."""
        await journal_service.record_analysis(db_session, _sample_analysis(instrument="XAUUSD"))
        await journal_service.record_analysis(db_session, _sample_analysis(instrument="MES"))

        entries = await journal_service.get_journal(db_session, instrument="XAUUSD")
        assert len(entries) == 1
        assert entries[0].instrument == "XAUUSD"

    @pytest.mark.asyncio
    async def test_get_journal_stats_empty(self, db_session, journal_service):
        """Empty journal should return zero stats."""
        stats = await journal_service.get_journal_stats(db_session)

        assert stats["total_analyses"] == 0
        assert stats["overall_win_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_get_journal_stats_with_outcomes(self, db_session, journal_service):
        """Should calculate correct win rates by conviction."""
        # Create entries with outcomes
        e1 = await journal_service.record_analysis(
            db_session, _sample_analysis(conviction="HIGH", score=16.0)
        )
        await journal_service.update_outcome(db_session, e1.id, "WIN")

        e2 = await journal_service.record_analysis(
            db_session, _sample_analysis(conviction="HIGH", score=14.0)
        )
        await journal_service.update_outcome(db_session, e2.id, "WIN")

        e3 = await journal_service.record_analysis(
            db_session, _sample_analysis(conviction="MEDIUM", score=11.0)
        )
        await journal_service.update_outcome(db_session, e3.id, "LOSS")

        stats = await journal_service.get_journal_stats(db_session)

        assert stats["total_with_outcome"] == 3
        assert stats["overall_win_rate"] == pytest.approx(66.67, abs=0.01)

        assert "HIGH" in stats["per_conviction"]
        assert stats["per_conviction"]["HIGH"]["win_rate"] == 100.0

        assert "MEDIUM" in stats["per_conviction"]
        assert stats["per_conviction"]["MEDIUM"]["win_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_get_journal_stats_avg_scores(self, db_session, journal_service):
        """Avg score of winners should differ from losers."""
        e1 = await journal_service.record_analysis(
            db_session, _sample_analysis(score=18.0)
        )
        await journal_service.update_outcome(db_session, e1.id, "WIN")

        e2 = await journal_service.record_analysis(
            db_session, _sample_analysis(score=10.5)
        )
        await journal_service.update_outcome(db_session, e2.id, "LOSS")

        stats = await journal_service.get_journal_stats(db_session)

        assert stats["avg_score_winners"] == 18.0
        assert stats["avg_score_losers"] == 10.5

    @pytest.mark.asyncio
    async def test_factors_serialized_as_json(self, db_session, journal_service):
        """Factors should be stored as JSON string."""
        data = _sample_analysis()
        entry = await journal_service.record_analysis(db_session, data)

        assert entry.factors is not None
        import json
        factors = json.loads(entry.factors)
        assert factors["d1_trend"] == 2
