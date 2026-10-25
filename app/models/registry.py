"""The Docker registries the application holds a login for."""

from datetime import datetime

from sqlalchemy import Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import UtcDateTime, utc_now


class DockerRegistry(Base):
    """A registry host with the login used to pull from it.

    ``host`` is stored normalized (lowercase, ``host`` or ``host:port``) and is unique ignoring
    case through the functional index below. ``token_encrypted`` is the password or token sealed
    with the master key under a context bound to the row id (see ``app.services.registries``);
    ids are never reused, so a sealed token cannot be taken over by a later row.
    """

    __tablename__ = "docker_registries"
    __table_args__ = (
        Index("uq_docker_registries_host_lower", text("lower(host)"), unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(300))
    username: Mapped[str] = mapped_column(String(255))
    token_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)
