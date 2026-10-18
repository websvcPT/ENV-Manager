"""Allowed sources: the configured list, the refusal and the guarantee that nothing else runs."""

import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from starlette.types import Message, Scope

from app.config import Settings, get_settings
from app.core.allowed_sources import (
    API_BODY,
    PLAIN_BODY,
    AllowedSourcesMiddleware,
    DenialLog,
)
from app.core.csrf import CSRF_COOKIE_NAME
from app.core.middleware import install_middleware
from app.core.security_headers import CONTENT_SECURITY_POLICY
from app.main import create_app
from app.models import AuditEntry, LoginCounter
from app.tests.api_helpers import bearer

SECRET = "test-secret-0123456789abcdef0123456789"
ALLOWED = ["10.0.0.0/8", "2001:db8::/32", "192.0.2.7"]


def build_app(**overrides: Any) -> FastAPI:
    """An application with the real middleware stack and a few probe routes."""
    settings = Settings(environment="testing", csrf_secret=SecretStr(SECRET), **overrides)
    application = FastAPI()
    install_middleware(application, settings)

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/page")
    async def page() -> dict[str, str]:
        return {"ok": "yes"}

    @application.post("/page")
    async def change() -> dict[str, str]:
        return {"ok": "yes"}

    @application.get("/api/v1/thing")
    async def api_thing() -> dict[str, str]:
        return {"ok": "yes"}

    @application.get("/healthz/deep")
    async def deep() -> dict[str, str]:
        return {"ok": "yes"}

    return application


def client_from(application: FastAPI, host: str) -> httpx.AsyncClient:
    """A client whose requests arrive from the given peer address."""
    transport = httpx.ASGITransport(app=application, client=(host, 4000))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.parametrize("host", ["10.1.2.3", "2001:db8:1::5", "192.0.2.7", "::ffff:10.1.2.3"])
async def test_addresses_inside_the_list_are_served(host: str) -> None:
    """IPv4 and IPv6 hosts and networks pass, and a mapped IPv4 client counts as IPv4."""
    async with client_from(build_app(allowed_sources=ALLOWED), host) as http:
        assert (await http.get("/page")).status_code == 200


@pytest.mark.parametrize(
    "host", ["11.0.0.1", "192.0.2.8", "2001:db9::1", "::1", "::ffff:11.0.0.1", "203.0.113.50"]
)
async def test_addresses_outside_the_list_are_refused(host: str) -> None:
    """Everything else gets the same 403, on every method and path."""
    async with client_from(build_app(allowed_sources=ALLOWED), host) as http:
        for response in (
            await http.get("/page"),
            await http.post("/page"),
            await http.get("/nothing-here"),
            await http.get("/static/app.js"),
        ):
            assert response.status_code == 403
            assert response.content == PLAIN_BODY


async def test_the_refusal_is_minimal_uncached_and_carries_the_security_headers() -> None:
    """The body is one line, nothing is cached, no cookie is set and the headers are on it."""
    async with client_from(build_app(allowed_sources=ALLOWED), "203.0.113.50") as http:
        response = await http.get("/page")
        head = await http.head("/page")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in response.headers and CSRF_COOKIE_NAME not in response.cookies
    assert head.status_code == 403 and head.content == b""


async def test_under_the_api_prefix_the_refusal_is_json_in_the_shape_of_the_api() -> None:
    """``/api`` answers in the one JSON error shape, with nothing about the list."""
    async with client_from(build_app(allowed_sources=ALLOWED), "203.0.113.50") as http:
        response = await http.get("/api/v1/thing", headers=bearer("envm_anything"))
        lookalike = await http.get("/apix")
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "error": {
            "code": "forbidden",
            "message": "Requests from this address are not allowed.",
            "fields": [],
            "request_id": None,
        }
    }
    assert response.content == API_BODY
    assert lookalike.content == PLAIN_BODY
    assert "set-cookie" not in response.headers


async def test_the_health_endpoint_stays_reachable_by_exact_path() -> None:
    """Container health checks work whatever the list says; only that one path is exempt."""
    async with client_from(build_app(allowed_sources=ALLOWED), "203.0.113.50") as http:
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/healthz/deep")).status_code == 403
        assert (await http.get("/healthz?x=1")).status_code == 200
        assert (await http.get("/healthzz")).status_code == 403


async def test_an_empty_list_allows_everyone_and_installs_nothing() -> None:
    """The default is no restriction, and the middleware is not even in the stack."""
    application = build_app()
    installed: list[object] = [entry.cls for entry in application.user_middleware]
    assert AllowedSourcesMiddleware not in installed
    async with client_from(application, "203.0.113.50") as http:
        assert (await http.get("/page")).status_code == 200


