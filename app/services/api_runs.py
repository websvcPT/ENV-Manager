"""Deploy runs as the API reports them, read from the stored run and its results.

A deploy and the last result answer from the same records through the same function, so both
have one shape. Only the environments the caller may reach are listed, and a run whose results
all lie outside that set does not exist for the caller. The post-deploy output and command, the
backup path and the file hashes are never read.
"""

from collections.abc import Collection

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeployResult, DeployRun, EnvironmentVersion
from app.schemas.api import EnvironmentResult, PostDeployInfo, RunResult, ServiceInfo


async def _result_of(
    db: AsyncSession, run: DeployRun, service_id: int, environment_ids: Collection[int]
) -> RunResult:
    """The run with the results of the environments in scope, in the order the engine wrote."""
    rows = (
        await db.execute(
            select(DeployResult, EnvironmentVersion.number)
            .outerjoin(EnvironmentVersion, EnvironmentVersion.id == DeployResult.version_id)
            .where(
                DeployResult.run_id == run.id,
                DeployResult.environment_id.in_(environment_ids),
            )
            .order_by(DeployResult.id)
        )
    ).all()
    return RunResult(
        run_id=run.id,
        service=ServiceInfo(id=service_id, name=run.service_name),
        dry_run=run.dry_run,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        environments=[
            EnvironmentResult(
                id=result.environment_id,
                name=result.environment_name,
                version=number,
                status=result.status,
                dist=result.dist_outcome,
                drift=result.drift_state,
                message=result.message,
            )
            for result, number in rows
            if result.environment_id is not None
        ],
        post_deploy=PostDeployInfo(
            status=run.post_deploy_status,
            exit_code=run.post_deploy_exit_code,
            reason=run.post_deploy_reason,
        ),
    )


async def load_run_result(
    db: AsyncSession, run_id: int, service_id: int, environment_ids: Collection[int]
) -> RunResult | None:
    """The run of this service as the caller sees it, or None when it does not exist."""
    run = (
        await db.execute(
            select(DeployRun).where(DeployRun.id == run_id, DeployRun.service_id == service_id)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    return await _result_of(db, run, service_id, environment_ids)


async def load_last_run_result(
    db: AsyncSession, service_id: int, environment_ids: Collection[int]
) -> RunResult | None:
    """The newest real run of the service that has a result inside the scope, or None.

    Dry runs are left out, and so is a run that touched only files the caller cannot reach.
    """
    in_scope = exists().where(
        DeployResult.run_id == DeployRun.id, DeployResult.environment_id.in_(environment_ids)
    )
    run = (
        await db.execute(
            select(DeployRun)
            .where(DeployRun.service_id == service_id, DeployRun.dry_run.is_(False), in_scope)
            .order_by(DeployRun.started_at.desc(), DeployRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    return await _result_of(db, run, service_id, environment_ids)
