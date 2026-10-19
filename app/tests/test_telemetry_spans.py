"""The spans and metrics of deploys, dist checks, drift checks and post-deploy commands.

They are read from in-memory providers, so the tests see what an exporter would be given.
"""

import pytest
from opentelemetry.trace import StatusCode

from app.core import telemetry
from app.models import DeployStatus, DriftState, PostDeployStatus
from app.services.deploy_lock import hold_deploy_lock
from app.services.drift import DriftKind, load_drift_view
from app.services.editor import load_editor_header
from app.services.hooks import PostDeployResult
from app.services.versions import create_version
from app.tests.deploy_helpers import Harness
from app.tests.dist_helpers import link_dist
from app.tests.telemetry_helpers import Telemetry

MANAGED = "A=1\nB=2\n"


async def deploy_staging(h: Harness, *, dry_run: bool = False, force: bool = False) -> None:
    """Deploy the staging web service (it stores a post-deploy command)."""
    await h.deploy(h.seed.services.staging_web.id, dry_run=dry_run, force=force)


async def test_a_real_deploy_is_one_span_with_ids_and_the_result_and_is_counted(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    outcome = await h.deploy(h.seed.services.staging_web.id)
    assert outcome.status is DeployStatus.SUCCESS

    span = telemetry_recorder.span(telemetry.DEPLOY_SPAN)
    deployment, service = h.seed.deployments.rentals_staging, h.seed.services.staging_web
    attributes = dict(span.attributes or {})
    assert attributes == {
        telemetry.PROJECT_ID: deployment.project_id,
        telemetry.DEPLOYMENT_ID: deployment.id,
        telemetry.DEPLOYMENT_NAME: deployment.name,
        telemetry.SERVICE_ID: service.id,
        telemetry.SERVICE_NAME: service.name,
        telemetry.ACTOR_TYPE: "user",
        telemetry.DRY_RUN: False,
        telemetry.FORCED: False,
        telemetry.ENVIRONMENT_COUNT: len(outcome.environments),
        telemetry.RESULT: "success",
    }
    assert span.status.status_code is not StatusCode.ERROR
    assert telemetry_recorder.counted("envmanager.deploys") == {(("result", "success"),): 1}
    (duration,) = telemetry_recorder.points("envmanager.deploy.duration")
    assert duration.count == 1 and dict(duration.attributes) == {"result": "success"}


async def test_the_deploy_checks_are_child_spans_of_the_deploy(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    await deploy_staging(h)

    deploy = telemetry_recorder.span(telemetry.DEPLOY_SPAN)
    for name in (telemetry.DIST_CHECK_SPAN, telemetry.DRIFT_CHECK_SPAN, telemetry.POST_DEPLOY_SPAN):
        children = telemetry_recorder.spans(name)
        assert children, name
        for child in children:
            assert child.context.trace_id == deploy.context.trace_id
    post_deploy = telemetry_recorder.span(telemetry.POST_DEPLOY_SPAN)
    assert post_deploy.parent is not None and post_deploy.parent.span_id == deploy.context.span_id


async def test_a_dry_run_is_a_span_but_is_not_counted_as_a_deploy(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    await deploy_staging(harness, dry_run=True)

    span = telemetry_recorder.span(telemetry.DEPLOY_SPAN)
    assert span.attributes is not None and span.attributes[telemetry.DRY_RUN] is True
    assert telemetry_recorder.counted("envmanager.deploys") == {}
    assert telemetry_recorder.spans(telemetry.POST_DEPLOY_SPAN) == []


async def test_a_deploy_refused_for_the_lock_counts_as_refused(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    with hold_deploy_lock(h.lock_dir, str(h.roots["staging"])):
        await deploy_staging(h)

    span = telemetry_recorder.span(telemetry.DEPLOY_SPAN)
    assert span.attributes is not None and span.attributes[telemetry.RESULT] == "refused"
    assert telemetry_recorder.counted("envmanager.deploys") == {(("result", "refused"),): 1}


async def test_a_failed_dist_check_is_counted_by_its_problem(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    await link_dist(h.db, h.seed.figures.tuning, "environments/not-there.dist")
    await deploy_staging(h, dry_run=True)

    failed = [
        span
        for span in telemetry_recorder.spans(telemetry.DIST_CHECK_SPAN)
        if (span.attributes or {}).get(telemetry.DIST_OUTCOME) == "failed"
    ]
    assert len(failed) == 1
    attributes = dict(failed[0].attributes or {})
    assert attributes[telemetry.ENVIRONMENT_ID] == h.seed.figures.tuning.id
    assert attributes[telemetry.ENVIRONMENT_NAME] == h.seed.figures.tuning.name
    problem = str(attributes[telemetry.DIST_PROBLEM])
    assert telemetry_recorder.counted("envmanager.dist_check.failures") == {
        (("problem", problem),): 1
    }


async def test_a_dist_check_that_passes_is_not_a_failure(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    await deploy_staging(h, dry_run=True)

    outcomes = {
        (span.attributes or {}).get(telemetry.DIST_OUTCOME)
        for span in telemetry_recorder.spans(telemetry.DIST_CHECK_SPAN)
    }
    assert outcomes, "the dry run checked at least one file"
    assert "failed" not in outcomes
    assert telemetry_recorder.counted("envmanager.dist_check.failures") == {}


async def test_drift_found_by_a_deploy_is_a_detection_with_its_state_on_the_span(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    web = h.seed.envs.staging_web
    await deploy_staging(h)
    assert telemetry_recorder.counted("envmanager.drift.detections") == {}
    (h.roots["staging"] / web.target_path).write_text("EDITED=1\n", encoding="utf-8")
    await deploy_staging(h)

    states = [
        (span.attributes or {}).get(telemetry.DRIFT_STATE)
        for span in telemetry_recorder.spans(telemetry.DRIFT_CHECK_SPAN)
    ]
    assert DriftState.DRIFT.value in states and DriftState.IN_SYNC.value in states
    assert telemetry_recorder.counted("envmanager.drift.detections") == {(("source", "deploy"),): 1}


async def test_drift_seen_in_the_drift_view_is_a_detection_from_the_view(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    web = h.seed.envs.staging_web
    await create_version(h.db, h.crypto, web.id, MANAGED, author=h.seed.users.admin)
    await h.db.commit()
    header = await load_editor_header(h.db, await h.scope_of("superadmin"), web.id)
    assert header is not None
    await h.deploy(h.seed.services.staging_web.id, environments=[web])
    (h.roots["staging"] / web.target_path).write_text("A=9\n", encoding="utf-8")

    view = await load_drift_view(h.db, h.crypto, header)

    assert view is not None and view.kind is DriftKind.DRIFT
    viewed = [
        span
        for span in telemetry_recorder.spans(telemetry.DRIFT_CHECK_SPAN)
        if (span.attributes or {}).get(telemetry.PROJECT_ID) is not None
    ]
    (span,) = viewed
    assert dict(span.attributes or {}) == {
        telemetry.PROJECT_ID: header.project_id,
        telemetry.DEPLOYMENT_ID: header.deployment_id,
        telemetry.SERVICE_ID: header.service_id,
        telemetry.ENVIRONMENT_ID: header.id,
        telemetry.DRIFT_STATE: "drift",
    }
    assert telemetry_recorder.counted("envmanager.drift.detections") == {(("source", "view"),): 1}


@pytest.mark.parametrize(
    ("result", "label", "exit_code"),
    [
        (PostDeployResult(PostDeployStatus.RAN, exit_code=0, output="done"), "ran", 0),
        (PostDeployResult(PostDeployStatus.FAILED, exit_code=3, output="bad"), "failed", 3),
        (PostDeployResult(PostDeployStatus.SKIPPED, reason="No docker."), "skipped", None),
    ],
)
async def test_a_post_deploy_command_is_a_span_and_is_counted_by_result(
    harness: Harness,
    telemetry_recorder: Telemetry,
    result: PostDeployResult,
    label: str,
    exit_code: int | None,
) -> None:
    h = harness
    h.runner.result = result
    await deploy_staging(h)

    span = telemetry_recorder.span(telemetry.POST_DEPLOY_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes[telemetry.RESULT] == label
    assert attributes.get(telemetry.EXIT_CODE) == exit_code
    assert attributes[telemetry.SERVICE_ID] == h.seed.services.staging_web.id
    assert telemetry_recorder.counted("envmanager.post_deploy.runs") == {(("result", label),): 1}
    (duration,) = telemetry_recorder.points("envmanager.post_deploy.duration")
    assert duration.count == 1 and dict(duration.attributes) == {"result": label}


async def test_a_post_deploy_runner_that_raises_is_an_error_span_and_counts_as_error(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    h.runner.error = RuntimeError("the runner broke on seed-secret")
    await deploy_staging(h)

    span = telemetry_recorder.span(telemetry.POST_DEPLOY_SPAN)
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})[telemetry.ERROR_TYPE] == "RuntimeError"
    assert "seed-secret" not in telemetry_recorder.everything_recorded()
    assert telemetry_recorder.counted("envmanager.post_deploy.runs") == {(("result", "error"),): 1}


async def test_a_service_without_a_command_has_no_post_deploy_span(
    harness: Harness, telemetry_recorder: Telemetry
) -> None:
    h = harness
    await link_dist(h.db, h.seed.envs.db, None)
    outcome = await h.deploy(h.seed.services.mysql.id, environments=[h.seed.envs.db])

    assert outcome.status is DeployStatus.SUCCESS
    assert telemetry_recorder.spans(telemetry.POST_DEPLOY_SPAN) == []
    assert telemetry_recorder.counted("envmanager.post_deploy.runs") == {}
