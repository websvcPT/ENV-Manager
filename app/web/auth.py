"""Login, single sign-on, second factor step, session expiry page and logout routes."""

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.client_address import client_address
from app.core.crypto import CryptoService
from app.core.sessions import (
    PENDING_LOGIN_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SSO_BINDING_COOKIE_NAME,
    SSO_BINDING_COOKIE_PATH,
)
from app.core.templating import render
from app.models import AuditResult
from app.services.app_settings import StoredSettings
from app.services.audit import record_actor_audit
from app.services.auth_policy import AuthPolicy
from app.services.login import LoginStatus, authenticate, complete_totp_login
from app.services.sessions import revoke_session
from app.services.sso import complete, start
from app.services.sso_logins import SSO_LOGIN_LIFETIME
from app.services.sso_provider import SsoProvider
from app.services.sso_refusal import (
    ISSUER_MISMATCH,
    MESSAGES,
    NOT_CONFIGURED,
    PROVIDER_INVALID,
    PROVIDER_UNAVAILABLE,
    SsoLocked,
    SsoRefusal,
)
from app.web.cookies import set_private_cookie
from app.web.dependencies import (
    AuthContext,
    audit_actor,
    get_auth_policy,
    get_crypto,
    get_db,
    get_notifier,
    get_sso_provider,
    get_stored_settings,
    require_auth,
    resolve_optional_auth,
)
from app.web.errors import is_htmx_request
from app.web.htmx import hx_redirect

router = APIRouter()

LOGIN_PAGE = "pages/login.html"
LOGIN_CARD = "fragments/login_card.html"
SSO_UNAVAILABLE_REASONS = frozenset(
    {NOT_CONFIGURED, PROVIDER_UNAVAILABLE, PROVIDER_INVALID, ISSUER_MISMATCH}
)

Database = Annotated[AsyncSession, Depends(get_db)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]
Policy = Annotated[AuthPolicy, Depends(get_auth_policy)]
Stored = Annotated[StoredSettings, Depends(get_stored_settings)]
Provider = Annotated[SsoProvider, Depends(get_sso_provider)]


def _format_remaining(seconds: int) -> str:
    """Minutes and seconds left of a lockout, as shown on the card (14:32)."""
    minutes, rest = divmod(max(1, seconds), 60)
    return f"{minutes}:{rest:02d}"


def _sso_context(stored: StoredSettings | None) -> dict[str, bool]:
    """What the card needs of the single sign-on settings: whether it is on, and whether an
    account is created at the first sign in."""
    on = stored is not None and stored.sso_on
    return {"sso": on, "sso_creates": on and stored is not None and stored.sso.create_users}


def _card(
    request: Request,
    context: dict[str, Any],
    status_code: int,
    headers: dict[str, str] | None = None,
    *,
    stored: StoredSettings | None = None,
) -> Response:
    """The sign-in card for htmx requests, the whole page for plain form posts.

    ``stored`` (the settings, when single sign-on may be on) adds the single sign-on link and
    puts the password form behind the superadmin disclosure.
    """
    template = LOGIN_CARD if is_htmx_request(request) else LOGIN_PAGE
    page = {**context, **_sso_context(stored)}
    response = render(request, template, page, status_code=status_code)
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


def _signed_in(request: Request, token: str, policy: AuthPolicy) -> Response:
    """Start the session: a redirect for plain posts, `HX-Redirect` for htmx."""
    if is_htmx_request(request):
        response: Response = hx_redirect("/")
    else:
        response = RedirectResponse("/", status_code=303)
    set_private_cookie(
        response,
        SESSION_COOKIE_NAME,
        token,
        max_age=int(policy.absolute_timeout.total_seconds()),
        path="/",
        samesite="lax",
    )
    return response


def _locked(request: Request, retry_after: timedelta | None, *, stored: StoredSettings) -> Response:
    """The card for a locked email or address, with the time left."""
    seconds = max(1, int(retry_after.total_seconds())) if retry_after else 1
    context = {"state": "locked", "remaining": _format_remaining(seconds)}
    return _card(request, context, 429, {"Retry-After": str(seconds)}, stored=stored)


