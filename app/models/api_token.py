"""API tokens owned by users."""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, utc_now


class ApiToken(Base):
    """A bearer token. The secret is shown once at creation and only its hash is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    operations: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_used_ip: Mapped[str | None] = mapped_column(String(45), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
