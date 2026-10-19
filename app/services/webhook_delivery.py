"""Delivering deploy results to the outbound webhook, and the test event.

``WebhookDelivery`` takes one finished deploy and posts its document to the stored address with
a few retries and growing waits. It reads the settings when it starts and again before every
retry, so a webhook that was switched off or fixed while an event waited is honoured. Every
attempt is audited with the host (never the whole address, which can hold a token), the attempt
number, the HTTP status and a fixed reason, and never the secret. An event whose last attempt
failed is counted once in the delivery health table for the banner; an event that was delivered
clears the count. Nothing here raises for a delivery problem, and nothing waits on the request
that caused the event.

The test event goes through the same stored settings, once, and answers how it went.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import telemetry
from app.core.crypto import CryptoError, CryptoService
from app.core.display_time import DisplayTime
from app.core.log import get_logger
from app.models import AuditResult
from app.models.types import utc_now
from app.services.app_settings import WEBHOOK_SECRET_KEY, load_stored_settings, read_secret
from app.services.audit import AuditActor, record_committed_audit
from app.services.deploy_events import DeployFinished
from app.services.notify_status import WEBHOOK_CHANNEL, clear_failures, record_failure
from app.services.webhook import (
    GENERIC_FAILURE,
    HttpxWebhookTransport,
    WebhookFailure,
    WebhookTransport,
    host_of,
    signed_headers,
)
from app.services.webhook_body import deploy_document, new_event_id, sample_document

log = get_logger("webhook")

WEBHOOK_ACTOR = "webhook"
DELIVER_ACTION = "webhook.deliver"
TEST_ACTION = "webhook.test"
CONCURRENT_ATTEMPTS = 4
UNREADABLE_SECRET = "The stored webhook secret cannot be read with the current key."  # noqa: S105
NOT_SET_UP = "Save a webhook address and a shared secret first."

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class WebhookRetry:
    """The waits between the attempts to deliver one event: one more attempt for each wait."""

    delays: tuple[float, ...] = (5.0, 30.0, 120.0)


@dataclass(frozen=True)
class WebhookTarget:
    """Where to post and the secret to sign with, read from the stored settings."""

    url: str
    secret: str


@dataclass(frozen=True)
class WebhookCheck:
    """The result of a test event: whether it was delivered, and a sentence to show."""

    ok: bool
    message: str


class WebhookDelivery:
    """Posts the documents of finished deploys to the webhook, in the background."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: CryptoService,
        *,
        transport: WebhookTransport | None = None,
        retry: WebhookRetry | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.time,
    ) -> None:
        """Hold what delivering needs. ``sleep`` and ``clock`` are fields so tests need no wait."""
        self._session_factory = session_factory
        self._crypto = crypto
        self._transport = transport or HttpxWebhookTransport()
        self._retry = retry or WebhookRetry()
        self._sleep = sleep
        self._clock = clock
        self._limit = asyncio.Semaphore(CONCURRENT_ATTEMPTS)

    async def deliver(self, event: DeployFinished, event_id: str) -> None:
        """Deliver the event if the webhook is on. Never raises for a delivery problem."""
        try:
            target = await self._target()
            if target is None:
                return
            async with self._session_factory() as db:
                body = await deploy_document(db, event, event_id)
            if body is None:
                return
            await self._post_with_retries(event, event_id, target, body)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error("webhook delivery failed: %s", type(error).__name__)

    async def _target(self) -> WebhookTarget | None:
        """The address and secret now, None when the webhook is off, not set up or unreadable."""
        async with self._session_factory() as db:
            notifications = (await load_stored_settings(db)).notifications
            if not notifications.webhook_enabled or not notifications.webhook_url:
                return None
            try:
                secret = await read_secret(db, self._crypto, WEBHOOK_SECRET_KEY)
            except CryptoError:
                await self._count_failure(UNREADABLE_SECRET)
                return None
        if not secret:
            return None
        return WebhookTarget(notifications.webhook_url, secret)

    async def _post_with_retries(
        self, event: DeployFinished, event_id: str, target: WebhookTarget, body: bytes
    ) -> None:
        """Post until it is accepted, refused for good or the waits are used up."""
        attempt = 1
        while True:
            status, failure = await self._attempt(target, body, attempt)
            will_retry = (
                failure is not None and failure.retryable and attempt <= len(self._retry.delays)
            )
            await self._audit_attempt(event, event_id, target, attempt, status, failure, will_retry)
            if failure is None:
                await self._count_success()
                return
            log.warning("webhook event not delivered (attempt %d): %s", attempt, failure.reason)
            if not will_retry:
                await self._count_failure(failure.reason)
                return
            await self._sleep(self._retry.delays[attempt - 1])
            attempt += 1
            current = await self._target()
            if current is None:
                return
            target = current

    async def _attempt(
        self, target: WebhookTarget, body: bytes, attempt: int
    ) -> tuple[int | None, WebhookFailure | None]:
        """One post with a fresh timestamp and signature: the status, or the failure.

        The post is a span with the attempt number, the result and the HTTP status. The address
        is a secret and is never attached, not even its host.
        """
        headers = signed_headers(target.secret, body, int(self._clock()))
        async with self._limit:
            with telemetry.span(
                telemetry.WEBHOOK_DELIVER_SPAN, {telemetry.ATTEMPT: attempt}
            ) as active:
                status, failure = await self._post(target, headers, body)
                telemetry.annotate(
                    active,
                    {
                        telemetry.RESULT: "failed" if failure else "delivered",
                        telemetry.HTTP_STATUS: status,
                        telemetry.RETRYABLE: failure.retryable if failure else None,
                    },
                )
                return status, failure

    async def _post(
        self, target: WebhookTarget, headers: Mapping[str, str], body: bytes
    ) -> tuple[int | None, WebhookFailure | None]:
        """Post once through the transport: the status, or the failure."""
        try:
            return await self._transport.post(target.url, headers, body), None
        except WebhookFailure as failure:
            return failure.status, failure
        except Exception as error:
            log.error("webhook transport raised %s", type(error).__name__)
            return None, WebhookFailure(GENERIC_FAILURE, retryable=True)

    async def _audit_attempt(
        self,
        event: DeployFinished,
        event_id: str,
        target: WebhookTarget,
        attempt: int,
        status: int | None,
        failure: WebhookFailure | None,
        will_retry: bool,
    ) -> None:
        """Audit one attempt. A failure to audit is only logged."""
        details: dict[str, object] = {
            "event_id": event_id,
            "run_id": event.run_id,
            "service": event.service_name,
            "attempt": attempt,
            "host": host_of(target.url),
        }
        if status is not None:
            details["http_status"] = status
        if failure is not None:
            details["reason"] = failure.reason
            details["will_retry"] = will_retry
        try:
            async with self._session_factory() as db:
                await record_committed_audit(
                    db,
                    AuditActor.system(WEBHOOK_ACTOR),
                    action=DELIVER_ACTION,
                    result=AuditResult.FAILURE if failure else AuditResult.SUCCESS,
                    target_type="webhook",
                    target_name=host_of(target.url),
                    details=details,
                )
        except Exception as error:
            log.error("could not audit a webhook attempt: %s", type(error).__name__)

    async def _count_failure(self, reason: str) -> None:
        """Count one undelivered event for the banner. A failure to count is only logged."""
        try:
            async with self._session_factory() as db:
                await record_failure(db, WEBHOOK_CHANNEL, reason, utc_now())
                await db.commit()
        except Exception as error:
            log.error("could not count a failed webhook event: %s", type(error).__name__)

    async def _count_success(self) -> None:
        """An event was delivered, so earlier failures no longer need attention."""
        try:
            async with self._session_factory() as db:
                await clear_failures(db, WEBHOOK_CHANNEL)
                await db.commit()
        except Exception as error:
            log.error("could not clear the webhook status: %s", type(error).__name__)


