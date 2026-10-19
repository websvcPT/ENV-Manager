"""Trace context from trusted proxies only.

FastAPI starts the request span before the middleware stack runs and takes its parent from the
``traceparent`` header, so a client could choose the trace id that lands on audit entries. The
application class here removes the propagation headers from a request whose peer is not a
configured trusted proxy before FastAPI looks at them, so every such request starts a root trace.
The peer is read from the scope as the server set it, which is exactly the connection's own
address at that point.
"""

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from app.core.networks import IPNetwork, NetworkSet, parse_address

PROPAGATION_HEADERS = frozenset({b"traceparent", b"tracestate", b"baggage"})


def drop_untrusted_trace_headers(scope: Scope, trusted: NetworkSet) -> None:
    """Remove the trace propagation headers unless the peer is a trusted proxy."""
    peer = scope.get("client")
    address = parse_address(peer[0]) if peer else None
    if address is not None and trusted.contains(address):
        return
    headers = scope.get("headers", [])
    if any(key in PROPAGATION_HEADERS for key, _ in headers):
        scope["headers"] = [
            (key, value) for key, value in headers if key not in PROPAGATION_HEADERS
        ]


class TraceGuardedFastAPI(FastAPI):
    """A FastAPI application that honours a client trace context only from trusted proxies."""

    def __init__(self, *, trusted_proxies: Sequence[IPNetwork] = (), **kwargs: Any) -> None:
        """Keep the trusted networks and build the application with the remaining arguments."""
        super().__init__(**kwargs)
        self._trusted_proxies = NetworkSet(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Strip an untrusted trace context, then handle the request as usual."""
        if scope["type"] in ("http", "websocket"):
            drop_untrusted_trace_headers(scope, self._trusted_proxies)
        await super().__call__(scope, receive, send)
