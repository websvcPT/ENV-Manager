"""The shell banners: the failed backup banner follows the newest backup that was asked for."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.core.display_time import DisplayTime
from app.models import Backup, BackupStatus, BackupTrigger
from app.services.backup import backup_file_name
from app.services.shell_banners import ShellBanner, load_shell_banners

NOW = datetime(2026, 10, 17, 2, 0, tzinfo=UTC)


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
        return await load_shell_banners(db, display or DisplayTime())


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
