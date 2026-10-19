"""Taking a backup: snapshot, optional extra copy, record, audit, retention and the event.

One backup at a time runs, across processes, under the backup lock; a second request is refused
at once with ``BackupBusyError``. The snapshot is taken first. The copy to the extra destination
follows, with the allowed paths loaded fresh, and its failure leaves a ``partial`` record: the
backup exists and can be restored, but is not where it was meant to be. Retention runs only after
a snapshot exists. The record and its audit entry are committed together, then the event is
announced to the notifier. Errors stored with a record are fixed messages, never a path or
operating system text.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import metrics, telemetry
from app.core.log import get_logger
from app.models import AuditResult, Backup, BackupStatus, BackupTrigger
from app.models.types import utc_now
from app.services.app_settings import BackupSettings, load_stored_settings
from app.services.audit import AuditActor, record_actor_audit
from app.services.backup import BackupSnapshotError, backup_file_name, create_backup
from app.services.backup_destination import ExtraDestinationError, copy_to_extra_destination
from app.services.backup_events import BackupFinished, BackupNotifier, announce_backup
from app.services.backup_files import backup_file_size
from app.services.backup_lock import BackupBusyError, hold_backup_lock
from app.services.backup_records import adopt_untracked_backups
from app.services.backup_retention import apply_retention
from app.services.paths import load_allowed_paths

log = get_logger("backup")

ACTION_CREATE = "backup.create"
TARGET_TYPE = "backup"


@dataclass(frozen=True)
class BackupLocations:
    """Where the live database, the backup folder and the lock folder are."""

    database_path: Path
    backup_dir: Path
    lock_dir: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> BackupLocations:
        """The locations inside the data volume of the startup configuration."""
        return cls(settings.database_path, settings.backup_path, settings.lock_path)


@dataclass(frozen=True)
class BackupRun:
    """What one backup did: its record and how many old backups retention removed."""

    record: Backup
    pruned: int


@dataclass(frozen=True)
class Attempt:
    """One backup being taken: why, by whom, the file it writes and when it started."""

    trigger: BackupTrigger
    actor: AuditActor
    name: str
    moment: datetime


async def run_backup(
    db: AsyncSession,
    locations: BackupLocations,
    *,
    trigger: BackupTrigger,
    actor: AuditActor,
    notifier: BackupNotifier,
) -> BackupRun:
    """Take one backup, record and audit it, apply retention and announce the result.

    Settings and the allowed paths are read here, so a change is effective for the next backup.
    A failed snapshot is a recorded failure, not an exception. Raises ``BackupBusyError`` when
    another backup or restore is running.

    The backup runs in a span that carries the trigger and the result, and is counted by result.
    A backup that left a snapshot also moves the time the backup age gauge reads.
    """
    attributes = {telemetry.TRIGGER: trigger.value, telemetry.ACTOR_TYPE: actor.type.value}
    with telemetry.span(telemetry.BACKUP_SPAN, attributes) as active:
        try:
            run = await _take_backup(db, locations, trigger, actor, notifier)
        except BackupBusyError:
            metrics.record_backup("busy", trigger.value)
            raise
        except Exception:
            metrics.record_backup("error", trigger.value)
            raise
        record = run.record
        result = {
            telemetry.RESULT: record.status.value,
            telemetry.BACKUP_ID: record.id,
            telemetry.BACKUP_PRUNED: run.pruned,
        }
        telemetry.annotate(active, result)
        metrics.record_backup(record.status.value, trigger.value)
        if record.status in (BackupStatus.SUCCESS, BackupStatus.PARTIAL):
            metrics.backup_clock.note(record.created_at)
        return run


async def _take_backup(
    db: AsyncSession,
    locations: BackupLocations,
    trigger: BackupTrigger,
    actor: AuditActor,
    notifier: BackupNotifier,
) -> BackupRun:
    """The backup, its record and audit entry, and the announcement of the result."""
    moment = utc_now()
    attempt = Attempt(trigger, actor, backup_file_name(trigger.value, moment), moment)
    settings = (await load_stored_settings(db)).backup
    try:
        with hold_backup_lock(locations.lock_dir):
            result = await _run_locked(db, locations, settings, attempt)
    except BackupSnapshotError as error:
        result = await _record(db, locations, settings, attempt, error=str(error), size=None)
    await db.commit()
    await announce_backup(
        notifier,
        BackupFinished(
            status=result.record.status,
            trigger=trigger,
            filename=attempt.name,
            size_bytes=result.record.size_bytes,
            error=result.record.error,
            finished_at=utc_now(),
            actor=actor.label,
        ),
    )
    return result


async def _run_locked(
    db: AsyncSession, locations: BackupLocations, settings: BackupSettings, attempt: Attempt
) -> BackupRun:
    """The snapshot, the extra copy, the record and retention, with the lock held."""
    await adopt_untracked_backups(db, locations.backup_dir)
    try:
        await create_backup(
            locations.database_path,
            locations.backup_dir,
            attempt.trigger.value,
            moment=attempt.moment,
        )
    except BackupSnapshotError as snapshot_error:
        return await _record(db, locations, settings, attempt, error=str(snapshot_error), size=None)
    size = backup_file_size(locations.backup_dir, attempt.name)
    error: str | None = None
    if settings.extra_destination:
        allowed = await load_allowed_paths(db)
        try:
            await asyncio.to_thread(
                copy_to_extra_destination,
                locations.backup_dir,
                attempt.name,
                settings.extra_destination,
                allowed,
            )
        except ExtraDestinationError as copy_error:
            error = str(copy_error)
    return await _record(db, locations, settings, attempt, error=error, size=size)


async def _record(
    db: AsyncSession,
    locations: BackupLocations,
    settings: BackupSettings,
    attempt: Attempt,
    *,
    error: str | None,
    size: int | None,
) -> BackupRun:
    """Write the record, apply retention and write the audit entry (the caller commits).

    A missing size means no snapshot was written (failure); a snapshot with an error is the
    failed extra copy (partial). Retention runs once the new record exists, so the new backup
    counts as the newest, and not at all after a failure, so a failing backup never costs the
    ones already taken.
    """
    if size is None:
        status, copy_state = BackupStatus.FAILURE, "none"
    elif error is not None:
        status, copy_state = BackupStatus.PARTIAL, "failed"
    else:
        status = BackupStatus.SUCCESS
        copy_state = "copied" if settings.extra_destination else "none"
    record = Backup(
        filename=attempt.name,
        trigger=attempt.trigger,
        status=status,
        size_bytes=size,
        extra_destination_path=settings.extra_destination or None,
        error=error,
        created_by_id=attempt.actor.user.id if attempt.actor.user is not None else None,
        created_at=attempt.moment,
    )
    db.add(record)
    await db.flush()
    pruned = 0
    if size is not None:
        pruned = await apply_retention(db, locations.backup_dir, settings, now=utc_now())
    await record_actor_audit(
        db,
        attempt.actor,
        action=ACTION_CREATE,
        result=AuditResult.SUCCESS if status is BackupStatus.SUCCESS else AuditResult.FAILURE,
        target_type=TARGET_TYPE,
        target_id=record.id,
        target_name=attempt.name,
        details={
            "trigger": attempt.trigger.value,
            "status": status.value,
            "size_bytes": size,
            "extra_destination": copy_state,
            "pruned": pruned,
            "error": error,
        },
    )
    if status is not BackupStatus.SUCCESS:
        log.warning("a %s backup ended as %s", attempt.trigger.value, status.value)
    return BackupRun(record, pruned)


async def adopt_backups(
    session_factory: async_sessionmaker[AsyncSession], locations: BackupLocations
) -> int:
    """Record the backup files that have no record yet (the backup taken before a migration).

    Skipped when a backup or restore is running, which keeps the records in step with the files
    it is writing. Returns how many records were added.
    """
    try:
        with hold_backup_lock(locations.lock_dir):
            async with session_factory() as db:
                adopted = await adopt_untracked_backups(db, locations.backup_dir)
                await db.commit()
                return adopted
    except BackupBusyError:
        return 0