async def send_test_event(
    db: AsyncSession,
    crypto: CryptoService,
    transport: WebhookTransport,
    *,
    actor: AuditActor,
    now: datetime,
    unix_time: int,
) -> WebhookCheck:
    """Post the test event once through the stored settings and say how it went.

    It works while the webhook is switched off, so a setup can be tried before it is enabled.
    The result is audited as ``webhook.test`` with the host, the HTTP status and a fixed reason,
    never the secret or the whole address. A delivered test also clears the failures counted for
    the banner.
    """
    stored = await load_stored_settings(db)
    display = DisplayTime(stored.general.time_zone, stored.general.date_format)
    url = stored.notifications.webhook_url
    try:
        secret = await read_secret(db, crypto, WEBHOOK_SECRET_KEY)
    except CryptoError:
        await _audit_test(db, actor, url, None, UNREADABLE_SECRET)
        return WebhookCheck(False, UNREADABLE_SECRET)
    if not url or not secret:
        return WebhookCheck(False, NOT_SET_UP)
    body = sample_document(new_event_id(), now)
    try:
        status = await transport.post(url, signed_headers(secret, body, unix_time), body)
    except WebhookFailure as failure:
        await _audit_test(db, actor, url, failure.status, failure.reason)
        return WebhookCheck(False, failure.reason)
    except Exception as error:
        log.error("webhook transport raised %s", type(error).__name__)
        await _audit_test(db, actor, url, None, GENERIC_FAILURE)
        return WebhookCheck(False, GENERIC_FAILURE)
    await clear_failures(db, WEBHOOK_CHANNEL)
    await _audit_test(db, actor, url, status, None)
    return WebhookCheck(True, f"Test event delivered {display.clock(now)} (HTTP {status})")


async def _audit_test(
    db: AsyncSession, actor: AuditActor, url: str, status: int | None, reason: str | None
) -> None:
    """Audit a test event and commit."""
    details: dict[str, object] = {"host": host_of(url)}
    if status is not None:
        details["http_status"] = status
    if reason is not None:
        details["reason"] = reason
    await record_committed_audit(
        db,
        actor,
        action=TEST_ACTION,
        result=AuditResult.FAILURE if reason else AuditResult.SUCCESS,
        target_type="settings",
        target_name="webhook",
        details=details,
    )
