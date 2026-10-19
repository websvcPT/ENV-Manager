"""The spans and metrics of backups, git pushes, mail, webhook deliveries and sign-in.

They are read from in-memory providers, so the tests see what an exporter would be given.
"""

from datetime import timedelta
from itertools import count
from pathlib import Path

import pytest
from opentelemetry.trace import StatusCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core import metrics, telemetry
from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import Backup, BackupStatus, BackupTrigger, DeployStatus
from app.models.types import utc_now
from app.services.audit import AuditActor
from app.services.auth_policy import AuthPolicy
from app.services.backup_lock import BackupBusyError, hold_backup_lock
from app.services.backup_records import latest_snapshot_time
from app.services.backup_run import BackupLocations, run_backup
from app.services.backup_schedule import BackupScheduler
from app.services.git_errors import GitProblem
from app.services.git_process import GitOutput
from app.services.git_push import PushStatus, SkipReason
from app.services.lockout import register_attempt_failure
from app.services.login import authenticate
from app.services.mailer import FastapiMailTransport, MailFailure, OutgoingMail
from app.services.webhook_delivery import WebhookDelivery, WebhookRetry
from app.tests.auth_helpers import PASSWORD, make_user
from app.tests.backup_helpers import RecordingBackupNotifier, locations_in
from app.tests.deploy_helpers import Harness
from app.tests.git_helpers import Call, failed
from app.tests.git_world import World as GitWorld
from app.tests.notify_helpers import SMTP, FakeSMTP, install_fake_smtp
from app.tests.telemetry_helpers import Telemetry
from app.tests.webhook_helpers import (
    WEBHOOK_URL,
    RecordingWebhook,
    configure_webhook,
    permanent_failure,
    temporary_failure,
)

MAIL = OutgoingMail("alice@example.com", "[ENV Manager] Test", "Plain body", "<p>Html body</p>")
POLICY = AuthPolicy(user_max_failures=3, ip_max_failures=5, lockout_duration=timedelta(minutes=10))


