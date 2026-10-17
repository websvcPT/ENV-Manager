"""Login: credential check, lockout counters, second factor step and session start."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.passwords import verify_decoy, verify_password
from app.models import (
    AuditActorType,
    AuditResult,
    LoginCounter,
    LoginCounterKind,
    User,
)
from app.models.types import utc_now
from app.services import totp
from app.services.audit import record_audit
from app.services.auth_policy import AuthPolicy
from app.services.pending_logins import consume_pending_login, create_pending_login
from app.services.sessions import create_session


class LoginStatus(StrEnum):
    """Outcome of a login attempt."""

    SUCCESS = "success"
    INVALID = "invalid"
    LOCKED = "locked"
    TOTP_REQUIRED = "totp_required"
    PENDING_INVALID = "pending_invalid"


@dataclass(frozen=True)
class LoginResult:
    """What the caller needs to answer a login attempt.

    `pending_token` accompanies TOTP_REQUIRED and a wrong code (INVALID after the password
    step): it is the fresh single-use token the next code attempt must present.
    """

    status: LoginStatus
    token: str | None = None
    user: User | None = None
    retry_after: timedelta | None = None
    pending_token: str | None = None


async def _counter(db: AsyncSession, kind: LoginCounterKind, subject: str) -> LoginCounter | None:
    """The failure counter for a subject, if one exists."""
    return (
        await db.execute(
            select(LoginCounter).where(LoginCounter.kind == kind, LoginCounter.subject == subject)
        )
    ).scalar_one_or_none()


def _remaining(counters: list[LoginCounter | None], now: datetime) -> timedelta | None:
    """The longest time any of the counters is still locked for, or None when none is locked."""
    waits = [
        counter.locked_until - now
        for counter in counters
        if counter is not None and counter.locked_until is not None and counter.locked_until > now
    ]
    return max(waits) if waits else None


async def _register_failure(
    db: AsyncSession,
    kind: LoginCounterKind,
    subject: str,
    limit: int,
    policy: AuthPolicy,
    now: datetime,
) -> bool:
    """Count one failure and start a lockout at the limit. Returns whether it locked."""
    counter = await _counter(db, kind, subject)
    if counter is None:
        counter = LoginCounter(kind=kind, subject=subject, failures=0, window_started_at=now)
        db.add(counter)
    stale_lock = counter.locked_until is not None and counter.locked_until <= now
    if stale_lock or now - counter.window_started_at > policy.failure_window:
        counter.failures = 0
        counter.window_started_at = now
        counter.locked_until = None
    counter.failures += 1
    if counter.failures >= limit:
        counter.locked_until = now + policy.lockout_duration
        return True
    return False


async def _find_user(db: AsyncSession, identifier: str) -> User | None:
    """The user whose name matches, else the one whose email matches when it is unambiguous."""
    lowered = identifier.lower()
    by_name = (
        await db.execute(select(User).where(func.lower(User.name) == lowered))
    ).scalar_one_or_none()
    if by_name is not None:
        return by_name
    matches = (
        (await db.execute(select(User).where(func.lower(User.email) == lowered))).scalars().all()
    )
    return matches[0] if len(matches) == 1 else None


async def _locked_out(
    db: AsyncSession,
    counters: list[LoginCounter | None],
    user: User | None,
    ip_address: str | None,
    now: datetime,
) -> LoginResult | None:
    """A locked result, with its blocked audit entry, when any of the counters is locked."""
    wait = _remaining(counters, now)
    if wait is None:
        return None
    await record_audit(
        db,
        action="login.failure",
        result=AuditResult.BLOCKED,
        actor_type=AuditActorType.USER,
        actor_user=user,
        ip_address=ip_address,
        details={"reason": "locked"},
    )
    await db.commit()
    return LoginResult(LoginStatus.LOCKED, retry_after=wait)


async def authenticate(
    db: AsyncSession,
    crypto: CryptoService,
    policy: AuthPolicy,
    *,
    identifier: str,
    password: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """Check the name and password and start a session on success.

    Failures and lockouts are audited and counted per submitted name and per client address.
    The submitted name is only recorded when it belongs to a user, because people sometimes type
    a password into the name field. A user with a second factor gets a pending login instead of
    a session, and finishes with `complete_totp_login`; that request is not a failure.
    """
    moment = now or utc_now()
    subject = identifier.strip().lower()
    user_counter = await _counter(db, LoginCounterKind.USER, subject) if subject else None
    ip_counter = await _counter(db, LoginCounterKind.IP, ip_address) if ip_address else None

    user = await _find_user(db, subject) if subject else None
    locked = await _locked_out(db, [user_counter, ip_counter], user, ip_address, moment)
    if locked is not None:
        return locked

    if user is None:
        verify_decoy(password)
        matched, upgraded_hash = False, None
    else:
        matched, upgraded_hash = verify_password(password, user.password_hash)

    reason = "invalid_credentials"
    if user is not None and matched and user.is_active:
        if upgraded_hash:
            user.password_hash = upgraded_hash
        if user.totp_enabled:
            pending_token = await create_pending_login(db, user, policy, now=moment)
            await db.commit()
            return LoginResult(LoginStatus.TOTP_REQUIRED, pending_token=pending_token)
        return await _succeed(db, policy, user, ip_address, user_agent, subject, moment)
    if user is not None and matched:
        reason = "user_disabled"

    return await _fail(db, policy, user, reason, subject, ip_address, moment)


async def complete_totp_login(
    db: AsyncSession,
    crypto: CryptoService,
    policy: AuthPolicy,
    *,
    pending_token: str,
    code: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """Finish a login with the authenticator code and the pending login from the password step.

    The pending login is single use: it is consumed by every attempt, and a wrong or empty code
    answers with a fresh one so the person can try again without sending the password again.
    Wrong codes count toward the same lockout counters as wrong passwords. A time step that was
    already accepted for the user is refused, so a code observed in transit cannot be replayed.
    """
    moment = now or utc_now()
    user_id = await consume_pending_login(db, pending_token, now=moment) if pending_token else None
    user = await db.get(User, user_id) if user_id is not None else None
    if user is None or not user.is_active:
        return LoginResult(LoginStatus.PENDING_INVALID)

    subject = user.name.lower()
    user_counter = await _counter(db, LoginCounterKind.USER, subject)
    ip_counter = await _counter(db, LoginCounterKind.IP, ip_address) if ip_address else None
    locked = await _locked_out(db, [user_counter, ip_counter], user, ip_address, moment)
    if locked is not None:
        return locked

    if not code.strip():
        pending = await create_pending_login(db, user, policy, now=moment)
        await db.commit()
        return LoginResult(LoginStatus.TOTP_REQUIRED, pending_token=pending)

    reason = "invalid_totp_code"
    step = totp.matching_step(user, code, crypto, now=moment)
    if step is not None:
        if await _claim_step(db, user, step):
            return await _succeed(db, policy, user, ip_address, user_agent, subject, moment)
        reason = "reused_totp_code"

    failed = await _fail(db, policy, user, reason, subject, ip_address, moment)
    if failed.status is LoginStatus.INVALID:
        pending = await create_pending_login(db, user, policy, now=moment)
        await db.commit()
        return LoginResult(LoginStatus.INVALID, pending_token=pending)
    return failed


async def _claim_step(db: AsyncSession, user: User, step: int) -> bool:
    """Record a time step as used unless it, or a later one, already was. Atomic in one UPDATE."""
    claimed = await db.execute(
        update(User)
        .where(User.id == user.id, or_(User.totp_last_step.is_(None), User.totp_last_step < step))
        .values(totp_last_step=step)
    )
    return claimed.rowcount == 1  # type: ignore[attr-defined, no-any-return]


async def _fail(
    db: AsyncSession,
    policy: AuthPolicy,
    user: User | None,
    reason: str,
    subject: str,
    ip_address: str | None,
    now: datetime,
) -> LoginResult:
    """Count and audit a failed attempt, locking the name or address at the limit."""
    locked_subjects: list[str] = []
    if subject and await _register_failure(
        db, LoginCounterKind.USER, subject, policy.user_max_failures, policy, now
    ):
        locked_subjects.append("user")
    if ip_address and await _register_failure(
        db, LoginCounterKind.IP, ip_address, policy.ip_max_failures, policy, now
    ):
        locked_subjects.append("ip")
    await record_audit(
        db,
        action="login.failure",
        result=AuditResult.FAILURE,
        actor_type=AuditActorType.USER,
        actor_user=user,
        ip_address=ip_address,
        details={"reason": reason},
    )
    for kind in locked_subjects:
        await record_audit(
            db,
            action="login.lockout",
            result=AuditResult.BLOCKED,
            actor_type=AuditActorType.USER,
            actor_user=user,
            ip_address=ip_address,
            details={"scope": kind, "minutes": policy.lockout_duration.total_seconds() // 60},
        )
    await db.commit()
    if locked_subjects:
        return LoginResult(LoginStatus.LOCKED, retry_after=policy.lockout_duration)
    return LoginResult(LoginStatus.INVALID)


async def _succeed(
    db: AsyncSession,
    policy: AuthPolicy,
    user: User,
    ip_address: str | None,
    user_agent: str | None,
    subject: str,
    now: datetime,
) -> LoginResult:
    """Reset the name counter, start the session and audit the login."""
    counter = await _counter(db, LoginCounterKind.USER, subject)
    if counter is not None:
        counter.failures = 0
        counter.locked_until = None
        counter.window_started_at = now
    user.last_login_at = now
    token = await create_session(
        db, user, policy, ip_address=ip_address, user_agent=user_agent, now=now
    )
    await record_audit(
        db,
        action="login.success",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=user,
        ip_address=ip_address,
    )
    await db.commit()
    return LoginResult(LoginStatus.SUCCESS, token=token, user=user)
