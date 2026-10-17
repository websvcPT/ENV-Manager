"""Service structure: name checks and superadmin-only creation of a service in a deployment."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditActorType, AuditResult, Deployment, Service, User
from app.services.audit import record_audit
from app.services.projects import MAX_PROJECT_NAME_LENGTH, PROJECT_NAME_PATTERN


class ServiceNameError(Exception):
    """A service name was refused; the message is safe to show next to the field."""


async def validate_service_name(db: AsyncSession, deployment_id: int, raw: str) -> str:
    """The trimmed service name when it is acceptable, else ``ServiceNameError``.

    Names follow the structure name rule and are unique within the deployment.
    """
    name = raw.strip()
    if not name:
        raise ServiceNameError("Enter a service name.")
    if len(name) > MAX_PROJECT_NAME_LENGTH or PROJECT_NAME_PATTERN.fullmatch(name) is None:
        raise ServiceNameError(
            "Use 1 to 100 lowercase letters, numbers and dashes, starting with a letter or number."
        )
    taken = select(Service.id).where(Service.deployment_id == deployment_id, Service.name == name)
    if (await db.execute(taken)).first() is not None:
        raise ServiceNameError(f"A service named {name} already exists in this deployment.")
    return name


async def add_service(
    db: AsyncSession,
    deployment: Deployment,
    name: str,
    *,
    actor: User,
    ip_address: str | None = None,
) -> Service:
    """Add a validated service and its audit entry to the session without committing."""
    service = Service(deployment_id=deployment.id, name=name)
    db.add(service)
    await db.flush()
    await record_audit(
        db,
        action="service.create",
        result=AuditResult.SUCCESS,
        actor_type=AuditActorType.USER,
        actor_user=actor,
        ip_address=ip_address,
        target_type="service",
        target_id=service.id,
        target_name=service.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=service.id,
    )
    return service


async def create_service(
    db: AsyncSession,
    deployment: Deployment,
    raw_name: str,
    *,
    actor: User,
    ip_address: str | None = None,
) -> Service:
    """Validate the name, create the service and commit. Raises ``ServiceNameError``."""
    name = await validate_service_name(db, deployment.id, raw_name)
    service = await add_service(db, deployment, name, actor=actor, ip_address=ip_address)
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise ServiceNameError(
            f"A service named {name} already exists in this deployment."
        ) from error
    return service
