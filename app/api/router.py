from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.trades import router as trades_router
from app.api.positions import router as positions_router
from app.api.analytics import router as analytics_router
from app.api.backtest import router as backtest_router
from app.api.journal import router as journal_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(trades_router)
api_router.include_router(positions_router)
api_router.include_router(analytics_router)
api_router.include_router(backtest_router)
api_router.include_router(journal_router)
