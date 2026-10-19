"""Tests for the notifier: switches, recipients, retries, the failure count and never blocking.

The mail transport is a recording fake, so these tests run the notifier, the recipient rules, the
templates and the stored settings together and read back what was sent and what was stored.
"""

import asyncio

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService, generate_master_key, parse_master_key
from app.core.db import create_session_factory
from app.models import (
    AccessGrant,
    BackupStatus,
    BackupTrigger,
    DeployResultStatus,
    DeployStatus,
    NotificationStatus,
    PostDeployStatus,
    Project,
)
from app.models.types import utc_now
from app.seeds import SeedData
from app.services import notifier as notifier_module
from app.services.app_settings import SmtpSettings
from app.services.backup_events import BackupFinished
from app.services.deploy_events import DeployFinished, EnvironmentResult
from app.services.notifier import Notifications, RetryPolicy
from app.services.notify_events import (
    FileChange,
    FileChanged,
    GitPushFailed,
    LockoutScope,
    LockoutStarted,
)
from app.services.notify_status import EMAIL_CHANNEL, record_failure
from app.services.settings_events import SettingsChanged
from app.tests.notify_helpers import (
    SMTP,
    SMTP_PASSWORD,
    RecordingTransport,
    build_notifier,
    configure_smtp,
    decoded_text,
    mime_of,
    permanent_failure,
    switch,
    temporary_failure,
)


def deploy(seed: SeedData, status: DeployStatus = DeployStatus.FAILED) -> DeployFinished:
    """A finished deploy of both files of the production MySQL service."""
    envs = seed.envs
    return DeployFinished(
        1,
        seed.projects.fancy.id,
        seed.deployments.fancy_prod.id,
        "prod",
        seed.services.mysql.id,
        "mysql",
        "root",
        False,
        status,
        PostDeployStatus.NONE,
        (
            EnvironmentResult(envs.db.id, ".env.db", 2, DeployResultStatus.WRITTEN),
            EnvironmentResult(envs.db_secrets.id, ".env.db.secrets", 1, DeployResultStatus.FAILED),
        ),
    )


def lockout() -> LockoutStarted:
    """A lockout of a user, a system event."""
    return LockoutStarted(LockoutScope.USER, "alice", None, 15)


async def failures(engine: AsyncEngine) -> list[NotificationStatus]:
    """The delivery status rows stored now."""
    async with create_session_factory(engine)() as db:
        return list((await db.execute(select(NotificationStatus))).scalars())


@pytest.fixture
def transport() -> RecordingTransport:
    """The transport every notifier of the test sends through."""
    return RecordingTransport()


