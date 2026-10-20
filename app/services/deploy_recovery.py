"""Closing deploy runs whose post-deploy command was cut short by a stop of the app.

While a post-deploy command runs, its run reads ``running``. The deploy lock is the proof that
something is still working on it: a held lock means a live deploy (possibly in the CLI process),
a free lock means the process that was running it is gone. On startup every run still marked
running whose lock is free is closed as a failed post-deploy with a fixed reason and audited, so
no page keeps showing a state that can never end.
"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import get_logger
from app.models import (
    ApiToken,
    AuditActorType,
    AuditResult,
    Deployment,
    DeployResult,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    EnvironmentVersion,
    PostDeployStatus,
    Service,
    User,
)
from app.models.types import utc_now
from app.services.audit import AuditActor, record_actor_audit
from app.services.deploy_lock import (
    DeployLockBusyError,
    DeployLockUnavailableError,
    hold_deploy_lock,
)
from app.services.deploy_records import ACTION_DEPLOY

log = get_logger("deploy_recovery")

INTERRUPTED_REASON = "The deploy was stopped while the command was running."

_ACTOR_TYPES = {
    DeployTrigger.UI: AuditActorType.USER,
    DeployTrigger.API: AuditActorType.API,
    DeployTrigger.CLI: AuditActorType.CLI,
    DeployTrigger.COMPOSE: AuditActorType.USER,
}


def _deploy_in_progress(lock_dir: Path, deployment_root: str) -> bool:
    """Whether another process holds the deploy lock of the root right now."""
    try:
        with hold_deploy_lock(lock_dir, deployment_root):
            return False
    except DeployLockBusyError:
        return True
    except DeployLockUnavailableError:
        return False


async def _audit_interrupted(
    db: AsyncSession, run: DeployRun, deployment: Deployment | None
) -> None:
    """Add the deploy audit entry the interrupted run never got."""
    user = await db.get(User, run.triggered_by_user_id) if run.triggered_by_user_id else None
    token = await db.get(ApiToken, run.triggered_by_token_id) if run.triggered_by_token_id else None
    rows = await db.execute(
        select(DeployResult, EnvironmentVersion.number)
        .outerjoin(EnvironmentVersion, EnvironmentVersion.id == DeployResult.version_id)
        .where(DeployResult.run_id == run.id)
        .order_by(DeployResult.id)
    )
    actor = AuditActor(_ACTOR_TYPES[run.trigger], user=user, token=token)
    entry = await record_actor_audit(
        db,
        actor,
        action=ACTION_DEPLOY,
        result=AuditResult.FAILURE,
        target_type="service",
        target_id=run.service_id,
        target_name=run.service_name,
        project_id=deployment.project_id if deployment else None,
        deployment_id=deployment.id if deployment else None,
        service_id=run.service_id,
        details={
            "run_id": run.id,
            "dry_run": False,
            "force": run.force,
            "status": run.status.value,
            "post_deploy": run.post_deploy_status.value,
            "post_deploy_reason": run.post_deploy_reason,
            "interrupted": True,
            "environments": [
                {
                    "environment": result.environment_name,
                    "version": number,
                    "status": result.status.value,
                }
                for result, number in rows.all()
            ],
        },
    )
    run.audit_entry_id = entry.id


async def recover_interrupted_runs(db: AsyncSession, lock_dir: Path) -> int:
    """Close the runs left in the running state by a process that no longer exists.

    A run whose deployment lock is held belongs to a live deploy and is left alone. Returns the
    number of runs closed and commits.
    """
    rows = (
        await db.execute(
            select(DeployRun, Deployment)
            .outerjoin(Service, Service.id == DeployRun.service_id)
            .outerjoin(Deployment, Deployment.id == Service.deployment_id)
            .where(DeployRun.post_deploy_status == PostDeployStatus.RUNNING)
            .order_by(DeployRun.id)
        )
    ).all()
    closed = 0
    for run, deployment in rows:
        if deployment is not None and _deploy_in_progress(lock_dir, deployment.root_path):
            continue
        run.post_deploy_status = PostDeployStatus.FAILED
        run.post_deploy_reason = INTERRUPTED_REASON
        run.status = DeployStatus.POST_DEPLOY_FAILED
        run.finished_at = utc_now()
        await _audit_interrupted(db, run, deployment)
        closed += 1
    if closed:
        await db.commit()
        log.warning("closed %s deploy runs interrupted while a command was running", closed)
    return closed
