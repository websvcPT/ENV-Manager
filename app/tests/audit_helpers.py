"""Readers of the audit log shared by the tests: the entries of an action, oldest first."""

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditEntry


async def audit_entries(db: AsyncSession, action: str | None = None) -> list[AuditEntry]:
    """The audit entries of one action (every entry without one), oldest first."""
    statement = select(AuditEntry).order_by(AuditEntry.id)
    if action is not None:
        statement = statement.where(AuditEntry.action == action)
    return list((await db.execute(statement)).scalars())


async def audit_entries_from(
    session_factory: async_sessionmaker[AsyncSession], action: str | None = None
) -> list[AuditEntry]:
    """The same, read through a session of its own from a session factory."""
    async with session_factory() as db:
        return await audit_entries(db, action)


async def web_audit_entries(web_app: FastAPI, action: str | None = None) -> list[AuditEntry]:
    """The same, read through a session of the running application."""
    return await audit_entries_from(web_app.state.session_factory, action)
