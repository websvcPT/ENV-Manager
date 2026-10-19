"""Tests for delivering deploy results to the webhook: the document, the retries, the audit trail,
the failure count behind the banner and the settings that switch it on or off."""

import asyncio
import json
from itertools import count

import pytest
from sqlalchemy import select

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.core.display_time import DisplayTime
from app.models import (
    AppSetting,
    AuditActorType,
    AuditEntry,
    AuditResult,
    DeployStatus,
    Environment,
    HookType,
    NotificationStatus,
    PostDeployStatus,
    ServiceHook,
)
from app.models.types import utc_now
from app.schemas.api import RunResult
from app.schemas.webhook import DeployWebhookBody
from app.seeds import EDITOR_ENVIRONMENT_NAME, EDITOR_SECRET_VALUES
from app.services.api_runs import load_whole_run_result
from app.services.app_settings import WEBHOOK_SECRET_KEY, load_stored_settings
from app.services.audit import AuditActor
from app.services.deploy_events import DeployFinished
from app.services.hooks import PostDeployResult
from app.services.notify_status import EMAIL_CHANNEL, WEBHOOK_CHANNEL, load_failures, record_failure
from app.services.shell_banners import load_shell_banners
from app.services.webhook_delivery import (
    NOT_SET_UP,
    UNREADABLE_SECRET,
    WebhookDelivery,
    WebhookRetry,
    send_test_event,
)
from app.tests.deploy_helpers import Harness, require_extra_key, version_text
from app.tests.notify_helpers import SMTP, RecordingTransport, build_notifier, configure_smtp
from app.tests.webhook_helpers import (
    WEBHOOK_SECRET,
    WEBHOOK_URL,
    RecordingWebhook,
    configure_webhook,
    permanent_failure,
    temporary_failure,
    verifies,
)

DELAYS = (5.0, 30.0, 120.0)


class World:
    """A delivery on the harness's database with a recording transport and a fake clock."""

    def __init__(self, h: Harness, webhook: RecordingWebhook) -> None:
        """Build the delivery; the clock moves ten seconds with every look at it."""
        self.h = h
        self.webhook = webhook
        self.sleeps: list[float] = []
        self.on_sleep: list[object] = []
        ticks = count(1_760_000_000, 10)
        self.delivery = WebhookDelivery(
            create_session_factory(h.database),
            h.crypto,
            transport=webhook,
            retry=WebhookRetry(DELAYS),
            sleep=self._sleep,
            clock=lambda: float(next(ticks)),
        )

    async def _sleep(self, seconds: float) -> None:
        """Remember the wait instead of making it, and run what the test wants to happen then."""
        self.sleeps.append(seconds)
        if self.on_sleep:
            step = self.on_sleep.pop(0)
            assert callable(step)
            await step()

    async def real_deploy(self) -> DeployFinished:
        """A real deploy of the staging web service and the event it announced."""
        outcome = await self.h.deploy(self.h.seed.services.staging_web.id)
        assert outcome.status is DeployStatus.SUCCESS
        return self.h.notifier.events[-1]

    async def audit(self, action: str = "webhook.deliver") -> list[AuditEntry]:
        """The audit entries of an action, oldest first."""
        async with create_session_factory(self.h.database)() as db:
            rows = await db.execute(
                select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
            )
            return list(rows.scalars())

    async def failures(self) -> list[NotificationStatus]:
        """The delivery status rows stored now."""
        async with create_session_factory(self.h.database)() as db:
            return await load_failures(db)


@pytest.fixture
def webhook() -> RecordingWebhook:
    """The transport every delivery of the test posts through."""
    return RecordingWebhook()


@pytest.fixture
async def world(harness: Harness, webhook: RecordingWebhook) -> World:
    """A delivery with the webhook switched on."""
    await configure_webhook(harness.database, harness.crypto)
    return World(harness, webhook)


