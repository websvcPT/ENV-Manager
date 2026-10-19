"""The observability test command: what it sends, where, and what really became of it.

The destinations are small HTTP servers on the loopback interface, so the real Sentry client and
the real OTLP exporter run against them. The blocking calls run in a thread, because the servers
live on the loop of the test.
"""

import asyncio
import gzip
import socket
from urllib.parse import urlsplit

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from pydantic import SecretStr
from typer.testing import CliRunner

from app import VERSION
from app.cli.main import app as cli_app
from app.config import Settings, get_settings
from app.core.observability_check import (
    TEST_MESSAGE,
    TEST_SPAN,
    CheckResult,
    check_error_reporting,
    check_tracing,
)
from app.tests.webhook_helpers import Receiver

DSN_KEY = "sentry-public-key-0123456789abcdef"
HEADER_SECRET = "otlp-header-secret-sentinel"
QUIET = {"default_integrations": False, "integrations": []}
EXPORTING = {"FASTAPI_OTEL_AUTO_CONFIGURE": "true"}


def port_of(receiver: Receiver) -> int:
    """The port a receiver listens on."""
    port = urlsplit(receiver.url).port
    assert port is not None
    return port


def dsn_for(receiver: Receiver) -> SecretStr:
    """A DSN that points at a receiver."""
    return SecretStr(f"http://{DSN_KEY}@127.0.0.1:{port_of(receiver)}/1")


def settings_for(receiver: Receiver | None = None) -> Settings:
    """Startup settings with a DSN for the receiver, or none."""
    return Settings(
        environment="testing",
        sentry_dsn=dsn_for(receiver) if receiver is not None else None,
    )


def closed_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def sentry_check(settings: Settings) -> CheckResult:
    """Run the Sentry test in a thread with a quiet client and a short wait."""
    return await asyncio.to_thread(
        check_error_reporting, settings, timeout_seconds=3.0, client_options=QUIET
    )


async def tracing_check(environ: dict[str, str]) -> CheckResult:
    """Run the span test in a thread with a short wait."""
    return await asyncio.to_thread(
        check_tracing, settings_for(), {**EXPORTING, **environ}, timeout_seconds=1.5
    )


async def test_a_sentry_event_the_server_accepted_is_reported_as_sent_to_its_host() -> None:
    async with Receiver(200) as receiver:
        result = await sentry_check(settings_for(receiver))

        assert result.configured and result.delivered and result.host == "127.0.0.1"
        assert result.message.startswith("test event ")
        assert "accepted by 127.0.0.1 (HTTP 200)" in result.message
        assert DSN_KEY not in repr(result)
        (request,) = [item for item in receiver.requests if b"event" in gzip.decompress(item.body)]
        sent = gzip.decompress(request.body)
        assert TEST_MESSAGE.encode() in sent and VERSION.encode() in sent
        assert b'"environment":"testing"' in sent.replace(b" ", b"")


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_a_sentry_event_the_server_refused_is_reported_as_failed(status: int) -> None:
    async with Receiver(status) as receiver:
        result = await sentry_check(settings_for(receiver))

    assert result.configured and not result.delivered
    assert result.message == f"the server at 127.0.0.1 answered HTTP {status}"


async def test_a_sentry_server_that_cannot_be_reached_is_reported_as_failed() -> None:
    gone = SecretStr(f"http://{DSN_KEY}@127.0.0.1:{closed_port()}/1")
    result = await sentry_check(Settings(environment="testing", sentry_dsn=gone))

    assert result.configured and not result.delivered
    assert "sent" not in result.message and "accepted" not in result.message
    assert result.message.startswith("the event could not be delivered")
    assert DSN_KEY not in repr(result)


