"""Tests for the webhook transport pieces: the address rules, the signature, and one real post."""

import ssl

import httpx
import pytest

from app.services.app_settings import url_origin
from app.services.webhook import (
    HttpxWebhookTransport,
    WebhookFailure,
    _tls_error,
    failure_for_status,
    host_of,
    secret_problem,
    sign,
    signed_headers,
    url_problem,
)
from app.tests.webhook_helpers import Receiver, reference_signature

SECRET = "0123456789abcdef-secret"


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "https://hooks.example.com/services/T1/B2/token?a=b#frag",
        "https://hooks.example.com:8443/x",
        "HTTPS://Hooks.Example.com/x",
        "http://127.0.0.1:8080/hook",
        "http://[::1]:8080/hook",
        "https://env-receiver/x",
    ],
)
def test_http_and_https_addresses_with_a_host_are_accepted(url: str) -> None:
    """Plain http and https addresses are fine, whatever the host or port."""
    assert url_problem(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://hooks.example.com/x", "Only http and https"),
        ("file:///etc/passwd", "Only http and https"),
        ("javascript:alert(1)", "Only http and https"),
        ("gopher://hooks.example.com/", "Only http and https"),
        ("hooks.example.com/x", "Only http and https"),
        ("//hooks.example.com/x", "Only http and https"),
        ("https://", "names a host"),
        ("https:///path", "names a host"),
        ("https://user@hooks.example.com/x", "user name or password"),
        ("https://user:pass@hooks.example.com/x", "user name or password"),
        ("https://:pass@hooks.example.com/x", "user name or password"),
        ("https://hooks.example.com@evil.example.org/x", "user name or password"),
        ("https://hooks.example.com:99999/x", "valid address"),
        ("https://hooks.example.com:abc/x", "valid address"),
        ("https://hooks example.com/x", "no spaces"),
        ("https://hooks.example.com/x\n", "no spaces"),
        ("https://hooks.example.com/é", "no spaces or special"),
        ("https://hooks.example.com/" + "a" * 2100, "at most 2048"),
    ],
)
def test_other_schemes_credentials_and_unusable_text_are_refused(url: str, reason: str) -> None:
    """Only http and https, a host, no credentials, no spaces and a bounded length."""
    problem = url_problem(url)
    assert problem is not None and reason in problem


def test_the_secret_needs_sixteen_printable_characters() -> None:
    """Short and unprintable secrets are refused, long ones are bounded."""
    assert secret_problem(SECRET) is None
    assert "at least 16" in (secret_problem("short") or "")
    assert secret_problem("a" * 15) is not None and secret_problem("a" * 16) is None
    assert secret_problem("tab\tinside-a-secret") is not None
    assert secret_problem("a" * 1025) is not None


def test_the_signature_is_the_reference_hmac_of_the_timestamp_a_dot_and_the_body() -> None:
    """The scheme a receiver implements from the documentation gives the same value."""
    body = '{"event":"deploy.finished","note":"café"}'.encode()
    assert sign(SECRET, "1760000000", body) == reference_signature(SECRET, "1760000000", body)
    assert sign(SECRET, "1760000000", body).startswith("sha256=")
    assert len(sign(SECRET, "1760000000", body)) == len("sha256=") + 64
    assert sign(SECRET, "1760000001", body) != sign(SECRET, "1760000000", body)
    assert sign(SECRET + "x", "1760000000", body) != sign(SECRET, "1760000000", body)
    assert sign(SECRET, "1760000000", body + b" ") != sign(SECRET, "1760000000", body)


def test_every_header_of_an_attempt_is_listed_and_the_secret_is_not_among_them() -> None:
    """Content type, agent, timestamp and signature; the secret never travels."""
    headers = signed_headers(SECRET, b"{}", 1760000000)
    assert set(headers) == {
        "Content-Type",
        "User-Agent",
        "X-EnvManager-Timestamp",
        "X-EnvManager-Signature",
    }
    assert headers["Content-Type"] == "application/json"
    assert headers["X-EnvManager-Timestamp"] == "1760000000"
    assert headers["User-Agent"].startswith("ENV-Manager/")
    assert SECRET not in "".join(headers.values())


