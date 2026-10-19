"""The outbound webhook: checking its address, signing a document and posting it once.

A document is posted with two headers that let the receiver trust it: a Unix timestamp, and an
HMAC-SHA256 signature, in hex, over the timestamp, a dot and the body bytes exactly as sent, made
with the shared secret. The same bytes that are signed are the ones sent, and every attempt gets
a fresh timestamp and signature.

A failure becomes a ``WebhookFailure`` that carries fixed words, the HTTP status when there was
one and whether trying again can help. The text of an exception is never kept, because the
library puts the address (which can hold a token) into its messages, and the answer of the
receiver is never read or stored. Redirects are not followed.
"""

import hashlib
import hmac
import ssl
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from app import VERSION

TIMESTAMP_HEADER = "X-EnvManager-Timestamp"
SIGNATURE_HEADER = "X-EnvManager-Signature"
SIGNATURE_PREFIX = "sha256="
MAX_URL_LENGTH = 2048
MIN_SECRET_LENGTH = 16
MAX_SECRET_LENGTH = 1024
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
ALLOWED_SCHEMES = ("http", "https")
RETRYABLE_STATUSES = frozenset({408, 429})

TIMED_OUT = "The webhook did not answer in time."
UNREACHABLE = "The webhook could not be reached."
TLS_FAILED = "The webhook's TLS certificate could not be verified."
REDIRECTED = "The webhook answered with a redirect, which is not followed."
GENERIC_FAILURE = "The webhook request failed."


def url_problem(text: str) -> str | None:
    """Why the text cannot be the webhook address, or None when it can.

    Only plain ``http`` and ``https`` addresses with a host are accepted, and an address that
    carries a user name or password is refused: the shared secret is how the receiver trusts a
    document, and a credential in the address would end up in logs along the way.
    """
    if len(text) > MAX_URL_LENGTH:
        return f"Use at most {MAX_URL_LENGTH} characters."
    if not text.isascii() or not text.isprintable() or any(char.isspace() for char in text):
        return "Use a plain address with no spaces or special characters."
    try:
        parts = urlsplit(text)
        _ = parts.port  # a port out of range raises here
    except ValueError:
        return "Enter a valid address such as https://chat.example.com/hooks/env."
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return "Only http and https addresses are accepted."
    if not parts.hostname:
        return "Enter an address that names a host."
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "Do not put a user name or password in the address. Use the shared secret."
    return None


def secret_problem(secret: str) -> str | None:
    """Why the text cannot be the shared secret, or None when it can."""
    if len(secret) > MAX_SECRET_LENGTH or not secret.isprintable():
        return f"Use printable characters, at most {MAX_SECRET_LENGTH}."
    if len(secret) < MIN_SECRET_LENGTH:
        return f"Use at least {MIN_SECRET_LENGTH} characters."
    return None


def host_of(url: str) -> str:
    """The host and port of an address, for audit entries and logs: never the path or query."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return ""
    return f"[{host}]{port}" if ":" in host else f"{host}{port}"


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The signature header value: ``sha256=`` and the hex HMAC of ``timestamp.body``."""
    message = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def signed_headers(secret: str, body: bytes, unix_time: int) -> dict[str, str]:
    """Every header of one attempt: content type, user agent, timestamp and signature."""
    timestamp = str(unix_time)
    return {
        "Content-Type": "application/json",
        "User-Agent": f"ENV-Manager/{VERSION}",
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(secret, timestamp, body),
    }


class WebhookFailure(Exception):
    """A document was not delivered. ``reason`` is a fixed sentence that is safe to show and log.

    ``status`` is the HTTP status of the answer, None when there was none; ``retryable`` says
    whether the same document could be accepted later.
    """

    def __init__(self, reason: str, *, retryable: bool, status: int | None = None) -> None:
        """Hold the reason, whether trying again can help and the status."""
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.status = status


def failure_for_status(status: int) -> WebhookFailure:
    """The failure for an answer that is not a success."""
    if 300 <= status < 400:
        return WebhookFailure(REDIRECTED, retryable=False, status=status)
    if status in RETRYABLE_STATUSES or status >= 500:
        return WebhookFailure(
            f"The webhook answered with an error (HTTP {status}).", retryable=True, status=status
        )
    return WebhookFailure(
        f"The webhook refused the request (HTTP {status}).", retryable=False, status=status
    )


class WebhookTransport(Protocol):
    """Posts one signed document to an address."""

    async def post(self, url: str, headers: Mapping[str, str], body: bytes) -> int:
        """Post the body and return the HTTP status of a success; raise ``WebhookFailure``."""
        ...


def _tls_error(error: BaseException) -> bool:
    """Whether the error, or what caused it, is a TLS failure."""
    seen: BaseException | None = error
    while seen is not None:
        if isinstance(seen, ssl.SSLError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class HttpxWebhookTransport:
    """The transport on httpx: short timeouts, no redirects, and the answer's body never read."""

    def __init__(self, timeout: httpx.Timeout = REQUEST_TIMEOUT) -> None:
        """Use the given timeouts, by default the short ones of ``REQUEST_TIMEOUT``."""
        self.timeout = timeout

    async def post(self, url: str, headers: Mapping[str, str], body: bytes) -> int:
        """Post the document; a status outside 200 to 299 or any network error is a failure."""
        try:
            async with (
                httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client,
                client.stream("POST", url, content=body, headers=dict(headers)) as response,
            ):
                status = response.status_code
        except httpx.TimeoutException:
            raise WebhookFailure(TIMED_OUT, retryable=True) from None
        except httpx.ConnectError as error:
            if _tls_error(error):
                raise WebhookFailure(TLS_FAILED, retryable=False) from None
            raise WebhookFailure(UNREACHABLE, retryable=True) from None
        except httpx.TransportError:
            raise WebhookFailure(UNREACHABLE, retryable=True) from None
        except httpx.HTTPError, httpx.InvalidURL, ValueError:
            raise WebhookFailure(GENERIC_FAILURE, retryable=False) from None
        if 200 <= status < 300:
            return status
        raise failure_for_status(status)
