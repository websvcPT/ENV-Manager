"""The proxy header middleware: which forwarded headers are believed, and who sees the result."""

import asyncio
import errno
import logging
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from pydantic import SecretStr
from starlette.types import Receive, Scope, Send

from app.config import Settings
from app.core.client_address import ProxyHeadersMiddleware, client_address
from app.core.middleware import install_middleware
from app.core.networks import parse_networks

Header = tuple[bytes, bytes]


def networks(*entries: str) -> list[Any]:
    """The networks of the entries, as the settings would parse them."""
    return parse_networks(list(entries), "test")


async def resolve(
    trusted: Sequence[str],
    peer: tuple[str, int] | None,
    headers: Sequence[Header] = (),
    *,
    kind: str = "http",
) -> Scope:
    """Run a scope through the middleware and return the dict the application received."""
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    middleware = ProxyHeadersMiddleware(app, networks(*trusted))
    scope: Scope = {
        "type": kind,
        "scheme": "ws" if kind == "websocket" else "http",
        "client": peer,
        "headers": list(headers),
    }
    await middleware(scope, None, None)  # type: ignore[arg-type]
    assert seen and seen[0] is scope, "the middleware must pass on the dict the server holds"
    return scope


def forwarded(value: str, name: bytes = b"x-forwarded-for") -> Header:
    """A forwarded header line."""
    return (name, value.encode())


async def test_the_server_dict_itself_carries_the_resolved_client() -> None:
    """The scope is changed in place, which is how the server's access log learns the client."""
    original: Scope = {
        "type": "http",
        "scheme": "http",
        "client": ("10.0.0.5", 4000),
        "headers": [forwarded("203.0.113.9")],
    }
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    await ProxyHeadersMiddleware(app, networks("10.0.0.5"))(original, None, None)  # type: ignore[arg-type]
    assert seen[0] is original
    assert original["client"] == ("203.0.113.9", 0)


async def test_a_trusted_peer_gives_the_forwarded_client() -> None:
    """Behind a trusted proxy the forwarded address replaces the peer's."""
    scope = await resolve(["10.0.0.5"], ("10.0.0.5", 4000), [forwarded("203.0.113.9")])
    assert scope["client"] == ("203.0.113.9", 0)


async def test_an_untrusted_peer_cannot_choose_its_address_or_scheme() -> None:
    """Forwarded headers from anyone else are ignored."""
    headers = [forwarded("203.0.113.9"), forwarded("https", b"x-forwarded-proto")]
    scope = await resolve(["10.0.0.5"], ("198.51.100.7", 4000), headers)
    assert scope["client"] == ("198.51.100.7", 4000)
    assert scope["scheme"] == "http"
    scope = await resolve([], ("198.51.100.7", 4000), headers)
    assert scope["client"] == ("198.51.100.7", 4000)


async def test_the_right_most_untrusted_address_wins() -> None:
    """A client cannot prepend an address: the list is read from the nearest proxy backwards."""
    trusted = ["10.0.0.0/8"]
    scope = await resolve(
        trusted, ("10.0.0.5", 1), [forwarded("1.1.1.1, 198.51.100.20, 10.1.2.3, 10.1.2.4")]
    )
    assert scope["client"] == ("198.51.100.20", 0)
    spoofed = await resolve(trusted, ("10.0.0.5", 1), [forwarded("10.9.9.9, 198.51.100.20")])
    assert spoofed["client"] == ("198.51.100.20", 0)


async def test_when_every_listed_address_is_a_trusted_proxy_the_first_is_the_client() -> None:
    """A request that started inside the trusted network has no untrusted address to find."""
    scope = await resolve(["10.0.0.0/8"], ("10.0.0.5", 1), [forwarded("10.1.1.1, 10.2.2.2")])
    assert scope["client"] == ("10.1.1.1", 0)


async def test_without_the_header_the_trusted_peer_is_the_client() -> None:
    """The proxy's own requests (a probe) are the proxy's."""
    scope = await resolve(["10.0.0.5"], ("10.0.0.5", 4000))
    assert scope["client"] == ("10.0.0.5", 4000)


async def test_several_header_lines_are_one_list() -> None:
    """Repeated header lines read as one comma separated list, in order, not the last line only."""
    headers = [forwarded("203.0.113.9"), forwarded("10.1.2.3")]
    scope = await resolve(["10.0.0.0/8"], ("10.0.0.5", 1), headers)
    assert scope["client"] == ("203.0.113.9", 0)


async def test_ports_in_the_list_are_dropped_from_the_address() -> None:
    """A proxy that writes ``addr:port`` still names the address, and IPv6 works too."""
    scope = await resolve(
        ["10.0.0.0/8", "2001:db8::/32"],
        ("10.0.0.5", 1),
        [forwarded("[2001:db8:1::7]:5000, 10.1.1.1:80")],
    )
    assert scope["client"] == ("2001:db8:1::7", 5000)