async def test_a_deploy_is_posted_once_as_a_signed_document_of_the_whole_run(
    world: World,
) -> None:
    """The document has the API's fields plus the event, its id, the project and the deployment."""
    event = await world.real_deploy()
    await world.delivery.deliver(event, "event-1")
    (request,) = world.webhook.sent
    assert request.url == WEBHOOK_URL and verifies(WEBHOOK_SECRET, request)
    body = DeployWebhookBody.model_validate_json(request.body)
    assert body.event == "deploy.finished" and body.event_id == "event-1"
    assert body.project.name == world.h.seed.projects.rentals.name
    assert body.deployment.name == "staging" and body.service.name == "web"
    assert body.run_id == event.run_id and body.status is DeployStatus.SUCCESS
    async with create_session_factory(world.h.database)() as db:
        names = (
            await db.scalars(
                select(Environment.name).where(
                    Environment.service_id == world.h.seed.services.staging_web.id
                )
            )
        ).all()
    assert len(names) >= 2 and {item.name for item in body.environments} == set(names)
    assert body.post_deploy.status is PostDeployStatus.RAN and body.post_deploy.exit_code == 0
    async with create_session_factory(world.h.database)() as db:
        run = await load_whole_run_result(db, event.run_id, event.service_id)
    assert run is not None
    document = json.loads(request.body)
    assert {key: document[key] for key in RunResult.model_fields} == run.model_dump(mode="json")
    assert set(document) == set(RunResult.model_fields) | {
        "event",
        "event_id",
        "project",
        "deployment",
    }


async def test_a_delivered_attempt_is_audited_with_the_host_and_never_the_secret(
    world: World,
) -> None:
    """One entry per attempt: the event, the run, the attempt, the host and the HTTP status."""
    event = await world.real_deploy()
    await world.delivery.deliver(event, "event-1")
    (entry,) = await world.audit()
    assert entry.result is AuditResult.SUCCESS and entry.actor_type is AuditActorType.SYSTEM
    assert entry.actor_name == "webhook" and entry.target_name == "hooks.example.com"
    assert entry.details == {
        "event_id": "event-1",
        "run_id": event.run_id,
        "service": "web",
        "attempt": 1,
        "host": "hooks.example.com",
        "http_status": 200,
    }
    assert (entry.project_id, entry.deployment_id, entry.service_id) == (None, None, None)
    stored = json.dumps([entry.details, entry.target_name, entry.actor_name])
    assert WEBHOOK_SECRET not in stored and "path-token" not in stored and "T0001" not in stored


async def test_every_status_of_a_real_deploy_is_posted_a_blocked_one_included(
    world: World,
) -> None:
    """The result is whatever the run found, with the per file outcome and the dist check."""
    h = world.h
    tuning = await require_extra_key(h)
    outcome = await h.deploy(h.seed.services.staging_web.id, environments=[tuning])
    assert outcome.status is DeployStatus.BLOCKED
    await world.delivery.deliver(h.notifier.events[-1], "event-blocked")
    (request,) = world.webhook.sent
    body = DeployWebhookBody.model_validate_json(request.body)
    assert body.status is DeployStatus.BLOCKED
    (file,) = body.environments
    assert (file.name, file.status.value, file.dist) == (tuning.name, "blocked", "blocked")
    assert file.message == "Blocked: 1 dist key is missing from this file."


async def test_a_temporary_failure_is_tried_again_with_the_same_document_and_a_new_signature(
    world: World,
) -> None:
    """The event id and bytes stay, the timestamp and signature are fresh, and the waits grow."""
    world.webhook.outcomes = [temporary_failure(), temporary_failure(), 202]
    event = await world.real_deploy()
    await world.delivery.deliver(event, "event-1")
    sent = world.webhook.sent
    assert len(sent) == 3 and world.sleeps == [5.0, 30.0]
    assert len({request.body for request in sent}) == 1
    assert json.loads(sent[0].body)["event_id"] == "event-1"
    stamps = [request.headers["X-EnvManager-Timestamp"] for request in sent]
    signatures = [request.headers["X-EnvManager-Signature"] for request in sent]
    assert len(set(stamps)) == 3 and len(set(signatures)) == 3
    assert all(verifies(WEBHOOK_SECRET, request) for request in sent)
    entries = await world.audit()
    assert [entry.details["attempt"] for entry in entries] == [1, 2, 3]
    assert [entry.result for entry in entries] == [
        AuditResult.FAILURE,
        AuditResult.FAILURE,
        AuditResult.SUCCESS,
    ]
    assert [entry.details.get("will_retry") for entry in entries] == [True, True, None]
    assert entries[0].details["http_status"] == 503
    assert entries[2].details["http_status"] == 202
    assert await world.failures() == []


