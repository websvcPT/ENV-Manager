"""The words of every notification mail, and the templates that turn them into a message.

Each event becomes a ``MailContent`` (a subject, a heading, a sentence, a few labelled facts and
an optional list) made only of ids, names and fixed words; the two templates in
``templates/mail`` lay it out as a plain text part and a simple HTML part. The environment has
autoescape on. The plain text part is rendered through it too and its escapes are undone, because
that part is not markup; a value can therefore never reach the HTML part unescaped.
"""

import html
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.core.display_time import DisplayTime
from app.models import BackupStatus, DeployResultStatus, DeployStatus, PostDeployStatus
from app.services.app_settings import SmtpSettings
from app.services.backup_events import BackupFinished
from app.services.deploy_events import DeployFinished
from app.services.mailer import TLS_MODES, OutgoingMail
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
from app.services.settings_events import SettingsChanged

MAIL_TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "mail"
SUBJECT_PREFIX = "[ENV Manager] "
MAX_SUBJECT_LENGTH = 200
_BLANKS = re.compile(r"\s+")

_environment = Environment(
    loader=FileSystemLoader(MAIL_TEMPLATES),
    autoescape=True,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)

DEPLOY_WORDS = {
    DeployStatus.SUCCESS: "succeeded",
    DeployStatus.HALTED: "halted",
    DeployStatus.BLOCKED: "blocked",
    DeployStatus.FAILED: "failed",
    DeployStatus.POST_DEPLOY_FAILED: "failed in its post-deploy command",
}
FILE_RESULT_WORDS = {
    DeployResultStatus.WRITTEN: "written",
    DeployResultStatus.UNCHANGED: "unchanged",
    DeployResultStatus.WOULD_WRITE: "would be written",
    DeployResultStatus.BLOCKED: "blocked",
    DeployResultStatus.HALTED: "halted",
    DeployResultStatus.FAILED: "failed",
}
POST_DEPLOY_WORDS = {
    PostDeployStatus.RUNNING: "running",
    PostDeployStatus.RAN: "ran",
    PostDeployStatus.SKIPPED: "skipped",
    PostDeployStatus.FAILED: "failed",
    PostDeployStatus.NONE: "no command",
}
USER_CHANGE_WORDS = {
    UserChange.CREATED: ("User created", "was created"),
    UserChange.DISABLED: ("User disabled", "was disabled"),
    UserChange.PASSWORD_CHANGED: ("Password changed", "changed their password"),
    UserChange.PASSWORD_RESET: ("Password reset", "got a new generated password"),
    UserChange.TWO_FACTOR_RESET: ("Two-factor reset", "had their two-factor sign-in reset"),
}
FILE_CHANGE_WORDS = {
    FileChange.CREATED: "created",
    FileChange.SAVED: "saved",
    FileChange.RESTORED: "restored from an older version",
    FileChange.IMPORTED: "imported from the file on the host",
}


@dataclass(frozen=True)
class MailContent:
    """What a notification says, before it is laid out.

    ``facts`` are label and value pairs; ``items`` is an optional list under ``items_title``.
    The link, when there is one, points into the application.
    """

    subject: str
    heading: str
    summary: str
    facts: tuple[tuple[str, str], ...] = ()
    items_title: str = ""
    items: tuple[str, ...] = ()
    link: str = ""
    link_label: str = ""


@dataclass(frozen=True)
class ItemNames:
    """The names of the project, deployment and service an event is about."""

    project: str
    deployment: str
    service: str


@dataclass(frozen=True)
class MessageContext:
    """What the builders need besides the event: the base address, the clock and the zone."""

    base_url: str
    display: DisplayTime
    now: datetime

    def when(self) -> str:
        """The event time in the chosen time zone and format."""
        return f"{self.display.text(self.now)} ({self.display.label})"

    def url(self, path: str) -> str:
        """An absolute address of a page of the application."""
        return self.base_url.rstrip("/") + path


def one_line(text: str, limit: int = MAX_SUBJECT_LENGTH) -> str:
    """The text on one line: control characters and runs of blanks become one space.

    Names are typed by people, so nothing a name holds may add a header line to a mail.
    """
    flat = "".join(" " if not char.isprintable() else char for char in text)
    return _BLANKS.sub(" ", flat).strip()[:limit]


def _subject(text: str) -> str:
    """A subject line with the product prefix."""
    return one_line(SUBJECT_PREFIX + text)