async def asgi_status(application: FastAPI, scope: dict[str, Any]) -> list[Message]:
    """Run a raw scope through the application and collect what it sent."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    base: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/page",
        "raw_path": b"/page",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("test", 80),
        "root_path": "",
    }
    await application({**base, **scope}, receive, send)
    return sent


async def test_a_request_with_no_client_address_is_refused() -> None:
    """With a list configured, a peer that cannot be identified is not let in."""
    sent = await asgi_status(build_app(allowed_sources=ALLOWED), {"client": None})
    assert sent[0]["status"] == 403


async def test_a_websocket_from_outside_the_list_is_closed_before_it_is_accepted() -> None:
    """A websocket handshake is refused too (the application has none, but the rule is one)."""
    sent = await asgi_status(
        build_app(allowed_sources=ALLOWED),
        {"type": "websocket", "client": ("203.0.113.50", 1), "scheme": "ws"},
    )
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_forwarded_headers_of_an_untrusted_peer_cannot_open_the_door() -> None:
    """A client outside the list that claims an allowed address is still outside."""
    async with client_from(build_app(allowed_sources=ALLOWED), "203.0.113.50") as http:
        response = await http.get("/page", headers={"X-Forwarded-For": "10.1.2.3"})
    assert response.status_code == 403


async def test_behind_a_trusted_proxy_the_list_applies_to_the_forwarded_client() -> None:
    """The proxy's own address is not what is checked, the client it forwards is."""
    application = build_app(allowed_sources=ALLOWED, trusted_proxies=["203.0.113.50"])
    async with client_from(application, "203.0.113.50") as http:
        assert (await http.get("/page", headers={"X-Forwarded-For": "10.1.2.3"})).status_code == 200
        assert (
            await http.get("/page", headers={"X-Forwarded-For": "198.51.100.1"})
        ).status_code == 403
        assert (await http.get("/page")).status_code == 403


async def test_an_unreadable_forwarded_list_is_refused_even_if_the_proxy_is_allowed() -> None:
    """Garbage in the header never turns into the proxy's own, possibly allowed, address."""
    application = build_app(allowed_sources=["203.0.113.50"], trusted_proxies=["203.0.113.50"])
    async with client_from(application, "203.0.113.50") as http:
        assert (await http.get("/page")).status_code == 200
        response = await http.get("/page", headers={"X-Forwarded-For": "unknown"})
    assert response.status_code == 403


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """The current time."""
        return self.now


def denial_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The messages the allowed sources logger wrote."""
    return [r.getMessage() for r in caplog.records if r.name == "envmanager.allowed_sources"]


def test_refusals_are_logged_at_a_bounded_rate(caplog: pytest.LogCaptureFixture) -> None:
    """A window lets a few lines through, counts the rest and reports the count next window."""
    caplog.set_level(logging.WARNING, logger="envmanager.allowed_sources")
    clock = FakeClock()
    denials = DenialLog(window=60.0, burst=3, clock=clock)
    for index in range(10):
        denials.record(f"203.0.113.{index}")
    first = denial_messages(caplog)
    assert len(first) == 3
    assert first[0] == "request refused, client address 203.0.113.0 is not an allowed source"
    clock.now = 59.0
    denials.record("203.0.113.99")
    assert len(denial_messages(caplog)) == 3
    clock.now = 61.0
    denials.record(None)
    lines = denial_messages(caplog)
    assert lines[3].startswith("8 more requests") and "not logged" in lines[3]
    assert lines[4] == "request refused, client address unknown is not an allowed source"
    assert len(lines) == 5


def test_the_log_holds_no_state_per_address() -> None:
    """A flood from many addresses leaves a constant amount of state."""
    denials = DenialLog(burst=1, clock=FakeClock())
    for index in range(1000):
        denials.record(f"2001:db8::{index:x}")
    assert not any(isinstance(value, dict | list | set) for value in vars(denials).values())


async def test_the_middleware_logs_the_refused_address_and_nothing_of_the_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The line names the client address and carries neither path, query, header nor body."""
    caplog.set_level(logging.WARNING, logger="envmanager.allowed_sources")
    async with client_from(build_app(allowed_sources=ALLOWED), "203.0.113.50") as http:
        await http.get("/page?token=hunter2", headers={"Authorization": "Bearer secret-value"})
    assert denial_messages(caplog) == [
        "request refused, client address 203.0.113.50 is not an allowed source"
    ]


