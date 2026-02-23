from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import Settings
from app.dependencies import get_db_session, get_settings, get_trade_analytics, get_risk_manager
from app.services.analytics import TradeAnalytics
from app.services.ibkr_client import IBKRClient
from app.services.risk_manager import RiskManager
from app.dependencies import get_ibkr_client
from app.models.schemas import AnalyticsResponse, CooldownStatusResponse

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.get("", response_model=AnalyticsResponse)
async def get_analytics(
    from_date: str | None = None,
    to_date: str | None = None,
    instrument: str | None = None,
    conviction: str | None = None,
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    db_session=Depends(get_db_session),
    analytics: TradeAnalytics = Depends(get_trade_analytics),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    from_dt = datetime.fromisoformat(from_date) if from_date else None
    to_dt = datetime.fromisoformat(to_date) if to_date else None

    result = await analytics.calculate(db_session, from_dt, to_dt, instrument, conviction)
    return AnalyticsResponse(**result)


@router.get("/cooldown", response_model=CooldownStatusResponse)
async def get_cooldown_status(
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    db_session=Depends(get_db_session),
    risk_manager: RiskManager = Depends(get_risk_manager),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        account_info = await ibkr_client.get_account_info()
        balance = account_info.get("NetLiquidation", 10000.0)
    except Exception:
        balance = 10000.0

    status = await risk_manager.get_cooldown_status(db_session, balance)
    return CooldownStatusResponse(**status)
