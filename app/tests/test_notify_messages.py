"""Tests for the words of the notification mails and the templates that lay them out."""

from datetime import UTC, datetime

import pytest

from app.core.display_time import DisplayTime
from app.models import (
    BackupStatus,
    BackupTrigger,
    DeployResultStatus,
    DeployStatus,
    PostDeployStatus,
)
from app.services.app_settings import SmtpSettings
from app.services.backup_events import BackupFinished
from app.services.deploy_events import DeployFinished, EnvironmentResult
from app.services.notify_events import (
    FileChange,
    FileChanged,
    GitPushFailed,
    LockoutScope,
    LockoutStarted,
    NotificationEvent,
    SignInFromNewAddress,
    UserChange,
    UserChanged,
)
from app.services.notify_messages import (
    ItemNames,
    MessageContext,
    describe_event,
    one_line,
    probe_mail_content,
    render_mail,
)
from app.services.settings_events import SettingsChanged
from app.tests.notify_helpers import SMTP, decoded_text, mime_of, parts_of

NOW = datetime(2026, 10, 18, 14, 30, tzinfo=UTC)
CONTEXT = MessageContext("https://env.example.com/", DisplayTime("Europe/Lisbon", "iso"), NOW)
NAMES = ItemNames("rentals", "prod", "web")
SENTINELS = (
    "Zq7vK2mPw9",
    "seed-secret",
    "MIIDdzCCAl7sentinelCertBody",
    "hunter2-sentinel",
    "generated-password-sentinel",
    "typed-into-the-name-field",
)


def deploy(
    status: DeployStatus = DeployStatus.FAILED,
    environments: tuple[EnvironmentResult, ...] | None = None,
) -> DeployFinished:
    """A finished deploy of two files."""
    files = environments or (
        EnvironmentResult(1, ".env.db", 3, DeployResultStatus.WRITTEN),
        EnvironmentResult(2, ".env.db.secrets", None, DeployResultStatus.FAILED),
    )
    return DeployFinished(
        7, 1, 2, "prod", 3, "web", "root", True, status, PostDeployStatus.FAILED, files
    )


EVENTS: dict[str, NotificationEvent] = {
    "deploy": deploy(),
    "backup": BackupFinished(
        BackupStatus.FAILURE,
        BackupTrigger.SCHEDULED,
        "x.db",
        None,
        "The database could not be copied.",
        NOW,
        "scheduler",
    ),
    "partial": BackupFinished(
        BackupStatus.PARTIAL, BackupTrigger.MANUAL, "x.db", 10, "The copy failed.", NOW, "root"
    ),
    "path": SettingsChanged("allowed_paths", "added", "/opt/rentals", "root"),
    "sign_in": SignInFromNewAddress("alice", "203.0.113.9"),
    "lockout_user": LockoutStarted(LockoutScope.USER, "alice", None, 15),
    "lockout_address": LockoutStarted(LockoutScope.ADDRESS, None, "203.0.113.9", 15),
    "lockout_api": LockoutStarted(LockoutScope.API_ADDRESS, None, "203.0.113.9", 15),
    "created": UserChanged(UserChange.CREATED, "bob", "root", "admin"),
    "disabled": UserChanged(UserChange.DISABLED, "bob", "cli"),
    "changed": UserChanged(UserChange.PASSWORD_CHANGED, "bob", "bob"),
    "reset": UserChanged(UserChange.PASSWORD_RESET, "bob", "cli"),
    "two_factor": UserChanged(UserChange.TWO_FACTOR_RESET, "bob", "root"),
    "file": FileChanged(1, 2, 3, 4, ".env.db", 5, FileChange.RESTORED, "root"),
    "git": GitPushFailed("The repository refused the credentials.", 3),
}


def content_of(event: NotificationEvent, visible: frozenset[int] | None = None):  # type: ignore[no-untyped-def]
    """The content of an event with the standard names and context."""
    content = describe_event(event, CONTEXT, NAMES, visible)
    assert content is not None
    return content


