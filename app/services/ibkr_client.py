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

        # Qualify all instrument contracts
        for key, spec in INSTRUMENTS.items():
            raw = build_ibkr_contract(spec)
            qualified = await self._ib.qualifyContractsAsync(raw)
            if qualified:
                self._contracts[key] = qualified[0]
                logger.info("%s contract qualified: %s", key, qualified[0])
            else:
                logger.warning("Failed to qualify %s contract — skipping", key)

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

    async def get_price(self, instrument_key: str = "XAUUSD") -> dict:
        """Get current bid/ask price for an instrument."""
        await self.ensure_connected()
        contract = self.get_contract(instrument_key)
        ticker = self._ib.reqMktData(contract, genericTickList="", snapshot=True)
        # Wait for snapshot data
        for _ in range(50):  # 5 seconds max
            await asyncio.sleep(0.1)
            if ticker.last is not None or ticker.bid is not None:
                break

        bid = ticker.bid if ticker.bid is not None else ticker.last
        ask = ticker.ask if ticker.ask is not None else ticker.last

        self._ib.cancelMktData(contract)

        return {
            "bid": float(bid) if bid is not None else 0.0,
            "ask": float(ask) if ask is not None else 0.0,
            "last": float(ticker.last) if ticker.last is not None else 0.0,
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
    ) -> dict:
        """
        Open a position with optional bracket orders (trailing SL + TP).

        Args:
            direction: "BUY" or "SELL"
            size: Position size in instrument units
            stop_distance: Trailing stop distance
            take_profit_price: Absolute price for take-profit
            instrument_key: Instrument to trade
        """
        await self.ensure_connected()
        contract = self.get_contract(instrument_key)

        if stop_distance and take_profit_price:
            return await self._place_bracket_order(
                contract, direction, size, stop_distance, take_profit_price
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
        stop_distance: float,
        take_profit_price: float,
    ) -> dict:
        """Place a bracket order (entry + trailing stop-loss + take-profit)."""
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

        # Trailing stop-loss: trails by stop_distance
        sl = Order(
            orderId=parent_id + 2,
            action=reverse,
            orderType="TRAIL",
            totalQuantity=size,
            auxPrice=stop_distance,
            parentId=parent_id,
            transmit=True,  # transmit all at once
        )

        # Place all three
        parent_trade = self._ib.placeOrder(contract, parent)
        self._ib.placeOrder(contract, tp)
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
