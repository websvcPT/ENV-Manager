"""Tests for the engine, migrations, backups and the startup key check."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)

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
        await _migrate(engine, "0009", down=True)
        async with engine.begin() as connection:
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


USER_TREE_STATEMENTS = (
    'INSERT INTO users (id, name, email, password_hash, "group", is_active, totp_enabled,'
    " created_at, updated_at) VALUES (1, 'ann', 'ann@example.test', 'x', 'admin', 1, 0,"
    " '2026-01-01', '2026-01-01')",
    'INSERT INTO users (id, name, email, password_hash, "group", is_active, totp_enabled,'
    " created_at, updated_at) VALUES (2, 'bob', 'bob@example.test', 'x', 'viewer', 1, 0,"
    " '2026-01-01', '2026-01-01')",
    "INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at)"
    " VALUES (1, 'h', 1, '2026-01-01', '2026-01-01', '2026-01-02')",
    "INSERT INTO api_tokens (id, owner_id, name, token_hash, prefix, operations, created_at)"
    " VALUES (1, 1, 't', 'th', 'envm_ab', '[]', '2026-01-01')",
    "INSERT INTO audit_entries (id, created_at, actor_type, actor_user_id, action, result,"
    " version_ids, details) VALUES (1, '2026-01-01', 'user', 1, 'login.success', 'success',"
    " '[]', '{}')",
)


async def _user_tree_state(connection: AsyncConnection) -> dict[str, object]:
    """What the users migration must keep or change, read from the migrated database."""
    return {
        "children": [
            (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            for table in ("user_sessions", "api_tokens")
        ],
        "audit_actor": (
            await connection.execute(text("SELECT actor_user_id FROM audit_entries"))
        ).scalar_one(),
        "table_sql": (
            await connection.execute(text("SELECT sql FROM sqlite_master WHERE name = 'users'"))
        ).scalar_one(),
        "email_index": (
            await connection.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'uq_users_email_lower'")
            )
        ).scalar_one_or_none(),
        "violations": (await connection.execute(text("PRAGMA foreign_key_check"))).all(),
        "enforcement": (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one(),
    }


async def test_the_users_migration_keeps_children_lets_names_repeat_and_goes_back(
    database_path: Path, backup_dir: Path
) -> None:
    """Rebuilding users keeps every user and what hangs on it, keeps the email index, drops the
    unique name and the id reuse, and the way back refuses shared names, then restores both."""
    engine = create_engine(database_path)
    try:
        await _migrate(engine, "0015")
        async with engine.begin() as connection:
            for statement in USER_TREE_STATEMENTS:
                await connection.execute(text(statement))
        await migrate_database(engine, database_path, backup_dir)
        # A refused way back leaves what an earlier downgrade already did (SQLite does not roll
        # back a column drop), so the check below starts at the revision of this migration.
        await _migrate(engine, "0016", down=True)
        async with engine.connect() as connection:
            after = await _user_tree_state(connection)
            sequence = (
                await connection.execute(
                    text("SELECT seq FROM sqlite_sequence WHERE name = 'users'")
                )
            ).scalar_one()
        assert after["children"] == [1, 1] and after["audit_actor"] == 1
        assert after["violations"] == [] and after["enforcement"] == 1
        assert "AUTOINCREMENT" in str(after["table_sql"])
        assert "uq_users_name" not in str(after["table_sql"])
        assert "UNIQUE" in str(after["email_index"]) and "lower(email)" in str(after["email_index"])
        assert sequence == 2

        async with engine.begin() as connection:
            await connection.execute(
                text(USER_INSERT), {"name": "bob", "email": "bob.two@example.test"}
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(USER_INSERT), {"name": "carl", "email": "BOB@example.TEST"}
                )
        await _migrate(engine, "0015", down=True, refused="names are shared: bob")
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM users WHERE name = 'bob' AND id > 2"))
            await connection.execute(
                text(USER_INSERT), {"name": "dee", "email": "dee@example.test"}
            )
            newest = (
                await connection.execute(text("SELECT max(id) FROM users WHERE name = 'dee'"))
            ).scalar_one()
        assert newest == 4

        await _migrate(engine, "0015", down=True)
        async with engine.connect() as connection:
            before = await _user_tree_state(connection)
        assert before["children"] == [1, 1] and before["audit_actor"] == 1
        assert before["violations"] == [] and "UNIQUE" in str(before["email_index"])
        assert "AUTOINCREMENT" not in str(before["table_sql"])
        assert "uq_users_name" in str(before["table_sql"])
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(USER_INSERT), {"name": "ann", "email": "another@example.test"}
                )
    finally:
        await engine.dispose()


async def _migrate(
    engine: AsyncEngine, revision: str, *, down: bool = False, refused: str | None = None
) -> None:
    """Move the schema to a revision on a connection with foreign keys off, as the runner does.

    With ``refused`` the move must fail with an error matching it and change nothing.
    """
    run = command.downgrade if down else command.upgrade
    async with engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            if refused is None:
                await connection.run_sync(lambda sync: run(build_config(sync), revision))
                await connection.commit()
            else:
                with pytest.raises(RuntimeError, match=refused):
                    await connection.run_sync(lambda sync: run(build_config(sync), revision))
                await connection.rollback()
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")


async def test_the_versions_migration_keeps_every_version_stops_id_reuse_and_goes_back(
    database_path: Path, backup_dir: Path
) -> None:
    """Rebuilding the versions table keeps the rows and what points at them, hands out ids from
    a monotonic sequence afterwards, and the way back keeps the rows and drops the sequence."""
    engine = create_engine(database_path)
    try:
        await _migrate(engine, "0017")
        async with engine.begin() as connection:
            for statement in TREE_STATEMENTS:
                await connection.execute(text(statement))
        await migrate_database(engine, database_path, backup_dir)
        async with engine.begin() as connection:
            table_sql = (
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'environment_versions'")
                )
            ).scalar_one()
            kept = await connection.execute(text("SELECT count(*) FROM environment_versions"))
            assert kept.scalar_one() == 1
            violations = (await connection.execute(text("PRAGMA foreign_key_check"))).all()
            await connection.execute(text("DELETE FROM environment_versions WHERE id = 1"))
            await connection.execute(
                text(
                    "INSERT INTO environment_versions (environment_id, number, content_encrypted,"
                    " content_hash, created_at) VALUES (1, 1, 'c', 'h', '2026-01-01')"
                )
            )
            new_id = (
                await connection.execute(text("SELECT id FROM environment_versions"))
            ).scalar_one()
        assert "AUTOINCREMENT" in table_sql and violations == []
        assert new_id == 2
        await _migrate(engine, "0017", down=True)
        async with engine.connect() as connection:
            earlier_sql = (
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'environment_versions'")
                )
            ).scalar_one()
            rows = (await connection.execute(text("SELECT id FROM environment_versions"))).all()
            violations = (await connection.execute(text("PRAGMA foreign_key_check"))).all()
        assert "AUTOINCREMENT" not in earlier_sql
        assert [row[0] for row in rows] == [2] and violations == []
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
        await _migrate(engine, "0010", down=True)
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


async def test_the_failure_notice_time_is_added_and_removed_in_place_and_keeps_the_state(
    database_path: Path, backup_dir: Path
) -> None:
    """Migration 0017 gives the git state row that existed before it no notice time; going back
    drops the column and keeps the row, and going forward starts without a notice again."""
    engine = create_engine(database_path)
    try:
        await migrate_database(engine, database_path, backup_dir)
        await _migrate(engine, "0016", down=True)
        async with engine.begin() as connection:
            columns = (await connection.execute(text("PRAGMA table_info(git_sync_state)"))).all()
            assert "failure_notified_at" not in [row[1] for row in columns]
            await connection.execute(
                text("INSERT INTO git_sync_state (id, failures) VALUES (1, 3)")
            )
        await _migrate(engine, "0017")
        async with engine.connect() as connection:
            columns = (await connection.execute(text("PRAGMA table_info(git_sync_state)"))).all()
            row = (
                await connection.execute(
                    text("SELECT failures, failure_notified_at FROM git_sync_state")
                )
            ).one()
        assert next(info[3] for info in columns if info[1] == "failure_notified_at") == 0
        assert tuple(row) == (3, None)
        await _migrate(engine, "0016", down=True)
        async with engine.connect() as connection:
            columns = (await connection.execute(text("PRAGMA table_info(git_sync_state)"))).all()
            state = await connection.execute(text("SELECT failures FROM git_sync_state"))
            kept = state.scalar_one()
        assert "failure_notified_at" not in [info[1] for info in columns] and kept == 3
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
        await _migrate(engine, "0012", down=True)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO projects (id, name, created_at, updated_at)"
                    " VALUES (7, 'before', '2026-01-01', '2026-01-01')"
                )
            )
        await _migrate(engine, "head")
        async with engine.begin() as connection:
            columns = (await connection.execute(text("PRAGMA table_info(projects)"))).all()
            added = next(row for row in columns if row[1] == "extra_addresses")
            assert added[3] == 1 and added[4] == "'[]'"
            stored = await connection.execute(text("SELECT extra_addresses FROM projects"))
            assert stored.scalar_one() == "[]"
            await connection.execute(
                text("UPDATE projects SET extra_addresses = :list WHERE id = 7"),
                {"list": '["team@example.org"]'},
            )
        await _migrate(engine, "0012", down=True)
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
        await _migrate(engine, "head")
        async with engine.begin() as connection:
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


PROJECT_INSERT = (
    "INSERT INTO projects (name, created_at, updated_at) VALUES (:name, '2026-01-01', '2026-01-01')"
)


async def test_the_database_refuses_a_project_name_that_differs_only_in_case(
    engine: AsyncEngine,
) -> None:
    """The unique index is on the lowercased name, so a case variant is a duplicate."""
    async with engine.begin() as connection:
        await connection.execute(text(PROJECT_INSERT), {"name": "Fancy Proj. Name"})
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(text(PROJECT_INSERT), {"name": "FANCY PROJ. NAME"})
    async with engine.connect() as connection:
        index_sql = (
            await connection.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'uq_projects_name_lower'")
            )
        ).scalar_one()
    assert "UNIQUE" in index_sql and "lower(name)" in index_sql


async def test_the_upgrade_refuses_projects_that_share_a_name_and_changes_nothing(
    database_path: Path, backup_dir: Path
) -> None:
    """Projects that differ only in case stop the upgrade, naming them."""
    engine = create_engine(database_path)
    try:
        await migrate_database(engine, database_path, backup_dir)
        await _migrate(engine, "0014", down=True)
        async with engine.begin() as connection:
            for name in ("Alpha", "alpha", "other"):
                await connection.execute(text(PROJECT_INSERT), {"name": name})
        with pytest.raises(RuntimeError) as refused:
            await migrate_database(engine, database_path, backup_dir)
        message = str(refused.value)
        assert "Alpha, alpha" in message and "other" not in message
        async with engine.connect() as connection:
            revision = await connection.execute(text("SELECT version_num FROM alembic_version"))
            projects = (
                await connection.execute(text("SELECT count(*) FROM projects"))
            ).scalar_one()
        assert revision.scalar_one() == "0014" and projects == 3
    finally:
        await engine.dispose()


async def test_the_project_name_index_is_added_and_removed_in_place_and_keeps_projects(
    database_path: Path, backup_dir: Path
) -> None:
    """Going back drops the index and keeps the projects; going forward adds it again."""
    engine = create_engine(database_path)
    try:
        await migrate_database(engine, database_path, backup_dir)
        async with engine.begin() as connection:
            await connection.execute(text(PROJECT_INSERT), {"name": "Fancy Proj. Name"})
        await _migrate(engine, "0014", down=True)
        async with engine.connect() as connection:
            gone = await connection.execute(
                text("SELECT count(*) FROM sqlite_master WHERE name = 'uq_projects_name_lower'")
            )
            kept = (await connection.execute(text("SELECT name FROM projects"))).scalars().all()
        assert gone.scalar_one() == 0 and kept == ["Fancy Proj. Name"]
        await migrate_database(engine, database_path, backup_dir)
        async with engine.connect() as connection:
            back = await connection.execute(
                text("SELECT count(*) FROM sqlite_master WHERE name = 'uq_projects_name_lower'")
            )
        assert back.scalar_one() == 1
    finally:
        await engine.dispose()


async def test_the_file_type_is_added_for_every_file_checked_and_removed_on_the_way_back(
    database_path: Path, backup_dir: Path
) -> None:
    """Migration 0019 makes the files that exist ENV files, refuses a type it does not know,
    keeps ids, versions, grants and the autoincrement sequence, and the way back drops the type
    and the check while keeping the rows."""
    engine = create_engine(database_path)
    try:
        await _migrate(engine, "0018")
        async with engine.begin() as connection:
            for statement in TREE_STATEMENTS:
                await connection.execute(text(statement))
        await migrate_database(engine, database_path, backup_dir)
        async with engine.begin() as connection:
            columns = (await connection.execute(text("PRAGMA table_info(environments)"))).all()
            added = next(row for row in columns if row[1] == "file_type")
            assert added[3] == 1 and added[4] == "'env'"
            stored = await connection.execute(text("SELECT id, name, file_type FROM environments"))
            assert [tuple(row) for row in stored.all()] == [(1, "e", "env")]
            table_sql = (
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'environments'")
                )
            ).scalar_one()
            assert "AUTOINCREMENT" in table_sql and "ck_environments_file_type" in table_sql
            kept = [
                (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
                for table in ("environment_versions", "access_grants")
            ]
            violations = (await connection.execute(text("PRAGMA foreign_key_check"))).all()
            assert kept == [1, 1] and violations == []
            await connection.execute(text("UPDATE environments SET file_type = 'cert'"))
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE environments SET file_type = 'binary'"))
        await _migrate(engine, "0018", down=True)
        async with engine.connect() as connection:
            columns = (await connection.execute(text("PRAGMA table_info(environments)"))).all()
            table_sql = (
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'environments'")
                )
            ).scalar_one()
            rows = (await connection.execute(text("SELECT id, name FROM environments"))).all()
            kept = [
                (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
                for table in ("environment_versions", "access_grants")
            ]
            violations = (await connection.execute(text("PRAGMA foreign_key_check"))).all()
        assert "file_type" not in [row[1] for row in columns]
        assert "AUTOINCREMENT" in table_sql and "ck_environments_file_type" not in table_sql
        assert [tuple(row) for row in rows] == [(1, "e")] and kept == [1, 1] and violations == []
        await _migrate(engine, "head")
        async with engine.connect() as connection:
            again = await connection.execute(text("SELECT file_type FROM environments"))
        assert again.scalar_one() == "env"
    finally:
        await engine.dispose()


async def test_the_compose_files_column_is_added_and_removed_in_place_and_keeps_deployments(
    database_path: Path, backup_dir: Path
) -> None:
    """Migration 0020 gives a deployment that existed before it no Compose files, keeps what a
    later import stores until the way back drops the column, and keeps the rows and the
    autoincrement ids both ways."""
    engine = create_engine(database_path)
    try:
        await _migrate(engine, "0019")
        async with engine.begin() as connection:
            for statement in TREE_STATEMENTS:
                await connection.execute(text(statement))
        await migrate_database(engine, database_path, backup_dir)
        async with engine.begin() as connection:
            columns = (await connection.execute(text("PRAGMA table_info(deployments)"))).all()
            added = next(row for row in columns if row[1] == "compose_files")
            assert added[3] == 0 and added[4] is None
            stored = await connection.execute(text("SELECT id, compose_files FROM deployments"))
            assert [tuple(row) for row in stored.all()] == [(1, None)]
            await connection.execute(
                text("UPDATE deployments SET compose_files = :files"),
                {"files": '["compose.yaml", "compose.override.yaml"]'},
            )
            violations = (await connection.execute(text("PRAGMA foreign_key_check"))).all()
            assert violations == []
        await _migrate(engine, "0019", down=True)
        async with engine.connect() as connection:
            names = [
                row[1]
                for row in (await connection.execute(text("PRAGMA table_info(deployments)"))).all()
            ]
            kept = (await connection.execute(text("SELECT id, name FROM deployments"))).all()
            table_sql = (
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'deployments'")
                )
            ).scalar_one()
        assert "compose_files" not in names and [tuple(row) for row in kept] == [(1, "d")]
        assert "AUTOINCREMENT" in table_sql
        await _migrate(engine, "head")
        async with engine.connect() as connection:
            again = await connection.execute(text("SELECT compose_files FROM deployments"))
        assert again.scalar_one() is None
    finally:
        await engine.dispose()


async def test_deployment_names_become_unique_ignoring_case_and_the_way_back_drops_the_index(
    database_path: Path, backup_dir: Path
) -> None:
    """Migration 0021 refuses a project whose deployments differ only in case, naming them, and
    otherwise adds the unique index on the project and the lowercased name; the way back drops
    it and keeps the names."""
    insert = (
        "INSERT INTO deployments (id, project_id, name, root_path, dist_missing_policy,"
        " drift_policy, missing_target_policy, created_at, updated_at)"
        " VALUES (:id, 1, :name, '/x', 'block', 'halt', 'create', '2026-01-01', '2026-01-01')"
    )
    engine = create_engine(database_path)
    try:
        await _migrate(engine, "0020")
        async with engine.begin() as connection:
            for statement in TREE_STATEMENTS:
                await connection.execute(text(statement))
            await connection.execute(text(insert), {"id": 2, "name": "D"})
        await _migrate(engine, "0021", refused="share a name: d, D")
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE deployments SET name = 'Web EU' WHERE id = 2"))
        await _migrate(engine, "0021")
        async with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(text(insert), {"id": 3, "name": "web eu"})
        await _migrate(engine, "0020", down=True)
        async with engine.begin() as connection:
            await connection.execute(text(insert), {"id": 3, "name": "web eu"})
            names = (await connection.execute(text("SELECT name FROM deployments"))).scalars()
            assert sorted(names) == ["Web EU", "d", "web eu"]
    finally:
        await engine.dispose()


async def test_docker_registries_are_added_and_dropped_by_the_migration_and_keep_other_rows(
    database_path: Path, backup_dir: Path
) -> None:
    """Migration 0022 adds the registries table next to existing rows, with a host that is unique
    ignoring case and ids that are never handed out twice; the way back drops the table and its
    index and keeps every other row, and going forward again gives an empty table."""
    insert = (
        "INSERT INTO docker_registries (host, username, token_encrypted, created_at, updated_at)"
        " VALUES (:host, 'bot', 'sealed', '2026-01-01', '2026-01-01')"
    )
    engine = create_engine(database_path)
    try:
        await _migrate(engine, "0021")
        async with engine.begin() as connection:
            for statement in TREE_STATEMENTS:
                await connection.execute(text(statement))
        await migrate_database(engine, database_path, backup_dir)
        async with engine.begin() as connection:
            await connection.execute(text(insert), {"host": "Registry.Example.com"})
            await connection.execute(text(insert), {"host": "ghcr.io"})
            with pytest.raises(IntegrityError):
                await connection.execute(text(insert), {"host": "REGISTRY.example.COM"})
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM docker_registries WHERE host = 'ghcr.io'"))
            await connection.execute(text(insert), {"host": "quay.io"})
            ids = (
                (await connection.execute(text("SELECT id FROM docker_registries ORDER BY id")))
                .scalars()
                .all()
            )
            table_sql = (
                await connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE name = 'docker_registries'")
                )
            ).scalar_one()
        assert ids == [1, 3] and "AUTOINCREMENT" in table_sql
        await _migrate(engine, "0021", down=True)
        async with engine.connect() as connection:
            left = (
                await connection.execute(
                    text("SELECT name FROM sqlite_master WHERE name LIKE '%docker_registries%'")
                )
            ).all()
            kept = [
                (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
                for table in CHILD_TABLES
            ]
        assert left == [] and kept == [1] * len(CHILD_TABLES)
        await _migrate(engine, "head")
        async with engine.connect() as connection:
            again = await connection.execute(text("SELECT count(*) FROM docker_registries"))
        assert again.scalar_one() == 0
    finally:
        await engine.dispose()
