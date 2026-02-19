import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db_session, get_ibkr_client, get_settings
from app.models.schemas import ClosePositionRequest, ClosePositionResponse
from app.models.trade import Trade, TradeStatus
from app.services.ibkr_client import IBKRClient
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/positions", tags=["positions"])


@router.get("/")
async def get_positions(
    x_api_key: str = Header(...),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    positions = await ibkr_client.get_open_positions()
    return {"positions": positions}


@router.get("/account")
async def get_account(
    x_api_key: str = Header(...),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    account = await ibkr_client.get_account_info()
    return {"account": account}


@router.post("/close", response_model=ClosePositionResponse)
async def close_position(
    request: ClosePositionRequest,
    x_api_key: str = Header(...),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    db_session: AsyncSession = Depends(get_db_session),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Find the open position matching this direction
    positions = await ibkr_client.get_open_positions()
    matching = [p for p in positions if p["direction"] == request.direction]

    if not matching:
        raise HTTPException(
            status_code=404,
            detail=f"No open {request.direction} position found",
        )

    position = matching[0]
    close_size = request.size or abs(position["size"])

    if close_size > abs(position["size"]):
        raise HTTPException(
            status_code=400,
            detail=f"Close size {close_size} exceeds position size {abs(position['size'])}",
        )

    try:
        result = await ibkr_client.close_position(request.direction, close_size)
        close_price = result.get("fillPrice")
        status = "closed" if result.get("status") == "Filled" else result.get("status", "unknown")

        # Calculate P&L
        pnl = None
        if close_price and position["avg_cost"]:
            if request.direction == "BUY":
                pnl = (close_price - position["avg_cost"]) * close_size
            else:
                pnl = (position["avg_cost"] - close_price) * close_size

        # Update the most recent executed trade for this direction
        stmt = (
            select(Trade)
            .where(Trade.direction == request.direction)
            .where(Trade.status == TradeStatus.EXECUTED)
            .order_by(Trade.id.desc())
            .limit(1)
        )
        result_row = await db_session.execute(stmt)
        trade = result_row.scalar_one_or_none()
        if trade:
            trade.status = TradeStatus.CLOSED
            trade.pnl = pnl
            trade.closed_at = datetime.now(timezone.utc)
            await db_session.commit()

        # Notify via Telegram
        notifier = TelegramNotifier(settings)
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "N/A"
        await notifier.send_message(
            f"Position CLOSED\n"
            f"Direction: {request.direction}\n"
            f"Size: {close_size} oz\n"
            f"Close Price: ${close_price:.2f}\n"
            f"P&L: {pnl_str}"
            + (f"\n\nReasoning: {request.reasoning[:200]}" if request.reasoning else "")
        )

        return ClosePositionResponse(
            status=status,
            direction=request.direction,
            size=close_size,
            close_price=close_price,
            pnl=pnl,
            message=f"Position closed at ${close_price:.2f}" if close_price else "Close order submitted",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to close position")
        raise HTTPException(status_code=500, detail=f"Failed to close position: {e}")
