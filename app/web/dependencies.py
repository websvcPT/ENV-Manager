"""Request dependencies: database session, authenticated user and group checks."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoService
from app.core.sessions import SESSION_COOKIE_NAME
from app.models import User, UserGroup, UserSession
from app.services.auth_policy import AuthPolicy
from app.services.permissions import AccessScope, Capability, load_user_scope
from app.services.sessions import resolve_session


@dataclass(frozen=True)
class AuthContext:
    """The signed-in user and the session that proved it."""

    user: User
    session: UserSession


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One database session per request."""
    async with request.app.state.session_factory() as db:
        yield db


def get_crypto(request: Request) -> CryptoService:
    """The application crypto service."""
    crypto: CryptoService = request.app.state.crypto
    return crypto


def get_auth_policy(request: Request) -> AuthPolicy:
    """The active authentication thresholds."""
    policy: AuthPolicy = request.app.state.auth_policy
    return policy


async def require_auth(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    policy: Annotated[AuthPolicy, Depends(get_auth_policy)],
) -> AuthContext:
    """Resolve the session cookie to a signed-in user, or answer 401.

    Every route declares this dependency (directly or through ``require_group``) unless it is on
    the explicit public list; a test fails when a route has neither.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    resolved = await resolve_session(db, token, policy) if token else None
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    context = AuthContext(user=resolved[1], session=resolved[0])
    request.state.auth = context
    return context


async def resolve_optional_auth(request: Request) -> AuthContext | None:
    """The signed-in user when the request carries a live session, else None.

    Used by error pages for routes that never ran authentication (an unknown path). Any
    failure to look the session up counts as anonymous.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        policy: AuthPolicy = request.app.state.auth_policy
        async with request.app.state.session_factory() as db:
            resolved = await resolve_session(db, token, policy)
    except Exception:
        return None
    return AuthContext(user=resolved[1], session=resolved[0]) if resolved else None


def require_group(*groups: UserGroup) -> Callable[..., Awaitable[AuthContext]]:
    """A dependency that admits only users in one of the given groups, else answers 403."""

    async def check(context: Annotated[AuthContext, Depends(require_auth)]) -> AuthContext:
        if context.user.group not in groups:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return context

    return check


async def get_scope(
    context: Annotated[AuthContext, Depends(require_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccessScope:
    """What the signed-in user may reach, loaded fresh so grant changes apply at once."""
    return await load_user_scope(db, context.user)


def require_capability(capability: Capability) -> Callable[..., Awaitable[AuthContext]]:
    """A dependency that admits only callers whose group holds a capability without a target.

    Used for superadmin-only structure changes, user and settings management and similar
    actions. Answers 403 otherwise, after authentication has already proved who is asking.
    """

    async def check(
        context: Annotated[AuthContext, Depends(require_auth)],
        scope: Annotated[AccessScope, Depends(get_scope)],
    ) -> AuthContext:
        if not scope.can(capability):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")
        return context

    return check