async def test_an_event_that_never_gets_through_is_counted_once_for_the_banner(
    world: World,
) -> None:
    """Four attempts, then one failure for the event, and the banner links to the settings tab."""
    world.webhook.outcomes = [temporary_failure() for _ in range(4)]
    event = await world.real_deploy()
    await world.delivery.deliver(event, "event-1")
    assert len(world.webhook.sent) == 4 and world.sleeps == [5.0, 30.0, 120.0]
    entries = await world.audit()
    assert [entry.details["will_retry"] for entry in entries] == [True, True, True, False]
    (row,) = await world.failures()
    assert (row.channel, row.failures) == (WEBHOOK_CHANNEL, 1)
    assert row.reason == "The webhook answered with an error (HTTP 503)."
    world.webhook.outcomes = [temporary_failure() for _ in range(4)]
    await world.delivery.deliver(event, "event-2")
    (row,) = await world.failures()
    assert row.failures == 2
    async with create_session_factory(world.h.database)() as db:
        (banner,) = await load_shell_banners(db, DisplayTime(), SMTP)
    assert banner.message.startswith("The deploy webhook is failing: 2 events could not be")
    assert (banner.action_label, banner.action_href) == (
        "Open notification settings",
        "/settings/notifications",
    )


async def test_a_refusal_that_waiting_cannot_fix_is_tried_once(world: World) -> None:
    """A 404 is final: one attempt, no wait, one failure counted."""
    world.webhook.outcomes = [permanent_failure()]
    await world.delivery.deliver(await world.real_deploy(), "event-1")
    assert len(world.webhook.sent) == 1 and world.sleeps == []
    (entry,) = await world.audit()
    assert entry.details["will_retry"] is False and entry.details["http_status"] == 404
    (row,) = await world.failures()
    assert row.failures == 1


async def test_a_delivered_event_clears_the_webhook_failures_and_leaves_the_mail_count(
    world: World,
) -> None:
    """Delivery health is per channel."""
    async with create_session_factory(world.h.database)() as db:
        await record_failure(db, WEBHOOK_CHANNEL, "old", utc_now(), 3)
        await record_failure(db, EMAIL_CHANNEL, "mail", utc_now(), 2)
        await db.commit()
    await world.delivery.deliver(await world.real_deploy(), "event-1")
    assert [(row.channel, row.failures) for row in await world.failures()] == [(EMAIL_CHANNEL, 2)]


@pytest.mark.parametrize("state", ["off", "no_url", "no_secret"])
async def test_nothing_is_posted_while_the_webhook_is_off_or_not_set_up(
    world: World, state: str
) -> None:
    """Switched off, no address or no secret means no request, no audit entry and no failure."""
    event = await world.real_deploy()
    h = world.h
    if state == "off":
        await configure_webhook(h.database, h.crypto, enabled=False)
    elif state == "no_url":
        await configure_webhook(h.database, h.crypto, url="", enabled=True)
    else:
        async with create_session_factory(h.database)() as db:
            row = await db.get(AppSetting, WEBHOOK_SECRET_KEY)
            assert row is not None
            await db.delete(row)
            await db.commit()
    await world.delivery.deliver(event, "event-1")
    assert world.webhook.sent == [] and await world.audit() == [] and await world.failures() == []


async def test_a_webhook_switched_off_while_an_event_waits_drops_it(world: World) -> None:
    """Each retry reads the settings again."""
    world.webhook.outcomes = [temporary_failure(), temporary_failure()]
    event = await world.real_deploy()

    async def switch_off() -> None:
        await configure_webhook(world.h.database, world.h.crypto, enabled=False, secret=None)

    world.on_sleep = [switch_off]
    await world.delivery.deliver(event, "event-1")
    assert len(world.webhook.sent) == 1 and len(await world.audit()) == 1
    assert await world.failures() == []


async def test_a_corrected_address_is_used_by_the_retry(world: World) -> None:
    """A receiver that was moved while the event waited gets it at the new address."""
    world.webhook.outcomes = [temporary_failure()]
    event = await world.real_deploy()

    async def move() -> None:
        await configure_webhook(
            world.h.database, world.h.crypto, url="https://other.example.net/hook", secret=None
        )

    world.on_sleep = [move]
    await world.delivery.deliver(event, "event-1")
    assert [request.url for request in world.webhook.sent] == [
        WEBHOOK_URL,
        "https://other.example.net/hook",
    ]
    entries = await world.audit()
    hosts = [entry.details["host"] for entry in entries]
    assert hosts == ["hooks.example.com", "other.example.net"]


async def test_a_secret_that_cannot_be_read_counts_a_failure_and_posts_nothing(
    world: World,
) -> None:
    """A key that does not open the stored secret is a failure the banner shows."""
    async with create_session_factory(world.h.database)() as db:
        row = await db.get(AppSetting, WEBHOOK_SECRET_KEY)
        assert row is not None
        row.value = "not-a-ciphertext"
        await db.commit()
    await world.delivery.deliver(await world.real_deploy(), "event-1")
    assert world.webhook.sent == []
    (status,) = await world.failures()
    assert status.reason == UNREADABLE_SECRET


