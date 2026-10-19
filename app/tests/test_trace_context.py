"""The trace propagation headers of a request are kept only when its peer is a trusted proxy."""

import httpx
from fastapi import Request
from starlette.types import Scope

from app.core.networks import NetworkSet, parse_networks
from app.core.trace_context import TraceGuardedFastAPI, drop_untrusted_trace_headers

TRACEPARENT = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRUSTED = NetworkSet(parse_networks("10.0.0.0/8", "trusted_proxies", allow_everything=False))


def scope_from(peer: tuple[str, int] | None) -> Scope:
    """A request scope from a peer, with the propagation headers and an ordinary one."""
    return {
        "type": "http",
        "client": peer,
        "headers": [
            (b"traceparent", TRACEPARENT),
            (b"tracestate", b"vendor=1"),
            (b"baggage", b"user=1"),
            (b"accept", b"text/html"),
        ],
    }


def names(scope: Scope) -> list[bytes]:
    """The header names left in a scope."""
    return [name for name, _value in scope["headers"]]


def test_an_untrusted_peer_loses_the_propagation_headers_and_keeps_the_others() -> None:
    scope = scope_from(("203.0.113.5", 1000))
    drop_untrusted_trace_headers(scope, TRUSTED)
    assert names(scope) == [b"accept"]


def test_a_request_without_a_peer_loses_them_too() -> None:
    scope = scope_from(None)
    drop_untrusted_trace_headers(scope, TRUSTED)
    assert names(scope) == [b"accept"]


def test_nobody_is_trusted_without_configured_proxies() -> None:
    scope = scope_from(("10.1.2.3", 1000))
    drop_untrusted_trace_headers(scope, NetworkSet())
    assert names(scope) == [b"accept"]


def test_a_trusted_peer_keeps_all_headers() -> None:
    scope = scope_from(("10.1.2.3", 1000))
    drop_untrusted_trace_headers(scope, TRUSTED)
    assert len(scope["headers"]) == 4


async def test_the_application_hands_its_routes_only_what_the_peer_may_send() -> None:
    application = TraceGuardedFastAPI(trusted_proxies=TRUSTED.networks)

    @application.get("/seen")
    async def seen(request: Request) -> dict[str, bool]:
        return {"traceparent": "traceparent" in request.headers}

    for peer, expected in ((("203.0.113.5", 1), False), (("10.9.9.9", 1), True)):
        transport = httpx.ASGITransport(app=application, client=peer)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/seen", headers={"traceparent": TRACEPARENT.decode()})
        assert response.json() == {"traceparent": expected}
