import asyncio
import logging

from ib_async import IB, Contract, MarketOrder, LimitOrder, Order, Trade as IBTrade

from app.config import Settings

logger = logging.getLogger(__name__)

# XAUUSD contract definition
GOLD_CONTRACT = Contract(
    symbol="XAUUSD",
    secType="CMDTY",
    exchange="SMART",
    currency="USD",
)


class IBKRClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ib = IB()
        self._connected = False
        self._gold_contract: Contract | None = None

    async def connect(self):
        """Connect to IB Gateway."""
        await self._ib.connectAsync(
            host=self.settings.ibkr_host,
            port=self.settings.ibkr_port,
            clientId=self.settings.ibkr_client_id,
            timeout=20,
        )
        self._connected = True

        # Qualify the gold contract once
        contracts = await self._ib.qualifyContractsAsync(GOLD_CONTRACT)
        if contracts:
            self._gold_contract = contracts[0]
            logger.info("XAUUSD contract qualified: %s", self._gold_contract)
        else:
            raise RuntimeError("Failed to qualify XAUUSD contract")

        logger.info(
            "Connected to IBKR Gateway at %s:%s",
            self.settings.ibkr_host,
            self.settings.ibkr_port,
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

    @property
    def gold_contract(self) -> Contract:
        if self._gold_contract is None:
            raise RuntimeError("XAUUSD contract not qualified — call connect() first")
        return self._gold_contract

    async def get_gold_price(self) -> dict:
        """Get current XAUUSD bid/ask price."""
        await self.ensure_connected()
        ticker = self._ib.reqMktData(self.gold_contract, genericTickList="", snapshot=True)
        # Wait for snapshot data
        for _ in range(50):  # 5 seconds max
            await asyncio.sleep(0.1)
            if ticker.last is not None or ticker.bid is not None:
                break

        bid = ticker.bid if ticker.bid is not None else ticker.last
        ask = ticker.ask if ticker.ask is not None else ticker.last

        self._ib.cancelMktData(self.gold_contract)

        return {
            "bid": float(bid) if bid is not None else 0.0,
            "ask": float(ask) if ask is not None else 0.0,
            "last": float(ticker.last) if ticker.last is not None else 0.0,
        }

    async def open_position(
        self,
        direction: str,
        size: float,
        stop_distance: float | None = None,
        take_profit_price: float | None = None,
    ) -> dict:
        """
        Open a XAUUSD position with optional bracket orders (trailing SL + TP).

        Args:
            direction: "BUY" or "SELL"
            size: Number of troy ounces
            stop_distance: Trailing stop distance in USD/oz
            take_profit_price: Absolute price for take-profit
        """
        await self.ensure_connected()

        if stop_distance and take_profit_price:
            # Bracket order: entry + trailing SL + TP
            return await self._place_bracket_order(
                direction, size, stop_distance, take_profit_price
            )
        else:
            # Simple market order
            order = MarketOrder(direction, size)
            trade = self._ib.placeOrder(self.gold_contract, order)
            await self._wait_for_fill(trade)
            return self._trade_to_dict(trade)

    async def _place_bracket_order(
        self,
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

        # Trailing stop-loss: trails by stop_distance dollars
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
        parent_trade = self._ib.placeOrder(self.gold_contract, parent)
        self._ib.placeOrder(self.gold_contract, tp)
        self._ib.placeOrder(self.gold_contract, sl)

        await self._wait_for_fill(parent_trade)
        return self._trade_to_dict(parent_trade)

    async def close_position(self, direction: str, size: float) -> dict:
        """Close a position by placing an opposite market order."""
        await self.ensure_connected()
        reverse = "SELL" if direction == "BUY" else "BUY"
        order = MarketOrder(reverse, size)
        trade = self._ib.placeOrder(self.gold_contract, order)
        await self._wait_for_fill(trade)
        return self._trade_to_dict(trade)

    async def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        await self.ensure_connected()
        positions = self._ib.positions()
        result = []
        for pos in positions:
            if pos.contract.symbol == "XAUUSD":
                result.append({
                    "symbol": pos.contract.symbol,
                    "size": float(pos.position),
                    "direction": "BUY" if pos.position > 0 else "SELL",
                    "avg_cost": float(pos.avgCost),
                    "unrealized_pnl": None,  # requires market data
                })
        return result

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
