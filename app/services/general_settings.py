"""The General settings: time zone, date format and the defaults of a new deployment.

The form values arrive as text and are checked together, so a refused save lists every problem.
The defaults only pre-fill the new deployment form; a deployment keeps its own values after it is
created, so changing a default never changes a deployment that exists.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.display_time import DATE_FORMATS, load_zone
from app.models import DistMissingPolicy, DriftPolicy, MissingTargetPolicy
from app.services.app_settings import GeneralSettings, SettingChange, save_section
from app.services.audit import AuditActor
from app.services.deployments import FILE_MODE_PATTERN, parse_account

DIST_MISSING_CHOICES = (("block", "Block"), ("warn", "Warn and continue"))
DRIFT_CHOICES = (
    ("halt", "Halt"),
    ("overwrite", "Overwrite"),
    ("backup_overwrite", "Back up then overwrite"),
)
MISSING_TARGET_CHOICES = (("create", "Create"), ("halt", "Halt"))


@dataclass(frozen=True)
class GeneralForm:
    """The General settings as typed."""

    time_zone: str = "UTC"
    date_format: str = "iso"
    dist_missing_policy: str = "block"
    drift_policy: str = "halt"
    missing_target_policy: str = "create"
    file_mode: str = "0600"
    file_owner: str = ""
    file_group: str = ""

    @classmethod
    def from_settings(cls, settings: GeneralSettings) -> GeneralForm:
        """The form values of the stored settings."""
        return cls(
            time_zone=settings.time_zone,
            date_format=settings.date_format,
            dist_missing_policy=settings.dist_missing_policy,
            drift_policy=settings.drift_policy,
            missing_target_policy=settings.missing_target_policy,
            file_mode=settings.file_mode,
            file_owner=settings.file_owner,
            file_group=settings.file_group,
        )


class GeneralFormError(Exception):
    """The values were refused; ``errors`` maps a field name to its message."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the safe messages by field."""
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def validate_general(form: GeneralForm) -> GeneralSettings:
    """The clean settings for the typed values, or raise ``GeneralFormError`` with every problem."""
    errors: dict[str, str] = {}
    zone = form.time_zone.strip()
    if load_zone(zone) is None:
        errors["time_zone"] = "Choose a time zone from the list."
    if form.date_format not in DATE_FORMATS:
        errors["date_format"] = "Choose a date format from the list."
    if form.dist_missing_policy not in {policy.value for policy in DistMissingPolicy}:
        errors["dist_missing_policy"] = "Choose one of the options."
    if form.drift_policy not in {policy.value for policy in DriftPolicy}:
        errors["drift_policy"] = "Choose one of the options."
    if form.missing_target_policy not in {policy.value for policy in MissingTargetPolicy}:
        errors["missing_target_policy"] = "Choose one of the options."
    mode = form.file_mode.strip()
    if FILE_MODE_PATTERN.fullmatch(mode) is None:
        errors["file_mode"] = "Use an octal mode such as 0600."
    owner = parse_account(form.file_owner, "Owner", errors, "file_owner")
    group = parse_account(form.file_group, "Group", errors, "file_group")
    if errors:
        raise GeneralFormError(errors)
    return GeneralSettings(
        time_zone=zone,
        date_format=form.date_format,
        dist_missing_policy=form.dist_missing_policy,
        drift_policy=form.drift_policy,
        missing_target_policy=form.missing_target_policy,
        file_mode=mode.zfill(4),
        file_owner=owner or "",
        file_group=group or "",
    )


async def save_general(
    db: AsyncSession, new: GeneralSettings, *, actor: AuditActor
) -> list[SettingChange]:
    """Store the changed general settings with one audit entry, and commit."""
    changes = await save_section(db, "general", new, actor=actor)
    await db.commit()
    return changes
