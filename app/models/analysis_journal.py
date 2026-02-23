"""Analysis journal model for recording AI analyses and forward-testing."""

import enum

from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey
from sqlalchemy.sql import func

from app.models.database import Base


class AnalysisJournal(Base):
    __tablename__ = "analysis_journal"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instrument = Column(String, nullable=False)  # XAUUSD, MES, etc.
    direction = Column(String, nullable=False)  # BUY, SELL, NO_TRADE
    conviction = Column(String, nullable=True)  # HIGH, MEDIUM, LOW
    total_score = Column(Float, nullable=False)
    factors = Column(Text, nullable=True)  # JSON-serialized dict of all factor scores
    reasoning = Column(Text, nullable=True)
    trade_idea = Column(Text, nullable=True)  # JSON: {stop_distance, limit_distance, entry_price}
    source = Column(String, default="krabbe")
    linked_trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)
    outcome = Column(String, nullable=True)  # WIN, LOSS, SKIPPED, PENDING
    outcome_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