async def test_a_transport_that_raises_something_unexpected_is_a_retryable_failure(
    world: World,
) -> None:
    """Whatever the transport raises becomes a failure with fixed words."""
    world.webhook.outcomes = [RuntimeError("boom https://hooks.example.com/token"), 200]
    await world.delivery.deliver(await world.real_deploy(), "event-1")
    assert len(world.webhook.sent) == 2
    entries = await world.audit()
    assert entries[0].details["reason"] == "The webhook request failed."
    assert "boom" not in json.dumps(entries[0].details)


async def test_the_document_and_headers_hold_no_value_secret_path_or_command(
    world: World,
) -> None:
    """A post-deploy command that printed the values leaves no trace in what is sent."""
    h = world.h
    marker = "OUTPUT-MARKER-4471"
    command = "docker compose up -d --no-deps sentinel-command-4471"
    mysql = h.seed.services.mysql.id
    editor = await h.db.scalar(
        select(Environment).where(Environment.name == EDITOR_ENVIRONMENT_NAME)
    )
    assert editor is not None
    h.db.add(ServiceHook(service_id=mysql, type=HookType.POST_DEPLOY, commands=command))
    await h.db.commit()
    values = list(EDITOR_SECRET_VALUES)
    for line in (await version_text(h.db, h.crypto, editor.id)).splitlines():
        if "=" in line and not line.startswith("#"):
            values.append(line.partition("=")[2].strip().strip("'\""))
    h.runner.result = PostDeployResult(
        PostDeployStatus.FAILED,
        exit_code=3,
        output=f"{marker} " + " ".join(values),
        reason="The command exited with code 3.",
    )
    await h.deploy(mysql, environments=[editor])
    event = h.notifier.events[-1]
    await world.delivery.deliver(event, "event-1")
    (request,) = world.webhook.sent
    text = request.body.decode() + json.dumps(dict(request.headers))
    assert marker not in text and "sentinel-command" not in text and "output" not in text
    assert str(h.roots["prod"]) not in text and ".bak" not in text
    assert not [value for value in values if len(value) >= 4 and value in text]
    assert WEBHOOK_SECRET not in text
    body = DeployWebhookBody.model_validate_json(request.body)
    assert body.status is DeployStatus.POST_DEPLOY_FAILED
    assert body.post_deploy.status is PostDeployStatus.FAILED and body.post_deploy.exit_code == 3
    entries = await world.audit()
    assert WEBHOOK_SECRET not in json.dumps([entry.details for entry in entries])


async def test_a_dry_run_sends_nothing_and_a_real_deploy_is_posted_through_the_notifier(
    harness: Harness, crypto: CryptoService, webhook: RecordingWebhook
) -> None:
    """The engine announces real runs only, and the notifier starts the webhook for them."""
    h = harness
    await configure_webhook(h.database, crypto)
    notifier = build_notifier(h.database, crypto, RecordingTransport(), webhook_transport=webhook)
    h.engine.notifier = notifier
    await h.deploy(h.seed.services.staging_web.id, dry_run=True)
    await notifier.idle()
    assert webhook.sent == []
    await h.deploy(h.seed.services.staging_web.id)
    await notifier.idle()
    (request,) = webhook.sent
    assert json.loads(request.body)["dry_run"] is False and verifies(WEBHOOK_SECRET, request)


async def test_the_command_line_posts_once_without_retries(
    harness: Harness, crypto: CryptoService, webhook: RecordingWebhook
) -> None:
    """An inline notifier delivers before it returns and gives up after one attempt."""
    h = harness
    await configure_webhook(h.database, crypto)
    webhook.outcomes = [temporary_failure(), 200]
    notifier = build_notifier(
        h.database, crypto, RecordingTransport(), webhook_transport=webhook, inline=True
    )
    h.engine.notifier = notifier
    await h.deploy(h.seed.services.staging_web.id)
    assert len(webhook.sent) == 1
    async with create_session_factory(h.database)() as db:
        (row,) = await load_failures(db)
    assert (row.channel, row.failures) == (WEBHOOK_CHANNEL, 1)


