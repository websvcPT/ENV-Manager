"""Tests that the application lifespan enforces the master key."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.core.crypto import KeyConfigurationError, KeyMismatchError, generate_master_key
from app.main import create_app


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point the settings at a temporary data folder with a fresh key."""
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    monkeypatch.delenv("ENVMANAGER_KEY_FILE", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_startup_creates_database_and_key_check(configured: Path) -> None:
    """A first start migrates the database and exposes the shared state."""
    application = create_app()
    async with application.router.lifespan_context(application):
        async with application.state.engine.connect() as connection:
            count = (await connection.execute(text("SELECT count(*) FROM app_settings"))).scalar()
        assert count == 1
        assert application.state.crypto is not None
    assert (configured / "envmanager.db").exists()


async def test_second_start_with_same_key_succeeds(configured: Path) -> None:
    """Restarting with the same key passes the stored check."""
    for _ in range(2):
        application = create_app()
        async with application.router.lifespan_context(application):
            pass


async def test_startup_refused_without_key(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing master key stops startup."""
    monkeypatch.delenv("ENVMANAGER_KEY")
    get_settings.cache_clear()
    application = create_app()
    with pytest.raises(KeyConfigurationError):
        async with application.router.lifespan_context(application):
            pass


async def test_startup_refused_with_wrong_key(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key that differs from the one the database was created with stops startup."""
    application = create_app()
    async with application.router.lifespan_context(application):
        pass
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    get_settings.cache_clear()
    application = create_app()
    with pytest.raises(KeyMismatchError):
        async with application.router.lifespan_context(application):
            pass


async def test_the_docker_cli_is_built_from_the_configured_socket(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The application-wide docker CLI talks to ``ENVMANAGER_DOCKER_SOCKET``."""
    monkeypatch.setenv("ENVMANAGER_DOCKER_SOCKET", str(configured / "elsewhere.sock"))
    get_settings.cache_clear()
    application = create_app()
    async with application.router.lifespan_context(application):
        cli = application.state.docker_cli
        assert cli.socket_path == configured / "elsewhere.sock"
        assert cli.environment()["DOCKER_HOST"] == f"unix://{configured / 'elsewhere.sock'}"
        assert (await cli.status()).mounted is False
