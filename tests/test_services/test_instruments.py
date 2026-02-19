import pytest
from unittest.mock import patch

from app.instruments import (
    INSTRUMENTS,
    get_instrument,
    build_ibkr_contract,
    get_next_futures_expiry,
)


def test_all_instruments_registered():
    assert set(INSTRUMENTS.keys()) == {"XAUUSD", "MES", "IBUS500", "EURUSD", "EURJPY", "BTC"}


def test_get_instrument_default():
    spec = get_instrument(None)
    assert spec.key == "XAUUSD"


def test_get_instrument_by_key():
    spec = get_instrument("MES")
    assert spec.key == "MES"
    assert spec.sec_type == "FUT"
    assert spec.exchange == "CME"


def test_get_instrument_case_insensitive():
    spec = get_instrument("eurusd")
    assert spec.key == "EURUSD"


def test_get_instrument_with_whitespace():
    spec = get_instrument("  EURJPY  ")
    assert spec.key == "EURJPY"


def test_get_instrument_unknown_raises():
    with pytest.raises(ValueError, match="Unknown instrument"):
        get_instrument("BTCUSD")


def test_build_ibkr_contract_cmdty():
    spec = INSTRUMENTS["XAUUSD"]
    contract = build_ibkr_contract(spec)
    assert contract.symbol == "XAUUSD"
    assert contract.secType == "CMDTY"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"


def test_build_ibkr_contract_cash():
    spec = INSTRUMENTS["EURUSD"]
    contract = build_ibkr_contract(spec)
    assert contract.symbol == "EUR"
    assert contract.secType == "CASH"
    assert contract.exchange == "IDEALPRO"
    assert contract.currency == "USD"


def test_build_ibkr_contract_future_includes_expiry():
    spec = INSTRUMENTS["MES"]
    contract = build_ibkr_contract(spec)
    assert contract.symbol == "MES"
    assert contract.secType == "FUT"
    assert contract.exchange == "CME"
    # Should have a YYYYMM expiry
    assert contract.lastTradeDateOrContractMonth is not None
    assert len(contract.lastTradeDateOrContractMonth) == 6


def test_futures_expiry_format():
    spec = INSTRUMENTS["MES"]
    expiry = get_next_futures_expiry(spec)
    assert len(expiry) == 6
    year = int(expiry[:4])
    month = int(expiry[4:])
    assert 2024 <= year <= 2030
    assert month in (3, 6, 9, 12)  # HMUZ cycle


def test_futures_expiry_no_cycle_raises():
    spec = INSTRUMENTS["XAUUSD"]
    with pytest.raises(ValueError, match="no future_cycle"):
        get_next_futures_expiry(spec)


def test_build_ibkr_contract_crypto():
    spec = INSTRUMENTS["BTC"]
    contract = build_ibkr_contract(spec)
    assert contract.symbol == "BRR"
    assert contract.secType == "CRYPTO"
    assert contract.exchange == "PAXOS"
    assert contract.currency == "USD"


def test_instrument_spec_fields():
    for key, spec in INSTRUMENTS.items():
        assert spec.key == key
        assert spec.min_size > 0
        assert spec.max_size > spec.min_size
        assert spec.min_stop_distance > 0
        assert spec.max_stop_distance > spec.min_stop_distance
        assert spec.default_sl_distance > 0
        assert spec.default_tp_distance > 0
        assert spec.multiplier >= 1
        assert spec.display_name
        assert spec.yahoo_symbol
        assert spec.size_unit
