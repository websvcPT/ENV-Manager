"""Read models of the Users screens: the list rows and the user detail.

Everything here is for the superadmin, who sees every account, so nothing is scoped.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiToken, AuditEntry, AuditResult, User, UserGroup, UserIdentity
from app.services.grants import GrantTarget, list_grants, projects_reached
from app.services.sso_identity import is_linked
from app.services.tokens import TokenStatus, token_status

ACTIVITY_LIMIT = 10
PAGE_SIZE = 50
MAX_QUERY_LENGTH = 100


@dataclass(frozen=True)
class UserRow:
    """One line of the users list. ``projects`` is None for a superadmin, who reaches all.

    ``sso`` marks an account linked to a single sign-on identity.
    """

    id: int
    name: str
    email: str
    group: UserGroup
    is_active: bool
    last_login_at: datetime | None
    projects: int | None
    sso: bool = False


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
    """A user with the direct grants, tokens and recent activity the detail page shows.

    ``sso`` marks an account linked to a single sign-on identity.
    """

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
    sso: bool = False


@dataclass(frozen=True)
class UserFilters:
    """The text search and the status of the users list.

    ``query`` matches part of the name or the email, ignoring case. ``status`` is ``all``,
    ``active`` or ``disabled``.
    """

    query: str = ""
    status: str = "all"

    @classmethod
    def parse(cls, params: Mapping[str, str]) -> UserFilters:
        """Filters from query parameters; a value that does not parse is ignored."""
        query = " ".join(params.get("q", "").split())[:MAX_QUERY_LENGTH]
        status = params.get("status", "")
        return cls(query, status if status in ("active", "disabled") else "all")

    @property
    def active(self) -> bool:
        """Whether anything narrows the list."""
        return bool(self.query) or self.status != "all"

    def query_string(self, after: int | None = None) -> str:
        """The filters, and the cursor when there is one, as a query string."""
        pairs: list[tuple[str, str]] = []
        if self.query:
            pairs.append(("q", self.query))
        if self.status != "all":
            pairs.append(("status", self.status))
        if after is not None:
            pairs.append(("after", str(after)))
        return urlencode(pairs)

    def conditions(self) -> list[ColumnElement[bool]]:
        """The conditions of the filters on the users table."""
        found: list[ColumnElement[bool]] = []
        if self.query:
            found.append(
                or_(
                    User.name.icontains(self.query, autoescape=True),
                    User.email.icontains(self.query, autoescape=True),
                )
            )
        if self.status != "all":
            found.append(User.is_active.is_(self.status == "active"))
        return found


@dataclass(frozen=True)
class UserPage:
    """A page of the users list and the cursor of the next one, None on the last page."""

    rows: tuple[UserRow, ...]
    next_after: int | None


async def _behind(db: AsyncSession, user_id: int) -> ColumnElement[bool] | None:
    """The condition for the users after ``user_id`` in the list order, None when it is gone."""
    key = func.lower(User.name)
    found = (await db.execute(select(User.is_active, key).where(User.id == user_id))).one_or_none()
    if found is None:
        return None
    active, name_key = found
    same_state = and_(
        User.is_active == active,
        or_(key > name_key, and_(key == name_key, User.id > user_id)),
    )
    return or_(User.is_active.is_(False), same_state) if active else same_state


async def list_user_rows(
    db: AsyncSession,
    filters: UserFilters | None = None,
    *,
    after: int | None = None,
    limit: int = PAGE_SIZE,
) -> UserPage:
    """A page of the users matching the filters, with the number of projects their grants reach.

    Active accounts come first, then disabled ones, each by name and then id. ``after`` is the
    id of the last user of the previous page: the page starts behind that user in the same
    order, compared in the database so a name of any script sorts the way the list does. An id
    that no longer exists starts from the top again.
    """
    has_identity = exists().where(UserIdentity.user_id == User.id).label("linked")
    query = select(User, has_identity).where(*(filters or UserFilters()).conditions())
    if after is not None:
        later = await _behind(db, after)
        if later is not None:
            query = query.where(later)
    query = query.order_by(User.is_active.desc(), func.lower(User.name), User.id).limit(limit + 1)
    found = list((await db.execute(query)).all())
    more = len(found) > limit
    found = found[:limit]
    users = [user for user, _ in found]
    linked = {user.id for user, has_link in found if has_link}
    reached = await projects_reached(db, [user.id for user in users])
    rows = tuple(
        UserRow(
            id=user.id,
            name=user.name,
            email=user.email,
            group=user.group,
            is_active=user.is_active,
            last_login_at=user.last_login_at,
            projects=None if user.group is UserGroup.SUPERADMIN else len(reached.get(user.id, ())),
            sso=user.id in linked,
        )
        for user in users
    )
    return UserPage(rows, users[-1].id if more else None)


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
        sso=await is_linked(db, user.id),
        grants=await list_grants(db, user.id),
        tokens=tuple(token_row(token, user) for token in tokens),
        activity=tuple(
            ActivityRow(entry.created_at, entry.action, entry.target_name, entry.result)
            for entry in entries
        ),
    )
