import os
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    TELEGRAM_API_ID: int
    TELEGRAM_API_HASH: SecretStr
    BOT_TOKEN: SecretStr
    ADMIN_ID: int
    PUBLISH_CHAT_ID: str

    DEEPSEEK_API_KEY: SecretStr
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_CHAT_MODEL: str = "deepseek-chat"
    DEEPSEEK_REASONER_MODEL: str = "deepseek-reasoner"

    DB_PATH: str = "data/ai_news_digest.db"
    SESSION_DIR: str = "data/session"
    TIMEZONE: str = "UTC"

    MORNING_WEB_FETCH: str = "08:15"
    MORNING_DIGEST_TIME: str = "09:00"
    EVENING_WEB_FETCH: str = "18:15"
    EVENING_DIGEST_TIME: str = "19:00"

    DEFAULT_NEWS_COUNT: int = 7
    DEFAULT_USEFUL_COUNT: int = 3
    DEDUP_WINDOW_DAYS: int = 14
    MAX_PER_SOURCE: int = 1
    RELAXED_MAX_PER_SOURCE: int = 2

    LOG_LEVEL: str = "INFO"
    UNREAD_EXCLUDED_USERNAMES: str = ""
    UNREAD_EXCLUDED_TITLES: str = ""
    SESSION_NAME: str = Field(default="news_userbot")

    model_config = SettingsConfigDict(
        env_file=os.path.join(PROJECT_ROOT, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

