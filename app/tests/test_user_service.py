"""Tests for the user service: creation, password and two-factor resets, enable or disable."""

import string
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.crypto import CryptoService
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
    PendingLogin,
    User,
    UserGroup,
    UserSession,
)
from app.models.types import utc_now
from app.seeds import SeedData
from app.services import totp
from app.services import users as users_module
from app.services.audit import AuditActor
from app.services.auth_policy import AuthPolicy
from app.services.grants import GrantError, GrantTarget, list_grants
from app.services.pending_logins import create_pending_login
from app.services.permissions import load_token_scope, load_user_scope
from app.services.sessions import create_session
from app.services.tokens import create_token, revoke_token
from app.services.users import (
    InvalidUserError,
    LastSuperadminError,
    TwoFactorNotOnError,
    UserExistsError,
    UserNotFoundError,
    clean_name,
    create_user,
    get_user_by_email,
    list_users,
    reset_password,
    reset_two_factor,
    set_user_active,
    set_user_group,
)
from app.tests.auth_helpers import make_user

CLI = AuditActorType.CLI


async def get_user_by_name(db: AsyncSession, name: str) -> User:
    """The one user with the name, for tests that give every user a name of their own."""
    return (await db.execute(select(User).where(User.name == name))).scalar_one()


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
            db,
            name=" Bob ",
            email="bob@example.test",
            group=UserGroup.VIEWER,
            actor=AuditActor.cli(),
        )
    async with factory() as db:
        stored = await get_user_by_email(db, "BOB@example.test")
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
        ("   ", "a@example.test"),
        ("a@b", "a@example.test"),
        ("under_score", "a@example.test"),
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
            await create_user(
                db, name=name, email=email, group=UserGroup.ADMIN, actor=AuditActor.cli()
            )
        assert await list_users(db) == []


@pytest.mark.parametrize(
    ("typed", "kept"),
    [
        ("John Doe", "John Doe"),
        ("  John   Doe  ", "John Doe"),
        ("Zoë Müller-Åberg", "Zoë Müller-Åberg"),
        ("O'Brien Jr.", "O'Brien Jr."),
        ("María José", "María José"),
        ("田中 太郎", "田中 太郎"),
        ("हिन्दी नाम", "हिन्दी नाम"),
        ("Anna 2", "Anna 2"),
        ("x" * 100, "x" * 100),
        ("7", "7"),
    ],
)
def test_the_name_rule_keeps_letters_of_any_language_digits_spaces_and_three_marks(
    typed: str, kept: str
) -> None:
    assert clean_name(typed) == kept


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "   ",
        "x" * 101,
        "a@b",
        "under_score",
        "slash/name",
        "semi;colon",
        "tab\tname",
        "line\nbreak",
        "zero\N{ZERO WIDTH SPACE}width",
        "bidi\N{RIGHT-TO-LEFT OVERRIDE}flip",
        "non\N{NO-BREAK SPACE}breaking",
        "bell\x07",
        "emoji \U0001f600",
        "\N{EM DASH}dash",
        "curly\N{RIGHT SINGLE QUOTATION MARK}quote",
    ],
)
def test_the_name_rule_refuses_control_format_and_other_characters(typed: str) -> None:
    with pytest.raises(InvalidUserError) as refused:
        clean_name(typed)
    assert refused.value.field == "name"


async def test_users_may_share_a_name_and_the_email_is_the_only_key(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        first, _ = await create_user(
            db,
            name="Dave",
            email="d@example.test",
            group=UserGroup.ADMIN,
            actor=AuditActor.cli(),
        )
        second, _ = await create_user(
            db,
            name="DAVE",
            email="e@example.test",
            group=UserGroup.VIEWER,
            actor=AuditActor.cli(),
        )
        assert first.id != second.id
        with pytest.raises(UserExistsError) as refused:
            await create_user(
                db,
                name="Someone Else",
                email="E@Example.TEST",
                group=UserGroup.ADMIN,
                actor=AuditActor.cli(),
            )
        assert refused.value.field == "email"
        assert [(user.name, user.email) for user in await list_users(db)] == [
            ("Dave", "d@example.test"),
            ("DAVE", "e@example.test"),
        ]
        found = await get_user_by_email(db, " e@EXAMPLE.test ")
        assert found.id == second.id


async def test_unknown_user_is_reported(engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as db:
        with pytest.raises(UserNotFoundError):
            await get_user_by_email(db, "nobody@example.test")


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
                subject="erin@example.test",
                failures=9,
                locked_until=utc_now() + timedelta(minutes=10),
            )
        )
        await db.commit()
    async with factory() as db:
        user = await get_user_by_name(db, "erin")
        password = await reset_password(db, user, actor=AuditActor.cli())
    async with factory() as db:
        stored = await get_user_by_name(db, "erin")
        assert stored.password_hash != old_hash
        assert verify_password(password, stored.password_hash)[0]
        assert (await db.execute(select(LoginCounter))).first() is None
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert entry.action == "user.password_reset"
        assert entry.details == {"sessions_ended": 1}


