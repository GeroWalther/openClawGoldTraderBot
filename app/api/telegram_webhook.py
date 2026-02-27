"""Telegram webhook endpoint — receives updates from Telegram's servers.

Secured via X-Telegram-Bot-Api-Secret-Token header, which Telegram sends
on every request when a secret_token is provided during setWebhook.
"""

import hmac

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["telegram"])


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Process incoming Telegram update via webhook."""
    handler = getattr(request.app.state, "telegram_handler", None)
    if handler is None:
        return Response(status_code=503)

    # Verify secret token — rejects forged requests
    expected = request.app.state.settings.telegram_webhook_secret
    if expected:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token, expected):
            return Response(status_code=403)

    data = await request.json()
    await handler.process_update(data)
    return Response(status_code=200)
