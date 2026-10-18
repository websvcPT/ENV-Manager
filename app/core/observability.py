"""Error reporting (Sentry) and tracing (OpenTelemetry) setup. Both are off unless configured."""

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import sentry_sdk
from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint

from app import VERSION
from app.config import Settings
from app.core.errors import ExpectedOutcome
from app.core.log import get_logger

log = get_logger("observability")

FILTERED = "[Filtered]"
SERVICE_NAME = "env-manager"

# A key is scrubbed when its lower-cased name contains any of these fragments.
SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "session",
    "csrf",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "dsn",
    "env_value",
    "content",
    "parameters",
    "params",
    "body",
)
# Sub-trees dropped entirely: request bodies and captured frame variables.
DROPPED_KEYS = frozenset({"data", "vars", "cookies"})


def _is_sensitive(key: str) -> bool:
    """Return True when a mapping key names a secret-bearing field."""
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def _scrub(node: Any) -> Any:
    """Return a copy of a nested structure with secret-bearing fields filtered."""
    if isinstance(node, Mapping):
        cleaned: dict[Any, Any] = {}
        for key, value in node.items():
            if isinstance(key, str) and (_is_sensitive(key) or key in DROPPED_KEYS):
                cleaned[key] = FILTERED
            else:
                cleaned[key] = _scrub(value)
        return cleaned
    if isinstance(node, list | tuple):
        return [_scrub(item) for item in node]
    return node


def _scrub_header_pairs(event: Any) -> None:
    """Filter sensitive headers when the request headers are stored as name/value pairs."""
    request = event.get("request")
    if not isinstance(request, dict):
        return
    headers = request.get("headers")
    if isinstance(headers, list):
        request["headers"] = [
            [pair[0], FILTERED] if len(pair) == 2 and _is_sensitive(str(pair[0])) else pair
            for pair in headers
        ]


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """Remove secrets from an event, or drop it when it reports an expected outcome."""
    exc_info = hint.get("exc_info")
    if exc_info and isinstance(exc_info[1], ExpectedOutcome):
        return None
    _scrub_header_pairs(event)
    scrubbed: Event = _scrub(event)
    request: Any = scrubbed.get("request")
    if isinstance(request, dict):
        request.pop("query_string", None)
    return scrubbed


def scrub_breadcrumb(crumb: Breadcrumb, hint: BreadcrumbHint) -> Breadcrumb | None:
    """Remove SQL parameters, HTTP bodies and sensitive fields from a breadcrumb."""
    scrubbed: Breadcrumb = _scrub(crumb)
    return scrubbed


def init_sentry(settings: Settings) -> bool:
    """Initialise Sentry when a DSN is configured. Returns whether it was enabled."""
    if settings.sentry_dsn is None:
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        release=VERSION,
        environment=settings.environment,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=0,
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )
    log.info("error reporting enabled")
    return True


def otel_resource_environment(environment_name: str, current: Mapping[str, str]) -> dict[str, str]:
    """Return the OpenTelemetry resource variables that are not already set.

    The service name defaults to ``env-manager``; the app version and deployment environment
    are added as resource attributes unless the operator provided their own attributes.
    """
    values: dict[str, str] = {}
    if "OTEL_SERVICE_NAME" not in current:
        values["OTEL_SERVICE_NAME"] = SERVICE_NAME
    if "OTEL_RESOURCE_ATTRIBUTES" not in current:
        values["OTEL_RESOURCE_ATTRIBUTES"] = (
            f"service.version={VERSION},deployment.environment.name={environment_name}"
        )
    return values


def configure_otel(settings: Settings) -> bool:
    """Prepare OpenTelemetry resource variables. Export stays off unless the operator enables it.

    FastAPI exports only when ``FASTAPI_OTEL_AUTO_CONFIGURE=true`` and an OTLP endpoint is set
    in the standard variables. Returns whether auto configuration is enabled.
    """
    for name, value in otel_resource_environment(settings.environment, os.environ).items():
        os.environ[name] = value
    enabled = otel_enabled(os.environ)
    if enabled:
        log.info("OpenTelemetry export enabled")
    return enabled


def otel_enabled(environ: Mapping[str, str]) -> bool:
    """Whether FastAPI's OpenTelemetry export is switched on in the environment."""
    return environ.get("FASTAPI_OTEL_AUTO_CONFIGURE", "").lower() == "true"


def otel_endpoint_host(environ: Mapping[str, str]) -> str | None:
    """The host name of the configured OTLP endpoint, and nothing else of the address.

    The standard endpoint variable is read first, then the one for traces, metrics and logs.
    Credentials, port, path and query of the address are never returned.
    """
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        value = environ.get(name, "").strip()
        if not value:
            continue
        try:
            host = urlsplit(value if "//" in value else f"//{value}").hostname
        except ValueError:
            return None
        return host or None
    return None
