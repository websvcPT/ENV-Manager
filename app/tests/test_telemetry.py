"""Custom spans, the trace id on audit entries and the metric instruments' basic behaviour."""

from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core import metrics, telemetry
from app.core.db import create_session_factory
from app.models import AuditActorType, AuditEntry, AuditResult
from app.services.audit import record_audit
from app.tests.telemetry_helpers import Telemetry

FORBIDDEN_FRAGMENTS = ("path", "command", "output", "value", "content", "token", "secret", "url")
SECRET = "hunter2-database-password"


def test_the_attribute_vocabulary_names_nothing_that_can_hold_a_value() -> None:
    """Every key is an id, a name, a count or a result; none names a path, text or a credential."""
    for key in telemetry.ATTRIBUTES:
        assert not any(fragment in key for fragment in FORBIDDEN_FRAGMENTS), key


def test_a_span_keeps_only_vocabulary_keys_and_short_scalars(telemetry_recorder: Telemetry) -> None:
    long_text = "x" * (telemetry.MAX_TEXT_LENGTH + 1)
    with telemetry.span(
        "test.span",
        {
            telemetry.SERVICE_ID: 4,
            telemetry.RESULT: "success",
            telemetry.DRY_RUN: False,
            "envmanager.target_path": "/srv/app/.env",
            "command": f"docker run -e PASSWORD={SECRET}",
            telemetry.SERVICE_NAME: long_text,
            telemetry.ENVIRONMENT_NAME: ["not", "a", "scalar"],
            telemetry.DEPLOYMENT_ID: None,
        },
    ) as active:
        telemetry.annotate(active, {"output": SECRET, telemetry.EXIT_CODE: 3})

    attributes = dict(telemetry_recorder.span("test.span").attributes or {})
    assert attributes == {
        telemetry.SERVICE_ID: 4,
        telemetry.RESULT: "success",
        telemetry.DRY_RUN: False,
        telemetry.EXIT_CODE: 3,
    }


def test_a_span_that_fails_keeps_the_class_name_and_never_the_message(
    telemetry_recorder: Telemetry,
) -> None:
    with pytest.raises(ValueError, match="boom"), telemetry.span("test.failing"):
        raise ValueError(f"boom {SECRET}")

    failed = telemetry_recorder.span("test.failing")
    assert failed.status.status_code is trace.StatusCode.ERROR
    assert failed.status.description is None
    assert failed.events == ()
    assert dict(failed.attributes or {}) == {telemetry.ERROR_TYPE: "ValueError"}
    assert SECRET not in telemetry_recorder.everything_recorded()


def test_a_span_is_a_child_of_the_current_one(telemetry_recorder: Telemetry) -> None:
    with telemetry.span("test.outer"), telemetry.span("test.inner"):
        pass
    outer, inner = telemetry_recorder.span("test.outer"), telemetry_recorder.span("test.inner")
    assert inner.parent is not None and inner.parent.span_id == outer.context.span_id
    assert inner.context.trace_id == outer.context.trace_id


def test_the_trace_id_is_that_of_the_recording_span(telemetry_recorder: Telemetry) -> None:
    with telemetry.span("test.traced"):
        found = telemetry.current_trace_id()
    expected = format(telemetry_recorder.span("test.traced").context.trace_id, "032x")
    assert found == expected and len(expected) == 32


def test_there_is_no_trace_id_outside_a_span() -> None:
    assert telemetry.current_trace_id() is None


def test_a_valid_remote_parent_that_nothing_records_gives_no_trace_id() -> None:
    """A context that was not sampled has an id but no trace behind it, so it is never stored."""
    unsampled = SpanContext(
        trace_id=int("4bf92f3577b34da6a3ce929d0e0e4736", 16),
        span_id=int("00f067aa0ba902b7", 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.DEFAULT),
    )
    with trace.use_span(NonRecordingSpan(unsampled)):
        assert trace.get_current_span().get_span_context().is_valid
        assert telemetry.current_trace_id() is None


def test_while_nothing_records_a_span_is_the_no_op_span(telemetry_providers: Telemetry) -> None:
    """The providers of this worker exist but their sampler is switched off outside a test that
    asks for recording: spans do not record and carry no trace id."""
    assert telemetry_providers.sampler.active is False
    with telemetry.span("test.silent") as active:
        assert not active.is_recording()
        assert telemetry.current_trace_id() is None


async def test_an_audit_entry_keeps_the_trace_id_of_the_recording_span(
    seeded_database: AsyncEngine, telemetry_recorder: Telemetry
) -> None:
    async with create_session_factory(seeded_database)() as db:
        with telemetry.span("test.audited"):
            await record_audit(
                db, action="test.traced", result=AuditResult.SUCCESS, actor_type=AuditActorType.CLI
            )
        await record_audit(
            db, action="test.untraced", result=AuditResult.SUCCESS, actor_type=AuditActorType.CLI
        )
        await db.commit()
        entries = {
            entry.action: entry.trace_id
            for entry in (
                await db.execute(select(AuditEntry).where(AuditEntry.action.like("test.%")))
            ).scalars()
        }

    expected = format(telemetry_recorder.span("test.audited").context.trace_id, "032x")
    assert entries == {"test.traced": expected, "test.untraced": None}


def test_the_backup_age_is_read_from_the_held_time_and_is_absent_without_one() -> None:
    clock = metrics._BackupClock()
    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    assert clock.age_seconds(now) is None
    clock.note(now - timedelta(hours=3))
    assert clock.age_seconds(now) == 3 * 3600
    clock.note(now - timedelta(hours=9))
    assert clock.age_seconds(now) == 3 * 3600, "an older backup never moves the time back"
    clock.note(now - timedelta(minutes=5))
    assert clock.age_seconds(now) == 300
    clock.reset()
    assert clock.age_seconds(now) is None
