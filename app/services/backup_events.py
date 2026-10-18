"""The backup-finished notification seam.

Every backup, whoever starts it and however it ends, is announced after it is recorded and
committed. Whatever delivers notifications implements ``BackupNotifier``; the backup service
calls it and ignores a notifier that fails. The event carries the outcome, the size and a fixed
reason, never a path or a secret.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.log import get_logger
from app.models import BackupStatus, BackupTrigger

log = get_logger("backup_events")


@dataclass(frozen=True)
class BackupFinished:
    """The result of one backup.

    ``status`` is success, failure (no snapshot was written) or partial (the snapshot exists but
    the copy to the extra destination failed). ``error`` is the fixed reason for the last two.
    ``actor`` is the label of who started it (a user name, ``cli`` or ``scheduler``).
    """

    status: BackupStatus
    trigger: BackupTrigger
    filename: str
    size_bytes: int | None
    error: str | None
    finished_at: datetime
    actor: str


class BackupNotifier(Protocol):
    """Receives backup-finished events."""

    async def backup_finished(self, event: BackupFinished) -> None:
        """Handle the event; the caller waits for nothing but this call."""
        ...


class NoBackupNotifier:
    """The notifier used until notifications exist: it does nothing."""

    async def backup_finished(self, event: BackupFinished) -> None:
        """Ignore the event."""


async def announce_backup(notifier: BackupNotifier, event: BackupFinished) -> None:
    """Deliver an event; a notifier that fails is logged and never fails the backup."""
    try:
        await notifier.backup_finished(event)
    except Exception as error:
        log.error("backup notification failed: %s", type(error).__name__)
