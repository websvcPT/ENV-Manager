"""The read-only diagnostics of the General settings screen.

Everything here is set at startup, so the screen can only show it. Nothing secret is read: error
reporting is shown as on or off (never its address), the OpenTelemetry endpoint as a host name
only, and the network lists are the ranges the operator configured. The client address is the
one the application resolved for the request that asks.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app import VERSION
from app.config import Settings
from app.core.observability import otel_enabled, otel_endpoint_host


@dataclass(frozen=True)
class Diagnostics:
    """What an operator needs to check a deployment without reading its environment."""

    error_reporting: bool
    tracing: bool
    tracing_host: str | None
    version: str
    environment: str
    trusted_proxies: tuple[str, ...]
    allowed_sources: tuple[str, ...]
    client_address: str | None


def collect_diagnostics(
    settings: Settings, environ: Mapping[str, str], client_address: str | None
) -> Diagnostics:
    """The diagnostics for the startup settings, the process environment and this request."""
    return Diagnostics(
        error_reporting=settings.sentry_dsn is not None,
        tracing=otel_enabled(environ),
        tracing_host=otel_endpoint_host(environ),
        version=VERSION,
        environment=settings.environment,
        trusted_proxies=tuple(str(network) for network in settings.trusted_proxies),
        allowed_sources=tuple(str(network) for network in settings.allowed_sources),
        client_address=client_address,
    )
