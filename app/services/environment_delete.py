"""Environment delete, superadmin only and audited, behind the token restriction guard."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Environment, EnvironmentVersion, User
from app.services.audit import AuditActor, AuditedEnvironment, record_environment_audit
from app.services.token_guard import guard_delete, restrictions_below


class EnvironmentInUseError(Exception):
    """API tokens are restricted to the environment, so it is not deleted."""


async def delete_environment(
    db: AsyncSession,
    environment: Environment,
    reference: AuditedEnvironment,
    *,
    actor: User,
    ip_address: str | None = None,
) -> None:
    """Delete a file with all its versions and audit it.

    Refused with ``EnvironmentInUseError`` while a usable API token is restricted to the file
    (the refusal is audited); restrictions of revoked or expired tokens go with it. The file on
    the host is not touched, and deploy history keeps the copied names.
    """
    who = AuditActor.for_user(actor, ip_address)
    tokens = await guard_delete(
        db,
        who,
        await restrictions_below(db, environment_id=environment.id),
        action="environment.delete",
        target_type="environment",
        target_id=environment.id,
        target_name=environment.name,
        project_id=reference.project_id,
        deployment_id=reference.deployment_id,
        service_id=reference.service_id,
        environment_id=environment.id,
    )
    if tokens:
        raise EnvironmentInUseError(
            "API tokens are restricted to this file: " + ", ".join(tokens) + ". Revoke them first."
        )
    versions = (
        await db.execute(
            select(func.count())
            .select_from(EnvironmentVersion)
            .where(EnvironmentVersion.environment_id == environment.id)
        )
    ).scalar_one()
    await record_environment_audit(
        db,
        who,
        reference,
        action="environment.delete",
        details={"versions": versions, "target_path": environment.target_path},
    )
    await db.delete(environment)
    await db.commit()
