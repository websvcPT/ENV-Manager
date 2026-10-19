"""The kinds of notification, which switch and recipients each one has, and the label of its row.

Every event belongs to one kind. A kind has a switch in the Notifications settings (the field of
the same name), except the announcement of an allowed path change, which is always sent when
mail is set up. A kind about a project also has a recipients choice; every other kind goes to the
superadmins, who are the ones who run the system.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.models import BackupStatus, DeployStatus
from app.services.app_settings import NotificationSettings
from app.services.deploy_events import DeployFinished
from app.services.notify_events import (
    FileChanged,
    GitPushFailed,
    GitPushRecovered,
    LockoutStarted,
    MasterKeyRotated,
    NotificationEvent,
    SignInFromNewAddress,
    UserChanged,
)
from app.services.settings_events import SettingsChanged


class EventKind(StrEnum):
    """A kind of notification. The value is the name of its switch in the settings."""

    LOGIN_NEW_ADDRESS = "login_new_address"
    LOCKOUT = "lockout"
    DEPLOY_SUCCEEDED = "deploy_succeeded"
    DEPLOY_PROBLEM = "deploy_problem"
    FILE_CHANGED = "file_changed"
    USER_CHANGED = "user_changed"
    BACKUP_FAILED = "backup_failed"
    GIT_PUSH_FAILED = "git_push_failed"
    KEY_ROTATED = "key_rotated"
    SETTINGS_CHANGED = "settings_changed"


class Recipients(StrEnum):
    """Who receives an event about a project."""

    SUPERADMINS = "superadmins"
    PROJECT_ADMINS = "project_admins"
    BOTH = "both"


RECIPIENT_LABELS: dict[str, str] = {
    Recipients.SUPERADMINS: "Superadmins",
    Recipients.PROJECT_ADMINS: "Project admins",
    Recipients.BOTH: "Superadmins and project admins",
}


@dataclass(frozen=True)
class EventRow:
    """One row of the settings matrix: the kind, its label and hint, and whether it is about a
    project (and so has a recipients choice)."""

    kind: EventKind
    label: str
    hint: str | None
    project: bool


EVENT_ROWS: tuple[EventRow, ...] = (
    EventRow(
        EventKind.LOGIN_NEW_ADDRESS,
        "Login",
        "Only a sign-in from an address that user has not used before",
        False,
    ),
    EventRow(EventKind.LOCKOUT, "Repeated failed logins or lockout", None, False),
    EventRow(EventKind.DEPLOY_SUCCEEDED, "Successful deploy", "Every deploy that worked", True),
    EventRow(
        EventKind.DEPLOY_PROBLEM,
        "Failed, blocked or halted deploy",
        "Includes a post-deploy command that failed",
        True,
    ),
    EventRow(EventKind.FILE_CHANGED, "File changed", "Every new version of a file", True),
    EventRow(
        EventKind.USER_CHANGED,
        "User created, disabled, password changed or two-factor reset",
        None,
        False,
    ),
    EventRow(EventKind.BACKUP_FAILED, "Backup failure", None, False),
    EventRow(EventKind.GIT_PUSH_FAILED, "Git push failure", None, False),
    EventRow(EventKind.KEY_ROTATED, "Master key rotated", None, False),
)
SWITCHABLE = frozenset(row.kind for row in EVENT_ROWS)
PROJECT_KINDS = frozenset(row.kind for row in EVENT_ROWS if row.project)


def kind_of(event: NotificationEvent) -> EventKind | None:
    """The kind an event belongs to, None for an event nobody is told about.

    A successful backup is announced to its seam but sends nothing, and a deploy is a problem
    unless it succeeded.
    """

    if isinstance(event, DeployFinished):
        if event.status is DeployStatus.SUCCESS:
            return EventKind.DEPLOY_SUCCEEDED
        return EventKind.DEPLOY_PROBLEM
    if isinstance(event, SettingsChanged):
        return EventKind.SETTINGS_CHANGED
    if isinstance(event, SignInFromNewAddress):
        return EventKind.LOGIN_NEW_ADDRESS
    if isinstance(event, LockoutStarted):
        return EventKind.LOCKOUT
    if isinstance(event, UserChanged):
        return EventKind.USER_CHANGED
    if isinstance(event, FileChanged):
        return EventKind.FILE_CHANGED
    if isinstance(event, GitPushFailed | GitPushRecovered):
        return EventKind.GIT_PUSH_FAILED
    if isinstance(event, MasterKeyRotated):
        return EventKind.KEY_ROTATED
    if event.status is BackupStatus.SUCCESS:
        return None
    return EventKind.BACKUP_FAILED


def is_enabled(settings: NotificationSettings, kind: EventKind) -> bool:
    """Whether the kind's switch is on. The allowed path announcement has no switch."""
    if kind not in SWITCHABLE:
        return True
    return bool(getattr(settings, kind.value))


def recipients_of(settings: NotificationSettings, kind: EventKind) -> Recipients:
    """Who receives the kind: the chosen recipients for a project kind, else the superadmins.

    A stored value that is not one of the choices reads as the project admins, the default.
    """
    if kind not in PROJECT_KINDS:
        return Recipients.SUPERADMINS
    try:
        return Recipients(getattr(settings, f"{kind.value}_to"))
    except ValueError:
        return Recipients.PROJECT_ADMINS
