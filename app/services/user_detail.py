"""Read models of the Users screens: the list rows and the user detail.

Everything here is for the superadmin, who sees every account, so nothing is scoped.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiToken, AuditEntry, AuditResult, User, UserGroup
from app.services.grants import GrantTarget, list_grants, projects_reached
from app.services.tokens import TokenStatus, token_status

ACTIVITY_LIMIT = 10


@dataclass(frozen=True)
class UserRow:
    """One line of the users list. ``projects`` is None for a superadmin, who reaches all."""

    id: int
    name: str
    email: str
    group: UserGroup
    is_active: bool
    last_login_at: datetime | None
    projects: int | None


@dataclass(frozen=True)
class TokenRow:
    """A token of the user. ``reason`` says why an otherwise valid token is stopped.

    ``revocable`` is True while revoking the token still changes something.
    """

    id: int
    name: str
    last_used_at: datetime | None
    status: TokenStatus
    reason: str | None
    revocable: bool


@dataclass(frozen=True)
class ActivityRow:
    """One audit entry the user made."""

    created_at: datetime
    action: str
    object_name: str | None
    result: AuditResult


@dataclass(frozen=True)
class UserDetail:
    """A user with the direct grants, tokens and recent activity the detail page shows."""

    id: int
    name: str
    email: str
    group: UserGroup
    is_active: bool
    last_login_at: datetime | None
    totp_enabled: bool
    grants: frozenset[GrantTarget]
    tokens: tuple[TokenRow, ...]
    activity: tuple[ActivityRow, ...]


async def list_user_rows(db: AsyncSession) -> list[UserRow]:
    """Every user ordered by name, with the number of projects their grants reach."""
    reached = await projects_reached(db)
    users = (await db.execute(select(User).order_by(func.lower(User.name), User.id))).scalars()
    return [
        UserRow(
            id=user.id,
            name=user.name,
            email=user.email,
            group=user.group,
            is_active=user.is_active,
            last_login_at=user.last_login_at,
            projects=None if user.group is UserGroup.SUPERADMIN else len(reached.get(user.id, ())),
        )
        for user in users
    ]


def token_row(token: ApiToken, owner: User, *, now: datetime | None = None) -> TokenRow:
    """The row of a token: its status (see ``token_status``) and whether it can be revoked."""
    status, reason = token_status(token, owner, now=now)
    revocable = status in (TokenStatus.ACTIVE, TokenStatus.STOPPED)
    return TokenRow(token.id, token.name, token.last_used_at, status, reason, revocable)


async def load_user_detail(db: AsyncSession, user_id: int) -> UserDetail | None:
    """The detail of one user, or None when there is no such user."""
    user = await db.get(User, user_id)
    if user is None:
        return None
    tokens = (
        await db.execute(select(ApiToken).where(ApiToken.owner_id == user.id).order_by(ApiToken.id))
    ).scalars()
    entries = (
        await db.execute(
            select(AuditEntry)
            .where(AuditEntry.actor_user_id == user.id)
            .order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc())
            .limit(ACTIVITY_LIMIT)
        )
    ).scalars()
    return UserDetail(
        id=user.id,
        name=user.name,
        email=user.email,
        group=user.group,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        totp_enabled=user.totp_enabled,
        grants=await list_grants(db, user.id),
        tokens=tuple(token_row(token, user) for token in tokens),
        activity=tuple(
            ActivityRow(entry.created_at, entry.action, entry.target_name, entry.result)
            for entry in entries
        ),
    )
