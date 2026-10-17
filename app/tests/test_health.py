"""Health endpoint and settings tests."""

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app


async def test_healthz_returns_ok() -> None:
    """The liveness probe answers 200 with a status body."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_defaults_and_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults apply and comma separated lists are parsed."""
    monkeypatch.setenv("ENVMANAGER_CORS_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("ENVMANAGER_LOG_LEVEL", "debug")
    settings = Settings()
    assert settings.port == 8080
    assert str(settings.data_path) == "/data"
    assert settings.cors_origins == ["https://a.example", "https://b.example"]
    assert settings.log_level == "DEBUG"


def test_settings_reject_bad_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown log level fails startup configuration."""
    monkeypatch.setenv("ENVMANAGER_LOG_LEVEL", "loud")
    with pytest.raises(ValidationError):
        Settings()
