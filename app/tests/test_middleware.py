"""Middleware stack tests: order, security headers, CORS, CSRF and forwarded headers."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import SecretStr

from app.config import Settings
from app.core.allowed_sources import AllowedSourcesMiddleware
from app.core.client_address import ProxyHeadersMiddleware
from app.core.cors import ApiCorsMiddleware
from app.core.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_FORM_FIELD,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    generate_token,
    is_valid_token,
)
from app.core.middleware import install_middleware
from app.core.security_headers import (
    CONTENT_SECURITY_POLICY,
    DOCS_CONTENT_SECURITY_POLICY,
    SecurityHeadersMiddleware,
)
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

    @application.get("/token")
    async def token(request: Request) -> dict[str, str]:
        return {"token": request.state.csrf_token}

    @application.post("/form")
    async def form(request: Request) -> dict[str, str]:
        data = await request.form()
        return {key: str(value) for key, value in data.items() if key != CSRF_FORM_FIELD}

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


def test_middleware_order_is_proxy_headers_security_headers_sources_cors_csrf() -> None:
    """From the outside in: proxy headers, security headers, allowed sources, CORS, CSRF."""
    application = build_app(
        cors_origins=[ORIGIN], trusted_proxies=["10.0.0.5"], allowed_sources=["10.0.0.0/8"]
    )
    order: list[object] = [entry.cls for entry in application.user_middleware]
    assert order == [
        ProxyHeadersMiddleware,
        SecurityHeadersMiddleware,
        AllowedSourcesMiddleware,
        ApiCorsMiddleware,
        CSRFMiddleware,
    ]


def test_optional_middleware_absent_when_unconfigured() -> None:
    """With no origins and no sources only the address, security header and CSRF layers run."""
    order: list[object] = [entry.cls for entry in build_app().user_middleware]
    assert order == [ProxyHeadersMiddleware, SecurityHeadersMiddleware, CSRFMiddleware]


async def test_security_headers_on_success(client: httpx.AsyncClient) -> None:
    """A normal response carries the security headers."""
    response = await client.get("/page")
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "default-src 'self'" in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_the_docs_routes_get_the_relaxed_policy_and_nothing_else_does() -> None:
    """With the docs on only the docs pages get the relaxed policy.

    Every other route keeps the strict policy byte for byte.
    """
    async with await client_for(build_app(serve_docs=True)) as http:
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc"):
            policy = (await http.get(path)).headers["content-security-policy"]
            assert policy == DOCS_CONTENT_SECURITY_POLICY
            assert "https://cdn.jsdelivr.net" in policy
        for path in ("/page", "/openapi.json", "/nothing-here"):
            response = await http.get(path)
            assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_the_docs_paths_keep_the_strict_policy_when_the_docs_are_off() -> None:
    """Without ``serve_docs`` no path is relaxed, whether or not the application has a route."""
    async with await client_for(build_app()) as http:
        for path in ("/docs", "/redoc"):
            response = await http.get(path)
            assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


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


async def session_client_with_token(client: httpx.AsyncClient) -> str:
    """Give the client a session cookie and a CSRF cookie, returning the token."""
    await client.get("/page")
    client.cookies.set(SESSION_COOKIE_NAME, "abc")
    return client.cookies[CSRF_COOKIE_NAME]


async def test_csrf_accepts_urlencoded_form_field_and_replays_body(
    client: httpx.AsyncClient,
) -> None:
    """The hidden field passes and the application still reads the whole form."""
    token = await session_client_with_token(client)
    response = await client.post("/form", data={CSRF_FORM_FIELD: token, "name": "value"})
    assert response.status_code == 200
    assert response.json() == {"name": "value"}


async def test_csrf_accepts_multipart_form_field_and_replays_files(
    client: httpx.AsyncClient,
) -> None:
    """A multipart body is verified from its field and replayed intact."""
    token = await session_client_with_token(client)
    response = await client.post(
        "/form",
        data={CSRF_FORM_FIELD: token, "name": "value"},
        files={"upload": ("a.env", b"KEY=1\n", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "value"
    assert "upload" in response.json()


async def test_csrf_rejects_wrong_or_missing_form_field(client: httpx.AsyncClient) -> None:
    """A forged or absent field is rejected, for both form encodings."""
    await session_client_with_token(client)
    forged = await client.post("/form", data={CSRF_FORM_FIELD: "forged", "name": "x"})
    assert forged.status_code == 403
    missing = await client.post("/form", data={"name": "x"})
    assert missing.status_code == 403
    multipart = await client.post(
        "/form", data={CSRF_FORM_FIELD: "forged"}, files={"upload": ("a", b"b", "text/plain")}
    )
    assert multipart.status_code == 403


async def test_csrf_form_field_works_on_login_without_session(client: httpx.AsyncClient) -> None:
    """The login form submits the token as a hidden field."""
    await client.get("/page")
    token = client.cookies[CSRF_COOKIE_NAME]
    response = await client.post("/login", data={CSRF_FORM_FIELD: token})
    assert response.status_code == 200


async def test_csrf_rejects_validly_signed_token_not_matching_cookie(
    client: httpx.AsyncClient,
) -> None:
    """A correctly signed token that is not the cookie token is rejected."""
    await session_client_with_token(client)
    other = generate_token(SECRET)
    response = await client.post("/change", headers={CSRF_HEADER_NAME: other})
    assert response.status_code == 403


async def test_csrf_rejects_oversized_form_body() -> None:
    """A form body over the bound is refused before it is buffered or parsed."""
    application = FastAPI()
    application.add_middleware(CSRFMiddleware, secret=SECRET, max_body_bytes=64)

    @application.post("/form")
    async def form() -> dict[str, str]:
        return {"ok": "yes"}

    async with await client_for(application) as http_client:
        await http_client.get("/form")
        token = http_client.cookies[CSRF_COOKIE_NAME]
        http_client.cookies.set(SESSION_COOKIE_NAME, "abc")
        declared = await http_client.post("/form", data={CSRF_FORM_FIELD: token, "pad": "x" * 200})
        assert declared.status_code == 413

        async def chunks() -> AsyncIterator[bytes]:
            yield f"{CSRF_FORM_FIELD}={token}&pad=".encode()
            yield b"x" * 200

        streamed = await http_client.post(
            "/form",
            content=chunks(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert streamed.status_code == 413


async def test_csrf_token_exposed_to_request_and_matches_cookie(
    client: httpx.AsyncClient,
) -> None:
    """The first render sees the token that the response then sets as the cookie."""
    first = await client.get("/token")
    assert first.json()["token"] == client.cookies[CSRF_COOKIE_NAME]
    second = await client.get("/token")
    assert second.json()["token"] == first.json()["token"]


async def test_csrf_cookie_attributes_and_reissue_of_invalid_cookie(
    client: httpx.AsyncClient,
) -> None:
    """The cookie is HttpOnly and strict, and an unsigned cookie value is replaced."""
    first = await client.get("/page")
    header = first.headers["set-cookie"]
    assert "HttpOnly" in header and "SameSite=Strict" in header and "Secure" not in header
    assert is_valid_token(SECRET, client.cookies[CSRF_COOKIE_NAME])
    client.cookies.clear()
    reissued = await client.get("/page", headers={"Cookie": f"{CSRF_COOKIE_NAME}=not-signed"})
    assert CSRF_COOKIE_NAME in reissued.headers["set-cookie"]
    assert is_valid_token(SECRET, client.cookies[CSRF_COOKIE_NAME])


async def test_csrf_cookie_secure_on_https_base_url() -> None:
    """An https base URL marks the token cookie Secure."""
    async with await client_for(build_app(base_url="https://env.example")) as http_client:
        response = await http_client.get("/page")
        assert "Secure" in response.headers["set-cookie"]


async def test_csrf_exempts_api_prefix(client: httpx.AsyncClient) -> None:
    """Bearer-token API calls skip CSRF even when a session cookie is sent."""
    client.cookies.set(SESSION_COOKIE_NAME, "abc")
    response = await client.post("/api/v1/thing")
    assert response.status_code == 200


async def test_api_answers_carry_no_csrf_cookie_and_the_prefix_is_exact(
    client: httpx.AsyncClient,
) -> None:
    """The API prefix is passed through without a token cookie; a page and a lookalike get one."""
    api = await client.get("/api/v1/thing")
    assert api.status_code == 200 and "set-cookie" not in api.headers
    posted = await client.post("/api/v1/thing")
    assert posted.status_code == 200 and "set-cookie" not in posted.headers
    assert CSRF_COOKIE_NAME not in client.cookies
    for path in ("/page", "/apix"):
        client.cookies.clear()
        answered = await client.get(path)
        assert CSRF_COOKIE_NAME in answered.headers["set-cookie"], path


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
