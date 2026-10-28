"""The scheduler: triggers, rescheduling from the stored settings, the jobs themselves."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.db import create_session_factory
from app.core.display_time import DisplayTime
from app.models import AuditActorType, BackupStatus, BackupTrigger, PendingLogin, SsoLogin
from app.seeds import SeedData
from app.services.app_settings import (
    BackupSettings,
    GeneralSettings,
    load_stored_settings,
    save_section,
)
from app.services.audit import AuditActor
from app.services.backup_lock import hold_backup_lock
from app.services.backup_schedule import (
    AUDIT_PURGE_JOB,
    BACKUP_AGE_JOB,
    BACKUP_JOB,
    GIT_JOB,
    PENDING_LOGINS_JOB,
    BackupScheduler,
    backup_trigger,
)
from app.tests.audit_helpers import audit_entries_from
from app.tests.backup_helpers import (
    RecordingBackupNotifier,
    backup_records,
    locations_in,
    save_backup_settings,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


HOUSEKEEPING_JOBS = (PENDING_LOGINS_JOB, BACKUP_AGE_JOB, AUDIT_PURGE_JOB)


def fire_times(schedule: str, zone: str, count: int) -> list[datetime]:
    """The next ``count`` fire times of a schedule from NOW, in the zone."""
    trigger = backup_trigger(schedule, DisplayTime(zone))
    assert trigger is not None
    times: list[datetime] = []
    previous = None
    moment = NOW
    for _ in range(count):
        found = trigger.get_next_fire_time(previous, moment)
        assert found is not None
        times.append(found)
        previous, moment = found, found
    return times


def test_the_daily_schedule_runs_at_two_in_the_chosen_zone() -> None:
    """02:00 means 02:00 where the screens show their times."""
    lisbon = fire_times("daily", "Europe/Lisbon", 2)
    assert [(time.hour, time.minute) for time in lisbon] == [(2, 0), (2, 0)]
    assert lisbon[1] - lisbon[0] == timedelta(days=1)
    tokyo = fire_times("daily", "Asia/Tokyo", 1)[0]
    assert (tokyo.hour, tokyo.utcoffset()) == (2, timedelta(hours=9))


def test_every_six_hours_is_aligned_to_the_day() -> None:
    """Runs at 00:00, 06:00, 12:00 and 18:00 of the zone."""
    times = fire_times("six_hours", "UTC", 5)
    assert [time.hour for time in times] == [12, 18, 0, 6, 12]


def test_weekly_runs_on_sunday_at_two() -> None:
    """The weekly schedule is Sunday 02:00."""
    first, second = fire_times("weekly", "UTC", 2)
    assert (first.weekday(), first.hour, first.minute) == (6, 2, 0)
    assert second - first == timedelta(days=7)


def test_off_has_no_trigger() -> None:
    """Off and anything unknown schedule nothing."""
    assert backup_trigger("off", DisplayTime()) is None
    assert backup_trigger("every-second", DisplayTime()) is None


@pytest.fixture
def factory(seeded_database: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions on the seeded database."""
    return create_session_factory(seeded_database)


@pytest.fixture
def notifier() -> RecordingBackupNotifier:
    """The notifier the scheduler under test reports to."""
    return RecordingBackupNotifier()


@pytest.fixture
async def scheduler(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    database_path: Path,
    notifier: RecordingBackupNotifier,
) -> AsyncIterator[BackupScheduler]:
    """A started, paused scheduler on the seeded database that reports to the notifier."""
    started = BackupScheduler(factory, locations_in(tmp_path, database_path), lambda: notifier)
    started.start()
    started.pause()
    yield started
    started.shutdown()


async def test_the_stored_schedule_is_applied_and_replaced(
    scheduler: BackupScheduler,
    factory: async_sessionmaker[AsyncSession],
    notifier: RecordingBackupNotifier,
) -> None:
    """Reloading follows the stored choice: daily, weekly, every six hours, then off."""
    await scheduler.reload()
    daily = scheduler.next_backup()
    assert daily is not None and (daily.hour, daily.minute) == (2, 0)
    assert scheduler.job_ids() == sorted([BACKUP_JOB, *HOUSEKEEPING_JOBS])

    await save_backup_settings(factory, schedule="weekly")
    await scheduler.reload()
    weekly = scheduler.next_backup()
    assert weekly is not None and weekly.weekday() == 6
    assert scheduler.job_ids() == sorted([BACKUP_JOB, *HOUSEKEEPING_JOBS])

    await save_backup_settings(factory, schedule="six_hours")
    await scheduler.reload()
    six = scheduler.next_backup()
    assert six is not None and six.hour in {0, 6, 12, 18}

    await save_backup_settings(factory, schedule="off")
    await scheduler.reload()
    assert scheduler.next_backup() is None
    assert scheduler.job_ids() == sorted(HOUSEKEEPING_JOBS)

    await save_backup_settings(factory, schedule="daily")
    await scheduler.reload()
    assert scheduler.next_backup() is not None


