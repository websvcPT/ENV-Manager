"""Middleware stack tests: order, security headers, CORS, CSRF and forwarded headers."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import SecretStr
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import Settings
from app.core.cors import ApiCorsMiddleware
from app.core.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, LoggingCSRFMiddleware
from app.core.middleware import install_middleware
from app.core.security_headers import CONTENT_SECURITY_POLICY, SecurityHeadersMiddleware
from app.core.sessions import SESSION_COOKIE_NAME

SECRET = "test-secret-0123456789abcdef0123456789"
ORIGIN = "https://client.example"


def build_app(**overrides: Any) -> FastAPI:
    """An application with the real middleware stack and a few probe routes."""
    settings = Settings(environment="testing", csrf_secret=SecretStr(SECRET), **overrides)
    application = FastAPI()
    install_middleware(application, settings)

    @application.get("/page")
    async def page() -> dict[str, str]:
        return {"ok": "yes"}

    @application.post("/change")
    async def change() -> dict[str, str]:
        return {"changed": "yes"}

    @application.post("/login")
    async def login() -> dict[str, str]:
        return {"login": "yes"}

    @application.post("/api/v1/thing")
    async def api_thing() -> dict[str, str]:
        return {"api": "yes"}

    @application.get("/api/v1/thing")
    async def api_read() -> dict[str, str]:
        return {"api": "yes"}

    @application.get("/client")
    async def client_info(request: Request) -> dict[str, str]:
        return {
            "host": request.client.host if request.client else "",
            "scheme": request.url.scheme,
        }

    return application


async def client_for(application: FastAPI) -> httpx.AsyncClient:
    """An HTTP client talking to the application in process."""
    transport = httpx.ASGITransport(app=application, client=("10.0.0.5", 4000))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Client for the default stack (no CORS origins, no trusted proxies)."""
    async with await client_for(build_app()) as http_client:
        yield http_client


def test_middleware_order_is_headers_cors_csrf() -> None:
    """The stack is proxy headers, security headers, CORS, then CSRF from the outside in."""
    application = build_app(cors_origins=[ORIGIN], trusted_proxies=["10.0.0.5"])
    order: list[object] = [entry.cls for entry in application.user_middleware]
    assert order == [
        ProxyHeadersMiddleware,
        SecurityHeadersMiddleware,
        ApiCorsMiddleware,
        LoggingCSRFMiddleware,
    ]


def test_cors_and_proxy_middleware_absent_when_unconfigured() -> None:
    """With no origins and no proxies only security headers and CSRF are installed."""
    order: list[object] = [entry.cls for entry in build_app().user_middleware]
    assert order == [SecurityHeadersMiddleware, LoggingCSRFMiddleware]


