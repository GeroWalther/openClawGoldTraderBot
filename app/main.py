import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import Settings
from app.instruments import INSTRUMENTS
from app.models.database import Base
from app.models.trade import Trade, TradeStatus
from app.services.atr_calculator import ATRCalculator
from app.services.ibkr_client import IBKRClient
from app.services.icmarkets_client import ICMarketsClient
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

    # IBKR Client — only connect if icm is not the sole broker
    ibkr_client = IBKRClient(settings)
    if settings.icm_client_id and not settings.ibkr_enabled:
        # IC Markets is configured and IBKR is disabled — skip IBKR entirely
        logger.info("IBKR disabled (IC Markets only mode)")
        app.state.ibkr_connected = False
    else:
        try:
            await ibkr_client.connect()
            app.state.ibkr_connected = True
        except Exception:
            logger.warning(
                "Could not connect to IB Gateway at %s:%s — IBKR disabled",
                settings.ibkr_host,
                settings.ibkr_port,
            )
            app.state.ibkr_connected = False

    app.state.ibkr_client = ibkr_client
    app.state.settings = settings
    app.state.atr_calculator = ATRCalculator(settings)
    app.state.technical_analyzer = TechnicalAnalyzer()

    # IC Markets Client (cTrader)
    icm_client = ICMarketsClient(settings)
    if settings.icm_client_id:
        try:
            await icm_client.connect()
            app.state.icm_connected = True
        except Exception as e:
            logger.warning("Could not connect to IC Markets — BTC trading disabled: %s", e, exc_info=True)
            app.state.icm_connected = False
    else:
        app.state.icm_connected = False
    app.state.icm_client = icm_client

    # Trade close monitor (background task)
    monitor_task = None
    if app.state.ibkr_connected or app.state.icm_connected:
        notifier = TelegramNotifier(settings)
        monitor = TradeCloseMonitor(
            ibkr_client=ibkr_client,
            icm_client=icm_client,
            session_factory=app.state.async_session,
            notifier=notifier,
            settings=settings,
        )
        monitor_task = asyncio.create_task(monitor.run_forever())

    # Telegram command handler (/status, /pnl) — webhook mode
    telegram_handler = TelegramCommandHandler(
        ibkr_client=ibkr_client,
        icm_client=icm_client,
        session_factory=app.state.async_session,
        settings=settings,
    )
    try:
        await telegram_handler.start()
        app.state.telegram_handler = telegram_handler
    except Exception:
        logger.exception("Failed to start Telegram command handler")
        telegram_handler = None

    # Reconcile orphaned positions on startup
    await _reconcile_positions(
        ibkr_client=ibkr_client,
        icm_client=icm_client,
        session_factory=app.state.async_session,
        ibkr_connected=app.state.ibkr_connected,
        icm_connected=app.state.icm_connected,
    )

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
    await icm_client.disconnect()
    await engine.dispose()
    logger.info("Trader Bot shut down")


async def _reconcile_positions(
    ibkr_client: IBKRClient,
    icm_client: ICMarketsClient,
    session_factory: async_sessionmaker,
    ibkr_connected: bool,
    icm_connected: bool,
):
    """Reconcile broker positions with DB on startup.

    Finds positions on brokers that have no matching EXECUTED trade in the DB
    and creates placeholder trades so the TradeCloseMonitor can track them.
    """
    broker_positions: list[dict] = []

    if ibkr_connected:
        try:
            for pos in await ibkr_client.get_open_positions():
                pos["_broker"] = "ibkr"
                broker_positions.append(pos)
        except Exception:
            logger.warning("Reconciliation: failed to fetch IBKR positions")

    if icm_connected:
        try:
            for pos in await icm_client.get_open_positions():
                pos["_broker"] = "icmarkets"
                broker_positions.append(pos)
        except Exception:
            logger.warning("Reconciliation: failed to fetch IC Markets positions")

    if not broker_positions:
        return

    async with session_factory() as session:
        result = await session.execute(
            select(Trade).where(Trade.status == TradeStatus.EXECUTED)
        )
        db_trades = result.scalars().all()
        # Build set of (epic, direction) from DB
        db_keys = {(t.epic, t.direction) for t in db_trades}

        reconciled = 0
        for pos in broker_positions:
            inst = pos.get("instrument", pos.get("contract", ""))
            direction = pos.get("direction", "")
            if not inst or not direction:
                continue
            if (inst, direction) in db_keys:
                continue  # Already tracked

            entry_price = pos.get("avg_cost", 0.0)
            size = abs(pos.get("size", 0))

            trade = Trade(
                direction=direction,
                epic=inst,
                size=size,
                entry_price=entry_price,
                status=TradeStatus.EXECUTED,
                source="reconciliation",
                strategy="orphaned",
                broker=pos["_broker"],
                claude_reasoning=f"Orphaned position found on {pos['_broker']} at startup",
                created_at=datetime.now(timezone.utc),
            )
            session.add(trade)
            reconciled += 1
            logger.warning(
                "Reconciled orphaned position: %s %s %.4f @ %.5f on %s",
                direction, inst, size, entry_price, pos["_broker"],
            )

        if reconciled:
            await session.commit()
            logger.info("Reconciliation complete: %d orphaned position(s) added to DB", reconciled)


app = FastAPI(title="Trader Bot", version="4.0.0", lifespan=lifespan)

from app.api.router import api_router  # noqa: E402

app.include_router(api_router)
