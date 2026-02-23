"""Journal API endpoints for recording and querying AI analyses."""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import Settings
from app.dependencies import get_db_session, get_settings, get_journal_service
from app.models.schemas import JournalCreateRequest, JournalResponse, JournalStatsResponse
from app.services.journal import JournalService

router = APIRouter(prefix="/api/v1/journal", tags=["journal"])


@router.post("", response_model=JournalResponse)
async def create_journal_entry(
    request: JournalCreateRequest,
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    db_session=Depends(get_db_session),
    journal_service: JournalService = Depends(get_journal_service),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    data = request.model_dump()
    entry = await journal_service.record_analysis(db_session, data)
    return _entry_to_response(entry)


@router.get("", response_model=list[JournalResponse])
async def list_journal_entries(
    from_date: str | None = None,
    to_date: str | None = None,
    instrument: str | None = None,
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    db_session=Depends(get_db_session),
    journal_service: JournalService = Depends(get_journal_service),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    from_dt = datetime.fromisoformat(from_date) if from_date else None
    to_dt = datetime.fromisoformat(to_date) if to_date else None

    entries = await journal_service.get_journal(db_session, from_dt, to_dt, instrument)
    return [_entry_to_response(e) for e in entries]


@router.get("/stats", response_model=JournalStatsResponse)
async def get_journal_stats(
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    db_session=Depends(get_db_session),
    journal_service: JournalService = Depends(get_journal_service),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    stats = await journal_service.get_journal_stats(db_session)
    return JournalStatsResponse(**stats)


@router.patch("/{journal_id}", response_model=JournalResponse)
async def update_journal_entry(
    journal_id: int,
    outcome: str | None = None,
    outcome_notes: str | None = None,
    linked_trade_id: int | None = None,
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    db_session=Depends(get_db_session),
    journal_service: JournalService = Depends(get_journal_service),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    entry = None
    if linked_trade_id is not None:
        entry = await journal_service.link_trade(db_session, journal_id, linked_trade_id)

    if outcome is not None:
        entry = await journal_service.update_outcome(db_session, journal_id, outcome, outcome_notes)

    if entry is None:
        raise HTTPException(status_code=404, detail="Journal entry not found")

    return _entry_to_response(entry)


def _entry_to_response(entry) -> JournalResponse:
    """Convert an AnalysisJournal ORM object to a response model."""
    factors = json.loads(entry.factors) if entry.factors else {}
    trade_idea = json.loads(entry.trade_idea) if entry.trade_idea else None

    return JournalResponse(
        id=entry.id,
        instrument=entry.instrument,
        direction=entry.direction,
        conviction=entry.conviction,
        total_score=entry.total_score,
        factors=factors,
        reasoning=entry.reasoning,
        trade_idea=trade_idea,
        source=entry.source,
        linked_trade_id=entry.linked_trade_id,
        outcome=entry.outcome,
        outcome_notes=entry.outcome_notes,
        created_at=entry.created_at,
    )
