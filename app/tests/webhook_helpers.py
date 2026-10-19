"""Helpers of the webhook tests: a recording transport, a real receiver on the loopback interface,
a reference verifier written the way a receiver would write it, and the stored settings."""

import asyncio
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.crypto import CryptoService
from app.core.db import create_session_factory
from app.services.app_settings import (
    WEBHOOK_SECRET_KEY,
    WEBHOOK_URL_KEY,
    clear_secret,
    load_stored_settings,
    save_section,
    write_secret,
)
from app.services.audit import AuditActor
from app.services.webhook import WebhookFailure

WEBHOOK_URL = "https://hooks.example.com/services/T0001/B0002/path-token-4f1c9a"
WEBHOOK_SECRET = "webhook-secret-sentinel-0123456789"


@dataclass(frozen=True)
class SentRequest:
    """One post the transport was given: the address, every header and the exact body bytes."""

    url: str
    headers: Mapping[str, str]
    body: bytes


@dataclass
class RecordingWebhook:
    """A webhook transport that records every post and can fail or hang on demand.

    ``outcomes`` are used one per call, oldest first: an exception is raised, an integer is the
    status of a success; an exhausted list means a plain 200. ``hang`` makes every call wait for
    that event, so a test can prove nothing waits for the webhook.
    """

    sent: list[SentRequest] = field(default_factory=list)
    outcomes: list[Exception | int] = field(default_factory=list)
    hang: asyncio.Event | None = None

    async def post(self, url: str, headers: Mapping[str, str], body: bytes) -> int:
        """Record the post, then answer the way the next outcome says."""
        self.sent.append(SentRequest(url, dict(headers), body))
        if self.hang is not None:
            await self.hang.wait()
        if not self.outcomes:
            return 200
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def temporary_failure() -> WebhookFailure:
    """A failure that trying again can fix."""
    return WebhookFailure(
        "The webhook answered with an error (HTTP 503).", retryable=True, status=503
    )


def permanent_failure() -> WebhookFailure:
    """A failure that trying again cannot fix."""
    return WebhookFailure(
        "The webhook refused the request (HTTP 404).", retryable=False, status=404
    )


def reference_signature(secret: str, timestamp: str, body: bytes) -> str:
    """What a receiver computes to check a document, written without the application's code."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def verifies(secret: str, request: SentRequest) -> bool:
    """Whether the signature header of a request matches the reference computation."""
    expected = reference_signature(secret, request.headers["X-EnvManager-Timestamp"], request.body)
    return hmac.compare_digest(expected, request.headers["X-EnvManager-Signature"])


async def configure_webhook(
    engine: AsyncEngine,
    crypto: CryptoService,
    *,
    url: str = WEBHOOK_URL,
    secret: str | None = WEBHOOK_SECRET,
    enabled: bool = True,
) -> None:
    """Store the webhook address, switch and secret, keeping the rest of the matrix."""
    async with create_session_factory(engine)() as db:
        current = (await load_stored_settings(db)).notifications
        new = replace(current, webhook_enabled=enabled)
        await save_section(db, "notifications", new, actor=AuditActor.cli())
        if url:
            await write_secret(db, crypto, WEBHOOK_URL_KEY, url, actor=AuditActor.cli())
        else:
            await clear_secret(db, WEBHOOK_URL_KEY, actor=AuditActor.cli())
        if secret is not None:
            await write_secret(db, crypto, WEBHOOK_SECRET_KEY, secret, actor=AuditActor.cli())
        await db.commit()


@dataclass(frozen=True)
class RawRequest:
    """A request as a real receiver read it off the socket."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes


class Receiver:
    """A tiny HTTP server on the loopback interface that records requests and answers a status.

    ``delay`` makes it wait before answering, ``location`` adds a redirect target.
    """

    def __init__(
        self, status: int = 200, *, delay: float = 0.0, location: str | None = None
    ) -> None:
        """Answer every request with ``status``."""
        self.status = status
        self.delay = delay
        self.location = location
        self.requests: list[RawRequest] = []
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> Receiver:
        """Start listening on a free port."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Stop listening."""
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        """The address of the receiver."""
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/hook?token=url-token-91c3"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read one request, record it and answer."""
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, path, _version = lines[0].split(" ", 2)
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            length = int(headers.get("Content-Length", "0"))
            body = await reader.readexactly(length) if length else b""
            self.requests.append(RawRequest(method, path, headers, body))
            if self.delay:
                await asyncio.sleep(self.delay)
            extra = f"Location: {self.location}\r\n" if self.location else ""
            writer.write(
                f"HTTP/1.1 {self.status} Answer\r\n{extra}Content-Length: 0\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()
