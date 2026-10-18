"""The deploy engine's lock, post-deploy hook seam, notification seam and secrecy."""

import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.db import create_session_factory
from app.models import (
    AuditActorType,
    AuditEntry,
    AuditResult,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DriftPolicy,
    PostDeployStatus,
)
from app.seeds import EDITOR_SECRET_VALUES
from app.services.audit import record_audit
from app.services.deploy import LOCK_UNAVAILABLE_MESSAGE, LOCKED_MESSAGE, DeployEngine
from app.services.deploy_lock import DeployLockBusyError, hold_deploy_lock
from app.services.deploy_types import DeployOutcome, DeployRefusal
from app.services.hooks import COMMANDS_UNAVAILABLE_REASON, PostDeployResult
from app.tests.deploy_helpers import (
    Harness,
    RecordingNotifier,
    audit_of,
    files_in,
    require_extra_key,
    set_deployment,
    set_environment,
)


def staging_root(h: Harness) -> str:
    """The real root of the staging deployment."""
    return str(h.roots["staging"])


async def deploy_staging(
    h: Harness, *, dry_run: bool = False, force: bool = False, only_tuning: bool = False
) -> DeployOutcome:
    """Deploy the staging web service, or only its tuning file."""
    return await h.deploy(
        h.seed.services.staging_web.id,
        environments=[h.seed.figures.tuning] if only_tuning else None,
        dry_run=dry_run,
        force=force,
    )


async def count_runs(h: Harness) -> int:
    """The number of recorded deploy runs."""
    return (await h.db.execute(select(func.count()).select_from(DeployRun))).scalar_one()


async def test_a_deploy_while_the_root_is_locked_is_refused_with_a_clear_result(
    harness: Harness,
) -> None:
    h = harness
    runs_before = await count_runs(h)
    with hold_deploy_lock(h.lock_dir, staging_root(h)):
        outcome = await deploy_staging(h, force=True)

    assert outcome.refusal is DeployRefusal.LOCKED
    assert outcome.status is DeployStatus.FAILED and outcome.message == LOCKED_MESSAGE
    assert outcome.run_id is None and outcome.environments == ()
    assert files_in(h.roots["staging"] / "environments") == []
    assert await count_runs(h) == runs_before
    (entry,) = await audit_of(h.db, "service.deploy")
    assert entry.result is AuditResult.FAILURE
    assert entry.details["refused"] == "locked" and entry.details["force"] is True
    assert entry.actor_name == "admin"
    assert h.runner.requests == [] and h.notifier.events == []

    assert (await deploy_staging(h)).status is DeployStatus.SUCCESS


async def test_two_deployments_sharing_a_root_never_deploy_at_the_same_time(
    harness: Harness,
) -> None:
    h = harness
    alias = h.roots["staging"].parent / "alias"
    alias.symlink_to(h.roots["staging"])
    await set_deployment(h.db, h.seed.deployments.billing_main.id, root_path=str(alias))
    with hold_deploy_lock(h.lock_dir, staging_root(h)):
        outcome = await h.deploy(h.seed.services.api.id)
    assert outcome.refusal is DeployRefusal.LOCKED
    assert files_in(h.roots["staging"] / "environments") == []


