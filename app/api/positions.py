import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db_session, get_ibkr_client, get_settings
from app.instruments import get_instrument
from app.models.schemas import (
    ClosePositionRequest,
    ClosePositionResponse,
    ModifyPositionRequest,
    ModifyPositionResponse,
    TradeStatusResponse,
)
from app.models.trade import Trade, TradeStatus
from app.services.ibkr_client import IBKRClient
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/positions", tags=["positions"])


@router.get("/")
async def get_positions(
    x_api_key: str = Header(...),
    instrument: str | None = Query(None, description="Filter by instrument key"),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    positions = await ibkr_client.get_open_positions(instrument_key=instrument)
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

    instrument = get_instrument(request.instrument)

    # Find the open position matching this direction and instrument
    positions = await ibkr_client.get_open_positions(instrument_key=instrument.key)
    matching = [p for p in positions if p["direction"] == request.direction]

    if not matching:
        raise HTTPException(
            status_code=404,
            detail=f"No open {request.direction} {instrument.display_name} position found",
        )

    position = matching[0]
    close_size = request.size or abs(position["size"])

    if close_size > abs(position["size"]):
        raise HTTPException(
            status_code=400,
            detail=f"Close size {close_size} exceeds position size {abs(position['size'])}",
        )

    try:
        result = await ibkr_client.close_position(
            request.direction, close_size, instrument_key=instrument.key
        )
        close_price = result.get("fillPrice")
        status = "closed" if result.get("status") == "Filled" else result.get("status", "unknown")

        # Calculate P&L (multiply by instrument multiplier)
        pnl = None
        if close_price and position["avg_cost"]:
            if request.direction == "BUY":
                pnl = (close_price - position["avg_cost"]) * close_size * instrument.multiplier
            else:
                pnl = (position["avg_cost"] - close_price) * close_size * instrument.multiplier

        # Update the most recent executed trade for this direction + instrument
        stmt = (
            select(Trade)
            .where(Trade.direction == request.direction)
            .where(Trade.epic == instrument.key)
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
            f"Position CLOSED — {instrument.display_name}\n"
            f"Direction: {request.direction}\n"
            f"Size: {close_size} {instrument.size_unit}\n"
            f"Close Price: {close_price}\n"
            f"P&L: {pnl_str}"
            + (f"\n\nReasoning: {request.reasoning[:200]}" if request.reasoning else "")
        )

        return ClosePositionResponse(
            status=status,
            instrument=instrument.key,
            direction=request.direction,
            size=close_size,
            close_price=close_price,
            pnl=pnl,
            message=f"Position closed at {close_price}" if close_price else "Close order submitted",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to close position")
        raise HTTPException(status_code=500, detail=f"Failed to close position: {e}")


@router.post("/modify", response_model=ModifyPositionResponse)
async def modify_position(
    request: ModifyPositionRequest,
    x_api_key: str = Header(...),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    db_session: AsyncSession = Depends(get_db_session),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if request.new_stop_loss is None and request.new_take_profit is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of new_stop_loss or new_take_profit must be provided",
        )

    instrument = get_instrument(request.instrument)

    try:
        result = await ibkr_client.modify_sl_tp(
            instrument_key=instrument.key,
            direction=request.direction,
            new_sl=request.new_stop_loss,
            new_tp=request.new_take_profit,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to modify SL/TP")
        raise HTTPException(status_code=500, detail=f"Failed to modify SL/TP: {e}")

    # Update the DB trade record with new SL/TP
    stmt = (
        select(Trade)
        .where(Trade.direction == request.direction)
        .where(Trade.epic == instrument.key)
        .where(Trade.status == TradeStatus.EXECUTED)
        .order_by(Trade.id.desc())
        .limit(1)
    )
    result_row = await db_session.execute(stmt)
    trade = result_row.scalar_one_or_none()
    if trade:
        if request.new_stop_loss is not None:
            trade.stop_loss = request.new_stop_loss
        if request.new_take_profit is not None:
            trade.take_profit = request.new_take_profit
        await db_session.commit()

    # Telegram notification
    notifier = TelegramNotifier(settings)
    await notifier.send_modify_update(
        instrument=instrument,
        direction=request.direction,
        old_sl=result["old_sl"],
        old_tp=result["old_tp"],
        new_sl=result["new_sl"],
        new_tp=result["new_tp"],
    )

    return ModifyPositionResponse(
        status="modified",
        instrument=instrument.key,
        direction=request.direction,
        old_stop_loss=result["old_sl"],
        old_take_profit=result["old_tp"],
        new_stop_loss=result["new_sl"],
        new_take_profit=result["new_tp"],
        message=f"SL/TP modified for {instrument.display_name} {request.direction}",
    )


@router.get("/status", response_model=TradeStatusResponse)
async def get_trade_status(
    x_api_key: str = Header(...),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    db_session: AsyncSession = Depends(get_db_session),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    positions = await ibkr_client.get_open_positions()
    open_orders = await ibkr_client.get_open_orders()
    account = await ibkr_client.get_account_info()
    pending_orders_raw = await ibkr_client.get_pending_orders()

    # Build pending orders section with SL/TP from children
    pending_orders = []
    for po in pending_orders_raw:
        entry = {
            "orderId": po["orderId"],
            "instrument": po["instrument"],
            "direction": po["action"],
            "size": po["totalQuantity"],
            "order_type": "LIMIT" if po["orderType"] == "LMT" else "STOP",
            "entry_price": po["entryPrice"],
            "status": po["status"],
            "stop_loss": None,
            "take_profit": None,
        }
        for child in po.get("children", []):
            if child["orderType"] == "STP":
                entry["stop_loss"] = child["auxPrice"]
            elif child["orderType"] == "LMT":
                # Pick the first LMT as TP (or last if multiple)
                if entry["take_profit"] is None:
                    entry["take_profit"] = child["lmtPrice"]
        pending_orders.append(entry)

    # Enrich each position with SL/TP from open orders and unrealized P&L
    for pos in positions:
        instrument_key = pos["instrument"]
        direction = pos["direction"]
        reverse = "SELL" if direction == "BUY" else "BUY"

        # Find SL (STP) and TP (LMT) child orders for this position
        pos["stop_loss"] = None
        pos["take_profit"] = None
        for order in open_orders:
            if (
                order["instrument"] == instrument_key
                and order["parentId"] > 0
                and order["action"] == reverse
            ):
                if order["orderType"] == "STP":
                    pos["stop_loss"] = order["auxPrice"]
                elif order["orderType"] == "LMT":
                    pos["take_profit"] = order["lmtPrice"]

        # Calculate unrealized P&L using current price
        try:
            spec = get_instrument(instrument_key)
            price_data = await ibkr_client.get_price(instrument_key)
            current_price = price_data.get("last") or price_data.get("bid") or 0
            if current_price and pos["avg_cost"]:
                if direction == "BUY":
                    pos["unrealized_pnl"] = round(
                        (current_price - pos["avg_cost"]) * abs(pos["size"]) * spec.multiplier, 2
                    )
                else:
                    pos["unrealized_pnl"] = round(
                        (pos["avg_cost"] - current_price) * abs(pos["size"]) * spec.multiplier, 2
                    )
                pos["current_price"] = current_price
        except Exception:
            logger.warning("Could not fetch price for %s", instrument_key)

    # Recent trades from DB (last 10)
    stmt = select(Trade).order_by(Trade.id.desc()).limit(10)
    result_row = await db_session.execute(stmt)
    trades = result_row.scalars().all()
    recent_trades = [
        {
            "id": t.id,
            "deal_id": t.deal_id,
            "epic": t.epic,
            "direction": t.direction,
            "size": t.size,
            "status": t.status.value,
            "entry_price": t.entry_price,
            "stop_loss": t.stop_loss,
            "take_profit": t.take_profit,
            "pnl": t.pnl,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in trades
    ]

    return TradeStatusResponse(
        positions=positions,
        pending_orders=pending_orders,
        open_orders=open_orders,
        account=account,
        recent_trades=recent_trades,
    )
