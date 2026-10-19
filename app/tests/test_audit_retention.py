"""Tests for the audit retention purge."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.db import create_session_factory
from app.models import AuditActorType, AuditEntry, AuditResult
from app.services.app_settings import GeneralSettings, save_section
from app.services.audit import AuditActor, record_audit
from app.services.audit_retention import PURGE_ACTION, purge_old_audit_entries

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
ACTOR = AuditActor.system("scheduler")


@pytest.fixture
def factory(seeded_database: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions on the seeded database."""
    return create_session_factory(seeded_database)


async def prepare(
    factory: async_sessionmaker[AsyncSession], days: int, ages: list[timedelta]
) -> None:
    """Empty the log, set the retention and add one entry per age before ``NOW``."""
    async with factory() as db:
        await db.execute(delete(AuditEntry))
        await save_section(db, "general", GeneralSettings(audit_retention_days=days), actor=ACTOR)
        await db.execute(delete(AuditEntry))
        for position, age in enumerate(ages):
            await record_audit(
                db,
                action=f"test.entry{position}",
                result=AuditResult.SUCCESS,
                actor_type=AuditActorType.CLI,
                created_at=NOW - age,
            )
        await db.commit()


async def remaining(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """The actions left in the log, oldest first."""
    async with factory() as db:
        rows = await db.execute(select(AuditEntry.action).order_by(AuditEntry.id))
        return list(rows.scalars())


async def purge(factory: async_sessionmaker[AsyncSession], **options: int) -> int:
    """Run the purge at ``NOW`` in a session of its own."""
    async with factory() as db:
        return await purge_old_audit_entries(db, ACTOR, now=NOW, **options)


async def test_entries_older_than_the_retention_go_and_one_exactly_at_the_cutoff_stays(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    day = timedelta(days=1)
    ages = [91 * day, 90 * day + timedelta(seconds=1), 90 * day, day]
    await prepare(factory, 90, ages)

    assert await purge(factory) == 2

    assert await remaining(factory) == ["test.entry2", "test.entry3", PURGE_ACTION]


async def test_the_purge_is_one_audit_entry_with_the_count_and_the_cutoff(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await prepare(factory, 30, [timedelta(days=40), timedelta(days=50)])

    await purge(factory)

    async with factory() as db:
        entry = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == PURGE_ACTION))
        ).scalar_one()
    assert entry.actor_type is AuditActorType.SYSTEM and entry.actor_name == "scheduler"
    assert entry.result is AuditResult.SUCCESS
    assert entry.details == {
        "deleted": 2,
        "cutoff": (NOW - timedelta(days=30)).isoformat(),
        "retention_days": 30,
    }


async def test_a_retention_of_zero_keeps_every_entry_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await prepare(factory, 0, [timedelta(days=4000), timedelta(days=1)])

    assert await purge(factory) == 0

    assert await remaining(factory) == ["test.entry0", "test.entry1"]


async def test_nothing_is_written_when_nothing_is_old_enough(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await prepare(factory, 90, [timedelta(days=89), timedelta(days=1)])

    assert await purge(factory) == 0

    assert await remaining(factory) == ["test.entry0", "test.entry1"]


async def test_a_backlog_is_deleted_in_batches(factory: async_sessionmaker[AsyncSession]) -> None:
    await prepare(factory, 10, [timedelta(days=20 + number) for number in range(7)])

    assert await purge(factory, batch_size=3) == 7

    assert await remaining(factory) == [PURGE_ACTION]


async def test_the_default_retention_is_ninety_days() -> None:
    assert GeneralSettings().audit_retention_days == 90
