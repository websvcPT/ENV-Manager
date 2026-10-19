"""Tests for the engine, migrations, backups and the startup key check."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.crypto import CryptoService, KeyMismatchError, generate_master_key, parse_master_key
from app.core.db import create_engine, create_session_factory, sqlite_url
from app.core.migrations import build_config, migrate_database
from app.models import AppSetting, Base
from app.models.app_setting import KEY_CHECK_SETTING
from app.services.backup import create_backup
from app.services.key_check import ensure_key_check
from app.tests.template_databases import copy_database


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


async def test_the_second_factor_time_column_is_added_and_removed_in_place(
    database_path: Path, backup_dir: Path
) -> None:
    """Migration 0011 adds a nullable time; going back drops it and keeps the email index."""
    engine = create_engine(database_path)
    try:
        await migrate_database(engine, database_path, backup_dir)
        async with engine.begin() as connection:
            ann = {"name": "ann", "email": "ann@example.test"}
            await connection.execute(text(USER_INSERT), ann)
            columns = (await connection.execute(text("PRAGMA table_info(users)"))).all()
            assert [row[1] for row in columns if row[1] == "totp_enabled_at"] == ["totp_enabled_at"]
            assert next(row[3] for row in columns if row[1] == "totp_enabled_at") == 0
            stored = await connection.execute(text("SELECT totp_enabled_at FROM users"))
            assert stored.scalar_one() is None
            await connection.run_sync(lambda sync: command.downgrade(build_config(sync), "0010"))
        async with engine.connect() as connection:
            info = await connection.execute(text("PRAGMA table_info(users)"))
            names = [row[1] for row in info.all()]
            index = await connection.execute(
                text("SELECT count(*) FROM sqlite_master WHERE name = 'uq_users_email_lower'")
            )
            kept = (await connection.execute(text("SELECT count(*) FROM users"))).scalar_one()
        assert "totp_enabled_at" not in names and index.scalar_one() == 1 and kept == 1
    finally:
        await engine.dispose()


async def test_the_extra_addresses_column_is_added_and_removed_in_place_and_keeps_projects(
    database_path: Path, backup_dir: Path
) -> None:
    """Migration 0013 gives a project that existed before it an empty list; going back drops the
    column, keeps the projects and the autoincrement ids, and going forward starts empty again."""
    engine = create_engine(database_path)
    try:
        await migrate_database(engine, database_path, backup_dir)
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: command.downgrade(build_config(sync), "0012"))
            await connection.execute(
                text(
                    "INSERT INTO projects (id, name, created_at, updated_at)"
                    " VALUES (7, 'before', '2026-01-01', '2026-01-01')"
                )
            )
            await connection.run_sync(lambda sync: command.upgrade(build_config(sync), "head"))
            columns = (await connection.execute(text("PRAGMA table_info(projects)"))).all()
            added = next(row for row in columns if row[1] == "extra_addresses")
            assert added[3] == 1 and added[4] == "'[]'"
            stored = await connection.execute(text("SELECT extra_addresses FROM projects"))
            assert stored.scalar_one() == "[]"
            await connection.execute(
                text("UPDATE projects SET extra_addresses = :list WHERE id = 7"),
                {"list": '["team@example.org"]'},
            )
            await connection.run_sync(lambda sync: command.downgrade(build_config(sync), "0012"))
        async with engine.connect() as connection:
            info = await connection.execute(text("PRAGMA table_info(projects)"))
            names = [row[1] for row in info.all()]
            kept = (await connection.execute(text("SELECT id, name FROM projects"))).all()
            table_sql = (
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'projects'")
                )
            ).scalar_one()
        assert "extra_addresses" not in names and [tuple(row) for row in kept] == [(7, "before")]
        assert "AUTOINCREMENT" in table_sql
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: command.upgrade(build_config(sync), "head"))
            again = await connection.execute(text("SELECT extra_addresses FROM projects"))
            assert again.scalar_one() == "[]"
    finally:
        await engine.dispose()


async def duplicate_setting_error(engine: AsyncEngine, secret: str) -> str:
    """The text of the error raised by saving a setting key twice, the value being ``secret``."""
    async with create_session_factory(engine)() as session:
        session.add(AppSetting(key="duplicate", value="first"))
        await session.commit()
        session.add(AppSetting(key="duplicate", value=secret))
        with pytest.raises(IntegrityError) as raised:
            await session.commit()
        return str(raised.value) + repr(raised.value)


async def test_database_error_text_never_holds_the_values_bound_to_the_statement(
    engine: AsyncEngine,
) -> None:
    """The text of an exception reaches logs, error reports and traces, so it names the statement
    and never the parameters (a stored secret would be one)."""
    secret = "hunter2-database-password"
    text_of_error = await duplicate_setting_error(engine, secret)
    assert "UNIQUE constraint failed" in text_of_error and "app_settings" in text_of_error
    assert secret not in text_of_error and "[parameters:" not in text_of_error


async def test_an_engine_without_hidden_parameters_would_leak_them(
    database_path: Path, migrated_template: Path
) -> None:
    """Pins the premise of the test above: SQLAlchemy puts the parameters in the error text."""
    copy_database(migrated_template, database_path)
    visible = create_async_engine(sqlite_url(database_path))
    try:
        assert "hunter2-database-password" in await duplicate_setting_error(
            visible, "hunter2-database-password"
        )
    finally:
        await visible.dispose()
