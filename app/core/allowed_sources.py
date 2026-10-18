"""Refuse requests from client addresses that are not on the configured allowed list.

The check runs inside the proxy header and security header middleware (so it sees the resolved
client address and its answer carries the security headers) and before everything else: it opens
no database session, writes no audit entry and sets no cookie, so traffic from outside the list
costs almost nothing. The only trace is a log line, written at a bounded rate. The health
endpoint stays reachable so the container health check works whatever the list says.
"""

import json
import logging
import time
from collections.abc import Callable, Sequence

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.cors import is_api_path
from app.core.log import get_logger
from app.core.networks import IPNetwork, NetworkSet, parse_address

HEALTH_PATH = "/healthz"
API_BODY = json.dumps(
    {
        "error": {
            "code": "forbidden",
            "message": "Requests from this address are not allowed.",
            "fields": [],
            "request_id": None,
        }
    }
).encode()
PLAIN_BODY = b"Forbidden\n"
POLICY_VIOLATION = 1008

log = get_logger("allowed_sources")


class DenialLog:
    """Log refused requests at a bounded rate.

    Each window lets a few lines through; the refusals after that are only counted, and the count
    is reported once the next window starts. The log never grows with the number of clients, so a
    flood of refusals from many addresses costs a constant amount of memory and output.
    """

    def __init__(
        self,
        logger: logging.Logger = log,
        *,
        window: float = 60.0,
        burst: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Set the window in seconds, the lines allowed per window and the clock to read."""
        self.logger = logger
        self.window = window
        self.burst = burst
        self.clock = clock
        self._window_started: float | None = None
        self._logged = 0
        self._suppressed = 0

    def record(self, address: str | None) -> None:
        """Log one refusal of the given client address, or count it when over the rate."""
        now = self.clock()
        if self._window_started is None or now - self._window_started >= self.window:
            if self._suppressed:
                self.logger.warning(
                    "%d more requests from addresses that are not allowed sources were refused "
                    "and not logged",
                    self._suppressed,
                )
            self._window_started = now
            self._logged = 0
            self._suppressed = 0
        if self._logged < self.burst:
            self._logged += 1
            self.logger.warning(
                "request refused, client address %s is not an allowed source", address or "unknown"
            )
        else:
            self._suppressed += 1


class AllowedSourcesMiddleware:
    """Answer 403 to every request whose client address is not inside an allowed network.

    The address is the one the proxy header middleware resolved. A request with no usable client
    address is refused too. Under ``/api`` the answer has the API error shape, elsewhere it is a
    short plain text line; neither says anything about the list.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_sources: Sequence[IPNetwork],
        denials: DenialLog | None = None,
    ) -> None:
        """Wrap the application with the networks that may connect."""
        self.app = app
        self.allowed = NetworkSet(allowed_sources)
        self.denials = denials or DenialLog()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass an allowed request on, answer anything else with a 403."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"] == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        address = parse_address(client[0]) if client else None
        if address is not None and self.allowed.contains(address):
            await self.app(scope, receive, send)
            return
        self.denials.record(None if address is None else str(address))
        await self._refuse(scope, send)

    async def _refuse(self, scope: Scope, send: Send) -> None:
        """Send the 403 of an HTTP request, or close a websocket before it is accepted."""
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": POLICY_VIOLATION})
            return
        if is_api_path(scope["path"]):
            body, content_type = API_BODY, b"application/json"
        else:
            body, content_type = PLAIN_BODY, b"text/plain; charset=utf-8"
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"" if scope["method"] == "HEAD" else body,
            }
        )
