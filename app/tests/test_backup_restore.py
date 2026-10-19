"""Restoring a backup: the round trip, every refusal, the safety backup and what must survive."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.migrations import build_config
from app.models import (
    AppSetting,
    AuditActorType,
    AuditResult,
    Backup,
    BackupStatus,
    BackupTrigger,
    UserSession,
)
from app.services.audit import AuditActor
from app.services.backup import backup_file_name
from app.services.backup_inspect import RestoreProblem, RestoreRefusedError
from app.services.backup_lock import hold_backup_lock
from app.services.backup_restore import restore_backup
from app.services.backup_run import BackupLocations, run_backup
from app.tests.audit_helpers import audit_entries_from
from app.tests.backup_helpers import (
    RecordingBackupNotifier,
    app_locations,
    app_session_factory,
    backup_records,
    read_rows,
)
from app.tests.conftest import sign_in

MARKER = "marker.value"


async def set_marker(application: FastAPI, value: str) -> None:
    """Write the marker setting, which a restore must bring back to the backed up value."""
    async with app_session_factory(application)() as db:
        row = await db.get(AppSetting, MARKER)
        if row is None:
            db.add(AppSetting(key=MARKER, value=value))
        else:
            row.value = value
        await db.commit()


async def marker(application: FastAPI) -> str | None:
    """The marker setting as the running application reads it now."""
    async with app_session_factory(application)() as db:
        row = await db.get(AppSetting, MARKER)
        return row.value if row else None


async def take_backup(application: FastAPI) -> Path:
    """Take a manual backup of the application's database and return its file."""
    locations = app_locations(application)
    async with app_session_factory(application)() as db:
        run = await run_backup(
            db,
            locations,
            trigger=BackupTrigger.MANUAL,
            actor=AuditActor.cli(),
            notifier=RecordingBackupNotifier(),
        )
    return locations.backup_dir / run.record.filename


async def restore(application: FastAPI, source: Path, actor: AuditActor | None = None) -> None:
    """Restore a file into the running application's database."""
    crypto: CryptoService = application.state.crypto
    async with app_session_factory(application)() as db:
        await restore_backup(
            db,
            app_locations(application),
            source=source,
            label=source.name,
            crypto=crypto,
            actor=actor or AuditActor.cli(),
        )


def leftovers(locations: BackupLocations) -> list[str]:
    """Working files of a restore that were left in the data folder."""
    return [
        entry.name
        for entry in locations.database_path.parent.iterdir()
        if entry.name.startswith(".restore-")
    ]


async def test_a_restore_brings_the_data_back_and_ends_every_session(
    web_app: FastAPI, web_client: httpx.AsyncClient
) -> None:
    """Rows return to the backed up state, no session survives and the restore is audited."""
    await sign_in(web_client, "root")
    await set_marker(web_app, "before")
    backup = await take_backup(web_app)
    await set_marker(web_app, "after")
    async with app_session_factory(web_app)() as db:
        sessions_before = (
            await db.execute(select(func.count()).select_from(UserSession))
        ).scalar_one()
    assert sessions_before >= 1

    await restore(web_app, backup)

    assert await marker(web_app) == "before"
    async with app_session_factory(web_app)() as db:
        assert (await db.execute(select(func.count()).select_from(UserSession))).scalar_one() == 0
    assert (await web_client.get("/projects")).headers["location"].startswith("/login")
    (entry,) = await audit_entries_from(app_session_factory(web_app), "backup.restore")
    assert (entry.actor_type, entry.actor_name, entry.result) == (
        AuditActorType.CLI,
        "cli",
        AuditResult.SUCCESS,
    )
    safety = entry.details["safety_backup"]
    assert isinstance(safety, str) and safety.endswith("-pre-restore.db")
    assert (backup.parent / safety).exists()
    assert (
        entry.details["backup"] == backup.name
        and entry.details["sessions_ended"] == sessions_before
    )
    assert entry.details["upgraded_from"] is None
    assert leftovers(app_locations(web_app)) == []


async def test_the_restored_database_is_still_wal_and_works_for_new_sessions(
    web_app: FastAPI, web_client: httpx.AsyncClient
) -> None:
    """The live file keeps its journal mode, and people can sign in again afterwards."""
    await set_marker(web_app, "before")
    backup = await take_backup(web_app)
    await restore(web_app, backup)
    path = app_locations(web_app).database_path
    assert read_rows(path, "PRAGMA journal_mode")[0][0] == "wal"
    assert read_rows(path, "PRAGMA integrity_check") == [("ok",)]
    await sign_in(web_client, "root")
    assert (await web_client.get("/projects")).status_code == 200


async def test_open_sessions_of_the_application_see_the_restored_data(web_app: FastAPI) -> None:
    """A session that held a read open while the restore ran reads the new data afterwards."""
    await set_marker(web_app, "before")
    backup = await take_backup(web_app)
    await set_marker(web_app, "after")
    async with app_session_factory(web_app)() as reader:
        assert (await reader.get(AppSetting, MARKER)).value == "after"  # type: ignore[union-attr]
        await restore(web_app, backup)
        await reader.rollback()
        reader.expire_all()
        assert (await reader.get(AppSetting, MARKER)).value == "before"  # type: ignore[union-attr]