def _set_pending_cookie(response: Response, token: str, policy: AuthPolicy) -> None:
    """Hand the single-use pending login token to the browser, scoped to the code step."""
    set_private_cookie(
        response,
        PENDING_LOGIN_COOKIE_NAME,
        token,
        max_age=int(policy.pending_login_timeout.total_seconds()),
        path="/login",
        samesite="strict",
    )


def _code_card(
    request: Request,
    policy: AuthPolicy,
    token: str,
    stored: StoredSettings,
    status_code: int = 200,
    code_error: str | None = None,
) -> Response:
    """The second factor card, with the fresh pending login token in its cookie."""
    context: dict[str, Any] = {"state": "totp"}
    if code_error:
        context["code_error"] = code_error
    response = _card(request, context, status_code, stored=stored)
    _set_pending_cookie(response, token, policy)
    return response


def _clear_pending(response: Response) -> Response:
    """Drop the pending login cookie once its token is used up or refused."""
    response.delete_cookie(PENDING_LOGIN_COOKIE_NAME, path="/login")
    return response


@router.post("/login", include_in_schema=False)
async def login(
    request: Request,
    db: Database,
    crypto: Crypto,
    policy: Policy,
    stored: Stored,
    identifier: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Sign in with an email and password; a second factor user is asked for a code.

    While single sign-on is on, only superadmin accounts may sign in this way; any other
    account is answered like a wrong password.
    Public by design: this route is on the default-deny allow list.
    """
    sso_on = stored.sso_on
    result = await authenticate(
        db,
        crypto,
        policy,
        identifier=identifier,
        password=password,
        ip_address=client_address(request),
        user_agent=request.headers.get("user-agent"),
        notifier=get_notifier(request),
        superadmins_only=sso_on,
    )
    if result.status is LoginStatus.SUCCESS and result.token:
        return _clear_pending(_signed_in(request, result.token, policy))
    if result.status is LoginStatus.LOCKED:
        return _locked(request, result.retry_after, stored=stored)
    if result.status is LoginStatus.TOTP_REQUIRED and result.pending_token:
        return _code_card(request, policy, result.pending_token, stored)
    return _card(request, {"state": "invalid", "identifier": identifier}, 401, stored=stored)


@router.get("/login", include_in_schema=False)
async def login_page(
    request: Request, stored: Stored, expired: bool = False, sso: str = ""
) -> Response:
    """The sign-in page; a signed-in user goes on to the projects page.

    ``sso=off`` is where the single sign-on link sends a visitor when it is not set up.
    Public by design: this route is on the default-deny allow list.
    """
    if await resolve_optional_auth(request) is not None:
        return RedirectResponse("/projects", status_code=303)
    context: dict[str, Any] = {"state": "expired" if expired else None, **_sso_context(stored)}
    if sso == "off" and not context["sso"]:
        context |= {"state": "sso_refused", "sso_message": MESSAGES[NOT_CONFIGURED]}
    return render(request, LOGIN_PAGE, context)


def _refusal_card(
    request: Request, message: str, status_code: int, stored: StoredSettings
) -> Response:
    """The sign-in card with the fixed sentence of a refused single sign-on attempt.

    The binding cookie of the attempt is used up either way.
    """
    context = {"state": "sso_refused", "sso_message": message}
    response = _card(request, context, status_code, stored=stored)
    response.delete_cookie(SSO_BINDING_COOKIE_NAME, path=SSO_BINDING_COOKIE_PATH)
    return response


def _sso_failed(
    request: Request, error: SsoRefusal | SsoLocked, stored: StoredSettings
) -> Response:
    """The card for a refused or locked single sign-on attempt.

    It carries only fixed sentences, never anything the provider or the visitor sent.
    """
    if isinstance(error, SsoLocked):
        locked = _locked(request, error.retry_after, stored=stored)
        locked.delete_cookie(SSO_BINDING_COOKIE_NAME, path=SSO_BINDING_COOKIE_PATH)
        return locked
    status_code = 503 if error.reason in SSO_UNAVAILABLE_REASONS else 401
    return _refusal_card(request, error.message, status_code, stored)


@router.get("/login/sso", include_in_schema=False)
async def login_sso(
    request: Request,
    db: Database,
    crypto: Crypto,
    policy: Policy,
    stored: Stored,
    provider: Provider,
) -> Response:
    """Send the browser to the identity provider and keep the attempt's binding in a cookie.

    A plain link, not a form: the content security policy allows form posts to this site only
    and the provider is another origin. The cookie is `SameSite=Lax`, so it comes back with the
    provider's redirect to the callback. Public by design: this route is on the default-deny
    allow list.
    """
    if await resolve_optional_auth(request) is not None:
        return RedirectResponse("/projects", status_code=303)
    if not stored.sso_on:
        return RedirectResponse("/login?sso=off", status_code=303)
    try:
        started = await start(
            db,
            crypto,
            provider,
            policy,
            base_url=get_settings().base_url,
            ip_address=client_address(request),
            notifier=get_notifier(request),
        )
    except (SsoRefusal, SsoLocked) as error:
        return _sso_failed(request, error, stored)
    response = RedirectResponse(started.url, status_code=303, headers={"Cache-Control": "no-store"})
    set_private_cookie(
        response,
        SSO_BINDING_COOKIE_NAME,
        started.binding_token,
        max_age=int(SSO_LOGIN_LIFETIME.total_seconds()),
        path=SSO_BINDING_COOKIE_PATH,
        samesite="lax",
    )
    return response


@router.get("/login/sso/callback", include_in_schema=False)
async def login_sso_callback(
    request: Request,
    db: Database,
    crypto: Crypto,
    policy: Policy,
    stored: Stored,
    provider: Provider,
    state: str = "",
    code: str | None = None,
    error: str | None = None,
) -> Response:
    """Finish a single sign-on attempt the provider sent the browser back from.

    Success starts the session exactly like a password sign in. A refusal is answered with the
    sign-in card and the fixed sentence of its reason: what the provider put in the address
    (its error and its description) is never shown or echoed. The binding cookie is used up
    either way. Public by design: this route is on the default-deny allow list.
    """
    if not stored.sso_on:
        return _refusal_card(request, MESSAGES[NOT_CONFIGURED], 503, stored)
    try:
        result = await complete(
            db,
            crypto,
            provider,
            policy,
            state=state,
            binding_token=request.cookies.get(SSO_BINDING_COOKIE_NAME, ""),
            code=code,
            provider_error=error,
            base_url=get_settings().base_url,
            ip_address=client_address(request),
            user_agent=request.headers.get("user-agent"),
            notifier=get_notifier(request),
        )
    except (SsoRefusal, SsoLocked) as refusal:
        return _sso_failed(request, refusal, stored)
    if result.token is None:
        return _refusal_card(request, MESSAGES[NOT_CONFIGURED], 503, stored)
    response = _signed_in(request, result.token, policy)
    response.delete_cookie(SSO_BINDING_COOKIE_NAME, path=SSO_BINDING_COOKIE_PATH)
    return response


@router.post("/login/totp", include_in_schema=False)
async def login_totp(
    request: Request,
    db: Database,
    crypto: Crypto,
    policy: Policy,
    stored: Stored,
    code: Annotated[str, Form()] = "",
) -> Response:
    """Complete a sign in with the authenticator code.

    The pending login from the password step arrives only as an HttpOnly cookie, so the name
    and password are never sent again. The token is single use: a wrong or empty code is
    answered with a fresh one. Failures share the lockout counters with wrong passwords.
    Public by design: this route is on the default-deny allow list.
    """
    sso_on = stored.sso_on
    result = await complete_totp_login(
        db,
        crypto,
        policy,
        pending_token=request.cookies.get(PENDING_LOGIN_COOKIE_NAME, ""),
        code=code,
        ip_address=client_address(request),
        user_agent=request.headers.get("user-agent"),
        notifier=get_notifier(request),
        superadmins_only=sso_on,
    )
    if result.status is LoginStatus.SUCCESS and result.token:
        return _clear_pending(_signed_in(request, result.token, policy))
    if result.status is LoginStatus.LOCKED:
        return _clear_pending(_locked(request, result.retry_after, stored=stored))
    if result.pending_token and result.status is LoginStatus.INVALID:
        return _code_card(
            request, policy, result.pending_token, stored, 401, "The code is incorrect or expired."
        )
    if result.pending_token:
        return _code_card(
            request, policy, result.pending_token, stored, 422, "Enter the 6-digit code."
        )
    return _clear_pending(_card(request, {"state": "pending_invalid"}, 401, stored=stored))


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
