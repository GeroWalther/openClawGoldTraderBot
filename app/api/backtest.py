from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import Settings
from app.dependencies import get_settings
from app.models.schemas import BacktestRequest, BacktestResponse
from app.services.backtester import Backtester
from app.services.macro_data import MacroDataService

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])


@router.post("", response_model=BacktestResponse)
async def run_backtest(
    request: BacktestRequest,
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    backtester = Backtester()
    macro_service = MacroDataService() if request.strategy == "krabbe_scored" else None

    result = backtester.run(
        instrument_key=request.instrument,
        strategy=request.strategy,
        period=request.period,
        initial_balance=request.initial_balance,
        risk_percent=request.risk_percent,
        atr_sl_multiplier=request.atr_sl_multiplier,
        atr_tp_multiplier=request.atr_tp_multiplier,
        session_filter=request.session_filter,
        partial_tp=request.partial_tp,
        macro_service=macro_service,
        start_date=request.start_date,
        end_date=request.end_date,
        max_trades=request.max_trades,
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return BacktestResponse(**result)
