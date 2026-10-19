"""The Notifications settings: the event matrix and the webhook, checking the form and saving it.

Each row of the matrix is an event kind with a switch, and the kinds about a project also have a
recipients choice. A checkbox that is not sent is off, which is how a browser submits a cleared
box, so the form is the whole state of the matrix.

The webhook has an address, a switch and a shared secret. The address and the secret are both
stored as secrets and are never shown back (chat tools put their credential in the address): the
form carries one only when someone types a new one, an empty field keeps the stored value, and a
separate choice removes it. The screen shows the scheme and host of the stored address and nothing
more. The switch can only be on while an address and a secret are set. Saving anything about the
webhook starts its delivery health afresh, since a failure counted under the old settings says
nothing about the new ones.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, CryptoService
from app.services.app_settings import (
    WEBHOOK_SECRET_KEY,
    WEBHOOK_URL_KEY,
    NotificationSettings,
    SettingChange,
    clear_secret,
    read_secret,
    save_section,
    url_origin,
    write_secret,
)
from app.services.audit import AuditActor
from app.services.notify_catalog import (
    EVENT_ROWS,
    PROJECT_KINDS,
    RECIPIENT_LABELS,
    EventKind,
    Recipients,
    is_enabled,
    recipients_of,
)
from app.services.notify_status import WEBHOOK_CHANNEL, clear_failures
from app.services.webhook import secret_problem, url_problem

RECIPIENTS_REFUSED = "Choose one of the listed recipients."
URL_REQUIRED = "Enter the address to switch the webhook on."
URL_CONFLICT = "Type a new address or remove the stored one, not both."
SECRET_REQUIRED = "Enter a shared secret to switch the webhook on."  # noqa: S105
SECRET_CONFLICT = "Type a new secret or remove the stored one, not both."  # noqa: S105
WEBHOOK_FIELDS = frozenset({"webhook_enabled"})


@dataclass(frozen=True)
class NotificationsForm:
    """The tab as submitted: the kinds switched on, the recipients chosen per project kind and
    the webhook fields. ``webhook_url`` and ``webhook_secret`` are empty to keep what is stored
    and ``remove_webhook_url`` and ``remove_webhook_secret`` ask to delete it."""

    enabled: frozenset[EventKind]
    recipients: Mapping[EventKind, str]
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_secret: str = ""
    remove_webhook_url: bool = False
    remove_webhook_secret: bool = False

    @classmethod
    def from_settings(cls, settings: NotificationSettings) -> NotificationsForm:
        """The form showing the stored settings, with the address and secret fields empty."""
        return cls(
            frozenset(row.kind for row in EVENT_ROWS if is_enabled(settings, row.kind)),
            {kind: recipients_of(settings, kind).value for kind in PROJECT_KINDS},
            settings.webhook_enabled,
        )

    @classmethod
    def from_submission(cls, submitted: Mapping[str, str]) -> NotificationsForm:
        """The form from the posted fields: ``<kind>`` is present when the box is checked,
        ``<kind>_to`` holds the recipients of a project kind, and the webhook has its own."""
        return cls(
            frozenset(row.kind for row in EVENT_ROWS if row.kind.value in submitted),
            {kind: submitted.get(f"{kind.value}_to", "") for kind in PROJECT_KINDS},
            "webhook_enabled" in submitted,
            submitted.get("webhook_url", ""),
            submitted.get("webhook_secret", ""),
            "remove_webhook_url" in submitted,
            "remove_webhook_secret" in submitted,
        )


@dataclass(frozen=True)
class CleanNotifications:
    """A checked form: the settings to store and what to do with the webhook address and secret."""

    settings: NotificationSettings
    url: str
    remove_url: bool
    secret: str
    remove_secret: bool


class NotificationsFormError(Exception):
    """The form has refused values; ``errors`` holds one message per field name."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the messages by the name of the field."""
        super().__init__("the notification settings are invalid")
        self.errors = errors


def recipient_choices() -> list[tuple[str, str]]:
    """The recipients choices as value and label."""
    return list(RECIPIENT_LABELS.items())


