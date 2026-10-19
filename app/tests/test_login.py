"""Tests for the login service: credentials, lockouts, second factor and auditing."""

from datetime import datetime, timedelta
from pathlib import Path

import pyotp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_engine, create_session_factory
from app.models import (
    AuditEntry,
    AuditResult,
    LoginCounter,
    PendingLogin,
    User,
    UserGroup,
    UserSession,
)
from app.models.types import utc_now
from app.services import totp
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginResult, LoginStatus, authenticate, complete_totp_login
from app.tests.auth_helpers import PASSWORD, make_user

POLICY = AuthPolicy(user_max_failures=3, ip_max_failures=5, lockout_duration=timedelta(minutes=10))
IP = "203.0.113.7"


async def _seed(engine: AsyncEngine, *users: User) -> None:
    async with create_session_factory(engine)() as db:
        db.add_all(users or [make_user()])
        await db.commit()


async def _audit(engine: AsyncEngine) -> list[AuditEntry]:
    async with create_session_factory(engine)() as db:
        return list((await db.execute(select(AuditEntry).order_by(AuditEntry.id))).scalars())


async def _login(
    engine: AsyncEngine,
    crypto: CryptoService,
    identifier: str = "alice@example.test",
    password: str = PASSWORD,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """One login attempt on its own database session."""
    async with create_session_factory(engine)() as db:
        return await authenticate(
            db,
            crypto,
            POLICY,
            identifier=identifier,
            password=password,
            ip_address=ip_address,
            now=now,
        )


async def test_success_by_email_ignoring_case_starts_a_session(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """The email works in any case, a session is stored, last login is set and it is audited."""
    await _seed(engine)
    exact = await _login(engine, crypto, "alice@example.test", ip_address=IP)
    shouted = await _login(engine, crypto, " ALICE@example.test ")
    assert exact.status is shouted.status is LoginStatus.SUCCESS
    assert exact.token and exact.token != shouted.token
    async with create_session_factory(engine)() as db:
        assert len((await db.execute(select(UserSession))).scalars().all()) == 2
        assert (await db.execute(select(User))).scalar_one().last_login_at is not None
    entries = await _audit(engine)
    assert [entry.action for entry in entries] == ["login.success", "login.success"]
    assert entries[0].actor_name == "alice"
    assert entries[0].ip_address == IP


async def test_a_name_never_signs_in_and_fails_like_a_wrong_password(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """The name of a user, even with the right password, is no sign-in and records no session."""
    await _seed(engine)
    by_name = await _login(engine, crypto, "alice")
    wrong = await _login(engine, crypto, password="wrong")
    assert by_name.status is wrong.status is LoginStatus.INVALID
    assert by_name.token is None and by_name.user is None
    async with create_session_factory(engine)() as db:
        assert (await db.execute(select(UserSession))).scalars().all() == []
    first, _second = await _audit(engine)
    assert first.actor_name is None and first.actor_user_id is None


async def test_users_sharing_a_name_sign_in_by_their_own_email(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """Two users with one display name are told apart by the email alone."""
    await _seed(
        engine,
        make_user("john", email="john.doe@example.test"),
        make_user("john", email="john.roe@example.test"),
    )
    first = await _login(engine, crypto, "john.doe@example.test")
    second = await _login(engine, crypto, "john.roe@example.test")
    assert first.status is second.status is LoginStatus.SUCCESS
    assert first.user is not None and second.user is not None
    assert first.user.id != second.user.id


async def test_wrong_password_and_unknown_user_look_the_same(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """Both answer with a generic invalid result and no session."""
    await _seed(engine)
    wrong = await _login(engine, crypto, password="wrong")
    unknown = await _login(engine, crypto, "nobody")
    assert wrong.status is unknown.status is LoginStatus.INVALID
    assert wrong.token is None and unknown.token is None


async def test_failures_are_audited_without_recording_the_typed_name(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """A failure for a real user names them; one for an unknown name records no name at all."""
    await _seed(engine)
    await _login(engine, crypto, password="wrong")
    await _login(engine, crypto, "hunter2-typed-in-name-field")
    known, unknown = await _audit(engine)
    assert (known.action, known.result, known.actor_name) == (
        "login.failure",
        AuditResult.FAILURE,
        "alice",
    )
    assert unknown.actor_name is None
    assert "hunter2" not in repr(unknown.details) + repr(unknown.target_name)


async def test_user_lockout_after_repeated_failures_and_audit(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """The failure at the limit locks the user, even a correct password is then refused."""
    await _seed(engine)
    for _ in range(2):
        assert (await _login(engine, crypto, password="x")).status is LoginStatus.INVALID
    locking = await _login(engine, crypto, password="x")
    assert locking.status is LoginStatus.LOCKED
    assert locking.retry_after == POLICY.lockout_duration
    refused = await _login(engine, crypto)
    assert refused.status is LoginStatus.LOCKED
    assert refused.token is None
    actions = [(entry.action, entry.result) for entry in await _audit(engine)]
    assert ("login.lockout", AuditResult.BLOCKED) in actions
    assert actions[-1] == ("login.failure", AuditResult.BLOCKED)


async def test_lockout_expires_and_success_resets_the_counter(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """After the lock time the user can sign in again and the counter is cleared."""
    await _seed(engine)
    start = utc_now()
    for _ in range(3):
        await _login(engine, crypto, password="x", now=start)
    still = await _login(engine, crypto, now=start + timedelta(minutes=5))
    assert still.status is LoginStatus.LOCKED
    assert 0 < still.retry_after.total_seconds() <= 300  # type: ignore[union-attr]
    later = start + timedelta(minutes=11)
    assert (await _login(engine, crypto, now=later)).status is LoginStatus.SUCCESS
    async with create_session_factory(engine)() as db:
        counter = (
            await db.execute(
                select(LoginCounter).where(LoginCounter.subject == "alice@example.test")
            )
        ).scalar_one()
    assert (counter.failures, counter.locked_until) == (0, None)


async def test_failures_outside_the_window_do_not_accumulate(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """Old failures expire, so slow guessing does not lock the account."""
    await _seed(engine)
    start = utc_now()
    for index in range(4):
        moment = start + timedelta(minutes=20 * index)
        assert (await _login(engine, crypto, password="x", now=moment)).status is (
            LoginStatus.INVALID
        )


async def test_ip_lockout_covers_many_names(engine: AsyncEngine, crypto: CryptoService) -> None:
    """One address that fails against many names is locked as a whole."""
    await _seed(engine)
    for index in range(4):
        await _login(engine, crypto, f"ghost{index}", ip_address=IP)
    locking = await _login(engine, crypto, "ghost4", ip_address=IP)
    assert locking.status is LoginStatus.LOCKED
    refused = await _login(engine, crypto, ip_address=IP)
    assert refused.status is LoginStatus.LOCKED
    other_address = await _login(engine, crypto, ip_address="198.51.100.9")
    assert other_address.status is LoginStatus.SUCCESS


async def test_lockout_survives_a_restart(
    engine: AsyncEngine, crypto: CryptoService, database_path: Path
) -> None:
    """Counters live in the database, so a new engine still sees the lock."""
    await _seed(engine)
    for _ in range(3):
        await _login(engine, crypto, password="x")
    fresh = create_engine(database_path)
    try:
        assert (await _login(fresh, crypto)).status is LoginStatus.LOCKED
    finally:
        await fresh.dispose()


async def test_disabled_user_cannot_sign_in(engine: AsyncEngine, crypto: CryptoService) -> None:
    """A correct password for a disabled user is a failure with a recorded reason."""
    await _seed(engine, make_user(active=False))
    result = await _login(engine, crypto)
    assert result.status is LoginStatus.INVALID
    assert (await _audit(engine))[0].details == {"reason": "user_disabled"}


async def _enrol_totp(engine: AsyncEngine, crypto: CryptoService) -> str:
    """Give the stored user a second factor and return its secret."""
    secret = totp.generate_secret()
    async with create_session_factory(engine)() as db:
        stored = (await db.execute(select(User))).scalar_one()
        totp.store_secret(stored, secret, crypto)
        stored.totp_enabled = True
        await db.commit()
    return secret


async def _complete(
    engine: AsyncEngine,
    crypto: CryptoService,
    pending_token: str | None,
    code: str,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """One second factor attempt on its own database session."""
    async with create_session_factory(engine)() as db:
        return await complete_totp_login(
            db,
            crypto,
            POLICY,
            pending_token=pending_token or "",
            code=code,
            ip_address=ip_address,
            now=now,
        )


async def test_second_factor_step(engine: AsyncEngine, crypto: CryptoService) -> None:
    """The password alone yields a pending login; the code completes it into a session."""
    await _seed(engine, make_user(group=UserGroup.SUPERADMIN))
    secret = await _enrol_totp(engine, crypto)

    asked = await _login(engine, crypto)
    assert asked.status is LoginStatus.TOTP_REQUIRED and asked.token is None
    assert asked.pending_token
    assert await _audit(engine) == []

    done = await _complete(engine, crypto, asked.pending_token, pyotp.TOTP(secret).now())
    assert done.status is LoginStatus.SUCCESS and done.token
    assert [entry.action for entry in await _audit(engine)] == ["login.success"]
    async with create_session_factory(engine)() as db:
        assert (await db.execute(select(PendingLogin))).scalars().all() == []


async def test_pending_login_is_single_use(engine: AsyncEngine, crypto: CryptoService) -> None:
    """A wrong code consumes the pending login and hands back a fresh one."""
    await _seed(engine)
    secret = await _enrol_totp(engine, crypto)
    asked = await _login(engine, crypto)
    assert asked.pending_token

    wrong = await _complete(engine, crypto, asked.pending_token, "000000")
    assert wrong.status is LoginStatus.INVALID
    assert wrong.pending_token and wrong.pending_token != asked.pending_token
    assert (await _audit(engine))[0].details == {"reason": "invalid_totp_code"}

    stale = await _complete(engine, crypto, asked.pending_token, pyotp.TOTP(secret).now())
    assert stale.status is LoginStatus.PENDING_INVALID and stale.token is None

    done = await _complete(engine, crypto, wrong.pending_token, pyotp.TOTP(secret).now())
    assert done.status is LoginStatus.SUCCESS


async def test_empty_code_is_not_a_failure(engine: AsyncEngine, crypto: CryptoService) -> None:
    """An empty code asks again with a fresh pending login and counts nothing."""
    await _seed(engine)
    await _enrol_totp(engine, crypto)
    asked = await _login(engine, crypto)
    again = await _complete(engine, crypto, asked.pending_token, "  ")
    assert again.status is LoginStatus.TOTP_REQUIRED and again.pending_token
    assert await _audit(engine) == []
    async with create_session_factory(engine)() as db:
        assert (await db.execute(select(LoginCounter))).scalars().all() == []


async def test_pending_login_expires_and_unknown_tokens_are_refused(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """Past the timeout, or with a made-up or missing token, the code step has no login."""
    await _seed(engine)
    secret = await _enrol_totp(engine, crypto)
    start = utc_now()
    asked = await _login(engine, crypto, now=start)
    code = pyotp.TOTP(secret).now()

    late = await _complete(
        engine, crypto, asked.pending_token, code, now=start + POLICY.pending_login_timeout
    )
    assert late.status is LoginStatus.PENDING_INVALID
    for token in ("made-up", None):
        refused = await _complete(engine, crypto, token, code)
        assert refused.status is LoginStatus.PENDING_INVALID
    async with create_session_factory(engine)() as db:
        assert (await db.execute(select(UserSession))).scalars().all() == []


async def test_pending_login_of_a_disabled_user_is_refused(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """A user disabled between the two steps does not get a session."""
    await _seed(engine)
    secret = await _enrol_totp(engine, crypto)
    asked = await _login(engine, crypto)
    async with create_session_factory(engine)() as db:
        (await db.execute(select(User))).scalar_one().is_active = False
        await db.commit()
    result = await _complete(engine, crypto, asked.pending_token, pyotp.TOTP(secret).now())
    assert result.status is LoginStatus.PENDING_INVALID


async def test_code_failures_lock_the_name(engine: AsyncEngine, crypto: CryptoService) -> None:
    """Wrong codes share the counters of wrong passwords and end with a lockout."""
    await _seed(engine)
    await _enrol_totp(engine, crypto)
    asked = await _login(engine, crypto, ip_address=IP)
    token = asked.pending_token
    for _ in range(POLICY.user_max_failures):
        result = await _complete(engine, crypto, token, "000000", IP)
        token = result.pending_token
    assert result.status is LoginStatus.LOCKED and result.pending_token is None

    blocked = await _login(engine, crypto, ip_address=IP)
    assert blocked.status is LoginStatus.LOCKED
    assert [entry.action for entry in await _audit(engine)].count("login.lockout") >= 1


async def test_accepted_code_cannot_be_replayed(engine: AsyncEngine, crypto: CryptoService) -> None:
    """A code that signed in once is refused again, even through a new pending login."""
    await _seed(engine)
    secret = await _enrol_totp(engine, crypto)
    code = pyotp.TOTP(secret).now()
    first = await _login(engine, crypto)
    assert (await _complete(engine, crypto, first.pending_token, code)).status is (
        LoginStatus.SUCCESS
    )

    second = await _login(engine, crypto)
    replay = await _complete(engine, crypto, second.pending_token, code)
    assert replay.status is LoginStatus.INVALID
    assert (await _audit(engine))[-1].details == {"reason": "reused_totp_code"}

    older = pyotp.TOTP(secret).at(utc_now() - timedelta(seconds=30))
    assert (await _complete(engine, crypto, replay.pending_token, older)).status is (
        LoginStatus.INVALID
    )
    async with create_session_factory(engine)() as db:
        assert (await db.execute(select(User))).scalar_one().totp_last_step is not None
