"""Tests that every service announces its notification event, once, after its commit.

Each test runs the real service on a real database with a notifier that records the events, and
where it matters the notifier looks at the database while the event arrives, so a change that was
not committed yet would show.
"""

from dataclasses import dataclass, field
from datetime import timedelta

import pyotp
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.models import AuditEntry, User, UserGroup
from app.models.types import utc_now
from app.services import totp, users
from app.services.account import AccountError, AccountLockedError, change_password
from app.services.api_auth import authenticate_token
from app.services.api_errors import ApiRefusal
from app.services.audit import AuditActor
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginResult, LoginStatus, authenticate, complete_totp_login
from app.services.notify_events import (
    LockoutScope,
    LockoutStarted,
    NotificationEvent,
    SignInFromNewAddress,
    UserChange,
    UserChanged,
)
from app.services.sessions import create_session
from app.tests.auth_helpers import PASSWORD, make_user
from app.tests.notify_helpers import RecordingEvents

POLICY = AuthPolicy(user_max_failures=3, ip_max_failures=5, lockout_duration=timedelta(minutes=10))
FIRST, SECOND = "203.0.113.1", "203.0.113.2"


@dataclass
class ProbingEvents(RecordingEvents):
    """Records events together with how many audit entries were committed when each arrived."""

    engine: AsyncEngine | None = None
    committed: list[int] = field(default_factory=list)

    async def notify(self, event: NotificationEvent) -> None:
        """Count the committed audit entries seen from a second session, then record."""
        assert self.engine is not None
        async with create_session_factory(self.engine)() as db:
            self.committed.append(int(await db.scalar(select(func.count(AuditEntry.id))) or 0))
        await super().notify(event)


async def sign_in(
    engine: AsyncEngine,
    crypto: CryptoService,
    notifier: RecordingEvents,
    *,
    name: str = "alice@example.test",
    password: str = PASSWORD,
    address: str | None = FIRST,
) -> LoginResult:
    """One sign-in attempt on its own session."""
    async with create_session_factory(engine)() as db:
        return await authenticate(
            db,
            crypto,
            POLICY,
            identifier=name,
            password=password,
            ip_address=address,
            notifier=notifier,
        )


@pytest.fixture
async def alice(engine: AsyncEngine) -> User:
    """An admin who signs in with the standard password."""
    async with create_session_factory(engine)() as db:
        user = make_user()
        db.add(user)
        await db.commit()
        return user


async def test_the_first_sign_in_is_learned_and_a_later_new_address_is_announced(
    engine: AsyncEngine, crypto: CryptoService, alice: User
) -> None:
    """Only an address the user has not used before is announced, never the first sign-in."""
    notifier = RecordingEvents()
    for address in (FIRST, FIRST, SECOND, SECOND, FIRST):
        assert (await sign_in(engine, crypto, notifier, address=address)).status is (
            LoginStatus.SUCCESS
        )
    assert notifier.events == [SignInFromNewAddress("alice", SECOND)]


async def test_a_sign_in_with_no_address_is_never_announced(
    engine: AsyncEngine, crypto: CryptoService, alice: User
) -> None:
    """Without a client address there is nothing to remember or compare."""
    notifier = RecordingEvents()
    await sign_in(engine, crypto, notifier, address=FIRST)
    await sign_in(engine, crypto, notifier, address=None)
    assert notifier.events == []


async def test_the_new_address_event_arrives_after_the_sign_in_was_committed(
    engine: AsyncEngine, crypto: CryptoService, alice: User
) -> None:
    """The notifier sees the login entry already stored when it is told."""
    notifier = ProbingEvents(engine=engine)
    await sign_in(engine, crypto, notifier, address=FIRST)
    before = len(await _entries(engine))
    await sign_in(engine, crypto, notifier, address=SECOND)
    assert notifier.committed == [before + 1]


async def _entries(engine: AsyncEngine) -> list[AuditEntry]:
    """Every audit entry stored now."""
    async with create_session_factory(engine)() as db:
        return list((await db.execute(select(AuditEntry))).scalars())


async def test_a_failed_sign_in_and_a_locked_attempt_announce_nothing(
    engine: AsyncEngine, crypto: CryptoService, alice: User
) -> None:
    """A wrong password below the limit, and a refused attempt while locked, are not events."""
    notifier = RecordingEvents()
    await sign_in(engine, crypto, notifier, password="wrong")
    await sign_in(engine, crypto, notifier)
    assert notifier.events == []


