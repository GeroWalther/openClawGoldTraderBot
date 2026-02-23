import asyncio
import logging

from ib_async import IB, Contract, MarketOrder, LimitOrder, Order, Trade as IBTrade

from app.config import Settings
from app.instruments import INSTRUMENTS, InstrumentSpec, build_ibkr_contract, get_instrument

logger = logging.getLogger(__name__)


class IBKRClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ib = IB()
        self._connected = False
        self._contracts: dict[str, Contract] = {}

    async def connect(self):
        """Connect to IB Gateway and qualify all instrument contracts."""
        await self._ib.connectAsync(
            host=self.settings.ibkr_host,
            port=self.settings.ibkr_port,
            clientId=self.settings.ibkr_client_id,
            timeout=20,
        )
        self._connected = True

        # Request delayed data as fallback when live data isn't subscribed
        self._ib.reqMarketDataType(4)  # 4 = delayed-frozen

        # Qualify all instrument contracts
        for key, spec in INSTRUMENTS.items():
            raw = build_ibkr_contract(spec)
            try:
                qualified = await self._ib.qualifyContractsAsync(raw)
                if qualified and qualified[0] and qualified[0].conId:
                    self._contracts[key] = qualified[0]
                    logger.info("%s contract qualified: %s", key, qualified[0])
                else:
                    logger.warning("Failed to qualify %s contract — skipping", key)
            except Exception as e:
                logger.warning("Error qualifying %s contract: %s — skipping", key, e)

        if not self._contracts:
            raise RuntimeError("Failed to qualify any instrument contracts")

        logger.info(
            "Connected to IBKR Gateway at %s:%s (%d instruments)",
            self.settings.ibkr_host,
            self.settings.ibkr_port,
            len(self._contracts),
        )

    async def disconnect(self):
        """Disconnect from IB Gateway."""
        if self._connected:
            self._ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IBKR Gateway")

    async def ensure_connected(self):
        """Reconnect if connection was lost."""
        if not self._ib.isConnected():
            logger.warning("IBKR connection lost, reconnecting...")
            await self.connect()

    def get_contract(self, instrument_key: str) -> Contract:
        """Get a qualified contract by instrument key."""
        if instrument_key not in self._contracts:
            raise RuntimeError(
                f"Contract for {instrument_key} not qualified — "
                f"available: {list(self._contracts)}"
            )
        return self._contracts[instrument_key]

    @property
    def gold_contract(self) -> Contract:
        """Backward-compat alias."""
        return self.get_contract("XAUUSD")

    @staticmethod
    def _valid_price(val) -> float | None:
        """Return a valid price or None. Filters out nan, inf, -1, 0."""
        import math
        if val is None:
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f) or f <= 0:
            return None
        return f

    async def get_price(self, instrument_key: str = "XAUUSD") -> dict:
        """Get current bid/ask price for an instrument."""
        await self.ensure_connected()
        contract = self.get_contract(instrument_key)
        # Use streaming (not snapshot) — more reliable for forex and futures
        ticker = self._ib.reqMktData(contract, genericTickList="", snapshot=False)
        # Wait for data to arrive
        for _ in range(100):  # 10 seconds max
            await asyncio.sleep(0.1)
            if self._valid_price(ticker.bid) or self._valid_price(ticker.last):
                break

        bid = self._valid_price(ticker.bid) or self._valid_price(ticker.last)
        ask = self._valid_price(ticker.ask) or self._valid_price(ticker.last)
        last = self._valid_price(ticker.last)

        # For forex, midpoint is often more useful if last is missing
        if last is None and bid and ask:
            last = (bid + ask) / 2

        self._ib.cancelMktData(contract)

        return {
            "bid": bid or 0.0,
            "ask": ask or 0.0,
            "last": last or 0.0,
        }

    async def get_gold_price(self) -> dict:
        """Backward-compat alias for get_price('XAUUSD')."""
        return await self.get_price("XAUUSD")

    async def open_position(
        self,
        direction: str,
        size: float,
        stop_distance: float | None = None,
        take_profit_price: float | None = None,
        instrument_key: str = "XAUUSD",
        stop_price: float | None = None,
    ) -> dict:
        """
        Open a position with bracket orders (market entry + STP + LMT).

        Args:
            direction: "BUY" or "SELL"
            size: Position size in instrument units
            stop_distance: Stop distance (kept for backward compat / logging)
            take_profit_price: Absolute price for take-profit
            instrument_key: Instrument to trade
            stop_price: Absolute stop-loss price
        """
        await self.ensure_connected()
        contract = self.get_contract(instrument_key)

        if stop_price and take_profit_price:
            return await self._place_bracket_order(
                contract, direction, size, stop_price, take_profit_price,
            )
        else:
            order = MarketOrder(direction, size)
            trade = self._ib.placeOrder(contract, order)
            await self._wait_for_fill(trade)
            return self._trade_to_dict(trade)

    async def _place_bracket_order(
        self,
        contract: Contract,
        direction: str,
        size: float,
        stop_price: float,
        take_profit_price: float,
    ) -> dict:
        """Place a bracket order: market entry + fixed stop-loss + limit take-profit."""
        parent_id = self._ib.client.getReqId()
        reverse = "SELL" if direction == "BUY" else "BUY"

        # Parent: market entry
        parent = Order(
            orderId=parent_id,
            action=direction,
            orderType="MKT",
            totalQuantity=size,
            transmit=False,
        )

        # Take-profit: limit order
        tp = Order(
            orderId=parent_id + 1,
            action=reverse,
            orderType="LMT",
            totalQuantity=size,
            lmtPrice=take_profit_price,
            parentId=parent_id,
            transmit=False,
        )

        # Stop-loss: fixed stop at absolute price
        sl = Order(
            orderId=parent_id + 2,
            action=reverse,
            orderType="STP",
            totalQuantity=size,
            auxPrice=stop_price,
            parentId=parent_id,
            transmit=True,
        )

        # Place all three
        parent_trade = self._ib.placeOrder(contract, parent)
        self._ib.placeOrder(contract, tp)
        self._ib.placeOrder(contract, sl)

        await self._wait_for_fill(parent_trade)
        return self._trade_to_dict(parent_trade)

    async def get_open_orders(
        self, instrument_key: str | None = None
    ) -> list[dict]:
        """Get all open/active orders, optionally filtered by instrument."""
        await self.ensure_connected()
        trades = self._ib.openTrades()
        result = []
        for t in trades:
            resolved_key = self._resolve_instrument_key(t.contract)
            if resolved_key is None:
                continue
            if instrument_key and resolved_key != instrument_key:
                continue
            order = t.order
            result.append({
                "orderId": order.orderId,
                "parentId": order.parentId,
                "orderType": order.orderType,
                "action": order.action,
                "totalQuantity": float(order.totalQuantity),
                "lmtPrice": getattr(order, "lmtPrice", None),
                "auxPrice": getattr(order, "auxPrice", None),
                "status": t.orderStatus.status,
                "instrument": resolved_key,
            })
        return result

    async def modify_order(self, order_id: int, new_price: float) -> dict:
        """Modify an existing order's price (STP → auxPrice, LMT → lmtPrice)."""
        await self.ensure_connected()
        for t in self._ib.openTrades():
            if t.order.orderId == order_id:
                order = t.order
                if order.orderType == "STP":
                    order.auxPrice = new_price
                elif order.orderType == "LMT":
                    order.lmtPrice = new_price
                else:
                    return {"success": False, "error": f"Unsupported order type: {order.orderType}"}
                self._ib.placeOrder(t.contract, order)
                logger.info("Modified order %d to price %.5f", order_id, new_price)
                return {"success": True, "orderId": order_id, "newPrice": new_price}
        return {"success": False, "error": f"Order {order_id} not found"}

    async def modify_sl_tp(
        self,
        instrument_key: str,
        direction: str,
        new_sl: float | None = None,
        new_tp: float | None = None,
    ) -> dict:
        """
        Modify SL and/or TP for an open position identified by instrument + direction.

        Finds child orders (parentId > 0) with the reverse action, then:
        - STP order = stop-loss → update auxPrice
        - LMT order = take-profit → update lmtPrice
        """
        await self.ensure_connected()
        reverse = "SELL" if direction == "BUY" else "BUY"
        contract = self.get_contract(instrument_key)

        old_sl = None
        old_tp = None
        sl_order_id = None
        tp_order_id = None

        for t in self._ib.openTrades():
            resolved_key = self._resolve_instrument_key(t.contract)
            if resolved_key != instrument_key:
                continue
            order = t.order
            # Child orders have parentId > 0 and reverse action
            if order.parentId > 0 and order.action == reverse:
                if order.orderType == "STP":
                    old_sl = order.auxPrice
                    sl_order_id = order.orderId
                elif order.orderType == "LMT":
                    old_tp = order.lmtPrice
                    tp_order_id = order.orderId

        results = {"old_sl": old_sl, "old_tp": old_tp, "new_sl": None, "new_tp": None}

        if new_sl is not None:
            if sl_order_id is None:
                raise RuntimeError(f"No STP (stop-loss) order found for {instrument_key} {direction}")
            res = await self.modify_order(sl_order_id, new_sl)
            if not res["success"]:
                raise RuntimeError(f"Failed to modify SL: {res['error']}")
            results["new_sl"] = new_sl

        if new_tp is not None:
            if tp_order_id is None:
                raise RuntimeError(f"No LMT (take-profit) order found for {instrument_key} {direction}")
            res = await self.modify_order(tp_order_id, new_tp)
            if not res["success"]:
                raise RuntimeError(f"Failed to modify TP: {res['error']}")
            results["new_tp"] = new_tp

        return results

    async def open_position_with_partial_tp(
        self,
        direction: str,
        size: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
        tp1_size: float,
        tp2_size: float,
        instrument_key: str = "XAUUSD",
    ) -> dict:
        """
        Open a position with partial take-profit: TP1 (at 1R) + TP2 (full TP).

        Falls back to regular bracket if sizes are too small.
        Known limitation: SL quantity doesn't auto-reduce after TP1 fill.
        """
        await self.ensure_connected()
        instrument = get_instrument(instrument_key)

        # Check if split sizes meet minimum — fall back to regular bracket
        if tp1_size < instrument.min_size or tp2_size < instrument.min_size:
            logger.info(
                "Partial TP sizes too small (%.1f/%.1f, min=%.1f) — falling back to regular bracket",
                tp1_size, tp2_size, instrument.min_size,
            )
            return await self.open_position(
                direction=direction, size=size,
                stop_price=stop_price, take_profit_price=tp2_price,
                instrument_key=instrument_key,
            )

        contract = self.get_contract(instrument_key)
        parent_id = self._ib.client.getReqId()
        reverse = "SELL" if direction == "BUY" else "BUY"

        # Parent: market entry
        parent = Order(
            orderId=parent_id,
            action=direction,
            orderType="MKT",
            totalQuantity=size,
            transmit=False,
        )

        # TP1: partial take-profit at 1R
        tp1 = Order(
            orderId=parent_id + 1,
            action=reverse,
            orderType="LMT",
            totalQuantity=tp1_size,
            lmtPrice=tp1_price,
            parentId=parent_id,
            transmit=False,
        )

        # TP2: remaining take-profit at full TP
        tp2 = Order(
            orderId=parent_id + 2,
            action=reverse,
            orderType="LMT",
            totalQuantity=tp2_size,
            lmtPrice=tp2_price,
            parentId=parent_id,
            transmit=False,
        )

        # SL: full size stop-loss
        # TODO: SL quantity doesn't auto-reduce after TP1 fill
        sl = Order(
            orderId=parent_id + 3,
            action=reverse,
            orderType="STP",
            totalQuantity=size,
            auxPrice=stop_price,
            parentId=parent_id,
            transmit=True,
        )

        parent_trade = self._ib.placeOrder(contract, parent)
        self._ib.placeOrder(contract, tp1)
        self._ib.placeOrder(contract, tp2)
        self._ib.placeOrder(contract, sl)

        await self._wait_for_fill(parent_trade)
        return self._trade_to_dict(parent_trade)

    async def close_position(
        self, direction: str, size: float, instrument_key: str = "XAUUSD"
    ) -> dict:
        """Close a position by placing an opposite market order."""
        await self.ensure_connected()
        contract = self.get_contract(instrument_key)
        reverse = "SELL" if direction == "BUY" else "BUY"
        order = MarketOrder(reverse, size)
        trade = self._ib.placeOrder(contract, order)
        await self._wait_for_fill(trade)
        return self._trade_to_dict(trade)

    async def get_open_positions(
        self, instrument_key: str | None = None
    ) -> list[dict]:
        """Get open positions, optionally filtered by instrument."""
        await self.ensure_connected()
        positions = self._ib.positions()
        result = []
        for pos in positions:
            resolved_key = self._resolve_instrument_key(pos.contract)
            if resolved_key is None:
                continue
            if instrument_key and resolved_key != instrument_key:
                continue
            spec = get_instrument(resolved_key)
            result.append({
                "instrument": resolved_key,
                "symbol": pos.contract.symbol,
                "size": float(pos.position),
                "direction": "BUY" if pos.position > 0 else "SELL",
                "avg_cost": float(pos.avgCost),
                "unrealized_pnl": None,
                "size_unit": spec.size_unit,
            })
        return result

    def _resolve_instrument_key(self, contract: Contract) -> str | None:
        """Map an IBKR contract back to our instrument key."""
        for key, qualified in self._contracts.items():
            if (
                contract.symbol == qualified.symbol
                and contract.secType == qualified.secType
                and contract.currency == qualified.currency
            ):
                return key
        # Fallback: match by symbol in INSTRUMENTS
        for key, spec in INSTRUMENTS.items():
            if contract.symbol == spec.symbol and contract.secType == spec.sec_type:
                return key
        return None

    async def get_account_info(self) -> dict:
        """Get account balance and margin info."""
        await self.ensure_connected()
        accounts = self._ib.managedAccounts()
        logger.info("Managed accounts: %s", accounts)

        # Use cached account values (ib_async auto-subscribes on connect).
        # Only request fresh data if cache is empty.
        values = self._ib.accountValues()
        if not values and accounts:
            try:
                await asyncio.wait_for(
                    self._ib.accountSummaryAsync(), timeout=10
                )
            except asyncio.TimeoutError:
                logger.warning("accountSummaryAsync timed out, using cached values")
            values = self._ib.accountValues()

        logger.info("Account values count: %d", len(values))
        info = {}
        target_tags = {"NetLiquidation", "TotalCashValue", "AvailableFunds",
                       "BuyingPower", "MaintMarginReq", "GrossPositionValue"}
        for item in values:
            if item.tag in target_tags and item.tag not in info:
                info[item.tag] = float(item.value)
        info["accounts"] = accounts
        return info

    async def _wait_for_fill(self, trade: IBTrade, timeout: float = 30.0):
        """Wait for a trade to be filled or timeout."""
        start = asyncio.get_event_loop().time()
        while trade.orderStatus.status not in ("Filled", "Cancelled", "Inactive"):
            await asyncio.sleep(0.1)
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout:
                logger.warning("Order fill timeout after %.1fs", timeout)
                break

    @staticmethod
    def _trade_to_dict(trade: IBTrade) -> dict:
        """Convert an IB Trade object to a simple dict."""
        fill_price = None
        if trade.fills:
            fill_price = float(trade.fills[0].execution.price)
        return {
            "orderId": trade.order.orderId,
            "status": trade.orderStatus.status,
            "direction": trade.order.action,
            "size": float(trade.order.totalQuantity),
            "fillPrice": fill_price,
            "dealId": str(trade.order.orderId),
        }