async def test_security_headers_on_success(client: httpx.AsyncClient) -> None:
    """A normal response carries the security headers."""
    response = await client.get("/page")
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "default-src 'self'" in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_security_headers_on_not_found_and_csrf_rejection(
    client: httpx.AsyncClient,
) -> None:
    """Error and rejected responses carry the headers too."""
    missing = await client.get("/nothing-here")
    assert missing.status_code == 404
    assert missing.headers["x-content-type-options"] == "nosniff"
    client.cookies.set(SESSION_COOKIE_NAME, "abc")
    rejected = await client.post("/change")
    assert rejected.status_code == 403
    assert rejected.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_no_cors_headers_by_default(client: httpx.AsyncClient) -> None:
    """Without configured origins no CORS headers are sent, even for API paths."""
    response = await client.get("/api/v1/thing", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in response.headers


async def test_cors_applies_to_api_prefix_only() -> None:
    """A configured origin is allowed on the API prefix and ignored elsewhere."""
    async with await client_for(build_app(cors_origins=[ORIGIN])) as http_client:
        api = await http_client.get("/api/v1/thing", headers={"Origin": ORIGIN})
        assert api.headers["access-control-allow-origin"] == ORIGIN
        assert "access-control-allow-credentials" not in api.headers
        web = await http_client.get("/page", headers={"Origin": ORIGIN})
        assert "access-control-allow-origin" not in web.headers
        lookalike = await http_client.get("/apix", headers={"Origin": ORIGIN})
        assert "access-control-allow-origin" not in lookalike.headers


async def test_cors_preflight_limits_headers_and_origins() -> None:
    """Preflight allows only Authorization and Content-Type, and only configured origins."""
    async with await client_for(build_app(cors_origins=[ORIGIN])) as http_client:
        allowed = await http_client.options(
            "/api/v1/thing",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == ORIGIN
        assert "access-control-allow-credentials" not in allowed.headers
        allowed_headers = allowed.headers["access-control-allow-headers"].split(", ")
        assert {"Authorization", "Content-Type"} <= set(allowed_headers)
        assert "X-Custom" not in allowed_headers
        denied_header = await http_client.options(
            "/api/v1/thing",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Custom",
            },
        )
        assert denied_header.status_code == 400
        other_origin = await http_client.options(
            "/api/v1/thing",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert other_origin.status_code == 400


def test_wildcard_cors_origin_refused() -> None:
    """Configuration refuses a wildcard or non-http origin."""
    with pytest.raises(ValueError, match="CORS origins"):
        Settings(environment="testing", cors_origins=["*"])
    with pytest.raises(ValueError, match="CORS origins"):
        Settings(environment="testing", cors_origins=["client.example"])
    with pytest.raises(ValueError, match="wildcard"):
        ApiCorsMiddleware(build_app(), allowed_origins=["*"])


async def test_csrf_cookie_issued_on_first_response(client: httpx.AsyncClient) -> None:
    """A safe request without a CSRF cookie receives one."""
    response = await client.get("/page")
    assert CSRF_COOKIE_NAME in response.cookies


async def test_csrf_rejects_unsafe_request_with_session_and_no_token(
    client: httpx.AsyncClient,
) -> None:
    """A state-changing request with a session cookie and no token is rejected with 403."""
    client.cookies.set(SESSION_COOKIE_NAME, "abc")
    response = await client.post("/change")
    assert response.status_code == 403


async def test_csrf_rejects_mismatching_token(client: httpx.AsyncClient) -> None:
    """A token that does not match the cookie is rejected."""
    await client.get("/page")
    client.cookies.set(SESSION_COOKIE_NAME, "abc")
    response = await client.post("/change", headers={CSRF_HEADER_NAME: "forged"})
    assert response.status_code == 403


async def test_csrf_accepts_matching_token(client: httpx.AsyncClient) -> None:
    """The cookie value echoed in the header passes the check."""
    await client.get("/page")
    client.cookies.set(SESSION_COOKIE_NAME, "abc")
    token = client.cookies[CSRF_COOKIE_NAME]
    response = await client.post("/change", headers={CSRF_HEADER_NAME: token})
    assert response.status_code == 200


async def test_csrf_not_enforced_without_session_cookie(client: httpx.AsyncClient) -> None:
    """Without a session cookie there is nothing for an attacker to ride on."""
    response = await client.post("/change")
    assert response.status_code == 200


async def test_csrf_required_on_login_without_session(client: httpx.AsyncClient) -> None:
    """The login path is always checked, so login forms need the token too."""
    rejected = await client.post("/login")
    assert rejected.status_code == 403
    await client.get("/page")
    token = client.cookies[CSRF_COOKIE_NAME]
    accepted = await client.post("/login", headers={CSRF_HEADER_NAME: token})
    assert accepted.status_code == 200


async def test_csrf_exempts_api_prefix(client: httpx.AsyncClient) -> None:
    """Bearer-token API calls skip CSRF even when a session cookie is sent."""
    client.cookies.set(SESSION_COOKIE_NAME, "abc")
    response = await client.post("/api/v1/thing")
    assert response.status_code == 200


async def test_forwarded_headers_ignored_without_trusted_proxy(
    client: httpx.AsyncClient,
) -> None:
    """Forwarded headers are not honoured when no proxy is configured."""
    response = await client.get(
        "/client", headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
    )
    assert response.json() == {"host": "10.0.0.5", "scheme": "http"}


async def test_forwarded_headers_honoured_from_trusted_proxy() -> None:
    """Forwarded headers apply only when the peer is a configured proxy."""
    headers = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
    async with await client_for(build_app(trusted_proxies=["10.0.0.5"])) as trusted:
        response = await trusted.get("/client", headers=headers)
    assert response.json() == {"host": "203.0.113.9", "scheme": "https"}
    async with await client_for(build_app(trusted_proxies=["10.9.9.9"])) as untrusted:
        response = await untrusted.get("/client", headers=headers)
    assert response.json() == {"host": "10.0.0.5", "scheme": "http"}


def test_csrf_secret_required_outside_testing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production-like environments refuse a missing or short CSRF secret."""
    monkeypatch.delenv("ENVMANAGER_CSRF_SECRET", raising=False)
    with pytest.raises(ValueError, match="CSRF_SECRET"):
        Settings(environment="production")
    with pytest.raises(ValueError, match="CSRF_SECRET"):
        Settings(environment="production", csrf_secret=SecretStr("short"))
    valid = Settings(environment="production", csrf_secret=SecretStr(SECRET))
    assert valid.environment == "production"
    assert Settings(environment="testing").csrf_secret is None
