"""Login, second factor step, session expiry page and logout routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import CryptoService
from app.core.sessions import SESSION_COOKIE_NAME
from app.core.templating import render
from app.models import AuditActorType, AuditResult
from app.services.audit import record_audit
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginResult, LoginStatus, authenticate
from app.services.sessions import revoke_session
from app.web.dependencies import (
    AuthContext,
    get_auth_policy,
    get_crypto,
    get_db,
    require_auth,
    resolve_optional_auth,
)
from app.web.errors import is_htmx_request

router = APIRouter()

LOGIN_PAGE = "pages/login.html"
LOGIN_CARD = "fragments/login_card.html"


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _format_remaining(seconds: int) -> str:
    """Minutes and seconds left of a lockout, as shown on the card (14:32)."""
    minutes, rest = divmod(max(1, seconds), 60)
    return f"{minutes}:{rest:02d}"


def _card(
    request: Request,
    context: dict[str, Any],
    status_code: int,
    headers: dict[str, str] | None = None,
) -> Response:
    """The sign-in card for htmx requests, the whole page for plain form posts."""
    template = LOGIN_CARD if is_htmx_request(request) else LOGIN_PAGE
    response = render(request, template, context, status_code=status_code)
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


def _signed_in(request: Request, token: str, policy: AuthPolicy) -> Response:
    """Start the session: a redirect for plain posts, `HX-Redirect` for htmx."""
    if is_htmx_request(request):
        response: Response = Response(status_code=200, headers={"HX-Redirect": "/"})
    else:
        response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=int(policy.absolute_timeout.total_seconds()),
        httponly=True,
        secure=get_settings().base_url.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return response


def _locked(request: Request, result: LoginResult) -> Response:
    """The card for a locked name or address, with the time left."""
    seconds = max(1, int(result.retry_after.total_seconds())) if result.retry_after else 1
    context = {"state": "locked", "remaining": _format_remaining(seconds)}
    return _card(request, context, 429, {"Retry-After": str(seconds)})


async def _attempt(
    request: Request,
    db: AsyncSession,
    crypto: CryptoService,
    policy: AuthPolicy,
    *,
    identifier: str,
    password: str,
    totp_code: str,
    code_step: bool,
) -> Response:
    """Run one login attempt and answer with the card state that matches its outcome."""
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
        return _signed_in(request, result.token, policy)
    if result.status is LoginStatus.LOCKED:
        return _locked(request, result)
    if result.status is LoginStatus.TOTP_REQUIRED or code_step:
        context: dict[str, Any] = {
            "state": "totp",
            "identifier": identifier,
            "password": password,
        }
        if code_step and result.status is LoginStatus.INVALID:
            context["code_error"] = "The code is incorrect or expired."
            return _card(request, context, 401)
        if code_step:
            context["code_error"] = "Enter the 6-digit code."
            return _card(request, context, 422)
        return _card(request, context, 200)
    return _card(request, {"state": "invalid", "identifier": identifier}, 401)


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, expired: bool = False) -> Response:
    """The sign-in page; a signed-in user goes on to the projects page.

    Public by design: this route is on the default-deny allow list.
    """
    if await resolve_optional_auth(request) is not None:
        return RedirectResponse("/projects", status_code=303)
    return render(request, LOGIN_PAGE, {"state": "expired" if expired else None})


@router.get("/session-expired", include_in_schema=False)
async def session_expired_page(request: Request) -> Response:
    """The page an open screen is sent to when one of its requests finds the session ended.

    Public by design: this route is on the default-deny allow list.
    """
    return render(request, "pages/session_expired.html")


@router.post("/login", include_in_schema=False)
async def login(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    policy: Annotated[AuthPolicy, Depends(get_auth_policy)],
    identifier: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Sign in with a name or email and password; a second factor user is asked for a code.

    Public by design: this route is on the default-deny allow list.
    """
    return await _attempt(
        request,
        db,
        crypto,
        policy,
        identifier=identifier,
        password=password,
        totp_code="",
        code_step=False,
    )


@router.post("/login/totp", include_in_schema=False)
async def login_totp(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    policy: Annotated[AuthPolicy, Depends(get_auth_policy)],
    identifier: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
) -> Response:
    """Complete a sign in with the authenticator code.

    The form carries the name and password again, so the credentials and the code are verified
    together and failures count against the same lockout counters.
    Public by design: this route is on the default-deny allow list.
    """
    return await _attempt(
        request,
        db,
        crypto,
        policy,
        identifier=identifier,
        password=password,
        totp_code=code,
        code_step=True,
    )


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
