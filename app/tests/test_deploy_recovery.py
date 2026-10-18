"""Runs left in the running state by a process that is gone are closed, live ones are not."""

from fastapi import FastAPI
from sqlalchemy import select

from app.main import create_app
from app.models import (
    AuditEntry,
    AuditResult,
    DeployResult,
    DeployResultStatus,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    PostDeployStatus,
)
from app.models.types import utc_now
from app.services.deploy_lock import hold_deploy_lock
from app.services.deploy_recovery import INTERRUPTED_REASON, recover_interrupted_runs
from app.tests.deploy_helpers import Harness, audit_of


async def running_run(h: Harness, *, service_id: int | None) -> DeployRun:
    """A run of the staging web service that reads running, as a crashed deploy leaves it."""
    run = DeployRun(
        service_id=service_id,
        service_name="web",
        deployment_name="staging",
        triggered_by_user_id=h.seed.users.admin.id,
        trigger=DeployTrigger.UI,
        force=True,
        status=DeployStatus.SUCCESS,
        post_deploy_status=PostDeployStatus.RUNNING,
        started_at=utc_now(),
    )
    h.db.add(run)
    await h.db.flush()
    h.db.add(
        DeployResult(
            run_id=run.id,
            environment_id=h.seed.figures.tuning.id,
            environment_name=h.seed.figures.tuning.name,
            target_path="environments/.env.web.tuning",
            status=DeployResultStatus.WRITTEN,
        )
    )
    await h.db.commit()
    return run


async def test_a_run_whose_lock_is_free_is_closed_as_a_failed_post_deploy(harness: Harness) -> None:
    h = harness
    run = await running_run(h, service_id=h.seed.services.staging_web.id)
    assert await recover_interrupted_runs(h.db, h.lock_dir) == 1

    await h.db.refresh(run)
    assert run.post_deploy_status is PostDeployStatus.FAILED
    assert run.status is DeployStatus.POST_DEPLOY_FAILED
    assert run.post_deploy_reason == INTERRUPTED_REASON and run.finished_at is not None
    (entry,) = [e for e in await audit_of(h.db, "service.deploy") if e.id == run.audit_entry_id]
    assert entry.result is AuditResult.FAILURE and entry.actor_name == "admin"
    assert entry.details["interrupted"] is True and entry.details["force"] is True
    assert entry.details["post_deploy"] == "failed" and entry.details["run_id"] == run.id
    assert entry.details["environments"] == [
        {"environment": ".env.web.tuning", "version": None, "status": "written"}
    ]
    assert entry.service_id == h.seed.services.staging_web.id


async def test_a_run_whose_lock_is_held_belongs_to_a_live_deploy_and_is_left_alone(
    harness: Harness,
) -> None:
    h = harness
    run = await running_run(h, service_id=h.seed.services.staging_web.id)
    with hold_deploy_lock(h.lock_dir, str(h.roots["staging"])):
        assert await recover_interrupted_runs(h.db, h.lock_dir) == 0
    await h.db.refresh(run)
    assert run.post_deploy_status is PostDeployStatus.RUNNING and run.finished_at is None
    assert await recover_interrupted_runs(h.db, h.lock_dir) == 1


async def test_closing_is_done_once_and_never_touches_finished_runs(harness: Harness) -> None:
    h = harness
    before = {run.id: run.post_deploy_status for run in (await h.db.scalars(select(DeployRun)))}
    assert PostDeployStatus.RUNNING not in before.values()
    await running_run(h, service_id=h.seed.services.staging_web.id)
    assert await recover_interrupted_runs(h.db, h.lock_dir) == 1
    assert await recover_interrupted_runs(h.db, h.lock_dir) == 0
    after = {run.id: run.post_deploy_status for run in (await h.db.scalars(select(DeployRun)))}
    assert {key: value for key, value in after.items() if key in before} == before


async def test_a_run_of_a_deleted_service_is_closed_too(harness: Harness) -> None:
    h = harness
    run = await running_run(h, service_id=None)
    assert await recover_interrupted_runs(h.db, h.lock_dir) == 1
    await h.db.refresh(run)
    assert run.post_deploy_status is PostDeployStatus.FAILED
    entry = await h.db.get(AuditEntry, run.audit_entry_id)
    assert entry is not None and entry.project_id is None and entry.service_id is None


async def test_the_app_closes_interrupted_runs_before_it_serves(web_app: FastAPI) -> None:
    async with web_app.state.session_factory() as db:
        run = DeployRun(
            service_name="web",
            deployment_name="staging",
            trigger=DeployTrigger.CLI,
            status=DeployStatus.SUCCESS,
            post_deploy_status=PostDeployStatus.RUNNING,
            started_at=utc_now(),
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    restarted = create_app()
    async with restarted.router.lifespan_context(restarted):
        pass
    async with web_app.state.session_factory() as db:
        stored = await db.get(DeployRun, run_id)
        assert stored is not None
        assert stored.post_deploy_status is PostDeployStatus.FAILED
        assert stored.post_deploy_reason == INTERRUPTED_REASON
