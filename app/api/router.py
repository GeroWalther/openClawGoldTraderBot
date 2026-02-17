from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.trades import router as trades_router
from app.api.positions import router as positions_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(trades_router)
api_router.include_router(positions_router)
