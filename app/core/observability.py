"""Error reporting (Sentry) and tracing (OpenTelemetry) setup. Both are off unless configured."""

import os
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

import sentry_sdk
from fastapi.telemetry import TelemetryConfig
from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint

from app import VERSION
from app.config import Settings
from app.core.errors import ExpectedOutcome
from app.core.log import get_logger
from app.core.telemetry_logs import scrubbing_logger_provider

log = get_logger("observability")

FILTERED = "[Filtered]"
SERVICE_NAME = "env-manager"
OTLP_SIGNALS = ("TRACES", "METRICS", "LOGS")

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


DATA_COLLECTION: dict[str, Any] = {
    "user_info": False,
    "cookies": {"mode": "off"},
    "http_headers": {"request": {"mode": "off"}},
    "http_bodies": [],
    "url_query_params": {"mode": "off"},
    "graphql": {"document": False, "variables": False},
    "gen_ai": {"inputs": False, "outputs": False},
    "database_query_data": False,
    "queues": False,
    "stack_frame_variables": False,
}


def sentry_options(settings: Settings) -> dict[str, Any]:
    """The Sentry client options: no personal data, no variables, no bodies, no tracing.

    ``data_collection`` replaces the deprecated ``send_default_pii``: it collects no user
    identity, cookies, request headers, query strings, request bodies, SQL data, queue data or
    stack frame variables. The SDK's own scrubber is not used when it is set, so ``before_send``
    and ``before_breadcrumb`` carry that work. The older body and variable options stay as a
    second guard for the integrations that still read them.
    """
    dsn = settings.sentry_dsn
    return {
        "dsn": dsn.get_secret_value() if dsn is not None else None,
        "release": VERSION,
        "environment": settings.environment,
        "data_collection": deepcopy(DATA_COLLECTION),
        "include_local_variables": False,
        "max_request_body_size": "never",
        "traces_sample_rate": 0,
        "before_send": scrub_event,
        "before_breadcrumb": scrub_breadcrumb,
    }


def init_sentry(settings: Settings) -> bool:
    """Initialise Sentry when a DSN is configured. Returns whether it was enabled."""
    if settings.sentry_dsn is None:
        return False
    sentry_sdk.init(**sentry_options(settings))
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


def otlp_endpoint(environ: Mapping[str, str], signal: str) -> str | None:
    """The address FastAPI's automatic export sends a signal to, None when it sends none.

    ``signal`` is ``TRACES``, ``METRICS`` or ``LOGS``. The address of the signal's own variable
    is used as it stands; the general variable gets the signal's path added. Nothing is exported
    when the SDK is disabled, the signal's exporter is set to ``none`` (or to anything but OTLP,
    which FastAPI does not set up) or no address is given.
    """
    if environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    exporter = (environ.get(f"OTEL_{signal}_EXPORTER") or "otlp").strip().lower()
    if exporter != "otlp":
        return None
    own = environ.get(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT", "").strip()
    if own:
        return own
    general = environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return f"{general.removesuffix('/')}/v1/{signal.lower()}" if general else None


def endpoint_host(endpoint: str) -> str | None:
    """The host name of an address, and nothing else of it.

    Credentials, port, path, query and fragment are never returned; an address that cannot be
    read gives None.
    """
    try:
        host = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}").hostname
    except ValueError:
        return None
    return host or None


def otel_endpoint_host(environ: Mapping[str, str]) -> str | None:
    """The host name of the OTLP endpoint that traces go to, else metrics, else logs.

    Credentials, port, path and query of the address are never returned.
    """
    for signal in OTLP_SIGNALS:
        endpoint = otlp_endpoint(environ, signal)
        if endpoint is not None:
            return endpoint_host(endpoint)
    return None


def fastapi_telemetry(environ: Mapping[str, str]) -> TelemetryConfig:
    """The ``telemetry`` argument of the application.

    Nothing is changed unless logs are exported: FastAPI's own setup then finds the provider that
    scrubs the exception text and stack of its logs before any exporter sees them, and adds the
    OTLP exporter after it. Without a logs destination the argument stays empty, so FastAPI keeps
    its defaults and skips telemetry altogether while nothing is configured.
    """
    config: TelemetryConfig = {}
    if otel_enabled(environ) and otlp_endpoint(environ, "LOGS") is not None:
        config["logger_provider"] = scrubbing_logger_provider()
    return config
