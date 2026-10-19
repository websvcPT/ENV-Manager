"""Application metrics.

The instruments come from one meter that binds to whatever meter provider the process has. While
OpenTelemetry export is not configured that provider is the API's no-op proxy, so recording is a
call that does nothing. Labels are fixed words only (a result, a status, a trigger, a scope):
never a name, an address, a path or any value. The backup age gauge reads a timestamp held in
this module, which the lifespan seeds from the database, every backup updates and the hourly
scheduler job reads again (so a backup taken by the command line shows up), so a scrape never
queries the database.
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from app import VERSION

METER_NAME = "env-manager"

CHANNEL_SIGN_IN = "sign_in"
CHANNEL_API = "api"

_meter = metrics.get_meter(METER_NAME, VERSION)

_deploys = _meter.create_counter(
    "envmanager.deploys", unit="{deploy}", description="Real deploys by result."
)
_deploy_duration = _meter.create_histogram(
    "envmanager.deploy.duration", unit="s", description="Duration of real deploys by result."
)
_post_deploys = _meter.create_counter(
    "envmanager.post_deploy.runs",
    unit="{run}",
    description="Post-deploy command runs by result.",
)
_post_deploy_duration = _meter.create_histogram(
    "envmanager.post_deploy.duration",
    unit="s",
    description="Duration of post-deploy command runs by result.",
)
_drift = _meter.create_counter(
    "envmanager.drift.detections",
    unit="{detection}",
    description="Targets found changed outside the app, by where they were found.",
)
_dist_failures = _meter.create_counter(
    "envmanager.dist_check.failures",
    unit="{check}",
    description="Dist checks that could not run, by problem.",
)
_login_failures = _meter.create_counter(
    "envmanager.login.failures",
    unit="{failure}",
    description="Failed credential checks, by channel.",
)
_login_lockouts = _meter.create_counter(
    "envmanager.login.lockouts",
    unit="{lockout}",
    description="Lockouts started, by counter kind.",
)
_backups = _meter.create_counter(
    "envmanager.backup.runs",
    unit="{backup}",
    description="Backups by result and trigger.",
)
_git_push_failures = _meter.create_counter(
    "envmanager.git_push.failures",
    unit="{push}",
    description="Git pushes that failed after their retries, by problem.",
)


def record_deploy(result: str, seconds: float) -> None:
    """Count one real deploy and its duration."""
    attributes = {"result": result}
    _deploys.add(1, attributes)
    _deploy_duration.record(seconds, attributes)


def record_post_deploy(result: str, seconds: float) -> None:
    """Count one post-deploy command run and its duration."""
    attributes = {"result": result}
    _post_deploys.add(1, attributes)
    _post_deploy_duration.record(seconds, attributes)


def record_drift_detection(source: str) -> None:
    """Count one target found changed outside the app."""
    _drift.add(1, {"source": source})


def record_dist_check_failure(problem: str) -> None:
    """Count one dist check that could not run."""
    _dist_failures.add(1, {"problem": problem})


def record_login_failure(channel: str) -> None:
    """Count one failed credential check."""
    _login_failures.add(1, {"channel": channel})


def record_login_lockout(kind: str) -> None:
    """Count one lockout started."""
    _login_lockouts.add(1, {"kind": kind})


def record_backup(result: str, trigger: str) -> None:
    """Count one backup by its result."""
    _backups.add(1, {"result": result, "trigger": trigger})


def record_git_push_failure(problem: str) -> None:
    """Count one git push that failed after its retries."""
    _git_push_failures.add(1, {"problem": problem})


class _BackupClock:
    """The time of the newest backup that produced a snapshot, kept in memory."""

    def __init__(self) -> None:
        """Start with no known backup."""
        self.newest: datetime | None = None

    def note(self, moment: datetime | None) -> None:
        """Remember a backup time when it is newer than the one held."""
        if moment is not None and (self.newest is None or moment > self.newest):
            self.newest = moment

    def reset(self, moment: datetime | None = None) -> None:
        """Replace the held time, for a fresh read of the database."""
        self.newest = moment

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the newest backup, None when none is known."""
        if self.newest is None:
            return None
        moment = now or datetime.now(UTC)
        return max((moment - self.newest).total_seconds(), 0.0)


backup_clock = _BackupClock()


def _observe_backup_age(_options: CallbackOptions) -> Iterable[Observation]:
    """The gauge callback: one reading from the held time, nothing when no backup is known."""
    age = backup_clock.age_seconds()
    return [] if age is None else [Observation(age)]


_meter.create_observable_gauge(
    "envmanager.backup.age",
    callbacks=[_observe_backup_age],
    unit="s",
    description="Seconds since the newest backup that produced a snapshot.",
)
