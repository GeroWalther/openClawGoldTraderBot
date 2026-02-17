from fastapi import APIRouter, Depends, Header, HTTPException

from app.dependencies import get_settings, get_trade_executor
from app.models.schemas import TradeSubmitRequest, TradeSubmitResponse
from app.services.trade_executor import TradeExecutor

router = APIRouter(prefix="/api/v1/trades", tags=["trades"])


@router.post("/submit", response_model=TradeSubmitResponse)
async def submit_trade(
    request: TradeSubmitRequest,
    x_api_key: str = Header(...),
    executor: TradeExecutor = Depends(get_trade_executor),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return await executor.submit_trade(request)