def _webhook_errors(
    form: NotificationsForm, *, url_stored: bool, secret_stored: bool
) -> dict[str, str]:
    """The refusals of the webhook fields, by field name."""
    errors: dict[str, str] = {}
    url = form.webhook_url.strip()
    if url and (problem := url_problem(url)):
        errors["webhook_url"] = problem
    elif url and form.remove_webhook_url:
        errors["webhook_url"] = URL_CONFLICT
    if form.webhook_secret and (problem := secret_problem(form.webhook_secret)):
        errors["webhook_secret"] = problem
    elif form.webhook_secret and form.remove_webhook_secret:
        errors["webhook_secret"] = SECRET_CONFLICT
    if form.webhook_enabled:
        keeps_url = url_stored and not form.remove_webhook_url
        if not url and not keeps_url:
            errors.setdefault("webhook_url", URL_REQUIRED)
        keeps_secret = secret_stored and not form.remove_webhook_secret
        if not form.webhook_secret and not keeps_secret:
            errors.setdefault("webhook_secret", SECRET_REQUIRED)
    return errors


def validate_notifications(
    form: NotificationsForm,
    *,
    stored: NotificationSettings,
    url_stored: bool,
    secret_stored: bool,
) -> CleanNotifications:
    """The settings the form stands for, or ``NotificationsFormError`` naming the bad fields.

    Fields the form does not cover keep their stored value. ``url_stored`` and ``secret_stored``
    say whether a webhook address and a shared secret are stored now, which decides whether the
    webhook can be switched on.
    """
    errors: dict[str, str] = {}
    values: dict[str, object] = {}
    for row in EVENT_ROWS:
        values[row.kind.value] = row.kind in form.enabled
        if not row.project:
            continue
        choice = form.recipients.get(row.kind, "")
        if choice not in {item.value for item in Recipients}:
            errors[f"{row.kind.value}_to"] = RECIPIENTS_REFUSED
        values[f"{row.kind.value}_to"] = choice
    errors.update(_webhook_errors(form, url_stored=url_stored, secret_stored=secret_stored))
    if errors:
        raise NotificationsFormError(errors)
    values["webhook_enabled"] = form.webhook_enabled
    return CleanNotifications(
        replace(stored, **values),  # type: ignore[arg-type]
        form.webhook_url.strip(),
        form.remove_webhook_url,
        form.webhook_secret,
        form.remove_webhook_secret,
    )


async def stored_webhook_origin(db: AsyncSession, crypto: CryptoService) -> str:
    """The scheme, host and port of the stored webhook address, empty when there is none.

    The address is opened only to cut the origin from it; nothing else of it leaves this call.
    An address that cannot be opened with the current key reads as empty.
    """
    try:
        return url_origin(await read_secret(db, crypto, WEBHOOK_URL_KEY))
    except CryptoError:
        return ""


async def save_notifications(
    db: AsyncSession, crypto: CryptoService, clean: CleanNotifications, *, actor: AuditActor
) -> bool:
    """Store the changed switches, recipients and webhook settings and commit.

    The settings are audited together, each secret change on its own entry (its name only).
    Returns whether anything changed. Delivery health of the webhook starts afresh when the
    address, the switch or the secret changed.
    """
    changes: list[SettingChange] = await save_section(
        db, "notifications", clean.settings, actor=actor
    )
    secret_changed = False
    for key, value, remove in (
        (WEBHOOK_URL_KEY, clean.url, clean.remove_url),
        (WEBHOOK_SECRET_KEY, clean.secret, clean.remove_secret),
    ):
        if value:
            await write_secret(db, crypto, key, value, actor=actor)
            secret_changed = True
        elif remove and await clear_secret(db, key, actor=actor):
            secret_changed = True
    webhook_changed = secret_changed or any(
        change.setting.removeprefix("notifications.") in WEBHOOK_FIELDS for change in changes
    )
    if webhook_changed:
        await clear_failures(db, WEBHOOK_CHANNEL)
    await db.commit()
    return bool(changes) or secret_changed