def test_the_settings_parse_both_lists_and_default_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment variables are comma separated lists of addresses and networks."""
    assert Settings(environment="testing").allowed_sources == []
    assert Settings(environment="testing").trusted_proxies == []
    monkeypatch.setenv("ENVMANAGER_ALLOWED_SOURCES", " 10.0.0.0/8, 2001:db8::/32 ,192.0.2.7")
    monkeypatch.setenv("ENVMANAGER_TRUSTED_PROXIES", "::ffff:172.18.0.2,172.19.0.0/16")
    settings = Settings(environment="testing")
    assert [str(n) for n in settings.allowed_sources] == [
        "10.0.0.0/8",
        "2001:db8::/32",
        "192.0.2.7/32",
    ]
    assert [str(n) for n in settings.trusted_proxies] == ["172.18.0.2/32", "172.19.0.0/16"]


@pytest.mark.parametrize("name", ["ENVMANAGER_TRUSTED_PROXIES", "ENVMANAGER_ALLOWED_SOURCES"])
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("*", "'*' is not accepted"),
        ("10.0.0.1,*", "'*' is not accepted"),
        ("proxy.internal", "'proxy.internal' is not an IPv4 or IPv6 address or network"),
        ("10.0.0.256", "is not an IPv4 or IPv6 address or network"),
        ("10.0.0.5/24", "'10.0.0.5/24' has host bits set"),
        ("2001:db8::1/32", "has host bits set"),
        ("10.0.0.1 10.0.0.2", "is not an IPv4 or IPv6 address or network"),
    ],
)
def test_invalid_entries_refuse_startup_naming_the_variable_and_entry(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, reason: str
) -> None:
    """A bad entry stops the application from starting, and the message says which one."""
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as caught:
        Settings(environment="testing")
    message = str(caught.value)
    assert f"{name}: " in message and reason in message


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0"])
def test_trusting_every_peer_is_refused_for_proxies_but_fine_for_sources(entry: str) -> None:
    """A network of every address trusts every peer, so proxies refuse it like the wildcard."""
    trusted: dict[str, Any] = {"trusted_proxies": [entry]}
    sources: dict[str, Any] = {"allowed_sources": [entry]}
    with pytest.raises(ValidationError, match="covers every address"):
        Settings(environment="testing", **trusted)
    assert Settings(environment="testing", **sources).allowed_sources


def test_the_production_app_refuses_to_start_with_a_bad_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the real application with an invalid entry raises before anything is served."""
    monkeypatch.setenv("ENVMANAGER_TRUSTED_PROXIES", "*")
    monkeypatch.setenv("ENVMANAGER_CSRF_SECRET", SECRET)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="ENVMANAGER_TRUSTED_PROXIES"):
            create_app()
    finally:
        get_settings.cache_clear()


class CountingFactory:
    """A session factory that counts how many sessions the application asked for."""

    def __init__(self, inner: Any) -> None:
        """Wrap the real factory."""
        self.inner = inner
        self.calls = 0

    def __call__(self) -> Any:
        """Count the request and hand out a real session."""
        self.calls += 1
        return self.inner()


@pytest.fixture
def allowed_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow only 10.0.0.0/8; runs before the application so its settings pick it up."""
    monkeypatch.setenv("ENVMANAGER_ALLOWED_SOURCES", "10.0.0.0/8")


async def table_counts(factory: Any) -> tuple[int, int]:
    """How many audit entries and login counters the database holds."""
    async with factory() as db:
        audit = (await db.execute(select(func.count()).select_from(AuditEntry))).scalar_one()
        counters = (await db.execute(select(func.count()).select_from(LoginCounter))).scalar_one()
    return int(audit), int(counters)


async def test_a_refused_request_never_opens_a_database_session(
    allowed_ten: None, web_app: FastAPI
) -> None:
    """Nothing is read or written for a request from outside the list: not a session, not a row.

    The factory is counted, so the test fails the moment any dependency, route or middleware asks
    for a session. The control at the end proves the probe sees sessions at all.
    """
    real_factory = web_app.state.session_factory
    before = await table_counts(real_factory)
    spy = CountingFactory(real_factory)
    web_app.state.session_factory = spy
    outside = httpx.ASGITransport(app=web_app, client=("203.0.113.50", 1))
    async with httpx.AsyncClient(transport=outside, base_url="http://test") as http:
        requests: list[Any] = [
            http.get("/projects"),
            http.get("/login"),
            http.post("/login", data={"identifier": "root", "password": "wrong"}),
            http.get("/audit"),
            http.get("/api/v1/services/1/deployments/last", headers=bearer("envm_unknown")),
            http.post("/api/v1/services/1/deploy", headers=bearer("envm_unknown"), json={}),
            http.get("/static/app.js"),
            http.get("/nothing-here"),
        ]
        for pending in requests:
            response = await pending
            assert response.status_code == 403, response.request.url
    assert spy.calls == 0
    assert await table_counts(real_factory) == before

    inside = httpx.ASGITransport(app=web_app, client=("10.1.2.3", 1))
    async with httpx.AsyncClient(transport=inside, base_url="http://test") as http:
        control = await http.get("/projects")
    assert control.status_code in (303, 401)
    assert spy.calls >= 1


async def test_the_health_check_works_from_outside_the_list_without_the_database(
    allowed_ten: None, web_app: FastAPI
) -> None:
    """The one exempt path answers from any address and does not touch the database either."""
    spy = CountingFactory(web_app.state.session_factory)
    web_app.state.session_factory = spy
    outside = httpx.ASGITransport(app=web_app, client=("203.0.113.50", 1))
    async with httpx.AsyncClient(transport=outside, base_url="http://test") as http:
        assert (await http.get("/healthz")).json() == {"status": "ok"}
    assert spy.calls == 0
