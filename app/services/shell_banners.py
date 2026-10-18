"""The banners of the application shell: problems only the superadmin needs to act on.

Each banner is derived from stored state when a page is requested, so it stays until the problem
is gone and no request has to clear it. Only pages are given banners, and only for the
superadmin. The backup banner follows the newest backup that a person or the schedule asked for:
it shows while that backup failed or could not be copied to the extra destination, and goes
when a later one succeeds.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.display_time import DisplayTime
from app.models import Backup, BackupStatus, BackupTrigger

REQUESTED_TRIGGERS = (BackupTrigger.MANUAL, BackupTrigger.SCHEDULED, BackupTrigger.CLI)
BACKUP_SETTINGS_HREF = "/settings/backup"


@dataclass(frozen=True)
class ShellBanner:
    """One banner: its message and the single link it carries."""

    message: str
    action_label: str
    action_href: str


async def backup_banner(db: AsyncSession, display: DisplayTime) -> ShellBanner | None:
    """The banner for a failed or incomplete newest backup, None when it is fine."""
    newest = (
        await db.execute(
            select(Backup)
            .where(Backup.trigger.in_(REQUESTED_TRIGGERS))
            .order_by(Backup.created_at.desc(), Backup.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if newest is None or newest.status is BackupStatus.SUCCESS:
        return None
    when = display.text(newest.created_at)
    if newest.status is BackupStatus.PARTIAL:
        message = (
            f"Backup incomplete: the last backup ({when}) could not be copied to the "
            "extra destination."
        )
    else:
        kind = "scheduled backup" if newest.trigger is BackupTrigger.SCHEDULED else "backup"
        message = f"Backup failed: last {kind} ({when}) did not complete."
    return ShellBanner(message, "Open backup settings", BACKUP_SETTINGS_HREF)


async def load_shell_banners(db: AsyncSession, display: DisplayTime) -> list[ShellBanner]:
    """Every banner the superadmin should see now, in the order they are shown."""
    banners: list[ShellBanner] = []
    backup = await backup_banner(db, display)
    if backup is not None:
        banners.append(backup)
    return banners
