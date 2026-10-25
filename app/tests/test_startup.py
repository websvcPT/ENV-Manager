"""Tests that the application lifespan enforces the master key."""

import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.config import get_settings
from app.core.crypto import KeyConfigurationError, KeyMismatchError, generate_master_key
from app.main import create_app
from app.services.docker_cli import DockerCli
from app.services.scratch_sweep import LOCK_FILE_NAME, SCRATCH_PREFIX


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


@pytest.mark.parametrize(
    ("environment", "base_url", "warns"),
    [
        ("production", "http://envmanager.example:8080", True),
        ("staging", "http://envmanager.example", True),
        ("production", "https://envmanager.example", False),
        ("development", "http://localhost:8080", False),
        ("testing", "http://localhost:8080", False),
    ],
)
async def test_an_http_base_url_outside_development_warns_once_at_startup(
    configured: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    base_url: str,
    warns: bool,
) -> None:
    """The session cookie is only Secure under an https base URL, so startup says so."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", environment)
    monkeypatch.setenv("ENVMANAGER_BASE_URL", base_url)
    monkeypatch.setenv("ENVMANAGER_CSRF_SECRET", "x" * 40)
    get_settings.cache_clear()
    application = create_app()
    # create_app replaces the root handlers, caplog's included, so listen on the logger itself.
    records: list[logging.LogRecord] = []

    class Listener(logging.Handler):
        """Keeps the records it is given."""

        def emit(self, record: logging.LogRecord) -> None:
            """Remember the record."""
            records.append(record)

    listener = Listener(logging.WARNING)
    logger = logging.getLogger("envmanager.main")
    logger.addHandler(listener)
    try:
        async with application.router.lifespan_context(application):
            pass
    finally:
        logger.removeHandler(listener)
    messages = [record.getMessage() for record in records]
    if warns:
        assert len(messages) == 1
        assert "ENVMANAGER_BASE_URL" in messages[0] and "Secure" in messages[0]
        assert base_url not in messages[0]
    else:
        assert messages == []


async def test_the_seeded_application_starts_on_a_database_already_at_head(
    web_app: FastAPI, tmp_path: Path
) -> None:
    """The lifespan finds the seeded copy migrated, so it takes no pre-migration backup."""
    assert not (tmp_path / "backups").exists()
    async with web_app.state.engine.connect() as connection:
        count = (await connection.execute(text("SELECT count(*) FROM users"))).scalar()
    assert count == 10


async def test_startup_removes_the_docker_run_folders_a_killed_process_left_and_keeps_live_ones(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep of the temporary directory runs at startup and logs only a count."""
    temp_root = configured / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    stale = temp_root / f"{SCRATCH_PREFIX}stale"
    (stale / "run-a").mkdir(parents=True)
    (stale / "run-b").mkdir()
    (stale / "run-a" / "config.json").write_text('{"auths": {}}', encoding="utf-8")
    (stale / LOCK_FILE_NAME).touch()
    live = DockerCli()
    live_run = live.scratch / "run-live"
    live_run.mkdir()
    records: list[str] = []

    class Listener(logging.Handler):
        """Keeps the messages it is given."""

        def emit(self, record: logging.LogRecord) -> None:
            """Remember the message."""
            records.append(record.getMessage())

    application = create_app()
    logger = logging.getLogger("envmanager.main")
    listener = Listener(logging.INFO)
    logger.addHandler(listener)
    try:
        async with application.router.lifespan_context(application):
            pass
    finally:
        logger.removeHandler(listener)
    assert not stale.exists() and live_run.is_dir()
    swept = [message for message in records if "Docker run folder" in message]
    assert swept == ["2 leftover Docker run folder(s) of a stopped process removed"]
    assert str(temp_root) not in swept[0]
