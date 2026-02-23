from fastapi import Depends, Request

from app.config import Settings
from app.services.atr_calculator import ATRCalculator
from app.services.ibkr_client import IBKRClient
from app.services.position_sizer import PositionSizer
from app.services.risk_manager import RiskManager
from app.services.session_filter import SessionFilter
from app.services.telegram_notifier import TelegramNotifier
from app.services.trade_executor import TradeExecutor
from app.services.trade_validator import TradeValidator
from app.services.analytics import TradeAnalytics
from app.services.journal import JournalService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_ibkr_client(request: Request) -> IBKRClient:
    return request.app.state.ibkr_client


def get_atr_calculator(request: Request) -> ATRCalculator:
    return request.app.state.atr_calculator


async def get_db_session(request: Request):
    async with request.app.state.async_session() as session:
        yield session


def get_trade_executor(
    request: Request,
    settings: Settings = Depends(get_settings),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    atr_calculator: ATRCalculator = Depends(get_atr_calculator),
    db_session=Depends(get_db_session),
) -> TradeExecutor:
    session_filter = SessionFilter(settings)
    validator = TradeValidator(settings, session_filter=session_filter)
    sizer = PositionSizer(settings)
    notifier = TelegramNotifier(settings)
    risk_manager = RiskManager(settings)
    return TradeExecutor(
        ibkr_client=ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=notifier,
        settings=settings,
        risk_manager=risk_manager,
        atr_calculator=atr_calculator,
    )


def get_trade_analytics() -> TradeAnalytics:
    return TradeAnalytics()


def get_risk_manager(
    settings: Settings = Depends(get_settings),
) -> RiskManager:
    return RiskManager(settings)


def get_journal_service() -> JournalService:
    return JournalService()