@pytest.mark.parametrize(
    ("status", "retryable", "words"),
    [
        (301, False, "redirect"),
        (302, False, "redirect"),
        (307, False, "redirect"),
        (400, False, "refused the request (HTTP 400)"),
        (401, False, "refused the request (HTTP 401)"),
        (404, False, "refused the request (HTTP 404)"),
        (410, False, "refused the request (HTTP 410)"),
        (408, True, "error (HTTP 408)"),
        (429, True, "error (HTTP 429)"),
        (500, True, "error (HTTP 500)"),
        (503, True, "error (HTTP 503)"),
    ],
)
def test_a_status_is_retryable_only_when_waiting_can_help(
    status: int, retryable: bool, words: str
) -> None:
    """Redirects and client errors are final; timeouts, throttling and server errors retry."""
    failure = failure_for_status(status)
    assert failure.retryable is retryable and failure.status == status
    assert words in failure.reason


def test_the_host_for_the_audit_never_includes_the_path_query_or_credentials() -> None:
    """Only the host and port are recorded, so a token in the path stays out."""
    assert host_of("https://hooks.example.com/services/T1/B2/secret?k=v") == "hooks.example.com"
    assert host_of("https://hooks.example.com:8443/x") == "hooks.example.com:8443"
    assert host_of("http://[::1]:8080/x") == "[::1]:8080"
    assert host_of("https://u:p@hooks.example.com/x") == "hooks.example.com"
    assert host_of("not an address") == ""


def test_the_origin_keeps_the_scheme_host_and_port_only() -> None:
    """What the settings audit stores for the address."""
    assert url_origin("https://hooks.example.com:8443/services/T1/B2/token?k=v") == (
        "https://hooks.example.com:8443"
    )
    assert url_origin("http://[::1]:8080/x") == "http://[::1]:8080"
    assert url_origin("") == "" and url_origin(None) == ""


def test_a_tls_failure_is_recognised_through_its_causes() -> None:
    """The library wraps the certificate error; it is still found."""
    certificate = ssl.SSLCertVerificationError("certificate verify failed")
    wrapped = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] https://secret.example/token")
    wrapped.__cause__ = certificate
    assert _tls_error(wrapped)
    assert not _tls_error(httpx.ConnectError("Connection refused"))


async def test_a_document_is_posted_with_the_signed_headers_and_the_exact_body() -> None:
    """A real receiver reads the bytes the signature was made over."""
    body = b'{"event":"deploy.finished"}'
    async with Receiver(200) as receiver:
        status = await HttpxWebhookTransport().post(
            receiver.url, signed_headers(SECRET, body, 1760000000), body
        )
    assert status == 200
    (request,) = receiver.requests
    assert request.method == "POST" and request.path == "/hook?token=url-token-91c3"
    assert request.body == body
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-EnvManager-Timestamp"] == "1760000000"
    assert request.headers["X-EnvManager-Signature"] == reference_signature(
        SECRET, "1760000000", request.body
    )


@pytest.mark.parametrize(
    ("status", "retryable"), [(404, False), (500, True), (429, True), (401, False)]
)
async def test_a_refusing_receiver_is_a_failure_with_its_status(
    status: int, retryable: bool
) -> None:
    """The answer's status decides whether to try again."""
    async with Receiver(status) as receiver:
        with pytest.raises(WebhookFailure) as caught:
            await HttpxWebhookTransport().post(receiver.url, {}, b"{}")
    assert caught.value.status == status and caught.value.retryable is retryable


async def test_a_redirect_is_not_followed_and_is_final() -> None:
    """The second address is never asked, and trying again is pointless."""
    async with Receiver(200) as target, Receiver(302, location=target.url) as redirecting:
        with pytest.raises(WebhookFailure) as caught:
            await HttpxWebhookTransport().post(redirecting.url, {}, b"{}")
        assert target.requests == []
    assert caught.value.status == 302 and not caught.value.retryable
    assert "redirect" in caught.value.reason


async def test_a_receiver_that_does_not_answer_in_time_is_a_retryable_timeout() -> None:
    """The timeout is short and the failure carries fixed words, not the library's text."""
    async with Receiver(200, delay=1.0) as receiver:
        transport = HttpxWebhookTransport(httpx.Timeout(0.2))
        with pytest.raises(WebhookFailure) as caught:
            await transport.post(receiver.url, {}, b"{}")
    assert caught.value.retryable and caught.value.status is None
    assert caught.value.reason == "The webhook did not answer in time."


async def test_a_port_nobody_listens_on_is_unreachable_and_the_address_is_not_repeated() -> None:
    """A refused connection is retryable; the reason holds no part of the address."""
    async with Receiver() as receiver:
        url = receiver.url
    with pytest.raises(WebhookFailure) as caught:
        await HttpxWebhookTransport().post(url, {}, b"{}")
    assert caught.value.retryable
    assert caught.value.reason == "The webhook could not be reached."
    assert "url-token-91c3" not in caught.value.reason and "127.0.0.1" not in caught.value.reason
