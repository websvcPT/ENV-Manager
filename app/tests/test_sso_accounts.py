"""Tests for the account a single sign in is for: linking, creating, the group from the roles,
and every reason to refuse."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.core.passwords import UNUSABLE_PASSWORD_HASH, verify_password
from app.models import (
    AuditActorType,
    AuditEntry,
    PendingLogin,
    User,
    UserGroup,
    UserIdentity,
    UserSession,
)
from app.services import totp
from app.services.login import LoginStatus, authenticate
from app.services.notify_events import NotificationEvent, UserChange, UserChanged
from app.services.sso_refusal import (
    ALREADY_LINKED,
    CREATION_OFF,
    CREATION_UNVERIFIED,
    EMAIL_COLLISION,
    EMAIL_MISSING,
    EMAIL_UNVERIFIED,
    LINK_OFF,
    NO_MATCHING_ROLE,
    USER_DISABLED,
    SsoRefusal,
)
from app.tests.auth_helpers import PASSWORD, make_user
from app.tests.sso_helpers import (
    CLIENT_ID,
    EMAIL,
    ISSUER,
    POLICY,
    SUBJECT,
    World,
    build_world,
)


class Announcements:
    """Collects the events a sign in announces."""

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    async def notify(self, event: NotificationEvent) -> None:
        """Keep the event."""
        self.events.append(event)


@pytest.fixture
async def world(engine: AsyncEngine, crypto: CryptoService) -> World:
    """A fake provider with sign in switched on and the default role rows."""
    built = build_world(engine, crypto)
    await built.configure()
    return built


def _roles(*roles: str) -> dict[str, object]:
    """The roles claim of the test client."""
    return {"resource_access": {CLIENT_ID: {"roles": list(roles)}}}


async def _user(world: World, email: str = EMAIL) -> User | None:
    async with create_session_factory(world.engine)() as db:
        return (
            await db.execute(select(User).where(func.lower(User.email) == email.lower()))
        ).scalar_one_or_none()


async def _identities(world: World) -> list[UserIdentity]:
    async with create_session_factory(world.engine)() as db:
        return list((await db.execute(select(UserIdentity))).scalars())


async def _link(world: World, user: User, subject: str = SUBJECT) -> None:
    async with create_session_factory(world.engine)() as db:
        db.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=subject))
        await db.commit()


async def _entries(world: World, action: str) -> list[AuditEntry]:
    async with create_session_factory(world.engine)() as db:
        rows = await db.execute(
            select(AuditEntry).where(AuditEntry.action == action).order_by(AuditEntry.id)
        )
        return list(rows.scalars())


async def _refusal(world: World, **kwargs: object) -> str:
    with pytest.raises(SsoRefusal) as info:
        await world.sign_in(**kwargs)
    return info.value.reason


async def _failure_details(world: World) -> dict[str, object]:
    [entry] = await _entries(world, "login.failure")
    return entry.details


async def test_a_first_sign_in_creates_the_account_with_the_group_of_the_first_matching_row(
    world: World,
) -> None:
    announced = Announcements()
    begun = await world.begin()
    result = await world.finish(begun, notifier=announced)
    assert result.status is LoginStatus.SUCCESS
    user = await _user(world)
    assert user is not None and result.user is not None and result.user.id == user.id
    assert (user.name, user.group, user.is_active) == ("Pat Example", UserGroup.ADMIN, True)
    [identity] = await _identities(world)
    assert (identity.user_id, identity.issuer, identity.subject) == (user.id, ISSUER, SUBJECT)
    assert announced.events == [UserChanged(UserChange.CREATED, "Pat Example", "sso", "admin")]


async def test_a_created_account_has_no_password_and_no_grants_and_the_audit_says_how(
    world: World,
) -> None:
    await world.sign_in()
    user = await _user(world)
    assert user is not None
    assert user.password_hash == UNUSABLE_PASSWORD_HASH
    [entry] = await _entries(world, "user.create")
    assert entry.details == {"group": "admin", "grants": 0, "via": "sso"}
    assert (entry.actor_type, entry.actor_name) == (AuditActorType.SYSTEM, "sso")
    assert (entry.target_id, entry.target_name) == (user.id, "Pat Example")
    assert await _entries(world, "user.sso_link") == []
    for password in (PASSWORD, "", UNUSABLE_PASSWORD_HASH, "!"):
        assert verify_password(password, user.password_hash) == (False, None)


async def test_an_account_made_by_sign_in_cannot_sign_in_with_a_password(
    world: World,
) -> None:
    await world.sign_in()
    async with create_session_factory(world.engine)() as db:
        for password in (PASSWORD, "", UNUSABLE_PASSWORD_HASH):
            result = await authenticate(
                db, world.crypto, POLICY, identifier=EMAIL, password=password
            )
            assert result.status is LoginStatus.INVALID


async def test_the_first_matching_row_decides_and_a_later_one_never_wins(world: World) -> None:
    await world.configure(mappings=(("env-viewer", "viewer"), ("env-admin", "admin")))
    await world.sign_in(changes=_roles("env-admin", "env-viewer", "other"))
    user = await _user(world)
    assert user is not None and user.group is UserGroup.VIEWER


async def test_roles_outside_the_rows_are_ignored_while_one_row_matches(world: World) -> None:
    await world.sign_in(changes=_roles("unrelated", "env-viewer", "x"))
    user = await _user(world)
    assert user is not None and user.group is UserGroup.VIEWER


async def test_the_roles_are_read_from_the_configured_claim_path(world: World) -> None:
    await world.configure(roles_claim="realm.groups")
    await world.sign_in(changes={"realm": {"groups": ["env-viewer"]}})
    user = await _user(world)
    assert user is not None and user.group is UserGroup.VIEWER


@pytest.mark.parametrize(
    "changes",
    [
        _roles(),
        _roles("unrelated"),
        _roles("ENV-ADMIN"),
        {"resource_access": {CLIENT_ID: {"roles": "env-admin"}}},
        {"resource_access": {CLIENT_ID: {"roles": ["env-admin", 7]}}},
        {"resource_access": {CLIENT_ID: {}}},
        {"resource_access": None},
    ],
)
async def test_a_sign_in_without_a_matching_role_is_refused_and_creates_nothing(
    world: World, changes: dict[str, object]
) -> None:
    assert await _refusal(world, changes=changes) == NO_MATCHING_ROLE
    assert await _user(world) is None and await _identities(world) == []
    assert await _entries(world, "user.create") == []
    assert await _failure_details(world) == {
        "method": "sso",
        "reason": NO_MATCHING_ROLE,
        "email": EMAIL,
    }


async def test_an_existing_account_is_linked_by_a_verified_email(world: World) -> None:
    existing = await world.add_user(make_user("pat", UserGroup.ADMIN, email="Pat@Example.test"))
    result = await world.sign_in()
    assert result.status is LoginStatus.SUCCESS and result.user is not None
    assert result.user.id == existing.id
    [identity] = await _identities(world)
    assert (identity.user_id, identity.subject) == (existing.id, SUBJECT)
    [entry] = await _entries(world, "user.sso_link")
    assert entry.details == {"issuer": ISSUER}
    assert (entry.actor_name, entry.target_id) == ("sso", existing.id)
    assert await _entries(world, "user.create") == []
    async with create_session_factory(world.engine)() as db:
        count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    assert count == 1


async def test_a_linked_account_keeps_its_password_sign_in(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await world.sign_in()
    async with create_session_factory(world.engine)() as db:
        result = await authenticate(db, world.crypto, POLICY, identifier=EMAIL, password=PASSWORD)
    assert result.status is LoginStatus.SUCCESS


async def test_an_unverified_email_never_links_to_an_existing_account(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    for verified in (False, None, "true", 1):
        assert await _refusal(world, changes={"email_verified": verified}) == EMAIL_UNVERIFIED
    assert await _identities(world) == []


async def test_linking_by_email_can_be_switched_off(world: World) -> None:
    await world.configure(link_by_email=False)
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    assert await _refusal(world) == LINK_OFF
    assert await _identities(world) == []


async def test_an_account_linked_to_another_identity_is_not_taken_over(world: World) -> None:
    existing = await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await _link(world, existing, subject="somebody-else")
    assert await _refusal(world) == ALREADY_LINKED
    [identity] = await _identities(world)
    assert identity.subject == "somebody-else"


async def test_an_unverified_email_never_creates_an_account(world: World) -> None:
    for verified in (False, None, "true", 1):
        assert await _refusal(world, changes={"email_verified": verified}) == CREATION_UNVERIFIED
    assert await _user(world) is None and await _identities(world) == []
    assert await _entries(world, "user.create") == []


async def test_a_linked_account_signs_in_although_the_email_is_no_longer_verified(
    world: World,
) -> None:
    await world.sign_in()
    result = await world.sign_in(changes={"email_verified": False})
    assert result.status is LoginStatus.SUCCESS


async def test_creating_accounts_can_be_switched_off(world: World) -> None:
    await world.configure(create_users=False)
    assert await _refusal(world) == CREATION_OFF
    assert await _user(world) is None and await _identities(world) == []


async def test_creation_off_still_signs_in_a_linked_or_linkable_account(world: World) -> None:
    await world.configure(create_users=False)
    await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    assert (await world.sign_in()).status is LoginStatus.SUCCESS
    assert (await world.sign_in()).status is LoginStatus.SUCCESS


async def test_the_identity_signs_in_again_whatever_the_link_and_create_switches_say(
    world: World,
) -> None:
    await world.sign_in()
    await world.configure(create_users=False, link_by_email=False)
    assert (await world.sign_in()).status is LoginStatus.SUCCESS
    assert len(await _entries(world, "login.success")) == 2


async def test_the_name_and_email_follow_the_provider_at_every_sign_in(world: World) -> None:
    await world.sign_in()
    await world.sign_in(changes={"name": "Patricia Example", "email": "patricia@example.test"})
    user = await _user(world, "patricia@example.test")
    assert user is not None and user.name == "Patricia Example"
    assert await _user(world, EMAIL) is None
    [entry] = await _entries(world, "user.sso_update")
    assert entry.details == {"changed": ["name", "email"]}
    assert (entry.actor_name, entry.target_id) == ("sso", user.id)


async def test_an_unchanged_name_and_an_email_in_another_case_write_nothing(
    world: World,
) -> None:
    await world.sign_in()
    await world.sign_in(changes={"email": EMAIL.upper()})
    assert await _entries(world, "user.sso_update") == []
    user = await _user(world)
    assert user is not None and user.email == EMAIL


async def test_a_changed_email_that_belongs_to_another_account_is_refused(
    world: World,
) -> None:
    await world.sign_in()
    other = await world.add_user(make_user("kim", UserGroup.VIEWER, email="kim@example.test"))
    assert await _refusal(world, changes={"email": "KIM@example.test"}) == EMAIL_COLLISION
    kim = await _user(world, "kim@example.test")
    pat = await _user(world)
    assert kim is not None and kim.id == other.id and pat is not None
    assert await _entries(world, "user.sso_update") == []
    details = await _failure_details(world)
    assert details["reason"] == EMAIL_COLLISION and details["email"] == "KIM@example.test"


async def test_the_group_follows_the_roles_at_every_sign_in_and_is_audited(world: World) -> None:
    await world.sign_in()
    await world.sign_in(changes=_roles("env-viewer"))
    user = await _user(world)
    assert user is not None and user.group is UserGroup.VIEWER
    await world.sign_in(changes=_roles("env-admin"))
    user = await _user(world)
    assert user is not None and user.group is UserGroup.ADMIN
    changes = await _entries(world, "user.group_change")
    assert [entry.details["to"] for entry in changes] == ["viewer", "admin"]
    assert {(entry.actor_type, entry.actor_name) for entry in changes} == {
        (AuditActorType.SYSTEM, "sso")
    }


async def test_a_sign_in_with_the_same_group_writes_no_group_entry(world: World) -> None:
    await world.sign_in()
    await world.sign_in()
    assert await _entries(world, "user.group_change") == []


async def test_a_linked_existing_account_gets_the_group_of_its_role(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.VIEWER, email=EMAIL))
    await world.sign_in()
    user = await _user(world)
    assert user is not None and user.group is UserGroup.ADMIN
    [entry] = await _entries(world, "user.group_change")
    assert entry.details == {"from": "viewer", "to": "admin"}


async def test_a_superadmin_keeps_the_group_whatever_the_roles_map_to(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.SUPERADMIN, email=EMAIL))
    for roles in (("env-admin",), ("env-viewer",)):
        assert (await world.sign_in(changes=_roles(*roles))).status is LoginStatus.SUCCESS
    user = await _user(world)
    assert user is not None and user.group is UserGroup.SUPERADMIN
    assert await _entries(world, "user.group_change") == []


async def test_a_superadmin_still_needs_a_matching_role(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.SUPERADMIN, email=EMAIL))
    assert await _refusal(world, changes=_roles("unrelated")) == NO_MATCHING_ROLE
    user = await _user(world)
    assert user is not None and user.group is UserGroup.SUPERADMIN


async def test_the_last_active_superadmin_is_never_demoted_by_a_sign_in(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.SUPERADMIN, email=EMAIL))
    await world.sign_in(changes=_roles("env-viewer"))
    user = await _user(world)
    assert user is not None and user.group is UserGroup.SUPERADMIN


async def test_a_superadmin_is_never_created_by_a_mapping(world: World) -> None:
    await world.configure(mappings=(("env-admin", "admin"),))
    await world.sign_in()
    user = await _user(world)
    assert user is not None and user.group is not UserGroup.SUPERADMIN


async def test_a_disabled_account_is_refused_by_identity_and_by_email(world: World) -> None:
    disabled = await world.add_user(make_user("pat", UserGroup.ADMIN, active=False, email=EMAIL))
    assert await _refusal(world) == USER_DISABLED
    assert await _identities(world) == []
    await _link(world, disabled)
    assert await _refusal(world) == USER_DISABLED
    async with create_session_factory(world.engine)() as db:
        sessions = (await db.execute(select(func.count()).select_from(UserSession))).scalar_one()
    assert sessions == 0
    details = (await _entries(world, "login.failure"))[-1].details
    assert details == {"method": "sso", "reason": USER_DISABLED, "email": EMAIL}


async def test_a_disabled_account_is_left_untouched(world: World) -> None:
    await world.add_user(make_user("pat", UserGroup.VIEWER, active=False, email=EMAIL))
    await _refusal(world, changes={"name": "Someone Else"})
    user = await _user(world)
    assert user is not None and (user.name, user.group) == ("pat", UserGroup.VIEWER)
    assert await _entries(world, "user.group_change") == []


async def test_a_refusal_names_the_account_that_was_signing_in(world: World) -> None:
    pat = await world.add_user(make_user("pat", UserGroup.ADMIN, active=False, email=EMAIL))
    await _refusal(world)
    [entry] = await _entries(world, "login.failure")
    assert (entry.actor_user_id, entry.actor_name) == (pat.id, "pat")
    assert entry.ip_address == "203.0.113.9"


@pytest.mark.parametrize("email", [None, "", "no-at-sign", "a b@example.test", "x@nodot"])
async def test_a_token_without_a_usable_email_is_refused(world: World, email: str | None) -> None:
    assert await _refusal(world, changes={"email": email}) == EMAIL_MISSING
    assert await _user(world) is None


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"name": "Pat Example", "preferred_username": "pat"}, "Pat Example"),
        ({"name": "x_y!", "preferred_username": "pat.p"}, "x y"),
        ({"name": None, "preferred_username": "pat_p"}, "pat p"),
        ({"name": None, "preferred_username": None}, "pat"),
        ({"name": "", "preferred_username": 7}, "pat"),
        ({"name": "​​", "preferred_username": None}, "pat"),
    ],
)
async def test_the_display_name_falls_back_and_is_cleaned(
    world: World, claims: dict[str, object], expected: str
) -> None:
    await world.sign_in(changes=claims)
    user = await _user(world)
    assert user is not None and user.name == expected


async def test_a_name_that_cannot_be_made_is_replaced_by_a_fixed_one(world: World) -> None:
    await world.sign_in(
        changes={"name": None, "preferred_username": None, "email": "+++@example.test"}
    )
    user = await _user(world, "+++@example.test")
    assert user is not None and user.name == "SSO user"


async def test_a_long_name_is_cut_to_fit(world: World) -> None:
    await world.sign_in(changes={"name": "n" * 300})
    user = await _user(world)
    assert user is not None and user.name == "n" * 100


async def _enrol_two_factor(world: World, user: User) -> None:
    """Turn the account's own two-factor sign in on."""
    async with create_session_factory(world.engine)() as db:
        stored = await db.get(User, user.id)
        assert stored is not None
        totp.store_secret(stored, totp.generate_secret(), world.crypto)
        stored.totp_enabled = True
        await db.commit()


async def test_sign_in_never_asks_for_the_two_factor_code_even_for_a_superadmin(
    world: World,
) -> None:
    for group in (UserGroup.SUPERADMIN, UserGroup.ADMIN):
        user = await world.add_user(
            make_user(group.value, group, email=f"{group.value}@example.test")
        )
        await _enrol_two_factor(world, user)
        result = await world.sign_in(changes={"email": user.email, "sub": f"sub-{group.value}"})
        assert result.status is LoginStatus.SUCCESS and result.token
    async with create_session_factory(world.engine)() as db:
        pending = (await db.execute(select(func.count()).select_from(PendingLogin))).scalar_one()
        sessions = (await db.execute(select(func.count()).select_from(UserSession))).scalar_one()
    assert (pending, sessions) == (0, 2)


async def test_the_password_path_of_the_same_account_still_asks_for_the_code(
    world: World,
) -> None:
    user = await world.add_user(make_user("pat", UserGroup.ADMIN, email=EMAIL))
    await _enrol_two_factor(world, user)
    async with create_session_factory(world.engine)() as db:
        result = await authenticate(db, world.crypto, POLICY, identifier=EMAIL, password=PASSWORD)
    assert result.status is LoginStatus.TOTP_REQUIRED
