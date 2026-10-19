"""Recording a deploy: the run, one result per environment and the audit entries.

Everything here only adds rows to the session; the engine commits. Results hold relative paths,
hashes and fixed messages, and audit details hold names, version numbers and outcomes, never a
value or a host path.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditActorType,
    AuditResult,
    Deployment,
    DeployResult,
    DeployRun,
    DeployStatus,
    DeployTrigger,
    PostDeployStatus,
    Service,
)
from app.models.types import utc_now
from app.services.audit import AuditActor, record_actor_audit
from app.services.deploy_types import (
    DeployRefusal,
    DeployRequest,
    EnvironmentOutcome,
)
from app.services.hooks import PostDeployResult

ACTION_DEPLOY = "service.deploy"
ACTION_DRY_RUN = "service.deploy_dry_run"
MESSAGE_LIMIT = 500

_TRIGGERS = {
    AuditActorType.USER: DeployTrigger.UI,
    AuditActorType.API: DeployTrigger.API,
}
_AUDIT_RESULTS = {
    DeployStatus.SUCCESS: AuditResult.SUCCESS,
    DeployStatus.HALTED: AuditResult.HALTED,
    DeployStatus.BLOCKED: AuditResult.BLOCKED,
}


def truncate(text: str | None) -> str | None:
    """Fit a message into the column that stores it."""
    return text[:MESSAGE_LIMIT] if text is not None else None


async def record_run(
    db: AsyncSession,
    *,
    service: Service,
    deployment: Deployment,
    actor: AuditActor,
    request: DeployRequest,
    started_at: datetime,
    status: DeployStatus,
    outcomes: tuple[EnvironmentOutcome, ...],
    post_deploy: PostDeployResult,
) -> DeployRun:
    """Add the run and one result per environment to the session and return the flushed run."""
    run = DeployRun(
        service_id=service.id,
        service_name=service.name,
        deployment_name=deployment.name,
        triggered_by_user_id=actor.user.id if actor.user and actor.token is None else None,
        triggered_by_token_id=actor.token.id if actor.token else None,
        trigger=_TRIGGERS.get(actor.type, DeployTrigger.CLI),
        dry_run=request.dry_run,
        force=request.force,
        status=status,
        post_deploy_status=post_deploy.status,
        post_deploy_exit_code=post_deploy.exit_code,
        post_deploy_output=post_deploy.output,
        post_deploy_reason=truncate(post_deploy.reason),
        started_at=started_at,
        finished_at=None if post_deploy.status is PostDeployStatus.RUNNING else utc_now(),
    )
    db.add(run)
    await db.flush()
    for outcome in outcomes:
        db.add(
            DeployResult(
                run_id=run.id,
                environment_id=outcome.environment_id,
                environment_name=outcome.name,
                version_id=outcome.version_id,
                target_path=outcome.target_path,
                status=outcome.status,
                message=truncate(outcome.message),
                target_hash_before=outcome.target_hash_before,
                written_hash=outcome.written_hash,
                backup_path=outcome.backup_path,
                dist_outcome=outcome.dist,
                drift_state=outcome.drift,
            )
        )
    await db.flush()
    return run


async def audit_run(
    db: AsyncSession,
    *,
    service: Service,
    deployment: Deployment,
    actor: AuditActor,
    request: DeployRequest,
    run: DeployRun,
    outcomes: tuple[EnvironmentOutcome, ...],
) -> None:
    """Audit the run with the actor, the versions involved, the force flag and each outcome.

    The post-deploy exit code, truncated output and reason are part of the details. Command text
    never is: admins read the audit log and only the superadmin may see commands.
    """
    entry = await record_actor_audit(
        db,
        actor,
        action=ACTION_DRY_RUN if request.dry_run else ACTION_DEPLOY,
        result=_AUDIT_RESULTS.get(run.status, AuditResult.FAILURE),
        target_type="service",
        target_id=service.id,
        target_name=service.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=service.id,
        details={
            "run_id": run.id,
            "dry_run": request.dry_run,
            "force": request.force,
            "status": run.status.value,
            "post_deploy": run.post_deploy_status.value,
            "post_deploy_exit_code": run.post_deploy_exit_code,
            "post_deploy_output": run.post_deploy_output,
            "post_deploy_reason": run.post_deploy_reason,
            "environments": [
                {
                    "environment": outcome.name,
                    "version": outcome.version_number,
                    "status": outcome.status.value,
                    "dist": outcome.dist.value if outcome.dist else None,
                    "drift": outcome.drift.value if outcome.drift else None,
                    "forced": outcome.forced,
                }
                for outcome in outcomes
            ],
        },
        version_ids=[outcome.version_id for outcome in outcomes if outcome.version_id is not None],
    )
    run.audit_entry_id = entry.id


async def audit_refusal(
    db: AsyncSession,
    *,
    service: Service,
    deployment: Deployment,
    actor: AuditActor,
    request: DeployRequest,
    environment_names: list[str],
    refusal: DeployRefusal,
) -> None:
    """Audit a deploy that did not start because the lock is held or unusable."""
    await record_actor_audit(
        db,
        actor,
        action=ACTION_DEPLOY,
        result=AuditResult.FAILURE,
        target_type="service",
        target_id=service.id,
        target_name=service.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=service.id,
        details={
            "dry_run": False,
            "force": request.force,
            "status": DeployStatus.FAILED.value,
            "refused": refusal.value,
            "environments": environment_names,
        },
    )
