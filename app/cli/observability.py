"""The ``observability`` commands: confirm that error reporting and tracing work."""

import os
from typing import Annotated

import typer

from app.config import get_settings
from app.core.observability_check import (
    DEFAULT_TIMEOUT_SECONDS,
    CheckResult,
    check_error_reporting,
    check_tracing,
)

observability_app = typer.Typer(help="Check error reporting and tracing.", no_args_is_help=True)


def _line(result: CheckResult) -> str:
    """The report line of one test."""
    return f"{result.name}: {result.message}"


@observability_app.command("test")
def test_observability(
    timeout: Annotated[
        float, typer.Option(min=1.0, max=120.0, help="Seconds to wait for each destination.")
    ] = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Send a test event to Sentry and a test span to the OpenTelemetry endpoint.

    Each destination is tested only when it is configured, and is reported by host name. A test
    counts as sent only when the other end accepted it; the command exits with code 1 when a
    configured destination did not.
    """
    settings = get_settings()
    results = (
        check_error_reporting(settings, timeout_seconds=timeout),
        check_tracing(settings, os.environ, timeout_seconds=timeout),
    )
    for result in results:
        typer.echo(_line(result))
    if any(result.configured and not result.delivered for result in results):
        raise typer.Exit(code=1)
