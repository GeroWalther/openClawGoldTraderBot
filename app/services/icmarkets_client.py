"""IC Markets cTrader Open API client.

Uses Twisted reactor in a background thread, bridged to asyncio via Futures.
Volume convention: cTrader uses volume = units * 100 (so 0.01 BTC = volume 1).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

# Symbol ID mapping — discovered at connect time via ProtoOASymbolsListReq.
# Fallback hardcoded values for IC Markets (may vary by account).
_DEFAULT_SYMBOL_IDS: dict[str, int] = {
    "BTCUSD": 0,  # Resolved dynamically on connect
}

# Volume multiplier: cTrader API uses volume in cents (1 unit = 100 volume)
VOLUME_MULTIPLIER = 100


def _to_volume(size: float) -> int:
    """Convert BTC size (e.g. 0.05) to cTrader volume (e.g. 5)."""
    return int(round(size * VOLUME_MULTIPLIER))


def _from_volume(volume: int) -> float:
    """Convert cTrader volume back to BTC size."""
    return volume / VOLUME_MULTIPLIER


class ICMarketsClient:
    """Async-compatible wrapper around the cTrader Open API (Twisted-based SDK)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._connected = False
        self._client = None
        self._reactor_thread: threading.Thread | None = None
        self._account_id: int = settings.icm_account_id
        self._access_token: str = settings.icm_access_token
        self._refresh_token: str = settings.icm_refresh_token

        # Resolved symbol IDs: instrument_key -> cTrader symbolId
        self._symbol_ids: dict[str, int] = {}

        # Price cache: instrument_key -> {bid, ask, last}
        self._prices: dict[str, dict[str, float]] = {}

        # Track which symbols we've subscribed to spots for
        self._subscribed_symbols: set[str] = set()

        # Symbol metadata: digits, lot size, min volume
        self._symbol_digits: dict[str, int] = {}
        self._symbol_lot_sizes: dict[str, int] = {}  # cTrader lotSize (e.g. 10_000_000 for forex)
        self._symbol_min_volumes: dict[str, int] = {}  # cTrader minVolume

        # Pending response futures: clientMsgId -> asyncio.Future
        self._pending: dict[str, asyncio.Future] = {}
        self._msg_counter = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconnect_lock = asyncio.Lock()
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self):
        """Connect to cTrader, authenticate app + account, resolve symbols."""
        if not self.settings.icm_client_id:
            raise RuntimeError("IC Markets client_id not configured")

        self._loop = asyncio.get_running_loop()

        from ctrader_open_api import Client, TcpProtocol, EndPoints
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAApplicationAuthReq,
            ProtoOAApplicationAuthRes,
            ProtoOAAccountAuthReq,
            ProtoOAAccountAuthRes,
            ProtoOASymbolsListReq,
            ProtoOASymbolsListRes,
            ProtoOASubscribeSpotsReq,
            ProtoOASubscribeSpotsRes,
            ProtoOASpotEvent,
            ProtoOAReconcileReq,
            ProtoOAReconcileRes,
            ProtoOATraderReq,
            ProtoOATraderRes,
            ProtoOANewOrderReq,
            ProtoOAExecutionEvent,
            ProtoOAAmendPositionSLTPReq,
            ProtoOAClosePositionReq,
            ProtoOACancelOrderReq,
            ProtoOAErrorRes,
            ProtoOAOrderErrorEvent,
            ProtoOAGetAccountListByAccessTokenReq,
            ProtoOAGetAccountListByAccessTokenRes,
            ProtoOASymbolByIdReq,
            ProtoOASymbolByIdRes,
            ProtoOADealListByPositionIdReq,
            ProtoOADealListByPositionIdRes,
        )
        from twisted.internet import reactor as twisted_reactor

        # Store refs for later use
        self._twisted_reactor = twisted_reactor
        self._proto_modules = {
            "ProtoOAApplicationAuthReq": ProtoOAApplicationAuthReq,
            "ProtoOAApplicationAuthRes": ProtoOAApplicationAuthRes,
            "ProtoOAAccountAuthReq": ProtoOAAccountAuthReq,
            "ProtoOAAccountAuthRes": ProtoOAAccountAuthRes,
            "ProtoOASymbolsListReq": ProtoOASymbolsListReq,
            "ProtoOASymbolsListRes": ProtoOASymbolsListRes,
            "ProtoOASubscribeSpotsReq": ProtoOASubscribeSpotsReq,
            "ProtoOASubscribeSpotsRes": ProtoOASubscribeSpotsRes,
            "ProtoOASpotEvent": ProtoOASpotEvent,
            "ProtoOAReconcileReq": ProtoOAReconcileReq,
            "ProtoOAReconcileRes": ProtoOAReconcileRes,
            "ProtoOATraderReq": ProtoOATraderReq,
            "ProtoOATraderRes": ProtoOATraderRes,
            "ProtoOANewOrderReq": ProtoOANewOrderReq,
            "ProtoOAExecutionEvent": ProtoOAExecutionEvent,
            "ProtoOAAmendPositionSLTPReq": ProtoOAAmendPositionSLTPReq,
            "ProtoOAClosePositionReq": ProtoOAClosePositionReq,
            "ProtoOACancelOrderReq": ProtoOACancelOrderReq,
            "ProtoOAErrorRes": ProtoOAErrorRes,
            "ProtoOAOrderErrorEvent": ProtoOAOrderErrorEvent,
            "ProtoHeartbeatEvent": ProtoHeartbeatEvent,
            "ProtoOASymbolByIdReq": ProtoOASymbolByIdReq,
            "ProtoOASymbolByIdRes": ProtoOASymbolByIdRes,
            "ProtoOADealListByPositionIdReq": ProtoOADealListByPositionIdReq,
            "ProtoOADealListByPositionIdRes": ProtoOADealListByPositionIdRes,
        }

        # Determine host
        host = self.settings.icm_host
        port = self.settings.icm_port
        if "demo" in host.lower():
            from ctrader_open_api import EndPoints as EP
            host = EP.PROTOBUF_DEMO_HOST
        else:
            from ctrader_open_api import EndPoints as EP
            host = EP.PROTOBUF_LIVE_HOST

        self._client = Client(host, port, TcpProtocol)
        self._client.setConnectedCallback(self._on_connected)
        self._client.setDisconnectedCallback(self._on_disconnected)
        self._client.setMessageReceivedCallback(self._on_message)

        # Start Twisted reactor in background thread
        if not self._reactor_thread or not self._reactor_thread.is_alive():
            self._reactor_thread = threading.Thread(
                target=self._run_reactor,
                args=(twisted_reactor,),
                daemon=True,
                name="twisted-reactor",
            )
            self._reactor_thread.start()

        # Start client service (must be called from reactor thread)
        connect_future = self._loop.create_future()
        self._pending["__connect__"] = connect_future

        self._twisted_reactor.callFromThread(self._client.startService)

        # Wait for app auth + account auth to complete
        await asyncio.wait_for(connect_future, timeout=30.0)
        self._connected = True

        # Resolve symbol IDs
        await self._resolve_symbols()

        # Subscribe to spot prices for all resolved instruments
        for key in self._symbol_ids:
            try:
                await self._subscribe_spots(key)
            except Exception:
                logger.warning("Failed to subscribe spots for %s", key)

        # Start background token refresh task
        if self._refresh_token:
            self._refresh_task = asyncio.create_task(self._token_refresh_loop())

        logger.info(
            "Connected to IC Markets cTrader (%s:%s, account=%s, symbols=%s)",
            host, port, self._account_id, list(self._symbol_ids.keys()),
        )

    def _run_reactor(self, reactor):
        """Run Twisted reactor in background thread."""
        try:
            reactor.run(installSignalHandlers=False)
        except Exception:
            logger.exception("Twisted reactor crashed")

    def _on_connected(self, client):
        """Twisted callback: TCP connected → send app auth."""
        logger.info("cTrader TCP connected, authenticating app...")
        Req = self._proto_modules["ProtoOAApplicationAuthReq"]
        request = Req()
        request.clientId = self.settings.icm_client_id
        request.clientSecret = self.settings.icm_client_secret
        deferred = client.send(request)
        deferred.addErrback(self._on_error)

    def _on_disconnected(self, client, reason):
        """Twisted callback: TCP disconnected — auto-reconnect unless shutting down."""
        logger.warning("cTrader disconnected: %s", reason)
        self._connected = False
        self._subscribed_symbols.clear()  # Must resubscribe after reconnect
        # Schedule auto-reconnect with backoff
        if self._loop and not self._shutting_down:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future, self._auto_reconnect()
            )

    def _on_error(self, failure):
        """Twisted errback."""
        logger.error("cTrader error: %s", failure)

    def _on_message(self, client, message):
        """Twisted callback: process all incoming messages."""
        from ctrader_open_api import Protobuf

        payload_type = message.payloadType
        extracted = Protobuf.extract(message)
        client_msg_id = getattr(message, "clientMsgId", "") or ""

        # Route by payload type
        AppAuthRes = self._proto_modules["ProtoOAApplicationAuthRes"]
        AccAuthRes = self._proto_modules["ProtoOAAccountAuthRes"]
        SpotEvent = self._proto_modules["ProtoOASpotEvent"]
        ErrorRes = self._proto_modules["ProtoOAErrorRes"]
        OrderError = self._proto_modules["ProtoOAOrderErrorEvent"]
        HeartbeatEvent = self._proto_modules["ProtoHeartbeatEvent"]

        if payload_type == HeartbeatEvent().payloadType:
            # Respond to server heartbeat to keep connection alive
            try:
                response = HeartbeatEvent()
                client.send(response)
            except Exception:
                pass
            return

        if payload_type == AppAuthRes().payloadType:
            logger.info("cTrader app authenticated, authenticating account...")
            self._send_account_auth()
            return

        if payload_type == AccAuthRes().payloadType:
            logger.info("cTrader account %s authenticated", self._account_id)
            # Resolve connect future
            if "__connect__" in self._pending:
                fut = self._pending.pop("__connect__")
                self._loop.call_soon_threadsafe(fut.set_result, True)
            return

        if payload_type == SpotEvent().payloadType:
            self._handle_spot_event(extracted)
            return

        if payload_type == ErrorRes().payloadType:
            error_code = getattr(extracted, "errorCode", "UNKNOWN")
            description = getattr(extracted, "description", "")
            # ALREADY_LOGGED_IN on reconnect — skip app auth, proceed to account auth
            if error_code == "ALREADY_LOGGED_IN":
                logger.info("cTrader app already authenticated, proceeding to account auth...")
                self._send_account_auth()
                return
            logger.error("cTrader error: %s — %s", error_code, description)
            if client_msg_id and client_msg_id in self._pending:
                fut = self._pending.pop(client_msg_id)
                self._loop.call_soon_threadsafe(
                    fut.set_exception,
                    RuntimeError(f"cTrader error {error_code}: {description}"),
                )
            return

        if payload_type == OrderError().payloadType:
            error_code = getattr(extracted, "errorCode", "UNKNOWN")
            description = getattr(extracted, "description", "")
            logger.error("cTrader order error: %s — %s", error_code, description)
            if client_msg_id and client_msg_id in self._pending:
                fut = self._pending.pop(client_msg_id)
                self._loop.call_soon_threadsafe(
                    fut.set_exception,
                    RuntimeError(f"cTrader order error {error_code}: {description}"),
                )
            return

        # Generic response — resolve any pending future
        if client_msg_id and client_msg_id in self._pending:
            fut = self._pending.pop(client_msg_id)
            self._loop.call_soon_threadsafe(fut.set_result, extracted)
            return

        # Execution events (fills) — may not have clientMsgId
        ExecEvent = self._proto_modules["ProtoOAExecutionEvent"]
        if payload_type == ExecEvent().payloadType:
            self._handle_execution_event(extracted)
            return

    def _send_account_auth(self):
        """Send account auth request (called from Twisted thread)."""
        Req = self._proto_modules["ProtoOAAccountAuthReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id
        request.accessToken = self._access_token
        deferred = self._client.send(request)
        deferred.addErrback(self._on_error)

    def _handle_spot_event(self, event):
        """Update price cache from spot event."""
        symbol_id = event.symbolId
        # Reverse lookup: symbolId -> instrument_key
        inst_key = None
        for key, sid in self._symbol_ids.items():
            if sid == symbol_id:
                inst_key = key
                break
        if inst_key is None:
            return

        bid = getattr(event, "bid", 0) or 0
        ask = getattr(event, "ask", 0) or 0

        # cTrader spot event bid/ask are always uint64 divided by 100000 (10^5)
        # regardless of the symbol's digits field. Round to digits for display.
        # Ref: https://help.ctrader.com/open-api/symbol-data/
        digits = self._symbol_digits.get(inst_key, 2)
        if bid > 0:
            bid = round(bid / 100000, digits)
        if ask > 0:
            ask = round(ask / 100000, digits)

        last = (bid + ask) / 2 if bid > 0 and ask > 0 else bid or ask

        self._prices[inst_key] = {"bid": bid, "ask": ask, "last": last}

    def _handle_execution_event(self, event):
        """Handle execution event (order fills, etc.)."""
        exec_type = getattr(event, "executionType", None)
        order = getattr(event, "order", None)
        position = getattr(event, "position", None)

        if order:
            client_msg_id = getattr(order, "label", "") or ""
            if client_msg_id and client_msg_id in self._pending:
                fut = self._pending.pop(client_msg_id)
                self._loop.call_soon_threadsafe(fut.set_result, event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_msg_id(self) -> str:
        self._msg_counter += 1
        return f"msg_{self._msg_counter}"

    async def _send_request(self, request, client_msg_id: str | None = None, timeout: float = 15.0) -> Any:
        """Send a protobuf request and wait for the response."""
        if not self._connected and client_msg_id != "__connect__":
            raise RuntimeError("IC Markets client not connected")

        msg_id = client_msg_id or self._next_msg_id()
        future = self._loop.create_future()
        self._pending[msg_id] = future

        def _send():
            deferred = self._client.send(request, clientMsgId=msg_id)
            deferred.addErrback(self._on_error)

        self._twisted_reactor.callFromThread(_send)
        return await asyncio.wait_for(future, timeout=timeout)

    async def _resolve_symbols(self):
        """Fetch symbol list, then full symbol details for correct digits."""
        Req = self._proto_modules["ProtoOASymbolsListReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id

        response = await self._send_request(request)

        self._symbol_digits = {}  # instrument_key -> digits (decimal places)

        # Map known symbols — key is our instrument key, values are cTrader symbol names
        symbol_map = {
            "BTC": ["BTCUSD", "BTC/USD"],
            "XAUUSD": ["XAUUSD", "XAU/USD"],
            "EURUSD": ["EURUSD", "EUR/USD"],
            "NZDUSD": ["NZDUSD", "NZD/USD"],
            "AUDUSD": ["AUDUSD", "AUD/USD"],
            "USDJPY": ["USDJPY", "USD/JPY"],
            "GBPUSD": ["GBPUSD", "GBP/USD"],
            "EURJPY": ["EURJPY", "EUR/JPY"],
            "CADJPY": ["CADJPY", "CAD/JPY"],
        }

        # Phase 1: resolve symbolIds from the light symbol list
        resolved_ids = []  # (inst_key, symbolId)
        for symbol in response.symbol:
            name = getattr(symbol, "symbolName", "")
            sid = symbol.symbolId

            for inst_key, names in symbol_map.items():
                if name in names:
                    self._symbol_ids[inst_key] = sid
                    resolved_ids.append((inst_key, sid))

        if not self._symbol_ids:
            logger.warning("No IC Markets symbols resolved — check symbol names")
            return

        # Phase 2: fetch full symbol details for correct digits
        ByIdReq = self._proto_modules["ProtoOASymbolByIdReq"]
        detail_req = ByIdReq()
        detail_req.ctidTraderAccountId = self._account_id
        for _, sid in resolved_ids:
            detail_req.symbolId.append(sid)

        try:
            detail_res = await self._send_request(detail_req)
            for full_symbol in detail_res.symbol:
                sid = full_symbol.symbolId
                digits = getattr(full_symbol, "digits", 2)
                lot_size = getattr(full_symbol, "lotSize", 100)
                min_volume = getattr(full_symbol, "minVolume", 1)
                for inst_key, resolved_sid in resolved_ids:
                    if resolved_sid == sid:
                        self._symbol_digits[inst_key] = digits
                        self._symbol_lot_sizes[inst_key] = lot_size
                        self._symbol_min_volumes[inst_key] = min_volume
                        logger.info(
                            "Resolved %s → symbolId=%d (digits=%d, lotSize=%d, minVolume=%d)",
                            inst_key, sid, digits, lot_size, min_volume,
                        )
        except Exception as e:
            logger.warning("Could not fetch full symbol details, using defaults: %s", e)
            for inst_key, sid in resolved_ids:
                self._symbol_digits[inst_key] = 2
                self._symbol_lot_sizes[inst_key] = 100
                self._symbol_min_volumes[inst_key] = 1
                logger.info("Resolved %s → symbolId=%d (defaults)", inst_key, sid)

    async def _subscribe_spots(self, instrument_key: str):
        """Subscribe to real-time spot prices for an instrument."""
        if instrument_key not in self._symbol_ids:
            logger.warning("Cannot subscribe spots for %s — symbolId unknown", instrument_key)
            return

        # Skip if already subscribed
        if instrument_key in self._subscribed_symbols:
            return

        Req = self._proto_modules["ProtoOASubscribeSpotsReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id
        request.symbolId.append(self._symbol_ids[instrument_key])

        try:
            await self._send_request(request)
        except RuntimeError as e:
            if "ALREADY_SUBSCRIBED" in str(e):
                logger.debug("Already subscribed to %s spots", instrument_key)
            else:
                raise

        self._subscribed_symbols.add(instrument_key)
        logger.info("Subscribed to spot prices for %s", instrument_key)

    def _get_symbol_id(self, instrument_key: str) -> int:
        """Get cTrader symbolId for an instrument key."""
        if instrument_key not in self._symbol_ids:
            raise RuntimeError(f"Symbol not resolved for {instrument_key}")
        return self._symbol_ids[instrument_key]

    def _size_to_volume(self, size: float, instrument_key: str) -> int:
        """Convert trade size (units) to cTrader volume.

        cTrader volume = units * 100 for all instruments:
        - BTC: 0.01 BTC → volume 1
        - Forex: 1000 AUD → volume 100,000
        - Gold: 1 oz → volume 100
        """
        return int(round(size * VOLUME_MULTIPLIER))

    def _volume_to_size(self, volume: int, instrument_key: str) -> float:
        """Convert cTrader volume back to trade size (units)."""
        return volume / VOLUME_MULTIPLIER

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def _token_refresh_loop(self):
        """Refresh the access token every 25 days (tokens expire in ~30 days)."""
        REFRESH_INTERVAL = 25 * 24 * 3600  # 25 days in seconds
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            try:
                await self._refresh_access_token()
            except Exception:
                logger.exception("Token refresh failed — will retry in 1 hour")
                await asyncio.sleep(3600)

    async def _refresh_access_token(self):
        """Use refresh token to get a new access token from cTrader OAuth."""
        import httpx

        url = "https://openapi.ctrader.com/apps/token"
        params = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self.settings.icm_client_id,
            "client_secret": self.settings.icm_client_secret,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        new_access = data.get("accessToken")
        new_refresh = data.get("refreshToken")

        if not new_access:
            raise RuntimeError(f"Token refresh returned no accessToken: {data}")

        old_token = self._access_token[:8] + "..."
        self._access_token = new_access
        if new_refresh:
            self._refresh_token = new_refresh

        # Update .env file so new tokens persist across restarts
        self._persist_tokens(new_access, new_refresh)

        logger.info("Access token refreshed (%s → %s...)", old_token, new_access[:8])

        # Reconnect with new token
        if self._connected:
            await self.disconnect()
            await asyncio.sleep(2)
            await self.connect()

    def _persist_tokens(self, access_token: str, refresh_token: str | None):
        """Update .env file with new tokens so they survive restarts."""
        import re
        from pathlib import Path

        for env_path in [Path(".env"), Path(".env.production"), Path("/app/.env"), Path("/app/.env.production")]:
            if not env_path.exists():
                continue
            try:
                content = env_path.read_text()
                content = re.sub(
                    r"(?m)^icm_access_token=.*$",
                    f"icm_access_token={access_token}",
                    content,
                )
                if refresh_token:
                    content = re.sub(
                        r"(?m)^icm_refresh_token=.*$",
                        f"icm_refresh_token={refresh_token}",
                        content,
                    )
                env_path.write_text(content)
                logger.info("Updated tokens in %s", env_path)
            except Exception as e:
                logger.warning("Could not update %s: %s", env_path, e)

    # ------------------------------------------------------------------
    # Public API (matches IBKRClient interface)
    # ------------------------------------------------------------------

    async def _auto_reconnect(self, max_retries: int = 5):
        """Auto-reconnect with exponential backoff after unexpected disconnect."""
        for attempt in range(max_retries):
            delay = min(2 ** attempt, 30)  # 1, 2, 4, 8, 16, 30s
            await asyncio.sleep(delay)
            if self._connected or self._shutting_down:
                return
            try:
                await self._reconnect()
                logger.info("IC Markets reconnected after %d attempt(s)", attempt + 1)
                return
            except Exception as e:
                logger.warning("Reconnect attempt %d/%d failed: %s", attempt + 1, max_retries, e)
        logger.error("IC Markets: gave up reconnecting after %d attempts", max_retries)

    async def disconnect(self):
        """Disconnect from cTrader."""
        self._shutting_down = True
        if hasattr(self, "_refresh_task") and self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self._connected and self._client:
            self._connected = False
            try:
                self._twisted_reactor.callFromThread(self._client.stopService)
            except Exception:
                pass
            logger.info("Disconnected from IC Markets cTrader")

    async def ensure_connected(self):
        """Reconnect if connection was lost (with lock to prevent concurrent reconnects)."""
        if self._connected:
            return
        async with self._reconnect_lock:
            if self._connected:  # Re-check after acquiring lock
                return
            logger.warning("IC Markets connection lost, reconnecting...")
            try:
                await self._reconnect()
            except Exception as e:
                logger.error("IC Markets reconnect failed: %s", e)
                raise RuntimeError("IC Markets not connected") from e

    async def _reconnect(self):
        """Reconnect by restarting the client service."""
        self._loop = asyncio.get_running_loop()

        # Stop existing service first
        if self._client:
            try:
                self._twisted_reactor.callFromThread(self._client.stopService)
            except Exception:
                pass
            await asyncio.sleep(2)

        # Clear stale state
        self._subscribed_symbols.clear()
        for msg_id, fut in list(self._pending.items()):
            if not fut.done():
                self._loop.call_soon_threadsafe(
                    fut.set_exception, RuntimeError("Reconnecting")
                )
        self._pending.clear()

        # Restart service — _on_connected will fire app auth → account auth
        connect_future = self._loop.create_future()
        self._pending["__connect__"] = connect_future

        self._twisted_reactor.callFromThread(self._client.startService)

        await asyncio.wait_for(connect_future, timeout=30.0)
        self._connected = True

        # Re-resolve symbols and resubscribe
        await self._resolve_symbols()
        for key in list(self._symbol_ids.keys()):
            if key in self._prices:  # Was previously subscribed
                await self._subscribe_spots(key)

    async def get_price(self, instrument_key: str = "BTC") -> dict:
        """Get current bid/ask/last for an instrument from cached spot data."""
        await self.ensure_connected()

        # Return cached price if available
        if instrument_key in self._prices:
            return self._prices[instrument_key]

        # If no cached price, request a snapshot via reconcile or wait briefly
        # First try subscribing if not yet subscribed
        if instrument_key not in self._symbol_ids:
            raise RuntimeError(f"Symbol not available for {instrument_key}")

        await self._subscribe_spots(instrument_key)

        # Wait for price data to arrive
        for _ in range(50):  # 5 seconds max
            await asyncio.sleep(0.1)
            if instrument_key in self._prices:
                return self._prices[instrument_key]

        return {"bid": 0.0, "ask": 0.0, "last": 0.0}

    def usd_to_eur(self, usd_amount: float) -> float:
        """Convert a USD amount to EUR using cached EURUSD spot price.

        Falls back to returning the USD amount unchanged if no rate available.
        """
        spot = self._prices.get("EURUSD")
        if spot:
            rate = spot.get("bid") or spot.get("ask") or spot.get("last")
            if rate and rate > 0:
                return usd_amount / rate
        return usd_amount

    async def get_account_info(self) -> dict:
        """Get account balance/equity info."""
        await self.ensure_connected()

        Req = self._proto_modules["ProtoOATraderReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id

        response = await self._send_request(request)
        trader = response.trader

        # cTrader returns balance in cents — divide by 100
        balance = getattr(trader, "balance", 0) / 100.0

        return {
            "NetLiquidation": balance,
            "TotalCashValue": balance,
            "AvailableFunds": balance,
            "BuyingPower": balance,
            "MaintMarginReq": 0.0,
            "GrossPositionValue": 0.0,
            "broker": "icmarkets",
            "accounts": [str(self._account_id)],
        }

    async def open_position(
        self,
        direction: str,
        size: float,
        stop_distance: float | None = None,
        take_profit_price: float | None = None,
        instrument_key: str = "BTC",
        stop_price: float | None = None,
    ) -> dict:
        """Open a market position with optional SL/TP."""
        await self.ensure_connected()

        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
            ProtoOAOrderType,
            ProtoOATradeSide,
        )

        Req = self._proto_modules["ProtoOANewOrderReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id
        request.symbolId = self._get_symbol_id(instrument_key)
        request.orderType = ProtoOAOrderType.MARKET
        request.tradeSide = ProtoOATradeSide.BUY if direction == "BUY" else ProtoOATradeSide.SELL
        request.volume = self._size_to_volume(size, instrument_key)

        msg_id = self._next_msg_id()
        request.label = msg_id  # Used to correlate execution events

        # MARKET orders: cTrader doesn't allow absolute SL/TP, so we open
        # without and amend immediately after fill.

        response = await self._send_request(request, client_msg_id=msg_id, timeout=30.0)

        # Extract fill info from execution event
        position = getattr(response, "position", None)
        order = getattr(response, "order", None)

        fill_price = None
        position_id = None
        if position:
            fill_price = getattr(position, "price", None)  # double — already actual price
            position_id = getattr(position, "positionId", None)

        # If a position was created, the order filled successfully
        status = "Filled" if position_id else "Failed"

        # Set SL/TP via amend after fill
        if position_id and (stop_price is not None or take_profit_price is not None):
            try:
                AmendReq = self._proto_modules["ProtoOAAmendPositionSLTPReq"]
                amend = AmendReq()
                amend.ctidTraderAccountId = self._account_id
                amend.positionId = position_id
                if stop_price is not None:
                    amend.stopLoss = stop_price
                if take_profit_price is not None:
                    amend.takeProfit = take_profit_price
                amend_id = self._next_msg_id()
                await self._send_request(amend, client_msg_id=amend_id, timeout=10.0)
                logger.info("Set SL=%s TP=%s on position %s", stop_price, take_profit_price, position_id)
            except Exception as e:
                logger.warning("Could not set SL/TP after fill: %s — trade monitor will handle", e)

        return {
            "orderId": position_id or 0,
            "status": status,
            "direction": direction,
            "size": size,
            "fillPrice": fill_price,
            "dealId": str(position_id) if position_id else None,
            "positionId": position_id,
        }

    async def open_position_with_runner(
        self,
        direction: str,
        size: float,
        stop_price: float,
        tp1_price: float,
        tp1_size: float,
        runner_size: float,
        instrument_key: str = "BTC",
        stop_distance: float = 0,
        r_distance: float = 0,
    ) -> dict:
        """Open position with TP1 + runner (SL only, no TP2).

        cTrader handles SL/TP per-position, so we:
        1. Open full position with SL and TP1 as TP
        2. After TP1 fills (partial close), amend remaining position's TP to None
        The trade monitor handles trailing the SL for the runner.

        For simplicity, we open with SL only — TP1 is handled by trade monitor.
        """
        result = await self.open_position(
            direction=direction,
            size=size,
            stop_price=stop_price,
            take_profit_price=None,  # Runner mode — no fixed TP
            instrument_key=instrument_key,
        )

        # If filled, set TP1 via amend for the partial close
        if result.get("positionId") and tp1_price:
            try:
                await self.modify_sl_tp(
                    instrument_key=instrument_key,
                    direction=direction,
                    new_sl=stop_price,
                    new_tp=tp1_price,
                )
            except Exception:
                logger.warning("Could not set TP1 after fill — monitor will handle")

        return result

    async def open_position_with_partial_tp(
        self,
        direction: str,
        size: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
        tp1_size: float,
        tp2_size: float,
        instrument_key: str = "BTC",
    ) -> dict:
        """Open position with partial TP.

        cTrader doesn't support split TP natively on one position.
        Open with SL + TP2 (full TP), monitor handles partial close at TP1.
        """
        return await self.open_position(
            direction=direction,
            size=size,
            stop_price=stop_price,
            take_profit_price=tp2_price,
            instrument_key=instrument_key,
        )

    async def open_pending_position(
        self,
        direction: str,
        size: float,
        entry_price: float,
        order_type: str,
        stop_price: float,
        take_profit_price: float,
        instrument_key: str = "BTC",
    ) -> dict:
        """Place a pending (limit/stop) order with SL/TP."""
        await self.ensure_connected()

        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
            ProtoOAOrderType,
            ProtoOATradeSide,
        )

        Req = self._proto_modules["ProtoOANewOrderReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id
        request.symbolId = self._get_symbol_id(instrument_key)
        request.tradeSide = ProtoOATradeSide.BUY if direction == "BUY" else ProtoOATradeSide.SELL
        request.volume = self._size_to_volume(size, instrument_key)

        if order_type == "LIMIT":
            request.orderType = ProtoOAOrderType.LIMIT
            request.limitPrice = entry_price
        else:
            request.orderType = ProtoOAOrderType.STOP
            request.stopPrice = entry_price

        if stop_price is not None:
            request.stopLoss = stop_price
        if take_profit_price is not None:
            request.takeProfit = take_profit_price

        msg_id = self._next_msg_id()
        request.label = msg_id

        response = await self._send_request(request, client_msg_id=msg_id, timeout=15.0)

        order = getattr(response, "order", None)
        order_id = getattr(order, "orderId", 0) if order else 0

        return {
            "orderId": order_id,
            "status": "PreSubmitted",
            "direction": direction,
            "size": size,
            "fillPrice": None,
            "dealId": str(order_id),
        }

    async def open_pending_position_with_partial_tp(
        self,
        direction: str,
        size: float,
        entry_price: float,
        order_type: str,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
        tp1_size: float,
        tp2_size: float,
        instrument_key: str = "BTC",
    ) -> dict:
        """Place pending order — cTrader uses single TP, so use tp2 (full TP)."""
        return await self.open_pending_position(
            direction=direction,
            size=size,
            entry_price=entry_price,
            order_type=order_type,
            stop_price=stop_price,
            take_profit_price=tp2_price,
            instrument_key=instrument_key,
        )

    async def close_position(
        self, direction: str, size: float, instrument_key: str = "BTC"
    ) -> dict:
        """Close a position by positionId."""
        await self.ensure_connected()

        # Find the position to close
        positions = await self.get_open_positions(instrument_key=instrument_key)
        matching = [p for p in positions if p["direction"] == direction]
        if not matching:
            raise RuntimeError(f"No open {direction} {instrument_key} position to close")

        position_id = matching[0]["positionId"]

        Req = self._proto_modules["ProtoOAClosePositionReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id
        request.positionId = position_id
        request.volume = self._size_to_volume(size, instrument_key)

        msg_id = self._next_msg_id()
        response = await self._send_request(request, client_msg_id=msg_id, timeout=30.0)

        # Extract close (fill) price from the order execution, not the position
        order = getattr(response, "order", None)
        position = getattr(response, "position", None)
        close_price = None

        if order:
            # order.executionPrice is the actual fill price for the close
            exec_price = getattr(order, "executionPrice", None)
            if exec_price:
                digits = self._symbol_digits.get(instrument_key, 5)
                close_price = round(exec_price / 100000, digits)
                logger.info("Close fill price from order.executionPrice: %s", close_price)

        # Fallback: use cached spot price (position.price is the ENTRY price, not fill)
        if not close_price:
            spot = self._prices.get(instrument_key)
            if spot:
                close_price = spot["ask"] if direction == "SELL" else spot["bid"]
                logger.info("Close fill price from spot fallback: %s", close_price)

        # Get actual P&L and close price from broker deal history
        details = await self.get_position_close_details(position_id)
        broker_pnl = details["pnl"] if details else None
        if details and details.get("close_price"):
            close_price = details["close_price"]

        return {
            "orderId": position_id,
            "status": "Filled",
            "direction": direction,
            "size": size,
            "fillPrice": close_price,
            "dealId": str(position_id),
            "pnl": broker_pnl,
        }

    async def get_position_close_details(self, position_id: int) -> dict | None:
        """Get actual realized P&L and close price from cTrader deal history.

        Returns dict with 'pnl' (in account currency EUR, includes swap/commission)
        and 'close_price', or None if unavailable.
        """
        try:
            Req = self._proto_modules["ProtoOADealListByPositionIdReq"]
            request = Req()
            request.ctidTraderAccountId = self._account_id
            request.positionId = position_id

            response = await self._send_request(request, timeout=10.0)

            total_pnl = 0.0
            close_price = None
            has_close = False
            for deal in response.deal:
                # closePositionDetail contains P&L for closing deals
                close_detail = getattr(deal, "closePositionDetail", None)
                if close_detail:
                    has_close = True
                    # All values in cents of account currency
                    gross = getattr(close_detail, "grossProfit", 0) / 100.0
                    swap = getattr(close_detail, "swap", 0) / 100.0
                    commission = getattr(close_detail, "commission", 0) / 100.0
                    total_pnl += gross + swap + commission
                    logger.info(
                        "Deal P&L for position %s: gross=%.2f swap=%.2f commission=%.2f total=%.2f",
                        position_id, gross, swap, commission, total_pnl,
                    )
                # Get execution price from the closing deal
                exec_price = getattr(deal, "executionPrice", None)
                if exec_price and close_detail:
                    digits = 5  # default for forex
                    close_price = exec_price / 100000

            if has_close:
                return {"pnl": round(total_pnl, 2), "close_price": close_price}
            return None
        except Exception:
            logger.warning("Failed to get deal details for position %s", position_id, exc_info=True)
            return None

    async def get_open_positions(
        self, instrument_key: str | None = None
    ) -> list[dict]:
        """Get open positions via ProtoOAReconcileReq."""
        await self.ensure_connected()

        Req = self._proto_modules["ProtoOAReconcileReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id

        response = await self._send_request(request)

        # Reverse symbol ID map
        sid_to_key = {v: k for k, v in self._symbol_ids.items()}

        result = []
        for pos in response.position:
            pos_symbol_id = pos.tradeData.symbolId
            inst_key = sid_to_key.get(pos_symbol_id)
            if inst_key is None:
                continue
            if instrument_key and inst_key != instrument_key:
                continue

            price = getattr(pos, "price", 0)  # double — already actual price

            is_buy = pos.tradeData.tradeSide == 1  # BUY=1, SELL=2
            size = self._volume_to_size(pos.tradeData.volume, inst_key)

            # Read broker-reported swap and commission (in account currency cents)
            swap = getattr(pos, "swap", 0) / 100.0
            commission = getattr(pos, "commission", 0) / 100.0

            # Log all position fields once for debugging
            logger.debug(
                "Position %s fields: %s",
                pos.positionId,
                {f.name: getattr(pos, f.name) for f in pos.DESCRIPTOR.fields},
            )

            # Ensure spot price subscription for this instrument
            if inst_key not in self._prices:
                try:
                    await self._subscribe_spots(inst_key)
                    for _ in range(20):  # wait up to 2s
                        await asyncio.sleep(0.1)
                        if inst_key in self._prices:
                            break
                except Exception:
                    pass

            # Calculate unrealized P&L from cached spot price (converted to EUR)
            unrealized_pnl = None
            current_price = None
            spot = self._prices.get(inst_key)
            if spot and price:
                current_price = spot["bid"] if is_buy else spot["ask"]
                if current_price and current_price > 0:
                    if is_buy:
                        raw_pnl = (current_price - price) * size
                    else:
                        raw_pnl = (price - current_price) * size
                    unrealized_pnl = round(self.usd_to_eur(raw_pnl), 2)

            result.append({
                "instrument": inst_key,
                "symbol": inst_key,
                "size": size if is_buy else -size,
                "direction": "BUY" if is_buy else "SELL",
                "avg_cost": price,
                "unrealized_pnl": unrealized_pnl,
                "current_price": current_price,
                "size_unit": "lots",
                "positionId": pos.positionId,
                "stopLoss": getattr(pos, "stopLoss", None),
                "takeProfit": getattr(pos, "takeProfit", None),
            })
        return result

    async def get_open_orders(
        self, instrument_key: str | None = None
    ) -> list[dict]:
        """Get open orders (not yet filled)."""
        return await self.get_pending_orders(instrument_key=instrument_key)

    async def get_pending_orders(
        self, instrument_key: str | None = None
    ) -> list[dict]:
        """Get pending (unfilled) orders via ProtoOAReconcileReq."""
        await self.ensure_connected()

        Req = self._proto_modules["ProtoOAReconcileReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id

        response = await self._send_request(request)

        sid_to_key = {v: k for k, v in self._symbol_ids.items()}

        result = []
        for order in response.order:
            order_symbol_id = order.tradeData.symbolId
            inst_key = sid_to_key.get(order_symbol_id)
            if inst_key is None:
                continue
            if instrument_key and inst_key != instrument_key:
                continue

            is_buy = order.tradeData.tradeSide == 1
            entry_price = None
            order_type = "LIMIT"
            if hasattr(order, "limitPrice") and order.limitPrice:
                entry_price = order.limitPrice  # double — already actual price
                order_type = "LMT"
            elif hasattr(order, "stopPrice") and order.stopPrice:
                entry_price = order.stopPrice  # double — already actual price
                order_type = "STP"

            result.append({
                "orderId": order.orderId,
                "orderType": order_type,
                "action": "BUY" if is_buy else "SELL",
                "totalQuantity": self._volume_to_size(order.tradeData.volume, inst_key),
                "entryPrice": entry_price,
                "status": "Submitted",
                "instrument": inst_key,
                "children": [],
            })
        return result

    async def modify_sl_tp(
        self,
        instrument_key: str,
        direction: str,
        new_sl: float | None = None,
        new_tp: float | None = None,
        new_sl_quantity: float | None = None,
    ) -> dict:
        """Modify SL/TP on an open position."""
        await self.ensure_connected()

        positions = await self.get_open_positions(instrument_key=instrument_key)
        matching = [p for p in positions if p["direction"] == direction]
        if not matching:
            raise RuntimeError(f"No open {direction} {instrument_key} position")

        pos = matching[0]
        old_sl = pos.get("stopLoss")
        old_tp = pos.get("takeProfit")

        Req = self._proto_modules["ProtoOAAmendPositionSLTPReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id
        request.positionId = pos["positionId"]

        if new_sl is not None:
            request.stopLoss = new_sl
        elif old_sl:
            request.stopLoss = old_sl

        if new_tp is not None:
            request.takeProfit = new_tp
        elif old_tp:
            request.takeProfit = old_tp

        msg_id = self._next_msg_id()
        await self._send_request(request, client_msg_id=msg_id)

        return {
            "old_sl": old_sl,
            "old_tp": old_tp,
            "new_sl": new_sl,
            "new_tp": new_tp,
        }

    async def cancel_order(self, order_id: int) -> dict:
        """Cancel a pending order by ID."""
        await self.ensure_connected()

        Req = self._proto_modules["ProtoOACancelOrderReq"]
        request = Req()
        request.ctidTraderAccountId = self._account_id
        request.orderId = order_id

        msg_id = self._next_msg_id()
        await self._send_request(request, client_msg_id=msg_id)

        return {"success": True, "orderId": order_id}

    async def modify_order(
        self, order_id: int, new_price: float, new_quantity: float | None = None,
    ) -> dict:
        """Modify order — not directly supported by cTrader, cancel + re-place."""
        logger.warning("modify_order not natively supported on cTrader — skipping")
        return {"success": False, "error": "Not supported on cTrader"}
