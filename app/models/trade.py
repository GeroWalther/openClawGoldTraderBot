import enum

from sqlalchemy import Column, Integer, String, Float, DateTime, Enum as SAEnum
from sqlalchemy.sql import func

from app.models.database import Base


class TradeStatus(str, enum.Enum):
    PENDING = "pending"
    VALIDATED = "validated"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    CLOSED = "closed"
    PENDING_ORDER = "pending_order"
    CANCELLED = "cancelled"


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deal_id = Column(String, nullable=True)
    direction = Column(String, nullable=False)
    epic = Column(String, nullable=False)
    size = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    stop_distance = Column(Float, nullable=True)
    limit_distance = Column(Float, nullable=True)
    status = Column(SAEnum(TradeStatus), default=TradeStatus.PENDING)
    source = Column(String, default="manual")
    claude_reasoning = Column(String, nullable=True)
    rejection_reason = Column(String, nullable=True)
    pnl = Column(Float, nullable=True)
    conviction = Column(String, nullable=True)  # HIGH, MEDIUM, LOW
    expected_price = Column(Float, nullable=True)
    actual_price = Column(Float, nullable=True)
    spread_at_entry = Column(Float, nullable=True)
    order_type = Column(String, default="MARKET")  # MARKET, LIMIT, STOP
    strategy = Column(String, nullable=True)  # intraday, swing, m5_scalp
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
    closed_at = Column(DateTime, nullable=True)