@pytest.fixture
def factory(seeded_database: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions on the seeded database."""
    return create_session_factory(seeded_database)


@pytest.fixture
def locations(tmp_path: Path, database_path: Path) -> BackupLocations:
    """Where the seeded database, the backups and the locks are."""
    return locations_in(tmp_path, database_path)


@pytest.fixture
def clean_backup_clock(monkeypatch: pytest.MonkeyPatch) -> metrics._BackupClock:
    """A backup clock that knows no backup, in place of the process wide one."""
    clock = metrics._BackupClock()
    monkeypatch.setattr(metrics, "backup_clock", clock)
    return clock


async def backup(
    factory: async_sessionmaker[AsyncSession],
    locations: BackupLocations,
    trigger: BackupTrigger = BackupTrigger.MANUAL,
) -> BackupStatus:
    """Take a backup in its own session and return how it ended."""
    async with factory() as db:
        run = await run_backup(
            db,
            locations,
            trigger=trigger,
            actor=AuditActor.cli(),
            notifier=RecordingBackupNotifier(),
        )
        return run.record.status


async def test_a_backup_is_a_span_is_counted_and_moves_the_age_clock(
    factory: async_sessionmaker[AsyncSession],
    locations: BackupLocations,
    telemetry_recorder: Telemetry,
    clean_backup_clock: metrics._BackupClock,
) -> None:
    assert await backup(factory, locations) is BackupStatus.SUCCESS

    span = telemetry_recorder.span(telemetry.BACKUP_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes[telemetry.TRIGGER] == "manual"
    assert attributes[telemetry.ACTOR_TYPE] == "cli"
    assert attributes[telemetry.RESULT] == "success"
    assert isinstance(attributes[telemetry.BACKUP_ID], int)
    assert telemetry_recorder.counted("envmanager.backup.runs") == {
        (("result", "success"), ("trigger", "manual")): 1
    }
    assert clean_backup_clock.newest is not None
    age = clean_backup_clock.age_seconds()
    assert age is not None and 0 <= age < 60


async def test_the_age_gauge_has_no_reading_until_a_backup_is_known(
    telemetry_recorder: Telemetry, clean_backup_clock: metrics._BackupClock
) -> None:
    assert telemetry_recorder.points("envmanager.backup.age") == []


async def test_the_age_gauge_reads_the_held_time_and_no_database(
    telemetry_recorder: Telemetry, clean_backup_clock: metrics._BackupClock
) -> None:
    """No engine or session exists in this test, so the reading can only come from the clock."""
    clean_backup_clock.reset(utc_now() - timedelta(hours=5))

    (point,) = telemetry_recorder.points("envmanager.backup.age")
    assert 5 * 3600 <= point.value < 5 * 3600 + 60
    assert point.attributes == {}


async def test_the_hourly_job_brings_a_command_line_backup_into_the_age_gauge(
    factory: async_sessionmaker[AsyncSession],
    locations: BackupLocations,
    telemetry_recorder: Telemetry,
    clean_backup_clock: metrics._BackupClock,
) -> None:
    """A backup the running process did not take only reaches the gauge through the database."""
    assert await backup(factory, locations, BackupTrigger.CLI) is BackupStatus.SUCCESS
    clean_backup_clock.reset()
    assert telemetry_recorder.points("envmanager.backup.age") == []

    scheduler = BackupScheduler(factory, locations, lambda: RecordingBackupNotifier())
    await scheduler.refresh_backup_age()

    (point,) = telemetry_recorder.points("envmanager.backup.age")
    assert 0 <= point.value < 60
    async with factory() as db:
        assert clean_backup_clock.newest == await latest_snapshot_time(db)


async def test_a_busy_backup_is_counted_as_busy_and_its_span_is_an_error(
    factory: async_sessionmaker[AsyncSession],
    locations: BackupLocations,
    telemetry_recorder: Telemetry,
    clean_backup_clock: metrics._BackupClock,
) -> None:
    with hold_backup_lock(locations.lock_dir), pytest.raises(BackupBusyError):
        await backup(factory, locations)

    span = telemetry_recorder.span(telemetry.BACKUP_SPAN)
    assert span.status.status_code is StatusCode.ERROR
    assert telemetry_recorder.counted("envmanager.backup.runs") == {
        (("result", "busy"), ("trigger", "manual")): 1
    }
    assert clean_backup_clock.newest is None


async def test_a_failed_backup_is_counted_and_does_not_move_the_age_clock(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    telemetry_recorder: Telemetry,
    clean_backup_clock: metrics._BackupClock,
) -> None:
    gone = locations_in(tmp_path, tmp_path / "missing" / "envmanager.db")
    assert await backup(factory, gone) is BackupStatus.FAILURE

    assert telemetry_recorder.counted("envmanager.backup.runs") == {
        (("result", "failure"), ("trigger", "manual")): 1
    }
    assert clean_backup_clock.newest is None


async def test_a_git_push_that_fails_is_a_span_and_a_counted_failure(
    git_world: GitWorld, telemetry_recorder: Telemetry
) -> None:
    def network_down(_call: Call) -> GitOutput:
        return failed("fatal: unable to access 'https://git.example.com/': Could not resolve host")

    git_world.runner.script["ls-remote"] = network_down
    await git_world.configure()
    result = await git_world.push()

    assert result.status is PushStatus.FAILED
    span = telemetry_recorder.span(telemetry.GIT_PUSH_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes[telemetry.RESULT] == "failed"
    assert attributes[telemetry.ATTEMPTS] == result.attempts
    assert attributes[telemetry.ACTOR_TYPE] == "system"
    assert set(attributes) == {telemetry.RESULT, telemetry.ATTEMPTS, telemetry.ACTOR_TYPE}
    assert telemetry_recorder.counted("envmanager.git_push.failures") == {
        (("problem", GitProblem.NETWORK.value),): 1
    }


async def test_a_git_push_that_does_nothing_says_why_and_counts_no_failure(
    git_world: GitWorld, telemetry_recorder: Telemetry
) -> None:
    result = await git_world.push()

    assert result.status is PushStatus.SKIPPED
    span = telemetry_recorder.span(telemetry.GIT_PUSH_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes[telemetry.RESULT] == "skipped"
    assert attributes[telemetry.SKIP_REASON] == SkipReason.NOT_CONFIGURED.value
    assert telemetry_recorder.counted("envmanager.git_push.failures") == {}


async def test_a_successful_git_push_is_a_pushed_span(
    git_world: GitWorld, telemetry_recorder: Telemetry
) -> None:
    await git_world.configure()
    result = await git_world.push()

    assert result.status is PushStatus.PUSHED
    span = telemetry_recorder.span(telemetry.GIT_PUSH_SPAN)
    assert (span.attributes or {})[telemetry.RESULT] == "pushed"
    assert telemetry_recorder.counted("envmanager.git_push.failures") == {}


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    """The fake SMTP client in place of aiosmtplib's, reset for the test."""
    client = install_fake_smtp(monkeypatch)
    FakeSMTP.instances = []
    FakeSMTP.connect_error = FakeSMTP.login_error = FakeSMTP.send_error = None
    return client


async def test_a_mail_that_is_sent_is_a_span_with_the_tls_mode_and_nothing_about_the_message(
    fake_smtp: type[FakeSMTP], telemetry_recorder: Telemetry
) -> None:
    await FastapiMailTransport().send(SMTP, "smtp-password-sentinel", MAIL)

    span = telemetry_recorder.span(telemetry.SMTP_SEND_SPAN)
    assert dict(span.attributes or {}) == {
        telemetry.SMTP_TLS_MODE: SMTP.tls_mode,
        telemetry.RESULT: "sent",
    }
    everything = telemetry_recorder.everything_recorded()
    for text in (SMTP.host, MAIL.to, MAIL.subject, "smtp-password-sentinel"):
        assert text not in everything


async def test_a_mail_that_fails_records_the_result_and_whether_trying_again_helps(
    fake_smtp: type[FakeSMTP], telemetry_recorder: Telemetry
) -> None:
    FakeSMTP.connect_error = OSError("connection refused by smtp.example.com")
    with pytest.raises(MailFailure):
        await FastapiMailTransport().send(SMTP, "smtp-password-sentinel", MAIL)

    span = telemetry_recorder.span(telemetry.SMTP_SEND_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes[telemetry.RESULT] == "failed" and attributes[telemetry.RETRYABLE] is True
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None and span.events == ()
    assert "refused" not in telemetry_recorder.everything_recorded()


async def test_a_mail_to_a_bad_address_fails_without_a_connection(
    fake_smtp: type[FakeSMTP], telemetry_recorder: Telemetry
) -> None:
    bad = OutgoingMail("not an address", "Subject", "Text", "<p>Text</p>")
    with pytest.raises(MailFailure):
        await FastapiMailTransport().send(SMTP, None, bad)

    attributes = dict(telemetry_recorder.span(telemetry.SMTP_SEND_SPAN).attributes or {})
    assert attributes[telemetry.RESULT] == "failed" and attributes[telemetry.RETRYABLE] is False
    assert FakeSMTP.instances == []


class Webhooks:
    """A delivery on the harness's database with a recording transport that never really waits."""

    def __init__(self, h: Harness, transport: RecordingWebhook) -> None:
        """Build the delivery."""
        ticks = count(1_760_000_000, 10)

        async def no_wait(_seconds: float) -> None:
            return None

        self.h = h
        self.delivery = WebhookDelivery(
            create_session_factory(h.database),
            h.crypto,
            transport=transport,
            retry=WebhookRetry((5.0, 30.0, 120.0)),
            sleep=no_wait,
            clock=lambda: float(next(ticks)),
        )

    async def deliver(self) -> None:
        """Deploy staging for real and deliver the event it announced."""
        outcome = await self.h.deploy(self.h.seed.services.staging_web.id)
        assert outcome.status is DeployStatus.SUCCESS
        await self.delivery.deliver(self.h.notifier.events[-1], "evt_test")


async def test_a_webhook_delivery_is_a_span_per_attempt_and_never_names_the_address(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    await configure_webhook(harness.database, harness.crypto)
    transport = RecordingWebhook(outcomes=[temporary_failure(), 202])
    await Webhooks(harness, transport).deliver()

    attempts = telemetry_recorder.spans(telemetry.WEBHOOK_DELIVER_SPAN)
    assert [dict(span.attributes or {}) for span in attempts] == [
        {
            telemetry.ATTEMPT: 1,
            telemetry.RESULT: "failed",
            telemetry.HTTP_STATUS: 503,
            telemetry.RETRYABLE: True,
        },
        {telemetry.ATTEMPT: 2, telemetry.RESULT: "delivered", telemetry.HTTP_STATUS: 202},
    ]
    everything = telemetry_recorder.everything_recorded()
    assert "hooks.example.com" not in everything and "path-token" not in everything
    assert WEBHOOK_URL not in everything


async def test_a_webhook_refused_for_good_is_one_failed_attempt(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    await configure_webhook(harness.database, harness.crypto)
    await Webhooks(harness, RecordingWebhook(outcomes=[permanent_failure()])).deliver()

    (span,) = telemetry_recorder.spans(telemetry.WEBHOOK_DELIVER_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes[telemetry.RESULT] == "failed" and attributes[telemetry.RETRYABLE] is False


async def test_sign_in_failures_and_lockouts_are_counted_without_a_name_or_address(
    engine: AsyncEngine, crypto: CryptoService, telemetry_recorder: Telemetry
) -> None:
    async with create_session_factory(engine)() as db:
        db.add(make_user())
        await db.commit()
    for attempt in range(3):
        async with create_session_factory(engine)() as db:
            result = await authenticate(
                db,
                crypto,
                POLICY,
                identifier="alice@example.test",
                password=f"{PASSWORD}-wrong-{attempt}",
                ip_address="203.0.113.7",
            )
        assert result.status.value in {"invalid", "locked"}

    assert telemetry_recorder.counted("envmanager.login.failures") == {
        (("channel", metrics.CHANNEL_SIGN_IN),): 3
    }
    assert telemetry_recorder.counted("envmanager.login.lockouts") == {(("kind", "user"),): 1}
    everything = telemetry_recorder.everything_recorded()
    assert "alice" not in everything and "203.0.113.7" not in everything


async def test_a_failure_with_two_counters_is_one_failure_and_each_lockout_is_counted(
    engine: AsyncEngine, telemetry_recorder: Telemetry
) -> None:
    now = utc_now()
    async with create_session_factory(engine)() as db:
        for _ in range(5):
            locked = await register_attempt_failure(
                db, POLICY, subject="bob", ip_address="198.51.100.2", now=now
            )
        await db.commit()

    assert locked == ["user", "ip"]
    assert telemetry_recorder.counted("envmanager.login.failures") == {
        (("channel", metrics.CHANNEL_SIGN_IN),): 5
    }
    assert telemetry_recorder.counted("envmanager.login.lockouts") == {
        (("kind", "user"),): 1,
        (("kind", "ip"),): 1,
    }, "a lockout is counted when it starts, not at every failure after it"


async def test_the_age_gauge_is_seeded_from_the_newest_backup_that_left_a_snapshot(
    factory: async_sessionmaker[AsyncSession],
    locations: BackupLocations,
    tmp_path: Path,
    clean_backup_clock: metrics._BackupClock,
) -> None:
    async with factory() as db:
        assert await latest_snapshot_time(db) is None
    failing = locations_in(tmp_path / "other", tmp_path / "missing" / "envmanager.db")
    assert await backup(factory, failing) is BackupStatus.FAILURE
    async with factory() as db:
        assert await latest_snapshot_time(db) is None, "a failed backup left no snapshot"
    assert await backup(factory, locations) is BackupStatus.SUCCESS
    async with factory() as db:
        newest = await latest_snapshot_time(db)
        found = await db.execute(select(Backup).where(Backup.status == BackupStatus.SUCCESS))
        record = found.scalar_one()
    assert newest is not None and newest == record.created_at
    assert newest.tzinfo is not None, "the time is aware, so the gauge can subtract it from now"
