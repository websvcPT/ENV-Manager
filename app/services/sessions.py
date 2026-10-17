"""Server-side sessions. The cookie holds an opaque token; only its hash is stored."""

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserSession
from app.models.types import utc_now
from app.services.auth_policy import AuthPolicy

TOKEN_BYTES = 32
USER_AGENT_LIMIT = 255


def hash_token(token: str) -> str:
    """Hex SHA-256 of a session token. The token has full entropy, so no salt is needed."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(
    db: AsyncSession,
    user: User,
    policy: AuthPolicy,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> str:
    """Start a session for the user and return the plaintext token for the cookie."""
    moment = now or utc_now()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(
        UserSession(
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=moment,
            last_seen_at=moment,
            expires_at=moment + policy.absolute_timeout,
            ip_address=ip_address,
            user_agent=user_agent[:USER_AGENT_LIMIT] if user_agent else None,
        )
    )
    await db.flush()
    return token


async def resolve_session(
    db: AsyncSession, token: str, policy: AuthPolicy, *, now: datetime | None = None
) -> tuple[UserSession, User] | None:
    """The live session and user for a cookie token, or None.

    A session past its absolute expiry or idle timeout is deleted, as is the session of a user
    who is no longer active. A valid session has its last-seen time refreshed at most once per
    touch interval.
    """
    moment = now or utc_now()
    row = (
        await db.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_token(token))
        )
    ).one_or_none()
    if row is None:
        return None
    user_session, user = row
    idle = moment - user_session.last_seen_at > policy.idle_timeout
    if moment >= user_session.expires_at or idle or not user.is_active:
        await db.delete(user_session)
        await db.commit()
        return None
    if moment - user_session.last_seen_at >= policy.touch_interval:
        user_session.last_seen_at = moment
        await db.commit()
    return user_session, user


async def revoke_session(db: AsyncSession, user_session: UserSession) -> None:
    """End one session."""
    await db.delete(user_session)
    await db.commit()


async def revoke_user_sessions(db: AsyncSession, user_id: int) -> int:
    """End every session of a user and return how many there were. The caller commits."""
    result = await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def purge_expired(
    db: AsyncSession, policy: AuthPolicy, *, now: datetime | None = None
) -> int:
    """Delete sessions that are expired or idle past the timeout and return how many."""
    moment = now or utc_now()
    result = await db.execute(
        delete(UserSession).where(
            (UserSession.expires_at <= moment)
            | (UserSession.last_seen_at <= moment - policy.idle_timeout)
        )
    )
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
