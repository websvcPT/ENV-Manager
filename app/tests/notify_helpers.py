"""Helpers shared by the notification tests: fake mail transports, recorded events and the
stored SMTP settings of a test database."""

import asyncio
import email
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from email.message import Message
from typing import Any, ClassVar

import aiosmtplib
import pytest
from fastapi import FastAPI
from fastapi_mail import FastMail
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import Deployment, Environment, Service
from app.services.app_settings import (
    SMTP_PASSWORD_KEY,
    NotificationSettings,
    SmtpSettings,
    load_stored_settings,
    save_section,
    write_secret,
)
from app.services.audit import AuditActor
from app.services.mailer import MailFailure, OutgoingMail, build_message, connection_config
from app.services.notifier import Notifications, RetryPolicy
from app.services.notify_events import NotificationEvent

SMTP_PASSWORD = "smtp-password-sentinel"
SMTP = SmtpSettings("smtp.example.com", 587, "starttls", "mailer", "env-manager@example.com")
FAST = RetryPolicy((0.0, 0.0))


@dataclass
class SentMail:
    """One call of the transport: the settings and password it was given, and the mail."""

    smtp: SmtpSettings
    password: str | None
    mail: OutgoingMail


@dataclass
class RecordingTransport:
    """A mail transport that records every mail and can fail or hang on demand.

    ``failures`` are raised one per call, oldest first, before anything is recorded; an exhausted
    list means the call works. ``hang`` makes every call wait for that event, so a test can prove
    nothing waits for mail.
    """

    sent: list[SentMail] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)
    hang: asyncio.Event | None = None
    calls: int = 0

    async def send(self, smtp: SmtpSettings, password: str | None, mail: OutgoingMail) -> None:
        """Record the mail, or fail the way the next failure says."""
        self.calls += 1
        if self.hang is not None:
            await self.hang.wait()
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(SentMail(smtp, password, mail))

    def to(self) -> list[str]:
        """The recipients of the mails sent, in order."""
        return [item.mail.to for item in self.sent]


@dataclass
class RecordingEvents:
    """A notifier that remembers every event of every kind it is given."""

    events: list[NotificationEvent] = field(default_factory=list)

    async def notify(self, event: NotificationEvent) -> None:
        """Remember the event."""
        self.events.append(event)

    async def deploy_finished(self, event: NotificationEvent) -> None:
        """Remember the event."""
        self.events.append(event)

    async def settings_changed(self, event: NotificationEvent) -> None:
        """Remember the event."""
        self.events.append(event)

    async def backup_finished(self, event: NotificationEvent) -> None:
        """Remember the event."""
        self.events.append(event)


def temporary_failure() -> MailFailure:
    """A failure that trying again can fix."""
    return MailFailure("The mail server did not answer in time.", retryable=True)


def permanent_failure() -> MailFailure:
    """A failure that trying again cannot fix."""
    return MailFailure("The mail server refused the username or password.", retryable=False)


async def configure_smtp(
    engine: AsyncEngine,
    crypto: CryptoService,
    smtp: SmtpSettings = SMTP,
    *,
    notifications: NotificationSettings | None = None,
    password: str | None = SMTP_PASSWORD,
) -> None:
    """Store SMTP settings, its password and optionally the notification matrix."""
    async with create_session_factory(engine)() as db:
        actor = AuditActor.cli()
        await save_section(db, "smtp", smtp, actor=actor)
        if password is not None:
            await write_secret(db, crypto, SMTP_PASSWORD_KEY, password, actor=actor)
        if notifications is not None:
            await save_section(db, "notifications", notifications, actor=actor)
        await db.commit()


async def switch(engine: AsyncEngine, **values: object) -> None:
    """Change fields of the stored notification matrix, keeping the rest as it is."""
    async with create_session_factory(engine)() as db:
        current = (await load_stored_settings(db)).notifications
        changed = replace(current, **values)  # type: ignore[arg-type]
        await save_section(db, "notifications", changed, actor=AuditActor.cli())
        await db.commit()


