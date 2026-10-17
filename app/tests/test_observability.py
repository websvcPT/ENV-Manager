"""Error reporting scrubbing, initialisation and OpenTelemetry resource setup tests."""

from typing import Any

import pytest
import sentry_sdk
from pydantic import SecretStr

from app import VERSION
from app.config import Settings
from app.core.errors import ExpectedOutcome
from app.core.observability import (
    FILTERED,
    configure_otel,
    init_sentry,
    otel_resource_environment,
    scrub_breadcrumb,
    scrub_event,
)

SECRET_VALUE = "hunter2-database-password"


class BlockedDeploy(ExpectedOutcome):
    """Stand-in for an expected deploy outcome."""


def sample_event() -> Any:
    """An event shaped like one the SDK would send for a failed request."""
    return {
        "message": "boom",
        "request": {
            "url": "http://app/environments/1",
            "method": "POST",
            "query_string": f"token={SECRET_VALUE}",
            "data": {"DB_PASSWORD": SECRET_VALUE},
            "cookies": {"envmanager_session": "abc"},
            "headers": {
                "Authorization": f"Bearer {SECRET_VALUE}",
                "Cookie": "envmanager_session=abc",
                "Host": "app",
                "X-CSRFToken": "csrf",
            },
        },
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "stacktrace": {
                        "frames": [{"function": "deploy", "vars": {"value": SECRET_VALUE}}]
                    },
                }
            ]
        },
        "extra": {"DB_PASSWORD": SECRET_VALUE, "environment_id": 4},
    }


def test_scrub_event_removes_secrets() -> None:
    """Headers, cookies, bodies, frame variables and secret-named fields are filtered."""
    cleaned: Any = scrub_event(sample_event(), {})
    assert cleaned is not None
    assert SECRET_VALUE not in repr(cleaned)
    headers = cleaned["request"]["headers"]
    assert headers["Authorization"] == FILTERED
    assert headers["Cookie"] == FILTERED
    assert headers["X-CSRFToken"] == FILTERED
    assert headers["Host"] == "app"
    assert cleaned["request"]["data"] == FILTERED
    assert cleaned["request"]["cookies"] == FILTERED
    assert "query_string" not in cleaned["request"]
    assert cleaned["extra"]["environment_id"] == 4
    assert cleaned["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"] == FILTERED


def test_scrub_event_filters_header_pairs() -> None:
    """Headers stored as name and value pairs are filtered too."""
    event: Any = {"request": {"headers": [["Authorization", "Bearer x"], ["Host", "app"]]}}
    cleaned: Any = scrub_event(event, {})
    assert cleaned is not None
    assert cleaned["request"]["headers"] == [["Authorization", FILTERED], ["Host", "app"]]


def test_scrub_event_drops_expected_outcomes() -> None:
    """Blocked deploys and other expected outcomes are not reported."""
    event: Any = {"message": "blocked"}
    assert scrub_event(event, {"exc_info": (BlockedDeploy, BlockedDeploy("x"), None)}) is None
    kept = scrub_event(event, {"exc_info": (ValueError, ValueError("x"), None)})
    assert kept == event


def test_scrub_breadcrumb_removes_sql_parameters_and_bodies() -> None:
    """SQL parameters and HTTP bodies do not survive in breadcrumbs."""
    crumb: Any = {
        "category": "query",
        "message": "SELECT value FROM environment_versions WHERE id = ?",
        "data": {"parameters": [SECRET_VALUE], "body": SECRET_VALUE},
    }
    cleaned: Any = scrub_breadcrumb(crumb, {})
    assert cleaned is not None
    assert SECRET_VALUE not in repr(cleaned)
    assert cleaned["category"] == "query"


def test_sentry_off_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DSN means the SDK is never initialised."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    assert init_sentry(Settings(environment="testing")) is False
    assert calls == []


def test_sentry_init_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured DSN initialises Sentry with the privacy-preserving options."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    settings = Settings(environment="testing", sentry_dsn=SecretStr("https://key@sentry.example/1"))
    assert init_sentry(settings) is True
    options = calls[0]
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sample_rate"] == 0
    assert options["release"] == VERSION
    assert options["environment"] == "testing"
    assert options["before_send"] is scrub_event
    assert options["before_breadcrumb"] is scrub_breadcrumb


def test_otel_resource_environment_defaults_and_overrides() -> None:
    """Defaults name the service and set version and environment; operator values win."""
    defaults = otel_resource_environment("production", {})
    assert defaults["OTEL_SERVICE_NAME"] == "env-manager"
    assert f"service.version={VERSION}" in defaults["OTEL_RESOURCE_ATTRIBUTES"]
    assert "deployment.environment.name=production" in defaults["OTEL_RESOURCE_ATTRIBUTES"]
    assert (
        otel_resource_environment(
            "production", {"OTEL_SERVICE_NAME": "x", "OTEL_RESOURCE_ATTRIBUTES": "a=b"}
        )
        == {}
    )


def test_otel_export_off_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export is enabled only by FASTAPI_OTEL_AUTO_CONFIGURE=true."""
    monkeypatch.delenv("FASTAPI_OTEL_AUTO_CONFIGURE", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    settings = Settings(environment="testing")
    assert configure_otel(settings) is False
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    assert configure_otel(settings) is True
