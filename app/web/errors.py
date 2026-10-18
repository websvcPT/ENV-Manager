"""Error responses: HTML pages for browsers, JSON for the API, no-swap answers for htmx."""

import secrets

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.errors import http_error_response, internal_error_response, validation_error_response
from app.core.cors import is_api_path
from app.core.log import get_logger
from app.core.security_headers import SECURITY_HEADERS
from app.core.sessions import SESSION_COOKIE_NAME
from app.core.templating import render
from app.web.dependencies import AuthContext, resolve_optional_auth

log = get_logger("errors")

PAGE_STATUSES = frozenset({403, 404, 500})
BODYLESS_STATUSES = frozenset({204, 304})
ERROR_COPY: dict[int, tuple[str, str]] = {
    403: (
        "No access",
        "Your group or grants do not allow this page. Viewers cannot open the audit log or API "
        "tokens.",
    ),
    404: ("Not found", "The page does not exist, or it is outside your granted scope."),
    500: (
        "Something went wrong",
        "The server hit an unexpected error. Nothing was changed by this request. Share the "
        "request id with a superadmin if it keeps happening.",
    ),
}
APP_LAYOUT = "layouts/app.html"
BARE_LAYOUT = "layouts/bare.html"


def is_api_request(request: Request) -> bool:
    """Whether the request targets the bearer-token JSON API."""
    return is_api_path(request.url.path)


def is_htmx_request(request: Request) -> bool:
    """Whether htmx issued the request."""
    return request.headers.get("hx-request", "").lower() == "true"


def wants_html(request: Request) -> bool:
    """Whether a browser page (not the API) is the right answer to this request."""
    return not is_api_request(request) and "text/html" in request.headers.get("accept", "")


def new_request_id() -> str:
    """A short identifier shown on the 500 page and written to the log line for that error."""
    return secrets.token_hex(4)


def error_page(
    request: Request,
    status_code: int,
    auth: AuthContext | None,
    request_id: str | None = None,
) -> Response:
    """The error page inside the application shell for a signed-in user, bare otherwise."""
    heading, message = ERROR_COPY[status_code]
    if auth is not None:
        request.state.auth = auth
    context = {
        "layout": APP_LAYOUT if auth is not None else BARE_LAYOUT,
        "show_brand": auth is None,
        "code": status_code,
        "heading": heading,
        "message": message,
        "request_id": request_id,
    }
    return render(request, "pages/error.html", context, status_code=status_code)


def unauthenticated_response(request: Request, headers: dict[str, str]) -> Response:
    """Send an unauthenticated browser or htmx request to the sign-in screens.

    A request that still carries a session cookie had a session that ended, so the person sees
    the session expired state and the stale cookie is dropped. A page request goes to the sign-in
    page; an htmx request from an open screen gets `HX-Redirect` to the standalone expiry page,
    or to sign-in when it never had a session.
    """
    expired = SESSION_COOKIE_NAME in request.cookies
    response: Response
    if is_htmx_request(request):
        target = "/session-expired" if expired else "/login"
        response = Response(status_code=401, headers={**headers, "HX-Redirect": target})
    else:
        target = "/login?expired=1" if expired else "/login"
        response = RedirectResponse(target, status_code=303, headers=headers)
    if expired:
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


async def http_error_handler(request: Request, exc: Exception) -> Response:
    """Render 403 and 404 for browsers; every other status keeps the JSON shape.

    An htmx request gets an empty answer that is not swapped; the page script shows a toast. The
    API prefix always answers in the API's error shape.
    """
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    status_code = exc.status_code
    headers = dict(exc.headers) if exc.headers else {}
    if is_api_request(request):
        return http_error_response(status_code, headers)
    browser_request = is_htmx_request(request) or wants_html(request)
    if status_code == 401 and not is_api_request(request) and browser_request:
        return unauthenticated_response(request, headers)
    if status_code in PAGE_STATUSES and not is_api_request(request):
        if is_htmx_request(request):
            return Response(status_code=status_code, headers={**headers, "HX-Reswap": "none"})
        if wants_html(request):
            auth = getattr(request.state, "auth", None) or await resolve_optional_auth(request)
            return error_page(request, status_code, auth)
    if status_code in BODYLESS_STATUSES:
        return Response(status_code=status_code, headers=headers)
    return JSONResponse({"detail": exc.detail}, status_code=status_code, headers=headers)


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    """Answer an unexpected error without exposing its text, tagged with a request id.

    This handler runs outside the middleware stack, so the security headers are set here. It
    never queries the database: the shell shows the user only when authentication already ran.
    """
    request_id = new_request_id()
    log.error(
        "unhandled %s request_id=%s %s %s",
        type(exc).__name__,
        request_id,
        request.method,
        request.url.path,
    )
    response: Response
    if is_api_request(request):
        response = internal_error_response(request_id)
    elif is_htmx_request(request):
        response = Response(status_code=500, headers={"HX-Reswap": "none"})
    elif wants_html(request):
        auth = getattr(request.state, "auth", None)
        response = error_page(request, 500, auth, request_id)
    else:
        response = JSONResponse(
            {"detail": "Internal Server Error", "request_id": request_id}, status_code=500
        )
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    response.headers["Cache-Control"] = "no-store"
    return response


async def validation_error_handler(request: Request, exc: Exception) -> Response:
    """Answer a request the framework could not read, in the API shape under ``/api``."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    if is_api_request(request):
        return validation_error_response(exc)
    return await request_validation_exception_handler(request, exc)
