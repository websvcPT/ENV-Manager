"""Custom spans and the current trace id.

Spans come from one tracer that binds to whatever tracer provider the process has. While
OpenTelemetry export is not configured the provider is the API's no-op proxy, so a span is a
shared non-recording object and the only cost is the call. Every attribute goes through a fixed
vocabulary of ids, names, counts and results: a key outside ``ATTRIBUTES`` or a value that is not
a short scalar is dropped, so nothing that holds a value, a path, a command, its output or a token
can be attached by mistake. A span never records the exception that ended it (the SDK would add
its message and stack, which can hold a value); it keeps the exception class name only.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TypeGuard

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.trace import Span, StatusCode

from app import VERSION

TRACER_NAME = "env-manager"
MAX_TEXT_LENGTH = 100
REDACTED = "[redacted]"
REDACTED_ATTRIBUTES = ("url.path", "url.query")

PROJECT_ID = "envmanager.project.id"
DEPLOYMENT_ID = "envmanager.deployment.id"
DEPLOYMENT_NAME = "envmanager.deployment.name"
SERVICE_ID = "envmanager.service.id"
SERVICE_NAME = "envmanager.service.name"
ENVIRONMENT_ID = "envmanager.environment.id"
ENVIRONMENT_NAME = "envmanager.environment.name"
ENVIRONMENT_COUNT = "envmanager.environment.count"
ACTOR_TYPE = "envmanager.actor.type"
DRY_RUN = "envmanager.dry_run"
FORCED = "envmanager.forced"
RESULT = "envmanager.result"
DRIFT_STATE = "envmanager.drift.state"
DIST_OUTCOME = "envmanager.dist.outcome"
DIST_PROBLEM = "envmanager.dist.problem"
EXIT_CODE = "envmanager.exit_code"
TRIGGER = "envmanager.trigger"
BACKUP_ID = "envmanager.backup.id"
BACKUP_PRUNED = "envmanager.backup.pruned"
ATTEMPT = "envmanager.attempt"
ATTEMPTS = "envmanager.attempts"
SKIP_REASON = "envmanager.skip_reason"
FAILURE_PROBLEM = "envmanager.failure.problem"
RETRYABLE = "envmanager.retryable"
SMTP_TLS_MODE = "envmanager.smtp.tls_mode"
HTTP_STATUS = "http.response.status_code"
ERROR_TYPE = "error.type"

ATTRIBUTES = frozenset(
    {
        PROJECT_ID,
        DEPLOYMENT_ID,
        DEPLOYMENT_NAME,
        SERVICE_ID,
        SERVICE_NAME,
        ENVIRONMENT_ID,
        ENVIRONMENT_NAME,
        ENVIRONMENT_COUNT,
        ACTOR_TYPE,
        DRY_RUN,
        FORCED,
        RESULT,
        DRIFT_STATE,
        DIST_OUTCOME,
        DIST_PROBLEM,
        EXIT_CODE,
        TRIGGER,
        BACKUP_ID,
        BACKUP_PRUNED,
        ATTEMPT,
        ATTEMPTS,
        SKIP_REASON,
        FAILURE_PROBLEM,
        RETRYABLE,
        SMTP_TLS_MODE,
        HTTP_STATUS,
        ERROR_TYPE,
    }
)

DEPLOY_SPAN = "envmanager.deploy"
DIST_CHECK_SPAN = "envmanager.dist_check"
DRIFT_CHECK_SPAN = "envmanager.drift_check"
POST_DEPLOY_SPAN = "envmanager.post_deploy"
BACKUP_SPAN = "envmanager.backup"
GIT_PUSH_SPAN = "envmanager.git_push"
SMTP_SEND_SPAN = "envmanager.smtp.send"
WEBHOOK_DELIVER_SPAN = "envmanager.webhook.deliver"

Scalar = str | int | float | bool

_tracer = trace.get_tracer(TRACER_NAME, VERSION)


def _is_scalar(value: object) -> TypeGuard[Scalar]:
    """Whether a value is a number, a boolean or a short text."""
    if isinstance(value, str):
        return len(value) <= MAX_TEXT_LENGTH
    return isinstance(value, bool | int | float)


def _allowed(attributes: Mapping[str, object]) -> dict[str, Scalar]:
    """The attributes whose key is in the vocabulary and whose value is a short scalar."""
    return {
        key: value for key, value in attributes.items() if key in ATTRIBUTES and _is_scalar(value)
    }


def annotate(span: Span, attributes: Mapping[str, object]) -> None:
    """Add attributes to a span, dropping anything outside the vocabulary."""
    if span.is_recording():
        span.set_attributes(_allowed(attributes))


def error_type_name(error: BaseException) -> str:
    """The qualified class name of an exception, never its message."""
    kind = type(error)
    if kind.__module__ == "builtins":
        return kind.__qualname__
    return f"{kind.__module__}.{kind.__qualname__}"


@contextmanager
def span(name: str, attributes: Mapping[str, object] | None = None) -> Iterator[Span]:
    """Run a block inside a new current span.

    An exception that leaves the block marks the span as an error with the exception class name
    and propagates unchanged.
    """
    with _tracer.start_as_current_span(
        name,
        attributes=_allowed(attributes) if attributes else None,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException as error:
            if current.is_recording():
                current.set_attribute(ERROR_TYPE, error_type_name(error))
                current.set_status(StatusCode.ERROR)
            raise


class RedactRequestSpans(SpanProcessor):
    """Keeps the raw path and the query string of a request out of every span.

    FastAPI records ``url.path`` and ``url.query`` when it starts the request span. A path can
    hold a key name (the reveal routes) and the query string can hold search text and a host path
    being typed, so both are replaced before any exporter sees the span. The route template is
    set by FastAPI as ``http.route`` once the route is known and stays on the span, so a trace
    still tells which screen was served. The attributes cannot be changed after the span ends,
    which is why this happens when it starts.
    """

    def on_start(self, span: SdkSpan, parent_context: Context | None = None) -> None:
        """Overwrite the path and query attributes the span was created with."""
        attributes = span.attributes or {}
        for key in REDACTED_ATTRIBUTES:
            if key in attributes:
                span.set_attribute(key, REDACTED)


def redacting_tracer_provider() -> TracerProvider:
    """A tracer provider whose first processor redacts the request path and query.

    The resource, the sampler and the id generator come from the standard OpenTelemetry
    variables as they do for any provider. The exporter FastAPI adds later runs after the
    redaction, in registration order.
    """
    provider = TracerProvider()
    provider.add_span_processor(RedactRequestSpans())
    return provider


def current_trace_id() -> str | None:
    """The trace id (32 hex characters) of the span being recorded, or None.

    Only a span that records counts. A request that carries a ``traceparent`` header while no
    tracer provider is configured never gets a span, and a remote parent that was not sampled
    gives a valid context that nothing records, so an audit entry never holds an id that has no
    trace behind it.
    """
    current = trace.get_current_span()
    context = current.get_span_context()
    if not current.is_recording() or not context.is_valid:
        return None
    return format(context.trace_id, "032x")