async def test_records_of_backups_survive_the_restore(web_app: FastAPI) -> None:
    """The restored table is merged with the folder: new backups, failures and the safety backup."""
    factory = app_session_factory(web_app)
    backup = await take_backup(web_app)
    later = await take_backup(web_app)
    gone = await take_backup(web_app)
    gone.unlink()
    async with factory() as db:
        db.add(
            Backup(
                filename=backup_file_name("scheduled", datetime(2026, 10, 17, 2, tzinfo=UTC)),
                trigger=BackupTrigger.SCHEDULED,
                status=BackupStatus.FAILURE,
                error="The database could not be copied.",
                created_at=datetime(2026, 10, 17, 2, tzinfo=UTC),
            )
        )
        await db.commit()

    await restore(web_app, backup)

    records = await backup_records(factory)
    by_name = {record.filename: record for record in records}
    assert backup.name in by_name and later.name in by_name
    assert gone.name not in by_name
    assert any(record.status is BackupStatus.FAILURE for record in records)
    safety = [record for record in records if record.trigger is BackupTrigger.RESTORE]
    assert len(safety) == 1 and safety[0].status is BackupStatus.SUCCESS
    assert all(record.created_by_id is None for record in records if record.filename == later.name)


def other_key_backup(backup: Path, tmp_path: Path) -> Path:
    """A copy of the backup whose key check record was sealed with another master key."""
    copy = tmp_path / "other-key.db"
    copy.write_bytes(backup.read_bytes())
    other = CryptoService(parse_master_key(generate_master_key()))
    with closing(sqlite3.connect(copy)) as connection, connection:
        connection.execute(
            "UPDATE app_settings SET value = ? WHERE key = 'key_check'", (other.make_key_check(),)
        )
    return copy


async def assert_refused(
    web_app: FastAPI, source: Path, problem: RestoreProblem, *, sessions_alive: bool = True
) -> RestoreRefusedError:
    """The restore is refused for the reason and changed nothing at all."""
    await set_marker(web_app, "current")
    with pytest.raises(RestoreRefusedError) as refused:
        await restore(web_app, source)
    assert refused.value.problem is problem
    assert await marker(web_app) == "current"
    locations = app_locations(web_app)
    assert leftovers(locations) == []
    assert not list(locations.backup_dir.glob("*-pre-restore.db"))
    (entry,) = await audit_entries_from(app_session_factory(web_app), "backup.restore")
    assert entry.result is AuditResult.BLOCKED and entry.details["reason"] == problem.value
    assert str(source.parent) not in str(entry.details)
    if sessions_alive:
        async with app_session_factory(web_app)() as db:
            assert (
                await db.execute(select(func.count()).select_from(UserSession))
            ).scalar_one() >= 1
    return refused.value