async def test_an_event_that_the_scrubber_drops_is_not_reported_as_sent() -> None:
    async with Receiver(200) as receiver:
        options = {**QUIET, "before_send": lambda event, hint: None}
        result = await asyncio.to_thread(
            check_error_reporting,
            settings_for(receiver),
            timeout_seconds=2.0,
            client_options=options,
        )

    assert result.configured and not result.delivered
    assert result.message == "the event was dropped before sending"
    assert not [
        item for item in receiver.requests if TEST_MESSAGE.encode() in gzip.decompress(item.body)
    ]


async def test_a_dsn_that_cannot_be_read_is_reported_as_invalid() -> None:
    result = await sentry_check(
        Settings(environment="testing", sentry_dsn=SecretStr("not-a-dsn-at-all"))
    )
    assert result.configured and not result.delivered
    assert result.message == "the DSN is not valid"


async def test_without_a_dsn_nothing_is_sent_and_it_says_so() -> None:
    result = await sentry_check(settings_for())
    assert (result.configured, result.delivered, result.host) == (False, False, None)
    assert result.message == "not configured, nothing sent (ENVMANAGER_SENTRY_DSN is not set)"


async def test_a_span_the_collector_accepted_is_reported_as_exported_to_its_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"authorization=Bearer {HEADER_SECRET}")
    async with Receiver(200) as receiver:
        result = await tracing_check(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port_of(receiver)}/"}
        )

        assert result.configured and result.delivered and result.host == "127.0.0.1"
        assert "exported to 127.0.0.1 and accepted" in result.message
        assert HEADER_SECRET not in repr(result)
        (request,) = receiver.requests
        assert request.path == "/v1/traces"
        assert HEADER_SECRET in str(request.headers), "the exporter used the configured headers"
        exported = ExportTraceServiceRequest.FromString(request.body)
        (span,) = exported.resource_spans[0].scope_spans[0].spans
        assert span.name == TEST_SPAN
        assert span.trace_id.hex() in result.message
        resource = {
            attribute.key: attribute.value.string_value
            for attribute in exported.resource_spans[0].resource.attributes
        }
        assert resource["service.name"] == "env-manager"
        assert resource["service.version"] == VERSION
        assert resource["deployment.environment.name"] == "testing"


async def test_the_traces_endpoint_variable_is_used_as_given_and_beats_the_general_one() -> None:
    async with Receiver(200) as receiver, Receiver(200) as general:
        result = await tracing_check(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port_of(general)}",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": (
                    f"http://127.0.0.1:{port_of(receiver)}/custom/traces"
                ),
            }
        )

        assert result.delivered
        assert [request.path for request in receiver.requests] == ["/custom/traces"]
        assert general.requests == []


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_a_collector_that_refuses_the_span_is_reported_as_failed(status: int) -> None:
    async with Receiver(status) as receiver:
        result = await tracing_check(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port_of(receiver)}"}
        )

    assert result.configured and not result.delivered
    assert result.message == (
        "the span was not accepted by 127.0.0.1 (unreachable, refused or too slow)"
    )


