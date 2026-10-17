"""Login: credential check, lockout counters, second factor step and session start."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
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
from app.services.sessions import create_session


class LoginStatus(StrEnum):
    """Outcome of a login attempt."""

    SUCCESS = "success"
    INVALID = "invalid"
    LOCKED = "locked"
    TOTP_REQUIRED = "totp_required"


@dataclass(frozen=True)
class LoginResult:
    """What the caller needs to answer a login attempt."""

    status: LoginStatus
    token: str | None = None
    user: User | None = None
    retry_after: timedelta | None = None


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


async def authenticate(
    db: AsyncSession,
    crypto: CryptoService,
    policy: AuthPolicy,
    *,
    identifier: str,
    password: str,
    totp_code: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """Check a login attempt and start a session on success.

    Failures and lockouts are audited and counted per submitted name and per client address.
    The submitted name is only recorded when it belongs to a user, because people sometimes type
    a password into the name field. A missing second factor code is a request for it, not a
    failure.
    """
    moment = now or utc_now()
    subject = identifier.strip().lower()
    user_counter = await _counter(db, LoginCounterKind.USER, subject) if subject else None
    ip_counter = await _counter(db, LoginCounterKind.IP, ip_address) if ip_address else None

    wait = _remaining([user_counter, ip_counter], moment)
    if wait is not None:
        user = await _find_user(db, subject) if subject else None
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

    user = await _find_user(db, subject) if subject else None
    if user is None:
        verify_decoy(password)
        matched, upgraded_hash = False, None
    else:
        matched, upgraded_hash = verify_password(password, user.password_hash)

    reason = "invalid_credentials"
    if user is not None and matched and user.is_active:
        if user.totp_enabled:
            if not totp_code or not totp_code.strip():
                return LoginResult(LoginStatus.TOTP_REQUIRED)
            if totp.verify_code(user, totp_code, crypto):
                return await _succeed(
                    db, policy, user, upgraded_hash, ip_address, user_agent, subject, moment
                )
            reason = "invalid_totp_code"
        else:
            return await _succeed(
                db, policy, user, upgraded_hash, ip_address, user_agent, subject, moment
            )
    elif user is not None and matched:
        reason = "user_disabled"

    return await _fail(db, policy, user, reason, subject, ip_address, moment)


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
    upgraded_hash: str | None,
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
    if upgraded_hash:
        user.password_hash = upgraded_hash
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