@pytest.mark.parametrize("name", sorted(EVENTS))
async def test_every_event_has_a_heading_a_sentence_and_a_link_into_the_application(
    name: str,
) -> None:
    """Each mail says what happened, when, and where to look, with the base address joined."""
    content = content_of(EVENTS[name])
    assert content.subject.startswith("[ENV Manager] ")
    assert content.heading and content.summary
    assert ("When", "2026-10-18 15:30 (Europe/Lisbon)") in content.facts
    assert content.link.startswith("https://env.example.com/") and "//" not in content.link[8:]


async def test_a_successful_backup_has_no_mail() -> None:
    """The backup seam announces every result, but only a failure has anything to say."""
    event = BackupFinished(BackupStatus.SUCCESS, BackupTrigger.MANUAL, "x.db", 10, None, NOW, "r")
    assert describe_event(event, CONTEXT) is None


async def test_the_deploy_mail_names_the_service_result_actor_and_files() -> None:
    """Project, deployment, service, result, who, force, the command and every file."""
    content = content_of(deploy())
    assert content.subject == "[ENV Manager] Deploy failed: web (prod)"
    assert dict(content.facts) == {
        "Project": "rentals",
        "Deployment": "prod",
        "Service": "web",
        "Result": "failed",
        "Started by": "root",
        "Forced": "yes",
        "Post-deploy command": "failed",
        "When": "2026-10-18 15:30 (Europe/Lisbon)",
    }
    assert content.items == (".env.db: written, version 3", ".env.db.secrets: failed")
    assert content.link == "https://env.example.com/services/3"


async def test_a_recipient_is_told_only_about_the_files_they_can_reach() -> None:
    """The file list is limited to the recipient's ids, and nothing else names the others."""
    content = content_of(deploy(), frozenset({1}))
    assert content.items == (".env.db: written, version 3",)
    assert ".env.db.secrets" not in repr(content)
    assert content_of(deploy(), frozenset()).items == ()


async def test_every_deploy_outcome_has_its_words() -> None:
    """The subject says what the run did, a failed command included."""
    words = {
        DeployStatus.SUCCESS: "succeeded",
        DeployStatus.HALTED: "halted",
        DeployStatus.BLOCKED: "blocked",
        DeployStatus.FAILED: "failed",
        DeployStatus.POST_DEPLOY_FAILED: "failed in its post-deploy command",
    }
    for status, word in words.items():
        assert content_of(deploy(status)).subject == (f"[ENV Manager] Deploy {word}: web (prod)")


async def test_a_lockout_never_repeats_a_name_that_belongs_to_no_user() -> None:
    """People type passwords into the name field, so only a user's own name is ever written."""
    unknown = LockoutStarted(LockoutScope.USER, None, None, 15)
    content = content_of(unknown)
    assert "a sign-in name" in content.summary
    assert not any(label == "User" for label, _ in content.facts)
    known = content_of(LockoutStarted(LockoutScope.USER, "alice", None, 15))
    assert ("User", "alice") in known.facts
    address = content_of(LockoutStarted(LockoutScope.ADDRESS, None, "203.0.113.9", 15))
    assert ("Address", "203.0.113.9") in address.facts
    assert ("Locked for", "15 minutes") in address.facts


async def test_the_user_mails_say_what_happened_without_any_password() -> None:
    """Created, disabled, changed, reset and two-factor reset each have their heading."""
    headings = {
        "created": "User created",
        "disabled": "User disabled",
        "changed": "Password changed",
        "reset": "Password reset",
        "two_factor": "Two-factor reset",
    }
    for name, heading in headings.items():
        content = content_of(EVENTS[name])
        assert content.heading == heading and "bob" in content.summary
    assert ("Group", "admin") in content_of(EVENTS["created"]).facts


