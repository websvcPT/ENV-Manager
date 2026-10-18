"""The bearer token dependency every API route declares.

It reads the ``Authorization`` header and nothing else: cookies, sessions and the CSRF token play
no part in the API. A test enumerates the routes and fails when an ``/api`` route lacks this
dependency or uses the cookie dependency of the web pages.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.api_auth import ApiCaller, authenticate_token
from app.services.auth_policy import AuthPolicy
from app.web.dependencies import get_auth_policy, get_db


def client_address(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware (trusted proxies only)."""
    return request.client.host if request.client else None


async def require_api_token(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    policy: Annotated[AuthPolicy, Depends(get_auth_policy)],
) -> ApiCaller:
    """The caller the bearer token stands for, or a refusal that is answered as 401 or 429.

    Every failure answers alike, so nothing says whether a token exists, is revoked, expired or
    stopped. A success records the token's last used time and address.
    """
    return await authenticate_token(
        db, request.headers.get("authorization"), client_address(request), policy
    )
