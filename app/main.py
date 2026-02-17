import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import Settings
from app.models.database import Base
from app.services.ibkr_client import IBKRClient

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

    logger.info(
        "Gold Trader v2 started (IBKR %s:%s, connected=%s)",
        settings.ibkr_host,
        settings.ibkr_port,
        app.state.ibkr_connected,
    )
    yield

    await ibkr_client.disconnect()
    await engine.dispose()
    logger.info("Gold Trader shut down")


app = FastAPI(title="Gold Trader", version="2.0.0", lifespan=lifespan)

from app.api.router import api_router  # noqa: E402

app.include_router(api_router)
