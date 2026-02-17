from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # IBKR Connection (IB Gateway)
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002  # 4001=live, 4002=paper
    ibkr_client_id: int = 1

    # Trading parameters
    max_risk_percent: float = 1.0
    max_position_size: float = 10.0  # max ounces per trade
    min_position_size: float = 1.0  # IBKR minimum: 1 troy ounce
    default_sl_distance: float = 50.0  # USD per ounce
    default_tp_distance: float = 100.0  # USD per ounce

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # App
    api_secret_key: str
    database_url: str = "sqlite+aiosqlite:///./trades.db"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
