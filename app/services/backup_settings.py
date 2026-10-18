"""The Backup settings: the schedule and retention choices, checking the form and saving it.

The schedule and the retention are chosen from fixed lists. A retention choice sets a limit by
count or a limit by days; the stored settings keep both numbers, so both limits are honoured when
both are set, and the form also offers whatever combination is stored if it is not one of the
fixed choices, so saving an unchanged form never changes it. The extra destination is checked the
way a backup uses it: inside the allowed paths, an existing folder the application can write to.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.app_settings import BackupSettings, SettingChange, save_section
from app.services.audit import AuditActor
from app.services.backup_destination import ExtraDestinationError, check_extra_destination
from app.services.paths import AllowedPaths

SCHEDULES: dict[str, str] = {
    "off": "Off",
    "daily": "Daily at 02:00",
    "six_hours": "Every 6 hours",
    "weekly": "Weekly",
}
SCHEDULE_HINT = "Runs in the display time zone ({zone}). Weekly runs on Sunday at 02:00."
RETENTIONS: dict[str, tuple[str, int, int]] = {
    "count:7": ("Keep 7 backups", 7, 0),
    "count:14": ("Keep 14 backups", 14, 0),
    "count:30": ("Keep 30 backups", 30, 0),
    "days:30": ("Keep 30 days", 0, 30),
    "days:60": ("Keep 60 days", 0, 60),
    "days:90": ("Keep 90 days", 0, 90),
}
SCHEDULE_REFUSED = "Choose one of the listed schedules."
RETENTION_REFUSED = "Choose one of the listed retention limits."


@dataclass(frozen=True)
class BackupForm:
    """The Backup settings as typed: two choices and a folder."""

    schedule: str
    retention: str
    extra_destination: str

    @classmethod
    def from_settings(cls, settings: BackupSettings) -> BackupForm:
        """The form showing the stored settings."""
        return cls(settings.schedule, retention_key(settings), settings.extra_destination)


class BackupFormError(Exception):
    """The form has refused values; ``errors`` holds one message per field name."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the messages by field name."""
        super().__init__("the backup settings are invalid")
        self.errors = errors


def retention_key(settings: BackupSettings) -> str:
    """The form value that stands for the stored limits."""
    for key, (_label, count, days) in RETENTIONS.items():
        if (count, days) == (settings.retention_count, settings.retention_days):
            return key
    return f"custom:{settings.retention_count}:{settings.retention_days}"


def _custom_label(count: int, days: int) -> str:
    """The text of a stored combination that is not a fixed choice."""
    parts = []
    if count:
        parts.append(f"{count} backups")
    if days:
        parts.append(f"{days} days")
    return "Keep " + " and ".join(parts) if parts else "Keep everything"


def retention_options(settings: BackupSettings) -> list[tuple[str, str]]:
    """The retention choices, with the stored combination added when it is not a fixed one."""
    options = [(key, label) for key, (label, _count, _days) in RETENTIONS.items()]
    key = retention_key(settings)
    if key not in RETENTIONS:
        label = _custom_label(settings.retention_count, settings.retention_days)
        options.append((key, label))
    return options


def schedule_options() -> list[tuple[str, str]]:
    """The schedule choices as value and label."""
    return list(SCHEDULES.items())


def _retention_numbers(choice: str, stored: BackupSettings) -> tuple[int, int] | None:
    """The count and days a retention choice stands for, None when it is not offered."""
    if choice in RETENTIONS:
        _label, count, days = RETENTIONS[choice]
        return count, days
    if choice == retention_key(stored):
        return stored.retention_count, stored.retention_days
    return None


def validate_backup(
    form: BackupForm, *, stored: BackupSettings, allowed: AllowedPaths
) -> BackupSettings:
    """The settings the form stands for, or ``BackupFormError`` with a message per field.

    ``allowed`` is the allowed paths loaded for this request. The extra destination is stored as
    its real path.
    """
    errors: dict[str, str] = {}
    if form.schedule not in SCHEDULES:
        errors["schedule"] = SCHEDULE_REFUSED
    numbers = _retention_numbers(form.retention, stored)
    if numbers is None:
        errors["retention"] = RETENTION_REFUSED
    destination = form.extra_destination.strip()
    if destination:
        try:
            destination = str(check_extra_destination(allowed, destination))
        except ExtraDestinationError as error:
            errors["extra_destination"] = str(error)
    if errors or numbers is None:
        raise BackupFormError(errors)
    return BackupSettings(
        schedule=form.schedule,
        retention_count=numbers[0],
        retention_days=numbers[1],
        extra_destination=destination,
    )


async def save_backup(
    db: AsyncSession, new: BackupSettings, *, actor: AuditActor
) -> list[SettingChange]:
    """Store the changed backup settings with one audit entry, and commit."""
    changes = await save_section(db, "backup", new, actor=actor)
    await db.commit()
    return changes
