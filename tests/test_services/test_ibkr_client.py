"""
IBKR client tests are limited to unit-level checks since the actual
ib_async library requires a running IB Gateway. Integration tests
should be run manually with paper trading.
"""
import pytest

from app.services.ibkr_client import IBKRClient, GOLD_CONTRACT


def test_ibkr_client_init(settings):
    client = IBKRClient(settings)
    assert client._connected is False
    assert client.settings == settings


def test_gold_contract_definition():
    assert GOLD_CONTRACT.symbol == "XAUUSD"
    assert GOLD_CONTRACT.secType == "CMDTY"
    assert GOLD_CONTRACT.exchange == "SMART"
    assert GOLD_CONTRACT.currency == "USD"


def test_ibkr_client_gold_contract_raises_before_connect(settings):
    client = IBKRClient(settings)
    with pytest.raises(RuntimeError, match="not qualified"):
        _ = client.gold_contract
