"""CORS limited to the API path prefix."""

from collections.abc import Sequence

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

API_PREFIX = "/api"
ALLOWED_HEADERS = ["Authorization", "Content-Type"]
ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def is_api_path(path: str) -> bool:
    """Return True for the API prefix itself and anything below it."""
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")


class ApiCorsMiddleware:
    """Apply CORS to API paths only; every other path stays same-origin with no CORS headers.

    Credentials are never allowed, so a cookie session cannot be used cross-origin.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Sequence[str]) -> None:
        """Wrap the application and build the CORS handler for the configured origins."""
        if "*" in allowed_origins:
            raise ValueError("wildcard CORS origins are not allowed")
        self.app = app
        self.cors = CORSMiddleware(
            app,
            allow_origins=list(allowed_origins),
            allow_methods=ALLOWED_METHODS,
            allow_headers=ALLOWED_HEADERS,
            allow_credentials=False,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Route API requests through CORS and everything else straight through."""
        if scope["type"] == "http" and is_api_path(scope["path"]):
            await self.cors(scope, receive, send)
        else:
            await self.app(scope, receive, send)
