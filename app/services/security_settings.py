"""The Security settings: checking the form and what a changed timeout does to live sessions.

Every number is a whole number inside a range chosen so a typing mistake cannot lock everyone out
or leave sessions open for months. The absolute timeout applies to sessions that exist already:
saving it recomputes the expiry of the sessions that are still open in the same transaction, so a
shorter limit ends old sessions at their next request and a longer one lets them run on. A session
that had run out already stays ended.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserSession
from app.models.types import utc_now
from app.services.app_settings import SecuritySettings, SettingChange, save_section
from app.services.audit import AuditActor

ABSOLUTE_SETTING = "security.absolute_timeout_hours"
_WHOLE_NUMBER = re.compile(r"[0-9]{1,6}")


@dataclass(frozen=True)
class Bounds:
    """The accepted range of one setting."""

    low: int
    high: int

    @property
    def message(self) -> str:
        """The refusal text for a value outside the range or not a whole number."""
        return f"Enter a whole number from {self.low} to {self.high}."


BOUNDS: dict[str, Bounds] = {
    "idle_timeout_minutes": Bounds(5, 1440),
    "absolute_timeout_hours": Bounds(1, 168),
    "user_max_failures": Bounds(1, 1000),
    "ip_max_failures": Bounds(1, 10000),
    "api_ip_max_failures": Bounds(1, 10000),
    "lockout_minutes": Bounds(1, 1440),
    "min_password_length": Bounds(8, 128),
}
IDLE_LONGER_MESSAGE = "The idle timeout cannot be longer than the absolute timeout."


@dataclass(frozen=True)
class SecurityForm:
    """The Security settings as typed."""

    idle_timeout_minutes: str
    absolute_timeout_hours: str
    user_max_failures: str
    ip_max_failures: str
    api_ip_max_failures: str
    lockout_minutes: str
    min_password_length: str
    mask_container_logs: bool

    @classmethod
    def from_settings(cls, settings: SecuritySettings) -> SecurityForm:
        """The form values of the stored settings."""
        return cls(
            idle_timeout_minutes=str(settings.idle_timeout_minutes),
            absolute_timeout_hours=str(settings.absolute_timeout_hours),
            user_max_failures=str(settings.user_max_failures),
            ip_max_failures=str(settings.ip_max_failures),
            api_ip_max_failures=str(settings.api_ip_max_failures),
            lockout_minutes=str(settings.lockout_minutes),
            min_password_length=str(settings.min_password_length),
            mask_container_logs=settings.mask_container_logs,
        )


class SecurityFormError(Exception):
    """The values were refused; ``errors`` maps a field name to its message."""

    def __init__(self, errors: dict[str, str]) -> None:
        """Hold the safe messages by field."""
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def _number(raw: str, name: str, errors: dict[str, str]) -> int | None:
    """A whole number inside the range of ``name``, or None with the refusal recorded."""
    text = raw.strip()
    bounds = BOUNDS[name]
    if _WHOLE_NUMBER.fullmatch(text) is None or not bounds.low <= int(text) <= bounds.high:
        errors[name] = bounds.message
        return None
    return int(text)


def validate_security(
    form: SecurityForm, *, stored: SecuritySettings, address_limit_fixed: bool
) -> SecuritySettings:
    """The clean settings for the typed values, or raise ``SecurityFormError`` with every problem.

    When the sign-in limit per address is fixed by the startup override its field is not
    editable and the stored value is kept whatever was posted.
    """
    errors: dict[str, str] = {}
    values: dict[str, int] = {}
    for name in BOUNDS:
        if name == "ip_max_failures" and address_limit_fixed:
            values[name] = stored.ip_max_failures
            continue
        number = _number(getattr(form, name), name, errors)
        if number is not None:
            values[name] = number
    if (
        "idle_timeout_minutes" in values
        and "absolute_timeout_hours" in values
        and values["idle_timeout_minutes"] > values["absolute_timeout_hours"] * 60
    ):
        errors["idle_timeout_minutes"] = IDLE_LONGER_MESSAGE
    if errors:
        raise SecurityFormError(errors)
    return SecuritySettings(**values, mask_container_logs=form.mask_container_logs)


async def apply_absolute_timeout(
    db: AsyncSession, hours: int, *, now: datetime | None = None
) -> int:
    """Recompute the expiry of every live session from its start. Returns how many were set.

    A session that already ran out stays ended, whatever the new limit is: it is only waiting for
    its row to be purged, and a longer limit must not bring it back.
    """
    moment = now or utc_now()
    limit = timedelta(hours=hours)
    live = select(UserSession).where(UserSession.expires_at > moment)
    sessions = list((await db.execute(live)).scalars())
    for session in sessions:
        session.expires_at = session.created_at + limit
    return len(sessions)


async def save_security(
    db: AsyncSession, new: SecuritySettings, *, actor: AuditActor
) -> list[SettingChange]:
    """Store the changed security settings with one audit entry, and commit.

    A changed absolute timeout reaches the sessions that exist in the same transaction.
    """
    changes = await save_section(db, "security", new, actor=actor)
    if any(change.setting == ABSOLUTE_SETTING for change in changes):
        await apply_absolute_timeout(db, new.absolute_timeout_hours)
    await db.commit()
    return changes
