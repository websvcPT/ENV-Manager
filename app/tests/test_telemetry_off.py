"""With nothing configured, telemetry is off: no provider, no exporter thread, no trace id.

The global OpenTelemetry providers can be set once per process and the other tests install their
own, so each case runs the real application, through a real ASGI startup and shutdown, in a fresh
interpreter. A request carrying a ``traceparent`` header is sent to a probe route; the cases with
export unconfigured must give it no span context at all, and the one with export configured shows
that the harness would notice if it did.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.crypto import generate_master_key

ROOT = Path(__file__).resolve().parents[2]
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
THREAD_MARKERS = ("Otel", "BatchSpan", "PeriodicExporting", "BatchLog")

SCRIPT = r"""
import asyncio
import json
import sys
import threading

import httpx
import sentry_sdk
from opentelemetry import _logs, metrics, trace

from app.core import telemetry
from app.main import create_app

application = create_app()


@application.get("/probe")
async def probe() -> dict[str, object]:
    context = trace.get_current_span().get_span_context()
    return {"valid": context.is_valid, "trace_id": telemetry.current_trace_id()}


async def lifespan_call(application, sent, received):
    scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
    async def receive():
        return await received.get()
    async def send(message):
        await sent.put(message)
    await application(scope, receive, send)


async def main():
    sent, received = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(lifespan_call(application, sent, received))
    await received.put({"type": "lifespan.startup"})
    assert (await sent.get())["type"] == "lifespan.startup.complete"
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/probe", headers={"traceparent": sys.argv[1]})
    with telemetry.span("probe.span") as span:
        recording = span.is_recording()
        inside = telemetry.current_trace_id()
    active = getattr(trace.get_tracer_provider(), "_active_span_processor", None)
    processors = getattr(active, "_span_processors", ())
    report = {
        "redaction": [type(item).__name__ for item in processors][:1] == ["RedactRequestSpans"],
        "probe": response.json(),
        "recording": recording,
        "inside": inside,
        "tracer": type(trace.get_tracer_provider()).__name__,
        "meter": type(metrics.get_meter_provider()).__name__,
        "logger": type(_logs.get_logger_provider()).__name__,
        "sentry": sentry_sdk.get_client().is_active(),
        "threads": [thread.name for thread in threading.enumerate()],
    }
    await received.put({"type": "lifespan.shutdown"})
    assert (await sent.get())["type"] == "lifespan.shutdown.complete"
    await task
    print("REPORT " + json.dumps(report))


asyncio.run(main())
"""


def run_unconfigured_app(tmp_path: Path, extra: dict[str, str]) -> dict[str, object]:
    """Run the script in a fresh interpreter with a clean environment and return its report."""
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("OTEL_", "FASTAPI_OTEL_", "ENVMANAGER_", "SENTRY_"))
    }
    environment = {
        **inherited,
        "PYTHONPATH": str(ROOT),
        "ENVMANAGER_ENVIRONMENT": "testing",
        "ENVMANAGER_CSRF_SECRET": "test-not-secret-0123456789abcdef0123456789",
        "ENVMANAGER_DATA_PATH": str(tmp_path),
        "ENVMANAGER_KEY": generate_master_key(),
        "OTEL_EXPORTER_OTLP_TIMEOUT": "1",
        **extra,
    }
    finished = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SCRIPT, TRACEPARENT],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr[-2000:]
    (line,) = [line for line in finished.stdout.splitlines() if line.startswith("REPORT ")]
    report: dict[str, object] = json.loads(line.removeprefix("REPORT "))
    return report


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"},
        {"FASTAPI_OTEL_AUTO_CONFIGURE": "true"},
        {
            "FASTAPI_OTEL_AUTO_CONFIGURE": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
            "OTEL_SDK_DISABLED": "true",
        },
    ],
    ids=["nothing set", "endpoint without the switch", "switch without an endpoint", "disabled"],
)
def test_nothing_is_set_up_and_a_traceparent_gives_no_span_context(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    report = run_unconfigured_app(tmp_path, extra)

    assert report["probe"] == {"valid": False, "trace_id": None}
    assert report["recording"] is False and report["inside"] is None
    assert report["tracer"] == "ProxyTracerProvider"
    assert report["meter"] == "_ProxyMeterProvider"
    assert report["logger"] == "ProxyLoggerProvider"
    assert report["sentry"] is False
    threads = report["threads"]
    assert isinstance(threads, list)
    assert not [name for name in threads if any(marker in name for marker in THREAD_MARKERS)]


EXPORTING = {
    "FASTAPI_OTEL_AUTO_CONFIGURE": "true",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
}
SENDER_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def test_with_export_configured_a_request_gets_a_span_and_a_trace_id_of_its_own(
    tmp_path: Path,
) -> None:
    """The control: a harness that could not see a span context would pass the cases above.

    The peer is not a trusted proxy, so the ``traceparent`` it sent is ignored and the request
    starts a root trace: an audit entry never carries an id the client chose.
    """
    report = run_unconfigured_app(tmp_path, EXPORTING)

    probe = report["probe"]
    assert isinstance(probe, dict) and probe["valid"] is True
    assert isinstance(probe["trace_id"], str) and len(probe["trace_id"]) == 32
    assert probe["trace_id"] != SENDER_TRACE_ID
    assert report["recording"] is True and isinstance(report["inside"], str)
    assert report["tracer"] == "TracerProvider"
    assert report["meter"] == "MeterProvider"
    assert report["logger"] == "ProxyLoggerProvider", "logs use the application's own provider"


def test_a_trusted_proxy_passes_the_trace_context_on(tmp_path: Path) -> None:
    """From a peer inside the trusted networks the request joins the sender's trace."""
    trusted = {**EXPORTING, "ENVMANAGER_TRUSTED_PROXIES": "127.0.0.1"}
    report = run_unconfigured_app(tmp_path, trusted)

    assert report["probe"] == {"valid": True, "trace_id": SENDER_TRACE_ID}


def test_the_application_tracer_provider_is_the_global_one_when_traces_are_exported(
    tmp_path: Path,
) -> None:
    report = run_unconfigured_app(tmp_path, EXPORTING)

    assert report["tracer"] == "TracerProvider"
    assert report["redaction"] is True
