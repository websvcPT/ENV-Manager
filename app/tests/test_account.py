"""The account service: password change and the second factor, with lockout and audit rules."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.core.passwords import verify_password
from app.models import (
    AuditResult,
    LoginCounter,
    LoginCounterKind,
    PendingLogin,
    User,
    UserSession,
)
from app.services import totp
from app.services.account import (
    AccountError,
    AccountLockedError,
    change_password,
    disable_totp,
    enable_totp,
)
from app.services.auth_policy import AuthPolicy
from app.services.pending_logins import create_pending_login
from app.services.sessions import create_session
from app.tests.audit_helpers import audit_entries
from app.tests.auth_helpers import PASSWORD, make_user

POLICY = AuthPolicy()
NEW_PASSWORD = "a different long password"
ADDRESS = "203.0.113.9"
# A fixed moment in the middle of a time step, so no test straddles the change of the code.
MOMENT = datetime(2026, 10, 18, 12, 0, 10, tzinfo=UTC)


@dataclass
class Account:
    """An open database session, the user and the session the requests come from."""

    db: AsyncSession
    user: User
    session: UserSession
    other_session: UserSession


@pytest.fixture
async def account(engine: AsyncEngine) -> AsyncIterator[Account]:
    """A user with two sessions: the current one and one on another device."""
    factory = create_session_factory(engine)
    async with factory() as db:
        user = make_user()
        db.add(user)
        await db.flush()
        for agent in ("current-agent", "other-agent"):
            await create_session(db, user, POLICY, ip_address=ADDRESS, user_agent=agent)
        await db.commit()
        sessions = list((await db.execute(select(UserSession).order_by(UserSession.id))).scalars())
        yield Account(db, user, sessions[0], sessions[1])


def current_code(secret: str) -> str:
    """The authenticator code of the time step of MOMENT."""
    return pyotp.TOTP(secret).at(MOMENT)


async def counters(db: AsyncSession) -> dict[LoginCounterKind, int]:
    """Failures counted so far, by kind."""
    rows = (await db.execute(select(LoginCounter))).scalars()
    return {row.kind: row.failures for row in rows}


async def change(account: Account, **overrides: str) -> int:
    """Change the password with valid fields unless overridden."""
    fields: dict[str, Any] = {
        "current_password": PASSWORD,
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
        **overrides,
    }
    return await change_password(
        account.db, POLICY, account.user, account.session, ip_address=ADDRESS, now=MOMENT, **fields
    )


async def test_changing_the_password_stores_a_new_hash_and_keeps_only_this_session(
    account: Account,
) -> None:
    ended = await change(account)
    assert ended == 1
    assert verify_password(NEW_PASSWORD, account.user.password_hash)[0]
    assert not verify_password(PASSWORD, account.user.password_hash)[0]
    left = (await account.db.execute(select(UserSession.id))).scalars().all()
    assert left == [account.session.id]


async def test_changing_the_password_ends_unfinished_second_factor_sign_ins(
    account: Account,
) -> None:
    await create_pending_login(account.db, account.user, POLICY)
    await account.db.commit()
    await change(account)
    assert (await account.db.execute(select(PendingLogin.id))).scalars().all() == []


async def test_the_change_is_audited_without_any_password(account: Account) -> None:
    await change(account)
    (entry,) = await audit_entries(account.db, "account.password_change")
    assert entry.result is AuditResult.SUCCESS
    assert entry.ip_address == ADDRESS and entry.actor_name == "alice"
    assert entry.target_type == "user" and entry.target_id == account.user.id
    assert entry.details == {"sessions_ended": 1}
    stored = repr([entry.details, entry.target_name, entry.actor_name])
    assert PASSWORD not in stored and NEW_PASSWORD not in stored


async def test_a_wrong_current_password_counts_toward_the_lockout_and_changes_nothing(
    account: Account,
) -> None:
    before = account.user.password_hash
    with pytest.raises(AccountError) as refused:
        await change(account, current_password="not the password")
    assert refused.value.field == "current_password"
    assert account.user.password_hash == before
    assert await counters(account.db) == {LoginCounterKind.USER: 1, LoginCounterKind.IP: 1}
    (entry,) = await audit_entries(account.db, "account.password_change")
    assert entry.result is AuditResult.FAILURE
    assert entry.details == {"reason": "invalid_current_password"}
    assert len((await account.db.execute(select(UserSession.id))).scalars().all()) == 2


async def test_repeated_wrong_passwords_lock_the_account_screens(account: Account) -> None:
    for _ in range(POLICY.user_max_failures - 1):
        with pytest.raises(AccountError):
            await change(account, current_password="nope")
    with pytest.raises(AccountLockedError) as locked:
        await change(account, current_password="nope")
    assert locked.value.retry_after == POLICY.lockout_duration
    scopes = [entry.details["scope"] for entry in await audit_entries(account.db, "login.lockout")]
    assert scopes == ["user"]
    with pytest.raises(AccountLockedError):
        await change(account)
    assert verify_password(PASSWORD, account.user.password_hash)[0]
    blocked = (await audit_entries(account.db, "account.password_change"))[-1]
    assert blocked.result is AuditResult.BLOCKED and blocked.details == {"reason": "locked"}


async def test_a_successful_change_clears_the_name_failures(account: Account) -> None:
    with pytest.raises(AccountError):
        await change(account, current_password="nope")
    await change(account)
    stored = (
        await account.db.execute(
            select(LoginCounter).where(LoginCounter.kind == LoginCounterKind.USER)
        )
    ).scalar_one()
    assert stored.failures == 0 and stored.locked_until is None


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"current_password": ""}, "current_password", "Enter your current password."),
        ({"new_password": "", "confirm_password": ""}, "new_password", "Enter a new password."),
        (
            {"new_password": "short", "confirm_password": "short"},
            "new_password",
            "The new password must be at least 12 characters.",
        ),
        (
            {"new_password": PASSWORD, "confirm_password": PASSWORD},
            "new_password",
            "The new password must differ from the current one.",
        ),
        ({"confirm_password": "another long password"}, "confirm_password", "do not match"),
    ],
)
async def test_refused_passwords_name_the_field_and_are_not_counted(
    account: Account, overrides: dict[str, str], field: str, message: str
) -> None:
    before = account.user.password_hash
    with pytest.raises(AccountError) as refused:
        await change(account, **overrides)
    assert refused.value.field == field and message in str(refused.value)
    assert account.user.password_hash == before
    assert await counters(account.db) == {}


async def enable(
    account: Account,
    crypto: CryptoService,
    secret: str,
    *,
    password: str = PASSWORD,
    code: str | None = None,
) -> None:
    """Turn the second factor on with the given fields; valid ones unless overridden."""
    await enable_totp(
        account.db,
        POLICY,
        account.user,
        crypto,
        secret=secret,
        password=password,
        code=current_code(secret) if code is None else code,
        ip_address=ADDRESS,
        now=MOMENT,
    )


async def test_enabling_stores_the_secret_as_ciphertext_only_after_a_valid_code(
    account: Account, crypto: CryptoService
) -> None:
    secret = totp.generate_secret()
    with pytest.raises(AccountError) as wrong:
        await enable(account, crypto, secret, code="000000")
    assert wrong.value.field == "code"
    assert account.user.totp_secret is None and not account.user.totp_enabled
    assert await counters(account.db) == {}

    code = current_code(secret)
    await enable(account, crypto, secret, code=code)
    assert account.user.totp_enabled and account.user.totp_enabled_at is not None
    assert account.user.totp_secret is not None and secret not in account.user.totp_secret
    assert totp.read_secret(account.user, crypto) == secret
    assert account.user.totp_last_step == pyotp.TOTP(secret).timecode(MOMENT)
    (entry,) = await audit_entries(account.db, "account.totp_enable")
    assert entry.result is AuditResult.SUCCESS and entry.details == {}
    assert secret not in repr(entry.details) and code not in repr(entry.details)


async def test_enabling_refuses_empty_fields_and_an_account_that_has_it_on(
    account: Account, crypto: CryptoService
) -> None:
    secret = totp.generate_secret()
    with pytest.raises(AccountError, match="Enter the 6-digit code") as no_code:
        await enable(account, crypto, secret, code="  ")
    assert no_code.value.field == "code"
    with pytest.raises(AccountError, match="Enter your current password") as no_password:
        await enable(account, crypto, secret, password="")
    assert no_password.value.field == "current_password"
    assert await counters(account.db) == {}
    await enable(account, crypto, secret)
    with pytest.raises(AccountError, match="already on"):
        await enable(account, crypto, secret)


async def test_enabling_with_a_wrong_password_counts_and_leaves_the_code_unused(
    account: Account, crypto: CryptoService
) -> None:
    """A valid code does not help without the password, and the password is checked first."""
    secret = totp.generate_secret()
    with pytest.raises(AccountError) as refused:
        await enable(account, crypto, secret, password="not the password")
    assert refused.value.field == "current_password"
    assert not account.user.totp_enabled and account.user.totp_secret is None
    assert account.user.totp_last_step is None
    assert await counters(account.db) == {LoginCounterKind.USER: 1, LoginCounterKind.IP: 1}
    (entry,) = await audit_entries(account.db, "account.totp_enable")
    assert entry.result is AuditResult.FAILURE and entry.details == {"reason": "invalid_password"}
    stored = repr([entry.details, entry.target_name, entry.actor_name])
    assert "not the password" not in stored and secret not in stored


async def test_a_wrong_code_after_the_right_password_is_not_counted(
    account: Account, crypto: CryptoService
) -> None:
    """Nothing is guessed about a secret that was just shown, so a wrong code costs no failure."""
    secret = totp.generate_secret()
    with pytest.raises(AccountError) as refused:
        await enable(account, crypto, secret, code="000000")
    assert refused.value.field == "code"
    assert await counters(account.db) == {}
    assert await audit_entries(account.db, "account.totp_enable") == []


async def test_enabling_is_refused_while_the_name_or_address_is_locked(
    account: Account, crypto: CryptoService
) -> None:
    """Repeated wrong passwords lock turning it on, and the lock is audited as blocked."""
    secret = totp.generate_secret()
    for _ in range(POLICY.user_max_failures):
        with pytest.raises((AccountError, AccountLockedError)):
            await enable(account, crypto, secret, password="nope")
    with pytest.raises(AccountLockedError) as locked:
        await enable(account, crypto, secret)
    assert locked.value.retry_after > timedelta(0)
    assert not account.user.totp_enabled and account.user.totp_secret is None
    blocked = (await audit_entries(account.db, "account.totp_enable"))[-1]
    assert blocked.result is AuditResult.BLOCKED and blocked.details == {"reason": "locked"}


async def turn_on(account: Account, crypto: CryptoService) -> str:
    """Turn the second factor on and return its secret."""
    secret = totp.generate_secret()
    await enable(account, crypto, secret)
    return secret


def next_code(secret: str) -> str:
    """The code of the next time step: inside the accepted window and newer than the current."""
    return pyotp.TOTP(secret).at(MOMENT + timedelta(seconds=30))


async def remove(account: Account, crypto: CryptoService, *, password: str, code: str) -> None:
    """Remove the second factor with the given fields."""
    await disable_totp(
        account.db,
        POLICY,
        account.user,
        crypto,
        password=password,
        code=code,
        ip_address=ADDRESS,
        now=MOMENT,
    )


async def test_removing_needs_the_password_and_a_fresh_code_and_clears_everything(
    account: Account, crypto: CryptoService
) -> None:
    secret = await turn_on(account, crypto)
    await remove(account, crypto, password=PASSWORD, code=next_code(secret))
    user = account.user
    assert (user.totp_enabled, user.totp_secret, user.totp_enabled_at, user.totp_last_step) == (
        False,
        None,
        None,
        None,
    )
    (entry,) = await audit_entries(account.db, "account.totp_disable")
    assert entry.result is AuditResult.SUCCESS
    assert secret not in repr(entry.details)


async def test_the_code_that_turned_it_on_cannot_remove_it(
    account: Account, crypto: CryptoService
) -> None:
    secret = await turn_on(account, crypto)
    with pytest.raises(AccountError) as refused:
        await remove(account, crypto, password=PASSWORD, code=current_code(secret))
    assert refused.value.field == "code"
    assert account.user.totp_enabled
    (entry,) = await audit_entries(account.db, "account.totp_disable")
    assert entry.details == {"reason": "reused_code"}
    assert await counters(account.db) == {LoginCounterKind.USER: 1, LoginCounterKind.IP: 1}


async def test_removing_with_a_wrong_password_or_code_counts_and_changes_nothing(
    account: Account, crypto: CryptoService
) -> None:
    secret = await turn_on(account, crypto)
    with pytest.raises(AccountError) as bad_password:
        await remove(account, crypto, password="nope", code=next_code(secret))
    assert bad_password.value.field == "current_password"
    with pytest.raises(AccountError) as bad_code:
        await remove(account, crypto, password=PASSWORD, code="000000")
    assert bad_code.value.field == "code"
    assert account.user.totp_enabled
    entries = await audit_entries(account.db, "account.totp_disable")
    reasons = [entry.details["reason"] for entry in entries]
    assert reasons == ["invalid_password", "invalid_code"]
    assert (await counters(account.db))[LoginCounterKind.USER] == 2


async def test_removing_is_refused_while_locked_and_when_it_is_not_on(
    account: Account, crypto: CryptoService
) -> None:
    with pytest.raises(AccountError, match="not on"):
        await remove(account, crypto, password=PASSWORD, code="123456")
    secret = await turn_on(account, crypto)
    for _ in range(POLICY.user_max_failures):
        with pytest.raises((AccountError, AccountLockedError)):
            await remove(account, crypto, password="nope", code=next_code(secret))
    with pytest.raises(AccountLockedError):
        await remove(account, crypto, password=PASSWORD, code=next_code(secret))
    assert account.user.totp_enabled