async def test_a_receiver_that_hangs_never_holds_up_mail_or_the_request(
    harness: Harness, crypto: CryptoService, webhook: RecordingWebhook
) -> None:
    """The webhook has a task of its own: mail is sent while the post is still waiting."""
    h = harness
    await configure_smtp(h.database, crypto)
    await configure_webhook(h.database, crypto)
    mail = RecordingTransport()
    webhook.hang = asyncio.Event()
    notifier = build_notifier(h.database, crypto, mail, webhook_transport=webhook)
    h.engine.notifier = notifier
    tuning = await require_extra_key(h)
    outcome = await asyncio.wait_for(
        h.deploy(h.seed.services.staging_web.id, environments=[tuning]), timeout=5
    )
    assert outcome.status is DeployStatus.BLOCKED
    for _ in range(200):
        if mail.sent and webhook.sent:
            break
        await asyncio.sleep(0.01)
    assert mail.sent and len(webhook.sent) == 1
    webhook.hang.set()
    await notifier.idle()
    assert len(webhook.sent) == 1


async def test_the_test_event_goes_through_the_saved_settings_once_and_is_audited(
    harness: Harness, crypto: CryptoService, webhook: RecordingWebhook
) -> None:
    """It works while the webhook is switched off, and a delivered one clears the banner."""
    h = harness
    await configure_webhook(h.database, crypto, enabled=False)
    actor = AuditActor.for_user(h.seed.users.superadmin)
    async with create_session_factory(h.database)() as db:
        await record_failure(db, WEBHOOK_CHANNEL, "old", utc_now(), 2)
        await db.commit()
    async with create_session_factory(h.database)() as db:
        check = await send_test_event(
            db, crypto, webhook, actor=actor, now=utc_now(), unix_time=1_760_000_000
        )
    assert check.ok and "HTTP 200" in check.message
    (request,) = webhook.sent
    assert verifies(WEBHOOK_SECRET, request)
    body = json.loads(request.body)
    assert body["event"] == "webhook.test" and body["project"]["name"] == "Test project"
    assert body["run_id"] == 0 and body["environments"][0]["name"] == ".env.test"
    async with create_session_factory(h.database)() as db:
        (entry,) = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "webhook.test"))
        ).scalars()
        assert await load_failures(db) == []
    assert entry.result is AuditResult.SUCCESS and entry.actor_name == "root"
    assert entry.details == {"host": "hooks.example.com", "http_status": 200}


async def test_a_failed_test_event_says_why_and_is_not_counted_for_the_banner(
    harness: Harness, crypto: CryptoService, webhook: RecordingWebhook
) -> None:
    """The reason is the fixed sentence, audited as a failure, and nothing is counted."""
    h = harness
    await configure_webhook(h.database, crypto)
    webhook.outcomes = [permanent_failure()]
    async with create_session_factory(h.database)() as db:
        check = await send_test_event(
            db,
            crypto,
            webhook,
            actor=AuditActor.for_user(h.seed.users.superadmin),
            now=utc_now(),
            unix_time=1_760_000_000,
        )
    assert not check.ok and check.message == "The webhook refused the request (HTTP 404)."
    async with create_session_factory(h.database)() as db:
        (entry,) = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "webhook.test"))
        ).scalars()
        assert await load_failures(db) == []
    assert entry.result is AuditResult.FAILURE
    assert entry.details["reason"] == check.message and entry.details["http_status"] == 404
    assert "path-token" not in json.dumps(entry.details)


async def test_the_test_event_needs_an_address_and_a_secret(
    harness: Harness, crypto: CryptoService, webhook: RecordingWebhook
) -> None:
    """Nothing is posted before the webhook is set up, and the unreadable secret is said."""
    h = harness
    actor = AuditActor.for_user(h.seed.users.superadmin)
    async with create_session_factory(h.database)() as db:
        first = await send_test_event(
            db, crypto, webhook, actor=actor, now=utc_now(), unix_time=1_760_000_000
        )
    assert not first.ok and first.message == NOT_SET_UP and webhook.sent == []
    await configure_webhook(h.database, crypto)
    async with create_session_factory(h.database)() as db:
        row = await db.get(AppSetting, WEBHOOK_SECRET_KEY)
        assert row is not None
        row.value = "not-a-ciphertext"
        await db.commit()
        broken = await send_test_event(
            db, crypto, webhook, actor=actor, now=utc_now(), unix_time=1_760_000_000
        )
    assert not broken.ok and broken.message == UNREADABLE_SECRET and webhook.sent == []
    async with create_session_factory(h.database)() as db:
        assert (await load_stored_settings(db)).notifications.webhook_enabled
