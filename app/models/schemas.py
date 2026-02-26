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
    conviction: str | None = Field(None, pattern="^(HIGH|MEDIUM|LOW)$")
    source: str = "manual"
    reasoning: str | None = None
    order_type: str | None = Field("MARKET", pattern="^(MARKET|LIMIT|STOP)$")
    entry_price: float | None = None
    strategy: str | None = None


class TradeSubmitResponse(BaseModel):
    trade_id: int
    deal_id: str | None
    instrument: str
    status: TradeStatus
    direction: str
    size: float
    stop_distance: float | None
    limit_distance: float | None
    conviction: str | None = None
    spread_at_entry: float | None = None
    order_type: str | None = None
    entry_price: float | None = None
    strategy: str | None = None
    message: str


class CancelOrderRequest(BaseModel):
    instrument: str | None = None
    direction: str = Field(..., pattern="^(BUY|SELL)$")
    order_id: int | None = None


class CancelOrderResponse(BaseModel):
    status: str
    instrument: str
    direction: str
    cancelled_order_ids: list[int]
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


class ModifyPositionRequest(BaseModel):
    instrument: str | None = None
    direction: str = Field(..., pattern="^(BUY|SELL)$")
    new_stop_loss: float | None = None
    new_take_profit: float | None = None
    new_sl_quantity: float | None = None
    reasoning: str | None = None


class ModifyPositionResponse(BaseModel):
    status: str
    instrument: str
    direction: str
    old_stop_loss: float | None
    old_take_profit: float | None
    new_stop_loss: float | None
    new_take_profit: float | None
    message: str


class TradeStatusResponse(BaseModel):
    positions: list[dict]
    pending_orders: list[dict] = []
    open_orders: list[dict]
    account: dict
    recent_trades: list[dict]


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


class AnalyticsResponse(BaseModel):
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    planned_rr: float | None = None
    achieved_rr: float | None = None
    current_streak: int = 0
    max_win_streak: int = 0
    max_loss_streak: int = 0
    per_instrument: dict[str, dict] = {}
    per_conviction: dict[str, dict] = {}
    per_strategy: dict[str, dict] = {}
    daily_pnl: list[dict] = []
    weekly_pnl: list[dict] = []
    monthly_pnl: list[dict] = []


class CooldownStatusResponse(BaseModel):
    can_trade: bool
    cooldown_active: bool = False
    cooldown_reason: str | None = None
    cooldown_remaining_minutes: float | None = None
    consecutive_losses: int = 0
    daily_trades_count: int = 0
    daily_trades_limit: int = 0
    daily_pnl: float = 0.0
    daily_loss_limit: float = 0.0


class BacktestRequest(BaseModel):
    instrument: str = "XAUUSD"
    strategy: str = Field(..., pattern="^(sma_crossover|rsi_reversal|breakout|krabbe_scored|m5_scalp)$")
    period: str = Field("1y", pattern="^(5d|60d|6mo|1y|2y|5y)$")
    start_date: str | None = None  # "YYYY-MM-DD" — overrides period if set
    end_date: str | None = None    # "YYYY-MM-DD" — overrides period if set
    max_trades: int | None = None  # Stop after N trades
    initial_balance: float = 10000.0
    risk_percent: float = 3.0
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 2.0
    session_filter: bool = True
    partial_tp: bool = True


class BacktestResponse(BaseModel):
    instrument: str
    strategy: str
    period: str
    initial_balance: float
    final_balance: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    expectancy: float
    profit_factor: float
    max_drawdown: float
    total_return_pct: float
    trades: list[dict] = []
    equity_curve: list[dict] = []
    monthly_breakdown: list[dict] = []


class JournalCreateRequest(BaseModel):
    instrument: str
    direction: str = Field(..., pattern="^(BUY|SELL|NO_TRADE)$")
    conviction: str | None = Field(None, pattern="^(HIGH|MEDIUM|LOW)$")
    total_score: float
    factors: dict = {}
    reasoning: str | None = None
    trade_idea: dict | None = None
    source: str = "krabbe"


class JournalResponse(BaseModel):
    id: int
    instrument: str
    direction: str
    conviction: str | None
    total_score: float
    factors: dict = {}
    reasoning: str | None = None
    trade_idea: dict | None = None
    source: str
    linked_trade_id: int | None = None
    outcome: str | None = None
    outcome_notes: str | None = None
    created_at: datetime | None = None


class JournalStatsResponse(BaseModel):
    total_analyses: int = 0
    total_with_outcome: int = 0
    overall_win_rate: float = 0.0
    per_conviction: dict = {}
    avg_score_winners: float = 0.0
    avg_score_losers: float = 0.0
    score_threshold_accuracy: dict = {}