async def test_reset_two_factor_clears_the_factor_and_ends_sessions_and_pending_sign_ins(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """A lost authenticator: the factor is gone and nothing opened with the old password stays."""
    factory = create_session_factory(engine)
    async with factory() as db:
        user = make_user("frank")
        db.add(user)
        await db.flush()
        secret = totp.generate_secret()
        totp.activate(user, secret, 123, crypto)
        await create_session(db, user, AuthPolicy())
        await create_session(db, user, AuthPolicy())
        await create_pending_login(db, user, AuthPolicy())
        other = make_user("gina")
        db.add(other)
        await db.flush()
        await create_session(db, other, AuthPolicy())
        await create_pending_login(db, other, AuthPolicy())
        await db.commit()
    async with factory() as db:
        user = await get_user_by_name(db, "frank")
        ended = await reset_two_factor(db, user, actor=AuditActor.cli())
    assert ended == 2
    async with factory() as db:
        stored = await get_user_by_name(db, "frank")
        assert (stored.totp_enabled, stored.totp_secret, stored.totp_enabled_at) == (
            False,
            None,
            None,
        )
        assert stored.totp_last_step is None
        remaining = (await db.execute(select(UserSession.user_id))).scalars().all()
        assert remaining == [(await get_user_by_name(db, "gina")).id]
        waiting = (await db.execute(select(PendingLogin.user_id))).scalars().all()
        assert waiting == remaining
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert (entry.action, entry.result, entry.actor_name, entry.target_name) == (
            "user.totp_reset",
            AuditResult.SUCCESS,
            "cli",
            "frank",
        )
        assert entry.details == {"sessions_ended": 2, "pending_logins_ended": 1}
        assert secret not in repr([entry.details, entry.target_name])


async def test_reset_two_factor_refuses_a_user_who_has_none_and_audits_nothing(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add(make_user("hank"))
        await db.commit()
    async with factory() as db:
        user = await get_user_by_name(db, "hank")
        with pytest.raises(TwoFactorNotOnError, match="hank has no two-factor sign-in to reset"):
            await reset_two_factor(db, user, actor=AuditActor.cli())
    async with factory() as db:
        assert (await db.execute(select(AuditEntry))).first() is None


async def test_reset_two_factor_by_a_user_names_them_as_the_actor(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        admin, target = make_user("root", UserGroup.SUPERADMIN), make_user("ivy")
        db.add_all([admin, target])
        await db.flush()
        totp.activate(target, totp.generate_secret(), 7, crypto)
        await db.commit()
    async with factory() as db:
        root, ivy = await get_user_by_name(db, "root"), await get_user_by_name(db, "ivy")
        await reset_two_factor(db, ivy, actor=AuditActor.for_user(root, "203.0.113.7"))
    async with factory() as db:
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert (entry.actor_type, entry.actor_name, entry.ip_address) == (
            AuditActorType.USER,
            "root",
            "203.0.113.7",
        )


async def test_last_active_superadmin_cannot_be_disabled(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with factory() as db:
        db.add_all([make_user("root", UserGroup.SUPERADMIN), make_user("adm", UserGroup.ADMIN)])
        await db.commit()
    async with factory() as db:
        root = await get_user_by_name(db, "root")
        with pytest.raises(LastSuperadminError):
            await set_user_active(db, root, False, actor=AuditActor.cli())
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
        await set_user_active(db, await get_user_by_name(db, "root"), False, actor=AuditActor.cli())
    async with factory() as db:
        assert not (await get_user_by_name(db, "root")).is_active
        two = await get_user_by_name(db, "two")
        with pytest.raises(LastSuperadminError):
            await set_user_active(db, two, False, actor=AuditActor.cli())


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
            await set_user_active(
                db, await get_user_by_name(db, "root"), False, actor=AuditActor.cli()
            )
    async with factory() as db:
        await set_user_active(db, await get_user_by_name(db, "old"), True, actor=AuditActor.cli())
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
            db, name="fay", email="Fay@Example.test", group=UserGroup.ADMIN, actor=AuditActor.cli()
        )
    async with factory() as db:
        with pytest.raises(UserExistsError) as refused:
            await create_user(
                db,
                name="faye",
                email="fay@EXAMPLE.test",
                group=UserGroup.ADMIN,
                actor=AuditActor.cli(),
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
            db, name="gus", email="gus@example.test", group=UserGroup.ADMIN, actor=AuditActor.cli()
        )

    async def not_found(db: AsyncSession, email: str) -> None:
        return None

    monkeypatch.setattr(users_module, "find_user_by_email", not_found)
    async with factory() as db:
        with pytest.raises(UserExistsError):
            await create_user(
                db,
                name="gus2",
                email="GUS@example.test",
                group=UserGroup.ADMIN,
                actor=AuditActor.cli(),
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
            actor=AuditActor.for_user(seed_data.users.superadmin, "10.1.2.3"),
            grants=targets,
        )
    async with factory() as db:
        assert await list_grants(db, user.id) == frozenset(targets)
        entries = (await db.execute(select(AuditEntry).order_by(AuditEntry.id))).scalars().all()
        created = [entry for entry in entries if entry.action == "user.create"]
        added = [entry for entry in entries if entry.action == "grant.add"]
        assert [entry.details for entry in created] == [{"group": "viewer", "grants": 2}]
        assert sorted(str(entry.details["item"]) for entry in added) == [
            "Fancy Proj. Name/prod/mysql/.env.db",
            "billing",
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
            actor=AuditActor.cli(),
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
                actor=AuditActor.cli(),
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
            db, kim, UserGroup.VIEWER, actor=AuditActor.for_user(root, "10.0.0.9")
        )
        unchanged = await set_user_group(db, kim, UserGroup.VIEWER, actor=AuditActor.cli())
    assert changed and not unchanged
    async with factory() as db:
        assert (await get_user_by_name(db, "kim")).group is UserGroup.VIEWER
        entry = (await db.execute(select(AuditEntry))).scalar_one()
        assert (entry.action, entry.result, entry.actor_name) == (
            "user.group_change",
            AuditResult.SUCCESS,
            "root",
        )
        assert entry.details == {"from": "admin", "to": "viewer", "tokens_revoked": 0}
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
            await set_user_group(db, root, UserGroup.ADMIN, actor=AuditActor.cli())
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
            db, await get_user_by_name(db, "root"), UserGroup.VIEWER, actor=AuditActor.cli()
        )
    async with factory() as db:
        with pytest.raises(LastSuperadminError):
            await set_user_group(
                db, await get_user_by_name(db, "two"), UserGroup.ADMIN, actor=AuditActor.cli()
            )


async def _live_token(db: AsyncSession, owner: User, name: str) -> ApiToken:
    """A fresh token for a seeded user, created the way the create form does."""
    created = await create_token(
        db,
        owner,
        await load_user_scope(db, owner),
        name=name,
        operations=["deploy"],
        restriction="",
        expires="",
        actor=AuditActor.for_user(owner),
    )
    return created.token


async def _revoke_entries(db: AsyncSession) -> list[AuditEntry]:
    """The token.revoke audit entries, oldest first."""
    rows = await db.execute(
        select(AuditEntry).where(AuditEntry.action == "token.revoke").order_by(AuditEntry.id)
    )
    return list(rows.scalars())


async def test_disabling_the_owner_revokes_every_token_for_good(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    """Enabling the user later never brings an old token back; they create new ones."""
    factory = create_session_factory(seeded_database)
    async with factory() as db:
        owner = await db.get(User, seed_data.users.admin.id)
        assert owner is not None
        seeded = (await db.execute(select(ApiToken))).scalars().one()
        extra = await _live_token(db, owner, "extra")
        earlier = await _live_token(db, owner, "earlier")
        await revoke_token(db, earlier, AuditActor.for_user(owner))
        stamp = earlier.revoked_at
        assert await load_token_scope(db, seeded) is not None

        await set_user_active(db, owner, False, actor=AuditActor(CLI, ip_address="10.0.0.9"))
        assert await load_token_scope(db, seeded) is None
        assert seeded.revoked_at is not None and extra.revoked_at is not None
        assert earlier.revoked_at == stamp

        await set_user_active(db, owner, True, actor=AuditActor.cli())
        assert await load_token_scope(db, seeded) is None
        assert await load_token_scope(db, extra) is None

        entries = await _revoke_entries(db)
        assert [(e.target_name, e.details) for e in entries[1:]] == [
            ("billing-ci", {"owner": "admin", "reason": "user_disabled"}),
            ("extra", {"owner": "admin", "reason": "user_disabled"}),
        ]
        assert {(e.actor_name, e.ip_address) for e in entries[1:]} == {("cli", "10.0.0.9")}
        disable = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "user.disable"))
        ).scalar_one()
        assert disable.details == {"sessions_ended": 0, "tokens_revoked": 2}
        enable = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "user.enable"))
        ).scalar_one()
        assert enable.details == {"sessions_ended": 0}
        fresh = await _live_token(db, owner, "after-enable")
        assert await load_token_scope(db, fresh) is not None


