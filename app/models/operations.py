"""Allowed host paths and backup records."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, enum_values, utc_now


class BackupTrigger(StrEnum):
    """What caused a backup."""

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    MIGRATION = "migration"
    CLI = "cli"


class BackupStatus(StrEnum):
    """Outcome of a backup."""

    SUCCESS = "success"
    FAILURE = "failure"


class AllowedPath(Base):
    """A host path the application may use, together with all its descendants."""

    __tablename__ = "allowed_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(4096), unique=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Backup(Base):
    """A database snapshot, stored in the data volume and optionally copied elsewhere."""

    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    trigger: Mapped[BackupTrigger] = mapped_column(
        Enum(BackupTrigger, native_enum=False, length=10, values_callable=enum_values)
    )
    status: Mapped[BackupStatus] = mapped_column(
        Enum(BackupStatus, native_enum=False, length=10, values_callable=enum_values)
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    extra_destination_path: Mapped[str | None] = mapped_column(String(4096), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
