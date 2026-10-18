"""Tests for the engine, migrations, backups and the startup key check."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.crypto import CryptoService, KeyMismatchError, generate_master_key, parse_master_key
from app.core.db import create_engine, create_session_factory
from app.core.migrations import build_config, migrate_database
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


@pytest.mark.filterwarnings("ignore:Skipped unsupported reflection of expression-based index")
@pytest.mark.filterwarnings("ignore:autogenerate skipping metadata-specified expression-based")
async def test_migrations_match_models(engine: AsyncEngine) -> None:
    """The migrated schema has no difference from the model metadata.

    SQLite cannot reflect the expression index on the lowercased email, so that one index is
    skipped by the comparison; its own test reads it from the schema.
    """

    def diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    async with engine.connect() as connection:
        assert await connection.run_sync(diff) == []


async def test_first_migration_takes_no_backup(database_path: Path, backup_dir: Path) -> None:
    """A brand new database has nothing to back up."""
    fresh = create_engine(database_path)
    try:
        await migrate_database(fresh, database_path, backup_dir)
    finally:
        await fresh.dispose()
    assert database_path.exists()
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


TREE_STATEMENTS = (
    'INSERT INTO users (id, name, email, password_hash, "group", is_active, totp_enabled,'
    " created_at, updated_at) VALUES (1, 'u', 'u@example.test', 'x', 'admin', 1, 0,"
    " '2026-01-01', '2026-01-01')",
    "INSERT INTO projects (id, name, created_at, updated_at)"
    " VALUES (1, 'p', '2026-01-01', '2026-01-01')",
    "INSERT INTO deployments (id, project_id, name, root_path, dist_missing_policy,"
    " drift_policy, missing_target_policy, created_at, updated_at)"
    " VALUES (1, 1, 'd', '/x', 'block', 'halt', 'create', '2026-01-01', '2026-01-01')",
    "INSERT INTO services (id, deployment_id, name, created_at, updated_at)"
    " VALUES (1, 1, 's', '2026-01-01', '2026-01-01')",
    "INSERT INTO environments (id, service_id, name, target_path, created_at, updated_at)"
    " VALUES (1, 1, 'e', 'e', '2026-01-01', '2026-01-01')",
    "INSERT INTO environment_versions (id, environment_id, number, content_encrypted,"
    " content_hash, created_at) VALUES (1, 1, 1, 'c', 'h', '2026-01-01')",
    "INSERT INTO access_grants (id, user_id, level, project_id, created_at)"
    " VALUES (1, 1, 'project', 1, '2026-01-01')",
)
CHILD_TABLES = (
    "projects",
    "deployments",
    "services",
    "environments",
    "environment_versions",
    "access_grants",
)


async def test_autoincrement_migration_keeps_children_and_stops_id_reuse(
    database_path: Path, backup_dir: Path
) -> None:
    """Rebuilding the structure tables over several revisions keeps every row and child.

    The upgrade starts two revisions earlier than the rebuild so the migration connection has
    already written to the database before it, and foreign keys must still be off for the
    rebuild and back on afterwards.
    """
    engine = create_engine(database_path)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(build_config(sync), "0004"))
            for statement in TREE_STATEMENTS:
                await connection.execute(text(statement))
        await migrate_database(engine, database_path, backup_dir)
        async with engine.connect() as connection:
            counts = [
                (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
                for table in CHILD_TABLES
            ]
            sequence_rows = await connection.execute(text("SELECT name, seq FROM sqlite_sequence"))
            sequences: dict[str, int] = dict(sequence_rows.all())
            violations = (await connection.execute(text("PRAGMA foreign_key_check"))).all()
            enforcement = (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one()
            deployment_sql = (
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'deployments'")
                )
            ).scalar_one()
            file_defaults = (
                await connection.execute(
                    text("SELECT file_mode, file_owner, file_group FROM deployments")
                )
            ).one()
        assert counts == [1] * len(CHILD_TABLES)
        assert sequences["projects"] == 1
        assert violations == []
        assert enforcement == 1
        assert "AUTOINCREMENT" in deployment_sql
        assert tuple(file_defaults) == ("0600", None, None)
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM projects WHERE id = 1"))
            await connection.execute(
                text(
                    "INSERT INTO projects (name, created_at, updated_at)"
                    " VALUES ('q', '2026-01-01', '2026-01-01')"
                )
            )
            new_id = (await connection.execute(text("SELECT id FROM projects"))).scalar_one()
            leftover = await connection.execute(text("SELECT count(*) FROM services"))
            remaining = leftover.scalar_one()
        assert new_id == 2
        assert remaining == 0
    finally:
        await engine.dispose()


USER_INSERT = (
    'INSERT INTO users (name, email, password_hash, "group", is_active, totp_enabled,'
    " created_at, updated_at) VALUES (:name, :email, 'x', 'admin', 1, 0, '2026-01-01',"
    " '2026-01-01')"
)


async def test_the_database_refuses_an_email_that_differs_only_in_case(
    engine: AsyncEngine,
) -> None:
    """The unique index is on the lowercased address, so a case variant is a duplicate."""
    async with engine.begin() as connection:
        await connection.execute(
            text(USER_INSERT), {"name": "first", "email": "Person@Example.test"}
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(USER_INSERT), {"name": "second", "email": "person@EXAMPLE.test"}
            )
    async with engine.connect() as connection:
        index_sql = (
            await connection.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'uq_users_email_lower'")
            )
        ).scalar_one()
    assert "UNIQUE" in index_sql and "lower(email)" in index_sql


async def test_the_upgrade_refuses_users_that_share_an_email_and_changes_nothing(
    database_path: Path, backup_dir: Path
) -> None:
    """Existing duplicates stop the upgrade with the user names, never the addresses."""
    engine = create_engine(database_path)
    try:
        await migrate_database(engine, database_path, backup_dir)
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: command.downgrade(build_config(sync), "0009"))
            for name, email in (("ann", "Shared@example.test"), ("bob", "shared@example.test")):
                await connection.execute(text(USER_INSERT), {"name": name, "email": email})
        with pytest.raises(RuntimeError) as refused:
            await migrate_database(engine, database_path, backup_dir)
        message = str(refused.value)
        assert "ann" in message and "bob" in message
        assert "example.test" not in message
        async with engine.connect() as connection:
            revision = await connection.execute(text("SELECT version_num FROM alembic_version"))
            users = (await connection.execute(text("SELECT count(*) FROM users"))).scalar_one()
        assert revision.scalar_one() == "0009" and users == 2
        assert any("pre-migration" in backup.name for backup in backup_dir.iterdir())
    finally:
        await engine.dispose()
