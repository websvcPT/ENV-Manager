"""The in-process scheduler: the scheduled backup and the housekeeping of pending sign-ins.

APScheduler runs with its default in-memory job store. Nothing about the jobs is persisted by
APScheduler: the schedule lives in our own settings table and is read again whenever it can have
changed (at startup, after the Backup or General settings are saved, after a restore), which
replaces the job. The application runs as one worker process; a second worker would start a
second scheduler and fire every job twice, which the backup lock turns into a skipped run.

A run that was due while the application was down is not made up: the next run is the next
matching time after the start. A command line restore cannot reach the scheduler of a running
server, so a restored schedule takes effect at the next start or the next save of the settings.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.display_time import DisplayTime
from app.core.log import get_logger
from app.models import BackupTrigger
from app.services.app_settings import BackupSettings, load_stored_settings
from app.services.audit import AuditActor
from app.services.backup_events import BackupNotifier
from app.services.backup_lock import BackupBusyError
from app.services.backup_run import BackupLocations, run_backup
from app.services.pending_logins import purge_expired_pending_logins

log = get_logger("scheduler")

BACKUP_JOB = "backup"
PENDING_LOGINS_JOB = "pending-logins"
SCHEDULER_ACTOR = "scheduler"
MISFIRE_GRACE_SECONDS = 3600
HOUSEKEEPING_HOURS = 1


def backup_trigger(schedule: str, display: DisplayTime) -> CronTrigger | None:
    """The cron trigger of a schedule choice in the display time zone, None for off."""
    zone = display.zone
    if schedule == "daily":
        return CronTrigger(hour=2, minute=0, timezone=zone)
    if schedule == "six_hours":
        return CronTrigger(hour="0,6,12,18", minute=0, timezone=zone)
    if schedule == "weekly":
        return CronTrigger(day_of_week="sun", hour=2, minute=0, timezone=zone)
    return None


class BackupScheduler:
    """The scheduler with its jobs; ``reload`` makes the backup job match the stored settings."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        locations: BackupLocations,
        notifier: Callable[[], BackupNotifier],
    ) -> None:
        """Hold what the jobs need. ``notifier`` is read each time a job runs, so the
        application can swap its notifier after startup."""
        self._session_factory = session_factory
        self._locations = locations
        self._notifier = notifier
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    def start(self) -> None:
        """Start the scheduler on the running event loop and add the housekeeping job."""
        self._scheduler.start()
        self._scheduler.add_job(
            self.purge_pending_logins,
            IntervalTrigger(hours=HOUSEKEEPING_HOURS),
            id=PENDING_LOGINS_JOB,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    def shutdown(self) -> None:
        """Stop the scheduler without waiting for a running job."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def pause(self) -> None:
        """Stop jobs from firing while keeping them registered (used by tests)."""
        self._scheduler.pause()

    def apply(self, settings: BackupSettings, display: DisplayTime) -> None:
        """Replace the backup job to match the settings, or remove it when the schedule is off."""
        trigger = backup_trigger(settings.schedule, display)
        if trigger is None:
            if self._scheduler.get_job(BACKUP_JOB) is not None:
                self._scheduler.remove_job(BACKUP_JOB)
            return
        self._scheduler.add_job(
            self.run_scheduled_backup,
            trigger,
            id=BACKUP_JOB,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )

    async def reload(self) -> None:
        """Read the stored settings and apply them."""
        async with self._session_factory() as db:
            stored = await load_stored_settings(db)
        display = DisplayTime(stored.general.time_zone, stored.general.date_format)
        self.apply(stored.backup, display)

    def job_ids(self) -> list[str]:
        """The ids of the registered jobs, sorted."""
        return sorted(str(job.id) for job in self._scheduler.get_jobs())

    def next_backup(self) -> datetime | None:
        """When the scheduled backup runs next, None when there is no backup job."""
        job = self._scheduler.get_job(BACKUP_JOB)
        if job is None:
            return None
        moment: datetime | None = job.next_run_time
        return moment

    async def run_scheduled_backup(self) -> None:
        """The scheduled backup job: a recorded backup by the scheduler, never an exception."""
        try:
            async with self._session_factory() as db:
                await run_backup(
                    db,
                    self._locations,
                    trigger=BackupTrigger.SCHEDULED,
                    actor=AuditActor.system(SCHEDULER_ACTOR),
                    notifier=self._notifier(),
                )
        except BackupBusyError:
            log.info("the scheduled backup was skipped, another backup or restore is running")
        except Exception as error:
            log.error("the scheduled backup failed: %s", type(error).__name__)

    async def purge_pending_logins(self) -> None:
        """Remove pending sign-ins that have expired."""
        try:
            async with self._session_factory() as db:
                await purge_expired_pending_logins(db)
                await db.commit()
        except Exception as error:
            log.error("removing expired sign-in steps failed: %s", type(error).__name__)
