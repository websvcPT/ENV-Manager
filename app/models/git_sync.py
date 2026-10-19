"""The state of git sync: what was last pushed, how the last attempts went, the pinned host key."""

from datetime import datetime

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime

SYNC_STATE_ID = 1


class GitSyncState(Base):
    """The single row that tracks git sync (its id is always ``SYNC_STATE_ID``).

    ``watermark`` is the change mark of the export that was last pushed (or found identical to
    the repository), so a push happens only when the mark has moved. ``failures`` counts the runs
    that failed since the last one that worked and ``retry_at`` is when the next attempt may
    start, which grows with every failure. ``last_error`` is a fixed phrase, never output of git.
    ``failure_notified_at`` is when the last failure notice of the current streak went out (empty
    when none did), which decides whether the next failure notifies and whether a success sends
    the recovered notice.
    The pinned host key belongs to ``host_key_host`` (the name the SSH client looks it up by) and
    holds a known_hosts line. Nothing here is a secret.
    """

    __tablename__ = "git_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    watermark: Mapped[str | None] = mapped_column(String(64), default=None)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(String(200), default=None)
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    failure_notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    host_key_host: Mapped[str | None] = mapped_column(String(300), default=None)
    host_key: Mapped[str | None] = mapped_column(Text, default=None)
    host_key_fingerprint: Mapped[str | None] = mapped_column(String(100), default=None)
    host_key_pinned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