def deploy_content(
    event: DeployFinished,
    names: ItemNames,
    ctx: MessageContext,
    visible: frozenset[int] | None,
) -> MailContent:
    """The deploy result mail. ``visible`` limits the files listed to the recipient's own (None
    lists them all), so a user never learns of a file outside their grants."""
    word = DEPLOY_WORDS[event.status]
    shown = [
        item for item in event.environments if visible is None or item.environment_id in visible
    ]
    lines = tuple(
        one_line(
            f"{item.name}: {FILE_RESULT_WORDS[item.status]}"
            + (f", version {item.version}" if item.version is not None else "")
        )
        for item in shown
    )
    facts = [
        ("Project", one_line(names.project)),
        ("Deployment", one_line(event.deployment_name)),
        ("Service", one_line(event.service_name)),
        ("Result", word),
        ("Started by", one_line(event.actor)),
        ("Forced", "yes" if event.force else "no"),
        ("Post-deploy command", POST_DEPLOY_WORDS[event.post_deploy]),
        ("When", ctx.when()),
    ]
    return MailContent(
        subject=_subject(f"Deploy {word}: {event.service_name} ({event.deployment_name})"),
        heading=f"Deploy {word}",
        summary=f"A deploy of {one_line(event.service_name)} in "
        f"{one_line(event.deployment_name)} {word}.",
        facts=tuple(facts),
        items_title="Files",
        items=lines,
        link=ctx.url(f"/services/{event.service_id}"),
        link_label="Open the service",
    )


def file_content(event: FileChanged, names: ItemNames, ctx: MessageContext) -> MailContent:
    """The mail about a new file version."""
    how = FILE_CHANGE_WORDS[event.change]
    return MailContent(
        subject=_subject(f"File changed: {event.environment_name} ({names.service})"),
        heading="File changed",
        summary=f"{one_line(event.environment_name)} {how}, as version {event.version}.",
        facts=(
            ("Project", one_line(names.project)),
            ("Deployment", one_line(names.deployment)),
            ("Service", one_line(names.service)),
            ("File", one_line(event.environment_name)),
            ("Version", str(event.version)),
            ("Changed by", one_line(event.actor)),
            ("When", ctx.when()),
        ),
        link=ctx.url(f"/environments/{event.environment_id}/history"),
        link_label="Open the history",
    )


def backup_content(event: BackupFinished, ctx: MessageContext) -> MailContent:
    """The mail about a backup that failed or could not be copied."""
    partial = event.status is BackupStatus.PARTIAL
    heading = "Backup incomplete" if partial else "Backup failed"
    summary = (
        "The backup was taken but could not be copied to the extra destination."
        if partial
        else "The backup did not complete."
    )
    facts = [
        ("Started by", one_line(event.actor)),
        ("Trigger", event.trigger.value),
        ("Reason", one_line(event.error or "unknown")),
        ("When", ctx.when()),
    ]
    return MailContent(
        subject=_subject(heading),
        heading=heading,
        summary=summary,
        facts=tuple(facts),
        link=ctx.url("/settings/backup"),
        link_label="Open backup settings",
    )


def settings_content(event: SettingsChanged, ctx: MessageContext) -> MailContent:
    """The mail about a change to the allowed paths."""
    area = "Allowed path"
    return MailContent(
        subject=_subject(f"{area} {event.action}: {event.subject}"),
        heading=f"{area} {event.action}",
        summary=f"{one_line(event.subject)} was {event.action} in the list of allowed paths.",
        facts=(
            ("Path", one_line(event.subject)),
            ("Changed by", one_line(event.actor)),
            ("When", ctx.when()),
        ),
        link=ctx.url("/settings/paths"),
        link_label="Open allowed paths",
    )


def sign_in_content(event: SignInFromNewAddress, ctx: MessageContext) -> MailContent:
    """The mail about a sign-in from an address the user had not used before."""
    return MailContent(
        subject=_subject(f"New sign-in address for {event.user_name}"),
        heading="Sign-in from a new address",
        summary=f"{one_line(event.user_name)} signed in from an address they had not used before.",
        facts=(
            ("User", one_line(event.user_name)),
            ("Address", one_line(event.address, 64)),
            ("When", ctx.when()),
        ),
        link=ctx.url("/audit"),
        link_label="Open the audit log",
    )


