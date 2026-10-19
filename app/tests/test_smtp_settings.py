"""Tests for the SMTP settings: checking the form, saving it with its secret and the test mail."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import AppSetting, AuditEntry, AuditResult
from app.models.types import utc_now
from app.services.app_settings import (
    SMTP_PASSWORD_KEY,
    SmtpSettings,
    has_secret,
    load_stored_settings,
    read_secret,
)
from app.services.audit import AuditActor
from app.services.notify_status import EMAIL_CHANNEL, load_failures, record_failure
from app.services.smtp_settings import (
    NOT_CONFIGURED_MESSAGE,
    MailCheck,
    SmtpForm,
    SmtpFormError,
    save_smtp,
    send_test_mail,
    validate_smtp,
)
from app.tests.notify_helpers import (
    SMTP,
    SMTP_PASSWORD,
    RecordingTransport,
    configure_smtp,
    decoded_text,
    mime_of,
    permanent_failure,
)

NOW = datetime(2026, 10, 18, 9, 5, tzinfo=UTC)


def form(**changes: object) -> SmtpForm:
    """A valid form for the SMTP constant, with fields replaced."""
    values: dict[str, object] = {
        "host": "smtp.example.com",
        "port": "587",
        "tls_mode": "starttls",
        "username": "mailer",
        "password": "",
        "from_address": "env-manager@example.com",
        "remove_password": False,
    }
    values.update(changes)
    return SmtpForm(**values)  # type: ignore[arg-type]


def refused(**changes: object) -> dict[str, str]:
    """The messages by field for a form that is refused."""
    with pytest.raises(SmtpFormError) as error:
        validate_smtp(form(**changes))
    return error.value.errors


def test_a_valid_form_becomes_the_settings_and_trims_its_text() -> None:
    """The numbers are numbers and the text has no spaces around it."""
    clean = validate_smtp(
        form(host=" smtp.example.com ", port=" 2525 ", username=" mailer ", from_address=" a@b.co ")
    )
    assert clean.settings == SmtpSettings("smtp.example.com", 2525, "starttls", "mailer", "a@b.co")
    assert (clean.password, clean.remove_password) == ("", False)


def test_an_empty_server_switches_mail_off_and_needs_no_from_address() -> None:
    """Saving everything empty is how the superadmin unsets SMTP."""
    clean = validate_smtp(form(host="", username="", from_address=""))
    assert not clean.settings.configured


def test_a_server_needs_a_from_address() -> None:
    """With a server named, the from address is required."""
    assert refused(from_address="") == {"from_address": "Enter the address the mail comes from."}


@pytest.mark.parametrize(
    "host", ["smtp://x.example.com", "x.example.com:25", "a b", "x/y", "-x.com"]
)
def test_a_host_must_be_a_name_or_address_without_scheme_port_or_path(host: str) -> None:
    """The server field takes a host name or an address and nothing else."""
    assert "host" in refused(host=host)


@pytest.mark.parametrize("host", ["localhost", "smtp4dev", "10.0.0.5", "mail.corp.local", "::1"])
def test_host_names_and_addresses_are_accepted(host: str) -> None:
    """Private names and addresses, IPv6 included, are valid servers."""
    assert validate_smtp(form(host=host)).settings.host == host


@pytest.mark.parametrize("port", ["", "0", "65536", "-1", "abc", "25.5"])
def test_the_port_is_a_number_from_1_to_65535(port: str) -> None:
    """Anything else is refused with one message."""
    assert refused(port=port) == {"port": "Enter a port from 1 to 65535."}


def test_the_tls_mode_is_one_of_the_three() -> None:
    """None, STARTTLS and SSL/TLS are the choices."""
    for mode in ("none", "starttls", "tls"):
        assert validate_smtp(form(tls_mode=mode)).settings.tls_mode == mode
    assert refused(tls_mode="ssl") == {"tls_mode": "Choose one of the listed modes."}


def test_the_from_address_must_be_one_plain_address() -> None:
    """A name part, a space or a second address is refused."""
    for address in ("not-an-address", "ENV <a@b.co>", "a@b.co, c@d.co", "a b@c.co"):
        assert "from_address" in refused(from_address=address)
    assert validate_smtp(form(from_address="ops@corp.local")).settings.from_address == (
        "ops@corp.local"
    )


def test_the_username_and_password_have_bounds_and_cannot_hold_control_characters() -> None:
    """Printable text only, and a password cannot be both typed and removed."""
    assert "username" in refused(username="a" * 256)
    assert "username" in refused(username="a\nb")
    assert "password" in refused(password="x" * 1025)
    assert "password" in refused(password="a\r\nb")
    assert refused(password="new", remove_password=True) == {
        "password": "Type a new password or remove the stored one, not both."
    }


async def test_saving_stores_the_settings_and_encrypts_the_password(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """The section is stored as typed values and the password only as ciphertext."""
    clean = validate_smtp(form(password=SMTP_PASSWORD))
    async with create_session_factory(seeded_database)() as db:
        assert await save_smtp(db, crypto, clean, actor=AuditActor.cli()) is True
    async with create_session_factory(seeded_database)() as db:
        assert (await load_stored_settings(db)).smtp == SMTP
        assert await read_secret(db, crypto, SMTP_PASSWORD_KEY) == SMTP_PASSWORD
        row = await db.get(AppSetting, SMTP_PASSWORD_KEY)
        assert row is not None and SMTP_PASSWORD not in row.value
        values = [item.value for item in (await db.execute(select(AppSetting))).scalars()]
        assert not any(SMTP_PASSWORD in value for value in values)


async def test_an_empty_password_keeps_the_stored_one_and_remove_deletes_it(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """Leaving the field empty is how the stored password stays; removing is its own choice."""
    await configure_smtp(seeded_database, crypto)
    async with create_session_factory(seeded_database)() as db:
        again = validate_smtp(form(port="2525"))
        assert await save_smtp(db, crypto, again, actor=AuditActor.cli()) is True
        assert await read_secret(db, crypto, SMTP_PASSWORD_KEY) == SMTP_PASSWORD
        unchanged = validate_smtp(form(port="2525"))
        assert await save_smtp(db, crypto, unchanged, actor=AuditActor.cli()) is False
        assert await has_secret(db, SMTP_PASSWORD_KEY)
        removal = validate_smtp(form(port="2525", remove_password=True))
        assert await save_smtp(db, crypto, removal, actor=AuditActor.cli()) is True
        assert (
            not await has_secret(db, SMTP_PASSWORD_KEY)
            and await read_secret(db, crypto, SMTP_PASSWORD_KEY) is None
        )
        assert await save_smtp(db, crypto, removal, actor=AuditActor.cli()) is False


async def test_the_audit_entries_name_the_changes_but_never_the_password(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """Plain settings list old and new values; the password is only named."""
    await configure_smtp(seeded_database, crypto, SmtpSettings(), password=None)
    clean = validate_smtp(form(password=SMTP_PASSWORD))
    async with create_session_factory(seeded_database)() as db:
        await save_smtp(db, crypto, clean, actor=AuditActor.cli())
        entries = list(
            (
                await db.execute(select(AuditEntry).where(AuditEntry.action == "settings.update"))
            ).scalars()
        )
    text = repr([entry.details for entry in entries])
    assert SMTP_PASSWORD not in text
    assert "smtp.host" in text and "'kind': 'secret'" in text


async def test_every_save_starts_the_delivery_status_afresh(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """A failure counted under the old settings says nothing about the new ones."""
    async with create_session_factory(seeded_database)() as db:
        await record_failure(db, EMAIL_CHANNEL, "The mail could not be sent.", utc_now())
        await db.commit()
        await save_smtp(db, crypto, validate_smtp(form()), actor=AuditActor.cli())
        assert await load_failures(db) == []


async def send(
    engine: AsyncEngine,
    crypto: CryptoService,
    transport: RecordingTransport,
    to: str = "alice@example.com",
) -> MailCheck:
    """The result of a test mail sent through the stored settings."""
    async with create_session_factory(engine)() as db:
        return await send_test_mail(
            db,
            crypto,
            transport,
            to=to,
            actor=AuditActor.cli(),
            base_url="https://env.example.com",
            now=NOW,
        )


async def test_the_test_mail_goes_through_the_stored_settings_and_reports_the_time(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """The stored host, password and from address are used; the answer names the time."""
    await configure_smtp(seeded_database, crypto)
    transport = RecordingTransport()
    check = await send(seeded_database, crypto, transport)
    assert (check.ok, check.message) == (True, "Test mail sent 09:05")
    (sent,) = transport.sent
    assert (sent.smtp, sent.password, sent.mail.to) == (SMTP, SMTP_PASSWORD, "alice@example.com")
    text = decoded_text(await mime_of(sent.mail))
    assert "Test mail" in text and "smtp.example.com:587" in text
    assert SMTP_PASSWORD not in text and "mailer" not in text


async def test_the_test_mail_is_refused_while_smtp_is_not_set_up(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """Nothing is sent and the answer says what to do."""
    transport = RecordingTransport()
    check = await send(seeded_database, crypto, transport)
    assert (check.ok, check.message) == (False, NOT_CONFIGURED_MESSAGE)
    assert transport.calls == 0


async def test_the_test_mail_needs_a_plain_address_to_send_to(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """A missing or bad recipient is refused before anything is sent."""
    await configure_smtp(seeded_database, crypto)
    transport = RecordingTransport()
    for address in ("", "nobody", "a@b.co, c@d.co"):
        check = await send(seeded_database, crypto, transport, address)
        assert check.ok is False and "valid address" in check.message
    assert transport.calls == 0


async def test_a_failed_test_mail_shows_the_fixed_reason_and_is_audited_without_credentials(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """The reason is the transport's fixed sentence; the audit entry names the recipient."""
    await configure_smtp(seeded_database, crypto)
    transport = RecordingTransport(failures=[permanent_failure()])
    check = await send(seeded_database, crypto, transport)
    assert (check.ok, check.message) == (False, "The mail server refused the username or password.")
    async with create_session_factory(seeded_database)() as db:
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "smtp.test"))
        ).scalar_one()
    assert entry.result is AuditResult.FAILURE
    assert entry.details == {"to": "alice@example.com", "reason": check.message}
    assert SMTP_PASSWORD not in repr(entry.details)


async def test_a_working_test_mail_is_audited_and_clears_the_failures(
    seeded_database: AsyncEngine, crypto: CryptoService
) -> None:
    """Success is an audit entry with the recipient, and the banner count starts over."""
    await configure_smtp(seeded_database, crypto)
    async with create_session_factory(seeded_database)() as db:
        await record_failure(db, EMAIL_CHANNEL, "The mail could not be sent.", utc_now())
        await db.commit()
    await send(seeded_database, crypto, RecordingTransport())
    async with create_session_factory(seeded_database)() as db:
        assert await load_failures(db) == []
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "smtp.test"))
        ).scalar_one()
    assert entry.result is AuditResult.SUCCESS and entry.details == {"to": "alice@example.com"}
