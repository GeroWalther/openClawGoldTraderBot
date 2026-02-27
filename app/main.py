import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import Settings
from app.models.database import Base
from app.services.atr_calculator import ATRCalculator
from app.services.ibkr_client import IBKRClient
from app.services.technical_analyzer import TechnicalAnalyzer
from app.services.telegram_notifier import TelegramNotifier
from app.services.trade_monitor import TradeCloseMonitor
from app.services.telegram_handler import TelegramCommandHandler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Database
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.state.engine = engine
    app.state.async_session = async_sessionmaker(engine, expire_on_commit=False)

    # IBKR Client
    ibkr_client = IBKRClient(settings)
    try:
        await ibkr_client.connect()
        app.state.ibkr_connected = True
    except Exception:
        logger.warning(
            "Could not connect to IB Gateway at %s:%s — "
            "trading disabled, analysis-only mode",
            settings.ibkr_host,
            settings.ibkr_port,
        )
        app.state.ibkr_connected = False

    app.state.ibkr_client = ibkr_client
    app.state.settings = settings
    app.state.atr_calculator = ATRCalculator(settings)
    app.state.technical_analyzer = TechnicalAnalyzer()

    # Trade close monitor (background task)
    monitor_task = None
    if app.state.ibkr_connected:
        notifier = TelegramNotifier(settings)
        monitor = TradeCloseMonitor(
            ibkr_client=ibkr_client,
            session_factory=app.state.async_session,
            notifier=notifier,
            settings=settings,
        )
        monitor_task = asyncio.create_task(monitor.run_forever())

    # Telegram command handler (/status, /pnl) — webhook mode
    telegram_handler = TelegramCommandHandler(
        ibkr_client=ibkr_client,
        session_factory=app.state.async_session,
        settings=settings,
    )
    try:
        await telegram_handler.start()
        app.state.telegram_handler = telegram_handler
    except Exception:
        logger.exception("Failed to start Telegram command handler")
        telegram_handler = None

    logger.info(
        "Trader Bot v4 started (IBKR %s:%s, connected=%s)",
        settings.ibkr_host,
        settings.ibkr_port,
        app.state.ibkr_connected,
    )
    yield

    # Shutdown
    if telegram_handler:
        await telegram_handler.stop()
    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    await ibkr_client.disconnect()
    await engine.dispose()
    logger.info("Trader Bot shut down")


app = FastAPI(title="Trader Bot", version="4.0.0", lifespan=lifespan)

from app.api.router import api_router  # noqa: E402

app.include_router(api_router)
