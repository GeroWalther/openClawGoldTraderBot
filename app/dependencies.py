from fastapi import Depends, Request

from app.config import Settings
from app.services.ibkr_client import IBKRClient
from app.services.position_sizer import PositionSizer
from app.services.telegram_notifier import TelegramNotifier
from app.services.trade_executor import TradeExecutor
from app.services.trade_validator import TradeValidator


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_ibkr_client(request: Request) -> IBKRClient:
    return request.app.state.ibkr_client


async def get_db_session(request: Request):
    async with request.app.state.async_session() as session:
        yield session


def get_trade_executor(
    request: Request,
    settings: Settings = Depends(get_settings),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    db_session=Depends(get_db_session),
) -> TradeExecutor:
    validator = TradeValidator(settings)
    sizer = PositionSizer(settings)
    notifier = TelegramNotifier(settings)
    return TradeExecutor(
        ibkr_client=ibkr_client,
        validator=validator,
        sizer=sizer,
        db_session=db_session,
        notifier=notifier,
        settings=settings,
    )
