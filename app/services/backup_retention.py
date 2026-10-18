"""Retention: removing the oldest backups of the backup folder.

Only our own files in the backup folder are ever removed, and never the copies in the extra
destination. A limit by count keeps the newest N backups and a limit by days keeps those taken
within the last N days; when both are set a backup must pass both to stay. The newest backup is
always kept, so a short limit cannot leave the application with none. Records of failed backups
carry no file; only the newest few are kept, so a long outage does not fill the list.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import get_logger
from app.models import Backup, BackupStatus
from app.services.app_settings import BackupSettings
from app.services.backup_files import remove_backup_file
from app.services.backup_records import HAS_FILE

log = get_logger("backup_retention")

MAX_FAILED_RECORDS = 20


def expired_backups(
    rows: Sequence[Backup], *, keep_count: int, keep_days: int, now: datetime
) -> list[Backup]:
    """The records that no longer fit the limits, from the backups that have a file.

    ``rows`` may come in any order. A limit of zero is no limit of that kind.
    """
    ordered = sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)
    cutoff = now - timedelta(days=keep_days) if keep_days > 0 else None
    expired: list[Backup] = []
    for position, row in enumerate(ordered):
        if position == 0:
            continue
        too_many = keep_count > 0 and position >= keep_count
        too_old = cutoff is not None and row.created_at < cutoff
        if too_many or too_old:
            expired.append(row)
    return expired


async def apply_retention(
    db: AsyncSession, backup_dir: Path, settings: BackupSettings, *, now: datetime
) -> int:
    """Remove the files and records that exceed the limits and return how many files went.

    A file that cannot be removed keeps its record, so the list never claims a file is gone
    while it is still there. The caller commits.
    """
    rows = (await db.execute(select(Backup).where(Backup.status.in_(HAS_FILE)))).scalars().all()
    removed = 0
    for row in expired_backups(
        rows,
        keep_count=settings.retention_count,
        keep_days=settings.retention_days,
        now=now,
    ):
        if not remove_backup_file(backup_dir, row.filename):
            log.warning("a backup file could not be removed by retention")
            continue
        await db.delete(row)
        removed += 1
    failed = (
        (
            await db.execute(
                select(Backup.id)
                .where(Backup.status == BackupStatus.FAILURE)
                .order_by(Backup.created_at.desc(), Backup.id.desc())
                .offset(MAX_FAILED_RECORDS)
            )
        )
        .scalars()
        .all()
    )
    if failed:
        await db.execute(delete(Backup).where(Backup.id.in_(failed)))
    await db.flush()
    return removed