async def test_moving_the_owner_to_viewer_revokes_every_token_for_good(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    factory = create_session_factory(seeded_database)
    async with factory() as db:
        owner = await db.get(User, seed_data.users.admin.id)
        root = await db.get(User, seed_data.users.superadmin.id)
        assert owner is not None and root is not None
        seeded = (await db.execute(select(ApiToken))).scalars().one()

        await set_user_group(db, owner, UserGroup.VIEWER, actor=AuditActor.for_user(root))
        assert seeded.revoked_at is not None and await load_token_scope(db, seeded) is None
        (revoked,) = await _revoke_entries(db)
        assert (revoked.actor_name, revoked.target_name) == ("root", "billing-ci")
        assert revoked.details == {"owner": "admin", "reason": "moved_to_viewer"}
        change = (
            await db.execute(select(AuditEntry).where(AuditEntry.action == "user.group_change"))
        ).scalar_one()
        assert change.details == {"from": "admin", "to": "viewer", "tokens_revoked": 1}

        await set_user_group(db, owner, UserGroup.ADMIN, actor=AuditActor.cli())
        assert await load_token_scope(db, seeded) is None
        assert len(await _revoke_entries(db)) == 1


async def test_other_group_changes_and_refused_changes_leave_tokens_alone(
    seeded_database: AsyncEngine, seed_data: SeedData
) -> None:
    factory = create_session_factory(seeded_database)
    async with factory() as db:
        admin = await db.get(User, seed_data.users.admin.id)
        root = await db.get(User, seed_data.users.superadmin.id)
        assert admin is not None and root is not None
        seeded = (await db.execute(select(ApiToken))).scalars().one()
        await set_user_group(db, admin, UserGroup.SUPERADMIN, actor=AuditActor.cli())
        assert await load_token_scope(db, seeded) is not None
        await set_user_group(db, admin, UserGroup.ADMIN, actor=AuditActor.cli())
        assert await load_token_scope(db, seeded) is not None

        roots_token = await _live_token(db, root, "roots")
        with pytest.raises(LastSuperadminError):
            await set_user_active(db, root, False, actor=AuditActor.cli())
        with pytest.raises(LastSuperadminError):
            await set_user_group(db, root, UserGroup.VIEWER, actor=AuditActor.cli())
        assert roots_token.revoked_at is None and await _revoke_entries(db) == []
        changes = await db.execute(
            select(AuditEntry)
            .where(AuditEntry.action == "user.group_change")
            .order_by(AuditEntry.id)
        )
        change = changes.scalars().first()
        assert change is not None and "tokens_revoked" not in change.details


async def test_the_generated_password_is_never_logged_or_audited(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    factory = create_session_factory(engine)
    async with factory() as db:
        _, password = await create_user(
            db,
            name="lena",
            email="lena@example.test",
            group=UserGroup.ADMIN,
            actor=AuditActor.cli(),
        )
        user = await get_user_by_name(db, "lena")
        reset = await reset_password(db, user, actor=AuditActor.cli())
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