def lockout_content(event: LockoutStarted, ctx: MessageContext) -> MailContent:
    """The mail about a lockout. A name that belongs to no user is never repeated."""
    if event.scope is LockoutScope.USER:
        who = f"the user {one_line(event.user_name)}" if event.user_name else "a sign-in name"
    elif event.scope is LockoutScope.API_ADDRESS:
        who = "API requests from an address"
    else:
        who = "sign-ins from an address"
    facts: list[tuple[str, str]] = [("Locked", who)]
    if event.user_name and event.scope is not LockoutScope.API_ADDRESS:
        facts.append(("User", one_line(event.user_name)))
    if event.address:
        facts.append(("Address", one_line(event.address, 64)))
    facts += [("Locked for", f"{event.minutes} minutes"), ("When", ctx.when())]
    return MailContent(
        subject=_subject("Lockout after repeated failed attempts"),
        heading="Lockout",
        summary=f"Repeated failed attempts locked {who} for {event.minutes} minutes.",
        facts=tuple(facts),
        link=ctx.url("/audit"),
        link_label="Open the audit log",
    )


def user_content(event: UserChanged, ctx: MessageContext) -> MailContent:
    """The mail about a change to a user account. It never carries a password."""
    heading, phrase = USER_CHANGE_WORDS[event.change]
    facts = [("User", one_line(event.user_name)), ("Done by", one_line(event.actor))]
    if event.group:
        facts.insert(1, ("Group", one_line(event.group)))
    facts.append(("When", ctx.when()))
    return MailContent(
        subject=_subject(f"{heading}: {event.user_name}"),
        heading=heading,
        summary=f"The account {one_line(event.user_name)} {phrase}.",
        facts=tuple(facts),
        link=ctx.url("/users"),
        link_label="Open users",
    )


def git_content(event: GitPushFailed, ctx: MessageContext) -> MailContent:
    """The mail about a push that failed after its retries."""
    return MailContent(
        subject=_subject("Git push failed"),
        heading="Git push failed",
        summary="The push to the git repository failed after retrying.",
        facts=(
            ("Reason", one_line(event.reason)),
            ("Attempts", str(event.attempts)),
            ("When", ctx.when()),
        ),
        link=ctx.url("/settings/git"),
        link_label="Open git settings",
    )


def probe_mail_content(smtp: SmtpSettings, sender: str, ctx: MessageContext) -> MailContent:
    """The test mail. It names the server and mode, never the username or password."""
    return MailContent(
        subject=_subject("Test mail"),
        heading="Test mail",
        summary="This is a test message. If you can read it, the SMTP settings work.",
        facts=(
            ("Server", one_line(f"{smtp.host}:{smtp.port}")),
            ("TLS mode", TLS_MODES.get(smtp.tls_mode, smtp.tls_mode)),
            ("Sent by", one_line(sender)),
            ("When", ctx.when()),
        ),
    )


def describe_event(
    event: NotificationEvent,
    ctx: MessageContext,
    names: ItemNames | None = None,
    visible: frozenset[int] | None = None,
) -> MailContent | None:
    """The content of an event, None for one that says nothing (a backup that worked).

    ``names`` is needed for the events about a project item and ``visible`` limits the files a
    deploy mail lists.
    """
    if isinstance(event, DeployFinished) and names is not None:
        return deploy_content(event, names, ctx, visible)
    if isinstance(event, FileChanged) and names is not None:
        return file_content(event, names, ctx)
    if isinstance(event, BackupFinished):
        return None if event.status is BackupStatus.SUCCESS else backup_content(event, ctx)
    if isinstance(event, SettingsChanged):
        return settings_content(event, ctx)
    if isinstance(event, SignInFromNewAddress):
        return sign_in_content(event, ctx)
    if isinstance(event, LockoutStarted):
        return lockout_content(event, ctx)
    if isinstance(event, UserChanged):
        return user_content(event, ctx)
    if isinstance(event, GitPushFailed):
        return git_content(event, ctx)
    return None


def render_mail(content: MailContent, to: str) -> OutgoingMail:
    """Lay the content out as a message to one recipient: plain text and an HTML alternative."""
    values = {
        "heading": content.heading,
        "summary": content.summary,
        "facts": content.facts,
        "items_title": content.items_title,
        "items": content.items,
        "link": content.link,
        "link_label": content.link_label,
    }
    text = html.unescape(_environment.get_template("notification.txt").render(**values))
    markup = _environment.get_template("notification.html").render(**values)
    return OutgoingMail(to=to, subject=content.subject, text=text, html=markup)
