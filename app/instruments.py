from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class TradingSession:
    name: str
    start_hour_utc: int  # 0-23
    end_hour_utc: int    # 0-23 (exclusive), wraps past midnight if end < start


@dataclass(frozen=True)
class InstrumentSpec:
    key: str
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    multiplier: float
    min_size: float
    max_size: float
    default_sl_distance: float
    default_tp_distance: float
    min_stop_distance: float
    max_stop_distance: float
    yahoo_symbol: str
    display_name: str
    size_unit: str
    is_future: bool = False
    future_cycle: str | None = None  # e.g. "HMUZ" for quarterly
    trading_sessions: tuple[TradingSession, ...] = ()
    warn_low_liquidity: bool = False
    swing_strategy: str = "krabbe_scored"  # "krabbe_scored" or "rsi_reversal"


INSTRUMENTS: dict[str, InstrumentSpec] = {
    "XAUUSD": InstrumentSpec(
        key="XAUUSD",
        symbol="XAUUSD",
        sec_type="CMDTY",
        exchange="SMART",
        currency="USD",
        multiplier=1,
        min_size=1,
        max_size=10,
        default_sl_distance=50.0,
        default_tp_distance=100.0,
        min_stop_distance=5.0,
        max_stop_distance=300.0,
        yahoo_symbol="GC=F",
        display_name="Gold (XAUUSD)",
        size_unit="oz",
        trading_sessions=(
            TradingSession("London", 7, 16),
            TradingSession("New York", 13, 21),
        ),
    ),
    "MES": InstrumentSpec(
        key="MES",
        symbol="MES",
        sec_type="FUT",
        exchange="CME",
        currency="USD",
        multiplier=5,
        min_size=1,
        max_size=20,
        default_sl_distance=20.0,
        default_tp_distance=40.0,
        min_stop_distance=2.0,
        max_stop_distance=100.0,
        yahoo_symbol="ES=F",
        display_name="Micro E-mini S&P 500",
        size_unit="contracts",
        is_future=True,
        future_cycle="HMUZ",
        trading_sessions=(
            TradingSession("US Market", 13, 20),
        ),
    ),
    "IBUS500": InstrumentSpec(
        key="IBUS500",
        symbol="IBUS500",
        sec_type="CFD",
        exchange="SMART",
        currency="USD",
        multiplier=1,
        min_size=1,
        max_size=50,
        default_sl_distance=20.0,
        default_tp_distance=40.0,
        min_stop_distance=2.0,
        max_stop_distance=100.0,
        yahoo_symbol="^GSPC",
        display_name="S&P 500 CFD",
        size_unit="units",
        trading_sessions=(
            TradingSession("US Market", 13, 20),
        ),
    ),
    "EURUSD": InstrumentSpec(
        key="EURUSD",
        symbol="EUR",
        sec_type="CASH",
        exchange="IDEALPRO",
        currency="USD",
        multiplier=1,
        min_size=20000,
        max_size=500000,
        default_sl_distance=0.0050,
        default_tp_distance=0.0100,
        min_stop_distance=0.0005,
        max_stop_distance=0.0500,
        yahoo_symbol="EURUSD=X",
        display_name="EUR/USD",
        size_unit="units",
        trading_sessions=(
            TradingSession("London+NY", 7, 21),
        ),
    ),
    "EURJPY": InstrumentSpec(
        key="EURJPY",
        symbol="EUR",
        sec_type="CASH",
        exchange="IDEALPRO",
        currency="JPY",
        multiplier=1,
        min_size=20000,
        max_size=500000,
        default_sl_distance=0.50,
        default_tp_distance=1.00,
        min_stop_distance=0.05,
        max_stop_distance=5.00,
        yahoo_symbol="EURJPY=X",
        display_name="EUR/JPY",
        size_unit="units",
        trading_sessions=(
            TradingSession("Tokyo", 0, 9),
            TradingSession("London", 7, 16),
        ),
    ),
    "CADJPY": InstrumentSpec(
        key="CADJPY",
        symbol="CAD",
        sec_type="CASH",
        exchange="IDEALPRO",
        currency="JPY",
        multiplier=1,
        min_size=20000,
        max_size=500000,
        default_sl_distance=0.50,
        default_tp_distance=1.00,
        min_stop_distance=0.05,
        max_stop_distance=5.00,
        yahoo_symbol="CADJPY=X",
        display_name="CAD/JPY",
        size_unit="units",
        trading_sessions=(
            TradingSession("Tokyo", 0, 9),
            TradingSession("London+NY", 7, 21),
        ),
    ),
    "USDJPY": InstrumentSpec(
        key="USDJPY",
        symbol="USD",
        sec_type="CASH",
        exchange="IDEALPRO",
        currency="JPY",
        multiplier=1,
        min_size=20000,
        max_size=500000,
        default_sl_distance=0.50,
        default_tp_distance=1.00,
        min_stop_distance=0.05,
        max_stop_distance=5.00,
        yahoo_symbol="JPY=X",
        display_name="USD/JPY",
        size_unit="units",
        trading_sessions=(
            TradingSession("Tokyo", 0, 9),
            TradingSession("London+NY", 7, 21),
        ),
    ),
    "BTC": InstrumentSpec(
        key="BTC",
        symbol="MBT",
        sec_type="FUT",
        exchange="CME",
        currency="USD",
        multiplier=0.1,
        min_size=1,
        max_size=50,
        default_sl_distance=2000.0,
        default_tp_distance=4000.0,
        min_stop_distance=200.0,
        max_stop_distance=15000.0,
        yahoo_symbol="BTC-USD",
        display_name="Micro Bitcoin (MBT)",
        size_unit="contracts",
        is_future=True,
        future_cycle="FGHJKMNQUVXZ",  # monthly
        trading_sessions=(),  # 24/7 crypto
        warn_low_liquidity=True,  # weekends
        swing_strategy="rsi_reversal",  # Krabbe macro factors don't suit crypto
    ),
}


