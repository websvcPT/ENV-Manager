"""Shared column types and helpers for the models."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that SQLite returns aware, always in UTC.

    SQLite has no timezone support, so values are normalized to UTC on the way in and tagged
    as UTC on the way out. Naive values are refused to avoid silent local-time bugs.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Store aware datetimes as UTC and refuse naive ones."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not accepted, use an aware UTC value")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Mark stored values as UTC."""
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def enum_values(enum_class: type[StrEnum]) -> list[str]:
    """Store enum values, not member names."""
    return [member.value for member in enum_class]
