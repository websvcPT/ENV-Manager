"""Tests for server-side sessions and for ending them when a user is disabled."""

from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import AuditActorType, AuditEntry, UserSession
from app.models.types import utc_now
from app.services.auth_policy import AuthPolicy
from app.services.sessions import (
    create_session,
    hash_token,
    purge_expired,
    resolve_session,
    revoke_session,
)
from app.services.users import set_user_active
from app.tests.auth_helpers import make_user

POLICY = AuthPolicy(idle_timeout=timedelta(minutes=30), absolute_timeout=timedelta(hours=2))


async def test_only_the_token_hash_is_stored(engine: AsyncEngine) -> None:
    """The plaintext cookie token never reaches the database."""
    factory = create_session_factory(engine)
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        token = await create_session(db, user, POLICY, ip_address="203.0.113.5", user_agent="ua")
        await db.commit()
    async with factory() as db:
        stored = (await db.execute(select(UserSession))).scalar_one()
        raw = (await db.execute(text("SELECT token_hash FROM user_sessions"))).scalar_one()
    assert raw == hash_token(token) != token
    assert stored.expires_at - stored.created_at == POLICY.absolute_timeout


async def test_resolve_returns_the_user_and_refreshes_last_seen(engine: AsyncEngine) -> None:
    """A live session resolves, and last seen moves forward after the touch interval."""
    factory = create_session_factory(engine)
    start = utc_now()
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        token = await create_session(db, user, POLICY, now=start)
        await db.commit()
    later = start + timedelta(minutes=10)
    async with factory() as db:
        resolved = await resolve_session(db, token, POLICY, now=later)
    assert resolved is not None
    assert resolved[1].name == "alice"
    async with factory() as db:
        assert (await db.execute(select(UserSession))).scalar_one().last_seen_at == later


async def test_unknown_token_does_not_resolve(engine: AsyncEngine) -> None:
    """A token that was never issued is rejected."""
    async with create_session_factory(engine)() as db:
        assert await resolve_session(db, "nope", POLICY) is None


async def test_idle_timeout_ends_the_session(engine: AsyncEngine) -> None:
    """A session unused past the idle timeout is rejected and deleted."""
    factory = create_session_factory(engine)
    start = utc_now()
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        token = await create_session(db, user, POLICY, now=start)
        await db.commit()
    async with factory() as db:
        assert await resolve_session(db, token, POLICY, now=start + timedelta(minutes=31)) is None
    async with factory() as db:
        assert (await db.execute(select(UserSession))).first() is None


async def test_absolute_timeout_ends_an_active_session(engine: AsyncEngine) -> None:
    """Steady use does not extend a session beyond its absolute lifetime."""
    factory = create_session_factory(engine)
    start = utc_now()
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        token = await create_session(db, user, POLICY, now=start)
        await db.commit()
    moment = start
    for _ in range(5):
        moment += timedelta(minutes=20)
        async with factory() as db:
            assert await resolve_session(db, token, POLICY, now=moment) is not None
    moment = start + POLICY.absolute_timeout + timedelta(minutes=1)
    async with factory() as db:
        assert await resolve_session(db, token, POLICY, now=moment) is None


async def test_revoked_session_no_longer_resolves(engine: AsyncEngine) -> None:
    """Logout removes the session."""
    factory = create_session_factory(engine)
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        token = await create_session(db, user, POLICY)
        await db.commit()
    async with factory() as db:
        resolved = await resolve_session(db, token, POLICY)
        assert resolved is not None
        await revoke_session(db, resolved[0])
    async with factory() as db:
        assert await resolve_session(db, token, POLICY) is None


async def test_disabling_a_user_ends_their_sessions_and_is_audited(engine: AsyncEngine) -> None:
    """Disabling deletes every session of that user only, and writes an audit entry."""
    factory = create_session_factory(engine)
    async with factory() as db:
        target, other = make_user("alice"), make_user("bob")
        db.add_all([target, other])
        await db.flush()
        first = await create_session(db, target, POLICY)
        await create_session(db, target, POLICY)
        kept = await create_session(db, other, POLICY)
        await db.commit()
    async with factory() as db:
        user = await db.get(type(target), target.id)
        assert user is not None
        ended = await set_user_active(db, user, False, actor_type=AuditActorType.CLI)
    assert ended == 2
    async with factory() as db:
        assert await resolve_session(db, first, POLICY) is None
        assert await resolve_session(db, kept, POLICY) is not None
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert (entry.action, entry.actor_name, entry.target_name) == (
            "user.disable",
            "cli",
            "alice",
        )


async def test_session_of_an_inactive_user_is_rejected(engine: AsyncEngine) -> None:
    """A session that survived somehow still fails once its user is inactive."""
    factory = create_session_factory(engine)
    async with factory() as db:
        user = make_user(active=False)
        db.add(user)
        await db.flush()
        token = await create_session(db, user, POLICY)
        await db.commit()
    async with factory() as db:
        assert await resolve_session(db, token, POLICY) is None


async def test_purge_removes_only_dead_sessions(engine: AsyncEngine) -> None:
    """Expired and idle sessions are purged, live ones stay."""
    factory = create_session_factory(engine)
    now = utc_now()
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        await create_session(db, user, POLICY, now=now - timedelta(hours=5))
        await create_session(db, user, POLICY, now=now - timedelta(minutes=40))
        live = await create_session(db, user, POLICY, now=now)
        await db.commit()
    async with factory() as db:
        assert await purge_expired(db, POLICY, now=now) == 2
        assert await resolve_session(db, live, POLICY, now=now) is not None
