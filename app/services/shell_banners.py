"""The banners of the application shell: problems only the superadmin needs to act on.

Each banner is derived from stored state when a page is requested, so it stays until the problem
is gone and no request has to clear it. Only pages are given banners, and only for the
superadmin. The backup banner follows the newest backup that a person or the schedule asked for:
it shows while that backup failed or could not be copied to the extra destination, and goes
when a later one succeeds. The SMTP banner shows while mail is not set up, and the delivery banner
while notification mail has failed since one last worked.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.display_time import DisplayTime
from app.models import Backup, BackupStatus, BackupTrigger
from app.services.app_settings import SmtpSettings, load_stored_settings
from app.services.notify_status import EMAIL_CHANNEL, load_failures

REQUESTED_TRIGGERS = (BackupTrigger.MANUAL, BackupTrigger.SCHEDULED, BackupTrigger.CLI)
BACKUP_SETTINGS_HREF = "/settings/backup"
SMTP_SETTINGS_HREF = "/settings/smtp"


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


def smtp_banner(smtp: SmtpSettings) -> ShellBanner | None:
    """The banner for mail that is not set up, None when it is."""
    if smtp.configured:
        return None
    return ShellBanner(
        "SMTP is not configured: no notification emails are sent.",
        "Open SMTP settings",
        SMTP_SETTINGS_HREF,
    )


async def delivery_banner(db: AsyncSession, display: DisplayTime) -> ShellBanner | None:
    """The banner for notification mail that failed since one last worked, None when none did."""
    failing = [row for row in await load_failures(db) if row.channel == EMAIL_CHANNEL]
    if not failing:
        return None
    row = failing[0]
    noun = "mail" if row.failures == 1 else "mails"
    return ShellBanner(
        f"Notification emails are failing: {row.failures} {noun} could not be sent, "
        f"the last at {display.text(row.last_failure_at)}. {row.reason}",
        "Open SMTP settings",
        SMTP_SETTINGS_HREF,
    )


async def load_shell_banners(
    db: AsyncSession, display: DisplayTime, smtp: SmtpSettings | None = None
) -> list[ShellBanner]:
    """Every banner the superadmin should see now, in the order they are shown.

    ``smtp`` is the stored SMTP section when the caller has it; otherwise it is read here.
    """
    if smtp is None:
        smtp = (await load_stored_settings(db)).smtp
    banners: list[ShellBanner] = []
    for banner in (
        await backup_banner(db, display),
        smtp_banner(smtp),
        await delivery_banner(db, display),
    ):
        if banner is not None:
            banners.append(banner)
    return banners
