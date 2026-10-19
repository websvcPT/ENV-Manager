"""The observability self-test: one test event and one test span, and what became of them.

Each test says whether its destination is configured, where it went (the host name only, never an
address with credentials, a path or a header) and whether the other end really took it. Nothing is
reported as sent on the strength of a call that returned: the Sentry result is the HTTP status the
transport saw, and the span result is the answer of the OTLP exporter, which is a failure for an
unreachable collector or a refused request. A failure is described in fixed words; the text of an
exception or of the collector's answer is never kept. The tests use their own client and provider,
so they never touch the global ones of a running application.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import sentry_sdk
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sentry_sdk.transport import HttpTransport
from sentry_sdk.utils import BadDsn

from app import VERSION
from app.config import Settings
from app.core.observability import (
    SERVICE_NAME,
    endpoint_host,
    otel_enabled,
    otlp_endpoint,
    sentry_options,
)
from app.core.telemetry import TRACER_NAME

DEFAULT_TIMEOUT_SECONDS = 10.0
TEST_MESSAGE = "ENV Manager observability test"
TEST_SPAN = "envmanager.observability.test"
ERROR_REPORTING = "Error reporting"
TRACING = "OpenTelemetry"


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one test: configured or not, delivered or not, where, and a sentence."""

    name: str
    configured: bool
    delivered: bool
    host: str | None
    message: str


def _not_configured(name: str, reason: str) -> CheckResult:
    """A test of something the operator did not set up: nothing is sent."""
    return CheckResult(name, False, False, None, f"not configured, nothing sent ({reason})")


class _ObservedTransport(HttpTransport):
    """The SDK's HTTP transport, remembering how the delivery of the event ended."""

    def __init__(self, options: dict[str, Any]) -> None:
        """Start with no answer and no drop recorded."""
        super().__init__(options)
        self.status: int | None = None
        self.dropped: str | None = None

    def _handle_response(self, response: Any, envelope: Any) -> None:
        """Keep the status the server answered an event with."""
        if envelope is not None and any(item.data_category == "error" for item in envelope.items):
            self.status = response.status
        super()._handle_response(response, envelope)

    def on_dropped_event(self, reason: str) -> None:
        """Keep the first reason the transport gave up on something."""
        self.dropped = self.dropped or reason
        super().on_dropped_event(reason)


def _sentry_outcome(
    host: str | None, event_id: str | None, transport: _ObservedTransport | None, timeout: float
) -> CheckResult:
    """The result of a test event from what the transport saw."""
    if event_id is None:
        dropped = "the event was dropped before sending"
        return CheckResult(ERROR_REPORTING, True, False, host, dropped)
    if transport is None:
        return CheckResult(ERROR_REPORTING, True, False, host, "no transport is available")
    status = transport.status
    if status is not None and 200 <= status < 300:
        return CheckResult(
            ERROR_REPORTING,
            True,
            True,
            host,
            f"test event {event_id} accepted by {host} (HTTP {status})",
        )
    if status is not None:
        return CheckResult(
            ERROR_REPORTING, True, False, host, f"the server at {host} answered HTTP {status}"
        )
    if transport.dropped is not None:
        reason = transport.dropped
        return CheckResult(
            ERROR_REPORTING, True, False, host, f"the event could not be delivered ({reason})"
        )
    return CheckResult(
        ERROR_REPORTING, True, False, host, f"no answer from {host} within {timeout:g} seconds"
    )


def check_error_reporting(
    settings: Settings,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client_options: Mapping[str, Any] | None = None,
) -> CheckResult:
    """Send one test event to Sentry and report what the server answered.

    ``client_options`` replaces client options, for a test that needs a quiet client.
    """
    if settings.sentry_dsn is None:
        return _not_configured(ERROR_REPORTING, "ENVMANAGER_SENTRY_DSN is not set")
    host = endpoint_host(settings.sentry_dsn.get_secret_value())
    options = {
        **sentry_options(settings),
        "transport": _ObservedTransport,
        **(client_options or {}),
    }
    try:
        client = sentry_sdk.Client(**options)
    except BadDsn:
        return CheckResult(ERROR_REPORTING, True, False, host, "the DSN is not valid")
    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_client(client)
            event_id = scope.capture_message(TEST_MESSAGE, level="info")
        client.flush(timeout=timeout_seconds)
        transport = client.transport
        observed = transport if isinstance(transport, _ObservedTransport) else None
        return _sentry_outcome(host, event_id, observed, timeout_seconds)
    finally:
        client.close(timeout=1.0)


@contextmanager
def _quiet_exporter_logs() -> Iterator[None]:
    """Keep the exporter's own log lines, which can repeat the collector's answer, out."""
    logger = logging.getLogger("opentelemetry")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _finished_test_span(settings: Settings) -> tuple[Any, str]:
    """Finish one test span on a private provider and return it with its trace id."""
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": VERSION,
            "deployment.environment.name": settings.environment,
        }
    )
    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer(TRACER_NAME, VERSION)
    with tracer.start_as_current_span(TEST_SPAN, attributes={"envmanager.check": "test"}) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
    return memory.get_finished_spans(), trace_id


def check_tracing(
    settings: Settings,
    environ: Mapping[str, str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> CheckResult:
    """Export one test span to the OTLP traces endpoint and report the exporter's own result.

    The destination is the one FastAPI's automatic export uses, so a pass here means the
    application's spans will be accepted too. ``timeout_seconds`` bounds the request and its
    retries.
    """
    if not otel_enabled(environ):
        return _not_configured(TRACING, "FASTAPI_OTEL_AUTO_CONFIGURE is not true")
    endpoint = otlp_endpoint(environ, "TRACES")
    if endpoint is None:
        return _not_configured(TRACING, "no OTLP traces endpoint is set or traces are switched off")
    host = endpoint_host(endpoint)
    if urlsplit(endpoint).scheme not in {"http", "https"} or host is None:
        return CheckResult(
            TRACING, True, False, host, "the traces endpoint is not an http or https address"
        )
    spans, trace_id = _finished_test_span(settings)
    exporter = OTLPSpanExporter(endpoint=endpoint, timeout=timeout_seconds)
    try:
        with _quiet_exporter_logs():
            outcome = exporter.export(spans)
    except Exception:
        outcome = SpanExportResult.FAILURE
    finally:
        exporter.shutdown()  # type: ignore[no-untyped-call]
    if outcome is SpanExportResult.SUCCESS:
        return CheckResult(
            TRACING, True, True, host, f"test span {trace_id} exported to {host} and accepted"
        )
    return CheckResult(
        TRACING,
        True,
        False,
        host,
        f"the span was not accepted by {host} (unreachable, refused or too slow)",
    )
