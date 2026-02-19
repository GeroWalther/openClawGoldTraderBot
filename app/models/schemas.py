from datetime import datetime

from pydantic import BaseModel, Field

from app.models.trade import TradeStatus


class TradeSubmitRequest(BaseModel):
    direction: str = Field(..., pattern="^(BUY|SELL)$")
    instrument: str | None = None
    size: float | None = None
    stop_distance: float | None = None
    limit_distance: float | None = None
    stop_level: float | None = None
    limit_level: float | None = None
    source: str = "manual"
    reasoning: str | None = None


class TradeSubmitResponse(BaseModel):
    trade_id: int
    deal_id: str | None
    instrument: str
    status: TradeStatus
    direction: str
    size: float
    stop_distance: float | None
    limit_distance: float | None
    message: str


class PositionResponse(BaseModel):
    deal_id: str
    direction: str
    size: float
    entry_price: float
    current_pnl: float | None
    stop_level: float | None
    limit_level: float | None


class ClosePositionRequest(BaseModel):
    direction: str = Field(..., pattern="^(BUY|SELL)$", description="Direction of the position to close")
    instrument: str | None = None
    size: float | None = Field(None, description="Size to close (omit to close full position)")
    reasoning: str | None = None


class ClosePositionResponse(BaseModel):
    status: str
    instrument: str
    direction: str
    size: float
    close_price: float | None
    pnl: float | None
    message: str


class TradeHistoryItem(BaseModel):
    id: int
    deal_id: str | None
    direction: str
    size: float
    status: TradeStatus
    entry_price: float | None
    pnl: float | None
    created_at: datetime
    closed_at: datetime | None