def build_notifier(
    engine: AsyncEngine,
    crypto: CryptoService,
    transport: RecordingTransport,
    *,
    retry: RetryPolicy | None = FAST,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    **options: object,
) -> Notifications:
    """A notifier on the test database that sends through the recording transport."""

    async def no_wait(_seconds: float) -> None:
        return None

    return Notifications(
        create_session_factory(engine),
        crypto,
        base_url="https://env.example.com",
        transport=transport,
        retry=retry,
        sleep=sleep or no_wait,
        **options,  # type: ignore[arg-type]
    )


async def mime_of(mail: OutgoingMail, smtp: SmtpSettings = SMTP) -> Message:
    """The message exactly as fastapi-mail builds it from the mail, before it goes on the wire."""
    built = await FastMail(connection_config(smtp, "x")).get_message(build_message(mail))
    assert isinstance(built, Message)
    return built


def decoded_text(message: Message) -> str:
    """Every header and every part of a message with the transfer encoding undone.

    A message whose parts are base64 holds none of its words in ``as_string()``, so a check that
    something is absent must look at this instead.
    """
    chunks = [f"{name}: {value}" for name, value in message.items()]
    for part in message.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        chunks.append(payload.decode(part.get_content_charset() or "utf-8"))
    return "\n".join(chunks)


def parts_of(message: Message) -> dict[str, str]:
    """The text parts of a message by content type, decoded."""
    found: dict[str, str] = {}
    for part in message.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        found[part.get_content_type()] = payload.decode(part.get_content_charset() or "utf-8")
    return found


def parse(raw: bytes) -> Message:
    """A message read back from the bytes a server received."""
    return email.message_from_bytes(raw)


class FakeSMTP:
    """The SMTP client of aiosmtplib as fastapi-mail drives it, recording what it was asked.

    Class attributes pick how every instance behaves: ``connect_error``, ``login_error`` and
    ``send_error`` are raised by the matching call, and ``hang`` makes the connection wait.
    """

    instances: ClassVar[list[FakeSMTP]] = []
    connect_error: ClassVar[Exception | None] = None
    login_error: ClassVar[Exception | None] = None
    send_error: ClassVar[Exception | None] = None
    hang: ClassVar[bool] = False

    def __init__(self, **arguments: Any) -> None:
        """Remember how the client was configured."""
        self.arguments: dict[str, Any] = arguments
        self.logins: list[tuple[str, str]] = []
        self.messages: list[Message] = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    async def connect(self) -> None:
        """Connect, or hang or fail as the test says."""
        if FakeSMTP.hang:
            await asyncio.sleep(3600)
        if FakeSMTP.connect_error is not None:
            raise FakeSMTP.connect_error

    async def login(self, username: str, password: str) -> None:
        """Record the login, or refuse it."""
        if FakeSMTP.login_error is not None:
            raise FakeSMTP.login_error
        self.logins.append((username, password))

    async def send_message(self, message: Message) -> None:
        """Record the message, or refuse it."""
        if FakeSMTP.send_error is not None:
            raise FakeSMTP.send_error
        self.messages.append(message)

    async def quit(self) -> None:
        """Note that the session was closed."""
        self.quit_called = True


def install_fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    """Put the fake client in place of aiosmtplib's, reset for the test."""
    FakeSMTP.instances = []
    FakeSMTP.connect_error = FakeSMTP.login_error = FakeSMTP.send_error = None
    FakeSMTP.hang = False
    monkeypatch.setattr(aiosmtplib, "SMTP", FakeSMTP)
    return FakeSMTP


async def file_event_ids(web_app: FastAPI, environment_id: int) -> tuple[int, int, int, str]:
    """The project, deployment and service ids and the name of an environment."""
    async with web_app.state.session_factory() as db:
        row = (
            await db.execute(
                select(Deployment.project_id, Deployment.id, Service.id, Environment.name)
                .join(Service, Service.deployment_id == Deployment.id)
                .join(Environment, Environment.service_id == Service.id)
                .where(Environment.id == environment_id)
            )
        ).one()
    return row[0], row[1], row[2], row[3]
