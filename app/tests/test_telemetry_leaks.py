"""No secret reaches a span, an event, a metric label or a log line, whatever the request does.

The real application runs on real folders with a fake docker that prints a configuration value
and a marker; requests deploy a service, read its last run, are refused, fail to authenticate and
sign in with a wrong password. Everything the in-memory providers received, and everything the
application logged, is searched for the values that must stay out.
"""

import logging

import pytest

from app.core.csrf import CSRF_COOKIE_NAME
from app.seeds import EDITOR_SECRET_VALUES
from app.tests.api_helpers import ApiWorld, audit_entries, bearer, deploy, last, make_token
from app.tests.telemetry_helpers import Telemetry

OUTPUT_MARKER = "command-output-marker-3f9a1c"
WRONG_PASSWORD = "wrong-password-sentinel-77"
UNKNOWN_TOKEN = "envm_" + "ab12" * 12
SEEDED_VALUES = ("seed-secret", "db.internal.example.com", "3306")


async def exercise(world: ApiWorld) -> tuple[str, ...]:
    """Run the requests and return every string that must not appear anywhere."""
    world.docker.behavior(f'echo "{OUTPUT_MARKER} {EDITOR_SECRET_VALUES[3]}"')
    secret = await make_token(world, "admin")
    mysql = world.services["prod/mysql"]
    assert (await deploy(world, secret, mysql)).status_code == 200
    assert (await last(world, secret, mysql)).status_code == 200
    refused = await deploy(world, secret, mysql, {"environments": [EDITOR_SECRET_VALUES[2]]})
    assert refused.status_code == 422
    unknown = await world.http.post(
        f"/api/v1/services/{mysql}/deploy", json={}, headers=bearer(UNKNOWN_TOKEN)
    )
    assert unknown.status_code == 401
    await world.http.get("/healthz")
    signed_in = await world.http.post(
        "/login",
        data={
            "identifier": "admin@example.test",
            "password": WRONG_PASSWORD,
            "csrf_token": world.http.cookies[CSRF_COOKIE_NAME],
        },
    )
    assert signed_in.status_code in {200, 401, 422}
    command = "compose up"
    roots = tuple(str(root) for root in world.roots.values())
    return (
        *EDITOR_SECRET_VALUES,
        *SEEDED_VALUES,
        OUTPUT_MARKER,
        WRONG_PASSWORD,
        UNKNOWN_TOKEN,
        secret,
        command,
        *roots,
    )


async def test_nothing_secret_reaches_a_span_a_metric_or_a_log_line(
    api_world: ApiWorld,
    telemetry_recorder: Telemetry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    forbidden = await exercise(api_world)

    recorded = telemetry_recorder.everything_recorded()
    names = {span.name for span in telemetry_recorder.spans()}
    assert {"envmanager.deploy", "envmanager.post_deploy", "envmanager.dist_check"} <= names
    assert any("http.route" in (span.attributes or {}) for span in telemetry_recorder.spans()), (
        "the request spans of FastAPI are among what was searched"
    )
    assert "envmanager.deploys" in telemetry_recorder.collected
    for value in forbidden:
        assert value not in recorded, value
        assert value not in caplog.text, value


async def test_the_trace_ids_of_audit_entries_belong_to_spans_that_were_recorded(
    api_world: ApiWorld, telemetry_recorder: Telemetry
) -> None:
    secret = await make_token(api_world, "admin")
    mysql = api_world.services["prod/mysql"]
    await deploy(api_world, secret, mysql)

    traces = {format(span.context.trace_id, "032x") for span in telemetry_recorder.spans()}
    entries = [e for e in await audit_entries(api_world) if e.action == "service.deploy"]
    assert entries, "the deploy was audited"
    for entry in entries:
        assert entry.trace_id in traces
