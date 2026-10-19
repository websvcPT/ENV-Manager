"""Delivery health: how many notifications failed since one last worked, per channel.

A failure is counted by one upsert, so deliveries that fail together are all counted, and a
delivery that works removes the row. The state is in the database, so a failure in the command
line process counts as well, and the banner shown to superadmins reads it from there. The reason
is a fixed phrase, never the text of an exception.
"""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationStatus

EMAIL_CHANNEL = "email"
MAX_REASON_LENGTH = 200


async def record_failure(
    db: AsyncSession, channel: str, reason: str, now: datetime, count: int = 1
) -> None:
    """Count failed deliveries on the channel and keep the latest reason. The caller commits."""
    upsert = insert(NotificationStatus).values(
        channel=channel, failures=count, last_failure_at=now, reason=reason[:MAX_REASON_LENGTH]
    )
    await db.execute(
        upsert.on_conflict_do_update(
            index_elements=[NotificationStatus.channel],
            set_={
                "failures": NotificationStatus.failures + count,
                "last_failure_at": now,
                "reason": upsert.excluded.reason,
            },
        )
    )


async def clear_failures(db: AsyncSession, channel: str) -> None:
    """Forget the failures of a channel, because a delivery worked or its settings changed.

    The caller commits.
    """
    await db.execute(delete(NotificationStatus).where(NotificationStatus.channel == channel))


async def load_failures(db: AsyncSession) -> list[NotificationStatus]:
    """The channels that have failures, in a fixed order."""
    return list(
        (await db.execute(select(NotificationStatus).order_by(NotificationStatus.channel)))
        .scalars()
        .all()
    )
