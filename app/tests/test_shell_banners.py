"""The shell banners: the failed backup banner follows the newest backup that was asked for, the
SMTP banner shows while mail is not set up and the delivery banner while mail keeps failing."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.core.display_time import DisplayTime
from app.models import Backup, BackupStatus, BackupTrigger
from app.services.app_settings import SmtpSettings
from app.services.backup import backup_file_name
from app.services.notify_status import EMAIL_CHANNEL, clear_failures, record_failure
from app.services.shell_banners import ShellBanner, load_shell_banners

NOW = datetime(2026, 10, 17, 2, 0, tzinfo=UTC)
CONFIGURED = SmtpSettings("smtp.test", 25, "none", "", "env-manager@example.com")


def record(trigger: BackupTrigger, status: BackupStatus, minutes: int) -> Backup:
    """A backup record made ``minutes`` after NOW."""
    moment = NOW + timedelta(minutes=minutes)
    return Backup(
        filename=backup_file_name(trigger.value, moment),
        trigger=trigger,
        status=status,
        size_bytes=None if status is BackupStatus.FAILURE else 10,
        created_at=moment,
    )


async def banners_after(
    engine: AsyncEngine, *records: Backup, display: DisplayTime | None = None
) -> list[ShellBanner]:
    """The banners the superadmin gets once the records exist."""
    async with create_session_factory(engine)() as db:
        db.add_all(records)
        await db.commit()
        return await load_shell_banners(db, display or DisplayTime(), CONFIGURED)


async def test_no_backup_and_a_good_backup_raise_no_banner(engine: AsyncEngine) -> None:
    """Nothing to say before the first backup and after a successful one."""
    assert await banners_after(engine) == []
    assert await banners_after(engine, record(BackupTrigger.MANUAL, BackupStatus.SUCCESS, 0)) == []


async def test_a_failed_scheduled_backup_is_named_with_its_time(engine: AsyncEngine) -> None:
    """The text says last scheduled backup, in the display zone, with the settings link."""
    banners = await banners_after(
        engine,
        record(BackupTrigger.SCHEDULED, BackupStatus.FAILURE, 0),
        display=DisplayTime("Asia/Tokyo", "iso"),
    )
    (banner,) = banners
    assert banner.message == (
        "Backup failed: last scheduled backup (2026-10-17 11:00) did not complete."
    )
    assert (banner.action_label, banner.action_href) == ("Open backup settings", "/settings/backup")


async def test_a_failed_manual_or_command_line_backup_says_last_backup(engine: AsyncEngine) -> None:
    """Only a scheduled failure is called scheduled."""
    for trigger in (BackupTrigger.MANUAL, BackupTrigger.CLI):
        (banner,) = await banners_after(engine, record(trigger, BackupStatus.FAILURE, 0))
        assert banner.message == "Backup failed: last backup (2026-10-17 02:00) did not complete."
        async with create_session_factory(engine)() as db:
            await db.execute(delete(Backup))
            await db.commit()


async def test_a_backup_not_copied_has_its_own_message(engine: AsyncEngine) -> None:
    """A snapshot that could not be copied to the extra destination is incomplete, not failed."""
    (banner,) = await banners_after(engine, record(BackupTrigger.MANUAL, BackupStatus.PARTIAL, 0))
    assert banner.message == (
        "Backup incomplete: the last backup (2026-10-17 02:00) could not be copied to the "
        "extra destination."
    )


@pytest.mark.parametrize("safety", [BackupTrigger.RESTORE, BackupTrigger.MIGRATION])
async def test_a_later_safety_backup_does_not_hide_a_failure(
    engine: AsyncEngine, safety: BackupTrigger
) -> None:
    """The backup before a migration or a restore is not one that was asked for."""
    banners = await banners_after(
        engine,
        record(BackupTrigger.SCHEDULED, BackupStatus.FAILURE, 0),
        record(safety, BackupStatus.SUCCESS, 5),
    )
    assert len(banners) == 1


async def test_a_later_success_clears_the_banner(engine: AsyncEngine) -> None:
    """The newest requested backup decides, so one good run ends the warning."""
    banners = await banners_after(
        engine,
        record(BackupTrigger.SCHEDULED, BackupStatus.FAILURE, 0),
        record(BackupTrigger.MANUAL, BackupStatus.SUCCESS, 5),
    )
    assert banners == []


async def test_mail_that_is_not_set_up_raises_the_smtp_banner(engine: AsyncEngine) -> None:
    """An empty server or from address means no mail, which the superadmin is told with a link."""
    async with create_session_factory(engine)() as db:
        for smtp in (
            SmtpSettings(),
            SmtpSettings(host="smtp.test"),
            SmtpSettings(from_address="a@b.c"),
        ):
            (banner,) = await load_shell_banners(db, DisplayTime(), smtp)
            assert banner.message == "SMTP is not configured: no notification emails are sent."
            assert (banner.action_label, banner.action_href) == (
                "Open SMTP settings",
                "/settings/smtp",
            )
        assert await load_shell_banners(db, DisplayTime(), CONFIGURED) == []


async def test_the_smtp_banner_is_read_from_the_stored_settings_when_none_is_given(
    engine: AsyncEngine,
) -> None:
    """A caller without the settings at hand still gets the banner for a database with none."""
    async with create_session_factory(engine)() as db:
        (banner,) = await load_shell_banners(db, DisplayTime())
        assert banner.action_href == "/settings/smtp"


async def test_failing_mail_is_counted_with_its_last_time_and_reason_until_one_works(
    engine: AsyncEngine,
) -> None:
    """The delivery banner counts the failures since the last mail that worked."""
    async with create_session_factory(engine)() as db:
        await record_failure(db, EMAIL_CHANNEL, "The mail server did not answer in time.", NOW)
        await record_failure(
            db, EMAIL_CHANNEL, "The mail server closed the connection.", NOW + timedelta(hours=1)
        )
        await db.commit()
        (banner,) = await load_shell_banners(db, DisplayTime(), CONFIGURED)
        assert banner.message == (
            "Notification emails are failing: 2 mails could not be sent, the last at "
            "2026-10-17 03:00. The mail server closed the connection."
        )
        assert banner.action_href == "/settings/smtp"
        await clear_failures(db, EMAIL_CHANNEL)
        await db.commit()
        assert await load_shell_banners(db, DisplayTime(), CONFIGURED) == []


async def test_banners_come_in_a_fixed_order_backup_smtp_delivery(engine: AsyncEngine) -> None:
    """With every problem at once the order is the backup, then SMTP, then delivery."""
    async with create_session_factory(engine)() as db:
        db.add(record(BackupTrigger.MANUAL, BackupStatus.FAILURE, 0))
        await record_failure(db, EMAIL_CHANNEL, "The mail could not be sent.", NOW)
        await db.commit()
        banners = await load_shell_banners(db, DisplayTime(), SmtpSettings())
    assert [banner.action_href for banner in banners] == [
        "/settings/backup",
        "/settings/smtp",
        "/settings/smtp",
    ]
    assert banners[2].message.startswith("Notification emails are failing: 1 mail could not")
