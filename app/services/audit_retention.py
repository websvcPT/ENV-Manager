"""The audit retention job: delete audit entries older than the retention set in General settings.

A retention of zero days keeps every entry. Entries are deleted in batches so a long backlog never
holds the database write lock for one large statement, and one ``audit.purged`` entry then records
how many went and the cutoff. Nothing is written when nothing was old enough. An entry exactly at
the cutoff stays: only entries strictly older than the retention are removed.
"""

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEntry, AuditResult
from app.models.types import utc_now
from app.services.app_settings import load_stored_settings
from app.services.audit import AuditActor, record_actor_audit

PURGE_ACTION = "audit.purged"
PURGE_BATCH = 1000


async def purge_old_audit_entries(
    db: AsyncSession,
    actor: AuditActor,
    *,
    now: datetime | None = None,
    batch_size: int = PURGE_BATCH,
) -> int:
    """Delete the entries older than the stored retention and audit the purge. Returns the count.

    The retention is read fresh from the stored settings. The caller's session is committed after
    every batch and after the purge entry.
    """
    days = (await load_stored_settings(db)).general.audit_retention_days
    if days <= 0:
        return 0
    cutoff = (now or utc_now()) - timedelta(days=days)
    deleted = 0
    while True:
        batch = list(
            (
                await db.execute(
                    select(AuditEntry.id)
                    .where(AuditEntry.created_at < cutoff)
                    .order_by(AuditEntry.id)
                    .limit(batch_size)
                )
            ).scalars()
        )
        if not batch:
            break
        await db.execute(delete(AuditEntry).where(AuditEntry.id.in_(batch)))
        await db.commit()
        deleted += len(batch)
    if deleted:
        await record_actor_audit(
            db,
            actor,
            action=PURGE_ACTION,
            result=AuditResult.SUCCESS,
            details={
                "deleted": deleted,
                "cutoff": cutoff.isoformat(),
                "retention_days": days,
            },
        )
        await db.commit()
    return deleted
