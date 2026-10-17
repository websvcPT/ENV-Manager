"""Key/value application settings stored in the database."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

KEY_CHECK_SETTING = "key_check"


def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


class AppSetting(Base):
    """One named setting. Sensitive values are stored as ciphertext by their writers."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
