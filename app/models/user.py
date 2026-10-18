"""User accounts, server-side sessions and login throttling counters."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, enum_values, utc_now


class UserGroup(StrEnum):
    """The three fixed permission groups."""

    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    VIEWER = "viewer"


class LoginCounterKind(StrEnum):
    """What a login failure counter is keyed on."""

    USER = "user"
    IP = "ip"


class User(Base):
    """A person or service account that can sign in. Disabled users are kept for audit.

    Email addresses are unique ignoring case through the functional index below, so the same
    address in another case is refused by the database too. Names are unique ignoring case by
    the user service.
    """

    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    group: Mapped[UserGroup] = mapped_column(
        Enum(UserGroup, native_enum=False, length=20, values_callable=enum_values)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[str | None] = mapped_column(Text, default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_step: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class UserSession(Base):
    """A server-side session. Only the hash of the opaque cookie token is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)


class LoginCounter(Base):
    """Failed login counter per user name or client address, persisted across restarts."""

    __tablename__ = "login_counters"
    __table_args__ = (UniqueConstraint("kind", "subject", name="uq_login_counters_kind_subject"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[LoginCounterKind] = mapped_column(
        Enum(LoginCounterKind, native_enum=False, length=10, values_callable=enum_values)
    )
    subject: Mapped[str] = mapped_column(String(320))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class PendingLogin(Base):
    """A password-verified login waiting for its second factor code.

    Single use and short lived. Only the hash of the opaque token (held in its own cookie) is
    stored, so the credentials never travel through the code step.
    """

    __tablename__ = "pending_logins"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
