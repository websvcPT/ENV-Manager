"""The SMTP settings: checking the form, saving it and sending the test mail.

The password is a secret. It is never shown back: the form carries it only when someone types a
new one, an empty field keeps the stored password, and a separate choice removes it. Saving the
section starts delivery health afresh, since a failure counted under the old settings says
nothing about the new ones. The test mail goes through the stored settings, exactly as a
notification does, so a working test means notifications work.
"""

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.core.display_time import DisplayTime
from app.models import AuditResult
from app.services.app_settings import (
    SMTP_PASSWORD_KEY,
    SettingChange,
    SmtpSettings,
    clear_secret,
    load_stored_settings,
    read_secret,
    save_section,
    write_secret,
)
from app.services.audit import AuditActor, record_committed_audit
from app.services.mailer import TLS_MODES, MailFailure, MailTransport, valid_address
from app.services.notify_messages import MessageContext, probe_mail_content, render_mail
from app.services.notify_status import EMAIL_CHANNEL, clear_failures

MAX_HOST_LENGTH = 253
MAX_USERNAME_LENGTH = 255
MAX_PASSWORD_LENGTH = 1024
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
NOT_CONFIGURED_MESSAGE = "SMTP is not configured. Save a server and a from address first."
UNREADABLE_MESSAGE = "The stored password cannot be read with the current key."


@dataclass(frozen=True)
class SmtpForm:
    """The SMTP settings as typed. ``password`` is empty to keep the stored one and
    ``remove_password`` asks to delete it."""

    host: str
    port: str
    tls_mode: str
    username: str
    password: str
    from_address: str
    remove_password: bool = False

    @classmethod
    def from_settings(cls, settings: SmtpSettings) -> SmtpForm:
        """The form showing the stored settings, with the password field empty."""
        return cls(
            settings.host,
            str(settings.port),
            settings.tls_mode,
            settings.username,
            "",
            settings.from_address,
        )


@dataclass(frozen=True)
class CleanSmtp:
    """A checked form: the settings to store and what to do with the password."""

    settings: SmtpSettings
    password: str
    remove_password: bool


class SmtpFormError(Exception):
    """The form has refused values; ``errors`` holds one message per field name."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the messages by field name."""
        super().__init__("the SMTP settings are invalid")
        self.errors = errors


@dataclass(frozen=True)
class MailCheck:
    """The result of a test mail: whether it was sent, and a sentence to show."""

    ok: bool
    message: str


def _host_problem(host: str) -> str | None:
    """Why the host is not a name or an address, or None when it is one."""
    if len(host) > MAX_HOST_LENGTH:
        return f"Use at most {MAX_HOST_LENGTH} characters."
    if _HOST.fullmatch(host):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return "Enter a host name or address, without a scheme, port or path."
    return None


def validate_smtp(form: SmtpForm) -> CleanSmtp:
    """The settings the form stands for, or ``SmtpFormError`` with a message per field.

    An empty server means mail is switched off, and then the from address is optional. With a
    server, the from address is required.
    """
    errors: dict[str, str] = {}
    host = form.host.strip()
    if host and (problem := _host_problem(host)):
        errors["host"] = problem
    port = 0
    try:
        port = int(form.port.strip())
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        errors["port"] = "Enter a port from 1 to 65535."
    if form.tls_mode not in TLS_MODES:
        errors["tls_mode"] = "Choose one of the listed modes."
    username = form.username.strip()
    if len(username) > MAX_USERNAME_LENGTH or not username.isprintable():
        errors["username"] = "Use printable characters, at most 255."
    if len(form.password) > MAX_PASSWORD_LENGTH or not form.password.isprintable():
        errors["password"] = "Use printable characters, at most 1024."  # noqa: S105
    elif form.password and form.remove_password:
        errors["password"] = "Type a new password or remove the stored one, not both."  # noqa: S105
    from_address = form.from_address.strip()
    if from_address and not valid_address(from_address):
        errors["from_address"] = "Enter an address such as env-manager@example.com."
    elif host and not from_address:
        errors["from_address"] = "Enter the address the mail comes from."
    if errors:
        raise SmtpFormError(errors)
    return CleanSmtp(
        SmtpSettings(host, port, form.tls_mode, username, from_address),
        form.password,
        form.remove_password,
    )


async def save_smtp(
    db: AsyncSession, crypto: CryptoService, clean: CleanSmtp, *, actor: AuditActor
) -> bool:
    """Store the settings and the password change, audit them and commit.

    Returns whether anything changed. Delivery health starts afresh either way.
    """
    changes: list[SettingChange] = await save_section(db, "smtp", clean.settings, actor=actor)
    secret_changed = False
    if clean.password:
        await write_secret(db, crypto, SMTP_PASSWORD_KEY, clean.password, actor=actor)
        secret_changed = True
    elif clean.remove_password:
        secret_changed = await clear_secret(db, SMTP_PASSWORD_KEY, actor=actor)
    await clear_failures(db, EMAIL_CHANNEL)
    await db.commit()
    return bool(changes) or secret_changed


async def send_test_mail(
    db: AsyncSession,
    crypto: CryptoService,
    transport: MailTransport,
    *,
    to: str,
    actor: AuditActor,
    base_url: str,
    now: datetime,
) -> MailCheck:
    """Send one mail through the stored settings and say how it went.

    The result is audited as ``smtp.test`` with the recipient and a fixed reason, never the
    password. A mail that works also clears the failures counted for the banner.
    """
    stored = await load_stored_settings(db)
    smtp = stored.smtp
    recipient = to.strip()
    display = DisplayTime(stored.general.time_zone, stored.general.date_format)
    if not smtp.configured:
        return MailCheck(False, NOT_CONFIGURED_MESSAGE)
    if not valid_address(recipient):
        return MailCheck(False, "Enter a valid address to send the test mail to.")
    try:
        password = await read_secret(db, crypto, SMTP_PASSWORD_KEY)
    except CryptoError:
        await _audit_test(db, actor, recipient, UNREADABLE_MESSAGE)
        return MailCheck(False, UNREADABLE_MESSAGE)
    content = probe_mail_content(smtp, actor.label, MessageContext(base_url, display, now))
    try:
        await transport.send(smtp, password, render_mail(content, recipient))
    except MailFailure as failure:
        await _audit_test(db, actor, recipient, failure.reason)
        return MailCheck(False, failure.reason)
    await clear_failures(db, EMAIL_CHANNEL)
    await _audit_test(db, actor, recipient, None)
    return MailCheck(True, f"Test mail sent {display.clock(now)}")


async def _audit_test(
    db: AsyncSession, actor: AuditActor, recipient: str, reason: str | None
) -> None:
    """Audit a test mail and commit."""
    details: dict[str, object] = {"to": recipient}
    if reason is not None:
        details["reason"] = reason
    await record_committed_audit(
        db,
        actor,
        action="smtp.test",
        result=AuditResult.FAILURE if reason else AuditResult.SUCCESS,
        target_type="settings",
        target_name="smtp",
        details=details,
    )
