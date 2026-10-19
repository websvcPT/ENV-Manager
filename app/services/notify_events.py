"""The events that can send a notification, and the seam that delivers them.

Deploys, backups and allowed path changes already have their own seams (``deploy_events``,
``backup_events``, ``settings_events``); the events here cover the rest: a sign-in from a new
address, a lockout, changes to a user, a changed file, a failed git push and a key rotation.
Every service that produces one calls ``announce_event`` after its commit, so an event is never
sent for a change that was rolled back, and a notifier that fails never fails the change. Events
carry ids, names and fixed words only: no password, token, file content or value.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.core.log import get_logger
from app.services.backup_events import BackupFinished, BackupNotifier
from app.services.deploy_events import DeployFinished, DeployNotifier
from app.services.settings_events import SettingsChanged, SettingsNotifier

log = get_logger("notify_events")


class UserChange(StrEnum):
    """What happened to a user account."""

    CREATED = "created"
    DISABLED = "disabled"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105
    PASSWORD_RESET = "password_reset"  # noqa: S105
    TWO_FACTOR_RESET = "two_factor_reset"


class FileChange(StrEnum):
    """How a managed file got a new version."""

    CREATED = "created"
    SAVED = "saved"
    RESTORED = "restored"
    IMPORTED = "imported"


class LockoutScope(StrEnum):
    """What a lockout applies to: a sign-in email, a client address, or the API from an address."""

    USER = "user"
    ADDRESS = "address"
    API_ADDRESS = "api_address"


@dataclass(frozen=True)
class SignInFromNewAddress:
    """A user signed in from an address they had not used before."""

    user_name: str
    address: str


@dataclass(frozen=True)
class LockoutStarted:
    """Repeated failures locked a name or an address.

    ``user_name`` is set only when the submitted name belongs to a user: people sometimes type a
    password into the name field, so any other text is never repeated.
    """

    scope: LockoutScope
    user_name: str | None
    address: str | None
    minutes: int


@dataclass(frozen=True)
class UserChanged:
    """An account was created, disabled, got a new password or lost its two-factor sign-in.

    ``actor`` is the label of who did it (a user name or ``cli``); ``group`` is set for a
    created account.
    """

    change: UserChange
    user_name: str
    actor: str
    group: str | None = None


@dataclass(frozen=True)
class FileChanged:
    """A managed file got a new version."""

    project_id: int
    deployment_id: int
    service_id: int
    environment_id: int
    environment_name: str
    version: int
    change: FileChange
    actor: str


@dataclass(frozen=True)
class GitPushFailed:
    """A push to the git repository failed after its retries. ``reason`` is a fixed phrase."""

    reason: str
    attempts: int


@dataclass(frozen=True)
class MasterKeyRotated:
    """The master key was replaced from the command line. It holds counts only, no key material."""

    versions: int
    secrets: int
    actor: str


NotificationEvent = (
    DeployFinished
    | BackupFinished
    | SettingsChanged
    | SignInFromNewAddress
    | LockoutStarted
    | UserChanged
    | FileChanged
    | GitPushFailed
    | MasterKeyRotated
)


class EventNotifier(Protocol):
    """Receives the events above."""

    async def notify(self, event: NotificationEvent) -> None:
        """Handle the event; the caller waits for nothing but this call."""
        ...


class FullNotifier(EventNotifier, DeployNotifier, SettingsNotifier, BackupNotifier, Protocol):
    """A notifier that takes every event: the ones above and those of the older seams."""


class NoEventNotifier:
    """The notifier a caller uses when none is given: it does nothing."""

    async def notify(self, event: NotificationEvent) -> None:
        """Ignore the event."""

    async def deploy_finished(self, event: DeployFinished) -> None:
        """Ignore the event."""

    async def settings_changed(self, event: SettingsChanged) -> None:
        """Ignore the event."""

    async def backup_finished(self, event: BackupFinished) -> None:
        """Ignore the event."""


NO_EVENT_NOTIFIER: FullNotifier = NoEventNotifier()


async def announce_event(notifier: EventNotifier, event: NotificationEvent) -> None:
    """Deliver an event; a notifier that fails is logged and never fails the change."""
    try:
        await notifier.notify(event)
    except Exception as error:
        log.error("notification failed: %s", type(error).__name__)
