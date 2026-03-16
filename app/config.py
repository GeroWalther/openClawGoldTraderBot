from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # IBKR Connection (IB Gateway)
    ibkr_enabled: bool = False  # Set True to connect to IBKR
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4001  # 4001=live, 4002=paper
    ibkr_client_id: int = 1

    # Trading parameters (per-instrument settings live in app/instruments.py)
    max_risk_percent: float = 3.0

    # Session filter
    session_filter_enabled: bool = True

    # ATR-based dynamic stops
    atr_enabled: bool = True
    atr_period: int = 14
    atr_sl_multiplier: float = 2.0
    atr_tp_multiplier: float = 2.0
    atr_cache_ttl_seconds: int = 3600

    # Partial take-profit
    partial_tp_enabled: bool = True
    partial_tp_percent: float = 50.0
    partial_tp_r_multiple: float = 1.0

    # Cooldown after consecutive losses
    cooldown_enabled: bool = True
    cooldown_after_losses: int = 2
    cooldown_hours_base: int = 2

    # M5 scalp cooldown (separate from main cooldown — shorter, minute-based)
    scalp_cooldown_enabled: bool = True
    scalp_cooldown_after_losses: int = 2       # trigger after N consecutive scalp losses
    scalp_cooldown_minutes_base: int = 10      # 2 M5 bars, matches backtest

    # Daily loss limits
    daily_loss_limit_enabled: bool = True
    max_daily_loss_percent: float = 6.0
    max_daily_trades: int = 9999

    # Weekly loss limit (% of account)
    weekly_loss_limit_enabled: bool = True
    max_weekly_loss_percent: float = 15.0

    # Spread protection — reject if spread > this % of stop distance
    max_spread_to_sl_ratio: float = 0.40

    # Conviction-based position sizing (matches backtest: HIGH=100%, MED=75%, LOW=50% of base)
    conviction_sizing_enabled: bool = True
    conviction_high_risk_pct: float = 3.0
    conviction_medium_risk_pct: float = 2.25
    conviction_low_risk_pct: float = 1.5

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_webhook_secret: str = ""  # secret_token for webhook verification

    # IC Markets (cTrader Open API)
    icm_client_id: str = ""
    icm_client_secret: str = ""
    icm_access_token: str = ""
    icm_refresh_token: str = ""
    icm_account_id: int = 0
    icm_host: str = "live.ctraderapi.com"
    icm_port: int = 5035

    # Max position size cap (0 = no cap, uses risk-based sizing only)
    max_position_size: float = 0.0

    # M5 scalp signal threshold (default 6, lower = more trades but lower quality)
    m5_signal_threshold: float = 8.0
    m5_high_conviction_threshold: float = 11.0

    # M5 Scalp BTC (separate flag + separate thresholds from FX)
    scalp_btc_enabled: bool = False
    m5_btc_signal_threshold: float = 6.0
    m5_btc_high_conviction_threshold: float = 9.0
    m5_btc_atr_sl_multiplier: float = 1.5

    # M15 Sensei (BTC W/M pattern strategy)
    sensei_btc_enabled: bool = False

    # M15 BB Bounce (range-specialist, trades when M5 scalp is quiet)
    bb_bounce_enabled: bool = False
    bb_bounce_signal_threshold: float = 8.0
    bb_bounce_high_conviction_threshold: float = 11.0

    # App
    api_secret_key: str
    database_url: str = "sqlite+aiosqlite:///./trades.db"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
