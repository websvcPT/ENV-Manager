"""The address of the client, as far as the application can know it.

``ProxyHeadersMiddleware`` is the outermost middleware. It reads the peer the connection came
from and, only when that peer is a configured trusted proxy, takes the client from the
``X-Forwarded-For`` header (the right-most address that is not itself a trusted proxy; an
unreadable hop gives the address of the trusted hop that wrote it) and the scheme from
``X-Forwarded-Proto``. It writes the result to ``scope["client"]`` and
``scope["scheme"]`` on the very dict the server holds, so everything that runs later sees it:
the audit entries, the lockout counters, the sessions, the token last used address, the allowed
source check and the server's own access log. ``client_address`` is the one way to read it.
"""

from collections.abc import Sequence

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.log import get_logger
from app.core.networks import (
    IPAddress,
    IPNetwork,
    NetworkSet,
    normalize_address,
    parse_address,
    parse_forwarded_entry,
)

log = get_logger("client_address")

FALLBACK_SCOPE_KEY = "envmanager.client_is_fallback"
WARNED_LIMIT = 256
FORWARDED_FOR = b"x-forwarded-for"
FORWARDED_PROTO = b"x-forwarded-proto"
SCHEMES = {"http": "http", "https": "https"}
WEBSOCKET_SCHEMES = {"http": "ws", "https": "wss"}


def client_address(connection: HTTPConnection) -> str | None:
    """The resolved client address of a request, or None when there is none.

    Only a valid address is returned: the text of a forwarded header is never passed on as is.
    """
    client = connection.client
    return normalize_address(client.host) if client else None


def _header_values(scope: Scope, name: bytes) -> list[str]:
    """Every value of a header, one per header line, in the order they were sent."""
    return [value.decode("latin1") for key, value in scope["headers"] if key == name]


class ProxyHeadersMiddleware:
    """Resolve the client address and scheme, honouring forwarded headers from trusted peers only.

    A peer that is not a trusted proxy keeps its own address and every forwarded header it sends
    is ignored, so nobody can choose the address that is counted, recorded or checked. When the
    walk of a trusted peer's forwarded list meets a hop that is not an address, the client becomes
    the address of the trusted hop that wrote it, so every request has an address for the
    counters, lockouts, logs and audit entries. The scope is marked (``FALLBACK_SCOPE_KEY``) and
    the allowed sources check refuses such a request, because the proxy's own address says nothing
    about where the client is.
    """

    def __init__(self, app: ASGIApp, trusted_proxies: Sequence[IPNetwork] = ()) -> None:
        """Wrap the application with the networks whose forwarded headers are believed."""
        self.app = app
        self.trusted = NetworkSet(trusted_proxies)
        self._warned: set[IPAddress] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Rewrite the client and scheme of a request or websocket in place, then continue."""
        if scope["type"] in ("http", "websocket"):
            self._resolve(scope)
        await self.app(scope, receive, send)

    def _resolve(self, scope: Scope) -> None:
        """Set ``scope['client']`` to the resolved address (or None) and the trusted scheme."""
        peer = scope.get("client")
        peer_address = parse_address(peer[0]) if peer else None
        if peer is None or peer_address is None:
            scope["client"] = None
            return
        client: tuple[IPAddress, int] = (peer_address, peer[1])
        if self.trusted.contains(peer_address):
            client, fallback = self._forwarded_client(scope, client)
            if fallback:
                scope[FALLBACK_SCOPE_KEY] = True
                self._warn_once(client[0])
            self._apply_scheme(scope)
        scope["client"] = (str(client[0]), client[1])

    def _warn_once(self, proxy: IPAddress) -> None:
        """Log that a trusted proxy wrote an unreadable hop, once per proxy address and process.

        The unreadable value is never logged: it is client controlled text. The set of warned
        addresses stops growing at a fixed size so a large trusted network cannot grow it.
        """
        if proxy in self._warned or len(self._warned) >= WARNED_LIMIT:
            return
        self._warned.add(proxy)
        log.warning(
            "trusted proxy %s forwarded an unreadable client address, so its own address is "
            "used as the client for such requests",
            proxy,
        )

    def _forwarded_client(
        self, scope: Scope, peer: tuple[IPAddress, int]
    ) -> tuple[tuple[IPAddress, int], bool]:
        """The right-most address of the forwarded list that is not a trusted proxy.

        Returns the client and whether it is the fallback for an unreadable hop. Without the
        header the trusted peer itself is the client. The list is walked from the right and each
        entry is read only when the walk reaches it: a trusted proxy continues the walk, and the
        first address that is not a trusted proxy is the client. What stands left of that address
        was written by the client and is never read, so junk a client prepends changes nothing.
        When every address listed is a trusted proxy, the left-most one is the client. An entry
        that is not an address ends the walk with the last trusted address seen, which is the
        hop that wrote it (the peer itself when the right-most entry is unreadable), and marks the
        result as a fallback.
        """
        lines = _header_values(scope, FORWARDED_FOR)
        if not lines:
            return peer, False
        items = [item for line in lines for item in line.split(",")]
        trusted_hop = peer
        for item in reversed(items):
            entry = parse_forwarded_entry(item)
            if entry is None:
                return trusted_hop, True
            if not self.trusted.contains(entry[0]):
                return entry, False
            trusted_hop = entry
        return trusted_hop, False

    def _apply_scheme(self, scope: Scope) -> None:
        """Take http or https from the right-most ``X-Forwarded-Proto`` value of a trusted proxy."""
        lines = _header_values(scope, FORWARDED_PROTO)
        values = [item.strip().lower() for line in lines for item in line.split(",")]
        if not values:
            return
        table = WEBSOCKET_SCHEMES if scope["type"] == "websocket" else SCHEMES
        scheme = table.get(values[-1])
        if scheme is not None:
            scope["scheme"] = scheme
