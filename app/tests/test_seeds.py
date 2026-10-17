"""Tests for the seed dataset and the dev seed command."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli.main import app
from app.config import get_settings
from app.core.crypto import CryptoService, generate_master_key
from app.core.db import create_engine, create_session_factory
from app.core.passwords import verify_password
from app.models import AccessGrant, Environment, EnvironmentVersion, User
from app.seeds import DEV_PASSWORD, SeedData, SeedError, seed

runner = CliRunner()


async def test_seed_creates_the_dataset(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService
) -> None:
    async with create_session_factory(seeded_database)() as db:
        users = list((await db.execute(select(User))).scalars())
        assert len(users) == 8
        assert all(verify_password(DEV_PASSWORD, user.password_hash)[0] for user in users)
        assert (await db.execute(select(func.count()).select_from(AccessGrant))).scalar_one() == 8
        assert (await db.execute(select(func.count()).select_from(Environment))).scalar_one() == 6
        versions = (
            (
                await db.execute(
                    select(EnvironmentVersion).where(
                        EnvironmentVersion.environment_id == seed_data.envs.db.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [version.number for version in versions] == [1, 2]
        assert crypto.decrypt_text(versions[1].content_encrypted) == "DB_HOST=db\nDB_PORT=3306\n"
        assert versions[1].content_hash == crypto.content_hash("DB_HOST=db\nDB_PORT=3306\n")


async def test_seeding_twice_is_refused(
    seeded_database: AsyncEngine, seed_data: SeedData, crypto: CryptoService
) -> None:
    async with create_session_factory(seeded_database)() as db:
        with pytest.raises(SeedError, match="already seeded"):
            await seed(db, crypto)


@pytest.fixture
def cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at an empty data folder with a generated master key."""
    data_path = tmp_path / "data"
    monkeypatch.setenv("ENVMANAGER_DATA_PATH", str(data_path))
    monkeypatch.setenv("ENVMANAGER_KEY", generate_master_key())
    get_settings.cache_clear()
    yield data_path
    get_settings.cache_clear()


def _user_count(data_path: Path) -> int:
    """Number of users in the CLI's database."""

    async def count() -> int:
        engine = create_engine(data_path / "envmanager.db")
        try:
            async with create_session_factory(engine)() as db:
                return (await db.execute(select(func.count()).select_from(User))).scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(count())


@pytest.mark.parametrize("environment", ["testing", "development"])
def test_dev_seed_command_seeds_in_allowed_environments(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", environment)
    get_settings.cache_clear()
    result = runner.invoke(app, ["dev", "seed"])
    assert result.exit_code == 0, result.output
    assert DEV_PASSWORD in result.output
    assert _user_count(cli_environment) == 8
    again = runner.invoke(app, ["dev", "seed"])
    assert again.exit_code == 1
    assert "already seeded" in again.output


def test_dev_seed_command_refuses_production(
    cli_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVMANAGER_ENVIRONMENT", "production")
    monkeypatch.setenv("ENVMANAGER_CSRF_SECRET", "x" * 40)
    get_settings.cache_clear()
    result = runner.invoke(app, ["dev", "seed"])
    assert result.exit_code == 1
    assert "only allowed" in result.output
    assert not (cli_environment / "envmanager.db").exists()
