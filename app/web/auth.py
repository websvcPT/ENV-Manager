"""Login, second factor step, session expiry page and logout routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.sessions import PENDING_LOGIN_COOKIE_NAME, SESSION_COOKIE_NAME
from app.core.templating import render
from app.models import AuditResult
from app.services.audit import record_actor_audit
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginResult, LoginStatus, authenticate, complete_totp_login
from app.services.sessions import revoke_session
from app.web.dependencies import (
    AuthContext,
    audit_actor,
    get_auth_policy,
    get_crypto,
    get_db,
    get_notifier,
    require_auth,
    resolve_optional_auth,
)
from app.web.errors import is_htmx_request
from app.web.htmx import hx_redirect

router = APIRouter()

LOGIN_PAGE = "pages/login.html"
LOGIN_CARD = "fragments/login_card.html"


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
        response: Response = hx_redirect("/")
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


def _set_pending_cookie(response: Response, token: str, policy: AuthPolicy) -> None:
    """Hand the single-use pending login token to the browser, scoped to the code step."""
    response.set_cookie(
        PENDING_LOGIN_COOKIE_NAME,
        token,
        max_age=int(policy.pending_login_timeout.total_seconds()),
        httponly=True,
        secure=get_settings().base_url.startswith("https://"),
        samesite="strict",
        path="/login",
    )


def _code_card(
    request: Request,
    policy: AuthPolicy,
    token: str,
    status_code: int = 200,
    code_error: str | None = None,
) -> Response:
    """The second factor card, with the fresh pending login token in its cookie."""
    context: dict[str, Any] = {"state": "totp"}
    if code_error:
        context["code_error"] = code_error
    response = _card(request, context, status_code)
    _set_pending_cookie(response, token, policy)
    return response


def _clear_pending(response: Response) -> Response:
    """Drop the pending login cookie once its token is used up or refused."""
    response.delete_cookie(PENDING_LOGIN_COOKIE_NAME, path="/login")
    return response


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
    result = await authenticate(
        db,
        crypto,
        policy,
        identifier=identifier,
        password=password,
        ip_address=client_address(request),
        user_agent=request.headers.get("user-agent"),
        notifier=get_notifier(request),
    )
    if result.status is LoginStatus.SUCCESS and result.token:
        return _clear_pending(_signed_in(request, result.token, policy))
    if result.status is LoginStatus.LOCKED:
        return _locked(request, result)
    if result.status is LoginStatus.TOTP_REQUIRED and result.pending_token:
        return _code_card(request, policy, result.pending_token)
    return _card(request, {"state": "invalid", "identifier": identifier}, 401)


@router.get("/login", include_in_schema=False)
async def login_page(request: Request, expired: bool = False) -> Response:
    """The sign-in page; a signed-in user goes on to the projects page.

    Public by design: this route is on the default-deny allow list.
    """
    if await resolve_optional_auth(request) is not None:
        return RedirectResponse("/projects", status_code=303)
    return render(request, LOGIN_PAGE, {"state": "expired" if expired else None})


@router.post("/login/totp", include_in_schema=False)
async def login_totp(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    crypto: Annotated[CryptoService, Depends(get_crypto)],
    policy: Annotated[AuthPolicy, Depends(get_auth_policy)],
    code: Annotated[str, Form()] = "",
) -> Response:
    """Complete a sign in with the authenticator code.

    The pending login from the password step arrives only as an HttpOnly cookie, so the name
    and password are never sent again. The token is single use: a wrong or empty code is
    answered with a fresh one. Failures share the lockout counters with wrong passwords.
    Public by design: this route is on the default-deny allow list.
    """
    result = await complete_totp_login(
        db,
        crypto,
        policy,
        pending_token=request.cookies.get(PENDING_LOGIN_COOKIE_NAME, ""),
        code=code,
        ip_address=client_address(request),
        user_agent=request.headers.get("user-agent"),
        notifier=get_notifier(request),
    )
    if result.status is LoginStatus.SUCCESS and result.token:
        return _clear_pending(_signed_in(request, result.token, policy))
    if result.status is LoginStatus.LOCKED:
        return _clear_pending(_locked(request, result))
    if result.pending_token and result.status is LoginStatus.INVALID:
        return _code_card(
            request, policy, result.pending_token, 401, "The code is incorrect or expired."
        )
    if result.pending_token:
        return _code_card(request, policy, result.pending_token, 422, "Enter the 6-digit code.")
    return _clear_pending(_card(request, {"state": "pending_invalid"}, 401))


@router.post("/logout")
async def logout(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    """End the current session and clear the cookie."""
    await record_actor_audit(
        db, audit_actor(request, context), action="logout", result=AuditResult.SUCCESS
    )
    await revoke_session(db, context.session)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
