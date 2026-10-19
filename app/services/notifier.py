"""The notifier: turns events into mail, off the request path, with bounded retries.

Every service announces its event after its own commit. ``Notifications`` implements the deploy,
backup and settings seams and the event seam; in the web server each event only starts a task on
the event loop and returns, so a slow or failing mail server never delays or fails the action
that caused the event. The task reads the SMTP settings and the switches when it runs (so a save
applies to the next event), finds the recipients, builds one message per recipient and sends them
with a few retries. A failure is logged with a fixed reason and counted in the database for the
banner. At most ``max_pending`` events wait at once; more are dropped and counted.

A finished deploy is also posted to the outbound webhook (``WebhookDelivery``), in a task of its
own so a slow receiver never holds up mail and the other way round.

The command line process cannot leave work behind (its loop ends with the command), so there the
notifier runs inline: it delivers before returning, without retries, under one total time limit.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import CryptoError, CryptoService
from app.core.display_time import DisplayTime
from app.core.log import get_logger
from app.models import Deployment, Project, Service
from app.models.types import utc_now
from app.services.app_settings import (
    SMTP_PASSWORD_KEY,
    SmtpSettings,
    StoredSettings,
    load_stored_settings,
    read_secret,
)
from app.services.backup_events import BackupFinished
from app.services.deploy_events import DeployFinished
from app.services.mailer import FastapiMailTransport, MailFailure, MailTransport, OutgoingMail
from app.services.notify_catalog import EventKind, is_enabled, kind_of, recipients_of
from app.services.notify_events import FileChanged, NotificationEvent
from app.services.notify_messages import (
    ItemNames,
    MailContent,
    MessageContext,
    describe_event,
    render_mail,
)
from app.services.notify_recipients import ProjectItem, Recipient, resolve_recipients
from app.services.notify_status import EMAIL_CHANNEL, clear_failures, record_failure
from app.services.settings_events import SettingsChanged
from app.services.webhook import WebhookTransport
from app.services.webhook_body import new_event_id
from app.services.webhook_delivery import WebhookDelivery, WebhookRetry

log = get_logger("notifier")

MAX_PENDING = 500
CONCURRENT_EVENTS = 4
INLINE_TIMEOUT_SECONDS = 30.0
CLOSE_GRACE_SECONDS = 5.0
GENERIC_FAILURE = "The mail could not be sent."
DROPPED_REASON = "Notifications were dropped because too many were waiting."
UNREADABLE_PASSWORD = "The stored SMTP password cannot be read with the current key."  # noqa: S105
UNKNOWN_NAME = "(unknown)"

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    """The waits between the attempts to send one mail: one more attempt for each wait."""

    delays: tuple[float, ...] = (5.0, 30.0)


class Notifications:
    """Delivers notification mail for every event the application announces."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: CryptoService,
        *,
        base_url: str,
        transport: MailTransport | None = None,
        retry: RetryPolicy | None = None,
        webhook_transport: WebhookTransport | None = None,
        webhook_retry: WebhookRetry | None = None,
        sleep: Sleep = asyncio.sleep,
        inline: bool = False,
        max_pending: int = MAX_PENDING,
    ) -> None:
        """Hold what delivering needs. ``sleep`` and ``retry`` are fields so tests need no wait."""
        self._session_factory = session_factory
        self._crypto = crypto
        self._base_url = base_url
        self._transport = transport or FastapiMailTransport()
        self._retry = retry or (RetryPolicy(()) if inline else RetryPolicy())
        self._webhook = WebhookDelivery(
            session_factory,
            crypto,
            transport=webhook_transport,
            retry=webhook_retry or (WebhookRetry(()) if inline else WebhookRetry()),
            sleep=sleep,
        )
        self._sleep = sleep
        self._inline = inline
        self._max_pending = max_pending
        self._pending: set[asyncio.Task[None]] = set()
        self._limit = asyncio.Semaphore(CONCURRENT_EVENTS)
        self._dropped = 0
        self._recorder: asyncio.Task[None] | None = None

    async def notify(self, event: NotificationEvent) -> None:
        """Take an event: inline for the command line, else start its task and return at once."""
        if kind_of(event) is None:
            return
        if self._inline:
            await self._run_inline(self._jobs(event))
            return
        if len(self._pending) >= self._max_pending:
            self._dropped += 1
            self._start_recorder()
            return
        for job in self._jobs(event):
            task = asyncio.get_running_loop().create_task(job)
            self._pending.add(task)
            task.add_done_callback(self._finished)

    def _jobs(self, event: NotificationEvent) -> list[Coroutine[Any, Any, None]]:
        """The deliveries an event starts: its mail, and for a finished deploy the webhook."""
        jobs = [self._run(event, utc_now())]
        if isinstance(event, DeployFinished):
            jobs.append(self._webhook.deliver(event, new_event_id()))
        return jobs

    async def deploy_finished(self, event: DeployFinished) -> None:
        """The deploy seam."""
        await self.notify(event)

    async def settings_changed(self, event: SettingsChanged) -> None:
        """The settings seam."""
        await self.notify(event)

    async def backup_finished(self, event: BackupFinished) -> None:
        """The backup seam."""
        await self.notify(event)

    async def idle(self) -> None:
        """Wait until every event taken so far has been delivered or has failed for good."""
        while self._pending or self._recorder is not None:
            pending = [*self._pending, *([self._recorder] if self._recorder else [])]
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        """Give events in flight a moment to finish, then cancel what is left."""
        pending = [*self._pending, *([self._recorder] if self._recorder else [])]
        if pending:
            _done, left = await asyncio.wait(pending, timeout=CLOSE_GRACE_SECONDS)
            for task in left:
                task.cancel()
            await asyncio.gather(*left, return_exceptions=True)

    def _finished(self, task: asyncio.Task[None]) -> None:
        """Forget a finished task and log a failure nobody caught."""
        self._pending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("notification task failed: %s", type(task.exception()).__name__)

    def _start_recorder(self) -> None:
        """Make sure one task is counting the dropped events."""
        if self._recorder is None:
            self._recorder = asyncio.get_running_loop().create_task(self._record_dropped())

    async def _record_dropped(self) -> None:
        """Count the events dropped so far as failures; one task serves a whole flood."""
        try:
            while self._dropped:
                count, self._dropped = self._dropped, 0
                log.warning("%d notifications dropped, too many waiting", count)
                await self._count_failure(DROPPED_REASON, count)
        finally:
            self._recorder = None

    async def _run_inline(self, jobs: list[Coroutine[Any, Any, None]]) -> None:
        """Deliver in the caller's task, all deliveries together under one limit, never raising."""
        try:
            async with asyncio.timeout(INLINE_TIMEOUT_SECONDS):
                await asyncio.gather(*jobs)
        except Exception as error:
            log.error("notification failed: %s", type(error).__name__)

    async def _run(self, event: NotificationEvent, now: datetime) -> None:
        """Plan the messages of one event and send them. Never raises for a delivery problem.

        The failures of an event are counted together; the count starts over only when every
        message of an event was sent, so a recipient that never gets their mail keeps it showing.
        """
        try:
            async with self._limit:
                planned = await self._plan(event, now)
                if planned is None:
                    return
                smtp, password, messages = planned
                reasons: list[str] = []
                for mail in messages:
                    failure = await self._send(smtp, password, mail)
                    if failure is not None:
                        log.warning("notification mail not sent: %s", failure.reason)
                        reasons.append(failure.reason)
                if reasons:
                    await self._count_failure(reasons[-1], len(reasons))
                else:
                    await self._count_success()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error("notification failed: %s", type(error).__name__)

    async def _plan(
        self, event: NotificationEvent, now: datetime
    ) -> tuple[SmtpSettings, str | None, list[OutgoingMail]] | None:
        """The SMTP settings, the password and the messages to send, None when nothing is due.

        Nothing is due when the kind has no mail, mail is not set up, the switch is off or there
        is nobody to tell. The database session is closed before anything is sent.
        """
        kind = kind_of(event)
        if kind is None:
            return None
        async with self._session_factory() as db:
            stored = await load_stored_settings(db)
            if not stored.smtp.configured or not is_enabled(stored.notifications, kind):
                return None
            messages = await self._messages(db, event, kind, stored, now)
            if not messages:
                return None
            try:
                password = await read_secret(db, self._crypto, SMTP_PASSWORD_KEY)
            except CryptoError:
                await self._count_failure(UNREADABLE_PASSWORD)
                return None
        return stored.smtp, password, messages

    async def _messages(
        self,
        db: AsyncSession,
        event: NotificationEvent,
        kind: EventKind,
        stored: StoredSettings,
        now: datetime,
    ) -> list[OutgoingMail]:
        """One message per recipient, each naming only the files that recipient may see."""
        context = MessageContext(
            self._base_url,
            DisplayTime(stored.general.time_zone, stored.general.date_format),
            now,
        )
        item = _project_item(event)
        names = await _item_names(db, item) if item is not None else None
        recipients = await resolve_recipients(db, recipients_of(stored.notifications, kind), item)
        return _render_all(event, context, names, recipients)

    async def _send(
        self, smtp: SmtpSettings, password: str | None, mail: OutgoingMail
    ) -> MailFailure | None:
        """Send one mail with the retry policy; None on success, else the final failure.

        Each retry reads the SMTP settings again, so a server that was fixed while the mail
        waited gets it. When mail has been switched off in the meantime the mail is dropped.
        """
        attempt = 0
        while True:
            failure = await self._attempt(smtp, password, mail)
            if failure is None:
                return None
            if not failure.retryable or attempt >= len(self._retry.delays):
                return failure
            await self._sleep(self._retry.delays[attempt])
            attempt += 1
            current = await self._current_settings()
            if current is None:
                return None
            smtp, password = current

    async def _current_settings(self) -> tuple[SmtpSettings, str | None] | None:
        """The stored SMTP settings and password now, None when mail is off or unreadable."""
        try:
            async with self._session_factory() as db:
                smtp = (await load_stored_settings(db)).smtp
                if not smtp.configured:
                    return None
                return smtp, await read_secret(db, self._crypto, SMTP_PASSWORD_KEY)
        except Exception as error:
            log.error("could not read the SMTP settings for a retry: %s", type(error).__name__)
            return None

    async def _attempt(
        self, smtp: SmtpSettings, password: str | None, mail: OutgoingMail
    ) -> MailFailure | None:
        """One try to send the mail; any error becomes a failure with a fixed reason."""
        try:
            await self._transport.send(smtp, password, mail)
        except MailFailure as failure:
            return failure
        except Exception as error:
            log.error("mail transport raised %s", type(error).__name__)
            return MailFailure(GENERIC_FAILURE, retryable=True)
        return None

    async def _count_failure(self, reason: str, count: int = 1) -> None:
        """Count a failed delivery for the banner. A failure to count is only logged."""
        try:
            async with self._session_factory() as db:
                await record_failure(db, EMAIL_CHANNEL, reason, utc_now(), count)
                await db.commit()
        except Exception as error:
            log.error("could not count a failed notification: %s", type(error).__name__)

    async def _count_success(self) -> None:
        """A delivery worked, so earlier failures no longer need attention."""
        try:
            async with self._session_factory() as db:
                await clear_failures(db, EMAIL_CHANNEL)
                await db.commit()
        except Exception as error:
            log.error("could not clear the notification status: %s", type(error).__name__)