def get_instrument(key: str | None) -> InstrumentSpec:
    """Look up an instrument by key. Defaults to XAUUSD if key is None."""
    if key is None:
        return INSTRUMENTS["XAUUSD"]
    normalized = key.upper().strip()
    if normalized not in INSTRUMENTS:
        raise ValueError(f"Unknown instrument: {key!r}. Available: {list(INSTRUMENTS)}")
    return INSTRUMENTS[normalized]


def build_ibkr_contract(spec: InstrumentSpec):
    """Build an ib_async Contract from an InstrumentSpec."""
    from ib_async import Contract

    kwargs = {
        "symbol": spec.symbol,
        "secType": spec.sec_type,
        "exchange": spec.exchange,
        "currency": spec.currency,
    }
    if spec.is_future:
        kwargs["lastTradeDateOrContractMonth"] = get_next_futures_expiry(spec)
    return Contract(**kwargs)


def get_next_futures_expiry(spec: InstrumentSpec) -> str:
    """Return YYYYMM for the next futures contract month in the cycle."""
    if not spec.future_cycle:
        raise ValueError(f"{spec.key} has no future_cycle defined")

    cycle_months = {
        "H": 3, "M": 6, "U": 9, "Z": 12,
        "F": 1, "G": 2, "J": 4, "K": 5,
        "N": 7, "Q": 8, "V": 10, "X": 11,
    }
    months_in_cycle = sorted(cycle_months[c] for c in spec.future_cycle)
    today = date.today()
    # Find next expiry month (with buffer: if within 5 days of month end, roll)
    rollover_date = today + timedelta(days=5)

    for m in months_in_cycle:
        if m > rollover_date.month or (m == rollover_date.month and rollover_date.day <= 15):
            return f"{today.year}{m:02d}"

    # Wrap to first month of next year
    return f"{today.year + 1}{months_in_cycle[0]:02d}"
