"""Security headers added to every response."""

from collections.abc import Collection

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)

# The interactive API docs load their script, styles, fonts and favicon from public hosts and
# start themselves with an inline script, so they get this policy and nothing else does.
DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "font-src 'self' https://fonts.gstatic.com; "
    "worker-src 'self' blob:"
)
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class SecurityHeadersMiddleware:
    """Set the security headers on every HTTP response, including errors and rejections."""

    def __init__(self, app: ASGIApp, docs_paths: Collection[str] = ()) -> None:
        """Wrap the downstream application; ``docs_paths`` get the relaxed docs policy."""
        self.app = app
        self.docs_paths = frozenset(docs_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add the headers when the response starts."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers_to_set = dict(SECURITY_HEADERS)
        if scope["path"] in self.docs_paths:
            headers_to_set["Content-Security-Policy"] = DOCS_CONTENT_SECURITY_POLICY

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in headers_to_set.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