@pytest.mark.parametrize("value", ["unknown", "203.0.113.9, unknown", "203.0.113.9,", ",", "x"])
async def test_a_hop_that_is_not_an_address_leaves_no_client_never_the_proxy(value: str) -> None:
    """Never fall back to the proxy's address: it may be on the allowed list."""
    scope = await resolve(["10.0.0.5"], ("10.0.0.5", 4000), [forwarded(value)])
    assert scope["client"] is None


@pytest.mark.parametrize(
    "prefix", ["unknown", "_hidden", "", "garbage, more garbage", "<script>alert(1)</script>"]
)
async def test_junk_a_client_prepends_is_never_read(prefix: str) -> None:
    """The client writes the left of the list: what stands left of the first untrusted address
    does not matter, so it cannot take the real address away.
    """
    value = f"{prefix}, 198.51.100.20, 10.1.2.3"
    scope = await resolve(["10.0.0.0/8"], ("10.0.0.5", 1), [forwarded(value)])
    assert scope["client"] == ("198.51.100.20", 0)
    plain = await resolve(["10.0.0.5"], ("10.0.0.5", 1), [forwarded(f"{prefix}, 198.51.100.20")])
    assert plain["client"] == ("198.51.100.20", 0)


async def test_a_mapped_peer_is_matched_and_recorded_as_ipv4() -> None:
    """A dual stack socket reports ``::ffff:a.b.c.d``; trust and the address use the IPv4 form."""
    scope = await resolve(["10.0.0.0/8"], ("::ffff:10.0.0.5", 4000), [forwarded("203.0.113.9")])
    assert scope["client"] == ("203.0.113.9", 0)
    plain = await resolve([], ("::ffff:198.51.100.7", 4000))
    assert plain["client"] == ("198.51.100.7", 4000)


async def test_a_mapped_forwarded_address_is_unwrapped_before_matching() -> None:
    """The trusted proxy list matches an address written in mapped form."""
    scope = await resolve(
        ["10.0.0.0/8"], ("10.0.0.5", 1), [forwarded("203.0.113.9, ::ffff:10.1.2.3")]
    )
    assert scope["client"] == ("203.0.113.9", 0)


async def test_ipv6_peers_and_networks() -> None:
    """IPv6 trusted networks and clients work like IPv4."""
    scope = await resolve(
        ["2001:db8:aaaa::/48"], ("2001:db8:aaaa::1", 1), [forwarded("2001:db8:bbbb::9")]
    )
    assert scope["client"] == ("2001:db8:bbbb::9", 0)
    outside = await resolve(
        ["2001:db8:aaaa::/48"], ("2001:db8:cccc::1", 1), [forwarded("2001:db8:bbbb::9")]
    )
    assert outside["client"] == ("2001:db8:cccc::1", 1)


async def test_a_peer_that_is_not_an_address_has_no_client() -> None:
    """Nothing can be said about a peer that is not an IP address (a socket path, a name)."""
    for peer in (None, ("not-an-ip", 1)):
        scope = await resolve(["10.0.0.5"], peer, [forwarded("203.0.113.9")])
        assert scope["client"] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https", "https"),
        ("HTTPS", "https"),
        ("http", "http"),
        ("http, https", "https"),
        ("https, http", "http"),
        ("ftp", "http"),
        ("", "http"),
        ("javascript", "http"),
    ],
)
async def test_the_scheme_comes_from_a_trusted_proxy_and_is_http_or_https(
    value: str, expected: str
) -> None:
    """Only http and https are taken, from the right-most value, and only from a trusted peer."""
    scope = await resolve(["10.0.0.5"], ("10.0.0.5", 1), [forwarded(value, b"x-forwarded-proto")])
    assert scope["scheme"] == expected


async def test_websockets_get_the_matching_scheme() -> None:
    """A websocket behind a trusted proxy that spoke https is wss."""
    scope = await resolve(
        ["10.0.0.5"],
        ("10.0.0.5", 1),
        [forwarded("203.0.113.9"), forwarded("https", b"x-forwarded-proto")],
        kind="websocket",
    )
    assert scope["scheme"] == "wss" and scope["client"] == ("203.0.113.9", 0)


async def test_lifespan_passes_through_untouched() -> None:
    """Only requests are rewritten."""
    scope: Scope = {"type": "lifespan"}
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    await ProxyHeadersMiddleware(app, networks("10.0.0.5"))(scope, None, None)  # type: ignore[arg-type]
    assert seen == [{"type": "lifespan"}]


def test_the_helper_returns_only_a_normalised_address() -> None:
    """The helper reads the resolved client of a request and never raw header text."""

    def request_for(client: tuple[str, int] | None) -> Request:
        return Request({"type": "http", "headers": [], "client": client})

    assert client_address(request_for(("203.0.113.9", 1))) == "203.0.113.9"
    assert client_address(request_for(("::ffff:203.0.113.9", 1))) == "203.0.113.9"
    assert client_address(request_for(("2001:db8:0::1", 1))) == "2001:db8::1"
    assert client_address(request_for(None)) is None
    assert client_address(request_for(("unknown", 1))) is None


