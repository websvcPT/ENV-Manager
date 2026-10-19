"""Filters and the paging cursor of the audit log, parsed leniently from query strings.

Every value arrives as text and a value that does not parse is ignored, never refused: the same
URL must answer the same whether a project or user exists, is hidden or was made up, and a page
or fragment must never turn a bad query into an error answer that htmx would swap into the table.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from urllib.parse import urlencode

from sqlalchemy import ColumnElement, and_, or_

from app.models import AuditActorType, AuditEntry, AuditResult

MAX_NAME_LENGTH = 100
MAX_ACTION_LENGTH = 64
FIRST_DATE = date(1970, 1, 1)
LAST_DATE = date(9998, 12, 31)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_NUMBER = re.compile(r"^[0-9]{1,15}$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ACTION = re.compile(r"^[a-z0-9_.-]+$")
_CURSOR = re.compile(r"^([0-9]{1,18})-([0-9]{1,15})$")


def parse_number(raw: str | None) -> int | None:
    """A positive whole number from text, or None."""
    if raw is None or not _NUMBER.match(raw):
        return None
    number = int(raw)
    return number if number > 0 else None


def _parse_date(raw: str | None) -> date | None:
    """A calendar day written as YYYY-MM-DD, or None for anything else."""
    if raw is None or not _DATE.match(raw):
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    return day if FIRST_DATE <= day <= LAST_DATE else None


def _parse_actor(raw: str | None) -> tuple[AuditActorType, int | str] | None:
    """An actor filter, or None.

    A user is written ``user:<id>`` (names are not unique, so the id is the key), a token, the
    command line or the system ``type:name`` (for example ``api:token billing-ci``).
    """
    if not raw:
        return None
    kind, separator, reference = raw.partition(":")
    if (
        not separator
        or not reference
        or len(reference) > MAX_NAME_LENGTH
        or not reference.isprintable()
    ):
        return None
    try:
        actor_type = AuditActorType(kind)
    except ValueError:
        return None
    if actor_type is AuditActorType.USER:
        user_id = parse_number(reference)
        return (actor_type, user_id) if user_id is not None else None
    return actor_type, reference


def _parse_result(raw: str | None) -> AuditResult | None:
    """A result filter, or None for an unknown value."""
    try:
        return AuditResult(raw) if raw else None
    except ValueError:
        return None


def _parse_action(raw: str | None) -> str | None:
    """An action filter such as ``environment.save``, or None."""
    if not raw or len(raw) > MAX_ACTION_LENGTH or not _ACTION.match(raw):
        return None
    return raw


@dataclass(frozen=True)
class AuditFilters:
    """The filters of one audit log view. A missing value means no restriction."""

    project_id: int | None = None
    actor_type: AuditActorType | None = None
    actor_user_id: int | None = None
    actor_name: str | None = None
    action: str | None = None
    result: AuditResult | None = None
    date_from: date | None = None
    date_to: date | None = None
    zone: tzinfo = UTC

    @classmethod
    def parse(cls, raw: Mapping[str, str], zone: tzinfo = UTC) -> AuditFilters:
        """Filters from query parameters ``project``, ``actor``, ``action``, ``result``, ``from``
        and ``to``. Whatever does not parse is dropped. ``zone`` is the time zone the dates are
        days of."""
        actor_type, reference = _parse_actor(raw.get("actor")) or (None, None)
        return cls(
            project_id=parse_number(raw.get("project")),
            actor_type=actor_type,
            actor_user_id=reference if isinstance(reference, int) else None,
            actor_name=reference if isinstance(reference, str) else None,
            action=_parse_action(raw.get("action")),
            result=_parse_result(raw.get("result")),
            date_from=_parse_date(raw.get("from")),
            date_to=_parse_date(raw.get("to")),
            zone=zone,
        )

    @property
    def actor(self) -> str | None:
        """The actor in the form the filter select and the URL use."""
        reference = self.actor_user_id if self.actor_user_id is not None else self.actor_name
        if self.actor_type is None or reference is None:
            return None
        return f"{self.actor_type.value}:{reference}"

    def params(self) -> dict[str, str]:
        """The non-empty filters as query parameters, in a fixed order."""
        found = {
            "project": str(self.project_id) if self.project_id else None,
            "actor": self.actor,
            "action": self.action,
            "result": self.result.value if self.result else None,
            "from": self.date_from.isoformat() if self.date_from else None,
            "to": self.date_to.isoformat() if self.date_to else None,
        }
        return {key: value for key, value in found.items() if value}

    def query_string(self, **extra: str) -> str:
        """The filters (plus extra parameters) as an encoded query string, empty when none."""
        return urlencode({**self.params(), **extra})

    @property
    def active(self) -> bool:
        """Whether any filter restricts the entries."""
        return bool(self.params())

    def conditions(self) -> list[ColumnElement[bool]]:
        """The filters as conditions over ``AuditEntry``.

        Dates are calendar days of the display zone, the last one inclusive.
        """
        found: list[ColumnElement[bool]] = []
        if self.project_id is not None:
            found.append(AuditEntry.project_id == self.project_id)
        if self.actor_type is not None and self.actor_user_id is not None:
            found.append(
                and_(
                    AuditEntry.actor_type == self.actor_type,
                    AuditEntry.actor_user_id == self.actor_user_id,
                )
            )
        elif self.actor_type is not None and self.actor_name is not None:
            found.append(
                and_(
                    AuditEntry.actor_type == self.actor_type,
                    AuditEntry.actor_name == self.actor_name,
                )
            )
        if self.action is not None:
            found.append(AuditEntry.action == self.action)
        if self.result is not None:
            found.append(AuditEntry.result == self.result)
        if self.date_from is not None:
            start = datetime.combine(self.date_from, time.min, self.zone)
            found.append(AuditEntry.created_at >= start)
        if self.date_to is not None and self.date_to < LAST_DATE:
            end = datetime.combine(self.date_to + timedelta(days=1), time.min, self.zone)
            found.append(AuditEntry.created_at < end)
        return found


@dataclass(frozen=True)
class Cursor:
    """Where the next page starts: the time and id of the last entry shown.

    Entries are ordered by time and then id, and seeded or imported entries can have a time that
    does not follow their id, so the cursor carries both. It names no entry to look up.
    """

    created_at: datetime
    entry_id: int

    def encode(self) -> str:
        """The cursor as a URL-safe token."""
        micros = (self.created_at - EPOCH) // timedelta(microseconds=1)
        return f"{micros}-{self.entry_id}"

    @classmethod
    def parse(cls, raw: str | None) -> Cursor | None:
        """A cursor from its token, or None when it is missing or malformed."""
        found = _CURSOR.match(raw or "")
        if found is None:
            return None
        try:
            moment = EPOCH + timedelta(microseconds=int(found.group(1)))
        except OverflowError:
            return None
        return cls(moment, int(found.group(2)))

    def after_condition(self) -> ColumnElement[bool]:
        """Entries older than the cursor in the display order (newest first)."""
        return or_(
            AuditEntry.created_at < self.created_at,
            and_(AuditEntry.created_at == self.created_at, AuditEntry.id < self.entry_id),
        )
