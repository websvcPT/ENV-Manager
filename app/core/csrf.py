"""Double-submit CSRF protection for cookie sessions, as a pure ASGI middleware."""

import hashlib
import hmac
import re
import secrets

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.cors import API_PREFIX
from app.core.log import get_logger
from app.core.sessions import SESSION_COOKIE_NAME

log = get_logger("csrf")

CSRF_COOKIE_NAME = "csrftoken"
CSRF_HEADER_NAME = "x-csrftoken"
CSRF_FORM_FIELD = "csrf_token"
LOGIN_PATH = "/login"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
MAX_FORM_BODY_BYTES = 8 * 1024 * 1024
NONCE_BYTES = 32

_LOGIN_PATTERN = re.compile(rf"^{re.escape(LOGIN_PATH)}/?$")
_API_PATTERN = re.compile(rf"^{re.escape(API_PREFIX)}(/|$)")


class BodyTooLargeError(Exception):
    """The request body exceeds the size the middleware is willing to buffer."""


def sign_nonce(secret: str, nonce: str) -> str:
    """Keyed signature of a token nonce."""
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def generate_token(secret: str) -> str:
    """A fresh signed token of the form `nonce.signature`."""
    nonce = secrets.token_urlsafe(NONCE_BYTES)
    return f"{nonce}.{sign_nonce(secret, nonce)}"


def is_valid_token(secret: str, token: str) -> bool:
    """Whether a token carries a correct signature for its nonce."""
    nonce, separator, signature = token.partition(".")
    if not separator or not nonce:
        return False
    return hmac.compare_digest(signature.encode(), sign_nonce(secret, nonce).encode())


def tokens_match(expected: str, submitted: str) -> bool:
    """Constant-time comparison of the cookie token and the submitted token."""
    return hmac.compare_digest(expected.encode(), submitted.encode())


def replay_receive(body: bytes) -> Receive:
    """A receive callable that yields a buffered body once, then reports a disconnect."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def read_bounded_body(receive: Receive, limit: int) -> bytes:
    """Read the whole request body, failing as soon as it exceeds the limit."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise BodyTooLargeError
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


class CSRFMiddleware:
    """Signed double-submit token check for state-changing browser requests.

    The token lives in an HttpOnly cookie and is exposed to the application for the current
    request as `request.state.csrf_token`, so templates render it into forms and
    `hx-headers`. A request passes when the submitted token (the `X-CSRFToken` header, or the
    hidden `csrf_token` form field) equals the cookie token and carries a valid signature.
    Unsafe requests are checked when a session cookie is present and always on the login path.
    The API prefix accepts bearer tokens and ignores cookies, so it is passed through untouched:
    no check, and no token cookie is set on its answers. A form body is buffered, up to a fixed
    bound, only to read the field, then replayed to the application.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: str,
        cookie_secure: bool = False,
        max_body_bytes: int = MAX_FORM_BODY_BYTES,
    ) -> None:
        """Wrap the downstream application."""
        self.app = app
        self.secret = secret
        self.cookie_secure = cookie_secure
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Issue the token cookie, and verify the token on sensitive unsafe requests."""
        if scope["type"] != "http" or _API_PATTERN.match(scope["path"]):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
        has_valid_cookie = bool(cookie_token) and is_valid_token(self.secret, cookie_token)
        token = cookie_token if has_valid_cookie else generate_token(self.secret)
        scope.setdefault("state", {})["csrf_token"] = token

        if self._requires_check(request):
            try:
                submitted = await self._submitted_token(request, receive)
            except BodyTooLargeError:
                log.warning("csrf body too large: %s %s", request.method, request.url.path)
                await PlainTextResponse("Request body too large", status_code=413)(
                    scope, receive, send
                )
                return
            if not has_valid_cookie or not submitted or not tokens_match(token, submitted[0]):
                log.warning("csrf check failed: %s %s", request.method, request.url.path)
                await PlainTextResponse("CSRF token verification failed", status_code=403)(
                    scope, receive, send
                )
                return
            receive = submitted[1]

        await self.app(scope, receive, self._send_with_cookie(send, token, has_valid_cookie))

    def _requires_check(self, request: Request) -> bool:
        """Whether this request must carry a valid token."""
        path = request.url.path
        if request.method in SAFE_METHODS:
            return False
        return bool(_LOGIN_PATTERN.match(path)) or SESSION_COOKIE_NAME in request.cookies

    async def _submitted_token(
        self, request: Request, receive: Receive
    ) -> tuple[str, Receive] | None:
        """The token the client submitted and the receive callable the application must use.

        The header wins and leaves the body untouched. Otherwise a form body is buffered within
        the bound, parsed for the hidden field and replayed. Returns None when no token was
        submitted or the body cannot be parsed.
        """
        header_token = request.headers.get(CSRF_HEADER_NAME)
        if header_token:
            return header_token, receive
        content_type = request.headers.get("content-type", "").lower()
        if not content_type.startswith(FORM_CONTENT_TYPES):
            return None
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_body_bytes:
            raise BodyTooLargeError
        body = await read_bounded_body(receive, self.max_body_bytes)
        replay = replay_receive(body)
        parsing = Request(request.scope, replay_receive(body))
        try:
            async with parsing.form() as form:
                value = form.get(CSRF_FORM_FIELD)
        except Exception:
            return None
        if not isinstance(value, str) or not value:
            return None
        return value, replay

    def _send_with_cookie(self, send: Send, token: str, has_valid_cookie: bool) -> Send:
        """Wrap send so a response sets the token cookie when the request had none."""

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and not has_valid_cookie:
                headers = MutableHeaders(scope=message)
                headers.append("Set-Cookie", self._cookie_header(token))
            await send(message)

        return send_with_cookie

    def _cookie_header(self, token: str) -> str:
        """The Set-Cookie value for the token."""
        parts = [f"{CSRF_COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
        if self.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)
