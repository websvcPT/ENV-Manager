"""Middleware assembly. Order, outermost first: proxy headers, security headers, CORS, CSRF."""

from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import Settings
from app.core.cors import ApiCorsMiddleware
from app.core.csrf import CSRFMiddleware
from app.core.security_headers import SecurityHeadersMiddleware


def install_middleware(application: FastAPI, settings: Settings) -> None:
    """Install the middleware stack.

    Session and authentication middleware belongs inside CSRF. Starlette puts the most recently
    added middleware outermost, so the stack is added innermost first. Forwarded headers are
    honoured only from the configured trusted proxies; with none configured they are ignored.
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
    application.add_middleware(SecurityHeadersMiddleware)
    if settings.trusted_proxies:
        application.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxies)
