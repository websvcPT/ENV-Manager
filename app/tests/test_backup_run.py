"""Taking a backup: records, audit entries, the extra copy, retention, locking and events."""

import asyncio
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.db import create_session_factory
from app.models import (
    AuditActorType,
    AuditResult,
    Backup,
    BackupStatus,
    BackupTrigger,
    User,
)
from app.seeds import SeedData
from app.services.audit import AuditActor
from app.services.backup import backup_file_name
from app.services.backup_lock import BackupBusyError, hold_backup_lock
from app.services.backup_records import adopt_untracked_backups
from app.services.backup_run import BackupLocations, adopt_backups, run_backup
from app.tests.backup_helpers import (
    FailingBackupNotifier,
    RecordingBackupNotifier,
    allow_path,
    audit_entries,
    backup_records,
    locations_in,
    read_rows,
    save_backup_settings,
)


@pytest.fixture
def factory(seeded_database: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions on the seeded database."""
    return create_session_factory(seeded_database)


@pytest.fixture
def locations(tmp_path: Path, database_path: Path) -> BackupLocations:
    """Where the seeded database, the backups and the locks are."""
    return locations_in(tmp_path, database_path)


async def take(
    factory: async_sessionmaker[AsyncSession],
    locations: BackupLocations,
    notifier: RecordingBackupNotifier | FailingBackupNotifier,
    trigger: BackupTrigger = BackupTrigger.MANUAL,
    actor: AuditActor | None = None,
) -> Backup:
    """Take a backup in its own session and return its record."""
    async with factory() as db:
        run = await run_backup(
            db, locations, trigger=trigger, actor=actor or AuditActor.cli(), notifier=notifier
        )
        return run.record


async def test_a_manual_backup_is_recorded_audited_and_announced(
    factory: async_sessionmaker[AsyncSession],
    locations: BackupLocations,
    seed_data: SeedData,
) -> None:
    """The record has time, size, trigger and status; the audit entry and event say the same."""
    notifier = RecordingBackupNotifier()
    async with factory() as db:
        root = (
            await db.execute(select(User).where(User.name == seed_data.users.superadmin.name))
        ).scalar_one()
        run = await run_backup(
            db,
            locations,
            trigger=BackupTrigger.MANUAL,
            actor=AuditActor.for_user(root, "198.51.100.4"),
            notifier=notifier,
        )
    record = run.record
    path = locations.backup_dir / record.filename
    assert (record.trigger, record.status) == (BackupTrigger.MANUAL, BackupStatus.SUCCESS)
    assert record.size_bytes == path.stat().st_size > 0
    assert record.error is None and record.created_by_id == root.id
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    (entry,) = await audit_entries(factory, "backup.create")
    assert (entry.actor_name, entry.actor_type, entry.result) == (
        root.name,
        AuditActorType.USER,
        AuditResult.SUCCESS,
    )
    assert entry.ip_address == "198.51.100.4" and entry.target_name == record.filename
    assert entry.details == {
        "trigger": "manual",
        "status": "success",
        "size_bytes": record.size_bytes,
        "extra_destination": "none",
        "pruned": 0,
        "error": None,
    }
    (event,) = notifier.events
    assert (event.status, event.trigger, event.size_bytes) == (
        BackupStatus.SUCCESS,
        BackupTrigger.MANUAL,
        record.size_bytes,
    )
    assert event.filename == record.filename and event.actor == root.name and event.error is None


async def test_the_scheduler_acts_as_the_system_and_the_cli_as_cli(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> None:
    """Scheduled backups are audited as the system actor, command line ones as the CLI."""
    notifier = RecordingBackupNotifier()
    await take(
        factory, locations, notifier, BackupTrigger.SCHEDULED, AuditActor.system("scheduler")
    )
    await take(factory, locations, notifier, BackupTrigger.CLI, AuditActor.cli())
    first, second = await audit_entries(factory, "backup.create")
    assert (first.actor_type, first.actor_name, first.actor_user_id) == (
        AuditActorType.SYSTEM,
        "scheduler",
        None,
    )
    assert (second.actor_type, second.actor_name) == (AuditActorType.CLI, "cli")
    records = await backup_records(factory)
    assert [record.trigger for record in records] == [BackupTrigger.SCHEDULED, BackupTrigger.CLI]


async def test_a_failed_snapshot_is_a_recorded_failure_without_paths(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, database_path: Path
) -> None:
    """A folder that cannot be used leaves a failure record, an audit entry and an event."""
    blocked = tmp_path / "backups"
    blocked.write_text("a file, not a folder")
    notifier = RecordingBackupNotifier()
    record = await take(factory, locations_in(tmp_path, database_path), notifier)
    assert (record.status, record.size_bytes) == (BackupStatus.FAILURE, None)
    assert record.error == "The backup folder cannot be used."
    (entry,) = await audit_entries(factory, "backup.create")
    assert entry.result is AuditResult.FAILURE
    assert entry.details["error"] == record.error and entry.details["status"] == "failure"
    (event,) = notifier.events
    assert (event.status, event.error, event.size_bytes) == (
        BackupStatus.FAILURE,
        record.error,
        None,
    )
    for text in (record.error, str(entry.details), str(event)):
        assert str(tmp_path) not in text


async def test_a_failure_does_not_prune_the_backups_that_exist(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, database_path: Path
) -> None:
    """When the new backup cannot be written, the old ones stay whatever retention says."""
    locations = locations_in(tmp_path, database_path)
    await save_backup_settings(factory, retention_count=1)
    notifier = RecordingBackupNotifier()
    good = await take(factory, locations, notifier)
    broken = locations_in(tmp_path / "other", database_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "backups").write_text("blocked")
    failed = await take(factory, broken, notifier)
    assert failed.status is BackupStatus.FAILURE
    assert (locations.backup_dir / good.filename).exists()


async def test_the_extra_destination_gets_a_private_copy(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations, tmp_path: Path
) -> None:
    """With an allowed extra folder the backup is copied there and the audit entry says so."""
    destination = (tmp_path / "allowed" / "backups").resolve()
    destination.mkdir(parents=True)
    await allow_path(factory, destination.parent)
    await save_backup_settings(factory, extra_destination=str(destination))
    record = await take(factory, locations, RecordingBackupNotifier())
    copy = destination / record.filename
    assert record.status is BackupStatus.SUCCESS and record.error is None
    assert record.extra_destination_path == str(destination)
    assert copy.read_bytes() == (locations.backup_dir / record.filename).read_bytes()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    (entry,) = await audit_entries(factory, "backup.create")
    assert entry.details["extra_destination"] == "copied"


async def test_an_extra_destination_outside_the_allowed_paths_is_not_written(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations, tmp_path: Path
) -> None:
    """A destination that is not (or no longer) allowed gives a partial backup, no copy."""
    destination = (tmp_path / "unallowed").resolve()
    destination.mkdir()
    await save_backup_settings(factory, extra_destination=str(destination))
    notifier = RecordingBackupNotifier()
    record = await take(factory, locations, notifier)
    assert record.status is BackupStatus.PARTIAL
    assert record.error == "The extra destination is outside the allowed paths."
    assert record.size_bytes and (locations.backup_dir / record.filename).exists()
    assert list(destination.iterdir()) == []
    (entry,) = await audit_entries(factory, "backup.create")
    assert (entry.result, entry.details["extra_destination"]) == (AuditResult.FAILURE, "failed")
    (event,) = notifier.events
    assert event.status is BackupStatus.PARTIAL and event.size_bytes == record.size_bytes
    assert str(tmp_path) not in (record.error or "")


async def test_an_extra_destination_that_became_a_symlink_out_is_not_followed(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations, tmp_path: Path
) -> None:
    """The folder was swapped for a link to a place outside the list: nothing lands there."""
    allowed_root = (tmp_path / "allowed").resolve()
    destination = allowed_root / "backups"
    destination.mkdir(parents=True)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    await allow_path(factory, allowed_root)
    await save_backup_settings(factory, extra_destination=str(destination))
    destination.rmdir()
    destination.symlink_to(outside)
    record = await take(factory, locations, RecordingBackupNotifier())
    assert record.status is BackupStatus.PARTIAL
    assert list(outside.iterdir()) == []


async def test_retention_prunes_our_files_and_never_the_extra_destination(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations, tmp_path: Path
) -> None:
    """Keeping two leaves two local files and records, and every copy in the extra folder."""
    destination = (tmp_path / "allowed" / "backups").resolve()
    destination.mkdir(parents=True)
    await allow_path(factory, destination.parent)
    await save_backup_settings(factory, retention_count=2, extra_destination=str(destination))
    records = [await take(factory, locations, RecordingBackupNotifier()) for _ in range(4)]
    local = sorted(entry.name for entry in locations.backup_dir.iterdir())
    assert local == sorted(record.filename for record in records[2:])
    assert sorted(entry.name for entry in destination.iterdir()) == sorted(
        record.filename for record in records
    )
    kept = await backup_records(factory)
    assert [record.filename for record in kept] == [record.filename for record in records[2:]]
    last = (await audit_entries(factory, "backup.create"))[-1]
    assert last.details["pruned"] == 1


async def test_retention_by_days_runs_after_a_backup(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> None:
    """A file older than the window is removed by the next backup, the new one stays."""
    old = backup_file_name("manual", datetime(2020, 1, 1, tzinfo=UTC))
    locations.backup_dir.mkdir(mode=0o700)
    (locations.backup_dir / old).write_bytes(b"old")
    await save_backup_settings(factory, retention_count=0, retention_days=30)
    record = await take(factory, locations, RecordingBackupNotifier())
    assert [entry.name for entry in locations.backup_dir.iterdir()] == [record.filename]


async def test_a_second_backup_is_refused_while_one_runs(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> None:
    """The lock is not waited for: the second request fails at once and records nothing."""
    with hold_backup_lock(locations.lock_dir), pytest.raises(BackupBusyError):
        await take(factory, locations, RecordingBackupNotifier())
    assert await backup_records(factory) == []
    assert await audit_entries(factory, "backup.create") == []


async def test_two_backups_started_together_run_one_after_the_other(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> None:
    """At most one of two simultaneous requests runs; the other is told it is busy."""
    notifier = RecordingBackupNotifier()
    results = await asyncio.gather(
        take(factory, locations, notifier),
        take(factory, locations, notifier),
        return_exceptions=True,
    )
    records = [result for result in results if isinstance(result, Backup)]
    busy = [result for result in results if isinstance(result, BackupBusyError)]
    assert len(records) + len(busy) == 2 and records


async def test_a_notifier_that_fails_does_not_fail_the_backup(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> None:
    """The backup is recorded and committed before the event, and a broken notifier is ignored."""
    record = await take(factory, locations, FailingBackupNotifier())
    assert record.status is BackupStatus.SUCCESS
    assert len(await backup_records(factory)) == 1


async def test_a_backup_taken_before_a_migration_gets_a_record(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> None:
    """Files with no record are adopted with the trigger their name carries."""
    locations.backup_dir.mkdir(mode=0o700)
    name = backup_file_name("pre-migration")
    (locations.backup_dir / name).write_bytes(b"x" * 10)
    (locations.backup_dir / "unrelated.db").write_bytes(b"y")
    assert await adopt_backups(factory, locations) == 1
    assert await adopt_backups(factory, locations) == 0
    (record,) = await backup_records(factory)
    assert (record.filename, record.trigger, record.size_bytes) == (
        name,
        BackupTrigger.MIGRATION,
        10,
    )
    async with factory() as db:
        assert await adopt_untracked_backups(db, locations.backup_dir) == 0


async def test_a_backup_file_named_for_a_key_rotation_is_adopted_as_one(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> None:
    """A rotation backup whose record was lost keeps its meaning from its name."""
    locations.backup_dir.mkdir(mode=0o700)
    name = backup_file_name(BackupTrigger.ROTATION.value)
    (locations.backup_dir / name).write_bytes(b"x" * 10)
    assert await adopt_backups(factory, locations) == 1
    (record,) = await backup_records(factory)
    assert (record.filename, record.trigger) == (name, BackupTrigger.ROTATION)


async def test_the_snapshot_is_the_database_as_it_was(
    factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> None:
    """The backup file holds the seeded rows and the audit entry is not part of it yet."""
    record = await take(factory, locations, RecordingBackupNotifier())
    path = locations.backup_dir / record.filename
    assert read_rows(path, "SELECT count(*) FROM users")[0][0] > 5
    assert read_rows(path, "SELECT count(*) FROM audit_entries WHERE action = 'backup.create'") == [
        (0,)
    ]
