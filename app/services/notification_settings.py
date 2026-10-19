"""The Notifications settings: the event matrix, checking the form and saving it.

Each row of the matrix is an event kind with a switch, and the kinds about a project also have a
recipients choice. A checkbox that is not sent is off, which is how a browser submits a cleared
box, so the form is the whole state of the matrix.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.app_settings import NotificationSettings, SettingChange, save_section
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

RECIPIENTS_REFUSED = "Choose one of the listed recipients."


@dataclass(frozen=True)
class NotificationsForm:
    """The matrix as submitted: the kinds switched on and the recipients chosen per project kind."""

    enabled: frozenset[EventKind]
    recipients: Mapping[EventKind, str]

    @classmethod
    def from_settings(cls, settings: NotificationSettings) -> NotificationsForm:
        """The form showing the stored matrix."""
        return cls(
            frozenset(row.kind for row in EVENT_ROWS if is_enabled(settings, row.kind)),
            {kind: recipients_of(settings, kind).value for kind in PROJECT_KINDS},
        )

    @classmethod
    def from_submission(cls, submitted: Mapping[str, str]) -> NotificationsForm:
        """The form from the posted fields: ``<kind>`` is present when the box is checked and
        ``<kind>_to`` holds the recipients of a project kind."""
        return cls(
            frozenset(row.kind for row in EVENT_ROWS if row.kind.value in submitted),
            {kind: submitted.get(f"{kind.value}_to", "") for kind in PROJECT_KINDS},
        )


class NotificationsFormError(Exception):
    """The form has refused values; ``errors`` holds one message per kind that has one."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the messages by the name of the field."""
        super().__init__("the notification settings are invalid")
        self.errors = errors


def recipient_choices() -> list[tuple[str, str]]:
    """The recipients choices as value and label."""
    return list(RECIPIENT_LABELS.items())


def validate_notifications(
    form: NotificationsForm, *, stored: NotificationSettings
) -> NotificationSettings:
    """The settings the form stands for, or ``NotificationsFormError`` naming the bad rows.

    Fields the form does not cover keep their stored value.
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
    if errors:
        raise NotificationsFormError(errors)
    return replace(stored, **values)  # type: ignore[arg-type]


async def save_notifications(
    db: AsyncSession, new: NotificationSettings, *, actor: AuditActor
) -> list[SettingChange]:
    """Store the changed switches and recipients with one audit entry, and commit."""
    changes = await save_section(db, "notifications", new, actor=actor)
    await db.commit()
    return changes
