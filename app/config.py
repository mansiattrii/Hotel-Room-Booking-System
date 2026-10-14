from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
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

    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(default=60, gt=0)

    booking_strategy: Literal["naive", "pessimistic", "constraint"] = "constraint"
    hold_minutes: int = Field(default=10, gt=0)
    max_stay_nights: int = Field(default=30, gt=0)
    booking_horizon_days: int = Field(default=365, gt=0)
    worker_interval_seconds: float = Field(default=30, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
