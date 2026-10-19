"""Sending one mail through fastapi-mail, configured from the SMTP settings at send time.

Nothing is read at import time: every send builds its connection settings from the stored SMTP
section and the decrypted password it is given, so a saved change applies to the next mail. A
failure is turned into a ``MailFailure`` that carries fixed words and whether trying again can
help. The text of the exception is never kept, because a server's answer or a library message
could repeat what was sent, including credentials.

The library's models refuse addresses on reserved names such as ``.local``, which private networks
use, so the settings and the message are built without its validation; every address is checked
by ``valid_address`` (when it is saved and again before it is used) instead.
"""

import asyncio
import re
import ssl
from dataclasses import dataclass
from typing import Protocol

import aiosmtplib
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
from fastapi_mail.schemas import MultipartSubtypeEnum
from opentelemetry.trace import Span
from pydantic import SecretStr

from app.core import telemetry
from app.services.app_settings import SmtpSettings

SENDER_NAME = "ENV Manager"
SEND_TIMEOUT_SECONDS = 10
TOTAL_TIMEOUT_SECONDS = 45
MAX_ADDRESS_LENGTH = 254
TLS_MODES: dict[str, str] = {"none": "None", "starttls": "STARTTLS", "tls": "SSL/TLS"}
_ADDRESS = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


def valid_address(text: str) -> bool:
    """Whether the text is one plain address: ASCII, no name, no spaces or separators."""
    return len(text) <= MAX_ADDRESS_LENGTH and _ADDRESS.fullmatch(text) is not None


@dataclass(frozen=True)
class OutgoingMail:
    """One message for one recipient: a plain text part and an HTML alternative."""

    to: str
    subject: str
    text: str
    html: str


class MailFailure(Exception):
    """A mail was not sent. ``reason`` is a fixed sentence that is safe to show and log.

    ``retryable`` says whether the same mail could work later (a server that is down) or never
    will (a refused login or address).
    """

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class MailTransport(Protocol):
    """Sends a mail with the given settings; raises ``MailFailure`` when it cannot."""

    async def send(self, smtp: SmtpSettings, password: str | None, mail: OutgoingMail) -> None:
        """Deliver one mail to the server."""
        ...


def connection_config(smtp: SmtpSettings, password: str | None) -> ConnectionConfig:
    """The fastapi-mail connection settings for the stored SMTP section.

    Every field is given, because the settings class would otherwise fill the ones left out from
    ``MAIL_*`` environment variables of the process.
    """
    return ConnectionConfig.model_construct(
        MAIL_USERNAME=smtp.username,
        MAIL_PASSWORD=SecretStr(password or ""),
        MAIL_PORT=smtp.port,
        MAIL_SERVER=smtp.host,
        MAIL_STARTTLS=smtp.tls_mode == "starttls",
        MAIL_SSL_TLS=smtp.tls_mode == "tls",
        MAIL_DEBUG=0,
        MAIL_FROM=smtp.from_address,
        MAIL_FROM_NAME=SENDER_NAME,
        TEMPLATE_FOLDER=None,
        SUPPRESS_SEND=0,
        USE_CREDENTIALS=bool(smtp.username),
        VALIDATE_CERTS=True,
        TIMEOUT=SEND_TIMEOUT_SECONDS,
        LOCAL_HOSTNAME=None,
        CERT_BUNDLE=None,
    )


def build_message(mail: OutgoingMail) -> MessageSchema:
    """The fastapi-mail message: plain text first, the HTML part as its alternative."""
    return MessageSchema.model_construct(
        recipients=[mail.to],
        attachments=[],
        subject=mail.subject,
        body=mail.text,
        alternative_body=mail.html,
        template_body=None,
        cc=[],
        bcc=[],
        reply_to=[],
        from_email=None,
        from_name=None,
        charset="utf-8",
        subtype=MessageType.plain,
        multipart_subtype=MultipartSubtypeEnum.alternative,
        headers=None,
    )


def _chain(error: BaseException) -> list[BaseException]:
    """The error and the errors it was raised from, outermost first."""
    found: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in found and len(found) < 6:
        found.append(current)
        current = current.__cause__ or current.__context__
    return found


_REFUSALS: tuple[tuple[type[BaseException], str, bool], ...] = (
    (
        aiosmtplib.SMTPAuthenticationError,
        "The mail server refused the username or password.",
        False,
    ),
    (aiosmtplib.SMTPSenderRefused, "The mail server refused the from address.", False),
    (aiosmtplib.SMTPRecipientsRefused, "The mail server refused the recipient address.", False),
    (aiosmtplib.SMTPRecipientRefused, "The mail server refused the recipient address.", False),
    (ssl.SSLError, "The TLS connection to the mail server failed.", False),
    (aiosmtplib.SMTPNotSupported, "The mail server does not support the chosen TLS mode.", False),
    (TimeoutError, "The mail server did not answer in time.", True),
    (aiosmtplib.SMTPServerDisconnected, "The mail server closed the connection.", True),
    (OSError, "Could not connect to the mail server.", True),
)


def classify(error: BaseException) -> MailFailure:
    """The failure for an exception raised while sending, in fixed words.

    The first kind that appears along the chain of causes decides, the most specific first.
    """
    chain = _chain(error)
    for kind, reason, retryable in _REFUSALS:
        if any(isinstance(item, kind) for item in chain):
            return MailFailure(reason, retryable=retryable)
    for item in chain:
        if isinstance(item, aiosmtplib.SMTPResponseException):
            return MailFailure("The mail server refused the message.", retryable=item.code < 500)
    return MailFailure("The mail could not be sent.", retryable=True)


class FastapiMailTransport:
    """The real transport: fastapi-mail over aiosmtplib."""

    async def send(self, smtp: SmtpSettings, password: str | None, mail: OutgoingMail) -> None:
        """Send one mail, with a bound on the whole attempt.

        Raises ``MailFailure`` for any problem, with a fixed reason. The send runs in a span that
        carries the TLS mode, the result and whether trying again can help; the server, the
        recipient, the subject and the text of an error are never attached.
        """
        attributes = {telemetry.SMTP_TLS_MODE: smtp.tls_mode}
        with telemetry.span(telemetry.SMTP_SEND_SPAN, attributes) as active:
            if not valid_address(mail.to) or not valid_address(smtp.from_address):
                self._mark_failed(active, retryable=False)
                raise MailFailure("An address in the mail is not valid.", retryable=False)
            failure: MailFailure | None = None
            try:
                async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                    message = build_message(mail)
                    await FastMail(connection_config(smtp, password)).send_message(message)
            except Exception as error:
                failure = classify(error)
            if failure is not None:
                self._mark_failed(active, retryable=failure.retryable)
                raise failure
            telemetry.annotate(active, {telemetry.RESULT: "sent"})

    @staticmethod
    def _mark_failed(active: Span, *, retryable: bool) -> None:
        """Put the failed result on the span."""
        telemetry.annotate(active, {telemetry.RESULT: "failed", telemetry.RETRYABLE: retryable})