SECRET = "test-secret-0123456789abcdef0123456789"


def dual_stack_available() -> bool:
    """Whether this host can bind an IPv6 socket that also takes IPv4 clients (``::`` mapped).

    The one test that serves the application on such a socket needs a kernel with IPv6; a host
    without it skips that test with this reason, which is the one allowed skip of the suite.
    """
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            probe.bind(("::", 0))
    except OSError:
        return False
    return True


@asynccontextmanager
async def live_server(
    *, dual_stack: bool = False, peers: list[Any] | None = None, **settings: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """The real middleware stack on a real server socket, with a client of it.

    The server is uvicorn without its own proxy handling, as ``start.sh`` runs it. A dual stack
    listener (an IPv6 socket that also takes IPv4) reports IPv4 peers in mapped form; ``peers``
    collects the peer address the server handed over, before the middleware changed it.
    """
    application = FastAPI()
    install_middleware(
        application, Settings(environment="testing", csrf_secret=SecretStr(SECRET), **settings)
    )

    @application.get("/who")
    async def who(request: Request) -> dict[str, str | None]:
        return {"client": client_address(request), "scheme": request.url.scheme}

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        if peers is not None and scope["type"] == "http":
            peers.append(scope["client"])
        await application(scope, receive, send)

    listener: socket.socket | None = None
    address: dict[str, Any] = {"host": "127.0.0.1", "port": 0}
    if dual_stack:
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind(("::", 0))
        address = {"fd": listener.fileno()}
    config = uvicorn.Config(
        recording,
        **address,
        lifespan="off",
        log_config=None,
        log_level="info",
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            yield http
    finally:
        server.should_exit = True
        await task
        if listener is not None:
            listener.close()


def access_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The server's access log lines, as text."""
    return [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]


async def test_the_servers_access_log_names_the_forwarded_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The access log of the real server shows the client, not the proxy that connected."""
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    headers = {"X-Forwarded-For": "198.51.100.20, 203.0.113.9", "X-Forwarded-Proto": "https"}
    async with live_server(trusted_proxies=["127.0.0.1"]) as http:
        answer = await http.get("/who", headers=headers)
    assert answer.json() == {"client": "203.0.113.9", "scheme": "https"}
    lines = access_lines(caplog)
    assert any(line.startswith("203.0.113.9:0 - ") and "/who" in line for line in lines)
    assert not any(line.startswith("127.0.0.1") for line in lines)


async def test_the_servers_access_log_ignores_headers_of_an_untrusted_peer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client that is not a trusted proxy shows up under its own address whatever it sends."""
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    async with live_server(trusted_proxies=["10.9.9.9"]) as http:
        answer = await http.get("/who", headers={"X-Forwarded-For": "203.0.113.9"})
    assert answer.json()["client"] == "127.0.0.1"
    lines = access_lines(caplog)
    assert any(line.startswith("127.0.0.1:") for line in lines)
    assert not any("203.0.113.9" in line for line in lines)


async def test_a_refused_request_is_logged_under_the_forwarded_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 403 of the allowed sources check is in the access log with the real client."""
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    async with live_server(trusted_proxies=["127.0.0.1"], allowed_sources=["10.0.0.0/8"]) as http:
        refused = await http.get("/who", headers={"X-Forwarded-For": "203.0.113.9"})
        allowed = await http.get("/who", headers={"X-Forwarded-For": "10.4.4.4"})
    assert refused.status_code == 403
    assert allowed.status_code == 200
    lines = access_lines(caplog)
    assert any(line.startswith("203.0.113.9:0 - ") and " 403" in line for line in lines)
    assert any(line.startswith("10.4.4.4:0 - ") and " 200" in line for line in lines)


@pytest.mark.skipif(
    not dual_stack_available(), reason="the host has no IPv6, so no dual stack socket can be bound"
)
async def test_a_mapped_peer_of_a_dual_stack_listener_is_logged_and_recorded_as_ipv4(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An IPv4 client of a dual stack socket arrives mapped and is seen as plain IPv4."""
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    peers: list[Any] = []
    async with live_server(dual_stack=True, peers=peers) as http:
        answer = await http.get("/who")
    assert peers and peers[0][0] == "::ffff:127.0.0.1"
    assert answer.json()["client"] == "127.0.0.1"
    lines = access_lines(caplog)
    assert any(line.startswith("127.0.0.1:") for line in lines)
    assert not any("::ffff" in line for line in lines)


def test_the_dual_stack_probe_says_no_without_ipv6_and_when_the_bind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either condition of a host without IPv6 turns the dual stack test into a skip."""
    monkeypatch.setattr(socket, "has_ipv6", False)
    assert dual_stack_available() is False

    def refuse(*_arguments: object, **_options: object) -> socket.socket:
        raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")

    monkeypatch.setattr(socket, "has_ipv6", True)
    monkeypatch.setattr(socket, "socket", refuse)
    assert dual_stack_available() is False
