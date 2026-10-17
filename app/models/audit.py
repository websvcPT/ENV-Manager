"""Append-only audit entries."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, enum_values, utc_now


class AuditActorType(StrEnum):
    """Who performed an audited action."""

    USER = "user"
    API = "api"
    CLI = "cli"
    SYSTEM = "system"


class AuditResult(StrEnum):
    """Outcome recorded on an audit entry."""

    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"
    HALTED = "halted"


class AuditEntry(Base):
    """One audited event. Entries are never updated and outlive the objects they describe.

    Actor and target names are copied into the row, and the scope ids are plain integers without
    foreign keys, so deleting a user, token or structure item keeps the history readable. The
    details document must never contain secret values.
    """

    __tablename__ = "audit_entries"
    __table_args__ = (
        Index("ix_audit_entries_created_at", "created_at"),
        Index("ix_audit_entries_action", "action"),
        Index("ix_audit_entries_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    actor_type: Mapped[AuditActorType] = mapped_column(
        Enum(AuditActorType, native_enum=False, length=10, values_callable=enum_values)
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    actor_token_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="SET NULL"), default=None, index=True
    )
    actor_name: Mapped[str | None] = mapped_column(String(100), default=None)
    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)
    action: Mapped[str] = mapped_column(String(64))
    result: Mapped[AuditResult] = mapped_column(
        Enum(AuditResult, native_enum=False, length=10, values_callable=enum_values)
    )
    target_type: Mapped[str | None] = mapped_column(String(32), default=None)
    target_id: Mapped[int | None] = mapped_column(default=None)
    target_name: Mapped[str | None] = mapped_column(String(255), default=None)
    project_id: Mapped[int | None] = mapped_column(default=None)
    deployment_id: Mapped[int | None] = mapped_column(default=None)
    service_id: Mapped[int | None] = mapped_column(default=None)
    environment_id: Mapped[int | None] = mapped_column(default=None)
    version_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(32), default=None)
