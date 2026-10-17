"""Startup configuration read from ``ENVMANAGER_`` environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


def _split_csv(value: object) -> object:
    """Turn a comma separated string into a list, leaving other values untouched."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    """Process-wide settings. Secrets are held as ``SecretStr`` and never logged."""

    model_config = SettingsConfigDict(env_prefix="ENVMANAGER_", extra="ignore")

    key: SecretStr | None = None
    key_file: Path | None = None
    base_url: str = "http://localhost:8080"
    port: int = 8080
    trusted_proxies: Annotated[list[str], NoDecode] = []
    data_path: Path = Path("/data")
    log_level: str = "INFO"
    csrf_secret: SecretStr | None = None
    cors_origins: Annotated[list[str], NoDecode] = []
    sentry_dsn: SecretStr | None = None
    environment: str = "production"

    @field_validator("trusted_proxies", "cors_origins", mode="before")
    @classmethod
    def _parse_list(cls, value: object) -> object:
        """Accept comma separated lists from the environment."""
        return _split_csv(value)

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        """Normalize and validate the log level name."""
        level = value.upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"log level must be one of {sorted(LOG_LEVELS)}")
        return level


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
