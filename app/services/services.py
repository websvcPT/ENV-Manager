"""Service structure: name checks and superadmin-only create, rename and delete of a service.

A deployment may also hold one service of its own, the deployment files: the files the whole
stack reads (the `.env` beside the Compose file, the Compose files), with their own deploy and
post-deploy command. It is a service row under the reserved name ``DEPLOYMENT_FILES_NAME``,
which the service name rule can never produce, so every rule of services applies to it
unchanged. It is never listed or counted as a service, never renamed or deleted on its own (it
goes with its deployment), and is created on first use.
"""

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DEPLOYMENT_FILES_NAME, AuditResult, Deployment, Environment, Service, User
from app.services.audit import AuditActor, record_actor_audit
from app.services.slug_names import SLUG_MESSAGE, is_slug_name
from app.services.token_guard import guard_delete, restrictions_below

DEPLOYMENT_FILES_MESSAGE = (
    "The deployment files go with their deployment: they are not renamed or deleted on their own."
)


class ServiceNameError(Exception):
    """A service name was refused; the message is safe to show next to the field."""


class ServiceInUseError(Exception):
    """API tokens are restricted to the service or something inside it, so it is not deleted."""


def is_deployment_files(name: str) -> bool:
    """Whether a service name is the reserved name of a deployment's own files."""
    return name == DEPLOYMENT_FILES_NAME


def listed_service() -> ColumnElement[bool]:
    """The condition that keeps a deployment's own files out of service lists and counts."""
    return Service.name != DEPLOYMENT_FILES_NAME


async def find_deployment_files(db: AsyncSession, deployment_id: int) -> Service | None:
    """The deployment files of a deployment, None until they are first used."""
    query = select(Service).where(
        Service.deployment_id == deployment_id, Service.name == DEPLOYMENT_FILES_NAME
    )
    return (await db.execute(query)).scalar_one_or_none()


async def ensure_deployment_files(
    db: AsyncSession, deployment: Deployment, *, actor: User, ip_address: str | None = None
) -> Service:
    """The deployment files of a deployment, added to the session (audited, not committed) when
    they do not exist yet."""
    found = await find_deployment_files(db, deployment.id)
    if found is not None:
        return found
    return await add_service(
        db, deployment, DEPLOYMENT_FILES_NAME, actor=actor, ip_address=ip_address
    )


async def validate_service_name(
    db: AsyncSession, deployment_id: int, raw: str, *, ignore_id: int | None = None
) -> str:
    """The trimmed service name when it is acceptable, else ``ServiceNameError``.

    Names follow the structure name rule and are unique within the deployment. ``ignore_id``
    leaves one service out of the uniqueness check, the one being renamed.
    """
    name = raw.strip()
    if not name:
        raise ServiceNameError("Enter a service name.")
    if not is_slug_name(name):
        raise ServiceNameError(SLUG_MESSAGE)
    taken = select(Service.id).where(Service.deployment_id == deployment_id, Service.name == name)
    if ignore_id is not None:
        taken = taken.where(Service.id != ignore_id)
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
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action="service.create",
        result=AuditResult.SUCCESS,
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


async def rename_service(
    db: AsyncSession,
    service: Service,
    raw_name: str,
    *,
    actor: User,
    ip_address: str | None = None,
) -> Service:
    """Rename a service and audit it. Renaming to the current name changes nothing.

    Raises ``ServiceNameError`` for a name that breaks the rule or is taken in the deployment,
    and for the deployment files. The post-deploy command stays as it was: it names the Compose
    service literally, so the superadmin changes it where the service is configured.
    """
    if is_deployment_files(service.name):
        raise ServiceNameError(DEPLOYMENT_FILES_MESSAGE)
    deployment = await db.get(Deployment, service.deployment_id)
    if deployment is None:
        raise ServiceNameError("The deployment of this service no longer exists.")
    name = await validate_service_name(db, deployment.id, raw_name, ignore_id=service.id)
    if name == service.name:
        return service
    previous = service.name
    service.name = name
    await record_actor_audit(
        db,
        AuditActor.for_user(actor, ip_address),
        action="service.rename",
        result=AuditResult.SUCCESS,
        target_type="service",
        target_id=service.id,
        target_name=name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=service.id,
        details={"previous_name": previous},
    )
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise ServiceNameError(
            f"A service named {name} already exists in this deployment."
        ) from error
    return service


async def delete_service(
    db: AsyncSession, service: Service, *, actor: User, ip_address: str | None = None
) -> None:
    """Delete a service with its environments, versions and post-deploy command, and audit it.

    Refused with ``ServiceInUseError`` while a usable API token is restricted to the service or
    one of its environments (the refusal is audited); restrictions of revoked or expired tokens
    go with it. Deploy history keeps the copied names and loses only the link to the service.
    The deployment files are refused with ``ServiceNameError``: they go with their deployment.
    """
    if is_deployment_files(service.name):
        raise ServiceNameError(DEPLOYMENT_FILES_MESSAGE)
    deployment = await db.get(Deployment, service.deployment_id)
    if deployment is None:
        raise ServiceNameError("The deployment of this service no longer exists.")
    who = AuditActor.for_user(actor, ip_address)
    tokens = await guard_delete(
        db,
        who,
        await restrictions_below(db, service_id=service.id),
        action="service.delete",
        target_type="service",
        target_id=service.id,
        target_name=service.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=service.id,
    )
    if tokens:
        raise ServiceInUseError(
            "API tokens are restricted to this service or its environments: "
            + ", ".join(tokens)
            + ". Revoke them first."
        )
    count = select(func.count()).select_from(Environment)
    environments = (
        await db.execute(count.where(Environment.service_id == service.id))
    ).scalar_one()
    await record_actor_audit(
        db,
        who,
        action="service.delete",
        result=AuditResult.SUCCESS,
        target_type="service",
        target_id=service.id,
        target_name=service.name,
        project_id=deployment.project_id,
        deployment_id=deployment.id,
        service_id=service.id,
        details={"environments": environments},
    )
    await db.delete(service)
    await db.commit()