def _project_item(event: NotificationEvent) -> ProjectItem | None:
    """What a project event is about, None for an event about the whole system."""
    if isinstance(event, DeployFinished):
        return ProjectItem(
            event.project_id,
            event.deployment_id,
            event.service_id,
            tuple(item.environment_id for item in event.environments),
        )
    if isinstance(event, FileChanged):
        return ProjectItem(
            event.project_id, event.deployment_id, event.service_id, (event.environment_id,)
        )
    return None


async def _item_names(db: AsyncSession, item: ProjectItem) -> ItemNames:
    """The names of the project, deployment and service, or a placeholder for one that is gone."""
    project = await db.get(Project, item.project_id)
    deployment = await db.get(Deployment, item.deployment_id)
    service = await db.get(Service, item.service_id)
    return ItemNames(
        project.name if project else UNKNOWN_NAME,
        deployment.name if deployment else UNKNOWN_NAME,
        service.name if service else UNKNOWN_NAME,
    )


def _render_all(
    event: NotificationEvent,
    context: MessageContext,
    names: ItemNames | None,
    recipients: list[Recipient],
) -> list[OutgoingMail]:
    """The message for each recipient, built from the files that recipient may see."""
    messages: list[OutgoingMail] = []
    for recipient in recipients:
        content: MailContent | None = describe_event(
            event, context, names, recipient.environment_ids
        )
        if content is not None:
            messages.append(render_mail(content, recipient.address))
    return messages
