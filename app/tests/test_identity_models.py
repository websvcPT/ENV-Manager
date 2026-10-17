"""Tests for the identity tables on a real migrated SQLite file."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.models import ApiToken, LoginCounter, LoginCounterKind, User, UserGroup, UserSession


def _user(name: str = "alice", group: UserGroup = UserGroup.ADMIN) -> User:
    """A user with placeholder credentials."""
    placeholder = "not-a-real-hash"
    return User(name=name, email=f"{name}@example.test", password_hash=placeholder, group=group)


async def test_user_defaults_and_group_stored_as_value(engine: AsyncEngine) -> None:
    """New users are active, without TOTP, and the group is stored as its plain value."""
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(_user())
        await session.commit()
    async with factory() as session:
        stored = (await session.execute(select(User))).scalar_one()
        raw = (await session.execute(text('SELECT "group" FROM users'))).scalar_one()
    assert stored.group is UserGroup.ADMIN
    assert raw == "admin"
    assert stored.is_active is True
    assert stored.totp_enabled is False
    assert stored.totp_secret is None
    assert stored.last_login_at is None
    assert stored.created_at.tzinfo is not None


async def test_user_names_are_unique(engine: AsyncEngine) -> None:
    """Two users cannot share a name."""
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add_all([_user("bob"), _user("bob")])
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_datetimes_round_trip_as_aware_utc(engine: AsyncEngine) -> None:
    """Offsets are normalized to UTC and read back aware."""
    factory = create_session_factory(engine)
    moment = datetime(2026, 10, 17, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    async with factory() as session:
        user = _user()
        user.last_login_at = moment
        session.add(user)
        await session.commit()
    async with factory() as session:
        stored = (await session.execute(select(User))).scalar_one()
    assert stored.last_login_at == moment
    assert stored.last_login_at is not None
    assert stored.last_login_at.utcoffset() == timedelta(0)


async def test_naive_datetime_is_refused(engine: AsyncEngine) -> None:
    """Naive datetimes never reach the database."""
    factory = create_session_factory(engine)
    async with factory() as session:
        user = _user()
        user.last_login_at = datetime(2026, 10, 17, 12, 0)
        session.add(user)
        with pytest.raises(Exception, match="naive datetime"):
            await session.commit()


async def test_session_token_hash_unique_and_deleted_with_user(engine: AsyncEngine) -> None:
    """Sessions are unique by token hash and removed when their user is deleted."""
    factory = create_session_factory(engine)
    expires = datetime.now(UTC) + timedelta(hours=8)
    async with factory() as session:
        user = _user()
        session.add(user)
        await session.flush()
        session.add(UserSession(token_hash="a" * 64, user_id=user.id, expires_at=expires))
        await session.commit()
        user_id = user.id
    async with factory() as session:
        session.add(UserSession(token_hash="a" * 64, user_id=user_id, expires_at=expires))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        await session.delete(await session.get(User, user_id))
        await session.commit()
        remaining = (await session.execute(select(UserSession))).scalars().all()
    assert remaining == []


async def test_session_requires_existing_user(engine: AsyncEngine) -> None:
    """Foreign keys are enforced for sessions."""
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(UserSession(token_hash="b" * 64, user_id=999, expires_at=datetime.now(UTC)))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_login_counter_unique_per_kind_and_subject(engine: AsyncEngine) -> None:
    """A subject has one counter per kind, and lockout survives a new session."""
    factory = create_session_factory(engine)
    locked_until = datetime.now(UTC) + timedelta(minutes=15)
    async with factory() as session:
        session.add(LoginCounter(kind=LoginCounterKind.USER, subject="alice", failures=5))
        session.add(
            LoginCounter(
                kind=LoginCounterKind.IP,
                subject="203.0.113.7",
                failures=5,
                locked_until=locked_until,
            )
        )
        await session.commit()
    async with factory() as session:
        counters = (await session.execute(select(LoginCounter))).scalars().all()
        assert {(item.kind, item.subject) for item in counters} == {
            (LoginCounterKind.USER, "alice"),
            (LoginCounterKind.IP, "203.0.113.7"),
        }
        session.add(LoginCounter(kind=LoginCounterKind.USER, subject="alice"))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        locked = (
            await session.execute(
                select(LoginCounter).where(LoginCounter.locked_until.is_not(None))
            )
        ).scalar_one()
    assert locked.locked_until == locked_until


async def test_api_token_round_trip_and_cascade(engine: AsyncEngine) -> None:
    """Tokens keep their operations list, default to unrevoked, and go with their owner."""
    factory = create_session_factory(engine)
    async with factory() as session:
        owner = _user()
        session.add(owner)
        await session.flush()
        session.add(
            ApiToken(
                owner_id=owner.id,
                name="ci",
                token_hash="c" * 64,
                prefix="emt_abcd",
                operations=["deploy", "dist_check"],
            )
        )
        await session.commit()
        owner_id = owner.id
    async with factory() as session:
        token = (await session.execute(select(ApiToken))).scalar_one()
        assert token.operations == ["deploy", "dist_check"]
        assert token.revoked_at is None
        assert token.expires_at is None
        assert token.last_used_at is None
        session.add(
            ApiToken(owner_id=owner_id, name="other", token_hash="c" * 64, prefix="emt_efgh")
        )
        with pytest.raises(IntegrityError):
            await session.commit()
    async with factory() as session:
        await session.delete(await session.get(User, owner_id))
        await session.commit()
        assert (await session.execute(select(ApiToken))).scalars().all() == []
