"""Pending logins: a verified password waiting for the second factor code."""

import secrets
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PendingLogin, User
from app.models.types import utc_now
from app.services.auth_policy import AuthPolicy
from app.services.sessions import TOKEN_BYTES, hash_token


async def create_pending_login(
    db: AsyncSession, user: User, policy: AuthPolicy, *, now: datetime | None = None
) -> str:
    """Store a short-lived pending login for the user and return its plaintext token.

    Expired pending logins are removed on the way. The caller commits.
    """
    moment = now or utc_now()
    await db.execute(delete(PendingLogin).where(PendingLogin.expires_at <= moment))
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(
        PendingLogin(
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=moment,
            expires_at=moment + policy.pending_login_timeout,
        )
    )
    await db.flush()
    return token


async def consume_pending_login(
    db: AsyncSession, token: str, *, now: datetime | None = None
) -> int | None:
    """Use up a pending login and return its user id, or None when it is unknown or expired.

    The row is deleted in one statement and committed, so concurrent requests cannot both use
    the same token, and a token that was presented is gone whatever the outcome.
    """
    moment = now or utc_now()
    row = (
        await db.execute(
            delete(PendingLogin)
            .where(PendingLogin.token_hash == hash_token(token))
            .returning(PendingLogin.user_id, PendingLogin.expires_at)
        )
    ).one_or_none()
    await db.commit()
    if row is None or row.expires_at <= moment:
        return None
    return int(row.user_id)


async def discard_user_pending_logins(db: AsyncSession, user_id: int) -> int:
    """Remove every pending login of a user.

    A password step passed before a password change can then not be finished afterwards. The
    caller commits.
    """
    result = await db.execute(delete(PendingLogin).where(PendingLogin.user_id == user_id))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
