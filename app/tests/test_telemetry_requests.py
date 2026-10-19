"""Request spans carry the route template and never the raw path or the query string.

FastAPI records ``url.path`` and ``url.query`` when it starts the request span. The application
hands it a tracer provider whose first processor replaces both, so a key name in a reveal path and
a host path being typed never reach an exporter. The routes below have the shapes of the real ones
(a key name in the path, a typed path in the query); the provider is the application's own.
"""

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.telemetry import REDACTED, RedactRequestSpans, redacting_tracer_provider

KEY_NAME = "DB_PASSWORD_KEY_sentinel"
TYPED_PATH = "/srv/secret-folder-sentinel/typed"
SEARCH = "search-text-sentinel"

CASES = [
    ("POST", f"/environments/7/reveal/{KEY_NAME}", "/environments/{environment_id}/reveal/{key}"),
    ("GET", f"/paths/validate?path={TYPED_PATH}", "/paths/validate"),
    ("GET", f"/paths/browse?path={TYPED_PATH}", "/paths/browse"),
    ("GET", f"/settings/paths/validate?path={TYPED_PATH}", "/settings/paths/validate"),
    (
        "GET",
        f"/environments/7/target-check?path={TYPED_PATH}",
        "/environments/{environment_id}/target-check",
    ),
    ("GET", f"/projects?q={SEARCH}", "/projects"),
]


def build_app() -> tuple[FastAPI, InMemorySpanExporter]:
    """An application with the routes of the cases and the application's tracer provider."""
    provider = redacting_tracer_provider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    application = FastAPI(telemetry={"tracer_provider": provider})

    @application.post("/environments/{environment_id}/reveal/{key}")
    async def reveal(environment_id: int, key: str) -> dict[str, str]:
        return {"ok": "yes"}

    @application.get("/environments/{environment_id}/target-check")
    async def target_check(environment_id: int, path: str) -> dict[str, str]:
        return {"ok": "yes"}

    for route in ("/paths/validate", "/paths/browse", "/settings/paths/validate"):

        async def typed(path: str) -> dict[str, str]:
            return {"ok": "yes"}

        application.add_api_route(route, typed, methods=["GET"])

    @application.get("/projects")
    async def search(q: str = "") -> dict[str, str]:
        return {"ok": "yes"}

    return application, exporter


async def send(application: FastAPI, method: str, target: str) -> None:
    """Make one request."""
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        assert (await http.request(method, target)).status_code == 200


def attributes_of(span: ReadableSpan) -> dict[str, object]:
    """The attributes of a span as a plain dict."""
    return dict(span.attributes or {})


@pytest.mark.parametrize(("method", "target", "route"), CASES)
async def test_a_request_span_has_the_route_template_and_no_value_of_the_request(
    method: str, target: str, route: str
) -> None:
    application, exporter = build_app()

    await send(application, method, target)

    (span,) = exporter.get_finished_spans()
    attributes = attributes_of(span)
    assert attributes["http.route"] == route
    assert span.name == f"{method} {route}"
    assert attributes["url.path"] == REDACTED
    assert attributes.get("url.query", REDACTED) == REDACTED
    everything = repr((span.name, attributes, [event.attributes for event in span.events]))
    for sentinel in (KEY_NAME, TYPED_PATH, "secret-folder-sentinel", SEARCH):
        assert sentinel not in everything


async def test_a_request_without_a_query_string_gets_no_query_attribute() -> None:
    application, exporter = build_app()

    await send(application, "GET", "/paths/validate?path=")

    (span,) = exporter.get_finished_spans()
    assert attributes_of(span).get("url.query") in (None, REDACTED)


def test_the_redaction_runs_before_any_exporter_of_the_provider() -> None:
    provider = redacting_tracer_provider()
    processors = provider._active_span_processor._span_processors
    assert isinstance(processors[0], RedactRequestSpans)
