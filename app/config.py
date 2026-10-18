"""Startup configuration read from ``ENVMANAGER_`` environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.networks import IPNetwork, parse_networks

LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
MIN_CSRF_SECRET = 32
TUNING_ENVIRONMENTS = {"development", "testing"}
MAX_FAILURE_LIMIT = 100_000


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
    trusted_proxies: Annotated[list[IPNetwork], NoDecode] = []
    allowed_sources: Annotated[list[IPNetwork], NoDecode] = []
    data_path: Path = Path("/data")
    log_level: str = "INFO"
    csrf_secret: SecretStr | None = None
    cors_origins: Annotated[list[str], NoDecode] = []
    sentry_dsn: SecretStr | None = None
    environment: str = "production"
    demo_root: Path | None = None
    docker_socket: Path = Path("/var/run/docker.sock")
    login_ip_max_failures: int | None = None

    @property
    def database_path(self) -> Path:
        """Location of the SQLite database file inside the data volume."""
        return self.data_path / "envmanager.db"

    @property
    def backup_path(self) -> Path:
        """Folder inside the data volume that holds database backups."""
        return self.data_path / "backups"

    @property
    def lock_path(self) -> Path:
        """Folder inside the data volume that holds the deploy lock files."""
        return self.data_path / "locks"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_list(cls, value: object) -> object:
        """Accept comma separated lists from the environment."""
        return _split_csv(value)

    @field_validator("trusted_proxies", "allowed_sources", mode="before")
    @classmethod
    def _parse_networks(cls, value: object, info: ValidationInfo) -> object:
        """Parse comma separated addresses and networks, refusing every bad entry.

        Trusting every peer would let anyone forge the address that lockouts and the allowed
        sources rely on, so the trusted proxies refuse ``*`` and any network that holds every
        address. The allowed sources accept ``0.0.0.0/0`` or ``::/0`` (all of one IP version).
        """
        if not isinstance(value, str | list | tuple):
            return value
        setting = f"{cls.model_config['env_prefix']}{info.field_name}".upper()
        return parse_networks(value, setting, allow_everything=info.field_name != "trusted_proxies")

    @field_validator("login_ip_max_failures")
    @classmethod
    def _check_failure_limit(cls, value: int | None) -> int | None:
        """Require a positive, bounded failure limit."""
        if value is not None and not 1 <= value <= MAX_FAILURE_LIMIT:
            raise ValueError(f"must be between 1 and {MAX_FAILURE_LIMIT}")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _check_cors_origins(cls, value: list[str]) -> list[str]:
        """Refuse wildcard origins and anything that is not an http(s) origin."""
        for origin in value:
            if origin == "*" or not origin.startswith(("http://", "https://")):
                raise ValueError("CORS origins must be explicit http or https origins")
        return value

    @field_validator("demo_root")
    @classmethod
    def _check_demo_root(cls, value: Path | None) -> Path | None:
        """Treat an empty value as unset and require an absolute path otherwise."""
        if value is None or str(value) in {"", "."}:
            return None
        if not value.is_absolute():
            raise ValueError("the demo root must be an absolute path")
        return value

    @model_validator(mode="after")
    def _restrict_failure_limit(self) -> Self:
        """Allow a non-default per-address sign-in failure limit in development and testing only.

        It exists so browser specs, which share one client address, do not lock themselves out.
        Production keeps the default, so a value left over from a development setup cannot weaken
        the lockout there.
        """
        if self.login_ip_max_failures is not None and self.environment not in TUNING_ENVIRONMENTS:
            raise ValueError(
                "ENVMANAGER_LOGIN_IP_MAX_FAILURES is only accepted when ENVMANAGER_ENVIRONMENT "
                f"is one of {sorted(TUNING_ENVIRONMENTS)}"
            )
        return self

    @model_validator(mode="after")
    def _require_csrf_secret(self) -> Self:
        """Require a long CSRF secret everywhere except the testing environment."""
        if self.environment == "testing":
            return self
        if self.csrf_secret is None or len(self.csrf_secret.get_secret_value()) < MIN_CSRF_SECRET:
            raise ValueError(
                f"ENVMANAGER_CSRF_SECRET must be set to at least {MIN_CSRF_SECRET} characters"
            )
        return self

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
