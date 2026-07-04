"""Единственный источник правды для всех настроек. Читает .env и env vas."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str

    # LLM
    anthropic_api_key: str
    groq_api_key: str
    claude_model: str = "claude-haiku-4-5-20251001"
    whisper_model: str = "whisper-large-v3"

    # DB
    database_url: str

    # Behaviour
    context_messages_limit: int = 20
    max_voice_seconds: int = 300
    log_level: str = "INFO"


settings = Settings()
