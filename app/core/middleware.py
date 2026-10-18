"""Middleware assembly. Order, outermost first: proxy headers, security headers, allowed sources,
CORS, CSRF."""

from fastapi import FastAPI

from app.config import Settings
from app.core.allowed_sources import AllowedSourcesMiddleware
from app.core.client_address import ProxyHeadersMiddleware
from app.core.cors import ApiCorsMiddleware
from app.core.csrf import CSRFMiddleware
from app.core.security_headers import SecurityHeadersMiddleware


def install_middleware(application: FastAPI, settings: Settings) -> None:
    """Install the middleware stack.

    Session and authentication belong inside CSRF (they are dependencies of the routes). Starlette
    puts the most recently added middleware outermost, so the stack is added innermost first.
    The proxy header middleware is always installed: it resolves the client address once, from
    forwarded headers only when the peer is a configured trusted proxy, so everything inside it
    sees one address. The allowed sources check is installed only when sources are configured.
    """
    if settings.csrf_secret is None:
        raise RuntimeError("the CSRF secret is not configured")
    application.add_middleware(
        CSRFMiddleware,
        secret=settings.csrf_secret.get_secret_value(),
        cookie_secure=settings.base_url.startswith("https://"),
    )
    if settings.cors_origins:
        application.add_middleware(ApiCorsMiddleware, allowed_origins=settings.cors_origins)
    if settings.allowed_sources:
        application.add_middleware(
            AllowedSourcesMiddleware, allowed_sources=settings.allowed_sources
        )
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(ProxyHeadersMiddleware, trusted_proxies=settings.trusted_proxies)
