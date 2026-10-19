"""The backup records: reading them, adopting files that have none, and carrying them over.

A record says when a backup was taken, why, how it ended and how large it is; the file itself
lives in the backup folder under a name that carries the moment and the reason. The folder is the
truth about which files exist, so a file with no record (a backup taken before a migration, which
runs before the table is current) gets one, and a record whose file has gone is dropped. A
restore replaces the table with the one in the backup, so the records of the current database are
carried over into it afterwards.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Backup, BackupStatus, BackupTrigger
from app.services.backup import parse_backup_name
from app.services.backup_files import list_backup_files

TRIGGER_BY_REASON = {
    "manual": BackupTrigger.MANUAL,
    "scheduled": BackupTrigger.SCHEDULED,
    "cli": BackupTrigger.CLI,
    "pre-migration": BackupTrigger.MIGRATION,
    "pre-restore": BackupTrigger.RESTORE,
    "rotation": BackupTrigger.ROTATION,
}
REASON_BY_TRIGGER = {
    BackupTrigger.MANUAL: "manual",
    BackupTrigger.SCHEDULED: "scheduled",
    BackupTrigger.CLI: "cli",
    BackupTrigger.MIGRATION: "pre-migration",
    BackupTrigger.RESTORE: "pre-restore",
    BackupTrigger.ROTATION: "rotation",
}
HAS_FILE = (BackupStatus.SUCCESS, BackupStatus.PARTIAL)


@dataclass(frozen=True)
class RecordSnapshot:
    """The content of one record, kept apart from the session it was read in."""

    filename: str
    trigger: BackupTrigger
    status: BackupStatus
    size_bytes: int | None
    extra_destination_path: str | None
    error: str | None
    created_at: datetime


async def list_backups(db: AsyncSession, limit: int) -> tuple[list[Backup], int]:
    """The newest records (at most ``limit``) and how many records there are in all."""
    rows = (
        await db.execute(
            select(Backup).order_by(Backup.created_at.desc(), Backup.id.desc()).limit(limit)
        )
    ).scalars()
    return list(rows), await count_backups(db)


async def get_backup(db: AsyncSession, backup_id: int) -> Backup | None:
    """One record by id."""
    return await db.get(Backup, backup_id)


async def latest_backup(db: AsyncSession) -> Backup | None:
    """The most recent record, whatever its outcome."""
    return (
        await db.execute(
            select(Backup).order_by(Backup.created_at.desc(), Backup.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def latest_snapshot_time(db: AsyncSession) -> datetime | None:
    """When the newest backup that produced a snapshot was taken, None when there is none.

    A partial backup counts: its snapshot exists and can be restored.
    """
    return (
        await db.execute(
            select(func.max(Backup.created_at)).where(
                Backup.status.in_((BackupStatus.SUCCESS, BackupStatus.PARTIAL))
            )
        )
    ).scalar_one()


async def count_backups(db: AsyncSession) -> int:
    """How many records exist."""
    return int((await db.execute(select(func.count()).select_from(Backup))).scalar_one())


async def adopt_untracked_backups(db: AsyncSession, backup_dir: Path) -> int:
    """Give a record to every backup file in the folder that has none, and return how many.

    The trigger comes from the reason in the file name and the time from its moment. The caller
    holds the backup lock, so a backup being written is never seen half done, and commits.
    """
    known = set((await db.execute(select(Backup.filename))).scalars())
    adopted = 0
    for name, size in sorted(list_backup_files(backup_dir).items()):
        parsed = parse_backup_name(name)
        if name in known or parsed is None or parsed.reason not in TRIGGER_BY_REASON:
            continue
        db.add(
            Backup(
                filename=name,
                trigger=TRIGGER_BY_REASON[parsed.reason],
                status=BackupStatus.SUCCESS,
                size_bytes=size,
                created_at=parsed.moment,
            )
        )
        adopted += 1
    if adopted:
        await db.flush()
    return adopted


def snapshot_records(rows: Sequence[Backup]) -> list[RecordSnapshot]:
    """The records as plain values, to be written into another database."""
    return [
        RecordSnapshot(
            row.filename,
            row.trigger,
            row.status,
            row.size_bytes,
            row.extra_destination_path,
            row.error,
            row.created_at,
        )
        for row in rows
    ]


async def merge_records(
    db: AsyncSession, carried: Sequence[RecordSnapshot], backup_dir: Path
) -> None:
    """Make the records of this database match the backup folder after a restore.

    Records of the database that was replaced are added unless the same file is already
    recorded (the author is dropped: user ids in the restored data may belong to other people),
    records whose file is gone are removed and files with no record are adopted. The caller
    commits.
    """
    known = set((await db.execute(select(Backup.filename))).scalars())
    for item in carried:
        if item.filename in known:
            continue
        db.add(
            Backup(
                filename=item.filename,
                trigger=item.trigger,
                status=item.status,
                size_bytes=item.size_bytes,
                extra_destination_path=item.extra_destination_path,
                error=item.error,
                created_at=item.created_at,
            )
        )
    await db.flush()
    present = set(list_backup_files(backup_dir))
    await db.execute(
        delete(Backup).where(Backup.status.in_(HAS_FILE), Backup.filename.not_in(present))
    )
    await adopt_untracked_backups(db, backup_dir)
