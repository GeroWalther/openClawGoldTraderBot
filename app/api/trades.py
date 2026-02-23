import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db_session, get_ibkr_client, get_settings, get_trade_executor
from app.instruments import get_instrument
from app.models.schemas import (
    CancelOrderRequest,
    CancelOrderResponse,
    TradeSubmitRequest,
    TradeSubmitResponse,
)
from app.models.trade import Trade, TradeStatus
from app.services.ibkr_client import IBKRClient
from app.services.telegram_notifier import TelegramNotifier
from app.services.trade_executor import TradeExecutor

logger = logging.getLogger(__name__)

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


@router.post("/cancel", response_model=CancelOrderResponse)
async def cancel_order(
    request: CancelOrderRequest,
    x_api_key: str = Header(...),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    db_session: AsyncSession = Depends(get_db_session),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    instrument = get_instrument(request.instrument)
    cancelled_ids: list[int] = []

    if request.order_id is not None:
        # Cancel specific order by ID
        result = await ibkr_client.cancel_order(request.order_id)
        if not result["success"]:
            raise HTTPException(status_code=404, detail=result["error"])
        cancelled_ids.append(request.order_id)
    else:
        # Find pending orders matching instrument + direction
        pending = await ibkr_client.get_pending_orders(instrument_key=instrument.key)
        matching = [p for p in pending if p["action"] == request.direction]
        if not matching:
            raise HTTPException(
                status_code=404,
                detail=f"No pending {request.direction} order found for {instrument.display_name}",
            )
        for order in matching:
            result = await ibkr_client.cancel_order(order["orderId"])
            if result["success"]:
                cancelled_ids.append(order["orderId"])

    if not cancelled_ids:
        raise HTTPException(status_code=500, detail="Failed to cancel any orders")

    # Update DB records for cancelled orders
    for order_id in cancelled_ids:
        stmt = (
            select(Trade)
            .where(Trade.deal_id == str(order_id))
            .where(Trade.status == TradeStatus.PENDING_ORDER)
            .limit(1)
        )
        result_row = await db_session.execute(stmt)
        trade = result_row.scalar_one_or_none()
        if trade:
            trade.status = TradeStatus.CANCELLED
    await db_session.commit()

    # Notify via Telegram
    notifier = TelegramNotifier(settings)
    await notifier.send_cancel_update(
        instrument=instrument,
        direction=request.direction,
        cancelled_order_ids=cancelled_ids,
    )

    return CancelOrderResponse(
        status="cancelled",
        instrument=instrument.key,
        direction=request.direction,
        cancelled_order_ids=cancelled_ids,
        message=f"Cancelled {len(cancelled_ids)} pending order(s) for {instrument.display_name} {request.direction}",
    )
