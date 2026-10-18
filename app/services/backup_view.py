"""What the Backup tab shows: the backup list and the forms' values.

A row is offered for download and restore only when its backup has a file in the backup folder
now, so a file removed by hand never leaves a link that fails. Sizes read like ``12.4 MB``.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Backup, BackupStatus, BackupTrigger
from app.services.app_settings import BackupSettings
from app.services.backup_destination import (
    OUTSIDE_MESSAGE,
    ExtraDestinationError,
    check_extra_destination,
)
from app.services.backup_files import list_backup_files
from app.services.backup_records import HAS_FILE, list_backups
from app.services.paths import AllowedPaths

LIST_LIMIT = 100
TRIGGER_LABELS = {
    BackupTrigger.MANUAL: "Manual",
    BackupTrigger.SCHEDULED: "Scheduled",
    BackupTrigger.CLI: "Command line",
    BackupTrigger.MIGRATION: "Before a migration",
    BackupTrigger.RESTORE: "Before a restore",
}
BADGES = {
    BackupStatus.SUCCESS: "success",
    BackupStatus.FAILURE: "failed",
    BackupStatus.PARTIAL: "partial",
}
_UNITS = ("KB", "MB", "GB", "TB")


def format_size(size: int) -> str:
    """A size in bytes as ``512 B``, ``20.0 KB``, ``12.4 MB`` and so on."""
    if size < 1000:
        return f"{size} B"
    value = float(size)
    for unit in _UNITS:
        value /= 1000
        if value < 1000 or unit == _UNITS[-1]:
            return f"{value:.1f} {unit}"
    return f"{size} B"


@dataclass(frozen=True)
class BackupRow:
    """One line of the backup list."""

    id: int
    created_at: datetime
    trigger: str
    status: BackupStatus
    size: str | None
    error: str | None
    has_file: bool

    @property
    def actionable(self) -> bool:
        """Whether the backup can be downloaded and restored."""
        return self.has_file and self.status in HAS_FILE

    @property
    def badge(self) -> str:
        """The key of the status badge of this outcome."""
        return BADGES[self.status]


@dataclass(frozen=True)
class BackupList:
    """The newest backups and how many records exist in all."""

    rows: list[BackupRow]
    total: int

    @property
    def truncated(self) -> bool:
        """Whether older records are not shown."""
        return self.total > len(self.rows)


def _row(record: Backup, files: dict[str, int]) -> BackupRow:
    """The list line of a record."""
    return BackupRow(
        id=record.id,
        created_at=record.created_at,
        trigger=TRIGGER_LABELS[record.trigger],
        status=record.status,
        size=format_size(record.size_bytes) if record.size_bytes is not None else None,
        error=record.error,
        has_file=record.filename in files,
    )


async def load_backup_list(db: AsyncSession, backup_dir: Path) -> BackupList:
    """The newest backups with whether their files exist now."""
    records, total = await list_backups(db, LIST_LIMIT)
    files = list_backup_files(backup_dir)
    return BackupList([_row(record, files) for record in records], total)


def extra_destination_problem(settings: BackupSettings, allowed: AllowedPaths) -> str | None:
    """Why the stored extra destination cannot be used now, None when it can or is not set."""
    if not settings.extra_destination:
        return None
    try:
        check_extra_destination(allowed, settings.extra_destination)
    except ExtraDestinationError as error:
        if str(error) == OUTSIDE_MESSAGE:
            return "The saved folder is no longer inside an allowed path."
        return f"The saved folder cannot be used: {str(error)[0].lower()}{str(error)[1:]}"
    return None