async def test_the_second_factor_path_announces_a_new_address_too(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """A user with a second factor is announced when the code step completes the sign in."""
    secret = pyotp.random_base32()
    async with create_session_factory(engine)() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        totp.activate(user, secret, 1, crypto, now=utc_now())
        await db.commit()
    notifier = RecordingEvents()
    for address, moment in ((FIRST, utc_now()), (SECOND, utc_now() + timedelta(minutes=2))):
        async with create_session_factory(engine)() as db:
            first = await authenticate(
                db,
                crypto,
                POLICY,
                identifier="alice@example.test",
                password=PASSWORD,
                ip_address=address,
            )
            assert first.status is LoginStatus.TOTP_REQUIRED and first.pending_token
            done = await complete_totp_login(
                db,
                crypto,
                POLICY,
                pending_token=first.pending_token,
                code=pyotp.TOTP(secret).at(moment),
                ip_address=address,
                notifier=notifier,
                now=moment,
            )
            assert done.status is LoginStatus.SUCCESS
    assert notifier.events == [SignInFromNewAddress("alice", SECOND)]


async def test_a_lockout_of_a_user_names_the_user_and_is_announced_once_after_the_commit(
    engine: AsyncEngine, crypto: CryptoService, alice: User
) -> None:
    """Failures below the limit say nothing; the one that locks announces the lock."""
    notifier = ProbingEvents(engine=engine)
    for _ in range(2):
        await sign_in(engine, crypto, notifier, password="wrong")
    assert notifier.events == []
    locked = await sign_in(engine, crypto, notifier, password="wrong")
    assert locked.status is LoginStatus.LOCKED
    assert notifier.events == [LockoutStarted(LockoutScope.USER, "alice", None, 10)]
    assert notifier.committed == [len(await _entries(engine))]
    await sign_in(engine, crypto, notifier, password="wrong")
    assert len(notifier.events) == 1


async def test_a_name_that_belongs_to_no_user_is_never_part_of_the_event(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """Someone typed a password into the name field: the lock is announced without the text."""
    notifier = RecordingEvents()
    typed = "typed-into-the-name-field"
    for _ in range(3):
        await sign_in(engine, crypto, notifier, name=typed, password="x")
    (event,) = notifier.events
    assert event == LockoutStarted(LockoutScope.USER, None, None, 10)
    assert typed not in repr(notifier.events)


async def test_a_lockout_of_an_address_is_announced_with_the_address(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """Many names from one address lock the address, which the event names."""
    notifier = RecordingEvents()
    for number in range(5):
        await sign_in(engine, crypto, notifier, name=f"user{number}", password="x", address=FIRST)
    assert notifier.events == [LockoutStarted(LockoutScope.ADDRESS, None, FIRST, 10)]


async def test_a_name_and_an_address_that_lock_together_are_two_events(
    engine: AsyncEngine, crypto: CryptoService, alice: User
) -> None:
    """Both counters can reach their limit on one attempt, and each lock is told."""
    notifier = RecordingEvents()
    tight = AuthPolicy(
        user_max_failures=1, ip_max_failures=1, lockout_duration=timedelta(minutes=5)
    )
    async with create_session_factory(engine)() as db:
        await authenticate(
            db,
            crypto,
            tight,
            identifier="alice@example.test",
            password="wrong",
            ip_address=FIRST,
            notifier=notifier,
        )
    assert notifier.events == [
        LockoutStarted(LockoutScope.USER, "alice", None, 5),
        LockoutStarted(LockoutScope.ADDRESS, None, FIRST, 5),
    ]


async def test_changing_a_password_is_announced_after_the_commit_without_any_password(
    engine: AsyncEngine, crypto: CryptoService, alice: User
) -> None:
    """The event names the user and the new password never appears in it."""
    notifier = ProbingEvents(engine=engine)
    async with create_session_factory(engine)() as db:
        session = await _session_for(db, alice)
        await change_password(
            db,
            POLICY,
            alice,
            session,
            current_password=PASSWORD,
            new_password="a-brand-new-password",
            confirm_password="a-brand-new-password",
            notifier=notifier,
        )
    assert notifier.events == [UserChanged(UserChange.PASSWORD_CHANGED, "alice", "alice")]
    assert notifier.committed == [len(await _entries(engine))]
    assert "brand-new" not in repr(notifier.events) and PASSWORD not in repr(notifier.events)


async def _session_for(db, user: User):  # type: ignore[no-untyped-def]
    """A session row for the user, as the account page has."""
    await create_session(db, user, POLICY, ip_address=FIRST, user_agent="agent")
    await db.commit()
    from app.models import UserSession

    return (await db.execute(select(UserSession))).scalars().first()


async def test_wrong_current_passwords_that_lock_the_account_are_announced(
    engine: AsyncEngine, alice: User
) -> None:
    """The account screens share the lockout counters and announce the lock the same way."""
    notifier = RecordingEvents()
    async with create_session_factory(engine)() as db:
        session = await _session_for(db, alice)
        for attempt in range(POLICY.user_max_failures):
            expected = AccountLockedError if attempt == 2 else AccountError
            with pytest.raises(expected):
                await change_password(
                    db,
                    POLICY,
                    alice,
                    session,
                    current_password="wrong",
                    new_password="x" * 14,
                    confirm_password="x" * 14,
                    notifier=notifier,
                )
            assert len(notifier.events) == (1 if attempt == 2 else 0)
    assert notifier.events == [LockoutStarted(LockoutScope.USER, "alice", None, 10)]


async def test_bad_api_tokens_that_lock_an_address_are_announced(engine: AsyncEngine) -> None:
    """The API counters announce their lock as a lockout of API requests from the address."""
    notifier = RecordingEvents()
    for _ in range(POLICY.api_ip_max_failures):
        async with create_session_factory(engine)() as db:
            with pytest.raises(ApiRefusal):
                await authenticate_token(
                    db, "Bearer envm_" + "0" * 48, FIRST, POLICY, notifier=notifier
                )
    assert notifier.events == [
        LockoutStarted(LockoutScope.API_ADDRESS, None, FIRST, 10),
    ]


async def test_creating_a_user_is_announced_with_the_group_and_never_the_password(
    engine: AsyncEngine,
) -> None:
    """The one-time password is returned to the caller and is not in the event."""
    notifier = ProbingEvents(engine=engine)
    async with create_session_factory(engine)() as db:
        _user, password = await users.create_user(
            db,
            name="bob",
            email="bob@example.test",
            group=UserGroup.VIEWER,
            actor=AuditActor.cli(),
            notifier=notifier,
        )
    assert notifier.events == [UserChanged(UserChange.CREATED, "bob", "cli", "viewer")]
    assert notifier.committed == [len(await _entries(engine))]
    assert password not in repr(notifier.events)


async def test_a_refused_creation_announces_nothing(engine: AsyncEngine) -> None:
    """Nothing happened, so nothing is announced."""
    notifier = RecordingEvents()
    async with create_session_factory(engine)() as db:
        with pytest.raises(users.InvalidUserError):
            await users.create_user(
                db,
                name="",
                email="x@example.test",
                group=UserGroup.VIEWER,
                actor=AuditActor.cli(),
                notifier=notifier,
            )
    assert notifier.events == []


async def test_disabling_is_announced_with_who_did_it_and_enabling_is_not(
    engine: AsyncEngine, alice: User
) -> None:
    """Only a disabled account is news; the actor is the signed-in user."""
    notifier = RecordingEvents()
    async with create_session_factory(engine)() as db:
        admin = make_user("carol", UserGroup.SUPERADMIN)
        db.add(admin)
        await db.commit()
        target = await db.get(User, alice.id)
        assert target is not None
        await users.set_user_active(
            db, target, False, actor=AuditActor.for_user(admin), notifier=notifier
        )
        await users.set_user_active(
            db, target, True, actor=AuditActor.for_user(admin), notifier=notifier
        )
    assert notifier.events == [UserChanged(UserChange.DISABLED, "alice", "carol")]


async def test_a_refused_disable_of_the_last_superadmin_announces_nothing(
    engine: AsyncEngine,
) -> None:
    """The refusal is audited and nothing is announced."""
    notifier = RecordingEvents()
    async with create_session_factory(engine)() as db:
        only = make_user("root", UserGroup.SUPERADMIN)
        db.add(only)
        await db.commit()
        with pytest.raises(users.LastSuperadminError):
            await users.set_user_active(db, only, False, actor=AuditActor.cli(), notifier=notifier)
    assert notifier.events == []


async def test_a_password_reset_and_a_two_factor_reset_are_announced(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """Both command line actions name the user and cli, and the password is not in the event."""
    notifier = RecordingEvents()
    async with create_session_factory(engine)() as db:
        user = make_user("dave")
        db.add(user)
        await db.flush()
        totp.activate(user, pyotp.random_base32(), 1, crypto, now=utc_now())
        await db.commit()
        password = await users.reset_password(db, user, actor=AuditActor.cli(), notifier=notifier)
        await users.reset_two_factor(db, user, actor=AuditActor.cli(), notifier=notifier)
    assert notifier.events == [
        UserChanged(UserChange.PASSWORD_RESET, "dave", "cli"),
        UserChanged(UserChange.TWO_FACTOR_RESET, "dave", "cli"),
    ]
    assert password not in repr(notifier.events)


async def test_a_two_factor_reset_that_is_refused_announces_nothing(
    engine: AsyncEngine, alice: User
) -> None:
    """A user with no second factor has none to reset."""
    notifier = RecordingEvents()
    async with create_session_factory(engine)() as db:
        target = await db.get(User, alice.id)
        assert target is not None
        with pytest.raises(users.TwoFactorNotOnError):
            await users.reset_two_factor(db, target, actor=AuditActor.cli(), notifier=notifier)
    assert notifier.events == []
