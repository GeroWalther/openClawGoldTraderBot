from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # IBKR Connection (IB Gateway)
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4001  # 4001=live, 4002=paper
    ibkr_client_id: int = 1

    # Trading parameters (per-instrument settings live in app/instruments.py)
    max_risk_percent: float = 1.0

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # App
    api_secret_key: str
    database_url: str = "sqlite+aiosqlite:///./trades.db"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
