"""The diagnostics of the General settings: what is shown, and that nothing secret is."""

import pytest
from pydantic import SecretStr

from app import VERSION
from app.config import Settings
from app.core.observability import otel_enabled, otel_endpoint_host
from app.services.diagnostics import collect_diagnostics

DSN = "https://publickey123@o4505.ingest.example-sentry.io/4506"


def settings_with(**overrides: object) -> Settings:
    """Startup settings for tests: production, with a CSRF secret, plus some overrides."""
    return Settings(environment="production", csrf_secret=SecretStr("x" * 40), **overrides)  # type: ignore[arg-type]


def test_with_nothing_configured_everything_is_off_and_empty() -> None:
    """A plain installation reports no error reporting, no tracing and no network lists."""
    found = collect_diagnostics(settings_with(), {}, None)
    assert found.error_reporting is False and found.tracing is False
    assert found.tracing_host is None
    assert (found.trusted_proxies, found.allowed_sources) == ((), ())
    assert (found.version, found.environment, found.client_address) == (
        VERSION,
        "production",
        None,
    )


def test_a_dsn_shows_as_on_and_never_as_text() -> None:
    """Error reporting is on or off: the address and its key are nowhere in the answer."""
    found = collect_diagnostics(settings_with(sentry_dsn=SecretStr(DSN)), {}, "203.0.113.9")
    assert found.error_reporting is True
    text = repr(found)
    for part in ("publickey123", "example-sentry", "4506", "4505", "https://"):
        assert part not in text


def test_the_network_lists_and_the_client_address_are_shown_as_configured() -> None:
    """The lists are the configured ranges; the address is the one resolved for the request."""
    found = collect_diagnostics(
        settings_with(trusted_proxies="172.18.0.0/16, 10.0.0.2", allowed_sources="203.0.113.0/24"),
        {},
        "203.0.113.9",
    )
    assert found.trusted_proxies == ("172.18.0.0/16", "10.0.0.2/32")
    assert found.allowed_sources == ("203.0.113.0/24",)
    assert found.client_address == "203.0.113.9"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("false", False),
        ("1", False),
        ("yes", False),
        ("", False),
    ],
)
def test_tracing_is_on_only_for_true(value: str, expected: bool) -> None:
    """The same switch FastAPI reads, compared without regard to case."""
    assert otel_enabled({"FASTAPI_OTEL_AUTO_CONFIGURE": value}) is expected
    assert otel_enabled({}) is False


@pytest.mark.parametrize(
    ("endpoint", "host"),
    [
        ("https://collector.example.com", "collector.example.com"),
        (
            "https://user:pa55word@collector.example.com:4318/v1/traces?key=abc#frag",
            "collector.example.com",
        ),
        ("collector.example.com:4318", "collector.example.com"),
        ("collector.example.com/v1/traces", "collector.example.com"),
        ("http://[2001:db8::1]:4318/", "2001:db8::1"),
        ("http://127.0.0.1:4318", "127.0.0.1"),
        ("HTTP://Collector.Example.COM:4318", "collector.example.com"),
        ("http://[bad", None),
        ("   ", None),
        ("", None),
    ],
)
def test_the_endpoint_is_reduced_to_its_host_name(endpoint: str, host: str | None) -> None:
    """Credentials, port, path, query and fragment never survive; a broken value gives None."""
    assert otel_endpoint_host({"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint}) == host


def test_the_traces_endpoint_is_shown_first_and_its_own_variable_beats_the_general_one() -> None:
    """The host is where traces go: the traces variable, else the general one; metrics and logs
    only show when nothing sends traces. This is the order FastAPI's export uses."""
    environ = {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://logs.example.com"}
    assert otel_endpoint_host(environ) == "logs.example.com"
    environ["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] = "https://metrics.example.com"
    assert otel_endpoint_host(environ) == "metrics.example.com"
    environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://general.example.com"
    assert otel_endpoint_host(environ) == "general.example.com"
    environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "https://traces.example.com"
    assert otel_endpoint_host(environ) == "traces.example.com"
    assert otel_endpoint_host({"OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer abc"}) is None


@pytest.mark.parametrize(
    "environ",
    [
        {"OTEL_SDK_DISABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://c.example.com"},
        {"OTEL_TRACES_EXPORTER": "none", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://c.example.com"},
        {"OTEL_TRACES_EXPORTER": "console", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://c.example.com"},
    ],
)
def test_no_host_is_shown_for_a_signal_that_is_not_exported(environ: dict[str, str]) -> None:
    """A disabled SDK, or an exporter other than OTLP for the signal, sends nothing there."""
    environ = {**environ, "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none"}
    assert otel_endpoint_host(environ) is None


def test_headers_and_other_variables_are_never_read_into_the_diagnostics() -> None:
    """Only the switch and the endpoint host are taken from the environment."""
    environ = {
        "FASTAPI_OTEL_AUTO_CONFIGURE": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example.com",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer topsecret",
        "ENVMANAGER_KEY": "keymaterial",
    }
    found = collect_diagnostics(settings_with(), environ, None)
    text = repr(found)
    assert found.tracing is True and found.tracing_host == "collector.example.com"
    assert "topsecret" not in text and "keymaterial" not in text
