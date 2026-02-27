"""Telegram webhook endpoint — receives updates from Telegram's servers."""

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["telegram"])


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Process incoming Telegram update via webhook."""
    handler = getattr(request.app.state, "telegram_handler", None)
    if handler is None:
        return Response(status_code=503)

    data = await request.json()
    await handler.process_update(data)
    return Response(status_code=200)