async def test_a_collector_that_cannot_be_reached_is_reported_as_failed() -> None:
    result = await tracing_check(
        {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{closed_port()}"}
    )
    assert result.configured and not result.delivered
    assert "exported" not in result.message


NOT_SWITCHED_ON = "FASTAPI_OTEL_AUTO_CONFIGURE is not true"
NO_TRACES = "no OTLP traces endpoint is set or traces are switched off"
ENDPOINT = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"}


@pytest.mark.parametrize(
    ("environ", "reason"),
    [
        ({**ENDPOINT, "FASTAPI_OTEL_AUTO_CONFIGURE": "no"}, NOT_SWITCHED_ON),
        ({**EXPORTING}, NO_TRACES),
        ({**EXPORTING, **ENDPOINT, "OTEL_TRACES_EXPORTER": "none"}, NO_TRACES),
        ({**EXPORTING, **ENDPOINT, "OTEL_SDK_DISABLED": "true"}, NO_TRACES),
    ],
)
async def test_without_a_configured_traces_destination_nothing_is_exported(
    environ: dict[str, str], reason: str
) -> None:
    result = await asyncio.to_thread(check_tracing, settings_for(), environ)

    assert (result.configured, result.delivered, result.host) == (False, False, None)
    assert result.message == f"not configured, nothing sent ({reason})"


async def test_an_endpoint_that_is_not_an_http_address_is_refused_before_anything_is_sent() -> None:
    result = await tracing_check({"OTEL_EXPORTER_OTLP_ENDPOINT": "ftp://collector.example.com"})
    assert result.configured and not result.delivered
    assert result.host == "collector.example.com"
    assert result.message == "the traces endpoint is not an http or https address"


@pytest.fixture
def cli_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command line environment with no Sentry and no OpenTelemetry settings."""
    for name in (
        "ENVMANAGER_SENTRY_DSN",
        "FASTAPI_OTEL_AUTO_CONFIGURE",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_TRACES_EXPORTER",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


async def run_cli(monkeypatch: pytest.MonkeyPatch, **variables: str) -> tuple[int, str]:
    """Run ``observability test`` in a thread with the variables set and return code and output."""
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        result = await asyncio.to_thread(
            CliRunner().invoke, cli_app, ["observability", "test", "--timeout", "2"]
        )
    finally:
        get_settings.cache_clear()
    return result.exit_code, result.output


@pytest.mark.usefixtures("cli_environment")
async def test_the_command_with_nothing_configured_sends_nothing_and_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, output = await run_cli(monkeypatch)

    assert code == 0
    assert output.splitlines() == [
        "Error reporting: not configured, nothing sent (ENVMANAGER_SENTRY_DSN is not set)",
        "OpenTelemetry: not configured, nothing sent (FASTAPI_OTEL_AUTO_CONFIGURE is not true)",
    ]


@pytest.mark.usefixtures("cli_environment")
async def test_the_command_reports_both_destinations_by_host_and_exits_cleanly_when_both_took_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with Receiver(200) as sentry, Receiver(200) as collector:
        ports = (str(port_of(sentry)), str(port_of(collector)))
        code, output = await run_cli(
            monkeypatch,
            ENVMANAGER_SENTRY_DSN=dsn_for(sentry).get_secret_value(),
            FASTAPI_OTEL_AUTO_CONFIGURE="true",
            OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{ports[1]}",
            OTEL_EXPORTER_OTLP_HEADERS=f"authorization=Bearer {HEADER_SECRET}",
        )

    assert code == 0, output
    sentry_line, tracing_line = output.splitlines()
    assert sentry_line.startswith("Error reporting: test event ")
    assert "accepted by 127.0.0.1 (HTTP 200)" in sentry_line
    assert tracing_line.startswith("OpenTelemetry: test span ")
    assert "exported to 127.0.0.1 and accepted" in tracing_line
    assert DSN_KEY not in output and HEADER_SECRET not in output
    assert not any(port in output for port in ports)


@pytest.mark.usefixtures("cli_environment")
async def test_the_command_exits_with_an_error_when_a_configured_destination_refused_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with Receiver(500) as sentry, Receiver(200) as collector:
        code, output = await run_cli(
            monkeypatch,
            ENVMANAGER_SENTRY_DSN=dsn_for(sentry).get_secret_value(),
            FASTAPI_OTEL_AUTO_CONFIGURE="true",
            OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{port_of(collector)}",
        )

    assert code == 1
    assert "Error reporting: the server at 127.0.0.1 answered HTTP 500" in output
    assert "OpenTelemetry: test span " in output


@pytest.mark.usefixtures("cli_environment")
async def test_the_command_reports_an_unreachable_collector_as_failed_not_as_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, output = await run_cli(
        monkeypatch,
        FASTAPI_OTEL_AUTO_CONFIGURE="true",
        OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{closed_port()}",
    )

    assert code == 1
    assert "OpenTelemetry: the span was not accepted by 127.0.0.1" in output
    assert "exported" not in output