async def test_the_display_time_zone_moves_the_schedule(
    scheduler: BackupScheduler,
    factory: async_sessionmaker[AsyncSession],
    notifier: RecordingBackupNotifier,
) -> None:
    """Changing the zone in General settings changes the hour the backup runs in UTC."""
    await scheduler.reload()
    utc_run = scheduler.next_backup()
    async with factory() as db:
        await save_section(
            db, "general", GeneralSettings(time_zone="Asia/Tokyo"), actor=AuditActor.cli()
        )
        await db.commit()
    await scheduler.reload()
    tokyo_run = scheduler.next_backup()
    assert utc_run is not None and tokyo_run is not None
    assert tokyo_run.utcoffset() == timedelta(hours=9)
    assert tokyo_run.astimezone(UTC).hour == 17


async def test_the_job_takes_a_scheduled_backup_as_the_scheduler(
    scheduler: BackupScheduler,
    factory: async_sessionmaker[AsyncSession],
    notifier: RecordingBackupNotifier,
) -> None:
    """The job records a scheduled backup audited as the scheduler and announces it."""
    await scheduler.run_scheduled_backup()
    (record,) = await backup_records(factory)
    assert (record.trigger, record.status) == (BackupTrigger.SCHEDULED, BackupStatus.SUCCESS)
    (entry,) = await audit_entries_from(factory, "backup.create")
    assert (entry.actor_type, entry.actor_name) == (AuditActorType.SYSTEM, "scheduler")
    events = notifier.events
    assert [event.trigger for event in events] == [BackupTrigger.SCHEDULED]


async def test_the_job_skips_its_turn_while_another_backup_runs(
    scheduler: BackupScheduler,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A second worker or a manual backup holding the lock makes the job skip without failing."""
    with hold_backup_lock(tmp_path / "locks"):
        await scheduler.run_scheduled_backup()
    assert await backup_records(factory) == []
    assert await audit_entries_from(factory, "backup.create") == []


async def test_the_job_never_raises_when_the_backup_cannot_be_taken(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, database_path: Path
) -> None:
    """A broken folder is a recorded failure and the job returns normally."""
    (tmp_path / "backups").write_text("a file, not a folder")
    notifier = RecordingBackupNotifier()
    broken = BackupScheduler(factory, locations_in(tmp_path, database_path), lambda: notifier)
    await broken.run_scheduled_backup()
    (record,) = await backup_records(factory)
    assert record.status is BackupStatus.FAILURE


async def test_the_housekeeping_job_removes_expired_pending_sign_ins(
    scheduler: BackupScheduler,
    factory: async_sessionmaker[AsyncSession],
    seed_data: SeedData,
) -> None:
    """Expired pending logins go, a live one stays."""
    user_id = seed_data.users.totp_user.id
    now = datetime.now(UTC)
    async with factory() as db:
        for index, expires in enumerate((now - timedelta(hours=2), now + timedelta(minutes=3))):
            db.add(
                PendingLogin(
                    token_hash=f"{index:064d}",
                    user_id=user_id,
                    created_at=now - timedelta(hours=3),
                    expires_at=expires,
                )
            )
        await db.commit()
    await scheduler.purge_pending_logins()
    async with factory() as db:
        remaining = (await db.execute(select(func.count()).select_from(PendingLogin))).scalar_one()
    assert remaining == 1


async def test_the_application_starts_the_scheduler_with_the_stored_schedule(
    web_app: FastAPI,
) -> None:
    """The lifespan registers the backup job from the settings, the hourly git push and the
    housekeeping job."""
    scheduler: BackupScheduler = web_app.state.backup_scheduler
    assert scheduler.job_ids() == sorted([BACKUP_JOB, GIT_JOB, *HOUSEKEEPING_JOBS])
    run = scheduler.next_backup()
    assert run is not None and run.hour == 2


async def test_the_defaults_are_daily_at_two_and_keep_fourteen(engine: AsyncEngine) -> None:
    """A database with no stored backup settings behaves as daily at 02:00, keeping 14 backups."""
    async with create_session_factory(engine)() as db:
        stored = (await load_stored_settings(db)).backup
    assert stored == BackupSettings(
        schedule="daily", retention_count=14, retention_days=0, extra_destination=""
    )


async def test_the_housekeeping_job_removes_expired_single_sign_on_attempts(
    scheduler: BackupScheduler, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Expired attempts go, a live one stays."""
    now = datetime.now(UTC)
    async with factory() as db:
        for index, expires in enumerate((now - timedelta(minutes=1), now + timedelta(minutes=3))):
            db.add(
                SsoLogin(
                    state_hash=f"{index:064d}",
                    binding_hash="b",
                    nonce="n",
                    code_verifier_encrypted="v",
                    created_at=now - timedelta(minutes=6),
                    expires_at=expires,
                )
            )
        await db.commit()
    await scheduler.purge_pending_logins()
    async with factory() as db:
        remaining = (await db.execute(select(SsoLogin.state_hash))).scalars().all()
    assert remaining == [f"{1:064d}"]
