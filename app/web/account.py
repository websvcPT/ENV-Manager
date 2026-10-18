"""The account screens of the signed-in user: password, two-factor authentication, sessions.

Open to every group; each user only ever reaches their own account and sessions. A pending
two-factor secret travels in an HttpOnly cookie as ciphertext and never reaches the database
or a URL; the QR code is drawn by the server at an address that is never cached.
"""

import json
import math
import secrets
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import CryptoService
from app.core.sessions import TOTP_SETUP_COOKIE_NAME, TOTP_SETUP_COOKIE_PATH
from app.core.templating import render
from app.models import User
from app.services import totp
from app.services.account import (
    AccountError,
    AccountLockedError,
    change_password,
    disable_totp,
    enable_totp,
)
from app.services.account_sessions import (
    CurrentSessionError,
    SessionNotFoundError,
    find_other_session,
    list_sessions,
    revoke_all_other_sessions,
    revoke_other_session,
    to_row,
)
from app.services.auth_policy import AuthPolicy
from app.services.totp_setup import SETUP_LIFETIME, PendingSetup, begin_setup, read_setup
from app.web.dependencies import AuthContext, get_auth_policy, get_crypto, get_db, require_auth

router = APIRouter()

CHANGE_PANEL_FRAGMENT = "fragments/account_password.html"
TOTP_FRAGMENT = "fragments/account_totp.html"
TOTP_REMOVE_FRAGMENT = "fragments/account_totp_remove.html"
SESSIONS_FRAGMENT = "fragments/account_sessions.html"
SESSION_REVOKE_FRAGMENT = "fragments/account_session_revoke.html"
SECRET_GROUP = 4

Signed = Annotated[AuthContext, Depends(require_auth)]
Database = Annotated[AsyncSession, Depends(get_db)]
Policy = Annotated[AuthPolicy, Depends(get_auth_policy)]
Crypto = Annotated[CryptoService, Depends(get_crypto)]


def _client_ip(request: Request) -> str | None:
    """The client address as resolved by the proxy header middleware."""
    return request.client.host if request.client else None


def _events(*names: str) -> str:
    """An ``HX-Trigger`` value firing the named events."""
    return json.dumps(dict.fromkeys(names, True))


def _wait_text(wait: timedelta) -> str:
    """A lockout time left in whole minutes, rounded up, as a phrase (15 minutes)."""
    minutes = max(1, math.ceil(wait.total_seconds() / 60))
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


def _locked_response(
    request: Request, template: str, context: dict[str, Any], error: AccountLockedError
) -> Response:
    """A 429 answer carrying the time left, for the screen that was refused."""
    response = render(request, template, context, status_code=429)
    response.headers["Retry-After"] = str(max(1, int(error.retry_after.total_seconds())))
    return response


def _password_context(
    policy: AuthPolicy,
    *,
    errors: dict[str, str] | None = None,
    notice: str | None = None,
    ended: int = 0,
    wait: str = "",
) -> dict[str, Any]:
    """What the password panel needs; every field has an entry in the errors."""
    names = ("current_password", "new_password", "confirm_password")
    return {
        "min_length": policy.min_password_length,
        "errors": {name: (errors or {}).get(name) for name in names},
        "notice": notice,
        "ended": ended,
        "wait": wait,
    }


def _setup_view(setup: PendingSetup) -> dict[str, str]:
    """The pending secret in groups of four for reading aloud, and the QR image's setup id."""
    secret = setup.secret
    groups = [secret[start : start + SECRET_GROUP] for start in range(0, len(secret), SECRET_GROUP)]
    return {"secret_text": " ".join(groups), "setup_id": setup.setup_id}


