"""Tests for the mail transport: fastapi-mail configured from the settings at send time.

The boundary that is faked is ``aiosmtplib.SMTP``, the client fastapi-mail drives, so the
library's own message building, connection settings and credential handling all run.
"""

import ssl
from collections.abc import Iterator

import aiosmtplib
import pytest

from app.services import mailer
from app.services.app_settings import SmtpSettings
from app.services.mailer import (
    FastapiMailTransport,
    MailFailure,
    OutgoingMail,
    classify,
    connection_config,
    valid_address,
)
from app.tests.notify_helpers import SMTP, FakeSMTP, install_fake_smtp, parts_of

PASSWORD = "hunter2-sentinel"
MAIL = OutgoingMail("alice@example.com", "[ENV Manager] Test", "Plain body", "<p>Html body</p>")


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeSMTP]]:
    """The fake client in place of aiosmtplib's, reset for the test."""
    yield install_fake_smtp(monkeypatch)
    FakeSMTP.instances = []


async def send(smtp: SmtpSettings = SMTP, mail: OutgoingMail = MAIL) -> FakeSMTP:
    """Send one mail and return the client that carried it."""
    await FastapiMailTransport().send(smtp, PASSWORD, mail)
    return FakeSMTP.instances[-1]


@pytest.mark.parametrize(
    ("mode", "starttls", "implicit"),
    [("none", False, False), ("starttls", True, False), ("tls", False, True)],
)
async def test_the_tls_mode_picks_how_the_connection_is_secured(
    fake_smtp: type[FakeSMTP], mode: str, starttls: bool, implicit: bool
) -> None:
    """None is plain, STARTTLS upgrades the connection and SSL/TLS is encrypted from the start."""
    client = await send(SmtpSettings("smtp.example.com", 2525, mode, "mailer", "env@example.com"))
    assert client.arguments["hostname"] == "smtp.example.com"
    assert client.arguments["port"] == 2525
    assert client.arguments["start_tls"] is starttls
    assert client.arguments["use_tls"] is implicit
    assert client.arguments["validate_certs"] is True
    assert client.arguments["timeout"] == mailer.SEND_TIMEOUT_SECONDS


async def test_the_credentials_are_used_only_when_there_is_a_username(
    fake_smtp: type[FakeSMTP],
) -> None:
    """A username with the stored password signs in; no username means no sign in."""
    assert (await send()).logins == [("mailer", PASSWORD)]
    anonymous = SmtpSettings("smtp.example.com", 25, "none", "", "env@example.com")
    assert (await send(anonymous)).logins == []


async def test_the_message_carries_both_parts_the_sender_and_the_subject(
    fake_smtp: type[FakeSMTP],
) -> None:
    """The plain text comes first with the HTML as its alternative, from the product name."""
    client = await send()
    (message,) = client.messages
    assert message["To"] == "alice@example.com"
    assert message["From"] == "ENV Manager <env-manager@example.com>"
    assert message["Subject"] == "[ENV Manager] Test"
    assert parts_of(message) == {"text/plain": "Plain body", "text/html": "<p>Html body</p>"}
    assert client.quit_called


