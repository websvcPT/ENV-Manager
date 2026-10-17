"""Tests for the user service: creation, password resets, enable or disable and the guard."""

import string
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import create_session_factory
from app.core.passwords import GENERATED_PASSWORD_LENGTH, generate_password, verify_password
from app.models import (
    AuditActorType,
    AuditEntry,
    AuditResult,
    LoginCounter,
    LoginCounterKind,
    User,
    UserGroup,
)
from app.models.types import utc_now
from app.services.auth_policy import AuthPolicy
from app.services.sessions import create_session
from app.services.users import (
    InvalidUserError,
    LastSuperadminError,
    UserExistsError,
    UserNotFoundError,
    create_user,
    get_user_by_name,
    list_users,
    reset_password,
    set_user_active,
)
from app.tests.auth_helpers import make_user

CLI = AuditActorType.CLI


def test_generated_passwords_are_sixteen_mixed_characters() -> None:
    passwords = {generate_password() for _ in range(200)}
    assert len(passwords) == 200
    for password in passwords:
        assert len(password) == GENERATED_PASSWORD_LENGTH == 16
        assert any(char in string.ascii_lowercase for char in password)
        assert any(char in string.ascii_uppercase for char in password)
        assert any(char in string.digits for char in password)
        assert any(char in string.punctuation for char in password)


async def test_create_user_stores_a_hash_and_audits_without_the_password(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        user, password = await create_user(
            db, name=" Bob ", email="bob@example.test", group=UserGroup.VIEWER, actor_type=CLI
        )
    async with factory() as db:
        stored = await get_user_by_name(db, "BOB")
        assert stored.id == user.id
        assert stored.name == "Bob"
        assert stored.is_active
        assert stored.password_hash != password
        assert verify_password(password, stored.password_hash)[0]
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert (entry.action, entry.actor_type, entry.actor_name, entry.result) == (
            "user.create",
            CLI,
            "cli",
            AuditResult.SUCCESS,
        )
        assert entry.target_name == "Bob"
        assert password not in repr(entry.details)
        assert entry.details == {"group": "viewer"}


@pytest.mark.parametrize(
    ("name", "email"),
    [
        ("", "a@example.test"),
        ("has space", "a@example.test"),
        ("a@b", "a@example.test"),
        ("x" * 101, "a@example.test"),
        ("carol", "not-an-email"),
        ("carol", "@example.test"),
        ("carol", "carol@localhost"),
    ],
)
async def test_create_user_refuses_invalid_input(
    engine: AsyncEngine, name: str, email: str
) -> None:
    async with create_session_factory(engine)() as db:
        with pytest.raises(InvalidUserError):
            await create_user(db, name=name, email=email, group=UserGroup.ADMIN, actor_type=CLI)
        assert await list_users(db) == []


async def test_create_user_refuses_a_duplicate_name_ignoring_case(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        await create_user(
            db, name="dave", email="d@example.test", group=UserGroup.ADMIN, actor_type=CLI
        )
    async with factory() as db:
        with pytest.raises(UserExistsError):
            await create_user(
                db, name="DAVE", email="e@example.test", group=UserGroup.ADMIN, actor_type=CLI
            )
        assert len(await list_users(db)) == 1


async def test_unknown_user_is_reported(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as db:
        with pytest.raises(UserNotFoundError):
            await get_user_by_name(db, "nobody")


async def test_list_users_is_ordered_by_name(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all([make_user("zed"), make_user("Amy"), make_user("bob")])
        await db.commit()
    async with factory() as db:
        assert [user.name for user in await list_users(db)] == ["Amy", "bob", "zed"]


async def test_reset_password_replaces_hash_ends_sessions_and_clears_lockout(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        user = make_user("erin")
        old_hash = user.password_hash
        db.add(user)
        await db.flush()
        await create_session(db, user, AuthPolicy())
        db.add(
            LoginCounter(
                kind=LoginCounterKind.USER,
                subject="erin",
                failures=9,
                locked_until=utc_now() + timedelta(minutes=10),
            )
        )
        await db.commit()
    async with factory() as db:
        user = await get_user_by_name(db, "erin")
        password = await reset_password(db, user, actor_type=CLI)
    async with factory() as db:
        stored = await get_user_by_name(db, "erin")
        assert stored.password_hash != old_hash
        assert verify_password(password, stored.password_hash)[0]
        assert (await db.execute(select(LoginCounter))).first() is None
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert entry.action == "user.password_reset"
        assert entry.details == {"sessions_ended": 1}


async def test_last_active_superadmin_cannot_be_disabled(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all([make_user("root", UserGroup.SUPERADMIN), make_user("adm", UserGroup.ADMIN)])
        await db.commit()
    async with factory() as db:
        root = await get_user_by_name(db, "root")
        with pytest.raises(LastSuperadminError):
            await set_user_active(db, root, False, actor_type=CLI)
    async with factory() as db:
        assert (await get_user_by_name(db, "root")).is_active
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert (entry.action, entry.result) == ("user.disable", AuditResult.BLOCKED)


async def test_a_superadmin_can_be_disabled_when_another_is_active(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all(
            [make_user("root", UserGroup.SUPERADMIN), make_user("two", UserGroup.SUPERADMIN)]
        )
        await db.commit()
    async with factory() as db:
        await set_user_active(db, await get_user_by_name(db, "root"), False, actor_type=CLI)
    async with factory() as db:
        assert not (await get_user_by_name(db, "root")).is_active
        two = await get_user_by_name(db, "two")
        with pytest.raises(LastSuperadminError):
            await set_user_active(db, two, False, actor_type=CLI)


async def test_a_disabled_superadmin_does_not_count_and_enabling_is_never_refused(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all(
            [
                make_user("root", UserGroup.SUPERADMIN),
                make_user("old", UserGroup.SUPERADMIN, active=False),
            ]
        )
        await db.commit()
    async with factory() as db:
        with pytest.raises(LastSuperadminError):
            await set_user_active(db, await get_user_by_name(db, "root"), False, actor_type=CLI)
    async with factory() as db:
        await set_user_active(db, await get_user_by_name(db, "old"), True, actor_type=CLI)
    async with factory() as db:
        users = list((await db.execute(select(User))).scalars())
        assert len(users) == 2
        assert all(user.is_active for user in users)
