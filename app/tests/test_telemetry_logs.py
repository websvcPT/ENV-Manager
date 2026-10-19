"""FastAPI's exception and validation logs carry no message, no stack and no request value.

FastAPI records an unhandled exception and a failed validation as OpenTelemetry logs linked to
the trace. The application hands FastAPI a logger provider whose first processor strips them (and
only when logs are exported); these tests run FastAPI's own emission through that provider.
"""

import httpx
from fastapi import FastAPI
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.trace import format_span_id, format_trace_id

from app.core.observability import fastapi_telemetry
from app.core.telemetry_logs import LOG_ATTRIBUTES, ScrubLogRecords
from app.tests.telemetry_helpers import Telemetry

SECRET = "hunter2-database-password"
EXPORTING = {
    "FASTAPI_OTEL_AUTO_CONFIGURE": "true",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid:4318",
}


def new_exporter() -> InMemoryLogRecordExporter:
    """An exporter that keeps the records it is given."""
    return InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]


def build_app(provider: LoggerProvider, exporter: InMemoryLogRecordExporter) -> FastAPI:
    """A FastAPI app with a route that fails with a value in its message and one that validates."""
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    application = FastAPI(telemetry={"logger_provider": provider})

    @application.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"cannot store {SECRET}")

    @application.post("/typed")
    async def typed(amount: int) -> dict[str, int]:
        return {"amount": amount}

    return application


async def call_both(application: FastAPI) -> None:
    """Make the unhandled exception and the validation failure happen."""
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        assert (await http.get("/boom")).status_code == 500
        assert (await http.post(f"/typed?amount={SECRET}")).status_code == 422


def exported_text(exporter: InMemoryLogRecordExporter) -> str:
    """Every body, attribute and severity of the exported records, as one text."""
    parts: list[str] = []
    for record in exporter.get_finished_logs():
        parts.append(repr(record.log_record.body))
        parts.append(repr(dict(record.log_record.attributes or {})))
        parts.append(repr(record.log_record.event_name))
    return "\n".join(parts)


async def test_without_the_scrubber_the_exception_message_and_stack_would_be_exported() -> None:
    """The premise: FastAPI puts the message and the formatted stack on the log record."""
    exporter = new_exporter()
    await call_both(build_app(LoggerProvider(shutdown_on_exit=False), exporter))

    text = exported_text(exporter)
    assert SECRET in text and "Traceback" in text


async def test_the_application_provider_exports_the_class_and_the_route_and_nothing_else() -> None:
    config = fastapi_telemetry(EXPORTING)
    provider = config["logger_provider"]
    assert isinstance(provider, LoggerProvider)
    exporter = new_exporter()
    await call_both(build_app(provider, exporter))

    records = exporter.get_finished_logs()
    assert len(records) == 2
    for record in records:
        assert set(record.log_record.attributes or {}) <= LOG_ATTRIBUTES
        assert record.log_record.exception is None
    text = exported_text(exporter)
    assert SECRET not in text and "Traceback" not in text and "cannot store" not in text
    by_event = {
        record.log_record.event_name: dict(record.log_record.attributes or {}) for record in records
    }
    assert by_event["http.server.request.exception"] == {
        "exception.type": "RuntimeError",
        "http.route": "/boom",
    }
    assert by_event["fastapi.validation.failed"] == {
        "http.route": "/typed",
        "fastapi.validation.error_count": 1,
    }


async def test_a_record_stays_linked_to_the_trace_of_its_request(
    telemetry_recorder: Telemetry,
) -> None:
    provider = fastapi_telemetry(EXPORTING)["logger_provider"]
    assert isinstance(provider, LoggerProvider)
    exporter = new_exporter()
    await call_both(build_app(provider, exporter))

    requests = {
        span.attributes["http.route"]: span
        for span in telemetry_recorder.spans()
        if span.attributes and "http.route" in span.attributes and span.parent is None
    }
    for record in exporter.get_finished_logs():
        route = (record.log_record.attributes or {})["http.route"]
        span = requests[str(route)]
        assert record.log_record.trace_id is not None
        assert format_trace_id(record.log_record.trace_id) == format_trace_id(span.context.trace_id)
        assert record.log_record.span_id is not None
        assert format_span_id(record.log_record.span_id)


def test_the_scrubber_is_the_first_processor_of_the_provider() -> None:
    provider = fastapi_telemetry(EXPORTING)["logger_provider"]
    assert isinstance(provider, LoggerProvider)
    processors = provider._multi_log_record_processor._log_record_processors
    assert isinstance(processors[0], ScrubLogRecords)


def test_the_provider_is_only_installed_when_logs_are_exported() -> None:
    assert fastapi_telemetry({}) == {}
    assert fastapi_telemetry({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"}) == {}, (
        "an endpoint without the switch exports nothing"
    )
    assert fastapi_telemetry({"FASTAPI_OTEL_AUTO_CONFIGURE": "true"}) == {}
    assert fastapi_telemetry({**EXPORTING, "OTEL_LOGS_EXPORTER": "none"}) == {}
    assert fastapi_telemetry({**EXPORTING, "OTEL_SDK_DISABLED": "true"}) == {}
    traces_only = {
        "FASTAPI_OTEL_AUTO_CONFIGURE": "true",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://c:4318/v1/traces",
    }
    assert fastapi_telemetry(traces_only) == {}
    assert "logger_provider" in fastapi_telemetry(
        {**traces_only, "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://c:4318/v1/logs"}
    )
