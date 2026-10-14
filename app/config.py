from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables (and .env in development)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str
    test_database_url: str | None = None

    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_connect_timeout_seconds: int = 5


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
