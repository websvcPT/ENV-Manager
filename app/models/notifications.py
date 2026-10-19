"""Records the notification feature keeps: addresses a user signed in from, and delivery health."""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, utc_now


class UserSeenAddress(Base):
    """A client address a user has signed in from, kept only as a keyed hash.

    The hash is an HMAC of the address under a subkey of the master key, so the table shows
    neither the addresses nor, without the key, lets anyone test a guess. The newest rows per
    user are kept and the rest are pruned, so the table stays small.
    """

    __tablename__ = "user_seen_addresses"
    __table_args__ = (UniqueConstraint("user_id", "address_hash", name="uq_seen_address_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    address_hash: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class NotificationStatus(Base):
    """Delivery health of one channel: failed deliveries since the last one that worked.

    A row exists per channel only while it has failures; a delivery that works removes it. The
    banner for superadmins reads it, and it lives in the database so a delivery that failed in
    the command line process counts too. ``reason`` is a fixed phrase, never an exception text.
    """

    __tablename__ = "notification_status"

    channel: Mapped[str] = mapped_column(String(20), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    reason: Mapped[str] = mapped_column(String(200), default="")
