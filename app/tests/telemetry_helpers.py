"""In-memory OpenTelemetry for the tests.

The global tracer and meter providers can be set once per process, so one pair is installed for
the whole worker the first time a test asks for it. Its sampler records nothing until a test
switches it on (``begin``) and switches it off again (``end``), so every other test of the worker
runs as it does with export unconfigured: no recording span, no trace id on audit entries. Counters
and histograms use delta temporality and the reader is drained when a test begins, so a test sees
only what it did itself.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import Link, SpanKind, TraceState
from opentelemetry.util.types import Attributes


class SwitchSampler(Sampler):
    """Samples everything while ``active`` and nothing otherwise."""

    def __init__(self) -> None:
        """Start switched off."""
        self.active = False

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        """Delegate to always-on or always-off."""
        chosen = ALWAYS_ON if self.active else ALWAYS_OFF
        return chosen.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        """Describe the sampler."""
        return "SwitchSampler"


@dataclass
class Telemetry:
    """The in-memory exporter and reader behind the global providers of this process."""

    sampler: SwitchSampler
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader
    collected: dict[str, list[Any]] = field(default_factory=dict)

    @classmethod
    def install(cls) -> Telemetry:
        """Set the global tracer and meter providers of this process, once."""
        sampler = SwitchSampler()
        exporter = InMemorySpanExporter()
        reader = InMemoryMetricReader(
            preferred_temporality={
                Counter: AggregationTemporality.DELTA,
                Histogram: AggregationTemporality.DELTA,
            }
        )
        resource = Resource.create({"service.name": "env-manager-tests"})
        tracer_provider = TracerProvider(
            sampler=ParentBased(sampler), resource=resource, shutdown_on_exit=False
        )
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(
            MeterProvider(metric_readers=[reader], resource=resource, shutdown_on_exit=False)
        )
        assert trace.get_tracer_provider() is tracer_provider, "a tracer provider was already set"
        return cls(sampler, exporter, reader)

    def begin(self) -> None:
        """Forget what earlier tests recorded and start recording."""
        self.exporter.clear()
        self.reader.get_metrics_data()
        self.collected = {}
        self.sampler.active = True

    def end(self) -> None:
        """Stop recording and drop what was recorded."""
        self.sampler.active = False
        self.exporter.clear()
        self.reader.get_metrics_data()

    def spans(self, name: str | None = None) -> list[ReadableSpan]:
        """The finished spans, only those with ``name`` when given."""
        found = self.exporter.get_finished_spans()
        return [span for span in found if name is None or span.name == name]

    def span(self, name: str) -> ReadableSpan:
        """The one finished span with a name."""
        (found,) = self.spans(name)
        return found

    def points(self, name: str) -> list[Any]:
        """Every data point of a metric recorded since the test began."""
        data = self.reader.get_metrics_data()
        if data is not None:
            for resource_metrics in data.resource_metrics:
                for scope_metrics in resource_metrics.scope_metrics:
                    for metric in scope_metrics.metrics:
                        self.collected.setdefault(metric.name, []).extend(metric.data.data_points)
        return self.collected.get(name, [])

    def counted(self, name: str) -> dict[tuple[tuple[str, Any], ...], float]:
        """The sum of a counter by its sorted attributes."""
        totals: dict[tuple[tuple[str, Any], ...], float] = {}
        for point in self.points(name):
            key = tuple(sorted((point.attributes or {}).items()))
            totals[key] = totals.get(key, 0) + point.value
        return totals

    def everything_recorded(self) -> str:
        """Span names, attributes, events and status texts and metric attributes, as one text."""
        parts: list[str] = []
        for span in self.exporter.get_finished_spans():
            parts.append(span.name)
            parts.append(repr(dict(span.attributes or {})))
            parts.append(repr(span.status.description))
            for event in span.events:
                parts.append(event.name)
                parts.append(repr(dict(event.attributes or {})))
        self.points("")
        for name, collected in self.collected.items():
            for point in collected:
                parts.append(name + repr(dict(point.attributes or {})))
        return "\n".join(parts)
