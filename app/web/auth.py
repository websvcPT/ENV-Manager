"""Login and logout routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import CryptoService
from app.core.sessions import SESSION_COOKIE_NAME
from app.models import AuditActorType, AuditResult
from app.services.audit import record_audit
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginStatus, authenticate
from app.services.sessions import revoke_session
from app.web.dependencies import (
    AuthContext,
    get_auth_policy,
    get_crypto,
    get_db,
    require_auth,
)

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


@router.post("/login")
async def login(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    policy: Annotated[AuthPolicy, Depends(get_auth_policy)],
    identifier: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    totp_code: Annotated[str, Form()] = "",
) -> Response:
    """Sign in with a name or email and password, plus a code when a second factor is enabled.

    Public by design: this route is on the default-deny allow list.
    """
    result = await authenticate(
        db,
        crypto,
        policy,
        identifier=identifier,
        password=password,
        totp_code=totp_code,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if result.status is LoginStatus.SUCCESS and result.token:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            result.token,
            max_age=int(policy.absolute_timeout.total_seconds()),
            httponly=True,
            secure=get_settings().base_url.startswith("https://"),
            samesite="lax",
            path="/",
        )
        return response
    if result.status is LoginStatus.LOCKED and result.retry_after is not None:
        seconds = max(1, int(result.retry_after.total_seconds()))
        return JSONResponse(
            {"status": result.status.value, "retry_after": seconds},
            status_code=429,
            headers={"Retry-After": str(seconds)},
        )
    return JSONResponse({"status": result.status.value}, status_code=401)


@router.post("/logout")
async def logout(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    """End the current session and clear the cookie."""
    await record_audit(
        db,
        action="logout",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=context.user,
        ip_address=_client_ip(request),
    )
    await revoke_session(db, context.session)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
