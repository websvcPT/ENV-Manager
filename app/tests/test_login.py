"""Tests for the login service: credentials, lockouts, second factor and auditing."""

from datetime import datetime, timedelta
from pathlib import Path

import pyotp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_engine, create_session_factory
from app.models import AuditEntry, AuditResult, LoginCounter, User, UserGroup, UserSession
from app.models.types import utc_now
from app.services import totp
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginResult, LoginStatus, authenticate
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
    identifier: str = "alice",
    password: str = PASSWORD,
    totp_code: str | None = None,
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
            totp_code=totp_code,
            ip_address=ip_address,
            now=now,
        )


async def test_success_by_name_and_by_email_starts_a_session(
    engine: AsyncEngine, crypto: CryptoService
) -> None:
    """Both identifiers work, a session is stored, last login is set and the login is audited."""
    await _seed(engine)
    by_name = await _login(engine, crypto, "Alice", ip_address=IP)
    by_email = await _login(engine, crypto, "ALICE@example.test")
    assert by_name.status is by_email.status is LoginStatus.SUCCESS
    assert by_name.token and by_name.token != by_email.token
    async with create_session_factory(engine)() as db:
        assert len((await db.execute(select(UserSession))).scalars().all()) == 2
        assert (await db.execute(select(User))).scalar_one().last_login_at is not None
    entries = await _audit(engine)
    assert [entry.action for entry in entries] == ["login.success", "login.success"]
    assert entries[0].actor_name == "alice"
    assert entries[0].ip_address == IP


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
            await db.execute(select(LoginCounter).where(LoginCounter.subject == "alice"))
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


async def test_second_factor_step(engine: AsyncEngine, crypto: CryptoService) -> None:
    """With TOTP enabled the password alone asks for a code, and the code completes the login."""
    user = make_user(group=UserGroup.SUPERADMIN)
    await _seed(engine, user)
    secret = totp.generate_secret()
    async with create_session_factory(engine)() as db:
        stored = (await db.execute(select(User))).scalar_one()
        totp.store_secret(stored, secret, crypto)
        stored.totp_enabled = True
        await db.commit()

    asked = await _login(engine, crypto)
    assert asked.status is LoginStatus.TOTP_REQUIRED and asked.token is None
    assert await _audit(engine) == []

    wrong = await _login(engine, crypto, totp_code="000000")
    assert wrong.status is LoginStatus.INVALID
    assert (await _audit(engine))[0].details == {"reason": "invalid_totp_code"}

    done = await _login(engine, crypto, totp_code=pyotp.TOTP(secret).now())
    assert done.status is LoginStatus.SUCCESS and done.token