async def test_the_test_mail_names_the_server_and_mode_but_never_the_username() -> None:
    """The test mail tells where it went through, and carries no credential."""
    content = probe_mail_content(SMTP, "root", CONTEXT)
    assert dict(content.facts)["Server"] == "smtp.example.com:587"
    assert dict(content.facts)["TLS mode"] == "STARTTLS"
    assert "mailer" not in repr(content)
    assert content.link == ""


async def test_subjects_and_facts_cannot_carry_a_header_line() -> None:
    """A name with a line break becomes one line, so it can never add a header to the mail."""
    event = UserChanged(UserChange.CREATED, "evil\r\nBcc: attacker@example.com", "root")
    content = content_of(event)
    assert "\r" not in content.subject and "\n" not in content.subject
    assert "\n" not in dict(content.facts)["User"]
    assert one_line("a\tb\x00c   d" + chr(0x2028) + "e") == "a b c d e"
    assert len(one_line("x" * 500)) == 200


async def test_html_escapes_every_value_and_the_text_part_keeps_the_characters() -> None:
    """A name with markup is inert in the HTML part and plain in the text part."""
    name = '<script>alert("x")</script> & "quotes"'
    content = content_of(UserChanged(UserChange.CREATED, name, "root"))
    mail = render_mail(content, "alice@example.com")
    assert "<script>" not in mail.html and "&lt;script&gt;" in mail.html
    assert "&amp;" in mail.html
    assert "<script>" in mail.text and '& "quotes"' in mail.text
    assert "&lt;" not in mail.text and "&amp;" not in mail.text


async def test_the_html_part_has_no_style_attribute_script_or_remote_content() -> None:
    """The message needs no external resource and no inline handler."""
    for event in EVENTS.values():
        mail = render_mail(content_of(event), "alice@example.com")
        assert not any(marker in mail.html for marker in (" style=", "<script", "onclick", "src="))
        assert chr(0x2014) not in mail.html + mail.text


async def test_the_text_part_lists_the_facts_the_items_and_the_link() -> None:
    """The plain text reads on its own."""
    mail = render_mail(content_of(deploy()), "alice@example.com")
    assert mail.text.startswith("Deploy failed\n\nA deploy of web in prod failed.\n")
    assert "Project: rentals\n" in mail.text
    assert "Files:\n- .env.db: written, version 3\n- .env.db.secrets: failed\n" in mail.text
    assert "Open the service: https://env.example.com/services/3\n" in mail.text
    assert mail.text.endswith("--\nSent by ENV Manager.\n")


@pytest.mark.parametrize("name", sorted(EVENTS))
async def test_no_secret_value_reaches_any_message_once_the_encoding_is_undone(name: str) -> None:
    """The real message is base64 inside, so the check reads the decoded parts and headers.

    The events carry names and fixed words, so the distinctive values of the seed, the SMTP
    password, a generated password and a name typed into the sign-in field are never present.
    """
    mail = render_mail(content_of(EVENTS[name]), "alice@example.com")
    message = await mime_of(mail)
    text = decoded_text(message)
    assert "Sent by ENV Manager." in text and set(parts_of(message)) == {"text/plain", "text/html"}
    for sentinel in SENTINELS:
        assert sentinel not in text
    assert "password:" not in text.lower().replace("password changed", "")


async def test_the_message_reads_back_as_the_content_that_was_rendered() -> None:
    """A guard on the check above: the decoded message does contain the rendered words."""
    mail = render_mail(content_of(deploy()), "alice@example.com")
    text = decoded_text(await mime_of(mail))
    assert ".env.db.secrets: failed" in text and "Deploy failed" in text


async def test_the_smtp_password_is_not_part_of_anything_the_test_mail_holds() -> None:
    """Even given settings with a username, the test mail has neither it nor a password."""
    settings = SmtpSettings("smtp.example.com", 587, "starttls", "mailer-user", "a@example.com")
    mail = render_mail(probe_mail_content(settings, "root", CONTEXT), "alice@example.com")
    text = decoded_text(await mime_of(mail, settings))
    assert "mailer-user" not in text and "hunter2" not in text