async def test_the_connection_is_built_from_the_settings_given_and_never_from_the_environment(
    fake_smtp: type[FakeSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAIL_* variables of the process change nothing: every field comes from the settings."""
    monkeypatch.setenv("MAIL_SERVER", "evil.example.org")
    monkeypatch.setenv("MAIL_PORT", "2")
    monkeypatch.setenv("MAIL_USERNAME", "intruder")
    monkeypatch.setenv("MAIL_FROM", "intruder@example.org")
    monkeypatch.setenv("TIMEOUT", "999")
    client = await send()
    assert client.arguments["hostname"] == "smtp.example.com"
    assert client.arguments["port"] == 587
    assert client.arguments["timeout"] == mailer.SEND_TIMEOUT_SECONDS
    assert client.logins == [("mailer", PASSWORD)]
    assert client.messages[0]["From"].endswith("<env-manager@example.com>")


async def test_settings_are_read_when_sending_so_a_change_applies_to_the_next_mail(
    fake_smtp: type[FakeSMTP],
) -> None:
    """The same transport follows whatever settings each call is given."""
    transport = FastapiMailTransport()
    await transport.send(SMTP, PASSWORD, MAIL)
    other = SmtpSettings("smtp.other.example", 465, "tls", "", "other@example.com")
    await transport.send(other, None, MAIL)
    first, second = FakeSMTP.instances
    assert (first.arguments["hostname"], second.arguments["hostname"]) == (
        "smtp.example.com",
        "smtp.other.example",
    )
    assert second.messages[0]["From"].endswith("<other@example.com>")


async def test_addresses_on_private_names_are_accepted(fake_smtp: type[FakeSMTP]) -> None:
    """Private networks use names such as .local and .test, which fastapi-mail would refuse."""
    for address in ("ops@corp.local", "root@example.test", "a.b+c@sub.example.com"):
        await FastapiMailTransport().send(SMTP, PASSWORD, OutgoingMail(address, "s", "t", "h"))
        assert FakeSMTP.instances[-1].messages[0]["To"] == address


@pytest.mark.parametrize(
    "address",
    ["", "no-at", "a b@example.com", "a@b", "a@example.com\nBcc: x@y.z", "<a@b.co>", "é@x.co"],
)
async def test_an_address_that_is_not_plain_is_refused_before_anything_connects(
    fake_smtp: type[FakeSMTP], address: str
) -> None:
    """A recipient or from address with a space, a newline or a name never reaches the server."""
    assert not valid_address(address)
    with pytest.raises(MailFailure) as refused:
        await FastapiMailTransport().send(SMTP, PASSWORD, OutgoingMail(address, "s", "t", "h"))
    assert not refused.value.retryable
    assert FakeSMTP.instances == []
    bad_from = SmtpSettings("smtp.example.com", 587, "starttls", "", address)
    with pytest.raises(MailFailure):
        await FastapiMailTransport().send(bad_from, None, MAIL)
    assert FakeSMTP.instances == []


@pytest.mark.parametrize(
    ("error", "reason", "retryable"),
    [
        (
            aiosmtplib.SMTPAuthenticationError(535, f"bad login {PASSWORD}"),
            "The mail server refused the username or password.",
            False,
        ),
        (
            aiosmtplib.SMTPSenderRefused(550, "no", "env@example.com"),
            "The mail server refused the from address.",
            False,
        ),
        (
            aiosmtplib.SMTPRecipientsRefused([]),
            "The mail server refused the recipient address.",
            False,
        ),
        (
            aiosmtplib.SMTPRecipientRefused(550, "no", "a@b.co"),
            "The mail server refused the recipient address.",
            False,
        ),
        (
            ssl.SSLError("certificate verify failed"),
            "The TLS connection to the mail server failed.",
            False,
        ),
        (
            aiosmtplib.SMTPNotSupported("STARTTLS not supported"),
            "The mail server does not support the chosen TLS mode.",
            False,
        ),
        (
            aiosmtplib.SMTPConnectTimeoutError("slow"),
            "The mail server did not answer in time.",
            True,
        ),
        (TimeoutError(), "The mail server did not answer in time.", True),
        (
            aiosmtplib.SMTPServerDisconnected("gone"),
            "The mail server closed the connection.",
            True,
        ),
        (aiosmtplib.SMTPConnectError("refused"), "Could not connect to the mail server.", True),
        (ConnectionRefusedError("[Errno 111]"), "Could not connect to the mail server.", True),
        (
            aiosmtplib.SMTPDataError(451, "try later"),
            "The mail server refused the message.",
            True,
        ),
        (
            aiosmtplib.SMTPDataError(554, "rejected as spam"),
            "The mail server refused the message.",
            False,
        ),
        (RuntimeError(f"surprise {PASSWORD}"), "The mail could not be sent.", True),
    ],
)
async def test_every_failure_becomes_a_fixed_sentence_that_never_repeats_the_error(
    fake_smtp: type[FakeSMTP], error: Exception, reason: str, retryable: bool
) -> None:
    """The reason is fixed words, whether it can work later is decided, and no text is echoed."""
    FakeSMTP.login_error = error
    with pytest.raises(MailFailure) as refused:
        await send()
    failure = refused.value
    assert (failure.reason, failure.retryable) == (reason, retryable)
    assert PASSWORD not in f"{failure!s} {failure!r} {failure.reason}"
    assert failure.__context__ is None and failure.__cause__ is None


async def test_a_failure_while_connecting_or_sending_is_classified_the_same_way(
    fake_smtp: type[FakeSMTP],
) -> None:
    """The connection errors fastapi-mail wraps are classified by the error underneath."""
    FakeSMTP.connect_error = ConnectionRefusedError("[Errno 111] Connect call failed")
    with pytest.raises(MailFailure, match="Could not connect"):
        await send()
    FakeSMTP.connect_error = None
    FakeSMTP.send_error = aiosmtplib.SMTPRecipientRefused(550, "no such user", "a@b.co")
    with pytest.raises(MailFailure, match="refused the recipient"):
        await send()


async def test_a_server_that_never_answers_ends_with_a_timeout_failure(
    fake_smtp: type[FakeSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One attempt is bounded as a whole, whatever the library does."""
    monkeypatch.setattr(mailer, "TOTAL_TIMEOUT_SECONDS", 0.05)
    FakeSMTP.hang = True
    with pytest.raises(MailFailure) as refused:
        await send()
    assert refused.value.reason == "The mail server did not answer in time."
    assert refused.value.retryable


def test_the_connection_settings_give_every_field_a_value() -> None:
    """Nothing is left to the settings class to read from the environment."""
    config = connection_config(SMTP, PASSWORD)
    assert config.MAIL_PASSWORD.get_secret_value() == PASSWORD
    assert (config.USE_CREDENTIALS, config.SUPPRESS_SEND, config.TEMPLATE_FOLDER) == (
        True,
        0,
        None,
    )
    anonymous = connection_config(SmtpSettings("h.example.com", 25, "none", "", "a@b.co"), None)
    assert (anonymous.USE_CREDENTIALS, anonymous.MAIL_PASSWORD.get_secret_value()) == (False, "")


def test_classify_walks_the_chain_of_causes() -> None:
    """A wrapper error is classified by the error it was raised from."""
    inner = aiosmtplib.SMTPAuthenticationError(535, "no")
    try:
        try:
            raise inner
        except Exception as error:
            raise RuntimeError("wrapped") from error
    except RuntimeError as wrapper:
        assert classify(wrapper).reason == "The mail server refused the username or password."
