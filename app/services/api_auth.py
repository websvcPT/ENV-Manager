"""Bearer token authentication for the API.

A call proves itself with ``Authorization: Bearer <secret>`` and nothing else; cookies play no
part. Every way of failing (no header, a malformed one, an unknown secret, a revoked, expired or
stopped token) ends in the same refusal with nothing that says which it was; the reason goes to
the audit log only. Failures are counted per client address in the persisted counters, and an
address at the limit is refused before any token is looked at, so the answer never depends on
whether the secret was right.

The token is found by the SHA-256 hash of its secret, a 192 bit random value, so how long an index
lookup takes says nothing about the secret. The stored hash is still compared in constant time.
"""

import hmac
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.models import ApiToken, AuditActorType, AuditResult, LoginCounterKind, User
from app.models.types import utc_now
from app.services.api_errors import ApiRefusal, RefusalKind
from app.services.audit import AuditActor, record_actor_audit, record_audit
from app.services.auth_policy import AuthPolicy
from app.services.lockout import get_counter, register_failure, remaining_lock
from app.services.notify_events import (
    NO_EVENT_NOTIFIER,
    EventNotifier,
    LockoutScope,
    LockoutStarted,
    announce_event,
)
from app.services.permissions import AccessScope, load_token_scope
from app.services.tokens import (
    MAX_SECRET_LENGTH,
    TokenStatus,
    find_token_by_secret,
    hash_secret,
    token_status,
)

ACTION_AUTH_FAILURE = "token.auth_failure"
ACTION_AUTH_LOCKOUT = "token.auth_lockout"
UNAUTHORIZED_MESSAGE = "Invalid or missing API token."
RATE_LIMITED_MESSAGE = "Too many failed attempts. Try again later."
UNKNOWN_ADDRESS = "unknown"
MAX_ADDRESS_LENGTH = 45
_BEARER_SCHEME = "bearer"
_DECOY_HASH = hash_secret("no token has this secret")


@dataclass(frozen=True)
class ApiCaller:
    """An authenticated call: the token, its owner and what the two may reach right now."""

    token: ApiToken
    owner: User
    scope: AccessScope
    ip_address: str | None

    @property
    def actor(self) -> AuditActor:
        """The token acting for its owner, for audit entries and deploy records."""
        return AuditActor.for_token(self.token, self.owner, self.ip_address)

    def may(self, operation: str) -> bool:
        """Whether the token carries the operation."""
        return operation in self.token.operations


def parse_bearer(header: str | None) -> str | None:
    """The secret of an ``Authorization: Bearer <secret>`` header, or None for anything else."""
    if header is None:
        return None
    scheme, separator, secret = header.partition(" ")
    if separator != " " or scheme.lower() != _BEARER_SCHEME:
        return None
    if not secret or len(secret) > MAX_SECRET_LENGTH or not secret.isascii():
        return None
    if not secret.isprintable() or " " in secret:
        return None
    return secret


def _seconds(wait: timedelta) -> int:
    """A wait rounded up to whole seconds, at least one."""
    return max(1, math.ceil(wait.total_seconds()))


def _matches(token: ApiToken | None, secret: str) -> bool:
    """Whether the token's stored hash equals the hash of the secret, compared in constant time.

    Without a token the comparison still runs, against a decoy, so a miss costs the same.
    """
    stored = token.token_hash if token is not None else _DECOY_HASH
    equal = hmac.compare_digest(stored.encode(), hash_secret(secret).encode())
    return equal and token is not None


async def _audit_failure(
    db: AsyncSession,
    *,
    token: ApiToken | None,
    owner: User | None,
    ip_address: str | None,
    reason: str,
    result: AuditResult,
    action: str = ACTION_AUTH_FAILURE,
    details: dict[str, object] | None = None,
) -> None:
    """Add the audit entry of a refused call: the token and owner when they are known.

    The entry holds a reason code and never any part of what the client presented.
    """
    found: dict[str, object] = {"reason": reason, **(details or {})}
    if token is not None and owner is not None:
        await record_actor_audit(
            db,
            AuditActor.for_token(token, owner, ip_address),
            action=action,
            result=result,
            target_type="token",
            target_id=token.id,
            target_name=token.name,
            details=found,
        )
        return
    await record_audit(
        db,
        action=action,
        result=result,
        actor_type=AuditActorType.API,
        ip_address=ip_address,
        details=found,
    )


async def authenticate_token(
    db: AsyncSession,
    header: str | None,
    ip_address: str | None,
    policy: AuthPolicy,
    *,
    notifier: EventNotifier = NO_EVENT_NOTIFIER,
    now: datetime | None = None,
) -> ApiCaller:
    """The caller a bearer token stands for, or an ``ApiRefusal``: 401, or 429 for a locked address.

    On success the token's last used time and address are recorded. Scopes are loaded fresh, so a
    revoked token, a disabled or demoted owner and a changed grant apply on the very next call.
    """
    moment = now or utc_now()
    subject = ip_address or UNKNOWN_ADDRESS
    counter = await get_counter(db, LoginCounterKind.API_IP, subject)
    wait = remaining_lock([counter], moment)
    if wait is not None:
        await _audit_failure(
            db,
            token=None,
            owner=None,
            ip_address=ip_address,
            reason="locked",
            result=AuditResult.BLOCKED,
        )
        await db.commit()
        raise ApiRefusal(RefusalKind.RATE_LIMITED, RATE_LIMITED_MESSAGE, retry_after=_seconds(wait))

    secret = parse_bearer(header)
    reason = "missing" if header is None else "malformed"
    token: ApiToken | None = None
    owner: User | None = None
    if secret is not None:
        reason = "unknown"
        found = await find_token_by_secret(db, secret)
        if _matches(found, secret):
            token = found
    if token is not None:
        owner = await db.get(User, token.owner_id)
        scope = await load_token_scope(db, token, now=moment)
        if owner is not None and scope is not None:
            token.last_used_at = moment
            token.last_used_ip = ip_address[:MAX_ADDRESS_LENGTH] if ip_address else None
            await db.commit()
            return ApiCaller(token, owner, scope, ip_address)
        status = token_status(token, owner, now=moment)[0] if owner is not None else None
        reason = status.value if status not in (None, TokenStatus.ACTIVE) else "unknown"

    metrics.record_login_failure(metrics.CHANNEL_API)
    locked = await register_failure(
        db, LoginCounterKind.API_IP, subject, policy.api_ip_max_failures, policy, moment
    )
    await _audit_failure(
        db,
        token=token,
        owner=owner,
        ip_address=ip_address,
        reason=reason,
        result=AuditResult.FAILURE,
    )
    if locked:
        await _audit_failure(
            db,
            token=token,
            owner=owner,
            ip_address=ip_address,
            reason="too_many_failures",
            result=AuditResult.BLOCKED,
            action=ACTION_AUTH_LOCKOUT,
            details={"minutes": policy.lockout_duration.total_seconds() // 60},
        )
    await db.commit()
    if locked:
        minutes = int(policy.lockout_duration.total_seconds() // 60)
        await announce_event(
            notifier, LockoutStarted(LockoutScope.API_ADDRESS, None, ip_address, minutes)
        )
        raise ApiRefusal(
            RefusalKind.RATE_LIMITED,
            RATE_LIMITED_MESSAGE,
            retry_after=_seconds(policy.lockout_duration),
        )
    raise ApiRefusal(RefusalKind.UNAUTHORIZED, UNAUTHORIZED_MESSAGE)