async def test_a_lock_folder_that_cannot_be_used_refuses_the_deploy(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    h.engine = DeployEngine(h.crypto, blocker, hook_runner=h.runner, notifier=h.notifier)
    outcome = await deploy_staging(h)
    assert outcome.refusal is DeployRefusal.LOCK_UNAVAILABLE
    assert outcome.message == LOCK_UNAVAILABLE_MESSAGE
    assert files_in(h.roots["staging"] / "environments") == []


async def test_a_dry_run_is_not_refused_while_the_root_is_locked(harness: Harness) -> None:
    h = harness
    with hold_deploy_lock(h.lock_dir, staging_root(h)):
        outcome = await deploy_staging(h, dry_run=True)
    assert outcome.refusal is None and outcome.status is DeployStatus.SUCCESS


async def test_the_lock_is_held_while_the_post_deploy_step_runs_and_released_after(
    harness: Harness,
) -> None:
    h = harness
    seen: dict[str, object] = {}

    async def during_post_deploy() -> None:
        seen["nested"] = await deploy_staging(h, only_tuning=True)
        try:
            with hold_deploy_lock(h.lock_dir, staging_root(h)):
                seen["busy"] = False
        except DeployLockBusyError:
            seen["busy"] = True

    h.runner.on_run = during_post_deploy
    outcome = await deploy_staging(h)
    assert outcome.status is DeployStatus.SUCCESS
    nested = seen["nested"]
    assert isinstance(nested, DeployOutcome) and nested.refusal is DeployRefusal.LOCKED
    assert seen["busy"] is True
    with hold_deploy_lock(h.lock_dir, staging_root(h)):
        pass


async def test_the_database_is_not_write_locked_while_the_post_deploy_step_runs(
    harness: Harness,
) -> None:
    h = harness
    wrote: list[bool] = []

    async def write_elsewhere() -> None:
        async with create_session_factory(h.database)() as other:
            await record_audit(
                other, action="probe", result=AuditResult.SUCCESS, actor_type=AuditActorType.SYSTEM
            )
            await other.commit()
        wrote.append(True)

    h.runner.on_run = write_elsewhere
    assert (await deploy_staging(h)).status is DeployStatus.SUCCESS
    assert wrote == [True]


async def test_post_deploy_is_called_once_after_everything_was_written(harness: Harness) -> None:
    h = harness
    outcome = await deploy_staging(h)
    (request,) = h.runner.requests
    assert (request.service_name, request.deployment_name) == ("web", "staging")
    assert request.service_id == h.seed.services.staging_web.id
    assert request.deployment_root == h.roots["staging"]
    assert request.changed and not request.force
    assert request.hook is not None and "docker compose" in request.hook.commands
    assert outcome.post_deploy.status is PostDeployStatus.RAN
    run = await h.db.get(DeployRun, outcome.run_id)
    assert run is not None
    assert (run.post_deploy_status, run.post_deploy_exit_code, run.post_deploy_output) == (
        PostDeployStatus.RAN,
        0,
        "done",
    )


async def test_post_deploy_is_called_when_everything_was_unchanged_and_says_so(
    harness: Harness,
) -> None:
    h = harness
    await deploy_staging(h, only_tuning=True)
    await deploy_staging(h, only_tuning=True, force=True)
    assert [(request.changed, request.force) for request in h.runner.requests] == [
        (True, False),
        (False, True),
    ]


@pytest.mark.parametrize("situation", ["dry_run", "blocked", "halted", "failed"])
async def test_post_deploy_is_not_called_on_a_dry_run_block_halt_or_failure(
    harness: Harness, situation: str
) -> None:
    h = harness
    tuning = h.seed.figures.tuning
    target = h.roots["staging"] / "environments" / tuning.name
    expected = {
        "dry_run": (DeployStatus.SUCCESS, PostDeployStatus.NONE),
        "blocked": (DeployStatus.BLOCKED, PostDeployStatus.SKIPPED),
        "halted": (DeployStatus.HALTED, PostDeployStatus.SKIPPED),
        "failed": (DeployStatus.FAILED, PostDeployStatus.SKIPPED),
    }[situation]
    if situation == "blocked":
        await require_extra_key(h)
    if situation == "halted":
        target.write_text("EDITED=1\n", encoding="utf-8")
    if situation == "failed":
        await set_environment(h.db, tuning.id, target_path="no-such-folder/.env")
    outcome = await deploy_staging(h, dry_run=situation == "dry_run", only_tuning=True)
    assert (outcome.status, outcome.post_deploy.status) == expected
    assert h.runner.requests == []
    if situation != "dry_run":
        assert outcome.post_deploy.reason == "Not run because an environment was not deployed."


async def test_a_service_without_a_command_records_none_and_a_stored_command_a_skip(
    harness: Harness,
) -> None:
    h = harness
    h.engine = DeployEngine(h.crypto, h.lock_dir)
    with_command = await deploy_staging(h)
    without_command = await h.deploy(h.seed.services.api.id)
    assert with_command.post_deploy == PostDeployResult(
        PostDeployStatus.SKIPPED, reason=COMMANDS_UNAVAILABLE_REASON
    )
    assert with_command.status is DeployStatus.SUCCESS
    assert without_command.post_deploy == PostDeployResult(PostDeployStatus.NONE)
    run = await h.db.get(DeployRun, with_command.run_id)
    assert run is not None and run.post_deploy_reason == COMMANDS_UNAVAILABLE_REASON


async def test_a_failed_post_deploy_marks_the_run_deployed_with_post_deploy_failed(
    harness: Harness,
) -> None:
    h = harness
    h.runner.result = PostDeployResult(
        PostDeployStatus.FAILED, exit_code=3, output="boom", reason="exit 3"
    )
    outcome = await deploy_staging(h)
    assert outcome.status is DeployStatus.POST_DEPLOY_FAILED
    assert {result.status for result in outcome.environments} == {DeployResultStatus.WRITTEN}
    run = await h.db.get(DeployRun, outcome.run_id)
    assert run is not None and run.status is DeployStatus.POST_DEPLOY_FAILED
    assert (run.post_deploy_exit_code, run.post_deploy_output) == (3, "boom")
    (entry,) = [
        entry
        for entry in await audit_of(h.db, "service.deploy")
        if entry.details["run_id"] == run.id
    ]
    assert entry.result is AuditResult.FAILURE and entry.details["post_deploy"] == "failed"


async def test_a_post_deploy_runner_that_raises_counts_as_failed_without_leaking(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h = harness
    h.runner.error = RuntimeError("secret-looking-detail-1234")
    caplog.set_level(logging.DEBUG)
    outcome = await deploy_staging(h)
    assert outcome.status is DeployStatus.POST_DEPLOY_FAILED
    assert outcome.post_deploy.reason == "The post-deploy runner failed unexpectedly."
    assert "secret-looking-detail-1234" not in caplog.text


async def test_one_event_is_emitted_after_the_lock_is_released(harness: Harness) -> None:
    h = harness
    lock_free: list[bool] = []

    def probe() -> None:
        try:
            with hold_deploy_lock(h.lock_dir, staging_root(h)):
                lock_free.append(True)
        except DeployLockBusyError:
            lock_free.append(False)

    h.notifier.on_event = probe
    outcome = await deploy_staging(h, force=True)
    (event,) = h.notifier.events
    assert lock_free == [True]
    assert event.run_id == outcome.run_id and event.actor == "admin" and event.force
    assert (event.service_name, event.deployment_name) == ("web", "staging")
    assert event.status is DeployStatus.SUCCESS and event.post_deploy is PostDeployStatus.RAN
    assert {(env.name, env.status) for env in event.environments} == {
        (".env.web", DeployResultStatus.WRITTEN),
        (".env.web.replica", DeployResultStatus.WRITTEN),
        (".env.web.tuning", DeployResultStatus.WRITTEN),
        ("web-ca.pem", DeployResultStatus.WRITTEN),
    }
    assert event.project_id == h.seed.projects.rentals.id


async def test_no_event_is_emitted_for_a_dry_run_or_a_refused_deploy(harness: Harness) -> None:
    h = harness
    await deploy_staging(h, dry_run=True)
    with hold_deploy_lock(h.lock_dir, staging_root(h)):
        await deploy_staging(h)
    assert h.notifier.events == []


async def test_a_notifier_that_fails_does_not_change_the_outcome(harness: Harness) -> None:
    h = harness
    h.engine = DeployEngine(
        h.crypto, h.lock_dir, hook_runner=h.runner, notifier=RecordingNotifier(error=OSError("x"))
    )
    outcome = await deploy_staging(h)
    assert outcome.status is DeployStatus.SUCCESS
    assert (h.roots["staging"] / "environments" / ".env.web").exists()


async def test_no_secret_value_reaches_results_audit_logs_or_messages(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h = harness
    caplog.set_level(logging.DEBUG)
    editor = h.seed.editor
    target = h.roots["prod"] / "environments" / editor.name
    first = await h.deploy(h.seed.services.mysql.id, environments=[editor])
    assert any(value in target.read_text(encoding="utf-8") for value in EDITOR_SECRET_VALUES)
    target.write_text("EDITED=1\n", encoding="utf-8")
    halted = await h.deploy(h.seed.services.mysql.id, environments=[editor])
    await set_deployment(
        h.db, h.seed.deployments.rentals_prod.id, drift_policy=DriftPolicy.OVERWRITE
    )
    dry = await h.deploy(h.seed.services.mysql.id, environments=[editor], dry_run=True)

    rows: list[object] = []
    for model in (AuditEntry, DeployResult, DeployRun):
        for row in (await h.db.execute(select(model))).scalars():
            rows.append(
                {column.name: getattr(row, column.name) for column in row.__table__.columns}
            )
    text = json.dumps(rows, default=str) + repr((first, halted, dry)) + caplog.text
    for value in EDITOR_SECRET_VALUES:
        assert value not in text
