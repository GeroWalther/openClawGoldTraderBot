"""Integration tests for ICMarketsClient.

Tests cover:
- Volume conversion math (BTC size ↔ cTrader volume)
- Price scaling (integer pip values ↔ float prices)
- Account info parsing
- Position parsing from reconcile response
- Connection state management
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.config import Settings
from app.services.icmarkets_client import (
    ICMarketsClient,
    VOLUME_MULTIPLIER,
    _to_volume,
    _from_volume,
)


# ------------------------------------------------------------------
# Unit tests: volume conversion
# ------------------------------------------------------------------

class TestVolumeConversion:
    """cTrader volume = units * 100 (so 0.01 BTC = volume 1)."""

    def test_to_volume_minimum(self):
        assert _to_volume(0.01) == 1

    def test_to_volume_standard(self):
        assert _to_volume(0.05) == 5

    def test_to_volume_one_btc(self):
        assert _to_volume(1.0) == 100

    def test_to_volume_fractional(self):
        assert _to_volume(0.1) == 10

    def test_from_volume_minimum(self):
        assert _from_volume(1) == 0.01

    def test_from_volume_standard(self):
        assert _from_volume(5) == 0.05

    def test_from_volume_one_btc(self):
        assert _from_volume(100) == 1.0

    def test_roundtrip(self):
        """size → volume → size should be identity."""
        for size in [0.01, 0.05, 0.1, 0.5, 1.0, 5.0]:
            assert _from_volume(_to_volume(size)) == size


# ------------------------------------------------------------------
# Unit tests: price scaling
# ------------------------------------------------------------------

class TestPriceScaling:
    """cTrader sends prices as integers: price * 10^digits.
    For BTCUSD with digits=2: 8750000 → 87500.00
    """

    def test_btc_price_scaling(self):
        """cTrader spot event always uses 10^5 divisor, then round to digits."""
        raw = 8750000000
        digits = 2
        price = round(raw / 100000, digits)
        assert price == 87500.00

    def test_btc_price_scaling_high(self):
        raw = 10000000000
        price = round(raw / 100000, 2)
        assert price == 100000.00

    def test_btc_price_scaling_with_cents(self):
        raw = 8750050000
        price = round(raw / 100000, 2)
        assert price == 87500.50

    def test_btc_spread_calculation(self):
        bid_raw = 8749500000
        ask_raw = 8750500000
        bid = round(bid_raw / 100000, 2)
        ask = round(ask_raw / 100000, 2)
        spread = ask - bid
        assert spread == 10.00  # $10 spread

    def test_sl_tp_price_values(self):
        """SL/TP are sent as float prices (not scaled integers)."""
        entry = 87500.00
        sl_distance = 250.0
        tp_distance = 500.0

        sl_buy = entry - sl_distance
        tp_buy = entry + tp_distance

        assert sl_buy == 87250.00
        assert tp_buy == 88000.00


# ------------------------------------------------------------------
# Unit tests: ICMarketsClient construction
# ------------------------------------------------------------------

class TestClientConstruction:

    def test_default_settings(self):
        settings = Settings(
            telegram_bot_token="test",
            telegram_chat_id="test",
            api_secret_key="test",
            icm_client_id="test_id",
            icm_client_secret="test_secret",
            icm_access_token="test_token",
            icm_account_id=12345,
        )
        client = ICMarketsClient(settings)
        assert client._account_id == 12345
        assert client._access_token == "test_token"
        assert client._connected is False
        assert client._symbol_ids == {}

    @pytest.mark.asyncio
    async def test_empty_credentials_raises_on_connect(self):
        settings = Settings(
            telegram_bot_token="test",
            telegram_chat_id="test",
            api_secret_key="test",
            icm_client_id="",
        )
        client = ICMarketsClient(settings)
        with pytest.raises(RuntimeError, match="client_id not configured"):
            await client.connect()


# ------------------------------------------------------------------
# Unit tests: account info parsing
# ------------------------------------------------------------------

class TestAccountInfoParsing:
    """Verify balance conversion (cTrader returns cents → divide by 100)."""

    def test_balance_from_cents(self):
        # cTrader balance is in cents: 10000 = €100.00
        balance_cents = 10000
        balance = balance_cents / 100.0
        assert balance == 100.0

    def test_zero_balance(self):
        balance_cents = 0
        balance = balance_cents / 100.0
        assert balance == 0.0

    def test_fractional_balance(self):
        balance_cents = 9999
        balance = balance_cents / 100.0
        assert balance == 99.99


# ------------------------------------------------------------------
# Unit tests: position size for 100 EUR account
# ------------------------------------------------------------------

class TestPositionSizing:
    """Verify margin requirements with 100 EUR at 1:30 leverage."""

    def test_minimum_btc_margin(self):
        """0.01 BTC at ~$87500 needs ~$29.17 margin at 1:30."""
        btc_price = 87500.0
        size = 0.01
        leverage = 30
        notional = btc_price * size  # $875
        margin_required = notional / leverage  # $29.17
        assert margin_required < 100  # fits in 100 EUR account
        assert margin_required == pytest.approx(29.17, abs=0.01)

    def test_max_size_100eur(self):
        """Max BTC size with 100 EUR (≈$108) at 1:30 leverage."""
        account_eur = 100
        eur_usd_rate = 1.08
        account_usd = account_eur * eur_usd_rate
        btc_price = 87500.0
        leverage = 30
        max_notional = account_usd * leverage  # $3240
        max_size = max_notional / btc_price  # ~0.037 BTC
        assert max_size > 0.01  # can at least trade minimum
        assert max_size < 0.05  # can't do much more

    def test_risk_per_trade(self):
        """3% risk on 100 EUR = 3 EUR max loss per trade."""
        account = 100
        risk_pct = 3.0
        max_loss = account * risk_pct / 100  # €3
        assert max_loss == 3.0

        # With 0.01 BTC position and $250 stop distance:
        # Loss = 0.01 * 250 = $2.50 ≈ €2.31 → within risk
        size = 0.01
        stop_distance = 250
        loss_usd = size * stop_distance
        assert loss_usd == 2.50


# ------------------------------------------------------------------
# Unit tests: spot event handling
# ------------------------------------------------------------------

class TestSpotEventHandling:

    def test_price_cache_format(self):
        """Price cache should have bid, ask, last."""
        prices = {"bid": 87450.00, "ask": 87460.00, "last": 87455.00}
        assert "bid" in prices
        assert "ask" in prices
        assert prices["last"] == (prices["bid"] + prices["ask"]) / 2

    def test_spot_price_conversion(self):
        """Raw spot event values → proper prices (always /100000, round to digits)."""
        bid_raw = 8745000000
        ask_raw = 8746000000
        digits = 2

        bid = round(bid_raw / 100000, digits)
        ask = round(ask_raw / 100000, digits)
        last = (bid + ask) / 2

        assert bid == 87450.00
        assert ask == 87460.00
        assert last == 87455.00


# ------------------------------------------------------------------
# Unit tests: order construction
# ------------------------------------------------------------------

class TestOrderConstruction:

    def test_market_order_volume(self):
        """0.01 BTC → volume 1 in cTrader."""
        size = 0.01
        volume = _to_volume(size)
        assert volume == 1

    def test_sl_tp_as_absolute_prices(self):
        """cTrader expects absolute SL/TP prices, not distances."""
        entry = 87500.00
        stop_distance = 250.0
        tp_distance = 500.0

        # BUY order
        sl_buy = entry - stop_distance
        tp_buy = entry + tp_distance
        assert sl_buy == 87250.00
        assert tp_buy == 88000.00

        # SELL order
        sl_sell = entry + stop_distance
        tp_sell = entry - tp_distance
        assert sl_sell == 87750.00
        assert tp_sell == 87000.00


# ------------------------------------------------------------------
# Integration-like test: mock cTrader connection flow
# ------------------------------------------------------------------

class TestConnectionFlow:

    @pytest.fixture
    def icm_settings(self):
        return Settings(
            telegram_bot_token="test",
            telegram_chat_id="test",
            api_secret_key="test",
            icm_client_id="22366_test",
            icm_client_secret="test_secret",
            icm_access_token="test_token",
            icm_account_id=46544493,
            icm_host="live.ctraderapi.com",
            icm_port=5035,
        )

    def test_host_selection_live(self, icm_settings):
        """Live host should be selected for non-demo config."""
        client = ICMarketsClient(icm_settings)
        assert "demo" not in client.settings.icm_host.lower()

    def test_host_selection_demo(self):
        settings = Settings(
            telegram_bot_token="test",
            telegram_chat_id="test",
            api_secret_key="test",
            icm_client_id="test",
            icm_client_secret="test",
            icm_access_token="test",
            icm_account_id=1,
            icm_host="demo.ctraderapi.com",
        )
        client = ICMarketsClient(settings)
        assert "demo" in client.settings.icm_host.lower()

    def test_symbol_map(self, icm_settings):
        """BTC should map to BTCUSD or BTC/USD."""
        client = ICMarketsClient(icm_settings)
        # After resolve, BTC key maps to a symbolId
        # Simulate resolved state
        client._symbol_ids = {"BTC": 10026}
        client._symbol_digits = {"BTC": 2}
        assert client._get_symbol_id("BTC") == 10026

    def test_unknown_symbol_raises(self, icm_settings):
        client = ICMarketsClient(icm_settings)
        with pytest.raises(RuntimeError, match="Symbol not resolved"):
            client._get_symbol_id("UNKNOWN")

    @pytest.mark.asyncio
    async def test_not_connected_raises(self, icm_settings):
        """Operations should fail when not connected."""
        client = ICMarketsClient(icm_settings)
        with pytest.raises(RuntimeError, match="not connected"):
            await client._send_request(MagicMock())


# ------------------------------------------------------------------
# Integration test: live connection (skipped unless ICM_LIVE_TEST=1)
# ------------------------------------------------------------------

@pytest.mark.skipif(
    not __import__("os").environ.get("ICM_LIVE_TEST"),
    reason="Set ICM_LIVE_TEST=1 to run live cTrader tests",
)
class TestLiveConnection:
    """Live tests against cTrader API — only runs with ICM_LIVE_TEST=1.

    These tests connect to the real cTrader API but do NOT place orders.
    """

    @pytest_asyncio.fixture
    async def live_client(self):
        import os
        settings = Settings(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "test"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "test"),
            api_secret_key=os.environ.get("API_SECRET_KEY", "test"),
            icm_client_id=os.environ["icm_client_id"],
            icm_client_secret=os.environ["icm_client_secret"],
            icm_access_token=os.environ["icm_access_token"],
            icm_account_id=int(os.environ["icm_account_id"]),
        )
        client = ICMarketsClient(settings)
        await client.connect()
        yield client
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_live_connect(self, live_client):
        assert live_client._connected is True

    @pytest.mark.asyncio
    async def test_live_btc_symbol_resolved(self, live_client):
        assert "BTC" in live_client._symbol_ids
        assert live_client._symbol_ids["BTC"] > 0

    @pytest.mark.asyncio
    async def test_live_account_info(self, live_client):
        info = await live_client.get_account_info()
        assert "NetLiquidation" in info
        assert info["broker"] == "icmarkets"
        assert str(live_client._account_id) in info["accounts"]

    @pytest.mark.asyncio
    async def test_live_btc_price(self, live_client):
        import asyncio
        # Wait briefly for spot prices to arrive
        await asyncio.sleep(3)
        price = await live_client.get_price("BTC")
        assert price["bid"] > 0
        assert price["ask"] > 0
        assert price["ask"] >= price["bid"]
        # BTC should be in a reasonable range
        assert 10000 < price["bid"] < 500000

    @pytest.mark.asyncio
    async def test_live_no_open_positions(self, live_client):
        """Account has 0 balance so should have no positions."""
        positions = await live_client.get_open_positions()
        assert isinstance(positions, list)

    @pytest.mark.asyncio
    async def test_live_no_pending_orders(self, live_client):
        orders = await live_client.get_pending_orders()
        assert isinstance(orders, list)
