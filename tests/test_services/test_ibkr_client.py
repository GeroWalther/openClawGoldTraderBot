"""
IBKR client tests are limited to unit-level checks since the actual
ib_async library requires a running IB Gateway. Integration tests
should be run manually with paper trading.
"""
import pytest

from app.services.ibkr_client import IBKRClient


def test_ibkr_client_init(settings):
    client = IBKRClient(settings)
    assert client._connected is False
    assert client.settings == settings
    assert client._contracts == {}


def test_get_contract_raises_before_connect(settings):
    client = IBKRClient(settings)
    with pytest.raises(RuntimeError, match="not qualified"):
        client.get_contract("XAUUSD")


def test_gold_contract_raises_before_connect(settings):
    client = IBKRClient(settings)
    with pytest.raises(RuntimeError, match="not qualified"):
        _ = client.gold_contract
