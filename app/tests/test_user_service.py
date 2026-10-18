"""Tests for the user service: creation, password resets, enable or disable and the guard."""

import string
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import create_session_factory
from app.core.passwords import GENERATED_PASSWORD_LENGTH, generate_password, verify_password
from app.models import (
    ApiToken,
    AuditActorType,
    AuditEntry,
    AuditResult,
    GrantLevel,
    LoginCounter,
    LoginCounterKind,
    User,
    UserGroup,
)
from app.models.types import utc_now
from app.seeds import SeedData
from app.services import users as users_module
from app.services.auth_policy import AuthPolicy
from app.services.grants import GrantError, GrantTarget, list_grants
from app.services.permissions import load_token_scope
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
    set_user_group,
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
        assert entry.details == {"group": "viewer", "grants": 0}


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


async def test_email_is_unique_ignoring_case_and_the_refusal_names_the_field(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        await create_user(
            db, name="fay", email="Fay@Example.test", group=UserGroup.ADMIN, actor_type=CLI
        )
    async with factory() as db:
        with pytest.raises(UserExistsError) as refused:
            await create_user(
                db, name="faye", email="fay@EXAMPLE.test", group=UserGroup.ADMIN, actor_type=CLI
            )
        assert refused.value.field == "email"
        assert "email address" in str(refused.value)
        assert [user.name for user in await list_users(db)] == ["fay"]
        assert len((await db.execute(select(AuditEntry))).scalars().all()) == 1


async def test_a_duplicate_that_slips_past_the_check_is_refused_by_the_database(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two creations racing for one address: the loser gets the same refusal, nothing is left."""
    factory = create_session_factory(engine)
    async with factory() as db:
        await create_user(
            db, name="gus", email="gus@example.test", group=UserGroup.ADMIN, actor_type=CLI
        )

    async def not_found(db: AsyncSession, email: str) -> None:
        return None

    monkeypatch.setattr(users_module, "find_user_by_email", not_found)
    async with factory() as db:
        with pytest.raises(UserExistsError):
            await create_user(
                db, name="gus2", email="GUS@example.test", group=UserGroup.ADMIN, actor_type=CLI
            )
        assert [user.name for user in await list_users(db)] == ["gus"]
        assert len((await db.execute(select(AuditEntry))).scalars().all()) == 1


async def test_create_user_with_grants_stores_them_and_audits_each(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    factory = create_session_factory(seeded_database)
    targets = [
        GrantTarget(GrantLevel.PROJECT, seed_data.projects.billing.id),
        GrantTarget(GrantLevel.ENVIRONMENT, seed_data.envs.db.id),
    ]
    async with factory() as db:
        user, password = await create_user(
            db,
            name="hana",
            email="hana@example.test",
            group=UserGroup.VIEWER,
            actor_type=AuditActorType.USER,
            actor_user=seed_data.users.superadmin,
            ip_address="10.1.2.3",
            grants=targets,
        )
    async with factory() as db:
        assert await list_grants(db, user.id) == frozenset(targets)
        entries = (await db.execute(select(AuditEntry).order_by(AuditEntry.id))).scalars().all()
        created = [entry for entry in entries if entry.action == "user.create"]
        added = [entry for entry in entries if entry.action == "grant.add"]
        assert [entry.details for entry in created] == [{"group": "viewer", "grants": 2}]
        assert sorted(str(entry.details["item"]) for entry in added) == [
            "billing",
            "rentals/prod/mysql/.env.db",
        ]
        for entry in [*created, *added]:
            assert entry.actor_name == "root" and entry.ip_address == "10.1.2.3"
            assert (entry.target_type, entry.target_name) == ("user", "hana")
            assert (entry.project_id, entry.deployment_id, entry.environment_id) == (None,) * 3
            assert password not in repr(entry.details)


async def test_grants_chosen_for_a_superadmin_are_not_stored(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    factory = create_session_factory(seeded_database)
    async with factory() as db:
        user, _ = await create_user(
            db,
            name="ivan",
            email="ivan@example.test",
            group=UserGroup.SUPERADMIN,
            actor_type=CLI,
            grants=[GrantTarget(GrantLevel.PROJECT, seed_data.projects.billing.id)],
        )
    async with factory() as db:
        assert await list_grants(db, user.id) == frozenset()
        actions = (await db.execute(select(AuditEntry.action))).scalars().all()
        assert "grant.add" not in actions


async def test_a_grant_on_a_missing_item_creates_nothing(seeded_database: AsyncEngine) -> None:
    """Every item is checked before anything is written: no user, no grant, no audit entry."""
    factory = create_session_factory(seeded_database)
    async with factory() as db:
        before = len(await list_users(db))
        entries = len((await db.execute(select(AuditEntry))).scalars().all())
        with pytest.raises(GrantError) as refused:
            await create_user(
                db,
                name="jo",
                email="jo@example.test",
                group=UserGroup.VIEWER,
                actor_type=CLI,
                grants=[GrantTarget(GrantLevel.DEPLOYMENT, 987654)],
            )
        assert refused.value.field == "grants"
    async with factory() as db:
        assert len(await list_users(db)) == before
        assert len((await db.execute(select(AuditEntry))).scalars().all()) == entries


async def test_set_user_group_changes_the_group_and_audits_it(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all([make_user("root", UserGroup.SUPERADMIN), make_user("kim", UserGroup.ADMIN)])
        await db.commit()
    async with factory() as db:
        kim = await get_user_by_name(db, "kim")
        root = await get_user_by_name(db, "root")
        changed = await set_user_group(
            db,
            kim,
            UserGroup.VIEWER,
            actor_type=AuditActorType.USER,
            actor_user=root,
            ip_address="10.0.0.9",
        )
        unchanged = await set_user_group(db, kim, UserGroup.VIEWER, actor_type=CLI)
    assert changed and not unchanged
    async with factory() as db:
        assert (await get_user_by_name(db, "kim")).group is UserGroup.VIEWER
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert (entry.action, entry.result, entry.actor_name) == (
            "user.group_change",
            AuditResult.SUCCESS,
            "root",
        )
        assert entry.details == {"from": "admin", "to": "viewer"}
        assert (entry.target_type, entry.target_name) == ("user", "kim")


async def test_the_last_active_superadmin_cannot_be_demoted_and_the_refusal_is_audited(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all([make_user("root", UserGroup.SUPERADMIN), make_user("adm", UserGroup.ADMIN)])
        await db.commit()
    async with factory() as db:
        root = await get_user_by_name(db, "root")
        with pytest.raises(LastSuperadminError) as refused:
            await set_user_group(db, root, UserGroup.ADMIN, actor_type=CLI)
        assert "demoted" in str(refused.value)
    async with factory() as db:
        assert (await get_user_by_name(db, "root")).group is UserGroup.SUPERADMIN
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert (entry.action, entry.result) == ("user.group_change", AuditResult.BLOCKED)
        assert entry.details == {
            "reason": "last_active_superadmin",
            "from": "superadmin",
            "to": "admin",
        }


async def test_a_superadmin_can_be_demoted_while_another_is_active(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all(
            [make_user("root", UserGroup.SUPERADMIN), make_user("two", UserGroup.SUPERADMIN)]
        )
        await db.commit()
    async with factory() as db:
        assert await set_user_group(
            db, await get_user_by_name(db, "root"), UserGroup.VIEWER, actor_type=CLI
        )
    async with factory() as db:
        with pytest.raises(LastSuperadminError):
            await set_user_group(
                db, await get_user_by_name(db, "two"), UserGroup.ADMIN, actor_type=CLI
            )


async def test_tokens_stop_when_the_owner_is_disabled_or_moved_to_viewer(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """The next token call finds the owner's state, so nothing needs to be revoked or ended."""
    factory = create_session_factory(seeded_database)
    async with factory() as db:
        token = (await db.execute(select(ApiToken))).scalars().first()
        assert token is not None
        owner = await db.get(User, token.owner_id)
        assert owner is not None
        assert await load_token_scope(db, token) is not None

        await set_user_group(db, owner, UserGroup.VIEWER, actor_type=CLI)
        assert await load_token_scope(db, token) is None
        await set_user_group(db, owner, UserGroup.ADMIN, actor_type=CLI)
        assert await load_token_scope(db, token) is not None

        await set_user_active(db, owner, False, actor_type=CLI)
        assert await load_token_scope(db, token) is None
        await set_user_active(db, owner, True, actor_type=CLI)
        assert await load_token_scope(db, token) is not None


async def test_the_generated_password_is_never_logged_or_audited(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    factory = create_session_factory(engine)
    async with factory() as db:
        _, password = await create_user(
            db, name="lena", email="lena@example.test", group=UserGroup.ADMIN, actor_type=CLI
        )
        user = await get_user_by_name(db, "lena")
        reset = await reset_password(db, user, actor_type=CLI)
    async with factory() as db:
        entries = (await db.execute(select(AuditEntry))).scalars().all()
        stored = (await db.execute(select(User.password_hash))).scalars().all()
    audited = " ".join(
        f"{entry.details!r} {entry.target_name} {entry.actor_name}" for entry in entries
    )
    for secret in (password, reset):
        assert secret not in caplog.text
        assert secret not in audited
        assert secret not in stored
