"""Tests for pointing a development database at the smtp4dev mail catcher."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli.main import app
from app.config import get_settings
from app.core.crypto import generate_master_key
from app.core.db import create_engine, create_session_factory
from app.seeds import DEV_SMTP, ensure_dev_smtp
from app.services.app_settings import SmtpSettings, load_stored_settings, save_section
from app.services.audit import AuditActor
from app.tests.notify_helpers import SMTP

runner = CliRunner()


@pytest.fixture
def data_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty data folder the command line tool is pointed at."""
    folder = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(folder))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    get_settings.cache_clear()
    yield folder
    get_settings.cache_clear()


def stored_smtp(folder: Path) -> SmtpSettings:
    """The SMTP settings stored in the CLI's database."""

    async def read() -> SmtpSettings:
        engine = create_engine(folder / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                return (await load_stored_settings(db)).smtp
        finally:
            await engine.dispose()

    return asyncio.run(read())


def store_smtp(folder: Path, smtp: SmtpSettings) -> None:
    """Store SMTP settings in the CLI's database."""

    async def write() -> None:
        engine = create_engine(folder / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                await save_section(db, "smtp", smtp, actor=AuditActor.cli())
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(write())


def test_the_development_catcher_is_the_service_of_the_compose_file() -> None:
    """smtp4dev on port 25 of the compose network, no TLS, no sign in, a from address."""
    assert (DEV_SMTP.host, DEV_SMTP.port, DEV_SMTP.tls_mode, DEV_SMTP.username) == (
        "smtp4dev",
        25,
        "none",
        "",
    )
    assert DEV_SMTP.configured and "@" in DEV_SMTP.from_address


def test_seeding_a_development_database_points_the_smtp_settings_at_the_catcher(
    data_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new database gets it, and so does one that was seeded before this existed."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "development")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0
    assert stored_smtp(data_path) == DEV_SMTP
    store_smtp(data_path, SmtpSettings())
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 1  # already seeded, still completed
    assert stored_smtp(data_path) == DEV_SMTP


def test_seeding_never_replaces_smtp_settings_that_are_configured(
    data_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer's own server stays."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "development")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0
    store_smtp(data_path, SMTP)
    runner.invoke(app, ["dev", "seed"])
    assert stored_smtp(data_path) == SMTP


def test_seeding_the_testing_environment_leaves_smtp_unset(
    data_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests and browser runs outside development start with no mail configured."""
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "testing")
    get_settings.cache_clear()
    assert runner.invoke(app, ["dev", "seed"]).exit_code == 0
    assert not stored_smtp(data_path).configured


async def test_ensure_dev_smtp_reports_whether_it_stored_anything(engine) -> None:  # type: ignore[no-untyped-def]
    """The helper stores once and then finds SMTP configured."""
    async with create_session_factory(engine)() as db:
        assert await ensure_dev_smtp(db) is True
        await db.commit()
        assert await ensure_dev_smtp(db) is False