@pytest.fixture
async def world(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> Notifications:
    """A notifier on the seeded database with SMTP configured."""
    await configure_smtp(seeded_database, crypto)
    return build_notifier(seeded_database, crypto, transport)


async def test_nothing_is_sent_while_smtp_is_not_set_up(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> None:
    """Without a host and a from address an event is dropped silently and counts as nothing."""
    notifier = build_notifier(seeded_database, crypto, transport)
    await notifier.notify(lockout())
    await notifier.idle()
    assert transport.calls == 0 and await failures(seeded_database) == []


async def test_a_failed_deploy_is_sent_to_each_project_admin_with_only_their_files(
    world: Notifications, transport: RecordingTransport, seed_data: SeedData
) -> None:
    """By default the admins of the project hear of a failure and the superadmin does not."""
    await world.notify(deploy(seed_data))
    await world.idle()
    assert sorted(transport.to()) == [
        "admin@example.test",
        "deployment_admin@example.test",
        "env_scoped_admin@example.test",
        "project_admin@example.test",
    ]
    by_address = {item.mail.to: item.mail for item in transport.sent}
    scoped = decoded_text(await mime_of(by_address["env_scoped_admin@example.test"]))
    assert ".env.db: written, version 2" in scoped and ".env.db.secrets" not in scoped
    full = decoded_text(await mime_of(by_address["project_admin@example.test"]))
    assert ".env.db.secrets: failed" in full
    assert by_address["admin@example.test"].subject == "[ENV Manager] Deploy failed: mysql (prod)"


async def test_a_successful_deploy_is_not_sent_until_its_switch_is_turned_on(
    world: Notifications,
    transport: RecordingTransport,
    seed_data: SeedData,
    seeded_database: AsyncEngine,
) -> None:
    """Successes are off by default; with the switch on they go to the chosen audience."""
    await world.notify(deploy(seed_data, DeployStatus.SUCCESS))
    await world.idle()
    assert transport.calls == 0
    await switch(seeded_database, deploy_succeeded=True, deploy_succeeded_to="both")
    await world.notify(deploy(seed_data, DeployStatus.SUCCESS))
    await world.idle()
    assert "root@example.test" in transport.to() and len(transport.sent) == 5


async def test_the_recipients_choice_applies_to_the_kind_it_was_made_for(
    world: Notifications,
    transport: RecordingTransport,
    seed_data: SeedData,
    seeded_database: AsyncEngine,
) -> None:
    """Choosing superadmins for problems sends to the superadmin and not to the project admins."""
    await switch(seeded_database, deploy_problem_to="superadmins")
    await world.notify(deploy(seed_data, DeployStatus.BLOCKED))
    await world.idle()
    assert transport.to() == ["root@example.test"]
    assert "Deploy blocked" in transport.sent[0].mail.subject


async def test_a_system_event_goes_to_the_superadmin_and_a_switch_turns_it_off(
    world: Notifications, transport: RecordingTransport, seeded_database: AsyncEngine
) -> None:
    """Lockouts are on by default for the superadmins; switched off they send nothing."""
    await world.notify(lockout())
    await world.idle()
    assert transport.to() == ["root@example.test"]
    await switch(seeded_database, lockout=False)
    await world.notify(lockout())
    await world.idle()
    assert len(transport.sent) == 1


async def test_a_successful_backup_sends_nothing_and_a_failed_one_does(
    world: Notifications, transport: RecordingTransport
) -> None:
    """Only a failed or incomplete backup is mail, and it is not even queued when it worked.

    Mails go out from background tasks, so the order of the two is not part of the contract.
    """

    def result(status: BackupStatus) -> BackupFinished:
        return BackupFinished(
            status,
            BackupTrigger.SCHEDULED,
            "x.db",
            None,
            "The copy failed.",
            utc_now(),
            "scheduler",
        )

    await world.backup_finished(result(BackupStatus.SUCCESS))
    assert not world._pending
    await world.backup_finished(result(BackupStatus.FAILURE))
    await world.backup_finished(result(BackupStatus.PARTIAL))
    await world.idle()
    assert sorted(item.mail.subject for item in transport.sent) == [
        "[ENV Manager] Backup failed",
        "[ENV Manager] Backup incomplete",
    ]


async def test_an_allowed_path_change_is_always_announced_to_the_superadmin(
    world: Notifications, transport: RecordingTransport
) -> None:
    """The settings seam has no switch: every change is a system notification."""
    await world.settings_changed(SettingsChanged("allowed_paths", "removed", "/opt/x", "root"))
    await world.idle()
    assert transport.to() == ["root@example.test"]
    assert transport.sent[0].mail.subject == "[ENV Manager] Allowed path removed: /opt/x"


async def test_the_stored_settings_and_decrypted_password_are_read_when_a_mail_is_sent(
    world: Notifications,
    transport: RecordingTransport,
    seeded_database: AsyncEngine,
    crypto: CryptoService,
) -> None:
    """A save between two events applies to the second: nothing is cached."""
    await world.notify(lockout())
    await world.idle()
    other = SmtpSettings("smtp.other.example", 465, "tls", "", "other@example.com")
    await configure_smtp(seeded_database, crypto, other, password="second-password")
    await world.notify(lockout())
    await world.idle()
    first, second = transport.sent
    assert (first.smtp, first.password) == (SMTP, SMTP_PASSWORD)
    assert (second.smtp, second.password) == (other, "second-password")


async def test_a_temporary_failure_is_retried_with_the_backoff_and_then_succeeds(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> None:
    """Two refusals in a row, then the mail goes; the waits are the policy's, in order."""
    await configure_smtp(seeded_database, crypto)
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    transport.failures = [temporary_failure(), temporary_failure()]
    notifier = build_notifier(
        seeded_database, crypto, transport, retry=RetryPolicy((1.5, 7.0)), sleep=record
    )
    await notifier.notify(lockout())
    await notifier.idle()
    assert (transport.calls, len(transport.sent), waits) == (3, 1, [1.5, 7.0])
    assert await failures(seeded_database) == []


async def test_the_retries_are_bounded_and_the_failure_is_counted_with_a_fixed_reason(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> None:
    """After the last attempt the mail is given up and counted once; nothing is raised."""
    await configure_smtp(seeded_database, crypto)
    transport.failures = [temporary_failure() for _ in range(10)]
    notifier = build_notifier(seeded_database, crypto, transport, retry=RetryPolicy((0, 0, 0)))
    await notifier.notify(lockout())
    await notifier.idle()
    assert transport.calls == 4 and transport.sent == []
    (row,) = await failures(seeded_database)
    assert (row.channel, row.failures, row.reason) == (
        EMAIL_CHANNEL,
        1,
        "The mail server did not answer in time.",
    )


async def test_a_permanent_failure_is_not_retried(
    world: Notifications, transport: RecordingTransport, seeded_database: AsyncEngine
) -> None:
    """A refused login or address will not work later, so it is tried once and counted."""
    transport.failures = [permanent_failure()]
    await world.notify(lockout())
    await world.idle()
    assert transport.calls == 1
    (row,) = await failures(seeded_database)
    assert row.reason == "The mail server refused the username or password."


async def test_one_recipient_that_fails_does_not_stop_the_others_and_still_counts(
    world: Notifications,
    transport: RecordingTransport,
    seed_data: SeedData,
    seeded_database: AsyncEngine,
) -> None:
    """Each recipient has their own message, so one refusal costs only that message, and the
    event still counts as a failure: the others working does not clear it."""
    transport.failures = [permanent_failure()]
    await world.notify(deploy(seed_data))
    await world.idle()
    assert transport.calls == 4 and len(transport.sent) == 3
    (row,) = await failures(seeded_database)
    assert row.failures == 1


async def test_failures_add_up_until_a_mail_works_and_then_start_over(
    world: Notifications,
    transport: RecordingTransport,
    seeded_database: AsyncEngine,
    seed_data: SeedData,
) -> None:
    """The count is per channel since the last delivery that worked."""
    transport.failures = [permanent_failure(), permanent_failure()]
    await world.notify(deploy(seed_data))
    await world.idle()
    (row,) = await failures(seeded_database)
    assert row.failures == 2
    transport.failures = [permanent_failure()]
    await world.notify(lockout())
    await world.idle()
    (row,) = await failures(seeded_database)
    assert row.failures == 3
    await world.notify(lockout())
    await world.idle()
    assert await failures(seeded_database) == []


async def test_an_unexpected_error_from_the_transport_is_a_counted_failure_not_a_crash(
    world: Notifications, transport: RecordingTransport, seeded_database: AsyncEngine
) -> None:
    """A transport that raises something it should not is treated as a retryable failure."""
    transport.failures = [RuntimeError("secret in the message"), RuntimeError("again")]
    await world.notify(lockout())
    await world.idle()
    assert len(transport.sent) == 1
    transport.failures = [RuntimeError("x") for _ in range(5)]
    await world.notify(lockout())
    await world.idle()
    (row,) = await failures(seeded_database)
    assert row.reason == "The mail could not be sent." and "secret" not in row.reason


async def test_a_password_the_key_cannot_open_is_counted_and_nothing_is_sent(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> None:
    """A different master key cannot read the stored password: a counted failure, no mail."""
    await configure_smtp(seeded_database, crypto)
    other = CryptoService(parse_master_key(generate_master_key()))
    notifier = build_notifier(seeded_database, other, transport)
    await notifier.notify(lockout())
    await notifier.idle()
    assert transport.calls == 0
    (row,) = await failures(seeded_database)
    assert row.reason == "The stored SMTP password cannot be read with the current key."


async def test_a_server_that_never_answers_does_not_hold_up_the_caller(
    world: Notifications, transport: RecordingTransport
) -> None:
    """notify returns at once while the mail waits; the work finishes when the server does."""
    transport.hang = asyncio.Event()
    await asyncio.wait_for(world.notify(lockout()), timeout=0.5)
    await asyncio.sleep(0.05)
    assert transport.sent == [] and world._pending
    transport.hang.set()
    await world.idle()
    assert len(transport.sent) == 1


async def test_events_over_the_cap_are_dropped_and_counted(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> None:
    """With the queue full, more events are dropped (never queued) and counted as failures."""
    await configure_smtp(seeded_database, crypto)
    transport.hang = asyncio.Event()
    notifier = build_notifier(seeded_database, crypto, transport, max_pending=2)
    for _ in range(5):
        await notifier.notify(lockout())
    assert len(notifier._pending) == 2
    await asyncio.sleep(0.05)
    (row,) = await failures(seeded_database)
    assert row.failures == 3 and "dropped" in row.reason
    transport.hang.set()
    await notifier.idle()
    assert len(transport.sent) == 2


async def test_closing_cancels_what_is_still_waiting(
    seeded_database: AsyncEngine,
    crypto: CryptoService,
    transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown gives events a moment and then cancels them, leaving no task behind."""
    await configure_smtp(seeded_database, crypto)
    monkeypatch.setattr(notifier_module, "CLOSE_GRACE_SECONDS", 0.05)
    transport.hang = asyncio.Event()
    notifier = build_notifier(seeded_database, crypto, transport)
    await notifier.notify(lockout())
    await asyncio.sleep(0.01)
    await asyncio.wait_for(notifier.close(), timeout=2)
    assert not notifier._pending and transport.sent == []


async def test_the_command_line_notifier_delivers_before_it_returns_and_does_not_retry(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> None:
    """Inline mode sends in the caller's task, once, and a failure never raises."""
    await configure_smtp(seeded_database, crypto)
    notifier = build_notifier(seeded_database, crypto, transport, retry=None, inline=True)
    await notifier.notify(lockout())
    assert transport.to() == ["root@example.test"] and not notifier._pending
    transport.failures = [temporary_failure()]
    await notifier.notify(lockout())
    assert transport.calls == 2
    (row,) = await failures(seeded_database)
    assert row.failures == 1


async def test_the_command_line_notifier_is_bounded_in_time(
    seeded_database: AsyncEngine,
    crypto: CryptoService,
    transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that never answers cannot hold the command beyond the limit, and nothing raises."""
    await configure_smtp(seeded_database, crypto)
    monkeypatch.setattr(notifier_module, "INLINE_TIMEOUT_SECONDS", 0.05)
    transport.hang = asyncio.Event()
    notifier = build_notifier(seeded_database, crypto, transport, inline=True)
    await asyncio.wait_for(notifier.notify(lockout()), timeout=2)
    assert transport.sent == []


async def test_a_recipient_list_with_no_one_in_it_sends_nothing_and_counts_nothing(
    seeded_database: AsyncEngine,
    crypto: CryptoService,
    transport: RecordingTransport,
    seed_data: SeedData,
) -> None:
    """A project whose admins are all gone has nobody to tell, which is not a failure."""
    await configure_smtp(seeded_database, crypto)
    async with create_session_factory(seeded_database)() as db:
        await db.execute(delete(AccessGrant))
        await db.commit()
    notifier = build_notifier(seeded_database, crypto, transport)
    await notifier.notify(deploy(seed_data))
    await notifier.idle()
    assert transport.calls == 0 and await failures(seeded_database) == []


async def test_the_status_is_one_row_per_channel_that_counts_up_by_the_given_amount(
    seeded_database: AsyncEngine,
) -> None:
    """Failures are counted by one upsert, and the latest reason replaces the earlier one."""
    async with create_session_factory(seeded_database)() as db:
        await record_failure(db, EMAIL_CHANNEL, "first", utc_now())
        await record_failure(db, EMAIL_CHANNEL, "second", utc_now(), 4)
        await db.commit()
    (row,) = await failures(seeded_database)
    assert (row.failures, row.reason) == (5, "second")


async def test_a_retry_uses_the_settings_saved_while_the_mail_waited(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> None:
    """The mail that failed against an old server goes to the new one once settings change."""
    await configure_smtp(seeded_database, crypto)
    other = SmtpSettings("smtp.fixed.example", 465, "tls", "", "other@example.com")

    async def fix_the_server(_seconds: float) -> None:
        await configure_smtp(seeded_database, crypto, other, password="fixed-password")

    transport.failures = [temporary_failure()]
    notifier = build_notifier(
        seeded_database, crypto, transport, retry=RetryPolicy((1.0,)), sleep=fix_the_server
    )
    await notifier.notify(lockout())
    await notifier.idle()
    (sent,) = transport.sent
    assert (sent.smtp, sent.password) == (other, "fixed-password")
    assert await failures(seeded_database) == []


async def test_a_retry_after_mail_was_switched_off_drops_the_mail_without_counting_it(
    seeded_database: AsyncEngine, crypto: CryptoService, transport: RecordingTransport
) -> None:
    """Emptying the SMTP settings while a mail waits is a decision, not a delivery failure."""
    await configure_smtp(seeded_database, crypto)

    async def switch_mail_off(_seconds: float) -> None:
        await configure_smtp(seeded_database, crypto, SmtpSettings(), password=None)

    transport.failures = [temporary_failure()]
    notifier = build_notifier(
        seeded_database, crypto, transport, retry=RetryPolicy((1.0,)), sleep=switch_mail_off
    )
    await notifier.notify(lockout())
    await notifier.idle()
    assert transport.calls == 1 and transport.sent == []
    assert await failures(seeded_database) == []


async def test_a_git_push_failure_is_sent_to_the_superadmin_until_its_switch_is_off(
    world: Notifications, transport: RecordingTransport, seeded_database: AsyncEngine
) -> None:
    """A failed git push is a system mail for the superadmins, with its own switch."""
    await world.notify(GitPushFailed("The repository refused the credentials.", 3))
    await world.idle()
    assert transport.to() == ["root@example.test"]
    assert transport.sent[0].mail.subject == "[ENV Manager] Git push failed"
    text = decoded_text(await mime_of(transport.sent[0].mail))
    assert "The repository refused the credentials." in text and "Attempts: 3" in text
    await switch(seeded_database, git_push_failed=False)
    await world.notify(GitPushFailed("again", 1))
    await world.idle()
    assert len(transport.sent) == 1


async def set_extra_addresses(engine: AsyncEngine, project_id: int, addresses: list[str]) -> None:
    """Store the extra notification addresses of a project."""
    async with create_session_factory(engine)() as db:
        project = await db.get(Project, project_id)
        assert project is not None
        project.extra_addresses = addresses
        await db.commit()


async def test_extra_addresses_hear_of_a_problem_with_every_file_whatever_the_choice(
    world: Notifications,
    transport: RecordingTransport,
    seed_data: SeedData,
    seeded_database: AsyncEngine,
) -> None:
    """They are told even when only the superadmins are chosen, once each, about all the files."""
    await switch(seeded_database, deploy_problem_to="superadmins")
    await set_extra_addresses(
        seeded_database,
        seed_data.projects.fancy.id,
        ["Team@Example.org", "team@example.org", "pager@corp.local"],
    )
    await world.notify(deploy(seed_data, DeployStatus.BLOCKED))
    await world.idle()
    assert sorted(transport.to()) == ["Team@Example.org", "pager@corp.local", "root@example.test"]
    by_address = {item.mail.to: item.mail for item in transport.sent}
    for extra in ("Team@Example.org", "pager@corp.local"):
        text = decoded_text(await mime_of(by_address[extra]))
        assert ".env.db: written, version 2" in text and ".env.db.secrets: failed" in text


async def test_an_extra_address_that_is_also_a_scoped_admin_is_told_about_the_whole_project(
    world: Notifications,
    transport: RecordingTransport,
    seed_data: SeedData,
    seeded_database: AsyncEngine,
) -> None:
    """One mail to the address, and it names every file of the event, as the superadmin chose."""
    await set_extra_addresses(
        seeded_database, seed_data.projects.fancy.id, ["ENV_SCOPED_ADMIN@example.test"]
    )
    await world.notify(deploy(seed_data))
    await world.idle()
    scoped = [item for item in transport.sent if item.mail.to.lower().startswith("env_scoped")]
    assert len(scoped) == 1
    assert ".env.db.secrets: failed" in decoded_text(await mime_of(scoped[0].mail))


async def test_the_extra_addresses_follow_the_switch_of_the_event(
    world: Notifications,
    transport: RecordingTransport,
    seed_data: SeedData,
    seeded_database: AsyncEngine,
) -> None:
    """A kind that is off sends nothing to anyone, and a success needs its own switch."""
    await set_extra_addresses(seeded_database, seed_data.projects.fancy.id, ["team@example.org"])
    await world.notify(deploy(seed_data, DeployStatus.SUCCESS))
    await world.idle()
    assert "team@example.org" not in transport.to()
    await switch(seeded_database, deploy_succeeded=True, deploy_problem=False)
    await world.notify(deploy(seed_data, DeployStatus.FAILED))
    await world.idle()
    assert "team@example.org" not in transport.to()
    await world.notify(deploy(seed_data, DeployStatus.SUCCESS))
    await world.idle()
    assert "team@example.org" in transport.to()


async def test_the_extra_addresses_are_told_of_file_changes_of_their_project_only(
    world: Notifications,
    transport: RecordingTransport,
    seed_data: SeedData,
    seeded_database: AsyncEngine,
) -> None:
    """A new version in the project reaches them; one in another project does not."""
    await switch(seeded_database, file_changed=True, file_changed_to="superadmins")
    await set_extra_addresses(seeded_database, seed_data.projects.fancy.id, ["team@example.org"])
    await set_extra_addresses(seeded_database, seed_data.projects.billing.id, ["bill@example.org"])
    envs = seed_data.envs
    await world.notify(
        FileChanged(
            seed_data.projects.fancy.id,
            seed_data.deployments.fancy_prod.id,
            seed_data.services.mysql.id,
            envs.db.id,
            ".env.db",
            3,
            FileChange.SAVED,
            "root",
        )
    )
    await world.idle()
    assert sorted(transport.to()) == ["root@example.test", "team@example.org"]


async def test_a_system_event_never_goes_to_the_extra_addresses(
    world: Notifications,
    transport: RecordingTransport,
    seed_data: SeedData,
    seeded_database: AsyncEngine,
) -> None:
    """Lockouts and the like are the superadmins' business."""
    await set_extra_addresses(seeded_database, seed_data.projects.fancy.id, ["team@example.org"])
    await world.notify(lockout())
    await world.idle()
    assert transport.to() == ["root@example.test"]