def _totp_context(
    user: User,
    *,
    setup: PendingSetup | None = None,
    code_error: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """What the two-factor panel needs: the setup when one is running, else the user's state."""
    idle_state = "on" if user.totp_enabled else "off"
    return {
        "state": "setup" if setup is not None else idle_state,
        "totp_notice": notice,
        "setup": _setup_view(setup) if setup is not None else None,
        "code_error": code_error,
    }


async def _sessions_context(
    db: AsyncSession, context: AuthContext, policy: AuthPolicy
) -> dict[str, Any]:
    """What the sessions panel needs: the live sessions and how many are not this one."""
    rows = await list_sessions(db, context.user, context.session.id, policy)
    return {"sessions": rows, "others": sum(1 for row in rows if not row.current)}


def _remove_context(
    state: str, *, errors: dict[str, str] | None = None, wait: str = ""
) -> dict[str, Any]:
    """What the remove two-factor dialog needs; every field has an entry in the errors."""
    return {
        "state": state,
        "errors": {name: (errors or {}).get(name) for name in ("current_password", "code")},
        "wait": wait,
    }


def _set_setup_cookie(response: Response, sealed: str) -> None:
    """Hand the sealed pending secret to the browser, readable only by the setup routes."""
    response.set_cookie(
        TOTP_SETUP_COOKIE_NAME,
        sealed,
        max_age=int(SETUP_LIFETIME.total_seconds()),
        httponly=True,
        secure=get_settings().base_url.startswith("https://"),
        samesite="strict",
        path=TOTP_SETUP_COOKIE_PATH,
    )


def _clear_setup_cookie(response: Response) -> Response:
    """Drop the pending secret once it was used, cancelled or refused."""
    response.delete_cookie(TOTP_SETUP_COOKIE_NAME, path=TOTP_SETUP_COOKIE_PATH)
    return response


def _pending_setup(
    request: Request, context: AuthContext, crypto: CryptoService
) -> PendingSetup | None:
    """The unexpired setup this session started, from its cookie."""
    return read_setup(
        context.user, context.session, request.cookies.get(TOTP_SETUP_COOKIE_NAME), crypto
    )


@router.get("/account", include_in_schema=False)
async def account_page(request: Request, db: Database, context: Signed, policy: Policy) -> Response:
    """The account page: password, two-factor authentication, sessions and theme."""
    page = {
        **_password_context(policy),
        **_totp_context(context.user),
        **await _sessions_context(db, context, policy),
    }
    return render(request, "pages/account.html", page)


@router.post("/account/password", include_in_schema=False)
async def password_route(
    request: Request,
    db: Database,
    context: Signed,
    policy: Policy,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
) -> Response:
    """Change the password and sign the user's other sessions out.

    The panel is answered again: with the reasons next to the fields when refused, with a
    lockout message while the name or address is locked, and with a confirmation (and an event
    that refreshes the sessions panel) on success. Fields are never echoed back.
    """
    try:
        ended = await change_password(
            db,
            policy,
            context.user,
            context.session,
            current_password=current_password,
            new_password=new_password,
            confirm_password=confirm_password,
            ip_address=_client_ip(request),
        )
    except AccountLockedError as error:
        page = _password_context(policy, notice="locked", wait=_wait_text(error.retry_after))
        return _locked_response(request, CHANGE_PANEL_FRAGMENT, page, error)
    except AccountError as error:
        page = _password_context(policy, errors={error.field or "current_password": str(error)})
        return render(request, CHANGE_PANEL_FRAGMENT, page, status_code=422)
    response = render(
        request, CHANGE_PANEL_FRAGMENT, _password_context(policy, notice="changed", ended=ended)
    )
    response.headers["HX-Trigger"] = _events("sessions-changed")
    return response


@router.get("/account/totp", include_in_schema=False)
async def totp_panel(request: Request, context: Signed) -> Response:
    """The two-factor panel as it stands. Leaving a setup, by Cancel, drops its pending secret."""
    response = render(request, TOTP_FRAGMENT, _totp_context(context.user))
    return _clear_setup_cookie(response)


@router.post("/account/totp/setup", include_in_schema=False)
async def totp_setup(request: Request, context: Signed, crypto: Crypto) -> Response:
    """Start a setup: a new secret, shown as a QR code and as text, held only in a cookie."""
    if context.user.totp_enabled:
        return render(request, TOTP_FRAGMENT, _totp_context(context.user), status_code=409)
    setup, sealed = begin_setup(context.user, context.session, crypto)
    response = render(request, TOTP_FRAGMENT, _totp_context(context.user, setup=setup))
    _set_setup_cookie(response, sealed)
    return response


@router.get("/account/totp/qr.svg", include_in_schema=False)
async def totp_qr(request: Request, context: Signed, crypto: Crypto, v: str = "") -> Response:
    """The QR code of the pending setup as an SVG, never cached.

    The address carries only the id of the setup, so a stale image of an earlier setup is never
    reused; the secret itself stays in the cookie. Without a pending setup there is nothing to
    draw.
    """
    setup = _pending_setup(request, context, crypto)
    if (
        setup is None
        or context.user.totp_enabled
        or not secrets.compare_digest(v.encode(), setup.setup_id.encode())
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no setup in progress")
    svg = totp.qr_svg(totp.provisioning_uri(context.user, setup.secret))
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.post("/account/totp/enable", include_in_schema=False)
async def totp_enable(
    request: Request,
    db: Database,
    context: Signed,
    crypto: Crypto,
    code: Annotated[str, Form()] = "",
) -> Response:
    """Turn two-factor authentication on once a valid code proves the scanned secret.

    A wrong code answers the setup again with the same secret; a setup that ran out answers the
    idle panel with a note. Success drops the cookie and shows the enabled state.
    """
    setup = _pending_setup(request, context, crypto)
    if context.user.totp_enabled or setup is None:
        page = _totp_context(context.user, notice=None if context.user.totp_enabled else "expired")
        return _clear_setup_cookie(render(request, TOTP_FRAGMENT, page, status_code=409))
    try:
        await enable_totp(
            db,
            context.user,
            crypto,
            secret=setup.secret,
            code=code,
            ip_address=_client_ip(request),
        )
    except AccountError as error:
        page = _totp_context(context.user, setup=setup, code_error=str(error))
        return render(request, TOTP_FRAGMENT, page, status_code=422)
    return _clear_setup_cookie(render(request, TOTP_FRAGMENT, _totp_context(context.user)))


@router.get("/account/totp/remove", include_in_schema=False)
async def totp_remove_dialog(request: Request, context: Signed) -> Response:
    """The confirm dialog for removing two-factor authentication."""
    state = "ask" if context.user.totp_enabled else "off"
    return render(request, TOTP_REMOVE_FRAGMENT, _remove_context(state))


@router.post("/account/totp/remove", include_in_schema=False)
async def totp_remove(
    request: Request,
    db: Database,
    context: Signed,
    policy: Policy,
    crypto: Crypto,
    current_password: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
) -> Response:
    """Remove two-factor authentication after the current password and a code.

    A refusal swaps the dialog with the reason next to the field; wrong passwords and codes
    count toward the lockout. Success closes the dialog and refreshes the two-factor panel.
    """
    try:
        await disable_totp(
            db,
            policy,
            context.user,
            crypto,
            password=current_password,
            code=code,
            ip_address=_client_ip(request),
        )
    except AccountLockedError as error:
        page = _remove_context("locked", wait=_wait_text(error.retry_after))
        return _locked_response(request, TOTP_REMOVE_FRAGMENT, page, error)
    except AccountError as error:
        if error.field is None:
            page = _remove_context("off")
            return render(request, TOTP_REMOVE_FRAGMENT, page, status_code=409)
        page = _remove_context("ask", errors={error.field: str(error)})
        return render(request, TOTP_REMOVE_FRAGMENT, page, status_code=422)
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Trigger": _events("totp-changed", "close-dialogs")},
    )


@router.get("/account/sessions", include_in_schema=False)
async def sessions_panel(
    request: Request, db: Database, context: Signed, policy: Policy
) -> Response:
    """The active sessions panel, refreshed after a session ends."""
    return render(request, SESSIONS_FRAGMENT, await _sessions_context(db, context, policy))


@router.get("/account/sessions/revoke-others", include_in_schema=False)
async def revoke_others_dialog(
    request: Request, db: Database, context: Signed, policy: Policy
) -> Response:
    """The confirm dialog for signing out every other session."""
    page = await _sessions_context(db, context, policy)
    return render(request, SESSION_REVOKE_FRAGMENT, {"row": None, "count": page["others"]})


@router.post("/account/sessions/revoke-others", include_in_schema=False)
async def revoke_others(request: Request, db: Database, context: Signed) -> Response:
    """End every session of the user but this one."""
    await revoke_all_other_sessions(
        db, context.user, context.session.id, ip_address=_client_ip(request)
    )
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Trigger": _events("sessions-changed", "close-dialogs")},
    )


@router.get("/account/sessions/{session_id:int}/revoke", include_in_schema=False)
async def revoke_dialog(
    request: Request, session_id: int, db: Database, context: Signed
) -> Response:
    """The confirm dialog for ending one other session of the user."""
    try:
        session = await find_other_session(db, context.user, context.session.id, session_id)
    except (SessionNotFoundError, CurrentSessionError) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found") from error
    row = to_row(session, context.session.id)
    return render(request, SESSION_REVOKE_FRAGMENT, {"row": row, "count": 1})


@router.post("/account/sessions/{session_id:int}/revoke", include_in_schema=False)
async def revoke_session_route(
    request: Request, session_id: int, db: Database, context: Signed
) -> Response:
    """End one other session of the user. The current one and other people's are refused."""
    try:
        await revoke_other_session(
            db,
            context.user,
            context.session.id,
            session_id,
            ip_address=_client_ip(request),
        )
    except (SessionNotFoundError, CurrentSessionError) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found") from error
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Trigger": _events("sessions-changed", "close-dialogs")},
    )