async def test_a_backup_made_with_another_key_is_refused_with_a_clear_message(
    web_app: FastAPI, web_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """The current key cannot read its values, so nothing is replaced and the message says why."""
    await sign_in(web_client, "root")
    backup = await take_backup(web_app)
    refusal = await assert_refused(
        web_app, other_key_backup(backup, tmp_path), RestoreProblem.WRONG_KEY
    )
    assert "different master key" in str(refusal)


async def test_a_file_that_is_not_a_backup_of_this_application_is_refused(
    web_app: FastAPI, web_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A foreign database, a text file and a missing file are refused without changes."""
    await sign_in(web_client, "root")
    foreign = tmp_path / "foreign.db"
    with closing(sqlite3.connect(foreign)) as connection, connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    refusal = await assert_refused(web_app, foreign, RestoreProblem.NOT_A_BACKUP)
    assert "not a backup made by ENV Manager" in str(refusal)


async def test_a_text_file_and_a_missing_file_are_refused(
    web_app: FastAPI, web_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Something that is not SQLite at all, and a path with nothing at it."""
    await sign_in(web_client, "root")
    notes = tmp_path / "notes.db"
    notes.write_text("just some notes\n" * 20)
    await assert_refused(web_app, notes, RestoreProblem.NOT_A_BACKUP)
    with pytest.raises(RestoreRefusedError) as missing:
        await restore(web_app, tmp_path / "nothing.db")
    assert missing.value.problem is RestoreProblem.UNREADABLE


async def test_a_damaged_backup_is_refused(
    web_app: FastAPI, web_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A backup whose pages were overwritten fails the integrity check."""
    await sign_in(web_client, "root")
    backup = await take_backup(web_app)
    damaged = tmp_path / "damaged.db"
    data = bytearray(backup.read_bytes())
    for offset in range(8192, len(data) - 4096, 8192):
        data[offset : offset + 4096] = b"\xff" * 4096
    damaged.write_bytes(bytes(data))
    refusal = await assert_refused(web_app, damaged, RestoreProblem.DAMAGED)
    assert "integrity" in str(refusal)


async def test_a_backup_of_a_newer_version_is_refused(
    web_app: FastAPI, web_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A migration revision this version does not know means a newer application made it."""
    await sign_in(web_client, "root")
    backup = await take_backup(web_app)
    newer = tmp_path / "newer.db"
    newer.write_bytes(backup.read_bytes())
    with closing(sqlite3.connect(newer)) as connection, connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999'")
    refusal = await assert_refused(web_app, newer, RestoreProblem.NEWER)
    assert "newer version" in str(refusal)


async def test_an_older_backup_is_brought_to_the_current_schema(
    web_app: FastAPI, tmp_path: Path
) -> None:
    """A backup from before later migrations restores, upgraded, with its data."""
    await set_marker(web_app, "older")
    backup = await take_backup(web_app)
    old = tmp_path / "older-schema.db"
    old.write_bytes(backup.read_bytes())
    engine = create_async_engine(f"sqlite+aiosqlite:///{old}")
    try:
        async with engine.connect() as connection:
            await connection.run_sync(lambda sync: command.downgrade(build_config(sync), "0010"))
            await connection.commit()
    finally:
        await engine.dispose()
    assert read_rows(old, "SELECT version_num FROM alembic_version") == [("0010",)]
    await set_marker(web_app, "newer")

    await restore(web_app, old)

    head = ScriptDirectory.from_config(build_config()).get_current_head()
    live = app_locations(web_app).database_path
    assert read_rows(live, "SELECT version_num FROM alembic_version") == [(head,)]
    assert await marker(web_app) == "older"
    columns = [row[1] for row in read_rows(live, "PRAGMA table_info(users)")]
    assert "totp_enabled_at" in columns
    (entry,) = await audit_entries_from(app_session_factory(web_app), "backup.restore")
    assert entry.details["upgraded_from"] == "0010"


async def test_a_failed_safety_backup_stops_the_restore(
    web_app: FastAPI, web_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """If the current data cannot be saved first, nothing is replaced."""
    await sign_in(web_client, "root")
    backup = await take_backup(web_app)
    await set_marker(web_app, "current")
    locations = app_locations(web_app)
    blocked = tmp_path / "blocked-folder"
    blocked.write_text("a file where the folder should be")
    broken = BackupLocations(locations.database_path, blocked, locations.lock_dir)
    async with app_session_factory(web_app)() as db:
        with pytest.raises(RestoreRefusedError) as refused:
            await restore_backup(
                db,
                broken,
                source=backup,
                label=backup.name,
                crypto=web_app.state.crypto,
                actor=AuditActor.cli(),
            )
    assert refused.value.problem is RestoreProblem.SAFETY_FAILED
    assert await marker(web_app) == "current"
    assert leftovers(locations) == []
    (entry,) = await audit_entries_from(app_session_factory(web_app), "backup.restore")
    assert entry.result is AuditResult.FAILURE


async def test_a_failure_while_switching_leaves_the_current_data(
    web_app: FastAPI, web_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch is one transaction: when it fails the live data is exactly as it was."""
    await sign_in(web_client, "root")
    backup = await take_backup(web_app)
    await set_marker(web_app, "current")

    def failing_switch(prepared: Path, live: Path) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("app.services.backup_restore.replace_database", failing_switch)
    with pytest.raises(RestoreRefusedError) as refused:
        await restore(web_app, backup)
    assert refused.value.problem is RestoreProblem.FAILED
    assert await marker(web_app) == "current"
    assert leftovers(app_locations(web_app)) == []
    assert list(app_locations(web_app).backup_dir.glob("*-pre-restore.db"))
    async with app_session_factory(web_app)() as db:
        assert (await db.execute(select(func.count()).select_from(UserSession))).scalar_one() >= 1
    assert "disk I/O" not in str(refused.value)


async def test_a_restore_is_refused_while_a_backup_runs(web_app: FastAPI) -> None:
    """The lock is shared: a restore does not start during a backup and records nothing."""
    backup = await take_backup(web_app)
    with (
        hold_backup_lock(app_locations(web_app).lock_dir),
        pytest.raises(RestoreRefusedError) as busy,
    ):
        await restore(web_app, backup)
    assert busy.value.problem is RestoreProblem.BUSY
    assert await audit_entries_from(app_session_factory(web_app), "backup.restore") == []


async def test_the_audit_entry_names_the_backup_and_never_a_user_id(
    web_app: FastAPI, web_client: httpx.AsyncClient
) -> None:
    """The restoring user may not exist in the restored data, so only their name is kept."""
    await sign_in(web_client, "root")
    backup = await take_backup(web_app)
    async with app_session_factory(web_app)() as db:
        user = (await db.execute(select(UserSession))).scalars().first()
        assert user is not None
    actor = AuditActor.system("a-person-who-is-not-in-the-backup")
    await restore(web_app, backup, actor)
    (entry,) = await audit_entries_from(app_session_factory(web_app), "backup.restore")
    assert entry.actor_user_id is None
    assert entry.actor_name == "a-person-who-is-not-in-the-backup"
