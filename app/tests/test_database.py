"""Tests for the engine, migrations, backups and the startup key check."""

import sqlite3
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.crypto import CryptoService, KeyMismatchError, generate_master_key, parse_master_key
from app.core.db import create_engine, create_session_factory
from app.core.migrations import migrate_database
from app.models import AppSetting, Base
from app.models.app_setting import KEY_CHECK_SETTING
from app.services.backup import create_backup
from app.services.key_check import ensure_key_check


async def test_connections_use_wal_and_foreign_keys(engine: AsyncEngine) -> None:
    """Every connection runs in WAL mode with foreign keys enforced."""
    async with engine.connect() as connection:
        journal = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
        foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one()
    assert journal == "wal"
    assert foreign_keys == 1


async def test_migrations_match_models(engine: AsyncEngine) -> None:
    """The migrated schema has no difference from the model metadata."""

    def diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    async with engine.connect() as connection:
        assert await connection.run_sync(diff) == []


async def test_first_migration_takes_no_backup(engine: AsyncEngine, backup_dir: Path) -> None:
    """A brand new database has nothing to back up."""
    assert not backup_dir.exists()


async def test_migrating_at_head_is_a_no_op(
    engine: AsyncEngine, database_path: Path, backup_dir: Path
) -> None:
    """Running the migration again changes nothing and takes no backup."""
    await migrate_database(engine, database_path, backup_dir)
    assert not backup_dir.exists()


async def test_pending_migration_on_existing_database_is_backed_up(
    database_path: Path, backup_dir: Path
) -> None:
    """A database at an older revision is snapshotted before it is migrated."""
    with sqlite3.connect(database_path) as raw:
        raw.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        raw.execute("INSERT INTO alembic_version VALUES ('older')")
        raw.execute("CREATE TABLE marker (value TEXT)")
        raw.execute("INSERT INTO marker VALUES ('kept')")
    engine = create_engine(database_path)
    try:
        with pytest.raises(Exception, match="older"):
            await migrate_database(engine, database_path, backup_dir)
    finally:
        await engine.dispose()
    backups = list(backup_dir.glob("*-pre-migration.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as snapshot:
        assert snapshot.execute("SELECT value FROM marker").fetchone() == ("kept",)


async def test_create_backup_is_a_consistent_private_copy(
    engine: AsyncEngine, database_path: Path, backup_dir: Path
) -> None:
    """The snapshot opens as a database with the same rows and is owner-only."""
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        session.add(AppSetting(key="sample", value="stored"))
        await session.commit()
    backup = await create_backup(database_path, backup_dir, "manual")
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as snapshot:
        row = snapshot.execute("SELECT value FROM app_settings WHERE key = 'sample'").fetchone()
    assert row == ("stored",)


async def test_key_check_created_then_verified(engine: AsyncEngine, crypto: CryptoService) -> None:
    """The record is created on first start and accepted on the next start."""
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        assert await ensure_key_check(session, crypto) is True
    async with session_factory() as session:
        assert await ensure_key_check(session, crypto) is False
        record = await session.get(AppSetting, KEY_CHECK_SETTING)
    assert record is not None
    assert "env-manager-key-check" not in record.value


async def test_key_check_rejects_another_key(engine: AsyncEngine, crypto: CryptoService) -> None:
    """A different master key is refused once the record exists."""
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await ensure_key_check(session, crypto)
    other = CryptoService(parse_master_key(generate_master_key()))
    async with session_factory() as session:
        with pytest.raises(KeyMismatchError):
            await ensure_key_check(session, other)


async def test_session_commit_persists_rows(engine: AsyncEngine) -> None:
    """Rows written through a session are read back by a new one."""
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        session.add(AppSetting(key="a", value="1"))
        await session.commit()
    async with session_factory() as other:
        assert isinstance(other, AsyncSession)
        stored = await other.get(AppSetting, "a")
    assert stored is not None
    assert stored.value == "1"
    assert stored.updated_at is not None
