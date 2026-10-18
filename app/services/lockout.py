"""Failure counters with a lockout, persisted so a restart does not clear them.

Sign-in and the account screens (password change, removing the second factor) count failures per
user name and per client address; the API counts failed bearer token attempts per client address
under a kind of its own, so the two never lock each other out. Counting is one atomic statement,
so concurrent failures from one subject are never lost.
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, case, literal, null, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditActorType, AuditResult, LoginCounter, LoginCounterKind, User
from app.models.types import UtcDateTime
from app.services.audit import record_audit
from app.services.auth_policy import AuthPolicy


async def get_counter(
    db: AsyncSession, kind: LoginCounterKind, subject: str
) -> LoginCounter | None:
    """The failure counter for a subject, if one exists."""
    return (
        await db.execute(
            select(LoginCounter).where(LoginCounter.kind == kind, LoginCounter.subject == subject)
        )
    ).scalar_one_or_none()


def remaining_lock(counters: list[LoginCounter | None], now: datetime) -> timedelta | None:
    """The longest time any of the counters is still locked for, or None when none is locked."""
    waits = [
        counter.locked_until - now
        for counter in counters
        if counter is not None and counter.locked_until is not None and counter.locked_until > now
    ]
    return max(waits) if waits else None


async def register_failure(
    db: AsyncSession,
    kind: LoginCounterKind,
    subject: str,
    limit: int,
    policy: AuthPolicy,
    now: datetime,
) -> bool:
    """Count one failure and start a lockout at the limit. Returns whether it locked.

    A counter whose window has run out or whose lock has ended starts again at one. The row is
    created or incremented by a single upsert, so two failures that arrive together are both
    counted and neither can fail on the unique key.
    """
    moment = literal(now, UtcDateTime())
    stale = or_(
        and_(LoginCounter.locked_until.is_not(None), LoginCounter.locked_until <= now),
        LoginCounter.window_started_at < now - policy.failure_window,
    )
    upsert = (
        insert(LoginCounter)
        .values(kind=kind, subject=subject, failures=1, window_started_at=now)
        .on_conflict_do_update(
            index_elements=[LoginCounter.kind, LoginCounter.subject],
            set_={
                "failures": case((stale, 1), else_=LoginCounter.failures + 1),
                "window_started_at": case((stale, moment), else_=LoginCounter.window_started_at),
                "locked_until": case((stale, null()), else_=LoginCounter.locked_until),
            },
        )
    )
    await db.execute(upsert)
    counter = (
        await db.execute(
            select(LoginCounter)
            .where(LoginCounter.kind == kind, LoginCounter.subject == subject)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if counter.failures >= limit:
        counter.locked_until = now + policy.lockout_duration
        return True
    return False


async def register_attempt_failure(
    db: AsyncSession,
    policy: AuthPolicy,
    *,
    subject: str,
    ip_address: str | None,
    now: datetime,
) -> list[str]:
    """Count one failed credential check against the name and the client address.

    Returns which of the two ("user", "ip") reached their limit and are now locked. Sign in and
    the account screens share these counters, so a guess costs the same wherever it is made.
    """
    locked: list[str] = []
    if subject and await register_failure(
        db, LoginCounterKind.USER, subject, policy.user_max_failures, policy, now
    ):
        locked.append("user")
    if ip_address and await register_failure(
        db, LoginCounterKind.IP, ip_address, policy.ip_max_failures, policy, now
    ):
        locked.append("ip")
    return locked


async def record_lockouts(
    db: AsyncSession,
    policy: AuthPolicy,
    *,
    user: User | None,
    ip_address: str | None,
    locked: list[str],
) -> None:
    """Audit each lockout that a failed attempt started. The caller commits."""
    for scope in locked:
        await record_audit(
            db,
            action="login.lockout",
            result=AuditResult.BLOCKED,
            actor_type=AuditActorType.USER,
            actor_user=user,
            ip_address=ip_address,
            details={"scope": scope, "minutes": policy.lockout_duration.total_seconds() // 60},
        )


async def current_lock(
    db: AsyncSession, *, subject: str, ip_address: str | None, now: datetime
) -> timedelta | None:
    """How long the name or the client address stays locked, or None when neither is."""
    counters = [
        await get_counter(db, LoginCounterKind.USER, subject),
        await get_counter(db, LoginCounterKind.IP, ip_address) if ip_address else None,
    ]
    return remaining_lock(counters, now)
