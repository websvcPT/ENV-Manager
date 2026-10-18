"""The shared failure counters: counting, locking, windows and concurrent failures."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import LoginCounterKind
from app.models.types import utc_now
from app.services.auth_policy import AuthPolicy
from app.services.lockout import get_counter, register_failure, remaining_lock

POLICY = AuthPolicy()
SUBJECT = "203.0.113.20"


async def fail(
    engine: AsyncEngine,
    kind: LoginCounterKind = LoginCounterKind.API_IP,
    subject: str = SUBJECT,
    limit: int = 3,
    now: datetime | None = None,
) -> bool:
    """Count one failure in a session of its own and commit it."""
    async with create_session_factory(engine)() as db:
        locked = await register_failure(db, kind, subject, limit, POLICY, now or utc_now())
        await db.commit()
        return locked


async def test_failures_are_counted_and_the_limit_locks(engine: AsyncEngine) -> None:
    """The limit-th failure locks the subject for the lockout duration."""
    now = utc_now()
    assert [await fail(engine, now=now) for _ in range(3)] == [False, False, True]
    async with create_session_factory(engine)() as db:
        counter = await get_counter(db, LoginCounterKind.API_IP, SUBJECT)
        assert counter is not None and counter.failures == 3
        assert counter.locked_until == now + POLICY.lockout_duration
        assert remaining_lock([counter], now) == POLICY.lockout_duration
        assert remaining_lock([counter, None], now + POLICY.lockout_duration) is None
        assert remaining_lock([None], now) is None


async def test_kinds_and_subjects_are_counted_apart(engine: AsyncEngine) -> None:
    """The same address under another kind, and another address, start from zero."""
    now = utc_now()
    for _ in range(3):
        await fail(engine, now=now)
    assert await fail(engine, LoginCounterKind.IP, now=now) is False
    assert await fail(engine, subject="203.0.113.21", now=now) is False


async def test_an_old_window_or_an_ended_lock_starts_again(engine: AsyncEngine) -> None:
    """Failures older than the window do not add up, and a lock that ended is forgotten."""
    start = utc_now()
    await fail(engine, now=start)
    await fail(engine, now=start)
    later = start + POLICY.failure_window + timedelta(seconds=1)
    assert await fail(engine, now=later) is False
    async with create_session_factory(engine)() as db:
        counter = await get_counter(db, LoginCounterKind.API_IP, SUBJECT)
        assert counter is not None and counter.failures == 1 and counter.window_started_at == later
    for _ in range(2):
        await fail(engine, now=later)
    ended = later + POLICY.lockout_duration + timedelta(seconds=1)
    assert await fail(engine, now=ended) is False
    async with create_session_factory(engine)() as db:
        counter = await get_counter(db, LoginCounterKind.API_IP, SUBJECT)
        assert counter is not None and counter.failures == 1 and counter.locked_until is None


async def test_failures_that_arrive_together_are_all_counted(engine: AsyncEngine) -> None:
    """The first failures of a new subject race on the row; none is lost and none errors."""
    now = utc_now()
    results = await asyncio.gather(*(fail(engine, limit=100, now=now) for _ in range(12)))
    assert results == [False] * 12
    async with create_session_factory(engine)() as db:
        counter = await get_counter(db, LoginCounterKind.API_IP, SUBJECT)
        assert counter is not None and counter.failures == 12
