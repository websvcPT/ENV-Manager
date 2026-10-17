"""Double-submit CSRF protection for cookie sessions."""

import re
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette_csrf.middleware import CSRFMiddleware

from app.core.cors import API_PREFIX
from app.core.log import get_logger
from app.core.sessions import SESSION_COOKIE_NAME

log = get_logger("csrf")

CSRF_COOKIE_NAME = "csrftoken"
CSRF_HEADER_NAME = "x-csrftoken"
LOGIN_PATH = "/login"


class LoggingCSRFMiddleware(CSRFMiddleware):
    """The library middleware, with failed checks written to the log."""

    def _get_error_response(self, request: Request) -> Response:
        """Log the rejection (method and path only) and answer 403."""
        log.warning("csrf check failed: %s %s", request.method, request.url.path)
        return PlainTextResponse(content="CSRF token verification failed", status_code=403)


def csrf_options(*, secure_cookie: bool) -> dict[str, Any]:
    """Options for the CSRF middleware, besides the secret.

    Unsafe requests are checked when a session cookie is present, and always on the login path
    so the login form is covered too. The API prefix accepts bearer tokens and is exempt.
    """
    return {
        "sensitive_cookies": {SESSION_COOKIE_NAME},
        "required_urls": [re.compile(rf"^{re.escape(LOGIN_PATH)}/?$")],
        "exempt_urls": [re.compile(rf"^{re.escape(API_PREFIX)}(/|$)")],
        "cookie_name": CSRF_COOKIE_NAME,
        "header_name": CSRF_HEADER_NAME,
        "cookie_secure": secure_cookie,
        "cookie_samesite": "strict",
    }
